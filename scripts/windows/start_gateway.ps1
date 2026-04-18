<#
Start Engram Gateway using local env files.

Usage:
  .\scripts\windows\start_gateway.ps1
  .\scripts\windows\start_gateway.ps1 -Port 8788

Notes:
- Loads .env / .env.local / .env.ps1 automatically
- Uses .venv\Scripts\python.exe when available
#>

[CmdletBinding()]
param(
  [int]$Port = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

. (Join-Path $PSScriptRoot "load_env_local.ps1")

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$pythonCmd = if (Test-Path $venvPython) { $venvPython } else { "python" }

if ($Port -le 0) {
  if (-not [string]::IsNullOrWhiteSpace($env:GATEWAY_PORT)) {
    $Port = [int]$env:GATEWAY_PORT
  } else {
    $Port = 8787
  }
}

Write-Host "[INFO] Starting Gateway..."
Write-Host "       port=$Port"
Write-Host "       project=$env:PROJECT_KEY"
Write-Host "       openmemory=$env:OPENMEMORY_BASE_URL"

& $pythonCmd -m uvicorn engram.gateway.main:app --host 0.0.0.0 --port $Port --reload
