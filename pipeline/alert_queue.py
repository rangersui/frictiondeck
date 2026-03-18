"""FrictionDeck v4 — Piggyback Alert Queue

Every MCP tool return carries pending_alerts. This module manages the queue.

Two sources of alerts:
  1. Stage events (GUI actions the AI should know about)
  2. Nagging (AI hasn't externalized findings in N tool calls)

Piggyback mechanism solves MCP's lack of true server→client push.
As long as AI keeps calling tools, it sees the latest alerts.
"""

import logging
from collections import deque
from datetime import datetime, UTC
from typing import Any

logger = logging.getLogger("frictiondeck.alerts")

# Alert queue — FIFO, bounded
_alerts: deque[dict[str, Any]] = deque(maxlen=100)

# Nagging state
_tool_calls_since_last_drop = 0
_NAGGING_THRESHOLD = 5


def push_alert(alert_type: str, message: str, data: dict | None = None) -> None:
    """Push an alert to the queue."""
    _alerts.append({
        "type": alert_type,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now(UTC).isoformat(),
    })


def drain_alerts() -> list[dict]:
    """Pop all pending alerts. Called by _attach_alerts()."""
    alerts = list(_alerts)
    _alerts.clear()
    return alerts


def record_tool_call(tool_name: str) -> None:
    """Track tool calls for nagging mechanism.

    If 5+ calls pass without externalizing, push a nagging alert.
    """
    global _tool_calls_since_last_drop

    # Reset counter on content-producing tools
    _RESET_TOOLS = {
        "mutate_stage", "append_stage",
        "promote_to_judgment", "flag_negative_space", "propose_commit",
    }

    if tool_name in _RESET_TOOLS:
        _tool_calls_since_last_drop = 0
        return

    _tool_calls_since_last_drop += 1

    if _tool_calls_since_last_drop >= _NAGGING_THRESHOLD:
        push_alert(
            "nagging",
            f"No findings externalized in last {_tool_calls_since_last_drop} tool calls. "
            "Consider using mutate_stage/append_stage or promote_to_judgment.",
        )


def _attach_alerts(result: dict) -> dict:
    """Attach pending alerts to any MCP tool return value.

    Core piggyback mechanism. Every MCP tool return gets
    pending_alerts injected.
    """
    alerts = drain_alerts()
    result["pending_alerts"] = alerts
    return result
