<#
Install Windows services via NSSM:
- Engram Gateway / Outbox (as configured)
- OpenMemory (as configured)

Notes:
- Run as Administrator
- Place nssm.exe at: scripts/windows/tools/nssm/nssm.exe
#>

param(
  [string]$ConfigPath = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\config.ps1"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ConfigPath)) {
  $example = Join-Path (Split-Path -Parent $ConfigPath) "config.ps1.example"
  throw "Missing config: $ConfigPath. Copy $example to $ConfigPath and edit paths/ports as needed. (config.ps1 should stay local and is gitignored.)"
}
. $ConfigPath

$nssm = Join-Path $PSScriptRoot "tools\nssm\nssm.exe"
if (-not (Test-Path $nssm)) { throw "Missing NSSM: $nssm (place nssm.exe there)" }

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Resolve-WorkDirFromArgs([string]$cmd, [string]$args) {
  # Default to executable's directory
  $workdir = Split-Path -Parent $cmd
  if ([string]::IsNullOrWhiteSpace($args)) { return $workdir }

  $first = $null
  if ($args -match '^\s*"(.*?)"') {
    $first = $matches[1]
  } elseif ($args -match '^\s*([^\s]+)') {
    $first = $matches[1]
  }

  if ($first -and (Test-Path -LiteralPath $first)) {
    $item = Get-Item -LiteralPath $first -ErrorAction SilentlyContinue
    if ($item) {
      if ($item.PSIsContainer) { return $item.FullName }
      return Split-Path -Parent $item.FullName
    }
  }

  return $workdir
}

function Install-Service($name, $cmd, $args, $workdir, $envMap) {
  Write-Host "Installing service: $name"
  & $nssm stop $name | Out-Null 2>$null
  & $nssm remove $name confirm | Out-Null 2>$null

  & $nssm install $name $cmd $args
  & $nssm set $name AppDirectory $workdir
  & $nssm set $name AppStdout (Join-Path $LogsDir "$name.out.log")
  & $nssm set $name AppStderr (Join-Path $LogsDir "$name.err.log")
  & $nssm set $name Start SERVICE_AUTO_START

  if ($envMap) {
    $pairs = @()
    foreach ($kv in $envMap.GetEnumerator()) {
      $pairs += "$($kv.Key)=$($kv.Value)"
    }
    $envString = [string]::Join("`n", $pairs)
    & $nssm set $name AppEnvironmentExtra $envString
  }

  & $nssm start $name
}

Install-Service -name "engram" -cmd $EngramCmd -args $EngramArgs -workdir $EngramHome -envMap $EngramEnv
$openMemoryWorkDir = Resolve-WorkDirFromArgs $OpenMemoryCmd $OpenMemoryArgs
Install-Service -name "openmemory" -cmd $OpenMemoryCmd -args $OpenMemoryArgs -workdir $openMemoryWorkDir -envMap $OpenMemoryEnv

Write-Host "OK: services installed and started. Logs: $LogsDir"
