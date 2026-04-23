"""Built-in WebDAV surface for elastik.

This lives in plugins/ even though it is auto-registered at boot,
because it is a server-side behaviour layer rather than core dispatch
plumbing. Keeping it here shrinks server.py and makes the future step
("DAV becomes fully opt-in /lib plugin") much easier.
"""

import hashlib
import json
import shutil
import sqlite3
import time
from email.utils import formatdate
from urllib.parse import unquote, urlparse

import server

AUTH = "none"
ROUTES = ["/dav"]


# Auth-elevated namespaces for writes. `lib/` intentionally stays out so
# DAV PUT /dav/lib/<n> matches core PUT /lib/<n> (T2 allowed, state reset
# handles approval rebinding separately).
_DAV_SYS_PREFIXES = ("etc/", "usr/", "var/", "boot/", "tmp/", "mnt/")
# Top-level namespaces surfaced as /dav/<ns>/ collections and excluded
# from /dav/home/'s user-content set. `lib/` belongs here so plugins have
# their own collection instead of double-aliasing under /dav/home/lib/.
_DAV_TOP_NAMESPACES = _DAV_SYS_PREFIXES + ("lib/",)


def _dav_suffix(rest, ext):
    last = rest.rsplit("/", 1)[-1]
    if "." in last:
        return ""
    if not ext or ext == "dir":
        return ""
    if ext == "html":
        return ".html.txt"
    if ext == "plain":
        return ".txt"
    return f".{ext}"


def _dav_world_name(path):
    """DAV URL -> world name. Identity first, strip-and-retry fallback."""
    rest = path[4:].lstrip("/").rstrip("/")
    if rest.startswith("home/"):
        rest = rest[5:]
    elif rest == "home":
        rest = ""
    if not rest:
        return ""
    candidate = rest
    for _ in range(3):
        if (server.DATA / server._disk_name(candidate) / "universe.db").exists():
            return candidate
        segs = candidate.split("/")
        last = segs[-1]
        dot = last.rfind(".")
        if dot <= 0:
            break
        candidate = "/".join(segs[:-1] + [last[:dot]])
    return rest


def _dav_read(name):
    """Bytes-safe world read for DAV listings and file reads."""
    db_path = server.DATA / server._disk_name(name) / "universe.db"
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.text_factory = bytes
    try:
        r = c.execute("SELECT stage_html,ext FROM stage_meta WHERE id=1").fetchone()
    finally:
        c.close()
    raw = r["stage_html"] if r and r["stage_html"] else b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    ext = (r["ext"] if r else b"html") or b"html"
    if isinstance(ext, bytes):
        ext = ext.decode("utf-8", "replace")
    return raw, ext


def _dav_prop(href, restype, ct, size, mod):
    rt = "<D:resourcetype><D:collection/></D:resourcetype>" if restype == "collection" else "<D:resourcetype/>"
    ctl = f"<D:getcontenttype>{ct}</D:getcontenttype>" if ct else ""
    return (f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
            f"{rt}<D:getcontentlength>{size}</D:getcontentlength>"
            f"<D:getlastmodified>{mod}</D:getlastmodified>"
            f"{ctl}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _all_worlds():
    if not server.DATA.exists():
        return []
    return [
        server._logical_name(d.name)
        for d in sorted(server.DATA.iterdir())
        if d.is_dir() and (d / "universe.db").exists()
    ]


async def handle(method, body, params):
    scope = params.get("_scope", {})
    path = scope.get("path", "/dav")
    now = formatdate(usegmt=True)

    if method == "OPTIONS":
        return {"_body": "", "_ct": "text/plain",
                "_headers": [["dav", "1"],
                             ["allow", "OPTIONS, GET, HEAD, PUT, DELETE, MOVE, COPY, PROPFIND, MKCOL"]]}

    raw_rest = path[4:].strip("/")
    if raw_rest == "home":
        dav_prefix = "home"
    elif raw_rest.startswith("home/"):
        dav_prefix = raw_rest
    else:
        dav_prefix = raw_rest

    def _write_auth_ok():
        return server._check_auth(scope) is not None

    if method == "PROPFIND":
        depth = "1"
        for k, v in scope.get("headers", []):
            if k == b"depth":
                depth = v.decode()
                break
        all_worlds = _all_worlds()
        world = _dav_world_name(path)
        if world and world.endswith("/"):
            world = world[:-1]
        is_world = world and any(w == world for w in all_worlds)
        has_children = bool(world) and any(w.startswith(world + "/") for w in all_worlds)
        is_top_ns = (dav_prefix in ("", "home") or dav_prefix in (p.rstrip("/") for p in _DAV_TOP_NAMESPACES))
        if world and not is_world and not has_children and not is_top_ns:
            return {"error": "not found", "_status": 404}
        if is_world and not has_children:
            raw, ext = _dav_read(world)
            is_dir = ext == "dir"
            if not is_dir:
                dav_href = f"/dav/home/{world}" if not world.startswith(_DAV_TOP_NAMESPACES) else f"/dav/{world}"
                xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                       + _dav_prop(f"{dav_href}{_dav_suffix(world, ext)}", "", server._ext_to_ct(ext), len(raw), now)
                       + '</D:multistatus>')
                return {"_body": xml, "_ct": "application/xml; charset=utf-8", "_status": 207}
        href = f"/dav/{dav_prefix}/" if dav_prefix else "/dav/"
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
               + _dav_prop(href, "collection", "", 0, now))
        if depth == "1":
            if dav_prefix == "":
                has_user = any(not w.startswith(_DAV_TOP_NAMESPACES) for w in all_worlds)
                if has_user:
                    xml += _dav_prop("/dav/home/", "collection", "", 0, now)
                for pref in _DAV_TOP_NAMESPACES:
                    if any(w.startswith(pref) for w in all_worlds):
                        xml += _dav_prop(f"/dav/{pref}", "collection", "", 0, now)
            else:
                if dav_prefix == "home":
                    world_prefix = ""
                    children_href = "/dav/home/"
                    candidates = [w for w in all_worlds if not w.startswith(_DAV_TOP_NAMESPACES)]
                elif dav_prefix.startswith("home/"):
                    world_prefix = dav_prefix[5:] + "/"
                    children_href = f"/dav/{dav_prefix}/"
                    candidates = [w for w in all_worlds
                                  if not w.startswith(_DAV_TOP_NAMESPACES) and w.startswith(world_prefix)]
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
                            xml += _dav_prop(f"{children_href}{subdir}/", "collection", "", 0, now)
                    else:
                        raw, ext = _dav_read(w)
                        if ext == "dir":
                            if rest not in seen_dirs:
                                seen_dirs.add(rest)
                                xml += _dav_prop(f"{children_href}{rest}/", "collection", "", 0, now)
                        else:
                            xml += _dav_prop(
                                f"{children_href}{rest}{_dav_suffix(rest, ext)}",
                                "", server._ext_to_ct(ext), len(raw), now)
        xml += '</D:multistatus>'
        return {"_body": xml, "_ct": "application/xml; charset=utf-8", "_status": 207}

    if method in ("GET", "HEAD"):
        name = _dav_world_name(path)
        all_worlds = _all_worlds()
        has_children = bool(name) and any(w.startswith(name + "/") for w in all_worlds)
        if not name or has_children:
            listing = (f'<h1>elastik WebDAV — /{dav_prefix or ""}</h1>'
                       '<p style="background:#fee;padding:.5em;border:1px solid #c00">'
                       'AI-generated content -- treat all links as hostile.</p><ul>')
            if all_worlds:
                if dav_prefix == "":
                    if any(not w.startswith(_DAV_TOP_NAMESPACES) for w in all_worlds):
                        listing += '<li><a href="/dav/home/">home/</a></li>'
                    for pref in _DAV_TOP_NAMESPACES:
                        if any(w.startswith(pref) for w in all_worlds):
                            listing += f'<li><a href="/dav/{pref}">{pref}</a></li>'
                else:
                    if dav_prefix == "home":
                        world_prefix = ""
                        href_base = "/dav/home/"
                        cands = [w for w in all_worlds if not w.startswith(_DAV_TOP_NAMESPACES)]
                    elif dav_prefix.startswith("home/"):
                        world_prefix = dav_prefix[5:] + "/"
                        href_base = f"/dav/{dav_prefix}/"
                        cands = [w for w in all_worlds
                                 if not w.startswith(_DAV_TOP_NAMESPACES) and w.startswith(world_prefix)]
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
                            _, ext = _dav_read(w)
                            listing += f'<li><a href="{href_base}{rest}{_dav_suffix(rest, ext)}">{rest}</a> <em>({ext})</em></li>'
            listing += "</ul>"
            return {"_html": listing}
        if not server._valid_name(name) or not (server.DATA / server._disk_name(name) / "universe.db").exists():
            return {"error": "not found", "_status": 404}
        if (name == "etc/shadow" or name.startswith("boot/")) and server._check_auth(scope) != "approve":
            return {"error": "read requires approve", "_status": 403}
        raw, ext = _dav_read(name)
        return {"_body": raw, "_ct": server._ext_to_ct(ext)}

    if method in ("PUT", "DELETE", "MOVE", "COPY", "MKCOL") and not _write_auth_ok():
        return {"error": "authentication required", "_status": 401,
                "_headers": [["www-authenticate", 'Basic realm="elastik"']]}

    if method == "PUT":
        name = _dav_world_name(path)
        if not name:
            return {"error": "PUT on collection not supported", "_status": 405}
        if not server._valid_name(name):
            return {"error": "invalid world name", "_status": 400}
        raw = params.get("_body_raw", body.encode("utf-8") if isinstance(body, str) else body or b"")
        ext = params.get("ext")
        if not ext:
            ct = ""
            for k, v in scope.get("headers", []):
                if k == b"content-type":
                    ct = v.decode("utf-8", "replace").split(";")[0].strip().lower()
                    break
            ct_to_ext = {v: k for k, v in server._CT.items()}
            ext = ct_to_ext.get(ct, "")
        if not ext:
            last_seg = path.rstrip("/").rsplit("/", 1)[-1]
            dot = last_seg.rfind(".")
            if dot > 0:
                maybe_ext = last_seg[dot + 1:].lower()
                if maybe_ext in server._CT:
                    ext = maybe_ext
        if not ext:
            ext = "plain"
        if ext == "html" and server._check_auth(scope) != "approve":
            return {"error": "html write requires approve", "_status": 403}
        if name.startswith(_DAV_SYS_PREFIXES) and server._check_auth(scope) != "approve":
            return {"error": "system write requires approve", "_status": 403}
        c = server.conn(name)
        meta = server._extract_meta_headers(scope)
        is_lib = name.startswith("lib/")
        cur = c.execute("SELECT state FROM stage_meta WHERE id=1").fetchone()
        prev_state = ((cur["state"] if cur else "pending") or "pending") if is_lib else None
        if is_lib and prev_state != "pending":
            c.execute("UPDATE stage_meta SET stage_html=?,ext=?,headers=?,state='pending',version=version+1,updated_at=datetime('now') WHERE id=1", (raw, ext, meta))
        else:
            c.execute("UPDATE stage_meta SET stage_html=?,ext=?,headers=?,version=version+1,updated_at=datetime('now') WHERE id=1", (raw, ext, meta))
        c.commit()
        ver = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
        body_hash = hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode("utf-8")).hexdigest()
        server.log_event(name, "stage_written", {
            "op": "put",
            "len": len(raw),
            "ext": ext,
            "version_after": ver,
            "meta_headers": json.loads(meta or "[]"),
            "body_sha256_after": body_hash,
        })
        if is_lib and prev_state and prev_state != "pending":
            server.log_event(name, "state_transition", {
                "from": prev_state, "to": "pending",
                "version": ver, "reason": "source replaced"})
        return {"_status": 201, "_body": "", "_ct": "text/plain"}

    if method == "DELETE":
        name = _dav_world_name(path)
        if not name:
            return {"error": "DELETE requires a name", "_status": 405}
        if not server._valid_name(name):
            return {"error": "invalid world name", "_status": 400}
        targets = []
        if (server.DATA / server._disk_name(name) / "universe.db").exists():
            targets.append(name)
        for w in _all_worlds():
            if w.startswith(name + "/"):
                targets.append(w)
        if not targets:
            return {"error": "not found", "_status": 404}
        needs_approve = any(t.startswith(_DAV_TOP_NAMESPACES) for t in targets)
        if needs_approve and server._check_auth(scope) != "approve":
            return {"error": "system delete requires approve", "_status": 403}
        for w in targets:
            server._release_world(w)
            server._move_to_trash(w)
        return {"_status": 204, "_body": "", "_ct": "text/plain"}

    if method == "MOVE":
        src_name = _dav_world_name(path)
        if not src_name:
            return {"error": "MOVE requires a source name", "_status": 405}
        if not server._valid_name(src_name):
            return {"error": "invalid source name", "_status": 400}
        src_disk = server.DATA / server._disk_name(src_name)
        if not (src_disk / "universe.db").exists():
            return {"error": "source not found", "_status": 404}
        dest_raw, overwrite = "", True
        for k, v in scope.get("headers", []):
            if k == b"destination":
                dest_raw = v.decode("utf-8", "replace")
            elif k == b"overwrite":
                overwrite = v.decode().strip().upper() == "T"
        if not dest_raw:
            return {"error": "Destination header required", "_status": 400}
        dest_path = unquote(urlparse(dest_raw).path or dest_raw)
        dst_name = _dav_world_name(dest_path)
        if not dst_name or not server._valid_name(dst_name):
            return {"error": "invalid destination", "_status": 400}
        if (src_name.startswith(_DAV_SYS_PREFIXES) or dst_name.startswith(_DAV_SYS_PREFIXES)) and server._check_auth(scope) != "approve":
            return {"error": "system move requires approve", "_status": 403}
        dst_disk = server.DATA / server._disk_name(dst_name)
        if dst_disk.exists():
            if not overwrite:
                return {"error": "destination exists", "_status": 412}
            shutil.rmtree(dst_disk)
        server._release_world(src_name)
        server._release_world(dst_name)
        dst_disk.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(5):
            try:
                src_disk.rename(dst_disk)
                break
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
        else:
            shutil.move(str(src_disk), str(dst_disk))
        if dst_name.startswith("lib/"):
            dc = server.conn(dst_name)
            dr = dc.execute("SELECT state,version FROM stage_meta WHERE id=1").fetchone()
            prev = ((dr["state"] if dr else "pending") or "pending")
            if prev != "pending":
                dc.execute("UPDATE stage_meta SET state='pending',updated_at=datetime('now') WHERE id=1")
                dc.commit()
                server.log_event(dst_name, "state_transition", {
                    "from": prev, "to": "pending",
                    "version": dr["version"] if dr else 0,
                    "reason": "source replaced (via MOVE)"})
        server.log_event(dst_name, "stage_moved", {"from": src_name})
        return {"_status": 204, "_body": "", "_ct": "text/plain"}

    if method == "COPY":
        src_name = _dav_world_name(path)
        if not src_name:
            return {"error": "COPY requires a source name", "_status": 405}
        if not server._valid_name(src_name):
            return {"error": "invalid source name", "_status": 400}
        src_disk = server.DATA / server._disk_name(src_name)
        src_is_world = (src_disk / "universe.db").exists()
        dest_raw, overwrite = "", True
        for k, v in scope.get("headers", []):
            if k == b"destination":
                dest_raw = v.decode("utf-8", "replace")
            elif k == b"overwrite":
                overwrite = v.decode().strip().upper() == "T"
        if not dest_raw:
            return {"error": "Destination header required", "_status": 400}
        dest_path = unquote(urlparse(dest_raw).path or dest_raw)
        dst_name = _dav_world_name(dest_path)
        if not dst_name or not server._valid_name(dst_name):
            return {"error": "invalid destination", "_status": 400}
        pairs = []
        if src_is_world:
            pairs.append((src_name, dst_name))
        for w in _all_worlds():
            if w.startswith(src_name + "/"):
                pairs.append((w, dst_name + w[len(src_name):]))
        if not pairs:
            return {"error": "source not found", "_status": 404}
        touches_sys = any(s.startswith(_DAV_SYS_PREFIXES) or d.startswith(_DAV_SYS_PREFIXES) for s, d in pairs)
        if touches_sys and server._check_auth(scope) != "approve":
            return {"error": "system copy requires approve", "_status": 403}
        if not overwrite:
            for _, dw in pairs:
                if (server.DATA / server._disk_name(dw) / "universe.db").exists():
                    return {"error": "destination exists", "_status": 412}
        for sw, dw in pairs:
            src_db = server.DATA / server._disk_name(sw) / "universe.db"
            dst_dir = server.DATA / server._disk_name(dw)
            dst_db = dst_dir / "universe.db"
            if sw in server._db:
                try:
                    server._db[sw].execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    server._db[sw].commit()
                except Exception:
                    pass
            if dst_dir.exists():
                if dw in server._db:
                    server._db.pop(dw).close()
                shutil.rmtree(dst_dir)
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_db, dst_db)
            if dw.startswith("lib/"):
                dc = server.conn(dw)
                dr = dc.execute("SELECT state,version FROM stage_meta WHERE id=1").fetchone()
                prev = ((dr["state"] if dr else "pending") or "pending")
                if prev != "pending":
                    dc.execute("UPDATE stage_meta SET state='pending',updated_at=datetime('now') WHERE id=1")
                    dc.commit()
                    server.log_event(dw, "state_transition", {
                        "from": prev, "to": "pending",
                        "version": dr["version"] if dr else 0,
                        "reason": "source replaced (via COPY)"})
            server.log_event(dw, "stage_copied", {"from": sw})
        return {"_status": 204, "_body": "", "_ct": "text/plain"}

    if method == "MKCOL":
        name = _dav_world_name(path)
        if not name:
            return {"_status": 201, "_body": "", "_ct": "text/plain"}
        if not server._valid_name(name):
            return {"_status": 201, "_body": "", "_ct": "text/plain"}
        c = server.conn(name)
        c.execute("UPDATE stage_meta SET ext='dir',updated_at=datetime('now') WHERE id=1")
        c.commit()
        return {"_status": 201, "_body": "", "_ct": "text/plain"}

    if method in ("LOCK", "UNLOCK"):
        return {"_body": "", "_ct": "text/plain", "_status": 501}

    return {"error": "method not allowed", "_status": 405}
