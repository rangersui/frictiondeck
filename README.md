# elastik

*v5.0.0 lambda — one file runs it. Plugins are worlds.*

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

The repo ships two specimens in `plugins/`. Neither is auto-loaded.

| File | Shape |
|---|---|
| `plugins/example.py` | 13-line template — `AUTH`, `ROUTES`, `handle()` contract |
| `plugins/reality.py` | self-replicator — GET `/__reality__` (data tar.gz) + GET `/self` (source tar.gz) |

Any source-changing PUT resets `state=pending`, so approval re-binds to
the new source hash. The chain records `stage_written` (with
`body_sha256_after`) and `state_transition` events on every step.

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
