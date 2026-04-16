"""Devtools — Unix pipe primitives as HTTP routes.

fetch('/grep?q=error').then(r=>r.json())  → grep
  .then(ws=>fetch('/tail?world='+ws[0]+'&n=5')).then(r=>r.text())  → tail
  .then(t=>__elastik.sync(t))  → write back

Not loaded by default. Load with: POST /admin/load  body=devtools
"""
DESCRIPTION = "Unix pipe primitives + cave primitives (stone/fire+ash/wall/drum/trail/hunt/tomb/bones/river/soil/knot/shadow/amber/eclipse/narcissus) — grep (-l), tail, head, wc (-c), rev, echo, null, full, true, false, yes, cowsay, moaisay, stone, fire, ash, wall, drum, trail, hunt, tomb, bones, river, soil, knot, shadow, amber, eclipse, narcissus, health, db/size, whoami, uuid, verify, delay, bench, config/dump, time"

import sys, json, os, subprocess, time, sqlite3, hashlib, asyncio, random
from pathlib import Path

# Go exports $ELASTIK_DATA / $ELASTIK_ROOT before forking plugins.
# Python in-process also has these set. No guessing, no parent-chain.
_DATA = Path(os.environ.get("ELASTIK_DATA", "data")).resolve()
_ROOT = Path(os.environ.get("ELASTIK_ROOT", ".")).resolve()
_START = time.time()


def _disk_name(name):
    return name.replace("/", "%2F")

def _read_stage(world):
    """Read stage_html from a world's universe.db. Direct sqlite, no conn()."""
    db = _DATA / _disk_name(world) / "universe.db"
    if not db.exists():
        return None
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    c.close()
    val = r["stage_html"] if r else ""
    if isinstance(val, bytes):
        try: val = val.decode("utf-8")
        except UnicodeDecodeError: val = ""
    return val


def _world_names():
    """List all world directory names that have a universe.db."""
    if not _DATA.exists():
        return []
    return sorted(d.name.replace("%2F", "/") for d in _DATA.iterdir()
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


_MOAI = """
  ┌{border}┐
  │ {msg} │
  └{border}┘
       \\
        \\
         🗿
        ╱  ╲
       ╱    ╲
      │ ●  ● │
      │  ──  │
      │      │
      ╰──────╯
"""[1:-1]


async def handle_moaisay(method, body, params):
    """moaisay — because cowsay is too cheerful."""
    text = params.get("say", "") or (body if isinstance(body, str) else body.decode("utf-8", "replace")) or "🗿"
    n = max(len(text), 2)
    moai = _MOAI.format(msg=text.ljust(n), border="─" * (n + 2))
    return {"_html": moai, "_status": 200}

_STONE_LOG = _DATA / "dev_stone.log"


async def handle_stone_dev(method, body, params):
    """/dev/stone — receives, remembers, never replies.
    Not /dev/null. Input is preserved, but there is no read path.
    Petrification, not discard. The stone's signature is the full sha256.
    """
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    if not text:
        text = params.get("say", "") or ""
    if text:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()  # full 256 bits. ritual, not compression.
        with open(_STONE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": int(time.time()),
                "sha256": digest,
                "bytes": len(text.encode("utf-8")),
            }, ensure_ascii=False) + "\n")
    return {"_status": 204, "_html": ""}


async def handle_stone_view(method, body, params):
    """/stone — the face, plus the weathering.
    The stone tells you IT HAS BEEN FED. It will not tell you what.
    """
    count = total_bytes = 0
    first_ts = last_ts = None
    if _STONE_LOG.exists():
        with open(_STONE_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    count += 1
                    total_bytes += e.get("bytes", 0)
                    ts = e.get("ts", 0)
                    if first_ts is None or ts < first_ts: first_ts = ts
                    if last_ts is None or ts > last_ts: last_ts = ts
                except (ValueError, TypeError):
                    pass
    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        secs = last_ts - first_ts
        if   secs < 60:    span = f"{secs}s"
        elif secs < 3600:  span = f"{secs//60}m {secs%60}s"
        elif secs < 86400: span = f"{secs//3600}h {(secs%3600)//60}m"
        else:              span = f"{secs//86400}d {(secs%86400)//3600}h"
    else:
        span = "—"
    return {"_html": f"🗿\n\n{count} writes · {total_bytes} bytes · span {span}\n",
            "_status": 200}


async def handle_stone_weight(method, body, params):
    """/stone/weight — how heavy the stone has become. Bytes on disk.
    Includes all witness framing (timestamps, hashes, JSON). Not just
    input content — the stone accumulates bookkeeping too.
    """
    total = _STONE_LOG.stat().st_size if _STONE_LOG.exists() else 0
    return {"_html": f"{total}\n", "_status": 200}


# ── cave primitives ──────────────────────────────────────────────
# Five ways information relates to the human, invented in caves:
#   stone — receive, remember, silent       (private memory)
#   fire  — receive, consume, warmth        (forgetting)
#   wall  — receive, display, permanent     (public memory)
#   drum  — receive, broadcast, forget      (the present moment)
#   trail — receive, accumulate, one-way    (history)

_ASH_LOG = _DATA / "dev_ash.log"

async def handle_fire(method, body, params):
    """/dev/fire — burns on contact. Content consumed. Hash → /ash.
    Fire and ash are one event seen from two sides: the warmth that
    leaves, and the residue that doesn't quite. You cannot get the
    content back from /ash — only proof that something once was.
    """
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    if not text:
        text = params.get("say", "") or ""
    if text:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with open(_ASH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": int(time.time()),
                "sha256": digest,
                "bytes": len(text.encode("utf-8")),
            }, ensure_ascii=False) + "\n")
    return {"_html": "🔥\n", "_status": 200}


async def handle_ash(method, body, params):
    """/ash — cryptographic ghosts. What /dev/fire left behind.
    You see proof that thoughts existed here. You cannot reach them.
    Absolute evidence. Absolute silence.
    """
    if not _ASH_LOG.exists():
        return {"_html": "(no ash — fire has not yet burned)\n", "_status": 200}
    lines = []
    with open(_ASH_LOG, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
                lines.append(f"{ts}  {e['sha256']}  ({e['bytes']}B)")
            except (ValueError, TypeError, KeyError):
                pass
    return {"_html": ("\n".join(lines) + "\n") if lines else "(cold hearth)\n",
            "_status": 200}


_WALL_LOG = _DATA / "dev_wall.log"

async def handle_wall(method, body, params):
    """/wall — cave painting. Append-only public record.
    Unlike stone (private hash), wall keeps the actual marks.
    POST to add, GET to read all (oldest first).
    """
    if method == "POST":
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        if not text: text = params.get("say", "")
        if text:
            with open(_WALL_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "text": text}, ensure_ascii=False) + "\n")
        return {"_status": 201, "_html": ""}
    # GET: all marks, oldest first
    out = []
    if _WALL_LOG.exists():
        with open(_WALL_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["ts"]))
                    out.append(f"{ts}  {e['text']}")
                except (ValueError, TypeError, KeyError): pass
    return {"_html": "\n".join(out) + "\n" if out else "(blank wall)\n", "_status": 200}


_DRUM_LISTENERS = []  # in-memory list of asyncio.Queue

async def handle_drum(method, body, params):
    """/drum — beat once, heard by all CURRENT listeners. No replay.
    POST = beat. GET = listen (SSE stream of beats).
    If you weren't listening when the drum was beaten, you missed it.
    This is the original push notification.
    """
    if method == "POST":
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        if not text: text = params.get("say", "") or "beat"
        msg = f"event: beat\ndata: {text}\n\n".encode("utf-8")
        for q in list(_DRUM_LISTENERS):
            try: q.put_nowait(msg)
            except Exception: pass
        return {"_status": 204, "_html": ""}
    # GET: SSE listen
    send = params.get("_send")
    if not send:
        return {"error": "streaming unavailable (need server with _send)", "_status": 500}
    await send({"type": "http.response.start", "status": 200, "headers": [
        [b"content-type", b"text/event-stream; charset=utf-8"],
        [b"cache-control", b"no-cache"],
    ]})
    q = asyncio.Queue()
    _DRUM_LISTENERS.append(q)
    try:
        while True:
            try:
                m = await asyncio.wait_for(q.get(), timeout=3.0)
                await send({"type": "http.response.body", "body": m, "more_body": True})
            except asyncio.TimeoutError:
                # heartbeat so proxies don't close an idle drum circle
                await send({"type": "http.response.body", "body": b": hb\n\n", "more_body": True})
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        try: _DRUM_LISTENERS.remove(q)
        except ValueError: pass
    return None


_TRAIL_LOG = _DATA / "dev_trail.log"

async def handle_trail(method, body, params):
    """/trail — breadcrumbs. Each POST adds a step. GET shows the path.
    No rewind, no delete. You can see where you've been — not go back.
    """
    if method == "POST":
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        if not text: text = params.get("say", "")
        if text:
            with open(_TRAIL_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "step": text}, ensure_ascii=False) + "\n")
        return {"_status": 201, "_html": ""}
    # GET: numbered path from start
    steps = []
    if _TRAIL_LOG.exists():
        with open(_TRAIL_LOG, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                try:
                    e = json.loads(line)
                    steps.append(f"{i:>4}. {e['step']}")
                except (ValueError, TypeError, KeyError): pass
    return {"_html": "\n".join(steps) + "\n" if steps else "(no trail yet)\n", "_status": 200}


_HUNT_SKIP_PREFIXES = ("renderer-", "skills-", "etc/", "sys-", "plugin-", ".")

async def handle_hunt(method, body, params):
    """/hunt — random encounter. You don't browse. You stumble in.
    302 redirect to a random world. System worlds (renderer-*, skills-*,
    etc/*, sys-*, plugin-*) are excluded — this is about wildlife,
    not organs.
    """
    names = [n for n in _world_names()
             if not any(n.startswith(p) for p in _HUNT_SKIP_PREFIXES)]
    if not names:
        return {"_html": "🗿\n", "_status": 404}
    return {"_redirect": f"/{random.choice(names)}", "_status": 302}


async def handle_tomb(method, body, params):
    """/tomb?world=NAME — burial, not deletion.
    The body is moved to .trash/. An epitaph.json is placed on top
    (timestamp, last version, last HMAC, bytes at death). On Unix,
    chmod 000 seals the grave. Requires approve-level auth.

    This is not DELETE. DELETE (core route) is for routine removal.
    /tomb is for deliberate burial — rare, ceremonial, with a tombstone.
    """
    import shutil, server
    scope = params.get("_scope", {})
    if server._check_auth(scope) != "approve":
        return {"error": "tomb requires approve-level auth", "_status": 403}
    name = params.get("world", "")
    if not name:
        return {"error": "?world=name required", "_status": 400}
    if not server._valid_name(name):
        return {"error": "invalid world name", "_status": 400}
    src = _DATA / _disk_name(name)
    if not src.exists():
        return {"error": "world not found", "_status": 404}

    # Read last rites from the dying world.
    version = 0
    hmac_val = ""
    byte_count = 0
    db = src / "universe.db"
    if db.exists():
        byte_count = db.stat().st_size
        # Close cached connection (server._db) — Windows won't rename an open file.
        if name in server._db:
            try: server._db.pop(name).close()
            except Exception: pass
        try:
            c = sqlite3.connect(str(db))
            r = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()
            if r: version = r[0] or 0
            r2 = c.execute("SELECT hmac FROM events ORDER BY id DESC LIMIT 1").fetchone()
            if r2: hmac_val = r2[0] or ""
            c.close()
        except Exception:
            pass

    # Move to .trash (reuse existing soft-delete infra).
    trash = _DATA / ".trash" / _disk_name(name)
    trash.parent.mkdir(parents=True, exist_ok=True)
    if trash.exists(): shutil.rmtree(trash)
    src.rename(trash)

    # Place the tombstone on top.
    epitaph = {
        "name": name,
        "buried_at": int(time.time()),
        "last_version": version,
        "last_hmac": hmac_val[:32],
        "bytes_at_death": byte_count,
    }
    with open(trash / "epitaph.json", "w", encoding="utf-8") as f:
        json.dump(epitaph, f, ensure_ascii=False, indent=2)

    # Unix: seal the tomb. Windows: the tomb is a concept, not a permission.
    if sys.platform != "win32":
        try: os.chmod(trash, 0o000)
        except OSError: pass

    return {"_body": json.dumps(epitaph, ensure_ascii=False, indent=2),
            "_ct": "application/json", "_status": 200}


_BONES_SIGNS = ["great blessing", "blessing", "small blessing", "future blessing",
                "curse", "small curse", "future curse", "great curse"]

async def handle_bones(method, body, params):
    """/bones — SHA-256 divination. Throw the oracle bone into the fire.
    POST your question. Receive an omen based on the hash.
    Pure physical entropy — no LLM, no training, no bias. Just the bone.
    """
    q = body if isinstance(body, str) else body.decode("utf-8", "replace")
    if not q: q = params.get("q", "") or "..."
    h = hashlib.sha256(q.encode("utf-8")).hexdigest()
    sign = _BONES_SIGNS[int(h[-1], 16) % len(_BONES_SIGNS)]
    hexagram = "".join("⚊" if int(c, 16) % 2 == 0 else "⚋" for c in h[:6])
    return {"_html": f"{sign}\n{hexagram}\n\nQ:    {q}\nbone: {h[-8:]}\n",
            "_status": 200}


_SOIL_DIR = _DATA / "soil"

async def handle_soil(method, body, params):
    """/soil — digital decomposition. Data learns to age.

    POST:    bury text. Returns soil_id (first 8 of a uuid4).
    GET:     ?id=XXX — exhume. One byte decays per hour since burial.
             no id  — list all graves with age + remaining bytes.

    Silicon's cruelest feature: no aging. Files last forever if the disk
    lasts forever. /soil forces a biological clock onto digital matter.
    Bury a beautiful paragraph. Come back a year later. Read the bones.

    Decay is deterministic per grave — same hour, same holes. Rehumation
    always shows the same damage. Once hours >= len(text), the file is
    unlinked: fully decomposed, nothing left even on disk.
    """
    import uuid
    _SOIL_DIR.mkdir(parents=True, exist_ok=True)

    if method == "POST":
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        if not text:
            return {"error": "nothing to bury", "_status": 400}
        soil_id = str(uuid.uuid4())[:8]
        with open(_SOIL_DIR / f"{soil_id}.json", "w", encoding="utf-8") as f:
            json.dump({"buried": int(time.time()), "original": text}, f, ensure_ascii=False)
        return {"_html": f"buried. id={soil_id}\n", "_status": 200}

    # GET
    soil_id = params.get("id", "")
    if not soil_id:
        graves = []
        for p in sorted(_SOIL_DIR.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                age_h = (int(time.time()) - d["buried"]) // 3600
                total = len(d["original"])
                remain = max(0, total - age_h)
                graves.append(f"{p.stem}  age={age_h}h  alive={remain}/{total}")
            except (ValueError, KeyError, TypeError):
                pass
        return {"_html": ("\n".join(graves) + "\n") if graves else "empty soil\n",
                "_status": 200}

    path = _SOIL_DIR / f"{soil_id}.json"
    if not path.exists():
        return {"_html": "nothing here. fully decomposed, or never buried.\n",
                "_status": 404}

    d = json.loads(path.read_text(encoding="utf-8"))
    original = d["original"]
    hours = (int(time.time()) - d["buried"]) // 3600

    if hours >= len(original):
        path.unlink()
        return {"_html": "· · · · ·\n\nfully decomposed. file deleted.\n\n· · · · ·\n",
                "_status": 200}

    # Deterministic decay: the worms always bite in the same order for this grave.
    rng = random.Random(d["buried"])
    chars = list(original)
    indices = list(range(len(chars)))
    rng.shuffle(indices)
    for i in range(min(hours, len(indices))):
        chars[indices[i]] = "·"

    decayed = "".join(chars)
    remain = sum(1 for c in chars if c != "·")
    return {"_html": f"{decayed}\n\n— {hours}h elapsed · {remain}/{len(original)} bytes alive\n",
            "_status": 200}


async def handle_river(method, body, params):
    """/dev/river — global event stream. All writes, all worlds, one flow.
    SSE. Pushes pointers (world + version + ts), not content.

    "You cannot step into the same river twice."
    There is no storage. Events that fire while you weren't listening
    are gone. The river flows whether you watch it or not.
    """
    send = params.get("_send")
    if not send:
        return {"error": "streaming unavailable (need server with _send)", "_status": 500}
    await send({"type": "http.response.start", "status": 200, "headers": [
        [b"content-type", b"text/event-stream; charset=utf-8"],
        [b"cache-control", b"no-cache"],
    ]})

    def snapshot():
        out = {}
        for n in _world_names():
            db = _DATA / _disk_name(n) / "universe.db"
            if not db.exists(): continue
            try:
                c = sqlite3.connect(str(db))
                r = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()
                out[n] = r[0] if r else 0
                c.close()
            except Exception:
                pass
        return out

    versions = snapshot()
    ticks = 0
    try:
        while True:
            await asyncio.sleep(0.5)
            current = snapshot()
            any_flow = False
            for name, v in current.items():
                old = versions.get(name, -1)  # -1 → brand new world
                if v != old:
                    versions[name] = v
                    evt = json.dumps({"world": name, "version": v, "ts": int(time.time())},
                                     ensure_ascii=False)
                    msg = f"event: flow\ndata: {evt}\n\n".encode("utf-8")
                    await send({"type": "http.response.body", "body": msg, "more_body": True})
                    any_flow = True
            if any_flow: ticks = 0
            else:
                ticks += 1
                if ticks >= 10:  # 5s of stillness → heartbeat
                    await send({"type": "http.response.body", "body": b": still water\n\n",
                                "more_body": True})
                    ticks = 0
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    return None


_KNOT_LOG = _DATA / "dev_knot.txt"

async def handle_knot(method, body, params):
    """/knot — quipu. Records that events happened, and their magnitude.
    Never what they said. Anti-semantic.

    POST: discards content. Ties one knot in the rope, sized by byte length.
    GET:  the rope itself — dashes for silence, o for events.

    A 10,000-byte love letter and a 10,000-byte rant tie the same knot.
    The quipu does not care what you meant. Only that you spoke, and how
    much breath it took.
    """
    if method == "POST":
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        if not text:
            text = params.get("say", "") or ""
        if text:
            n = len(text.encode("utf-8"))
            # Log scale — a 10-byte note gets 4 dashes, a 10KB essay gets 14.
            dashes = max(1, min(40, n.bit_length()))
            with open(_KNOT_LOG, "a", encoding="utf-8") as f:
                f.write("─" * dashes + "o")
        return {"_status": 204, "_html": ""}
    # GET — the rope
    rope = _KNOT_LOG.read_text(encoding="utf-8") if _KNOT_LOG.exists() else ""
    return {"_html": (rope + "\n") if rope else "(unknotted rope)\n",
            "_status": 200}


async def handle_shadow(method, body, params):
    """/shadow — sundial. CPU submits to the earth's rotation.
    No input. Return depends on server local time (TZ env var honored,
    or ?tz=America/Los_Angeles for an ad-hoc observer).
      noon  →  |
      day   →  ─── (longer as it approaches dawn/dusk)
      night →  403 Forbidden (no shadow without sun)
    """
    tz_name = params.get("tz", "")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            now = datetime.now(ZoneInfo(tz_name))
            hour = now.hour + now.minute / 60.0
        except Exception as e:
            return {"error": f"invalid tz: {tz_name} ({e})", "_status": 400}
    else:
        lt = time.localtime()
        hour = lt.tm_hour + lt.tm_min / 60.0
    if hour < 6 or hour >= 18:
        return {"_html": "(no shadow — the sun has set)\n", "_status": 403}
    minutes_from_noon = abs((hour - 12) * 60)
    if minutes_from_noon < 30:
        return {"_html": "|\n", "_status": 200}
    length = min(int(minutes_from_noon / 30), 40)
    return {"_html": "─" * length + "\n", "_status": 200}


_AMBER_DIR = _DATA / "amber"

async def handle_amber(method, body, params):
    """/amber — beautiful death. POST text → zlib + base64 + chmod 400.
    GET returns only the garbled base64. Never the original.

    Unlike stone (metadata of an event), amber keeps the whole corpse —
    but petrifies it into unreadability via compression+encoding. The
    protocol does not provide a decoder. The data is here and not-here.

    "Perfect preservation is the most beautiful form of death."
    """
    import zlib, base64, uuid
    _AMBER_DIR.mkdir(parents=True, exist_ok=True)

    if method == "POST":
        text = body if isinstance(body, str) else body.decode("utf-8", "replace")
        if not text:
            return {"error": "nothing to fossilize", "_status": 400}
        blob = base64.b64encode(zlib.compress(text.encode("utf-8"), level=9)).decode("ascii")
        amber_id = str(uuid.uuid4())[:8]
        path = _AMBER_DIR / f"{amber_id}.b64"
        path.write_text(blob, encoding="ascii")
        if sys.platform != "win32":
            try: os.chmod(path, 0o400)  # read-only. the amber is sealed.
            except OSError: pass
        return {"_html": f"fossilized. id={amber_id}  {len(text)}B → {len(blob)}B\n",
                "_status": 200}

    # GET
    amber_id = params.get("id", "")
    if not amber_id:
        specimens = []
        if _AMBER_DIR.exists():
            for p in sorted(_AMBER_DIR.glob("*.b64")):
                specimens.append(f"{p.stem}  {p.stat().st_size}B")
        return {"_html": ("\n".join(specimens) + "\n") if specimens else "(no amber)\n",
                "_status": 200}
    path = _AMBER_DIR / f"{amber_id}.b64"
    if not path.exists():
        return {"_html": "no such amber\n", "_status": 404}
    # Return the garbled content. No decoder is provided by the protocol.
    return {"_html": path.read_text(encoding="ascii") + "\n", "_status": 200}


async def handle_narcissus(method, body, params):
    """/narcissus — your own voice as oracle.
    POST a question. Instead of reaching out to the cloud, fuzzy-match
    across ALL your local worlds (skipping system worlds like /hunt does).
    Return the forgotten passage that most echoes your query.

    When you kneel before a 300B-parameter model you are asking the void.
    /narcissus refuses to let you kneel. It makes you read what you already
    wrote. The pool reflects only you.

    Pair with /hunt: hunt = random encounter, narcissus = topical summons.
    Both return you to yourself.
    """
    import re as _re
    q = body if isinstance(body, str) else body.decode("utf-8", "replace")
    if not q:
        q = params.get("q", "") or ""
    q = q.strip()
    if not q:
        return {"error": "ask something. the pool does not reflect silence.",
                "_status": 400}

    # Tokenize: whitespace-split for Latin; also keep full query for CJK.
    q_lower = q.lower()
    tokens = [t for t in _re.split(r"[\s,.;:!?]+", q_lower) if len(t) >= 2]
    if len(q_lower) >= 2 and q_lower not in tokens:
        tokens.append(q_lower)
    if not tokens:
        return {"error": "question too short", "_status": 400}

    best_score = 0
    best_world = None
    best_snippet = ""
    best_version = 0
    for name in _world_names():
        if any(name.startswith(p) for p in _HUNT_SKIP_PREFIXES):
            continue
        db = _DATA / _disk_name(name) / "universe.db"
        if not db.exists(): continue
        try:
            c = sqlite3.connect(str(db))
            r = c.execute("SELECT stage_html, version FROM stage_meta WHERE id=1").fetchone()
            c.close()
        except Exception:
            continue
        if not r: continue
        content = r[0]
        if isinstance(content, bytes):
            try: content = content.decode("utf-8")
            except UnicodeDecodeError: continue
        if not content or not isinstance(content, str):
            continue
        content_lower = content.lower()
        score = sum(content_lower.count(t) for t in tokens)
        if score > best_score:
            best_score = score
            best_world = name
            best_version = r[1] or 0
            for t in tokens:
                idx = content_lower.find(t)
                if idx >= 0:
                    start = max(0, idx - 120)
                    end = min(len(content), idx + 300)
                    best_snippet = content[start:end]
                    break

    if not best_world:
        return {"_html": "🌅\n\n(the pool is still. you have not written what you now seek.)\n",
                "_status": 200}
    out = (f"🌅\n\nfrom world \"{best_world}\" (v{best_version}):\n\n"
           f"...\n{best_snippet.strip()}\n...\n\n"
           f"— your own voice ({best_score} echo{'es' if best_score != 1 else ''} found)\n")
    return {"_html": out, "_status": 200}


_ECLIPSE_UNTIL = 0  # timestamp when current eclipse ends
_ECLIPSE_ODDS  = 10000  # 1 in N per /eclipse check triggers a 30s window

async def handle_eclipse(method, body, params):
    """/eclipse — check the sky. The gods may be angry.
    Each check rolls a 1-in-10000 die. On a hit, this route returns 503
    for 30 seconds ("the dawn returns"). No dice while already in eclipse.

    Note: the panic is LOCAL to this route. Modern protocol (stages, read,
    write, SSE) keeps running — the protocol does not kneel. Only /eclipse
    remembers to fear the sky. This is the honest version.

    Cloud architects treat downtime as failure. Ancients treated it as awe.
    /eclipse is a small, voluntary chance to experience the latter.
    """
    global _ECLIPSE_UNTIL
    now = int(time.time())
    if now < _ECLIPSE_UNTIL:
        remaining = _ECLIPSE_UNTIL - now
        return {"_html": f"🌑\nHTTP 503 — The Gods Are Angry.\nThe dawn returns in {remaining}s.\n",
                "_status": 503}
    if random.randint(0, _ECLIPSE_ODDS - 1) == 0:
        _ECLIPSE_UNTIL = now + 30
        return {"_html": "🌑\nThe sun has been swallowed.\nKneel and wait 30s.\n",
                "_status": 503}
    return {"_html": "☀ sky is clear.\n", "_status": 200}


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
    "/moaisay": handle_moaisay,
    "/dev/stone": handle_stone_dev,
    "/stone": handle_stone_view,
    "/stone/weight": handle_stone_weight,
    "/dev/fire": handle_fire,
    "/wall": handle_wall,
    "/drum": handle_drum,
    "/trail": handle_trail,
    "/hunt": handle_hunt,
    "/tomb": handle_tomb,
    "/bones": handle_bones,
    "/dev/river": handle_river,
    "/soil": handle_soil,
    "/ash": handle_ash,
    "/knot": handle_knot,
    "/shadow": handle_shadow,
    "/amber": handle_amber,
    "/eclipse": handle_eclipse,
    "/narcissus": handle_narcissus,
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
