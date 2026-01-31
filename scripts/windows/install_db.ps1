<#
初始化数据库（最小可用）：
- 执行 Logbook 迁移与角色/权限脚本
说明：
- 需要能以超级用户连接 Postgres（默认 postgres）
- 密码可通过 $env:PGPASSWORD 或 PGPASSFILE 提供
#>

param(
  [string]$ConfigPath = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\config.ps1"
)

. $ConfigPath

$ErrorActionPreference = "Stop"

function Require-Exe($name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "找不到可执行文件：$name，请确保 Postgres bin 在 PATH" }
}

Require-Exe "psql"
Require-Exe "python"

Write-Host "[1/2] Bootstrap 服务账号（可选，依赖 LOGBOOK/OPENMEMORY 密码环境变量）"
$adminDsn = "postgresql://$PgSuperUser@${PgHost}:${PgPort}/${PgDb}"
python "$(Resolve-Path "$PSScriptRoot\..\..\logbook_postgres\scripts\db_bootstrap.py")" --dsn "$adminDsn"

Write-Host "[2/2] 执行迁移与权限脚本"
python "$(Resolve-Path "$PSScriptRoot\..\..\logbook_postgres\scripts\db_migrate.py")" --dsn "$adminDsn" --apply-roles --apply-openmemory-grants

Write-Host "✅ 数据库初始化完成"
