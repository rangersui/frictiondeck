"""Postman plugin — raw HTTP with full headers.

Install: lucy install postman
Requires approve token. Returns complete response including headers.

Config: postman.json (hot-pluggable, mtime pattern)
  {"hosts": ["localhost", "127.0.0.1", "api.github.com"]}

Fallback env: POSTMAN_HOSTS=localhost,127.0.0.1
"""

import json, os, socket
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DESCRIPTION = "Raw HTTP gateway — full headers, whitelist-only"
ROUTES = {}

# ── hot-plug config ─────────────────────────────────────────────────────

_CONFIG_FILE = Path(__file__).resolve().parents[2] / "postman.json"
_config = {"hosts": []}
_config_mtime = 0
_in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

def _reload_config():
    global _config_mtime
    if not _CONFIG_FILE.exists():
        if not _config["hosts"]:
            _config["hosts"] = [h.strip() for h in os.getenv("POSTMAN_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
        return
    try:
        mt = _CONFIG_FILE.stat().st_mtime
        if mt == _config_mtime:
            return
        _config_mtime = mt
        data = json.loads(_CONFIG_FILE.read_text())
        _config.clear()
        _config["hosts"] = data.get("hosts", [])
    except (json.JSONDecodeError, OSError):
        pass

# ── params schema ───────────────────────────────────────────────────────

PARAMS_SCHEMA = {
    "/proxy/postman": {
        "method": "POST",
        "params": {
            "url": {"type": "string", "required": True, "description": "Full URL"},
            "method": {"type": "string", "required": False, "description": "HTTP method (default GET)"},
            "headers": {"type": "object", "required": False, "description": "Request headers"},
            "body": {"type": "string", "required": False, "description": "Request body"},
        },
        "example": {"url": "https://httpbin.org/get", "method": "GET"},
        "returns": {"status": "int", "headers": "object", "body": "string", "container": "bool"}
    },
}

# ── handler ─────────────────────────────────────────────────────────────

async def handle_postman(method, body, params):
    _reload_config()

    b = json.loads(body) if body else {}
    url = params.get("url") or b.get("url", "")
    if not url:
        return {"error": "url required", "container": _in_container}
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in ("localhost", "127.0.0.1") and parsed.port == int(os.getenv("ELASTIK_PORT", "3004")):
        return {"error": "cannot request elastik's own port", "container": _in_container}
    if not _config["hosts"]:
        return {"error": "no allowed hosts configured. Set postman.json or POSTMAN_HOSTS env var", "container": _in_container}
    if host not in _config["hosts"]:
        hint = ""
        if _in_container and host in ("localhost", "127.0.0.1"):
            hint = " (running in container — localhost points to the container, not the host. Use host.docker.internal instead)"
        return {"error": f"host '{host}' not in whitelist{hint}", "allowed": _config["hosts"], "container": _in_container}
    req_method = (params.get("method") or b.get("method", "GET")).upper()
    req_headers = b.get("headers", {})
    req_body = (b.get("body") or "").encode("utf-8") or None

    req = Request(url, data=req_body, headers=req_headers, method=req_method)
    try:
        r = urlopen(req, timeout=30)
        return {
            "status": r.status,
            "headers": dict(r.headers),
            "body": r.read().decode("utf-8", "replace"),
            "container": _in_container,
        }
    except HTTPError as e:
        return {
            "status": e.code,
            "headers": dict(e.headers),
            "body": e.read().decode("utf-8", "replace"),
            "container": _in_container,
        }
    except URLError as e:
        return {"error": str(e.reason), "container": _in_container}


ROUTES["/proxy/postman"] = handle_postman
