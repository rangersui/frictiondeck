"""URL token auth — ?k=<token> in query string.

Separate plugin because URL tokens leak to logs, referer, browser history.
Not installed = ?k= does nothing anywhere. Zero attack surface by default.

When installed, registers server._check_url_auth for other plugins to call.
Used by: /gate (QR/NFC), /mcp (MCP remote config).
"""
import hmac as _hmac, os
import server

DESCRIPTION = "URL token auth (?k=) for QR/NFC/MCP"
ROUTES = {}

_APPROVE = os.getenv("ELASTIK_APPROVE_TOKEN", "")
_TOKEN = os.getenv("ELASTIK_TOKEN", "")

def _check(scope):
    """?k=<token> → 'approve', 'auth', or None."""
    qs = scope.get("query_string", b"").decode()
    for p in qs.split("&"):
        if p.startswith("k="):
            tok = p[2:]
            if _APPROVE and _hmac.compare_digest(tok, _APPROVE): return "approve"
            if _TOKEN and _hmac.compare_digest(tok, _TOKEN): return "auth"
            return None
    return None

server._check_url_auth = _check
