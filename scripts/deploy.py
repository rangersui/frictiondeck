"""Deploy/export renderers and skills. Zero token cost.

Usage:
  python scripts/deploy.py deploy                        # deploy all (renderers + skills)
  python scripts/deploy.py deploy --renderers            # deploy renderers only
  python scripts/deploy.py deploy --skills               # deploy skills only
  python scripts/deploy.py deploy renderer-health        # deploy one renderer
  python scripts/deploy.py export                        # export all renderers
  python scripts/deploy.py export renderer-health        # export one renderer
"""
import json, os, sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
URL = os.getenv("ELASTIK_URL", "http://localhost:3005")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
if not TOKEN:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ELASTIK_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()


def deploy_file(world_name, data):
    req = Request(f"{URL}/{world_name}/write", data=data, method="POST",
                  headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        r = urlopen(req)
        print(f"  {world_name} -> {r.status}")
    except URLError as e:
        print(f"  {world_name} -> FAILED: {e}")


def deploy_renderer(file):
    deploy_file(file.stem, file.read_bytes())


def deploy_skill(file):
    world_name = f"skills-{file.stem}"
    deploy_file(world_name, file.read_bytes())


def export(name):
    file = ROOT / "renderers" / f"{name}.html"
    try:
        data = json.loads(urlopen(f"{URL}/{name}/read").read())
        html = data.get("stage_html", "")
        if html:
            file.write_text(html, encoding="utf-8")
            print(f"  {name} -> {file.relative_to(ROOT)}")
        else:
            print(f"  {name} -> (empty, skipped)")
    except URLError as e:
        print(f"  {name} -> FAILED: {e}")


def get_renderers():
    try:
        data = json.loads(urlopen(f"{URL}/info").read())
        return data.get("renderers", [])
    except Exception:
        return []


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("deploy", "export"):
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]
    rest = sys.argv[2:]

    if action == "deploy":
        # Parse flags
        do_renderers = "--renderers" in rest or "--all" in rest
        do_skills = "--skills" in rest or "--all" in rest
        named = [a for a in rest if not a.startswith("--")]

        # No flags and no named target = deploy all
        if not do_renderers and not do_skills and not named:
            do_renderers = do_skills = True

        if named:
            # Deploy specific named target (renderer)
            for target in named:
                f = ROOT / "renderers" / f"{target}.html"
                if f.exists():
                    deploy_renderer(f)
                else:
                    print(f"  {target}.html not found")
        else:
            if do_renderers:
                rdir = ROOT / "renderers"
                files = sorted(rdir.glob("renderer-*.html")) if rdir.exists() else []
                print(f"deploying {len(files)} renderers -> {URL}")
                for f in files:
                    deploy_renderer(f)

            if do_skills:
                sdir = ROOT / "skills"
                files = sorted(sdir.glob("*.md")) if sdir.exists() else []
                print(f"deploying {len(files)} skills -> {URL}")
                for f in files:
                    deploy_skill(f)

            print("done")

    elif action == "export":
        target = named[0] if (named := [a for a in rest if not a.startswith("--")]) else None
        if target:
            export(target)
        else:
            print(f"exporting renderers from {URL}")
            for name in get_renderers():
                export(name)
            print("done")
