"""Public access gate — HTTP-native, header-only.

Unauthorized (non-localhost) → 401 + WWW-Authenticate: Basic realm="pastebin".
Authorization: Bearer <token> / Basic <user>:<pwd> / cap token → pass through.

No session cookies. No URL tokens here. Credentials ride every request.
Browsers cache Basic auth for the tab session; that's the browser's job,
not ours. `/gate` + the static HMAC cookie were removed in favour of
this because:
  - the cookie was a deterministic replay token with no server-side
    expiry (Codex P1, 2026-04-20)
  - collapsing a header auth into ambient cookie authority on every
    subsequent same-origin request contradicts the "physics, not
    policy" stance (Codex P2)

App shell resources that browsers fetch *anonymously* (manifest.json,
sw.js, opensearch.xml, favicon, icons) are allowed through even
without auth: they're static, hard-coded in server.py, carry zero
world content, and gating them breaks PWA + Service Worker
registration under any public-exposure deployment (Cloudflare tunnel,
nginx, etc.).
"""
import os, ipaddress as _ipa
import server

DESCRIPTION = "Public access gate (header-only, no cookies)"
NEEDS = []

APPROVE = os.getenv("ELASTIK_APPROVE_TOKEN", "")
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

# ── Gate middleware ────────────────────────────────────────────────

# App shell resources browsers fetch anonymously (no cookie, no basic
# auth) by default. Gating them breaks PWA install, SW registration,
# and the browser tab icon under any public-exposure deployment. All
# static, hard-coded in server.py (MANIFEST / SW / ICON / ICON192),
# zero world content.
_PUBLIC_SHELL = {
    "/manifest.json", "/sw.js", "/opensearch.xml",
    "/favicon.ico", "/icon.png", "/icon-192.png",
}

async def auth_gate(scope, receive, send, path, method):
    if not APPROVE: return None
    if path in _PUBLIC_SHELL: return None
    ip = _real_ip(scope)
    if ip.startswith("127.") or ip == "::1": return None
    if server._check_auth(scope): return None
    # 401 — browser shows Basic Auth dialog, cancel shows pastebin text
    body = b"pastebin\nPOST to create, GET /<key> to fetch.\n"
    await send({"type": "http.response.start", "status": 401, "headers": [
        [b"www-authenticate", b'Basic realm="pastebin"'],
        [b"content-type", b"text/plain; charset=utf-8"],
        [b"content-length", str(len(body)).encode()],
        [b"server", b"pastebin"]]})
    await send({"type": "http.response.body", "body": body})
    return True

if APPROVE:
    AUTH_MIDDLEWARE = auth_gate
    import sys; print("  public_gate: active (header-only, no cookies)", file=sys.stderr)
