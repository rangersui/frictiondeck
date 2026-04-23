"""semantic plugin tests.

  Layer 1: pure-helper unit tests          (no server, no ollama)
  Layer 2: HTTP integration with SLM down  (server + plugin, ollama unreachable)
  Layer 3: live SLM generation (optional)  (server + plugin + ollama)

Layer 2 deterministically exercises /shaped/'s fallback + 406 + 429
paths by NOT installing /dev/gpu. semantic's _call_gpu_device then
always raises _SLMUnavailable("/dev/gpu not registered"), so every
cache miss lands in the Accept-gated fallback branch without any
model non-determinism.

Layer 3 is opt-in; it can only check status codes and headers because
the SLM output itself is non-deterministic.

Usage:
  python tests/test_semantic.py                # layers 1 + 2
  python tests/test_semantic.py unit           # layer 1 only
  python tests/test_semantic.py --with-ollama  # layers 1 + 2 + 3

Matches the style of tests/test_plugins.py (homegrown PASS/FAIL/SKIP
counters, urllib.request for HTTP, server.py as a subprocess).
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PASS = 0
FAIL = 0
SKIP = 0

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def test(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  -- {detail}")


def skip(name, reason=""):
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}  ({reason})")


# ====================================================================
# Layer 1: pure-helper unit tests
# ====================================================================

def _stub_server_module():
    """Inject a minimal `server` stub so plugins/semantic.py imports
    cleanly without a running elastik. Only the attributes the plugin
    touches at import time matter; the rest are stubs that raise if
    Layer 1 accidentally calls into server-dependent helpers."""
    stub = types.ModuleType("server")
    from pathlib import Path as _P
    stub.DATA = _P("data")
    stub._db = {}
    stub._disk_name = lambda n: n.replace("/", "%2F")
    stub._logical_name = lambda d: d.replace("%2F", "/")
    stub._valid_name = lambda n: bool(
        n and not n.startswith("/") and not n.endswith("/")
    )

    def _raise(*a, **kw):
        raise RuntimeError("Layer 1 must not reach server-dependent code")

    stub.conn = _raise
    stub.log_event = _raise
    stub._release_world = _raise
    stub._move_to_trash = _raise
    sys.modules["server"] = stub


def run_layer1():
    print("\n=== Layer 1: pure-helper unit tests ===")
    _stub_server_module()
    sys.path.insert(0, os.path.join(ROOT, "plugins"))
    # Re-import if test harness has been invoked multiple times.
    if "semantic" in sys.modules:
        del sys.modules["semantic"]
    import semantic  # type: ignore

    # ---- _parse_accept -------------------------------------------
    test("parse_accept: empty -> */*;q=1.0",
         semantic._parse_accept("") == [("*/*", 1.0)])
    test("parse_accept: single concrete",
         semantic._parse_accept("text/csv") == [("text/csv", 1.0)])
    test("parse_accept: q-sort descending",
         semantic._parse_accept("text/plain;q=0.5, text/csv") ==
         [("text/csv", 1.0), ("text/plain", 0.5)])
    test("parse_accept: preserves wildcard",
         semantic._parse_accept("text/*, image/png;q=0.1") ==
         [("text/*", 1.0), ("image/png", 0.1)])
    test("parse_accept: lowercases mime",
         semantic._parse_accept("TEXT/CSV") == [("text/csv", 1.0)])

    # ---- _accept_allows ------------------------------------------
    test("accept_allows: */* admits anything",
         semantic._accept_allows([("*/*", 1.0)], "image/png"))
    test("accept_allows: exact match",
         semantic._accept_allows([("text/csv", 1.0)], "text/csv"))
    test("accept_allows: rejects different concrete",
         not semantic._accept_allows([("text/csv", 1.0)], "text/plain"))
    test("accept_allows: family wildcard admits family member",
         semantic._accept_allows([("text/*", 1.0)], "text/html"))
    test("accept_allows: family wildcard rejects other family",
         not semantic._accept_allows([("text/*", 1.0)], "image/png"))
    test("accept_allows: q=0 excludes",
         not semantic._accept_allows([("text/csv", 0.0)], "text/csv"))
    test("accept_allows: ignores charset suffix",
         semantic._accept_allows([("text/plain", 1.0)],
                                 "text/plain; charset=utf-8"))

    # ---- _pick_required_ct ---------------------------------------
    test("pick_required_ct: concrete top-q",
         semantic._pick_required_ct([("text/csv", 1.0),
                                     ("text/plain", 0.5)]) == "text/csv")
    test("pick_required_ct: */* stays */*",
         semantic._pick_required_ct([("*/*", 1.0)]) == "*/*")
    test("pick_required_ct: family wildcard degrades to */*",
         semantic._pick_required_ct([("text/*", 1.0)]) == "*/*")
    test("pick_required_ct: empty defaults to */*",
         semantic._pick_required_ct([]) == "*/*")
    test("pick_required_ct: skips q=0 entries",
         semantic._pick_required_ct([("text/csv", 0.0),
                                     ("text/plain", 1.0)]) == "text/plain")

    # ---- _canonicalise_world_path --------------------------------
    test("canonicalise: /home/X strips prefix",
         semantic._canonicalise_world_path("home/metrics") == "metrics")
    test("canonicalise: /home/nested/path",
         semantic._canonicalise_world_path("home/notes/meeting") ==
         "notes/meeting")
    test("canonicalise: /lib/X kept",
         semantic._canonicalise_world_path("lib/clock") == "lib/clock")
    test("canonicalise: /etc/X kept",
         semantic._canonicalise_world_path("etc/manifest") == "etc/manifest")
    test("canonicalise: bare 'home' -> None",
         semantic._canonicalise_world_path("home") is None)
    test("canonicalise: empty -> None",
         semantic._canonicalise_world_path("") is None)

    # ---- _canonicalise_accept (stable for cache key) --------------
    a_ordered = [("text/csv", 1.0), ("text/plain", 0.5)]
    a_reordered = sorted(a_ordered, key=lambda x: x[0])
    test("canonicalise_accept: stable formatting",
         semantic._canonicalise_accept(a_ordered) ==
         "text/csv;q=1.00,text/plain;q=0.50")

    # ---- _cache_key ---------------------------------------------
    FP_A, FP_B = "aaaaaaaa", "bbbbbbbb"  # stand-in gpu fingerprints
    k1 = semantic._cache_key("metrics", 3, "excel/0.1", "text/csv;q=1.00", FP_A)
    k2 = semantic._cache_key("metrics", 3, "excel/0.1", "text/csv;q=1.00", FP_A)
    test("cache_key: deterministic", k1 == k2)
    k3 = semantic._cache_key("metrics", 4, "excel/0.1", "text/csv;q=1.00", FP_A)
    test("cache_key: changes with version", k1 != k3)
    k4 = semantic._cache_key("notes", 3, "excel/0.1", "text/csv;q=1.00", FP_A)
    test("cache_key: changes with world name", k1 != k4)
    k5 = semantic._cache_key("metrics", 3, "grandma/1.0", "text/csv;q=1.00", FP_A)
    test("cache_key: changes with user-agent", k1 != k5)
    k6 = semantic._cache_key("metrics", 3, "excel/0.1", "text/plain;q=1.00", FP_A)
    test("cache_key: changes with accept", k1 != k6)
    k7 = semantic._cache_key("metrics", 3, "excel/0.1", "text/csv;q=1.00", FP_B)
    test("cache_key: changes with gpu_conf fingerprint (backend swap)",
         k1 != k7)

    # ---- _gpu_conf_fingerprint (runtime backend swap detection) ---
    # _read_gpu_conf_raw is stubbed to hit real server, so monkey-patch
    # it for determinism in Layer 1.
    orig_reader = semantic._read_gpu_conf_raw
    semantic._read_gpu_conf_raw = lambda: "ollama://localhost:11434\n"
    fp_ollama = semantic._gpu_conf_fingerprint()
    semantic._read_gpu_conf_raw = lambda: "claude://api.anthropic.com\n"
    fp_claude = semantic._gpu_conf_fingerprint()
    semantic._read_gpu_conf_raw = lambda: ""
    fp_empty = semantic._gpu_conf_fingerprint()
    semantic._read_gpu_conf_raw = lambda: "# just a comment\n\nollama://localhost:11434\n"
    fp_ollama_with_comment = semantic._gpu_conf_fingerprint()
    semantic._read_gpu_conf_raw = orig_reader

    test("gpu_conf_fingerprint: rotates on backend swap",
         fp_ollama != fp_claude)
    test("gpu_conf_fingerprint: empty conf has stable hash",
         len(fp_empty) == 8)
    test("gpu_conf_fingerprint: skips comments + blank lines",
         fp_ollama == fp_ollama_with_comment)
    test("gpu_conf_fingerprint: 8-char hex",
         len(fp_ollama) == 8 and all(c in "0123456789abcdef" for c in fp_ollama))

    # ---- _compute_render_fingerprint (P1: includes MAX_SOURCE) ----
    fp0 = semantic._compute_render_fingerprint()
    test("fingerprint: matches RENDER_FINGERPRINT at import",
         fp0 == semantic.RENDER_FINGERPRINT)

    orig_prompt = semantic.SYSTEM_PROMPT
    semantic.SYSTEM_PROMPT = orig_prompt + " (tweak)"
    fp1 = semantic._compute_render_fingerprint()
    semantic.SYSTEM_PROMPT = orig_prompt
    test("fingerprint: rotates on SYSTEM_PROMPT change", fp1 != fp0)

    orig_maxsrc = semantic.SEMANTIC_MAX_SOURCE
    semantic.SEMANTIC_MAX_SOURCE = orig_maxsrc + 1
    fp2 = semantic._compute_render_fingerprint()
    semantic.SEMANTIC_MAX_SOURCE = orig_maxsrc
    test("fingerprint: rotates on SEMANTIC_MAX_SOURCE change (P1 fix)",
         fp2 != fp0)

    # NOTE: SEMANTIC_MODEL / SEMANTIC_OLLAMA_URL / SEMANTIC_TEMPERATURE /
    # SEMANTIC_MAX_TOKENS / SEMANTIC_TIMEOUT_MS no longer exist --
    # they belong to /dev/gpu + /etc/gpu.conf. RENDER_FINGERPRINT here
    # covers only semantic's own knobs (prompt text + source-truncation
    # boundary). Backend identity is captured by _gpu_conf_fingerprint
    # below, computed per-request from /etc/gpu.conf contents.

    test("fingerprint: only includes semantic's own knobs",
         not hasattr(semantic, "SEMANTIC_MODEL")
         and not hasattr(semantic, "SEMANTIC_OLLAMA_URL")
         and not hasattr(semantic, "SEMANTIC_TEMPERATURE"),
         "ollama-specific config should be gone after /dev/gpu refactor")

    # ---- _parse_slm_output ---------------------------------------
    body, ct, shape = semantic._parse_slm_output(
        'name,value\na,1\n===META===\n{"content_type":"text/csv","shape":"csv"}'
    )
    test("parse_slm: well-formed meta",
         (body, ct, shape) == ("name,value\na,1", "text/csv", "csv"))

    body, ct, shape = semantic._parse_slm_output("hello")
    test("parse_slm: no meta -> text/plain + unknown",
         (body, ct, shape) == ("hello", "text/plain", "unknown"))

    body, ct, shape = semantic._parse_slm_output(
        "partial\n===META===\nnot-json"
    )
    test("parse_slm: invalid meta json falls back",
         (body, ct, shape) == ("partial", "text/plain", "unknown"))

    prompt = semantic._build_prompt(
        "hello",
        "grandma/1.0",
        "text/html",
        "text/plain",
        {"x-meta-title": "Greeting", "x-meta-topic": "demo"},
    )
    test("build_prompt: includes SOURCE_METADATA block",
         "SOURCE_METADATA:" in prompt)
    test("build_prompt: includes x-meta entries",
         "x-meta-title=Greeting" in prompt and "x-meta-topic=demo" in prompt,
         prompt)

    body, ct, shape = semantic._parse_slm_output(
        '\n===META===\n{"content_type":"text/plain","shape":"empty"}'
    )
    test("parse_slm: empty body preserved",
         body == "" and ct == "text/plain" and shape == "empty")

    # ---- _may_generate (rate cap) --------------------------------
    # Reset the deque (in case earlier tests poked it)
    semantic._gen_timestamps.clear()
    orig_cap = semantic.SEMANTIC_GEN_CAP_PER_MIN
    semantic.SEMANTIC_GEN_CAP_PER_MIN = 3
    test("may_generate: first call passes",
         semantic._may_generate())
    test("may_generate: second call passes",
         semantic._may_generate())
    test("may_generate: third call passes",
         semantic._may_generate())
    test("may_generate: fourth call over cap -> False",
         not semantic._may_generate())

    # Fast-forward the sliding window by rewriting the deque
    # to timestamps >60s old.
    semantic._gen_timestamps.clear()
    past = time.monotonic() - 70.0
    for _ in range(3):
        semantic._gen_timestamps.append(past)
    test("may_generate: window rolls over after 60s",
         semantic._may_generate())

    semantic.SEMANTIC_GEN_CAP_PER_MIN = orig_cap
    semantic._gen_timestamps.clear()


# ====================================================================
# Layer 2: HTTP integration with SLM unreachable
# ====================================================================

PORT = 13019
TOKEN = "test-semantic-token"
APPROVE = "test-semantic-approve"
KEY = "test-semantic-hmac-key"


def _free_port_check(port, timeout=10.0):
    """Return True once the port accepts a TCP connection."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.1)
    return False


def _http(method, path, body=None, token="", headers=None):
    try:
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}",
            data=data, method=method,
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, dict(r.getheaders()), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, {}, str(e)


def _start_server(extra_env=None):
    """Spawn server.py in an isolated temporary data directory and
    return (proc, tmp_root).

    Why isolate: server.py's DATA = Path("data") is CWD-relative, and
    plugins like db.py read ELASTIK_DATA. If the test subprocess booted
    against the repo's real data/ tree, any /lib/gpu that the developer
    had previously installed would satisfy semantic's _call_gpu_device
    lookup and flip Layer 2's "SLM unreachable" fallback assertions
    into live-generation responses. Codex reproduced exactly that.

    The temp dir is empty; tests that need preconditions (e.g. a source
    world, or /dev/gpu installed for Layer 3) seed them explicitly.
    Caller MUST shutil.rmtree(tmp_root) in its finally — we return the
    path as the second tuple element so cleanup stays explicit.

    Windows note: server's SQLite conn cache keeps file handles open;
    caller should proc.terminate() + proc.wait() BEFORE rmtree. rmtree
    uses ignore_errors=True as a belt against the race."""
    tmp_root = tempfile.mkdtemp(prefix="elastik-test-semantic-")
    tmp_data = os.path.join(tmp_root, "data")
    os.makedirs(tmp_data, exist_ok=True)

    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(PORT)
    env["ELASTIK_HOST"] = "127.0.0.1"
    env["ELASTIK_TOKEN"] = TOKEN
    env["ELASTIK_APPROVE_TOKEN"] = APPROVE
    env["ELASTIK_KEY"] = KEY
    # ELASTIK_DATA for plugins that read env (e.g. db.py); cwd for
    # server.py's own DATA = Path("data"). Both point at the same
    # tmp tree so the subprocess cannot see the repo's real data/.
    env["ELASTIK_DATA"] = tmp_data
    env["SEMANTIC_GEN_CAP_PER_MIN"] = "60"
    if extra_env:
        env.update(extra_env)

    # Run server.py by absolute path, with cwd=tmp_root, so DATA
    # resolves to tmp_root/data and .env / data/ under ROOT are
    # ignored. server.py's INDEX/SW/MANIFEST use Path(__file__) and
    # survive the cwd change.
    server_py = os.path.join(ROOT, "server.py")
    proc = subprocess.Popen(
        [sys.executable, server_py], env=env, cwd=tmp_root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc, tmp_root


def _stop_server(proc, tmp_root):
    """Terminate the subprocess, wait briefly, then rmtree the temp
    data dir. Swallow all cleanup errors -- the test verdict already
    stands."""
    if proc is not None:
        try: proc.terminate()
        except Exception: pass
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except Exception: pass
    if tmp_root:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _install_gpu_plugin():
    """PUT plugins/gpu.py into /lib/gpu and activate. Only for Layer 3."""
    plugin_path = os.path.join(ROOT, "plugins", "gpu.py")
    with open(plugin_path, "rb") as f:
        src = f.read()
    s1, _, _ = _http("PUT", "/lib/gpu", body=src, token=APPROVE,
                     headers={"Content-Type": "text/x-python"})
    if s1 not in (200, 201):
        return False, f"PUT /lib/gpu -> HTTP {s1}"
    s2, _, _ = _http("PUT", "/lib/gpu/state", body="active", token=APPROVE)
    if s2 not in (200, 204):
        return False, f"PUT /lib/gpu/state -> HTTP {s2}"
    return True, "installed"


def _write_gpu_conf(scheme_endpoint):
    """Write /etc/gpu.conf with the given backend line. Requires APPROVE
    because /etc/* writes are gated that way."""
    s, _, _ = _http("PUT", "/etc/gpu.conf", body=scheme_endpoint,
                    token=APPROVE,
                    headers={"Content-Type": "text/plain"})
    return s in (200, 201)


def _install_plugin():
    """PUT plugins/semantic.py into /lib/semantic and activate."""
    plugin_path = os.path.join(ROOT, "plugins", "semantic.py")
    with open(plugin_path, "rb") as f:
        src = f.read()
    s1, _, _ = _http("PUT", "/lib/semantic", body=src, token=APPROVE,
                     headers={"Content-Type": "text/x-python"})
    if s1 not in (200, 201):
        return False, f"PUT /lib/semantic -> HTTP {s1}"
    s2, _, _ = _http("PUT", "/lib/semantic/state", body="active",
                     token=APPROVE)
    if s2 not in (200, 204):
        return False, f"PUT /lib/semantic/state -> HTTP {s2}"
    return True, "installed"


def _seed_source():
    """PUT a small source world at /home/smoke for /shaped/ to read."""
    body = "metric,value\nrevenue,1200000\ngrowth,0.23\n"
    s, _, _ = _http("PUT", "/home/smoke?ext=csv", body=body, token=TOKEN,
                    headers={"Content-Type": "text/csv"})
    return s in (200, 201)


def run_layer2():
    print("\n=== Layer 2: HTTP integration (SLM unreachable) ===")
    proc, tmp_root = _start_server()
    try:
        if not _free_port_check(PORT, timeout=15):
            test("server boots", False, "server didn't accept connections")
            return
        test("server boots", True)

        ok, detail = _install_plugin()
        test("install semantic plugin", ok, detail)
        if not ok:
            return

        test("seed /home/smoke source world", _seed_source())

        # --- routing edge cases ---
        s, _, _ = _http("GET", "/shaped/nonexistent-xyz", token=TOKEN)
        test("404 on missing world", s == 404)

        # /shaped with no world path. /shaped alone (no trailing slash)
        # matches the plugin, then our handler returns 400.
        s, _, _ = _http("GET", "/shaped/", token=TOKEN)
        test("400 on empty world path", s == 400)

        # --- SLM-down + Accept permits text/plain -> 200 fallback-raw ---
        s, h, body = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/plain", "User-Agent": "smoke/1.0"},
        )
        test("SLM-down + Accept=text/plain -> 200", s == 200,
             f"got {s}")
        test("SLM-down + fallback-raw cache tag",
             h.get("X-Semantic-Cache") == "fallback-raw",
             f"headers={h}")
        test("SLM-down + body is raw source",
             "revenue,1200000" in body,
             f"body={body[:100]!r}")

        # --- SLM-down + text/plain not top -> 503 fallback-503 (v0.2) --
        # Pre-v0.2 this was 406/fallback-406. Track A moved the
        # SLM-infra-failure path to 503 + Retry-After so clients can
        # distinguish "service unavailable" from "Accept unsatisfiable"
        # (the latter stays 406 on SLM-output-mismatch, tested elsewhere).
        s, h, body = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/csv", "User-Agent": "smoke/1.0"},
        )
        test("SLM-down + Accept=text/csv -> 503", s == 503,
             f"got {s}, body={body[:100]!r}")
        test("SLM-down + fallback-503 cache tag",
             h.get("X-Semantic-Cache") == "fallback-503",
             f"headers={h}")
        test("SLM-down 503 carries Retry-After: 5",
             h.get("Retry-After") == "5",
             f"Retry-After={h.get('Retry-After')!r}")

        # --- wildcard Accept takes SLM-down fallback path too ---
        s, h, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "*/*", "User-Agent": "smoke/1.0"},
        )
        test("SLM-down + Accept=*/* -> 200 fallback-raw",
             s == 200 and h.get("X-Semantic-Cache") == "fallback-raw")

        # --- no Accept header -> same as */* ---
        s, h, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"User-Agent": "smoke/1.0"},
        )
        test("SLM-down + no Accept -> 200 fallback-raw",
             s == 200 and h.get("X-Semantic-Cache") == "fallback-raw")

        # --- browser shell vs raw API auth boundary ---
        # Browser navigation to /shaped/<world> is allowed through to the
        # app shell on localhost, but the raw /shaped/* API itself remains
        # header-driven: a bare no-auth API call should still 403.
        s, h, body = _http(
            "GET", "/shaped/home/smoke", token="",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "User-Agent": "Mozilla/5.0 semantic-test",
            },
        )
        test("localhost no-auth browser GET /shaped/<world> -> shell 200",
             s == 200, f"got HTTP {s}")
        test("localhost browser GET /shaped/<world> returns shell HTML",
             h.get("content-type", "").startswith("text/html") and "<!doctype html" in body[:200].lower(),
             f"headers={h} body={body[:150]!r}")
        test("localhost browser GET /shaped/<world> does NOT hit semantic handler",
             not h.get("X-Semantic-Cache"),
             f"unexpected handler headers={h}")
        test("localhost /shaped/<subpath> does NOT serve auto man-page",
             'action="/shaped"' not in body,
             "dispatcher man-page leaked into subpath response")
        s, _, _ = _http(
            "GET", "/shaped/home/smoke", token="",
            headers={"Accept": "text/html", "User-Agent": "grandma/1.0 (big-font)"},
        )
        test("localhost no-auth raw /shaped API -> 403",
             s == 403, f"got HTTP {s}")

        # --- v0.2 regressions: fallback-raw gated by top-q + Accept `*` ---
        # These test the semantic v0.2 hardening: raw-fallback is only
        # offered when text/plain is the client's top preference (not
        # merely admissible), and bare `*` in Accept is treated as */*.

        # v0.2 #1: stronger preference than text/plain + SLM down -> 503
        #          with fallback-503 tag + Retry-After: 5.
        s, h, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "image/png, text/plain;q=0.8",
                     "User-Agent": "mixed/1.0"},
        )
        test("v0.2: image/png top, SLM down -> 503 fallback-503",
             s == 503 and h.get("X-Semantic-Cache") == "fallback-503",
             f"got HTTP {s} cache={h.get('X-Semantic-Cache')}")
        test("v0.2: 503 carries Retry-After: 5",
             h.get("Retry-After") == "5",
             f"Retry-After={h.get('Retry-After')!r}")

        # v0.2 #2: text/plain as top preference + SLM down -> 200 raw.
        s, h, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/plain, text/csv;q=0.5",
                     "User-Agent": "plain-top/1.0"},
        )
        test("v0.2: text/plain top, SLM down -> 200 fallback-raw",
             s == 200 and h.get("X-Semantic-Cache") == "fallback-raw",
             f"got HTTP {s} cache={h.get('X-Semantic-Cache')}")

        # v0.2 #3: Accept: `*` (malformed per RFC 7231 but user-friendly
        #         to fold to */*). Request must reach the handler;
        #         presence of X-Semantic-Cache proves dispatch happened.
        s, h, body = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "*", "User-Agent": "malformed/1.0"},
        )
        test("v0.2: Accept `*` treated as */*, reaches handler",
             bool(h.get("X-Semantic-Cache")),
             f"no X-Semantic-Cache header -> request never dispatched; "
             f"status={s} body={body[:120]!r}")

        # v0.2 #4: text/plain;q=0 exclusion + SLM down. q=0 is stricter
        #         than "just not top" and lands in the same 503 bucket.
        #         The 406 branch (SLM-output-mismatch) is tested
        #         elsewhere by design.
        s, h, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "image/png, text/plain;q=0",
                     "User-Agent": "strict/1.0"},
        )
        test("v0.2: text/plain;q=0 + SLM down -> 503 (same bucket as top-q mismatch)",
             s == 503 and h.get("X-Semantic-Cache") == "fallback-503",
             f"got HTTP {s} cache={h.get('X-Semantic-Cache')}")

        # --- X-Semantic-Render header is populated ---
        s, h, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/plain"},
        )
        test("X-Semantic-Render header present",
             bool(h.get("X-Semantic-Render")),
             f"headers={list(h.keys())}")
        test("X-Semantic-Render is 16 hex chars",
             len(h.get("X-Semantic-Render") or "") == 16)
    finally:
        _stop_server(proc, tmp_root)


def run_layer2_ratecap():
    """Separate server instance with cap=1 so we can exercise both
    ratelimit-raw and ratelimit-429 without flakiness from other tests'
    generation counter state."""
    print("\n=== Layer 2b: HTTP integration (rate cap) ===")
    proc, tmp_root = _start_server(extra_env={"SEMANTIC_GEN_CAP_PER_MIN": "1"})
    try:
        if not _free_port_check(PORT, timeout=15):
            test("server boots (cap=1)", False, "timeout")
            return
        test("server boots (cap=1)", True)

        ok, detail = _install_plugin()
        test("install semantic plugin (cap=1)", ok, detail)
        if not ok: return
        test("seed source (cap=1)", _seed_source())

        # First request with Accept=text/csv: cache miss, _may_generate
        # passes (budget was 1, we just burned it), SLM call fails,
        # returns fallback-503 (Accept=text/csv excludes text/plain,
        # and Track A made SLM-infra-failure a 503 path). Note this
        # IS NOT the rate-limit path -- the rate cap doesn't fire
        # until the SECOND request. First req is a plain SLM-down
        # fallback-503, identical to Layer 2's main block.
        s1, h1, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/csv", "User-Agent": "rc1/1.0"},
        )
        test("rate-cap burn 1: first req -> 503 (fallback-503, SLM down)",
             s1 == 503 and h1.get("X-Semantic-Cache") == "fallback-503",
             f"s={s1} h={h1}")

        # Second request with DIFFERENT UA (so cache key differs, forces
        # miss) and Accept=text/csv. Now cap is exhausted, so instead of
        # even attempting SLM, we get ratelimit-429.
        s2, h2, _ = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/csv", "User-Agent": "rc2/1.0"},
        )
        test("rate-cap burn 2: over cap + Accept=text/csv -> 429 ratelimit-429",
             s2 == 429 and h2.get("X-Semantic-Cache") == "ratelimit-429",
             f"s={s2} h={h2}")
        test("rate-cap: Retry-After present",
             h2.get("Retry-After") == "60",
             f"Retry-After={h2.get('Retry-After')!r}")

        # Third request, Accept permits text/plain: ratelimit-raw, 200.
        s3, h3, body3 = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/plain", "User-Agent": "rc3/1.0"},
        )
        test("rate-cap burn 3: over cap + Accept=text/plain -> 200 ratelimit-raw",
             s3 == 200 and h3.get("X-Semantic-Cache") == "ratelimit-raw",
             f"s={s3} h={h3}")
        test("rate-cap: source bytes surfaced in raw fallback",
             "revenue" in body3)
    finally:
        _stop_server(proc, tmp_root)


# ====================================================================
# Layer 3: live ollama (opt-in)
# ====================================================================

def run_layer3():
    print("\n=== Layer 3: live SLM generation (requires ollama) ===")
    # Sanity check ollama is reachable before bothering to spin up a
    # server pointed at it.
    ollama_url = os.environ.get("SEMANTIC_OLLAMA_URL",
                                "http://127.0.0.1:11434")
    try:
        r = urllib.request.urlopen(ollama_url + "/api/tags", timeout=2)
        r.read()
    except Exception as e:
        skip("Layer 3", f"ollama not reachable at {ollama_url}: {e}")
        return

    proc, tmp_root = _start_server()
    try:
        if not _free_port_check(PORT, timeout=15):
            test("live server boots", False, "timeout")
            return
        test("live server boots", True)

        # Install /dev/gpu first -- semantic calls through it.
        ok, detail = _install_gpu_plugin()
        test("install /dev/gpu plugin (live)", ok, detail)
        if not ok: return

        # Point gpu at ollama
        ollama_host = ollama_url.replace("http://", "").replace("https://", "")
        test("write /etc/gpu.conf -> ollama://{host}".format(host=ollama_host),
             _write_gpu_conf(f"ollama://{ollama_host}"))

        ok, detail = _install_plugin()
        test("install semantic plugin (live)", ok, detail)
        if not ok: return
        test("seed source (live)", _seed_source())

        s, h, body = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/plain",
                     "User-Agent": "live-smoke/1.0 (needs=plain)"},
        )
        test("live GET /shaped -> 200",
             s == 200, f"s={s} h={h} body={body[:100]!r}")
        test("live cache tag=generated on first request",
             h.get("X-Semantic-Cache") == "generated",
             f"got {h.get('X-Semantic-Cache')}")

        # Same request: should be cache hit now.
        s2, h2, body2 = _http(
            "GET", "/shaped/home/smoke", token=TOKEN,
            headers={"Accept": "text/plain",
                     "User-Agent": "live-smoke/1.0 (needs=plain)"},
        )
        test("live second request -> 200", s2 == 200)
        test("live cache tag=hit on second request",
             h2.get("X-Semantic-Cache") == "hit",
             f"got {h2.get('X-Semantic-Cache')}")
        test("live cache-hit body matches first response",
             body == body2)
    finally:
        _stop_server(proc, tmp_root)


# ====================================================================
# entrypoint
# ====================================================================

def main():
    args = sys.argv[1:]
    with_ollama = "--with-ollama" in args
    if "--with-ollama" in args: args.remove("--with-ollama")
    mode = args[0] if args else "full"

    run_layer1()
    if mode != "unit":
        run_layer2()
        run_layer2_ratecap()
    if with_ollama:
        run_layer3()

    print()
    print(f"PASS: {PASS}   FAIL: {FAIL}   SKIP: {SKIP}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
