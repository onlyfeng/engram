#!/usr/bin/env python3
"""
mypy 指标阈值检查脚本

功能：
1. 读取 mypy_metrics.json 指标文件
2. 检查是否超过配置的阈值
3. 输出 [WARN] 告警（默认不 fail）
4. 当 --fail-on-threshold=true 时，超阈值会导致脚本失败

阈值配置（可通过 GitHub Actions Variables 覆盖）：
- ENGRAM_MYPY_TOTAL_ERROR_THRESHOLD: 总错误数阈值（默认 50）
- ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD: 是否超阈值时 fail（默认 false）

使用方式：
    # 仅告警模式（默认）
    python scripts/ci/check_mypy_metrics_thresholds.py --verbose

    # 超阈值则失败
    python scripts/ci/check_mypy_metrics_thresholds.py --fail-on-threshold true

    # 指定自定义阈值
    python scripts/ci/check_mypy_metrics_thresholds.py --total-error-threshold 30

退出码：
    0 - 检查通过或仅告警模式
    1 - --fail-on-threshold=true 且超阈值
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# 默认配置
DEFAULT_METRICS_FILE = "artifacts/mypy_metrics.json"
DEFAULT_TOTAL_ERROR_THRESHOLD = 50
DEFAULT_GATEWAY_ERROR_THRESHOLD = 10
DEFAULT_LOGBOOK_ERROR_THRESHOLD = 40


def parse_bool(value: str | bool) -> bool:
    """解析布尔值字符串"""
    if isinstance(value, bool):
        return value
    return value.lower() in ("true", "1", "yes", "on")


def load_metrics(metrics_path: Path) -> dict[str, Any] | None:
    """加载指标文件"""
    if not metrics_path.exists():
        return None
    try:
        with open(metrics_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ERROR] 无法加载指标文件 {metrics_path}: {e}")
        return None


def check_thresholds(
    metrics: dict[str, Any],
    total_error_threshold: int,
    gateway_error_threshold: int,
    logbook_error_threshold: int,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """
    检查指标是否超过阈值

    返回违规列表：
    [
        {"type": "total_errors", "current": 60, "threshold": 50, "severity": "warn"},
        ...
    ]
    """
    violations: list[dict[str, Any]] = []

    # 获取汇总统计
    summary = metrics.get("summary", {})
    total_errors = summary.get("total_errors", 0)

    # 检查总错误数
    if total_errors > total_error_threshold:
        violations.append(
            {
                "type": "total_errors",
                "current": total_errors,
                "threshold": total_error_threshold,
                "severity": "warn",
                "message": f"总错误数 {total_errors} 超过阈值 {total_error_threshold}",
            }
        )

    # 检查按目录分布
    by_directory = metrics.get("by_directory", {})

    # Gateway 错误数
    gateway_errors = by_directory.get("src/engram/gateway/", {}).get("errors", 0)
    if gateway_errors > gateway_error_threshold:
        violations.append(
            {
                "type": "gateway_errors",
                "current": gateway_errors,
                "threshold": gateway_error_threshold,
                "severity": "warn",
                "message": f"Gateway 错误数 {gateway_errors} 超过阈值 {gateway_error_threshold}",
            }
        )

    # Logbook 错误数
    logbook_errors = by_directory.get("src/engram/logbook/", {}).get("errors", 0)
    if logbook_errors > logbook_error_threshold:
        violations.append(
            {
                "type": "logbook_errors",
                "current": logbook_errors,
                "threshold": logbook_error_threshold,
                "severity": "warn",
                "message": f"Logbook 错误数 {logbook_errors} 超过阈值 {logbook_error_threshold}",
            }
        )

    return violations


def print_summary(
    metrics: dict[str, Any],
    violations: list[dict[str, Any]],
    verbose: bool = False,
) -> None:
    """打印检查摘要"""
    summary = metrics.get("summary", {})
    by_directory = metrics.get("by_directory", {})

    print("=== mypy 指标阈值检查 ===")
    print()
    print("[指标摘要]")
    print(f"  总错误数: {summary.get('total_errors', 0)}")
    print(f"  总 note 数: {summary.get('total_notes', 0)}")
    print()

    if by_directory:
        print("[按目录分布]")
        for dir_path, stats in by_directory.items():
            print(f"  {dir_path}: {stats.get('errors', 0)} errors")
        print()

    if violations:
        print(f"[阈值检查结果] 发现 {len(violations)} 个告警")
        for v in violations:
            print(f"  [WARN] {v['message']}")
        print()
    else:
        print("[OK] 所有指标均在阈值范围内")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mypy 指标阈值检查脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--metrics-file",
        type=Path,
        default=Path(DEFAULT_METRICS_FILE),
        help=f"指标 JSON 文件路径（默认: {DEFAULT_METRICS_FILE}）",
    )
    parser.add_argument(
        "--total-error-threshold",
        type=int,
        default=int(
            os.environ.get("ENGRAM_MYPY_TOTAL_ERROR_THRESHOLD", DEFAULT_TOTAL_ERROR_THRESHOLD)
        ),
        help=f"总错误数阈值（默认: {DEFAULT_TOTAL_ERROR_THRESHOLD}，可通过 ENGRAM_MYPY_TOTAL_ERROR_THRESHOLD 环境变量覆盖）",
    )
    parser.add_argument(
        "--gateway-error-threshold",
        type=int,
        default=int(
            os.environ.get("ENGRAM_MYPY_GATEWAY_ERROR_THRESHOLD", DEFAULT_GATEWAY_ERROR_THRESHOLD)
        ),
        help=f"Gateway 模块错误数阈值（默认: {DEFAULT_GATEWAY_ERROR_THRESHOLD}）",
    )
    parser.add_argument(
        "--logbook-error-threshold",
        type=int,
        default=int(
            os.environ.get("ENGRAM_MYPY_LOGBOOK_ERROR_THRESHOLD", DEFAULT_LOGBOOK_ERROR_THRESHOLD)
        ),
        help=f"Logbook 模块错误数阈值（默认: {DEFAULT_LOGBOOK_ERROR_THRESHOLD}）",
    )
    parser.add_argument(
        "--fail-on-threshold",
        type=str,
        default=os.environ.get("ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD", "false"),
        help="超阈值时是否失败（默认: false，可通过 ENGRAM_MYPY_METRICS_FAIL_ON_THRESHOLD 环境变量覆盖）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出详细信息",
    )

    args = parser.parse_args()

    # 解析 fail-on-threshold
    fail_on_threshold = parse_bool(args.fail_on_threshold)

    # 加载指标
    metrics = load_metrics(args.metrics_file)
    if metrics is None:
        print(f"[WARN] 指标文件不存在或无法读取: {args.metrics_file}")
        print("[INFO] 跳过阈值检查")
        return 0

    # 检查阈值
    violations = check_thresholds(
        metrics=metrics,
        total_error_threshold=args.total_error_threshold,
        gateway_error_threshold=args.gateway_error_threshold,
        logbook_error_threshold=args.logbook_error_threshold,
        verbose=args.verbose,
    )

    # 打印摘要
    print_summary(metrics, violations, verbose=args.verbose)

    # 根据模式决定退出码
    if violations and fail_on_threshold:
        print("[FAIL] --fail-on-threshold=true 且存在阈值违规")
        return 1

    if violations:
        print("[INFO] 仅告警模式，不影响 CI 结果")

    return 0


if __name__ == "__main__":
    sys.exit(main())
