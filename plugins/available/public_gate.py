"""Public access gate — pastebin disguise + MCP endpoint.

When ELASTIK_MCP_TOKEN is set, this plugin intercepts ALL requests:
  - Authorized → proceed to normal server.py routing
  - Unauthorized → pastebin disguise (real ephemeral pastebin)
  - POST /mcp (authorized) → MCP JSON-RPC handler

When ELASTIK_MCP_TOKEN is not set, the plugin is inert — auth.py stays active.

Auth paths (any one is sufficient):
  1. Session cookie — from /gate?k=<token> login (browser)
  2. URL secret ?k= + Anthropic egress IP (Claude.ai remote MCP)
  3. URL secret ?k= + knock-whitelisted IP (direct MCP clients)
  4. X-Auth-Token header (internal _do_http self-calls, local CLI)

/gate requires token only (no knock/IP gate). Cookie is server-side
(random nonce + IP binding), expires after ELASTIK_SESSION_TTL.
"""
import json, os, secrets as _secrets, time as _time, hmac as _hmac
import ipaddress as _ipaddress
from urllib.parse import parse_qs as _parse_qs
from collections import OrderedDict
from mini_mcp import handle_message
from mcp_server import _do_http, _MINI_TOOLS, _mini_tool_handler

DESCRIPTION = "Public access gate: knock + bearer + pastebin + MCP"
NEEDS = []

# ── Config ────────────────────────────────────────────────────────

MCP_TOKEN = os.getenv("ELASTIK_MCP_TOKEN", "")
KNOCK = [p.strip().rstrip("/") for p in os.getenv("ELASTIK_KNOCK", "").split(",") if p.strip()]
KNOCK_WINDOW = int(os.getenv("ELASTIK_KNOCK_WINDOW", "10"))
KNOCK_TTL = int(os.getenv("ELASTIK_KNOCK_TTL", "600"))
SESSION_TTL = int(os.getenv("ELASTIK_SESSION_TTL", "86400"))  # cookie lifetime, default 24h
TRUST_HEADER = os.getenv("ELASTIK_TRUST_PROXY_HEADER", "").lower()
SLIDE_EXTEND = 120

def _parse_cidrs(raw):
    nets = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if not cidr: continue
        try: nets.append(_ipaddress.ip_network(cidr, strict=False))
        except ValueError: pass
    return nets

ANTHROPIC_NETS = _parse_cidrs(os.getenv("ELASTIK_ANTHROPIC_IPS",
                              "160.79.104.0/21,2607:6bc0::/48"))
TRUST_FROM_NETS = _parse_cidrs(os.getenv("ELASTIK_TRUST_PROXY_FROM", ""))

# Startup validation
if MCP_TOKEN:
    if TRUST_HEADER and not TRUST_FROM_NETS:
        import sys
        print(f"  public_gate: refusing — TRUST_PROXY_HEADER={TRUST_HEADER!r} "
              f"set but TRUST_PROXY_FROM is empty.", file=sys.stderr)
        raise SystemExit(1)
    for kp in KNOCK:
        if kp == "/" or not kp.startswith("/") or len(kp) < 12:
            import sys
            print(f"  public_gate: refusing — knock path too short: {kp!r}", file=sys.stderr)
            raise SystemExit(1)

# ── State ─────────────────────────────────────────────────────────

PASTE_MAX = 256
PASTE_SIZE = 4096
_pastes = OrderedDict()
_knock_state = {}      # ip -> (step_idx, last_ts)
_whitelist = {}        # ip -> expiry_ts
_sessions = {}         # session_id -> {"ip": str, "exp": float}

# ── Helpers ───────────────────────────────────────────────────────

def _real_ip(scope):
    client = scope.get("client")
    socket_ip = client[0] if client else "127.0.0.1"
    if TRUST_HEADER and TRUST_FROM_NETS:
        try:
            addr = _ipaddress.ip_address(socket_ip)
        except ValueError:
            return socket_ip
        if any(addr in n for n in TRUST_FROM_NETS):
            headers = dict(scope.get("headers", []))
            v = headers.get(TRUST_HEADER.encode(), b"").decode()
            if v:
                return v.split(",")[0].strip()
    return socket_ip


def _ip_in_anthropic(ip):
    if not ANTHROPIC_NETS: return False
    try: addr = _ipaddress.ip_address(ip)
    except ValueError: return False
    return any(addr in n for n in ANTHROPIC_NETS)


def _gc_knock():
    if len(_knock_state) < 1024: return
    cutoff = _time.time() - KNOCK_WINDOW
    for k in list(_knock_state):
        if _knock_state[k][1] < cutoff:
            _knock_state.pop(k, None)


def _gc_whitelist():
    if len(_whitelist) < 1024: return
    now = _time.time()
    for k in list(_whitelist):
        if _whitelist[k] < now:
            _whitelist.pop(k, None)


def _advance_knock(ip, path):
    if not KNOCK: return
    if _ip_in_anthropic(ip): return
    _gc_knock()
    now = _time.time()
    idx, ts = _knock_state.get(ip, (0, 0))
    if now - ts > KNOCK_WINDOW: idx = 0
    if idx < len(KNOCK) and path == KNOCK[idx]:
        idx += 1
        if idx == len(KNOCK):
            _whitelist[ip] = now + KNOCK_TTL
            _knock_state.pop(ip, None)
            print(f"  knock ok: {ip}")
            return
        _knock_state[ip] = (idx, now)
    else:
        _knock_state.pop(ip, None)


def _is_whitelisted(ip):
    _gc_whitelist()
    exp = _whitelist.get(ip)
    if not exp: return False
    if _time.time() > exp:
        _whitelist.pop(ip, None)
        return False
    return True


def _extend_whitelist(ip):
    new_exp = _time.time() + SLIDE_EXTEND
    cur = _whitelist.get(ip, 0)
    if new_exp > cur: _whitelist[ip] = new_exp


def _url_secret_ok(query):
    if not MCP_TOKEN: return False
    k = query.get("k", [""])[0]
    return _hmac.compare_digest(k, MCP_TOKEN)


# ── Session (server-side store, nonce per login) ─────────────────

def _gc_sessions():
    if len(_sessions) < 1024: return
    now = _time.time()
    for k in list(_sessions):
        if _sessions[k]["exp"] < now:
            _sessions.pop(k, None)


def _create_session(ip):
    _gc_sessions()
    nonce = _secrets.token_urlsafe(16)
    sid = _hmac.new(MCP_TOKEN.encode(), (ip + nonce).encode(), "sha256").hexdigest()[:32]
    _sessions[sid] = {"ip": ip, "exp": _time.time() + SESSION_TTL}
    return sid


def _check_internal_token(scope):
    """Check X-Auth-Token header. Used by internal _do_http self-calls
    and local CLI tools. Same check that auth.py used to do."""
    token = os.getenv("ELASTIK_TOKEN", "")
    if not token: return False
    headers = dict(scope.get("headers", []))
    tok = headers.get(b"x-auth-token", b"").decode()
    if not tok: return False
    return _hmac.compare_digest(tok, token)


def _check_session(scope, ip):
    if not MCP_TOKEN: return False
    headers = dict(scope.get("headers", []))
    cookie = headers.get(b"cookie", b"").decode()
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("elastik_s="):
            sid = part[10:]
            s = _sessions.get(sid)
            if not s: return False
            if s["ip"] != ip: return False
            if _time.time() > s["exp"]:
                _sessions.pop(sid, None)
                return False
            return True
    return False


def _session_cookie_header(ip):
    sid = _create_session(ip)
    return f"elastik_s={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"


# ── Pastebin ──────────────────────────────────────────────────────

def _paste_store(body):
    if isinstance(body, str): body = body.encode()
    if len(body) > PASTE_SIZE: body = body[:PASTE_SIZE]
    key = _secrets.token_urlsafe(6)[:6]
    _pastes[key] = body
    while len(_pastes) > PASTE_MAX:
        _pastes.popitem(last=False)
    return key


def _paste_get(key):
    v = _pastes.get(key)
    if v is not None:
        _pastes.move_to_end(key)  # true LRU, not FIFO
    return v


# ── ASGI send helpers ─────────────────────────────────────────────

async def _send_plain(send, status, body, extra_headers=None, head_only=False):
    if isinstance(body, str): body = body.encode()
    h = [[b"content-type", b"text/plain; charset=utf-8"],
         [b"content-length", str(len(body)).encode()],
         [b"server", b"pastebin"]]
    if extra_headers: h.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": h})
    await send({"type": "http.response.body", "body": b"" if head_only else body})


async def _recv_body(receive, max_bytes=1024*1024):
    """Read request body up to max_bytes. Must only be called once per request."""
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if len(body) > max_bytes:
            body = body[:max_bytes]
            break
        if not msg.get("more_body"): break
    return body


# ── Auth middleware ────────────────────────────────────────────────

async def auth_gate(scope, receive, send, path, method):
    """Intercepts all requests when MCP_TOKEN is configured.
    Authorized → return None (proceed to server.py).
    Unauthorized → serve pastebin, return True (intercepted).
    """
    if not MCP_TOKEN:
        return None  # No public gate, pass through

    # /gate is the login endpoint — exempt from gate, handler does own check
    if path == "/gate":
        return None

    ip = _real_ip(scope)
    qs = scope.get("query_string", b"").decode()
    query = _parse_qs(qs)

    # Advance knock on every GET/HEAD (always, before auth check)
    if method in ("GET", "HEAD"):
        _advance_knock(ip, path)

    # ── Check authorization (three paths) ──
    has_cookie = _check_session(scope, ip)
    has_url_secret = _url_secret_ok(query) and (
        _is_whitelisted(ip) or _ip_in_anthropic(ip))
    has_internal = _check_internal_token(scope)

    if has_cookie or has_url_secret or has_internal:
        # Extend whitelist if knock-path client
        if _is_whitelisted(ip):
            _extend_whitelist(ip)

        # Browser GET with ?k= → redirect to strip token, set cookie
        if method == "GET" and "k" in query and not has_cookie:
            headers = dict(scope.get("headers", []))
            accept = headers.get(b"accept", b"").decode()
            if "text/html" in accept:
                # Build clean URL without ?k=
                from urllib.parse import urlencode as _urlencode
                other = {k: v[0] for k, v in query.items() if k != "k"}
                clean = path
                if other:
                    clean += "?" + _urlencode(other)
                cookie = _session_cookie_header(ip)
                await send({"type": "http.response.start", "status": 302, "headers": [
                    [b"location", clean.encode()],
                    [b"set-cookie", cookie.encode()],
                    [b"server", b"pastebin"]]})
                await send({"type": "http.response.body", "body": b""})
                return True  # Intercepted (redirect)

        return None  # Authorized, proceed to server.py

    # ── Unauthorized → pastebin disguise ──
    is_head = method == "HEAD"
    if method in ("GET", "HEAD"):
        if path in ("/", ""):
            await _send_plain(send, 200, "pastebin\nPOST to create, GET /<key> to fetch.\n", head_only=is_head)
        else:
            key = path.lstrip("/")
            data = _paste_get(key)
            if data is None:
                await _send_plain(send, 404, "not found\n", head_only=is_head)
            else:
                await _send_plain(send, 200, data, head_only=is_head)
        return True

    if method == "POST":
        body = await _recv_body(receive, max_bytes=PASTE_SIZE)
        key = _paste_store(body)
        await _send_plain(send, 200, key + "\n")
        return True

    if method in ("DELETE", "PUT"):
        await _send_plain(send, 405, "method not allowed\n")
        return True

    return None  # Unknown method, let server.py handle


# ── MCP route ─────────────────────────────────────────────────────

async def handle_mcp(method, body, params):
    """JSON-RPC MCP endpoint. Auth already checked by auth_gate.
    Runs handle_message in a thread so _do_http self-calls don't
    deadlock the event loop."""
    if method != "POST":
        return {"error": "method not allowed", "_status": 405}
    try:
        import asyncio
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        resp = await asyncio.to_thread(handle_message, text, _MINI_TOOLS, _mini_tool_handler)
    except Exception as e:
        return {"error": str(e), "_status": 500}
    if resp is None:
        return {"_status": 202}
    return json.loads(resp)


# ── Gate route (explicit login) ───────────────────────────────────

async def handle_gate(method, body, params):
    """Visit /gate?k=<token> to get a session cookie and redirect to /.
    Only requires the URL token — no knock or IP gate needed.
    The cookie is tied to the client's IP via HMAC, so a stolen cookie
    from a different IP won't pass _check_session."""
    scope = params.get("_scope", {})
    ip = _real_ip(scope)
    qs = scope.get("query_string", b"").decode()
    query = _parse_qs(qs)

    if not _url_secret_ok(query):
        return {"error": "invalid token", "_status": 403}

    cookie = _session_cookie_header(ip)
    return {"_redirect": "/", "_cookies": [cookie], "_status": 302}


# ── Route + middleware registration ───────────────────────────────

ROUTES = {"/mcp": handle_mcp, "/gate": handle_gate}

if MCP_TOKEN:
    AUTH_MIDDLEWARE = auth_gate
    import sys
    print(f"  public_gate: active", file=sys.stderr)
    print(f"    knock: {'on (' + str(len(KNOCK)) + ' steps)' if KNOCK else 'off'}", file=sys.stderr)
    print(f"    anthropic: {', '.join(str(n) for n in ANTHROPIC_NETS) if ANTHROPIC_NETS else 'off'}", file=sys.stderr)
    if TRUST_HEADER:
        print(f"    trust: {TRUST_HEADER} from {', '.join(str(n) for n in TRUST_FROM_NETS)}", file=sys.stderr)
