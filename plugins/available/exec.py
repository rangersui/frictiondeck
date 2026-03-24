"""Shell plugin — execute commands in the container.

Install: lucy install exec
Handler signature: async def handler(method, body, params) -> dict
"""

import subprocess

DESCRIPTION = "Execute shell commands (container only)"
ROUTES = {}


async def handle_exec(method, body, params):
    cmd = body.decode("utf-8") if isinstance(body, bytes) else body
    if not cmd: return {"error": "no command"}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd="/elastik")
        return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "timeout": 30}


ROUTES["/proxy/exec"] = handle_exec
