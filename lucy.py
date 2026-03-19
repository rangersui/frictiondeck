"""lucy — elastik CLI. Zero dependencies."""
import argparse, os, shutil, socket, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
PLUGINS = ROOT / "plugins"
AVAILABLE = PLUGINS / "available"

def cmd_start(args):
    env = os.environ.copy()
    if args.public: env["ELASTIK_HOST"] = "0.0.0.0"
    if args.port: env["ELASTIK_PORT"] = str(args.port)
    subprocess.run([sys.executable, str(ROOT / "server.py")], env=env)

def cmd_stages(_):
    if not DATA.exists(): print("no stages."); return
    found = False
    for d in sorted(DATA.iterdir()):
        if d.is_dir() and (d / "universe.db").exists():
            c = sqlite3.connect(str(d / "universe.db"))
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
            print(f"  {d.name}  v{r['version']}  {r['updated_at']}")
            c.close(); found = True
    if not found: print("no stages.")

def cmd_create(args):
    name = "".join(c for c in args.name if c.isalnum() or c in "-_")
    if not name: print("invalid name."); return
    d = DATA / name
    if d.exists(): print(f"exists: {name}"); return
    d.mkdir(parents=True)
    c = sqlite3.connect(str(d / "universe.db"))
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
            stage_html TEXT DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
            version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
        INSERT OR IGNORE INTO stage_meta(id,updated_at) VALUES(1,datetime('now'));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT DEFAULT '{}',
            hmac TEXT NOT NULL, prev_hmac TEXT DEFAULT '');
    """)
    c.commit(); c.close()
    print(f"created: {name}")

def cmd_status(_):
    port = int(os.environ.get("ELASTIK_PORT", "3004"))
    try:
        with socket.create_connection(("localhost", port), timeout=1): running = True
    except (ConnectionRefusedError, OSError): running = False
    stages = len([d for d in DATA.iterdir() if d.is_dir()]) if DATA.exists() else 0
    plugins = len([f for f in PLUGINS.glob("*.py") if not f.name.startswith("_")]) if PLUGINS.exists() else 0
    print(f"  server: {'running' if running else 'stopped'} (:{port})")
    print(f"  stages: {stages}")
    print(f"  plugins: {plugins}")

def cmd_install(args):
    src = AVAILABLE / f"{args.name}.py"
    dst = PLUGINS / f"{args.name}.py"
    if not src.exists():
        avail = [f.stem for f in AVAILABLE.glob("*.py")] if AVAILABLE.exists() else []
        print(f"not found. available: {', '.join(avail) or 'none'}"); return
    if dst.exists(): print(f"already installed: {args.name}"); return
    PLUGINS.mkdir(exist_ok=True); shutil.copy2(src, dst)
    print(f"installed: {args.name}")

def cmd_remove(args):
    p = PLUGINS / f"{args.name}.py"
    if not p.exists(): print(f"not installed: {args.name}"); return
    p.unlink(); print(f"removed: {args.name}")

def cmd_list(_):
    if not PLUGINS.exists(): print("no plugins."); return
    ps = [f.stem for f in sorted(PLUGINS.glob("*.py")) if not f.name.startswith("_")]
    if not ps: print("no plugins."); return
    for p in ps: print(f"  {p}")

def main():
    ap = argparse.ArgumentParser(prog="lucy", description="elastik CLI")
    sp = ap.add_subparsers(dest="cmd")
    s = sp.add_parser("start"); s.add_argument("--public", action="store_true"); s.add_argument("--port", type=int)
    sp.add_parser("stages"); sp.add_parser("status"); sp.add_parser("list")
    c = sp.add_parser("create"); c.add_argument("name")
    i = sp.add_parser("install"); i.add_argument("name")
    r = sp.add_parser("remove"); r.add_argument("name")
    args = ap.parse_args()
    cmds = {"start":cmd_start,"stages":cmd_stages,"create":cmd_create,"status":cmd_status,
            "install":cmd_install,"remove":cmd_remove,"list":cmd_list}
    if args.cmd in cmds: cmds[args.cmd](args)
    else: ap.print_help()

if __name__ == "__main__": main()
