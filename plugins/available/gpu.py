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
  curl -X PUT localhost:3005/home/etc/gpu.conf \\
    -H "Authorization: Bearer $APPROVE" \\
    -d "vast://xxx.proxy.vast.ai"

Device is blind: no context injection, no world awareness. The caller pipes:
  curl /home/work?raw | curl -X POST /dev/gpu -d @-
"""
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

  curl -X PUT localhost:3005/home/etc/gpu.conf \\
    -H "Authorization: Bearer $APPROVE" \\
    -d "vast://xxx.proxy.vast.ai"

## Pipe

  curl /home/article?raw | curl -X POST /dev/gpu -d @-
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
    """
    if method != "POST":
        return {"error": "POST only — body=prompt, response=text/plain",
                "_status": 405}
    # POST is the mutating action — costs API $. Gate inline so browser GET
    # can still render the man page (plugin AUTH="none").
    scope = params.get("_scope", {})
    if not server._check_auth(scope):
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

    try:
        text = _dispatch(scheme, endpoint, prompt, params.get("model"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return {"error": f"{scheme} HTTP {e.code}: {detail}", "_status": 502}
    except Exception as e:
        return {"error": f"{scheme} error: {e}", "_status": 502}

    server.log_event("dev/gpu", "gpu_call",
                     {"scheme": scheme, "prompt_len": len(prompt), "reply_len": len(text)})
    return {"_body": text, "_ct": "text/plain"}


ROUTES = ["/dev/gpu"]
