# -*- coding: utf-8 -*-
"""
test_gitlab_client_integration.py - GitLab HTTP 客户端集成测试

通过环境变量 ENGRAM_GITLAB_INTEGRATION=1 启用测试。

测试覆盖:
1. 无 token 401 认证错误
2. 触发 429 限流（通过快速多请求或低速率限制）
3. 模拟 5xx 服务器错误（通过无效路径触发 404/500）
4. 日志/异常文本中的 redact() 脱敏断言

环境变量配置:
    export ENGRAM_GITLAB_INTEGRATION=1
    export GITLAB_URL=https://gitlab.example.com
    export GITLAB_TOKEN=glpat-xxx
    export GITLAB_PROJECT_ID=123  # 或 namespace/project

运行测试:
    pytest tests/test_gitlab_client_integration.py -v
"""

import logging
import os
import re
import sys
import time
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest
import requests

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_step1.gitlab_client import (
    GitLabClient,
    GitLabAPIError,
    GitLabAPIResult,
    GitLabRateLimitError,
    GitLabAuthError,
    GitLabServerError,
    GitLabErrorCategory,
    HttpConfig,
    StaticTokenProvider,
    redact,
    redact_headers,
)
from engram_step1.scm_auth import redact as scm_auth_redact, redact_dict


# ============ 测试启用条件 ============

GITLAB_INTEGRATION_ENABLED = os.environ.get(
    "ENGRAM_GITLAB_INTEGRATION", ""
).lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not GITLAB_INTEGRATION_ENABLED,
    reason="GitLab 集成测试未启用，设置 ENGRAM_GITLAB_INTEGRATION=1 启用"
)


# ============ Fixtures ============


@pytest.fixture(scope="module")
def gitlab_config():
    """GitLab 配置（从环境变量读取）"""
    config = {
        "url": os.environ.get("GITLAB_URL", "https://gitlab.com"),
        "token": os.environ.get("GITLAB_TOKEN", ""),
        "project_id": os.environ.get("GITLAB_PROJECT_ID", ""),
    }
    
    if not config["token"]:
        pytest.skip("缺少 GITLAB_TOKEN 环境变量")
    if not config["project_id"]:
        pytest.skip("缺少 GITLAB_PROJECT_ID 环境变量")
    
    return config


@pytest.fixture(scope="module")
def gitlab_client(gitlab_config):
    """创建配置正确的 GitLab 客户端"""
    http_config = HttpConfig(
        timeout_seconds=30.0,
        max_attempts=2,
        backoff_base_seconds=0.5,
        backoff_max_seconds=10.0,
    )
    return GitLabClient(
        base_url=gitlab_config["url"],
        private_token=gitlab_config["token"],
        http_config=http_config,
    )


@pytest.fixture
def invalid_token_client(gitlab_config):
    """创建使用无效 token 的 GitLab 客户端"""
    http_config = HttpConfig(
        timeout_seconds=10.0,
        max_attempts=1,  # 不重试
        backoff_base_seconds=0.1,
    )
    return GitLabClient(
        base_url=gitlab_config["url"],
        private_token="invalid-token-for-testing",
        http_config=http_config,
    )


@pytest.fixture
def log_capture():
    """捕获日志输出以验证脱敏"""
    captured = []
    
    class LogHandler(logging.Handler):
        def emit(self, record):
            captured.append(self.format(record))
    
    handler = LogHandler()
    handler.setLevel(logging.DEBUG)
    
    # 获取 gitlab_client 模块的 logger
    logger = logging.getLogger("engram_step1.gitlab_client")
    original_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    
    yield captured
    
    logger.removeHandler(handler)
    logger.setLevel(original_level)


# ============ 基础连接测试 ============


class TestGitLabConnection:
    """GitLab 连接测试"""

    def test_connection_success(self, gitlab_client, gitlab_config):
        """成功连接到 GitLab 并获取 commits"""
        project_id = gitlab_config["project_id"]
        
        # 获取最近的 commits（即使项目没有 commits 也应该成功返回空列表）
        commits = gitlab_client.get_commits(project_id, per_page=1)
        
        # 验证返回类型
        assert isinstance(commits, list)

    def test_get_merge_requests(self, gitlab_client, gitlab_config):
        """获取 merge requests 列表"""
        project_id = gitlab_config["project_id"]
        
        mrs = gitlab_client.get_merge_requests(project_id, state="all", per_page=1)
        
        assert isinstance(mrs, list)


# ============ 401 认证错误测试 ============


class TestAuthError401:
    """测试 401 认证错误"""

    def test_invalid_token_returns_401(self, invalid_token_client, gitlab_config):
        """使用无效 token 应返回 401 认证错误"""
        project_id = gitlab_config["project_id"]
        
        with pytest.raises(GitLabAuthError) as exc_info:
            invalid_token_client.get_commits(project_id)
        
        error = exc_info.value
        assert error.status_code == 401
        assert error.category == GitLabErrorCategory.AUTH_ERROR
        # 验证错误消息包含 401
        assert "401" in str(error)

    def test_invalid_token_error_message_redacted(self, invalid_token_client, gitlab_config):
        """验证 401 错误消息中的 token 被脱敏"""
        project_id = gitlab_config["project_id"]
        
        with pytest.raises(GitLabAuthError) as exc_info:
            invalid_token_client.get_commits(project_id)
        
        error = exc_info.value
        error_str = str(error)
        
        # 核心验证：明文 token 不在错误消息中
        assert "invalid-token-for-testing" not in error_str
        
        # 验证 to_dict() 结果也被脱敏
        if hasattr(error, 'details') and error.details:
            details_str = str(error.details)
            assert "invalid-token-for-testing" not in details_str

    def test_401_triggers_token_invalidate(self, gitlab_config):
        """验证 401 错误触发 TokenProvider.invalidate()"""
        mock_provider = MagicMock()
        mock_provider.get_token.return_value = "invalid-token"
        
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=2,
            backoff_base_seconds=0.1,
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            token_provider=mock_provider,
            http_config=http_config,
        )
        
        with pytest.raises(GitLabAuthError):
            client.get_commits(gitlab_config["project_id"])
        
        # 验证 invalidate 被调用
        mock_provider.invalidate.assert_called()

    def test_empty_token_returns_401(self, gitlab_config):
        """使用空 token 应返回 401"""
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=1,
        )
        
        # 使用特殊的空 token（实际发送请求时 header 会是空的）
        # 注意：StaticTokenProvider 会拒绝空 token，所以我们使用空格
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token="   ",  # 只有空格的 token
            http_config=http_config,
        )
        
        with pytest.raises((GitLabAuthError, ValueError)):
            client.get_commits(gitlab_config["project_id"])


# ============ 429 限流错误测试 ============


class TestRateLimited429:
    """测试 429 限流错误"""

    def test_rapid_requests_may_trigger_429(self, gitlab_client, gitlab_config):
        """快速发送多个请求可能触发 429（或成功但有 429 统计）"""
        project_id = gitlab_config["project_id"]
        
        # 重置统计
        gitlab_client.stats.reset()
        
        # 快速发送多个请求
        request_count = 5
        success_count = 0
        rate_limit_count = 0
        
        for i in range(request_count):
            try:
                result = gitlab_client.request_safe(
                    "GET",
                    f"/projects/{project_id}/repository/commits",
                    params={"per_page": 1}
                )
                if result.success:
                    success_count += 1
                elif result.error_category == GitLabErrorCategory.RATE_LIMITED:
                    rate_limit_count += 1
            except GitLabRateLimitError:
                rate_limit_count += 1
            except Exception:
                pass  # 忽略其他错误
        
        # 验证至少有一些请求成功（除非全部被限流）
        assert success_count > 0 or rate_limit_count > 0
        
        # 记录统计（用于调试）
        stats = gitlab_client.stats.to_dict()
        print(f"Stats: {stats}")

    def test_429_error_includes_retry_after(self, gitlab_config):
        """验证 429 错误结果包含 retry_after 字段"""
        # 使用 Mock 模拟 429 响应
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=1,  # 不重试以捕获原始 429
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=gitlab_config["token"],
            http_config=http_config,
        )
        
        # Mock 请求返回 429
        with patch.object(client.session, 'request') as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {"Retry-After": "60"}
            mock_response.json.return_value = {"message": "Rate limit exceeded"}
            mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
            mock_response.text = '{"message": "Rate limit exceeded"}'
            mock_request.return_value = mock_response
            
            result = client.request_safe("GET", "/test")
            
            assert result.success is False
            assert result.error_category == GitLabErrorCategory.RATE_LIMITED
            assert result.retry_after == 60.0

    def test_429_recorded_in_stats(self, gitlab_config):
        """验证 429 命中被记录在统计中"""
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=2,
            backoff_base_seconds=0.1,
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=gitlab_config["token"],
            http_config=http_config,
        )
        client.stats.reset()
        
        # Mock 第一次 429，第二次成功
        with patch.object(client.session, 'request') as mock_request:
            mock_429 = MagicMock()
            mock_429.status_code = 429
            mock_429.headers = {"Retry-After": "0.1"}
            mock_429.json.return_value = {"message": "Rate limited"}
            mock_429.raise_for_status.side_effect = requests.HTTPError(response=mock_429)
            mock_429.text = '{"message": "Rate limited"}'
            
            mock_200 = MagicMock()
            mock_200.status_code = 200
            mock_200.json.return_value = []
            mock_200.raise_for_status.return_value = None
            
            mock_request.side_effect = [mock_429, mock_200]
            
            with patch("time.sleep"):  # 跳过等待
                result = client.request_safe("GET", "/test")
            
            assert result.success is True
            
            stats = client.stats.to_dict()
            assert stats["total_429_hits"] >= 1


# ============ 5xx 服务器错误测试 ============


class TestServerError5xx:
    """测试 5xx 服务器错误"""

    def test_nonexistent_endpoint_error(self, gitlab_client, gitlab_config):
        """访问不存在的端点应返回 4xx 错误"""
        # 使用一个肯定不存在的端点
        result = gitlab_client.request_safe(
            "GET",
            "/nonexistent/endpoint/that/does/not/exist/12345"
        )
        
        assert result.success is False
        assert result.status_code in (400, 401, 403, 404, 500, 502, 503)

    def test_invalid_project_id_error(self, gitlab_client):
        """使用无效的项目 ID 应返回错误"""
        # 使用一个不可能存在的项目 ID
        result = gitlab_client.request_safe(
            "GET",
            "/projects/99999999999/repository/commits"
        )
        
        assert result.success is False
        # 可能是 404 (Not Found) 或 403 (Forbidden)
        assert result.status_code in (403, 404)

    def test_mock_500_error_handling(self, gitlab_config):
        """模拟 500 服务器错误处理"""
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=2,
            backoff_base_seconds=0.1,
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=gitlab_config["token"],
            http_config=http_config,
        )
        
        # Mock 返回 500 错误
        with patch.object(client.session, 'request') as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.json.return_value = {"error": "Internal Server Error"}
            mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
            mock_response.text = '{"error": "Internal Server Error"}'
            mock_request.return_value = mock_response
            
            with patch("time.sleep"):  # 跳过退避等待
                with pytest.raises(GitLabServerError) as exc_info:
                    client.get_commits("123")
            
            error = exc_info.value
            assert error.status_code == 500
            assert error.category == GitLabErrorCategory.SERVER_ERROR

    def test_mock_502_gateway_error(self, gitlab_config):
        """模拟 502 Bad Gateway 错误处理"""
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=2,
            backoff_base_seconds=0.1,
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=gitlab_config["token"],
            http_config=http_config,
        )
        
        with patch.object(client.session, 'request') as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 502
            mock_response.json.return_value = {"message": "Bad Gateway"}
            mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
            mock_response.text = '{"message": "Bad Gateway"}'
            mock_request.return_value = mock_response
            
            with patch("time.sleep"):
                with pytest.raises(GitLabServerError) as exc_info:
                    client.get_commits("123")
            
            assert exc_info.value.status_code == 502

    def test_mock_503_service_unavailable(self, gitlab_config):
        """模拟 503 Service Unavailable 错误处理"""
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=2,
            backoff_base_seconds=0.1,
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=gitlab_config["token"],
            http_config=http_config,
        )
        
        with patch.object(client.session, 'request') as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.json.return_value = {"message": "Service Unavailable"}
            mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
            mock_response.text = '{"message": "Service Unavailable"}'
            mock_request.return_value = mock_response
            
            with patch("time.sleep"):
                with pytest.raises(GitLabServerError) as exc_info:
                    client.get_commits("123")
            
            assert exc_info.value.status_code == 503


# ============ Redact 脱敏测试（集成测试中需要真实连接的部分） ============


class TestRedactSensitiveInfoIntegration:
    """测试敏感信息脱敏（需要集成测试环境）"""

    def test_gitlab_api_result_to_dict_redacted(self, gitlab_config):
        """测试 GitLabAPIResult.to_dict() 结果被脱敏"""
        # 创建包含敏感信息的结果
        result = GitLabAPIResult(
            success=False,
            status_code=401,
            endpoint=f"https://gitlab.com/api/v4/projects?private_token=glpat-secret123",
            error_category=GitLabErrorCategory.AUTH_ERROR,
            error_message="Invalid token: glpat-secret123",
        )
        
        result_dict = result.to_dict()
        result_str = str(result_dict)
        
        # 验证敏感信息被脱敏
        assert "glpat-secret123" not in result_str
        
        # 验证结构保持
        assert result_dict["success"] is False
        assert result_dict["status_code"] == 401

    def test_log_messages_no_token_leak(self, gitlab_config, log_capture):
        """验证日志消息中不包含明文 token"""
        http_config = HttpConfig(
            timeout_seconds=10.0,
            max_attempts=1,
        )
        
        test_token = "glpat-test123456789xyz"
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=test_token,
            http_config=http_config,
        )
        
        # 触发认证错误以产生日志
        try:
            client.get_commits("invalid-project-id")
        except Exception:
            pass
        
        # 检查所有捕获的日志
        for log_msg in log_capture:
            assert test_token not in log_msg, f"Token 泄露到日志: {log_msg}"


# ============ Redact 脱敏纯函数测试（无需集成测试环境） ============

# 注意: 这些纯函数测试在 test_redact_sensitive.py 中已有覆盖，
# 这里额外添加一些特定于 GitLab 集成场景的脱敏断言测试


class TestRedactPureFunctions:
    """测试敏感信息脱敏纯函数（集成测试环境中额外的验证）"""

    def test_redact_gitlab_token_glpat(self):
        """测试 GitLab Personal Access Token 脱敏"""
        text = "Error with glpat-abc123def456xyz789"
        result = redact(text)
        
        # 核心验证：明文 token 不在结果中
        assert "glpat-abc123def456xyz789" not in result
        assert "[GITLAB_TOKEN]" in result

    def test_redact_private_token_header(self):
        """测试 PRIVATE-TOKEN header 值脱敏"""
        text = "PRIVATE-TOKEN: glpat-secrettoken123"
        result = redact(text)
        
        assert "glpat-secrettoken123" not in result
        assert "[REDACTED]" in result

    def test_redact_authorization_header(self):
        """测试 Authorization header 值脱敏"""
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"
        result = redact(text)
        
        assert "eyJhbGciOiJIUzI1NiJ9" not in result

    def test_redact_url_credentials(self):
        """测试 URL 中的凭证脱敏"""
        text = "https://user:password123@gitlab.com/repo.git"
        result = redact(text)
        
        assert "password123" not in result
        assert "[REDACTED]" in result

    def test_redact_headers_function(self):
        """测试 redact_headers 函数"""
        headers = {
            "PRIVATE-TOKEN": "glpat-secret123",
            "Accept": "application/json",
            "Authorization": "Bearer xyz",
        }
        
        result = redact_headers(headers)
        
        assert result["PRIVATE-TOKEN"] == "[REDACTED]"
        assert result["Authorization"] == "[REDACTED]"
        assert result["Accept"] == "application/json"

    def test_exception_error_message_redacted(self):
        """测试异常消息中的敏感信息脱敏"""
        # 构造包含 token 的错误消息
        error_msg = (
            "GitLab API Error: 401 Unauthorized\n"
            "Request failed with PRIVATE-TOKEN: glpat-xyz789\n"
            "Endpoint: https://gitlab.com/api/v4/projects"
        )
        
        redacted = redact(error_msg)
        
        # 验证 token 被脱敏
        assert "glpat-xyz789" not in redacted
        # 验证其他信息保留
        assert "GitLab API Error: 401 Unauthorized" in redacted

    def test_redact_dict_nested_structure(self):
        """测试嵌套字典结构脱敏"""
        data = {
            "request": {
                "headers": {
                    "PRIVATE-TOKEN": "glpat-secret",
                    "Accept": "application/json",
                },
                "url": "/api/v4/projects",
            },
            "response": {
                "error": "Token glpat-abc123 is invalid",
            },
        }
        
        result = redact_dict(data)
        
        # 验证敏感信息被脱敏
        assert result["request"]["headers"]["PRIVATE-TOKEN"] == "[REDACTED]"
        assert "glpat-abc123" not in result["response"]["error"]
        
        # 验证非敏感信息保留
        assert result["request"]["headers"]["Accept"] == "application/json"
        assert result["request"]["url"] == "/api/v4/projects"
    
    def test_redact_gitlab_api_result_structure(self):
        """测试 GitLabAPIResult.to_dict() 结果结构脱敏"""
        result = GitLabAPIResult(
            success=False,
            status_code=401,
            endpoint="https://gitlab.com/api/v4/projects?private_token=glpat-test123",
            error_category=GitLabErrorCategory.AUTH_ERROR,
            error_message="Invalid token: glpat-test123",
        )
        
        result_dict = result.to_dict()
        result_str = str(result_dict)
        
        # 验证敏感信息被脱敏
        assert "glpat-test123" not in result_str
        
        # 验证结构保持
        assert result_dict["success"] is False
        assert result_dict["status_code"] == 401


# ============ 完整流程测试 ============


class TestIntegrationFlow:
    """完整流程集成测试"""

    def test_full_api_flow(self, gitlab_client, gitlab_config):
        """完整的 API 调用流程"""
        project_id = gitlab_config["project_id"]
        
        # 1. 获取 commits
        commits = gitlab_client.get_commits(project_id, per_page=5)
        assert isinstance(commits, list)
        
        # 2. 如果有 commits，获取第一个 commit 的 diff
        if commits:
            sha = commits[0].get("id")
            if sha:
                diff_result = gitlab_client.get_commit_diff_safe(project_id, sha)
                # 可能成功也可能因 diff 太大失败
                assert isinstance(diff_result, GitLabAPIResult)
        
        # 3. 获取 merge requests
        mrs = gitlab_client.get_merge_requests(project_id, state="all", per_page=5)
        assert isinstance(mrs, list)
        
        # 4. 如果有 MR，获取第一个 MR 的 discussions
        if mrs:
            mr_iid = mrs[0].get("iid")
            if mr_iid:
                discussions = gitlab_client.get_mr_discussions(project_id, mr_iid, per_page=5)
                assert isinstance(discussions, list)

    def test_stats_tracking(self, gitlab_client, gitlab_config):
        """验证请求统计跟踪"""
        project_id = gitlab_config["project_id"]
        
        # 重置统计
        gitlab_client.stats.reset()
        
        # 执行几个请求
        gitlab_client.get_commits(project_id, per_page=1)
        gitlab_client.get_merge_requests(project_id, per_page=1)
        
        # 验证统计
        stats = gitlab_client.stats.to_dict()
        assert stats["total_requests"] >= 2
        assert stats["successful_requests"] >= 2
        assert stats["failed_requests"] == 0


# ============ 边界条件测试 ============


class TestEdgeCases:
    """边界条件测试"""

    def test_project_id_with_namespace(self, gitlab_config):
        """测试带 namespace 的项目 ID（如 group/project）"""
        http_config = HttpConfig(
            timeout_seconds=30.0,
            max_attempts=2,
        )
        
        client = GitLabClient(
            base_url=gitlab_config["url"],
            private_token=gitlab_config["token"],
            http_config=http_config,
        )
        
        # 使用 namespace/project 格式
        project_id = gitlab_config["project_id"]
        if "/" in project_id:
            # 项目 ID 包含斜杠，测试 URL 编码
            result = client.request_safe(
                "GET",
                f"/projects/{client._encode_project_id(project_id)}/repository/commits",
                params={"per_page": 1}
            )
            # 应该成功或返回可预期的错误
            assert isinstance(result, GitLabAPIResult)

    def test_special_characters_in_error_message(self):
        """测试错误消息中特殊字符的处理"""
        error_msg = "Error: <token>glpat-abc123</token> in XML"
        result = redact(error_msg)
        
        assert "glpat-abc123" not in result

    def test_multiple_tokens_in_text(self):
        """测试文本中多个 token 的脱敏"""
        text = "Token1: glpat-token1xxxxx Token2: glpat-token2yyyyy"
        result = redact(text)
        
        assert "glpat-token1xxxxx" not in result
        assert "glpat-token2yyyyy" not in result
        assert result.count("[GITLAB_TOKEN]") == 2


# ============ 运行入口 ============


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
