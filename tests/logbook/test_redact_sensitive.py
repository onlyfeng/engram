#!/usr/bin/env python3
"""
test_redact_sensitive.py - 敏感信息脱敏单元测试

验证:
- redact() 函数对 token、password、Authorization/PRIVATE-TOKEN header 做脱敏
- redact_dict() 函数对字典中的敏感字段做脱敏
- redact_headers() 函数对 HTTP headers 做脱敏
- 构造包含 token 的异常文本，断言最终日志/返回结构不包含明文 token
"""

import pytest
import sys
import os

# 确保能够导入 engram_logbook 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engram.logbook.scm_auth import redact, redact_dict, redact_headers


class TestRedactFunction:
    """测试 redact() 函数"""

    def test_redact_gitlab_token_glpat(self):
        """测试 GitLab Personal Access Token (glpat-xxx) 脱敏"""
        text = "Error: invalid glpat-abc123def456ghi789jkl used"
        result = redact(text)
        # 核心验证：明文 token 不在结果中
        assert "glpat-abc123def456ghi789jkl" not in result
        # 验证已被脱敏（可能是 [GITLAB_TOKEN] 或 [REDACTED]）
        assert "[GITLAB_TOKEN]" in result or "[REDACTED]" in result

    def test_redact_gitlab_token_glptt(self):
        """测试 GitLab Project Access Token (glptt-xxx) 脱敏"""
        text = "Using glptt-xyz987654321abcdef for auth"
        result = redact(text)
        # 核心验证：明文 token 不在结果中
        assert "glptt-xyz987654321abcdef" not in result
        # 验证已被脱敏
        assert "[GITLAB_TOKEN]" in result or "[REDACTED]" in result

    def test_redact_bearer_token(self):
        """测试 Bearer token 脱敏"""
        text = "Header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact(text)
        # 核心验证：JWT token 值不在结果中
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        # 验证已被脱敏
        assert "[TOKEN]" in result or "[REDACTED]" in result

    def test_redact_private_token_header(self):
        """测试 PRIVATE-TOKEN header 脱敏"""
        text = "PRIVATE-TOKEN: glpat-secrettoken123456789"
        result = redact(text)
        assert "glpat-secrettoken123456789" not in result
        assert "PRIVATE-TOKEN:" in result
        assert "[REDACTED]" in result

    def test_redact_authorization_header(self):
        """测试 Authorization header 脱敏"""
        text = "Failed with Authorization: Basic dXNlcjpwYXNz"
        result = redact(text)
        # 核心验证：Base64 凭证不在结果中
        assert "dXNlcjpwYXNz" not in result
        # 验证已被脱敏
        assert "[REDACTED]" in result or "Authorization:" in result

    def test_redact_password_in_url(self):
        """测试 URL 中 password 参数脱敏"""
        text = "Connecting to https://example.com?password=secret123"
        result = redact(text)
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_redact_token_in_url(self):
        """测试 URL 中 token 参数脱敏"""
        text = "Request to https://api.example.com?token=abc123xyz"
        result = redact(text)
        assert "abc123xyz" not in result
        assert "[REDACTED]" in result

    def test_redact_url_credentials(self):
        """测试 URL 中用户凭证脱敏 (user:pass@host)"""
        text = "Cloning from https://user:mypassword@gitlab.com/repo.git"
        result = redact(text)
        assert "mypassword" not in result
        assert "[REDACTED]" in result

    def test_redact_none_input(self):
        """测试 None 输入返回空字符串"""
        result = redact(None)
        assert result == ""

    def test_redact_empty_string(self):
        """测试空字符串输入返回空字符串"""
        result = redact("")
        assert result == ""

    def test_redact_preserves_non_sensitive(self):
        """测试非敏感信息保留不变"""
        text = "Normal log message without any secrets"
        result = redact(text)
        assert result == text

    def test_redact_multiple_tokens(self):
        """测试多个 token 同时脱敏"""
        text = "Token1: glpat-token1xxxxx Token2: glpat-token2yyyyy"
        result = redact(text)
        assert "glpat-token1xxxxx" not in result
        assert "glpat-token2yyyyy" not in result
        assert result.count("[GITLAB_TOKEN]") == 2


class TestRedactDict:
    """测试 redact_dict() 函数"""

    def test_redact_dict_authorization_key(self):
        """测试 Authorization 键脱敏"""
        data = {
            "Authorization": "Bearer secret_token_value",
            "url": "/api/v4/projects",
        }
        result = redact_dict(data)
        assert result["Authorization"] == "[REDACTED]"
        assert result["url"] == "/api/v4/projects"

    def test_redact_dict_private_token_key(self):
        """测试 PRIVATE-TOKEN 键脱敏"""
        data = {
            "PRIVATE-TOKEN": "glpat-secret123456789",
            "Accept": "application/json",
        }
        result = redact_dict(data)
        assert result["PRIVATE-TOKEN"] == "[REDACTED]"
        assert result["Accept"] == "application/json"

    def test_redact_dict_nested(self):
        """测试嵌套字典脱敏"""
        data = {
            "request": {
                "headers": {
                    "Authorization": "Bearer xxx",
                },
                "url": "/api/v4/projects",
            },
        }
        result = redact_dict(data)
        assert result["request"]["headers"]["Authorization"] == "[REDACTED]"
        assert result["request"]["url"] == "/api/v4/projects"

    def test_redact_dict_string_values(self):
        """测试字符串值中的敏感信息脱敏"""
        data = {
            "error": "Failed with glpat-secret123456789 auth",
            "status": 401,
        }
        result = redact_dict(data)
        # 核心验证：明文 token 不在结果中
        assert "glpat-secret123456789" not in result["error"]
        # 验证已被脱敏
        assert "[GITLAB_TOKEN]" in result["error"] or "[REDACTED]" in result["error"]
        assert result["status"] == 401

    def test_redact_dict_preserves_original(self):
        """测试不修改原始字典"""
        data = {
            "Authorization": "Bearer xxx",
        }
        result = redact_dict(data)
        assert data["Authorization"] == "Bearer xxx"
        assert result["Authorization"] == "[REDACTED]"

    def test_redact_dict_empty(self):
        """测试空字典返回空字典"""
        result = redact_dict({})
        assert result == {}

    def test_redact_dict_none(self):
        """测试 None 返回空字典"""
        result = redact_dict(None)
        assert result == {}


class TestRedactHeaders:
    """测试 redact_headers() 函数"""

    def test_redact_headers_private_token(self):
        """测试 PRIVATE-TOKEN header 脱敏"""
        headers = {
            "PRIVATE-TOKEN": "glpat-secret123",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        result = redact_headers(headers)
        assert result["PRIVATE-TOKEN"] == "[REDACTED]"
        assert result["Accept"] == "application/json"
        assert result["Content-Type"] == "application/json"

    def test_redact_headers_authorization(self):
        """测试 Authorization header 脱敏"""
        headers = {
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9",
            "User-Agent": "engram/1.0",
        }
        result = redact_headers(headers)
        assert result["Authorization"] == "[REDACTED]"
        assert result["User-Agent"] == "engram/1.0"

    def test_redact_headers_cookie(self):
        """测试 Cookie header 脱敏"""
        headers = {
            "Cookie": "session=abc123; token=xyz789",
        }
        result = redact_headers(headers)
        assert result["Cookie"] == "[REDACTED]"

    def test_redact_headers_case_insensitive(self):
        """测试 header 名称大小写不敏感"""
        headers = {
            "private-token": "glpat-secret",
            "AUTHORIZATION": "Bearer xxx",
        }
        result = redact_headers(headers)
        assert result["private-token"] == "[REDACTED]"
        assert result["AUTHORIZATION"] == "[REDACTED]"

    def test_redact_headers_empty(self):
        """测试空 headers 返回空字典"""
        result = redact_headers({})
        assert result == {}

    def test_redact_headers_none(self):
        """测试 None 返回空字典"""
        result = redact_headers(None)
        assert result == {}


class TestRedactInExceptionMessage:
    """测试异常消息中的敏感信息脱敏"""

    def test_exception_message_with_token(self):
        """构造包含 token 的异常文本，验证脱敏后不包含明文 token"""
        # 模拟 GitLab API 错误响应
        error_msg = (
            "GitLab API Error: 401 Unauthorized\n"
            "Request: GET https://gitlab.example.com/api/v4/projects\n"
            "Headers: PRIVATE-TOKEN: glpat-abc123def456xyz789\n"
            "Response: {\"error\": \"invalid_token\", \"error_description\": \"Token was revoked\"}"
        )
        
        result = redact(error_msg)
        
        # 验证明文 token 被脱敏
        assert "glpat-abc123def456xyz789" not in result
        assert "[REDACTED]" in result or "[GITLAB_TOKEN]" in result
        
        # 验证非敏感信息保留
        assert "GitLab API Error: 401 Unauthorized" in result
        assert "gitlab.example.com" in result

    def test_exception_with_url_credentials(self):
        """构造包含 URL 凭证的异常文本，验证脱敏"""
        error_msg = (
            "SVN Error: Authorization failed\n"
            "Repository: svn://admin:secret123@svn.example.com/repo/trunk\n"
            "Error code: E170001"
        )
        
        result = redact(error_msg)
        
        # 验证密码被脱敏
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_exception_dict_structure(self):
        """验证包含敏感信息的字典结构脱敏后不包含明文 token"""
        error_details = {
            "endpoint": "https://gitlab.example.com/api/v4/projects?private_token=glpat-secret",
            "status_code": 401,
            "error_message": "Token glpat-abc123xyz789 is invalid",
            "headers": {
                "PRIVATE-TOKEN": "glpat-abc123xyz789",
            },
        }
        
        result = redact_dict(error_details)
        
        # 验证字典中所有敏感信息被脱敏
        assert "glpat-secret" not in str(result)
        assert "glpat-abc123xyz789" not in str(result)
        
        # 验证结构保持
        assert result["status_code"] == 401
        assert "headers" in result

    def test_to_dict_like_method(self):
        """模拟 GitLabAPIResult.to_dict() 方法的脱敏效果"""
        # 模拟 API 结果转换为字典
        api_result = {
            "success": False,
            "status_code": 401,
            "endpoint": "https://gitlab.com/api/v4/projects?private_token=glpat-secret123",
            "error_message": "Invalid token: glpat-secret123",
        }
        
        # 模拟 to_dict 的脱敏逻辑
        redacted_result = {
            "success": api_result["success"],
            "status_code": api_result["status_code"],
            "endpoint": redact(api_result["endpoint"]),
            "error_message": redact(api_result["error_message"]),
        }
        
        # 验证脱敏效果
        assert "glpat-secret123" not in redacted_result["endpoint"]
        assert "glpat-secret123" not in redacted_result["error_message"]
        assert "[REDACTED]" in redacted_result["endpoint"] or "[GITLAB_TOKEN]" in redacted_result["error_message"]


class TestMetaJsonSafety:
    """测试 meta_json 字段安全性"""

    def test_meta_json_source_fetch_error(self):
        """验证 meta_json.source_fetch_error 字段脱敏"""
        source_fetch_error = (
            "Request failed: PRIVATE-TOKEN: glpat-abc123 returned 401"
        )
        
        redacted = redact(source_fetch_error)
        
        assert "glpat-abc123" not in redacted
        assert "[REDACTED]" in redacted

    def test_meta_json_original_endpoint(self):
        """验证 meta_json.original_endpoint 字段脱敏"""
        original_endpoint = (
            "https://gitlab.com/api/v4/projects?access_token=secret123"
        )
        
        redacted = redact(original_endpoint)
        
        # token 参数应该被脱敏
        assert "secret123" not in redacted

    def test_meta_json_complete_structure(self):
        """验证完整的 meta_json 结构脱敏"""
        meta_json = {
            "materialize_status": "failed",
            "degraded": True,
            "degrade_reason": "http_error",
            "source_fetch_error": "401 Unauthorized: token glpat-xyz789 expired",
            "original_endpoint": "https://gitlab.com/api/v4/projects/123/repository/commits/abc/diff",
        }
        
        # 模拟写入 meta_json 前的脱敏处理
        safe_meta = {
            "materialize_status": meta_json["materialize_status"],
            "degraded": meta_json["degraded"],
            "degrade_reason": meta_json["degrade_reason"],
            "source_fetch_error": redact(meta_json["source_fetch_error"]),
            "original_endpoint": redact(meta_json["original_endpoint"]),
        }
        
        # 验证敏感信息被脱敏
        assert "glpat-xyz789" not in safe_meta["source_fetch_error"]
        
        # 验证非敏感信息保留
        assert safe_meta["materialize_status"] == "failed"
        assert safe_meta["degraded"] is True


class TestWorkerComponentRedaction:
    """
    测试 Worker 组件中的敏感信息脱敏
    
    验证 worker 在处理任务结果、错误信息时正确脱敏敏感数据
    """

    def test_worker_error_with_token(self):
        """验证 worker 错误消息中的 token 被脱敏"""
        error_msg = (
            "GitLab API 认证失败: PRIVATE-TOKEN: glpat-abc123xyz789 "
            "returned 401 Unauthorized"
        )
        result = redact(error_msg)
        # 核心验证：明文 token 不在结果中
        assert "glpat-abc123xyz789" not in result
        # 验证错误上下文保留
        assert "401 Unauthorized" in result

    def test_worker_sync_result_error_summary(self):
        """验证 worker sync_run 的 error_summary 脱敏"""
        error_summary = {
            "error": "Request failed with Bearer eyJhbGciOiJIUzI1NiJ9.xxx token",
            "error_category": "auth_error",
            "request_headers": {
                "PRIVATE-TOKEN": "glpat-secret123",
            },
        }
        result = redact_dict(error_summary)
        # 验证 Bearer token 被脱敏
        assert "eyJhbGciOiJIUzI1NiJ9" not in str(result)
        # 验证 PRIVATE-TOKEN header 被脱敏
        assert "glpat-secret123" not in str(result)
        # 验证结构保留
        assert result["error_category"] == "auth_error"

    def test_worker_payload_token_redaction(self):
        """验证 worker payload 中的 token 字段脱敏"""
        payload = {
            "repo_id": 123,
            "job_type": "gitlab_commits",
            "token": "glpat-payload-token-xxx",
            "gitlab_instance": "gitlab.example.com",
        }
        result = redact_dict(payload)
        # token 值应被脱敏（redact_dict 会处理字符串值）
        assert "glpat-payload-token-xxx" not in str(result)
        # 非敏感字段保留
        assert result["repo_id"] == 123
        assert result["job_type"] == "gitlab_commits"


class TestReaperComponentRedaction:
    """
    测试 Reaper 组件中的敏感信息脱敏
    
    验证 reaper 在处理过期任务、记录错误时正确脱敏敏感数据
    """

    def test_reaper_last_error_with_token(self):
        """验证 reaper 处理 last_error 中的 token 脱敏"""
        last_error = (
            "Sync failed: PRIVATE-TOKEN: glpat-reaper-test-token123 "
            "returned HTTP 403 Forbidden"
        )
        result = redact(last_error)
        # 核心验证：明文 token 不在结果中
        assert "glpat-reaper-test-token123" not in result
        # 验证错误上下文保留
        assert "HTTP 403 Forbidden" in result

    def test_reaper_error_summary_redaction(self):
        """验证 reaper 的 error_summary 结构脱敏"""
        error_summary = {
            "error_type": "lease_lost",
            "error_category": "timeout",
            "message": "Reaped: sync run timed out, last error with token glpat-xxx",
        }
        result = redact_dict(error_summary)
        # 验证 token 被脱敏
        assert "glpat-xxx" not in result["message"]
        # 验证结构保留
        assert result["error_type"] == "lease_lost"
        assert result["error_category"] == "timeout"

    def test_reaper_locked_by_with_sensitive_info(self):
        """验证 locked_by 中包含敏感信息时的脱敏"""
        # 虽然 locked_by 通常只包含 worker_id，但如果误包含敏感信息应被脱敏
        locked_by = "worker-with-token-glpat-secret123-in-name"
        result = redact(locked_by)
        # 如果 locked_by 中意外包含 token 模式，应被脱敏
        assert "glpat-secret123" not in result


class TestSchedulerComponentRedaction:
    """
    测试 Scheduler 组件中的敏感信息脱敏
    
    验证 scheduler 在记录暂停原因、任务参数时正确脱敏敏感数据
    """

    def test_scheduler_pause_reason_redaction(self):
        """验证 scheduler 暂停原因中的敏感信息脱敏"""
        pause_reason = (
            "error_budget_exceeded: last error was 'auth failed with "
            "PRIVATE-TOKEN: glpat-scheduler-token456'"
        )
        result = redact(pause_reason)
        # 核心验证：明文 token 不在结果中
        assert "glpat-scheduler-token456" not in result
        # 验证原因上下文保留
        assert "error_budget_exceeded" in result

    def test_scheduler_job_payload_redaction(self):
        """验证 scheduler 入队任务的 payload 脱敏"""
        payload = {
            "reason": "cursor_age",
            "cursor_age_seconds": 3600,
            "failure_rate": 0.1,
            "scheduled_at": "2024-01-01T00:00:00Z",
            "gitlab_instance": "gitlab.example.com",
            "token": "glpat-should-not-be-here",  # 敏感字段
        }
        result = redact_dict(payload)
        # 验证 token 被脱敏
        assert "glpat-should-not-be-here" not in str(result)
        # 验证非敏感字段保留
        assert result["reason"] == "cursor_age"
        assert result["cursor_age_seconds"] == 3600


class TestCrossComponentRedaction:
    """
    测试跨组件的敏感信息脱敏一致性
    
    验证从 scheduler -> worker -> reaper 整个流程中的脱敏一致性
    """

    def test_end_to_end_token_not_leaked(self):
        """验证 token 在整个处理流程中不泄露"""
        # 模拟 scheduler 创建的 payload
        scheduler_payload = {
            "reason": "incremental",
            "gitlab_instance": "https://user:glpat-xxx@gitlab.com/api",
        }
        
        # 模拟 worker 处理后的错误
        worker_error = {
            "success": False,
            "error": f"Auth failed for {scheduler_payload['gitlab_instance']}",
            "error_category": "auth_error",
        }
        
        # 模拟 reaper 记录的错误摘要
        reaper_summary = {
            "message": f"Reaped: {worker_error['error']}",
            "original_payload": scheduler_payload,
        }
        
        # 对最终结果进行脱敏
        final_result = redact_dict(reaper_summary)
        
        # 验证 token 在任何层级都不存在
        final_str = str(final_result)
        assert "glpat-xxx" not in final_str
        assert ":glpat-" not in final_str

    def test_multiple_sensitive_patterns(self):
        """验证多种敏感信息模式同时脱敏"""
        mixed_data = {
            "url": "https://admin:secret123@gitlab.com/api?token=abc123",
            "headers": {
                "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9",
                "PRIVATE-TOKEN": "glpat-multi-token",
            },
            "error": "Failed with password=secret in URL",
        }
        
        result = redact_dict(mixed_data)
        result_str = str(result)
        
        # 验证所有敏感信息被脱敏
        assert "secret123" not in result_str
        assert "abc123" not in result_str
        assert "eyJhbGciOiJIUzI1NiJ9" not in result_str
        assert "glpat-multi-token" not in result_str


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
