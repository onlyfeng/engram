<#
Stack doctor wrapper for Windows PowerShell.
Equivalent to `make stack-doctor`:
  1) MCP doctor (Gateway MCP checks, no OpenMemory dependency)
  2) Stack doctor (OpenMemory /health + memory_store write)
#>

[CmdletBinding()]
param(
  [string]$GatewayUrl = "",
  [string]$OpenMemoryUrl = "",
  [string]$Timeout = "",
  [string]$Authorization = "",
  [switch]$Json,
  [switch]$Pretty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Python() {
  $cmd = Get-Command "python" -ErrorAction SilentlyContinue
  if ($cmd) { return @("python", @()) }
  $cmd = Get-Command "py" -ErrorAction SilentlyContinue
  if ($cmd) { return @("py", @("-3")) }
  throw "Missing python. Ensure Python (or venv) is on PATH."
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$mcpDoctor = Join-Path $repoRoot "scripts\ops\mcp_doctor.py"
$stackDoctor = Join-Path $repoRoot "scripts\ops\stack_doctor.py"
if (-not (Test-Path $mcpDoctor)) { throw "Missing script: $mcpDoctor" }
if (-not (Test-Path $stackDoctor)) { throw "Missing script: $stackDoctor" }

$pyInfo = Resolve-Python
$pyExe = $pyInfo[0]
$pyPrefix = $pyInfo[1]

# Save env (best-effort restore)
$oldGatewayUrl = $env:GATEWAY_URL
$oldOpenMemoryUrl = $env:OPENMEMORY_URL
$oldOpenMemoryBaseUrl = $env:OPENMEMORY_BASE_URL
$oldMcpTimeout = $env:MCP_DOCTOR_TIMEOUT
$oldStackTimeout = $env:STACK_DOCTOR_TIMEOUT
$oldAuth = $env:MCP_DOCTOR_AUTHORIZATION

$exitCode = 0

try {
  if (-not [string]::IsNullOrWhiteSpace($GatewayUrl)) { $env:GATEWAY_URL = $GatewayUrl }
  if (-not [string]::IsNullOrWhiteSpace($OpenMemoryUrl)) { $env:OPENMEMORY_URL = $OpenMemoryUrl }
  if (-not [string]::IsNullOrWhiteSpace($Timeout)) {
    $env:MCP_DOCTOR_TIMEOUT = $Timeout
    $env:STACK_DOCTOR_TIMEOUT = $Timeout
  }
  if (-not [string]::IsNullOrWhiteSpace($Authorization)) { $env:MCP_DOCTOR_AUTHORIZATION = $Authorization }

  Write-Host "========== Stack doctor (PowerShell) =========="
  Write-Host ""
  Write-Host "[1/2] Gateway MCP endpoint checks (no OpenMemory dependency)..."

  $mcpArgs = @($mcpDoctor)
  if (-not [string]::IsNullOrWhiteSpace($GatewayUrl)) { $mcpArgs += @("--gateway-url", $GatewayUrl) }
  if ($Json) { $mcpArgs += "--json" }
  if ($Pretty) { $mcpArgs += "--pretty" }
  if (-not [string]::IsNullOrWhiteSpace($Authorization)) {
    $mcpArgs += @("--header", ("Authorization: " + $Authorization))
  }

  & $pyExe @pyPrefix @mcpArgs
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) { return }

  Write-Host ""
  Write-Host "[2/2] OpenMemory + memory_store write check..."

  $stackArgs = @($stackDoctor)
  if (-not [string]::IsNullOrWhiteSpace($GatewayUrl)) { $stackArgs += @("--gateway-url", $GatewayUrl) }
  if ($Json) { $stackArgs += "--json" }
  if ($Pretty) { $stackArgs += "--pretty" }

  & $pyExe @pyPrefix @stackArgs
  $exitCode = $LASTEXITCODE
}
finally {
  $env:GATEWAY_URL = $oldGatewayUrl
  $env:OPENMEMORY_URL = $oldOpenMemoryUrl
  $env:OPENMEMORY_BASE_URL = $oldOpenMemoryBaseUrl
  $env:MCP_DOCTOR_TIMEOUT = $oldMcpTimeout
  $env:STACK_DOCTOR_TIMEOUT = $oldStackTimeout
  $env:MCP_DOCTOR_AUTHORIZATION = $oldAuth
}

exit $exitCode
