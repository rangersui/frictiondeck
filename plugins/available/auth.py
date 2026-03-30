"""Auth plugin — X-Auth-Token middleware. Install to enable authentication."""
import os

DESCRIPTION = "Token-based auth middleware"
ROUTES = {}

async def auth_middleware(scope, path, method):
    # GET always open
    if method == "GET": return True
    # Browser routes — no token available
    parts = [p for p in path.split("/") if p]
    if len(parts) == 2 and parts[1] in ("sync", "result", "clear"): return True
    # Auth plugin routes must be open
    if path.startswith("/auth/"): return True
    # Plugin approve has its own token — let server.py handle it
    if path == "/plugins/approve": return True
    # Admin + config worlds = modify system = approve token required
    if path.startswith("/admin/") or (path.startswith("/config-") and method == "POST"):
        headers = dict(scope.get("headers", []))
        tok = headers.get(b"x-approve-token", b"").decode()
        approve = os.getenv("ELASTIK_APPROVE_TOKEN", "")
        import hmac as _hmac
        return _hmac.compare_digest(tok, approve) if approve else True

    # Everything else — check X-Auth-Token
    token = os.getenv("ELASTIK_TOKEN", "")
    if not token: return True  # no token set = public mode
    headers = dict(scope.get("headers", []))
    tok = headers.get(b"x-auth-token", b"").decode()
    import hmac as _hmac
    return _hmac.compare_digest(tok, token)

AUTH_MIDDLEWARE = auth_middleware
