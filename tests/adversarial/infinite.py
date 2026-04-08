"""Adversarial: infinite output — tests OOM guard (5MB limit).

Proves: Go kills the plugin at 5MB, returns 502, no OOM.
"""
import sys, json

if len(sys.argv) > 1 and sys.argv[1] == "--routes":
    print(json.dumps(["/infinite"]))
    sys.exit(0)

d = json.loads(sys.stdin.readline())
sys.stdout.write('{"status": 200, "body": "')
# Infinite loop — Go must cut the pipe
while True:
    sys.stdout.write("A" * 65536)
    sys.stdout.flush()
