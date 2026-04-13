"""WebDAV — worlds as files. Optional data pipe for editors."""
DESCRIPTION = "/dav/ → WebDAV surface. PROPFIND/GET/PUT/DELETE over worlds."
AUTH = "none"  # reads are public (like core routes). writes check inline.
from email.utils import formatdate
import hmac as _hmac, os
import server

def _ext(typ):
    if typ == "html": return ".html.txt"  # file:// has no sandbox
    if typ and typ != "plain": return "." + typ
    return ".txt"

def _check_write_auth(scope):
    """Write ops need Bearer or Basic auth. Read is open."""
    return server._check_auth(scope) is not None

def _world_name(path):
    rest = path[4:].lstrip("/")  # strip /dav
    if not rest: return ""
    dot = rest.find(".")  # first dot — world names have no dots
    return rest[:dot] if dot > 0 else rest

def _read(name):
    c = server.conn(name)
    r = c.execute("SELECT stage_html,ext FROM stage_meta WHERE id=1").fetchone()
    raw = r["stage_html"] if r and r["stage_html"] else b""
    if isinstance(raw, str): raw = raw.encode("utf-8")
    ext = (r["ext"] if r else "html") or "html"
    return raw, ext

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
        # Build list of all worlds (logical names)
        all_worlds = []
        if server.DATA.exists():
            for d in sorted(server.DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    all_worlds.append(server._logical_name(d.name))
        # Determine prefix from path: /dav/photos/ → "photos"
        prefix = _world_name(path)
        if prefix and prefix.endswith("/"): prefix = prefix[:-1]
        # Single world (not a virtual dir prefix)
        if prefix and any(w == prefix for w in all_worlds):
            raw, ext = _read(prefix)
            # ext=dir or has children → it's a collection (folder)
            is_dir = ext == "dir" or any(w.startswith(prefix + "/") for w in all_worlds)
            if not is_dir:
                xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                       + _prop(f"/dav/{prefix}{_ext(ext)}", "", "text/plain", len(raw), now)
                       + '</D:multistatus>')
                return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}
        # Collection listing (root or virtual dir)
        href = f"/dav/{prefix}/" if prefix else "/dav/"
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
               + _prop(href, "collection", "", 0, now))
        if depth == "1":
            # Filter worlds under this prefix, group by next level
            children_prefix = prefix + "/" if prefix else ""
            seen_dirs = set()
            for w in all_worlds:
                if children_prefix and not w.startswith(children_prefix):
                    continue
                if not children_prefix:
                    rest = w
                else:
                    rest = w[len(children_prefix):]
                if "/" in rest:
                    # Virtual subdirectory — show as collection
                    subdir = rest.split("/")[0]
                    if subdir not in seen_dirs:
                        seen_dirs.add(subdir)
                        xml += _prop(f"/dav/{children_prefix}{subdir}/", "collection", "", 0, now)
                else:
                    # Direct child — file or ext=dir folder
                    raw, ext = _read(w)
                    if ext == "dir":
                        xml += _prop(f"/dav/{w}/", "collection", "", 0, now)
                    else:
                        xml += _prop(f"/dav/{w}{_ext(ext)}", "", "text/plain", len(raw), now)
        xml += '</D:multistatus>'
        return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}

    if method in ("GET", "HEAD"):
        name = _world_name(path)
        if not name:
            listing = ('<h1>elastik WebDAV</h1>'
                    '<p style="background:#fee;padding:.5em;border:1px solid #c00">'
                    'AI-generated content -- treat all links as hostile.</p><ul>')
            if server.DATA.exists():
                for d in sorted(server.DATA.iterdir()):
                    if d.is_dir() and (d / "universe.db").exists():
                        wname = server._logical_name(d.name)
                        _, ext = _read(wname)
                        listing += f'<li><a href="/dav/{wname}{_ext(ext)}">{wname}</a> <em>({ext})</em></li>'
            listing += "</ul>"
            return {"_html": listing}
        if not server._valid_name(name) or not (server.DATA / server._disk_name(name) / "universe.db").exists():
            return {"error":"not found", "_status":404}
        raw, _ = _read(name)
        return {"_body":raw, "_ct":"text/plain"}

    if method in ("PUT", "DELETE") and not _check_write_auth(scope):
        return {"error":"authentication required", "_status":401,
                "_headers":[["www-authenticate",'Basic realm="elastik"']]}

    if method == "PUT":
        name = _world_name(path)
        if not name: return {"error":"PUT on collection not supported", "_status":405}
        if not server._valid_name(name): return {"error":"invalid world name", "_status":400}
        # Use raw bytes from params (plugin dispatch preserves them)
        raw = params.get("_body_raw", body.encode("utf-8") if isinstance(body, str) else body or b"")
        # Ext from filename: /dav/photo.png → ext=png
        dot = path.rfind(".")
        ext = path[dot+1:].lower().strip() if dot > 0 else "plain"
        if not ext: ext = "plain"
        if ext == "html" and server._check_auth(scope) != "approve":
            return {"error": "html write requires approve", "_status": 403}
        c = server.conn(name)
        c.execute("UPDATE stage_meta SET stage_html=?,ext=?,version=version+1,updated_at=datetime('now') WHERE id=1", (raw, ext)); c.commit()
        server.log_event(name, "stage_written", {"len":len(raw), "ext":ext})
        return {"_status":201, "_body":"", "_ct":"text/plain"}

    if method == "DELETE":
        name = _world_name(path)
        if not name: return {"error":"DELETE on collection not supported", "_status":405}
        if not server._valid_name(name) or not (server.DATA / server._disk_name(name) / "universe.db").exists():
            return {"error":"not found", "_status":404}
        c = server.conn(name)
        c.execute("UPDATE stage_meta SET stage_html='',pending_js='',js_result='',updated_at=datetime('now') WHERE id=1"); c.commit()
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "MKCOL":
        name = _world_name(path)
        if not name: return {"_status":201, "_body":"", "_ct":"text/plain"}
        if not server._valid_name(name): return {"_status":201, "_body":"", "_ct":"text/plain"}
        c = server.conn(name)
        c.execute("UPDATE stage_meta SET ext='dir',updated_at=datetime('now') WHERE id=1"); c.commit()
        return {"_status":201, "_body":"", "_ct":"text/plain"}
    if method in ("LOCK", "UNLOCK"):
        return {"_body":"", "_ct":"text/plain", "_status":501}
    return {"error":"method not allowed", "_status":405}


ROUTES = ["/dav"]
