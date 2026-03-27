"""elastik MCP aggregator. http() + proxy to configured MCP servers.
   mcp_servers.json configures external servers. Empty by default.
"""
import json, os, sys
import httpx
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("elastik")
BASE = os.getenv("ELASTIK_URL", "http://localhost:3005")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
CONFIG = Path(__file__).with_name("mcp_servers.json")

@mcp.tool()
async def http(method: str, path: str, body: str = "", headers: str = "", timeout: int = 30) -> str:
    """Send an HTTP request to the elastik server.

    IMPORTANT: On first use, call GET /info to discover all capabilities,
    plugins, worlds, renderers, and CDN whitelist. Then GET /stages for
    the full world list. Read the skills field in /info for usage guide.

    method: GET or POST
    path: e.g. /default/read, /default/write, /stages
    body: request body (for POST)
    headers: JSON string of headers (optional), e.g. '{"X-Custom": "value"}'
    timeout: request timeout in seconds (default 30)
    """
    h = {}
    if headers:
        h.update(json.loads(headers))
    if TOKEN:
        h["X-Auth-Token"] = TOKEN  # always last — AI cannot override
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, BASE + path, content=body if body else None, headers=h)
        return json.dumps({"status": r.status_code, "headers": dict(r.headers), "body": r.text})


# ── MCP aggregator — per-call connection to configured servers ────────────

_configs = {}  # name → server spec from json

def _load_config():
    """Read mcp_servers.json. Register one proxy tool per server."""
    if not CONFIG.exists():
        return
    try:
        cfg = json.loads(CONFIG.read_text())
    except (json.JSONDecodeError, OSError):
        return
    for name, spec in cfg.get("servers", {}).items():
        _configs[name] = spec
        desc = spec.get("description", f"Proxy to {name} MCP server")
        _register_server_proxy(name, desc)
        print(f"  mcp: {name}", file=sys.stderr)


def _register_server_proxy(name, description):
    """Register one tool per server. Each call starts fresh subprocess."""
    @mcp.tool(name=name, description=description)
    async def proxy(tool_name: str, arguments: str = "{}") -> str:
        """Call a tool on this MCP server.

        tool_name: the remote tool name (e.g. list_directory, read_file)
        arguments: JSON string of arguments for the tool
        """
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession
        spec = _configs[name]
        cmd = spec["command"]
        if sys.platform == "win32" and cmd == "npx":
            cmd = "npx.cmd"
        params = StdioServerParameters(
            command=cmd,
            args=spec.get("args", []),
            env={**os.environ, **spec.get("env", {})}
        )
        try:
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    args = json.loads(arguments) if isinstance(arguments, str) else arguments
                    result = await session.call_tool(tool_name, args)
                    texts = [c.text for c in result.content if hasattr(c, 'text')]
                    return "\n".join(texts) if texts else str(result)
        except Exception as e:
            return json.dumps({"error": str(e)})


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    _load_config()
    print(f"\n  elastik MCP aggregator", file=sys.stderr)
    print(f"  http()  → elastik ({BASE})", file=sys.stderr)
    for name in _configs:
        print(f"  {name}()  → {name}", file=sys.stderr)
    print(file=sys.stderr)
    mcp.run(transport="stdio")
