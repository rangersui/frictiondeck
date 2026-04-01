# Verified Boot — Server Integrity + A/B Rollback

## Concept
Verify server.py integrity before starting, and maintain two versions for safe rollback on failure.

## Design

### Why a separate launcher

server.py cannot verify itself. The file being verified must not be the file doing the verification. A small launcher script (outside the server codebase) performs the check and decides whether to start.

### SHA256 verification

The known-good hash of server.py is stored in the `config-boot-hash` world's SQLite database. The launcher reads the hash, computes the current file's hash, and compares.

```python
#!/usr/bin/env python3
"""boot.py — verified launcher for server.py. ~20 lines."""
import hashlib, sqlite3, sys, os, subprocess
from pathlib import Path

DATA = Path("data")
SERVER_A = Path("server.py")      # current
SERVER_B = Path("server.py.b")    # previous (rollback)

def read_approved_hash():
    db = DATA / "config-boot-hash" / "universe.db"
    if not db.exists():
        return None  # first boot, no hash stored yet
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    return row["stage_html"].strip() if row and row["stage_html"] else None

def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    approved = read_approved_hash()
    current = file_hash(SERVER_A)

    if approved and current != approved:
        print(f"BOOT FAILED: server.py hash mismatch")
        print(f"  expected: {approved}")
        print(f"  actual:   {current}")
        print(f"  server.py may have been tampered with")

        if SERVER_B.exists():
            backup_hash = file_hash(SERVER_B)
            if backup_hash == approved:
                print("  rolling back to server.py.b")
                SERVER_A.write_bytes(SERVER_B.read_bytes())
                # Fall through to start
            else:
                print("  server.py.b also doesn't match. Manual intervention required.")
                sys.exit(1)
        else:
            sys.exit(1)

    # Start server
    print(f"  boot: server.py hash verified ({current[:12]}...)")
    os.execvp(sys.executable, [sys.executable, str(SERVER_A)])

if __name__ == "__main__":
    main()
```

### A/B partition scheme

Two copies of server.py are maintained:
- `server.py` (partition A) — the active version
- `server.py.b` (partition B) — the previous known-good version

On update (e.g., received via sync.py from a trusted peer):

```python
def update_server(new_content: bytes):
    # 1. Copy current to backup
    SERVER_B.write_bytes(SERVER_A.read_bytes())

    # 2. Write new version
    SERVER_A.write_bytes(new_content)

    # 3. Update approved hash
    new_hash = hashlib.sha256(new_content).hexdigest()
    c = conn("config-boot-hash")
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,"
              "updated_at=datetime('now') WHERE id=1", (new_hash,))
    c.commit()

    # 4. Restart (systemd, Docker, or manual)
    print(f"  server updated. new hash: {new_hash[:12]}...")
    print(f"  previous version saved to server.py.b")
```

### Boot failure detection

If the server crashes within 30 seconds of starting, the launcher treats it as a boot failure and swaps partitions:

```python
proc = subprocess.Popen([sys.executable, str(SERVER_A)])
try:
    proc.wait(timeout=30)
    # If it exited within 30 seconds, it crashed
    if proc.returncode != 0 and SERVER_B.exists():
        print("  boot failure detected, rolling back")
        SERVER_A.write_bytes(SERVER_B.read_bytes())
        os.execvp(sys.executable, [sys.executable, str(SERVER_A)])
except subprocess.TimeoutExpired:
    pass  # Still running after 30s — healthy boot
```

### First boot (no hash stored)

On first run, no `config-boot-hash` world exists. The launcher computes the hash and stores it, establishing the initial trust anchor. This assumes the first boot is on trusted hardware.

## Implementation estimate
- boot.py launcher: ~40 lines
- A/B swap logic: ~15 lines (part of launcher)
- Update function (for sync.py integration): ~15 lines
- No changes to server.py itself
- Dependencies: none (stdlib only)

## Trigger
When server.py is auto-updated from remote peers via sync.py, or when running on a device where physical access by untrusted parties is possible. The launcher becomes the standard way to start elastik in production.

## Related
- sync.py — could deliver server.py updates from trusted peers
- server.py startup sequence (line 1-25, env loading)
- `config-boot-hash` — new config world for storing approved hash
- Docker deployment (Dockerfile uses `python server.py` — would change to `python boot.py`)
