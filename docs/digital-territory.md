# Digital Territory — WiFi as Physical Boundary

## Concept
Your WiFi coverage area is your digital territory. Devices are identified by MAC address, and behavior adapts to physical proximity.

## Design

### MAC registry

A `config-territory` world stores known devices as JSON. Each MAC address maps to a trust level and metadata.

```json
{
  "devices": {
    "aa:bb:cc:dd:ee:01": {
      "name": "chen-laptop",
      "trust": "owner",
      "first_seen": "2024-01-15T10:00:00",
      "last_seen": "2024-06-01T08:30:00"
    },
    "aa:bb:cc:dd:ee:02": {
      "name": "living-room-tablet",
      "trust": "household",
      "first_seen": "2024-03-01T12:00:00"
    }
  },
  "unknown_policy": "captive_portal"
}
```

Trust levels:
- `owner` — full read/write, admin access, no auth required
- `household` — read/write, no admin, no auth required
- `guest` — read only, captive portal on first connect
- `blocked` — all requests rejected

### Device detection

On each incoming request, extract the client's IP and resolve it to a MAC address via ARP table lookup:

```python
import subprocess, re, os

def get_mac_for_ip(ip):
    """Look up MAC address from OS ARP table."""
    if os.name == 'nt':
        out = subprocess.check_output(["arp", "-a", ip]).decode()
    else:
        out = subprocess.check_output(["arp", "-n", ip]).decode()
    match = re.search(r'([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}', out)
    return match.group(0).lower().replace('-', ':') if match else None

def get_trust_level(ip, territory_config):
    mac = get_mac_for_ip(ip)
    if not mac:
        return "guest"
    device = territory_config["devices"].get(mac)
    if not device:
        return territory_config.get("unknown_policy", "guest")
    return device["trust"]
```

### Integration with auth.py

The territory check runs before token-based auth. Known devices skip token verification entirely.

```python
# In auth middleware
async def auth_middleware(scope, path, method):
    client_ip = scope.get("client", ("127.0.0.1", 0))[0]
    trust = get_trust_level(client_ip, load_territory_config())

    if trust == "blocked":
        return False
    if trust == "owner":
        return True  # full access, no token needed
    if trust == "household":
        return method == "GET" or not path.startswith("/admin/")
    # "guest" falls through to existing auth logic
    ...
```

### Spatial awareness — RSSI as distance proxy

WiFi signal strength (RSSI) provides a rough distance estimate. Devices physically closer to the access point can receive higher trust.

```python
# Linux: parse iwinfo or iw station dump
# This is AP-side — requires running on the WiFi router or AP
def get_rssi_for_mac(mac):
    """Returns RSSI in dBm. Closer to 0 = stronger = closer."""
    out = subprocess.check_output(["iw", "dev", "wlan0", "station", "dump"]).decode()
    # Parse station blocks for matching MAC
    # signal: -45 dBm → close, -80 dBm → far
    ...
```

RSSI thresholds (approximate):
- `-30` to `-50` dBm: same room (high trust boost)
- `-50` to `-70` dBm: same building (normal trust)
- `-70` to `-90` dBm: edge of coverage (reduced trust)

This is informational, not a hard security boundary. RSSI is easily spoofed. It provides ambient awareness, not access control.

### Territory federation

Two overlapping WiFi networks, each running elastik, can share worlds via sync.py. The shared coverage area becomes a larger territory.

```
Network A (home)          Network B (neighbor)
  elastik:3004              elastik:3004
       ↕ sync.py (whitelisted worlds)

  Combined territory: shared worlds visible on both networks
```

Federation config in `config-endpoints`:
```json
{
  "neighbor": {
    "url": "http://192.168.1.100:3004",
    "token": "shared-secret",
    "federation": true
  }
}
```

Federated peers share their `config-territory` device lists. A device known to either network is recognized on both.

## Implementation estimate
- MAC lookup function: ~15 lines (cross-platform, Windows + Linux)
- Territory config world schema: ~10 lines
- Auth middleware integration: ~20 lines delta to auth.py
- RSSI parsing (Linux only): ~25 lines
- Federation territory merge: ~20 lines
- Dependencies: none (uses OS `arp` command, `iw` for RSSI on Linux)

## Trigger
When running elastik as a household or office ambient system where multiple devices connect throughout the day. Specifically relevant when the captive portal (captive-portal.md) is active and unknown devices need a guest experience.

## Related
- captive-portal.md — guest device landing page
- auth.py — current token-based auth middleware
- sync.py — peer sync for territory federation
- `config-territory` — new config world for device registry
- `config-endpoints` — existing peer configuration
