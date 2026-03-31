"""Local network peer discovery -- multicast on 224.0.251.99:3006.

Every 30s: multicast self, collect peers, write to discovery world.
Trust route: human clicks trust in renderer, writes to config-endpoints.
"""
import asyncio, json, os, socket, time

DESCRIPTION = "Local network peer discovery (multicast + broadcast fallback)"
CRON = 30
ROUTES = {}

_MCAST_GROUP = "224.0.251.99"
_PORT = 3006
_NODE = os.getenv("ELASTIK_NODE", socket.gethostname())
_APP_PORT = int(os.getenv("ELASTIK_PORT", "3004"))
_SEED_PEERS = [ip.strip() for ip in os.getenv("ELASTIK_PEERS", "").split(",") if ip.strip()]
_peers = {}  # ip -> {name, port, version, last_seen}
_sock = None


def _init_socket():
    global _sock
    if _sock: return
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _sock.setblocking(False)
    _sock.bind(("0.0.0.0", _PORT))
    # Join multicast group
    group = socket.inet_aton(_MCAST_GROUP)
    mreq = group + socket.inet_aton("0.0.0.0")
    _sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


def _broadcast():
    msg = json.dumps({"name": _NODE, "port": _APP_PORT, "v": "1.10"}).encode()
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
            data, addr = _sock.recvfrom(1024)
            peer = json.loads(data)
            ip = addr[0]
            # Skip self
            if peer.get("name") == _NODE and peer.get("port") == _APP_PORT:
                continue
            _peers[ip] = {
                "name": peer.get("name", "?"),
                "port": peer.get("port", 3004),
                "version": peer.get("v", "?"),
                "last_seen": now,
            }
        except (BlockingIOError, OSError):
            break
    for ip in [k for k, v in _peers.items() if now - v["last_seen"] > 120]:
        del _peers[ip]


async def _tick():
    _init_socket()
    _broadcast()
    _collect()
    # Write to discovery world for renderer
    snap = {
        "node": _NODE,
        "port": _APP_PORT,
        "peers": {ip: {"name": p["name"], "port": p["port"], "version": p["version"],
                        "ago": int(time.time() - p["last_seen"])}
                  for ip, p in _peers.items()},
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
    return {"node": _NODE, "peers": {ip: p for ip, p in _peers.items()}}


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
