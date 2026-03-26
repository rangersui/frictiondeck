"""elastik — reference implementation. ~100 lines. One dependency."""
import hashlib, hmac as _hmac, json, os, re, secrets, sqlite3
from pathlib import Path

DATA, PLUGINS = Path("data"), Path("plugins")
KEY = os.getenv("ELASTIK_KEY", "elastik-dev-key").encode()
APPROVE_TOKEN = os.getenv("ELASTIK_TOKEN", "") or secrets.token_hex(16)
HOST = os.getenv("ELASTIK_HOST", "0.0.0.0")
PORT = int(os.getenv("ELASTIK_PORT", "3004"))
MAX_BODY = 5 * 1024 * 1024
INDEX = Path(__file__).with_name("index.html").read_text()
OPENAPI = Path(__file__).with_name("openapi.json").read_text()
SW = Path(__file__).with_name("sw.js").read_text()
def _csp():
    cdn = "https:"
    try:
        if (DATA / "config-cdn").exists():
            c = conn("config-cdn")
            r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            if r and r["stage_html"] and r["stage_html"].strip():
                domains = [d.strip() for d in r["stage_html"].splitlines() if d.strip()]
                cdn = " ".join(f"https://{d}" for d in domains)
    except Exception: pass
    return (f"default-src 'self' data: blob:; script-src 'unsafe-inline' 'unsafe-eval' {cdn} data:; "
            f"style-src 'unsafe-inline' {cdn} data:; img-src * data: blob:; font-src * data:; "
            f"connect-src 'self'; worker-src 'self'")
_VALID_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')
_db = {}

def conn(name):
    if name not in _db:
        d = DATA / name; d.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(d / "universe.db"), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL")
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
    h = [[b"content-type", ct.encode()]]
    if csp: h.append([b"content-security-policy", _csp().encode()])
    if extra_headers: h.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": h})
    await send({"type": "http.response.body", "body": data.encode() if isinstance(data, str) else data})

_plugins, _auth, _plugin_meta = {}, None, []

def load_plugin(name):
    """Load or reload a single plugin by name."""
    global _auth
    f = PLUGINS / f"{name}.py"
    if not f.exists():
        src = PLUGINS / "available" / f"{name}.py"
        if src.exists():
            PLUGINS.mkdir(exist_ok=True)
            f.write_text(src.read_text())
            print(f"  installed from available: {name}")
        else:
            print(f"  not found: {name}"); return
    try:
        ns = {"conn": conn, "log_event": log_event, "load_plugin": load_plugin,
              "unload_plugin": unload_plugin, "_plugins": _plugins, "_plugin_meta": _plugin_meta}
        exec(f.read_text(), ns)
        # Remove old routes for this plugin
        old = next((m for m in _plugin_meta if m["name"] == name), None)
        if old:
            for r in old["routes"]: _plugins.pop(r, None)
            _plugin_meta[:] = [m for m in _plugin_meta if m["name"] != name]
        # Register new routes
        routes = list(ns.get("ROUTES", {}).keys())
        for path, handler in ns.get("ROUTES", {}).items():
            _plugins[path] = handler
        if "AUTH_MIDDLEWARE" in ns: _auth = ns["AUTH_MIDDLEWARE"]
        _plugin_meta.append({"name": name, "description": ns.get("DESCRIPTION", ""),
            "routes": routes, "params": ns.get("PARAMS_SCHEMA", {}), "ops": ns.get("OPS_SCHEMA", [])})
        print(f"  loaded: {name} ({routes})")
    except Exception as e: print(f"  error loading {name}: {e}")

def unload_plugin(name):
    """Unload a plugin — remove its routes."""
    global _auth
    meta = next((m for m in _plugin_meta if m["name"] == name), None)
    if not meta: print(f"  not loaded: {name}"); return
    for r in meta["routes"]: _plugins.pop(r, None)
    if name == "auth" or "auth" in meta.get("description", "").lower(): _auth = None
    _plugin_meta[:] = [m for m in _plugin_meta if m["name"] != name]
    print(f"  unloaded: {name}")


def load_plugins():
    """Load all plugins at startup. Install defaults if empty."""
    installed = [f for f in PLUGINS.glob("*.py") if not f.name.startswith("_")] if PLUGINS.exists() else []
    if not installed:
        available = PLUGINS / "available"
        if available.exists():
            PLUGINS.mkdir(exist_ok=True)
            for name in ["admin.py", "auth.py"]:
                src = available / name
                if src.exists():
                    (PLUGINS / name).write_text(src.read_text())
                    print(f"  installed default: {name}")
    if not PLUGINS.exists(): return
    for f in PLUGINS.glob("*.py"):
        if not f.name.startswith("_"): load_plugin(f.stem)

async def app(scope, receive, send):
    if scope["type"] != "http": return
    path = scope["path"].rstrip("/") or "/"; method = scope["method"]
    raw = scope.get("raw_path", b"").decode("utf-8", "replace")
    if '..' in path or '//' in path or '..' in raw or '//' in raw:
        return await send_r(send, 400, '{"error":"invalid path"}')
    if _auth and not await _auth(scope, path, method):
        return await send_r(send, 403, '{"error":"unauthorized"}')
    parts = [p for p in path.split("/") if p]

    base_path = path.split("?")[0]
    if base_path in _plugins:
        b = await recv(receive)
        qs = scope.get("query_string", b"").decode()
        params = dict(x.split("=",1) for x in qs.split("&") if "=" in x) if qs else {}
        result = await _plugins[base_path](method, b, params)
        status = result.pop("_status", 200); redirect = result.pop("_redirect", None)
        cookies = result.pop("_cookies", []); html_body = result.pop("_html", None)
        extra_h = [[b"set-cookie", c.encode()] for c in cookies]
        if redirect: extra_h.append([b"location", redirect.encode()]); status = 302
        if html_body: return await send_r(send, status, html_body, ct="text/html", extra_headers=extra_h or None)
        return await send_r(send, status, json.dumps(result), extra_headers=extra_h or None)

    if method == "GET" and path == "/openapi.json": return await send_r(send, 200, OPENAPI)
    if method == "GET" and path == "/sw.js": return await send_r(send, 200, SW, "application/javascript")
    if method == "GET" and path == "/info":
        skills = ""
        sp = Path(__file__).with_name("SKILLS.md")
        if sp.exists(): skills = sp.read_text()
        auth_name = next((p["name"] for p in _plugin_meta if p["name"] == "auth" or "auth" in p.get("description","").lower()), None)
        renderers, worlds = [], []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    if d.name.startswith("renderer-"): renderers.append(d.name)
                    elif not d.name.startswith("config-"): worlds.append(d.name)
        cdn_raw = ""
        try:
            if (DATA / "config-cdn").exists():
                r = conn("config-cdn").execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
                if r: cdn_raw = r["stage_html"]
        except Exception: pass
        cdn = [d.strip() for d in cdn_raw.splitlines() if d.strip()] if cdn_raw.strip() else ["* (all HTTPS)"]
        return await send_r(send, 200, json.dumps({
            "routes": list(_plugins.keys()),
            "auth": auth_name,
            "plugins": _plugin_meta,
            "renderers": renderers,
            "worlds": worlds,
            "cdn": cdn,
            "skills": skills,
        }))
    if method == "GET" and path == "/stages":
        stages = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    r = conn(d.name).execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
                    stages.append({"name": d.name, "version": r["version"], "updated_at": r["updated_at"]})
        return await send_r(send, 200, json.dumps(stages))

    if method == "POST" and len(parts) == 2 and parts[0] == "webhook":
        try: b = (await recv(receive)).decode("utf-8", "replace")
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        log_event("default", "webhook_received", {"source": parts[1], "body": b})
        return await send_r(send, 200, '{"ok":true}')

    if method == "POST" and len(parts) == 2 and parts[0] == "plugins":
        try: b = json.loads(await recv(receive))
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        if parts[1] == "propose":
            log_event("default", "plugin_proposed", b)
            return await send_r(send, 200, '{"ok":true}')
        if parts[1] == "approve":
            tok = dict(scope.get("headers", [])).get(b"x-approve-token", b"").decode()
            if tok != APPROVE_TOKEN: return await send_r(send, 403, '{"error":"invalid token"}')
            n, code = b.get("name", ""), b.get("code", "")
            if n and code:
                PLUGINS.mkdir(exist_ok=True); (PLUGINS / f"{n}.py").write_text(code)
                load_plugin(n)
                log_event("default", "plugin_approved", {"name": n})
            return await send_r(send, 200, '{"ok":true}')

    if len(parts) == 2 and parts[1] in ("read","write","append","pending","result","clear","sync"):
        name, action = parts
        if not _VALID_NAME.match(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        c = conn(name)
        if method == "GET" and action == "read":
            r = c.execute("SELECT stage_html,pending_js,js_result,version FROM stage_meta WHERE id=1").fetchone()
            return await send_r(send, 200, json.dumps(dict(r)))
        try: b = (await recv(receive)).decode("utf-8", "replace")
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        b = _extract(b, action)
        if action == "write":
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            log_event(name, "stage_written", {"len": len(b)})
            return await send_r(send, 200, json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]}))
        if action == "append":
            old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",(old+b,)); c.commit()
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

if __name__ == "__main__":
    load_plugins()
    print(f"\n  elastik → http://{HOST}:{PORT}\n  approve token: {APPROVE_TOKEN}\n")
    import uvicorn; uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
