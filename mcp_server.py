"""FrictionDeck v4 — MCP Server (Multi-Stage)

stdio transport. Claude Desktop spawns this as a subprocess.
All tools accept stage parameter (default: "default").
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("FRICTIONDECK_DB_DIR", os.path.join(_PROJECT_ROOT, "data"))

from mcp.server.fastmcp import FastMCP
from pipeline.config import VERSION, PERSONAL_MODE, setup_logging
from pipeline.history import init_history_db
from pipeline.stage import init_stage_db

setup_logging()
init_history_db()
init_stage_db()

from pipeline.prompts import load as _load_prompt
SERVER_INSTRUCTIONS = _load_prompt(
    "server_instructions.md",
    mode="personal" if PERSONAL_MODE else "enterprise",
)

mcp = FastMCP("FrictionDeck", instructions=SERVER_INSTRUCTIONS)


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# READ
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_world_state(stage: str = "default") -> str:
    """Get complete world state for context recovery.

    Call this first when starting a new conversation to understand
    what's on the Stage, what's been committed, and recent history events.
    """
    from pipeline.mcp_adapter import get_world_state as _get
    return _json(_get(stage))


@mcp.tool()
def get_stage_state(stage: str = "default") -> str:
    """Get current Stage state: HTML canvas content, judgments, and version."""
    from pipeline.mcp_adapter import mcp_get_stage_state as _get
    return _json(_get(stage))


@mcp.tool()
def get_stage_html(stage: str = "default") -> str:
    """Get current Stage HTML (full DOM). Use when you need to see the whole canvas."""
    from pipeline.mcp_adapter import mcp_get_stage_html as _get
    return _json(_get(stage))


@mcp.tool()
def get_stage_summary(stage: str = "default") -> str:
    """Get structured summary (judgments + version, no HTML). Saves tokens."""
    from pipeline.mcp_adapter import mcp_get_stage_summary as _get
    return _json(_get(stage))


@mcp.tool()
def wait_for_stage_update(last_known_version: int, stage: str = "default") -> str:
    """Check if the Stage has changed since last_known_version.

    Returns the diff if version has advanced. Returns {changed: false}
    if nothing changed.
    """
    from pipeline.mcp_adapter import wait_for_stage_update as _wait
    return _json(_wait(last_known_version, stage))


@mcp.tool()
def search_commits(
    query: str = "", engineer: str = "",
    date_from: str = "", date_to: str = "",
    stage: str = "default",
) -> str:
    """Search committed (sealed) judgments. Filter by text, engineer, date range."""
    from pipeline.mcp_adapter import search_commits as _search
    return _json(_search(
        query=query or None, engineer=engineer or None,
        date_from=date_from or None, date_to=date_to or None, stage=stage,
    ))


@mcp.tool()
def get_history(
    limit: int = 50, offset: int = 0,
    event_type: str = "", stage: str = "default",
) -> str:
    """Get history events. Filter by event_type if needed."""
    from pipeline.mcp_adapter import get_history as _get
    return _json(_get(limit=limit, offset=offset, event_type=event_type or None, stage=stage))


@mcp.tool()
def get_proxy_whitelist() -> str:
    """List whitelisted proxy services that Stage JS can fetch via /proxy/<service>/."""
    from pipeline.config import PROXY_WHITELIST, PERSONAL_MODE
    return _json({
        "mode": "personal" if PERSONAL_MODE else "enterprise",
        "services": {k: v for k, v in PROXY_WHITELIST.items()},
        "usage": "fetch('/proxy/<service>/<path>')",
    })


@mcp.tool()
def get_csp_whitelist() -> str:
    """List whitelisted CDN domains allowed by Content-Security-Policy."""
    from pipeline.mcp_adapter import get_csp_whitelist as _get
    return _json(_get())


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-STAGE
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_stages() -> str:
    """List all available Stages with name, version, and last update time."""
    from pipeline.mcp_adapter import mcp_list_stages as _list
    return _json(_list())


@mcp.tool()
def create_stage(name: str) -> str:
    """Create a new Stage. Initializes empty stage.db and history.db.

    name: Alphanumeric + hyphens + underscores. e.g. "albon", "venice-2026"
    """
    from pipeline.mcp_adapter import mcp_create_stage as _create
    return _json(_create(name))


# ═══════════════════════════════════════════════════════════════════════════
# PLUGINS
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def propose_plugin(
    name: str, code: str, description: str,
    permissions: str = "[]", stage: str = "default",
) -> str:
    """Propose a backend plugin for human approval.

    name: Plugin name (alphanumeric + hyphens + underscores)
    code: Full Python source code of the plugin
    description: What the plugin does
    permissions: JSON array of permission strings
    """
    from pipeline.mcp_adapter import mcp_propose_plugin as _propose
    perms = json.loads(permissions) if isinstance(permissions, str) else permissions
    return _json(_propose(name, code, description, perms, stage))


@mcp.tool()
def list_plugin_proposals(stage: str = "default") -> str:
    """List pending plugin proposals awaiting human approval."""
    from pipeline.mcp_adapter import mcp_list_plugin_proposals as _list
    return _json(_list(stage))


# ═══════════════════════════════════════════════════════════════════════════
# DOM operations
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def query_stage(selector: str, stage: str = "default") -> str:
    """Query the Stage for elements matching a CSS selector (read-only)."""
    from pipeline.mcp_adapter import query_stage as _query
    return _json(_query(selector, stage))


@mcp.tool()
def mutate_stage(selector: str, new_html: str, stage: str = "default") -> str:
    """Replace an element on the Stage matching the CSS selector with new HTML."""
    from pipeline.mcp_adapter import mutate_stage as _mutate
    return _json(_mutate(selector, new_html, stage))


@mcp.tool()
def append_stage(parent_selector: str, html: str, stage: str = "default") -> str:
    """Append HTML to an element on the Stage matching parent_selector."""
    from pipeline.mcp_adapter import append_stage as _append
    return _json(_append(parent_selector, html, stage))


# ═══════════════════════════════════════════════════════════════════════════
# Constraint tools
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def promote_to_judgment(claim_text: str, params: str = "[]", stage: str = "default") -> str:
    """Promote a claim to a judgment object (viscous state).

    claim_text: The precise factual claim being made.
    params: JSON array, e.g. [{"name":"power","value":300,"unit":"W"}]
    """
    from pipeline.mcp_adapter import mcp_promote_to_judgment as _promote
    params_list = json.loads(params) if isinstance(params, str) else params
    return _json(_promote(claim_text, params_list, stage))


@mcp.tool()
def flag_negative_space(description: str, severity: str = "medium", stage: str = "default") -> str:
    """Flag something MISSING from the analysis.

    Only AI can flag. Only humans can dismiss.
    """
    from pipeline.mcp_adapter import flag_negative_space as _flag
    return _json(_flag(description, severity, stage))


@mcp.tool()
def propose_commit(judgment_ids: str, message: str, stage: str = "default") -> str:
    """Propose a commit of judgment objects.

    You can only PROPOSE. The human must APPROVE.
    judgment_ids: JSON array of judgment IDs to commit.
    """
    from pipeline.mcp_adapter import propose_commit as _propose
    ids = json.loads(judgment_ids) if isinstance(judgment_ids, str) else judgment_ids
    return _json(_propose(ids, message, stage))


if __name__ == "__main__":
    mcp.run(transport="stdio")
