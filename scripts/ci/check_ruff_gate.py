#!/usr/bin/env python3
"""
ruff 门禁检查脚本

功能：
1. 运行 ruff check 并解析结果
2. 支持两种门禁模式：current / future-baseline
3. current 模式：严格失败，任何 violation 都阻断
4. future-baseline 模式：对新增规则集仅阻止新增违规

门禁模式：
- current:         任何 ruff violation 都导致非零退出码
- future-baseline: 读取 baseline 文件，仅当新增 violation 时失败

baseline 文件格式（ruff_baseline_future.json）：
{
    "rules": ["E501", "W503"],  // future baseline 包含的规则
    "violations": {
        "E501": {
            "count": 45,
            "files": {
                "src/foo.py": [10, 20, 30],  // 行号列表
                ...
            }
        },
        ...
    }
}

环境变量：
- ENGRAM_RUFF_GATE:          门禁级别 (current/future-baseline)
- ENGRAM_RUFF_BASELINE_FILE: baseline 文件路径

用法：
    # 默认模式（current）
    python scripts/ci/check_ruff_gate.py

    # future-baseline 模式
    python scripts/ci/check_ruff_gate.py --gate future-baseline

    # 更新 baseline 文件
    python scripts/ci/check_ruff_gate.py --write-baseline --rules E501,W503

退出码：
    0 - 检查通过
    1 - 检查失败（存在 violation 或新增 violation）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 默认配置
DEFAULT_GATE = "current"
DEFAULT_BASELINE_FILE = Path(__file__).parent / "ruff_baseline_future.json"
DEFAULT_SCAN_PATHS = ["src/", "tests/"]

# 环境变量名称
ENV_RUFF_GATE = "ENGRAM_RUFF_GATE"
ENV_RUFF_BASELINE_FILE = "ENGRAM_RUFF_BASELINE_FILE"

# 有效的门禁级别
VALID_GATES = {"current", "future-baseline"}


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

    output = result.stdout
    if not output.strip():
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


def load_baseline(baseline_path: Path) -> dict[str, Any]:
    """
    加载 baseline 文件。

    Returns:
        baseline 数据字典，如果文件不存在返回空结构
    """
    if not baseline_path.exists():
        return {"rules": [], "violations": {}}

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] 无法加载 baseline 文件 {baseline_path}: {e}", file=sys.stderr)
        return {"rules": [], "violations": {}}


def save_baseline(
    violations: list[dict[str, Any]],
    rules: list[str],
    baseline_path: Path,
) -> None:
    """
    保存 baseline 文件。

    Args:
        violations: 所有 violation 列表
        rules: 要包含在 baseline 中的规则列表
        baseline_path: 保存路径
    """
    # 过滤出指定规则的 violations
    filtered = [v for v in violations if v.get("code") in rules]

    # 构建 baseline 结构
    violations_dict: dict[str, dict[str, Any]] = {}
    for rule in rules:
        rule_violations = [v for v in filtered if v.get("code") == rule]
        files_dict: dict[str, list[int]] = defaultdict(list)
        for v in rule_violations:
            filename = v.get("filename", "")
            line = v.get("location", {}).get("row", 0)
            files_dict[filename].append(line)

        # 排序行号
        for filename in files_dict:
            files_dict[filename] = sorted(files_dict[filename])

        violations_dict[rule] = {
            "count": len(rule_violations),
            "files": dict(files_dict),
        }

    baseline = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": sorted(rules),
        "violations": violations_dict,
    }

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    with open(baseline_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)


def parse_violation_key(v: dict[str, Any]) -> str:
    """
    生成 violation 的唯一标识 key。

    格式：filename:line:column:code
    """
    filename = v.get("filename", "")
    location = v.get("location", {})
    line = location.get("row", 0)
    column = location.get("column", 0)
    code = v.get("code", "")
    return f"{filename}:{line}:{column}:{code}"


def check_future_baseline(
    violations: list[dict[str, Any]],
    baseline: dict[str, Any],
    verbose: bool = False,
) -> tuple[bool, list[dict[str, Any]]]:
    """
    检查 future-baseline 模式下是否有新增 violation。

    Args:
        violations: 当前所有 violations
        baseline: baseline 数据
        verbose: 是否输出详细信息

    Returns:
        (passed, new_violations) 元组
    """
    rules = set(baseline.get("rules", []))
    baseline_violations = baseline.get("violations", {})

    # 过滤出 baseline 规则的 violations
    filtered = [v for v in violations if v.get("code") in rules]

    # 构建 baseline 中已有的 violation set
    baseline_set: set[str] = set()
    for rule, rule_data in baseline_violations.items():
        files = rule_data.get("files", {})
        for filename, lines in files.items():
            for line in lines:
                # baseline 不记录 column，使用 :*: 通配
                baseline_set.add(f"{filename}:{line}:{rule}")

    # 检查新增
    new_violations: list[dict[str, Any]] = []
    for v in filtered:
        filename = v.get("filename", "")
        location = v.get("location", {})
        line = location.get("row", 0)
        code = v.get("code", "")
        key = f"{filename}:{line}:{code}"
        if key not in baseline_set:
            new_violations.append(v)

    if verbose:
        print(f"[INFO] Baseline 规则: {', '.join(sorted(rules)) or '(空)'}")
        print(f"[INFO] Baseline 中的 violation 数: {sum(r['count'] for r in baseline_violations.values())}")
        print(f"[INFO] 当前 baseline 规则 violation 数: {len(filtered)}")
        print(f"[INFO] 新增 violation 数: {len(new_violations)}")

    passed = len(new_violations) == 0
    return passed, new_violations


def resolve_config(args: argparse.Namespace) -> dict[str, Any]:
    """
    解析配置，按优先级合并 CLI 参数和环境变量。

    优先级: CLI 参数 > 环境变量 > 默认值
    """
    # Gate 解析
    if args.gate is not None:
        gate = args.gate
    elif os.environ.get(ENV_RUFF_GATE):
        gate = os.environ[ENV_RUFF_GATE]
    else:
        gate = DEFAULT_GATE

    if gate not in VALID_GATES:
        print(f"[WARN] 无效的 gate 值 '{gate}'，使用默认值 '{DEFAULT_GATE}'", file=sys.stderr)
        gate = DEFAULT_GATE

    # Baseline file 解析
    if args.baseline_file is not None:
        baseline_file = Path(args.baseline_file)
    elif os.environ.get(ENV_RUFF_BASELINE_FILE):
        baseline_file = Path(os.environ[ENV_RUFF_BASELINE_FILE])
    else:
        baseline_file = DEFAULT_BASELINE_FILE

    # Scan paths 解析
    scan_paths = args.scan_paths if args.scan_paths else DEFAULT_SCAN_PATHS

    return {
        "gate": gate,
        "baseline_file": baseline_file,
        "scan_paths": scan_paths,
        "write_baseline": args.write_baseline,
        "rules": args.rules.split(",") if args.rules else [],
        "verbose": args.verbose,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ruff 门禁检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--gate",
        choices=["current", "future-baseline"],
        default=None,
        help=(
            f"门禁级别: current=任何 violation 阻断, "
            f"future-baseline=对比 baseline "
            f"(默认: {DEFAULT_GATE}, 环境变量: {ENV_RUFF_GATE})"
        ),
    )
    parser.add_argument(
        "--baseline-file",
        type=str,
        default=None,
        help=(
            f"Baseline 文件路径 "
            f"(默认: {DEFAULT_BASELINE_FILE}, 环境变量: {ENV_RUFF_BASELINE_FILE})"
        ),
    )
    parser.add_argument(
        "--scan-paths",
        nargs="+",
        default=None,
        help=f"扫描路径列表（默认: {' '.join(DEFAULT_SCAN_PATHS)}）",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="生成/更新 baseline 文件（需配合 --rules 使用）",
    )
    parser.add_argument(
        "--rules",
        type=str,
        default="",
        help="写入 baseline 的规则列表，逗号分隔（如: E501,W503）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )

    args = parser.parse_args()

    # 解析配置
    config = resolve_config(args)
    gate = config["gate"]
    baseline_file = config["baseline_file"]
    scan_paths = config["scan_paths"]
    write_baseline = config["write_baseline"]
    rules = config["rules"]
    verbose = config["verbose"]

    print("=" * 70)
    print("ruff 门禁检查")
    print("=" * 70)
    print()
    print(f"门禁级别:     {gate}")
    print(f"扫描路径:     {', '.join(scan_paths)}")
    if gate == "future-baseline":
        print(f"Baseline 文件: {baseline_file}")
    print()

    # 运行 ruff
    print("正在运行 ruff check...")
    violations, return_code = run_ruff_check(scan_paths)
    print(f"当前 violation 数: {len(violations)}")
    print()

    # 写入 baseline 模式
    if write_baseline:
        if not rules:
            print("[ERROR] --write-baseline 需要配合 --rules 使用")
            return 1
        save_baseline(violations, rules, baseline_file)
        print(f"[OK] Baseline 已更新: {baseline_file}")
        print(f"     规则: {', '.join(rules)}")
        filtered_count = len([v for v in violations if v.get("code") in rules])
        print(f"     记录的 violation 数: {filtered_count}")
        return 0

    # gate=current 模式：严格失败
    if gate == "current":
        if violations:
            print(f"[FAIL] current 模式: 存在 {len(violations)} 个 ruff violation")
            print()
            print("Violation 摘要（按 code 分组）:")
            by_code: dict[str, int] = defaultdict(int)
            for v in violations:
                by_code[v.get("code", "UNKNOWN")] += 1
            for code, count in sorted(by_code.items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"  {code}: {count}")
            if verbose:
                print()
                print("详细列表（前 20 条）:")
                for v in violations[:20]:
                    filename = v.get("filename", "")
                    location = v.get("location", {})
                    line = location.get("row", 0)
                    code = v.get("code", "")
                    message = v.get("message", "")[:60]
                    print(f"  {filename}:{line} [{code}] {message}")
                if len(violations) > 20:
                    print(f"  ... 及其他 {len(violations) - 20} 条")
            print()
            print("=" * 70)
            print("[FAIL] 退出码: 1")
            return 1
        else:
            print("[OK] current 模式: 无 ruff violation")
            print()
            print("=" * 70)
            print("[OK] 退出码: 0")
            return 0

    # gate=future-baseline 模式：对比 baseline
    if gate == "future-baseline":
        baseline = load_baseline(baseline_file)
        passed, new_violations = check_future_baseline(violations, baseline, verbose)

        if not passed:
            print(f"[FAIL] future-baseline 模式: 存在 {len(new_violations)} 个新增 violation")
            print()
            print("新增 violation 列表:")
            for v in new_violations[:20]:
                filename = v.get("filename", "")
                location = v.get("location", {})
                line = location.get("row", 0)
                code = v.get("code", "")
                message = v.get("message", "")[:60]
                print(f"  {filename}:{line} [{code}] {message}")
            if len(new_violations) > 20:
                print(f"  ... 及其他 {len(new_violations) - 20} 条")
            print()
            print("解决方案:")
            print("  1. 修复新增的 lint 违规")
            print("  2. 如需更新 baseline（需 reviewer 批准）:")
            rules_str = ",".join(baseline.get("rules", []))
            print(f"     python {__file__} --write-baseline --rules {rules_str}")
            print()
            print("=" * 70)
            print("[FAIL] 退出码: 1")
            return 1
        else:
            print("[OK] future-baseline 模式: 无新增 violation")
            print()
            print("=" * 70)
            print("[OK] 退出码: 0")
            return 0

    # 不应该到达这里
    return 0


if __name__ == "__main__":
    sys.exit(main())
