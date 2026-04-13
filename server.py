"""elastik — the protocol. Core routes only; everything else is a plugin."""
import asyncio, base64, hashlib, hmac as _hmac, json, os, re, sqlite3, sys
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
_icon192_path = Path(__file__).with_name("icon-192.png")
ICON192 = _icon192_path.read_bytes() if _icon192_path.exists() else None
def _csp():
    cdn = "https:"
    try:
        if (DATA / "config-cdn").exists():
            c = conn("config-cdn")
            r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            s = r["stage_html"] if r else None
            if isinstance(s, bytes): s = s.decode("utf-8", "replace")
            if s and s.strip():
                domains = [d.strip() for d in s.splitlines() if d.strip()]
                cdn = " ".join(f"https://{d}" for d in domains)
    except Exception as e: print(f"  warn: CDN config read failed: {e}")
    return (f"default-src 'self' data: blob:; script-src 'unsafe-inline' 'unsafe-eval' {cdn} data:; "
            f"style-src 'unsafe-inline' {cdn} data:; img-src * data: blob:; font-src * data:; "
            f"connect-src 'self'; worker-src 'self'")
_INVALID_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f\\:*?"<>|]')
def _valid_name(name):
    """Check world name: any Unicode allowed, reject control chars + Windows-illegal + traversal."""
    if not name or _INVALID_NAME_CHARS.search(name): return False
    if "//" in name or ".." in name: return False
    if name.startswith("/") or name.endswith("/"): return False
    return True
def _disk_name(name):
    """World name → safe filesystem dir name. / → %2F (flat on disk)."""
    return name.replace("/", "%2F")
def _logical_name(disk):
    """Filesystem dir name → world name. %2F → /."""
    return disk.replace("%2F", "/")
_CT = {"html":"text/html","htm":"text/html","txt":"text/plain","plain":"text/plain",
       "css":"text/css","js":"text/javascript","json":"application/json",
       "png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg","gif":"image/gif",
       "svg":"image/svg+xml","webp":"image/webp","ico":"image/x-icon",
       "pdf":"application/pdf","zip":"application/zip",
       "md":"text/markdown","py":"text/x-python","c":"text/x-c","cpp":"text/x-c++src",
       "h":"text/x-c","go":"text/x-go","rs":"text/x-rust","ts":"text/typescript",
       "tsx":"text/tsx","jsx":"text/jsx","sh":"text/x-sh","yaml":"text/yaml",
       "yml":"text/yaml","toml":"text/toml","xml":"application/xml","sql":"text/x-sql",
       "lua":"text/x-lua","rb":"text/x-ruby","java":"text/x-java",
       "kt":"text/x-kotlin","swift":"text/x-swift","v":"text/x-verilog"}
def _ext_to_ct(ext): return _CT.get(ext or "plain", "application/octet-stream")
_BINARY_EXT = {"png","jpg","jpeg","gif","webp","ico","pdf","zip","mp3","mp4",
               "wav","ogg","woff","woff2","ttf","otf","eot","bin"}

def _infer_type(stage_html):
    """Heuristic for one-time migration of pre-type-column worlds."""
    s = (stage_html or '').lstrip()
    if not s: return 'plain'
    if s.startswith('<!--use:'): return 'html'
    if s[0] == '<': return 'html'
    sl = s[:1024].lower()
    if '<script' in sl or '<body' in sl or '<html' in sl: return 'html'
    return 'plain'

def _check_auth(scope):
    """Authorization header → 'approve', 'auth', or None.
    Bearer or Basic, same tokens."""
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            a = v.decode("utf-8", "replace")
            if a.startswith("Bearer "):
                tok = a[7:]
                if APPROVE_TOKEN and _hmac.compare_digest(tok, APPROVE_TOKEN): return "approve"
                if AUTH_TOKEN and _hmac.compare_digest(tok, AUTH_TOKEN): return "auth"
            elif a.startswith("Basic "):
                try:
                    _, pwd = base64.b64decode(a[6:]).decode().split(":", 1)
                    if APPROVE_TOKEN and _hmac.compare_digest(pwd, APPROVE_TOKEN): return "approve"
                    if AUTH_TOKEN and _hmac.compare_digest(pwd, AUTH_TOKEN): return "auth"
                except (ValueError, UnicodeDecodeError): pass
            return None
    return None

    # _check_url_auth — injected by url_auth plugin if installed.
    # Not here. No plugin = no URL auth = no attack surface.

_db = {}

def conn(name):
    if name not in _db:
        d = DATA / _disk_name(name); d.mkdir(parents=True, exist_ok=True)
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
                stage_html BLOB DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '',
                ext TEXT DEFAULT 'plain');
            INSERT OR IGNORE INTO stage_meta(id,updated_at) VALUES(1,datetime('now'));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT DEFAULT '{}',
                hmac TEXT NOT NULL, prev_hmac TEXT DEFAULT '');
        """)
        # Migration: stage_html TEXT+type → stage_html BLOB+ext.
        # Column NAME stays stage_html (40+ references). Type changes to BLOB.
        cols = {row[1] for row in c.execute("PRAGMA table_info(stage_meta)").fetchall()}
        def _safe(row, key, default=""):
            try: return row[key] or default
            except (IndexError, KeyError): return default
        if "ext" not in cols:
            # Old schema: stage_html TEXT, type TEXT, no ext → full rebuild
            r = c.execute("SELECT * FROM stage_meta WHERE id=1").fetchone()
            content = r["stage_html"] or ""
            old_type = "plain"
            try: old_type = r["type"] or "plain"
            except (IndexError, KeyError): pass
            # Strip legacy :::type:xxx::: prefix if present
            import re as _re
            _m = _re.match(r'^:::type:(\w+):::\n?', content) if content else None
            if _m:
                ext = _m.group(1)
                content = content[_m.end():]
            else:
                ext = old_type
                if ext == "plain" and _infer_type(content) == "html":
                    ext = "html"
            c.execute("DROP TABLE stage_meta")
            c.executescript("""CREATE TABLE stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html BLOB DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '', ext TEXT DEFAULT 'plain');""")
            c.execute("INSERT INTO stage_meta VALUES(?,?,?,?,?,?,?)",
                (1, content, _safe(r,"pending_js"), _safe(r,"js_result"),
                 _safe(r,"version",0), _safe(r,"updated_at"), ext))
            c.commit()
            print(f"  migrated: {name} (ext={ext})")
        elif "stage_html" not in cols and "stage" in cols:
            # Broken schema from earlier attempt: column named 'stage' → fix to 'stage_html'
            r = c.execute("SELECT * FROM stage_meta WHERE id=1").fetchone()
            c.execute("DROP TABLE stage_meta")
            c.executescript("""CREATE TABLE stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html BLOB DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '', ext TEXT DEFAULT 'plain');""")
            c.execute("INSERT INTO stage_meta VALUES(?,?,?,?,?,?,?)",
                (1, _safe(r,"stage"), _safe(r,"pending_js"), _safe(r,"js_result"),
                 _safe(r,"version",0), _safe(r,"updated_at"), _safe(r,"ext","html")))
            c.commit()
            print(f"  fixed: {name} (stage→stage_html)")
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

def _check_basic_auth(scope):
    """Legacy compat — returns True if auth level is approve. Used by plugin dispatch."""
    return _check_auth(scope) == "approve"

# ── Plugin slots — empty by default. plugins.py fills them. ──────────
_plugins = {}      # route path → async handler
_plugin_auth = {}  # route path → "none" | "auth" | "approve"
_auth = None       # auth middleware (set by auth plugin)
_plugin_meta = []  # plugin metadata list

def _check_auth_token(scope):
    """Legacy compat — returns True if any auth level present, or no token configured."""
    if not AUTH_TOKEN: return True  # no token configured = open
    return _check_auth(scope) is not None

def _match_plugin(base_path):
    """Exact match first, then prefix match (longest wins).
    Returns (handler, matched_route) or (None, None)."""
    h = _plugins.get(base_path)
    if h: return h, base_path
    best = None
    for p in _plugins:
        if base_path.startswith(p + "/") and (best is None or len(p) > len(best)):
            best = p
    return (_plugins[best], best) if best else (None, None)

async def app(scope, receive, send):
    if scope["type"] != "http": return
    path = scope["path"].rstrip("/") or "/"; method = scope["method"]
    try: print(f"  {method} {path}")
    except UnicodeEncodeError: print(f"  {method} {ascii(path)}")
    # Auth gate — if a plugin sets _auth, it can intercept any request.
    # Return truthy = "I sent a response" (e.g. pastebin). Falsy = proceed.
    if _auth:
        if await _auth(scope, receive, send, path, method):
            return
    raw = scope.get("raw_path", b"").decode("utf-8", "replace")
    if '..' in path or '//' in path or '..' in raw or '//' in raw:
        return await send_r(send, 400, '{"error":"invalid path"}')

    # (auth gate moved to top of app — see above)
    parts = [p for p in path.split("/") if p]

    # Plugin dispatch — exact or prefix match, server gates auth centrally.
    base_path = path.split("?")[0]
    handler, matched = _match_plugin(base_path)
    if handler:
        level = _plugin_auth.get(matched, "none")
        # OPTIONS is capability discovery — always allow so WebDAV/CORS works.
        if method != "OPTIONS":
            if level == "approve":
                if not APPROVE_TOKEN:
                    return await send_r(send, 403, '{"error":"approve token not configured"}')
                if not _check_basic_auth(scope):
                    return await send_r(send, 401, '{"error":"authentication required"}',
                                        extra_headers=[[b"www-authenticate", b'Basic realm="elastik"']])
            elif level == "auth":
                if not _check_auth_token(scope):
                    return await send_r(send, 403, '{"error":"unauthorized"}')
        try: body_raw = await recv(receive)
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        b = body_raw.decode("utf-8", "replace")
        qs = scope.get("query_string", b"").decode()
        params = dict(x.split("=",1) for x in qs.split("&") if "=" in x) if qs else {}
        params["_scope"] = scope
        params["_body_raw"] = body_raw  # raw bytes for binary plugins (dav PUT)
        result = await handler(method, b, params)
        status = result.pop("_status", 200)
        redirect = result.pop("_redirect", None)
        cookies = result.pop("_cookies", [])
        html_body = result.pop("_html", None)
        raw_body = result.pop("_body", None)
        ct = result.pop("_ct", "application/json")
        custom_h = result.pop("_headers", [])
        extra_h = [[b"set-cookie", c.encode()] for c in cookies]
        if custom_h: extra_h.extend([[str(k).encode(), str(v).encode()] for k, v in custom_h])
        if redirect: extra_h.append([b"location", redirect.encode()]); status = 302
        if html_body is not None:
            return await send_r(send, status, html_body, ct="text/html", extra_headers=extra_h or None)
        if raw_body is not None:
            return await send_r(send, status, raw_body, ct=ct, extra_headers=extra_h or None)
        return await send_r(send, status, json.dumps(result), extra_headers=extra_h or None)

    if method == "GET" and path == "/opensearch.xml":
        host = "localhost"
        for k, v in scope.get("headers", []):
            if k == b"host": host = v.decode(); break
        scheme = "https" if scope.get("scheme") == "https" else "http"
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
               f'<ShortName>elastik</ShortName><Description>elastik shell</Description>'
               f'<Url type="text/html" template="{scheme}://{host}/shell?q={{searchTerms}}"/>'
               f'</OpenSearchDescription>')
        return await send_r(send, 200, xml, ct="application/opensearchdescription+xml")
    if method == "GET" and path == "/openapi.json": return await send_r(send, 200, OPENAPI)
    if method == "GET" and path == "/sw.js": return await send_r(send, 200, SW, "application/javascript")
    if method == "GET" and path == "/manifest.json": return await send_r(send, 200, MANIFEST, "application/manifest+json")
    if method == "GET" and path == "/icon.png" and ICON:
        await send({"type":"http.response.start","status":200,"headers":[[b"content-type",b"image/png"]]})
        await send({"type":"http.response.body","body":ICON})
        return
    if method == "GET" and path == "/icon-192.png" and ICON192:
        await send({"type":"http.response.start","status":200,"headers":[[b"content-type",b"image/png"]]})
        await send({"type":"http.response.body","body":ICON192})
        return
    # Web Share Target — phone share sheet → store in "shared" world
    if method == "POST" and path == "/share":
        try: body = (await recv(receive)).decode("utf-8", "replace")
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        qs = scope.get("query_string", b"").decode()
        params = dict(x.split("=",1) for x in qs.split("&") if "=" in x) if qs else {}
        # Collect shared content from query params + body
        from urllib.parse import unquote_plus as unquote
        parts = []
        for k in ("title", "text", "url"):
            v = unquote(params.get(k, ""))
            if v: parts.append(v)
        if body and body.strip(): parts.append(body.strip())
        content = "\n".join(parts) if parts else "(empty share)"
        ts = __import__('time').strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n---\n{ts}\n{content}\n"
        c = conn("shared")
        c.execute("UPDATE stage_meta SET stage_html=stage_html||?,version=version+1,updated_at=datetime('now') WHERE id=1",(entry,)); c.commit()
        # Redirect to /shared so user sees what they shared
        return await send_r(send, 302, "", extra_headers=[[b"location", b"/shared"]])
    if method == "GET" and path == "/stages":
        stages = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    name = _logical_name(d.name)
                    r = conn(name).execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
                    stages.append({"name": name, "version": r["version"], "updated_at": r["updated_at"]})
        return await send_r(send, 200, json.dumps(stages))

    _ACTIONS = {"read","raw","write","append","pending","result","clear","sync"}
    if len(parts) >= 2 and parts[-1] in _ACTIONS:
        action = parts[-1]
        name = "/".join(parts[:-1])  # everything before the action
        if not _valid_name(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        if method == "GET" and action == "read":
            if not (DATA / _disk_name(name) / "universe.db").exists():
                return await send_r(send, 404, '{"error":"world not found"}')
            c = conn(name)
            r = c.execute("SELECT stage_html,pending_js,js_result,version,ext FROM stage_meta WHERE id=1").fetchone()
            # 304: client sends ?v=N from last poll → skip body if unchanged
            qs = scope.get("query_string", b"").decode()
            for p in qs.split("&"):
                if p.startswith("v="):
                    try:
                        if int(p[2:]) == r["version"]:
                            return await send_r(send, 304, "")
                    except ValueError: pass
                    break
            # Normalize: stage_html might be bytes (binary content) or str
            raw = r["stage_html"] or ""
            if isinstance(raw, bytes):
                try: raw = raw.decode("utf-8")
                except UnicodeDecodeError: raw = ""  # binary — use /raw
            ext = r["ext"] or "html"
            return await send_r(send, 200, json.dumps({
                "stage_html": raw, "pending_js": r["pending_js"] or "",
                "js_result": r["js_result"] or "", "version": r["version"],
                "ext": ext, "type": ext}))
        if method == "GET" and action == "raw":
            if not (DATA / _disk_name(name) / "universe.db").exists():
                return await send_r(send, 404, '{"error":"world not found"}')
            c = conn(name)
            r = c.execute("SELECT stage_html,ext FROM stage_meta WHERE id=1").fetchone()
            body = r["stage_html"] or b""
            if isinstance(body, str): body = body.encode("utf-8")
            ext = r["ext"] or "plain"
            ct = _ext_to_ct(ext)
            total = len(body)
            # Range header — lets TVs fast-forward, browsers seek, AirPlay work
            range_h = ""
            for k, v in scope.get("headers", []):
                if k == b"range": range_h = v.decode(); break
            if range_h.startswith("bytes="):
                parts = range_h[6:].split("-", 1)
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if len(parts) > 1 and parts[1] else total - 1
                if start >= total: start = total - 1
                if end >= total: end = total - 1
                chunk = body[start:end+1]
                await send({"type":"http.response.start","status":206,"headers":[
                    [b"content-type", ct.encode()],
                    [b"content-range", f"bytes {start}-{end}/{total}".encode()],
                    [b"content-length", str(len(chunk)).encode()],
                    [b"accept-ranges", b"bytes"]]})
                await send({"type":"http.response.body","body":chunk})
            else:
                await send({"type":"http.response.start","status":200,"headers":[
                    [b"content-type", ct.encode()],
                    [b"content-length", str(total).encode()],
                    [b"accept-ranges", b"bytes"]]})
                await send({"type":"http.response.body","body":body})
            return
        c = conn(name)
        # Check ?ext= early — binary exts skip text decode
        req_ext = None
        qs = scope.get("query_string", b"").decode()
        for p in qs.split("&"):
            if p.startswith("ext="):
                req_ext = p[4:].lower().strip() or None
                break
        try:
            body_bytes = await recv(receive)
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        if req_ext and req_ext in _BINARY_EXT:
            b = body_bytes  # raw bytes for binary content
        else:
            b = body_bytes.decode("utf-8", "replace")
            b = _extract(b, action)
        # Determine ext
        if action in ("write","append","sync"):
            cur = c.execute("SELECT ext FROM stage_meta WHERE id=1").fetchone()
            cur_ext = (cur["ext"] if cur else "plain") or "plain"
            if action == "write":
                new_ext = req_ext
                if not new_ext and isinstance(b, str):
                    new_ext = _infer_type(b)
                elif not new_ext:
                    new_ext = cur_ext
            else:
                new_ext = cur_ext  # append/sync preserve ext
            # Type gate: html worlds require approve token
            if (cur_ext == 'html' or new_ext == 'html') and _check_auth(scope) != "approve":
                return await send_r(send, 403, '{"error":"html write requires approve-level auth"}')
        if action == "write":
            c.execute("UPDATE stage_meta SET stage_html=?,ext=?,version=version+1,updated_at=datetime('now') WHERE id=1",(b, new_ext)); c.commit()
            log_event(name, "stage_written", {"len": len(b), "ext": new_ext})
            return await send_r(send, 200, json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"], "ext": new_ext, "type": new_ext}))
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
            except OSError: pass
        finally:
            try: writer.close()
            except OSError: pass
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

def _sync_dir(directory, glob_pattern, world_name_fn, label):
    """Sync files from a directory to worlds at startup."""
    d = Path(__file__).resolve().parent / directory
    if not d.exists(): return
    for f in sorted(d.glob(glob_pattern)):
        name = world_name_fn(f)
        content = f.read_text(encoding="utf-8")
        c = conn(name)
        old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
        if old["stage_html"] != content:
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (content,))
            c.commit()
            print(f"  {label}: synced {name}")

if __name__ == "__main__":
    if not AUTH_TOKEN:
        print("\n  ! ELASTIK_TOKEN not set. Refusing to start in public mode.")
        print("  Set ELASTIK_TOKEN in .env or environment.\n")
        sys.exit(1)
    _root = Path(__file__).resolve().parent
    os.environ.setdefault("ELASTIK_DATA", str(_root / "data"))
    os.environ.setdefault("ELASTIK_ROOT", str(_root))
    try:
        import plugins
        plugins.load_plugins()
        plugins.register_plugin_routes()
        _sync_dir("skills", "*.md", lambda f: f"skills-{f.stem}", "skills")
        _sync_dir("renderers", "renderer-*.html", lambda f: f.stem, "renderers")
        print(f"\n  elastik -> http://{HOST}:{PORT}\n")
        run(extra_tasks=[plugins.cron_loop()])
    except ImportError:
        print(f"\n  elastik -> http://{HOST}:{PORT}  [no plugins.py]\n")
        run()
