# Pairing Code Key Exchange + DH + Web of Trust

## Concept
Two elastik nodes establish mutual trust by exchanging a short pairing code displayed on screen, deriving a shared secret via Diffie-Hellman, and storing peer public keys for future authenticated sync.

## Design

### Pairing flow

1. **Node A** generates a DH keypair and a 6-digit pairing code. Displays the code on screen. Stores the pending pairing state in memory.
2. **User** reads the code from Node A's screen, types it into Node B's pairing UI.
3. **Node B** generates its own DH keypair, sends its public key + the pairing code to Node A via `POST /proxy/pairing/complete`.
4. **Node A** verifies the pairing code matches, responds with its own public key.
5. Both nodes derive the shared secret from DH, store each other's public key in `config-peers` world.

### Key generation (stdlib only)

```python
import hashlib, secrets, json

# Using a pre-agreed safe prime for DH (RFC 3526 Group 14, 2048-bit)
DH_P = 0xFFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74...  # truncated
DH_G = 2

def _generate_keypair():
    private = secrets.randbelow(DH_P - 2) + 1
    public = pow(DH_G, private, DH_P)
    return private, public

def _derive_shared(private, peer_public):
    shared = pow(peer_public, private, DH_P)
    return hashlib.sha256(str(shared).encode()).hexdigest()
```

### Pairing code

```python
def _generate_pairing_code():
    """6-digit numeric code, valid for 5 minutes."""
    code = f"{secrets.randbelow(1000000):06d}"
    return code

# Pending pairings stored in memory (not persisted -- expires on restart)
_pending = {}  # code → {"private": int, "public": int, "created": float}
```

### Plugin routes

```python
ROUTES = {}

async def handle_initiate(method, body, params):
    """Node A calls this to start pairing. Returns the code to display."""
    private, public = _generate_keypair()
    code = _generate_pairing_code()
    _pending[code] = {"private": private, "public": public, "created": time.time()}
    return {"code": code, "expires_in": 300}

async def handle_complete(method, body, params):
    """Node B sends its public key + the pairing code."""
    data = json.loads(body)
    code = data["code"]
    peer_public = data["public_key"]
    peer_name = data["peer_name"]

    if code not in _pending:
        return {"error": "invalid or expired code", "_status": 400}

    entry = _pending.pop(code)
    if time.time() - entry["created"] > 300:
        return {"error": "code expired", "_status": 400}

    # Derive shared secret
    shared = _derive_shared(entry["private"], peer_public)

    # Store peer in config-peers
    _store_peer(peer_name, peer_public, shared)

    return {"public_key": entry["public"], "peer_name": _my_name()}

ROUTES["/proxy/pairing/initiate"] = handle_initiate
ROUTES["/proxy/pairing/complete"] = handle_complete
```

### Peer storage: `config-peers` world

```json
{
  "desktop": {
    "public_key": "abc123...",
    "shared_secret_hash": "def456...",
    "trusted_at": "2025-01-15T10:30:00Z",
    "trust_level": "direct"
  },
  "phone": {
    "public_key": "789ghi...",
    "shared_secret_hash": "jkl012...",
    "trusted_at": "2025-01-16T08:00:00Z",
    "trust_level": "direct"
  }
}
```

### Web of Trust (transitive trust)

If Node A trusts Node B (direct pairing) and Node B trusts Node C (direct pairing), Node A can optionally extend trust to Node C. This is opt-in per peer.

```python
def _evaluate_transitive_trust(target_name, max_depth=2):
    """Check if target is reachable through trust chain."""
    peers = _read_peers()
    visited = set()
    queue = [(name, 0) for name in peers if peers[name]["trust_level"] == "direct"]

    while queue:
        current, depth = queue.pop(0)
        if current == target_name:
            return True, depth
        if depth >= max_depth or current in visited:
            continue
        visited.add(current)
        # Ask the peer for their peer list
        remote_peers = _fetch_peer_list(current)
        for rp in remote_peers:
            queue.append((rp, depth + 1))

    return False, -1
```

Transitive trust is stored with `"trust_level": "transitive"` and a `"via"` field indicating the chain.

### Sync integration

Once paired, sync.py reads `config-peers` instead of static tokens in `config-endpoints`. The shared secret replaces the `"token"` field:

```python
# In sync.py, auth header uses HMAC of timestamp with shared secret
import hmac, hashlib, time

def _peer_auth_header(shared_secret):
    ts = str(int(time.time()))
    sig = hmac.new(shared_secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return {"X-Peer-Auth": f"{ts}:{sig}"}
```

## Implementation estimate
- ~80 lines Python for the pairing plugin
- ~20 lines modification to sync.py for peer-auth headers
- Dependencies: none beyond stdlib (hashlib, secrets, json)
- Two new worlds: `config-peers` (peer keys), existing `config-endpoints` gets a migration path

## Trigger
When sync.py needs secure peer authentication instead of static tokens pasted into `config-endpoints`. Static tokens work for two machines you control; pairing is needed when adding a third node or when tokens feel too fragile.

## Related
- `sync.py` plugin: currently reads `config-endpoints` with static `{"url": "...", "token": "..."}` format
- `config-endpoints` world: peer connection config (will gain a `"paired": true` flag)
- HMAC chain in server.py: same `_hmac` pattern reused for peer auth signatures
- `lockdown.md`: paired peers could auto-whitelist each other's IPs
