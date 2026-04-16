"""info plugin — /info endpoint. System introspection."""
DESCRIPTION = "System info and plugin registry"
NEEDS = ["_plugin_meta", "_plugins"]
from pathlib import Path

PLUGINS = Path("plugins")


async def handle_info(method, body, params):
    DATA = Path("data")
    skills = ""
    try:
        if (DATA / "usr%2Flib%2Fskills%2Fcore").exists():
            skills = conn("usr/lib/skills/core").execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
    except Exception as e: print(f"  warn: usr/lib/skills/core read failed: {e}")
    # SKILLS.md removed — skills live in worlds now
    auth_name = next((p["name"] for p in _plugin_meta if p["name"] == "auth" or "auth" in p.get("description","").lower()), None)
    renderers, worlds = [], []
    if DATA.exists():
        for d in sorted(DATA.iterdir()):
            if d.is_dir() and (d / "universe.db").exists():
                logical = d.name.replace("%2F", "/")
                if logical.startswith("usr/lib/renderer/"): renderers.append(logical)
                elif not logical.startswith(("etc/", "usr/", "var/")): worlds.append(logical)
    cdn_raw = ""
    try:
        if (DATA / "etc%2Fcdn").exists():
            r = conn("etc/cdn").execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            if r: cdn_raw = r["stage_html"]
    except Exception as e: print(f"  warn: CDN config read failed: {e}")
    cdn = [d.strip() for d in cdn_raw.splitlines() if d.strip()] if cdn_raw.strip() else ["* (all HTTPS)"]
    available = []
    avail_dir = PLUGINS / "available"
    if avail_dir.exists():
        loaded = {m["name"] for m in _plugin_meta}
        for f in sorted(avail_dir.glob("*.py")):
            if f.stem not in loaded:
                desc = ""
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.startswith("DESCRIPTION"):
                        try: desc = line.split("=", 1)[1].strip().strip('"').strip("'")
                        except Exception: pass
                        break
                available.append({"name": f.stem, "description": desc})
    skill_worlds = [d.name.replace("%2F", "/") for d in sorted(DATA.iterdir())
                    if d.is_dir() and d.name.startswith("usr%2Flib%2Fskills%2F") and (d / "universe.db").exists()] if DATA.exists() else []
    return {
        "routes": list(_plugins.keys()),
        "auth": auth_name,
        "plugins": _plugin_meta,
        "available": available,
        "renderers": renderers,
        "worlds": worlds,
        "skill_worlds": skill_worlds,
        "cdn": cdn,
        "skills": skills,
    }


ROUTES = {"/info": handle_info}
