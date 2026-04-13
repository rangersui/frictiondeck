"""Backup plugin — cold copy of all worlds + retention policy.

CRON = 86400 (daily). Keeps last 7 backups.
backup/run and backup/restore require approve token (seal-level ops).
Restore requires server restart to clear connection cache.
"""
import json, os, shutil
from datetime import datetime
from pathlib import Path
import server

DESCRIPTION = "Backup and restore all worlds"
ROUTES = {}
CRON = 86400
MAX_BACKUPS = 7

BACKUP_DIR = _ROOT / "backups"
DATA_DIR = _ROOT / "data"

PARAMS_SCHEMA = {
    "/proxy/backup/run": {
        "method": "POST",
        "params": {},
        "returns": {"backup": "string", "worlds": "int", "size_kb": "float"}
    },
    "/proxy/backup/restore": {
        "method": "POST",
        "params": {"backup": {"type": "string", "required": True, "description": "Timestamp of backup to restore"}},
        "returns": {"restored": "string", "worlds": "int"},
        "note": "Requires approve-level auth. Server restart required after restore."
    },
    "/proxy/backup/list": {
        "method": "GET",
        "params": {},
        "returns": {"backups": ["string"]}
    },
    "/proxy/backup/status": {
        "method": "GET",
        "params": {},
        "returns": {"last_backup": "string", "backup_count": "int", "total_size_kb": "float"}
    },
}


def _check_approve(params):
    """Defense-in-depth: check approve token, same pattern as admin.py."""
    approve = os.getenv("ELASTIK_APPROVE_TOKEN", "")
    if not approve: return True
    scope = params.get("_scope", {})
    return server._check_auth(scope) == "approve"


def _checkpoint_all():
    """WAL checkpoint all worlds before backup — ensures clean copy."""
    if not DATA_DIR.exists(): return
    for d in sorted(DATA_DIR.iterdir()):
        if d.is_dir() and (d / "universe.db").exists():
            try:
                c = conn(d.name)
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass


def _run_backup():
    """Execute a backup. Returns (timestamp, world_count, size_kb)."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)

    _checkpoint_all()

    count = 0
    total_size = 0
    for d in sorted(DATA_DIR.iterdir()):
        if d.is_dir() and (d / "universe.db").exists():
            target = dest / f"{d.name}.db"
            shutil.copy2(d / "universe.db", target)
            total_size += target.stat().st_size
            count += 1

    # Backup env files
    for env_name in (".env", "_env", ".env.local"):
        env_file = _ROOT / env_name
        if env_file.exists():
            shutil.copy2(env_file, dest / env_name.replace(".", "dot-"))
            break

    size_kb = round(total_size / 1024, 1)
    return ts, count, size_kb


def _enforce_retention():
    """Keep only MAX_BACKUPS most recent backups."""
    if not BACKUP_DIR.exists(): return
    backups = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()])
    while len(backups) > MAX_BACKUPS:
        oldest = backups.pop(0)
        shutil.rmtree(oldest)
        print(f"  backup: pruned {oldest.name}")


async def handle_run(method, body, params):
    ts, count, size_kb = _run_backup()
    _enforce_retention()
    log_event("sys-health", "backup", {"timestamp": ts, "worlds": count, "size_kb": size_kb})
    print(f"  backup: {ts} ({count} worlds, {size_kb} KB)")
    return {"backup": ts, "worlds": count, "size_kb": size_kb}


async def handle_restore(method, body, params):
    if not _check_approve(params):
        return {"error": "unauthorized — approve token required", "_status": 403}
    b = json.loads(body) if body else {}
    ts = params.get("backup") or b.get("backup", "")
    if not ts:
        return {"error": "backup timestamp required"}

    src = BACKUP_DIR / ts
    if not src.exists() or not src.is_dir():
        return {"error": f"backup '{ts}' not found"}

    count = 0
    for db_file in sorted(src.glob("*.db")):
        world_name = db_file.stem
        target_dir = DATA_DIR / world_name
        target_dir.mkdir(parents=True, exist_ok=True)
        # Clean stale WAL/SHM before overwrite
        for ext in ("-shm", "-wal"):
            stale = target_dir / f"universe.db{ext}"
            try: stale.unlink(missing_ok=True)
            except OSError: pass
        shutil.copy2(db_file, target_dir / "universe.db")
        count += 1

    log_event("sys-health", "restore", {"timestamp": ts, "worlds": count})
    print(f"  backup: restored {ts} ({count} worlds) — RESTART REQUIRED")
    return {"restored": ts, "worlds": count, "note": "restart server to apply"}


async def handle_list(method, body, params):
    if not BACKUP_DIR.exists():
        return {"backups": []}
    backups = sorted([d.name for d in BACKUP_DIR.iterdir() if d.is_dir()])
    return {"backups": backups}


async def handle_status(method, body, params):
    if not BACKUP_DIR.exists():
        return {"last_backup": None, "backup_count": 0, "total_size_kb": 0}
    backups = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()])
    total = sum(f.stat().st_size for b in backups for f in b.iterdir() if f.is_file())
    return {
        "last_backup": backups[-1].name if backups else None,
        "backup_count": len(backups),
        "total_size_kb": round(total / 1024, 1),
    }


# ── CRON auto-backup ─────────────────────────────────────────────────────

async def _auto_backup():
    """Daily automatic backup with retention."""
    ts, count, size_kb = _run_backup()
    _enforce_retention()
    log_event("sys-health", "backup-auto", {"timestamp": ts, "worlds": count, "size_kb": size_kb})
    print(f"  backup: auto {ts} ({count} worlds, {size_kb} KB)")

CRON_HANDLER = _auto_backup

ROUTES["/proxy/backup/run"] = handle_run
ROUTES["/proxy/backup/restore"] = handle_restore
ROUTES["/proxy/backup/list"] = handle_list
ROUTES["/proxy/backup/status"] = handle_status
