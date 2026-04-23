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
| `dav.py` | `/dav/*` | **opt-in** WebDAV view over the FHS tree; install into `/lib/dav` to expose `/dav/*`; reads are public like core reads, writes require auth, system-prefix writes require approve |
| `semantic.py` | `/shaped/*` | Accept-driven shape renderer; `text/event-stream` in Accept triggers SSE outer transport with inner MIME picked from the rest of the list; `X-Semantic-Intent` is the browser-safe hint override when `User-Agent` cannot be changed; delegates to `/dev/gpu` (one-shot) or `/dev/gpu/stream` |
| `router.py` | `/_router_fallback` (hook-only) | **opt-in** SLM-assisted resolver for unmatched paths; converts "no such world" into `303`/`300`/`404-with-prose` based on the caller's readable pool; caller-scoped candidate pool, separate rate cap, local-only backend by default; see "Semantic router" note below |

`gpu` / `fstab` / `db` / `fanout` form a **machine-primitives set** —
blind device, blind mount, blind query, blind broadcast. Each has a
config world under `/etc/<plugin>` or `/etc/<plugin>.conf`; runtime
behaviour swaps by `PUT /etc/...` without a plugin reload. `semantic.py`
and `router.py` are higher-layer plugins that compose on top of
`/dev/gpu`.

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
`router` is also opt-in for the same reason plus the ones in the
router note below.

## Semantic router (`/_router_fallback`) — opt-in

Installs exactly like any other plugin:

```bash
# Install source (T2) + activate (T3):
curl -X PUT http://localhost:3005/lib/router \
  -H "Authorization: Bearer $ELASTIK_TOKEN" \
  --data-binary @plugins/router.py
curl -X PUT http://localhost:3005/lib/router/state \
  -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" \
  --data-binary "active"
```

Once installed, a `GET /<path>` that matches nothing else no longer
falls straight to the default 404. The server-side hook (added in
`server.py`'s `app()` dispatch, enabled by the presence of
`/lib/router`) hands the request to router, which:

1. Looks up the recent worlds the caller is READABLE against — T1
   anonymous gets `/home/*` + `proc/*` + `bin/*` + `mnt/*`; T2/T3
   get everything; cap tokens stay inside their prefix. Router
   never surfaces world names the caller cannot discover by direct
   navigation, because "auth is physics, not policy."
2. Narrows the pool to ~50 candidates by stdlib Levenshtein +
   substring scoring (no SLM yet, no rate cap consumed).
3. Asks `/dev/gpu` non-stream to pick the closest match. SLM sees
   only the normalized request path and candidate names — never
   headers, never body, never worlds outside the pool.
4. Returns `303` (single match), `300` (ambiguous, 5 or fewer), or
   `404` with SLM-written prose (nothing close enough). The
   second-line defence discards any SLM-returned name that is not
   in the candidate pool, so hallucinated "here, let me suggest
   `/etc/private-note`" answers are silently dropped.

### Safety posture

- **Default local-only backend.** Router refuses to call SLM when
  `/etc/gpu.conf` names a non-local scheme (openai, claude, etc.)
  unless `SEMANTIC_ROUTE_EXTERNAL_OK=1` or
  `SEMANTIC_ROUTE_LOCAL_ONLY=0`. Otherwise anonymous typo traffic
  would leak candidate world names to the external model — wider
  exfiltration than `/shaped/*` has.
- **Separate rate cap.** `SEMANTIC_ROUTE_CAP_PER_MIN` (default 120)
  is distinct from semantic's `SEMANTIC_GEN_CAP_PER_MIN` so a
  typo-heavy crawler cannot drain `/shaped/*` budget.
- **URL-as-prompt privacy.** Natural-language URLs end up in
  access logs, Referer headers, browser history, and proxy logs.
  Do NOT put secrets, people's names, or references to specific
  conversations in router-targeted URLs. The /shaped/* + header
  path (`X-Semantic-Intent`) is safer for sensitive intent.
- **Hook-only route.** `/_router_fallback` is blocked for direct
  external requests by a server-core gate; it is reachable only
  via the internal fallback hook.

### Scope today

Router resolves **top-level natural-language paths** (`/salse-report`,
`/帮我画销售饼图`, `/sales report`). It does NOT currently catch
typos under specific-route prefixes like `/home/<typo>` — elastik's
`/home/*` handler emits its own "world not found" 404 before the
fallback hook reaches router. Extending router to cover those
namespaces is follow-up work; see `PLAN-semantic-router.md`
architecture-scope note.

### Config

All knobs are env-overridable. Defaults in `plugins/router.py`:

```
SEMANTIC_ROUTE_CAP_PER_MIN   = 120   # separate from shape cap
SEMANTIC_ROUTE_CACHE_MAX     = 10000 # LRU cap on cached decisions
SEMANTIC_ROUTE_RECENT_MAX    = 500   # per-caller pool size
SEMANTIC_ROUTE_SCAN_CAP      = 4000  # FS scan ceiling
SEMANTIC_ROUTE_TOPK          = 50    # pre-filter narrows to this
SEMANTIC_ROUTE_TTL_SEC       = 3600  # cached decision expiry
SEMANTIC_ROUTE_LOCAL_ONLY    = 1     # refuse external backends
SEMANTIC_ROUTE_EXTERNAL_OK   = 0     # explicit opt-in for external
SEMANTIC_ROUTE_DEBUG         = 0     # surface pool_set in 404 hdrs
```

## `/shaped/*` browser note

Today `/shaped/*` is still primarily a **header contract**, not a
finished browser product surface.

Recommended testing today:

- `curl` when you want exact control
- or a browser header-editing tool when you want to watch the response in
  a tab

Browser caveat:

- `User-Agent` is not reliably writable from browser JS, so semantic
  accepts `X-Semantic-Intent` as the explicit hint override

Deferred on purpose:

- a dedicated `shaped.html` shell / polished browser UI is future work
- it is **not** part of the current merge target

## What does NOT belong here

Do not put these in `plugins/`:

- desktop app wrappers
- Office workbooks/documents/decks
- shell convenience clients
- bots that merely call elastik over HTTP
- dashboards that render existing routes without extending the server

Those belong in [`clients/`](../clients/README.md).
