"""MCP JSON-RPC endpoint. Legacy — AI should just curl directly.

POST /mcp with JSON-RPC body. Auth via header or ?k=<token> (URL auth opt-in).
"""
import json, asyncio
import server
from mini_mcp import handle_message
from mcp_server import _MINI_TOOLS, _mini_tool_handler

DESCRIPTION = "/mcp — MCP JSON-RPC (legacy)"
AUTH = "none"  # we check auth ourselves — need allow_url=True

async def handle_mcp(method, body, params):
    if method != "POST":
        return {"error": "method not allowed", "_status": 405}
    scope = params.get("_scope", {})
    _url = getattr(server, '_check_url_auth', lambda s: None)
    if not (server._check_auth(scope) or _url(scope)):
        return {"error": "unauthorized", "_status": 403}
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        resp = await asyncio.to_thread(handle_message, text, _MINI_TOOLS, _mini_tool_handler)
    except Exception as e:
        return {"error": str(e), "_status": 500}
    if resp is None:
        return {"_status": 202}
    return json.loads(resp)

ROUTES = {"/mcp": handle_mcp}
