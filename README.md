# elastik

Strings in, UI out. HTTP + SQLite + HMAC. That's it.

## Install

```bash
python server.py
```

No pip needed. Falls back to a built-in HTTP server if uvicorn is missing.
Works on anything with Python 3.8+: laptop, Raspberry Pi, iOS (a-Shell), Android (Termux).

## How it works

AI writes a string. Browser renders it. You see something.

```
POST /work/write    body: <h1>hello</h1>    → stored in SQLite
GET  /work/read                              → {"stage_html":"<h1>hello</h1>","version":1}
GET  /work                                   → browser renders it in an iframe
```

Every path is a world. Writing to a new path creates it.

## API

```
GET  /{name}/read      read content + version
GET  /{name}/raw       raw bytes with correct Content-Type
POST /{name}/write     overwrite content (version++)
POST /{name}/append    append to content (version++)
GET  /stages           list all worlds
```

Polling: `GET /{name}/read?v=3` → returns 304 if version unchanged.

Binary: `POST /{name}/write?ext=png` with raw bytes. `GET /{name}/raw` serves it back as `image/png`.

## Auth

Two tokens. One header. `Authorization: Bearer <token>`.

```
ELASTIK_TOKEN          → read/write worlds, use plugins
ELASTIK_APPROVE_TOKEN  → install plugins, admin, shell, HTML write
```

Basic Auth also works (password = token). WebDAV and browsers use this.

AI gets `ELASTIK_TOKEN`. Never sees the approve token. Physics, not policy.

```bash
# .env
ELASTIK_TOKEN=your-auth-token
ELASTIK_APPROVE_TOKEN=your-approve-token
```

## Plugins

Plugins are .py files. Hot load/unload via HTTP. No restart.

```bash
curl -X POST "/admin/load?name=reality" -H "Authorization: Bearer $APPROVE"
curl -X POST "/admin/unload?name=shell" -H "Authorization: Bearer $APPROVE"
```

Included plugins:

| Plugin | What it does |
|--------|-------------|
| auth | Token gate for writes |
| admin | Load/unload plugins at runtime |
| dav | WebDAV — worlds as files |
| shell | Browser terminal |
| reality | Self-snapshot for cloning |
| backup | Daily auto-backup with retention |
| ai | Ollama/Claude/OpenAI/Deepseek/Google relay |
| devtools | grep, tail, head, wc, rev, cowsay, etc. |
| public_gate | Public access gate. Unauthorized visitors see a pastebin. |

## WebDAV

Worlds as files. Mount in Finder, VS Code, Obsidian, or any editor.

```bash
# macOS: Cmd+K →
http://localhost:3005/dav

# Windows:
net use Z: http://127.0.0.1:3005/dav /user:x YOUR_TOKEN
```

## Self-replication

Every running elastik can clone itself.

```bash
# Pull source code (git-tracked files, no secrets)
curl -H "Authorization: Bearer $APPROVE" http://A/self > elastik.tar.gz

# Pull all data (atomic SQLite snapshots, WAL-merged)
curl -H "Authorization: Bearer $APPROVE" http://A/__reality__ > data.tar.gz

# Boot the clone
tar xzf elastik.tar.gz && tar xzf data.tar.gz && python server.py
```

No secrets included. `.env` and tokens are excluded from `/self`.

Two curls. One clone. The clone can clone itself.

## Files

```
server.py       the protocol + startup. One entry point.
plugins.py      plugin load/unload/cron
index.html      one iframe, one polling loop
```

## Security

```
physics > policy > training > luck
```

iframe sandbox (frontend). Docker optional (backend). Bearer auth (API). HMAC chain (audit).

AI holds `ELASTIK_TOKEN`. Can read/write. Cannot install plugins.
Human holds `ELASTIK_APPROVE_TOKEN`. Can do everything.
Different keys. Not "shouldn't." **Can't.**

## Connect AI

```bash
curl -X POST http://localhost:3005/work/write \
  -H "Authorization: Bearer $TOKEN" \
  -d '<h1>hello world</h1>'
```

If it can send a string, it's an elastik client.

---

*MIT License. Ranger Chen, 2026.*
