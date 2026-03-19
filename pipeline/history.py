"""FrictionDeck v4 — History Layer (Multi-Stage)

Append-only HMAC-SHA256 hash-chain ledger.
Each stage_name maps to data/{stage_name}/history.db.
HMAC key is shared across all stages (from data/config.json or env).

Replaces audit.py. File renamed to history.db for clarity.
"""

import hashlib
import hmac as _hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("frictiondeck.history")

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

DATA_ROOT = os.environ.get(
    "FRICTIONDECK_DB_DIR", os.path.join(_PROJECT_ROOT, "data"),
)

# Connection pool: stage_name → Connection
_conns: dict[str, sqlite3.Connection] = {}


# ── Connection ───────────────────────────────────────────────────────────────


def _db_path(stage: str) -> str:
    return os.path.join(DATA_ROOT, stage, "history.db")


def _get_conn(stage: str = "default") -> sqlite3.Connection:
    if stage not in _conns:
        db_dir = os.path.join(DATA_ROOT, stage)
        os.makedirs(db_dir, exist_ok=True)
        path = _db_path(stage)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        _conns[stage] = conn
        _init_tables(conn, stage)
    return _conns[stage]


def _init_tables(conn: sqlite3.Connection, stage: str) -> None:
    logger.info("init history db  stage=%s  path=%s", stage, _db_path(stage))
    # Migrate: rename old audit_events table if it exists
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    if "audit_events" in tables and "events" not in tables:
        conn.execute("ALTER TABLE audit_events RENAME TO events")
        conn.commit()
        logger.info("migrated audit_events → events  stage=%s", stage)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL UNIQUE,
            event_type  TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            actor       TEXT NOT NULL DEFAULT 'system',
            pathway     TEXT,
            payload     TEXT NOT NULL DEFAULT '{}',
            environment TEXT NOT NULL DEFAULT '{}',
            prev_hash   TEXT NOT NULL,
            event_hash  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_actor   ON events(actor);
        CREATE INDEX IF NOT EXISTS idx_events_pathway ON events(pathway);
    """)
    conn.commit()


def init_history_db(stage: str = "default") -> None:
    """Ensure a stage's history db is initialized."""
    _get_conn(stage)


# ── Environment snapshot ─────────────────────────────────────────────────────


def _collect_environment() -> str:
    from pipeline.config import VERSION
    env: dict[str, Any] = {"frictiondeck_version": VERSION}
    return json.dumps(env, ensure_ascii=False, sort_keys=True)


# ── Hash computation ─────────────────────────────────────────────────────────


def _compute_hash(event_id: str, event_type: str, timestamp: str,
                  prev_hash: str, environment: str, payload: str,
                  *, key: str) -> str:
    canonical = f"{event_id}|{event_type}|{timestamp}|{prev_hash}|{environment}|{payload}"
    return _hmac.new(key.encode(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


# ── Core logging ─────────────────────────────────────────────────────────────


def log_event(
    event_type: str,
    *,
    actor: str = "system",
    pathway: str | None = None,
    payload: dict[str, Any] | None = None,
    stage: str = "default",
) -> str:
    """Append an event to the hash chain. Returns event_id."""
    conn = _get_conn(stage)
    event_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    env_json = _collect_environment()

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT event_hash FROM events ORDER BY id DESC LIMIT 1",
        ).fetchone()
        prev_hash = row["event_hash"] if row else "GENESIS"

        from pipeline.config import AUDIT_HMAC_KEY
        event_hash = _compute_hash(
            event_id, event_type, ts, prev_hash, env_json, payload_json,
            key=AUDIT_HMAC_KEY,
        )

        conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, timestamp, actor, pathway, "
            " payload, environment, prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, event_type, ts, actor, pathway,
             payload_json, env_json, prev_hash, event_hash),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return event_id


# ── Query functions ──────────────────────────────────────────────────────────


def get_events(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    actor: str | None = None,
    pathway: str | None = None,
    stage: str = "default",
) -> list[dict]:
    conn = _get_conn(stage)
    clauses: list[str] = []
    params: list[Any] = []

    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if pathway:
        clauses.append("pathway = ?")
        params.append(pathway)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT * FROM events{where} "
        f"ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Chain verification ───────────────────────────────────────────────────────


def _break_info(event: dict, index: int, expected: str) -> dict:
    return {
        "event_index": index,
        "expected": expected,
        "got": event.get("prev_hash") or event.get("event_hash", "")[:16],
        "event_type": event["event_type"],
        "created_at": event["timestamp"],
    }


def verify_chain(limit: int = 0, stage: str = "default") -> dict:
    from pipeline.config import AUDIT_HMAC_KEY

    base: dict = {
        "valid": True, "degraded": False, "total_events": 0,
        "verified_events": 0, "anomalies": 0, "first_break": None,
        "verified_at": datetime.now(UTC).isoformat(),
    }

    conn = _get_conn(stage)
    if limit > 0:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id ASC "
            "LIMIT ? OFFSET (SELECT MAX(0, COUNT(*) - ?) FROM events)",
            (limit, limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
    events = [dict(r) for r in rows]

    if not events:
        return base

    verified = tampered = 0
    for i, event in enumerate(events):
        if i == 0 and event["id"] == 1 and event["prev_hash"] != "GENESIS":
            base.update(valid=False, total_events=len(events), anomalies=1,
                        first_break=_break_info(event, 1, "GENESIS"))
            return base
        if i > 0 and event["prev_hash"] != events[i - 1]["event_hash"]:
            base.update(valid=False, total_events=len(events),
                        verified_events=verified, anomalies=1,
                        first_break=_break_info(event, i + 1, events[i - 1]["event_hash"]))
            return base

        expected = _compute_hash(
            event["event_id"], event["event_type"], event["timestamp"],
            event["prev_hash"], event["environment"], event["payload"],
            key=AUDIT_HMAC_KEY,
        )
        if event["event_hash"] == expected:
            verified += 1
        else:
            tampered += 1
            if not base["first_break"]:
                info = _break_info(event, i + 1, expected[:16] + "…")
                info["reason"] = "tampered"
                base["first_break"] = info

    base["total_events"] = len(events)
    base["verified_events"] = verified
    if tampered > 0:
        base.update(valid=False, degraded=True, anomalies=tampered)
    return base


# ── Cleanup ──────────────────────────────────────────────────────────────────


def close_history(stage: str | None = None) -> None:
    if stage:
        conn = _conns.pop(stage, None)
        if conn:
            conn.close()
    else:
        for conn in _conns.values():
            conn.close()
        _conns.clear()
