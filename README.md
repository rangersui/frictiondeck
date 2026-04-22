# elastik

*v5.0.0 lambda — one file runs it. Plugins are worlds.*

You have an HTTP machine.

It can store raw worlds like a pastebin.

But on read, its content-negotiation layer can think.

This is not an AI API server.

It is an HTTP server where AI shapes the response representation.

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

Most "AI servers" today mean:

```
POST /v1/chat/completions
{"messages":[{"role":"user","content":"..."}]}
```

That is an API for AI.

elastik flips the stack:

- HTTP is the product
- AI is infrastructure
- the model sits between data and representation

AI is not the endpoint. AI is in the protocol path.

```
PUT  /home/work -d "<h1>hello</h1>"   → stored in SQLite
GET  /home/work                        → {"stage_html":"<h1>hello</h1>","version":1}
```

Every path is a world. Writing to a new path creates it.

At the storage layer, elastik is still beautifully dumb: bytes go in, bytes come out.

But on read, plugins like `/shaped/*` can insert a thinking layer into HTTP semantics:

```
source bytes -> Accept + hint negotiation -> /dev/gpu transform -> cache -> response
```

Accept constrains output format. 406 constrains mismatch. 429 constrains rate.
304 constrains repetition. `Vary` constrains cache identity.

The protocol keeps the model in a cage.

FHS layout:

```
/home/       your stuff
/etc/        config (T3 to write)
/boot/       startup config (T3 to read, changes need restart)
/usr/lib/    skills and renderers (conventional)
/var/log/    logs (conventional)
/proc/       introspection (uptime, version, status, worlds)
/bin/        active routes (core + /lib plugins)
/dev/        devices (Tier 1 plugins can register)
/dav/        WebDAV (mount in Finder/Explorer)
```

## API

```
GET    /home/{name}       read (JSON)
GET    /home/{name}?raw   raw bytes
HEAD   /home/{name}       stat — same headers as GET, no body
PUT    /home/{name}       overwrite
POST   /home/{name}       append
DELETE /home/{name}       delete (T3)
GET    /home/             ls (trailing slash)

GET    /stream/{name}     SSE live updates
GET    /proc/status       machine state {pid, uptime, worlds, plugins, version}
GET    /proc/worlds       list all worlds
GET    /bin               list all active routes
```

HTTP method IS the action. No `/read` `/write` suffixes. Trailing `/` = ls.

Content negotiation: browser gets HTML, curl gets JSON, pipes get plain text.

## Auth

Three tiers. Physics, not policy.

```
T1 (no token)      read anything public
T2 (auth token)    write /home/*
T3 (approve token) write /etc/*, /usr/*, /boot/*. delete. activate plugins.
```

```bash
# .env
ELASTIK_TOKEN=your-t2-token
ELASTIK_APPROVE_TOKEN=your-t3-token
```

The examples below assume those env vars are already present in your shell.
If they are not, either export/set them first, or replace the `$...TOKEN`
placeholders with literal bearer tokens by hand.

```bash
# bash/zsh
export ELASTIK_TOKEN=your-t2-token
export ELASTIK_APPROVE_TOKEN=your-t3-token
```

```powershell
# PowerShell
$env:ELASTIK_TOKEN="your-t2-token"
$env:ELASTIK_APPROVE_TOKEN="your-t3-token"
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

## Metadata headers

`X-Meta-*` request headers travel with each PUT. Stored with the world,
replayed on read, bound to the body via an HMAC-chained event.

```bash
curl -X PUT localhost:3005/home/findings/x \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Meta-Author: codex" \
  -H "X-Meta-Confidence: 0.95" \
  -H "X-Meta-Severity: high" \
  -d "uint16 truncation in parse_url. upstream archived."

# GET and HEAD reflect metadata in response headers:
curl -sI "localhost:3005/home/findings/x"
# → x-meta-author: codex
#   x-meta-confidence: 0.95
#   x-meta-severity: high
# ?raw is the same plus raw body bytes (content-type from ext).

# bound to content in the event log. Each world has an events table in
# its universe.db; inspect directly with sqlite3:
sqlite3 'data/home%2Ffindings%2Fx/universe.db' \
  "SELECT payload FROM events ORDER BY id DESC LIMIT 1"
# → {"op":"put", "meta_headers":[["x-meta-author","codex"],...],
#     "body_sha256_after":"e3b0...", "version_after":1, "len":58}
```

Body is content. Header is metadata about content. Event is the
receipt that binds both to a moment in time.

Only `X-Meta-*` is stored. `Authorization`, `X-Forwarded-*`,
`X-Accel-Redirect`, and everything else is dropped — the prefix
whitelist keeps infrastructure-interpreted response headers out of
the reflection path. Byte budget: 1 KB per value, 8 KB total.

Scope note: POST append does **not** update metadata. The audit
event records `meta_headers: []` for appends. Use PUT when
authorship changes.

Audit boundary: the HMAC chain covers the `events` table, not
`stage_meta`. You get event-level binding — "this body hash matches
what this writer claimed they wrote" — not full-state tamper-proof
snapshot history. Direct edits to the current row cause the current
body hash to stop matching the last event's `body_sha256_after`; a
broken events chain means the audit log itself was tampered with.
Two independent checks.

Convention hints (not enforced, invent your own if needed):
`X-Meta-Author`, `X-Meta-Confidence` (0.0–1.0), `X-Meta-Intent`,
`X-Meta-Severity`, `X-Meta-Scope`, `X-Meta-Source`, `X-Meta-Model`,
`X-Meta-Supersedes`, `X-Meta-Depends-On`.

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

Plugins are `/lib/<name>` worlds in SQLite. PUT source with T2, approve
with T3 by setting `state=active`, routes register live. No disk loader.
No restart.

```bash
# Install (T2 writes source; state resets to 'pending'):
curl -X PUT http://localhost:3005/lib/example \
  -H "Authorization: Bearer $ELASTIK_TOKEN" \
  --data-binary @plugins/example.py   # --data-binary preserves newlines; -d will mangle Python source

# Activate (T3 — execs source, registers declared ROUTES):
curl -X PUT http://localhost:3005/lib/example/state \
  -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" \
  --data-binary "active"

# Use it:
curl http://localhost:3005/example -d "hi"
# → {"hello":"from example plugin","echo":"hi"}

# Verify:
curl http://localhost:3005/bin     # is the route registered?
curl http://localhost:3005/lib/    # list all installed plugin worlds

# Disable (T3 — route down, source kept):
curl -X PUT http://localhost:3005/lib/example/state \
  -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" --data-binary "disabled"

# Delete (T3 — source world moves to .trash):
curl -X DELETE http://localhost:3005/lib/example \
  -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN"
```

The repo ships the following plugins in `plugins/`. None auto-load.
Install one by name via `./plugins/install.sh <name>` (or the
PowerShell twin `./plugins/install.ps1 <name>` on Windows), or use the
official primitive-set target to bootstrap a complete machine surface in
one shot.

First time, set the token env vars in the shell where you will run the
helper:

```bash
# bash/zsh
export ELASTIK_TOKEN=your-t2-token
export ELASTIK_APPROVE_TOKEN=your-t3-token

# one plugin:
./plugins/install.sh gpu

# official machine-primitives set:
./plugins/install.sh primitives

# primitive set + semantic shaping:
./plugins/install.sh primitives --with-semantic
```

```powershell
# PowerShell
$env:ELASTIK_TOKEN="your-t2-token"
$env:ELASTIK_APPROVE_TOKEN="your-t3-token"

# one plugin:
.\plugins\install.ps1 gpu

# official machine-primitives set:
.\plugins\install.ps1 primitives

# primitive set + semantic shaping:
.\plugins\install.ps1 primitives -WithSemantic
```

| File | Routes | Role |
|---|---|---|
| `plugins/example.py` | `/example` | 13-line template — `AUTH`, `ROUTES`, `handle()` contract |
| `plugins/reality.py` | `/__reality__`, `/self` | self-replicator — data tar.gz + source tar.gz |
| `plugins/gpu.py` | `/dev/gpu`, `/dev/gpu/stream` | AI as a device. One-shot + streaming sibling. Backend from `/etc/gpu.conf` (ollama / openai / claude / deepseek / vast) |
| `plugins/fstab.py` | `/mnt/*` | Mount local directories AND external sources (https, http) under `/mnt/`. Mount table in `/etc/fstab`. Per-scheme adapters in the plugin. |
| `plugins/db.py` | `/dev/db` | Read-only SQL over worlds or **file-kind** `/mnt/*` mounts. http(s) mounts reject with 400. |
| `plugins/fanout.py` | `/dev/fanout` | Broadcast one write to N worlds. Target list in `/etc/fanout.conf` |
| `plugins/semantic.py` | `/shaped/*` | Accept-driven shape renderer. `text/event-stream` in Accept turns on SSE outer transport; `X-Semantic-Intent` is the browser-safe hint override when you cannot set `User-Agent`. Delegates to `/dev/gpu` or `/dev/gpu/stream`. |

`gpu / fstab / db / fanout` form a **machine-primitives set** — blind
device, blind mount, blind query, blind broadcast. Each has a config
world under `/etc/<plugin>` or `/etc/<plugin>.conf`, so runtime
behaviour swaps by `PUT /etc/...` without a plugin reload.

The `primitives` install target expands exactly to:

- `gpu`
- `fstab`
- `db`
- `fanout`

`semantic` is left opt-in because it composes on top of `/dev/gpu`
rather than being part of the minimal blind primitive base.

### `/shaped/*` today

`/shaped/*` is still a **header-driven API** first.

Canonical test path today:

- `curl` for exact headers
- or a browser extension / devtool that can edit request headers

The minimum useful streaming request is:

```bash
curl -N "http://localhost:3005/shaped/home/retro" \
  -H "Authorization: Bearer $ELASTIK_TOKEN" \
  -H "Accept: text/event-stream, text/html" \
  -H "X-Semantic-Intent: grandma/1.0 (big-font, no-jargon)"
```

Notes:

- `Accept: text/event-stream, <inner-mime>` means:
  - outer transport = SSE
  - inner shape = `<inner-mime>`
- browsers cannot reliably override `User-Agent`, so browser tests should
  send `X-Semantic-Intent` instead
- plain `Accept: text/html` is a normal one-shot shaped response, **not**
  the streaming path

#### Ask a world

For a single world, `/shaped/*` already covers a large part of the
"ask your docs" / summarise-this-file use case.

Store the source once, then read it back with an answering or
summarising intent:

```bash
curl "http://localhost:3005/shaped/home/albon-intel" \
  -H "Authorization: Bearer $ELASTIK_TOKEN" \
  -H "Accept: text/plain" \
  -H "X-Semantic-Intent: answer-only/1.0 (question: what is their funding source?)"
```

With modern long-context models, many of these single-world reads do not
need chunking, embeddings, or a vector database. `/shaped/*` can often
just read the world and answer.

This is **not** cross-world retrieval. It is direct semantic read over
one source. Cross-world routing / composition is future work.

Dedicated `/shaped/*` browser UX is intentionally deferred. A proper
`shaped.html` / browser-side shell is future work and **not part of this
merge**; today the supported story is still "set the headers you want."

### Mount anything

`plugins/fstab.py` mounts **any URI scheme with a registered adapter**
under `/mnt/*`. Phase 1 ships `file` (local directories) and `http` /
`https` (remote HTTP endpoints). Files that live outside elastik —
your projects folder, a browser history SQLite, an internal JSON API,
Excel `.xlsm`, images, a music library — all become readable through
elastik's HTTP surface via one uniform `/mnt/<name>/<path>` form.

Install first (it is a plugin, not core):

```bash
./plugins/install.sh fstab
```

Then write `/etc/fstab` — one line per mount, `source  /mnt/name  mode[,opts]`:

```bash
curl -X PUT http://localhost:3005/etc/fstab \
  -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" \
  --data-binary @- <<'EOF'
/Users/ranger/projects  /mnt/work   rw
/home/ranger/docs       /mnt/docs   ro
/Users/ranger/Library/Application Support/BraveSoftware/Brave-Browser/Default  /mnt/brave  ro
https://api.example.com  /mnt/api   ro,bearer=xyz
http://10.0.0.5:8080     /mnt/intra ro
EOF
```

Source column syntax:
- **absolute local path** → `file` adapter (backwards-compatible with pre-v0.2 fstab)
- **`scheme://endpoint`** → looked up in fstab's adapter table (`https`, `http` today; `postgres` / `s3` / `redis` in later phases)

Mode column accepts comma-delimited opts. `ro` / `rw` is the mode;
anything after the first comma is adapter-specific. The https adapter
reads `bearer=<value>` and attaches it as the upstream `Authorization`
header so `/mnt/api` can front an authenticated API without leaking
the token to clients.

Path with spaces (load-bearing example — Brave's profile path): the
parser right-biases on `rsplit(None, 2)` so the mount-point and mode
fields are always the last two whitespace-separated tokens; everything
before them is one source path regardless of internal whitespace.

Once mounted:

```bash
curl http://localhost:3005/mnt/                  # list mounts (all kinds)
curl http://localhost:3005/mnt/work/             # directory listing (file)
curl http://localhost:3005/mnt/brave/History     # read a file (file, raw bytes)
curl http://localhost:3005/mnt/api/users/42      # proxy GET to https upstream
```

Response headers:
- `Content-Type` — inferred from extension for file mounts; proxied from upstream for http(s)
- `X-Mount-Version` — `mtime:<ns>` for file; `etag:<value>` or `len=N;head=<hex>` for http(s)

`rw` mounts accept POST (file-kind only, write requires T2 auth);
`ro` mounts are GET-only. https mounts are read-only in Phase 1
regardless of mode (a POST to an http mount rejects with 405 before
the upstream is contacted). http(s) reads are capped at 5 MB per
response — /mnt/ is an unauthenticated surface (`AUTH="none"`) and
an uncapped proxy would let any declared remote mount pull arbitrary
bytes into memory.

Writing to `/etc/fstab` requires T3 (approve) — that IS the permission
model: what can enter the tree is decided by who can write the mount
table.

**Composes with `/dev/db`** for SQL over any file-kind external SQLite:

```bash
./plugins/install.sh db
curl -X POST "http://localhost:3005/dev/db?file=brave/History" \
  -H "Authorization: Bearer $ELASTIK_TOKEN" \
  -d "SELECT url FROM urls ORDER BY last_visit_time DESC LIMIT 10"
```

`?file=brave/History` resolves against the mount table: `brave/...`
means "the file at this path under whatever local directory `/mnt/brave`
points to." Read-only connection enforced at the SQLite layer.

**`/dev/db` only accepts file-kind mounts.** SQLite can only open local
files; an https mount pointed at `?file=api/whatever` rejects with a
clean 400 naming the scheme — use `/mnt/<name>/<path>` for raw bytes
over the adapter instead. Status matrix:

- `?file=<file-mount>/<path>` → `200` if path exists, `404` if not
- `?file=<http-mount>/<path>` → `400` wrong kind
- `?file=<unknown-mount>/...` → `404` mount missing
- path traversal (`..`) → `403`

Any source-changing PUT resets `state=pending`, so approval re-binds to
the new source hash. The chain records `stage_written` (with
`body_sha256_after`) and `state_transition` events on every step.

A plugin can do anything its source allows — including route registrations
that make the HTTP interface unreachable. elastik is a process, not a
sovereign OS. The filesystem is the real source of truth:

```bash
# Stop the server. Then:
rm -rf "data/lib%2F<bad-plugin>/"
python server.py
```

No recovery endpoint. No "safe mode" flag. The directory under `data/` is
the plugin; `rm` is the uninstall. Works the same whether the plugin
never activated, bricked everything, or just turned out boring.

## Pipes

curl output is plain text. Unix pipes just work.

```bash
curl localhost:3005/home/ | grep boot
curl localhost:3005/home/ | wc -l
curl localhost:3005/home/ | shuf | head -1
```

## Self-test

```bash
# From repo:
python tests/boot.py

# From inside elastik (the compiler compiles itself):
curl -s "localhost:3005/home/boot?raw" | python -X utf8 -
```

## Introspection

Snapshot a machine into durable worlds with one command:

```bash
# If ELASTIK_TOKEN is configured (default via .env.example), export it first:
ELASTIK_TOKEN=your-t2-token bash examples/introspect.sh   # writes /home/env/{os,disk,...}
curl localhost:3005/home/env/                             # list
curl localhost:3005/home/env/processes?raw                # read content
```

`ps` output becomes addressable. Useful for remote diagnostics without
SSH, AI agents that want persistent self-knowledge across sessions,
and `diff`ing environment state over time (each write bumps the
world's version).

What gets stored: `ps -eo user,pid,pcpu,pmem,comm` — command *names*
plus basic stats (user, pid, %cpu, %mem). No argv, so secrets passed
on the command line are **not** persisted. Edit the script to use
`ps aux` if you want full argv (and accept that trade-off).

## Self-replication

`/self` and `/__reality__` come from `plugins/reality.py`. It ships in
the repo but isn't auto-loaded — install it once per deployment:

```bash
curl -X PUT http://A/lib/reality \
  -H "Authorization: Bearer $TOKEN" --data-binary @plugins/reality.py
curl -X PUT http://A/lib/reality/state \
  -H "Authorization: Bearer $APPROVE" --data-binary "active"
```

Then the clone is two curls and a tar:

```bash
curl -H "Authorization: Bearer $APPROVE" http://A/self > elastik.tar.gz
curl -H "Authorization: Bearer $APPROVE" http://A/__reality__ > data.tar.gz
tar xzf elastik.tar.gz && tar xzf data.tar.gz && python server.py
```

The clone can install reality and clone itself.

## Supply chain

Zero runtime deps. `_mini_serve` makes uvicorn optional.

`uvicorn[standard]` installs `httptools`, which Uvicorn uses by default
for HTTP/1.1. We reported an upstream `parse_url()` truncation bug as
[MagicStack/httptools#142](https://github.com/MagicStack/httptools/issues/142).

elastik on `_mini_serve` avoids that code path entirely. Under uvicorn,
elastik forces `http="h11"` to avoid it there too. It still keeps
conservative path handling and an 8 KB URL cap for ordinary oversized
requests.

---

elastik is storage-agnostic, transport-agnostic, and interface-relative.
What remains is only relation.

埏埴以为器，当其无，有器之用。
*You shape clay into a vessel. The emptiness inside makes it useful.*

🗿

*MIT License. Ranger Chen, 2026.*
