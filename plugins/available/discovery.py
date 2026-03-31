"""Local network peer discovery + capability announcement.

Multicast + gossip on 224.0.251.99:3006.
Every 30s: announce self + capabilities, collect peers, gossip.
Like BGP: discovery tells you who exists, capabilities tell you
what's behind each door.
"""
import asyncio, json, os, socket, time
from urllib.request import urlopen

NEEDS = ["_plugin_meta"]
DESCRIPTION = "Peer discovery + capability announcement (multicast + gossip)"
CRON = 30
ROUTES = {}

_MCAST_GROUP = "224.0.251.99"
_PORT = 3006
_NODE = os.getenv("ELASTIK_NODE", socket.gethostname())
_APP_PORT = int(os.getenv("ELASTIK_PORT", "3004"))
_SEED_PEERS = [ip.strip() for ip in os.getenv("ELASTIK_PEERS", "").split(",") if ip.strip()]
_peers = {}   # ip -> {name, port, version, caps, last_seen} — direct UDP
_known = {}   # ip -> {name, port, version, caps, via, last_seen} — gossip
_sock = None
_my_ip = None


def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _my_caps():
    """Build capability summary from live plugin metadata."""
    plugins = [m["name"] for m in _plugin_meta]
    routes = []
    for m in _plugin_meta:
        routes.extend(m.get("routes", []))
    return {"plugins": plugins, "routes": routes}


def _init_socket():
    global _sock, _my_ip
    if _sock: return
    _my_ip = _get_local_ip()
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _sock.setblocking(False)
    try:
        _sock.bind(("0.0.0.0", _PORT))
    except OSError:
        pass  # port busy -- can still send unicast/seed
    # Join multicast group (may fail if bind failed)
    try:
        group = socket.inet_aton(_MCAST_GROUP)
        mreq = group + socket.inet_aton("0.0.0.0")
        _sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError:
        pass


def _broadcast():
    caps = _my_caps()
    msg = json.dumps({
        "name": _NODE, "port": _APP_PORT, "v": "1.10",
        "plugins": caps["plugins"],
    }).encode()
    # Primary: multicast
    try: _sock.sendto(msg, (_MCAST_GROUP, _PORT))
    except OSError: pass
    # Fallback: broadcast
    try: _sock.sendto(msg, ("255.255.255.255", _PORT))
    except OSError: pass
    # Seed peers: containers, cloud, cross-subnet
    for ip in _SEED_PEERS:
        try: _sock.sendto(msg, (ip, _PORT))
        except OSError: pass
    # Unicast reply to known peers (iOS can't send multicast)
    for ip in list(_peers):
        if ip not in _SEED_PEERS:
            try: _sock.sendto(msg, (ip, _PORT))
            except OSError: pass


def _collect():
    """Read all pending datagrams from buffer. Ignore self."""
    now = time.time()
    while True:
        try:
            data, addr = _sock.recvfrom(2048)
            peer = json.loads(data)
            ip = addr[0]
            # Skip self
            if peer.get("name") == _NODE and peer.get("port") == _APP_PORT and ip == _my_ip:
                continue
            _peers[ip] = {
                "name": peer.get("name", "?"),
                "port": peer.get("port", 3004),
                "version": peer.get("v", "?"),
                "plugins": peer.get("plugins", []),
                "last_seen": now,
            }
            # If we learned about this via gossip before, promote to direct
            _known.pop(ip, None)
        except (BlockingIOError, OSError):
            break
    for ip in [k for k, v in _peers.items() if now - v["last_seen"] > 120]:
        del _peers[ip]


def _gossip():
    """Ask each direct peer who they know + what they can do."""
    now = time.time()
    for ip, peer in list(_peers.items()):
        try:
            r = urlopen(f"http://{ip}:{peer['port']}/proxy/discovery/peers", timeout=2)
            data = json.loads(r.read())
            # Update direct peer's full capabilities from HTTP response
            if "caps" in data:
                peer["plugins"] = data["caps"].get("plugins", peer.get("plugins", []))
                peer["routes"] = data["caps"].get("routes", [])
            for their_ip, their_peer in data.get("peers", {}).items():
                # Skip self and already-direct peers
                if their_ip == _my_ip or their_ip in _peers:
                    continue
                _known[their_ip] = {
                    "name": their_peer.get("name", "?"),
                    "port": their_peer.get("port", 3004),
                    "version": their_peer.get("version", "?"),
                    "plugins": their_peer.get("plugins", []),
                    "via": ip,
                    "last_seen": now,
                }
        except Exception:
            pass
    # Expire gossip peers not refreshed in 120s
    for ip in [k for k, v in _known.items() if now - v["last_seen"] > 120]:
        del _known[ip]


async def _tick():
    _init_socket()
    _broadcast()
    _collect()
    _gossip()
    # Write to discovery world for renderer
    now = time.time()
    snap = {
        "node": _NODE,
        "port": _APP_PORT,
        "caps": _my_caps(),
        "peers": {ip: {"name": p["name"], "port": p["port"], "version": p["version"],
                        "plugins": p.get("plugins", []),
                        "ago": int(now - p["last_seen"])}
                  for ip, p in _peers.items()},
        "known": {ip: {"name": p["name"], "port": p["port"], "version": p["version"],
                        "plugins": p.get("plugins", []),
                        "via": p["via"], "ago": int(now - p["last_seen"])}
                  for ip, p in _known.items()},
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = "<!--use:renderer-discovery-->\n" + json.dumps(snap)
    c = conn("discovery")
    old = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    if old is None or old["stage_html"] != payload:
        c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
                  (payload,))
        c.commit()

CRON_HANDLER = _tick


async def handle_peers(method, body, params):
    return {"node": _NODE, "caps": _my_caps(),
            "peers": {ip: p for ip, p in _peers.items()},
            "known": {ip: p for ip, p in _known.items()}}


async def handle_trust(method, body, params):
    """Add a discovered peer to config-endpoints."""
    try:
        req = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError):
        return {"error": "invalid json"}
    ip = req.get("ip", "").strip()
    port = req.get("port", 3004)
    name = req.get("name", "").strip()
    token = req.get("token", "").strip()
    if not ip:
        return {"error": "ip required"}
    url = f"http://{ip}:{port}"
    # Read current endpoints
    c = conn("config-endpoints")
    row = c.execute("SELECT stage_html FROM stage_meta WHERE id=1").fetchone()
    try:
        endpoints = json.loads(row["stage_html"]) if row and row["stage_html"] else {}
    except (json.JSONDecodeError, TypeError):
        endpoints = {}
    # Add/update this peer
    endpoints[name or ip] = {"url": url, "token": token}
    c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1",
              (json.dumps(endpoints, indent=2),))
    c.commit()
    return {"ok": True, "added": name or ip, "url": url}

ROUTES["/proxy/discovery/peers"] = handle_peers
ROUTES["/proxy/discovery/trust"] = handle_trust
