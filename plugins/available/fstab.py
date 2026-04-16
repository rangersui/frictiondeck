"""fstab — mount local directories under /mnt/*.

Gate: etc/fstab world. One line per mount:
  /Users/ranger/projects  /mnt/work  rw
  /home/ranger/docs       /mnt/docs  ro

Write etc/fstab → T3 (approve). That IS the permission model.
No container check. No mode flag. You control what's mounted.

Routes:
  GET  /mnt/                 list mount points
  GET  /mnt/{name}/          list directory
  GET  /mnt/{name}/path/file read file
  POST /mnt/{name}/path/file write file (rw only, needs auth token)
"""
DESCRIPTION = "/mnt/ — local filesystem via etc/fstab"
AUTH = "none"
import os, json
from pathlib import Path
import server

_MAX_FILE = 5 * 1024 * 1024  # 5 MB


def _parse_fstab():
    """Read etc/fstab world → list of (local_path, mount_name, mode)."""
    try:
        raw = server.conn("etc/fstab").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
    except Exception:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    mounts = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Parse from the right: mode is last, mount_point second-to-last,
        # everything before is the local path (may contain spaces).
        parts = line.rsplit(None, 2)
        if len(parts) < 3:
            continue
        local_path, mount_point, mode = parts[0], parts[1], parts[2].lower()
        # mount_point must be /mnt/{name}
        if not mount_point.startswith("/mnt/"):
            continue
        name = mount_point[5:].strip("/")
        if not name:
            continue
        if mode not in ("ro", "rw"):
            mode = "ro"
        mounts.append((local_path, name, mode))
    return mounts


def _find_mount(mounts, name):
    """Find mount by name → (local_path, mode) or None."""
    for local_path, mname, mode in mounts:
        if mname == name:
            return local_path, mode
    return None


def _safe_resolve(root, rel):
    """Resolve rel under root. Reject traversal. Returns (abs_path, err)."""
    if not rel or rel == "/":
        return root, None
    # reject ..
    for part in rel.replace("\\", "/").split("/"):
        if part == "..":
            return None, "traversal denied"
    full = os.path.normpath(os.path.join(root, rel))
    # must stay under root — use commonpath, not startswith
    # (startswith "/mnt/root" would accept "/mnt/root_evil/...")
    try:
        if os.path.commonpath([full, os.path.normpath(root)]) != os.path.normpath(root):
            return None, "traversal denied"
    except ValueError:
        return None, "traversal denied"
    return full, None


async def handle(method, body, params):
    scope = params.get("_scope", {})
    path = scope.get("path", "/mnt")
    rest = path[4:].strip("/")  # strip /mnt

    mounts = _parse_fstab()

    # GET /mnt/ — list mount points
    if not rest:
        return {"mounts": [{"name": m[1], "path": m[0], "mode": m[2]} for m in mounts]}

    # Split: /mnt/{name}/subpath
    slash = rest.find("/")
    if slash < 0:
        mount_name, subpath = rest, ""
    else:
        mount_name, subpath = rest[:slash], rest[slash + 1:]

    found = _find_mount(mounts, mount_name)
    if not found:
        return {"error": f"mount not found: {mount_name}. write etc/fstab first.", "_status": 404}
    local_root, mode = found

    if not os.path.isdir(local_root):
        return {"error": f"mount path does not exist: {local_root}", "_status": 500}

    full, err = _safe_resolve(local_root, subpath)
    if err:
        return {"error": err, "_status": 403}

    # --- writes ---
    if method == "POST":
        if mode != "rw":
            return {"error": "mount is read-only", "_status": 403}
        if not server._check_auth(scope):
            return {"error": "unauthorized", "_status": 401,
                    "_headers": [["www-authenticate", 'Basic realm="elastik"']]}
        raw = params.get("_body_raw", body.encode("utf-8") if isinstance(body, str) else body or b"")
        if len(raw) > _MAX_FILE:
            return {"error": "file too large", "_status": 413}
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(raw)
        return {"ok": True, "path": subpath, "size": len(raw)}

    # --- reads ---
    if not os.path.exists(full):
        return {"error": "not found", "_status": 404}

    # directory listing
    if os.path.isdir(full):
        entries = []
        try:
            for name in sorted(os.listdir(full)):
                if name.startswith("."):
                    continue  # skip dotfiles
                fp = os.path.join(full, name)
                entries.append({
                    "name": name,
                    "type": "dir" if os.path.isdir(fp) else "file",
                    "size": os.path.getsize(fp) if os.path.isfile(fp) else 0,
                })
        except PermissionError:
            return {"error": "permission denied", "_status": 403}
        return {"path": subpath or "/", "mount": mount_name, "mode": mode, "entries": entries}

    # file read
    size = os.path.getsize(full)
    if size > _MAX_FILE:
        return {"error": "file too large", "size": size, "_status": 413}
    try:
        with open(full, "rb") as f:
            raw = f.read()
        # try text
        try:
            text = raw.decode("utf-8")
            return {"path": subpath, "content": text, "size": size}
        except UnicodeDecodeError:
            # binary — return as raw bytes
            import base64
            return {"path": subpath, "size": size, "binary": True,
                    "content_b64": base64.b64encode(raw).decode()}
    except PermissionError:
        return {"error": "permission denied", "_status": 403}


ROUTES = ["/mnt"]
