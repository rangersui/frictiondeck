#!/usr/bin/env python3
"""Admin CLI — hot plug plugins via HTTP.

Usage:
    python scripts/admin.py list
    python scripts/admin.py load patch
    python scripts/admin.py unload fs
    python scripts/admin.py reload auth
    python scripts/admin.py status
"""
import http.client as _http, json, os, sys, urllib.parse
from pathlib import Path

env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_port = os.getenv("ELASTIK_PORT", "3004")
BASE = f"http://127.0.0.1:{_port}"
TOKEN = os.getenv("ELASTIK_APPROVE_TOKEN", "")

def http(method, path):
    parsed = urllib.parse.urlparse(BASE)
    try:
        c = _http.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
        headers = {}
        if TOKEN: headers["Authorization"] = f"Bearer {TOKEN}"
        c.request(method, path, headers=headers)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        if r.status >= 400:
            return {"error": body, "status": r.status}
        return json.loads(body)
    except Exception as e:
        return {"error": str(e)}

cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
name = sys.argv[2] if len(sys.argv) > 2 else ""

if cmd == "list":
    r = http("GET", "/admin/list")
    for p in r.get("plugins", []):
        routes = ", ".join(p.get("routes", [])) or "(middleware)"
        print(f"  {p['name']:20s} {routes}")
elif cmd == "load" and name:
    print(json.dumps(http("POST", f"/admin/load?name={name}"), indent=2))
elif cmd == "unload" and name:
    print(json.dumps(http("POST", f"/admin/unload?name={name}"), indent=2))
elif cmd == "reload" and name:
    http("POST", f"/admin/unload?name={name}")
    print(json.dumps(http("POST", f"/admin/load?name={name}"), indent=2))
elif cmd == "status":
    print(json.dumps(http("GET", "/admin/status"), indent=2))
elif cmd in ("interactive", "i"):
    print("elastik admin. type 'help' for commands.\n")
    while True:
        try:
            line = input("elastik> ").strip()
            if not line: continue
            parts = line.split()
            c, n = parts[0], parts[1] if len(parts) > 1 else ""
            if c == "list":
                r = http("GET", "/admin/list")
                for p in r.get("plugins", []):
                    routes = ", ".join(p.get("routes", [])) or "(middleware)"
                    print(f"  {p['name']:20s} {routes}")
            elif c == "load" and n:
                print(json.dumps(http("POST", f"/admin/load?name={n}"), indent=2))
            elif c == "unload" and n:
                print(json.dumps(http("POST", f"/admin/unload?name={n}"), indent=2))
            elif c == "reload" and n:
                http("POST", f"/admin/unload?name={n}")
                print(json.dumps(http("POST", f"/admin/load?name={n}"), indent=2))
            elif c == "status":
                print(json.dumps(http("GET", "/admin/status"), indent=2))
            elif c in ("quit", "exit"): break
            elif c == "help":
                print("  load <name>  unload <name>  reload <name>  list  status  quit")
            else: print(f"  unknown: {c}")
        except (EOFError, KeyboardInterrupt): break
    print("\nbye.")
else:
    print("Usage: admin-cli.py [list|load|unload|reload|status|interactive] [name]")
