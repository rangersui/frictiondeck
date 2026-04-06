# elastik protocol

## Five rules

1. **Listen.** Accept connections.
2. **Send and receive strings.** Nothing else. No types. No schemas.
3. **Store strings in SQLite.** One file per world: `universe.db`.
4. **Sign strings with HMAC.** Chain-linked. Append-only. Immutable history.
5. **Render strings in a browser.** One reactive loop.

The reference implementation uses HTTP. The protocol is transport-agnostic.
HTTP, WebRTC, WebSocket, stdin/stdout, NFC, sound — if it carries strings,
it works. Flow (1, 2, 5) · store (3) · verify (4). Miss one and it isn't
elastik.

Any implementation that follows these five rules is elastik-compatible.
This repo is a reference implementation in Python (~300 lines).

## Database schema

Each world has one `universe.db` with two tables:

### stage_meta

| Column     | Type    | Description                             |
| ---------- | ------- | --------------------------------------- |
| stage_html | TEXT    | Main string. Browser renders this.      |
| pending_js | TEXT    | Command string. Browser evals this.     |
| js_result  | TEXT    | Reply string. Browser writes this back. |
| version    | INTEGER | Increments on each write/append.        |
| updated_at | TEXT    | Last modification timestamp.            |

### events

| Column     | Type | Description                        |
| ---------- | ---- | ---------------------------------- |
| timestamp  | TEXT | When it happened.                  |
| event_type | TEXT | What happened.                     |
| payload    | TEXT | Details (JSON string).             |
| hmac       | TEXT | HMAC-SHA256(prev_hmac + payload).  |
| prev_hmac  | TEXT | Previous event's HMAC. Chain link. |

## HTTP endpoints (reference implementation)

HTTP is one transport. These are the routes the Python reference exposes.
Other transports map the same operations to their own framing.

### String operations

```
GET  /{name}/read      → returns {stage_html, pending_js, js_result, version}
POST /{name}/write     → overwrites stage_html → version++
POST /{name}/append    → appends to stage_html → version++
POST /{name}/sync      → overwrites stage_html → no version bump
POST /{name}/pending   → writes to pending_js
POST /{name}/result    → writes to js_result
POST /{name}/clear     → clears pending_js + js_result
```

### Infrastructure

```
GET  /stages           → lists all worlds [{name, version, updated_at}]
GET  /{name}           → returns index.html (browser entry point)
GET  /                 → returns index.html (stage list)
POST /webhook/{source} → logs event
POST /plugins/propose  → logs plugin proposal event
POST /plugins/approve  → writes plugin file + registers route (requires token)
```

## Authentication

Authentication is not part of the protocol. It is a plugin.

Without auth plugin: all routes are open. Pure protocol.
With `plugins/auth.py`: POST routes require `X-Auth-Token` header.

- GET always open.
- `sync`, `result`, `clear` exempt (browser needs these).

The approve token is the only hardcoded security.
It protects plugin installation. It is printed in the terminal.
AI cannot approve its own proposals.

## Hot Plug

Plugins can be loaded and unloaded at runtime without restarting the server.

`load_plugin(name)` — loads a plugin from `plugins/{name}.py`.
If not found, copies from `plugins/available/{name}.py` first.
`unload_plugin(name)` — removes all routes registered by that plugin.
File stays on disk. Next restart will reload it.

The `/admin/*` routes are protected by the approve token,
not the auth token. This is a constitutional boundary:

- Auth token (`X-Auth-Token`): daily operations. AI has this.
- Approve token (`X-Approve-Token`): system changes. Only human has this.

Loading a plugin grants new capabilities to the system.
Granting capabilities is a constitutional act.
Constitutional acts require the approve token.

## World name format

World names match `[a-zA-Z0-9][a-zA-Z0-9_-]*`. This is the canonical
regex. All implementations share it. It appears in:

- Server-side route validation (reject invalid names with 400)
- Renderer dispatch (`<!--use:{name}-->` pattern in the browser client)
- Navigation guard (postMessage bridge only allows paths matching this pattern)
- CLI commands (`lucy create {name}`)

No dots. No slashes. No spaces. No unicode. Just ASCII alphanumeric,
dash, underscore. First character must be alphanumeric.

## Request limits

- Request body capped at 5MB. Exceeding returns 413.
- World names validated per above. Invalid names return 400.

## JSON body compatibility

POST routes accept both raw strings and JSON.
If the body starts with `{`, the server attempts to parse it as JSON
and extracts the value from `body`, `content`, or `text` field.

This allows tool-calling AI platforms (e.g. GPT Actions) 
that can only send JSON to write strings without server modifications.

curl sends raw string → works.
MCP sends raw string → works.
GPT Actions sends {"content": "hello"} → extracts "hello" → works.

## Three mailboxes

A world has three string fields. Three mailboxes.

```
stage_html   →  main mailbox      →  browser renders it
pending_js   →  command mailbox   →  browser evals it
js_result    →  reply mailbox     →  browser writes back
```

AI writes to mailboxes. Browser reads from mailboxes.
Human types in browser. Sync writes back to mailbox. AI reads.

The protocol doesn't care what's in the strings.
The browser interprets them. That's the browser's job.

## Audit chain

Every write, append, sync, plugin proposal, and plugin approval
is logged in the events table with an HMAC signature.

```
hmac = HMAC-SHA256(key, prev_hmac + payload)
```

Each event links to the previous. The chain is append-only.
Tampering with any event breaks the chain.

## Security

- iframe sandbox: allow-scripts allow-popups (no allow-same-origin — null origin)
- postMessage bus: iframe communicates with parent via __elastik helper
- CSP: connect-src 'self' — browser can only fetch localhost
- X-Auth-Token: all POST routes authenticated (except sync/result/clear)
- Approve token: printed in terminal, required for plugin approval and hot plug
- HMAC chain: immutable audit history
- Body limit: 5MB max
- Navigation guard: postMessage navigate only allows paths matching world name format (no external URLs, no protocol-relative URLs)
- Cross-world write guard: postMessage write/append only allowed to the current world — iframe cannot write to other worlds
- Fetch guard: postMessage fetch only allowed to current world paths + /stages
- World names: canonical format (see "World name format" section above)
- Three mailboxes are independent: writing pending does not clear result

Six layers of physical isolation:
1. iframe sandbox — frontend containment
2. Docker container — backend containment  
3. Auth token — write permission control
4. HMAC chain — tamper-evident audit
5. git merge — evolution gating
6. Client filtering — sensitive content exclusion

AI cannot approve its own proposals. The token exists only in the terminal.
This is not a rule. It is physics.

## Deployment modes

Three modes. Three tradeoffs. You choose.

```
localhost      → sovereignty   → zero third-party trust → one device
Tailscale      → freedom       → trust WireGuard        → your devices
Cloudflare     → exposure      → trust Cloudflare       → the world
```

Each step outward trusts one more layer. Each step outward exposes one more layer.

- `localhost`: data never leaves your network card. Your router, ISP, and cloud providers don't know elastik exists.
- `Tailscale`: data is encrypted end-to-end via WireGuard. Only your devices can reach each other. No central server sees the content.
- `Cloudflare Tunnel`: data is encrypted in transit but Cloudflare terminates TLS. Cloudflare can see the content. The world can reach your server (with auth).

This is not a security flaw. It is a tradeoff.
The protocol doesn't choose for you. You choose for yourself.

## Plugins

A plugin is a Python file in `plugins/` that exports a `ROUTES` dict.
Each route maps a path to an async handler.

```python
ROUTES = {}
async def handle(method, body, params):
    return {"hello": "world"}
ROUTES["/greeting"] = handle
```

Plugins are loaded at startup. New plugins require approval.

## Worlds

Every path is a world. `data/{name}/universe.db`.

Write to a path that doesn't exist → auto-created. Empty. Ready.
Read a world that doesn't exist → 404. Reading never creates.

```
localhost:3004/work     → work world
localhost:3004/home     → home world
localhost:3004/anything → anything world
```

`lucy create {name}` also works.

## Implementations

The protocol is language- and transport-agnostic. Any runtime that can:

- Accept connections (any transport)
- Carry strings
- Read/write SQLite (or any append-only store)
- Compute HMAC-SHA256

...can implement elastik.

```
elastik (this repo)    →  Python + uvicorn    (reference)
go/                    →  Go (core + native)  (in-tree, zero-dep)
```

## Self-describing API (reference implementation)

`GET /info` returns:
- `routes` — all registered plugin routes
- `auth` — which auth plugin is active
- `plugins` — name, description, routes, params schema for each
- `skills` — SKILLS.md content

AI calls `GET /info` once. Knows all capabilities. No guessing.

## MCP Aggregator (reference implementation)

`mcp_server.py` is the single bridge between AI and elastik.
Two hot-pluggable config files, both checked on every call:

- `endpoints.json` — HTTP targets. `http(target="slim")` hits a remote elastik.
  One AI, one bridge, N elastik instances. Edit the file, next call picks it up.
- `mcp_servers.json` — external MCP servers. `mcp_call(server="email", ...)`
  proxies to any stdio MCP server. Add/remove servers, next call picks it up.

Bridge never restarts. Everything behind it is hot-swappable.

## That's it

Five rules. Three mailboxes. One integer. One hash chain.

Five rules describe concepts, not technologies.
Any implementation that receives, transports, stores, signs, and
renders strings is elastik-compatible.
