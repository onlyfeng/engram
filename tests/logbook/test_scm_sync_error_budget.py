"""
test_scm_sync_error_budget.py - SCM 同步熔断机制单元测试

测试场景:
- CircuitBreakerController 状态流转（CLOSED -> OPEN -> HALF_OPEN -> CLOSED）
- 基于健康统计的熔断触发
- 半开状态的探测和恢复
- 降级参数的建议
- 熔断状态的持久化和加载
- 与 DegradationController 的集成
- 熔断 key 规范和旧 key 兼容

集成测试场景:
- 从 sync_runs 读取健康统计
- 熔断状态存储到 logbook.kv
"""

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

# 确保 scripts 目录在 path 中
scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

# 导入 key 构建函数（从 scm_sync_policy 模块导入）
from engram.logbook.scm_sync_policy import (
    CircuitBreakerConfig,
    CircuitBreakerController,
    CircuitBreakerDecision,
    CircuitState,
    DegradationConfig,
    DegradationController,
    build_circuit_breaker_key,
    get_legacy_key_fallbacks,
    normalize_instance_key_for_cb,
)


class TestCircuitBreakerConfig:
    """CircuitBreakerConfig 测试类"""

    def test_default_values(self):
        """测试默认配置值"""
        config = CircuitBreakerConfig()

        assert config.failure_rate_threshold == 0.3
        assert config.rate_limit_threshold == 0.2
        assert config.timeout_rate_threshold == 0.2
        assert config.window_count == 20
        assert config.window_minutes == 30
        assert config.open_duration_seconds == 300
        assert config.half_open_max_requests == 3
        assert config.recovery_success_count == 2
        # 新增参数
        assert config.min_samples == 5
        assert config.enable_smoothing is True
        assert config.smoothing_alpha == 0.5

    def test_from_config_with_none(self):
        """测试从 None 配置加载"""
        config = CircuitBreakerConfig.from_config(None)

        assert config.failure_rate_threshold == 0.3

    def test_from_config_with_env_override(self):
        """测试环境变量覆盖配置文件"""
        import os

        # 保存原始环境变量
        original_env = {}
        env_vars = [
            "SCM_CB_FAILURE_RATE_THRESHOLD",
            "SCM_CB_RATE_LIMIT_THRESHOLD",
            "SCM_CB_TIMEOUT_RATE_THRESHOLD",
            "SCM_CB_OPEN_DURATION_SECONDS",
            "SCM_CB_HALF_OPEN_MAX_REQUESTS",
            "SCM_CB_RECOVERY_SUCCESS_COUNT",
            "SCM_CB_BACKFILL_ONLY_MODE",
        ]
        for var in env_vars:
            original_env[var] = os.environ.get(var)

        try:
            # 设置环境变量
            os.environ["SCM_CB_FAILURE_RATE_THRESHOLD"] = "0.5"
            os.environ["SCM_CB_RATE_LIMIT_THRESHOLD"] = "0.4"
            os.environ["SCM_CB_TIMEOUT_RATE_THRESHOLD"] = "0.35"
            os.environ["SCM_CB_OPEN_DURATION_SECONDS"] = "600"
            os.environ["SCM_CB_HALF_OPEN_MAX_REQUESTS"] = "5"
            os.environ["SCM_CB_RECOVERY_SUCCESS_COUNT"] = "4"
            os.environ["SCM_CB_BACKFILL_ONLY_MODE"] = "false"

            # 模拟配置文件（环境变量应覆盖）
            mock_config = MagicMock()
            mock_config.get.side_effect = lambda key, default: {
                "scm.circuit_breaker.failure_rate_threshold": 0.1,  # 应被覆盖
                "scm.circuit_breaker.rate_limit_threshold": 0.1,  # 应被覆盖
            }.get(key, default)

            config = CircuitBreakerConfig.from_config(mock_config)

            # 验证环境变量优先于配置文件
            assert config.failure_rate_threshold == 0.5
            assert config.rate_limit_threshold == 0.4
            assert config.timeout_rate_threshold == 0.35
            assert config.open_duration_seconds == 600
            assert config.half_open_max_requests == 5
            assert config.recovery_success_count == 4
            assert config.backfill_only_mode is False
        finally:
            # 恢复原始环境变量
            for var, val in original_env.items():
                if val is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = val

    def test_from_config_fallback_to_config_file(self):
        """测试无环境变量时回退到配置文件"""
        import os

        # 确保环境变量不存在
        env_vars = ["SCM_CB_FAILURE_RATE_THRESHOLD", "SCM_CB_RATE_LIMIT_THRESHOLD"]
        original_env = {var: os.environ.pop(var, None) for var in env_vars}

        try:
            # 模拟配置文件
            mock_config = MagicMock()
            mock_config.get.side_effect = lambda key, default: {
                "scm.circuit_breaker.failure_rate_threshold": 0.45,
                "scm.circuit_breaker.rate_limit_threshold": 0.25,
            }.get(key, default)

            config = CircuitBreakerConfig.from_config(mock_config)

            # 验证使用配置文件值
            assert config.failure_rate_threshold == 0.45
            assert config.rate_limit_threshold == 0.25
        finally:
            # 恢复环境变量
            for var, val in original_env.items():
                if val is not None:
                    os.environ[var] = val

    def test_custom_thresholds(self):
        """测试自定义阈值"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.5,
            rate_limit_threshold=0.3,
            open_duration_seconds=600,
        )

        assert config.failure_rate_threshold == 0.5
        assert config.rate_limit_threshold == 0.3
        assert config.open_duration_seconds == 600


class TestCircuitBreakerController:
    """CircuitBreakerController 测试类"""

    def test_initial_state_is_closed(self):
        """测试初始状态为 CLOSED"""
        controller = CircuitBreakerController()

        assert controller.state == CircuitState.CLOSED
        assert controller.is_closed is True
        assert controller.is_open is False
        assert controller.is_half_open is False

    def test_check_returns_allow_sync_when_healthy(self):
        """测试健康状态下允许同步"""
        controller = CircuitBreakerController()

        health_stats = {
            "total_runs": 10,
            "failed_runs": 1,
            "failed_rate": 0.1,  # 10% < 30%
            "rate_limit_rate": 0.05,  # 5% < 20%
            "total_requests": 100,
            "total_timeout_count": 5,
        }

        decision = controller.check(health_stats)

        assert decision.allow_sync is True
        assert decision.is_backfill_only is False
        assert decision.current_state == "closed"

    def test_trips_on_high_failure_rate(self):
        """测试高失败率触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )
        controller = CircuitBreakerController(config=config)

        health_stats = {
            "total_runs": 10,
            "failed_runs": 4,  # 40% > 30%
            "failed_rate": 0.4,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        decision = controller.check(health_stats)

        assert controller.state == CircuitState.OPEN
        assert decision.current_state == "open"
        assert "failure_rate" in decision.trigger_reason

    def test_trips_on_high_rate_limit_rate(self):
        """测试高 429 命中率触发熔断"""
        config = CircuitBreakerConfig(
            rate_limit_threshold=0.2,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )
        controller = CircuitBreakerController(config=config)

        health_stats = {
            "total_runs": 10,
            "failed_runs": 0,
            "failed_rate": 0.0,
            "rate_limit_rate": 0.25,  # 25% > 20%
            "total_requests": 100,
            "total_429_hits": 25,
            "total_timeout_count": 0,
        }

        decision = controller.check(health_stats)

        assert controller.state == CircuitState.OPEN
        assert "rate_limit_rate" in decision.trigger_reason

    def test_trips_on_high_timeout_rate(self):
        """测试高超时率触发熔断"""
        config = CircuitBreakerConfig(
            timeout_rate_threshold=0.2,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )
        controller = CircuitBreakerController(config=config)

        health_stats = {
            "total_runs": 10,
            "failed_runs": 0,
            "failed_rate": 0.0,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 25,  # 25% > 20%
        }

        decision = controller.check(health_stats)

        assert controller.state == CircuitState.OPEN
        assert "timeout_rate" in decision.trigger_reason

    def test_not_trip_with_insufficient_data(self):
        """测试数据不足时不触发熔断"""
        # 使用默认 min_samples=5
        controller = CircuitBreakerController()

        health_stats = {
            "total_runs": 4,  # 少于 min_samples=5
            "failed_runs": 4,
            "failed_rate": 1.0,  # 100% 但数据不足
        }

        decision = controller.check(health_stats)

        assert controller.state == CircuitState.CLOSED
        assert decision.allow_sync is True

    def test_open_state_returns_degraded_params(self):
        """测试 OPEN 状态返回降级参数"""
        config = CircuitBreakerConfig(
            degraded_batch_size=10,
            degraded_forward_window_seconds=300,
            backfill_only_mode=True,
        )
        controller = CircuitBreakerController(config=config)

        # 先触发熔断
        health_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller.check(health_stats)

        # 再次检查
        decision = controller.check(health_stats)

        assert decision.is_backfill_only is True
        assert decision.suggested_batch_size == 10
        assert decision.suggested_forward_window_seconds == 300
        assert decision.suggested_diff_mode == "none"

    def test_transition_to_half_open_after_duration(self):
        """测试熔断持续时间后转换到 HALF_OPEN"""
        config = CircuitBreakerConfig(open_duration_seconds=1)  # 1秒
        controller = CircuitBreakerController(config=config)

        # 触发熔断
        controller.force_open("test")
        assert controller.state == CircuitState.OPEN

        # 等待熔断时间过去
        time.sleep(1.1)

        # 检查应转换到 HALF_OPEN
        decision = controller.check({})

        assert controller.state == CircuitState.HALF_OPEN
        assert decision.current_state == "half_open"

    def test_half_open_allows_probe_requests(self):
        """测试 HALF_OPEN 状态允许探测请求"""
        config = CircuitBreakerConfig(half_open_max_requests=3)
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN 状态
        controller._state = CircuitState.HALF_OPEN

        decision = controller.check({})

        assert decision.allow_sync is True
        assert decision.is_backfill_only is True

    def test_half_open_recovery_after_consecutive_success(self):
        """测试 HALF_OPEN 连续成功后恢复到 CLOSED"""
        config = CircuitBreakerConfig(recovery_success_count=2)
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN 状态
        controller._state = CircuitState.HALF_OPEN

        # 记录连续成功
        controller.record_result(success=True)
        assert controller.state == CircuitState.HALF_OPEN  # 还没恢复

        controller.record_result(success=True)
        assert controller.state == CircuitState.CLOSED  # 恢复

    def test_half_open_reopen_on_failure(self):
        """测试 HALF_OPEN 失败后重新进入 OPEN"""
        controller = CircuitBreakerController()

        # 进入 HALF_OPEN 状态
        controller._state = CircuitState.HALF_OPEN

        # 记录失败
        controller.record_result(success=False, error_category="timeout")

        assert controller.state == CircuitState.OPEN

    def test_force_open(self):
        """测试强制打开熔断器"""
        controller = CircuitBreakerController()

        controller.force_open("manual_test")

        assert controller.state == CircuitState.OPEN
        assert controller._last_failure_reason == "manual_test"

    def test_force_close(self):
        """测试强制关闭熔断器"""
        controller = CircuitBreakerController()
        controller.force_open("test")

        controller.force_close()

        assert controller.state == CircuitState.CLOSED
        assert controller._last_failure_reason is None

    def test_reset(self):
        """测试重置熔断器"""
        controller = CircuitBreakerController()
        controller.force_open("test")
        controller._half_open_attempts = 5

        controller.reset()

        assert controller.state == CircuitState.CLOSED
        assert controller._half_open_attempts == 0
        assert controller._opened_at is None

    def test_get_state_dict(self):
        """测试获取状态字典"""
        controller = CircuitBreakerController(key="test_key")
        controller.force_open("test_reason")

        state_dict = controller.get_state_dict()

        assert state_dict["state"] == "open"
        assert state_dict["key"] == "test_key"
        assert state_dict["last_failure_reason"] == "test_reason"
        assert state_dict["opened_at"] is not None

    def test_load_state_dict(self):
        """测试从字典加载状态"""
        controller = CircuitBreakerController()

        state_dict = {
            "state": "half_open",
            "opened_at": time.time() - 100,
            "half_open_attempts": 2,
            "half_open_successes": 1,
            "last_failure_reason": "test",
        }

        controller.load_state_dict(state_dict)

        assert controller.state == CircuitState.HALF_OPEN
        assert controller._half_open_attempts == 2
        assert controller._half_open_successes == 1

    def test_load_state_dict_with_invalid_state(self):
        """测试加载无效状态时回退到 CLOSED"""
        controller = CircuitBreakerController()

        state_dict = {
            "state": "invalid_state",
        }

        controller.load_state_dict(state_dict)

        assert controller.state == CircuitState.CLOSED


class TestCircuitBreakerDecision:
    """CircuitBreakerDecision 测试类"""

    def test_to_dict(self):
        """测试转换为字典"""
        decision = CircuitBreakerDecision(
            allow_sync=False,
            is_backfill_only=True,
            suggested_batch_size=10,
            suggested_diff_mode="none",
            wait_seconds=30.0,
            current_state="open",
            trigger_reason="failure_rate=40%",
        )

        d = decision.to_dict()

        assert d["allow_sync"] is False
        assert d["is_backfill_only"] is True
        assert d["suggested_batch_size"] == 10
        assert d["suggested_diff_mode"] == "none"
        assert d["wait_seconds"] == 30.0
        assert d["current_state"] == "open"
        assert d["trigger_reason"] == "failure_rate=40%"


class TestCircuitBreakerGradualRecovery:
    """测试熔断器渐进恢复"""

    def test_gradual_batch_size_recovery(self):
        """测试 batch_size 渐进恢复"""
        config = CircuitBreakerConfig(
            degraded_batch_size=10,
            recovery_success_count=3,
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN 状态
        controller._state = CircuitState.HALF_OPEN

        # 第一次检查：使用最小 batch_size
        decision1 = controller.check({})
        assert decision1.suggested_batch_size >= 10

        # 记录成功
        controller.record_result(success=True)

        # 第二次检查：batch_size 应该增加
        decision2 = controller.check({})
        assert decision2.suggested_batch_size > decision1.suggested_batch_size

    def test_diff_mode_recovery(self):
        """测试 diff_mode 恢复"""
        config = CircuitBreakerConfig(recovery_success_count=2)
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN 状态
        controller._state = CircuitState.HALF_OPEN

        # 第一次检查：应该是 none
        decision1 = controller.check({})
        assert decision1.suggested_diff_mode == "none"

        # 记录成功
        controller.record_result(success=True)

        # 第二次检查：应该恢复到 best_effort
        decision2 = controller.check({})
        assert decision2.suggested_diff_mode == "best_effort"


class TestCircuitBreakerWithDegradationController:
    """测试熔断器与 DegradationController 的集成"""

    def test_combined_degradation(self):
        """测试熔断器与降级控制器协同工作"""
        # 创建降级控制器
        degradation_config = DegradationConfig(
            rate_limit_threshold=3,
            default_batch_size=100,
        )
        degradation_controller = DegradationController(config=degradation_config)

        # 创建熔断控制器
        circuit_config = CircuitBreakerConfig(failure_rate_threshold=0.3)
        circuit_breaker = CircuitBreakerController(config=circuit_config)

        # 模拟健康统计
        health_stats = {
            "total_runs": 10,
            "failed_runs": 2,  # 20% < 30%，不触发熔断
            "failed_rate": 0.2,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        # 检查熔断
        circuit_decision = circuit_breaker.check(health_stats)
        assert circuit_decision.allow_sync is True

        # 更新降级控制器（模拟有一些 429 错误）
        degradation_suggestion = degradation_controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=10,
        )

        # 降级控制器应该缩小 batch_size
        assert degradation_suggestion.batch_size < 100


class TestCircuitBreakerStateTransitions:
    """测试熔断器状态流转"""

    def test_full_state_cycle(self):
        """测试完整的状态循环: CLOSED -> OPEN -> HALF_OPEN -> CLOSED"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=0.1,  # 100ms
            recovery_success_count=2,
        )
        controller = CircuitBreakerController(config=config)

        # 1. 初始状态: CLOSED
        assert controller.state == CircuitState.CLOSED

        # 2. 高失败率触发熔断 -> OPEN
        bad_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller.check(bad_stats)
        assert controller.state == CircuitState.OPEN

        # 3. 等待熔断时间 -> HALF_OPEN
        time.sleep(0.15)
        controller.check({})
        assert controller.state == CircuitState.HALF_OPEN

        # 4. 连续成功 -> CLOSED
        controller.record_result(success=True)
        controller.record_result(success=True)
        assert controller.state == CircuitState.CLOSED

    def test_half_open_reopen_cycle(self):
        """测试 HALF_OPEN 失败后重新熔断"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            recovery_success_count=3,
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        controller.check({})
        assert controller.state == CircuitState.HALF_OPEN

        # 成功一次
        controller.record_result(success=True)
        assert controller.state == CircuitState.HALF_OPEN

        # 然后失败，应该重新 OPEN
        controller.record_result(success=False)
        assert controller.state == CircuitState.OPEN


class TestCircuitBreakerEdgeCases:
    """测试边界情况"""

    def test_empty_health_stats(self):
        """测试空健康统计"""
        controller = CircuitBreakerController()

        decision = controller.check({})

        assert decision.allow_sync is True
        assert controller.state == CircuitState.CLOSED

    def test_none_health_stats(self):
        """测试 None 健康统计"""
        controller = CircuitBreakerController()

        decision = controller.check(None)

        assert decision.allow_sync is True

    def test_zero_total_requests(self):
        """测试零请求时不触发超时率熔断"""
        controller = CircuitBreakerController()

        health_stats = {
            "total_runs": 10,
            "failed_rate": 0.1,
            "rate_limit_rate": 0.0,
            "total_requests": 0,  # 零请求
            "total_timeout_count": 10,
        }

        controller.check(health_stats)

        # 不应该因为超时率触发熔断（因为无法计算）
        assert controller.state == CircuitState.CLOSED

    def test_exactly_at_threshold(self):
        """测试恰好在阈值边界"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )
        controller = CircuitBreakerController(config=config)

        health_stats = {
            "total_runs": 10,
            "failed_rate": 0.3,  # 恰好等于阈值
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        controller.check(health_stats)

        # 等于阈值应该触发
        assert controller.state == CircuitState.OPEN


# ============ 集成测试（需要数据库）============


class TestCircuitBreakerIntegration:
    """集成测试（标记为需要数据库的测试）"""

    @pytest.fixture
    def migrated_db(self):
        """数据库迁移 fixture（跳过如果无数据库）"""
        pytest.skip("需要数据库环境")

    def test_health_stats_from_db(self, migrated_db):
        """测试从数据库读取健康统计"""
        # 此测试需要真实数据库环境
        pass

    def test_circuit_breaker_state_persistence(self, migrated_db):
        """测试熔断状态持久化"""
        # 此测试需要真实数据库环境
        pass


# ============ Key 规范测试 ============


class TestCircuitBreakerKeySpec:
    """熔断 Key 规范测试类"""

    def test_build_key_default_global(self):
        """测试默认全局 key 构建"""
        key = build_circuit_breaker_key()
        assert key == "default:global"

    def test_build_key_with_project_key(self):
        """测试带 project_key 的 key 构建"""
        key = build_circuit_breaker_key(project_key="myproject")
        assert key == "myproject:global"

    def test_build_key_with_pool_name(self):
        """测试带 pool_name 的 key 构建"""
        key = build_circuit_breaker_key(pool_name="gitlab-prod")
        assert key == "default:pool:gitlab-prod"

    def test_build_key_with_project_and_pool(self):
        """测试带 project_key 和 pool_name 的 key 构建"""
        key = build_circuit_breaker_key(project_key="myproject", pool_name="svn-only")
        assert key == "myproject:pool:svn-only"

    def test_build_key_with_explicit_scope(self):
        """测试显式 scope 的 key 构建"""
        key = build_circuit_breaker_key(project_key="myproject", scope="pool:custom")
        assert key == "myproject:pool:custom"

    def test_build_key_pool_name_overrides_scope(self):
        """测试 pool_name 覆盖 scope"""
        key = build_circuit_breaker_key(scope="global", pool_name="override")
        assert key == "default:pool:override"

    def test_build_key_with_none_project_key(self):
        """测试 project_key 为 None 时使用默认值"""
        key = build_circuit_breaker_key(project_key=None)
        assert key == "default:global"

    def test_build_key_with_empty_project_key(self):
        """测试 project_key 为空字符串时使用默认值"""
        key = build_circuit_breaker_key(project_key="")
        assert key == "default:global"


class TestLegacyKeyFallbacks:
    """旧 Key 格式回退测试类"""

    def test_fallback_for_global_key(self):
        """测试全局 key 的旧格式回退"""
        fallbacks = get_legacy_key_fallbacks("default:global")
        assert "global" in fallbacks

    def test_fallback_for_pool_key(self):
        """测试 pool key 的旧格式回退"""
        fallbacks = get_legacy_key_fallbacks("default:pool:gitlab-prod")
        assert "pool:gitlab-prod" in fallbacks
        assert "gitlab-prod" in fallbacks

    def test_no_fallback_for_worker_key(self):
        """测试 worker key 不应作为回退"""
        fallbacks = get_legacy_key_fallbacks("worker:abc123")
        # worker 开头的 key 不应该在回退列表中
        assert "worker:abc123" not in fallbacks

    def test_fallback_list_order(self):
        """测试回退列表顺序（更具体的优先）"""
        fallbacks = get_legacy_key_fallbacks("default:pool:test")
        # 验证回退列表存在
        assert len(fallbacks) >= 1

    def test_empty_key_returns_empty_fallbacks(self):
        """测试空 key 返回空回退列表"""
        fallbacks = get_legacy_key_fallbacks("")
        assert fallbacks == []

    def test_none_key_returns_empty_fallbacks(self):
        """测试 None key 返回空回退列表"""
        fallbacks = get_legacy_key_fallbacks(None)
        assert fallbacks == []


class TestCircuitBreakerWithNewKey:
    """使用新 Key 规范的熔断器测试"""

    def test_controller_with_pool_key(self):
        """测试使用 pool key 的熔断控制器"""
        controller = CircuitBreakerController(key="default:pool:gitlab-prod")

        assert controller._key == "default:pool:gitlab-prod"
        assert controller.state == CircuitState.CLOSED

    def test_controller_with_global_key(self):
        """测试使用全局 key 的熔断控制器"""
        controller = CircuitBreakerController(key="myproject:global")

        assert controller._key == "myproject:global"
        assert controller.state == CircuitState.CLOSED

    def test_state_dict_contains_new_key(self):
        """测试状态字典包含新 key 格式"""
        controller = CircuitBreakerController(key="default:pool:test")
        state_dict = controller.get_state_dict()

        assert state_dict["key"] == "default:pool:test"

    def test_load_state_dict_preserves_key(self):
        """测试加载状态字典保持 key"""
        controller = CircuitBreakerController(key="original:global")

        state_dict = {
            "state": "closed",
            "key": "different:key",  # 不同的 key
        }

        controller.load_state_dict(state_dict)

        # 加载后 controller 的 _key 应该保持不变（_key 是构造时设置的）
        assert controller._key == "original:global"


class TestWorkerPoolKeyBuilding:
    """Worker Pool Key 构建测试"""

    def test_global_key_when_no_pool(self):
        """测试无 pool 配置时使用全局 key"""
        # 模拟 _build_worker_circuit_breaker_key 的逻辑
        project_key = "default"
        pool_name = None
        instance_allowlist = None
        tenant_allowlist = None

        if pool_name:
            key = build_circuit_breaker_key(project_key=project_key, pool_name=pool_name)
        elif instance_allowlist:
            pool_id = instance_allowlist[0].replace(".", "-")
            key = build_circuit_breaker_key(
                project_key=project_key, pool_name=f"instance-{pool_id}"
            )
        elif tenant_allowlist:
            pool_id = tenant_allowlist[0]
            key = build_circuit_breaker_key(project_key=project_key, pool_name=f"tenant-{pool_id}")
        else:
            key = build_circuit_breaker_key(project_key=project_key, scope="global")

        assert key == "default:global"

    def test_pool_key_with_pool_name(self):
        """测试使用 pool_name 构建 pool key"""
        project_key = "default"
        pool_name = "gitlab-prod"

        key = build_circuit_breaker_key(project_key=project_key, pool_name=pool_name)

        assert key == "default:pool:gitlab-prod"

    def test_pool_key_from_instance_allowlist(self):
        """测试从 instance_allowlist 推断 pool key"""
        project_key = "default"
        instance_allowlist = ["gitlab.example.com"]

        pool_id = instance_allowlist[0].replace(".", "-")
        key = build_circuit_breaker_key(project_key=project_key, pool_name=f"instance-{pool_id}")

        assert key == "default:pool:instance-gitlab-example-com"

    def test_pool_key_from_tenant_allowlist(self):
        """测试从 tenant_allowlist 推断 pool key"""
        project_key = "default"
        tenant_allowlist = ["tenant-a"]

        key = build_circuit_breaker_key(
            project_key=project_key, pool_name=f"tenant-{tenant_allowlist[0]}"
        )

        assert key == "default:pool:tenant-tenant-a"

    def test_pool_name_takes_precedence(self):
        """测试 pool_name 优先于 allowlist"""
        project_key = "myproject"
        pool_name = "explicit-pool"

        # pool_name 应该优先使用
        key = build_circuit_breaker_key(project_key=project_key, pool_name=pool_name)

        assert key == "myproject:pool:explicit-pool"
        assert "ignored" not in key


class TestCircuitBreakerKeyScopeIsolation:
    """
    熔断 Key 作用域隔离测试

    验证场景：
    - 不同 key 的熔断器状态相互独立
    - per-instance 熔断不影响全局熔断器
    - 全局熔断不影响其他 scope 的熔断器
    - project_key 不同时熔断器完全隔离
    """

    def test_different_keys_have_independent_states(self):
        """不同 key 的熔断器状态相互独立"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )

        # 创建两个不同 key 的熔断器
        global_breaker = CircuitBreakerController(config=config, key="default:global")
        instance_breaker = CircuitBreakerController(config=config, key="default:pool:gitlab-prod")

        # 让一个熔断，另一个保持正常
        bad_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,  # 50% > 30%
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        good_stats = {
            "total_runs": 10,
            "failed_rate": 0.1,  # 10% < 30%
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        # instance 熔断器使用坏的统计数据
        instance_decision = instance_breaker.check(bad_stats)
        # global 熔断器使用好的统计数据
        global_decision = global_breaker.check(good_stats)

        # instance 应该 OPEN，global 应该 CLOSED
        assert instance_breaker.state == CircuitState.OPEN
        assert global_breaker.state == CircuitState.CLOSED

        # 决策应该反映各自状态
        assert instance_decision.current_state == "open"
        assert global_decision.current_state == "closed"
        assert global_decision.allow_sync is True

    def test_per_instance_breaker_does_not_affect_global(self):
        """per-instance 熔断不影响全局熔断器"""
        config = CircuitBreakerConfig(failure_rate_threshold=0.3)

        # 创建全局熔断器和多个实例熔断器
        global_breaker = CircuitBreakerController(config=config, key="default:global")
        instance_a_breaker = CircuitBreakerController(config=config, key="default:pool:instance-a")
        instance_b_breaker = CircuitBreakerController(config=config, key="default:pool:instance-b")

        # instance-a 强制熔断
        instance_a_breaker.force_open("instance-a high failure rate")

        # 验证 global 和 instance-b 不受影响
        assert instance_a_breaker.state == CircuitState.OPEN
        assert global_breaker.state == CircuitState.CLOSED
        assert instance_b_breaker.state == CircuitState.CLOSED

    def test_global_breaker_open_does_not_affect_instance_breakers(self):
        """全局熔断不影响实例级熔断器"""
        config = CircuitBreakerConfig(failure_rate_threshold=0.3)

        global_breaker = CircuitBreakerController(config=config, key="default:global")
        instance_breaker = CircuitBreakerController(config=config, key="default:pool:gitlab-prod")

        # 全局强制熔断
        global_breaker.force_open("global circuit breaker triggered")

        # 验证实例熔断器不受影响
        assert global_breaker.state == CircuitState.OPEN
        assert instance_breaker.state == CircuitState.CLOSED

    def test_different_project_keys_fully_isolated(self):
        """不同 project_key 的熔断器完全隔离"""
        config = CircuitBreakerConfig(failure_rate_threshold=0.3)

        project_a_breaker = CircuitBreakerController(config=config, key="project-a:global")
        project_b_breaker = CircuitBreakerController(config=config, key="project-b:global")

        # project-a 熔断
        project_a_breaker.force_open("project-a issues")

        # project-b 不受影响
        assert project_a_breaker.state == CircuitState.OPEN
        assert project_b_breaker.state == CircuitState.CLOSED

    def test_state_dict_preserves_key_scope(self):
        """状态字典正确保留 key scope 信息"""
        config = CircuitBreakerConfig()

        breakers = [
            CircuitBreakerController(config=config, key="default:global"),
            CircuitBreakerController(config=config, key="default:pool:prod"),
            CircuitBreakerController(config=config, key="project-x:pool:instance-1"),
        ]

        for breaker in breakers:
            state_dict = breaker.get_state_dict()
            assert "key" in state_dict
            assert state_dict["key"] == breaker._key

    def test_force_close_only_affects_target_key(self):
        """force_close 只影响目标 key 的熔断器"""
        config = CircuitBreakerConfig()

        breaker_a = CircuitBreakerController(config=config, key="key-a")
        breaker_b = CircuitBreakerController(config=config, key="key-b")

        # 两个都强制打开
        breaker_a.force_open("test")
        breaker_b.force_open("test")

        assert breaker_a.state == CircuitState.OPEN
        assert breaker_b.state == CircuitState.OPEN

        # 只关闭 breaker_a
        breaker_a.force_close()

        # breaker_b 应该仍然是 OPEN
        assert breaker_a.state == CircuitState.CLOSED
        assert breaker_b.state == CircuitState.OPEN


class TestPerInstanceCircuitBreakerIsolation:
    """
    Per-instance 熔断隔离测试

    验证场景：
    - 单个 instance 熔断时，其他 instance 正常工作
    - instance 熔断恢复后独立恢复
    - 多个 instance 可以同时处于不同状态
    """

    def test_single_instance_breaker_isolated(self):
        """单个 instance 熔断时其他 instance 正常"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            rate_limit_threshold=0.2,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )

        # 创建多个 instance 熔断器
        breakers = {
            "gitlab-a.com": CircuitBreakerController(
                config=config, key="default:pool:gitlab-a.com"
            ),
            "gitlab-b.com": CircuitBreakerController(
                config=config, key="default:pool:gitlab-b.com"
            ),
            "gitlab-c.com": CircuitBreakerController(
                config=config, key="default:pool:gitlab-c.com"
            ),
        }

        # instance-a 健康统计差
        bad_stats_a = {
            "total_runs": 10,
            "failed_rate": 0.5,
            "rate_limit_rate": 0.3,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        # 其他 instance 健康统计好
        good_stats = {
            "total_runs": 10,
            "failed_rate": 0.1,
            "rate_limit_rate": 0.05,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        # 检查各个熔断器
        decisions = {}
        decisions["gitlab-a.com"] = breakers["gitlab-a.com"].check(bad_stats_a)
        decisions["gitlab-b.com"] = breakers["gitlab-b.com"].check(good_stats)
        decisions["gitlab-c.com"] = breakers["gitlab-c.com"].check(good_stats)

        # 验证隔离性
        assert breakers["gitlab-a.com"].state == CircuitState.OPEN
        assert breakers["gitlab-b.com"].state == CircuitState.CLOSED
        assert breakers["gitlab-c.com"].state == CircuitState.CLOSED

        # 验证决策
        assert (
            decisions["gitlab-a.com"].allow_sync is False
            or decisions["gitlab-a.com"].is_backfill_only is True
        )
        assert decisions["gitlab-b.com"].allow_sync is True
        assert decisions["gitlab-c.com"].allow_sync is True

    def test_instance_breaker_independent_recovery(self):
        """instance 熔断器独立恢复"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=0.1,
            recovery_success_count=2,
        )

        breaker_a = CircuitBreakerController(config=config, key="default:pool:instance-a")
        breaker_b = CircuitBreakerController(config=config, key="default:pool:instance-b")

        # 两个都熔断
        breaker_a.force_open("test")
        breaker_b.force_open("test")

        # 等待熔断时间
        time.sleep(0.15)

        # 两个都进入 HALF_OPEN
        breaker_a.check({})
        breaker_b.check({})

        assert breaker_a.state == CircuitState.HALF_OPEN
        assert breaker_b.state == CircuitState.HALF_OPEN

        # instance-a 恢复成功
        breaker_a.record_result(success=True)
        breaker_a.record_result(success=True)

        # instance-b 继续失败
        breaker_b.record_result(success=False)

        # 验证独立恢复
        assert breaker_a.state == CircuitState.CLOSED
        assert breaker_b.state == CircuitState.OPEN

    def test_multiple_instances_different_states(self):
        """多个 instance 可以同时处于不同状态"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=0.1,
            recovery_success_count=2,
        )

        # 创建 5 个 instance 熔断器
        instances = ["inst-1", "inst-2", "inst-3", "inst-4", "inst-5"]
        breakers = {
            inst: CircuitBreakerController(config=config, key=f"default:pool:{inst}")
            for inst in instances
        }

        # 设置不同状态
        # inst-1: CLOSED (正常)
        # inst-2: OPEN (熔断)
        breakers["inst-2"].force_open("high failure rate")

        # inst-3: HALF_OPEN (恢复中)
        breakers["inst-3"].force_open("test")
        time.sleep(0.15)
        breakers["inst-3"].check({})

        # inst-4: CLOSED (已恢复)
        breakers["inst-4"].force_open("test")
        time.sleep(0.15)
        breakers["inst-4"].check({})
        breakers["inst-4"].record_result(success=True)
        breakers["inst-4"].record_result(success=True)

        # inst-5: OPEN (重新熔断)
        breakers["inst-5"].force_open("test")
        time.sleep(0.15)
        breakers["inst-5"].check({})
        breakers["inst-5"].record_result(success=False)

        # 验证各自状态
        assert breakers["inst-1"].state == CircuitState.CLOSED
        assert breakers["inst-2"].state == CircuitState.OPEN
        assert breakers["inst-3"].state == CircuitState.HALF_OPEN
        assert breakers["inst-4"].state == CircuitState.CLOSED
        assert breakers["inst-5"].state == CircuitState.OPEN

    def test_instance_breaker_stats_isolation(self):
        """instance 熔断器统计数据隔离"""
        config = CircuitBreakerConfig(recovery_success_count=3)

        breaker_a = CircuitBreakerController(config=config, key="default:pool:inst-a")
        breaker_b = CircuitBreakerController(config=config, key="default:pool:inst-b")

        # 进入 HALF_OPEN
        breaker_a._state = CircuitState.HALF_OPEN
        breaker_b._state = CircuitState.HALF_OPEN

        # instance-a 记录 2 次成功
        breaker_a.record_result(success=True)
        breaker_a.record_result(success=True)

        # instance-b 记录 1 次成功
        breaker_b.record_result(success=True)

        # 验证计数器独立
        assert breaker_a._half_open_successes == 2
        assert breaker_b._half_open_successes == 1

        # 验证状态（都还在 HALF_OPEN，因为没达到 recovery_success_count=3）
        assert breaker_a.state == CircuitState.HALF_OPEN
        assert breaker_b.state == CircuitState.HALF_OPEN


# ============ 小样本保护测试 ============


class TestMinSamplesProtection:
    """
    小样本保护测试

    验证场景：
    - 样本数低于 min_samples 时不触发熔断
    - 样本数达到 min_samples 时正常触发熔断
    - 自定义 min_samples 配置生效
    """

    def test_not_trip_with_samples_below_threshold(self):
        """样本数低于 min_samples 时不触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=5,  # 需要至少 5 个样本
        )
        controller = CircuitBreakerController(config=config)

        # 只有 4 个样本，即使全部失败也不应触发熔断
        health_stats = {
            "total_runs": 4,  # 低于 min_samples=5
            "failed_runs": 4,
            "failed_rate": 1.0,  # 100% 失败率
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        decision = controller.check(health_stats)

        # 不应触发熔断
        assert controller.state == CircuitState.CLOSED
        assert decision.allow_sync is True
        assert decision.current_state == "closed"

    def test_trips_when_samples_reach_threshold(self):
        """样本数达到 min_samples 时正常触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=5,
            enable_smoothing=False,  # 禁用平滑以便测试
        )
        controller = CircuitBreakerController(config=config)

        # 恰好 5 个样本，高失败率应该触发熔断
        health_stats = {
            "total_runs": 5,  # 等于 min_samples=5
            "failed_runs": 3,
            "failed_rate": 0.6,  # 60% > 30%
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        decision = controller.check(health_stats)

        # 应触发熔断
        assert controller.state == CircuitState.OPEN
        assert decision.current_state == "open"
        assert "failure_rate" in decision.trigger_reason

    def test_not_trip_with_exactly_min_samples_and_good_stats(self):
        """样本数恰好等于 min_samples 且统计正常时不触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=5,
        )
        controller = CircuitBreakerController(config=config)

        health_stats = {
            "total_runs": 5,
            "failed_runs": 1,
            "failed_rate": 0.2,  # 20% < 30%
            "rate_limit_rate": 0.1,  # 10% < 20%
            "total_requests": 100,
            "total_timeout_count": 5,
        }

        decision = controller.check(health_stats)

        # 不应触发熔断
        assert controller.state == CircuitState.CLOSED
        assert decision.allow_sync is True

    def test_custom_min_samples_value(self):
        """自定义 min_samples 配置生效"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=10,  # 更高的阈值
            enable_smoothing=False,
        )
        controller = CircuitBreakerController(config=config)

        # 7 个样本，低于 min_samples=10
        health_stats_7 = {
            "total_runs": 7,
            "failed_rate": 0.8,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        controller.check(health_stats_7)
        assert controller.state == CircuitState.CLOSED  # 不触发

        # 10 个样本，达到 min_samples=10
        health_stats_10 = {
            "total_runs": 10,
            "failed_rate": 0.8,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        controller.check(health_stats_10)
        assert controller.state == CircuitState.OPEN  # 触发

    def test_min_samples_zero_allows_immediate_trip(self):
        """min_samples=0 时允许立即触发"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=0,  # 无最小样本限制
            enable_smoothing=False,
        )
        controller = CircuitBreakerController(config=config)

        # 只有 1 个样本
        health_stats = {
            "total_runs": 1,
            "failed_rate": 1.0,
            "rate_limit_rate": 0.0,
            "total_requests": 10,
            "total_timeout_count": 0,
        }

        controller.check(health_stats)
        assert controller.state == CircuitState.OPEN  # 立即触发


# ============ 平滑策略测试 ============


class TestSmoothingStrategy:
    """
    平滑策略测试

    验证场景：
    - 启用平滑时减少抖动
    - 平滑系数影响收敛速度
    - 禁用平滑时直接使用原始值
    - 平滑状态持久化
    """

    def test_smoothing_reduces_jitter(self):
        """平滑策略减少抖动：突然高失败率不立即触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=3,
            enable_smoothing=True,
            smoothing_alpha=0.3,  # 较小的 alpha 意味着更强的平滑
        )
        controller = CircuitBreakerController(config=config)

        # 第一次检查：正常状态
        good_stats = {
            "total_runs": 10,
            "failed_rate": 0.1,  # 10%
            "rate_limit_rate": 0.05,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller.check(good_stats)
        assert controller.state == CircuitState.CLOSED

        # 记录平滑后的值
        first_smoothed = controller._smoothed_failure_rate
        assert first_smoothed is not None
        assert abs(first_smoothed - 0.1) < 0.01  # 首次应接近原始值

        # 第二次检查：突然高失败率（抖动）
        # 如果没有平滑，0.5 > 0.3 会立即触发
        # 但使用 alpha=0.3 的平滑后：
        # smoothed = 0.3 * 0.5 + 0.7 * 0.1 = 0.15 + 0.07 = 0.22 < 0.3
        spike_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,  # 突然升高到 50%
            "rate_limit_rate": 0.05,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        decision = controller.check(spike_stats)

        # 由于平滑，不应立即触发熔断
        assert controller.state == CircuitState.CLOSED
        assert decision.allow_sync is True

        # 验证平滑后的值确实低于原始值
        second_smoothed = controller._smoothed_failure_rate
        assert second_smoothed < 0.5  # 平滑后应低于原始值
        assert second_smoothed > 0.1  # 但高于之前的平滑值

    def test_sustained_high_failure_rate_eventually_triggers(self):
        """持续高失败率最终会触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=3,
            enable_smoothing=True,
            smoothing_alpha=0.5,
        )
        controller = CircuitBreakerController(config=config)

        # 持续发送高失败率统计
        high_failure_stats = {
            "total_runs": 10,
            "failed_rate": 0.6,  # 60%
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        # 多次检查，平滑值会逐渐收敛到高失败率
        for i in range(10):
            controller.check(high_failure_stats)
            if controller.state == CircuitState.OPEN:
                break

        # 最终应该触发熔断
        assert controller.state == CircuitState.OPEN

    def test_smoothing_alpha_affects_convergence_speed(self):
        """平滑系数影响收敛速度"""
        # 高 alpha（0.9）= 弱平滑，快速收敛到新值
        config_fast = CircuitBreakerConfig(
            failure_rate_threshold=0.9,  # 设高阈值避免触发
            min_samples=1,
            enable_smoothing=True,
            smoothing_alpha=0.9,
        )
        controller_fast = CircuitBreakerController(config=config_fast)

        # 低 alpha（0.1）= 强平滑，缓慢收敛
        config_slow = CircuitBreakerConfig(
            failure_rate_threshold=0.9,
            min_samples=1,
            enable_smoothing=True,
            smoothing_alpha=0.1,
        )
        controller_slow = CircuitBreakerController(config=config_slow)

        # 初始化：低失败率
        init_stats = {
            "total_runs": 10,
            "failed_rate": 0.1,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller_fast.check(init_stats)
        controller_slow.check(init_stats)

        # 突然高失败率
        high_stats = {
            "total_runs": 10,
            "failed_rate": 0.8,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller_fast.check(high_stats)
        controller_slow.check(high_stats)

        # 高 alpha 的控制器平滑值应更接近新值
        assert controller_fast._smoothed_failure_rate > controller_slow._smoothed_failure_rate

    def test_smoothing_disabled_uses_raw_values(self):
        """禁用平滑时直接使用原始值"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=3,
            enable_smoothing=False,  # 禁用平滑
        )
        controller = CircuitBreakerController(config=config)

        # 第一次检查：正常
        good_stats = {
            "total_runs": 10,
            "failed_rate": 0.1,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller.check(good_stats)
        assert controller.state == CircuitState.CLOSED

        # 第二次检查：突然高失败率，应立即触发
        high_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,  # 50% > 30%
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        decision = controller.check(high_stats)

        # 禁用平滑时，应立即触发熔断
        assert controller.state == CircuitState.OPEN
        assert "failure_rate=50.00%" in decision.trigger_reason

    def test_smoothed_state_persistence(self):
        """平滑状态持久化"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.9,
            min_samples=1,
            enable_smoothing=True,
            smoothing_alpha=0.5,
        )
        controller = CircuitBreakerController(config=config)

        # 进行一些检查以建立平滑状态
        stats = {
            "total_runs": 10,
            "failed_rate": 0.3,
            "rate_limit_rate": 0.15,
            "total_requests": 100,
            "total_timeout_count": 10,
        }
        controller.check(stats)

        # 获取状态字典
        state_dict = controller.get_state_dict()

        # 验证平滑状态被保存
        assert "smoothed_failure_rate" in state_dict
        assert "smoothed_rate_limit_rate" in state_dict
        assert "smoothed_timeout_rate" in state_dict
        assert state_dict["smoothed_failure_rate"] is not None

        # 创建新控制器并加载状态
        new_controller = CircuitBreakerController(config=config, key="new")
        new_controller.load_state_dict(state_dict)

        # 验证平滑状态被恢复
        assert new_controller._smoothed_failure_rate == state_dict["smoothed_failure_rate"]
        assert new_controller._smoothed_rate_limit_rate == state_dict["smoothed_rate_limit_rate"]
        assert new_controller._smoothed_timeout_rate == state_dict["smoothed_timeout_rate"]

    def test_reset_clears_smoothed_state(self):
        """重置熔断器清除平滑状态"""
        config = CircuitBreakerConfig(enable_smoothing=True)
        controller = CircuitBreakerController(config=config)

        # 建立平滑状态
        stats = {
            "total_runs": 10,
            "failed_rate": 0.2,
            "rate_limit_rate": 0.1,
            "total_requests": 100,
            "total_timeout_count": 5,
        }
        controller.check(stats)

        assert controller._smoothed_failure_rate is not None

        # 重置
        controller.reset()

        # 平滑状态应被清除
        assert controller._smoothed_failure_rate is None
        assert controller._smoothed_rate_limit_rate is None
        assert controller._smoothed_timeout_rate is None

    def test_smoothing_with_rate_limit_rate(self):
        """平滑策略对 429 命中率也生效"""
        config = CircuitBreakerConfig(
            rate_limit_threshold=0.2,
            min_samples=3,
            enable_smoothing=True,
            smoothing_alpha=0.3,
        )
        controller = CircuitBreakerController(config=config)

        # 正常状态
        good_stats = {
            "total_runs": 10,
            "failed_rate": 0.0,
            "rate_limit_rate": 0.05,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller.check(good_stats)
        assert controller.state == CircuitState.CLOSED

        # 突然高 429 命中率（抖动）
        # 平滑后：0.3 * 0.5 + 0.7 * 0.05 = 0.15 + 0.035 = 0.185 < 0.2
        spike_stats = {
            "total_runs": 10,
            "failed_rate": 0.0,
            "rate_limit_rate": 0.5,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        controller.check(spike_stats)

        # 平滑后不应立即触发
        assert controller.state == CircuitState.CLOSED


class TestCircuitBreakerWithSchedulerIntegration:
    """
    熔断器与调度器集成测试

    验证场景：
    - 全局熔断时所有任务被暂停
    - instance 熔断时仅该 instance 的任务被暂停
    - 熔断决策影响调度优先级
    """

    def test_global_breaker_blocks_all_jobs(self):
        """全局熔断时所有任务被阻断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            enable_smoothing=False,  # 禁用平滑以便立即触发
        )
        global_breaker = CircuitBreakerController(config=config, key="default:global")

        # 触发全局熔断
        bad_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }

        decision = global_breaker.check(bad_stats)

        # 验证所有同步被阻断（或进入 backfill_only 模式）
        assert decision.current_state == "open"
        assert decision.is_backfill_only is True or decision.allow_sync is False

    def test_instance_breaker_only_blocks_that_instance_repos(self):
        """instance 熔断仅阻断该 instance 的 repo"""
        from engram.logbook.scm_sync_policy import CircuitBreakerDecision

        CircuitBreakerConfig(failure_rate_threshold=0.3)

        # 模拟调度场景：有 3 个 repo，来自 2 个不同 instance
        repos = [
            {"repo_id": 1, "instance": "gitlab-bad.com"},
            {"repo_id": 2, "instance": "gitlab-bad.com"},
            {"repo_id": 3, "instance": "gitlab-good.com"},
        ]

        # instance 熔断决策
        instance_decisions = {
            "gitlab-bad.com": CircuitBreakerDecision(
                allow_sync=False,
                current_state="open",
            ),
            "gitlab-good.com": CircuitBreakerDecision(
                allow_sync=True,
                current_state="closed",
            ),
        }

        # 模拟调度过滤
        allowed_repos = []
        blocked_repos = []

        for repo in repos:
            instance = repo["instance"]
            if instance in instance_decisions:
                if instance_decisions[instance].allow_sync:
                    allowed_repos.append(repo)
                else:
                    blocked_repos.append(repo)

        # 验证过滤结果
        assert len(blocked_repos) == 2  # gitlab-bad.com 的 repo 1 和 2
        assert len(allowed_repos) == 1  # gitlab-good.com 的 repo 3
        assert allowed_repos[0]["repo_id"] == 3


# ============ Key 隔离增强测试 ============


class TestKeyIsolationEnhanced:
    """
    Key 隔离增强测试

    验证不同维度的 key 构建和隔离：
    - instance_key 参数
    - tenant_id 参数
    - worker_pool 参数
    - normalize_instance_key_for_cb 函数
    """

    def test_build_key_with_instance_key(self):
        """测试带 instance_key 的 key 构建"""
        key = build_circuit_breaker_key(instance_key="https://gitlab.example.com/group/project")
        assert key == "default:instance:gitlab.example.com"

    def test_build_key_with_hostname_instance_key(self):
        """测试 hostname 形式的 instance_key"""
        key = build_circuit_breaker_key(instance_key="gitlab.example.com")
        assert key == "default:instance:gitlab.example.com"

    def test_build_key_with_tenant_id(self):
        """测试带 tenant_id 的 key 构建"""
        key = build_circuit_breaker_key(tenant_id="tenant-abc")
        assert key == "default:tenant:tenant-abc"

    def test_build_key_with_worker_pool(self):
        """测试带 worker_pool 的 key 构建"""
        key = build_circuit_breaker_key(worker_pool="gitlab-prod-pool")
        assert key == "default:pool:gitlab-prod-pool"

    def test_worker_pool_overrides_pool_name(self):
        """测试 worker_pool 优先于 pool_name"""
        key = build_circuit_breaker_key(pool_name="old-pool", worker_pool="new-pool")
        assert key == "default:pool:new-pool"

    def test_pool_takes_precedence_over_instance_and_tenant(self):
        """测试 pool 参数优先级高于 instance 和 tenant"""
        key = build_circuit_breaker_key(
            worker_pool="explicit-pool",
            instance_key="gitlab.example.com",
            tenant_id="tenant-a",
        )
        assert key == "default:pool:explicit-pool"

    def test_instance_takes_precedence_over_tenant(self):
        """测试 instance 参数优先级高于 tenant（无 pool 时）"""
        key = build_circuit_breaker_key(
            instance_key="gitlab.example.com",
            tenant_id="tenant-a",
        )
        assert key == "default:instance:gitlab.example.com"

    def test_normalize_instance_key_extracts_hostname_from_url(self):
        """测试 normalize_instance_key_for_cb 从 URL 提取 hostname"""
        assert (
            normalize_instance_key_for_cb("https://gitlab.example.com/path") == "gitlab.example.com"
        )
        assert (
            normalize_instance_key_for_cb("http://GITLAB.EXAMPLE.COM:8080/api")
            == "gitlab.example.com:8080"
        )

    def test_normalize_instance_key_lowercases_hostname(self):
        """测试 normalize_instance_key_for_cb 转小写"""
        assert normalize_instance_key_for_cb("GITLAB.EXAMPLE.COM") == "gitlab.example.com"
        assert normalize_instance_key_for_cb("GitLab.Example.Com") == "gitlab.example.com"

    def test_normalize_instance_key_handles_none_and_empty(self):
        """测试 normalize_instance_key_for_cb 处理 None 和空字符串"""
        assert normalize_instance_key_for_cb(None) is None
        assert normalize_instance_key_for_cb("") is None
        assert normalize_instance_key_for_cb("   ") is None

    def test_multiple_breakers_same_instance_different_projects(self):
        """测试同一 instance 不同 project 的 key 隔离"""
        key1 = build_circuit_breaker_key(project_key="project-a", instance_key="gitlab.example.com")
        key2 = build_circuit_breaker_key(project_key="project-b", instance_key="gitlab.example.com")

        assert key1 == "project-a:instance:gitlab.example.com"
        assert key2 == "project-b:instance:gitlab.example.com"
        assert key1 != key2


# ============ 重启后加载一致性测试 ============


class TestStatePersistenceConsistency:
    """
    重启后加载一致性测试

    验证场景：
    - 完整状态序列化/反序列化一致
    - 各状态字段正确保留
    - 边界情况处理
    """

    def test_state_dict_roundtrip_closed(self):
        """测试 CLOSED 状态的序列化/反序列化一致性"""
        config = CircuitBreakerConfig()
        original = CircuitBreakerController(config=config, key="test:global")

        # 保持 CLOSED 状态，进行一些检查
        stats = {
            "total_runs": 10,
            "failed_rate": 0.1,
            "rate_limit_rate": 0.05,
            "total_requests": 100,
            "total_timeout_count": 5,
        }
        original.check(stats)

        # 序列化
        state_dict = original.get_state_dict()

        # 反序列化到新实例
        restored = CircuitBreakerController(config=config, key="test:global")
        restored.load_state_dict(state_dict)

        # 验证状态一致
        assert restored.state == original.state
        assert restored._opened_at == original._opened_at
        assert restored._half_open_attempts == original._half_open_attempts
        assert restored._half_open_successes == original._half_open_successes
        assert restored._last_failure_reason == original._last_failure_reason
        # 验证平滑状态一致
        assert restored._smoothed_failure_rate == original._smoothed_failure_rate
        assert restored._smoothed_rate_limit_rate == original._smoothed_rate_limit_rate
        assert restored._smoothed_timeout_rate == original._smoothed_timeout_rate

    def test_state_dict_roundtrip_open(self):
        """测试 OPEN 状态的序列化/反序列化一致性"""
        config = CircuitBreakerConfig()
        original = CircuitBreakerController(config=config, key="test:global")

        # 强制进入 OPEN 状态
        original.force_open("test_reason")

        # 序列化
        state_dict = original.get_state_dict()

        # 验证序列化内容
        assert state_dict["state"] == "open"
        assert state_dict["last_failure_reason"] == "test_reason"
        assert state_dict["opened_at"] is not None

        # 反序列化到新实例
        restored = CircuitBreakerController(config=config, key="test:global")
        restored.load_state_dict(state_dict)

        # 验证状态一致
        assert restored.state == CircuitState.OPEN
        assert restored._last_failure_reason == "test_reason"
        assert restored._opened_at == original._opened_at

    def test_state_dict_roundtrip_half_open(self):
        """测试 HALF_OPEN 状态的序列化/反序列化一致性"""
        config = CircuitBreakerConfig(recovery_success_count=3)
        original = CircuitBreakerController(config=config, key="test:global")

        # 进入 HALF_OPEN 状态并记录一些探测结果
        original._state = CircuitState.HALF_OPEN
        original._opened_at = time.time() - 100
        original.record_result(success=True)
        original.record_result(success=True)

        # 序列化
        state_dict = original.get_state_dict()

        # 验证序列化内容
        assert state_dict["state"] == "half_open"
        assert state_dict["half_open_attempts"] == 2
        assert state_dict["half_open_successes"] == 2

        # 反序列化到新实例
        restored = CircuitBreakerController(config=config, key="test:global")
        restored.load_state_dict(state_dict)

        # 验证状态一致
        assert restored.state == CircuitState.HALF_OPEN
        assert restored._half_open_attempts == 2
        assert restored._half_open_successes == 2

        # 再记录一次成功应该恢复到 CLOSED
        restored.record_result(success=True)
        assert restored.state == CircuitState.CLOSED

    def test_state_dict_contains_all_required_fields(self):
        """测试状态字典包含所有必需字段"""
        controller = CircuitBreakerController(key="test:key")

        # 建立一些状态
        stats = {
            "total_runs": 10,
            "failed_rate": 0.2,
            "rate_limit_rate": 0.1,
            "total_requests": 50,
            "total_timeout_count": 3,
        }
        controller.check(stats)

        state_dict = controller.get_state_dict()

        # 验证必需字段存在
        required_fields = [
            "state",
            "opened_at",
            "half_open_attempts",
            "half_open_successes",
            "last_failure_reason",
            "key",
            "smoothed_failure_rate",
            "smoothed_rate_limit_rate",
            "smoothed_timeout_rate",
        ]

        for field in required_fields:
            assert field in state_dict, f"Missing required field: {field}"

    def test_load_state_dict_handles_missing_optional_fields(self):
        """测试加载状态字典时处理缺失的可选字段"""
        controller = CircuitBreakerController(key="test:key")

        # 只提供最少的字段
        minimal_state = {
            "state": "closed",
        }

        # 应该不抛出异常
        controller.load_state_dict(minimal_state)

        assert controller.state == CircuitState.CLOSED
        assert controller._opened_at is None
        assert controller._half_open_attempts == 0
        assert controller._half_open_successes == 0

    def test_state_persistence_across_simulated_restart(self):
        """测试模拟重启后状态恢复"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=300,
            recovery_success_count=2,
            enable_smoothing=True,
            smoothing_alpha=0.5,
        )

        # 创建原始控制器并建立状态
        original = CircuitBreakerController(config=config, key="prod:pool:gitlab-main")

        # 进行一些检查建立平滑状态
        for _ in range(5):
            original.check(
                {
                    "total_runs": 10,
                    "failed_rate": 0.15,
                    "rate_limit_rate": 0.08,
                    "total_requests": 100,
                    "total_timeout_count": 3,
                }
            )

        # 触发熔断
        original.force_open("high_failure_rate")

        # 模拟持久化（保存到某处）
        persisted_state = original.get_state_dict()

        # === 模拟重启 ===

        # 创建新控制器实例（模拟进程重启）
        restored = CircuitBreakerController(config=config, key="prod:pool:gitlab-main")

        # 从持久化状态恢复
        restored.load_state_dict(persisted_state)

        # 验证关键状态恢复
        assert restored.state == CircuitState.OPEN
        assert restored._last_failure_reason == "high_failure_rate"
        assert restored._smoothed_failure_rate is not None

        # 验证后续行为正确（例如：时间到后可以转换到 HALF_OPEN）
        # 注意：这里不测试时间相关的转换，因为 opened_at 已被恢复


# ============ HALF_OPEN 探测配额测试 ============


class TestHalfOpenProbeQuota:
    """
    HALF_OPEN 探测配额测试

    验证场景：
    - probe_budget_per_interval 配置生效
    - is_probe_mode 标志正确设置
    - probe_job_types_allowlist 限制生效
    - 探测配额在决策中正确传递
    """

    def test_half_open_decision_includes_probe_mode_flag(self):
        """测试 HALF_OPEN 决策包含 probe_mode 标志"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            probe_budget_per_interval=2,
            probe_job_types_allowlist=["commits"],
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        assert controller.state == CircuitState.HALF_OPEN
        assert decision.is_probe_mode is True
        assert decision.probe_budget == 2
        assert decision.probe_job_types_allowlist == ["commits"]

    def test_closed_state_not_in_probe_mode(self):
        """测试 CLOSED 状态不是 probe 模式"""
        config = CircuitBreakerConfig()
        controller = CircuitBreakerController(config=config)

        decision = controller.check(
            {
                "total_runs": 10,
                "failed_rate": 0.1,
                "rate_limit_rate": 0.05,
                "total_requests": 100,
                "total_timeout_count": 0,
            }
        )

        assert controller.state == CircuitState.CLOSED
        assert decision.is_probe_mode is False
        assert decision.probe_budget == 0

    def test_open_state_not_in_probe_mode(self):
        """测试 OPEN 状态不是 probe 模式（在熔断时间内）"""
        config = CircuitBreakerConfig(
            open_duration_seconds=300,  # 长熔断时间
            enable_smoothing=False,
        )
        controller = CircuitBreakerController(config=config)

        # 触发熔断
        controller.check(
            {
                "total_runs": 10,
                "failed_rate": 0.5,
                "rate_limit_rate": 0.0,
                "total_requests": 100,
                "total_timeout_count": 0,
            }
        )

        assert controller.state == CircuitState.OPEN

        # 再次检查（仍在熔断时间内）
        decision = controller.check({})

        assert decision.is_probe_mode is False
        assert decision.probe_budget == 0

    def test_probe_budget_configurable(self):
        """测试 probe_budget 可配置"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            probe_budget_per_interval=5,
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        assert decision.probe_budget == 5

    def test_probe_job_types_allowlist_configurable(self):
        """测试 probe_job_types_allowlist 可配置"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            probe_job_types_allowlist=["commits", "mrs"],
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        assert decision.probe_job_types_allowlist == ["commits", "mrs"]

    def test_probe_job_types_empty_list_allows_all(self):
        """测试空的 probe_job_types_allowlist 允许所有类型"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            probe_job_types_allowlist=[],  # 空列表
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        assert decision.probe_job_types_allowlist == []

    def test_half_open_attempts_tracked_independently(self):
        """测试 HALF_OPEN 尝试次数独立跟踪"""
        config = CircuitBreakerConfig(
            half_open_max_requests=5,
            recovery_success_count=3,  # 需要 3 次连续成功才能恢复
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller._state = CircuitState.HALF_OPEN

        # 记录第一次探测成功
        controller.record_result(success=True)
        assert controller._half_open_attempts == 1
        assert controller._half_open_successes == 1
        assert controller.state == CircuitState.HALF_OPEN  # 还未恢复

        # 记录第二次探测成功
        controller.record_result(success=True)
        assert controller._half_open_attempts == 2
        assert controller._half_open_successes == 2
        assert controller.state == CircuitState.HALF_OPEN  # 还未恢复

        # 记录第三次探测成功，达到 recovery_success_count=3
        controller.record_result(success=True)
        # 恢复到 CLOSED 后计数器会被重置
        assert controller.state == CircuitState.CLOSED

    def test_half_open_max_requests_exceeded_reopens_on_insufficient_success(self):
        """测试超过最大探测次数但成功不足时重新熔断"""
        config = CircuitBreakerConfig(
            half_open_max_requests=3,
            recovery_success_count=3,  # 需要 3 次成功
        )
        controller = CircuitBreakerController(config=config)

        # 进入 HALF_OPEN
        controller._state = CircuitState.HALF_OPEN

        # 记录 2 次成功，1 次失败
        controller.record_result(success=True)
        controller.record_result(success=True)
        # 失败会重置成功计数并重新熔断
        controller.record_result(success=False)

        # 应该重新进入 OPEN
        assert controller.state == CircuitState.OPEN

    def test_decision_to_dict_includes_probe_fields(self):
        """测试决策的 to_dict 包含探测相关字段"""
        decision = CircuitBreakerDecision(
            allow_sync=True,
            is_probe_mode=True,
            probe_budget=3,
            probe_job_types_allowlist=["commits", "mrs"],
        )

        d = decision.to_dict()

        assert d["is_probe_mode"] is True
        assert d["probe_budget"] == 3
        assert d["probe_job_types_allowlist"] == ["commits", "mrs"]

    def test_probe_budget_from_env_config(self):
        """测试从环境变量加载 probe_budget 配置"""
        import os

        original_env = {
            "SCM_CB_PROBE_BUDGET_PER_INTERVAL": os.environ.get("SCM_CB_PROBE_BUDGET_PER_INTERVAL"),
            "SCM_CB_PROBE_JOB_TYPES_ALLOWLIST": os.environ.get("SCM_CB_PROBE_JOB_TYPES_ALLOWLIST"),
        }

        try:
            os.environ["SCM_CB_PROBE_BUDGET_PER_INTERVAL"] = "10"
            os.environ["SCM_CB_PROBE_JOB_TYPES_ALLOWLIST"] = "commits,mrs,reviews"

            config = CircuitBreakerConfig.from_config(None)

            assert config.probe_budget_per_interval == 10
            assert config.probe_job_types_allowlist == ["commits", "mrs", "reviews"]
        finally:
            # 恢复环境变量
            for key, val in original_env.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


# ============ 序列化字段命名一致性测试 ============


class TestSerializationFieldNaming:
    """
    序列化字段命名一致性测试

    验证场景：
    - get_state_dict 和 load_state_dict 使用一致的字段名
    - 字段名遵循一致的命名规范（snake_case）
    - 无冗余或遗漏字段
    """

    def test_state_dict_field_names_snake_case(self):
        """测试状态字典字段名使用 snake_case"""
        controller = CircuitBreakerController(key="test")
        controller.force_open("test")

        state_dict = controller.get_state_dict()

        for key in state_dict.keys():
            # 验证是 snake_case（小写字母、数字、下划线）
            assert key == key.lower(), f"Field '{key}' is not lowercase"
            assert " " not in key, f"Field '{key}' contains space"
            assert "-" not in key, f"Field '{key}' contains hyphen (should use underscore)"

    def test_state_dict_load_matches_save_fields(self):
        """测试 load_state_dict 识别 get_state_dict 的所有字段"""
        config = CircuitBreakerConfig(enable_smoothing=True)

        # 创建一个有状态的控制器
        original = CircuitBreakerController(config=config, key="original")
        original.force_open("test_reason")
        original._half_open_attempts = 5
        original._half_open_successes = 3
        original._smoothed_failure_rate = 0.25
        original._smoothed_rate_limit_rate = 0.15
        original._smoothed_timeout_rate = 0.10

        # 序列化
        state_dict = original.get_state_dict()

        # 反序列化
        restored = CircuitBreakerController(config=config, key="restored")
        restored.load_state_dict(state_dict)

        # 获取恢复后的状态
        restored_state = restored.get_state_dict()

        # 比较关键字段（排除 key，因为它是构造时设置的）
        compare_fields = [
            "state",
            "opened_at",
            "half_open_attempts",
            "half_open_successes",
            "last_failure_reason",
            "smoothed_failure_rate",
            "smoothed_rate_limit_rate",
            "smoothed_timeout_rate",
        ]

        for field in compare_fields:
            assert state_dict.get(field) == restored_state.get(field), (
                f"Field '{field}' mismatch: {state_dict.get(field)} != {restored_state.get(field)}"
            )

    def test_decision_to_dict_complete_fields(self):
        """测试 CircuitBreakerDecision.to_dict 包含所有字段"""
        decision = CircuitBreakerDecision(
            allow_sync=True,
            is_backfill_only=True,
            suggested_batch_size=50,
            suggested_forward_window_seconds=600,
            suggested_diff_mode="none",
            wait_seconds=30.0,
            next_allowed_at=time.time() + 30,
            current_state="half_open",
            trigger_reason="test_reason",
            health_stats={"test": 1},
            is_probe_mode=True,
            probe_budget=2,
            probe_job_types_allowlist=["commits"],
        )

        d = decision.to_dict()

        # 验证所有字段存在
        expected_fields = [
            "allow_sync",
            "is_backfill_only",
            "suggested_batch_size",
            "suggested_forward_window_seconds",
            "suggested_diff_mode",
            "wait_seconds",
            "next_allowed_at",
            "current_state",
            "trigger_reason",
            "health_stats",
            "is_probe_mode",
            "probe_budget",
            "probe_job_types_allowlist",
        ]

        for field in expected_fields:
            assert field in d, f"Missing field: {field}"

    def test_backward_compatible_state_loading(self):
        """测试向后兼容的状态加载（旧格式状态字典）"""
        controller = CircuitBreakerController(key="test")

        # 模拟旧版本的状态字典（缺少新字段）
        old_state_dict = {
            "state": "open",
            "opened_at": time.time() - 100,
            "half_open_attempts": 1,
            "half_open_successes": 0,
            "last_failure_reason": "old_reason",
            # 缺少 smoothed_* 字段
        }

        # 应该能正常加载
        controller.load_state_dict(old_state_dict)

        assert controller.state == CircuitState.OPEN
        assert controller._last_failure_reason == "old_reason"
        # 新字段应该是默认值（None）
        assert controller._smoothed_failure_rate is None


# ============ Probe 模式端到端测试 ============


class TestProbeModePolicyIntegration:
    """
    Probe 模式与 Policy 层集成测试

    验证场景：
    - probe_job_types_allowlist 过滤在 policy 层的正确应用
    - probe_budget 限制在多 job_type 场景下的行为
    - probe 模式下 CircuitBreakerDecision 字段传递一致性
    """

    def test_probe_decision_fields_propagate_correctly(self):
        """测试 probe 决策字段正确传递"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            probe_budget_per_interval=3,
            probe_job_types_allowlist=["commits", "mrs"],
        )
        controller = CircuitBreakerController(config=config, key="test")

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        assert decision.is_probe_mode is True
        assert decision.probe_budget == 3
        assert decision.probe_job_types_allowlist == ["commits", "mrs"]

        # 验证 to_dict 输出
        d = decision.to_dict()
        assert d["is_probe_mode"] is True
        assert d["probe_budget"] == 3
        assert d["probe_job_types_allowlist"] == ["commits", "mrs"]

    def test_probe_allowlist_empty_means_all_allowed(self):
        """测试空 allowlist 意味着允许所有 job_type"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            probe_budget_per_interval=5,
            probe_job_types_allowlist=[],  # 空列表
        )
        controller = CircuitBreakerController(config=config, key="test")

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        assert decision.is_probe_mode is True
        assert decision.probe_job_types_allowlist == []

        # 空 allowlist 时过滤逻辑不应排除任何类型
        all_types = ["commits", "mrs", "reviews"]
        if decision.probe_job_types_allowlist:
            filtered = [t for t in all_types if t in decision.probe_job_types_allowlist]
        else:
            filtered = all_types

        assert filtered == all_types

    def test_probe_mode_transitions_correctly(self):
        """测试 probe 模式在状态流转中正确变化"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=0.1,
            recovery_success_count=2,
            probe_budget_per_interval=2,
            enable_smoothing=False,
        )
        controller = CircuitBreakerController(config=config, key="test")

        # 初始状态：CLOSED，非 probe 模式
        decision = controller.check(
            {
                "total_runs": 10,
                "failed_rate": 0.1,
                "rate_limit_rate": 0.0,
                "total_requests": 100,
                "total_timeout_count": 0,
            }
        )
        assert decision.is_probe_mode is False
        assert controller.state == CircuitState.CLOSED

        # 触发熔断：OPEN，非 probe 模式
        bad_stats = {
            "total_runs": 10,
            "failed_rate": 0.5,
            "rate_limit_rate": 0.0,
            "total_requests": 100,
            "total_timeout_count": 0,
        }
        decision = controller.check(bad_stats)
        assert controller.state == CircuitState.OPEN
        assert decision.is_probe_mode is False

        # 等待后进入 HALF_OPEN：probe 模式
        time.sleep(0.15)
        decision = controller.check({})
        assert controller.state == CircuitState.HALF_OPEN
        assert decision.is_probe_mode is True
        assert decision.probe_budget == 2

        # 连续成功恢复：CLOSED，非 probe 模式
        controller.record_result(success=True)
        controller.record_result(success=True)
        decision = controller.check({})
        assert controller.state == CircuitState.CLOSED
        assert decision.is_probe_mode is False

    def test_probe_budget_default_value(self):
        """测试 probe budget 默认值"""
        config = CircuitBreakerConfig()

        assert config.probe_budget_per_interval == 2  # 默认值
        assert config.probe_job_types_allowlist == ["commits"]  # 默认值

    def test_probe_decision_with_degraded_params(self):
        """测试 probe 决策包含降级参数"""
        config = CircuitBreakerConfig(
            open_duration_seconds=0.1,
            degraded_batch_size=15,
            degraded_forward_window_seconds=600,
            probe_budget_per_interval=3,
        )
        controller = CircuitBreakerController(config=config, key="test")

        # 进入 HALF_OPEN
        controller.force_open("test")
        time.sleep(0.15)
        decision = controller.check({})

        # 验证降级参数与 probe 参数同时存在
        assert decision.is_probe_mode is True
        assert decision.suggested_batch_size == 15  # 首次探测使用降级值
        assert decision.suggested_forward_window_seconds == 600
        assert decision.suggested_diff_mode == "none"  # 首次探测使用 none
        assert decision.probe_budget == 3
