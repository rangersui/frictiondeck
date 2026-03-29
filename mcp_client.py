"""Universal MCP client. Stdlib only. Sync. No frameworks.

Usage:
    client = MCPClient('npx', ['@softeria/ms-365-mcp-server'])
    client.initialize()
    tools = client.list_tools()
    result = client.call_tool('list-emails', {'count': 20})
    client.close()

    # or context manager:
    with MCPClient('npx', ['@softeria/ms-365-mcp-server']) as client:
        result = client.call_tool('list-emails', {'count': 20})
"""
import json
import subprocess
import threading
import sys


class MCPClient:
    def __init__(self, cmd, args=None, env=None, timeout=30):
        self.cmd = cmd
        self.args = args or []
        self.env = env
        self.timeout = timeout
        self._id = 0
        self._proc = None
        self._lock = threading.Lock()
        self._buf = b""

    def __enter__(self):
        self._start()
        self.initialize()
        return self

    def __exit__(self, *_):
        self.close()

    def _start(self):
        self._proc = subprocess.Popen(
            [self.cmd] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            shell=(sys.platform == "win32"),
        )

    def _next_id(self):
        self._id += 1
        return self._id

    def _send(self, method, params=None):
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        raw = json.dumps(msg) + "\n"
        with self._lock:
            self._proc.stdin.write(raw.encode())
            self._proc.stdin.flush()
        return rid

    def _recv(self, expected_id):
        deadline = None
        if self.timeout:
            import time
            deadline = time.time() + self.timeout
        while True:
            if deadline and time.time() > deadline:
                raise TimeoutError(f"MCP server did not respond within {self.timeout}s")
            line = self._readline()
            if not line:
                rc = self._proc.poll()
                if rc is not None:
                    err = self._proc.stderr.read().decode(errors="replace")
                    raise ConnectionError(f"MCP server exited ({rc}): {err}")
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == expected_id:
                if "error" in msg:
                    e = msg["error"]
                    raise RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                return msg.get("result")
            # notification or wrong id — skip

    def _readline(self):
        """Read one newline-delimited message from stdout."""
        while b"\n" not in self._buf:
            chunk = self._proc.stdout.read1(4096) if hasattr(self._proc.stdout, 'read1') else self._proc.stdout.read(1)
            if not chunk:
                if self._buf:
                    line = self._buf
                    self._buf = b""
                    return line.decode(errors="replace")
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def initialize(self):
        if not self._proc:
            self._start()
        rid = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "elastik-mcp-client", "version": "1.0.0"},
        })
        result = self._recv(rid)
        # send initialized notification (no id)
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        with self._lock:
            self._proc.stdin.write(notif.encode())
            self._proc.stdin.flush()
        return result

    def list_tools(self):
        rid = self._send("tools/list")
        result = self._recv(rid)
        return result.get("tools", []) if result else []

    def call_tool(self, name, arguments=None):
        rid = self._send("tools/call", {"name": name, "arguments": arguments or {}})
        return self._recv(rid)

    def list_resources(self):
        rid = self._send("resources/list")
        result = self._recv(rid)
        return result.get("resources", []) if result else []

    def read_resource(self, uri):
        rid = self._send("resources/read", {"uri": uri})
        return self._recv(rid)

    def list_prompts(self):
        rid = self._send("prompts/list")
        result = self._recv(rid)
        return result.get("prompts", []) if result else []

    def get_prompt(self, name, arguments=None):
        rid = self._send("prompts/get", {"name": name, "arguments": arguments or {}})
        return self._recv(rid)

    def close(self):
        if not self._proc:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
            self._proc.wait()
        self._proc = None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python mcp_client.py <cmd> [args...]\n  e.g. python mcp_client.py npx @softeria/ms-365-mcp-server")
        sys.exit(1)
    cmd, args = sys.argv[1], sys.argv[2:]
    with MCPClient(cmd, args) as c:
        print("Server info:", json.dumps(c.initialize() if False else "connected"))
        tools = c.list_tools()
        print(f"\n{len(tools)} tools:")
        for t in tools:
            desc = t.get("description", "")[:60]
            print(f"  {t['name']:30s} {desc}")
