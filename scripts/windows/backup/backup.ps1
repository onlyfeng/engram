<#
Minimal backup script (pg_dump)
- Recommended: run daily (via scheduled task)
- Rotation: keep N days
#>

param(
  [string]$PgHost = "127.0.0.1",
  [int]$PgPort = 5432,
  [string]$PgDb = "engram",
  [string]$PgUser = "postgres",
  [string]$OutDir = "D:\engram-backups",
  [int]$KeepDays = 7
)

$ErrorActionPreference = "Stop"

function Require-Exe($name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Missing executable: $name. Ensure Postgres bin is on PATH." }
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

Write-Host "OK: backup done."
