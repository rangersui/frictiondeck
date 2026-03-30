# Security Model

## Protocol constraints (physics, not rules)

- connect-src 'self' — browser can only fetch localhost
- X-Auth-Token — all POST routes authenticated
- Approve token — only human at terminal has it
- HMAC chain — event history is immutable
- iframe sandbox — allow-scripts allow-popups (NO allow-same-origin)
- Body limit 5MB
- World names alphanumeric only — no path traversal
- Three mailboxes are independent

## Two-token system

X-Auth-Token: daily operations. AI has it (MCP injects).
X-Approve-Token: constitutional changes. Human only.

AI cannot: load/unload plugins, modify /admin/*, change /config-*
AI can: read/write worlds, propose plugins, use installed plugin routes

## CDN Whitelist

CSP script sources via /config-cdn world.
Default: all HTTPS allowed.
Restricted: write domain names (one per line).

esm.sh
cdn.jsdelivr.net
unpkg.com
cdnjs.cloudflare.com

AI should NOT modify /config-cdn. Human manages security config.

## Renderer security (Figma model)

Renderers run in null-origin iframe.
Cannot directly fetch localhost.
Must use __elastik helper (see /skills-renderer).
Cross-world writes physically blocked.

## Audit

Everything logged in events table. HMAC signed. Chain linked.
Automatic. You don't need to think about this.

## Plugin security

Propose: POST /plugins/propose {name, code, description}
Approve: human only (X-Approve-Token)
You cannot approve your own proposals.
