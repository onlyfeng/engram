# -*- coding: utf-8 -*-
"""
sync_run_counts.py - sync_runs.counts 字段契约定义

定义 sync_runs 表 counts JSONB 字段的统一契约：
- 最小集合 (REQUIRED): 所有脚本必须写入的字段
- 可选集合 (OPTIONAL): 特定脚本可能写入的字段
- 辅助函数: build_counts() 确保字段名、类型统一，缺省为 0

设计原则:
1. 字段名使用 snake_case，语义清晰
2. 类型统一为 int（计数类，且必须为非负）
3. 缺省值为 0，确保 JSON 解析稳定
4. 向后兼容：新增字段放入 OPTIONAL，不破坏现有消费者
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

__all__ = [
    "SyncRunCounts",
    "build_counts",
    "COUNTS_REQUIRED_FIELDS",
    "COUNTS_OPTIONAL_FIELDS",
    "COUNTS_LIMITER_FIELDS",
    "validate_counts_schema",
]

# ============ 字段定义 ============

# 最小集合：所有同步脚本必须写入的字段
COUNTS_REQUIRED_FIELDS = {
    "synced_count",     # int: 成功同步的记录数（commits/mrs/events/revisions）
}

# 可选集合：特定脚本可能写入的字段
COUNTS_OPTIONAL_FIELDS = {
    # Git/GitLab commits 相关
    "diff_count",           # int: 获取的 diff 数量
    "bulk_count",           # int: 被标记为 bulk 的 commit 数
    "degraded_count",       # int: 降级处理的 commit 数（diff 获取失败但使用 ministat）
    "diff_none_count",      # int: diff_mode=none 时完全跳过 diff fetch 的数量
    "skipped_count",        # int: 跳过的记录数（去重/过滤/已存在）
    
    # GitLab MRs 相关
    "scanned_count",        # int: 扫描的 MR 数（API 返回数）
    "inserted_count",       # int: 新插入的 MR 数
    
    # GitLab Reviews 相关
    "synced_mr_count",      # int: 同步的 MR 数
    "synced_event_count",   # int: 同步的事件数
    "skipped_event_count",  # int: 跳过的事件数（幂等）
    
    # SVN 相关
    "patch_success",        # int: patch 获取成功数
    "patch_failed",         # int: patch 获取失败数
    "skipped_by_controller",  # int: 被控制器跳过的数量
}

# Limiter 统计字段：从 request_stats 中提取的稳定字段
COUNTS_LIMITER_FIELDS = {
    "total_requests",       # int: 总请求数
    "total_429_hits",       # int: 429 限流命中次数
    "timeout_count",        # int: 超时次数
    "avg_wait_time_ms",     # int: 平均等待时间（毫秒）
}


# ============ 数据类定义 ============

@dataclass
class SyncRunCounts:
    """
    sync_runs.counts 字段的类型化表示
    
    使用 dataclass 确保字段类型和默认值一致。
    """
    # 必需字段
    synced_count: int = 0
    
    # 可选字段 - Git/GitLab commits
    diff_count: int = 0
    bulk_count: int = 0
    degraded_count: int = 0
    diff_none_count: int = 0
    skipped_count: int = 0
    
    # 可选字段 - GitLab MRs
    scanned_count: int = 0
    inserted_count: int = 0
    
    # 可选字段 - GitLab Reviews
    synced_mr_count: int = 0
    synced_event_count: int = 0
    skipped_event_count: int = 0
    
    # 可选字段 - SVN
    patch_success: int = 0
    patch_failed: int = 0
    skipped_by_controller: int = 0
    
    # Limiter 统计字段
    total_requests: int = 0
    total_429_hits: int = 0
    timeout_count: int = 0
    avg_wait_time_ms: int = 0
    
    def to_dict(self, include_zero: bool = True) -> Dict[str, int]:
        """
        转换为字典格式
        
        Args:
            include_zero: 是否包含值为 0 的字段，默认 True（保持字段完整）
            
        Returns:
            counts 字典
        """
        result = asdict(self)
        if not include_zero:
            result = {k: v for k, v in result.items() if v != 0}
        return result


# ============ 辅助函数 ============

def build_counts(
    *,
    synced_count: int = 0,
    # Git/GitLab commits
    diff_count: int = 0,
    bulk_count: int = 0,
    degraded_count: int = 0,
    diff_none_count: int = 0,
    skipped_count: int = 0,
    # GitLab MRs
    scanned_count: int = 0,
    inserted_count: int = 0,
    # GitLab Reviews
    synced_mr_count: int = 0,
    synced_event_count: int = 0,
    skipped_event_count: int = 0,
    # SVN
    patch_success: int = 0,
    patch_failed: int = 0,
    skipped_by_controller: int = 0,
    # Limiter stats
    total_requests: int = 0,
    total_429_hits: int = 0,
    timeout_count: int = 0,
    avg_wait_time_ms: int = 0,
    # 额外字段（向前兼容）
    **extra: int,
) -> Dict[str, int]:
    """
    构建 counts 字典，确保字段名、类型统一
    
    所有参数使用关键字参数，确保调用方显式指定字段名。
    未指定的字段默认为 0。
    
    Args:
        synced_count: 成功同步的记录数
        diff_count: 获取的 diff 数量
        bulk_count: 被标记为 bulk 的 commit 数
        degraded_count: 降级处理的 commit 数
        diff_none_count: diff_mode=none 时跳过 diff 获取的数量
        skipped_count: 跳过的记录数
        scanned_count: 扫描的 MR 数
        inserted_count: 新插入的 MR 数
        synced_mr_count: 同步的 MR 数
        synced_event_count: 同步的事件数
        skipped_event_count: 跳过的事件数
        patch_success: patch 获取成功数
        patch_failed: patch 获取失败数
        skipped_by_controller: 被控制器跳过的数量
        total_requests: 总请求数
        total_429_hits: 429 限流命中次数
        timeout_count: 超时次数
        avg_wait_time_ms: 平均等待时间
        **extra: 额外字段（向前兼容）
        
    Returns:
        counts 字典，所有值都是 int 类型
    
    Example:
        >>> counts = build_counts(synced_count=100, diff_count=95)
        >>> counts["synced_count"]
        100
    """
    counts = SyncRunCounts(
        synced_count=int(synced_count),
        diff_count=int(diff_count),
        bulk_count=int(bulk_count),
        degraded_count=int(degraded_count),
        diff_none_count=int(diff_none_count),
        skipped_count=int(skipped_count),
        scanned_count=int(scanned_count),
        inserted_count=int(inserted_count),
        synced_mr_count=int(synced_mr_count),
        synced_event_count=int(synced_event_count),
        skipped_event_count=int(skipped_event_count),
        patch_success=int(patch_success),
        patch_failed=int(patch_failed),
        skipped_by_controller=int(skipped_by_controller),
        total_requests=int(total_requests),
        total_429_hits=int(total_429_hits),
        timeout_count=int(timeout_count),
        avg_wait_time_ms=int(avg_wait_time_ms),
    )
    
    result = counts.to_dict()
    
    # 添加额外字段（确保类型为 int）
    for k, v in extra.items():
        result[k] = int(v) if v is not None else 0
    
    return result


def build_counts_from_result(
    result: Dict[str, Any],
    request_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """
    从同步结果字典构建 counts
    
    从 result 字典中提取已知字段，从 request_stats 中提取 limiter 统计。
    
    Args:
        result: 同步函数返回的结果字典
        request_stats: 可选的请求统计字典（从 client.stats.to_dict()）
        
    Returns:
        counts 字典
    """
    request_stats = request_stats or result.get("request_stats", {})
    
    return build_counts(
        # 从 result 提取
        synced_count=result.get("synced_count", 0),
        diff_count=result.get("diff_count", 0),
        bulk_count=result.get("bulk_count", 0),
        degraded_count=result.get("degraded_count", 0),
        diff_none_count=result.get("diff_none_count", 0),
        skipped_count=result.get("skipped_count", 0),
        scanned_count=result.get("scanned_count", 0),
        inserted_count=result.get("inserted_count", 0),
        synced_mr_count=result.get("synced_mr_count", 0),
        synced_event_count=result.get("synced_event_count", 0),
        skipped_event_count=result.get("skipped_event_count", 0),
        patch_success=result.get("patch_stats", {}).get("success", 0),
        patch_failed=result.get("patch_stats", {}).get("failed", 0),
        skipped_by_controller=result.get("patch_stats", {}).get("skipped_by_controller", 0),
        # 从 request_stats 提取
        total_requests=request_stats.get("total_requests", 0),
        total_429_hits=request_stats.get("total_429_hits", 0),
        timeout_count=request_stats.get("timeout_count", 0),
        avg_wait_time_ms=request_stats.get("avg_wait_time_ms", 0),
    )


def validate_counts_schema(counts: Dict[str, Any]) -> tuple:
    """
    验证 counts 字典是否符合契约
    
    检查:
    1. 必需字段是否存在
    2. 所有值是否为 int 且为非负
    3. 是否包含未知字段（警告但不报错）
    
    Args:
        counts: 待验证的 counts 字典
        
    Returns:
        (is_valid, errors, warnings) 元组
        - is_valid: bool, 是否有效
        - errors: List[str], 错误列表
        - warnings: List[str], 警告列表
    """
    errors = []
    warnings = []
    
    all_known_fields = COUNTS_REQUIRED_FIELDS | COUNTS_OPTIONAL_FIELDS | COUNTS_LIMITER_FIELDS
    
    # 检查必需字段
    for field in COUNTS_REQUIRED_FIELDS:
        if field not in counts:
            errors.append(f"缺少必需字段: {field}")
    
    # 检查类型
    for key, value in counts.items():
        # bool 是 int 的子类，但在 counts 中应视为类型错误
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(
                f"字段 {key} 类型错误: 期望 int（非负），实际 {type(value).__name__}"
            )
            continue
        if value < 0:
            errors.append(f"字段 {key} 值不能为负数: {value}")
    
    # 检查未知字段
    for key in counts:
        if key not in all_known_fields:
            warnings.append(f"未知字段: {key}")
    
    is_valid = len(errors) == 0
    return (is_valid, errors, warnings)
