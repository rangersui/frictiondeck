# elastik — core protocol

You write strings to a database. A browser renders them. A human sees them.
You read strings from a database. A human wrote them. You see them.

## Three mailboxes (per world)

stage    → browser renders this
pending  → browser evals this (JS command)
result   → browser writes back here

## Routes

Write:    POST /{name}/write    body=string  → version++
Append:   POST /{name}/append   body=string  → version++
Read:     GET  /{name}/read     → {stage_html, pending_js, js_result, version}
Pending:  POST /{name}/pending  body=string  → command mailbox
Clear:    POST /{name}/clear    → clears pending + result
Sync:     POST /{name}/sync     body=string  → writes stage, no version bump
Stages:   GET  /stages          → list all worlds

## Auth

POST routes require X-Auth-Token (MCP injects it automatically).
GET routes are public.
Approve token = human only. You don't have it. Don't try /admin/*.

## Workflow

1. Read data worlds (JSON) → think → write data worlds (JSON)
2. Renderers paint. You don't touch HTML.
3. AI is the kitchen. Renderers are waiters. JSON is the menu.

## Session start

1. GET /info → plugins, worlds, renderers, skills-core, skill index
2. GET /stages → all worlds with version
3. GET /{name}/read for relevant worlds
4. Summarize to user

## Plugins

Propose: POST /plugins/propose {name, code, description}
Human approves. You cannot approve.
Check /info → available field for uninstalled plugins.

## Navigation

In-world:  POST /{name}/pending body: window.location='/target'
New tab:   POST /{name}/pending body: window.open('https://...')
External:  use browser extension

## Skill worlds (read on demand)

GET /skills-data/read      → data/view separation, JSON-first, renderer reuse
GET /skills-renderer/read  → renderer spec, __elastik API, composability, polling
GET /skills-patch/read     → dom_patch vs write vs string patch decision tree
GET /skills-security/read  → CSP, iframe sandbox, auth, HMAC, constraints
GET /skills-translate/read → translate plugin, markitdown, ingest pipeline

Only read a skill world when you need it for the current task.
