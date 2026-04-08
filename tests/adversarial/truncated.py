"""Adversarial: truncated JSON — process dies mid-output.

Proves: Go catches incomplete JSON and returns 502, not panic.
"""
import sys, json, os

if len(sys.argv) > 1 and sys.argv[1] == "--routes":
    print(json.dumps(["/truncated"]))
    sys.exit(0)

# Read request, then die mid-response
d = json.loads(sys.stdin.readline())
sys.stdout.write('{"status": 200, "body": "he')
sys.stdout.flush()
os._exit(1)  # hard kill — no cleanup, no newline, no closing brace
