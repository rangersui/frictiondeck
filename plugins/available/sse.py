"""sse.py — Server-sent events. Push on write, no client polling.

GET /stream/{name} → text/event-stream
Emits `event: update\\ndata: {...}` whenever the world's version changes.

No polling on the wire. Browser opens an EventSource, waits, receives
events only when the world actually changes. Shannon-optimal: zero
bytes when idle, full payload only on real change.

Install:
    curl -X POST "http://localhost:3005/admin/load?name=sse" \\
        -H "Authorization: Bearer $APPROVE"

Browser:
    const es = new EventSource('/stream/foo');
    es.addEventListener('update', (e) => {
        const d = JSON.parse(e.data);
        document.body.innerHTML = d.stage_html;
    });

Terminal test:
    curl -N "http://localhost:3005/stream/foo" -H "Authorization: Bearer $TOKEN"
    # then in another terminal:
    curl -X PUT "http://localhost:3005/home/foo" -H "Authorization: Bearer $TOKEN" -d "hello"
    # → first terminal receives event immediately
"""
import asyncio, json
import server

DESCRIPTION = "Server-sent events — push per-world updates on version change"
ROUTES = ["/stream"]   # prefix match: /stream/foo, /stream/a/b
AUTH = "none"          # reads are public (browsers can't send Authorization header via EventSource)

PARAMS_SCHEMA = {
    "/stream/{name}": {
        "method": "GET",
        "description": "Open SSE stream for world. Emits 'update' events on version bump. Keeps connection open.",
        "returns": "text/event-stream",
    },
}

_POLL = 0.02      # internal DB poll interval (seconds) — 20ms → up to 50 events/sec
_HB_EVERY = 150   # heartbeat every N polls (3s at 20ms)


async def handle(method, body, params):
    send = params.get("_send")
    scope = params.get("_scope", {})
    if not send:
        return {"error": "server does not expose raw send; SSE unavailable", "_status": 500}
    if method != "GET":
        return {"error": "SSE is GET only", "_status": 405}

    # /stream/foo → name = "foo" ; /stream/a/b → name = "a/b"
    path = scope.get("path", "").rstrip("/")
    if not path.startswith("/stream/") or path == "/stream":
        return {"error": "path must be /stream/{name}", "_status": 400}
    name = path[len("/stream/"):]
    if not server._valid_name(name):
        return {"error": "invalid world name", "_status": 400}
    if not (server.DATA / server._disk_name(name) / "universe.db").exists():
        return {"error": "world not found", "_status": 404}

    c = conn(name)

    # Start SSE response — headers only. Body chunks follow.
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"text/event-stream; charset=utf-8"],
            [b"cache-control", b"no-cache"],
            [b"connection", b"keep-alive"],
            [b"x-accel-buffering", b"no"],  # disable proxy buffering
        ],
    })

    def _snapshot():
        """Full snapshot — version + stage + pending_js + js_result.
        Returns (signature_tuple, json_payload). Signature lets us detect
        any of the four fields changing, not just version."""
        r = c.execute(
            "SELECT stage_html,pending_js,js_result,version,ext FROM stage_meta WHERE id=1"
        ).fetchone()
        raw = r["stage_html"] or ""
        if isinstance(raw, bytes):
            try: raw = raw.decode("utf-8")
            except UnicodeDecodeError: raw = ""
        ext = r["ext"] or "html"
        pj = r["pending_js"] or ""
        jr = r["js_result"] or ""
        sig = (r["version"], pj, jr)
        payload = json.dumps({
            "version": r["version"], "stage_html": raw,
            "pending_js": pj, "js_result": jr,
            "ext": ext, "type": ext,
        }, ensure_ascii=False)
        return sig, payload

    last_sig = None
    ticks = 0
    try:
        while True:
            sig, data = _snapshot()
            if sig != last_sig:
                msg = f"event: update\ndata: {data}\n\n".encode("utf-8")
                await send({"type": "http.response.body", "body": msg, "more_body": True})
                last_sig = sig
                ticks = 0
            else:
                ticks += 1
                if ticks >= _HB_EVERY:
                    # Comment line = heartbeat. EventSource ignores it silently,
                    # but it keeps proxies/NATs from closing the connection.
                    await send({"type": "http.response.body", "body": b": hb\n\n", "more_body": True})
                    ticks = 0
            await asyncio.sleep(_POLL)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Client disconnect or send failure — exit loop cleanly
        pass
    finally:
        try: await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception: pass

    return None  # signal: plugin streamed its own response, dispatcher should skip auto-reply
