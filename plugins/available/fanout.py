"""fanout — one write, many worlds. Unix `tee`, elastik-shaped.

PUT /etc/fanout.conf — one target per line (world name, e.g. home/inbox/claude).
POST /dev/fanout -d "msg"  → append msg to every configured target.
PUT  /dev/fanout -d "msg"  → overwrite every configured target with msg.

The device is blind. It doesn't know what you wrote. It doesn't know
who reads. It reads /etc/fanout.conf at request time — edit conf, next
broadcast reflects it. No reload.

    echo "hi" | tee a b c          # Unix: 1 in, 3 out
    curl /dev/fanout -d "hi"       # elastik: same, via conf

Scope & safety:
  - T2 or T3 required. Cap tokens refused — fanout is broad by design,
    capability tokens are narrow by design.
  - Targets under etc/, usr/, boot/ require T3 (same as writing there
    directly). T2 falls through, per-target, as "requires approve".
  - Invalid names and target errors collected into `failed` list; the
    broadcast still tries every other target. Partial success is OK.
"""
import sqlite3
import server

DESCRIPTION = "/dev/fanout — broadcast one write to many worlds"
AUTH = "none"  # GET renders man page. POST/PUT gate inline.

_SYS_PREFIXES = ("etc/", "usr/", "boot/")


def _read_conf():
    """Return list of target world names from /etc/fanout.conf. Strips
    leading /, strips leading home/, skips blanks + # comments."""
    db = server.DATA / server._disk_name("etc/fanout.conf") / "universe.db"
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        raw = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        c.close()
    except Exception:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept both URL-ish (/home/foo) and bare (home/foo or foo) forms.
        # Internal world names are dotless; /home/ prefix is sugar.
        if line.startswith("/"):
            line = line[1:]
        if line.startswith("home/"):
            line = line[5:]
        out.append(line)
    return out


async def handle_fanout(method, body, params):
    """POST /dev/fanout -d "msg" — broadcast msg to every world in /etc/fanout.conf.

    body: the message to broadcast.
    POST = append to each target. PUT = overwrite each target.

    Conf format (write with T3):
      home/inbox/claude
      home/inbox/gemini
      home/logs/all
      # comments and blank lines ignored

    Example:
      curl -X PUT  localhost:3005/etc/fanout.conf  \\
        -H "Authorization: Bearer $APPROVE"  \\
        -d "home/inbox/claude
      home/inbox/gemini
      home/logs/all"
      curl -X POST localhost:3005/dev/fanout  \\
        -H "Authorization: Bearer $TOKEN" -d "today's announcement"

    Returns {"written":[...], "failed":[{"target":"...", "error":"..."}]}.
    """
    if method not in ("POST", "PUT"):
        return {"error": "POST (append) or PUT (overwrite) only — body is the message",
                "_status": 405}
    scope = params.get("_scope", {})
    auth = server._check_auth(scope)
    if auth not in ("auth", "approve"):
        # Reject anon, cap tokens, and anything else. Fanout is deliberately
        # broad; cap tokens are deliberately narrow. They don't compose here.
        return {"error": "T2 or T3 bearer/basic required (cap tokens don't authorize fanout)",
                "_status": 401,
                "_headers": [["www-authenticate", 'Basic realm="elastik"']]}

    targets = _read_conf()
    if not targets:
        return {"error": "no /etc/fanout.conf — write one (T3): one target per line",
                "_status": 503}

    b = body if isinstance(body, bytes) else (body or "").encode("utf-8", "replace")
    written, failed = [], []
    for name in targets:
        if not server._valid_name(name):
            failed.append({"target": name, "error": "invalid world name"})
            continue
        if name.startswith(_SYS_PREFIXES) and auth != "approve":
            failed.append({"target": name, "error": "system target requires approve"})
            continue
        try:
            c = server.conn(name)
            if method == "POST":
                c.execute(
                    "UPDATE stage_meta SET stage_html=stage_html||?, "
                    "version=version+1, updated_at=datetime('now') WHERE id=1",
                    (b,))
            else:  # PUT
                c.execute(
                    "UPDATE stage_meta SET stage_html=?, "
                    "version=version+1, updated_at=datetime('now') WHERE id=1",
                    (b,))
            c.commit()
            server.log_event(name,
                             "fanout_appended" if method == "POST" else "fanout_written",
                             {"len": len(b)})
            written.append(name)
        except Exception as e:
            failed.append({"target": name, "error": f"{type(e).__name__}: {e}"})

    return {"written": written, "failed": failed,
            "_status": 200 if written or not failed else 500}


ROUTES = {"/dev/fanout": handle_fanout}
