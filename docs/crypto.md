# Crypto — Encryption at Rest and in Transit

## Concept
Encryption layer for world content at rest (SQLite) and in transit (peer sync), building on the existing HMAC audit chain.

## Design

The current HMAC chain in `log_event()` signs events for tamper detection but does not encrypt world content. A stolen `universe.db` file exposes all `stage_html` in plaintext. This design adds encryption without changing the world read/write API.

### Encryption at rest — HMAC-CTR mode

Each world's `stage_html` is encrypted before writing to SQLite and decrypted on read. The counter is the world's `version` number, which is monotonically increasing and never reused (critical for CTR mode safety).

```python
import hashlib, hmac as _hmac, os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def derive_world_key(master_key: bytes, world_name: str) -> bytes:
    """Derive a per-world AES key from ELASTIK_KEY + world name."""
    return hashlib.sha256(master_key + world_name.encode()).digest()

def encrypt_stage(key: bytes, version: int, plaintext: str) -> bytes:
    """CTR-mode encrypt. Counter = version (monotonic, never reused)."""
    # 16-byte nonce: 12 bytes zero-padded + 4 bytes version
    nonce = version.to_bytes(16, 'big')
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    enc = cipher.encryptor()
    return enc.update(plaintext.encode('utf-8')) + enc.finalize()

def decrypt_stage(key: bytes, version: int, ciphertext: bytes) -> str:
    nonce = version.to_bytes(16, 'big')
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    dec = cipher.decryptor()
    return (dec.update(ciphertext) + dec.finalize()).decode('utf-8')
```

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

sync.py currently sends world content as plaintext HTTP POST bodies. Adding DH key exchange on first peer contact derives a shared AES key for sync payloads.

```python
# First sync handshake — Diffie-Hellman
from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives import serialization

# Peer A generates parameters + keypair
parameters = dh.generate_parameters(generator=2, key_size=2048)
private_key = parameters.generate_private_key()
public_key = private_key.public_key()

# Send public key to Peer B via new endpoint
# POST /sync/handshake {public_key: <pem>, parameters: <pem>}

# Peer B generates its keypair with same parameters, sends public key back
# Both derive shared secret
shared_key = private_key.exchange(peer_public_key)
# Derive AES key from shared secret
aes_key = hashlib.sha256(shared_key).digest()
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
- Dependencies: `cryptography` (already optional dep for bus.py TLS cert generation)

## Trigger
When worlds contain sensitive data (credentials, personal notes, API keys stored in config worlds) and the device running elastik might be physically accessed by others or the SQLite files are stored on shared/cloud-synced storage.

## Related
- HMAC chain in `log_event()` (server.py line 71-77)
- `KEY` = `ELASTIK_KEY` env var (server.py line 16)
- sync.py — current plaintext HTTP sync
- bus.py — optional `cryptography` dependency for self-signed certs
- `config-encrypted-worlds` — new config world (opt-in list)
