"""FrictionDeck v4 — GUI Adapter (Human's hands)

All operations a human can perform through the GUI.
Physical isolation from mcp_adapter — zero intersection.
CI scans enforce: approve/lock/delete NEVER appear in mcp_adapter.

Operations only humans can do:
  lock_parameter, unlock_parameter, approve_commit, reject_commit,
  delete_card, drag_card, edit_card_text, group_cards,
  dismiss_negative_space, upload_document
"""

import json
import logging
from typing import Any

from pipeline.constants import ArtifactState, EventType, OverlayType
from pipeline.stage import (
    add_overlay,
    create_group,
    delete_artifact,
    get_judgments,
    get_overlays,
    move_artifact,
    remove_overlay,
    seal_judgments,
)

logger = logging.getLogger("frictiondeck.gui_adapter")


# ═══════════════════════════════════════════════════════════════════════════
# Card operations (human only)
# ═══════════════════════════════════════════════════════════════════════════


def drag_card(artifact_id: str, to_position: int) -> dict:
    """Move a card to a new position on the Stage."""
    from pipeline.audit import log_event

    result = move_artifact(artifact_id, to_position)

    log_event(
        EventType.CARD_MOVED,
        actor="user",
        pathway="gui",
        payload={"artifact_id": artifact_id, "to_position": to_position},
    )
    return result


def gui_delete_card(artifact_id: str) -> dict:
    """Delete a card from the Stage. Only humans can delete.

    Committed (solid) cards cannot be deleted — only archived.
    """
    from pipeline.audit import log_event

    # Check if any judgment on this artifact is solid
    from pipeline.stage import _get_conn
    conn = _get_conn()
    solid = conn.execute(
        "SELECT COUNT(*) as cnt FROM judgment_objects "
        "WHERE artifact_id = ? AND state = ?",
        (artifact_id, ArtifactState.SOLID),
    ).fetchone()

    if solid and solid["cnt"] > 0:
        return {"error": "Cannot delete committed cards. They are HMAC sealed."}

    result = delete_artifact(artifact_id, deleted_by="user")

    log_event(
        EventType.CARD_DELETED,
        actor="user",
        pathway="gui",
        payload={"artifact_id": artifact_id},
    )
    return result


def edit_card_text(artifact_id: str, new_text: str) -> dict:
    """Edit a card's text. If judgment was verified, status reverts to draft."""
    from pipeline.audit import log_event
    from pipeline.stage import update_artifact, _get_conn

    result = update_artifact(artifact_id, content=new_text)

    # Revert any verified judgment on this artifact back to viscous
    conn = _get_conn()
    conn.execute(
        "UPDATE judgment_objects SET nli_verdict = NULL, nli_confidence = NULL "
        "WHERE artifact_id = ? AND state = ?",
        (artifact_id, ArtifactState.VISCOUS),
    )
    conn.commit()

    log_event(
        EventType.CARD_EDITED,
        actor="user",
        pathway="gui",
        payload={"artifact_id": artifact_id, "new_text": new_text[:200]},
    )
    return result


def gui_group_cards(name: str, card_ids: list[str]) -> dict:
    """Group cards together."""
    from pipeline.audit import log_event

    result = create_group(name=name, card_ids=card_ids, created_by="user")

    log_event(
        EventType.CARDS_GROUPED,
        actor="user",
        pathway="gui",
        payload={"group_id": result["group_id"], "name": name, "cards": card_ids},
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Parameter operations (human only: lock/unlock)
# ═══════════════════════════════════════════════════════════════════════════


def lock_parameter(judgment_id: str, param_name: str) -> dict:
    """Lock a parameter. AI cannot modify locked parameters.

    Lock = human confirmation. "I've verified this value."
    """
    from pipeline.audit import log_event

    result = add_overlay(
        target_id=judgment_id,
        target_type="judgment",
        overlay_type=OverlayType.LOCK,
        value={"param_name": param_name, "locked": True},
        created_by="user",
    )

    log_event(
        EventType.PARAM_LOCKED,
        actor="user",
        pathway="gui",
        payload={"judgment_id": judgment_id, "param_name": param_name},
    )
    return result


def unlock_parameter(judgment_id: str, param_name: str) -> dict:
    """Unlock a parameter. AI can modify again.

    Unlock = human withdrawing confirmation.
    """
    from pipeline.audit import log_event

    # Find and remove the lock overlay
    overlays = get_overlays(target_id=judgment_id)
    for o in overlays:
        val = json.loads(o.get("value", "{}"))
        if o["overlay_type"] == OverlayType.LOCK and val.get("param_name") == param_name:
            remove_overlay(o["overlay_id"])
            break

    log_event(
        EventType.PARAM_UNLOCKED,
        actor="user",
        pathway="gui",
        payload={"judgment_id": judgment_id, "param_name": param_name},
    )
    return {"status": "unlocked", "param_name": param_name}


# ═══════════════════════════════════════════════════════════════════════════
# Negative space (human only: dismiss)
# ═══════════════════════════════════════════════════════════════════════════


def dismiss_negative_space(artifact_id: str, reason: str) -> dict:
    """Dismiss a negative space flag. Only humans can dismiss.

    The flag is still in the audit trail — just marked as dismissed.
    """
    from pipeline.audit import log_event

    result = add_overlay(
        target_id=artifact_id,
        target_type="artifact",
        overlay_type="ns_dismissed",
        value={"reason": reason, "dismissed": True},
        created_by="user",
    )

    log_event(
        EventType.NEGATIVE_SPACE_DISMISSED,
        actor="user",
        pathway="gui",
        payload={"artifact_id": artifact_id, "reason": reason[:200]},
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Commit operations (human only via Friction Gate)
# ═══════════════════════════════════════════════════════════════════════════


def approve_commit(proposal_id: str, gate_token: str, engineer: str = "ranger") -> dict:
    """Approve a commit proposal via Friction Gate.

    Requires a valid gate_token (one-time, 60s expiry, server-generated).
    """
    from pipeline.audit import log_event
    from pipeline.friction_gate import consume_token

    # Validate gate token
    token_result = consume_token(gate_token, proposal_id)
    if not token_result["valid"]:
        log_event(
            EventType.GATE_CHALLENGE_FAILED,
            actor="user",
            pathway="gui",
            payload={"proposal_id": proposal_id, "reason": token_result["message"]},
        )
        return {"error": token_result["message"]}

    # Find all judgments with this proposal overlay
    from pipeline.stage import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT target_id FROM overlays WHERE overlay_type = 'proposal' "
        "AND value LIKE ?",
        (f'%{proposal_id}%',),
    ).fetchall()
    judgment_ids = [r["target_id"] for r in rows]

    if not judgment_ids:
        return {"error": f"No judgments found for proposal: {proposal_id}"}

    # Generate commit_id
    import hashlib
    from datetime import datetime, UTC
    commit_id = hashlib.sha256(
        f"{proposal_id}|{engineer}|{datetime.now(UTC).isoformat()}".encode()
    ).hexdigest()[:16]

    # Seal judgments (viscous → solid)
    seal_result = seal_judgments(judgment_ids, commit_id)

    log_event(
        EventType.COMMIT_APPROVED,
        actor="user",
        pathway="gui",
        payload={
            "commit_id": commit_id,
            "proposal_id": proposal_id,
            "judgment_ids": judgment_ids,
            "engineer": engineer,
            "friction_gate_passed": True,
            "sealed": seal_result["sealed"],
        },
    )

    logger.info("commit approved  id=%s  proposal=%s  sealed=%d",
                commit_id, proposal_id, seal_result["sealed"])
    return {
        "commit_id": commit_id,
        "proposal_id": proposal_id,
        "sealed": seal_result["sealed"],
        "version": seal_result["version"],
        "status": "committed",
    }


def reject_commit(proposal_id: str, reason: str) -> dict:
    """Reject a commit proposal."""
    from pipeline.audit import log_event

    # Remove proposal overlays
    from pipeline.stage import _get_conn
    conn = _get_conn()
    conn.execute(
        "DELETE FROM overlays WHERE overlay_type = 'proposal' AND value LIKE ?",
        (f'%{proposal_id}%',),
    )
    conn.commit()

    log_event(
        EventType.COMMIT_REJECTED,
        actor="user",
        pathway="gui",
        payload={"proposal_id": proposal_id, "reason": reason[:500]},
    )

    return {"proposal_id": proposal_id, "status": "rejected", "reason": reason}
