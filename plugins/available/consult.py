"""Consult plugin — two-model consultation pipeline.

Install: lucy install consult

Claude asks a question. Chief of staff (small model) picks relevant worlds.
Advisor (big model) thinks and answers with that context. Claude never
touches the filing cabinet.

Pipeline:
  Claude (question) → chief of staff (select worlds) → advisor (think + answer)

Config: conf/consult.json (hot-pluggable, same mtime pattern as endpoints.json)
  {"advisor": "qwen3:8b", "chief": "qwen3:1.7b", "url": "http://localhost:11434"}

Fallback env vars:
    CONSULT_URL      (default http://localhost:11434)
    CONSULT_ADVISOR  (default qwen3:8b)
    CONSULT_CHIEF    (default qwen3:1.7b)
"""

import json, os, re
from pathlib import Path
from urllib.request import Request, urlopen

DESCRIPTION = "Two-model consultation — chief of staff selects, advisor thinks"
ROUTES = {}

# ── hot-plug config ─────────────────────────────────────────────────────

_CONFIG_FILE = Path(__file__).resolve().parents[2] / "conf" / "consult.json"
_config = {}
_config_mtime = 0

def _reload_config():
    global _config_mtime
    if not _CONFIG_FILE.exists():
        if not _config:
            _config["url"] = os.getenv("CONSULT_URL", "http://localhost:11434")
            _config["advisor"] = os.getenv("CONSULT_ADVISOR", "qwen3:8b")
            _config["chief"] = os.getenv("CONSULT_CHIEF", "qwen3:1.7b")
        return
    try:
        mt = _CONFIG_FILE.stat().st_mtime
        if mt == _config_mtime:
            return
        _config_mtime = mt
        data = json.loads(_CONFIG_FILE.read_text())
        _config.clear()
        _config.update(data)
        _config.setdefault("url", os.getenv("CONSULT_URL", "http://localhost:11434"))
        _config.setdefault("advisor", os.getenv("CONSULT_ADVISOR", "qwen3:8b"))
        _config.setdefault("chief", os.getenv("CONSULT_CHIEF", "qwen3:1.7b"))
    except (json.JSONDecodeError, OSError):
        pass

# ── params schema ───────────────────────────────────────────────────────

PARAMS_SCHEMA = {
    "/consult": {
        "method": "POST",
        "params": {
            "question": {"type": "string", "required": True, "description": "Question to ask"},
            "worlds": {"type": "array", "required": False, "description": "Override world selection (skip chief of staff)"},
        },
        "example": {"question": "how does this system work"},
        "returns": {"answer": "string", "advisor": "string", "chief": "string", "worlds_read": "array"}
    },
}

# ── ollama helper ───────────────────────────────────────────────────────

def _ollama(model, messages, think=False, timeout=120):
    """Call Ollama chat API."""
    print(f"  ollama → {model} ({'think' if think else 'fast'})")
    payload = {"model": model, "messages": messages, "stream": False}
    if not think:
        payload["options"] = {"num_predict": 256}
    req = Request(
        f"{_config['url']}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    r = urlopen(req, timeout=timeout)
    return json.loads(r.read()).get("message", {}).get("content", "")

# ── handler ─────────────────────────────────────────────────────────────

async def handle_consult(method, body, params):
    """POST /consult — ask the AI a question with world context.

    body (JSON):
      {"question": "summarize the readme", "worlds": ["readme"]}

    If `worlds` omitted, a chief-of-staff model picks relevant ones.
    Returns: {"answer", "advisor", "chief", "worlds_read"}.

    Config: /etc/consult.json sets {advisor, chief} ollama models.
    """
    _reload_config()

    try:
        b = json.loads(body) if body and body.strip() else {}
    except json.JSONDecodeError:
        return {"error": "body must be JSON", "_status": 400}
    question = params.get("question") or b.get("question", "")
    if not question:
        return {"error": "question required", "_status": 400}

    explicit_worlds = b.get("worlds")

    if explicit_worlds:
        selected = explicit_worlds
    else:
        # 1. Chief of staff selects worlds
        data_dir = Path(__file__).resolve().parents[2] / "data"
        all_worlds = []
        if data_dir.exists():
            all_worlds = [d.name for d in sorted(data_dir.iterdir())
                          if d.is_dir() and (d / "universe.db").exists()]
        if not all_worlds:
            all_worlds = ["map"]

        try:
            chief_response = _ollama(_config["chief"], [
                {"role": "system", "content": (
                    "You are a chief of staff. Given a question and a list of available worlds, "
                    "select which worlds are relevant. Output ONLY a JSON array of world names. "
                    "Always include 'map' if it exists. Select 1-5 worlds max.\n\n"
                    f"Available worlds: {json.dumps(all_worlds)}"
                )},
                {"role": "user", "content": question},
            ], think=False, timeout=30)

            match = re.search(r'\[.*?\]', chief_response)
            if match:
                selected = json.loads(match.group())
                selected = [w for w in selected if w in all_worlds]
            else:
                selected = ["map"]
        except Exception:
            selected = ["map"]

    if not selected:
        selected = ["map"]

    # ── cache check ────────────────────────────────────────────────────
    try:
        cr = await _call("/proxy/cache/get", body=json.dumps({"key": question, "worlds": selected}).encode())
        if cr.get("hit"):
            print(f"  cache hit → skip ollama")
            cached = json.loads(cr["value"]) if isinstance(cr["value"], str) else cr["value"]
            cached["_cached"] = True
            return cached
    except Exception:
        pass

    # 2. Read selected worlds
    context_parts = []
    for w in selected:
        try:
            c = conn(w)
            row = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            html = row["stage_html"] if row else ""
            if html:
                context_parts.append(f"=== {w} ===\n{html}")
        except Exception:
            context_parts.append(f"=== {w} === (not found)")

    # 3. Read recent events
    try:
        c = conn("default")
        rows = c.execute("SELECT type, payload, created_at FROM event_log ORDER BY id DESC LIMIT 10").fetchall()
        if rows:
            events = "\n".join(f"[{r['created_at']}] {r['type']}: {r['payload']}" for r in reversed(rows))
            context_parts.append(f"=== recent events ===\n{events}")
    except Exception:
        pass

    context = "\n\n".join(context_parts)

    # 4. Advisor thinks and answers
    try:
        answer = _ollama(_config["advisor"], [
            {"role": "system", "content": f"You are a knowledgeable advisor. Answer based on the following context:\n\n{context}"},
            {"role": "user", "content": question},
        ], think=True)
        result = {"answer": answer, "advisor": _config["advisor"], "chief": _config["chief"], "worlds_read": selected}
        # ── cache set ──────────────────────────────────────────────────
        try:
            await _call("/proxy/cache/set", body=json.dumps({"key": question, "value": json.dumps(result), "worlds": selected}).encode())
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": str(e), "advisor": _config["advisor"], "chief": _config["chief"], "worlds_read": selected}


ROUTES["/consult"] = handle_consult
