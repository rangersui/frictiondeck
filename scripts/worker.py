#!/usr/bin/env python3
"""
Exec Queue Worker — polls elastik for commands, asks human approval, executes.
Run this on the machine that has the tools (Quartus, Questa, etc).

Usage:
    python worker.py                          # default: http://localhost:3004
    python worker.py --url http://192.168.1.5:3004  # remote elastik
    python worker.py --token mytoken          # explicit token
"""

import json, os, subprocess, time, sys, argparse, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

# Load .env
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.getenv("ELASTIK_TOKEN", "")

def http(method, url, body=None):
    """Minimal HTTP client. No dependencies."""
    data = body.encode('utf-8') if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    if TOKEN and method == 'POST':
        req.add_header('Authorization', f'Bearer {TOKEN}')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        return None

def main():
    global TOKEN
    parser = argparse.ArgumentParser(description='Exec Queue Worker')
    parser.add_argument('--url', default='http://localhost:3004', help='elastik server URL')
    parser.add_argument('--interval', type=float, default=2.0, help='poll interval seconds')
    parser.add_argument('--token', default='', help='auth token (overrides ELASTIK_TOKEN)')
    args = parser.parse_args()

    if args.token: TOKEN = args.token

    base = args.url.rstrip('/')
    last_version = -1

    print(f"[worker] polling {base}/exec-queue every {args.interval}s")
    print(f"[worker] Ctrl+C to stop\n")

    while True:
        try:
            data = http('GET', f'{base}/exec-queue/read')
            if not data or data.get('version', -1) == last_version:
                time.sleep(args.interval)
                continue

            last_version = data['version']
            raw = data.get('stage_html', '').strip()
            if not raw:
                time.sleep(args.interval)
                continue

            # Parse command
            try:
                task = json.loads(raw)
            except json.JSONDecodeError:
                task = {"cmd": raw, "cwd": ".", "reason": "(no reason given)"}

            cmd = task.get('cmd', '')
            cwd = task.get('cwd', '.')
            reason = task.get('reason', '')

            # Display to human
            print("=" * 60)
            print(f"[INCOMING COMMAND]  v{last_version}")
            print(f"  cmd:    {cmd}")
            print(f"  cwd:    {cwd}")
            print(f"  reason: {reason}")
            print("=" * 60)

            # Ask approval
            approval = input("Execute? [Y/n] > ").strip().lower()
            if approval in ('', 'y', 'yes'):
                print(f"[worker] executing...")
                start = time.time()
                try:
                    result = subprocess.run(
                        cmd, shell=True, cwd=cwd,
                        capture_output=True, text=True, timeout=300
                    )
                    duration = round(time.time() - start, 2)
                    output = json.dumps({
                        "exit_code": result.returncode,
                        "stdout": result.stdout[-5000:],
                        "stderr": result.stderr[-2000:],
                        "approved_at": datetime.now().isoformat(),
                        "duration": duration,
                        "cmd": cmd
                    })
                    print(f"[worker] exit={result.returncode} ({duration}s)")
                    if result.stdout.strip():
                        print(f"[stdout] {result.stdout[:500]}")
                    if result.stderr.strip():
                        print(f"[stderr] {result.stderr[:500]}")
                except subprocess.TimeoutExpired:
                    output = json.dumps({
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "TIMEOUT after 300s",
                        "approved_at": datetime.now().isoformat(),
                        "duration": 300,
                        "cmd": cmd
                    })
                    print("[worker] TIMEOUT")
                except Exception as e:
                    output = json.dumps({
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": str(e),
                        "approved_at": datetime.now().isoformat(),
                        "duration": 0,
                        "cmd": cmd
                    })
                    print(f"[worker] ERROR: {e}")

                # Write result back
                http('POST', f'{base}/exec-result/write', output)
                print("[worker] result written to /exec-result\n")
            else:
                # Rejected
                output = json.dumps({
                    "exit_code": -999,
                    "stdout": "",
                    "stderr": "REJECTED by human",
                    "approved_at": datetime.now().isoformat(),
                    "duration": 0,
                    "cmd": cmd
                })
                http('POST', f'{base}/exec-result/write', output)
                print("[worker] REJECTED. Result written.\n")

        except KeyboardInterrupt:
            print("\n[worker] stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[worker] error: {e}")
            time.sleep(args.interval)

if __name__ == '__main__':
    main()
