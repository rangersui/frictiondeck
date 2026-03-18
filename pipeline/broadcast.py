"""FrictionDeck v4 — WebSocket Broadcast

Memory-based pub/sub. ~10 lines connecting MCP writes to GUI updates.
Zero Redis. Zero message queue. Just an asyncio.Queue per subscriber.

Usage:
    # At startup (server.py):
    from pipeline.broadcast import broadcast, subscribe, unsubscribe
    from pipeline.stage import set_broadcast
    set_broadcast(broadcast)

    # In WebSocket handler:
    queue = subscribe()
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    finally:
        unsubscribe(queue)
"""

import asyncio
import json
import logging
from datetime import datetime, UTC
from typing import Any

logger = logging.getLogger("frictiondeck.broadcast")

# Set of subscriber queues — each WebSocket connection gets one
_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    """Register a new subscriber. Returns an asyncio.Queue to await on."""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    logger.debug("subscriber added  total=%d", len(_subscribers))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove a subscriber."""
    _subscribers.discard(q)
    logger.debug("subscriber removed  total=%d", len(_subscribers))


def broadcast(event_type: str, data: dict[str, Any]) -> None:
    """Push event to all subscribers (non-blocking).

    This is called synchronously from stage.py's _broadcast().
    We use put_nowait — if a subscriber's queue is full (slow consumer),
    the event is dropped for that subscriber with a warning.
    """
    event = {
        "event": event_type,
        "data": data,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    dead: list[asyncio.Queue] = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("subscriber queue full, dropping event: %s", event_type)
        except Exception:
            dead.append(q)

    for q in dead:
        _subscribers.discard(q)


def subscriber_count() -> int:
    """Return number of active subscribers."""
    return len(_subscribers)
