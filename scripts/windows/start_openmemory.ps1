<#
Start OpenMemory using local Engram env files.

Usage:
  .\scripts\windows\start_openmemory.ps1
  .\scripts\windows\start_openmemory.ps1 -OpenMemoryDir "D:\openmemory\packages\openmemory-js"
  .\scripts\windows\start_openmemory.ps1 -FirstRun
  .\scripts\windows\start_openmemory.ps1 -Help

Notes:
- Loads .env / .env.local / .env.ps1 automatically
- Resolution order: -OpenMemoryDir / OPENMEMORY_DIR, ..\openmemory, common local dirs, global opm
- Falls back to global opm when no runnable local checkout is found
- Use -FirstRun when OpenMemory needs migrator credentials for initial schema creation
- OPENMEMORY_FIRST_RUN=1 in env files has the same effect as -FirstRun
#>

[CmdletBinding()]
param(
  [string]$OpenMemoryDir = "",
  [switch]$FirstRun,
  [switch]$Help
)

if ($Help) {
  Get-Help $PSCommandPath
  exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Save caller location before changing directory so relative -OpenMemoryDir
# paths are resolved against the caller's working directory, not the repo root.
$CallerDir = (Get-Location).Path

# Save caller-set env vars before env-file loading can overwrite them.
$CallerOMFirstRun = $env:OPENMEMORY_FIRST_RUN
# Normalize relative OPENMEMORY_DIR against caller CWD before Set-Location.
$CallerOMDir = ""
if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DIR)) {
  if ([System.IO.Path]::IsPathRooted($env:OPENMEMORY_DIR)) {
    $CallerOMDir = $env:OPENMEMORY_DIR
  } else {
    $CallerOMDir = Join-Path $CallerDir $env:OPENMEMORY_DIR
  }
}
$CallerOMMigratorPw = $env:OPENMEMORY_MIGRATOR_PASSWORD
$CallerOMPGPw = $env:OM_PG_PASSWORD

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

. (Join-Path $PSScriptRoot "load_env_local.ps1")

# Restore caller's explicit env vars so they take precedence over env files.
if (-not [string]::IsNullOrWhiteSpace($CallerOMFirstRun)) {
  $env:OPENMEMORY_FIRST_RUN = $CallerOMFirstRun
}
if (-not [string]::IsNullOrWhiteSpace($CallerOMDir)) {
  $env:OPENMEMORY_DIR = $CallerOMDir
}
if (-not [string]::IsNullOrWhiteSpace($CallerOMMigratorPw)) {
  $env:OPENMEMORY_MIGRATOR_PASSWORD = $CallerOMMigratorPw
}
if (-not [string]::IsNullOrWhiteSpace($CallerOMPGPw)) {
  $env:OM_PG_PASSWORD = $CallerOMPGPw
}

# Resolve a relative -OpenMemoryDir against the caller's directory.
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and
    -not [System.IO.Path]::IsPathRooted($OpenMemoryDir)) {
  $OpenMemoryDir = Join-Path $CallerDir $OpenMemoryDir
}

if ([string]::IsNullOrWhiteSpace($OpenMemoryDir)) {
  if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DIR)) {
    $OpenMemoryDir = $env:OPENMEMORY_DIR
  } else {
    # Prefer sibling checkout, then try common local clones.
    # Skip candidates that exist but lack build artefacts so a runnable checkout
    # later in the list is not silently bypassed.
    $candidates = @(
      (Join-Path $RepoRoot "..\openmemory\packages\openmemory-js")
    )
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
      $candidates += @(
        (Join-Path $env:USERPROFILE "openmemory\packages\openmemory-js"),
        (Join-Path $env:USERPROFILE "Documents\openmemory\packages\openmemory-js"),
        (Join-Path $env:USERPROFILE "Documents\ai\openmemory\packages\openmemory-js")
      )
    }
    foreach ($candidate in $candidates) {
      if (-not (Test-Path -PathType Container $candidate)) { continue }
      $opmScript = Join-Path $candidate "bin\opm.js"
      $distEntry  = Join-Path $candidate "dist\server\index.js"
      if ((Test-Path -PathType Leaf $opmScript) -and (Test-Path -PathType Leaf $distEntry)) {
        $OpenMemoryDir = $candidate
        break
      }
      Write-Warning "发现 $candidate 但缺少编译产物，跳过继续搜索。"
      Write-Host "       如需使用该目录，请先在该目录执行: npm install; npm run build"
    }
  }
}

# Fail fast when an explicit directory was given but does not exist or is not a directory.
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and
    -not (Test-Path -PathType Container $OpenMemoryDir)) {
  throw "指定的 OpenMemory 目录不存在: $OpenMemoryDir"
}

# Also honour OPENMEMORY_FIRST_RUN=1 from env files (loaded above).
if ($env:OPENMEMORY_FIRST_RUN -eq "1") {
  $FirstRun = $true
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
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and (Test-Path -PathType Container $OpenMemoryDir)) {
  Write-Host "       dir=$OpenMemoryDir"
} else {
  Write-Host "       dir=<auto>"
}
if ($FirstRun) {
  Write-Host "       mode=first-run (migrator + auto-ddl)"
}

if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and (Test-Path -PathType Container $OpenMemoryDir)) {
  $opmScript = Join-Path $OpenMemoryDir "bin\opm.js"
  $distEntry = Join-Path $OpenMemoryDir "dist\server\index.js"
  if ((Test-Path -PathType Leaf $opmScript) -and (Test-Path -PathType Leaf $distEntry)) {
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

  if (-not (Test-Path -PathType Leaf $opmScript)) {
    Write-Warning "本地 openmemory 缺少 $opmScript；将尝试回退到全局 opm。"
  } elseif (-not (Test-Path -PathType Leaf $distEntry)) {
    Write-Warning "本地 openmemory 缺少编译产物 $distEntry；将尝试回退到全局 opm。"
    Write-Host "       如需优先使用源码目录，请先在该目录执行 npm install; npm run build"
  }
}

if (-not $opmCmd) {
  throw "Missing executable: opm. Checked -OpenMemoryDir/OPENMEMORY_DIR, ..\\openmemory, common local dirs, and global opm."
}

Write-Host "       runtime=global-opm"
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and (Test-Path -PathType Container $OpenMemoryDir)) {
  Set-Location $OpenMemoryDir
}
& $opmCmd.Source serve
exit $LASTEXITCODE
