#!/usr/bin/env sh
# elastik Go Lite — zero-dependency bootstrap (linux / macos)
#
# Downloads the prebuilt elastik-go binary for your OS/arch from the
# latest GitHub Release, verifies its SHA-256 checksum, and drops it
# in the current directory as ./elastik-go.
#
# No Python. No Go toolchain. No sudo. No global install.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/rangersui/Elastik/master/get-elastik-go.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/rangersui/Elastik/master/get-elastik-go.sh | sh -s v2.0.0
#
# This is the LITE path. For the full Python system (plugins, MCP,
# Claude Desktop integration), use ./install.sh instead.

set -eu

REPO="rangersui/Elastik"
VERSION="${1:-latest}"

# ── detect OS/arch ──────────────────────────────────────────────────
uname_s=$(uname -s 2>/dev/null || echo unknown)
uname_m=$(uname -m 2>/dev/null || echo unknown)
case "$uname_s" in
    Linux)  goos=linux ;;
    Darwin) goos=darwin ;;
    *) echo "error: unsupported OS: $uname_s (try building from source: cd go && ./build.sh)" >&2; exit 1 ;;
esac
case "$uname_m" in
    x86_64|amd64)  goarch=amd64 ;;
    arm64|aarch64) goarch=arm64 ;;
    *) echo "error: unsupported arch: $uname_m" >&2; exit 1 ;;
esac

asset="elastik-go-${goos}-${goarch}"

# ── resolve tag ─────────────────────────────────────────────────────
if [ "$VERSION" = "latest" ]; then
    tag=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
    [ -n "$tag" ] || { echo "error: could not resolve latest tag (has a release been published?)" >&2; exit 1; }
else
    tag="$VERSION"
fi

base="https://github.com/${REPO}/releases/download/${tag}"
echo "==> elastik Go Lite ${tag} for ${goos}/${goarch}"

# ── download binary + checksums ─────────────────────────────────────
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "==> downloading ${asset}"
curl -fsSL "${base}/${asset}" -o "${tmp}/${asset}"

echo "==> downloading SHA256SUMS.txt"
curl -fsSL "${base}/SHA256SUMS.txt" -o "${tmp}/SHA256SUMS.txt"

# ── verify ──────────────────────────────────────────────────────────
echo "==> verifying checksum"
expected=$(awk -v f="${asset}" '$2==f {print $1}' "${tmp}/SHA256SUMS.txt")
[ -n "$expected" ] || { echo "error: no checksum entry for ${asset}" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "${tmp}/${asset}" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "${tmp}/${asset}" | awk '{print $1}')
else
    echo "error: need sha256sum or shasum on PATH" >&2; exit 1
fi

if [ "$actual" != "$expected" ]; then
    echo "error: checksum mismatch" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual"   >&2
    exit 1
fi

# ── install ─────────────────────────────────────────────────────────
cp "${tmp}/${asset}" ./elastik-go
chmod +x ./elastik-go

echo "==> installed ./elastik-go (${tag})"
echo ""
echo "    next:  ./elastik-go"
echo "    (drop index.html in the same directory for the browser UI)"
