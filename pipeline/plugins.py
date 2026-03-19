"""Elastik OS — Plugin Approval Queue

AI proposes plugins. Human approves. Hot-loaded into FastAPI.

Plugin format (saved to plugins/<name>.py):
    ROUTES = {"/proxy/<service>/<path>": handler_func}
    PROXY_WHITELIST = {"service": "https://target.com"}
    DESCRIPTION = "What this plugin does"
    PERMISSIONS = ["list: /home/user", "read: /home/user"]
    async def handler_func(request): ...
"""

import importlib.util
import logging
import os
from pathlib import Path

from pipeline.constants import EventType

logger = logging.getLogger("frictiondeck.plugins")

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
PLUGINS_DIR = os.path.join(_PROJECT_ROOT, "plugins")


# ── Propose ──────────────────────────────────────────────────────────────


def propose_plugin(
    name: str,
    code: str,
    description: str,
    permissions: list[str] | None = None,
    stage: str = "default",
) -> dict:
    """AI proposes a plugin. Stored in history.db as pending."""
    from pipeline.history import log_event

    clean = "".join(c for c in name if c.isalnum() or c in "-_")
    if not clean:
        return {"error": "Invalid plugin name"}

    # Check if already installed
    plugin_path = os.path.join(PLUGINS_DIR, f"{clean}.py")
    if os.path.exists(plugin_path):
        return {"error": f"Plugin '{clean}' already installed"}

    event_id = log_event(
        EventType.PLUGIN_PROPOSED,
        actor="ai",
        pathway="mcp",
        payload={
            "name": clean,
            "code": code,
            "description": description,
            "permissions": permissions or [],
        },
        stage=stage,
    )

    logger.info("plugin proposed  name=%s  stage=%s", clean, stage)
    return {
        "proposal_id": event_id,
        "name": clean,
        "description": description,
        "status": "pending_approval",
    }


def list_plugin_proposals(stage: str = "default") -> list[dict]:
    """List pending plugin proposals from history."""
    from pipeline.history import get_events
    import json

    proposals = get_events(event_type=EventType.PLUGIN_PROPOSED, stage=stage, limit=100)
    approved = get_events(event_type=EventType.PLUGIN_APPROVED, stage=stage, limit=100)
    rejected = get_events(event_type=EventType.PLUGIN_REJECTED, stage=stage, limit=100)

    resolved_ids = set()
    for e in approved + rejected:
        payload = json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
        resolved_ids.add(payload.get("proposal_id", ""))

    pending = []
    for p in proposals:
        if p["event_id"] not in resolved_ids:
            payload = json.loads(p["payload"]) if isinstance(p["payload"], str) else p["payload"]
            pending.append({
                "proposal_id": p["event_id"],
                "name": payload.get("name", ""),
                "description": payload.get("description", ""),
                "permissions": payload.get("permissions", []),
                "code_length": len(payload.get("code", "")),
                "timestamp": p["timestamp"],
            })
    return pending


# ── Approve / Reject (human only) ────────────────────────────────────────


def approve_plugin(proposal_id: str, app=None, stage: str = "default") -> dict:
    """Human approves plugin. Write to disk + hot-load."""
    from pipeline.history import get_events, log_event
    import json

    # Find the proposal
    events = get_events(event_type=EventType.PLUGIN_PROPOSED, stage=stage, limit=200)
    proposal = None
    for e in events:
        if e["event_id"] == proposal_id:
            proposal = e
            break

    if not proposal:
        return {"error": f"Proposal not found: {proposal_id}"}

    payload = json.loads(proposal["payload"]) if isinstance(proposal["payload"], str) else proposal["payload"]
    name = payload["name"]
    code = payload["code"]

    # Write plugin file
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    plugin_path = os.path.join(PLUGINS_DIR, f"{name}.py")
    with open(plugin_path, "w", encoding="utf-8") as f:
        f.write(code)

    # Hot-load if app provided
    loaded_routes = []
    if app:
        loaded_routes = _hot_load_plugin(name, app)

    # Update proxy whitelist
    whitelist_entries = _load_plugin_whitelist(name)

    log_event(
        EventType.PLUGIN_APPROVED,
        actor="user",
        pathway="gui",
        payload={
            "proposal_id": proposal_id,
            "name": name,
            "routes": loaded_routes,
            "whitelist": whitelist_entries,
        },
        stage=stage,
    )

    logger.info("plugin approved  name=%s  routes=%d", name, len(loaded_routes))
    return {
        "name": name,
        "status": "approved",
        "routes": loaded_routes,
        "whitelist": whitelist_entries,
    }


def reject_plugin(proposal_id: str, reason: str, stage: str = "default") -> dict:
    """Human rejects plugin proposal."""
    from pipeline.history import log_event

    log_event(
        EventType.PLUGIN_REJECTED,
        actor="user",
        pathway="gui",
        payload={"proposal_id": proposal_id, "reason": reason[:500]},
        stage=stage,
    )

    return {"proposal_id": proposal_id, "status": "rejected", "reason": reason}


# ── Plugin loading ───────────────────────────────────────────────────────


def _load_module(name: str):
    """Import a plugin module from plugins/<name>.py."""
    plugin_path = os.path.join(PLUGINS_DIR, f"{name}.py")
    if not os.path.exists(plugin_path):
        return None
    spec = importlib.util.spec_from_file_location(f"plugins.{name}", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hot_load_plugin(name: str, app) -> list[str]:
    """Load a single plugin's routes into FastAPI app."""
    mod = _load_module(name)
    if not mod:
        return []

    routes = getattr(mod, "ROUTES", {})
    loaded = []
    for path, handler in routes.items():
        methods = getattr(handler, "_methods", ["GET"])
        if isinstance(methods, str):
            methods = [methods]
        app.add_api_route(path, handler, methods=methods)
        loaded.append(path)
        logger.info("plugin route registered  plugin=%s  path=%s", name, path)

    return loaded


def _load_plugin_whitelist(name: str) -> dict[str, str]:
    """Read PROXY_WHITELIST from a plugin and merge into config."""
    mod = _load_module(name)
    if not mod:
        return {}

    whitelist = getattr(mod, "PROXY_WHITELIST", {})
    if whitelist:
        from pipeline import config
        config.PROXY_WHITELIST.update(whitelist)
        logger.info("plugin whitelist merged  plugin=%s  services=%s", name, list(whitelist.keys()))

    return whitelist


def load_all_plugins(app=None) -> list[str]:
    """Scan plugins/ dir and load all installed plugins. Called at startup."""
    if not os.path.exists(PLUGINS_DIR):
        return []

    loaded = []
    for fname in sorted(os.listdir(PLUGINS_DIR)):
        if fname.endswith(".py") and not fname.startswith("_"):
            name = fname[:-3]
            try:
                _load_plugin_whitelist(name)
                if app:
                    routes = _hot_load_plugin(name, app)
                    logger.info("plugin loaded  name=%s  routes=%d", name, len(routes))
                loaded.append(name)
            except Exception as exc:
                logger.error("plugin load failed  name=%s  error=%s", name, exc)

    return loaded


def list_installed_plugins() -> list[dict]:
    """List installed plugins with metadata."""
    if not os.path.exists(PLUGINS_DIR):
        return []

    plugins = []
    for fname in sorted(os.listdir(PLUGINS_DIR)):
        if fname.endswith(".py") and not fname.startswith("_"):
            name = fname[:-3]
            try:
                mod = _load_module(name)
                plugins.append({
                    "name": name,
                    "description": getattr(mod, "DESCRIPTION", ""),
                    "routes": list(getattr(mod, "ROUTES", {}).keys()),
                    "whitelist": getattr(mod, "PROXY_WHITELIST", {}),
                    "permissions": getattr(mod, "PERMISSIONS", []),
                })
            except Exception as exc:
                plugins.append({"name": name, "error": str(exc)})

    return plugins
