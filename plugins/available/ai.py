"""AI plugin — auto-detect provider, expose /ai/status and /ai/ask.

Two modes, one file:
  Python in-process: server loads ROUTES dict via exec()
  Go CGI:            go exec python ai.py --routes / stdin JSON
"""
DESCRIPTION = "AI provider detection and prompt relay (ollama/claude/openai/deepseek/google)"

import json, os, sys, re, urllib.request, urllib.error

_VALID_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')


# ── Shared logic ────────────────────────────────────────────────────

def _env(key, default=""):
    return os.environ.get(key, default) or default


def _probe_ollama(host):
    try:
        req = urllib.request.Request(host + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _resolve_model(wanted, available):
    if not wanted:
        return available[0]
    if wanted in available:
        return wanted
    for m in available:
        if m.startswith(wanted):
            return m
    return available[0]


def _detect_ai():
    host = _env("OLLAMA_HOST", "http://localhost:11434")
    models = _probe_ollama(host)
    if models:
        model = _resolve_model(_env("OLLAMA_MODEL"), models)
        print(f"  ai: ollama at {host} ({model})", file=sys.stderr)
        return {"provider": "ollama", "model": model, "status": "connected",
                "_base_url": host, "_api_key": ""}

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        model = _env("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        print(f"  ai: Claude API ({model})", file=sys.stderr)
        return {"provider": "claude", "model": model, "status": "connected",
                "_base_url": "https://api.anthropic.com", "_api_key": key}

    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        model = _env("OPENAI_MODEL", "gpt-4o-mini")
        print(f"  ai: OpenAI API ({model})", file=sys.stderr)
        return {"provider": "openai", "model": model, "status": "connected",
                "_base_url": "https://api.openai.com", "_api_key": key}

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        model = _env("DEEPSEEK_MODEL", "deepseek-chat")
        print(f"  ai: DeepSeek API ({model})", file=sys.stderr)
        return {"provider": "deepseek", "model": model, "status": "connected",
                "_base_url": "https://api.deepseek.com", "_api_key": key}

    key = os.environ.get("GOOGLE_API_KEY", "")
    if key:
        model = _env("GOOGLE_MODEL", "gemini-2.0-flash")
        print(f"  ai: Google Gemini API ({model})", file=sys.stderr)
        return {"provider": "google", "model": model, "status": "connected",
                "_base_url": "https://generativelanguage.googleapis.com", "_api_key": key}

    print("  ai: no provider detected", file=sys.stderr)
    return {"provider": "none", "model": "", "status": "no provider",
            "hint": "curl -fsSL https://ollama.com/install.sh | sh && ollama pull gemma3:4b",
            "_base_url": "", "_api_key": ""}


def _post_json(url, data, headers=None, timeout=120):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _ask_ollama(base_url, model, prompt):
    r = _post_json(base_url + "/api/generate",
                   {"model": model, "prompt": prompt, "stream": False})
    return r.get("response", "")


def _ask_claude(api_key, model, prompt):
    r = _post_json("https://api.anthropic.com/v1/messages",
                   {"model": model, "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]},
                   {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    parts = r.get("content", [])
    if not parts:
        raise ValueError("claude: empty response")
    return parts[0].get("text", "")


def _ask_openai_compat(base_url, api_key, model, prompt):
    r = _post_json(base_url + "/v1/chat/completions",
                   {"model": model, "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]},
                   {"Authorization": "Bearer " + api_key})
    choices = r.get("choices", [])
    if not choices:
        raise ValueError("openai-compat: empty response")
    return choices[0].get("message", {}).get("content", "")


def _ask_google(base_url, api_key, model, prompt):
    url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"
    r = _post_json(url, {"contents": [{"parts": [{"text": prompt}]}]})
    cands = r.get("candidates", [])
    if not cands or not cands[0].get("content", {}).get("parts", []):
        raise ValueError("google: empty response")
    return cands[0]["content"]["parts"][0].get("text", "")


def _ask_ai(cfg, prompt):
    p = cfg["provider"]
    base, key, model = cfg["_base_url"], cfg["_api_key"], cfg["model"]
    if p == "ollama":
        return _ask_ollama(base, model, prompt)
    if p == "claude":
        return _ask_claude(key, model, prompt)
    if p in ("openai", "deepseek"):
        return _ask_openai_compat(base, key, model, prompt)
    if p == "google":
        return _ask_google(base, key, model, prompt)
    raise ValueError("no AI provider configured")


_ai_cfg = _detect_ai()


# ── Python in-process handlers (loaded via ROUTES) ─────

async def handle_status(method, body, params):
    out = {"provider": _ai_cfg["provider"], "model": _ai_cfg["model"],
           "status": _ai_cfg["status"]}
    if _ai_cfg.get("hint"):
        out["hint"] = _ai_cfg["hint"]
    return out


async def handle_ask(method, body, params):
    if method != "POST":
        return {"error": "POST only", "_status": 405}
    prompt = body if isinstance(body, str) else body.decode("utf-8", "replace")
    if not prompt.strip():
        return {"error": "empty prompt", "_status": 400}
    if _ai_cfg["provider"] == "none":
        return {"error": "no AI provider — install ollama or set ANTHROPIC_API_KEY",
                "_status": 503}
    world_name = params.get("world", "")
    if world_name and _VALID_NAME.match(world_name):
        try:
            c = conn(world_name)
            r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            if r and r["stage_html"]:
                prompt = ("Current content of this world:\n\n" + r["stage_html"]
                          + "\n\n---\n\nUser request: " + prompt)
        except Exception:
            pass
    try:
        response = _ask_ai(_ai_cfg, prompt)
    except Exception as e:
        return {"error": f"ai: {e}", "_status": 502}
    return {"_html": response, "_status": 200}


ROUTES = {"/ai/status": handle_status, "/ai/ask": handle_ask}

PARAMS_SCHEMA = {
    "/ai/status": {
        "method": "GET",
        "returns": {"provider": "string", "model": "string", "status": "string"}
    },
    "/ai/ask": {
        "method": "POST",
        "params": {
            "world": {"type": "string", "required": False,
                      "description": "world name to include as context"}
        },
        "returns": {"response": "string (text/plain)"}
    }
}


# ── Go CGI entry point (stdin/stdout JSON) ──────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--routes":
        print(json.dumps(list(ROUTES.keys())))
        sys.exit(0)

    d = json.loads(sys.stdin.readline())
    path, method, body = d["path"], d.get("method", "GET"), d.get("body", "")

    if path == "/ai/status":
        out = {"provider": _ai_cfg["provider"], "model": _ai_cfg["model"],
               "status": _ai_cfg["status"]}
        if _ai_cfg.get("hint"):
            out["hint"] = _ai_cfg["hint"]
        print(json.dumps({"status": 200, "body": json.dumps(out)}))
    elif path == "/ai/ask":
        if method != "POST":
            print(json.dumps({"status": 405, "body": json.dumps({"error": "POST only"})}))
        elif not body.strip():
            print(json.dumps({"status": 400, "body": json.dumps({"error": "empty prompt"})}))
        elif _ai_cfg["provider"] == "none":
            print(json.dumps({"status": 503, "body": json.dumps({"error": "no AI provider"})}))
        else:
            try:
                response = _ask_ai(_ai_cfg, body)
                print(json.dumps({"status": 200, "body": response,
                                  "content_type": "text/plain; charset=utf-8"}))
            except Exception as e:
                print(json.dumps({"status": 502, "body": json.dumps({"error": f"ai: {e}"})}))
    else:
        print(json.dumps({"status": 404, "body": json.dumps({"error": "not found"})}))
