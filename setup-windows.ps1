<#
  Content Machine - one-time Windows setup. LAN ONLY. RUN AS ADMINISTRATOR.

    powershell -ExecutionPolicy Bypass -File D:\ContentMachine\setup-windows.ps1

  1. Firewall: allow inbound TCP 3000 from THIS SUBNET ONLY (auto-detected, e.g.
     192.168.7.0/24). Not open to the internet - your router already blocks
     inbound - and not open to any other network you join.
  2. Power: never sleep/hibernate on AC, don't spin down disks. A sleeping PC
     can't receive a video.
  3. Startup task: run.bat at logon, auto-restart on crash.
  4. Reports the LAN URL to bookmark + the DHCP reservation details you need.

  -Remove undoes the firewall rule and the task.
#>
param(
  [int]$Port = 3000,
  [switch]$SkipStartupTask,
  [switch]$Remove,
  # Content Machine does not use Tailscale at all. By default this stops and
  # disables the VPN service so it isn't running; the app stays installed so the
  # change is reversible (-KeepTailscale skips it entirely).
  [switch]$KeepTailscale,
  # Fully removes the Tailscale app as well. Opt-in - not reversible without
  # reinstalling.
  [switch]$UninstallTailscale
)

$ErrorActionPreference = 'Stop'
$RuleName = "Content Machine $Port (LAN only)"
$LegacyRuleName = "Content Machine $Port (Tailscale only)"
$TaskName = 'ContentMachine'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Assert-Admin {
  $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Needs Administrator. Right-click PowerShell -> Run as administrator." -ForegroundColor Red
    exit 1
  }
}
Assert-Admin

# One-time cleanup: an earlier design scoped this port to a VPN range. That rule
# must go, or it lingers in the firewall allowing a network you no longer use.
Get-NetFirewallRule -DisplayName $LegacyRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Removed firewall rules and startup task." -ForegroundColor Yellow
  exit 0
}

# ---- turn the VPN off (not used by this project) ---------------------------
if (-not $KeepTailscale) {
  $svc = Get-Service -Name 'Tailscale' -ErrorAction SilentlyContinue
  if ($svc) {
    if ($svc.Status -ne 'Stopped') { Stop-Service -Name 'Tailscale' -Force -ErrorAction SilentlyContinue }
    Set-Service -Name 'Tailscale' -StartupType Disabled -ErrorAction SilentlyContinue
    Write-Host "[ok] Tailscale service stopped and set to Disabled (app left installed)" -ForegroundColor Green
  } else {
    Write-Host "[--] No Tailscale service found - nothing to disable" -ForegroundColor DarkGray
  }
  # Kill the tray app too, or it will prompt to reconnect.
  Get-Process -Name 'tailscale-ipn' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

  if ($UninstallTailscale) {
    $wg = "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"
    if (Test-Path $wg) {
      & $wg uninstall --id Tailscale.Tailscale --silent --disable-interactivity 2>&1 | Out-Null
      Write-Host "[ok] Tailscale app uninstalled" -ForegroundColor Green
    } else {
      Write-Host "[!] winget not found; remove Tailscale via Settings > Apps" -ForegroundColor Yellow
    }
  }
}

# ---- detect the LAN interface (skip VPN/virtual adapters) ------------------
$cfg = Get-NetIPConfiguration |
  Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' -and $_.InterfaceAlias -notmatch 'Tailscale|Loopback' } |
  Select-Object -First 1
if (-not $cfg) { Write-Host "No active LAN adapter with a gateway. Connect to WiFi first." -ForegroundColor Red; exit 1 }

$ipObj  = $cfg.IPv4Address | Select-Object -First 1
$ip     = $ipObj.IPAddress
$prefix = $ipObj.PrefixLength
$gw     = $cfg.IPv4DefaultGateway.NextHop
$ad     = Get-NetAdapter -InterfaceIndex $cfg.InterfaceIndex
$subnet = (($ip -split '\.')[0..2] -join '.') + ".0/$prefix"

# ---- 1. firewall, this subnet only ----------------------------------------
New-NetFirewallRule -DisplayName $RuleName `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
  -RemoteAddress $subnet -Profile Any -Enabled True | Out-Null
Write-Host "[ok] Firewall: inbound TCP $Port allowed from $subnet only" -ForegroundColor Green

# ---- 2. never sleep on AC -------------------------------------------------
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 15   # screen may sleep; the machine stays up
$scheme = (powercfg /getactivescheme) -replace '.*GUID:\s*([0-9a-f-]+).*', '$1'
powercfg /setacvalueindex $scheme SUB_SLEEP STANDBYIDLE 0
powercfg /setactive $scheme
Write-Host "[ok] Power: no standby/hibernate/disk-sleep on AC" -ForegroundColor Green

# ---- 3. start at logon ---------------------------------------------------
if (-not $SkipStartupTask) {
  $bat = Join-Path $Root 'run.bat'
  if (Test-Path $bat) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    $action  = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
      -Settings $set -RunLevel Highest -Description 'Content Machine local server (LAN only)' | Out-Null
    Write-Host "[ok] Startup task '$TaskName' registered" -ForegroundColor Green
  } else {
    Write-Host "[!] run.bat not found; skipped startup task" -ForegroundColor Yellow
  }
}

# ---- 4. mDNS bonus attempt ----------------------------------------------
$bonjour = Get-Service -Name 'Bonjour Service' -ErrorAction SilentlyContinue
if ($bonjour) {
  Write-Host "[ok] Bonjour present ($($bonjour.Status)) - http://$($env:COMPUTERNAME.ToLower()).local:$Port may work" -ForegroundColor Green
} else {
  Write-Host "[--] No Bonjour service. <pcname>.local is unlikely to resolve from iPhone." -ForegroundColor DarkGray
  Write-Host "     Optional: winget install Apple.Bonjour   (bonus only, not required)" -ForegroundColor DarkGray
}

# ---- report -------------------------------------------------------------
Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " BOOKMARK THIS ON YOUR PHONE (same WiFi):" -ForegroundColor Cyan
Write-Host "     http://${ip}:$Port" -ForegroundColor White
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " To make that address permanent, reserve it at your router:" -ForegroundColor Yellow
Write-Host "     Router admin : http://$gw"
Write-Host "     PC name      : $env:COMPUTERNAME"
Write-Host "     MAC address  : $($ad.MacAddress)"
Write-Host "     Reserve IP   : $ip"
Write-Host "     Adapter      : $($cfg.InterfaceAlias)"
Write-Host ""
Write-Host " Reserving at the router is safer than a Windows static IP" -ForegroundColor DarkGray
Write-Host " (a static IP can collide with the router's DHCP pool)." -ForegroundColor DarkGray
