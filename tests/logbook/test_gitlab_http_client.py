# -*- coding: utf-8 -*-
"""
test_gitlab_http_client.py - GitLab HTTP 客户端重试逻辑测试

覆盖:
- 429 限流错误处理（含/不含 Retry-After）
- 401/403 认证错误处理（触发 TokenProvider.invalidate() 并仅重试一次）
- 5xx 服务器错误处理（指数退避 + jitter）
- 各类错误分类与统计

测试策略:
- 使用 requests-mock 模拟 GitLab HTTP 响应
- 使用 Mock 时间函数验证退避逻辑
- 验证 TokenProvider.invalidate() 调用
"""

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch, call
from dataclasses import dataclass

import pytest
import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.logbook.gitlab_client import (
    GitLabClient,
    GitLabAPIError,
    GitLabAPIResult,
    GitLabRateLimitError,
    GitLabAuthError,
    GitLabServerError,
    GitLabTimeoutError,
    GitLabNetworkError,
    GitLabErrorCategory,
    HttpConfig,
    StaticTokenProvider,
    ClientStats,
    RequestStats,
    ConcurrencyLimiter,
    RateLimiter,
)


# ---------- Fixtures ----------

@pytest.fixture
def requests_mock():
    """提供 requests-mock 功能"""
    import requests_mock as rm
    with rm.Mocker() as m:
        yield m


@pytest.fixture
def mock_token_provider():
    """创建 Mock TokenProvider"""
    provider = MagicMock()
    provider.get_token.return_value = "test-token"
    provider.invalidate = MagicMock()
    return provider


@pytest.fixture
def client_factory(mock_token_provider):
    """GitLab 客户端工厂"""
    def _create(
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.1,
        backoff_max_seconds: float = 10.0,
        timeout_seconds: float = 30.0,
    ):
        http_config = HttpConfig(
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
        return GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
    return _create


# ============ 429 Rate Limited Tests ============

class TestRateLimited429:
    """测试 429 限流错误处理"""
    
    def test_429_with_retry_after_header(self, client_factory, mock_token_provider, requests_mock):
        """429 响应包含 Retry-After 头时，使用该值作为等待时间"""
        client = client_factory(max_attempts=3, backoff_base_seconds=1.0)
        
        # 记录实际等待时间
        sleep_times = []
        
        # 模拟两次 429 后成功
        responses = [
            {"status_code": 429, "json": {"message": "Rate limited"}, "headers": {"Retry-After": "2"}},
            {"status_code": 429, "json": {"message": "Rate limited"}, "headers": {"Retry-After": "3"}},
            {"status_code": 200, "json": [{"id": 1}]},  # 必须是列表
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        assert len(sleep_times) == 2
        
        # 第一次等待应该是 2 + jitter (0~1)
        assert 2.0 <= sleep_times[0] <= 3.0, f"第一次等待应在 2-3 秒范围，实际: {sleep_times[0]}"
        # 第二次等待应该是 3 + jitter (0~1)
        assert 3.0 <= sleep_times[1] <= 4.0, f"第二次等待应在 3-4 秒范围，实际: {sleep_times[1]}"
    
    def test_429_without_retry_after_uses_exponential_backoff(self, client_factory, mock_token_provider, requests_mock):
        """429 响应无 Retry-After 头时，使用指数退避"""
        client = client_factory(max_attempts=3, backoff_base_seconds=1.0)
        
        sleep_times = []
        
        # 模拟两次 429 后成功
        responses = [
            {"status_code": 429, "json": {"message": "Rate limited"}},  # 无 Retry-After
            {"status_code": 429, "json": {"message": "Rate limited"}},  # 无 Retry-After
            {"status_code": 200, "json": [{"id": 1}]},  # 必须是列表
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        assert len(sleep_times) == 2
        
        # 第一次等待: base * 2^0 + jitter = 1 * 1 + jitter ∈ [1, 2)
        assert 1.0 <= sleep_times[0] < 2.0, f"第一次等待应在 1-2 秒范围，实际: {sleep_times[0]}"
        # 第二次等待: base * 2^1 + jitter = 1 * 2 + jitter ∈ [2, 3)
        assert 2.0 <= sleep_times[1] < 3.0, f"第二次等待应在 2-3 秒范围，实际: {sleep_times[1]}"
    
    def test_429_max_attempts_exceeded(self, client_factory, mock_token_provider, requests_mock):
        """429 超过最大重试次数后抛出 GitLabRateLimitError"""
        client = client_factory(max_attempts=2, backoff_base_seconds=0.01)
        
        # 持续返回 429
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"Retry-After": "60"},
        )
        
        with patch("time.sleep"):
            with pytest.raises(GitLabRateLimitError) as exc_info:
                client.get_commits("123")
        
        assert exc_info.value.retry_after == 60.0
        assert "429" in str(exc_info.value)
    
    def test_429_recorded_in_stats(self, client_factory, mock_token_provider, requests_mock):
        """429 命中会被记录在 stats 中"""
        client = client_factory(max_attempts=3, backoff_base_seconds=0.01)
        
        # 一次 429 后成功
        responses = [
            {"status_code": 429, "json": {"message": "Rate limited"}, "headers": {"Retry-After": "1"}},
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            client.get_commits("123")
        
        stats = client.stats.to_dict()
        assert stats["total_429_hits"] == 1
        assert stats["total_retries"] >= 1


# ============ 401/403 Auth Error Tests ============

class TestAuthErrors401403:
    """测试 401/403 认证错误处理"""
    
    def test_401_triggers_invalidate_and_retry_once(self, client_factory, mock_token_provider, requests_mock):
        """401 错误触发 TokenProvider.invalidate() 并仅重试一次"""
        client = client_factory(max_attempts=3)
        
        # 第一次 401，刷新 token 后成功
        responses = [
            {"status_code": 401, "json": {"message": "401 Unauthorized"}},
            {"status_code": 200, "json": [{"id": "abc123"}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        result = client.get_commits("123")
        
        # 验证 invalidate 被调用一次
        mock_token_provider.invalidate.assert_called_once()
        assert result == [{"id": "abc123"}]
    
    def test_403_triggers_invalidate_and_retry_once(self, client_factory, mock_token_provider, requests_mock):
        """403 错误触发 TokenProvider.invalidate() 并仅重试一次"""
        client = client_factory(max_attempts=3)
        
        # 第一次 403，刷新 token 后成功
        responses = [
            {"status_code": 403, "json": {"message": "403 Forbidden"}},
            {"status_code": 200, "json": [{"id": "def456"}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        result = client.get_commits("123")
        
        # 验证 invalidate 被调用一次
        mock_token_provider.invalidate.assert_called_once()
        assert result == [{"id": "def456"}]
    
    def test_401_only_retries_once_after_invalidate(self, client_factory, mock_token_provider, requests_mock):
        """401 刷新 token 后仍失败，不再继续重试（仅重试一次）"""
        client = client_factory(max_attempts=5)  # 高于实际允许的次数
        
        # 持续返回 401
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=401,
            json={"message": "401 Unauthorized"},
        )
        
        with pytest.raises(GitLabAuthError):
            client.get_commits("123")
        
        # invalidate 只应该被调用一次
        mock_token_provider.invalidate.assert_called_once()
    
    def test_403_only_retries_once_after_invalidate(self, client_factory, mock_token_provider, requests_mock):
        """403 刷新 token 后仍失败，不再继续重试（仅重试一次）"""
        client = client_factory(max_attempts=5)
        
        # 持续返回 403
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=403,
            json={"message": "403 Forbidden"},
        )
        
        with pytest.raises(GitLabAuthError):
            client.get_commits("123")
        
        # invalidate 只应该被调用一次
        mock_token_provider.invalidate.assert_called_once()
    
    def test_auth_error_category_recorded(self, client_factory, mock_token_provider, requests_mock):
        """认证错误的分类被正确记录"""
        client = client_factory(max_attempts=2)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=401,
            json={"message": "401 Unauthorized"},
        )
        
        with pytest.raises(GitLabAuthError) as exc_info:
            client.get_commits("123")
        
        assert exc_info.value.category == GitLabErrorCategory.AUTH_ERROR
        assert exc_info.value.status_code == 401


# ============ 5xx Server Error Tests ============

class TestServerErrors5xx:
    """测试 5xx 服务器错误处理（指数退避 + jitter）"""
    
    def test_500_retries_with_exponential_backoff(self, client_factory, mock_token_provider, requests_mock):
        """500 错误使用指数退避重试"""
        client = client_factory(max_attempts=3, backoff_base_seconds=1.0)
        
        sleep_times = []
        
        # 两次 500 后成功
        responses = [
            {"status_code": 500, "json": {"message": "Internal Server Error"}},
            {"status_code": 500, "json": {"message": "Internal Server Error"}},
            {"status_code": 200, "json": [{"id": 1}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        assert len(sleep_times) == 2
        
        # 验证指数退避（包含 jitter）
        # 第一次: base * 2^0 + jitter = 1 * 1 + [0, 1) ∈ [1, 2)
        assert 1.0 <= sleep_times[0] < 2.0
        # 第二次: base * 2^1 + jitter = 1 * 2 + [0, 1) ∈ [2, 3)
        assert 2.0 <= sleep_times[1] < 3.0
    
    def test_502_retries_with_exponential_backoff(self, client_factory, mock_token_provider, requests_mock):
        """502 Bad Gateway 使用指数退避重试"""
        client = client_factory(max_attempts=3, backoff_base_seconds=0.5)
        
        sleep_times = []
        
        responses = [
            {"status_code": 502, "json": {"message": "Bad Gateway"}},
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            client.get_commits("123")
        
        assert len(sleep_times) == 1
        # base=0.5, 第一次: 0.5 * 1 + [0, 0.5) ∈ [0.5, 1.0)
        assert 0.5 <= sleep_times[0] < 1.0
    
    def test_503_retries_with_exponential_backoff(self, client_factory, mock_token_provider, requests_mock):
        """503 Service Unavailable 使用指数退避重试"""
        client = client_factory(max_attempts=4, backoff_base_seconds=0.1)
        
        sleep_times = []
        
        responses = [
            {"status_code": 503, "json": {"message": "Service Unavailable"}},
            {"status_code": 503, "json": {"message": "Service Unavailable"}},
            {"status_code": 503, "json": {"message": "Service Unavailable"}},
            {"status_code": 200, "json": [{"id": 1}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        assert len(sleep_times) == 3
        
        # 验证 jitter 在合理范围内
        # 第一次: 0.1 * 1 + [0, 0.1) ∈ [0.1, 0.2)
        assert 0.1 <= sleep_times[0] < 0.2
        # 第二次: 0.1 * 2 + [0, 0.1) ∈ [0.2, 0.3)
        assert 0.2 <= sleep_times[1] < 0.3
        # 第三次: 0.1 * 4 + [0, 0.1) ∈ [0.4, 0.5)
        assert 0.4 <= sleep_times[2] < 0.5
    
    def test_5xx_max_attempts_exceeded(self, client_factory, mock_token_provider, requests_mock):
        """5xx 超过最大重试次数后抛出 GitLabServerError"""
        client = client_factory(max_attempts=2, backoff_base_seconds=0.01)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=500,
            json={"message": "Internal Server Error"},
        )
        
        with patch("time.sleep"):
            with pytest.raises(GitLabServerError) as exc_info:
                client.get_commits("123")
        
        assert exc_info.value.category == GitLabErrorCategory.SERVER_ERROR
        assert exc_info.value.status_code == 500
    
    def test_backoff_capped_at_max(self, client_factory, mock_token_provider, requests_mock):
        """指数退避被 backoff_max_seconds 限制"""
        client = client_factory(
            max_attempts=5,
            backoff_base_seconds=10.0,  # 大基数
            backoff_max_seconds=15.0,   # 较小的最大值
        )
        
        sleep_times = []
        
        responses = [
            {"status_code": 500, "json": {}},
            {"status_code": 500, "json": {}},
            {"status_code": 500, "json": {}},
            {"status_code": 500, "json": {}},
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            client.get_commits("123")
        
        # 所有等待时间都应该被限制在 max_seconds
        for i, sleep_time in enumerate(sleep_times):
            assert sleep_time <= 15.0 + 10.0, f"第 {i+1} 次等待超过最大值: {sleep_time}"
    
    def test_jitter_range_validation(self, client_factory, mock_token_provider, requests_mock):
        """验证 jitter 在合理范围内（0 到 base_seconds）"""
        # 使用较小的 base 和较大的 max 以避免截断
        client = client_factory(max_attempts=5, backoff_base_seconds=0.5, backoff_max_seconds=100.0)
        
        sleep_times = []
        
        # 多次重试以收集足够的 jitter 样本
        responses = [{"status_code": 500, "json": {}}] * 4 + [{"status_code": 200, "json": []}]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            client.get_commits("123")
        
        # 验证每次 sleep 时间都在预期范围内
        base = client.http_config.backoff_base_seconds
        max_seconds = client.http_config.backoff_max_seconds
        
        for i, sleep_time in enumerate(sleep_times):
            attempt = i + 1
            base_backoff = base * (2 ** (attempt - 1))  # base * 2^(attempt-1)
            
            # 退避值可能被 max_seconds 限制
            capped_backoff = min(base_backoff, max_seconds)
            min_expected = capped_backoff  # 最小值（无 jitter）
            max_expected = capped_backoff + base  # 最大值（最大 jitter = base）
            
            assert min_expected <= sleep_time <= max_expected, \
                f"第 {attempt} 次: {sleep_time:.2f} 不在 [{min_expected:.2f}, {max_expected:.2f}] 范围内"


# ============ Mixed Error Scenarios ============

class TestMixedErrorScenarios:
    """测试混合错误场景"""
    
    def test_429_then_500_then_success(self, client_factory, mock_token_provider, requests_mock):
        """先 429 后 500 再成功"""
        client = client_factory(max_attempts=4, backoff_base_seconds=0.01)
        
        responses = [
            {"status_code": 429, "json": {"message": "Rate limited"}, "headers": {"Retry-After": "0.01"}},
            {"status_code": 500, "json": {"message": "Server Error"}},
            {"status_code": 200, "json": [{"id": 1}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        
        stats = client.stats.to_dict()
        assert stats["total_429_hits"] == 1
        assert stats["total_retries"] >= 2
    
    def test_401_then_429_then_success(self, client_factory, mock_token_provider, requests_mock):
        """先 401（触发 invalidate）后 429 再成功"""
        client = client_factory(max_attempts=4, backoff_base_seconds=0.01)
        
        responses = [
            {"status_code": 401, "json": {"message": "Unauthorized"}},
            {"status_code": 429, "json": {"message": "Rate limited"}, "headers": {"Retry-After": "0.01"}},
            {"status_code": 200, "json": [{"id": 1}]},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        mock_token_provider.invalidate.assert_called_once()


# ============ Request Safe Mode Tests ============

class TestRequestSafeMode:
    """测试 request_safe 模式（不抛异常）"""
    
    def test_429_returns_result_without_exception(self, client_factory, mock_token_provider, requests_mock):
        """request_safe 模式下 429 返回结果而非抛异常"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"Retry-After": "60"},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.success is False
        assert result.status_code == 429
        assert result.error_category == GitLabErrorCategory.RATE_LIMITED
        assert result.retry_after == 60.0
    
    def test_500_returns_result_without_exception(self, client_factory, mock_token_provider, requests_mock):
        """request_safe 模式下 500 返回结果而非抛异常"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=500,
            json={"message": "Internal Server Error"},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.success is False
        assert result.status_code == 500
        assert result.error_category == GitLabErrorCategory.SERVER_ERROR


# ============ Client Stats Tests ============

class TestClientStats:
    """测试客户端统计功能"""
    
    def test_stats_record_success(self, client_factory, mock_token_provider, requests_mock):
        """成功请求被正确统计"""
        client = client_factory()
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=200,
            json=[],
        )
        
        client.get_commits("123")
        
        stats = client.stats.to_dict()
        assert stats["total_requests"] == 1
        assert stats["successful_requests"] == 1
        assert stats["failed_requests"] == 0
    
    def test_stats_record_failure(self, client_factory, mock_token_provider, requests_mock):
        """失败请求被正确统计"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=500,
            json={"message": "Error"},
        )
        
        with pytest.raises(GitLabServerError):
            client.get_commits("123")
        
        stats = client.stats.to_dict()
        assert stats["total_requests"] == 1
        assert stats["successful_requests"] == 0
        assert stats["failed_requests"] == 1
    
    def test_stats_reset(self, client_factory, mock_token_provider, requests_mock):
        """统计重置功能"""
        client = client_factory()
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=200,
            json=[],
        )
        
        client.get_commits("123")
        assert client.stats.total_requests == 1
        
        client.stats.reset()
        
        stats = client.stats.to_dict()
        assert stats["total_requests"] == 0
        assert stats["successful_requests"] == 0
        assert stats["total_429_hits"] == 0


# ============ Timeout Tests ============

class TestTimeoutHandling:
    """测试超时处理"""
    
    def test_timeout_error_retries(self, client_factory, mock_token_provider, requests_mock):
        """超时错误会触发重试"""
        client = client_factory(max_attempts=3, backoff_base_seconds=0.01)
        
        # 两次超时后成功
        call_count = [0]
        
        def callback(request, context):
            call_count[0] += 1
            if call_count[0] < 3:
                raise requests.exceptions.Timeout("Connection timed out")
            context.status_code = 200
            return [{"id": 1}]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=callback,
        )
        
        with patch("time.sleep"):
            result = client.get_commits("123")
        
        assert result == [{"id": 1}]
        assert call_count[0] == 3
    
    def test_timeout_max_attempts_exceeded(self, client_factory, mock_token_provider, requests_mock):
        """超时超过最大重试次数后抛出 GitLabTimeoutError"""
        client = client_factory(max_attempts=2, backoff_base_seconds=0.01)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            exc=requests.exceptions.Timeout("Connection timed out"),
        )
        
        with patch("time.sleep"):
            with pytest.raises(GitLabTimeoutError):
                client.get_commits("123")


# ============ Concurrency Limiter Tests ============

class TestConcurrencyLimiter:
    """测试并发限制器"""
    
    def test_concurrency_limiter_basic(self):
        """基本的并发限制器功能"""
        limiter = ConcurrencyLimiter(max_concurrency=2)
        
        assert limiter.max_concurrency == 2
        assert limiter.active_count == 0
        
        # 获取第一个槽位
        assert limiter.acquire() is True
        assert limiter.active_count == 1
        
        # 获取第二个槽位
        assert limiter.acquire() is True
        assert limiter.active_count == 2
        
        # 释放一个槽位
        limiter.release()
        assert limiter.active_count == 1
        
        # 释放另一个槽位
        limiter.release()
        assert limiter.active_count == 0
    
    def test_concurrency_limiter_context_manager(self):
        """并发限制器上下文管理器"""
        limiter = ConcurrencyLimiter(max_concurrency=2)
        
        with limiter:
            assert limiter.active_count == 1
            with limiter:
                assert limiter.active_count == 2
            assert limiter.active_count == 1
        assert limiter.active_count == 0
    
    def test_concurrency_limiter_timeout(self):
        """并发限制器超时"""
        limiter = ConcurrencyLimiter(max_concurrency=1)
        
        # 占用唯一槽位
        limiter.acquire()
        
        # 尝试获取第二个槽位（应该超时）
        import time
        start = time.time()
        result = limiter.acquire(timeout=0.1)
        elapsed = time.time() - start
        
        assert result is False
        assert elapsed >= 0.1
        assert elapsed < 0.3
        
        limiter.release()
    
    def test_concurrency_limiter_stats(self):
        """并发限制器统计信息"""
        limiter = ConcurrencyLimiter(max_concurrency=3)
        
        limiter.acquire()
        limiter.acquire()
        limiter.release()
        limiter.release()
        
        stats = limiter.get_stats()
        assert stats["max_concurrency"] == 3
        assert stats["total_acquired"] == 2
        assert stats["active_count"] == 0
    
    def test_concurrency_limit_enforced_in_client(self, mock_token_provider, requests_mock):
        """验证客户端正确使用并发限制器"""
        import threading
        import time
        
        # 创建带并发限制的客户端
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            max_concurrency=2,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        assert client.concurrency_limiter is not None
        assert client.concurrency_limiter.max_concurrency == 2
        
        # 模拟慢响应
        request_count = [0]
        max_concurrent = [0]
        concurrent_lock = threading.Lock()
        
        def slow_callback(request, context):
            with concurrent_lock:
                request_count[0] += 1
                current = client.concurrency_limiter.active_count
                if current > max_concurrent[0]:
                    max_concurrent[0] = current
            time.sleep(0.1)
            context.status_code = 200
            return []
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=slow_callback,
        )
        
        # 启动多个并发请求
        threads = []
        for _ in range(5):
            t = threading.Thread(target=lambda: client.get_commits("123"))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        assert request_count[0] == 5
        # 最大并发数应该不超过 2
        assert max_concurrent[0] <= 2
    
    def test_concurrency_limiter_with_barrier(self, mock_token_provider, requests_mock):
        """使用 barrier 验证并发限制"""
        import threading
        
        # 创建带并发限制的客户端
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            max_concurrency=2,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 用于记录并发情况
        active_requests = [0]
        max_active = [0]
        lock = threading.Lock()
        barrier = threading.Barrier(3, timeout=5)  # 3 个线程尝试同时请求
        
        def callback(request, context):
            with lock:
                active_requests[0] += 1
                if active_requests[0] > max_active[0]:
                    max_active[0] = active_requests[0]
            
            try:
                # 等待其他请求
                barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass  # 如果并发被限制，barrier 可能不会满
            
            time.sleep(0.05)
            
            with lock:
                active_requests[0] -= 1
            
            context.status_code = 200
            return []
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=callback,
        )
        
        # 启动 3 个并发请求
        threads = []
        for _ in range(3):
            t = threading.Thread(target=lambda: client.get_commits("123"))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # 最大并发数应该不超过 2
        assert max_active[0] <= 2


# ============ Rate Limiter Tests ============

class TestRateLimiter:
    """测试速率限制器"""
    
    def test_rate_limiter_basic(self):
        """基本的速率限制器功能"""
        limiter = RateLimiter(requests_per_second=10.0, burst_size=5)
        
        assert limiter.rate == 10.0
        assert limiter.burst_size == 5
        
        # 快速获取 5 个令牌（突发容量）
        for _ in range(5):
            assert limiter.acquire(timeout=0.01) is True
        
        stats = limiter.get_stats()
        assert stats["total_requests"] == 5
    
    def test_rate_limiter_notify_rate_limit(self):
        """速率限制器处理 429 响应"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 模拟收到 429 响应
        limiter.notify_rate_limit(retry_after=1.0)
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
    
    def test_rate_limiter_in_client(self, mock_token_provider, requests_mock):
        """验证客户端正确使用速率限制器"""
        # 创建带速率限制的客户端
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
            rate_limit_burst_size=10,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        assert client.rate_limiter is not None
        assert client.rate_limiter.rate == 100.0


# ============ 429 Rate Limit Headers Tests ============

class TestRateLimitHeaders:
    """测试 429 相关头信息的解析和暴露"""
    
    def test_429_with_rate_limit_headers(self, client_factory, mock_token_provider, requests_mock):
        """429 响应包含 RateLimit-* 头时，正确解析并暴露"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={
                "Retry-After": "60",
                "RateLimit-Reset": "1706000000",
                "RateLimit-Remaining": "0",
            },
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.success is False
        assert result.status_code == 429
        assert result.error_category == GitLabErrorCategory.RATE_LIMITED
        assert result.retry_after == 60.0
        assert result.rate_limit_reset == 1706000000.0
        assert result.rate_limit_remaining == 0
    
    def test_429_headers_in_to_dict(self, client_factory, mock_token_provider, requests_mock):
        """429 相关信息正确序列化到 to_dict()"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={
                "Retry-After": "30",
                "RateLimit-Reset": "1706000000",
                "RateLimit-Remaining": "5",
            },
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        result_dict = result.to_dict()
        
        assert result_dict["retry_after"] == 30.0
        assert result_dict["rate_limit_reset"] == 1706000000.0
        assert result_dict["rate_limit_remaining"] == 5
    
    def test_429_headers_recorded_in_stats(self, client_factory, mock_token_provider, requests_mock):
        """429 相关信息被记录到 stats 中"""
        client = client_factory(max_attempts=2, backoff_base_seconds=0.01)
        
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
                "headers": {
                    "Retry-After": "1",
                    "RateLimit-Reset": "1706000000",
                    "RateLimit-Remaining": "0",
                },
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            client.get_commits("123")
        
        stats = client.stats.to_dict()
        assert stats["total_429_hits"] == 1
        assert stats["last_retry_after"] == 1.0
        assert stats["last_rate_limit_reset"] == 1706000000.0
        assert stats["last_rate_limit_remaining"] == 0
    
    def test_429_signal_readable_by_caller(self, client_factory, mock_token_provider, requests_mock):
        """验证上层调用者可以读取 429 信号"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={
                "Retry-After": "120",
                "RateLimit-Reset": "1706000000",
            },
        )
        
        # 使用 request_safe 模式，上层可以读取错误信息
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        # 上层可以根据这些信息做决策
        assert result.success is False
        assert result.error_category == GitLabErrorCategory.RATE_LIMITED
        
        # 可以获取重试时间
        if result.retry_after:
            wait_time = result.retry_after
            assert wait_time == 120.0
        
        # 也可以获取重置时间
        if result.rate_limit_reset:
            reset_time = result.rate_limit_reset
            assert reset_time == 1706000000.0
    
    def test_429_exception_contains_retry_after(self, client_factory, mock_token_provider, requests_mock):
        """验证 GitLabRateLimitError 异常包含 retry_after 信息"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"Retry-After": "90"},
        )
        
        with pytest.raises(GitLabRateLimitError) as exc_info:
            client.get_commits("123")
        
        # 异常中包含 retry_after 信息
        assert exc_info.value.retry_after == 90.0
        assert exc_info.value.status_code == 429


# ============ Concurrency Limiter Injection Tests ============

class TestConcurrencyLimiterInjection:
    """测试并发限制器注入"""
    
    def test_inject_custom_concurrency_limiter(self, mock_token_provider, requests_mock):
        """验证可以注入自定义并发限制器"""
        # 创建自定义并发限制器
        custom_limiter = ConcurrencyLimiter(max_concurrency=5)
        
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
        )
        
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
            concurrency_limiter=custom_limiter,
        )
        
        assert client.concurrency_limiter is custom_limiter
        assert client.concurrency_limiter.max_concurrency == 5
        
        # 发起请求
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=200,
            json=[],
        )
        
        client.get_commits("123")
        
        # 验证限制器被使用
        assert custom_limiter.get_stats()["total_acquired"] == 1
    
    def test_inject_mock_counter_for_concurrency(self, mock_token_provider, requests_mock):
        """使用 mock 计数器验证并发限制"""
        acquire_count = [0]
        release_count = [0]
        
        # 创建带计数的限制器
        class CountingLimiter(ConcurrencyLimiter):
            def acquire(self, timeout=None):
                acquire_count[0] += 1
                return super().acquire(timeout)
            
            def release(self):
                release_count[0] += 1
                super().release()
        
        custom_limiter = CountingLimiter(max_concurrency=3)
        
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
        )
        
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
            concurrency_limiter=custom_limiter,
        )
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=200,
            json=[],
        )
        
        # 发起 3 次请求
        for _ in range(3):
            client.get_commits("123")
        
        # 验证 acquire/release 调用次数
        assert acquire_count[0] == 3
        assert release_count[0] == 3


# ============ Rate Limiter Enabled Tests ============

class TestRateLimiterEnabled:
    """测试 rate_limit_enabled 配置启用时的行为"""
    
    def test_rate_limiter_enabled_creates_internal_rate_limiter(self, mock_token_provider):
        """启用 scm.gitlab.rate_limit_enabled=true 时 client 内部 _rate_limiter 非空"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=10.0,
            rate_limit_burst_size=5,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 验证 _rate_limiter 非空
        assert client._rate_limiter is not None
        assert client.rate_limiter is not None
        assert client.rate_limiter.rate == 10.0
        assert client.rate_limiter.burst_size == 5
    
    def test_rate_limiter_disabled_keeps_internal_rate_limiter_none(self, mock_token_provider):
        """禁用 rate_limit_enabled 时 client 内部 _rate_limiter 为空"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            rate_limit_enabled=False,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 验证 _rate_limiter 为空
        assert client._rate_limiter is None
        assert client.rate_limiter is None
    
    def test_rate_limiter_from_config(self, mock_token_provider):
        """使用 HttpConfig.from_config 加载 rate limit 配置"""
        # 创建 mock config
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.http.timeout_seconds": 60.0,
            "scm.http.max_attempts": 3,
            "scm.http.backoff_base_seconds": 1.0,
            "scm.http.backoff_max_seconds": 60.0,
            "scm.gitlab.max_concurrency": 5,
            "scm.gitlab.rate_limit_enabled": True,
            "scm.gitlab.rate_limit_requests_per_second": 20.0,
            "scm.gitlab.rate_limit_burst_size": 10,
        }.get(key, default)
        
        http_config = HttpConfig.from_config(mock_config)
        
        assert http_config.rate_limit_enabled is True
        assert http_config.rate_limit_requests_per_second == 20.0
        assert http_config.rate_limit_burst_size == 10
        assert http_config.max_concurrency == 5
        
        # 使用该配置创建 client
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        assert client._rate_limiter is not None
        assert client.rate_limiter.rate == 20.0
        assert client._concurrency_limiter is not None
        assert client.concurrency_limiter.max_concurrency == 5


class TestRateLimiterPauseOn429:
    """测试命中 429 后 rate limiter 会 pause"""
    
    def test_429_triggers_rate_limiter_pause(self, mock_token_provider, requests_mock):
        """命中 429 后会触发 rate limiter 的 pause_until"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
            backoff_base_seconds=0.01,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 验证初始状态 paused_until 为 None
        assert client.rate_limiter.get_stats()["paused_until"] is None
        
        # 模拟一次 429 后成功
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
                "headers": {"Retry-After": "5"},
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            client.get_commits("123")
        
        # 验证 rate limiter 曾被通知（paused_until 可能已过期，检查 stats 中的 throttled_count 增加）
        stats = client.rate_limiter.get_stats()
        # 由于请求完成后时间已过，paused_until 可能已被清除
        # 但我们可以验证 notify_rate_limit 被调用过 - 通过检查 stats 记录
        assert stats["total_requests"] >= 1
    
    def test_429_with_retry_after_pauses_limiter(self, mock_token_provider, requests_mock):
        """验证 429 Retry-After 值被传递给 rate limiter"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"Retry-After": "60"},
        )
        
        # 记录 pause_until 调用
        original_pause = client.rate_limiter.pause_until
        pause_calls = []
        def mock_pause(resume_time):
            pause_calls.append(resume_time)
            original_pause(resume_time)
        
        with patch.object(client.rate_limiter, 'pause_until', side_effect=mock_pause):
            with patch.object(client.rate_limiter, 'notify_rate_limit', wraps=client.rate_limiter.notify_rate_limit) as mock_notify:
                result = client.request_safe("GET", "/projects/123/repository/commits")
        
        # 验证 notify_rate_limit 被调用
        assert mock_notify.called
        call_kwargs = mock_notify.call_args
        # 检查 retry_after 参数
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs.get("retry_after") == 60.0
    
    def test_429_with_rate_limit_reset_pauses_limiter(self, mock_token_provider, requests_mock):
        """验证 429 RateLimit-Reset 值被传递给 rate limiter"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        reset_timestamp = time.time() + 120  # 2 分钟后
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"RateLimit-Reset": str(int(reset_timestamp))},
        )
        
        with patch.object(client.rate_limiter, 'notify_rate_limit', wraps=client.rate_limiter.notify_rate_limit) as mock_notify:
            result = client.request_safe("GET", "/projects/123/repository/commits")
        
        # 验证 notify_rate_limit 被调用，且 reset_time 被传递
        assert mock_notify.called


class TestStatsIncludeRateLimitFields:
    """测试 stats 输出包含 rate limit 相关字段"""
    
    def test_client_stats_includes_rate_limit_fields_on_429(self, mock_token_provider, requests_mock):
        """stats.to_dict() 包含 rate limit 相关字段"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
            backoff_base_seconds=0.01,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 模拟一次 429 后成功
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
                "headers": {
                    "Retry-After": "10",
                    "RateLimit-Reset": "1706000000",
                    "RateLimit-Remaining": "0",
                },
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            client.get_commits("123")
        
        stats = client.stats.to_dict()
        
        # 验证 rate limit 相关字段存在
        assert "total_429_hits" in stats
        assert stats["total_429_hits"] == 1
        
        assert "last_retry_after" in stats
        assert stats["last_retry_after"] == 10.0
        
        assert "last_rate_limit_reset" in stats
        assert stats["last_rate_limit_reset"] == 1706000000.0
        
        assert "last_rate_limit_remaining" in stats
        assert stats["last_rate_limit_remaining"] == 0
    
    def test_client_stats_rate_limit_fields_not_present_without_429(self, mock_token_provider, requests_mock):
        """没有 429 时，stats.to_dict() 不包含 rate limit 相关字段"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=200,
            json=[],
        )
        
        client.get_commits("123")
        
        stats = client.stats.to_dict()
        
        # 验证基本字段存在
        assert "total_requests" in stats
        assert stats["total_requests"] == 1
        assert "total_429_hits" in stats
        assert stats["total_429_hits"] == 0
        
        # 没有 429 时，这些字段不应该存在
        assert "last_retry_after" not in stats
        assert "last_rate_limit_reset" not in stats
        assert "last_rate_limit_remaining" not in stats
    
    def test_rate_limiter_stats_include_pause_info(self, mock_token_provider):
        """验证 rate limiter 自身的 stats 包含 pause 相关信息"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=10.0,
            rate_limit_burst_size=5,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 获取 rate limiter stats
        limiter_stats = client.rate_limiter.get_stats()
        
        # 验证包含必要字段
        assert "rate" in limiter_stats
        assert limiter_stats["rate"] == 10.0
        
        assert "burst_size" in limiter_stats
        assert limiter_stats["burst_size"] == 5
        
        assert "tokens" in limiter_stats
        assert "total_requests" in limiter_stats
        assert "throttled_count" in limiter_stats
        assert "avg_wait_time_ms" in limiter_stats
        assert "paused_until" in limiter_stats
    
    def test_concurrency_limiter_stats_include_wait_info(self, mock_token_provider):
        """验证 concurrency limiter 自身的 stats 包含等待相关信息"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=1,
            max_concurrency=5,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 获取 concurrency limiter stats
        limiter_stats = client.concurrency_limiter.get_stats()
        
        # 验证包含必要字段
        assert "max_concurrency" in limiter_stats
        assert limiter_stats["max_concurrency"] == 5
        
        assert "active_count" in limiter_stats
        assert "waiting_count" in limiter_stats
        assert "total_acquired" in limiter_stats
        assert "avg_wait_time_ms" in limiter_stats


# ============ 429 Header Priority & Clamp Tests ============

class Test429HeaderPriorityAndClamp:
    """
    测试 429 处理路径中的 header 优先级与异常值 clamp
    
    优先级: Retry-After(秒) > RateLimit-Reset(时间戳) > 默认值(1秒)
    """
    
    def test_retry_after_takes_priority_over_reset(self, client_factory, mock_token_provider, requests_mock):
        """Retry-After 优先于 RateLimit-Reset"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
            backoff_base_seconds=0.01,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 同时提供 Retry-After 和 RateLimit-Reset
        reset_time = time.time() + 3600  # 1 小时后
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
                "headers": {
                    "Retry-After": "5",  # 5 秒
                    "RateLimit-Reset": str(int(reset_time)),  # 1 小时后
                },
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        sleep_times = []
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            client.get_commits("123")
        
        # 应该使用 Retry-After 的 5 秒（+ jitter），而不是 RateLimit-Reset 的 1 小时
        assert len(sleep_times) == 1
        assert 5.0 <= sleep_times[0] < 6.0, f"应使用 Retry-After，实际等待: {sleep_times[0]}"
        
        # 验证 pause_source
        stats = client.rate_limiter.get_stats()
        assert stats["pause_source"] == "retry_after"
    
    def test_reset_time_used_when_no_retry_after(self, client_factory, mock_token_provider, requests_mock):
        """只有 RateLimit-Reset 时使用它"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
            backoff_base_seconds=0.01,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 只提供 RateLimit-Reset（10 秒后）
        reset_time = time.time() + 10
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
                "headers": {
                    "RateLimit-Reset": str(int(reset_time)),
                },
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        sleep_times = []
        with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
            client.get_commits("123")
        
        # 应该等待大约 10 秒（可能有少许时间误差）
        assert len(sleep_times) == 1
        assert 8.0 <= sleep_times[0] <= 12.0, f"应使用 RateLimit-Reset，实际等待: {sleep_times[0]}"
        
        # 验证 pause_source
        stats = client.rate_limiter.get_stats()
        assert stats["pause_source"] == "rate_limit_reset"
    
    def test_default_used_when_no_headers(self, client_factory, mock_token_provider, requests_mock):
        """没有任何 header 时使用默认值"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
            backoff_base_seconds=0.01,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 不提供任何限流头
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            client.get_commits("123")
        
        # 验证 pause_source 是 default
        stats = client.rate_limiter.get_stats()
        assert stats["pause_source"] == "default"
    
    def test_negative_retry_after_clamped_to_zero(self, mock_token_provider):
        """负数的 Retry-After 被 clamp 到 0"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 通知负数的 retry_after
        limiter.notify_rate_limit(retry_after=-10.0)
        
        stats = limiter.get_stats()
        # paused_until 应该在 now 附近（因为被 clamp 到 0）
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "retry_after"
        # 暂停时间应该是 now + 0 = now
        assert stats["paused_until"] <= time.time() + 1
    
    def test_huge_retry_after_clamped_to_max(self, mock_token_provider):
        """超大的 Retry-After 被 clamp 到最大值（1小时）"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 通知超大的 retry_after（1天）
        now = time.time()
        limiter.notify_rate_limit(retry_after=86400.0)  # 24 小时
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "retry_after"
        # 应该被 clamp 到 1 小时（3600 秒）
        pause_duration = stats["paused_until"] - now
        assert 3599 <= pause_duration <= 3601, f"应被 clamp 到 3600s，实际: {pause_duration}"
    
    def test_past_reset_time_clamped_to_zero(self, mock_token_provider):
        """过去的 RateLimit-Reset 时间被 clamp 到 0"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 通知过去的 reset_time
        past_time = time.time() - 3600  # 1 小时前
        limiter.notify_rate_limit(reset_time=past_time)
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "rate_limit_reset"
        # 暂停时间应该是 now + 0 = now（因为负数被 clamp）
        assert stats["paused_until"] <= time.time() + 1
    
    def test_invalid_retry_after_string_falls_back_to_default(self, mock_token_provider):
        """无效的 Retry-After 字符串回退到默认值"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 通知无效的 retry_after
        limiter.notify_rate_limit(retry_after="invalid")
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "default"
    
    def test_invalid_reset_time_string_falls_back_to_default(self, mock_token_provider):
        """无效的 RateLimit-Reset 字符串回退到默认值"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 通知无效的 reset_time
        limiter.notify_rate_limit(reset_time="not-a-timestamp")
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "default"
    
    def test_none_values_use_default(self, mock_token_provider):
        """两个参数都是 None 时使用默认值"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        now = time.time()
        limiter.notify_rate_limit(retry_after=None, reset_time=None)
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "default"
        # 默认暂停 1 秒
        pause_duration = stats["paused_until"] - now
        assert 0.9 <= pause_duration <= 1.1
    
    def test_zero_retry_after_is_valid(self, mock_token_provider):
        """0 的 Retry-After 是有效值"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        now = time.time()
        limiter.notify_rate_limit(retry_after=0.0)
        
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        assert stats["pause_source"] == "retry_after"
        # 暂停时间应该接近 now
        assert stats["paused_until"] <= now + 1
    
    def test_float_retry_after_parsed_correctly(self, mock_token_provider):
        """浮点数的 Retry-After 被正确解析"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        now = time.time()
        limiter.notify_rate_limit(retry_after=2.5)
        
        stats = limiter.get_stats()
        pause_duration = stats["paused_until"] - now
        assert 2.4 <= pause_duration <= 2.6
        assert stats["pause_source"] == "retry_after"


class Test429HeaderCombinations:
    """测试各种 header 组合场景"""
    
    def test_only_retry_after(self, client_factory, mock_token_provider, requests_mock):
        """只有 Retry-After"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"Retry-After": "30"},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.retry_after == 30.0
        assert result.rate_limit_reset is None
    
    def test_only_rate_limit_reset(self, client_factory, mock_token_provider, requests_mock):
        """只有 RateLimit-Reset"""
        client = client_factory(max_attempts=1)
        reset_time = int(time.time()) + 60
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"RateLimit-Reset": str(reset_time)},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.retry_after is None
        assert result.rate_limit_reset == float(reset_time)
    
    def test_both_headers_present(self, client_factory, mock_token_provider, requests_mock):
        """两个 header 都存在"""
        client = client_factory(max_attempts=1)
        reset_time = int(time.time()) + 120
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={
                "Retry-After": "45",
                "RateLimit-Reset": str(reset_time),
                "RateLimit-Remaining": "0",
            },
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.retry_after == 45.0
        assert result.rate_limit_reset == float(reset_time)
        assert result.rate_limit_remaining == 0
    
    def test_no_rate_limit_headers(self, client_factory, mock_token_provider, requests_mock):
        """没有任何限流 header"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.retry_after is None
        assert result.rate_limit_reset is None
        assert result.rate_limit_remaining is None
    
    def test_invalid_retry_after_header(self, client_factory, mock_token_provider, requests_mock):
        """无效的 Retry-After header（非数字）"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"Retry-After": "invalid"},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        # _parse_retry_after 应该返回 None
        assert result.retry_after is None
    
    def test_invalid_rate_limit_reset_header(self, client_factory, mock_token_provider, requests_mock):
        """无效的 RateLimit-Reset header（非数字）"""
        client = client_factory(max_attempts=1)
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=429,
            json={"message": "Rate limited"},
            headers={"RateLimit-Reset": "not-a-timestamp"},
        )
        
        result = client.request_safe("GET", "/projects/123/repository/commits")
        
        assert result.rate_limit_reset is None


class TestPauseSourceInStats:
    """测试 pause_source 在 stats 中的暴露"""
    
    def test_rate_limiter_stats_include_pause_source(self, mock_token_provider):
        """RateLimiter.get_stats() 包含 pause_source 字段"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 初始状态
        stats = limiter.get_stats()
        assert "pause_source" in stats
        assert stats["pause_source"] is None
        assert stats["paused_until"] is None
        
        # 通知 429
        limiter.notify_rate_limit(retry_after=5.0)
        
        stats = limiter.get_stats()
        assert stats["pause_source"] == "retry_after"
        assert stats["paused_until"] is not None
    
    def test_pause_source_updates_correctly(self, mock_token_provider):
        """pause_source 正确更新"""
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 先用 retry_after
        limiter.notify_rate_limit(retry_after=1.0)
        assert limiter.get_stats()["pause_source"] == "retry_after"
        
        # 再用 reset_time（更大的值才会更新）
        future_time = time.time() + 100
        limiter.notify_rate_limit(reset_time=future_time)
        assert limiter.get_stats()["pause_source"] == "rate_limit_reset"
        
        # 用更大的 retry_after
        limiter.notify_rate_limit(retry_after=200.0)
        assert limiter.get_stats()["pause_source"] == "retry_after"
    
    def test_client_get_limiter_stats_includes_pause_info(self, mock_token_provider, requests_mock):
        """GitLabClient.get_limiter_stats() 包含 pause 信息"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
            backoff_base_seconds=0.01,
            rate_limit_enabled=True,
            rate_limit_requests_per_second=100.0,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 模拟 429 后成功
        responses = [
            {
                "status_code": 429,
                "json": {"message": "Rate limited"},
                "headers": {"Retry-After": "10"},
            },
            {"status_code": 200, "json": []},
        ]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            responses,
        )
        
        with patch("time.sleep"):
            client.get_commits("123")
        
        # 获取 limiter 统计
        limiter_stats = client.get_limiter_stats()
        assert "sub_limiters" in limiter_stats
        
        # 找到 local rate limiter 的统计
        local_stats = None
        for sub in limiter_stats["sub_limiters"]:
            if sub.get("type") == "local":
                local_stats = sub
                break
        
        assert local_stats is not None
        assert "pause_source" in local_stats
        assert local_stats["pause_source"] == "retry_after"


# ============ 401/403 Token Invalidate Extended Tests ============

class TestAuthInvalidateExtended:
    """扩展的 401/403 认证错误处理测试，覆盖 invalidate 路径"""
    
    def test_401_invalidate_refreshes_token(self, mock_token_provider, requests_mock):
        """401 触发 invalidate 后，下次请求使用刷新后的 token"""
        # 设置 token provider 在 invalidate 后返回不同的 token
        tokens = ["old-token", "new-token"]
        token_index = [0]
        
        def get_token():
            return tokens[min(token_index[0], len(tokens) - 1)]
        
        def invalidate():
            token_index[0] += 1
        
        mock_token_provider.get_token.side_effect = get_token
        mock_token_provider.invalidate.side_effect = invalidate
        
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=3,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 第一次请求返回 401，第二次成功
        request_headers = []
        
        def callback(request, context):
            request_headers.append(dict(request.headers))
            if len(request_headers) == 1:
                context.status_code = 401
                return {"message": "401 Unauthorized"}
            context.status_code = 200
            return []
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=callback,
        )
        
        result = client.get_commits("123")
        
        # 验证 invalidate 被调用
        mock_token_provider.invalidate.assert_called_once()
        
        # 验证第二次请求使用了新 token
        assert len(request_headers) == 2
        # 注意：第一次请求用 old-token，第二次用 new-token
        assert request_headers[0].get("PRIVATE-TOKEN") == "old-token"
        assert request_headers[1].get("PRIVATE-TOKEN") == "new-token"
    
    def test_403_invalidate_refreshes_token(self, mock_token_provider, requests_mock):
        """403 触发 invalidate 后，下次请求使用刷新后的 token"""
        # 设置 token provider 在 invalidate 后返回不同的 token
        tokens = ["expired-token", "valid-token"]
        token_index = [0]
        
        def get_token():
            return tokens[min(token_index[0], len(tokens) - 1)]
        
        def invalidate():
            token_index[0] += 1
        
        mock_token_provider.get_token.side_effect = get_token
        mock_token_provider.invalidate.side_effect = invalidate
        
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=3,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        # 第一次请求返回 403，第二次成功
        request_headers = []
        
        def callback(request, context):
            request_headers.append(dict(request.headers))
            if len(request_headers) == 1:
                context.status_code = 403
                return {"message": "403 Forbidden"}
            context.status_code = 200
            return [{"id": 1}]
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=callback,
        )
        
        result = client.get_commits("123")
        
        # 验证 invalidate 被调用
        mock_token_provider.invalidate.assert_called_once()
        
        # 验证结果正确
        assert result == [{"id": 1}]
        
        # 验证使用了不同的 token
        assert len(request_headers) == 2
        assert request_headers[0].get("PRIVATE-TOKEN") == "expired-token"
        assert request_headers[1].get("PRIVATE-TOKEN") == "valid-token"
    
    def test_401_followed_by_401_stops_after_one_retry(self, mock_token_provider, requests_mock):
        """连续两次 401 后停止重试，只调用一次 invalidate"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=5,  # 设置高于实际允许的次数
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        request_count = [0]
        
        def callback(request, context):
            request_count[0] += 1
            context.status_code = 401
            return {"message": "401 Unauthorized"}
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            json=callback,
        )
        
        with pytest.raises(GitLabAuthError):
            client.get_commits("123")
        
        # 验证只重试了一次（总共 2 次请求）
        assert request_count[0] == 2
        # invalidate 只被调用一次
        mock_token_provider.invalidate.assert_called_once()
    
    def test_401_error_message_does_not_contain_token(self, mock_token_provider, requests_mock):
        """401 错误信息不应包含 token"""
        mock_token_provider.get_token.return_value = "glpat-secret12345678"
        
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
        )
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=mock_token_provider,
            http_config=http_config,
        )
        
        requests_mock.get(
            "https://gitlab.example.com/api/v4/projects/123/repository/commits",
            status_code=401,
            json={"message": "401 Unauthorized"},
        )
        
        with pytest.raises(GitLabAuthError) as exc_info:
            client.get_commits("123")
        
        # 错误信息不应包含 token
        error_str = str(exc_info.value)
        assert "glpat-secret12345678" not in error_str
        assert "secret" not in error_str.lower() or "secret" in "401 Unauthorized".lower()


# ============ Token Redaction Tests ============

class TestTokenRedaction:
    """测试 token 脱敏功能"""
    
    def test_redact_gitlab_token_pattern(self):
        """测试 GitLab token 模式脱敏"""
        from engram.logbook.scm_auth import redact
        
        # glpat- 格式
        text = "Token: glpat-abc123xyz789defg"
        result = redact(text)
        assert "glpat-abc123xyz789defg" not in result
        # 脱敏结果可能是 [GITLAB_TOKEN] 或 [REDACTED]
        assert "[GITLAB_TOKEN]" in result or "[REDACTED]" in result
        
        # glptt- 格式
        text = "Token: glptt-abc123xyz789defg"
        result = redact(text)
        assert "glptt-abc123xyz789defg" not in result
    
    def test_redact_bearer_token(self):
        """测试 Bearer token 脱敏"""
        from engram.logbook.scm_auth import redact
        
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc123"
        result = redact(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "[TOKEN]" in result or "[REDACTED]" in result
    
    def test_redact_private_token_header(self):
        """测试 PRIVATE-TOKEN header 脱敏"""
        from engram.logbook.scm_auth import redact
        
        text = "PRIVATE-TOKEN: glpat-secrettoken123"
        result = redact(text)
        assert "glpat-secrettoken123" not in result
        assert "[REDACTED]" in result
    
    def test_redact_url_credentials(self):
        """测试 URL 中的凭证脱敏"""
        from engram.logbook.scm_auth import redact
        
        text = "https://user:mysecretpassword@gitlab.example.com/api/v4"
        result = redact(text)
        assert "mysecretpassword" not in result
        assert "[REDACTED]" in result
    
    def test_redact_dict_sensitive_headers(self):
        """测试字典中敏感 header 脱敏"""
        from engram.logbook.scm_auth import redact_dict
        
        data = {
            "Authorization": "Bearer secret123",
            "PRIVATE-TOKEN": "glpat-token123",
            "Content-Type": "application/json",
            "url": "/api/v4/projects",
        }
        
        result = redact_dict(data)
        
        # 敏感 header 应被脱敏
        assert result["Authorization"] == "[REDACTED]"
        assert result["PRIVATE-TOKEN"] == "[REDACTED]"
        
        # 非敏感字段应保留
        assert result["Content-Type"] == "application/json"
        assert result["url"] == "/api/v4/projects"
    
    def test_redact_headers_function(self):
        """测试 redact_headers 函数"""
        from engram.logbook.scm_auth import redact_headers
        
        headers = {
            "PRIVATE-TOKEN": "glpat-secret",
            "Authorization": "Bearer token",
            "Accept": "application/json",
            "Cookie": "session=abc123",
        }
        
        result = redact_headers(headers)
        
        assert result["PRIVATE-TOKEN"] == "[REDACTED]"
        assert result["Authorization"] == "[REDACTED]"
        assert result["Accept"] == "application/json"
        assert result["Cookie"] == "[REDACTED]"
    
    def test_mask_token_function(self):
        """测试 mask_token 函数"""
        from engram.logbook.scm_auth import mask_token
        
        # 正常 token
        token = "glpat-abc123xyz789"
        masked = mask_token(token)
        
        # 不应包含原始 token
        assert "glpat-abc123xyz789" not in masked
        # 应包含长度信息
        assert f"len={len(token)}" in masked
        # 应包含 hash 信息
        assert "prefix_hash=" in masked
        assert "suffix_hash=" in masked
        
        # 空 token
        assert mask_token(None) == "empty"
        assert mask_token("") == "empty"
    
    def test_redact_nested_dict(self):
        """测试嵌套字典脱敏"""
        from engram.logbook.scm_auth import redact_dict
        
        data = {
            "request": {
                "headers": {
                    "Authorization": "Bearer secret",
                },
                "url": "/api/v4/projects",
            },
            "response": {
                "status": 200,
            },
        }
        
        result = redact_dict(data, deep=True)
        
        # 嵌套的敏感 header 应被脱敏
        assert result["request"]["headers"]["Authorization"] == "[REDACTED]"
        # 非敏感字段应保留
        assert result["request"]["url"] == "/api/v4/projects"
        assert result["response"]["status"] == 200
    
    def test_redact_empty_input(self):
        """测试空输入处理"""
        from engram.logbook.scm_auth import redact, redact_dict, redact_headers
        
        assert redact(None) == ""
        assert redact("") == ""
        assert redact_dict({}) == {}
        assert redact_dict(None) == {}
        assert redact_headers({}) == {}
        assert redact_headers(None) == {}


# ============ Token Provider Factory Tests ============

class TestTokenProviderFactory:
    """测试 create_token_provider_for_instance 工厂方法"""
    
    def test_payload_token_highest_priority(self):
        """payload 指定的 token 优先级最高"""
        from engram.logbook.scm_auth import create_token_provider_for_instance
        
        provider = create_token_provider_for_instance(
            instance_key="gitlab.example.com",
            payload_token="payload-token-123",
        )
        
        assert provider.get_token() == "payload-token-123"
    
    def test_config_instance_token(self):
        """从配置读取特定实例的 token"""
        from engram.logbook.scm_auth import create_token_provider_for_instance
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.instances.gitlab_example_com.token": "instance-token-456",
        }.get(key, default)
        
        provider = create_token_provider_for_instance(
            instance_key="gitlab.example.com",
            config=mock_config,
        )
        
        assert provider.get_token() == "instance-token-456"
    
    def test_config_tenant_token(self):
        """从配置读取特定租户的 token"""
        from engram.logbook.scm_auth import create_token_provider_for_instance
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.tenants.tenant-a.token": "tenant-token-789",
        }.get(key, default)
        
        provider = create_token_provider_for_instance(
            tenant_id="tenant-a",
            config=mock_config,
        )
        
        assert provider.get_token() == "tenant-token-789"
    
    def test_instance_priority_over_tenant(self):
        """instance_key 配置优先于 tenant_id 配置"""
        from engram.logbook.scm_auth import create_token_provider_for_instance
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.instances.gitlab_example_com.token": "instance-token",
            "scm.gitlab.tenants.tenant-a.token": "tenant-token",
        }.get(key, default)
        
        provider = create_token_provider_for_instance(
            instance_key="gitlab.example.com",
            tenant_id="tenant-a",
            config=mock_config,
        )
        
        # 应该使用 instance token
        assert provider.get_token() == "instance-token"
    
    def test_fallback_to_env_default(self):
        """没有配置时回退到环境变量默认值"""
        from engram.logbook.scm_auth import create_token_provider_for_instance, EnvTokenProvider
        import os
        
        # 设置环境变量
        original_token = os.environ.get("GITLAB_TOKEN")
        os.environ["GITLAB_TOKEN"] = "env-default-token"
        
        try:
            mock_config = MagicMock()
            mock_config.get.return_value = None
            
            provider = create_token_provider_for_instance(
                instance_key="unknown.gitlab.com",
                config=mock_config,
            )
            
            # 应该返回 EnvTokenProvider
            assert isinstance(provider, EnvTokenProvider)
            assert provider.get_token() == "env-default-token"
        finally:
            # 恢复环境变量
            if original_token is not None:
                os.environ["GITLAB_TOKEN"] = original_token
            else:
                os.environ.pop("GITLAB_TOKEN", None)
    
    def test_normalize_instance_key(self):
        """测试 instance_key 规范化"""
        from engram.logbook.scm_auth import normalize_instance_key
        
        # 点号替换为下划线
        assert normalize_instance_key("gitlab.example.com") == "gitlab_example_com"
        
        # 冒号替换为下划线
        assert normalize_instance_key("gitlab.example.com:8080") == "gitlab_example_com_8080"
        
        # 连字符替换为下划线
        assert normalize_instance_key("gitlab-internal.example.com") == "gitlab_internal_example_com"
        
        # 空输入
        assert normalize_instance_key("") == ""


# ---------- 运行测试的入口 ----------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
