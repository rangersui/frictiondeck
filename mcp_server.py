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


if __name__ == "__main__":
    if _USE_OFFICIAL:
        _run_official()
    else:
        _run_mini()
