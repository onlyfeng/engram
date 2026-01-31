<#
最小备份脚本（pg_dump）
- 建议每天一次（计划任务触发）
- 轮转：保留 N 天
#>

param(
  [string]$PgHost = "127.0.0.1",
  [int]$PgPort = 5432,
  [string]$PgDb = "engram_project",
  [string]$PgUser = "postgres",
  [string]$OutDir = "D:\engram-backups",
  [int]$KeepDays = 7
)

$ErrorActionPreference = "Stop"

function Require-Exe($name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "找不到可执行文件：$name，请确保 Postgres bin 在 PATH" }
}
Require-Exe "pg_dump"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$outFile = Join-Path $OutDir "engram_${PgDb}_${ts}.dump"

Write-Host "Backing up to: $outFile"
pg_dump -h $PgHost -p $PgPort -U $PgUser -F c -f $outFile $PgDb

$limit = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $OutDir -Filter "engram_${PgDb}_*.dump" |
  Where-Object { $_.LastWriteTime -lt $limit } |
  Remove-Item -Force

Write-Host "✅ Backup done."
