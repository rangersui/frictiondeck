"""elastik — the protocol. ~258 lines. Hand-copyable. The survivor's format."""
import asyncio, base64, hashlib, hmac as _hmac, json, os, re, secrets, sqlite3, subprocess, sys, time
from pathlib import Path

DATA = Path("data")
# Load env file: .env, _env, .env.local (iOS doesn't support dotfiles)
for _ef in (".env", "_env", ".env.local"):
    _ep = Path(__file__).resolve().parent / _ef
    if _ep.exists():
        for _line in _ep.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                k, v = _line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break
KEY = os.getenv("ELASTIK_KEY", "elastik-dev-key").encode()
AUTH_TOKEN = os.getenv("ELASTIK_TOKEN", "")
APPROVE_TOKEN = os.getenv("ELASTIK_APPROVE_TOKEN", "")
HOST = os.getenv("ELASTIK_HOST", "127.0.0.1")
PORT = int(os.getenv("ELASTIK_PORT", "3004"))
MAX_BODY = 5 * 1024 * 1024
INDEX = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
OPENAPI = Path(__file__).with_name("openapi.json").read_text(encoding="utf-8")
SW = Path(__file__).with_name("sw.js").read_text(encoding="utf-8")
MANIFEST = Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
_icon_path = Path(__file__).with_name("icon.png")
ICON = _icon_path.read_bytes() if _icon_path.exists() else None
_shell_path = Path(__file__).with_name("shell.html")
SHELL = _shell_path.read_text(encoding="utf-8") if _shell_path.exists() else None
_mirror_path = Path(__file__).with_name("mirror.html")
MIRROR = _mirror_path.read_text(encoding="utf-8") if _mirror_path.exists() else None
def _csp():
    cdn = "https:"
    try:
        if (DATA / "config-cdn").exists():
            c = conn("config-cdn")
            r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            if r and r["stage_html"] and r["stage_html"].strip():
                domains = [d.strip() for d in r["stage_html"].splitlines() if d.strip()]
                cdn = " ".join(f"https://{d}" for d in domains)
    except Exception as e: print(f"  warn: CDN config read failed: {e}")
    return (f"default-src 'self' data: blob:; script-src 'unsafe-inline' 'unsafe-eval' {cdn} data:; "
            f"style-src 'unsafe-inline' {cdn} data:; img-src * data: blob:; font-src * data:; "
            f"connect-src 'self'; worker-src 'self'")
_VALID_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')
_db = {}

def conn(name):
    if name not in _db:
        d = DATA / name; d.mkdir(parents=True, exist_ok=True)
        db_path = d / "universe.db"
        for ext in ("-shm", "-wal"):
            stale = d / f"universe.db{ext}"
            try: stale.unlink(missing_ok=True)
            except OSError: pass
        c = sqlite3.connect(str(db_path), check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
        except sqlite3.OperationalError:
            pass
        c.executescript("""
            CREATE TABLE IF NOT EXISTS stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html TEXT DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
            INSERT OR IGNORE INTO stage_meta(id,updated_at) VALUES(1,datetime('now'));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT DEFAULT '{}',
                hmac TEXT NOT NULL, prev_hmac TEXT DEFAULT '');
        """)
        _db[name] = c
    return _db[name]

def log_event(name, etype, payload=None):
    c = conn(name); p = json.dumps(payload or {}, ensure_ascii=False)
    row = c.execute("SELECT hmac FROM events ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hmac"] if row else ""
    h = _hmac.new(KEY, (prev + p).encode(), hashlib.sha256).hexdigest()
    c.execute("INSERT INTO events(timestamp,event_type,payload,hmac,prev_hmac) VALUES(datetime('now'),?,?,?,?)",
              (etype, p, h, prev)); c.commit()

def _extract(b, action):
    if b.startswith("{") and action != "patch":
        try:
            p = json.loads(b)
            return p.get("body") or p.get("content") or p.get("text") or b
        except json.JSONDecodeError: pass
    return b

async def recv(receive):
    b = b""
    while True:
        m = await receive(); b += m.get("body", b"")
        if len(b) > MAX_BODY: raise ValueError("body too large")
        if not m.get("more_body"): return b

async def send_r(send, status, data, ct="application/json", csp=False, extra_headers=None):
    body = data.encode() if isinstance(data, str) else data
    h = [[b"content-type", ct.encode()], [b"content-length", str(len(body)).encode()]]
    if csp: h.append([b"content-security-policy", _csp().encode()])
    if extra_headers: h.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": h})
    await send({"type": "http.response.body", "body": body})

# ── Mirror reverse proxy ─────────────────────────────────────────────
def _mirror_proxy(target, domain=""):
    """curl target, return (body_bytes, content_type). Injects <base> for HTML."""
    try:
        r = subprocess.run(["curl", "-s", "-L", "-m", "30", "-D", "-", target],
                           capture_output=True, timeout=35)
        raw = r.stdout
        # curl -D - with -L may produce multiple header blocks; use the last one.
        sep = raw.rfind(b"\r\n\r\n")
        if sep == -1: sep = raw.rfind(b"\n\n")
        if sep == -1: return raw, "text/html"
        headers_part = raw[:sep].decode("utf-8", "replace").lower()
        body = raw[sep+4:] if raw[sep:sep+4] == b"\r\n\r\n" else raw[sep+2:]
        ct = "text/html"
        for line in headers_part.split("\n"):
            if line.strip().startswith("content-type:"):
                ct = line.split(":", 1)[1].strip()
                break
        if "text/html" in ct and domain:
            body = re.sub(rb'(?i)<meta[^>]*(?:content-security-policy|x-frame-options)[^>]*>', b'', body)
            body = f'<base href="/m/{domain}/">'.encode() + body
        return body, ct
    except Exception as e:
        return json.dumps({"error": str(e)}).encode(), "application/json"

def _mirror_target(path, qs):
    """Parse mirror URL. Returns (target, domain) or (None, None)."""
    from urllib.parse import parse_qs, urlparse
    if path in ("/mirror", "/mirror/"):
        params = parse_qs(qs)
        raw = params.get("url", [""])[0]
        if not raw or not raw.startswith(("http://", "https://")): return None, None
        return raw, urlparse(raw).netloc
    if path.startswith("/m/"):
        rest = path[3:]
        slash = rest.find("/")
        if slash == -1: return "https://" + rest, rest
        dom = rest[:slash]
        p = rest[slash:]
        target = "https://" + dom + p
        if qs: target += "?" + qs
        return target, dom
    return None, None

def _check_basic_auth(scope):
    """Check Basic Auth against APPROVE_TOKEN. Returns True if valid."""
    if not APPROVE_TOKEN: return False
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            auth = v.decode()
            if auth.startswith("Basic "):
                try:
                    _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
                    return _hmac.compare_digest(pwd, APPROVE_TOKEN)
                except Exception: pass
            break
    return False

# ── Plugin slots — empty by default. plugins.py fills them. ──────────
_plugins = {}     # route path → async handler
_auth = None      # auth middleware (set by auth plugin)
_plugin_meta = [] # plugin metadata list

async def app(scope, receive, send):
    if scope["type"] != "http": return
    path = scope["path"].rstrip("/") or "/"; method = scope["method"]
    print(f"  {method} {path}")
    raw = scope.get("raw_path", b"").decode("utf-8", "replace")
    # Mirror: /mirror?url=X (entry) or /m/domain/path (subsequent).
    _mt, _md = _mirror_target(path, scope.get("query_string", b"").decode())
    if _mt:
        if not _check_basic_auth(scope):
            return await send_r(send, 401, '{"error":"authentication required"}',
                                extra_headers=[[b"www-authenticate", b'Basic realm="elastik"']])
        body, ct = _mirror_proxy(_mt, _md)
        return await send_r(send, 200, body, ct=ct)
    # Mirror Referer fallback — absolute paths from mirrored pages.
    _ref = ""
    for k, v in scope.get("headers", []):
        if k == b"referer": _ref = v.decode(); break
    _dom = ""
    if "/m/" in _ref:
        _ri = _ref.index("/m/")
        _rest = _ref[_ri+3:]
        _dom = _rest.split("/")[0].split("?")[0]
    elif "/mirror?url=" in _ref:
        from urllib.parse import unquote, urlparse
        _ri = _ref.index("/mirror?url=")
        _raw = unquote(_ref[_ri+12:])
        try: _dom = urlparse(_raw).netloc
        except Exception: pass
    if _dom and _check_basic_auth(scope):
        _qs = scope.get("query_string", b"").decode()
        if method == "GET":
            # 302 redirect to /m/domain/path — pull URL back into namespace.
            _redir = "/m/" + _dom + path
            if _qs: _redir += "?" + _qs
            return await send_r(send, 302, '{"redirect":true}',
                                extra_headers=[[b"location", _redir.encode()]])
        else:
            # POST: proxy directly — redirect would lose body.
            _target = "https://" + _dom + path
            if _qs: _target += "?" + _qs
            body, ct = _mirror_proxy(_target, _dom)
            return await send_r(send, 200, body, ct=ct)
    if '..' in path or '//' in path or '..' in raw or '//' in raw:
        return await send_r(send, 400, '{"error":"invalid path"}')
    # /shell, /mirror — Basic Auth protected root pages.
    _root_page = (SHELL if path == "/shell" else MIRROR if path == "/mirror" else None)
    if method == "GET" and path in ("/shell", "/mirror") and _root_page:
        if not APPROVE_TOKEN:
            return await send_r(send, 403, '{"error":"approve token not configured"}')
        auth_header = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization": auth_header = v.decode(); break
        ok = False
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                _, pwd = decoded.split(":", 1)
                ok = _hmac.compare_digest(pwd, APPROVE_TOKEN)
            except Exception: pass
        if not ok:
            return await send_r(send, 401, '{"error":"authentication required"}',
                                extra_headers=[[b"www-authenticate", b'Basic realm="elastik"']])
        return await send_r(send, 200, _root_page, ct="text/html")

    if _auth and not await _auth(scope, path, method):
        return await send_r(send, 403, '{"error":"unauthorized"}')
    parts = [p for p in path.split("/") if p]

    # Plugin route dispatch — slot empty = skip
    base_path = path.split("?")[0]
    if base_path in _plugins:
        b = await recv(receive)
        qs = scope.get("query_string", b"").decode()
        params = dict(x.split("=",1) for x in qs.split("&") if "=" in x) if qs else {}
        params["_scope"] = scope
        result = await _plugins[base_path](method, b, params)
        status = result.pop("_status", 200); redirect = result.pop("_redirect", None)
        cookies = result.pop("_cookies", []); html_body = result.pop("_html", None)
        extra_h = [[b"set-cookie", c.encode()] for c in cookies]
        if redirect: extra_h.append([b"location", redirect.encode()]); status = 302
        if html_body is not None: return await send_r(send, status, html_body, ct="text/html", extra_headers=extra_h or None)
        return await send_r(send, status, json.dumps(result), extra_headers=extra_h or None)

    if method == "GET" and path == "/openapi.json": return await send_r(send, 200, OPENAPI)
    if method == "GET" and path == "/sw.js": return await send_r(send, 200, SW, "application/javascript")
    if method == "GET" and path == "/manifest.json": return await send_r(send, 200, MANIFEST, "application/manifest+json")
    if method == "GET" and path == "/icon.png" and ICON:
        await send({"type":"http.response.start","status":200,"headers":[[b"content-type",b"image/png"]]})
        await send({"type":"http.response.body","body":ICON})
        return
    if method == "GET" and path == "/stages":
        stages = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    r = conn(d.name).execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
                    stages.append({"name": d.name, "version": r["version"], "updated_at": r["updated_at"]})
        return await send_r(send, 200, json.dumps(stages))

    if len(parts) == 2 and parts[1] in ("read","write","append","pending","result","clear","sync"):
        name, action = parts
        if not _VALID_NAME.match(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        if method == "GET" and action == "read":
            if not (DATA / name / "universe.db").exists():
                return await send_r(send, 404, '{"error":"world not found"}')
            c = conn(name)
            r = c.execute("SELECT stage_html,pending_js,js_result,version FROM stage_meta WHERE id=1").fetchone()
            return await send_r(send, 200, json.dumps(dict(r)))
        c = conn(name)
        try: b = (await recv(receive)).decode("utf-8", "replace")
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        b = _extract(b, action)
        if action == "write":
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            log_event(name, "stage_written", {"len": len(b)})
            return await send_r(send, 200, json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]}))
        if action == "append":
            c.execute("UPDATE stage_meta SET stage_html=stage_html||?,version=version+1,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            log_event(name, "stage_appended", {"len": len(b)})
            return await send_r(send, 200, json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]}))
        if action == "sync":
            c.execute("UPDATE stage_meta SET stage_html=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            return await send_r(send, 200, '{"ok":true}')
        if action == "pending":
            c.execute("UPDATE stage_meta SET pending_js=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            return await send_r(send, 200, '{"ok":true}')
        if action == "result":
            c.execute("UPDATE stage_meta SET js_result=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            return await send_r(send, 200, '{"ok":true}')
        if action == "clear":
            c.execute("UPDATE stage_meta SET pending_js='',js_result='',updated_at=datetime('now') WHERE id=1"); c.commit()
            return await send_r(send, 200, '{"ok":true}')

    if method == "GET": return await send_r(send, 200, INDEX, "text/html", csp=True)
    await send_r(send, 404, '{"error":"not found"}')

# ── Mini ASGI server — zero dependencies ─────────────────────────────

async def _mini_serve(asgi_app, host, port):
    """Zero-dependency ASGI server. Enough for single-user localhost."""
    async def handle(reader, writer):
        try:
            line = await reader.readline()
            if not line: writer.close(); return
            parts = line.decode("utf-8", "replace").strip().split(" ", 2)
            if len(parts) < 2: writer.close(); return
            method, full_path = parts[0], parts[1]
            headers = []
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""): break
                decoded = h.decode("utf-8", "replace").strip()
                if ": " in decoded:
                    k, v = decoded.split(": ", 1)
                    headers.append([k.lower().encode(), v.encode()])
            content_length = 0
            for k, v in headers:
                if k == b"content-length": content_length = int(v); break
            body = await reader.readexactly(content_length) if content_length else b""
            path_part = full_path.split("?")[0]
            qs = full_path.split("?", 1)[1] if "?" in full_path else ""
            scope = {
                "type": "http", "method": method, "path": path_part,
                "raw_path": path_part.encode(), "query_string": qs.encode(),
                "headers": headers,
            }
            response = {}
            async def _recv():
                return {"type": "http.request", "body": body}
            async def _send(msg):
                if msg["type"] == "http.response.start":
                    response["status"] = msg["status"]
                    response["headers"] = msg.get("headers", [])
                elif msg["type"] == "http.response.body":
                    out = f"HTTP/1.1 {response['status']} OK\r\n".encode()
                    for k, v in response["headers"]:
                        out += k + b": " + v + b"\r\n"
                    out += b"\r\n" + msg.get("body", b"")
                    writer.write(out)
                    await writer.drain()
            await asgi_app(scope, _recv, _send)
        except Exception:
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                await writer.drain()
            except Exception: pass
        finally:
            try: writer.close()
            except Exception: pass
    srv = await asyncio.start_server(handle, host, port)
    await srv.serve_forever()

def run(extra_tasks=None):
    """Start the server. extra_tasks: list of coroutines to run alongside."""
    tasks = extra_tasks or []
    try:
        import uvicorn
        async def _serve():
            config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
            server = uvicorn.Server(config)
            await asyncio.gather(server.serve(), *tasks)
        asyncio.run(_serve())
    except ImportError:
        print("  (uvicorn not found -- using built-in server)")
        async def _serve():
            await asyncio.gather(_mini_serve(app, HOST, PORT), *tasks)
        asyncio.run(_serve())

if __name__ == "__main__":
    if not AUTH_TOKEN:
        print("\n  ! ELASTIK_TOKEN not set. Refusing to start in public mode.")
        print("  Set ELASTIK_TOKEN in .env or environment.\n")
        sys.exit(1)
    print(f"\n  elastik -> http://{HOST}:{PORT}  [protocol only]")
    print(f"  no plugins loaded. use boot.py for full system.\n")
    run()
