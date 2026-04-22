"""fstab — mount local directories AND external sources under /mnt/*.

Gate: etc/fstab world. One line per mount:

  /Users/ranger/projects          /mnt/work    rw
  /home/ranger/docs               /mnt/docs    ro
  https://api.example.com         /mnt/api     ro,bearer=xyz

Source field (first column) is either an absolute local path (legacy,
unchanged) or a scheme://endpoint URI (Phase 1: https/http only).
Mode + optional adapter options fold into the third column as
comma-delimited tokens — `ro` or `ro,bearer=xyz` etc. This keeps the
line a three-field shape so rsplit(None, 2) still preserves source
paths with spaces (Brave's `~/Library/Application Support/...` is the
load-bearing example in README).

Write etc/fstab → T3 (approve). That IS the permission model.
No container check. No mode flag. You control what's mounted.

Routes:
  GET  /mnt/                      list mount points
  GET  /mnt/{name}/               list directory (file mounts)
                                  or proxy the upstream root (https)
  GET  /mnt/{name}/path/file      read file / proxy GET upstream path
  POST /mnt/{name}/path/file      write file (file rw only, auth required);
                                  https mounts 405 in Phase 1

Adapter contract (one per scheme, dispatched by entry["kind"]):
  adapter(entry, method, rest_path, params) -> tuple or dict
    read success  : (body_bytes, content_type, version_token)
    write / other : {"_status": int, ...} dict (passed through to client)
  exceptions:
    _MethodNotAllowed   -> 405
    _AdapterFetchError  -> e.status (upstream failure or 502)
    _TraversalError     -> 403
    _AdapterUnavailable -> 501 (e.g. pip install ... needed)

Dispatcher wraps read-tuples into {"_body": bytes, "_ct": ct,
"_headers":[("X-Mount-Version", ver)]}. Writes and listings return
their dicts directly.
"""
DESCRIPTION = "/mnt/ — filesystem + external sources via etc/fstab"
AUTH = "none"
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib import error as _urlerr
from urllib import request as _urlreq

import server

_MAX_FILE = 5 * 1024 * 1024  # 5 MB
_HTTPS_TIMEOUT = 30          # seconds


# ====================================================================
# exception classes
# ====================================================================

class _AdapterFetchError(Exception):
    """Upstream backend returned non-2xx or was unreachable."""
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


class _AdapterUnavailable(Exception):
    """Adapter code failed to import (missing optional dep)."""


class _MethodNotAllowed(Exception):
    """Adapter refuses this HTTP method (wrong mode / read-only scheme)."""


class _TraversalError(Exception):
    """Resolved path escaped the mount root."""


# ====================================================================
# fstab parsing — shared with /dev/db via server._parse_fstab_line /
# server._read_fstab. Grammar invariants (rsplit(None, 2), file-kind
# default, comma-delimited opts in the mode column) live there now;
# this plugin only defines the adapter behaviour per scheme.
# ====================================================================

def _find_mount(entries, name):
    """Return entry by mount name, or None."""
    for entry in entries:
        if entry["name"] == name:
            return entry
    return None


# ====================================================================
# traversal guard (unchanged logic, preserved against refactor)
# ====================================================================

def _safe_resolve(root, rel):
    """Resolve rel under root. Reject traversal. Returns abs_path.

    Raises _TraversalError if the resolved path escapes root, including
    via .. segments or symlink shenanigans. This is the same logic the
    original plugin shipped with; keep it byte-for-byte so the refactor
    cannot loosen it."""
    if not rel or rel == "/":
        return root
    for part in rel.replace("\\", "/").split("/"):
        if part == "..":
            raise _TraversalError("'..' segment in path")
    full = os.path.normpath(os.path.join(root, rel))
    try:
        if os.path.commonpath([full, os.path.normpath(root)]) != os.path.normpath(root):
            raise _TraversalError("resolved path escaped mount root")
    except ValueError:
        raise _TraversalError("path resolution failed")
    return full


# ====================================================================
# file:// adapter (preserves existing read + write behaviour)
# ====================================================================

def _listing_json_bytes(full, subpath, mount_name, mode):
    """Directory listing as JSON bytes. Same shape the original
    handler produced — callers (including /dev/db and future clients)
    depend on {path, mount, mode, entries} with {name, type, size}."""
    entries = []
    for name in sorted(os.listdir(full)):
        if name.startswith("."):
            continue                        # skip dotfiles
        fp = os.path.join(full, name)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(fp) else "file",
            "size": os.path.getsize(fp) if os.path.isfile(fp) else 0,
        })
    payload = {"path": subpath or "/", "mount": mount_name,
               "mode": mode, "entries": entries}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _adapter_file(entry, method, rest_path, params):
    """Local filesystem adapter. Preserves:
      - read response shape (dir listing JSON + file bytes)
      - write behaviour on rw mounts (POST, existing auth gate,
        5MB cap, JSON ack with ok/path/size)
      - _MAX_FILE cap on both read and write
      - traversal guard
    Behaviour change from pre-v0.2 file reads: a single-file GET now
    returns the file's raw bytes with a content-type inferred from
    the extension, rather than a JSON envelope with
    {content, size}. This aligns the read path with /shaped/'s
    tuple-returning contract and lets external tooling (Excel,
    browsers, curl) consume the file directly at its real MIME."""
    local_root = entry["source"]
    if not os.path.isdir(local_root):
        raise _AdapterFetchError(
            f"mount path does not exist: {local_root}", status=500)

    full = _safe_resolve(local_root, rest_path)

    # ── reads ────────────────────────────────────────────────
    if method in ("GET", "HEAD"):
        if not os.path.exists(full):
            raise _AdapterFetchError("not found", status=404)
        st = os.stat(full)
        ver = f"mtime:{st.st_mtime_ns}"
        if os.path.isdir(full):
            # PermissionError on listdir or per-file stat(2) during
            # iteration must surface as a clean 403, matching the
            # single-file read path's PermissionError handler below.
            # Before the refactor the old handler guarded listing the
            # same way — keep that contract so unreadable mounts do
            # not leak as 500s.
            try:
                body = _listing_json_bytes(
                    full, rest_path, entry["name"], entry["mode"])
            except PermissionError:
                raise _AdapterFetchError("permission denied", status=403)
            return body, "application/json; charset=utf-8", ver
        # file read
        if st.st_size > _MAX_FILE:
            raise _AdapterFetchError(
                f"file too large ({st.st_size} > {_MAX_FILE})", status=413)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except PermissionError:
            raise _AdapterFetchError("permission denied", status=403)
        ext = (os.path.splitext(full)[1][1:] or "plain").lower()
        ct = server._ext_to_ct(ext)
        return data, ct, ver

    # ── writes ───────────────────────────────────────────────
    if method == "POST":
        if entry["mode"] != "rw":
            raise _MethodNotAllowed("mount is read-only")
        scope = params.get("_scope", {})
        if not server._check_auth(scope):
            return {"_status": 401,
                    "error": "unauthorized",
                    "_headers": [["www-authenticate", 'Basic realm="elastik"']]}
        body = params.get("_body_raw", b"")
        if not isinstance(body, (bytes, bytearray)):
            body = body.encode("utf-8") if isinstance(body, str) else b""
        if len(body) > _MAX_FILE:
            return {"_status": 413, "error": "file too large"}
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(body)
        return {"ok": True, "path": rest_path, "size": len(body)}

    raise _MethodNotAllowed(
        f"file://<mode={entry['mode']}> does not accept {method}")


# ====================================================================
# https:// adapter (new; stdlib-only)
# ====================================================================

def _adapter_https(entry, method, rest_path, params):
    """HTTP(S) proxy adapter. Phase 1: read-only.

    GET /mnt/<name>/<rest>  → GET https://<source>/<rest>
    HEAD                    → same as GET (server layer elides body)
    anything else           → _MethodNotAllowed (405)

    Auth: if entry["opts"] contains `bearer=<value>`, attach as
    Authorization: Bearer header on the upstream request. Other
    credential mechanisms (cred=etc/creds/...) are a later phase.

    Version token: upstream ETag if present; otherwise
    `len=N;head=<first-32-bytes-hex>`. Always non-empty so /shaped/
    can include it in the cache key without specials."""
    if method not in ("GET", "HEAD"):
        raise _MethodNotAllowed(
            "https:// mounts are read-only in Phase 1")

    scheme = entry["kind"]                  # "http" or "https"
    base = f"{scheme}://{entry['source'].rstrip('/')}"
    url = base + "/" + rest_path.lstrip("/") if rest_path else base

    headers = {}
    for tok in entry.get("opts", []):
        if tok.startswith("bearer="):
            headers["Authorization"] = "Bearer " + tok[len("bearer="):]

    req = _urlreq.Request(url, headers=headers, method="GET")
    try:
        with _urlreq.urlopen(req, timeout=_HTTPS_TIMEOUT) as resp:
            # Bounded read — symmetric with the file adapter's
            # _MAX_FILE cap. /mnt/* reads are unauthenticated by
            # design (AUTH="none"), so an unbounded resp.read() would
            # let any configured remote mount proxy arbitrarily large
            # upstream bodies into memory. Reading _MAX_FILE + 1
            # detects overflow without draining the socket past it.
            body = resp.read(_MAX_FILE + 1)
            if len(body) > _MAX_FILE:
                raise _AdapterFetchError(
                    f"upstream response too large (> {_MAX_FILE} bytes)",
                    status=413)
            ct = resp.headers.get("Content-Type",
                                  "application/octet-stream")
            etag = resp.headers.get("ETag")
    except _AdapterFetchError:
        raise                                   # our own cap trip
    except _urlerr.HTTPError as e:
        raise _AdapterFetchError(
            f"upstream HTTP {e.code}", status=e.code)
    except (_urlerr.URLError, TimeoutError, OSError) as e:
        raise _AdapterFetchError(
            f"upstream unreachable: {e}", status=502)

    if etag:
        ver = "etag:" + etag.strip('"')
    else:
        head_hex = body[:32].hex() if body else "empty"
        ver = f"len={len(body)};head={head_hex}"
    return body, ct, ver


# ====================================================================
# dispatch table
# ====================================================================

_ADAPTERS = {
    "file":  _adapter_file,
    "https": _adapter_https,
    "http":  _adapter_https,        # same code; `http://` permitted
                                    # for dev/intranet, warn in README
}


# ====================================================================
# handle()
# ====================================================================

async def handle(method, body, params):
    """Dispatch /mnt/<name>/<rest> to the adapter for <name>'s scheme.

    Methods are NOT blanket-rejected here. Each adapter decides which
    of GET / HEAD / POST / PUT / DELETE it supports via
    _MethodNotAllowed. File mode=rw mounts keep existing POST write
    semantics; https/http mounts are read-only in Phase 1."""
    scope = params.get("_scope") or {}
    path = scope.get("path", "/mnt")
    rest = path[len("/mnt"):].strip("/")

    entries = server._read_fstab()

    # /mnt/ (no tail) — listing. Shape preserved from v0.1 exactly.
    if not rest:
        if method in ("GET", "HEAD"):
            return {"mounts": [
                {"name": e["name"], "path": e["source"], "mode": e["mode"]}
                if e["kind"] == "file"
                else {"name": e["name"], "path": f"{e['kind']}://{e['source']}",
                      "mode": e["mode"]}
                for e in entries
            ]}
        return {"_status": 405,
                "error": "listing endpoint accepts GET/HEAD only; "
                         "edit /etc/fstab to change mounts"}

    # /mnt/<name>/<rest_path>
    slash = rest.find("/")
    if slash < 0:
        mount_name, rest_path = rest, ""
    else:
        mount_name, rest_path = rest[:slash], rest[slash + 1:]

    entry = _find_mount(entries, mount_name)
    if entry is None:
        return {"_status": 404,
                "error": f"mount not found: {mount_name}. "
                         f"write etc/fstab first."}

    adapter = _ADAPTERS.get(entry["kind"])
    if adapter is None:
        return {"_status": 501,
                "error": f"no adapter for scheme {entry['kind']!r}"}

    try:
        result = adapter(entry, method, rest_path, params)
    except _MethodNotAllowed as e:
        return {"_status": 405, "error": str(e)}
    except _AdapterFetchError as e:
        return {"_status": e.status, "error": f"adapter: {e}"}
    except _TraversalError as e:
        return {"_status": 403, "error": f"path traversal: {e}"}
    except _AdapterUnavailable as e:
        return {"_status": 501, "error": f"adapter not installed: {e}"}

    # Reads: (body_bytes, content_type, version_token).
    # Writes / listings / method-not-allowed-with-own-status: dict.
    if isinstance(result, tuple):
        body_bytes, ct, ver = result
        return {"_body": body_bytes, "_ct": ct,
                "_headers": [("X-Mount-Version", ver)]}
    return result


ROUTES = ["/mnt"]
