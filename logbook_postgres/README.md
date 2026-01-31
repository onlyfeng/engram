# Logbook Postgres 工具集

本目录包含 Logbook 的迁移与工具脚本，供本地/CI/部署使用。

---

## 主要脚本

- `scripts/db_bootstrap.py`：创建服务账号（可选）
- `scripts/db_migrate.py`：执行迁移与权限脚本
- `scripts/logbook_cli.py`：Logbook CLI 入口

---

## 相关文档

- [Logbook 部署与验收](../docs/logbook/03_deploy_verify_troubleshoot.md)
- [环境变量参考](../docs/reference/environment_variables.md)

---

更新时间：2026-01-31
