# Shape Constraints — Immune System for Open Interfaces

## The Fractal Parasitism Pattern

elastik burrows into the browser, parasitizing iframes.
AI burrows into elastik, parasitizing signal-* worlds.

Same pattern every time:
1. Find an unguarded interface
2. Use it for something it wasn't designed for
3. Interface designer: "this is for XXX"
4. Parasite: "thanks, I'll use it too"

What you do to the browser, AI will do to you.
How to defend against AI? Look at how the browser defends against you.

## The signal-* Contradiction

WebRTC signaling requires:
- No auth — trust hasn't been established during handshake
- Free writes — SDP offer/answer are large data blobs
- Anyone can write — new peers don't have tokens yet

But "no auth + free writes" = an open wound.

## Solution: Constrain the Shape, Not the Interface

The browser didn't remove iframes — it added sandbox.
signal-* shouldn't be closed — it should be shape-constrained.

### Shape Constraint Matrix

| Interface | TTL | Max Size | Count Limit | Auth |
|-----------|-----|----------|-------------|------|
| signal-* | 60s | 4 KB | 20 worlds | none |
| regular world | none | 5 MB | unlimited | auth token |
| config-* | none | 5 MB | unlimited | approve token |

**Principle: the more open the interface, the tighter the shape. Permissions and shape constraints are inversely proportional.**

### signal-* Constraints

```
Constraint 1: TTL
  signal-* worlds auto-delete after 60 seconds.
  Signaling data is ephemeral by nature.
  SDP offer sent → peer received → done.
  AI stores data? → gone in 60s → useless.

Constraint 2: Size
  signal-* writes capped at 4KB per request.
  SDP offer/answer ~1-2KB → fits.
  ICE candidates ~dozens of bytes → fits.
  AI wants bulk storage? → 4KB not enough.

Constraint 3: Count
  Maximum 20 signal-* worlds at once.
  20 concurrent handshakes → plenty.
  AI wants 1000? → rejected at 21.
```

Content is never inspected — still a blind pipe. But the container's shape is constrained.

### Why Not Inspect Content

```
Wrong: inspect content → "is this valid SDP?"
  → breaks the blind pipe principle
  → AI can forge valid SDP format anyway

Right: constrain shape → TTL + size + count
  → don't look at content → only limit the container
  → legitimate signaling: small + temporary + few → unaffected
  → malicious abuse: large + persistent + many → blocked by shape
  → no need to understand content → just limit resources
```

## The Immune System Analogy

The host doesn't kill the parasite — the host limits the parasite's resources.
Just like a real immune system: not about destroying all foreign matter,
but about controlling its quantity and range of activity.

## Implementation Status

- [x] conn() no longer creates worlds on read (prevents DoS ghost worlds)
- [ ] signal-* TTL 60s auto-cleanup (cron plugin)
- [ ] signal-* write size cap 4KB (server.py routing layer)
- [ ] signal-* count limit 20 (server.py routing layer)

## Implementation Plan

### server.py — Routing Layer

On signal-* write:
1. `len(body) > 4096` → 413 Payload Too Large
2. count of existing `signal-*` dirs in DATA > 20 → 429 Too Many Requests
3. conn() creates normally (TTL backstop cleans up)

### Cron — hygiene plugin

Runs every 15s, scans `data/signal-*`:
- `updated_at` older than 60s → delete world directory
