"""Cross-language HMAC chain verifier.

Reads an elastik universe.db written by *any* implementation (Python
server.py or Go elastik-go.exe) and verifies that every event's HMAC
matches the Python-canonical rule:

    payload_json = json.dumps(stored_payload, ensure_ascii=False)
    hmac_sha256(KEY, prev_hmac || payload_json) == event.hmac

Note: we do NOT re-encode the payload. We use the exact bytes stored
in the `payload` column. This is correct because the Go side ships
encodePayload that produces byte-identical output to Python's
json.dumps default, so the stored bytes are already canonical.

Exit 0 = chain valid. Exit 1 = broken (prints the offending index).

Usage:
    python verify_chain.py <universe.db> [key]

Default key = "elastik-dev-key" (matches server.py fallback).
"""
import hashlib
import hmac
import os
import sqlite3
import sys


def verify(db_path: str, key: bytes) -> tuple[bool, str]:
    if not os.path.exists(db_path):
        return False, f"no such file: {db_path}"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, event_type, payload, hmac, prev_hmac FROM events ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    prev = ""
    for i, r in enumerate(rows):
        if r["prev_hmac"] != prev:
            return False, (
                f"event #{i} (id={r['id']} type={r['event_type']}): "
                f"prev_hmac mismatch — stored {r['prev_hmac']!r}, expected {prev!r}"
            )
        # The stored payload column is the exact bytes that were hashed.
        computed = hmac.new(
            key, (prev + r["payload"]).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if computed != r["hmac"]:
            return False, (
                f"event #{i} (id={r['id']} type={r['event_type']}): "
                f"hmac mismatch\n"
                f"  payload : {r['payload']!r}\n"
                f"  stored  : {r['hmac']}\n"
                f"  computed: {computed}"
            )
        prev = r["hmac"]
    return True, f"OK — verified {len(rows)} events"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    db_path = sys.argv[1]
    key = (sys.argv[2] if len(sys.argv) > 2 else "elastik-dev-key").encode()
    ok, msg = verify(db_path, key)
    print(msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
