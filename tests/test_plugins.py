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

EXE_NAME = "elastik-lite.exe" if sys.platform == "win32" else "elastik-lite"

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


def http_get(port, path):
    """GET request, return (status, body_str)."""
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def http_post(port, path, body="", token=""):
    """POST request, return (status, body_str)."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body.encode(), method="POST"
        )
        if token:
            req.add_header("X-Auth-Token", token)
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

    # Edge cases on echo
    echo_path = os.path.join("plugins", "echo.py")
    if os.path.exists(echo_path):
        print(f"\n  --- echo edge cases ---")

        # Empty body
        req = json.dumps({"path": "/echo", "method": "POST", "body": "", "query": ""})
        out, _, rc = run_plugin(echo_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("echo: empty body -> status 200", resp["status"] == 200)
            test("echo: empty body -> empty body back", resp["body"] == "")

        # Large body
        big = "x" * 10000
        req = json.dumps({"path": "/echo", "method": "POST", "body": big, "query": ""})
        out, _, rc = run_plugin(echo_path, stdin_data=req + "\n")
        if rc == 0:
            resp = json.loads(out.strip())
            test("echo: large body -> preserved", len(resp["body"]) == 10000)

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

    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(go_port)
    env["ELASTIK_HOST"] = "127.0.0.1"
    proc = subprocess.Popen(
        [exe], env=env, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    try:
        if not wait_for_server(go_port):
            test("Go server starts", False, "timeout waiting for server")
            return
        test("Go server starts", True)

        _run_http_tests(go_port, "go", token="")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Layer 3: Python HTTP Integration ────────────────────────────────

def test_python():
    print("\n=== Layer 3: Python HTTP Integration ===")

    # Ensure ai plugin is installed for testing
    import shutil
    ai_src = os.path.join(ROOT, "plugins", "available", "ai.py")
    ai_dst = os.path.join(ROOT, "plugins", "ai.py")
    _installed_ai = False
    if os.path.exists(ai_src) and not os.path.exists(ai_dst):
        shutil.copy2(ai_src, ai_dst)
        _installed_ai = True

    py_port = 13007
    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(py_port)
    env["ELASTIK_HOST"] = "127.0.0.1"
    env["ELASTIK_TOKEN"] = "test-token"
    proc = subprocess.Popen(
        [sys.executable, "boot.py"], env=env, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    try:
        if not wait_for_server(py_port):
            test("Python server starts", False, "timeout waiting for server")
            return
        test("Python server starts", True)

        _run_http_tests(py_port, "python", token="test-token")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Clean up test-installed ai plugin
        if _installed_ai and os.path.exists(ai_dst):
            os.remove(ai_dst)


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

    env_go = os.environ.copy()
    env_go["ELASTIK_PORT"] = str(go_port)
    env_go["ELASTIK_HOST"] = "127.0.0.1"

    env_py = os.environ.copy()
    env_py["ELASTIK_PORT"] = str(py_port)
    env_py["ELASTIK_HOST"] = "127.0.0.1"
    env_py["ELASTIK_TOKEN"] = "test-token"

    # Install ai plugin for Python
    import shutil
    ai_src = os.path.join(ROOT, "plugins", "available", "ai.py")
    ai_dst = os.path.join(ROOT, "plugins", "ai.py")
    _installed_ai = False
    if os.path.exists(ai_src) and not os.path.exists(ai_dst):
        shutil.copy2(ai_src, ai_dst)
        _installed_ai = True

    go_proc = subprocess.Popen(
        [exe], env=env_go, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    py_proc = subprocess.Popen(
        [sys.executable, "boot.py"], env=env_py, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
        go_st, go_body = http_post(go_port, "/echo", "parity test")
        py_st, py_body = http_post(py_port, "/echo", "parity test", token="test-token")
        test("parity: /echo same status", go_st == py_st,
             f"go={go_st} py={py_st}")
        test("parity: /echo same body",
             "parity test" in go_body and "parity test" in py_body,
             f"go={go_body[:40]} py={py_body[:40]}")

        # Compare error handling
        go_st, _ = http_post(go_port, "/ai/ask", "")
        py_st, _ = http_post(py_port, "/ai/ask", "", token="test-token")
        test("parity: /ai/ask empty -> same status", go_st == py_st,
             f"go={go_st} py={py_st}")

    finally:
        go_proc.terminate()
        py_proc.terminate()
        for p in [go_proc, py_proc]:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if _installed_ai and os.path.exists(ai_dst):
            os.remove(ai_dst)


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
