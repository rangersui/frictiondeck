# Crypto — Encryption at Rest and in Transit

## Concept
Encryption layer for world content at rest (SQLite) and in transit (peer sync), building on the existing HMAC audit chain.

## Design

The current HMAC chain in `log_event()` signs events for tamper detection but does not encrypt world content. A stolen `universe.db` file exposes all `stage_html` in plaintext. This design adds encryption without changing the world read/write API.

### Encryption at rest — HMAC-CTR mode

Each world's `stage_html` is encrypted before writing to SQLite and decrypted on read. The counter is the world's `version` number, which is monotonically increasing and never reused (critical for CTR mode safety).

**Primary implementation: stdlib only** (zero dependencies)

```python
import hashlib, hmac as _hmac, os

def derive_world_key(master_key: bytes, world_name: str) -> bytes:
    """Derive a per-world key from ELASTIK_KEY + world name."""
    return hashlib.sha256(master_key + world_name.encode()).digest()

def _ctr_keystream(key: bytes, version: int, length: int) -> bytes:
    """HMAC-CTR: generate keystream using HMAC-SHA256 as PRF."""
    stream = b''
    counter = 0
    while len(stream) < length:
        block = _hmac.new(key, version.to_bytes(8, 'big') + counter.to_bytes(8, 'big'),
                          hashlib.sha256).digest()
        stream += block
        counter += 1
    return stream[:length]

def encrypt_stage(key: bytes, version: int, plaintext: str) -> bytes:
    """XOR plaintext with HMAC-CTR keystream. Version = nonce (monotonic, never reused)."""
    data = plaintext.encode('utf-8')
    return bytes(a ^ b for a, b in zip(data, _ctr_keystream(key, version, len(data))))

def decrypt_stage(key: bytes, version: int, ciphertext: bytes) -> str:
    return bytes(a ^ b for a, b in zip(ciphertext, _ctr_keystream(key, version, len(ciphertext)))).decode('utf-8')
```

**Optional upgrade: `cryptography` package** (AES-CTR, faster for large worlds)

```python
# Only if cryptography is installed — not required
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def encrypt_stage_aes(key: bytes, version: int, plaintext: str) -> bytes:
    nonce = version.to_bytes(16, 'big')
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    return cipher.encryptor().update(plaintext.encode('utf-8'))
```

The stdlib HMAC-CTR is the default. If `cryptography` is available, use AES-CTR for performance. Both produce the same security guarantees — CTR mode with a monotonic nonce.

Integration point in server.py — wrap the existing read/write handlers:

```python
# In the write handler (server.py ~line 358)
world_key = derive_world_key(KEY, name)
encrypted = encrypt_stage(world_key, new_version, body)
c.execute("UPDATE stage_meta SET stage_html=?,version=?,...", (encrypted, new_version))

# In the read handler (server.py ~line 352)
row = c.execute("SELECT stage_html, version FROM stage_meta WHERE id=1").fetchone()
world_key = derive_world_key(KEY, name)
plaintext = decrypt_stage(world_key, row["version"], row["stage_html"])
```

Encrypted worlds are opt-in. A `config-encrypted-worlds` world lists which worlds to encrypt (one name per line). Unencrypted worlds pass through unchanged.

### Encryption in transit — DH key exchange for sync

sync.py currently sends world content as plaintext HTTP POST bodies. Adding DH key exchange on first peer contact derives a shared key for sync payloads.

**Primary implementation: stdlib `pow(G, x, P)` for DH**

```python
import os, hashlib

# RFC 3526 Group 14 (2048-bit MODP)
P = 0xFFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74...  # truncated
G = 2

def dh_keypair():
    """Generate DH keypair using stdlib only."""
    x = int.from_bytes(os.urandom(256), 'big') % (P - 2) + 1  # private
    Y = pow(G, x, P)  # public
    return x, Y

def dh_shared_secret(my_private, peer_public):
    """Derive shared secret. XOR-encrypt sync payloads with HMAC-CTR using this."""
    shared = pow(peer_public, my_private, P)
    return hashlib.sha256(shared.to_bytes(256, 'big')).digest()

# POST /sync/handshake {public_key: Y}
# Both peers derive same shared_key → use HMAC-CTR from above
```

**Optional upgrade: `cryptography` package** (for proper parameter validation)

```python
from cryptography.hazmat.primitives.asymmetric import dh
parameters = dh.generate_parameters(generator=2, key_size=2048)
private_key = parameters.generate_private_key()
# ... standard cryptography DH flow
```

After handshake, all sync payloads are AES-encrypted with the derived key. The shared key is stored in memory only — lost on restart, renegotiated automatically.

### Fallback — Tailscale / SSH tunnel

If the `cryptography` package is not installed (it is an optional dependency), sync can be tunneled through Tailscale or SSH, both of which handle key exchange:

```bash
# Tailscale: peers on same tailnet, sync uses tailscale IPs
# config-endpoints: {"peer": {"url": "http://100.x.x.x:3004"}}

# SSH port forward: tunnel sync port through SSH
ssh -L 3004:localhost:3004 user@peer-host
# config-endpoints: {"peer": {"url": "http://localhost:3004"}}
```

This is the zero-dependency fallback. No code changes needed in sync.py — the tunnel is transparent.

### What changes, what does not

| Component | Changes | Does not change |
|-----------|---------|-----------------|
| stage_html in SQLite | Encrypted bytes | Column name, schema |
| Read/write API | Encrypt/decrypt layer | HTTP endpoints, JSON shape |
| HMAC chain | Signs encrypted content | Chain logic, `log_event()` |
| sync.py payloads | Encrypted with DH key | Sync protocol, version logic |

## Implementation estimate
- `derive_world_key` + `encrypt_stage` + `decrypt_stage`: ~25 lines
- server.py read/write wrapper: ~15 lines delta
- DH handshake endpoint in sync.py: ~40 lines
- Encrypted sync payload handling: ~20 lines
- Dependencies: stdlib only (`hashlib`, `hmac`, `os`). `cryptography` optional for AES-CTR upgrade.

## Trigger
When worlds contain sensitive data (credentials, personal notes, API keys stored in config worlds) and the device running elastik might be physically accessed by others or the SQLite files are stored on shared/cloud-synced storage.

## Related
- HMAC chain in `log_event()` (server.py line 71-77)
- `KEY` = `ELASTIK_KEY` env var (server.py line 16)
- sync.py — current plaintext HTTP sync
- bus.py — optional `cryptography` dependency for self-signed certs
- `config-encrypted-worlds` — new config world (opt-in list)
