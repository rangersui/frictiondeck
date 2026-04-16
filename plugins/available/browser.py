"""browser — Chrome remote control via CDP. Zero MCP, zero npm.

POST /opt/browser/open?url=https://...   launch Chrome + navigate
POST /opt/browser/eval                   Runtime.evaluate (body = JS)
GET  /opt/browser/screenshot             PNG base64
POST /opt/browser/click?x=100&y=200      click
POST /opt/browser/type                   type text (body)
GET  /opt/browser/dom                    full page text
POST /opt/browser/close                  kill Chrome

Chrome DevTools Protocol over WebSocket. No Puppeteer. No Playwright.
No MCP. Just the 20-year-old debugging port that Selenium, Puppeteer,
and every browser automation tool secretly calls underneath.
"""
DESCRIPTION = "/opt/browser — Chrome via CDP. Screenshot, eval, click."
AUTH = "approve"  # browser control is root-only
ROUTES = {}

import asyncio, base64, json, os, platform, secrets, socket, struct, subprocess, threading
import urllib.request

# ── CDP WebSocket client — stdlib only, ~50 lines ────────────────────

class CDP:
    def __init__(self, ws_url):
        _, _, host_port_path = ws_url.partition("://")
        host_port, _, path = host_port_path.partition("/")
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.send(
            f"GET /{path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        if b"101" not in buf.split(b"\r\n")[0]:
            raise ConnectionError(f"WS handshake failed: {buf[:80]}")
        self.sock.settimeout(30)  # screenshots can be big
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method, params=None):
        with self._lock:
            self._id += 1
            mid = self._id
            msg = json.dumps({"id": mid, "method": method, "params": params or {}}).encode()
            self._ws_send(msg)
            # recv until we get OUR response (skip CDP events)
            while True:
                r = self._recv_frame()
                if r.get("id") == mid:
                    if "error" in r:
                        return {"error": r["error"].get("message", str(r["error"]))}
                    return r.get("result", {})

    def _ws_send(self, payload):
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81])
        n = len(payload)
        if n < 126:
            frame += bytes([0x80 | n])
        elif n < 65536:
            frame += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            frame += bytes([0x80 | 127]) + struct.pack(">Q", n)
        frame += mask + masked
        self.sock.sendall(frame)

    def _recv_frame(self):
        """Read one logical message. Handles continuation frames (fragmentation)."""
        buf = b""
        while True:
            h = self._readn(2)
            fin = h[0] & 0x80
            n = h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._readn(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._readn(8))[0]
            if h[1] & 0x80:
                self._readn(4)  # server shouldn't mask, but tolerate
            buf += self._readn(n)
            if fin:
                break
        return json.loads(buf)

    def _readn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("CDP connection closed")
            buf += chunk
        return buf

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ── Chrome launcher ──────────────────────────────────────────────────

_proc = None
_cdp = None
_CDP_PORT = 9222


def _find_chrome():
    """Find any Chromium-based browser. They all speak CDP."""
    system = platform.system()
    candidates = []
    if system == "Windows":
        for base in [os.environ.get("PROGRAMFILES", ""),
                     os.environ.get("PROGRAMFILES(X86)", ""),
                     os.path.expanduser("~") + "\\AppData\\Local"]:
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
            candidates.append(os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))
            candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = ["google-chrome", "brave-browser", "chromium-browser", "chromium", "microsoft-edge"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "chrome" if system != "Windows" else "chrome.exe"


# ── Handlers ─────────────────────────────────────────────────────────

async def handle_open(method, body, params):
    """POST /opt/browser/open?url=... — launch Chrome, connect CDP, navigate."""
    global _proc, _cdp
    if not _proc or _proc.poll() is not None:
        chrome = _find_chrome()
        tmpdir = os.path.join(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp")),
                              "elastik-chrome")
        _proc = subprocess.Popen([
            chrome, f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={tmpdir}",
            "--no-first-run", "--no-default-browser-check",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{_CDP_PORT}/json", timeout=1).read())
                page = next((t for t in targets if t.get("type") == "page"), None)
                if not page:
                    # no page tab — create one
                    urllib.request.urlopen(f"http://127.0.0.1:{_CDP_PORT}/json/new")
                    await asyncio.sleep(0.5)
                    targets = json.loads(urllib.request.urlopen(
                        f"http://127.0.0.1:{_CDP_PORT}/json", timeout=1).read())
                    page = next((t for t in targets if t.get("type") == "page"), targets[0])
                _cdp = CDP(page["webSocketDebuggerUrl"])
                _cdp.send("Page.enable")
                break
            except Exception:
                await asyncio.sleep(0.3)
        else:
            return {"error": "Chrome didn't start", "_status": 500}
    url = params.get("url", "")
    if not url:
        url = (body if isinstance(body, str) else body.decode()).strip()
    if url:
        _cdp.send("Page.navigate", {"url": url})
        await asyncio.sleep(1)
    return {"ok": True, "url": url}


async def handle_eval(method, body, params):
    """POST /opt/browser/eval — execute JS in page, return result."""
    if not _cdp:
        return {"error": "no browser. POST /opt/browser/open first", "_status": 400}
    code = body if isinstance(body, str) else body.decode()
    r = _cdp.send("Runtime.evaluate", {"expression": code, "returnByValue": True})
    if "error" in r:
        return {"error": r["error"], "_status": 500}
    val = r.get("result", {}).get("value", "")
    return {"_html": str(val) if val is not None else "", "_status": 200}


async def handle_screenshot(method, body, params):
    """GET /opt/browser/screenshot — PNG as base64 data URI."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    r = _cdp.send("Page.captureScreenshot", {"format": "png"})
    if "error" in r:
        return {"error": r["error"], "_status": 500}
    return {"_html": "data:image/png;base64," + r["data"], "_status": 200}


async def handle_click(method, body, params):
    """POST /opt/browser/click?x=100&y=200 — mouse click at coordinates."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    x, y = float(params.get("x", 0)), float(params.get("y", 0))
    _cdp.send("Input.dispatchMouseEvent",
              {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    _cdp.send("Input.dispatchMouseEvent",
              {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    return {"ok": True, "x": x, "y": y}


async def handle_type(method, body, params):
    """POST /opt/browser/type — type text into focused element."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    text = body if isinstance(body, str) else body.decode()
    _cdp.send("Input.insertText", {"text": text})
    return {"ok": True, "typed": len(text)}


async def handle_dom(method, body, params):
    """GET /opt/browser/dom — page innerText, plain."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    r = _cdp.send("Runtime.evaluate",
                   {"expression": "document.body.innerText", "returnByValue": True})
    val = r.get("result", {}).get("value", "")
    return {"_html": str(val) if val else "", "_status": 200}


async def handle_close(method, body, params):
    """POST /opt/browser/close — kill Chrome process."""
    global _proc, _cdp
    if _cdp:
        _cdp.close()
        _cdp = None
    if _proc:
        _proc.terminate()
        _proc = None
    return {"ok": True}


ROUTES = {
    "/opt/browser/open": handle_open,
    "/opt/browser/eval": handle_eval,
    "/opt/browser/screenshot": handle_screenshot,
    "/opt/browser/click": handle_click,
    "/opt/browser/type": handle_type,
    "/opt/browser/dom": handle_dom,
    "/opt/browser/close": handle_close,
}
