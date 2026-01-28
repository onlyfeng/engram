#!/bin/bash
# =============================================================================
# run_artifact_audit_cron.sh - 制品审计 cron/CI 调度脚本
#
# 功能:
#   - 包装 artifact_audit.py 用于 cron/CI 环境
#   - 支持增量审计（自动读取/保存游标文件）
#   - 输出结构化 JSON 报告
#   - 支持告警集成（webhook、邮件）
#
# 用法:
#   # 全量审计
#   ./run_artifact_audit_cron.sh
#
#   # 增量审计（使用游标文件）
#   ./run_artifact_audit_cron.sh --incremental
#
#   # 自定义配置
#   AUDIT_WORKERS=4 AUDIT_SAMPLE_RATE=0.1 ./run_artifact_audit_cron.sh
#
# 环境变量:
#   AUDIT_CURSOR_FILE   - 增量游标文件路径（默认: /tmp/artifact_audit_cursor）
#   AUDIT_REPORT_DIR    - 报告输出目录（默认: /tmp/artifact_audit_reports）
#   AUDIT_WORKERS       - 并发线程数（默认: 2）
#   AUDIT_SAMPLE_RATE   - 采样率 0.0-1.0（默认: 1.0 全量）
#   AUDIT_HEAD_ONLY     - 是否使用 head-only 模式（默认: false）
#   AUDIT_FAIL_ON_MISMATCH - 发现不匹配时失败（默认: true）
#   AUDIT_ALERT_WEBHOOK - 告警 webhook URL（可选）
#   POSTGRES_DSN        - 数据库连接字符串（必须）
#   ENGRAM_S3_*         - S3 配置（参考 artifact_audit.py）
#
# Cron 示例:
#   # 每天凌晨 2 点增量审计
#   0 2 * * * /path/to/run_artifact_audit_cron.sh --incremental >> /var/log/artifact_audit.log 2>&1
#
#   # 每周日全量审计
#   0 3 * * 0 /path/to/run_artifact_audit_cron.sh >> /var/log/artifact_audit_full.log 2>&1
# =============================================================================

set -euo pipefail

# =============================================================================
# 配置
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT_SCRIPT="${SCRIPT_DIR}/../artifact_audit.py"

# 默认配置
AUDIT_CURSOR_FILE="${AUDIT_CURSOR_FILE:-/tmp/artifact_audit_cursor}"
AUDIT_REPORT_DIR="${AUDIT_REPORT_DIR:-/tmp/artifact_audit_reports}"
AUDIT_WORKERS="${AUDIT_WORKERS:-2}"
AUDIT_SAMPLE_RATE="${AUDIT_SAMPLE_RATE:-1.0}"
AUDIT_HEAD_ONLY="${AUDIT_HEAD_ONLY:-false}"
AUDIT_FAIL_ON_MISMATCH="${AUDIT_FAIL_ON_MISMATCH:-true}"
AUDIT_ALERT_WEBHOOK="${AUDIT_ALERT_WEBHOOK:-}"

# =============================================================================
# 函数
# =============================================================================

log_info() {
    echo "[$(date -Iseconds)] [INFO] $*"
}

log_warn() {
    echo "[$(date -Iseconds)] [WARN] $*" >&2
}

log_error() {
    echo "[$(date -Iseconds)] [ERROR] $*" >&2
}

# 发送告警
send_alert() {
    local title="$1"
    local message="$2"
    local report_file="${3:-}"
    
    if [ -z "${AUDIT_ALERT_WEBHOOK}" ]; then
        return 0
    fi
    
    log_info "发送告警到 webhook..."
    
    local payload
    payload=$(cat <<EOF
{
    "title": "${title}",
    "message": "${message}",
    "timestamp": "$(date -Iseconds)",
    "hostname": "$(hostname)",
    "report_file": "${report_file}"
}
EOF
)
    
    curl -s -X POST \
        -H "Content-Type: application/json" \
        -d "${payload}" \
        "${AUDIT_ALERT_WEBHOOK}" || log_warn "发送告警失败"
}

# 解析命令行参数
INCREMENTAL=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --incremental|-i)
            INCREMENTAL=true
            shift
            ;;
        --help|-h)
            head -50 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
            exit 0
            ;;
        *)
            log_error "未知参数: $1"
            exit 1
            ;;
    esac
done

# =============================================================================
# 主逻辑
# =============================================================================

log_info "=========================================="
log_info "制品审计开始"
log_info "=========================================="

# 创建报告目录
mkdir -p "${AUDIT_REPORT_DIR}"

# 构建审计命令
AUDIT_ARGS=(
    "--json"
    "--workers" "${AUDIT_WORKERS}"
    "--sample-rate" "${AUDIT_SAMPLE_RATE}"
)

if [ "${AUDIT_HEAD_ONLY}" = "true" ]; then
    AUDIT_ARGS+=("--head-only")
fi

if [ "${AUDIT_FAIL_ON_MISMATCH}" = "true" ]; then
    AUDIT_ARGS+=("--fail-on-mismatch")
fi

# 增量模式：读取游标
if [ "${INCREMENTAL}" = "true" ]; then
    if [ -f "${AUDIT_CURSOR_FILE}" ]; then
        SINCE_VALUE=$(cat "${AUDIT_CURSOR_FILE}")
        if [ -n "${SINCE_VALUE}" ]; then
            log_info "增量审计，起始时间: ${SINCE_VALUE}"
            AUDIT_ARGS+=("--since" "${SINCE_VALUE}")
        fi
    else
        log_info "首次增量审计，将创建游标文件"
    fi
fi

# 生成报告文件名
REPORT_TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORT_FILE="${AUDIT_REPORT_DIR}/audit_${REPORT_TIMESTAMP}.json"

log_info "执行审计..."
log_info "命令: python ${AUDIT_SCRIPT} ${AUDIT_ARGS[*]}"

# 执行审计
AUDIT_EXIT_CODE=0
python "${AUDIT_SCRIPT}" "${AUDIT_ARGS[@]}" > "${REPORT_FILE}" 2>&1 || AUDIT_EXIT_CODE=$?

# 解析报告
if [ -f "${REPORT_FILE}" ] && [ -s "${REPORT_FILE}" ]; then
    # 提取关键指标
    TOTAL_RECORDS=$(jq -r '.total_records // 0' "${REPORT_FILE}")
    AUDITED_RECORDS=$(jq -r '.audited_records // 0' "${REPORT_FILE}")
    OK_COUNT=$(jq -r '.ok_count // 0' "${REPORT_FILE}")
    MISMATCH_COUNT=$(jq -r '.mismatch_count // 0' "${REPORT_FILE}")
    MISSING_COUNT=$(jq -r '.missing_count // 0' "${REPORT_FILE}")
    ERROR_COUNT=$(jq -r '.error_count // 0' "${REPORT_FILE}")
    HEAD_ONLY_UNVERIFIED=$(jq -r '.head_only_unverified_count // 0' "${REPORT_FILE}")
    NEXT_CURSOR=$(jq -r '.next_cursor // empty' "${REPORT_FILE}")
    DURATION=$(jq -r '.duration_seconds // 0' "${REPORT_FILE}")
    
    log_info "审计完成"
    log_info "  总记录数: ${TOTAL_RECORDS}"
    log_info "  已审计: ${AUDITED_RECORDS}"
    log_info "  正常: ${OK_COUNT}"
    log_info "  不匹配: ${MISMATCH_COUNT}"
    log_info "  缺失: ${MISSING_COUNT}"
    log_info "  错误: ${ERROR_COUNT}"
    if [ "${HEAD_ONLY_UNVERIFIED}" -gt 0 ]; then
        log_info "  未验证(head-only): ${HEAD_ONLY_UNVERIFIED}"
    fi
    log_info "  耗时: ${DURATION}s"
    log_info "  报告: ${REPORT_FILE}"
    
    # 更新游标文件（仅在成功时）
    if [ -n "${NEXT_CURSOR}" ] && [ "${NEXT_CURSOR}" != "null" ]; then
        echo "${NEXT_CURSOR}" > "${AUDIT_CURSOR_FILE}"
        log_info "  下次游标: ${NEXT_CURSOR}"
    fi
    
    # 检查是否有问题
    HAS_ISSUES=false
    if [ "${MISMATCH_COUNT}" -gt 0 ] || [ "${MISSING_COUNT}" -gt 0 ]; then
        HAS_ISSUES=true
    fi
    
    if [ "${HAS_ISSUES}" = "true" ]; then
        log_warn "=========================================="
        log_warn "审计发现问题！"
        log_warn "  不匹配: ${MISMATCH_COUNT}"
        log_warn "  缺失: ${MISSING_COUNT}"
        log_warn "=========================================="
        
        # 发送告警
        send_alert \
            "制品审计发现问题" \
            "不匹配: ${MISMATCH_COUNT}, 缺失: ${MISSING_COUNT}" \
            "${REPORT_FILE}"
    fi
else
    log_error "审计报告为空或不存在: ${REPORT_FILE}"
    
    # 发送告警
    send_alert \
        "制品审计失败" \
        "审计脚本执行失败，退出码: ${AUDIT_EXIT_CODE}" \
        ""
fi

# 清理旧报告（保留最近 30 个）
REPORT_COUNT=$(ls -1 "${AUDIT_REPORT_DIR}"/audit_*.json 2>/dev/null | wc -l)
if [ "${REPORT_COUNT}" -gt 30 ]; then
    log_info "清理旧报告（保留最近 30 个）..."
    ls -1t "${AUDIT_REPORT_DIR}"/audit_*.json | tail -n +31 | xargs rm -f
fi

log_info "=========================================="
log_info "制品审计结束，退出码: ${AUDIT_EXIT_CODE}"
log_info "=========================================="

exit ${AUDIT_EXIT_CODE}
