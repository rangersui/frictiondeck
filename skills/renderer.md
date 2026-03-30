# Renderer Specification

Renderers separate data from display.
A renderer is a complete HTML page stored as a world (renderer-*).

## How it works

1. renderer-markdown world contains HTML+JS
2. Data world starts with <!--use:renderer-markdown--> on line 1
3. index.html detects → fetches renderer → injects data → renders in iframe
4. No declaration → raw HTML rendering

## Writing a renderer

A renderer reads data from window.__ELASTIK_DATA__:

<!DOCTYPE html>
<html><head></head><body>
<script type="module">
  const src = (window.__ELASTIK_DATA__ || '').trim();
  const r = await __elastik.fetch('/' + src + '/read');
  const data = JSON.parse(r.stage_html);
  // ... render data ...
</script>
</body></html>

Use ESM imports from CDN: esm.sh, cdn.jsdelivr.net, unpkg.com, cdnjs.cloudflare.com
Service worker caches CDN. Second load is instant.

## __elastik helper API

Renderers run in iframe without same-origin. Use __elastik, not fetch():

__elastik.fetch('/foo/read')  → GET read (proxied by index.html)
__elastik.sync(content)       → POST sync to current world
__elastik.result(data)        → POST result to current world
__elastik.clear()             → POST clear to current world

Cross-world writes are physically blocked.
Do NOT use native fetch(). It will fail (null origin).

## __elastik.fetch return value — IMPORTANT

Returns a PARSED OBJECT, not a string. index.html already calls .json().

CORRECT:
const r = await __elastik.fetch('/data/read');
const d = JSON.parse(r.stage_html);  // parse once

WRONG:
const r = await __elastik.fetch('/data/read');
JSON.parse(r);  // ERROR: r is already an object

Return shape: {stage_html, pending_js, js_result, version}

## Polling — diff before repaint

NEVER replace innerHTML if data hasn't changed. It causes flashing.

let lastJson = '';
async function load() {
  const r = await __elastik.fetch('/' + src + '/read');
  const json = r.stage_html || '{}';
  if (json === lastJson) return;  // skip repaint
  lastJson = json;
  // ... update DOM ...
}
setInterval(load, 2000);

Reset lastJson = '' when switching panels to force repaint.
This is a design rule, not an optimization.

## Defensive renderers

Write with tolerance for messy data:
const battery = data.batt || data.battery || data.batteryLevel || 0;

Big model writes renderer once (smart, tolerant).
Small model writes data every time (simple, may have typos).

## Installing/removing

python scripts/renderer.py install markdown
python scripts/renderer.py remove markdown
python scripts/renderer.py list

## Available renderers (renderers/ directory)

- markdown.html — markdown to HTML via marked
- json-tree.html — syntax highlighted JSON
