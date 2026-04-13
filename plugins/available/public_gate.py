"""Public access gate — auth + pastebin disguise.

Unauthorized → 401 + WWW-Authenticate: Basic (browser pops dialog).
Authorized → pass through. /gate?k= for QR/NFC one-time entry.

Auth: Authorization header (Bearer or Basic) → server._check_auth().
Cookie: stateless HMAC from /gate?k= entry. No server-side sessions.
"""
import os, hmac as _hmac, ipaddress as _ipa
import server

DESCRIPTION = "Public access gate"
NEEDS = []

APPROVE = os.getenv("ELASTIK_APPROVE_TOKEN", "")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
TRUST_HEADER = os.getenv("ELASTIK_TRUST_PROXY_HEADER", "").lower()
TRUST_FROM = []
for _c in os.getenv("ELASTIK_TRUST_PROXY_FROM", "").split(","):
    _c = _c.strip()
    if _c:
        try: TRUST_FROM.append(_ipa.ip_network(_c, strict=False))
        except ValueError: pass

if APPROVE and TRUST_HEADER and not TRUST_FROM:
    import sys; print("  public_gate: refusing — TRUST_PROXY_HEADER set but TRUST_PROXY_FROM empty", file=sys.stderr); raise SystemExit(1)

def _real_ip(scope):
    ip = (scope.get("client") or ["127.0.0.1"])[0]
    if TRUST_HEADER and TRUST_FROM:
        try: addr = _ipa.ip_address(ip)
        except ValueError: return ip
        if any(addr in n for n in TRUST_FROM):
            v = dict(scope.get("headers", [])).get(TRUST_HEADER.encode(), b"").decode()
            if v: return v.split(",")[0].strip()
    return ip

def _cookie_sig():
    return _hmac.new((APPROVE or TOKEN).encode(), b"elastik-gate", "sha256").hexdigest()[:32]

def _has_cookie(scope):
    for part in dict(scope.get("headers", [])).get(b"cookie", b"").decode().split(";"):
        if part.strip().startswith("elastik="):
            return _hmac.compare_digest(part.strip()[8:], _cookie_sig())
    return False

# ── Gate middleware ────────────────────────────────────────────────

async def auth_gate(scope, receive, send, path, method):
    if not APPROVE: return None
    if path == "/gate": return None
    ip = _real_ip(scope)
    if ip.startswith("127.") or ip == "::1": return None
    if server._check_auth(scope) or _has_cookie(scope): return None
    # 401 — browser shows Basic Auth dialog, cancel shows pastebin text
    body = b"pastebin\nPOST to create, GET /<key> to fetch.\n"
    await send({"type": "http.response.start", "status": 401, "headers": [
        [b"www-authenticate", b'Basic realm="pastebin"'],
        [b"content-type", b"text/plain; charset=utf-8"],
        [b"content-length", str(len(body)).encode()],
        [b"server", b"pastebin"]]})
    await send({"type": "http.response.body", "body": body})
    return True

# ── /gate — QR/NFC one-time entry ────────────────────────────────

async def handle_gate(method, body, params):
    """Bearer/Basic/QR(?k=) → set cookie → redirect. URL auth opt-in here only."""
    scope = params.get("_scope", {})
    _url = getattr(server, '_check_url_auth', lambda s: None)
    if not (server._check_auth(scope) or _url(scope)):
        return {"error": "unauthorized", "_status": 403}
    c = f"elastik={_cookie_sig()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"
    return {"_redirect": "/", "_cookies": [c], "_status": 302}

ROUTES = {"/gate": handle_gate}

if APPROVE:
    AUTH_MIDDLEWARE = auth_gate
    import sys; print("  public_gate: active", file=sys.stderr)
