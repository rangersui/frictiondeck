"""Auth plugin — Authorization header middleware.

Bearer APPROVE_TOKEN → full access (html + admin + shell)
Bearer AUTH_TOKEN    → basic access (read + write plain)
Basic *:token        → same logic on password
"""
import os
import server

DESCRIPTION = "Token-based auth middleware"
ROUTES = {}

async def auth_middleware(scope, receive, send, path, method):
    # Read + discovery always open
    if method in ("GET", "HEAD", "OPTIONS", "PROPFIND"): return None
    parts = [p for p in path.split("/") if p]
    # Admin + config + postman = approve required
    if path.startswith("/admin/") or path.startswith("/proxy") or \
       (len(parts) >= 1 and parts[0].startswith("config-") and method == "POST"):
        if server._check_auth(scope) == "approve": return None
        return await _deny(send)
    # Browser routes — sync/result/clear
    if len(parts) == 2 and parts[1] in ("sync", "result", "clear"): return None
    # Signal worlds — ephemeral WebRTC signaling
    if len(parts) >= 1 and parts[0].startswith("signal-"): return None
    # Auth plugin routes
    if path.startswith("/auth/"): return None
    # Plugin approve
    if path == "/plugins/approve": return None
    # WebDAV — dav plugin handles auth inline
    if path.startswith("/dav"): return None
    # Everything else — any auth level passes
    token = os.getenv("ELASTIK_TOKEN", "")
    if not token: return None  # no token = open
    if server._check_auth(scope) is not None: return None
    return await _deny(send)

async def _deny(send):
    body = b'{"error":"unauthorized"}'
    await send({"type": "http.response.start", "status": 403, "headers": [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()]]})
    await send({"type": "http.response.body", "body": body})
    return True

AUTH_MIDDLEWARE = auth_middleware
