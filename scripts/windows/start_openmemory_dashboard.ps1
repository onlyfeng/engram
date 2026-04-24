<#
Start the OpenMemory Dashboard (Next.js dev server) using local Engram env files.

Usage:
  .\scripts\windows\start_openmemory_dashboard.ps1
  .\scripts\windows\start_openmemory_dashboard.ps1 -OpenMemoryDir "D:\openmemory"
  .\scripts\windows\start_openmemory_dashboard.ps1 -Port 3001
  .\scripts\windows\start_openmemory_dashboard.ps1 -Help

Notes:
- Loads .env / .env.local / .env.ps1 automatically
- Resolution order: -OpenMemoryDir / OPENMEMORY_DASHBOARD_DIR, ..\openmemory\dashboard, common local dirs
- -OpenMemoryDir accepts either the OpenMemory repo root or the dashboard directory directly
- NEXT_PUBLIC_API_URL defaults to http://localhost:<OM_PORT|8080> when not set
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

# Resolve -OpenMemoryDir: relative paths against caller CWD.
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir) -and
    -not [System.IO.Path]::IsPathRooted($OpenMemoryDir)) {
  $OpenMemoryDir = Join-Path $CallerDir $OpenMemoryDir
}

# Determine dashboard directory.
$DashboardDir = ""
if (-not [string]::IsNullOrWhiteSpace($OpenMemoryDir)) {
  # Accept repo root (contains dashboard\) or a direct dashboard path.
  $dashSub = Join-Path $OpenMemoryDir "dashboard"
  if (Test-Path -PathType Container $dashSub) {
    $DashboardDir = $dashSub
  } else {
    $DashboardDir = $OpenMemoryDir
  }
} elseif (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_DASHBOARD_DIR)) {
  $DashboardDir = $env:OPENMEMORY_DASHBOARD_DIR
} else {
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
    if (-not (Test-Path -PathType Container $candidate)) { continue }
    $pkgJson  = Join-Path $candidate "package.json"
    $nextBin  = Join-Path $candidate "node_modules\.bin\next"
    if ((Test-Path -PathType Leaf $pkgJson) -and (Test-Path $nextBin)) {
      $DashboardDir = $candidate
      break
    }
    if (Test-Path -PathType Leaf $pkgJson) {
      Write-Warning "发现 $candidate 但缺少 node_modules，跳过继续搜索。"
      Write-Host "       如需使用该目录，请先在该目录执行: npm install"
    }
  }
}

# Fail fast when an explicit directory does not exist.
if (-not [string]::IsNullOrWhiteSpace($DashboardDir) -and
    -not (Test-Path -PathType Container $DashboardDir)) {
  throw "指定的 Dashboard 目录不存在: $DashboardDir"
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

# Default NEXT_PUBLIC_API_URL.
if ([string]::IsNullOrWhiteSpace($env:NEXT_PUBLIC_API_URL)) {
  $omPort = if (-not [string]::IsNullOrWhiteSpace($env:OM_PORT)) { $env:OM_PORT } else { "8080" }
  $env:NEXT_PUBLIC_API_URL = "http://localhost:$omPort"
}

Write-Host "[INFO] Starting OpenMemory Dashboard..."
Write-Host "       dir=$DashboardDir"
Write-Host "       port=$ResolvedPort"
Write-Host "       api=$($env:NEXT_PUBLIC_API_URL)"

$nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
  throw "未找到 node 命令，请确认 Node.js 已安装并在 PATH 中。"
}

Set-Location $DashboardDir
& npm run dev -- --port $ResolvedPort
exit $LASTEXITCODE
