# Translate Plugin — Bidirectional Format Conversion

POST /translate

## HTML → Markdown (inbound)

POST /translate body:
{"html": "<h1>Report</h1><p>Q1 results...</p>", "to": "markdown"}
→ {"result": "# Report\nQ1 results...", "engine": "fallback"}

## Markdown → HTML (outbound)

POST /translate body:
{"markdown": "# Hello\n**bold**", "to": "html"}
→ {"result": "<h1>Hello</h1>\n<p><strong>bold</strong></p>", "engine": "fallback"}

## File → Markdown (ingest)

POST /translate body:
{"path": "/path/to/file.docx", "to": "markdown"}
→ {"result": "...", "engine": "markitdown"}

Supports: docx, pdf, xlsx, pptx, html (requires pip install markitdown)

## Three-tier engine

1. markitdown (if installed) — best quality, supports Office files
2. markdown lib (if installed) — md→html
3. regex fallback (always available) — basic but works

## Typical workflow

Browser extension captures HTML in js_result:
POST /translate body: {"html": "[captured]", "to": "markdown"}
→ clean markdown
POST /research-notes/write body: [clean markdown]

External data → translate → clean format → store in world.
This is the inbound half of the translator.
dom_patch is the outbound half (see /skills-patch).

Requires plugin installation. Recommend to human if available in /info.
