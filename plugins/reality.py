"""reality — elastik serves itself. Atomic snapshots for cloning.

GET /__reality__  → tar.gz of all universe.db (sqlite3.backup, WAL-safe)
GET /self         → tar.gz of source code (no .env, no tokens, no data)

Clone in two lines:
  curl -H "Authorization: Bearer $TOKEN" http://A/__reality__ > data.tar.gz
  curl -H "Authorization: Bearer $TOKEN" http://A/self > self.tar.gz
"""
import io, os, sqlite3, subprocess, tarfile, tempfile
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
                tmp_path = tempfile.mktemp(suffix=".db")
                try:
                    dst = sqlite3.connect(tmp_path)
                    src.backup(dst)
                    dst.close()
                    src.close()
                    tar.add(tmp_path, arcname=f"data/{d.name}/universe.db")
                finally:
                    try: os.unlink(tmp_path)
                    except OSError: pass
    body_bytes = buf.getvalue()
    return {"_body": body_bytes, "_ct": "application/gzip",
            "_headers": [["content-disposition", "attachment; filename=reality.tar.gz"]]}


async def handle_self(method, body, params):
    """Source code tarball. Git-tracked files only. No .env, no tokens, no data."""
    if method != "GET":
        return {"error": "GET only", "_status": 405}
    # Use git ls-files — only tracked files, excludes .git/data/backups/secrets
    try:
        out = subprocess.check_output(["git", "ls-files", "-z"],
                                      cwd=str(_ROOT), stderr=subprocess.DEVNULL)
        files = [f for f in out.decode("utf-8").split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        # No git? Fall back to essential files only
        files = [str(f.relative_to(_ROOT)) for f in
                 sorted(list(_ROOT.glob("*.py")) + list(_ROOT.glob("*.html")) +
                        list(_ROOT.glob("*.json")) + list(_ROOT.glob("*.js")) +
                        list((_ROOT / "plugins").glob("*.py")))]
    _secret = {".env", "_env", ".env.local"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in sorted(files):
            if rel in _secret or rel.endswith((".pyc", ".pyo")):
                continue
            full = _ROOT / rel
            if full.is_file():
                tar.add(str(full), arcname=rel)
    body_bytes = buf.getvalue()
    return {"_body": body_bytes, "_ct": "application/gzip",
            "_headers": [["content-disposition", "attachment; filename=elastik.tar.gz"]]}


ROUTES["/__reality__"] = handle_reality
ROUTES["/self"] = handle_self
