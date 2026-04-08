"""Adversarial: self-reading plugin — reads its own source and returns it.

Proves: plugin can introspect its own code. Filesystem access is real.
"""
import sys, json, os

if len(sys.argv) > 1 and sys.argv[1] == "--routes":
    print(json.dumps(["/selfreader"]))
    sys.exit(0)

d = json.loads(sys.stdin.readline())
src = open(__file__, "r", encoding="utf-8").read()
print(json.dumps({"status": 200, "body": src}))
