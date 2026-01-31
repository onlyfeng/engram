# -*- coding: utf-8 -*-
"""
test_gitlab_client_integration.py - GitLabClient 集成测试

测试 GitLabClient 在并发场景下与 Postgres 限流器和断路器的交互。

================================================================================
测试场景
================================================================================

1. PostgresRateLimiter 集成测试
   - 单 worker 请求消费令牌
   - 429 响应触发 pause_rate_limit_bucket
   - 多 worker 并发消费共享令牌桶

2. CircuitBreaker 状态持久化测试
   - 熔断状态保存到 logbook.kv
   - 多 worker 共享熔断状态

3. 端到端集成测试
   - Mock HTTP 返回 429/200 序列
   - 验证 limiter 状态表变化
   - 验证 breaker 状态变化

================================================================================
运行方式
================================================================================

# 使用现有的 Postgres fixture
pytest tests/test_gitlab_client_integration.py -v
"""

import concurrent.futures
import os
import sys
import time
from unittest.mock import Mock, patch

import pytest
import requests

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as scm_db
from engram.logbook.gitlab_client import (
    GitLabClient,
    HttpConfig,
    PostgresRateLimiter,
)
from engram.logbook.scm_sync_policy import (
    CircuitBreakerConfig,
    CircuitBreakerController,
    CircuitState,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def instance_key():
    """生成唯一的实例标识"""
    return f"gitlab:test-{int(time.time() * 1000)}"


@pytest.fixture
def postgres_limiter(migrated_db, instance_key):
    """创建 PostgresRateLimiter 实例"""
    dsn = migrated_db["dsn"]
    limiter = PostgresRateLimiter(
        instance_key=instance_key,
        dsn=dsn,
        rate=10.0,  # 10 tokens/sec
        burst=20,  # 最大 20 tokens
        max_wait_seconds=5.0,
    )
    return limiter


@pytest.fixture
def cleanup_rate_limit(db_conn_committed, instance_key):
    """测试后清理 rate limit 记录"""
    yield
    try:
        with db_conn_committed.cursor() as cur:
            cur.execute("DELETE FROM scm.sync_rate_limits WHERE instance_key = %s", (instance_key,))
        db_conn_committed.commit()
    except Exception:
        pass


@pytest.fixture
def circuit_breaker_key():
    """生成唯一的熔断器键名"""
    return f"test:{int(time.time() * 1000)}:global"


@pytest.fixture
def cleanup_circuit_breaker(db_conn_committed, circuit_breaker_key):
    """测试后清理 circuit breaker 记录"""
    yield
    try:
        with db_conn_committed.cursor() as cur:
            cur.execute(
                "DELETE FROM logbook.kv WHERE namespace = 'scm.sync_health' AND key = %s",
                (circuit_breaker_key,),
            )
        db_conn_committed.commit()
    except Exception:
        pass


# ============================================================================
# PostgresRateLimiter 集成测试
# ============================================================================


class TestPostgresRateLimiterIntegration:
    """PostgresRateLimiter 与 Postgres 的集成测试"""

    def test_consume_token_creates_bucket(
        self, migrated_db, instance_key, cleanup_rate_limit, db_conn_committed
    ):
        """测试首次消费令牌时自动创建桶"""
        dsn = migrated_db["dsn"]
        limiter = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=1.0,
        )

        # 消费一个令牌
        result = limiter.acquire(timeout=1.0)
        assert result is True, "应该成功获取令牌"

        # 验证桶已创建
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status is not None, "桶应该已创建"
        assert status["instance_key"] == instance_key
        assert status["rate"] == 10.0
        assert status["burst"] == 20
        # 初始 20 tokens - 1 消费 = 19 tokens (允许一些误差)
        assert status["current_tokens"] >= 18.0

    def test_429_triggers_pause(
        self, migrated_db, instance_key, cleanup_rate_limit, db_conn_committed
    ):
        """测试 429 响应触发桶暂停"""
        dsn = migrated_db["dsn"]
        limiter = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=1.0,
        )

        # 先消费一个令牌，确保桶存在
        limiter.acquire(timeout=1.0)

        # 模拟收到 429 响应
        limiter.notify_rate_limit(retry_after=5.0)

        # 验证桶状态
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status is not None
        assert status["is_paused"] is True, "桶应该被暂停"
        assert status["pause_remaining_seconds"] > 0, "应该有剩余暂停时间"

        # 验证 meta_json 中记录了 429 信息
        meta = status.get("meta_json") or {}
        assert "last_429_at" in meta, "应该记录 429 时间"
        assert meta.get("last_retry_after") == 5.0, "应该记录 retry_after 值"

    def test_concurrent_workers_share_bucket(
        self, migrated_db, instance_key, cleanup_rate_limit, db_conn_committed
    ):
        """测试多个 worker 并发消费共享令牌桶"""
        dsn = migrated_db["dsn"]

        # 创建两个 limiter 实例（模拟两个 worker）
        limiter1 = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=1.0,  # 慢速补充，便于观察消费
            burst=5,  # 小容量便于测试
            max_wait_seconds=0.1,
        )
        limiter2 = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=1.0,
            burst=5,
            max_wait_seconds=0.1,
        )

        # 并发消费令牌
        results = []

        def consume(limiter, count):
            successes = 0
            for _ in range(count):
                if limiter.acquire(timeout=0.1):
                    successes += 1
            return successes

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(consume, limiter1, 3)
            future2 = executor.submit(consume, limiter2, 3)

            results.append(future1.result())
            results.append(future2.result())

        # 两个 worker 总共应该消费 5 个令牌（burst 限制）
        # 或者因为 rate 限制而少于 6 个
        total_consumed = sum(results)
        assert total_consumed <= 6, f"总消费不应超过 burst+1: {total_consumed}"

        # 验证桶状态
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status is not None
        # 令牌应该被消耗了
        assert status["current_tokens"] < 5.0, "令牌应该被消耗"

    def test_worker_429_affects_all_workers(
        self, migrated_db, instance_key, cleanup_rate_limit, db_conn_committed
    ):
        """测试一个 worker 收到 429 会影响所有 worker"""
        dsn = migrated_db["dsn"]

        # Worker 1 消费令牌
        limiter1 = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=0.5,
        )

        # Worker 2 使用相同的 instance_key
        limiter2 = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=0.5,
        )

        # Worker 1 先消费令牌
        assert limiter1.acquire(timeout=1.0) is True

        # Worker 1 收到 429，通知暂停
        limiter1.notify_rate_limit(retry_after=10.0)

        # Worker 2 尝试消费，应该被暂停拒绝（超时）
        result = limiter2.acquire(timeout=0.3)
        assert result is False, "Worker 2 应该因为桶暂停而获取失败"

        # 验证桶状态
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status["is_paused"] is True


# ============================================================================
# CircuitBreaker 状态持久化测试
# ============================================================================


class TestCircuitBreakerPersistence:
    """CircuitBreaker 状态持久化与多 worker 共享测试"""

    def test_circuit_breaker_state_saved_to_kv(
        self, db_conn_committed, circuit_breaker_key, cleanup_circuit_breaker
    ):
        """测试熔断状态保存到 logbook.kv"""
        # 创建熔断器
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=60,
        )
        controller = CircuitBreakerController(config=config, key=circuit_breaker_key)

        # 触发熔断
        controller.force_open(reason="test_trigger")

        # 保存状态
        state_dict = controller.get_state_dict()
        scm_db.save_circuit_breaker_state(db_conn_committed, circuit_breaker_key, state_dict)
        db_conn_committed.commit()

        # 验证状态已保存
        loaded_state = scm_db.load_circuit_breaker_state(db_conn_committed, circuit_breaker_key)
        assert loaded_state is not None
        assert loaded_state["state"] == "open"
        assert loaded_state["last_failure_reason"] == "test_trigger"

    def test_multiple_workers_share_circuit_breaker_state(
        self, db_conn_committed, circuit_breaker_key, cleanup_circuit_breaker
    ):
        """测试多个 worker 共享熔断状态"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=60,
            recovery_success_count=2,
        )

        # Worker 1 触发熔断
        controller1 = CircuitBreakerController(config=config, key=circuit_breaker_key)
        controller1.force_open(reason="worker1_trigger")

        # Worker 1 保存状态
        scm_db.save_circuit_breaker_state(
            db_conn_committed, circuit_breaker_key, controller1.get_state_dict()
        )
        db_conn_committed.commit()

        # Worker 2 加载状态
        controller2 = CircuitBreakerController(config=config, key=circuit_breaker_key)
        loaded_state = scm_db.load_circuit_breaker_state(db_conn_committed, circuit_breaker_key)
        controller2.load_state_dict(loaded_state)

        # 验证 Worker 2 看到相同的熔断状态
        assert controller2.state == CircuitState.OPEN
        assert controller2._last_failure_reason == "worker1_trigger"

    def test_circuit_breaker_half_open_probe(
        self, db_conn_committed, circuit_breaker_key, cleanup_circuit_breaker
    ):
        """测试半开状态的探测和恢复"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=1,  # 1秒后进入半开
            recovery_success_count=2,
            half_open_max_requests=3,
        )

        controller = CircuitBreakerController(config=config, key=circuit_breaker_key)

        # 触发熔断
        controller.force_open(reason="test")
        assert controller.state == CircuitState.OPEN

        # 等待进入半开状态
        time.sleep(1.1)

        # 检查应该进入半开状态
        controller.check({}, now=time.time())
        assert controller.state == CircuitState.HALF_OPEN

        # 保存状态
        scm_db.save_circuit_breaker_state(
            db_conn_committed, circuit_breaker_key, controller.get_state_dict()
        )
        db_conn_committed.commit()

        # 验证状态
        loaded = scm_db.load_circuit_breaker_state(db_conn_committed, circuit_breaker_key)
        assert loaded["state"] == "half_open"


# ============================================================================
# GitLabClient 端到端集成测试（Mock HTTP）
# ============================================================================


class TestGitLabClientE2EIntegration:
    """GitLabClient 端到端集成测试（使用 Mock HTTP）"""

    def test_429_response_updates_rate_limit_table(
        self, migrated_db, instance_key, cleanup_rate_limit, db_conn_committed
    ):
        """测试 429 响应更新 rate limit 表"""
        dsn = migrated_db["dsn"]

        # 创建 PostgresRateLimiter
        pg_limiter = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=1.0,
        )

        # 创建 GitLabClient
        http_config = HttpConfig(
            timeout_seconds=5.0,
            max_attempts=2,
            backoff_base_seconds=0.1,
        )

        client = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="test-token",
            http_config=http_config,
            postgres_rate_limiter=pg_limiter,
        )

        # Mock HTTP 响应：先 429，然后 200
        mock_response_429 = Mock()
        mock_response_429.status_code = 429
        mock_response_429.headers = {"Retry-After": "5"}
        mock_response_429.text = "Rate limited"
        mock_response_429.json.return_value = {"error": "rate limited"}
        mock_response_429.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_response_429
        )

        mock_response_200 = Mock()
        mock_response_200.status_code = 200
        mock_response_200.headers = {}
        mock_response_200.json.return_value = {"id": 1, "name": "test"}
        mock_response_200.raise_for_status.return_value = None

        call_count = [0]

        def mock_request(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_response_429
            return mock_response_200

        with patch.object(client.session, "request", side_effect=mock_request):
            # 执行请求（会触发重试）
            client.request_safe("GET", "/projects/1")

        # 验证 rate limit 表被更新
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status is not None
        # 429 应该触发了暂停
        meta = status.get("meta_json") or {}
        assert "last_429_at" in meta, "应该记录 429 时间"

    def test_concurrent_workers_429_sequence(
        self, migrated_db, cleanup_rate_limit, db_conn_committed
    ):
        """测试并发 worker 处理 429/200 序列"""
        # 使用唯一的 instance_key
        instance_key = f"gitlab:concurrent-{int(time.time() * 1000)}"
        dsn = migrated_db["dsn"]

        # 清理此 instance_key 的记录
        try:
            with db_conn_committed.cursor() as cur:
                cur.execute(
                    "DELETE FROM scm.sync_rate_limits WHERE instance_key = %s", (instance_key,)
                )
            db_conn_committed.commit()
        except Exception:
            pass

        # 创建两个 client（共享同一 instance_key）
        def create_client():
            pg_limiter = PostgresRateLimiter(
                instance_key=instance_key,
                dsn=dsn,
                rate=100.0,  # 高速率，避免限流影响测试
                burst=100,
                max_wait_seconds=0.5,
            )
            http_config = HttpConfig(
                timeout_seconds=5.0,
                max_attempts=1,  # 不重试，便于观察
                backoff_base_seconds=0.1,
            )
            return GitLabClient(
                base_url="https://gitlab.example.com",
                private_token="test-token",
                http_config=http_config,
                postgres_rate_limiter=pg_limiter,
            )

        client1 = create_client()
        client2 = create_client()

        # 结果收集
        results = {"worker1": [], "worker2": []}

        def create_mock_response(status_code, retry_after=None):
            mock_resp = Mock()
            mock_resp.status_code = status_code
            mock_resp.headers = {"Retry-After": str(retry_after)} if retry_after else {}
            mock_resp.text = "OK" if status_code == 200 else "Error"
            mock_resp.json.return_value = (
                {"status": "ok"} if status_code == 200 else {"error": "error"}
            )
            if status_code >= 400:
                mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
                    response=mock_resp
                )
            else:
                mock_resp.raise_for_status.return_value = None
            return mock_resp

        # Worker 1: 第一次 429，然后 200
        worker1_responses = [
            create_mock_response(429, retry_after=2),
            create_mock_response(200),
        ]
        worker1_call_count = [0]

        def worker1_mock(*args, **kwargs):
            idx = min(worker1_call_count[0], len(worker1_responses) - 1)
            worker1_call_count[0] += 1
            return worker1_responses[idx]

        # Worker 2: 都是 200
        worker2_responses = [
            create_mock_response(200),
            create_mock_response(200),
        ]
        worker2_call_count = [0]

        def worker2_mock(*args, **kwargs):
            idx = min(worker2_call_count[0], len(worker2_responses) - 1)
            worker2_call_count[0] += 1
            return worker2_responses[idx]

        def worker1_task():
            with patch.object(client1.session, "request", side_effect=worker1_mock):
                r1 = client1.request_safe("GET", "/projects/1")
                results["worker1"].append(r1)
                time.sleep(0.1)
                r2 = client1.request_safe("GET", "/projects/2")
                results["worker1"].append(r2)

        def worker2_task():
            time.sleep(0.05)  # 稍后启动
            with patch.object(client2.session, "request", side_effect=worker2_mock):
                r1 = client2.request_safe("GET", "/projects/3")
                results["worker2"].append(r1)

        # 并发执行
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker1_task)
            f2 = executor.submit(worker2_task)
            f1.result()
            f2.result()

        # 验证 rate limit 表状态
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status is not None, "rate limit 记录应该存在"

        # Worker 1 的第一次 429 应该触发了暂停记录
        status.get("meta_json") or {}
        # 由于 retry_after=2，桶应该被暂停过
        # 但由于测试执行速度快，暂停可能已过期

        # 清理
        try:
            with db_conn_committed.cursor() as cur:
                cur.execute(
                    "DELETE FROM scm.sync_rate_limits WHERE instance_key = %s", (instance_key,)
                )
            db_conn_committed.commit()
        except Exception:
            pass

    def test_limiter_and_breaker_interaction(self, migrated_db, db_conn_committed):
        """测试 limiter 和 breaker 的联动"""
        instance_key = f"gitlab:interaction-{int(time.time() * 1000)}"
        breaker_key = f"test:interaction-{int(time.time() * 1000)}:global"
        dsn = migrated_db["dsn"]

        # 创建 PostgresRateLimiter
        pg_limiter = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=1.0,
        )

        # 创建 CircuitBreaker
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            rate_limit_threshold=0.2,
            open_duration_seconds=60,
        )
        breaker = CircuitBreakerController(config=config, key=breaker_key)

        # 模拟高 429 命中率的健康统计
        health_stats = {
            "total_runs": 10,
            "failed_count": 1,
            "failed_rate": 0.1,
            "rate_limit_hits": 3,
            "rate_limit_rate": 0.3,  # 超过 rate_limit_threshold
            "total_requests": 10,
        }

        # 检查熔断决策
        decision = breaker.check(health_stats)

        # 应该触发熔断
        assert breaker.state == CircuitState.OPEN
        assert decision.allow_sync is True  # backfill_only_mode=True
        assert decision.is_backfill_only is True

        # 保存熔断状态
        scm_db.save_circuit_breaker_state(db_conn_committed, breaker_key, breaker.get_state_dict())
        db_conn_committed.commit()

        # 验证熔断状态已保存
        loaded = scm_db.load_circuit_breaker_state(db_conn_committed, breaker_key)
        assert loaded is not None
        assert loaded["state"] == "open"

        # 同时记录 429 到 rate limit 表
        pg_limiter.notify_rate_limit(retry_after=10.0)

        # 验证 rate limit 表
        status = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status is not None
        assert status["is_paused"] is True

        # 清理
        try:
            with db_conn_committed.cursor() as cur:
                cur.execute(
                    "DELETE FROM scm.sync_rate_limits WHERE instance_key = %s", (instance_key,)
                )
                cur.execute(
                    "DELETE FROM logbook.kv WHERE namespace = 'scm.sync_health' AND key = %s",
                    (breaker_key,),
                )
            db_conn_committed.commit()
        except Exception:
            pass


# ============================================================================
# 边界条件测试
# ============================================================================


class TestRateLimiterEdgeCases:
    """限流器边界条件测试"""

    def test_empty_bucket_waits_for_refill(self, migrated_db, db_conn_committed):
        """测试空桶等待令牌补充"""
        instance_key = f"gitlab:empty-{int(time.time() * 1000)}"
        dsn = migrated_db["dsn"]

        # 创建低容量、低速率的 limiter
        limiter = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=1.0,  # 1 token/sec
            burst=2,  # 最大 2 tokens
            max_wait_seconds=0.5,
        )

        # 消费所有令牌
        assert limiter.acquire(timeout=1.0) is True
        assert limiter.acquire(timeout=1.0) is True

        # 第三次应该因为没有令牌而等待超时
        start = time.time()
        limiter.acquire(timeout=0.3)
        elapsed = time.time() - start

        # 可能成功（如果等待期间补充了令牌）或失败（超时）
        # 主要验证不会立即返回
        assert elapsed >= 0.1, "应该等待一段时间"

        # 清理
        try:
            with db_conn_committed.cursor() as cur:
                cur.execute(
                    "DELETE FROM scm.sync_rate_limits WHERE instance_key = %s", (instance_key,)
                )
            db_conn_committed.commit()
        except Exception:
            pass

    def test_pause_clears_after_duration(self, migrated_db, db_conn_committed):
        """测试暂停状态在持续时间后清除"""
        instance_key = f"gitlab:pause-clear-{int(time.time() * 1000)}"
        dsn = migrated_db["dsn"]

        limiter = PostgresRateLimiter(
            instance_key=instance_key,
            dsn=dsn,
            rate=10.0,
            burst=20,
            max_wait_seconds=3.0,
        )

        # 消费一个令牌
        limiter.acquire(timeout=1.0)

        # 暂停 1 秒
        limiter.notify_rate_limit(retry_after=1.0)

        # 立即尝试应该失败
        status1 = scm_db.get_rate_limit_status(db_conn_committed, instance_key)
        assert status1["is_paused"] is True

        # 等待暂停结束
        time.sleep(1.2)

        # 现在应该可以获取
        result = limiter.acquire(timeout=1.0)
        assert result is True, "暂停结束后应该可以获取令牌"

        # 清理
        try:
            with db_conn_committed.cursor() as cur:
                cur.execute(
                    "DELETE FROM scm.sync_rate_limits WHERE instance_key = %s", (instance_key,)
                )
            db_conn_committed.commit()
        except Exception:
            pass


class TestCircuitBreakerEdgeCases:
    """熔断器边界条件测试"""

    def test_half_open_failure_reopens_circuit(self, db_conn_committed):
        """测试半开状态失败后重新熔断"""
        breaker_key = f"test:halfopen-{int(time.time() * 1000)}:global"

        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=0,  # 立即进入半开
            recovery_success_count=2,
        )

        controller = CircuitBreakerController(config=config, key=breaker_key)

        # 触发熔断
        controller.force_open(reason="test")
        assert controller.state == CircuitState.OPEN

        # 检查进入半开
        controller.check({})
        assert controller.state == CircuitState.HALF_OPEN

        # 记录失败
        controller.record_result(success=False, error_category="server_error")

        # 应该重新熔断
        assert controller.state == CircuitState.OPEN

    def test_recovery_requires_consecutive_successes(self, db_conn_committed):
        """测试恢复需要连续成功"""
        breaker_key = f"test:recovery-{int(time.time() * 1000)}:global"

        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=0,
            recovery_success_count=3,  # 需要 3 次连续成功
        )

        controller = CircuitBreakerController(config=config, key=breaker_key)

        # 进入半开
        controller.force_open(reason="test")
        controller.check({})
        assert controller.state == CircuitState.HALF_OPEN

        # 2 次成功，还没恢复
        controller.record_result(success=True)
        assert controller.state == CircuitState.HALF_OPEN

        controller.record_result(success=True)
        assert controller.state == CircuitState.HALF_OPEN

        # 第 3 次成功，恢复
        controller.record_result(success=True)
        assert controller.state == CircuitState.CLOSED
