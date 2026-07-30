<#
  Bring Postiz up. Run this AFTER Docker Desktop is installed and running.
  No admin needed once Docker exists.

      powershell -ExecutionPolicy Bypass -File D:\ContentMachine\postiz\start-postiz.ps1

  -Down stops it. -Logs tails it. -Pull updates the image.
#>
param([switch]$Down, [switch]$Logs, [switch]$Pull)

$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker not found. Install Docker Desktop first (see POSTIZ-SETUP.md)." -ForegroundColor Red
  exit 1
}
try { docker info *> $null } catch {
  Write-Host "Docker is installed but not running. Start Docker Desktop and wait for the whale to go steady." -ForegroundColor Yellow
  exit 1
}

if ($Down)  { docker compose down; exit 0 }
if ($Logs)  { docker compose logs -f --tail 80; exit 0 }
if ($Pull)  { docker compose pull }

Write-Host "Starting Postiz (first run pulls ~1-2 GB and takes a few minutes)..." -ForegroundColor Cyan
docker compose up -d

Write-Host ""
Write-Host "Waiting for Postiz to answer on http://localhost:5000 ..." -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 5
  try {
    $r = Invoke-WebRequest 'http://localhost:5000' -UseBasicParsing -TimeoutSec 6
    if ([int]$r.StatusCode -lt 500) { $ok = $true; break }
  } catch { }
  Write-Host "  ...still booting ($([int](($i+1)*5))s)" -ForegroundColor DarkGray
}

Write-Host ""
if ($ok) {
  Write-Host "Postiz is up: http://localhost:5000" -ForegroundColor Green
  Write-Host "First visit: create your local account (it is your own machine)." -ForegroundColor Green
} else {
  Write-Host "Did not answer in 5 minutes. Check logs:" -ForegroundColor Yellow
  Write-Host "  powershell -File `"$dir\start-postiz.ps1`" -Logs" -ForegroundColor Yellow
}
docker compose ps
