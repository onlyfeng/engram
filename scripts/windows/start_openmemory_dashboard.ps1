<#
Start the OpenMemory Dashboard (Next.js dev server) using local Engram env files.

Usage:
  .\scripts\windows\start_openmemory_dashboard.ps1
  .\scripts\windows\start_openmemory_dashboard.ps1 -OpenMemoryDir "D:\openmemory"
  .\scripts\windows\start_openmemory_dashboard.ps1 -Port 3001
  .\scripts\windows\start_openmemory_dashboard.ps1 -Help

Notes:
- Loads .env / .env.local / .env.ps1 automatically
- Resolution order: -OpenMemoryDir / OPENMEMORY_DASHBOARD_DIR, OPENMEMORY_DIR-derived dashboard, ..\openmemory\dashboard, common local dirs
- -OpenMemoryDir accepts the OpenMemory repo root, packages\openmemory-js, or the dashboard directory directly
- NEXT_PUBLIC_API_URL defaults to OPENMEMORY_BASE_URL, then http://localhost:<OM_PORT|8080>
- OPENMEMORY_DASHBOARD_PORT env has the same effect as -Port
#>

[CmdletBinding()]
param(
  [string]$OpenMemoryDir = "",
  [string]$Port = "",
  [switch]$Help
)

if ($Help) {
  Get-Help $PSCommandPath
  exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$CallerDir = (Get-Location).Path

# Save caller-set env vars before env-file loading can overwrite them.
$CallerDashboardDir = ""
if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DASHBOARD_DIR)) {
  if ([System.IO.Path]::IsPathRooted($env:OPENMEMORY_DASHBOARD_DIR)) {
    $CallerDashboardDir = $env:OPENMEMORY_DASHBOARD_DIR
  } else {
    $CallerDashboardDir = Join-Path $CallerDir $env:OPENMEMORY_DASHBOARD_DIR
  }
}
$CallerDashboardPort = $env:OPENMEMORY_DASHBOARD_PORT
# Normalize a caller-set OPENMEMORY_DIR before we change directory.
$CallerOMDir = ""
if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DIR)) {
  if ([System.IO.Path]::IsPathRooted($env:OPENMEMORY_DIR)) {
    $CallerOMDir = $env:OPENMEMORY_DIR
  } else {
    $CallerOMDir = Join-Path $CallerDir $env:OPENMEMORY_DIR
  }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

. (Join-Path $PSScriptRoot "load_env_local.ps1")

# Restore caller overrides so they win over env files.
if (-not [string]::IsNullOrWhiteSpace($CallerDashboardDir)) {
  $env:OPENMEMORY_DASHBOARD_DIR = $CallerDashboardDir
}
if (-not [string]::IsNullOrWhiteSpace($CallerDashboardPort)) {
  $env:OPENMEMORY_DASHBOARD_PORT = $CallerDashboardPort
}
if (-not [string]::IsNullOrWhiteSpace($CallerOMDir)) {
  $env:OPENMEMORY_DIR = $CallerOMDir
}

# Resolve -OpenMemoryDir: relative paths against caller CWD.
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and
    -not [System.IO.Path]::IsPathRooted($OpenMemoryDir)) {
  $OpenMemoryDir = Join-Path $CallerDir $OpenMemoryDir
}

function Resolve-DashboardFromOpenMemoryDir {
  param([string]$PathValue)

  if ([string]::IsNullOrWhiteSpace($PathValue)) { return "" }

  $dashSub = Join-Path $PathValue "dashboard"
  if (Test-Path -LiteralPath $dashSub -PathType Container) {
    return (Resolve-Path -LiteralPath $dashSub).Path
  }

  # OPENMEMORY_DIR normally points at packages\openmemory-js for the backend launcher.
  # Only probe the sibling dashboard when the parent directory is named "packages".
  $parentDirName = Split-Path -Leaf (Split-Path -Parent $PathValue)
  $packageSiblingDash = Join-Path (Join-Path $PathValue "..\..") "dashboard"
  if ($parentDirName -eq "packages" -and (Test-Path -LiteralPath $packageSiblingDash -PathType Container)) {
    return (Resolve-Path -LiteralPath $packageSiblingDash).Path
  }

  return ""
}

# Determine dashboard directory.
$DashboardDir = ""
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir)) {
  # Accept repo root, packages\openmemory-js, or a direct dashboard path.
  $resolvedDashboard = Resolve-DashboardFromOpenMemoryDir $OpenMemoryDir
  if (-not [string]::IsNullOrWhiteSpace($resolvedDashboard)) {
    $DashboardDir = $resolvedDashboard
  } else {
    $DashboardDir = $OpenMemoryDir
  }
} elseif (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DASHBOARD_DIR)) {
  $DashboardDir = $env:OPENMEMORY_DASHBOARD_DIR
} elseif (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DIR)) {
  $DashboardDir = Resolve-DashboardFromOpenMemoryDir $env:OPENMEMORY_DIR
}
if ([string]::IsNullOrWhiteSpace($DashboardDir)) {
  # Auto-discover candidates.
  $candidates = @(
    (Join-Path $RepoRoot "..\openmemory\dashboard")
  )
  if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $candidates += @(
      (Join-Path $env:USERPROFILE "openmemory\dashboard"),
      (Join-Path $env:USERPROFILE "Documents\openmemory\dashboard"),
      (Join-Path $env:USERPROFILE "Documents\ai\openmemory\dashboard")
    )
  }
  foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { continue }
    $pkgJson  = Join-Path $candidate "package.json"
    $nextBin  = Join-Path $candidate "node_modules\.bin\next"
    if ((Test-Path -LiteralPath $pkgJson -PathType Leaf) -and (Test-Path -LiteralPath $nextBin)) {
      $DashboardDir = $candidate
      break
    }
    if (Test-Path -LiteralPath $pkgJson -PathType Leaf) {
      Write-Warning "发现 $candidate 但缺少 node_modules，跳过继续搜索。"
      Write-Host "       如需使用该目录，请先在该目录执行: npm install"
    }
  }
}

# Fail fast when an explicit directory does not exist.
if (-not [string]::IsNullOrWhiteSpace($DashboardDir) -and
    -not (Test-Path -LiteralPath $DashboardDir -PathType Container)) {
  throw "指定的 Dashboard 目录不存在: $DashboardDir"
}

# Fail fast when an explicit directory exists but is not a Node.js project.
if (-not [string]::IsNullOrWhiteSpace($DashboardDir) -and
    (Test-Path -LiteralPath $DashboardDir -PathType Container) -and
    -not (Test-Path -LiteralPath (Join-Path $DashboardDir "package.json") -PathType Leaf)) {
  throw "指定目录缺少 package.json: $DashboardDir`n请传入 OpenMemory repo root 或包含 package.json 的 dashboard 目录。若依赖尚未安装，请在 dashboard 目录执行 npm install"
}

if ([string]::IsNullOrWhiteSpace($DashboardDir)) {
  Write-Error "未找到可启动的 OpenMemory Dashboard"
  Write-Host "  已按顺序检查：指定目录、..\openmemory\dashboard、常见本地目录"
  Write-Host "  可选方案："
  Write-Host "    1. 将 OpenMemory checkout 放到 ..\openmemory"
  Write-Host "    2. 通过 -OpenMemoryDir D:\openmemory 指定目录"
  Write-Host "    3. 在 dashboard 目录执行 npm install 安装依赖"
  exit 1
}

# Resolve port.
$ResolvedPort = $Port
if ([string]::IsNullOrWhiteSpace($ResolvedPort)) {
  $ResolvedPort = $env:OPENMEMORY_DASHBOARD_PORT
}
if ([string]::IsNullOrWhiteSpace($ResolvedPort)) {
  $ResolvedPort = "3000"
}

# Validate port is a number in range 1-65535.
$portNumber = 0
if (-not [int]::TryParse($ResolvedPort, [ref]$portNumber) -or $portNumber -lt 1 -or $portNumber -gt 65535) {
  throw "Port 必须是 1-65535 之间的整数，得到: '$ResolvedPort'"
}

# Default NEXT_PUBLIC_API_URL: prefer explicit OPENMEMORY_BASE_URL, then localhost.
if ([string]::IsNullOrWhiteSpace($env:NEXT_PUBLIC_API_URL)) {
  if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_BASE_URL)) {
    $env:NEXT_PUBLIC_API_URL = $env:OPENMEMORY_BASE_URL
  } else {
    $omPort = if (-not [string]::IsNullOrWhiteSpace($env:OM_PORT)) { $env:OM_PORT } else { "8080" }
    $env:NEXT_PUBLIC_API_URL = "http://localhost:$omPort"
  }
}

$nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
  throw "未找到 node 命令，请安装 Node.js 并确认其在 PATH 中。"
}

$npmCmd = Get-Command "npm" -ErrorAction SilentlyContinue
if (-not $npmCmd) {
  throw "未找到 npm 命令，请安装 Node.js/npm 并确认其在 PATH 中。"
}

Write-Host "[INFO] Starting OpenMemory Dashboard..."
Write-Host "       dir=$DashboardDir"
Write-Host "       port=$ResolvedPort"
Write-Host "       api=$($env:NEXT_PUBLIC_API_URL)"

Set-Location -LiteralPath $DashboardDir
# Disable PSNativeCommandUseErrorActionPreference so npm's non-zero exit (e.g. Ctrl-C = 130)
# is captured as $LASTEXITCODE rather than being promoted to a terminating error.
$_savedNativeErrPref = if (Get-Variable -Name PSNativeCommandUseErrorActionPreference `
    -Scope Global -ErrorAction SilentlyContinue) {
  $global:PSNativeCommandUseErrorActionPreference
} else { $null }
if ($null -ne $_savedNativeErrPref) { $global:PSNativeCommandUseErrorActionPreference = $false }
& $npmCmd.Source run dev -- --port $ResolvedPort
$_exitCode = $LASTEXITCODE
if ($null -ne $_savedNativeErrPref) { $global:PSNativeCommandUseErrorActionPreference = $_savedNativeErrPref }
exit $_exitCode
