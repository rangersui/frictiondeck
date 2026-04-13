#!/usr/bin/env python3
"""elastik.py — one command to start everything.

Usage:
    python elastik.py                    # detect, start server, open browser
    python elastik.py --headless         # no GUI (edge device / NAS / CI)
    python elastik.py --no-browser       # start server only, skip browser
    python elastik.py --port 8080        # override port

Relationship to boot.py / server.py:
    elastik.py is the launcher.  boot.py is the full system.  server.py is the bare protocol.
    elastik.py never imports either — it starts boot.py as a subprocess.
    Communication is plain HTTP to localhost — same as any renderer or curl.
    If you don't want elastik.py, `python boot.py` or `python server.py` still works.
"""
import argparse, json, os, platform, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── hardware detection ────────────────────────────────────────────────

# GPU type classification
GPU_DISCRETE = "discrete"    # dedicated VRAM — NVIDIA GeForce/Quadro, AMD RX/Radeon Pro
GPU_APPLE    = "apple"       # Apple Silicon unified memory — M1/M2/M3/M4
GPU_INTEGRATED = "integrated"  # shared RAM — Intel UHD/Iris, AMD Radeon(TM) Graphics
GPU_NONE     = "none"

# Keywords to classify GPU name → type
_DISCRETE_KEYWORDS = [
    "geforce", "quadro", "tesla", "rtx", "gtx",  # NVIDIA
    "radeon rx", "radeon pro", "radeon vii",       # AMD discrete
    "arc a",                                        # Intel Arc discrete
]
_INTEGRATED_KEYWORDS = [
    "uhd graphics", "iris", "hd graphics",         # Intel integrated
    "radeon(tm) graphics", "radeon graphics",       # AMD APU (Ryzen integrated)
    "radeon vega",                                  # AMD Vega APU
    "videocore", "vc4", "v3d",                     # Raspberry Pi
    "mali", "adreno", "powervr", "img gpu",        # Mobile / ARM SoC
]


def _classify_gpu(name):
    """Classify GPU name → (name, type)."""
    if not name:
        return None, GPU_NONE
    low = name.lower()
    for kw in _DISCRETE_KEYWORDS:
        if kw in low:
            return name, GPU_DISCRETE
    for kw in _INTEGRATED_KEYWORDS:
        if kw in low:
            return name, GPU_INTEGRATED
    # Unknown GPU — assume integrated to be conservative
    return name, GPU_INTEGRATED


def detect_gpu():
    """Detect GPU name and type. Returns (name, type) tuple.

    Tries multiple methods per platform:
      Windows:  PowerShell WMI → nvidia-smi
      macOS:    system_profiler → Apple Silicon check
      Linux:    lspci → /sys/class/drm → vcgencmd (Raspberry Pi)
    """
    system = platform.system()

    # ── Windows: PowerShell WMI (catches all GPUs) ──
    if system == "Windows":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                gpus = [g.strip() for g in r.stdout.strip().splitlines() if g.strip()]
                # If multiple GPUs, prefer discrete over integrated
                best_name, best_type = None, GPU_NONE
                for g in gpus:
                    name, gtype = _classify_gpu(g)
                    if gtype == GPU_DISCRETE:
                        return name, gtype
                    if name:
                        best_name, best_type = name, gtype
                if best_name:
                    return best_name, best_type
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  detect    powershell gpu query failed: {e}")

    # ── macOS: system_profiler + Apple Silicon ──
    if system == "Darwin":
        # Check Apple Silicon first
        if platform.machine() == "arm64":
            try:
                r = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=5,
                )
                chip = r.stdout.strip()  # e.g. "Apple M2 Pro"
                if "Apple" in chip:
                    return chip, GPU_APPLE
            except Exception:
                return "Apple Silicon", GPU_APPLE
        # Intel Mac — try system_profiler
        try:
            r = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines():
                if "Chipset Model" in line or "Chip Model" in line:
                    name = line.split(":")[-1].strip()
                    return _classify_gpu(name)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  detect    system_profiler failed: {e}")

    # ── Linux: lspci → vcgencmd (Pi) ──
    if system == "Linux":
        # lspci — works on most Linux distros
        try:
            r = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                best_name, best_type = None, GPU_NONE
                for line in r.stdout.splitlines():
                    if "VGA" in line or "3D" in line or "Display" in line:
                        # Format: "xx:xx.x VGA compatible controller: NVIDIA Corporation GeForce RTX 3090"
                        name = line.split(":")[-1].strip()
                        n, t = _classify_gpu(name)
                        if t == GPU_DISCRETE:
                            return n, t
                        if n:
                            best_name, best_type = n, t
                if best_name:
                    return best_name, best_type
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  detect    lspci failed: {e}")

        # Raspberry Pi — vcgencmd
        try:
            r = subprocess.run(
                ["vcgencmd", "get_mem", "gpu"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return "VideoCore (Raspberry Pi)", GPU_INTEGRATED
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  detect    vcgencmd failed: {e}")

    # ── Cross-platform fallback: nvidia-smi ──
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            name = r.stdout.strip().splitlines()[0]
            return name, GPU_DISCRETE
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  detect    nvidia-smi error: {e}")

    return None, GPU_NONE


def detect_ram_mb():
    """Total physical RAM in MB. Returns 0 on failure."""
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            mem = ctypes.c_ulonglong()
            ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(mem))
            return mem.value // 1024
        elif system == "Linux":
            with open("/proc/meminfo") as f:
                return int(f.readline().split()[1]) // 1024
        elif system == "Darwin":
            r = subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True, timeout=5)
            return int(r.stdout.strip()) // (1024 * 1024)
    except Exception as e:
        print(f"  detect    ram detection failed: {e}")
    return 0


def detect_device_hint():
    """Guess device category from platform clues."""
    system = platform.system()
    arch = platform.machine().lower()

    # Raspberry Pi
    if system == "Linux" and arch.startswith("a"):  # arm, aarch64
        try:
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read().lower()
                if "raspberry" in cpuinfo or "bcm" in cpuinfo:
                    return "raspberry-pi"
        except Exception:
            pass
        # Generic ARM Linux — could be phone/SBC/server
        return "arm-linux"

    # iOS/Android won't typically run this, but just in case (Termux, iSH)
    if system == "Linux" and os.path.isdir("/data/data/com.termux"):
        return "android-termux"

    if system == "Darwin":
        if arch == "arm64":
            return "apple-silicon-mac"
        return "intel-mac"

    if system == "Windows":
        return "windows-pc"

    return "unknown"


def detect_tier():
    """Detect hardware → assign tier 1-4.

    Tier assignment matrix:
    ┌──────────────────────┬────────┬────────┬────────┬────────┐
    │ GPU \\ RAM           │  ≥16GB │  ≥8GB  │  ≥4GB  │  <4GB  │
    ├──────────────────────┼────────┼────────┼────────┼────────┤
    │ discrete (RTX, RX)   │ tier 1 │ tier 1 │ tier 2 │ tier 2 │
    │ apple silicon        │ tier 1 │ tier 1 │ tier 2 │ tier 2 │
    │ integrated (UHD, APU)│ tier 2 │ tier 2 │ tier 3 │ tier 3 │
    │ none / unknown       │ tier 2 │ tier 3 │ tier 3 │ tier 4 │
    └──────────────────────┴────────┴────────┴────────┴────────┘

    Tier 1  full       discrete/apple GPU + 8GB+   WebGPU, video, AI
    Tier 2  capable    decent GPU or lots of RAM    most renderers, WebRTC
    Tier 3  minimal    integrated GPU or 4GB+       lightweight renderers
    Tier 4  text-only  very constrained             plain text worlds only
    """
    gpu_name, gpu_type = detect_gpu()
    ram = detect_ram_mb()
    device = detect_device_hint()

    info = {
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "ram_mb": ram,
        "gpu": gpu_name,
        "gpu_type": gpu_type,
        "device": device,
        "python": platform.python_version(),
    }

    # ── tier assignment ──
    has_strong_gpu = gpu_type in (GPU_DISCRETE, GPU_APPLE)
    has_igpu = gpu_type == GPU_INTEGRATED

    if has_strong_gpu and ram >= 8192:
        info["tier"] = 1
    elif has_strong_gpu:
        info["tier"] = 2
    elif has_igpu and ram >= 8192:
        info["tier"] = 2
    elif ram >= 16384:
        # Lots of RAM, no GPU — still capable (server / headless)
        info["tier"] = 2
    elif has_igpu or ram >= 4096:
        info["tier"] = 3
    else:
        info["tier"] = 4

    return info


# ── server lifecycle ──────────────────────────────────────────────────

def wait_for_port(port, host="127.0.0.1", timeout=30):
    """Block until server responds on port, or timeout."""
    url = f"http://{host}:{port}/stages"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def load_token():
    """Read ELASTIK_TOKEN from env or .env file."""
    token = os.environ.get("ELASTIK_TOKEN", "")
    if token:
        return token
    env_file = os.path.join(ROOT, ".env")
    if os.path.isfile(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ELASTIK_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return ""


def write_world(port, world, body, host="127.0.0.1"):
    """POST body to a world via standard elastik HTTP API."""
    url = f"http://{host}:{port}/{world}/write"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain")
    token = load_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        print(f"  warning   write_world({world}) failed: {e}")
        return False


def start_server(port):
    """Launch boot.py (or server.py) as a subprocess. Returns Popen object."""
    env = os.environ.copy()
    env["ELASTIK_PORT"] = str(port)
    # Bind to 127.0.0.1 unless user explicitly set ELASTIK_HOST
    if "ELASTIK_HOST" not in env:
        env.setdefault("ELASTIK_HOST", "127.0.0.1")

    # Prefer boot.py (full system with plugins), fallback to server.py (bare protocol)
    target = "boot.py" if os.path.exists(os.path.join(ROOT, "boot.py")) else "server.py"
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, target)],
        env=env,
        cwd=ROOT,
    )


# ── browser launch ────────────────────────────────────────────────────

# Chromium flags for headless / edge devices
# (from thesis: table 3.1, validated on Raspberry Pi)
HEADLESS_CHROMIUM_FLAGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--use-fake-ui-for-media-stream",
    "--disable-translate",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--no-first-run",
    "--mute-audio",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-oop-rasterization",
    "--enable-zero-copy",
    "--enable-features=VaapiVideoEncoder,VaapiVideoDecoder,CanvasOopRasterization",
]


def launch_headless(url):
    """Launch headless Chromium via playwright (if available)."""
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, args=HEADLESS_CHROMIUM_FLAGS)
        page = browser.new_page()
        page.goto(url)
        print(f"  headless  -> {url}  [playwright]")
        return browser
    except ImportError:
        print("  headless  playwright not installed, trying system chrome...")

    # Fallback: try launching system chromium/chrome directly
    chrome = find_chrome()
    if chrome:
        args = [chrome, f"--headless=new", f"--app={url}"] + HEADLESS_CHROMIUM_FLAGS
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  headless  -> {url}  [chrome --headless]")
        return proc

    print("  warning: playwright not installed, no system chrome found")
    print("           install with: pip install playwright && playwright install chromium")
    return None


def find_chrome():
    """Find Chrome/Chromium executable on this system."""
    if platform.system() == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    elif platform.system() == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def launch_browser(url):
    """Open URL in default browser."""
    import webbrowser
    webbrowser.open(url)
    print(f"  browser   -> {url}")


# ── main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="elastik — one command to start everything",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Without elastik.py, `python boot.py` (full) or `python server.py` (bare) still works.",
    )
    parser.add_argument("--port", type=int, default=None,
                        help="override port (default: from .env or 3004)")
    parser.add_argument("--headless", action="store_true",
                        help="headless Chromium, no GUI (edge device / NAS / CI)")
    parser.add_argument("--no-browser", action="store_true",
                        help="start server only, don't open browser")
    parser.add_argument("--skip-detect", action="store_true",
                        help="skip hardware detection")
    args = parser.parse_args()

    # resolve port: CLI > .env > default
    port = args.port
    if port is None:
        port = int(os.environ.get("ELASTIK_PORT", "3004"))
        # also check .env file directly
        env_file = os.path.join(ROOT, ".env")
        if os.path.isfile(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ELASTIK_PORT="):
                        port = int(line.split("=", 1)[1].strip())

    url = f"http://localhost:{port}"

    # ── Phase 1: hardware detection ──
    tier_info = None
    if not args.skip_detect:
        print("  detect    hardware...")
        tier_info = detect_tier()
        tier = tier_info["tier"]
        gpu_name = tier_info["gpu"] or "none"
        gpu_type = tier_info["gpu_type"]
        gpu_str = f"{gpu_name} ({gpu_type})" if gpu_name else "none"
        ram_str = f'{tier_info["ram_mb"]}MB' if tier_info["ram_mb"] else "unknown"
        device = tier_info.get("device", "unknown")
        print(f"  tier {tier}     {tier_info['os']}/{tier_info['arch']}  "
              f"cpu={tier_info['cpu_count']}  ram={ram_str}  gpu={gpu_str}")
        print(f"            device={device}")
    else:
        print("  detect    skipped")

    # ── Phase 2: start server.py ──
    print(f"  server    starting on :{port}...")
    proc = start_server(port)

    if not wait_for_port(port):
        print("  error     server did not start within 30s")
        proc.terminate()
        sys.exit(1)

    print(f"  server    ready -> {url}")

    # ── Phase 3: write tier info ──
    if tier_info:
        write_world(port, "tier-info", json.dumps(tier_info, indent=2))
        print(f"  tier-info written to world")

    # ── Phase 4: launch browser ──
    if args.headless:
        launch_headless(url)
    elif not args.no_browser:
        launch_browser(url)
    else:
        print("  browser   skipped (--no-browser)")

    # ── Phase 5: keep running ──
    print()
    print(f"  elastik   running on {url}")
    print(f"            Ctrl+C to stop")
    print()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n  shutting down...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("  stopped.")


if __name__ == "__main__":
    main()
