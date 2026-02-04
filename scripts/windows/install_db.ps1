<#
Initialize database (minimal usable):
- Create database if missing
- Enable pgvector extension
- Bootstrap service roles (optional; requires LOGBOOK/OPENMEMORY password env vars)
- Apply migrations + grants

Notes:
- Requires superuser/admin connection to Postgres (default: postgres)
- Password can be provided via $env:PGPASSWORD or PGPASSFILE
#>

param(
  [string]$ConfigPath = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\config.ps1"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ConfigPath)) {
  $example = Join-Path (Split-Path -Parent $ConfigPath) "config.ps1.example"
  throw "Missing config: $ConfigPath. Copy $example to $ConfigPath and edit values, or run scripts/windows/setup_db.ps1 to create it interactively. (config.ps1 should stay local and is gitignored.)"
}
. $ConfigPath

function Set-PgClientEncodingFromConsole() {
  # Avoid garbled psql output on Windows consoles (e.g. CP936).
  if ($env:PGCLIENTENCODING) { return }
  $cp = [Console]::OutputEncoding.CodePage
  switch ($cp) {
    65001 { $env:PGCLIENTENCODING = "UTF8"; break }
    936   { $env:PGCLIENTENCODING = "GBK"; break }
    950   { $env:PGCLIENTENCODING = "BIG5"; break }
    932   { $env:PGCLIENTENCODING = "SJIS"; break }
    default { break }
  }
}

function Try-AddPostgresBinToPath() {
  # If psql is already available, do nothing
  if (Get-Command "psql" -ErrorAction SilentlyContinue) { return }

  $candidates = New-Object System.Collections.Generic.List[string]

  # 1) Explicit env var (common in docs): PGROOT
  if ($env:PGROOT) {
    $candidates.Add((Join-Path $env:PGROOT "bin"))
  }

  # 2) Optional config var: PgRoot / PgBinDir
  $pgRootVar = Get-Variable -Name "PgRoot" -ErrorAction SilentlyContinue
  if ($null -eq $pgRootVar) { $pgRootVar = Get-Variable -Name "PgRoot" -Scope Global -ErrorAction SilentlyContinue }
  if ($pgRootVar -and $pgRootVar.Value) {
    $candidates.Add((Join-Path ([string]$pgRootVar.Value) "bin"))
  }

  $pgBinVar = Get-Variable -Name "PgBinDir" -ErrorAction SilentlyContinue
  if ($null -eq $pgBinVar) { $pgBinVar = Get-Variable -Name "PgBinDir" -Scope Global -ErrorAction SilentlyContinue }
  if ($pgBinVar -and $pgBinVar.Value) {
    $candidates.Add([string]$pgBinVar.Value)
  }

  # 3) Registry: PostgreSQL Windows installer records Base Directory
  $regRoots = @(
    "HKLM:\SOFTWARE\PostgreSQL\Installations",
    "HKLM:\SOFTWARE\WOW6432Node\PostgreSQL\Installations"
  )
  foreach ($root in $regRoots) {
    try {
      if (Test-Path $root) {
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
          try {
            $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($p -and $p.'Base Directory') {
              $candidates.Add((Join-Path ([string]$p.'Base Directory') "bin"))
            }
          } catch { }
        }
      }
    } catch { }
  }

  # 4) Common default install locations (best-effort)
  $commonRoots = @(
    "C:\Program Files\PostgreSQL",
    "D:\Program Files\PostgreSQL"
  )
  foreach ($r in $commonRoots) {
    if (-not (Test-Path $r)) { continue }
    try {
      # Prefer higher versions first
      Get-ChildItem $r -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { $candidates.Add((Join-Path $_.FullName "bin")) }
    } catch { }
  }

  foreach ($bin in $candidates | Select-Object -Unique) {
    if (-not $bin) { continue }
    if (-not (Test-Path $bin)) { continue }
    $psqlPath = Join-Path $bin "psql.exe"
    $createdbPath = Join-Path $bin "createdb.exe"
    if ((Test-Path $psqlPath) -and (Test-Path $createdbPath)) {
      $env:Path = "$bin;$env:Path"
      return
    }
  }
}

function Require-Exe($name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Missing executable: $name. Ensure Postgres bin and venv Scripts are on PATH (activate venv first)." }
}

Set-PgClientEncodingFromConsole
Try-AddPostgresBinToPath

Require-Exe "psql"
Require-Exe "createdb"
Require-Exe "engram-bootstrap-roles"
Require-Exe "engram-migrate"

Write-Host "[1/4] Create database (skip if exists)"
$bootstrapDsn = "postgresql://$PgSuperUser@${PgHost}:${PgPort}/postgres"
$dbExistsRaw = & psql "$bootstrapDsn" -Atc "SELECT COUNT(1) FROM pg_database WHERE datname='${PgDb}';" 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host "[ERROR] psql connection check failed."
  Write-Host "        Hint: set `$env:PGPASSWORD or configure pgpass.conf (%APPDATA%\\postgresql\\pgpass.conf)."
  if ($dbExistsRaw) {
    Write-Host ""
    Write-Host "---- psql output ----"
    $dbExistsRaw | ForEach-Object { Write-Host $_ }
    Write-Host "---------------------"
  }
  throw "psql database existence check failed (exit=$LASTEXITCODE)"
}
# Note: Use COUNT(1) so we always get 0/1 when the query succeeds.
$dbExists = "0"
$line = ($dbExistsRaw | Where-Object { $_ -match "\S" } | Select-Object -First 1)
if ($null -ne $line) { $dbExists = ([string]$line).Trim() }
if ($dbExists -ne "1") {
  & createdb -h $PgHost -p $PgPort -U $PgSuperUser $PgDb
  if ($LASTEXITCODE -ne 0) { throw "createdb failed (exit=$LASTEXITCODE)" }
} else {
  Write-Host "  - Database exists: $PgDb"
}

Write-Host "[2/4] Enable pgvector extension"
$adminDsn = "postgresql://$PgSuperUser@${PgHost}:${PgPort}/${PgDb}"
& psql "$adminDsn" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Enable pgvector failed (exit=$LASTEXITCODE)" }

Write-Host "[3/4] Bootstrap service roles (optional; requires password env vars)"
& engram-bootstrap-roles --dsn "$bootstrapDsn"
if ($LASTEXITCODE -ne 0) { throw "engram-bootstrap-roles failed (exit=$LASTEXITCODE)" }

Write-Host "[4/4] Run migrations + grants"
& engram-migrate --dsn "$adminDsn" --apply-roles --apply-openmemory-grants
if ($LASTEXITCODE -ne 0) { throw "engram-migrate failed (exit=$LASTEXITCODE)" }

Write-Host "OK: database initialization completed"
