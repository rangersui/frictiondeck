"""Health plugin — system task manager with heartbeat.

CRON = 60. Every minute, collects system state and writes to /var/log/health world.
Any AI can GET /var/log/health to know the empire's health.

"""

import json, os, time, shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

DESCRIPTION = "System health monitor — heartbeat every 60s"
NEEDS = ["_plugin_meta", "_cron_tasks", "_start_time"]
ROUTES = {}
CRON = 60
NEEDS = ["conn", "_call", "log_event", "_plugin_meta", "_cron_tasks", "_start_time"]

_OLLAMA_URL = os.getenv("CONSULT_URL", os.getenv("OLLAMA_URL", "http://localhost:11434"))

# ── collectors ─────────────────────────────────────────────────────────

def _check_ollama():
    """Check if ollama is online and list models."""
    try:
        r = urlopen(f"{_OLLAMA_URL}/api/tags", timeout=5)
        data = json.loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        return {"status": "online", "models": models}
    except (URLError, Exception):
        return {"status": "offline", "models": []}


def _get_worlds():
    """Count worlds and total size."""
    data_dir = _ROOT / "data"
    if not data_dir.exists():
        return {"count": 0, "total_size_kb": 0}
    worlds = [d for d in data_dir.iterdir() if d.is_dir() and (d / "universe.db").exists()]
    total = sum((d / "universe.db").stat().st_size for d in worlds if (d / "universe.db").exists())
    return {"count": len(worlds), "total_size_kb": round(total / 1024, 1)}


def _get_plugins():
    """List loaded plugins."""
    names = [m["name"] for m in _plugin_meta]
    return {"loaded": names, "count": len(names)}


def _get_cron():
    """List cron tasks with status."""
    result = {}
    for name, task in _cron_tasks.items():
        result[name] = {
            "interval": task["interval"],
            "last_run": datetime.fromtimestamp(task["last_run"], tz=timezone.utc).isoformat()
        }
    return result


def _get_memory():
    """Get process memory in MB."""
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return round(usage.ru_maxrss / 1024, 1)  # Linux: KB → MB
    except ImportError:
        pass
    # Windows / fallback
    try:
        pid = os.getpid()
        # /proc on Linux containers
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)  # KB → MB
    except Exception:
        pass
    return -1


def _get_disk():
    """Get free disk space in GB."""
    try:
        usage = shutil.disk_usage(_ROOT)
        return round(usage.free / (1024 ** 3), 1)
    except Exception:
        return -1


# ── cron handler ───────────────────────────────────────────────────────

async def _heartbeat():
    """Collect all health data and write to var/log/health world."""
    # Cache stats via service binding
    cache_stats = {"entries": 0, "hits": 0, "misses": 0}
    try:
        cs = await _call("/proxy/cache/stats")
        if "entries" in cs:
            cache_stats = {"entries": cs["entries"], "hits": cs["hits"], "misses": cs["misses"]}
    except Exception:
        pass

    health = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "ollama": _check_ollama(),
        "worlds": _get_worlds(),
        "plugins": _get_plugins(),
        "cron_tasks": _get_cron(),
        "cache": cache_stats,
        "uptime_seconds": round(time.time() - _start_time),
        "memory_mb": _get_memory(),
        "disk_free_gb": _get_disk(),
    }

    # Write JSON to var/log/health world (data only, renderer paints)
    try:
        c = conn("var/log/health")
        payload = "<!--use:usr/lib/renderer/health-->\n" + json.dumps(health)
        c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
                  (payload,))
        c.commit()
        # Heartbeat in HMAC chain — gaps = downtime
        log_event("var/log/health", "heartbeat", {"uptime": health["uptime_seconds"]})
    except Exception as e:
        print(f"  health: write failed: {e}")

CRON_HANDLER = _heartbeat


# ── manual route ───────────────────────────────────────────────────────

async def handle_health(method, body, params):
    """GET /var/log/health/status — trigger immediate health check and return."""
    await _heartbeat()
    c = conn("var/log/health")
    row = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    if not row or not row["stage_html"]:
        return {"error": "no data yet"}
    raw = row["stage_html"]
    # Strip renderer declaration prefix
    if raw.startswith("<!--"):
        raw = raw.split("\n", 1)[-1]
    return json.loads(raw)

ROUTES["/var/log/health/status"] = handle_health

PARAMS_SCHEMA = {
    "/var/log/health/status": {
        "method": "GET",
        "params": {},
        "returns": {"timestamp": "string", "ollama": "object", "worlds": "object",
                    "plugins": "object", "cache": "object", "uptime_seconds": "int",
                    "memory_mb": "float", "disk_free_gb": "float"}
    },
}
