# -*- coding: utf-8 -*-
"""
scm_sync_run_contract.py - sync_runs 记录构建器模块

统一 worker 的 run start/finish 写入点，确保任何退出路径都能写出契约一致的 JSON：
- 成功 (completed)
- 无数据 (no_data)
- 异常 (failed)
- 租约丢失 (failed - lease_lost)
- 标记为死 (failed - mark_dead)

数据结构：
- RunCounts: 同步计数统计
- ErrorSummary: 错误摘要
- DegradationSnapshot: 降级快照
- RequestStats: 请求统计
- RunFinishPayload: 完整的 run finish payload

设计原则:
1. 所有字段都有默认值（向后兼容）
2. 统一字段名使用 snake_case
3. 支持从同步函数结果字典构建
4. 支持异常路径的结构化构建
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from engram.logbook.scm_sync_errors import ErrorCategory, resolve_backoff, BackoffSource
from engram.logbook.sync_run_counts import validate_counts_schema


__all__ = [
    # 状态枚举
    "RunStatus",
    # 数据结构
    "RunCounts",
    "ErrorSummary",
    "DegradationSnapshot",
    "RequestStats",
    "RunFinishPayload",
    # 构建函数
    "build_run_finish_payload",
    "build_run_finish_payload_from_result",
    "build_error_summary_from_exception",
    # 默认值常量
    "DEFAULT_COUNTS",
    "DEFAULT_ERROR_SUMMARY",
    "DEFAULT_DEGRADATION_SNAPSHOT",
    "DEFAULT_REQUEST_STATS",
    # 验证函数
    "validate_run_finish_payload",
    "validate_and_build_error_summary",
    # 结构化错误
    "RunPayloadValidationError",
]


# ============ 结构化错误 ============


class RunPayloadValidationError(Exception):
    """
    Run finish payload 校验失败的结构化错误
    
    当 payload 不符合契约时抛出，包含详细的错误信息。
    """
    
    def __init__(
        self,
        message: str,
        errors: List[str],
        warnings: Optional[List[str]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        """
        Args:
            message: 错误消息
            errors: 错误列表（导致校验失败的原因）
            warnings: 警告列表（不导致失败但需要注意）
            payload: 原始 payload（用于调试）
        """
        super().__init__(message)
        self.message = message
        self.errors = errors
        self.warnings = warnings or []
        self.payload = payload
    
    def to_error_summary(self) -> "ErrorSummary":
        """
        转换为 ErrorSummary 对象
        
        用于写入 sync_runs 的 error_summary_json 字段。
        """
        return ErrorSummary(
            error_category="validation_error",
            error_message=self.message,
            context={
                "validation_errors": self.errors,
                "validation_warnings": self.warnings,
            },
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "error_type": "RunPayloadValidationError",
            "message": self.message,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ============ 状态枚举 ============


class RunStatus(str, Enum):
    """sync_runs 状态枚举"""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_DATA = "no_data"


# ============ 数据结构定义 ============


@dataclass
class RunCounts:
    """
    同步计数统计
    
    与 sync_run_counts.py 中的 SyncRunCounts 保持一致，
    但提供更简洁的接口用于 worker 构建。
    """
    # 必需字段
    synced_count: int = 0
    
    # Git/GitLab commits 相关
    diff_count: int = 0
    bulk_count: int = 0
    degraded_count: int = 0
    diff_none_count: int = 0
    skipped_count: int = 0
    
    # GitLab MRs 相关
    scanned_count: int = 0
    inserted_count: int = 0
    
    # GitLab Reviews 相关
    synced_mr_count: int = 0
    synced_event_count: int = 0
    skipped_event_count: int = 0
    
    # SVN 相关
    patch_success: int = 0
    patch_failed: int = 0
    skipped_by_controller: int = 0
    
    # Limiter 统计字段
    total_requests: int = 0
    total_429_hits: int = 0
    timeout_count: int = 0
    avg_wait_time_ms: int = 0
    
    def to_dict(self, include_zero: bool = True) -> Dict[str, int]:
        """转换为字典"""
        result = asdict(self)
        if not include_zero:
            result = {k: v for k, v in result.items() if v != 0}
        return result
    
    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RunCounts":
        """从字典构建"""
        if data is None:
            return cls()
        
        # 只取已知字段
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: int(v) if v is not None else 0 
                   for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class ErrorSummary:
    """
    错误摘要结构
    
    记录同步失败时的错误信息，支持结构化存储。
    """
    # 错误分类（使用 ErrorCategory 枚举值）
    error_category: str = ""
    
    # 错误消息（已脱敏）
    error_message: str = ""
    
    # 异常类型（如 TimeoutError, ConnectionError）
    exception_type: str = ""
    
    # 堆栈跟踪（截断，可选）
    stack_trace: str = ""
    
    # 重试信息
    attempts: int = 0
    max_attempts: int = 0
    
    # Backoff 信息（记录最终采用的退避策略）
    # 优先级：retry_after > error_category backoff > default backoff
    backoff_seconds: int = 0
    backoff_source: str = ""  # retry_after | error_category | default
    retry_after: Optional[int] = None  # 原始 retry_after 值（如果有）
    
    # 额外上下文
    context: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，排除空值"""
        result = {}
        if self.error_category:
            result["error_category"] = self.error_category
        if self.error_message:
            result["error_message"] = self.error_message
        if self.exception_type:
            result["exception_type"] = self.exception_type
        if self.stack_trace:
            result["stack_trace"] = self.stack_trace
        if self.attempts > 0:
            result["attempts"] = self.attempts
        if self.max_attempts > 0:
            result["max_attempts"] = self.max_attempts
        # Backoff 信息（有值时才输出）
        if self.backoff_seconds > 0:
            result["backoff_seconds"] = self.backoff_seconds
        if self.backoff_source:
            result["backoff_source"] = self.backoff_source
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        if self.context:
            result["context"] = self.context
        return result
    
    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ErrorSummary":
        """从字典构建"""
        if data is None:
            return cls()
        return cls(
            error_category=str(data.get("error_category", "")),
            error_message=str(data.get("error_message", data.get("error", ""))),
            exception_type=str(data.get("exception_type", "")),
            stack_trace=str(data.get("stack_trace", "")),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 0)),
            backoff_seconds=int(data.get("backoff_seconds", 0)),
            backoff_source=str(data.get("backoff_source", "")),
            retry_after=data.get("retry_after"),
            context=dict(data.get("context", {})),
        )


@dataclass
class DegradationSnapshot:
    """
    降级快照结构
    
    记录同步过程中的降级情况（diff 获取失败、超时等）。
    """
    # 是否处于降级模式
    is_degraded: bool = False
    
    # 降级原因统计 {reason: count}
    degraded_reasons: Dict[str, int] = field(default_factory=dict)
    
    # 熔断状态
    circuit_state: str = ""
    
    # 熔断相关参数
    is_backfill_only: bool = False
    suggested_batch_size: Optional[int] = None
    suggested_diff_mode: Optional[str] = None
    
    # 时间戳
    degraded_at: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，排除空值"""
        result = {}
        if self.is_degraded:
            result["is_degraded"] = self.is_degraded
        if self.degraded_reasons:
            result["degraded_reasons"] = self.degraded_reasons
        if self.circuit_state:
            result["circuit_state"] = self.circuit_state
        if self.is_backfill_only:
            result["is_backfill_only"] = self.is_backfill_only
        if self.suggested_batch_size is not None:
            result["suggested_batch_size"] = self.suggested_batch_size
        if self.suggested_diff_mode is not None:
            result["suggested_diff_mode"] = self.suggested_diff_mode
        if self.degraded_at:
            result["degraded_at"] = self.degraded_at
        return result
    
    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "DegradationSnapshot":
        """从字典构建"""
        if data is None:
            return cls()
        return cls(
            is_degraded=bool(data.get("is_degraded", False)),
            degraded_reasons=dict(data.get("degraded_reasons", {})),
            circuit_state=str(data.get("circuit_state", "")),
            is_backfill_only=bool(data.get("is_backfill_only", False)),
            suggested_batch_size=data.get("suggested_batch_size"),
            suggested_diff_mode=data.get("suggested_diff_mode"),
            degraded_at=data.get("degraded_at"),
        )


@dataclass
class RequestStats:
    """
    请求统计结构
    
    记录 HTTP 客户端的请求统计信息。
    """
    # 总请求数
    total_requests: int = 0
    
    # 成功/失败数
    success_count: int = 0
    failure_count: int = 0
    
    # 429 限流统计
    total_429_hits: int = 0
    
    # 超时统计
    timeout_count: int = 0
    
    # 延时统计（毫秒）
    avg_latency_ms: int = 0
    max_latency_ms: int = 0
    min_latency_ms: int = 0
    
    # 等待时间（限流退避）
    avg_wait_time_ms: int = 0
    total_wait_time_ms: int = 0
    
    def to_dict(self) -> Dict[str, int]:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RequestStats":
        """从字典构建"""
        if data is None:
            return cls()
        
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: int(v) if v is not None else 0 
                   for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class RunFinishPayload:
    """
    完整的 run finish payload
    
    包含所有需要写入 sync_runs 的信息。
    """
    # 状态
    status: str = RunStatus.COMPLETED.value
    
    # 统计
    counts: RunCounts = field(default_factory=RunCounts)
    
    # 错误摘要（仅 status=failed 时有值）
    error_summary: Optional[ErrorSummary] = None
    
    # 降级快照（可选）
    degradation: Optional[DegradationSnapshot] = None
    
    # 请求统计（可选）
    request_stats: Optional[RequestStats] = None
    
    # 游标（同步前后）
    # cursor_before: 同步开始前的游标位置（用于审计和断点续传）
    # cursor_after: 同步完成后的游标位置
    cursor_before: Optional[Dict[str, Any]] = None
    cursor_after: Optional[Dict[str, Any]] = None
    
    # 关联的 logbook item ID
    logbook_item_id: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典（用于数据库写入）
        
        Returns:
            包含 status, counts, error_summary_json, degradation_json 等字段的字典
        """
        result = {
            "status": self.status,
            "counts": self.counts.to_dict() if self.counts else {},
        }
        
        if self.error_summary:
            result["error_summary_json"] = self.error_summary.to_dict()
        
        if self.degradation:
            result["degradation_json"] = self.degradation.to_dict()
        
        if self.request_stats:
            result["request_stats"] = self.request_stats.to_dict()
        
        if self.cursor_before:
            result["cursor_before"] = self.cursor_before
        
        if self.cursor_after:
            result["cursor_after"] = self.cursor_after
        
        if self.logbook_item_id is not None:
            result["logbook_item_id"] = self.logbook_item_id
        
        return result


# ============ 默认值常量 ============


DEFAULT_COUNTS = RunCounts()
DEFAULT_ERROR_SUMMARY = ErrorSummary()
DEFAULT_DEGRADATION_SNAPSHOT = DegradationSnapshot()
DEFAULT_REQUEST_STATS = RequestStats()


# ============ 构建函数 ============


def build_run_finish_payload(
    *,
    status: str = RunStatus.COMPLETED.value,
    counts: Optional[Union[Dict[str, Any], RunCounts]] = None,
    error_summary: Optional[Union[Dict[str, Any], ErrorSummary]] = None,
    degradation: Optional[Union[Dict[str, Any], DegradationSnapshot]] = None,
    request_stats: Optional[Union[Dict[str, Any], RequestStats]] = None,
    cursor_before: Optional[Dict[str, Any]] = None,
    cursor_after: Optional[Dict[str, Any]] = None,
    logbook_item_id: Optional[int] = None,
) -> RunFinishPayload:
    """
    构建 run finish payload
    
    统一入口，确保所有字段都有正确的默认值。
    
    Args:
        status: 运行状态 (completed, failed, no_data)
        counts: 同步计数（字典或 RunCounts 对象）
        error_summary: 错误摘要（字典或 ErrorSummary 对象）
        degradation: 降级快照（字典或 DegradationSnapshot 对象）
        request_stats: 请求统计（字典或 RequestStats 对象）
        cursor_before: 同步前的游标（用于审计和断点续传）
        cursor_after: 同步后的游标
        logbook_item_id: 关联的 logbook item ID
    
    Returns:
        RunFinishPayload 对象
    
    Example:
        >>> payload = build_run_finish_payload(
        ...     status="completed",
        ...     counts={"synced_count": 100},
        ... )
        >>> payload.counts.synced_count
        100
    """
    # 转换 counts
    if counts is None:
        counts_obj = RunCounts()
    elif isinstance(counts, RunCounts):
        counts_obj = counts
    else:
        counts_obj = RunCounts.from_dict(counts)
    
    # 转换 error_summary
    error_obj = None
    if error_summary is not None:
        if isinstance(error_summary, ErrorSummary):
            error_obj = error_summary
        else:
            error_obj = ErrorSummary.from_dict(error_summary)
    
    # 转换 degradation
    degrade_obj = None
    if degradation is not None:
        if isinstance(degradation, DegradationSnapshot):
            degrade_obj = degradation
        else:
            degrade_obj = DegradationSnapshot.from_dict(degradation)
    
    # 转换 request_stats
    stats_obj = None
    if request_stats is not None:
        if isinstance(request_stats, RequestStats):
            stats_obj = request_stats
        else:
            stats_obj = RequestStats.from_dict(request_stats)
    
    return RunFinishPayload(
        status=status,
        counts=counts_obj,
        error_summary=error_obj,
        degradation=degrade_obj,
        request_stats=stats_obj,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        logbook_item_id=logbook_item_id,
    )


def build_run_finish_payload_from_result(
    result: Dict[str, Any],
    default_status: str = RunStatus.COMPLETED.value,
) -> RunFinishPayload:
    """
    从同步函数结果字典构建 run finish payload
    
    自动从 result 中提取 counts、error、degradation 等信息。
    
    Args:
        result: 同步函数返回的结果字典
        default_status: 默认状态（如果 result 中没有指定）
    
    Returns:
        RunFinishPayload 对象
    
    Example:
        >>> result = {
        ...     "success": True,
        ...     "synced_count": 100,
        ...     "diff_count": 50,
        ...     "request_stats": {"total_requests": 200},
        ... }
        >>> payload = build_run_finish_payload_from_result(result)
        >>> payload.status
        'completed'
    """
    # 确定状态（对齐 schemas/scm_sync_run_v1.schema.json 的语义）
    # 优先级：
    # 1) 显式 status（completed/failed/no_data）
    # 2) 有 error / error_category -> failed
    # 3) synced_count == 0 -> no_data（即使 success=True，也视为无数据）
    # 4) success=True -> completed
    # 5) fallback -> default_status（防御性）
    explicit_status = result.get("status")
    if explicit_status in (
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.NO_DATA.value,
    ):
        status = explicit_status
    elif explicit_status == "running":
        status = RunStatus.FAILED.value
    elif result.get("error") or result.get("error_category"):
        status = RunStatus.FAILED.value
    elif int(result.get("synced_count", 0) or 0) == 0:
        status = RunStatus.NO_DATA.value
    elif result.get("success", False):
        status = RunStatus.COMPLETED.value
    else:
        status = default_status
    
    # 提取 counts（从 result 顶层字段）
    counts_data = {
        "synced_count": result.get("synced_count", 0),
        "diff_count": result.get("diff_count", 0),
        "bulk_count": result.get("bulk_count", 0),
        "degraded_count": result.get("degraded_count", 0),
        "diff_none_count": result.get("diff_none_count", 0),
        "skipped_count": result.get("skipped_count", 0),
        "scanned_count": result.get("scanned_count", 0),
        "inserted_count": result.get("inserted_count", 0),
        "synced_mr_count": result.get("synced_mr_count", 0),
        "synced_event_count": result.get("synced_event_count", 0),
        "skipped_event_count": result.get("skipped_event_count", 0),
    }
    
    # 从 patch_stats 提取 SVN 相关计数
    patch_stats = result.get("patch_stats", {})
    if patch_stats:
        counts_data["patch_success"] = patch_stats.get("success", 0)
        counts_data["patch_failed"] = patch_stats.get("failed", 0)
        counts_data["skipped_by_controller"] = patch_stats.get("skipped_by_controller", 0)
    
    # 从 request_stats 提取 limiter 统计
    request_stats = result.get("request_stats", {})
    if request_stats:
        counts_data["total_requests"] = request_stats.get("total_requests", 0)
        counts_data["total_429_hits"] = request_stats.get("total_429_hits", 0)
        counts_data["timeout_count"] = request_stats.get("timeout_count", 0)
        counts_data["avg_wait_time_ms"] = request_stats.get("avg_wait_time_ms", 0)
    
    counts = RunCounts.from_dict(counts_data)
    
    # 提取 error_summary
    error_summary = None
    if result.get("error") or result.get("error_category"):
        error_category = result.get("error_category", "")
        error_message = str(result.get("error", ""))
        retry_after_raw = result.get("retry_after")
        
        # 计算最终 backoff（优先级：retry_after > error_category > default）
        backoff_seconds, backoff_source = resolve_backoff(
            retry_after=retry_after_raw,
            error_category=error_category,
            error_message=error_message,
        )
        
        error_summary = ErrorSummary(
            error_category=error_category,
            error_message=error_message,
            exception_type=result.get("exception_type", ""),
            backoff_seconds=backoff_seconds,
            backoff_source=backoff_source,
            retry_after=retry_after_raw,
        )
    
    # 提取 degradation
    degradation = None
    degradation_data = result.get("degradation") or result.get("degradation_json")
    if degradation_data:
        degradation = DegradationSnapshot.from_dict(degradation_data)
    elif result.get("is_backfill_only") or result.get("circuit_state"):
        degradation = DegradationSnapshot(
            is_degraded=True,
            circuit_state=result.get("circuit_state", ""),
            is_backfill_only=result.get("is_backfill_only", False),
            suggested_batch_size=result.get("suggested_batch_size"),
            suggested_diff_mode=result.get("suggested_diff_mode"),
        )
    
    # 提取 request_stats
    stats_obj = None
    if request_stats:
        stats_obj = RequestStats.from_dict(request_stats)
    
    return RunFinishPayload(
        status=status,
        counts=counts,
        error_summary=error_summary,
        degradation=degradation,
        request_stats=stats_obj,
        cursor_before=result.get("cursor_before"),
        cursor_after=result.get("cursor_after"),
        logbook_item_id=result.get("logbook_item_id"),
    )


def build_error_summary_from_exception(
    exc: Exception,
    error_category: Optional[str] = None,
    max_trace_length: int = 2000,
    context: Optional[Dict[str, Any]] = None,
) -> ErrorSummary:
    """
    从异常对象构建 ErrorSummary
    
    用于异常退出路径，自动提取异常信息并进行脱敏。
    
    Args:
        exc: 异常对象
        error_category: 可选的错误分类（如果未指定则自动推断）
        max_trace_length: 堆栈跟踪最大长度
        context: 额外上下文信息
    
    Returns:
        ErrorSummary 对象
    """
    # 获取异常类型
    exception_type = type(exc).__name__
    
    # 获取错误消息（截断）
    error_message = str(exc)
    if len(error_message) > 1000:
        error_message = error_message[:997] + "..."
    
    # 获取堆栈跟踪（截断）
    try:
        stack_trace = traceback.format_exc()
        if len(stack_trace) > max_trace_length:
            stack_trace = stack_trace[:max_trace_length - 3] + "..."
    except Exception:
        stack_trace = ""
    
    # 自动推断错误分类
    if error_category is None:
        error_category = _infer_error_category(exception_type, error_message)
    
    return ErrorSummary(
        error_category=error_category,
        error_message=error_message,
        exception_type=exception_type,
        stack_trace=stack_trace,
        context=context or {},
    )


def _infer_error_category(exception_type: str, error_message: str) -> str:
    """
    根据异常类型和消息推断错误分类
    
    Args:
        exception_type: 异常类型名
        error_message: 错误消息
    
    Returns:
        错误分类字符串
    """
    error_lower = error_message.lower()
    type_lower = exception_type.lower()
    
    # 超时
    if "timeout" in type_lower or "timeout" in error_lower or "timed out" in error_lower:
        return ErrorCategory.TIMEOUT.value
    
    # 连接错误
    if "connection" in type_lower or "connection" in error_lower:
        return ErrorCategory.CONNECTION.value
    
    # HTTP 状态码
    if "401" in error_lower or "unauthorized" in error_lower:
        return ErrorCategory.AUTH_ERROR.value
    
    if "403" in error_lower or "forbidden" in error_lower:
        return ErrorCategory.PERMISSION_DENIED.value
    
    if "404" in error_lower or "not found" in error_lower:
        return ErrorCategory.REPO_NOT_FOUND.value
    
    if "429" in error_lower or "rate limit" in error_lower or "too many requests" in error_lower:
        return ErrorCategory.RATE_LIMIT.value
    
    if "502" in error_lower or "503" in error_lower or "504" in error_lower:
        return ErrorCategory.SERVER_ERROR.value
    
    # 默认
    return ErrorCategory.EXCEPTION.value


# ============ 验证函数 ============


def validate_run_finish_payload(
    payload: Union[Dict[str, Any], RunFinishPayload],
    raise_on_error: bool = False,
) -> tuple:
    """
    验证 run finish payload 是否符合契约
    
    检查:
    1. status 是否有效
    2. counts 是否包含必需字段且类型正确（集成 validate_counts_schema）
    3. error_summary 结构是否正确（如果有）
    
    Args:
        payload: 待验证的 payload（字典或 RunFinishPayload 对象）
        raise_on_error: 是否在校验失败时抛出 RunPayloadValidationError
    
    Returns:
        (is_valid, errors, warnings) 元组
    
    Raises:
        RunPayloadValidationError: 当 raise_on_error=True 且校验失败时抛出
    """
    errors = []
    warnings = []
    
    # 转换为字典
    if isinstance(payload, RunFinishPayload):
        data = payload.to_dict()
    else:
        data = payload
    
    # 验证 status
    valid_statuses = {s.value for s in RunStatus}
    status = data.get("status", "")
    if status not in valid_statuses:
        errors.append(f"无效的 status: {status}，有效值: {valid_statuses}")
    
    # 验证 counts - 使用 validate_counts_schema 进行完整校验
    counts = data.get("counts", {})
    if not isinstance(counts, dict):
        errors.append(f"counts 必须是字典类型，实际: {type(counts).__name__}")
    else:
        # 调用 validate_counts_schema 进行详细校验
        counts_valid, counts_errors, counts_warnings = validate_counts_schema(counts)
        if not counts_valid:
            errors.extend(counts_errors)
        warnings.extend(counts_warnings)
    
    # 验证 error_summary（如果 status=failed）
    # 根据 schema 规定，failed 状态必须包含 error_summary_json
    if status == RunStatus.FAILED.value:
        error_summary = data.get("error_summary_json")
        if error_summary is None:
            errors.append("status=failed 时必须提供 error_summary_json")
        elif not isinstance(error_summary, dict):
            errors.append(f"error_summary_json 必须是字典类型，实际: {type(error_summary).__name__}")
    
    is_valid = len(errors) == 0
    
    # 如果需要抛出异常
    if raise_on_error and not is_valid:
        raise RunPayloadValidationError(
            message=f"Payload 校验失败: {'; '.join(errors)}",
            errors=errors,
            warnings=warnings,
            payload=data,
        )
    
    return (is_valid, errors, warnings)


def validate_and_build_error_summary(
    payload: Union[Dict[str, Any], RunFinishPayload],
) -> Optional["ErrorSummary"]:
    """
    验证 payload 并在失败时返回 ErrorSummary
    
    用于 worker 写入 sync_runs 时的校验：
    - 校验通过返回 None
    - 校验失败返回包含错误信息的 ErrorSummary
    
    Args:
        payload: 待验证的 payload
    
    Returns:
        校验通过返回 None，失败返回 ErrorSummary
    
    Example:
        >>> payload = build_run_finish_payload(...)
        >>> error_summary = validate_and_build_error_summary(payload)
        >>> if error_summary:
        ...     # 校验失败，将 error_summary 写入 run.error_summary_json
        ...     run.error_summary_json = error_summary.to_dict()
    """
    is_valid, errors, warnings = validate_run_finish_payload(payload)
    
    if is_valid:
        return None
    
    return ErrorSummary(
        error_category="validation_error",
        error_message=f"Payload 校验失败: {'; '.join(errors)}",
        context={
            "validation_errors": errors,
            "validation_warnings": warnings,
        },
    )


# ============ 便捷函数：特定退出路径 ============


def build_payload_for_success(
    counts: Optional[Union[Dict[str, Any], RunCounts]] = None,
    cursor_before: Optional[Dict[str, Any]] = None,
    cursor_after: Optional[Dict[str, Any]] = None,
    logbook_item_id: Optional[int] = None,
    request_stats: Optional[Dict[str, Any]] = None,
) -> RunFinishPayload:
    """
    构建成功退出的 payload
    
    快捷方法，等同于 build_run_finish_payload(status="completed", ...)
    """
    return build_run_finish_payload(
        status=RunStatus.COMPLETED.value,
        counts=counts,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        logbook_item_id=logbook_item_id,
        request_stats=request_stats,
    )


def build_payload_for_no_data(
    cursor_before: Optional[Dict[str, Any]] = None,
    cursor_after: Optional[Dict[str, Any]] = None,
    request_stats: Optional[Dict[str, Any]] = None,
) -> RunFinishPayload:
    """
    构建无数据退出的 payload
    
    快捷方法，等同于 build_run_finish_payload(status="no_data", ...)
    
    注意：no_data 状态下，cursor_after 通常为 None（游标不推进），
    但 cursor_before 应该记录同步起点用于审计。
    """
    return build_run_finish_payload(
        status=RunStatus.NO_DATA.value,
        counts=RunCounts(),
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        request_stats=request_stats,
    )


def build_payload_for_exception(
    exc: Exception,
    error_category: Optional[str] = None,
    counts: Optional[Union[Dict[str, Any], RunCounts]] = None,
    context: Optional[Dict[str, Any]] = None,
    cursor_before: Optional[Dict[str, Any]] = None,
) -> RunFinishPayload:
    """
    构建异常退出的 payload
    
    快捷方法，自动从异常对象构建 error_summary。
    
    Args:
        exc: 异常对象
        error_category: 可选的错误分类
        counts: 同步计数（可能部分完成）
        context: 额外上下文
        cursor_before: 同步前的游标（用于审计）
    
    Returns:
        RunFinishPayload 对象
    """
    error_summary = build_error_summary_from_exception(
        exc=exc,
        error_category=error_category,
        context=context,
    )
    
    return build_run_finish_payload(
        status=RunStatus.FAILED.value,
        counts=counts,
        error_summary=error_summary,
        cursor_before=cursor_before,
    )


def build_payload_for_lease_lost(
    job_id: str,
    worker_id: str,
    failure_count: int,
    max_failures: int,
    last_error: Optional[str] = None,
    counts: Optional[Union[Dict[str, Any], RunCounts]] = None,
    cursor_before: Optional[Dict[str, Any]] = None,
) -> RunFinishPayload:
    """
    构建租约丢失退出的 payload
    
    快捷方法，用于 HeartbeatManager.should_abort 时。
    
    Args:
        job_id: 任务 ID
        worker_id: Worker ID
        failure_count: 续租失败次数
        max_failures: 最大续租失败次数
        last_error: 最后一次续租失败的错误
        counts: 同步计数（可能部分完成）
        cursor_before: 同步前的游标（用于审计）
    
    Returns:
        RunFinishPayload 对象
    """
    error_summary = ErrorSummary(
        error_category=ErrorCategory.LEASE_LOST.value,
        error_message=f"Lease lost after {failure_count} consecutive renewal failures. "
                     f"Last error: {last_error or 'renew_lease returned False'}",
        context={
            "job_id": job_id,
            "worker_id": worker_id,
            "failure_count": failure_count,
            "max_failures": max_failures,
        },
    )
    
    return build_run_finish_payload(
        status=RunStatus.FAILED.value,
        counts=counts,
        error_summary=error_summary,
        cursor_before=cursor_before,
    )


def build_payload_for_mark_dead(
    error: str,
    error_category: str,
    attempts: int = 0,
    max_attempts: int = 0,
    counts: Optional[Union[Dict[str, Any], RunCounts]] = None,
    cursor_before: Optional[Dict[str, Any]] = None,
) -> RunFinishPayload:
    """
    构建标记为死的 payload
    
    快捷方法，用于永久性错误（auth_error, repo_not_found 等）。
    
    Args:
        error: 错误消息
        error_category: 错误分类
        attempts: 尝试次数
        max_attempts: 最大尝试次数
        counts: 同步计数（可能部分完成）
        cursor_before: 同步前的游标（用于审计）
    
    Returns:
        RunFinishPayload 对象
    """
    error_summary = ErrorSummary(
        error_category=error_category,
        error_message=error,
        attempts=attempts,
        max_attempts=max_attempts,
    )
    
    return build_run_finish_payload(
        status=RunStatus.FAILED.value,
        counts=counts,
        error_summary=error_summary,
        cursor_before=cursor_before,
    )
