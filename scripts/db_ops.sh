#!/bin/bash
#
# Engram 数据库运维脚本
#
# 提供以下功能：
#   - precheck: 配置预检（验证环境变量）
#   - backup: pg_dump 备份（按 schema 或全库）
#   - restore: 恢复备份
#   - cleanup: 清理 schema（仅空库/测试环境）
#   - verify: 权限验证 + 关键表存在性检查
#   - bootstrap: 初始化角色、权限和安全配置
#   - rotate: 轮换密码（调用 bootstrap rotate 子命令）
#
# 使用方法:
#   ./scripts/db_ops.sh precheck              # 配置预检
#   ./scripts/db_ops.sh backup                # 备份所有 schema
#   ./scripts/db_ops.sh backup --schema om    # 仅备份 openmemory schema
#   ./scripts/db_ops.sh backup --full         # 全库备份
#   ./scripts/db_ops.sh restore backup.sql    # 恢复备份
#   ./scripts/db_ops.sh restore backup.sql --yes  # 非交互式恢复（CI 场景）
#   ./scripts/db_ops.sh cleanup --schema om   # 清理指定 schema（危险操作）
#   ./scripts/db_ops.sh cleanup --schema om --yes  # 非交互式清理（CI 场景）
#   ./scripts/db_ops.sh verify                # 验证权限配置和关键表
#   ./scripts/db_ops.sh bootstrap             # 初始化数据库角色和权限
#   ./scripts/db_ops.sh rotate                # 轮换数据库密码
#
# 升级与回滚:
#   ./scripts/db_ops.sh pre-upgrade           # 升级前备份（开发环境，备份 OM schema）
#   ./scripts/db_ops.sh pre-upgrade --full    # 升级前备份（生产环境，全库备份）
#
# ============================================================================
# 升级与回滚策略
# ============================================================================
#
# 升级前必须备份（强制要求）:
#   - 开发/测试环境: ./scripts/db_ops.sh pre-upgrade
#   - 生产环境: ./scripts/db_ops.sh pre-upgrade --full
#
# 回滚路径:
#   1. 回退 OpenMemory.upstream.lock.json 到之前的 commit ref
#   2. 重新构建镜像: make openmemory-build
#   3. 恢复备份: ./scripts/db_ops.sh restore <backup_file.sql>
#
# 不可逆迁移:
#   OpenMemory 的某些 schema 迁移是不可逆的（如删除列、重命名表等）。
#   对于不可逆迁移，唯一回滚策略是: 从备份恢复。
#   因此升级前备份是强制要求，没有例外。
#
# ============================================================================
#
# 环境变量（敏感参数只通过环境变量读取，禁止在命令行传递密码）:
#   POSTGRES_HOST: PostgreSQL 主机（默认 localhost）
#   POSTGRES_PORT: PostgreSQL 端口（默认 5432）
#   POSTGRES_DB: 数据库名（默认 engram）
#   POSTGRES_USER: 用户名（默认 postgres）
#   POSTGRES_PASSWORD: 密码（敏感，仅从环境变量读取）
#   PROJECT_KEY: 项目标识（用于 Step1 表前缀）
#   OM_PG_SCHEMA: OpenMemory schema 名（默认 openmemory）
#   BACKUP_DIR: 备份目录（默认 ./backups）
#   ENGRAM_PG_ADMIN_DSN: 管理员 DSN（bootstrap 使用）
#
# 安全说明:
#   - 所有密码参数只从环境变量读取，避免 shell history 泄露
#   - 禁止在命令行参数中传递密码
#

set -e

# ============================================================================
# 配置
# ============================================================================
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-engram}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
PROJECT_KEY="${PROJECT_KEY:-default}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

# Schema 列表
STEP1_SCHEMAS="identity logbook scm analysis governance"
# OpenMemory schema: 优先读取 OM_PG_SCHEMA 环境变量，默认为 openmemory
OM_SCHEMA="${OM_PG_SCHEMA:-openmemory}"

# 脚本目录（用于定位 db_bootstrap.py 等脚本）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
STEP1_DIR="${PROJECT_ROOT}/apps/step1_logbook_postgres"
SQL_DIR="${STEP1_DIR}/sql"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ============================================================================
# 预检函数
# ============================================================================

# 检测是否为统一栈/同库模式
# 返回: 0 = 统一栈模式, 1 = 非统一栈模式
is_unified_stack_mode() {
    # 检测条件 1: OM_METADATA_BACKEND=postgres
    if [ "${OM_METADATA_BACKEND:-}" = "postgres" ]; then
        return 0
    fi
    
    # 检测条件 2: 显式设置了任意一个服务账号密码环境变量（表示用户意图使用统一栈）
    if [ -n "${STEP1_MIGRATOR_PASSWORD:-}" ] || \
       [ -n "${STEP1_SVC_PASSWORD:-}" ] || \
       [ -n "${OPENMEMORY_MIGRATOR_PASSWORD:-}" ] || \
       [ -n "${OPENMEMORY_SVC_PASSWORD:-}" ]; then
        return 0
    fi
    
    # 检测条件 3: 通过命令行参数显式请求（由调用者设置 ENGRAM_UNIFIED_MODE=1）
    if [ "${ENGRAM_UNIFIED_MODE:-}" = "1" ]; then
        return 0
    fi
    
    return 1
}

do_precheck() {
    echo "========================================"
    echo "Engram 配置预检"
    echo "========================================"
    
    local has_error=0
    local has_warn=0
    local missing_passwords=()
    
    # CI 模式检测：CI=1 时冲突强制失败；本地默认为 warn
    local ci_mode=0
    if [ "${CI:-}" = "1" ]; then
        ci_mode=1
    fi
    
    # 检查 OM_PG_SCHEMA 是否为 public
    if [ "$OM_SCHEMA" = "public" ]; then
        log_error "OM_PG_SCHEMA=public 是禁止的配置！"
        echo ""
        echo "原因："
        echo "  1. public schema 是 PostgreSQL 默认 schema，可能包含其他应用的表"
        echo "  2. 无法使用 pg_dump --schema 进行隔离备份"
        echo "  3. DROP SCHEMA public CASCADE 会破坏整个数据库"
        echo ""
        echo "解决方案："
        echo "  设置环境变量 OM_PG_SCHEMA 为非 public 值"
        echo "  例如: export OM_PG_SCHEMA=myproject_openmemory"
        has_error=1
    else
        log_info "OM_PG_SCHEMA=$OM_SCHEMA (非 public) ✓"
    fi
    
    # 显示 OpenMemory schema 配置来源
    if [ -n "$OM_PG_SCHEMA" ]; then
        log_info "OM_PG_SCHEMA 来源: 环境变量显式配置"
    else
        log_info "OM_PG_SCHEMA 来源: 默认值 'openmemory'"
    fi
    
    # 检查 PROJECT_KEY（仅用于 Step1 schema 前缀提示）
    if [ "$PROJECT_KEY" = "default" ]; then
        log_warn "PROJECT_KEY 使用默认值 'default'（影响 Step1 表前缀，OpenMemory 使用 OM_PG_SCHEMA）"
    else
        log_info "PROJECT_KEY=$PROJECT_KEY ✓"
    fi
    
    # ========================================================================
    # 统一栈/同库模式: 检查服务账号密码环境变量
    # ========================================================================
    if is_unified_stack_mode; then
        echo ""
        log_info "检测到统一栈模式（OM_METADATA_BACKEND=${OM_METADATA_BACKEND:-未设置}）"
        log_info "检查服务账号密码环境变量..."
        
        # 检查 STEP1_MIGRATOR_PASSWORD
        if [ -z "${STEP1_MIGRATOR_PASSWORD:-}" ]; then
            log_error "STEP1_MIGRATOR_PASSWORD 未设置"
            missing_passwords+=("STEP1_MIGRATOR_PASSWORD")
            has_error=1
        else
            log_info "STEP1_MIGRATOR_PASSWORD 已设置 ✓"
        fi
        
        # 检查 STEP1_SVC_PASSWORD
        if [ -z "${STEP1_SVC_PASSWORD:-}" ]; then
            log_error "STEP1_SVC_PASSWORD 未设置"
            missing_passwords+=("STEP1_SVC_PASSWORD")
            has_error=1
        else
            log_info "STEP1_SVC_PASSWORD 已设置 ✓"
        fi
        
        # 检查 OPENMEMORY_MIGRATOR_PASSWORD
        if [ -z "${OPENMEMORY_MIGRATOR_PASSWORD:-}" ]; then
            log_error "OPENMEMORY_MIGRATOR_PASSWORD 未设置"
            missing_passwords+=("OPENMEMORY_MIGRATOR_PASSWORD")
            has_error=1
        else
            log_info "OPENMEMORY_MIGRATOR_PASSWORD 已设置 ✓"
        fi
        
        # 检查 OPENMEMORY_SVC_PASSWORD
        if [ -z "${OPENMEMORY_SVC_PASSWORD:-}" ]; then
            log_error "OPENMEMORY_SVC_PASSWORD 未设置"
            missing_passwords+=("OPENMEMORY_SVC_PASSWORD")
            has_error=1
        else
            log_info "OPENMEMORY_SVC_PASSWORD 已设置 ✓"
        fi
        
        # 如有缺失，输出示例导出命令
        if [ ${#missing_passwords[@]} -gt 0 ]; then
            echo ""
            echo "解决方案："
            echo "  请在 shell 中设置以下环境变量（建议使用强密码）："
            echo ""
            for var in "${missing_passwords[@]}"; do
                echo "  export ${var}='your_secure_password_here'"
            done
            echo ""
            echo "  或在 .env 文件中添加（注意：.env 文件不应提交到版本控制）："
            echo ""
            for var in "${missing_passwords[@]}"; do
                echo "  ${var}=your_secure_password_here"
            done
            echo ""
        fi
    else
        # 非统一栈模式，跳过密码检查
        log_info "非统一栈模式，跳过服务账号密码检查"
        log_info "  提示: 设置 OM_METADATA_BACKEND=postgres 启用统一栈模式"
    fi
    
    # ========================================================================
    # Step3 配置冲突检测
    # ========================================================================
    echo ""
    log_info "检查 Step3 配置..."
    
    local step3_has_conflict=0
    
    # --- 辅助函数：检测 canonical vs legacy 冲突 ---
    # 参数: canonical_name, legacy_name, canonical_value, legacy_value
    # 返回: 0=无冲突, 1=有冲突
    check_env_conflict() {
        local canonical_name="$1"
        local legacy_name="$2"
        local canonical_val="$3"
        local legacy_val="$4"
        
        # 两者都未设置：无冲突
        if [ -z "$canonical_val" ] && [ -z "$legacy_val" ]; then
            return 0
        fi
        
        # 只设置了 legacy：输出废弃警告
        if [ -z "$canonical_val" ] && [ -n "$legacy_val" ]; then
            log_warn "${legacy_name} 已废弃，请迁移到 ${canonical_name}"
            return 0
        fi
        
        # 只设置了 canonical：无冲突
        if [ -n "$canonical_val" ] && [ -z "$legacy_val" ]; then
            return 0
        fi
        
        # 两者都设置：检查值是否一致
        if [ "$canonical_val" != "$legacy_val" ]; then
            if [ $ci_mode -eq 1 ]; then
                log_error "环境变量冲突: ${canonical_name}='${canonical_val}' vs ${legacy_name}='${legacy_val}'"
                log_error "  CI 模式下冲突不被允许，请只设置 ${canonical_name}"
            else
                log_warn "环境变量冲突: ${canonical_name}='${canonical_val}' vs ${legacy_name}='${legacy_val}'"
                log_warn "  将使用 ${canonical_name}='${canonical_val}'（建议移除 ${legacy_name}）"
            fi
            return 1
        else
            # 值一致但同时设置：提示移除 legacy
            log_warn "${legacy_name} 已废弃且与 ${canonical_name} 重复设置，建议移除 ${legacy_name}"
            return 0
        fi
    }
    
    # --- 1. STEP3_PG_SCHEMA vs STEP3_SCHEMA ---
    # 使用 || true 避免 set -e 导致的提前退出
    if ! check_env_conflict "STEP3_PG_SCHEMA" "STEP3_SCHEMA" "${STEP3_PG_SCHEMA:-}" "${STEP3_SCHEMA:-}"; then
        step3_has_conflict=1
        if [ $ci_mode -eq 1 ]; then
            has_error=1
        else
            has_warn=1
        fi
    fi
    
    # --- 2. STEP3_PG_TABLE vs STEP3_TABLE ---
    if ! check_env_conflict "STEP3_PG_TABLE" "STEP3_TABLE" "${STEP3_PG_TABLE:-}" "${STEP3_TABLE:-}"; then
        step3_has_conflict=1
        if [ $ci_mode -eq 1 ]; then
            has_error=1
        else
            has_warn=1
        fi
    fi
    
    # --- 3. STEP3_PGVECTOR_AUTO_INIT vs STEP3_AUTO_INIT ---
    if ! check_env_conflict "STEP3_PGVECTOR_AUTO_INIT" "STEP3_AUTO_INIT" "${STEP3_PGVECTOR_AUTO_INIT:-}" "${STEP3_AUTO_INIT:-}"; then
        step3_has_conflict=1
        if [ $ci_mode -eq 1 ]; then
            has_error=1
        else
            has_warn=1
        fi
    fi
    
    # --- 4. STEP3_PGVECTOR_COLLECTION_STRATEGY 值校验 ---
    local strategy_val="${STEP3_PGVECTOR_COLLECTION_STRATEGY:-}"
    local final_strategy=""
    local supported_strategies="per_table single_table routing"
    local legacy_strategies="per-collection per-table"
    
    if [ -n "$strategy_val" ]; then
        # 检查是否为 legacy 值（带连字符）
        case "$strategy_val" in
            per-collection)
                log_warn "STEP3_PGVECTOR_COLLECTION_STRATEGY='per-collection' 是 legacy 值"
                log_warn "  将映射为 'per_table'（建议直接使用 'per_table'）"
                final_strategy="per_table"
                has_warn=1
                ;;
            per-table)
                log_warn "STEP3_PGVECTOR_COLLECTION_STRATEGY='per-table' 是 legacy 值"
                log_warn "  将映射为 'per_table'（建议直接使用 'per_table'）"
                final_strategy="per_table"
                has_warn=1
                ;;
            per_table|single_table|routing)
                final_strategy="$strategy_val"
                ;;
            *)
                log_error "STEP3_PGVECTOR_COLLECTION_STRATEGY='${strategy_val}' 不是已支持的值"
                log_error "  支持的值: per_table, single_table, routing"
                final_strategy="${strategy_val} [无效]"
                has_error=1
                ;;
        esac
    else
        final_strategy="per_table"  # 默认值
    fi
    
    # --- 计算最终生效值（用于预检摘要）---
    # STEP3_PG_SCHEMA: canonical 优先，legacy 作为 fallback
    local final_schema="${STEP3_PG_SCHEMA:-${STEP3_SCHEMA:-step3}}"
    # STEP3_PG_TABLE: canonical 优先，legacy 作为 fallback
    local final_table="${STEP3_PG_TABLE:-${STEP3_TABLE:-chunks}}"
    # STEP3_PGVECTOR_AUTO_INIT: canonical 优先，legacy 作为 fallback
    local final_auto_init="${STEP3_PGVECTOR_AUTO_INIT:-${STEP3_AUTO_INIT:-1}}"
    
    # --- 输出 Step3 预检摘要 ---
    echo ""
    echo "----------------------------------------"
    echo "Step3 配置预检摘要"
    echo "----------------------------------------"
    echo "  STEP3_PG_SCHEMA                       = ${final_schema}"
    echo "  STEP3_PG_TABLE                        = ${final_table}"
    echo "  STEP3_PGVECTOR_COLLECTION_STRATEGY    = ${final_strategy}"
    echo "  STEP3_PGVECTOR_AUTO_INIT              = ${final_auto_init}"
    if [ $step3_has_conflict -eq 1 ]; then
        if [ $ci_mode -eq 1 ]; then
            echo "  [!] 检测到配置冲突（CI 模式：失败退出）"
        else
            echo "  [!] 检测到配置冲突（本地模式：警告）"
        fi
    else
        echo "  [✓] 无配置冲突"
    fi
    echo "----------------------------------------"
    
    # 检查数据库连接（如果 psql 可用）
    echo ""
    if command -v psql &> /dev/null; then
        if PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1" &> /dev/null; then
            log_info "数据库连接正常 ✓"
        else
            log_warn "无法连接到数据库（可能尚未启动）"
        fi
    else
        log_warn "psql 命令不可用，跳过数据库连接检查"
    fi
    
    echo ""
    if [ $has_error -eq 0 ]; then
        log_info "预检通过！"
        return 0
    else
        log_error "预检失败！"
        return 1
    fi
}

# ============================================================================
# 备份函数
# ============================================================================
do_backup() {
    local backup_type="schemas"  # schemas | schema | full
    local target_schema=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --schema)
                backup_type="schema"
                target_schema="$2"
                shift 2
                ;;
            --full)
                backup_type="full"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    # 创建备份目录
    mkdir -p "$BACKUP_DIR"
    
    # 生成备份文件名
    local timestamp=$(date +%Y%m%d_%H%M%S)
    local backup_file=""
    
    case $backup_type in
        full)
            backup_file="${BACKUP_DIR}/${POSTGRES_DB}_full_${timestamp}.sql"
            log_info "执行全库备份: $backup_file"
            
            PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
                -h "$POSTGRES_HOST" \
                -p "$POSTGRES_PORT" \
                -U "$POSTGRES_USER" \
                -d "$POSTGRES_DB" \
                --format=plain \
                --no-owner \
                --no-acl \
                > "$backup_file"
            ;;
            
        schema)
            # 解析 schema 简写
            case $target_schema in
                om|openmemory)
                    target_schema="$OM_SCHEMA"
                    ;;
            esac
            
            backup_file="${BACKUP_DIR}/${POSTGRES_DB}_${target_schema}_${timestamp}.sql"
            log_info "备份 schema $target_schema: $backup_file"
            
            PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
                -h "$POSTGRES_HOST" \
                -p "$POSTGRES_PORT" \
                -U "$POSTGRES_USER" \
                -d "$POSTGRES_DB" \
                --schema="$target_schema" \
                --format=plain \
                --no-owner \
                --no-acl \
                > "$backup_file"
            ;;
            
        schemas)
            # 备份所有 Engram schema
            backup_file="${BACKUP_DIR}/${POSTGRES_DB}_engram_${timestamp}.sql"
            log_info "备份所有 Engram schema: $backup_file"
            
            local schema_args=""
            for schema in $STEP1_SCHEMAS; do
                schema_args="$schema_args --schema=$schema"
            done
            schema_args="$schema_args --schema=$OM_SCHEMA"
            
            PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
                -h "$POSTGRES_HOST" \
                -p "$POSTGRES_PORT" \
                -U "$POSTGRES_USER" \
                -d "$POSTGRES_DB" \
                $schema_args \
                --format=plain \
                --no-owner \
                --no-acl \
                > "$backup_file"
            ;;
    esac
    
    local size=$(du -h "$backup_file" | cut -f1)
    log_info "备份完成: $backup_file ($size)"
    
    echo ""
    echo "恢复命令:"
    echo "  ./scripts/db_ops.sh restore $backup_file"
    echo ""
    echo "或直接使用 psql:"
    echo "  PGPASSWORD=\$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB < $backup_file"
}

# ============================================================================
# 恢复函数
# ============================================================================
do_restore() {
    local backup_file=""
    local non_interactive=0
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --yes|--non-interactive|-y)
                non_interactive=1
                shift
                ;;
            -*)
                log_error "未知参数: $1"
                return 1
                ;;
            *)
                if [ -z "$backup_file" ]; then
                    backup_file="$1"
                else
                    log_error "多余的参数: $1"
                    return 1
                fi
                shift
                ;;
        esac
    done
    
    if [ -z "$backup_file" ]; then
        log_error "请指定备份文件路径"
        echo "用法: $0 restore <backup_file.sql> [--yes|--non-interactive]"
        return 1
    fi
    
    if [ ! -f "$backup_file" ]; then
        log_error "备份文件不存在: $backup_file"
        return 1
    fi
    
    log_info "恢复备份: $backup_file"
    
    if [ $non_interactive -eq 0 ]; then
        log_warn "这将覆盖现有数据，是否继续? (y/N)"
        read -r confirm
        
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            log_info "操作已取消"
            return 0
        fi
    else
        log_warn "非交互模式：跳过确认提示"
    fi
    
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        < "$backup_file"
    
    log_info "恢复完成"
}

# ============================================================================
# 清理函数（危险操作）
# ============================================================================
do_cleanup() {
    local target_schema=""
    local force=0
    local non_interactive=0
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --schema)
                target_schema="$2"
                shift 2
                ;;
            --force)
                force=1
                shift
                ;;
            --yes|--non-interactive|-y)
                non_interactive=1
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    if [ -z "$target_schema" ]; then
        log_error "请指定要清理的 schema"
        echo "用法: $0 cleanup --schema <schema_name> [--force] [--yes|--non-interactive]"
        return 1
    fi
    
    # 解析 schema 简写
    case $target_schema in
        om|openmemory)
            target_schema="$OM_SCHEMA"
            ;;
    esac
    
    # 禁止清理 public schema
    if [ "$target_schema" = "public" ]; then
        log_error "禁止清理 public schema！"
        return 1
    fi
    
    log_warn "========================================"
    log_warn "危险操作：DROP SCHEMA $target_schema CASCADE"
    log_warn "========================================"
    log_warn "这将永久删除以下内容："
    log_warn "  - Schema '$target_schema' 中的所有表"
    log_warn "  - 所有相关的索引、触发器、函数"
    log_warn "  - 所有数据（不可恢复）"
    log_warn ""
    log_warn "此操作仅适用于："
    log_warn "  - 空库/测试环境"
    log_warn "  - 首次部署失败后的清理"
    log_warn ""
    
    if [ $force -eq 0 ] && [ $non_interactive -eq 0 ]; then
        log_warn "请输入 schema 名称确认删除: $target_schema"
        read -r confirm
        
        if [ "$confirm" != "$target_schema" ]; then
            log_info "操作已取消"
            return 0
        fi
    elif [ $non_interactive -eq 1 ]; then
        log_warn "非交互模式：跳过确认提示"
    fi
    
    log_info "执行清理..."
    
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        -c "DROP SCHEMA IF EXISTS \"$target_schema\" CASCADE;"
    
    log_info "Schema '$target_schema' 已清理"
}

# ============================================================================
# 权限验证函数
# ============================================================================
do_verify() {
    local quiet=0
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --quiet|-q)
                quiet=1
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    echo "========================================"
    echo "Engram 权限验证"
    echo "========================================"
    
    local has_error=0
    local verify_sql="${SQL_DIR}/99_verify_permissions.sql"
    
    # 1. 执行 99_verify_permissions.sql
    log_info "执行权限验证脚本..."
    
    if [ ! -f "$verify_sql" ]; then
        log_error "验证脚本不存在: $verify_sql"
        return 1
    fi
    
    # 设置 om.target_schema 并执行验证
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        -v target_schema="'$OM_SCHEMA'" \
        -f "$verify_sql"
    
    local psql_exit=$?
    if [ $psql_exit -ne 0 ]; then
        log_error "权限验证脚本执行失败"
        has_error=1
    fi
    
    echo ""
    
    # 2. 关键表存在性检查
    log_info "检查关键表存在性..."
    
    # 检查 governance.security_events 表（用于安全审计）
    local check_sql="
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'governance' AND table_name = 'security_events'
        ) AS governance_security_events,
        EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'logbook' AND table_name = 'items'
        ) AS logbook_items,
        EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'identity' AND table_name = 'actors'
        ) AS identity_actors,
        EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = '$OM_SCHEMA' AND table_name = 'memories'
        ) AS om_memories;
    "
    
    local result
    result=$(PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        -t -A \
        -c "$check_sql" 2>/dev/null)
    
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        # 解析结果 (格式: t|t|t|t 或 f|t|f|t)
        IFS='|' read -r gov_sec logbook_items id_act om_mem <<< "$result"
        
        if [ "$gov_sec" = "t" ]; then
            log_info "governance.security_events 表存在 ✓"
        else
            log_warn "governance.security_events 表不存在（可能尚未迁移）"
        fi
        
        if [ "$logbook_items" = "t" ]; then
            log_info "logbook.items 表存在 ✓"
        else
            log_warn "logbook.items 表不存在（可能尚未迁移）"
        fi
        
        if [ "$id_act" = "t" ]; then
            log_info "identity.actors 表存在 ✓"
        else
            log_warn "identity.actors 表不存在（可能尚未迁移）"
        fi
        
        if [ "$om_mem" = "t" ]; then
            log_info "${OM_SCHEMA}.memories 表存在 ✓"
        else
            log_warn "${OM_SCHEMA}.memories 表不存在（可能尚未迁移）"
        fi
    else
        log_warn "无法检查表存在性（可能数据库未初始化）"
    fi
    
    echo ""
    if [ $has_error -eq 0 ]; then
        log_info "权限验证完成！请检查上方输出是否有 FAIL 或 WARNING。"
        return 0
    else
        log_error "权限验证发现问题！"
        return 1
    fi
}

# ============================================================================
# Bootstrap 函数
# ============================================================================
do_bootstrap() {
    local precheck_only=0
    local extra_args=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --precheck-only)
                precheck_only=1
                extra_args="$extra_args --precheck-only"
                shift
                ;;
            --quiet|-q)
                extra_args="$extra_args --quiet"
                shift
                ;;
            --pretty)
                extra_args="$extra_args --pretty"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local bootstrap_script="${STEP1_DIR}/scripts/db_bootstrap.py"
    
    if [ ! -f "$bootstrap_script" ]; then
        log_error "Bootstrap 脚本不存在: $bootstrap_script"
        return 1
    fi
    
    log_info "执行数据库 Bootstrap..."
    
    # 调用 Python 脚本（敏感参数通过环境变量传递）
    python3 "$bootstrap_script" $extra_args
    
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        log_info "Bootstrap 完成"
    else
        log_error "Bootstrap 失败（退出码: $exit_code）"
    fi
    
    return $exit_code
}

# ============================================================================
# 升级前备份函数（强制要求）
# ============================================================================
do_pre_upgrade() {
    local backup_type="om"  # 默认仅备份 OpenMemory schema
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --full)
                backup_type="full"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    echo "========================================"
    echo "升级前强制备份"
    echo "========================================"
    echo ""
    log_warn "升级前备份是强制要求！"
    log_warn "某些 OpenMemory 迁移是不可逆的，回滚唯一策略是恢复备份。"
    echo ""
    
    # 显示回滚路径
    echo "回滚路径（如需回滚）:"
    echo "  1. 回退 OpenMemory.upstream.lock.json 到之前的 commit"
    echo "  2. 重新构建镜像: make openmemory-build"
    echo "  3. 恢复备份: $0 restore <backup_file.sql>"
    echo ""
    
    # 执行备份
    if [ "$backup_type" = "full" ]; then
        log_info "执行全库备份（生产环境推荐）..."
        do_backup --full
    else
        log_info "执行 OpenMemory schema 备份..."
        do_backup --schema om
    fi
    
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        echo ""
        log_info "升级前备份完成！"
        echo ""
        echo "不可逆迁移说明:"
        echo "  - 删除列（DROP COLUMN）"
        echo "  - 重命名表/列（RENAME）"
        echo "  - 修改数据类型（ALTER TYPE）"
        echo "  - 删除索引后重建"
        echo ""
        echo "对于上述不可逆迁移，回滚=还原备份 是唯一策略。"
    else
        log_error "升级前备份失败！请勿继续升级。"
    fi
    
    return $exit_code
}

# ============================================================================
# 密码轮换函数
# ============================================================================
do_rotate() {
    local extra_args=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --quiet|-q)
                extra_args="$extra_args --quiet"
                shift
                ;;
            --pretty)
                extra_args="$extra_args --pretty"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local bootstrap_script="${STEP1_DIR}/scripts/db_bootstrap.py"
    
    if [ ! -f "$bootstrap_script" ]; then
        log_error "Bootstrap 脚本不存在: $bootstrap_script"
        return 1
    fi
    
    log_info "执行密码轮换..."
    log_warn "请确保已更新以下环境变量中的新密码："
    log_warn "  STEP1_MIGRATOR_PASSWORD"
    log_warn "  STEP1_SVC_PASSWORD"
    log_warn "  OPENMEMORY_MIGRATOR_PASSWORD"
    log_warn "  OPENMEMORY_SVC_PASSWORD"
    echo ""
    
    # 调用 Python 脚本的 rotate 子命令（敏感参数通过环境变量传递）
    python3 "$bootstrap_script" rotate $extra_args
    
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        log_info "密码轮换完成"
    else
        log_error "密码轮换失败（退出码: $exit_code）"
    fi
    
    return $exit_code
}

# ============================================================================
# Step3 索引/检索函数
# ============================================================================

# Step3 依赖检测：确保 engram_step1 已安装
ensure_step3_deps() {
    # 检测 engram_step1 是否可导入
    if ! python -c "import engram_step1" 2>/dev/null; then
        log_warn "engram_step1 模块未安装，尝试自动安装..."
        local step1_scripts="${PROJECT_ROOT}/apps/step1_logbook_postgres/scripts"
        if [ -d "$step1_scripts" ]; then
            pip install -e "$step1_scripts" --quiet 2>/dev/null || {
                log_error "自动安装 engram_step1 失败"
                echo ""
                echo "请手动执行以下命令安装依赖："
                echo "  pip install -e ${step1_scripts}"
                echo ""
                return 1
            }
            log_info "engram_step1 安装成功"
        else
            log_error "找不到 step1_logbook_postgres/scripts 目录"
            echo ""
            echo "请确保项目结构完整，或手动安装 engram_step1 模块"
            return 1
        fi
    fi
    return 0
}

do_step3_index() {
    # 确保依赖已安装
    ensure_step3_deps || return 1
    
    local json_output=0
    local extra_args=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --json)
                json_output=1
                extra_args="$extra_args --json"
                shift
                ;;
            --mode)
                extra_args="$extra_args --mode $2"
                shift 2
                ;;
            --source)
                extra_args="$extra_args --source $2"
                shift 2
                ;;
            --blob-id)
                extra_args="$extra_args --blob-id $2"
                shift 2
                ;;
            --batch-size)
                extra_args="$extra_args --batch-size $2"
                shift 2
                ;;
            --project-key)
                extra_args="$extra_args --project-key $2"
                shift 2
                ;;
            --dry-run)
                extra_args="$extra_args --dry-run"
                shift
                ;;
            --verbose|-v)
                extra_args="$extra_args --verbose"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local step3_dir="${PROJECT_ROOT}/apps/step3_seekdb_rag_hybrid"
    
    if [ $json_output -eq 0 ]; then
        log_info "执行 Step3 索引同步..."
    fi
    
    cd "$step3_dir" && python -m seek_indexer $extra_args
    
    local exit_code=$?
    if [ $json_output -eq 0 ]; then
        if [ $exit_code -eq 0 ]; then
            log_info "Step3 索引同步完成"
        else
            log_error "Step3 索引同步失败（退出码: $exit_code）"
        fi
    fi
    
    return $exit_code
}

do_step3_query() {
    # 确保依赖已安装
    ensure_step3_deps || return 1
    
    local json_output=0
    # 使用数组累加参数，避免 eval 和引号转义问题
    local args=(python -m seek_query)
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --json)
                json_output=1
                args+=(--json)
                shift
                ;;
            --query|-q)
                args+=(--query "$2")
                shift 2
                ;;
            --query-file)
                args+=(--query-file "$2")
                shift 2
                ;;
            --project-key)
                args+=(--project-key "$2")
                shift 2
                ;;
            --source-type)
                args+=(--source-type "$2")
                shift 2
                ;;
            --owner)
                args+=(--owner "$2")
                shift 2
                ;;
            --top-k|-k)
                args+=(--top-k "$2")
                shift 2
                ;;
            --output-format)
                args+=(--output-format "$2")
                shift 2
                ;;
            --verbose|-v)
                args+=(--verbose)
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local step3_dir="${PROJECT_ROOT}/apps/step3_seekdb_rag_hybrid"
    
    if [ $json_output -eq 0 ]; then
        log_info "执行 Step3 证据检索..."
    fi
    
    # 使用数组展开执行命令，无需 eval，正确保留参数中的特殊字符
    cd "$step3_dir" && "${args[@]}"
    
    local exit_code=$?
    if [ $json_output -eq 0 ]; then
        if [ $exit_code -eq 0 ]; then
            log_info "Step3 检索完成"
        else
            log_error "Step3 检索失败（退出码: $exit_code）"
        fi
    fi
    
    return $exit_code
}

do_step3_check() {
    # 确保依赖已安装
    ensure_step3_deps || return 1
    
    local json_output=0
    local extra_args=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --json)
                json_output=1
                extra_args="$extra_args --json"
                shift
                ;;
            --chunking-version)
                extra_args="$extra_args --chunking-version $2"
                shift 2
                ;;
            --project-key)
                extra_args="$extra_args --project-key $2"
                shift 2
                ;;
            --sample-ratio)
                extra_args="$extra_args --sample-ratio $2"
                shift 2
                ;;
            --limit)
                extra_args="$extra_args --limit $2"
                shift 2
                ;;
            --skip-artifacts)
                extra_args="$extra_args --skip-artifacts"
                shift
                ;;
            --skip-sha256)
                extra_args="$extra_args --skip-sha256"
                shift
                ;;
            --verbose|-v)
                extra_args="$extra_args --verbose"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local step3_dir="${PROJECT_ROOT}/apps/step3_seekdb_rag_hybrid"
    
    if [ $json_output -eq 0 ]; then
        log_info "执行 Step3 一致性检查..."
    fi
    
    cd "$step3_dir" && python -m seek_consistency_check $extra_args
    
    local exit_code=$?
    if [ $json_output -eq 0 ]; then
        if [ $exit_code -eq 0 ]; then
            log_info "Step3 一致性检查完成"
        else
            log_error "Step3 一致性检查发现问题（退出码: $exit_code）"
        fi
    fi
    
    return $exit_code
}

do_step3_inspect() {
    # 确保依赖已安装
    ensure_step3_deps || return 1
    
    local json_output=0
    local extra_args=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --json)
                json_output=1
                shift
                ;;
            --pretty)
                extra_args="$extra_args --pretty"
                shift
                ;;
            --schema-pattern)
                extra_args="$extra_args --schema-pattern $2"
                shift 2
                ;;
            --table-pattern)
                extra_args="$extra_args --table-pattern $2"
                shift 2
                ;;
            --base-schema)
                extra_args="$extra_args --base-schema $2"
                shift 2
                ;;
            --base-table)
                extra_args="$extra_args --base-table $2"
                shift 2
                ;;
            --verbose|-v)
                extra_args="$extra_args --verbose"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local step3_dir="${PROJECT_ROOT}/apps/step3_seekdb_rag_hybrid"
    local inspect_script="${step3_dir}/scripts/pgvector_inspect.py"
    
    if [ ! -f "$inspect_script" ]; then
        log_error "检查脚本不存在: $inspect_script"
        return 1
    fi
    
    if [ $json_output -eq 0 ]; then
        log_info "执行 Step3 PGVector 信息架构检查..."
    fi
    
    # 添加 --pretty 如果不是 json 输出模式
    if [ $json_output -eq 0 ] && [[ ! "$extra_args" =~ "--pretty" ]]; then
        extra_args="$extra_args --pretty"
    fi
    
    python "$inspect_script" $extra_args
    
    local exit_code=$?
    if [ $json_output -eq 0 ]; then
        if [ $exit_code -eq 0 ]; then
            log_info "Step3 PGVector 检查完成"
        else
            log_error "Step3 PGVector 检查失败（退出码: $exit_code）"
        fi
    fi
    
    return $exit_code
}

do_step3_migration_drill() {
    # 确保依赖已安装
    ensure_step3_deps || return 1
    
    local dry_run=0
    local auto_confirm=0
    local project_key="${PROJECT_KEY:-default}"
    local collection=""
    local target_shared_table=""
    local strategy="per_table"
    local is_production="${ENGRAM_PRODUCTION:-0}"
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --dry-run)
                dry_run=1
                shift
                ;;
            --yes|-y)
                auto_confirm=1
                shift
                ;;
            --project-key)
                project_key="$2"
                shift 2
                ;;
            --collection)
                collection="$2"
                shift 2
                ;;
            --target-shared-table)
                target_shared_table="$2"
                shift 2
                ;;
            --strategy)
                case "$2" in
                    per_table|single_table|routing)
                        strategy="$2"
                        ;;
                    *)
                        log_error "不支持的策略: $2"
                        log_error "支持的策略: per_table, single_table, routing"
                        return 1
                        ;;
                esac
                shift 2
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    local step3_dir="${PROJECT_ROOT}/apps/step3_seekdb_rag_hybrid"
    local migrate_script="${step3_dir}/scripts/pgvector_collection_migrate.py"
    local inspect_script="${step3_dir}/scripts/pgvector_inspect.py"
    
    echo "========================================"
    echo "Step3 迁移演练流程"
    echo "========================================"
    echo ""
    echo "参数:"
    echo "  --dry-run:            $dry_run"
    echo "  --yes (auto-confirm): $auto_confirm"
    echo "  --project-key:        $project_key"
    echo "  --collection:         ${collection:-<未指定>}"
    echo "  --target-shared-table: ${target_shared_table:-<未指定>}"
    echo "  --strategy:           $strategy"
    echo ""
    
    # ========================================================================
    # Step 1: 配置预检
    # ========================================================================
    log_info "[1/7] 执行配置预检..."
    if ! do_precheck; then
        log_error "预检失败，演练中止"
        return 1
    fi
    echo ""
    
    # ========================================================================
    # Step 2: 检查当前 PGVector 状态（step3-inspect --json）
    # ========================================================================
    log_info "[2/7] 检查当前 PGVector 信息架构..."
    
    if [ ! -f "$inspect_script" ]; then
        log_error "检查脚本不存在: $inspect_script"
        return 1
    fi
    
    local inspect_output
    inspect_output=$(python "$inspect_script" --json 2>&1)
    local inspect_exit=$?
    
    if [ $inspect_exit -ne 0 ]; then
        log_error "PGVector 检查失败"
        echo "$inspect_output"
        return 1
    fi
    
    # 输出 inspect 结果到 stdout（不落盘）
    echo ""
    echo "----------------------------------------"
    echo "PGVector 信息架构检查结果 (JSON):"
    echo "----------------------------------------"
    echo "$inspect_output"
    echo "----------------------------------------"
    echo ""
    
    # ========================================================================
    # Step 3: 备份（生产强制，开发可选）
    # ========================================================================
    log_info "[3/7] 备份检查..."
    
    if [ "$is_production" = "1" ] || [ "${ENGRAM_PRODUCTION:-}" = "true" ]; then
        # 生产环境：强制全库备份
        log_warn "检测到生产环境 (ENGRAM_PRODUCTION=1)，强制执行全库备份"
        if [ $dry_run -eq 1 ]; then
            log_info "[dry-run] 将执行: backup --full"
        else
            if ! do_backup --full; then
                log_error "备份失败，演练中止"
                return 1
            fi
        fi
    else
        # 开发/测试环境：提示可选备份
        if [ $auto_confirm -eq 0 ]; then
            log_warn "是否在迁移前执行全库备份? (推荐生产环境) (y/N)"
            read -r backup_confirm
            if [ "$backup_confirm" = "y" ] || [ "$backup_confirm" = "Y" ]; then
                if [ $dry_run -eq 1 ]; then
                    log_info "[dry-run] 将执行: backup --full"
                else
                    if ! do_backup --full; then
                        log_error "备份失败，演练中止"
                        return 1
                    fi
                fi
            else
                log_info "跳过备份"
            fi
        else
            log_info "非交互模式，跳过备份提示（生产环境请设置 ENGRAM_PRODUCTION=1 强制备份）"
        fi
    fi
    echo ""
    
    # ========================================================================
    # Step 4: 生成迁移计划（dry-run + plan-json）
    # ========================================================================
    log_info "[4/7] 生成迁移计划..."
    
    if [ ! -f "$migrate_script" ]; then
        log_error "迁移脚本不存在: $migrate_script"
        return 1
    fi
    
    # 构建迁移命令参数
    local migrate_mode=""
    local migrate_extra_args=""
    
    case "$strategy" in
        per_table)
            migrate_mode="table-per-collection"
            ;;
        single_table)
            migrate_mode="shared-table"
            if [ -n "$target_shared_table" ]; then
                migrate_extra_args="$migrate_extra_args --target-table $target_shared_table"
            fi
            ;;
        routing)
            # routing 策略暂时映射到 shared-table，等待后续支持
            migrate_mode="shared-table"
            log_warn "routing 策略当前映射到 shared-table 模式"
            if [ -n "$target_shared_table" ]; then
                migrate_extra_args="$migrate_extra_args --target-table $target_shared_table"
            fi
            ;;
    esac
    
    # 添加 collection 过滤（如果指定）
    if [ -n "$collection" ]; then
        migrate_extra_args="$migrate_extra_args --table-allowlist $collection"
    fi
    
    # 始终先运行 dry-run 生成计划
    log_info "迁移模式: $migrate_mode"
    log_info "执行 dry-run 生成迁移计划..."
    
    local plan_output
    plan_output=$(cd "$step3_dir" && python "$migrate_script" $migrate_mode --dry-run --json $migrate_extra_args 2>&1)
    local plan_exit=$?
    
    echo ""
    echo "----------------------------------------"
    echo "迁移计划 (dry-run):"
    echo "----------------------------------------"
    echo "$plan_output"
    echo "----------------------------------------"
    echo ""
    
    if [ $plan_exit -ne 0 ]; then
        log_error "迁移计划生成失败"
        return 1
    fi
    
    # ========================================================================
    # Step 5: 可选执行实际迁移（需要 --yes）
    # ========================================================================
    log_info "[5/7] 迁移执行..."
    
    if [ $dry_run -eq 1 ]; then
        log_info "[dry-run] 跳过实际迁移执行"
    else
        if [ $auto_confirm -eq 0 ]; then
            log_warn "是否执行实际迁移? 此操作将修改数据库 (y/N)"
            read -r migrate_confirm
            if [ "$migrate_confirm" != "y" ] && [ "$migrate_confirm" != "Y" ]; then
                log_info "取消迁移执行"
                echo ""
                echo "提示: 使用 --yes 参数跳过确认提示"
                return 0
            fi
        fi
        
        log_info "执行实际迁移..."
        local migrate_result
        migrate_result=$(cd "$step3_dir" && python "$migrate_script" $migrate_mode --json $migrate_extra_args 2>&1)
        local migrate_exit=$?
        
        echo ""
        echo "----------------------------------------"
        echo "迁移执行结果:"
        echo "----------------------------------------"
        echo "$migrate_result"
        echo "----------------------------------------"
        echo ""
        
        if [ $migrate_exit -ne 0 ]; then
            log_error "迁移执行失败"
            echo ""
            echo "========================================"
            echo "回滚命令:"
            echo "========================================"
            echo "  # 如有备份，使用以下命令恢复:"
            echo "  ./scripts/db_ops.sh restore <backup_file.sql>"
            echo ""
            return 1
        fi
        
        log_info "迁移执行完成"
    fi
    echo ""
    
    # ========================================================================
    # Step 6: 抽样查询门禁验证
    # ========================================================================
    log_info "[6/7] 抽样查询门禁验证..."
    
    if [ $dry_run -eq 1 ]; then
        log_info "[dry-run] 跳过查询门禁验证"
    else
        # 执行一组抽样查询验证迁移后的数据可访问性
        local sample_queries=(
            "测试查询"
            "迁移验证"
        )
        
        local query_passed=0
        local query_failed=0
        
        for query in "${sample_queries[@]}"; do
            log_info "执行抽样查询: '$query'"
            
            # 构建查询参数
            local query_args=(python -m seek_query --query "$query" --top-k 3 --json)
            if [ -n "$project_key" ]; then
                query_args+=(--project-key "$project_key")
            fi
            
            local query_result
            query_result=$(cd "$step3_dir" && "${query_args[@]}" 2>&1)
            local query_exit=$?
            
            if [ $query_exit -eq 0 ]; then
                ((query_passed++))
                log_info "  查询成功 ✓"
            else
                ((query_failed++))
                log_warn "  查询失败 (可能是正常的空结果)"
            fi
        done
        
        echo ""
        log_info "抽样查询结果: 通过 $query_passed / 总计 $((query_passed + query_failed))"
        
        if [ $query_failed -gt 0 ]; then
            log_warn "部分抽样查询失败，建议检查迁移结果"
        fi
    fi
    echo ""
    
    # ========================================================================
    # Step 7: 输出下一步命令
    # ========================================================================
    log_info "[7/7] 下一步操作指引"
    echo ""
    echo "========================================"
    echo "下一步操作命令"
    echo "========================================"
    echo ""
    
    if [ $dry_run -eq 1 ]; then
        echo "# 实际执行迁移（移除 --dry-run）:"
        echo "./scripts/db_ops.sh step3-migration-drill --strategy $strategy --yes"
        if [ -n "$collection" ]; then
            echo "  --collection $collection"
        fi
        if [ -n "$target_shared_table" ]; then
            echo "  --target-shared-table $target_shared_table"
        fi
        echo ""
    fi
    
    echo "# 验证迁移结果:"
    echo "./scripts/db_ops.sh step3-inspect --pretty"
    echo ""
    
    echo "# 运行一致性检查:"
    echo "./scripts/db_ops.sh step3-check --json"
    echo ""
    
    echo "# 执行查询测试:"
    echo "./scripts/db_ops.sh step3-query --query '你的测试查询' --json"
    echo ""
    
    echo "# 如需回滚 (从备份恢复):"
    echo "./scripts/db_ops.sh restore <backup_file.sql> --yes"
    echo ""
    
    echo "# 切换到新策略后更新环境变量:"
    echo "export STEP3_PGVECTOR_COLLECTION_STRATEGY=$strategy"
    echo ""
    echo "========================================"
    
    log_info "Step3 迁移演练完成"
    return 0
}

do_step3_migrate() {
    # 确保依赖已安装
    ensure_step3_deps || return 1
    
    local migrate_mode=""
    local json_output=0
    local extra_args=""
    
    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            shared-table|table-per-collection|consolidate-to-shared-table)
                migrate_mode="$1"
                shift
                ;;
            --json)
                json_output=1
                extra_args="$extra_args --json"
                shift
                ;;
            --dry-run)
                extra_args="$extra_args --dry-run"
                shift
                ;;
            --batch-size)
                extra_args="$extra_args --batch-size $2"
                shift 2
                ;;
            --default-collection-id)
                extra_args="$extra_args --default-collection-id $2"
                shift 2
                ;;
            --verbose|-v)
                extra_args="$extra_args --verbose"
                shift
                ;;
            # consolidate-to-shared-table 专用参数
            --target-table)
                extra_args="$extra_args --target-table $2"
                shift 2
                ;;
            --conflict-strategy)
                extra_args="$extra_args --conflict-strategy $2"
                shift 2
                ;;
            --table-pattern)
                extra_args="$extra_args --table-pattern $2"
                shift 2
                ;;
            --table-regex)
                extra_args="$extra_args --table-regex $2"
                shift 2
                ;;
            --table-allowlist)
                extra_args="$extra_args --table-allowlist $2"
                shift 2
                ;;
            --exclude-tables)
                extra_args="$extra_args --exclude-tables $2"
                shift 2
                ;;
            --collection-mapping-file)
                extra_args="$extra_args --collection-mapping-file $2"
                shift 2
                ;;
            --no-verify-counts)
                extra_args="$extra_args --no-verify-counts"
                shift
                ;;
            --no-sample-verify)
                extra_args="$extra_args --no-sample-verify"
                shift
                ;;
            --sample-size)
                extra_args="$extra_args --sample-size $2"
                shift 2
                ;;
            *)
                log_error "未知参数: $1"
                return 1
                ;;
        esac
    done
    
    if [ -z "$migrate_mode" ]; then
        log_error "请指定迁移模式: shared-table, table-per-collection 或 consolidate-to-shared-table"
        echo ""
        echo "用法:"
        echo "  $0 step3-migrate shared-table [--dry-run] [--batch-size N]"
        echo "  $0 step3-migrate table-per-collection [--dry-run] [--batch-size N]"
        echo "  $0 step3-migrate consolidate-to-shared-table [--dry-run] [--target-table TABLE] ..."
        echo ""
        echo "模式说明:"
        echo "  shared-table:               单表方案 - 添加 collection_id 列并回填"
        echo "  table-per-collection:       按表方案 - 按 collection 分表存储"
        echo "  consolidate-to-shared-table: 合并迁移 - 将多个分表合并到单一共享表"
        echo ""
        echo "通用选项:"
        echo "  --dry-run              只显示将执行的操作，不实际修改数据库"
        echo "  --batch-size N         每批处理的记录数 (默认 1000)"
        echo "  --default-collection-id ID  默认 collection_id (默认 default:v1:nomodel)"
        echo "  --verbose, -v          显示详细进度"
        echo "  --json                 以 JSON 格式输出结果"
        echo ""
        echo "consolidate-to-shared-table 专用选项:"
        echo "  --target-table TABLE        目标表名 (默认 chunks)"
        echo "  --conflict-strategy STRATEGY  冲突策略: skip, overwrite, fail (默认 skip)"
        echo "  --table-pattern PATTERN     表名模式 (SQL LIKE 语法, 如 'chunks_%')"
        echo "  --table-regex REGEX         表名正则匹配"
        echo "  --table-allowlist TABLES    允许的表名列表 (逗号分隔)"
        echo "  --exclude-tables TABLES     排除的表名列表 (逗号分隔)"
        echo "  --collection-mapping-file FILE  collection_id 映射文件 (JSON 或 YAML)"
        echo "  --no-verify-counts          跳过记录数验证"
        echo "  --no-sample-verify          跳过采样数据验证"
        echo "  --sample-size N             采样验证的记录数 (默认 100)"
        return 1
    fi
    
    local step3_dir="${PROJECT_ROOT}/apps/step3_seekdb_rag_hybrid"
    local migrate_script="${step3_dir}/scripts/pgvector_collection_migrate.py"
    
    if [ ! -f "$migrate_script" ]; then
        log_error "迁移脚本不存在: $migrate_script"
        return 1
    fi
    
    if [ $json_output -eq 0 ]; then
        log_info "执行 Step3 Collection 迁移..."
        log_info "  模式: $migrate_mode"
    fi
    
    cd "$step3_dir" && python "$migrate_script" $migrate_mode $extra_args
    
    local exit_code=$?
    if [ $json_output -eq 0 ]; then
        if [ $exit_code -eq 0 ]; then
            log_info "Step3 Collection 迁移完成"
        else
            log_error "Step3 Collection 迁移失败（退出码: $exit_code）"
        fi
    fi
    
    return $exit_code
}

# ============================================================================
# 帮助信息
# ============================================================================
show_help() {
    echo "Engram 数据库运维脚本"
    echo ""
    echo "用法: $0 <command> [options]"
    echo ""
    echo "命令:"
    echo "  precheck              配置预检（验证环境变量）"
    echo "  backup                备份所有 Engram schema"
    echo "  backup --schema <s>   仅备份指定 schema（om=openmemory）"
    echo "  backup --full         全库备份"
    echo "  restore <file>        恢复备份文件"
    echo "  restore <file> --yes  非交互式恢复（CI 场景）"
    echo "  cleanup --schema <s>  清理指定 schema（危险操作）"
    echo "  cleanup --schema <s> --yes  非交互式清理（CI 场景）"
    echo "  verify                权限验证 + 关键表存在性检查"
    echo "  bootstrap             初始化角色、权限和安全配置"
    echo "  bootstrap --precheck-only  仅预检，不执行实际操作"
    echo "  rotate                轮换数据库密码"
    echo ""
    echo "升级与回滚命令:"
    echo "  pre-upgrade           升级前备份（开发环境，仅备份 OM schema）"
    echo "  pre-upgrade --full    升级前备份（生产环境，全库备份）"
    echo ""
    echo "Step3 索引/检索命令（支持 --json 输出）:"
    echo "  step3-index           索引同步（增量/全量/单条）"
    echo "  step3-query           证据检索"
    echo "  step3-check           一致性校验"
    echo "  step3-migrate         Collection 迁移（shared-table/table-per-collection/consolidate-to-shared-table）"
    echo "  step3-inspect         PGVector 信息架构检查（表清单/大小/维度/索引）"
    echo "  step3-migration-drill 迁移演练流程（预检→检查→备份→计划→执行→验证）"
    echo ""
    echo "环境变量（敏感参数只从环境变量读取，禁止命令行传密码）:"
    echo "  POSTGRES_HOST         PostgreSQL 主机（默认 localhost）"
    echo "  POSTGRES_PORT         PostgreSQL 端口（默认 5432）"
    echo "  POSTGRES_DB           数据库名（默认 engram）"
    echo "  POSTGRES_USER         用户名（默认 postgres）"
    echo "  POSTGRES_PASSWORD     密码（敏感）"
    echo "  PROJECT_KEY           项目标识（默认 default，影响 Step1 表前缀）"
    echo "  OM_PG_SCHEMA          OpenMemory schema 名（默认 openmemory）"
    echo "  BACKUP_DIR            备份目录（默认 ./backups）"
    echo ""
    echo "Bootstrap 专用环境变量:"
    echo "  ENGRAM_PG_ADMIN_DSN           管理员 DSN（敏感）"
    echo "  STEP1_MIGRATOR_PASSWORD       step1_migrator 密码（敏感）"
    echo "  STEP1_SVC_PASSWORD            step1_svc 密码（敏感）"
    echo "  OPENMEMORY_MIGRATOR_PASSWORD  openmemory_migrator_login 密码（敏感）"
    echo "  OPENMEMORY_SVC_PASSWORD       openmemory_svc 密码（敏感）"
    echo ""
    echo "示例:"
    echo "  # 配置预检"
    echo "  $0 precheck"
    echo ""
    echo "  # 备份 openmemory schema"
    echo "  $0 backup --schema om"
    echo ""
    echo "  # 全库备份"
    echo "  $0 backup --full"
    echo ""
    echo "  # 非交互式恢复（CI 场景）"
    echo "  $0 restore backup.sql --yes"
    echo ""
    echo "  # 清理 openmemory schema（首次部署失败时）"
    echo "  $0 cleanup --schema om"
    echo ""
    echo "  # 非交互式清理（CI 场景）"
    echo "  $0 cleanup --schema om --yes"
    echo ""
    echo "  # 权限验证"
    echo "  $0 verify"
    echo ""
    echo "  # 初始化数据库（首次部署）"
    echo "  $0 bootstrap"
    echo ""
    echo "  # 密码轮换"
    echo "  $0 rotate"
    echo ""
    echo "  # 升级前备份（开发环境）"
    echo "  $0 pre-upgrade"
    echo ""
    echo "  # 升级前备份（生产环境，全库）"
    echo "  $0 pre-upgrade --full"
    echo ""
    echo "  # Step3 索引同步（增量）"
    echo "  $0 step3-index --json"
    echo ""
    echo "  # Step3 索引同步（全量）"
    echo "  $0 step3-index --mode full --json"
    echo ""
    echo "  # Step3 证据检索"
    echo "  $0 step3-query --query '修复 XSS 漏洞' --json"
    echo ""
    echo "  # Step3 一致性校验"
    echo "  $0 step3-check --json"
    echo ""
    echo "  # Step3 Collection 迁移（单表方案 - dry-run）"
    echo "  $0 step3-migrate shared-table --dry-run"
    echo ""
    echo "  # Step3 Collection 迁移（单表方案 - 实际执行）"
    echo "  $0 step3-migrate shared-table --batch-size 500"
    echo ""
    echo "  # Step3 Collection 迁移（按表方案 - dry-run）"
    echo "  $0 step3-migrate table-per-collection --dry-run"
    echo ""
    echo "  # Step3 Collection 合并迁移（分表合并到共享表 - dry-run）"
    echo "  $0 step3-migrate consolidate-to-shared-table --dry-run --table-pattern 'chunks_%'"
    echo ""
    echo "  # Step3 Collection 合并迁移（指定目标表和冲突策略）"
    echo "  $0 step3-migrate consolidate-to-shared-table --target-table chunks --conflict-strategy skip"
    echo ""
    echo "  # Step3 Collection 合并迁移（使用映射文件和排除表）"
    echo "  $0 step3-migrate consolidate-to-shared-table --collection-mapping-file mapping.json --exclude-tables 'chunks_temp,chunks_old'"
    echo ""
    echo "  # Step3 PGVector 信息架构检查"
    echo "  $0 step3-inspect --pretty"
    echo ""
    echo "  # Step3 PGVector 检查（JSON 输出，用于脚本）"
    echo "  $0 step3-inspect --json"
    echo ""
    echo "  # Step3 迁移演练（dry-run 模式，仅生成计划）"
    echo "  $0 step3-migration-drill --dry-run --strategy single_table"
    echo ""
    echo "  # Step3 迁移演练（实际执行，非交互模式）"
    echo "  $0 step3-migration-drill --strategy single_table --yes"
    echo ""
    echo "  # Step3 迁移演练（指定 collection 和目标表）"
    echo "  $0 step3-migration-drill --strategy single_table --collection my_collection --target-shared-table chunks_unified --yes"
    echo ""
    echo "  # Step3 迁移演练（per_table 策略）"
    echo "  $0 step3-migration-drill --strategy per_table --project-key myproject --dry-run"
}

# ============================================================================
# 主入口
# ============================================================================
main() {
    local command="${1:-help}"
    shift || true
    
    case $command in
        precheck)
            do_precheck "$@"
            ;;
        backup)
            do_backup "$@"
            ;;
        restore)
            do_restore "$@"
            ;;
        cleanup)
            do_cleanup "$@"
            ;;
        verify)
            do_verify "$@"
            ;;
        bootstrap)
            do_bootstrap "$@"
            ;;
        rotate)
            do_rotate "$@"
            ;;
        pre-upgrade)
            do_pre_upgrade "$@"
            ;;
        step3-index)
            do_step3_index "$@"
            ;;
        step3-query)
            do_step3_query "$@"
            ;;
        step3-check)
            do_step3_check "$@"
            ;;
        step3-migrate)
            do_step3_migrate "$@"
            ;;
        step3-inspect)
            do_step3_inspect "$@"
            ;;
        step3-migration-drill)
            do_step3_migration_drill "$@"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            log_error "未知命令: $command"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
