# Elastik OS

**Everything is a DOM.**

---

## What is this

An empty iframe. A backend with `/proxy`. AI fills the rest.

There is no UI. AI builds it. Right now. For you. Based on what you just said.

There are no apps. There are no templates. There are no components.
There is a wall. AI draws on it. You use what appears. It disappears when you're done.

Your judgments are silently logged. Your history is immutable. You never notice.

---

## Install

```bash
git clone https://github.com/rangersui/elastik
cd elastik
pip install -r requirements.txt
python server.py
```

Open `http://localhost:3004`. Empty. Say something to your AI. Watch.

---

## Connect AI

Any MCP-compatible client works:

```json
{
  "mcpServers": {
    "elastik": {
      "command": "python",
      "args": ["path/to/elastik/mcp_server.py"]
    }
  }
}
```

Claude Desktop, Gemini CLI, Cursor, Windsurf, VS Code Copilot, Cline, JetBrains — anything with MCP.

---

## Architecture

```
Two processes. Two databases per world. One iframe.

server.py       → HTTP server (human's eyes)
mcp_server.py   → MCP endpoint (AI's hands)

data/<name>/
  stage.db      → what's on the wall right now
  history.db    → everything that ever happened (HMAC signed)

static/
  index.html    → an iframe and a polling loop. ~10 lines.
```

---

## Multi-Stage

Every URL is a world.

```
localhost:3004/          → list of all worlds
localhost:3004/work      → work world
localhost:3004/project   → project world
localhost:3004/home      → home world

Visit a URL that doesn't exist → it's created. Empty wall. Ready.
```

Each world has its own `stage.db` and `history.db`. Independent. Parallel.

---

## How it works

```
AI writes HTML → stored in stage.db → iframe renders it → you see it

That's it.

AI writes <script> tags    → they execute in the iframe
AI writes onclick handlers → they work
AI loads CDN libraries     → React, D3, Three.js, Chart.js, anything
AI builds full applications → calculators, dashboards, editors, games

The wall grows. Or you wipe it clean. The history stays.
```

---

## Backend capabilities

AI can't touch the backend. You can.

```bash
lucy install fs          # file system access
lucy install modbus      # industrial protocol
lucy install terminal    # shell access
lucy install mqtt        # IoT messaging
lucy remove fs           # revoke access
lucy list                # what's installed
```

Every plugin adds a `/proxy` route. AI discovers it via `get_proxy_whitelist()` and starts using it immediately. No configuration. No restart.

Plugins are reviewed before installation. AI proposes. You approve.

---

## Security model

```
Frontend (iframe):
  sandbox="allow-scripts allow-same-origin allow-popups"
  CSP: connect-src 'self' (can only fetch localhost)
  AI draws freely. Can't escape. Can't reach the internet directly.

Backend (server.py):
  /proxy whitelist — AI can only call approved APIs
  Plugin approval — AI proposes code, human reviews and approves
  History — every action logged, HMAC signed, immutable

AI is in a glass room. It can paint anything.
It can't break the glass.
```

---

## What dies

```
Replaced by AI + iframe + /proxy:

  File managers      IDE               Email clients
  Note-taking apps   Git GUIs          Cloud storage UI
  Calculators        Search engines    Meeting transcription
  Browser extensions RAG pipelines     Low-code platforms
  Desktop widgets    Smart home panels  Arduino IDE
  Voice assistants   PR review bots    Workflow automation (Zapier)

Not replaced:
  Social networks (network effects)
  3A games (GPU rendering)
  VPN / networking (transport layer)
  Foundation models (the brain itself)
  Chips (the compute itself)
```

---

## Elastic Client

AI builds tools on the spot. You use them. They disappear when done.

Need a power calculator? AI builds one with sliders.
Need a Modbus debugger? AI builds one that talks to your PLC.
Need a news dashboard? AI builds one with live data.
Need something that has never existed? AI builds it.

The tool is temporary. Your judgment is permanent.

---

## Philosophy

```
Linux:    Everything is a file.
elastik:  Everything is a DOM.

Install     = appendChild
Uninstall   = element.remove()
Process     = iframe
IPC         = postMessage
Memory      = document.body.innerHTML
Persistence = stage.db
Log         = history.db (natural language, HMAC signed)
```

Traditional software is planned economy — product managers guess what users need, developers build it, users adapt.

elastik is free market — users say what they need, AI builds it, instantly.

Applications are nouns. elastik is a verb.

---

## Self-evolution

The system grows with you.

```
Day 1:    empty wall + default MCP tools
Day 30:   10 plugins + custom tools + AI knows your patterns  
Day 365:  a system that is uniquely yours

No two elastik instances are alike.
Because no two people are alike.
```

Code has no personality. `stage.db` does.
Code is DNA. The database is a life lived.

---

## The emptiness

```
We removed RAG.
We removed the LLM.
We removed embeddings.
We removed the component library.
We removed card templates.
We removed the HTML sanitizer.
We removed NLI verification.
We removed the friction gate.
We removed the tabs.
We removed the nav bar.
We removed the UI.

What's left:

  An iframe.
  A polling loop.
  Two SQLite files per world.
  A hash function.
  A version counter.

本来无一物。
```

---

## Stack

```
Python:     fastapi + uvicorn
Frontend:   one iframe
Database:   SQLite (WAL mode)
Protocol:   MCP (open standard)
Signature:  HMAC-SHA256
AI:         yours (Claude, Gemini, Llama, anything)

Total: ~2000 lines. Dependencies: 2.
```

---

## Name

**elastik** — because the system takes whatever shape you need.

**lucy** — the CLI. Named after our ancestor. One finger. Everything starts. Genesis.

---

*Copyright © 2026 Ranger Chen. AGPL v3.0.*
