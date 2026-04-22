"""/dev/db — read-only SQL against any world's SQLite. Device, not command.

One of ?world= or ?file= is required; bare POST /dev/db returns 400.

POST /dev/db?world=work                      body: SELECT * FROM stage_meta
POST /dev/db?file=brave/History              body: SELECT url FROM urls ORDER BY last_visit_time DESC LIMIT 10

?file=<mount-name>/<path> resolves against /etc/fstab. Mount-name is
the suffix after /mnt/ in the fstab line; path is anything under that
mount. No .db extension required — SQLite opens any file that looks
like a database.

**file-kind mounts only.** fstab now supports multiple source schemes
(file://, https://, etc.) but /dev/db can only open local files. An
https:// or other non-file mount rejects with 400 "wrong mount kind"
rather than trying and failing cryptically inside sqlite3. An unknown
mount name rejects with 404. Use /mnt/<name>/<path> if you want the
raw bytes an http mount serves; /dev/db is for SQL, which requires a
local DB file.

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

    SQLite can only open local files. fstab now supports multiple
    source schemes (file://, https://, etc. — anything registered in
    fstab.py's _ADAPTERS table), so /dev/db MUST distinguish between:

      - "this mount doesn't exist"  (unknown mount name)   -> 404
      - "mount exists, wrong kind"  (http, https, ...)     -> 400
      - "mount exists, file-kind, resolves cleanly"        -> Path

    Collapsing these into one 403 (as the pre-refactor code did) made
    diagnosis impossible when fstab started mounting non-file sources.

    Returns a (status, value) pair:
      ("ok",        Path)        — file mount, path resolved
      ("unknown",   None)        — no /mnt/<name> in fstab (or fstab
                                   empty / missing — same meaning to
                                   the caller: "that mount isn't there")
      ("wrong_kind", kind_str)   — mount exists but kind != "file"
      ("traversal", None)        — resolved path escaped mount root

    Grammar parsing is delegated to server._read_fstab so /dev/db and
    /mnt/ always agree on what an fstab line means. No private copy
    of rsplit(None, 2) / scheme-detection / comma-opt handling lives
    here anymore — if the contract changes, one place changes.
    """
    # First segment of file_path is the mount name — same segmentation
    # rule /mnt/ uses (/mnt/<name>/<rest_path>).
    first_slash = file_path.find("/")
    if first_slash < 0:
        want_name, want_rest = file_path, ""
    else:
        want_name, want_rest = file_path[:first_slash], file_path[first_slash + 1:]

    # Match on name first so wrong_kind vs unknown stays accurate — a
    # bare startswith-scan on the raw source string would let a file
    # mount partially shadow a similarly-named http mount.
    for entry in server._read_fstab():
        if entry["name"] != want_name:
            continue
        if entry["kind"] != "file":
            return ("wrong_kind", entry["kind"])
        local_path = entry["source"]
        full = os.path.normpath(os.path.join(local_path, want_rest))
        try:
            if os.path.commonpath([full, os.path.normpath(local_path)]) != os.path.normpath(local_path):
                return ("traversal", None)
        except ValueError:
            return ("traversal", None)
        return ("ok", Path(full))
    return ("unknown", None)


async def handle_db(method, body, params):
    """POST /dev/db — SELECT-only SQL over a world or file-kind fstab mount.

    body: the query. SELECT / PRAGMA / WITH / EXPLAIN only.
    query: ?world=<name>            → that world's universe.db
           ?file=<mount>/<path>     → a file under a file-kind fstab mount

    Exactly one of ?world= / ?file= is required; otherwise 400.

    ?file= status semantics:
      200  — mount is file-kind AND the resolved path exists + is a DB
      404  — <mount> is not declared in /etc/fstab
      404  — <mount> is declared but the path under it doesn't exist
      400  — <mount> is declared but its source scheme is non-file
             (http/https/etc. — use /mnt/<name>/<path> for raw bytes)
      403  — resolved path escaped the mount root (traversal)

    Examples:
      curl -X POST "localhost:3005/dev/db?file=brave/History" \\
        -d "SELECT url, title FROM urls ORDER BY visit_count DESC LIMIT 10"
      curl -X POST "localhost:3005/dev/db?world=toilet" \\
        -d "SELECT * FROM stage_meta"

    Connection is ro + immutable=1 — safe to read DBs that other
    processes currently hold open (e.g. Brave History while browser runs).
    Returns JSON pretty-printed as text by default; Accept: application/json
    gets raw JSON. Hard cap: 1000 rows.
    """
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
        # External file — must resolve under a file-kind fstab mount.
        # http(s)/other-scheme mounts serve bytes over the network and
        # aren't openable by sqlite3.connect(); we surface that as 400
        # rather than trying and failing with a cryptic sqlite error.
        status, value = _resolve_mnt(file_path)
        if status == "ok":
            db_path = value
            if not db_path.exists():
                return {"error": f"file not found: {file_path}", "_status": 404}
        elif status == "wrong_kind":
            return {"error": (f"/dev/db requires a file-kind mount; "
                              f"'{file_path.split('/', 1)[0]}' is a "
                              f"{value!r} mount. Mount a local path in "
                              f"/etc/fstab to query it here, or use "
                              f"/mnt/<name>/<path> to fetch bytes over "
                              f"the adapter."),
                    "_status": 400}
        elif status == "traversal":
            return {"error": f"path traversal: {file_path}", "_status": 403}
        else:
            # "unknown" — mount name not in /etc/fstab (or fstab empty,
            # which server._read_fstab reports as an empty list — same
            # meaning to the caller).
            return {"error": (f"mount not found: "
                              f"'{file_path.split('/', 1)[0]}'. "
                              f"Check /etc/fstab."),
                    "_status": 404}
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

        # Plain text for pipe, JSON for explicit request. Both paths
        # return a dict envelope — server.py's dispatcher pops status/
        # headers/body off the returned dict and bare `return result`
        # (a list) would crash with AttributeError on list.pop("_status").
        scope = params.get("_scope", {})
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept":
                accept = v.decode()
                break
        payload = json.dumps(result, ensure_ascii=False,
                             indent=None if "json" in accept else 2,
                             default=str)
        if "json" in accept:
            return {"_body": payload, "_ct": "application/json",
                    "_status": 200}
        # Default: pretty-printed JSON as text/plain (pipe-friendly)
        return {"_body": payload, "_ct": "text/plain; charset=utf-8",
                "_status": 200}
    except sqlite3.OperationalError as e:
        if "timeout" in str(e).lower():
            return {"error": "query timeout (2s limit)", "_status": 408}
        return {"error": str(e), "_status": 400}
    except sqlite3.Error as e:
        return {"error": str(e), "_status": 400}


ROUTES = {"/dev/db": handle_db}
