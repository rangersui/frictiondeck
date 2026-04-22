"""semantic — Accept/User-Agent driven shape renderer.

    Store strings. Serve shapes.

Full design + rationale: PLAN-semantic-http.md at repo root.

    GET /shaped/<world_path>

    Accept is authoritative:
      * Accept specific and SLM output matches  -> 200 + cache
      * Accept specific and SLM output mismatch -> 406, no cache
      * Accept "*/*" / missing                  -> 200 with SLM-chosen ct

    SLM unavailable (/dev/gpu not installed / /etc/gpu.conf missing /
    backend unreachable):
      * Accept permits text/plain (incl "*/*") -> 200 fallback-raw, no cache
      * Accept excludes text/plain             -> 406 fallback-406, no cache

    Rate cap (SEMANTIC_GEN_CAP_PER_MIN, default 60, sliding 60s window):
      * Exhausted + Accept permits text/plain -> 200 ratelimit-raw, no cache
      * Exhausted + Accept excludes text/plain -> 429 ratelimit-429 + Retry-After

    Cache:
      * Lives as worlds under /var/cache/semantic/<hash>
      * Cache key = sha256(world || ver || UA || Accept ||
                           RENDER_FINGERPRINT || GPU_CONF_FINGERPRINT)
      * RENDER_FINGERPRINT rotates on SYSTEM_PROMPT / MAX_SOURCE change
      * GPU_CONF_FINGERPRINT rotates on /etc/gpu.conf change (operator
        swaps ollama -> claude etc. at runtime -> cache invalidates
        globally, next request re-shapes against the new backend)
      * LRU eviction via server._release_world + server._move_to_trash
        once over SEMANTIC_CACHE_MAX entries

SLM backend: delegated to elastik's /dev/gpu plugin (plugins/available/
gpu.py in history, installed via /lib/gpu). semantic does not speak
ollama / openai / claude directly -- /dev/gpu is the single blind pipe
between elastik and whichever LLM backend /etc/gpu.conf names. This
means:
  * No ollama-specific config in semantic (SEMANTIC_MODEL /
    OLLAMA_URL / TEMPERATURE / MAX_TOKENS all belong to gpu's config).
  * Operator switches backend via PUT /etc/gpu.conf, no semantic
    reload needed (GPU_CONF_FINGERPRINT catches the swap).
  * /dev/gpu not registered -> semantic falls back to raw (same path
    as "backend unreachable"), so semantic installs cleanly ahead of
    gpu if needed.

Install (from repo root):
  curl -X PUT http://localhost:3005/lib/semantic \
    -H "Authorization: Bearer $ELASTIK_TOKEN" \
    --data-binary @plugins/semantic.py
  curl -X PUT http://localhost:3005/lib/semantic/state \
    -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" \
    --data-binary "active"

Requires: /dev/gpu installed (see its own install instructions in
plugins/available/gpu.py history). /etc/gpu.conf must name a reachable
backend.
"""
import asyncio
import collections
import hashlib
import json
import os
import time

import server


# ====================================================================
# plugin metadata
# ====================================================================

AUTH = "auth"               # T2 bearer or localhost (public_gate).
ROUTES = ["/shaped"]

# Route we dispatch to for the actual model call. Must match
# /dev/gpu's ROUTES declaration. If the gpu plugin isn't registered,
# _call_gpu_device() raises _SLMUnavailable and we hit the fallback
# path -- same as backend being unreachable.
GPU_ROUTE = "/dev/gpu"


# ====================================================================
# config (env-overridable)
# ====================================================================
#
# semantic's own knobs only. SLM knobs (model / endpoint / temperature
# / max_tokens / timeout) live in /dev/gpu + /etc/gpu.conf and are
# invisible here by design.

SEMANTIC_CACHE_MAX       = int(os.environ.get("SEMANTIC_CACHE_MAX", "10000"))
SEMANTIC_MAX_SOURCE      = int(os.environ.get("SEMANTIC_MAX_SOURCE", "20000"))
SEMANTIC_GEN_CAP_PER_MIN = int(os.environ.get("SEMANTIC_GEN_CAP_PER_MIN", "60"))

# Cache worlds live under this logical prefix. Privilege crossing
# (PLAN §5 / §9): plugin writes here with T3-equivalent rights scoped
# to this one prefix. Cache keys are sha256 so T2 clients can't steer
# writes outside it.
CACHE_PREFIX = "var/cache/semantic/"

# /etc/gpu.conf internal world name (matches /dev/gpu's _read_conf).
GPU_CONF_WORLD = "etc/gpu.conf"


# ====================================================================
# system prompt (PLAN §8)
# ====================================================================

SYSTEM_PROMPT = """You are a format-shaping renderer for elastik. You receive three inputs:
  - source content (bytes stored under /home/..., /lib/..., etc.)
  - REQUIRED_CONTENT_TYPE: a specific MIME type. Your output MUST be
    valid for this type. This is a hard constraint from the client's
    Accept header.
  - INTENT_HINT: a natural-language User-Agent describing the client.
    This ONLY affects dialect/width/style WITHIN the required type.
    It must NOT change the output's MIME type.

Your job: return the source content in the required MIME type, shaped
according to the intent hint.

Return ONLY the reshaped content. No explanation, no markdown fences,
no preamble. First line of your response is the first line of the
response body.

On the line AFTER the content, add one line starting with the literal
string '===META===' followed by JSON:
{"content_type": "<MUST-equal-REQUIRED_CONTENT_TYPE>", "shape": "<short-tag>"}

If REQUIRED_CONTENT_TYPE is "*/*" (wildcard), you may choose the best
type for the intent and source shape.

If the source cannot be rendered meaningfully in REQUIRED_CONTENT_TYPE
(e.g. image/png requested but source is plain text and no rendering
library is available to you), return an empty body + content_type
"text/plain" + shape "unrenderable". The plugin will detect the
mismatch and return 406 to the client -- do not try to smuggle plain
text past a request that asked for PNG.

Intent-hint examples (within a locked MIME type):
- INTENT_HINT "curl/*" and REQUIRED text/plain -> clean single-pass text
- INTENT_HINT "thermal-printer/1.0 (width=48)" and REQUIRED text/csv
  -> CSV with rows trimmed to 48 total chars
- INTENT_HINT "grandma/1.0 (big-font, simple)" and REQUIRED text/html
  -> HTML with large inline font-size and short sentences
- INTENT_HINT "Excel/16.0" and REQUIRED text/csv -> CSV with \\r\\n rows
- Any natural-language INTENT_HINT -> follow the declared intent
  literally, but never exit REQUIRED_CONTENT_TYPE.

Do not invent data. Do not summarise away information unless
INTENT_HINT explicitly asks. Do not add commentary. Render, don't edit.
"""


# ====================================================================
# render fingerprint (PLAN §4) — split into two axes
# ====================================================================

def _compute_render_fingerprint() -> str:
    """Static part of the renderer identity. Rotates when semantic's
    own knobs change (prompt text, source-truncation boundary).

    Does NOT include any /dev/gpu concerns -- those go in
    GPU_CONF_FINGERPRINT, read fresh per request."""
    h = hashlib.sha256()
    h.update(SYSTEM_PROMPT.encode("utf-8"))
    h.update(b"\x00")
    h.update(f"{SEMANTIC_MAX_SOURCE}".encode("utf-8"))
    return h.hexdigest()[:16]


RENDER_FINGERPRINT = _compute_render_fingerprint()


def _read_gpu_conf_raw() -> str:
    """Return /etc/gpu.conf contents (raw bytes decoded utf-8), or ""
    if absent. Mirrors plugins/available/gpu.py's _read_conf but with
    our safe _world_exists check so we don't auto-create the world
    just by reading it."""
    if not _world_exists(GPU_CONF_WORLD):
        return ""
    try:
        c = server.conn(GPU_CONF_WORLD)
        r = c.execute(
            "SELECT stage_html FROM stage_meta WHERE id=1"
        ).fetchone()
        raw = r["stage_html"] if r else b""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw or "")
    except Exception:
        return ""


def _gpu_conf_fingerprint() -> str:
    """Dynamic part of the renderer identity: hash of /etc/gpu.conf's
    first non-blank non-comment line (the scheme://endpoint tuple).

    Called per request. Operator switching backend via
      PUT /home/etc/gpu.conf -d "claude://api.anthropic.com"
    rotates this value, so cache entries from the old backend
    naturally miss. Empty gpu.conf -> empty hash input -> stable
    "no-backend" fingerprint (which is fine; those requests fall
    back to raw anyway)."""
    raw = _read_gpu_conf_raw()
    # Canonicalise: first non-blank non-comment line, trimmed. Matches
    # how /dev/gpu itself parses the file.
    canonical = ""
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        canonical = s
        break
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


# ====================================================================
# rate cap (PLAN §9): bound L3 generations per process per minute.
# ====================================================================
#
# Per-minute sliding window. Without this, any authenticated caller
# can drive unbounded model calls by varying UA / Accept / source
# version, turning /shaped/ into an open local-SLM gateway.
#
# When cap is exhausted:
#   * Accept permits text/plain -> 200 fallback-raw (same degradation
#     path as SLM-unavailable; caller at least gets the source).
#   * Accept excludes text/plain -> 429 Too Many Requests with
#     Retry-After: 60 (caller asked for a shape we can't produce
#     right now, and it's a rate-limit failure not an Accept failure).
#
# Thread-safety: deque.append / popleft are GIL-atomic in CPython;
# the check-then-append race here allows up to a handful of extra
# generations past the cap under concurrent load. Acceptable for
# v0.1; tighten with asyncio.Lock later if we ever see genuine
# concurrent pressure on a single elastik process.

_gen_timestamps: "collections.deque[float]" = collections.deque()


def _may_generate() -> bool:
    """Sliding 60-second window, SEMANTIC_GEN_CAP_PER_MIN samples.
    Returns True and records the timestamp on success; False when
    the cap is exhausted."""
    now = time.monotonic()
    window_start = now - 60.0
    while _gen_timestamps and _gen_timestamps[0] < window_start:
        _gen_timestamps.popleft()
    if len(_gen_timestamps) >= SEMANTIC_GEN_CAP_PER_MIN:
        return False
    _gen_timestamps.append(now)
    return True


# ====================================================================
# world-store helpers (read-only for sources, write for cache)
# ====================================================================

def _world_exists(name: str) -> bool:
    """True if the world has backing storage. Avoids auto-creating via conn()."""
    if not server._valid_name(name):
        return False
    disk_db = server.DATA / server._disk_name(name) / "universe.db"
    return disk_db.exists()


def _read_world(name: str):
    """Return (body_bytes, version, ext) for an existing world, or None.

    Unlike server.conn(name), this never creates a new world. Callers
    rely on that -- we don't want a typo'd GET /shaped/foo to instantiate
    an empty /home/foo world.
    """
    if not _world_exists(name):
        return None
    c = server.conn(name)
    row = c.execute(
        "SELECT stage_html, version, ext FROM stage_meta WHERE id=1"
    ).fetchone()
    if not row:
        return None
    return (row["stage_html"] or b"", row["version"] or 0, row["ext"] or "plain")


def _read_cached(key: str):
    """Return (body, content_type, shape) from the cache world, or None.

    Subtle distinction: server.conn() auto-creates a stage_meta row
    with stage_html='' and version=0 on first touch. That default row
    must read as 'miss' (cache was never populated for this key), but
    a real _write_cached() for an empty shaped output -- totally valid,
    e.g. shaping an empty source into an empty text/plain -- is ALSO
    stage_html=b''. The two are indistinguishable by body alone.

    Use version > 0 as the 'real write' marker: _write_cached() does
    `version = version + 1` atomically with the UPDATE, so any
    post-write read sees version >= 1. Default rows stay at 0."""
    name = CACHE_PREFIX + key
    if not _world_exists(name):
        return None
    c = server.conn(name)
    row = c.execute(
        "SELECT stage_html, version, headers FROM stage_meta WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    if (row["version"] or 0) == 0:
        return None
    body = row["stage_html"] if row["stage_html"] is not None else b""
    ct, shape = "text/plain", "raw"
    if row["headers"]:
        try:
            for k, v in json.loads(row["headers"]):
                kl = (k or "").lower()
                if kl == "x-semantic-content-type":
                    ct = v
                elif kl == "x-semantic-shape":
                    shape = v
        except (ValueError, TypeError):
            pass
    return (body, ct, shape)


def _write_cached(key: str, body, content_type: str, shape: str) -> None:
    """Persist shaped output as a cache world. Appends an event to the
    per-world HMAC chain so cache writes are audited alongside every
    other write in the system."""
    name = CACHE_PREFIX + key
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    hdrs = json.dumps([
        ["X-Semantic-Content-Type", content_type],
        ["X-Semantic-Shape", shape],
    ], ensure_ascii=False)
    c = server.conn(name)
    c.execute(
        "UPDATE stage_meta SET stage_html=?, ext='plain', headers=?, "
        "version=version+1, updated_at=datetime('now') WHERE id=1",
        (body_bytes, hdrs),
    )
    c.commit()
    server.log_event(name, "semantic_cache_write", {
        "key": key,
        "content_type": content_type,
        "shape": shape,
        "render": RENDER_FINGERPRINT,
        "bytes": len(body_bytes),
    })


def _evict_if_over_cap() -> None:
    """LRU eviction by updated_at once the cache exceeds SEMANTIC_CACHE_MAX.

    Uses the same world-lifecycle primitives the server's own DELETE
    handler uses: _release_world() closes SQLite + unlinks WAL/SHM,
    _move_to_trash() renames the dir into DATA/.trash/. That way:
      - .trash preserves the evicted world for recovery (Codex P2
        flagged that raw rmtree bypassed this and undercut the design
        claim of cache entries being first-class auditable worlds).
      - SQLite handles close cleanly; no stale conn in server._db.
      - HMAC event chain inside the evicted world travels into .trash
        untouched. An operator restoring from .trash gets the full
        history back.

    Walks DATA/ filtering by the cache's disk-name prefix. Good enough
    for one-user localhost; optimise later if it ever matters."""
    prefix_disk = server._disk_name(CACHE_PREFIX)
    entries = []
    if not server.DATA.exists():
        return
    for d in server.DATA.iterdir():
        if not d.is_dir():
            continue
        if not d.name.startswith(prefix_disk):
            continue
        if not (d / "universe.db").exists():
            continue
        entries.append(d)
    if len(entries) <= SEMANTIC_CACHE_MAX:
        return

    stats = []
    for d in entries:
        name = server._logical_name(d.name)
        try:
            row = server.conn(name).execute(
                "SELECT updated_at FROM stage_meta WHERE id=1"
            ).fetchone()
            ts = row["updated_at"] if row else ""
        except Exception:
            ts = ""
        stats.append((ts, d, name))
    stats.sort(key=lambda t: t[0])

    to_evict = len(entries) - SEMANTIC_CACHE_MAX
    for _, _d, name in stats[:to_evict]:
        try:
            server._release_world(name)
            server._move_to_trash(name)
        except Exception:
            continue


# ====================================================================
# request parsing
# ====================================================================

def _read_headers(scope) -> tuple:
    """Extract (User-Agent, Accept) strings from the ASGI scope."""
    ua = b""
    accept = b""
    for k, v in scope.get("headers", []) or []:
        if k == b"user-agent":
            ua = v
        elif k == b"accept":
            accept = v
    return (ua.decode("utf-8", "replace"), accept.decode("utf-8", "replace"))


def _parse_accept(hdr: str):
    """Parse Accept into a list of (mime, q) tuples, sorted by q desc.
    Missing / empty header -> [("*/*", 1.0)]."""
    if not hdr:
        return [("*/*", 1.0)]
    out = []
    for part in hdr.split(","):
        part = part.strip()
        if not part:
            continue
        mime, q = part, 1.0
        if ";" in part:
            bits = part.split(";")
            mime = bits[0].strip()
            for b in bits[1:]:
                b = b.strip()
                if b.startswith("q="):
                    try: q = float(b[2:])
                    except ValueError: pass
        out.append((mime.lower(), q))
    out.sort(key=lambda x: -x[1])
    return out


def _canonicalise_accept(accept_list) -> str:
    """Stable string for cache-key inclusion. Equivalent orderings collapse."""
    return ",".join(f"{m};q={q:.2f}" for m, q in accept_list)


def _accept_allows(accept_list, candidate_ct: str) -> bool:
    """Does this parsed Accept list permit the given Content-Type?"""
    cand = candidate_ct.split(";")[0].strip().lower()
    cand_family = cand.split("/")[0] if "/" in cand else cand
    for mime, q in accept_list:
        if q <= 0:
            continue
        if mime == "*/*":
            return True
        if mime == cand:
            return True
        if mime.endswith("/*"):
            if mime[:-2] == cand_family:
                return True
    return False


def _pick_required_ct(accept_list) -> str:
    """For the SLM prompt: return the highest-q concrete type, or '*/*'.
    Family wildcards (text/*) degrade to '*/*' so the SLM picks freely
    within the family and our _accept_allows check validates afterward."""
    for mime, q in accept_list:
        if q <= 0:
            continue
        if mime == "*/*" or mime.endswith("/*"):
            return "*/*"
        return mime
    return "*/*"


def _canonicalise_world_path(raw: str):
    """URL path -> internal world name. Mirrors server.py:923 rule:
       /home/X  -> X         (home prefix stripped)
       /lib/X   -> lib/X     (kept)
       /etc/X   -> etc/X     (kept)
    Returns the internal name, or None if the path is malformed."""
    parts = [p for p in raw.split("/") if p]
    if not parts:
        return None
    if parts[0] == "home":
        if len(parts) == 1:
            return None
        return "/".join(parts[1:])
    return "/".join(parts)


def _cache_key(world_name: str, version: int, ua: str,
               accept_canon: str, gpu_fp: str) -> str:
    """Composite cache key.

    Six axes:
      - world_name   : which source we shape
      - version      : source bumped -> old shape invalid
      - ua           : dialect hint (same Accept, different UA = new shape)
      - accept_canon : what MIME was asked for
      - RENDER_FINGERPRINT : semantic's own knobs (prompt, MAX_SOURCE)
      - gpu_fp       : /etc/gpu.conf hash (backend swap rotates this)

    Operator swapping backend via PUT /home/etc/gpu.conf instantly
    invalidates every cached shape across every world: gpu_fp changes,
    no cache key can hit, every request re-generates against the new
    backend."""
    h = hashlib.sha256()
    h.update(world_name.encode("utf-8")); h.update(b"\x00")
    h.update(str(version).encode("utf-8")); h.update(b"\x00")
    h.update(ua.encode("utf-8")); h.update(b"\x00")
    h.update(accept_canon.encode("utf-8")); h.update(b"\x00")
    h.update(RENDER_FINGERPRINT.encode("utf-8")); h.update(b"\x00")
    h.update(gpu_fp.encode("utf-8"))
    return h.hexdigest()


# ====================================================================
# /dev/gpu dispatch
# ====================================================================

class _SLMUnavailable(Exception):
    """Raised when /dev/gpu is not registered, /etc/gpu.conf is missing,
    or the configured backend returns an error. Caller decides whether
    this degrades to 200 fallback-raw or 406 fallback-406 based on
    Accept."""


def _safe_source(body) -> str:
    """Decode world body for prompt inclusion. Cap at SEMANTIC_MAX_SOURCE
    chars so a pathological 5MB world doesn't blow the context window.
    Truncation boundary is in RENDER_FINGERPRINT, so changing
    SEMANTIC_MAX_SOURCE rotates the cache."""
    if isinstance(body, (bytes, bytearray)):
        s = body.decode("utf-8", "replace")
    else:
        s = str(body)
    if len(s) > SEMANTIC_MAX_SOURCE:
        s = s[:SEMANTIC_MAX_SOURCE] + "\n...[TRUNCATED]..."
    return s


async def _call_gpu_device(prompt: str, scope) -> str:
    """POST our combined prompt to /dev/gpu in-process and return the
    text response.

    We look up the handler via server._plugins[GPU_ROUTE] rather than
    making an HTTP loopback call because:
      - no port contention / no extra TCP round trip.
      - gpu.py's own `server.log_event('dev/gpu', ...)` still fires,
        so the audit chain on /dev/gpu stays intact.

    About auth forwarding: /dev/gpu declares `AUTH = "none"` at the
    dispatcher level (so its GET man-page is accessible to browsers)
    but gates POSTs inline via `server._check_auth(scope)`. That means
    a params dict of `{"_scope": {}}` would get rejected with 401.
    We therefore forward the incoming /shaped/ request's scope -- it
    already cleared our own AUTH="auth" gate, so its Authorization
    header is present and gpu's inline check accepts it transparently.
    No privilege escalation: gpu accepts exactly what our dispatcher
    already accepted.

    /dev/gpu's public handle signature is async def handle(method,
    body, params). It treats body as the prompt text and returns
    either {"_body": text, "_ct": "text/plain"} on success or
    {"_status": 4xx/5xx, "error": "..."} on failure.

    If /dev/gpu isn't registered (plugin not installed / not active),
    this raises _SLMUnavailable. The caller handles it with the same
    Accept-gated fallback as any other SLM failure.

    Note on blocking: /dev/gpu's handle is `async def` but internally
    calls urllib.urlopen (blocking). Awaiting it here blocks the event
    loop for the duration of the model call -- same as if elastik
    served /dev/gpu directly. Not worse, not better. If ever a
    problem, fix gpu.py to run its dispatch in run_in_executor; not
    semantic's concern."""
    gpu_handler = server._plugins.get(GPU_ROUTE)
    if gpu_handler is None:
        raise _SLMUnavailable(f"{GPU_ROUTE} not registered")

    # Forward scope so gpu's inline _check_auth finds the Authorization
    # header that our dispatcher already validated. gpu reads
    # params.get("model") but that's irrelevant here -- let gpu.conf
    # decide the model via its scheme-specific defaults.
    result = await gpu_handler("POST", prompt, {"_scope": scope or {}})

    if not isinstance(result, dict):
        raise _SLMUnavailable(f"unexpected result type: {type(result).__name__}")
    status = result.get("_status", 200)
    if "error" in result or status >= 400:
        raise _SLMUnavailable(
            f"gpu {status}: {str(result.get('error') or '')[:200]}"
        )
    text = result.get("_body")
    if text is None:
        raise _SLMUnavailable("gpu returned no body")
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    return text


def _build_prompt(source_body, intent_hint: str, required_ct: str) -> str:
    """SYSTEM_PROMPT + user frame, inlined into a single blob for
    /dev/gpu. Most gpu backends don't expose the system-role
    distinction (ollama's `prompt` field, openai-compat's single-user-
    message pattern), so we prepend. Loses some model 'authority'
    on system instructions but works uniformly across every backend
    gpu.py supports."""
    return (
        SYSTEM_PROMPT
        + "\n\n---\n\n"
        + f"REQUIRED_CONTENT_TYPE: {required_ct}\n"
        + f"INTENT_HINT: {intent_hint or '(none)'}\n"
        + f"<<<SOURCE CONTENT>>>\n{_safe_source(source_body)}\n<<<END SOURCE>>>\n"
    )


def _parse_slm_output(raw: str):
    """Split the SLM response on '\\n===META==='. Returns (body, ct, shape).

    Per PLAN §8: META-parse failure falls back to (body, 'text/plain',
    'unknown'). The Accept-gate in handle() then decides 200 vs 406."""
    marker = "\n===META==="
    idx = raw.rfind(marker)
    if idx < 0:
        return (raw, "text/plain", "unknown")
    body = raw[:idx]
    meta_str = raw[idx + len(marker):].strip()
    try:
        meta = json.loads(meta_str)
        ct = str(meta.get("content_type") or "text/plain")
        shape = str(meta.get("shape") or "unknown")
        return (body, ct, shape)
    except (ValueError, TypeError):
        return (body, "text/plain", "unknown")


# ====================================================================
# main handler
# ====================================================================

async def handle(method, body, params):
    """GET /shaped/<world_path>. See PLAN-semantic-http.md for the contract."""
    if method == "OPTIONS":
        return {
            "_body": "",
            "_ct": "text/plain",
            "_headers": [("Allow", "GET, HEAD, OPTIONS")],
        }
    if method not in ("GET", "HEAD"):
        return {"_status": 405, "error": "method not allowed"}

    scope = params.get("_scope") or {}
    raw_path = scope.get("path", "")

    # 1. Strip the /shaped prefix and canonicalise to an internal name.
    url_tail = raw_path[len("/shaped"):].lstrip("/")
    world_name = _canonicalise_world_path(url_tail)
    if not world_name or not server._valid_name(world_name):
        return {"_status": 400, "error": "bad world path"}

    # 2. Negotiation inputs.
    ua, accept_hdr = _read_headers(scope)
    accept_list = _parse_accept(accept_hdr)
    accept_canon = _canonicalise_accept(accept_list)

    # 3. Source world must exist.
    src = _read_world(world_name)
    if src is None:
        return {"_status": 404, "error": f"world not found: {world_name}"}
    body_src, version, _ext = src

    # 4. Cache key. GPU config fingerprint is computed per request so
    #    operator backend swaps via PUT /home/etc/gpu.conf naturally
    #    rotate the cache without any semantic reload.
    gpu_fp = _gpu_conf_fingerprint()
    key = _cache_key(world_name, version, ua, accept_canon, gpu_fp)
    hit = _read_cached(key)
    if hit is not None:
        body_c, ct, shape = hit
        return {
            "_body": body_c,
            "_ct": ct,
            "_headers": _hdr_set("hit", shape, ct, gpu_fp),
        }

    # 5. Rate cap BEFORE spending a /dev/gpu call. Exhausted cap
    #    degrades along the Accept-gated path.
    if not _may_generate():
        return _accept_gated_fallback(
            body_src, accept_list,
            tag_ok="ratelimit-raw",
            tag_block="ratelimit-429",
            error_detail=f"generation rate cap ({SEMANTIC_GEN_CAP_PER_MIN}/min) reached",
            block_status=429,
            retry_after="60",
            gpu_fp=gpu_fp,
        )

    # 6. L3: dispatch to /dev/gpu. Forward scope so gpu's inline
    #    POST auth check sees the same Authorization header that
    #    cleared our own dispatcher-level AUTH="auth" gate.
    required_ct = _pick_required_ct(accept_list)
    prompt = _build_prompt(body_src, ua, required_ct)
    try:
        raw_text = await _call_gpu_device(prompt, scope)
    except _SLMUnavailable as e:
        return _accept_gated_fallback(
            body_src, accept_list,
            tag_ok="fallback-raw",
            tag_block="fallback-406",
            error_detail=str(e),
            block_status=406,
            gpu_fp=gpu_fp,
        )
    shaped, slm_ct, shape = _parse_slm_output(raw_text)

    # 7. Validate SLM output against Accept. Mismatch -> 406, no cache.
    if not _accept_allows(accept_list, slm_ct):
        return {
            "_status": 406,
            "error": "slm produced unacceptable content-type",
            "_headers": [
                ("X-Semantic-Shape-Returned", shape),
                ("X-Semantic-Content-Type-Returned", slm_ct),
                ("X-Semantic-Render", RENDER_FINGERPRINT),
                ("X-Semantic-Gpu", gpu_fp),
            ],
        }

    # 8. Cache + return. Cache-write failures don't block the response.
    try:
        _write_cached(key, shaped, slm_ct, shape)
        _evict_if_over_cap()
    except Exception:
        pass

    return {
        "_body": shaped,
        "_ct": slm_ct,
        "_headers": _hdr_set("generated", shape, slm_ct, gpu_fp),
    }


def _hdr_set(cache_status: str, shape: str, ct: str = "", gpu_fp: str = ""):
    """Common X-Semantic-* headers plus HTML hardening when ct is text/html.

    PLAN Q5 settled to 'strict over permissive': any text/html payload
    from /shaped/ rides with a tight CSP so SLM-injected <script> can't
    execute, plus nosniff to block content-type guessing."""
    hdrs = [
        ("X-Semantic-Cache", cache_status),
        ("X-Semantic-Shape", shape),
        ("X-Semantic-Render", RENDER_FINGERPRINT),
    ]
    if gpu_fp:
        hdrs.append(("X-Semantic-Gpu", gpu_fp))
    if ct and ct.split(";")[0].strip().lower() == "text/html":
        hdrs.append(
            ("Content-Security-Policy",
             "default-src 'none'; style-src 'unsafe-inline'"))
        hdrs.append(("X-Content-Type-Options", "nosniff"))
    return hdrs


def _accept_gated_fallback(
    body_src,
    accept_list,
    tag_ok: str,
    tag_block: str,
    error_detail: str,
    block_status: int = 406,
    retry_after: str = "",
    gpu_fp: str = "",
):
    """PLAN §6 / §9 / §11: Accept-gated fallback shared by SLM-unavailable
    and rate-limited paths.

    * Accept permits text/plain -> 200 + raw body as text/plain,
      cache tag = tag_ok.
    * Accept excludes text/plain -> block_status (406 for SLM-down,
      429 for rate-limited) + cache tag = tag_block. retry_after
      adds a Retry-After header (useful for 429).

    Neither branch caches. The caller reading X-Semantic-Cache can
    distinguish fallback-raw vs ratelimit-raw vs hit vs generated."""
    if _accept_allows(accept_list, "text/plain"):
        raw = body_src.decode("utf-8", "replace") if isinstance(body_src, (bytes, bytearray)) else str(body_src)
        hdrs = [
            ("X-Semantic-Cache", tag_ok),
            ("X-Semantic-Error", error_detail[:100]),
            ("X-Semantic-Render", RENDER_FINGERPRINT),
        ]
        if gpu_fp:
            hdrs.append(("X-Semantic-Gpu", gpu_fp))
        if retry_after:
            hdrs.append(("Retry-After", retry_after))
        return {
            "_body": raw,
            "_ct": "text/plain; charset=utf-8",
            "_headers": hdrs,
        }
    hdrs = [
        ("X-Semantic-Cache", tag_block),
        ("X-Semantic-Error", error_detail[:100]),
        ("X-Semantic-Render", RENDER_FINGERPRINT),
    ]
    if gpu_fp:
        hdrs.append(("X-Semantic-Gpu", gpu_fp))
    if retry_after:
        hdrs.append(("Retry-After", retry_after))
    return {
        "_status": block_status,
        "error": error_detail,
        "_headers": hdrs,
    }
