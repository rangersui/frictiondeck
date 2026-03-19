"""FrictionDeck v4 — GUI Adapter (Human's hands)

Physical isolation from mcp_adapter — zero intersection.
Functions exist but have no routes registered in server.py until UI is built.

Operations only humans can do:
  approve_commit, reject_commit
"""

import hashlib
import logging
from datetime import datetime, UTC

from pipeline.constants import EventType, JudgmentState
from pipeline.stage import get_judgments, seal_judgments

logger = logging.getLogger("frictiondeck.gui_adapter")


def _generate_commit_id(proposal_id: str, engineer: str) -> str:
    return hashlib.sha256(
        f"{proposal_id}|{engineer}|{datetime.now(UTC).isoformat()}".encode()
    ).hexdigest()[:16]


def approve_commit(
    proposal_id: str,
    engineer: str = "ranger",
    stage: str = "default",
) -> dict:
    """Approve a commit proposal. Direct seal + HMAC."""
    from pipeline.history import log_event

    judgments = get_judgments(state=JudgmentState.VISCOUS, stage=stage)
    judgment_ids = [j["judgment_id"] for j in judgments]
    if not judgment_ids:
        return {"error": "No viscous judgments to commit."}

    commit_id = _generate_commit_id(proposal_id, engineer)
    seal_result = seal_judgments(judgment_ids, commit_id, stage=stage)

    log_event(
        EventType.COMMIT_APPROVED, actor="user", pathway="gui",
        payload={
            "commit_id": commit_id, "proposal_id": proposal_id,
            "judgment_ids": judgment_ids, "engineer": engineer,
            "sealed": seal_result["sealed"],
        },
        stage=stage,
    )

    logger.info("commit approved  stage=%s  id=%s  sealed=%d", stage, commit_id, seal_result["sealed"])
    return {
        "commit_id": commit_id, "proposal_id": proposal_id,
        "sealed": seal_result["sealed"], "version": seal_result["version"],
        "status": "committed",
    }


def reject_commit(proposal_id: str, reason: str, stage: str = "default") -> dict:
    """Reject a commit proposal."""
    from pipeline.history import log_event

    log_event(
        EventType.COMMIT_REJECTED, actor="user", pathway="gui",
        payload={"proposal_id": proposal_id, "reason": reason[:500]},
        stage=stage,
    )
    return {"proposal_id": proposal_id, "status": "rejected", "reason": reason}


def add_csp_domain(category: str, domain: str) -> dict:
    """Add a domain to CSP whitelist. Human only."""
    from pipeline.history import log_event
    from pipeline import config

    lists = {
        "script": config.CSP_SCRIPT_WHITELIST,
        "style": config.CSP_STYLE_WHITELIST,
        "font": config.CSP_FONT_WHITELIST,
    }
    target = lists.get(category)
    if target is None:
        return {"error": f"Invalid category: {category}. Use: script, style, font"}
    if domain in target:
        return {"status": "already_present", "category": category, "domain": domain}

    target.append(domain)

    log_event(
        EventType.CSP_DOMAIN_ADDED, actor="user", pathway="gui",
        payload={"category": category, "domain": domain},
    )
    return {"status": "added", "category": category, "domain": domain}
