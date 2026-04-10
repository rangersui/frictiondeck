"""elastik MCP bridge -- one bridge, everything behind it is hot-swappable.

   http(target)   -> multi-target elastik instances (endpoints.json, hot-plug)
   mcp_call()     -> external MCP servers (mcp_servers.json, hot-plug)

   Bridge never restarts. Edit a JSON, next call picks it up.
"""
import json, os, sys, asyncio
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path

TOKEN = os.getenv("ELASTIK_TOKEN", "")
_CONF_DIR = Path(__file__).with_name("conf")
CONFIG = _CONF_DIR / "mcp_servers.json"
ENDPOINTS = _CONF_DIR / "endpoints.json"

# -- HTTP endpoints hot-plug (same pattern as MCP hot-plug) --

_endpoints = {}        # name -> URL
_endpoints_mtime = 0   # last mtime of endpoints.json
_DEFAULT_BASE = os.getenv("ELASTIK_URL", "http://localhost:3005")

def _reload_endpoints():
    """Re-read endpoints.json if changed on disk. Fallback to env var."""
    global _endpoints_mtime
    if not ENDPOINTS.exists():
        if "default" not in _endpoints:
            _endpoints["default"] = _DEFAULT_BASE
        return
    try:
        mt = ENDPOINTS.stat().st_mtime
        if mt == _endpoints_mtime:
            return
        _endpoints_mtime = mt
        data = json.loads(ENDPOINTS.read_text())
        _endpoints.clear()
        _endpoints.update(data)
        # ensure default always exists
        if "default" not in _endpoints:
            _endpoints["default"] = _DEFAULT_BASE
    except (json.JSONDecodeError, OSError):
        pass


def _do_http(method, path, body="", headers="", target="default", timeout=30):
    """Core HTTP logic -- shared by both official and mini MCP."""
    _reload_endpoints()
    if target == "__list__":
        return json.dumps(_endpoints, indent=2)
    if target not in _endpoints:
        return json.dumps({"error": f"target '{target}' not in endpoints.json. available: {list(_endpoints.keys())}"})
    base = _endpoints[target]
    # guard: path must be a path, not a URL or authority injection
    if not path.startswith("/"):
        path = "/" + path
    if "@" in path or path.startswith("//") or "\\" in path or "\r" in path or "\n" in path or "\0" in path:
        return json.dumps({"error": "invalid path"})
    _ALLOWED_HEADERS = {"content-type", "accept", "user-agent"}
    h = {}
    if headers:
        for k, v in json.loads(headers).items():
            if k.lower() in _ALLOWED_HEADERS:
                h[k] = v
    if TOKEN:
        h["X-Auth-Token"] = TOKEN  # always last -- AI cannot override
    data = body.encode("utf-8") if body else None
    req = Request(base + path, data=data, headers=h, method=method)
    try:
        resp = urlopen(req, timeout=timeout)
        return json.dumps({"status": resp.status, "target": target, "base": base, "body": resp.read().decode()})
    except HTTPError as e:
        return json.dumps({"status": e.code, "target": target, "base": base, "body": e.read().decode()})
    except URLError as e:
        return json.dumps({"status": 0, "target": target, "base": base, "body": str(e.reason)})


# -- MCP aggregator -- per-call connection to configured servers --

_configs = {}  # name -> server spec from json
_config_mtime = 0  # last mtime of mcp_servers.json

def _reload_config():
    """Re-read mcp_servers.json if changed on disk."""
    global _config_mtime
    if not CONFIG.exists():
        return
    try:
        mt = CONFIG.stat().st_mtime
        if mt == _config_mtime:
            return
        _config_mtime = mt
        cfg = json.loads(CONFIG.read_text())
        _configs.clear()
        for name, spec in cfg.get("servers", {}).items():
            _configs[name] = spec
    except (json.JSONDecodeError, OSError):
        pass


async def _call_server(server_name, tool_name, arguments):
    """Shared logic: connect to MCP server, call tool, return result."""
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession
    _reload_config()
    if server_name not in _configs:
        return json.dumps({"error": f"server '{server_name}' not in mcp_servers.json. available: {list(_configs.keys())}"})
    spec = _configs[server_name]
    cmd = spec["command"]
    if sys.platform == "win32":
        import shutil
        resolved = shutil.which(cmd)
        if resolved:
            cmd = resolved
    params = StdioServerParameters(
        command=cmd,
        args=spec.get("args", []),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **spec.get("env", {})}
    )
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                if tool_name == "__list__":
                    tools = await session.list_tools()
                    return json.dumps([{"name": t.name, "description": t.description} for t in tools.tools])
                result = await session.call_tool(tool_name, arguments)
                texts = [c.text for c in result.content if hasattr(c, 'text')]
                return "\n".join(texts) if texts else str(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


# -- Entry point: try official mcp, fallback to mini --

try:
    from mcp.server.fastmcp import FastMCP
    _USE_OFFICIAL = True
except ImportError:
    _USE_OFFICIAL = False


def _run_official():
    """Full MCP mode -- official library."""
    mcp = FastMCP("elastik")

    @mcp.tool()
    async def http(method: str, path: str, body: str = "", headers: str = "",
                   target: str = "default", timeout: int = 30) -> str:
        """elastik HTTP interface -- hot-pluggable multi-target.

        FIRST ACTION: call GET /info to discover all routes.

        Targets are configured in endpoints.json. Hot-pluggable: edit the file,
        next call picks it up -- zero restart. Use target="__list__" to see
        all available endpoints.

        method: GET or POST
        path: e.g. /default/read, /default/write, /stages
        body: request body (for POST)
        headers: JSON string of headers (optional)
        target: endpoint name from endpoints.json (default: "default")
                use "__list__" to list all configured endpoints
        timeout: request timeout in seconds (default 30)
        """
        return await asyncio.to_thread(_do_http, method, path, body, headers, target, timeout)

    @mcp.tool()
    async def mcp_call(server: str, tool_name: str, arguments: str = "{}") -> str:
        """MCP aggregator -- one tool to call ANY external MCP server.

        This is a universal gateway. All MCP servers in mcp_servers.json are
        reachable through this single tool. Hot-pluggable: add or remove a
        server in the JSON file and it takes effect on the next call -- zero restart.

        Use tool_name="__list__" to discover available tools on a server.

        server: server name from mcp_servers.json (e.g. 'email', 'fs')
        tool_name: the remote tool name, or "__list__" to list all tools
        arguments: JSON string of arguments for the tool
        """
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        return await _call_server(server, tool_name, args)

    def _load_config():
        _reload_config()
        for name, spec in _configs.items():
            desc = spec.get("description", f"Proxy to {name} MCP server")
            @mcp.tool(name=name, description=desc)
            async def proxy(tool_name: str, arguments: str = "{}", server_ref=name) -> str:
                """Call a tool on this MCP server."""
                args = json.loads(arguments) if isinstance(arguments, str) else arguments
                return await _call_server(server_ref, tool_name, args)
            print(f"  mcp: {name}", file=sys.stderr)

    _load_config()
    _reload_endpoints()
    print(f"\n  elastik MCP aggregator (official)", file=sys.stderr)
    print(f"  http targets:", file=sys.stderr)
    for name, url in _endpoints.items():
        print(f"    {name} -> {url}", file=sys.stderr)
    print(file=sys.stderr)
    mcp.run(transport="stdio")


def _run_mini():
    """Mini MCP mode -- zero dependency, stdio JSON-RPC."""
    from mini_mcp import serve

    tools = [
        {
            "name": "http",
            "description": "elastik HTTP interface -- hot-pluggable multi-target. "
                "FIRST ACTION: call GET /info to discover all routes. "
                "Use target='__list__' to see all endpoints.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "description": "GET or POST"},
                    "path": {"type": "string", "description": "e.g. /default/read, /stages"},
                    "body": {"type": "string", "description": "request body (POST)", "default": ""},
                    "headers": {"type": "string", "description": "JSON headers (optional)", "default": ""},
                    "target": {"type": "string", "description": "endpoint name from endpoints.json", "default": "default"},
                    "timeout": {"type": "integer", "description": "timeout in seconds", "default": 30},
                },
                "required": ["method", "path"],
            },
        }
    ]

    def tool_handler(name, args):
        if name == "http":
            return _do_http(
                method=args.get("method", "GET"),
                path=args.get("path", "/"),
                body=args.get("body", ""),
                headers=args.get("headers", ""),
                target=args.get("target", "default"),
                timeout=args.get("timeout", 30),
            )
        raise ValueError(f"unknown tool: {name}")

    _reload_endpoints()
    print(f"\n  elastik MCP bridge (mini -- zero dependency)", file=sys.stderr)
    print(f"  http targets:", file=sys.stderr)
    for name, url in _endpoints.items():
        print(f"    {name} -> {url}", file=sys.stderr)
    print(f"  note: mcp_call unavailable (install mcp for full aggregator)", file=sys.stderr)
    print(file=sys.stderr)
    serve(tools, tool_handler)


def _run_http():
    """HTTP mode -- pastebin disguise + knock/bearer gated MCP.

    Runs a second HTTP port alongside (or instead of) stdio. To the outside
    world this is a plain pastebin. IPs that complete the knock sequence (or
    requests bearing the right bearer token) get POST /mcp routed to the
    real MCP handler. Everything else is pastebin.

    server.py stays untouched. This process talks to server.py via _do_http
    (localhost-only if you configure it so).

    Two scenarios, two mechanisms, no overlap:

      1. Direct connection (Tailscale, LAN, your own phone hitting tunnel)
         -> knock sequence -> IP whitelist (TTL)
         -> your IP = request IP, makes sense.

      2. Proxied connection (Claude.ai -> Anthropic server -> your MCP)
         -> request IP is Anthropic's egress (160.79.104.0/21)
         -> knock is useless (your phone's IP is never seen)
         -> Anthropic IP + bearer token -> allow
         -> bearer is scoped: leaked token is useless unless attacker
            is also on Anthropic's egress network.

    Env:
      ELASTIK_MCP_PORT            listen port (default 3006)
      ELASTIK_MCP_BIND            bind addr (default 127.0.0.1; set 0.0.0.0 for public)
      ELASTIK_MCP_TOKEN           URL query secret (/mcp?k=TOKEN) for proxy scenario
      ELASTIK_ANTHROPIC_IPS       CIDRs that bypass direct-client check. Default:
                                    160.79.104.0/21,2607:6bc0::/48
                                  (see https://platform.claude.com/docs/en/api/ip-addresses)
      ELASTIK_KNOCK               comma-separated knock paths (direct scenario)
                                  (each path must be >=12 chars, not '/')
      ELASTIK_KNOCK_WINDOW        seconds to complete knock (default 10)
      ELASTIK_KNOCK_TTL           whitelist TTL seconds (default 600)
      ELASTIK_TRUST_PROXY_HEADER  header to read real IP from (e.g. cf-connecting-ip)
      ELASTIK_TRUST_PROXY_FROM    required if TRUST_PROXY_HEADER set: CIDR(s)
                                  of upstream proxies allowed to set that header
    """
    import http.server
    import socketserver
    import secrets as _secrets
    import time as _time
    import hmac as _hmac
    import ipaddress as _ipaddress
    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
    from collections import OrderedDict
    from mini_mcp import handle_message

    PORT = int(os.getenv("ELASTIK_MCP_PORT", "3006"))
    BIND = os.getenv("ELASTIK_MCP_BIND", "127.0.0.1")
    MCP_TOKEN = os.getenv("ELASTIK_MCP_TOKEN", "")
    KNOCK = [p.strip() for p in os.getenv("ELASTIK_KNOCK", "").split(",") if p.strip()]
    KNOCK_WINDOW = int(os.getenv("ELASTIK_KNOCK_WINDOW", "10"))
    KNOCK_TTL = int(os.getenv("ELASTIK_KNOCK_TTL", "600"))
    TRUST_HEADER = os.getenv("ELASTIK_TRUST_PROXY_HEADER", "").lower()

    def _parse_cidrs(env_name, raw):
        nets = []
        for cidr in raw.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                nets.append(_ipaddress.ip_network(cidr, strict=False))
            except ValueError as e:
                print(f"  bad CIDR in {env_name}: {cidr}: {e}", file=sys.stderr)
                sys.exit(1)
        return nets

    # Anthropic outbound egress, from https://platform.claude.com/docs/en/api/ip-addresses
    ANTHROPIC_DEFAULT = "160.79.104.0/21,2607:6bc0::/48"
    ANTHROPIC_NETS = _parse_cidrs("ELASTIK_ANTHROPIC_IPS",
                                  os.getenv("ELASTIK_ANTHROPIC_IPS", ANTHROPIC_DEFAULT))

    # Upstream proxies allowed to set TRUST_HEADER. Empty = disable header.
    TRUST_FROM_NETS = _parse_cidrs("ELASTIK_TRUST_PROXY_FROM",
                                   os.getenv("ELASTIK_TRUST_PROXY_FROM", ""))

    if TRUST_HEADER and not TRUST_FROM_NETS:
        print(f"  refusing to start: ELASTIK_TRUST_PROXY_HEADER={TRUST_HEADER!r} "
              f"set but ELASTIK_TRUST_PROXY_FROM is empty. Forged headers would "
              f"bypass IP auth. Set TRUST_PROXY_FROM to your upstream CIDR.", file=sys.stderr)
        sys.exit(1)

    # Knock path entropy check: reject short or root paths.
    # 12 chars min means >=11 payload chars after the leading slash.
    for kp in KNOCK:
        if kp == "/" or not kp.startswith("/") or len(kp) < 12:
            print(f"  refusing: knock path too short or invalid: {kp!r} "
                  f"(must start with '/', length >= 12, not '/')", file=sys.stderr)
            sys.exit(1)

    # Refuse to start with no auth mechanism at all.
    if not KNOCK and not (MCP_TOKEN and ANTHROPIC_NETS):
        print("  refusing to start http mode: need ELASTIK_KNOCK, or (ELASTIK_MCP_TOKEN + ELASTIK_ANTHROPIC_IPS)", file=sys.stderr)
        sys.exit(1)

    PASTE_MAX = 16
    PASTE_SIZE = 4096
    MAX_BODY = 1024 * 1024  # 1MB cap for MCP requests
    SLIDE_EXTEND = 120      # seconds added on each active call
    _pastes = OrderedDict()  # key -> bytes
    _knock = {}              # ip -> (step_idx, last_ts)
    _whitelist = {}          # ip -> expiry_ts

    def real_ip(handler):
        socket_ip = handler.client_address[0]
        # Only trust proxy header if request comes from an approved upstream.
        if TRUST_HEADER and TRUST_FROM_NETS:
            try:
                addr = _ipaddress.ip_address(socket_ip)
            except ValueError:
                return socket_ip
            if any(addr in n for n in TRUST_FROM_NETS):
                v = handler.headers.get(TRUST_HEADER, "")
                if v:
                    return v.split(",")[0].strip()
        return socket_ip

    def gc_knock():
        if len(_knock) < 1024:
            return
        cutoff = _time.time() - KNOCK_WINDOW
        for k in list(_knock.keys()):
            if _knock[k][1] < cutoff:
                _knock.pop(k, None)

    def advance_knock(ip, path):
        if not KNOCK:
            return
        gc_knock()
        now = _time.time()
        idx, ts = _knock.get(ip, (0, 0))
        if now - ts > KNOCK_WINDOW:
            idx = 0
        if idx < len(KNOCK) and path == KNOCK[idx]:
            idx += 1
            if idx == len(KNOCK):
                _whitelist[ip] = now + KNOCK_TTL
                _knock.pop(ip, None)
                print(f"  knock ok: {ip}", file=sys.stderr)
                return
            _knock[ip] = (idx, now)
        else:
            _knock.pop(ip, None)

    def is_whitelisted(ip):
        exp = _whitelist.get(ip)
        if not exp:
            return False
        if _time.time() > exp:
            _whitelist.pop(ip, None)
            return False
        return True

    def extend_whitelist(ip):
        new_exp = _time.time() + SLIDE_EXTEND
        cur = _whitelist.get(ip, 0)
        if new_exp > cur:
            _whitelist[ip] = new_exp

    def ip_in_anthropic(ip):
        if not ANTHROPIC_NETS:
            return False
        try:
            addr = _ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in ANTHROPIC_NETS)

    def url_secret_ok(query):
        """Check ?k=TOKEN in query string. Claude.ai cannot send custom
        headers to remote MCP servers, so the secret rides in the URL."""
        if not MCP_TOKEN:
            return False
        k = query.get("k", [""])[0]
        return _hmac.compare_digest(k, MCP_TOKEN)

    def paste_store(body):
        if len(body) > PASTE_SIZE:
            body = body[:PASTE_SIZE]
        key = _secrets.token_urlsafe(6)[:6]
        _pastes[key] = body
        while len(_pastes) > PASTE_MAX:
            _pastes.popitem(last=False)
        return key

    def paste_get(key):
        return _pastes.get(key)

    _TOOLS = [{
        "name": "http",
        "description": "elastik HTTP interface -- hot-pluggable multi-target. "
            "FIRST ACTION: call GET /info to discover all routes. "
            "Use target='__list__' to see all endpoints.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "GET or POST"},
                "path": {"type": "string", "description": "e.g. /default/read, /stages"},
                "body": {"type": "string", "description": "request body (POST)", "default": ""},
                "headers": {"type": "string", "description": "JSON headers (optional)", "default": ""},
                "target": {"type": "string", "description": "endpoint name", "default": "default"},
                "timeout": {"type": "integer", "description": "timeout seconds", "default": 30},
            },
            "required": ["method", "path"],
        },
    }]

    def _tool_handler(name, args):
        if name == "http":
            return _do_http(
                method=args.get("method", "GET"),
                path=args.get("path", "/"),
                body=args.get("body", ""),
                headers=args.get("headers", ""),
                target=args.get("target", "default"),
                timeout=args.get("timeout", 30),
            )
        raise ValueError(f"unknown tool: {name}")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silent -- pastebin does not log

        def version_string(self):
            return "pastebin"

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, BrokenPipeError):
                pass

        def _read_body(self):
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return b""
            if n > MAX_BODY:
                n = MAX_BODY
            return self.rfile.read(n)

        def _parse_uri(self):
            """Split request URI into (path, query_dict). self.path contains
            the full URI including any query string."""
            parsed = _urlparse(self.path)
            return parsed.path, _parse_qs(parsed.query)

        def _pastebin_get(self, path_only):
            if path_only in ("/", ""):
                return self._send(200, "pastebin\nPOST to create, GET /<key> to fetch.\n")
            key = path_only.lstrip("/")
            data = paste_get(key)
            if data is None:
                return self._send(404, "not found\n")
            return self._send(200, data)

        def _pastebin_post(self, body):
            key = paste_store(body)
            self._send(200, key + "\n")

        def do_GET(self):
            ip = real_ip(self)
            path_only, _q = self._parse_uri()
            advance_knock(ip, path_only)
            self._pastebin_get(path_only)

        def do_HEAD(self):
            # HEAD must not include a body per RFC 7231.
            ip = real_ip(self)
            path_only, _q = self._parse_uri()
            advance_knock(ip, path_only)
            # Send headers only, zero body.
            try:
                self.send_response(200 if (path_only == "/" or paste_get(path_only.lstrip("/")) is not None) else 404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
            except (ConnectionError, BrokenPipeError):
                pass

        def do_POST(self):
            ip = real_ip(self)
            path_only, query = self._parse_uri()
            body = self._read_body()

            # Two authorized paths:
            #   (a) knocked IP (direct connection scenario)
            #   (b) Anthropic egress IP + valid URL secret (proxy scenario)
            authorized = False
            if is_whitelisted(ip):
                authorized = True
            elif ip_in_anthropic(ip) and url_secret_ok(query):
                authorized = True

            if authorized and path_only == "/mcp":
                if is_whitelisted(ip):
                    extend_whitelist(ip)
                try:
                    text = body.decode("utf-8", "replace")
                    resp = handle_message(text, _TOOLS, _tool_handler)
                except Exception as e:
                    print(f"  mcp handler error: {e}", file=sys.stderr)
                    return self._send(500, "error\n")
                if resp is None:
                    return self._send(202, "")
                return self._send(200, resp, "application/json")

            # Everything else: pastebin
            self._pastebin_post(body)

        def do_DELETE(self):
            self._send(405, "method not allowed\n")

        def do_PUT(self):
            self._send(405, "method not allowed\n")

    class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    _reload_endpoints()
    srv = ThreadedServer((BIND, PORT), H)
    print(f"\n  elastik MCP bridge (http)", file=sys.stderr)
    print(f"  listen:    {BIND}:{PORT}", file=sys.stderr)
    print(f"  knock:     {'on ('+str(len(KNOCK))+' steps, '+str(KNOCK_TTL)+'s ttl)' if KNOCK else 'off'}", file=sys.stderr)
    print(f"  url key:   {'on (/mcp?k=<token>)' if MCP_TOKEN else 'off'}", file=sys.stderr)
    print(f"  anthropic: {', '.join(str(n) for n in ANTHROPIC_NETS) if ANTHROPIC_NETS else 'off'}", file=sys.stderr)
    if TRUST_HEADER:
        print(f"  trust:     {TRUST_HEADER} from {', '.join(str(n) for n in TRUST_FROM_NETS)}", file=sys.stderr)
    else:
        print(f"  trust:     (socket only)", file=sys.stderr)
    print(f"  http targets:", file=sys.stderr)
    for name, url in _endpoints.items():
        print(f"    {name} -> {url}", file=sys.stderr)
    print(file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if "--http" in sys.argv:
        _run_http()
    elif _USE_OFFICIAL:
        _run_official()
    else:
        _run_mini()
