"""Safe exec plugin — command whitelist, no shell injection.

Handler signature: async def handler(method, body, params) -> dict
"""

import os, subprocess, json, shlex, fnmatch
from pathlib import Path

_in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
# _ROOT is injected by server.py (project root). Don't compute from __file__.
_CONF = _ROOT / "conf" / "exec.json"

# default: empty — human opens commands in conf/exec.json as needed
_ALLOW = []
_DENY_CHARS = ["|", ";", "&&", "||", "`", "$(", ">>", ">", "<"]
_TIMEOUT = 30

def _load_conf():
    global _ALLOW, _DENY_CHARS, _TIMEOUT
    if _CONF.exists():
        try:
            c = json.loads(_CONF.read_text())
            _ALLOW = c.get("allow", _ALLOW)
            _DENY_CHARS = c.get("deny_chars", _DENY_CHARS)
            _TIMEOUT = c.get("timeout", _TIMEOUT)
        except (json.JSONDecodeError, OSError):
            pass

_load_conf()

DESCRIPTION = "Safe exec — command whitelist" + (" (container)" if _in_container else "")
ROUTES = {}

PARAMS_SCHEMA = {
    "/proxy/exec": {
        "method": "POST",
        "params": {"command": {"type": "string", "required": True, "description": "Command to run (must match whitelist)"}},
        "returns": {"stdout": "string", "stderr": "string", "code": "int"}
    },
    "/proxy/exec/allow": {
        "method": "GET",
        "params": {},
        "returns": {"allow": ["string"]}
    },
}


def _is_allowed(cmd):
    """Check command against whitelist. Returns (allowed, reason)."""
    # block shell metacharacters
    for ch in _DENY_CHARS:
        if ch in cmd:
            return False, f"blocked character: {ch}"
    # match against allow patterns
    for pattern in _ALLOW:
        if fnmatch.fnmatch(cmd, pattern):
            return True, pattern
    return False, f"not in whitelist. allowed: {_ALLOW}"


async def handle_exec(method, body, params):
    cmd = body.decode("utf-8").strip() if isinstance(body, bytes) else body.strip()
    if not cmd: return {"error": "no command"}
    allowed, reason = _is_allowed(cmd)
    if not allowed:
        return {"error": f"command blocked: {reason}"}
    try:
        # use shell=False with split args for safety
        args = shlex.split(cmd)
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=_TIMEOUT, cwd=str(_ROOT))
        return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode,
                "container": _in_container}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "timeout": _TIMEOUT}
    except FileNotFoundError:
        return {"error": f"command not found: {args[0]}"}


async def handle_allow(method, body, params):
    return {"allow": _ALLOW, "deny_chars": _DENY_CHARS, "timeout": _TIMEOUT}


ROUTES["/proxy/exec"] = handle_exec
ROUTES["/proxy/exec/allow"] = handle_allow
