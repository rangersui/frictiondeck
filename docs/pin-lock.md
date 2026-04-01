# Lock Screen PIN + Wrong Attempts Self-Destruct

## Concept
A browser-side PIN gate displayed before any world content is rendered. Too many wrong attempts triggers a server-side wipe of sensitive worlds.

## Design

### Security model

The PIN is explicitly a speed bump, not cryptographic security. Real security comes from ELASTIK_TOKEN (auth.py) and the HMAC audit chain. The PIN prevents casual physical access -- someone picks up your tablet, they see a PIN screen instead of your data. It does not protect against a determined attacker with access to the filesystem.

### PIN storage: `config-pin` world

```json
{
  "pin_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "max_attempts": 5,
  "wipe_worlds": ["default", "notes", "journal"],
  "enabled": true
}
```

The PIN itself is never stored. Only `SHA256(pin)` is persisted. The `wipe_worlds` list defines which worlds get cleared on self-destruct. Config worlds are never wiped (you need them to re-setup the system).

### Frontend: PIN screen

Injected at the top of `index.html` (and `tyrant/index.html`) before any world rendering logic runs:

```javascript
// PIN gate — runs before anything else
(function() {
  const PIN_KEY = '__elastik_pin_attempts';
  const config = null; // fetched from /config-pin/read

  async function checkPin() {
    const res = await fetch('/config-pin/read');
    const data = await res.json();
    if (!data.stage_html || !data.stage_html.trim()) return true; // no PIN set
    const cfg = JSON.parse(data.stage_html);
    if (!cfg.enabled) return true;

    const attempts = parseInt(localStorage.getItem(PIN_KEY) || '0');
    if (attempts >= cfg.max_attempts) {
      await selfDestruct();
      return false;
    }

    const pin = prompt('PIN:');
    if (!pin) return false;

    const hash = await sha256(pin);
    if (hash === cfg.pin_hash) {
      localStorage.setItem(PIN_KEY, '0'); // reset counter
      return true;
    } else {
      localStorage.setItem(PIN_KEY, String(attempts + 1));
      if (attempts + 1 >= cfg.max_attempts) {
        await selfDestruct();
      }
      return false;
    }
  }

  async function sha256(str) {
    const buf = await crypto.subtle.digest('SHA-256',
      new TextEncoder().encode(str));
    return Array.from(new Uint8Array(buf))
      .map(b => b.toString(16).padStart(2, '0')).join('');
  }

  async function selfDestruct() {
    // Tell server to wipe sensitive worlds
    await fetch('/config-pin/write', {
      method: 'POST',
      headers: {'X-Auth-Token': '__WIPE_SIGNAL__'},
      body: '__DESTRUCT__'
    });
    document.body.innerHTML = '<h1 style="color:red;text-align:center;margin-top:40vh">WIPED</h1>';
  }

  checkPin().then(ok => {
    if (!ok) document.body.innerHTML = '<h1 style="text-align:center;margin-top:40vh">LOCKED</h1>';
  });
})();
```

### Server-side wipe handler

A small plugin (or addition to auth.py) that watches for the wipe signal on `config-pin/write`:

```python
async def handle_pin_write(method, body, params):
    """Intercept writes to config-pin. If body is __DESTRUCT__, wipe listed worlds."""
    if body.decode().strip() == "__DESTRUCT__":
        config = json.loads(
            conn("config-pin").execute(
                "SELECT stage_html FROM stage_meta WHERE id=1"
            ).fetchone()["stage_html"]
        )
        for world_name in config.get("wipe_worlds", []):
            c = conn(world_name)
            c.execute("UPDATE stage_meta SET stage_html='',version=version+1,updated_at=datetime('now') WHERE id=1")
            c.commit()
            log_event(world_name, "pin_wipe", {"trigger": "max_attempts_exceeded"})
        log_event("security-log", "self_destruct", {"worlds_wiped": config.get("wipe_worlds", [])})
        return {"wiped": True}

    # Normal PIN config write (setting/changing PIN)
    # ... standard write logic
```

### Setting a PIN

```javascript
async function setPin(newPin) {
  const hash = await sha256(newPin);
  const config = {
    pin_hash: hash,
    max_attempts: 5,
    wipe_worlds: ['default', 'notes', 'journal'],
    enabled: true
  };
  await fetch('/config-pin/write', {
    method: 'POST',
    body: JSON.stringify(config)
  });
}
```

### Wrong attempts counter

The counter lives in `localStorage`, not on the server. This is intentional:
- Clearing localStorage resets the counter, but that requires developer tools (not casual access).
- A server-side counter would require auth to read, creating a chicken-and-egg problem.
- The threat model is "someone picks up the device," not "someone attacks the API."

## Implementation estimate
- ~30 lines JavaScript in index.html / tyrant/index.html
- ~25 lines Python for the wipe handler (plugin or patch to existing write flow)
- Dependencies: none (uses Web Crypto API in browser, stdlib on server)
- Worlds: `config-pin` (PIN hash + wipe list)

## Trigger
When the device running elastik might be physically accessed by others -- shared tablets, car-mounted devices, demo units. Not needed for a server in a locked room.

## Related
- `index.html`: main frontend, PIN gate injected here
- `tyrant/index.html`: tyrant frontend, same PIN gate
- `config-*` worlds: `config-pin` follows the standard config world convention
- `auth.py` plugin: PIN is layered on top of auth, not a replacement
- `log_event()` in server.py: wipe events are logged in the HMAC chain
