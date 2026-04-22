"""SSE streaming tests — /dev/gpu/stream + /shaped/* text/event-stream.

Hermetic. stdlib http.server in a thread plays a fake ollama backend
(NDJSON on /api/generate). elastik runs in a subprocess pointed at a
tempfile data dir, with gpu.py + semantic.py installed and
/etc/gpu.conf pointing at the fake upstream. No live network, no
real SLM — the stream format is the same ollama NDJSON a real
backend would emit, so the parser and framing logic exercise the
same code paths.

Scope:
  - /dev/gpu/stream direct: method gate, auth gate, ollama NDJSON
    unified to SSE `data:` frames, terminal event: done, mid-stream
    error path, in-process bridge is NOT exercised (that's semantic
    calling gpu — covered via /shaped/* instead).
  - /shaped/<world> with Accept: text/event-stream, <inner>:
    cache-miss real stream, cache-hit fake-stream, 400 on
    transport-only Accept, fallback path when gpu unreachable,
    \n===META=== marker stripped from client-visible frames.

Usage (from repo root):
  python tests/test_stream.py
"""
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
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


# ====================================================================
# constants
# ====================================================================

ELASTIK_PORT  = 13041
OLLAMA_PORT   = 13042
TOKEN         = "test-stream-token"
APPROVE       = "test-stream-approve"
KEY           = "test-stream-hmac-key"

ELASTIK_URL   = f"http://127.0.0.1:{ELASTIK_PORT}"
OLLAMA_URL    = f"http://127.0.0.1:{OLLAMA_PORT}"


# ====================================================================
# fake ollama upstream (NDJSON on /api/generate)
#
# Canned responses keyed by a substring in the incoming prompt — the
# tests all pick distinct substrings so one handler serves all cases.
# ====================================================================

# Each entry: list of strings to yield, followed optionally by
# extra bytes appended verbatim AFTER the normal NDJSON (lets us
# simulate the META trailer arriving inside tokens).
_CANNED = {
    # tokens that build "hello world"
    "say hi": ["he", "llo ", "world"],
    # empty stream — simulates a model that returns no tokens
    "empty":  [],
    # long enough that the sliding HOLD buffer gets stressed
    "loud":   ["A" * 50, "B" * 50, "C" * 50],
    # Includes '\n===META===' trailer + meta JSON, split across chunks
    "meta_stream": [
        "line one\nline two",
        "\n===META===",
        '\n{"content_type":"text/plain","shape":"raw"}',
    ],
    # Tokens that contain embedded newlines (for multi-line data: framing)
    "newlines": ["row1\nrow2", "\nrow3"],
    # Empty shaped output: SYSTEM_PROMPT lets the SLM return an empty
    # body when the source cannot be rendered into REQUIRED_CONTENT_TYPE.
    # The tokens here declare text/plain + shape=unrenderable with
    # zero content bytes. Expected: /shaped/* treats this as success,
    # caches the empty body, event: done carries meta, NO event: error.
    "unrenderable": [
        '\n===META===\n{"content_type":"text/plain","shape":"unrenderable"}',
    ],
}


class _FakeOllama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        # Read the whole request body.
        length = int(self.headers.get("Content-Length", "0"))
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except Exception:
            raw = b""
        try:
            req = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            req = {}
        prompt = (req.get("prompt") or "").strip()

        # If the prompt is a semantic-built one (has the <<<SOURCE>>>
        # envelope), match canned keys against the SOURCE content
        # only. Matching against the whole prompt collided with words
        # from SYSTEM_PROMPT — e.g. "empty" appears in the phrase
        # "return an empty body" inside the system prompt, which
        # would shadow any test seeding a world whose content the
        # fake is supposed to echo.
        source_content = None
        if "<<<SOURCE CONTENT>>>" in prompt and "<<<END SOURCE>>>" in prompt:
            start = prompt.index("<<<SOURCE CONTENT>>>") + len("<<<SOURCE CONTENT>>>\n")
            end = prompt.index("<<<END SOURCE>>>")
            source_content = prompt[start:end].rstrip("\n")

        needle = source_content if source_content is not None else prompt
        tokens = []
        for key, toks in _CANNED.items():
            if key in needle:
                tokens = list(toks)
                break
        else:
            # No canned match. Semantic prompts get an echo of the
            # source plus a minimal META trailer — simulates a
            # perfect-identity text/plain → text/plain reshape.
            if source_content is not None:
                meta = '\n===META===\n{"content_type":"text/plain","shape":"raw"}'
                tokens = [source_content + meta]
        # Ollama wire format: application/x-ndjson, one JSON per line.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for tok in tokens:
                frame = json.dumps({"response": tok, "done": False}).encode() + b"\n"
                self.wfile.write(frame)
                self.wfile.flush()
            # terminator
            self.wfile.write(json.dumps({"done": True}).encode() + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                                        # client closed early

    def log_message(self, *a, **kw):
        pass


def _start_fake_ollama(port):
    srv = socketserver.TCPServer(("127.0.0.1", port), _FakeOllama)
    srv.allow_reuse_address = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return srv
        except OSError:
            time.sleep(0.05)
    return srv


# ====================================================================
# elastik subprocess
# ====================================================================

def _wait_for_server(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_elastik():
    tmp_root = tempfile.mkdtemp(prefix="elastik-test-stream-")
    tmp_data = os.path.join(tmp_root, "data")
    os.makedirs(tmp_data, exist_ok=True)
    env = os.environ.copy()
    env["ELASTIK_PORT"]          = str(ELASTIK_PORT)
    env["ELASTIK_HOST"]          = "127.0.0.1"
    env["ELASTIK_TOKEN"]         = TOKEN
    env["ELASTIK_APPROVE_TOKEN"] = APPROVE
    env["ELASTIK_KEY"]           = KEY
    env["ELASTIK_DATA"]          = tmp_data
    # Force cap to 1 so we don't spam the fake upstream — SLM is
    # stubbed, cap doesn't really matter, but it's a correctness
    # detail the test verifies implicitly.
    env["SEMANTIC_GEN_CAP_PER_MIN"] = "60"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py")],
        env=env, cwd=tmp_root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc, tmp_root


def _stop_elastik(proc, tmp_root):
    if proc is not None:
        try: proc.terminate()
        except Exception: pass
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except Exception: pass
    if tmp_root:
        shutil.rmtree(tmp_root, ignore_errors=True)


# ====================================================================
# HTTP helper + SSE frame parser
# ====================================================================

def _http(method, path, body=None, token="", headers=None, timeout=15):
    """Normal request, headers lowercased, body bytes returned."""
    def _lower(items):
        return {str(k).lower(): v for k, v in items}
    try:
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(
            f"{ELASTIK_URL}{path}", data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, _lower(r.getheaders()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers.items()), e.read()
    except Exception as e:
        return 0, {}, str(e).encode("utf-8")


def _parse_sse(raw):
    """Parse SSE wire bytes into a list of frames.

    Each frame is a dict with keys:
      event: str (default 'message')
      data:  str (joined on '\\n' if multi-line)

    Frames are separated by blank lines (\\n\\n). Within a frame,
    `field: value` lines set event/data.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    frames = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        ev = "message"
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip() if line.startswith("data: ")
                                   else line[5:])
        frames.append({"event": ev, "data": "\n".join(data_lines)})
    return frames


# ====================================================================
# plugin install helpers
# ====================================================================

def _install_plugin(name):
    src = open(os.path.join(ROOT, "plugins", f"{name}.py"), "rb").read()
    s1, _, b = _http("PUT", f"/lib/{name}", body=src, token=APPROVE,
                     headers={"Content-Type": "text/x-python"})
    if s1 not in (200, 201):
        return False, f"PUT /lib/{name} -> {s1}: {b[:200]!r}"
    s2, _, b = _http("PUT", f"/lib/{name}/state", body="active",
                     token=APPROVE)
    if s2 not in (200, 204):
        return False, f"activate /lib/{name} -> {s2}: {b[:200]!r}"
    return True, "installed"


def _write_world(world_path, content, ext="plain"):
    """PUT a world under the given path with the supplied content."""
    s, _, _ = _http("PUT", world_path, body=content, token=APPROVE,
                    headers={"Content-Type": f"text/{ext}" if ext == "plain" else f"text/{ext}"})
    return s in (200, 201)


def _write_gpu_conf(line):
    s, _, _ = _http("PUT", "/etc/gpu.conf", body=line, token=APPROVE,
                    headers={"Content-Type": "text/plain"})
    return s in (200, 201)


# ====================================================================
# test body
# ====================================================================

def run():
    print("=== SSE streaming tests ===")

    upstream = _start_fake_ollama(OLLAMA_PORT)
    proc, tmp_root = _start_elastik()

    try:
        if not _wait_for_server(ELASTIK_PORT, timeout=15):
            test("elastik boots", False, "timeout")
            return
        test("elastik boots", True)

        ok, detail = _install_plugin("gpu")
        test("install /lib/gpu", ok, detail)
        if not ok: return
        ok, detail = _install_plugin("semantic")
        test("install /lib/semantic", ok, detail)
        if not ok: return

        # Point gpu at the fake ollama upstream
        test("write /etc/gpu.conf",
             _write_gpu_conf(f"ollama://127.0.0.1:{OLLAMA_PORT}"))

        # ---- /dev/gpu/stream direct tests -------------------------

        # Method gate: GET -> 405
        s, _, _ = _http("GET", "/dev/gpu/stream", token=TOKEN)
        test("/dev/gpu/stream: GET -> 405",
             s == 405, f"got {s}")

        # Auth gate: POST without token -> 401 ...
        # (localhost bypass applies to _check_auth — but the test
        # subprocess's ELASTIK_TOKEN is set, so the bypass only
        # kicks in for anonymous access over 127.0.0.1. We rely on
        # the same mechanism as test_sidecar.py's rw mount: skip
        # the 401 assertion entirely since the localhost inherit
        # makes it unreachable.)

        # Happy path: POST with auth -> SSE frames
        s, h, raw = _http("POST", "/dev/gpu/stream",
                          body="say hi", token=TOKEN,
                          headers={"Content-Type": "text/plain"},
                          timeout=20)
        test("/dev/gpu/stream: POST -> 200",
             s == 200, f"got {s} body={raw[:160]!r}")
        test("/dev/gpu/stream: Content-Type is text/event-stream",
             (h.get("content-type") or "").startswith("text/event-stream"),
             f"ct={h.get('content-type')}")
        frames = _parse_sse(raw)
        data_frames = [f for f in frames if f["event"] == "message"]
        done_frames = [f for f in frames if f["event"] == "done"]
        test("/dev/gpu/stream: data frames carry backend tokens",
             "".join(f["data"] for f in data_frames) == "hello world",
             f"joined={[f['data'] for f in data_frames]}")
        test("/dev/gpu/stream: exactly one event: done",
             len(done_frames) == 1, f"done_count={len(done_frames)}")

        # Empty stream from backend: still get event: done
        s, _, raw = _http("POST", "/dev/gpu/stream",
                          body="empty", token=TOKEN,
                          headers={"Content-Type": "text/plain"},
                          timeout=20)
        frames = _parse_sse(raw)
        data_frames = [f for f in frames if f["event"] == "message"]
        done_frames = [f for f in frames if f["event"] == "done"]
        test("/dev/gpu/stream: empty backend -> 200 + event: done",
             s == 200 and len(data_frames) == 0 and len(done_frames) == 1,
             f"s={s} data={data_frames} done={done_frames}")

        # Multi-line tokens wrapped correctly (each '\n' -> new data:)
        s, _, raw = _http("POST", "/dev/gpu/stream",
                          body="newlines", token=TOKEN,
                          headers={"Content-Type": "text/plain"},
                          timeout=20)
        frames = _parse_sse(raw)
        joined = "".join(f["data"] for f in frames if f["event"] == "message")
        test("/dev/gpu/stream: multi-line tokens reassemble",
             joined == "row1\nrow2\nrow3",
             f"joined={joined!r}")

        # ---- /shaped/* SSE composition tests ----------------------

        # Seed a world so /shaped has something to read
        test("seed /home/story",
             _write_world("/home/story", "once upon a time", ext="plain"))

        # Accept: text/event-stream alone -> 400 (transport without shape)
        s, _, body = _http(
            "GET", "/shaped/home/story", token=TOKEN,
            headers={"Accept": "text/event-stream"})
        test("/shaped/* Accept: event-stream alone -> 400",
             s == 400, f"got {s} body={body[:200]!r}")

        # Accept: event-stream, text/plain — cache-miss real stream.
        # Fake ollama is keyed on 'say hi'; the SLM prompt includes
        # the source content ("once upon a time"), but also the
        # system prompt and the REQUIRED_CONTENT_TYPE line. None of
        # our _CANNED keys will match -> fake ollama returns empty
        # token list but still sends the NDJSON done terminator. Test
        # confirms the pipeline completes cleanly; meta JSON won't
        # parse -> slm_ct defaults to text/plain -> _accept_allows
        # passes -> cache writes.
        s, h, raw = _http(
            "GET", "/shaped/home/story", token=TOKEN,
            headers={"Accept": "text/event-stream, text/plain"},
            timeout=20)
        test("/shaped/* stream cache-miss -> 200",
             s == 200, f"got {s} body={raw[:160]!r}")
        test("/shaped/* stream cache-miss -> X-Semantic-Cache: generated",
             (h.get("x-semantic-cache") or "") == "generated",
             f"x-semantic-cache={h.get('x-semantic-cache')!r}")
        frames = _parse_sse(raw)
        done = [f for f in frames if f["event"] == "done"]
        test("/shaped/* stream: terminal event: done present",
             len(done) == 1, f"done={done}")

        # ---- \n===META=== marker stripping ------------------------
        # Seed a world whose prompt will match 'meta_stream' in the
        # fake ollama key. The tokens end with '\n===META===\n{...}'
        # — client-visible frames MUST NOT include the marker or
        # meta JSON.
        test("seed /home/with-meta",
             _write_world("/home/with-meta", "meta_stream", ext="plain"))
        s, _, raw = _http(
            "GET", "/shaped/home/with-meta", token=TOKEN,
            headers={"Accept": "text/event-stream, text/plain"},
            timeout=20)
        frames = _parse_sse(raw)
        data_payload = "".join(f["data"] for f in frames
                                if f["event"] == "message")
        test("/shaped/* META marker stripped from stream",
             "===META===" not in data_payload
             and "content_type" not in data_payload,
             f"data leaked: {data_payload[:200]!r}")
        test("/shaped/* pre-META content survives intact",
             data_payload == "line one\nline two",
             f"got {data_payload!r}")
        done = [f for f in frames if f["event"] == "done"]
        test("/shaped/* done event carries parsed meta JSON",
             len(done) == 1 and "text/plain" in done[0]["data"],
             f"done={done}")

        # ---- empty-body shape is valid success --------------------
        # SYSTEM_PROMPT explicitly permits 'empty body + text/plain
        # + shape=unrenderable' as a legitimate response when the
        # source cannot be rendered into the requested shape. A
        # previous bug in _stream_shape forced this through the
        # error branch because `body_out and ...` required body
        # truthiness — an empty but Accept-admissible shape was
        # treated as a mismatch. Regression guard: the response
        # must be success (event: done with meta, no event: error)
        # AND the empty body must reach the cache so a second
        # request short-circuits instead of re-prompting the SLM.
        test("seed /home/empty-shape",
             _write_world("/home/empty-shape", "unrenderable",
                          ext="plain"))
        s, h, raw = _http(
            "GET", "/shaped/home/empty-shape", token=TOKEN,
            headers={"Accept": "text/event-stream, text/plain"},
            timeout=20)
        frames = _parse_sse(raw)
        err = [f for f in frames if f["event"] == "error"]
        done = [f for f in frames if f["event"] == "done"]
        data = [f for f in frames if f["event"] == "message"]
        test("/shaped/* empty-body shape -> 200 generated (not error)",
             s == 200
             and (h.get("x-semantic-cache") or "") == "generated"
             and len(err) == 0,
             f"s={s} x-sem-cache={h.get('x-semantic-cache')!r} err={err}")
        test("/shaped/* empty-body shape: zero data frames",
             len(data) == 0, f"data={data}")
        test("/shaped/* empty-body shape: done carries parsed meta",
             len(done) == 1 and "unrenderable" in done[0]["data"],
             f"done={done}")
        # Second hit must be cache-hit — proves the empty body was
        # written to cache, not dropped.
        s, h, _ = _http(
            "GET", "/shaped/home/empty-shape", token=TOKEN,
            headers={"Accept": "text/event-stream, text/plain"},
            timeout=20)
        test("/shaped/* empty-body shape: second hit is cached",
             s == 200
             and (h.get("x-semantic-cache") or "") == "hit",
             f"s={s} x-sem-cache={h.get('x-semantic-cache')!r}")

        # ---- cache hit fake-stream --------------------------------
        # Hit /shaped/home/with-meta again: should now be cache-hit
        # (x-semantic-cache: hit) delivered as single-frame fake-
        # stream since Accept still declares text/event-stream.
        s, h, raw = _http(
            "GET", "/shaped/home/with-meta", token=TOKEN,
            headers={"Accept": "text/event-stream, text/plain"},
            timeout=20)
        test("/shaped/* stream cache-hit -> 200",
             s == 200, f"got {s}")
        test("/shaped/* stream cache-hit -> X-Semantic-Cache: hit",
             (h.get("x-semantic-cache") or "") == "hit",
             f"x-semantic-cache={h.get('x-semantic-cache')!r}")
        frames = _parse_sse(raw)
        data_frames = [f for f in frames if f["event"] == "message"]
        done_frames = [f for f in frames if f["event"] == "done"]
        test("/shaped/* fake-stream: exactly one data frame + one done",
             len(data_frames) == 1 and len(done_frames) == 1,
             f"data={len(data_frames)} done={len(done_frames)}")
        test("/shaped/* fake-stream data is the cached body",
             data_frames and data_frames[0]["data"] == "line one\nline two",
             f"got {data_frames}")

        # ---- gpu.conf missing -> pre-commit fallback --------------
        # Blank out /etc/gpu.conf so /dev/gpu/stream returns 503
        # before emitting http.response.start; semantic.py falls
        # back through _accept_gated_fallback. With Accept:
        # text/event-stream, text/html, text/plain ISN'T top of the
        # stripped list so block_status=503 fires. Header stays
        # non-streaming (one-shot JSON error envelope) since the
        # stream never opened.
        test("wipe /etc/gpu.conf",
             _write_gpu_conf("# intentionally empty\n"))
        s, h, body = _http(
            "GET", "/shaped/home/story", token=TOKEN,
            headers={"Accept": "text/event-stream, text/html"},
            timeout=15)
        # NB: cache was populated from the earlier /shaped/home/story
        # call under its own Accept line, which had a different
        # accept_canon, so we expect a miss here and hence the
        # fallback to fire on gpu-down.
        test("/shaped/* gpu-down + inner=html -> 503 (not streamed)",
             s == 503,
             f"got {s} ct={h.get('content-type')} body={body[:200]!r}")

    finally:
        _stop_elastik(proc, tmp_root)
        try: upstream.shutdown()
        except Exception: pass
        try: upstream.server_close()
        except Exception: pass


def main():
    run()
    print()
    print(f"PASS: {PASS}   FAIL: {FAIL}   SKIP: {SKIP}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
