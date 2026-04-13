"""Install, remove, or list renderers.

Usage:
  python scripts/renderer.py install markdown
  python scripts/renderer.py remove markdown
  python scripts/renderer.py list
"""
import sys, os, requests
from pathlib import Path

env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

ELASTIK = os.getenv("ELASTIK_URL", "http://localhost:3005")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
DIR = Path(__file__).parent.parent / "renderers"

def h():
    headers = {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers

cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
name = sys.argv[2] if len(sys.argv) > 2 else ""

if cmd == "install" and name:
    f = DIR / f"{name}.html"
    if not f.exists():
        avail = [x.stem for x in DIR.glob("*.html")] if DIR.exists() else []
        print(f"Not found: {f}")
        if avail:
            print(f"Available: {', '.join(avail)}")
        sys.exit(1)
    code = f.read_text()
    r = requests.post(f"{ELASTIK}/renderer-{name}/write", data=code.encode(), headers=h())
    print(f"Installed renderer-{name} ({len(code)} bytes) → status {r.status_code}")

elif cmd == "remove" and name:
    r = requests.post(f"{ELASTIK}/renderer-{name}/write", data=b"", headers=h())
    print(f"Removed renderer-{name} → status {r.status_code}")

elif cmd == "list":
    # Installed
    try:
        stages = requests.get(f"{ELASTIK}/stages").json()
        installed = [s["name"] for s in stages if s["name"].startswith("renderer-")]
    except Exception:
        installed = []
    print("Installed:")
    for s in installed:
        print(f"  {s}")
    if not installed:
        print("  (none)")

    # Available
    print(f"\nAvailable:")
    if DIR.exists():
        for f in sorted(DIR.glob("*.html")):
            marker = " [installed]" if f"renderer-{f.stem}" in installed else ""
            print(f"  {f.stem}{marker}")
    else:
        print("  (no renderers/ directory)")

else:
    print("Usage: renderer.py [install|remove|list] [name]")
