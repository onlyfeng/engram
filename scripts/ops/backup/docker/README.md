# Docker 备份（宿主机脚本）

1) 赋权：
   chmod +x backup.sh
2) 执行：
   export POSTGRES_CONTAINER=<你的 postgres 容器名>
   export PGPASSWORD=<postgres 密码>
   ./backup.sh
3) 定时：
   - Linux：cron
   - Windows：计划任务（调用 wsl bash 或直接执行 docker + powershell）
