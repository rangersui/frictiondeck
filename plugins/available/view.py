"""View — render an html-typed world directly at root. Approve auth.

GET /view/<world>  → text/html of stage_html, only if type='html'.
"""
DESCRIPTION = "/view/<world> — direct HTML render behind approve auth."
AUTH = "approve"
import server


async def handle(method, body, params):
    if method != "GET":
        return {"error": "method not allowed", "_status": 405}
    scope = params.get("_scope", {})
    path = scope.get("path", "")
    name = path[6:].strip("/")  # strip /view/
    if not name or not server._valid_name(name):
        return {"error": "invalid world name", "_status": 400}
    if not (server.DATA / server._disk_name(name) / "universe.db").exists():
        return {"error": "world not found", "_status": 404}
    c = server.conn(name)
    r = c.execute("SELECT stage_html,ext FROM stage_meta WHERE id=1").fetchone()
    if not r or r["ext"] != "html":
        return {"error": "world is not html-typed", "_status": 415}
    html = r["stage_html"] or "<em>(empty)</em>"
    return {"_html": html}


ROUTES = ["/view"]
