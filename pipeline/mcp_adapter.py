"""FrictionDeck v4 — MCP Adapter (AI's hands) — Multi-Stage

Physical isolation from gui_adapter. CI enforces zero intersection.
Every return value gets pending_alerts attached via _attach_alerts().
All tools accept stage parameter (default: "default").
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
    list_stages as _list_stages,
    create_stage as _create_stage,
)

logger = logging.getLogger("frictiondeck.mcp_adapter")


# ── T1 — READ ────────────────────────────────────────────────────────────

def get_world_state(stage: str = "default") -> dict:
    """Return complete world state for AI context recovery."""
    record_tool_call("get_world_state")
    from pipeline.history import get_events
    state = get_stage_state(stage)
    state["recent_history"] = get_events(limit=20, stage=stage)
    return _attach_alerts(state)


def mcp_get_stage_state(stage: str = "default") -> dict:
    record_tool_call("get_stage_state")
    return _attach_alerts(get_stage_state(stage))


def mcp_get_stage_html(stage: str = "default") -> dict:
    record_tool_call("get_stage_html")
    return _attach_alerts({"html": get_html(stage), "version": get_version(stage), "stage": stage})


def mcp_get_stage_summary(stage: str = "default") -> dict:
    record_tool_call("get_stage_summary")
    judgments = get_judgments(stage=stage)
    return _attach_alerts({
        "stage": stage,
        "version": get_version(stage),
        "judgments": judgments,
        "judgment_count": len(judgments),
    })


def wait_for_stage_update(last_known_version: int, stage: str = "default") -> dict:
    record_tool_call("wait_for_stage_update")
    return _attach_alerts(get_stage_diff(last_known_version, stage))


def search_commits(
    query: str | None = None,
    engineer: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    stage: str = "default",
) -> dict:
    """Search committed judgments (solid state)."""
    record_tool_call("search_commits")
    judgments = get_judgments(state=JudgmentState.SOLID, stage=stage)
    results = judgments
    if query:
        q = query.lower()
        results = [j for j in results if q in j.get("claim_text", "").lower()]
    if date_from:
        results = [j for j in results if j.get("created_at", "") >= date_from]
    if date_to:
        results = [j for j in results if j.get("created_at", "") <= date_to]
    return _attach_alerts({"query": query, "results": results, "total": len(results)})


def get_history(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    stage: str = "default",
) -> dict:
    """Return history events."""
    record_tool_call("get_history")
    from pipeline.history import get_events
    return _attach_alerts({
        "events": get_events(limit=limit, offset=offset, event_type=event_type, stage=stage),
    })


def get_csp_whitelist() -> dict:
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


# ── MULTI-STAGE ──────────────────────────────────────────────────────────

def mcp_list_stages() -> dict:
    record_tool_call("list_stages")
    return _attach_alerts({"stages": _list_stages()})


def mcp_create_stage(name: str) -> dict:
    record_tool_call("create_stage")
    from pipeline.history import log_event
    result = _create_stage(name)
    if "error" not in result:
        log_event(
            EventType.STAGE_MUTATED,
            actor="ai", pathway="mcp",
            payload={"action": "create_stage", "stage_name": result["name"]},
            stage=result["name"],
        )
    return _attach_alerts(result)


# ── T2 — DOM operations ──────────────────────────────────────────────────

def mutate_stage(selector: str, new_html: str, stage: str = "default") -> dict:
    record_tool_call("mutate_stage")
    from pipeline.history import log_event
    result = set_html(new_html, stage)
    log_event(EventType.STAGE_MUTATED, actor="ai", pathway="mcp",
              payload={"selector": selector, "html_length": len(new_html)}, stage=stage)
    return _attach_alerts({"action": "mutate", "stage": stage, "version": result["version"]})


def append_stage(parent_selector: str, html: str, stage: str = "default") -> dict:
    record_tool_call("append_stage")
    from pipeline.history import log_event
    current = get_html(stage)
    result = set_html(current + html, stage)
    log_event(EventType.STAGE_APPENDED, actor="ai", pathway="mcp",
              payload={"parent_selector": parent_selector, "html_length": len(html)}, stage=stage)
    return _attach_alerts({"action": "append", "stage": stage, "version": result["version"]})


def query_stage(selector: str, stage: str = "default") -> dict:
    record_tool_call("query_stage")
    return _attach_alerts({
        "html": get_html(stage), "selector": selector,
        "stage": stage, "version": get_version(stage),
    })


# ── T2 — Constraint tools ────────────────────────────────────────────────

def mcp_promote_to_judgment(
    claim_text: str, params: list[dict] | None = None, stage: str = "default",
) -> dict:
    record_tool_call("promote_to_judgment")
    from pipeline.history import log_event
    result = promote_to_judgment(claim_text=claim_text, params=params, created_by="ai", stage=stage)
    log_event(EventType.JUDGMENT_PROMOTED, actor="ai", pathway="mcp",
              payload={"judgment_id": result["judgment_id"], "claim_text": claim_text[:200]}, stage=stage)
    return _attach_alerts(result)


def flag_negative_space(description: str, severity: str = "medium", stage: str = "default") -> dict:
    record_tool_call("flag_negative_space")
    from pipeline.history import log_event
    log_event(EventType.NEGATIVE_SPACE_FLAGGED, actor="ai", pathway="mcp",
              payload={"description": description[:200], "severity": severity}, stage=stage)
    return _attach_alerts({
        "status": "flagged", "description": description,
        "severity": severity, "stage": stage, "version": get_version(stage),
    })


# ── T3 — PROPOSE ─────────────────────────────────────────────────────────

def propose_commit(judgment_ids: list[str], message: str, stage: str = "default") -> dict:
    record_tool_call("propose_commit")
    from pipeline.history import log_event
    from uuid import uuid4

    proposal_id = uuid4().hex
    judgments = get_judgments(state=JudgmentState.VISCOUS, stage=stage)
    valid_ids = {j["judgment_id"] for j in judgments}
    invalid = [jid for jid in judgment_ids if jid not in valid_ids]
    if invalid:
        return _attach_alerts({"error": f"Invalid or non-viscous judgment IDs: {invalid}", "proposal_id": None})

    log_event(EventType.COMMIT_PROPOSED, actor="ai", pathway="mcp",
              payload={"proposal_id": proposal_id, "judgment_ids": judgment_ids, "message": message[:500]},
              stage=stage)
    logger.info("commit proposed  stage=%s  proposal=%s  judgments=%d", stage, proposal_id, len(judgment_ids))
    return _attach_alerts({
        "proposal_id": proposal_id, "judgment_ids": judgment_ids,
        "message": message, "stage": stage, "status": "pending_approval",
    })
