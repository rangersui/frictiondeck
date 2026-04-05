"""boot.py — Full system startup. `python boot.py` = server + plugins + cron.

  python server.py  → bare protocol (~258 lines, no plugins)
  python boot.py    → full system (plugins, cron, /info)
  python elastik.py → boot + hardware detect + browser
"""
import hashlib, os, sys
from pathlib import Path

import server
import plugins

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "plugins.lock"


def _verify_lock():
    """Secure boot — verify file checksums against plugins.lock."""
    if not LOCK.exists(): return True  # no lock file = first run, skip
    ok, failed, missing = True, [], []
    for line in LOCK.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip(): continue
        expected, rel = line.split("  ", 1)
        f = ROOT / rel
        if not f.exists():
            missing.append(rel); ok = False; continue
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != expected:
            failed.append(rel); ok = False
    if not ok:
        print("\n  ! SECURE BOOT FAILED")
        for rel in failed:  print(f"    MODIFIED: {rel}")
        for rel in missing: print(f"    MISSING:  {rel}")
        print("  regenerate: python scripts/lock.py")
        print("  or delete plugins.lock to skip verification\n")
    return ok


def _sync_dir(directory, glob_pattern, world_name_fn, label):
    """Sync files from a directory to worlds. Only writes if content changed."""
    d = Path(__file__).resolve().parent / directory
    if not d.exists(): return
    for f in sorted(d.glob(glob_pattern)):
        name = world_name_fn(f)
        content = f.read_text(encoding="utf-8")
        c = server.conn(name)
        old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
        if old["stage_html"] != content:
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (content,))
            c.commit()
            print(f"  {label}: synced {name}")


def _sync_map():
    """Sync skills/map.md + append undocumented worlds. map.md is source of truth."""
    f = Path(__file__).resolve().parent / "skills" / "map.md"
    if not f.exists(): return
    text = f.read_text(encoding="utf-8")
    documented = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            name = stripped.split("—")[0].split("–")[0].strip()
            if name: documented.add(name)
    suffix = ""
    for db in sorted(server.DATA.iterdir()) if server.DATA.exists() else []:
        if db.is_dir() and db.name not in documented:
            suffix += f"{db.name:<21}— (undocumented)\n"
    content = text.rstrip("\n") + "\n"
    if suffix: content += suffix
    c = server.conn("map")
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    if old["stage_html"] != content:
        c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (content,))
        c.commit()
        print(f"  map: synced ({len(documented)} documented" + (f", {suffix.count(chr(10))} undocumented)" if suffix else ")"))


if __name__ == "__main__":
    # Secure boot — verify checksums before loading anything
    if not _verify_lock():
        sys.exit(1)

    # Load plugins and sync content
    plugins.load_plugins()
    plugins.register_plugin_routes()
    _sync_dir("skills", "*.md", lambda f: f"skills-{f.stem}", "skills")
    _sync_dir("renderers", "renderer-*.html", lambda f: f.stem, "renderers")
    _sync_map()

    if not server.AUTH_TOKEN:
        print("\n  ! ELASTIK_TOKEN not set. Refusing to start in public mode.")
        print("  Set ELASTIK_TOKEN in .env or environment.\n")
        sys.exit(1)

    env = "container" if plugins.IN_CONTAINER else "bare metal"
    mode_label = {1: "executor", 2: "autonomous"}
    print(f"\n  elastik -> http://{server.HOST}:{server.PORT}")
    print(f"  environment:   {env} (ceiling={plugins._ENV_CEILING})")
    print(f"  mode:          {plugins.MODE} ({mode_label.get(plugins.MODE, '?')})")
    print(f"  auth token:    {server.AUTH_TOKEN}")
    print(f"  approve token: {plugins.APPROVE_TOKEN}")
    if plugins.MODE < 2:
        print(f"  ! mode {plugins.MODE} -- dangerous plugins ({', '.join(plugins._DANGEROUS_PLUGINS)}) blocked")
    print()

    server.run(extra_tasks=[plugins.cron_loop()])
