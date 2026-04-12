"""WebDAV — worlds as files. Optional data pipe for editors."""
DESCRIPTION = "/dav/ → WebDAV surface. PROPFIND/GET/PUT/DELETE over worlds."
AUTH = "approve"  # server gates at dispatch — plugin only sees authenticated calls
from email.utils import formatdate
import server

_EXT = {"html":".html","plain":".txt","markdown":".md","md":".md","json":".json","css":".css","js":".js","py":".py"}

def _ext(typ): return _EXT.get(typ or 'plain', '.txt')

def _world_name(path):
    rest = path[4:].lstrip("/")  # strip /dav
    if not rest: return ""
    dot = rest.rfind(".")
    return rest[:dot] if dot > 0 else rest

def _read(name):
    c = server.conn(name)
    r = c.execute("SELECT stage_html,type FROM stage_meta WHERE id=1").fetchone()
    html = r["stage_html"] if r and r["stage_html"] else ""
    typ = (r["type"] if r else 'plain') or 'plain'
    if typ != 'plain': html = f":::type:{typ}:::\n" + html
    return html, typ

def _prop(href, restype, ct, size, mod):
    rt = "<D:resourcetype><D:collection/></D:resourcetype>" if restype == "collection" else "<D:resourcetype/>"
    ctl = f"<D:getcontenttype>{ct}</D:getcontenttype>" if ct else ""
    return (f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
            f"{rt}<D:getcontentlength>{size}</D:getcontentlength>"
            f"<D:getlastmodified>{mod}</D:getlastmodified>"
            f"{ctl}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


async def handle(method, body, params):
    scope = params.get("_scope", {})
    path = scope.get("path", "/dav")
    now = formatdate(usegmt=True)

    if method == "OPTIONS":
        return {"_body":"", "_ct":"text/plain",
                "_headers":[["dav","1"],["allow","OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND"]]}

    if method == "PROPFIND":
        depth = "1"
        for k, v in scope.get("headers", []):
            if k == b"depth": depth = v.decode(); break
        name = _world_name(path)
        if name:
            if not server._VALID_NAME.match(name) or not (server.DATA / name / "universe.db").exists():
                return {"error":"not found", "_status":404}
            html, typ = _read(name)
            xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                   + _prop(f"/dav/{name}{_ext(typ)}", "", "text/plain", len(html), now)
                   + '</D:multistatus>')
            return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
               + _prop("/dav/", "collection", "", 0, now))
        if depth == "1" and server.DATA.exists():
            for d in sorted(server.DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    html, typ = _read(d.name)
                    xml += _prop(f"/dav/{d.name}{_ext(typ)}", "", "text/plain", len(html), now)
        xml += '</D:multistatus>'
        return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}

    if method in ("GET", "HEAD"):
        name = _world_name(path)
        if not name:
            html = ('<h1>elastik WebDAV</h1>'
                    '<p style="background:#fee;padding:.5em;border:1px solid #c00">'
                    'AI-generated content -- treat all links as hostile.</p><ul>')
            if server.DATA.exists():
                for d in sorted(server.DATA.iterdir()):
                    if d.is_dir() and (d / "universe.db").exists():
                        _, typ = _read(d.name)
                        html += f'<li><a href="/dav/{d.name}{_ext(typ)}">{d.name}</a> <em>({typ})</em></li>'
            html += "</ul>"
            return {"_html": html}
        if not server._VALID_NAME.match(name) or not (server.DATA / name / "universe.db").exists():
            return {"error":"not found", "_status":404}
        body, _ = _read(name)
        return {"_body":body, "_ct":"text/plain"}

    if method == "PUT":
        name = _world_name(path)
        if not name: return {"error":"PUT on collection not supported", "_status":405}
        if not server._VALID_NAME.match(name): return {"error":"invalid world name", "_status":400}
        b = body.decode("utf-8","replace") if isinstance(body, bytes) else (body or "")
        c = server.conn(name)
        new_type, b = server._parse_type(b)
        c.execute("UPDATE stage_meta SET stage_html=?,type=?,version=version+1,updated_at=datetime('now') WHERE id=1", (b, new_type)); c.commit()
        server.log_event(name, "stage_written", {"len":len(b), "type":new_type})
        return {"_status":201, "_body":"", "_ct":"text/plain"}

    if method == "DELETE":
        name = _world_name(path)
        if not name: return {"error":"DELETE on collection not supported", "_status":405}
        if not server._VALID_NAME.match(name) or not (server.DATA / name / "universe.db").exists():
            return {"error":"not found", "_status":404}
        c = server.conn(name)
        c.execute("UPDATE stage_meta SET stage_html='',pending_js='',js_result='',updated_at=datetime('now') WHERE id=1"); c.commit()
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "MKCOL":
        return {"error":"worlds are flat — no subdirectories", "_status":405}
    if method in ("LOCK", "UNLOCK"):
        return {"_body":"", "_ct":"text/plain", "_status":501}
    return {"error":"method not allowed", "_status":405}


ROUTES = ["/dav"]
