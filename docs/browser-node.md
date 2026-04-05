# Browser Node — Full Protocol Node in the Browser

Infrastructure design for elastik 3.0. Not implemented yet.

## Prior Work

This is not a new idea. The author's 2023 undergraduate thesis —
*"WebRTC-based Video Surveillance System"* — already validated the
browser-as-edge-node architecture in a production deployment:

- Raspberry Pi running Chromium (Puppeteer-automated) as the edge node
- WebRTC P2P video streaming directly from the Pi to mobile/desktop clients
- IndexedDB on the Pi's browser context for local recording + diagnostic
  event storage, never uploaded to cloud
- TensorFlow.js PoseNet running in the same tab for on-device inference
- Backend server only handled signaling and user management — never
  touched media content

Measured baselines from that system (Chapter 4 of the thesis):

| Scenario | Latency | Quality |
|---|---|---|
| LAN P2P (host candidate) | 2 ms | 60fps 480p stable |
| NAT-traversable (server reflexive) | 30–40 ms | 60fps 480p stable |
| TURN relay (symmetric NAT) | ~100 ms | degrades gracefully via adaptive bitrate |

**What the thesis proved**: a browser tab can be a sovereign edge node.
Local persistence, P2P networking, on-device AI, privacy-preserving
storage — all of it worked, in production, on a Raspberry Pi, in 2023.

**What the thesis couldn't do** (and why v3.0 matters):

1. **IndexedDB is a blob bucket, not a database.** The thesis stored
   video chunks as `{id: timestamp, data: Blob, event: tag}` — no SQL,
   no relational schema, no shared code with the server-side SQLite.
   Query capability was limited to primary-key lookup.
2. **The Pi still needed Node.js + Puppeteer + Chromium** to bootstrap
   the "browser node." Not a real browser tab — a scripted headless
   Chromium controlled by Node.js. Couldn't run on iOS, couldn't run
   on a locked-down Chromebook, couldn't run without a host OS capable
   of spawning Chromium processes.
3. **No shared protocol code between edge and server.** The edge ran
   JS + IndexedDB, the server ran Node.js + Express + in-memory state.
   No HMAC chain, no byte-identical event log, no audit trail that
   could be verified on both sides.

v3.0 browser-node is the direct continuation of that thesis path,
using the 2024–2025 technology stack that finally closes the three
gaps above:

- **OPFS + SQLite WASM** replaces IndexedDB → real relational schema,
  shared DDL with native server, SQL queries instead of blob scans
- **Go WASM** replaces Node.js + Puppeteer → the browser tab *is* the
  node, no host-side runtime required, runs on iOS Safari 17+
- **Same `core/` Go package** compiles for both native server and
  browser → HMAC event chain is byte-identical by construction, not
  "mostly compatible"

**Success criterion for v3.0**: match the thesis's latency and
throughput numbers (2 ms LAN, 40 ms WAN, 60fps sustained write
throughput) with *none* of the host-side dependencies. If a browser
tab on an iPhone can do what a Raspberry Pi with Chromium + Puppeteer
+ Node.js did in 2023, v3.0 is a win — not because it's faster, but
because it's universal.

The thesis also anticipated this trajectory. Chapter 5 "Future Work"
lists three items, in order:

1. Edge devices should support independent local configuration and
   survive backend failures
2. The backend should store no media-adjacent content, only user
   and resource metadata
3. Scale-out should come from P2P topology, not from centralized
   media servers

v3.0 browser-node makes all three structurally true, not by policy
but by architecture. The tab owns its own `universe.db`. The backend
is optional. Peers find each other over WebRTC. The thesis was the
proof-of-concept; elastik 3.0 is the generalization.

## The Goal

A browser tab should be able to run a complete elastik protocol node — not a
client talking to a server, but the server itself. Same SQLite schema. Same
HMAC chain. Same world CRUD. Same code, compiled twice.

## The Stack

```
┌─────────────────────────────────────────────────┐
│ UI layer (JS)                 — glue only       │
├─────────────────────────────────────────────────┤
│ Go WASM       — protocol logic (CRUD, HMAC)     │  ← same source as native
│ SQLite WASM   — database engine (C → WASM)      │
│ OPFS          — persistent storage (sync I/O)   │
├─────────────────────────────────────────────────┤
│ WebRTC        — native C++  → transport         │
│ Web Crypto    — native      → hashing, signing  │
│ WebGPU        — native      → AI inference      │
└─────────────────────────────────────────────────┘
```

JS does glue only. Every expensive operation runs in WASM or native browser APIs.

## Why OPFS Changes Everything

Before OPFS, browser persistence meant IndexedDB — async, key-value, not a
relational database. Running SQLite in the browser meant keeping everything
in memory, or slow async writes through IDB.

OPFS (Origin Private File System) is different:
- Real file system API, private per origin
- `createSyncAccessHandle()` — synchronous reads and writes
- SQLite's official WASM build supports OPFS natively
- Chrome, Edge, Firefox, Safari 17+ — all ship it
- Production-proven (Notion uses OPFS + SQLite)

Performance, measured honestly:
- WASM itself: ~90% of native
- OPFS sync access: ~1ms write latency (vs IndexedDB ~3ms+)
- Web Worker message serialization: ~4ms overhead per call
- **Overall: 70-80% of native SQLite on disk**

Not "indistinguishable from native." But 3-4× faster than IndexedDB, and
fast enough that elastik's `universe.db` (usually <10 MB) is never the
bottleneck.

## Isomorphic Compilation

One `core/` package in Go. Two compile targets:

```bash
# Native server
go build -o elastik-lite ./native

# Browser node
GOOS=js GOARCH=wasm go build -o elastik.wasm ./wasm
```

Both link the same `core/` package:

```go
// core/world.go — pure logic, no I/O
func WriteWorld(db DB, name, content string) (version int, err error) {
    // ... same code runs both sides
}

func LogEvent(db DB, action, body string) error {
    // ... same HMAC chain algorithm, byte-for-byte
}
```

The `DB` interface has two implementations:
- `native/db_sqlite.go` — wraps `modernc.org/sqlite` via `database/sql`
- `wasm/db_opfs.go` — wraps SQLite WASM via `syscall/js`, backed by OPFS

What you get from this:
1. **HMAC chain consistency** — if the server signs, the browser validates,
   both run the exact same function. Not "should match" — byte-identical AST.
2. **Schema consistency** — DDL is a `const` in core. Compiled once. Cannot drift.
3. **Type safety across the boundary** — `World.Version int` is the same struct
   on both sides. No Python-style `world["version"]` runtime guessing.

Python cannot do this. Pyodide can run Python in the browser but ships a
full CPython interpreter (~15 MB). Go WASM compiles the code itself.

## The Three Real Gotchas

### 1. SharedArrayBuffer requires COOP/COEP headers

OPFS's synchronous VFS needs `SharedArrayBuffer`, which browsers only expose
when the page is served with:

```
Cross-Origin-Opener-Policy:   same-origin
Cross-Origin-Embedder-Policy: require-corp
```

Miss either header, `SharedArrayBuffer` is unavailable, OPFS VFS fails to load.

**Workaround**: `coi-serviceworker.js` injects these headers from inside the
Service Worker, no backend changes required. elastik already ships a Service
Worker — just extend it.

### 2. Concurrent writes corrupt the database

Notion hit this in production: two tabs writing the same OPFS-backed SQLite
file → file corruption → users see wrong data.

**elastik is immune** because of the single-tab-single-world model. Each
universe.db is owned by one tab. Cross-tab coordination happens through
WebRTC DataChannels, not shared storage.

If we ever do need multi-tab, the rule is: one Web Worker holds the
connection, other tabs message it.

### 3. OPFS sync API is Worker-only

`createSyncAccessHandle()` only works in Web Workers. The main thread can't
touch OPFS synchronously.

**Architecture implication**: the Go WASM module runs in a dedicated Worker.
Main thread just forwards requests via `postMessage`. This is fine —
elastik's HTTP-style request/response model maps cleanly onto messages.

## Safari Quirk

Safari 16.x: OPFS exists but has bugs. SQLite WASM docs explicitly list
it as incompatible.

Safari 17+: fixed, but requires the **sahpool VFS** (Storage Access Handle
Pool) instead of the standard OPFS VFS. Slightly slower but works.

elastik's existing tier detection handles this:

| Tier | Browser | Storage | Speed |
|------|---------|---------|-------|
| 1 | Chrome, Edge | Standard OPFS VFS | Best |
| 2 | Safari 17+ | sahpool VFS fallback | Slightly slower |
| 2 | Firefox | OPFS with minor limits | Good |
| 3 | Older browsers | IndexedDB fallback | Slowest, still works |

Tier 1 gets the full experience. Tier 3 still runs the protocol, just slower.
No tier is excluded.

## Where This Fits in the Roadmap

**2.0 (shipped)** — Python split, secure boot, shape constraints doc

**2.x (next)** — Go Lite. Native binary, single-file distribution.
Architectural discipline: `core/` package is pure, no `net/http`, no
`database/sql` directly. Everything I/O behind interfaces. This is the
groundwork for 3.0.

**3.0 (this document)** — Browser node. Compile the same `core/` to WASM,
back it with SQLite WASM + OPFS, run it in a dedicated Worker. A browser
tab becomes a full elastik node that can sync with other nodes over WebRTC
using the same protocol code the server runs.

## Pre-3.0 Validation Spikes

Three spikes to run before committing to 3.0. Each ~1 day:

1. **TinyGo vs Go bundle size** — compile `core/` with both. Decide which
   toolchain the WASM target uses. TinyGo gives 10× smaller output but loses
   parts of stdlib. Need to verify `core/` dependencies fit.

2. **SQLite WASM + OPFS integration** — minimal prototype: open a DB, write
   a row, close, reopen, read it back. Measure binary size and first-write
   latency.

3. **Worker message bridge** — 50 lines of JS glue: main thread
   `postMessage` → Worker → Go WASM → SQLite WASM → OPFS → reply. End-to-end
   latency measurement.

All three green → 3.0 is mechanical translation work.
Any one blocked → redesign before committing.

## The Point

Browser-as-node is not a metaphor anymore. With Go WASM + SQLite WASM +
OPFS + WebRTC, a tab can run the same protocol code as the server, with
the same types, reading the same schema, signed with the same HMAC chain.
No translation layer. No "mostly compatible." Compiler-guaranteed identical.

This is what "AI-native OS in the browser" means at the implementation
level. Not marketing. Load-bearing architecture.
