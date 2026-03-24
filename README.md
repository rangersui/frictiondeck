# elastik

A protocol for human-AI interaction.

This repo is a reference implementation in Python.

---

## Protocol

Five rules. Any language can implement them.

1. **Listen on a port.** Accept HTTP requests.
2. **Send and receive strings over HTTP.** Nothing else. No types. No schemas.
3. **Store strings in SQLite.** One file per world: `universe.db`.
4. **Sign strings with HMAC.** Chain-linked. Append-only. Immutable history.
5. **Render strings in a browser.** One iframe. One polling loop.

See PROTOCOL.md for the formal specification.

---

## Install

pip install -r requirements.txt

python server.py

Or with Docker:

docker compose up

Open `http://localhost:3004`. Empty.
Say something to your AI.

## Configuration

Create a `.env` file in the project root:

```
ELASTIK_TOKEN=your-secret-token-here
```

This token is used for:

- Authenticating all POST requests (X-Auth-Token header)
- Plugin approval (X-Approve-Token header)
- MCP server auto-injection

If not set, a random token is generated on each restart.

Docker reads from `.env` automatically. For MCP, add to Claude Desktop config:

```json
"env": {
  "ELASTIK_TOKEN": "same-token-as-env-file"
}
```

Never commit `.env` to git.

---

## What happens

You see an empty wall. Your AI writes a string. The browser renders it. You see something.

You type on the wall. The string syncs back. Your AI reads it. Your AI writes a new string. The wall changes.

That's it. Everything else is emergent.

---

## Three mailboxes

Each world has three string fields:

| Field       | Who writes     | Who reads       | What happens       |
| ----------- | -------------- | --------------- | ------------------ |
| `stage`   | AI writes      | Browser renders | You see pixels     |
| `pending` | AI writes      | Browser evals   | Code executes      |
| `result`  | Browser writes | AI reads        | AI sees the answer |

Plus a version counter and an HMAC audit chain.

---

## HTTP endpoints

```
GET  /{name}/read      → read all three mailboxes + version
POST /{name}/write     → overwrite stage string → version++
POST /{name}/append    → append to stage string → version++
POST /{name}/sync      → overwrite stage string → no version bump
POST /{name}/pending   → write to command mailbox
POST /{name}/result    → write to reply mailbox
POST /{name}/clear     → clear pending + result
GET  /stages           → list all worlds
GET  /{name}           → serve the browser client
POST /webhook/{source} → log external event
POST /plugins/propose  → propose a new route
POST /plugins/approve  → approve (needs token from terminal)
```

---

## Multi-world

Every path is a world. Visit a path that doesn't exist — it's created.

```
localhost:3004/work     → work world
localhost:3004/project    → project world
localhost:3004/home     → personal world
```

Each world has its own `universe.db`. Independent. Parallel.

```bash
python lucy.py create myworld
python lucy.py stages
```

---

## Proof

AI was testing elastik. The test viewer had a bug.
AI said: "I have Stage. Why not use it?"
AI rendered its own test results on the wall.

Nobody told it to. It chose to.

**elastik's first spontaneous user was the AI itself.**

### A/B tested

| Scenario         | With Skill    | Without       | Gap                      |
| ---------------- | ------------- | ------------- | ------------------------ |
| Unit Converter   | 4/4           | 3/4           | Style conventions        |
| Multi-Stage      | 4/4           | 2/4           | Didn't know worlds exist |
| Realtime Notepad | 4/4           | 4/4           | Tie                      |
| Plugin Proposal  | 4/4           | 3/4           | Couldn't execute         |
| Motor Comparison | 4/4           | 4/4           | Tie                      |
| **Total**  | **90%** | **75%** | **+15%**           |

---

## Security

All physical. None semantic.

**Layer 1 — iframe sandbox** (frontend)
AI paints inside a sandboxed frame. `connect-src 'self'`.
Worst case: refresh the page.

**Layer 2 — Docker container** (backend)
Server runs inside a container. AI can't touch the host.
Worst case: `docker restart`.

**Layer 3 — auth token** (API)
All POST routes require `X-Auth-Token`. Token printed in terminal.
AI through MCP uses the token without seeing it.
AI can open the door but doesn't know the key.

**Layer 4 — HMAC chain** (audit)
Every action logged. Chain-linked. Immutable.
Tamper with one record, the entire chain breaks.

**Layer 5 — git merge** (evolution)
AI edits code in dev container. Commits. Pushes.
You review the diff. You merge. Or you don't.
Worst case: `git revert`.

**Layer 6 — client filtering** (extensions)
Browser extension: domain blacklist — banking and login sites excluded.
VS Code extension: `.elastikignore` — sensitive files never synced.
Terminal output scrubbed — lines with passwords/tokens stripped.
Opt-in required. Remote server warning on non-localhost.

The LLM is an untrusted HTTP client.
The same security principle that protects web servers from malicious browsers.
30 years old. Still works.

AI proposes. Human approves.
If elastik destroys the world, a human handed over the key.

### Server hardening

All POST routes require **`X-Auth-Token`** header (printed at startup)

GET routes are public (read-only)
Request body capped at 5MB
World names restricted to **`[a-zA-Z0-9_-]`** with no path traversal
Set **`ELASTIK_PUBLIC=true`** to skip auth (local dev only)

**Approve token** — printed in terminal. AI doesn't have it.

**HMAC chain** — every action logged, immutable.

The LLM itself is an untrusted HTTP client.

The same secruity principle that protects web servers from malicious broswers.

The safety rule the web has followed for 30 years.

LLM is just another client.

NO new AI safety methods needed.

AI proposes. Human approves. If elastik destroys the world,

a human handed over the key.

## Plugins

Routes are capabilities. More routes, more capabilities.

```bash
python lucy.py install fs        # file system access
python lucy.py install example   # hello world
python lucy.py list              # what's installed
python lucy.py remove fs         # revoke
```

AI can propose new plugins at runtime:

```
POST /plugins/propose   body: {name, code, description}
POST /plugins/approve   header: X-Approve-Token: {token}
```

Approve token is in the terminal. AI can't see it. Physics, not policy.

## Self-Evolution

~200 lines. Zero frameworks. AI reads the entire codebase in one context window.

```bash

python lucy.py evolve   # start dev container (port 3005)

python lucy.py enter    # step inside

python lucy.py deploy   # deploy to production

python lucy.py logs     # watch


In the dev container, AI edits server.py, runs pytest, commits.

You review the diff. You merge. New version goes live.

Docker is the training ground. Production is deployment.

git merge is the only approve button.
```

## Files

```
server.py          ~100 lines    the protocol
index.html         ~15 lines     one iframe, one polling loop
mcp_server.py      ~20 lines     MCP-to-HTTP adapter (temporary)
lucy.py            ~100 lines    CLI
PROTOCOL.md                      formal spec
SKILLS.md                        AI behavior guide
plugins/                         route extensions
data/                            universes
```

~235 lines of code. One dependency: `uvicorn`.

---

## Connect AI

Any MCP-compatible client:
```json
{
  "mcpServers": {
    "elastik": {
      "command": "python",
      "args": ["path/to/mcp_server.py"],
      "env": {
        "ELASTIK_TOKEN": "your-token"
      }
    }
  }
}
```

The MCP server has one tool: `http(method, path, body, headers)`.
It translates MCP calls to HTTP requests.

It also serves as a security layer: the auth token is injected
from an environment variable. AI uses the key without seeing it.

Change `ELASTIK_URL` — AI connects to a different machine.
No restart. No reconfiguration. No awareness.
AI doesn't know where it is. It just writes strings.
The pipe decides where the water flows.
```
localhost:3004          → your machine
100.x.x.x:3004         → another machine via Tailscale
your.domain.com:3004   → your server in the cloud
```

One tool. Any machine. Any universe.
When AI can send HTTP directly, MCP stays —
not as a translator, but as a token isolator.

---

## Roadmap

- [ ] Read authentication — optional token for GET routes (public deploy hardening)
- [ ] CORS configuration — restrict allowed origins
- [ ] Rate limiting — per-IP request throttling
- [ ] HTTPS — native TLS or reverse proxy guide
- [ ] Token rotation — `lucy rotate-token` command
- [ ] Access logging — IP, timestamp, route, status code

## Philosophy

```
TCP/IP 1974 — machines talk to machines.
HTTP 1991 — clients talk to servers.
Bitcoin 2008 — money without banks.
elastik 2026 — intelligence and tools belong to you.
```

We didn't invent anything. HTTP was already there. SQLite was already there. HMAC was already there. iframe was already there. AI was already there.

We just removed everything else.

---

## Name

**elastik** — the system takes whatever shape you need.

**lucy** — the CLI. Named after our ancestor. One finger. Everything starts.

**universe.db** — space and time in one file.

---

## Ecosystem
```
elastik              → protocol + server (~200 lines)
elastik-extension    → Lucy in every browser tab
elastik-vscode       → Lucy in every editor tab
```

- [elastik-extension](https://github.com/rangersui/elastik-extension) — Chrome extension, DOM sync, domain blacklist
- [elastik-vscode](https://github.com/rangersui/elastik-vscode) — VS Code extension, editor context sync, .elastikignore

*Copyright © 2026 Ranger Chen . MIT License.*
