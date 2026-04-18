"""WebDAV — FHS tree over worlds. Mount it, cd home."""
DESCRIPTION = "/dav/ → FHS WebDAV surface. PROPFIND/GET/PUT/DELETE over worlds."
AUTH = "none"  # reads are public (like core routes). writes check inline.
from email.utils import formatdate
import hmac as _hmac, os
import server

# DAV URL → world name mirrors the HTTP scheme: /dav/home/foo ↔ GET /home/foo.
# Worlds with these prefixes are system worlds; /home/ is the user namespace
# and gets stripped on the wire (world "foo" is displayed at /dav/home/foo).
_SYS_PREFIXES = ("etc/", "usr/", "var/", "boot/", "tmp/", "mnt/")

def _check_write_auth(scope):
    """Write ops need Bearer or Basic auth. Read is open."""
    return server._check_auth(scope) is not None

def _world_name(path):
    """DAV URL → world name. Identity. Path IS name.

      /dav/home/foo.png                   → 'foo.png'
      /dav/home/docs/v2.0/notes.md        → 'docs/v2.0/notes.md'
      /dav/home/foo.txt/bar.txt           → 'foo.txt/bar.txt'
      /dav/etc/gpu.conf                   → 'etc/gpu.conf'
      /dav/foo                            → 'foo' (legacy)

    No dot stripping. No extension inference. Paths go through unchanged
    after the /dav and home/ URL-prefix sugar is removed. This matches
    HTTP: PUT /home/foo.txt creates world 'foo.txt', PUT /dav/home/foo.txt
    also creates world 'foo.txt'. Same namespace. No divergence.

    Every prior "smart" strip rule (first-dot, last-dot, per-segment)
    produced its own class of collision or MKCOL/PUT inconsistency. The
    blind-pipe reading is: the pipe does not parse, and path is path.
    """
    rest = path[4:].lstrip("/").rstrip("/")  # strip /dav + trailing slash
    if rest.startswith("home/"): rest = rest[5:]  # home/ is URL-only sugar
    elif rest == "home": rest = ""                # /dav/home/ itself is the user root
    return rest

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
                "_headers":[["dav","1"],["allow","OPTIONS, GET, HEAD, PUT, DELETE, MOVE, COPY, PROPFIND, MKCOL"]]}

    # Strip /dav from path, normalise into a "DAV-level prefix" that mirrors
    # the FHS: "" (root), "home", "home/sub", "etc", "usr/lib", etc.
    # /dav/home/... is sugar — internally we use the real world-name form.
    raw_rest = path[4:].strip("/")
    if raw_rest == "home": dav_prefix = "home"
    elif raw_rest.startswith("home/"): dav_prefix = raw_rest  # keep for href
    else: dav_prefix = raw_rest  # system prefix or legacy flat

    if method == "PROPFIND":
        depth = "1"
        for k, v in scope.get("headers", []):
            if k == b"depth": depth = v.decode(); break
        all_worlds = []
        if server.DATA.exists():
            for d in sorted(server.DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    all_worlds.append(server._logical_name(d.name))
        # Single real world (not a prefix) → describe it as a file or collection
        world = _world_name(path)
        if world and world.endswith("/"): world = world[:-1]
        if world and any(w == world for w in all_worlds):
            raw, ext = _read(world)
            is_dir = ext == "dir" or any(w.startswith(world + "/") for w in all_worlds)
            if not is_dir:
                dav_href = f"/dav/home/{world}" if not world.startswith(_SYS_PREFIXES) else f"/dav/{world}"
                xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                       + _prop(dav_href, "", server._ext_to_ct(ext), len(raw), now)
                       + '</D:multistatus>')
                return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}
        # Collection listing — root, /home/, /etc/, virtual dir inside a namespace.
        href = f"/dav/{dav_prefix}/" if dav_prefix else "/dav/"
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
               + _prop(href, "collection", "", 0, now))
        if depth == "1":
            if dav_prefix == "":
                # Root: synthesise /home/ and any system namespace that has worlds.
                has_user = any(not w.startswith(_SYS_PREFIXES) for w in all_worlds)
                if has_user:
                    xml += _prop("/dav/home/", "collection", "", 0, now)
                for pref in _SYS_PREFIXES:
                    if any(w.startswith(pref) for w in all_worlds):
                        xml += _prop(f"/dav/{pref}", "collection", "", 0, now)
            else:
                # List worlds under this DAV prefix.
                # world_prefix = the world-namespace prefix; children_href = DAV href base.
                if dav_prefix == "home":
                    world_prefix = ""
                    children_href = "/dav/home/"
                    # Only non-system worlds
                    candidates = [w for w in all_worlds if not w.startswith(_SYS_PREFIXES)]
                elif dav_prefix.startswith("home/"):
                    world_prefix = dav_prefix[5:] + "/"
                    children_href = f"/dav/{dav_prefix}/"
                    candidates = [w for w in all_worlds
                                  if not w.startswith(_SYS_PREFIXES) and w.startswith(world_prefix)]
                else:
                    world_prefix = dav_prefix + "/"
                    children_href = f"/dav/{dav_prefix}/"
                    candidates = [w for w in all_worlds if w.startswith(world_prefix)]
                seen_dirs = set()
                for w in candidates:
                    rest = w[len(world_prefix):] if world_prefix else w
                    if "/" in rest:
                        subdir = rest.split("/")[0]
                        if subdir not in seen_dirs:
                            seen_dirs.add(subdir)
                            xml += _prop(f"{children_href}{subdir}/", "collection", "", 0, now)
                    else:
                        raw, ext = _read(w)
                        if ext == "dir":
                            if rest not in seen_dirs:
                                seen_dirs.add(rest)
                                xml += _prop(f"{children_href}{rest}/", "collection", "", 0, now)
                        else:
                            xml += _prop(f"{children_href}{rest}", "", server._ext_to_ct(ext), len(raw), now)
        xml += '</D:multistatus>'
        return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}

    if method in ("GET", "HEAD"):
        name = _world_name(path)
        if not name:
            # Render an HTML index of whatever dav_prefix is pointing at.
            listing = (f'<h1>elastik WebDAV — /{dav_prefix or ""}</h1>'
                    '<p style="background:#fee;padding:.5em;border:1px solid #c00">'
                    'AI-generated content -- treat all links as hostile.</p><ul>')
            if server.DATA.exists():
                all_worlds = [server._logical_name(d.name) for d in sorted(server.DATA.iterdir())
                              if d.is_dir() and (d / "universe.db").exists()]
                if dav_prefix == "":
                    if any(not w.startswith(_SYS_PREFIXES) for w in all_worlds):
                        listing += '<li><a href="/dav/home/">home/</a></li>'
                    for pref in _SYS_PREFIXES:
                        if any(w.startswith(pref) for w in all_worlds):
                            listing += f'<li><a href="/dav/{pref}">{pref}</a></li>'
                else:
                    # Same mapping as PROPFIND listing
                    if dav_prefix == "home":
                        world_prefix = ""
                        href_base = "/dav/home/"
                        cands = [w for w in all_worlds if not w.startswith(_SYS_PREFIXES)]
                    elif dav_prefix.startswith("home/"):
                        world_prefix = dav_prefix[5:] + "/"
                        href_base = f"/dav/{dav_prefix}/"
                        cands = [w for w in all_worlds
                                 if not w.startswith(_SYS_PREFIXES) and w.startswith(world_prefix)]
                    else:
                        world_prefix = dav_prefix + "/"
                        href_base = f"/dav/{dav_prefix}/"
                        cands = [w for w in all_worlds if w.startswith(world_prefix)]
                    seen = set()
                    for w in cands:
                        rest = w[len(world_prefix):] if world_prefix else w
                        if "/" in rest:
                            first = rest.split("/")[0]
                            if first not in seen:
                                seen.add(first)
                                listing += f'<li><a href="{href_base}{first}/">{first}/</a></li>'
                        else:
                            _, ext = _read(w)
                            listing += f'<li><a href="{href_base}{rest}">{rest}</a> <em>({ext})</em></li>'
            listing += "</ul>"
            return {"_html": listing}
        if not server._valid_name(name) or not (server.DATA / server._disk_name(name) / "universe.db").exists():
            return {"error":"not found", "_status":404}
        if (name == "etc/shadow" or name.startswith("boot/")) and server._check_auth(scope) != "approve":
            return {"error":"read requires approve", "_status":403}
        raw, ext = _read(name)
        return {"_body":raw, "_ct":server._ext_to_ct(ext)}

    if method in ("PUT", "DELETE", "MOVE", "COPY") and not _check_write_auth(scope):
        return {"error":"authentication required", "_status":401,
                "_headers":[["www-authenticate",'Basic realm="elastik"']]}

    if method == "PUT":
        name = _world_name(path)
        if not name: return {"error":"PUT on collection not supported", "_status":405}
        if not server._valid_name(name): return {"error":"invalid world name", "_status":400}
        # Use raw bytes from params (plugin dispatch preserves them)
        raw = params.get("_body_raw", body.encode("utf-8") if isinstance(body, str) else body or b"")
        # ext comes from ?ext= query (explicit) or Content-Type header (client's
        # own MIME hint). No parsing of URL path — the path is the name, dots
        # are just bytes in the name. Default "plain".
        ext = params.get("ext")
        if not ext:
            ct = ""
            for k, v in scope.get("headers", []):
                if k == b"content-type":
                    ct = v.decode("utf-8", "replace").split(";")[0].strip().lower()
                    break
            # Reverse the server._CT table so we can go content-type → ext.
            ct_to_ext = {v: k for k, v in server._CT.items()}
            ext = ct_to_ext.get(ct, "plain")
        if ext == "html" and server._check_auth(scope) != "approve":
            return {"error": "html write requires approve", "_status": 403}
        if name.startswith(_SYS_PREFIXES) and server._check_auth(scope) != "approve":
            return {"error": "system write requires approve", "_status": 403}
        c = server.conn(name)
        c.execute("UPDATE stage_meta SET stage_html=?,ext=?,version=version+1,updated_at=datetime('now') WHERE id=1", (raw, ext)); c.commit()
        server.log_event(name, "stage_written", {"len":len(raw), "ext":ext})
        return {"_status":201, "_body":"", "_ct":"text/plain"}

    if method == "DELETE":
        name = _world_name(path)
        if not name: return {"error":"DELETE requires a name", "_status":405}
        if not server._valid_name(name): return {"error":"invalid world name", "_status":400}
        # Collect targets: the world itself (if exists) + any worlds under name/.
        # Matches HTTP DELETE: "rm -rf prefix/" semantics.
        targets = []
        if (server.DATA / server._disk_name(name) / "universe.db").exists():
            targets.append(name)
        if server.DATA.exists():
            for d in sorted(server.DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    w = server._logical_name(d.name)
                    if w.startswith(name + "/"):
                        targets.append(w)
        if not targets:
            return {"error":"not found", "_status":404}
        # Tiered auth mirrors PUT: system paths → T3, user paths → T2.
        needs_approve = any(t.startswith(_SYS_PREFIXES) for t in targets)
        if needs_approve and server._check_auth(scope) != "approve":
            return {"error":"system delete requires approve", "_status":403}
        import shutil
        for w in targets:
            if w in server._db: server._db.pop(w).close()
            trash = server.DATA / ".trash" / server._disk_name(w)
            trash.parent.mkdir(parents=True, exist_ok=True)
            if trash.exists(): shutil.rmtree(trash)
            (server.DATA / server._disk_name(w)).rename(trash)
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "MOVE":
        # Rename / move a world. WinSCP: rename file, drag file to new folder.
        src_name = _world_name(path)
        if not src_name:
            return {"error":"MOVE requires a source name", "_status":405}
        if not server._valid_name(src_name):
            return {"error":"invalid source name", "_status":400}
        src_disk = server.DATA / server._disk_name(src_name)
        if not (src_disk / "universe.db").exists():
            return {"error":"source not found", "_status":404}
        # Destination + Overwrite headers (RFC 4918 §9.9).
        dest_raw, overwrite = "", True
        for k, v in scope.get("headers", []):
            if k == b"destination": dest_raw = v.decode("utf-8", "replace")
            elif k == b"overwrite": overwrite = v.decode().strip().upper() == "T"
        if not dest_raw:
            return {"error":"Destination header required", "_status":400}
        from urllib.parse import urlparse, unquote
        dest_path = unquote(urlparse(dest_raw).path or dest_raw)
        dst_name = _world_name(dest_path)
        if not dst_name or not server._valid_name(dst_name):
            return {"error":"invalid destination", "_status":400}
        # Tiered auth: T3 if either endpoint touches a system prefix.
        if (src_name.startswith(_SYS_PREFIXES) or dst_name.startswith(_SYS_PREFIXES)) \
           and server._check_auth(scope) != "approve":
            return {"error":"system move requires approve", "_status":403}
        dst_disk = server.DATA / server._disk_name(dst_name)
        if dst_disk.exists():
            if not overwrite:
                return {"error":"destination exists", "_status":412}
            import shutil
            shutil.rmtree(dst_disk)
        if src_name in server._db: server._db.pop(src_name).close()
        if dst_name in server._db: server._db.pop(dst_name).close()
        dst_disk.parent.mkdir(parents=True, exist_ok=True)
        src_disk.rename(dst_disk)
        server.log_event(dst_name, "stage_moved", {"from": src_name})
        # 201 if dst didn't exist before; 204 if it did (and overwrite was OK).
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "COPY":
        # Duplicate a world (or prefix subtree) to a new path. RFC 4918 §9.8.
        # Depth: infinity by default — a collection copy takes all children.
        src_name = _world_name(path)
        if not src_name:
            return {"error":"COPY requires a source name", "_status":405}
        if not server._valid_name(src_name):
            return {"error":"invalid source name", "_status":400}
        src_disk = server.DATA / server._disk_name(src_name)
        src_is_world = (src_disk / "universe.db").exists()
        # Destination + Overwrite headers.
        dest_raw, overwrite = "", True
        for k, v in scope.get("headers", []):
            if k == b"destination": dest_raw = v.decode("utf-8", "replace")
            elif k == b"overwrite": overwrite = v.decode().strip().upper() == "T"
        if not dest_raw:
            return {"error":"Destination header required", "_status":400}
        from urllib.parse import urlparse, unquote
        dest_path = unquote(urlparse(dest_raw).path or dest_raw)
        dst_name = _world_name(dest_path)
        if not dst_name or not server._valid_name(dst_name):
            return {"error":"invalid destination", "_status":400}
        # Build the (src, dst) pairs to copy. Includes src itself if it's a
        # world, plus any descendants under src_name/. Empty = 404.
        pairs = []
        if src_is_world:
            pairs.append((src_name, dst_name))
        if server.DATA.exists():
            for d in sorted(server.DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    w = server._logical_name(d.name)
                    if w.startswith(src_name + "/"):
                        pairs.append((w, dst_name + w[len(src_name):]))
        if not pairs:
            return {"error":"source not found", "_status":404}
        # Tiered auth: T3 if either any src or any dst touches a system prefix.
        touches_sys = any(s.startswith(_SYS_PREFIXES) or d.startswith(_SYS_PREFIXES) for s, d in pairs)
        if touches_sys and server._check_auth(scope) != "approve":
            return {"error":"system copy requires approve", "_status":403}
        # Precheck: fail fast if any destination exists and Overwrite: F.
        if not overwrite:
            for _, dw in pairs:
                if (server.DATA / server._disk_name(dw) / "universe.db").exists():
                    return {"error":"destination exists", "_status":412}
        import shutil, sqlite3
        for sw, dw in pairs:
            src_db = server.DATA / server._disk_name(sw) / "universe.db"
            dst_dir = server.DATA / server._disk_name(dw)
            dst_db = dst_dir / "universe.db"
            # Checkpoint src so its WAL is merged into universe.db. File-level
            # copy of universe.db then captures a consistent state. Without
            # this, copying a world with uncheckpointed writes would lose data
            # (the same class of bug that ate /etc/ before the WAL fix).
            if sw in server._db:
                try: server._db[sw].execute("PRAGMA wal_checkpoint(TRUNCATE)"); server._db[sw].commit()
                except Exception: pass
            if dst_dir.exists():
                if dw in server._db: server._db.pop(dw).close()
                shutil.rmtree(dst_dir)
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_db, dst_db)
            server.log_event(dw, "stage_copied", {"from": sw})
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
