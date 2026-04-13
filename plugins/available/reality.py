"""reality — elastik serves itself. Atomic snapshots for cloning.

GET /__reality__  → tar.gz of all universe.db (sqlite3.backup, WAL-safe)
GET /self         → tar.gz of source code (no .env, no tokens, no data)

Clone in two lines:
  curl -H "Authorization: Bearer $TOKEN" http://A/__reality__ > data.tar.gz
  curl -H "Authorization: Bearer $TOKEN" http://A/self > self.tar.gz
"""
import io, os, sqlite3, tarfile, tempfile
from pathlib import Path
import server

DESCRIPTION = "/__reality__ → data snapshot, /self → source code. Atomic cloning."
AUTH = "approve"
ROUTES = {}

_ROOT = Path(server.__file__).resolve().parent


async def handle_reality(method, body, params):
    """All universe.db files, WAL-merged via sqlite3.backup, as tar.gz."""
    if method != "GET":
        return {"error": "GET only", "_status": 405}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if server.DATA.exists():
            for d in sorted(server.DATA.iterdir()):
                db = d / "universe.db"
                if not d.is_dir() or not db.exists():
                    continue
                # Atomic snapshot — merges WAL, consistent read
                src = sqlite3.connect(str(db))
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
                try:
                    dst = sqlite3.connect(tmp.name)
                    src.backup(dst)
                    dst.close()
                    src.close()
                    tar.add(tmp.name, arcname=f"data/{d.name}/universe.db")
                finally:
                    os.unlink(tmp.name)
    body_bytes = buf.getvalue()
    return {"_body": body_bytes, "_ct": "application/gzip",
            "_headers": [["content-disposition", "attachment; filename=reality.tar.gz"]]}


async def handle_self(method, body, params):
    """Source code tarball. No .env, no tokens, no data."""
    if method != "GET":
        return {"error": "GET only", "_status": 405}
    buf = io.BytesIO()
    _skip = {".env", "_env", ".env.local", "__pycache__", ".git",
             "data", "backups", "node_modules", "_mock"}
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(_ROOT.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(_ROOT)
            if any(part in _skip for part in rel.parts):
                continue
            if rel.suffix in (".pyc", ".pyo"):
                continue
            tar.add(str(f), arcname=str(rel))
    body_bytes = buf.getvalue()
    return {"_body": body_bytes, "_ct": "application/gzip",
            "_headers": [["content-disposition", "attachment; filename=elastik.tar.gz"]]}


ROUTES["/__reality__"] = handle_reality
ROUTES["/self"] = handle_self
