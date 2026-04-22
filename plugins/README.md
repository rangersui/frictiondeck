# plugins/

This directory is for **server primitives** that are installed as
`/lib/<name>` worlds.

A plugin belongs here when it changes elastik's behavior from the server side:

- adds routes
- adds device surfaces
- adds replication or cloning capabilities
- adds negotiated response logic
- becomes part of the machine's primitive vocabulary

Rule of thumb:

- `plugins/` = "server behavior"
- `clients/` = "external consumer"

## Current primitives

| File | Route(s) | Role |
|---|---|---|
| `example.py` | `/example` | smallest Tier 1 specimen; template for a route plugin |
| `reality.py` | `/__reality__`, `/self` | self-replication — data tar.gz + source tar.gz |
| `gpu.py` | `/dev/gpu`, `/dev/gpu/stream` | blind AI device + SSE streaming sibling; backend from `/etc/gpu.conf`; three wire formats (ollama NDJSON / openai-compat SSE / claude named-event SSE) unified behind one token-iterator contract |
| `fstab.py` | `/mnt/*` | blind mount of **any registered URI scheme** (file + http/https in Phase 1; postgres/s3/redis in later phases); mount table in `/etc/fstab`; per-scheme adapters in the plugin |
| `db.py` | `/dev/db` | read-only SQL over worlds or **file-kind** fstab mounts; non-file mounts (http/https/…) reject with 400 — use `/mnt/<name>/<path>` for raw bytes |
| `fanout.py` | `/dev/fanout` | broadcast one write to N worlds; target list in `/etc/fanout.conf` |
| `semantic.py` | `/shaped/*` | Accept/User-Agent driven shape renderer; `text/event-stream` in Accept triggers SSE outer transport with inner MIME picked from the rest of the list; delegates to `/dev/gpu` (one-shot) or `/dev/gpu/stream` |

`gpu` / `fstab` / `db` / `fanout` form a **machine-primitives set** —
blind device, blind mount, blind query, blind broadcast. Each has a
config world under `/etc/<plugin>` or `/etc/<plugin>.conf`; runtime
behaviour swaps by `PUT /etc/...` without a plugin reload. `semantic.py`
is a higher-layer plugin that composes on top of `/dev/gpu`.

## Install model

Plugins are not auto-loaded from the repo checkout. They are staged into
`/lib/<name>` and then activated. The `install.sh` / `install.ps1`
helpers wrap the two-PUT dance.

You can either install one plugin at a time, or install the official
machine-primitives set in one command:

```bash
export ELASTIK_TOKEN=your-t2-token
export ELASTIK_APPROVE_TOKEN=your-t3-token

# one plugin:
./plugins/install.sh gpu

# official primitive set:
./plugins/install.sh primitives

# primitive set + semantic layer:
./plugins/install.sh primitives --with-semantic
```

```powershell
$env:ELASTIK_TOKEN="your-t2-token"
$env:ELASTIK_APPROVE_TOKEN="your-t3-token"

# one plugin:
.\plugins\install.ps1 gpu

# official primitive set:
.\plugins\install.ps1 primitives

# primitive set + semantic layer:
.\plugins\install.ps1 primitives -WithSemantic
```

If you do not want to set env vars first, PowerShell can also pass the
token explicitly:

```powershell
.\plugins\install.ps1 gpu -Token "your-t3-token"
.\plugins\install.ps1 primitives -Token "your-t3-token"
```

The raw HTTP form (what the helpers run for you):

```bash
curl -X PUT http://localhost:3005/lib/example \
  -H "Authorization: Bearer $ELASTIK_TOKEN" \
  --data-binary @plugins/example.py

curl -X PUT http://localhost:3005/lib/example/state \
  -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" \
  --data-binary "active"
```

`primitives` expands to:

- `gpu`
- `fstab`
- `db`
- `fanout`

`semantic` stays opt-in because it builds on `/dev/gpu` rather than
being part of the minimal blind device/mount/query/broadcast base.

## What does NOT belong here

Do not put these in `plugins/`:

- desktop app wrappers
- Office workbooks/documents/decks
- shell convenience clients
- bots that merely call elastik over HTTP
- dashboards that render existing routes without extending the server

Those belong in [`clients/`](../clients/README.md).
