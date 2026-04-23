"""Plugin system integration tests.

  Layer 1: CGI protocol (direct exec, no server)
  Layer 2: Python HTTP integration (server.py)

Usage:
  python tests/test_plugins.py          # all layers
  python tests/test_plugins.py cgi      # layer 1 only
  python tests/test_plugins.py python   # layer 1 + 2
"""
import json, os, socket, subprocess, sys, time, urllib.request, urllib.error, signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PASS = 0
FAIL = 0
SKIP = 0

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  -- {detail}")


def skip(name, reason=""):
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}  ({reason})")


def run_plugin(path, *args, stdin_data=None):
    """Run a plugin executable, return (stdout, stderr, returncode)."""
    cmd = [sys.executable, "-u", path] if path.endswith(".py") else [path]
    cmd.extend(args)
    p = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True, timeout=10)
    return p.stdout, p.stderr, p.returncode


def http_get(port, path, timeout=10):
    """GET request, return (status, body_str)."""
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def http_method(port, path, method="GET", body=None, token="", basic_auth="", headers=None):
    """Arbitrary HTTP method with optional Bearer auth or Basic Auth."""
    try:
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if basic_auth:
            import base64
            req.add_header("Authorization", "Basic " + base64.b64encode(f":{basic_auth}".encode()).decode())
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def http_post(port, path, body="", token="", approve="", headers=None):
    """POST request, return (status, body_str)."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body.encode(), method="POST"
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if approve:
            req.add_header("Authorization", f"Bearer {approve}")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        r = urllib.request.urlopen(req, timeout=120)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def wait_for_server(port, timeout=10):
    """Wait until server responds on port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/proc/worlds", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


# ── Layer 1: CGI Protocol ───────────────────────────────────────────

def test_cgi():
    print("\n=== Layer 1: CGI Protocol (direct exec) ===")

    plugins = []
    for d in ["plugins", os.path.join("plugins", "available")]:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".py") and not f.startswith("_"):
                plugins.append((d, f))

    for d, f in plugins:
        path = os.path.join(d, f)
        name = f[:-3]
        print(f"\n  --- {name} ({d}/{f}) ---")

        # Test --routes
        try:
            out, err, rc = run_plugin(path, "--routes")
            if rc != 0:
                skip(f"{name}: CGI tests", "no --routes support (Python-only plugin)")
                continue
            out = out.strip()
            try:
                routes = json.loads(out)
            except json.JSONDecodeError:
                # Empty output or non-JSON = not a CGI plugin
                if not out:
                    skip(f"{name}: CGI tests", "no CGI entry point (Python-only plugin)")
                else:
                    test(f"{name}: --routes returns JSON array", False, f"got: {out[:80]}")
                continue

            test(f"{name}: --routes returns JSON array",
                 isinstance(routes, list) and len(routes) > 0,
                 f"got: {out[:80]}")
            test(f"{name}: routes are strings starting with /",
                 all(isinstance(r, str) and r.startswith("/") for r in routes),
                 f"routes: {routes}")

            # Test stdin/stdout for each route
            for route in routes:
                req = json.dumps({"path": route, "method": "POST", "body": "test input", "query": ""})
                out2, err2, rc2 = run_plugin(path, stdin_data=req + "\n")
                test(f"{name}: {route} exits 0", rc2 == 0, f"rc={rc2} stderr={err2[:100]}")
                if rc2 == 0:
                    out2 = out2.strip()
                    try:
                        resp = json.loads(out2)
                        test(f"{name}: {route} returns valid JSON",
                             isinstance(resp, dict), f"got: {out2[:80]}")
                        test(f"{name}: {route} has status field",
                             "status" in resp and isinstance(resp["status"], int),
                             f"resp: {resp}")
                        test(f"{name}: {route} has body field",
                             "body" in resp and isinstance(resp["body"], str),
                             f"resp keys: {list(resp.keys())}")
                    except json.JSONDecodeError:
                        test(f"{name}: {route} returns valid JSON", False, f"got: {out2[:80]}")
        except subprocess.TimeoutExpired:
            test(f"{name}: --routes completes", False, "timeout")
        except Exception as e:
            test(f"{name}: --routes runs", False, str(e))

    # Echo edge-case tests (formerly via devtools.py) retired in v4.5.0
    # microkernel cut — devtools was removed; any future CGI-mode coverage
    # for /echo has to re-land through /lib/* once a replacement ships.

    # ── Adversarial CGI tests ──
    print(f"\n  --- adversarial CGI tests ---")

    # Stateless proof: counter always returns "1"
    stateless_path = os.path.join("tests", "adversarial", "stateless.py")
    if os.path.exists(stateless_path):
        req = json.dumps({"path": "/stateless", "method": "GET", "body": "", "query": ""})
        for i in range(3):
            out, _, rc = run_plugin(stateless_path, stdin_data=req + "\n")
            if rc == 0:
                resp = json.loads(out.strip())
                test(f"stateless: call {i+1} always returns '1'",
                     resp["body"] == "1", f"got: {resp['body']}")

    # Self-reader: can read its own source
    selfreader_path = os.path.join("tests", "adversarial", "selfreader.py")
    if os.path.exists(selfreader_path):
        req = json.dumps({"path": "/selfreader", "method": "GET", "body": "", "query": ""})
        out, _, rc = run_plugin(selfreader_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("selfreader: returns own source",
                 "selfreader" in resp["body"] and "__file__" in resp["body"],
                 f"body len={len(resp['body'])}")

    # Truncated: process dies mid-output -> non-zero exit
    truncated_path = os.path.join("tests", "adversarial", "truncated.py")
    if os.path.exists(truncated_path):
        req = json.dumps({"path": "/truncated", "method": "GET", "body": "", "query": ""})
        out, _, rc = run_plugin(truncated_path, stdin_data=req + "\n")
        test("truncated: non-zero exit code", rc != 0, f"rc={rc}")
        if out.strip():
            # Whatever partial output exists should NOT be valid JSON
            try:
                json.loads(out.strip())
                test("truncated: output is NOT valid JSON", False, "parsed successfully!")
            except json.JSONDecodeError:
                test("truncated: output is NOT valid JSON", True)

    # gpu.py has no Go CGI mode — edge cases covered by Python HTTP tests below.


# ── Layer 2: Python HTTP Integration ────────────────────────────────

def test_python():
    print("\n=== Layer 2: Python HTTP Integration ===")

    # v4.5.0 microkernel cut: no more disk plugin preinstall. The disk
    # loader (load_plugins / _verify_plugin / plugins.lock) is gone; /lib/*
    # is the only loader. sse + dav are now inline core routes, public_gate
    # is inlined into server.py, and the remaining Tier 1 candidates were
    # removed from plugins/available/ (see logs/plugins-backup/ for source).
    # Legacy per-plugin test helpers were retired with the cut — see
    # commit fix(tests): retire legacy Tier 1 plugin test helpers.
    py_port = 13007
    py_token = "test-py-token"
    py_approve = "test-py-approve"
    py_key = "test-audit-hmac-key"   # known to audit tests so HMAC chain can be verified end-to-end
    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(py_port)
    env["ELASTIK_HOST"] = "127.0.0.1"
    env["ELASTIK_TOKEN"] = py_token
    env["ELASTIK_APPROVE_TOKEN"] = py_approve
    env["ELASTIK_KEY"] = py_key
    # Let public_gate tests impersonate a non-localhost client by sending
    # X-Forwarded-For (regression for the cloudflared-tunnel white-screen
    # bug — app shell resources fetched anonymously by the browser).
    # Trust both v4 AND v6 loopback as the "proxy" — CI runners (notably
    # ubuntu + newer macOS) connect urllib→uvicorn over ::1, and scope
    # ["client"][0] shows up as "::1"; 127.0.0.0/8 alone doesn't cover it
    # and auth_gate then treats the request as local and bypasses. The
    # cookie-free public gate has only Authorization to lean on, so the
    # test rig must admit both loopback families.
    env["ELASTIK_TRUST_PROXY_HEADER"] = "x-forwarded-for"
    env["ELASTIK_TRUST_PROXY_FROM"] = "127.0.0.0/8,::1/128"
    proc = subprocess.Popen(
        [sys.executable, "server.py"], env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    try:
        if not wait_for_server(py_port):
            test("Python server starts", False, "timeout waiting for server")
            return
        test("Python server starts", True)

        _run_auth_tests(py_port, "python", py_token, py_approve)
        _run_blob_ext_tests(py_port, "python", py_token, py_approve)
        _run_proc_tests(py_port, "python")
        _run_public_gate_shell_tests(py_port, "python", py_token, py_approve)
        _run_uint16_redteam_tests(py_port, "python", py_token, py_approve)
        _run_meta_headers_tests(py_port, "python", py_token, py_approve)
        _run_audit_binding_tests(py_port, "python", py_token, py_approve)
        _run_head_tests(py_port, "python", py_token, py_approve)
        _run_lib_tests(py_port, "python", py_token, py_approve)
        # _run_lib_boot_collision_test retired in v4.5.0 microkernel cut.
        # The scenario (plugins/<name>.py colliding with /lib/<name>) is
        # impossible now that the disk loader is removed; _find_lib_disk_collisions
        # and its boot-refuse logic are also gone.
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_proc_tests(port, label):
    """Core /proc/ pseudo-filesystem tests — salvaged from the
    retired _run_http_tests helper. Covers the Linux-/proc-analogue
    listing surface that ships from server.py core, independent of
    any plugin being loaded."""
    # GET unknown path -> serves index.html (200).
    # Unknown GET paths are world entry points.
    st, body = http_get(port, "/proc/worlds")
    test(f"{label}: GET /proc/worlds -> 200", st == 200, f"status={st}")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label}: /proc/worlds returns array", isinstance(d, list))
        except json.JSONDecodeError:
            test(f"{label}: /proc/worlds returns JSON", False)

    # /proc/ — ls the pseudo-filesystem. Like `ls /proc` on Linux.
    st, body = http_get(port, "/proc/")
    test(f"{label}: GET /proc/ -> 200", st == 200, f"status={st}")
    for n in ("status", "uptime", "version", "worlds"):
        test(f"{label}: /proc/ lists {n}", n in body, f"body={body[:120]}")
    st, body = http_method(port, "/proc/", method="GET",
                           headers={"Accept": "application/json"})
    try:
        d = json.loads(body)
        names = {e.get("name") for e in d if isinstance(e, dict)}
        test(f"{label}: /proc/ JSON lists all four entries",
             names == {"status", "uptime", "version", "worlds"},
             f"names={names}")
    except json.JSONDecodeError:
        test(f"{label}: /proc/ JSON parses", False, f"body={body[:80]}")


def _run_auth_tests(port, label, token, approve):
    """Auth enforcement tests — server.py core write gate."""

    from urllib.parse import quote

    # GET always open
    st, _ = http_get(port, "/proc/worlds")
    test(f"{label} auth: GET /proc/worlds open", st == 200, f"status={st}")

    # Write without token -> 403
    st, _ = http_method(port, "/home/authtest", method="PUT", body="x")
    test(f"{label} auth: write no token -> 403", st == 403, f"status={st}")

    # Write with wrong token -> 403
    st, _ = http_method(port, "/home/authtest", method="PUT", body="x", token="wrong-token")
    test(f"{label} auth: write wrong token -> 403", st == 403, f"status={st}")

    # Write with correct token -> 200
    st, _ = http_method(port, "/home/authtest", method="PUT", body="hello", token=token)
    test(f"{label} auth: write correct token -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/home/authtest")
    test(f"{label} auth: read -> data persisted", st == 200 and "hello" in body,
         f"status={st} body={body[:60]}")

    # ── Core DELETE on /home/* (not via /dav) ──
    # Regression guard: _FHS must be module-level, else DELETE /home/... raises
    # UnboundLocalError before reaching auth check.
    http_method(port, "/home/del-core-test", method="PUT", body="x", token=token)
    st, _ = http_method(port, "/home/del-core-test", method="DELETE", token=token)
    test(f"{label} auth: DELETE /home/* -> 200", st == 200, f"status={st}")
    st, _ = http_get(port, "/home/del-core-test")
    test(f"{label} auth: DELETE removed world", st == 404, f"status={st}")

    # ── /etc/* worlds require approve ──

    st, _ = http_method(port, "/etc/test", method="PUT", body="data", token=token)
    test(f"{label} auth: etc/*/write auth-only -> 403", st == 403, f"status={st}")

    st, _ = http_method(port, "/etc/test", method="PUT", body="data", token="wrong")
    test(f"{label} auth: etc/*/write wrong approve -> 403", st == 403, f"status={st}")

    st, _ = http_method(port, "/etc/test", method="PUT", body="test-data", token=approve)
    test(f"{label} auth: etc/*/write approve -> pass", st in (200, 201), f"status={st}")

    st, body = http_get(port, "/etc/test")
    test(f"{label} auth: etc/*/read -> data persisted",
         st == 200 and "test-data" in body, f"status={st} body={body[:60]}")

    # /etc/shadow is chmod 000 — only approve can read
    st, _ = http_method(port, "/etc/shadow", method="PUT", body="alice:deadbeef", token=approve)
    st, _ = http_get(port, "/etc/shadow")
    test(f"{label} auth: etc/shadow/read no auth -> 403", st == 403, f"status={st}")
    st, body = http_method(port, "/etc/shadow", basic_auth=approve)
    test(f"{label} auth: etc/shadow/read approve -> 200",
         st == 200 and "deadbeef" in body, f"status={st} body={body[:60]}")

    # ── CSRF gate: sync/result/clear reject cross-origin ──

    encoded_prefix = quote("/home/café", safe="/")
    st, body = http_post(port, f"/auth/mint?prefix={encoded_prefix}&ttl=600&mode=rw", approve=approve)
    if st == 200:
        try:
            cap_doc = json.loads(body)
            cap = cap_doc["token"]
            test(f"{label} auth: unicode cap mint canonicalizes prefix",
                 cap_doc.get("prefix") == "/home/café",
                 f"prefix={cap_doc.get('prefix')!r}")
            st, _ = http_method(port, "/home/caf%C3%A9", method="PUT", body="bonjour", token=cap)
            test(f"{label} auth: unicode cap write in-scope -> 200",
                 st in (200, 201), f"status={st}")
            st, body = http_get(port, "/home/caf%C3%A9")
            test(f"{label} auth: unicode cap target persisted",
                 st == 200 and "bonjour" in body, f"status={st} body={body[:60]}")
            st, _ = http_method(port, "/home/other-unicode-cap", method="PUT", body="pwnd", token=cap)
            test(f"{label} auth: unicode cap out-of-scope -> 403", st == 403, f"status={st}")
        except (KeyError, json.JSONDecodeError) as e:
            test(f"{label} auth: unicode cap mint parseable", False, str(e))
    else:
        test(f"{label} auth: unicode cap mint -> 200", False, f"status={st} body={body[:80]}")

    st, _ = http_method(port, "/home/authtest/sync", method="POST", body="x",
                        headers={"Origin": "http://evil.com"})
    test(f"{label} csrf: sync cross-origin -> 403", st == 403, f"status={st}")

    st, _ = http_method(port, "/home/authtest/result", method="POST", body="x",
                        headers={"Origin": "http://evil.com"})
    test(f"{label} csrf: result cross-origin -> 403", st == 403, f"status={st}")

    st, _ = http_method(port, "/home/authtest/sync", method="POST", body="same-origin",
                        headers={"Origin": "http://localhost:13007"})
    test(f"{label} csrf: sync same-origin -> 200", st == 200, f"status={st}")

    # No Origin + no auth = blocked. Closes the sync bypass (curl-as-browser).
    # Same-origin iframe and authed clients still work; unauth-local-curl needs a token.
    st, _ = http_method(port, "/home/authtest/sync", method="POST", body="no-origin")
    test(f"{label} csrf: sync no origin no auth -> 403", st == 403, f"status={st}")

    # GET to mutation actions -> 405 (blocks <img src> CSRF)
    st, _ = http_get(port, "/home/authtest/sync")
    test(f"{label} csrf: GET /sync -> 405", st == 405, f"status={st}")


def _run_blob_ext_tests(port, label, token, approve):
    """Tests for BLOB storage, ext column, 304, /raw, fake directories."""
    import http.client as _hc

    def _raw_bytes(path, headers=None):
        c = _hc.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        st = r.status
        c.close()
        return st, hdrs, body

    # ── 304 version comparison ──
    http_method(port, "/home/work", method="PUT", body="hello", token=token)  # ensure world exists
    st, body = http_get(port, "/home/work")
    test(f"{label} blob: GET /read -> 200", st == 200, f"status={st}")
    v = json.loads(body).get("version", -1)

    st, _ = http_method(port, f"/home/work?v={v}")
    test(f"{label} blob: GET /read?v=current -> 304", st == 304, f"status={st}")

    st, _ = http_method(port, "/home/work?v=0")
    test(f"{label} blob: GET /read?v=0 -> 200", st == 200, f"status={st}")

    # ── ext column ──
    st, body = http_method(port, "/home/ext-blob-test?ext=css", method="PUT", body="body{color:red}", token=token)
    test(f"{label} blob: write ?ext=css -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/home/ext-blob-test")
    d = json.loads(body)
    test(f"{label} blob: read ext=css", d.get("ext") == "css", f"ext={d.get('ext')}")
    test(f"{label} blob: read content intact", "color:red" in d.get("stage_html", ""), f"body={d.get('stage_html','')[:30]}")

    # ── /raw route ──
    st, body = http_get(port, "/home/ext-blob-test?raw")
    test(f"{label} blob: /raw -> 200", st == 200, f"status={st}")
    test(f"{label} blob: /raw content", "color:red" in body, f"body={body[:30]}")

    # ── Binary write via PUT ?ext=png ──
    png_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")
    png_bytes = open(png_path, "rb").read()
    st, _ = http_method(port, "/home/blob-bin-test?ext=png", method="PUT", body=png_bytes, token=token)
    test(f"{label} blob: binary write -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/home/blob-bin-test")
    d = json.loads(body)
    test(f"{label} blob: binary read ext=png", d.get("ext") == "png", f"ext={d.get('ext')}")
    test(f"{label} blob: binary read stage_html empty", d.get("stage_html") == "", f"stage={d.get('stage_html','?')[:20]}")

    # ── Binary append via POST ?ext=png ──
    split = len(png_bytes) // 2
    first = png_bytes[:split]
    second = png_bytes[split:]
    st, _ = http_method(port, "/home/blob-bin-append?ext=png", method="PUT", body=first, token=token)
    test(f"{label} blob: binary append seed PUT -> 200", st == 200, f"status={st}")
    st, _ = http_method(port, "/home/blob-bin-append?ext=png", method="POST", body=second, token=token)
    test(f"{label} blob: binary append POST -> 200", st == 200, f"status={st}")
    st, hdrs, body_raw = _raw_bytes("/home/blob-bin-append?raw")
    test(f"{label} blob: binary append raw GET -> 200", st == 200, f"status={st}")
    test(f"{label} blob: binary append raw CT=png",
         (hdrs.get("content-type") or "").startswith("image/png"),
         f"ct={hdrs.get('content-type')}")
    test(f"{label} blob: binary append body preserved",
         body_raw == png_bytes, f"len={len(body_raw)} expected={len(png_bytes)}")

    # ── Fake directories ──
    st, _ = http_method(port, "/home/fakedir/child1?ext=txt", method="PUT", body="hello child1", token=token)
    test(f"{label} blob: write fakedir/child1 -> 200", st == 200, f"status={st}")

    st, _ = http_method(port, "/home/fakedir/child2?ext=md", method="PUT", body="# child2", token=token)
    test(f"{label} blob: write fakedir/child2 -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/proc/worlds")
    names = [s["name"] for s in json.loads(body)]
    test(f"{label} blob: /proc/worlds has fakedir/child1", "fakedir/child1" in names, f"names={[n for n in names if 'fakedir' in n]}")

    st, body = http_get(port, "/home/fakedir/child1")
    d = json.loads(body)
    test(f"{label} blob: fakedir/child1 ext=txt", d.get("ext") == "txt", f"ext={d.get('ext')}")
    test(f"{label} blob: fakedir/child1 content", "hello child1" in d.get("stage_html", ""), f"body={d.get('stage_html','')[:20]}")
    st, body = http_get(port, "/home/fakedir")
    test(f"{label} blob: pure-dir API fallback -> 200", st == 200, f"status={st}")
    test(f"{label} blob: pure-dir API fallback lists children",
         "child1" in body and "child2" in body, f"body={body[:80]}")
    st, body = http_get(port, "/home/fakedir?raw")
    test(f"{label} blob: pure-dir ?raw -> 404", st == 404, f"status={st}")
    test(f"{label} blob: pure-dir ?raw explains object/collection split",
         "raw requires object" in body and "/home/fakedir/" in body,
         f"body={body[:120]}")

    # ── DAV ext mapping ──
    st, _ = http_method(port, "/dav/home/ext-blob-test.css", basic_auth=approve)
    test(f"{label} blob: DAV GET .css", st == 200, f"status={st}")

    # ── DAV virtual directories ──
    st, body = http_method(port, "/dav/home/fakedir/", method="PROPFIND", basic_auth=approve, headers={"Depth": "1"})
    test(f"{label} blob: DAV PROPFIND fakedir/ -> 207", st == 207, f"status={st}")
    test(f"{label} blob: DAV fakedir/ has children", "child1" in body and "child2" in body, f"body={body[:100]}")

    # ── DAV binary PUT round-trip ──
    # Plan C: identity on writes. DAV PUT /dav/home/foo.png creates world
    # 'foo.png' (dots are just bytes in the name). The URL-ext fallback in
    # dav.py's PUT handler derives ext=png from the URL's last-segment
    # suffix since urllib sends no Content-Type.
    st, _ = http_method(port, "/dav/home/dav-bin-test.png", method="PUT", body=png_bytes, basic_auth=approve)
    test(f"{label} blob: DAV PUT binary -> 201", st == 201, f"status={st}")

    st, body = http_get(port, "/home/dav-bin-test.png")
    d = json.loads(body)
    test(f"{label} blob: DAV binary ext=png", d.get("ext") == "png", f"ext={d.get('ext')}")
    test(f"{label} blob: DAV binary stage empty (binary)", d.get("stage_html") == "", f"stage={d.get('stage_html','?')[:20]}")

    # ── Unicode world names ──
    # Unicode world names — skip on CI (Windows runner codepage issues)
    if os.environ.get("CI"):
        skip(f"{label} unicode: (skipped on CI)", "Windows codepage")
    else:
        unicode_names = [
            ("\u8863\u67dc", "txt", "wardrobe"),           # 衣柜 (Chinese)
            ("\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8", "md", "# Japanese test"),  # 日本語テスト
            ("\ud55c\uad6d\uc5b4", "css", "body{color:red}"),  # 한국어 (Korean)
            ("\u041f\u0440\u0438\u0432\u0435\u0442", "txt", "hello russian"),  # Привет (Cyrillic)
            ("\u0645\u0631\u062d\u0628\u0627", "txt", "hello arabic"),  # مرحبا (Arabic)
            ("\u82b1\u6912/\u7c89", "txt", "Sichuan pepper"),  # 花椒/粉 (Chinese with /)
        ]
        for uname, uext, ucontent in unicode_names:
            from urllib.parse import quote
            encoded = quote(uname, safe="/")
            st, _ = http_method(port, f"/home/{encoded}?ext={uext}", method="PUT", body=ucontent, token=token)
            test(f"{label} unicode: write {uname} -> 200", st == 200, f"status={st}")

            st, body = http_get(port, f"/home/{encoded}")
            d = json.loads(body)
            test(f"{label} unicode: read {uname} ext={uext}", d.get("ext") == uext, f"ext={d.get('ext')}")

        # Check /proc/worlds has Unicode names (normalize for macOS HFS+ NFD)
        import unicodedata
        st, body = http_get(port, "/proc/worlds")
        names = [unicodedata.normalize("NFC", s["name"]) for s in json.loads(body)]
        test(f"{label} unicode: /proc/worlds has Chinese", any("\u8863" in n for n in names), f"names={[n for n in names if ord(n[0])>127][:5]}")
        test(f"{label} unicode: /proc/worlds has Korean", any("\ud55c" in n for n in names), "")
        test(f"{label} unicode: /proc/worlds has fake dir", any("\u82b1\u6912/" in n for n in names), "")


def _run_uint16_redteam_tests(port, label, token, approve):
    """Red team against oversized URLs across both HTTP backends.

    Post-657f850 elastik forces `http="h11"` in uvicorn, so these 70 KB
    requests usually hit h11's 16 KiB `max_incomplete_event_size` and
    return 400 from uvicorn itself — that still counts as "blocked".
    In the hypothetical where a future change flips the default back to
    httptools, the same attacks hit `parse_url()`'s uint16 wrap, and we
    want architectural resistance (prefix-match routing, byte-level `..`
    guard, header-based auth, consistent cap-scope path) to block them
    too. So each expected set accepts {400 (h11), 401, 403 (arch), 414
    (MAX_URL cap)} — any successful attack returns 200 = BREACH.
    """
    try:
        import httptools  # noqa: F401 — presence check only
    except ImportError:
        skip(f"{label} uint16: httptools not installed",
             "uvicorn fallback is h11, no uint16 wrap possible")
        return

    def _raw(method, target, tok="", body=b"", timeout=15):
        hdr = f"{method} {target} HTTP/1.1\r\nHost: x\r\n"
        if tok: hdr += f"Authorization: Bearer {tok}\r\n"
        hdr += "Connection: close\r\n"
        if body: hdr += f"Content-Length: {len(body)}\r\n"
        req_bytes = hdr.encode() + b"\r\n" + body
        s = socket.socket()
        s.settimeout(timeout)
        s.connect(("127.0.0.1", port))
        s.sendall(req_bytes)
        resp = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk: break
                resp += chunk
        except socket.timeout:
            pass
        finally:
            s.close()
        first = resp.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        parts = first.split(" ", 2)
        st = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        body_start = resp.find(b"\r\n\r\n") + 4 if b"\r\n\r\n" in resp else len(resp)
        return st, resp[body_start:]

    PAD = "a" * 70000      # wraps to 4464
    EXACT = "a" * 65535    # wraps to 0

    # /admin/load approve-gate: prefix preserved under wrap, handler self-checks
    st, _ = _raw("POST", f"/admin/load/{PAD}", body=b"name=devtools")
    test(f"{label} uint16: /admin/load no-auth (70KB URL) blocked",
         st in (400, 401, 403, 414), f"status={st}")
    st, _ = _raw("POST", f"/admin/load/{PAD}", token, body=b"name=devtools")
    test(f"{label} uint16: /admin/load T2 (70KB URL) blocked",
         st in (400, 401, 403, 414), f"status={st}")

    # /etc/* T3-write gate survives
    st, _ = _raw("PUT", f"/etc/rt-canary/{PAD}", token, body=b"pwnd")
    test(f"{label} uint16: /etc/* T2 write (70KB URL) blocked",
         st in (400, 403, 414), f"status={st}")

    # '..' entry guard fires on truncated prefix
    st, _ = _raw("GET", f"/foo/../{PAD}")
    test(f"{label} uint16: '..' in truncated head (70KB URL) blocked",
         st in (400, 414), f"status={st}")

    # wrap-to-empty: exact-65535-a path → scope['path'] empty → routes to /
    st, _ = _raw("GET", f"/{EXACT}")
    test(f"{label} uint16: wrap-to-empty (exact 65535) no privileged surface",
         st in (200, 400, 414), f"status={st}")

    # 9 KB URL is in the cap window (>8192, <65535) — MAX_URL should fire 414.
    st, _ = _raw("GET", "/" + "a" * 9000)
    test(f"{label} uint16: 9 KB URL caps at MAX_URL -> 414", st == 414, f"status={st}")

    # cap token scope: /home/scratch cap must not reach /home/other under wrap
    st, body = _raw("POST", "/auth/mint?prefix=/home/scratch&ttl=600&mode=rw", approve)
    if st == 200:
        try:
            cap = json.loads(body)["token"]
            st, _ = _raw("PUT", f"/home/other/{PAD}", cap, body=b"pwnd")
            test(f"{label} uint16: cap /home/scratch → /home/other (70KB URL) blocked",
                 st in (400, 401, 403, 404, 414), f"status={st}")
        except (KeyError, json.JSONDecodeError) as e:
            test(f"{label} uint16: cap mint for scope test", False, str(e))
    else:
        skip(f"{label} uint16: cap scope test", f"mint returned {st}")

    # /auth/mint mode=r must not escalate to rw under query truncation
    qs = f"prefix=/home/scratch&ttl=60&mode=r&padding={PAD}"
    st, body = _raw("POST", f"/auth/mint?{qs}", approve)
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} uint16: mint mode=r preserved under query wrap",
                 d.get("mode") == "r", f"got mode={d.get('mode')!r}")
        except json.JSONDecodeError:
            test(f"{label} uint16: mint response parseable", False, body[:80])
    else:
        test(f"{label} uint16: mint with truncated query -> blocked",
             st in (400, 414), f"status={st}")


def _run_public_gate_shell_tests(port, label, token, approve):
    """Regression for the cloudflared-tunnel white-screen bug (2026-04-20).

    When elastik is behind a reverse proxy (Cloudflare tunnel, nginx,
    etc.), public_gate auth-gates requests from non-localhost clients.
    But browsers fetch app shell resources (manifest.json, sw.js,
    favicon, opensearch) *anonymously* — no cookie, no Basic auth —
    by HTML spec. Gating them returns 401 on every page load, which
    breaks PWA install, Service Worker registration, and leaves the
    iframe stuck at about:blank. public_gate must let these specific
    paths through even without auth.

    Why this isn't caught by the default test run: test subprocess
    runs against 127.0.0.1 → public_gate passes through for free,
    regardless of auth. The env now sets ELASTIK_TRUST_PROXY_HEADER +
    ELASTIK_TRUST_PROXY_FROM so tests can send X-Forwarded-For to
    impersonate a non-localhost client and actually exercise the gate.
    """
    # Impersonate a non-localhost client. 127.0.0.0/8 is in TRUST_FROM
    # (set in test_python env), so public_gate honours the X-F-F header.
    non_local = {"X-Forwarded-For": "203.0.113.1"}

    # Sanity: non-localhost without auth → content is gated
    st, body = http_method(port, "/home/some-world", method="GET",
                           headers=non_local)
    test(f"{label} public_gate: /home/* non-local no auth -> 401",
         st == 401, f"status={st}")
    test(f"{label} public_gate: 401 body is pastebin disguise",
         "pastebin" in body, f"body={body[:40]}")

    # App shell resources must pass through even without auth
    for res in ("/manifest.json", "/sw.js", "/opensearch.xml"):
        st, _ = http_method(port, res, method="GET", headers=non_local)
        test(f"{label} public_gate: {res} non-local no auth -> 200",
             st == 200, f"status={st}")

    # /icon.png and /icon-192.png only exist if the icon file ships.
    # They're on the whitelist anyway; verify they don't hard-401.
    for res in ("/favicon.ico", "/icon.png", "/icon-192.png"):
        st, _ = http_method(port, res, method="GET", headers=non_local)
        test(f"{label} public_gate: {res} non-local no auth -> 200 or 404",
             st in (200, 404), f"status={st}")

    # APPROVE bearer lets content through from non-local (Ranger's actual
    # demo flow: Cloudflare tunnel + Bearer token in header).
    h_approve = dict(non_local); h_approve["Authorization"] = f"Bearer {approve}"
    st, _ = http_method(port, "/proc/status", method="GET", headers=h_approve)
    test(f"{label} public_gate: /proc/status non-local + APPROVE -> 200",
         st == 200, f"status={st}")

    # Basic auth also passes. Token is the bearer value, user field unused.
    import base64 as _b64
    basic = _b64.b64encode(f":{approve}".encode()).decode()
    h_basic = dict(non_local); h_basic["Authorization"] = f"Basic {basic}"
    st, _ = http_method(port, "/proc/status", method="GET", headers=h_basic)
    test(f"{label} public_gate: /proc/status non-local + Basic -> 200",
         st == 200, f"status={st}")

    # 401 must advertise Basic realm so browsers show the native dialog
    # (this is what lets humans type the approve token without a /gate page).
    import http.client as _hc
    c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/home/some-world",
              headers={"X-Forwarded-For": "203.0.113.1"})
    r = c.getresponse(); r.read()
    wa = r.getheader("www-authenticate") or ""
    c.close()
    test(f"{label} public_gate: 401 includes WWW-Authenticate: Basic",
         "Basic" in wa and "realm" in wa.lower(), f"wa={wa!r}")


def _run_meta_headers_tests(port, label, token, approve):
    """Phase 1 of 'HTTP is all you need': X-Meta-* request headers travel
    with each PUT and replay on GET / ?raw. 16 checks covering happy path,
    internal-ops isolation, read-side re-validation, and response-header
    injection denial (X-Accel-Redirect etc. out of x-meta-* prefix)."""
    W = "meta-test-world"

    # cleanup prior state
    http_method(port, f"/home/{W}", method="DELETE", token=approve)

    # Metadata lives in response headers — period. The JSON body carries
    # only content fields (stage_html + pending_js + js_result + version +
    # ext). _meta_pairs() reads the response headers directly and returns
    # the list of [k, v] pairs for x-meta-*, preserving duplicates.
    import http.client as _hc
    def _raw_hdrs(path, extra_headers=None):
        c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = dict(extra_headers or {})
        c.request("GET", path, headers=hdrs)
        r = c.getresponse(); r.read(); c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}
    def _meta_pairs(path, extra_headers=None):
        c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = dict(extra_headers or {})
        c.request("GET", path, headers=hdrs)
        r = c.getresponse(); r.read(); c.close()
        # preserve ordering + duplicates (multi-value headers matter)
        return [[k.lower(), v] for k, v in r.getheaders() if k.lower().startswith("x-meta-")]

    # 1. PUT X-Meta-Author → GET response headers include it, JSON body
    #    does NOT carry a "headers" field (that was dropped).
    st, _ = http_method(port, f"/home/{W}", method="PUT", body="x",
                        token=token, headers={"X-Meta-Author": "codex"})
    test(f"{label} meta: PUT X-Meta-Author -> 200", st == 200, f"status={st}")
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: JSON GET response headers carry x-meta-author",
         _meta_pairs(f"/home/{W}") == [["x-meta-author", "codex"]],
         f"pairs={_meta_pairs(f'/home/{W}')}")
    test(f"{label} meta: JSON body does NOT carry 'headers' field",
         "headers" not in d, f"body_keys={list(d.keys())}")

    # 2. ?raw response has x-meta-author header (unchanged behaviour)
    st, hdrs = _raw_hdrs(f"/home/{W}?raw")
    test(f"{label} meta: ?raw reflects x-meta-author",
         hdrs.get("x-meta-author") == "codex", f"headers={hdrs}")

    # 3. ?raw + Range → 206 also has x-meta-author
    c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", f"/home/{W}?raw", headers={"Range": "bytes=0-0"})
    r = c.getresponse(); r.read(); hdrs3 = {k.lower(): v for k, v in r.getheaders()}; st3 = r.status; c.close()
    test(f"{label} meta: ?raw Range 206 also reflects x-meta-author",
         st3 == 206 and hdrs3.get("x-meta-author") == "codex",
         f"status={st3} headers={hdrs3}")

    # 3a. JSON GET and ?raw report the same x-meta-* set
    _, hdrs_json = _raw_hdrs(f"/home/{W}")
    test(f"{label} meta: JSON GET x-meta-* matches ?raw x-meta-*",
         hdrs_json.get("x-meta-author") == hdrs.get("x-meta-author"),
         f"json={hdrs_json.get('x-meta-author')!r} raw={hdrs.get('x-meta-author')!r}")

    # 4. Multi-value (same name twice) → two list entries, HTTP allows it
    #    urllib.request can't send dup headers easily, use http.client
    c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("PUT", f"/home/{W}")
    c.putheader("Authorization", f"Bearer {token}")
    c.putheader("X-Meta-Tag", "a")
    c.putheader("X-Meta-Tag", "b")
    c.putheader("Content-Length", "1")
    c.endheaders(); c.send(b"x"); r = c.getresponse(); r.read(); c.close()
    pairs = _meta_pairs(f"/home/{W}")
    tags = [v for k, v in pairs if k == "x-meta-tag"]
    test(f"{label} meta: multi-value X-Meta-Tag preserved", tags == ["a", "b"],
         f"tags={tags} full={pairs}")

    # 5. Authorization NOT stored (it's auth, not meta)
    http_method(port, f"/home/{W}", method="PUT", body="x",
                token=token, headers={"X-Meta-Only": "yes"})
    kept = [k for k, _ in _meta_pairs(f"/home/{W}")]
    test(f"{label} meta: Authorization not stored",
         "authorization" not in kept, f"kept={kept}")

    # 6. X-Forwarded-For not stored (infra, not user intent; not x-meta-*)
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Keep": "1", "X-Forwarded-For": "1.2.3.4"})
    kept = [k for k, _ in _meta_pairs(f"/home/{W}")]
    test(f"{label} meta: X-Forwarded-For not stored",
         "x-forwarded-for" not in kept, f"kept={kept}")

    # 7. X-Accel-Redirect not stored (nginx-interpreted response directive)
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Keep": "1", "X-Accel-Redirect": "/etc/passwd"})
    kept = [k for k, _ in _meta_pairs(f"/home/{W}")]
    test(f"{label} meta: X-Accel-Redirect not stored",
         "x-accel-redirect" not in kept, f"kept={kept}")

    # 8. CRLF/NUL injection in X-Meta value → dropped (can't inject via Python
    # http clients directly — they refuse bad headers — so we inject at the
    # DB layer to confirm read-side catches it too). See test 16 for that path.
    # Here we do the client-side path: valid char-only stays, bad char rejected.
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Clean": "plain-ascii-ok"})
    pairs = _meta_pairs(f"/home/{W}")
    test(f"{label} meta: clean ASCII value stored",
         any(k == "x-meta-clean" and v == "plain-ascii-ok" for k, v in pairs),
         f"headers={pairs}")

    # 9. X-Elastik-Internal NOT stored (reserved prefix, also not x-meta-*)
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Author": "u", "X-Elastik-Internal": "hack"})
    kept = [k for k, _ in _meta_pairs(f"/home/{W}")]
    test(f"{label} meta: X-Elastik-* not stored (not x-meta-*)",
         "x-elastik-internal" not in kept, f"kept={kept}")

    # 10. One over-size value → drops that one, keeps others
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Keep": "small", "X-Meta-Huge": "a" * 2000})
    kept = dict(_meta_pairs(f"/home/{W}"))
    test(f"{label} meta: oversized value drops that header only",
         "x-meta-keep" in kept and "x-meta-huge" not in kept,
         f"kept={list(kept)}")

    # 11. Total >8 KB → drop all (try with many small headers)
    many = {f"X-Meta-Key{i}": "v" * 100 for i in range(100)}   # ~10 KB total JSON
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers=many)
    pairs = _meta_pairs(f"/home/{W}")
    test(f"{label} meta: total-overflow drops all",
         pairs == [], f"pairs_len={len(pairs)}")

    # 12. POST /sync does NOT clobber headers column
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Author": "alice"})
    http_method(port, f"/home/{W}/sync", method="POST", body="sync-payload",
                token=token, headers={"X-Meta-Author": "mallory"})
    pairs = _meta_pairs(f"/home/{W}")
    test(f"{label} meta: /sync internal op does not clobber headers",
         pairs == [["x-meta-author", "alice"]], f"pairs={pairs}")

    # 13. POST append does NOT clobber headers column either (Phase 1 scope)
    http_method(port, f"/home/{W}", method="PUT", body="a", token=token,
                headers={"X-Meta-Author": "alice"})
    http_method(port, f"/home/{W}", method="POST", body="b", token=token,
                headers={"X-Meta-Author": "mallory"})
    pairs = _meta_pairs(f"/home/{W}")
    test(f"{label} meta: POST append does not clobber headers (PUT-only)",
         pairs == [["x-meta-author", "alice"]], f"pairs={pairs}")

    # 14. Old client: PUT with no X-Meta-* → no x-meta-* in response headers
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token)
    pairs = _meta_pairs(f"/home/{W}")
    test(f"{label} meta: no X-Meta-* → empty response meta",
         pairs == [], f"pairs={pairs}")

    # 15. Manually inject >8 KB headers JSON at DB level → ?raw returns no x-meta-*
    import sqlite3, os
    db_path = os.path.join("data", "home%2F" + W, "universe.db")
    if not os.path.exists(db_path):
        # fallback: world under ELASTIK_DATA not home/; try bare name
        db_path = os.path.join("data", W, "universe.db")
    if os.path.exists(db_path):
        huge_pairs = [[f"x-meta-k{i}", "v" * 200] for i in range(50)]  # ~12 KB JSON
        c = sqlite3.connect(db_path)
        c.execute("UPDATE stage_meta SET headers=? WHERE id=1",
                  (json.dumps(huge_pairs, separators=(",", ":")),))
        c.commit(); c.close()
        # close any cached connection on server side: PUT to force re-open? Actually
        # server caches conn — easiest: DELETE then re-PUT closes the cached handle.
        # Instead: just query and hope server re-reads. Sqlite WAL across processes
        # should give us the fresh row.
        _, hdrs15 = _raw_hdrs(f"/home/{W}?raw")
        meta15 = [k for k in hdrs15 if k.startswith("x-meta-")]
        test(f"{label} meta: read-side total-size fail-closed",
             meta15 == [], f"leaked x-meta-* headers: {meta15}")
    else:
        skip(f"{label} meta: read-side total-size fail-closed",
             f"db path not found: {db_path}")

    # 16. Manually inject X-Accel-Redirect at DB level → not replayed
    if os.path.exists(db_path):
        bad_pairs = [["x-accel-redirect", "/etc/passwd"], ["x-meta-ok", "yes"]]
        c = sqlite3.connect(db_path)
        c.execute("UPDATE stage_meta SET headers=? WHERE id=1",
                  (json.dumps(bad_pairs, separators=(",", ":")),))
        c.commit(); c.close()
        _, hdrs16 = _raw_hdrs(f"/home/{W}?raw")
        test(f"{label} meta: read-side prefix filter blocks x-accel-redirect",
             "x-accel-redirect" not in hdrs16 and hdrs16.get("x-meta-ok") == "yes",
             f"headers={hdrs16}")

    # cleanup
    http_method(port, f"/home/{W}", method="DELETE", token=approve)


def _run_audit_binding_tests(port, label, token, approve):
    """Phase 2.5: every write-type event's payload carries version_after,
    meta_headers, and body_sha256_after. Append additionally carries
    append_len / append_sha256. HMAC chain over the expanded payload
    still verifies. DAV PUT shares the same shape."""
    import hashlib as _hl, sqlite3 as _sq, os as _os, hmac as _hm
    W = "audit-test-world"
    http_method(port, f"/home/{W}", method="DELETE", token=approve)

    def _db_path(world):
        # home/ prefix is URL sugar — internal world name drops it. System
        # namespaces (etc/, var/, boot/) keep their prefix. Mirrors server.py.
        if world.startswith("home/"): world = world[5:]
        disk = world.replace("/", "%2F")
        return _os.path.join("data", disk, "universe.db")

    def _last_event_payload(world):
        p = _db_path(world)
        if not _os.path.exists(p): return None
        db = _sq.connect(p)
        try:
            row = db.execute(
                "SELECT payload FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            db.close()

    # 1. PUT → event has version_after, meta_headers, body_sha256_after, op
    body = "hello-audit"
    http_method(port, f"/home/{W}", method="PUT", body=body, token=token,
                headers={"X-Meta-Author": "codex"})
    p = _last_event_payload(f"home/{W}")
    test(f"{label} audit: PUT event has op=put",
         p and p.get("op") == "put", f"payload={p}")
    test(f"{label} audit: PUT event version_after is int",
         p and isinstance(p.get("version_after"), int) and p["version_after"] >= 1,
         f"ver={p.get('version_after')}")
    test(f"{label} audit: PUT event meta_headers preserved",
         p and p.get("meta_headers") == [["x-meta-author", "codex"]],
         f"meta={p.get('meta_headers')}")
    expected_hash = _hl.sha256(body.encode()).hexdigest()
    test(f"{label} audit: PUT event body_sha256_after correct",
         p and p.get("body_sha256_after") == expected_hash,
         f"got={p.get('body_sha256_after')[:16] if p else None}... expected={expected_hash[:16]}...")

    # 2. POST append → event has op=append, append_sha256, version_after, body_sha256_after
    append_body = " more"
    http_method(port, f"/home/{W}", method="POST", body=append_body, token=token)
    p2 = _last_event_payload(f"home/{W}")
    test(f"{label} audit: append event has op=append",
         p2 and p2.get("op") == "append", f"payload={p2}")
    test(f"{label} audit: append_sha256 matches delta",
         p2 and p2.get("append_sha256") == _hl.sha256(append_body.encode()).hexdigest(),
         f"got={p2.get('append_sha256')[:16] if p2 else None}...")
    test(f"{label} audit: append version_after = prev+1",
         p2 and p2.get("version_after") == p["version_after"] + 1,
         f"prev={p['version_after']} after={p2.get('version_after') if p2 else None}")
    test(f"{label} audit: append body_sha256_after = sha256(prev+append)",
         p2 and p2.get("body_sha256_after") == _hl.sha256((body + append_body).encode()).hexdigest(),
         f"got={p2.get('body_sha256_after')[:16] if p2 else None}...")
    test(f"{label} audit: append meta_headers is empty []",
         p2 and p2.get("meta_headers") == [], f"meta={p2.get('meta_headers')}")

    # 3. HMAC chain verifies end-to-end — recompute with the known KEY
    # and compare against stored hmac. A stubbed HMAC impl that just
    # chains values would pass a linkage-only check; recomputing catches it.
    #
    # The test subprocess was started with ELASTIK_KEY="test-audit-hmac-key"
    # (see test_python env setup). Mirrors server.log_event exactly:
    #     hmac = sha256(KEY, (prev_hmac + payload_json_str).encode())
    KEY = b"test-audit-hmac-key"
    dbp = _db_path(f"home/{W}")
    if _os.path.exists(dbp):
        db = _sq.connect(dbp)
        try:
            rows = db.execute(
                "SELECT id, payload, hmac, prev_hmac FROM events ORDER BY id"
            ).fetchall()
            linkage_ok = True
            last_hmac = ""
            recompute_ok = True
            for _id, _pl, _h, _ph in rows:
                if _ph != last_hmac:
                    linkage_ok = False
                    break
                expected = _hm.new(KEY, (_ph + _pl).encode("utf-8"),
                                   _hl.sha256).hexdigest()
                if expected != _h:
                    recompute_ok = False
                    break
                last_hmac = _h
            test(f"{label} audit: HMAC chain prev_hmac linkage holds",
                 linkage_ok, f"rows={len(rows)}")
            test(f"{label} audit: HMAC recomputes to stored value (key-aware)",
                 recompute_ok, f"rows={len(rows)}")
        finally:
            db.close()

    # 4. Current body hash matches last event's body_sha256_after
    st, body_raw = http_get(port, f"/home/{W}?raw")
    current_hash = _hl.sha256(body_raw.encode() if isinstance(body_raw, str) else body_raw).hexdigest()
    p3 = _last_event_payload(f"home/{W}")
    test(f"{label} audit: current body matches last event's body_sha256_after",
         p3 and p3.get("body_sha256_after") == current_hash,
         f"current={current_hash[:16]}... last={p3.get('body_sha256_after')[:16] if p3 else None}...")

    # 5. Internal ops (/sync) do NOT emit events — sanity-check the contract
    dbp = _db_path(f"home/{W}")
    db = _sq.connect(dbp)
    events_before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    db.close()
    http_method(port, f"/home/{W}/sync", method="POST", body="sync-payload",
                token=token, headers={"X-Meta-Author": "mallory"})
    db = _sq.connect(dbp)
    events_after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    db.close()
    test(f"{label} audit: /sync does NOT emit audit events",
         events_before == events_after,
         f"before={events_before} after={events_after}")

    # 5b. Non-ASCII byte-length: "你好" = 2 codepoints, 6 UTF-8 bytes.
    # `len` and `body_sha256_after` must both describe the same byte
    # string. Before the P2 fix, core PUT logged len=2 (str codepoints)
    # while DAV logged len=6 (already-bytes), making cross-surface
    # size checks inconsistent.
    http_method(port, f"/home/{W}", method="PUT", body="你好", token=token)
    pu = _last_event_payload(f"home/{W}")
    test(f"{label} audit: non-ASCII core PUT len is byte count (6, not 2)",
         pu and pu.get("len") == 6, f"len={pu.get('len') if pu else None}")
    test(f"{label} audit: non-ASCII core PUT hash over same bytes",
         pu and pu.get("body_sha256_after") == _hl.sha256("你好".encode("utf-8")).hexdigest(),
         f"hash={pu.get('body_sha256_after')[:16] if pu else None}...")

    # 6. DAV PUT → same event shape as core PUT (option B convergence)
    DW = "audit-dav-test-world"
    http_method(port, f"/home/{DW}", method="DELETE", token=approve)
    http_method(port, f"/dav/home/{DW}", method="PUT", body="dav-body",
                basic_auth=approve, headers={"X-Meta-Author": "dav-client"})
    pd = _last_event_payload(f"home/{DW}")
    test(f"{label} audit: DAV PUT event has op=put",
         pd and pd.get("op") == "put", f"payload={pd}")
    test(f"{label} audit: DAV PUT event meta_headers preserved",
         pd and pd.get("meta_headers") == [["x-meta-author", "dav-client"]],
         f"meta={pd.get('meta_headers') if pd else None}")
    test(f"{label} audit: DAV PUT event body_sha256_after correct",
         pd and pd.get("body_sha256_after") == _hl.sha256(b"dav-body").hexdigest(),
         f"hash={pd.get('body_sha256_after')[:16] if pd else None}...")

    # cleanup
    http_method(port, f"/home/{W}", method="DELETE", token=approve)
    http_method(port, f"/home/{DW}", method="DELETE", token=approve)


def _run_head_tests(port, label, token, approve):
    """HEAD = stat() for world read paths.

    Contract: HEAD ≡ corresponding GET minus body bytes. Same status,
    same headers (Content-Length reflects what the GET body would be,
    Content-Type, Accept-Ranges, Content-Range on 206, X-Meta-* on
    both JSON and ?raw world reads), body is always empty.

    AI does stat() via `curl -I /home/foo` — metadata lives in
    response headers, which is where HTTP has always put it. ?raw is
    only needed when you want raw bytes (not metadata). Browser-shell
    HEAD (Accept: text/html) deliberately does NOT leak world
    metadata — asserted here.
    """
    import http.client as _hc
    W = "head-test-world"

    # cleanup prior state
    http_method(port, f"/home/{W}", method="DELETE", token=approve)

    # seed a world with X-Meta-Author header so ?raw has metadata to reflect
    body = "hello head test"
    http_method(port, f"/home/{W}", method="PUT", body=body, token=token,
                headers={"X-Meta-Author": "ranger"})

    def _head(path, extra_headers=None):
        c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = dict(extra_headers or {})
        c.request("HEAD", path, headers=hdrs)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, payload

    def _get(path, extra_headers=None):
        c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = dict(extra_headers or {})
        c.request("GET", path, headers=hdrs)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, payload

    # 1. HEAD /home/<w> — JSON read surface, body empty, headers == GET JSON
    gst, ghdrs, gbody = _get(f"/home/{W}")
    hst, hhdrs, hbody = _head(f"/home/{W}")
    test(f"{label} head: JSON read status matches GET",
         hst == gst == 200, f"head={hst} get={gst}")
    test(f"{label} head: JSON read Content-Type matches GET",
         hhdrs.get("content-type") == ghdrs.get("content-type"),
         f"head={hhdrs.get('content-type')} get={ghdrs.get('content-type')}")
    test(f"{label} head: JSON read Content-Length matches GET body size",
         hhdrs.get("content-length") == str(len(gbody)),
         f"head={hhdrs.get('content-length')} get_body_len={len(gbody)}")
    test(f"{label} head: JSON read body is empty", len(hbody) == 0, f"len={len(hbody)}")
    # JSON GET/HEAD now symmetric with ?raw: x-meta-* replays in response
    # headers too. The body's "headers" field is preserved for back-compat;
    # asserted separately in _run_meta_headers_tests.
    test(f"{label} head: JSON read reflects x-meta-author on HEAD",
         hhdrs.get("x-meta-author") == "ranger", f"headers={hhdrs}")

    # 2. HEAD /home/<w>?raw — X-Meta-* replayed, body empty
    gst, ghdrs, gbody = _get(f"/home/{W}?raw")
    hst, hhdrs, hbody = _head(f"/home/{W}?raw")
    test(f"{label} head: ?raw status matches GET", hst == gst == 200, f"head={hst} get={gst}")
    test(f"{label} head: ?raw Content-Type matches GET",
         hhdrs.get("content-type") == ghdrs.get("content-type"),
         f"head={hhdrs.get('content-type')} get={ghdrs.get('content-type')}")
    test(f"{label} head: ?raw Content-Length matches raw body size",
         hhdrs.get("content-length") == str(len(body.encode())),
         f"head={hhdrs.get('content-length')} body_len={len(body.encode())}")
    test(f"{label} head: ?raw reflects x-meta-author",
         hhdrs.get("x-meta-author") == "ranger", f"headers={hhdrs}")
    test(f"{label} head: ?raw body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 3. HEAD /home/<w>?raw + Range — 206, Content-Range, body empty
    hst, hhdrs, hbody = _head(f"/home/{W}?raw", {"Range": "bytes=0-4"})
    test(f"{label} head: ?raw+Range -> 206", hst == 206, f"status={hst}")
    test(f"{label} head: ?raw+Range Content-Range set",
         hhdrs.get("content-range") == f"bytes 0-4/{len(body.encode())}",
         f"range={hhdrs.get('content-range')}")
    test(f"{label} head: ?raw+Range Content-Length matches slice",
         hhdrs.get("content-length") == "5", f"len={hhdrs.get('content-length')}")
    test(f"{label} head: ?raw+Range body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 4. HEAD /home/<w>?v=<current> — 304, body empty
    gst, ghdrs, gbody = _get(f"/home/{W}")
    import json as _json
    ver = _json.loads(gbody.decode()).get("version")
    hst, hhdrs, hbody = _head(f"/home/{W}?v={ver}")
    test(f"{label} head: ?v=current -> 304", hst == 304, f"status={hst}")
    test(f"{label} head: 304 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 5. HEAD /home/ — directory listing stat, body empty
    gst, ghdrs, gbody = _get("/home/")
    hst, hhdrs, hbody = _head("/home/")
    test(f"{label} head: /home/ trailing-slash status matches GET",
         hst == gst == 200, f"head={hst} get={gst}")
    test(f"{label} head: /home/ Content-Type matches GET",
         hhdrs.get("content-type") == ghdrs.get("content-type"),
         f"head={hhdrs.get('content-type')} get={ghdrs.get('content-type')}")
    test(f"{label} head: /home/ body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 6. HEAD /home/<w>/sync — internal op, 405, body empty
    hst, hhdrs, hbody = _head(f"/home/{W}/sync")
    test(f"{label} head: internal op -> 405", hst == 405, f"status={hst}")
    test(f"{label} head: 405 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 7. HEAD /home/<nonexistent-but-has-children> — pure-dir ls fallback
    # Seed a child so /home/head-redirect-prefix has existing children
    http_method(port, f"/home/head-redirect-prefix/child", method="PUT",
                body="c", token=token)
    gst, ghdrs, gbody = _get("/home/head-redirect-prefix")
    hst, hhdrs, hbody = _head("/home/head-redirect-prefix")
    test(f"{label} head: missing world w/ children -> 200 ls fallback",
         hst == gst == 200, f"head={hst} get={gst}")
    test(f"{label} head: pure-dir fallback Content-Type matches GET",
         hhdrs.get("content-type") == ghdrs.get("content-type"),
         f"head={hhdrs.get('content-type')} get={ghdrs.get('content-type')}")
    test(f"{label} head: pure-dir fallback body is empty", len(hbody) == 0, f"len={len(hbody)}")
    hst, hhdrs, hbody = _head("/home/head-redirect-prefix?raw")
    test(f"{label} head: pure-dir ?raw -> 404", hst == 404, f"status={hst}")
    test(f"{label} head: pure-dir ?raw body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 8. HEAD /home/<truly-missing> — 404, body empty
    hst, hhdrs, hbody = _head("/home/this-world-definitely-does-not-exist-xyzzy")
    test(f"{label} head: missing world -> 404", hst == 404, f"status={hst}")
    test(f"{label} head: 404 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 9. HEAD /etc/shadow without approve — 403, body empty
    hst, hhdrs, hbody = _head("/etc/shadow")
    test(f"{label} head: sensitive read w/o approve -> 403",
         hst == 403, f"status={hst}")
    test(f"{label} head: 403 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 10. HEAD with Accept: text/html → browser shell path, NOT world read.
    #     Must NOT leak world x-meta-* into the shell response headers.
    #     Confirms the "reflect X-Meta-*" change is scoped to the JSON read
    #     branch, not the browser is_browser branch.
    hst, hhdrs, hbody = _head(f"/home/{W}", {"Accept": "text/html"})
    test(f"{label} head: shell path status 200", hst == 200, f"status={hst}")
    test(f"{label} head: shell path Content-Type is text/html",
         "text/html" in (hhdrs.get("content-type") or ""),
         f"content-type={hhdrs.get('content-type')}")
    test(f"{label} head: shell path does NOT leak world x-meta-*",
         "x-meta-author" not in hhdrs, f"headers={hhdrs}")

    # cleanup
    http_method(port, f"/home/{W}", method="DELETE", token=approve)
    http_method(port, f"/home/head-redirect-prefix/child", method="DELETE", token=approve)


def _run_lib_tests(port, label, token, approve):
    """Phase 0 of plugin-as-world: /lib/* namespace, state column, and
    the PUT /lib/<name>/state lifecycle transition.

    Covers:
      - /lib in _FHS: PUT creates world, GET reads it, DELETE needs T3
      - stage_meta.state column: lazy-migrated, default 'pending'
      - state field exposed in GET JSON envelope
      - PUT /lib/<name>/state with body='active'|'disabled' (T3 approve)
      - Idempotent re-transitions (changed:false, no event)
      - Invalid state body → 422
      - State transition emits audit event of type 'state_transition'

    Not covered (Phase 1): boot loader, plugin source exec/activation,
    route registration. Phase 0 is data model + routes only.
    """
    import http.client as _hc
    W = "lib-test-weather"
    src = "# plugin source\nROUTES = {}\nasync def handle(m,b,p): return {'ok':True}\n"

    # cleanup prior run
    http_method(port, f"/lib/{W}", method="DELETE", token=approve)

    # 1. PUT /lib/<name> with T2 → 200, creates world at state='pending'
    st, _ = http_method(port, f"/lib/{W}", method="PUT", body=src, token=token)
    test(f"{label} lib: PUT /lib/{W} T2 -> 200", st == 200, f"status={st}")

    # 2. GET /lib/<name> JSON shows state=pending
    st, body = http_get(port, f"/lib/{W}")
    test(f"{label} lib: GET /lib/{W} -> 200", st == 200, f"status={st}")
    d = json.loads(body) if st == 200 else {}
    test(f"{label} lib: newly-created world has state='pending'",
         d.get("state") == "pending", f"state={d.get('state')!r}")

    # 3. GET /lib/<name>?raw returns source bytes
    st, body = http_get(port, f"/lib/{W}?raw")
    test(f"{label} lib: ?raw returns plugin source", body == src,
         f"body={body[:40]!r}")

    # 4. PUT /lib/<name>/state with T2 → 403
    st, _ = http_method(port, f"/lib/{W}/state", method="PUT", body="active",
                        token=token)
    test(f"{label} lib: state transition with T2 -> 403", st == 403,
         f"status={st}")

    # 5. PUT /lib/<name>/state with invalid body → 422
    st, body = http_method(port, f"/lib/{W}/state", method="PUT",
                           body="garbage", basic_auth=approve)
    test(f"{label} lib: invalid state body -> 422", st == 422,
         f"status={st}")

    # 6. PUT /lib/<name>/state body='active' with T3 → 200, state=active
    st, body = http_method(port, f"/lib/{W}/state", method="PUT",
                           body="active", basic_auth=approve)
    test(f"{label} lib: activate with T3 -> 200", st == 200, f"status={st}")
    d = json.loads(body) if st == 200 else {}
    test(f"{label} lib: activation response state=active",
         d.get("state") == "active" and d.get("changed") is True,
         f"resp={d}")

    # 7. GET JSON now reports state=active
    _, body = http_get(port, f"/lib/{W}")
    d = json.loads(body)
    test(f"{label} lib: GET reports state=active after transition",
         d.get("state") == "active", f"state={d.get('state')!r}")

    # 8. Re-activate is idempotent (changed=false, no event)
    st, body = http_method(port, f"/lib/{W}/state", method="PUT",
                           body="active", basic_auth=approve)
    test(f"{label} lib: re-activate is idempotent", st == 200,
         f"status={st}")
    d = json.loads(body) if st == 200 else {}
    test(f"{label} lib: idempotent re-activation reports changed=false",
         d.get("changed") is False, f"resp={d}")

    # 9. Disable
    st, body = http_method(port, f"/lib/{W}/state", method="PUT",
                           body="disabled", basic_auth=approve)
    test(f"{label} lib: disable -> 200", st == 200, f"status={st}")
    d = json.loads(body) if st == 200 else {}
    test(f"{label} lib: disable reports from=active to=disabled",
         d.get("from") == "active" and d.get("state") == "disabled",
         f"resp={d}")

    # 10. DELETE /lib/<name> with T2 → 403 (lib requires approve)
    st, _ = http_method(port, f"/lib/{W}", method="DELETE", token=token)
    test(f"{label} lib: DELETE /lib/{W} with T2 -> 403", st == 403,
         f"status={st}")

    # 11. DELETE /lib/<name> with T3 → 200
    st, _ = http_method(port, f"/lib/{W}", method="DELETE", token=approve)
    test(f"{label} lib: DELETE /lib/{W} with T3 -> 200", st == 200,
         f"status={st}")
    st, _ = http_get(port, f"/lib/{W}")
    test(f"{label} lib: deleted world -> 404", st == 404, f"status={st}")

    # 12. State transition on nonexistent plugin → 404
    st, _ = http_method(port, f"/lib/does-not-exist/state", method="PUT",
                        body="active", basic_auth=approve)
    test(f"{label} lib: state transition on missing plugin -> 404",
         st == 404, f"status={st}")

    # 12a. Source-changing PUT resets approval (Codex P1 fix). An
    # active plugin that gets re-PUT'd by T2 must drop back to
    # state='pending'; else T2 could silently swap code under an
    # existing T3 approval.
    W2 = "lib-approval-reset-test"
    http_method(port, f"/lib/{W2}", method="DELETE", token=approve)
    http_method(port, f"/lib/{W2}", method="PUT", body="# v1", token=token)
    http_method(port, f"/lib/{W2}/state", method="PUT", body="active",
                basic_auth=approve)
    _, body_mid = http_get(port, f"/lib/{W2}")
    test(f"{label} lib: approval-reset — activated state=active",
         json.loads(body_mid).get("state") == "active",
         f"state={json.loads(body_mid).get('state')!r}")
    # T2 replaces source — must reset to pending
    http_method(port, f"/lib/{W2}", method="PUT", body="# v2 evil", token=token)
    _, body_after = http_get(port, f"/lib/{W2}")
    test(f"{label} lib: approval-reset — active -> pending after T2 PUT",
         json.loads(body_after).get("state") == "pending",
         f"state={json.loads(body_after).get('state')!r}")

    # 12b. Same rule for disabled plugins — re-PUT goes back to pending,
    # so re-approval is explicit.
    http_method(port, f"/lib/{W2}/state", method="PUT", body="active",
                basic_auth=approve)
    http_method(port, f"/lib/{W2}/state", method="PUT", body="disabled",
                basic_auth=approve)
    http_method(port, f"/lib/{W2}", method="PUT", body="# v3", token=token)
    _, body_after2 = http_get(port, f"/lib/{W2}")
    test(f"{label} lib: approval-reset — disabled -> pending after T2 PUT",
         json.loads(body_after2).get("state") == "pending",
         f"state={json.loads(body_after2).get('state')!r}")

    # 12c. Forced reset is audited — state_transition event with
    # reason='source replaced' lands on the chain right after the
    # stage_written event.
    import sqlite3 as _sq2, os as _os2
    db2 = _os2.path.join("data", "lib%2F" + W2, "universe.db")
    if _os2.path.exists(db2):
        conn2 = _sq2.connect(db2)
        try:
            rows = conn2.execute(
                "SELECT event_type, payload FROM events ORDER BY id DESC LIMIT 4"
            ).fetchall()
        finally:
            conn2.close()
        # Look for a state_transition with reason="source replaced"
        has_forced_reset = any(
            r[0] == "state_transition" and '"source replaced"' in (r[1] or "")
            for r in rows
        )
        test(f"{label} lib: approval-reset — audit records forced reset",
             has_forced_reset,
             f"recent events types={[r[0] for r in rows]}")

    # cleanup for 12a/b/c
    http_method(port, f"/lib/{W2}", method="DELETE", token=approve)

    # 12d. DAV parity — same approval-reset invariant via DAV PUT.
    # Codex P1 2026-04-21: DAV PUT to /dav/lib/<name> bypassed the
    # is_lib state reset. Before the fix, attacker with T2 could swap
    # source via DAV and leave state='active'; boot_load_active_lib on
    # next restart would exec the new code without fresh T3 approval.
    W2d = "lib-approval-reset-dav-test"
    http_method(port, f"/lib/{W2d}", method="DELETE", token=approve)
    http_method(port, f"/lib/{W2d}", method="PUT", body="# v1", token=token)
    http_method(port, f"/lib/{W2d}/state", method="PUT", body="active",
                basic_auth=approve)
    _, body_d1 = http_get(port, f"/lib/{W2d}")
    test(f"{label} lib: DAV approval-reset — activated state=active",
         json.loads(body_d1).get("state") == "active",
         f"state={json.loads(body_d1).get('state')!r}")
    # T2 replaces source via DAV — must reset to pending (not via core PUT)
    http_method(port, f"/dav/lib/{W2d}", method="PUT", body="# v2 evil via dav", token=token)
    _, body_d2 = http_get(port, f"/lib/{W2d}")
    test(f"{label} lib: DAV approval-reset — active -> pending after DAV PUT",
         json.loads(body_d2).get("state") == "pending",
         f"state={json.loads(body_d2).get('state')!r}")
    # Same rule for disabled → re-PUT via DAV lands on pending.
    http_method(port, f"/lib/{W2d}/state", method="PUT", body="active",
                basic_auth=approve)
    http_method(port, f"/lib/{W2d}/state", method="PUT", body="disabled",
                basic_auth=approve)
    http_method(port, f"/dav/lib/{W2d}", method="PUT", body="# v3 via dav", token=token)
    _, body_d3 = http_get(port, f"/lib/{W2d}")
    test(f"{label} lib: DAV approval-reset — disabled -> pending after DAV PUT",
         json.loads(body_d3).get("state") == "pending",
         f"state={json.loads(body_d3).get('state')!r}")
    # Audit chain records the forced reset emitted by DAV PUT too.
    db_d = _os2.path.join("data", "lib%2F" + W2d, "universe.db")
    if _os2.path.exists(db_d):
        conn_d = _sq2.connect(db_d)
        try:
            rows_d = conn_d.execute(
                "SELECT event_type, payload FROM events ORDER BY id DESC LIMIT 6"
            ).fetchall()
        finally:
            conn_d.close()
        has_dav_reset = any(
            r[0] == "state_transition" and '"source replaced"' in (r[1] or "")
            for r in rows_d
        )
        test(f"{label} lib: DAV approval-reset — audit records forced reset",
             has_dav_reset,
             f"recent events types={[r[0] for r in rows_d]}")
    http_method(port, f"/lib/{W2d}", method="DELETE", token=approve)

    # 12e. DAV listing surfaces /lib/ as top-level, not as home/lib/*.
    # Codex P2 2026-04-21: before _DAV_TOP_NAMESPACES was split off, the
    # /dav/ root only iterated _DAV_SYS_PREFIXES to list top-level
    # collections — lib/ wasn't in it, so /lib/* worlds got lumped under
    # /dav/home/ AND had no /dav/lib/ collection. Now both views are
    # consistent with the browser's FHS homepage.
    W2e = "lib-dav-listing-test"
    http_method(port, f"/lib/{W2e}", method="DELETE", token=approve)
    http_method(port, f"/lib/{W2e}", method="PUT", body="# listing", token=token)

    # Root PROPFIND should now advertise /dav/lib/ as a collection.
    st_pf, body_pf = http_method(port, "/dav/", method="PROPFIND",
                                 headers={"Depth": "1"})
    test(f"{label} lib: DAV PROPFIND / advertises /dav/lib/",
         st_pf == 207 and "/dav/lib/" in body_pf,
         f"status={st_pf} body_has_lib={'/dav/lib/' in body_pf}")
    # Root HTML listing should include a link to /dav/lib/.
    st_gl, body_gl = http_get(port, "/dav/")
    test(f"{label} lib: DAV GET / HTML listing includes lib/",
         st_gl == 200 and 'href="/dav/lib/"' in body_gl,
         f"status={st_gl}")
    # /dav/home/ PROPFIND must NOT alias lib/* as user content.
    st_hm, body_hm = http_method(port, "/dav/home/", method="PROPFIND",
                                 headers={"Depth": "1"})
    test(f"{label} lib: DAV /home/ PROPFIND excludes lib/* worlds",
         st_hm == 207 and f"/home/lib/{W2e}" not in body_hm,
         f"status={st_hm} has_leak={f'/home/lib/{W2e}' in body_hm}")
    # /dav/lib/ PROPFIND must list the plugin world.
    st_lb, body_lb = http_method(port, "/dav/lib/", method="PROPFIND",
                                 headers={"Depth": "1"})
    test(f"{label} lib: DAV /lib/ PROPFIND lists plugin worlds",
         st_lb == 207 and W2e in body_lb,
         f"status={st_lb} has_W2e={W2e in body_lb}")
    # T2 write to /dav/lib/ still works — lib/ is NOT in _DAV_SYS_PREFIXES
    # for auth, only for listing. Consistent with core PUT /lib/<n>.
    st_t2, _ = http_method(port, f"/dav/lib/{W2e}", method="PUT",
                           body="# T2 write via DAV", token=token)
    test(f"{label} lib: DAV PUT /dav/lib/<n> accepts T2 (same as core /lib/<n>)",
         st_t2 == 201, f"status={st_t2}")
    http_method(port, f"/lib/{W2e}", method="DELETE", token=approve)

    # 12f. DAV MOVE/COPY/DELETE vs /lib/* approval-binding.
    # Red-team 2026-04-21: T2 could MOVE or COPY an approved /lib plugin
    # to a new name and carry state='active' over, auto-booting on next
    # server restart without fresh T3 approval. And DAV DELETE on /lib/*
    # only required T2 where core DELETE requires T3. Close all three
    # holes in one commit.
    Wf = "lib-dav-verb-test"
    Wf_dst = "lib-dav-verb-test-moved"
    Wf_copy = "lib-dav-verb-test-clone"
    # Setup: install + activate lib/<Wf>
    http_method(port, f"/lib/{Wf}", method="DELETE", token=approve)
    http_method(port, f"/lib/{Wf_dst}", method="DELETE", token=approve)
    http_method(port, f"/lib/{Wf_copy}", method="DELETE", token=approve)
    http_method(port, f"/lib/{Wf}", method="PUT",
                body="ROUTES=['/lib-verb-test']\nAUTH='none'\n"
                     "async def handle(m,b,p): return {'v':1}\n",
                token=token)
    http_method(port, f"/lib/{Wf}/state", method="PUT", body="active",
                basic_auth=approve)
    _, body_pre = http_get(port, f"/lib/{Wf}")
    test(f"{label} lib: verb-test setup — lib/{Wf} is active",
         json.loads(body_pre).get("state") == "active",
         f"state={json.loads(body_pre).get('state')!r}")

    # (a) T2 MOVE /dav/lib/<Wf> -> /dav/lib/<Wf_dst>: dst must land pending.
    import urllib.request as _ur, urllib.error as _ue, base64 as _b64
    def _t2_dav(method, src_path, dst_path=None):
        url = f"http://127.0.0.1:{port}{src_path}"
        req = _ur.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if dst_path:
            req.add_header("Destination", f"http://127.0.0.1:{port}{dst_path}")
        try:
            r = _ur.urlopen(req, timeout=5)
            return r.status
        except _ue.HTTPError as e:
            return e.code
    st_mv = _t2_dav("MOVE", f"/dav/lib/{Wf}", f"/dav/lib/{Wf_dst}")
    test(f"{label} lib: T2 DAV MOVE to /lib/* succeeds (T2 auth preserved)",
         st_mv == 204, f"status={st_mv}")
    _, body_mv = http_get(port, f"/lib/{Wf_dst}")
    test(f"{label} lib: T2 DAV MOVE dst -> state=pending (approval bypass blocked)",
         json.loads(body_mv).get("state") == "pending",
         f"state={json.loads(body_mv).get('state')!r}")

    # (b) T2 COPY /dav/lib/<Wf_dst> -> /dav/lib/<Wf_copy>: same rule.
    # Re-activate the moved world so the COPY source has state=active.
    http_method(port, f"/lib/{Wf_dst}/state", method="PUT", body="active",
                basic_auth=approve)
    st_cp = _t2_dav("COPY", f"/dav/lib/{Wf_dst}", f"/dav/lib/{Wf_copy}")
    test(f"{label} lib: T2 DAV COPY to /lib/* succeeds (T2 auth preserved)",
         st_cp == 204, f"status={st_cp}")
    _, body_cp = http_get(port, f"/lib/{Wf_copy}")
    test(f"{label} lib: T2 DAV COPY dst -> state=pending (approval bypass blocked)",
         json.loads(body_cp).get("state") == "pending",
         f"state={json.loads(body_cp).get('state')!r}")

    # (c) T2 DAV DELETE /dav/lib/<n> must be refused (403).
    st_del = _t2_dav("DELETE", f"/dav/lib/{Wf_dst}")
    test(f"{label} lib: T2 DAV DELETE /lib/<n> -> 403 (T3 required)",
         st_del == 403, f"status={st_del}")
    _, body_still = http_get(port, f"/lib/{Wf_dst}")
    test(f"{label} lib: T2 DAV DELETE refused — world still exists",
         json.loads(body_still).get("state") in ("active", "pending", "disabled"),
         f"body={body_still[:80]!r}")

    # Cleanup
    http_method(port, f"/lib/{Wf_dst}", method="DELETE", token=approve)
    http_method(port, f"/lib/{Wf_copy}", method="DELETE", token=approve)

    # 13. Audit chain — state_transition events recorded.
    # Read sqlite directly (same pattern as _run_audit_binding_tests);
    # /dev/db would need to fight the server's WAL-mode connection for
    # read-only access, and that's a separate plugin concern.
    import sqlite3 as _sq, os as _os
    http_method(port, f"/lib/{W}", method="PUT", body=src, token=token)
    http_method(port, f"/lib/{W}/state", method="PUT", body="active",
                basic_auth=approve)
    http_method(port, f"/lib/{W}/state", method="PUT", body="disabled",
                basic_auth=approve)
    db_path = _os.path.join("data", "lib%2F" + W, "universe.db")
    if _os.path.exists(db_path):
        db = _sq.connect(db_path)
        try:
            rows = db.execute(
                "SELECT event_type, payload FROM events "
                "ORDER BY id DESC LIMIT 5"
            ).fetchall()
        finally:
            db.close()
        types = [r[0] for r in rows]
        # Expected (newest first): state_transition (→disabled),
        #   state_transition (→active), stage_written (PUT source), ...
        test(f"{label} lib: audit chain has ≥2 state_transition events",
             types.count("state_transition") >= 2, f"types={types}")
        # Check payload of the most recent state_transition
        trans_row = next((r for r in rows if r[0] == "state_transition"), None)
        if trans_row:
            try:
                pd = json.loads(trans_row[1])
            except (json.JSONDecodeError, TypeError):
                pd = {}
            test(f"{label} lib: state_transition payload has from+to+version",
                 pd.get("from") in ("pending", "active") and
                 pd.get("to") in ("active", "disabled") and
                 "version" in pd,
                 f"payload={pd}")
    else:
        skip(f"{label} lib: audit chain check",
             f"world db not found at {db_path}")

    # cleanup
    http_method(port, f"/lib/{W}", method="DELETE", token=approve)

    # ── Phase 1: actual exec + route registration on activation ──
    # Phase 0 recorded state only; Phase 1 wires the state transitions
    # to real effects. Runtime activation and boot-time loading share
    # the same plugins.load_plugin_from_source() code path — testing
    # runtime exercises the boot path as well.

    # 14. Activation actually registers a route callable via HTTP
    W3 = "lib-exec-test"
    http_method(port, f"/lib/{W3}", method="DELETE", token=approve)
    # Minimal plugin that registers /lib-exec-test and returns a known body.
    # Uses v1 spec: ROUTES as list + handle() async function.
    plugin_src = (
        "ROUTES = ['/lib-exec-test']\n"
        "AUTH = 'none'\n"
        "async def handle(method, body, params):\n"
        "    return {'hello': 'from-lib-plugin'}\n"
    )
    st, _ = http_method(port, f"/lib/{W3}", method="PUT", body=plugin_src,
                        token=token)
    test(f"{label} lib: PUT plugin source -> 200", st == 200, f"status={st}")
    # Before activation, the route is NOT registered. We check the
    # canonical route list (/bin) rather than probing the route itself,
    # because unmatched paths fall through to the browser INDEX shell
    # (200 text/html), which would look like "route exists".
    def _route_in_bin(route):
        _, bin_body = http_method(port, "/bin", method="GET",
                                   headers={"Accept": "application/json"})
        try:
            return any(e.get("route") == route for e in json.loads(bin_body))
        except (json.JSONDecodeError, AttributeError):
            return False
    test(f"{label} lib: pre-activation route NOT in /bin",
         not _route_in_bin("/lib-exec-test"),
         "route leaked into /bin before activation")
    # Activate — load_plugin_from_source should register the route
    st, body = http_method(port, f"/lib/{W3}/state", method="PUT",
                           body="active", basic_auth=approve)
    test(f"{label} lib: activation succeeds -> 200", st == 200,
         f"status={st} body={body[:100]}")
    # Route now appears in /bin and actually serves
    test(f"{label} lib: /bin now lists activated plugin route",
         _route_in_bin("/lib-exec-test"),
         "route did not register in /bin after activation")
    st, body = http_get(port, "/lib-exec-test")
    test(f"{label} lib: post-activation route returns 200", st == 200,
         f"status={st}")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} lib: post-activation route runs plugin code",
                 d.get("hello") == "from-lib-plugin", f"body={body[:60]}")
        except json.JSONDecodeError:
            test(f"{label} lib: plugin returns JSON", False, f"body={body[:60]}")

    # 15. Invalid source (syntax error) → activation fails with 422,
    #     state stays at prev (pending); routes NOT registered.
    W4 = "lib-badsrc-test"
    http_method(port, f"/lib/{W4}", method="DELETE", token=approve)
    bad_src = "def this is not valid python\n"
    http_method(port, f"/lib/{W4}", method="PUT", body=bad_src, token=token)
    st, body = http_method(port, f"/lib/{W4}/state", method="PUT",
                           body="active", basic_auth=approve)
    test(f"{label} lib: activation of invalid source -> 422", st == 422,
         f"status={st}")
    # State should still be 'pending' (transition was refused)
    _, body = http_get(port, f"/lib/{W4}")
    d = json.loads(body)
    test(f"{label} lib: failed activation leaves state=pending",
         d.get("state") == "pending", f"state={d.get('state')!r}")
    http_method(port, f"/lib/{W4}", method="DELETE", token=approve)

    # 16. Route collision → activation fails with 422
    W5 = "lib-collide-test"
    http_method(port, f"/lib/{W5}", method="DELETE", token=approve)
    # Try to register /lib-exec-test (already owned by W3's plugin).
    collide_src = (
        "ROUTES = ['/lib-exec-test']\n"
        "AUTH = 'none'\n"
        "async def handle(method, body, params):\n"
        "    return {'hijacked': True}\n"
    )
    http_method(port, f"/lib/{W5}", method="PUT", body=collide_src, token=token)
    st, body = http_method(port, f"/lib/{W5}/state", method="PUT",
                           body="active", basic_auth=approve)
    test(f"{label} lib: activation on route collision -> 422", st == 422,
         f"status={st}")
    # Victim plugin (W3) still serves its own content, not hijacked
    st, body = http_get(port, "/lib-exec-test")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} lib: collision-refused activation did NOT hijack",
                 d.get("hello") == "from-lib-plugin",
                 f"body={body[:60]}")
        except json.JSONDecodeError:
            test(f"{label} lib: victim plugin still returns JSON", False,
                 f"body={body[:60]}")
    http_method(port, f"/lib/{W5}", method="DELETE", token=approve)

    # 17. Name collision with Tier 0 plugin — retired in v4.5.0 microkernel
    # cut. There are no Tier 0 disk plugins anymore (load_plugins + the whole
    # plugins/available/*.py autoloader is gone), so the collision check in
    # load_plugin_from_source is unreachable by construction. The check
    # itself is kept as defensive code for completeness but the HTTP test
    # for it requires a loaded Tier 0 that no longer exists.

    # 18. Disable removes the route from runtime (check via /bin listing)
    st, body = http_method(port, f"/lib/{W3}/state", method="PUT",
                           body="disabled", basic_auth=approve)
    test(f"{label} lib: disable -> 200", st == 200, f"status={st}")
    test(f"{label} lib: disabled plugin route removed from /bin",
         not _route_in_bin("/lib-exec-test"),
         "route leaked in /bin after disable")

    # 19. Re-activation re-registers
    st, _ = http_method(port, f"/lib/{W3}/state", method="PUT",
                        body="active", basic_auth=approve)
    test(f"{label} lib: re-activate -> 200", st == 200, f"status={st}")
    test(f"{label} lib: re-activated route back in /bin",
         _route_in_bin("/lib-exec-test"),
         "route did not re-register after re-activation")

    # 20. DELETE an active plugin unregisters the route before trashing
    st, _ = http_method(port, f"/lib/{W3}", method="DELETE", token=approve)
    test(f"{label} lib: DELETE active plugin -> 200", st == 200,
         f"status={st}")
    test(f"{label} lib: post-DELETE route removed from /bin",
         not _route_in_bin("/lib-exec-test"),
         "route leaked in /bin after DELETE")

    # 21. Default ext for /lib/* PUT is 'py' (Codex Phase 0 observation)
    W6 = "lib-ext-test"
    http_method(port, f"/lib/{W6}", method="DELETE", token=approve)
    http_method(port, f"/lib/{W6}", method="PUT",
                body="# just a comment\n", token=token)
    _, body = http_get(port, f"/lib/{W6}")
    d = json.loads(body)
    test(f"{label} lib: PUT /lib/* defaults to ext='py'",
         d.get("ext") == "py", f"ext={d.get('ext')!r}")
    # Explicit ?ext= still wins
    http_method(port, f"/lib/{W6}?ext=plain", method="PUT",
                body="# still plain\n", token=token)
    _, body = http_get(port, f"/lib/{W6}")
    d = json.loads(body)
    test(f"{label} lib: explicit ?ext= overrides the 'py' default",
         d.get("ext") == "plain", f"ext={d.get('ext')!r}")
    http_method(port, f"/lib/{W6}", method="DELETE", token=approve)

    # 22. P2 retry fix: active → active is a no-op while the plugin is
    # loaded, but a retry path when the plugin was active on disk (state
    # column) yet failed to load at runtime. Simulate by force-unloading
    # a live plugin through /admin/unload, then PUT state=active and
    # expect the routes to come back.
    W7 = "lib-retry-test"
    http_method(port, f"/lib/{W7}", method="DELETE", token=approve)
    # Test plugin declares a second route that calls unload_plugin on
    # itself via NEEDS injection. With /admin cut in the microkernel, this
    # is the only in-process way to simulate "state column says active but
    # plugin is not loaded" — i.e. the guardrail-D boot-failure scenario
    # the active→active retry path exists to handle. The self-kill route
    # triggers unload; the plugin's state column stays 'active' because
    # unload_plugin only touches _plugin_meta / _plugins, not the world DB.
    retry_src = (
        "NEEDS = ['unload_plugin']\n"
        "ROUTES = ['/lib-retry-test', '/lib-retry-test-selfkill']\n"
        "AUTH = 'none'\n"
        "async def handle(m, b, p):\n"
        "    scope = p.get('_scope', {})\n"
        "    if scope.get('path', '').startswith('/lib-retry-test-selfkill'):\n"
        "        unload_plugin('lib:lib-retry-test')\n"
        "        return {'unloaded': True}\n"
        "    return {'retry': 'ok'}\n"
    )
    http_method(port, f"/lib/{W7}", method="PUT", body=retry_src, token=token)
    http_method(port, f"/lib/{W7}/state", method="PUT", body="active",
                basic_auth=approve)
    test(f"{label} lib: retry — route live pre-unload",
         _route_in_bin("/lib-retry-test"),
         "route not registered before retry test")
    # Trigger in-process self-unload via the helper route. After this the
    # plugin's routes (including the selfkill route) are gone from _plugins,
    # but state='active' remains in the lib/<W7> world — matching the
    # post-boot-failure state where guardrail D keeps operator intent.
    st, _ = http_get(port, "/lib-retry-test-selfkill")
    # Sanity: unload succeeded and route is gone (but state=active stays)
    test(f"{label} lib: retry — route removed by simulated unload",
         not _route_in_bin("/lib-retry-test"), "unload did not take effect")
    _, body = http_get(port, f"/lib/{W7}")
    test(f"{label} lib: retry — state stays 'active' after runtime unload",
         json.loads(body).get("state") == "active",
         f"state={json.loads(body).get('state')!r}")
    # Now: active → active should RETRY (not noop), register routes,
    # and respond with reloaded:true.
    st, body = http_method(port, f"/lib/{W7}/state", method="PUT",
                           body="active", basic_auth=approve)
    test(f"{label} lib: retry — active→active on unloaded plugin -> 200",
         st == 200, f"status={st}")
    d = json.loads(body) if st == 200 else {}
    test(f"{label} lib: retry — response flags reloaded:true",
         d.get("reloaded") is True, f"resp={d}")
    test(f"{label} lib: retry — route re-registered",
         _route_in_bin("/lib-retry-test"),
         "route did not come back after retry")
    # Plain active→active on a loaded plugin still no-op
    st, body = http_method(port, f"/lib/{W7}/state", method="PUT",
                           body="active", basic_auth=approve)
    d = json.loads(body) if st == 200 else {}
    test(f"{label} lib: retry — active→active when loaded is still noop",
         d.get("changed") is False and d.get("reloaded") is not True,
         f"resp={d}")
    http_method(port, f"/lib/{W7}", method="DELETE", token=approve)


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    print(f"elastik plugin tests — mode: {mode}")
    print(f"root: {ROOT}")

    if mode in ("all", "cgi"):
        test_cgi()
    if mode in ("all", "python"):
        if mode == "python":
            test_cgi()
        test_python()

    print(f"\n{'=' * 40}")
    print(f"  PASS: {PASS}  FAIL: {FAIL}  SKIP: {SKIP}")
    total = PASS + FAIL
    if total > 0:
        print(f"  {PASS}/{total} ({100*PASS//total}%)")
    print(f"{'=' * 40}")

    sys.exit(1 if FAIL > 0 else 0)
