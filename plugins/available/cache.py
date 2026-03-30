"""Cache plugin — in-memory cache with version-based invalidation.

Install: lucy install cache
Cache key = hash(question + world versions). World changes = cache miss.
Restart clears cache — that's a feature, not a bug.

Other plugins can call directly via service binding:
  result = await _call("/proxy/cache/get", body=json.dumps({...}).encode())
"""

import hashlib, json

DESCRIPTION = "In-memory cache with world-version invalidation"
ROUTES = {}

_cache = {}  # key → {"value": ..., "versions": {world: version}}
_stats = {"hits": 0, "misses": 0}

PARAMS_SCHEMA = {
    "/proxy/cache/get": {
        "method": "POST",
        "params": {
            "key": {"type": "string", "required": True, "description": "Cache key (question or identifier)"},
            "worlds": {"type": "array", "required": False, "description": "Worlds to check versions against"},
        },
        "returns": {"hit": "bool", "value": "string|null"}
    },
    "/proxy/cache/set": {
        "method": "POST",
        "params": {
            "key": {"type": "string", "required": True, "description": "Cache key"},
            "value": {"type": "string", "required": True, "description": "Value to cache"},
            "worlds": {"type": "array", "required": False, "description": "Worlds whose versions to track"},
        },
        "returns": {"ok": "bool", "cache_key": "string"}
    },
    "/proxy/cache/clear": {
        "method": "POST",
        "params": {},
        "returns": {"ok": "bool", "cleared": "int"}
    },
}


def _world_versions(worlds):
    """Get current versions for a list of worlds."""
    versions = {}
    for w in (worlds or []):
        try:
            r = conn(w).execute("SELECT version FROM stage_meta WHERE id=1").fetchone()
            versions[w] = r["version"] if r else 0
        except Exception:
            versions[w] = 0
    return versions


def _make_key(key, versions):
    """Hash key + world versions into a cache key."""
    raw = key + "|" + json.dumps(versions, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def handle_get(method, body, params):
    b = json.loads(body) if body else {}
    key = params.get("key") or b.get("key", "")
    if not key:
        return {"error": "key required"}
    worlds = b.get("worlds", [])
    versions = _world_versions(worlds)
    cache_key = _make_key(key, versions)
    entry = _cache.get(cache_key)
    if entry:
        _stats["hits"] += 1
        return {"hit": True, "value": entry["value"], "cache_key": cache_key}
    _stats["misses"] += 1
    return {"hit": False, "value": None, "cache_key": cache_key}


async def handle_set(method, body, params):
    b = json.loads(body) if body else {}
    key = params.get("key") or b.get("key", "")
    value = b.get("value", "")
    if not key:
        return {"error": "key required"}
    worlds = b.get("worlds", [])
    versions = _world_versions(worlds)
    cache_key = _make_key(key, versions)
    _cache[cache_key] = {"value": value, "versions": versions}
    return {"ok": True, "cache_key": cache_key}


async def handle_clear(method, body, params):
    n = len(_cache)
    _cache.clear()
    return {"ok": True, "cleared": n}


async def handle_stats(method, body, params):
    total = _stats["hits"] + _stats["misses"]
    rate = round(_stats["hits"] / total * 100, 1) if total else 0
    return {"hits": _stats["hits"], "misses": _stats["misses"], "total": total,
            "rate": f"{rate}%", "entries": len(_cache)}

ROUTES["/proxy/cache/get"] = handle_get
ROUTES["/proxy/cache/set"] = handle_set
ROUTES["/proxy/cache/clear"] = handle_clear
ROUTES["/proxy/cache/stats"] = handle_stats
