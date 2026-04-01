# Elastic Compute — Adaptive Frontend Tiers

## Concept
The system adapts its compute strategy based on available hardware, automatically and continuously.

## Design

### Three tiers detected at runtime

**Tier 3 — Full (WebGPU available)**
The browser has GPU access. Load WebLLM, run inference locally, POST results to the bus. The server is just storage.

```javascript
// Detection (already in tyrant/index.html line 90)
if (navigator.gpu) {
    const adapter = await navigator.gpu.requestAdapter({
        powerPreference: 'high-performance'
    });
    if (adapter) {
        tier = 3;
        // Load WebLLM, run inference in browser
        const webllm = await import('https://esm.run/@mlc-ai/web-llm');
        engine = await webllm.CreateMLCEngine(model, { ... });
    }
}
```

In Tier 3, the browser is the compute engine. It reads world state, runs LLM inference, and writes results back. The server (bus.py or server.py) is a SQLite proxy.

**Tier 2 — Medium (JS but no WebGPU)**
The browser can run JavaScript but lacks GPU access. Inference is delegated to server-side Ollama or a remote API. The browser handles rendering and interaction.

```javascript
// Detection
if (!navigator.gpu && window.fetch) {
    tier = 2;
    // Inference via server-side Ollama
    async function infer(prompt) {
        const r = await fetch('/proxy/ollama/generate', {
            method: 'POST',
            body: JSON.stringify({ prompt })
        });
        return (await r.json()).response;
    }
}
```

**Tier 1 — Minimal (basic HTML)**
No JavaScript or severely constrained environment (old browser, terminal-based, curl). The server renders everything. The client is a pure display surface.

```python
# Server-side rendering (server.py already does this for index.html)
# Tier 1 clients get fully rendered HTML — no JS execution expected
# The existing /{name}/read endpoint returns raw stage_html
# A Tier 1 renderer would wrap it in a basic HTML page server-side
```

### Detection logic

```javascript
function detectTier() {
    if (typeof navigator !== 'undefined' && navigator.gpu) return 3;
    if (typeof window !== 'undefined' && window.fetch)     return 2;
    return 1;
}
```

Tier detection runs on every page load. A device can change tiers between sessions (GPU driver update, browser flag change, battery saver mode). The same device might be Tier 3 on power and Tier 2 on battery.

### The bus does not change

All three tiers use the same HTTP API:
```
GET  /{name}/read    →  get world state
POST /{name}/write   →  set world state
POST /{name}/append  →  append to world
```

The protocol is tier-agnostic. A Tier 3 browser and a Tier 1 curl script read the same world through the same endpoint. Only who does the computation changes.

### Tier negotiation flow

```
Browser loads page
  → detectTier()
  → Tier 3: import WebLLM, request GPU adapter
  → Tier 2: check for /proxy/ollama endpoint availability
  → Tier 1: render server-provided HTML as-is

  If Tier 3 fails (GPU out of memory, model too large):
    → downgrade to Tier 2 automatically
    → addMsg('system', 'GPU failed, falling back to server-side inference')

  If Tier 2 fails (no Ollama available):
    → downgrade to Tier 1
    → display-only mode, no local inference
```

### Tier indicator in UI

```javascript
const tierLabels = {
    3: 'GPU local',
    2: 'server inference',
    1: 'display only'
};
document.getElementById('tier-badge').textContent = tierLabels[tier];
```

The user always knows which tier they are on. This is not hidden or automatic in a confusing way — it is visible in the UI.

### Deployment spectrum (not architecture)

Traditional apps have a fixed deployment model: client-server, serverless, peer-to-peer. elastik is a spectrum:

```
← Tier 1 ──────────── Tier 2 ──────────── Tier 3 →
  server does all      server infers        browser does all
  client displays      client renders       server stores

  curl on a Pi         laptop + Ollama      gaming PC + WebGPU
  IoT display          phone browser        Tesla screen
```

Your position on the spectrum can change every second. A phone on WiFi might be Tier 2 (server inference available). The same phone on cellular with no server might fall back to Tier 1. If a future phone gets WebGPU, it moves to Tier 3.

## Implementation estimate
- Tier detection + negotiation: ~30 lines JS (partially exists in tyrant/index.html)
- Automatic downgrade logic: ~20 lines JS
- Tier 1 server-side render wrapper: ~15 lines Python
- Tier badge UI: ~5 lines JS
- No new dependencies. WebLLM is loaded dynamically via ESM import.

## Trigger
Already partially implemented in tyrant/index.html (GPU detection at line 88-101, WebLLM loading at line 311-324). This document describes the full three-tier vision including automatic downgrade and Tier 1 server rendering. Implement the missing pieces when deploying to mixed-capability devices (phone + laptop + Pi on the same network).

## Related
- tyrant/index.html — GPU detection (line 88-101), WebLLM loading (line 311-324)
- tyrant/bus.py — minimal server for Tier 3 mode
- server.py — full server for Tier 1 and Tier 2 mode
- Ollama proxy plugin — server-side inference for Tier 2
- tyrant/README.md — Normal vs Tyrant vs Parasite deployment modes
