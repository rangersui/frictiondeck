"""Sync — P2P world synchronization. High version wins.

POST /proxy/sync/run     → run sync immediately
GET  /proxy/sync/status  → last sync result (from var/log/sync world)

Setup:
  1. Write etc/endpoints world: {"peer": {"url": "http://...", "token": "..."}}
  2. Write etc/sync world: one world name per line (whitelist)
  3. Empty whitelist = nothing syncs. Safe default.

Conflict resolution: high version wins. Last writer wins.
Cron: auto-syncs every 5 minutes.
"""
import datetime, json, urllib.request, urllib.error

DESCRIPTION = "P2P world sync — high version wins, whitelist-only"
SKILL = """\
# Sync — P2P world synchronization

POST /proxy/sync/run   → run sync immediately
GET  /proxy/sync/status → last sync result

Setup:
1. Write peer list to etc/endpoints world (JSON):
   {"peer-name": {"url": "http://...", "token": "auth-token"}}
2. Write whitelist to etc/sync world (one world name per line)
3. Only whitelisted worlds sync. Empty whitelist = nothing syncs.

Conflict: high version wins. Last writer wins.
Cron: auto-syncs every 5 minutes.
Results stored in var/log/sync world (JSON).
View at /home/var/log/sync with usr/lib/renderer/sync installed.
"""
ROUTES = {}
CRON = 300


def _read_config():
    """Read peers from etc/endpoints and whitelist from etc/sync."""
    peers = {}
    try:
        raw = conn("etc/endpoints").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        if raw.strip():
            peers = json.loads(raw)
    except Exception:
        pass
    whitelist = set()
    try:
        raw = conn("etc/sync").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        whitelist = {l.strip() for l in raw.splitlines() if l.strip()}
    except Exception:
        pass
    return peers, whitelist


def _http(url, token=None, data=None, timeout=3, method=None):
    """Zero-dep HTTP. GET if data is None, else method (default POST)."""
    headers = {"Content-Type": "text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = data.encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("GET" if body is None else "POST"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _do_sync():
    """Run one sync cycle across all peers × whitelisted worlds."""
    peers, whitelist = _read_config()
    if not peers or not whitelist:
        return {"last_sync": _now(), "peers": {}, "note": "no peers or empty whitelist"}

    result = {"last_sync": _now(), "peers": {}}

    for peer_name, peer_cfg in peers.items():
        url = peer_cfg.get("url", "").rstrip("/")
        token = peer_cfg.get("token", "")
        if not url:
            continue
        pr = {"status": "ok", "pulled": [], "pushed": [], "skipped": 0}
        try:
            remote_stages = _http(f"{url}/proc/worlds", token)
            remote_versions = {s["name"]: s["version"] for s in remote_stages}

            for world in whitelist:
                local = conn(world)
                local_row = local.execute(
                    "SELECT stage_html, version FROM stage_meta WHERE id=1").fetchone()
                local_v = local_row["version"]
                remote_v = remote_versions.get(world, 0)

                if remote_v > local_v:
                    # Pull: remote is newer
                    remote_data = _http(f"{url}/home/{world}", token)
                    content = remote_data.get("stage_html", "")
                    local.execute(
                        "UPDATE stage_meta SET stage_html=?,version=?,updated_at=datetime('now') WHERE id=1",
                        (content, remote_v))
                    local.commit()
                    log_event(world, "sync_pulled", {"from": peer_name, "version": remote_v})
                    pr["pulled"].append(world)
                elif local_v > remote_v:
                    # Push: local is newer
                    content = local_row["stage_html"]
                    _http(f"{url}/home/{world}", token, data=content, method="PUT")
                    log_event(world, "sync_pushed", {"to": peer_name, "version": local_v})
                    pr["pushed"].append(world)
                else:
                    pr["skipped"] += 1

        except (urllib.error.URLError, OSError, ValueError) as e:
            pr = {"status": "unreachable", "error": str(e)}
        except Exception as e:
            pr = {"status": "error", "error": str(e)}

        result["peers"][peer_name] = pr

    # Write result to var/log/sync
    c = conn("var/log/sync")
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
              (json.dumps(result, ensure_ascii=False),))
    c.commit()
    return result


def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


async def handle_run(method, body, params):
    return _do_sync()


async def handle_status(method, body, params):
    try:
        raw = conn("var/log/sync").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        if raw.strip():
            return json.loads(raw)
    except Exception:
        pass
    return {"last_sync": None, "peers": {}}


async def _tick():
    _do_sync()


CRON_HANDLER = _tick
ROUTES["/proxy/sync/run"] = handle_run
ROUTES["/proxy/sync/status"] = handle_status
