# First-Call Load + Idle Unload for Plugins

## Concept
Don't load all plugins at startup. Register lightweight route stubs that trigger `load_plugin()` on first request, then forward the request. Unload plugins after an idle timeout.

## Design

### The problem

`load_plugins()` in server.py loads every `.py` file in the `plugins/` directory at startup. Each plugin executes its top-level code, registers routes, and may start CRON tasks. With 10+ plugins, this adds startup time, memory overhead, and CRON load even for plugins that are rarely used.

### Stub registration

Instead of calling `load_plugin()` for each file, register a stub route that intercepts the first request:

```python
# In server.py, replaces the current load_plugins() behavior when JIT is enabled

def _register_stub(plugin_name, routes):
    """Register placeholder routes that load the real plugin on first hit."""
    async def _stub_handler(method, body, params):
        # First request — load the real plugin
        load_plugin(plugin_name)
        _jit_state[plugin_name] = {"loaded_at": time.time(), "last_used": time.time()}

        # Forward this request to the now-loaded real handler
        handler = _plugins.get(params.get("_original_route"))
        if handler:
            return await handler(method, body, params)
        return {"error": f"plugin {plugin_name} loaded but route not found", "_status": 500}

    for route in routes:
        _plugins[route] = _stub_handler
```

### Route discovery without full load

To register stubs, we need to know each plugin's routes without executing it. A lightweight scan of the plugin source:

```python
import re

def _scan_routes(plugin_path):
    """Extract route paths from plugin source without executing it."""
    text = plugin_path.read_text(encoding="utf-8")
    # Match patterns like: ROUTES["/proxy/foo/bar"] = handler
    # and ROUTES = {"/proxy/foo/bar": handler, ...}
    routes = re.findall(r'ROUTES\[(["\'])(/[^"\']+)\1\]', text)
    routes += re.findall(r'["\'](/proxy/[^"\']+)["\']', text)
    return list(set(r[1] if isinstance(r, tuple) else r for r in routes))

def load_plugins_jit():
    """Scan plugins and register stubs instead of loading."""
    jit_config = _read_jit_config()
    always_load = jit_config.get("always_load", ["auth", "admin"])

    for f in PLUGINS.glob("*.py"):
        if f.name.startswith("_"):
            continue
        name = f.stem
        if name in always_load:
            load_plugin(name)  # critical plugins load immediately
        else:
            routes = _scan_routes(f)
            if routes:
                _register_stub(name, routes)
                print(f"  stub: {name} ({routes})")
```

### Idle tracking + unload

```python
_jit_state = {}  # plugin_name → {"loaded_at": float, "last_used": float}

# Patch the plugin dispatch to update last_used
# (in server.py's app() function, after calling _plugins[base_path])
def _update_jit_usage(plugin_name):
    if plugin_name in _jit_state:
        _jit_state[plugin_name]["last_used"] = time.time()
```

CRON handler checks for idle plugins and unloads them:

```python
CRON = 60

async def _jit_idle_check():
    config = _read_jit_config()
    idle_timeout = config.get("idle_timeout", 300)  # 5 min default

    for name, state in list(_jit_state.items()):
        idle = time.time() - state["last_used"]
        if idle > idle_timeout:
            routes = _scan_routes(PLUGINS / f"{name}.py")
            unload_plugin(name)
            _register_stub(name, routes)  # re-register stubs
            del _jit_state[name]
            print(f"  jit-unload: {name} (idle {idle:.0f}s)")

CRON_HANDLER = _jit_idle_check
```

### Config: `config-plugin-jit`

```json
{
  "enabled": true,
  "always_load": ["auth", "admin"],
  "idle_timeout": 300,
  "never_unload": ["auth", "sync"]
}
```

- `always_load`: plugins loaded at startup (not JIT). Auth is always here.
- `idle_timeout`: seconds before an idle plugin gets unloaded and stubbed.
- `never_unload`: plugins that are JIT-loaded but never unloaded once active.

### Interaction with existing plugin system

The JIT system uses the existing `load_plugin()` and `unload_plugin()` functions without modification. It only changes _when_ they are called. The `_plugins` dict, `_plugin_meta` list, and CRON registration all work as before.

Key constraint: `auth.py` must always be in `always_load`. If auth is JIT-loaded, there is a window where unauthenticated requests can reach real handlers.

## Implementation estimate
- ~30 lines modification to server.py (stub registration, JIT load_plugins variant)
- ~15 lines for CRON idle checker
- ~10 lines for route scanning regex
- Dependencies: none (re is stdlib, rest uses existing plugin infrastructure)
- World: `config-plugin-jit`

## Trigger
When running many plugins (8+) but most are rarely used. Saves memory and startup time. Not worth the complexity for 3-4 plugins.

## Related
- `load_plugin()` / `unload_plugin()` in server.py: the core plugin lifecycle
- `_plugins` dict in server.py: route → handler mapping that stubs populate
- `_plugin_meta` list: metadata for /info endpoint
- CRON system: `_cron_tasks`, `CRON` + `CRON_HANDLER` for idle detection
- `load_plugins()` in server.py: the function this replaces/wraps
