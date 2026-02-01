#!/usr/bin/env python3
"""
ruff 指标聚合脚本

功能：
1. 调用 ruff check --output-format=json 获取 lint 结果
2. 按 rule code 分组统计
3. 按目录分组统计
4. 按文件 topN 统计
5. 输出 JSON 格式的指标报告

使用方式：
    python scripts/ci/ruff_metrics.py --output artifacts/ruff_metrics.json

输出格式（ruff_metrics.json）：
{
    "generated_at": "2026-02-01T12:00:00Z",
    "summary": {
        "total_violations": 143,
        "total_files": 25,
        "total_fixable": 80
    },
    "by_code": {
        "F401": {"count": 45, "fixable": 45, "description": "unused import"},
        "E501": {"count": 30, "fixable": 0, "description": "line too long"},
        ...
    },
    "by_directory": {
        "src/engram/gateway/": {"count": 30, "fixable": 20},
        "src/engram/logbook/": {"count": 80, "fixable": 50},
        ...
    },
    "top_files": [
        {"file": "src/engram/logbook/db.py", "count": 20, "fixable": 15},
        ...
    ],
    "violations_by_file": {
        "src/engram/logbook/db.py": 20,
        ...
    }
}

退出码：
    0 - 成功生成指标报告
    1 - ruff 命令执行失败或解析错误
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 默认路径
DEFAULT_OUTPUT_FILE = "artifacts/ruff_metrics.json"
DEFAULT_SCAN_PATHS = ["src/", "tests/"]

# 目录前缀分组
DIRECTORY_PREFIXES = [
    "src/engram/gateway/",
    "src/engram/logbook/",
    "src/engram/",
    "tests/gateway/",
    "tests/logbook/",
    "tests/",
    "scripts/",
]


def get_project_root() -> Path:
    """获取项目根目录"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent  # scripts/ci/ -> 项目根目录


def normalize_filepath(filepath: str, project_root: Path) -> str:
    """
    将文件路径规范化为相对于项目根目录的路径。

    Args:
        filepath: 原始文件路径（可能是绝对路径）
        project_root: 项目根目录

    Returns:
        相对路径字符串
    """
    try:
        path = Path(filepath)
        if path.is_absolute():
            return str(path.relative_to(project_root))
        return filepath
    except ValueError:
        # 路径不在项目根目录下
        return filepath


def run_ruff_check(scan_paths: list[str]) -> tuple[list[dict[str, Any]], int]:
    """
    运行 ruff check 并返回 JSON 结果。

    Args:
        scan_paths: 扫描路径列表

    Returns:
        (violations_list, return_code) 元组
    """
    cmd = ["ruff", "check", "--output-format=json"] + scan_paths

    project_root = get_project_root()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    # ruff 返回非零退出码表示有 lint 错误，但我们仍然可以解析 JSON 输出
    output = result.stdout
    if not output.strip():
        # 没有输出，可能是没有 violations 或命令失败
        if result.returncode != 0 and result.stderr:
            print(f"[WARN] ruff 命令输出: {result.stderr}", file=sys.stderr)
        return [], result.returncode

    try:
        violations = json.loads(output)
        if not isinstance(violations, list):
            violations = []
        # 规范化文件路径为相对路径
        for v in violations:
            if "filename" in v:
                v["filename"] = normalize_filepath(v["filename"], project_root)
        return violations, result.returncode
    except json.JSONDecodeError as e:
        print(f"[ERROR] 无法解析 ruff JSON 输出: {e}", file=sys.stderr)
        return [], 1


def aggregate_by_code(violations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    按 rule code 聚合统计。

    返回格式：
    {
        "F401": {"count": 10, "fixable": 10, "description": "..."},
        ...
    }
    """
    result: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "fixable": 0, "description": ""}
    )

    for v in violations:
        code = v.get("code", "UNKNOWN")
        is_fixable = v.get("fix") is not None
        message = v.get("message", "")

        result[code]["count"] += 1
        if is_fixable:
            result[code]["fixable"] += 1
        # 使用第一条消息作为描述
        if not result[code]["description"] and message:
            # 截取前 80 字符
            result[code]["description"] = message[:80]

    # 转换为普通字典并按 count 排序
    sorted_result = dict(
        sorted(result.items(), key=lambda x: x[1]["count"], reverse=True)
    )
    return sorted_result


def aggregate_by_directory(
    violations: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """
    按目录前缀聚合统计。

    返回格式：
    {
        "src/engram/gateway/": {"count": 30, "fixable": 20},
        ...
    }
    """
    result: dict[str, dict[str, int]] = {}

    for prefix in DIRECTORY_PREFIXES:
        count = 0
        fixable = 0
        for v in violations:
            filename = v.get("filename", "")
            if filename.startswith(prefix):
                count += 1
                if v.get("fix") is not None:
                    fixable += 1
        if count > 0:
            result[prefix] = {"count": count, "fixable": fixable}

    # 统计未分类的文件
    other_count = 0
    other_fixable = 0
    for v in violations:
        filename = v.get("filename", "")
        if not any(filename.startswith(p) for p in DIRECTORY_PREFIXES):
            other_count += 1
            if v.get("fix") is not None:
                other_fixable += 1

    if other_count > 0:
        result["other"] = {"count": other_count, "fixable": other_fixable}

    return result


def aggregate_by_file(violations: list[dict[str, Any]]) -> dict[str, int]:
    """
    按文件聚合统计。

    返回格式：
    {
        "src/engram/logbook/db.py": 20,
        ...
    }
    """
    result: dict[str, int] = defaultdict(int)

    for v in violations:
        filename = v.get("filename", "")
        result[filename] += 1

    # 按 count 排序
    sorted_result = dict(sorted(result.items(), key=lambda x: x[1], reverse=True))
    return sorted_result


def generate_metrics_report(
    scan_paths: list[str],
    top_n: int = 20,
    verbose: bool = False,
) -> tuple[dict[str, Any], int]:
    """
    生成完整的指标报告。

    Args:
        scan_paths: 扫描路径列表
        top_n: top N 文件数量
        verbose: 是否输出详细信息

    Returns:
        (report_dict, exit_code) 元组
    """
    # 运行 ruff
    violations, return_code = run_ruff_check(scan_paths)

    # 统计
    total_violations = len(violations)
    total_fixable = sum(1 for v in violations if v.get("fix") is not None)
    total_files = len(set(v.get("filename", "") for v in violations))

    # 按 code 聚合
    by_code = aggregate_by_code(violations)

    # 按目录聚合
    by_directory = aggregate_by_directory(violations)

    # 按文件聚合
    violations_by_file = aggregate_by_file(violations)

    # top N 文件
    top_files = [
        {"file": f, "count": c, "fixable": sum(
            1 for v in violations
            if v.get("filename") == f and v.get("fix") is not None
        )}
        for f, c in list(violations_by_file.items())[:top_n]
    ]

    # 构建报告
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_paths": scan_paths,
        "summary": {
            "total_violations": total_violations,
            "total_files": total_files,
            "total_fixable": total_fixable,
        },
        "by_code": by_code,
        "by_directory": by_directory,
        "top_files": top_files,
        "violations_by_file": violations_by_file,
    }

    if verbose:
        print("=== ruff 指标报告 ===")
        print(f"生成时间: {report['generated_at']}")
        print(f"扫描路径: {', '.join(scan_paths)}")
        print()
        print("[Summary]")
        print(f"  总违规数: {total_violations}")
        print(f"  涉及文件数: {total_files}")
        print(f"  可自动修复: {total_fixable}")
        print()
        if by_code:
            print("[按 rule code 分布（top 10）]")
            for code, stats in list(by_code.items())[:10]:
                desc = stats.get("description", "")[:40]
                print(f"  {code}: {stats['count']} ({stats['fixable']} fixable) - {desc}")
            print()
        if by_directory:
            print("[按目录分布]")
            for dir_prefix, stats in by_directory.items():
                print(f"  {dir_prefix}: {stats['count']} ({stats['fixable']} fixable)")
            print()
        if top_files:
            print(f"[按文件分布（top {min(10, len(top_files))}）]")
            for item in top_files[:10]:
                print(f"  {item['file']}: {item['count']} ({item['fixable']} fixable)")
            print()

    return report, return_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ruff 指标聚合脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path(DEFAULT_OUTPUT_FILE),
        help=f"输出 JSON 文件路径（默认: {DEFAULT_OUTPUT_FILE}）",
    )
    parser.add_argument(
        "--scan-paths",
        nargs="+",
        default=DEFAULT_SCAN_PATHS,
        help=f"扫描路径列表（默认: {' '.join(DEFAULT_SCAN_PATHS)}）",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="top N 文件数量（默认: 20）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出详细信息",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="同时输出到 stdout（JSON 格式）",
    )

    args = parser.parse_args()

    # 生成报告
    report, ruff_return_code = generate_metrics_report(
        scan_paths=args.scan_paths,
        top_n=args.top_n,
        verbose=args.verbose,
    )

    # 确保输出目录存在
    args.output.parent.mkdir(parents=True, exist_ok=True)

    # 写入文件
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    if args.verbose:
        print(f"[OK] 指标报告已写入: {args.output}")

    if args.stdout:
        print(json.dumps(report, indent=2, ensure_ascii=False))

    # 始终返回 0（指标收集不应阻断 CI）
    return 0


if __name__ == "__main__":
    sys.exit(main())
