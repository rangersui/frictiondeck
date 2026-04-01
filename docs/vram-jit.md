# OLLAMA_KEEP_ALIVE Tiered + Model Load/Unload on Demand

## Concept
Keep frequently used models in VRAM, evict rarely used ones. Three-tier keep-alive system based on usage frequency, managed through Ollama's `keep_alive` API parameter.

## Design

### The problem

Ollama's default `OLLAMA_KEEP_ALIVE` is global -- all models get the same timeout. On a machine with 8-12GB VRAM running 3-4 models, this means either all models stay loaded (VRAM exhaustion) or all models unload too quickly (cold-start latency on every request).

### Three tiers

| Tier | keep_alive | Behavior | Criteria |
|------|-----------|----------|----------|
| Hot | `-1` (forever) | Always in VRAM | Used 10+ times in last hour |
| Warm | `5m` | Stays loaded 5 min after last use | Used 1-9 times in last hour |
| Cold | `0` (immediate unload) | Loads on request, unloads after response | Not used in last hour |

### Usage tracking

```python
import time, json

_model_usage = {}  # model_name → [timestamp, timestamp, ...]

def _record_usage(model):
    now = time.time()
    if model not in _model_usage:
        _model_usage[model] = []
    _model_usage[model].append(now)
    # Trim entries older than 1 hour
    cutoff = now - 3600
    _model_usage[model] = [t for t in _model_usage[model] if t > cutoff]

def _get_tier(model):
    now = time.time()
    cutoff = now - 3600
    recent = [t for t in _model_usage.get(model, []) if t > cutoff]
    count = len(recent)
    if count >= 10:
        return "hot"
    elif count >= 1:
        return "warm"
    return "cold"

def _keep_alive_value(tier):
    return {"hot": "-1", "warm": "5m", "cold": "0"}[tier]
```

### Ollama API integration

When proxying a request to Ollama, inject the appropriate `keep_alive` value:

```python
import urllib.request, json

OLLAMA_URL = "http://localhost:11434"

def _ollama_generate(model, prompt, keep_alive):
    """Send a generate request with tier-appropriate keep_alive."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "keep_alive": keep_alive,
        "stream": False
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())

async def handle_generate(method, body, params):
    data = json.loads(body)
    model = data.get("model", "")
    _record_usage(model)
    tier = _get_tier(model)
    ka = _keep_alive_value(tier)
    result = _ollama_generate(model, data.get("prompt", ""), ka)
    return {"response": result.get("response", ""), "tier": tier, "keep_alive": ka}
```

### CRON: tier management

A periodic job reviews all tracked models and forcefully unloads any that have dropped to cold tier but might still be occupying VRAM (because a previous warm-tier timeout hasn't expired yet).

```python
CRON = 120  # every 2 minutes

async def _manage_tiers():
    """Demote models and force-unload cold ones."""
    for model, timestamps in list(_model_usage.items()):
        tier = _get_tier(model)
        if tier == "cold":
            _force_unload(model)

    # Persist current state to config-vram-jit for observability
    state = {m: {"tier": _get_tier(m), "count_1h": len(ts)}
             for m, ts in _model_usage.items()}
    c = conn("config-vram-jit")
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
              (json.dumps(state),))
    c.commit()

CRON_HANDLER = _manage_tiers

def _force_unload(model):
    """Tell Ollama to unload a model by sending keep_alive=0."""
    try:
        payload = json.dumps({"model": model, "keep_alive": "0"}).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        # Send a no-op generate with immediate unload
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # model might already be unloaded
```

### Config: `config-vram-jit`

Override default thresholds per model:

```json
{
  "thresholds": {
    "hot_min_uses": 10,
    "warm_min_uses": 1,
    "window_seconds": 3600
  },
  "overrides": {
    "llama3:8b": "hot",
    "codellama:7b": "warm"
  }
}
```

The `overrides` field lets you pin a model to a specific tier regardless of usage (e.g., your primary chat model is always hot).

## Implementation estimate
- ~50 lines Python for usage tracking and tier logic
- ~20 lines for Ollama API calls
- ~15 lines for CRON handler
- Dependencies: none beyond stdlib (urllib, json)
- Worlds: `config-vram-jit` (tier config + overrides + current state)

## Trigger
When running multiple Ollama models on a machine with limited VRAM (8-24GB). Not needed if you only use one model or have enough VRAM for everything.

## Related
- CRON system in server.py: `_cron_tasks`, `CRON` + `CRON_HANDLER`
- `config-*` worlds: `config-vram-jit` for tier thresholds and overrides
- `wol.md`: can combine with WoL -- wake the GPU machine, load the model, run inference, unload model, sleep machine
