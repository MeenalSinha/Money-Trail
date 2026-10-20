#Requires -Version 5.1
<#
.SYNOPSIS
    Money Trail - Reproducible one-shot setup script (Windows PowerShell).

.DESCRIPTION
    Performs the full setup sequence for Money Trail on Windows:
      1. Creates a Python virtual environment (.venv)
      2. Installs all required packages (including tzdata for Windows timezone support)
      3. Copies .env.example -> .env (if no .env exists)
      4. Starts the FalkorDB Docker container via compose
      5. Waits for FalkorDB to be ready (up to 60s)
      6. Initializes the graph schema (idempotent)
      7. Runs the health check
      8. Prints the next-steps summary

    Prerequisites:
      - Python 3.10-3.13 on PATH (python or python3)
      - Docker Desktop running on Windows
      - A .env file (or .env.example configured with real credentials)

.EXAMPLE
    .\scripts\setup.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot

function Write-Step($n, $total, $msg) {
    Write-Host ""
    Write-Host "[$n/$total] $msg" -ForegroundColor Cyan
}

function Abort($msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

$TotalSteps = 7

# 1. Python
Write-Step 1 $TotalSteps "Checking Python"
$PythonExe = $null
foreach ($candidate in @("python", "python3")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(1[0-3]|[0-9]{2})") {
            $PythonExe = $candidate
            Write-Host "  Found: $ver"
            break
        }
    } catch {}
}
if (-not $PythonExe) {
    Abort "Python 3.10-3.13 not found on PATH. Install from https://www.python.org/downloads/"
}

# 2. Virtual environment
Write-Step 2 $TotalSteps "Creating virtual environment (.venv)"
$VenvDir = Join-Path $Root ".venv"
if (-not (Test-Path $VenvDir)) {
    & $PythonExe -m venv $VenvDir
    Write-Host "  Created .venv"
} else {
    Write-Host "  .venv already exists; skipping creation"
}

$PipExe   = Join-Path $VenvDir "Scripts\pip.exe"
$PythonVE = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "  Installing money-trail[dev] ..."
& $PipExe install --quiet -e (Join-Path $Root ".[dev]")
Write-Host "  Installing tzdata (Windows timezone support) ..."
& $PipExe install --quiet tzdata
Write-Host "  Packages installed."

# 3. Environment file
Write-Step 3 $TotalSteps "Checking environment file"
$EnvFile = Join-Path $Root ".env"
$EnvExample = Join-Path $Root ".env.example"
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Copy-Item $EnvExample $EnvFile
        Write-Host "  Copied .env.example -> .env"
        Write-Host "  !! Edit .env and set FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD," -ForegroundColor Yellow
        Write-Host "     FALKORDB_GRAPH_NAME, and MONEY_TRAIL_TEST_GRAPH_PREFIX before continuing." -ForegroundColor Yellow
    } else {
        Write-Host "  WARNING: no .env or .env.example found. Create .env manually." -ForegroundColor Yellow
    }
} else {
    Write-Host "  .env already exists; skipping copy"
}

# 4. Start FalkorDB
Write-Step 4 $TotalSteps "Starting FalkorDB (docker compose)"
Push-Location $Root
try {
    & docker compose up -d 2>&1 | ForEach-Object { Write-Host "  $_" }
} catch {
    Abort "docker compose up failed. Is Docker Desktop running?"
} finally {
    Pop-Location
}

# 5. Wait for readiness
Write-Step 5 $TotalSteps "Waiting for FalkorDB to be ready (up to 60s)"
$MoneyTrailCheck = Join-Path $VenvDir "Scripts\money-trail-check.exe"
$ready = $false
for ($i = 1; $i -le 12; $i++) {
    Start-Sleep -Seconds 5
    $result = & $MoneyTrailCheck 2>&1
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        Write-Host "  FalkorDB ready after ~$($i*5)s"
        break
    }
    Write-Host "  Attempt $i/12 - not ready yet ..."
}
if (-not $ready) {
    Abort "FalkorDB did not become ready within 60s. Check docker compose logs."
}

# 6. Initialize schema
Write-Step 6 $TotalSteps "Initializing / migrating graph schema"
$MoneyTrailInit = Join-Path $VenvDir "Scripts\money-trail-init.exe"
& $MoneyTrailInit | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) {
    Abort "money-trail-init failed."
}

# 7. Health check
Write-Step 7 $TotalSteps "Final health check"
& $MoneyTrailCheck | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) {
    Abort "Health check failed after init."
}

Write-Host ""
Write-Host ("=" * 60) -ForegroundColor Green
Write-Host "  Money Trail setup complete!" -ForegroundColor Green
Write-Host ("=" * 60) -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    Activate env : .venv\Scripts\Activate.ps1"
Write-Host "    Run demo     : money-trail demo"
Write-Host "    Run QA suite : money-trail qa --output-dir evaluation_reports"
Write-Host "    Launch UI    : money-trail dashboard"
Write-Host "    Run tests    : pytest"
Write-Host ""
