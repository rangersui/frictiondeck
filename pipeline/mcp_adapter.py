"""FrictionDeck v4 — MCP Adapter (AI's hands)

Physical isolation from gui_adapter. CI enforces zero intersection.
Every return value gets pending_alerts attached via _attach_alerts().
"""

import logging

from pipeline.alert_queue import _attach_alerts, record_tool_call
from pipeline.constants import EventType, JudgmentState
from pipeline.stage import (
    get_html,
    get_judgments,
    get_stage_diff,
    get_stage_state,
    get_version,
    promote_to_judgment,
    set_html,
)

logger = logging.getLogger("frictiondeck.mcp_adapter")


# ── T1 — READ ────────────────────────────────────────────────────────────

def get_world_state() -> dict:
    """Return complete world state for AI context recovery."""
    record_tool_call("get_world_state")
    from pipeline.audit import get_audit_log
    state = get_stage_state()
    state["recent_audit"] = get_audit_log(limit=20)
    return _attach_alerts(state)


def mcp_get_stage_state() -> dict:
    """Return current stage state snapshot."""
    record_tool_call("get_stage_state")
    return _attach_alerts(get_stage_state())


def mcp_get_stage_html() -> dict:
    """Return current stage HTML (full DOM)."""
    record_tool_call("get_stage_html")
    return _attach_alerts({"html": get_html(), "version": get_version()})


def mcp_get_stage_summary() -> dict:
    """Return structured summary of stage (judgments + version, no HTML)."""
    record_tool_call("get_stage_summary")
    return _attach_alerts({
        "version": get_version(),
        "judgments": get_judgments(),
        "judgment_count": len(get_judgments()),
    })


def wait_for_stage_update(last_known_version: int) -> dict:
    """Return changes since last_known_version."""
    record_tool_call("wait_for_stage_update")
    return _attach_alerts(get_stage_diff(last_known_version))


def search_commits(
    query: str | None = None,
    engineer: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Search committed judgments (solid state)."""
    record_tool_call("search_commits")

    judgments = get_judgments(state=JudgmentState.SOLID)

    results = judgments
    if query:
        q = query.lower()
        results = [j for j in results if q in j.get("claim_text", "").lower()]
    if date_from:
        results = [j for j in results if j.get("created_at", "") >= date_from]
    if date_to:
        results = [j for j in results if j.get("created_at", "") <= date_to]

    return _attach_alerts({
        "query": query,
        "results": results,
        "total": len(results),
    })


def get_audit_trail(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
) -> dict:
    """Return audit log entries."""
    record_tool_call("get_audit_trail")
    from pipeline.audit import get_audit_log
    return _attach_alerts({
        "events": get_audit_log(limit=limit, offset=offset, event_type=event_type),
    })


def get_csp_whitelist() -> dict:
    """Return current CSP whitelist domains."""
    record_tool_call("get_csp_whitelist")
    from pipeline.config import (
        CSP_SCRIPT_WHITELIST, CSP_STYLE_WHITELIST, CSP_FONT_WHITELIST,
        PERSONAL_MODE,
    )
    return _attach_alerts({
        "mode": "personal" if PERSONAL_MODE else "enterprise",
        "script_src": list(CSP_SCRIPT_WHITELIST),
        "style_src": list(CSP_STYLE_WHITELIST),
        "font_src": list(CSP_FONT_WHITELIST),
    })


# ── T2 — DOM operations ──────────────────────────────────────────────────

def mutate_stage(selector: str, new_html: str) -> dict:
    """Full replacement of stage HTML. Selector is logged for audit only.

    AI owns the HTML — pass the complete new version.
    """
    record_tool_call("mutate_stage")
    from pipeline.audit import log_event

    result = set_html(new_html)

    log_event(
        EventType.STAGE_MUTATED,
        actor="ai",
        pathway="mcp",
        payload={"selector": selector, "html_length": len(new_html)},
    )

    return _attach_alerts({
        "action": "mutate",
        "selector": selector,
        "version": result["version"],
    })


def append_stage(parent_selector: str, html: str) -> dict:
    """Append html to current stage HTML (concatenation).

    parent_selector is logged for audit only — actual operation is
    string append to the end of stage_html. AI owns the structure.
    """
    record_tool_call("append_stage")
    from pipeline.audit import log_event

    current = get_html()
    result = set_html(current + html)

    log_event(
        EventType.STAGE_APPENDED,
        actor="ai",
        pathway="mcp",
        payload={"parent_selector": parent_selector, "html_length": len(html)},
    )

    return _attach_alerts({
        "action": "append",
        "parent_selector": parent_selector,
        "version": result["version"],
    })



def query_stage(selector: str) -> dict:
    """Return full stage HTML. AI finds what it needs in context.

    Selector is logged for audit/intent. Returns entire stage_html —
    AI generated it, AI knows the structure.
    """
    record_tool_call("query_stage")
    return _attach_alerts({
        "html": get_html(),
        "selector": selector,
        "version": get_version(),
    })


# ── T2 — Constraint tools ────────────────────────────────────────────────

def mcp_promote_to_judgment(
    claim_text: str,
    params: list[dict] | None = None,
) -> dict:
    """Promote a claim to a judgment object (viscous state)."""
    record_tool_call("promote_to_judgment")
    from pipeline.audit import log_event

    result = promote_to_judgment(
        claim_text=claim_text,
        params=params,
        created_by="ai",
    )

    log_event(
        EventType.JUDGMENT_PROMOTED,
        actor="ai",
        pathway="mcp",
        payload={
            "judgment_id": result["judgment_id"],
            "claim_text": claim_text[:200],
        },
    )

    return _attach_alerts(result)


def flag_negative_space(description: str, severity: str = "medium") -> dict:
    """Flag something MISSING. Only AI can flag, only human can dismiss."""
    record_tool_call("flag_negative_space")
    from pipeline.audit import log_event

    log_event(
        EventType.NEGATIVE_SPACE_FLAGGED,
        actor="ai",
        pathway="mcp",
        payload={"description": description[:200], "severity": severity},
    )

    return _attach_alerts({
        "status": "flagged",
        "description": description,
        "severity": severity,
        "version": get_version(),
    })


# ── T3 — PROPOSE ─────────────────────────────────────────────────────────

def propose_commit(judgment_ids: list[str], message: str) -> dict:
    """Propose a commit. AI proposes, human approves via Friction Gate."""
    record_tool_call("propose_commit")
    from pipeline.audit import log_event
    from uuid import uuid4

    proposal_id = uuid4().hex

    # Validate all judgments exist and are viscous
    judgments = get_judgments(state=JudgmentState.VISCOUS)
    valid_ids = {j["judgment_id"] for j in judgments}
    invalid = [jid for jid in judgment_ids if jid not in valid_ids]
    if invalid:
        return _attach_alerts({
            "error": f"Invalid or non-viscous judgment IDs: {invalid}",
            "proposal_id": None,
        })

    log_event(
        EventType.COMMIT_PROPOSED,
        actor="ai",
        pathway="mcp",
        payload={
            "proposal_id": proposal_id,
            "judgment_ids": judgment_ids,
            "message": message[:500],
        },
    )

    logger.info("commit proposed  proposal=%s  judgments=%d",
                proposal_id, len(judgment_ids))
    return _attach_alerts({
        "proposal_id": proposal_id,
        "judgment_ids": judgment_ids,
        "message": message,
        "status": "pending_approval",
    })
