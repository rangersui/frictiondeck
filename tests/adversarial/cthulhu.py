"""Adversarial: Cthulhu — binary garbage on stdout.

Not incomplete JSON (truncated.py). Not too-much JSON (infinite.py).
This is NOT TEXT AT ALL. Raw bytes from /dev/urandom including
null bytes, control chars, invalid UTF-8 sequences.

Proves: Go's json.Unmarshal fails gracefully on binary noise,
falls through to raw text response, and doesn't segfault or
corrupt memory on \\x00 or \\x04 (EOF char).
"""
import sys, json, os

if len(sys.argv) > 1 and sys.argv[1] == "--routes":
    print(json.dumps(["/cthulhu"]))
    sys.exit(0)

d = json.loads(sys.stdin.readline())
# Write raw binary garbage to stdout — not text, not JSON
sys.stdout.buffer.write(os.urandom(1024))
sys.stdout.buffer.flush()
