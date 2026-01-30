# -*- coding: utf-8 -*-
"""
sync_result.py - 统一的同步结果结构定义

各 scm_sync_*.py 脚本返回的统一结构化结果，供 run-contract 构建器汇总为
counts/request_stats。

计数语义:
1. synced_count: 成功写入 DB 的记录数（commits/mrs/events/revisions）
2. skipped_count: 
   - 去重过滤掉的记录数（cursor 比较或 SHA 去重）
   - 水位过滤掉的记录数
   - 跳过的事件（幂等）
3. diff_count: 成功写入 patch_blobs 的数量（无论是完整 diff 还是 ministat）
4. degraded_count: diff 获取失败但仍写入 ministat/diffstat 的数量
5. bulk_count: 被标记为 bulk 的 commit 数（变更文件过多）
6. diff_none_count: diff_mode=none 时完全跳过 diff fetch 的数量

设计原则:
1. 所有字段都有默认值（向后兼容）
2. 提供 to_dict() 供 run-contract 构建器使用
3. 支持 + 操作合并多个结果
4. 与 RunFinishPayload / build_run_finish_payload_from_result 兼容
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


__all__ = [
    "SyncResult",
    "DiffStatus",
    # 验证函数
    "validate_sync_result",
    "normalize_sync_result",
    "SyncResultValidationError",
    # 兼容性常量
    "LEGACY_FIELD_MAPPING",
]


# ============ 兼容性常量 ============

# 旧字段 → 新字段映射（向后兼容）
LEGACY_FIELD_MAPPING = {
    "ok": "success",        # 旧版使用 ok，新版使用 success
    "count": "synced_count",  # 旧版可能使用 count
}


# ============ 结构化错误 ============


class SyncResultValidationError(Exception):
    """
    SyncResult 校验失败的结构化错误
    
    当同步结果不符合契约时抛出，包含详细的错误信息。
    """
    
    def __init__(
        self,
        message: str,
        errors: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        result_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Args:
            message: 错误消息
            errors: 错误列表（导致校验失败的原因）
            warnings: 警告列表（不导致失败但需要注意）
            result_data: 原始 result 数据（用于调试）
        """
        super().__init__(message)
        self.message = message
        self.errors = errors or []
        self.warnings = warnings or []
        self.result_data = result_data
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "error_type": "SyncResultValidationError",
            "message": self.message,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class DiffStatus(str, Enum):
    """
    单条记录的 diff 状态
    
    用于细粒度追踪每条 commit/revision 的 diff 结果。
    """
    # 成功获取完整 diff
    SUCCESS = "success"
    
    # diff 获取失败，回退到 ministat/diffstat
    DEGRADED = "degraded"
    
    # diff_mode=none，完全跳过 diff 获取
    NONE = "none"
    
    # bulk commit，文件数过多不获取 diff
    BULK = "bulk"
    
    # 被去重过滤，不尝试获取 diff
    SKIPPED = "skipped"


@dataclass
class SyncResult:
    """
    统一的同步结果结构
    
    各 scm_sync_*.py 脚本的返回类型，可直接传递给
    build_run_finish_payload_from_result(result.to_dict())。
    
    Example:
        >>> result = SyncResult(success=True, synced_count=10, diff_count=8)
        >>> payload = build_run_finish_payload_from_result(result.to_dict())
    """
    
    # ============ 状态字段 ============
    
    # 同步是否成功（无不可恢复错误）
    success: bool = True
    
    # 是否还有更多数据待同步
    has_more: bool = False
    
    # ============ 通用计数字段 ============
    
    # 成功写入 DB 的记录数（commits/mrs/events/revisions）
    synced_count: int = 0
    
    # 跳过的记录数（去重/水位过滤/幂等跳过）
    skipped_count: int = 0
    
    # ============ Git/GitLab commits 计数 ============
    
    # 成功写入 patch_blobs 的数量（包含完整 diff 或降级后的 ministat）
    diff_count: int = 0
    
    # diff 获取失败但仍写入 ministat/diffstat 的数量
    degraded_count: int = 0
    
    # 被标记为 bulk 的 commit 数（变更文件过多）
    bulk_count: int = 0
    
    # diff_mode=none 时完全跳过 diff fetch 的数量
    diff_none_count: int = 0
    
    # ============ GitLab MRs 计数 ============
    
    # API 返回的 MR 数量
    scanned_count: int = 0
    
    # 新插入的 MR 数
    inserted_count: int = 0
    
    # ============ GitLab Reviews 计数 ============
    
    # 同步的 MR 数（包含 events）
    synced_mr_count: int = 0
    
    # 同步的事件数
    synced_event_count: int = 0
    
    # 跳过的事件数（幂等）
    skipped_event_count: int = 0
    
    # ============ SVN 计数 ============
    
    # patch 获取成功数
    patch_success: int = 0
    
    # patch 获取失败数
    patch_failed: int = 0
    
    # 被控制器跳过的数量（限流等）
    skipped_by_controller: int = 0
    
    # ============ 请求统计 ============
    
    # HTTP 请求统计（从 client.stats.to_dict()）
    request_stats: Dict[str, Any] = field(default_factory=dict)
    
    # ============ 降级信息 ============
    
    # 降级原因统计 {reason: count}
    degraded_reasons: Dict[str, int] = field(default_factory=dict)
    
    # 不可恢复错误列表
    unrecoverable_errors: List[str] = field(default_factory=list)
    
    # ============ 游标 ============
    
    # 同步后的游标（用于下次增量同步）
    cursor_after: Optional[Dict[str, Any]] = None
    
    # 游标是否已持久化
    cursor_persisted: bool = False
    
    # 水位是否已更新（backfill 模式）
    watermark_updated: bool = False
    
    # ============ Backfill 模式专用字段 ============
    
    # 同步模式（incremental/backfill）
    mode: Optional[str] = None
    
    # 是否为 dry-run 模式
    dry_run: bool = False
    
    # 最后处理的 revision（SVN）
    last_rev: Optional[int] = None
    
    # 最后处理的 commit SHA（GitLab）
    last_commit_sha: Optional[str] = None
    
    # 最后处理的 commit 时间戳（GitLab）
    last_commit_ts: Optional[str] = None
    
    # 摘要消息（例如 dry-run 模式的描述）
    message: Optional[str] = None
    
    # ============ 关联数据 ============
    
    # 关联的 logbook item ID
    logbook_item_id: Optional[int] = None
    
    # SVN patch_stats（向后兼容）
    patch_stats: Optional[Dict[str, Any]] = None
    
    # ============ 错误信息 ============
    
    # 错误消息（如果 success=False）
    error: Optional[str] = None
    
    # 错误分类
    error_category: Optional[str] = None
    
    # ============ 资源锁状态 ============
    
    # 是否因外部资源锁定而无法执行（如 watermark lock、并发锁等）
    # 当 locked=True 且 skipped=True 时，表示任务可安全让出并重新入队
    locked: bool = False
    
    # 是否跳过执行（与 locked 配合使用）
    skipped: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典（供 run-contract 使用）
        
        与 build_run_finish_payload_from_result 兼容。
        """
        result: Dict[str, Any] = {
            "success": self.success,
            "has_more": self.has_more,
            "synced_count": self.synced_count,
            "skipped_count": self.skipped_count,
            "diff_count": self.diff_count,
            "degraded_count": self.degraded_count,
            "bulk_count": self.bulk_count,
            "diff_none_count": self.diff_none_count,
            "scanned_count": self.scanned_count,
            "inserted_count": self.inserted_count,
            "synced_mr_count": self.synced_mr_count,
            "synced_event_count": self.synced_event_count,
            "skipped_event_count": self.skipped_event_count,
            "patch_success": self.patch_success,
            "patch_failed": self.patch_failed,
            "skipped_by_controller": self.skipped_by_controller,
            "cursor_persisted": self.cursor_persisted,
            "watermark_updated": self.watermark_updated,
        }
        
        if self.request_stats:
            result["request_stats"] = self.request_stats
        
        if self.degraded_reasons:
            result["degraded_reasons"] = self.degraded_reasons
        
        if self.unrecoverable_errors:
            result["unrecoverable_errors"] = self.unrecoverable_errors
        
        if self.cursor_after is not None:
            result["cursor_after"] = self.cursor_after
        
        if self.logbook_item_id is not None:
            result["logbook_item_id"] = self.logbook_item_id
        
        if self.patch_stats is not None:
            result["patch_stats"] = self.patch_stats
        
        if self.error is not None:
            result["error"] = self.error
        
        if self.error_category is not None:
            result["error_category"] = self.error_category
        
        # Backfill 模式专用字段
        if self.mode is not None:
            result["mode"] = self.mode
        
        if self.dry_run:
            result["dry_run"] = self.dry_run
        
        if self.last_rev is not None:
            result["last_rev"] = self.last_rev
        
        if self.last_commit_sha is not None:
            result["last_commit_sha"] = self.last_commit_sha
        
        if self.last_commit_ts is not None:
            result["last_commit_ts"] = self.last_commit_ts
        
        if self.message is not None:
            result["message"] = self.message
        
        # 资源锁状态字段（用于 lock_held 场景的重入队判断）
        if self.locked:
            result["locked"] = self.locked
        
        if self.skipped:
            result["skipped"] = self.skipped
        
        return result
    
    def __add__(self, other: "SyncResult") -> "SyncResult":
        """
        合并两个 SyncResult
        
        用于多批次同步结果聚合。
        """
        if not isinstance(other, SyncResult):
            return NotImplemented
        
        # 合并 degraded_reasons
        merged_reasons: Dict[str, int] = dict(self.degraded_reasons)
        for reason, count in other.degraded_reasons.items():
            merged_reasons[reason] = merged_reasons.get(reason, 0) + count
        
        # 合并 unrecoverable_errors
        merged_errors = list(self.unrecoverable_errors) + list(other.unrecoverable_errors)
        
        # 合并 request_stats（取非空的）
        merged_stats = self.request_stats if self.request_stats else other.request_stats
        
        # 合并 patch_stats
        merged_patch_stats: Optional[Dict[str, Any]] = None
        if self.patch_stats or other.patch_stats:
            ps1 = self.patch_stats or {}
            ps2 = other.patch_stats or {}
            merged_patch_stats = {
                "success": ps1.get("success", 0) + ps2.get("success", 0),
                "failed": ps1.get("failed", 0) + ps2.get("failed", 0),
                "skipped": ps1.get("skipped", 0) + ps2.get("skipped", 0),
                "skipped_by_controller": ps1.get("skipped_by_controller", 0) + ps2.get("skipped_by_controller", 0),
            }
        
        return SyncResult(
            success=self.success and other.success,
            has_more=other.has_more,  # 使用最新的 has_more
            synced_count=self.synced_count + other.synced_count,
            skipped_count=self.skipped_count + other.skipped_count,
            diff_count=self.diff_count + other.diff_count,
            degraded_count=self.degraded_count + other.degraded_count,
            bulk_count=self.bulk_count + other.bulk_count,
            diff_none_count=self.diff_none_count + other.diff_none_count,
            scanned_count=self.scanned_count + other.scanned_count,
            inserted_count=self.inserted_count + other.inserted_count,
            synced_mr_count=self.synced_mr_count + other.synced_mr_count,
            synced_event_count=self.synced_event_count + other.synced_event_count,
            skipped_event_count=self.skipped_event_count + other.skipped_event_count,
            patch_success=self.patch_success + other.patch_success,
            patch_failed=self.patch_failed + other.patch_failed,
            skipped_by_controller=self.skipped_by_controller + other.skipped_by_controller,
            watermark_updated=self.watermark_updated or other.watermark_updated,
            request_stats=merged_stats,
            degraded_reasons=merged_reasons,
            unrecoverable_errors=merged_errors,
            cursor_after=other.cursor_after,  # 使用最新的 cursor
            cursor_persisted=other.cursor_persisted,
            logbook_item_id=other.logbook_item_id or self.logbook_item_id,
            patch_stats=merged_patch_stats,
            error=other.error or self.error,
            error_category=other.error_category or self.error_category,
            locked=other.locked or self.locked,  # 任一被锁则标记为锁定
            skipped=other.skipped or self.skipped,
        )
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SyncResult":
        """
        从字典构建 SyncResult
        
        用于从现有返回字典迁移。
        支持旧字段名兼容：ok → success, count → synced_count
        """
        # === 兼容旧字段名 ===
        # ok → success（旧版本使用 ok 表示成功状态）
        success_value = data.get("success")
        if success_value is None:
            success_value = data.get("ok", True)  # 回退到 ok 字段
        
        # count → synced_count（旧版本可能使用 count）
        synced_count_value = data.get("synced_count")
        if synced_count_value is None:
            synced_count_value = data.get("count", 0)  # 回退到 count 字段
        
        return cls(
            success=success_value,
            has_more=data.get("has_more", False),
            synced_count=synced_count_value,
            skipped_count=data.get("skipped_count", 0),
            diff_count=data.get("diff_count", 0),
            degraded_count=data.get("degraded_count", 0),
            bulk_count=data.get("bulk_count", 0),
            diff_none_count=data.get("diff_none_count", 0),
            scanned_count=data.get("scanned_count", 0),
            inserted_count=data.get("inserted_count", 0),
            synced_mr_count=data.get("synced_mr_count", 0),
            synced_event_count=data.get("synced_event_count", 0),
            skipped_event_count=data.get("skipped_event_count", 0),
            patch_success=data.get("patch_success", 0),
            patch_failed=data.get("patch_failed", 0),
            skipped_by_controller=data.get("skipped_by_controller", 0),
            watermark_updated=data.get("watermark_updated", False),
            request_stats=data.get("request_stats", {}),
            degraded_reasons=data.get("degraded_reasons", {}),
            unrecoverable_errors=data.get("unrecoverable_errors", []),
            cursor_after=data.get("cursor_after"),
            cursor_persisted=data.get("cursor_persisted", False),
            logbook_item_id=data.get("logbook_item_id"),
            patch_stats=data.get("patch_stats"),
            error=data.get("error"),
            error_category=data.get("error_category"),
            mode=data.get("mode"),
            dry_run=data.get("dry_run", False),
            last_rev=data.get("last_rev"),
            last_commit_sha=data.get("last_commit_sha"),
            last_commit_ts=data.get("last_commit_ts"),
            message=data.get("message"),
            locked=data.get("locked", False),
            skipped=data.get("skipped", False),
        )
    
    @classmethod
    def for_no_data(cls, cursor_after: Optional[Dict[str, Any]] = None) -> "SyncResult":
        """
        创建无数据结果
        """
        return cls(
            success=True,
            has_more=False,
            synced_count=0,
            cursor_after=cursor_after,
        )
    
    @classmethod
    def for_error(
        cls,
        error: str,
        error_category: Optional[str] = None,
    ) -> "SyncResult":
        """
        创建错误结果
        """
        return cls(
            success=False,
            error=error,
            error_category=error_category,
        )
    
    def record_dedup(self, count: int) -> None:
        """
        记录去重跳过的数量
        
        Args:
            count: 被去重过滤掉的记录数
        """
        self.skipped_count += count
    
    def record_diff_success(self) -> None:
        """
        记录 diff 成功获取并写入
        """
        self.diff_count += 1
    
    def record_diff_degraded(self, reason: str) -> None:
        """
        记录 diff 获取失败但写入 ministat/diffstat
        
        Args:
            reason: 降级原因（timeout, content_too_large 等）
        """
        self.diff_count += 1  # ministat 也算写入了 patch
        self.degraded_count += 1
        self.degraded_reasons[reason] = self.degraded_reasons.get(reason, 0) + 1
    
    def record_diff_none(self) -> None:
        """
        记录完全跳过 diff（diff_mode=none）
        """
        self.diff_none_count += 1
    
    def record_bulk(self) -> None:
        """
        记录 bulk commit
        """
        self.bulk_count += 1


# ============ 验证函数 ============


def validate_sync_result(
    result: Any,
    raise_on_error: bool = False,
) -> tuple:
    """
    验证同步结果是否符合契约
    
    检查:
    1. result 必须是 dict 类型或 SyncResult 对象
    2. 必须包含 success 或 ok 字段（bool 类型）
    3. success=False 时应该有 error 或 error_category 字段
    4. 计数字段（如果存在）必须是非负整数
    
    支持兼容策略：
    - ok → success（旧版本字段映射）
    - count → synced_count（旧版本字段映射）
    
    Args:
        result: 待验证的同步结果（dict 或 SyncResult）
        raise_on_error: 是否在校验失败时抛出 SyncResultValidationError
    
    Returns:
        (is_valid, errors, warnings) 元组
        - is_valid: bool, 是否有效
        - errors: List[str], 错误列表
        - warnings: List[str], 警告列表
    
    Raises:
        SyncResultValidationError: 当 raise_on_error=True 且校验失败时抛出
    
    Example:
        >>> result = {"success": True, "synced_count": 100}
        >>> is_valid, errors, warnings = validate_sync_result(result)
        >>> is_valid
        True
        
        >>> # 旧格式兼容
        >>> result = {"ok": True, "count": 50}
        >>> is_valid, errors, warnings = validate_sync_result(result)
        >>> is_valid
        True
        >>> warnings
        ["使用了旧字段 'ok'，建议迁移到 'success'", ...]
    """
    errors: List[str] = []
    warnings: List[str] = []
    
    # 1. 类型检查
    if result is None:
        errors.append("result 不能为 None")
        if raise_on_error and errors:
            raise SyncResultValidationError(
                message=f"SyncResult 校验失败: {'; '.join(errors)}",
                errors=errors,
                warnings=warnings,
                result_data=None,
            )
        return (False, errors, warnings)
    
    # 转换为字典
    if isinstance(result, SyncResult):
        data = result.to_dict()
    elif isinstance(result, dict):
        data = result
    else:
        errors.append(f"result 类型错误: 期望 dict 或 SyncResult，实际 {type(result).__name__}")
        if raise_on_error and errors:
            raise SyncResultValidationError(
                message=f"SyncResult 校验失败: {'; '.join(errors)}",
                errors=errors,
                warnings=warnings,
                result_data=None,
            )
        return (False, errors, warnings)
    
    # 2. 检查 success/ok 字段
    has_success = "success" in data
    has_ok = "ok" in data
    
    if not has_success and not has_ok:
        errors.append("缺少必需字段: success（或旧字段 ok）")
    elif has_ok and not has_success:
        warnings.append("使用了旧字段 'ok'，建议迁移到 'success'")
    
    # 获取成功状态值
    success_value = data.get("success", data.get("ok"))
    if success_value is not None and not isinstance(success_value, bool):
        errors.append(f"success 字段类型错误: 期望 bool，实际 {type(success_value).__name__}")
    
    # 3. 检查 synced_count/count 字段（警告但不报错）
    has_synced_count = "synced_count" in data
    has_count = "count" in data
    
    if has_count and not has_synced_count:
        warnings.append("使用了旧字段 'count'，建议迁移到 'synced_count'")
    
    # 4. 检查 success=False 时的错误信息
    if success_value is False:
        has_error_info = data.get("error") or data.get("error_category")
        if not has_error_info:
            warnings.append("success=False 时建议提供 error 或 error_category 字段")
    
    # 5. 检查计数字段类型（非负整数）
    count_fields = [
        "synced_count", "skipped_count", "diff_count", "degraded_count",
        "bulk_count", "diff_none_count", "scanned_count", "inserted_count",
        "synced_mr_count", "synced_event_count", "skipped_event_count",
        "patch_success", "patch_failed", "skipped_by_controller",
        "count",  # 旧字段
    ]
    
    for field_name in count_fields:
        if field_name in data:
            value = data[field_name]
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool):
                    errors.append(
                        f"字段 {field_name} 类型错误: 期望 int（非负），实际 {type(value).__name__}"
                    )
                elif value < 0:
                    errors.append(f"字段 {field_name} 值不能为负数: {value}")
    
    # 6. 检查 has_more 类型
    if "has_more" in data:
        has_more_value = data["has_more"]
        if not isinstance(has_more_value, bool):
            errors.append(f"has_more 字段类型错误: 期望 bool，实际 {type(has_more_value).__name__}")
    
    is_valid = len(errors) == 0
    
    # 如果需要抛出异常
    if raise_on_error and not is_valid:
        raise SyncResultValidationError(
            message=f"SyncResult 校验失败: {'; '.join(errors)}",
            errors=errors,
            warnings=warnings,
            result_data=data,
        )
    
    return (is_valid, errors, warnings)


def normalize_sync_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    规范化同步结果，将旧字段映射到新字段
    
    此函数不修改原始 dict，返回新的规范化 dict。
    
    映射规则（参见 LEGACY_FIELD_MAPPING）:
    - ok → success
    - count → synced_count
    
    Args:
        result: 原始同步结果字典
    
    Returns:
        规范化后的结果字典（不修改原始 dict）
    
    Example:
        >>> result = {"ok": True, "count": 50}
        >>> normalized = normalize_sync_result(result)
        >>> normalized
        {"success": True, "synced_count": 50, "ok": True, "count": 50}
    """
    if not isinstance(result, dict):
        return result
    
    # 创建副本，避免修改原始 dict
    normalized = dict(result)
    
    # 应用旧字段映射
    for old_field, new_field in LEGACY_FIELD_MAPPING.items():
        if old_field in normalized and new_field not in normalized:
            normalized[new_field] = normalized[old_field]
    
    return normalized
