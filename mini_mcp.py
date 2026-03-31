"""Mini MCP server -- zero dependency fallback.

Implements MCP protocol (JSON-RPC over stdio) using pure stdlib.
Used when `mcp` package is not installed.
"""
import json, sys


def _response(id, result):
    return json.dumps({"jsonrpc": "2.0", "id": id, "result": result})


def _error(id, code, msg):
    return json.dumps({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}})


def handle_message(line, tools, tool_handler):
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return _error(None, -32700, "parse error")

    id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "initialize":
        return _response(id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "elastik", "version": "1.11.0"}
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _response(id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            result = tool_handler(name, args)
            return _response(id, {
                "content": [{"type": "text", "text": result}]
            })
        except Exception as e:
            return _error(id, -32000, str(e))

    return _error(id, -32601, f"unknown method: {method}")


def serve(tools, tool_handler):
    """Block on stdin, process JSON-RPC, write to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        resp = handle_message(line, tools, tool_handler)
        if resp:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()
