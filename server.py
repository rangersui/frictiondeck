"""elastik — the protocol. Core routes only; everything else is a plugin."""
import asyncio, base64, hashlib, hmac as _hmac, json, os, re, shutil, sqlite3, sys, time
from pathlib import Path
VERSION = "4"
_BOOT = time.time()
if __name__ == "__main__": sys.modules["server"] = sys.modules[__name__]

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
SW = Path(__file__).with_name("sw.js").read_text(encoding="utf-8")
MANIFEST = Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
_icon_path = Path(__file__).with_name("icon.png")
ICON = _icon_path.read_bytes() if _icon_path.exists() else None
_icon192_path = Path(__file__).with_name("icon-192.png")
ICON192 = _icon192_path.read_bytes() if _icon192_path.exists() else None
def _csp():
    cdn = "https:"
    try:
        etc_cdn_disk = DATA / "etc%2Fcdn"
        if etc_cdn_disk.exists():
            c = conn("etc/cdn")
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

def _lookup_etc_auth(user, pwd):
    """Look up user:password in /etc/passwd (user:tier) + /etc/shadow (user:sha256(tok)).
    Returns 'approve' for T3, 'auth' for T2, None for T1 or unknown.

    /etc/passwd format, one per line:   user:T1|T2|T3
    /etc/shadow format, one per line:   user:<sha256 hex of token>
    Both stored as regular worlds under etc/. Read auth for /etc/shadow is
    enforced separately (T3 only — see the /read dispatch)."""
    passwd_disk = DATA / "etc%2Fpasswd"
    shadow_disk = DATA / "etc%2Fshadow"
    if not (passwd_disk.exists() and shadow_disk.exists()): return None
    try:
        pwd_raw = conn("etc/passwd").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"] or ""
        if isinstance(pwd_raw, bytes): pwd_raw = pwd_raw.decode("utf-8", "replace")
        tiers = {}
        for line in pwd_raw.splitlines():
            if ":" in line:
                u, t = line.split(":", 1); tiers[u.strip()] = t.strip()
        if user not in tiers: return None
        tier = tiers[user]
        shd_raw = conn("etc/shadow").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"] or ""
        if isinstance(shd_raw, bytes): shd_raw = shd_raw.decode("utf-8", "replace")
        hashes = {}
        for line in shd_raw.splitlines():
            if ":" in line:
                u, h = line.split(":", 1); hashes[u.strip()] = h.strip()
        expected = hashes.get(user)
        if not expected: return None
        actual = hashlib.sha256(pwd.encode("utf-8")).hexdigest()
        if not _hmac.compare_digest(actual, expected): return None
        if tier == "T3": return "approve"
        if tier == "T2": return "auth"
        return None  # T1 or unknown tier — treat as unauthenticated for write gates
    except Exception:
        return None


def _check_auth(scope):
    """Authorization header → 'approve' (T3), 'auth' (T2), or None.

    Sources, in priority order:
      1. Env tokens APPROVE_TOKEN / AUTH_TOKEN (bootstrap, Bearer or Basic pwd)
      2. /etc/passwd + /etc/shadow lookup (Basic user:pass, multi-user)
    """
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            a = v.decode("utf-8", "replace")
            if a.startswith("Bearer "):
                tok = a[7:]
                if APPROVE_TOKEN and _hmac.compare_digest(tok, APPROVE_TOKEN): return "approve"
                if AUTH_TOKEN and _hmac.compare_digest(tok, AUTH_TOKEN): return "auth"
            elif a.startswith("Basic "):
                try:
                    user, pwd = base64.b64decode(a[6:]).decode().split(":", 1)
                    if APPROVE_TOKEN and _hmac.compare_digest(pwd, APPROVE_TOKEN): return "approve"
                    if AUTH_TOKEN and _hmac.compare_digest(pwd, AUTH_TOKEN): return "auth"
                    return _lookup_etc_auth(user, pwd)
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
    body = data.encode("utf-8") if isinstance(data, str) else data
    _ct = ct if "charset" in ct or ct.startswith(("image/", "audio/", "video/", "application/octet")) else (ct + "; charset=utf-8" if "/" in ct else ct)
    h = [[b"content-type", _ct.encode()], [b"content-length", str(len(body)).encode()]]
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

def _ls(prefix):
    """List immediate children under a world-name prefix. Like ls.
    prefix="" → top-level names. prefix="photos" → children of photos/.
    Returns sorted list of (child_name, is_dir) tuples."""
    all_names = []
    if DATA.exists():
        for d in sorted(DATA.iterdir()):
            if d.is_dir() and (d / "universe.db").exists():
                all_names.append(_logical_name(d.name))
    children = {}
    pfx = prefix + "/" if prefix else ""
    for w in all_names:
        if pfx and not w.startswith(pfx): continue
        rest = w[len(pfx):] if pfx else w
        if "/" in rest:
            children[rest.split("/")[0]] = True   # directory
        else:
            children[rest] = False                 # file
    return sorted(children.items())

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
    _raw_path = scope["path"]; trailing_slash = _raw_path.endswith("/") and len(_raw_path) > 1
    path = _raw_path.rstrip("/") or "/"; method = scope["method"]
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

    # /bin/* → alias for plugin routes. /bin/grep = /grep. FHS executable namespace.
    # GET /bin → list all plugin routes (like `ls /bin`).
    if path == "/bin" and method == "GET":
        # ls /bin — every plugin route with one-line description
        bins = []
        for route in sorted(_plugins.keys()):
            doc = ""
            h = _plugins[route]
            if callable(h) and h.__doc__:
                doc = h.__doc__.strip().split("\n")[0].strip()
            bins.append({"route": route, "description": doc})
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept": accept = v.decode(); break
        if accept.startswith("text/html"):
            return await send_r(send, 200, INDEX, "text/html", csp=True)
        if "json" in accept:
            return await send_r(send, 200, json.dumps(bins))
        # plain text — one per line, pipe-friendly (curl | grep)
        return await send_r(send, 200, "\n".join(b["route"].lstrip("/") for b in bins) + "\n", "text/plain")
    if path.startswith("/bin/"):
        path = path[4:]  # /bin/grep → /grep
        base_path_override = path.split("?")[0]
    else:
        base_path_override = None
    # Plugin dispatch — exact or prefix match, server gates auth centrally.
    base_path = base_path_override or path.split("?")[0]
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
        # Man page: browser GET, no query params, handler has docstring → show form
        if method == "GET" and not qs:
            accept = ""
            for k, v in scope.get("headers", []):
                if k == b"accept": accept = v.decode(); break
            if accept.startswith("text/html") and callable(handler) and handler.__doc__:
                doc = handler.__doc__.strip()
                route = base_path_override or matched
                # Parse params from docstring: find ?key=value and &key=value patterns
                import re as _re
                _params = list(dict.fromkeys(_re.findall(r'[?&](\w+)=', doc)))  # dedupe, keep order
                _is_post = "POST " in doc or "body" in doc.lower()[:200]
                # Build form fields
                if _is_post:
                    _fields = (f'<textarea name="_body" rows="4" placeholder="body..." '
                               f'style="font:14px monospace;padding:6px;width:95%;display:block;margin:4px 0"></textarea>')
                    _method = "POST"
                    _curl = f'curl -X POST localhost:3005{route} -d "..."'
                else:
                    seen = set()
                    _fields = ""
                    for p in (_params or ["q"]):
                        if p not in seen:
                            seen.add(p)
                            _fields += (f'<div style="margin:4px 0"><label style="display:inline-block;width:80px;font-weight:bold">{p}</label>'
                                        f'<input name="{p}" placeholder="{p}..." style="font:14px monospace;padding:4px;width:60%"></div>')
                    _method = "GET"
                    _qex = "&".join(f"{p}=..." for p in (_params or ["q"]))
                    _curl = f'curl "localhost:3005{route}?{_qex}"'
                _man = (f'<meta charset="utf-8"><div style="font:14px/1.6 system-ui;max-width:700px;margin:2em auto;padding:0 1em">'
                        f'<h2 style="margin:0 0 .5em">{route}</h2>'
                        f'<pre style="white-space:pre-wrap;background:#f5f5f5;padding:1em;border-radius:4px;font-size:13px">{doc}</pre>'
                        f'<div style="margin:8px 0"><code style="background:#e8e8e8;padding:4px 8px;border-radius:3px;font-size:12px">{_curl}</code></div>'
                        f'<form method="{_method}" action="{route}" style="margin-top:1em;padding:1em;background:#fafafa;border:1px solid #eee;border-radius:4px">'
                        f'{_fields}'
                        f'<button style="margin-top:8px;padding:6px 16px;cursor:pointer">{_method} {route}</button></form></div>')
                return await send_r(send, 200, _man, "text/html")
        # If body came from a man-page HTML form, extract the _body field
        if b.startswith("_body="):
            from urllib.parse import unquote_plus
            b = unquote_plus(b[6:])
        params["_scope"] = scope
        params["_body_raw"] = body_raw  # raw bytes for binary plugins (dav PUT)
        params["_send"] = send          # raw send for plugins that stream (SSE)
        result = await handler(method, b, params)
        if result is None: return       # plugin streamed its own response
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
            _hct = "text/html" if re.search(r"<[a-zA-Z/!]", html_body) else "text/plain"
            return await send_r(send, status, html_body, ct=_hct, extra_headers=extra_h or None)
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
        _origin = ""
        for k, v in scope.get("headers", []):
            if k == b"origin": _origin = v.decode(); break
        if _origin and not _origin.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")):
            return await send_r(send, 403, '{"error":"cross-origin rejected"}')
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
    # /proc/worlds — list of worlds (was: /stages)
    if method == "GET" and path == "/proc/worlds":
        stages = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    name = _logical_name(d.name)
                    r = conn(name).execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
                    stages.append({"name": name, "version": r["version"], "updated_at": r["updated_at"]})
        return await send_r(send, 200, json.dumps(stages))
    # /proc/uptime, /proc/version, /proc/status — Unix-style introspection
    if method == "GET" and path == "/proc/uptime":
        return await send_r(send, 200, f"{int(time.time() - _BOOT)}\n", "text/plain")
    if method == "GET" and path == "/proc/version":
        return await send_r(send, 200, f"{VERSION}\n", "text/plain")
    if method == "GET" and path == "/proc/status":
        n = sum(1 for d in DATA.iterdir()
                if d.is_dir() and (d / "universe.db").exists()) if DATA.exists() else 0
        return await send_r(send, 200, json.dumps({
            "pid": os.getpid(), "uptime": int(time.time() - _BOOT),
            "worlds": n, "plugins": len(_plugin_meta), "version": VERSION}))

    # DELETE /home/{name} → approve only → move to .trash
    if method == "DELETE" and len(parts) >= 2 and parts[0] == "home":
        name = "/".join(parts[1:])  # strip "home" prefix
        if not _valid_name(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        db = DATA / _disk_name(name) / "universe.db"
        if not db.exists(): return await send_r(send, 404, '{"error":"world not found"}')
        if _check_auth(scope) != "approve": return await send_r(send, 403, '{"error":"delete requires approve"}')
        if name in _db: _db.pop(name).close()
        trash = DATA / ".trash" / _disk_name(name)
        trash.parent.mkdir(parents=True, exist_ok=True)
        if trash.exists(): shutil.rmtree(trash)
        (DATA / _disk_name(name)).rename(trash)
        return await send_r(send, 200, json.dumps({"deleted": name}))

    # Trailing slash on FHS paths = ls (list children). Like Unix: cd dir/ vs cat file.
    # GET /home/       → ls all user worlds
    # GET /etc/        → ls config worlds
    # GET /home/photos/→ ls worlds under photos/
    _FHS = {"home", "etc", "usr", "var", "boot"}
    _INTERNAL = {"sync", "pending", "result", "clear"}
    if method == "GET" and len(parts) >= 1 and parts[0] in _FHS and trailing_slash:
        # Determine world-name prefix for ls
        if parts[0] == "home":
            ls_prefix = "/".join(parts[1:])  # "home" → "", "home/photos" → "photos"
            # Only show non-system worlds
            entries = [(n, d) for n, d in _ls(ls_prefix)
                       if n not in ("etc","usr","var","boot")]
        else:
            ls_prefix = "/".join(parts)  # "etc" → "etc", "usr/lib" → "usr/lib"
            entries = _ls(ls_prefix)
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept": accept = v.decode(); break
        if accept.startswith("text/html"):
            return await send_r(send, 200, INDEX, "text/html", csp=True)
        if "json" in accept:
            return await send_r(send, 200, json.dumps([{"name": n, "dir": d} for n, d in entries]))
        # plain text — one per line. dirs get trailing /
        lines = [(n + "/" if d else n) for n, d in entries]
        return await send_r(send, 200, "\n".join(lines) + "\n" if lines else "", "text/plain")

    # World routes — HTTP method IS the action.
    #   GET    /home/foo       → read content (no trailing slash)
    #   GET    /home/foo?raw   → raw bytes
    #   PUT    /home/foo       → overwrite
    #   POST   /home/foo       → append
    #   DELETE /home/foo       → handled above
    # If world doesn't exist but children do → 302 redirect to path/
    if len(parts) >= 2 and parts[0] in _FHS:
        # Parse: is last segment an internal op, or part of the world name?
        if len(parts) >= 3 and parts[-1] in _INTERNAL:
            iop = parts[-1]
            name = "/".join(parts[1:-1]) if parts[0] == "home" else parts[0] + "/" + "/".join(parts[1:-1])
        else:
            iop = None
            name = "/".join(parts[1:]) if parts[0] == "home" else "/".join(parts)
        if not _valid_name(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        qs = scope.get("query_string", b"").decode()
        params = dict(x.split("=",1) for x in qs.split("&") if "=" in x) if qs else {}
        # ── Auth gates ──
        if method in ("PUT", "POST"):
            if iop:
                # Internal ops (sync/pending/result/clear): need auth OR same-origin.
                # Browser iframe is same-origin (sends Origin header). curl without
                # auth AND without Origin = blocked. Closes the sync bypass.
                if method != "POST":
                    return await send_r(send, 405, '{"error":"method not allowed"}')
                origin = ""
                for k, v in scope.get("headers", []):
                    if k == b"origin": origin = v.decode(); break
                is_local = origin.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))
                has_auth = _check_auth(scope) is not None
                if origin and not is_local:
                    return await send_r(send, 403, '{"error":"cross-origin rejected"}')
                if not has_auth and not is_local:
                    return await send_r(send, 403, '{"error":"unauthorized"}')
            elif name.startswith(("etc/", "usr/", "var/", "boot/")):
                if _check_auth(scope) != "approve":
                    return await send_r(send, 403, '{"error":"system write requires approve"}')
            elif AUTH_TOKEN and _check_auth(scope) is None:
                return await send_r(send, 403, '{"error":"unauthorized"}')
        # Sensitive-read gate moved into the GET handler below (after
        # browser detection). Browser navigations always get index.html;
        # the iframe's own fetch hits the gate separately.
        # ── GET on internal ops → 405 ──
        if method == "GET" and iop:
            return await send_r(send, 405, '{"error":"method not allowed"}')
        # ── GET: read / raw / browser ──
        if method == "GET":
            # Content negotiation: browser gets index.html, API gets JSON
            accept = ""
            for k, v in scope.get("headers", []):
                if k == b"accept": accept = v.decode(); break
            is_browser = accept.startswith("text/html")
            # Browser always gets the app shell — auth errors show inside iframe
            if is_browser: return await send_r(send, 200, INDEX, "text/html", csp=True)
            # Sensitive-read gate (API only — browser handled above)
            if name == "etc/shadow" or name.startswith("boot/"):
                if _check_auth(scope) != "approve":
                    return await send_r(send, 403, '{"error":"read requires approve"}')
            if not (DATA / _disk_name(name) / "universe.db").exists():
                # World doesn't exist — check if it's a prefix with children → 302 to ls
                children = _ls(name if parts[0] != "home" else "/".join(parts[1:]))
                if children:
                    return await send_r(send, 302, "", extra_headers=[[b"location", (path + "/").encode()]])
                return await send_r(send, 404, '{"error":"world not found"}')
            # ?raw → raw bytes with correct Content-Type
            if "raw" in params or "raw" in qs.split("&"):
                c = conn(name)
                r = c.execute("SELECT stage_html,ext FROM stage_meta WHERE id=1").fetchone()
                body = r["stage_html"] or b""
                if isinstance(body, str): body = body.encode("utf-8")
                ext = r["ext"] or "plain"
                ct = _ext_to_ct(ext)
                total = len(body)
                range_h = ""
                for k, v in scope.get("headers", []):
                    if k == b"range": range_h = v.decode(); break
                if range_h.startswith("bytes="):
                    rp = range_h[6:].split("-", 1)
                    start = int(rp[0]) if rp[0] else 0
                    end = int(rp[1]) if len(rp) > 1 and rp[1] else total - 1
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
            if is_browser: return await send_r(send, 200, INDEX, "text/html", csp=True)
            # JSON read
            c = conn(name)
            r = c.execute("SELECT stage_html,pending_js,js_result,version,ext FROM stage_meta WHERE id=1").fetchone()
            cv = params.get("v")
            if cv:
                try:
                    if int(cv) == r["version"]: return await send_r(send, 304, "")
                except ValueError: pass
            raw = r["stage_html"] or ""
            if isinstance(raw, bytes):
                try: raw = raw.decode("utf-8")
                except UnicodeDecodeError: raw = ""
            ext = r["ext"] or "html"
            return await send_r(send, 200, json.dumps({
                "stage_html": raw, "pending_js": r["pending_js"] or "",
                "js_result": r["js_result"] or "", "version": r["version"],
                "ext": ext, "type": ext}))
        # ── PUT: overwrite ──
        if method == "PUT":
            c = conn(name)
            req_ext = params.get("ext")
            try: body_bytes = await recv(receive)
            except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
            if req_ext and req_ext in _BINARY_EXT:
                b = body_bytes
            else:
                b = body_bytes.decode("utf-8", "replace")
                b = _extract(b, "write")
            cur = c.execute("SELECT ext FROM stage_meta WHERE id=1").fetchone()
            cur_ext = (cur["ext"] if cur else "plain") or "plain"
            new_ext = req_ext or (_infer_type(b) if isinstance(b, str) else cur_ext)
            if (cur_ext == "html" or new_ext == "html") and _check_auth(scope) != "approve":
                return await send_r(send, 403, '{"error":"html write requires approve"}')
            c.execute("UPDATE stage_meta SET stage_html=?,ext=?,version=version+1,updated_at=datetime('now') WHERE id=1",(b, new_ext)); c.commit()
            log_event(name, "stage_written", {"len": len(b), "ext": new_ext})
            return await send_r(send, 200, json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"], "ext": new_ext, "type": new_ext}))
        # ── POST: append or internal op ──
        if method == "POST":
            c = conn(name)
            try: body_bytes = await recv(receive)
            except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
            b = body_bytes.decode("utf-8", "replace")
            if iop == "sync":
                c.execute("UPDATE stage_meta SET stage_html=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            if iop == "pending":
                c.execute("UPDATE stage_meta SET pending_js=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            if iop == "result":
                c.execute("UPDATE stage_meta SET js_result=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            if iop == "clear":
                c.execute("UPDATE stage_meta SET pending_js='',js_result='',updated_at=datetime('now') WHERE id=1"); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            # Append
            req_ext = params.get("ext")
            if req_ext and req_ext in _BINARY_EXT:
                b = body_bytes
            else:
                b = _extract(b, "append")
            cur = c.execute("SELECT ext FROM stage_meta WHERE id=1").fetchone()
            cur_ext = (cur["ext"] if cur else "plain") or "plain"
            if (cur_ext == "html") and _check_auth(scope) != "approve":
                return await send_r(send, 403, '{"error":"html write requires approve"}')
            c.execute("UPDATE stage_meta SET stage_html=stage_html||?,version=version+1,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            log_event(name, "stage_appended", {"len": len(b)})
            return await send_r(send, 200, json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]}))

    if method == "GET": return await send_r(send, 200, INDEX, "text/html", csp=True)
    await send_r(send, 404, '{"error":"not found"}')

# ── Mini ASGI server — zero dependencies ─────────────────────────────

_REASONS = {200:"OK",201:"Created",204:"No Content",206:"Partial Content",
            301:"Moved Permanently",302:"Found",304:"Not Modified",
            400:"Bad Request",401:"Unauthorized",403:"Forbidden",404:"Not Found",
            405:"Method Not Allowed",413:"Payload Too Large",
            500:"Internal Server Error"}

async def _mini_serve(asgi_app, host, port):
    """Zero-dependency ASGI server. Enough for single-user localhost.
    Supports streaming: if a response lacks Content-Length, automatically
    uses Transfer-Encoding: chunked so SSE works without uvicorn."""
    async def handle(reader, writer):
        chunked = False  # set per-response in _send
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
            async def _recv():
                return {"type": "http.request", "body": body}
            async def _send(msg):
                nonlocal chunked
                if msg["type"] == "http.response.start":
                    status = msg["status"]
                    reason = _REASONS.get(status, "OK")
                    hdrs = msg.get("headers", [])
                    has_cl = any(k.lower() == b"content-length" for k, v in hdrs)
                    has_te = any(k.lower() == b"transfer-encoding" for k, v in hdrs)
                    # No length and no transfer-encoding → use chunked so clients can frame.
                    chunked = not has_cl and not has_te
                    out = f"HTTP/1.1 {status} {reason}\r\n".encode()
                    for k, v in hdrs:
                        # Strip Connection: keep-alive — we close after one request.
                        if k.lower() == b"connection" and v.lower() == b"keep-alive":
                            continue
                        out += k + b": " + v + b"\r\n"
                    if chunked: out += b"Transfer-Encoding: chunked\r\n"
                    out += b"Connection: close\r\n\r\n"
                    writer.write(out)
                    await writer.drain()
                elif msg["type"] == "http.response.body":
                    chunk = msg.get("body", b"")
                    more = msg.get("more_body", False)
                    if chunked:
                        if chunk:
                            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        if not more:
                            writer.write(b"0\r\n\r\n")  # end-of-chunked marker
                    elif chunk:
                        writer.write(chunk)
                    await writer.drain()
            await asgi_app(scope, _recv, _send)
        except Exception:
            import traceback; traceback.print_exc()
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n")
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
    except ImportError:
        plugins = None
    if plugins:
        plugins.load_plugins()
        plugins.register_plugin_routes()
        _sync_dir("skills", "*.md", lambda f: f"usr/lib/skills/{f.stem}", "skills")
        _sync_dir("renderers", "renderer-*.html",
                  lambda f: f"usr/lib/renderer/{f.stem[9:]}", "renderers")  # strip "renderer-"
    # /boot — write once at startup, read requires T3.
    # boot/env: which env vars are set (names + non-secret values).
    # boot/grub: which plugins loaded.
    _boot_env = []
    # Allowlist: only show values for these safe keys. Everything else → name only.
    _safe_values = {"ELASTIK_HOST", "ELASTIK_PORT", "ELASTIK_PUBLIC",
                    "ELASTIK_DATA", "ELASTIK_ROOT", "OLLAMA_URL", "OLLAMA_MODEL"}
    for k, v in sorted(os.environ.items()):
        if k.startswith("ELASTIK_") or k.startswith("OLLAMA_"):
            _boot_env.append(f"{k}={v}" if k in _safe_values else f"{k}=***")
    c = conn("boot/env")
    c.execute("UPDATE stage_meta SET stage_html=?,ext='txt',version=version+1,"
              "updated_at=datetime('now') WHERE id=1", ("\n".join(_boot_env),))
    c.commit()
    _boot_grub = [m["name"] for m in _plugin_meta]
    c = conn("boot/grub")
    c.execute("UPDATE stage_meta SET stage_html=?,ext='txt',version=version+1,"
              "updated_at=datetime('now') WHERE id=1", ("\n".join(_boot_grub),))
    c.commit()
    if plugins:
        print(f"\n  elastik -> http://{HOST}:{PORT}\n")
        run(extra_tasks=[plugins.cron_loop()])
    else:
        print(f"\n  elastik -> http://{HOST}:{PORT}  [no plugins.py]\n")
        run()
