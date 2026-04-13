"""Mirror — reverse proxy any URL behind approve auth.

/mirror?url=https://example.com/x  → entry point
/m/example.com/x                   → subsequent navigation (same namespace)
"""
DESCRIPTION = "Reverse-proxy mirror. /mirror?url=X entry, /m/domain/path follow-up."
AUTH = "approve"
import json, re, subprocess

MIRROR_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>elastik mirror</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{background:#1a1a2e;display:flex;flex-direction:column;font-family:system-ui,sans-serif}
#bar{display:flex;padding:8px;gap:8px;background:#1a1a2e;border-bottom:1px solid #2a2a4e;flex-shrink:0}
#url{flex:1;background:#2a2a4e;border:1px solid #3a3a5e;border-radius:4px;color:#e0e0e0;font-size:16px;padding:8px 12px;outline:none}
#url:focus{border-color:#7fdbca}
#go{background:#2a2a4e;color:#7fdbca;border:1px solid #3a3a5e;border-radius:4px;padding:8px 16px;font-size:16px;cursor:pointer}
#go:active{background:#3a3a6e}
#view{flex:1;border:none;background:#fff}
</style></head><body>
<div id="bar"><input id="url" placeholder="https://..." autocapitalize="none" autocorrect="off" spellcheck="false"><button id="go">Go</button></div>
<iframe id="view"></iframe>
<script>
const url=document.getElementById('url'),view=document.getElementById('view');
function go(){
  let u=url.value.trim();
  if(!u)return;
  if(!u.startsWith('http'))u='https://'+u;
  url.value=u;
  view.src='/mirror?url='+encodeURIComponent(u);
}
url.addEventListener('keydown',e=>{if(e.key==='Enter')go()});
document.getElementById('go').addEventListener('click',go);
</script></body></html>"""


def _proxy(target, domain=""):
    """curl target, return (body_bytes, content_type). Injects <base> for HTML."""
    try:
        r = subprocess.run(["curl", "-s", "-L", "-m", "30", "-D", "-", target],
                           capture_output=True, timeout=35)
        raw = r.stdout
        sep = raw.rfind(b"\r\n\r\n")
        if sep == -1: sep = raw.rfind(b"\n\n")
        if sep == -1: return raw, "text/html"
        headers_part = raw[:sep].decode("utf-8", "replace").lower()
        body = raw[sep+4:] if raw[sep:sep+4] == b"\r\n\r\n" else raw[sep+2:]
        ct = "text/html"
        for line in headers_part.split("\n"):
            if line.strip().startswith("content-type:"):
                ct = line.split(":", 1)[1].strip()
                break
        if "text/html" in ct and domain:
            body = re.sub(rb'(?i)<meta[^>]*(?:content-security-policy|x-frame-options)[^>]*>', b'', body)
            body = f'<base href="/m/{domain}/">'.encode() + body
        return body, ct
    except Exception as e:
        return json.dumps({"error": str(e)}).encode(), "application/json"


def _target(path, qs):
    """Parse mirror URL. Returns (target, domain) or (None, None)."""
    from urllib.parse import parse_qs, urlparse
    if path in ("/mirror", "/mirror/"):
        params = parse_qs(qs)
        raw = params.get("url", [""])[0]
        if not raw or not raw.startswith(("http://", "https://")): return None, None
        return raw, urlparse(raw).netloc
    if path.startswith("/m/"):
        rest = path[3:]
        slash = rest.find("/")
        if slash == -1: return "https://" + rest, rest
        dom = rest[:slash]
        p = rest[slash:]
        target = "https://" + dom + p
        if qs: target += "?" + qs
        return target, dom
    return None, None


async def handle(method, body, params):
    scope = params.get("_scope", {})
    path = scope.get("path", "")
    qs = scope.get("query_string", b"").decode()
    target, domain = _target(path, qs)
    if not target:
        if method == "GET" and path in ("/mirror", "/mirror/"):
            return {"_html": MIRROR_HTML}
        return {"error": "invalid mirror path", "_status": 400}
    body_bytes, ct = _proxy(target, domain)
    return {"_body": body_bytes, "_ct": ct}


ROUTES = ["/mirror", "/m"]
