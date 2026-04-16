#!/usr/bin/env python3
"""Migrate pre-FHS data directories to the new naming scheme.

Renames:
  config-X     → etc%2FX       (config namespace)
  skills-X     → usr%2Flib%2Fskills%2FX
  renderer-X   → usr%2Flib%2Frenderer%2FX
  sys-health   → var%2Flog%2Fhealth
  sync-log     → var%2Flog%2Fsync
  sys-metrics  → var%2Flog%2Fmetrics
  sys-alerts   → var%2Flog%2Falerts
  sys-tasks    → var%2Fspool%2Ftasks

Run: python scripts/migrate-fhs.py [DATA_DIR]
Default DATA_DIR: ./data
"""
import os, sys
from pathlib import Path

DATA = Path(sys.argv[1] if len(sys.argv) > 1 else os.getenv("ELASTIK_DATA", "data"))

# Static renames
RENAMES = {
    "sys-health": "var%2Flog%2Fhealth",
    "sync-log": "var%2Flog%2Fsync",
    "sys-metrics": "var%2Flog%2Fmetrics",
    "sys-alerts": "var%2Flog%2Falerts",
    "sys-tasks": "var%2Fspool%2Ftasks",
}

# Pattern renames (prefix-based)
PREFIXES = [
    ("config-", "etc%2F"),           # config-cdn → etc%2Fcdn
    ("skills-", "usr%2Flib%2Fskills%2F"),   # skills-sse → usr%2Flib%2Fskills%2Fsse
    ("renderer-", "usr%2Flib%2Frenderer%2F"),  # renderer-foo → usr%2Flib%2Frenderer%2Ffoo
]

if not DATA.exists():
    print(f"Data dir not found: {DATA}")
    sys.exit(1)

moved = 0
for d in sorted(DATA.iterdir()):
    if not d.is_dir() or not (d / "universe.db").exists():
        continue
    old_name = d.name
    new_name = RENAMES.get(old_name)
    if not new_name:
        for prefix, replacement in PREFIXES:
            if old_name.startswith(prefix):
                new_name = replacement + old_name[len(prefix):]
                break
    if not new_name:
        continue
    target = DATA / new_name
    if target.exists():
        print(f"  skip {old_name} → {new_name} (target exists)")
        continue
    d.rename(target)
    print(f"  {old_name} → {new_name}")
    moved += 1

print(f"\nmigrated {moved} worlds in {DATA}")
