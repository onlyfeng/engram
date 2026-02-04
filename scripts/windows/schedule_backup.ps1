<#
Create a scheduled task to run the backup script daily.

Notes:
- Run as Administrator
#>

param(
  [string]$TaskName = "EngramDailyBackup",
  [string]$BackupScript = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\backup\backup.ps1",
  [string]$OutDir = "D:\engram-backups",
  [string]$PgDb = "engram",
  [string]$PgUser = "postgres",
  [string]$PgHost = "127.0.0.1",
  [int]$PgPort = 5432,
  [string]$Time = "03:30"
)

$ErrorActionPreference = "Stop"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`" -PgHost $PgHost -PgPort $PgPort -PgDb $PgDb -PgUser $PgUser -OutDir `"$OutDir`""
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Parse($Time))
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Write-Host "OK: scheduled task created: $TaskName (daily at $Time)"
