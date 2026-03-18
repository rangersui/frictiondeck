"""FrictionDeck v4 — Friction Gate (Server-side Enforcement)

The only path from Stage to Log. Human-only. AI cannot pass.

Flow:
  1. AI calls propose_commit → proposal stored
  2. User clicks Approve in GUI → server generates challenge question
  3. User answers correctly → server issues gate_token (one-time UUID, 60s expiry)
  4. GUI sends gate_token with commit request → server validates → commit executes
  5. 3 wrong answers → 5-minute lockout

Security:
  - gate_token is generated server-side, never exposed to MCP
  - DevTools detection in frontend is courtesy only — real security is here
  - Audit trail records everything: challenge, answer, token, commit
"""

import logging
import secrets
from datetime import datetime, UTC, timedelta
from typing import Any

from pipeline.constants import EventType

logger = logging.getLogger("frictiondeck.gate")

# Active tokens: {token: {"proposal_id": str, "expires_at": datetime}}
_active_tokens: dict[str, dict[str, Any]] = {}

# Failed attempt tracking: {ip_or_session: {"count": int, "locked_until": datetime | None}}
_attempt_tracker: dict[str, dict[str, Any]] = {}

_TOKEN_TTL = timedelta(seconds=60)
_MAX_ATTEMPTS = 3
_LOCKOUT_DURATION = timedelta(minutes=5)


def generate_challenge(proposal_id: str, session_id: str = "default") -> dict:
    """Generate a verification challenge for a commit proposal.

    Returns: {"challenge_id": str, "question": str, "hint": str}

    For now, returns a placeholder challenge. Phase 2 will pull
    questions from datasheet parameters stored in judgment_objects.
    """
    # Check lockout
    tracker = _attempt_tracker.get(session_id, {"count": 0, "locked_until": None})
    if tracker.get("locked_until"):
        if datetime.now(UTC) < tracker["locked_until"]:
            remaining = (tracker["locked_until"] - datetime.now(UTC)).seconds
            return {
                "locked": True,
                "message": f"Too many failed attempts. Try again in {remaining}s.",
                "retry_after": remaining,
            }
        # Lockout expired, reset
        tracker = {"count": 0, "locked_until": None}
        _attempt_tracker[session_id] = tracker

    challenge_id = secrets.token_hex(16)

    # TODO Phase 2: pull real questions from judgment_objects params
    # For now, use a simple confirmation
    return {
        "locked": False,
        "challenge_id": challenge_id,
        "proposal_id": proposal_id,
        "question": "Type COMMIT to confirm this proposal.",
        "hint": "This is a placeholder. Real challenges will use datasheet parameters.",
    }


def validate_answer(
    challenge_id: str,
    proposal_id: str,
    answer: str,
    session_id: str = "default",
) -> dict:
    """Validate challenge answer and issue gate_token if correct.

    Returns: {"valid": bool, "gate_token": str | None, "message": str}
    """
    # Check lockout
    tracker = _attempt_tracker.get(session_id, {"count": 0, "locked_until": None})
    if tracker.get("locked_until") and datetime.now(UTC) < tracker["locked_until"]:
        remaining = (tracker["locked_until"] - datetime.now(UTC)).seconds
        return {
            "valid": False,
            "gate_token": None,
            "message": f"Locked out. Try again in {remaining}s.",
        }

    # TODO Phase 2: real answer validation against datasheet params
    # For now, accept "COMMIT" (case-insensitive)
    correct = answer.strip().upper() == "COMMIT"

    if not correct:
        tracker["count"] = tracker.get("count", 0) + 1
        if tracker["count"] >= _MAX_ATTEMPTS:
            tracker["locked_until"] = datetime.now(UTC) + _LOCKOUT_DURATION
            _attempt_tracker[session_id] = tracker
            logger.warning("gate lockout  session=%s  proposal=%s", session_id, proposal_id)
            return {
                "valid": False,
                "gate_token": None,
                "message": f"Locked out for {_LOCKOUT_DURATION.seconds}s after {_MAX_ATTEMPTS} failed attempts.",
            }
        _attempt_tracker[session_id] = tracker
        remaining = _MAX_ATTEMPTS - tracker["count"]
        return {
            "valid": False,
            "gate_token": None,
            "message": f"Incorrect. {remaining} attempts remaining.",
        }

    # Correct — issue token
    gate_token = secrets.token_hex(32)
    _active_tokens[gate_token] = {
        "proposal_id": proposal_id,
        "expires_at": datetime.now(UTC) + _TOKEN_TTL,
        "issued_at": datetime.now(UTC).isoformat(),
    }

    # Reset attempt counter
    _attempt_tracker[session_id] = {"count": 0, "locked_until": None}

    logger.info("gate_token issued  proposal=%s  expires_in=%ds",
                proposal_id, _TOKEN_TTL.seconds)
    return {
        "valid": True,
        "gate_token": gate_token,
        "message": f"Gate token issued. Valid for {_TOKEN_TTL.seconds}s.",
    }


def consume_token(gate_token: str, proposal_id: str) -> dict:
    """Validate and consume a gate token (one-time use).

    Returns: {"valid": bool, "message": str}
    """
    # Clean expired tokens
    _cleanup_expired()

    token_data = _active_tokens.pop(gate_token, None)
    if not token_data:
        return {"valid": False, "message": "Invalid or expired gate token."}

    if token_data["proposal_id"] != proposal_id:
        return {"valid": False, "message": "Gate token does not match proposal."}

    if datetime.now(UTC) > token_data["expires_at"]:
        return {"valid": False, "message": "Gate token expired."}

    logger.info("gate_token consumed  proposal=%s", proposal_id)
    return {"valid": True, "message": "Gate token valid. Commit authorized."}


def _cleanup_expired() -> None:
    """Remove expired tokens."""
    now = datetime.now(UTC)
    expired = [k for k, v in _active_tokens.items() if now > v["expires_at"]]
    for k in expired:
        del _active_tokens[k]
