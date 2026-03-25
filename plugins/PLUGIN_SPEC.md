# elastik Plugin Specification

Every plugin is a .py file in plugins/. server.py loads them at startup.

## Required exports

```python
DESCRIPTION = "One-line description"
ROUTES = {"/path": handler_function}
```

## Optional exports

```python
AUTH_MIDDLEWARE = async def(scope, path, method) → bool
OPS_SCHEMA = [{"op": "name", "params": {...}}]  # if plugin has operation types
PARAMS_SCHEMA = {
    "/route/path": {
        "method": "POST",
        "params": {
            "field_name": {"type": "string", "required": True, "description": "..."},
        },
        "example": {"field": "value"},
        "returns": {"field": "type"}
    }
}
```

## Handler signature

```python
async def handler(method: str, body: bytes|str, params: dict) -> dict
```

Return a plain dict → json.dumps'd automatically.

Special keys in return dict:
- `_status: int` → HTTP status code (default 200)
- `_redirect: str` → 302 redirect
- `_cookies: [str]` → Set-Cookie headers
- `_html: str` → return HTML instead of JSON

## Injected globals

Plugin namespace automatically includes:
- `conn(name)` → get SQLite connection for a world
- `log_event(name, type, payload)` → write to audit chain

No imports needed. Dependency injection via exec().

## /info endpoint

GET /info collects from all plugins:
- name (filename without .py)
- DESCRIPTION
- ROUTES
- PARAMS_SCHEMA (if exported)
- OPS_SCHEMA (if exported)

AI calls GET /info → gets complete self-describing capability map → zero guessing.

## File locations

- `plugins/` → installed (loaded at startup)
- `plugins/available/` → available (install via `lucy install <name>`)
