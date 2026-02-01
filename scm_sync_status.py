#!/usr/bin/env python3
"""
scm_sync_status - SCM 同步状态摘要 CLI 入口

⚠️ DEPRECATION NOTICE:
此脚本已弃用，将在未来版本中移除。
请使用以下方式替代:
    - python -m engram.logbook.cli.scm_sync status [args]
    - engram-scm-sync status [args]
    - engram-scm-status [args]

核心实现位于: src/engram/logbook/scm_sync_status.py

用法:
    python scm_sync_status.py                  # JSON 输出
    python scm_sync_status.py --prometheus     # Prometheus 指标格式
    python scm_sync_status.py --json           # 美化 JSON 输出
"""

from __future__ import annotations

import sys
import warnings

# 导出核心模块的所有公共 API（向后兼容）
from engram.logbook.scm_sync_status import (
    _aggregate_circuit_breakers,
    _default_error_budget,
    _legacy_token_buckets,
    _load_circuit_breakers,
    _load_error_budget,
    _load_pauses,
    _load_rate_limit_buckets,
    _parse_circuit_breaker_key,
    format_prometheus_metrics,
    get_sync_summary,
)

__all__ = [
    "get_sync_summary",
    "format_prometheus_metrics",
    # 内部函数（供测试使用）
    "_load_error_budget",
    "_load_circuit_breakers",
    "_parse_circuit_breaker_key",
    "_aggregate_circuit_breakers",
    "_load_rate_limit_buckets",
    "_legacy_token_buckets",
    "_load_pauses",
    "_default_error_budget",
    # CLI
    "main",
]


# ============ CLI 入口（转发到新模块） ============


def main() -> int:
    """CLI 入口函数 - 转发到 engram.logbook.cli.scm_sync.status_main"""
    warnings.warn(
        "scm_sync_status.py 已弃用，请使用 'python -m engram.logbook.cli.scm_sync status' "
        "或 'engram-scm-status' 代替",
        DeprecationWarning,
        stacklevel=2,
    )
    # 转发到新的 CLI 入口
    from engram.logbook.cli.scm_sync import status_main
    return status_main()


if __name__ == "__main__":
    sys.exit(main())
