#!/usr/bin/env python3
"""install.py — One-click elastik setup.

1. Install Python dependencies
2. Inject MCP config into Claude Desktop
3. Start elastik server

Usage:
  python install.py           # install + configure + start
  python install.py --dry-run # show what would happen, don't do it
  python install.py --config  # only inject Claude Desktop config
  python install.py --start   # only start the server
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

ELASTIK_DIR = Path(__file__).resolve().parent
REQUIREMENTS = ELASTIK_DIR / "requirements.txt"
MCP_SERVER = ELASTIK_DIR / "mcp_server.py"
SERVER = ELASTIK_DIR / "server.py"

# Claude Desktop config paths per platform
CLAUDE_CONFIG_PATHS = {
    "Darwin": Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    "Windows": Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json",
    "Linux": Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
}


def find_python() -> str:
    """Find the best python executable."""
    # Prefer the one running this script
    return sys.executable


def find_claude_config() -> Path:
    """Find Claude Desktop config file."""
    system = platform.system()
    path = CLAUDE_CONFIG_PATHS.get(system)
    if path and path.parent.exists():
        return path
    # Try all paths as fallback
    for p in CLAUDE_CONFIG_PATHS.values():
        if p.parent.exists():
            return p
    return None


def step_install_deps(dry_run=False):
    """Step 1: Install Python dependencies."""
    print("\n[1/3] Installing dependencies...")

    if not REQUIREMENTS.exists():
        print("  requirements.txt not found, skipping")
        return True

    deps = REQUIREMENTS.read_text().strip().splitlines()
    print(f"  packages: {', '.join(deps)}")

    if dry_run:
        print("  (dry run) would run: pip install -r requirements.txt")
        return True

    try:
        subprocess.check_call(
            [find_python(), "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"],
            cwd=str(ELASTIK_DIR),
        )
        print("  done")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  FAILED: {e}")
        print("  try: pip install -r requirements.txt manually")
        return False


def step_configure_claude(dry_run=False):
    """Step 2: Inject elastik MCP config into Claude Desktop."""
    print("\n[2/3] Configuring Claude Desktop...")

    config_path = find_claude_config()
    if config_path is None:
        print("  Claude Desktop config directory not found")
        print("  Is Claude Desktop installed?")
        print("  You can manually add to your claude_desktop_config.json:")
        print_manual_config()
        return False

    print(f"  config: {config_path}")

    # Build the MCP server entry
    python = find_python()
    elastik_entry = {
        "command": python,
        "args": [str(MCP_SERVER)],
        "env": {
            "ELASTIK_URL": "http://localhost:3005",
            "ELASTIK_TOKEN": "",
        },
    }

    # Read existing config or start fresh
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            config = {}
    else:
        config = {}

    # Check if already configured
    mcp_servers = config.get("mcpServers", {})
    if "frictiondeck" in mcp_servers:
        existing = mcp_servers["frictiondeck"]
        if existing.get("args") == elastik_entry["args"]:
            print("  already configured (same path)")
            return True
        else:
            print(f"  updating existing config")
            print(f"    old: {existing.get('args', ['?'])}")
            print(f"    new: {elastik_entry['args']}")

    if dry_run:
        print(f"  (dry run) would write to: {config_path}")
        print(f"  entry: frictiondeck -> {python} {MCP_SERVER}")
        return True

    # Inject
    if "mcpServers" not in config:
        config["mcpServers"] = {}
    config["mcpServers"]["frictiondeck"] = elastik_entry

    # Write back
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup = config_path.with_suffix(".json.bak")
    if config_path.exists():
        import shutil
        shutil.copy2(config_path, backup)
        print(f"  backup: {backup}")

    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  written: {config_path}")
    print(f"  restart Claude Desktop to activate")
    return True


def step_start_server(dry_run=False):
    """Step 3: Start elastik."""
    print("\n[3/3] Starting elastik...")

    if not SERVER.exists():
        print(f"  server.py not found at {ELASTIK_DIR}")
        return False

    if dry_run:
        print(f"  (dry run) would run: python server.py")
        return True

    print(f"  starting: python server.py")
    print(f"  press Ctrl+C to stop\n")
    print("=" * 50)

    try:
        subprocess.call([find_python(), str(SERVER)], cwd=str(ELASTIK_DIR))
    except KeyboardInterrupt:
        print("\n  elastik stopped")
    return True


def step_setup_hooks(dry_run=False):
    """Set up git hooks if this is a git repo."""
    hooks_dir = ELASTIK_DIR / "scripts" / "hooks"
    git_dir = ELASTIK_DIR / ".git"
    if not git_dir.exists() or not hooks_dir.exists():
        return
    print("\n  Setting up git hooks...")
    if dry_run:
        print("  (dry run) would run: git config core.hooksPath scripts/hooks")
        return
    try:
        subprocess.check_call(
            ["git", "config", "core.hooksPath", "scripts/hooks"],
            cwd=str(ELASTIK_DIR),
        )
        print("  git hooks -> scripts/hooks/")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  git hooks skipped: {e}")


def print_manual_config():
    """Print manual config instructions."""
    python = find_python()
    entry = {
        "frictiondeck": {
            "command": python,
            "args": [str(MCP_SERVER)],
            "env": {
                "ELASTIK_URL": "http://localhost:3005",
                "ELASTIK_TOKEN": "",
            },
        }
    }
    print()
    print('  Add this to "mcpServers" in your claude_desktop_config.json:')
    print()
    print(f"  {json.dumps(entry, indent=4)}")
    print()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="elastik one-click installer")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen")
    ap.add_argument("--config", action="store_true", help="only configure Claude Desktop")
    ap.add_argument("--start", action="store_true", help="only start the server")
    args = ap.parse_args()

    print("=" * 50)
    print("  elastik installer")
    print(f"  directory: {ELASTIK_DIR}")
    print(f"  python:    {find_python()}")
    print(f"  platform:  {platform.system()}")
    print("=" * 50)

    if args.config:
        step_configure_claude(args.dry_run)
        return

    if args.start:
        step_start_server(args.dry_run)
        return

    # Full install
    ok = step_install_deps(args.dry_run)
    if not ok:
        print("\nDependency install failed. Fix and retry.")
        return

    step_setup_hooks(args.dry_run)
    step_configure_claude(args.dry_run)
    step_start_server(args.dry_run)


if __name__ == "__main__":
    main()
