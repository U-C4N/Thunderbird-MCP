<#
.SYNOPSIS
  Builds the capability-probe XPI and drops it into the Thunderbird profile.

.DESCRIPTION
  Thunderbird 153 release builds ship MOZ_REQUIRE_SIGNING=false and default
  extensions.experiments.enabled=true, so an unsigned add-on with an
  experiment_apis section loads with full XPCOM privileges. Add-ons live in
  <profile>\extensions\<id>.xpi; dropping the file there while Thunderbird is
  closed makes it register on the next start.

.PARAMETER Remove
  Delete the probe instead of installing it.
#>
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$AddonId = 'probe@thunderbird-mcp.local'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AddonDir = Join-Path $Here 'addon'
$Xpi = Join-Path $Here 'tbmcp-probe.xpi'

# --- locate the default profile from profiles.ini -----------------------------
$IniPath = Join-Path $env:APPDATA 'Thunderbird\profiles.ini'
if (-not (Test-Path $IniPath)) { throw "profiles.ini not found at $IniPath" }

$profileRel = $null
$current = $null
$sections = @{}
foreach ($line in Get-Content $IniPath) {
    if ($line -match '^\[(.+)\]$') { $current = $Matches[1]; $sections[$current] = @{}; continue }
    if ($current -and $line -match '^([^=]+)=(.*)$') { $sections[$current][$Matches[1]] = $Matches[2] }
}
# An [InstallXXXX] section's Default= wins; it is the profile the installed
# Thunderbird actually uses.
foreach ($name in $sections.Keys) {
    if ($name -like 'Install*' -and $sections[$name]['Default']) {
        $profileRel = $sections[$name]['Default']
        break
    }
}
if (-not $profileRel) {
    foreach ($name in $sections.Keys) {
        if ($name -like 'Profile*' -and $sections[$name]['Default'] -eq '1') {
            $profileRel = $sections[$name]['Path']
            break
        }
    }
}
if (-not $profileRel) { throw 'Could not determine the default profile from profiles.ini' }

$ProfileDir = Join-Path $env:APPDATA ("Thunderbird\" + ($profileRel -replace '/', '\'))
if (-not (Test-Path $ProfileDir)) { throw "Profile directory not found: $ProfileDir" }
Write-Host "Profile: $ProfileDir"

$ExtDir = Join-Path $ProfileDir 'extensions'
$Target = Join-Path $ExtDir "$AddonId.xpi"

if (Get-Process thunderbird -ErrorAction SilentlyContinue) {
    Write-Warning 'Thunderbird is running. Close it before installing, then start it again.'
}

if ($Remove) {
    if (Test-Path $Target) { Remove-Item $Target -Force; Write-Host "Removed $Target" }
    else { Write-Host 'Probe was not installed.' }
    $report = Join-Path $ProfileDir 'tbmcp-probe-report.json'
    if (Test-Path $report) { Remove-Item $report -Force; Write-Host 'Removed probe report.' }
    return
}

# --- build the XPI (a plain zip with the manifest at the root) ----------------
if (Test-Path $Xpi) { Remove-Item $Xpi -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $AddonDir, $Xpi, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Write-Host ("Built {0} ({1} bytes)" -f $Xpi, (Get-Item $Xpi).Length)

New-Item -ItemType Directory -Force $ExtDir | Out-Null
Copy-Item $Xpi $Target -Force
Write-Host "Installed to $Target"
Write-Host ''
Write-Host 'Next:  1) python probe/probe_server.py      (leave it running)'
Write-Host '       2) start Thunderbird'
Write-Host "       3) read $ProfileDir\tbmcp-probe-report.json"
