"""FrictionDeck v4 — Stage State Manager (Multi-Stage)

Each stage_name maps to data/{stage_name}/stage.db.
Connection pool caches open connections by stage_name.

stage.db has two things:
  stage_html        TEXT  → current page DOM, AI writes freely
  judgment_objects   TABLE → promoted judgments, HMAC signed

Two-state model:
  viscous → promoted to judgment_object (constrained, tracked, editable with trail)
  solid   → committed (HMAC sealed, irreversible)

SQLite WAL + synchronous=FULL for durability.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from uuid import uuid4

from pipeline.constants import JudgmentState

logger = logging.getLogger("frictiondeck.stage")

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

DATA_ROOT = os.environ.get(
    "FRICTIONDECK_DB_DIR", os.path.join(_PROJECT_ROOT, "data"),
)

# Connection pool: stage_name → Connection
_conns: dict[str, sqlite3.Connection] = {}

# Broadcast callback — set by server.py at startup
_broadcast_fn = None


def set_broadcast(fn) -> None:
    """Register broadcast callback. Called by server.py at startup."""
    global _broadcast_fn
    _broadcast_fn = fn


def _broadcast(event_type: str, data: dict) -> None:
    """Fire broadcast if callback is registered."""
    if _broadcast_fn is not None:
        try:
            _broadcast_fn(event_type, data)
        except Exception as exc:
            logger.error("broadcast failed: %s", exc)


# ── Connection ───────────────────────────────────────────────────────────


def _db_dir(stage: str) -> str:
    return os.path.join(DATA_ROOT, stage)


def _db_path(stage: str) -> str:
    return os.path.join(_db_dir(stage), "stage.db")


def _get_conn(stage: str = "default") -> sqlite3.Connection:
    if stage not in _conns:
        db_dir = _db_dir(stage)
        os.makedirs(db_dir, exist_ok=True)
        path = _db_path(stage)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        _conns[stage] = conn
        _init_tables(conn, stage)
    return _conns[stage]


def _init_tables(conn: sqlite3.Connection, stage: str) -> None:
    """Create stage tables if they don't exist."""
    logger.info("init stage db  stage=%s  path=%s", stage, _db_path(stage))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stage_meta (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            version     INTEGER NOT NULL DEFAULT 0,
            stage_html  TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS judgment_objects (
            judgment_id     TEXT PRIMARY KEY,
            claim_text      TEXT NOT NULL,
            params          TEXT NOT NULL DEFAULT '[]',
            state           TEXT NOT NULL DEFAULT 'viscous',
            commit_id       TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            created_by      TEXT NOT NULL DEFAULT 'ai'
        );

        CREATE INDEX IF NOT EXISTS idx_judgments_state
            ON judgment_objects(state);
        CREATE INDEX IF NOT EXISTS idx_judgments_commit
            ON judgment_objects(commit_id);

        INSERT OR IGNORE INTO stage_meta (id, version, stage_html, updated_at)
        VALUES (1, 0, '', datetime('now'));
    """)
    conn.commit()


def init_stage_db(stage: str = "default") -> None:
    """Ensure a stage's db is initialized. Called at startup for 'default'."""
    _get_conn(stage)


# ── Version management ───────────────────────────────────────────────────


def _bump_version(conn: sqlite3.Connection) -> int:
    """Increment version counter. Returns new version."""
    ts = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE stage_meta SET version = version + 1, updated_at = ? WHERE id = 1",
        (ts,),
    )
    row = conn.execute("SELECT version FROM stage_meta WHERE id = 1").fetchone()
    return row["version"]


def get_version(stage: str = "default") -> int:
    """Return current stage version."""
    conn = _get_conn(stage)
    row = conn.execute("SELECT version FROM stage_meta WHERE id = 1").fetchone()
    return row["version"] if row else 0


# ═══════════════════════════════════════════════════════════════════════════
# STAGE HTML operations (the canvas — AI writes freely)
# ═══════════════════════════════════════════════════════════════════════════


def get_html(stage: str = "default") -> str:
    """Return current stage HTML."""
    conn = _get_conn(stage)
    row = conn.execute("SELECT stage_html FROM stage_meta WHERE id = 1").fetchone()
    return row["stage_html"] if row else ""


def set_html(html: str, stage: str = "default") -> dict:
    """Replace entire stage HTML. Returns {"version": int}."""
    conn = _get_conn(stage)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE stage_meta SET stage_html = ?, updated_at = ? WHERE id = 1",
            (html, datetime.now(UTC).isoformat()),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("stage_updated", {"stage": stage, "version": version, "html": html})
    return {"version": version}


# ═══════════════════════════════════════════════════════════════════════════
# JUDGMENT OBJECT operations (audit layer — viscous/solid state)
# ═══════════════════════════════════════════════════════════════════════════


def promote_to_judgment(
    claim_text: str,
    params: list[dict] | None = None,
    created_by: str = "ai",
    stage: str = "default",
) -> dict:
    """Create a judgment object (viscous state).

    Returns: {"judgment_id": str, "version": int}
    """
    conn = _get_conn(stage)
    judgment_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()
    params_json = json.dumps(params or [], ensure_ascii=False)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO judgment_objects "
            "(judgment_id, claim_text, params, state, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (judgment_id, claim_text, params_json,
             JudgmentState.VISCOUS, ts, ts, created_by),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("judgment_promoted", {
        "stage": stage,
        "judgment_id": judgment_id,
        "claim_text": claim_text,
        "version": version,
    })
    logger.info("judgment promoted  stage=%s  id=%s  version=%d", stage, judgment_id, version)
    return {"judgment_id": judgment_id, "version": version}


def seal_judgments(judgment_ids: list[str], commit_id: str, stage: str = "default") -> dict:
    """Seal judgment objects (viscous → solid). Called after commit approval.

    Returns: {"version": int, "sealed": int}
    """
    conn = _get_conn(stage)
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        sealed = 0
        for jid in judgment_ids:
            result = conn.execute(
                "UPDATE judgment_objects SET state = ?, commit_id = ?, "
                "updated_at = ? WHERE judgment_id = ? AND state = ?",
                (JudgmentState.SOLID, commit_id, ts, jid, JudgmentState.VISCOUS),
            )
            sealed += result.rowcount

        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("commit_approved", {
        "stage": stage,
        "commit_id": commit_id,
        "judgment_ids": judgment_ids,
        "sealed": sealed,
        "version": version,
    })
    return {"version": version, "sealed": sealed}


def get_judgments(state: str | None = None, stage: str = "default") -> list[dict]:
    """Return judgment objects, optionally filtered by state."""
    conn = _get_conn(stage)
    if state:
        rows = conn.execute(
            "SELECT * FROM judgment_objects WHERE state = ? ORDER BY created_at ASC",
            (state,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM judgment_objects ORDER BY created_at ASC",
        ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# FULL STATE (for get_world_state / get_stage_state)
# ═══════════════════════════════════════════════════════════════════════════


def get_stage_state(stage: str = "default") -> dict:
    """Return complete stage state snapshot."""
    return {
        "stage": stage,
        "version": get_version(stage),
        "stage_html": get_html(stage),
        "judgments": get_judgments(stage=stage),
    }


def get_stage_diff(since_version: int, stage: str = "default") -> dict:
    """Return changes since a given version.

    Returns full state if version has changed, else {changed: false}.
    """
    current = get_version(stage)
    if current == since_version:
        return {"changed": False, "version": current}

    state = get_stage_state(stage)
    state["changed"] = True
    state["since_version"] = since_version
    return state


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-STAGE operations
# ═══════════════════════════════════════════════════════════════════════════


def list_stages() -> list[dict]:
    """Return all stages with name, version, updated_at."""
    stages = []
    if not os.path.exists(DATA_ROOT):
        return stages
    for name in sorted(os.listdir(DATA_ROOT)):
        stage_dir = os.path.join(DATA_ROOT, name)
        if os.path.isdir(stage_dir) and os.path.exists(os.path.join(stage_dir, "stage.db")):
            try:
                conn = _get_conn(name)
                row = conn.execute(
                    "SELECT version, updated_at FROM stage_meta WHERE id = 1"
                ).fetchone()
                stages.append({
                    "name": name,
                    "version": row["version"] if row else 0,
                    "updated_at": row["updated_at"] if row else "",
                })
            except Exception as exc:
                logger.error("failed to read stage %s: %s", name, exc)
                stages.append({"name": name, "version": -1, "updated_at": "error"})
    return stages


def create_stage(name: str) -> dict:
    """Create a new stage. Returns {"name": str, "version": int}."""
    # Validate name: alphanumeric + hyphens + underscores
    clean = "".join(c for c in name if c.isalnum() or c in "-_")
    if not clean:
        return {"error": "Invalid stage name"}
    _get_conn(clean)  # initializes db
    return {"name": clean, "version": 0}


# ── Cleanup ──────────────────────────────────────────────────────────────


def close_stage(stage: str | None = None) -> None:
    if stage:
        conn = _conns.pop(stage, None)
        if conn:
            conn.close()
    else:
        for conn in _conns.values():
            conn.close()
        _conns.clear()
