"""browser — Chrome remote control via CDP. Zero MCP, zero npm.

POST /opt/browser/open?url=https://...   launch Chrome + navigate
GET  /opt/browser/screenshot             PNG base64
GET  /opt/browser/extract?s=h1           text by CSS selector
POST /opt/browser/click?x=100&y=200      click
POST /opt/browser/type                   type text (body)
POST /opt/browser/scroll?y=500           scroll
POST /opt/browser/back                   history back
POST /opt/browser/close                  kill Chrome

No /eval. The verb doesn't exist in this universe.
AI sees pages, reads text, clicks buttons. It cannot run arbitrary JS.
Each verb has a bounded blast radius.

Chrome launches incognito — no cookies, no logins, no sessions.
Even on a hostile page, there's nothing to steal.

Chrome DevTools Protocol over WebSocket. No Puppeteer. No Playwright.
No MCP. Just the 20-year-old debugging port.
"""
DESCRIPTION = "/opt/browser — Chrome via CDP. See, read, click. No eval."
AUTH = "approve"  # browser control is root-only
ROUTES = {}

import asyncio, base64, json, os, platform, secrets, socket, struct, subprocess, threading
import urllib.request

# ── CDP WebSocket client — stdlib only ───────────────────────────────

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
        self.sock.settimeout(30)
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method, params=None):
        with self._lock:
            self._id += 1
            mid = self._id
            msg = json.dumps({"id": mid, "method": method, "params": params or {}}).encode()
            self._ws_send(msg)
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
                self._readn(4)
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

    def _extract(self, selector):
        """Get text content of elements matching a CSS selector.
        Uses DOM.querySelectorAll — not eval. Bounded."""
        # Get document root
        doc = self.send("DOM.getDocument", {"depth": 0})
        root_id = doc.get("root", {}).get("nodeId", 0)
        if not root_id:
            return []
        # Query
        nodes = self.send("DOM.querySelectorAll", {"nodeId": root_id, "selector": selector})
        node_ids = nodes.get("nodeIds", [])
        results = []
        for nid in node_ids[:50]:  # cap at 50 results
            outer = self.send("DOM.getOuterHTML", {"nodeId": nid})
            html = outer.get("outerHTML", "")
            # Strip tags for plain text
            import re
            text = re.sub(r"<[^>]+>", "", html).strip()
            if text:
                results.append(text)
        return results


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
    """POST /opt/browser/open?url=... — launch incognito Chrome, connect CDP, navigate."""
    global _proc, _cdp
    if not _proc or _proc.poll() is not None:
        chrome = _find_chrome()
        tmpdir = os.path.join(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp")),
                              "elastik-chrome")
        _proc = subprocess.Popen([
            chrome, f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={tmpdir}",
            "--no-first-run", "--no-default-browser-check",
            "--incognito",  # no cookies, no sessions, nothing to steal
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{_CDP_PORT}/json", timeout=1).read())
                page = next((t for t in targets if t.get("type") == "page"), None)
                if not page:
                    urllib.request.urlopen(f"http://127.0.0.1:{_CDP_PORT}/json/new")
                    await asyncio.sleep(0.5)
                    targets = json.loads(urllib.request.urlopen(
                        f"http://127.0.0.1:{_CDP_PORT}/json", timeout=1).read())
                    page = next((t for t in targets if t.get("type") == "page"), targets[0])
                _cdp = CDP(page["webSocketDebuggerUrl"])
                _cdp.send("Page.enable")
                _cdp.send("DOM.enable")
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


async def handle_screenshot(method, body, params):
    """GET /opt/browser/screenshot — PNG as base64 data URI."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    r = _cdp.send("Page.captureScreenshot", {"format": "png"})
    if "error" in r:
        return {"error": r["error"], "_status": 500}
    return {"_html": "data:image/png;base64," + r["data"], "_status": 200}


async def handle_extract(method, body, params):
    """GET /opt/browser/extract?s=h1 — text content of elements matching CSS selector.
    Not eval. DOM.querySelectorAll. Bounded to 50 results."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    selector = params.get("s", "") or params.get("selector", "") or "body"
    results = _cdp._extract(selector)
    if len(results) == 1:
        return {"_html": results[0], "_status": 200}
    return {"results": results, "count": len(results)}


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


async def handle_scroll(method, body, params):
    """POST /opt/browser/scroll?y=500 — scroll page."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    x = float(params.get("x", 0))
    y = float(params.get("y", 300))
    _cdp.send("Input.dispatchMouseEvent",
              {"type": "mouseWheel", "x": 100, "y": 100,
               "deltaX": x, "deltaY": y})
    return {"ok": True}


async def handle_back(method, body, params):
    """POST /opt/browser/back — history back."""
    if not _cdp:
        return {"error": "no browser", "_status": 400}
    history = _cdp.send("Page.getNavigationHistory")
    idx = history.get("currentIndex", 0)
    if idx > 0:
        entries = history.get("entries", [])
        _cdp.send("Page.navigateToHistoryEntry", {"entryId": entries[idx - 1]["id"]})
    return {"ok": True}


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
    "/opt/browser/screenshot": handle_screenshot,
    "/opt/browser/extract": handle_extract,
    "/opt/browser/click": handle_click,
    "/opt/browser/type": handle_type,
    "/opt/browser/scroll": handle_scroll,
    "/opt/browser/back": handle_back,
    "/opt/browser/close": handle_close,
}
