# P2P Security — Threat Model for Local Network Sync

## Concept
Threat model for elastik P2P synchronization on local networks, covering attack vectors and defenses.

## Design

### Attack vectors

**1. ARP spoofing (MITM)**
An attacker on the same LAN poisons ARP tables to intercept traffic between two elastik peers. All sync payloads (world content, version numbers, tokens) are visible and modifiable in transit.

```
Peer A  ──[ARP poisoned]──  Attacker  ──  Peer B
         HTTP world sync is plaintext
```

**2. Passive sniffing**
Without ARP spoofing, any device on the same network segment can capture broadcast/multicast traffic. sync.py currently uses HTTP — world content is readable by any packet sniffer on the LAN.

**3. Supply chain — malicious plugin via sync**
If sync.py is extended to sync plugins (not just worlds), an attacker who compromises one peer can inject a malicious plugin that propagates to all peers. The `exec` and `fs` plugins are already flagged as dangerous in server.py (`_DANGEROUS_PLUGINS`), but a crafted plugin could bypass this.

**4. DNS poisoning / peer URL redirect**
Peer URLs are stored in `config-endpoints` world as JSON. If an attacker can modify DNS responses, a peer URL like `http://trustedpeer.local:3004` resolves to the attacker's IP.

**5. Version rollback**
sync.py uses "high version wins" conflict resolution. An attacker with write access can set a world's version to `MAX_INT`, making their content permanently win all future syncs.

### Defenses

**HMAC chain verification (existing)**
Every event logged via `log_event()` includes an HMAC chained to the previous event's HMAC. If an attacker tampers with a synced event, the chain breaks:

```python
# Verification: recompute chain from first event
def verify_chain(events, key):
    prev = ""
    for e in events:
        expected = hmac.new(key, (prev + e["payload"]).encode(),
                           hashlib.sha256).hexdigest()
        if expected != e["hmac"]:
            return False, e["id"]  # tampered event
        prev = e["hmac"]
    return True, None
```

sync.py should call this on received events before applying them.

**TLS between peers**
sync.py should use HTTPS for all peer communication. bus.py already generates self-signed certs for WebGPU (secure context requirement). The same cert generation can be reused:

```python
# In sync.py — upgrade peer URLs to HTTPS
def sync_to_peer(peer_url, payload):
    if not peer_url.startswith("https://"):
        raise SecurityError("sync requires TLS")
    req = urllib.request.Request(peer_url, data=payload,
        headers={"X-Auth-Token": peer_token})
    # Accept self-signed certs from known peers (pinned)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile="peer_certs/peer-a.crt")
    urllib.request.urlopen(req, context=ctx)
```

**Plugin signature verification**
Store SHA256 hashes of approved plugin source code in `config-approved-hashes` world. Before loading any plugin, verify its hash:

```python
# In plugin loader
import hashlib
source = Path(f"plugins/available/{name}.py").read_bytes()
actual = hashlib.sha256(source).hexdigest()
approved = read_approved_hashes()  # from config-approved-hashes world
if actual not in approved:
    log_event("default", "plugin_rejected", {"name": name, "hash": actual})
    raise PluginError(f"plugin {name} hash mismatch")
```

**Peer certificate pinning**
On first sync with a new peer, store the peer's TLS certificate fingerprint in `config-peer-certs` world. On subsequent connections, reject if the fingerprint changes (TOFU — trust on first use).

**Version ceiling**
Add a maximum version delta per sync cycle. If a received version is more than 1000 ahead of local version, reject it as suspicious.

## Implementation estimate
- HMAC chain verification function: ~15 lines
- TLS enforcement in sync.py: ~20 lines (reuse bus.py cert generation)
- Plugin hash verification: ~15 lines
- Peer cert pinning: ~25 lines
- Version ceiling check: ~5 lines
- Dependencies: `ssl` (stdlib), `cryptography` (optional, for cert generation — already optional dep of bus.py)

## Trigger
When sync.py is used across untrusted networks, or when syncing between devices not on the same physical network. Immediate priority if elastik is used in a shared office or public WiFi environment.

## Related
- sync.py — current sync implementation (HTTP, no TLS)
- HMAC chain in `log_event()` (server.py line 71-77)
- `config-endpoints` world (peer URL storage)
- `_DANGEROUS_PLUGINS` set in server.py (line 22)
- bus.py cert generation (line 130 area, fallback to HTTP)
