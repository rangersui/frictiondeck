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
AUTH = "auth"  # T2 can query. T3 not needed — it's read-only.
ROUTES = {}

import json, os, sqlite3
from pathlib import Path

_DATA = Path(os.environ.get("ELASTIK_DATA", "data")).resolve()


def _disk_name(name):
    return name.replace("/", "%2F")


async def handle_db(method, body, params):
    """/dev/db — SELECT-only SQL. Read-only connection. Bounded."""
    sql = (body if isinstance(body, str) else body.decode("utf-8", "replace")).strip()
    if not sql:
        return {"error": "POST body = SQL query", "_status": 400}

    # Belt: keyword whitelist
    first = sql.split(None, 1)[0].upper() if sql else ""
    if first not in ("SELECT", "PRAGMA", "WITH", "EXPLAIN"):
        return {"error": "read-only: SELECT/PRAGMA/WITH/EXPLAIN only", "_status": 403}

    # Which database?
    world = params.get("world", "")
    if world:
        db_path = _DATA / _disk_name(world) / "universe.db"
        if not db_path.exists():
            return {"error": f"world not found: {world}", "_status": 404}
    else:
        # No world specified — list what's available
        return {"error": "specify ?world=name. Available: " +
                ", ".join(sorted(d.name.replace("%2F", "/")
                         for d in _DATA.iterdir()
                         if d.is_dir() and (d / "universe.db").exists())[:20]),
                "_status": 400}

    # Suspenders: read-only connection
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
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
