#!/bin/bash
# =============================================================================
# verify_build_boundaries.sh
# Docker 构建边界校验脚本
#
# 功能:
#   1. 校验 docker-compose.unified.yml 中的构建配置
#   2. 检查 Dockerfile/compose 是否包含不当的 context 用法
#   3. 统计 context 中大文件模式并 fail-fast
#   4. 可选执行 docker compose build 验证
#
# 用法:
#   ./scripts/verify_build_boundaries.sh [--dry-run] [--build] [--verbose]
#
# 选项:
#   --dry-run    仅执行静态检查，不执行 docker build
#   --build      执行实际的 docker compose build
#   --verbose    显示详细输出
#   --json       JSON 格式输出
#
# 退出码:
#   0 - 校验通过
#   1 - 发现问题
#   2 - 脚本错误
# =============================================================================

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 默认参数
DRY_RUN=false
DO_BUILD=false
VERBOSE=false
JSON_OUTPUT=false
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.unified.yml}"
EXIT_CODE=0

# 问题计数
ERRORS=0
WARNINGS=0

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --build)
            DO_BUILD=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --compose-file|-f)
            COMPOSE_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 [--dry-run] [--build] [--verbose] [--json] [--compose-file FILE]"
            echo ""
            echo "选项:"
            echo "  --dry-run      仅执行静态检查，不执行 docker build"
            echo "  --build        执行实际的 docker compose build"
            echo "  --verbose      显示详细输出"
            echo "  --json         JSON 格式输出"
            echo "  --compose-file 指定 compose 文件（默认: docker-compose.unified.yml）"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            exit 2
            ;;
    esac
done

# 日志函数
log_info() {
    if [ "$JSON_OUTPUT" = "false" ]; then
        echo -e "${BLUE}[INFO]${NC} $1"
    fi
}

log_ok() {
    if [ "$JSON_OUTPUT" = "false" ]; then
        echo -e "${GREEN}[OK]${NC} $1"
    fi
}

log_warn() {
    if [ "$JSON_OUTPUT" = "false" ]; then
        echo -e "${YELLOW}[WARN]${NC} $1"
    fi
    ((WARNINGS++)) || true
}

log_error() {
    if [ "$JSON_OUTPUT" = "false" ]; then
        echo -e "${RED}[ERROR]${NC} $1"
    fi
    ((ERRORS++)) || true
}

log_verbose() {
    if [ "$VERBOSE" = "true" ] && [ "$JSON_OUTPUT" = "false" ]; then
        echo -e "       $1"
    fi
}

# =============================================================================
# 检查 1: Compose 文件存在性
# =============================================================================
check_compose_file() {
    log_info "检查 Compose 文件..."
    
    if [ ! -f "$COMPOSE_FILE" ]; then
        log_error "Compose 文件不存在: $COMPOSE_FILE"
        return 1
    fi
    
    log_ok "Compose 文件存在: $COMPOSE_FILE"
    return 0
}

# =============================================================================
# 检查 2: Compose 中的 context 配置
# =============================================================================
check_compose_contexts() {
    log_info "检查 Compose 构建上下文配置..."
    
    local found_issues=0
    
    # 检查是否有 "context: ." 在根目录以外的位置（潜在问题）
    # 这里我们检查合理的 context 配置
    
    # 获取所有 build context 配置
    local contexts
    contexts=$(grep -E '^\s+context:' "$COMPOSE_FILE" 2>/dev/null || true)
    
    if [ -z "$contexts" ]; then
        log_verbose "未找到显式的 context 配置（使用默认值）"
        return 0
    fi
    
    log_verbose "发现的 context 配置:"
    while IFS= read -r line; do
        context_path=$(echo "$line" | sed 's/.*context:\s*//' | tr -d '"' | tr -d "'" | xargs)
        log_verbose "  - $context_path"
        
        # 检查绝对路径（不推荐）
        if [[ "$context_path" == /* ]]; then
            log_warn "发现绝对路径 context: $context_path（不推荐，影响可移植性）"
            found_issues=1
        fi
        
        # 检查 context 目录是否存在
        if [ -n "$context_path" ] && [ "$context_path" != "." ]; then
            if [ ! -d "$context_path" ]; then
                log_error "context 目录不存在: $context_path"
                found_issues=1
            fi
        fi
    done <<< "$contexts"
    
    if [ $found_issues -eq 0 ]; then
        log_ok "Compose context 配置检查通过"
    fi
    
    return $found_issues
}

# =============================================================================
# 检查 3: Dockerfile 中的不当 COPY 用法
# =============================================================================
check_dockerfile_copy_patterns() {
    log_info "检查 Dockerfile COPY 模式..."
    
    local found_issues=0
    local dockerfiles
    
    # 查找所有 Dockerfile
    dockerfiles=$(find . -name "Dockerfile*" -type f 2>/dev/null | grep -v node_modules | grep -v __pycache__ || true)
    
    if [ -z "$dockerfiles" ]; then
        log_warn "未找到 Dockerfile 文件"
        return 0
    fi
    
    for dockerfile in $dockerfiles; do
        log_verbose "检查: $dockerfile"
        
        # 检查 1: COPY .. 模式（危险，可能导致 context 泄露）
        if grep -qE '^COPY\s+\.\.' "$dockerfile" 2>/dev/null; then
            log_error "发现危险的 COPY .. 模式: $dockerfile"
            log_verbose "  建议: 调整 build context 或使用具体路径"
            found_issues=1
        fi
        
        # 检查 2: COPY . . 在非隔离 context 中（潜在大 context 问题）
        if grep -qE '^COPY\s+\.\s+\.' "$dockerfile" 2>/dev/null; then
            # 获取 dockerfile 所在目录
            local dockerfile_dir
            dockerfile_dir=$(dirname "$dockerfile")
            
            # 如果是根目录的 Dockerfile 使用 COPY . .，发出警告
            if [ "$dockerfile_dir" = "." ]; then
                log_warn "根目录 Dockerfile 使用 COPY . . 可能导致大 context: $dockerfile"
                log_verbose "  建议: 确保 .dockerignore 正确配置"
            fi
        fi
        
        # 检查 3: 检查是否缺少 .dockerignore
        local dockerfile_dir
        dockerfile_dir=$(dirname "$dockerfile")
        local dockerignore="$dockerfile_dir/.dockerignore"
        
        if [ ! -f "$dockerignore" ]; then
            # 如果 Dockerfile 中有 COPY . 模式，缺少 .dockerignore 是问题
            if grep -qE '^COPY\s+\.' "$dockerfile" 2>/dev/null; then
                log_warn "缺少 .dockerignore: $dockerfile_dir（COPY . 模式需要 .dockerignore）"
            fi
        fi
    done
    
    if [ $found_issues -eq 0 ]; then
        log_ok "Dockerfile COPY 模式检查通过"
    fi
    
    return $found_issues
}

# =============================================================================
# 检查 4: 大文件模式检测
# =============================================================================
check_large_file_patterns() {
    log_info "检查大文件模式..."
    
    local found_issues=0
    
    # 定义应该被忽略的大文件模式
    local large_patterns=(
        "*.zip"
        "*.tar.gz"
        "*.tar"
        "*.7z"
        "*.rar"
        "node_modules"
        "__pycache__"
        ".git"
        "*.pyc"
        ".venv"
        "venv"
        "*.egg-info"
        "dist"
        "build"
        ".next"
    )
    
    # 检查根目录 .dockerignore
    local root_dockerignore=".dockerignore"
    
    if [ -f "$root_dockerignore" ]; then
        log_verbose "检查根目录 .dockerignore..."
        
        for pattern in "${large_patterns[@]}"; do
            if ! grep -qF "$pattern" "$root_dockerignore" 2>/dev/null; then
                # 只对关键模式发出警告
                case "$pattern" in
                    ".git"|"node_modules"|"__pycache__"|".venv"|"venv")
                        log_warn ".dockerignore 可能缺少关键模式: $pattern"
                        ;;
                esac
            fi
        done
    else
        # 检查是否有根目录 context 的构建配置
        if grep -qE 'context:\s*\.' "$COMPOSE_FILE" 2>/dev/null; then
            log_warn "根目录缺少 .dockerignore（有 context: . 配置）"
            found_issues=1
        fi
    fi
    
    # 统计根目录下的大文件
    log_verbose "扫描潜在的大文件/目录..."
    
    # 检查 archives 目录（如果存在且在 context 中）
    if [ -d "archives" ]; then
        local archive_size
        archive_size=$(du -sh archives 2>/dev/null | cut -f1 || echo "unknown")
        log_verbose "  archives/ 目录大小: $archive_size"
        
        # 检查是否在 .dockerignore 中
        if [ -f "$root_dockerignore" ]; then
            if ! grep -qE '^archives/?$' "$root_dockerignore" 2>/dev/null; then
                log_warn "archives/ 目录未在 .dockerignore 中排除（可能导致构建缓慢）"
            fi
        fi
    fi
    
    if [ $found_issues -eq 0 ]; then
        log_ok "大文件模式检查通过"
    fi
    
    return $found_issues
}

# =============================================================================
# 检查 5: 执行 docker compose build（可选）
# =============================================================================
run_docker_build() {
    if [ "$DRY_RUN" = "true" ]; then
        log_info "跳过 docker build（--dry-run 模式）"
        return 0
    fi
    
    if [ "$DO_BUILD" = "false" ]; then
        log_info "跳过 docker build（未指定 --build）"
        return 0
    fi
    
    log_info "执行 docker compose build..."
    
    # 构建所有服务（排除 profile 服务）
    local build_services="openmemory openmemory_migrate gateway worker"
    
    for service in $build_services; do
        log_info "构建服务: $service"
        
        if ! docker compose -f "$COMPOSE_FILE" build "$service" 2>&1; then
            log_error "构建失败: $service"
            return 1
        fi
        
        log_ok "构建成功: $service"
    done
    
    log_ok "所有服务构建完成"
    return 0
}

# =============================================================================
# 主函数
# =============================================================================
main() {
    if [ "$JSON_OUTPUT" = "false" ]; then
        echo "========================================"
        echo "Docker 构建边界校验"
        echo "========================================"
        echo ""
    fi
    
    # 执行检查
    check_compose_file || EXIT_CODE=1
    check_compose_contexts || EXIT_CODE=1
    check_dockerfile_copy_patterns || EXIT_CODE=1
    check_large_file_patterns || EXIT_CODE=1
    run_docker_build || EXIT_CODE=1
    
    # 输出结果
    if [ "$JSON_OUTPUT" = "true" ]; then
        echo "{\"ok\": $([ $EXIT_CODE -eq 0 ] && echo "true" || echo "false"), \"errors\": $ERRORS, \"warnings\": $WARNINGS}"
    else
        echo ""
        echo "========================================"
        if [ $EXIT_CODE -eq 0 ]; then
            echo -e "${GREEN}[OK] 构建边界校验通过${NC}"
            if [ $WARNINGS -gt 0 ]; then
                echo -e "     ${YELLOW}警告: $WARNINGS${NC}"
            fi
        else
            echo -e "${RED}[FAIL] 构建边界校验失败${NC}"
            echo -e "     ${RED}错误: $ERRORS${NC}"
            echo -e "     ${YELLOW}警告: $WARNINGS${NC}"
        fi
        echo "========================================"
    fi
    
    exit $EXIT_CODE
}

# 运行主函数
main
