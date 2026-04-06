# elastik Go Lite — zero-dependency bootstrap (Windows)
#
# Downloads the prebuilt elastik-go.exe for your architecture from the
# latest GitHub Release, verifies its SHA-256 checksum, and drops it
# in the current directory as .\elastik-go.exe.
#
# No Python. No Go toolchain. No admin rights.
#
# Usage (PowerShell):
#   iwr -useb https://raw.githubusercontent.com/rangersui/Elastik/master/get-elastik-go.ps1 | iex
#   # or with a specific version:
#   & ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/rangersui/Elastik/master/get-elastik-go.ps1))) v2.0.0
#
# This is the LITE path. For the full Python system (plugins, MCP,
# Claude Desktop integration), use .\install.cmd instead.

param(
    [string]$Version = "latest"
)

$ErrorActionPreference = "Stop"
$Repo = "rangersui/Elastik"

# Windows PowerShell 5.x defaults to TLS 1.0/1.1 which GitHub
# rejects. Force TLS 1.2 before any network call. PowerShell 7+ uses
# TLS 1.2+ by default so this is harmless there.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# ── detect arch ──────────────────────────────────────────────────────
switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { $goarch = "amd64" }
    "ARM64" { $goarch = "arm64" }
    default { throw "unsupported arch: $env:PROCESSOR_ARCHITECTURE" }
}

$asset = "elastik-go-windows-$goarch.exe"

# Only amd64 is currently built; fail fast with a clear message.
if ($goarch -ne "amd64") {
    throw "no prebuilt Windows binary for $goarch — build from source: cd go; .\build.bat"
}

# ── resolve tag ──────────────────────────────────────────────────────
#
# GitHub's /releases/latest skips prereleases AND does not filter by
# asset content, so asking it for "latest" returns whatever the most
# recent stable tag is — which may be a docs/refactor release with
# no Go binaries at all. Walk /releases and pick the first entry that
# actually has `elastik-go-*` assets attached. Works for both stable
# and prerelease Go Lite tags.
if ($Version -eq "latest") {
    try {
        $releases = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases?per_page=30"
    } catch {
        throw "could not reach GitHub releases API: $_"
    }
    $match = $releases | Where-Object {
        $_.assets | Where-Object { $_.name -like 'elastik-go-*' }
    } | Select-Object -First 1
    if (-not $match) {
        throw "no release with elastik-go-* assets found (has a Go Lite release been published?)"
    }
    $tag = $match.tag_name
} else {
    $tag = $Version
}

$base = "https://github.com/$Repo/releases/download/$tag"
Write-Host "==> elastik Go Lite $tag for windows/$goarch"

# ── download binary + checksums ──────────────────────────────────────
$tmp = New-Item -ItemType Directory -Path ([System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.IO.Path]::GetRandomFileName()))
try {
    $assetPath    = Join-Path $tmp.FullName $asset
    $checksumPath = Join-Path $tmp.FullName "SHA256SUMS.txt"

    Write-Host "==> downloading $asset"
    try {
        Invoke-WebRequest -UseBasicParsing "$base/$asset" -OutFile $assetPath
    } catch {
        throw "failed to download $asset from tag $tag (does this release have Go Lite binaries?)"
    }

    Write-Host "==> downloading SHA256SUMS.txt"
    try {
        Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS.txt" -OutFile $checksumPath
    } catch {
        throw "failed to download SHA256SUMS.txt from tag $tag"
    }

    # ── verify ───────────────────────────────────────────────────────
    Write-Host "==> verifying checksum"
    $expected = (Get-Content $checksumPath | Where-Object { $_ -match "\s$([regex]::Escape($asset))$" } | ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
    if (-not $expected) { throw "no checksum entry for $asset" }

    $actual = (Get-FileHash -Algorithm SHA256 $assetPath).Hash.ToLower()
    if ($actual -ne $expected.ToLower()) {
        throw "checksum mismatch`n  expected: $expected`n  actual:   $actual"
    }

    # ── install ──────────────────────────────────────────────────────
    Copy-Item -Force $assetPath ".\elastik-go.exe"

    Write-Host "==> installed .\elastik-go.exe ($tag)"

    # ── fetch static frontend (best-effort) ──────────────────────────
    #
    # The Go binary serves index.html / sw.js / openapi.json / manifest.json from the
    # current directory when they exist. Without them, GET / returns
    # a JSON stub and the browser UI does not render. Pull them from
    # the same tag so the versions stay in lockstep with the binary.
    #
    # Best-effort: a failure here warns but does not abort. The
    # binary is already installed and usable.
    $raw = "https://raw.githubusercontent.com/$Repo/$tag"
    Write-Host "==> fetching frontend assets"
    $fetched = 0
    foreach ($f in @("index.html", "sw.js", "openapi.json", "manifest.json")) {
        try {
            Invoke-WebRequest -UseBasicParsing "$raw/$f" -OutFile ".\$f"
            $fetched++
        } catch {
            Write-Host "    warn: could not fetch $f (skipping)"
        }
    }
    Write-Host "    $fetched/3 frontend files in place"

    Write-Host ""
    Write-Host "    next:  .\elastik-go.exe"
} finally {
    Remove-Item -Recurse -Force $tmp.FullName -ErrorAction SilentlyContinue
}
