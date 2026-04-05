"""plugins.py — Plugin load/unload/dispatch/cron. Peacetime luxury."""
import asyncio, hashlib, json, os, re, secrets, time
from pathlib import Path

import server

PLUGINS = Path("plugins")
_ROOT = Path(__file__).resolve().parent
_LOCK = _ROOT / "plugins.lock"
_lock_hashes = {}  # rel_path → expected sha256, loaded once


def _load_lock():
    """Parse plugins.lock once into memory."""
    if _lock_hashes or not _LOCK.exists(): return
    for line in _LOCK.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip(): continue
        h, rel = line.split("  ", 1)
        _lock_hashes[rel] = h


def _verify_plugin(path):
    """Check a file against plugins.lock. No lock file = pass."""
    _load_lock()
    if not _lock_hashes: return True  # no lock = first run
    rel = path.resolve().relative_to(_ROOT).as_posix()
    expected = _lock_hashes.get(rel)
    if not expected: return True  # not in lock = user-added, pass
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return actual == expected
APPROVE_TOKEN = os.getenv("ELASTIK_APPROVE_TOKEN", "") or secrets.token_hex(16)
_DANGEROUS_PLUGINS = {"exec", "fs"}
_cron_tasks = {}   # name → {interval, handler, last_run}
_start_time = time.time()

# ── Mode system — environment detection × user intent ───────────────
# Environment ceiling: container=2, bare metal=1. User cannot exceed it.
IN_CONTAINER = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv") or os.getenv("CONTAINER") == "1"
_ENV_CEILING = 2 if IN_CONTAINER else 1
_USER_MODE = int(os.getenv("ELASTIK_MODE", "0"))  # 0 = auto
MODE = min(_USER_MODE, _ENV_CEILING) if _USER_MODE else _ENV_CEILING
# MODE 1: executor  — read/write worlds, use plugins. admin/config/dangerous locked.
# MODE 2: autonomous — approve token unlocks admin/config. dangerous plugins allowed.


def load_plugin(name):
    """Load or reload a single plugin by name. Returns True on success."""
    if not server._VALID_NAME.match(name):
        print(f"  rejected invalid plugin name: {name}"); return False
    if name in _DANGEROUS_PLUGINS and MODE < 2:
        print(f"  ! {name} blocked -- mode {MODE} (need mode 2 = container)"); return False
    f = PLUGINS / f"{name}.py"
    src = PLUGINS / "available" / f"{name}.py"
    if src.exists():
        # Verify against lock before trusting available/ source
        if not _verify_plugin(src):
            print(f"  ! {name} blocked -- checksum mismatch in plugins.lock"); return False
        PLUGINS.mkdir(exist_ok=True)
        new_text = src.read_text(encoding="utf-8")
        old_text = f.read_text(encoding="utf-8") if f.exists() else ""
        if new_text != old_text:
            f.write_text(new_text, encoding="utf-8")
            print(f"  updated from available: {name}")
    elif not f.exists():
        print(f"  not found: {name}"); return False
    try:
        async def _call(route, method="POST", body=b"", params=None):
            """Plugin service binding — call another plugin's handler directly."""
            h = server._plugins.get(route)
            if not h: return {"error": f"route {route} not found"}
            return await h(method, body, params or {})
        _injectable = {
            "load_plugin": load_plugin, "unload_plugin": unload_plugin,
            "_plugins": server._plugins, "_plugin_meta": server._plugin_meta,
            "_cron_tasks": _cron_tasks, "_start_time": _start_time,
        }
        ns = {"__file__": str(f), "_ROOT": Path(server.__file__).resolve().parent,
              "conn": server.conn, "log_event": server.log_event, "_call": _call}
        text = f.read_text(encoding="utf-8")
        needs_match = re.search(r'NEEDS\s*=\s*\[([^\]]*)\]', text)
        if needs_match:
            needed = [s.strip().strip('"').strip("'") for s in needs_match.group(1).split(",") if s.strip()]
            ns.update({k: _injectable[k] for k in needed if k in _injectable})
        exec(text, ns)
        # Remove old routes for this plugin
        old = next((m for m in server._plugin_meta if m["name"] == name), None)
        if old:
            for r in old["routes"]: server._plugins.pop(r, None)
            server._plugin_meta[:] = [m for m in server._plugin_meta if m["name"] != name]
        # Register new routes
        routes = list(ns.get("ROUTES", {}).keys())
        for path, handler in ns.get("ROUTES", {}).items():
            server._plugins[path] = handler
        if "AUTH_MIDDLEWARE" in ns: server._auth = ns["AUTH_MIDDLEWARE"]
        server._plugin_meta.append({"name": name, "description": ns.get("DESCRIPTION", ""),
            "routes": routes, "params": ns.get("PARAMS_SCHEMA", {}), "ops": ns.get("OPS_SCHEMA", [])})
        # Auto-create skills world from plugin SKILL field
        skill_doc = ns.get("SKILL", "")
        if skill_doc:
            c = server.conn(f"skills-{name.replace('_', '-')}")
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (skill_doc,))
            c.commit()
            print(f"  skill: skills-{name.replace('_', '-')}")
        # Auto-register cron task
        if "CRON" in ns and "CRON_HANDLER" in ns:
            _cron_tasks[name] = {"interval": int(ns["CRON"]), "handler": ns["CRON_HANDLER"], "last_run": time.time()}
            print(f"  cron: {name} every {ns['CRON']}s")
        print(f"  loaded: {name} ({routes})")
        return True
    except Exception as e:
        print(f"  error loading {name}: {e}")
        return False


def unload_plugin(name):
    """Unload a plugin — remove its routes."""
    meta = next((m for m in server._plugin_meta if m["name"] == name), None)
    if not meta: print(f"  not loaded: {name}"); return
    for r in meta["routes"]: server._plugins.pop(r, None)
    if name == "auth" or "auth" in meta.get("description", "").lower(): server._auth = None
    _sync_actions_remove(name, meta["routes"])
    # Auto-clear skills world
    skill_world = f"skills-{name.replace('_', '-')}"
    try:
        if (server.DATA / skill_world).exists():
            c = server.conn(skill_world)
            c.execute("UPDATE stage_meta SET stage_html='',version=version+1,updated_at=datetime('now') WHERE id=1")
            c.commit()
            print(f"  skill cleared: {skill_world}")
    except Exception as e: print(f"  warn: skill cleanup failed for {skill_world}: {e}")
    _cron_tasks.pop(name, None)
    server._plugin_meta[:] = [m for m in server._plugin_meta if m["name"] != name]
    print(f"  unloaded: {name}")


def _sync_actions_add(name):
    """Register a plugin's routes in config-actions whitelist."""
    meta = next((m for m in server._plugin_meta if m["name"] == name), None)
    if not meta or not meta["routes"]: return
    c = server.conn("config-actions")
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
    existing = set(l.strip() for l in old.splitlines() if l.strip())
    added = [r for r in meta["routes"] if r not in existing]
    if added:
        new = old.rstrip("\n") + "\n" + "\n".join(added) + "\n" if old.strip() else "\n".join(added) + "\n"
        c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (new,))
        c.commit()


def _sync_actions_remove(name, routes):
    """Remove a plugin's routes from config-actions whitelist."""
    if not routes: return
    try:
        c = server.conn("config-actions")
        old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        remove = set(routes)
        lines = [l for l in old.splitlines() if l.strip() and l.strip() not in remove]
        c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
                  ("\n".join(lines) + "\n" if lines else "",))
        c.commit()
    except Exception as e: print(f"  warn: actions cleanup failed: {e}")


def load_plugins():
    """Load all plugins at startup. Install defaults if empty."""
    installed = [f for f in PLUGINS.glob("*.py") if not f.name.startswith("_")] if PLUGINS.exists() else []
    if not installed:
        available = PLUGINS / "available"
        if available.exists():
            PLUGINS.mkdir(exist_ok=True)
            for name in ["admin.py", "auth.py", "info.py"]:
                src = available / name
                if src.exists():
                    (PLUGINS / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                    print(f"  installed default: {name}")
    if not PLUGINS.exists(): return
    for f in PLUGINS.glob("*.py"):
        if not f.name.startswith("_"): load_plugin(f.stem)


async def handle_propose(method, body, params):
    """POST /plugins/propose — submit a plugin proposal."""
    try: b = json.loads(body)
    except (json.JSONDecodeError, TypeError): return {"error": "invalid json", "_status": 400}
    server.log_event("default", "plugin_proposed", b)
    name = b.get("name", "unknown")
    desc = b.get("description", "")
    code = b.get("code", "")
    summary = f"\n---\n## {name}\n{desc}\n```python\n{code}\n```\n"
    c = server.conn("plugin-proposals")
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (old + summary,))
    c.commit()
    return {"ok": True}


async def handle_approve(method, body, params):
    """POST /plugins/approve — approve and install a plugin (requires approve token)."""
    import hmac as _hmac
    try: b = json.loads(body)
    except (json.JSONDecodeError, TypeError): return {"error": "invalid json", "_status": 400}
    scope = params.get("_scope", {})
    tok = dict(scope.get("headers", [])).get(b"x-approve-token", b"").decode()
    if not _hmac.compare_digest(tok, APPROVE_TOKEN):
        return {"error": "invalid token", "_status": 403}
    n, code = b.get("name", ""), b.get("code", "")
    if n and not server._VALID_NAME.match(n):
        return {"error": "invalid plugin name", "_status": 400}
    if n and code:
        PLUGINS.mkdir(exist_ok=True)
        (PLUGINS / f"{n}.py").write_text(code)
        load_plugin(n); _sync_actions_add(n)
        server.log_event("default", "plugin_approved", {"name": n})
    return {"ok": True}


def register_plugin_routes():
    """Register /plugins/propose and /plugins/approve in server._plugins."""
    server._plugins["/plugins/propose"] = handle_propose
    server._plugins["/plugins/approve"] = handle_approve


async def cron_loop():
    """Background cron loop — runs plugin CRON handlers."""
    while True:
        await asyncio.sleep(1)
        now = time.time()
        for name, task in list(_cron_tasks.items()):
            if now - task["last_run"] >= task["interval"]:
                try:
                    await task["handler"]()
                    task["last_run"] = now
                except Exception as e:
                    print(f"  cron {name}: {e}")
