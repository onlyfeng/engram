# -*- coding: utf-8 -*-
"""
Gateway 测试隔离策略合规性回归测试

检查内容:
1. no_singleton_reset opt-out 策略
   - 单元测试默认禁止使用 @pytest.mark.no_singleton_reset
   - 只有标记为集成/E2E 测试才允许使用:
     - 必须同时标记 @pytest.mark.gate_profile("full")
     - 或模块级 pytestmark 包含 gate_profile("full") 或 integration

2. sys.modules 直接写入策略
   - 不允许直接写入 sys.modules["engram.gateway..."]
   - 必须使用 tests/gateway/helpers/sys_modules_patch.py 中的 patch_sys_modules()
   - 白名单文件（允许直接写入）:
     - tests/gateway/helpers/*.py
     - tests/gateway/conftest.py
     - tests/gateway/test_sys_modules_patch_helper.py

此测试扫描 tests/gateway/ 目录，确保所有使用都符合策略。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple


class OptOutViolation(NamedTuple):
    """表示一个违规使用 no_singleton_reset 的测试"""

    file_path: str
    line_number: int
    function_name: str
    reason: str


class SysModulesViolation(NamedTuple):
    """表示一个违规直接写入 sys.modules 的操作"""

    file_path: str
    line_number: int
    reason: str


# sys.modules 直接写入白名单（相对于 tests/gateway/ 的路径）
SYS_MODULES_WRITE_ALLOWLIST = {
    "helpers/sys_modules_patch.py",
    "helpers/__init__.py",
    "conftest.py",
    "test_sys_modules_patch_helper.py",
}

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
]


def _get_gateway_tests_dir() -> Path:
    """获取 tests/gateway/ 目录路径"""
    return Path(__file__).parent


def _get_gateway_test_files() -> list[Path]:
    """获取 tests/gateway/ 下所有测试文件"""
    gateway_tests_dir = _get_gateway_tests_dir()
    return list(gateway_tests_dir.glob("test_*.py"))


def _get_all_python_files() -> list[Path]:
    """获取 tests/gateway/ 下所有 Python 文件（包括 helpers）"""
    gateway_tests_dir = _get_gateway_tests_dir()
    return list(gateway_tests_dir.glob("**/*.py"))


def _get_relative_path(file_path: Path) -> str:
    """获取相对于 tests/gateway/ 的路径"""
    gateway_tests_dir = _get_gateway_tests_dir()
    try:
        return str(file_path.relative_to(gateway_tests_dir))
    except ValueError:
        return str(file_path)


def _is_in_sys_modules_allowlist(file_path: Path) -> bool:
    """检查文件是否在 sys.modules 写入白名单中"""
    rel_path = _get_relative_path(file_path)
    return rel_path in SYS_MODULES_WRITE_ALLOWLIST


def _has_marker_in_decorators(
    decorators: list[ast.expr], marker_name: str, marker_args: list[str] | None = None
) -> bool:
    """
    检查装饰器列表中是否包含指定的 pytest.mark 标记

    Args:
        decorators: AST 装饰器节点列表
        marker_name: 要查找的 marker 名称（如 "no_singleton_reset", "gate_profile"）
        marker_args: 可选的 marker 参数值列表（如 ["full"] 表示 gate_profile("full")）

    Returns:
        True 如果找到匹配的 marker
    """
    for decorator in decorators:
        # 处理 @pytest.mark.xxx 形式
        if isinstance(decorator, ast.Attribute):
            if decorator.attr == marker_name:
                return marker_args is None  # 无参数 marker 匹配
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
    """
    检查装饰器是否包含 gate_profile("full") 或 integration 标记

    Returns:
        True 如果测试被标记为集成/E2E 测试
    """
    # 检查 gate_profile("full")
    if _has_marker_in_decorators(decorators, "gate_profile", ["full"]):
        return True
    # 检查 @pytest.mark.integration
    if _has_marker_in_decorators(decorators, "integration"):
        return True
    return False


def _parse_module_pytestmark(tree: ast.Module) -> list[str]:
    """
    解析模块级 pytestmark 变量中的 marker 名称

    支持的格式:
    - pytestmark = pytest.mark.xxx
    - pytestmark = [pytest.mark.xxx, pytest.mark.yyy]

    Returns:
        marker 名称列表（如 ["gate_profile", "integration"]）
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
    """
    检查模块级 pytestmark 是否表明这是一个集成/E2E 测试模块

    Returns:
        True 如果模块级标记表明这是集成测试模块
    """
    # 检查是否有 gate_profile (假设是 full) 或 integration
    return "gate_profile" in module_markers or "integration" in module_markers


def scan_opt_out_violations() -> list[OptOutViolation]:
    """
    扫描 tests/gateway/ 目录中所有不合规使用 no_singleton_reset 的测试

    Returns:
        违规列表
    """
    violations: list[OptOutViolation] = []

    for test_file in _get_gateway_test_files():
        # 跳过当前文件
        if test_file.name == "test_opt_out_policy_contract.py":
            continue

        try:
            source = test_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(test_file))
        except SyntaxError:
            continue

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
                        OptOutViolation(
                            file_path=str(test_file.relative_to(test_file.parent)),
                            line_number=node.lineno,
                            function_name=node.name,
                            reason=(
                                "使用 @pytest.mark.no_singleton_reset 但未标记为集成测试。"
                                "必须同时使用 @pytest.mark.gate_profile('full') 或 "
                                "@pytest.mark.integration，或在模块级 pytestmark 中声明。"
                            ),
                        )
                    )

    return violations


class TestOptOutPolicyContract:
    """no_singleton_reset opt-out 策略合规性测试"""

    def test_no_singleton_reset_requires_integration_marker(self):
        """
        验证所有使用 no_singleton_reset 的测试都有正确的集成测试标记

        策略:
        - 单元测试默认禁止使用 no_singleton_reset
        - 只有 @pytest.mark.gate_profile("full") 或
          @pytest.mark.integration 标记的测试才允许使用
        - 或模块级 pytestmark 包含上述标记
        """
        violations = scan_opt_out_violations()

        if violations:
            violation_messages = []
            for v in violations:
                violation_messages.append(
                    f"  - {v.file_path}:{v.line_number} {v.function_name}\n    原因: {v.reason}"
                )

            error_message = (
                f"发现 {len(violations)} 个 no_singleton_reset 策略违规:\n\n"
                + "\n".join(violation_messages)
                + "\n\n"
                "修复方法:\n"
                "1. 如果是单元测试，移除 @pytest.mark.no_singleton_reset\n"
                "2. 如果是集成测试，添加 @pytest.mark.gate_profile('full') 或 "
                "@pytest.mark.integration\n"
                "3. 如果整个模块都是集成测试，使用模块级 pytestmark"
            )
            raise AssertionError(error_message)

    def test_scan_function_returns_correct_structure(self):
        """验证扫描函数返回正确的数据结构"""
        violations = scan_opt_out_violations()
        # 验证返回类型
        assert isinstance(violations, list)
        for v in violations:
            assert isinstance(v, OptOutViolation)
            assert isinstance(v.file_path, str)
            assert isinstance(v.line_number, int)
            assert isinstance(v.function_name, str)
            assert isinstance(v.reason, str)


# ============================================================================
# sys.modules 直接写入策略检查
# ============================================================================


def scan_sys_modules_violations() -> list[SysModulesViolation]:
    """
    扫描 tests/gateway/ 目录中所有直接写入 sys.modules["engram.gateway..."] 的操作

    只检查不在白名单中的文件。

    Returns:
        违规列表
    """
    violations: list[SysModulesViolation] = []

    for py_file in _get_all_python_files():
        # 跳过白名单文件
        if _is_in_sys_modules_allowlist(py_file):
            continue

        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # 检查是否包含 engram.gateway 相关的 sys.modules 操作
        if "engram.gateway" not in content:
            continue

        # 逐行检查写入模式
        lines = content.split("\n")
        for line_num, line in enumerate(lines, start=1):
            # 跳过注释行
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            # 跳过文档字符串中的内容（简化检查）
            if '"""' in line or "'''" in line:
                continue

            for pattern in SYS_MODULES_WRITE_PATTERNS:
                if pattern.search(line):
                    violations.append(
                        SysModulesViolation(
                            file_path=_get_relative_path(py_file),
                            line_number=line_num,
                            reason=(
                                '直接写入 sys.modules["engram.gateway..."] 不允许。'
                                "请使用 tests/gateway/helpers/sys_modules_patch.py "
                                "中的 patch_sys_modules()。"
                            ),
                        )
                    )
                    break  # 每行只报告一次

    return violations


class TestSysModulesWritePolicy:
    """sys.modules 直接写入策略合规性测试"""

    def test_no_direct_sys_modules_write_to_engram_gateway(self):
        """
        验证非白名单文件不直接写入 sys.modules["engram.gateway..."]

        策略:
        - 不允许直接写入 sys.modules["engram.gateway..."]
        - 必须使用 patch_sys_modules() 上下文管理器

        白名单文件（允许直接写入）:
        - tests/gateway/helpers/*.py
        - tests/gateway/conftest.py
        - tests/gateway/test_sys_modules_patch_helper.py
        """
        violations = scan_sys_modules_violations()

        if violations:
            violation_messages = []
            for v in violations:
                violation_messages.append(
                    f"  - {v.file_path}:{v.line_number}\n    原因: {v.reason}"
                )

            error_message = (
                f"发现 {len(violations)} 个 sys.modules 直接写入违规:\n\n"
                + "\n".join(violation_messages)
                + "\n\n"
                "修复方法:\n"
                "使用 patch_sys_modules() 上下文管理器替代直接写入:\n"
                "    from tests.gateway.helpers import patch_sys_modules\n"
                "    \n"
                "    with patch_sys_modules(\n"
                '        replacements={"engram.gateway.xxx": mock_module},\n'
                '        remove=["engram.gateway.yyy"],\n'
                "    ):\n"
                "        # 测试代码\n"
                "        pass"
            )
            raise AssertionError(error_message)

    def test_scan_function_returns_correct_structure(self):
        """验证扫描函数返回正确的数据结构"""
        violations = scan_sys_modules_violations()
        # 验证返回类型
        assert isinstance(violations, list)
        for v in violations:
            assert isinstance(v, SysModulesViolation)
            assert isinstance(v.file_path, str)
            assert isinstance(v.line_number, int)
            assert isinstance(v.reason, str)

    def test_allowlist_files_exist(self):
        """验证白名单中的文件都存在"""
        gateway_tests_dir = _get_gateway_tests_dir()
        for allowlist_path in SYS_MODULES_WRITE_ALLOWLIST:
            full_path = gateway_tests_dir / allowlist_path
            assert full_path.exists(), (
                f"白名单文件不存在: {allowlist_path}\n"
                f"如果文件已删除，请更新 SYS_MODULES_WRITE_ALLOWLIST"
            )
