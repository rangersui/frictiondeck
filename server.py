"""elastik — the protocol. Core routes only; everything else is a plugin."""
import asyncio, base64, hashlib, hmac as _hmac, ipaddress as _ipa, json, os, re, shutil, sqlite3, sys, time
from email.utils import formatdate
from pathlib import Path
VERSION = "4"
_BOOT = time.time()
if __name__ == "__main__": sys.modules["server"] = sys.modules[__name__]

DATA = Path("data")
# Load env file: .env, _env, .env.local (iOS doesn't support dotfiles)
for _ef in (".env", "_env", ".env.local"):
    _ep = Path(__file__).resolve().parent / _ef
    if _ep.exists():
        for _line in _ep.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                k, v = _line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break
KEY = os.getenv("ELASTIK_KEY", "elastik-dev-key").encode()
AUTH_TOKEN = os.getenv("ELASTIK_TOKEN", "")
APPROVE_TOKEN = os.getenv("ELASTIK_APPROVE_TOKEN", "")
HOST = os.getenv("ELASTIK_HOST", "127.0.0.1")
PORT = int(os.getenv("ELASTIK_PORT", "3004"))
MAX_BODY = 5 * 1024 * 1024
INDEX = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
SW = Path(__file__).with_name("sw.js").read_text(encoding="utf-8")
MANIFEST = Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
_icon_path = Path(__file__).with_name("icon.png")
ICON = _icon_path.read_bytes() if _icon_path.exists() else None
_icon192_path = Path(__file__).with_name("icon-192.png")
ICON192 = _icon192_path.read_bytes() if _icon192_path.exists() else None
def _csp():
    cdn = "https:"
    try:
        etc_cdn_disk = DATA / "etc%2Fcdn"
        if etc_cdn_disk.exists():
            c = conn("etc/cdn")
            r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
            s = r["stage_html"] if r else None
            if isinstance(s, bytes): s = s.decode("utf-8", "replace")
            if s and s.strip():
                domains = [d.strip() for d in s.splitlines() if d.strip()]
                cdn = " ".join(f"https://{d}" for d in domains)
    except Exception as e: print(f"  warn: CDN config read failed: {e}")
    return (f"default-src 'self' data: blob:; script-src 'unsafe-inline' 'unsafe-eval' {cdn} data:; "
            f"style-src 'unsafe-inline' {cdn} data:; img-src * data: blob:; font-src * data:; "
            f"connect-src 'self'; worker-src 'self'")
_INVALID_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f\\:*?"<>|]')

def _parse_qs(qs):
    """Parse a query string into {k: v}, URL-decoded. Single-value per key.
    Browsers encode `/` as %2F in form fields — this handles that."""
    from urllib.parse import parse_qsl
    return dict(parse_qsl(qs, keep_blank_values=True)) if qs else {}
def _valid_name(name):
    """Check world name: any Unicode allowed, reject control chars + Windows-illegal + traversal."""
    if not name or _INVALID_NAME_CHARS.search(name): return False
    if "//" in name or ".." in name: return False
    if name.startswith("/") or name.endswith("/"): return False
    return True
def _disk_name(name):
    """World name → safe filesystem dir name. / → %2F (flat on disk)."""
    return name.replace("/", "%2F")
def _logical_name(disk):
    """Filesystem dir name → world name. %2F → /."""
    return disk.replace("%2F", "/")
_CT = {"html":"text/html","htm":"text/html","txt":"text/plain","plain":"text/plain",
       "css":"text/css","js":"text/javascript","json":"application/json",
       "png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg","gif":"image/gif",
       "svg":"image/svg+xml","webp":"image/webp","ico":"image/x-icon",
       "pdf":"application/pdf","zip":"application/zip",
       "md":"text/markdown","py":"text/x-python","c":"text/x-c","cpp":"text/x-c++src",
       "h":"text/x-c","go":"text/x-go","rs":"text/x-rust","ts":"text/typescript",
       "tsx":"text/tsx","jsx":"text/jsx","sh":"text/x-sh","yaml":"text/yaml",
       "yml":"text/yaml","toml":"text/toml","xml":"application/xml","sql":"text/x-sql",
       "lua":"text/x-lua","rb":"text/x-ruby","java":"text/x-java",
       "kt":"text/x-kotlin","swift":"text/x-swift","v":"text/x-verilog"}
def _ext_to_ct(ext): return _CT.get(ext or "plain", "application/octet-stream")
_BINARY_EXT = {"png","jpg","jpeg","gif","webp","ico","pdf","zip","mp3","mp4",
               "wav","ogg","woff","woff2","ttf","otf","eot","bin"}
# FHS top-level namespaces + per-world internal ops. Module-level so
# DELETE (line <710) can read them before the GET-ls branch would assign.
_FHS = {"home", "etc", "usr", "var", "boot", "lib"}
_INTERNAL = {"sync", "pending", "result", "clear"}

# ── Public gate (inline; formerly plugins/available/public_gate.py) ──
# Header-only HTTP auth for non-localhost traffic. Inlined from the
# Tier 0 plugin in v4.5.0's microkernel cut. No cookies. No URL
# tokens. Authorization header is the only authenticated surface.
# App shell resources pass through anonymously (PWA needs that).
_PUBLIC_SHELL = {
    "/manifest.json", "/sw.js", "/opensearch.xml",
    "/favicon.ico", "/icon.png", "/icon-192.png",
}
_TRUST_HEADER = os.getenv("ELASTIK_TRUST_PROXY_HEADER", "").lower()
_TRUST_FROM = []
for _c in os.getenv("ELASTIK_TRUST_PROXY_FROM", "").split(","):
    _c = _c.strip()
    if _c:
        try: _TRUST_FROM.append(_ipa.ip_network(_c, strict=False))
        except ValueError: pass
if APPROVE_TOKEN and _TRUST_HEADER and not _TRUST_FROM:
    print("  public_gate: refusing — ELASTIK_TRUST_PROXY_HEADER set "
          "but ELASTIK_TRUST_PROXY_FROM is empty", file=sys.stderr)
    sys.exit(1)

def _real_ip(scope):
    """Resolve the real client IP, honouring X-Forwarded-For only when
    the immediate hop is in ELASTIK_TRUST_PROXY_FROM."""
    ip = (scope.get("client") or ["127.0.0.1"])[0]
    if _TRUST_HEADER and _TRUST_FROM:
        try: addr = _ipa.ip_address(ip)
        except ValueError: return ip
        if any(addr in n for n in _TRUST_FROM):
            v = dict(scope.get("headers", [])).get(_TRUST_HEADER.encode(), b"").decode()
            if v: return v.split(",")[0].strip()
    return ip

# ── X-Meta-* metadata headers (Phase 1: X-Meta-* only, PUT only) ────
# Blind propagation: PUT request X-Meta-* headers are stored with the
# world and replayed on GET/?raw. Not a governance surface — elastik
# never interprets the values, never auth-checks on them, never routes
# on them. Narrow whitelist (x-meta-* only) on both write and read to
# keep infra response-control headers (X-Accel-Redirect, X-Sendfile,
# X-Frame-Options) out of the reflection path.
_MAX_META_VAL   = 1024       # per-header value bytes
_MAX_META_TOTAL = 8192       # serialized JSON total bytes (same metric both sides)
_META_NAME  = re.compile(r'^[a-z0-9!#$%&*+\-.^_`|~]+$')   # RFC 7230 token, lowercase
_META_VALUE = re.compile(r'^[\t\x20-\x7e]*$')              # visible ASCII + space + HT

def _extract_meta_headers(scope):
    """Request scope → stored JSON list-of-pairs. Lowercase, validated,
    fail-closed. Single over-size → drop that header; serialized total
    over _MAX_META_TOTAL → drop all. Returns '[]' if none."""
    out = []
    for k, v in scope.get("headers", []):
        try:
            name = k.decode("ascii", "strict").lower()
            value = v.decode("ascii", "strict")
        except UnicodeDecodeError:
            continue
        if not name.startswith("x-meta-"): continue
        if not _META_NAME.match(name):     continue
        if not _META_VALUE.match(value):   continue
        if len(value) > _MAX_META_VAL:     continue
        out.append([name, value])
    if not out: return "[]"
    s = json.dumps(out, ensure_ascii=True, separators=(",", ":"))
    return s if len(s) <= _MAX_META_TOTAL else "[]"

def _replay_meta_headers(stored):
    """Stored JSON → ASGI extra-headers list (byte-encoded pairs).
    Re-validates every pair on read — DB may have been hand-edited or
    imported with dirty data. Total size uses the exact same metric
    as the write side (serialized JSON len with identical separators)
    so write/read invariants are symmetric.

    Metadata lives in response headers — period. The JSON GET body no
    longer duplicates it. Body is content, header is metadata; this
    function's only job is to produce the headers."""
    try:
        stored_list = json.loads(stored or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(stored_list, list):
        return []
    filtered = []
    for item in stored_list:
        if not (isinstance(item, list) and len(item) == 2): continue
        k, v = item
        if not (isinstance(k, str) and isinstance(v, str)): continue
        if not k.startswith("x-meta-"):                     continue
        if not _META_NAME.match(k):                         continue
        if not _META_VALUE.match(v):                        continue
        if len(v) > _MAX_META_VAL:                          continue
        filtered.append([k, v])
    # Total-size check uses the same metric as the write side so a
    # hand-written DB >8 KB JSON gets rejected identically on read.
    serialized = json.dumps(filtered, ensure_ascii=True, separators=(",", ":"))
    if len(serialized) > _MAX_META_TOTAL:
        return []
    return [[k.encode(), v.encode()] for k, v in filtered]

def _infer_type(stage_html):
    """Heuristic for one-time migration of pre-type-column worlds."""
    s = (stage_html or '').lstrip()
    if not s: return 'plain'
    if s.startswith('<!--use:'): return 'html'
    if s[0] == '<': return 'html'
    sl = s[:1024].lower()
    if '<script' in sl or '<body' in sl or '<html' in sl: return 'html'
    return 'plain'

def _lookup_etc_auth(user, pwd):
    """Look up user:password in /etc/passwd (user:tier) + /etc/shadow (user:sha256(tok)).
    Returns 'approve' for T3, 'auth' for T2, None for T1 or unknown.

    /etc/passwd format, one per line:   user:T1|T2|T3
    /etc/shadow format, one per line:   user:<sha256 hex of token>
    Both stored as regular worlds under etc/. Read auth for /etc/shadow is
    enforced separately (T3 only — see the /read dispatch)."""
    passwd_disk = DATA / "etc%2Fpasswd"
    shadow_disk = DATA / "etc%2Fshadow"
    if not (passwd_disk.exists() and shadow_disk.exists()): return None
    try:
        pwd_raw = conn("etc/passwd").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"] or ""
        if isinstance(pwd_raw, bytes): pwd_raw = pwd_raw.decode("utf-8", "replace")
        tiers = {}
        for line in pwd_raw.splitlines():
            if ":" in line:
                u, t = line.split(":", 1); tiers[u.strip()] = t.strip()
        if user not in tiers: return None
        tier = tiers[user]
        shd_raw = conn("etc/shadow").execute(
            "SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"] or ""
        if isinstance(shd_raw, bytes): shd_raw = shd_raw.decode("utf-8", "replace")
        hashes = {}
        for line in shd_raw.splitlines():
            if ":" in line:
                u, h = line.split(":", 1); hashes[u.strip()] = h.strip()
        expected = hashes.get(user)
        if not expected: return None
        actual = hashlib.sha256(pwd.encode("utf-8")).hexdigest()
        if not _hmac.compare_digest(actual, expected): return None
        if tier == "T3": return "approve"
        if tier == "T2": return "auth"
        return None  # T1 or unknown tier — treat as unauthenticated for write gates
    except Exception:
        return None


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _mint_cap(prefix, ttl_sec=3600, mode="rw"):
    """Issue a capability token: <b64url(payload)>.<b64url(hmac)>.
    payload = `<prefix>|<exp_sec>|<mode>`. HMAC-SHA256 over payload with KEY.
    prefix is normalized — leading "/", no trailing "/" unless root."""
    prefix = prefix if prefix.startswith("/") else "/" + prefix
    prefix = prefix.rstrip("/") or "/"
    if mode not in ("r", "rw"):
        raise ValueError("mode must be 'r' or 'rw'")
    exp = int(time.time()) + int(ttl_sec)
    payload = f"{prefix}|{exp}|{mode}".encode()
    sig = _hmac.new(KEY, payload, hashlib.sha256).digest()
    return _b64e(payload) + "." + _b64e(sig)

def _verify_cap(token):
    """Returns (prefix, exp, mode) or None. HMAC + expiration check."""
    parts = token.split(".", 1)
    if len(parts) != 2: return None
    try:
        payload = _b64d(parts[0])
        sig = _b64d(parts[1])
    except Exception:
        return None
    expected = _hmac.new(KEY, payload, hashlib.sha256).digest()
    if not _hmac.compare_digest(sig, expected): return None
    try:
        prefix, exp_s, mode = payload.decode().split("|", 2)
        exp = int(exp_s)
    except Exception:
        return None
    if mode not in ("r", "rw"): return None
    if exp < int(time.time()): return None
    return prefix, exp, mode

def _path_in_scope(path, prefix):
    """Path canonicalization + prefix-boundary check.
    Resolves '..' via posixpath.normpath BEFORE the startswith check —
    without this step, '/home/dreams/../etc/shadow' would pass a naive
    startswith on '/home/dreams'. Boundary-sensitive: '/home/dre' must
    not match '/home/dreams'."""
    import posixpath
    cleaned = posixpath.normpath("/" + path.lstrip("/"))
    p = prefix.rstrip("/") or "/"
    if p == "/":
        return True  # root scope covers everything
    return cleaned == p or cleaned.startswith(p + "/")


def _check_auth(scope):
    """Authorization header → auth level.

    Returns:
      "approve"                 — T3 admin (env APPROVE_TOKEN or Basic pwd)
      "auth"                    — T2 user (env AUTH_TOKEN or Basic pwd)
      "cap:<mode>:<prefix>"     — scoped capability token, valid + in-scope
      None                      — anonymous OR out-of-scope cap (indistinguishable
                                  by design — leaks no path existence)

    Cap tokens are recognized by a '.' in the bearer token (they are
    `<b64>.<b64>` shaped). Legacy opaque tokens have no dot.
    """
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            a = v.decode("utf-8", "replace")
            if a.startswith("Bearer "):
                tok = a[7:]
                if "." in tok:
                    cap = _verify_cap(tok)
                    if cap is None: return None
                    prefix, _exp, mode = cap
                    if _path_in_scope(scope.get("path", "/"), prefix):
                        return f"cap:{mode}:{prefix}"
                    return None
                if APPROVE_TOKEN and _hmac.compare_digest(tok, APPROVE_TOKEN): return "approve"
                if AUTH_TOKEN and _hmac.compare_digest(tok, AUTH_TOKEN): return "auth"
            elif a.startswith("Basic "):
                try:
                    user, pwd = base64.b64decode(a[6:]).decode().split(":", 1)
                    if APPROVE_TOKEN and _hmac.compare_digest(pwd, APPROVE_TOKEN): return "approve"
                    if AUTH_TOKEN and _hmac.compare_digest(pwd, AUTH_TOKEN): return "auth"
                    return _lookup_etc_auth(user, pwd)
                except (ValueError, UnicodeDecodeError): pass
            return None
    return None

    # _check_url_auth — injected by url_auth plugin if installed.
    # Not here. No plugin = no URL auth = no attack surface.


async def _public_gate(scope, receive, send, path, method):
    """Inline public-access gate. Returns True if it sent a 401
    response (caller should stop processing), None to let the request
    continue.

    Active when ELASTIK_APPROVE_TOKEN is set. App shell resources
    (_PUBLIC_SHELL) pass through anonymously. Localhost (127.* / ::1)
    passes through. Anything else needs an Authorization header —
    Basic, Bearer, or cap token.

    Behind a reverse proxy, set ELASTIK_TRUST_PROXY_HEADER and
    ELASTIK_TRUST_PROXY_FROM so _real_ip() resolves X-Forwarded-For
    correctly; without those, the proxy's own IP is what the gate
    sees and traffic either always-passes or always-fails depending
    on the proxy's network position.
    """
    if not APPROVE_TOKEN: return None
    if path in _PUBLIC_SHELL: return None
    ip = _real_ip(scope)
    if ip.startswith("127.") or ip == "::1": return None
    if _check_auth(scope): return None
    body = b"pastebin\nPOST to create, GET /<key> to fetch.\n"
    await send({"type": "http.response.start", "status": 401, "headers": [
        [b"www-authenticate", b'Basic realm="pastebin"'],
        [b"content-type", b"text/plain; charset=utf-8"],
        [b"content-length", str(len(body)).encode()],
        [b"server", b"pastebin"]]})
    await send({"type": "http.response.body", "body": body})
    return True


_db = {}

def _release_world(name):
    """Close cached sqlite connection for `name`, unlink WAL/SHM, and
    collect garbage. Call before rename()-ing a world dir on Windows —
    sqlite3's close() doesn't always release the WAL/SHM file handles
    immediately, and Windows won't rename a dir containing an open file."""
    import gc, time
    if name in _db:
        c = _db.pop(name)
        try: c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception: pass
        try: c.rollback()
        except Exception: pass
        try: c.close()
        except Exception: pass
    gc.collect()
    # After checkpoint + close, WAL/SHM should be removable. If they're
    # not — the handle is still stuck somewhere — we swallow the error
    # and let the rename retry loop handle it.
    d = DATA / _disk_name(name)
    if d.exists():
        for ext in ("-wal", "-shm"):
            try: (d / f"universe.db{ext}").unlink(missing_ok=True)
            except (OSError, PermissionError): pass
    time.sleep(0.01)

def _move_to_trash(name):
    """Move world dir to .trash/<disk_name>. 10-attempt retry loop for
    the Windows file-handle race, then copytree+rmtree fallback. Call
    _release_world first."""
    import shutil, time
    src = DATA / _disk_name(name)
    if not src.exists():
        return
    trash = DATA / ".trash" / _disk_name(name)
    trash.parent.mkdir(parents=True, exist_ok=True)
    if trash.exists():
        shutil.rmtree(trash)
    last_err = None
    for attempt in range(10):
        try:
            src.rename(trash)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.03 * (attempt + 1))
    # Fallback — slower but works when rename can't win the file-lock race.
    try:
        shutil.copytree(str(src), str(trash))
        shutil.rmtree(str(src), ignore_errors=True)
    except Exception:
        raise last_err

def conn(name):
    if name not in _db:
        d = DATA / _disk_name(name); d.mkdir(parents=True, exist_ok=True)
        db_path = d / "universe.db"
        # NOTE: do NOT delete -wal/-shm here. They are not stale state — the
        # WAL holds uncheckpointed commits. SQLite auto-recovers from WAL on
        # open. Blowing them away silently nukes any small write that hadn't
        # yet been checkpointed (default: <1000 pages). This cost us /etc/*.
        c = sqlite3.connect(str(db_path), check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
        except sqlite3.OperationalError:
            pass
        c.executescript("""
            CREATE TABLE IF NOT EXISTS stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html BLOB DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '',
                ext TEXT DEFAULT 'plain', headers TEXT DEFAULT '[]',
                state TEXT DEFAULT 'pending');
            INSERT OR IGNORE INTO stage_meta(id,updated_at) VALUES(1,datetime('now'));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT DEFAULT '{}',
                hmac TEXT NOT NULL, prev_hmac TEXT DEFAULT '');
        """)
        # Migration: stage_html TEXT+type → stage_html BLOB+ext.
        # Column NAME stays stage_html (40+ references). Type changes to BLOB.
        cols = {row[1] for row in c.execute("PRAGMA table_info(stage_meta)").fetchall()}
        def _safe(row, key, default=""):
            try: return row[key] or default
            except (IndexError, KeyError): return default
        if "ext" not in cols:
            # Old schema: stage_html TEXT, type TEXT, no ext → full rebuild
            r = c.execute("SELECT * FROM stage_meta WHERE id=1").fetchone()
            content = r["stage_html"] or ""
            old_type = "plain"
            try: old_type = r["type"] or "plain"
            except (IndexError, KeyError): pass
            # Strip legacy :::type:xxx::: prefix if present
            import re as _re
            _m = _re.match(r'^:::type:(\w+):::\n?', content) if content else None
            if _m:
                ext = _m.group(1)
                content = content[_m.end():]
            else:
                ext = old_type
                if ext == "plain" and _infer_type(content) == "html":
                    ext = "html"
            c.execute("DROP TABLE stage_meta")
            c.executescript("""CREATE TABLE stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html BLOB DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '',
                ext TEXT DEFAULT 'plain', headers TEXT DEFAULT '[]');""")
            c.execute("INSERT INTO stage_meta VALUES(?,?,?,?,?,?,?,?)",
                (1, content, _safe(r,"pending_js"), _safe(r,"js_result"),
                 _safe(r,"version",0), _safe(r,"updated_at"), ext, "[]"))
            c.commit()
            print(f"  migrated: {name} (ext={ext})")
        elif "stage_html" not in cols and "stage" in cols:
            # Broken schema from earlier attempt: column named 'stage' → fix to 'stage_html'
            r = c.execute("SELECT * FROM stage_meta WHERE id=1").fetchone()
            c.execute("DROP TABLE stage_meta")
            c.executescript("""CREATE TABLE stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html BLOB DEFAULT '', pending_js TEXT DEFAULT '', js_result TEXT DEFAULT '',
                version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '',
                ext TEXT DEFAULT 'plain', headers TEXT DEFAULT '[]');""")
            c.execute("INSERT INTO stage_meta VALUES(?,?,?,?,?,?,?,?)",
                (1, _safe(r,"stage"), _safe(r,"pending_js"), _safe(r,"js_result"),
                 _safe(r,"version",0), _safe(r,"updated_at"), _safe(r,"ext","html"), "[]"))
            c.commit()
            print(f"  fixed: {name} (stage→stage_html)")
        elif "headers" not in cols:
            # Phase 1 (X-Meta-* propagation): one new column. Empty default
            # means existing worlds transparently have no metadata until
            # someone PUTs with X-Meta-* headers.
            c.execute("ALTER TABLE stage_meta ADD COLUMN headers TEXT DEFAULT '[]'")
            c.execute("UPDATE stage_meta SET headers='[]' WHERE headers IS NULL OR headers=''")
            c.commit()
        if "state" not in cols:
            # plugin-as-world Phase 0: `state` column for /lib/* plugin
            # lifecycle (pending / active / disabled). Existing non-plugin
            # worlds get 'pending' as default — meaningless outside /lib/*,
            # harmless inside. Route contract: PUT /lib/<name> creates with
            # state='pending'; PUT /lib/<name>/state with approve promotes.
            c.execute("ALTER TABLE stage_meta ADD COLUMN state TEXT DEFAULT 'pending'")
            c.execute("UPDATE stage_meta SET state='pending' WHERE state IS NULL OR state=''")
            c.commit()
        _db[name] = c
    return _db[name]

def log_event(name, etype, payload=None):
    c = conn(name); p = json.dumps(payload or {}, ensure_ascii=False)
    row = c.execute("SELECT hmac FROM events ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hmac"] if row else ""
    h = _hmac.new(KEY, (prev + p).encode(), hashlib.sha256).hexdigest()
    c.execute("INSERT INTO events(timestamp,event_type,payload,hmac,prev_hmac) VALUES(datetime('now'),?,?,?,?)",
              (etype, p, h, prev)); c.commit()

def _extract(b, action):
    if b.startswith("{") and action != "patch":
        try:
            p = json.loads(b)
            return p.get("body") or p.get("content") or p.get("text") or b
        except json.JSONDecodeError: pass
    return b

async def recv(receive):
    b = b""
    while True:
        m = await receive(); b += m.get("body", b"")
        if len(b) > MAX_BODY: raise ValueError("body too large")
        if not m.get("more_body"): return b

async def send_r(send, status, data, ct="application/json", csp=False, extra_headers=None, head_only=False):
    body = data.encode("utf-8") if isinstance(data, str) else data
    _ct = ct if "charset" in ct or ct.startswith(("image/", "audio/", "video/", "application/octet")) else (ct + "; charset=utf-8" if "/" in ct else ct)
    h = [[b"content-type", _ct.encode()], [b"content-length", str(len(body)).encode()]]
    if csp: h.append([b"content-security-policy", _csp().encode()])
    if extra_headers: h.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": h})
    # HEAD: Content-Length still reflects what GET would return; body bytes
    # are empty. Caller passes head_only=True when method == "HEAD".
    await send({"type": "http.response.body", "body": b"" if head_only else body})

def _check_basic_auth(scope):
    """Legacy compat — returns True if auth level is approve. Used by plugin dispatch."""
    return _check_auth(scope) == "approve"

# ── Plugin slots — empty by default. plugins.py fills them. ──────────
_plugins = {}      # route path → async handler
_plugin_auth = {}  # route path → "none" | "auth" | "approve"
_auth = None       # auth middleware (set by auth plugin)
_plugin_meta = []  # plugin metadata list

def _check_auth_token(scope):
    """Legacy compat — returns True if any auth level present, or no token configured."""
    if not AUTH_TOKEN: return True  # no token configured = open
    return _check_auth(scope) is not None

def _ls(prefix):
    """List immediate children under a world-name prefix. Like ls.
    prefix="" → top-level names. prefix="photos" → children of photos/.
    Returns sorted list of (child_name, is_dir) tuples."""
    all_names = []
    if DATA.exists():
        for d in sorted(DATA.iterdir()):
            if d.is_dir() and (d / "universe.db").exists():
                all_names.append(_logical_name(d.name))
    children = {}
    pfx = prefix + "/" if prefix else ""
    for w in all_names:
        if pfx and not w.startswith(pfx): continue
        rest = w[len(pfx):] if pfx else w
        if "/" in rest:
            children[rest.split("/")[0]] = True   # directory
        else:
            children[rest] = False                 # file
    return sorted(children.items())

def _match_plugin(base_path):
    """Exact match first, then prefix match (longest wins).
    Returns (handler, matched_route) or (None, None)."""
    h = _plugins.get(base_path)
    if h: return h, base_path
    best = None
    for p in _plugins:
        if base_path.startswith(p + "/") and (best is None or len(p) > len(best)):
            best = p
    return (_plugins[best], best) if best else (None, None)

MAX_URL = 8192  # URL length cap — DoS protection for the 8K–65K range.
                # By the time the app sees scope["path"], httptools has already
                # applied its uint16 wrap (if any), so this cap CANNOT prevent
                # a 70 KB URL from being truncated to 4 KB — it only catches
                # giant URLs that haven't wrapped yet. The uint16-wrap class
                # itself is handled by architecture: routing matches prefixes
                # (truncation chops tails, leaves /admin/ /etc/ intact), '..'
                # is a byte-level check, and cap scope uses the same (possibly
                # truncated) path the router uses — no smuggling. See
                # logs/redteam_uint16.py for the full claim set and proofs.

async def app(scope, receive, send):
    if scope["type"] != "http": return
    _raw_path = scope["path"]; trailing_slash = _raw_path.endswith("/") and len(_raw_path) > 1
    path = _raw_path.rstrip("/") or "/"; method = scope["method"]
    try: print(f"  {method} {path}")
    except UnicodeEncodeError: print(f"  {method} {ascii(path)}")
    # URL length gate — DoS cap only. Catches URLs 8K–65K; cannot catch
    # ≥65K (those have already been uint16-wrapped by httptools by the time
    # we get here). See MAX_URL comment above for the architectural layer.
    raw_len = len(scope.get("raw_path", b"")) or len(_raw_path)
    if raw_len > MAX_URL:
        return await send_r(send, 414, '{"error":"URI too long"}')
    # Public gate — inline (v4.5.0 microkernel cut; formerly a Tier 0
    # plugin at plugins/available/public_gate.py). Runs on every
    # request when ELASTIK_APPROVE_TOKEN is set. Returns True if it
    # sent a 401; None to proceed.
    if await _public_gate(scope, receive, send, path, method):
        return
    # Legacy plugin-registered middleware (kept so a user-written
    # Tier 1 plugin could in principle still set server._auth). Runs
    # after the inline gate so a plugin can ADD stricter auth on top.
    if _auth:
        if await _auth(scope, receive, send, path, method):
            return
    raw = scope.get("raw_path", b"").decode("utf-8", "replace")
    if '..' in path or '//' in path or '..' in raw or '//' in raw:
        return await send_r(send, 400, '{"error":"invalid path"}')

    # (auth gate moved to top of app — see above)
    parts = [p for p in path.split("/") if p]

    # /bin/* → alias for plugin routes. /bin/grep = /grep. FHS executable namespace.
    # GET /bin → list all plugin routes (like `ls /bin`).
    if path == "/bin" and method == "GET":
        # ls /bin — every plugin route with one-line description
        bins = []
        for route in sorted(_plugins.keys()):
            doc = ""
            h = _plugins[route]
            if callable(h) and h.__doc__:
                doc = h.__doc__.strip().split("\n")[0].strip()
            bins.append({"route": route, "description": doc})
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept": accept = v.decode(); break
        if accept.startswith("text/html"):
            return await send_r(send, 200, INDEX, "text/html", csp=True)
        if "json" in accept:
            return await send_r(send, 200, json.dumps(bins))
        # plain text — one per line, pipe-friendly (curl | grep)
        return await send_r(send, 200, "\n".join(b["route"].lstrip("/") for b in bins) + "\n", "text/plain")
    if path.startswith("/bin/"):
        path = path[4:]  # /bin/grep → /grep
        base_path_override = path.split("?")[0]
    else:
        base_path_override = None
    # Plugin dispatch — exact or prefix match, server gates auth centrally.
    base_path = base_path_override or path.split("?")[0]
    handler, matched = _match_plugin(base_path)
    if handler:
        level = _plugin_auth.get(matched, "none")
        # OPTIONS is capability discovery — always allow so WebDAV/CORS works.
        if method != "OPTIONS":
            if level == "approve":
                if not APPROVE_TOKEN:
                    return await send_r(send, 403, '{"error":"approve token not configured"}')
                if not _check_basic_auth(scope):
                    return await send_r(send, 401, '{"error":"authentication required"}',
                                        extra_headers=[[b"www-authenticate", b'Basic realm="elastik"']])
            elif level == "auth":
                if not _check_auth_token(scope):
                    return await send_r(send, 403, '{"error":"unauthorized"}')
        try: body_raw = await recv(receive)
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        b = body_raw.decode("utf-8", "replace")
        qs = scope.get("query_string", b"").decode()
        params = _parse_qs(qs)
        # Man page: browser GET, no query params, handler has docstring → show form
        if method == "GET" and not qs:
            accept = ""
            for k, v in scope.get("headers", []):
                if k == b"accept": accept = v.decode(); break
            if accept.startswith("text/html") and callable(handler) and handler.__doc__:
                doc = handler.__doc__.strip()
                route = base_path_override or matched
                # Parse params from docstring: find ?key=value and &key=value patterns
                import re as _re
                _params = list(dict.fromkeys(_re.findall(r'[?&](\w+)=', doc)))  # dedupe, keep order
                _is_post = "POST " in doc or "body" in doc.lower()[:200]
                # Build form fields
                if _is_post:
                    seen = set()
                    _fields = ""
                    # Query-param inputs first (e.g. ?file=, ?world=, ?ext=)
                    for p in _params or []:
                        if p not in seen and p != "_body":
                            seen.add(p)
                            _fields += (f'<div style="margin:4px 0"><label style="display:inline-block;width:80px;font-weight:bold">{p}</label>'
                                        f'<input name="{p}" placeholder="{p}..." style="font:14px monospace;padding:4px;width:60%"></div>')
                    # Then the body textarea
                    _fields += (f'<textarea name="_body" rows="4" placeholder="body..." '
                                f'style="font:14px monospace;padding:6px;width:95%;display:block;margin:4px 0"></textarea>')
                    _method = "POST"
                    _qex = "&".join(f"{p}=..." for p in _params) if _params else ""
                    _curl = (f'curl -X POST "localhost:3005{route}?{_qex}" -d "..."' if _qex
                             else f'curl -X POST localhost:3005{route} -d "..."')
                else:
                    seen = set()
                    _fields = ""
                    for p in (_params or ["q"]):
                        if p not in seen:
                            seen.add(p)
                            _fields += (f'<div style="margin:4px 0"><label style="display:inline-block;width:80px;font-weight:bold">{p}</label>'
                                        f'<input name="{p}" placeholder="{p}..." style="font:14px monospace;padding:4px;width:60%"></div>')
                    _method = "GET"
                    _qex = "&".join(f"{p}=..." for p in (_params or ["q"]))
                    _curl = f'curl "localhost:3005{route}?{_qex}"'
                _script = (
                    '<script>'
                    'document.querySelectorAll("form").forEach(f=>f.addEventListener("submit",async e=>{'
                    'e.preventDefault();'
                    'const fd=new FormData(f),m=f.method.toUpperCase();'
                    'let url=f.action,opts={method:m};'
                    'if(m==="GET"){'
                    'const q=new URLSearchParams();for(const[k,v]of fd)if(v)q.set(k,v);'
                    'if([...q].length)url+="?"+q;'
                    '}else{'
                    # POST: non-_body fields become query-string params, _body is the body
                    'const q=new URLSearchParams();'
                    'for(const[k,v]of fd)if(k!=="_body"&&v)q.set(k,v);'
                    'if([...q].length)url+="?"+q;'
                    'opts.body=fd.get("_body")||"";'
                    'opts.headers={"Content-Type":"text/plain"};'
                    '}'
                    'let out=document.getElementById("_out");'
                    'if(!out){out=document.createElement("div");out.id="_out";'
                    'out.style="margin-top:1em;padding:1em;background:#0f172a;color:#e2e8f0;border-radius:4px;font:13px ui-monospace,monospace;overflow:auto";'
                    'f.parentNode.appendChild(out);}'
                    'out.innerHTML="<div style=\\"color:#94a3b8;font-size:11px;margin-bottom:6px\\">sending…</div>";'
                    'try{'
                    'const r=await fetch(url,opts),ct=r.headers.get("content-type")||"",t=await r.text();'
                    'const meta=`<div style="color:#94a3b8;font-size:11px;margin-bottom:6px">HTTP ${r.status} · ${ct}</div>`;'
                    'const pre=document.createElement("pre");pre.style="margin:0;white-space:pre-wrap;word-break:break-word";pre.textContent=t;'
                    'out.innerHTML=meta;out.appendChild(pre);'
                    '}catch(err){out.innerHTML=`<div style="color:#f87171">${err}</div>`;}'
                    '}));'
                    '</script>'
                )
                _man = (f'<meta charset="utf-8"><div style="font:14px/1.6 system-ui;max-width:700px;margin:2em auto;padding:0 1em">'
                        f'<h2 style="margin:0 0 .5em">{route}</h2>'
                        f'<pre style="white-space:pre-wrap;background:#f5f5f5;padding:1em;border-radius:4px;font-size:13px">{doc}</pre>'
                        f'<div style="margin:8px 0"><code style="background:#e8e8e8;padding:4px 8px;border-radius:3px;font-size:12px">{_curl}</code></div>'
                        f'<form method="{_method}" action="{route}" style="margin-top:1em;padding:1em;background:#fafafa;border:1px solid #eee;border-radius:4px">'
                        f'{_fields}'
                        f'<button style="margin-top:8px;padding:6px 16px;cursor:pointer">{_method} {route}</button></form>'
                        f'{_script}</div>')
                return await send_r(send, 200, _man, "text/html")
        # If body came from a man-page HTML form, extract the _body field
        if b.startswith("_body="):
            from urllib.parse import unquote_plus
            b = unquote_plus(b[6:])
        params["_scope"] = scope
        params["_body_raw"] = body_raw  # raw bytes for binary plugins (dav PUT)
        params["_send"] = send          # raw send for plugins that stream (SSE)
        result = await handler(method, b, params)
        if result is None: return       # plugin streamed its own response
        status = result.pop("_status", 200)
        redirect = result.pop("_redirect", None)
        cookies = result.pop("_cookies", [])
        html_body = result.pop("_html", None)
        raw_body = result.pop("_body", None)
        ct = result.pop("_ct", "application/json")
        custom_h = result.pop("_headers", [])
        extra_h = [[b"set-cookie", c.encode()] for c in cookies]
        if custom_h: extra_h.extend([[str(k).encode(), str(v).encode()] for k, v in custom_h])
        if redirect: extra_h.append([b"location", redirect.encode()]); status = 302
        if html_body is not None:
            _hct = "text/html" if re.search(r"<[a-zA-Z/!]", html_body) else "text/plain"
            return await send_r(send, status, html_body, ct=_hct, extra_headers=extra_h or None)
        if raw_body is not None:
            return await send_r(send, status, raw_body, ct=ct, extra_headers=extra_h or None)
        return await send_r(send, status, json.dumps(result), extra_headers=extra_h or None)

    if method == "GET" and path == "/opensearch.xml":
        host = "localhost"
        for k, v in scope.get("headers", []):
            if k == b"host": host = v.decode(); break
        scheme = "https" if scope.get("scheme") == "https" else "http"
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
               f'<ShortName>elastik</ShortName><Description>elastik shell</Description>'
               f'<Url type="text/html" template="{scheme}://{host}/shell?q={{searchTerms}}"/>'
               f'</OpenSearchDescription>')
        return await send_r(send, 200, xml, ct="application/opensearchdescription+xml")
    if method == "GET" and path == "/sw.js": return await send_r(send, 200, SW, "application/javascript")
    if method == "GET" and path == "/manifest.json": return await send_r(send, 200, MANIFEST, "application/manifest+json")
    if method == "GET" and path == "/icon.png" and ICON:
        await send({"type":"http.response.start","status":200,"headers":[[b"content-type",b"image/png"]]})
        await send({"type":"http.response.body","body":ICON})
        return
    if method == "GET" and path == "/icon-192.png" and ICON192:
        await send({"type":"http.response.start","status":200,"headers":[[b"content-type",b"image/png"]]})
        await send({"type":"http.response.body","body":ICON192})
        return
    # Web Share Target — phone share sheet → store in "shared" world
    if method == "POST" and path == "/share":
        _origin = ""
        for k, v in scope.get("headers", []):
            if k == b"origin": _origin = v.decode(); break
        if _origin and not _origin.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")):
            return await send_r(send, 403, '{"error":"cross-origin rejected"}')
        try: body = (await recv(receive)).decode("utf-8", "replace")
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        qs = scope.get("query_string", b"").decode()
        params = _parse_qs(qs)
        # Collect shared content from query params + body
        from urllib.parse import unquote_plus as unquote
        parts = []
        for k in ("title", "text", "url"):
            v = unquote(params.get(k, ""))
            if v: parts.append(v)
        if body and body.strip(): parts.append(body.strip())
        content = "\n".join(parts) if parts else "(empty share)"
        ts = __import__('time').strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n---\n{ts}\n{content}\n"
        c = conn("shared")
        c.execute("UPDATE stage_meta SET stage_html=stage_html||?,version=version+1,updated_at=datetime('now') WHERE id=1",(entry,)); c.commit()
        # Redirect to /shared so user sees what they shared
        return await send_r(send, 302, "", extra_headers=[[b"location", b"/shared"]])
    # /dev — ls the device plugins. `_plugins` is populated at plugin load,
    # filter by /dev/* prefix. Unlike /proc, entries are dynamic — reflects
    # whichever plugins are currently loaded. Browser shell already filters
    # /bin for /dev/* routes, so this endpoint is mostly for `curl /dev/`
    # to work symmetrically with /proc/ and /home/.
    if method in ("GET", "HEAD") and path == "/dev":
        _dev_routes = sorted(r for r in _plugins.keys() if r.startswith("/dev/"))
        _ho = method == "HEAD"
        _acc = ""
        for k, v in scope.get("headers", []):
            if k == b"accept": _acc = v.decode(); break
        if "json" in _acc:
            _entries = []
            for route in _dev_routes:
                h = _plugins.get(route)
                doc = ""
                if callable(h) and h.__doc__:
                    doc = h.__doc__.strip().split("\n")[0].strip()
                _entries.append({"name": route[5:], "route": route, "description": doc})
            return await send_r(send, 200, json.dumps(_entries), head_only=_ho)
        return await send_r(send, 200, "\n".join(r[5:] for r in _dev_routes) + "\n",
                            "text/plain", head_only=_ho)
    # /proc — ls the pseudo-filesystem. Like `ls /proc` on Linux: lists the
    # introspection endpoints, doesn't expose them as on-disk worlds. Static
    # list because /proc/* entries are hand-written, not world-backed.
    if method in ("GET", "HEAD") and path == "/proc":
        _proc_entries = ["status", "uptime", "version", "worlds"]
        _ho = method == "HEAD"
        _acc = ""
        for k, v in scope.get("headers", []):
            if k == b"accept": _acc = v.decode(); break
        if "json" in _acc:
            return await send_r(send, 200, json.dumps(
                [{"name": n, "dir": False} for n in _proc_entries]), head_only=_ho)
        return await send_r(send, 200, "\n".join(_proc_entries) + "\n",
                            "text/plain", head_only=_ho)
    # /proc/worlds — list of worlds (was: /stages)
    if method == "GET" and path == "/proc/worlds":
        stages = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    name = _logical_name(d.name)
                    r = conn(name).execute("SELECT version,updated_at,state FROM stage_meta WHERE id=1").fetchone()
                    entry = {"name": name, "version": r["version"], "updated_at": r["updated_at"]}
                    # state is /lib-scoped semantics (pending/active/disabled).
                    # Non-lib worlds have the column (default 'pending') but
                    # the field is meaningless outside /lib — don't surface it
                    # as if it were a general world property.
                    if name.startswith("lib/"):
                        entry["state"] = r["state"] or "pending"
                    stages.append(entry)
        return await send_r(send, 200, json.dumps(stages))
    # /proc/uptime, /proc/version, /proc/status — Unix-style introspection
    if method == "GET" and path == "/proc/uptime":
        return await send_r(send, 200, f"{int(time.time() - _BOOT)}\n", "text/plain")
    if method == "GET" and path == "/proc/version":
        return await send_r(send, 200, f"{VERSION}\n", "text/plain")
    if method == "GET" and path == "/proc/status":
        n = sum(1 for d in DATA.iterdir()
                if d.is_dir() and (d / "universe.db").exists()) if DATA.exists() else 0
        return await send_r(send, 200, json.dumps({
            "pid": os.getpid(), "uptime": int(time.time() - _BOOT),
            "worlds": n, "plugins": len(_plugin_meta), "version": VERSION}))

    # /auth/mint — approve-only, issue a path-scoped capability token.
    #   POST /auth/mint?prefix=/home/dreams&ttl=3600&mode=rw
    # Token format: <b64url(prefix|exp|mode)>.<b64url(HMAC-SHA256)>.
    # Verify in _check_auth; path canonicalized before prefix check.
    if method == "POST" and path == "/auth/mint":
        if _check_auth(scope) != "approve":
            return await send_r(send, 403, '{"error":"approve required to mint"}')
        qs = scope.get("query_string", b"").decode()
        p = dict(x.split("=", 1) for x in qs.split("&") if "=" in x) if qs else {}
        prefix = p.get("prefix", "/home")
        mode = p.get("mode", "rw")
        try:
            ttl = int(p.get("ttl", "3600"))
        except ValueError:
            return await send_r(send, 400, '{"error":"ttl must be int seconds"}')
        if mode not in ("r", "rw"):
            return await send_r(send, 400, '{"error":"mode must be r or rw"}')
        try:
            tok = _mint_cap(prefix, ttl, mode)
        except ValueError as e:
            return await send_r(send, 400, json.dumps({"error": str(e)}))
        return await send_r(send, 200, json.dumps({
            "token": tok, "prefix": prefix, "mode": mode,
            "ttl_sec": ttl, "exp": int(time.time()) + ttl,
        }))

    # DELETE /{fhs}/{name} → tiered auth (mirrors PUT), move to .trash, recursive.
    #   /home/* → T2 auth; /etc/* /usr/* /var/* /boot/* → T3 approve.
    #   If `name` is a prefix (has children but isn't itself a world), all
    #   matching children are moved. If `name` IS a world, it plus any children
    #   under it are moved — "rm -rf prefix/" semantics.
    if method == "DELETE" and len(parts) >= 2 and parts[0] in _FHS:
        name = "/".join(parts[1:]) if parts[0] == "home" else "/".join(parts)
        if not _valid_name(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        # Collect targets: the world itself (if exists) + any worlds under name/
        targets = []
        if (DATA / _disk_name(name) / "universe.db").exists():
            targets.append(name)
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    w = _logical_name(d.name)
                    if w.startswith(name + "/"):
                        targets.append(w)
        if not targets:
            return await send_r(send, 404, '{"error":"world not found"}')
        # Auth: T3 if any target is under a system prefix; else T2 is enough.
        # Capability tokens in read-only mode are rejected for writes.
        auth = _check_auth(scope)
        needs_approve = any(t.startswith(("etc/", "usr/", "var/", "boot/", "lib/")) for t in targets)
        if needs_approve and auth != "approve":
            return await send_r(send, 403, '{"error":"system delete requires approve"}')
        if not needs_approve and AUTH_TOKEN and auth is None:
            return await send_r(send, 403, '{"error":"unauthorized"}')
        if isinstance(auth, str) and auth.startswith("cap:r:"):
            return await send_r(send, 403, '{"error":"read-only token"}')
        # Phase 1: DELETE /lib/<name> must unregister routes before
        # trashing the world, otherwise the plugin keeps running with
        # an orphan _plugins dict entry pointing at an exec'd handler
        # that references a now-deleted world.
        for w in targets:
            if w.startswith("lib/"):
                deactivate_lib_world(w[4:])
            _release_world(w)
            _move_to_trash(w)
        return await send_r(send, 200, json.dumps({"deleted": targets}))

    # Trailing slash on FHS paths = ls (list children). Like Unix: cd dir/ vs cat file.
    # GET /home/       → ls all user worlds
    # GET /etc/        → ls config worlds
    # GET /home/photos/→ ls worlds under photos/
    if method in ("GET", "HEAD") and len(parts) >= 1 and parts[0] in _FHS and trailing_slash:
        # Determine world-name prefix for ls
        ho = (method == "HEAD")
        if parts[0] == "home":
            ls_prefix = "/".join(parts[1:])  # "home" → "", "home/photos" → "photos"
            # Only show non-system worlds
            entries = [(n, d) for n, d in _ls(ls_prefix)
                       if n not in ("etc","usr","var","boot")]
        else:
            ls_prefix = "/".join(parts)  # "etc" → "etc", "usr/lib" → "usr/lib"
            entries = _ls(ls_prefix)
        accept = ""
        for k, v in scope.get("headers", []):
            if k == b"accept": accept = v.decode(); break
        if accept.startswith("text/html"):
            return await send_r(send, 200, INDEX, "text/html", csp=True, head_only=ho)
        if "json" in accept:
            return await send_r(send, 200, json.dumps([{"name": n, "dir": d} for n, d in entries]), head_only=ho)
        # plain text — one per line. dirs get trailing /
        lines = [(n + "/" if d else n) for n, d in entries]
        return await send_r(send, 200, "\n".join(lines) + "\n" if lines else "", "text/plain", head_only=ho)

    # /lib/<name>/state — plugin lifecycle transition (Phase 0 of
    # plugin-as-world). Separated from _INTERNAL ops because the
    # semantics differ: this is a PUT on a virtual "state" sub-
    # resource (set state to value), not a POST content-modifying op
    # like /sync. Requires approve (T3) per the /etc/* analogue.
    # This Phase 0 handler records the desired state only; it does
    # NOT exec or register/unregister plugin routes. Route
    # registration at activation is Phase 1.
    if (method == "PUT" and len(parts) == 3 and parts[0] == "lib"
            and parts[2] == "state"):
        if _check_auth(scope) != "approve":
            return await send_r(send, 403, '{"error":"state transition requires approve"}')
        plugin = parts[1]
        if not _valid_name(plugin):
            return await send_r(send, 400, '{"error":"invalid plugin name"}')
        world_name = "lib/" + plugin
        try: body_bytes = await recv(receive)
        except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
        target_state = body_bytes.decode("utf-8", "replace").strip()
        if target_state not in ("active", "disabled"):
            return await send_r(send, 422, json.dumps({
                "error": "invalid state",
                "allowed": ["active", "disabled"]}))
        if not (DATA / _disk_name(world_name) / "universe.db").exists():
            return await send_r(send, 404, '{"error":"plugin not found"}')
        c = conn(world_name)
        r = c.execute("SELECT state,version FROM stage_meta WHERE id=1").fetchone()
        prev_state = (r["state"] if r else "pending") or "pending"
        ver = r["version"] if r else 0
        # Idempotent EXCEPT for the retry case: state is already 'active'
        # but the plugin is not currently loaded (e.g. boot-time exec
        # failed, operator fixed the problem, now wants to retry).
        # Per PLAN guardrail D, boot failures preserve state='active'
        # as operator intent; that only remains useful if PUT state=active
        # can trigger a reload without demanding a disable→active dance.
        is_loaded = any(m["name"] == f"lib:{plugin}" for m in _plugin_meta)
        if prev_state == target_state:
            if target_state == "active" and not is_loaded:
                # Retry activation. Fall through to the exec path below.
                pass
            else:
                return await send_r(send, 200, json.dumps({
                    "state": target_state, "version": ver, "changed": False}))
        # Phase 1: wire state transitions to actual route registration.
        # - activate: exec source + register ROUTES atomically. If exec
        #   fails, refuse the transition; state stays at prev (no UPDATE,
        #   no event) and the plugin remains not-loaded.
        # - disable: unregister routes. Idempotent — safe even if routes
        #   weren't registered (silent no-op inside deactivate_lib_world).
        if target_state == "active":
            ok, err = activate_lib_world(plugin)
            if not ok:
                return await send_r(send, 422, json.dumps({
                    "error": "activation failed", "detail": err,
                    "state": prev_state}))
        elif target_state == "disabled":
            deactivate_lib_world(plugin)
        if prev_state != target_state:
            c.execute("UPDATE stage_meta SET state=?,updated_at=datetime('now') WHERE id=1",
                      (target_state,))
            c.commit()
            # Audit: state_transition is its own event type, separate
            # from stage_written. "Who wrote source" and "who activated
            # it" are independently queryable on the HMAC chain.
            log_event(world_name, "state_transition", {
                "from": prev_state, "to": target_state, "version": ver})
            return await send_r(send, 200, json.dumps({
                "state": target_state, "version": ver, "changed": True,
                "from": prev_state}))
        # Retry case: state column didn't change, but plugin got loaded.
        # Emit a separate event type so the chain shows "reload happened"
        # distinct from a fresh state transition.
        log_event(world_name, "plugin_reloaded", {
            "state": target_state, "version": ver,
            "reason": "active→active retry after boot/load failure"})
        return await send_r(send, 200, json.dumps({
            "state": target_state, "version": ver, "changed": False,
            "reloaded": True}))

    # World routes — HTTP method IS the action.
    #   GET    /home/foo       → read content (no trailing slash)
    #   GET    /home/foo?raw   → raw bytes
    #   PUT    /home/foo       → overwrite
    #   POST   /home/foo       → append
    #   DELETE /home/foo       → handled above
    # If world doesn't exist but children do → 302 redirect to path/
    if len(parts) >= 2 and parts[0] in _FHS:
        # Parse: is last segment an internal op, or part of the world name?
        if len(parts) >= 3 and parts[-1] in _INTERNAL:
            iop = parts[-1]
            name = "/".join(parts[1:-1]) if parts[0] == "home" else parts[0] + "/" + "/".join(parts[1:-1])
        else:
            iop = None
            name = "/".join(parts[1:]) if parts[0] == "home" else "/".join(parts)
        if not _valid_name(name): return await send_r(send, 400, '{"error":"invalid world name"}')
        qs = scope.get("query_string", b"").decode()
        params = _parse_qs(qs)
        # ── Auth gates ──
        if method in ("PUT", "POST"):
            auth = _check_auth(scope)
            if iop:
                # Internal ops (sync/pending/result/clear): need auth OR same-origin.
                # Browser iframe is same-origin (sends Origin header). curl without
                # auth AND without Origin = blocked. Closes the sync bypass.
                if method != "POST":
                    return await send_r(send, 405, '{"error":"method not allowed"}')
                origin = ""
                for k, v in scope.get("headers", []):
                    if k == b"origin": origin = v.decode(); break
                is_local = origin.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))
                has_auth = auth is not None
                if origin and not is_local:
                    return await send_r(send, 403, '{"error":"cross-origin rejected"}')
                if not has_auth and not is_local:
                    return await send_r(send, 403, '{"error":"unauthorized"}')
            elif name.startswith(("etc/", "usr/", "var/", "boot/")):
                if auth != "approve":
                    return await send_r(send, 403, '{"error":"system write requires approve"}')
            elif AUTH_TOKEN and auth is None:
                return await send_r(send, 403, '{"error":"unauthorized"}')
            # Capability tokens in read-only mode cannot write.
            if isinstance(auth, str) and auth.startswith("cap:r:"):
                return await send_r(send, 403, '{"error":"read-only token"}')
        # Sensitive-read gate moved into the GET handler below (after
        # browser detection). Browser navigations always get index.html;
        # the iframe's own fetch hits the gate separately.
        # ── GET/HEAD on internal ops → 405 ──
        if method in ("GET", "HEAD") and iop:
            return await send_r(send, 405, '{"error":"method not allowed"}',
                                head_only=(method == "HEAD"))
        # ── GET/HEAD: read / raw / browser ──
        # HEAD ≡ GET minus body. Status, headers (Content-Length, Content-Type,
        # X-Meta-* replay on ?raw, Accept-Ranges, Content-Range on 206) all
        # identical; body bytes are empty. AI uses HEAD /world?raw as stat().
        # Plain HEAD /world still only mirrors the JSON read surface and does
        # NOT expose X-Meta-* as response headers — that's on ?raw only, same
        # as GET's existing contract.
        if method in ("GET", "HEAD"):
            ho = (method == "HEAD")
            # Content negotiation: browser gets index.html, API gets JSON
            accept = ""
            for k, v in scope.get("headers", []):
                if k == b"accept": accept = v.decode(); break
            is_browser = accept.startswith("text/html")
            # Browser always gets the app shell — auth errors show inside iframe
            if is_browser: return await send_r(send, 200, INDEX, "text/html", csp=True, head_only=ho)
            # Sensitive-read gate (API only — browser handled above)
            if name == "etc/shadow" or name.startswith("boot/"):
                if _check_auth(scope) != "approve":
                    return await send_r(send, 403, '{"error":"read requires approve"}', head_only=ho)
            if not (DATA / _disk_name(name) / "universe.db").exists():
                # World doesn't exist — check if it's a prefix with children → 302 to ls
                children = _ls(name if parts[0] != "home" else "/".join(parts[1:]))
                if children:
                    return await send_r(send, 302, "", extra_headers=[[b"location", (path + "/").encode()]], head_only=ho)
                return await send_r(send, 404, '{"error":"world not found"}', head_only=ho)
            # ?raw → raw bytes with correct Content-Type
            if "raw" in params or "raw" in qs.split("&"):
                c = conn(name)
                r = c.execute("SELECT stage_html,ext,headers FROM stage_meta WHERE id=1").fetchone()
                body = r["stage_html"] or b""
                if isinstance(body, str): body = body.encode("utf-8")
                ext = r["ext"] or "plain"
                ct = _ext_to_ct(ext)
                total = len(body)
                # Replay stored X-Meta-* on both 200 and 206 paths. Re-validates
                # on read so hand-edited DB / dirty imports can't bypass.
                extra_hdrs = _replay_meta_headers(r["headers"])
                range_h = ""
                for k, v in scope.get("headers", []):
                    if k == b"range": range_h = v.decode(); break
                if range_h.startswith("bytes="):
                    rp = range_h[6:].split("-", 1)
                    start = int(rp[0]) if rp[0] else 0
                    end = int(rp[1]) if len(rp) > 1 and rp[1] else total - 1
                    if start >= total: start = total - 1
                    if end >= total: end = total - 1
                    chunk = body[start:end+1]
                    await send({"type":"http.response.start","status":206,"headers":[
                        [b"content-type", ct.encode()],
                        [b"content-range", f"bytes {start}-{end}/{total}".encode()],
                        [b"content-length", str(len(chunk)).encode()],
                        [b"accept-ranges", b"bytes"]] + extra_hdrs})
                    await send({"type":"http.response.body","body": b"" if ho else chunk})
                else:
                    await send({"type":"http.response.start","status":200,"headers":[
                        [b"content-type", ct.encode()],
                        [b"content-length", str(total).encode()],
                        [b"accept-ranges", b"bytes"]] + extra_hdrs})
                    await send({"type":"http.response.body","body": b"" if ho else body})
                return
            if is_browser: return await send_r(send, 200, INDEX, "text/html", csp=True, head_only=ho)
            # JSON read
            c = conn(name)
            r = c.execute("SELECT stage_html,pending_js,js_result,version,ext,headers,state FROM stage_meta WHERE id=1").fetchone()
            cv = params.get("v")
            if cv:
                try:
                    if int(cv) == r["version"]: return await send_r(send, 304, "", head_only=ho)
                except ValueError: pass
            raw = r["stage_html"] or ""
            if isinstance(raw, bytes):
                try: raw = raw.decode("utf-8")
                except UnicodeDecodeError: raw = ""
            ext = r["ext"] or "html"
            # Metadata lives in response headers. The JSON body carries
            # content (stage_html + pending_js + js_result + version +
            # ext + state). There is no "headers" field in the body —
            # that was a transitional duplicate; header is the canonical
            # home for metadata, same as everywhere else in HTTP.
            # `state` is meaningful for /lib/* plugin worlds (pending /
            # active / disabled) and always 'pending' by default for
            # other worlds — semantic scope is /lib/*, but the column
            # exists on every row so the field is always present.
            extra_hdrs = _replay_meta_headers(r["headers"])
            return await send_r(send, 200, json.dumps({
                "stage_html": raw, "pending_js": r["pending_js"] or "",
                "js_result": r["js_result"] or "", "version": r["version"],
                "ext": ext, "type": ext, "state": r["state"] or "pending"}),
                extra_headers=extra_hdrs, head_only=ho)
        # ── PUT: overwrite ──
        if method == "PUT":
            c = conn(name)
            req_ext = params.get("ext")
            try: body_bytes = await recv(receive)
            except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
            if req_ext and req_ext in _BINARY_EXT:
                b = body_bytes
            else:
                b = body_bytes.decode("utf-8", "replace")
                b = _extract(b, "write")
            cur = c.execute("SELECT ext,state FROM stage_meta WHERE id=1").fetchone()
            cur_ext = (cur["ext"] if cur else "plain") or "plain"
            # Plugin-as-world: source-changing PUT on /lib/* invalidates
            # prior approval. If state was 'active' or 'disabled', reset to
            # 'pending' — approval is bound to a specific source version
            # (body_sha256_after in the stage_written event), not to the
            # plugin name. T2 caller cannot silently swap in new code
            # under an existing T3 approval.
            is_lib = name.startswith("lib/")
            prev_state = ((cur["state"] if cur else "pending") or "pending") if is_lib else None
            # Default ext for /lib/* is 'py' (plugin source). Without
            # this, _infer_type sees no '<' tags and falls back to 'plain',
            # which gives ?raw a Content-Type of text/plain on source
            # code instead of text/x-python. Explicit ?ext= still wins.
            if is_lib and not req_ext:
                new_ext = "py"
            else:
                new_ext = req_ext or (_infer_type(b) if isinstance(b, str) else cur_ext)
            if (cur_ext == "html" or new_ext == "html") and _check_auth(scope) != "approve":
                return await send_r(send, 403, '{"error":"html write requires approve"}')
            hdrs = _extract_meta_headers(scope)
            if is_lib and prev_state != "pending":
                c.execute("UPDATE stage_meta SET stage_html=?,ext=?,headers=?,state='pending',version=version+1,updated_at=datetime('now') WHERE id=1",(b, new_ext, hdrs))
            else:
                c.execute("UPDATE stage_meta SET stage_html=?,ext=?,headers=?,version=version+1,updated_at=datetime('now') WHERE id=1",(b, new_ext, hdrs))
            c.commit()
            # Phase 2.5 audit binding: bind metadata to content at event time.
            # Version + body hash + meta_headers go into the HMAC-chained
            # payload so the claim ("codex wrote this, confidence 0.95")
            # cannot be separated from the content it covers.
            # len + sha256 both measured over the UTF-8-encoded bytes — so
            # PUT "你好" (2 codepoints, 6 bytes) and DAV PUT of the same body
            # agree on len=6 and on the hash. Python's len(str) counts code
            # points, which would lie for non-ASCII if we used it directly.
            ver = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
            body_bytes_for_hash = b if isinstance(b, bytes) else b.encode("utf-8")
            body_hash = hashlib.sha256(body_bytes_for_hash).hexdigest()
            log_event(name, "stage_written", {
                "op": "put",
                "len": len(body_bytes_for_hash),
                "ext": new_ext,
                "version_after": ver,
                "meta_headers": json.loads(hdrs or "[]"),
                "body_sha256_after": body_hash,
            })
            # If this was a /lib/* PUT that reset state, record the
            # forced transition on the chain after the stage_written
            # event so "source replaced, approval invalidated" is
            # independently auditable from "source written".
            if is_lib and prev_state and prev_state != "pending":
                log_event(name, "state_transition", {
                    "from": prev_state, "to": "pending",
                    "version": ver, "reason": "source replaced"})
            return await send_r(send, 200, json.dumps({"version": ver, "ext": new_ext, "type": new_ext}))
        # ── POST: append or internal op ──
        if method == "POST":
            c = conn(name)
            try: body_bytes = await recv(receive)
            except ValueError: return await send_r(send, 413, '{"error":"body too large"}')
            b = body_bytes.decode("utf-8", "replace")
            if iop == "sync":
                c.execute("UPDATE stage_meta SET stage_html=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            if iop == "pending":
                c.execute("UPDATE stage_meta SET pending_js=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            if iop == "result":
                c.execute("UPDATE stage_meta SET js_result=?,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            if iop == "clear":
                c.execute("UPDATE stage_meta SET pending_js='',js_result='',updated_at=datetime('now') WHERE id=1"); c.commit()
                return await send_r(send, 200, '{"ok":true}')
            # Append
            req_ext = params.get("ext")
            if req_ext and req_ext in _BINARY_EXT:
                b = body_bytes
            else:
                b = _extract(b, "append")
            cur = c.execute("SELECT ext FROM stage_meta WHERE id=1").fetchone()
            cur_ext = (cur["ext"] if cur else "plain") or "plain"
            if (cur_ext == "html") and _check_auth(scope) != "approve":
                return await send_r(send, 403, '{"error":"html write requires approve"}')
            # Measure the delta over its byte representation — same metric
            # the hash uses — so append_len is unambiguous across text and
            # binary payloads.
            append_bytes_for_hash = b if isinstance(b, bytes) else b.encode("utf-8")
            append_hash = hashlib.sha256(append_bytes_for_hash).hexdigest()
            c.execute("UPDATE stage_meta SET stage_html=stage_html||?,version=version+1,updated_at=datetime('now') WHERE id=1",(b,)); c.commit()
            # Phase 2.5 audit binding: append-level provenance.
            # append_sha256 pins the delta; body_sha256_after pins the resulting
            # full state. meta_headers is intentionally empty — append does not
            # update metadata (Phase 1 contract), and the event makes that visible.
            r2 = c.execute("SELECT stage_html,version FROM stage_meta WHERE id=1").fetchone()
            full = r2["stage_html"]
            if isinstance(full, str): full = full.encode("utf-8")
            full = full or b""
            body_hash = hashlib.sha256(full).hexdigest()
            log_event(name, "stage_appended", {
                "op": "append",
                "append_len": len(append_bytes_for_hash),
                "append_sha256": append_hash,
                "version_after": r2["version"],
                "body_sha256_after": body_hash,
                "meta_headers": [],
            })
            return await send_r(send, 200, json.dumps({"version": r2["version"]}))

    if method == "GET": return await send_r(send, 200, INDEX, "text/html", csp=True)
    await send_r(send, 404, '{"error":"not found"}')

# ── Mini ASGI server — zero dependencies ─────────────────────────────

_REASONS = {200:"OK",201:"Created",204:"No Content",206:"Partial Content",
            301:"Moved Permanently",302:"Found",304:"Not Modified",
            400:"Bad Request",401:"Unauthorized",403:"Forbidden",404:"Not Found",
            405:"Method Not Allowed",413:"Payload Too Large",
            500:"Internal Server Error"}

async def _mini_serve(asgi_app, host, port):
    """Zero-dependency ASGI server. Enough for single-user localhost.
    Supports streaming: if a response lacks Content-Length, automatically
    uses Transfer-Encoding: chunked so SSE works without uvicorn."""
    async def handle(reader, writer):
        chunked = False  # set per-response in _send
        try:
            line = await reader.readline()
            if not line: writer.close(); return
            parts = line.decode("utf-8", "replace").strip().split(" ", 2)
            if len(parts) < 2: writer.close(); return
            method, full_path = parts[0], parts[1]
            headers = []
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""): break
                decoded = h.decode("utf-8", "replace").strip()
                if ": " in decoded:
                    k, v = decoded.split(": ", 1)
                    headers.append([k.lower().encode(), v.encode()])
            content_length = 0
            for k, v in headers:
                if k == b"content-length": content_length = int(v); break
            body = await reader.readexactly(content_length) if content_length else b""
            path_part = full_path.split("?")[0]
            qs = full_path.split("?", 1)[1] if "?" in full_path else ""
            scope = {
                "type": "http", "method": method, "path": path_part,
                "raw_path": path_part.encode(), "query_string": qs.encode(),
                "headers": headers,
            }
            async def _recv():
                return {"type": "http.request", "body": body}
            async def _send(msg):
                nonlocal chunked
                if msg["type"] == "http.response.start":
                    status = msg["status"]
                    reason = _REASONS.get(status, "OK")
                    hdrs = msg.get("headers", [])
                    has_cl = any(k.lower() == b"content-length" for k, v in hdrs)
                    has_te = any(k.lower() == b"transfer-encoding" for k, v in hdrs)
                    # No length and no transfer-encoding → use chunked so clients can frame.
                    chunked = not has_cl and not has_te
                    out = f"HTTP/1.1 {status} {reason}\r\n".encode()
                    for k, v in hdrs:
                        # Strip Connection: keep-alive — we close after one request.
                        if k.lower() == b"connection" and v.lower() == b"keep-alive":
                            continue
                        out += k + b": " + v + b"\r\n"
                    if chunked: out += b"Transfer-Encoding: chunked\r\n"
                    out += b"Connection: close\r\n\r\n"
                    writer.write(out)
                    await writer.drain()
                elif msg["type"] == "http.response.body":
                    chunk = msg.get("body", b"")
                    more = msg.get("more_body", False)
                    if chunked:
                        if chunk:
                            writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        if not more:
                            writer.write(b"0\r\n\r\n")  # end-of-chunked marker
                    elif chunk:
                        writer.write(chunk)
                    await writer.drain()
            await asgi_app(scope, _recv, _send)
        except Exception:
            import traceback; traceback.print_exc()
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except OSError: pass
        finally:
            try: writer.close()
            except OSError: pass
    srv = await asyncio.start_server(handle, host, port)
    await srv.serve_forever()

def run(extra_tasks=None):
    """Start the server. extra_tasks: list of coroutines to run alongside."""
    tasks = extra_tasks or []
    try:
        import uvicorn
        async def _serve():
            # http="h11" forces uvicorn's pure-Python HTTP parser, avoiding
            # httptools.parse_url()'s uint16 URL-field wrap (MagicStack/
            # httptools#142). h11 has looser grammar in places but none of
            # elastik's surfaces consume the semantics those attacks need
            # (no Host routing, no http_version branching, no front proxy).
            config = uvicorn.Config(app, host=HOST, port=PORT,
                                    log_level="warning", http="h11")
            server = uvicorn.Server(config)
            await asyncio.gather(server.serve(), *tasks)
        asyncio.run(_serve())
    except ImportError:
        print("  (uvicorn not found -- using built-in server)")
        async def _serve():
            await asyncio.gather(_mini_serve(app, HOST, PORT), *tasks)
        asyncio.run(_serve())

# ──────────────────────────────────────────────────────────────────────
# Plugin subsystem — Tier 1 (/lib/*) only, v4.5.0 microkernel cut.
# World loader (load_plugin_from_source), activation lifecycle
# (activate_lib_world / deactivate_lib_world), boot loader
# (boot_load_active_lib), cron scheduler (cron_loop), and the
# AI plugin-propose/approve flow (rewired to /lib/*).
#
# Disk-based Tier 0 loading was removed: no more load_plugin(),
# load_plugins(), plugins.lock, _verify_plugin, _find_lib_disk_collisions,
# plugins/available/ scan, plugins/ auto-install. The runtime reads
# /lib/<name> worlds from DATA and nothing from the filesystem.
# ──────────────────────────────────────────────────────────────────────

_DANGEROUS_PLUGINS = {"exec", "fs"}
_cron_tasks = {}   # name → {interval, handler, last_run}
_start_time = time.time()

# Mode system — environment detection × user intent.
# Environment ceiling: container=2, bare metal=1. User cannot exceed it.
IN_CONTAINER = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv") or os.getenv("CONTAINER") == "1"
_ENV_CEILING = 2 if IN_CONTAINER else 1
_USER_MODE = int(os.getenv("ELASTIK_MODE", "0"))  # 0 = auto
MODE = min(_USER_MODE, _ENV_CEILING) if _USER_MODE else _ENV_CEILING
# MODE 1: executor  — read/write worlds, use plugins. admin/config/dangerous locked.
# MODE 2: autonomous — approve token unlocks admin/config. dangerous plugins allowed.


def unload_plugin(name):
    """Unload a plugin — remove its routes. Works for both Tier 0
    (bare names) and Tier 1 (`lib:<name>` prefix)."""
    global _auth
    meta = next((m for m in _plugin_meta if m["name"] == name), None)
    if not meta: print(f"  not loaded: {name}"); return
    for r in meta["routes"]:
        _plugins.pop(r, None)
        _plugin_auth.pop(r, None)
    if name == "auth" or "auth" in meta.get("description", "").lower(): _auth = None
    _sync_actions_remove(name, meta["routes"])
    # Auto-clear skills world (only for Tier 0 — Tier 1 plugins use
    # a different name scheme "lib:<basename>" and don't emit skills).
    if not name.startswith("lib:"):
        skill_world = f"usr/lib/skills/{name.replace('_', '-')}"
        try:
            if (DATA / _disk_name(skill_world)).exists():
                c = conn(skill_world)
                c.execute("UPDATE stage_meta SET stage_html='',version=version+1,updated_at=datetime('now') WHERE id=1")
                c.commit()
                print(f"  skill cleared: {skill_world}")
        except Exception as e: print(f"  warn: skill cleanup failed for {skill_world}: {e}")
    _cron_tasks.pop(name, None)
    _plugin_meta[:] = [m for m in _plugin_meta if m["name"] != name]
    print(f"  unloaded: {name}")


def load_plugin_from_source(plugin_name, source):
    """Tier 1 plugin loader: exec source from a /lib/<plugin_name> world,
    register declared ROUTES, track in _plugin_meta under 'lib:<name>'.
    Returns (True, None) or (False, error_str). AUTH_MIDDLEWARE from
    Tier 1 is silently ignored — privileged hook stays Tier 0."""
    if not _valid_name(plugin_name):
        return False, f"invalid plugin name: {plugin_name}"
    if plugin_name in _DANGEROUS_PLUGINS and MODE < 2:
        return False, f"{plugin_name} blocked — mode {MODE} requires mode 2 (container)"
    if any(m["name"] == plugin_name for m in _plugin_meta):
        return False, f"name collision: '{plugin_name}' is already loaded as a Tier 0 plugin"
    meta_name = f"lib:{plugin_name}"

    async def _call(route, method="POST", body=b"", params=None):
        h = _plugins.get(route)
        if not h: return {"error": f"route {route} not found"}
        return await h(method, body, params or {})
    _injectable = {
        "unload_plugin": unload_plugin,
        "_plugins": _plugins, "_plugin_meta": _plugin_meta,
        "_cron_tasks": _cron_tasks, "_start_time": _start_time,
    }
    ns = {"__file__": f"<lib/{plugin_name}>", "_ROOT": Path(__file__).resolve().parent,
          "conn": conn, "log_event": log_event, "_call": _call}
    needs_match = re.search(r'NEEDS\s*=\s*\[([^\]]*)\]', source)
    if needs_match:
        needed = [s.strip().strip('"').strip("'") for s in needs_match.group(1).split(",") if s.strip()]
        ns.update({k: _injectable[k] for k in needed if k in _injectable})
    try:
        exec(source, ns)
    except Exception as e:
        return False, f"exec failed: {type(e).__name__}: {e}"
    raw_routes = ns.get("ROUTES", {})
    declared = []
    handle_fn = ns.get("handle")
    if isinstance(raw_routes, list):
        if not handle_fn:
            return False, "ROUTES is a list but no handle() function defined"
        declared = [(r, handle_fn) for r in raw_routes]
    elif isinstance(raw_routes, dict):
        declared = list(raw_routes.items())
    else:
        return False, f"ROUTES must be a list or dict, got {type(raw_routes).__name__}"
    prior = next((m for m in _plugin_meta if m["name"] == meta_name), None)
    own_routes = set(prior["routes"]) if prior else set()
    for route, _h in declared:
        if route in _plugins and route not in own_routes:
            return False, f"route conflict: '{route}' is already registered"
    if prior:
        for r in prior["routes"]:
            _plugins.pop(r, None)
            _plugin_auth.pop(r, None)
        _plugin_meta[:] = [m for m in _plugin_meta if m["name"] != meta_name]
    auth_level = ns.get("AUTH", "none")
    routes = []
    for route, h in declared:
        _plugins[route] = h
        _plugin_auth[route] = auth_level
        routes.append(route)
    _plugin_meta.append({
        "name": meta_name, "description": ns.get("DESCRIPTION", ""),
        "routes": routes, "params": ns.get("PARAMS_SCHEMA", {}),
        "ops": ns.get("OPS_SCHEMA", []),
    })
    if "CRON" in ns and "CRON_HANDLER" in ns:
        _cron_tasks[meta_name] = {
            "interval": int(ns["CRON"]),
            "handler": ns["CRON_HANDLER"],
            "last_run": time.time(),
        }
    return True, None


def activate_lib_world(plugin_name):
    """Wire PUT /lib/<name>/state=active to exec + registration."""
    world_name = f"lib/{plugin_name}"
    db_path = DATA / _disk_name(world_name) / "universe.db"
    if not db_path.exists():
        return False, "plugin world not found"
    c = conn(world_name)
    r = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    source = r["stage_html"] if r else None
    if isinstance(source, bytes):
        source = source.decode("utf-8", "replace")
    if not source or not source.strip():
        return False, "plugin source is empty"
    return load_plugin_from_source(plugin_name, source)


def deactivate_lib_world(plugin_name):
    """Wire PUT /lib/<name>/state=disabled (and DELETE /lib/<name> if
    active) to route unregistration. Idempotent — silently no-op if
    the plugin wasn't loaded."""
    unload_plugin(f"lib:{plugin_name}")


def boot_load_active_lib():
    """Boot-time loader: iterate data/ for lib/<name> worlds with
    state='active' and exec each. Per PLAN guardrail D, exec failures
    log and skip — they do NOT auto-disable."""
    if not DATA.exists(): return
    loaded, failed = 0, 0
    for d in sorted(DATA.iterdir()):
        if not (d.is_dir() and (d / "universe.db").exists()): continue
        try: name = _logical_name(d.name)
        except Exception: continue
        if not name.startswith("lib/"): continue
        plugin_name = name[4:]
        try:
            c = conn(name)
            row = c.execute("SELECT state,stage_html FROM stage_meta WHERE id=1").fetchone()
        except Exception as e:
            print(f"  lib: {plugin_name}: read failed — {e}")
            continue
        if not row: continue
        state = (row["state"] or "pending") if row else "pending"
        if state != "active": continue
        source = row["stage_html"]
        if isinstance(source, bytes):
            source = source.decode("utf-8", "replace")
        if not source or not source.strip():
            print(f"  lib: {plugin_name}: state=active but source empty; skipping")
            failed += 1
            continue
        ok, err = load_plugin_from_source(plugin_name, source)
        if ok:
            loaded += 1
            print(f"  lib: loaded {plugin_name}")
            try: log_event(name, "plugin_activated_on_boot", {"source_len": len(source)})
            except Exception: pass
        else:
            failed += 1
            print(f"  lib: {plugin_name}: LOAD FAILED — {err} (state stays active)")
            try: log_event(name, "plugin_load_failed", {"error": err})
            except Exception: pass
    if loaded or failed:
        print(f"  lib: {loaded} loaded, {failed} failed")


def _sync_actions_remove(name, routes):
    """Remove a plugin's routes from etc/actions whitelist."""
    if not routes: return
    try:
        c = conn("etc/actions")
        old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
        remove = set(routes)
        lines = [l for l in old.splitlines() if l.strip() and l.strip() not in remove]
        c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
                  ("\n".join(lines) + "\n" if lines else "",))
        c.commit()
    except Exception as e: print(f"  warn: actions cleanup failed: {e}")


async def handle_propose(method, body, params):
    """POST /plugins/propose — submit a plugin proposal (AI → operator review).
    Appends the proposal to the plugin-proposals world. Operator then
    reviews and, if approved, POSTs to /plugins/approve which PUTs the
    source into /lib/<name> and activates it."""
    try: b = json.loads(body)
    except (json.JSONDecodeError, TypeError): return {"error": "invalid json", "_status": 400}
    log_event("default", "plugin_proposed", b)
    name = b.get("name", "unknown")
    desc = b.get("description", "")
    code = b.get("code", "")
    summary = f"\n---\n## {name}\n{desc}\n```python\n{code}\n```\n"
    c = conn("plugin-proposals")
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()["stage_html"]
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (old + summary,))
    c.commit()
    return {"ok": True}


async def handle_approve(method, body, params):
    """POST /plugins/approve — approve and install a plugin as /lib/<name>
    (requires approve). Mirrors the canonical /lib/* surface end-to-end:

      1. Source write: same UPDATE + state-reset + stage_written event
         (with body_sha256_after) and forced state_transition (if prior
         state != 'pending') that PUT /lib/<name> emits on its own.
      2. Activation: activate_lib_world (shared with PUT /lib/<name>/state)
         + state flip to 'active' + state_transition event identical in
         shape to the one emitted by PUT /lib/<name>/state=active.

    Post-condition: the event-chain provenance of a plugin installed via
    /plugins/approve is indistinguishable from one installed via
    PUT /lib/<name> + PUT /lib/<name>/state=active. Codex P1 2026-04-21.

    Rewired for the v4.5.0 microkernel cut: no disk writes remain; /lib/*
    is the only loader."""
    try: b = json.loads(body)
    except (json.JSONDecodeError, TypeError): return {"error": "invalid json", "_status": 400}
    scope = params.get("_scope", {})
    if _check_auth(scope) != "approve":
        return {"error": "unauthorized", "_status": 403}
    n, code = b.get("name", ""), b.get("code", "")
    if n and not _valid_name(n):
        return {"error": "invalid plugin name", "_status": 400}
    if n and code:
        world_name = f"lib/{n}"
        c = conn(world_name)
        cur = c.execute("SELECT state FROM stage_meta WHERE id=1").fetchone()
        prev_state = ((cur["state"] if cur else "pending") or "pending")
        code_bytes = code.encode("utf-8") if isinstance(code, str) else code
        # Source write — /lib/* PUT semantics: reset state to pending, bump
        # version, default ext='py'. Approval-binding invariant stays intact:
        # a source-changing write on an already-approved world sinks it back
        # to pending so the re-approval below re-binds to the new source hash.
        c.execute(
            "UPDATE stage_meta SET stage_html=?,ext='py',headers=NULL,"
            "state='pending',version=version+1,"
            "updated_at=datetime('now') WHERE id=1",
            (code,),
        )
        c.commit()
        ver = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
        body_hash = hashlib.sha256(code_bytes).hexdigest()
        log_event(world_name, "stage_written", {
            "op": "put",
            "len": len(code_bytes),
            "ext": "py",
            "version_after": ver,
            "meta_headers": [],
            "body_sha256_after": body_hash,
        })
        if prev_state != "pending":
            log_event(world_name, "state_transition", {
                "from": prev_state, "to": "pending",
                "version": ver, "reason": "source replaced"})
        # Activation — /lib/<name>/state=active semantics: exec + register
        # routes, flip state, emit state_transition. Exec failure leaves
        # state='pending' and refuses activation (mirrors PUT state).
        ok, err = activate_lib_world(n)
        if not ok:
            log_event(world_name, "plugin_approve_failed", {"name": n, "error": err})
            return {"error": f"activation failed: {err}", "_status": 500}
        c.execute("UPDATE stage_meta SET state='active',"
                  "updated_at=datetime('now') WHERE id=1")
        c.commit()
        log_event(world_name, "state_transition", {
            "from": "pending", "to": "active", "version": ver})
        log_event("default", "plugin_approved", {"name": n})
    return {"ok": True}


# ── Core route: /stream (SSE) — inlined v4.5.0, formerly Tier 0 plugin.
# Push on write, no client polling. Opens EventSource, receives events
# only when the world's version/pending_js/js_result changes.
_SSE_POLL = 0.02      # internal DB poll (20ms → up to 50 events/sec)
_SSE_HB_EVERY = 25    # heartbeat every 25 polls (0.5s) — keeps edge buffers flushing.

async def _core_sse_handle(method, body, params):
    """Server-sent events. GET /stream/{name} → text/event-stream.
    Emits event: update when world state changes; comment heartbeat every 0.5s.
    """
    send = params.get("_send")
    scope = params.get("_scope", {})
    if not send:
        return {"error": "server does not expose raw send; SSE unavailable", "_status": 500}
    if method != "GET":
        return {"error": "SSE is GET only", "_status": 405}
    path = scope.get("path", "").rstrip("/")
    if not path.startswith("/stream/") or path == "/stream":
        return {"error": "path must be /stream/{name}", "_status": 400}
    name = path[len("/stream/"):]
    if not _valid_name(name):
        return {"error": "invalid world name", "_status": 400}
    if not (DATA / _disk_name(name) / "universe.db").exists():
        return {"error": "world not found", "_status": 404}
    c = conn(name)
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"text/event-stream; charset=utf-8"],
            [b"cache-control", b"no-cache"],
            [b"connection", b"keep-alive"],
            [b"x-accel-buffering", b"no"],
        ],
    })
    def _snapshot():
        r = c.execute(
            "SELECT stage_html,pending_js,js_result,version,ext FROM stage_meta WHERE id=1"
        ).fetchone()
        raw = r["stage_html"] or ""
        if isinstance(raw, bytes):
            try: raw = raw.decode("utf-8")
            except UnicodeDecodeError: raw = ""
        ext = r["ext"] or "html"
        pj = r["pending_js"] or ""
        jr = r["js_result"] or ""
        sig = (r["version"], pj, jr)
        payload = json.dumps({
            "version": r["version"], "stage_html": raw,
            "pending_js": pj, "js_result": jr,
            "ext": ext, "type": ext,
        }, ensure_ascii=False)
        return sig, payload
    last_sig = None
    ticks = 0
    try:
        while True:
            sig, data = _snapshot()
            if sig != last_sig:
                msg = f"event: update\ndata: {data}\n\n".encode("utf-8")
                await send({"type": "http.response.body", "body": msg, "more_body": True})
                last_sig = sig
                ticks = 0
            else:
                ticks += 1
                if ticks >= _SSE_HB_EVERY:
                    await send({"type": "http.response.body", "body": b": hb\n\n", "more_body": True})
                    ticks = 0
            await asyncio.sleep(_SSE_POLL)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        try: await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception: pass
    return None


# ── Core route: /dav (WebDAV) — inlined v4.5.0, formerly Tier 0 plugin.
# FHS tree over worlds. Mount it, cd home. PROPFIND/GET/PUT/DELETE/
# MOVE/COPY/MKCOL. Reads are public (like core routes); writes require
# auth; system prefix writes require approve.
#
# Two distinct prefix sets — originally conflated into one, split per
# Codex review 2026-04-21:
#
#   _DAV_SYS_PREFIXES      Auth-elevated. Writes require T3 (approve).
#                          lib/ NOT here — core PUT /lib/<n> accepts T2,
#                          DAV must match (state-reset via DAV PUT is
#                          enforced separately; see 132e718).
#
#   _DAV_TOP_NAMESPACES    Top-level namespaces surfaced as /dav/<ns>/
#                          collections in root PROPFIND + HTML listings
#                          AND excluded from /dav/home/'s "user content"
#                          set. lib/ IS here so plugin worlds appear at
#                          /dav/lib/ and don't double-alias as
#                          /dav/home/lib/<n>.
_DAV_SYS_PREFIXES = ("etc/", "usr/", "var/", "boot/", "tmp/", "mnt/")
_DAV_TOP_NAMESPACES = _DAV_SYS_PREFIXES + ("lib/",)

def _dav_suffix(rest, ext):
    """Render-time ext decoration for PROPFIND hrefs.
    Dotted or dir/empty → ''. html → '.html.txt' (file:// safety).
    plain → '.txt'. Everything else → '.{ext}'.
    """
    last = rest.rsplit("/", 1)[-1]
    if "." in last: return ""
    if not ext or ext == "dir": return ""
    if ext == "html": return ".html.txt"
    if ext == "plain": return ".txt"
    return f".{ext}"

def _dav_world_name(path):
    """DAV URL → world name. Identity first, strip-and-retry fallback.
    Strip-retry handles PROPFIND-decorated hrefs (.html.txt etc.)."""
    rest = path[4:].lstrip("/").rstrip("/")
    if rest.startswith("home/"): rest = rest[5:]
    elif rest == "home": rest = ""
    if not rest: return ""
    candidate = rest
    for _ in range(3):
        if (DATA / _disk_name(candidate) / "universe.db").exists():
            return candidate
        segs = candidate.split("/")
        last = segs[-1]
        dot = last.rfind(".")
        if dot <= 0:
            break
        candidate = "/".join(segs[:-1] + [last[:dot]])
    return rest

def _dav_read(name):
    c = conn(name)
    r = c.execute("SELECT stage_html,ext FROM stage_meta WHERE id=1").fetchone()
    raw = r["stage_html"] if r and r["stage_html"] else b""
    if isinstance(raw, str): raw = raw.encode("utf-8")
    ext = (r["ext"] if r else "html") or "html"
    return raw, ext

def _dav_prop(href, restype, ct, size, mod):
    rt = "<D:resourcetype><D:collection/></D:resourcetype>" if restype == "collection" else "<D:resourcetype/>"
    ctl = f"<D:getcontenttype>{ct}</D:getcontenttype>" if ct else ""
    return (f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
            f"{rt}<D:getcontentlength>{size}</D:getcontentlength>"
            f"<D:getlastmodified>{mod}</D:getlastmodified>"
            f"{ctl}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


async def _core_dav_handle(method, body, params):
    """/dav/ → FHS WebDAV surface. PROPFIND/GET/PUT/DELETE over worlds."""
    scope = params.get("_scope", {})
    path = scope.get("path", "/dav")
    now = formatdate(usegmt=True)

    if method == "OPTIONS":
        return {"_body":"", "_ct":"text/plain",
                "_headers":[["dav","1"],["allow","OPTIONS, GET, HEAD, PUT, DELETE, MOVE, COPY, PROPFIND, MKCOL"]]}

    raw_rest = path[4:].strip("/")
    if raw_rest == "home": dav_prefix = "home"
    elif raw_rest.startswith("home/"): dav_prefix = raw_rest
    else: dav_prefix = raw_rest

    def _write_auth_ok():
        return _check_auth(scope) is not None

    if method == "PROPFIND":
        depth = "1"
        for k, v in scope.get("headers", []):
            if k == b"depth": depth = v.decode(); break
        all_worlds = []
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    all_worlds.append(_logical_name(d.name))
        world = _dav_world_name(path)
        if world and world.endswith("/"): world = world[:-1]
        is_world = world and any(w == world for w in all_worlds)
        has_children = bool(world) and any(w.startswith(world + "/") for w in all_worlds)
        is_top_ns = (dav_prefix in ("", "home") or dav_prefix in (p.rstrip("/") for p in _DAV_TOP_NAMESPACES))
        if world and not is_world and not has_children and not is_top_ns:
            return {"error":"not found", "_status":404}
        if is_world:
            raw, ext = _dav_read(world)
            is_dir = ext == "dir" or has_children
            if not is_dir:
                dav_href = f"/dav/home/{world}" if not world.startswith(_DAV_TOP_NAMESPACES) else f"/dav/{world}"
                xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
                       + _dav_prop(f"{dav_href}{_dav_suffix(world, ext)}", "", _ext_to_ct(ext), len(raw), now)
                       + '</D:multistatus>')
                return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}
        href = f"/dav/{dav_prefix}/" if dav_prefix else "/dav/"
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
               + _dav_prop(href, "collection", "", 0, now))
        if depth == "1":
            if dav_prefix == "":
                has_user = any(not w.startswith(_DAV_TOP_NAMESPACES) for w in all_worlds)
                if has_user:
                    xml += _dav_prop("/dav/home/", "collection", "", 0, now)
                for pref in _DAV_TOP_NAMESPACES:
                    if any(w.startswith(pref) for w in all_worlds):
                        xml += _dav_prop(f"/dav/{pref}", "collection", "", 0, now)
            else:
                if dav_prefix == "home":
                    world_prefix = ""
                    children_href = "/dav/home/"
                    candidates = [w for w in all_worlds if not w.startswith(_DAV_TOP_NAMESPACES)]
                elif dav_prefix.startswith("home/"):
                    world_prefix = dav_prefix[5:] + "/"
                    children_href = f"/dav/{dav_prefix}/"
                    candidates = [w for w in all_worlds
                                  if not w.startswith(_DAV_TOP_NAMESPACES) and w.startswith(world_prefix)]
                else:
                    world_prefix = dav_prefix + "/"
                    children_href = f"/dav/{dav_prefix}/"
                    candidates = [w for w in all_worlds if w.startswith(world_prefix)]
                seen_dirs = set()
                for w in candidates:
                    rest = w[len(world_prefix):] if world_prefix else w
                    if "/" in rest:
                        subdir = rest.split("/")[0]
                        if subdir not in seen_dirs:
                            seen_dirs.add(subdir)
                            xml += _dav_prop(f"{children_href}{subdir}/", "collection", "", 0, now)
                    else:
                        raw, ext = _dav_read(w)
                        if ext == "dir":
                            if rest not in seen_dirs:
                                seen_dirs.add(rest)
                                xml += _dav_prop(f"{children_href}{rest}/", "collection", "", 0, now)
                        else:
                            xml += _dav_prop(f"{children_href}{rest}{_dav_suffix(rest, ext)}", "", _ext_to_ct(ext), len(raw), now)
        xml += '</D:multistatus>'
        return {"_body":xml, "_ct":"application/xml; charset=utf-8", "_status":207}

    if method in ("GET", "HEAD"):
        name = _dav_world_name(path)
        if not name:
            listing = (f'<h1>elastik WebDAV — /{dav_prefix or ""}</h1>'
                    '<p style="background:#fee;padding:.5em;border:1px solid #c00">'
                    'AI-generated content -- treat all links as hostile.</p><ul>')
            if DATA.exists():
                all_worlds = [_logical_name(d.name) for d in sorted(DATA.iterdir())
                              if d.is_dir() and (d / "universe.db").exists()]
                if dav_prefix == "":
                    if any(not w.startswith(_DAV_TOP_NAMESPACES) for w in all_worlds):
                        listing += '<li><a href="/dav/home/">home/</a></li>'
                    for pref in _DAV_TOP_NAMESPACES:
                        if any(w.startswith(pref) for w in all_worlds):
                            listing += f'<li><a href="/dav/{pref}">{pref}</a></li>'
                else:
                    if dav_prefix == "home":
                        world_prefix = ""
                        href_base = "/dav/home/"
                        cands = [w for w in all_worlds if not w.startswith(_DAV_TOP_NAMESPACES)]
                    elif dav_prefix.startswith("home/"):
                        world_prefix = dav_prefix[5:] + "/"
                        href_base = f"/dav/{dav_prefix}/"
                        cands = [w for w in all_worlds
                                 if not w.startswith(_DAV_TOP_NAMESPACES) and w.startswith(world_prefix)]
                    else:
                        world_prefix = dav_prefix + "/"
                        href_base = f"/dav/{dav_prefix}/"
                        cands = [w for w in all_worlds if w.startswith(world_prefix)]
                    seen = set()
                    for w in cands:
                        rest = w[len(world_prefix):] if world_prefix else w
                        if "/" in rest:
                            first = rest.split("/")[0]
                            if first not in seen:
                                seen.add(first)
                                listing += f'<li><a href="{href_base}{first}/">{first}/</a></li>'
                        else:
                            _, ext = _dav_read(w)
                            listing += f'<li><a href="{href_base}{rest}{_dav_suffix(rest, ext)}">{rest}</a> <em>({ext})</em></li>'
            listing += "</ul>"
            return {"_html": listing}
        if not _valid_name(name) or not (DATA / _disk_name(name) / "universe.db").exists():
            return {"error":"not found", "_status":404}
        if (name == "etc/shadow" or name.startswith("boot/")) and _check_auth(scope) != "approve":
            return {"error":"read requires approve", "_status":403}
        raw, ext = _dav_read(name)
        return {"_body":raw, "_ct":_ext_to_ct(ext)}

    if method in ("PUT", "DELETE", "MOVE", "COPY", "MKCOL") and not _write_auth_ok():
        return {"error":"authentication required", "_status":401,
                "_headers":[["www-authenticate",'Basic realm="elastik"']]}

    if method == "PUT":
        name = _dav_world_name(path)
        if not name: return {"error":"PUT on collection not supported", "_status":405}
        if not _valid_name(name): return {"error":"invalid world name", "_status":400}
        raw = params.get("_body_raw", body.encode("utf-8") if isinstance(body, str) else body or b"")
        ext = params.get("ext")
        if not ext:
            ct = ""
            for k, v in scope.get("headers", []):
                if k == b"content-type":
                    ct = v.decode("utf-8", "replace").split(";")[0].strip().lower()
                    break
            ct_to_ext = {v: k for k, v in _CT.items()}
            ext = ct_to_ext.get(ct, "")
        if not ext:
            last_seg = path.rstrip("/").rsplit("/", 1)[-1]
            dot = last_seg.rfind(".")
            if dot > 0:
                maybe_ext = last_seg[dot+1:].lower()
                if maybe_ext in _CT:
                    ext = maybe_ext
        if not ext: ext = "plain"
        if ext == "html" and _check_auth(scope) != "approve":
            return {"error": "html write requires approve", "_status": 403}
        if name.startswith(_DAV_SYS_PREFIXES) and _check_auth(scope) != "approve":
            return {"error": "system write requires approve", "_status": 403}
        c = conn(name)
        meta = _extract_meta_headers(scope)
        # Phase 0/1 P2: source-changing PUT on /lib/* invalidates approval.
        # Mirror the core FHS PUT handler — if state was active/disabled,
        # reset to pending so re-approval re-binds to the new source hash.
        # Without this, DAV writes silently swap code under an existing
        # T3 approval and boot_load_active_lib would exec the new code on
        # next restart without fresh operator sign-off.
        is_lib = name.startswith("lib/")
        cur = c.execute("SELECT state FROM stage_meta WHERE id=1").fetchone()
        prev_state = ((cur["state"] if cur else "pending") or "pending") if is_lib else None
        if is_lib and prev_state != "pending":
            c.execute("UPDATE stage_meta SET stage_html=?,ext=?,headers=?,state='pending',version=version+1,updated_at=datetime('now') WHERE id=1", (raw, ext, meta))
        else:
            c.execute("UPDATE stage_meta SET stage_html=?,ext=?,headers=?,version=version+1,updated_at=datetime('now') WHERE id=1", (raw, ext, meta))
        c.commit()
        ver = c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]
        body_hash = hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode("utf-8")).hexdigest()
        log_event(name, "stage_written", {
            "op": "put",
            "len": len(raw),
            "ext": ext,
            "version_after": ver,
            "meta_headers": json.loads(meta or "[]"),
            "body_sha256_after": body_hash,
        })
        # Forced state-reset audit event — matches core PUT handler.
        if is_lib and prev_state and prev_state != "pending":
            log_event(name, "state_transition", {
                "from": prev_state, "to": "pending",
                "version": ver, "reason": "source replaced"})
        return {"_status":201, "_body":"", "_ct":"text/plain"}

    if method == "DELETE":
        name = _dav_world_name(path)
        if not name: return {"error":"DELETE requires a name", "_status":405}
        if not _valid_name(name): return {"error":"invalid world name", "_status":400}
        targets = []
        if (DATA / _disk_name(name) / "universe.db").exists():
            targets.append(name)
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    w = _logical_name(d.name)
                    if w.startswith(name + "/"):
                        targets.append(w)
        if not targets:
            return {"error":"not found", "_status":404}
        # DELETE on /lib/* requires T3 — matches core DELETE /lib/<n> which
        # already includes lib/ in its elevated-auth tuple (server.py:915).
        # _DAV_TOP_NAMESPACES = _DAV_SYS_PREFIXES + ("lib/",); reused here so
        # T2 can't trash T3-approved plugins via DAV. PUT/MOVE/COPY keep
        # _DAV_SYS_PREFIXES so DAV writes to /lib/<n> stay T2 (matching core
        # PUT /lib/<n>) — MOVE/COPY into /lib/* are neutralized via state
        # reset below, not via auth elevation.
        needs_approve = any(t.startswith(_DAV_TOP_NAMESPACES) for t in targets)
        if needs_approve and _check_auth(scope) != "approve":
            return {"error":"system delete requires approve", "_status":403}
        for w in targets:
            _release_world(w)
            _move_to_trash(w)
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "MOVE":
        src_name = _dav_world_name(path)
        if not src_name:
            return {"error":"MOVE requires a source name", "_status":405}
        if not _valid_name(src_name):
            return {"error":"invalid source name", "_status":400}
        src_disk = DATA / _disk_name(src_name)
        if not (src_disk / "universe.db").exists():
            return {"error":"source not found", "_status":404}
        dest_raw, overwrite = "", True
        for k, v in scope.get("headers", []):
            if k == b"destination": dest_raw = v.decode("utf-8", "replace")
            elif k == b"overwrite": overwrite = v.decode().strip().upper() == "T"
        if not dest_raw:
            return {"error":"Destination header required", "_status":400}
        from urllib.parse import urlparse, unquote
        dest_path = unquote(urlparse(dest_raw).path or dest_raw)
        dst_name = _dav_world_name(dest_path)
        if not dst_name or not _valid_name(dst_name):
            return {"error":"invalid destination", "_status":400}
        if (src_name.startswith(_DAV_SYS_PREFIXES) or dst_name.startswith(_DAV_SYS_PREFIXES)) \
           and _check_auth(scope) != "approve":
            return {"error":"system move requires approve", "_status":403}
        dst_disk = DATA / _disk_name(dst_name)
        if dst_disk.exists():
            if not overwrite:
                return {"error":"destination exists", "_status":412}
            shutil.rmtree(dst_disk)
        _release_world(src_name)
        _release_world(dst_name)
        dst_disk.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(5):
            try:
                src_disk.rename(dst_disk)
                break
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
        else:
            shutil.move(str(src_disk), str(dst_disk))
        # Approval-binding: a MOVE landing inside /lib/* carries the source
        # world's state column over with the rename. If that state was
        # active/disabled, the destination would auto-boot on next restart
        # under a new name without any T3 re-approval. Force state back to
        # pending so re-approval is explicit. Mirrors core PUT /lib/<n>.
        if dst_name.startswith("lib/"):
            dc = conn(dst_name)
            dr = dc.execute("SELECT state,version FROM stage_meta WHERE id=1").fetchone()
            prev = ((dr["state"] if dr else "pending") or "pending")
            if prev != "pending":
                dc.execute("UPDATE stage_meta SET state='pending',"
                           "updated_at=datetime('now') WHERE id=1")
                dc.commit()
                log_event(dst_name, "state_transition", {
                    "from": prev, "to": "pending",
                    "version": dr["version"] if dr else 0,
                    "reason": "source replaced (via MOVE)"})
        log_event(dst_name, "stage_moved", {"from": src_name})
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "COPY":
        src_name = _dav_world_name(path)
        if not src_name:
            return {"error":"COPY requires a source name", "_status":405}
        if not _valid_name(src_name):
            return {"error":"invalid source name", "_status":400}
        src_disk = DATA / _disk_name(src_name)
        src_is_world = (src_disk / "universe.db").exists()
        dest_raw, overwrite = "", True
        for k, v in scope.get("headers", []):
            if k == b"destination": dest_raw = v.decode("utf-8", "replace")
            elif k == b"overwrite": overwrite = v.decode().strip().upper() == "T"
        if not dest_raw:
            return {"error":"Destination header required", "_status":400}
        from urllib.parse import urlparse, unquote
        dest_path = unquote(urlparse(dest_raw).path or dest_raw)
        dst_name = _dav_world_name(dest_path)
        if not dst_name or not _valid_name(dst_name):
            return {"error":"invalid destination", "_status":400}
        pairs = []
        if src_is_world:
            pairs.append((src_name, dst_name))
        if DATA.exists():
            for d in sorted(DATA.iterdir()):
                if d.is_dir() and (d / "universe.db").exists():
                    w = _logical_name(d.name)
                    if w.startswith(src_name + "/"):
                        pairs.append((w, dst_name + w[len(src_name):]))
        if not pairs:
            return {"error":"source not found", "_status":404}
        touches_sys = any(s.startswith(_DAV_SYS_PREFIXES) or d.startswith(_DAV_SYS_PREFIXES) for s, d in pairs)
        if touches_sys and _check_auth(scope) != "approve":
            return {"error":"system copy requires approve", "_status":403}
        if not overwrite:
            for _, dw in pairs:
                if (DATA / _disk_name(dw) / "universe.db").exists():
                    return {"error":"destination exists", "_status":412}
        for sw, dw in pairs:
            src_db = DATA / _disk_name(sw) / "universe.db"
            dst_dir = DATA / _disk_name(dw)
            dst_db = dst_dir / "universe.db"
            if sw in _db:
                try: _db[sw].execute("PRAGMA wal_checkpoint(TRUNCATE)"); _db[sw].commit()
                except Exception: pass
            if dst_dir.exists():
                if dw in _db: _db.pop(dw).close()
                shutil.rmtree(dst_dir)
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_db, dst_db)
            # Approval-binding: same reason as MOVE above. A raw file-copy
            # of universe.db carries the state column over. If the copy
            # target sits in /lib/*, force state=pending so the operator
            # must re-approve before the clone can auto-boot under a new
            # name.
            if dw.startswith("lib/"):
                dc = conn(dw)
                dr = dc.execute("SELECT state,version FROM stage_meta WHERE id=1").fetchone()
                prev = ((dr["state"] if dr else "pending") or "pending")
                if prev != "pending":
                    dc.execute("UPDATE stage_meta SET state='pending',"
                               "updated_at=datetime('now') WHERE id=1")
                    dc.commit()
                    log_event(dw, "state_transition", {
                        "from": prev, "to": "pending",
                        "version": dr["version"] if dr else 0,
                        "reason": "source replaced (via COPY)"})
            log_event(dw, "stage_copied", {"from": sw})
        return {"_status":204, "_body":"", "_ct":"text/plain"}

    if method == "MKCOL":
        name = _dav_world_name(path)
        if not name: return {"_status":201, "_body":"", "_ct":"text/plain"}
        if not _valid_name(name): return {"_status":201, "_body":"", "_ct":"text/plain"}
        c = conn(name)
        c.execute("UPDATE stage_meta SET ext='dir',updated_at=datetime('now') WHERE id=1"); c.commit()
        return {"_status":201, "_body":"", "_ct":"text/plain"}
    if method in ("LOCK", "UNLOCK"):
        return {"_body":"", "_ct":"text/plain", "_status":501}
    return {"error":"method not allowed", "_status":405}


def register_plugin_routes():
    """Register /plugins/propose and /plugins/approve in _plugins, plus
    core inline routes (sse, dav)."""
    _plugins["/plugins/propose"] = handle_propose
    _plugins["/plugins/approve"] = handle_approve
    _plugins["/stream"] = _core_sse_handle
    _plugin_auth["/stream"] = "none"
    _plugins["/dav"] = _core_dav_handle
    _plugin_auth["/dav"] = "none"


async def cron_loop():
    """Background cron loop — runs plugin CRON handlers."""
    while True:
        await asyncio.sleep(1)
        now = time.time()
        for name, task in list(_cron_tasks.items()):
            if now - task["last_run"] >= task["interval"]:
                try:
                    await task["handler"]()
                    task["last_run"] = now
                except Exception as e:
                    print(f"  cron {name}: {e}")


if __name__ == "__main__":
    if not AUTH_TOKEN:
        print("\n  ! ELASTIK_TOKEN not set. Refusing to start in public mode.")
        print("  Set ELASTIK_TOKEN in .env or environment.\n")
        sys.exit(1)
    _root = Path(__file__).resolve().parent
    os.environ.setdefault("ELASTIK_DATA", str(_root / "data"))
    os.environ.setdefault("ELASTIK_ROOT", str(_root))

    register_plugin_routes()
    # /lib/* is the only loader. Iterate data/ for lib/<name> worlds
    # with state='active' and exec each. Per PLAN guardrail B, reads
    # the DB directly — no self-HTTP. Per guardrail D, exec failures
    # log and skip; state stays 'active' (operator intent), runtime
    # route registration is missing.
    boot_load_active_lib()
    # /boot — write once at startup, read requires T3.
    # boot/env: which env vars are set (names + non-secret values).
    # boot/grub: which plugins loaded.
    _boot_env = []
    # Allowlist: only show values for these safe keys. Everything else → name only.
    _safe_values = {"ELASTIK_HOST", "ELASTIK_PORT", "ELASTIK_PUBLIC",
                    "ELASTIK_DATA", "ELASTIK_ROOT", "OLLAMA_URL", "OLLAMA_MODEL"}
    for k, v in sorted(os.environ.items()):
        if k.startswith("ELASTIK_") or k.startswith("OLLAMA_"):
            _boot_env.append(f"{k}={v}" if k in _safe_values else f"{k}=***")
    c = conn("boot/env")
    c.execute("UPDATE stage_meta SET stage_html=?,ext='txt',version=version+1,"
              "updated_at=datetime('now') WHERE id=1", ("\n".join(_boot_env),))
    c.commit()
    _boot_grub = [m["name"] for m in _plugin_meta]
    c = conn("boot/grub")
    c.execute("UPDATE stage_meta SET stage_html=?,ext='txt',version=version+1,"
              "updated_at=datetime('now') WHERE id=1", ("\n".join(_boot_grub),))
    c.commit()
    print(f"\n  elastik -> http://{HOST}:{PORT}\n")
    run(extra_tasks=[cron_loop()])
