# Honeypot + Tarpit + Auto-LOCKDOWN

## Concept
Two-layer detection: (1) port honeypots on common attack targets (22/23/80/8080) that instantly flag scanners, and (2) auth failure tarpit with exponential delays and auto-lockdown after threshold.

## Design

### Layer 1: Port honeypots

Listen on ports that elastik never uses but attackers always probe: 22 (SSH), 23 (Telnet), 80 (HTTP), 8080 (alt HTTP). Any connection attempt = instant attacker signature. No legitimate user will ever connect to these.

```python
HONEYPOT_PORTS = [22, 23, 80, 8080]

async def honeypot_listener(port):
    """Accept connection, log IP, close. That's it."""
    server = await asyncio.start_server(
        lambda r, w: _honeypot_hit(w, port), '0.0.0.0', port)

async def _honeypot_hit(writer, port):
    ip = writer.get_extra_info('peername')[0]
    _blacklist.add(ip)
    log_event("security-log", "honeypot_trip", {"ip": ip, "port": port})
    writer.close()
```

One connection to a honeypot port → immediate blacklist. No tarpit, no warnings. Legitimate users never touch these ports.

### Layer 2: Auth failure tarpit

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
