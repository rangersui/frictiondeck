"""FrictionDeck v4 — Single-process server

One port. Four entry points:
  /       → GUI (static index.html)
  /mcp    → MCP endpoint (stdio + SSE)
  /api    → REST API
  /ws     → WebSocket broadcast
  /docs   → Swagger

FastAPI serves everything. Zero Redis. Zero microservices.
"""

import asyncio
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import PORT, VERSION, setup_logging
from pipeline.broadcast import broadcast, subscribe, unsubscribe
from pipeline.stage import set_broadcast, init_stage_db
from pipeline.audit import init_audit_db

# ── Logging ──────────────────────────────────────────────────────────────
setup_logging()
logger = logging.getLogger("frictiondeck.server")

# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="FrictionDeck",
    version=VERSION,
    description="Engineering judgment infrastructure. AI remembers everything. You just judge.",
    docs_url="/docs",
)


# ── Startup ──────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Initialize databases and wire broadcast."""
    init_audit_db()
    init_stage_db()
    set_broadcast(broadcast)
    logger.info("FrictionDeck v%s started on port %d", VERSION, PORT)


# ── WebSocket broadcast ──────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for real-time Stage updates."""
    await ws.accept()
    queue = subscribe()
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("ws error: %s", exc)
    finally:
        unsubscribe(queue)


# ── API routes ───────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Health check."""
    from pipeline.stage import get_version
    from pipeline.audit import verify_chain
    from pipeline.broadcast import subscriber_count

    chain = verify_chain(limit=10)
    return {
        "status": "ok",
        "version": VERSION,
        "stage_version": get_version(),
        "audit_chain_valid": chain["valid"],
        "audit_events": chain["total_events"],
        "ws_subscribers": subscriber_count(),
    }


@app.get("/api/stage")
async def api_stage():
    """Return current stage state."""
    from pipeline.stage import get_stage_state
    return get_stage_state()


@app.get("/api/audit")
async def api_audit(limit: int = 50, offset: int = 0):
    """Return audit log."""
    from pipeline.audit import get_audit_log
    return get_audit_log(limit=limit, offset=offset)


@app.get("/api/audit/verify")
async def api_verify_chain():
    """Verify audit chain integrity."""
    from pipeline.audit import verify_chain
    return verify_chain()


# ── GUI adapter API routes (human-only operations) ───────────────────────

@app.post("/api/card/move")
async def api_move_card(artifact_id: str, to_position: int):
    from pipeline.gui_adapter import drag_card
    return drag_card(artifact_id, to_position)


@app.delete("/api/card/{artifact_id}")
async def api_delete_card(artifact_id: str):
    from pipeline.gui_adapter import gui_delete_card
    return gui_delete_card(artifact_id)


@app.post("/api/card/edit")
async def api_edit_card(artifact_id: str, new_text: str):
    from pipeline.gui_adapter import edit_card_text
    return edit_card_text(artifact_id, new_text)


@app.post("/api/param/lock")
async def api_lock_param(judgment_id: str, param_name: str):
    from pipeline.gui_adapter import lock_parameter
    return lock_parameter(judgment_id, param_name)


@app.post("/api/param/unlock")
async def api_unlock_param(judgment_id: str, param_name: str):
    from pipeline.gui_adapter import unlock_parameter
    return unlock_parameter(judgment_id, param_name)


@app.post("/api/gate/challenge")
async def api_gate_challenge(proposal_id: str, session_id: str = "default"):
    from pipeline.friction_gate import generate_challenge
    return generate_challenge(proposal_id, session_id)


@app.post("/api/gate/validate")
async def api_gate_validate(
    challenge_id: str, proposal_id: str, answer: str, session_id: str = "default",
):
    from pipeline.friction_gate import validate_answer
    return validate_answer(challenge_id, proposal_id, answer, session_id)


@app.post("/api/commit/approve")
async def api_approve_commit(proposal_id: str, gate_token: str, engineer: str = "ranger"):
    from pipeline.gui_adapter import approve_commit
    return approve_commit(proposal_id, gate_token, engineer)


@app.post("/api/commit/reject")
async def api_reject_commit(proposal_id: str, reason: str):
    from pipeline.gui_adapter import reject_commit
    return reject_commit(proposal_id, reason)


@app.post("/api/ns/dismiss")
async def api_dismiss_ns(artifact_id: str, reason: str):
    from pipeline.gui_adapter import dismiss_negative_space
    return dismiss_negative_space(artifact_id, reason)


# ── Static files (must be last — catch-all) ──────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
