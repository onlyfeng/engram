#!/usr/bin/env python3
"""
Strict Island 准入检查脚本

功能:
1. 检查候选路径是否满足 Strict Island 准入条件
2. 验证 baseline 中该路径相关错误数为 0
3. 验证 pyproject.toml 中存在对应 module 且配置正确

准入条件:
- baseline 中该路径相关错误数为 0
- pyproject.toml [[tool.mypy.overrides]] 中存在对应 module
- disallow_untyped_defs = true
- ignore_missing_imports = false

用法:
    # 检查单个候选路径
    python scripts/ci/check_strict_island_admission.py --candidate src/engram/gateway/foo.py

    # 检查多个候选路径
    python scripts/ci/check_strict_island_admission.py \
        --candidate src/engram/gateway/foo.py \
        --candidate src/engram/logbook/bar.py

    # 从配置文件读取候选路径
    python scripts/ci/check_strict_island_admission.py --candidates-file candidates.json

    # JSON 输出
    python scripts/ci/check_strict_island_admission.py --candidate src/engram/gateway/foo.py --json

退出码:
    0 - 所有候选路径满足准入条件
    1 - 存在不满足准入条件的路径
    2 - 配置错误或参数错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Python 3.11+ 内置 tomllib，3.10 需要 tomli
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]


# ============================================================================
# 数据类
# ============================================================================


@dataclass
class AdmissionError:
    """准入检查错误。"""

    candidate: str
    error_type: str  # "baseline_errors" | "missing_override" | "config_error"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdmissionResult:
    """准入检查结果。"""

    candidate: str
    passed: bool
    baseline_error_count: int = 0
    has_override: bool = False
    disallow_untyped_defs: bool | None = None
    ignore_missing_imports: bool | None = None
    errors: list[AdmissionError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式。"""
        return {
            "candidate": self.candidate,
            "passed": self.passed,
            "baseline_error_count": self.baseline_error_count,
            "has_override": self.has_override,
            "disallow_untyped_defs": self.disallow_untyped_defs,
            "ignore_missing_imports": self.ignore_missing_imports,
            "errors": [
                {
                    "error_type": e.error_type,
                    "message": e.message,
                    "details": e.details,
                }
                for e in self.errors
            ],
        }


@dataclass
class CheckResult:
    """整体检查结果。"""

    ok: bool
    candidates_checked: int = 0
    passed_count: int = 0
    failed_count: int = 0
    results: list[AdmissionResult] = field(default_factory=list)
    config_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式。"""
        return {
            "ok": self.ok,
            "candidates_checked": self.candidates_checked,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "results": [r.to_dict() for r in self.results],
            "config_errors": self.config_errors,
        }


# ============================================================================
# 路径转换工具
# ============================================================================


def path_to_module(path: str) -> str:
    """将文件路径转换为 mypy module 名称。

    Examples:
        src/engram/gateway/di.py -> engram.gateway.di
        src/engram/gateway/services/ -> engram.gateway.services
        src/engram/gateway/services/*.py -> engram.gateway.services.*
    """
    # 移除 src/ 前缀
    if path.startswith("src/"):
        path = path[4:]

    # 处理目录路径（以 / 结尾）
    if path.endswith("/"):
        path = path.rstrip("/")
        # 目录映射到 module.* 通配符
        module = path.replace("/", ".")
        return f"{module}.*"

    # 处理 .py 文件
    if path.endswith(".py"):
        path = path[:-3]

    # 处理通配符
    if path.endswith("/*"):
        path = path[:-2]
        module = path.replace("/", ".")
        return f"{module}.*"

    return path.replace("/", ".")


def module_matches_path(module: str, path: str) -> bool:
    """检查 mypy override module 是否匹配文件路径。

    Args:
        module: mypy override 中的 module 名称，如 "engram.gateway.di" 或 "engram.gateway.services.*"
        path: 文件路径，如 "src/engram/gateway/di.py" 或 "src/engram/gateway/services/"

    Returns:
        是否匹配
    """
    path_module = path_to_module(path)

    # 精确匹配
    if module == path_module:
        return True

    # 通配符匹配：module 以 .* 结尾
    if module.endswith(".*"):
        prefix = module[:-2]  # 移除 .*
        # 路径 module 应该以 prefix 开头
        if path_module.startswith(prefix):
            return True

    # 路径是目录（.*），module 匹配其下的任意子模块
    if path_module.endswith(".*"):
        prefix = path_module[:-2]
        if module.startswith(prefix):
            return True

    return False


# ============================================================================
# 核心检查逻辑
# ============================================================================


def count_baseline_errors(baseline_path: Path, candidate: str) -> int:
    """统计 baseline 中与候选路径相关的错误数。

    Args:
        baseline_path: baseline 文件路径
        candidate: 候选路径（如 src/engram/gateway/foo.py）

    Returns:
        错误数量
    """
    if not baseline_path.exists():
        return 0

    content = baseline_path.read_text(encoding="utf-8")
    count = 0

    # 判断是否为目录路径
    is_directory = candidate.endswith("/")
    # 规范化候选路径（用于目录匹配）
    candidate_dir_prefix = candidate.rstrip("/") + "/"
    # 规范化候选路径（用于文件匹配）
    candidate_file = candidate.rstrip("/")

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # baseline 格式: path: error: message [code]
        # 或 path: note: message
        if ": error:" in line or ": note:" in line:
            # 提取文件路径
            match = re.match(r"^([^:]+):", line)
            if match:
                error_path = match.group(1).strip()

                # 检查是否匹配
                if is_directory:
                    # 候选是目录，检查 error_path 是否在该目录下
                    if error_path.startswith(candidate_dir_prefix):
                        count += 1
                else:
                    # 候选是文件，精确匹配
                    if error_path == candidate_file:
                        count += 1
    return count


def find_matching_override(
    overrides: list[dict[str, Any]], candidate: str
) -> dict[str, Any] | None:
    """查找与候选路径匹配的 mypy override 配置。

    Args:
        overrides: pyproject.toml 中的 [[tool.mypy.overrides]] 列表
        candidate: 候选路径

    Returns:
        匹配的 override 配置，或 None
    """
    for override in overrides:
        module = override.get("module", "")
        if module_matches_path(module, candidate):
            return override
    return None


def check_override_config(override: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    """检查 override 配置是否满足准入条件。

    Args:
        override: mypy override 配置

    Returns:
        (disallow_untyped_defs, not ignore_missing_imports, error_messages)
    """
    errors: list[str] = []

    disallow_untyped_defs = override.get("disallow_untyped_defs")
    ignore_missing_imports = override.get("ignore_missing_imports")

    if disallow_untyped_defs is not True:
        errors.append(
            f"disallow_untyped_defs 应为 true，当前值: {disallow_untyped_defs}"
        )

    if ignore_missing_imports is not False:
        errors.append(
            f"ignore_missing_imports 应为 false，当前值: {ignore_missing_imports}"
        )

    return disallow_untyped_defs is True, ignore_missing_imports is False, errors


def check_candidate(
    candidate: str,
    baseline_path: Path,
    overrides: list[dict[str, Any]],
) -> AdmissionResult:
    """检查单个候选路径是否满足准入条件。

    Args:
        candidate: 候选路径
        baseline_path: baseline 文件路径
        overrides: pyproject.toml 中的 overrides 配置

    Returns:
        准入检查结果
    """
    result = AdmissionResult(candidate=candidate, passed=True)

    # 1. 检查 baseline 错误数
    error_count = count_baseline_errors(baseline_path, candidate)
    result.baseline_error_count = error_count

    if error_count > 0:
        result.passed = False
        result.errors.append(
            AdmissionError(
                candidate=candidate,
                error_type="baseline_errors",
                message=f"baseline 中存在 {error_count} 个错误",
                details={"error_count": error_count},
            )
        )

    # 2. 查找匹配的 override
    override = find_matching_override(overrides, candidate)
    result.has_override = override is not None

    if override is None:
        result.passed = False
        result.errors.append(
            AdmissionError(
                candidate=candidate,
                error_type="missing_override",
                message="pyproject.toml 中缺少对应的 [[tool.mypy.overrides]] 配置",
                details={"expected_module": path_to_module(candidate)},
            )
        )
    else:
        # 3. 检查 override 配置
        disallow_ok, ignore_ok, config_errors = check_override_config(override)
        result.disallow_untyped_defs = override.get("disallow_untyped_defs")
        result.ignore_missing_imports = override.get("ignore_missing_imports")

        if not disallow_ok or not ignore_ok:
            result.passed = False
            for err in config_errors:
                result.errors.append(
                    AdmissionError(
                        candidate=candidate,
                        error_type="config_error",
                        message=err,
                        details={"module": override.get("module")},
                    )
                )

    return result


def load_candidates_from_file(candidates_file: Path) -> list[str]:
    """从 JSON 文件加载候选路径列表。

    支持的格式:
    1. 简单列表: ["path1", "path2"]
    2. 带键的对象: {"candidates": ["path1", "path2"]}
    3. 带键的对象: {"strict_island_candidates": ["path1", "path2"]}
    """
    content = candidates_file.read_text(encoding="utf-8")
    data = json.loads(content)

    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        # 尝试多个可能的键
        for key in ["candidates", "strict_island_candidates", "paths"]:
            if key in data:
                return data[key]
        raise ValueError(
            "JSON 文件中未找到有效的候选路径列表，尝试的键: candidates, strict_island_candidates, paths"
        )
    else:
        raise ValueError("JSON 文件格式无效: 期望列表或对象")


def load_pyproject_overrides(pyproject_path: Path) -> list[dict[str, Any]]:
    """从 pyproject.toml 加载 mypy overrides 配置。"""
    content = pyproject_path.read_text(encoding="utf-8")
    data = tomllib.loads(content)

    tool = data.get("tool", {})
    mypy = tool.get("mypy", {})
    overrides = mypy.get("overrides", [])

    return overrides


def run_check(
    candidates: list[str],
    baseline_path: Path,
    pyproject_path: Path,
) -> CheckResult:
    """运行准入检查。

    Args:
        candidates: 候选路径列表
        baseline_path: baseline 文件路径
        pyproject_path: pyproject.toml 文件路径

    Returns:
        检查结果
    """
    result = CheckResult(ok=True, candidates_checked=len(candidates))

    # 检查 pyproject.toml 是否存在
    if not pyproject_path.exists():
        result.ok = False
        result.config_errors.append(f"pyproject.toml 不存在: {pyproject_path}")
        return result

    # 加载 overrides
    try:
        overrides = load_pyproject_overrides(pyproject_path)
    except Exception as e:
        result.ok = False
        result.config_errors.append(f"解析 pyproject.toml 失败: {e}")
        return result

    # 检查每个候选路径
    for candidate in candidates:
        admission_result = check_candidate(candidate, baseline_path, overrides)
        result.results.append(admission_result)

        if admission_result.passed:
            result.passed_count += 1
        else:
            result.failed_count += 1
            result.ok = False

    return result


# ============================================================================
# CLI
# ============================================================================


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Strict Island 准入检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 检查单个候选路径
    %(prog)s --candidate src/engram/gateway/foo.py

    # 检查多个候选路径
    %(prog)s --candidate src/engram/gateway/foo.py --candidate src/engram/logbook/bar.py

    # 从配置文件读取候选路径
    %(prog)s --candidates-file candidates.json

    # JSON 输出
    %(prog)s --candidate src/engram/gateway/foo.py --json
""",
    )

    # 候选路径参数（互斥组）
    parser.add_argument(
        "--candidate",
        action="append",
        dest="candidates",
        metavar="PATH",
        help="候选路径（可多次指定）",
    )
    parser.add_argument(
        "--candidates-file",
        type=Path,
        metavar="FILE",
        help="候选路径 JSON 文件",
    )

    # 配置文件路径
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=Path("scripts/ci/mypy_baseline.txt"),
        metavar="FILE",
        help="mypy baseline 文件路径 (默认: scripts/ci/mypy_baseline.txt)",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        metavar="FILE",
        help="pyproject.toml 文件路径 (默认: pyproject.toml)",
    )

    # 输出格式
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出",
    )

    return parser.parse_args()


def main() -> int:
    """主入口函数。"""
    args = parse_args()

    # 收集候选路径
    candidates: list[str] = []

    if args.candidates:
        candidates.extend(args.candidates)

    if args.candidates_file:
        if not args.candidates_file.exists():
            print(f"[ERROR] 候选路径文件不存在: {args.candidates_file}", file=sys.stderr)
            return 2
        try:
            file_candidates = load_candidates_from_file(args.candidates_file)
            candidates.extend(file_candidates)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[ERROR] 解析候选路径文件失败: {e}", file=sys.stderr)
            return 2

    if not candidates:
        print("[ERROR] 请指定至少一个候选路径（--candidate 或 --candidates-file）", file=sys.stderr)
        return 2

    # 运行检查
    result = run_check(
        candidates=candidates,
        baseline_path=args.baseline_file,
        pyproject_path=args.pyproject,
    )

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        # 人类可读格式
        if result.config_errors:
            print("[CONFIG ERROR]")
            for err in result.config_errors:
                print(f"  - {err}")
            print()

        print(f"检查 {result.candidates_checked} 个候选路径")
        print(f"  通过: {result.passed_count}")
        print(f"  失败: {result.failed_count}")
        print()

        for admission_result in result.results:
            status = "[PASS]" if admission_result.passed else "[FAIL]"
            print(f"{status} {admission_result.candidate}")

            if args.verbose or not admission_result.passed:
                print(f"       baseline 错误数: {admission_result.baseline_error_count}")
                print(f"       存在 override: {admission_result.has_override}")
                if admission_result.has_override:
                    print(f"       disallow_untyped_defs: {admission_result.disallow_untyped_defs}")
                    print(f"       ignore_missing_imports: {admission_result.ignore_missing_imports}")

                for err in admission_result.errors:
                    print(f"       [ERROR] {err.message}")

            print()

        if result.ok:
            print("[OK] 所有候选路径满足 Strict Island 准入条件")
        else:
            print("[FAIL] 存在不满足准入条件的路径")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
