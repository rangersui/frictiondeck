# Tyrant Mode — Compute Inversion Design Philosophy

## Concept
The server shrinks to a dumb string-storage pipe; the browser becomes the operating system. All compute, rendering, and decision-making moves to the client.

## Design

### The key insight

server.py is not a server. It is a bus. It receives strings, stores them in SQLite, returns strings. Who computed them, where, with what model, on what hardware -- the bus does not know and does not care. A world's `stage_html` could be written by a Python plugin, a browser running WebLLM, a cURL command, or a human typing. The bus stores it and serves it. That is its entire job.

### Three-layer separation

The architecture separates into three independent layers:

```
  COMPUTE              BUS                RENDER
  (who thinks)         (who stores)       (who shows)

  Ollama          ←→   server.py     ←→   index.html
  WebLLM          ←→   bus.py        ←→   tyrant/index.html
  Claude API      ←→   (SQLite)      ←→   iframe sandbox
  Human typing
```

Each layer is independently replaceable:
- **Compute** can be any LLM, any API, or a human. The bus never calls a model directly.
- **Bus** can be server.py (full features, plugins, HMAC) or bus.py (zero plugins, raw read/write). Both speak the same protocol: `/{world}/read`, `/{world}/write`, `/stages`.
- **Render** can be index.html (server-side features, plugin UI) or tyrant/index.html (self-contained, all logic in the browser).

### The `?bus=` parameter

tyrant/index.html connects to any elastik-compatible bus via a URL parameter:

```
tyrant/index.html?bus=https://192.168.1.50:3005
tyrant/index.html?bus=http://localhost:3004
```

This means one browser tab can control a remote machine's data. The bus URL is the only configuration. The client discovers available worlds via `GET /stages` and reads/writes via the standard endpoints.

### The `__elastik.*` bridge

When index.html renders world content inside a sandboxed iframe, the iframe cannot directly access the parent page or make network requests (CSP + sandbox). The bridge provides controlled escape hatches:

```javascript
// Injected into the iframe's sandbox
window.__elastik = {
  read: (world) => fetch(`/${world}/read`).then(r => r.json()),
  write: (world, content) => fetch(`/${world}/write`, {method:'POST', body: content}),
  append: (world, content) => fetch(`/${world}/append`, {method:'POST', body: content}),
  stages: () => fetch('/stages').then(r => r.json()),
  // No exec, no fs, no plugin access — sandbox boundary
};
```

The bridge is the security boundary. It exposes read/write to SQLite worlds and nothing else. The iframe can render arbitrary HTML/JS (that is its job), but it can only affect the system through the bridge's narrow API.

### XSS-as-feature security model

Traditional web apps defend against XSS at the render layer: sanitize HTML, escape output, use CSP to block inline scripts. elastik inverts this.

In elastik, `write = execute`. Any string written to a world is rendered as raw HTML in an iframe. If that HTML contains `<script>`, it runs. This is not a bug. This is the execution model. A world is a program. Writing to it is deploying code.

The defense is therefore at the **write gate**, not the render gate:
- auth.py controls who can write (token check)
- HMAC chain records what was written and when (audit trail)
- The iframe sandbox prevents writes from escalating to system access
- The `__elastik` bridge controls what the rendered code can do

If you control writes, you control execution. If you lose control of writes, you have lost control of the system. This is why auth.py exists and why the HMAC chain is append-only.

### Four deployment modes

These are not separate products. They are points on a spectrum of where compute happens:

```
  Server-heavy ←————————————————————————→ Client-heavy

  NORMAL        PARASITE      BLACKBOARD      TYRANT
```

**Normal** (`server.py` + `index.html`):
Server runs plugins, CRON jobs, LLM proxies. Browser renders results. Traditional client-server. The server is smart, the client is a viewer.

**Parasite** (`server.py` + `tyrant/index.html`):
Server has plugins, but the browser ignores them. The browser uses its own compute (WebLLM, local JS) and writes results back to the server for storage. The server is a database the parasite feeds on.

**Blackboard** (`bus.py` + `index.html`):
bus.py is a pure read/write pipe with no plugins. Multiple clients (browsers, scripts, other servers) read and write to the same worlds. The bus is a shared blackboard. Coordination happens through convention, not enforcement.

**Tyrant** (`bus.py` + `tyrant/index.html`):
The server is an empty pipe. The browser does everything: LLM inference (WebLLM/WebGPU), UI rendering, state management. The server is a SQLite API that happens to be reachable over HTTP. The browser is the operating system.

### Why bus.py exists separately

bus.py is ~90 lines. It has no plugin system, no HMAC chain, no CRON, no auth middleware hooks. It is server.py with everything removed except `conn()`, `GET /stages`, `/{world}/read`, and `POST /{world}/write`.

This matters because:
1. It can run on extremely constrained hardware (a router, a phone, a Raspberry Pi Zero).
2. It has zero attack surface beyond SQLite writes.
3. It generates its own TLS certificate so WebGPU works on remote clients (browsers require HTTPS for WebGPU).
4. It proves the protocol is simple enough to reimplement in an afternoon.

### WebLLM integration (client-side)

In tyrant mode, the browser loads models directly into GPU memory via WebGPU:

```javascript
// In tyrant/index.html
async function localInference(prompt, model) {
  // WebLLM handles model download, caching, and GPU execution
  const engine = await webllm.CreateMLCEngine(model);
  const reply = await engine.chat.completions.create({
    messages: [{role: "user", content: prompt}],
    temperature: 0.7
  });
  const text = reply.choices[0].message.content;

  // Write result back to the bus for persistence
  await fetch(`${BUS_URL}/${currentWorld}/write`, {
    method: 'POST',
    body: text
  });
  return text;
}
```

The bus never knows an LLM was involved. It received a string and stored it.

### Protocol contract

Any system that implements these three endpoints is an elastik bus:

```
GET  /stages              → [{"name": "...", "version": N, "updated_at": "..."}]
GET  /{world}/read        → {"stage_html": "...", "version": N}
POST /{world}/write       → {"version": N}  (body = raw content)
```

Everything else (plugins, CRON, auth, HMAC, events, pending/result/sync/clear) is server.py features layered on top of the bus protocol. They are optional. The bus protocol is the only contract.

## Implementation estimate
bus.py already exists at ~90 lines. tyrant/index.html already exists. This document describes the design philosophy, not new code. The "implementation" is understanding why the pieces are shaped the way they are.

Future work that extends this design:
- WebRTC data channels as an alternative bus transport (~50 lines JS)
- Service Worker as an offline bus (intercept fetch, read/write to IndexedDB) (~40 lines JS)
- WASM SQLite in the browser as a local bus with no server at all (~30 lines JS wrapper)

## Trigger
Already partially implemented. The design philosophy applies whenever deciding where to put new functionality: if it can run in the browser, it should run in the browser. The server should only do things that require persistence, network access, or hardware the browser cannot reach.

## Related
- `tyrant/bus.py`: the minimal bus implementation (~90 lines)
- `tyrant/index.html`: the fat client that treats the bus as a dumb pipe
- `server.py`: the full bus + plugin system + CRON + HMAC
- `index.html`: the traditional thin client
- `auth.py` plugin: write-gate security (controls who can write = who can execute)
- HMAC chain in server.py: audit trail for all writes (the bus remembers)
- `__elastik` bridge: sandbox boundary between rendered worlds and the system
