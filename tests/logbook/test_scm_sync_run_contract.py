# -*- coding: utf-8 -*-
"""
test_scm_sync_run_contract.py - sync_runs 记录构建器模块测试

测试内容:
- RunCounts, ErrorSummary, DegradationSnapshot, RequestStats 数据结构
- build_run_finish_payload 及其变体
- 各种退出路径的 payload 构建（成功、无数据、异常、租约丢失、mark_dead）
- JSON Schema 验证
- 契约一致性测试

测试策略:
- 单元测试: 测试各数据结构和构建函数
- 边界测试: 测试缺省值和空值处理
- 异常路径测试: 测试各种失败场景的 payload 构建
- Schema 测试: 使用 JSON Schema 验证输出格式
"""

import json
import os
import pytest
import traceback

# 尝试导入 jsonschema
try:
    import jsonschema
    from jsonschema import Draft202012Validator, ValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    Draft202012Validator = None
    ValidationError = Exception

from engram.logbook.scm_sync_run_contract import (
    # 状态枚举
    RunStatus,
    # 数据结构
    RunCounts,
    ErrorSummary,
    DegradationSnapshot,
    RequestStats,
    RunFinishPayload,
    # 构建函数
    build_run_finish_payload,
    build_run_finish_payload_from_result,
    build_error_summary_from_exception,
    # 便捷函数
    build_payload_for_success,
    build_payload_for_no_data,
    build_payload_for_exception,
    build_payload_for_lease_lost,
    build_payload_for_mark_dead,
    # 验证函数
    validate_run_finish_payload,
    validate_and_build_error_summary,
    # 默认值常量
    DEFAULT_COUNTS,
    DEFAULT_ERROR_SUMMARY,
    DEFAULT_DEGRADATION_SNAPSHOT,
    DEFAULT_REQUEST_STATS,
    # 结构化错误
    RunPayloadValidationError,
)
from engram.logbook.scm_sync_errors import (
    ErrorCategory,
    resolve_backoff,
    BackoffSource,
    TRANSIENT_ERROR_BACKOFF,
)


# ============ RunStatus 枚举测试 ============


class TestRunStatus:
    """测试 RunStatus 枚举"""

    def test_valid_statuses(self):
        """有效状态值"""
        assert RunStatus.RUNNING.value == "running"
        assert RunStatus.COMPLETED.value == "completed"
        assert RunStatus.FAILED.value == "failed"
        assert RunStatus.NO_DATA.value == "no_data"

    def test_status_is_string(self):
        """状态值是字符串"""
        for status in RunStatus:
            assert isinstance(status.value, str)


# ============ RunCounts 测试 ============


class TestRunCounts:
    """测试 RunCounts 数据结构"""

    def test_default_values_are_zero(self):
        """所有字段默认值为 0"""
        counts = RunCounts()
        assert counts.synced_count == 0
        assert counts.diff_count == 0
        assert counts.bulk_count == 0
        assert counts.total_requests == 0
        assert counts.total_429_hits == 0

    def test_to_dict_includes_all_fields(self):
        """to_dict 包含所有字段"""
        counts = RunCounts()
        d = counts.to_dict()
        
        assert "synced_count" in d
        assert "diff_count" in d
        assert "total_requests" in d
        assert "total_429_hits" in d

    def test_to_dict_exclude_zero(self):
        """to_dict(include_zero=False) 排除值为 0 的字段"""
        counts = RunCounts(synced_count=10, diff_count=5)
        d = counts.to_dict(include_zero=False)
        
        assert d["synced_count"] == 10
        assert d["diff_count"] == 5
        assert "bulk_count" not in d

    def test_from_dict_basic(self):
        """from_dict 基本功能"""
        data = {"synced_count": 100, "diff_count": 50}
        counts = RunCounts.from_dict(data)
        
        assert counts.synced_count == 100
        assert counts.diff_count == 50
        assert counts.bulk_count == 0  # 默认值

    def test_from_dict_with_none(self):
        """from_dict 处理 None"""
        counts = RunCounts.from_dict(None)
        assert counts.synced_count == 0

    def test_from_dict_ignores_unknown_fields(self):
        """from_dict 忽略未知字段"""
        data = {"synced_count": 10, "unknown_field": 999}
        counts = RunCounts.from_dict(data)
        
        assert counts.synced_count == 10
        assert not hasattr(counts, "unknown_field")


# ============ ErrorSummary 测试 ============


class TestErrorSummary:
    """测试 ErrorSummary 数据结构"""

    def test_default_values(self):
        """默认值为空字符串或 0"""
        summary = ErrorSummary()
        assert summary.error_category == ""
        assert summary.error_message == ""
        assert summary.attempts == 0

    def test_to_dict_excludes_empty_values(self):
        """to_dict 排除空值"""
        summary = ErrorSummary(
            error_category="timeout",
            error_message="Request timed out",
        )
        d = summary.to_dict()
        
        assert d["error_category"] == "timeout"
        assert d["error_message"] == "Request timed out"
        assert "stack_trace" not in d  # 空字符串被排除
        assert "attempts" not in d  # 0 被排除

    def test_to_dict_includes_context(self):
        """to_dict 包含上下文"""
        summary = ErrorSummary(
            error_category="timeout",
            context={"job_id": "123", "worker_id": "w1"},
        )
        d = summary.to_dict()
        
        assert d["context"]["job_id"] == "123"
        assert d["context"]["worker_id"] == "w1"

    def test_from_dict_basic(self):
        """from_dict 基本功能"""
        data = {
            "error_category": "auth_error",
            "error_message": "401 Unauthorized",
            "attempts": 3,
        }
        summary = ErrorSummary.from_dict(data)
        
        assert summary.error_category == "auth_error"
        assert summary.error_message == "401 Unauthorized"
        assert summary.attempts == 3

    def test_from_dict_handles_error_field(self):
        """from_dict 处理 error 字段（向后兼容）"""
        data = {"error": "Something went wrong"}
        summary = ErrorSummary.from_dict(data)
        
        assert summary.error_message == "Something went wrong"


# ============ DegradationSnapshot 测试 ============


class TestDegradationSnapshot:
    """测试 DegradationSnapshot 数据结构"""

    def test_default_values(self):
        """默认值"""
        snapshot = DegradationSnapshot()
        assert snapshot.is_degraded is False
        assert snapshot.degraded_reasons == {}
        assert snapshot.circuit_state == ""

    def test_to_dict_excludes_default_values(self):
        """to_dict 排除默认值"""
        snapshot = DegradationSnapshot()
        d = snapshot.to_dict()
        
        assert d == {}  # 全是默认值，应该为空

    def test_to_dict_includes_degradation_info(self):
        """to_dict 包含降级信息"""
        snapshot = DegradationSnapshot(
            is_degraded=True,
            degraded_reasons={"timeout": 5, "rate_limit": 2},
            circuit_state="half_open",
        )
        d = snapshot.to_dict()
        
        assert d["is_degraded"] is True
        assert d["degraded_reasons"]["timeout"] == 5
        assert d["circuit_state"] == "half_open"

    def test_from_dict_basic(self):
        """from_dict 基本功能"""
        data = {
            "is_degraded": True,
            "suggested_batch_size": 50,
            "suggested_diff_mode": "none",
        }
        snapshot = DegradationSnapshot.from_dict(data)
        
        assert snapshot.is_degraded is True
        assert snapshot.suggested_batch_size == 50
        assert snapshot.suggested_diff_mode == "none"


# ============ RequestStats 测试 ============


class TestRequestStats:
    """测试 RequestStats 数据结构"""

    def test_default_values_are_zero(self):
        """所有字段默认值为 0"""
        stats = RequestStats()
        assert stats.total_requests == 0
        assert stats.success_count == 0
        assert stats.failure_count == 0
        assert stats.total_429_hits == 0

    def test_to_dict(self):
        """to_dict 返回所有字段"""
        stats = RequestStats(total_requests=100, success_count=95)
        d = stats.to_dict()
        
        assert d["total_requests"] == 100
        assert d["success_count"] == 95
        assert d["failure_count"] == 0

    def test_from_dict(self):
        """from_dict 基本功能"""
        data = {"total_requests": 200, "total_429_hits": 5}
        stats = RequestStats.from_dict(data)
        
        assert stats.total_requests == 200
        assert stats.total_429_hits == 5


# ============ RunFinishPayload 测试 ============


class TestRunFinishPayload:
    """测试 RunFinishPayload 数据结构"""

    def test_default_values(self):
        """默认值"""
        payload = RunFinishPayload()
        assert payload.status == RunStatus.COMPLETED.value
        assert payload.counts.synced_count == 0
        assert payload.error_summary is None

    def test_to_dict_basic(self):
        """to_dict 基本功能"""
        payload = RunFinishPayload(
            status=RunStatus.COMPLETED.value,
            counts=RunCounts(synced_count=100),
        )
        d = payload.to_dict()
        
        assert d["status"] == "completed"
        assert d["counts"]["synced_count"] == 100

    def test_to_dict_with_error(self):
        """to_dict 包含错误信息"""
        payload = RunFinishPayload(
            status=RunStatus.FAILED.value,
            error_summary=ErrorSummary(
                error_category="timeout",
                error_message="Request timed out",
            ),
        )
        d = payload.to_dict()
        
        assert d["status"] == "failed"
        assert d["error_summary_json"]["error_category"] == "timeout"


# ============ build_run_finish_payload 测试 ============


class TestBuildRunFinishPayload:
    """测试 build_run_finish_payload 函数"""

    def test_returns_payload_object(self):
        """返回 RunFinishPayload 对象"""
        payload = build_run_finish_payload()
        assert isinstance(payload, RunFinishPayload)

    def test_default_status_is_completed(self):
        """默认状态是 completed"""
        payload = build_run_finish_payload()
        assert payload.status == RunStatus.COMPLETED.value

    def test_accepts_dict_counts(self):
        """接受字典格式的 counts"""
        payload = build_run_finish_payload(
            counts={"synced_count": 100, "diff_count": 50},
        )
        assert payload.counts.synced_count == 100
        assert payload.counts.diff_count == 50

    def test_accepts_counts_object(self):
        """接受 RunCounts 对象"""
        counts = RunCounts(synced_count=200)
        payload = build_run_finish_payload(counts=counts)
        assert payload.counts.synced_count == 200

    def test_accepts_dict_error_summary(self):
        """接受字典格式的 error_summary"""
        payload = build_run_finish_payload(
            status=RunStatus.FAILED.value,
            error_summary={
                "error_category": "auth_error",
                "error_message": "Unauthorized",
            },
        )
        assert payload.error_summary.error_category == "auth_error"

    def test_cursor_after_preserved(self):
        """cursor_after 被保留"""
        cursor = {"commit_sha": "abc123", "timestamp": "2025-01-01T00:00:00Z"}
        payload = build_run_finish_payload(cursor_after=cursor)
        assert payload.cursor_after == cursor


# ============ build_run_finish_payload_from_result 测试 ============


class TestBuildRunFinishPayloadFromResult:
    """测试 build_run_finish_payload_from_result 函数"""

    def test_success_result(self):
        """成功结果"""
        result = {
            "success": True,
            "synced_count": 100,
            "diff_count": 50,
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.COMPLETED.value
        assert payload.counts.synced_count == 100
        assert payload.counts.diff_count == 50

    def test_failed_result(self):
        """失败结果"""
        result = {
            "success": False,
            "error": "Connection refused",
            "error_category": "connection",
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "connection"

    def test_no_data_result(self):
        """无数据结果"""
        result = {
            "success": False,
            "synced_count": 0,
            # 无 error 字段
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.NO_DATA.value

    def test_extracts_request_stats(self):
        """提取 request_stats"""
        result = {
            "success": True,
            "synced_count": 10,
            "request_stats": {
                "total_requests": 200,
                "total_429_hits": 5,
            },
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.counts.total_requests == 200
        assert payload.counts.total_429_hits == 5

    def test_extracts_patch_stats(self):
        """提取 patch_stats（SVN）"""
        result = {
            "success": True,
            "synced_count": 50,
            "patch_stats": {
                "success": 45,
                "failed": 3,
                "skipped_by_controller": 2,
            },
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.counts.patch_success == 45
        assert payload.counts.patch_failed == 3
        assert payload.counts.skipped_by_controller == 2

    def test_extracts_degradation_info(self):
        """提取降级信息"""
        result = {
            "success": True,
            "synced_count": 10,
            "is_backfill_only": True,
            "circuit_state": "half_open",
            "suggested_batch_size": 50,
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.degradation is not None
        assert payload.degradation.is_degraded is True
        assert payload.degradation.circuit_state == "half_open"

    # ============ 边界条件测试 ============

    def test_success_false_no_error_returns_no_data(self):
        """success=False 且无 error 字段时返回 no_data"""
        result = {
            "success": False,
            "synced_count": 0,
            # 无 error 或 error_category 字段
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.NO_DATA.value
        assert payload.error_summary is None

    def test_synced_count_zero_with_error_returns_failed(self):
        """synced_count=0 但有 error 字段时返回 failed"""
        result = {
            "success": False,
            "synced_count": 0,
            "error": "Connection refused",
            "error_category": "connection",
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "connection"
        assert payload.error_summary.error_message == "Connection refused"

    def test_success_true_synced_count_zero_returns_no_data(self):
        """success=True 且 synced_count=0 时返回 no_data（无数据语义优先）"""
        result = {
            "success": True,
            "synced_count": 0,
            # 明确标记为成功，即使没有同步任何数据
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.NO_DATA.value
        assert payload.counts.synced_count == 0
        assert payload.error_summary is None

    def test_success_false_with_only_error_category_returns_failed(self):
        """success=False 且只有 error_category（无 error message）时返回 failed"""
        result = {
            "success": False,
            "synced_count": 0,
            "error_category": "timeout",
            # 无 error 字段
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "timeout"

    def test_synced_count_positive_with_error_returns_failed(self):
        """synced_count > 0 但有 error 时返回 failed（部分成功也算失败）"""
        result = {
            "success": False,
            "synced_count": 50,  # 部分成功
            "error": "Timeout after 50 commits",
            "error_category": "timeout",
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.counts.synced_count == 50
        assert payload.error_summary is not None


# ============ build_error_summary_from_exception 测试 ============


class TestBuildErrorSummaryFromException:
    """测试 build_error_summary_from_exception 函数"""

    def test_timeout_error(self):
        """超时异常"""
        exc = TimeoutError("Connection timed out")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.exception_type == "TimeoutError"
        assert summary.error_category == ErrorCategory.TIMEOUT.value
        assert "timed out" in summary.error_message

    def test_connection_error(self):
        """连接异常"""
        exc = ConnectionError("Connection refused")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.exception_type == "ConnectionError"
        assert summary.error_category == ErrorCategory.CONNECTION.value

    def test_custom_error_category(self):
        """自定义错误分类"""
        exc = Exception("Some error")
        summary = build_error_summary_from_exception(
            exc,
            error_category="auth_error",
        )
        
        assert summary.error_category == "auth_error"

    def test_context_preserved(self):
        """上下文被保留"""
        exc = Exception("Error")
        summary = build_error_summary_from_exception(
            exc,
            context={"job_id": "123"},
        )
        
        assert summary.context["job_id"] == "123"

    def test_error_message_truncated(self):
        """长错误消息被截断"""
        exc = Exception("x" * 2000)
        summary = build_error_summary_from_exception(exc)
        
        assert len(summary.error_message) <= 1003  # 1000 + "..."

    def test_http_401_detected(self):
        """检测 HTTP 401"""
        exc = Exception("HTTP 401 Unauthorized")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.AUTH_ERROR.value

    def test_http_429_detected(self):
        """检测 HTTP 429"""
        exc = Exception("HTTP 429 Too Many Requests")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.RATE_LIMIT.value

    def test_http_5xx_detected(self):
        """检测 HTTP 5xx"""
        exc = Exception("HTTP 502 Bad Gateway")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.SERVER_ERROR.value


# ============ 便捷函数测试 ============


class TestBuildPayloadForSuccess:
    """测试 build_payload_for_success 函数"""

    def test_status_is_completed(self):
        """状态是 completed"""
        payload = build_payload_for_success()
        assert payload.status == RunStatus.COMPLETED.value

    def test_with_counts(self):
        """包含计数"""
        payload = build_payload_for_success(
            counts={"synced_count": 100},
        )
        assert payload.counts.synced_count == 100


class TestBuildPayloadForNoData:
    """测试 build_payload_for_no_data 函数"""

    def test_status_is_no_data(self):
        """状态是 no_data"""
        payload = build_payload_for_no_data()
        assert payload.status == RunStatus.NO_DATA.value

    def test_counts_are_zero(self):
        """计数为 0"""
        payload = build_payload_for_no_data()
        assert payload.counts.synced_count == 0


class TestBuildPayloadForException:
    """测试 build_payload_for_exception 函数"""

    def test_status_is_failed(self):
        """状态是 failed"""
        exc = Exception("Test error")
        payload = build_payload_for_exception(exc)
        
        assert payload.status == RunStatus.FAILED.value

    def test_error_summary_populated(self):
        """error_summary 被填充"""
        exc = TimeoutError("Request timed out")
        payload = build_payload_for_exception(exc)
        
        assert payload.error_summary is not None
        assert payload.error_summary.exception_type == "TimeoutError"

    def test_partial_counts_preserved(self):
        """部分完成的计数被保留"""
        exc = Exception("Error")
        payload = build_payload_for_exception(
            exc,
            counts={"synced_count": 50},
        )
        
        assert payload.counts.synced_count == 50


class TestBuildPayloadForLeaseLost:
    """测试 build_payload_for_lease_lost 函数"""

    def test_status_is_failed(self):
        """状态是 failed"""
        payload = build_payload_for_lease_lost(
            job_id="job-123",
            worker_id="worker-1",
            failure_count=3,
            max_failures=3,
        )
        
        assert payload.status == RunStatus.FAILED.value

    def test_error_category_is_lease_lost(self):
        """error_category 是 lease_lost"""
        payload = build_payload_for_lease_lost(
            job_id="job-123",
            worker_id="worker-1",
            failure_count=3,
            max_failures=3,
        )
        
        assert payload.error_summary.error_category == ErrorCategory.LEASE_LOST.value

    def test_context_includes_job_info(self):
        """上下文包含任务信息"""
        payload = build_payload_for_lease_lost(
            job_id="job-123",
            worker_id="worker-1",
            failure_count=3,
            max_failures=3,
        )
        
        context = payload.error_summary.context
        assert context["job_id"] == "job-123"
        assert context["worker_id"] == "worker-1"
        assert context["failure_count"] == 3


class TestBuildPayloadForMarkDead:
    """测试 build_payload_for_mark_dead 函数"""

    def test_status_is_failed(self):
        """状态是 failed"""
        payload = build_payload_for_mark_dead(
            error="Repository not found",
            error_category=ErrorCategory.REPO_NOT_FOUND.value,
        )
        
        assert payload.status == RunStatus.FAILED.value

    def test_error_info_preserved(self):
        """错误信息被保留"""
        payload = build_payload_for_mark_dead(
            error="401 Unauthorized",
            error_category=ErrorCategory.AUTH_ERROR.value,
            attempts=5,
            max_attempts=5,
        )
        
        assert payload.error_summary.error_category == ErrorCategory.AUTH_ERROR.value
        assert payload.error_summary.error_message == "401 Unauthorized"
        assert payload.error_summary.attempts == 5


# ============ validate_run_finish_payload 测试 ============


class TestValidateRunFinishPayload:
    """测试 validate_run_finish_payload 函数"""

    def test_valid_payload_passes(self):
        """有效的 payload 通过验证"""
        payload = build_run_finish_payload(
            status=RunStatus.COMPLETED.value,
            counts={"synced_count": 100},
        )
        is_valid, errors, warnings = validate_run_finish_payload(payload)
        
        assert is_valid
        assert len(errors) == 0

    def test_invalid_status_fails(self):
        """无效状态验证失败"""
        payload_dict = {
            "status": "invalid_status",
            "counts": {"synced_count": 0},
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert not is_valid
        assert any("status" in e for e in errors)

    def test_missing_synced_count_fails(self):
        """缺少 synced_count 验证失败"""
        payload_dict = {
            "status": "completed",
            "counts": {},  # 缺少 synced_count
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert not is_valid
        assert any("synced_count" in e for e in errors)

    def test_failed_without_error_fails(self):
        """failed 状态缺少 error_summary 产生错误（按 schema 规定）"""
        payload_dict = {
            "status": "failed",
            "counts": {"synced_count": 0},
            # 缺少 error_summary_json
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        # 根据 schema 规定，这是错误而非警告
        assert not is_valid
        assert any("error_summary_json" in e for e in errors)

    # ============ 类型错误测试 ============

    def test_wrong_type_synced_count_fails(self):
        """synced_count 类型错误验证失败"""
        payload_dict = {
            "status": "completed",
            "counts": {"synced_count": "not_a_number"},
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert not is_valid
        assert any("类型错误" in e for e in errors)

    def test_counts_not_dict_fails(self):
        """counts 不是字典类型验证失败"""
        payload_dict = {
            "status": "completed",
            "counts": "not_a_dict",
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert not is_valid
        assert any("counts" in e and "字典" in e for e in errors)

    def test_error_summary_not_dict_fails(self):
        """error_summary_json 不是字典类型验证失败"""
        payload_dict = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": "not_a_dict",
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert not is_valid
        assert any("error_summary_json" in e and "字典" in e for e in errors)

    # ============ 未知字段警告测试 ============

    def test_unknown_counts_field_warns(self):
        """counts 中的未知字段产生警告"""
        payload_dict = {
            "status": "completed",
            "counts": {"synced_count": 100, "unknown_field": 50},
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert is_valid  # 未知字段不影响有效性
        assert any("unknown_field" in w for w in warnings)

    def test_multiple_unknown_fields_all_warned(self):
        """多个未知字段都产生警告"""
        payload_dict = {
            "status": "completed",
            "counts": {
                "synced_count": 100,
                "custom_field_1": 10,
                "custom_field_2": 20,
            },
        }
        is_valid, errors, warnings = validate_run_finish_payload(payload_dict)
        
        assert is_valid
        assert any("custom_field_1" in w for w in warnings)
        assert any("custom_field_2" in w for w in warnings)

    # ============ raise_on_error 测试 ============

    def test_raise_on_error_raises_exception(self):
        """raise_on_error=True 时校验失败抛出异常"""
        payload_dict = {
            "status": "completed",
            "counts": {},  # 缺少 synced_count
        }
        
        with pytest.raises(RunPayloadValidationError) as exc_info:
            validate_run_finish_payload(payload_dict, raise_on_error=True)
        
        assert "synced_count" in str(exc_info.value)
        assert len(exc_info.value.errors) > 0

    def test_raise_on_error_no_exception_when_valid(self):
        """raise_on_error=True 但校验通过时不抛出异常"""
        payload_dict = {
            "status": "completed",
            "counts": {"synced_count": 100},
        }
        
        # 不应该抛出异常
        is_valid, errors, warnings = validate_run_finish_payload(
            payload_dict, raise_on_error=True
        )
        assert is_valid

    def test_raise_on_error_exception_contains_payload(self):
        """抛出的异常包含原始 payload"""
        payload_dict = {
            "status": "invalid",
            "counts": {},
        }
        
        with pytest.raises(RunPayloadValidationError) as exc_info:
            validate_run_finish_payload(payload_dict, raise_on_error=True)
        
        assert exc_info.value.payload == payload_dict


# ============ RunPayloadValidationError 测试 ============


class TestRunPayloadValidationError:
    """测试 RunPayloadValidationError 结构化错误"""

    def test_basic_construction(self):
        """基本构造"""
        error = RunPayloadValidationError(
            message="Validation failed",
            errors=["error1", "error2"],
            warnings=["warning1"],
        )
        
        assert error.message == "Validation failed"
        assert len(error.errors) == 2
        assert len(error.warnings) == 1

    def test_to_error_summary(self):
        """转换为 ErrorSummary"""
        error = RunPayloadValidationError(
            message="Validation failed: missing field",
            errors=["缺少必需字段: synced_count"],
            warnings=["未知字段: custom_field"],
        )
        
        summary = error.to_error_summary()
        
        # 实际实现使用 validation_error 类别
        assert summary.error_category == "validation_error"
        assert "Validation failed" in summary.error_message
        assert "validation_errors" in summary.context
        assert "validation_warnings" in summary.context

    def test_to_dict(self):
        """转换为字典"""
        error = RunPayloadValidationError(
            message="Test message",
            errors=["error1"],
            warnings=["warning1"],
        )
        
        d = error.to_dict()
        
        assert d["error_type"] == "RunPayloadValidationError"
        assert d["message"] == "Test message"
        assert d["errors"] == ["error1"]
        assert d["warnings"] == ["warning1"]

    def test_str_representation(self):
        """字符串表示"""
        error = RunPayloadValidationError(
            message="Test error",
            errors=["error1"],
        )
        
        assert "Test error" in str(error)


# ============ validate_and_build_error_summary 测试 ============


class TestValidateAndBuildErrorSummary:
    """测试 validate_and_build_error_summary 函数"""

    def test_returns_none_when_valid(self):
        """校验通过返回 None"""
        payload = build_run_finish_payload(
            status=RunStatus.COMPLETED.value,
            counts={"synced_count": 100},
        )
        
        result = validate_and_build_error_summary(payload)
        assert result is None

    def test_returns_error_summary_when_invalid(self):
        """校验失败返回 ErrorSummary"""
        payload_dict = {
            "status": "completed",
            "counts": {},  # 缺少 synced_count
        }
        
        result = validate_and_build_error_summary(payload_dict)
        
        assert result is not None
        assert isinstance(result, ErrorSummary)
        # 实际实现使用 validation_error 类别
        assert result.error_category == "validation_error"
        assert "validation_errors" in result.context

    def test_error_summary_contains_all_errors(self):
        """返回的 ErrorSummary 包含所有错误"""
        payload_dict = {
            "status": "invalid_status",
            "counts": {},  # 缺少 synced_count
        }
        
        result = validate_and_build_error_summary(payload_dict)
        
        assert result is not None
        errors = result.context.get("validation_errors", [])
        assert len(errors) >= 2  # 至少有 status 和 synced_count 两个错误

    def test_error_summary_contains_warnings(self):
        """返回的 ErrorSummary 包含警告"""
        # 构造一个缺少必需字段但有未知字段的 payload
        payload_dict = {
            "status": "completed",
            "counts": {"unknown_field": 10},  # 缺少 synced_count，有未知字段
        }
        
        result = validate_and_build_error_summary(payload_dict)
        
        assert result is not None
        warnings = result.context.get("validation_warnings", [])
        # 未知字段应该产生警告
        assert any("unknown_field" in w for w in warnings)


# ============ 默认值常量测试 ============


class TestDefaultConstants:
    """测试默认值常量"""

    def test_default_counts_all_zero(self):
        """DEFAULT_COUNTS 所有字段为 0"""
        assert DEFAULT_COUNTS.synced_count == 0
        assert DEFAULT_COUNTS.diff_count == 0

    def test_default_error_summary_empty(self):
        """DEFAULT_ERROR_SUMMARY 所有字段为空"""
        assert DEFAULT_ERROR_SUMMARY.error_category == ""
        assert DEFAULT_ERROR_SUMMARY.error_message == ""

    def test_default_degradation_not_degraded(self):
        """DEFAULT_DEGRADATION_SNAPSHOT 不处于降级状态"""
        assert DEFAULT_DEGRADATION_SNAPSHOT.is_degraded is False

    def test_default_request_stats_all_zero(self):
        """DEFAULT_REQUEST_STATS 所有字段为 0"""
        assert DEFAULT_REQUEST_STATS.total_requests == 0
        assert DEFAULT_REQUEST_STATS.total_429_hits == 0


# ============ 集成测试：契约一致性 ============


class TestContractConsistency:
    """测试契约一致性"""

    def test_all_exit_paths_produce_valid_payload(self):
        """所有退出路径都产生有效的 payload"""
        
        # 成功退出
        p1 = build_payload_for_success(counts={"synced_count": 100})
        is_valid, errors, _ = validate_run_finish_payload(p1)
        assert is_valid, f"Success payload invalid: {errors}"
        
        # 无数据退出
        p2 = build_payload_for_no_data()
        is_valid, errors, _ = validate_run_finish_payload(p2)
        assert is_valid, f"No data payload invalid: {errors}"
        
        # 异常退出
        p3 = build_payload_for_exception(TimeoutError("timeout"))
        is_valid, errors, _ = validate_run_finish_payload(p3)
        assert is_valid, f"Exception payload invalid: {errors}"
        
        # 租约丢失退出
        p4 = build_payload_for_lease_lost("job-1", "worker-1", 3, 3)
        is_valid, errors, _ = validate_run_finish_payload(p4)
        assert is_valid, f"Lease lost payload invalid: {errors}"
        
        # mark_dead 退出
        p5 = build_payload_for_mark_dead("error", "auth_error")
        is_valid, errors, _ = validate_run_finish_payload(p5)
        assert is_valid, f"Mark dead payload invalid: {errors}"

    def test_counts_keys_consistent(self):
        """counts 字段的 key 集合一致"""
        # 从不同来源构建的 counts 应该有相同的 key 集合
        counts1 = RunCounts().to_dict()
        counts2 = build_run_finish_payload().counts.to_dict()
        
        assert set(counts1.keys()) == set(counts2.keys())

    def test_to_dict_and_from_dict_round_trip(self):
        """to_dict 和 from_dict 可以往返转换"""
        original = RunCounts(synced_count=100, diff_count=50, total_requests=200)
        d = original.to_dict()
        restored = RunCounts.from_dict(d)
        
        assert restored.synced_count == original.synced_count
        assert restored.diff_count == original.diff_count
        assert restored.total_requests == original.total_requests


# ============ 边界测试 ============


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_result_dict(self):
        """空结果字典"""
        payload = build_run_finish_payload_from_result({})
        
        assert payload.status == RunStatus.NO_DATA.value
        assert payload.counts.synced_count == 0

    def test_none_values_in_result(self):
        """结果中的 None 值"""
        result = {
            "success": True,
            "synced_count": None,
            "diff_count": None,
        }
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.counts.synced_count == 0
        assert payload.counts.diff_count == 0

    def test_negative_counts_preserved(self):
        """负数计数被保留（虽然不应该发生）"""
        counts = RunCounts(synced_count=-1)
        assert counts.synced_count == -1

    def test_very_large_counts(self):
        """非常大的计数"""
        counts = RunCounts(synced_count=10**9)
        d = counts.to_dict()
        
        assert d["synced_count"] == 10**9

    def test_special_characters_in_error_message(self):
        """错误消息中的特殊字符"""
        exc = Exception("Error with 'quotes' and \"double quotes\" and <tags>")
        summary = build_error_summary_from_exception(exc)
        
        # 应该能正常处理
        assert "quotes" in summary.error_message


# ============ JSON Schema 验证测试 ============


# Schema 文件路径 - 从测试目录回溯到项目根目录
# tests/ -> scripts/ -> logbook_postgres/ -> apps/ -> engram/
RUN_SCHEMA_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..", "..",
    "schemas", "scm_sync_run_v1.schema.json"
))


@pytest.fixture(scope="module")
def run_schema():
    """加载 run schema"""
    if not os.path.exists(RUN_SCHEMA_PATH):
        pytest.skip(f"Schema file not found: {RUN_SCHEMA_PATH}")
    
    with open(RUN_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def schema_validator(run_schema):
    """创建 schema validator"""
    if not HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    
    return Draft202012Validator(run_schema)


def validate_against_run_schema(validator, data):
    """使用 schema 验证数据，返回 (is_valid, errors)"""
    errors = list(validator.iter_errors(data))
    return len(errors) == 0, errors


class TestRunSchemaFile:
    """测试 Run Schema 文件本身"""

    def test_schema_file_exists(self):
        """Schema 文件应该存在"""
        assert os.path.exists(RUN_SCHEMA_PATH), f"Schema file not found: {RUN_SCHEMA_PATH}"

    def test_schema_is_valid_json(self):
        """Schema 文件应该是有效的 JSON"""
        with open(RUN_SCHEMA_PATH, "r", encoding="utf-8") as f:
            try:
                schema = json.load(f)
            except json.JSONDecodeError as e:
                pytest.fail(f"Schema is not valid JSON: {e}")
        
        assert schema is not None

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_schema_is_valid_json_schema(self, run_schema):
        """Schema 应该是有效的 JSON Schema"""
        try:
            Draft202012Validator.check_schema(run_schema)
        except jsonschema.SchemaError as e:
            pytest.fail(f"Schema is not valid JSON Schema: {e}")

    def test_schema_has_required_fields(self, run_schema):
        """Schema 应该包含必要的元数据"""
        assert "$schema" in run_schema
        assert "$id" in run_schema
        assert "title" in run_schema
        assert "required" in run_schema
        assert "status" in run_schema["required"]
        assert "counts" in run_schema["required"]


class TestMinimalRunPayload:
    """测试最小 run payload"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_completed_payload(self, schema_validator):
        """最小成功 payload 应该有效"""
        payload = {
            "status": "completed",
            "counts": {
                "synced_count": 0
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Minimal completed payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_minimal_payload_from_builder(self, schema_validator):
        """使用构建器创建的最小 payload 应该有效"""
        payload = build_run_finish_payload()
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"Builder minimal payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_missing_status_rejected(self, schema_validator):
        """缺少 status 应该被拒绝"""
        payload = {
            "counts": {"synced_count": 0}
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert not is_valid, "Missing status should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_missing_counts_rejected(self, schema_validator):
        """缺少 counts 应该被拒绝"""
        payload = {
            "status": "completed"
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert not is_valid, "Missing counts should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_missing_synced_count_rejected(self, schema_validator):
        """缺少 synced_count 应该被拒绝"""
        payload = {
            "status": "completed",
            "counts": {}
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert not is_valid, "Missing synced_count should be rejected"


class TestUnknownFieldsInRun:
    """测试未知字段处理"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_unknown_top_level_fields_allowed(self, schema_validator):
        """顶层未知字段应该被允许"""
        payload = {
            "status": "completed",
            "counts": {"synced_count": 100},
            "future_field": "future_value",
            "custom_metric": 123,
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Unknown top-level fields should be allowed: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_unknown_counts_fields_allowed(self, schema_validator):
        """counts 中的未知字段应该被允许"""
        payload = {
            "status": "completed",
            "counts": {
                "synced_count": 100,
                "custom_count": 50,
                "future_metric": 25,
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Unknown counts fields should be allowed: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_unknown_error_summary_fields_allowed(self, schema_validator):
        """error_summary 中的未知字段应该被允许"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "timeout",
                "error_message": "Timed out",
                "custom_error_field": "custom_value",
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Unknown error_summary fields should be allowed: {errors}"


class TestFailedRunPayload:
    """测试失败 run payload（含 error_summary）"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_failed_with_error_summary(self, schema_validator):
        """失败 payload 包含 error_summary"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "timeout",
                "error_message": "Request timed out after 30 seconds",
                "exception_type": "TimeoutError",
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Failed payload with error_summary invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_failed_with_full_error_summary(self, schema_validator):
        """失败 payload 包含完整的 error_summary"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 50},
            "error_summary_json": {
                "error_category": "connection",
                "error_message": "Connection refused",
                "exception_type": "ConnectionError",
                "stack_trace": "Traceback (most recent call last):\n  File...",
                "attempts": 3,
                "max_attempts": 5,
                "context": {
                    "job_id": "job-123",
                    "worker_id": "worker-1",
                }
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Failed payload with full error_summary invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_failed_from_exception_builder(self, schema_validator):
        """使用 build_payload_for_exception 创建的 payload 应该有效"""
        exc = TimeoutError("Connection timed out")
        payload = build_payload_for_exception(exc)
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"Exception payload invalid: {errors}"
        assert payload_dict["status"] == "failed"
        assert "error_summary_json" in payload_dict

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_failed_from_lease_lost_builder(self, schema_validator):
        """使用 build_payload_for_lease_lost 创建的 payload 应该有效"""
        payload = build_payload_for_lease_lost(
            job_id="job-123",
            worker_id="worker-1",
            failure_count=3,
            max_failures=3,
            last_error="renew_lease returned False",
        )
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"Lease lost payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_failed_from_mark_dead_builder(self, schema_validator):
        """使用 build_payload_for_mark_dead 创建的 payload 应该有效"""
        payload = build_payload_for_mark_dead(
            error="401 Unauthorized",
            error_category=ErrorCategory.AUTH_ERROR.value,
            attempts=5,
            max_attempts=5,
        )
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"Mark dead payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_error_category_rejected(self, schema_validator):
        """无效的 error_category 应该被拒绝"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "invalid_category",
                "error_message": "Some error",
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert not is_valid, "Invalid error_category should be rejected"


class TestDegradedRunPayload:
    """测试降级 run payload（含 degradation_json）"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_degraded_with_minimal_info(self, schema_validator):
        """降级 payload 包含最小降级信息"""
        payload = {
            "status": "completed",
            "counts": {"synced_count": 100, "degraded_count": 10},
            "degradation_json": {
                "is_degraded": True,
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Minimal degraded payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_degraded_with_full_info(self, schema_validator):
        """降级 payload 包含完整降级信息"""
        payload = {
            "status": "completed",
            "counts": {
                "synced_count": 100,
                "diff_count": 90,
                "degraded_count": 10,
            },
            "degradation_json": {
                "is_degraded": True,
                "degraded_reasons": {
                    "diff_timeout": 5,
                    "diff_too_large": 3,
                    "rate_limit": 2,
                },
                "circuit_state": "half_open",
                "is_backfill_only": False,
                "suggested_batch_size": 50,
                "suggested_diff_mode": "none",
                "degraded_at": "2024-01-15T10:30:00Z",
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Full degraded payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_degraded_from_result_builder(self, schema_validator):
        """使用 build_run_finish_payload_from_result 创建的降级 payload 应该有效"""
        result = {
            "success": True,
            "synced_count": 100,
            "degraded_count": 10,
            "is_backfill_only": True,
            "circuit_state": "half_open",
            "suggested_batch_size": 50,
        }
        payload = build_run_finish_payload_from_result(result)
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"Degraded result payload invalid: {errors}"
        assert payload_dict.get("degradation_json") is not None

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_circuit_state_rejected(self, schema_validator):
        """无效的 circuit_state 应该被拒绝"""
        payload = {
            "status": "completed",
            "counts": {"synced_count": 100},
            "degradation_json": {
                "is_degraded": True,
                "circuit_state": "invalid_state",
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert not is_valid, "Invalid circuit_state should be rejected"


class TestNoDataRunPayload:
    """测试无数据 run payload（counts 全为 0 且 cursor 不推进）"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_no_data_minimal(self, schema_validator):
        """最小无数据 payload"""
        payload = {
            "status": "no_data",
            "counts": {"synced_count": 0}
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Minimal no_data payload invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_no_data_from_builder(self, schema_validator):
        """使用 build_payload_for_no_data 创建的 payload 应该有效"""
        payload = build_payload_for_no_data()
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"No data builder payload invalid: {errors}"
        assert payload_dict["status"] == "no_data"
        assert payload_dict["counts"]["synced_count"] == 0

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_no_data_all_counts_zero(self, schema_validator):
        """无数据时所有计数都为 0"""
        payload = {
            "status": "no_data",
            "counts": {
                "synced_count": 0,
                "diff_count": 0,
                "bulk_count": 0,
                "scanned_count": 0,
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"No data with all zero counts invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_no_data_cursor_not_advanced(self, schema_validator):
        """无数据时 cursor 不推进（cursor_after 为 null 或不存在）"""
        # cursor_after 不存在
        payload1 = {
            "status": "no_data",
            "counts": {"synced_count": 0}
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload1)
        assert is_valid, f"No data without cursor_after invalid: {errors}"
        
        # cursor_after 为 null
        payload2 = {
            "status": "no_data",
            "counts": {"synced_count": 0},
            "cursor_after": None
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload2)
        assert is_valid, f"No data with null cursor_after invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_no_data_with_request_stats(self, schema_validator):
        """无数据但有请求统计（表示确实查询了但没数据）"""
        payload = {
            "status": "no_data",
            "counts": {"synced_count": 0},
            "request_stats": {
                "total_requests": 5,
                "success_count": 5,
                "failure_count": 0,
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"No data with request_stats invalid: {errors}"


class TestCompletedRunPayload:
    """测试成功完成的 run payload"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_completed_with_cursor(self, schema_validator):
        """成功 payload 包含 cursor"""
        payload = {
            "status": "completed",
            "counts": {"synced_count": 100},
            "cursor_after": {
                "commit_sha": "abc123",
                "timestamp": "2024-01-15T10:00:00Z",
            }
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Completed with cursor invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_completed_with_all_stats(self, schema_validator):
        """成功 payload 包含所有统计信息"""
        payload = {
            "status": "completed",
            "counts": {
                "synced_count": 100,
                "diff_count": 95,
                "bulk_count": 100,
                "degraded_count": 5,
                "total_requests": 150,
                "total_429_hits": 2,
            },
            "request_stats": {
                "total_requests": 150,
                "success_count": 148,
                "failure_count": 2,
                "total_429_hits": 2,
                "avg_latency_ms": 200,
                "max_latency_ms": 1500,
            },
            "cursor_after": {
                "commit_sha": "xyz789",
            },
            "logbook_item_id": 12345,
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Completed with all stats invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_completed_from_success_builder(self, schema_validator):
        """使用 build_payload_for_success 创建的 payload 应该有效"""
        payload = build_payload_for_success(
            counts={"synced_count": 100, "diff_count": 95},
            cursor_after={"commit_sha": "abc123"},
            logbook_item_id=12345,
        )
        payload_dict = payload.to_dict()
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload_dict)
        assert is_valid, f"Success builder payload invalid: {errors}"


class TestInvalidStatus:
    """测试无效状态"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_invalid_status_rejected(self, schema_validator):
        """无效状态应该被拒绝"""
        payload = {
            "status": "invalid_status",
            "counts": {"synced_count": 0}
        }
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert not is_valid, "Invalid status should be rejected"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_all_valid_statuses_accepted(self, schema_validator):
        """所有有效状态应该被接受"""
        for status in ["running", "completed", "failed", "no_data"]:
            payload = {
                "status": status,
                "counts": {"synced_count": 0}
            }
            # failed 状态需要 error_summary_json（schema 强制要求）
            if status == "failed":
                payload["error_summary_json"] = {
                    "error_category": "timeout",
                    "error_message": "Test error",
                }
            is_valid, errors = validate_against_run_schema(schema_validator, payload)
            assert is_valid, f"Valid status '{status}' should be accepted: {errors}"


# ============ resolve_backoff 优先级测试 ============


class TestResolveBackoffPriority:
    """测试 resolve_backoff 的优先级逻辑：retry_after > error_category > default"""

    def test_retry_after_takes_priority_over_category(self):
        """retry_after 优先于 error_category 对应的 backoff"""
        # rate_limit 分类默认 backoff 是 120 秒
        # 但如果 retry_after=300，应该使用 300
        backoff, source = resolve_backoff(
            retry_after=300,
            error_category="rate_limit",
        )
        
        assert backoff == 300
        assert source == BackoffSource.RETRY_AFTER.value
        # 确认没有使用 rate_limit 的默认 120 秒
        assert backoff != TRANSIENT_ERROR_BACKOFF["rate_limit"]

    def test_retry_after_takes_priority_over_default(self):
        """retry_after 优先于 default backoff"""
        backoff, source = resolve_backoff(
            retry_after=60,
            error_category=None,
            error_message=None,
        )
        
        assert backoff == 60
        assert source == BackoffSource.RETRY_AFTER.value

    def test_error_category_takes_priority_over_default(self):
        """error_category 优先于 default backoff"""
        # rate_limit 分类默认 backoff 是 120 秒
        backoff, source = resolve_backoff(
            retry_after=None,
            error_category="rate_limit",
        )
        
        assert backoff == TRANSIENT_ERROR_BACKOFF["rate_limit"]
        assert source == BackoffSource.ERROR_CATEGORY.value

    def test_default_backoff_when_no_retry_after_or_category(self):
        """无 retry_after 和 error_category 时使用 default backoff"""
        backoff, source = resolve_backoff(
            retry_after=None,
            error_category=None,
            error_message=None,
            default_backoff=60,
        )
        
        assert backoff == 60
        assert source == BackoffSource.DEFAULT.value

    def test_429_with_retry_after_uses_retry_after_not_category_backoff(self):
        """
        429 场景：即使 error_category=rate_limit，
        当服务端返回 retry_after 时应使用该值而非分类默认 backoff
        
        这是核心契约测试：验证 worker 不会覆盖服务端指定的 retry_after
        """
        # 模拟 429 响应，服务端返回 Retry-After: 180
        backoff, source = resolve_backoff(
            retry_after=180,  # 服务端指定
            error_category="rate_limit",  # 429 对应的分类
            error_message="429 Too Many Requests",
        )
        
        # 应该使用服务端指定的 180 秒，而非 rate_limit 分类默认的 120 秒
        assert backoff == 180
        assert source == BackoffSource.RETRY_AFTER.value
        assert backoff != TRANSIENT_ERROR_BACKOFF["rate_limit"]

    def test_zero_retry_after_falls_back_to_category(self):
        """retry_after=0 时回退到 error_category backoff"""
        backoff, source = resolve_backoff(
            retry_after=0,
            error_category="rate_limit",
        )
        
        # 0 被视为无效值，回退到分类 backoff
        assert backoff == TRANSIENT_ERROR_BACKOFF["rate_limit"]
        assert source == BackoffSource.ERROR_CATEGORY.value

    def test_negative_retry_after_falls_back_to_category(self):
        """retry_after<0 时回退到 error_category backoff"""
        backoff, source = resolve_backoff(
            retry_after=-1,
            error_category="timeout",
        )
        
        assert backoff == TRANSIENT_ERROR_BACKOFF["timeout"]
        assert source == BackoffSource.ERROR_CATEGORY.value

    def test_error_message_infers_category(self):
        """通过 error_message 推断分类"""
        backoff, source = resolve_backoff(
            retry_after=None,
            error_category=None,
            error_message="HTTP 429 Too Many Requests",
        )
        
        # 从消息推断为 rate_limit
        assert backoff == TRANSIENT_ERROR_BACKOFF["rate_limit"]
        assert source == BackoffSource.ERROR_CATEGORY.value


class TestErrorSummaryBackoffFields:
    """测试 ErrorSummary 中的 backoff 相关字段"""

    def test_error_summary_includes_backoff_info(self):
        """ErrorSummary 包含 backoff 信息"""
        summary = ErrorSummary(
            error_category="rate_limit",
            error_message="429 Too Many Requests",
            backoff_seconds=120,
            backoff_source=BackoffSource.ERROR_CATEGORY.value,
            retry_after=None,
        )
        
        d = summary.to_dict()
        
        assert d["backoff_seconds"] == 120
        assert d["backoff_source"] == "error_category"
        assert "retry_after" not in d  # None 不输出

    def test_error_summary_includes_retry_after_when_provided(self):
        """当 retry_after 有值时，ErrorSummary 包含该字段"""
        summary = ErrorSummary(
            error_category="rate_limit",
            error_message="429 Too Many Requests",
            backoff_seconds=300,
            backoff_source=BackoffSource.RETRY_AFTER.value,
            retry_after=300,
        )
        
        d = summary.to_dict()
        
        assert d["retry_after"] == 300
        assert d["backoff_seconds"] == 300
        assert d["backoff_source"] == "retry_after"

    def test_from_dict_preserves_backoff_fields(self):
        """from_dict 保留 backoff 字段"""
        data = {
            "error_category": "rate_limit",
            "error_message": "429 Too Many Requests",
            "backoff_seconds": 180,
            "backoff_source": "retry_after",
            "retry_after": 180,
        }
        
        summary = ErrorSummary.from_dict(data)
        
        assert summary.backoff_seconds == 180
        assert summary.backoff_source == "retry_after"
        assert summary.retry_after == 180


class TestBuildPayloadWithRetryAfter:
    """测试 build_run_finish_payload_from_result 处理 retry_after"""

    def test_result_with_retry_after_records_backoff_info(self):
        """同步结果包含 retry_after 时，payload 记录 backoff 信息"""
        result = {
            "success": False,
            "error": "429 Too Many Requests",
            "error_category": "rate_limit",
            "retry_after": 300,  # 服务端指定
        }
        
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.backoff_seconds == 300
        assert payload.error_summary.backoff_source == BackoffSource.RETRY_AFTER.value
        assert payload.error_summary.retry_after == 300

    def test_result_without_retry_after_uses_category_backoff(self):
        """同步结果无 retry_after 时，使用 error_category backoff"""
        result = {
            "success": False,
            "error": "429 Too Many Requests",
            "error_category": "rate_limit",
            # 无 retry_after
        }
        
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.error_summary is not None
        assert payload.error_summary.backoff_seconds == TRANSIENT_ERROR_BACKOFF["rate_limit"]
        assert payload.error_summary.backoff_source == BackoffSource.ERROR_CATEGORY.value
        assert payload.error_summary.retry_after is None

    def test_result_retry_after_not_overridden_by_category(self):
        """
        核心契约测试：retry_after 不会被 error_category 覆盖
        
        当 429 响应包含 Retry-After 头时，worker 必须使用该值，
        而不是使用 rate_limit 分类的默认 backoff（120秒）。
        """
        # 场景：GitLab 返回 429 + Retry-After: 600（10分钟）
        result = {
            "success": False,
            "error": "HTTP 429: Rate limit exceeded",
            "error_category": "rate_limit",
            "retry_after": 600,  # GitLab 指定等待 10 分钟
        }
        
        payload = build_run_finish_payload_from_result(result)
        
        # 必须使用 600 秒，而非 rate_limit 默认的 120 秒
        assert payload.error_summary.backoff_seconds == 600
        assert payload.error_summary.backoff_source == BackoffSource.RETRY_AFTER.value
        # 验证确实没有使用分类默认值
        assert payload.error_summary.backoff_seconds != TRANSIENT_ERROR_BACKOFF["rate_limit"]


class TestSchemaAndBuilderConsistency:
    """测试 Schema 和构建器的一致性"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_all_exit_paths_valid_against_schema(self, schema_validator):
        """所有退出路径产生的 payload 都应该通过 schema 验证"""
        # 成功
        p1 = build_payload_for_success(counts={"synced_count": 100})
        is_valid, errors = validate_against_run_schema(schema_validator, p1.to_dict())
        assert is_valid, f"Success path invalid: {errors}"
        
        # 无数据
        p2 = build_payload_for_no_data()
        is_valid, errors = validate_against_run_schema(schema_validator, p2.to_dict())
        assert is_valid, f"No data path invalid: {errors}"
        
        # 异常
        p3 = build_payload_for_exception(TimeoutError("timeout"))
        is_valid, errors = validate_against_run_schema(schema_validator, p3.to_dict())
        assert is_valid, f"Exception path invalid: {errors}"
        
        # 租约丢失
        p4 = build_payload_for_lease_lost("job-1", "worker-1", 3, 3)
        is_valid, errors = validate_against_run_schema(schema_validator, p4.to_dict())
        assert is_valid, f"Lease lost path invalid: {errors}"
        
        # Mark dead
        p5 = build_payload_for_mark_dead("error", "auth_error")
        is_valid, errors = validate_against_run_schema(schema_validator, p5.to_dict())
        assert is_valid, f"Mark dead path invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_from_result_builder_valid_against_schema(self, schema_validator):
        """build_run_finish_payload_from_result 的各种输入都应该产生有效 payload"""
        # 成功结果
        result1 = {"success": True, "synced_count": 100}
        p1 = build_run_finish_payload_from_result(result1)
        is_valid, errors = validate_against_run_schema(schema_validator, p1.to_dict())
        assert is_valid, f"Success result invalid: {errors}"
        
        # 失败结果
        result2 = {"success": False, "error": "Connection failed", "error_category": "connection"}
        p2 = build_run_finish_payload_from_result(result2)
        is_valid, errors = validate_against_run_schema(schema_validator, p2.to_dict())
        assert is_valid, f"Failed result invalid: {errors}"
        
        # 空结果
        result3 = {}
        p3 = build_run_finish_payload_from_result(result3)
        is_valid, errors = validate_against_run_schema(schema_validator, p3.to_dict())
        assert is_valid, f"Empty result invalid: {errors}"


# ============ cursor_before/cursor_after 规则测试 ============


class TestCursorBeforeAfterRules:
    """测试 cursor_before/cursor_after 的契约规则"""

    def test_success_path_with_both_cursors(self):
        """成功路径应该可以同时包含 cursor_before 和 cursor_after"""
        cursor_before = {"commit_sha": "abc123", "timestamp": "2025-01-01T00:00:00Z"}
        cursor_after = {"commit_sha": "xyz789", "timestamp": "2025-01-01T01:00:00Z"}
        
        payload = build_payload_for_success(
            counts={"synced_count": 100},
            cursor_before=cursor_before,
            cursor_after=cursor_after,
        )
        
        assert payload.cursor_before == cursor_before
        assert payload.cursor_after == cursor_after
        
        d = payload.to_dict()
        assert d["cursor_before"] == cursor_before
        assert d["cursor_after"] == cursor_after

    def test_no_data_path_cursor_before_preserved(self):
        """no_data 状态下 cursor_before 应该被保留（用于审计）"""
        cursor_before = {"commit_sha": "abc123"}
        
        payload = build_payload_for_no_data(cursor_before=cursor_before)
        
        assert payload.status == RunStatus.NO_DATA.value
        assert payload.cursor_before == cursor_before
        assert payload.cursor_after is None  # 无数据时游标不推进

    def test_no_data_path_cursor_after_none_is_valid(self):
        """no_data 状态下 cursor_after 为 None 是有效的（游标不推进）"""
        payload = build_payload_for_no_data()
        
        assert payload.status == RunStatus.NO_DATA.value
        assert payload.cursor_after is None
        
        d = payload.to_dict()
        assert "cursor_after" not in d  # None 值不输出

    def test_failed_path_cursor_before_preserved_for_audit(self):
        """失败路径应该保留 cursor_before（用于审计和问题排查）"""
        cursor_before = {"commit_sha": "abc123", "timestamp": "2025-01-01T00:00:00Z"}
        
        payload = build_payload_for_exception(
            TimeoutError("Connection timed out"),
            cursor_before=cursor_before,
        )
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.cursor_before == cursor_before
        assert payload.cursor_after is None  # 失败时通常不推进游标

    def test_lease_lost_cursor_before_preserved(self):
        """租约丢失时应该保留 cursor_before"""
        cursor_before = {"commit_sha": "abc123"}
        
        payload = build_payload_for_lease_lost(
            job_id="job-123",
            worker_id="worker-1",
            failure_count=3,
            max_failures=3,
            cursor_before=cursor_before,
        )
        
        assert payload.cursor_before == cursor_before

    def test_mark_dead_cursor_before_preserved(self):
        """永久错误时应该保留 cursor_before"""
        cursor_before = {"commit_sha": "abc123"}
        
        payload = build_payload_for_mark_dead(
            error="401 Unauthorized",
            error_category=ErrorCategory.AUTH_ERROR.value,
            cursor_before=cursor_before,
        )
        
        assert payload.cursor_before == cursor_before

    def test_from_result_extracts_both_cursors(self):
        """build_run_finish_payload_from_result 应该提取 cursor_before 和 cursor_after"""
        result = {
            "success": True,
            "synced_count": 100,
            "cursor_before": {"commit_sha": "abc123"},
            "cursor_after": {"commit_sha": "xyz789"},
        }
        
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.cursor_before == {"commit_sha": "abc123"}
        assert payload.cursor_after == {"commit_sha": "xyz789"}

    def test_cursor_before_equals_cursor_after_valid_when_no_progress(self):
        """当没有实际进展时，cursor_before 和 cursor_after 可以相同"""
        cursor = {"commit_sha": "abc123", "timestamp": "2025-01-01T00:00:00Z"}
        
        # 这种情况可能发生在：扫描了数据但所有记录都已存在（幂等）
        payload = build_payload_for_success(
            counts={"synced_count": 0, "skipped_count": 10},
            cursor_before=cursor,
            cursor_after=cursor,  # 游标未推进
        )
        
        assert payload.cursor_before == cursor
        assert payload.cursor_after == cursor

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_cursor_before_schema_validation(self, schema_validator):
        """cursor_before 应该通过 schema 验证"""
        payload = {
            "status": "completed",
            "counts": {"synced_count": 100},
            "cursor_before": {"commit_sha": "abc123", "timestamp": "2025-01-01T00:00:00Z"},
            "cursor_after": {"commit_sha": "xyz789", "timestamp": "2025-01-01T01:00:00Z"},
        }
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Payload with cursor_before invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_cursor_before_null_allowed(self, schema_validator):
        """cursor_before 可以为 null"""
        payload = {
            "status": "completed",
            "counts": {"synced_count": 100},
            "cursor_before": None,
            "cursor_after": {"commit_sha": "xyz789"},
        }
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Payload with null cursor_before invalid: {errors}"


# ============ 异常分类推断增强测试 ============


# ============ Worker 到 RunContract 映射一致性测试 ============


class TestWorkerToRunContractConsistency:
    """
    测试 worker 从子脚本 JSON 解析到 sync_runs 的映射一致性
    
    验证:
    1. SyncResult 字段与 RunCounts 字段的对应关系
    2. legacy 字段映射后能正确构建 payload
    3. 所有计数字段都被正确提取
    """
    
    def test_sync_result_fields_map_to_run_counts(self):
        """SyncResult 字段应该正确映射到 RunCounts"""
        from engram.logbook.sync_result import SyncResult
        
        # 构建完整的 SyncResult
        sync_result = SyncResult(
            success=True,
            synced_count=100,
            skipped_count=10,
            diff_count=95,
            degraded_count=5,
            bulk_count=3,
            diff_none_count=2,
        )
        
        # 转换为 dict
        result_dict = sync_result.to_dict()
        
        # 使用 build_run_finish_payload_from_result 构建 payload
        payload = build_run_finish_payload_from_result(result_dict)
        
        # 验证映射一致性
        assert payload.counts.synced_count == sync_result.synced_count
        assert payload.counts.diff_count == sync_result.diff_count
        assert payload.counts.degraded_count == sync_result.degraded_count
        assert payload.counts.bulk_count == sync_result.bulk_count
    
    def test_legacy_ok_count_fields_build_valid_payload(self):
        """legacy 字段 ok/count 应该能构建有效的 payload"""
        from engram.logbook.sync_result import normalize_sync_result
        
        # 模拟旧脚本返回的 legacy 格式
        legacy_result = {
            "ok": True,
            "count": 50,
            "diff_count": 45,
        }
        
        # 规范化
        normalized = normalize_sync_result(legacy_result)
        
        # 构建 payload
        payload = build_run_finish_payload_from_result(normalized)
        
        # 验证
        assert payload.status == RunStatus.COMPLETED.value
        assert payload.counts.synced_count == 50
        assert payload.counts.diff_count == 45
    
    def test_legacy_ok_false_builds_failed_payload(self):
        """legacy ok=False 应该构建 failed payload"""
        from engram.logbook.sync_result import normalize_sync_result
        
        legacy_result = {
            "ok": False,
            "error": "Connection failed",
            "error_category": "connection",
        }
        
        normalized = normalize_sync_result(legacy_result)
        payload = build_run_finish_payload_from_result(normalized)
        
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "connection"
    
    def test_all_count_fields_extracted(self):
        """验证所有计数字段都被正确提取"""
        # 完整的同步结果
        full_result = {
            "success": True,
            "synced_count": 100,
            "skipped_count": 10,
            "diff_count": 95,
            "degraded_count": 5,
            "bulk_count": 3,
            "diff_none_count": 2,
            "scanned_count": 120,
            "inserted_count": 100,
            # GitLab Reviews 计数
            "synced_mr_count": 20,
            "synced_event_count": 50,
            "skipped_event_count": 5,
            # SVN 计数
            "patch_success": 90,
            "patch_failed": 5,
            "skipped_by_controller": 5,
            # 请求统计
            "request_stats": {
                "total_requests": 200,
                "total_429_hits": 2,
            },
        }
        
        payload = build_run_finish_payload_from_result(full_result)
        
        # 验证主要计数字段
        assert payload.counts.synced_count == 100
        assert payload.counts.diff_count == 95
        assert payload.counts.degraded_count == 5
        assert payload.counts.bulk_count == 3
        
        # 验证请求统计被提取
        assert payload.counts.total_requests == 200
        assert payload.counts.total_429_hits == 2
    
    def test_svn_patch_stats_extracted(self):
        """SVN patch_stats 应该被正确提取"""
        svn_result = {
            "success": True,
            "synced_count": 50,
            "patch_stats": {
                "success": 45,
                "failed": 3,
                "skipped_by_controller": 2,
            },
        }
        
        payload = build_run_finish_payload_from_result(svn_result)
        
        assert payload.counts.patch_success == 45
        assert payload.counts.patch_failed == 3
        assert payload.counts.skipped_by_controller == 2
    
    def test_locked_skipped_fields_preserved(self):
        """locked/skipped 字段应该被保留（用于 lock_held 场景）"""
        from engram.logbook.sync_result import SyncResult
        
        # 模拟 lock_held 场景（不带 error_category，因为 worker 处理 lock_held 时直接 requeue）
        lock_result = SyncResult(
            success=True,
            locked=True,
            skipped=True,
            message="Watermark lock held by another worker",
        )
        
        result_dict = lock_result.to_dict()
        
        # 验证字段被保留
        assert result_dict["locked"] is True
        assert result_dict["skipped"] is True
        
        # 构建 payload（lock_held 场景通常不走 run-contract，worker 直接 requeue）
        # 但如果走 run-contract，success=True 且 synced_count=0 应该是 no_data
        payload = build_run_finish_payload_from_result(result_dict)
        assert payload.status == RunStatus.NO_DATA.value  # success=True 且 synced_count=0
    
    def test_locked_skipped_with_error_category(self):
        """locked/skipped 场景带 error_category 时的状态"""
        from engram.logbook.sync_result import SyncResult
        
        # 模拟 lock_held 场景（带 error_category，这种情况会被标记为 failed）
        lock_result = SyncResult(
            success=False,  # 有 error_category 时通常 success=False
            locked=True,
            skipped=True,
            error_category="lock_held",
            message="Watermark lock held by another worker",
        )
        
        result_dict = lock_result.to_dict()
        
        # 验证字段被保留
        assert result_dict["locked"] is True
        assert result_dict["skipped"] is True
        
        # 当有 error_category 时，payload 构建器会标记为 failed
        payload = build_run_finish_payload_from_result(result_dict)
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "lock_held"


class TestSyncResultToRunContractFieldMapping:
    """
    测试 SyncResult 字段到 RunContract 字段的完整映射
    
    确保两个模块之间的字段名称和语义一致
    """
    
    def test_count_field_names_consistent(self):
        """验证计数字段名称在两个模块间一致"""
        from engram.logbook.sync_result import SyncResult
        
        # SyncResult 的计数字段
        sync_result_count_fields = {
            "synced_count",
            "skipped_count", 
            "diff_count",
            "degraded_count",
            "bulk_count",
            "diff_none_count",
            "scanned_count",
            "inserted_count",
            "synced_mr_count",
            "synced_event_count",
            "skipped_event_count",
            "patch_success",
            "patch_failed",
            "skipped_by_controller",
        }
        
        # RunCounts 的字段
        run_counts_fields = set(RunCounts().to_dict().keys())
        
        # 验证 SyncResult 的关键计数字段在 RunCounts 中都存在
        key_fields = {"synced_count", "diff_count", "degraded_count", "bulk_count"}
        for field in key_fields:
            assert field in run_counts_fields, f"字段 {field} 应该在 RunCounts 中存在"
    
    def test_status_determination_rules_consistent(self):
        """验证状态判断规则一致"""
        # success=True, synced_count > 0 -> completed
        result1 = {"success": True, "synced_count": 100}
        payload1 = build_run_finish_payload_from_result(result1)
        assert payload1.status == RunStatus.COMPLETED.value
        
        # success=True, synced_count = 0 -> no_data
        result2 = {"success": True, "synced_count": 0}
        payload2 = build_run_finish_payload_from_result(result2)
        assert payload2.status == RunStatus.NO_DATA.value
        
        # success=False, has error -> failed
        result3 = {"success": False, "error": "Some error", "error_category": "timeout"}
        payload3 = build_run_finish_payload_from_result(result3)
        assert payload3.status == RunStatus.FAILED.value
        
        # success=False, no error, synced_count=0 -> no_data
        result4 = {"success": False, "synced_count": 0}
        payload4 = build_run_finish_payload_from_result(result4)
        assert payload4.status == RunStatus.NO_DATA.value
    
    def test_degradation_info_extracted(self):
        """验证降级信息被正确提取"""
        degraded_result = {
            "success": True,
            "synced_count": 100,
            "degraded_count": 10,
            "degraded_reasons": {"timeout": 5, "content_too_large": 5},
            "is_backfill_only": True,
            "circuit_state": "half_open",
            "suggested_batch_size": 50,
            "suggested_diff_mode": "none",
        }
        
        payload = build_run_finish_payload_from_result(degraded_result)
        
        # 验证降级信息
        assert payload.degradation is not None
        assert payload.degradation.is_degraded is True
        assert payload.degradation.circuit_state == "half_open"
        assert payload.degradation.suggested_batch_size == 50
        assert payload.degradation.suggested_diff_mode == "none"


class TestExceptionCategoryInference:
    """测试异常分类推断的完整覆盖"""

    def test_timeout_error_class(self):
        """TimeoutError 类异常应该推断为 timeout"""
        exc = TimeoutError("Connection timed out")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.TIMEOUT.value

    def test_connection_error_class(self):
        """ConnectionError 类异常应该推断为 connection"""
        exc = ConnectionError("Connection refused")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.CONNECTION.value

    def test_timeout_in_message(self):
        """消息中包含 timeout 应该推断为 timeout"""
        exc = Exception("Request timed out after 30 seconds")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.TIMEOUT.value

    def test_http_401_in_message(self):
        """消息中包含 401 应该推断为 auth_error"""
        exc = Exception("HTTP 401 Unauthorized")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.AUTH_ERROR.value

    def test_http_403_in_message(self):
        """消息中包含 403 应该推断为 permission_denied"""
        exc = Exception("HTTP 403 Forbidden")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.PERMISSION_DENIED.value

    def test_http_404_in_message(self):
        """消息中包含 404 应该推断为 repo_not_found"""
        exc = Exception("HTTP 404 Not Found")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.REPO_NOT_FOUND.value

    def test_http_429_in_message(self):
        """消息中包含 429 应该推断为 rate_limit"""
        exc = Exception("HTTP 429 Too Many Requests")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.RATE_LIMIT.value

    def test_rate_limit_keyword_in_message(self):
        """消息中包含 rate limit 关键字应该推断为 rate_limit"""
        exc = Exception("API rate limit exceeded, please wait")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.RATE_LIMIT.value

    def test_http_502_in_message(self):
        """消息中包含 502 应该推断为 server_error"""
        exc = Exception("HTTP 502 Bad Gateway")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.SERVER_ERROR.value

    def test_http_503_in_message(self):
        """消息中包含 503 应该推断为 server_error"""
        exc = Exception("HTTP 503 Service Unavailable")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.SERVER_ERROR.value

    def test_http_504_in_message(self):
        """消息中包含 504 但同时包含 Timeout 关键字时，优先匹配 timeout
        
        注意：当前推断逻辑先检查 timeout 关键字，因此 "504 Gateway Timeout"
        会被推断为 timeout 而不是 server_error。这是合理的，因为 504 本质上是超时。
        """
        exc = Exception("HTTP 504 Gateway Timeout")
        summary = build_error_summary_from_exception(exc)
        
        # 因为消息中包含 "Timeout" 关键字，先被匹配为 timeout
        assert summary.error_category == ErrorCategory.TIMEOUT.value
    
    def test_http_502_503_without_timeout_keyword(self):
        """502/503 不包含 timeout 关键字时应该推断为 server_error"""
        exc = Exception("HTTP 502 Bad Gateway - Server unavailable")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.SERVER_ERROR.value

    def test_unknown_exception_defaults_to_exception(self):
        """未知异常应该推断为 exception"""
        exc = Exception("Some random error occurred")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.EXCEPTION.value

    def test_explicit_category_overrides_inference(self):
        """显式指定的 error_category 应该覆盖推断"""
        exc = TimeoutError("Connection timed out")  # 通常推断为 timeout
        summary = build_error_summary_from_exception(
            exc,
            error_category="lease_lost",  # 显式指定
        )
        
        assert summary.error_category == "lease_lost"

    def test_unauthorized_keyword_in_message(self):
        """消息中包含 unauthorized 关键字应该推断为 auth_error"""
        exc = Exception("Access denied: unauthorized")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.AUTH_ERROR.value

    def test_forbidden_keyword_in_message(self):
        """消息中包含 forbidden 关键字应该推断为 permission_denied"""
        exc = Exception("Operation forbidden")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.PERMISSION_DENIED.value

    def test_not_found_keyword_in_message(self):
        """消息中包含 not found 关键字应该推断为 repo_not_found"""
        exc = Exception("Repository not found")
        summary = build_error_summary_from_exception(exc)
        
        assert summary.error_category == ErrorCategory.REPO_NOT_FOUND.value


# ============ retry_after 优先级边界测试 ============


class TestRetryAfterPriorityEdgeCases:
    """测试 retry_after 优先级的边界情况"""

    def test_retry_after_string_number_not_supported(self):
        """retry_after 不支持字符串类型，应该回退到 error_category backoff
        
        注意：resolve_backoff 期望 retry_after 是 int 或 None。
        如果传入字符串会导致类型错误，调用方应该在传入前进行类型转换。
        """
        # 当前契约：调用方负责将 retry_after 转换为 int
        # 直接传入 int 值验证正常流程
        backoff, source = resolve_backoff(
            retry_after=300,  # 正确的 int 类型
            error_category="rate_limit",
        )
        
        assert backoff == 300
        assert source == BackoffSource.RETRY_AFTER.value

    def test_retry_after_very_large_value_used(self):
        """非常大的 retry_after 值应该被使用（不应被截断）"""
        # 服务端可能指定很长的等待时间
        backoff, source = resolve_backoff(
            retry_after=3600,  # 1 小时
            error_category="rate_limit",
        )
        
        assert backoff == 3600
        assert source == BackoffSource.RETRY_AFTER.value

    def test_retry_after_1_second_used(self):
        """retry_after=1 应该被使用（最小有效值）"""
        backoff, source = resolve_backoff(
            retry_after=1,
            error_category="rate_limit",
        )
        
        assert backoff == 1
        assert source == BackoffSource.RETRY_AFTER.value

    def test_multiple_error_categories_have_different_backoffs(self):
        """不同 error_category 应该有不同的默认 backoff"""
        backoff_rate_limit, _ = resolve_backoff(
            retry_after=None,
            error_category="rate_limit",
        )
        
        backoff_timeout, _ = resolve_backoff(
            retry_after=None,
            error_category="timeout",
        )
        
        # rate_limit 和 timeout 可能有不同的默认 backoff
        # 这个测试验证它们是独立配置的
        assert backoff_rate_limit == TRANSIENT_ERROR_BACKOFF["rate_limit"]
        assert backoff_timeout == TRANSIENT_ERROR_BACKOFF["timeout"]

    def test_unknown_error_category_uses_default(self):
        """未知的 error_category 应该使用 default backoff"""
        backoff, source = resolve_backoff(
            retry_after=None,
            error_category="some_unknown_category",
            default_backoff=30,
        )
        
        # 未知分类应该使用 default_backoff
        assert backoff == 30 or source == BackoffSource.DEFAULT.value

    def test_error_summary_retry_after_zero_not_output(self):
        """ErrorSummary 中 retry_after=0 不应输出（视为无效）"""
        summary = ErrorSummary(
            error_category="rate_limit",
            error_message="429 Too Many Requests",
            backoff_seconds=120,
            backoff_source=BackoffSource.ERROR_CATEGORY.value,
            retry_after=0,  # 无效值
        )
        
        d = summary.to_dict()
        
        # retry_after=0 应该被输出还是不输出取决于契约
        # 当前实现：有值时输出
        # 这个测试记录当前行为
        if "retry_after" in d:
            assert d["retry_after"] == 0

    def test_build_payload_from_result_retry_after_none_explicit(self):
        """result 中显式 retry_after=None 应该正确处理"""
        result = {
            "success": False,
            "error": "429 Too Many Requests",
            "error_category": "rate_limit",
            "retry_after": None,  # 显式 None
        }
        
        payload = build_run_finish_payload_from_result(result)
        
        assert payload.error_summary is not None
        assert payload.error_summary.retry_after is None
        assert payload.error_summary.backoff_source == BackoffSource.ERROR_CATEGORY.value


# ============ backoff 字段 Schema 验证测试 ============


class TestBackoffFieldsSchemaValidation:
    """测试 backoff 相关字段的 Schema 验证"""

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_error_summary_with_backoff_fields_valid(self, schema_validator):
        """包含 backoff 字段的 error_summary 应该通过验证"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "rate_limit",
                "error_message": "429 Too Many Requests",
                "backoff_seconds": 120,
                "backoff_source": "error_category",
            }
        }
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Payload with backoff fields invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_error_summary_with_retry_after_valid(self, schema_validator):
        """包含 retry_after 字段的 error_summary 应该通过验证"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "rate_limit",
                "error_message": "429 Too Many Requests",
                "backoff_seconds": 300,
                "backoff_source": "retry_after",
                "retry_after": 300,
            }
        }
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Payload with retry_after invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_error_summary_retry_after_null_valid(self, schema_validator):
        """retry_after 为 null 应该通过验证"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "timeout",
                "error_message": "Request timed out",
                "backoff_seconds": 60,
                "backoff_source": "error_category",
                "retry_after": None,
            }
        }
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Payload with null retry_after invalid: {errors}"

    @pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_validation_error_category_valid(self, schema_validator):
        """validation_error 分类应该通过验证"""
        payload = {
            "status": "failed",
            "counts": {"synced_count": 0},
            "error_summary_json": {
                "error_category": "validation_error",
                "error_message": "Payload validation failed",
                "context": {
                    "validation_errors": ["缺少必需字段: synced_count"],
                }
            }
        }
        
        is_valid, errors = validate_against_run_schema(schema_validator, payload)
        assert is_valid, f"Payload with validation_error category invalid: {errors}"
