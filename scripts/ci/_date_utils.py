#!/usr/bin/env python3
"""
共享日期工具模块

提供 CI 门禁脚本使用的统一 UTC 日期获取函数。

日期语义说明（与 docs/architecture/no_root_wrappers_exceptions.md 对齐）：
- 过期日期当天 UTC 23:59:59 之后视为过期
- 即：today > expires 才算过期，today == expires 仍有效
- 所有比较使用 UTC 时区，确保跨时区一致性

用法:
    from _date_utils import utc_today

    # 获取当前 UTC 日期
    today = utc_today()

    # 判断是否过期
    is_expired = today > expires_date  # today == expires 仍有效
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def utc_today() -> date:
    """获取当前 UTC 日期

    返回 datetime.now(timezone.utc).date()，确保跨时区一致性。

    Returns:
        当前 UTC 日期（仅日期部分，无时区信息）

    示例:
        >>> today = utc_today()
        >>> isinstance(today, date)
        True
    """
    return datetime.now(timezone.utc).date()


def is_expired(expires_date: date, today: date | None = None) -> bool:
    """判断是否已过期

    过期语义：today > expires_date 才算过期（expires_date 当天仍有效）

    Args:
        expires_date: 过期日期
        today: 当前日期（用于测试注入，默认使用 utc_today()）

    Returns:
        True 如果已过期，False 如果仍有效

    示例:
        >>> from datetime import date
        >>> is_expired(date(2026, 1, 1), today=date(2026, 1, 1))  # today == expires
        False
        >>> is_expired(date(2026, 1, 1), today=date(2026, 1, 2))  # today > expires
        True
    """
    if today is None:
        today = utc_today()
    return today > expires_date


def parse_date_safe(date_str: str) -> date | None:
    """安全解析 ISO8601 日期字符串

    Args:
        date_str: YYYY-MM-DD 格式的日期字符串

    Returns:
        解析后的 date 对象，如果解析失败则返回 None

    示例:
        >>> parse_date_safe("2026-12-31")
        datetime.date(2026, 12, 31)
        >>> parse_date_safe("invalid") is None
        True
    """
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None
