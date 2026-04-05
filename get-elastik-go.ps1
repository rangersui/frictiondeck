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
if ($Version -eq "latest") {
    try {
        $release = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest"
        $tag = $release.tag_name
    } catch {
        throw "could not resolve latest tag (has a release been published?)"
    }
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
    Invoke-WebRequest -UseBasicParsing "$base/$asset" -OutFile $assetPath

    Write-Host "==> downloading SHA256SUMS.txt"
    Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS.txt" -OutFile $checksumPath

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
    Write-Host ""
    Write-Host "    next:  .\elastik-go.exe"
    Write-Host "    (drop index.html in the same directory for the browser UI)"
} finally {
    Remove-Item -Recurse -Force $tmp.FullName -ErrorAction SilentlyContinue
}
