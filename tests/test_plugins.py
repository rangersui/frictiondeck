"""Plugin system integration tests.

  Layer 1: CGI protocol (direct exec, no server)
  Layer 2: Python HTTP integration (server.py)

Usage:
  python tests/test_plugins.py          # all layers
  python tests/test_plugins.py cgi      # layer 1 only
  python tests/test_plugins.py python   # layer 1 + 2
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error, signal

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

    # Ensure gpu + devtools plugins are installed for testing
    import shutil
    _installed = []
    for pname in ["gpu.py", "devtools.py", "shell.py", "mirror.py", "view.py", "dav.py"]:
        src = os.path.join(ROOT, "plugins", "available", pname)
        dst = os.path.join(ROOT, "plugins", pname)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            _installed.append(dst)

    py_port = 13007
    py_token = "test-py-token"
    py_approve = "test-py-approve"
    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(py_port)
    env["ELASTIK_HOST"] = "127.0.0.1"
    env["ELASTIK_TOKEN"] = py_token
    env["ELASTIK_APPROVE_TOKEN"] = py_approve
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
        _run_http_tests(py_port, "python", token=py_token)
        _run_devtools_tests(py_port, "python", token=py_token)
        _run_flush_sse_test(py_port, "python", py_token)
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


def _run_http_tests(port, label, token=""):
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

    # No /etc/gpu.conf configured -> 503.
    # Whitespace-only body still counts as empty (handler strips), so use real body.
    st3, body3 = http_post(port, "/dev/gpu", "ping", token=token)
    # Either 503 (no conf) or 502 (conf set but backend unreachable in CI). Both acceptable.
    test(f"{label}: POST /dev/gpu (no conf) -> 503 or 502",
         st3 in (502, 503), f"status={st3} body={body3[:80]}")

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

    # 4. Verify SSE captured the show
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
