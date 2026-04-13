"""ollama-bridge — minimal Ollama ↔ elastik loop.

Usage:
    python ollama-bridge.py                          # read default world, ask ollama, write back
    python ollama-bridge.py "draw a blue page"       # ask with custom prompt
    python ollama-bridge.py --world work             # target a different world
    python ollama-bridge.py --watch                  # loop: respond to every stage change

Env vars:
    ELASTIK_URL   (default http://localhost:3004)
    OLLAMA_URL    (default http://localhost:11434)
    ELASTIK_TOKEN (default empty)
    OLLAMA_MODEL  (default qwen3:8b)
"""

import os, sys, time, json
from urllib.request import Request, urlopen

ELASTIK = os.getenv("ELASTIK_URL", "http://localhost:3004")
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")


def read(world):
    return json.loads(urlopen(f"{ELASTIK}/{world}/read").read())


def write(world, content):
    h = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    req = Request(f"{ELASTIK}/{world}/write", data=content.encode("utf-8"), headers=h, method="POST")
    try:
        urlopen(req)
    except Exception as e:
        print(f"Write failed: {e}", file=sys.stderr)


def ask(prompt):
    body = json.dumps({"model": MODEL, "stream": False,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = Request(f"{OLLAMA}/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urlopen(req).read())["message"]["content"]


def main():
    world = "default"
    prompt = None
    watch = False

    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--world" and args: world = args.pop(0)
        elif a == "--watch": watch = True
        else: prompt = a

    if watch:
        v = read(world).get("version", 0)
        print(f"Watching /{world} (v{v}). Ctrl+C to stop.")
        while True:
            time.sleep(3)
            state = read(world)
            if state.get("version", 0) > v:
                v = state["version"]
                reply = ask(state.get("stage_html", ""))
                write(world, reply)
                print(f"v{v} → responded")
    else:
        state = read(world)
        prompt = prompt or state.get("stage_html", "") or "say hello"
        reply = ask(prompt)
        write(world, reply)
        print(f"Written to /{world}")


if __name__ == "__main__":
    main()
