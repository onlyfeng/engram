#!/usr/bin/env python3
"""
mypy 门禁检查脚本

功能:
1. 运行 mypy 类型检查
2. 支持五种门禁模式：strict / baseline / strict-island / warn / off
3. 支持基线文件对比（仅 baseline 模式）
4. 支持 strict island 模式（仅检查特定模块）
5. 支持 CLI 和环境变量配置
6. 支持阈值检查和迁移阶段管理

门禁模式:
- strict:        任何 mypy 错误都会导致非零退出码
- baseline:      对比基线文件，仅新增错误时失败（默认）
- strict-island: 仅检查 strict island 模块，错误则失败
- warn:          运行 mypy 并输出错误，但永远返回 0（仅警告模式）
- off:           跳过检查，永远返回 0

三阶段切换策略:
- 阶段 0: 所有分支 = baseline（默认）
- 阶段 1: master/main = strict, PR = baseline
- 阶段 2: 所有分支 = strict（当 baseline 错误数 = 0）
- 阶段 3: 已归档，直接 strict

配置优先级: CLI 参数 > ENGRAM_* 环境变量 > 旧环境变量 > 默认值

环境变量:
- ENGRAM_MYPY_GATE:              门禁级别 (strict/baseline/strict-island/warn/off)
- ENGRAM_MYPY_BASELINE_FILE:     基线文件路径
- ENGRAM_MYPY_PATH:              mypy 扫描路径
- ENGRAM_MYPY_GATE_OVERRIDE:     回滚开关，强制使用指定 gate
- ENGRAM_MYPY_STRICT_THRESHOLD:  PR 切换到 strict 的阈值（默认 0）
- ENGRAM_MYPY_MIGRATION_PHASE:   迁移阶段 (0/1/2/3)

旧环境变量（兼容性，优先级低于 ENGRAM_* 前缀）:
- MYPY_GATE:          门禁级别
- MYPY_BASELINE_FILE: 基线文件路径
- MYPY_PATH:          mypy 扫描路径

用法:
    # 默认模式（baseline）
    python scripts/ci/check_mypy_gate.py

    # 严格模式
    python scripts/ci/check_mypy_gate.py --gate strict

    # Strict Island 模式
    python scripts/ci/check_mypy_gate.py --gate strict-island

    # 警告模式（仅输出，不阻断）
    python scripts/ci/check_mypy_gate.py --gate warn

    # 更新基线文件
    python scripts/ci/check_mypy_gate.py --write-baseline

    # 归档基线文件（阶段 3）
    python scripts/ci/check_mypy_gate.py --archive-baseline

    # 检查阈值状态
    python scripts/ci/check_mypy_gate.py --check-threshold

    # 自定义基线文件和扫描路径
    python scripts/ci/check_mypy_gate.py --baseline-file mypy_errors.txt --mypy-path src/

退出码:
    0 - 检查通过（或 gate=off/warn）
    1 - 检查失败（存在新增错误或 strict 模式下有任何错误）
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Set

# Python 3.11+ 内置 tomllib，3.10 需要 tomli
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]


# ============================================================================
# 默认配置
# ============================================================================

DEFAULT_GATE = "baseline"
DEFAULT_BASELINE_FILE = Path(__file__).parent / "mypy_baseline.txt"
DEFAULT_MYPY_PATH = "src/engram/"

# Artifact 输出目录（CI 上传用）
ARTIFACTS_DIR = Path("artifacts")
ARTIFACT_CURRENT_FILE = ARTIFACTS_DIR / "mypy_current.txt"
ARTIFACT_NEW_ERRORS_FILE = ARTIFACTS_DIR / "mypy_new_errors.txt"

# 环境变量名称
ENV_MYPY_GATE = "ENGRAM_MYPY_GATE"
ENV_MYPY_BASELINE_FILE = "ENGRAM_MYPY_BASELINE_FILE"
ENV_MYPY_PATH = "ENGRAM_MYPY_PATH"

# 迁移阶段控制
ENV_MYPY_GATE_OVERRIDE = "ENGRAM_MYPY_GATE_OVERRIDE"
ENV_MYPY_STRICT_THRESHOLD = "ENGRAM_MYPY_STRICT_THRESHOLD"
ENV_MYPY_MIGRATION_PHASE = "ENGRAM_MYPY_MIGRATION_PHASE"

# 有效的门禁级别
VALID_GATES = {"strict", "baseline", "strict-island", "warn", "off"}

# 归档目录
ARCHIVED_DIR = Path(__file__).parent / "archived"
ARCHIVED_BASELINE_FILE = ARCHIVED_DIR / "mypy_baseline.txt.archived"

# 旧环境变量名称（兼容性支持，优先级低于 ENGRAM_* 前缀）
ENV_MYPY_GATE_LEGACY = "MYPY_GATE"
ENV_MYPY_BASELINE_FILE_LEGACY = "MYPY_BASELINE_FILE"
ENV_MYPY_PATH_LEGACY = "MYPY_PATH"


# ============================================================================
# 配置文件读取
# ============================================================================


def get_project_root() -> Path:
    """
    获取项目根目录。

    从脚本位置推断: scripts/ci/ -> 项目根目录
    """
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def load_strict_island_paths(pyproject_path: Path | None = None) -> List[str]:
    """
    从 pyproject.toml 读取 strict island 路径列表。

    Args:
        pyproject_path: pyproject.toml 文件路径，默认为项目根目录下的 pyproject.toml

    Returns:
        strict island 路径列表

    Raises:
        FileNotFoundError: 如果 pyproject.toml 不存在
        KeyError: 如果配置中缺少 [tool.engram.mypy].strict_island_paths
    """
    if pyproject_path is None:
        pyproject_path = get_project_root() / "pyproject.toml"

    with open(pyproject_path, "rb") as f:
        config = tomllib.load(f)

    try:
        paths = config["tool"]["engram"]["mypy"]["strict_island_paths"]
    except KeyError as e:
        raise KeyError(
            f"pyproject.toml 中缺少 [tool.engram.mypy].strict_island_paths 配置: {e}"
        ) from e

    if not isinstance(paths, list):
        raise TypeError(
            f"strict_island_paths 必须是列表类型，实际类型: {type(paths).__name__}"
        )

    return paths


# Strict Island 模块列表（从 pyproject.toml 读取）
# 这些模块已通过类型修复，mypy 错误为 0
# 配置位于 [tool.engram.mypy].strict_island_paths
def _get_strict_island_modules() -> List[str]:
    """
    获取 strict island 模块列表。

    优先从 pyproject.toml 读取，如果失败则返回空列表并打印警告。
    """
    try:
        return load_strict_island_paths()
    except (FileNotFoundError, KeyError, TypeError) as e:
        print(f"[WARN] 无法加载 strict island 配置: {e}", file=sys.stderr)
        return []


STRICT_ISLAND_MODULES = _get_strict_island_modules()


# ============================================================================
# 规范化算法
# ============================================================================


def normalize_error(line: str) -> str:
    """
    规范化 mypy 错误行，确保稳定性和可比较性。

    规范化规则:
    1. 移除行号（便于代码移动后对比）
    2. 统一路径分隔符为 /（跨平台兼容）
    3. 移除尾部空白

    示例:
        输入: src\\engram\\foo.py:42: error: Something wrong  [error-code]
        输出: src/engram/foo.py: error: Something wrong  [error-code]
    """
    # 移除行号: "file.py:123:" -> "file.py:"
    line = re.sub(r"^([^:]+):\d+:", r"\1:", line)
    # 统一路径分隔符为 /
    # 只处理文件路径部分（第一个 : 之前）
    parts = line.split(":", 1)
    if len(parts) == 2:
        file_path = parts[0].replace("\\", "/")
        line = f"{file_path}:{parts[1]}"
    else:
        line = line.replace("\\", "/")
    return line.strip()


def parse_mypy_output(output: str) -> Set[str]:
    """
    解析 mypy 输出，提取并规范化错误行。

    过滤规则:
    - 跳过空行
    - 跳过 "Found X errors..." 摘要行
    - 跳过 "Success:" 行
    - 只处理包含 "error:" / "warning:" / "note:" 的行

    返回规范化后的错误集合（去重）。
    """
    errors: Set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        # 跳过空行
        if not line:
            continue
        # 过滤摘要行: "Found X errors in Y files..."
        if line.startswith("Found "):
            continue
        # 过滤成功行
        if line.startswith("Success:"):
            continue
        # 只处理包含错误类型标记的行
        if ": error:" in line or ": warning:" in line or ": note:" in line:
            errors.add(normalize_error(line))
    return errors


def stable_sort(errors: Set[str]) -> List[str]:
    """
    对错误集合进行稳定排序。

    排序规则: 按字母顺序排序，确保输出稳定。
    """
    return sorted(errors)


# ============================================================================
# 基线文件操作
# ============================================================================


def load_baseline(baseline_path: Path) -> Set[str]:
    """
    加载基线文件。

    如果基线文件不存在，返回空集合。
    基线文件格式: 每行一条规范化后的错误。
    """
    if not baseline_path.exists():
        return set()
    content = baseline_path.read_text(encoding="utf-8")
    return {line.strip() for line in content.splitlines() if line.strip()}


def save_baseline(errors: Set[str], baseline_path: Path) -> None:
    """
    保存基线文件。

    按字母顺序排序以保持稳定性。
    """
    sorted_errors = stable_sort(errors)
    content = "\n".join(sorted_errors) + "\n" if sorted_errors else ""
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(content, encoding="utf-8")


def write_artifacts(current_errors: Set[str], new_errors: Set[str]) -> None:
    """
    写入 artifact 文件供 CI 上传。

    输出文件:
    - artifacts/mypy_current.txt: 当前所有错误
    - artifacts/mypy_new_errors.txt: 新增错误（相对于基线）

    如果错误集合为空，文件仍会创建但内容为空。
    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    # 写入当前错误
    current_content = "\n".join(stable_sort(current_errors)) + "\n" if current_errors else ""
    ARTIFACT_CURRENT_FILE.write_text(current_content, encoding="utf-8")

    # 写入新增错误
    new_content = "\n".join(stable_sort(new_errors)) + "\n" if new_errors else ""
    ARTIFACT_NEW_ERRORS_FILE.write_text(new_content, encoding="utf-8")

    print("[INFO] Artifact 文件已写入:")
    print(f"       - {ARTIFACT_CURRENT_FILE} ({len(current_errors)} 条错误)")
    print(f"       - {ARTIFACT_NEW_ERRORS_FILE} ({len(new_errors)} 条新增错误)")


# ============================================================================
# 阈值检查和归档操作
# ============================================================================


def get_baseline_count(baseline_path: Path) -> int:
    """
    获取基线文件中的错误数量。

    Args:
        baseline_path: 基线文件路径

    Returns:
        错误数量，如果文件不存在返回 0
    """
    if not baseline_path.exists():
        return 0
    content = baseline_path.read_text(encoding="utf-8")
    return len([line for line in content.splitlines() if line.strip()])


def check_threshold_status(baseline_path: Path, threshold: int = 0) -> dict:
    """
    检查阈值状态，判断是否可以切换到 strict 模式。

    Args:
        baseline_path: 基线文件路径
        threshold: strict 阈值（默认 0）

    Returns:
        包含检查结果的字典
    """
    count = get_baseline_count(baseline_path)
    can_switch = count <= threshold

    return {
        "baseline_count": count,
        "threshold": threshold,
        "can_switch_to_strict": can_switch,
        "baseline_exists": baseline_path.exists(),
    }


def archive_baseline(baseline_path: Path, archived_path: Path | None = None) -> bool:
    """
    归档基线文件（阶段 3 操作）。

    Args:
        baseline_path: 当前基线文件路径
        archived_path: 归档目标路径（默认: scripts/ci/archived/mypy_baseline.txt.archived）

    Returns:
        True 表示归档成功，False 表示归档失败
    """
    if archived_path is None:
        archived_path = ARCHIVED_BASELINE_FILE

    if not baseline_path.exists():
        print(f"[WARN] 基线文件不存在: {baseline_path}")
        return False

    # 检查基线是否为空
    count = get_baseline_count(baseline_path)
    if count > 0:
        print(f"[ERROR] 基线文件仍有 {count} 个错误，不建议归档")
        print("        请先修复所有基线错误后再归档")
        return False

    # 创建归档目录
    archived_path.parent.mkdir(parents=True, exist_ok=True)

    # 移动文件
    import shutil
    shutil.move(str(baseline_path), str(archived_path))

    print(f"[OK] 基线文件已归档: {baseline_path} -> {archived_path}")
    print()
    print("后续步骤:")
    print("  1. git add -A && git commit -m 'chore: archive mypy baseline (phase 3)'")
    print("  2. 更新 repository variable: ENGRAM_MYPY_MIGRATION_PHASE=3")
    print("  3. （可选）移除 CI 中的 baseline 相关逻辑")

    return True


def print_threshold_report(baseline_path: Path) -> None:
    """
    打印阈值状态报告。

    Args:
        baseline_path: 基线文件路径
    """
    threshold_env = os.environ.get(ENV_MYPY_STRICT_THRESHOLD, "0")
    try:
        threshold = int(threshold_env)
    except ValueError:
        threshold = 0

    status = check_threshold_status(baseline_path, threshold)

    print("=" * 70)
    print("mypy Baseline 阈值状态报告")
    print("=" * 70)
    print()
    print(f"基线文件:       {baseline_path}")
    print(f"基线存在:       {'是' if status['baseline_exists'] else '否'}")
    print(f"基线错误数:     {status['baseline_count']}")
    print(f"strict 阈值:    {status['threshold']}")
    print()

    if status["can_switch_to_strict"]:
        print("[OK] 可以切换到 strict 模式")
        print()
        print("建议操作:")
        if status["baseline_count"] == 0:
            print("  - 当前为阶段 2，建议执行阶段 3 归档:")
            print("    python scripts/ci/check_mypy_gate.py --archive-baseline")
        else:
            print("  - 更新 ENGRAM_MYPY_MIGRATION_PHASE 到下一阶段")
    else:
        print(f"[INFO] baseline 错误数 ({status['baseline_count']}) > 阈值 ({status['threshold']})")
        print("       需要继续修复 baseline 错误")
    print()


# ============================================================================
# mypy 执行
# ============================================================================


def run_mypy(mypy_path: str) -> tuple[str, int]:
    """
    运行 mypy 并返回输出和返回码。

    Args:
        mypy_path: mypy 扫描的目标路径

    Returns:
        (output, return_code) 元组
    """
    mypy_cmd = ["mypy", mypy_path]

    # 从脚本位置推断项目根目录
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent.parent  # scripts/ci/ -> 项目根目录

    result = subprocess.run(
        mypy_cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    # mypy 的输出可能在 stdout 或 stderr
    output = result.stdout + result.stderr
    return output, result.returncode


def run_mypy_strict_island() -> tuple[str, int]:
    """
    运行 mypy 检查 strict island 模块。

    只检查 STRICT_ISLAND_MODULES 中定义的模块。

    Returns:
        (output, return_code) 元组
    """
    mypy_cmd = ["mypy"] + STRICT_ISLAND_MODULES

    # 从脚本位置推断项目根目录
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent.parent  # scripts/ci/ -> 项目根目录

    result = subprocess.run(
        mypy_cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    # mypy 的输出可能在 stdout 或 stderr
    output = result.stdout + result.stderr
    return output, result.returncode


def filter_strict_island_errors(errors: Set[str]) -> Set[str]:
    """
    过滤出仅属于 strict island 模块的错误。

    Args:
        errors: 所有错误集合

    Returns:
        仅属于 strict island 模块的错误集合
    """
    island_errors: Set[str] = set()
    for err in errors:
        # 检查错误是否属于 strict island 模块
        for module in STRICT_ISLAND_MODULES:
            # 移除 src/ 前缀进行比较
            module_path = module.replace("src/", "")
            if module_path.endswith("/"):
                # 目录匹配
                if module_path.rstrip("/") in err:
                    island_errors.add(err)
                    break
            else:
                # 文件匹配
                if module_path in err:
                    island_errors.add(err)
                    break
    return island_errors


# ============================================================================
# 配置解析
# ============================================================================


def resolve_config(args: argparse.Namespace) -> dict:
    """
    解析配置，按优先级合并 CLI 参数和环境变量。

    优先级: CLI 参数 > ENGRAM_* 环境变量 > 旧环境变量 > 默认值
    """
    # Gate 解析
    # 优先级: CLI > ENGRAM_* ENV > legacy ENV > default
    if args.gate is not None:
        gate = args.gate
    elif os.environ.get(ENV_MYPY_GATE):
        gate = os.environ[ENV_MYPY_GATE]
    elif os.environ.get(ENV_MYPY_GATE_LEGACY):
        gate = os.environ[ENV_MYPY_GATE_LEGACY]
    else:
        gate = DEFAULT_GATE

    # 验证 gate 值
    if gate not in VALID_GATES:
        print(
            f"[WARN] 无效的 gate 值 '{gate}'，使用默认值 '{DEFAULT_GATE}'",
            file=sys.stderr,
        )
        gate = DEFAULT_GATE

    # Baseline file 解析
    # 优先级: CLI > ENGRAM_* ENV > legacy ENV > default
    if args.baseline_file is not None:
        baseline_file = Path(args.baseline_file)
    elif os.environ.get(ENV_MYPY_BASELINE_FILE):
        baseline_file = Path(os.environ[ENV_MYPY_BASELINE_FILE])
    elif os.environ.get(ENV_MYPY_BASELINE_FILE_LEGACY):
        baseline_file = Path(os.environ[ENV_MYPY_BASELINE_FILE_LEGACY])
    else:
        baseline_file = DEFAULT_BASELINE_FILE

    # Mypy path 解析
    # 优先级: CLI > ENGRAM_* ENV > legacy ENV > default
    if args.mypy_path is not None:
        mypy_path = args.mypy_path
    elif os.environ.get(ENV_MYPY_PATH):
        mypy_path = os.environ[ENV_MYPY_PATH]
    elif os.environ.get(ENV_MYPY_PATH_LEGACY):
        mypy_path = os.environ[ENV_MYPY_PATH_LEGACY]
    else:
        mypy_path = DEFAULT_MYPY_PATH

    return {
        "gate": gate,
        "baseline_file": baseline_file,
        "mypy_path": mypy_path,
        "write_baseline": args.write_baseline,
        "verbose": args.verbose,
    }


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mypy 门禁检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--gate",
        choices=["strict", "baseline", "strict-island", "warn", "off"],
        default=None,
        help=(
            f"门禁级别: strict=任何错误阻断, baseline=对比基线, "
            f"strict-island=仅检查 strict island 模块, warn=仅警告不阻断, off=跳过检查 "
            f"(默认: {DEFAULT_GATE}, 环境变量: {ENV_MYPY_GATE})"
        ),
    )
    parser.add_argument(
        "--baseline-file",
        type=str,
        default=None,
        help=(
            f"基线文件路径 "
            f"(默认: {DEFAULT_BASELINE_FILE}, 环境变量: {ENV_MYPY_BASELINE_FILE})"
        ),
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="生成/更新基线文件（不进行对比检查）",
    )
    parser.add_argument(
        "--archive-baseline",
        action="store_true",
        help="归档基线文件（阶段 3 操作，需要基线错误数为 0）",
    )
    parser.add_argument(
        "--check-threshold",
        action="store_true",
        help="检查阈值状态，判断是否可以切换到 strict 模式",
    )
    parser.add_argument(
        "--mypy-path",
        type=str,
        default=None,
        help=(
            f"mypy 扫描路径 "
            f"(默认: {DEFAULT_MYPY_PATH}, 环境变量: {ENV_MYPY_PATH})"
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出（包括 mypy 原始输出）",
    )
    args = parser.parse_args()

    # 解析配置
    config = resolve_config(args)
    gate = config["gate"]
    baseline_file = config["baseline_file"]
    mypy_path = config["mypy_path"]
    write_baseline = config["write_baseline"]
    verbose = config["verbose"]

    # 处理 --check-threshold 命令
    if args.check_threshold:
        print_threshold_report(baseline_file)
        return 0

    # 处理 --archive-baseline 命令
    if args.archive_baseline:
        print("=" * 70)
        print("mypy Baseline 归档（阶段 3）")
        print("=" * 70)
        print()
        success = archive_baseline(baseline_file)
        return 0 if success else 1

    print("=" * 70)
    print("mypy 门禁检查")
    print("=" * 70)
    print()
    print(f"门禁级别:     {gate}")
    print(f"基线文件:     {baseline_file}")
    print(f"扫描路径:     {mypy_path}")
    print()

    # gate=off 模式: 直接返回 0
    if gate == "off":
        print("[SKIP] gate=off，跳过 mypy 检查")
        print()
        print("=" * 70)
        print("[OK] 退出码: 0")
        return 0

    # gate=warn 模式: 运行 mypy，输出结果，但永远返回 0
    if gate == "warn":
        print("正在运行 mypy (warn 模式)...")
        output, return_code = run_mypy(mypy_path)

        if verbose:
            print()
            print("--- mypy 原始输出 ---")
            print(output)
            print("--- 原始输出结束 ---")
            print()

        # 解析当前错误
        current_errors = parse_mypy_output(output)
        print(f"当前错误数:   {len(current_errors)}")
        print()

        # 写入 artifact 文件（供 CI 上传）
        # warn 模式下，new_errors 设为空集合（因为没有基线对比）
        write_artifacts(current_errors, set())
        print()

        # 显示错误摘要
        if current_errors:
            print(f"[WARN] 发现 {len(current_errors)} 个 mypy 错误:")
            for err in stable_sort(current_errors)[:20]:
                print(f"  - {err}")
            if len(current_errors) > 20:
                print(f"  ... 及其他 {len(current_errors) - 20} 条")
            print()

        print("=" * 70)
        print("[OK] warn 模式: 仅输出警告，不阻断 CI")
        print()
        print("[OK] 退出码: 0")
        return 0

    # gate=strict-island 模式: 使用专用函数
    if gate == "strict-island":
        print("正在运行 mypy (strict-island 模式)...")
        print(f"检查模块: {', '.join(STRICT_ISLAND_MODULES)}")
        output, return_code = run_mypy_strict_island()
    else:
        # 运行 mypy
        print("正在运行 mypy...")
        output, return_code = run_mypy(mypy_path)

    if verbose:
        print()
        print("--- mypy 原始输出 ---")
        print(output)
        print("--- 原始输出结束 ---")
        print()

    # 解析当前错误
    current_errors = parse_mypy_output(output)
    print(f"当前错误数:   {len(current_errors)}")

    # 写入基线模式
    if write_baseline:
        save_baseline(current_errors, baseline_file)
        print()
        print(f"[OK] 基线已更新: {baseline_file}")
        print(f"     共 {len(current_errors)} 条错误记录")
        return 0

    # gate=strict-island 模式: 仅检查 strict island 模块
    if gate == "strict-island":
        # 过滤出仅属于 strict island 模块的错误
        island_errors = filter_strict_island_errors(current_errors)
        print(f"Strict Island 错误数: {len(island_errors)}")
        print()

        if island_errors:
            print(f"[FAIL] strict-island 模式: Strict Island 模块存在 {len(island_errors)} 个 mypy 错误")
            print()
            print("错误列表:")
            for err in stable_sort(island_errors):
                print(f"  - {err}")
            print()
            print("=" * 70)
            print("[FAIL] 退出码: 1")
            return 1
        else:
            print("[OK] strict-island 模式: Strict Island 模块无 mypy 错误")
            print()
            print("=" * 70)
            print("[OK] 退出码: 0")
            return 0

    # gate=strict 模式: 任何错误都失败
    if gate == "strict":
        print()
        if current_errors:
            print(f"[FAIL] strict 模式: 存在 {len(current_errors)} 个 mypy 错误")
            print()
            print("错误列表:")
            for err in stable_sort(current_errors)[:20]:
                print(f"  - {err}")
            if len(current_errors) > 20:
                print(f"  ... 及其他 {len(current_errors) - 20} 条")
            print()
            print("=" * 70)
            print("[FAIL] 退出码: 1")
            return 1
        else:
            print("[OK] strict 模式: 无 mypy 错误")
            print()
            print("=" * 70)
            print("[OK] 退出码: 0")
            return 0

    # gate=baseline 模式: 对比基线
    baseline_errors = load_baseline(baseline_file)
    print(f"基线错误数:   {len(baseline_errors)}")
    print()

    # 计算差异
    new_errors = current_errors - baseline_errors
    fixed_errors = baseline_errors - current_errors

    # 写入 artifact 文件（供 CI 上传）
    write_artifacts(current_errors, new_errors)
    print()

    # 显示修复的错误
    if fixed_errors:
        print(f"[INFO] 已修复 {len(fixed_errors)} 个错误:")
        for err in stable_sort(fixed_errors)[:10]:
            print(f"  - {err}")
        if len(fixed_errors) > 10:
            print(f"  ... 及其他 {len(fixed_errors) - 10} 条")
        print()

    # 显示新增的错误
    if new_errors:
        print(f"[ERROR] 新增 {len(new_errors)} 个错误:")
        for err in stable_sort(new_errors):
            print(f"  - {err}")
        print()

    # 判断结果
    if new_errors:
        print("=" * 70)
        print("[FAIL] baseline 模式: 存在新增的 mypy 错误")
        print()
        print("解决方案:")
        print("  1. 修复新增的类型错误")
        print("  2. 如果是误报，添加 # type: ignore[error-code] 注释")
        print("  3. 如需更新基线（需 reviewer 批准）:")
        print(f"     python {__file__} --write-baseline")
        print()
        print("[FAIL] 退出码: 1")
        return 1

    print("=" * 70)
    print("[OK] baseline 模式: 无新增 mypy 错误")

    if fixed_errors:
        print()
        print("提示: 基线中有错误已修复，建议更新基线:")
        print(f"  python {__file__} --write-baseline")

    print()
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
