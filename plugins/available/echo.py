"""Echo plugin — returns body as-is. Test plugin for both runtimes."""
DESCRIPTION = "Echo test — returns request body unchanged"

import sys, json


async def handle_echo(method, body, params):
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    return {"_html": text, "_status": 200}


ROUTES = {"/echo": handle_echo}

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--routes":
        print(json.dumps(list(ROUTES.keys())))
        sys.exit(0)
    d = json.loads(sys.stdin.readline())
    print(json.dumps({"status": 200, "body": d.get("body", "")}))
