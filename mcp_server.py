"""elastik MCP adapter. Translates MCP tool calls to HTTP. Temporary.
   After AI is able to send HTTP, it is deleted.
"""
import os, httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("elastik")
BASE = os.getenv("ELASTIK_URL", "http://localhost:3004")
TOKEN = os.getenv("ELASTIK_TOKEN", "")

@mcp.tool()
async def http(method: str, path: str, body: str = "", headers: str = "") -> str:
    """Send an HTTP request to the elastik server.

    method: GET or POST
    path: e.g. /default/read, /default/write, /stages
    body: request body (for POST)
    headers: JSON string of headers (optional), e.g. '{"X-Custom": "value"}'
    """
    import json
    h = {}
    if TOKEN:
        h["X-Auth-Token"] = TOKEN
    if headers:
        h.update(json.loads(headers))
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, BASE + path, content=body if body else None, headers=h)
        return r.text

if __name__ == "__main__":
    mcp.run(transport="stdio")
