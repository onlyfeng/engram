#!/bin/bash
# ============================================================
# Engram Step1: OpenMemory Schema 权限管理 Wrapper 脚本
# ============================================================
#
# 本脚本用于在 initdb 阶段注入 schema 参数并执行 SQL 模板。
#
# 设计目的：
#   - 支持从环境变量 OM_PG_SCHEMA 读取目标 schema 名称
#   - 通过 SET om.target_schema = '...' 注入参数到 SQL
#   - 用于 docker-entrypoint-initdb.d 自动执行场景
#
# 环境变量：
#   OM_PG_SCHEMA     - OpenMemory 目标 schema 名称（可选，默认: openmemory）
#   POSTGRES_USER    - PostgreSQL 用户（默认: postgres）
#   POSTGRES_DB      - 目标数据库（默认: engram_test）
#
# 使用方式：
#   # 使用默认 schema 'openmemory'
#   ./05_openmemory_roles_and_grants.sh
#
#   # 指定 schema 名称
#   OM_PG_SCHEMA=myproject_openmemory ./05_openmemory_roles_and_grants.sh
#
#   # 在 docker-compose 中配置
#   environment:
#     OM_PG_SCHEMA: ${PROJECT_KEY:-default}_openmemory
#
# 注意事项：
#   - 脚本可重复执行（幂等）
#   - 需要在角色基础表创建之后执行（顺序 05_）
#   - 若 OM_PG_SCHEMA 为 'public'，脚本将报错退出（禁止配置）
# ============================================================

set -e

# ============================================================
# 配置参数
# ============================================================

# 目标 schema，默认 'openmemory'
OM_SCHEMA="${OM_PG_SCHEMA:-openmemory}"

# PostgreSQL 连接参数
PGUSER="${POSTGRES_USER:-postgres}"
PGDATABASE="${POSTGRES_DB:-engram_test}"

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_FILE="${SCRIPT_DIR}/05_openmemory_roles_and_grants.sql"

# ============================================================
# 预检：禁止使用 public schema
# ============================================================

if [ "$OM_SCHEMA" = "public" ]; then
    echo "========================================" >&2
    echo "[FATAL] OM_PG_SCHEMA=public 是禁止的配置！" >&2
    echo "" >&2
    echo "原因：" >&2
    echo "  1. public schema 是 PostgreSQL 默认 schema，可能包含其他应用的表" >&2
    echo "  2. 无法使用 pg_dump --schema 进行隔离备份" >&2
    echo "  3. DROP SCHEMA public CASCADE 会破坏整个数据库" >&2
    echo "" >&2
    echo "解决方案：" >&2
    echo "  设置环境变量 OM_PG_SCHEMA 为非 public 值，例如：" >&2
    echo "  - OM_PG_SCHEMA=openmemory" >&2
    echo "  - OM_PG_SCHEMA=\${PROJECT_KEY}_openmemory" >&2
    echo "========================================" >&2
    exit 1
fi

# ============================================================
# 检查 SQL 文件是否存在
# ============================================================

if [ ! -f "$SQL_FILE" ]; then
    echo "[ERROR] SQL file not found: $SQL_FILE" >&2
    exit 1
fi

# ============================================================
# 执行 SQL 脚本
# ============================================================

echo "========================================"
echo "OpenMemory Schema Permissions Setup"
echo "========================================"
echo "Target schema: $OM_SCHEMA"
echo "Database: $PGDATABASE"
echo "User: $PGUSER"
echo "SQL file: $SQL_FILE"
echo "========================================"

# 使用 SET 语句注入 schema 参数，然后执行 SQL 文件
# 通过 -c 执行 SET 后用 -f 执行 SQL 文件
psql -v ON_ERROR_STOP=1 \
     --username "$PGUSER" \
     --dbname "$PGDATABASE" \
     -c "SET om.target_schema = '$OM_SCHEMA'" \
     -f "$SQL_FILE"

echo "========================================"
echo "OpenMemory schema permissions applied successfully."
echo "Target schema: $OM_SCHEMA"
echo "========================================"
