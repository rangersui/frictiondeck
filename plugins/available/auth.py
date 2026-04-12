"""Auth plugin — X-Auth-Token middleware. Mode-aware.

Mode 1 (executor):    auth token → read/write. admin/config blocked.
Mode 2 (autonomous):  approve token → admin/config unlocked.
"""
import base64, os

DESCRIPTION = "Token-based auth middleware"
ROUTES = {}

def _get_approve_token(scope):
    """Extract approve token from X-Approve-Token header or Basic Auth password."""
    headers = dict(scope.get("headers", []))
    tok = headers.get(b"x-approve-token", b"").decode()
    if tok: return tok
    auth = headers.get(b"authorization", b"").decode()
    if auth.startswith("Basic "):
        try:
            _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
            return pwd
        except Exception: pass
    return ""

async def auth_middleware(scope, receive, send, path, method):
    """New signature: (scope, receive, send, path, method).
    Return truthy = intercepted (sent own response). Falsy = proceed.
    """
    # Read + discovery methods always open. Plugin dispatch gates per-route.
    if method in ("GET", "HEAD", "OPTIONS", "PROPFIND"): return None
    parts = [p for p in path.split("/") if p]
    # Admin + config + postman = approve token required — checked FIRST, before any bypass
    if path.startswith("/admin/") or path.startswith("/proxy") or (len(parts) >= 1 and parts[0].startswith("config-") and method == "POST"):
        approve = os.getenv("ELASTIK_APPROVE_TOKEN", "")
        if not approve:
            return await _deny(send)
        import hmac as _hmac
        if _hmac.compare_digest(_get_approve_token(scope), approve):
            return None  # proceed
        return await _deny(send)
    # Browser routes — no token available (sync/result/clear for non-config worlds)
    if len(parts) == 2 and parts[1] in ("sync", "result", "clear"): return None
    # Signal worlds — ephemeral WebRTC signaling, no auth needed
    if len(parts) >= 1 and parts[0].startswith("signal-"): return None
    # Auth plugin routes must be open
    if path.startswith("/auth/"): return None
    # Plugin approve has its own token check inside handler
    if path == "/plugins/approve": return None
    # WebDAV uses Basic Auth, not X-Auth-Token. dav plugin handles auth inline.
    if path.startswith("/dav"): return None

    # Everything else — check X-Auth-Token (approve token also passes — higher privilege)
    token = os.getenv("ELASTIK_TOKEN", "")
    if not token: return None  # no token set = public mode
    headers = dict(scope.get("headers", []))
    tok = headers.get(b"x-auth-token", b"").decode()
    import hmac as _hmac
    if _hmac.compare_digest(tok, token): return None
    approve = os.getenv("ELASTIK_APPROVE_TOKEN", "")
    if approve and _hmac.compare_digest(_get_approve_token(scope), approve): return None
    return await _deny(send)


async def _deny(send):
    body = b'{"error":"unauthorized"}'
    await send({"type": "http.response.start", "status": 403, "headers": [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()]]})
    await send({"type": "http.response.body", "body": body})
    return True  # intercepted

AUTH_MIDDLEWARE = auth_middleware
