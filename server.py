"""elastik — reference implementation. ~100 lines. One dependency."""
import hashlib, hmac as _hmac, json, os, secrets, sqlite3
from pathlib import Path

DATA, PLUGINS = Path("data"), Path("plugins")
KEY = os.getenv("ELASTIK_KEY", "elastik-dev-key").encode()
TOKEN = secrets.token_hex(16)
HOST = os.getenv("ELASTIK_HOST", "0.0.0.0")
PORT = int(os.getenv("ELASTIK_PORT", "3004"))
INDEX = Path(__file__).with_name("index.html").read_text()
OPENAPI = Path(__file__).with_name("openapi.json").read_text()
CSP = "default-src 'self' data: blob:; script-src 'unsafe-inline' 'unsafe-eval' https: data:; style-src 'unsafe-inline' https: data:; img-src * data: blob:; font-src * data:; connect-src 'self'"
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
              (etype, p, h, prev))
    c.commit()

def apply_patch(html, ops):
    """Apply a list of string operations to html. Returns (new_html, applied_count)."""
    count = 0
    for op in ops:
        t = op.get("op")
        if t == "insert":
            pos = op.get("pos", 0)
            pos = max(0, min(pos, len(html)))
            html = html[:pos] + op.get("text", "") + html[pos:]
            count += 1
        elif t == "delete":
            start = max(0, op.get("start", 0))
            end = min(len(html), op.get("end", start))
            html = html[:start] + html[end:]
            count += 1
        elif t == "replace":
            find = op.get("find", "")
            text = op.get("text", "")
            n = op.get("count", 1)
            if find:
                html = html.replace(find, text, n)
                count += 1
        elif t == "replace_all":
            find = op.get("find", "")
            text = op.get("text", "")
            if find:
                html = html.replace(find, text)
                count += 1
        elif t == "slice":
            start = op.get("start", 0)
            end = op.get("end", len(html))
            html = html[start:end]
            count += 1
        elif t == "prepend":
            html = op.get("text", "") + html
            count += 1
        elif t == "regex_replace":
            import re
            pattern = op.get("pattern", "")
            text = op.get("text", "")
            n = op.get("count", 0)
            if pattern:
                html = re.sub(pattern, text, html, count=n)
                count += 1
    return html, count

async def recv(receive):
    b = b""
    while True:
        m = await receive(); b += m.get("body", b"")
        if not m.get("more_body"): return b

async def send_r(send, status, data, ct="application/json", csp=False):
    h = [[b"content-type", ct.encode()]]
    if csp: h.append([b"content-security-policy", CSP.encode()])
    await send({"type": "http.response.start", "status": status, "headers": h})
    await send({"type": "http.response.body", "body": data.encode() if isinstance(data, str) else data})

_plugins = {}
def load_plugins():
    if not PLUGINS.exists(): return
    for f in PLUGINS.glob("*.py"):
        if f.name.startswith("_"): continue
        try:
            ns = {}; exec(f.read_text(), ns)
            for path, handler in ns.get("ROUTES", {}).items():
                _plugins[path] = handler; print(f"  plugin: {path}")
        except Exception as e: print(f"  plugin {f.name} error: {e}")

async def app(scope, receive, send):
    if scope["type"] != "http": return
    path = scope["path"].rstrip("/") or "/"; method = scope["method"]
    parts = [p for p in path.split("/") if p]

    base_path = path.split("?")[0]
    if base_path in _plugins:
        b = await recv(receive)
        qs = scope.get("query_string", b"").decode()
        params = dict(x.split("=",1) for x in qs.split("&") if "=" in x) if qs else {}
        result = await _plugins[base_path](method, b, params)
        return await send_r(send, 200, json.dumps(result))

    if method == "GET" and path == "/openapi.json":
        return await send_r(send, 200, OPENAPI)

    if method == "GET" and path == "/stages":
        stages = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    r = conn(d.name).execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
                    stages.append({"name": d.name, "version": r["version"], "updated_at": r["updated_at"]})
        return await send_r(send, 200, json.dumps(stages))

    if method == "POST" and len(parts) == 2 and parts[0] == "webhook":
        b = (await recv(receive)).decode("utf-8", "replace")
        log_event("default", "webhook_received", {"source": parts[1], "body": b})
        return await send_r(send, 200, '{"ok":true}')

    if method == "POST" and len(parts) == 2 and parts[0] == "plugins":
        b = json.loads(await recv(receive))
        if parts[1] == "propose":
            log_event("default", "plugin_proposed", b)
            return await send_r(send, 200, '{"ok":true}')
        if parts[1] == "approve":
            tok = dict(scope.get("headers", [])).get(b"x-approve-token", b"").decode()
            if tok != TOKEN: return await send_r(send, 403, '{"error":"invalid token"}')
            n, code = b.get("name", ""), b.get("code", "")
            if n and code:
                PLUGINS.mkdir(exist_ok=True); (PLUGINS / f"{n}.py").write_text(code)
                log_event("default", "plugin_approved", {"name": n})
            return await send_r(send, 200, '{"ok":true}')

    if len(parts) == 2 and parts[1] in ("read","write","append","patch","pending","result","clear","sync"):
        name, action = parts; c = conn(name)
        if method == "GET" and action == "read":
            r = c.execute("SELECT stage_html,pending_js,js_result,version FROM stage_meta WHERE id=1").fetchone()
            return await send_r(send, 200, json.dumps(dict(r)))
        b = (await recv(receive)).decode("utf-8", "replace")
        if action == "write":
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            log_event(name, "stage_written", {"len": len(b)})
            v = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
            return await send_r(send, 200, json.dumps({"version": v}))
        if action == "append":
            old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",(old+b,)); c.commit()
            log_event(name, "stage_appended", {"len": len(b)})
            v = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
            return await send_r(send, 200, json.dumps({"version": v}))
        if action == "patch":
            try:
                ops = json.loads(b).get("ops", [])
            except json.JSONDecodeError:
                return await send_r(send, 400, '{"error":"invalid JSON"}')
            old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
            new_html, applied = apply_patch(old, ops)
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",(new_html,)); c.commit()
            log_event(name, "stage_patched", {"ops": len(ops), "applied": applied})
            v = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
            return await send_r(send, 200, json.dumps({"version": v, "applied": applied, "length": len(new_html)}))
        if action == "sync":
            c.execute("UPDATE stage_meta SET stage_html=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            return await send_r(send, 200, '{"ok":true}')
        if action == "pending":
            print(f"PENDING: writing {b!r}", flush=True)
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
    print(f"\n  elastik → http://{HOST}:{PORT}\n  approve token: {TOKEN}\n")
    import uvicorn; uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
