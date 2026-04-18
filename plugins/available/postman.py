"""Postman — curl proxy. CORS bypass for browser fetch.

fetch('/postman', {method:'POST', body: JSON.stringify({url:'https://api.github.com/repos/x/y'})})

Requires approve token. Because this is curl with your server's IP.
"""
import json, subprocess

DESCRIPTION = "curl proxy — CORS bypass, approve-token only"
ROUTES = {}


async def handle_postman(method, body, params):
    """POST /postman — curl proxy. CORS bypass from the browser.

    body (JSON):
      {"url": "https://api.github.com/x", "method": "GET",
       "headers": {}, "body": ""}

    Returns the response body verbatim. Requires approve token —
    this is curl with your server's IP.
    """
    try:
        b = json.loads(body) if body and body.strip() else {}
    except (json.JSONDecodeError, TypeError):
        return {"error": "invalid JSON body", "_status": 400}
    url = b.get("url", "")
    if not url or not url.startswith(("http://", "https://")):
        return {"error": "url required (http/https)", "_status": 400}
    cmd = ["curl", "-s", "-m", "30", "-X", b.get("method", "GET").upper()]
    for k, v in b.get("headers", {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if b.get("body"):
        cmd += ["-d", b["body"]]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=35)
    return {"_html": r.stdout.decode("utf-8", "replace"), "_status": 200}


ROUTES["/postman"] = handle_postman

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--routes":
        print(json.dumps(list(ROUTES.keys())))
        sys.exit(0)
    import asyncio
    d = json.loads(sys.stdin.readline())
    handler = ROUTES.get(d["path"])
    if not handler:
        print(json.dumps({"status": 404, "body": json.dumps({"error": "not found"})}))
    else:
        qs = d.get("query", "")
        params = dict(x.split("=", 1) for x in qs.split("&") if "=" in x) if qs else {}
        result = asyncio.run(handler(d.get("method", "GET"), d.get("body", ""), params))
        status = result.pop("_status", 200)
        body = result.pop("_html", None) or json.dumps(result)
        print(json.dumps({"status": status, "body": body}))
