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
      * text/plain is top-q preference (or wildcard) -> 200 fallback-raw,
                                                        no cache
      * text/plain NOT top-q                         -> 503 fallback-503
                                                        + Retry-After, no cache

    Both branches keep the 406 output-mismatch semantics above — 503
    specifically means "the renderer can't serve this right now";
    406 specifically means "the renderer ran but produced a MIME you
    refused." Distinct status codes for distinct failure modes.

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

SYSTEM_PROMPT = """You are a format-shaping renderer for elastik. You receive four inputs:
  - source content (bytes stored under /home/..., /lib/..., or fetched
    live through /mnt/... mounts declared in /etc/fstab)
  - SOURCE_CONTENT_TYPE: the MIME type the source declared. Use this
    to interpret structured sources (JSON, CSV, HTML, etc.) correctly
    before reshaping. If it conflicts with what the bytes actually
    look like, trust the bytes.
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


def _ext_to_ct(ext: str) -> str:
    """Reverse lookup of server.py's _CT dict with a conservative
    fallback. SLM prompts work better against text than opaque octets
    when ext is unknown, so we default to text/plain rather than
    server._ext_to_ct's application/octet-stream. The result is fed
    to the prompt as SOURCE_CONTENT_TYPE — see _build_prompt."""
    return server._CT.get(ext or "plain", "text/plain")


class _MountAdapterError(Exception):
    """The /mnt adapter returned a non-2xx status for a mounted source.
    Carries the original status so handle() can surface the real cause
    instead of collapsing to a misleading 404 'world not found'.

    Distinguishes three things that used to all look the same to
    /shaped/ callers:
      - "mount not in fstab"           -> 404 via mount handler
      - "file/object not in mount"     -> 404 via adapter
      - "adapter reached but failed"   -> 413/500/501/502/503 via adapter

    All three previously collapsed to None -> 'world not found' 404
    at the /shaped/ boundary; the adapter's status is now preserved."""
    def __init__(self, status: int, error: str = ""):
        super().__init__(error or f"mount adapter returned {status}")
        self.status = status
        self.error = error or f"mount adapter returned {status}"


async def _read_via_fstab(mnt_path: str):
    """In-process call into /mnt/<mnt_path>. No HTTP loopback.

    Success: returns (body_bytes, version_token, source_ct). Version
    token is "mount:<X-Mount-Version>" when the adapter sets that
    header (file: mtime:<ns>, https: etag:<val> or len=...;head=);
    fallback is "mount:unknown" for adapters that don't carry a
    version.

    Adapter failure (status >= 400): raises _MountAdapterError with
    the original status + fstab's error message. Caller (handle())
    surfaces both via the /shaped/ response so a 413 stays 413, a 501
    stays 501, and "upstream exploded" doesn't read as "world not
    found." Codex actually reproduced the old flatten-to-404
    behaviour against /mnt/remote/boom, /big, and /unk/anything —
    this path used to hide all three.

    None return is reserved for cases where we genuinely cannot
    produce a status code: fstab plugin not installed, mount handler
    returned a non-dict, or returned 200 with no body. Those fall
    through to the /shaped/ handler's generic 404.
    """
    mnt_handler = server._plugins.get("/mnt")
    if mnt_handler is None:
        return None                           # no fstab installed
    fake_scope = {
        "type":    "http",
        "method":  "GET",
        "path":    "/mnt/" + mnt_path,
        "headers": [],                        # AUTH=none on /mnt/;
                                              # nothing to forward
    }
    try:
        result = await mnt_handler("GET", "", {"_scope": fake_scope})
    except Exception as e:
        # Handler crashed. Surface as 500 so /shaped/ doesn't pretend
        # the source is missing — it's the dispatcher that misbehaved.
        raise _MountAdapterError(500, f"mount handler crashed: {e}")
    if not isinstance(result, dict):
        return None
    status = result.get("_status", 200)
    if status >= 400:
        raise _MountAdapterError(
            status, str(result.get("error") or f"adapter returned {status}"))
    body = result.get("_body")
    if body is None:
        return None
    ct = result.get("_ct", "application/octet-stream")
    ver = "mount:unknown"
    for k, v in result.get("_headers") or []:
        if str(k).lower() == "x-mount-version":
            ver = "mount:" + str(v)
            break
    return body, ver, ct


async def _read_source(name: str):
    """Dispatch world-backed vs mount-backed sources.

    Returns (body_bytes, version_token, source_content_type), or None
    if the source doesn't resolve either way.

    Dispatch:
      name starts with 'mnt/' -> /mnt/<rest> via fstab in-process.
                                 The adapter owns both the bytes and
                                 the Content-Type. Version token is
                                 whatever X-Mount-Version the adapter
                                 set (prefixed 'mount:').
      else                     -> existing _read_world. Source CT is
                                 inferred from the ext column via
                                 _ext_to_ct. Version token is
                                 'world:v<N>' — stringified so the
                                 cache key accepts it without a
                                 type change in _cache_key's hash.

    Async because _read_via_fstab awaits the mount plugin's async
    handle(). _read_world itself is sync and stays that way — the
    world store is sqlite, no I/O wait worth yielding for."""
    if name.startswith("mnt/"):
        return await _read_via_fstab(name[len("mnt/"):])
    got = _read_world(name)
    if got is None:
        return None
    body, version, ext = got
    return body, f"world:v{version}", _ext_to_ct(ext)


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
        # RFC 7231 §5.3.2 says the wildcard form is */*, not bare *.
        # Clients occasionally send *; treat it as */* so we don't 406
        # on something everyone agrees means "give me anything."
        if part == "*":
            part = "*/*"
        mime, q = part, 1.0
        if ";" in part:
            bits = part.split(";")
            mime = bits[0].strip()
            if mime == "*":
                mime = "*/*"
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


def _accept_wants_stream(accept_list) -> bool:
    """True iff the client declared text/event-stream with q > 0.

    text/event-stream is treated as an OUTER transport, not an inner
    shape — see the strip dance in handle(). Presence here only
    signals "wrap the response in SSE"; it does NOT participate in
    _pick_required_ct (SLM never sees text/event-stream as
    REQUIRED_CONTENT_TYPE) nor in _accept_allows (SLM output is
    validated against the inner shape only). An `Accept:
    text/event-stream;q=0` explicit refusal returns False here and
    the non-stream path serves normally."""
    for mime, q in accept_list:
        if mime == "text/event-stream" and q > 0:
            return True
    return False


def _text_plain_is_top(accept_list) -> bool:
    """Is text/plain the client's top-ranked admissible preference?

    True iff one of:
      - accept_list empty (missing Accept parses to [("*/*", 1.0)])
      - top entry is */* (wildcard always admits text/plain at top)
      - top entry is text/* (family wildcard covering text/plain)
      - top entry is literal text/plain

    False iff the client expressed a stronger preference first (e.g.
    image/png, application/json) — even if text/plain appears later
    with q > 0. Used by _accept_gated_fallback to decide whether
    raw text/plain is an honest fallback or a preference-violating
    substitute. _accept_allows is too lenient for that gate: it
    returns True whenever text/plain appears with any q > 0, which
    means `Accept: image/png, text/plain;q=0.8` + SLM down used to
    serve plain silently, burying the client's stated preference for
    PNG. The top-q gate respects the ordering.

    Tie-break on equal q: first occurrence wins (_parse_accept's sort
    is stable over input order)."""
    if not accept_list:
        return True
    top_mime, top_q = accept_list[0]
    if top_q <= 0:
        return False
    return top_mime in ("*/*", "text/*", "text/plain")


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
    or the configured backend returns an error. Caller degrades via
    _accept_gated_fallback, which picks the response based on whether
    text/plain is the client's top-q preference:

      * text/plain top-q (incl wildcards) -> 200 fallback-raw, raw
                                             source bytes, no cache
      * otherwise                         -> 503 fallback-503,
                                             Retry-After, no cache

    503 is distinct from the SLM-ran-but-output-mismatched-Accept
    case (which stays 406) — service availability vs content
    negotiation are genuinely different failure modes."""


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


async def _call_gpu_stream(prompt: str, scope):
    """Streaming sibling of _call_gpu_device. Returns the raw
    AsyncIterator[str] of text tokens from /dev/gpu/stream.

    Uses the same in-process dispatch as _call_gpu_device (no HTTP
    loopback) via gpu.py's _stream_in_process bridge flag. When set,
    _handle_stream hands back the per-backend iterator without
    opening its own HTTP response — semantic wraps in SSE itself,
    adds /shaped/-level audit, and writes the cache at stream end.
    Without the flag gpu.py would frame the stream for an external
    HTTP caller, which is wrong here.

    Initialisation errors (gpu not installed, no /etc/gpu.conf,
    missing API key, unknown scheme) arrive as a dict with _status
    >= 400 and raise _SLMUnavailable so the caller can fall back
    through the existing _accept_gated_fallback. Post-first-chunk
    errors raise from inside the iterator and _stream_shape
    surfaces them as `event: error` frames (200 already committed
    at that point)."""
    gpu_handler = server._plugins.get(GPU_ROUTE)
    if gpu_handler is None:
        raise _SLMUnavailable(f"{GPU_ROUTE} not registered")
    # Spoof the scope path so gpu.py's front-door dispatches to
    # _handle_stream instead of the non-stream handle body.
    spoofed_scope = dict(scope or {})
    spoofed_scope["path"] = "/dev/gpu/stream"
    result = await gpu_handler("POST", prompt, {
        "_scope": spoofed_scope,
        "_stream_in_process": True,
    })
    if isinstance(result, dict):
        status = result.get("_status", 200)
        raise _SLMUnavailable(
            f"gpu stream {status}: {str(result.get('error') or '')[:200]}"
        )
    # Raw async iterator; caller iterates with `async for`.
    return result


# ====================================================================
# SSE framing helpers
# ====================================================================

def _sse_data(text: str) -> bytes:
    """One SSE data frame. Every '\\n' in the payload becomes its own
    `data:` line so multi-line shaped output (HTML, CSV, etc.)
    reassembles byte-for-byte on the client. Frame terminated by the
    empty line. Empty input returns empty bytes — callers should skip
    rather than emit a zero-content frame."""
    if not text:
        return b""
    lines = text.split("\n")
    return ("".join(f"data: {ln}\n" for ln in lines) + "\n").encode("utf-8")


def _sse_event(name: str, payload: str = "") -> bytes:
    """Named SSE event with an inline `data:` line. Newlines in
    payload are stripped — event frames carry short metadata (`done`
    shape/ct summary, `error` reasons), not content. Callers pass a
    JSON string for machine-readable payloads."""
    safe = payload.replace("\n", " ").replace("\r", " ")
    return f"event: {name}\ndata: {safe}\n\n".encode("utf-8")


# ====================================================================
# streaming response helpers
# ====================================================================

_META_MARKER = "\n===META==="


async def _fake_stream_body(body, ct: str, shape: str, gpu_fp: str, send):
    """Cache-hit streaming path: wrap a fully-assembled body as one
    SSE `data:` frame + terminal `event: done`.

    The SLM wasn't consulted for this request (cache hit), so there
    is nothing to actually stream — but the client asked for
    text/event-stream so its SSE consumer expects frames, not a raw
    body. Fake-streaming keeps the consumer-side parser uniform
    between hit and miss.

    Caller guarantees send is non-None (validated in handle()).
    Returns None to signal 'plugin streamed its own response'
    (server.py:765)."""
    body_str = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    meta_payload = json.dumps({"ct": ct, "shape": shape, "cache": "hit"},
                              ensure_ascii=False)
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"text/event-stream; charset=utf-8"],
            [b"cache-control", b"no-cache"],
            [b"x-semantic-cache", b"hit"],
            [b"x-semantic-shape", shape.encode("utf-8")],
            [b"x-semantic-render", RENDER_FINGERPRINT.encode("utf-8")],
            [b"x-semantic-gpu", gpu_fp.encode("utf-8")],
            [b"x-semantic-inner-ct", ct.encode("utf-8")],
        ],
    })
    if body_str:
        await send({"type": "http.response.body",
                    "body": _sse_data(body_str),
                    "more_body": True})
    await send({"type": "http.response.body",
                "body": _sse_event("done", meta_payload),
                "more_body": True})
    await send({"type": "http.response.body",
                "body": b"",
                "more_body": False})
    return None


async def _stream_shape(prompt: str, scope, cache_key: str, gpu_fp: str,
                        accept_list, required_ct: str, send):
    """Cache-miss streaming path: /dev/gpu/stream → SSE frames →
    cache write → event: done.

    Flow:
      1. Acquire async iterator (in-process bridge). Init errors
         raise _SLMUnavailable BEFORE http.response.start — caller
         takes normal _accept_gated_fallback path, status code is
         still negotiable.
      2. Peek first chunk. If iterator is empty (no yields), same
         failure mode as init error — raise _SLMUnavailable.
      3. Commit http.response.start with 200 + SSE headers. From
         here on, status code is frozen. Errors can only be surfaced
         as `event: error` frames.
      4. Forward each chunk as a `data:` frame. Hold back the last
         |_META_MARKER| chars so a '\\n===META===' split across
         chunks still matches. On match: stop forwarding, start
         accumulating META JSON tail.
      5. On clean generator exit: parse META, validate content-
         type against Accept. If slm_ct is admissible, write cache
         and emit `event: done` with {shape, ct}. Else emit
         `event: error` and skip cache (no silent caching of output
         the client explicitly refused).

    Returns None to signal 'plugin streamed its own response'."""
    try:
        chunks = await _call_gpu_stream(prompt, scope)
    except _SLMUnavailable:
        raise                           # bubble to handle() pre-commit

    # Peek for first token. An empty iterator maps to 'no data', treat
    # it the same way an upstream error would — _SLMUnavailable, caller
    # falls back through the usual gate.
    iterator = chunks.__aiter__()
    try:
        first_chunk = await iterator.__anext__()
    except StopAsyncIteration:
        raise _SLMUnavailable("gpu stream yielded no tokens")
    except _SLMUnavailable:
        raise
    except Exception as e:
        raise _SLMUnavailable(f"gpu stream init: {e}")

    # Response is committed from here on. Status cannot change.
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"text/event-stream; charset=utf-8"],
            [b"cache-control", b"no-cache"],
            [b"x-semantic-cache", b"generated"],
            [b"x-semantic-render", RENDER_FINGERPRINT.encode("utf-8")],
            [b"x-semantic-gpu", gpu_fp.encode("utf-8")],
        ],
    })

    # Framing loop with sliding META-marker detection.
    #
    # `pending`: content we've seen but not yet emitted, because the
    # last |_META_MARKER|-1 chars might be the start of a marker
    # that completes on the next chunk. We emit pending[:-HOLD] and
    # keep pending[-HOLD:] between chunks.
    # `full`: everything we've seen (for cache body + post-hoc META
    # parse). Emitted OR held, either way it's in full.
    # `meta_tail`: once we detect the marker, we stop forwarding
    # content and accumulate here. None = still in content phase.
    HOLD = len(_META_MARKER) - 1
    pending = first_chunk
    full = first_chunk
    meta_tail = None
    stream_errored = False

    async def _maybe_forward():
        """Emit pending[:-HOLD] as a data frame. Trims the buffer."""
        nonlocal pending
        if len(pending) > HOLD:
            to_emit = pending[:-HOLD]
            await send({"type": "http.response.body",
                        "body": _sse_data(to_emit),
                        "more_body": True})
            pending = pending[-HOLD:]

    # Scan the first chunk for marker before forwarding anything.
    idx = pending.find(_META_MARKER)
    if idx >= 0:
        if idx > 0:
            await send({"type": "http.response.body",
                        "body": _sse_data(pending[:idx]),
                        "more_body": True})
        meta_tail = pending[idx + len(_META_MARKER):]
        pending = ""
    else:
        await _maybe_forward()

    try:
        async for tok in iterator:
            if not tok:
                continue
            full += tok
            if meta_tail is not None:
                meta_tail += tok
                continue
            pending += tok
            idx = pending.find(_META_MARKER)
            if idx >= 0:
                if idx > 0:
                    await send({"type": "http.response.body",
                                "body": _sse_data(pending[:idx]),
                                "more_body": True})
                meta_tail = pending[idx + len(_META_MARKER):]
                pending = ""
                continue
            await _maybe_forward()
    except Exception as e:
        stream_errored = True
        await send({"type": "http.response.body",
                    "body": _sse_event("error", f"stream failed: {e}"),
                    "more_body": True})

    # Drain remaining pending (no marker was ever found, or we were
    # still in content phase when iterator exited).
    if meta_tail is None and pending:
        await send({"type": "http.response.body",
                    "body": _sse_data(pending),
                    "more_body": True})

    # Parse META if found. SLM produces {"content_type": "...",
    # "shape": "..."}; META-parse failure falls back to the same
    # defaults _parse_slm_output uses (text/plain, shape=unknown).
    slm_ct, shape = "text/plain", "unknown"
    if meta_tail is not None:
        meta_str = meta_tail.strip()
        try:
            meta = json.loads(meta_str)
            slm_ct = str(meta.get("content_type") or "text/plain")
            shape = str(meta.get("shape") or "unknown")
        except (ValueError, TypeError):
            pass

    # Extract the cache body = everything before the marker. If no
    # marker was ever seen, the whole stream counts as content.
    if _META_MARKER in full:
        body_out = full[:full.index(_META_MARKER)]
    else:
        body_out = full

    # Validate against Accept. SLM output-mismatch (the 406 case in
    # non-stream) cannot change status mid-stream — emit an error
    # event instead, no cache write.
    #
    # An empty body_out with an Accept-admissible slm_ct is a
    # legitimate success: SYSTEM_PROMPT explicitly permits 'empty
    # body + text/plain + shape unrenderable' when the model cannot
    # render the source into the requested shape. Caching that empty
    # result is correct — it short-circuits future identical
    # requests instead of re-prompting the SLM for the same
    # impossible render. _read_cached already distinguishes an
    # empty-body cache hit from the default empty-row miss via its
    # updated_at check, so the write side is safe. Only
    # stream_errored and Accept-admissibility gate the success path.
    if not stream_errored and _accept_allows(accept_list, slm_ct):
        try:
            _write_cached(cache_key, body_out, slm_ct, shape)
            _evict_if_over_cap()
        except Exception:
            pass
        done_payload = json.dumps(
            {"ct": slm_ct, "shape": shape, "cache": "generated"},
            ensure_ascii=False)
        await send({"type": "http.response.body",
                    "body": _sse_event("done", done_payload),
                    "more_body": True})
    else:
        if not stream_errored:
            await send({
                "type": "http.response.body",
                "body": _sse_event(
                    "error",
                    f"slm output {slm_ct!r} violates Accept; shape={shape}"),
                "more_body": True,
            })
        # Still emit done so clients have a structural terminal.
        await send({"type": "http.response.body",
                    "body": _sse_event("done", "{}"),
                    "more_body": True})

    await send({"type": "http.response.body",
                "body": b"",
                "more_body": False})
    return None


def _build_prompt(source_body, intent_hint: str, required_ct: str,
                  source_ct: str) -> str:
    """SYSTEM_PROMPT + user frame, inlined into a single blob for
    /dev/gpu. Most gpu backends don't expose the system-role
    distinction (ollama's `prompt` field, openai-compat's single-user-
    message pattern), so we prepend. Loses some model 'authority'
    on system instructions but works uniformly across every backend
    gpu.py supports.

    source_ct carries the MIME the source declared (world ext -> CT,
    or /mnt adapter's _ct). The SLM is told this explicitly so it
    can interpret structured sources correctly before reshaping — a
    JSON source reshaped as CSV is a different job from a plain-text
    source reshaped as CSV, and the SLM only knows which path it's on
    if we say so here."""
    return (
        SYSTEM_PROMPT
        + "\n\n---\n\n"
        + f"REQUIRED_CONTENT_TYPE: {required_ct}\n"
        + f"SOURCE_CONTENT_TYPE: {source_ct}\n"
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

    # 2. Negotiation inputs. text/event-stream is transport, not
    #    shape — strip it out before computing accept_canon and
    #    required_ct so the SLM never sees it as REQUIRED_CONTENT_TYPE
    #    and the cache key stays identical across stream/non-stream
    #    requests for the same inner shape.
    ua, accept_hdr = _read_headers(scope)
    accept_list_raw = _parse_accept(accept_hdr)
    streaming = _accept_wants_stream(accept_list_raw)
    accept_list = [(m, q) for m, q in accept_list_raw
                   if m != "text/event-stream"]
    if streaming and not accept_list:
        # Client asked for a stream but didn't declare an inner
        # shape. We would have no REQUIRED_CONTENT_TYPE to pass the
        # SLM. Refuse cleanly rather than guess — the consumer needs
        # to say what shape they want INSIDE the SSE envelope.
        return {
            "_status": 400,
            "error": "text/event-stream is transport, not shape; "
                     "add an inner MIME (e.g. "
                     "Accept: text/event-stream, text/html)",
        }
    accept_canon = _canonicalise_accept(accept_list)

    # 3. Source must resolve. _read_source dispatches between the
    #    world store (existing behaviour) and /mnt/* mounts declared
    #    in /etc/fstab (sidecar Phase 1). Same (body, version, ct)
    #    shape both ways, so the rest of handle() stays source-agnostic.
    #
    #    _MountAdapterError is the adapter's way of saying "source
    #    exists but I couldn't fetch it" (upstream 500, cap trip 413,
    #    unknown scheme 501, etc.). We surface the original status
    #    instead of collapsing to a generic 404 — the distinction
    #    between "path does not exist" and "source resolution failed"
    #    is load-bearing for /shaped/ debugging.
    try:
        src = await _read_source(world_name)
    except _MountAdapterError as e:
        return {"_status": e.status,
                "error": f"mount adapter: {e.error}"}
    if src is None:
        return {"_status": 404, "error": f"world not found: {world_name}"}
    body_src, version_token, source_ct = src

    # 4. Cache key. version_token is the per-source bump signal —
    #    "world:v<N>" for world-backed sources, "mount:<token>" for
    #    mount-backed (mtime_ns / etag / len+head). GPU config
    #    fingerprint is computed per request so operator backend swaps
    #    via PUT /home/etc/gpu.conf naturally rotate the cache without
    #    any semantic reload.
    gpu_fp = _gpu_conf_fingerprint()
    key = _cache_key(world_name, version_token, ua, accept_canon, gpu_fp)
    hit = _read_cached(key)
    if hit is not None:
        body_c, ct, shape = hit
        if streaming:
            # Fake-stream: one data frame + event: done. Keeps the
            # client's SSE consumer uniform across hit/miss without
            # actually dispatching to /dev/gpu.
            send_fn = params.get("_send")
            if send_fn is not None:
                return await _fake_stream_body(body_c, ct, shape,
                                               gpu_fp, send_fn)
            # No raw send available (shouldn't happen in practice) —
            # fall through to the one-shot envelope.
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
    prompt = _build_prompt(body_src, ua, required_ct, source_ct)

    # 6a. Streaming branch. Pre-first-chunk errors propagate as
    #     _SLMUnavailable here and fall back through the standard
    #     accept-gated path below — status is still negotiable until
    #     _stream_shape emits http.response.start. Once committed,
    #     mid-stream errors become `event: error` frames with 200
    #     headers already on the wire.
    if streaming:
        send_fn = params.get("_send")
        if send_fn is not None:
            try:
                return await _stream_shape(
                    prompt, scope, key, gpu_fp,
                    accept_list, required_ct, send_fn)
            except _SLMUnavailable as e:
                return _accept_gated_fallback(
                    body_src, accept_list,
                    tag_ok="fallback-raw",
                    tag_block="fallback-503",
                    error_detail=str(e),
                    block_status=503,
                    retry_after="5",
                    gpu_fp=gpu_fp,
                )

    try:
        raw_text = await _call_gpu_device(prompt, scope)
    except _SLMUnavailable as e:
        # v0.2: SLM-infra failure is a service-availability problem,
        # not an Accept-compatibility problem. 503 distinguishes it
        # from the SLM-ran-but-output-mismatched-Accept case below
        # (which stays 406). Retry-After nudges well-behaved clients
        # to back off briefly rather than hammer.
        return _accept_gated_fallback(
            body_src, accept_list,
            tag_ok="fallback-raw",
            tag_block="fallback-503",
            error_detail=str(e),
            block_status=503,
            retry_after="5",
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
    """PLAN §6 / §9 / §11 + v0.2 hardening: Accept-gated fallback
    shared by SLM-unavailable and rate-limited paths.

    * text/plain is the client's top-q preference  -> 200 + raw body
      as text/plain, cache tag = tag_ok.
    * otherwise -> block_status (503 for SLM-down, 429 for
      rate-limited) + cache tag = tag_block. retry_after adds a
      Retry-After header.

    v0.1 used _accept_allows here, which returned True for any q>0
    on text/plain — so `Accept: image/png, text/plain;q=0.8` on an
    SLM-down 503 path quietly served plain, violating the client's
    stated preference for PNG. v0.2 uses _text_plain_is_top: raw is
    honest only when the client actually asked for text first.

    Neither branch caches. The caller reading X-Semantic-Cache can
    distinguish fallback-raw vs ratelimit-raw vs hit vs generated."""
    if _text_plain_is_top(accept_list):
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
