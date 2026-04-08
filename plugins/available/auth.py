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

async def auth_middleware(scope, path, method):
    # GET always open
    if method == "GET": return True
    parts = [p for p in path.split("/") if p]
    # Admin + config + postman = approve token required — checked FIRST, before any bypass
    if path.startswith("/admin/") or path.startswith("/proxy") or (len(parts) >= 1 and parts[0].startswith("config-") and method == "POST"):
        approve = os.getenv("ELASTIK_APPROVE_TOKEN", "")
        if not approve: return False  # no approve token = locked
        import hmac as _hmac
        return _hmac.compare_digest(_get_approve_token(scope), approve)
    # Browser routes — no token available (sync/result/clear for non-config worlds)
    if len(parts) == 2 and parts[1] in ("sync", "result", "clear"): return True
    # Signal worlds — ephemeral WebRTC signaling, no auth needed
    if len(parts) >= 1 and parts[0].startswith("signal-"): return True
    # Auth plugin routes must be open
    if path.startswith("/auth/"): return True
    # Plugin approve has its own token check inside handler
    if path == "/plugins/approve": return True

    # Everything else — check X-Auth-Token (approve token also passes — higher privilege)
    token = os.getenv("ELASTIK_TOKEN", "")
    if not token: return True  # no token set = public mode
    headers = dict(scope.get("headers", []))
    tok = headers.get(b"x-auth-token", b"").decode()
    import hmac as _hmac
    if _hmac.compare_digest(tok, token): return True
    approve = os.getenv("ELASTIK_APPROVE_TOKEN", "")
    if approve and _hmac.compare_digest(_get_approve_token(scope), approve): return True
    return False

AUTH_MIDDLEWARE = auth_middleware
