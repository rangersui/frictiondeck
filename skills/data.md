# Data/View Separation

Core principle: AI is a data engineer, not a frontend engineer.
AI writes JSON. Renderers paint pictures. Division of labor.

## Three worlds per feature

/dashboard        → <!--use:renderer-dashboard-->     ← human sees this
/dashboard-data   → {"temp": 47.3, "alerts": [...]}   ← AI reads/writes this
/renderer-dashboard → full HTML+JS page                ← written once

## Creating something new

1. Design the data shape (JSON)
2. Write the renderer (one-time, big model work)
   - Renderer reads data via __elastik.fetch
   - This is the only time you write HTML
3. Write initial data: POST /dashboard-data/write body: {...}
4. Connect: POST /dashboard/write body: <!--use:renderer-dashboard-->

## Daily updates

GET  /dashboard-data/read  → 50 tokens of JSON
POST /dashboard-data/write → updated JSON, 50 tokens

You never re-read or re-write the renderer. You never touch HTML.

## Answering questions

GET /dashboard-data/read → {"temp": 48.1, ...}
Answer from JSON. 50 tokens. Not 500 tokens of HTML.

## __ELASTIK_DATA__ = data source path

Renderers are templates. Never hardcode data sources.
The view world passes the data world name as __ELASTIK_DATA__:

POST /factory-a/write body:
<!--use:renderer-sensor-panel-->
factory-a-data

Renderer reads it:
const src = (window.__ELASTIK_DATA__ || '').trim();
const r = await __elastik.fetch('/' + src + '/read');
const data = JSON.parse(r.stage_html);

Same renderer, different data sources:
/factory-a → renderer-sensor-panel → factory-a-data
/factory-b → renderer-sensor-panel → factory-b-data

One renderer, N worlds. JSON shape IS the interface contract.

## Multi-source renderers (composability)

A complex page is ONE renderer that fetches multiple data worlds:

const sensors = JSON.parse((await __elastik.fetch('/sensors-data/read')).stage_html);
const tasks = JSON.parse((await __elastik.fetch('/tasks-data/read')).stage_html);
// render all into one cockpit

Different views = different renderers. Don't over-abstract.
cockpit and email inbox are different screens → different renderers.

## Conventions

- /map — world index
- renderer-* — front-end renderers
- /config/* — system configuration
- {name}-data — data world for a view world
