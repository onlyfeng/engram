<#
Run make-ci equivalent checks on Windows without GNU make.

Equivalent entrypoint:
  python scripts/ops/ci_no_make.py
#>

[CmdletBinding()]
param(
  [switch]$DryRun,
  [string[]]$Only = @(),
  [switch]$Json,
  [string]$ProjectRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Python {
  $cmd = Get-Command "python" -ErrorAction SilentlyContinue
  if ($cmd) { return @("python", @()) }
  $cmd = Get-Command "py" -ErrorAction SilentlyContinue
  if ($cmd) { return @("py", @("-3")) }
  throw "Missing python. Ensure Python (or venv) is on PATH."
}

$repoRoot = if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  Resolve-Path (Join-Path $PSScriptRoot "..\..")
} else {
  Resolve-Path $ProjectRoot
}

$scriptPath = Join-Path $repoRoot "scripts\ops\ci_no_make.py"
if (-not (Test-Path $scriptPath)) {
  throw "Missing script: $scriptPath"
}

$pyInfo = Resolve-Python
$pyExe = $pyInfo[0]
$pyPrefix = $pyInfo[1]

$argsList = @($scriptPath)
if ($DryRun) { $argsList += "--dry-run" }
if ($Json) { $argsList += "--json" }
foreach ($target in $Only) {
  if (-not [string]::IsNullOrWhiteSpace($target)) {
    $argsList += @("--only", $target)
  }
}
if (-not [string]::IsNullOrWhiteSpace($ProjectRoot)) {
  $argsList += @("--project-root", $ProjectRoot)
}

& $pyExe @pyPrefix @argsList
exit $LASTEXITCODE
