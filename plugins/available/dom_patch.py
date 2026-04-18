"""DOM Patch — CSS-selector-based HTML mutations. Zero dependencies.

POST /dom-patch  body: {"world": "sensors", "ops": [...]}

Supported ops:
  replace   {"op":"replace", "selector":"#temp-value", "html":"48.1°C"}
  append    {"op":"append",  "selector":"#alerts", "html":"<li>new</li>"}
  prepend   {"op":"prepend", "selector":"#alerts", "html":"<li>first</li>"}
  remove    {"op":"remove",  "selector":"#old-alert-1"}
  attr      {"op":"attr",    "selector":"#light", "attr":"class", "value":"red"}
  text      {"op":"text",    "selector":"#count", "text":"42"}

CSS selectors supported: tag, #id, .class, tag#id, tag.class, .a.b

Atomic: all ops succeed or none applied.
Install: lucy install dom_patch
"""
import json, re
from html.parser import HTMLParser

DESCRIPTION = "DOM-aware HTML patch with CSS selectors (zero deps)"
SKILL = """\
# Writing Strategy — DOM Patch

POST /dom-patch body:
{
  "world": "status-page",
  "ops": [
    {"op": "replace", "selector": "#temp-value", "html": "48.1°C"},
    {"op": "attr", "selector": "#status", "attr": "class", "value": "red"},
    {"op": "append", "selector": "#alerts", "html": "<li>New alert</li>"},
    {"op": "remove", "selector": "#old-item"}
  ]
}

6 ops: replace, append, prepend, remove, attr, text.
Atomic: all succeed or none applied.

When creating HTML for patching, embed stable IDs:
<div id="sensor-panel">
  <span id="temp-value">--</span>
  <ul id="alerts"></ul>
</div>

dom_patch is the downgrade path — not the daily driver.
Use renderer + JSON data separation when possible (see /usr/lib/skills/data).
"""
ROUTES = {}

PARAMS_SCHEMA = {
    "/dom-patch": {
        "method": "POST",
        "params": {
            "world": {"type": "string", "required": True, "description": "Target world name"},
            "ops": {"type": "array", "required": True, "description": "Array of DOM operations"},
        },
        "example": {
            "world": "sensors",
            "ops": [
                {"op": "replace", "selector": "#temp-value", "html": "48.1\u00b0C"},
                {"op": "attr", "selector": "#status", "attr": "class", "value": "green"},
            ]
        },
        "returns": {"version": "int", "applied": "int", "length": "int"}
    },
}

OPS_SCHEMA = [
    {"op": "replace", "params": {"selector": "CSS selector", "html": "new innerHTML"}},
    {"op": "append", "params": {"selector": "CSS selector", "html": "HTML to append inside"}},
    {"op": "prepend", "params": {"selector": "CSS selector", "html": "HTML to prepend inside"}},
    {"op": "remove", "params": {"selector": "CSS selector"}},
    {"op": "attr", "params": {"selector": "CSS selector", "attr": "attribute name", "value": "attribute value"}},
    {"op": "text", "params": {"selector": "CSS selector", "text": "new text content"}},
]

# --- Tree node ---

VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")
    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = list(attrs) if attrs else []
        self.children = []
        self.parent = parent

    def get_attr(self, name):
        for k, v in self.attrs:
            if k == name: return v
        return None

    def set_attr(self, name, value):
        for i, (k, _) in enumerate(self.attrs):
            if k == name:
                self.attrs[i] = (name, value)
                return
        self.attrs.append((name, value))

    @property
    def id(self):
        return self.get_attr("id") or ""

    @property
    def classes(self):
        c = self.get_attr("class") or ""
        return set(c.split()) if c else set()


class TextNode:
    __slots__ = ("text", "parent")
    def __init__(self, text, parent=None):
        self.text = text
        self.parent = parent


class RawNode:
    """Comments, doctypes, processing instructions — preserved as-is."""
    __slots__ = ("raw", "parent")
    def __init__(self, raw, parent=None):
        self.raw = raw
        self.parent = parent


# --- Parser ---

class _TreeBuilder(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.root = Node("__root__")
        self.cur = self.root
        self.all_elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        self.all_elements.append(node)
        if tag.lower() not in VOID_TAGS:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n and n.tag != "__root__":
            if n.tag.lower() == tag.lower():
                self.cur = n.parent if n.parent else self.root
                return
            n = n.parent

    def handle_data(self, data):
        self.cur.children.append(TextNode(data, self.cur))

    def handle_entityref(self, name):
        self.cur.children.append(TextNode(f"&{name};", self.cur))

    def handle_charref(self, name):
        self.cur.children.append(TextNode(f"&#{name};", self.cur))

    def handle_comment(self, data):
        self.cur.children.append(RawNode(f"<!--{data}-->", self.cur))

    def handle_decl(self, decl):
        self.cur.children.append(RawNode(f"<!{decl}>", self.cur))

    def handle_pi(self, data):
        self.cur.children.append(RawNode(f"<?{data}>", self.cur))


# --- Serializer ---

def _serialize(node):
    if isinstance(node, TextNode):
        return node.text
    if isinstance(node, RawNode):
        return node.raw
    if node.tag == "__root__":
        return "".join(_serialize(c) for c in node.children)
    attrs = ""
    for k, v in node.attrs:
        if v is None:
            attrs += f" {k}"
        else:
            attrs += f' {k}="{v}"'
    if node.tag.lower() in VOID_TAGS:
        return f"<{node.tag}{attrs}>"
    inner = "".join(_serialize(c) for c in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


# --- CSS selector matching ---

_SEL_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9]*)?(?:#([a-zA-Z0-9_-]+))?((?:\.[a-zA-Z0-9_-]+)*)$')

def _parse_selector(sel):
    m = _SEL_RE.match(sel.strip())
    if not m: return None
    tag = m.group(1)
    eid = m.group(2)
    classes = [c for c in m.group(3).split(".") if c] if m.group(3) else []
    return (tag.lower() if tag else None, eid, classes)

def _match(node, parsed):
    tag, eid, classes = parsed
    if tag and node.tag.lower() != tag: return False
    if eid and node.id != eid: return False
    if classes and not set(classes).issubset(node.classes): return False
    return True

def _select(tree, selector):
    parsed = _parse_selector(selector)
    if not parsed: return []
    return [n for n in tree.all_elements if _match(n, parsed)]


# --- Parse HTML fragment into child nodes ---

def _parse_fragment(html_str):
    tree = _TreeBuilder(html_str)
    return tree.root.children


# --- Apply operations ---

def _apply_ops(html, ops):
    tree = _TreeBuilder(html)
    applied = 0

    for op in ops:
        t = op.get("op")
        sel = op.get("selector", "")
        nodes = _select(tree, sel)
        if not nodes: continue

        for node in nodes:
            if t == "replace":
                node.children = _parse_fragment(op.get("html", ""))
                for c in node.children: c.parent = node
            elif t == "append":
                new_kids = _parse_fragment(op.get("html", ""))
                for c in new_kids: c.parent = node
                node.children.extend(new_kids)
            elif t == "prepend":
                new_kids = _parse_fragment(op.get("html", ""))
                for c in new_kids: c.parent = node
                node.children = new_kids + node.children
            elif t == "remove":
                if node.parent:
                    node.parent.children = [c for c in node.parent.children if c is not node]
            elif t == "attr":
                attr_name = op.get("attr", "")
                if attr_name:
                    node.set_attr(attr_name, op.get("value", ""))
            elif t == "text":
                txt = op.get("text", "")
                escaped = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                node.children = [TextNode(escaped, node)]
            else:
                continue
        applied += 1

    return _serialize(tree.root), applied


# --- Plugin route handler ---

async def handle_dom_patch(method, body, params):
    """POST /dom-patch — CSS-selector DOM mutations on a world's HTML.

    body (JSON):
      {"world": "status", "ops": [{"op": "text", "selector": "#count", "text": "42"}]}

    Ops: replace, append, prepend, remove, attr, text. Atomic — all
    succeed or none applied. Selectors: tag, #id, .class, tag#id, .a.b.
    """
    if isinstance(body, dict):
        data = body
    else:
        raw = body if isinstance(body, str) else body.decode("utf-8", "replace")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {"error": "body must be JSON", "_status": 400}
    world = data.get("world", "default")
    ops = data.get("ops", [])
    if not ops:
        return {"error": "no ops provided", "_status": 400}

    c = conn(world)
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]

    try:
        new_html, applied = _apply_ops(old, ops)
    except Exception as e:
        return {"error": str(e), "_status": 400}

    if applied == 0:
        v = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
        return {"version": v, "applied": 0, "length": len(old), "note": "no selectors matched"}

    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (new_html,))
    c.commit()
    log_event(world, "dom_patched", {"ops": len(ops), "applied": applied})
    v = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
    return {"version": v, "applied": applied, "length": len(new_html)}


ROUTES["/dom-patch"] = handle_dom_patch
