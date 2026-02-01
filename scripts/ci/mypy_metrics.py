#!/usr/bin/env python3
"""
mypy 指标聚合脚本

功能：
1. 读取 baseline 文件，聚合错误统计
2. 按目录前缀分组统计错误
3. 按 error-code 分组统计
4. 读取 pyproject.toml 的 strict-island 配置并输出覆盖清单
5. 输出 JSON 格式的指标报告

使用方式：
    python scripts/ci/mypy_metrics.py --output artifacts/mypy_metrics.json

输出格式（mypy_metrics.json）：
{
    "generated_at": "2026-02-01T12:00:00Z",
    "baseline_file": "scripts/ci/mypy_baseline.txt",
    "summary": {
        "total_errors": 143,
        "total_notes": 25,
        "total_lines": 168
    },
    "by_directory": {
        "src/engram/gateway/": {"errors": 30, "notes": 10},
        "src/engram/logbook/": {"errors": 113, "notes": 15}
    },
    "by_error_code": {
        "Incompatible types in": 45,
        "Returning Any from": 30,
        ...
    },
    "top_files": [
        {"file": "src/engram/logbook/db.py", "errors": 20},
        {"file": "src/engram/gateway/app.py", "errors": 15},
        ...
    ],
    "errors_by_file": {
        "src/engram/logbook/db.py": 20,
        "src/engram/gateway/app.py": 15,
        ...
    },
    "strict_island": {
        "paths": ["src/engram/gateway/di.py", ...],
        "count": 5,
        "modules": ["src.engram.gateway.di", ...],
        "coverage_summary": "5 paths configured"
    }
}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 默认路径
DEFAULT_BASELINE_FILE = "scripts/ci/mypy_baseline.txt"
DEFAULT_PYPROJECT_FILE = "pyproject.toml"
DEFAULT_OUTPUT_FILE = "artifacts/mypy_metrics.json"

# 目录前缀分组
DIRECTORY_PREFIXES = [
    "src/engram/gateway/",
    "src/engram/logbook/",
    "src/engram/",
    "tests/",
    "scripts/",
]

# error-code 提取正则（匹配行尾的 [error-code] 或错误类型描述）
ERROR_CODE_PATTERN = re.compile(r"\[([a-z\-]+)\]$")
# 错误类型描述提取（从 error: 后提取主要错误描述）
ERROR_TYPE_PATTERN = re.compile(r"error:\s+(.+?)(?:\s+\[|$)")


def load_pyproject_config(pyproject_path: Path) -> dict[str, Any]:
    """加载 pyproject.toml 配置"""
    if not pyproject_path.exists():
        return {}

    content = pyproject_path.read_text(encoding="utf-8")

    # 尝试使用 tomllib (Python 3.11+) 或 tomli
    try:
        import tomllib

        return tomllib.loads(content)
    except ImportError:
        pass

    try:
        import tomli

        return tomli.loads(content)
    except ImportError:
        pass

    # 手动解析 strict_island_paths（简化版本）
    result: dict[str, Any] = {}
    in_engram_mypy = False
    paths: list[str] = []

    for line in content.split("\n"):
        line = line.strip()
        if line == "[tool.engram.mypy]":
            in_engram_mypy = True
            continue
        if in_engram_mypy:
            if line.startswith("["):
                break
            if line.startswith("strict_island_paths"):
                # 开始解析路径列表
                continue
            if line.startswith('"') and line.endswith('",'):
                paths.append(line.strip('",'))
            elif line.startswith('"') and line.endswith('"'):
                paths.append(line.strip('"'))
            elif line == "]":
                break

    if paths:
        result["tool"] = {"engram": {"mypy": {"strict_island_paths": paths}}}

    return result


def parse_baseline(baseline_path: Path) -> dict[str, Any]:
    """
    解析 baseline 文件

    返回：
    {
        "total_errors": int,
        "total_notes": int,
        "total_lines": int,
        "errors_by_file": {filepath: count},
        "notes_by_file": {filepath: count},
        "error_codes": {code: count},
        "error_types": {type_description: count},
        "raw_errors": [{"file": str, "line": int|None, "type": "error"|"note", "message": str}]
    }
    """
    if not baseline_path.exists():
        return {
            "total_errors": 0,
            "total_notes": 0,
            "total_lines": 0,
            "errors_by_file": {},
            "notes_by_file": {},
            "error_codes": {},
            "error_types": {},
            "raw_errors": [],
        }

    lines = baseline_path.read_text(encoding="utf-8").strip().split("\n")
    lines = [line for line in lines if line.strip()]

    total_errors = 0
    total_notes = 0
    errors_by_file: dict[str, int] = defaultdict(int)
    notes_by_file: dict[str, int] = defaultdict(int)
    error_codes: dict[str, int] = defaultdict(int)
    error_types: dict[str, int] = defaultdict(int)
    raw_errors: list[dict[str, Any]] = []

    for line in lines:
        # 解析行格式: filepath[:line]: type: message
        parts = line.split(": ", 2)
        if len(parts) < 2:
            continue

        location = parts[0]
        # 提取文件路径（可能带行号）
        if ":" in location:
            # filepath:line 格式
            loc_parts = location.rsplit(":", 1)
            filepath = loc_parts[0]
            try:
                line_num: int | None = int(loc_parts[1])
            except ValueError:
                filepath = location
                line_num = None
        else:
            filepath = location
            line_num = None

        rest = ": ".join(parts[1:])
        is_note = "note:" in rest
        is_error = "error:" in rest

        if is_note:
            total_notes += 1
            notes_by_file[filepath] += 1
            raw_errors.append(
                {
                    "file": filepath,
                    "line": line_num,
                    "type": "note",
                    "message": rest,
                }
            )
        elif is_error:
            total_errors += 1
            errors_by_file[filepath] += 1

            # 提取 error code（如果有 [...] 后缀）
            code_match = ERROR_CODE_PATTERN.search(line)
            if code_match:
                error_codes[code_match.group(1)] += 1

            # 提取错误类型描述
            type_match = ERROR_TYPE_PATTERN.search(rest)
            if type_match:
                # 截取前 50 字符作为错误类型标识
                error_type = type_match.group(1)[:50]
                error_types[error_type] += 1

            raw_errors.append(
                {
                    "file": filepath,
                    "line": line_num,
                    "type": "error",
                    "message": rest,
                }
            )

    return {
        "total_errors": total_errors,
        "total_notes": total_notes,
        "total_lines": len(lines),
        "errors_by_file": dict(errors_by_file),
        "notes_by_file": dict(notes_by_file),
        "error_codes": dict(error_codes),
        "error_types": dict(error_types),
        "raw_errors": raw_errors,
    }


def aggregate_by_directory(
    errors_by_file: dict[str, int], notes_by_file: dict[str, int]
) -> dict[str, dict[str, int]]:
    """按目录前缀聚合统计"""
    result: dict[str, dict[str, int]] = {}

    for prefix in DIRECTORY_PREFIXES:
        errors = sum(count for f, count in errors_by_file.items() if f.startswith(prefix))
        notes = sum(count for f, count in notes_by_file.items() if f.startswith(prefix))
        if errors > 0 or notes > 0:
            result[prefix] = {"errors": errors, "notes": notes}

    # 统计未分类的文件（不匹配任何前缀）
    other_errors = 0
    other_notes = 0
    for f, count in errors_by_file.items():
        if not any(f.startswith(p) for p in DIRECTORY_PREFIXES):
            other_errors += count
    for f, count in notes_by_file.items():
        if not any(f.startswith(p) for p in DIRECTORY_PREFIXES):
            other_notes += count

    if other_errors > 0 or other_notes > 0:
        result["other"] = {"errors": other_errors, "notes": other_notes}

    return result


def path_to_module(path: str) -> str:
    """
    将文件/目录路径转换为模块名

    规则:
    - 文件路径: src/engram/gateway/di.py -> src.engram.gateway.di
    - 目录路径: src/engram/gateway/ -> src.engram.gateway.*
    """
    # 移除尾部斜杠
    path = path.rstrip("/")

    # 判断是否为目录（原始路径以 / 结尾或不含 .py）
    is_directory = not path.endswith(".py")

    # 移除 .py 扩展名
    if path.endswith(".py"):
        path = path[:-3]

    # 将路径分隔符转为点号
    module = path.replace("/", ".").replace("\\", ".")

    # 目录添加 .* 后缀
    if is_directory:
        module = f"{module}.*"

    return module


def get_strict_island_info(pyproject_config: dict[str, Any]) -> dict[str, Any]:
    """提取 strict-island 配置信息"""
    try:
        paths = pyproject_config.get("tool", {}).get("engram", {}).get("mypy", {}).get(
            "strict_island_paths", []
        )
    except (KeyError, TypeError):
        paths = []

    # 将路径转换为模块名
    modules = [path_to_module(p) for p in paths]

    return {
        "paths": paths,
        "count": len(paths),
        "modules": modules,
        "coverage_summary": f"{len(paths)} paths configured",
    }


def generate_metrics_report(
    baseline_path: Path,
    pyproject_path: Path,
    verbose: bool = False,
) -> dict[str, Any]:
    """生成完整的指标报告"""
    # 解析 baseline
    baseline_data = parse_baseline(baseline_path)

    # 加载 pyproject 配置
    pyproject_config = load_pyproject_config(pyproject_path)

    # 按目录聚合
    by_directory = aggregate_by_directory(
        baseline_data["errors_by_file"],
        baseline_data["notes_by_file"],
    )

    # 获取 strict-island 信息
    strict_island = get_strict_island_info(pyproject_config)

    # 按错误数排序的文件列表（top 20）
    errors_by_file = baseline_data["errors_by_file"]
    sorted_files = sorted(errors_by_file.items(), key=lambda x: x[1], reverse=True)
    top_files = [{"file": f, "errors": c} for f, c in sorted_files[:20]]

    # 构建报告
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_file": str(baseline_path),
        "summary": {
            "total_errors": baseline_data["total_errors"],
            "total_notes": baseline_data["total_notes"],
            "total_lines": baseline_data["total_lines"],
        },
        "by_directory": by_directory,
        "by_error_code": baseline_data["error_codes"],
        "by_error_type": dict(
            sorted(
                baseline_data["error_types"].items(),
                key=lambda x: x[1],
                reverse=True,
            )[:20]  # 取 top 20
        ),
        "top_files": top_files,
        "errors_by_file": errors_by_file,
        "strict_island": strict_island,
    }

    if verbose:
        print("=== mypy 指标报告 ===")
        print(f"生成时间: {report['generated_at']}")
        print(f"Baseline 文件: {baseline_path}")
        print()
        print("[Summary]")
        print(f"  总错误数: {baseline_data['total_errors']}")
        print(f"  总 note 数: {baseline_data['total_notes']}")
        print(f"  总行数: {baseline_data['total_lines']}")
        print()
        print("[按目录分布]")
        for dir_prefix, stats in by_directory.items():
            print(f"  {dir_prefix}: {stats['errors']} errors, {stats['notes']} notes")
        print()
        if baseline_data["error_codes"]:
            print("[按 error-code 分布（top 10）]")
            for code, count in sorted(
                baseline_data["error_codes"].items(), key=lambda x: x[1], reverse=True
            )[:10]:
                print(f"  [{code}]: {count}")
            print()
        if top_files:
            print("[按文件分布（top 10）]")
            for item in top_files[:10]:
                print(f"  {item['file']}: {item['errors']} errors")
            print()
        print("[Strict Island 配置]")
        print(f"  路径数: {strict_island['count']}")
        for i, path in enumerate(strict_island["paths"]):
            module = strict_island["modules"][i] if i < len(strict_island["modules"]) else ""
            print(f"    - {path} -> {module}")
        print()

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mypy 指标聚合脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=Path(DEFAULT_BASELINE_FILE),
        help=f"Baseline 文件路径（默认: {DEFAULT_BASELINE_FILE}）",
    )
    parser.add_argument(
        "--pyproject-file",
        type=Path,
        default=Path(DEFAULT_PYPROJECT_FILE),
        help=f"pyproject.toml 文件路径（默认: {DEFAULT_PYPROJECT_FILE}）",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path(DEFAULT_OUTPUT_FILE),
        help=f"输出 JSON 文件路径（默认: {DEFAULT_OUTPUT_FILE}）",
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
    report = generate_metrics_report(
        baseline_path=args.baseline_file,
        pyproject_path=args.pyproject_file,
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
