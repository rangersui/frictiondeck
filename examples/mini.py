#!/usr/bin/env python3
"""mini-elastik — the five rules, nothing more.

    1. Listen.                       asyncio server
    2. Read/write strings.           HTTP body
    3. Store strings.                files + json sidecar (not SQLite)
    4. Sign with an HMAC chain.      hmac.new
    5. Render in a browser.          → index.html (out of scope here)

This proves elastik is not SQLite, not a framework, not server.py.
It's the five rules. Any implementation that satisfies them is elastik.

Run:    python mini.py                    → http://127.0.0.1:3006
Write:  curl -X POST :3006/foo/write -d hi
Read:   curl :3006/foo/read
List:   curl :3006/stages

Bare protocol — no FHS namespace (/home/, /etc/). The full server
adds that layer on top. This is the kernel, not the distro.

No dependencies. Python 3.8+ stdlib only.
"""
import asyncio, hashlib, hmac, json, os, tempfile
from pathlib import Path

KEY = os.getenv("ELASTIK_KEY", "dev-key").encode()
ROOT = Path(os.getenv("WORLDS_DIR", "worlds")); ROOT.mkdir(exist_ok=True)
PORT = int(os.getenv("PORT", 3006))


def meta(name):
    p = ROOT / name / "meta.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {"version": 0, "hmac": ""}


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, path)


def chain(prev, body):
    return hmac.new(KEY, prev.encode() + body, hashlib.sha256).hexdigest()


def resp(status, body=b""):
    if isinstance(body, str): body = body.encode()
    return (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode() + body


async def handle(reader, writer):
    try:
        line = await reader.readline()
        if not line: return
        method, path, _ = line.decode().split(" ", 2)
        n = 0
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""): break
            s = h.decode(errors="replace").lower()
            if s.startswith("content-length:"): n = int(s.split(":", 1)[1])
        body = await reader.readexactly(n) if n else b""
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]

        if method == "GET" and parts == ["stages"]:
            out = [{"name": d.name, **meta(d.name)}
                   for d in sorted(ROOT.iterdir()) if d.is_dir() and (d / "meta.json").exists()]
            writer.write(resp("200 OK", json.dumps(out)))

        elif method == "GET" and len(parts) == 2 and parts[1] == "read":
            m = meta(parts[0])
            c = ROOT / parts[0] / "content"
            writer.write(resp("200 OK", json.dumps({
                "stage_html": c.read_text("utf-8", errors="replace") if c.exists() else "", **m})))

        elif method == "POST" and len(parts) == 2 and parts[1] in ("write", "append"):
            name = parts[0]
            m = meta(name)
            cp = ROOT / name / "content"
            cur = cp.read_bytes() if cp.exists() else b""
            new = body if parts[1] == "write" else cur + body
            h = chain(m["hmac"], new)
            atomic(cp, new)
            atomic(ROOT / name / "meta.json", json.dumps({"version": m["version"] + 1, "hmac": h}))
            writer.write(resp("200 OK", json.dumps({"version": m["version"] + 1, "hmac": h})))

        else:
            writer.write(resp("404 Not Found", '{"error":"not found"}'))
        await writer.drain()
    except Exception as e:
        try: writer.write(resp("500 Error", json.dumps({"error": str(e)})))
        except OSError: pass
    finally:
        try: writer.close()
        except OSError: pass


async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", PORT)
    print(f"mini-elastik -> http://127.0.0.1:{PORT}  (worlds/ = {ROOT.resolve()})")
    await srv.serve_forever()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
