#!/usr/bin/env bash
# =============================================================================
# run_artifact_migration_playbook.sh - 制品迁移 Playbook 脚本
#
# 功能:
#   串联 artifact_audit.py、artifact_migrate.py、artifact_gc.py --dry-run
#   在切换 backend 前后各跑一次审计（支持 --since 游标增量）
#   生成机器可读 JSON 输出
#   失败时打印回滚指令
#
# 使用示例:
#   # 完整迁移流程（dry-run）
#   ./run_artifact_migration_playbook.sh \
#       --source-backend local \
#       --target-backend object \
#       --prefix scm/
#
#   # 实际执行迁移
#   ./run_artifact_migration_playbook.sh \
#       --source-backend local \
#       --target-backend object \
#       --prefix scm/ \
#       --execute
#
#   # 增量审计（使用上次游标）
#   ./run_artifact_migration_playbook.sh \
#       --source-backend local \
#       --target-backend object \
#       --prefix scm/ \
#       --since "2024-01-01T00:00:00" \
#       --execute
#
# 输出:
#   所有阶段的 JSON 报告写入 OUTPUT_DIR（默认 ./migration_reports/）
#   最终汇总报告写入 stdout
#
# 退出码:
#   0 - 成功
#   1 - 迁移过程中发现问题
#   2 - 参数错误
#   3 - 审计失败
#   4 - 迁移失败
#   5 - GC 预览失败
# =============================================================================

set -euo pipefail

# =============================================================================
# 默认配置
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_ROOT="${SCRIPT_DIR}/.."
OUTPUT_DIR="${OUTPUT_DIR:-./migration_reports}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Python 脚本路径
AUDIT_SCRIPT="${SCRIPTS_ROOT}/artifact_audit.py"
MIGRATE_SCRIPT="${SCRIPTS_ROOT}/artifact_migrate.py"
GC_SCRIPT="${SCRIPTS_ROOT}/artifact_gc.py"

# 默认参数
SOURCE_BACKEND=""
TARGET_BACKEND=""
PREFIX=""
SINCE=""
EXECUTE=false
VERIFY=true
DB_UPDATE_MODE="to-artifact-key"
WORKERS=4
VERBOSE=false

# =============================================================================
# 帮助信息
# =============================================================================

usage() {
    cat <<EOF
用法: $(basename "$0") [OPTIONS]

制品迁移 Playbook - 串联审计、迁移、GC 的完整流程

必需参数:
  --source-backend TYPE   源存储后端 (local|file|object)
  --target-backend TYPE   目标存储后端 (local|file|object)
  --prefix PREFIX         迁移前缀范围 (如 scm/ 或 attachments/)

可选参数:
  --since TIMESTAMP       增量审计起始时间 (ISO 格式，如 2024-01-01T00:00:00)
  --execute               执行实际迁移（默认为 dry-run 模式）
  --no-verify             跳过迁移后校验
  --db-update-mode MODE   DB 更新模式 (none|to-artifact-key|to-physical-s3，默认 to-artifact-key)
  --workers N             并发线程数（默认 4）
  --output-dir DIR        输出目录（默认 ./migration_reports/）
  --verbose               详细输出
  -h, --help              显示此帮助

环境变量:
  POSTGRES_DSN            数据库连接字符串
  ENGRAM_ARTIFACTS_ROOT   制品根目录（local 后端）
  ENGRAM_S3_BUCKET        S3 存储桶（object 后端）

示例:
  # Dry-run 预览
  $0 --source-backend local --target-backend object --prefix scm/

  # 实际执行
  $0 --source-backend local --target-backend object --prefix scm/ --execute

  # 增量审计
  $0 --source-backend local --target-backend object --prefix scm/ \\
     --since "2024-01-01T00:00:00" --execute
EOF
    exit 0
}

# =============================================================================
# 工具函数
# =============================================================================

log_info() {
    echo "[INFO] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2
}

log_warn() {
    echo "[WARN] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2
}

log_error() {
    echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2
}

log_step() {
    echo "" >&2
    echo "============================================================" >&2
    echo "[STEP] $*" >&2
    echo "============================================================" >&2
}

# 打印回滚指令
print_rollback_instructions() {
    local stage="$1"
    local error_msg="${2:-}"
    
    cat >&2 <<EOF

================================================================================
!! 迁移失败 - 需要手动回滚 !!
================================================================================
失败阶段: ${stage}
错误信息: ${error_msg}

回滚指令:
--------------------------------------------------------------------------------

1. 如果迁移已开始但未完成:
   # 检查目标存储中的部分数据
   python ${MIGRATE_SCRIPT} \\
       --source-backend ${TARGET_BACKEND} \\
       --target-backend ${SOURCE_BACKEND} \\
       --prefix ${PREFIX} \\
       --dry-run

2. 如果需要从目标回滚到源:
   python ${MIGRATE_SCRIPT} \\
       --source-backend ${TARGET_BACKEND} \\
       --target-backend ${SOURCE_BACKEND} \\
       --prefix ${PREFIX} \\
       --verify --execute

3. 如果 DB URI 已更新，需要回滚:
   # 查看当前 DB 中的 URI 状态
   psql "\$POSTGRES_DSN" -c "SELECT uri FROM scm.patch_blobs WHERE uri LIKE '${PREFIX}%' LIMIT 10;"
   
   # 根据 db_update_mode 执行逆向转换
   # to-artifact-key 模式无需回滚（artifact key 兼容所有后端）
   # to-physical-s3 模式需要将 s3:// 转回 artifact key

4. 检查审计报告确认数据完整性:
   python ${AUDIT_SCRIPT} --prefix ${PREFIX} --json > audit_check.json

================================================================================
EOF
}

# =============================================================================
# 参数解析
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-backend)
            SOURCE_BACKEND="$2"
            shift 2
            ;;
        --target-backend)
            TARGET_BACKEND="$2"
            shift 2
            ;;
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --since)
            SINCE="$2"
            shift 2
            ;;
        --execute)
            EXECUTE=true
            shift
            ;;
        --no-verify)
            VERIFY=false
            shift
            ;;
        --db-update-mode)
            DB_UPDATE_MODE="$2"
            shift 2
            ;;
        --workers)
            WORKERS="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            log_error "未知参数: $1"
            usage
            ;;
    esac
done

# 验证必需参数
if [[ -z "$SOURCE_BACKEND" ]]; then
    log_error "缺少必需参数: --source-backend"
    exit 2
fi

if [[ -z "$TARGET_BACKEND" ]]; then
    log_error "缺少必需参数: --target-backend"
    exit 2
fi

if [[ -z "$PREFIX" ]]; then
    log_error "缺少必需参数: --prefix"
    exit 2
fi

# 验证后端类型
for backend in "$SOURCE_BACKEND" "$TARGET_BACKEND"; do
    if [[ ! "$backend" =~ ^(local|file|object)$ ]]; then
        log_error "无效的后端类型: $backend（支持: local, file, object）"
        exit 2
    fi
done

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# =============================================================================
# 主流程
# =============================================================================

# 初始化汇总报告
SUMMARY_FILE="${OUTPUT_DIR}/migration_summary_${TIMESTAMP}.json"
PRE_AUDIT_FILE="${OUTPUT_DIR}/audit_pre_${TIMESTAMP}.json"
POST_AUDIT_FILE="${OUTPUT_DIR}/audit_post_${TIMESTAMP}.json"
MIGRATE_FILE="${OUTPUT_DIR}/migrate_${TIMESTAMP}.json"
GC_FILE="${OUTPUT_DIR}/gc_preview_${TIMESTAMP}.json"

# 构建审计参数
AUDIT_ARGS=(--prefix "$PREFIX" --json --workers "$WORKERS")
if [[ -n "$SINCE" ]]; then
    AUDIT_ARGS+=(--since "$SINCE")
fi

# 构建迁移参数
MIGRATE_ARGS=(
    --source-backend "$SOURCE_BACKEND"
    --target-backend "$TARGET_BACKEND"
    --prefix "$PREFIX"
    --concurrency "$WORKERS"
    --db-update-mode "$DB_UPDATE_MODE"
    --json
)
if [[ "$VERIFY" == "true" ]]; then
    MIGRATE_ARGS+=(--verify)
fi
if [[ "$EXECUTE" == "true" ]]; then
    MIGRATE_ARGS+=(--execute)
fi

# 构建 GC 参数（始终 dry-run）
GC_ARGS=(--prefix "$PREFIX" --json)

# =============================================================================
# Step 1: 迁移前审计
# =============================================================================

log_step "Step 1/4: 迁移前审计 (Pre-Migration Audit)"
log_info "执行审计: python ${AUDIT_SCRIPT} ${AUDIT_ARGS[*]}"

PRE_AUDIT_EXIT=0
if python "$AUDIT_SCRIPT" "${AUDIT_ARGS[@]}" > "$PRE_AUDIT_FILE" 2>&1; then
    log_info "迁移前审计完成: $PRE_AUDIT_FILE"
else
    PRE_AUDIT_EXIT=$?
    log_error "迁移前审计失败 (exit code: $PRE_AUDIT_EXIT)"
    
    # 检查是否有 mismatch/missing
    if [[ -f "$PRE_AUDIT_FILE" ]]; then
        MISMATCH_COUNT=$(jq -r '.mismatch_count // 0' "$PRE_AUDIT_FILE" 2>/dev/null || echo "0")
        MISSING_COUNT=$(jq -r '.missing_count // 0' "$PRE_AUDIT_FILE" 2>/dev/null || echo "0")
        
        if [[ "$MISMATCH_COUNT" != "0" || "$MISSING_COUNT" != "0" ]]; then
            log_error "发现数据完整性问题: mismatch=$MISMATCH_COUNT, missing=$MISSING_COUNT"
            print_rollback_instructions "pre-audit" "数据完整性问题"
            exit 3
        fi
    fi
    
    # 其他审计错误
    print_rollback_instructions "pre-audit" "审计脚本执行失败"
    exit 3
fi

# 提取审计游标（用于下次增量审计）
PRE_AUDIT_CURSOR=$(jq -r '.next_cursor // empty' "$PRE_AUDIT_FILE" 2>/dev/null || true)
log_info "审计游标 (next_cursor): ${PRE_AUDIT_CURSOR:-N/A}"

# =============================================================================
# Step 2: 执行迁移
# =============================================================================

log_step "Step 2/4: 执行迁移 (Migration)"

if [[ "$EXECUTE" == "true" ]]; then
    log_info "模式: 实际执行"
else
    log_info "模式: dry-run（预览）"
fi

log_info "执行迁移: python ${MIGRATE_SCRIPT} ${MIGRATE_ARGS[*]}"

MIGRATE_EXIT=0
if python "$MIGRATE_SCRIPT" "${MIGRATE_ARGS[@]}" > "$MIGRATE_FILE" 2>&1; then
    log_info "迁移完成: $MIGRATE_FILE"
else
    MIGRATE_EXIT=$?
    log_error "迁移失败 (exit code: $MIGRATE_EXIT)"
    
    # 打印迁移结果摘要
    if [[ -f "$MIGRATE_FILE" ]]; then
        MIGRATED_COUNT=$(jq -r '.migrated_count // 0' "$MIGRATE_FILE" 2>/dev/null || echo "0")
        FAILED_COUNT=$(jq -r '.failed_count // 0' "$MIGRATE_FILE" 2>/dev/null || echo "0")
        log_error "迁移统计: migrated=$MIGRATED_COUNT, failed=$FAILED_COUNT"
    fi
    
    print_rollback_instructions "migration" "迁移脚本执行失败"
    exit 4
fi

# 提取迁移统计
MIGRATED_COUNT=$(jq -r '.migrated_count // 0' "$MIGRATE_FILE" 2>/dev/null || echo "0")
FAILED_COUNT=$(jq -r '.failed_count // 0' "$MIGRATE_FILE" 2>/dev/null || echo "0")
DB_UPDATED_COUNT=$(jq -r '.db_updated_count // 0' "$MIGRATE_FILE" 2>/dev/null || echo "0")

log_info "迁移统计: migrated=$MIGRATED_COUNT, failed=$FAILED_COUNT, db_updated=$DB_UPDATED_COUNT"

# 检查迁移失败数
if [[ "$FAILED_COUNT" != "0" ]]; then
    log_warn "有 $FAILED_COUNT 个文件迁移失败"
fi

# =============================================================================
# Step 3: 迁移后审计
# =============================================================================

log_step "Step 3/4: 迁移后审计 (Post-Migration Audit)"

# 只有在实际执行时才进行迁移后审计
if [[ "$EXECUTE" == "true" ]]; then
    # 使用迁移前的游标进行增量审计（验证新迁移的数据）
    POST_AUDIT_ARGS=(--prefix "$PREFIX" --json --workers "$WORKERS")
    if [[ -n "$PRE_AUDIT_CURSOR" ]]; then
        POST_AUDIT_ARGS+=(--since "$PRE_AUDIT_CURSOR")
    fi
    
    log_info "执行审计: python ${AUDIT_SCRIPT} ${POST_AUDIT_ARGS[*]}"
    
    POST_AUDIT_EXIT=0
    if python "$AUDIT_SCRIPT" "${POST_AUDIT_ARGS[@]}" > "$POST_AUDIT_FILE" 2>&1; then
        log_info "迁移后审计完成: $POST_AUDIT_FILE"
    else
        POST_AUDIT_EXIT=$?
        log_error "迁移后审计失败 (exit code: $POST_AUDIT_EXIT)"
        
        # 检查是否有 mismatch/missing
        if [[ -f "$POST_AUDIT_FILE" ]]; then
            MISMATCH_COUNT=$(jq -r '.mismatch_count // 0' "$POST_AUDIT_FILE" 2>/dev/null || echo "0")
            MISSING_COUNT=$(jq -r '.missing_count // 0' "$POST_AUDIT_FILE" 2>/dev/null || echo "0")
            
            if [[ "$MISMATCH_COUNT" != "0" || "$MISSING_COUNT" != "0" ]]; then
                log_error "迁移后发现数据完整性问题: mismatch=$MISMATCH_COUNT, missing=$MISSING_COUNT"
                print_rollback_instructions "post-audit" "迁移后数据完整性问题"
                exit 3
            fi
        fi
    fi
    
    # 提取迁移后审计的游标
    POST_AUDIT_CURSOR=$(jq -r '.next_cursor // empty' "$POST_AUDIT_FILE" 2>/dev/null || true)
    log_info "迁移后审计游标: ${POST_AUDIT_CURSOR:-N/A}"
else
    log_info "跳过迁移后审计（dry-run 模式）"
    echo '{"skipped": true, "reason": "dry-run mode"}' > "$POST_AUDIT_FILE"
fi

# =============================================================================
# Step 4: GC 预览
# =============================================================================

log_step "Step 4/4: GC 预览 (Garbage Collection Preview)"
log_info "执行 GC 预览: python ${GC_SCRIPT} ${GC_ARGS[*]}"

GC_EXIT=0
if python "$GC_SCRIPT" "${GC_ARGS[@]}" > "$GC_FILE" 2>&1; then
    log_info "GC 预览完成: $GC_FILE"
else
    GC_EXIT=$?
    log_warn "GC 预览失败 (exit code: $GC_EXIT)，这可能不影响迁移结果"
fi

# 提取 GC 统计
GC_CANDIDATES=$(jq -r '.candidates_count // 0' "$GC_FILE" 2>/dev/null || echo "0")
GC_SIZE_MB=$(jq -r '(.total_size_bytes // 0) / 1024 / 1024 | floor' "$GC_FILE" 2>/dev/null || echo "0")

log_info "GC 预览: $GC_CANDIDATES 个候选文件，约 ${GC_SIZE_MB}MB"

# =============================================================================
# 生成汇总报告
# =============================================================================

log_step "生成汇总报告"

# 构建 JSON 汇总
cat > "$SUMMARY_FILE" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "parameters": {
    "source_backend": "${SOURCE_BACKEND}",
    "target_backend": "${TARGET_BACKEND}",
    "prefix": "${PREFIX}",
    "since": "${SINCE}",
    "execute": ${EXECUTE},
    "verify": ${VERIFY},
    "db_update_mode": "${DB_UPDATE_MODE}",
    "workers": ${WORKERS}
  },
  "results": {
    "pre_audit": {
      "file": "${PRE_AUDIT_FILE}",
      "exit_code": ${PRE_AUDIT_EXIT},
      "next_cursor": "${PRE_AUDIT_CURSOR:-null}"
    },
    "migration": {
      "file": "${MIGRATE_FILE}",
      "exit_code": ${MIGRATE_EXIT},
      "migrated_count": ${MIGRATED_COUNT},
      "failed_count": ${FAILED_COUNT},
      "db_updated_count": ${DB_UPDATED_COUNT}
    },
    "post_audit": {
      "file": "${POST_AUDIT_FILE}",
      "exit_code": ${POST_AUDIT_EXIT:-0}
    },
    "gc_preview": {
      "file": "${GC_FILE}",
      "exit_code": ${GC_EXIT},
      "candidates_count": ${GC_CANDIDATES},
      "total_size_mb": ${GC_SIZE_MB}
    }
  },
  "next_steps": {
    "next_audit_cursor": "${POST_AUDIT_CURSOR:-${PRE_AUDIT_CURSOR:-null}}",
    "gc_command": "python ${GC_SCRIPT} --prefix ${PREFIX} --trash-prefix .trash/ --delete"
  }
}
EOF

log_info "汇总报告已写入: $SUMMARY_FILE"

# =============================================================================
# 输出最终结果
# =============================================================================

echo ""
echo "================================================================================"
echo "                         迁移 Playbook 执行完成"
echo "================================================================================"
echo ""
echo "参数配置:"
echo "  源后端:       $SOURCE_BACKEND"
echo "  目标后端:     $TARGET_BACKEND"
echo "  迁移前缀:     $PREFIX"
echo "  执行模式:     $(if [[ "$EXECUTE" == "true" ]]; then echo "实际执行"; else echo "dry-run（预览）"; fi)"
echo "  DB 更新模式:  $DB_UPDATE_MODE"
echo ""
echo "执行结果:"
echo "  迁移前审计:   $(if [[ $PRE_AUDIT_EXIT -eq 0 ]]; then echo "✓ 通过"; else echo "✗ 失败"; fi)"
echo "  迁移执行:     $(if [[ $MIGRATE_EXIT -eq 0 ]]; then echo "✓ 完成"; else echo "✗ 失败"; fi) (migrated=$MIGRATED_COUNT, failed=$FAILED_COUNT)"
if [[ "$EXECUTE" == "true" ]]; then
echo "  迁移后审计:   $(if [[ ${POST_AUDIT_EXIT:-0} -eq 0 ]]; then echo "✓ 通过"; else echo "✗ 失败"; fi)"
fi
echo "  GC 预览:      $(if [[ $GC_EXIT -eq 0 ]]; then echo "✓ 完成"; else echo "⚠ 警告"; fi) (candidates=$GC_CANDIDATES)"
echo ""
echo "输出文件:"
echo "  汇总报告:     $SUMMARY_FILE"
echo "  迁移前审计:   $PRE_AUDIT_FILE"
echo "  迁移结果:     $MIGRATE_FILE"
if [[ "$EXECUTE" == "true" ]]; then
echo "  迁移后审计:   $POST_AUDIT_FILE"
fi
echo "  GC 预览:      $GC_FILE"
echo ""
echo "下次增量审计游标 (--since):"
echo "  ${POST_AUDIT_CURSOR:-${PRE_AUDIT_CURSOR:-N/A}}"
echo ""

# 如果是 dry-run 模式，提示下一步操作
if [[ "$EXECUTE" != "true" ]]; then
    echo "下一步操作:"
    echo "  确认预览结果后，添加 --execute 参数执行实际迁移:"
    echo "  $0 --source-backend $SOURCE_BACKEND --target-backend $TARGET_BACKEND --prefix $PREFIX --execute"
    echo ""
fi

# 如果有 GC 候选，提示 GC 操作
if [[ "$GC_CANDIDATES" != "0" ]]; then
    echo "GC 清理提示:"
    echo "  发现 $GC_CANDIDATES 个孤立文件，可执行以下命令清理:"
    echo "  python ${GC_SCRIPT} --prefix $PREFIX --trash-prefix .trash/ --delete"
    echo ""
fi

echo "================================================================================"

# 输出 JSON 格式的汇总报告到 stdout
cat "$SUMMARY_FILE"

# 返回最终状态
if [[ $MIGRATE_EXIT -ne 0 ]]; then
    exit 4
elif [[ $PRE_AUDIT_EXIT -ne 0 ]] || [[ ${POST_AUDIT_EXIT:-0} -ne 0 ]]; then
    exit 3
elif [[ "$FAILED_COUNT" != "0" ]]; then
    exit 1
else
    exit 0
fi
