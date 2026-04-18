<#
Start OpenMemory using local Engram env files.

Usage:
  .\scripts\windows\start_openmemory.ps1
  .\scripts\windows\start_openmemory.ps1 -OpenMemoryDir "D:\openmemory\packages\openmemory-js"
  .\scripts\windows\start_openmemory.ps1 -FirstRun

Notes:
- Loads .env / .env.local / .env.ps1 automatically
- Defaults OpenMemory dir to %USERPROFILE%\openmemory\packages\openmemory-js
- Use -FirstRun when OpenMemory needs migrator credentials for initial schema creation
#>

[CmdletBinding()]
param(
  [string]$OpenMemoryDir = "",
  [switch]$FirstRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

. (Join-Path $PSScriptRoot "load_env_local.ps1")

if ([string]::IsNullOrWhiteSpace($OpenMemoryDir)) {
  if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DIR)) {
    $OpenMemoryDir = $env:OPENMEMORY_DIR
  } else {
    $candidates = @(
      (Join-Path $env:USERPROFILE "openmemory\packages\openmemory-js"),
      (Join-Path $env:USERPROFILE "Documents\openmemory\packages\openmemory-js"),
      (Join-Path $env:USERPROFILE "Documents\ai\openmemory\packages\openmemory-js")
    )
    $OpenMemoryDir = ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
  }
}

$opmCmd = Get-Command "opm" -ErrorAction SilentlyContinue
if (-not $opmCmd) {
  throw "Missing executable: opm. Install OpenMemory CLI first (npm link)."
}

if ($FirstRun) {
  if ([string]::IsNullOrWhiteSpace($env:OPENMEMORY_MIGRATOR_PASSWORD) -and [string]::IsNullOrWhiteSpace($env:OM_PG_PASSWORD)) {
    throw "需要 OPENMEMORY_MIGRATOR_PASSWORD 或 OM_PG_PASSWORD 才能首次启动 OpenMemory"
  }
  $env:OM_PG_USER = "openmemory_migrator_login"
  if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_MIGRATOR_PASSWORD)) {
    $env:OM_PG_PASSWORD = $env:OPENMEMORY_MIGRATOR_PASSWORD
  }
  $env:OM_PG_AUTO_DDL = "true"
}

Write-Host "[INFO] Starting OpenMemory..."
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and (Test-Path $OpenMemoryDir)) {
  Write-Host "       dir=$OpenMemoryDir"
} else {
  Write-Host "       dir=<auto>"
  Write-Host "       hint=local OpenMemory dir not found, using installed opm"
}
if ($FirstRun) {
  Write-Host "       mode=first-run"
}

if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and (Test-Path $OpenMemoryDir)) {
  Set-Location $OpenMemoryDir
}
& $opmCmd.Source serve
