#!/bin/bash
# ============================================================================
# apply_lifecycle.sh - S3 生命周期规则应用脚本
# 支持 MinIO (mc) 与 AWS (awscli) 两种执行路径
#
# 用法:
#   ./apply_lifecycle.sh [OPTIONS]
#
# 选项:
#   -b, --bucket BUCKET      目标 bucket 名称（必填）
#   -p, --policy FILE        生命周期策略 JSON 文件路径（默认: ../templates/s3_lifecycle_policy.json）
#   -e, --endpoint URL       S3 端点（MinIO 模式必填，AWS 模式可选）
#   -m, --mode MODE          执行模式: minio|aws（自动检测）
#   -a, --alias ALIAS        mc 别名（仅 minio 模式，默认: myminio）
#   --access-key KEY         访问密钥（或使用环境变量）
#   --secret-key KEY         密钥（或使用环境变量）
#   --region REGION          AWS 区域（仅 aws 模式，默认: us-east-1）
#   --insecure               跳过 TLS 验证（开发环境）
#   --verify-only            仅验证规则，不应用
#   --dry-run                显示将执行的命令但不实际执行
#   -v, --verbose            详细输出
#   -h, --help               显示帮助
#
# 环境变量:
#   ENGRAM_S3_ENDPOINT       S3 端点
#   ENGRAM_S3_ACCESS_KEY     访问密钥
#   ENGRAM_S3_SECRET_KEY     密钥
#   ENGRAM_S3_BUCKET         目标 bucket
#   ENGRAM_S3_REGION         AWS 区域
#   MINIO_ROOT_USER          MinIO root 用户（备选）
#   MINIO_ROOT_PASSWORD      MinIO root 密码（备选）
#
# 示例:
#   # MinIO 模式
#   ./apply_lifecycle.sh -b engram -e http://localhost:9000 -m minio
#
#   # AWS 模式
#   ./apply_lifecycle.sh -b my-bucket -m aws --region us-west-2
#
#   # 仅验证
#   ./apply_lifecycle.sh -b engram -e http://localhost:9000 --verify-only
# ============================================================================

set -e

# 脚本目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_POLICY="${SCRIPT_DIR}/../templates/s3_lifecycle_policy.json"

# 默认值
BUCKET=""
POLICY_FILE="${DEFAULT_POLICY}"
ENDPOINT="${ENGRAM_S3_ENDPOINT:-}"
MODE=""
MC_ALIAS="myminio"
ACCESS_KEY="${ENGRAM_S3_ACCESS_KEY:-${MINIO_ROOT_USER:-}}"
SECRET_KEY="${ENGRAM_S3_SECRET_KEY:-${MINIO_ROOT_PASSWORD:-}}"
REGION="${ENGRAM_S3_REGION:-us-east-1}"
INSECURE=""
VERIFY_ONLY=false
DRY_RUN=false
VERBOSE=false

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "[INFO] $1"
}

log_ok() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_debug() {
    if [ "$VERBOSE" = true ]; then
        echo -e "[DEBUG] $1"
    fi
}

usage() {
    head -50 "$0" | grep "^#" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
    exit 0
}

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        -b|--bucket)
            BUCKET="$2"
            shift 2
            ;;
        -p|--policy)
            POLICY_FILE="$2"
            shift 2
            ;;
        -e|--endpoint)
            ENDPOINT="$2"
            shift 2
            ;;
        -m|--mode)
            MODE="$2"
            shift 2
            ;;
        -a|--alias)
            MC_ALIAS="$2"
            shift 2
            ;;
        --access-key)
            ACCESS_KEY="$2"
            shift 2
            ;;
        --secret-key)
            SECRET_KEY="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --insecure)
            INSECURE="--insecure"
            shift
            ;;
        --verify-only)
            VERIFY_ONLY=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            log_error "未知参数: $1"
            exit 1
            ;;
    esac
done

# 从环境变量填充 bucket（如未指定）
if [ -z "$BUCKET" ]; then
    BUCKET="${ENGRAM_S3_BUCKET:-}"
fi

# 验证必填参数
if [ -z "$BUCKET" ]; then
    log_error "必须指定 bucket (-b/--bucket 或 ENGRAM_S3_BUCKET 环境变量)"
    exit 1
fi

# 检查策略文件
if [ ! -f "$POLICY_FILE" ]; then
    log_error "策略文件不存在: $POLICY_FILE"
    exit 1
fi

# 自动检测执行模式
detect_mode() {
    if [ -n "$MODE" ]; then
        echo "$MODE"
        return
    fi
    
    # 优先使用 mc（如果可用且有 endpoint）
    if command -v mc &> /dev/null && [ -n "$ENDPOINT" ]; then
        echo "minio"
        return
    fi
    
    # 其次使用 awscli
    if command -v aws &> /dev/null; then
        echo "aws"
        return
    fi
    
    # 最后尝试 mc
    if command -v mc &> /dev/null; then
        echo "minio"
        return
    fi
    
    log_error "未找到 mc 或 aws 命令，请安装 MinIO Client 或 AWS CLI"
    exit 1
}

MODE=$(detect_mode)
log_info "执行模式: $MODE"
log_info "目标 bucket: $BUCKET"
log_info "策略文件: $POLICY_FILE"

# ============================================================================
# MinIO 模式 (mc)
# ============================================================================
apply_minio() {
    log_info "使用 MinIO Client (mc) 应用生命周期规则"
    
    # 验证 endpoint
    if [ -z "$ENDPOINT" ]; then
        log_error "MinIO 模式需要指定 endpoint (-e/--endpoint)"
        exit 1
    fi
    
    # 验证凭证
    if [ -z "$ACCESS_KEY" ] || [ -z "$SECRET_KEY" ]; then
        log_error "需要 access key 和 secret key"
        exit 1
    fi
    
    log_debug "Endpoint: $ENDPOINT"
    log_debug "Alias: $MC_ALIAS"
    
    # 配置 mc 别名
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] mc $INSECURE alias set $MC_ALIAS $ENDPOINT [ACCESS_KEY] [SECRET_KEY]"
    else
        mc $INSECURE alias set "$MC_ALIAS" "$ENDPOINT" "$ACCESS_KEY" "$SECRET_KEY" > /dev/null
        log_ok "mc 别名已配置: $MC_ALIAS -> $ENDPOINT"
    fi
    
    # 应用或验证
    if [ "$VERIFY_ONLY" = true ]; then
        log_info "验证模式: 仅检查现有规则"
    else
        # 应用生命周期规则
        if [ "$DRY_RUN" = true ]; then
            echo "[DRY-RUN] mc $INSECURE ilm import $MC_ALIAS/$BUCKET < $POLICY_FILE"
        else
            log_info "导入生命周期规则..."
            if mc $INSECURE ilm import "$MC_ALIAS/$BUCKET" < "$POLICY_FILE"; then
                log_ok "生命周期规则已应用"
            else
                log_error "应用生命周期规则失败"
                exit 1
            fi
        fi
    fi
    
    # 验证规则
    verify_minio
}

verify_minio() {
    log_info "验证生命周期规则..."
    
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] mc $INSECURE ilm ls $MC_ALIAS/$BUCKET"
        return 0
    fi
    
    ILM_OUTPUT=$(mc $INSECURE ilm ls "$MC_ALIAS/$BUCKET" 2>&1) || {
        log_error "无法获取生命周期规则: $ILM_OUTPUT"
        return 1
    }
    
    echo "$ILM_OUTPUT"
    echo ""
    
    # 验证关键规则
    RULES_VERIFIED=0
    EXPECTED_RULES=("tmp-cleanup-7d" "exports-cleanup-90d" "trash-cleanup-30d" "abort-incomplete-multipart-1d")
    
    for rule in "${EXPECTED_RULES[@]}"; do
        if echo "$ILM_OUTPUT" | grep -q "$rule"; then
            log_ok "规则 $rule 已生效"
            RULES_VERIFIED=$((RULES_VERIFIED + 1))
        else
            log_warn "规则 $rule 未找到"
        fi
    done
    
    if [ $RULES_VERIFIED -ge ${#EXPECTED_RULES[@]} ]; then
        log_ok "所有 ${#EXPECTED_RULES[@]} 条生命周期规则已验证"
        return 0
    else
        log_warn "仅验证到 $RULES_VERIFIED/${#EXPECTED_RULES[@]} 条规则"
        return 1
    fi
}

# ============================================================================
# AWS 模式 (awscli)
# ============================================================================
apply_aws() {
    log_info "使用 AWS CLI 应用生命周期规则"
    
    # 设置凭证（如提供）
    if [ -n "$ACCESS_KEY" ] && [ -n "$SECRET_KEY" ]; then
        export AWS_ACCESS_KEY_ID="$ACCESS_KEY"
        export AWS_SECRET_ACCESS_KEY="$SECRET_KEY"
        log_debug "使用提供的 AWS 凭证"
    fi
    
    # 设置 endpoint（如提供，用于 MinIO 兼容模式）
    ENDPOINT_URL=""
    if [ -n "$ENDPOINT" ]; then
        ENDPOINT_URL="--endpoint-url $ENDPOINT"
        log_debug "使用自定义 endpoint: $ENDPOINT"
    fi
    
    log_debug "Region: $REGION"
    
    # 应用或验证
    if [ "$VERIFY_ONLY" = true ]; then
        log_info "验证模式: 仅检查现有规则"
    else
        # 应用生命周期规则
        if [ "$DRY_RUN" = true ]; then
            echo "[DRY-RUN] aws s3api put-bucket-lifecycle-configuration --bucket $BUCKET --lifecycle-configuration file://$POLICY_FILE $ENDPOINT_URL --region $REGION"
        else
            log_info "应用生命周期规则..."
            if aws s3api put-bucket-lifecycle-configuration \
                --bucket "$BUCKET" \
                --lifecycle-configuration "file://$POLICY_FILE" \
                $ENDPOINT_URL \
                --region "$REGION"; then
                log_ok "生命周期规则已应用"
            else
                log_error "应用生命周期规则失败"
                exit 1
            fi
        fi
    fi
    
    # 验证规则
    verify_aws
}

verify_aws() {
    log_info "验证生命周期规则..."
    
    ENDPOINT_URL=""
    if [ -n "$ENDPOINT" ]; then
        ENDPOINT_URL="--endpoint-url $ENDPOINT"
    fi
    
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] aws s3api get-bucket-lifecycle-configuration --bucket $BUCKET $ENDPOINT_URL --region $REGION"
        return 0
    fi
    
    LIFECYCLE_OUTPUT=$(aws s3api get-bucket-lifecycle-configuration \
        --bucket "$BUCKET" \
        $ENDPOINT_URL \
        --region "$REGION" 2>&1) || {
        log_error "无法获取生命周期规则: $LIFECYCLE_OUTPUT"
        return 1
    }
    
    echo "$LIFECYCLE_OUTPUT"
    echo ""
    
    # 验证关键规则
    RULES_VERIFIED=0
    EXPECTED_RULES=("tmp-cleanup-7d" "exports-cleanup-90d" "trash-cleanup-30d" "abort-incomplete-multipart-1d")
    
    for rule in "${EXPECTED_RULES[@]}"; do
        if echo "$LIFECYCLE_OUTPUT" | grep -q "$rule"; then
            log_ok "规则 $rule 已生效"
            RULES_VERIFIED=$((RULES_VERIFIED + 1))
        else
            log_warn "规则 $rule 未找到"
        fi
    done
    
    if [ $RULES_VERIFIED -ge ${#EXPECTED_RULES[@]} ]; then
        log_ok "所有 ${#EXPECTED_RULES[@]} 条生命周期规则已验证"
        return 0
    else
        log_warn "仅验证到 $RULES_VERIFIED/${#EXPECTED_RULES[@]} 条规则"
        return 1
    fi
}

# ============================================================================
# 主逻辑
# ============================================================================
echo "========================================"
echo "S3 生命周期规则管理"
echo "========================================"
echo ""

case "$MODE" in
    minio)
        apply_minio
        ;;
    aws)
        apply_aws
        ;;
    *)
        log_error "未知模式: $MODE（支持: minio, aws）"
        exit 1
        ;;
esac

echo ""
echo "========================================"
if [ "$VERIFY_ONLY" = true ]; then
    log_ok "验证完成"
else
    log_ok "生命周期规则应用完成"
fi
echo "========================================"
