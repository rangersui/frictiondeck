"""GPU — AI as a pluggable device. /dev/gpu accepts text, returns text.

Backend picked by /etc/gpu.conf: one line, scheme://endpoint.
  ollama://localhost:11434
  openai://api.openai.com
  claude://api.anthropic.com
  vast://xxx.proxy.vast.ai
  deepseek://api.deepseek.com

API keys come from env (OPENAI_API_KEY, ANTHROPIC_API_KEY, VAST_API_KEY,
DEEPSEEK_API_KEY). ollama needs none.

Switch at runtime:
  curl -X PUT localhost:3005/etc/gpu.conf \\
    -H "Authorization: Bearer $APPROVE" \\
    -d "vast://xxx.proxy.vast.ai"

Device is blind: no context injection, no world awareness. The caller pipes:
  curl /home/work?raw | curl -X POST /dev/gpu -d @-

Routes:
  POST /dev/gpu          one-shot text in, full text out (existing).
  POST /dev/gpu/stream   same prompt, server-sent-event stream out.
                         Each backend's native stream format (ollama
                         NDJSON / openai-compat SSE-anon / claude
                         SSE-named-event) is unified to `data: <tok>`
                         frames plus a terminal `event: done`.
                         POST only — a stream call costs tokens just
                         like /dev/gpu. Consumer-facing GET streaming
                         lives at /shaped/*, not here.
"""
import asyncio
import json, os, urllib.request, urllib.error
import server

DESCRIPTION = "/dev/gpu — AI as a device. conf in /etc/gpu.conf, keys in env."

SKILL = """\
# /dev/gpu — AI as a pluggable device

POST text → text response. Backend swappable via /etc/gpu.conf.
Pipe-friendly, no context injection: caller composes with other curls.

## Setup

Write backend to /etc/gpu.conf (one line, scheme://endpoint):
  ollama://localhost:11434         (no key)
  openai://api.openai.com          (OPENAI_API_KEY)
  claude://api.anthropic.com       (ANTHROPIC_API_KEY)
  vast://xxx.proxy.vast.ai         (VAST_API_KEY)
  deepseek://api.deepseek.com      (DEEPSEEK_API_KEY)

Model picked by ?model=xxx, else env (OPENAI_MODEL / ANTHROPIC_MODEL / ...),
else per-scheme default.

## Use

  curl -X POST localhost:3005/dev/gpu \\
    -H "Authorization: Bearer $TOKEN" \\
    -d "翻译: hello"

## Switch backend, no restart

  curl -X PUT localhost:3005/etc/gpu.conf \\
    -H "Authorization: Bearer $APPROVE" \\
    -d "vast://xxx.proxy.vast.ai"

## Pipe

  curl /home/article?raw | curl -X POST /dev/gpu -d @-

## Streaming

  curl -X POST localhost:3005/dev/gpu/stream \\
    -H "Authorization: Bearer $TOKEN" \\
    -d "tell me a story"

Returns `text/event-stream`. Each token arrives as `data: <text>\\n\\n`;
stream ends with `event: done`. All backends supported — wire-format
differences (ollama NDJSON / openai-compat SSE / claude SSE-named) are
hidden. Still POST only; a stream call costs the same tokens.
"""

AUTH = "none"  # GET renders man page (browser) / 405 (curl). POST checks auth inline.


# ── HTTP helper ──────────────────────────────────────────────

def _post_json(url, data, headers=None, timeout=180):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── Per-scheme adapters ──────────────────────────────────────

def _ask_ollama(endpoint, prompt, model):
    r = _post_json(f"http://{endpoint}/api/generate",
                   {"model": model or os.getenv("OLLAMA_MODEL", "qwen3:0.6b"),
                    "prompt": prompt, "stream": False})
    return r.get("response", "")


def _ask_openai_compat(endpoint, prompt, model, key_env, default_model_env, default_model):
    key = os.getenv(key_env, "")
    if not key:
        raise RuntimeError(f"{key_env} not set")
    r = _post_json(f"https://{endpoint}/v1/chat/completions",
                   {"model": model or os.getenv(default_model_env, default_model),
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]},
                   {"Authorization": f"Bearer {key}"})
    choices = r.get("choices", [])
    if not choices:
        raise RuntimeError(f"empty response: {str(r)[:200]}")
    return choices[0].get("message", {}).get("content", "")


def _ask_claude(endpoint, prompt, model):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    r = _post_json(f"https://{endpoint}/v1/messages",
                   {"model": model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]},
                   {"x-api-key": key, "anthropic-version": "2023-06-01"})
    parts = r.get("content", [])
    if not parts:
        raise RuntimeError(f"empty response: {str(r)[:200]}")
    return parts[0].get("text", "")


def _dispatch(scheme, endpoint, prompt, model):
    if scheme == "ollama":
        return _ask_ollama(endpoint, prompt, model)
    if scheme == "claude":
        return _ask_claude(endpoint, prompt, model)
    if scheme == "openai":
        return _ask_openai_compat(endpoint, prompt, model,
                                  "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini")
    if scheme == "vast":
        return _ask_openai_compat(endpoint, prompt, model,
                                  "VAST_API_KEY", "VAST_MODEL", "default")
    if scheme == "deepseek":
        return _ask_openai_compat(endpoint, prompt, model,
                                  "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-chat")
    raise RuntimeError(f"unknown scheme: {scheme}")


# ── Streaming adapters (one per backend family) ──────────────
#
# Each is an async generator yielding text tokens only. Backend-
# specific protocol framing (NDJSON vs SSE-anon vs SSE-named) stays
# private to the adapter. Callers (/dev/gpu/stream external route,
# semantic.py in-process) consume a uniform AsyncIterator[str].
#
# urllib.request.urlopen + resp.readline() are blocking; each call is
# wrapped in asyncio.to_thread so the event loop stays free between
# chunks. The existing non-stream _post_json path still blocks the
# loop — that's a separate (pre-existing) concern, explicitly left
# alone for this commit.

async def _stream_ollama(endpoint, prompt, model):
    """ollama /api/generate with stream=true. NDJSON — one JSON per
    line, final line has {"done": true}."""
    body = json.dumps({
        "model": model or os.getenv("OLLAMA_MODEL", "qwen3:0.6b"),
        "prompt": prompt,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(f"http://{endpoint}/api/generate",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=180)
    try:
        while True:
            line = await asyncio.to_thread(resp.readline)
            if not line:
                return
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            tok = obj.get("response") or ""
            if tok:
                yield tok
            if obj.get("done"):
                return
    finally:
        try:
            await asyncio.to_thread(resp.close)
        except Exception:
            pass


async def _stream_openai_compat(endpoint, prompt, model,
                                key_env, default_model_env, default_model):
    """OpenAI-compat /v1/chat/completions with stream=true. SSE
    anonymous `data: <json>\\n\\n` frames, terminator `data: [DONE]`.
    Content lives at choices[0].delta.content; role-only frames (no
    content) are skipped silently."""
    key = os.getenv(key_env, "")
    if not key:
        raise RuntimeError(f"{key_env} not set")
    body = json.dumps({
        "model": model or os.getenv(default_model_env, default_model),
        "max_tokens": 4096,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(f"https://{endpoint}/v1/chat/completions",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=180)
    try:
        while True:
            raw = await asyncio.to_thread(resp.readline)
            if not raw:
                return
            s = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not s.startswith("data:"):
                continue
            payload = s[5:].strip()
            if payload == "[DONE]":
                return
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            tok = delta.get("content") or ""
            if tok:
                yield tok
    finally:
        try:
            await asyncio.to_thread(resp.close)
        except Exception:
            pass


async def _stream_claude(endpoint, prompt, model):
    """Anthropic /v1/messages with stream=true. SSE named-event:
    `event: <name>\\ndata: <json>\\n\\n`. We only care about
    content_block_delta frames (carrying delta.text); message_start /
    content_block_start / ping / message_delta are ignored for output
    but message_stop terminates the stream."""
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = json.dumps({
        "model": model or os.getenv("ANTHROPIC_MODEL",
                                    "claude-sonnet-4-20250514"),
        "max_tokens": 4096,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(f"https://{endpoint}/v1/messages",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=180)
    try:
        current_event = ""
        while True:
            raw = await asyncio.to_thread(resp.readline)
            if not raw:
                return
            s = raw.decode("utf-8", "replace").rstrip("\r\n")
            if s.startswith("event:"):
                current_event = s[6:].strip()
                continue
            if not s.startswith("data:"):
                continue
            if current_event == "message_stop":
                return
            if current_event != "content_block_delta":
                continue
            payload = s[5:].strip()
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            delta = obj.get("delta") or {}
            tok = delta.get("text") or ""
            if tok:
                yield tok
    finally:
        try:
            await asyncio.to_thread(resp.close)
        except Exception:
            pass


def _stream_dispatch(scheme, endpoint, prompt, model):
    """Pick the backend-specific streamer. Returns AsyncIterator[str]
    yielding content-only text tokens. Initialization errors (missing
    key, unknown scheme) raise here rather than inside the iterator
    so callers can distinguish 'never started' from 'failed mid-stream'."""
    if scheme == "ollama":
        return _stream_ollama(endpoint, prompt, model)
    if scheme == "claude":
        return _stream_claude(endpoint, prompt, model)
    if scheme == "openai":
        return _stream_openai_compat(endpoint, prompt, model,
                                     "OPENAI_API_KEY", "OPENAI_MODEL",
                                     "gpt-4o-mini")
    if scheme == "vast":
        return _stream_openai_compat(endpoint, prompt, model,
                                     "VAST_API_KEY", "VAST_MODEL",
                                     "default")
    if scheme == "deepseek":
        return _stream_openai_compat(endpoint, prompt, model,
                                     "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
                                     "deepseek-chat")
    raise RuntimeError(f"unknown scheme: {scheme}")


def _sse_data(text: str) -> bytes:
    """Wire-format a text token as one SSE data frame. Every '\\n' in
    the payload becomes its own `data:` line so multi-line content
    reassembles correctly on the client. Frame terminated by `\\n\\n`."""
    if not text:
        return b""
    lines = text.split("\n")
    return ("".join(f"data: {ln}\n" for ln in lines) + "\n").encode("utf-8")


def _sse_event(name: str, payload: str = "") -> bytes:
    """Named SSE event. Newlines in payload are stripped — event-type
    frames are short metadata, not content."""
    safe = payload.replace("\n", " ").replace("\r", " ")
    return f"event: {name}\ndata: {safe}\n\n".encode("utf-8")


# ── Config reader ────────────────────────────────────────────

def _read_conf():
    """First non-empty line of /etc/gpu.conf, parsed as scheme://endpoint."""
    try:
        c = server.conn("etc/gpu.conf")
        r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
        raw = r["stage_html"] if r else b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        for line in (raw or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "://" not in s:
                return None, None
            scheme, rest = s.split("://", 1)
            return scheme.strip().lower(), rest.strip().rstrip("/")
    except Exception:
        pass
    return None, None


# ── Route handler ────────────────────────────────────────────

async def handle(method, body, params):
    """POST /dev/gpu — AI as a pluggable device. Text in, text out.

    body: raw prompt (no JSON wrapper).
    query: ?model=xxx overrides the backend's default model.

    Backend from /etc/gpu.conf (scheme://endpoint, e.g. ollama://...,
    openai://api.openai.com, claude://api.anthropic.com, vast://...).
    Keys via env: OPENAI_API_KEY / ANTHROPIC_API_KEY / VAST_API_KEY /
    DEEPSEEK_API_KEY. Ollama needs no key.

    Pipe-friendly:
      curl /home/article?raw | curl -X POST /dev/gpu --data-binary @-

    /dev/gpu/stream is the streaming sibling; see _handle_stream.
    This function dispatches there when scope.path names it, so
    elastik's prefix-matching loader reaches both through a single
    plugin file without a ROUTES list change.
    """
    scope = params.get("_scope", {})
    if scope.get("path", "").rstrip("/") == "/dev/gpu/stream":
        return await _handle_stream(method, body, params)
    if method != "POST":
        return {"error": "POST only — body=prompt, response=text/plain",
                "_status": 405}
    # POST is the mutating action — costs API $. Gate inline so browser GET
    # can still render the man page (plugin AUTH="none").
    #
    # Trusted-loopback bypass: an in-process plugin (today router.py;
    # future readers of /dev/gpu as infrastructure) may stamp
    # scope["_internal_caller"] = "<plugin>" to signal "this is a
    # server-side loopback call, not an external user request." The
    # sentinel is a top-level ASGI scope key, which the server builds;
    # external HTTP clients cannot set it (their headers land in
    # scope["headers"], never as top-level scope keys), so it is not
    # forgeable from the wire. Same non-forgeability the
    # `_router_triggered` sentinel in server.py relies on.
    #
    # Why this is necessary: router.py's whole purpose is resolving
    # typo / natural-language URLs for anonymous (T1) callers. Those
    # callers are not directly authorised for /dev/gpu and never will
    # be — but the router legitimately uses the SLM as internal
    # infrastructure on their behalf, behind its own rate cap and
    # caller-scoped pool. Without this bypass, the T1 branch — the
    # most important one router exists for — would never reach the
    # model and would always degrade to slm-unavailable-static-404.
    internal_caller = scope.get("_internal_caller")
    if not internal_caller and not server._check_auth(scope):
        return {"error": "auth required — T2 token or cap token scoped to /dev/gpu",
                "_status": 401,
                "_headers": [["www-authenticate", 'Basic realm="elastik"']]}
    prompt = body if isinstance(body, str) else body.decode("utf-8", "replace")
    prompt = prompt.strip()
    if not prompt:
        return {"error": "empty prompt", "_status": 400}

    scheme, endpoint = _read_conf()
    if not scheme:
        return {"error": "no /etc/gpu.conf — write one: scheme://endpoint",
                "_status": 503}

    # _dispatch is sync and spends most of its wall-clock inside
    # _post_json's blocking urllib.request.urlopen + full-body read.
    # Awaiting it directly in this async handler (the pre-refactor
    # shape) stalled the event loop for the entire model round-trip —
    # no other request could be served while the SLM was thinking.
    # asyncio.to_thread runs the sync call on the loop's default thread
    # pool and yields back here when the backend returns. Zero
    # protocol change: _dispatch itself, _post_json's urllib usage,
    # the response-envelope shape, the audit event, and the
    # 502-on-exception mapping all stay byte-for-byte the same.
    # Exceptions propagate out of the thread and are caught here as
    # if _dispatch had raised synchronously (asyncio.to_thread does
    # the right thing there). /dev/gpu/stream is untouched — its
    # blocking is already wrapped per-readline inside the streamers.
    try:
        text = await asyncio.to_thread(
            _dispatch, scheme, endpoint, prompt, params.get("model"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return {"error": f"{scheme} HTTP {e.code}: {detail}", "_status": 502}
    except Exception as e:
        return {"error": f"{scheme} error: {e}", "_status": 502}

    server.log_event("dev/gpu", "gpu_call",
                     {"scheme": scheme, "prompt_len": len(prompt),
                      "reply_len": len(text),
                      "caller": internal_caller or "external"})
    return {"_body": text, "_ct": "text/plain"}


async def _handle_stream(method, body, params):
    """POST /dev/gpu/stream — same prompt shape as /dev/gpu, streamed.

    Two calling modes:

    External HTTP caller
        Consumes the per-backend stream via params["_send"], wraps
        each token as `data: <text>\\n\\n`, ends with `event: done`.
        Returns None so server.py:765 skips the default envelope.
        Audit log fires once at clean close with total reply length.

    In-process caller (semantic.py via its _call_gpu_stream)
        Sets params["_stream_in_process"]=True. We return the raw
        AsyncIterator[str] without opening an HTTP response — the
        caller frames its own SSE (/shaped/ outer transport) and
        writes its own audit event at the /shaped/ boundary. No
        gpu_stream_call log from this path to avoid double-counting
        a single user request.

    POST only. A stream call costs the same tokens as /dev/gpu;
    GET is reserved for /shaped/* (consumer-facing) and the man page.
    """
    if method != "POST":
        return {"error": "POST only — body=prompt, response=text/event-stream",
                "_status": 405}
    scope = params.get("_scope", {})
    # Trusted-loopback bypass — see matching comment in handle() above.
    # Streaming path is reachable both via external POST /dev/gpu/stream
    # AND via semantic.py's _call_gpu_stream in-process bridge. The
    # latter already exits this handler early via _stream_in_process,
    # so the bypass here mainly covers a hypothetical future in-process
    # caller that WANTS the SSE framing (e.g. a router variant that
    # streams suggestions). Keep parity with handle().
    internal_caller = scope.get("_internal_caller")
    if not internal_caller and not server._check_auth(scope):
        return {"error": "auth required — T2 token or cap token scoped to /dev/gpu",
                "_status": 401,
                "_headers": [["www-authenticate", 'Basic realm="elastik"']]}
    prompt = body if isinstance(body, str) else body.decode("utf-8", "replace")
    prompt = prompt.strip()
    if not prompt:
        return {"error": "empty prompt", "_status": 400}

    scheme, endpoint = _read_conf()
    if not scheme:
        return {"error": "no /etc/gpu.conf — write one: scheme://endpoint",
                "_status": 503}

    # Initialisation errors (missing key, unknown scheme) surface as
    # a 502 dict before any HTTP response is committed. Post-first-
    # chunk errors can only be surfaced as SSE `event: error` frames.
    try:
        chunks = _stream_dispatch(scheme, endpoint, prompt, params.get("model"))
    except Exception as e:
        return {"error": f"{scheme} stream init failed: {e}",
                "_status": 502}

    # In-process bridge: hand the raw iterator to the caller. They own
    # framing + audit from here on.
    if params.get("_stream_in_process"):
        return chunks

    # External HTTP path. params["_send"] must exist for SSE to work.
    send = params.get("_send")
    if send is None:
        return {"error": "server does not expose raw send; SSE unavailable",
                "_status": 500}

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"text/event-stream; charset=utf-8"],
            [b"cache-control", b"no-cache"],
        ],
    })

    total_len = 0
    errored = False
    try:
        async for tok in chunks:
            if not tok:
                continue
            await send({
                "type": "http.response.body",
                "body": _sse_data(tok),
                "more_body": True,
            })
            total_len += len(tok)
    except urllib.error.HTTPError as e:
        errored = True
        await send({"type": "http.response.body",
                    "body": _sse_event("error", f"{scheme} HTTP {e.code}"),
                    "more_body": True})
    except Exception as e:
        errored = True
        await send({"type": "http.response.body",
                    "body": _sse_event("error", f"{scheme}: {e}"),
                    "more_body": True})

    # Clean close. 'done' arrives even on errored streams so clients
    # see a terminal marker either way — event: error is informative,
    # event: done is the structural end.
    await send({"type": "http.response.body",
                "body": _sse_event("done", "{}"),
                "more_body": True})
    await send({"type": "http.response.body",
                "body": b"",
                "more_body": False})

    server.log_event(
        "dev/gpu", "gpu_stream_call",
        {"scheme": scheme, "prompt_len": len(prompt),
         "reply_len": total_len, "errored": errored,
         "caller": internal_caller or "external"})
    return None


ROUTES = ["/dev/gpu"]
