"""Adversarial: Terminator — traps SIGTERM, refuses to die.

Proves: Go sends SIGKILL (not SIGTERM) on context cancel.
SIGKILL cannot be caught, blocked, or ignored.
If Go only sends SIGTERM, this process lives forever.

On Windows: TerminateProcess is also uncatchable, so this
should always be killed regardless of platform.
"""
import sys, json, signal, time

if len(sys.argv) > 1 and sys.argv[1] == "--routes":
    print(json.dumps(["/terminator"]))
    sys.exit(0)

# Trap SIGTERM — refuse to die
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, lambda *_: None)  # ignore SIGTERM

d = json.loads(sys.stdin.readline())
# Write valid JSON immediately, then refuse to exit
sys.stdout.write(json.dumps({"status": 200, "body": "I'll be back"}))
sys.stdout.flush()
# Sleep forever — only SIGKILL or TerminateProcess can stop us
while True:
    time.sleep(1)
