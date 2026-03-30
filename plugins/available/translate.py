"""Translate — format conversion gateway.

POST /translate  body: {"html": "<h1>Hello</h1>", "to": "markdown"}
POST /translate  body: {"markdown": "# Hello", "to": "html"}
POST /translate  body: {"path": "/path/to/file.docx", "to": "markdown"}

Supported conversions:
  html → markdown     (markitdown or fallback)
  markdown → html     (stdlib or markdown lib)
  file → markdown     (markitdown, supports docx/pdf/xlsx/pptx/html)

If markitdown not installed: HTML→markdown uses basic tag stripping.
If markdown lib not installed: markdown→HTML uses basic regex.

Install: lucy install translate
"""
import json, re, html as html_mod

DESCRIPTION = "Format translation: HTML↔markdown, file→markdown"
ROUTES = {}

PARAMS_SCHEMA = {
    "/translate": {
        "method": "POST",
        "params": {
            "html": {"type": "string", "required": False, "description": "HTML string to convert"},
            "markdown": {"type": "string", "required": False, "description": "Markdown string to convert"},
            "path": {"type": "string", "required": False, "description": "File path to convert"},
            "to": {"type": "string", "required": True, "description": "Target format: 'markdown' or 'html'"},
        },
        "returns": {"result": "string", "engine": "string"}
    },
}

# --- HTML → Markdown fallback (no deps) ---

_BLOCK_TAGS = {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "hr", "blockquote", "pre"}

def _html_to_md_fallback(html_str):
    """Strip HTML tags, preserve structure. Basic but works."""
    s = html_str
    # headings
    for i in range(1, 7):
        s = re.sub(rf'<h{i}[^>]*>(.*?)</h{i}>', lambda m: "#" * i + " " + m.group(1).strip() + "\n\n", s, flags=re.I | re.DOTALL)
    # bold/italic
    s = re.sub(r'<(strong|b)[^>]*>(.*?)</\1>', r'**\2**', s, flags=re.I | re.DOTALL)
    s = re.sub(r'<(em|i)[^>]*>(.*?)</\1>', r'*\2*', s, flags=re.I | re.DOTALL)
    # links
    s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', s, flags=re.I | re.DOTALL)
    # images
    s = re.sub(r'<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*/?\s*>', r'![\2](\1)', s, flags=re.I | re.DOTALL)
    s = re.sub(r'<img[^>]*src="([^"]*)"[^>]*/?\s*>', r'![](\1)', s, flags=re.I | re.DOTALL)
    # list items
    s = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', s, flags=re.I | re.DOTALL)
    # code blocks
    s = re.sub(r'<pre[^>]*><code[^>]*>(.*?)</code></pre>', r'```\n\1\n```\n', s, flags=re.I | re.DOTALL)
    s = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', s, flags=re.I | re.DOTALL)
    # paragraphs and breaks
    s = re.sub(r'<br\s*/?\s*>', '\n', s, flags=re.I)
    s = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', s, flags=re.I | re.DOTALL)
    s = re.sub(r'<hr\s*/?\s*>', '\n---\n', s, flags=re.I)
    # strip remaining tags
    s = re.sub(r'<[^>]+>', '', s)
    # decode entities
    s = html_mod.unescape(s)
    # clean up whitespace
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return s


# --- Markdown → HTML fallback (no deps) ---

def _md_to_html_fallback(md):
    """Basic markdown to HTML. Handles headings, bold, italic, links, lists, code."""
    lines = md.split("\n")
    out = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(html_mod.escape(line))
            continue
        # headings
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
            continue
        # hr
        if re.match(r'^---+\s*$', line):
            out.append("<hr>")
            continue
        # list items
        m = re.match(r'^[-*+]\s+(.*)', line)
        if m:
            out.append(f"<li>{m.group(1)}</li>")
            continue
        # inline formatting
        l = line
        l = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', l)
        l = re.sub(r'\*(.+?)\*', r'<em>\1</em>', l)
        l = re.sub(r'`(.+?)`', r'<code>\1</code>', l)
        l = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', l)
        l = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'<img src="\2" alt="\1">', l)
        if l.strip():
            out.append(f"<p>{l}</p>")
        else:
            out.append("")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


# --- Engine detection ---

def _get_markitdown():
    try:
        from markitdown import MarkItDown
        return MarkItDown()
    except ImportError:
        return None

def _get_markdown_lib():
    try:
        import markdown
        return markdown
    except ImportError:
        return None


# --- Route handler ---

async def handle_translate(method, body, params):
    data = body if isinstance(body, dict) else json.loads(body if isinstance(body, str) else body.decode("utf-8"))
    target = data.get("to", "markdown")
    html_in = data.get("html", "")
    md_in = data.get("markdown", "")
    path_in = data.get("path", "")

    if target == "markdown":
        if path_in:
            mid = _get_markitdown()
            if mid:
                try:
                    result = mid.convert(path_in)
                    return {"result": result.text_content, "engine": "markitdown"}
                except Exception as e:
                    return {"error": str(e), "_status": 400}
            else:
                return {"error": "markitdown not installed. pip install markitdown", "_status": 501}

        if html_in:
            mid = _get_markitdown()
            if mid:
                try:
                    import tempfile, os
                    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
                    tmp.write(html_in); tmp.close()
                    result = mid.convert(tmp.name)
                    os.unlink(tmp.name)
                    return {"result": result.text_content, "engine": "markitdown"}
                except Exception:
                    pass
            return {"result": _html_to_md_fallback(html_in), "engine": "fallback"}

        return {"error": "provide 'html' or 'path'", "_status": 400}

    elif target == "html":
        if md_in:
            lib = _get_markdown_lib()
            if lib:
                return {"result": lib.markdown(md_in), "engine": "markdown"}
            return {"result": _md_to_html_fallback(md_in), "engine": "fallback"}
        return {"error": "provide 'markdown'", "_status": 400}

    return {"error": f"unsupported target: {target}", "_status": 400}


ROUTES["/translate"] = handle_translate
