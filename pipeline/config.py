"""FrictionDeck v4 — Centralized configuration.

Stripped to essentials: HMAC key management + logging setup.
No LLM providers. No embedding. No RAG config.
FrictionDeck v4 doesn't call any LLM — the AI client brings its own brain.
"""

import json
import logging
import os
import secrets
from datetime import datetime, UTC
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("frictiondeck.config")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_DIR = os.environ.get("FRICTIONDECK_DB_DIR", os.path.join(_PROJECT_ROOT, "data"))
os.makedirs(_DB_DIR, exist_ok=True)
_CONFIG_PATH = os.path.join(_DB_DIR, "config.json")

VERSION = "4.0.0-alpha"
PORT = int(os.environ.get("FRICTIONDECK_PORT", "3004"))


# ── Logging ──────────────────────────────────────────────────────────────

_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")


def setup_logging() -> None:
    """Configure file + console logging for all frictiondeck.* loggers.

    Call once at process startup.
    Writes to ``logs/frictiondeck.log`` (10 MB rotating, 3 backups).
    Console handler at WARNING; file handler at DEBUG.
    Idempotent — safe to call multiple times.
    """
    root = logging.getLogger("frictiondeck")
    if root.handlers:
        return  # already configured

    root.setLevel(logging.DEBUG)

    # ── File handler: all levels, persisted ──
    os.makedirs(_LOG_DIR, exist_ok=True)
    log_path = os.path.join(_LOG_DIR, "frictiondeck.log")
    fh = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # ── Console handler: warnings and above ──
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter(
        "%(levelname)-8s  %(name)s  %(message)s",
    ))
    root.addHandler(ch)

    root.info("logging initialized — file=%s", log_path)


# ── HMAC audit key management ────────────────────────────────────────────
# Security credentials are NOT editable through any UI.
# Priority: env var → config.json → auto-generate.
# For production, use FD_AUDIT_HMAC_KEY env var.


def _get_audit_hmac_key() -> str:
    """Return HMAC key for audit hash chain."""
    # 1. Environment variable (recommended for production)
    env_key = os.environ.get("FD_AUDIT_HMAC_KEY")
    if env_key:
        return env_key
    # 2. Persisted in config.json
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("audit_hmac_key"):
                return data["audit_hmac_key"]
        except Exception:  # noqa: S110
            pass
    # 3. Auto-generate and persist — but warn if events already exist
    _audit_db = os.path.join(_DB_DIR, "audit.db")
    if os.path.exists(_audit_db):
        try:
            import sqlite3
            _c = sqlite3.connect(_audit_db)
            _n = _c.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            _c.close()
            if _n > 0:
                logger.warning(
                    "AUDIT HMAC KEY LOST — %d audit events "
                    "exist but no key found in config or env. "
                    "Chain verification will fail for HMAC-signed events. "
                    "Restore key from backup or set FD_AUDIT_HMAC_KEY env var.",
                    _n,
                )
        except Exception:  # noqa: S110
            pass

    key = secrets.token_hex(32)
    data: dict = {}
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: S110
            pass
    data["audit_hmac_key"] = key
    data["hmac_migration_ts"] = datetime.now(UTC).isoformat()
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Generated new HMAC key, migration ts written")
    return key


def _get_hmac_migration_ts() -> str | None:
    """Return the ISO timestamp when HMAC migration started (or None)."""
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("hmac_migration_ts")
        except Exception:  # noqa: S110
            pass
    return None


AUDIT_HMAC_KEY: str = _get_audit_hmac_key()
HMAC_MIGRATION_TS: str | None = _get_hmac_migration_ts()
