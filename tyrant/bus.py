"""elastik bus — SQLite read/write endpoint. No plugins, no logic, no opinions."""
import asyncio, json, os, sqlite3, ssl, sys
from pathlib import Path

DATA = Path("data")
PORT = int(os.getenv("ELASTIK_PORT", "3005"))
HOST = os.getenv("ELASTIK_HOST", "0.0.0.0")
AUTH = os.getenv("ELASTIK_TOKEN", "")  # empty = no auth (local dev only)
INDEX = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
_db = {}

def conn(name):
    if name not in _db:
        d = DATA / name; d.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(d / "universe.db"), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript("""
            CREATE TABLE IF NOT EXISTS stage_meta(id INTEGER PRIMARY KEY CHECK(id=1),
                stage_html TEXT DEFAULT '', version INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
            INSERT OR IGNORE INTO stage_meta(id,updated_at) VALUES(1,datetime('now'));
        """)
        _db[name] = c
    return _db[name]

async def handle(reader, writer):
    try:
        line = await asyncio.wait_for(reader.readline(), 5)
        if not line: writer.close(); return
        parts = line.decode("utf-8", "replace").strip().split(" ", 2)
        if len(parts) < 2: writer.close(); return
        method, full_path = parts[0], parts[1]
        headers = []
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""): break
            decoded = h.decode("utf-8", "replace").strip()
            if ": " in decoded:
                k, v = decoded.split(": ", 1)
                headers.append([k.lower(), v])
        cl = int(next((v for k, v in headers if k == "content-length"), 0))
        body = (await reader.readexactly(cl)).decode("utf-8", "replace") if cl else ""
        path = full_path.split("?")[0].rstrip("/") or "/"
        segs = [s for s in path.split("/") if s]
        # CORS
        cors = b"Access-Control-Allow-Origin: *\r\nAccess-Control-Allow-Headers: *\r\nAccess-Control-Allow-Methods: GET,POST,OPTIONS\r\n"
        if method == "OPTIONS":
            writer.write(b"HTTP/1.1 204 No Content\r\n" + cors + b"\r\n")
            await writer.drain(); writer.close(); return
        status, ct, out = 200, "application/json", ""
        # Auth: write = execute. Control who can write.
        if AUTH and method == "POST":
            tok = next((v for k, v in headers if k == "authorization"), "").replace("Bearer ", "")
            if tok != AUTH:
                status, out = 403, '{"error":"unauthorized"}'
                resp = f"HTTP/1.1 {status} OK\r\n".encode() + cors
                resp += f"Content-Type: {ct}\r\nContent-Length: {len(out.encode())}\r\n\r\n".encode() + out.encode()
                writer.write(resp); await writer.drain(); writer.close(); return
        if method == "GET" and path == "/":
            ct, out = "text/html", INDEX
        elif method == "GET" and path == "/stages":
            stages = []
            if DATA.exists():
                for d in sorted(DATA.iterdir()):
                    if d.is_dir() and (d / "universe.db").exists():
                        r = conn(d.name).execute("SELECT version,updated_at FROM stage_meta WHERE id=1").fetchone()
                        stages.append({"name": d.name, "version": r["version"], "updated_at": r["updated_at"]})
            out = json.dumps(stages)
        elif len(segs) == 2 and segs[1] == "read":
            r = conn(segs[0]).execute("SELECT stage_html,version FROM stage_meta WHERE id=1").fetchone()
            out = json.dumps({"stage_html": r["stage_html"], "version": r["version"], "pending_js": "", "js_result": ""})
        elif method == "POST" and len(segs) == 2 and segs[1] == "write":
            c = conn(segs[0])
            c.execute("UPDATE stage_meta SET stage_html=?,version=version+1,updated_at=datetime('now') WHERE id=1", (body,)); c.commit()
            out = json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]})
        elif method == "POST" and len(segs) == 2 and segs[1] == "append":
            c = conn(segs[0])
            c.execute("UPDATE stage_meta SET stage_html=stage_html||?,version=version+1,updated_at=datetime('now') WHERE id=1", (body,)); c.commit()
            out = json.dumps({"version": c.execute("SELECT version FROM stage_meta WHERE id=1").fetchone()["version"]})
        else:
            status, out = 404, '{"error":"not found"}'
        resp = f"HTTP/1.1 {status} OK\r\n".encode() + cors
        resp += f"Content-Type: {ct}\r\nContent-Length: {len(out.encode())}\r\n\r\n".encode() + out.encode()
        writer.write(resp); await writer.drain()
    except Exception as e:
        try: writer.write(b"HTTP/1.1 500 Error\r\n\r\n"); await writer.drain()
        except: pass
    finally:
        try: writer.close()
        except: pass

def _ensure_cert():
    """Generate self-signed cert on first run using Python stdlib only."""
    cert_path, key_path = Path("bus.crt"), Path("bus.key")
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    import datetime, hashlib, struct, secrets
    # --- RSA key generation via Python's ssl (OpenSSL is always linked) ---
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(65537, 2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "elastik-bus")])
        cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        print("  cert: generated bus.crt + bus.key (cryptography)")
        return str(cert_path), str(key_path)
    except ImportError:
        pass
    # Fallback: try openssl CLI
    import subprocess
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path), "-days", "3650",
            "-subj", "/CN=elastik-bus"], check=True, capture_output=True)
        print("  cert: generated bus.crt + bus.key (openssl)")
        return str(cert_path), str(key_path)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    print("  warn: no cryptography pkg, no openssl CLI")
    print("  run: pip install cryptography")
    print("  falling back to HTTP (no WebGPU on remote clients)")
    return None, None

async def main():
    cert_file, key_file = _ensure_cert()
    if cert_file:
        sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sc.load_cert_chain(cert_file, key_file)
        srv = await asyncio.start_server(handle, HOST, PORT, ssl=sc)
        print(f"  elastik bus -> https://{HOST}:{PORT}  [tyrant mode, TLS]")
    else:
        srv = await asyncio.start_server(handle, HOST, PORT)
        print(f"  elastik bus -> http://{HOST}:{PORT}  [tyrant mode, no TLS]")
    await srv.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
