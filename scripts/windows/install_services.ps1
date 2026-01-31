<#
使用 NSSM 安装服务：
- Engram
- OpenMemory
说明：
- 需要管理员权限运行
- 需要把 nssm.exe 放在 scripts/windows/tools/nssm/nssm.exe
#>

param(
  [string]$ConfigPath = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\config.ps1"
)

. $ConfigPath
$ErrorActionPreference = "Stop"

$nssm = Join-Path $PSScriptRoot "tools\nssm\nssm.exe"
if (-not (Test-Path $nssm)) { throw "找不到 NSSM：$nssm（请放置 nssm.exe）" }

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

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
Install-Service -name "openmemory" -cmd $OpenMemoryCmd -args $OpenMemoryArgs -workdir (Split-Path -Parent $OpenMemoryCmd) -envMap $OpenMemoryEnv

Write-Host "✅ 服务已安装并启动（请检查日志目录）: $LogsDir"
