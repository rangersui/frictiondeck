"""Write a fresh elastik universe.db using server.py's canonical
log_event algorithm. Used as the input to go-side VerifyChain to
prove Python→Go cross-language chain verification works."""
import hashlib
import hmac
import json
import os
import sqlite3
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
    stage_html TEXT DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
    version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
INSERT OR IGNORE INTO stage_meta(id,updated_at) VALUES(1,datetime('now'));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT DEFAULT '{}',
    hmac TEXT NOT NULL, prev_hmac TEXT DEFAULT '');
"""


def log_event(conn, key: bytes, etype: str, payload: dict):
    # Exact replication of server.py:67-73.
    p = json.dumps(payload or {}, ensure_ascii=False)
    row = conn.execute(
        "SELECT hmac FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = row[0] if row else ""
    h = hmac.new(key, (prev + p).encode("utf-8"), hashlib.sha256).hexdigest()
    conn.execute(
        "INSERT INTO events(timestamp,event_type,payload,hmac,prev_hmac) "
        "VALUES(datetime('now'),?,?,?,?)",
        (etype, p, h, prev),
    )
    conn.commit()


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "_py_data/py-verify/universe.db"
    key = (sys.argv[2] if len(sys.argv) > 2 else "elastik-dev-key").encode()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    cases = [
        ("stage_written", {"len": len("hello")}),         # 5
        ("stage_written", {"len": len("中文测试")}),        # 4 (codepoints)
        ("stage_appended", {"len": len("café🙂")}),        # 5
        ("webhook_received", {"source": "slack", "body": "hi"}),  # multi-key
    ]
    for etype, payload in cases:
        log_event(conn, key, etype, payload)
    conn.close()
    print(f"wrote {len(cases)} events to {db_path}")


if __name__ == "__main__":
    main()
