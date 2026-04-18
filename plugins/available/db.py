"""/dev/db — read-only SQL against any world's SQLite. Device, not command.

POST /dev/db?world=work         body: SELECT * FROM stage_meta
POST /dev/db                    body: SELECT * FROM stage_meta  (default: all worlds index)

Read-only. file:...?mode=ro enforced at connection level.
SELECT/PRAGMA/WITH/EXPLAIN only. Keyword whitelist is belt; ro mode is suspenders.
1000 row cap. 2 second timeout. No traversal outside DATA.

This is the self-referential device. elastik querying its own SQLite
through its own HTTP through its own plugin. The query gets logged.
The log can be queried. The observer observes itself observing.
"""
DESCRIPTION = "/dev/db — read-only SQL on world databases"
AUTH = "none"  # GET renders man page (browser) / 405 (curl). POST checks auth inline.
ROUTES = {}

import json, os, sqlite3
from pathlib import Path
import server

_DATA = Path(os.environ.get("ELASTIK_DATA", "data")).resolve()


def _disk_name(name):
    return name.replace("/", "%2F")


def _resolve_mnt(file_path):
    """Resolve a file path like 'brave/History' against fstab mounts.
    Returns absolute Path if allowed, None if not under any mount."""
    # Read etc/fstab for mount definitions
    fstab_db = _DATA / "etc%2Ffstab" / "universe.db"
    if not fstab_db.exists():
        return None
    try:
        c = sqlite3.connect(str(fstab_db))
        c.row_factory = sqlite3.Row
        raw = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        c.close()
    except Exception:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    # Parse fstab: local_path  /mnt/name  mode
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 2)
        if len(parts) < 3:
            continue
        local_path, mount_point, _ = parts
        name = mount_point.replace("/mnt/", "").strip("/")
        if not name:
            continue
        # Check if file_path starts with this mount name
        if file_path.startswith(name + "/") or file_path == name:
            rest = file_path[len(name):].lstrip("/")
            full = os.path.normpath(os.path.join(local_path, rest))
            # Traversal check — commonpath, not startswith
            try:
                if os.path.commonpath([full, os.path.normpath(local_path)]) != os.path.normpath(local_path):
                    return None
            except ValueError:
                return None
            return Path(full)
    return None


async def handle_db(method, body, params):
    """/dev/db — SELECT-only SQL. Read-only connection. Bounded."""
    if method != "POST":
        return {"error": "POST only — body=SQL, response=JSON/text",
                "_status": 405}
    # Query is read-only, but anon reads of browser history would still leak.
    # Gate inline so browser GET can render man page (plugin AUTH="none").
    scope = params.get("_scope", {})
    if not server._check_auth(scope):
        return {"error": "auth required — T2 token or cap token scoped to /dev/db",
                "_status": 401,
                "_headers": [["www-authenticate", 'Basic realm="elastik"']]}
    sql = (body if isinstance(body, str) else body.decode("utf-8", "replace")).strip()
    if not sql:
        return {"error": "POST body = SQL query", "_status": 400}

    # Belt: keyword whitelist
    first = sql.split(None, 1)[0].upper() if sql else ""
    if first not in ("SELECT", "PRAGMA", "WITH", "EXPLAIN"):
        return {"error": "read-only: SELECT/PRAGMA/WITH/EXPLAIN only", "_status": 403}

    # Which database?
    world = params.get("world", "")
    file_path = params.get("file", "")  # for /mnt/ paths
    if file_path:
        # External file — must be under a fstab mount (read etc/fstab)
        db_path = _resolve_mnt(file_path)
        if not db_path:
            return {"error": "file not under any fstab mount", "_status": 403}
        if not db_path.exists():
            return {"error": f"file not found: {file_path}", "_status": 404}
    elif world:
        db_path = _DATA / _disk_name(world) / "universe.db"
        if not db_path.exists():
            return {"error": f"world not found: {world}", "_status": 404}
    else:
        return {"error": "specify ?world=name or ?file=mount/path/to.db", "_status": 400}

    # Suspenders: read-only connection
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=2.0)
        c.row_factory = sqlite3.Row
        rows = c.execute(sql).fetchmany(1000)
        result = [dict(r) for r in rows]
        c.close()

        # Plain text for pipe, JSON for explicit request
        scope = params.get("_scope", {})
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept":
                accept = v.decode()
                break
        if "json" in accept:
            return result  # auto-serialized to JSON by plugin dispatch
        # Default: JSON pretty-printed as plain text (pipe-friendly)
        return {"_html": json.dumps(result, ensure_ascii=False, indent=2, default=str),
                "_status": 200}
    except sqlite3.OperationalError as e:
        if "timeout" in str(e).lower():
            return {"error": "query timeout (2s limit)", "_status": 408}
        return {"error": str(e), "_status": 400}
    except sqlite3.Error as e:
        return {"error": str(e), "_status": 400}


ROUTES = {"/dev/db": handle_db}
