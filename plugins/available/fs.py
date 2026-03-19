"""File system plugin — read-only access to allowed directories.

Install: lucy install fs
Configure ALLOWED_DIRS before use.
"""

import os

DESCRIPTION = "File system access (read only)"
ROUTES = {}
PROXY_WHITELIST = {}
PERMISSIONS = ["list: ~/Documents", "read: ~/Documents"]

# Configure allowed directories
ALLOWED_DIRS = [os.path.expanduser("~/Documents")]


async def handle_list(request):
    """List files in a directory."""
    path = request.query_params.get("path", "")
    if not path:
        return {"error": "path parameter required"}

    path = os.path.abspath(path)
    if not any(path.startswith(d) for d in ALLOWED_DIRS):
        return {"error": "path not in allowed directories", "allowed": ALLOWED_DIRS}

    if not os.path.isdir(path):
        return {"error": "not a directory"}

    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(full) else "file",
            "size": os.path.getsize(full) if os.path.isfile(full) else 0,
        })
    return {"path": path, "entries": entries}


async def handle_read(request):
    """Read a file's content."""
    path = request.query_params.get("path", "")
    if not path:
        return {"error": "path parameter required"}

    path = os.path.abspath(path)
    if not any(path.startswith(d) for d in ALLOWED_DIRS):
        return {"error": "path not in allowed directories", "allowed": ALLOWED_DIRS}

    if not os.path.isfile(path):
        return {"error": "not a file"}

    size = os.path.getsize(path)
    if size > 1_000_000:
        return {"error": "file too large", "size": size, "max": 1_000_000}

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"path": path, "content": content, "size": size}
    except UnicodeDecodeError:
        return {"error": "binary file, cannot read as text"}


ROUTES["/proxy/fs/list"] = handle_list
ROUTES["/proxy/fs/read"] = handle_read
handle_list._methods = ["GET"]
handle_read._methods = ["GET"]
