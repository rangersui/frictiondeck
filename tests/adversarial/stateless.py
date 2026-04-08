"""Adversarial: stateless proof — counter must reset every call.

Proves: CGI process is ephemeral. No state leaks between requests.
"""
import sys, json

count = 0  # module-level — would increment if process persisted

if len(sys.argv) > 1 and sys.argv[1] == "--routes":
    print(json.dumps(["/stateless"]))
    sys.exit(0)

d = json.loads(sys.stdin.readline())
count += 1
print(json.dumps({"status": 200, "body": str(count)}))
