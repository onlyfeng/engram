#!/usr/bin/env python3
"""
ruff 指标阈值检查脚本

功能：
1. 读取 ruff_metrics.json 文件
2. 根据环境变量或命令行参数检查阈值
3. 输出 warn/fail 结果

环境变量：
- ENGRAM_RUFF_TOTAL_THRESHOLD: 总违规数阈值（默认无限制）
- ENGRAM_NOQA_TOTAL_THRESHOLD: noqa 总数阈值（默认无限制）
- ENGRAM_RUFF_FAIL_ON_THRESHOLD: 超阈值是否失败（true/false，默认 false）

使用方式：
    # 仅警告模式（默认）
    python scripts/ci/check_ruff_metrics_thresholds.py \
        --metrics-file artifacts/ruff_metrics.json

    # 失败模式
    python scripts/ci/check_ruff_metrics_thresholds.py \
        --metrics-file artifacts/ruff_metrics.json \
        --fail-on-threshold true

    # 指定阈值
    python scripts/ci/check_ruff_metrics_thresholds.py \
        --metrics-file artifacts/ruff_metrics.json \
        --total-threshold 100 \
        --noqa-threshold 50

退出码：
    0 - 检查通过或仅警告模式
    1 - 失败模式下超出阈值
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 默认配置
DEFAULT_METRICS_FILE = "artifacts/ruff_metrics.json"


@dataclass
class ThresholdConfig:
    """阈值配置"""

    total_threshold: int | None  # 总违规数阈值
    noqa_threshold: int | None  # noqa 总数阈值（预留）
    fail_on_threshold: bool  # 超阈值是否失败


@dataclass
class CheckResult:
    """检查结果"""

    success: bool
    total_violations: int
    total_threshold: int | None
    total_exceeded: bool
    noqa_count: int | None
    noqa_threshold: int | None
    noqa_exceeded: bool
    warnings: list[str]
    errors: list[str]


def load_config_from_env() -> ThresholdConfig:
    """从环境变量加载配置"""
    total_threshold = os.environ.get("ENGRAM_RUFF_TOTAL_THRESHOLD")
    noqa_threshold = os.environ.get("ENGRAM_NOQA_TOTAL_THRESHOLD")
    fail_on_threshold = os.environ.get("ENGRAM_RUFF_FAIL_ON_THRESHOLD", "false")

    return ThresholdConfig(
        total_threshold=int(total_threshold) if total_threshold else None,
        noqa_threshold=int(noqa_threshold) if noqa_threshold else None,
        fail_on_threshold=fail_on_threshold.lower() in ("true", "1", "yes"),
    )


def merge_config(
    env_config: ThresholdConfig,
    cli_total_threshold: int | None,
    cli_noqa_threshold: int | None,
    cli_fail_on_threshold: str | None,
) -> ThresholdConfig:
    """合并环境变量和 CLI 配置（CLI 优先）"""
    total_threshold = cli_total_threshold if cli_total_threshold is not None else env_config.total_threshold
    noqa_threshold = cli_noqa_threshold if cli_noqa_threshold is not None else env_config.noqa_threshold

    if cli_fail_on_threshold is not None:
        fail_on_threshold = cli_fail_on_threshold.lower() in ("true", "1", "yes")
    else:
        fail_on_threshold = env_config.fail_on_threshold

    return ThresholdConfig(
        total_threshold=total_threshold,
        noqa_threshold=noqa_threshold,
        fail_on_threshold=fail_on_threshold,
    )


def load_metrics(metrics_file: Path) -> dict[str, Any]:
    """加载 ruff_metrics.json 文件"""
    if not metrics_file.exists():
        raise FileNotFoundError(f"指标文件不存在: {metrics_file}")

    with open(metrics_file, encoding="utf-8") as f:
        return json.load(f)


def check_thresholds(
    metrics: dict[str, Any],
    config: ThresholdConfig,
) -> CheckResult:
    """检查阈值"""
    summary = metrics.get("summary", {})
    total_violations = summary.get("total_violations", 0)

    warnings: list[str] = []
    errors: list[str] = []

    # 检查总违规数阈值
    total_exceeded = False
    if config.total_threshold is not None:
        if total_violations > config.total_threshold:
            total_exceeded = True
            msg = f"总违规数 ({total_violations}) 超出阈值 ({config.total_threshold})"
            if config.fail_on_threshold:
                errors.append(msg)
            else:
                warnings.append(msg)

    # noqa 阈值检查（预留，当前 ruff_metrics.json 不包含 noqa 统计）
    # 如果将来 ruff_metrics.py 添加 noqa 统计，可在此扩展
    noqa_count = metrics.get("noqa_count")  # 预留字段
    noqa_exceeded = False
    if config.noqa_threshold is not None and noqa_count is not None:
        if noqa_count > config.noqa_threshold:
            noqa_exceeded = True
            msg = f"noqa 总数 ({noqa_count}) 超出阈值 ({config.noqa_threshold})"
            if config.fail_on_threshold:
                errors.append(msg)
            else:
                warnings.append(msg)

    success = len(errors) == 0

    return CheckResult(
        success=success,
        total_violations=total_violations,
        total_threshold=config.total_threshold,
        total_exceeded=total_exceeded,
        noqa_count=noqa_count,
        noqa_threshold=config.noqa_threshold,
        noqa_exceeded=noqa_exceeded,
        warnings=warnings,
        errors=errors,
    )


def format_output(result: CheckResult, config: ThresholdConfig, verbose: bool = False) -> str:
    """格式化输出"""
    lines: list[str] = []

    lines.append("=== ruff 指标阈值检查 ===")
    lines.append("")

    # 配置信息
    if verbose:
        lines.append("[配置]")
        lines.append(f"  总违规数阈值: {config.total_threshold or '无限制'}")
        lines.append(f"  noqa 阈值: {config.noqa_threshold or '无限制'}")
        lines.append(f"  失败模式: {config.fail_on_threshold}")
        lines.append("")

    # 检查结果
    lines.append("[检查结果]")
    lines.append(f"  总违规数: {result.total_violations}")

    if config.total_threshold is not None:
        status = "超出" if result.total_exceeded else "正常"
        lines.append(f"  阈值状态: {status} (阈值: {config.total_threshold})")

    if result.noqa_count is not None and config.noqa_threshold is not None:
        lines.append(f"  noqa 总数: {result.noqa_count}")
        status = "超出" if result.noqa_exceeded else "正常"
        lines.append(f"  noqa 阈值状态: {status} (阈值: {config.noqa_threshold})")

    lines.append("")

    # 警告和错误
    if result.warnings:
        lines.append("[WARN] 以下指标超出阈值:")
        for warn in result.warnings:
            lines.append(f"  - {warn}")
        lines.append("")

    if result.errors:
        lines.append("[FAIL] 以下指标超出阈值（失败模式）:")
        for err in result.errors:
            lines.append(f"  - {err}")
        lines.append("")

    # 总结
    if result.success:
        if result.warnings:
            lines.append("[WARN] 检查通过，但有警告")
        else:
            lines.append("[OK] 所有指标在阈值范围内")
    else:
        lines.append("[FAIL] 检查失败，存在超阈值指标")

    return "\n".join(lines)


def format_json_output(result: CheckResult, config: ThresholdConfig) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "total_violations": result.total_violations,
        "total_threshold": config.total_threshold,
        "total_exceeded": result.total_exceeded,
        "noqa_count": result.noqa_count,
        "noqa_threshold": config.noqa_threshold,
        "noqa_exceeded": result.noqa_exceeded,
        "fail_on_threshold": config.fail_on_threshold,
        "warnings": result.warnings,
        "errors": result.errors,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ruff 指标阈值检查脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--metrics-file",
        "-f",
        type=Path,
        default=Path(DEFAULT_METRICS_FILE),
        help=f"指标文件路径（默认: {DEFAULT_METRICS_FILE}）",
    )
    parser.add_argument(
        "--total-threshold",
        type=int,
        default=None,
        help="总违规数阈值（覆盖环境变量 ENGRAM_RUFF_TOTAL_THRESHOLD）",
    )
    parser.add_argument(
        "--noqa-threshold",
        type=int,
        default=None,
        help="noqa 总数阈值（覆盖环境变量 ENGRAM_NOQA_TOTAL_THRESHOLD）",
    )
    parser.add_argument(
        "--fail-on-threshold",
        type=str,
        default=None,
        help="超阈值是否失败（true/false，覆盖环境变量 ENGRAM_RUFF_FAIL_ON_THRESHOLD）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出详细信息",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出",
    )

    args = parser.parse_args()

    # 加载配置
    env_config = load_config_from_env()
    config = merge_config(
        env_config,
        args.total_threshold,
        args.noqa_threshold,
        args.fail_on_threshold,
    )

    # 加载指标
    try:
        metrics = load_metrics(args.metrics_file)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"[ERROR] 无法解析指标文件: {e}", file=sys.stderr)
        return 1

    # 检查阈值
    result = check_thresholds(metrics, config)

    # 输出结果
    if args.json:
        print(format_json_output(result, config))
    else:
        print(format_output(result, config, verbose=args.verbose))

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
