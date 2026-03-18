"""FrictionDeck v4 — MCP Adapter (AI's hands)

All operations an AI can perform through MCP. Physical isolation from gui_adapter.
CI scans enforce: zero intersection between mcp_adapter and gui_adapter function sets.

Two categories:
  Content (free):     drop_artifact, drop_note, attach_evidence
  Constraint (controlled): verify_claim, promote_to_judgment, flag_negative_space,
                           propose_commit, attach_relation, update_parameter

Every function returns a dict. mcp_server.py does json.dumps.
Every return value gets pending_alerts attached via _attach_alerts().
"""

import logging
from typing import Any

from pipeline.alert_queue import _attach_alerts, record_tool_call
from pipeline.constants import ArtifactState, EventType, OverlayType, SourceTrust, Verdict
from pipeline.stage import (
    add_overlay,
    add_relation,
    get_artifacts,
    get_judgments,
    get_overlays,
    get_relations,
    get_stage_diff,
    get_stage_state,
    get_version,
    promote_to_judgment,
    update_artifact,
    update_judgment_nli,
    write_artifact,
)

logger = logging.getLogger("frictiondeck.mcp_adapter")


# ═══════════════════════════════════════════════════════════════════════════
# T1 — READ (any agent can call)
# ═══════════════════════════════════════════════════════════════════════════


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


def wait_for_stage_update(last_known_version: int) -> dict:
    """Return changes since last_known_version.

    If version hasn't changed, returns {changed: false}.
    AI polls this to detect human actions on the GUI.
    """
    record_tool_call("wait_for_stage_update")
    return _attach_alerts(get_stage_diff(last_known_version))


def search_chunks(query: str, limit: int = 20) -> dict:
    """Search artifacts and judgments by text content.

    Uses simple LIKE matching. Phase 1 will add FTS5.
    """
    record_tool_call("search_chunks")
    from pipeline.stage import _get_conn

    conn = _get_conn()
    pattern = f"%{query}%"

    # Search artifacts
    artifacts = conn.execute(
        "SELECT * FROM artifacts WHERE deleted = 0 AND content LIKE ? LIMIT ?",
        (pattern, limit),
    ).fetchall()

    # Search judgments
    judgments = conn.execute(
        "SELECT * FROM judgment_objects WHERE claim_text LIKE ? LIMIT ?",
        (pattern, limit),
    ).fetchall()

    return _attach_alerts({
        "query": query,
        "artifacts": [dict(r) for r in artifacts],
        "judgments": [dict(r) for r in judgments],
        "total": len(artifacts) + len(judgments),
    })


def search_commits(
    query: str | None = None,
    engineer: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Search committed judgments (solid state)."""
    record_tool_call("search_commits")

    judgments = get_judgments(state=ArtifactState.SOLID)

    # Basic filtering
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


# ═══════════════════════════════════════════════════════════════════════════
# T2 — SPECULATE (agent + logged)
# Content tools: free, AI drops whatever it wants
# ═══════════════════════════════════════════════════════════════════════════


def drop_artifact(
    content: str,
    content_type: str = "html",
    metadata: dict | None = None,
    source_trust: str = "grey",
) -> dict:
    """Drop an artifact onto the Stage (fluid state).

    content_type: "html", "markdown", "svg", "text", "jsx"
    source_trust: "green" (has original), "yellow" (external), "grey" (AI assertion)
    """
    record_tool_call("drop_artifact")
    from pipeline.audit import log_event

    result = write_artifact(
        content=content,
        content_type=content_type,
        metadata=metadata,
        source_trust=source_trust,
        created_by="ai",
    )

    log_event(
        EventType.ARTIFACT_DROPPED,
        actor="ai",
        pathway="mcp",
        payload={"artifact_id": result["artifact_id"], "content_type": content_type},
    )

    return _attach_alerts(result)


def drop_note(text: str, metadata: dict | None = None) -> dict:
    """Shortcut: drop a text note (plain text artifact)."""
    record_tool_call("drop_note")
    return drop_artifact(content=text, content_type="text", metadata=metadata)


def attach_evidence(
    artifact_id: str,
    evidence_text: str,
    source: str | None = None,
) -> dict:
    """Attach evidence to an existing artifact."""
    record_tool_call("attach_evidence")
    from pipeline.audit import log_event

    current_meta = {}
    artifacts = get_artifacts()
    for a in artifacts:
        if a["artifact_id"] == artifact_id:
            import json
            current_meta = json.loads(a.get("metadata", "{}"))
            break

    evidence_list = current_meta.get("evidence", [])
    evidence_list.append({"text": evidence_text, "source": source})
    current_meta["evidence"] = evidence_list

    # Upgrade trust if source is provided
    source_trust = SourceTrust.YELLOW if source else SourceTrust.GREY

    result = update_artifact(artifact_id, metadata=current_meta)

    log_event(
        EventType.ARTIFACT_UPDATED,
        actor="ai",
        pathway="mcp",
        payload={"artifact_id": artifact_id, "evidence_source": source},
    )

    return _attach_alerts(result)


# ═══════════════════════════════════════════════════════════════════════════
# T2 — Constraint tools: controlled, state-changing
# ═══════════════════════════════════════════════════════════════════════════


def mcp_promote_to_judgment(
    artifact_id: str,
    claim_text: str,
    params: list[dict] | None = None,
) -> dict:
    """Promote an artifact to a judgment object (fluid → viscous).

    This is a significant action — the judgment object enters the audit layer
    and can eventually be committed + HMAC sealed.
    """
    record_tool_call("promote_to_judgment")
    from pipeline.audit import log_event

    result = promote_to_judgment(
        artifact_id=artifact_id,
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
            "artifact_id": artifact_id,
            "claim_text": claim_text[:200],
        },
    )

    return _attach_alerts(result)


def verify_claim(
    judgment_id: str,
    evidence_text: str | None = None,
) -> dict:
    """Run NLI verification on a judgment object.

    Uses DeBERTa (encoder verifies decoder — architectural independence).
    """
    record_tool_call("verify_claim")
    from pipeline.audit import log_event

    # Get the judgment's claim text
    judgments = get_judgments()
    target = None
    for j in judgments:
        if j["judgment_id"] == judgment_id:
            target = j
            break

    if not target:
        return _attach_alerts({"error": f"judgment not found: {judgment_id}"})

    # TODO Phase 1: call pipeline.nli with DeBERTa
    # For now, return neutral (DeBERTa not yet integrated in v4)
    verdict = Verdict.NEUTRAL
    confidence = 0.5

    result = update_judgment_nli(judgment_id, verdict, confidence)

    log_event(
        EventType.NLI_VERIFIED,
        actor="ai",
        pathway="mcp",
        payload={
            "judgment_id": judgment_id,
            "verdict": verdict,
            "confidence": confidence,
        },
    )

    result["verdict"] = verdict
    result["confidence"] = confidence
    return _attach_alerts(result)


def flag_negative_space(description: str, related_ids: list[str] | None = None) -> dict:
    """Flag something that's MISSING from the analysis.

    Only AI can flag. Only human can dismiss.
    """
    record_tool_call("flag_negative_space")
    from pipeline.audit import log_event

    # Create an artifact for the negative space flag
    artifact_result = write_artifact(
        content=f"⚠️ NEGATIVE SPACE: {description}",
        content_type="text",
        metadata={"type": "negative_space", "related_ids": related_ids or []},
        source_trust=SourceTrust.GREY,
        created_by="ai",
    )

    # Add negative_space overlay
    add_overlay(
        target_id=artifact_result["artifact_id"],
        target_type="artifact",
        overlay_type=OverlayType.NEGATIVE_SPACE,
        value={"description": description, "related_ids": related_ids or []},
        created_by="ai",
    )

    log_event(
        EventType.NEGATIVE_SPACE_FLAGGED,
        actor="ai",
        pathway="mcp",
        payload={
            "artifact_id": artifact_result["artifact_id"],
            "description": description[:200],
        },
    )

    return _attach_alerts(artifact_result)


def mcp_attach_relation(
    from_id: str,
    to_id: str,
    relation_type: str = "depends_on",
    label: str | None = None,
) -> dict:
    """Create a relation between two cards."""
    record_tool_call("attach_relation")
    from pipeline.audit import log_event

    result = add_relation(
        from_id=from_id,
        to_id=to_id,
        relation_type=relation_type,
        label=label,
        created_by="ai",
    )

    log_event(
        EventType.RELATION_ADDED,
        actor="ai",
        pathway="mcp",
        payload={
            "relation_id": result["relation_id"],
            "from_id": from_id,
            "to_id": to_id,
            "type": relation_type,
        },
    )

    return _attach_alerts(result)


def update_parameter(
    judgment_id: str,
    param_name: str,
    new_value: Any,
) -> dict:
    """Update a parameter on a judgment object (if not locked)."""
    record_tool_call("update_parameter")
    import json
    from pipeline.audit import log_event
    from pipeline.stage import _get_conn

    conn = _get_conn()

    # Check if parameter is locked
    overlays = get_overlays(target_id=judgment_id)
    for o in overlays:
        import json as _json
        val = _json.loads(o.get("value", "{}"))
        if o["overlay_type"] == OverlayType.LOCK and val.get("param_name") == param_name:
            return _attach_alerts({
                "error": f"Parameter '{param_name}' is locked. Only humans can unlock.",
                "locked": True,
            })

    # Get current params
    row = conn.execute(
        "SELECT params FROM judgment_objects WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    if not row:
        return _attach_alerts({"error": f"judgment not found: {judgment_id}"})

    params = json.loads(row["params"])
    old_value = None
    for p in params:
        if p.get("name") == param_name:
            old_value = p.get("value")
            p["value"] = new_value
            break
    else:
        params.append({"name": param_name, "value": new_value})

    from datetime import datetime, UTC
    ts = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE judgment_objects SET params = ?, updated_at = ? WHERE judgment_id = ?",
            (json.dumps(params, ensure_ascii=False), ts, judgment_id),
        )
        from pipeline.stage import _bump_version
        version = _bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_event(
        EventType.PARAM_UPDATED,
        actor="ai",
        pathway="mcp",
        payload={
            "judgment_id": judgment_id,
            "param_name": param_name,
            "old_value": old_value,
            "new_value": new_value,
        },
    )

    return _attach_alerts({"version": version, "param_name": param_name, "new_value": new_value})


# ═══════════════════════════════════════════════════════════════════════════
# T3 — PROPOSE (AI can propose, only human can approve)
# ═══════════════════════════════════════════════════════════════════════════


def propose_commit(
    judgment_ids: list[str],
    message: str,
) -> dict:
    """Propose a commit of judgment objects.

    AI can only propose. Human approves via Friction Gate (GUI).
    The proposal is stored as an overlay on each judgment.
    """
    record_tool_call("propose_commit")
    from pipeline.audit import log_event
    import json

    proposal_id = __import__("uuid").uuid4().hex

    # Validate all judgments exist and are viscous
    judgments = get_judgments(state=ArtifactState.VISCOUS)
    valid_ids = {j["judgment_id"] for j in judgments}
    invalid = [jid for jid in judgment_ids if jid not in valid_ids]
    if invalid:
        return _attach_alerts({
            "error": f"Invalid or non-viscous judgment IDs: {invalid}",
            "proposal_id": None,
        })

    # Create proposal overlay on each judgment
    for jid in judgment_ids:
        add_overlay(
            target_id=jid,
            target_type="judgment",
            overlay_type="proposal",
            value={"proposal_id": proposal_id, "message": message},
            created_by="ai",
        )

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

    logger.info("commit proposed  proposal=%s  judgments=%d", proposal_id, len(judgment_ids))
    return _attach_alerts({
        "proposal_id": proposal_id,
        "judgment_ids": judgment_ids,
        "message": message,
        "status": "pending_approval",
    })
