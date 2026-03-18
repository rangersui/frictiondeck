"""FrictionDeck v4 — Stage State Manager

The hub. Every write increments version. Every version change triggers broadcast.

stage.db has four tables:
  artifacts        → display layer (AI freely generates, fluid state)
  judgment_objects → audit layer (promoted from artifacts, viscous → solid)
  overlays         → state layer (lock, commit, contradiction, nli_result)
  stage_meta       → single-row: current version counter

Three-state model:
  fluid   → artifact dropped (grey, free to change/delete)
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
from typing import Any
from uuid import uuid4

from pipeline.constants import ArtifactState, OverlayType

logger = logging.getLogger("frictiondeck.stage")

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

STAGE_DB_DIR = os.environ.get(
    "FRICTIONDECK_DB_DIR", os.path.join(_PROJECT_ROOT, "data"),
)
STAGE_DB_PATH = os.path.join(STAGE_DB_DIR, "stage.db")

_conn: sqlite3.Connection | None = None

# Broadcast callback — set by server.py at startup
# Signature: broadcast_fn(event_type: str, data: dict) -> None
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


# ── Connection ───────────────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(STAGE_DB_DIR, exist_ok=True)
        _conn = sqlite3.connect(STAGE_DB_PATH, check_same_thread=False, timeout=15)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=FULL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


# ── Schema ───────────────────────────────────────────────────────────────────


def init_stage_db() -> None:
    """Create stage tables if they don't exist."""
    conn = _get_conn()
    logger.info("init_stage_db  path=%s", STAGE_DB_PATH)
    conn.executescript("""
        -- Version counter (single row)
        CREATE TABLE IF NOT EXISTS stage_meta (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            version     INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL
        );

        -- Display layer: AI drops anything here (fluid state)
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id     TEXT PRIMARY KEY,
            content_type    TEXT NOT NULL DEFAULT 'html',
            content         TEXT NOT NULL,
            metadata        TEXT NOT NULL DEFAULT '{}',
            source_trust    TEXT NOT NULL DEFAULT 'grey',
            position        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            created_by      TEXT NOT NULL DEFAULT 'ai',
            deleted         INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_artifacts_deleted
            ON artifacts(deleted);
        CREATE INDEX IF NOT EXISTS idx_artifacts_position
            ON artifacts(position);

        -- Audit layer: promoted from artifacts (viscous → solid)
        CREATE TABLE IF NOT EXISTS judgment_objects (
            judgment_id     TEXT PRIMARY KEY,
            artifact_id     TEXT,
            claim_text      TEXT NOT NULL,
            params          TEXT NOT NULL DEFAULT '[]',
            state           TEXT NOT NULL DEFAULT 'viscous',
            nli_verdict     TEXT,
            nli_confidence  REAL,
            commit_id       TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            created_by      TEXT NOT NULL DEFAULT 'ai',

            FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
        );

        CREATE INDEX IF NOT EXISTS idx_judgments_state
            ON judgment_objects(state);
        CREATE INDEX IF NOT EXISTS idx_judgments_artifact
            ON judgment_objects(artifact_id);
        CREATE INDEX IF NOT EXISTS idx_judgments_commit
            ON judgment_objects(commit_id);

        -- State layer: overlays on artifacts/judgments
        CREATE TABLE IF NOT EXISTS overlays (
            overlay_id      TEXT PRIMARY KEY,
            target_id       TEXT NOT NULL,
            target_type     TEXT NOT NULL CHECK (target_type IN ('artifact', 'judgment')),
            overlay_type    TEXT NOT NULL,
            value           TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL,
            created_by      TEXT NOT NULL DEFAULT 'system'
        );

        CREATE INDEX IF NOT EXISTS idx_overlays_target
            ON overlays(target_id);
        CREATE INDEX IF NOT EXISTS idx_overlays_type
            ON overlays(overlay_type);

        -- Relations between cards
        CREATE TABLE IF NOT EXISTS relations (
            relation_id     TEXT PRIMARY KEY,
            from_id         TEXT NOT NULL,
            to_id           TEXT NOT NULL,
            relation_type   TEXT NOT NULL DEFAULT 'manual',
            label           TEXT,
            nli_verdict     TEXT,
            created_at      TEXT NOT NULL,
            created_by      TEXT NOT NULL DEFAULT 'ai'
        );

        CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id);
        CREATE INDEX IF NOT EXISTS idx_relations_to   ON relations(to_id);

        -- Groups
        CREATE TABLE IF NOT EXISTS groups (
            group_id    TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            card_ids    TEXT NOT NULL DEFAULT '[]',
            created_at  TEXT NOT NULL,
            created_by  TEXT NOT NULL DEFAULT 'user'
        );

        -- Seed stage_meta if empty
        INSERT OR IGNORE INTO stage_meta (id, version, updated_at)
        VALUES (1, 0, datetime('now'));
    """)
    conn.commit()


# ── Version management ───────────────────────────────────────────────────────


def _bump_version(conn: sqlite3.Connection) -> int:
    """Increment version counter. Returns new version. Must be called inside transaction."""
    ts = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE stage_meta SET version = version + 1, updated_at = ? WHERE id = 1",
        (ts,),
    )
    row = conn.execute("SELECT version FROM stage_meta WHERE id = 1").fetchone()
    return row["version"]


def get_version() -> int:
    """Return current stage version."""
    conn = _get_conn()
    row = conn.execute("SELECT version FROM stage_meta WHERE id = 1").fetchone()
    return row["version"] if row else 0


# ═══════════════════════════════════════════════════════════════════════════
# ARTIFACT operations (display layer — fluid state)
# ═══════════════════════════════════════════════════════════════════════════


def write_artifact(
    content: str,
    content_type: str = "html",
    metadata: dict | None = None,
    source_trust: str = "grey",
    created_by: str = "ai",
) -> dict:
    """Drop an artifact onto the Stage.

    Returns: {"artifact_id": str, "version": int}
    """
    conn = _get_conn()
    artifact_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)

    # Get next position
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos FROM artifacts WHERE deleted = 0",
    ).fetchone()
    position = row["next_pos"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO artifacts "
            "(artifact_id, content_type, content, metadata, source_trust, "
            " position, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, content_type, content, meta_json, source_trust,
             position, ts, ts, created_by),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    data = {
        "artifact_id": artifact_id,
        "content_type": content_type,
        "source_trust": source_trust,
        "position": position,
        "version": version,
    }
    _broadcast("artifact_added", data)
    logger.info("artifact dropped  id=%s  version=%d", artifact_id, version)
    return {"artifact_id": artifact_id, "version": version}


def update_artifact(
    artifact_id: str,
    content: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Update an existing artifact. Returns {"version": int}."""
    conn = _get_conn()
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND deleted = 0",
            (artifact_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"artifact not found: {artifact_id}")

        updates = ["updated_at = ?"]
        params: list[Any] = [ts]

        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata, ensure_ascii=False))

        params.append(artifact_id)
        conn.execute(
            f"UPDATE artifacts SET {', '.join(updates)} WHERE artifact_id = ?",
            params,
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("artifact_updated", {"artifact_id": artifact_id, "version": version})
    return {"version": version}


def delete_artifact(artifact_id: str, deleted_by: str = "user") -> dict:
    """Soft-delete an artifact. Returns {"version": int}."""
    conn = _get_conn()
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND deleted = 0",
            (artifact_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"artifact not found: {artifact_id}")

        conn.execute(
            "UPDATE artifacts SET deleted = 1, updated_at = ? WHERE artifact_id = ?",
            (ts, artifact_id),
        )
        # Break relations involving this artifact
        broken = conn.execute(
            "SELECT relation_id FROM relations WHERE from_id = ? OR to_id = ?",
            (artifact_id, artifact_id),
        ).fetchall()
        broken_ids = [r["relation_id"] for r in broken]
        if broken_ids:
            placeholders = ",".join("?" * len(broken_ids))
            conn.execute(
                f"DELETE FROM relations WHERE relation_id IN ({placeholders})",
                broken_ids,
            )

        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("card_deleted", {
        "card_id": artifact_id,
        "relations_broken": broken_ids,
        "by": deleted_by,
        "version": version,
    })
    return {"version": version}


def move_artifact(artifact_id: str, to_position: int) -> dict:
    """Move artifact to new position. Returns {"version": int}."""
    conn = _get_conn()
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT position FROM artifacts WHERE artifact_id = ? AND deleted = 0",
            (artifact_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"artifact not found: {artifact_id}")

        from_position = row["position"]
        conn.execute(
            "UPDATE artifacts SET position = ?, updated_at = ? WHERE artifact_id = ?",
            (to_position, ts, artifact_id),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("card_moved", {
        "card_id": artifact_id,
        "from_position": from_position,
        "to_position": to_position,
        "version": version,
    })
    return {"version": version}


def get_artifacts(include_deleted: bool = False) -> list[dict]:
    """Return all artifacts, ordered by position."""
    conn = _get_conn()
    if include_deleted:
        rows = conn.execute(
            "SELECT * FROM artifacts ORDER BY position ASC",
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE deleted = 0 ORDER BY position ASC",
        ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# JUDGMENT OBJECT operations (audit layer — viscous/solid state)
# ═══════════════════════════════════════════════════════════════════════════


def promote_to_judgment(
    artifact_id: str,
    claim_text: str,
    params: list[dict] | None = None,
    created_by: str = "ai",
) -> dict:
    """Promote an artifact to a judgment object (fluid → viscous).

    Returns: {"judgment_id": str, "version": int}
    """
    conn = _get_conn()
    judgment_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()
    params_json = json.dumps(params or [], ensure_ascii=False)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Verify artifact exists
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND deleted = 0",
            (artifact_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"artifact not found: {artifact_id}")

        conn.execute(
            "INSERT INTO judgment_objects "
            "(judgment_id, artifact_id, claim_text, params, state, "
            " created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (judgment_id, artifact_id, claim_text, params_json,
             ArtifactState.VISCOUS, ts, ts, created_by),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("judgment_promoted", {
        "judgment_id": judgment_id,
        "artifact_id": artifact_id,
        "claim_text": claim_text,
        "version": version,
    })
    logger.info("judgment promoted  id=%s  from_artifact=%s  version=%d",
                judgment_id, artifact_id, version)
    return {"judgment_id": judgment_id, "version": version}


def update_judgment_nli(
    judgment_id: str,
    verdict: str,
    confidence: float,
) -> dict:
    """Record NLI verification result on a judgment object."""
    conn = _get_conn()
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE judgment_objects SET nli_verdict = ?, nli_confidence = ?, "
            "updated_at = ? WHERE judgment_id = ?",
            (verdict, confidence, ts, judgment_id),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("nli_complete", {
        "judgment_id": judgment_id,
        "verdict": verdict,
        "confidence": confidence,
        "version": version,
    })
    return {"version": version}


def seal_judgments(judgment_ids: list[str], commit_id: str) -> dict:
    """Seal judgment objects (viscous → solid). Called after commit approval.

    Returns: {"version": int, "sealed": int}
    """
    conn = _get_conn()
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        sealed = 0
        for jid in judgment_ids:
            result = conn.execute(
                "UPDATE judgment_objects SET state = ?, commit_id = ?, "
                "updated_at = ? WHERE judgment_id = ? AND state = ?",
                (ArtifactState.SOLID, commit_id, ts, jid, ArtifactState.VISCOUS),
            )
            sealed += result.rowcount

        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("commit_approved", {
        "commit_id": commit_id,
        "judgment_ids": judgment_ids,
        "sealed": sealed,
        "version": version,
    })
    return {"version": version, "sealed": sealed}


def get_judgments(state: str | None = None) -> list[dict]:
    """Return judgment objects, optionally filtered by state."""
    conn = _get_conn()
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
# OVERLAY operations (state layer)
# ═══════════════════════════════════════════════════════════════════════════


def add_overlay(
    target_id: str,
    target_type: str,
    overlay_type: str,
    value: dict | None = None,
    created_by: str = "system",
) -> dict:
    """Add an overlay to an artifact or judgment.

    Returns: {"overlay_id": str, "version": int}
    """
    conn = _get_conn()
    overlay_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()
    value_json = json.dumps(value or {}, ensure_ascii=False)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO overlays "
            "(overlay_id, target_id, target_type, overlay_type, value, "
            " created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (overlay_id, target_id, target_type, overlay_type, value_json,
             ts, created_by),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("overlay_changed", {
        "overlay_id": overlay_id,
        "target_id": target_id,
        "overlay_type": overlay_type,
        "version": version,
    })
    return {"overlay_id": overlay_id, "version": version}


def remove_overlay(overlay_id: str) -> dict:
    """Remove an overlay. Returns {"version": int}."""
    conn = _get_conn()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM overlays WHERE overlay_id = ?", (overlay_id,))
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("overlay_changed", {"overlay_id": overlay_id, "removed": True, "version": version})
    return {"version": version}


def get_overlays(target_id: str | None = None) -> list[dict]:
    """Return overlays, optionally filtered by target."""
    conn = _get_conn()
    if target_id:
        rows = conn.execute(
            "SELECT * FROM overlays WHERE target_id = ? ORDER BY created_at ASC",
            (target_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM overlays ORDER BY created_at ASC",
        ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# RELATION operations
# ═══════════════════════════════════════════════════════════════════════════


def add_relation(
    from_id: str,
    to_id: str,
    relation_type: str = "manual",
    label: str | None = None,
    created_by: str = "ai",
) -> dict:
    """Add a relation between two cards.

    Returns: {"relation_id": str, "version": int}
    """
    conn = _get_conn()
    relation_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO relations "
            "(relation_id, from_id, to_id, relation_type, label, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (relation_id, from_id, to_id, relation_type, label, ts, created_by),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("relation_added", {
        "relation_id": relation_id,
        "from_id": from_id,
        "to_id": to_id,
        "type": relation_type,
        "label": label,
        "version": version,
    })
    return {"relation_id": relation_id, "version": version}


def remove_relation(relation_id: str) -> dict:
    """Remove a relation. Returns {"version": int}."""
    conn = _get_conn()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM relations WHERE relation_id = ?", (relation_id,))
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("relation_removed", {"relation_id": relation_id, "version": version})
    return {"version": version}


def get_relations() -> list[dict]:
    """Return all relations."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM relations ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# GROUP operations
# ═══════════════════════════════════════════════════════════════════════════


def create_group(
    name: str,
    card_ids: list[str],
    created_by: str = "user",
) -> dict:
    """Create a card group. Returns {"group_id": str, "version": int}."""
    conn = _get_conn()
    group_id = uuid4().hex
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO groups (group_id, name, card_ids, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, name, json.dumps(card_ids), ts, created_by),
        )
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _broadcast("cards_grouped", {
        "group_id": group_id,
        "name": name,
        "cards": card_ids,
        "version": version,
    })
    return {"group_id": group_id, "version": version}


# ═══════════════════════════════════════════════════════════════════════════
# FULL STATE (for get_world_state / get_stage_state)
# ═══════════════════════════════════════════════════════════════════════════


def get_stage_state() -> dict:
    """Return complete stage state snapshot.

    Used by get_world_state and get_stage_state MCP tools.
    """
    return {
        "version": get_version(),
        "artifacts": get_artifacts(),
        "judgments": get_judgments(),
        "overlays": get_overlays(),
        "relations": get_relations(),
    }


def get_stage_diff(since_version: int) -> dict:
    """Return changes since a given version.

    For now, returns full state if version has changed.
    TODO: implement proper event log for incremental diffs.
    """
    current = get_version()
    if current == since_version:
        return {"changed": False, "version": current}

    state = get_stage_state()
    state["changed"] = True
    state["since_version"] = since_version
    return state


# ── Cleanup ──────────────────────────────────────────────────────────────────


def close_stage() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
