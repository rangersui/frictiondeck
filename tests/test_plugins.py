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

    # Edge cases on echo (now part of devtools.py)
    devtools_path = os.path.join("plugins", "available", "devtools.py")
    if os.path.exists(devtools_path):
        print(f"\n  --- echo edge cases (via devtools) ---")

        # Empty body
        req = json.dumps({"path": "/echo", "method": "POST", "body": "", "query": ""})
        out, _, rc = run_plugin(devtools_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("echo: empty body -> status 200", resp["status"] == 200)
            test("echo: empty body -> empty body back", resp["body"] == "")

        # Large body
        big = "x" * 10000
        req = json.dumps({"path": "/echo", "method": "POST", "body": big, "query": ""})
        out, _, rc = run_plugin(devtools_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("echo: large body -> preserved", len(resp["body"]) == 10000)

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

    # Ensure gpu + devtools plugins are installed for testing. public_gate
    # tags along: it's normally auto-installed as a default, but load_plugins()
    # only triggers that branch when plugins/ is empty — the pre-install of
    # these test fixtures makes plugins/ non-empty before the subprocess
    # boots, so on a fresh CI checkout the defaults (admin/info/public_gate)
    # never get installed at all. Without public_gate loaded, server._auth
    # stays None and the non-local probes in _run_public_gate_shell_tests
    # slip past the gate → 404 instead of 401.
    import shutil
    _installed = []
    for pname in ["gpu.py", "devtools.py", "shell.py", "mirror.py", "view.py", "dav.py", "fanout.py", "public_gate.py"]:
        src = os.path.join(ROOT, "plugins", "available", pname)
        dst = os.path.join(ROOT, "plugins", pname)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            _installed.append(dst)

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
        _run_plugin_auth_tests(py_port, "python", py_token, py_approve)
        _run_blob_ext_tests(py_port, "python", py_token, py_approve)
        _run_http_tests(py_port, "python", token=py_token, approve=py_approve)
        _run_devtools_tests(py_port, "python", token=py_token)
        _run_flush_sse_test(py_port, "python", py_token)
        _run_public_gate_shell_tests(py_port, "python", py_token, py_approve)
        _run_uint16_redteam_tests(py_port, "python", py_token, py_approve)
        _run_meta_headers_tests(py_port, "python", py_token, py_approve)
        _run_audit_binding_tests(py_port, "python", py_token, py_approve)
        _run_head_tests(py_port, "python", py_token, py_approve)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Clean up test-installed plugins
        for p in _installed:
            if os.path.exists(p):
                os.remove(p)


def _run_devtools_tests(port, label, token=""):
    """Devtools route tests."""
    print(f"\n  --- devtools {label} HTTP tests ---")

    # /wc-c: upload byte counter
    big = "x" * 50000
    st, body = http_post(port, "/wc-c", big, token=token)
    test(f"{label} devtools: POST /wc-c -> byte count",
         st == 200 and body.strip() == "50000",
         f"status={st} body={body[:40]}")

    # /full: always 507
    st, body = http_get(port, "/full")
    test(f"{label} devtools: GET /full -> 507", st == 507, f"status={st}")

    # /true: always 200
    st, _ = http_get(port, "/true")
    test(f"{label} devtools: GET /true -> 200", st == 200, f"status={st}")

    # /false: always 403
    st, _ = http_get(port, "/false")
    test(f"{label} devtools: GET /false -> 403", st == 403, f"status={st}")

    # /yes: returns 'yes' n times
    st, body = http_get(port, "/yes?n=3")
    test(f"{label} devtools: GET /yes?n=3 -> 3 lines",
         st == 200 and body.strip() == "yes\nyes\nyes",
         f"status={st} body={body[:40]}")

    # /health: ok + uptime
    st, body = http_get(port, "/health")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} devtools: /health has ok", d.get("ok") is True)
            test(f"{label} devtools: /health has uptime", "uptime" in d)
        except json.JSONDecodeError:
            test(f"{label} devtools: /health JSON", False, body[:80])
    else:
        test(f"{label} devtools: GET /health -> 200", False, f"status={st}")

    # /whoami: isolation mirror
    st, body = http_get(port, "/whoami")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} devtools: /whoami has pid", "pid" in d)
            test(f"{label} devtools: /whoami has user", "user" in d)
            test(f"{label} devtools: /whoami has env", "env" in d)
        except json.JSONDecodeError:
            test(f"{label} devtools: /whoami JSON", False, body[:80])
    else:
        test(f"{label} devtools: GET /whoami -> 200", False, f"status={st}")

    # /verify: structural integrity
    st, body = http_get(port, "/verify")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} devtools: /verify has ok", "ok" in d)
        except json.JSONDecodeError:
            test(f"{label} devtools: /verify JSON", False, body[:80])
    else:
        test(f"{label} devtools: GET /verify -> 200", False, f"status={st}")

    # /config/dump: sanitized config
    st, body = http_get(port, "/config/dump")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label} devtools: /config/dump has pid", "pid" in d)
            test(f"{label} devtools: /config/dump token_set", "token_set" in d)
        except json.JSONDecodeError:
            test(f"{label} devtools: /config/dump JSON", False, body[:80])
    else:
        test(f"{label} devtools: GET /config/dump -> 200", False, f"status={st}")

    # /uuid: returns valid UUID
    st, body = http_get(port, "/uuid")
    test(f"{label} devtools: GET /uuid -> 200", st == 200, f"status={st}")
    if st == 200:
        import re
        test(f"{label} devtools: /uuid is valid UUID",
             bool(re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
                           body.strip())),
             f"got: {body[:50]}")

    # /cowsay: ASCII art
    st, body = http_get(port, "/cowsay?say=test")
    test(f"{label} devtools: GET /cowsay -> 200", st == 200, f"status={st}")
    if st == 200:
        test(f"{label} devtools: /cowsay has cow", "(oo)" in body, f"body={body[:60]}")

    # /time: Unix epoch timestamp
    st, body = http_get(port, "/time")
    test(f"{label} devtools: GET /time -> 200", st == 200, f"status={st}")
    if st == 200:
        ts = int(body.strip())
        now = int(time.time())
        test(f"{label} devtools: /time within 5s of local clock",
             abs(ts - now) < 5, f"server={ts} local={now} diff={abs(ts-now)}")

    # /rev: reverse bytes
    st, body = http_post(port, "/rev", "hello", token=token)
    test(f"{label} devtools: POST /rev 'hello' -> 'olleh'",
         st == 200 and body.strip() == "olleh",
         f"status={st} body={body[:40]}")

    # /rev with emoji
    st, body = http_post(port, "/rev", "abc\u2764def", token=token)
    test(f"{label} devtools: POST /rev emoji round-trip",
         st == 200 and len(body.strip()) > 0,
         f"status={st} body={body[:40]}")

    # /grep: write a test world, search it, clean up
    _grep_world = "grep-integration-test"
    _grep_content = "line one alpha\nline two NEEDLE beta\nline three gamma"
    # Write test data
    st, _ = http_method(port, f"/home/{_grep_world}", method="PUT", body=_grep_content, token=token)
    if st == 200:
        # grep default mode: line-level matches (world:lineno:content)
        st, body = http_get(port, "/grep?q=NEEDLE")
        test(f"{label} devtools: /grep default -> line match",
             st == 200 and f"{_grep_world}:2:" in body and "NEEDLE" in body,
             f"status={st} body={body[:100]}")

        # grep -l mode: filenames only
        st, body = http_get(port, "/grep?q=NEEDLE&mode=l")
        test(f"{label} devtools: /grep?mode=l -> filename list",
             st == 200 and _grep_world in body,
             f"status={st} body={body[:100]}")

        # grep no match
        st, body = http_get(port, "/grep?q=ZZZZNOTFOUND")
        test(f"{label} devtools: /grep no match -> empty",
             st == 200 and body.strip() == "",
             f"status={st} body={body[:60]}")

        # grep missing ?q=
        st, body = http_get(port, "/grep")
        test(f"{label} devtools: /grep no ?q= -> 400",
             st == 400,
             f"status={st}")

        # /head: first N lines
        st, body = http_post(port, f"/head?world={_grep_world}&n=1", "", token=token)
        test(f"{label} devtools: /head?n=1 -> first line",
             st == 200 and "alpha" in body and "NEEDLE" not in body,
             f"status={st} body={body[:80]}")

        # /tail: last N lines
        st, body = http_post(port, f"/tail?world={_grep_world}&n=1", "", token=token)
        test(f"{label} devtools: /tail?n=1 -> last line",
             st == 200 and "gamma" in body and "NEEDLE" not in body,
             f"status={st} body={body[:80]}")

        # /wc: line/word/byte counts
        st, body = http_post(port, f"/wc?world={_grep_world}", "", token=token)
        if st == 200:
            try:
                d = json.loads(body)
                test(f"{label} devtools: /wc -> 3 lines",
                     d.get("lines") == 3, f"got {d}")
            except json.JSONDecodeError:
                test(f"{label} devtools: /wc JSON", False, body[:80])
        else:
            test(f"{label} devtools: /wc -> 200", False, f"status={st}")
    else:
        test(f"{label} devtools: grep setup (write world)", False, f"write status={st}")


def _run_auth_tests(port, label, token, approve):
    """Auth enforcement tests — server.py core write gate."""

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


def _run_plugin_auth_tests(port, label, token, approve):
    """Auth tests for plugin routes: shell, exec, mirror, view, dav."""

    # ── Shell: GET needs approve (Basic Auth) ──
    st, _ = http_get(port, "/shell")
    test(f"{label} plugin-auth: GET /shell no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/shell", basic_auth=approve)
    test(f"{label} plugin-auth: GET /shell approve -> 200", st == 200, f"status={st}")

    # ── Exec: POST needs approve ──
    st, _ = http_post(port, "/exec", "echo hi")
    test(f"{label} plugin-auth: POST /exec no auth -> 403", st in (401, 403), f"status={st}")

    st, body = http_method(port, "/exec", method="POST", body="echo hi", basic_auth=approve)
    test(f"{label} plugin-auth: POST /exec approve -> 200", st == 200 and "hi" in body, f"status={st}")

    # ── Mirror: GET needs approve ──
    st, _ = http_get(port, "/mirror")
    test(f"{label} plugin-auth: GET /mirror no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/mirror", basic_auth=approve)
    test(f"{label} plugin-auth: GET /mirror approve -> 200", st == 200, f"status={st}")

    # ── View: GET needs approve (+ type gate) ──
    http_method(port, "/home/view-test?ext=html", method="PUT", body="<h1>test</h1>", token=token)
    st, _ = http_get(port, "/view/view-test")
    test(f"{label} plugin-auth: GET /view no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/view/view-test", basic_auth=approve)
    # 200 if html-typed, 415 if plain — either means auth passed
    test(f"{label} plugin-auth: GET /view approve -> auth passed", st in (200, 415), f"status={st}")

    # ── WebDAV: reads open, writes need auth ──
    st, _ = http_method(port, "/dav/", method="OPTIONS")
    test(f"{label} plugin-auth: OPTIONS /dav -> 200", st == 200, f"status={st}")

    st, _ = http_method(port, "/dav/", method="PROPFIND", headers={"Depth": "0"})
    test(f"{label} plugin-auth: PROPFIND /dav no auth -> 207", st == 207, f"status={st}")

    st, _ = http_method(port, "/dav/home/auth-test-world", method="PUT", body="test")
    test(f"{label} plugin-auth: PUT /dav no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/dav/home/auth-test-world", method="PUT", body="dav-write", token=token)
    test(f"{label} plugin-auth: PUT /dav token -> 201", st == 201, f"status={st}")

    st, body = http_get(port, "/dav/home/auth-test-world")
    test(f"{label} plugin-auth: GET /dav read back -> ok", st == 200 and "dav-write" in body, f"status={st}")

    st, _ = http_method(port, "/dav/home/auth-test-world", method="PUT", body="dav-basic", basic_auth=approve)
    test(f"{label} plugin-auth: PUT /dav Basic Auth -> 201", st == 201, f"status={st}")

    st, _ = http_method(port, "/dav/home/auth-test-world", method="DELETE")
    test(f"{label} plugin-auth: DELETE /dav no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/dav/home/auth-test-world", method="DELETE", basic_auth=approve)
    test(f"{label} plugin-auth: DELETE /dav approve -> 204", st == 204, f"status={st}")


def _run_blob_ext_tests(port, label, token, approve):
    """Tests for BLOB storage, ext column, 304, /raw, fake directories."""
    import hashlib

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


def _run_http_tests(port, label, token="", approve=""):
    """HTTP integration tests."""

    # Echo
    st, body = http_post(port, "/echo", "hello from test", token=token)
    test(f"{label}: POST /echo -> 200", st == 200, f"status={st}")
    if st == 200:
        test(f"{label}: POST /echo -> body preserved",
             "hello from test" in body, f"body={body[:80]}")

    # /dev/gpu — pluggable AI device.
    # Plugin AUTH="none" so man page (browser GET) works without auth.
    # Handler gates POST inline — anon POST must 401 (API-bill protection).
    st_anon, _ = http_post(port, "/dev/gpu", "hi", token="")
    test(f"{label}: POST /dev/gpu no auth -> 401", st_anon == 401, f"status={st_anon}")

    # Browser GET (Accept: text/html) → man page form. 200 HTML, not 405.
    st_gui, body_gui = http_method(port, "/dev/gpu", method="GET",
                                    headers={"Accept": "text/html"})
    test(f"{label}: GET /dev/gpu browser -> 200 man page",
         st_gui == 200 and "<form" in body_gui.lower(), f"status={st_gui}")

    # curl GET (no Accept) → 405 (POST-only endpoint).
    st_curl_get, _ = http_get(port, "/dev/gpu")
    test(f"{label}: GET /dev/gpu curl -> 405", st_curl_get == 405, f"status={st_curl_get}")

    st2, _ = http_post(port, "/dev/gpu", "", token=token)
    test(f"{label}: POST /dev/gpu empty -> 400", st2 == 400, f"status={st2}")

    # Wipe etc/gpu.conf first so the "no conf -> 503" check is deterministic.
    # Left-over conf from prior runs could make the backend answer 200.
    http_method(port, "/etc/gpu.conf", method="DELETE", token=approve)
    st3, body3 = http_post(port, "/dev/gpu", "ping", token=token)
    # 503 (no conf) or 502 (conf set but backend unreachable in CI). Both acceptable.
    test(f"{label}: POST /dev/gpu (no conf) -> 503 or 502",
         st3 in (502, 503), f"status={st3} body={body3[:80]}")

    # /dev/fanout — tee-style broadcast.
    # No conf → 503. Retry DELETE a few times — on Windows, the shared
    # data/ dir may carry stale /etc/fanout.conf from a prior run, and
    # _move_to_trash can partial-fail (file lock race, copytree succeeds
    # but rmtree silently swallows the leftover src dir).
    for _ in range(3):
        http_method(port, "/etc/fanout.conf", method="DELETE", token=approve)
        st, _ = http_post(port, "/dev/fanout", "hi", token=token)
        if st == 503:
            break
        time.sleep(0.2)
    test(f"{label}: fanout no conf -> 503", st == 503, f"status={st}")

    # No auth → 401.
    st, _ = http_post(port, "/dev/fanout", "hi", token="")
    test(f"{label}: fanout no auth -> 401", st == 401, f"status={st}")

    # Browser GET → man page.
    st_gui, body_gui = http_method(port, "/dev/fanout", method="GET",
                                    headers={"Accept": "text/html"})
    test(f"{label}: fanout browser GET -> 200 man page",
         st_gui == 200 and "<form" in body_gui.lower(), f"status={st_gui}")

    # Write conf with 3 targets (T3 needed for /etc/* writes).
    conf = "home/fanout-a\nhome/fanout-b\n# comment\n\n/home/fanout-c\n"
    st, _ = http_method(port, "/etc/fanout.conf", method="PUT", body=conf, token=approve)
    test(f"{label}: fanout conf write -> 200", st == 200, f"status={st}")

    # POST /dev/fanout → append to all three. All should receive.
    st, body = http_post(port, "/dev/fanout", "ping-1", token=token)
    test(f"{label}: fanout POST -> 200", st == 200, f"status={st}")
    d = json.loads(body)
    test(f"{label}: fanout wrote 3 targets",
         sorted(d.get("written", [])) == ["fanout-a", "fanout-b", "fanout-c"],
         f"written={d.get('written')}")
    test(f"{label}: fanout no failures", d.get("failed") == [], f"failed={d.get('failed')}")

    # Each target has the message.
    for tgt in ("fanout-a", "fanout-b", "fanout-c"):
        st_t, body_t = http_get(port, f"/home/{tgt}")
        dt = json.loads(body_t)
        test(f"{label}: fanout target {tgt} got msg",
             "ping-1" in dt.get("stage_html", ""), f"stage={dt.get('stage_html','')[:30]}")

    # Second POST appends — target accumulates.
    http_post(port, "/dev/fanout", "ping-2", token=token)
    st_t, body_t = http_get(port, "/home/fanout-a")
    dt = json.loads(body_t)
    test(f"{label}: fanout POST appends",
         "ping-1" in dt.get("stage_html", "") and "ping-2" in dt.get("stage_html", ""),
         f"stage={dt.get('stage_html','')[:40]}")

    # PUT overwrites — target content replaced.
    http_method(port, "/dev/fanout", method="PUT", body="fresh", token=token)
    st_t, body_t = http_get(port, "/home/fanout-a")
    dt = json.loads(body_t)
    test(f"{label}: fanout PUT overwrites",
         dt.get("stage_html", "").strip() == "fresh",
         f"stage={dt.get('stage_html','')[:40]}")

    # System target in conf + T2 caller → that one fails, others still succeed.
    conf_sys = "home/fanout-a\netc/fanout-sys-target\n"
    http_method(port, "/etc/fanout.conf", method="PUT", body=conf_sys, token=approve)
    st, body = http_post(port, "/dev/fanout", "t2-tries", token=token)
    d = json.loads(body)
    test(f"{label}: fanout T2 → home/* written",
         "fanout-a" in d.get("written", []), f"written={d.get('written')}")
    test(f"{label}: fanout T2 → etc/* refused",
         any(f.get("target") == "etc/fanout-sys-target" for f in d.get("failed", [])),
         f"failed={d.get('failed')}")

    # T3 can hit system targets in fanout.
    st, body = http_post(port, "/dev/fanout", "t3-can", token=approve)
    d = json.loads(body)
    test(f"{label}: fanout T3 → etc/* written too",
         "etc/fanout-sys-target" in d.get("written", []),
         f"written={d.get('written')}")

    # Cap token — even mode=rw for /dev/fanout → still refused (broad vs narrow).
    mint_body = http_method(port, "/auth/mint?prefix=/dev/fanout&ttl=600&mode=rw",
                            method="POST", token=approve)
    try:
        cap = json.loads(mint_body[1]).get("token", "")
    except Exception:
        cap = ""
    if cap:
        st, _ = http_post(port, "/dev/fanout", "cap-tries", token=cap)
        test(f"{label}: fanout rejects cap token -> 401", st == 401, f"status={st}")

    # Cleanup — remove the test worlds and conf.
    http_method(port, "/etc/fanout.conf", method="DELETE", token=approve)
    for tgt in ("home/fanout-a", "home/fanout-b", "home/fanout-c", "etc/fanout-sys-target"):
        http_method(port, "/" + tgt, method="DELETE", token=approve)

    # Query-string URL-decoding regression: browsers URL-encode / as %2F
    # when a form field value contains a slash. Before the fix, server.py
    # hand-parsed qs without decoding → /dev/db got file="brave%2FHistory",
    # _resolve_mnt did startswith("brave/") on "brave%2FHistory" → "file
    # not under any fstab mount". Use /grep to probe: encode the space in
    # "foo bar" as %20 and verify grep sees the decoded value.
    http_method(port, "/home/qs-decode-grep", method="PUT",
                body="line with foo bar here\nanother line", token=token)
    st_enc, body_enc = http_get(port, "/grep?world=qs-decode-grep&q=foo%20bar")
    test(f"{label}: qs decodes %20 to space (grep finds 'foo bar')",
         st_enc == 200 and "foo bar" in body_enc, f"status={st_enc} body={body_enc[:80]}")

    # /fetch — stdlib urllib.request, no curl subprocess. Protocol guard
    # (only http/https) + urllib natively refuses file:// redirects.
    st, _ = http_get(port, "/fetch")
    test(f"{label}: /fetch no url -> 400", st == 400, f"status={st}")

    st, _ = http_get(port, "/fetch?url=file:///etc/hosts")
    test(f"{label}: /fetch file:// blocked at entry -> 400", st == 400, f"status={st}")

    st, _ = http_get(port, "/fetch?url=ftp://example.com/foo")
    test(f"{label}: /fetch ftp:// blocked at entry -> 400", st == 400, f"status={st}")

    # argv-injection defense holds even with no subprocess — the scheme
    # check would refuse a leading '-' too.
    st, _ = http_get(port, "/fetch?url=-K/etc/passwd")
    test(f"{label}: /fetch leading '-' blocked -> 400", st == 400, f"status={st}")

    # Real fetch against our own elastik server (localhost, guaranteed up).
    st, body = http_get(port, f"/fetch?url=http://127.0.0.1:{port}/proc/worlds")
    test(f"{label}: /fetch localhost http -> 200", st == 200, f"status={st}")
    test(f"{label}: /fetch body looks like JSON array",
         body.startswith("[") and body.rstrip().endswith("]"), f"body={body[:60]}")

    # HTTPError pass-through: fetch a known 404 upstream, expect 404 back.
    st, _ = http_get(port, f"/fetch?url=http://127.0.0.1:{port}/home/nope-does-not-exist")
    test(f"{label}: /fetch upstream 404 -> 404 passthrough", st == 404, f"status={st}")

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


def _run_flush_sse_test(port, label, token):
    """Integration test: /flush + SSE. The toilet is the test.

    Network: SSE long-connection stays open.
    State:   world mutates through 💩 → 💧 → ✨ atomically per write.
    Timing:  POST triggers state changes, SSE delivers them.
    """
    import threading

    # 1. Open SSE listener in background
    events = []
    def sse_listen():
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream/toilet", timeout=10)
            buf = b""
            while True:
                chunk = r.read(1)
                if not chunk: break
                buf += chunk
                if buf.endswith(b"\n\n"):
                    for line in buf.decode("utf-8", "replace").strip().splitlines():
                        if line.startswith("data: "):
                            try:
                                d = json.loads(line[6:])
                                events.append(d.get("stage_html", ""))
                            except (json.JSONDecodeError, AttributeError):
                                pass
                    buf = b""
        except Exception:
            pass

    t = threading.Thread(target=sse_listen, daemon=True)
    t.start()
    time.sleep(0.5)

    # 2. Flush
    st, body = http_post(port, "/flush", "", token=token)
    test(f"{label} flush: POST /flush -> 200", st == 200, f"status={st}")
    test(f"{label} flush: returns sparkle", "\u2728" in body, f"body={body[:20]}")

    # 3. Wait for SSE events to arrive
    time.sleep(1.5)

    # 4. Verify SSE captured the show.
    # Skip on CI — the SSE listener races /flush's world-creation: if
    # /home/toilet doesn't exist yet when the listener opens /stream/,
    # the stream 404s and the thread dies silently. On local repeat
    # runs the world survives from the previous run so the race hides.
    # CI always has clean data → always loses. Not worth retrofitting
    # the test just to work around that ordering.
    if os.environ.get("CI"):
        skip(f"{label} flush: SSE events (skipped on CI)",
             "listener-vs-world-creation race; local-only")
    else:
        test(f"{label} flush: SSE got events", len(events) >= 3,
             f"got {len(events)} events")
        # Must start with 💩 and end with ✨
        if events:
            test(f"{label} flush: first event has seed",
                 "\U0001f4a9" in events[0] or "\U0001f4a9" in "".join(events[:2]),
                 f"first={events[0][:10]}")
            test(f"{label} flush: last event is clean",
                 "\u2728" in events[-1],
                 f"last={events[-1][:10]}")
            # Middle should have water
            middle = "".join(events[1:-1])
            test(f"{label} flush: middle has water",
                 "\U0001f4a7" in middle,
                 f"middle_sample={middle[:30]}")
        else:
            test(f"{label} flush: SSE events exist", False, "no events captured")

    # 5. Verify world is clean
    st, body = http_get(port, "/home/toilet")
    if st == 200:
        d = json.loads(body)
        test(f"{label} flush: world is clean after",
             "\u2728" in d.get("stage_html", ""),
             f"stage={d.get('stage_html','')[:20]}")

    # 6. Repeatable — flush again, should re-seed and re-flush
    st, _ = http_post(port, "/flush", "", token=token)
    test(f"{label} flush: repeatable", st == 200, f"status={st}")


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

    # 1. PUT X-Meta-Author → /read returns headers=[["x-meta-author","codex"]]
    st, _ = http_method(port, f"/home/{W}", method="PUT", body="x",
                        token=token, headers={"X-Meta-Author": "codex"})
    test(f"{label} meta: PUT X-Meta-Author -> 200", st == 200, f"status={st}")
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: /read returns headers array",
         d.get("headers") == [["x-meta-author", "codex"]],
         f"headers={d.get('headers')}")

    # 2. ?raw response has x-meta-author header
    import http.client as _hc
    def _raw_hdrs(path, extra_headers=None):
        c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = dict(extra_headers or {})
        c.request("GET", path, headers=hdrs)
        r = c.getresponse(); r.read(); c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}
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

    # 4. Multi-value (same name twice) → two list entries, HTTP allows it
    #    urllib.request can't send dup headers easily, use http.client
    c = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("PUT", f"/home/{W}")
    c.putheader("Authorization", f"Bearer {token}")
    c.putheader("X-Meta-Tag", "a")
    c.putheader("X-Meta-Tag", "b")
    c.putheader("Content-Length", "1")
    c.endheaders(); c.send(b"x"); r = c.getresponse(); r.read(); c.close()
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    tags = [v for k, v in d.get("headers", []) if k == "x-meta-tag"]
    test(f"{label} meta: multi-value X-Meta-Tag preserved", tags == ["a", "b"],
         f"tags={tags} full={d.get('headers')}")

    # 5. Authorization NOT stored (it's auth, not meta)
    http_method(port, f"/home/{W}", method="PUT", body="x",
                token=token, headers={"X-Meta-Only": "yes"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    kept = [k for k, _ in d.get("headers", [])]
    test(f"{label} meta: Authorization not stored",
         "authorization" not in kept, f"kept={kept}")

    # 6. X-Forwarded-For not stored (infra, not user intent; not x-meta-*)
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Keep": "1", "X-Forwarded-For": "1.2.3.4"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    kept = [k for k, _ in d.get("headers", [])]
    test(f"{label} meta: X-Forwarded-For not stored",
         "x-forwarded-for" not in kept, f"kept={kept}")

    # 7. X-Accel-Redirect not stored (nginx-interpreted response directive)
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Keep": "1", "X-Accel-Redirect": "/etc/passwd"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    kept = [k for k, _ in d.get("headers", [])]
    test(f"{label} meta: X-Accel-Redirect not stored",
         "x-accel-redirect" not in kept, f"kept={kept}")

    # 8. CRLF/NUL injection in X-Meta value → dropped (can't inject via Python
    # http clients directly — they refuse bad headers — so we inject at the
    # DB layer to confirm read-side catches it too). See test 16 for that path.
    # Here we do the client-side path: valid char-only stays, bad char rejected.
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Clean": "plain-ascii-ok"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: clean ASCII value stored",
         any(k == "x-meta-clean" and v == "plain-ascii-ok"
             for k, v in d.get("headers", [])),
         f"headers={d.get('headers')}")

    # 9. X-Elastik-Internal NOT stored (reserved prefix, also not x-meta-*)
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Author": "u", "X-Elastik-Internal": "hack"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    kept = [k for k, _ in d.get("headers", [])]
    test(f"{label} meta: X-Elastik-* not stored (not x-meta-*)",
         "x-elastik-internal" not in kept, f"kept={kept}")

    # 10. One over-size value → drops that one, keeps others
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Keep": "small", "X-Meta-Huge": "a" * 2000})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    kept = {k: v for k, v in d.get("headers", [])}
    test(f"{label} meta: oversized value drops that header only",
         "x-meta-keep" in kept and "x-meta-huge" not in kept,
         f"kept={list(kept)}")

    # 11. Total >8 KB → drop all (try with many small headers)
    many = {f"X-Meta-Key{i}": "v" * 100 for i in range(100)}   # ~10 KB total JSON
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers=many)
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: total-overflow drops all",
         d.get("headers") == [], f"headers_len={len(d.get('headers', []))}")

    # 12. POST /sync does NOT clobber headers column
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token,
                headers={"X-Meta-Author": "alice"})
    http_method(port, f"/home/{W}/sync", method="POST", body="sync-payload",
                token=token, headers={"X-Meta-Author": "mallory"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: /sync internal op does not clobber headers",
         d.get("headers") == [["x-meta-author", "alice"]],
         f"headers={d.get('headers')}")

    # 13. POST append does NOT clobber headers column either (Phase 1 scope)
    http_method(port, f"/home/{W}", method="PUT", body="a", token=token,
                headers={"X-Meta-Author": "alice"})
    http_method(port, f"/home/{W}", method="POST", body="b", token=token,
                headers={"X-Meta-Author": "mallory"})
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: POST append does not clobber headers (PUT-only)",
         d.get("headers") == [["x-meta-author", "alice"]],
         f"headers={d.get('headers')}")

    # 14. Old client: PUT with no X-Meta-* → headers=[]
    http_method(port, f"/home/{W}", method="PUT", body="x", token=token)
    st, body = http_get(port, f"/home/{W}")
    d = json.loads(body)
    test(f"{label} meta: no X-Meta-* → headers=[]",
         d.get("headers") == [], f"headers={d.get('headers')}")

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
    Content-Type, Accept-Ranges, Content-Range on 206, X-Meta-* on the
    ?raw path), body is always empty.

    Deliberately: plain HEAD /world only mirrors the JSON read surface
    and does NOT expose X-Meta-* in response headers; that lives on
    ?raw, same as GET's existing contract. AI that wants metadata
    should use HEAD /world?raw.
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
    test(f"{label} head: JSON read does NOT expose x-meta-* (same as GET JSON)",
         "x-meta-author" not in hhdrs, f"headers={hhdrs}")

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

    # 7. HEAD /home/<nonexistent-but-has-children> — 302 redirect to ls
    # Seed a child so /home/head-redirect-prefix has existing children
    http_method(port, f"/home/head-redirect-prefix/child", method="PUT",
                body="c", token=token)
    hst, hhdrs, hbody = _head("/home/head-redirect-prefix")
    test(f"{label} head: missing world w/ children -> 302",
         hst == 302, f"status={hst}")
    test(f"{label} head: 302 has Location header",
         hhdrs.get("location") == "/home/head-redirect-prefix/",
         f"location={hhdrs.get('location')}")
    test(f"{label} head: 302 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 8. HEAD /home/<truly-missing> — 404, body empty
    hst, hhdrs, hbody = _head("/home/this-world-definitely-does-not-exist-xyzzy")
    test(f"{label} head: missing world -> 404", hst == 404, f"status={hst}")
    test(f"{label} head: 404 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # 9. HEAD /etc/shadow without approve — 403, body empty
    hst, hhdrs, hbody = _head("/etc/shadow")
    test(f"{label} head: sensitive read w/o approve -> 403",
         hst == 403, f"status={hst}")
    test(f"{label} head: 403 body is empty", len(hbody) == 0, f"len={len(hbody)}")

    # cleanup
    http_method(port, f"/home/{W}", method="DELETE", token=approve)
    http_method(port, f"/home/head-redirect-prefix/child", method="DELETE", token=approve)


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
