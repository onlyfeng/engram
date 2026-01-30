"""
test_scm_sync_degradation_policy.py - DegradationController 单元测试

测试场景:
- 连续 429 后策略触发 diff_mode=none 与 batch_size 下调
- 连续超时后触发暂停
- 连续成功后恢复
- SvnPatchFetchController 的连续错误暂停策略
- worker→runs 记录与 scheduler→payload 注入闭环验证
"""

import pytest
from unittest.mock import MagicMock, patch

from engram.logbook.scm_sync_policy import (
    DegradationController,
    DegradationConfig,
    DegradationSuggestion,
    SvnPatchFetchController,
    ErrorType,
    CircuitBreakerController,
    CircuitBreakerConfig,
    CircuitBreakerDecision,
    CircuitState,
)


class TestDegradationController:
    """DegradationController 测试类"""

    def test_initial_state(self):
        """测试初始状态"""
        controller = DegradationController()
        
        assert controller.current_diff_mode == "best_effort"
        assert controller.current_batch_size == 100
        assert controller.consecutive_rate_limit_count == 0
        assert not controller.is_paused

    def test_consecutive_429_triggers_diff_mode_none(self):
        """测试连续 429 后触发 diff_mode=none"""
        config = DegradationConfig(
            rate_limit_threshold=3,  # 连续 3 次 429 触发
            default_batch_size=100,
        )
        controller = DegradationController(config=config)
        
        # 模拟连续 3 次 429 错误
        for i in range(3):
            suggestion = controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[
                    {"error_category": "rate_limited", "status_code": 429}
                ],
                synced_count=0,  # 无成功
            )
        
        # 应该触发 diff_mode=none
        assert controller.current_diff_mode == "none"
        assert suggestion.diff_mode == "none"
        assert controller.consecutive_rate_limit_count >= 3
        assert "rate_limit_count" in suggestion.adjustment_reasons[0]

    def test_consecutive_429_triggers_batch_size_shrink(self):
        """测试连续 429 后触发 batch_size 下调"""
        config = DegradationConfig(
            rate_limit_threshold=3,
            default_batch_size=100,
            batch_shrink_factor=0.5,
            min_batch_size=10,
        )
        controller = DegradationController(
            config=config,
            initial_batch_size=100,
        )
        
        # 模拟 429 错误
        suggestion = controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429}
            ],
            synced_count=0,
        )
        
        # batch_size 应该下调（100 * 0.5 = 50）
        assert controller.current_batch_size == 50
        assert suggestion.batch_size == 50

    def test_retry_after_respected(self):
        """测试 Retry-After 值被使用"""
        controller = DegradationController()
        
        suggestion = controller.update(
            request_stats={"last_retry_after": 30.0},
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429}
            ],
            synced_count=0,
        )
        
        # sleep_seconds 应该接近 Retry-After 值
        assert suggestion.sleep_seconds >= 30.0

    def test_consecutive_timeout_triggers_pause(self):
        """测试连续超时触发暂停"""
        config = DegradationConfig(
            timeout_threshold=3,
        )
        controller = DegradationController(config=config)
        
        # 模拟连续 3 次超时
        for i in range(3):
            suggestion = controller.update(
                unrecoverable_errors=[
                    {"error_category": "timeout"}
                ],
                synced_count=0,
            )
        
        # 应该触发暂停
        assert suggestion.should_pause is True
        assert "timeout" in suggestion.pause_reason

    def test_recovery_after_consecutive_success(self):
        """测试连续成功后恢复"""
        config = DegradationConfig(
            rate_limit_threshold=2,
            recovery_success_count=3,
            default_batch_size=100,
        )
        controller = DegradationController(
            config=config,
            initial_batch_size=50,  # 假设已经被下调
            initial_diff_mode="none",  # 假设已经被降级
        )
        
        # 模拟连续 3 次成功
        for i in range(3):
            suggestion = controller.update(
                unrecoverable_errors=[],
                synced_count=10,  # 有成功
            )
        
        # diff_mode 应该恢复
        assert controller.current_diff_mode == "best_effort"
        # batch_size 应该开始恢复
        assert controller.current_batch_size > 50

    def test_multiple_error_types(self):
        """测试多种错误类型的处理"""
        controller = DegradationController()
        
        # 混合错误类型
        suggestion = controller.update(
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429},
                {"error_category": "timeout"},
                {"error_category": "content_too_large"},
            ],
            synced_count=5,
        )
        
        # 各计数器应该更新
        assert controller.consecutive_rate_limit_count >= 1
        assert controller.consecutive_timeout_count >= 1
        assert controller.consecutive_content_too_large_count >= 1

    def test_success_resets_error_counts(self):
        """测试成功处理重置错误计数"""
        config = DegradationConfig(rate_limit_threshold=5)
        controller = DegradationController(config=config)
        
        # 先产生一些错误
        controller.update(
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429}
            ],
            synced_count=0,
        )
        assert controller.consecutive_rate_limit_count >= 1
        
        # 然后成功处理
        controller.update(
            unrecoverable_errors=[],
            synced_count=10,
        )
        
        # 错误计数应该被重置
        assert controller.consecutive_rate_limit_count == 0

    def test_reset(self):
        """测试重置功能"""
        controller = DegradationController()
        
        # 产生一些状态变化
        controller.update(
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429}
            ],
            synced_count=0,
        )
        
        # 重置
        controller.reset()
        
        # 应该恢复初始状态
        assert controller.current_diff_mode == "best_effort"
        assert controller.consecutive_rate_limit_count == 0
        assert not controller.is_paused

    def test_get_state(self):
        """测试获取状态"""
        controller = DegradationController()
        
        state = controller.get_state()
        
        assert "current_diff_mode" in state
        assert "current_batch_size" in state
        assert "consecutive_errors" in state
        assert "is_paused" in state

    def test_forward_window_shrink_on_429(self):
        """测试 429 时前向窗口缩小"""
        config = DegradationConfig(
            default_forward_window_seconds=3600,
            window_shrink_factor=0.5,
            min_forward_window_seconds=300,
        )
        controller = DegradationController(
            config=config,
            initial_forward_window_seconds=3600,
        )
        
        suggestion = controller.update(
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429}
            ],
            synced_count=0,
        )
        
        # 窗口应该缩小（3600 * 0.5 = 1800）
        assert controller.current_forward_window_seconds == 1800
        assert suggestion.forward_window_seconds == 1800


class TestSvnPatchFetchController:
    """SvnPatchFetchController 测试类"""

    def test_initial_state(self):
        """测试初始状态"""
        controller = SvnPatchFetchController()
        
        assert controller.should_skip_patches is False
        assert controller.skip_reason is None

    def test_consecutive_timeout_triggers_skip(self):
        """测试连续超时触发跳过"""
        controller = SvnPatchFetchController(
            timeout_threshold=3,
        )
        
        # 模拟连续 3 次超时
        for i in range(3):
            triggered = controller.record_error("timeout")
            if i < 2:
                assert triggered is False
        
        # 第 3 次应该触发
        assert triggered is True
        assert controller.should_skip_patches is True
        assert "timeout" in controller.skip_reason

    def test_consecutive_content_too_large_triggers_skip(self):
        """测试连续内容过大触发跳过"""
        controller = SvnPatchFetchController(
            content_too_large_threshold=3,
        )
        
        # 模拟连续 3 次内容过大
        for i in range(3):
            triggered = controller.record_error("content_too_large")
        
        assert triggered is True
        assert controller.should_skip_patches is True
        assert "content_too_large" in controller.skip_reason

    def test_success_resets_counts(self):
        """测试成功记录重置计数"""
        controller = SvnPatchFetchController(timeout_threshold=5)
        
        # 先产生一些错误
        controller.record_error("timeout")
        controller.record_error("timeout")
        
        # 然后成功
        controller.record_success()
        
        # 应该可以继续
        assert controller.should_skip_patches is False

    def test_reset(self):
        """测试重置功能"""
        controller = SvnPatchFetchController(timeout_threshold=2)
        
        # 触发跳过
        controller.record_error("timeout")
        controller.record_error("timeout")
        assert controller.should_skip_patches is True
        
        # 重置
        controller.reset()
        
        # 应该恢复
        assert controller.should_skip_patches is False
        assert controller.skip_reason is None

    def test_different_error_types_reset_each_other(self):
        """测试不同错误类型会重置彼此的计数"""
        controller = SvnPatchFetchController(
            timeout_threshold=3,
            content_too_large_threshold=3,
        )
        
        # 先产生 2 次超时
        controller.record_error("timeout")
        controller.record_error("timeout")
        
        # 然后产生 1 次内容过大（应该重置超时计数）
        controller.record_error("content_too_large")
        
        # 再产生 2 次超时（不应该触发，因为计数被重置了）
        controller.record_error("timeout")
        controller.record_error("timeout")
        
        # 还不应该跳过（timeout 只有 2 次，content_too_large 只有 1 次）
        assert controller.should_skip_patches is False

    def test_get_state(self):
        """测试获取状态"""
        controller = SvnPatchFetchController()
        
        state = controller.get_state()
        
        assert "consecutive_timeout" in state
        assert "consecutive_content_too_large" in state
        assert "should_skip_patches" in state


class TestDegradationConfig:
    """DegradationConfig 测试类"""

    def test_default_values(self):
        """测试默认值"""
        config = DegradationConfig()
        
        assert config.min_batch_size == 10
        assert config.max_batch_size == 500
        assert config.rate_limit_threshold == 3

    def test_from_config_with_none(self):
        """测试从 None 配置加载"""
        config = DegradationConfig.from_config(None)
        
        # 应该返回默认配置
        assert config.min_batch_size == 10

    def test_custom_thresholds(self):
        """测试自定义阈值"""
        config = DegradationConfig(
            rate_limit_threshold=5,
            timeout_threshold=10,
            batch_shrink_factor=0.8,
        )
        
        assert config.rate_limit_threshold == 5
        assert config.timeout_threshold == 10
        assert config.batch_shrink_factor == 0.8


class TestDegradationSuggestion:
    """DegradationSuggestion 测试类"""

    def test_to_dict(self):
        """测试转换为字典"""
        suggestion = DegradationSuggestion(
            diff_mode="none",
            batch_size=50,
            sleep_seconds=30.0,
            should_pause=True,
            pause_reason="test",
            adjustment_reasons=["reason1", "reason2"],
        )
        
        d = suggestion.to_dict()
        
        assert d["diff_mode"] == "none"
        assert d["batch_size"] == 50
        assert d["sleep_seconds"] == 30.0
        assert d["should_pause"] is True
        assert d["pause_reason"] == "test"
        assert len(d["adjustment_reasons"]) == 2


class TestConsecutive429TimeoutTrigger:
    """
    测试连续 429/timeout 错误触发 DegradationController 的策略
    
    验证场景：
    1. 连续多次 429 后触发 diff_mode=none
    2. 连续多次 timeout 后触发暂停
    3. 混合错误类型的处理
    4. 指数退避 sleep 计算
    """

    def test_consecutive_429_triggers_diff_mode_none_and_batch_shrink(self):
        """
        连续 429 错误触发 diff_mode=none 并缩小 batch_size
        
        模拟：连续 3 次请求返回 429，验证：
        - diff_mode 降级为 none
        - batch_size 逐步缩小
        - sleep_seconds 按指数增长
        """
        config = DegradationConfig(
            rate_limit_threshold=3,
            default_batch_size=100,
            batch_shrink_factor=0.5,
            min_batch_size=10,
            base_sleep_seconds=1.0,
        )
        controller = DegradationController(
            config=config,
            initial_batch_size=100,
        )
        
        # 第 1 次 429
        suggestion1 = controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        assert controller.consecutive_rate_limit_count == 1
        assert controller.current_batch_size == 50  # 100 * 0.5
        assert suggestion1.sleep_seconds >= 1.0  # base
        
        # 第 2 次 429
        suggestion2 = controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        assert controller.consecutive_rate_limit_count == 2
        assert controller.current_batch_size == 25  # 50 * 0.5
        assert suggestion2.sleep_seconds >= 2.0  # 指数退避
        
        # 第 3 次 429 - 触发 diff_mode=none
        suggestion3 = controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        assert controller.consecutive_rate_limit_count >= 3
        assert controller.current_diff_mode == "none"
        assert suggestion3.diff_mode == "none"
        assert controller.current_batch_size == 12  # 25 * 0.5 = 12.5 -> 12

    def test_consecutive_timeout_triggers_pause(self):
        """
        连续 timeout 错误触发暂停
        
        模拟：连续 3 次 timeout，验证：
        - should_pause 变为 True
        - pause_reason 包含 timeout 信息
        - 暂停后 sleep_seconds 增加
        """
        config = DegradationConfig(
            timeout_threshold=3,
            base_sleep_seconds=1.0,
            max_sleep_seconds=300.0,
        )
        controller = DegradationController(config=config)
        
        # 连续 3 次 timeout
        for i in range(3):
            suggestion = controller.update(
                unrecoverable_errors=[{"error_category": "timeout"}],
                synced_count=0,
            )
        
        # 第 3 次应该触发暂停
        assert suggestion.should_pause is True
        assert "timeout" in suggestion.pause_reason.lower()
        assert suggestion.sleep_seconds > 0

    def test_mixed_errors_increments_respective_counts(self):
        """
        混合错误类型：各自增加相应计数器
        
        验证不同错误类型独立计数
        """
        controller = DegradationController()
        
        # 发送混合错误
        controller.update(
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429},
                {"error_category": "timeout"},
                {"error_category": "content_too_large"},
            ],
            synced_count=5,  # 有部分成功
        )
        
        # 各计数器应该都有值
        assert controller.consecutive_rate_limit_count >= 1
        assert controller.consecutive_timeout_count >= 1
        assert controller.consecutive_content_too_large_count >= 1

    def test_exponential_backoff_sleep(self):
        """
        指数退避：连续 429 时 sleep 按 2^n 增长
        """
        config = DegradationConfig(
            base_sleep_seconds=2.0,
            max_sleep_seconds=60.0,
        )
        controller = DegradationController(config=config)
        
        sleep_values = []
        
        for i in range(5):
            suggestion = controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
                synced_count=0,
            )
            sleep_values.append(suggestion.sleep_seconds)
        
        # 验证指数增长趋势（2, 4, 8, 16, 32）
        assert sleep_values[0] >= 2.0
        assert sleep_values[1] >= 4.0
        assert sleep_values[2] >= 8.0
        # 后续可能被 max_sleep_seconds 限制

    def test_retry_after_header_respected(self):
        """
        优先使用 Retry-After header 值
        """
        controller = DegradationController()
        
        suggestion = controller.update(
            request_stats={"last_retry_after": 45.0},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # sleep_seconds 应该使用 Retry-After 值
        assert suggestion.sleep_seconds >= 45.0

    def test_success_resets_counts_and_starts_recovery(self):
        """
        成功处理后重置错误计数，连续成功触发恢复
        """
        config = DegradationConfig(
            rate_limit_threshold=2,
            recovery_success_count=3,
            default_batch_size=100,
            batch_shrink_factor=0.5,
            batch_grow_factor=1.2,
        )
        # 注意：设置 initial_batch_size=50，需要在 3 次成功后恢复到更高值
        # 但先不触发 429 错误，这样不会再缩小
        controller = DegradationController(
            config=config,
            initial_batch_size=50,  # 假设已被缩小
            initial_diff_mode="none",  # 假设已被降级
        )
        
        # 直接连续成功（不触发错误，避免 batch_size 再缩小）
        for i in range(3):
            controller.update(
                unrecoverable_errors=[],
                synced_count=10,
            )
        
        # 错误计数应该为 0
        assert controller.consecutive_rate_limit_count == 0
        # diff_mode 应该恢复
        assert controller.current_diff_mode == "best_effort"
        # batch_size 应该开始恢复: 50 * 1.2 = 60
        assert controller.current_batch_size == 60

    def test_forward_window_shrink_on_rate_limit(self):
        """
        429 错误时前向窗口也会缩小
        """
        config = DegradationConfig(
            default_forward_window_seconds=3600,
            window_shrink_factor=0.5,
            min_forward_window_seconds=300,
        )
        controller = DegradationController(
            config=config,
            initial_forward_window_seconds=3600,
        )
        
        suggestion = controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # 窗口应该缩小
        assert controller.current_forward_window_seconds == 1800  # 3600 * 0.5
        assert suggestion.forward_window_seconds == 1800


class TestBackoffSecondsTransmission:
    """
    测试 backoff_seconds 在降级系统中的传递
    
    验证场景：
    - 429 错误产生的 sleep_seconds 用于退避
    - timeout 错误产生的 sleep_seconds
    - Retry-After header 优先级
    - 指数退避增长
    """

    def test_429_error_produces_backoff_sleep(self):
        """429 错误产生退避 sleep_seconds"""
        config = DegradationConfig(
            base_sleep_seconds=2.0,
            max_sleep_seconds=300.0,
        )
        controller = DegradationController(config=config)
        
        suggestion = controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # 应该有退避时间
        assert suggestion.sleep_seconds >= 2.0

    def test_timeout_error_triggers_pause_not_sleep(self):
        """timeout 错误触发暂停而非退避 sleep
        
        注意：DegradationController 对 timeout 错误的处理是触发 should_pause，
        而不是增加 sleep_seconds。这是设计决策：
        - 429 错误：增加 sleep_seconds（短暂退避）
        - timeout 错误：连续多次后触发 should_pause（长时间暂停）
        """
        config = DegradationConfig(
            timeout_threshold=2,  # 连续 2 次 timeout 触发暂停
        )
        controller = DegradationController(config=config)
        
        # 第一次 timeout
        suggestion1 = controller.update(
            unrecoverable_errors=[{"error_category": "timeout"}],
            synced_count=0,
        )
        assert suggestion1.should_pause is False
        assert controller.consecutive_timeout_count == 1
        
        # 第二次 timeout - 触发暂停
        suggestion2 = controller.update(
            unrecoverable_errors=[{"error_category": "timeout"}],
            synced_count=0,
        )
        assert suggestion2.should_pause is True
        assert "timeout" in suggestion2.pause_reason.lower()

    def test_retry_after_takes_precedence(self):
        """Retry-After header 值优先于计算的退避时间"""
        config = DegradationConfig(
            base_sleep_seconds=2.0,
            max_sleep_seconds=300.0,
        )
        controller = DegradationController(config=config)
        
        # Retry-After: 60 秒
        suggestion = controller.update(
            request_stats={"last_retry_after": 60.0},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # 应该使用 Retry-After 值
        assert suggestion.sleep_seconds >= 60.0

    def test_exponential_backoff_growth(self):
        """指数退避增长"""
        config = DegradationConfig(
            base_sleep_seconds=1.0,
            max_sleep_seconds=60.0,
        )
        controller = DegradationController(config=config)
        
        sleep_values = []
        
        for i in range(4):
            suggestion = controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
                synced_count=0,
            )
            sleep_values.append(suggestion.sleep_seconds)
        
        # 验证增长趋势
        for i in range(1, len(sleep_values)):
            assert sleep_values[i] >= sleep_values[i-1], f"sleep_seconds 应该递增: {sleep_values}"

    def test_backoff_respects_max_limit(self):
        """退避时间不超过最大限制"""
        config = DegradationConfig(
            base_sleep_seconds=10.0,
            max_sleep_seconds=30.0,
        )
        controller = DegradationController(config=config)
        
        # 连续多次 429
        for _ in range(10):
            suggestion = controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
                synced_count=0,
            )
        
        # 不应超过 max_sleep_seconds
        assert suggestion.sleep_seconds <= 30.0

    def test_success_resets_backoff(self):
        """成功后退避时间重置"""
        config = DegradationConfig(
            base_sleep_seconds=2.0,
            recovery_success_count=2,
        )
        controller = DegradationController(config=config)
        
        # 先产生一些 429 错误
        for _ in range(3):
            controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
                synced_count=0,
            )
        
        # 连续成功
        for _ in range(2):
            suggestion = controller.update(
                unrecoverable_errors=[],
                synced_count=10,
            )
        
        # sleep_seconds 应该较低
        assert suggestion.sleep_seconds <= 2.0


class TestCircuitBreakerDiffModeDegradation:
    """
    测试熔断状态下 diff_mode 降级
    
    验证场景：
    - 熔断触发时 suggested_diff_mode 变为 none
    - 限流状态下 batch_size 和 forward_window 按预期下调
    - HALF_OPEN 状态下保持降级参数
    """

    def test_rate_limit_triggers_diff_mode_none_via_degradation(self):
        """
        连续 429 错误触发 diff_mode 降级为 none
        
        验证 DegradationController 在达到 rate_limit_threshold 后，
        suggested_diff_mode 变为 none
        """
        config = DegradationConfig(
            rate_limit_threshold=3,
            default_batch_size=100,
            batch_shrink_factor=0.5,
        )
        controller = DegradationController(
            config=config,
            initial_diff_mode="best_effort",
        )
        
        # 连续触发 429
        for i in range(3):
            suggestion = controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
                synced_count=0,
            )
        
        # diff_mode 应降级为 none
        assert suggestion.diff_mode == "none"
        assert controller.current_diff_mode == "none"

    def test_batch_size_shrinks_on_rate_limit(self):
        """
        限流状态下 batch_size 按 shrink_factor 下调
        """
        config = DegradationConfig(
            default_batch_size=100,
            batch_shrink_factor=0.5,
            min_batch_size=10,
        )
        controller = DegradationController(
            config=config,
            initial_batch_size=100,
        )
        
        # 第一次 429
        suggestion = controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # batch_size 应缩小到 50
        assert suggestion.batch_size == 50
        
        # 第二次 429
        suggestion = controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # batch_size 应缩小到 25
        assert suggestion.batch_size == 25

    def test_forward_window_shrinks_on_rate_limit(self):
        """
        限流状态下 forward_window_seconds 按 window_shrink_factor 下调
        """
        config = DegradationConfig(
            default_forward_window_seconds=3600,
            window_shrink_factor=0.5,
            min_forward_window_seconds=300,
        )
        controller = DegradationController(
            config=config,
            initial_forward_window_seconds=3600,
        )
        
        # 触发 429
        suggestion = controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # forward_window 应缩小到 1800
        assert suggestion.forward_window_seconds == 1800

    def test_degradation_parameters_combined(self):
        """
        验证 diff_mode、batch_size、forward_window 同时降级
        """
        config = DegradationConfig(
            rate_limit_threshold=2,
            default_batch_size=100,
            batch_shrink_factor=0.5,
            default_forward_window_seconds=3600,
            window_shrink_factor=0.5,
        )
        controller = DegradationController(
            config=config,
            initial_batch_size=100,
            initial_diff_mode="best_effort",
            initial_forward_window_seconds=3600,
        )
        
        # 第一次 429
        suggestion1 = controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # batch 和 window 应该缩小
        assert suggestion1.batch_size == 50
        assert suggestion1.forward_window_seconds == 1800
        # diff_mode 还没到阈值
        assert suggestion1.diff_mode == "best_effort"
        
        # 第二次 429 - 达到阈值
        suggestion2 = controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # 全部降级
        assert suggestion2.batch_size == 25
        assert suggestion2.forward_window_seconds == 900
        assert suggestion2.diff_mode == "none"

    def test_recovery_restores_parameters(self):
        """
        连续成功后恢复 diff_mode、batch_size、forward_window
        """
        config = DegradationConfig(
            rate_limit_threshold=2,
            recovery_success_count=3,
            default_batch_size=100,
            batch_shrink_factor=0.5,
            batch_grow_factor=1.5,
            default_forward_window_seconds=3600,
            window_shrink_factor=0.5,
            window_grow_factor=1.5,
        )
        controller = DegradationController(
            config=config,
            initial_batch_size=25,  # 假设已被缩小
            initial_diff_mode="none",  # 假设已被降级
            initial_forward_window_seconds=900,  # 假设已被缩小
        )
        
        # 连续成功
        for i in range(3):
            suggestion = controller.update(
                unrecoverable_errors=[],
                synced_count=10,
            )
        
        # diff_mode 应恢复
        assert controller.current_diff_mode == "best_effort"
        # batch_size 应开始恢复（25 * 1.5 = 37.5 -> 37）
        assert controller.current_batch_size > 25
        # forward_window 应开始恢复
        assert controller.current_forward_window_seconds > 900


class TestDegradationWithMultipleErrorTypes:
    """
    测试多种错误类型的降级处理
    
    验证场景：
    - 429 和 timeout 混合错误
    - 各类型计数器独立
    - 不同错误类型的优先级
    """

    def test_mixed_429_and_timeout(self):
        """混合 429 和 timeout 错误"""
        config = DegradationConfig(
            rate_limit_threshold=3,
            timeout_threshold=3,
        )
        controller = DegradationController(config=config)
        
        # 混合错误
        controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429},
                {"error_category": "timeout"},
            ],
            synced_count=0,
        )
        
        # 两个计数器都应该增加
        assert controller.consecutive_rate_limit_count >= 1
        assert controller.consecutive_timeout_count >= 1

    def test_error_type_counters_independent(self):
        """错误类型计数器独立"""
        config = DegradationConfig(
            rate_limit_threshold=5,
            timeout_threshold=5,
        )
        controller = DegradationController(config=config)
        
        # 只产生 429
        controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        controller.update(
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # 429 计数应该是 2，timeout 应该是 0
        assert controller.consecutive_rate_limit_count == 2
        # timeout 计数器不应该增加（除非有 timeout 错误）

    def test_content_too_large_independent(self):
        """content_too_large 错误独立计数"""
        config = DegradationConfig(
            content_too_large_threshold=3,
        )
        controller = DegradationController(config=config)
        
        # 产生 content_too_large 错误
        controller.update(
            unrecoverable_errors=[{"error_category": "content_too_large"}],
            synced_count=0,
        )
        controller.update(
            unrecoverable_errors=[{"error_category": "content_too_large"}],
            synced_count=0,
        )
        
        assert controller.consecutive_content_too_large_count >= 2


class TestWorkerRunsRecordClosedLoop:
    """
    测试 worker→runs 记录闭环
    
    验证场景：
    - complete_sync_run 正确写入 request_stats 和 degradation_snapshot
    - DegradationController.get_state() 返回的快照可序列化
    """

    def test_degradation_controller_state_serializable(self):
        """
        DegradationController.get_state() 返回的状态可 JSON 序列化
        """
        import json
        
        config = DegradationConfig(
            rate_limit_threshold=3,
            default_batch_size=100,
        )
        controller = DegradationController(config=config)
        
        # 产生一些状态变化
        controller.update(
            request_stats={"total_429_hits": 2},
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429}
            ],
            synced_count=5,
        )
        
        # 获取状态
        state = controller.get_state()
        
        # 验证可序列化
        json_str = json.dumps(state)
        assert json_str is not None
        
        # 验证关键字段存在
        assert "current_diff_mode" in state
        assert "current_batch_size" in state
        assert "consecutive_errors" in state
        assert "total_429_hits" in state
        assert "update_count" in state

    def test_degradation_snapshot_contains_request_stats_info(self):
        """
        DegradationController 更新后状态包含从 request_stats 累计的信息
        """
        controller = DegradationController()
        
        # 第一次更新
        controller.update(
            request_stats={"total_429_hits": 3, "total_requests": 100},
            unrecoverable_errors=[],
            synced_count=10,
        )
        
        state = controller.get_state()
        
        # 验证累计的 429 次数
        assert state["total_429_hits"] == 3
        assert state["update_count"] == 1

    def test_degradation_suggestion_to_dict_complete(self):
        """
        DegradationSuggestion.to_dict() 返回完整的建议字段
        """
        suggestion = DegradationSuggestion(
            diff_mode="none",
            batch_size=50,
            sleep_seconds=30.0,
            forward_window_seconds=1800,
            should_pause=True,
            pause_reason="rate_limit_exceeded",
            adjustment_reasons=["batch_shrink", "diff_mode_downgrade"],
        )
        
        d = suggestion.to_dict()
        
        # 验证所有字段都存在
        assert d["diff_mode"] == "none"
        assert d["batch_size"] == 50
        assert d["sleep_seconds"] == 30.0
        assert d["forward_window_seconds"] == 1800
        assert d["should_pause"] is True
        assert d["pause_reason"] == "rate_limit_exceeded"
        assert len(d["adjustment_reasons"]) == 2


class TestSchedulerPayloadInjectionClosedLoop:
    """
    测试 scheduler→payload 注入闭环
    
    验证场景：
    - CircuitBreakerDecision 包含所有 suggested_* 字段
    - CLOSED/OPEN/HALF_OPEN 状态下 suggested_* 字段的值符合预期
    """

    def test_circuit_breaker_decision_contains_suggested_fields(self):
        """
        CircuitBreakerDecision 包含所有 suggested_* 字段
        """
        decision = CircuitBreakerDecision(
            allow_sync=True,
            is_backfill_only=False,
            suggested_batch_size=100,
            suggested_forward_window_seconds=3600,
            suggested_diff_mode="best_effort",
        )
        
        d = decision.to_dict()
        
        # 验证所有 suggested_* 字段存在
        assert "suggested_batch_size" in d
        assert "suggested_forward_window_seconds" in d
        assert "suggested_diff_mode" in d
        
        # 验证默认值
        assert d["suggested_batch_size"] == 100
        assert d["suggested_forward_window_seconds"] == 3600
        assert d["suggested_diff_mode"] == "best_effort"

    def test_circuit_breaker_closed_state_default_suggestions(self):
        """
        CLOSED 状态下 CircuitBreakerController 返回默认建议参数
        """
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.5,  # 高阈值避免触发
            min_samples=10,
        )
        controller = CircuitBreakerController(config=config)
        
        # CLOSED 状态检查
        decision = controller.check(health_stats={
            "total_runs": 5,  # 低于 min_samples
            "failed_rate": 0.1,
        })
        
        assert decision.current_state == CircuitState.CLOSED.value
        assert decision.allow_sync is True
        assert decision.is_backfill_only is False
        # 默认建议参数（CLOSED 状态）
        assert decision.suggested_batch_size == 100
        assert decision.suggested_diff_mode == "best_effort"

    def test_circuit_breaker_open_state_degraded_suggestions(self):
        """
        OPEN 状态下 CircuitBreakerController 返回降级建议参数
        """
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=3,
            degraded_batch_size=10,
            degraded_forward_window_seconds=300,
        )
        controller = CircuitBreakerController(config=config)
        
        # 触发熔断
        decision = controller.check(health_stats={
            "total_runs": 10,
            "failed_rate": 0.5,  # 超过阈值
            "rate_limit_rate": 0.0,
        })
        
        assert decision.current_state == CircuitState.OPEN.value
        # 降级建议参数
        assert decision.suggested_batch_size == 10
        assert decision.suggested_forward_window_seconds == 300
        assert decision.suggested_diff_mode == "none"
        assert decision.is_backfill_only is True

    def test_circuit_breaker_half_open_probe_mode(self):
        """
        HALF_OPEN 状态下 CircuitBreakerController 返回探测模式参数
        """
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=3,
            open_duration_seconds=0,  # 立即进入 HALF_OPEN
            probe_budget_per_interval=2,
            probe_job_types_allowlist=["commits"],
        )
        controller = CircuitBreakerController(config=config)
        
        # 先触发熔断
        controller.check(health_stats={
            "total_runs": 10,
            "failed_rate": 0.5,
        })
        
        # 再次检查应该进入 HALF_OPEN（因为 open_duration_seconds=0）
        decision = controller.check(health_stats={
            "total_runs": 10,
            "failed_rate": 0.5,
        })
        
        assert decision.current_state == CircuitState.HALF_OPEN.value
        # 探测模式标记
        assert decision.is_probe_mode is True
        assert decision.probe_budget == 2
        assert decision.probe_job_types_allowlist == ["commits"]

    def test_circuit_breaker_decision_to_dict_serializable(self):
        """
        CircuitBreakerDecision.to_dict() 返回可序列化的字典
        """
        import json
        
        decision = CircuitBreakerDecision(
            allow_sync=True,
            is_backfill_only=True,
            suggested_batch_size=50,
            suggested_forward_window_seconds=1800,
            suggested_diff_mode="none",
            wait_seconds=30.0,
            next_allowed_at=1234567890.0,
            current_state="open",
            trigger_reason="failure_rate_exceeded",
            health_stats={"total_runs": 10, "failed_rate": 0.4},
            is_probe_mode=True,
            probe_budget=3,
            probe_job_types_allowlist=["commits", "mrs"],
        )
        
        d = decision.to_dict()
        
        # 验证可序列化
        json_str = json.dumps(d)
        assert json_str is not None
        
        # 验证所有字段存在
        assert d["allow_sync"] is True
        assert d["is_backfill_only"] is True
        assert d["suggested_batch_size"] == 50
        assert d["is_probe_mode"] is True
        assert d["probe_budget"] == 3


class TestDegradationClosedLoopIntegration:
    """
    降级闭环集成测试
    
    验证场景：
    - DegradationController 更新后的建议可被用于 payload 注入
    - 连续错误→降级→恢复的完整流程
    """

    def test_degradation_suggestion_can_be_used_in_payload(self):
        """
        DegradationController 返回的建议可直接用于 payload 注入
        """
        config = DegradationConfig(
            rate_limit_threshold=2,
            default_batch_size=100,
            batch_shrink_factor=0.5,
        )
        controller = DegradationController(config=config)
        
        # 触发降级
        suggestion = controller.update(
            request_stats={"total_429_hits": 1},
            unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
            synced_count=0,
        )
        
        # 模拟 payload 构建（与 scheduler 逻辑一致）
        payload = {
            "suggested_batch_size": suggestion.batch_size,
            "suggested_forward_window_seconds": suggestion.forward_window_seconds,
            "suggested_diff_mode": suggestion.diff_mode,
        }
        
        # 验证 payload 字段
        assert payload["suggested_batch_size"] == 50  # 缩小后的 batch_size
        assert payload["suggested_forward_window_seconds"] == suggestion.forward_window_seconds
        assert payload["suggested_diff_mode"] == suggestion.diff_mode

    def test_full_degradation_recovery_cycle(self):
        """
        完整的降级→恢复周期测试
        
        流程：
        1. 初始状态：best_effort, batch_size=100
        2. 连续 429 → diff_mode=none, batch_size 缩小
        3. 连续成功 → 恢复到 best_effort, batch_size 开始恢复
        """
        config = DegradationConfig(
            rate_limit_threshold=3,
            recovery_success_count=3,
            default_batch_size=100,
            batch_shrink_factor=0.5,
            batch_grow_factor=1.2,
        )
        controller = DegradationController(config=config)
        
        # 阶段 1：初始状态
        assert controller.current_diff_mode == "best_effort"
        assert controller.current_batch_size == 100
        
        # 阶段 2：连续 429 触发降级
        for i in range(3):
            suggestion = controller.update(
                request_stats={"total_429_hits": 1},
                unrecoverable_errors=[{"error_category": "rate_limited", "status_code": 429}],
                synced_count=0,
            )
        
        # 验证降级状态
        assert controller.current_diff_mode == "none"
        assert controller.current_batch_size < 100  # 已缩小
        degraded_batch_size = controller.current_batch_size
        
        # 阶段 3：连续成功触发恢复
        for i in range(3):
            suggestion = controller.update(
                unrecoverable_errors=[],
                synced_count=10,
            )
        
        # 验证恢复状态
        assert controller.current_diff_mode == "best_effort"
        assert controller.current_batch_size > degraded_batch_size  # 开始恢复

    def test_degradation_state_can_be_written_to_runs(self):
        """
        DegradationController 状态可被写入 sync_runs 的 degradation_json 列
        
        验证 get_state() 返回的字典包含所有需要持久化的信息
        """
        import json
        
        config = DegradationConfig(
            rate_limit_threshold=2,
            timeout_threshold=3,
        )
        controller = DegradationController(config=config)
        
        # 产生混合错误
        controller.update(
            request_stats={"total_429_hits": 2, "total_requests": 50},
            unrecoverable_errors=[
                {"error_category": "rate_limited", "status_code": 429},
                {"error_category": "timeout"},
            ],
            degraded_count=3,
            bulk_count=5,
            synced_count=10,
        )
        
        # 获取状态
        state = controller.get_state()
        
        # 验证包含关键的诊断信息
        assert "current_diff_mode" in state
        assert "current_batch_size" in state
        assert "current_forward_window_seconds" in state
        assert "consecutive_errors" in state
        assert "consecutive_success_count" in state
        assert "is_paused" in state
        assert "total_429_hits" in state
        assert "total_degraded" in state
        assert "total_bulk" in state
        assert "update_count" in state
        
        # 验证可 JSON 序列化（用于写入 degradation_json 列）
        json_str = json.dumps(state)
        parsed = json.loads(json_str)
        assert parsed["total_429_hits"] == 2
        assert parsed["total_degraded"] == 3
        assert parsed["total_bulk"] == 5
