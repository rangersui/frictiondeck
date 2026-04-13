# elastik Plugin Specification

Two runtimes. Same routes. Same behavior. Different mechanisms.

## Python (in-process)

Python plugins are `.py` files in `plugins/`. Loaded at startup
via `exec()`. They run inside the server process.

### Required exports

```python
DESCRIPTION = "One-line description"
ROUTES = {"/path": handler_function}
```

### Handler signature

```python
async def handler(method: str, body: bytes|str, params: dict) -> dict
```

Return a plain dict → json.dumps'd automatically.

Special keys in return dict:
- `_status: int` → HTTP status code (default 200)
- `_redirect: str` → 302 redirect
- `_cookies: [str]` → Set-Cookie headers
- `_html: str` → return HTML instead of JSON

### Optional exports

```python
AUTH_MIDDLEWARE = async def(scope, path, method) → bool
PARAMS_SCHEMA = {"/route": {"method": "POST", "params": {...}}}
OPS_SCHEMA = [{"op": "name", "params": {...}}]
```

### Injected globals

Plugin namespace automatically includes:
- `conn(name)` → get SQLite connection for a world
- `log_event(name, type, payload)` → write to audit chain
- `load_plugin`, `unload_plugin`, `_plugins`, `_plugin_meta`

No imports needed. Dependency injection via exec().

### File locations

- `plugins/` → installed (loaded at startup)
- `plugins/available/` → available (install via admin plugin)

### Hot Plug

- `load_plugin(name)` — loads from `plugins/{name}.py`
- `unload_plugin(name)` — removes routes from memory, file stays
- Reload = unload + load

---

## Go Lite (CGI, any language)

Go Lite runs plugins as external processes. Any executable that follows
the stdin/stdout protocol is a valid plugin. Language doesn't matter.

### Five rules

1. Plugin is an executable file in `plugins/`
2. `plugin --routes` → stdout: JSON array of routes it handles
3. Request → stdin: one line JSON `{"path", "method", "body", "query"}`
4. Response → stdout: one line JSON `{"status", "body"}`
5. Exit code 0 → normal, non-zero → 502

### How it works

**Startup**: Go scans `plugins/`, runs each with `--routes`, registers
the declared routes. `.py` files are run via `python -u`.

**Request**: HTTP request hits a plugin route → Go spawns the plugin →
sends request as one JSON line on stdin → reads one JSON line from stdout
→ returns to client. Process exits after each request.

### Request format (stdin)

```json
{"path": "/ai/ask", "method": "POST", "body": "hello", "query": "world=work"}
```

### Response format (stdout)

```json
{"status": 200, "body": "response text", "content_type": "text/plain"}
```

`content_type` is optional (defaults to `application/json`).

### Example: echo plugin (Python)

```python
#!/usr/bin/env python3
import sys, json

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--routes":
        print(json.dumps(["/echo"]))
        sys.exit(0)
    d = json.loads(sys.stdin.readline())
    print(json.dumps({"status": 200, "body": d["body"]}))
```

### Example: echo plugin (Bash)

```bash
#!/bin/bash
if [ "$1" = "--routes" ]; then
  echo '["/echo"]'
  exit 0
fi
read line
body=$(echo "$line" | jq -r .body)
echo "{\"status\":200,\"body\":\"$body\"}"
```

### Example: echo plugin (Go)

```go
package main

import (
    "encoding/json"
    "fmt"
    "os"
)

func main() {
    if len(os.Args) > 1 && os.Args[1] == "--routes" {
        fmt.Println(`["/echo"]`)
        return
    }
    var req map[string]string
    json.NewDecoder(os.Stdin).Decode(&req)
    out, _ := json.Marshal(map[string]any{"status": 200, "body": req["body"]})
    fmt.Println(string(out))
}
```

---

## Alignment

Both runtimes expose the same routes to the client. The client does not
know whether `/ai/ask` is handled by a Python in-process handler or a
Go-spawned bash script. Blind pipe.

| | Python | Go Lite |
|---|--------|---------|
| Mechanism | `exec()` import | `os/exec` spawn |
| Route declaration | `ROUTES` dict | `--routes` flag |
| Data flow | function call | stdin/stdout JSON |
| Isolation | in-process | per-process |
| Hot plug | load/unload at runtime | restart to rescan |
| Language | Python only | any executable |

Python plugins in `plugins/available/` serve as the reference
implementation. To write a Go Lite plugin in any language, just
follow the five stdin/stdout rules above.
