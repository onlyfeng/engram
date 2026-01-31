# 最小安全清单（能跑 + 不容易翻车）

## 1) 网络与端口
- Postgres 端口（5432）尽量不对外暴露：只允许本机/内网网段
- Engram（8787）与 OpenMemory（8080）只开放内网网段
- Dashboard（Metabase/pgAdmin）尽量限制为管理员访问（或内网 + Basic Auth）

## 2) 密钥与配置
- 必配：
  - ENGRAM_API_KEY（写操作强制）
  - OM_API_KEY（OpenMemory 侧写操作强制）
- 密钥不要写进 repo：
  - Windows：用系统环境变量或受控配置文件（ACL）
  - Docker：用 `.env`（仅服务器保存）或 Docker secrets（后续可上）

## 3) 数据库权限（最小权限）
- logbook_svc（继承 `engram_app_readwrite`）：仅 DML（logbook/identity/scm/analysis/governance）
- openmemory_svc（继承 `openmemory_app`）：仅 DML（openmemory schema）
- dashboard_ro：只读（Metabase/pgAdmin）

## 4) 审计
- Engram 对每次写入记录 audit event（谁、何时、写了什么、是否成功）
- 证据包输出记录 evidence references（便于复查）

## 5) 备份与演练
- 每日 pg_dump（custom 格式）+ 轮转
- 每月至少做一次恢复演练（选一个备份恢复到临时库）
