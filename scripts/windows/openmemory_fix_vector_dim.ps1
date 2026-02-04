<#
Fix OpenMemory vector column dimension (PostgreSQL 18 / HNSW):
Equivalent to `make openmemory-fix-vector-dim`.
#
Usage:
  .\scripts\windows\openmemory_fix_vector_dim.ps1 -VecDim 1536
  .\scripts\windows\openmemory_fix_vector_dim.ps1 -VecDim 1536 -Schema "openmemory"
  .\scripts\windows\openmemory_fix_vector_dim.ps1 -VecDim 1536 -AdminDsn "postgresql://postgres@127.0.0.1:5432/engram"
#
Notes:
- Requires psql on PATH (PGROOT/bin or PostgreSQL install dir)
- Provide password via $env:PGPASSWORD or pgpass.conf
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [int]$VecDim,
  [string]$ConfigPath = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\config.ps1",
  [string]$Schema = "",
  [string]$AdminDsn = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Set-PgClientEncodingFromConsole() {
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
  if (Get-Command "psql" -ErrorAction SilentlyContinue) { return }

  $candidates = New-Object System.Collections.Generic.List[string]
  if ($env:PGROOT) { $candidates.Add((Join-Path $env:PGROOT "bin")) }

  $pgRootVar = Get-Variable -Name "PgRoot" -ErrorAction SilentlyContinue
  if ($null -eq $pgRootVar) { $pgRootVar = Get-Variable -Name "PgRoot" -Scope Global -ErrorAction SilentlyContinue }
  if ($pgRootVar -and $pgRootVar.Value) { $candidates.Add((Join-Path ([string]$pgRootVar.Value) "bin")) }

  $pgBinVar = Get-Variable -Name "PgBinDir" -ErrorAction SilentlyContinue
  if ($null -eq $pgBinVar) { $pgBinVar = Get-Variable -Name "PgBinDir" -Scope Global -ErrorAction SilentlyContinue }
  if ($pgBinVar -and $pgBinVar.Value) { $candidates.Add([string]$pgBinVar.Value) }

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

  $commonRoots = @("C:\Program Files\PostgreSQL", "D:\Program Files\PostgreSQL")
  foreach ($r in $commonRoots) {
    if (-not (Test-Path $r)) { continue }
    try {
      Get-ChildItem $r -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { $candidates.Add((Join-Path $_.FullName "bin")) }
    } catch { }
  }

  foreach ($bin in $candidates | Select-Object -Unique) {
    if (-not $bin) { continue }
    if (-not (Test-Path $bin)) { continue }
    if ((Test-Path (Join-Path $bin "psql.exe"))) {
      $env:Path = "$bin;$env:Path"
      return
    }
  }
}

function Require-Exe($name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Missing executable: $name. Ensure Postgres bin is on PATH." }
}

function Invoke-Psql($dsn, $sql) {
  $out = & psql "$dsn" -v ON_ERROR_STOP=1 -c $sql 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] psql failed."
    Write-Host "        Hint: set `$env:PGPASSWORD or configure pgpass.conf (%APPDATA%\\postgresql\\pgpass.conf)."
    if ($out) {
      Write-Host ""
      Write-Host "---- psql output ----"
      $out | ForEach-Object { Write-Host $_ }
      Write-Host "---------------------"
    }
    throw "psql failed (exit=$LASTEXITCODE)"
  }
}

function Mask-Dsn([string]$dsn) {
  if ([string]::IsNullOrWhiteSpace($dsn)) { return $dsn }
  if ($dsn -match '^(.*?://)([^@]+)@(.+)$') {
    $scheme = $matches[1]
    $userinfo = $matches[2]
    $rest = $matches[3]
    if ($userinfo -match '^([^:]+):(.+)$') {
      $user = $matches[1]
      return "$scheme${user}:***@$rest"
    }
  }
  return $dsn
}

function Assert-SafeIdentifier([string]$value, [string]$label) {
  if ([string]::IsNullOrWhiteSpace($value)) { throw "Missing $label" }
  if ($value -eq "public") { throw "$label cannot be public" }
  if ($value -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw "Invalid ${label}: ${value} (allowed: [A-Za-z_][A-Za-z0-9_]*)"
  }
}

function Test-PgSchemaExists($dsn, [string]$schemaName) {
  $out = & psql "$dsn" -Atc "SELECT 1 FROM information_schema.schemata WHERE schema_name='${schemaName}' LIMIT 1;" 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] psql failed while checking schema: $schemaName"
    Write-Host "        Hint: set `$env:PGPASSWORD or configure pgpass.conf (%APPDATA%\\postgresql\\pgpass.conf)."
    if ($out) {
      Write-Host ""
      Write-Host "---- psql output ----"
      $out | ForEach-Object { Write-Host $_ }
      Write-Host "---------------------"
    }
    throw "psql schema existence check failed (exit=$LASTEXITCODE)"
  }
  return ([string]$out).Trim() -eq "1"
}

function Test-PgTableExists($dsn, [string]$schemaName, [string]$tableName) {
  $out = & psql "$dsn" -Atc "SELECT 1 FROM information_schema.tables WHERE table_schema='${schemaName}' AND table_name='${tableName}' LIMIT 1;" 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] psql failed while checking table: ${schemaName}.${tableName}"
    Write-Host "        Hint: set `$env:PGPASSWORD or configure pgpass.conf (%APPDATA%\\postgresql\\pgpass.conf)."
    if ($out) {
      Write-Host ""
      Write-Host "---- psql output ----"
      $out | ForEach-Object { Write-Host $_ }
      Write-Host "---------------------"
    }
    throw "psql table existence check failed (exit=$LASTEXITCODE)"
  }
  return ([string]$out).Trim() -eq "1"
}

Set-PgClientEncodingFromConsole
Try-AddPostgresBinToPath
Require-Exe "psql"

if (Test-Path $ConfigPath) {
  . $ConfigPath
}

if ([string]::IsNullOrWhiteSpace($Schema)) {
  if ($env:OM_PG_SCHEMA) {
    $Schema = [string]$env:OM_PG_SCHEMA
  } elseif ($OpenMemoryEnv -and ($OpenMemoryEnv -is [System.Collections.IDictionary]) -and $OpenMemoryEnv.Contains("OM_PG_SCHEMA")) {
    $Schema = [string]$OpenMemoryEnv["OM_PG_SCHEMA"]
  } else {
    $Schema = "openmemory"
  }
}

Assert-SafeIdentifier $Schema "schema"

if ([string]::IsNullOrWhiteSpace($AdminDsn)) {
  if ($env:ENGRAM_PG_ADMIN_DSN) {
    $AdminDsn = [string]$env:ENGRAM_PG_ADMIN_DSN
  } else {
    $pgHost = if ($env:PGHOST) { $env:PGHOST } elseif ($PgHost) { $PgHost } else { "127.0.0.1" }
    $pgPort = if ($env:PGPORT) { $env:PGPORT } elseif ($PgPort) { $PgPort } else { "5432" }
    $pgDb = if ($env:PGDATABASE) { $env:PGDATABASE } elseif ($PgDb) { $PgDb } else { "engram" }
    $pgUser = if ($env:PGUSER) { $env:PGUSER } elseif ($PgSuperUser) { $PgSuperUser } else { "postgres" }
    $AdminDsn = "postgresql://$pgUser@${pgHost}:${pgPort}/${pgDb}"
  }
}

Write-Host "[INFO] Fixing vector dimension for OpenMemory"
Write-Host "       schema=$Schema"
Write-Host "       dim=$VecDim"
Write-Host ("       admin_dsn={0}" -f (Mask-Dsn $AdminDsn))

if (-not (Test-PgSchemaExists $AdminDsn $Schema)) {
  throw "Schema does not exist: $Schema. Ensure OpenMemory schema is created before running this fix."
}
if (-not (Test-PgTableExists $AdminDsn $Schema "openmemory_vectors")) {
  throw "Table does not exist: $Schema.openmemory_vectors. Ensure OpenMemory has initialized its tables, then retry."
}

Invoke-Psql $AdminDsn "DROP INDEX IF EXISTS $Schema.openmemory_vectors_v_idx;"
Invoke-Psql $AdminDsn "ALTER TABLE $Schema.openmemory_vectors ALTER COLUMN v TYPE vector($VecDim);"

Write-Host "[OK] Vector dimension fixed. Restart OpenMemory (opm serve) to rebuild index."
