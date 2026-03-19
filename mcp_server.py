"""elastik MCP adapter. Translates MCP tool calls to HTTP. Temporary.
   After AI is able to send HTTP, it is deleted.
"""
import os, httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("elastik")
BASE = os.getenv("ELASTIK_URL", "http://localhost:3004")

@mcp.tool()
async def http(method: str, path: str, body: str = "") -> str:
    """Send an HTTP request to the elastik server.

    method: GET or POST
    path: e.g. /default/read, /default/write, /stages
    body: request body (for POST)
    """
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, BASE + path, content=body if body else None)
        return r.text

if __name__ == "__main__":
    mcp.run(transport="stdio")
