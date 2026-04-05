"""Build elastik.pyz — single-file distribution.

Usage: python scripts/build-pyz.py
Output: dist/elastik.pyz

First run extracts to CWD, subsequent runs use existing files.
  python elastik.pyz          # extract + start
  python elastik.pyz          # just start (files already there)
"""
import os, shutil, tempfile, zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# Files to bundle
CORE_FILES = ["server.py", "plugins.py", "boot.py", "index.html", "openapi.json", "sw.js"]
DIRS = {
    "plugins/available": "plugins/available",
    "skills": "skills",
    "renderers": "renderers",
}

MAIN = '''\
"""elastik bootstrap — extract bundled files to CWD, then run server."""
import os, sys, zipfile

pyz = os.path.dirname(__file__) or "."
cwd = os.getcwd()

# Extract from zip on first run (skip files that already exist)
if zipfile.is_zipfile(sys.argv[0]):
    with zipfile.ZipFile(sys.argv[0]) as zf:
        for info in zf.infolist():
            name = info.filename
            if name == "__main__.py" or name.endswith("/"):
                continue
            target = os.path.join(cwd, name)
            if not os.path.exists(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                print(f"  extracted: {name}")

# Run boot.py from CWD (full system with plugins)
os.chdir(cwd)
boot_path = os.path.join(cwd, "boot.py")
if not os.path.exists(boot_path):
    boot_path = os.path.join(cwd, "server.py")  # fallback
sys.argv[0] = boot_path
g = {"__file__": boot_path, "__name__": "__main__", "__builtins__": __builtins__}
exec(compile(open(boot_path, encoding="utf-8").read(), boot_path, "exec"), g)
'''

def build():
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "elastik"
        pkg.mkdir()

        # Write __main__.py
        (pkg / "__main__.py").write_text(MAIN, encoding="utf-8")

        # Copy core files
        for f in CORE_FILES:
            src = ROOT / f
            if src.exists():
                shutil.copy2(src, pkg / f)

        # Copy directories
        for src_dir, dst_dir in DIRS.items():
            src = ROOT / src_dir
            if not src.exists():
                continue
            dst = pkg / dst_dir
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file() and not f.name.startswith("_"):
                    shutil.copy2(f, dst / f.name)

        # Build .pyz
        DIST.mkdir(exist_ok=True)
        output = DIST / "elastik.pyz"
        zipapp.create_archive(pkg, target=output, interpreter="/usr/bin/env python3")
        size_kb = output.stat().st_size / 1024
        print(f"  built: {output} ({size_kb:.0f} KB)")

        # List contents
        import zipfile
        with zipfile.ZipFile(output) as zf:
            for info in zf.infolist():
                print(f"    {info.filename:<40} {info.file_size:>8}")

if __name__ == "__main__":
    build()
