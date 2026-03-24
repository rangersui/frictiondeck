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

| Column | Type | Description |
|---|---|---|
| stage_html | TEXT | Main string. Browser renders this. |
| pending_js | TEXT | Command string. Browser evals this. |
| js_result | TEXT | Reply string. Browser writes this back. |
| version | INTEGER | Increments on each write/append. |
| updated_at | TEXT | Last modification timestamp. |

### events

| Column | Type | Description |
|---|---|---|
| timestamp | TEXT | When it happened. |
| event_type | TEXT | What happened. |
| payload | TEXT | Details (JSON string). |
| hmac | TEXT | HMAC-SHA256(prev_hmac + payload). |
| prev_hmac | TEXT | Previous event's HMAC. Chain link. |

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

## Authentication

All POST routes require `X-Auth-Token` header except:
- `/{name}/sync` — browser writes back DOM state
- `/{name}/result` — browser writes back JS execution result  
- `/{name}/clear` — browser clears pending after consumption

These three are exempt because the browser cannot access the token.
The token is printed in terminal at startup.

GET routes are public. No token needed to read.

Set `ELASTIK_PUBLIC=true` environment variable to skip all auth checks.

## Request limits

- Request body capped at 5MB. Exceeding returns 413.
- World names must match `^[a-zA-Z0-9][a-zA-Z0-9_-]*$`. Invalid names return 400.

## JSON body compatibility

POST routes accept both raw strings and JSON.
If the body starts with `{`, the server attempts to parse it as JSON
and extracts the value from `body`, `content`, or `text` field.

This allows tool-calling AI platforms (e.g. GPT Actions) 
that can only send JSON to write strings without server modifications.

curl sends raw string → works.
MCP sends raw string → works.
GPT Actions sends {"content": "hello"} → extracts "hello" → works.

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

- iframe sandbox: allow-scripts allow-same-origin allow-popups
- CSP: connect-src 'self' — browser can only fetch localhost
- X-Auth-Token: all POST routes authenticated (except sync/result/clear)
- Approve token: printed in terminal, required for plugin approval
- HMAC chain: immutable audit history
- Body limit: 5MB max
- World names: alphanumeric, dash, underscore only
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
