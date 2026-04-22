"""sidecar Phase 1 tests — /etc/fstab structured entries + file/https adapters.

Hermetic. No reliance on live network. A threaded stdlib http.server
plays upstream for the https:// adapter. elastik server runs in a
subprocess pointed at a temp data directory (same isolation pattern
tests/test_semantic.py uses) so the test never sees the repo's real
/lib/* state.

Scope matches feat(fstab): structured entries + file/https adapter
dispatch — NOT Track B.2 (/shaped/mnt composition) or B.3 (/dev/db
guard). Those lands with their own commits and extend this file.

Usage (from repo root):

  python tests/test_sidecar.py

Matches the style of tests/test_plugins.py and tests/test_semantic.py
— homegrown PASS/FAIL counters, urllib.request for HTTP client,
subprocess for elastik, tempfile for data isolation.
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

ELASTIK_PORT = 13029
UPSTREAM_PORT = 13030
TOKEN = "test-sidecar-token"
APPROVE = "test-sidecar-approve"
KEY = "test-sidecar-hmac-key"

ELASTIK_URL = f"http://127.0.0.1:{ELASTIK_PORT}"
UPSTREAM_URL = f"http://127.0.0.1:{UPSTREAM_PORT}"


# ====================================================================
# fake upstream (stdlib http.server in a thread)
# ====================================================================

# Must match plugins/fstab.py:_MAX_FILE. Locked here so the cap-trip
# assertion is a real regression test even if the fstab constant drifts.
_FSTAB_MAX_FILE = 5 * 1024 * 1024


class _FakeUpstream(http.server.BaseHTTPRequestHandler):
    """Canned responses for the https:// adapter to proxy.

    /ping       -> 200 text/plain "pong", ETag present  (tests etag version)
    /json       -> 200 application/json, no ETag         (tests len+head version)
    /echo-auth  -> 200 echoes Authorization header        (tests bearer= opt)
    /boom       -> 500                                    (tests status passthrough)
    /big        -> 200 _FSTAB_MAX_FILE+1 bytes            (tests upstream cap)
    anything    -> 404
    """
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("ETag", '"W/pong-v1"')
            self.end_headers()
            self.wfile.write(b"pong")
        elif self.path == "/json":
            body = b'{"hello":"world","count":42}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/echo-auth":
            # returns the Authorization header it saw, for bearer= test
            got = self.headers.get("Authorization", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(got.encode("utf-8"))
        elif self.path == "/boom":
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"upstream exploded")
        elif self.path == "/big":
            # One byte over the adapter cap. The adapter should trip
            # at _MAX_FILE+1 without reading any further. Client-side
            # urlopen may not actually drain past that, and we don't
            # wait for it to — TCPServer's daemon thread will GC the
            # half-written response when the adapter closes.
            payload = b"x" * (_FSTAB_MAX_FILE + 1)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                # adapter closed early — expected
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # Accepted only so the adapter's 405 path is tested against a
        # reachable upstream (we should 405 BEFORE sending anything).
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a, **kw):
        pass  # silence


def _start_fake_upstream(port):
    srv = socketserver.TCPServer(("127.0.0.1", port), _FakeUpstream)
    srv.allow_reuse_address = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # Quick readiness poll.
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return srv
        except OSError:
            time.sleep(0.05)
    return srv


# ====================================================================
# elastik subprocess (isolated tmp data dir, same pattern as test_semantic.py)
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
    tmp_root = tempfile.mkdtemp(prefix="elastik-test-sidecar-")
    tmp_data = os.path.join(tmp_root, "data")
    os.makedirs(tmp_data, exist_ok=True)

    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(ELASTIK_PORT)
    env["ELASTIK_HOST"] = "127.0.0.1"
    env["ELASTIK_TOKEN"] = TOKEN
    env["ELASTIK_APPROVE_TOKEN"] = APPROVE
    env["ELASTIK_KEY"] = KEY
    env["ELASTIK_DATA"] = tmp_data

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
# HTTP helper
# ====================================================================

def _http(method, path, body=None, token="", headers=None):
    """Return (status, headers_lower, body). Header keys are lowercased
    so callers don't have to guess HTTP header casing — uvicorn emits
    lowercase, other stacks may emit title-case, tests shouldn't care."""
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
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, _lower(r.getheaders()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers.items()), e.read()
    except Exception as e:
        return 0, {}, str(e).encode("utf-8") if isinstance(str(e), str) else b""


def _install_plugin(name):
    """PUT plugins/<name>.py into /lib/<name> + activate."""
    plugin = os.path.join(ROOT, "plugins", f"{name}.py")
    with open(plugin, "rb") as f:
        src = f.read()
    s1, _, _ = _http("PUT", f"/lib/{name}", body=src, token=APPROVE,
                     headers={"Content-Type": "text/x-python"})
    if s1 not in (200, 201):
        return False, f"PUT /lib/{name} -> HTTP {s1}"
    s2, _, _ = _http("PUT", f"/lib/{name}/state", body="active",
                     token=APPROVE)
    if s2 not in (200, 204):
        return False, f"PUT /lib/{name}/state -> HTTP {s2}"
    return True, "installed"


def _install_fstab_plugin():
    return _install_plugin("fstab")


def _write_fstab(content):
    s, _, _ = _http("PUT", "/etc/fstab", body=content, token=APPROVE,
                    headers={"Content-Type": "text/plain"})
    return s in (200, 201)


# ====================================================================
# test body
# ====================================================================

def run():
    print("=== sidecar Phase 1 tests ===")

    # ---- set up a local dir for the file:// mount ---------------
    local_mount_root = tempfile.mkdtemp(prefix="elastik-sidecar-localfs-")
    # seed a file and a subdir so the adapter has something to read
    os.makedirs(os.path.join(local_mount_root, "sub"), exist_ok=True)
    with open(os.path.join(local_mount_root, "hello.txt"), "w",
              encoding="utf-8") as f:
        f.write("hello from disk")
    with open(os.path.join(local_mount_root, "sub", "inner.txt"), "w",
              encoding="utf-8") as f:
        f.write("nested")

    # Separate rw dir so the POST/read-back path has a real target that
    # the read-only /mnt/local cannot provide. Preserving the rw write
    # contract is the most compatibility-sensitive promise in B.1; this
    # mount locks it in.
    rw_mount_root = tempfile.mkdtemp(prefix="elastik-sidecar-localfs-rw-")

    # Seed a real SQLite file under the local mount so /dev/db can
    # open it for the file-kind positive case. Uses stdlib sqlite3
    # — same engine /dev/db itself uses, so the file format is
    # guaranteed compatible.
    import sqlite3 as _sqlite3
    _sample_db = os.path.join(local_mount_root, "sample.db")
    _c = _sqlite3.connect(_sample_db)
    _c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    _c.execute("INSERT INTO items(name) VALUES ('alpha'), ('beta')")
    _c.commit()
    _c.close()

    upstream = _start_fake_upstream(UPSTREAM_PORT)
    proc, tmp_root = _start_elastik()

    try:
        if not _wait_for_server(ELASTIK_PORT, timeout=15):
            test("elastik boots", False, "timeout")
            return
        test("elastik boots", True)

        ok, detail = _install_fstab_plugin()
        test("install fstab plugin", ok, detail)
        if not ok:
            return

        # fstab: file+ro / file+rw / http+ro / http+ro+bearer / bogus-scheme
        fstab_lines = "\n".join([
            f"{local_mount_root}  /mnt/local  ro",
            f"{rw_mount_root}  /mnt/rw  rw",
            f"http://127.0.0.1:{UPSTREAM_PORT}  /mnt/remote  ro",
            f"http://127.0.0.1:{UPSTREAM_PORT}  /mnt/authed  ro,bearer=sekret",
            "nothing://somewhere  /mnt/unk  ro",
        ]) + "\n"
        test("write /etc/fstab with 5 mounts", _write_fstab(fstab_lines))

        # ---- /mnt/ listing preserves v0.1 shape ------------------
        s, h, body = _http("GET", "/mnt/", token=TOKEN)
        test("GET /mnt/ -> 200", s == 200, f"got {s}")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = None
        test("listing is JSON with 'mounts' key",
             isinstance(data, dict) and "mounts" in data,
             f"body={body[:200]!r}")
        mount_names = [m.get("name") for m in (data or {}).get("mounts", [])]
        test("listing contains 'local' (file mount)",
             "local" in mount_names, f"names={mount_names}")
        test("listing contains 'remote' (http mount)",
             "remote" in mount_names, f"names={mount_names}")
        test("listing preserves v0.1 keys (name, path, mode)",
             all(set(m.keys()) >= {"name", "path", "mode"}
                 for m in (data or {}).get("mounts", [])),
             f"keys={[list(m.keys()) for m in (data or {}).get('mounts', [])]}")

        # ---- file:// adapter regression --------------------------
        # Directory listing: JSON shape preserved from v0.1
        s, h, body = _http("GET", "/mnt/local/", token=TOKEN)
        test("file dir listing -> 200", s == 200)
        ct = (h.get("content-type") or "").lower()
        test("file dir listing -> application/json",
             "application/json" in ct, f"ct={ct}")
        try:
            lst = json.loads(body.decode("utf-8"))
        except Exception:
            lst = None
        entry_names = [e["name"] for e in (lst or {}).get("entries", [])]
        test("file dir listing contains seeded names",
             "hello.txt" in entry_names and "sub" in entry_names,
             f"entries={entry_names}")

        # Single file read: raw bytes + inferred content-type (behaviour
        # shift from v0.1 — called out in commit message).
        s, h, body = _http("GET", "/mnt/local/hello.txt", token=TOKEN)
        test("file read -> 200", s == 200)
        test("file read body is raw bytes from disk",
             body == b"hello from disk", f"got {body!r}")
        test("file read CT inferred from extension (text/plain)",
             (h.get("content-type") or "").lower().startswith("text/plain"),
             f"ct={h.get('content-type')}")
        test("file read carries X-Mount-Version with mtime",
             (h.get("x-mount-version") or "").startswith("mtime:"),
             f"X-Mount-Version={h.get('x-mount-version')!r}")

        # Nested file still resolvable through the adapter
        s, _, body = _http("GET", "/mnt/local/sub/inner.txt", token=TOKEN)
        test("file nested read -> 200 with correct body",
             s == 200 and body == b"nested")

        # Traversal rejection. elastik enforces two guards:
        #   outer: server.py's URL validator rejects any path containing
        #          `..` with 400 "invalid path" (fires first on HTTP)
        #   inner: fstab.py's _safe_resolve raises _TraversalError -> 403
        # The outer guard is the one clients hit in practice; the inner
        # guard is belt-and-suspenders against non-HTTP callers. Accept
        # either as proof the traversal did not slip through.
        s, _, _ = _http("GET", "/mnt/local/../etc/fstab", token=TOKEN)
        test("file traversal (..) rejected",
             s in (400, 403),
             f"got {s} -- traversal NOT blocked")

        # ---- file:// rw write path (regression coverage) ---------
        # Preserves the authenticated write contract: POST with bearer
        # token -> 200 ack; content actually hits disk; read-back
        # through the same adapter returns the written bytes; ro
        # mount still refuses POST with 405.
        #
        # NOTE on the 401 branch: fstab.py's `if not _check_auth(scope)`
        # gate lives in server.py:287. Since the f98e8e7 "localhost
        # bypass" fix, any request from 127.0.0.1 returns "auth" even
        # without a bearer header — so the 401 case is unreachable from
        # a local test runner. The gate is covered by test_plugins.py's
        # non-localhost cases; here we lock in that the gate is wired
        # into the rewritten dispatch at all (ack proves auth ran).
        payload = b"written by test"

        s, _, body = _http("POST", "/mnt/rw/hello.txt", body=payload,
                           token=TOKEN,
                           headers={"Content-Type": "text/plain"})
        test("rw POST with auth -> 200",
             s == 200, f"got {s} body={body[:200]!r}")
        try:
            ack = json.loads(body.decode("utf-8"))
        except Exception:
            ack = None
        test("rw POST ack shape {ok, path, size} preserved",
             (isinstance(ack, dict)
              and ack.get("ok") is True
              and ack.get("path") == "hello.txt"
              and ack.get("size") == len(payload)),
             f"ack={ack}")

        # Bytes hit disk
        try:
            with open(os.path.join(rw_mount_root, "hello.txt"), "rb") as f:
                on_disk = f.read()
        except OSError as e:
            on_disk = f"<OSError: {e}>".encode()
        test("rw POST wrote bytes to disk byte-for-byte",
             on_disk == payload, f"disk={on_disk!r}")

        # Read-back through the same adapter
        s, _, body = _http("GET", "/mnt/rw/hello.txt", token=TOKEN)
        test("rw read-back -> 200 with same bytes",
             s == 200 and body == payload,
             f"got s={s} body={body[:80]!r}")

        # ro mount still rejects POST (405), auth or not
        s, _, _ = _http("POST", "/mnt/local/nope.txt",
                        body=b"should not land", token=TOKEN,
                        headers={"Content-Type": "text/plain"})
        test("ro mount POST -> 405 (even with auth)",
             s == 405, f"got {s}")

        # ---- https:// adapter ------------------------------------
        s, h, body = _http("GET", "/mnt/remote/ping", token=TOKEN)
        test("http adapter GET -> 200", s == 200, f"got {s}")
        test("http adapter body proxied from upstream",
             body == b"pong", f"got {body!r}")
        test("http adapter Content-Type preserved from upstream",
             (h.get("content-type") or "").startswith("text/plain"),
             f"ct={h.get('content-type')}")
        test("http adapter X-Mount-Version carries upstream ETag",
             "etag:" in (h.get("x-mount-version") or ""),
             f"X-Mount-Version={h.get('x-mount-version')!r}")

        # upstream without ETag -> fallback version token format
        s, h, body = _http("GET", "/mnt/remote/json", token=TOKEN)
        test("http adapter (no ETag) -> 200",
             s == 200 and body == b'{"hello":"world","count":42}')
        ver = h.get("x-mount-version") or ""
        test("http adapter version falls back to len=N;head=<hex>",
             ver.startswith("len=") and ";head=" in ver,
             f"ver={ver!r}")

        # bearer= opt carried through to upstream
        s, _, body = _http("GET", "/mnt/authed/echo-auth", token=TOKEN)
        test("http adapter attaches bearer= from opts",
             s == 200 and body == b"Bearer sekret",
             f"upstream saw: {body!r}")

        # Upstream 500 propagates with that status
        s, _, body = _http("GET", "/mnt/remote/boom", token=TOKEN)
        test("http adapter surfaces upstream 500",
             s == 500, f"got {s} body={body[:120]!r}")

        # Non-GET on https adapter -> 405 (before reaching upstream)
        s, _, _ = _http("POST", "/mnt/remote/ping",
                        body="junk", token=TOKEN)
        test("http adapter rejects POST -> 405",
             s == 405, f"got {s} -- https adapter accepted a write?")

        # Upstream bigger than _MAX_FILE -> 413 before the body drains
        # into memory. /mnt/* is unauthenticated (AUTH="none"), so an
        # unbounded read would let any configured remote mount proxy
        # arbitrarily large upstream bodies. Cap must be enforced.
        s, _, body = _http("GET", "/mnt/remote/big", token=TOKEN)
        test("http adapter caps upstream body at _MAX_FILE -> 413",
             s == 413, f"got {s} body={body[:200]!r}")

        # ---- dispatcher: unknown mount + unknown scheme ----------
        s, _, _ = _http("GET", "/mnt/not-a-mount/x", token=TOKEN)
        test("unknown mount -> 404", s == 404, f"got {s}")

        s, _, _ = _http("GET", "/mnt/unk/anything", token=TOKEN)
        test("unknown adapter scheme -> 501", s == 501, f"got {s}")

        # ---- /shaped/mnt/<name>/<path> composition through semantic --
        # Install semantic on top of fstab. /dev/gpu is absent in this
        # subprocess, so semantic's _call_gpu_device raises
        # _SLMUnavailable and the handler routes through
        # _accept_gated_fallback. With Accept: text/plain at top of q,
        # that branch returns 200 with the raw source bytes (tag
        # `fallback-raw` in X-Semantic-Cache). Delivery of the mount
        # bytes via /shaped/ is proof that _read_source("mnt/...")
        # reached fstab in-process, unpacked the adapter's (body, ct,
        # X-Mount-Version) triple, and threaded it into the prompt
        # path. Without B.2 this would 404 with "world not found".
        sem = os.path.join(ROOT, "plugins", "semantic.py")
        with open(sem, "rb") as f:
            sem_src = f.read()
        s1, _, _ = _http("PUT", "/lib/semantic", body=sem_src, token=APPROVE,
                         headers={"Content-Type": "text/x-python"})
        test("install semantic plugin",
             s1 in (200, 201), f"PUT /lib/semantic -> {s1}")
        s2, _, _ = _http("PUT", "/lib/semantic/state", body="active",
                         token=APPROVE)
        test("activate semantic plugin",
             s2 in (200, 204), f"PUT /lib/semantic/state -> {s2}")

        if s1 in (200, 201) and s2 in (200, 204):
            # file mount via /shaped/*
            s, h, body = _http(
                "GET", "/shaped/mnt/local/hello.txt", token=TOKEN,
                headers={"Accept": "text/plain"})
            test("/shaped/mnt/<file>/* -> 200 (fallback-raw, SLM down)",
                 s == 200, f"got {s} body={body[:120]!r}")
            test("/shaped/mnt/* reached semantic (X-Semantic-Cache set)",
                 bool(h.get("x-semantic-cache")),
                 f"headers={list(h.keys())}")
            test("/shaped/mnt/<file>/* delivers file mount bytes",
                 body == b"hello from disk", f"body={body!r}")

            # http mount via /shaped/* — proves adapter CT + body flow
            # through _read_source's mount branch, not just the file one
            s, h, body = _http(
                "GET", "/shaped/mnt/remote/ping", token=TOKEN,
                headers={"Accept": "text/plain"})
            test("/shaped/mnt/<http>/* -> 200 (fallback-raw, SLM down)",
                 s == 200, f"got {s}")
            test("/shaped/mnt/<http>/* delivers upstream bytes",
                 body == b"pong", f"body={body!r}")

            # ---- adapter-failure status propagation (P2 regression) --
            # The old _read_via_fstab flattened every non-2xx adapter
            # result into None, which handle() then turned into a 404
            # "world not found". Codex reproduced this against the
            # three endpoints below — all three used to surface as
            # 404 even though the underlying failure was 500 / 413 /
            # 501. With _MountAdapterError in place, each adapter
            # status propagates through /shaped/ unchanged.
            s, _, body = _http(
                "GET", "/shaped/mnt/remote/boom", token=TOKEN,
                headers={"Accept": "text/plain"})
            test("/shaped/mnt/* preserves upstream 500 (not 404)",
                 s == 500, f"got {s} body={body[:160]!r}")

            s, _, body = _http(
                "GET", "/shaped/mnt/remote/big", token=TOKEN,
                headers={"Accept": "text/plain"})
            test("/shaped/mnt/* preserves adapter 413 (not 404)",
                 s == 413, f"got {s} body={body[:160]!r}")

            s, _, body = _http(
                "GET", "/shaped/mnt/unk/anything", token=TOKEN,
                headers={"Accept": "text/plain"})
            test("/shaped/mnt/* preserves unknown-scheme 501 (not 404)",
                 s == 501, f"got {s} body={body[:160]!r}")

            # Unknown mount name still 404s — that IS a legitimate
            # "path does not resolve" case, not an adapter failure,
            # so the generic 404 branch (src is None) still fires.
            s, _, body = _http(
                "GET", "/shaped/mnt/not-a-mount/x", token=TOKEN,
                headers={"Accept": "text/plain"})
            test("/shaped/mnt/<unknown>/* -> 404 (unknown mount)",
                 s == 404, f"got {s} body={body[:160]!r}")

        # ---- /dev/db guard against non-file mounts ---------------
        # fstab now mounts file + http(s) + bogus-scheme sources. /dev/db
        # can only open local SQLite files; it MUST distinguish "mount
        # missing" (404) from "mount exists but wrong kind for SQL" (400)
        # so operators can diagnose instead of hitting a cryptic sqlite
        # error. Pre-Track-B.3 code collapsed every failure into 403.
        ok, detail = _install_plugin("db")
        test("install db plugin", ok, detail)
        if ok:
            # Positive: file-kind mount, real .db → 200 + rows
            s, h, body = _http(
                "POST", "/dev/db?file=local/sample.db",
                body="SELECT name FROM items ORDER BY id",
                token=TOKEN,
                headers={"Content-Type": "text/plain",
                         "Accept": "application/json"})
            test("/dev/db?file=local/sample.db -> 200",
                 s == 200, f"got {s} body={body[:200]!r}")
            try:
                rows = json.loads(body.decode("utf-8"))
            except Exception:
                rows = None
            test("/dev/db positive: rows come back",
                 (isinstance(rows, list)
                  and [r.get("name") for r in rows] == ["alpha", "beta"]),
                 f"rows={rows}")

            # Unknown mount name → 404 (mount doesn't exist in fstab).
            s, _, body = _http(
                "POST", "/dev/db?file=not-a-mount/x.db",
                body="SELECT 1", token=TOKEN,
                headers={"Content-Type": "text/plain"})
            test("/dev/db unknown mount -> 404",
                 s == 404, f"got {s} body={body[:200]!r}")

            # http(s) mount → 400 (wrong kind). This is the pre-Track-
            # B.3 regression: previously collapsed to 403 or would try
            # to sqlite3.connect an http:// path and fail with a
            # cryptic sqlite error. New contract: clean 400 naming
            # the kind so the operator knows to switch to /mnt/<name>.
            s, _, body = _http(
                "POST", "/dev/db?file=remote/anything",
                body="SELECT 1", token=TOKEN,
                headers={"Content-Type": "text/plain"})
            test("/dev/db http mount -> 400 (wrong kind)",
                 s == 400, f"got {s} body={body[:200]!r}")
            test("/dev/db wrong-kind error names the scheme",
                 b"http" in body,
                 f"body={body[:200]!r}")

            # Bogus scheme in fstab → also 400 wrong_kind. Symmetric
            # behaviour: any non-file kind is a 400, regardless of
            # whether the adapter is actually registered.
            s, _, body = _http(
                "POST", "/dev/db?file=unk/x.db",
                body="SELECT 1", token=TOKEN,
                headers={"Content-Type": "text/plain"})
            test("/dev/db non-file scheme -> 400 (wrong kind)",
                 s == 400, f"got {s} body={body[:200]!r}")

            # File-kind mount, but the path under it doesn't exist → 404
            # (same status as unknown-mount but different meaning; the
            # 'file not found' message distinguishes).
            s, _, body = _http(
                "POST", "/dev/db?file=local/does-not-exist.db",
                body="SELECT 1", token=TOKEN,
                headers={"Content-Type": "text/plain"})
            test("/dev/db file-kind + missing path -> 404",
                 s == 404, f"got {s} body={body[:200]!r}")
    finally:
        _stop_elastik(proc, tmp_root)
        try: upstream.shutdown()
        except Exception: pass
        try: upstream.server_close()
        except Exception: pass
        shutil.rmtree(local_mount_root, ignore_errors=True)
        shutil.rmtree(rw_mount_root, ignore_errors=True)


def main():
    run()
    print()
    print(f"PASS: {PASS}   FAIL: {FAIL}   SKIP: {SKIP}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
