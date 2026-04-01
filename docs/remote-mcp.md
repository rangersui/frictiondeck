# Remote MCP — Three Access Tiers

## Concept
Three access tiers for remote MCP connections: read-free, write-OTP, admin-token.

## Design

### Tier 1 — Read (no auth)
GET requests require no authentication. Worlds are public by default. This matches the current behavior of auth.py, which passes all GET requests through unconditionally.

```
GET /my-world/read  →  200, returns {stage_html, pending_js, js_result, version}
```

Any MCP client can read any world without credentials. This is intentional: worlds are strings, not secrets. If a world contains sensitive data, encryption (see crypto.md) protects the content, not the access.

### Tier 2 — Write (one-time password)
POST requests to world endpoints require a one-time password. The OTP is generated on the server and displayed in the terminal — the person physically at the machine reads it and gives it to the remote user.

```python
# OTP generation — add to server.py or auth.py
import secrets, time, hashlib

_otp_store = {}  # hash → expiry timestamp

def generate_otp():
    code = secrets.token_hex(4)  # 8 hex chars, e.g. "a3f1b20c"
    h = hashlib.sha256(code.encode()).hexdigest()
    _otp_store[h] = time.time() + 300  # expires in 5 minutes
    print(f"\n  OTP for remote write: {code}\n")
    return code

def verify_otp(code):
    h = hashlib.sha256(code.encode()).hexdigest()
    expiry = _otp_store.get(h)
    if not expiry:
        return False
    if time.time() > expiry:
        del _otp_store[h]
        return False
    del _otp_store[h]  # single use — consumed on verification
    return True
```

Remote MCP client sends the OTP in a header:

```
POST /my-world/write
X-OTP: a3f1b20c
Content-Type: text/plain

<world content>
```

OTP endpoint to request a new code (requires being on localhost or having admin token):

```
POST /auth/otp/generate  →  prints OTP to server stdout, returns 200
```

### Tier 3 — Admin (permanent token)
Plugin management, config world writes, and system administration require `ELASTIK_TOKEN` (existing) or `ELASTIK_APPROVE_TOKEN` (existing). No changes needed — auth.py already enforces this.

```
POST /plugins/approve
X-Approve-Token: <ELASTIK_APPROVE_TOKEN>

POST /config-endpoints/write
X-Approve-Token: <ELASTIK_APPROVE_TOKEN>
```

### Auth middleware changes

```python
# Modified auth.py middleware — add OTP check for write tier
async def auth_middleware(scope, path, method):
    if method == "GET":
        return True  # Tier 1: read-free

    # Tier 3 paths (existing logic, unchanged)
    if path.startswith("/admin/") or path.startswith("/config-"):
        return check_approve_token(scope)
    if path == "/plugins/approve":
        return True  # server.py handles its own token check

    # Tier 2: write requires OTP or permanent token
    headers = dict(scope.get("headers", []))
    otp = headers.get(b"x-otp", b"").decode()
    if otp and verify_otp(otp):
        return True

    # Fall through to existing token check
    token = os.getenv("ELASTIK_TOKEN", "")
    if not token:
        return True
    tok = headers.get(b"x-auth-token", b"").decode()
    return hmac.compare_digest(tok, token)
```

### MCP client flow

```
1. Remote user: "I want to write to your elastik"
2. Local user runs: curl -X POST http://localhost:3004/auth/otp/generate
3. Server prints: "OTP for remote write: a3f1b20c"
4. Local user tells remote user the code (voice, chat, etc.)
5. Remote MCP client uses code once, it expires
```

## Implementation estimate
- OTP generation + verification: ~25 lines (in auth.py or new otp.py plugin)
- OTP generation endpoint: ~10 lines
- auth.py middleware modification: ~10 lines delta
- No new dependencies (uses stdlib `secrets`, `hashlib`, `time`)

## Trigger
When sharing elastik with others who need limited write access — pair programming, collaborative world editing, remote MCP tool use. Specifically when someone connects Claude Code's MCP client to a remote elastik instance.

## Related
- auth.py plugin — current auth middleware (GET open, POST needs token)
- `ELASTIK_TOKEN` and `ELASTIK_APPROVE_TOKEN` in server.py (line 17-18)
- mcp_server.py — MCP interface that would use these tiers
- config-endpoints world (peer configuration)
