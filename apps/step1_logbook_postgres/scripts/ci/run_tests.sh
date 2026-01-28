#!/usr/bin/env bash
# =============================================================================
# CI 测试运行脚本
# 
# 功能:
# - 自动启动/检测 PostgreSQL（使用 docker-compose.step1-test.yml）
# - 导出 TEST_PG_DSN / TEST_PG_ADMIN_DSN 环境变量
# - 执行 pytest 测试
# - 返回严格的退出码（测试失败则非 0）
#
# 用法:
#   ./ci/run_tests.sh               # 运行测试（自动启动 PG）
#   ./ci/run_tests.sh --no-pg       # 跳过 PG 启动（假设已有数据库）
#   ./ci/run_tests.sh --cleanup     # 测试后清理容器
#   ./ci/run_tests.sh --clean-cache # 运行前清理 Python 缓存（__pycache__、egg-info）
#   ./ci/run_tests.sh -- -v -x      # 传递额外 pytest 参数
#
# 环境变量:
#   PYTEST_ARGS='--junitxml=report.xml --durations=20'  # 额外 pytest 参数
#
# CI 调用示例:
#   PYTEST_ARGS='--junitxml=.artifacts/junit.xml --durations=20' ./ci/run_tests.sh --cleanup
#
# 配置文件:
#   使用 docker-compose.step1-test.yml（测试专用）
#   生产部署请使用根目录 make deploy
# =============================================================================

set -euo pipefail

# ---------- 配置 ----------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_DIR="$(dirname "$SCRIPTS_DIR")"

# PostgreSQL 连接参数（与 docker-compose.step1-test.yml 一致）
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-postgres}"
PG_PASSWORD="${PG_PASSWORD:-postgres}"
PG_DB="${PG_DB:-engram_test}"

# CI 专用 Compose 文件
COMPOSE_FILE="docker-compose.step1-test.yml"

# DSN 格式
TEST_PG_DSN="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${PG_DB}"
TEST_PG_ADMIN_DSN="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/postgres"

# 选项
START_PG=true
CLEANUP=false
CLEAN_CACHE=false

# 额外 pytest 参数（可通过环境变量 PYTEST_ARGS 或 -- 后传入）
EXTRA_PYTEST_ARGS="${PYTEST_ARGS:-}"

# 检测 docker compose 命令（v2 或 v1）
get_docker_compose_cmd() {
    if docker compose version &>/dev/null; then
        echo "docker compose"
    elif docker-compose --version &>/dev/null; then
        echo "docker-compose"
    else
        echo ""
    fi
}

DOCKER_COMPOSE_CMD=""

# ---------- 参数解析 ----------

PASSTHROUGH_ARGS=()
PARSING_SCRIPT_ARGS=true

for arg in "$@"; do
    if [ "$PARSING_SCRIPT_ARGS" = true ]; then
        case $arg in
            --no-pg)
                START_PG=false
                ;;
            --cleanup)
                CLEANUP=true
                ;;
            --clean-cache)
                CLEAN_CACHE=true
                ;;
            --)
                # 后续参数全部传递给 pytest
                PARSING_SCRIPT_ARGS=false
                ;;
            -h|--help)
                echo "用法: $0 [选项] [-- pytest_args...]"
                echo ""
                echo "选项:"
                echo "  --no-pg       跳过 PostgreSQL 启动（假设已有数据库）"
                echo "  --cleanup     测试后清理 Docker 容器"
                echo "  --clean-cache 运行前清理 Python 缓存（__pycache__、egg-info）"
                echo "  -h, --help    显示帮助信息"
                echo ""
                echo "环境变量:"
                echo "  PYTEST_ARGS   额外 pytest 参数（例如 '--junitxml=report.xml --durations=20'）"
                echo ""
                echo "示例:"
                echo "  $0 --cleanup                            # 测试后清理容器"
                echo "  $0 -- -v -x                             # 传递 -v -x 给 pytest"
                echo "  PYTEST_ARGS='--durations=10' $0         # 通过环境变量传递参数"
                exit 0
                ;;
            *)
                ;;
        esac
    else
        # -- 之后的参数收集到数组
        PASSTHROUGH_ARGS+=("$arg")
    fi
done

# 合并环境变量和命令行传递的 pytest 参数
if [ ${#PASSTHROUGH_ARGS[@]} -gt 0 ]; then
    EXTRA_PYTEST_ARGS="$EXTRA_PYTEST_ARGS ${PASSTHROUGH_ARGS[*]}"
fi

# ---------- 工具函数 ----------

log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

# 检查 PostgreSQL 是否就绪
check_pg_ready() {
    # 尝试使用 pg_isready（如果可用）
    if command -v pg_isready &>/dev/null; then
        PGPASSWORD="$PG_PASSWORD" pg_isready -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -q 2>/dev/null
        return $?
    fi
    
    # 备用：使用 Python psycopg 检查连接
    python3 -c "
import sys
try:
    import psycopg
    conn = psycopg.connect('$TEST_PG_DSN', connect_timeout=3)
    conn.close()
    sys.exit(0)
except:
    sys.exit(1)
" 2>/dev/null
}

# 等待 PostgreSQL 就绪
wait_for_pg() {
    local max_attempts=30
    local attempt=0
    
    log_info "等待 PostgreSQL 就绪..."
    
    while [ $attempt -lt $max_attempts ]; do
        if check_pg_ready; then
            log_info "PostgreSQL 已就绪"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    
    log_error "PostgreSQL 在 ${max_attempts} 秒内未就绪"
    return 1
}

# 启动 PostgreSQL（使用 docker compose）
start_pg() {
    log_info "使用 Docker Compose 启动 PostgreSQL（$COMPOSE_FILE）..."
    
    # 检测 docker compose 命令
    DOCKER_COMPOSE_CMD=$(get_docker_compose_cmd)
    if [ -z "$DOCKER_COMPOSE_CMD" ]; then
        log_error "未找到 docker compose 或 docker-compose 命令"
        log_error "请安装 Docker Desktop 并启用 WSL 集成，或安装 docker-compose"
        return 1
    fi
    
    cd "$PROJECT_DIR"
    
    # 检查容器是否已在运行
    if $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" ps --services --filter "status=running" 2>/dev/null | grep -q "postgres"; then
        log_info "PostgreSQL 容器已在运行"
    else
        # 启动容器
        $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" up -d postgres
    fi
    
    # 等待就绪
    wait_for_pg
}

# 清理 Docker 容器
cleanup_pg() {
    log_info "清理 PostgreSQL 容器..."
    DOCKER_COMPOSE_CMD=$(get_docker_compose_cmd)
    if [ -n "$DOCKER_COMPOSE_CMD" ]; then
        cd "$PROJECT_DIR"
        $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    fi
}

# 清理 Python 缓存目录
clean_python_cache() {
    log_info "清理 Python 缓存目录..."
    
    # 删除指定的缓存目录
    rm -rf "$SCRIPTS_DIR/__pycache__/" \
        "$SCRIPTS_DIR/tests/__pycache__/" \
        "$SCRIPTS_DIR/engram_step1/__pycache__/" \
        "$SCRIPTS_DIR/engram_step1.egg-info/" \
        2>/dev/null || true
    
    # 递归清理所有 __pycache__ 和 egg-info 目录
    find "$SCRIPTS_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$SCRIPTS_DIR" -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
    
    log_info "Python 缓存清理完成"
}

# ---------- 主流程 ----------

main() {
    local exit_code=0
    
    # 0. 清理缓存（如果需要）
    if [ "$CLEAN_CACHE" = true ]; then
        clean_python_cache
    fi
    
    # 1. 启动 PostgreSQL（如果需要）
    if [ "$START_PG" = true ]; then
        # 先检查是否已有 PG 可用
        if check_pg_ready; then
            log_info "PostgreSQL 已可用，跳过启动"
        else
            start_pg
        fi
    fi
    
    # 2. 导出环境变量
    export TEST_PG_DSN
    export TEST_PG_ADMIN_DSN
    
    log_info "TEST_PG_DSN=$TEST_PG_DSN"
    log_info "TEST_PG_ADMIN_DSN=$TEST_PG_ADMIN_DSN"
    
    # 3. 切换到 scripts 目录
    cd "$SCRIPTS_DIR"
    
    # 4. 安装依赖（如果 requirements.txt 存在且未安装）
    if [ -f "requirements.txt" ]; then
        log_info "检查/安装 Python 依赖..."
        pip install -q -r requirements.txt 2>/dev/null || pip install -r requirements.txt
    fi
    
    # 5. 运行 pytest
    log_info "运行 pytest..."
    
    # 明确列出所有测试文件，确保新增测试被包含
    # 核心测试模块:
    # - test_gitlab_commits_windowed_incremental.py: GitLab commits 窗口化增量同步（含高活跃不漏测试）
    # - test_scm_sync_degradation_policy.py: DegradationController 降级策略（含连续 429/timeout 测试）
    # - test_strict_mode_cursor_advance.py: strict/best_effort 游标推进差异
    # - test_svn_patch_degradation_batch.py: SVN patch 阶段连续超时暂停
    
    # 创建测试结果目录（默认位置）
    ARTIFACTS_DIR="$PROJECT_DIR/.artifacts/test-results"
    mkdir -p "$ARTIFACTS_DIR"
    
    # 构建 pytest 命令
    # 基础参数：安静模式 + 测试目录
    PYTEST_CMD="pytest -q tests/"
    
    # 默认 junitxml 和 durations（如果 EXTRA_PYTEST_ARGS 中未指定）
    if [[ "$EXTRA_PYTEST_ARGS" != *"--junitxml"* ]]; then
        PYTEST_CMD="$PYTEST_CMD --junitxml=$ARTIFACTS_DIR/step1-ci.xml"
    fi
    if [[ "$EXTRA_PYTEST_ARGS" != *"--durations"* ]]; then
        PYTEST_CMD="$PYTEST_CMD --durations=20"
    fi
    
    # 添加额外参数
    if [ -n "$EXTRA_PYTEST_ARGS" ]; then
        PYTEST_CMD="$PYTEST_CMD $EXTRA_PYTEST_ARGS"
        log_info "额外 pytest 参数: $EXTRA_PYTEST_ARGS"
    fi
    
    log_info "执行命令: $PYTEST_CMD"
    
    if eval "$PYTEST_CMD"; then
        log_info "测试通过"
        exit_code=0
    else
        log_error "测试失败"
        exit_code=1
    fi
    
    # 6. 清理（如果需要）
    if [ "$CLEANUP" = true ]; then
        cleanup_pg
    fi
    
    return $exit_code
}

# 捕获退出信号，确保清理
trap 'if [ "$CLEANUP" = true ]; then cleanup_pg; fi' EXIT

# 执行主流程
main "$@"
exit $?
