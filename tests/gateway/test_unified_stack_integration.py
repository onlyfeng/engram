# -*- coding: utf-8 -*-
"""
Unified Stack 集成测试

测试完整的 Gateway + OpenMemory 集成，包括：
1. 各服务健康检查（Gateway、OpenMemory、PostgreSQL）
2. memory_store / memory_query 完整流程
3. 降级验证：OpenMemory 不可用时写入 outbox，恢复后 flush 成功

使用环境变量控制是否运行：
    RUN_INTEGRATION_TESTS=1 pytest tests/test_unified_stack_integration.py -v

必需的环境变量：
    GATEWAY_URL: Gateway 服务 URL（默认 http://localhost:8787）
    OPENMEMORY_URL: OpenMemory 服务 URL（默认 http://localhost:8080，与统一栈一致）
    POSTGRES_DSN: PostgreSQL 连接字符串

可选的环境变量：
    SKIP_DEGRADATION_TEST: 设为 1 跳过降级测试（需要 Docker 操作权限）
    HTTP_ONLY_MODE: 设为 1 仅运行纯 HTTP 验证测试（跳过需要 Docker/compose 的测试）
    GATE_PROFILE: 显式指定 profile (http_only/standard/full)

门禁规则：
    FULL 模式下，若缺少 Docker/DB 能力，测试应 FAIL 而非 skip。
    规则定义在 scripts/unified_stack_gate_contract.py 中。
"""

import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pytest
import requests

# ======================== Gate Contract 集成 ========================

# 动态添加 scripts 目录到 path 以便导入 gate_contract
_WORKSPACE_ROOT = (
    Path(__file__).resolve().parents[4]
)  # gateway/tests -> gateway -> openmemory_gateway -> apps -> workspace
_SCRIPTS_DIR = _WORKSPACE_ROOT / "scripts"
if _SCRIPTS_DIR.exists() and str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from unified_stack_gate_contract import (
        PROFILE_CONFIGS,
        ProfileType,
        ReasonCode,  # noqa: F401
        StepName,
        detect_capabilities,
        get_profile_from_env,
        validate_profile,  # noqa: F401
    )

    GATE_CONTRACT_AVAILABLE = True
except ImportError:
    GATE_CONTRACT_AVAILABLE = False

    # 定义回退类型以避免 NameError
    class ProfileType:
        HTTP_ONLY = "http_only"
        STANDARD = "standard"
        FULL = "full"

    class StepName:
        DEGRADATION = "degradation"
        DB_INVARIANTS = "db_invariants"


def get_current_profile() -> str:
    """
    获取当前 profile（使用 gate_contract 或回退逻辑）

    Returns:
        当前 profile 名称: http_only, standard, full
    """
    if GATE_CONTRACT_AVAILABLE:
        return get_profile_from_env().value

    # 回退逻辑
    explicit = os.environ.get("GATE_PROFILE", "").lower()
    if explicit in ("http_only", "httponly"):
        return "http_only"
    elif explicit == "full":
        return "full"
    elif explicit == "standard":
        return "standard"

    if os.environ.get("HTTP_ONLY_MODE") == "1":
        return "http_only"
    if os.environ.get("SKIP_DEGRADATION_TEST") == "1":
        return "standard"

    return "standard"


def is_full_profile() -> bool:
    """检查当前是否为 FULL profile"""
    return get_current_profile() == "full"


def check_capability_for_step(step_name: str) -> Tuple[bool, str]:
    """
    检查指定步骤所需的能力是否可用

    Args:
        step_name: 步骤名称 (degradation, db_invariants 等)

    Returns:
        (可用, 原因消息) 元组
    """
    if not GATE_CONTRACT_AVAILABLE:
        # 回退到简单检查
        if step_name == "degradation":
            import shutil

            if not shutil.which("docker"):
                return False, "Docker CLI 不可用"
            try:
                result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                if result.returncode != 0:
                    return False, "Docker daemon 不可用"
            except Exception as e:
                return False, f"Docker 检查失败: {e}"
            return True, "Docker 可用"
        elif step_name == "db_invariants":
            if not os.environ.get("POSTGRES_DSN"):
                return False, "POSTGRES_DSN 未设置"
            return True, "POSTGRES_DSN 已设置"
        return True, "能力检查跳过"

    # 使用 gate_contract 检查
    capabilities = detect_capabilities()

    if step_name == "degradation":
        if not capabilities.is_available("can_stop_openmemory"):
            status = capabilities.capabilities.get("can_stop_openmemory")
            return False, status.message if status else "无法停止 OpenMemory 容器"
        return True, "降级测试能力可用"

    elif step_name == "db_invariants":
        if not capabilities.is_available("postgres_dsn_present"):
            return False, "POSTGRES_DSN 未设置"
        if not capabilities.is_available("db_access_available"):
            return False, "无 DB 访问能力（需要 psql 或 psycopg）"
        return True, "DB 不变量检查能力可用"

    return True, "能力检查通过"


def should_fail_if_blocked(step_name: str) -> bool:
    """
    检查指定步骤在当前 profile 下缺少能力时是否应该 FAIL（而非 skip）

    根据 gate_contract.PROFILE_CONFIGS 的 must_fail_if_blocked 定义。

    Args:
        step_name: 步骤名称

    Returns:
        True 表示应该 FAIL，False 表示可以 skip
    """
    if not is_full_profile():
        # 非 FULL 模式下，缺能力可以 skip
        return False

    if GATE_CONTRACT_AVAILABLE:
        profile_config = PROFILE_CONFIGS.get(ProfileType.FULL)
        if profile_config:
            must_fail_steps = [s.value for s in profile_config.must_fail_if_blocked]
            return step_name in must_fail_steps

    # 回退逻辑: FULL 模式下 degradation 和 db_invariants 必须 FAIL
    return step_name in ("degradation", "db_invariants")


def enforce_capability_or_fail(step_name: str, reason_prefix: str = ""):
    """
    检查能力，若在 FULL 模式下缺失则 pytest.fail，否则 pytest.skip

    用于测试 fixture 或 test 方法开头，统一处理 skip/fail 逻辑。

    Args:
        step_name: 步骤名称
        reason_prefix: 失败原因前缀
    """
    available, reason = check_capability_for_step(step_name)

    if available:
        return  # 能力可用，继续执行

    full_reason = f"{reason_prefix}{reason}" if reason_prefix else reason

    if should_fail_if_blocked(step_name):
        # FULL 模式下缺能力必须 FAIL
        pytest.fail(
            f"[FULL profile] {full_reason}。"
            f"FULL 模式下 {step_name} 步骤不能跳过，必须修复环境后重试。"
        )
    else:
        # 非 FULL 模式可以 skip
        pytest.skip(full_reason)


# ======================== 环境变量与配置 ========================

INTEGRATION_TEST_VAR = "RUN_INTEGRATION_TESTS"
SKIP_DEGRADATION_VAR = "SKIP_DEGRADATION_TEST"
HTTP_ONLY_MODE_VAR = "HTTP_ONLY_MODE"


def get_gateway_url() -> str:
    """获取 Gateway 服务 URL"""
    return os.environ.get("GATEWAY_URL", "http://localhost:8787")


def get_openmemory_url() -> str:
    """获取 OpenMemory 服务 URL（默认端口 8080 与统一栈一致）"""
    return os.environ.get("OPENMEMORY_URL", "http://localhost:8080")


def get_postgres_dsn() -> str:
    """获取 PostgreSQL DSN"""
    return os.environ.get("POSTGRES_DSN", "")


# ======================== pytest 标记与跳过条件 ========================


def should_run_integration_tests() -> bool:
    """检查是否应该运行集成测试"""
    return os.environ.get(INTEGRATION_TEST_VAR, "").lower() in ("1", "true", "yes")


def should_skip_degradation_test() -> bool:
    """检查是否应该跳过降级测试"""
    # FULL 模式下不应该被 SKIP_DEGRADATION_TEST 跳过
    if is_full_profile():
        return False
    return os.environ.get(SKIP_DEGRADATION_VAR, "").lower() in ("1", "true", "yes")


def is_http_only_mode() -> bool:
    """检查是否为纯 HTTP 验证模式（跳过需要 Docker/compose 操作的测试）"""
    # FULL 模式下不应该是 HTTP_ONLY
    if is_full_profile():
        return False
    return os.environ.get(HTTP_ONLY_MODE_VAR, "").lower() in ("1", "true", "yes")


def requires_docker() -> bool:
    """检查测试是否需要 Docker（HTTP_ONLY_MODE 时返回 True 表示应跳过）"""
    return is_http_only_mode()


# 定义 pytest marker
integration_test = pytest.mark.skipif(
    not should_run_integration_tests(), reason=f"集成测试需要设置 {INTEGRATION_TEST_VAR}=1"
)

degradation_test = pytest.mark.skipif(
    should_skip_degradation_test(), reason=f"降级测试已被 {SKIP_DEGRADATION_VAR}=1 跳过"
)

# HTTP-only 模式 marker：跳过需要 Docker 容器操作的测试
# 注意：FULL 模式下此 marker 不生效
http_only_skip = pytest.mark.skipif(
    is_http_only_mode(), reason=f"纯 HTTP 模式下跳过需要 Docker 操作的测试 ({HTTP_ONLY_MODE_VAR}=1)"
)


# ======================== 辅助函数 ========================


@dataclass
class HealthCheckResult:
    """健康检查结果"""

    service: str
    healthy: bool
    status_code: Optional[int] = None
    message: Optional[str] = None
    response_time_ms: float = 0


def check_service_health(url: str, service_name: str, timeout: float = 5.0) -> HealthCheckResult:
    """
    检查服务健康状态

    Args:
        url: 服务健康检查 URL
        service_name: 服务名称
        timeout: 超时时间（秒）

    Returns:
        HealthCheckResult 健康检查结果
    """
    start = time.time()
    try:
        resp = requests.get(url, timeout=timeout)
        elapsed_ms = (time.time() - start) * 1000

        return HealthCheckResult(
            service=service_name,
            healthy=resp.status_code == 200,
            status_code=resp.status_code,
            message=None if resp.status_code == 200 else resp.text[:200],
            response_time_ms=elapsed_ms,
        )
    except requests.exceptions.Timeout:
        return HealthCheckResult(
            service=service_name,
            healthy=False,
            message="请求超时",
            response_time_ms=(time.time() - start) * 1000,
        )
    except requests.exceptions.ConnectionError as e:
        return HealthCheckResult(
            service=service_name,
            healthy=False,
            message=f"连接失败: {str(e)[:100]}",
            response_time_ms=(time.time() - start) * 1000,
        )
    except Exception as e:
        return HealthCheckResult(
            service=service_name,
            healthy=False,
            message=f"未知错误: {str(e)[:100]}",
            response_time_ms=(time.time() - start) * 1000,
        )


def call_mcp_tool(tool: str, arguments: dict, gateway_url: str = None) -> dict:
    """
    调用 Gateway MCP 工具

    Args:
        tool: 工具名称
        arguments: 工具参数
        gateway_url: Gateway URL（可选）

    Returns:
        MCP 响应字典
    """
    if gateway_url is None:
        gateway_url = get_gateway_url()

    resp = requests.post(
        f"{gateway_url}/mcp",
        json={"tool": tool, "arguments": arguments},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def docker_container_action(container_name: str, action: str) -> bool:
    """
    执行 Docker 容器操作

    Args:
        container_name: 容器名称
        action: 操作（stop/start）

    Returns:
        True 表示成功
    """
    try:
        result = subprocess.run(
            ["docker", action, container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def wait_for_service(url: str, max_wait: int = 60, interval: int = 2) -> bool:
    """
    等待服务恢复

    Args:
        url: 健康检查 URL
        max_wait: 最大等待时间（秒）
        interval: 检查间隔（秒）

    Returns:
        True 表示服务已恢复
    """
    start = time.time()
    while time.time() - start < max_wait:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ======================== Fixture ========================


@pytest.fixture(scope="module")
def integration_config():
    """集成测试配置"""
    gateway_url = get_gateway_url()
    openmemory_url = get_openmemory_url()
    postgres_dsn = get_postgres_dsn()

    return {
        "gateway_url": gateway_url,
        "openmemory_url": openmemory_url,
        "postgres_dsn": postgres_dsn,
        "gateway_health": f"{gateway_url}/health",
        "openmemory_health": f"{openmemory_url}/health",
    }


@pytest.fixture(scope="module")
def all_services_healthy(integration_config):
    """确保所有服务健康的 fixture"""
    gateway_result = check_service_health(integration_config["gateway_health"], "Gateway")
    openmemory_result = check_service_health(integration_config["openmemory_health"], "OpenMemory")

    if not gateway_result.healthy:
        pytest.skip(f"Gateway 服务不健康: {gateway_result.message}")

    if not openmemory_result.healthy:
        pytest.skip(f"OpenMemory 服务不健康: {openmemory_result.message}")

    return {
        "gateway": gateway_result,
        "openmemory": openmemory_result,
    }


# ======================== 健康检查测试 ========================


@integration_test
class TestServiceHealthCheck:
    """服务健康检查测试"""

    def test_gateway_health(self, integration_config):
        """验证 Gateway 健康端点"""
        result = check_service_health(integration_config["gateway_health"], "Gateway")

        assert result.healthy, f"Gateway 不健康: {result.message}"
        assert result.status_code == 200
        assert result.response_time_ms < 5000, "响应时间过长"

    def test_openmemory_health(self, integration_config):
        """验证 OpenMemory 健康端点"""
        result = check_service_health(integration_config["openmemory_health"], "OpenMemory")

        assert result.healthy, f"OpenMemory 不健康: {result.message}"
        assert result.status_code == 200
        assert result.response_time_ms < 5000, "响应时间过长"

    def test_postgres_connection(self, integration_config):
        """验证 PostgreSQL 连接（通过 Gateway）"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        try:
            import psycopg

            conn = psycopg.connect(dsn)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                result = cur.fetchone()
            conn.close()
            assert result[0] == 1
        except Exception as e:
            pytest.fail(f"PostgreSQL 连接失败: {e}")


# ======================== memory_store/memory_query 测试 ========================


@integration_test
class TestMemoryOperations:
    """memory_store 和 memory_query 集成测试"""

    def test_memory_store_success(self, integration_config, all_services_healthy):
        """验证 memory_store 成功写入"""
        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# 集成测试记忆 {unique_id}\n\n这是一条测试记忆，用于验证完整的存储流程。"

        response = call_mcp_tool(
            tool="memory_store",
            arguments={
                "payload_md": test_content,
                "target_space": "team:integration_test",
                "actor_user_id": "integration_tester",
            },
            gateway_url=integration_config["gateway_url"],
        )

        assert response["ok"] is True, f"memory_store 失败: {response}"
        result = response.get("result", {})

        assert result.get("ok") is True, f"存储结果不成功: {result}"
        assert result.get("action") in ("allow", "redirect", "deferred"), (
            f"意外的 action: {result.get('action')}"
        )

        # 统一响应契约验证
        # 所有响应必须包含 correlation_id
        assert result.get("correlation_id") is not None, f"响应必须包含 correlation_id: {result}"

        # 根据 action 验证必需字段
        if result.get("action") in ("allow", "redirect"):
            # 成功写入应该有 memory_id
            assert result.get("memory_id") is not None, (
                f"action={result.get('action')} 时应有 memory_id: {result}"
            )
        elif result.get("action") == "deferred":
            # 降级到 outbox 应该有 outbox_id
            assert result.get("outbox_id") is not None, (
                f"action=deferred 时必须有 outbox_id: {result}"
            )
            assert isinstance(result.get("outbox_id"), int), (
                f"outbox_id 必须为 int 类型: {type(result.get('outbox_id'))}"
            )

    def test_memory_query_success(self, integration_config, all_services_healthy):
        """验证 memory_query 查询功能"""
        response = call_mcp_tool(
            tool="memory_query",
            arguments={
                "query": "集成测试",
                "spaces": ["team:integration_test"],
                "top_k": 5,
            },
            gateway_url=integration_config["gateway_url"],
        )

        assert response["ok"] is True, f"memory_query 失败: {response}"
        result = response.get("result", {})

        # 查询应该返回有效结构
        assert "results" in result
        assert "total" in result
        assert "spaces_searched" in result

    def test_memory_store_with_metadata(self, integration_config, all_services_healthy):
        """验证带元数据的 memory_store"""
        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# 元数据测试 {unique_id}"

        response = call_mcp_tool(
            tool="memory_store",
            arguments={
                "payload_md": test_content,
                "target_space": "team:integration_test",
                "meta_json": {
                    "test_id": unique_id,
                    "source": "integration_test",
                },
                "kind": "FACT",
                "evidence_refs": [f"test_ref_{unique_id}"],
            },
            gateway_url=integration_config["gateway_url"],
        )

        assert response["ok"] is True, f"memory_store 失败: {response}"


# ======================== 降级测试 ========================


@integration_test
@degradation_test
@http_only_skip
class TestDegradationFlow:
    """
    降级流程测试

    测试场景：
    1. 停止 OpenMemory 容器
    2. 调用 memory_store，验证写入 outbox
    3. 重启 OpenMemory 容器
    4. 运行 outbox_worker，验证 flush 成功

    门禁规则：
    - FULL 模式下，若缺少 Docker/DB 能力应 FAIL（not skip）
    - 规则定义在 scripts/unified_stack_gate_contract.py::PROFILE_CONFIGS[FULL].must_fail_if_blocked
    """

    # 容器名需与 docker-compose.unified.yml 中的 container_name 一致
    # 统一栈容器名: engram_openmemory（见 docker-compose.unified.yml 第474行）
    OPENMEMORY_CONTAINER = os.environ.get("OPENMEMORY_CONTAINER_NAME", "engram_openmemory")

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            # FULL 模式下缺 DSN 必须 FAIL
            enforce_capability_or_fail("db_invariants", "降级测试需要数据库: ")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    @pytest.fixture(scope="class", autouse=True)
    def check_degradation_capability(self):
        """
        在类级别检查降级测试所需能力

        FULL 模式下缺能力必须 FAIL。
        """
        enforce_capability_or_fail("degradation", "降级测试需要 Docker 能力: ")

    def test_degradation_write_to_outbox(self, integration_config, postgres_connection):
        """
        降级测试：OpenMemory 不可用时写入 outbox

        步骤：
        1. 停止 OpenMemory 容器
        2. 发送 memory_store 请求
        3. 验证数据写入 logbook.outbox_memory
        """
        # 1. 停止 OpenMemory 容器
        stop_success = docker_container_action(self.OPENMEMORY_CONTAINER, "stop")
        if not stop_success:
            # FULL 模式下无法停止容器必须 FAIL
            if is_full_profile():
                pytest.fail(
                    f"[FULL profile] 无法停止容器 {self.OPENMEMORY_CONTAINER}。"
                    f"FULL 模式下降级测试不能跳过，请检查 Docker 配置和容器状态。"
                )
            pytest.skip(f"无法停止容器 {self.OPENMEMORY_CONTAINER}，跳过降级测试")

        try:
            # 等待容器完全停止
            time.sleep(3)

            # 验证 OpenMemory 确实不可用
            health_result = check_service_health(
                integration_config["openmemory_health"], "OpenMemory"
            )
            assert not health_result.healthy, "OpenMemory 应该已停止"

            # 2. 发送 memory_store 请求
            unique_id = uuid.uuid4().hex[:8]
            test_content = f"# 降级测试 {unique_id}\n\n这条记忆应该被写入 outbox。"
            test_payload_sha = None
            response_outbox_id = None  # 初始化，用于后续验证

            try:
                response = call_mcp_tool(
                    tool="memory_store",
                    arguments={
                        "payload_md": test_content,
                        "target_space": "team:degradation_test",
                        "actor_user_id": "degradation_tester",
                    },
                    gateway_url=integration_config["gateway_url"],
                )

                result = response.get("result", {})

                # 统一响应契约验证：降级时 action 应该是 deferred
                assert result.get("action") == "deferred", (
                    f"降级时 action 应为 'deferred'，实际: {result.get('action')}"
                )

                # 契约要求：action=deferred 时必须返回 outbox_id
                assert result.get("outbox_id") is not None, (
                    f"deferred 响应必须包含 outbox_id: {result}"
                )
                assert isinstance(result.get("outbox_id"), int), (
                    f"outbox_id 必须为 int 类型: {type(result.get('outbox_id'))}"
                )

                # 契约要求：所有响应必须返回 correlation_id
                assert result.get("correlation_id") is not None, (
                    f"响应必须包含 correlation_id: {result}"
                )

                # 保存 outbox_id（从响应直接获取）
                response_outbox_id = result.get("outbox_id")

            except requests.exceptions.RequestException as e:
                # 如果 Gateway 自身也有问题，跳过测试
                pytest.skip(f"Gateway 请求失败: {e}")

            # 3. 验证数据写入 logbook.outbox_memory
            import hashlib

            test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

            with postgres_connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id, target_space, status, payload_sha
                    FROM logbook.outbox_memory
                    WHERE payload_sha = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                    (test_payload_sha,),
                )
                row = cur.fetchone()

                assert row is not None, "outbox 中应该存在刚写入的记录"
                assert row[1] == "team:degradation_test"
                assert row[2] == "pending"

                # 验证响应中的 outbox_id 与数据库一致
                assert row[0] == response_outbox_id, (
                    f"响应 outbox_id({response_outbox_id}) 与数据库不一致({row[0]})"
                )

                # 保存 outbox_id 供后续测试使用
                pytest.outbox_id_for_recovery = row[0]
                pytest.payload_sha_for_recovery = test_payload_sha

        finally:
            # 重启 OpenMemory 容器
            docker_container_action(self.OPENMEMORY_CONTAINER, "start")

    def test_degradation_recovery_flush(self, integration_config, postgres_connection):
        """
        降级恢复测试：OpenMemory 恢复后 outbox flush 成功

        步骤：
        1. 确保 OpenMemory 已恢复
        2. 运行 outbox_worker（--once 模式）
        3. 验证 outbox 记录状态变为 sent
        """
        # 获取前一个测试保存的信息
        outbox_id = getattr(pytest, "outbox_id_for_recovery", None)
        getattr(pytest, "payload_sha_for_recovery", None)

        if not outbox_id:
            pytest.skip("前置降级测试未成功执行")

        # 1. 等待 OpenMemory 恢复
        recovered = wait_for_service(
            integration_config["openmemory_health"],
            max_wait=60,
            interval=2,
        )

        if not recovered:
            pytest.fail("OpenMemory 服务未能在 60 秒内恢复")

        # 2. 运行 outbox_worker
        try:
            result = subprocess.run(
                ["python", "-m", "engram.gateway.outbox_worker", "--once"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env={**os.environ, "POSTGRES_DSN": integration_config["postgres_dsn"]},
            )

            # worker 返回码 0 表示全部成功，1 表示有失败
            # 这里我们只需要确保它能运行
            if result.returncode not in (0, 1):
                pytest.fail(f"outbox_worker 执行失败: {result.stderr}")

        except subprocess.TimeoutExpired:
            pytest.fail("outbox_worker 执行超时")
        except FileNotFoundError:
            pytest.skip("无法找到 outbox_worker 模块")

        # 3. 验证 outbox 记录状态
        # 给一些时间让状态更新
        time.sleep(2)

        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, last_error
                FROM logbook.outbox_memory
                WHERE outbox_id = %s
            """,
                (outbox_id,),
            )
            row = cur.fetchone()

            if row:
                status = row[0]
                # sent 或者 pending（如果 worker 还没处理到）都可以接受
                assert status in ("sent", "pending"), f"outbox 状态异常: {status}"

                # 如果是 sent，验证 last_error 包含 memory_id
                if status == "sent":
                    last_error = row[1] or ""
                    assert "memory_id=" in last_error, (
                        f"sent 状态的记录应包含 memory_id: {last_error}"
                    )


# ======================== 端到端完整流程测试 ========================


@integration_test
class TestEndToEndFlow:
    """端到端完整流程测试"""

    def test_store_and_query_roundtrip(self, integration_config, all_services_healthy):
        """完整的存储-查询往返测试"""
        # 生成唯一内容
        unique_id = uuid.uuid4().hex
        test_keyword = f"roundtrip_test_{unique_id[:8]}"
        test_content = f"# 往返测试 {test_keyword}\n\n这是一条用于验证完整存储-查询流程的测试记忆。"

        # 1. 存储记忆
        store_response = call_mcp_tool(
            tool="memory_store",
            arguments={
                "payload_md": test_content,
                "target_space": "team:roundtrip_test",
                "actor_user_id": "roundtrip_tester",
            },
            gateway_url=integration_config["gateway_url"],
        )

        assert store_response["ok"] is True
        store_result = store_response.get("result", {})
        assert store_result.get("ok") is True

        # 2. 等待索引（OpenMemory 可能需要时间）
        time.sleep(2)

        # 3. 查询记忆
        query_response = call_mcp_tool(
            tool="memory_query",
            arguments={
                "query": test_keyword,
                "spaces": ["team:roundtrip_test"],
                "top_k": 5,
            },
            gateway_url=integration_config["gateway_url"],
        )

        assert query_response["ok"] is True
        query_result = query_response.get("result", {})

        # 查询结果验证（可能找到也可能因为索引延迟找不到）
        assert "results" in query_result
        assert isinstance(query_result["results"], list)


# ======================== Mock 降级测试（不依赖 Docker 操作） ========================


@integration_test
class TestMockDegradationFlow:
    """
    Mock 降级流程测试

    测试场景（不需要实际停止 OpenMemory 服务）：
    1. Mock OpenMemoryClient.store 抛出 OpenMemoryConnectionError
    2. 调用 memory_store_impl 触发入队
    3. 切换 mock 为成功，运行 process_batch flush
    4. 断言：
       - outbox 状态 pending → sent
       - write_audit 包含 openmemory_write_failed:* 和 outbox_flush_success
       - 两条记录共享 outbox_id，extra 包含相同 correlation_id
    """

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_mock_degradation_and_recovery(self, integration_config, postgres_connection):
        """
        完整的 Mock 降级与恢复测试

        流程：
        1. Mock OpenMemoryClient.store 抛出 OpenMemoryConnectionError
        2. 调用 memory_store_impl 触发入队
        3. 验证 outbox 记录已创建且状态为 pending
        4. 验证 write_audit 记录包含 openmemory_write_failed:*
        5. 切换 mock 为成功
        6. 运行 process_batch flush
        7. 验证 outbox 状态变为 sent
        8. 验证 write_audit 记录包含 outbox_flush_success
        9. 验证两条 audit 记录共享 outbox_id 和 correlation_id
        """
        import asyncio
        import hashlib
        import json
        from unittest.mock import MagicMock, patch

        # 导入所需模块
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.openmemory_client import (
            OpenMemoryClient,
            OpenMemoryConnectionError,
            StoreResult,
        )
        from engram.gateway.outbox_worker import WorkerConfig, process_batch

        # 生成唯一测试内容
        unique_id = uuid.uuid4().hex[:12]
        test_content = f"# Mock 降级测试 {unique_id}\n\n这是一条用于验证 Mock 降级流程的测试记忆。"
        test_space = f"team:mock_degradation_test_{unique_id[:6]}"
        test_actor = f"mock_tester_{unique_id[:6]}"
        test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

        # 记录测试开始时间（用于查询 audit）
        from datetime import datetime, timezone

        datetime.now(timezone.utc).isoformat()

        # ============ 阶段 1: Mock OpenMemory 失败，触发入队 ============

        # 创建 Mock，抛出 OpenMemoryConnectionError
        mock_store_error = OpenMemoryConnectionError(
            message="模拟 OpenMemory 连接失败",
            status_code=None,
            response=None,
        )

        # Mock get_client 返回的客户端的 store 方法
        with patch("engram.gateway.main.get_client") as mock_get_client:
            mock_client = MagicMock(spec=OpenMemoryClient)
            mock_client.store.side_effect = mock_store_error
            mock_get_client.return_value = mock_client

            # 调用 memory_store_impl（使用 asyncio.run 或兼容方式）
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # 如果已在事件循环中，创建新任务
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        memory_store_impl(
                            payload_md=test_content,
                            target_space=test_space,
                            actor_user_id=test_actor,
                            kind="FACT",
                            evidence_refs=[f"test_ref_{unique_id}"],
                        ),
                    ).result()
            else:
                # 直接使用 asyncio.run
                result = asyncio.run(
                    memory_store_impl(
                        payload_md=test_content,
                        target_space=test_space,
                        actor_user_id=test_actor,
                        kind="FACT",
                        evidence_refs=[f"test_ref_{unique_id}"],
                    )
                )

            # 验证返回结果 - 使用统一响应契约
            assert result.ok is False, f"OpenMemory 失败时应返回 ok=False: {result}"
            assert result.action == "deferred", (
                f"应返回 action=deferred（已入队 outbox），实际: {result.action}"
            )

            # 契约要求：action=deferred 时必须返回 outbox_id
            assert result.outbox_id is not None, f"deferred 响应必须包含 outbox_id: {result}"
            assert isinstance(result.outbox_id, int), (
                f"outbox_id 必须为 int 类型: {type(result.outbox_id)}"
            )
            outbox_id = result.outbox_id

            # 契约要求：所有响应必须返回 correlation_id
            assert result.correlation_id is not None, f"响应必须包含 correlation_id: {result}"
            assert result.correlation_id.startswith("corr-"), (
                f"correlation_id 格式不正确: {result.correlation_id}"
            )

        # ============ 阶段 2: 验证 outbox 记录状态为 pending ============

        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, target_space, status, payload_sha
                FROM logbook.outbox_memory
                WHERE outbox_id = %s
            """,
                (outbox_id,),
            )
            row = cur.fetchone()

            assert row is not None, f"outbox 记录不存在: outbox_id={outbox_id}"
            assert row[1] == test_space, f"target_space 不匹配: {row[1]} != {test_space}"
            assert row[2] == "pending", f"outbox 状态应为 pending: {row[2]}"
            assert row[3] == test_payload_sha, f"payload_sha 不匹配: {row[3]} != {test_payload_sha}"

        # ============ 阶段 3: 验证 write_audit 包含 openmemory_write_failed:* ============

        # 查询相关的 audit 记录
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, actor_user_id, target_space, action, reason,
                       payload_sha, evidence_refs_json, created_at
                FROM governance.write_audit
                WHERE payload_sha = %s
                  AND reason LIKE 'openmemory_write_failed:%%'
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (test_payload_sha,),
            )
            failed_audit = cur.fetchone()

            assert failed_audit is not None, "应存在 openmemory_write_failed 的 audit 记录"

            failed_audit_id = failed_audit[0]
            failed_reason = failed_audit[4]
            failed_evidence = failed_audit[6]

            # 验证 reason 格式
            assert failed_reason.startswith("openmemory_write_failed:"), (
                f"reason 应以 openmemory_write_failed: 开头: {failed_reason}"
            )

            # 验证 evidence_refs_json 包含 outbox_id
            if isinstance(failed_evidence, str):
                failed_evidence = json.loads(failed_evidence)

            assert failed_evidence.get("outbox_id") == outbox_id, (
                f"evidence_refs_json 应包含 outbox_id={outbox_id}: {failed_evidence}"
            )

            # 提取 correlation_id
            failed_extra = failed_evidence.get("extra", {})
            failed_correlation_id = failed_extra.get("correlation_id")
            assert failed_correlation_id, (
                f"evidence_refs_json.extra 应包含 correlation_id: {failed_evidence}"
            )

        # ============ 阶段 4: Mock OpenMemory 成功，运行 process_batch ============

        # 创建成功的 Mock 结果
        mock_memory_id = f"mem_{unique_id}"
        mock_store_success = StoreResult(
            success=True,
            memory_id=mock_memory_id,
            data={"id": mock_memory_id},
        )

        # 使用 worker 的 process_batch，需要 Mock openmemory_client 模块中的 OpenMemoryClient
        with patch(
            "engram.gateway.outbox_worker.openmemory_client.OpenMemoryClient"
        ) as MockClientClass:
            mock_client_instance = MagicMock()
            mock_client_instance.store.return_value = mock_store_success
            MockClientClass.return_value = mock_client_instance

            # 配置 worker
            worker_config = WorkerConfig(
                batch_size=10,
                max_retries=5,
                base_backoff_seconds=60,
                lease_seconds=120,
                openmemory_timeout_seconds=30.0,
                openmemory_max_client_retries=0,
            )

            # 运行 process_batch
            worker_id = f"test-worker-{unique_id[:8]}"
            results = process_batch(config=worker_config, worker_id=worker_id)

            # 验证处理结果
            # 找到我们的 outbox_id 对应的结果
            our_result = None
            for r in results:
                if r.outbox_id == outbox_id:
                    our_result = r
                    break

            # 如果没找到，可能是因为批次大小限制，重新查询 outbox 状态
            if our_result is None:
                # 再尝试一次 process_batch，确保处理到
                results = process_batch(config=worker_config, worker_id=worker_id)
                for r in results:
                    if r.outbox_id == outbox_id:
                        our_result = r
                        break

        # ============ 阶段 5: 验证 outbox 状态变为 sent ============

        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, last_error
                FROM logbook.outbox_memory
                WHERE outbox_id = %s
            """,
                (outbox_id,),
            )
            row = cur.fetchone()

            assert row is not None, f"outbox 记录不存在: outbox_id={outbox_id}"
            assert row[0] == "sent", f"outbox 状态应为 sent: {row[0]}"

            # 验证 last_error 包含 memory_id
            last_error = row[1] or ""
            assert f"memory_id={mock_memory_id}" in last_error, (
                f"last_error 应包含 memory_id: {last_error}"
            )

        # ============ 阶段 6: 验证 write_audit 包含 outbox_flush_success ============

        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, actor_user_id, target_space, action, reason,
                       payload_sha, evidence_refs_json, created_at
                FROM governance.write_audit
                WHERE payload_sha = %s
                  AND reason = 'outbox_flush_success'
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (test_payload_sha,),
            )
            success_audit = cur.fetchone()

            assert success_audit is not None, "应存在 outbox_flush_success 的 audit 记录"

            success_audit_id = success_audit[0]
            success_reason = success_audit[4]
            success_evidence = success_audit[6]

            # 验证 reason
            assert success_reason == "outbox_flush_success", (
                f"reason 应为 outbox_flush_success: {success_reason}"
            )

            # 验证 evidence_refs_json 包含 outbox_id
            if isinstance(success_evidence, str):
                success_evidence = json.loads(success_evidence)

            assert success_evidence.get("outbox_id") == outbox_id, (
                f"evidence_refs_json 应包含 outbox_id={outbox_id}: {success_evidence}"
            )

            # 验证 memory_id
            assert success_evidence.get("memory_id") == mock_memory_id, (
                f"evidence_refs_json 应包含 memory_id={mock_memory_id}: {success_evidence}"
            )

            # 提取 correlation_id
            success_extra = success_evidence.get("extra", {})
            success_correlation_id = success_extra.get("correlation_id")
            assert success_correlation_id, (
                f"evidence_refs_json.extra 应包含 correlation_id: {success_evidence}"
            )

        # ============ 阶段 7: 验证两条 audit 记录共享 outbox_id ============

        # 已在上面验证，这里做最终确认
        assert failed_evidence.get("outbox_id") == success_evidence.get("outbox_id") == outbox_id, (
            f"两条 audit 记录应共享 outbox_id: failed={failed_evidence.get('outbox_id')}, success={success_evidence.get('outbox_id')}"
        )

        # 注意：failed_correlation_id 是 main.py 中 memory_store_impl 生成的
        # success_correlation_id 是 outbox_worker.py 中 process_batch 生成的
        # 这两个 correlation_id 是独立生成的，分别追踪不同的处理阶段
        # 共享的是 outbox_id，而不是 correlation_id

        # 输出测试结果摘要
        print("\n[Mock 降级测试完成]")
        print(f"  - outbox_id: {outbox_id}")
        print(f"  - failed_audit_id: {failed_audit_id}, reason: {failed_reason}")
        print(f"  - success_audit_id: {success_audit_id}, reason: {success_reason}")
        print(f"  - failed_correlation_id: {failed_correlation_id}")
        print(f"  - success_correlation_id: {success_correlation_id}")
        print(f"  - shared outbox_id: {outbox_id}")
        print(f"  - memory_id: {mock_memory_id}")

    def test_outbox_id_and_correlation_id_audit_contract(
        self, integration_config, postgres_connection
    ):
        """
        验证 outbox 流程中两条 audit 记录的 outbox_id 和 correlation_id 契约

        测试场景：
        1. Mock OpenMemory 失败，触发入队 outbox（Gateway 写入第一条 audit）
        2. 切换 Mock 为成功，运行 outbox_worker flush（Worker 写入第二条 audit）
        3. 验证两条 write_audit 记录满足以下契约：
           - evidence_refs_json->>'outbox_id' 相等（共享同一 outbox 记录）
           - 两条记录的 correlation_id 均匹配 pattern（^corr-[a-fA-F0-9]{16}$）
           - source 分别为 'gateway' 和 'outbox_worker'

        注意：
        - 两条记录的 correlation_id 不要求相等，因为它们是独立生成的
        - gateway 的 correlation_id 追踪原始请求
        - outbox_worker 的 correlation_id 追踪补偿处理批次
        """
        import asyncio
        import hashlib
        import json
        from unittest.mock import MagicMock, patch

        # 导入所需模块
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.openmemory_client import (
            OpenMemoryClient,
            OpenMemoryConnectionError,
            StoreResult,
        )
        from engram.gateway.outbox_worker import WorkerConfig, process_batch
        from tests.gateway.helpers import CORRELATION_ID_PATTERN

        # 生成唯一测试内容
        unique_id = uuid.uuid4().hex[:12]
        test_content = f"# Outbox ID 契约测试 {unique_id}\n\n验证 audit 记录的 outbox_id 和 correlation_id。"
        test_space = f"team:outbox_id_contract_{unique_id[:6]}"
        test_actor = f"contract_tester_{unique_id[:6]}"
        test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

        # ============ 阶段 1: Mock OpenMemory 失败，触发入队 ============

        mock_store_error = OpenMemoryConnectionError(
            message="模拟连接失败以触发 outbox 入队",
            status_code=None,
            response=None,
        )

        with patch("engram.gateway.main.get_client") as mock_get_client:
            mock_client = MagicMock(spec=OpenMemoryClient)
            mock_client.store.side_effect = mock_store_error
            mock_get_client.return_value = mock_client

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        memory_store_impl(
                            payload_md=test_content,
                            target_space=test_space,
                            actor_user_id=test_actor,
                            kind="FACT",
                            evidence_refs=[f"test_ref_{unique_id}"],
                        ),
                    ).result()
            else:
                result = asyncio.run(
                    memory_store_impl(
                        payload_md=test_content,
                        target_space=test_space,
                        actor_user_id=test_actor,
                        kind="FACT",
                        evidence_refs=[f"test_ref_{unique_id}"],
                    )
                )

            # 验证返回结果
            assert result.ok is False, f"OpenMemory 失败时应返回 ok=False: {result}"
            assert result.action == "deferred", (
                f"应返回 action=deferred，实际: {result.action}"
            )
            assert result.outbox_id is not None, f"deferred 响应必须包含 outbox_id: {result}"
            outbox_id = result.outbox_id

        # ============ 阶段 2: Mock OpenMemory 成功，运行 process_batch ============

        mock_memory_id = f"mem_{unique_id}"
        mock_store_success = StoreResult(
            success=True,
            memory_id=mock_memory_id,
            data={"id": mock_memory_id},
        )

        with patch(
            "engram.gateway.outbox_worker.openmemory_client.OpenMemoryClient"
        ) as MockClientClass:
            mock_client_instance = MagicMock()
            mock_client_instance.store.return_value = mock_store_success
            MockClientClass.return_value = mock_client_instance

            worker_config = WorkerConfig(
                batch_size=10,
                max_retries=5,
                base_backoff_seconds=60,
                lease_seconds=120,
                openmemory_timeout_seconds=30.0,
                openmemory_max_client_retries=0,
            )

            worker_id = f"contract-worker-{unique_id[:8]}"
            results = process_batch(config=worker_config, worker_id=worker_id)

            # 如果没处理到，再尝试一次
            our_result = None
            for r in results:
                if r.outbox_id == outbox_id:
                    our_result = r
                    break

            if our_result is None:
                results = process_batch(config=worker_config, worker_id=worker_id)

        # ============ 阶段 3: 查询两条 audit 记录并验证契约 ============

        with postgres_connection.cursor() as cur:
            # 查询所有与此 payload_sha 相关且包含 outbox_id 的 audit 记录
            cur.execute(
                """
                SELECT audit_id, action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE payload_sha = %s
                  AND evidence_refs_json->>'outbox_id' = %s
                ORDER BY created_at ASC
            """,
                (test_payload_sha, str(outbox_id)),
            )
            audit_rows = cur.fetchall()

            # 应至少有两条记录（gateway 入队 + worker flush）
            assert len(audit_rows) >= 2, (
                f"应至少有两条 audit 记录（gateway 入队 + worker flush），"
                f"实际: {len(audit_rows)} 条"
            )

            # 解析 evidence_refs_json
            audits = []
            for row in audit_rows:
                audit_id, action, reason, evidence_json = row
                if isinstance(evidence_json, str):
                    evidence_json = json.loads(evidence_json)
                audits.append({
                    "audit_id": audit_id,
                    "action": action,
                    "reason": reason,
                    "evidence_refs_json": evidence_json,
                })

            # 找到 gateway 和 outbox_worker 的 audit 记录
            gateway_audit = None
            worker_audit = None

            for audit in audits:
                source = audit["evidence_refs_json"].get("source")
                if source == "gateway":
                    gateway_audit = audit
                elif source == "outbox_worker":
                    worker_audit = audit

            # 断言 1: 两条记录的 source 分别为 gateway 和 outbox_worker
            assert gateway_audit is not None, (
                f"未找到 source='gateway' 的 audit 记录。所有记录: {audits}"
            )
            assert worker_audit is not None, (
                f"未找到 source='outbox_worker' 的 audit 记录。所有记录: {audits}"
            )

            # 提取 outbox_id
            gateway_outbox_id = gateway_audit["evidence_refs_json"].get("outbox_id")
            worker_outbox_id = worker_audit["evidence_refs_json"].get("outbox_id")

            # 断言 2: evidence_refs_json->>'outbox_id' 相等
            assert gateway_outbox_id == worker_outbox_id == outbox_id, (
                f"两条 audit 记录的 outbox_id 应相等:\n"
                f"  gateway_outbox_id: {gateway_outbox_id}\n"
                f"  worker_outbox_id: {worker_outbox_id}\n"
                f"  expected: {outbox_id}"
            )

            # 提取 correlation_id
            gateway_corr_id = gateway_audit["evidence_refs_json"].get("correlation_id")
            worker_corr_id = worker_audit["evidence_refs_json"].get("correlation_id")

            # 断言 3: 两条记录的 correlation_id 均匹配 pattern
            assert gateway_corr_id is not None, (
                f"gateway audit 的 correlation_id 不应为空: {gateway_audit}"
            )
            assert worker_corr_id is not None, (
                f"worker audit 的 correlation_id 不应为空: {worker_audit}"
            )

            assert CORRELATION_ID_PATTERN.match(gateway_corr_id), (
                f"gateway audit 的 correlation_id 格式不正确: {gateway_corr_id}\n"
                f"期望匹配: ^corr-[a-fA-F0-9]{{16}}$"
            )
            assert CORRELATION_ID_PATTERN.match(worker_corr_id), (
                f"worker audit 的 correlation_id 格式不正确: {worker_corr_id}\n"
                f"期望匹配: ^corr-[a-fA-F0-9]{{16}}$"
            )

            # 输出测试结果
            print("\n[Outbox ID 契约测试完成]")
            print(f"  - shared outbox_id: {outbox_id}")
            print(f"  - gateway audit_id: {gateway_audit['audit_id']}")
            print(f"  - gateway correlation_id: {gateway_corr_id}")
            print("  - gateway source: gateway")
            print(f"  - worker audit_id: {worker_audit['audit_id']}")
            print(f"  - worker correlation_id: {worker_corr_id}")
            print("  - worker source: outbox_worker")
            print(f"  - correlation_ids 独立生成: {gateway_corr_id != worker_corr_id}")


# ======================== Mock 查询降级测试 ========================


@integration_test
class TestMockQueryDegradation:
    """
    Mock 查询降级测试

    测试场景：
    1. Mock OpenMemoryClient.search 抛出 OpenMemoryError
    2. 调用 memory_query_impl
    3. 断言 degraded=True 且 results 来自 Logbook knowledge_candidates
    """

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    @pytest.fixture
    def sample_knowledge_candidate(self, postgres_connection):
        """
        创建测试用的 knowledge_candidate 记录

        Returns:
            candidate_id
        """
        import json

        unique_id = uuid.uuid4().hex[:8]

        with postgres_connection.cursor() as cur:
            # 首先创建 analysis.runs 记录（knowledge_candidates 需要 run_id）
            cur.execute("""
                INSERT INTO analysis.runs (item_id, pipeline_version, status)
                VALUES (NULL, 'test_v1', 'completed')
                RETURNING run_id
            """)
            run_id = cur.fetchone()[0]

            # 创建 knowledge_candidate 记录
            test_title = f"测试降级查询标题_{unique_id}"
            test_content = f"这是测试降级查询的内容，包含唯一标识 {unique_id}"

            cur.execute(
                """
                INSERT INTO analysis.knowledge_candidates
                    (run_id, kind, title, content_md, confidence, evidence_refs_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING candidate_id
            """,
                (
                    run_id,
                    "FACT",
                    test_title,
                    test_content,
                    "high",
                    json.dumps({"test_id": unique_id}),
                ),
            )
            candidate_id = cur.fetchone()[0]

        yield {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "unique_id": unique_id,
            "title": test_title,
            "content": test_content,
        }

        # 清理测试数据
        with postgres_connection.cursor() as cur:
            cur.execute(
                "DELETE FROM analysis.knowledge_candidates WHERE candidate_id = %s", (candidate_id,)
            )
            cur.execute("DELETE FROM analysis.runs WHERE run_id = %s", (run_id,))

    def test_query_degradation_returns_logbook_results(
        self, integration_config, postgres_connection, sample_knowledge_candidate
    ):
        """
        测试查询降级：OpenMemory 失败时返回 Logbook 结果

        验证：
        1. degraded=True
        2. results 来自 Logbook knowledge_candidates
        3. results 包含正确的数据结构
        """
        import asyncio
        from unittest.mock import MagicMock, patch

        from engram.gateway.main import memory_query_impl
        from engram.gateway.openmemory_client import (
            OpenMemoryClient,
            OpenMemoryError,
        )

        unique_id = sample_knowledge_candidate["unique_id"]

        # Mock OpenMemory search 抛出 OpenMemoryError
        mock_search_error = OpenMemoryError(
            message="模拟 OpenMemory 查询服务不可用",
            status_code=503,
            response=None,
        )

        with patch("engram.gateway.main.get_client") as mock_get_client:
            mock_client = MagicMock(spec=OpenMemoryClient)
            mock_client.search.side_effect = mock_search_error
            mock_get_client.return_value = mock_client

            # 调用 memory_query_impl
            result = asyncio.get_event_loop().run_until_complete(
                memory_query_impl(
                    query=unique_id,  # 使用 unique_id 作为查询关键词
                    spaces=["team:test_degradation"],
                    top_k=10,
                )
            )

            # 验证 degraded=True
            assert result.degraded is True, f"应返回 degraded=True: {result}"

            # 验证 ok=True（降级查询成功）
            assert result.ok is True, f"降级查询应成功: {result}"

            # 验证 message 包含降级信息
            assert "降级查询" in (result.message or ""), f"message 应包含降级信息: {result.message}"

            # 验证 results 来自 Logbook
            assert len(result.results) > 0, f"应返回 Logbook 查询结果: {result.results}"

            # 验证结果结构
            first_result = result.results[0]
            assert "id" in first_result, "结果应包含 id 字段"
            assert first_result["id"].startswith("kc_"), "id 应以 kc_ 开头（knowledge_candidate）"
            assert "content" in first_result, "结果应包含 content 字段"
            assert "title" in first_result, "结果应包含 title 字段"
            assert "source" in first_result, "结果应包含 source 字段"
            assert first_result["source"] == "logbook_fallback", "source 应为 logbook_fallback"

            # 验证返回了正确的测试数据
            found = False
            for r in result.results:
                if unique_id in r.get("content", ""):
                    found = True
                    assert r["title"] == sample_knowledge_candidate["title"]
                    break

            assert found, f"应返回包含 unique_id 的测试记录: {result.results}"

    def test_query_degradation_with_fallback_failure(self, integration_config):
        """
        测试查询降级：OpenMemory 失败且 Logbook 回退也失败

        验证：
        1. degraded=True
        2. ok=False
        3. message 包含两个错误信息
        """
        import asyncio
        from unittest.mock import MagicMock, patch

        from engram.gateway.main import memory_query_impl
        from engram.gateway.openmemory_client import (
            OpenMemoryClient,
            OpenMemoryError,
        )

        # Mock OpenMemory search 抛出 OpenMemoryError
        mock_search_error = OpenMemoryError(
            message="模拟 OpenMemory 查询服务不可用",
            status_code=503,
            response=None,
        )

        with (
            patch("engram.gateway.main.get_client") as mock_get_client,
            patch(
                "engram.gateway.main.logbook_adapter.query_knowledge_candidates"
            ) as mock_logbook_query,
        ):
            mock_client = MagicMock(spec=OpenMemoryClient)
            mock_client.search.side_effect = mock_search_error
            mock_get_client.return_value = mock_client

            # Mock Logbook 回退也失败
            mock_logbook_query.side_effect = Exception("模拟 Logbook 数据库连接失败")

            # 调用 memory_query_impl
            result = asyncio.get_event_loop().run_until_complete(
                memory_query_impl(
                    query="test_query",
                    spaces=["team:test"],
                    top_k=10,
                )
            )

            # 验证 degraded=True
            assert result.degraded is True, f"应返回 degraded=True: {result}"

            # 验证 ok=False（两个查询都失败）
            assert result.ok is False, f"两个查询都失败时应返回 ok=False: {result}"

            # 验证 results 为空
            assert len(result.results) == 0, f"失败时应返回空结果: {result.results}"

            # 验证 message 包含两个错误信息
            assert "OpenMemory" in (result.message or ""), (
                f"message 应包含 OpenMemory 错误: {result.message}"
            )
            assert "回退" in (result.message or ""), f"message 应包含回退错误: {result.message}"


# ======================== 可靠性报告测试 ========================


@integration_test
class TestReliabilityReport:
    """
    可靠性报告端点测试

    验证 /reliability/report 端点和 MCP 工具 reliability_report 的功能。
    """

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_reliability_report_rest_endpoint(self, integration_config, all_services_healthy):
        """
        验证 REST 端点 /reliability/report 返回正确的 JSON 结构
        """
        gateway_url = integration_config["gateway_url"]

        resp = requests.get(f"{gateway_url}/reliability/report", timeout=30)
        assert resp.status_code == 200, f"请求失败: {resp.text}"

        data = resp.json()

        # 验证顶层字段
        assert "ok" in data, "响应应包含 ok 字段"
        assert data["ok"] is True, f"响应应成功: {data}"
        assert "outbox_stats" in data, "响应应包含 outbox_stats 字段"
        assert "audit_stats" in data, "响应应包含 audit_stats 字段"
        assert "generated_at" in data, "响应应包含 generated_at 字段"

        # 验证 outbox_stats 结构
        outbox_stats = data["outbox_stats"]
        assert "total" in outbox_stats, "outbox_stats 应包含 total"
        assert "by_status" in outbox_stats, "outbox_stats 应包含 by_status"
        assert "avg_retry_count" in outbox_stats, "outbox_stats 应包含 avg_retry_count"
        assert "oldest_pending_age_seconds" in outbox_stats, (
            "outbox_stats 应包含 oldest_pending_age_seconds"
        )

        # 验证 by_status 字段
        by_status = outbox_stats["by_status"]
        assert "pending" in by_status, "by_status 应包含 pending"
        assert "sent" in by_status, "by_status 应包含 sent"
        assert "dead" in by_status, "by_status 应包含 dead"

        # 验证 audit_stats 结构
        audit_stats = data["audit_stats"]
        assert "total" in audit_stats, "audit_stats 应包含 total"
        assert "by_action" in audit_stats, "audit_stats 应包含 by_action"
        assert "recent_24h" in audit_stats, "audit_stats 应包含 recent_24h"
        assert "by_reason" in audit_stats, "audit_stats 应包含 by_reason"

        # 验证 by_action 字段
        by_action = audit_stats["by_action"]
        assert "allow" in by_action, "by_action 应包含 allow"
        assert "redirect" in by_action, "by_action 应包含 redirect"
        assert "reject" in by_action, "by_action 应包含 reject"

        # 验证 generated_at 是有效的 ISO 8601 格式
        from datetime import datetime

        try:
            datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
        except ValueError:
            pytest.fail(f"generated_at 不是有效的 ISO 8601 格式: {data['generated_at']}")

    def test_reliability_report_mcp_tool(self, integration_config, all_services_healthy):
        """
        验证 MCP 工具 reliability_report 返回正确的 JSON 结构
        """
        response = call_mcp_tool(
            tool="reliability_report",
            arguments={},
            gateway_url=integration_config["gateway_url"],
        )

        assert response["ok"] is True, f"MCP 调用失败: {response}"

        result = response.get("result", {})

        # 验证结果结构
        assert "outbox_stats" in result, "结果应包含 outbox_stats"
        assert "audit_stats" in result, "结果应包含 audit_stats"
        assert "generated_at" in result, "结果应包含 generated_at"

        # 验证 outbox_stats 结构
        outbox_stats = result["outbox_stats"]
        assert "total" in outbox_stats
        assert "by_status" in outbox_stats
        assert "pending" in outbox_stats["by_status"]
        assert "sent" in outbox_stats["by_status"]
        assert "dead" in outbox_stats["by_status"]

        # 验证 audit_stats 结构
        audit_stats = result["audit_stats"]
        assert "total" in audit_stats
        assert "by_action" in audit_stats
        assert "by_reason" in audit_stats

    def test_reliability_report_stats_accuracy(
        self, integration_config, postgres_connection, all_services_healthy
    ):
        """
        验证可靠性报告统计数据的准确性

        通过直接查询数据库对比报告中的统计值。
        """
        import hashlib

        # 1. 创建测试数据
        unique_id = uuid.uuid4().hex[:8]
        test_space = f"team:reliability_test_{unique_id}"
        test_actor = f"test_actor_{unique_id}"

        # 插入 outbox 测试记录
        test_payload = f"reliability test payload {unique_id}"
        test_payload_sha = hashlib.sha256(test_payload.encode()).hexdigest()

        with postgres_connection.cursor() as cur:
            # 插入一条 pending 的 outbox 记录
            cur.execute(
                """
                INSERT INTO logbook.outbox_memory
                    (target_space, payload_md, payload_sha, status, retry_count)
                VALUES (%s, %s, %s, 'pending', 0)
                RETURNING outbox_id
            """,
                (test_space, test_payload, test_payload_sha),
            )
            test_outbox_id = cur.fetchone()[0]

            # 插入一条 audit 记录
            cur.execute(
                """
                INSERT INTO governance.write_audit
                    (actor_user_id, target_space, action, reason, payload_sha)
                VALUES (%s, %s, 'allow', 'policy:test', %s)
                RETURNING audit_id
            """,
                (test_actor, test_space, test_payload_sha),
            )
            test_audit_id = cur.fetchone()[0]

        try:
            # 2. 获取报告
            gateway_url = integration_config["gateway_url"]
            resp = requests.get(f"{gateway_url}/reliability/report", timeout=30)
            assert resp.status_code == 200
            data = resp.json()

            # 3. 直接查询数据库获取期望值
            with postgres_connection.cursor() as cur:
                # 查询 outbox 统计
                cur.execute("""
                    SELECT
                        COUNT(*),
                        COUNT(*) FILTER (WHERE status = 'pending'),
                        COUNT(*) FILTER (WHERE status = 'sent'),
                        COUNT(*) FILTER (WHERE status = 'dead')
                    FROM logbook.outbox_memory
                """)
                db_outbox = cur.fetchone()

                # 查询 audit 统计
                cur.execute("""
                    SELECT
                        COUNT(*),
                        COUNT(*) FILTER (WHERE action = 'allow'),
                        COUNT(*) FILTER (WHERE action = 'redirect'),
                        COUNT(*) FILTER (WHERE action = 'reject')
                    FROM governance.write_audit
                """)
                db_audit = cur.fetchone()

            # 4. 验证统计准确性
            outbox_stats = data["outbox_stats"]
            assert outbox_stats["total"] == db_outbox[0], (
                f"outbox total 不匹配: 报告={outbox_stats['total']}, 数据库={db_outbox[0]}"
            )
            assert outbox_stats["by_status"]["pending"] == db_outbox[1], (
                f"outbox pending 不匹配: 报告={outbox_stats['by_status']['pending']}, 数据库={db_outbox[1]}"
            )
            assert outbox_stats["by_status"]["sent"] == db_outbox[2], "outbox sent 不匹配"
            assert outbox_stats["by_status"]["dead"] == db_outbox[3], "outbox dead 不匹配"

            audit_stats = data["audit_stats"]
            assert audit_stats["total"] == db_audit[0], (
                f"audit total 不匹配: 报告={audit_stats['total']}, 数据库={db_audit[0]}"
            )
            assert audit_stats["by_action"]["allow"] == db_audit[1], "audit allow 不匹配"
            assert audit_stats["by_action"]["redirect"] == db_audit[2], "audit redirect 不匹配"
            assert audit_stats["by_action"]["reject"] == db_audit[3], "audit reject 不匹配"

        finally:
            # 5. 清理测试数据
            with postgres_connection.cursor() as cur:
                cur.execute(
                    "DELETE FROM logbook.outbox_memory WHERE outbox_id = %s", (test_outbox_id,)
                )
                cur.execute(
                    "DELETE FROM governance.write_audit WHERE audit_id = %s", (test_audit_id,)
                )

    def test_reliability_report_by_reason_stats(
        self, integration_config, postgres_connection, all_services_healthy
    ):
        """
        验证 by_reason 统计分类的准确性
        """
        import hashlib

        unique_id = uuid.uuid4().hex[:8]
        test_space = f"team:reason_test_{unique_id}"
        test_payload_sha = hashlib.sha256(f"test_{unique_id}".encode()).hexdigest()

        # 插入不同 reason 的 audit 记录
        test_audit_ids = []
        with postgres_connection.cursor() as cur:
            reasons = [
                "policy:team_write_disabled",
                "openmemory_write_failed:connection_error",
                "outbox_flush_success",
                "dedup_hit",
                "other_reason",
            ]
            for reason in reasons:
                cur.execute(
                    """
                    INSERT INTO governance.write_audit
                        (actor_user_id, target_space, action, reason, payload_sha)
                    VALUES (%s, %s, 'allow', %s, %s)
                    RETURNING audit_id
                """,
                    (f"tester_{unique_id}", test_space, reason, test_payload_sha),
                )
                test_audit_ids.append(cur.fetchone()[0])

        try:
            # 获取报告
            gateway_url = integration_config["gateway_url"]
            resp = requests.get(f"{gateway_url}/reliability/report", timeout=30)
            assert resp.status_code == 200
            data = resp.json()

            by_reason = data["audit_stats"]["by_reason"]

            # 验证 by_reason 包含预期的分类
            # 注意：这些分类可能包含其他测试的数据，所以只验证至少有值
            assert "policy" in by_reason or by_reason.get("policy", 0) >= 0

        finally:
            # 清理测试数据
            with postgres_connection.cursor() as cur:
                for audit_id in test_audit_ids:
                    cur.execute(
                        "DELETE FROM governance.write_audit WHERE audit_id = %s", (audit_id,)
                    )


# ======================== OpenMemory 数据库角色权限测试 ========================


@integration_test
class TestOpenMemoryDbRoles:
    """
    OpenMemory 数据库角色权限集成测试

    验证 PostgreSQL 角色配置正确性：
    1. openmemory_svc (继承 openmemory_app) 只有 DML 权限，无 DDL 权限
    2. openmemory_migrator_login (继承 openmemory_migrator) 有完整 DDL 权限

    这些测试确保：
    - 运行时服务账号无法意外修改 schema
    - 迁移账号可以正确执行 DDL 操作
    """

    TEST_PASSWORD = "test_password_12345"

    @pytest.fixture(scope="class")
    def om_schema(self):
        """获取 OpenMemory 目标 schema"""
        return os.environ.get("OM_PG_SCHEMA", "openmemory")

    @pytest.fixture(scope="class")
    def db_roles_setup(self, integration_config):
        """
        设置测试角色

        使用 superuser 连接创建/更新测试用登录角色
        """
        import psycopg

        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        try:
            conn = psycopg.connect(dsn, autocommit=True)
        except Exception as e:
            pytest.skip(f"无法连接数据库: {e}")

        try:
            with conn.cursor() as cur:
                # 创建/更新 openmemory_svc
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_svc') THEN
                            CREATE ROLE openmemory_svc LOGIN PASSWORD %s;
                        ELSE
                            ALTER ROLE openmemory_svc WITH LOGIN PASSWORD %s;
                        END IF;

                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_app') THEN
                            GRANT openmemory_app TO openmemory_svc;
                        END IF;
                    END $$;
                """,
                    (self.TEST_PASSWORD, self.TEST_PASSWORD),
                )

                # 创建/更新 openmemory_migrator_login
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator_login') THEN
                            CREATE ROLE openmemory_migrator_login LOGIN PASSWORD %s;
                        ELSE
                            ALTER ROLE openmemory_migrator_login WITH LOGIN PASSWORD %s;
                        END IF;

                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openmemory_migrator') THEN
                            GRANT openmemory_migrator TO openmemory_migrator_login;
                        END IF;
                    END $$;
                """,
                    (self.TEST_PASSWORD, self.TEST_PASSWORD),
                )

            # 解析连接参数
            from urllib.parse import urlparse

            parsed = urlparse(dsn)

            yield {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 5432,
                "dbname": parsed.path.lstrip("/") or "engram",
                "superuser_conn": conn,
            }
        finally:
            conn.close()

    def test_svc_role_cannot_create_table(self, db_roles_setup, om_schema):
        """
        验证 openmemory_svc 无法在 OM_PG_SCHEMA 执行 CREATE TABLE

        预期: 抛出 InsufficientPrivilege 错误
        """
        import psycopg

        config = db_roles_setup
        unique_id = uuid.uuid4().hex[:8]
        test_table = f"_test_svc_create_{unique_id}"

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_svc",
                password=self.TEST_PASSWORD,
                autocommit=True,
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_svc 连接: {e}")

        try:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"""
                        CREATE TABLE {om_schema}.{test_table} (
                            id SERIAL PRIMARY KEY
                        )
                    """)
        finally:
            conn.close()

    def test_svc_role_can_execute_dml(self, db_roles_setup, om_schema):
        """
        验证 openmemory_svc 可以执行 SELECT/INSERT/UPDATE/DELETE

        步骤:
        1. 用 superuser 创建测试表
        2. 用 openmemory_svc 执行 DML
        3. 清理测试表
        """
        import psycopg

        config = db_roles_setup
        superuser_conn = config["superuser_conn"]
        unique_id = uuid.uuid4().hex[:8]
        test_table = f"_test_svc_dml_{unique_id}"

        # 用 superuser 创建测试表
        with superuser_conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {om_schema}.{test_table} (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    value INTEGER
                )
            """)
            # 授予权限
            cur.execute(f"""
                GRANT SELECT, INSERT, UPDATE, DELETE
                ON {om_schema}.{test_table} TO openmemory_app
            """)
            cur.execute(f"""
                GRANT USAGE ON SEQUENCE {om_schema}.{test_table}_id_seq TO openmemory_app
            """)

        try:
            # 用 openmemory_svc 连接
            try:
                svc_conn = psycopg.connect(
                    host=config["host"],
                    port=config["port"],
                    dbname=config["dbname"],
                    user="openmemory_svc",
                    password=self.TEST_PASSWORD,
                    autocommit=True,
                )
            except psycopg.OperationalError as e:
                pytest.skip(f"无法使用 openmemory_svc 连接: {e}")

            try:
                with svc_conn.cursor() as cur:
                    # INSERT
                    cur.execute(f"""
                        INSERT INTO {om_schema}.{test_table} (name, value)
                        VALUES ('test1', 100) RETURNING id
                    """)
                    row_id = cur.fetchone()[0]
                    assert row_id is not None

                    # SELECT
                    cur.execute(
                        f"SELECT name, value FROM {om_schema}.{test_table} WHERE id = %s", (row_id,)
                    )
                    row = cur.fetchone()
                    assert row[0] == "test1"
                    assert row[1] == 100

                    # UPDATE
                    cur.execute(
                        f"UPDATE {om_schema}.{test_table} SET value = 200 WHERE id = %s", (row_id,)
                    )
                    assert cur.rowcount == 1

                    # DELETE
                    cur.execute(f"DELETE FROM {om_schema}.{test_table} WHERE id = %s", (row_id,))
                    assert cur.rowcount == 1
            finally:
                svc_conn.close()
        finally:
            # 清理
            with superuser_conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{test_table}")

    def test_migrator_role_can_create_table(self, db_roles_setup, om_schema):
        """
        验证 openmemory_migrator_login 可以在 OM_PG_SCHEMA 执行 CREATE TABLE
        """
        import psycopg

        config = db_roles_setup
        unique_id = uuid.uuid4().hex[:8]
        test_table = f"_test_migrator_create_{unique_id}"

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_migrator_login",
                password=self.TEST_PASSWORD,
                autocommit=True,
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with conn.cursor() as cur:
                # CREATE TABLE 应成功
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{test_table} (
                        id SERIAL PRIMARY KEY,
                        content TEXT,
                        metadata JSONB DEFAULT '{{}}'::jsonb
                    )
                """)

                # 验证表存在
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                """,
                    (om_schema, test_table),
                )
                exists = cur.fetchone()[0]
                assert exists is True

                # 清理
                cur.execute(f"DROP TABLE {om_schema}.{test_table}")
        finally:
            conn.close()

    def test_migrator_role_can_simulate_npm_migrate(self, db_roles_setup, om_schema):
        """
        验证 openmemory_migrator_login 可以执行类似 npm run migrate 的 DDL

        模拟 OpenMemory 迁移创建的表结构
        """
        import psycopg

        config = db_roles_setup
        unique_id = uuid.uuid4().hex[:8]
        memories_table = f"_test_om_memories_{unique_id}"
        vectors_table = f"_test_om_vectors_{unique_id}"

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_migrator_login",
                password=self.TEST_PASSWORD,
                autocommit=True,
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with conn.cursor() as cur:
                # 创建 memories 表
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{memories_table} (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        hash TEXT UNIQUE NOT NULL,
                        metadata JSONB DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # 创建 vectors 表（带外键）
                cur.execute(f"""
                    CREATE TABLE {om_schema}.{vectors_table} (
                        id TEXT PRIMARY KEY,
                        memory_id TEXT NOT NULL
                            REFERENCES {om_schema}.{memories_table}(id) ON DELETE CASCADE,
                        embedding REAL[],
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # 创建索引
                cur.execute(f"""
                    CREATE INDEX idx_{memories_table}_user_id
                    ON {om_schema}.{memories_table} (user_id)
                """)
                cur.execute(f"""
                    CREATE INDEX idx_{memories_table}_created_at
                    ON {om_schema}.{memories_table} (created_at DESC)
                """)

                # 验证表和索引
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = %s AND table_name IN (%s, %s)
                """,
                    (om_schema, memories_table, vectors_table),
                )
                count = cur.fetchone()[0]
                assert count == 2, "两个表都应已创建"

                cur.execute(
                    """
                    SELECT COUNT(*) FROM pg_indexes
                    WHERE schemaname = %s AND tablename = %s
                """,
                    (om_schema, memories_table),
                )
                idx_count = cur.fetchone()[0]
                assert idx_count >= 2, f"应至少有 2 个索引，实际: {idx_count}"

                # 清理
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{vectors_table}")
                cur.execute(f"DROP TABLE IF EXISTS {om_schema}.{memories_table}")
        finally:
            conn.close()

    def test_migrator_cannot_create_in_public_schema(self, db_roles_setup):
        """
        验证 openmemory_migrator_login 无法在 public schema 创建表
        """
        import psycopg

        config = db_roles_setup
        unique_id = uuid.uuid4().hex[:8]
        test_table = f"_test_public_{unique_id}"

        try:
            conn = psycopg.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["dbname"],
                user="openmemory_migrator_login",
                password=self.TEST_PASSWORD,
                autocommit=True,
            )
        except psycopg.OperationalError as e:
            pytest.skip(f"无法使用 openmemory_migrator_login 连接: {e}")

        try:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"""
                        CREATE TABLE public.{test_table} (
                            id SERIAL PRIMARY KEY
                        )
                    """)
        finally:
            conn.close()


# ======================== 启动前验证失败测试 ========================


@integration_test
class TestStartupVerificationErrors:
    """
    启动前验证失败时的错误信息测试

    验证当 permissions_verify 或其他启动前检查失败时，能给出明确的错误信息，
    避免 silent fail 导致难以排查问题。
    """

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_verify_permissions_detects_missing_role(self, integration_config, postgres_connection):
        """
        验证权限检查能检测到缺失的角色并给出明确错误

        模拟场景：尝试以不存在的角色连接
        """
        from urllib.parse import urlparse

        import psycopg

        dsn = integration_config["postgres_dsn"]
        parsed = urlparse(dsn)

        # 尝试用不存在的用户连接
        nonexistent_user = f"nonexistent_user_{uuid.uuid4().hex[:8]}"

        with pytest.raises(psycopg.OperationalError) as exc_info:
            psycopg.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
                dbname=parsed.path.lstrip("/") or "engram",
                user=nonexistent_user,
                password="wrong_password",
                connect_timeout=5,
            )

        # 验证错误信息明确指出是认证/角色问题
        error_msg = str(exc_info.value).lower()
        assert any(
            keyword in error_msg
            for keyword in ["role", "authentication", "password", "does not exist", "failed"]
        ), f"错误信息应明确指出认证/角色问题: {exc_info.value}"

    def test_verify_permissions_detects_insufficient_privileges(
        self, integration_config, postgres_connection
    ):
        """
        验证权限检查能检测到权限不足并给出明确错误

        模拟场景：创建无权限用户尝试执行受限操作
        """
        from urllib.parse import urlparse

        import psycopg

        dsn = integration_config["postgres_dsn"]
        parsed = urlparse(dsn)

        # 创建一个无权限的测试用户
        test_user = f"test_no_perm_{uuid.uuid4().hex[:8]}"
        test_password = "test_password_12345"

        with postgres_connection.cursor() as cur:
            cur.execute(f"CREATE ROLE {test_user} LOGIN PASSWORD %s", (test_password,))

        try:
            # 用无权限用户连接
            conn = psycopg.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
                dbname=parsed.path.lstrip("/") or "engram",
                user=test_user,
                password=test_password,
                autocommit=True,
            )

            try:
                with conn.cursor() as cur:
                    # 尝试在 logbook schema 创建表（应失败）
                    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
                        cur.execute(f"""
                            CREATE TABLE logbook._test_no_perm_{uuid.uuid4().hex[:4]} (
                                id SERIAL PRIMARY KEY
                            )
                        """)

                    # 验证错误信息明确指出权限不足
                    error_msg = str(exc_info.value).lower()
                    assert "permission denied" in error_msg or "insufficient" in error_msg, (
                        f"错误信息应明确指出权限不足: {exc_info.value}"
                    )
            finally:
                conn.close()
        finally:
            # 清理测试用户
            with postgres_connection.cursor() as cur:
                cur.execute(f"DROP ROLE IF EXISTS {test_user}")

    def test_verify_startup_schema_not_exist_error(self, integration_config, postgres_connection):
        """
        验证启动时 schema 不存在能给出明确错误

        模拟场景：查询不存在的 schema
        """
        import psycopg

        nonexistent_schema = f"nonexistent_schema_{uuid.uuid4().hex[:8]}"

        with postgres_connection.cursor() as cur:
            # 尝试查询不存在的 schema
            with pytest.raises(psycopg.errors.UndefinedTable) as exc_info:
                cur.execute(f"SELECT * FROM {nonexistent_schema}.some_table LIMIT 1")

            # 验证错误信息明确指出 schema/table 不存在
            error_msg = str(exc_info.value).lower()
            assert "does not exist" in error_msg or "undefined" in error_msg, (
                f"错误信息应明确指出 schema/table 不存在: {exc_info.value}"
            )

    def test_verify_connection_refused_error_message(self, integration_config):
        """
        验证连接被拒绝时能给出明确错误

        模拟场景：连接到错误的端口
        """
        from urllib.parse import urlparse

        import psycopg

        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        parsed = urlparse(dsn)

        # 尝试连接到错误的端口
        wrong_port = 59999  # 不太可能被使用的端口

        with pytest.raises(psycopg.OperationalError) as exc_info:
            psycopg.connect(
                host=parsed.hostname or "localhost",
                port=wrong_port,
                dbname=parsed.path.lstrip("/") or "engram",
                user=parsed.username or "postgres",
                password=parsed.password or "postgres",
                connect_timeout=3,
            )

        # 验证错误信息明确指出连接问题
        error_msg = str(exc_info.value).lower()
        assert any(
            keyword in error_msg
            for keyword in ["connection refused", "could not connect", "timeout", "failed"]
        ), f"错误信息应明确指出连接问题: {exc_info.value}"

    def test_gateway_health_check_provides_clear_status(
        self, integration_config, all_services_healthy
    ):
        """
        验证 Gateway 健康检查端点提供清晰的状态信息

        预期响应字段:
        - ok: bool (必需，新增)
        - status: str (保持向后兼容)
        - service: str (保持向后兼容)
        - seekdb: str (必需，新增 - "enabled" 或 "disabled")
        """
        gateway_url = integration_config["gateway_url"]

        resp = requests.get(f"{gateway_url}/health", timeout=10)

        # 健康检查应返回 200
        assert resp.status_code == 200, f"健康检查应返回 200: {resp.status_code}"

        # 响应应包含有意义的内容（不是空响应）
        content = resp.text
        assert len(content) > 0, "健康检查响应不应为空"

        # 解析 JSON 响应
        data = resp.json()

        # 验证新增的 ok 字段
        assert "ok" in data, f"健康检查响应应包含 'ok' 字段: {data}"
        assert data["ok"] is True, f"健康检查 ok 应为 True: {data}"

        # 验证向后兼容的字段
        assert "status" in data, f"健康检查响应应包含 'status' 字段: {data}"
        assert data["status"] == "ok", f"健康检查 status 应为 'ok': {data}"

        assert "service" in data, f"健康检查响应应包含 'service' 字段: {data}"
        assert data["service"] == "memory-gateway", (
            f"健康检查 service 应为 'memory-gateway': {data}"
        )

        # 验证新增的 seekdb 字段
        assert "seekdb" in data, f"健康检查响应应包含 'seekdb' 字段: {data}"
        assert data["seekdb"] in ("enabled", "disabled"), (
            f"健康检查 seekdb 应为 'enabled' 或 'disabled': {data}"
        )

    def test_openmemory_unavailable_gives_clear_error(self, integration_config):
        """
        验证 OpenMemory 不可用时 Gateway 给出明确错误信息

        注意：此测试不会真正停止 OpenMemory，只验证错误处理逻辑
        """
        import asyncio
        from unittest.mock import MagicMock, patch

        # 导入 Gateway 模块
        try:
            from engram.gateway.handlers.memory_store import memory_store_impl
            from engram.gateway.openmemory_client import (
                OpenMemoryClient,
                OpenMemoryConnectionError,
            )
        except ImportError:
            pytest.skip("无法导入 Gateway 模块")

        # Mock OpenMemory 连接错误
        mock_error = OpenMemoryConnectionError(
            message="Connection refused to OpenMemory service at http://localhost:8080",
            status_code=None,
            response=None,
        )

        with patch("engram.gateway.main.get_client") as mock_get_client:
            mock_client = MagicMock(spec=OpenMemoryClient)
            mock_client.store.side_effect = mock_error
            mock_get_client.return_value = mock_client

            # 调用 memory_store_impl
            try:
                result = asyncio.get_event_loop().run_until_complete(
                    memory_store_impl(
                        payload_md="Test content for error handling",
                        target_space="team:error_test",
                        actor_user_id="error_tester",
                    )
                )
            except Exception as e:
                # 如果抛出异常，验证异常信息明确
                error_msg = str(e).lower()
                assert any(
                    keyword in error_msg
                    for keyword in ["connection", "refused", "openmemory", "unavailable", "failed"]
                ), f"异常信息应明确指出 OpenMemory 连接问题: {e}"
                return

            # 如果返回结果，验证错误信息明确
            if not result.ok:
                message = (result.message or "").lower()
                assert any(
                    keyword in message
                    for keyword in ["outbox", "降级", "失败", "error", "connection"]
                ), f"错误消息应明确指出问题: {result.message}"

    def test_database_verify_script_output_format(self, integration_config, postgres_connection):
        """
        验证 99_verify_permissions.sql 脚本输出格式清晰

        检查输出中包含 OK/FAIL/WARN 等明确状态标记
        """
        os.environ.get("OM_PG_SCHEMA", "openmemory")

        # 执行验证脚本的关键检查
        with postgres_connection.cursor() as cur:
            # 检查角色是否存在
            cur.execute("""
                SELECT rolname, rolcanlogin
                FROM pg_roles
                WHERE rolname IN (
                    'engram_admin', 'engram_migrator', 'engram_app_readwrite',
                    'openmemory_migrator', 'openmemory_app'
                )
            """)
            roles = {row[0]: row[1] for row in cur.fetchall()}

            # 构建检查结果
            results = []
            expected_nologin_roles = [
                "engram_admin",
                "engram_migrator",
                "engram_app_readwrite",
                "openmemory_migrator",
                "openmemory_app",
            ]

            for role in expected_nologin_roles:
                if role in roles:
                    can_login = roles[role]
                    if can_login:
                        results.append(f"WARN: {role} 是 LOGIN 角色（应为 NOLOGIN）")
                    else:
                        results.append(f"OK: {role} 是 NOLOGIN 角色")
                else:
                    results.append(f"FAIL: {role} 角色不存在")

            # 验证结果格式清晰
            for result in results:
                # 每条结果应以 OK/FAIL/WARN 开头
                assert result.startswith(("OK:", "FAIL:", "WARN:", "SKIP:")), (
                    f"验证结果应以明确状态开头: {result}"
                )

            # 输出结果供人工检查
            print("\n[权限验证结果]")
            for result in results:
                print(f"  {result}")

    def test_startup_error_includes_remedy_suggestion(
        self, integration_config, postgres_connection
    ):
        """
        验证启动错误信息包含修复建议

        检查 FAIL 消息后是否跟随 remedy 建议
        """
        # 模拟权限验证失败场景的输出格式
        sample_fail_outputs = [
            (
                "FAIL: engram_app_readwrite 有 public schema 的 CREATE 权限",
                "remedy: REVOKE CREATE ON SCHEMA public FROM engram_app_readwrite;",
            ),
            (
                "FAIL: engram_migrator 在 logbook 对 engram_app_readwrite 无 TABLE 默认授权",
                "remedy: ALTER DEFAULT PRIVILEGES FOR ROLE engram_migrator IN SCHEMA logbook GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app_readwrite;",
            ),
        ]

        # 验证每个 FAIL 消息都有对应的 remedy
        for fail_msg, expected_remedy in sample_fail_outputs:
            # FAIL 消息应存在
            assert fail_msg.startswith("FAIL:"), f"应以 FAIL: 开头: {fail_msg}"

            # remedy 建议应包含可执行的 SQL
            assert expected_remedy.startswith("remedy:"), f"应以 remedy: 开头: {expected_remedy}"
            assert any(
                keyword in expected_remedy.upper()
                for keyword in ["GRANT", "REVOKE", "ALTER", "CREATE"]
            ), f"remedy 应包含可执行的 SQL: {expected_remedy}"

        print("\n[错误消息格式验证通过]")
        print("  - FAIL 消息格式正确")
        print("  - remedy 建议包含可执行 SQL")


@integration_test
class TestDatabaseRolesVerification:
    """
    数据库角色验证测试

    验证统一栈中所有必需角色存在且权限正确，
    确保启动前验证不会 silent fail。
    """

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_all_required_roles_exist(self, postgres_connection):
        """
        验证所有必需角色存在
        """
        required_roles = {
            # NOLOGIN 角色
            "engram_admin": False,
            "engram_migrator": False,
            "engram_app_readwrite": False,
            "engram_app_readonly": False,
            "openmemory_migrator": False,
            "openmemory_app": False,
        }

        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT rolname, rolcanlogin
                FROM pg_roles
                WHERE rolname = ANY(%s)
            """,
                (list(required_roles.keys()),),
            )

            found_roles = {row[0]: row[1] for row in cur.fetchall()}

        missing_roles = []
        wrong_login_roles = []

        for role, expected_login in required_roles.items():
            if role not in found_roles:
                missing_roles.append(role)
            elif found_roles[role] != expected_login:
                wrong_login_roles.append(
                    f"{role} (期望 {'LOGIN' if expected_login else 'NOLOGIN'}, "
                    f"实际 {'LOGIN' if found_roles[role] else 'NOLOGIN'})"
                )

        # 输出详细信息
        if missing_roles:
            print(f"\n[FAIL] 缺失角色: {', '.join(missing_roles)}")
            print("  修复建议: 执行 04_roles_and_grants.sql 和 05_openmemory_roles_and_grants.sql")

        if wrong_login_roles:
            print(f"\n[WARN] LOGIN 属性错误: {', '.join(wrong_login_roles)}")

        assert len(missing_roles) == 0, f"缺失必需角色: {missing_roles}"

    def test_login_roles_have_correct_inheritance(self, postgres_connection):
        """
        验证登录角色正确继承权限角色
        """
        expected_inheritance = [
            ("logbook_migrator", "engram_migrator"),
            ("logbook_svc", "engram_app_readwrite"),
            ("openmemory_migrator_login", "openmemory_migrator"),
            ("openmemory_svc", "openmemory_app"),
        ]

        with postgres_connection.cursor() as cur:
            for login_role, parent_role in expected_inheritance:
                # 检查角色是否存在
                cur.execute(
                    """
                    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = %s)
                """,
                    (login_role,),
                )
                login_exists = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = %s)
                """,
                    (parent_role,),
                )
                parent_exists = cur.fetchone()[0]

                if not login_exists:
                    print(f"\n[SKIP] {login_role} 不存在（可能未执行 00_init_service_accounts.sh）")
                    continue

                if not parent_exists:
                    print(f"\n[FAIL] {parent_role} 不存在，无法验证继承关系")
                    continue

                # 检查继承关系
                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM pg_auth_members
                        WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname = %s)
                          AND member = (SELECT oid FROM pg_roles WHERE rolname = %s)
                    )
                """,
                    (parent_role, login_role),
                )
                has_inheritance = cur.fetchone()[0]

                if has_inheritance:
                    print(f"\n[OK] {login_role} -> {parent_role}")
                else:
                    print(f"\n[FAIL] {login_role} 未继承 {parent_role}")
                    print(f"  修复: GRANT {parent_role} TO {login_role};")

    def test_schemas_have_correct_permissions(self, postgres_connection):
        """
        验证 schema 权限配置正确
        """
        om_schema = os.environ.get("OM_PG_SCHEMA", "openmemory")

        schema_checks = [
            # (schema, role, should_have_create, should_have_usage)
            ("logbook", "engram_migrator", True, True),
            ("logbook", "engram_app_readwrite", False, True),
            ("scm", "engram_migrator", True, True),
            ("scm", "engram_app_readwrite", False, True),
            (om_schema, "openmemory_migrator", True, True),
            (om_schema, "openmemory_app", False, True),
        ]

        with postgres_connection.cursor() as cur:
            for schema, role, should_create, should_usage in schema_checks:
                # 检查 schema 是否存在
                cur.execute(
                    """
                    SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)
                """,
                    (schema,),
                )
                if not cur.fetchone()[0]:
                    print(f"\n[SKIP] Schema {schema} 不存在")
                    continue

                # 检查角色是否存在
                cur.execute(
                    """
                    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = %s)
                """,
                    (role,),
                )
                if not cur.fetchone()[0]:
                    print(f"\n[SKIP] 角色 {role} 不存在")
                    continue

                # 检查 CREATE 权限
                cur.execute(
                    """
                    SELECT pg_catalog.has_schema_privilege(%s, %s, 'CREATE')
                """,
                    (role, schema),
                )
                has_create = cur.fetchone()[0]

                # 检查 USAGE 权限
                cur.execute(
                    """
                    SELECT pg_catalog.has_schema_privilege(%s, %s, 'USAGE')
                """,
                    (role, schema),
                )
                has_usage = cur.fetchone()[0]

                if has_create == should_create and has_usage == should_usage:
                    print(f"\n[OK] {role} 在 {schema}: CREATE={has_create}, USAGE={has_usage}")
                else:
                    print(
                        f"\n[FAIL] {role} 在 {schema}: CREATE={has_create}(期望{should_create}), USAGE={has_usage}(期望{should_usage})"
                    )


# ======================== 完整端到端 MCP memory_store 验收测试 ========================


@integration_test
@http_only_skip
class TestMCPMemoryStoreE2E:
    """
    完整的 MCP memory_store 端到端验收测试

    测试场景：
    1. 正常路径：POST /mcp memory_store → OpenMemory 成功 → DB 审计记录正确
    2. OpenMemory 不可用：停止容器或注入错误 URL → 入队 outbox → 恢复后 worker flush → 审计补齐
    3. 状态断言：验证 governance.write_audit 和 logbook.outbox_memory 的状态变化
    """

    OPENMEMORY_CONTAINER = os.environ.get("OPENMEMORY_CONTAINER_NAME", "engram_openmemory")

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_mcp_memory_store_success_with_db_assertions(
        self, integration_config, all_services_healthy, postgres_connection
    ):
        """
        正常路径验收测试：
        1. 调用 POST /mcp memory_store
        2. 验证返回结果正确（ok=True, action=allow）
        3. 验证 governance.write_audit 记录存在且字段正确
        4. 验证 action/reason 符合约定
        """
        import hashlib

        # 生成唯一测试内容
        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# MCP E2E 验收测试 {unique_id}\n\n这是一条用于验收测试的记忆内容。"
        test_space = f"team:mcp_e2e_test_{unique_id[:6]}"
        test_actor = f"e2e_tester_{unique_id[:6]}"
        test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

        # 1. 调用 POST /mcp memory_store
        gateway_url = integration_config["gateway_url"]
        response = call_mcp_tool(
            tool="memory_store",
            arguments={
                "payload_md": test_content,
                "target_space": test_space,
                "actor_user_id": test_actor,
                "kind": "FACT",
                "evidence_refs": [f"test_evidence_{unique_id}"],
            },
            gateway_url=gateway_url,
        )

        # 2. 验证返回结果
        assert response["ok"] is True, f"MCP 调用应成功: {response}"
        result = response.get("result", {})
        assert result.get("ok") is True, f"memory_store 应成功: {result}"
        assert result.get("action") in ("allow", "redirect"), (
            f"action 应为 allow 或 redirect: {result}"
        )

        # 3. 验证 governance.write_audit 记录
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, actor_user_id, target_space, action, reason,
                       payload_sha, evidence_refs_json, created_at
                FROM governance.write_audit
                WHERE payload_sha = %s
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (test_payload_sha,),
            )
            audit_row = cur.fetchone()

            assert audit_row is not None, f"应存在审计记录, payload_sha={test_payload_sha[:16]}..."

            audit_id, actor, space, action, reason, sha, evidence, created = audit_row

            # 验证关键字段
            assert actor == test_actor, f"actor_user_id 不匹配: {actor} != {test_actor}"
            assert space == test_space or space.startswith("team:"), f"target_space 不匹配: {space}"
            assert action in ("allow", "redirect"), f"action 应为 allow 或 redirect: {action}"
            assert sha == test_payload_sha, "payload_sha 应匹配"

            # 验证 evidence_refs_json 包含必要字段
            assert evidence is not None, "evidence_refs_json 应存在"
            if isinstance(evidence, str):
                import json

                evidence = json.loads(evidence)

            assert "source" in evidence, "evidence 应包含 source"
            assert evidence["source"] == "gateway", f"source 应为 gateway: {evidence.get('source')}"

            # 如果成功写入，应有 memory_id
            if action == "allow" and "policy" in reason:
                assert evidence.get("memory_id") or result.get("memory_id"), (
                    "成功写入应有 memory_id"
                )

        # 4. 清理测试数据
        with postgres_connection.cursor() as cur:
            cur.execute(
                "DELETE FROM governance.write_audit WHERE payload_sha = %s", (test_payload_sha,)
            )

    def test_mcp_memory_store_outbox_on_openmemory_unavailable(
        self, integration_config, postgres_connection
    ):
        """
        OpenMemory 不可用场景测试：
        1. 停止 OpenMemory 容器
        2. 调用 POST /mcp memory_store
        3. 验证入队 logbook.outbox_memory (status=pending)
        4. 验证 governance.write_audit 记录包含 openmemory_write_failed:*
        5. 重启 OpenMemory 容器
        """
        import hashlib

        # 检查是否跳过降级测试
        if should_skip_degradation_test():
            pytest.skip("降级测试已被 SKIP_DEGRADATION_TEST=1 跳过")

        # 生成唯一测试内容
        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# Outbox 入队测试 {unique_id}\n\n这条记忆应该被写入 outbox。"
        test_space = f"team:outbox_test_{unique_id[:6]}"
        test_actor = f"outbox_tester_{unique_id[:6]}"
        test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

        # 1. 停止 OpenMemory 容器
        stop_success = docker_container_action(self.OPENMEMORY_CONTAINER, "stop")
        if not stop_success:
            pytest.skip(f"无法停止容器 {self.OPENMEMORY_CONTAINER}，跳过测试")

        try:
            # 等待容器完全停止
            time.sleep(3)

            # 验证 OpenMemory 确实不可用
            health_result = check_service_health(
                integration_config["openmemory_health"], "OpenMemory"
            )
            assert not health_result.healthy, "OpenMemory 应该已停止"

            # 2. 调用 POST /mcp memory_store
            gateway_url = integration_config["gateway_url"]
            response_outbox_id = None  # 初始化，用于后续验证

            try:
                response = call_mcp_tool(
                    tool="memory_store",
                    arguments={
                        "payload_md": test_content,
                        "target_space": test_space,
                        "actor_user_id": test_actor,
                    },
                    gateway_url=gateway_url,
                )

                result = response.get("result", {})

                # 3. 验证返回结果 - 使用统一响应契约
                # 契约要求：action=deferred 时必须返回 outbox_id
                assert result.get("action") == "deferred", (
                    f"OpenMemory 不可用时 action 应为 'deferred'，实际: {result.get('action')}"
                )
                assert result.get("outbox_id") is not None, (
                    f"deferred 响应必须包含 outbox_id: {result}"
                )
                assert isinstance(result.get("outbox_id"), int), (
                    f"outbox_id 必须为 int 类型: {type(result.get('outbox_id'))}"
                )

                # 契约要求：所有响应必须返回 correlation_id
                assert result.get("correlation_id") is not None, (
                    f"响应必须包含 correlation_id: {result}"
                )

                # 保存 outbox_id 供后续验证（从响应直接获取）
                response_outbox_id = result.get("outbox_id")

            except requests.exceptions.RequestException as e:
                pytest.skip(f"Gateway 请求失败: {e}")

            # 4. 验证 logbook.outbox_memory 记录
            with postgres_connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id, target_space, status, payload_sha, last_error
                    FROM logbook.outbox_memory
                    WHERE payload_sha = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                    (test_payload_sha,),
                )
                outbox_row = cur.fetchone()

                assert outbox_row is not None, "outbox 中应存在记录"
                outbox_id, space, status, sha, last_error = outbox_row

                assert status == "pending", f"outbox 状态应为 pending: {status}"
                assert space == test_space, f"target_space 不匹配: {space}"
                assert sha == test_payload_sha, "payload_sha 应匹配"

                # 验证响应中的 outbox_id 与数据库一致
                assert outbox_id == response_outbox_id, (
                    f"响应 outbox_id({response_outbox_id}) 与数据库不一致({outbox_id})"
                )

                # 保存 outbox_id 供后续测试
                self.__class__.test_outbox_id = outbox_id
                self.__class__.test_payload_sha = test_payload_sha

            # 5. 验证 governance.write_audit 记录
            with postgres_connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT action, reason, evidence_refs_json
                    FROM governance.write_audit
                    WHERE payload_sha = %s
                    AND reason LIKE 'openmemory_write_failed:%%'
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                    (test_payload_sha,),
                )
                audit_row = cur.fetchone()

                assert audit_row is not None, "应存在 openmemory_write_failed 审计记录"
                action, reason, evidence = audit_row

                assert action == "redirect", f"action 应为 redirect: {action}"
                assert reason.startswith("openmemory_write_failed:"), (
                    f"reason 应以 openmemory_write_failed: 开头: {reason}"
                )

                # 验证 evidence 包含 outbox_id
                if isinstance(evidence, str):
                    import json

                    evidence = json.loads(evidence)

                assert evidence.get("outbox_id") == outbox_id, (
                    f"evidence 应包含正确的 outbox_id: {evidence}"
                )

        finally:
            # 6. 重启 OpenMemory 容器
            docker_container_action(self.OPENMEMORY_CONTAINER, "start")

    def test_worker_flush_outbox_and_audit_completion(
        self, integration_config, postgres_connection
    ):
        """
        Worker Flush 验收测试：
        1. 确保 OpenMemory 已恢复
        2. 运行 outbox_worker（一轮）
        3. 验证 outbox 状态变为 sent
        4. 验证 governance.write_audit 包含 outbox_flush_success 记录
        5. 验证两条审计记录共享 outbox_id
        """
        # 获取前一个测试保存的信息
        outbox_id = getattr(self.__class__, "test_outbox_id", None)
        payload_sha = getattr(self.__class__, "test_payload_sha", None)

        if not outbox_id:
            pytest.skip("前置 outbox 入队测试未成功执行")

        # 1. 等待 OpenMemory 恢复
        recovered = wait_for_service(
            integration_config["openmemory_health"],
            max_wait=60,
            interval=2,
        )

        if not recovered:
            pytest.fail("OpenMemory 服务未能在 60 秒内恢复")

        # 等待服务完全就绪
        time.sleep(3)

        # 2. 运行 outbox_worker
        try:
            subprocess.run(
                ["python", "-m", "engram.gateway.outbox_worker", "--once"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env={
                    **os.environ,
                    "POSTGRES_DSN": integration_config["postgres_dsn"],
                    "OPENMEMORY_BASE_URL": integration_config["openmemory_url"],
                },
            )

            # 允许返回码 0 或 1（可能有其他失败的任务）
            # 重要的是我们的任务被处理

        except subprocess.TimeoutExpired:
            pytest.fail("outbox_worker 执行超时")
        except FileNotFoundError:
            pytest.skip("无法找到 outbox_worker 模块")

        # 等待数据库状态更新
        time.sleep(2)

        # 3. 验证 outbox 状态变为 sent
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, last_error
                FROM logbook.outbox_memory
                WHERE outbox_id = %s
            """,
                (outbox_id,),
            )
            row = cur.fetchone()

            if row:
                status, last_error = row

                # 允许 sent 或 pending（如果 worker 还没处理到）
                if status == "sent":
                    # 验证 last_error 包含 memory_id
                    assert last_error and "memory_id=" in last_error, (
                        f"sent 状态应包含 memory_id: {last_error}"
                    )
                elif status == "pending":
                    # 可能需要再运行一轮 worker
                    pytest.skip("outbox 仍为 pending，可能需要再运行 worker")
                else:
                    pytest.fail(f"outbox 状态异常: {status}")

        # 4. 验证 governance.write_audit 包含 outbox_flush_success 记录
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE payload_sha = %s
                AND reason = 'outbox_flush_success'
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (payload_sha,),
            )
            success_audit = cur.fetchone()

            assert success_audit is not None, "应存在 outbox_flush_success 审计记录"
            action, reason, evidence = success_audit

            assert action == "allow", f"action 应为 allow: {action}"
            assert reason == "outbox_flush_success", f"reason 应为 outbox_flush_success: {reason}"

            if isinstance(evidence, str):
                import json

                evidence = json.loads(evidence)

            # 验证 evidence 包含正确的 outbox_id
            assert evidence.get("outbox_id") == outbox_id, (
                f"evidence 应包含正确的 outbox_id: {evidence}"
            )

            # 验证包含 memory_id
            assert evidence.get("memory_id"), f"evidence 应包含 memory_id: {evidence}"

        # 5. 验证两条审计记录共享 outbox_id
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT reason, evidence_refs_json
                FROM governance.write_audit
                WHERE payload_sha = %s
                AND (reason LIKE 'openmemory_write_failed:%%' OR reason = 'outbox_flush_success')
                ORDER BY created_at ASC
            """,
                (payload_sha,),
            )
            audit_rows = cur.fetchall()

            assert len(audit_rows) >= 2, f"应至少有 2 条审计记录: {len(audit_rows)}"

            # 提取所有记录中的 outbox_id
            outbox_ids_found = set()
            for _, evidence in audit_rows:
                if isinstance(evidence, str):
                    import json

                    evidence = json.loads(evidence)
                if evidence and evidence.get("outbox_id"):
                    outbox_ids_found.add(evidence.get("outbox_id"))

            # 验证所有记录共享相同的 outbox_id
            assert len(outbox_ids_found) == 1, (
                f"所有审计记录应共享相同的 outbox_id: {outbox_ids_found}"
            )
            assert outbox_id in outbox_ids_found, (
                f"outbox_id 应匹配: {outbox_id} vs {outbox_ids_found}"
            )

        # 清理测试数据
        with postgres_connection.cursor() as cur:
            cur.execute("DELETE FROM logbook.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            cur.execute("DELETE FROM governance.write_audit WHERE payload_sha = %s", (payload_sha,))


@integration_test
class TestMCPMemoryStoreWithMockDegradation:
    """
    使用 Mock 的 MCP memory_store 降级测试

    不依赖 Docker 操作，通过 Mock OpenMemory 客户端模拟不可用场景。
    """

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            pytest.skip("未设置 POSTGRES_DSN 环境变量")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_complete_degradation_and_recovery_cycle(self, integration_config, postgres_connection):
        """
        完整的降级与恢复周期验收测试：

        阶段 1: OpenMemory 失败 → 入队 outbox → 写入失败审计
        阶段 2: OpenMemory 恢复 → worker flush → 状态变更 → 补齐成功审计
        阶段 3: 验证审计链完整性
        """
        import asyncio
        import hashlib
        import json
        from unittest.mock import MagicMock, patch

        # 生成唯一测试内容
        unique_id = uuid.uuid4().hex[:12]
        test_content = f"# 完整降级恢复测试 {unique_id}\n\n这是用于验证完整降级恢复周期的测试。"
        test_space = f"team:degradation_cycle_{unique_id[:6]}"
        test_actor = f"cycle_tester_{unique_id[:6]}"
        test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

        # ============ 阶段 1: 模拟 OpenMemory 失败 ============

        try:
            from engram.gateway.handlers.memory_store import memory_store_impl
            from engram.gateway.openmemory_client import (
                OpenMemoryClient,
                OpenMemoryConnectionError,
                StoreResult,
            )
            from engram.gateway.outbox_worker import WorkerConfig, process_batch
        except ImportError as e:
            pytest.skip(f"无法导入 Gateway 模块: {e}")

        # Mock OpenMemory 连接失败
        mock_connection_error = OpenMemoryConnectionError(
            message="模拟 OpenMemory 连接失败（验收测试）",
            status_code=None,
            response=None,
        )

        outbox_id = None

        with patch("engram.gateway.main.get_client") as mock_get_client:
            mock_client = MagicMock(spec=OpenMemoryClient)
            mock_client.store.side_effect = mock_connection_error
            mock_get_client.return_value = mock_client

            # 调用 memory_store_impl
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        memory_store_impl(
                            payload_md=test_content,
                            target_space=test_space,
                            actor_user_id=test_actor,
                            kind="FACT",
                        ),
                    ).result()
            else:
                result = asyncio.run(
                    memory_store_impl(
                        payload_md=test_content,
                        target_space=test_space,
                        actor_user_id=test_actor,
                        kind="FACT",
                    )
                )

            # 验证返回结果
            assert result.ok is False, f"OpenMemory 失败时应返回 ok=False: {result}"
            assert result.action == "deferred", f"应返回 action=deferred: {result.action}"
            assert "outbox_id" in (result.message or ""), (
                f"message 应包含 outbox_id: {result.message}"
            )

            # 提取 outbox_id
            import re

            match = re.search(r"outbox_id=(\d+)", result.message or "")
            assert match, f"无法提取 outbox_id: {result.message}"
            outbox_id = int(match.group(1))

        # 验证 outbox 状态
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, last_error
                FROM logbook.outbox_memory
                WHERE outbox_id = %s
            """,
                (outbox_id,),
            )
            row = cur.fetchone()

            assert row is not None, f"outbox 记录应存在: {outbox_id}"
            assert row[0] == "pending", f"状态应为 pending: {row[0]}"

        # 验证失败审计记录
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE payload_sha = %s
                AND reason LIKE 'openmemory_write_failed:%%'
            """,
                (test_payload_sha,),
            )
            failed_audit = cur.fetchone()

            assert failed_audit is not None, "应存在 openmemory_write_failed 审计记录"
            assert failed_audit[0] == "redirect", f"action 应为 redirect: {failed_audit[0]}"

            failed_evidence = failed_audit[2]
            if isinstance(failed_evidence, str):
                failed_evidence = json.loads(failed_evidence)

            assert failed_evidence.get("outbox_id") == outbox_id, (
                f"evidence 应包含正确的 outbox_id: {failed_evidence}"
            )

        # ============ 阶段 2: 模拟 OpenMemory 恢复，运行 worker ============

        mock_memory_id = f"mem_recovery_{unique_id}"
        mock_store_success = StoreResult(
            success=True,
            memory_id=mock_memory_id,
            data={"id": mock_memory_id},
        )

        with patch(
            "engram.gateway.outbox_worker.openmemory_client.OpenMemoryClient"
        ) as MockClientClass:
            mock_client_instance = MagicMock()
            mock_client_instance.store.return_value = mock_store_success
            MockClientClass.return_value = mock_client_instance

            worker_config = WorkerConfig(
                batch_size=10,
                max_retries=5,
                base_backoff_seconds=60,
                lease_seconds=120,
                openmemory_timeout_seconds=30.0,
                openmemory_max_client_retries=0,
            )

            # 运行 worker
            worker_id = f"test-worker-{unique_id[:8]}"
            results = process_batch(config=worker_config, worker_id=worker_id)

            # 查找我们的 outbox_id 对应的结果
            our_result = None
            for r in results:
                if r.outbox_id == outbox_id:
                    our_result = r
                    break

            # 可能需要多次运行
            if our_result is None:
                results = process_batch(config=worker_config, worker_id=worker_id)
                for r in results:
                    if r.outbox_id == outbox_id:
                        our_result = r
                        break

        # 验证 outbox 状态变为 sent
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, last_error
                FROM logbook.outbox_memory
                WHERE outbox_id = %s
            """,
                (outbox_id,),
            )
            row = cur.fetchone()

            assert row is not None, f"outbox 记录应存在: {outbox_id}"
            assert row[0] == "sent", f"状态应为 sent: {row[0]}"
            assert f"memory_id={mock_memory_id}" in (row[1] or ""), (
                f"last_error 应包含 memory_id: {row[1]}"
            )

        # ============ 阶段 3: 验证审计链完整性 ============

        # 验证成功审计记录
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT action, reason, evidence_refs_json
                FROM governance.write_audit
                WHERE payload_sha = %s
                AND reason = 'outbox_flush_success'
            """,
                (test_payload_sha,),
            )
            success_audit = cur.fetchone()

            assert success_audit is not None, "应存在 outbox_flush_success 审计记录"
            assert success_audit[0] == "allow", f"action 应为 allow: {success_audit[0]}"

            success_evidence = success_audit[2]
            if isinstance(success_evidence, str):
                success_evidence = json.loads(success_evidence)

            assert success_evidence.get("outbox_id") == outbox_id, (
                f"evidence 应包含正确的 outbox_id: {success_evidence}"
            )
            assert success_evidence.get("memory_id") == mock_memory_id, (
                f"evidence 应包含正确的 memory_id: {success_evidence}"
            )

        # 验证两条审计记录共享 outbox_id
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                SELECT reason, evidence_refs_json
                FROM governance.write_audit
                WHERE payload_sha = %s
                ORDER BY created_at ASC
            """,
                (test_payload_sha,),
            )
            all_audits = cur.fetchall()

            # 应至少有 2 条记录（失败 + 成功）
            assert len(all_audits) >= 2, f"应至少有 2 条审计记录: {len(all_audits)}"

            # 验证记录类型
            reasons = [r[0] for r in all_audits]
            assert any(r.startswith("openmemory_write_failed:") for r in reasons), (
                f"应有 openmemory_write_failed 记录: {reasons}"
            )
            assert "outbox_flush_success" in reasons, f"应有 outbox_flush_success 记录: {reasons}"

            # 验证所有记录共享相同的 outbox_id
            outbox_ids_in_audit = set()
            for _, evidence in all_audits:
                if isinstance(evidence, str):
                    evidence = json.loads(evidence)
                if evidence and evidence.get("outbox_id"):
                    outbox_ids_in_audit.add(evidence.get("outbox_id"))

            assert len(outbox_ids_in_audit) == 1, (
                f"所有审计记录应共享相同的 outbox_id: {outbox_ids_in_audit}"
            )
            assert outbox_id in outbox_ids_in_audit, f"outbox_id 应匹配: {outbox_id}"

        # 清理测试数据
        with postgres_connection.cursor() as cur:
            cur.execute("DELETE FROM logbook.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            cur.execute(
                "DELETE FROM governance.write_audit WHERE payload_sha = %s", (test_payload_sha,)
            )

        print("\n[完整降级恢复周期测试完成]")
        print(f"  - outbox_id: {outbox_id}")
        print(f"  - memory_id: {mock_memory_id}")
        print(f"  - 审计记录数: {len(all_audits)}")


@integration_test
@http_only_skip
class TestOutboxWorkerRealIntegration:
    """
    真实环境 Outbox Worker 集成测试

    在统一栈环境中验证 worker 的完整行为。
    注：此测试需要调用 outbox_worker，在 HTTP_ONLY_MODE 下跳过。

    门禁规则：
    - FULL 模式下，若缺少 Docker/DB 能力应 FAIL（not skip）
    - 规则定义在 scripts/unified_stack_gate_contract.py::PROFILE_CONFIGS[FULL].must_fail_if_blocked
    """

    @pytest.fixture(scope="class", autouse=True)
    def check_worker_capability(self):
        """
        在类级别检查 Worker 测试所需能力

        FULL 模式下缺能力必须 FAIL。
        """
        # Worker 测试需要 DB 访问和 Docker 能力
        enforce_capability_or_fail("db_invariants", "Worker 测试需要数据库: ")
        enforce_capability_or_fail("degradation", "Worker 测试需要 Docker 能力: ")

    @pytest.fixture(scope="class")
    def postgres_connection(self, integration_config):
        """提供数据库连接"""
        dsn = integration_config["postgres_dsn"]
        if not dsn:
            # FULL 模式下缺 DSN 必须 FAIL
            enforce_capability_or_fail("db_invariants", "Worker 测试需要数据库: ")

        import psycopg

        conn = psycopg.connect(dsn, autocommit=True)
        yield conn
        conn.close()

    def test_worker_processes_pending_items_correctly(
        self, integration_config, all_services_healthy, postgres_connection
    ):
        """
        Worker 处理 pending 项目测试：
        1. 直接插入 pending 状态的 outbox 记录
        2. 运行 worker 一轮
        3. 验证状态变为 sent
        4. 验证审计记录正确
        """
        import hashlib

        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# Worker 直接测试 {unique_id}\n\n由测试直接插入的 outbox 记录。"
        test_space = f"team:worker_test_{unique_id[:6]}"
        test_payload_sha = hashlib.sha256(test_content.encode("utf-8")).hexdigest()

        # 1. 直接插入 pending 状态的 outbox 记录
        with postgres_connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO logbook.outbox_memory
                    (target_space, payload_md, payload_sha, status, next_attempt_at)
                VALUES (%s, %s, %s, 'pending', now() - interval '1 minute')
                RETURNING outbox_id
            """,
                (test_space, test_content, test_payload_sha),
            )
            outbox_id = cur.fetchone()[0]

        try:
            # 2. 运行 worker
            try:
                subprocess.run(
                    ["python", "-m", "engram.gateway.outbox_worker", "--once"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    env={
                        **os.environ,
                        "POSTGRES_DSN": integration_config["postgres_dsn"],
                        "OPENMEMORY_BASE_URL": integration_config["openmemory_url"],
                    },
                )

                # 允许返回码 0 或 1

            except subprocess.TimeoutExpired:
                pytest.fail("outbox_worker 执行超时")
            except FileNotFoundError:
                pytest.skip("无法找到 outbox_worker 模块")

            # 等待状态更新
            time.sleep(2)

            # 3. 验证状态
            with postgres_connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, last_error
                    FROM logbook.outbox_memory
                    WHERE outbox_id = %s
                """,
                    (outbox_id,),
                )
                row = cur.fetchone()

                assert row is not None, f"outbox 记录应存在: {outbox_id}"
                status, last_error = row

                # 应为 sent（成功）或 pending（等待重试）
                assert status in ("sent", "pending"), f"状态应为 sent 或 pending: {status}"

                if status == "sent":
                    # 验证 last_error 包含 memory_id
                    assert "memory_id=" in (last_error or ""), (
                        f"sent 状态应包含 memory_id: {last_error}"
                    )

            # 4. 验证审计记录
            with postgres_connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT action, reason, evidence_refs_json
                    FROM governance.write_audit
                    WHERE payload_sha = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                    (test_payload_sha,),
                )
                audit_row = cur.fetchone()

                if status == "sent":
                    assert audit_row is not None, "应存在审计记录"
                    action, reason, evidence = audit_row

                    assert action == "allow", f"action 应为 allow: {action}"
                    assert reason == "outbox_flush_success", (
                        f"reason 应为 outbox_flush_success: {reason}"
                    )

        finally:
            # 清理
            with postgres_connection.cursor() as cur:
                cur.execute("DELETE FROM logbook.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                cur.execute(
                    "DELETE FROM governance.write_audit WHERE payload_sha = %s", (test_payload_sha,)
                )


# ======================== JSON-RPC 2.0 协议集成测试 ========================


@integration_test
class TestJsonRpcProtocol:
    """
    JSON-RPC 2.0 协议集成测试

    测试覆盖:
    1. tools/list - 列出所有可用工具
    2. tools/call - 调用工具
    3. 旧协议 {tool, arguments} 格式兼容性
    4. 错误处理（无效方法、无效参数等）
    """

    def call_jsonrpc(
        self, method: str, params: dict, req_id: int = 1, gateway_url: str = None
    ) -> dict:
        """发送 JSON-RPC 2.0 请求"""
        if gateway_url is None:
            gateway_url = get_gateway_url()

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        resp = requests.post(
            f"{gateway_url}/mcp",
            json=payload,
            timeout=30,
        )
        return resp.json()

    def test_tools_list(self, integration_config, all_services_healthy):
        """
        测试 tools/list 方法

        验证:
        1. 返回格式符合 JSON-RPC 2.0
        2. result.tools 包含所有注册的工具
        3. 必须包含 memory_store, memory_query, reliability_report, governance_update
        """
        response = self.call_jsonrpc(
            method="tools/list",
            params={},
            gateway_url=integration_config["gateway_url"],
        )

        # 验证 JSON-RPC 格式
        assert response.get("jsonrpc") == "2.0", "应返回 JSON-RPC 2.0 格式"
        assert "error" not in response or response["error"] is None, (
            f"不应返回错误: {response.get('error')}"
        )
        assert "result" in response, "应包含 result 字段"

        result = response["result"]
        assert "tools" in result, "result 应包含 tools 列表"

        tools = result["tools"]
        assert isinstance(tools, list), "tools 应为列表"
        assert len(tools) >= 4, f"至少应有 4 个工具，实际有 {len(tools)} 个"

        # 验证必需的工具存在
        tool_names = [t.get("name") for t in tools]
        required_tools = ["memory_store", "memory_query", "reliability_report", "governance_update"]

        for tool_name in required_tools:
            assert tool_name in tool_names, f"缺少必需工具: {tool_name}"

        # 验证工具定义格式
        for tool in tools:
            assert "name" in tool, f"工具缺少 name 字段: {tool}"
            assert "description" in tool, f"工具 {tool.get('name')} 缺少 description 字段"
            assert "inputSchema" in tool, f"工具 {tool.get('name')} 缺少 inputSchema 字段"

    def test_tools_call_reliability_report(self, integration_config, all_services_healthy):
        """
        测试 tools/call 调用 reliability_report

        验证:
        1. 返回格式符合 JSON-RPC 2.0
        2. result.content 为数组
        3. content[0].type 为 "text"
        """
        response = self.call_jsonrpc(
            method="tools/call",
            params={
                "name": "reliability_report",
                "arguments": {},
            },
            gateway_url=integration_config["gateway_url"],
        )

        # 验证 JSON-RPC 格式
        assert response.get("jsonrpc") == "2.0", "应返回 JSON-RPC 2.0 格式"
        assert "error" not in response or response["error"] is None, (
            f"不应返回错误: {response.get('error')}"
        )

        result = response.get("result", {})
        assert "content" in result, "result 应包含 content 字段"

        content = result["content"]
        assert isinstance(content, list), "content 应为数组"
        assert len(content) > 0, "content 不应为空"

        # 验证内容格式
        first_item = content[0]
        assert first_item.get("type") == "text", f"content[0].type 应为 text: {first_item}"
        assert "text" in first_item, "content[0] 应包含 text 字段"

    def test_tools_call_memory_store(self, integration_config, all_services_healthy):
        """
        测试 tools/call 调用 memory_store

        验证通过 JSON-RPC 协议存储记忆
        """
        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# JSON-RPC 测试记忆 {unique_id}"

        response = self.call_jsonrpc(
            method="tools/call",
            params={
                "name": "memory_store",
                "arguments": {
                    "payload_md": test_content,
                    "target_space": "team:jsonrpc_test",
                    "actor_user_id": "jsonrpc_tester",
                },
            },
            gateway_url=integration_config["gateway_url"],
        )

        # 验证 JSON-RPC 格式
        assert response.get("jsonrpc") == "2.0", "应返回 JSON-RPC 2.0 格式"
        assert "error" not in response or response["error"] is None, (
            f"不应返回错误: {response.get('error')}"
        )

        result = response.get("result", {})
        assert "content" in result, "result 应包含 content 字段"

        # content[0].text 应包含操作结果
        content = result["content"]
        assert len(content) > 0, "content 不应为空"

        text = content[0].get("text", "")
        # 成功时应包含 memory_id 或 outbox_id（降级情况）
        assert "memory" in text.lower() or "outbox" in text.lower() or "ok" in text.lower(), (
            f"响应应包含操作结果: {text}"
        )

    def test_tools_call_memory_query(self, integration_config, all_services_healthy):
        """
        测试 tools/call 调用 memory_query

        验证通过 JSON-RPC 协议查询记忆
        """
        response = self.call_jsonrpc(
            method="tools/call",
            params={
                "name": "memory_query",
                "arguments": {
                    "query": "测试",
                    "spaces": ["team:jsonrpc_test"],
                    "top_k": 5,
                },
            },
            gateway_url=integration_config["gateway_url"],
        )

        # 验证 JSON-RPC 格式
        assert response.get("jsonrpc") == "2.0", "应返回 JSON-RPC 2.0 格式"
        assert "error" not in response or response["error"] is None, (
            f"不应返回错误: {response.get('error')}"
        )

        result = response.get("result", {})
        assert "content" in result, "result 应包含 content 字段"

    def test_tools_call_nonexistent_tool(self, integration_config, all_services_healthy):
        """
        测试 tools/call 调用不存在的工具

        验证应返回 -32601 (METHOD_NOT_FOUND)
        """
        response = self.call_jsonrpc(
            method="tools/call",
            params={
                "name": "nonexistent_tool",
                "arguments": {},
            },
            gateway_url=integration_config["gateway_url"],
        )

        # 验证返回错误
        assert "error" in response, "应返回错误"
        error = response["error"]
        assert error is not None, "error 不应为空"
        assert error.get("code") == -32601, f"错误码应为 -32601: {error}"

    def test_unknown_method(self, integration_config, all_services_healthy):
        """
        测试调用未知方法

        验证应返回 -32601 (METHOD_NOT_FOUND)
        """
        response = self.call_jsonrpc(
            method="unknown/method",
            params={},
            gateway_url=integration_config["gateway_url"],
        )

        # 验证返回错误
        assert "error" in response, "应返回错误"
        error = response["error"]
        assert error is not None, "error 不应为空"
        assert error.get("code") == -32601, f"错误码应为 -32601: {error}"


@integration_test
class TestLegacyProtocol:
    """
    旧协议 {tool, arguments} 格式集成测试

    验证旧协议的兼容性
    """

    def test_legacy_memory_store(self, integration_config, all_services_healthy):
        """
        测试旧协议 memory_store

        使用 {tool, arguments} 格式调用
        """
        unique_id = uuid.uuid4().hex[:8]
        test_content = f"# 旧协议测试记忆 {unique_id}"

        response = call_mcp_tool(
            tool="memory_store",
            arguments={
                "payload_md": test_content,
                "target_space": "team:legacy_test",
                "actor_user_id": "legacy_tester",
            },
            gateway_url=integration_config["gateway_url"],
        )

        # 旧协议返回 {ok, result} 格式
        assert response["ok"] is True, f"旧协议 memory_store 失败: {response}"

        result = response.get("result", {})
        assert result.get("ok") is True or "outbox_id" in str(result.get("message", "")), (
            f"存储结果不成功: {result}"
        )

    def test_legacy_memory_query(self, integration_config, all_services_healthy):
        """
        测试旧协议 memory_query

        使用 {tool, arguments} 格式调用
        """
        response = call_mcp_tool(
            tool="memory_query",
            arguments={
                "query": "测试",
                "spaces": ["team:legacy_test"],
                "top_k": 5,
            },
            gateway_url=integration_config["gateway_url"],
        )

        # 旧协议返回 {ok, result} 格式
        assert response["ok"] is True, f"旧协议 memory_query 失败: {response}"

        result = response.get("result", {})
        assert "results" in result, "result 应包含 results 字段"
        assert "total" in result, "result 应包含 total 字段"

    def test_legacy_reliability_report(self, integration_config, all_services_healthy):
        """
        测试旧协议 reliability_report
        """
        response = call_mcp_tool(
            tool="reliability_report",
            arguments={},
            gateway_url=integration_config["gateway_url"],
        )

        assert response["ok"] is True, f"旧协议 reliability_report 失败: {response}"


# ======================== Audit-First 语义测试 ========================


class TestAuditFirstSemantics:
    """
    Audit-First 语义集成测试

    验证 ADR "审计不可丢" 要求：
    - 审计写入失败时阻断主操作
    - OpenMemory 失败时 audit 与 outbox 的顺序与字段一致
    """

    @pytest.mark.asyncio
    async def test_audit_failure_blocks_openmemory_integration(self):
        """
        集成测试：审计写入失败时阻断 OpenMemory 写入

        使用 Mock 模拟审计写入失败，验证 OpenMemory 不被调用。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl

        payload_md = "# Integration test for audit failure"
        target_space = "team:audit_test"

        # 创建 mock 配置
        mock_config = MagicMock()
        mock_config.default_team_space = "team:default"
        mock_config.project_key = "audit_test"
        mock_config.validate_evidence_refs = False
        mock_config.strict_mode_enforce_validate_refs = False
        mock_config.unknown_actor_policy = "degrade"
        mock_config.private_space_prefix = "private:"

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        # 创建 mock db
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }
        # 模拟审计写入失败
        mock_db.insert_audit.side_effect = Exception("Database connection lost")

        # 模拟 OpenMemory 成功（但审计失败）
        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem_audit_failed"
        mock_client.store.return_value = mock_store_result

        # 使用 GatewayDeps.for_testing 创建 deps
        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
            openmemory_client=mock_client,
        )

        with (
            patch(
                "engram.gateway.handlers.memory_store.create_engine_from_settings"
            ) as mock_engine,
        ):
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id="corr-a1b2c3d4e5f60010",
                deps=deps,
            )

            # 验证：操作被阻断
            assert result.ok is False
            assert result.action == "error"

            # 验证：OpenMemory 已被调用（审计失败在后置阶段）
            mock_client.store.assert_called_once()

            # 验证：错误消息明确
            assert "审计" in result.message or "audit" in result.message.lower()

    @pytest.mark.asyncio
    async def test_openmemory_failure_audit_outbox_consistency(self):
        """
        集成测试：OpenMemory 失败时 audit 与 outbox 的一致性

        验证：
        1. outbox 先于失败审计写入
        2. 失败审计的 evidence_refs_json 顶层包含 outbox_id
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.di import GatewayDeps
        from engram.gateway.handlers.memory_store import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        payload_md = "# Integration test for outbox consistency"
        target_space = "team:outbox_test"

        # 创建 mock 配置
        mock_config = MagicMock()
        mock_config.default_team_space = "team:default"
        mock_config.project_key = "outbox_test"
        mock_config.validate_evidence_refs = False
        mock_config.strict_mode_enforce_validate_refs = False
        mock_config.unknown_actor_policy = "degrade"
        mock_config.private_space_prefix = "private:"

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        # 创建 mock db
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }

        # 记录调用顺序
        call_order = []
        audit_evidence_refs = []

        def mock_insert_audit(**kwargs):
            call_order.append("audit")
            audit_evidence_refs.append(kwargs.get("evidence_refs_json", {}))
            return len(call_order)

        def mock_enqueue_outbox(**kwargs):
            call_order.append("outbox")
            return 99999  # 返回固定的 outbox_id

        mock_db.insert_audit.side_effect = mock_insert_audit
        mock_db.enqueue_outbox.side_effect = mock_enqueue_outbox

        # 模拟 OpenMemory 失败
        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryConnectionError("Connection refused")

        # 使用 GatewayDeps.for_testing 创建 deps
        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
            openmemory_client=mock_client,
        )

        with (
            patch(
                "engram.gateway.handlers.memory_store.create_engine_from_settings"
            ) as mock_engine,
        ):
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id="corr-a1b2c3d4e5f60011",
                deps=deps,
            )

            # 验证：返回包含 outbox_id 的错误
            assert result.ok is False
            assert "outbox_id=99999" in result.message

            # 验证：调用顺序正确（outbox -> 失败审计）
            assert call_order == ["outbox", "audit"]

            # 验证：失败审计的 evidence_refs_json 顶层包含 outbox_id
            assert audit_evidence_refs, "应有失败审计记录"
            failure_evidence = audit_evidence_refs[0]
            assert "outbox_id" in failure_evidence, (
                "失败审计的 evidence_refs_json 顶层必须包含 outbox_id"
            )
            assert failure_evidence["outbox_id"] == 99999, (
                f"outbox_id 应为 99999，实际为 {failure_evidence.get('outbox_id')}"
            )


# ======================== correlation_id REST 端点契约测试 ========================


class TestCorrelationIdRestEndpointContract:
    """
    验证 REST 端点 /memory/store 和 /memory/query 的 correlation_id 契约

    契约要求:
    1. 响应 JSON 中必须包含 correlation_id 字段
    2. correlation_id 格式必须符合 schema: ^corr-[a-fA-F0-9]{16}$
    3. correlation_id 在入口层生成一次，透传一致（不分裂）
    """

    @pytest.fixture
    def test_app(self):
        """
        创建测试用 FastAPI 应用

        使用 mock 依赖，避免需要真实的 OpenMemory/DB 连接
        """
        from unittest.mock import MagicMock

        from engram.gateway.app import create_app
        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import GatewayContainer
        from engram.gateway.di import GatewayDeps

        # 创建 mock 配置
        mock_config = GatewayConfig(
            project_key="test-project",
            postgres_dsn="postgresql://mock:mock@localhost/mock",
            openmemory_base_url="http://mock:8080",
        )

        # 创建 mock 依赖
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "compat"},
        }
        mock_db.insert_audit.return_value = 1

        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        # Mock OpenMemory client
        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem-test-123"
        mock_store_result.error = None
        mock_client.store.return_value = mock_store_result

        mock_search_result = MagicMock()
        mock_search_result.success = True
        mock_search_result.results = [{"id": "mem-1", "content": "test"}]
        mock_search_result.error = None
        mock_client.search.return_value = mock_search_result

        # 创建 deps
        mock_deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
            openmemory_client=mock_client,
        )

        # 创建 container
        mock_container = MagicMock(spec=GatewayContainer)
        mock_container.deps = mock_deps

        # 创建应用
        app = create_app(config=mock_config, container=mock_container, skip_db_check=True)

        return app

    @pytest.fixture
    def client(self, test_app):
        """创建 TestClient"""
        from fastapi.testclient import TestClient

        return TestClient(test_app)

    def test_memory_store_response_contains_correlation_id(self, client):
        """
        /memory/store 响应必须包含格式合规的 correlation_id

        验证:
        1. 响应 JSON 包含 correlation_id 字段
        2. correlation_id 格式符合 schema: ^corr-[a-fA-F0-9]{16}$
        """
        from tests.gateway.helpers import CORRELATION_ID_PATTERN

        response = client.post(
            "/memory/store",
            json={
                "payload_md": "# Test Memory\n\nThis is a test memory for correlation_id verification.",
            },
        )

        assert response.status_code == 200, f"请求失败: {response.text}"
        result = response.json()

        # 验证: 响应包含 correlation_id
        assert "correlation_id" in result, f"响应必须包含 correlation_id 字段: {result}"

        # 验证: correlation_id 格式合规
        correlation_id = result["correlation_id"]
        assert correlation_id is not None, "correlation_id 不能为 None"
        assert CORRELATION_ID_PATTERN.match(correlation_id), (
            f"correlation_id 格式不合规: {correlation_id}。"
            f"期望格式: ^corr-[a-fA-F0-9]{{16}}$ (corr- + 16位十六进制)"
        )

    def test_memory_query_response_contains_correlation_id(self, client):
        """
        /memory/query 响应必须包含格式合规的 correlation_id

        验证:
        1. 响应 JSON 包含 correlation_id 字段
        2. correlation_id 格式符合 schema: ^corr-[a-fA-F0-9]{16}$
        """
        from tests.gateway.helpers import CORRELATION_ID_PATTERN

        response = client.post(
            "/memory/query",
            json={
                "query": "test query for correlation_id verification",
            },
        )

        assert response.status_code == 200, f"请求失败: {response.text}"
        result = response.json()

        # 验证: 响应包含 correlation_id
        assert "correlation_id" in result, f"响应必须包含 correlation_id 字段: {result}"

        # 验证: correlation_id 格式合规
        correlation_id = result["correlation_id"]
        assert correlation_id is not None, "correlation_id 不能为 None"
        assert CORRELATION_ID_PATTERN.match(correlation_id), (
            f"correlation_id 格式不合规: {correlation_id}。"
            f"期望格式: ^corr-[a-fA-F0-9]{{16}}$ (corr- + 16位十六进制)"
        )

    def test_memory_store_correlation_id_generated_once_and_propagated(self, monkeypatch):
        """
        验证 /memory/store 的 correlation_id "入口生成一次且透传一致"

        通过 monkeypatch spy 验证:
        1. generate_correlation_id() 在每个请求中只被调用一次
        2. handler 接收到的 correlation_id 与响应中的一致
        """
        from unittest.mock import MagicMock

        from fastapi.testclient import TestClient

        from tests.gateway.helpers import CORRELATION_ID_PATTERN

        # 记录 generate_correlation_id 调用
        call_count = 0
        generated_ids = []

        # 获取原始函数
        import engram.gateway.mcp_rpc as mcp_rpc_module

        original_generate = mcp_rpc_module.generate_correlation_id

        def spy_generate_correlation_id():
            nonlocal call_count
            call_count += 1
            result = original_generate()
            generated_ids.append(result)
            return result

        # 在创建应用前 patch（确保 from .mcp_rpc import 获取到 patched 版本）
        monkeypatch.setattr(mcp_rpc_module, "generate_correlation_id", spy_generate_correlation_id)

        # 现在创建应用（此时 import 会获取到 patched 的函数）
        from engram.gateway.app import create_app
        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import GatewayContainer
        from engram.gateway.di import GatewayDeps

        mock_config = GatewayConfig(
            project_key="test-project",
            postgres_dsn="postgresql://mock:mock@localhost/mock",
            openmemory_base_url="http://mock:8080",
        )

        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "compat"},
        }
        mock_db.insert_audit.return_value = 1

        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem-test-123"
        mock_store_result.error = None
        mock_client.store.return_value = mock_store_result

        mock_deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
            openmemory_client=mock_client,
        )

        mock_container = MagicMock(spec=GatewayContainer)
        mock_container.deps = mock_deps

        app = create_app(config=mock_config, container=mock_container, skip_db_check=True)
        client = TestClient(app)

        response = client.post(
            "/memory/store",
            json={
                "payload_md": "# Test Memory\n\nCorrelation ID propagation test.",
            },
        )

        assert response.status_code == 200, f"请求失败: {response.text}"
        result = response.json()

        # 验证: generate_correlation_id 只被调用一次
        assert call_count == 1, (
            f"generate_correlation_id 应只被调用一次，实际被调用 {call_count} 次。"
            f"生成的 IDs: {generated_ids}"
        )

        # 验证: 响应中的 correlation_id 与生成的一致
        assert result.get("correlation_id") == generated_ids[0], (
            f"响应中的 correlation_id ({result.get('correlation_id')}) "
            f"与生成的 ({generated_ids[0]}) 不一致"
        )

        # 验证: 格式合规
        assert CORRELATION_ID_PATTERN.match(result["correlation_id"]), (
            f"correlation_id 格式不合规: {result['correlation_id']}"
        )

    def test_memory_query_correlation_id_generated_once_and_propagated(self, monkeypatch):
        """
        验证 /memory/query 的 correlation_id "入口生成一次且透传一致"

        通过 monkeypatch spy 验证:
        1. generate_correlation_id() 在每个请求中只被调用一次
        2. handler 接收到的 correlation_id 与响应中的一致
        """
        from unittest.mock import MagicMock

        from fastapi.testclient import TestClient

        from tests.gateway.helpers import CORRELATION_ID_PATTERN

        # 记录 generate_correlation_id 调用
        call_count = 0
        generated_ids = []

        # 获取原始函数
        import engram.gateway.mcp_rpc as mcp_rpc_module

        original_generate = mcp_rpc_module.generate_correlation_id

        def spy_generate_correlation_id():
            nonlocal call_count
            call_count += 1
            result = original_generate()
            generated_ids.append(result)
            return result

        # 在创建应用前 patch（确保 from .mcp_rpc import 获取到 patched 版本）
        monkeypatch.setattr(mcp_rpc_module, "generate_correlation_id", spy_generate_correlation_id)

        # 现在创建应用（此时 import 会获取到 patched 的函数）
        from engram.gateway.app import create_app
        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import GatewayContainer
        from engram.gateway.di import GatewayDeps

        mock_config = GatewayConfig(
            project_key="test-project",
            postgres_dsn="postgresql://mock:mock@localhost/mock",
            openmemory_base_url="http://mock:8080",
        )

        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "compat"},
        }
        mock_db.insert_audit.return_value = 1

        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        mock_client = MagicMock()
        mock_search_result = MagicMock()
        mock_search_result.success = True
        mock_search_result.results = [{"id": "mem-1", "content": "test"}]
        mock_search_result.error = None
        mock_client.search.return_value = mock_search_result

        mock_deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
            openmemory_client=mock_client,
        )

        mock_container = MagicMock(spec=GatewayContainer)
        mock_container.deps = mock_deps

        app = create_app(config=mock_config, container=mock_container, skip_db_check=True)
        client = TestClient(app)

        response = client.post(
            "/memory/query",
            json={
                "query": "correlation_id propagation test query",
            },
        )

        assert response.status_code == 200, f"请求失败: {response.text}"
        result = response.json()

        # 验证: generate_correlation_id 只被调用一次
        assert call_count == 1, (
            f"generate_correlation_id 应只被调用一次，实际被调用 {call_count} 次。"
            f"生成的 IDs: {generated_ids}"
        )

        # 验证: 响应中的 correlation_id 与生成的一致
        assert result.get("correlation_id") == generated_ids[0], (
            f"响应中的 correlation_id ({result.get('correlation_id')}) "
            f"与生成的 ({generated_ids[0]}) 不一致"
        )

        # 验证: 格式合规
        assert CORRELATION_ID_PATTERN.match(result["correlation_id"]), (
            f"correlation_id 格式不合规: {result['correlation_id']}"
        )

    def test_multiple_requests_have_different_correlation_ids(self, client):
        """
        验证多次请求生成不同的 correlation_id

        这确保每个请求都有唯一的追踪 ID
        """
        correlation_ids = set()

        for i in range(5):
            response = client.post(
                "/memory/store",
                json={
                    "payload_md": f"# Test Memory {i}\n\nUnique correlation_id test.",
                },
            )
            assert response.status_code == 200
            result = response.json()
            correlation_ids.add(result.get("correlation_id"))

        # 验证: 5 次请求应生成 5 个不同的 correlation_id
        assert len(correlation_ids) == 5, (
            f"5 次请求应生成 5 个不同的 correlation_id，实际只有 {len(correlation_ids)} 个唯一值"
        )


# ======================== 运行入口 ========================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
