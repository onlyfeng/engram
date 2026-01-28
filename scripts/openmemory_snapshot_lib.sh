#!/usr/bin/env bash
# OpenMemory 库快照脚本
# 用于升级前备份 libs/OpenMemory 目录，生成 tarball 和 SHA256 校验和
#
# 用法: ./scripts/openmemory_snapshot_lib.sh [options]
#
# 选项:
#   --output-dir DIR     输出目录（默认: archives/）
#   --artifacts          输出到 .artifacts/（用于 CI 上传）
#   --dry-run            仅显示将执行的操作，不实际执行
#   --json               JSON 格式输出
#   --help               显示帮助信息
#
# 环境变量:
#   SNAPSHOT_SKIP        设为 1 跳过快照（用于 CI 可选开关）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOCK_FILE="$PROJECT_ROOT/OpenMemory.upstream.lock.json"
LIB_DIR="$PROJECT_ROOT/libs/OpenMemory"

# 默认输出目录
OUTPUT_DIR="$PROJECT_ROOT/archives"
USE_ARTIFACTS=false
DRY_RUN=false
JSON_OUTPUT=false

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${GREEN}[INFO]${NC} $1"
    fi
}

log_warn() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${YELLOW}[WARN]${NC} $1"
    fi
}

log_error() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${RED}[ERROR]${NC} $1" >&2
    fi
}

log_debug() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${BLUE}[DEBUG]${NC} $1"
    fi
}

# 检查依赖
check_deps() {
    local required_deps=("tar" "sha256sum" "date")
    local optional_deps=("jq")
    local missing=()
    
    for dep in "${required_deps[@]}"; do
        if ! command -v "$dep" &> /dev/null; then
            missing+=("$dep")
        fi
    done
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "缺少必需依赖: ${missing[*]}"
        exit 1
    fi
    
    # 检查可选依赖
    for dep in "${optional_deps[@]}"; do
        if ! command -v "$dep" &> /dev/null; then
            log_warn "可选依赖 '$dep' 未安装（部分功能可能受限）"
        fi
    done
}

# 从 lock 文件读取 upstream_ref
get_upstream_ref() {
    if [[ -f "$LOCK_FILE" ]]; then
        if command -v jq &> /dev/null; then
            jq -r '.upstream_ref // "unknown"' "$LOCK_FILE"
        else
            # 无 jq 时使用 grep/sed 提取
            grep -o '"upstream_ref"[[:space:]]*:[[:space:]]*"[^"]*"' "$LOCK_FILE" | \
                sed 's/.*"upstream_ref"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || echo "unknown"
        fi
    else
        echo "unknown"
    fi
}

# 生成时间戳
get_timestamp() {
    date +%Y%m%d_%H%M%S
}

# 生成 ISO 8601 时间戳
get_iso_timestamp() {
    date -Iseconds
}

# 计算目录的 SHA256（基于 tarball）
compute_sha256() {
    local file="$1"
    sha256sum "$file" | awk '{print $1}'
}

# 显示帮助
show_help() {
    cat << EOF
OpenMemory 库快照脚本

用法: $0 [options]

选项:
  --output-dir DIR     输出目录（默认: archives/）
  --artifacts          输出到 .artifacts/（用于 CI 上传）
  --dry-run            仅显示将执行的操作，不实际执行
  --json               JSON 格式输出
  --help               显示帮助信息

环境变量:
  SNAPSHOT_SKIP        设为 1 跳过快照（用于 CI 可选开关）

示例:
  $0                          # 生成快照到 archives/
  $0 --artifacts              # 生成快照到 .artifacts/（CI 模式）
  $0 --dry-run                # 预览将执行的操作
  $0 --json                   # JSON 格式输出
  SNAPSHOT_SKIP=1 $0          # 跳过快照

输出文件:
  archives/openmemory-lib-<upstream_ref>-<timestamp>.tar.gz
  archives/openmemory-lib-<upstream_ref>-<timestamp>.sha256
EOF
}

# 创建快照
create_snapshot() {
    local upstream_ref
    local timestamp
    local archive_name
    local archive_path
    local sha256_path
    local sha256_value
    
    # 获取上游引用和时间戳
    upstream_ref=$(get_upstream_ref)
    timestamp=$(get_timestamp)
    iso_timestamp=$(get_iso_timestamp)
    
    # 构建文件名（sanitize upstream_ref 中的特殊字符）
    local safe_ref
    safe_ref=$(echo "$upstream_ref" | tr '/' '_' | tr ':' '_')
    archive_name="openmemory-lib-${safe_ref}-${timestamp}.tar.gz"
    archive_path="$OUTPUT_DIR/$archive_name"
    sha256_path="${archive_path%.tar.gz}.sha256"
    
    # 检查源目录是否存在
    if [[ ! -d "$LIB_DIR" ]]; then
        log_error "源目录不存在: $LIB_DIR"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"ok":false,"code":"SOURCE_NOT_FOUND","message":"libs/OpenMemory 目录不存在"}'
        fi
        exit 1
    fi
    
    # Dry-run 模式
    if [[ "$DRY_RUN" == "true" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            cat << EOF
{
  "ok": true,
  "dry_run": true,
  "upstream_ref": "$upstream_ref",
  "timestamp": "$timestamp",
  "archive_path": "$archive_path",
  "sha256_path": "$sha256_path",
  "source_dir": "$LIB_DIR"
}
EOF
        else
            log_info "Dry-run 模式，将执行以下操作:"
            echo "  上游引用: $upstream_ref"
            echo "  时间戳: $timestamp"
            echo "  源目录: $LIB_DIR"
            echo "  输出目录: $OUTPUT_DIR"
            echo "  归档文件: $archive_path"
            echo "  校验和文件: $sha256_path"
        fi
        return 0
    fi
    
    # 创建输出目录
    mkdir -p "$OUTPUT_DIR"
    
    log_info "创建 OpenMemory 库快照..."
    log_info "  上游引用: $upstream_ref"
    log_info "  时间戳: $timestamp"
    log_info "  源目录: $LIB_DIR"
    
    # 创建 tarball（排除 node_modules、__pycache__ 等）
    tar --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='.git' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='.env.local' \
        -czf "$archive_path" \
        -C "$PROJECT_ROOT/libs" \
        "OpenMemory"
    
    # 计算 SHA256
    sha256_value=$(compute_sha256 "$archive_path")
    
    # 写入 SHA256 文件
    echo "$sha256_value  $archive_name" > "$sha256_path"
    
    log_info "快照创建成功:"
    log_info "  归档文件: $archive_path"
    log_info "  SHA256: $sha256_value"
    
    # 更新 lock 文件的 artifacts 部分（如果存在 jq）
    local lock_updated=false
    if command -v jq &> /dev/null && [[ -f "$LOCK_FILE" ]]; then
        local tmp_lock
        tmp_lock=$(mktemp)
        
        # 添加或更新 artifacts.snapshot 字段
        jq --arg path "$archive_path" \
           --arg sha256 "$sha256_value" \
           --arg ref "$upstream_ref" \
           --arg ts "$iso_timestamp" \
           '.artifacts.snapshot = {
              "path": $path,
              "sha256": $sha256,
              "upstream_ref": $ref,
              "created_at": $ts
            }' "$LOCK_FILE" > "$tmp_lock"
        
        mv "$tmp_lock" "$LOCK_FILE"
        lock_updated=true
        log_info "已更新 OpenMemory.upstream.lock.json 的 artifacts.snapshot 信息"
    else
        log_warn "jq 未安装，跳过更新 lock 文件（快照信息将仅保存在 .sha256 文件中）"
    fi
    
    # JSON 输出
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        cat << EOF
{
  "ok": true,
  "upstream_ref": "$upstream_ref",
  "timestamp": "$timestamp",
  "archive_path": "$archive_path",
  "sha256": "$sha256_value",
  "sha256_path": "$sha256_path",
  "lock_updated": $lock_updated
}
EOF
    fi
}

# 解析参数
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --output-dir)
                OUTPUT_DIR="$2"
                shift 2
                ;;
            --artifacts)
                USE_ARTIFACTS=true
                OUTPUT_DIR="$PROJECT_ROOT/.artifacts"
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --json)
                JSON_OUTPUT=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                log_error "未知选项: $1"
                show_help
                exit 1
                ;;
        esac
    done
}

# 主入口
main() {
    parse_args "$@"
    
    # 检查跳过标志
    if [[ "${SNAPSHOT_SKIP:-0}" == "1" ]]; then
        log_info "SNAPSHOT_SKIP=1，跳过快照"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"ok":true,"skipped":true,"reason":"SNAPSHOT_SKIP=1"}'
        fi
        exit 0
    fi
    
    check_deps
    create_snapshot
}

main "$@"
