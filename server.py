"""FrictionDeck v4 — Single-process server (Multi-Stage)

One port. Three entry points:
  /             → Stage list
  /<name>       → Stage view (iframe + poll)
  /api          → REST API
  /proxy        → Whitelisted API proxy
  /ws           → WebSocket broadcast

FastAPI serves everything. Zero Redis. Zero microservices.
"""

import logging
import httpx

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from pipeline.config import (
    PORT, VERSION, PROXY_WHITELIST,
    CSP_SCRIPT_WHITELIST, CSP_STYLE_WHITELIST, CSP_FONT_WHITELIST,
    setup_logging,
)
from pipeline.broadcast import broadcast, subscribe, unsubscribe
from pipeline.stage import set_broadcast, init_stage_db

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
    path = request.url.path
    if not path.startswith(("/api/", "/proxy/", "/static/", "/ws", "/docs")):
        response.headers["Content-Security-Policy"] = CSP
    return response


# ── Startup ──────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    from pipeline.history import init_history_db
    init_history_db()
    init_stage_db()
    set_broadcast(broadcast)
    logger.info("FrictionDeck v%s started on port %d", VERSION, PORT)


# ── WebSocket broadcast ──────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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
    from pipeline.stage import get_version
    from pipeline.history import verify_chain
    from pipeline.broadcast import subscriber_count
    chain = verify_chain(limit=10)
    return {
        "status": "ok", "version": VERSION,
        "stage_version": get_version(),
        "chain_valid": chain["valid"],
        "chain_events": chain["total_events"],
        "ws_subscribers": subscriber_count(),
    }


@app.get("/api/stages")
async def api_stages():
    from pipeline.stage import list_stages
    return list_stages()


@app.get("/api/{stage_name}/stage")
async def api_stage(stage_name: str):
    from pipeline.stage import get_stage_state
    return get_stage_state(stage_name)


@app.post("/api/csp/add")
async def api_add_csp_domain(category: str, domain: str):
    from pipeline.gui_adapter import add_csp_domain
    return add_csp_domain(category, domain)


# ── Proxy layer ──────────────────────────────────────────────────────────

@app.api_route("/proxy/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(service: str, path: str, request: Request):
    from pipeline.history import log_event
    from pipeline.constants import EventType

    base_url = PROXY_WHITELIST.get(service)
    if not base_url:
        return Response(
            content=f'{{"error":"service not whitelisted: {service}"}}',
            status_code=403, media_type="application/json",
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
            method=request.method, url=target,
            headers=headers, content=body if body else None,
        )

    log_event(
        EventType.PROXY_FORWARDED, actor="iframe", pathway="proxy",
        payload={"service": service, "path": f"/{path}",
                 "method": request.method, "status_code": resp.status_code},
    )

    return Response(
        content=resp.content, status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ── Static files ─────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/{stage_name}")
async def stage_view(stage_name: str):
    from pipeline.stage import init_stage_db
    from pipeline.history import init_history_db
    init_stage_db(stage_name)
    init_history_db(stage_name)
    return FileResponse("static/index.html")


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
