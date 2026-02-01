#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gateway App 导入策略检查

检查内容：
1. 扫描 tests/gateway/ 中直接导入 engram.gateway.main.app 的用法
2. 要求同时使用 gateway_test_app fixture 或标注 gateway_app_import_allowed marker

策略说明：
- 禁止直接导入 from engram.gateway.main import app
- 应使用 gateway_test_app fixture（conftest.py 提供）
- 例外：测试导入行为本身的测试可使用 @pytest.mark.gateway_app_import_allowed

白名单（允许直接导入 app）：
- tests/gateway/test_import_safe_entrypoints.py - 测试模块导入行为

用法：
    python scripts/ci/check_gateway_app_import_policy.py [--verbose]

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

# 白名单（相对于 tests/gateway/ 的路径）
# 这些文件允许直接导入 engram.gateway.main.app
APP_IMPORT_ALLOWLIST = {
    # 测试模块导入行为的测试文件
    "test_import_safe_entrypoints.py",
    # 在文档注释中引用的文件（非实际导入）
    "conftest.py",
    # MinIO Audit Webhook 测试：需要特殊的 mock_config 和 mock_db_insert 配置
    # 这些测试 mock 了 get_config() 返回特定配置，不适合使用 gateway_test_container
    "test_minio_audit_webhook.py",
    # ImportError 可选依赖契约测试：测试依赖缺失时的行为
    # 需要直接控制 sys.modules 和 app 创建顺序
    "test_importerror_optional_deps_contract.py",
}

# 导入模式匹配
APP_IMPORT_PATTERNS = [
    # from engram.gateway.main import app
    re.compile(
        r"from\s+engram\.gateway\.main\s+import\s+.*\bapp\b",
        re.MULTILINE,
    ),
    # import engram.gateway.main（然后使用 .app）
    re.compile(
        r"import\s+engram\.gateway\.main\b",
        re.MULTILINE,
    ),
]


class Violation(NamedTuple):
    """表示一个违规"""

    file_path: str
    line_number: int
    violation_type: str
    message: str


def get_gateway_test_files() -> list[Path]:
    """获取 tests/gateway/ 下所有 Python 测试文件"""
    return [f for f in GATEWAY_TESTS_DIR.glob("**/*.py") if f.name.startswith("test_")]


def get_relative_path(file_path: Path) -> str:
    """获取相对于 tests/gateway/ 的路径"""
    try:
        return str(file_path.relative_to(GATEWAY_TESTS_DIR))
    except ValueError:
        return str(file_path)


def is_in_allowlist(file_path: Path) -> bool:
    """检查文件是否在白名单中"""
    rel_path = get_relative_path(file_path)
    return rel_path in APP_IMPORT_ALLOWLIST


def _has_marker_in_decorators(
    decorators: list[ast.expr], marker_name: str
) -> bool:
    """
    检查装饰器列表中是否包含指定的 pytest.mark 标记

    Args:
        decorators: AST 装饰器节点列表
        marker_name: 要查找的 marker 名称

    Returns:
        True 如果找到匹配的 marker
    """
    for decorator in decorators:
        # 处理 @pytest.mark.xxx 形式
        if isinstance(decorator, ast.Attribute):
            if decorator.attr == marker_name:
                return True
        # 处理 @pytest.mark.xxx(...) 形式
        elif isinstance(decorator, ast.Call):
            func = decorator.func
            if isinstance(func, ast.Attribute) and func.attr == marker_name:
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


def _function_uses_fixture(node: ast.FunctionDef, fixture_name: str) -> bool:
    """
    检查函数是否使用了指定的 fixture（通过参数名）

    Args:
        node: 函数定义 AST 节点
        fixture_name: fixture 名称

    Returns:
        True 如果函数参数中包含该 fixture
    """
    for arg in node.args.args:
        if arg.arg == fixture_name:
            return True
    return False


def _find_class_containing_function(tree: ast.Module, func_node: ast.FunctionDef) -> ast.ClassDef | None:
    """查找包含指定函数的类"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if item is func_node:
                    return node
    return None


def _class_has_fixture_method(cls_node: ast.ClassDef, fixture_name: str) -> bool:
    """检查类中是否有返回指定 fixture 的 fixture 方法"""
    for item in cls_node.body:
        if isinstance(item, ast.FunctionDef):
            # 检查方法是否有 @pytest.fixture 装饰器
            is_fixture = False
            for decorator in item.decorator_list:
                if isinstance(decorator, ast.Attribute) and decorator.attr == "fixture":
                    is_fixture = True
                elif isinstance(decorator, ast.Call):
                    func = decorator.func
                    if isinstance(func, ast.Attribute) and func.attr == "fixture":
                        is_fixture = True
            
            if is_fixture:
                # 检查 fixture 方法是否使用了目标 fixture
                if _function_uses_fixture(item, fixture_name):
                    return True
    return False


def check_app_import_policy(file_path: Path) -> list[Violation]:
    """
    检查文件中是否有违规的 app 导入

    策略：
    1. 如果文件在白名单中，跳过
    2. 如果文件导入了 engram.gateway.main.app
       - 且测试函数/类使用了 gateway_test_app fixture → 允许
       - 且测试函数/类有 gateway_app_import_allowed marker → 允许
       - 否则 → 违规

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

    # 检查是否有 app 导入
    has_app_import = False
    import_lines: list[tuple[int, str]] = []
    
    lines = content.split("\n")
    for line_num, line in enumerate(lines, start=1):
        # 跳过注释行
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        
        # 跳过文档字符串中的内容
        if '"""' in line or "'''" in line:
            continue

        for pattern in APP_IMPORT_PATTERNS:
            if pattern.search(line):
                has_app_import = True
                import_lines.append((line_num, line.strip()))
                break

    if not has_app_import:
        return violations

    # 解析 AST
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return violations

    # 解析模块级 pytestmark
    module_markers = _parse_module_pytestmark(tree)
    module_has_allowed_marker = "gateway_app_import_allowed" in module_markers

    if module_has_allowed_marker:
        return violations  # 模块级豁免

    # 检查是否所有使用 app 的测试函数/类都有合规的方式
    # 简化检查：如果文件有 app 导入，且没有模块级豁免，
    # 则检查是否有任何测试函数使用了 gateway_test_app fixture 或有 allowed marker

    has_compliant_usage = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # 检查类是否有 allowed marker 或使用了 fixture
            class_has_marker = _has_marker_in_decorators(
                node.decorator_list, "gateway_app_import_allowed"
            )
            class_uses_fixture = _class_has_fixture_method(node, "gateway_test_app")
            
            if class_has_marker or class_uses_fixture:
                has_compliant_usage = True
                
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            # 检查测试函数
            has_marker = _has_marker_in_decorators(
                node.decorator_list, "gateway_app_import_allowed"
            )
            uses_fixture = _function_uses_fixture(node, "gateway_test_app")
            
            if has_marker or uses_fixture:
                has_compliant_usage = True

    # 如果有合规的使用方式，不报告违规
    # 注意：这是一个宽松的策略，只要文件中有任何合规使用，就认为文件符合要求
    # 更严格的策略可以检查每个导入 app 的位置
    if has_compliant_usage:
        return violations

    # 报告违规
    for line_num, line_content in import_lines:
        violations.append(
            Violation(
                file_path=get_relative_path(file_path),
                line_number=line_num,
                violation_type="GATEWAY_APP_IMPORT_POLICY",
                message=(
                    f"直接导入 engram.gateway.main.app 不推荐。\n"
                    f"    请使用 gateway_test_app fixture，或添加 "
                    f"@pytest.mark.gateway_app_import_allowed marker。\n"
                    f"    导入行: {line_content}"
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
        "app_import_violations": 0,
    }

    test_files = get_gateway_test_files()
    stats["files_checked"] = len(test_files)

    for file_path in test_files:
        violations = check_app_import_policy(file_path)
        stats["app_import_violations"] += len(violations)
        all_violations.extend(violations)

        if verbose and violations:
            print(f"  {get_relative_path(file_path)}: {len(violations)} 个违规")

    return all_violations, stats


def main() -> int:
    """主入口"""
    parser = argparse.ArgumentParser(
        description="检查 Gateway App 导入策略",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
检查内容：
  扫描 tests/gateway/ 中直接导入 engram.gateway.main.app 的用法，
  要求同时使用 gateway_test_app fixture 或标注 gateway_app_import_allowed marker。

策略说明：
  - 禁止直接导入 from engram.gateway.main import app
  - 应使用 gateway_test_app fixture（conftest.py 提供）
  - 例外：测试导入行为本身的测试可使用 @pytest.mark.gateway_app_import_allowed

白名单（允许直接导入 app）：
  - tests/gateway/test_import_safe_entrypoints.py

修复方法：
  1. 使用 gateway_test_app fixture:
     def test_xxx(gateway_test_app):
         response = gateway_test_app.get("/health")
         assert response.status_code == 200

  2. 添加允许 marker（仅用于测试导入行为）:
     @pytest.mark.gateway_app_import_allowed
     def test_import_behavior():
         from engram.gateway.main import app
         assert app is not None
""",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细输出",
    )
    args = parser.parse_args()

    print("检查 Gateway App 导入策略...")
    print(f"  目录: {GATEWAY_TESTS_DIR}")
    print()

    violations, stats = run_all_checks(verbose=args.verbose)

    print("检查完成:")
    print(f"  - 测试文件数: {stats['files_checked']}")
    print(f"  - App 导入策略违规: {stats['app_import_violations']}")
    print()

    if violations:
        print(f"发现 {len(violations)} 个违规:")
        print()

        for v in violations:
            print(f"  {v.file_path}:{v.line_number}")
            print(f"    {v.message}")
            print()

        print("修复方法:")
        print("  1. 使用 gateway_test_app fixture（推荐）:")
        print("     def test_xxx(gateway_test_app):")
        print('         response = gateway_test_app.get("/health")')
        print()
        print("  2. 如需访问 fake 依赖，同时使用两个 fixture:")
        print("     def test_xxx(gateway_test_app, gateway_test_container):")
        print('         gateway_test_container["adapter"].configure_xxx()')
        print('         response = gateway_test_app.post("/mcp", ...)')
        print()
        print("  3. 测试导入行为时添加 marker:")
        print("     @pytest.mark.gateway_app_import_allowed")
        print("     def test_import_safe(): ...")
        print()

        return 1

    print("[OK] Gateway App 导入策略检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
