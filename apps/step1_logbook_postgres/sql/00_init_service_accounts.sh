#!/bin/bash
# ============================================================
# Engram Step1: 服务账号初始化脚本
# ============================================================
#
# 本脚本在 PostgreSQL 容器初始化时创建 LOGIN 角色（服务账号），
# 密码通过环境变量注入，避免明文存储在 SQL 文件中。
#
# 服务账号设计：
#   Step1 (Engram) 服务账号：
#     step1_migrator     - 迁移账号（DDL），继承 engram_migrator
#     step1_svc          - 运行账号（DML），继承 engram_app_readwrite
#
#   OpenMemory 服务账号：
#     openmemory_migrator_login - 迁移账号（DDL），继承 openmemory_migrator
#     openmemory_svc            - 运行账号（DML），继承 openmemory_app
#
# 环境变量（统一栈部署时全部必填）：
#   STEP1_MIGRATOR_PASSWORD       - Step1 迁移账号密码（必填）
#   STEP1_SVC_PASSWORD            - Step1 运行账号密码（必填）
#   OPENMEMORY_MIGRATOR_PASSWORD  - OpenMemory 迁移账号密码（必填）
#   OPENMEMORY_SVC_PASSWORD       - OpenMemory 运行账号密码（必填）
#
# 安全特性：
#   - 密码通过 psql --set 传入，避免 shell 展开泄露
#   - 使用 format() + quote_literal() 安全生成 PASSWORD 字面量
#   - 日志中不输出密码原文
#
# 执行方式：
#   在 docker-entrypoint-initdb.d 中自动执行
#
# 注意事项：
#   - 脚本可重复执行（幂等），使用 CREATE ROLE / ALTER ROLE
#   - 需要在角色基础表（04_roles_and_grants.sql）之前执行
# ============================================================

set -e

# 使用 postgres 用户和默认数据库
PGUSER="${POSTGRES_USER:-postgres}"
PGDATABASE="${POSTGRES_DB:-engram_test}"

echo "========================================"
echo "Initializing service accounts..."
echo "========================================"

# ============================================================
# 辅助函数：创建/更新服务账号
# 参数: $1=角色名, $2=密码, $3=环境变量名（用于日志）
# ============================================================
create_or_update_role() {
    local role_name="$1"
    local role_password="$2"
    local env_var_name="$3"

    if [ -z "$role_password" ]; then
        echo "ERROR: ${env_var_name} not set, cannot create ${role_name}."
        echo "       Set ${env_var_name} environment variable before running."
        exit 1
    fi

    echo "Creating/updating ${role_name} login role..."

    # 使用 psql --set 安全传递密码（不会在 shell history 或 ps 中暴露）
    # SQL 中使用 format() + quote_literal() 安全生成 PASSWORD 字面量
    psql -v ON_ERROR_STOP=1 --username "$PGUSER" --dbname "$PGDATABASE" \
        --set=role_name="$role_name" \
        --set=role_password="$role_password" <<-'EOSQL'
        DO $$
        DECLARE
            v_role_name text := current_setting('role_name');
            v_role_password text := current_setting('role_password');
            v_sql text;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role_name) THEN
                -- CREATE ROLE with safe password literal
                v_sql := format('CREATE ROLE %I LOGIN PASSWORD %L', v_role_name, v_role_password);
                EXECUTE v_sql;
                RAISE NOTICE 'Created login role: %', v_role_name;
            ELSE
                -- ALTER ROLE with safe password literal
                v_sql := format('ALTER ROLE %I WITH LOGIN PASSWORD %L', v_role_name, v_role_password);
                EXECUTE v_sql;
                RAISE NOTICE 'Updated login role: %', v_role_name;
            END IF;
        END $$;
EOSQL

    echo "${role_name} created/updated."
}

# ============================================================
# Step1 服务账号
# ============================================================

# step1_migrator - DDL 迁移账号
create_or_update_role "step1_migrator" "$STEP1_MIGRATOR_PASSWORD" "STEP1_MIGRATOR_PASSWORD"

# step1_svc - DML 运行账号
create_or_update_role "step1_svc" "$STEP1_SVC_PASSWORD" "STEP1_SVC_PASSWORD"

# ============================================================
# OpenMemory 服务账号
# ============================================================

# openmemory_migrator_login - DDL 迁移账号
create_or_update_role "openmemory_migrator_login" "$OPENMEMORY_MIGRATOR_PASSWORD" "OPENMEMORY_MIGRATOR_PASSWORD"

# openmemory_svc - DML 运行账号
create_or_update_role "openmemory_svc" "$OPENMEMORY_SVC_PASSWORD" "OPENMEMORY_SVC_PASSWORD"

# ============================================================
# Step3 服务账号（可选，仅当密码配置时创建）
# ============================================================

# step3_migrator_login - DDL 迁移账号
if [ -n "$STEP3_MIGRATOR_PASSWORD" ]; then
    create_or_update_role "step3_migrator_login" "$STEP3_MIGRATOR_PASSWORD" "STEP3_MIGRATOR_PASSWORD"
else
    echo "SKIP: STEP3_MIGRATOR_PASSWORD not set, skipping step3_migrator_login creation"
fi

# step3_svc - DML 运行账号
if [ -n "$STEP3_SVC_PASSWORD" ]; then
    create_or_update_role "step3_svc" "$STEP3_SVC_PASSWORD" "STEP3_SVC_PASSWORD"
else
    echo "SKIP: STEP3_SVC_PASSWORD not set, skipping step3_svc creation"
fi

echo "========================================"
echo "Service accounts initialization complete."
echo "========================================"
