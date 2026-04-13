"""Plugin system integration tests.

Tests all combinations:
  Layer 1: CGI protocol (direct exec, no server)
  Layer 2: Go HTTP integration (elastik-lite)
  Layer 3: Python HTTP integration (boot.py)
  Layer 4: Cross-runtime parity

Usage:
  python tests/test_plugins.py          # all layers
  python tests/test_plugins.py cgi      # layer 1 only
  python tests/test_plugins.py go       # layer 1 + 2
  python tests/test_plugins.py python   # layer 1 + 3
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error, signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

EXE_NAME = "elastik.exe" if sys.platform == "win32" else "elastik"

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
            urllib.request.urlopen(f"http://127.0.0.1:{port}/stages", timeout=1)
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

    # Edge cases on ai
    ai_path = os.path.join("plugins", "available", "ai.py")
    if os.path.exists(ai_path):
        print(f"\n  --- ai edge cases ---")

        # /ai/ask with GET -> 405
        req = json.dumps({"path": "/ai/ask", "method": "GET", "body": "", "query": ""})
        out, _, rc = run_plugin(ai_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("ai: GET /ai/ask -> 405", resp["status"] == 405)

        # /ai/ask with empty body -> 400
        req = json.dumps({"path": "/ai/ask", "method": "POST", "body": "", "query": ""})
        out, _, rc = run_plugin(ai_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("ai: POST /ai/ask empty -> 400", resp["status"] == 400)

        # /ai/ask whitespace body -> 400
        req = json.dumps({"path": "/ai/ask", "method": "POST", "body": "   ", "query": ""})
        out, _, rc = run_plugin(ai_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("ai: POST /ai/ask whitespace -> 400", resp["status"] == 400)

        # unknown route -> 404
        req = json.dumps({"path": "/ai/nonexistent", "method": "GET", "body": "", "query": ""})
        out, _, rc = run_plugin(ai_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("ai: unknown route -> 404", resp["status"] == 404)

        # /ai/status -> has provider, model, status fields
        req = json.dumps({"path": "/ai/status", "method": "GET", "body": "", "query": ""})
        out, _, rc = run_plugin(ai_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            body = json.loads(resp["body"])
            test("ai: /ai/status has provider", "provider" in body)
            test("ai: /ai/status has model", "model" in body)
            test("ai: /ai/status has status", "status" in body)


# ── Layer 2: Go HTTP Integration ────────────────────────────────────

def test_go():
    print("\n=== Layer 2: Go HTTP Integration ===")

    go_port = 13006
    exe = os.path.join(ROOT, EXE_NAME)
    if not os.path.exists(exe):
        # Try building
        print(f"  building {EXE_NAME}...")
        rc = subprocess.run(
            ["go", "build", "-o", exe, "."],
            cwd=os.path.join(ROOT, "go", "native"),
            capture_output=True
        ).returncode
        if rc != 0:
            skip("Go HTTP tests", "build failed")
            return

    # Install adversarial plugins for Go to discover
    import shutil
    adv_dir = os.path.join(ROOT, "tests", "adversarial")
    _adv_installed = []
    if os.path.isdir(adv_dir):
        for f in os.listdir(adv_dir):
            if f.endswith(".py"):
                src = os.path.join(adv_dir, f)
                dst = os.path.join(ROOT, "plugins", f)
                shutil.copy2(src, dst)
                _adv_installed.append(dst)

    go_token = "test-go-token"
    go_approve = "test-go-approve"
    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(go_port)
    env["ELASTIK_HOST"] = "127.0.0.1"
    env["ELASTIK_TOKEN"] = go_token  # override .env
    env["ELASTIK_APPROVE_TOKEN"] = go_approve
    # Use DEVNULL for stdout — if piped, Go blocks when the 65KB buffer
    # fills during 30s+ adversarial tests (classic pipe deadlock).
    proc = subprocess.Popen(
        [exe], env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    try:
        if not wait_for_server(go_port, timeout=20):
            test("Go server starts", False, "timeout waiting for server")
            return
        # Wait for plugin scanning to complete (devtools has 20 routes)
        time.sleep(3)
        test("Go server starts", True)

        _run_auth_tests(go_port, "go", go_token, go_approve)
        _run_http_tests(go_port, "go", token=go_token)
        _run_adversarial_tests(go_port, go_token)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Kill any orphan forkbomb children (they sleep 300s on Windows)
        if sys.platform == "win32":
            subprocess.run(
                'wmic process where "name=\'python.exe\' and commandline like \'%%--child%%\'" call terminate',
                shell=True, capture_output=True
            )
        # Clean up adversarial plugins
        for p in _adv_installed:
            if os.path.exists(p):
                os.remove(p)


def _run_adversarial_tests(port, token):
    """Adversarial tests — prove Go daemon survives malicious plugins."""
    print(f"\n  --- adversarial Go HTTP tests ---")

    # 1. Truncated JSON: plugin dies mid-output → Go returns 502
    st, body = http_get(port, "/truncated")
    test("adversarial: truncated JSON -> 502", st == 502,
         f"status={st} body={body[:80]}")

    # 2. Infinite output: Go kills at 5MB → 502
    st, body = http_get(port, "/infinite")
    test("adversarial: infinite output -> 413", st == 413,
         f"status={st} body={body[:80]}")

    # Server should still be alive after adversarial attacks
    st, _ = http_get(port, "/stages")
    test("adversarial: server alive after attacks", st == 200,
         f"status={st}")

    # 3. Stateless: every call returns "1" (fresh process each time)
    for i in range(3):
        st, body = http_get(port, "/stateless")
        test(f"adversarial: stateless call {i+1} -> '1'",
             st == 200 and body.strip() == "1",
             f"status={st} body={body[:40]}")

    # 4. Self-reader: plugin reads its own source
    st, body = http_get(port, "/selfreader")
    test("adversarial: selfreader -> 200", st == 200, f"status={st}")
    if st == 200:
        test("adversarial: selfreader contains __file__",
             "__file__" in body, f"body len={len(body)}")

    # 5. Cthulhu: binary garbage on stdout → Go handles gracefully
    st, body = http_get(port, "/cthulhu")
    # Go's json.Unmarshal fails on binary → raw text fallback (200)
    # or process exit error (502). Either is correct.
    test("adversarial: cthulhu -> no crash (200 or 502)",
         st in (200, 502), f"status={st}")

    # Server alive after Cthulhu
    st, _ = http_get(port, "/stages")
    test("adversarial: server alive after cthulhu", st == 200,
         f"status={st}")

    # ── devtools route tests ──
    _run_devtools_tests(port, "go", token=token)

    # ── Slow adversarial tests (30s+ each) ──
    # These go LAST because forceful process kills can destabilize Go on Windows.
    print(f"\n  --- slow adversarial tests (30s+ each) ---")

    # Slow drip: exceeds 30s timeout → Go returns 504
    print("    (slowdrip: waiting for 30s timeout...)")
    st, body = http_get(port, "/slowdrip", timeout=45)
    test("adversarial: slowdrip -> 504 (timeout)", st == 504,
         f"status={st} body={body[:80]}")

    # Server still alive after timeout kill
    st, _ = http_get(port, "/stages")
    test("adversarial: server alive after slowdrip", st == 200,
         f"status={st}")

    # Fork bomb: spawns children, parent sleeps forever → Go kills parent
    print("    (forkbomb: waiting for 30s timeout...)")
    st, body = http_get(port, "/forkbomb", timeout=45)
    test("adversarial: forkbomb -> killed (502/504)", st in (502, 504),
         f"status={st} body={body[:80]}")

    # Server still alive after fork bomb
    st, _ = http_get(port, "/stages")
    test("adversarial: server alive after forkbomb", st == 200,
         f"status={st}")

    # Terminator: traps SIGTERM, refuses to die → Go must use SIGKILL
    print("    (terminator: waiting for 30s timeout...)")
    st, body = http_get(port, "/terminator", timeout=45)
    test("adversarial: terminator -> killed (502/504)", st in (502, 504),
         f"status={st} body={body[:80]}")

    # Server alive after terminator
    st, _ = http_get(port, "/stages")
    test("adversarial: server alive after terminator", st == 200,
         f"status={st}")


# ── Layer 3: Python HTTP Integration ────────────────────────────────

def test_python():
    print("\n=== Layer 3: Python HTTP Integration ===")

    # Ensure ai + devtools plugins are installed for testing
    import shutil
    _installed = []
    for pname in ["ai.py", "devtools.py"]:
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
        [sys.executable, "boot.py"], env=env, cwd=ROOT,
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
    """Devtools route tests — shared by Go and Python servers."""
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
    st, _ = http_post(port, f"/{_grep_world}/write", _grep_content, token=token)
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
    """Auth enforcement tests — shared by Go and Python."""

    # ── Tier 2: Bearer AUTH_TOKEN ──

    # GET always open (no token needed)
    st, _ = http_get(port, "/stages")
    test(f"{label} auth: GET /stages open", st == 200, f"status={st}")

    # POST without token -> 403
    st, _ = http_post(port, "/echo", "x")
    test(f"{label} auth: POST /echo no token -> 403", st == 403, f"status={st}")

    # POST with wrong token -> 403
    st, _ = http_post(port, "/echo", "x", token="wrong-token")
    test(f"{label} auth: POST /echo wrong token -> 403", st == 403, f"status={st}")

    # POST with correct token -> 200
    st, _ = http_post(port, "/echo", "x", token=token)
    test(f"{label} auth: POST /echo correct token -> 200", st == 200, f"status={st}")

    # ── Tier 1: Bearer APPROVE_TOKEN for config-* worlds ──

    # POST config-* with auth token only -> 403
    st, _ = http_post(port, "/config-test/write", "data", token=token)
    test(f"{label} auth: POST config-*/write auth-only -> 403", st == 403, f"status={st}")

    # POST config-* with wrong approve -> 403
    st, _ = http_post(port, "/config-test/write", "data", approve="wrong")
    test(f"{label} auth: POST config-*/write wrong approve -> 403", st == 403, f"status={st}")

    # POST config-* with correct approve -> pass auth (200 or creates world)
    st, _ = http_post(port, "/config-test/write", "test-data", approve=approve)
    test(f"{label} auth: POST config-*/write approve -> pass", st in (200, 201), f"status={st}")

    # Verify config-* world was actually written
    st, body = http_get(port, "/config-test/read")
    test(f"{label} auth: GET config-*/read -> data persisted",
         st == 200 and "test-data" in body, f"status={st} body={body[:60]}")

    # ── Plugin reload (Go-only feature) ──
    if label == "go":
        st, _ = http_post(port, "/plugins/reload")
        test(f"{label} auth: POST /plugins/reload no token -> 403", st == 403, f"status={st}")

        st, _ = http_post(port, "/plugins/reload", token=token)
        test(f"{label} auth: POST /plugins/reload auth -> 200", st == 200, f"status={st}")

    # ── Tier 1: /proxy/* requires approve token (postman = curl proxy) ──

    # POST /proxy/postman with no token -> 403
    st, _ = http_post(port, "/proxy/postman", '{"url":"https://httpbin.org/get"}')
    test(f"{label} auth: POST /proxy/postman no token -> 403", st == 403, f"status={st}")

    # POST /proxy/postman with auth token only -> 403 (needs approve, not auth)
    st, _ = http_post(port, "/proxy/postman", '{"url":"https://httpbin.org/get"}', token=token)
    test(f"{label} auth: POST /proxy/postman auth-only -> 403", st == 403, f"status={st}")

    # POST /proxy/postman with wrong approve -> 403
    st, _ = http_post(port, "/proxy/postman", '{"url":"https://httpbin.org/get"}', approve="wrong")
    test(f"{label} auth: POST /proxy/postman wrong approve -> 403", st == 403, f"status={st}")

    # POST /proxy/postman with correct approve -> passes auth (not 403)
    # Go returns 404 (route not registered — Python-only plugin), Python returns 200
    st, _ = http_post(port, "/proxy/postman", '{"url":"https://httpbin.org/get"}', approve=approve)
    test(f"{label} auth: POST /proxy/postman approve -> not 403", st != 403, f"status={st}")

    # ── Normal world with auth token should work ──

    st, _ = http_post(port, "/authtest/write", "hello", token=token)
    test(f"{label} auth: POST normal world write -> pass", st in (200, 201), f"status={st}")

    st, body = http_get(port, "/authtest/read")
    test(f"{label} auth: GET normal world read -> data", st == 200 and "hello" in body,
         f"status={st} body={body[:60]}")


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
    st, _ = http_get(port, "/view/work")
    test(f"{label} plugin-auth: GET /view no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/view/work", basic_auth=approve)
    # 200 if html-typed, 415 if plain — either means auth passed
    test(f"{label} plugin-auth: GET /view approve -> auth passed", st in (200, 415), f"status={st}")

    # ── WebDAV: reads open, writes need auth ──
    st, _ = http_method(port, "/dav/", method="OPTIONS")
    test(f"{label} plugin-auth: OPTIONS /dav -> 200", st == 200, f"status={st}")

    st, _ = http_method(port, "/dav/", method="PROPFIND", headers={"Depth": "0"})
    test(f"{label} plugin-auth: PROPFIND /dav no auth -> 207", st == 207, f"status={st}")

    st, _ = http_method(port, "/dav/auth-test-world", method="PUT", body="test")
    test(f"{label} plugin-auth: PUT /dav no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/dav/auth-test-world", method="PUT", body="dav-write", token=token)
    test(f"{label} plugin-auth: PUT /dav token -> 201", st == 201, f"status={st}")

    st, body = http_get(port, "/dav/auth-test-world")
    test(f"{label} plugin-auth: GET /dav read back -> ok", st == 200 and "dav-write" in body, f"status={st}")

    st, _ = http_method(port, "/dav/auth-test-world", method="PUT", body="dav-basic", basic_auth=approve)
    test(f"{label} plugin-auth: PUT /dav Basic Auth -> 201", st == 201, f"status={st}")

    st, _ = http_method(port, "/dav/auth-test-world", method="DELETE")
    test(f"{label} plugin-auth: DELETE /dav no auth -> 401", st == 401, f"status={st}")

    st, _ = http_method(port, "/dav/auth-test-world", method="DELETE", basic_auth=approve)
    test(f"{label} plugin-auth: DELETE /dav approve -> 204", st == 204, f"status={st}")


def _run_blob_ext_tests(port, label, token, approve):
    """Tests for BLOB storage, ext column, 304, /raw, fake directories."""
    import hashlib

    # ── 304 version comparison ──
    st, body = http_get(port, "/work/read")
    test(f"{label} blob: GET /read -> 200", st == 200, f"status={st}")
    v = json.loads(body).get("version", -1)

    st, _ = http_method(port, f"/work/read?v={v}")
    test(f"{label} blob: GET /read?v=current -> 304", st == 304, f"status={st}")

    st, _ = http_method(port, "/work/read?v=0")
    test(f"{label} blob: GET /read?v=0 -> 200", st == 200, f"status={st}")

    # ── ext column ──
    st, body = http_post(port, "/ext-blob-test/write?ext=css", "body{color:red}", token=token)
    test(f"{label} blob: write ?ext=css -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/ext-blob-test/read")
    d = json.loads(body)
    test(f"{label} blob: read ext=css", d.get("ext") == "css", f"ext={d.get('ext')}")
    test(f"{label} blob: read content intact", "color:red" in d.get("stage_html", ""), f"body={d.get('stage_html','')[:30]}")

    # ── /raw route ──
    st, body = http_get(port, "/ext-blob-test/raw")
    test(f"{label} blob: /raw -> 200", st == 200, f"status={st}")
    test(f"{label} blob: /raw content", "color:red" in body, f"body={body[:30]}")

    # ── Binary write via /write?ext=png ──
    png_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")
    png_bytes = open(png_path, "rb").read()
    st, _ = http_method(port, "/blob-bin-test/write?ext=png", method="POST", body=png_bytes, token=token)
    test(f"{label} blob: binary write -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/blob-bin-test/read")
    d = json.loads(body)
    test(f"{label} blob: binary read ext=png", d.get("ext") == "png", f"ext={d.get('ext')}")
    test(f"{label} blob: binary read stage_html empty", d.get("stage_html") == "", f"stage={d.get('stage_html','?')[:20]}")

    # ── Fake directories ──
    st, _ = http_post(port, "/fakedir/child1/write?ext=txt", "hello child1", token=token)
    test(f"{label} blob: write fakedir/child1 -> 200", st == 200, f"status={st}")

    st, _ = http_post(port, "/fakedir/child2/write?ext=md", "# child2", token=token)
    test(f"{label} blob: write fakedir/child2 -> 200", st == 200, f"status={st}")

    st, body = http_get(port, "/stages")
    names = [s["name"] for s in json.loads(body)]
    test(f"{label} blob: /stages has fakedir/child1", "fakedir/child1" in names, f"names={[n for n in names if 'fakedir' in n]}")

    st, body = http_get(port, "/fakedir/child1/read")
    d = json.loads(body)
    test(f"{label} blob: fakedir/child1 ext=txt", d.get("ext") == "txt", f"ext={d.get('ext')}")
    test(f"{label} blob: fakedir/child1 content", "hello child1" in d.get("stage_html", ""), f"body={d.get('stage_html','')[:20]}")

    # ── DAV ext mapping ──
    st, _ = http_method(port, "/dav/ext-blob-test.css", basic_auth=approve)
    test(f"{label} blob: DAV GET .css", st == 200, f"status={st}")

    # ── DAV virtual directories ──
    st, body = http_method(port, "/dav/fakedir/", method="PROPFIND", basic_auth=approve, headers={"Depth": "1"})
    test(f"{label} blob: DAV PROPFIND fakedir/ -> 207", st == 207, f"status={st}")
    test(f"{label} blob: DAV fakedir/ has children", "child1" in body and "child2" in body, f"body={body[:100]}")

    # ── DAV binary PUT round-trip ──
    st, _ = http_method(port, "/dav/dav-bin-test.png", method="PUT", body=png_bytes, basic_auth=approve)
    test(f"{label} blob: DAV PUT binary -> 201", st == 201, f"status={st}")

    st, body = http_get(port, "/dav-bin-test/read")
    d = json.loads(body)
    test(f"{label} blob: DAV binary ext=png", d.get("ext") == "png", f"ext={d.get('ext')}")
    test(f"{label} blob: DAV binary stage empty (binary)", d.get("stage_html") == "", f"stage={d.get('stage_html','?')[:20]}")

    # ── Unicode world names ──
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
        st, _ = http_post(port, f"/{encoded}/write?ext={uext}", ucontent, token=token)
        test(f"{label} unicode: write {uname} -> 200", st == 200, f"status={st}")

        st, body = http_get(port, f"/{encoded}/read")
        d = json.loads(body)
        test(f"{label} unicode: read {uname} ext={uext}", d.get("ext") == uext, f"ext={d.get('ext')}")

    # Check /stages has Unicode names
    st, body = http_get(port, "/stages")
    names = [s["name"] for s in json.loads(body)]
    test(f"{label} unicode: /stages has Chinese", any("\u8863" in n for n in names), "")
    test(f"{label} unicode: /stages has Korean", any("\ud55c" in n for n in names), "")
    test(f"{label} unicode: /stages has fake dir", any("\u82b1\u6912/" in n for n in names), "")


def _run_http_tests(port, label, token=""):
    """Shared HTTP tests for both Go and Python servers."""

    # Echo
    st, body = http_post(port, "/echo", "hello from test", token=token)
    test(f"{label}: POST /echo -> 200", st == 200, f"status={st}")
    if st == 200:
        test(f"{label}: POST /echo -> body preserved",
             "hello from test" in body, f"body={body[:80]}")

    # AI status
    st, body = http_get(port, "/ai/status")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label}: GET /ai/status -> JSON", True)
            test(f"{label}: /ai/status has provider", "provider" in d, f"keys={list(d.keys())}")
            test(f"{label}: /ai/status has model", "model" in d)
            test(f"{label}: /ai/status has status", "status" in d)
        except json.JSONDecodeError:
            test(f"{label}: GET /ai/status -> JSON", False, f"body={body[:80]}")
    else:
        # AI plugin might not be installed in Python mode
        skip(f"{label}: GET /ai/status", f"status={st} (plugin not loaded?)")

    # AI ask (only if /ai/status returned a valid provider)
    provider = "none"
    if st == 200:
        try:
            provider = json.loads(body).get("provider", "none")
        except Exception:
            pass

    if provider != "none":
        st2, body2 = http_post(port, "/ai/ask", "What is 1+1? Answer with just the number.", token=token)
        test(f"{label}: POST /ai/ask -> 200", st2 == 200, f"status={st2}")
        if st2 == 200:
            test(f"{label}: POST /ai/ask -> non-empty response",
                 len(body2.strip()) > 0, "empty response")

        # AI ask empty body -> 400
        st3, _ = http_post(port, "/ai/ask", "", token=token)
        test(f"{label}: POST /ai/ask empty -> 400", st3 == 400, f"status={st3}")
    else:
        skip(f"{label}: POST /ai/ask", "no AI provider or plugin not loaded")
        skip(f"{label}: POST /ai/ask empty", "no AI provider or plugin not loaded")

    # GET unknown path -> serves index.html (200) in both Go and Python.
    # This is correct behavior: unknown GET paths are world entry points.
    st, body = http_get(port, "/stages")
    test(f"{label}: GET /stages -> 200", st == 200, f"status={st}")
    if st == 200:
        try:
            d = json.loads(body)
            test(f"{label}: /stages returns array", isinstance(d, list))
        except json.JSONDecodeError:
            test(f"{label}: /stages returns JSON", False)


# ── Layer 4: Cross-runtime parity ───────────────────────────────────

def test_parity():
    print("\n=== Layer 4: Cross-runtime Parity ===")

    go_port = 13008
    py_port = 13009

    exe = os.path.join(ROOT, EXE_NAME)
    if not os.path.exists(exe):
        skip("Parity tests", f"no {EXE_NAME}")
        return

    parity_token = "test-parity-token"
    parity_approve = "test-parity-approve"
    env_go = os.environ.copy()
    env_go["ELASTIK_PORT"] = str(go_port)
    env_go["ELASTIK_HOST"] = "127.0.0.1"
    env_go["ELASTIK_TOKEN"] = parity_token
    env_go["ELASTIK_APPROVE_TOKEN"] = parity_approve

    env_py = os.environ.copy()
    env_py["ELASTIK_PORT"] = str(py_port)
    env_py["ELASTIK_HOST"] = "127.0.0.1"
    env_py["ELASTIK_TOKEN"] = parity_token
    env_py["ELASTIK_APPROVE_TOKEN"] = parity_approve

    # Install ai + devtools plugins for Python
    import shutil
    _parity_installed = []
    for pname in ["ai.py", "devtools.py"]:
        src = os.path.join(ROOT, "plugins", "available", pname)
        dst = os.path.join(ROOT, "plugins", pname)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            _parity_installed.append(dst)

    go_proc = subprocess.Popen(
        [exe], env=env_go, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    py_proc = subprocess.Popen(
        [sys.executable, "boot.py"], env=env_py, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )

    try:
        go_ok = wait_for_server(go_port)
        py_ok = wait_for_server(py_port)
        if not go_ok or not py_ok:
            test("Both servers start", False,
                 f"go={'ok' if go_ok else 'fail'} py={'ok' if py_ok else 'fail'}")
            return
        test("Both servers start", True)

        # Compare /ai/status structure
        go_st, go_body = http_get(go_port, "/ai/status")
        py_st, py_body = http_get(py_port, "/ai/status")
        test("parity: /ai/status same status code", go_st == py_st,
             f"go={go_st} py={py_st}")
        if go_st == 200 and py_st == 200:
            try:
                go_d = json.loads(go_body)
                py_d = json.loads(py_body)
                test("parity: /ai/status same keys",
                     set(go_d.keys()) == set(py_d.keys()),
                     f"go={set(go_d.keys())} py={set(py_d.keys())}")
                test("parity: /ai/status same provider",
                     go_d.get("provider") == py_d.get("provider"),
                     f"go={go_d.get('provider')} py={py_d.get('provider')}")
            except json.JSONDecodeError as e:
                test("parity: /ai/status JSON parse", False, str(e))

        # Compare /echo
        go_st, go_body = http_post(go_port, "/echo", "parity test", token=parity_token)
        py_st, py_body = http_post(py_port, "/echo", "parity test", token=parity_token)
        test("parity: /echo same status", go_st == py_st,
             f"go={go_st} py={py_st}")
        test("parity: /echo same body",
             "parity test" in go_body and "parity test" in py_body,
             f"go={go_body[:40]} py={py_body[:40]}")

        # Compare error handling
        go_st, _ = http_post(go_port, "/ai/ask", "", token=parity_token)
        py_st, _ = http_post(py_port, "/ai/ask", "", token=parity_token)
        test("parity: /ai/ask empty -> same status", go_st == py_st,
             f"go={go_st} py={py_st}")

        # ── Auth parity ──

        # POST no token -> both 403
        go_st, _ = http_post(go_port, "/echo", "x")
        py_st, _ = http_post(py_port, "/echo", "x")
        test("parity: POST no token -> both 403",
             go_st == 403 and py_st == 403, f"go={go_st} py={py_st}")

        # POST wrong token -> both 403
        go_st, _ = http_post(go_port, "/echo", "x", token="wrong")
        py_st, _ = http_post(py_port, "/echo", "x", token="wrong")
        test("parity: POST wrong token -> both 403",
             go_st == 403 and py_st == 403, f"go={go_st} py={py_st}")

        # config-* with auth token only -> both 403
        go_st, _ = http_post(go_port, "/config-x/write", "d", token=parity_token)
        py_st, _ = http_post(py_port, "/config-x/write", "d", token=parity_token)
        test("parity: config-* auth-only -> both 403",
             go_st == 403 and py_st == 403, f"go={go_st} py={py_st}")

        # config-* with approve -> both pass
        go_st, _ = http_post(go_port, "/config-x/write", "d", approve=parity_approve)
        py_st, _ = http_post(py_port, "/config-x/write", "d", approve=parity_approve)
        test("parity: config-* approve -> both pass",
             go_st in (200, 201) and py_st in (200, 201), f"go={go_st} py={py_st}")

        # reload is Go-only — test auth on it separately
        go_st, _ = http_post(go_port, "/plugins/reload")
        test("parity: go reload no token -> 403", go_st == 403, f"go={go_st}")
        go_st, _ = http_post(go_port, "/plugins/reload", token=parity_token)
        test("parity: go reload auth -> 200", go_st == 200, f"go={go_st}")

        # ── Devtools parity ──
        # Both runtimes should serve the same devtools routes with same behavior

        # /health
        go_st, go_body = http_get(go_port, "/health")
        py_st, py_body = http_get(py_port, "/health")
        test("parity: /health same status", go_st == py_st, f"go={go_st} py={py_st}")

        # /true
        go_st, _ = http_get(go_port, "/true")
        py_st, _ = http_get(py_port, "/true")
        test("parity: /true same status", go_st == py_st == 200, f"go={go_st} py={py_st}")

        # /false
        go_st, _ = http_get(go_port, "/false")
        py_st, _ = http_get(py_port, "/false")
        test("parity: /false same status", go_st == py_st == 403, f"go={go_st} py={py_st}")

        # /full
        go_st, _ = http_get(go_port, "/full")
        py_st, _ = http_get(py_port, "/full")
        test("parity: /full same status", go_st == py_st == 507, f"go={go_st} py={py_st}")

        # /rev
        go_st, go_body = http_post(go_port, "/rev", "hello", token=parity_token)
        py_st, py_body = http_post(py_port, "/rev", "hello", token=parity_token)
        test("parity: /rev same status", go_st == py_st == 200, f"go={go_st} py={py_st}")
        test("parity: /rev same body",
             go_body.strip() == py_body.strip() == "olleh",
             f"go={go_body[:20]} py={py_body[:20]}")

        # /wc-c
        go_st, go_body = http_post(go_port, "/wc-c", "test123", token=parity_token)
        py_st, py_body = http_post(py_port, "/wc-c", "test123", token=parity_token)
        test("parity: /wc-c same status", go_st == py_st == 200, f"go={go_st} py={py_st}")
        test("parity: /wc-c same count",
             go_body.strip() == py_body.strip() == "7",
             f"go={go_body[:10]} py={py_body[:10]}")

        # /time — both should return timestamps within 5s of each other
        go_st, go_body = http_get(go_port, "/time")
        py_st, py_body = http_get(py_port, "/time")
        test("parity: /time same status", go_st == py_st == 200, f"go={go_st} py={py_st}")
        if go_st == 200 and py_st == 200:
            test("parity: /time within 5s",
                 abs(int(go_body.strip()) - int(py_body.strip())) < 5,
                 f"go={go_body.strip()} py={py_body.strip()}")

        # /grep — write a world on both, grep on both, compare
        _pw = "parity-grep-test"
        _pc = "parity line one\nparity NEEDLE two\nparity line three"
        go_ws, _ = http_post(go_port, f"/{_pw}/write", _pc, token=parity_token)
        py_ws, _ = http_post(py_port, f"/{_pw}/write", _pc, token=parity_token)
        if go_ws == 200 and py_ws == 200:
            go_st, go_body = http_get(go_port, "/grep?q=NEEDLE")
            py_st, py_body = http_get(py_port, "/grep?q=NEEDLE")
            test("parity: /grep same status", go_st == py_st == 200,
                 f"go={go_st} py={py_st}")
            test("parity: /grep both find NEEDLE",
                 "NEEDLE" in go_body and "NEEDLE" in py_body,
                 f"go={go_body[:60]} py={py_body[:60]}")
            # mode=l
            go_st, go_body = http_get(go_port, "/grep?q=NEEDLE&mode=l")
            py_st, py_body = http_get(py_port, "/grep?q=NEEDLE&mode=l")
            test("parity: /grep?mode=l same status", go_st == py_st == 200,
                 f"go={go_st} py={py_st}")
            test("parity: /grep?mode=l both list world",
                 _pw in go_body and _pw in py_body,
                 f"go={go_body[:60]} py={py_body[:60]}")

    finally:
        go_proc.terminate()
        py_proc.terminate()
        for p in [go_proc, py_proc]:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for p in _parity_installed:
            if os.path.exists(p):
                os.remove(p)


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    print(f"elastik plugin tests — mode: {mode}")
    print(f"root: {ROOT}")

    if mode in ("all", "cgi"):
        test_cgi()
    if mode in ("all", "go"):
        if mode == "go":
            test_cgi()
        test_go()
    if mode in ("all", "python"):
        if mode == "python":
            test_cgi()
        test_python()
    if mode == "all":
        test_parity()

    print(f"\n{'=' * 40}")
    print(f"  PASS: {PASS}  FAIL: {FAIL}  SKIP: {SKIP}")
    total = PASS + FAIL
    if total > 0:
        print(f"  {PASS}/{total} ({100*PASS//total}%)")
    print(f"{'=' * 40}")

    sys.exit(1 if FAIL > 0 else 0)
