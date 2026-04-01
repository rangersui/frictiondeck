# Wake-on-LAN + JIT Hardware Scheduling

## Concept
Wake up sleeping machines only when compute is needed, send them back to sleep when idle. The elastik node acts as a power scheduler for GPU servers and other heavy hardware.

## Design

### WoL magic packet

The Wake-on-LAN protocol is simple: a UDP broadcast containing 6 bytes of `0xFF` followed by the target MAC address repeated 16 times. Total packet: 102 bytes.

```python
import socket, struct

def send_wol(mac_address, broadcast="255.255.255.255", port=9):
    """Send a Wake-on-LAN magic packet.

    mac_address: string like "AA:BB:CC:DD:EE:FF"
    """
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16  # 102 bytes

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))
```

### Config world: `config-wol`

```json
{
  "gpu-server": {
    "mac": "AA:BB:CC:DD:EE:FF",
    "ip": "192.168.1.50",
    "port": 22,
    "idle_timeout": 600,
    "shutdown_cmd": "ssh user@192.168.1.50 'sudo shutdown -h now'",
    "health_url": "http://192.168.1.50:11434/api/tags"
  }
}
```

- `mac`: target MAC address for WoL packet
- `ip` + `port`: for health polling (is the machine up?)
- `idle_timeout`: seconds of no requests before triggering shutdown
- `shutdown_cmd`: shell command to gracefully shut down the machine
- `health_url`: URL to poll to confirm the machine is awake (e.g. Ollama's API)

### Wake + poll + route cycle

```python
import subprocess, urllib.request, time

_machine_state = {}  # name → {"status": "sleeping"|"waking"|"awake", "last_used": float}

async def wake_and_wait(machine_name, timeout=120):
    """Wake a machine and block until it responds."""
    cfg = _read_wol_config()[machine_name]
    state = _machine_state.get(machine_name, {"status": "sleeping", "last_used": 0})

    if state["status"] == "awake":
        state["last_used"] = time.time()
        return True

    send_wol(cfg["mac"])
    state["status"] = "waking"
    _machine_state[machine_name] = state

    # Poll until health endpoint responds
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(cfg["health_url"], timeout=2)
            state["status"] = "awake"
            state["last_used"] = time.time()
            log_event("wol-log", "machine_woke", {"name": machine_name, "took": time.time() - start})
            return True
        except (urllib.error.URLError, OSError):
            await asyncio.sleep(3)

    log_event("wol-log", "wake_timeout", {"name": machine_name})
    state["status"] = "sleeping"
    return False
```

### Idle detection + shutdown (CRON)

```python
CRON = 60  # check every minute

async def _idle_check():
    """Shut down machines that have been idle too long."""
    config = _read_wol_config()
    for name, cfg in config.items():
        state = _machine_state.get(name)
        if not state or state["status"] != "awake":
            continue
        idle = time.time() - state["last_used"]
        if idle > cfg["idle_timeout"]:
            # Graceful shutdown
            try:
                subprocess.run(cfg["shutdown_cmd"], shell=True, timeout=10)
                state["status"] = "sleeping"
                log_event("wol-log", "machine_shutdown", {"name": name, "idle_seconds": idle})
            except Exception as e:
                log_event("wol-log", "shutdown_failed", {"name": name, "error": str(e)})

CRON_HANDLER = _idle_check
```

### Plugin route

Other plugins (e.g. a model inference proxy) call `wake_and_wait()` before forwarding requests:

```python
async def handle_wake(method, body, params):
    data = json.loads(body)
    machine = data.get("machine", "gpu-server")
    ok = await wake_and_wait(machine)
    return {"status": "awake" if ok else "timeout", "machine": machine}

ROUTES = {"/proxy/wol/wake": handle_wake}
```

## Implementation estimate
- ~40 lines for WoL packet + health polling
- ~20 lines for idle CRON handler
- ~15 lines for plugin route and config reading
- Dependencies: none beyond stdlib (socket, subprocess, urllib)
- Worlds: `config-wol` (machine definitions), `wol-log` (audit via log_event)

## Trigger
When you have a GPU server (or any heavy machine) that should not run 24/7 but needs to be available on demand. Typical case: a desktop with an RTX card that serves Ollama, woken up only when an LLM request comes in.

## Related
- CRON system in server.py: `_cron_tasks`, `CRON` + `CRON_HANDLER` fields
- `log_event()` in server.py: HMAC-chained audit for wake/shutdown events
- `config-*` worlds: `config-wol` follows the standard config convention
- Plugin route system: other plugins call `/proxy/wol/wake` via `_call()` before forwarding compute requests
