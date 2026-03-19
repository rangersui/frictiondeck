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
PERSONAL_MODE = os.environ.get("FRICTIONDECK_MODE", "personal") == "personal"

# ── iframe sandbox ────────────────────────────────────────────────────────
# personal: allow-same-origin lets iframe fetch /proxy/*
# enterprise: remove allow-same-origin, AI uses MCP for data
IFRAME_SANDBOX = (
    "allow-scripts allow-same-origin allow-popups"
    if PERSONAL_MODE
    else "allow-scripts allow-popups"
)

# ── CSP whitelists ────────────────────────────────────────────────────────
# Domains allowed in Content-Security-Policy for Stage iframe.
# AI can query these via get_csp_whitelist(). Human can add via /api/csp/add.
CSP_SCRIPT_WHITELIST: list[str] = [
    "https://cdn.jsdelivr.net",
    "https://cdnjs.cloudflare.com",
    "https://cdn.tailwindcss.com",
    "https://unpkg.com",
] if PERSONAL_MODE else []

CSP_STYLE_WHITELIST: list[str] = [
    "https://cdn.jsdelivr.net",
    "https://cdnjs.cloudflare.com",
    "https://unpkg.com",
    "https://fonts.googleapis.com",
] if PERSONAL_MODE else []

CSP_FONT_WHITELIST: list[str] = [
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
] if PERSONAL_MODE else []

# ── Proxy whitelist ───────────────────────────────────────────────────────
# iframe JS can fetch('/proxy/<service>/...') → forwarded to target URL
# Only whitelisted services are allowed. Everything else → 403.
PROXY_WHITELIST: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "weather": "https://api.openweathermap.org",
    # Add your own:
    # "stocks": "https://api.example.com",
}


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
    _history_db = os.path.join(_DB_DIR, "default", "history.db")
    if os.path.exists(_history_db):
        try:
            import sqlite3
            _c = sqlite3.connect(_history_db)
            # Check both old and new table names
            _tables = [r[0] for r in _c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            _tbl = "events" if "events" in _tables else "audit_events" if "audit_events" in _tables else None
            _n = _c.execute(f"SELECT COUNT(*) FROM {_tbl}").fetchone()[0] if _tbl else 0
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
