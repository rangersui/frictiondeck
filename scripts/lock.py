#!/usr/bin/env python3
"""lock.py — Generate or verify plugins.lock (secure boot checksums).

Usage:
  python scripts/lock.py              # generate plugins.lock
  python scripts/lock.py --verify     # verify against plugins.lock
  python scripts/lock.py --show       # show what would be locked
  python scripts/lock.py --list       # list locked file paths (for git hooks)

Locks: server.py, plugins.py, and plugins/available/*.py (shipped code only).
plugins/ is user-installed — not locked, not shipped.
"""
import hashlib, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "plugins.lock"

def _targets():
    """Files to lock — core + all plugin .py files."""
    core = ["server.py", "plugins.py"]
    files = []
    for name in core:
        f = ROOT / name
        if f.exists(): files.append(f)
    # Only lock plugins/available/ (shipped code).
    # plugins/ is user-installed — changes on every machine.
    avail = ROOT / "plugins" / "available"
    if avail.exists():
        for f in sorted(avail.glob("*.py")):
            if not f.name.startswith("_"):
                files.append(f)
    return files

def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def generate():
    files = _targets()
    lines = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        lines.append(f"{_sha256(f)}  {rel}")
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  locked {len(files)} files -> plugins.lock")
    for line in lines:
        h, name = line.split("  ", 1)
        print(f"    {h[:12]}..  {name}")

def verify():
    if not LOCK.exists():
        print("  plugins.lock not found — run: python scripts/lock.py")
        return False
    ok, failed, missing = True, [], []
    for line in LOCK.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip(): continue
        expected, rel = line.split("  ", 1)
        f = ROOT / rel
        if not f.exists():
            missing.append(rel); ok = False; continue
        actual = _sha256(f)
        if actual != expected:
            failed.append((rel, expected[:12], actual[:12])); ok = False
    if ok:
        print("  secure boot: all checksums match")
    else:
        if failed:
            print("  ! CHECKSUM MISMATCH:")
            for rel, exp, act in failed:
                print(f"    {rel}  expected {exp}..  got {act}..")
        if missing:
            print("  ! MISSING FILES:")
            for rel in missing:
                print(f"    {rel}")
    return ok

if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(0 if verify() else 1)
    elif "--list" in sys.argv:
        for f in _targets():
            print(f.relative_to(ROOT).as_posix())
    elif "--show" in sys.argv:
        for f in _targets():
            print(f"  {_sha256(f)[:12]}..  {f.relative_to(ROOT).as_posix()}")
    else:
        generate()
