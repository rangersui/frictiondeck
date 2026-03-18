"""FrictionDeck v4 — MCP Server

stdio transport. Claude Desktop spawns this as a subprocess.
Tools exposed via FastMCP, all routed through mcp_adapter.py.

Usage:
    python mcp_server.py
    (or configured in claude_desktop_config.json)
"""

import json
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

# Set DB dir before any imports
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("FRICTIONDECK_DB_DIR", os.path.join(_PROJECT_ROOT, "data"))

from mcp.server.fastmcp import FastMCP

from pipeline.config import VERSION, setup_logging
from pipeline.audit import init_audit_db
from pipeline.stage import init_stage_db

# ── Init ──────────────────────────────────────────────────────────────────
setup_logging()
init_audit_db()
init_stage_db()

# ── Load SERVER_INSTRUCTIONS ──────────────────────────────────────────────
from pipeline.config import PERSONAL_MODE
from pipeline.prompts import load as _load_prompt
SERVER_INSTRUCTIONS = _load_prompt(
    "server_instructions.md",
    mode="personal" if PERSONAL_MODE else "enterprise",
)

mcp = FastMCP(
    "FrictionDeck",
    instructions=SERVER_INSTRUCTIONS,
)


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# T1 — READ
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_world_state() -> str:
    """Get complete world state for context recovery.

    Call this first when starting a new conversation to understand
    what's on the Stage, what's been committed, and recent audit events.
    """
    from pipeline.mcp_adapter import get_world_state as _get
    return _json(_get())


@mcp.tool()
def get_stage_state() -> str:
    """Get current Stage state: HTML canvas content, judgments, and version."""
    from pipeline.mcp_adapter import mcp_get_stage_state as _get
    return _json(_get())


@mcp.tool()
def get_stage_html() -> str:
    """Get current Stage HTML (full DOM). Use when you need to see the whole canvas."""
    from pipeline.mcp_adapter import mcp_get_stage_html as _get
    return _json(_get())


@mcp.tool()
def get_stage_summary() -> str:
    """Get structured summary (judgments + version, no HTML). Saves tokens."""
    from pipeline.mcp_adapter import mcp_get_stage_summary as _get
    return _json(_get())


@mcp.tool()
def wait_for_stage_update(last_known_version: int) -> str:
    """Check if the Stage has changed since last_known_version.

    Returns the diff if version has advanced. Returns {changed: false}
    if nothing changed.
    """
    from pipeline.mcp_adapter import wait_for_stage_update as _wait
    return _json(_wait(last_known_version))


@mcp.tool()
def search_commits(
    query: str = "",
    engineer: str = "",
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Search committed (sealed) judgments.

    Filter by text, engineer, date range.
    """
    from pipeline.mcp_adapter import search_commits as _search
    return _json(_search(
        query=query or None,
        engineer=engineer or None,
        date_from=date_from or None,
        date_to=date_to or None,
    ))


@mcp.tool()
def get_audit_trail(
    limit: int = 50,
    offset: int = 0,
    event_type: str = "",
) -> str:
    """Get audit log entries. Filter by event_type if needed."""
    from pipeline.mcp_adapter import get_audit_trail as _get
    return _json(_get(limit=limit, offset=offset, event_type=event_type or None))


@mcp.tool()
def get_proxy_whitelist() -> str:
    """List whitelisted proxy services that Stage JS can fetch via /proxy/<service>/.

    Returns service names and their target base URLs.
    Personal mode only — enterprise mode has no proxy access from Stage.
    """
    from pipeline.config import PROXY_WHITELIST, PERSONAL_MODE
    return _json({
        "mode": "personal" if PERSONAL_MODE else "enterprise",
        "services": {k: v for k, v in PROXY_WHITELIST.items()},
        "usage": "fetch('/proxy/<service>/<path>')",
    })


@mcp.tool()
def get_csp_whitelist() -> str:
    """List whitelisted CDN domains allowed by Content-Security-Policy.

    Check this before using external libraries in your Stage HTML.
    If a CDN is not listed, tell the human to add it via /api/csp/add.
    """
    from pipeline.mcp_adapter import get_csp_whitelist as _get
    return _json(_get())


# ═══════════════════════════════════════════════════════════════════════════
# T2 — DOM operations (AI writes freely to the canvas)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def query_stage(selector: str) -> str:
    """Query the Stage for elements matching a CSS selector (read-only)."""
    from pipeline.mcp_adapter import query_stage as _query
    return _json(_query(selector))


@mcp.tool()
def mutate_stage(selector: str, new_html: str) -> str:
    """Replace an element on the Stage matching the CSS selector with new HTML."""
    from pipeline.mcp_adapter import mutate_stage as _mutate
    return _json(_mutate(selector, new_html))


@mcp.tool()
def append_stage(parent_selector: str, html: str) -> str:
    """Append HTML to an element on the Stage matching parent_selector."""
    from pipeline.mcp_adapter import append_stage as _append
    return _json(_append(parent_selector, html))



# ═══════════════════════════════════════════════════════════════════════════
# T2 — Constraint tools (controlled, state-changing)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def promote_to_judgment(
    claim_text: str,
    params: str = "[]",
) -> str:
    """Promote a claim to a judgment object (viscous state).

    The judgment enters the audit layer and can be committed + HMAC sealed.

    claim_text: The precise factual claim being made.
    params: JSON array, e.g. [{"name":"power","value":300,"unit":"W"}]
    """
    from pipeline.mcp_adapter import mcp_promote_to_judgment as _promote
    params_list = json.loads(params) if isinstance(params, str) else params
    return _json(_promote(claim_text, params_list))


@mcp.tool()
def flag_negative_space(
    description: str,
    severity: str = "medium",
) -> str:
    """Flag something MISSING from the analysis.

    Only AI can flag. Only humans can dismiss.

    description: What's missing and why it matters.
    severity: "low" | "medium" | "high" | "critical"
    """
    from pipeline.mcp_adapter import flag_negative_space as _flag
    return _json(_flag(description, severity))


# ═══════════════════════════════════════════════════════════════════════════
# T3 — PROPOSE (AI proposes, human approves via Friction Gate)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def propose_commit(
    judgment_ids: str,
    message: str,
) -> str:
    """Propose a commit of judgment objects.

    You can only PROPOSE. The human must APPROVE via the Friction Gate.

    judgment_ids: JSON array of judgment IDs to commit.
    message: Commit message describing what's being committed and why.
    """
    from pipeline.mcp_adapter import propose_commit as _propose
    ids = json.loads(judgment_ids) if isinstance(judgment_ids, str) else judgment_ids
    return _json(_propose(ids, message))


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
