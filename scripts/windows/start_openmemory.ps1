<#
Start OpenMemory using local Engram env files.

Usage:
  .\scripts\windows\start_openmemory.ps1
  .\scripts\windows\start_openmemory.ps1 -OpenMemoryDir "D:\openmemory\packages\openmemory-js"
  .\scripts\windows\start_openmemory.ps1 -FirstRun

Notes:
- Loads .env / .env.local / .env.ps1 automatically
- Resolution order: -OpenMemoryDir / OPENMEMORY_DIR, ..\openmemory, common local dirs, global opm
- Falls back to global opm when no runnable local checkout is found
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
    # Prefer sibling checkout, then try common local clones.
    $candidates = @(
      (Join-Path $RepoRoot "..\openmemory\packages\openmemory-js"),
      (Join-Path $env:USERPROFILE "openmemory\packages\openmemory-js"),
      (Join-Path $env:USERPROFILE "Documents\openmemory\packages\openmemory-js"),
      (Join-Path $env:USERPROFILE "Documents\ai\openmemory\packages\openmemory-js")
    )
    $OpenMemoryDir = ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
  }
}

$nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
$opmCmd = Get-Command "opm" -ErrorAction SilentlyContinue

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
  $opmScript = Join-Path $OpenMemoryDir "bin\opm.js"
  $distEntry = Join-Path $OpenMemoryDir "dist\server\index.js"
  if ((Test-Path $opmScript) -and (Test-Path $distEntry)) {
    if (-not $nodeCmd) {
      Write-Warning "本地 openmemory 可运行，但未找到 node；将尝试回退到全局 opm。"
    } else {
      if ($opmCmd) {
        Write-Warning "检测到全局 opm: $($opmCmd.Source)"
        Write-Host "       当前将优先直接运行本地 checkout，避免误用旧版本。"
      }
      Set-Location $OpenMemoryDir
      & $nodeCmd.Source "bin/opm.js" "serve"
      exit $LASTEXITCODE
    }
  }

  if (-not (Test-Path $opmScript)) {
    Write-Warning "本地 openmemory 缺少 $opmScript；将尝试回退到全局 opm。"
  } elseif (-not (Test-Path $distEntry)) {
    Write-Warning "本地 openmemory 缺少编译产物 $distEntry；将尝试回退到全局 opm。"
    Write-Host "       如需优先使用源码目录，请先在该目录执行 npm install; npm run build"
  }
}

if (-not $opmCmd) {
  throw "Missing executable: opm. Checked -OpenMemoryDir/OPENMEMORY_DIR, ..\\openmemory, common local dirs, and global opm."
}

if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and (Test-Path $OpenMemoryDir)) {
  Set-Location $OpenMemoryDir
}
& $opmCmd.Source serve
