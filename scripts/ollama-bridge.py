"""ollama-bridge — connect Ollama to elastik.

Usage:
    python ollama-bridge.py ask "draw a blue hello world page" --world default
    python ollama-bridge.py review --world default [--write]
    python ollama-bridge.py watch --world default
    python ollama-bridge.py chat --world default

Requires: requests (pip install requests)
"""

import argparse, json, os, sys, time
import requests

ELASTIK = os.getenv("ELASTIK_URL", "http://localhost:3004")
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
TOKEN = os.getenv("ELASTIK_TOKEN", "")
MODEL = "qwen2.5:1.5b"


def headers():
    h = {}
    if TOKEN:
        h["X-Auth-Token"] = TOKEN
    return h


def elastik_read(world):
    r = requests.get(f"{ELASTIK}/{world}/read")
    return r.json()


def elastik_write(world, content):
    requests.post(f"{ELASTIK}/{world}/write", data=content.encode("utf-8"), headers=headers())


def elastik_append(world, content):
    requests.post(f"{ELASTIK}/{world}/append", data=content.encode("utf-8"), headers=headers())


def ollama_chat(messages, model):
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": model,
        "messages": messages,
        "stream": False,
    })
    return r.json()["message"]["content"]


def ollama_generate(prompt, model):
    r = requests.post(f"{OLLAMA}/api/generate", json={
        "model": model,
        "prompt": prompt,
        "stream": False,
    })
    return r.json()["response"]


# ── ask ──────────────────────────────────────────────────────────────────

def cmd_ask(args):
    prompt = " ".join(args.prompt)
    print(f"Asking {args.model}...")
    response = ollama_generate(prompt, args.model)
    print(response)
    elastik_write(args.world, response)
    print(f"\nWritten to /{args.world}")


# ── review ───────────────────────────────────────────────────────────────

def cmd_review(args):
    state = elastik_read(args.world)
    html = state.get("stage_html", "")
    if not html:
        print(f"/{args.world} is empty."); return

    prompt = f"Review this HTML and suggest improvements. Be specific.\n\n{html}"
    print(f"Reviewing /{args.world} with {args.model}...")
    response = ollama_generate(prompt, args.model)
    print(response)

    if args.write:
        improve_prompt = f"Improve this HTML based on your review. Return only the improved HTML, no explanation.\n\n{html}"
        improved = ollama_generate(improve_prompt, args.model)
        elastik_write(args.world, improved)
        print(f"\nImproved version written to /{args.world}")


# ── watch ────────────────────────────────────────────────────────────────

def cmd_watch(args):
    state = elastik_read(args.world)
    last_version = state.get("version", 0)
    print(f"Watching /{args.world} (v{last_version}). Ctrl+C to stop.")

    try:
        while True:
            time.sleep(5)
            state = elastik_read(args.world)
            v = state.get("version", 0)
            if v > last_version:
                last_version = v
                html = state.get("stage_html", "")
                print(f"\n--- v{v} ---")
                prompt = f"The user updated this page. Summarize what changed and respond helpfully.\n\n{html}"
                response = ollama_generate(prompt, args.model)
                print(response)
                if args.write:
                    elastik_write(args.world, response)
                    print(f"Response written to /{args.world}")
    except KeyboardInterrupt:
        print("\nStopped.")


# ── chat ─────────────────────────────────────────────────────────────────

def cmd_chat(args):
    messages = []
    # Load current stage as context
    state = elastik_read(args.world)
    html = state.get("stage_html", "")
    if html:
        messages.append({"role": "system", "content": f"Current stage content:\n{html}"})
        print(f"Loaded /{args.world} context (v{state.get('version', 0)})")

    print(f"Chat with {args.model}. Type 'quit' to exit.\n")

    try:
        while True:
            user_input = input("you: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                break

            messages.append({"role": "user", "content": user_input})
            response = ollama_chat(messages, args.model)
            messages.append({"role": "assistant", "content": response})
            print(f"\n{args.model}: {response}\n")

            # Append conversation to stage
            entry = f"<div class='chat-entry'><p><b>User:</b> {user_input}</p><p><b>{args.model}:</b> {response}</p></div>\n"
            elastik_append(args.world, entry)

    except (KeyboardInterrupt, EOFError):
        pass

    print(f"\nSession ended. {len(messages)} messages. Stage: /{args.world}")


# ── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="ollama-bridge", description="Connect Ollama to elastik")
    ap.add_argument("--elastik", default=ELASTIK, help="elastik URL")
    ap.add_argument("--ollama", default=OLLAMA, help="Ollama URL")
    ap.add_argument("--model", default=MODEL, help="Ollama model name")
    ap.add_argument("--token", default=TOKEN, help="elastik auth token")
    ap.add_argument("--world", default="default", help="target world")

    sp = ap.add_subparsers(dest="cmd")

    p_ask = sp.add_parser("ask", help="Ask ollama, write response to stage")
    p_ask.add_argument("prompt", nargs="+", help="The prompt")

    p_review = sp.add_parser("review", help="Review current stage with ollama")
    p_review.add_argument("--write", action="store_true", help="Write improved version back")

    p_watch = sp.add_parser("watch", help="Watch stage, respond to changes")
    p_watch.add_argument("--write", action="store_true", help="Write responses back to stage")

    sp.add_parser("chat", help="Interactive chat, appends to stage")

    args = ap.parse_args()

    # Apply overrides
    global ELASTIK, OLLAMA, TOKEN, MODEL
    ELASTIK = args.elastik
    OLLAMA = args.ollama
    TOKEN = args.token
    MODEL = args.model

    cmds = {"ask": cmd_ask, "review": cmd_review, "watch": cmd_watch, "chat": cmd_chat}
    if args.cmd in cmds:
        cmds[args.cmd](args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
