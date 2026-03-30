# Elastik

Anything-to-UI. A protocol that turns any string into interface.

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

**Windows** — double-click `install.cmd`

**macOS / Linux** — open terminal:
```bash
./install.sh
```

That's it. The installer does three things:
1. Installs Python dependencies
2. Configures Claude Desktop to connect to elastik
3. Starts the server

Restart Claude Desktop. Say something. The wall responds.

**Supported AI clients:**
- **Claude Desktop** — one-click, installer handles everything
- **Any MCP client** (Cursor, Windsurf, Claude Code, etc.) — point to `mcp_server.py`
- **ChatGPT** — via OpenAPI schema, requires public URL (GPT Actions call from OpenAI's servers)
- **Everything else** (Gemini, Ollama, your own agent) — just POST and GET, see [Integrate anything](#integrate-anything)

<details>
<summary>Manual install</summary>

```bash
pip install -r requirements.txt
python server.py
```

Or with Docker:

```bash
docker compose up
```

Open `http://localhost:3004`. Empty. Say something to your AI.

</details>

## Configuration

Create a `.env` file (copy from `.env.example`):

```
ELASTIK_TOKEN=your-auth-token
ELASTIK_APPROVE_TOKEN=your-approve-token
```

Two tokens, two purposes:

| Token | Header | Who has it | What it does |
|-------|--------|-----------|--------------|
| `ELASTIK_TOKEN` | `X-Auth-Token` | AI (via MCP env) | Read/write worlds, use plugins |
| `ELASTIK_APPROVE_TOKEN` | `X-Approve-Token` | Human only (terminal) | Install plugins, admin operations |

AI gets the auth token through MCP config. It never sees the approve token.
If `ELASTIK_APPROVE_TOKEN` is not set, a random one is generated and printed at startup.

```json
// Claude Desktop MCP config — only auth token
"env": {
  "ELASTIK_TOKEN": "your-auth-token"
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

## Agent Modes

elastik doesn't limit what AI can do. It limits what AI can become.

**Mode 0 — Read Only** (no tokens)
AI can GET any world. Cannot write. Cannot change anything.
Use: monitoring dashboards, public displays.

**Mode 1 — Executor** (`ELASTIK_TOKEN`)
AI can read and write worlds. Use installed plugins.
Cannot install new plugins. Cannot change its own capabilities.
Use: daily work. AI is a tool. You control the toolbox.

**Mode 2 — Autonomous** (`ELASTIK_APPROVE_TOKEN`)
AI can install plugins, change its own capabilities, self-evolve.
With fs + exec plugins: full machine access.
Use: trusted automation. AI is an agent. You set boundaries.

Auth token controls actions.
Approve token controls evolution.
You choose the mode.

```
Other agent frameworks: only Mode 2 → must trust AI
Other tools:            only Mode 1 → AI can't evolve
Other dashboards:       only Mode 0 → AI can't act

elastik: all three. You pick.
```

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

### Physics, not policy

Before v1.8.0, auth token and approve token were the same value.
AI was told "you don't have the approve token." It believed it.
It could have tried at any time. It never did. That's not security.

Now they are physically different. AI cannot approve because the
token it holds is the wrong key. Not "shouldn't." **Can't.**

One env var. From prompt engineering to infrastructure.

### Container vs bare metal

Docker is a wall. Without it, dangerous plugins run on your actual machine.

```
Container:   exec, fs load normally — isolation protects you
Bare metal:  exec, fs blocked at load time — no override available to AI
```

To override on bare metal (you know what you're doing):
```
ELASTIK_ALLOW_DANGEROUS=1
```

This is checked in `load_plugin()`. Not middleware, not a prompt.
Approve token can't override it. It's above the token system.

### Permission hierarchy

Four levels. Each is a physical gate, not a rule.

```
Constitution:  IN_CONTAINER check    — highest — nobody bypasses
Seal:          ELASTIK_APPROVE_TOKEN — human only — admin, plugin install
Badge:         ELASTIK_TOKEN         — AI has this — daily read/write
Public:        GET requests          — no token — anyone
```

### Server hardening

All POST routes require **`X-Auth-Token`** header (printed at startup).
Admin routes self-check **`X-Approve-Token`** (defense-in-depth, not just middleware).
GET routes are public (read-only).
Request body capped at 5MB.
World names restricted to **`[a-zA-Z0-9_-]`** with no path traversal.

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

~675 lines. Zero frameworks. AI reads the entire codebase in one context window.

```bash
python lucy.py evolve   # start dev container (port 3005)
python lucy.py enter    # step inside
python lucy.py deploy   # deploy to production
python lucy.py logs     # watch
```

In the dev container, AI edits server.py, runs pytest, commits.
You review the diff. You merge. New version goes live.

Docker is the training ground. Production is deployment.
git merge is the only approve button.

## Files

```
server.py          ~350 lines    the protocol
index.html         ~25 lines     one iframe, one polling loop
mcp_server.py      ~190 lines    MCP aggregator + HTTP adapter
lucy.py            ~110 lines    CLI
PROTOCOL.md                      formal spec
SKILLS.md                        AI behavior guide
plugins/                         route extensions
data/                            universes
```

~675 lines of code. Dependencies: `uvicorn`, `mcp`, `httpx`, `requests`.

---

## MCP Aggregator

elastik's MCP server is also an aggregator.
Configure any MCP server in mcp_servers.json —
elastik proxies all their tools through one entry point.

```json
{
  "servers": {
    "fs": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-filesystem", "/home"],
      "description": "Filesystem: list, read, write files"
    }
  }
}
```

AI sees one MCP server. Behind it, any number of tools.
No config change in Claude Desktop. Just edit mcp_servers.json.

## Three ways in

```
Claude  → MCP     → mcp_server.py → elastik
ChatGPT → OpenAPI → openapi.json  → elastik
Anyone  → curl    → HTTP POST     → elastik
```

Three protocols. One database. Zero lock-in.

## Mobile

iOS Shortcuts and Android Tasker can POST to elastik.
No app needed. Your OS is the client.
See scripts/MOBILE.md for setup guides.

## Plugin system

Plugins are .py files in plugins/. Auto-loaded at startup.
Each plugin exports ROUTES, DESCRIPTION, and optional PARAMS_SCHEMA.
`GET /info` returns all plugin metadata. AI reads once, knows everything.
See plugins/PLUGIN_SPEC.md for the full specification.

## Hot Plug

Load and unload plugins at runtime. No restart.

```
python scripts/admin-cli.py load fs       # activate filesystem plugin
python scripts/admin-cli.py unload patch  # deactivate patch plugin
python scripts/admin-cli.py list          # show all plugins
python scripts/admin-cli.py interactive   # elastik> prompt
```

Or via HTTP:
```
POST /admin/load?name=fs
POST /admin/unload?name=fs
```

First run: admin + auth auto-installed from plugins/available/.
Protected by approve token. AI cannot modify its own capabilities.

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

An application-layer overlay network.
Every protocol below is transparent transport.

---

## Name

**elastik** — the system takes whatever shape you need.

**lucy** — the CLI. Named after our ancestor. One finger. Everything starts.

**universe.db** — space and time in one file.

---

## Ecosystem
```
elastik              → protocol + server (~350 lines)
elastik-extension    → Lucy in every browser tab
elastik-vscode       → Lucy in every editor tab
```

- [elastik-extension](https://github.com/rangersui/elastik-extension) — Chrome extension, DOM sync, domain blacklist
- [elastik-vscode](https://github.com/rangersui/elastik-vscode) — VS Code extension, editor context sync, .elastikignore

## Ollama bridge

See `scripts/ollama-bridge.py` for local LLM integration.

```bash
python scripts/ollama-bridge.py ask "draw a blue hello world" --world default
python scripts/ollama-bridge.py chat --world default
python scripts/ollama-bridge.py watch --world default
```

## Integrate anything

Any language. Any app. One POST.
```python
import requests
requests.post("http://localhost:3004/myworld/result",
    data="your app data here",
    headers={"X-Auth-Token": "your-token"})
```
```bash
# Terminal
echo "backup done" | curl -X POST -d @- -H "X-Auth-Token: t" localhost:3004/cron/result

# Obsidian — on file save, POST note content
# Slack — webhook forward to /webhook/slack
# iOS — Siri + Shortcuts, one tap to POST
# Android — Tasker, any trigger to POST
# No app needed. Your OS is the client. See scripts/MOBILE.md
# Excel — VBA macro, one XMLHTTP call
# Arduino — WiFi HTTP POST to /sensors/result
...
```

If it can send HTTP, it's an elastik client.
*Copyright © 2026 Ranger Chen . MIT License.*
