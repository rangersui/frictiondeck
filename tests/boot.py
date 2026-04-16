#!/usr/bin/env python3
"""elastik POST (Power-On Self-Test).

Does this thing behave like a Linux box?
Boot up, poke every subsystem, report.

Lives in the elastik world /home/boot. Run it:
  curl -s localhost:3005/home/boot?raw | python -X utf8 -
  python tests/boot.py
  python tests/boot.py http://remote:3005

Tokens from env vars (ELASTIK_TOKEN, ELASTIK_APPROVE_TOKEN).
"""
import urllib.request, urllib.error, json, base64, sys, os, time, threading, re

HOST = sys.argv[1] if len(sys.argv) > 1 else os.getenv("ELASTIK_HOST_URL", "http://127.0.0.1:3005")

# Find tokens: env vars first, then .env in CWD or parents
TOKEN = os.getenv("ELASTIK_TOKEN", "")
APPROVE = os.getenv("ELASTIK_APPROVE_TOKEN", "")
if not TOKEN:
    # Walk up from CWD looking for .env
    d = os.getcwd()
    for _ in range(5):
        ef = os.path.join(d, ".env")
        if os.path.exists(ef):
            for line in open(ef):
                line = line.strip()
                if line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "ELASTIK_TOKEN" and not TOKEN: TOKEN = v
                if k == "ELASTIK_APPROVE_TOKEN" and not APPROVE: APPROVE = v
            break
        parent = os.path.dirname(d)
        if parent == d: break
        d = parent

OK = 0; FAIL = 0


def _auth(tok):
    return "Basic " + base64.b64encode(f":{tok}".encode()).decode()


def _req(path, method="GET", body=None, auth=None):
    data = body.encode("utf-8") if isinstance(body, str) else body
    r = urllib.request.Request(f"{HOST}{path}", data=data, method=method)
    if auth:
        r.add_header("Authorization", auth)
    try:
        resp = urllib.request.urlopen(r, timeout=8)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def j(body):
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}


def check(name, ok, detail=""):
    global OK, FAIL
    if ok:
        OK += 1
        print(f"  \033[32mOK\033[0m   {name}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {name}  -- {detail}")


# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1melastik POST (Power-On Self-Test)\033[0m")
print(f"  host: {HOST}")
print(f"  token: {'set' if TOKEN else 'MISSING'}")
print(f"  approve: {'set' if APPROVE else 'MISSING'}")

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/proc — can we see ourselves?\033[0m\n")

st, body = _req("/proc/version")
check("/proc/version", st == 200 and body.strip().isdigit(), f"st={st} body={body[:20]}")

st, body = _req("/proc/uptime")
check("/proc/uptime", st == 200 and body.strip().isdigit(), f"st={st}")

st, body = _req("/proc/status")
d = j(body)
check("/proc/status → pid", d.get("pid", 0) > 0, f"got={d}")
check("/proc/status → plugins > 0", d.get("plugins", 0) > 0)
check("/proc/status → worlds >= 0", d.get("worlds", -1) >= 0)

st, body = _req("/proc/worlds")
worlds = j(body) if st == 200 else []
check("/proc/worlds → array", isinstance(worlds, list) and len(worlds) >= 0)

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/home — user worlds\033[0m\n")

st, _ = _req("/home/boot-test?ext=txt", "PUT", "boot test data",
             auth=f"Bearer {TOKEN}")
check("write /home (T2)", st == 200, f"st={st}")

st, body = _req("/home/boot-test")
d = j(body)
check("read /home → content", d.get("stage_html") == "boot test data")
check("read /home → ext", d.get("ext") == "txt")
check("read /home → version", d.get("version", 0) > 0)

v = d.get("version", 0)
st, _ = _req(f"/home/boot-test?v={v}")
check("304 when version matches", st == 304, f"st={st}")

st, _ = _req("/home/boot-test?raw")
check("/home/raw → 200", st == 200)

st, _ = _req("/home/boot-test", "PUT", "no auth")
check("write /home no auth → 403", st == 403, f"st={st}")

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/etc — config (T3 only write)\033[0m\n")

st, _ = _req("/etc/boot-cfg", "PUT", "test=1", auth=f"Bearer {TOKEN}")
check("/etc write T2 → 403", st == 403, f"st={st}")

st, _ = _req("/etc/boot-cfg", "PUT", "test=1", auth=_auth(APPROVE))
check("/etc write T3 → 200", st == 200, f"st={st}")

st, body = _req("/etc/boot-cfg")
check("/etc read → open", st == 200 and "test=1" in j(body).get("stage_html", ""))

# shadow
_req("/etc/shadow", "PUT", "root:deadbeef", auth=_auth(APPROVE))
st, _ = _req("/etc/shadow")
check("/etc/shadow read (no auth) → 403", st == 403, f"st={st}")

st, body = _req("/etc/shadow", auth=_auth(APPROVE))
check("/etc/shadow read (T3) → 200", st == 200 and "deadbeef" in body, f"st={st}")

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/usr/lib — system worlds (T3 only write)\033[0m\n")

st, _ = _req("/usr/lib/skills/boot-s?ext=md", "PUT", "# test",
             auth=_auth(APPROVE))
check("/usr/lib write T3 → 200", st == 200, f"st={st}")

st, _ = _req("/usr/lib/skills/boot-s?ext=md", "PUT", "# test",
             auth=f"Bearer {TOKEN}")
check("/usr/lib write T2 → 403", st == 403, f"st={st}")

st, _ = _req("/usr/lib/skills/boot-s")
check("/usr/lib read → open", st == 200)

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/var/log — system logs\033[0m\n")

st, _ = _req("/var/log/health/status")
check("/var/log/health/status → 200", st == 200, f"st={st}")

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/dav — FHS WebDAV tree\033[0m\n")

st, body = _req("/dav/", method="PROPFIND", body="")
hrefs = re.findall(r"<D:href>([^<]+)</D:href>", body) if st == 207 else []
check("PROPFIND /dav/ → 207", st == 207, f"st={st}")
check("/dav/ → home/", "/dav/home/" in hrefs, f"hrefs={hrefs}")
check("/dav/ → etc/", "/dav/etc/" in hrefs)

st, body = _req("/dav/home/boot-test.txt")
check("DAV GET /dav/home/ → content", st == 200 and "boot test data" in body)

st, _ = _req("/dav/home/dav-boot.txt", "PUT", "dav wrote",
             auth=_auth(APPROVE))
check("DAV PUT → 201", st == 201, f"st={st}")

st, body = _req("/home/dav-boot")
check("DAV write → /home readable", "dav wrote" in j(body).get("stage_html", ""))

st, _ = _req("/dav/etc/shadow.txt")
check("DAV /etc/shadow → 403", st == 403, f"st={st}")

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/mnt — fstab local mount\033[0m\n")

st, body = _req("/mnt/")
check("/mnt/ → lists mounts", "mounts" in j(body), f"st={st}")

st, _ = _req("/mnt/nosuchmount/")
check("/mnt bad mount → 404", st == 404, f"st={st}")

# Full read/write test only if we can make a temp dir (local run)
import tempfile, shutil
try:
    tmpdir = tempfile.mkdtemp(prefix="elastik-boot-")
    with open(os.path.join(tmpdir, "hello.txt"), "w") as f:
        f.write("mounted!")
    fstab = f"{tmpdir}  /mnt/boottest  rw"
    _req("/etc/fstab", "PUT", fstab, auth=_auth(APPROVE))

    st, body = _req("/mnt/boottest/")
    d = j(body)
    check("/mnt list → has file",
          any(e["name"] == "hello.txt" for e in d.get("entries", [])),
          f"got={d}")

    st, body = _req("/mnt/boottest/hello.txt")
    check("/mnt read → content", j(body).get("content") == "mounted!")

    st, _ = _req("/mnt/boottest/new.txt", "POST", "via mnt",
                 auth=_auth(APPROVE))
    check("/mnt write rw → ok", st == 200, f"st={st}")
    check("/mnt write → on disk",
          os.path.exists(os.path.join(tmpdir, "new.txt")))
    shutil.rmtree(tmpdir, ignore_errors=True)
except OSError:
    print("  SKIP /mnt read/write (no local filesystem)")

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/dev — primitives\033[0m\n")

st, _ = _req("/dev/stone")
check("/dev/stone → exists", st in (200, 204), f"st={st}")

st, _ = _req("/true")
check("/true → 200", st == 200)

st, _ = _req("/false")
check("/false → 403", st == 403)

st, body = _req("/not", "POST", "true")
check("/not true → false", body.strip() == "false", f"body={body.strip()}")

st, body = _req("/nand?a=true&b=true")
check("/nand(1,1) → false", body.strip() == "false")

st, body = _req("/resistor?ohm=470")
check("/resistor → R 470", j(body).get("type") == "R" and j(body).get("value") == 470)

st, body = _req("/led?color=blue")
check("/led blue → 470nm", j(body).get("wavelength") == 470)

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m/flush — SSE streaming integration\033[0m\n")

events = []


def sse_listen():
    try:
        r = urllib.request.urlopen(f"{HOST}/stream/toilet", timeout=8)
        buf = b""
        while True:
            chunk = r.read(1)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n\n"):
                for line in buf.decode("utf-8", "replace").strip().splitlines():
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]).get("stage_html", ""))
                        except (json.JSONDecodeError, AttributeError):
                            pass
                buf = b""
    except Exception:
        pass


t = threading.Thread(target=sse_listen, daemon=True)
t.start()
time.sleep(0.5)

st, body = _req("/flush", "POST", "", auth=f"Bearer {TOKEN}")
check("/flush → 200 + sparkle", st == 200 and "\u2728" in body, f"st={st}")

time.sleep(1.5)

check("SSE captured >= 3 stages", len(events) >= 3, f"got {len(events)}")
if events:
    check("SSE has water", "\U0001f4a7" in "".join(events))
    check("SSE ends clean", "\u2728" in events[-1], f"last={events[-1][:10]}")

st, body = _req("/home/toilet")
check("toilet → clean after flush", "\u2728" in j(body).get("stage_html", ""))

# ═══════════════════════════════════════════════════════════════
print(f"\n\033[1m=== THREE-TIER AUTH ===\033[0m\n")

# T1
st, _ = _req("/home/boot-test")
check("T1 read → 200", st == 200)
st, _ = _req("/home/boot-test", "PUT", "x")
check("T1 write → 403", st == 403, f"st={st}")

# T2
st, _ = _req("/home/boot-test?ext=txt", "PUT", "t2",
             auth=f"Bearer {TOKEN}")
check("T2 /home write → 200", st == 200, f"st={st}")
st, _ = _req("/etc/boot-cfg", "PUT", "t2 etc", auth=f"Bearer {TOKEN}")
check("T2 /etc write → 403", st == 403, f"st={st}")

# T3
st, _ = _req("/etc/boot-cfg", "PUT", "t3", auth=_auth(APPROVE))
check("T3 /etc write → 200", st == 200, f"st={st}")

# ═══════════════════════════════════════════════════════════════
# Summary
total = OK + FAIL
pct = OK * 100 // total if total else 0
color = "\033[32m" if FAIL == 0 else "\033[31m"
print(f"\n{'=' * 50}")
print(f"  {color}PASS: {OK}  FAIL: {FAIL}  TOTAL: {total}\033[0m")
print(f"  {pct}%")
print(f"{'=' * 50}\n")
sys.exit(0 if FAIL == 0 else 1)
