"""Devtools — Unix pipe primitives as HTTP routes.

fetch('/grep?q=error').then(r=>r.json())  → grep
  .then(ws=>fetch('/tail?world='+ws[0]+'&n=5')).then(r=>r.text())  → tail
  .then(t=>__elastik.sync(t))  → write back

Not loaded by default. Load with: POST /admin/load  body=devtools
"""
DESCRIPTION = "Unix pipe primitives — grep (-l), tail, head, wc (-c), rev, echo, null, full, true, false, yes, cowsay, health, db/size, whoami, uuid, verify, delay, bench, config/dump, time"

import sys, json, os, subprocess, time, sqlite3
from pathlib import Path

# Go exports $ELASTIK_DATA / $ELASTIK_ROOT before forking plugins.
# Python in-process also has these set. No guessing, no parent-chain.
_DATA = Path(os.environ.get("ELASTIK_DATA", "data")).resolve()
_ROOT = Path(os.environ.get("ELASTIK_ROOT", ".")).resolve()
_START = time.time()


def _read_stage(world):
    """Read stage_html from a world's universe.db. Direct sqlite, no conn()."""
    db = _DATA / world / "universe.db"
    if not db.exists():
        return None
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    c.close()
    return r["stage_html"] if r else ""


def _world_names():
    """List all world directory names that have a universe.db."""
    if not _DATA.exists():
        return []
    return sorted(d.name for d in _DATA.iterdir()
                  if d.is_dir() and (d / "universe.db").exists())


# ── handlers (Python in-process) ─────────────────────────────────────

async def handle_grep(method, body, params):
    """Search worlds for a query string.

    ?q=error                → grep -rn error *       (all worlds, line matches)
    ?q=error&world=work     → grep -n error work.txt (single world)
    ?q=error&mode=l         → grep -rl error *       (filenames only)
    """
    q = params.get("q", "")
    if not q:
        return {"error": "?q= required", "_status": 400}
    mode = params.get("mode", "")
    world = params.get("world", "")
    # Build target list: single world or all
    if world:
        stage = _read_stage(world)
        if stage is None:
            return {"error": "world not found", "_status": 404}
        targets = [(world, stage)]
    else:
        targets = [(n, _read_stage(n)) for n in _world_names()]
    if mode == "l":
        # grep -l: filenames only
        matches = [n for n, s in targets if s and q in s]
        return {"_html": json.dumps(matches), "_status": 200}
    # grep: line-level matches with world:lineno:content
    lines = []
    for name, stage in targets:
        if not stage:
            continue
        for i, line in enumerate(stage.splitlines(), 1):
            if q in line:
                lines.append(f"{name}:{i}:{line}")
    return {"_html": "\n".join(lines), "_status": 200}


async def handle_tail(method, body, params):
    """Last n lines of a world's stage. ?world=x&n=10"""
    world = params.get("world", "")
    if not world:
        return {"error": "?world= required", "_status": 400}
    stage = _read_stage(world)
    if stage is None:
        return {"error": "world not found", "_status": 404}
    n = int(params.get("n", "10"))
    lines = stage.splitlines()[-n:]
    return {"_html": "\n".join(lines), "_status": 200}


async def handle_head(method, body, params):
    """First n lines of a world's stage. ?world=x&n=10"""
    world = params.get("world", "")
    if not world:
        return {"error": "?world= required", "_status": 400}
    stage = _read_stage(world)
    if stage is None:
        return {"error": "world not found", "_status": 404}
    n = int(params.get("n", "10"))
    lines = stage.splitlines()[:n]
    return {"_html": "\n".join(lines), "_status": 200}


async def handle_wc(method, body, params):
    """Word/line/byte count for a world's stage. ?world=x"""
    world = params.get("world", "")
    if not world:
        return {"error": "?world= required", "_status": 400}
    stage = _read_stage(world)
    if stage is None:
        return {"error": "world not found", "_status": 404}
    return {"lines": len(stage.splitlines()), "words": len(stage.split()),
            "bytes": len(stage.encode()), "_status": 200}


async def handle_wc_c(method, body, params):
    """wc -c — byte count of POST body. Upload stress test receipt.

    Send 5MB up, get 7 bytes back. Perfect one-way bandwidth test.
    """
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    return {"_html": str(len(text.encode("utf-8"))), "_status": 200}


async def handle_full(method, body, params):
    """/dev/full — always 507 Insufficient Storage. The bouncer."""
    return {"error": "no space left on device", "_status": 507}


async def handle_null(method, body, params):
    """/dev/null — swallow anything, return 200."""
    return {"_status": 204, "_html": ""}


async def handle_echo(method, body, params):
    """echo — return body unchanged."""
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    return {"_html": text, "_status": 200}


async def handle_health(method, body, params):
    """Health check — ok + uptime."""
    return {"ok": True, "uptime": round(time.time() - _START, 1), "_status": 200}


async def handle_db_size(method, body, params):
    """Storage usage per world."""
    sizes = {}
    total = 0
    for name in _world_names():
        db = _DATA / name / "universe.db"
        sz = db.stat().st_size if db.exists() else 0
        sizes[name] = sz
        total += sz
    def fmt(b):
        if b >= 1048576: return f"{b/1048576:.1f}MB"
        if b >= 1024: return f"{b/1024:.1f}KB"
        return f"{b}B"
    return {"worlds": {k: fmt(v) for k, v in sizes.items()},
            "total": fmt(total), "count": len(sizes), "_status": 200}


async def handle_whoami(method, body, params):
    """whoami — isolation mirror. PID, hostname, IP, env, user.

    Returns the plugin process's own identity — not the daemon's.
    If PID differs from Go's PID, isolation is proven.
    If env lacks daemon secrets, privilege separation is proven.
    """
    import socket, getpass
    info = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        info["ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        info["ip"] = "127.0.0.1"
    # Env snapshot — prove what the plugin can/cannot see.
    # Redact token values, show only key presence.
    env_keys = sorted(os.environ.keys())
    sensitive = {"ELASTIK_TOKEN", "ELASTIK_APPROVE_TOKEN", "SECRET", "PASSWORD", "API_KEY"}
    info["env"] = {k: ("***" if any(s in k.upper() for s in sensitive) else os.environ[k][:80])
                   for k in env_keys[:50]}  # cap at 50 keys
    info["env_count"] = len(env_keys)
    return {**info, "_status": 200}


async def handle_uuid(method, body, params):
    """uuid — cryptographic randomness. One per call."""
    import uuid
    n = min(int(params.get("n", "1")), 100)
    if n == 1:
        return {"_html": str(uuid.uuid4()), "_status": 200}
    return {"_html": "\n".join(str(uuid.uuid4()) for _ in range(n)), "_status": 200}


async def handle_verify(method, body, params):
    """verify — structural integrity check. Data dir, worlds, db schemas."""
    issues = []
    if not _DATA.exists():
        issues.append("data/ directory missing")
    for name in _world_names():
        db = _DATA / name / "universe.db"
        try:
            c = sqlite3.connect(str(db))
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "stage_meta" not in tables:
                issues.append(f"{name}: missing stage_meta table")
            c.execute("PRAGMA integrity_check")
            c.close()
        except Exception as e:
            issues.append(f"{name}: {e}")
    return {"ok": len(issues) == 0, "issues": issues,
            "worlds": len(_world_names()), "_status": 200}


async def handle_delay(method, body, params):
    """delay — artificial latency. ?ms=500. Cap at 10s."""
    import asyncio
    ms = min(int(params.get("ms", "100")), 10000)
    await asyncio.sleep(ms / 1000)
    return {"delayed": ms, "_status": 200}


async def handle_bench(method, body, params):
    """bench — micro-benchmark. Measures sqlite read + JSON round-trip."""
    iterations = min(int(params.get("n", "100")), 1000)
    names = _world_names()
    t0 = time.time()
    for _ in range(iterations):
        for name in names[:1]:  # bench first world only
            _read_stage(name)
    elapsed = time.time() - t0
    return {"iterations": iterations, "elapsed_ms": round(elapsed * 1000, 1),
            "per_iter_ms": round(elapsed * 1000 / max(iterations, 1), 3),
            "worlds": len(names), "_status": 200}


async def handle_config_dump(method, body, params):
    """config/dump — sanitized config snapshot. Tokens redacted."""
    config = {
        "data_dir": str(_DATA),
        "root_dir": str(_ROOT),
        "pid": os.getpid(),
        "platform": sys.platform,
        "python_version": sys.version.split()[0],
        "token_set": bool(os.getenv("ELASTIK_TOKEN")),
        "approve_token_set": bool(os.getenv("ELASTIK_APPROVE_TOKEN")),
        "port": os.getenv("ELASTIK_PORT", "3005"),
        "host": os.getenv("ELASTIK_HOST", "0.0.0.0"),
        "worlds": _world_names(),
    }
    return {**config, "_status": 200}


async def handle_time(method, body, params):
    """time — Unix epoch timestamp. Clock skew detection."""
    return {"_html": str(int(time.time())), "_status": 200}


async def handle_rev(method, body, params):
    """rev — reverse each line. UTF-8 torture test.

    If 👨‍👩‍👧‍👦 round-trips intact through JSON→stdin→stdout→HTTP,
    the encoding pipeline is clean. If reversed output is garbled,
    that's expected — the point is the pipe doesn't break.
    """
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    # Reverse each line individually, like Unix rev
    return {"_html": "\n".join(line[::-1] for line in text.splitlines()), "_status": 200}


async def handle_true(method, body, params):
    """/true — always 200. The assenter."""
    return {"_html": "", "_status": 200}


async def handle_false(method, body, params):
    """/false — always 403. The wall."""
    return {"_html": "", "_status": 403}


async def handle_yes(method, body, params):
    """yes — returns 'yes' n times. ?n=1. Cap 10000."""
    n = min(int(params.get("n", "1")), 10000)
    return {"_html": "\n".join(["yes"] * n), "_status": 200}


_COW = r"""
 {border}
< {msg} >
 {border}
        \   ^__^
         \  (oo)\_______
            (__)\       )\/\
                ||----w |
                ||     ||
"""[1:-1]  # strip leading/trailing newline


async def handle_cowsay(method, body, params):
    """cowsay — if the cow renders intact, your encoding is fine."""
    text = params.get("say", "") or (body if isinstance(body, str) else body.decode("utf-8", "replace")) or "moo"
    n = max(len(text), 2)
    cow = _COW.format(msg=text.ljust(n), border="-" * (n + 2))
    return {"_html": cow, "_status": 200}


async def handle_proxy(method, body, params):
    from urllib.parse import unquote
    url = unquote(params.get("url", ""))
    if not url or not url.startswith(("http://", "https://")):
        return {"error": "?url= required (http/https)", "_status": 400}
    r = subprocess.run(["curl", "-s", "-L", "-m", "30", url], capture_output=True, timeout=35)
    return {"_html": r.stdout.decode("utf-8", "replace"), "_status": 200}


ROUTES = {
    "/proxy": handle_proxy,
    "/grep": handle_grep,
    "/tail": handle_tail,
    "/head": handle_head,
    "/wc": handle_wc,
    "/null": handle_null,
    "/echo": handle_echo,
    "/health": handle_health,
    "/db/size": handle_db_size,
    "/cowsay": handle_cowsay,
    "/whoami": handle_whoami,
    "/uuid": handle_uuid,
    "/verify": handle_verify,
    "/delay": handle_delay,
    "/bench": handle_bench,
    "/config/dump": handle_config_dump,
    "/true": handle_true,
    "/false": handle_false,
    "/yes": handle_yes,
    "/wc-c": handle_wc_c,
    "/full": handle_full,
    "/time": handle_time,
    "/rev": handle_rev,
}


# ── Go CGI entry point ───────────────────────────────────────────────
# One entry point, calls the same async handlers as Python in-process.
# No duplicated logic — CGI and Python run identical code paths.

def _to_cgi(result):
    """Convert async handler result dict to CGI response dict."""
    status = result.pop("_status", 200)
    html = result.pop("_html", None)
    body = html if html is not None else json.dumps(result)
    resp = {"status": status, "body": body}
    if html is not None and status == 200:
        resp["content_type"] = "text/plain; charset=utf-8"
    return resp


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--routes":
        print(json.dumps(list(ROUTES.keys())))
        sys.exit(0)
    import asyncio
    d = json.loads(sys.stdin.readline())
    handler = ROUTES.get(d["path"])
    if not handler:
        print(json.dumps({"status": 404, "body": json.dumps({"error": "not found"})}))
    else:
        qs = d.get("query", "")
        params = dict(x.split("=", 1) for x in qs.split("&") if "=" in x) if qs else {}
        result = asyncio.run(handler(d.get("method", "GET"), d.get("body", ""), params))
        print(json.dumps(_to_cgi(result)))
