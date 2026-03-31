"""File system plugin — access to allowed directories.

Install: lucy install fs
Configure ALLOWED_DIRS before use.
Handler signature: async def handler(method, body, params) -> dict
"""

import os
from pathlib import Path

DESCRIPTION = "File system access (read + write)"
ROUTES = {}
ALLOWED_DIRS = [str(Path(__file__).resolve().parents[2])]

PARAMS_SCHEMA = {
    "/proxy/fs/list": {
        "method": "POST",
        "params": {
            "path": {"type": "string", "required": False, "description": "Directory path, default '.'"}
        },
        "example": {"path": "./data"},
        "returns": {"files": ["string"]}
    },
    "/proxy/fs/read": {
        "method": "POST",
        "params": {
            "path": {"type": "string", "required": True, "description": "File path to read"}
        },
        "example": {"path": "server.py"},
        "returns": {"content": "string"}
    },
    "/proxy/fs/write": {
        "method": "POST",
        "params": {
            "path": {"type": "string", "required": True, "description": "File path to write"},
            "content": {"type": "string", "required": True, "description": "File content"}
        },
        "example": {"path": "test.txt", "content": "hello"},
        "returns": {"ok": "boolean"}
    },
}


def _safe_path(path):
    """Resolve and validate path against ALLOWED_DIRS. Returns (resolved, error)."""
    if not path: return None, "path parameter required"
    if "\x00" in path: return None, "invalid path"
    resolved = str(Path(os.path.abspath(path)).resolve())
    if not any(Path(resolved).is_relative_to(Path(d).resolve()) for d in ALLOWED_DIRS):
        return None, "path not in allowed directories"
    if Path(resolved).is_relative_to(Path(__file__).resolve().parents[1]):
        return None, "plugins directory is restricted"
    return resolved, None


async def handle_list(method, body, params):
    path = params.get("path", "")
    path, err = _safe_path(path)
    if err: return {"error": err, "allowed": ALLOWED_DIRS}
    if not os.path.isdir(path): return {"error": "not a directory"}
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file",
                        "size": os.path.getsize(full) if os.path.isfile(full) else 0})
    return {"path": path, "entries": entries}


async def handle_read(method, body, params):
    path = params.get("path", "")
    path, err = _safe_path(path)
    if err: return {"error": err, "allowed": ALLOWED_DIRS}
    if not os.path.isfile(path): return {"error": "not a file"}
    size = os.path.getsize(path)
    if size > 1_000_000: return {"error": "file too large", "size": size}
    try:
        with open(path, "r", encoding="utf-8") as f: content = f.read()
        return {"path": path, "content": content, "size": size}
    except UnicodeDecodeError: return {"error": "binary file"}


async def handle_write(method, body, params):
    path = params.get("path", "")
    path, err = _safe_path(path)
    if err: return {"error": err}
    content = body.decode("utf-8") if isinstance(body, bytes) else body
    with open(path, "w", encoding="utf-8") as f: f.write(content)
    return {"ok": True, "path": path, "size": len(content)}

ROUTES["/proxy/fs/write"] = handle_write
ROUTES["/proxy/fs/list"] = handle_list
ROUTES["/proxy/fs/read"] = handle_read
