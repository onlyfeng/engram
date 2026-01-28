#!/bin/bash
# ==============================================================================
# verify_unified_stack.sh - Unified Stack 集成验证脚本
#
# 验证 Gateway + OpenMemory + PostgreSQL 服务栈是否正常工作
#
# 功能:
#   1. 检查各服务健康端点
#   2. 调用 Gateway 的 memory_store / memory_query
#   3. （可选）验证降级：停止 OpenMemory，写入 outbox，恢复后 flush
#
# 使用方法:
#   ./verify_unified_stack.sh                    # 基础验证（自动判断模式）
#   ./verify_unified_stack.sh --mode stepwise    # stepwise 模式（仅 HTTP 验证）
#   ./verify_unified_stack.sh --mode default     # default 模式（完整 compose 验证）
#   ./verify_unified_stack.sh --full             # 完整验证（含降级测试）
#   ./verify_unified_stack.sh --help             # 显示帮助
#
# 模式说明:
#   default   - 依赖 Docker Compose，支持完整降级测试
#   stepwise  - 纯 HTTP 验证，不依赖 Docker 容器操作，适用于分步部署或 CI
#   auto      - 自动判断（默认）：有 Docker 且容器可达时用 default，否则用 stepwise
#
# 环境变量:
#   GATEWAY_URL             Gateway 服务地址 (默认: http://localhost:8787)
#   OPENMEMORY_URL          OpenMemory 服务地址 (默认: http://localhost:8080)
#   POSTGRES_DSN            PostgreSQL 连接字符串 (可选，用于降级测试和 DB 验证)
#   COMPOSE_PROJECT_NAME    Docker Compose 项目名 (推荐，用于动态获取容器)
#   COMPOSE_FILE            Docker Compose 文件路径 (可选，默认 docker-compose.unified.yml)
#   OPENMEMORY_CONTAINER_NAME  OpenMemory 容器名 (回退方案，当无法通过 compose 获取时使用)
#
# 退出码:
#   0 - 所有验证通过
#   1 - 验证失败
#   2 - 参数错误
#
# ==============================================================================

set -euo pipefail

# ======================== 配置 ========================

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8787}"

# JSON 输出配置
JSON_OUT_PATH="${JSON_OUT_PATH:-}"

# JSON 结果存储（步骤结果数组）
declare -a JSON_STEPS=()
SCRIPT_START_TIME=""
SCRIPT_END_TIME=""
# 默认端口 8080 对应 docker-compose.unified.yml 中的 OM_PORT
OPENMEMORY_URL="${OPENMEMORY_URL:-http://localhost:8080}"
POSTGRES_DSN="${POSTGRES_DSN:-}"

# 模式: default / stepwise / auto
# default   - 依赖 Docker Compose，支持完整降级测试
# stepwise  - 纯 HTTP 验证，不依赖 Docker 容器操作
# auto      - 自动判断（默认）
VERIFY_MODE="${VERIFY_MODE:-auto}"

# Compose 配置（用于多项目并行支持）
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.unified.yml}"

# OpenMemory 容器名：动态获取或回退到显式指定
# 优先级: 1. docker compose 动态查询  2. OPENMEMORY_CONTAINER_NAME 环境变量
OPENMEMORY_CONTAINER=""

# MinIO 配置（可选）
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_BUCKET="${MINIO_BUCKET:-engram}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ======================== 辅助函数 ========================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[FAIL]${NC} $1"
}

log_step() {
    echo -e "\n${BLUE}======== $1 ========${NC}\n"
}

# ======================== JSON 记录函数 ========================

# 记录步骤开始时间
step_start_time() {
    date +%s%N
}

# 计算步骤耗时（毫秒）
step_duration_ms() {
    local start_ns="$1"
    local end_ns=$(date +%s%N)
    echo $(( (end_ns - start_ns) / 1000000 ))
}

# 记录步骤结果到 JSON 数组
# 参数: step_name status duration_ms [extra_json_fields...]
record_step_result() {
    local step_name="$1"
    local status="$2"
    local duration_ms="$3"
    shift 3
    
    # 构建额外字段 JSON
    local extra_fields=""
    while [ $# -gt 0 ]; do
        if [ -n "$extra_fields" ]; then
            extra_fields="$extra_fields, "
        fi
        extra_fields="$extra_fields$1"
        shift
    done
    
    # 构建步骤 JSON
    local step_json="{\"name\": \"$step_name\", \"status\": \"$status\", \"duration_ms\": $duration_ms"
    if [ -n "$extra_fields" ]; then
        step_json="$step_json, $extra_fields"
    fi
    step_json="$step_json}"
    
    JSON_STEPS+=("$step_json")
}

# 写入汇总 JSON 文件
write_summary_json() {
    if [ -z "$JSON_OUT_PATH" ]; then
        return 0
    fi
    
    local overall_status="$1"
    local total_failed="$2"
    
    # 计算总耗时
    SCRIPT_END_TIME=$(date +%s%N)
    local total_duration_ms=$(( (SCRIPT_END_TIME - SCRIPT_START_TIME) / 1000000 ))
    
    # 构建步骤数组 JSON
    local steps_json="["
    local first=true
    for step in "${JSON_STEPS[@]}"; do
        if [ "$first" = true ]; then
            first=false
        else
            steps_json="$steps_json, "
        fi
        steps_json="$steps_json$step"
    done
    steps_json="$steps_json]"
    
    # 构建完整 JSON
    local summary_json
    summary_json=$(cat <<EOF
{
    "verify_mode": "$VERIFY_MODE",
    "overall_status": "$overall_status",
    "total_failed": $total_failed,
    "total_duration_ms": $total_duration_ms,
    "gateway_url": "$GATEWAY_URL",
    "openmemory_url": "$OPENMEMORY_URL",
    "timestamp": "$(date -Iseconds)",
    "steps": $steps_json
}
EOF
)
    
    # 确保输出目录存在
    local out_dir
    out_dir=$(dirname "$JSON_OUT_PATH")
    if [ -n "$out_dir" ] && [ "$out_dir" != "." ]; then
        mkdir -p "$out_dir"
    fi
    
    # 写入文件
    echo "$summary_json" > "$JSON_OUT_PATH"
    log_info "验证结果已写入: $JSON_OUT_PATH"
}

show_help() {
    cat << EOF
Unified Stack 集成验证脚本

使用方法:
    $0 [选项]

选项:
    --mode MODE     验证模式: default / stepwise / auto（默认 auto）
                    - default:  依赖 Docker Compose，支持完整降级测试
                    - stepwise: 纯 HTTP 验证，不依赖 Docker 容器操作
                    - auto:     自动判断（有 Docker 且容器可达时用 default，否则用 stepwise）
    --full          执行完整验证（含降级测试，需要 Docker 权限和 POSTGRES_DSN，仅 default 模式）
    --skip-degrade  跳过降级测试
    --skip-jsonrpc  跳过 JSON-RPC 2.0 协议验证
    --json-out PATH 输出验证结果 JSON 文件路径（默认不输出）
    --help, -h      显示此帮助信息

环境变量:
    GATEWAY_URL                 Gateway 服务地址 (默认: http://localhost:8787)
    OPENMEMORY_URL              OpenMemory 服务地址 (默认: http://localhost:8080)
    POSTGRES_DSN                PostgreSQL 连接字符串 (可选，用于降级测试和 DB 验证)
    VERIFY_MODE                 验证模式 (可通过 --mode 参数覆盖)
    COMPOSE_PROJECT_NAME        Docker Compose 项目名 (推荐，用于动态获取容器)
    COMPOSE_FILE                Docker Compose 文件路径 (默认: docker-compose.unified.yml)
    OPENMEMORY_CONTAINER_NAME   OpenMemory 容器名 (回退方案，当无法通过 compose 获取时使用)
    MINIO_ENDPOINT              MinIO 端点地址 (默认: http://localhost:9000)
    MINIO_BUCKET                MinIO bucket 名称 (默认: engram)
    MINIO_ROOT_USER             MinIO 用户名 (可选，用于 bucket 检查)
    MINIO_ROOT_PASSWORD         MinIO 密码 (可选，用于 bucket 检查)

示例:
    # 基础验证（自动判断模式）
    $0

    # stepwise 模式（仅 HTTP 验证，适用于分步部署或 CI）
    $0 --mode stepwise

    # default 模式完整验证（含降级测试）
    COMPOSE_PROJECT_NAME=proj_a POSTGRES_DSN="postgresql://user:pass@localhost:5432/proj_a" $0 --mode default --full

    # 多项目并行部署时验证
    COMPOSE_PROJECT_NAME=proj_a COMPOSE_FILE=docker-compose.unified.yml $0 --full
    COMPOSE_PROJECT_NAME=proj_b COMPOSE_FILE=docker-compose.unified.yml $0 --full

    # 自定义 Gateway 地址
    GATEWAY_URL="http://192.168.1.100:8787" $0

退出码:
    0 - 所有验证通过
    1 - 验证失败
    2 - 参数错误
EOF
}

# 检查命令是否存在
check_command() {
    if ! command -v "$1" &> /dev/null; then
        log_error "缺少必要工具: $1"
        return 1
    fi
}

# 自动判断验证模式
# 如果有 Docker 且容器可达，使用 default 模式；否则使用 stepwise 模式
auto_detect_mode() {
    if [ "$VERIFY_MODE" != "auto" ]; then
        # 已显式指定模式
        return
    fi
    
    log_info "自动检测验证模式..."
    
    # 检查 Docker 是否可用
    if ! command -v docker &> /dev/null; then
        VERIFY_MODE="stepwise"
        log_info "Docker 不可用，使用 stepwise 模式"
        return
    fi
    
    # 检查 Docker 是否可连接
    if ! docker info &>/dev/null; then
        VERIFY_MODE="stepwise"
        log_info "Docker 守护进程不可连接，使用 stepwise 模式"
        return
    fi
    
    # 检查是否有 compose 项目或容器可达
    local compose_args=""
    if [ -n "$COMPOSE_PROJECT_NAME" ]; then
        compose_args="-p $COMPOSE_PROJECT_NAME"
    fi
    if [ -n "$COMPOSE_FILE" ] && [ -f "$COMPOSE_FILE" ]; then
        compose_args="$compose_args -f $COMPOSE_FILE"
    fi
    
    if [ -n "$compose_args" ]; then
        local container_count
        container_count=$(docker compose $compose_args ps -q 2>/dev/null | wc -l) || container_count=0
        
        if [ "$container_count" -gt 0 ]; then
            VERIFY_MODE="default"
            log_info "检测到 $container_count 个 compose 容器，使用 default 模式"
            return
        fi
    fi
    
    # 回退到 stepwise
    VERIFY_MODE="stepwise"
    log_info "未检测到活动容器，使用 stepwise 模式"
}

# 是否为 stepwise 模式
is_stepwise_mode() {
    [ "$VERIFY_MODE" = "stepwise" ]
}

# stepwise 模式：DSN 基础验证（不依赖 Docker）
verify_postgres_dsn() {
    log_step "PostgreSQL DSN 基础验证"
    
    if [ -z "$POSTGRES_DSN" ]; then
        log_warn "未设置 POSTGRES_DSN，跳过数据库验证"
        return 0
    fi
    
    log_info "验证 PostgreSQL 连接: ${POSTGRES_DSN%%@*}@..."
    
    # 检查是否安装了 psql
    if command -v psql &> /dev/null; then
        # 使用 psql 验证连接
        if psql "$POSTGRES_DSN" -c "SELECT 1" &>/dev/null; then
            log_success "PostgreSQL 连接成功"
            
            # 检查 openmemory schema 是否存在
            local schema_check
            schema_check=$(psql "$POSTGRES_DSN" -t -c "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'openmemory'" 2>/dev/null | tr -d ' ')
            
            if [ "$schema_check" = "openmemory" ]; then
                log_success "openmemory schema 存在"
            else
                log_warn "openmemory schema 不存在（可能尚未迁移）"
            fi
            
            return 0
        else
            log_error "PostgreSQL 连接失败"
            return 1
        fi
    fi
    
    # 如果没有 psql，尝试用 Python
    if command -v python &> /dev/null || command -v python3 &> /dev/null; then
        local python_cmd
        python_cmd=$(command -v python3 || command -v python)
        
        if $python_cmd -c "
import sys
try:
    import psycopg
    conn = psycopg.connect('$POSTGRES_DSN')
    conn.execute('SELECT 1')
    conn.close()
    sys.exit(0)
except ImportError:
    try:
        import psycopg2
        conn = psycopg2.connect('$POSTGRES_DSN')
        cur = conn.cursor()
        cur.execute('SELECT 1')
        conn.close()
        sys.exit(0)
    except ImportError:
        print('需要安装 psycopg 或 psycopg2')
        sys.exit(2)
except Exception as e:
    print(str(e))
    sys.exit(1)
" 2>/dev/null; then
            log_success "PostgreSQL 连接成功（通过 Python）"
            return 0
        else
            local exit_code=$?
            if [ $exit_code -eq 2 ]; then
                log_warn "无法验证 PostgreSQL（缺少 psycopg/psycopg2）"
                return 0
            else
                log_error "PostgreSQL 连接失败"
                return 1
            fi
        fi
    fi
    
    log_warn "无法验证 PostgreSQL（缺少 psql 和 Python）"
    return 0
}

# 动态获取 OpenMemory 容器名
# 优先级:
#   1. docker compose -p $COMPOSE_PROJECT_NAME ps -q openmemory
#   2. OPENMEMORY_CONTAINER_NAME 环境变量
#   3. 返回空（跳过需要容器的测试）
resolve_openmemory_container() {
    # 如果已经解析过，直接返回
    if [ -n "$OPENMEMORY_CONTAINER" ]; then
        return 0
    fi
    
    # 方式 1: 通过 docker compose 动态获取
    if command -v docker &> /dev/null; then
        local compose_args=""
        
        # 构建 compose 命令参数
        if [ -n "$COMPOSE_PROJECT_NAME" ]; then
            compose_args="-p $COMPOSE_PROJECT_NAME"
        fi
        if [ -n "$COMPOSE_FILE" ] && [ -f "$COMPOSE_FILE" ]; then
            compose_args="$compose_args -f $COMPOSE_FILE"
        fi
        
        if [ -n "$compose_args" ]; then
            log_info "尝试通过 docker compose 获取容器..."
            local container_id
            container_id=$(docker compose $compose_args ps -q openmemory 2>/dev/null | head -n1) || true
            
            if [ -n "$container_id" ]; then
                # 获取容器名称（而非 ID，便于日志可读）
                OPENMEMORY_CONTAINER=$(docker inspect --format '{{.Name}}' "$container_id" 2>/dev/null | sed 's/^\///')
                if [ -n "$OPENMEMORY_CONTAINER" ]; then
                    log_success "动态获取容器名: $OPENMEMORY_CONTAINER"
                    return 0
                fi
            fi
        fi
    fi
    
    # 方式 2: 回退到显式指定的环境变量
    if [ -n "${OPENMEMORY_CONTAINER_NAME:-}" ]; then
        OPENMEMORY_CONTAINER="$OPENMEMORY_CONTAINER_NAME"
        log_info "使用显式指定的容器名: $OPENMEMORY_CONTAINER"
        return 0
    fi
    
    # 无法获取容器名
    log_warn "无法获取 OpenMemory 容器名"
    log_warn "请设置 COMPOSE_PROJECT_NAME 或 OPENMEMORY_CONTAINER_NAME 环境变量"
    return 1
}

# HTTP 健康检查
check_health() {
    local url="$1"
    local service="$2"
    local timeout="${3:-5}"
    
    log_info "检查 $service 健康状态: $url"
    
    local start_time=$(date +%s%N)
    local http_code
    
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$timeout" "$url" 2>/dev/null) || true
    
    local end_time=$(date +%s%N)
    local elapsed_ms=$(( (end_time - start_time) / 1000000 ))
    
    if [ "$http_code" = "200" ]; then
        log_success "$service 健康 (HTTP $http_code, ${elapsed_ms}ms)"
        return 0
    else
        log_error "$service 不健康 (HTTP $http_code)"
        return 1
    fi
}

# 调用 MCP 工具（旧格式）
call_mcp_tool() {
    local tool="$1"
    local arguments="$2"
    
    local payload=$(cat <<EOF
{"tool": "$tool", "arguments": $arguments}
EOF
)
    
    local response
    response=$(curl -s -X POST \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 30 \
        "$GATEWAY_URL/mcp") || {
        log_error "MCP 调用失败: $tool"
        return 1
    }
    
    echo "$response"
}

# 调用 JSON-RPC 2.0 方法
call_jsonrpc() {
    local method="$1"
    local params="$2"
    local req_id="${3:-1}"
    
    local payload=$(cat <<EOF
{"jsonrpc": "2.0", "id": $req_id, "method": "$method", "params": $params}
EOF
)
    
    local response
    response=$(curl -s -X POST \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 30 \
        "$GATEWAY_URL/mcp") || {
        log_error "JSON-RPC 调用失败: $method"
        return 1
    }
    
    echo "$response"
}

# 检查 JSON-RPC 响应是否成功
check_jsonrpc_response() {
    local response="$1"
    local operation="$2"
    
    # 检查是否有 error 字段
    local has_error=$(echo "$response" | jq 'has("error")')
    
    if [ "$has_error" = "false" ]; then
        log_success "$operation 成功"
        return 0
    else
        local error_code=$(echo "$response" | jq -r '.error.code // "unknown"')
        local error_msg=$(echo "$response" | jq -r '.error.message // "unknown error"')
        log_error "$operation 失败: [$error_code] $error_msg"
        return 1
    fi
}

# 检查 MCP 响应是否成功
check_mcp_response() {
    local response="$1"
    local operation="$2"
    
    local ok=$(echo "$response" | jq -r '.ok // false')
    
    if [ "$ok" = "true" ]; then
        log_success "$operation 成功"
        return 0
    else
        local error=$(echo "$response" | jq -r '.error // .result.message // "unknown error"')
        log_error "$operation 失败: $error"
        return 1
    fi
}

# ======================== 验证函数 ========================

# 检查 MinIO 可用性（可选）
verify_minio_availability() {
    log_step "MinIO 可用性检查（可选）"
    
    local minio_endpoint="${MINIO_ENDPOINT:-http://localhost:9000}"
    local minio_bucket="${MINIO_BUCKET:-engram}"
    
    log_info "检查 MinIO 端点: $minio_endpoint"
    
    # 检查 MinIO 健康端点
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$minio_endpoint/minio/health/live" 2>/dev/null) || true
    
    if [ "$http_code" = "200" ]; then
        log_success "MinIO 服务可用 (HTTP $http_code)"
        
        # 如果提供了凭证，检查 bucket 是否存在
        if [ -n "${MINIO_ROOT_USER:-}" ] && [ -n "${MINIO_ROOT_PASSWORD:-}" ]; then
            log_info "检查 bucket 可用性: $minio_bucket"
            
            # 使用 AWS S3 API 检查 bucket（HEAD 请求）
            local bucket_check
            bucket_check=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
                -u "${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}" \
                "$minio_endpoint/$minio_bucket" 2>/dev/null) || true
            
            if [ "$bucket_check" = "200" ] || [ "$bucket_check" = "403" ]; then
                # 403 表示 bucket 存在但需要认证
                log_success "Bucket '$minio_bucket' 存在"
            elif [ "$bucket_check" = "404" ]; then
                log_warn "Bucket '$minio_bucket' 不存在，请确保 minio_init 已运行"
            else
                log_warn "无法确认 bucket 状态 (HTTP $bucket_check)"
            fi
        else
            log_info "未提供 MinIO 凭证，跳过 bucket 检查"
        fi
        
        return 0
    else
        log_warn "MinIO 不可用或未启用 (HTTP $http_code)，跳过相关检查"
        return 0  # MinIO 是可选的，不影响整体验证
    fi
}

# 步骤 1: 服务健康检查
verify_health_checks() {
    log_step "步骤 1: 服务健康检查"
    
    local start_time=$(step_start_time)
    local failed=0
    local gateway_ok=false
    local openmemory_ok=false
    
    if check_health "$GATEWAY_URL/health" "Gateway"; then
        gateway_ok=true
    else
        ((failed++))
    fi
    
    if check_health "$OPENMEMORY_URL/health" "OpenMemory"; then
        openmemory_ok=true
    else
        ((failed++))
    fi
    
    local duration=$(step_duration_ms "$start_time")
    
    if [ "$failed" -gt 0 ]; then
        log_error "健康检查失败: $failed 个服务不健康"
        record_step_result "health_checks" "fail" "$duration" \
            "\"gateway_ok\": $gateway_ok" "\"openmemory_ok\": $openmemory_ok"
        return 1
    fi
    
    log_success "所有服务健康检查通过"
    record_step_result "health_checks" "ok" "$duration" \
        "\"gateway_ok\": $gateway_ok" "\"openmemory_ok\": $openmemory_ok"
    return 0
}

# 步骤 2: memory_store 测试
verify_memory_store() {
    log_step "步骤 2: memory_store 功能测试"
    
    local start_time=$(step_start_time)
    local unique_id=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N | md5sum | head -c 8)
    unique_id="${unique_id:0:8}"
    
    local test_content="# 验证脚本测试 ${unique_id}

这是由 verify_unified_stack.sh 创建的测试记忆。
创建时间: $(date -Iseconds)"

    # 转义特殊字符用于 JSON
    local escaped_content=$(echo "$test_content" | jq -Rs .)
    
    local arguments=$(cat <<EOF
{
    "payload_md": $escaped_content,
    "target_space": "team:verification_test",
    "actor_user_id": "verify_script"
}
EOF
)
    
    log_info "发送 memory_store 请求..."
    local response
    if ! response=$(call_mcp_tool "memory_store" "$arguments"); then
        local duration=$(step_duration_ms "$start_time")
        record_step_result "memory_store" "fail" "$duration" "\"error\": \"request_failed\""
        return 1
    fi
    
    # 检查响应
    if ! check_mcp_response "$response" "memory_store"; then
        local duration=$(step_duration_ms "$start_time")
        record_step_result "memory_store" "fail" "$duration" "\"error\": \"response_check_failed\""
        return 1
    fi
    
    # 额外检查：result.ok
    local result_ok=$(echo "$response" | jq -r '.result.ok // false')
    local result_action=$(echo "$response" | jq -r '.result.action // "unknown"')
    local has_outbox_id=false
    local memory_id=""
    
    if [ "$result_ok" != "true" ]; then
        local result_message=$(echo "$response" | jq -r '.result.message // ""')
        
        # action=error 但有 outbox_id 表示降级成功
        if [ "$result_action" = "error" ] && echo "$result_message" | grep -q "outbox_id"; then
            log_warn "memory_store 降级到 outbox: $result_message"
            has_outbox_id=true
        else
            log_error "memory_store result.ok=false: action=$result_action, message=$result_message"
            local duration=$(step_duration_ms "$start_time")
            record_step_result "memory_store" "fail" "$duration" \
                "\"action\": \"$result_action\"" "\"has_outbox_id\": false"
            return 1
        fi
    else
        memory_id=$(echo "$response" | jq -r '.result.memory_id // "N/A"')
        log_info "memory_id: $memory_id"
    fi
    
    local duration=$(step_duration_ms "$start_time")
    record_step_result "memory_store" "ok" "$duration" \
        "\"action\": \"$result_action\"" "\"has_outbox_id\": $has_outbox_id" \
        "\"memory_id\": \"$memory_id\""
    return 0
}

# 步骤 3: memory_query 测试
verify_memory_query() {
    log_step "步骤 3: memory_query 功能测试"
    
    local start_time=$(step_start_time)
    local arguments=$(cat <<EOF
{
    "query": "验证脚本测试",
    "spaces": ["team:verification_test"],
    "top_k": 5
}
EOF
)
    
    log_info "发送 memory_query 请求..."
    local response
    if ! response=$(call_mcp_tool "memory_query" "$arguments"); then
        local duration=$(step_duration_ms "$start_time")
        record_step_result "memory_query" "fail" "$duration" "\"error\": \"request_failed\""
        return 1
    fi
    
    if ! check_mcp_response "$response" "memory_query"; then
        local duration=$(step_duration_ms "$start_time")
        record_step_result "memory_query" "fail" "$duration" "\"error\": \"response_check_failed\""
        return 1
    fi
    
    local total=$(echo "$response" | jq -r '.result.total // 0')
    log_info "查询返回 $total 条结果"
    
    local duration=$(step_duration_ms "$start_time")
    record_step_result "memory_query" "ok" "$duration" "\"results_count\": $total"
    return 0
}

# 步骤 3.5: JSON-RPC 2.0 协议测试
verify_jsonrpc() {
    log_step "步骤 3.5: JSON-RPC 2.0 协议测试"
    
    local start_time=$(step_start_time)
    local failed=0
    local tools_count=0
    
    # 3.5.1: tools/list 测试
    log_info "发送 tools/list 请求..."
    local list_response
    list_response=$(call_jsonrpc "tools/list" "{}" "1") || ((failed++))
    
    if [ $failed -eq 0 ]; then
        check_jsonrpc_response "$list_response" "tools/list" || ((failed++))
        
        if [ $failed -eq 0 ]; then
            tools_count=$(echo "$list_response" | jq -r '.result.tools | length // 0')
            log_info "tools/list 返回 $tools_count 个工具"
            
            # 验证必需的工具存在
            local has_memory_store=$(echo "$list_response" | jq '.result.tools[] | select(.name == "memory_store") | .name' | head -1)
            local has_memory_query=$(echo "$list_response" | jq '.result.tools[] | select(.name == "memory_query") | .name' | head -1)
            local has_reliability=$(echo "$list_response" | jq '.result.tools[] | select(.name == "reliability_report") | .name' | head -1)
            local has_governance=$(echo "$list_response" | jq '.result.tools[] | select(.name == "governance_update") | .name' | head -1)
            
            if [ -n "$has_memory_store" ] && [ -n "$has_memory_query" ] && [ -n "$has_reliability" ] && [ -n "$has_governance" ]; then
                log_success "所有必需工具已注册"
            else
                log_error "部分工具未注册"
                ((failed++))
            fi
        fi
    fi
    
    # 3.5.2: tools/call 测试（调用 reliability_report）
    log_info "发送 tools/call (reliability_report) 请求..."
    local call_params=$(cat <<EOF
{
    "name": "reliability_report",
    "arguments": {}
}
EOF
)
    local call_response
    call_response=$(call_jsonrpc "tools/call" "$call_params" "2") || ((failed++))
    
    if [ $? -eq 0 ]; then
        check_jsonrpc_response "$call_response" "tools/call (reliability_report)" || ((failed++))
        
        if [ $? -eq 0 ]; then
            # 验证返回的 content 格式
            local content_type=$(echo "$call_response" | jq -r '.result.content[0].type // ""')
            if [ "$content_type" = "text" ]; then
                log_success "tools/call 返回格式正确 (content[].type=text)"
            else
                log_warn "tools/call 返回格式异常: content[0].type=$content_type"
            fi
        fi
    fi
    
    # 3.5.3: 错误处理测试（调用不存在的工具）
    log_info "测试 JSON-RPC 错误处理..."
    local error_params=$(cat <<EOF
{
    "name": "nonexistent_tool",
    "arguments": {}
}
EOF
)
    local error_response
    error_response=$(call_jsonrpc "tools/call" "$error_params" "3") || true
    
    local error_code=$(echo "$error_response" | jq -r '.error.code // 0')
    if [ "$error_code" = "-32601" ]; then
        log_success "错误处理正确: 返回 METHOD_NOT_FOUND (-32601)"
    elif [ "$error_code" != "0" ]; then
        log_success "错误处理正确: 返回错误码 $error_code"
    else
        log_warn "错误处理可能异常: 未返回预期的错误码"
    fi
    
    local duration=$(step_duration_ms "$start_time")
    
    if [ $failed -gt 0 ]; then
        log_error "JSON-RPC 验证失败: $failed 项测试未通过"
        record_step_result "jsonrpc" "fail" "$duration" "\"tools_count\": $tools_count"
        return 1
    fi
    
    log_success "JSON-RPC 2.0 协议测试通过"
    record_step_result "jsonrpc" "ok" "$duration" "\"tools_count\": $tools_count"
    return 0
}

# 步骤 4: 降级测试（可选）
verify_degradation() {
    log_step "步骤 4: 降级测试"
    
    local start_time=$(step_start_time)
    local has_outbox_id=false
    
    # 检查前置条件
    if [ -z "$POSTGRES_DSN" ]; then
        log_warn "未设置 POSTGRES_DSN，跳过降级测试"
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "skipped" "$duration" "\"reason\": \"no_postgres_dsn\""
        return 0
    fi
    
    if ! command -v docker &> /dev/null; then
        log_warn "未安装 Docker，跳过降级测试"
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "skipped" "$duration" "\"reason\": \"no_docker\""
        return 0
    fi
    
    if ! command -v psql &> /dev/null; then
        log_warn "未安装 psql，跳过降级测试的数据库验证"
    fi
    
    # 动态获取容器名
    if ! resolve_openmemory_container; then
        log_warn "无法获取 OpenMemory 容器名，跳过降级测试"
        log_warn "提示: 设置 COMPOSE_PROJECT_NAME 或 OPENMEMORY_CONTAINER_NAME"
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "skipped" "$duration" "\"reason\": \"no_container\""
        return 0
    fi
    
    local unique_id=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N | md5sum | head -c 8)
    unique_id="${unique_id:0:8}"
    
    # 4.1 停止 OpenMemory 容器
    log_info "停止 OpenMemory 容器: $OPENMEMORY_CONTAINER"
    if ! docker stop "$OPENMEMORY_CONTAINER" &>/dev/null; then
        log_warn "无法停止容器 $OPENMEMORY_CONTAINER，跳过降级测试"
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "skipped" "$duration" "\"reason\": \"cannot_stop_container\""
        return 0
    fi
    
    # 确保测试结束后重启容器
    trap 'docker start "$OPENMEMORY_CONTAINER" &>/dev/null || true' EXIT
    
    sleep 3
    
    # 验证 OpenMemory 已停止
    if check_health "$OPENMEMORY_URL/health" "OpenMemory" 2>/dev/null; then
        log_error "OpenMemory 应该已停止但仍在响应"
        docker start "$OPENMEMORY_CONTAINER" &>/dev/null || true
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "fail" "$duration" "\"error\": \"container_still_running\""
        return 1
    fi
    log_info "确认 OpenMemory 已停止"
    
    # 4.2 发送 memory_store 请求（应该降级到 outbox）
    local test_content="# 降级测试 ${unique_id}

这条记忆应该被写入 outbox 而不是 OpenMemory。"

    local escaped_content=$(echo "$test_content" | jq -Rs .)
    
    local arguments=$(cat <<EOF
{
    "payload_md": $escaped_content,
    "target_space": "team:degradation_test",
    "actor_user_id": "degrade_tester"
}
EOF
)
    
    log_info "发送 memory_store 请求（预期降级到 outbox）..."
    local response
    response=$(call_mcp_tool "memory_store" "$arguments") || {
        log_error "降级测试请求失败"
        docker start "$OPENMEMORY_CONTAINER" &>/dev/null || true
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "fail" "$duration" "\"error\": \"request_failed\""
        return 1
    }
    
    # 检查是否成功降级到 outbox
    local result_message=$(echo "$response" | jq -r '.result.message // ""')
    if echo "$result_message" | grep -q "outbox_id"; then
        log_success "成功降级到 outbox: $result_message"
        has_outbox_id=true
    else
        log_warn "降级行为不符合预期: $result_message"
    fi
    
    # 4.3 重启 OpenMemory 容器
    log_info "重启 OpenMemory 容器..."
    docker start "$OPENMEMORY_CONTAINER" &>/dev/null || {
        log_error "无法重启容器 $OPENMEMORY_CONTAINER"
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "fail" "$duration" \
            "\"has_outbox_id\": $has_outbox_id" "\"error\": \"cannot_restart_container\""
        return 1
    }
    
    # 等待 OpenMemory 恢复
    log_info "等待 OpenMemory 恢复..."
    local max_wait=60
    local waited=0
    while [ $waited -lt $max_wait ]; do
        if check_health "$OPENMEMORY_URL/health" "OpenMemory" 2>/dev/null; then
            log_success "OpenMemory 已恢复"
            break
        fi
        sleep 2
        ((waited+=2))
    done
    
    if [ $waited -ge $max_wait ]; then
        log_error "OpenMemory 未能在 ${max_wait} 秒内恢复"
        local duration=$(step_duration_ms "$start_time")
        record_step_result "degradation" "fail" "$duration" \
            "\"has_outbox_id\": $has_outbox_id" "\"error\": \"recovery_timeout\""
        return 1
    fi
    
    # 4.4 运行 outbox_worker 进行 flush
    log_info "运行 outbox_worker flush..."
    
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local gateway_dir="$script_dir/../gateway"
    
    if [ -d "$gateway_dir" ]; then
        cd "$gateway_dir"
        # 设置必要的环境变量
        export POSTGRES_DSN="$POSTGRES_DSN"
        export OPENMEMORY_BASE_URL="$OPENMEMORY_URL"
        export PROJECT_KEY="${PROJECT_KEY:-default}"
        
        python -m gateway.outbox_worker --once 2>&1 || {
            log_warn "outbox_worker 执行返回非零状态（可能有失败记录）"
        }
        log_success "outbox_worker 执行完成"
    else
        log_warn "找不到 gateway 目录，跳过 outbox_worker 执行"
    fi
    
    # 清除 trap
    trap - EXIT
    
    local duration=$(step_duration_ms "$start_time")
    record_step_result "degradation" "ok" "$duration" "\"has_outbox_id\": $has_outbox_id"
    return 0
}

# ======================== 主流程 ========================

main() {
    local full_test=false
    local skip_degrade=false
    
    # 解析参数
    while [ $# -gt 0 ]; do
        case "$1" in
            --mode)
                if [ -z "${2:-}" ]; then
                    log_error "--mode 需要指定模式: default / stepwise / auto"
                    exit 2
                fi
                case "$2" in
                    default|stepwise|auto)
                        VERIFY_MODE="$2"
                        ;;
                    *)
                        log_error "无效模式: $2（有效值: default / stepwise / auto）"
                        exit 2
                        ;;
                esac
                shift 2
                ;;
            --full)
                full_test=true
                shift
                ;;
            --skip-degrade)
                skip_degrade=true
                shift
                ;;
            --skip-jsonrpc)
                export SKIP_JSONRPC=true
                shift
                ;;
            --json-out)
                if [ -z "${2:-}" ]; then
                    log_error "--json-out 需要指定输出文件路径"
                    exit 2
                fi
                JSON_OUT_PATH="$2"
                shift 2
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                log_error "未知参数: $1"
                show_help
                exit 2
                ;;
        esac
    done
    
    # 自动检测模式（如果是 auto）
    auto_detect_mode
    
    # 记录脚本开始时间
    SCRIPT_START_TIME=$(date +%s%N)
    
    echo ""
    echo "=============================================="
    echo "   Unified Stack 集成验证"
    echo "=============================================="
    echo ""
    echo "配置:"
    echo "  验证模式:             $VERIFY_MODE"
    echo "  Gateway URL:          $GATEWAY_URL"
    echo "  OpenMemory URL:       $OPENMEMORY_URL"
    echo "  PostgreSQL DSN:       ${POSTGRES_DSN:-(未设置)}"
    if [ "$VERIFY_MODE" = "default" ]; then
        echo "  Compose Project:      ${COMPOSE_PROJECT_NAME:-(未设置，将动态检测)}"
        echo "  Compose File:         ${COMPOSE_FILE}"
    fi
    echo "  MinIO Endpoint:       $MINIO_ENDPOINT"
    echo "  MinIO Bucket:         $MINIO_BUCKET"
    echo "  完整测试模式:         $full_test"
    echo "  JSON 输出路径:        ${JSON_OUT_PATH:-(不输出)}"
    echo ""
    
    # 检查必要工具
    check_command curl || exit 1
    check_command jq || exit 1
    
    local failed=0
    
    # stepwise 模式提示
    if is_stepwise_mode; then
        log_info "stepwise 模式：仅执行 HTTP 验证，不依赖 Docker 容器操作"
        echo ""
    fi
    
    # 步骤 1: 健康检查
    verify_health_checks || ((failed++))
    
    if [ $failed -gt 0 ]; then
        log_error "健康检查失败，无法继续后续测试"
        exit 1
    fi
    
    # stepwise 模式：DSN 基础验证（可选）
    if is_stepwise_mode && [ -n "$POSTGRES_DSN" ]; then
        verify_postgres_dsn || ((failed++))
    fi
    
    # MinIO 可用性检查（可选，不影响整体验证）
    verify_minio_availability || true
    
    # 步骤 2: memory_store 测试
    verify_memory_store || ((failed++))
    
    # 步骤 3: memory_query 测试
    verify_memory_query || ((failed++))
    
    # 步骤 3.5: JSON-RPC 协议测试（可通过 --skip-jsonrpc 跳过）
    if [ "${SKIP_JSONRPC:-false}" != "true" ]; then
        verify_jsonrpc || ((failed++))
    else
        log_info "跳过 JSON-RPC 验证（SKIP_JSONRPC=true）"
    fi
    
    # 步骤 4: 降级测试（仅在 --full 模式且未跳过时执行，且不在 stepwise 模式）
    if [ "$full_test" = true ] && [ "$skip_degrade" = false ]; then
        if is_stepwise_mode; then
            log_info "stepwise 模式：跳过降级测试（需要 Docker 容器操作）"
        else
            verify_degradation || ((failed++))
        fi
    else
        log_info "跳过降级测试（使用 --full 启用）"
    fi
    
    # 汇总结果
    log_step "验证结果汇总"
    
    if [ $failed -eq 0 ]; then
        log_success "所有验证通过！"
        write_summary_json "pass" "$failed"
        echo ""
        exit 0
    else
        log_error "验证失败: $failed 项测试未通过"
        write_summary_json "fail" "$failed"
        echo ""
        exit 1
    fi
}

# 运行主流程
main "$@"
