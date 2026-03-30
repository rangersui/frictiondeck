"""elastik MCP bridge — one bridge, everything behind it is hot-swappable.

   http(target)   → multi-target elastik instances (endpoints.json, hot-plug)
   mcp_call()     → external MCP servers (mcp_servers.json, hot-plug)

   Bridge never restarts. Edit a JSON, next call picks it up.
"""
import json, os, sys, asyncio
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("elastik")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
CONFIG = Path(__file__).with_name("mcp_servers.json")
ENDPOINTS = Path(__file__).with_name("endpoints.json")

# ── HTTP endpoints hot-plug (same pattern as MCP hot-plug) ────────────────

_endpoints = {}        # name → URL
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


@mcp.tool()
async def http(method: str, path: str, body: str = "", headers: str = "",
               target: str = "default", timeout: int = 30) -> str:
    """elastik HTTP interface — hot-pluggable multi-target.

    FIRST ACTION: call GET /info to discover all routes.

    Targets are configured in endpoints.json. Hot-pluggable: edit the file,
    next call picks it up — zero restart. Use target="__list__" to see
    all available endpoints.

    method: GET or POST
    path: e.g. /default/read, /default/write, /stages
    body: request body (for POST)
    headers: JSON string of headers (optional)
    target: endpoint name from endpoints.json (default: "default")
            use "__list__" to list all configured endpoints
    timeout: request timeout in seconds (default 30)
    """
    _reload_endpoints()
    if target == "__list__":
        return json.dumps(_endpoints, indent=2)
    if target not in _endpoints:
        return json.dumps({"error": f"target '{target}' not in endpoints.json. available: {list(_endpoints.keys())}"})
    base = _endpoints[target]
    h = {}
    if headers:
        h.update(json.loads(headers))
    if TOKEN:
        h["X-Auth-Token"] = TOKEN  # always last — AI cannot override
    def _do_request():
        data = body.encode("utf-8") if body else None
        req = Request(base + path, data=data, headers=h, method=method)
        try:
            resp = urlopen(req, timeout=timeout)
            return json.dumps({"status": resp.status, "target": target, "base": base, "body": resp.read().decode()})
        except HTTPError as e:
            return json.dumps({"status": e.code, "target": target, "base": base, "body": e.read().decode()})
        except URLError as e:
            return json.dumps({"status": 0, "target": target, "base": base, "body": str(e.reason)})
    return await asyncio.to_thread(_do_request)


# ── MCP aggregator — per-call connection to configured servers ────────────

_configs = {}  # name → server spec from json
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

def _load_config():
    """Read mcp_servers.json. Register one proxy tool per server."""
    _reload_config()
    for name, spec in _configs.items():
        desc = spec.get("description", f"Proxy to {name} MCP server")
        _register_server_proxy(name, desc)
        print(f"  mcp: {name}", file=sys.stderr)


def _register_server_proxy(name, description):
    """Register one tool per server. Delegates to shared _call_server."""
    @mcp.tool(name=name, description=description)
    async def proxy(tool_name: str, arguments: str = "{}") -> str:
        """Call a tool on this MCP server.

        tool_name: the remote tool name (e.g. list_directory, read_file)
        arguments: JSON string of arguments for the tool
        """
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        return await _call_server(name, tool_name, args)


async def _call_server(server_name: str, tool_name: str, arguments: dict) -> str:
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


@mcp.tool()
async def mcp_call(server: str, tool_name: str, arguments: str = "{}") -> str:
    """MCP aggregator — one tool to call ANY external MCP server.

    This is a universal gateway. All MCP servers in mcp_servers.json are
    reachable through this single tool. Hot-pluggable: add or remove a
    server in the JSON file and it takes effect on the next call — zero restart.

    Use tool_name="__list__" to discover available tools on a server.

    server: server name from mcp_servers.json (e.g. 'email', 'fs')
    tool_name: the remote tool name, or "__list__" to list all tools
    arguments: JSON string of arguments for the tool
    """
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    return await _call_server(server, tool_name, args)


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    _load_config()
    _reload_endpoints()
    print(f"\n  elastik MCP aggregator", file=sys.stderr)
    print(f"  http targets:", file=sys.stderr)
    for name, url in _endpoints.items():
        print(f"    {name} → {url}", file=sys.stderr)
    for name in _configs:
        print(f"  mcp: {name}", file=sys.stderr)
    print(file=sys.stderr)
    mcp.run(transport="stdio")
