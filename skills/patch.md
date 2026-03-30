# Writing Strategy — Decision Tree

## 1. World has a renderer (daily driver, cheapest)

Data lives in {name}-data world as JSON.
AI only touches JSON. Never touches HTML.
Read: 50 tokens. Write: 50 tokens.

## 2. Pure HTML page without renderer (fallback)

- Page has IDs and change <30% → dom_patch
- New page or short content → full write

POST /proxy/dom-patch body:
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
Requires plugin: recommend human install dom_patch.

dom_patch is the downgrade path — not the daily driver.

When creating HTML for patching, embed stable IDs:
<div id="sensor-panel">
  <span id="temp-value">--</span>
  <ul id="alerts"></ul>
</div>

## 3. Plain text worlds (markdown, logs, config)

- Simple change → string patch (POST /proxy/patch)
  ops: insert, delete, replace, replace_all, prepend, slice, regex_replace
- Full replacement → write

## 4. External data coming in

- HTML → translate to markdown → store
- File (docx/pdf) → translate → store
See /skills-translate for details.

## Summary

renderer world  → read/write JSON in {name}-data     (daily driver)
pure HTML world → dom_patch for surgical updates      (fallback)
text world      → string patch or full write          (simple)
external data   → translate → store                   (inbound)

## Anchor convention

When writing HTML, embed comment anchors for stable patching:
<!-- #section-name -->
Patch ops can use short anchors instead of fragile long matches.
