# plugins/install.ps1 - install + activate one elastik plugin (Windows).
#
# Usage:
#   ./plugins/install.ps1 <plugin-name>
#   ./plugins/install.ps1 primitives
#   ./plugins/install.ps1 primitives -WithSemantic
#   $env:ELASTIK_APPROVE_TOKEN = "..."; ./plugins/install.ps1 semantic
#
# Params (all optional):
#   -Plugin        plugin name (matches plugins/<name>.py)
#   -ElastikHost   default http://localhost:3005 ($env:ELASTIK_HOST)
#   -Token         default $env:ELASTIK_APPROVE_TOKEN (falls back to $env:ELASTIK_TOKEN)
#
# What it does:
#   1. PUT plugins/<name>.py  ->  /lib/<name>          (upload source)
#   2. PUT "active"           ->  /lib/<name>/state    (activate)
#
# Idempotent: re-running overwrites the source + re-activates. Server
# hot-swaps the plugin; no restart needed.
#
# Not done here (config is separate from install):
#   - /etc/gpu.conf   (gpu plugin needs this; write it yourself)

[CmdletBinding()]
param(
    [Parameter(Position=0)]
    [string]$Plugin = "",
    [string]$ElastikHost = "",
    [string]$Token = "",
    [switch]$WithSemantic
)

$ErrorActionPreference = 'Stop'

if (-not $ElastikHost) {
    $ElastikHost = if ($env:ELASTIK_HOST) { $env:ELASTIK_HOST } else { 'http://localhost:3005' }
}
if (-not $Token) {
    if ($env:ELASTIK_APPROVE_TOKEN) { $Token = $env:ELASTIK_APPROVE_TOKEN }
    elseif ($env:ELASTIK_TOKEN) { $Token = $env:ELASTIK_TOKEN }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-AvailablePlugins {
    Get-ChildItem -Path $scriptDir -Filter *.py |
        Where-Object { $_.BaseName -ne '__init__' } |
        Select-Object -ExpandProperty BaseName
}

function Show-Usage {
    Write-Host "usage: install.ps1 <plugin-name>" -ForegroundColor Red
    Write-Host "       install.ps1 primitives [-WithSemantic]" -ForegroundColor Red
    Write-Host ""
    Write-Host "available plugins:"
    Get-AvailablePlugins | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "special targets:"
    Write-Host "  primitives        gpu + fstab + db + fanout"
    Write-Host "  -WithSemantic     add semantic after primitives"
}

if (-not $Plugin) {
    Show-Usage
    exit 2
}

$headers = @{}
if ($Token) { $headers['Authorization'] = "Bearer $Token" }

function Invoke-Step {
    param(
        [string]$Label,
        [string]$Uri,
        $Body,
        [string]$ContentType
    )
    Write-Host "  $Label" -NoNewline
    try {
        $h = $headers.Clone()
        if ($ContentType) { $h['Content-Type'] = $ContentType }
        $r = Invoke-WebRequest -Method Put -Uri $Uri -Headers $h -Body $Body `
                               -UseBasicParsing -ErrorAction Stop
        Write-Host " -> HTTP $($r.StatusCode)" -ForegroundColor Green
        if ($r.Content) { Write-Host $r.Content }
        return $true
    } catch {
        $status = ''
        if ($_.Exception.Response) {
            $status = $_.Exception.Response.StatusCode.value__
        }
        Write-Host " -> HTTP $status FAIL" -ForegroundColor Red
        Write-Host "       $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

function Show-PostInstallHint {
    param([string]$Name)
    switch ($Name) {
        'semantic' {
            Write-Host "semantic depends on /dev/gpu. If you haven't:" -ForegroundColor Cyan
            Write-Host "  ./plugins/install.ps1 gpu"
            Write-Host "  curl -X PUT $ElastikHost/etc/gpu.conf ``"
            Write-Host "       -H `"Authorization: Bearer `$env:ELASTIK_APPROVE_TOKEN`" ``"
            Write-Host "       -d 'ollama://127.0.0.1:11434'"
        }
        'gpu' {
            Write-Host "/dev/gpu needs a backend in /etc/gpu.conf. E.g.:" -ForegroundColor Cyan
            Write-Host "  curl -X PUT $ElastikHost/etc/gpu.conf ``"
            Write-Host "       -H `"Authorization: Bearer `$env:ELASTIK_APPROVE_TOKEN`" ``"
            Write-Host "       -d 'ollama://127.0.0.1:11434'"
        }
    }
}

function Install-One {
    param([string]$Name)

    $src = Join-Path $scriptDir "$Name.py"
    if (-not (Test-Path $src)) {
        Write-Host "error: $src not found" -ForegroundColor Red
        Write-Host "(plugin name must match a .py file in plugins/)" -ForegroundColor Red
        exit 2
    }

    Write-Host "Installing '$Name' -> $ElastikHost" -ForegroundColor Cyan
    Write-Host "  source: $src"
    if ($Token) {
        Write-Host "  auth:   Bearer (token set)"
    } else {
        Write-Host "  auth:   (no token -- works only on localhost with no server token)"
    }
    Write-Host ""

    $srcBytes = [System.IO.File]::ReadAllBytes($src)
    $ok1 = Invoke-Step -Label "1/2  PUT $src -> $ElastikHost/lib/$Name" `
                       -Uri "$ElastikHost/lib/$Name" `
                       -Body $srcBytes `
                       -ContentType 'text/x-python'
    if (-not $ok1) { exit 1 }

    Write-Host ""
    $ok2 = Invoke-Step -Label "2/2  PUT active -> $ElastikHost/lib/$Name/state" `
                       -Uri "$ElastikHost/lib/$Name/state" `
                       -Body 'active' `
                       -ContentType 'text/plain'
    if (-not $ok2) { exit 1 }

    Write-Host ""
    Write-Host "installed + activated: $Name" -ForegroundColor Green
    Write-Host ""
    Show-PostInstallHint -Name $Name
    if ($Name -in @('semantic', 'gpu')) {
        Write-Host ""
    }
}

if ($Plugin -ieq 'primitives') {
    $targets = @('gpu', 'fstab', 'db', 'fanout')
    if ($WithSemantic) { $targets += 'semantic' }

    Write-Host "Installing primitive set -> $ElastikHost" -ForegroundColor Cyan
    Write-Host "  plugins: $($targets -join ', ')"
    if ($Token) {
        Write-Host "  auth:    Bearer (token set)"
    } else {
        Write-Host "  auth:    (no token -- works only on localhost with no server token)"
    }
    Write-Host ""

    foreach ($name in $targets) {
        Install-One -Name $name
    }

    Write-Host "primitive set installed: $($targets -join ', ')" -ForegroundColor Green
    Write-Host ""
    Write-Host "verify routes registered:"
    Write-Host "  curl $ElastikHost/lib/"
    exit 0
}

if ($WithSemantic) {
    Write-Host "-WithSemantic is only valid with the 'primitives' target" -ForegroundColor Red
    exit 2
}

Install-One -Name $Plugin
Write-Host "verify routes registered:"
Write-Host "  curl $ElastikHost/lib/"
