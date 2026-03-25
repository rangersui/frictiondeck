"""Patch — composable string operations on any world's stage.

POST /proxy/patch  body: {"world": "default", "ops": [...]}

Supported ops:
  insert       {op:"insert", pos:0, text:"hello"}
  delete       {op:"delete", start:0, end:5}
  replace      {op:"replace", find:"old", text:"new", count:1}
  replace_all  {op:"replace_all", find:"old", text:"new"}
  slice        {op:"slice", start:0, end:100}
  prepend      {op:"prepend", text:"header"}
  regex_replace {op:"regex_replace", pattern:"\\d+", text:"X", count:0}

Install: lucy install patch
"""
import json, re

DESCRIPTION = "String operations: insert, delete, replace, prepend, slice, regex"
ROUTES = {}

PARAMS_SCHEMA = {
    "/proxy/patch": {
        "method": "POST",
        "params": {
            "world": {"type": "string", "required": True, "description": "Target world name"},
            "ops": {"type": "array", "required": True, "description": "Array of operations"},
        },
        "example": {
            "world": "default",
            "ops": [{"op": "replace", "find": "old", "text": "new"}]
        },
        "returns": {"version": "int", "applied": "int", "length": "int"}
    },
}

OPS_SCHEMA = [
    {"op": "insert", "params": {"pos": "int", "text": "string"}},
    {"op": "delete", "params": {"start": "int", "end": "int"}},
    {"op": "replace", "params": {"find": "string", "text": "string (replacement)", "count": "int (default 1)"}},
    {"op": "replace_all", "params": {"find": "string", "text": "string (replacement)"}},
    {"op": "prepend", "params": {"text": "string"}},
    {"op": "slice", "params": {"start": "int", "end": "int"}},
    {"op": "regex_replace", "params": {"pattern": "string", "text": "string (replacement)", "count": "int (default 0 = all)"}},
]


def _txt(op):
    """Get replacement text from op, accepting multiple field names."""
    return op.get("text") or op.get("replace") or op.get("with") or op.get("value") or ""


def apply_patch(html, ops):
    count = 0
    for op in ops:
        t = op.get("op")
        if t == "insert":
            pos = max(0, min(op.get("pos", 0), len(html)))
            html = html[:pos] + _txt(op) + html[pos:]; count += 1
        elif t == "delete":
            s, e = max(0, op.get("start", 0)), min(len(html), op.get("end", 0))
            html = html[:s] + html[e:]; count += 1
        elif t == "replace":
            f = op.get("find", "")
            n = op.get("count", 1)
            if f: html = html.replace(f, _txt(op), n); count += 1
        elif t == "replace_all":
            f = op.get("find", "")
            if f: html = html.replace(f, _txt(op)); count += 1
        elif t == "slice":
            html = html[op.get("start", 0):op.get("end", len(html))]; count += 1
        elif t == "prepend":
            html = _txt(op) + html; count += 1
        elif t == "regex_replace":
            p, n = op.get("pattern", ""), op.get("count", 0)
            if p: html = re.sub(p, _txt(op), html, count=n); count += 1
    return html, count


async def handle_patch(method, body, params):
    data = body if isinstance(body, dict) else json.loads(body if isinstance(body, str) else body.decode("utf-8"))
    world = data.get("world", "default")
    ops = data.get("ops", [])
    if not ops:
        return {"error": "no ops provided"}
    c = conn(world)
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
    new_html, applied = apply_patch(old, ops)
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (new_html,))
    c.commit()
    log_event(world, "stage_patched", {"ops": len(ops), "applied": applied})
    v = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
    return {"version": v, "applied": applied, "length": len(new_html)}


ROUTES["/proxy/patch"] = handle_patch
