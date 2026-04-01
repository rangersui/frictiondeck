# IP Whitelist + Network Awareness

## Concept
Auto-detect the current network context (home vs. public) and adjust security posture accordingly -- relaxed auth on trusted networks, strict rejection on unknown ones.

## Design

### Network detection

On startup and periodically (CRON), the plugin reads the machine's local IP and compares it against known trusted subnets stored in `config-lockdown` world.

```python
import socket

def _get_local_ip():
    """Get the local IP without making an external request."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # doesn't actually send anything
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def _detect_network(local_ip, trusted_subnets):
    """Determine if current network is trusted.

    trusted_subnets example: ["192.168.1.0/24", "10.0.0.0/8"]
    """
    from ipaddress import ip_address, ip_network
    addr = ip_address(local_ip)
    for subnet in trusted_subnets:
        if addr in ip_network(subnet, strict=False):
            return "home"
    return "public"
```

### Config world: `config-lockdown`

Stored as JSON in `config-lockdown` world's `stage_html`:

```json
{
  "trusted_subnets": ["192.168.1.0/24"],
  "whitelist_ips": ["192.168.1.50", "192.168.1.100"],
  "public_mode": "reject_all",
  "home_mode": "relaxed"
}
```

- `trusted_subnets`: CIDR ranges that qualify as "home"
- `whitelist_ips`: specific IPs allowed even in public mode
- `public_mode`: `"reject_all"` (default) or `"auth_only"` (require token, no relaxation)
- `home_mode`: `"relaxed"` (skip token check for GET+POST) or `"normal"` (standard auth)

### Auth middleware override

```python
_current_mode = "public"  # updated by CRON

async def lockdown_middleware(scope, path, method):
    if _current_mode == "home" and _config["home_mode"] == "relaxed":
        return True  # home network, all requests allowed

    # Public mode: only whitelisted IPs
    client_ip = scope.get("client", ("unknown", 0))[0]
    if client_ip in _config["whitelist_ips"]:
        return await _original_auth(scope, path, method)

    if _config["public_mode"] == "reject_all":
        log_event("security-log", "lockdown_rejected", {"ip": client_ip, "path": path})
        return False

    # Fall through to normal auth
    return await _original_auth(scope, path, method)
```

### CRON: periodic network check

```python
CRON = 30  # check every 30 seconds

async def _check_network():
    global _current_mode
    local_ip = _get_local_ip()
    config = _read_lockdown_config()
    new_mode = _detect_network(local_ip, config.get("trusted_subnets", []))
    if new_mode != _current_mode:
        log_event("security-log", "network_change", {
            "from": _current_mode, "to": new_mode, "local_ip": local_ip
        })
        _current_mode = new_mode

CRON_HANDLER = _check_network
```

### Container awareness

Interacts with the existing `IN_CONTAINER` detection in server.py. Inside containers, the local IP is always a Docker/Podman bridge address -- the plugin should read the container's gateway or trust environment variables instead:

```python
IN_CONTAINER = os.path.exists("/.dockerenv") or os.getenv("CONTAINER") == "1"

if IN_CONTAINER:
    # Trust the host network config, not the container's virtual IP
    _current_mode = os.getenv("ELASTIK_NETWORK_MODE", "public")
```

## Implementation estimate
- ~50 lines Python as a plugin
- Dependencies: `ipaddress` (stdlib), `socket` (stdlib)
- One new world: `config-lockdown`
- Writes to existing `security-log` world via `log_event()`

## Trigger
When deploying on mobile devices (laptops, tablets) that move between home WiFi and public/cellular networks. Also relevant for travel setups where the server runs on a device that connects to hotel/airport WiFi.

## Related
- `auth.py` plugin: provides the AUTH_MIDDLEWARE that lockdown wraps or overrides
- `IN_CONTAINER` detection in server.py (line 21)
- `config-*` worlds: convention for system configuration
- `log_event()` in server.py: HMAC-chained audit trail
- `honeypot.md`: lockdown and honeypot are complementary -- lockdown is the first gate, honeypot catches what gets through
