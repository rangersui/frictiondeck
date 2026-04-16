#!/usr/bin/env python3
"""slow_write.py — stream a file into an elastik world, chunk by chunk,
with configurable delay. Watch SSE deliver each chunk in real time.

Usage:
    python slow_write.py testworld.html
    python slow_write.py page.html --world testworld --delay 0.02 --chunk 8 --token TOK

Uses HTTP Keep-Alive on one persistent connection via http.client.
Default host is 127.0.0.1 (not 'localhost') to avoid Windows DNS overhead
that otherwise makes each request take ~2 seconds.
"""
import argparse, http.client, os, sys, time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="file to stream into the world")
    ap.add_argument("--world", default=None, help="world name (default: file stem)")
    ap.add_argument("--host", default="127.0.0.1", help="server host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=3005)
    ap.add_argument("--delay", type=float, default=0.02, help="seconds between chunks (default 20ms)")
    ap.add_argument("--chunk", type=int, default=8, help="bytes per chunk (default 8, ~2 tokens)")
    ap.add_argument("--ext", default=None, help="override ext (default: inferred from file extension)")
    ap.add_argument("--token", default=None,
                    help="bearer token. Default: $ELASTIK_APPROVE_TOKEN then $ELASTIK_TOKEN. "
                         "ext=html requires approve-level.")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}"); sys.exit(1)
    data = path.read_bytes()

    world = args.world or path.stem
    ext = args.ext or (path.suffix.lstrip(".") or "plain")

    token = args.token or os.environ.get("ELASTIK_APPROVE_TOKEN") or os.environ.get("ELASTIK_TOKEN")
    if not token:
        print("error: no token. Pass --token TOKEN, or set ELASTIK_APPROVE_TOKEN / ELASTIK_TOKEN env")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {token}", "Connection": "keep-alive"}

    print(f"→ target:  http://{args.host}:{args.port}/{world} (ext={ext})")
    print(f"→ source:  {path} ({len(data):,} bytes)")
    print(f"→ chunk:   {args.chunk} bytes, delay {args.delay}s between")
    print()

    # One persistent connection
    conn = http.client.HTTPConnection(args.host, args.port, timeout=10)

    def request(method, url, body=b""):
        conn.request(method, url, body=body, headers={**headers, "Content-Length": str(len(body))})
        r = conn.getresponse()
        body_out = r.read()
        if r.status >= 400:
            print(f"\nerror {r.status}: {body_out.decode(errors='replace')}")
            sys.exit(1)
        return body_out

    # Reset
    request("POST", f"/home/{world}/write?ext={ext}", b"")
    print(f"[reset] world '{world}' cleared")

    sent = 0
    t0 = time.time()
    for i in range(0, len(data), args.chunk):
        chunk = data[i:i + args.chunk]
        request("POST", f"/home/{world}/append?ext={ext}", chunk)
        sent += len(chunk)
        pct = sent * 100 / len(data)
        # Flush stdout so progress shows as it happens
        print(f"[{pct:5.1f}%]  sent {sent:>6}/{len(data):<6}  (+{len(chunk)}B)", flush=True)
        time.sleep(args.delay)

    dt = time.time() - t0
    print(f"\ndone. {sent:,} bytes in {dt:.2f}s ({sent/dt:.0f} B/s, {len(data)/args.chunk/dt:.1f} chunks/s)")
    conn.close()


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("\naborted")
