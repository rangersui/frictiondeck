# Honeypot + Tarpit + Auto-LOCKDOWN

## Concept
Detect unauthorized access attempts, slow them down with exponential delays (tarpit), and auto-lock endpoints after a failure threshold.

## Design

Failed auth attempts are tracked per IP in a `security-log` world. Each failure increments a counter and doubles the response delay. After N failures from a single IP, that IP is blacklisted and all requests are rejected until manual review.

### Data structure (in-memory, persisted to `config-honeypot` world)

```python
# In auth.py plugin or as a standalone honeypot.py plugin
_fail_counter = {}  # ip → {"count": int, "first_seen": float, "last_seen": float}
TARPIT_BASE = 1       # seconds
TARPIT_MAX = 64        # cap at 64s
LOCKDOWN_THRESHOLD = 10  # failures before auto-blacklist
```

### Tarpit delay

```python
import asyncio

async def tarpit_delay(ip):
    """Exponential backoff before responding to a failed auth attempt."""
    info = _fail_counter.get(ip, {"count": 0})
    delay = min(TARPIT_BASE * (2 ** info["count"]), TARPIT_MAX)
    await asyncio.sleep(delay)
```

### Auth middleware integration

The honeypot wraps the existing `AUTH_MIDDLEWARE` in auth.py. On every failed POST auth check, it records the failure and applies tarpit delay before returning 403.

```python
async def honeypot_middleware(scope, path, method):
    ip = _extract_ip(scope)

    # Check blacklist first
    if ip in _blacklist:
        await tarpit_delay(ip)
        return False

    # Run real auth
    ok = await _original_auth(scope, path, method)

    if not ok:
        _record_failure(ip)
        await tarpit_delay(ip)
        if _fail_counter[ip]["count"] >= LOCKDOWN_THRESHOLD:
            _blacklist.add(ip)
            log_event("security-log", "auto_lockdown", {
                "ip": ip,
                "failures": _fail_counter[ip]["count"],
            })
    else:
        # Successful auth clears the counter
        _fail_counter.pop(ip, None)

    return ok

def _extract_ip(scope):
    """Pull client IP from ASGI scope."""
    client = scope.get("client", ("unknown", 0))
    return client[0]

def _record_failure(ip):
    import time
    now = time.time()
    if ip not in _fail_counter:
        _fail_counter[ip] = {"count": 0, "first_seen": now, "last_seen": now}
    _fail_counter[ip]["count"] += 1
    _fail_counter[ip]["last_seen"] = now
```

### Persistence

A CRON handler (every 60s) persists the current fail counter and blacklist to `config-honeypot` world as JSON. On startup, the plugin reads it back. This survives server restarts.

```python
CRON = 60

async def _cron_persist():
    c = conn("config-honeypot")
    state = json.dumps({"fails": _fail_counter, "blacklist": list(_blacklist)})
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (state,))
    c.commit()

CRON_HANDLER = _cron_persist
```

### Alert mechanism

Every auto-lockdown event writes to `security-log` world via `log_event()`. The HMAC chain ensures the log cannot be tampered with retroactively. A renderer (`renderer-security`) can display these events in the UI.

## Implementation estimate
- ~60 lines Python as a plugin
- Dependencies: none (uses asyncio.sleep, existing log_event, existing CRON system)
- One new world: `config-honeypot` for persistent state
- One new world: `security-log` for audit trail (already used by convention)

## Trigger
When elastik is exposed to the public internet -- either directly or through a tunnel. Not needed for localhost-only or home LAN deployments.

## Related
- `auth.py` plugin: provides AUTH_MIDDLEWARE that this wraps
- `log_event()` in server.py: HMAC-chained audit log
- CRON system in server.py: `_cron_tasks` dict, `CRON` + `CRON_HANDLER` plugin fields
- `config-*` worlds: convention for system configuration stored in SQLite
