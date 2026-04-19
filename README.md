# elastik

You have a Linux machine whose interface is curl.

## Quickstart

```bash
python server.py

# in another terminal:
curl -X PUT http://localhost:3005/home/hello -d "<h1>I am alive</h1>"

# open browser:
open http://localhost:3005/home/hello
```

Three seconds. You see it.

No pip. No npm. No Docker. Falls back to a built-in HTTP server if uvicorn is missing.
Works on anything with Python 3.8+: laptop, Raspberry Pi, iOS (a-Shell), Android (Termux).

## How it works

AI writes a string. Browser renders it. You see something.

```
PUT  /home/work -d "<h1>hello</h1>"   → stored in SQLite
GET  /home/work                        → {"stage_html":"<h1>hello</h1>","version":1}
```

Every path is a world. Writing to a new path creates it. FHS layout:

```
/home/       your stuff
/etc/        config (T3 to write)
/boot/       startup config (T3 to read, changes need restart)
/usr/lib/    skills, renderers (auto-synced)
/var/log/    system logs
/proc/       introspection (uptime, version, status, worlds)
/bin/        commands (plugin routes)
/dev/        devices (gpu, stone, fire, river, db)
/mnt/        local filesystem mounts (via /etc/fstab)
/dav/        WebDAV (mount in Finder/Explorer)
```

## API

```
GET    /home/{name}       read (JSON)
GET    /home/{name}?raw   raw bytes
PUT    /home/{name}       overwrite
POST   /home/{name}       append
DELETE /home/{name}       delete (T3)
GET    /home/             ls (trailing slash)
GET    /proc/worlds       list all worlds
GET    /bin               list all commands
```

HTTP method IS the action. No `/read` `/write` suffixes. Trailing `/` = ls.

Content negotiation: browser gets HTML, curl gets JSON, pipes get plain text.

## Mount anything

elastik mounts local paths through HTTP. No FUSE. No kernel module.

```bash
# Write fstab (T3):
curl -X PUT localhost:3005/etc/fstab \
  -H "Authorization: Basic $(echo -n ':$APPROVE' | base64)" \
  -d "/Users/you/Documents   /mnt/docs   ro
/Users/you/Code          /mnt/code   rw"

# Now:
curl localhost:3005/mnt/docs/               # ls
curl localhost:3005/mnt/code/server.py      # read file

# Query any SQLite database under a mount:
curl -X POST "localhost:3005/dev/db?file=brave/History" \
  -d "SELECT url, title FROM urls WHERE title LIKE '%pizza%' LIMIT 5"
```

Edit fstab. Next request reflects it. No reload. No mount command.
fstab is queried per-request, not loaded at boot.

## Auth

Three tiers. Physics, not policy.

```
T1 (no token)      read anything public
T2 (auth token)    write /home/*
T3 (approve token) write /etc/*, /usr/*, /boot/*. delete. admin.
```

```bash
# .env
ELASTIK_TOKEN=your-t2-token
ELASTIK_APPROVE_TOKEN=your-t3-token
```

## Capability tokens

Give an AI a path-scoped key instead of the full T2/T3 token.

```bash
# T3 mints a cap scoped to /home/scratch, 1-hour TTL, read+write:
curl -X POST "localhost:3005/auth/mint?prefix=/home/scratch&ttl=3600&mode=rw" \
  -H "Authorization: Bearer $APPROVE"
# → {"token":"<base64>.<hmac>"}
```

HMAC-signed, carries its own expiry, rejects anything outside `prefix`.
`mode=r` is read-only. No server state to revoke — wait for the TTL.

```bash
# AI writes inside /home/scratch:
curl -X PUT localhost:3005/home/scratch/notes \
  -H "Authorization: Bearer $CAP_TOKEN" -d "notes"

# But not outside:
curl -X PUT localhost:3005/home/other ...  # → 403
curl -X PUT localhost:3005/etc/foo  ...    # → 403
```

chroot for LLMs. Physics, not policy.

## Connect AI

Tell any AI:

> Write to http://localhost:3005/home/scratch with PUT.
> Token: $TOKEN. The browser will render whatever you write.

The AI writes HTML. You see it. Refresh writes again. That's the whole loop.

```bash
curl -X PUT http://localhost:3005/home/work \
  -H "Authorization: Bearer $TOKEN" \
  -d '<h1>hello world</h1>'
```

No SDK. No tool registration. No MCP. If it can send a string, it's an elastik client.

## Plugins

Plugins are .py files. Hot load/unload via HTTP. No restart.

```bash
curl -X POST /admin/load -H "Authorization: Bearer $APPROVE" -d "devtools"
```

| Plugin | What it does |
|--------|-------------|
| admin | Load/unload plugins at runtime |
| dav | WebDAV — worlds as files |
| devtools | grep, cowsay, 🗿, logic gates, electronics, flush |
| fstab | /mnt/ local filesystem mount |
| browser | Chrome/Brave/Edge remote control via CDP |
| db | /dev/db — read-only SQL on any SQLite |
| shell | Browser terminal |
| gpu | `/dev/gpu` — AI as device. Backend via `/etc/gpu.conf`. Ollama/Claude/OpenAI/Deepseek/Vast. |
| sse | Server-Sent Events — real-time streaming |

## Pipes

curl output is plain text. Unix pipes just work.

```bash
curl localhost:3005/home/ | grep boot
curl localhost:3005/home/ | wc -l
curl localhost:3005/bin | grep say
curl localhost:3005/home/ | shuf | head -1
```

## Self-test

```bash
# From repo:
python tests/boot.py

# From inside elastik (the compiler compiles itself):
curl -s "localhost:3005/home/boot?raw" | python -X utf8 -
```

## Self-replication

```bash
curl -H "Authorization: Bearer $APPROVE" http://A/self > elastik.tar.gz
curl -H "Authorization: Bearer $APPROVE" http://A/__reality__ > data.tar.gz
tar xzf elastik.tar.gz && tar xzf data.tar.gz && python server.py
```

Two curls. One clone. The clone can clone itself.

## Supply chain

Zero runtime deps. `_mini_serve` makes uvicorn optional.

`uvicorn[standard]` installs `httptools`, which Uvicorn uses by default
for HTTP/1.1. We reported an upstream `parse_url()` truncation bug as
[MagicStack/httptools#142](https://github.com/MagicStack/httptools/issues/142).

elastik on `_mini_serve` avoids that code path entirely. Under uvicorn,
elastik still keeps conservative path handling and an 8 KB URL cap for
ordinary oversized requests.

---

elastik is storage-agnostic, transport-agnostic, and interface-relative.
What remains is only relation.

埏埴以为器，当其无，有器之用。
*You shape clay into a vessel. The emptiness inside makes it useful.*

🗿

*MIT License. Ranger Chen, 2026.*
