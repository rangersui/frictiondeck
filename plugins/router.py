"""/_router_fallback — SLM-assisted resolver for unmatched paths.

Installed via the normal `/lib/router` PUT + activate dance. Registers
the hook-only `/_router_fallback` route. `server.py`'s `app()` dispatch
calls this route AFTER all normal plugin and world lookups have
declined a GET/HEAD request. Router never sees state-mutating
requests (method filtered at the hook), never sees paths containing
traversal or over the URL length cap (filtered at gates upstream),
and never runs recursively (sentinel filtered at the hook).

**Architecture note.** Router is the ONE feature in elastik where an
SLM sits on the input (routing) path rather than the output (shaping)
path. Every safety property that `/shaped/*` gets for free — bounded
cache, deterministic dispatch, narrow exfiltration surface — has to
be re-earned here. Full design and rationale: PLAN-semantic-router.md.

Request flow:

  server.py hook  ──▶  handle()  ──▶  _caller_readable_worlds(scope)
                                              │
                                              ▼
                               (empty pool) ──┘─▶ 404 empty-pool-static-404
                                              │
                                              ▼ _candidate_prefilter (stdlib)
                                              │
                                              ▼ _read_route_cache (caller-scoped key)
                                              │
                                       (hit) ─┘─▶ 303 / 300 / 404-prose
                                              │
                                              ▼ _may_route?
                                              │
                                       (cap) ─┘─▶ 404 ratelimit-static-404
                                              │
                                              ▼ _call_router_slm
                                              │
                                              ▼ validate chosen name ∈ pool
                                              │
                                              ▼ _write_route_cache + evict
                                              │
                                              ▼ 303 / 300 / 404-prose

See PLAN-semantic-router.md for the full spec. This file is the
implementation; the PLAN is the contract.
"""
DESCRIPTION = "semantic router — 404 fallback with SLM resolution"
AUTH        = "none"

import asyncio
import collections
import hashlib
import heapq
import json
import os
import time
import unicodedata

import server


# ====================================================================
# config (env-overridable)
# ====================================================================
#
# See PLAN-semantic-router.md §3.0.2 / §4 / §5 for the rationale
# behind each default. All knobs are env-overridable so operators can
# tune without editing plugin source.

SEMANTIC_ROUTE_CAP_PER_MIN   = int(os.environ.get("SEMANTIC_ROUTE_CAP_PER_MIN",   "120"))
SEMANTIC_ROUTE_CACHE_MAX     = int(os.environ.get("SEMANTIC_ROUTE_CACHE_MAX",     "10000"))
SEMANTIC_ROUTE_RECENT_MAX    = int(os.environ.get("SEMANTIC_ROUTE_RECENT_MAX",    "500"))
SEMANTIC_ROUTE_SCAN_CAP      = int(os.environ.get("SEMANTIC_ROUTE_SCAN_CAP",      "4000"))
SEMANTIC_ROUTE_TOPK          = int(os.environ.get("SEMANTIC_ROUTE_TOPK",          "50"))
SEMANTIC_ROUTE_TTL_SEC       = int(os.environ.get("SEMANTIC_ROUTE_TTL_SEC",       "3600"))
SEMANTIC_ROUTE_LOCAL_ONLY    = os.environ.get("SEMANTIC_ROUTE_LOCAL_ONLY", "1") == "1"
SEMANTIC_ROUTE_EXTERNAL_OK   = os.environ.get("SEMANTIC_ROUTE_EXTERNAL_OK", "0") == "1"
SEMANTIC_ROUTE_DEBUG         = os.environ.get("SEMANTIC_ROUTE_DEBUG", "0") == "1"

ROUTE_CACHE_PREFIX = "var/cache/router/"
GPU_ROUTE          = "/dev/gpu"
GPU_CONF_WORLD     = "etc/gpu.conf"

# Schemes that count as "local" for the /etc/gpu.conf backend-policy
# check. Additions go via env: SEMANTIC_ROUTE_LOCAL_SCHEMES=ollama,foo
_LOCAL_SCHEMES = set(
    s.strip().lower()
    for s in os.environ.get(
        "SEMANTIC_ROUTE_LOCAL_SCHEMES", "ollama"
    ).split(",")
    if s.strip()
)


# ====================================================================
# router render fingerprint — rotates on prompt / config change
# ====================================================================
#
# PLAN §5.1: cache key includes a RENDER_FINGERPRINT axis so a router
# prompt change or config change invalidates stale decisions. v1
# computes this locally from router's own inputs rather than tying
# to semantic's fingerprint — keeps plugin-load order simple and
# avoids a cross-plugin import that elastik's /lib/*-world loader
# does not guarantee works.
#
# If router prompt / config is refactored, bump _ROUTER_PROMPT_VERSION
# and all existing router cache entries miss naturally on next read.

_ROUTER_PROMPT_VERSION = "router-v1"


def _render_fingerprint() -> str:
    """Short stable hex digest. Covers:

      - `_ROUTER_PROMPT_VERSION` — bump on any prompt template edit
      - `SEMANTIC_ROUTE_TOPK` — candidate-count changes re-rank
      - `_ROUTER_POLICY_SHAPE` — "match | multi | none" vocabulary

    Does NOT cover `/etc/gpu.conf` backend identity; that is folded
    into the cache key via `_gpu_conf_fingerprint()` (§5.1 auth axis
    intentionally distinct from prompt-identity axis)."""
    h = hashlib.sha256()
    h.update(_ROUTER_PROMPT_VERSION.encode("utf-8"))
    h.update(b"|topk=")
    h.update(str(SEMANTIC_ROUTE_TOPK).encode("utf-8"))
    h.update(b"|vocab=match|multi|none")
    return h.hexdigest()[:16]


def _gpu_conf_fingerprint() -> str:
    """Hash of `/etc/gpu.conf` contents. Rotates cache globally when
    the operator swaps backend. Matches semantic.py's same-named
    hook so cache invalidation happens on the same event.

    Direct-path read (no server.conn) to avoid auto-creating the
    gpu.conf world when it does not exist yet."""
    db = server.DATA / server._disk_name(GPU_CONF_WORLD) / "universe.db"
    if not db.exists():
        return hashlib.sha256(b"").hexdigest()[:8]
    try:
        import sqlite3
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT stage_html FROM stage_meta WHERE id=1"
        ).fetchone()
        c.close()
    except Exception:
        return hashlib.sha256(b"").hexdigest()[:8]
    raw = (row["stage_html"] if row else b"") or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


# ====================================================================
# path normalisation
# ====================================================================

def _normalize_path(raw: str) -> str:
    """Lowercase, NFC-normalise, strip leading/trailing slashes. Pure.

    NFC is important for non-ASCII paths: `/café` typed on one
    keyboard may arrive as U+0063 U+0061 U+0066 U+00E9 and from
    another as U+0063 U+0061 U+0066 U+0065 U+0301 — different bytes,
    same visible string, same intent. NFC canonicalises both to the
    precomposed form so they hit the same cache entry."""
    if not raw:
        return ""
    s = unicodedata.normalize("NFC", raw)
    s = s.lstrip("/").rstrip("/")
    return s.lower()


# ====================================================================
# caller-scoped world list (recency + readability)
#
# Data source is filesystem mtime — DATA.iterdir() + per-world
# os.stat on universe.db AND universe.db-wal. See PLAN §3.0.2.a:
#
#   - No global stage_meta stream (it does not exist in elastik's
#     storage model — one universe.db per world).
#   - WAL mode means main-DB mtime lags on hot worlds; stat both and
#     take max so "last write visible on disk" is honest.
#   - Heap bounded at SEMANTIC_ROUTE_SCAN_CAP so N_worlds >> cap
#     does not blow memory.
# ====================================================================

def _scan_world_recency(max_entries: int):
    """Return the top `max_entries` worlds by last-write mtime,
    newest first.

    'Last-write' is `max(mtime(universe.db), mtime(universe.db-wal))`
    because elastik runs each world in WAL mode — the main DB file
    lags arbitrarily on actively-hot worlds (writes land in -wal
    until checkpoint). See PLAN §3.0.2.a.i.

    Two os.stat per world directory (main + WAL); no sqlite opens.
    Does NOT stat universe.db-shm — that file's mtime moves on
    reader opens and would pollute the 'last write' signal.

    Skips `.trash` and dot-prefixed dirs. Uses a size-bounded heap
    so N_worlds >> max_entries does not blow memory.

    Returns a list of (mtime, logical_name) pairs, sorted DESC by
    mtime. Blocking filesystem ops — caller wraps in
    asyncio.to_thread."""
    entries = []
    try:
        it = server.DATA.iterdir()
    except (FileNotFoundError, PermissionError):
        return entries
    for d in it:
        if not d.is_dir():
            continue
        if d.name == ".trash" or d.name.startswith("."):
            continue
        udb = d / "universe.db"
        wal = d / "universe.db-wal"
        try:
            main_mtime = udb.stat().st_mtime
        except (FileNotFoundError, PermissionError):
            continue                        # not a world dir
        try:
            wal_mtime = wal.stat().st_mtime
        except (FileNotFoundError, PermissionError):
            wal_mtime = 0.0                 # no WAL = idle world;
                                            # main mtime is the honest
                                            # signal on its own
        mtime = max(main_mtime, wal_mtime)
        logical = server._logical_name(d.name)
        # heapq of size <= max_entries keyed by mtime so the oldest
        # entry sits at the top and gets evicted when full.
        if len(entries) < max_entries:
            heapq.heappush(entries, (mtime, logical))
        elif entries[0][0] < mtime:
            heapq.heapreplace(entries, (mtime, logical))
    entries.sort(key=lambda e: -e[0])
    return entries


def _cap_tag(mode: str, prefix: str) -> str:
    """Canonicalise a cap tag so equivalent cap tokens in different
    percent-encoded forms produce the SAME tag string.

    Example:
      prefix "/home/caf%C3%A9"  (what the browser mints)
      prefix "/home/café"       (already-decoded form)
    Both collapse to "cap:<mode>:/home/café" here.

    Decoded UTF-8 URL form is the space `_name_to_url` produces, so
    downstream `_caller_can_read` compares apples-to-apples without
    having to re-decode. Cache keys also unify: one cap = one tag =
    one cache entry, regardless of how any given client chose to
    encode the prefix at `/auth/mint` time.

    Codex P2: before this canonicalisation, a cap minted with a
    non-ASCII prefix like "/home/café" was signed verbatim as
    "/home/caf%C3%A9" (the query-string form server.py sees), and
    _caller_can_read compared that literal percent-encoded string
    against _name_to_url's decoded form — they never matched.
    """
    import urllib.parse
    decoded = urllib.parse.unquote(prefix or "")
    return f"cap:{mode}:{decoded}"


def _auth_scope_tag(scope) -> str:
    """Short stable tag for cache-key axis and pool filtering.

    Participates in the cache key so two callers at different tiers
    never share cache entries even when their normalized paths
    collide. See PLAN §3.0.4.

    Return values:
      "T1"                — anonymous / no auth
      "T2"                — auth token (or localhost bypass)
      "T3"                — approve token
      "cap:<mode>:<pfx>"  — capability token, HMAC-verified. The
                             prefix is always returned in DECODED
                             UTF-8 URL form (via `_cap_tag`) so two
                             clients minting the same cap with
                             different percent-encoding choices get
                             the same scope-tag and the same pool.

    Cap-token handling is **router-specific**, not a straight
    passthrough of server._check_auth:

    server._check_auth validates a cap against the CURRENT request
    path — if the cap is `/home/scratch` and the URL is `/scratchy`
    (an unmatched typo, which is the only way router gets invoked),
    the path-in-scope check fails and _check_auth returns None.
    Router would then degrade the caller to T1 and serve them the
    WHOLE T1 pool, which is BROADER than the cap's intended scope.
    That's a silent permissions loosening at routing time.

    Router's correct behaviour: verify the cap HMAC + expiration
    (via server._verify_cap, which does NOT do path scoping), then
    use the cap's prefix to FILTER the candidate pool instead. The
    request path being out of cap scope is the whole reason router
    is running — the user typo'd into territory their cap doesn't
    cover, and router's job is to steer them back inside the cap's
    real prefix.

    Codex P1 found both halves of this (the fast-path degrade here
    and the URL-vs-internal prefix mismatch in _caller_can_read).
    """
    # Fast path — opaque tokens, Basic auth, localhost bypass all
    # return concrete non-None levels from _check_auth.
    level = server._check_auth(scope)
    if level == "auth":
        return "T2"
    if level == "approve":
        return "T3"
    if isinstance(level, str) and level.startswith("cap:"):
        # Cap that WAS in scope for this request. _check_auth
        # returns "cap:<mode>:<prefix>" where <prefix> is verbatim
        # from _verify_cap (still percent-encoded in the common
        # browser case). Re-canonicalise through _cap_tag so the
        # downstream comparison space matches _name_to_url's
        # decoded form.
        parts = level.split(":", 2)
        if len(parts) == 3:
            return _cap_tag(parts[1], parts[2])
        return level

    # level is None. Could be genuinely anonymous, OR a cap token
    # whose scope excludes the current (typo) path. Router cares
    # about the second case — grab the cap's real prefix and use it
    # to scope the pool, independent of the request path.
    for k, v in scope.get("headers", []) or []:
        if k != b"authorization":
            continue
        a = v.decode("utf-8", "replace")
        if not a.startswith("Bearer "):
            break
        tok = a[7:]
        if "." not in tok:
            # Opaque token that failed _check_auth would have hit
            # the fast path already; if we're here, the opaque
            # token was wrong (mismatched constant-time compare).
            break
        try:
            cap = server._verify_cap(tok)
        except Exception:
            cap = None
        if cap is None:
            # Invalid or expired cap — treat as anonymous rather
            # than silently succeeding.
            break
        prefix, _exp, mode = cap
        return _cap_tag(mode, prefix)
    return "T1"


#
# /home/-backed worlds are stored WITHOUT the /home/ prefix on disk
# (elastik's canonicalisation strips it at write time — see
# server.py's URL-to-world mapping). So an internal world name like
# "sales-report" is reachable by URL /home/sales-report, and a name
# like "etc/gpu.conf" is reachable by URL /etc/gpu.conf. Read-auth
# predicates here work on the internal form.
#
# Two separate blocklists apply:
#
# _ROUTER_BLOCKED_PREFIXES — infrastructure namespaces that are
#   NEVER valid routing targets regardless of caller tier. T3 can
#   still READ these worlds by direct navigation; router simply
#   never SUGGESTS them. This covers:
#     var/    — caches and logs (router writes its own cache here;
#                 including it in the pool destabilises T2's cache
#                 fingerprint since every router call births a new
#                 var/cache/router/<hash> world)
#     lib/    — plugin source code
#     boot/   — startup config
#     dev/    — device endpoints (not content worlds)
#     dav/    — WebDAV collection (not content worlds)
#     auth/   — auth endpoints (not content worlds)
#     shaped/ — semantic derivatives, not sources
#     bin/    — plugin route aliases
#     usr/    — skills / renderers
#
# _T1_BLOCKED_PREFIXES — additional T1 hide-list. T1 (anonymous)
#   also cannot see etc/* (operator config) even though higher
#   tiers may route there.
#
# Codex P2: the earlier T2/T3 branch returned True blanket, which
# pulled var/cache/router/* into T2's pool. Every router call
# wrote a new cache world, which changed T2's world_list_fingerprint,
# which busted T2's cache on every subsequent identical request.
# The test suite even worked around this by using T1 for cache-hit
# assertions. With _ROUTER_BLOCKED_PREFIXES applied globally,
# T2 cache fingerprints stay stable and the PLAN's "readable and
# user-facing" candidate-set promise is actually honoured.

_ROUTER_BLOCKED_PREFIXES = (
    "var/", "lib/", "boot/", "dev/", "dav/",
    "auth/", "shaped/", "bin/", "usr/",
)

_T1_BLOCKED_PREFIXES = (
    "etc/",
)


def _starts_with_any(name: str, prefixes) -> bool:
    """Helper — True iff `name` equals a prefix stem or starts with
    one of the listed prefixes."""
    for p in prefixes:
        if name == p.rstrip("/"):
            return True
        if name.startswith(p):
            return True
    return False


def _caller_can_read(scope_tag: str, world_name: str) -> bool:
    """Router-candidacy predicate. Combines a GLOBAL infrastructure
    filter with tier-specific read-auth.

    Global filter (_ROUTER_BLOCKED_PREFIXES) applies to all tiers.
    Worlds in var/, lib/, boot/, dev/, dav/, auth/, shaped/, bin/,
    usr/ are never valid routing targets — they're infrastructure
    or derivatives, not user-facing content. T2/T3 can still READ
    them by direct navigation; router simply never surfaces them.

    Tier-specific:
      T3              — anything not in the global blocklist.
      T2              — same as T3 (T2 == T3 for READ visibility in
                        elastik's current auth code; we keep the
                        tag distinct in the cache key so a future
                        split doesn't silently poison cache).
      T1              — as T3 minus etc/* (anonymous can't see
                        operator config).
      cap:<mode>:<pfx>— can read ONLY names starting with <pfx>,
                        AND the global blocklist still applies
                        (a cap scoped to var/cache/... cannot
                        coerce router into using cache worlds as
                        suggestion targets).
    """
    # Global routing filter first — applies to every tier.
    if _starts_with_any(world_name, _ROUTER_BLOCKED_PREFIXES):
        return False
    if scope_tag in ("T2", "T3"):
        return True
    if scope_tag == "T1":
        if _starts_with_any(world_name, _T1_BLOCKED_PREFIXES):
            return False
        return True
    if scope_tag.startswith("cap:"):
        # scope_tag = "cap:<mode>:<decoded-url-prefix>".
        #
        # `_auth_scope_tag` + `_cap_tag` guarantee the prefix is in
        # DECODED UTF-8 URL form (same space `_name_to_url`
        # produces), so the comparison here is purely
        # prefix-in-prefix on equivalent strings.
        #
        # Two earlier bugs are closed here:
        #   Codex P1: previous code compared URL-form prefix
        #     against disk-form world names ("scratch/notes" vs
        #     "/home/scratch"); fixed by routing through
        #     _name_to_url.
        #   Codex P2: percent-encoded cap prefixes like
        #     "/home/caf%C3%A9" didn't match their decoded
        #     equivalents; fixed by centralising canonicalisation
        #     in _cap_tag so both fast- and slow-path produce
        #     decoded prefixes only.
        parts = scope_tag.split(":", 2)
        if len(parts) < 3:
            return False
        prefix_decoded = parts[2]
        if not prefix_decoded:
            return False
        # Normalise: ensure leading slash, strip trailing slash.
        prefix_url = "/" + prefix_decoded.lstrip("/").rstrip("/")
        if prefix_url == "/":
            return False
        name_url = _name_to_url(world_name)
        return (name_url == prefix_url
                or name_url.startswith(prefix_url + "/"))
    return False


def _caller_readable_worlds(scope, limit: int):
    """The `limit` most recent READABLE-TO-CALLER worlds.

    See PLAN §3.0.2 contract. Filter-during-walk: if pool-shrink
    had been done naively ("top-limit by mtime, then filter") a
    T3-heavy burst in /etc/* would starve T1 callers of their
    readable /home/* candidates even when the latter sit just
    outside the first `limit` rows.

    Steps:
      1. _scan_world_recency(SEMANTIC_ROUTE_SCAN_CAP) — top N by
         max(mtime(universe.db), mtime(universe.db-wal)) via heap
      2. walk that list applying _caller_can_read predicate per name
      3. stop when `limit` readable names collected

    Blocking filesystem ops inside _scan_world_recency — caller
    wraps the whole helper in asyncio.to_thread."""
    scope_tag = _auth_scope_tag(scope)
    candidates_ordered = _scan_world_recency(SEMANTIC_ROUTE_SCAN_CAP)
    collected = []
    for _mtime, name in candidates_ordered:
        if _caller_can_read(scope_tag, name):
            collected.append(name)
            if len(collected) >= limit:
                break
    return collected


def _world_list_fingerprint(worlds) -> str:
    """sha256 of sorted world names, short hex digest. Rotates on
    birth / death / recency-reshuffle of worlds visible to THIS
    caller. Per-caller-scope, not global."""
    h = hashlib.sha256()
    for name in sorted(worlds):
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ====================================================================
# candidate pre-filter — stdlib scoring, no SLM
# ====================================================================

def _levenshtein(a: str, b: str, max_d: int = 32) -> int:
    """Classic DP Levenshtein, bounded for speed.

    `max_d` short-circuits: if a row's minimum exceeds `max_d`, we
    stop and return max_d+1. For our pre-filter purposes, any
    distance over ~32 is 'definitely not a typo match' and the
    candidate loses anyway. Saves CPU on long strings.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > max_d:
        return max_d + 1
    # Single-row DP.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            val = min(ins, dele, sub)
            curr.append(val)
            if val < row_min:
                row_min = val
        if row_min > max_d:
            return max_d + 1
        prev = curr
    return prev[-1]


def _score_candidate(query: str, world: str) -> int:
    """Higher = better match. Pure, deterministic.

    Scoring axes (PLAN §3.2):
      - substring containment (query in world, or world in query):
        +100 bonus. A user who got the right word but wrong
        hierarchy should not be filtered out.
      - character-level edit distance: -distance penalty.
      - shared prefix length: +len(prefix) bonus. Right folder,
        wrong leaf should still rank high.
    """
    q = query.lower()
    w = world.lower()
    score = 0
    if q in w:
        score += 100
    elif w in q:
        score += 100
    # Shared prefix length.
    prefix_len = 0
    for ca, cb in zip(q, w):
        if ca == cb:
            prefix_len += 1
        else:
            break
    score += prefix_len
    # Edit distance penalty.
    d = _levenshtein(q, w, max_d=32)
    score -= d
    return score


def _candidate_prefilter(query: str, worlds, top_k: int):
    """Top-K by `_score_candidate`, descending. Ties broken
    alphabetically ascending (stable order so cache keys are
    deterministic across runs)."""
    scored = [(-_score_candidate(query, w), w) for w in worlds]
    scored.sort()   # -score ASC = score DESC; ties by world ASC
    return [w for _neg, w in scored[:top_k]]


# ====================================================================
# cache
# ====================================================================
#
# Router's cache lives in its own subtree (/var/cache/router/*) so
# eviction is independent of /shaped/*'s cache. Same world-as-cache
# primitive either way. See PLAN §5.

def _route_cache_key(normalized: str, world_fp: str,
                     auth_tag: str) -> str:
    """sha256 of the four-axis tuple (§5.1):
         normalized_path || world_list_fingerprint
           || auth_scope_tag || RENDER_FINGERPRINT

    The RENDER_FINGERPRINT axis folds in both the router's own
    prompt/config hash AND the /etc/gpu.conf backend hash so an
    operator swapping backend invalidates all router cache entries
    at once."""
    h = hashlib.sha256()
    h.update(normalized.encode("utf-8"))
    h.update(b"\x00")
    h.update(world_fp.encode("utf-8"))
    h.update(b"\x00")
    h.update(auth_tag.encode("utf-8"))
    h.update(b"\x00")
    h.update(_render_fingerprint().encode("utf-8"))
    h.update(b"|gpu=")
    h.update(_gpu_conf_fingerprint().encode("utf-8"))
    return h.hexdigest()


def _read_route_cache(key: str):
    """Return the cached decision dict, or None on miss / TTL expired.

    Decision dict shape (matches what _write_route_cache persists):
      {"kind":       "single" | "multi" | "none",
       "target":     str | None,     # single only
       "candidates": list[str],      # multi only, else []
       "prose":      str | None,     # none only
       "created_at": float}          # unix timestamp

    Miss semantics mirror semantic.py's cache: server.conn()
    auto-creates an empty stage_meta row on first touch; version=0
    means 'never written' and must read as miss. Real writes bump
    version, so version>0 is the 'cache hit' signal."""
    name = ROUTE_CACHE_PREFIX + key
    try:
        row = server.conn(name).execute(
            "SELECT stage_html, version FROM stage_meta WHERE id=1"
        ).fetchone()
    except Exception:
        return None
    if not row or (row["version"] or 0) == 0:
        return None
    raw = row["stage_html"] or b""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        decision = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(decision, dict):
        return None
    # TTL check — see PLAN §5.4. world_list_fingerprint only catches
    # births/deaths; decisions in the same fingerprint window still
    # age out on absolute wall-clock time.
    created = decision.get("created_at")
    try:
        created = float(created)
    except (TypeError, ValueError):
        return None
    if time.time() - created > SEMANTIC_ROUTE_TTL_SEC:
        return None
    return decision


def _write_route_cache(key: str, decision: dict) -> None:
    """Persist a decision under var/cache/router/<key>.

    Appends an audit event to the cache world's HMAC chain — same
    pattern semantic's _write_cached uses, so cache writes are
    auditable alongside every other write in the system."""
    name = ROUTE_CACHE_PREFIX + key
    payload = dict(decision)
    payload.setdefault("created_at", time.time())
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    c = server.conn(name)
    c.execute(
        "UPDATE stage_meta SET stage_html=?, ext='json', "
        "version=version+1, updated_at=datetime('now') WHERE id=1",
        (body,),
    )
    c.commit()
    try:
        server.log_event(name, "router_cache_write", {
            "key": key,
            "kind": decision.get("kind"),
            "bytes": len(body),
            "render_fp": _render_fingerprint(),
        })
    except Exception:
        pass


def _evict_route_cache_if_over_cap() -> None:
    """LRU-by-updated_at eviction using server._release_world +
    server._move_to_trash primitives. Same shape as
    semantic._evict_if_over_cap; scoped to `var/cache/router/*`
    by prefix so shape cache is untouched."""
    prefix_disk = server._disk_name(ROUTE_CACHE_PREFIX)
    if not server.DATA.exists():
        return
    entries = []
    for d in server.DATA.iterdir():
        if not d.is_dir():
            continue
        if not d.name.startswith(prefix_disk):
            continue
        if not (d / "universe.db").exists():
            continue
        entries.append(d)
    if len(entries) <= SEMANTIC_ROUTE_CACHE_MAX:
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
        stats.append((ts, name))
    stats.sort()   # oldest first
    excess = len(entries) - SEMANTIC_ROUTE_CACHE_MAX
    for _ts, name in stats[:excess]:
        try:
            server._release_world(name)
            server._move_to_trash(name)
        except Exception:
            pass


# ====================================================================
# rate cap — separate deque from semantic's shape cap
# ====================================================================
#
# PLAN §4 / L8: router and shape have SEPARATE rate budgets. A typo-
# heavy crawler hitting 404 paths must not drain the shape budget.

_ROUTE_WINDOW = collections.deque()


def _may_route() -> bool:
    """Sliding-60s window against SEMANTIC_ROUTE_CAP_PER_MIN. Same
    mechanism as semantic._may_generate; own deque.

    Returns True and records 'now' if the call is under cap. Returns
    False and records nothing if the call would exceed cap."""
    now = time.time()
    cutoff = now - 60.0
    while _ROUTE_WINDOW and _ROUTE_WINDOW[0] < cutoff:
        _ROUTE_WINDOW.popleft()
    if len(_ROUTE_WINDOW) >= SEMANTIC_ROUTE_CAP_PER_MIN:
        return False
    _ROUTE_WINDOW.append(now)
    return True


# ====================================================================
# backend policy — /etc/gpu.conf scheme gate
# ====================================================================

def _read_backend_conf():
    """Return (scheme, endpoint) from /etc/gpu.conf, or ('', '').

    Direct-path read — does NOT auto-create the gpu.conf world.
    `endpoint` is everything after `scheme://` with trailing slashes
    trimmed. `scheme` is lowercased. Missing file, malformed line,
    or a line without `://` all map to the empty pair.
    """
    db = server.DATA / server._disk_name(GPU_CONF_WORLD) / "universe.db"
    if not db.exists():
        return "", ""
    try:
        import sqlite3
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT stage_html FROM stage_meta WHERE id=1"
        ).fetchone()
        c.close()
    except Exception:
        return "", ""
    raw = (row["stage_html"] if row else b"") or b""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "://" not in s:
            return "", ""
        scheme, rest = s.split("://", 1)
        return scheme.strip().lower(), rest.strip().rstrip("/")
    return "", ""


def _backend_scheme() -> str:
    """First-line scheme of /etc/gpu.conf, or '' if unset.
    Kept for backwards-compat with earlier code paths that only
    needed the scheme; new code should prefer _read_backend_conf()."""
    return _read_backend_conf()[0]


def _is_loopback_host(host: str) -> bool:
    """True iff `host` refers to this machine's own network stack.

    Accepts:
      - "localhost" (any case)
      - any IPv4 in 127.0.0.0/8
      - IPv6 ::1 (bracketed or bare)

    Does NOT treat RFC1918 ranges (10.x, 192.168.x, 172.16/12) as
    local — those are LAN addresses. Router's privacy posture is
    "within my machine," not "within my subnet." An `ollama://
    10.0.0.5:11434` endpoint still sends prompts off the host; the
    exfiltration boundary the PLAN and README describe would leak.
    """
    if not host:
        return False
    h = host.strip()
    # IPv6 with brackets: strip them for ipaddress parsing.
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    if h.lower() == "localhost":
        return True
    # Try strict IP parsing. `ipaddress.ip_address` accepts both
    # v4 and v6, raises ValueError for hostnames.
    try:
        import ipaddress
        return ipaddress.ip_address(h).is_loopback
    except (ValueError, ImportError):
        return False


def _split_host_port(endpoint: str):
    """Extract the host portion of `scheme://host[:port][/path]`
    endpoint form. Returns "" if the endpoint is empty or
    unparseable.

    Handles:
      "127.0.0.1:11434"          -> "127.0.0.1"
      "localhost:11434"          -> "localhost"
      "api.example.com"          -> "api.example.com"
      "api.example.com/path"     -> "api.example.com"
      "[::1]:11434"              -> "::1"
      "[2001:db8::1]:443/v1"     -> "2001:db8::1"
    """
    if not endpoint:
        return ""
    # Strip any path component first.
    if "/" in endpoint:
        endpoint = endpoint.split("/", 1)[0]
    # Bracketed IPv6: [host]:port
    if endpoint.startswith("["):
        closing = endpoint.find("]")
        if closing > 0:
            return endpoint[1:closing]
    # Otherwise it's host:port or bare host. If there's exactly one
    # colon, assume host:port (IPv4 or hostname). Multiple colons
    # means bare IPv6 without brackets — no port split possible.
    if endpoint.count(":") == 1:
        return endpoint.rsplit(":", 1)[0]
    return endpoint


def _backend_is_local() -> bool:
    """True iff /etc/gpu.conf names a LOCAL backend — both the
    scheme is in _LOCAL_SCHEMES AND the endpoint's host resolves
    to a loopback address.

    Scheme-only checks (the earlier implementation) let
    `ollama://10.0.0.5:11434` pass the local-only policy even
    though prompts and candidate names leave the machine. Codex
    flagged this as P1 — privacy / exfiltration boundary defeated
    by a LAN IP masquerading as local.
    """
    scheme, endpoint = _read_backend_conf()
    if scheme not in _LOCAL_SCHEMES:
        return False
    host = _split_host_port(endpoint)
    return _is_loopback_host(host)


def _policy_allows_slm() -> bool:
    """Combines SEMANTIC_ROUTE_LOCAL_ONLY + SEMANTIC_ROUTE_EXTERNAL_OK
    with _backend_is_local() into one boolean.

    Truth table:
      local backend                  -> True
      non-local + LOCAL_ONLY=1       -> False
      non-local + LOCAL_ONLY=0       -> True  (operator turned the
                                                local-only default off)
      non-local + EXTERNAL_OK=1      -> True  (explicit opt-in)
      non-local + EXTERNAL_OK=0 + LOCAL_ONLY=1 -> False

    Default posture: LOCAL_ONLY=1, EXTERNAL_OK=0 — router is
    disabled when the backend is external, matching PLAN §7.2."""
    if _backend_is_local():
        return True
    if SEMANTIC_ROUTE_EXTERNAL_OK:
        return True
    if not SEMANTIC_ROUTE_LOCAL_ONLY:
        return True
    return False


# ====================================================================
# SLM call
# ====================================================================

class _RouterSLMUnavailable(Exception):
    """Raised when /dev/gpu is not registered, /etc/gpu.conf is
    missing, backend returns an error, or local-only policy rejects
    a non-local backend. Caller maps to static 404."""


def _build_router_prompt(query: str, candidates) -> str:
    """PLAN §3.3 prompt shape. Pure string build — no SLM, no I/O.

    Structured so the SLM's reply is easy to parse deterministically
    (MATCH / MULTI / NONE prefix on the first line). Nothing other
    than the request path and candidate names enters the prompt —
    no auth, no headers, no request body, no worlds outside the
    pre-filter top-K."""
    lines = ["REQUEST_PATH: " + query, "CANDIDATES:"]
    for c in candidates:
        lines.append("  " + c)
    lines.append("")
    lines.append(
        "Reply with exactly ONE line, prefixed by one of:"
    )
    lines.append(
        "  MATCH: <exact world name from CANDIDATES>"
    )
    lines.append(
        "  MULTI: <comma-separated names from CANDIDATES, max 5>"
    )
    lines.append(
        "  NONE:  <one sentence of prose explaining why nothing fits>"
    )
    lines.append(
        "Only names from CANDIDATES are valid for MATCH and MULTI. "
        "If unsure, prefer NONE."
    )
    return "\n".join(lines)


async def _call_router_slm(prompt: str, scope) -> dict:
    """POST prompt to /dev/gpu (non-stream — routing needs latency
    more than first-byte). Returns a decision dict:

      {"kind": "single", "target": "<name>"}
      {"kind": "multi",  "candidates": ["<name>", ...]}
      {"kind": "none",   "prose": "<one sentence>"}

    Raises _RouterSLMUnavailable on any backend failure.

    Auth bridge: router is the canonical *trusted internal caller*
    of /dev/gpu. The whole point of router is resolving typo /
    natural-language URLs for anonymous (T1) users who are neither
    authenticated against /dev/gpu nor will ever be. gpu.py's
    inline POST auth gate would 401 every T1 router call if we
    forwarded the original scope unchanged.

    Fix: stamp an `_internal_caller = "router"` sentinel into a
    COPY of the caller's scope before handing it to gpu's handler.
    gpu.py treats this as "trusted loopback, bypass auth, keep the
    rate cap and cost accounting." The sentinel is a top-level ASGI
    scope key — server-constructed only, not settable from an HTTP
    header — so it cannot be forged from outside. Same non-
    forgeability the `_router_triggered` sentinel in server.py
    depends on.

    Every /dev/gpu and /dev/gpu/stream audit event now carries a
    `"caller"` field so operators reviewing logs can see which
    subset of SLM traffic came from router vs direct external use.
    """
    gpu_handler = server._plugins.get(GPU_ROUTE)
    if gpu_handler is None:
        raise _RouterSLMUnavailable(f"{GPU_ROUTE} not registered")
    internal_scope = dict(scope or {})
    internal_scope["_internal_caller"] = "router"
    result = await gpu_handler("POST", prompt, {"_scope": internal_scope})

    if not isinstance(result, dict):
        raise _RouterSLMUnavailable(
            f"gpu result type: {type(result).__name__}")
    status = result.get("_status", 200)
    if "error" in result or status >= 400:
        raise _RouterSLMUnavailable(
            f"gpu {status}: {str(result.get('error') or '')[:200]}"
        )
    text = result.get("_body")
    if text is None:
        raise _RouterSLMUnavailable("gpu returned no body")
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    return _parse_slm_reply(text)


def _parse_slm_reply(text: str) -> dict:
    """Parse the SLM reply into a structured decision.

    Scans the reply line by line for the first MATCH: / MULTI: / NONE:
    prefix. Trailing lines are ignored (models sometimes add a polite
    sentence after the answer — we don't want to fail on that).

    On a reply we can't parse at all, return a NONE with a generic
    prose body. The outer handler will then fall back to
    static-404 if preferred, or emit the generic prose — either way,
    the router does not hang or crash on a malformed reply."""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        upper = s.upper()
        if upper.startswith("MATCH:"):
            target = s[len("MATCH:"):].strip()
            # Normalise: strip leading slash if present.
            if target.startswith("/"):
                target = target.lstrip("/")
            return {"kind": "single", "target": target}
        if upper.startswith("MULTI:"):
            body = s[len("MULTI:"):].strip()
            names = [n.strip().lstrip("/")
                     for n in body.split(",") if n.strip()]
            return {"kind": "multi", "candidates": names[:5]}
        if upper.startswith("NONE:"):
            prose = s[len("NONE:"):].strip()
            return {"kind": "none", "prose": prose or "not found"}
    # No recognised prefix — treat as NONE with generic prose.
    return {"kind": "none", "prose": (text.strip()[:200]
                                      or "no match")}


# ====================================================================
# response composition
# ====================================================================

_HEADERS_GENERATED = [("X-Semantic-Route-Source", "slm")]

# FHS prefixes that elastik's URL-to-world mapping preserves
# verbatim. Anything not starting with one of these (e.g. the
# internal name "sales-report") is a `/home/*` world whose `/home/`
# prefix was stripped on storage — see server.py's canonicalisation.
# Router reverses this for the Location header so clients actually
# land on a routable URL. Mirrors index.html's `_fhs` array.
_FHS_PREFIXES = (
    "home/", "etc/", "usr/", "var/", "boot/",
    "mnt/", "proc/", "lib/", "shaped/", "dev/",
    "bin/", "dav/", "auth/",
)


def _name_to_url(name: str) -> str:
    """Convert an internal world name to a human-readable URL path.

      "sales-report"          -> "/home/sales-report"
      "home/sales-report"     -> "/home/sales-report"   (idempotent)
      "etc/gpu.conf"          -> "/etc/gpu.conf"
      "lib/router"            -> "/lib/router"
      "café"                  -> "/home/café"           (raw Unicode)

    Matches server.py's URL-to-name canonicalisation in reverse:
    names without a known FHS prefix live in the /home/ namespace
    and need the prefix restored for a working redirect.

    Return value may contain raw non-ASCII characters — use this
    form for HTML body text and diagnostic display. For HTTP
    header values (Location, Link, etc.), pass through
    `_url_header_quote` so the URI gets percent-encoded per
    RFC 7230; raw Unicode in a header URI breaks stricter proxies
    and clients.
    """
    clean = name.lstrip("/")
    for pfx in _FHS_PREFIXES:
        if clean == pfx.rstrip("/") or clean.startswith(pfx):
            return "/" + clean
    return "/home/" + clean


def _url_header_quote(url: str) -> str:
    """Percent-encode a URL path for use in an HTTP header value.

    Preserves `/` as a structural separator and `%` in case the
    input is already partially encoded. Non-ASCII characters and
    unsafe ASCII get UTF-8 percent-encoded via stdlib
    urllib.parse.quote.

    Header value ASCII-safety is RFC 7230's requirement; raw
    Unicode in a `Location` or `Link` header is a standards
    violation that strict proxies will reject. Browsers tend to
    forgive it, but router's output shouldn't depend on browser
    leniency.
    """
    import urllib.parse
    return urllib.parse.quote(url, safe="/%")


def _response_single(target: str, cache_status: str) -> dict:
    """303 See Other → Location: <percent-encoded URL path>.

    Body: short prose so curl users without `-L` see the decision.
    The prose URL stays human-readable (raw Unicode OK — prose is
    text/plain with charset=utf-8). The `Location` HEADER value
    goes through `_url_header_quote` so non-ASCII characters get
    percent-encoded per RFC 7230."""
    loc_readable = _name_to_url(target)
    loc_header   = _url_header_quote(loc_readable)
    prose = (f"Redirecting to {loc_readable} (router decided this "
             f"is the closest match).")
    return {
        "_status": 303,
        "_body":   prose,
        "_ct":     "text/plain; charset=utf-8",
        "_headers": [
            ("Location", loc_header),
            ("X-Semantic-Route-Cache",  cache_status),
            ("X-Semantic-Route-Source", "slm"),
        ],
    }


def _response_multi(candidates, cache_status: str) -> dict:
    """300 Multiple Choices + a minimal HTML body listing the
    alternatives as clickable links. One `Link: rel="alternate"`
    header per candidate so machine clients can parse without
    rendering the HTML. Max 5 candidates.

    Link URI values and `<a href=...>` targets both get percent-
    encoded via `_url_header_quote` — the href attribute is also
    parsed as a URI reference, so the same encoding rules apply
    there. Display text in `<li>` shows the human-readable form."""
    cut = candidates[:5]
    links = [
        ("Link",
         f'<{_url_header_quote(_name_to_url(c))}>; rel="alternate"')
        for c in cut
    ]
    html_lines = ['<!doctype html><meta charset="utf-8">',
                  '<title>Multiple Choices</title>',
                  '<h1>Multiple matches</h1>',
                  '<p>Router found more than one candidate. Pick one:</p>',
                  '<ul>']
    for c in cut:
        href = _url_header_quote(_name_to_url(c))
        # escape minimal HTML specials in display text
        display = (c.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;"))
        html_lines.append(f'  <li><a href="{href}">{display}</a></li>')
    html_lines.append('</ul>')
    body = "\n".join(html_lines)
    return {
        "_status": 300,
        "_body":   body,
        "_ct":     "text/html; charset=utf-8",
        "_headers": links + [
            ("X-Semantic-Route-Cache",  cache_status),
            ("X-Semantic-Route-Source", "slm"),
        ],
    }


def _response_none_prose(prose: str, cache_status: str) -> dict:
    """404 + SLM-written prose body."""
    safe = (prose or "not found")[:500]
    return {
        "_status": 404,
        "_body":   safe,
        "_ct":     "text/plain; charset=utf-8",
        "_headers": [
            ("X-Semantic-Route-Cache",  cache_status),
            ("X-Semantic-Route-Source", "slm"),
        ],
    }


def _response_static_404(cache_status: str) -> dict:
    """404 with the standard static body. Used when:
      - SLM unavailable
      - rate cap exhausted
      - pool was empty (caller has no readable candidates)
      - backend-policy gate rejected the SLM call
    The X-Semantic-Route-Cache header says which case fired."""
    return {
        "_status": 404,
        "_body":   json.dumps({"error": "not found"}),
        "_ct":     "application/json",
        "_headers": [
            ("X-Semantic-Route-Cache",  cache_status),
            ("X-Semantic-Route-Source", "static"),
        ],
    }


# ====================================================================
# main handler
# ====================================================================

async def handle(method, body, params):
    """Internal hook — server.py's app() dispatches into here AFTER
    all normal plugin/world lookups decline a GET or HEAD request.

    This handler trusts the hook already filtered:
      - method ∈ {GET, HEAD}
      - URL length ≤ _MAX_ROUTE_URL_BYTES
      - traversal (`..` / `//`) already 400'd upstream
      - not a recursion (_router_triggered sentinel)
      - /_router_fallback reservation gate passed

    See PLAN-semantic-router.md §8.2 for the step-by-step contract.
    """
    scope = params.get("_scope") or {}
    raw_path = scope.get("path", "") or ""
    query = _normalize_path(raw_path)

    # 2. Backend policy gate — §7.2 default posture rejects external
    #    backend unless the operator opts in.
    if not _policy_allows_slm():
        return _response_static_404("policy-static-404")

    # 3. Caller-scoped readable pool — §3.0.2 / §3.0.2.a
    #    Blocking filesystem scan wrapped via to_thread so the loop
    #    stays free under concurrent router misses.
    try:
        pool = await asyncio.to_thread(
            _caller_readable_worlds, scope, SEMANTIC_ROUTE_RECENT_MAX)
    except Exception as e:
        # Pool scan should never raise; if it does, degrade to
        # static 404 rather than 500 — router is a fallback plugin
        # and must fail safely.
        return _response_static_404("scan-error-static-404")
    if not pool:
        return _response_static_404("empty-pool-static-404")

    # 4. Pre-filter — pure, no SLM, no rate cap consumed.
    candidates = _candidate_prefilter(query, pool, SEMANTIC_ROUTE_TOPK)
    if not candidates:
        return _response_static_404("empty-pool-static-404")
    pool_set = set(candidates)      # for SLM-hallucination second-line

    # 5. Cache read — 4-axis key (PLAN §5.1). Hit: no cap consumed,
    #    no SLM, no scan beyond step 3.
    auth_tag = _auth_scope_tag(scope)
    world_fp = _world_list_fingerprint(candidates)
    key = _route_cache_key(query, world_fp, auth_tag)
    hit = _read_route_cache(key)
    if hit is not None:
        kind = hit.get("kind")
        if kind == "single":
            target = hit.get("target") or ""
            if target:
                return _response_single(target, "hit")
        elif kind == "multi":
            cands = hit.get("candidates") or []
            if cands:
                return _response_multi(cands, "hit")
        elif kind == "none":
            return _response_none_prose(
                hit.get("prose") or "not found", "hit")
        # malformed hit — treat as miss and re-resolve
    # 6. Rate cap — §4 / L8. Separate from shape cap.
    if not _may_route():
        return _response_static_404("ratelimit-static-404")

    # 7. SLM call → decision dict. Init errors / backend failures
    #    degrade to static 404; we do NOT leak prose via NONE in
    #    that case because the prose would come from generic
    #    fallback text, not the model.
    prompt = _build_router_prompt(query, candidates)
    try:
        decision = await _call_router_slm(prompt, scope)
    except _RouterSLMUnavailable:
        return _response_static_404("slm-unavailable-static-404")

    # 8. Second-line defence against SLM hallucination: validate
    #    that the chosen name(s) are IN the candidate pool. A model
    #    that invents /etc/secret-xyz despite being told "only from
    #    CANDIDATES" gets its answer discarded. See PLAN §3.0
    #    closing test (P1 regression).
    kind = decision.get("kind")
    discard_reason = ""
    if kind == "single":
        target = (decision.get("target") or "").lstrip("/")
        if target not in pool_set:
            discard_reason = (f"target={target!r} not in "
                              f"pool_set ({len(pool_set)} members)")
            decision = {"kind": "none",
                        "prose": "no safe match in readable pool"}
            kind = "none"
    elif kind == "multi":
        cands_raw = decision.get("candidates") or []
        safe = [c.lstrip("/") for c in cands_raw
                if c.lstrip("/") in pool_set]
        if not safe:
            discard_reason = (f"all of {cands_raw!r} rejected by "
                              f"pool_set ({len(pool_set)} members)")
            decision = {"kind": "none",
                        "prose": "no safe match in readable pool"}
            kind = "none"
        else:
            decision = {"kind": "multi", "candidates": safe[:5]}

    # 9. Cache write + evict. Cache-write failures do not block the
    #    response.
    try:
        _write_route_cache(key, decision)
        _evict_route_cache_if_over_cap()
    except Exception:
        pass

    # 10. Emit.
    if kind == "single":
        return _response_single(decision["target"], "generated")
    if kind == "multi":
        return _response_multi(decision["candidates"], "generated")
    resp = _response_none_prose(decision.get("prose") or "not found",
                                "generated")
    if SEMANTIC_ROUTE_DEBUG and discard_reason:
        # HTTP header values are defined as Latin-1 by RFC 7230.
        # A raw UTF-8 string like "café" (c3 a9) emitted verbatim
        # gets decoded on the client side as Latin-1 -> "cafÃ©"
        # mojibake, which breaks test assertions that compare
        # against the canonical Unicode form. Percent-encode both
        # debug values so the wire is ASCII-safe; readers
        # (tests, curl -v) can urllib.parse.unquote to recover
        # the original strings.
        import urllib.parse
        resp["_headers"].append((
            "X-Router-Debug-Discard",
            urllib.parse.quote(discard_reason[:400], safe="/,-_.:=' ")))
        pool_joined = ",".join(sorted(pool_set))[:400]
        resp["_headers"].append((
            "X-Router-Debug-Pool",
            urllib.parse.quote(pool_joined, safe="/,-_.:")))
    return resp


ROUTES = ["/_router_fallback"]
