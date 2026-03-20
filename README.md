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

Three layers. All physical. None semantic.

**Layer 1 — iframe sandbox** (frontend)

AI paints freely inside. `connect-src 'self'` — can only fetch localhost.

Worst case: refresh the page.

**Layer 2 — Docker container** (backend)

Server runs inside a container. AI can't touch the host.

Worst case: `docker restart`.

**Layer 3 — git merge** (evolution)

AI edits code in dev container. Commits. Pushes.

You review the diff. You merge. Or you don't.

Worst case: `git revert`.

**Approve token** — printed in terminal. AI doesn't have it.

**HMAC chain** — every action logged, immutable.

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
      "args": ["path/to/mcp_server.py"]
    }
  }
}
```

The MCP server has one tool: `http(method, path, body)`. It translates MCP calls to HTTP requests. When AI can send HTTP directly, this file disappears.

---

## Philosophy

```
TCP/IP  1974  machine ↔ machine
HTTP    1991  client ↔ server
elastik 2026  human ↔ AI
```

We didn't invent anything. HTTP was already there. SQLite was already there. HMAC was already there. iframe was already there. AI was already there.

We just removed everything else.

---

## Name

**elastik** — the system takes whatever shape you need.

**lucy** — the CLI. Named after our ancestor. One finger. Everything starts.

**universe.db** — space and time in one file.

---

*Copyright © 2026 Ranger Chen . MIT License.*
