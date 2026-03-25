# elastik protocol

## Five rules

1. **Listen on a port.** Accept HTTP connections.
2. **Send and receive strings over HTTP.** No other protocol required.
3. **Store strings in SQLite.** One file per world: `universe.db`.
4. **Sign strings with HMAC.** Chain-linked. Append-only. Immutable history.
5. **Render strings in a browser.** An iframe. A polling loop. That's the UI.

Any implementation that follows these five rules is elastik-compatible.
This repo is a reference implementation in Python (~200 lines).

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

## HTTP endpoints

### String operations

```
GET  /{name}/read      → returns {stage_html, pending_js, js_result, version}
POST /{name}/write     → overwrites stage_html → version++
POST /{name}/append    → appends to stage_html → version++
POST /{name}/sync      → overwrites stage_html → no version bump
POST /{name}/pending   → writes to pending_js (clears js_result)
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

- **iframe sandbox**: `allow-scripts allow-same-origin allow-popups`
- **CSP**: `connect-src 'self'` — browser can only fetch localhost
- **Approve token**: random, printed in terminal at startup, required for plugin approval
- **HMAC chain**: immutable history

AI cannot approve its own proposals. The token exists only in the terminal.
This is not a rule. It is physics. The token is not in the iframe's universe.

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

Visit a path that doesn't exist → auto-created. Empty. Ready.

```
localhost:3004/work     → work world
localhost:3004/home     → home world
localhost:3004/anything → anything world
```

`lucy create {name}` also works.

## Implementations

The protocol is language-agnostic. Any language that can:

- Listen on a port
- Handle HTTP
- Read/write SQLite
- Compute HMAC-SHA256

...can implement elastik.

```
elastik (this repo)    →  Python + uvicorn
elastik-node           →  Node.js (future)
elastik-go             →  Go (future)
elastik-rust           →  Rust (future)
```

## That's it

Five rules. Three mailboxes. One integer. One hash chain.
A protocol for human-AI interaction.

Five rules describe concepts, not technologies.
Any implementation that:

- RECEIVES
- TRANSPORTS
- STORES
- SIGNS
- RENDERS STRINGS
  are elastik-compatible.
