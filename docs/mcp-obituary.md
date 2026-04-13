# MCP: An Obituary

Model Context Protocol. 2024-2025. Rest in peace.

## What happened

Anthropic created MCP: a JSON-RPC protocol so AI could call tools.
Then Anthropic gave Claude bash access.

```
Claude with MCP:   AI → JSON-RPC → /mcp → parse → _do_http → HTTP → /read → SQLite
Claude with curl:  AI → HTTP → /read → SQLite
```

Same result. Four fewer layers.

## The logic

```
Has MCP    → Claude      → has bash → doesn't need MCP
No MCP     → GPT et al.  → no bash  → can't use MCP either
```

MCP's target audience is zero.

The only AI with MCP servers is the one AI that doesn't need them.
The AIs that lack bash also lack MCP. They can't use either path.

Anthropic built a bridge, then gave wings to the only person
who needed to cross.

## The nesting problem

MCP is HTTP-over-JSON-RPC-over-HTTP. Protocol wrapping protocol
wrapping the same protocol. Same data, three layers of encoding.

```
HTTP(JSONRPC(HTTP)) → curl: just send HTTP directly
```

Skip the middle layer. Speak the native protocol directly.

## What MCP actually was

A translation layer for sandboxed environments where AI had no
system access. A shim between "AI wants to read a URL" and
"AI cannot open a socket."

The moment AI got a shell, every MCP server became a verbose
synonym for curl.

## What replaces it

Nothing. HTTP was always there. MCP didn't add a capability.
It added a translation. Remove the translation, the capability
remains.

```bash
# MCP "read" tool — 47 lines of JSON-RPC + handler + _do_http
curl https://host/sensors/read?k=TOKEN

# MCP "write" tool — 52 lines of JSON-RPC + handler + _do_http
curl -X POST https://host/sensors/write?k=TOKEN -d 'hello'

# MCP "list" tool — 38 lines of JSON-RPC + handler + _do_http
curl https://host/stages?k=TOKEN
```

## What stays (for now)

mcp_server.py and mini_mcp.py remain in the codebase.
The /mcp route in public_gate.py still works.
No one calls it. Dead code waiting for deletion.

stdio mode (mcp_server.py --official / --mini) still works for
Claude Desktop, which runs MCP over stdin/stdout because it has
no HTTP client. This is the last legitimate MCP use case, and
it will die the moment Claude Desktop gets a shell.

## Lesson

Don't build protocols for restrictions. Restrictions get lifted.
Build for the capability that was always underneath.

HTTP was always the capability. MCP was the restriction workaround.
The restriction is gone. So is the reason for MCP.

---

*elastik's MCP was implemented in v2.8, converted to a plugin in
v3.0, and made obsolete by Claude's bash access approximately
three weeks later.*
