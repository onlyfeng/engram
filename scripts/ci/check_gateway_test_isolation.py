#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gateway 测试隔离策略检查

检查内容：
1. sys.modules 直接写入检查 - 只允许白名单文件直接写入 sys.modules["engram.gateway..."]
2. no_singleton_reset 使用策略检查 - 只允许在 integration/E2E + gate_profile("full") 的测试中使用

白名单（允许 sys.modules 直接写入）：
- tests/gateway/helpers/*.py - 官方 helper 文件
- tests/gateway/conftest.py - pytest 共享 fixture
- tests/gateway/test_sys_modules_patch_helper.py - helper 的测试

用法：
    python scripts/ci/check_gateway_test_isolation.py [--verbose]

退出码：
    0 - 检查通过
    1 - 发现违规
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import NamedTuple

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY_TESTS_DIR = PROJECT_ROOT / "tests" / "gateway"

# sys.modules 直接写入白名单（相对于 tests/gateway/ 的路径）
SYS_MODULES_WRITE_ALLOWLIST = {
    "helpers/sys_modules_patch.py",
    "helpers/__init__.py",
    "conftest.py",
    "test_sys_modules_patch_helper.py",
}

# 允许的 sys.modules 写入模式（非 engram.gateway 前缀的可以忽略）
# 我们只检查 sys.modules["engram.gateway... 的写入
SYS_MODULES_ENGRAM_PATTERN = re.compile(
    r'sys\.modules\s*\[\s*["\']engram\.gateway',
    re.MULTILINE,
)

# sys.modules 写入模式（包含赋值和删除）
SYS_MODULES_WRITE_PATTERNS = [
    # 直接赋值: sys.modules["engram.gateway..."] = ...
    re.compile(
        r'sys\.modules\s*\[\s*["\']engram\.gateway[^"\']*["\']\s*\]\s*=',
        re.MULTILINE,
    ),
    # 删除: del sys.modules["engram.gateway..."]
    re.compile(
        r'del\s+sys\.modules\s*\[\s*["\']engram\.gateway',
        re.MULTILINE,
    ),
    # 使用变量形式删除（如 for 循环中的 del sys.modules[m]）
    # 这种情况需要通过 AST 来检测，但我们先用简单的模式匹配
]


class Violation(NamedTuple):
    """表示一个违规"""

    file_path: str
    line_number: int
    violation_type: str
    message: str


def get_gateway_test_files() -> list[Path]:
    """获取 tests/gateway/ 下所有 Python 文件"""
    return list(GATEWAY_TESTS_DIR.glob("**/*.py"))


def get_relative_path(file_path: Path) -> str:
    """获取相对于 tests/gateway/ 的路径"""
    try:
        return str(file_path.relative_to(GATEWAY_TESTS_DIR))
    except ValueError:
        return str(file_path)


def is_in_allowlist(file_path: Path) -> bool:
    """检查文件是否在白名单中"""
    rel_path = get_relative_path(file_path)
    return rel_path in SYS_MODULES_WRITE_ALLOWLIST


def check_sys_modules_direct_write(file_path: Path) -> list[Violation]:
    """
    检查文件中是否有 sys.modules 直接写入 engram.gateway 模块的操作

    只检查不在白名单中的文件。

    Returns:
        违规列表
    """
    violations: list[Violation] = []

    if is_in_allowlist(file_path):
        return violations

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return violations

    # 检查是否包含 engram.gateway 相关的 sys.modules 操作
    if not SYS_MODULES_ENGRAM_PATTERN.search(content):
        return violations

    # 逐行检查写入模式
    lines = content.split("\n")
    for line_num, line in enumerate(lines, start=1):
        # 跳过注释行
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        # 跳过文档字符串中的内容（简化检查：包含三引号的行）
        if '"""' in line or "'''" in line:
            continue

        for pattern in SYS_MODULES_WRITE_PATTERNS:
            if pattern.search(line):
                violations.append(
                    Violation(
                        file_path=get_relative_path(file_path),
                        line_number=line_num,
                        violation_type="SYS_MODULES_DIRECT_WRITE",
                        message=(
                            "直接写入 sys.modules[\"engram.gateway...\"] 不允许。"
                            "请使用 tests/gateway/helpers/sys_modules_patch.py 中的 patch_sys_modules()。"
                        ),
                    )
                )
                break  # 每行只报告一次

    return violations


def _has_marker_in_decorators(
    decorators: list[ast.expr], marker_name: str, marker_args: list[str] | None = None
) -> bool:
    """
    检查装饰器列表中是否包含指定的 pytest.mark 标记

    Args:
        decorators: AST 装饰器节点列表
        marker_name: 要查找的 marker 名称
        marker_args: 可选的 marker 参数值列表

    Returns:
        True 如果找到匹配的 marker
    """
    for decorator in decorators:
        # 处理 @pytest.mark.xxx 形式
        if isinstance(decorator, ast.Attribute):
            if decorator.attr == marker_name:
                return marker_args is None
        # 处理 @pytest.mark.xxx(...) 形式
        elif isinstance(decorator, ast.Call):
            func = decorator.func
            if isinstance(func, ast.Attribute) and func.attr == marker_name:
                if marker_args is None:
                    return True
                # 检查参数值
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and arg.value in marker_args:
                        return True
    return False


def _has_gate_profile_full_or_integration(decorators: list[ast.expr]) -> bool:
    """检查装饰器是否包含 gate_profile("full") 或 integration 标记"""
    if _has_marker_in_decorators(decorators, "gate_profile", ["full"]):
        return True
    if _has_marker_in_decorators(decorators, "integration"):
        return True
    return False


def _parse_module_pytestmark(tree: ast.Module) -> list[str]:
    """
    解析模块级 pytestmark 变量中的 marker 名称

    Returns:
        marker 名称列表
    """
    markers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    value = node.value
                    # 单个 marker: pytestmark = pytest.mark.xxx
                    if isinstance(value, ast.Attribute):
                        markers.append(value.attr)
                    # marker 列表: pytestmark = [pytest.mark.xxx, ...]
                    elif isinstance(value, ast.List):
                        for elt in value.elts:
                            if isinstance(elt, ast.Attribute):
                                markers.append(elt.attr)
                            elif isinstance(elt, ast.Call) and isinstance(elt.func, ast.Attribute):
                                markers.append(elt.func.attr)
    return markers


def _check_module_level_compliance(module_markers: list[str]) -> bool:
    """检查模块级 pytestmark 是否表明这是一个集成/E2E 测试模块"""
    return "gate_profile" in module_markers or "integration" in module_markers


def check_no_singleton_reset_usage(file_path: Path) -> list[Violation]:
    """
    检查文件中 no_singleton_reset 的使用是否合规

    策略规则：
    - 只有标记为集成/E2E 测试才允许使用 no_singleton_reset
    - 必须同时标记 @pytest.mark.gate_profile("full") 或
    - 模块级 pytestmark 包含 gate_profile("full") 或 integration

    Returns:
        违规列表
    """
    violations: list[Violation] = []

    # 跳过非测试文件
    if not file_path.name.startswith("test_"):
        return violations

    # 跳过本检查的契约测试文件
    if file_path.name == "test_opt_out_policy_contract.py":
        return violations

    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return violations

    # 解析模块级 pytestmark
    module_markers = _parse_module_pytestmark(tree)
    module_is_integration = _check_module_level_compliance(module_markers)

    # 遍历所有函数定义
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            decorators = node.decorator_list

            # 检查是否使用了 no_singleton_reset
            has_opt_out = _has_marker_in_decorators(decorators, "no_singleton_reset")
            if not has_opt_out:
                continue

            # 检查是否是合规的集成/E2E 测试
            has_integration_marker = _has_gate_profile_full_or_integration(decorators)

            # 合规条件：函数级有 integration/gate_profile(full) 或 模块级有
            is_compliant = has_integration_marker or module_is_integration

            if not is_compliant:
                violations.append(
                    Violation(
                        file_path=get_relative_path(file_path),
                        line_number=node.lineno,
                        violation_type="NO_SINGLETON_RESET_POLICY",
                        message=(
                            f"函数 {node.name} 使用 @pytest.mark.no_singleton_reset 但未标记为集成测试。"
                            f"必须同时使用 @pytest.mark.gate_profile('full') 或 "
                            f"@pytest.mark.integration，或在模块级 pytestmark 中声明。"
                        ),
                    )
                )

    return violations


def run_all_checks(verbose: bool = False) -> tuple[list[Violation], dict]:
    """
    运行所有检查

    Returns:
        (violations, stats) - 违规列表和统计信息
    """
    all_violations: list[Violation] = []
    stats = {
        "files_checked": 0,
        "sys_modules_violations": 0,
        "no_singleton_reset_violations": 0,
    }

    test_files = get_gateway_test_files()
    stats["files_checked"] = len(test_files)

    for file_path in test_files:
        # 检查 sys.modules 直接写入
        sys_modules_violations = check_sys_modules_direct_write(file_path)
        stats["sys_modules_violations"] += len(sys_modules_violations)
        all_violations.extend(sys_modules_violations)

        # 检查 no_singleton_reset 使用
        reset_violations = check_no_singleton_reset_usage(file_path)
        stats["no_singleton_reset_violations"] += len(reset_violations)
        all_violations.extend(reset_violations)

        if verbose and (sys_modules_violations or reset_violations):
            print(f"  {get_relative_path(file_path)}: "
                  f"{len(sys_modules_violations)} sys.modules, "
                  f"{len(reset_violations)} no_singleton_reset")

    return all_violations, stats


def main() -> int:
    """主入口"""
    parser = argparse.ArgumentParser(
        description="检查 Gateway 测试隔离策略",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
检查内容：
  1. sys.modules 直接写入检查
     - 不允许直接写入 sys.modules["engram.gateway..."]
     - 必须使用 helpers/sys_modules_patch.py 中的 patch_sys_modules()

  2. no_singleton_reset 使用策略检查
     - 只允许在 integration/E2E + gate_profile("full") 的测试中使用
     - 必须有 @pytest.mark.integration 或 @pytest.mark.gate_profile("full")

白名单（允许 sys.modules 直接写入）：
  - tests/gateway/helpers/*.py
  - tests/gateway/conftest.py
  - tests/gateway/test_sys_modules_patch_helper.py
""",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细输出",
    )
    args = parser.parse_args()

    print("检查 Gateway 测试隔离策略...")
    print(f"  目录: {GATEWAY_TESTS_DIR}")
    print()

    violations, stats = run_all_checks(verbose=args.verbose)

    print("检查完成:")
    print(f"  - 文件数: {stats['files_checked']}")
    print(f"  - sys.modules 直接写入违规: {stats['sys_modules_violations']}")
    print(f"  - no_singleton_reset 策略违规: {stats['no_singleton_reset_violations']}")
    print()

    if violations:
        print(f"发现 {len(violations)} 个违规:")
        print()

        # 按类型分组输出
        by_type: dict[str, list[Violation]] = {}
        for v in violations:
            by_type.setdefault(v.violation_type, []).append(v)

        for vtype, vlist in sorted(by_type.items()):
            print(f"=== {vtype} ({len(vlist)} 个) ===")
            for v in vlist:
                print(f"  {v.file_path}:{v.line_number}")
                print(f"    {v.message}")
            print()

        print("修复方法:")
        print("  1. sys.modules 直接写入：使用 patch_sys_modules() 上下文管理器")
        print("     from tests.gateway.helpers import patch_sys_modules")
        print("     with patch_sys_modules(replacements={...}, remove=[...]):")
        print("         ...")
        print()
        print("  2. no_singleton_reset 违规：添加集成测试标记")
        print("     @pytest.mark.gate_profile('full')")
        print("     @pytest.mark.no_singleton_reset")
        print("     def test_xxx(): ...")
        print()

        return 1

    print("[OK] Gateway 测试隔离策略检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
