param(
  [string]$EnvLocalFile = $env:ENV_LOCAL_FILE
)

function Import-DotenvFile {
  param([string]$Path)
  if (-not (Test-Path $Path)) { return $false }

  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) { return }
    if ($line.StartsWith("#")) { return }
    if ($line -notmatch '^[A-Za-z_][A-Za-z0-9_]*\s*=') { return }

    $parts = $line -split "=", 2
    $key = $parts[0].Trim()
    $value = $parts[1].Trim()

    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
      $value = $value.Substring(1, $value.Length - 2)
    }

    Set-Item -Path ("Env:{0}" -f $key) -Value $value
  }

  return $true
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

if ([string]::IsNullOrWhiteSpace($EnvLocalFile)) { $EnvLocalFile = ".env.local" }
$envPath = Join-Path $RepoRoot ".env"
$envLocalPath = if ([System.IO.Path]::IsPathRooted($EnvLocalFile)) { $EnvLocalFile } else { Join-Path $RepoRoot $EnvLocalFile }

$loaded = @()
if (Import-DotenvFile -Path $envPath) { $loaded += ".env" }
if (Import-DotenvFile -Path $envLocalPath) { $loaded += $envLocalPath }

if ($loaded.Count -eq 0) {
  Write-Warning "未找到 .env 或 $EnvLocalFile"
} else {
  Write-Host ("[OK] 已加载: {0}" -f ($loaded -join ", "))
}
