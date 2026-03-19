"""lucy — Elastik OS CLI

Usage:
    lucy start              Start the server
    lucy start --safe       Start in enterprise (safe) mode
    lucy stages             List all stages
    lucy status             Show server status, stage count, plugin count
    lucy install <name>     Install plugin from plugins/available/
    lucy remove <name>      Remove installed plugin
    lucy list               List installed plugins
    lucy create <name>      Create a new stage
"""

import argparse
import os
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
PLUGINS_DIR = os.path.join(PROJECT_ROOT, "plugins")
AVAILABLE_DIR = os.path.join(PLUGINS_DIR, "available")


def cmd_start(args):
    env = os.environ.copy()
    if args.safe:
        env["FRICTIONDECK_MODE"] = "enterprise"
        print("Starting in enterprise (safe) mode...")
    else:
        print("Starting in personal mode...")
    subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "server.py")],
        env=env,
    )


def cmd_stages(args):
    if not os.path.exists(DATA_DIR):
        print("No data directory.")
        return
    stages = [
        d for d in sorted(os.listdir(DATA_DIR))
        if os.path.isdir(os.path.join(DATA_DIR, d))
    ]
    if not stages:
        print("No stages.")
        return
    for s in stages:
        stage_db = os.path.join(DATA_DIR, s, "stage.db")
        exists = "ok" if os.path.exists(stage_db) else "no db"
        print(f"  {s}  ({exists})")


def cmd_status(args):
    # Check if server is running
    import socket
    port = int(os.environ.get("FRICTIONDECK_PORT", "3004"))
    running = False
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            running = True
    except (ConnectionRefusedError, OSError):
        pass
    print(f"Server: {'running' if running else 'stopped'}  (port {port})")

    # Count stages
    stages = 0
    if os.path.exists(DATA_DIR):
        stages = len([
            d for d in os.listdir(DATA_DIR)
            if os.path.isdir(os.path.join(DATA_DIR, d))
        ])
    print(f"Stages: {stages}")

    # Count plugins
    plugins = 0
    if os.path.exists(PLUGINS_DIR):
        plugins = len([
            f for f in os.listdir(PLUGINS_DIR)
            if f.endswith(".py") and not f.startswith("_")
        ])
    print(f"Plugins: {plugins}")


def cmd_install(args):
    name = args.name
    src = os.path.join(AVAILABLE_DIR, f"{name}.py")
    dst = os.path.join(PLUGINS_DIR, f"{name}.py")

    if not os.path.exists(src):
        print(f"Not found: {src}")
        available = []
        if os.path.exists(AVAILABLE_DIR):
            available = [f[:-3] for f in os.listdir(AVAILABLE_DIR) if f.endswith(".py")]
        if available:
            print(f"Available: {', '.join(available)}")
        return

    if os.path.exists(dst):
        print(f"Already installed: {name}")
        return

    os.makedirs(PLUGINS_DIR, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"Installed: {name}")


def cmd_remove(args):
    name = args.name
    path = os.path.join(PLUGINS_DIR, f"{name}.py")
    if not os.path.exists(path):
        print(f"Not installed: {name}")
        return
    os.remove(path)
    print(f"Removed: {name}")


def cmd_list(args):
    if not os.path.exists(PLUGINS_DIR):
        print("No plugins installed.")
        return
    plugins = [
        f[:-3] for f in sorted(os.listdir(PLUGINS_DIR))
        if f.endswith(".py") and not f.startswith("_")
    ]
    if not plugins:
        print("No plugins installed.")
        return
    for p in plugins:
        print(f"  {p}")


def cmd_create(args):
    name = "".join(c for c in args.name if c.isalnum() or c in "-_")
    if not name:
        print("Invalid stage name.")
        return
    stage_dir = os.path.join(DATA_DIR, name)
    if os.path.exists(stage_dir):
        print(f"Stage already exists: {name}")
        return
    os.makedirs(stage_dir, exist_ok=True)

    # Initialize stage.db
    import sqlite3
    from datetime import datetime, timezone
    conn = sqlite3.connect(os.path.join(stage_dir, "stage.db"))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stage_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL DEFAULT 0,
            stage_html TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS judgment_objects (
            judgment_id TEXT PRIMARY KEY,
            claim_text TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'viscous',
            commit_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'ai'
        );
    """)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO stage_meta (id, version, stage_html, updated_at) VALUES (1, 0, '', ?)",
        (ts,),
    )
    conn.commit()
    conn.close()

    # Initialize history.db
    conn = sqlite3.connect(os.path.join(stage_dir, "history.db"))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            pathway TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            environment TEXT NOT NULL DEFAULT '{}',
            prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
    """)
    conn.commit()
    conn.close()

    print(f"Created stage: {name}")


def main():
    parser = argparse.ArgumentParser(prog="lucy", description="Elastik OS CLI")
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="Start the server")
    p_start.add_argument("--safe", action="store_true", help="Enterprise mode")

    sub.add_parser("stages", help="List all stages")
    sub.add_parser("status", help="Show status")

    p_install = sub.add_parser("install", help="Install plugin from available/")
    p_install.add_argument("name", help="Plugin name")

    p_remove = sub.add_parser("remove", help="Remove installed plugin")
    p_remove.add_argument("name", help="Plugin name")

    sub.add_parser("list", help="List installed plugins")

    p_create = sub.add_parser("create", help="Create a new stage")
    p_create.add_argument("name", help="Stage name")

    args = parser.parse_args()

    commands = {
        "start": cmd_start,
        "stages": cmd_stages,
        "status": cmd_status,
        "install": cmd_install,
        "remove": cmd_remove,
        "list": cmd_list,
        "create": cmd_create,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
