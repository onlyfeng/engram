#!/usr/bin/env python3
"""
文档快照时间新鲜度检查（仅警告，不阻断 CI）

功能：
1. 扫描指定文档中的快照时间戳
2. 检查是否超过配置的阈值（默认 14 天）
3. 输出警告信息，不影响退出码

使用方式：
    python scripts/ci/check_doc_snapshot_freshness.py
    python scripts/ci/check_doc_snapshot_freshness.py --threshold-days 7

检查的文档：
- docs/dev/mypy_type_debt_plan.md

时间戳格式要求：
- ISO 8601 格式，如 2026-02-01T06:46:37+00:00
- 文档中使用 `**快照时间**:` 标记

退出码：
- 始终返回 0（仅警告模式）
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 默认配置
DEFAULT_THRESHOLD_DAYS = 14
DOCS_TO_CHECK = [
    "docs/dev/mypy_type_debt_plan.md",
]

# 时间戳提取正则（支持多种格式）
TIMESTAMP_PATTERNS = [
    # ISO 8601 格式: 2026-02-01T06:46:37+00:00
    re.compile(r"\*\*快照时间\*\*:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})"),
    # 简单日期格式: 2026-02-01
    re.compile(r"\*\*快照时间\*\*:\s*(\d{4}-\d{2}-\d{2})"),
]


def parse_timestamp(timestamp_str: str) -> datetime | None:
    """解析时间戳字符串"""
    # 尝试 ISO 8601 格式
    try:
        return datetime.fromisoformat(timestamp_str)
    except ValueError:
        pass

    # 尝试简单日期格式
    try:
        dt = datetime.strptime(timestamp_str, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    return None


def extract_snapshot_timestamp(doc_path: Path) -> datetime | None:
    """从文档中提取快照时间戳"""
    if not doc_path.exists():
        return None

    content = doc_path.read_text(encoding="utf-8")

    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(content)
        if match:
            timestamp_str = match.group(1)
            return parse_timestamp(timestamp_str)

    return None


def check_freshness(
    doc_path: Path,
    threshold_days: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    检查文档快照是否新鲜

    返回: (is_fresh, message)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    timestamp = extract_snapshot_timestamp(doc_path)

    if timestamp is None:
        return False, f"[WARN] {doc_path}: 未找到快照时间戳（格式: **快照时间**: YYYY-MM-DDTHH:MM:SS+00:00）"

    # 确保时区感知
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    age_days = (now - timestamp).days

    if age_days > threshold_days:
        return False, (
            f"[WARN] {doc_path}: 快照时间过旧\n"
            f"       快照时间: {timestamp.isoformat()}\n"
            f"       已过去: {age_days} 天（阈值: {threshold_days} 天）\n"
            f"       建议运行: python scripts/ci/mypy_metrics.py --stdout --verbose 并更新文档"
        )

    return True, f"[OK] {doc_path}: 快照时间新鲜（{age_days} 天前）"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="文档快照时间新鲜度检查（仅警告）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--threshold-days",
        type=int,
        default=DEFAULT_THRESHOLD_DAYS,
        help=f"过旧阈值天数（默认: {DEFAULT_THRESHOLD_DAYS}）",
    )
    parser.add_argument(
        "--docs",
        nargs="+",
        default=DOCS_TO_CHECK,
        help="要检查的文档路径列表",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出详细信息",
    )

    args = parser.parse_args()

    print("=== 文档快照时间新鲜度检查 ===")
    print(f"阈值: {args.threshold_days} 天")
    print()

    warnings = []
    for doc in args.docs:
        doc_path = Path(doc)
        is_fresh, message = check_freshness(doc_path, args.threshold_days)
        print(message)
        if not is_fresh:
            warnings.append(message)

    print()
    if warnings:
        print(f"⚠️  发现 {len(warnings)} 个警告（不阻断 CI）")
        print("建议在下次迭代前更新文档快照。")
    else:
        print("✅ 所有文档快照时间均在阈值内")

    # 始终返回 0（仅警告模式）
    return 0


if __name__ == "__main__":
    sys.exit(main())
