"""FrictionDeck v4 — Single-process server

One port. Four entry points:
  /       → GUI (static index.html)
  /ws     → WebSocket broadcast
  /api    → REST API
  /docs   → Swagger

FastAPI serves everything. Zero Redis. Zero microservices.
"""

import logging
import httpx

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from pipeline.config import (
    PORT, VERSION, PROXY_WHITELIST,
    CSP_SCRIPT_WHITELIST, CSP_STYLE_WHITELIST, CSP_FONT_WHITELIST,
    setup_logging,
)
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
    description="Engineering judgment infrastructure.",
    docs_url="/docs",
)


# ── CSP middleware ────────────────────────────────────────────────────────

def _build_csp() -> str:
    """Build CSP header from config whitelists."""
    scripts = " ".join(CSP_SCRIPT_WHITELIST)
    styles = " ".join(CSP_STYLE_WHITELIST)
    fonts = " ".join(CSP_FONT_WHITELIST)
    return (
        "default-src 'self'; "
        f"script-src 'unsafe-inline' {scripts}; "
        "connect-src 'self'; "
        "frame-src 'self'; "
        "img-src * data:; "
        f"style-src 'unsafe-inline' {styles}; "
        f"font-src {fonts}"
    ).strip()

CSP = _build_csp()


@app.middleware("http")
async def add_csp_header(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        response.headers["Content-Security-Policy"] = CSP
    return response


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


@app.get("/api/proposals")
async def api_proposals():
    """Return pending commit proposals that still have viscous judgments."""
    import json as _json
    from pipeline.audit import get_audit_log
    from pipeline.stage import get_judgments
    from pipeline.constants import JudgmentState

    def _payload(e):
        p = e.get("payload")
        if isinstance(p, str):
            p = _json.loads(p)
        return p or {}

    # No viscous judgments → no actionable proposals
    viscous_ids = {j["judgment_id"] for j in get_judgments(state=JudgmentState.VISCOUS)}
    if not viscous_ids:
        return []

    proposed = get_audit_log(limit=100, event_type="commit_proposed")
    approved = get_audit_log(limit=100, event_type="commit_approved")
    rejected = get_audit_log(limit=100, event_type="commit_rejected")

    closed_ids = set()
    for e in approved + rejected:
        closed_ids.add(_payload(e).get("proposal_id"))

    pending = []
    for e in proposed:
        p = _payload(e)
        pid = p.get("proposal_id")
        if pid and pid not in closed_ids:
            jids = p.get("judgment_ids", [])
            # Only show if at least one judgment is still viscous
            if any(jid in viscous_ids for jid in jids):
                pending.append({
                    "proposal_id": pid,
                    "judgment_ids": jids,
                    "message": p.get("message", ""),
                    "timestamp": e.get("timestamp", ""),
                })
    return pending


@app.get("/api/commits")
async def api_commits():
    """Return commit log: approved + rejected, with messages from proposals."""
    import json as _json
    from pipeline.audit import get_audit_log

    def _payload(e):
        p = e.get("payload")
        if isinstance(p, str):
            p = _json.loads(p)
        return p or {}

    # Build proposal_id → message lookup from proposed events
    proposed = get_audit_log(limit=200, event_type="commit_proposed")
    msg_map = {}
    for e in proposed:
        p = _payload(e)
        pid = p.get("proposal_id")
        if pid:
            msg_map[pid] = p.get("message", "")

    entries = []

    for e in get_audit_log(limit=100, event_type="commit_approved"):
        p = _payload(e)
        pid = p.get("proposal_id", "")
        entries.append({
            "type": "approved",
            "commit_id": p.get("commit_id", ""),
            "proposal_id": pid,
            "engineer": p.get("engineer", ""),
            "message": msg_map.get(pid, ""),
            "sealed_count": p.get("sealed", 0),
            "hmac": e.get("event_hash", ""),
            "timestamp": e.get("timestamp", ""),
        })

    for e in get_audit_log(limit=100, event_type="commit_rejected"):
        p = _payload(e)
        pid = p.get("proposal_id", "")
        entries.append({
            "type": "rejected",
            "proposal_id": pid,
            "reason": p.get("reason", ""),
            "message": msg_map.get(pid, ""),
            "timestamp": e.get("timestamp", ""),
        })

    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return entries


@app.get("/api/audit/verify")
async def api_verify_chain():
    """Verify audit chain integrity."""
    from pipeline.audit import verify_chain
    return verify_chain()


# ── GUI adapter API routes (human-only operations) ───────────────────────

@app.post("/api/param/lock")
async def api_lock_param(judgment_id: str, param_name: str):
    from pipeline.gui_adapter import lock_parameter
    return lock_parameter(judgment_id, param_name)


@app.post("/api/param/unlock")
async def api_unlock_param(judgment_id: str, param_name: str):
    from pipeline.gui_adapter import unlock_parameter
    return unlock_parameter(judgment_id, param_name)


@app.post("/api/commit/approve")
async def api_approve_commit(proposal_id: str, engineer: str = "ranger"):
    from pipeline.gui_adapter import approve_commit
    return approve_commit(proposal_id, engineer)


@app.post("/api/commit/reject")
async def api_reject_commit(proposal_id: str, reason: str):
    from pipeline.gui_adapter import reject_commit
    return reject_commit(proposal_id, reason)


@app.post("/api/csp/add")
async def api_add_csp_domain(category: str, domain: str):
    """Add a domain to CSP whitelist. Requires server restart to take effect on CSP header."""
    from pipeline.gui_adapter import add_csp_domain
    return add_csp_domain(category, domain)


# ── Proxy layer ──────────────────────────────────────────────────────────

@app.api_route("/proxy/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(service: str, path: str, request: Request):
    """Forward request to whitelisted service. Audit logged."""
    from pipeline.audit import log_event
    from pipeline.constants import EventType

    base_url = PROXY_WHITELIST.get(service)
    if not base_url:
        return Response(
            content=f'{{"error":"service not whitelisted: {service}"}}',
            status_code=403,
            media_type="application/json",
        )

    target = f"{base_url}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method=request.method,
            url=target,
            headers=headers,
            content=body if body else None,
        )

    log_event(
        EventType.PROXY_FORWARDED,
        actor="iframe",
        pathway="proxy",
        payload={
            "service": service,
            "path": f"/{path}",
            "method": request.method,
            "status_code": resp.status_code,
        },
    )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ── Static files (must be last — catch-all) ──────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
