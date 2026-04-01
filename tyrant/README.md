# elastik tyrant mode

The server becomes a dumb pipe. The browser becomes everything.

```
bus.py     — ~80 lines. HTTPS + SQLite. No plugins, no logic, no opinions.
index.html — Fat client. WebLLM inference, world management, renderer bridge, AI panel.
```

## Four deployment modes

| Mode | Server | Browser | Compute | Storage | Use case |
|------|--------|---------|---------|---------|----------|
| **Normal** | full server.py (plugins, HMAC, auth) | thin renderer | server-side (Ollama) | server SQLite | Production |
| **Tyrant** | bus.py (SQLite read/write only) | WebLLM + full UI | browser GPU | bus SQLite | GPU device (Tesla, gaming laptop) |
| **Parasite** | any HTTP+KV (CF Worker, VPS, friend's Pi) | WebLLM + full UI | browser GPU | remote KV | Zero install |
| **Blackboard** | none | index.html from file/GitHub Pages | browser GPU | `?bus=` remote | Truly nothing |

These aren't four products. They're four points on a continuous spectrum. You slide between them based on what devices and network you have right now.

## Quick start

```bash
pip install cryptography   # one-time, for self-signed cert
python tyrant/bus.py       # starts HTTPS on :3005
```

Open `https://<ip>:3005` on any device. Accept the self-signed cert warning once.

### Cross-device

```
Machine A (storage):  python tyrant/bus.py          → HTTPS :3005
Machine B (display):  browser → https://A-ip:3005   → WebGPU renders + WebLLM computes
```

One bus.py. Any number of browsers. bus.py is the hard drive. Browsers are the monitors.

### `?bus=` parameter

Only needed when index.html and data are NOT on the same machine:

```
# Normal: HTML and data from same bus.py — no parameter needed
https://192.168.1.100:3005

# Parasite: HTML from GitHub Pages, data from your machine
https://user.github.io/elastik/?bus=https://192.168.1.100:3005

# Blackboard: HTML from local file, data from anywhere
file:///usb/index.html?bus=https://your-vps.com:3005
```

If you're opening bus.py's URL directly, you don't need `?bus=`. 99% of the time.

## Security model

**Write permission = code execution permission.**

World content is HTML+JS. That's not XSS — that's the feature. The renderer IS JavaScript. You wrote it. You trust it.

Defense is at the write gate, not the render gate:

- `ELASTIK_TOKEN` env var → POST requires `Authorization: Bearer <token>`
- No token set → open writes (local dev only)
- iframe `sandbox="allow-scripts allow-popups"` → no `allow-same-origin` → world JS can't escape to parent page
- Bridge (`__elastik.fetch/action/sync`) → controlled postMessage channel → parent decides what to allow

## Gotchas

### HTTPS required for remote WebGPU

WebGPU requires a [secure context](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts). `localhost` counts, but `http://192.168.x.x` does not.

bus.py auto-generates a self-signed cert on first run via the `cryptography` package. No `cryptography` and no `openssl` → falls back to HTTP (WebGPU only on localhost).

### Storage persist before model download

WebLLM caches models in Cache API. Chrome limits storage for non-persistent origins. Before loading a model:

```js
// browser console, one-time per origin
await navigator.storage.persist()  // must return true
```

Without this, model downloads fail at ~30% with `Quota exceeded`.

### Model selection

| Model | Download | VRAM | Quality |
|-------|----------|------|---------|
| `Qwen2.5-0.5B-Instruct-q4f16_1-MLC` | ~300MB | ~1GB | Basic |
| `Qwen2.5-1.5B-Instruct-q4f16_1-MLC` | ~800MB | ~2GB | Better |
| `Qwen3-1.7B-q4f16_1-MLC` | ~900MB | ~2GB | Best small |

Default is 0.5B. Change in AI panel: `/webllm Qwen2.5-1.5B-Instruct-q4f16_1-MLC`

## AI panel commands

| Command | Action |
|---------|--------|
| `/webllm` | Load default WebLLM model on GPU |
| `/webllm <model-id>` | Load specific model |
| `/api <url> <key>` | Use remote API for inference |
| `/write <content>` | Write to current world |
| `/read` | Read current world content |

## Bridge API (inside iframe)

Renderer worlds use the same `__elastik` bridge as Normal mode:

```js
__elastik.fetch('/other-world/read')    // read any world
__elastik.action('/proxy/route', body)  // call plugin routes
__elastik.sync(data)                    // write back to current world
__elastik.result(data)                  // return JS execution result
__elastik.clear()                       // clear pending state
__elastik._w                            // current world name
```

All calls go through postMessage → parent page → bus.py. iframe cannot bypass sandbox.

## Architecture

```
Compute: wherever GPU is
  ├── Browser WebLLM (local GPU)
  ├── Remote API (Claude, OpenAI, Ollama)
  └── Both can coexist, switch on the fly

Bus: wherever SQLite is
  ├── bus.py on a phone
  ├── bus.py on a VPS
  ├── CF Worker + D1
  └── Anything that does GET/POST + stores strings

Render: wherever a browser is
  ├── Tesla center screen
  ├── iPad, laptop, phone
  └── Multiple simultaneously
```

Three layers, fully independent, any combination. The protocol doesn't care who computes, who stores, or who displays. It only defines the interface between them.

## TODO

- [ ] Navigate handler — iframe `window.location` interception
- [ ] Action whitelist — front-end equivalent of config-actions
- [ ] AI approve write — WebLLM generates → user confirms → writes to world
- [ ] Front-end plugins — load JS from plugin-* worlds, hot-swap capabilities
