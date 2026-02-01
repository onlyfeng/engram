"""
Gateway correlation_id 单一来源门禁测试

测试 scripts/ci/check_gateway_correlation_id_single_source.py 的功能，确保：
1. 正例：实际的 src/engram/gateway/ 目录通过检查
2. 负例：重复定义 generate_correlation_id 等函数会被检测
3. 负例：使用 uuid.uuid4().hex[:16] 等实现片段会被检测

与 CI job 对应关系：
- 本测试对应 CI workflow 中的 gateway-correlation-id-single-source job
- 门禁脚本路径: scripts/ci/check_gateway_correlation_id_single_source.py
- 被检查目录: src/engram/gateway/**/*.py

运行方式：
    pytest tests/ci/test_gateway_correlation_id_single_source_gate.py -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_gateway_correlation_id_single_source import (
    FORBIDDEN_FUNCTION_DEFINITIONS,
    FORBIDDEN_PATTERNS,
    SCAN_DIRECTORY,
    SINGLE_SOURCE_FILE,
    find_function_definitions,
    scan_file,
    scan_gateway_directory,
    verify_single_source_file,
)
from tests.ci.helpers.subprocess_env import get_subprocess_env

# ============================================================================
# 辅助函数
# ============================================================================


def create_temp_file(content: str) -> Path:
    """创建临时 Python 文件并返回路径"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(content)
        return Path(f.name)


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent.resolve()


# ============================================================================
# 正例测试：验证实际的 Gateway 目录通过检查
# ============================================================================


class TestRealGatewayPassesCheck:
    """验证实际的 src/engram/gateway 目录通过检查"""

    def test_real_gateway_passes_check(self) -> None:
        """实际的 Gateway 目录应通过 correlation_id 单一来源检查"""
        project_root = get_project_root()

        result = scan_gateway_directory(project_root)

        assert not result.has_violations(), (
            f"Gateway 目录不应有 correlation_id 单一来源违规，"
            f"但发现 {len(result.violations)} 处违规: "
            f"{[(v.file, v.pattern_name, v.message) for v in result.violations]}"
        )

    def test_single_source_file_exists(self) -> None:
        """单一来源文件 correlation_id.py 应存在"""
        project_root = get_project_root()
        file_path = project_root / SCAN_DIRECTORY / SINGLE_SOURCE_FILE

        assert file_path.exists(), f"单一来源文件不存在: {file_path}"

    def test_single_source_file_valid(self) -> None:
        """单一来源文件应包含必要的函数定义"""
        project_root = get_project_root()

        exists, valid = verify_single_source_file(project_root)

        assert exists, "单一来源文件不存在"
        assert valid, "单一来源文件缺少必要的函数定义或常量"

    def test_single_source_file_has_required_functions(self) -> None:
        """单一来源文件应定义所有必要的函数"""
        project_root = get_project_root()
        file_path = project_root / SCAN_DIRECTORY / SINGLE_SOURCE_FILE

        content = file_path.read_text()
        func_defs = find_function_definitions(content)
        defined_funcs = {name for name, _ in func_defs}

        for func_name in FORBIDDEN_FUNCTION_DEFINITIONS:
            assert func_name in defined_funcs, f"单一来源文件应定义 {func_name}()"

    def test_single_source_file_has_pattern_constant(self) -> None:
        """单一来源文件应定义 CORRELATION_ID_PATTERN 常量"""
        project_root = get_project_root()
        file_path = project_root / SCAN_DIRECTORY / SINGLE_SOURCE_FILE

        content = file_path.read_text()

        assert "CORRELATION_ID_PATTERN" in content, "单一来源文件应定义 CORRELATION_ID_PATTERN 常量"


class TestScriptSubprocess:
    """测试通过子进程运行脚本"""

    def test_script_exits_zero_for_real_gateway(self) -> None:
        """脚本检查实际 Gateway 目录应返回 0（成功）"""
        project_root = get_project_root()
        script_path = (
            project_root / "scripts" / "ci" / "check_gateway_correlation_id_single_source.py"
        )

        result = subprocess.run(
            [sys.executable, str(script_path), "--project-root", str(project_root)],
            capture_output=True,
            text=True,
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        assert result.returncode == 0, (
            f"脚本应返回 0，但返回 {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_script_json_output_ok_true(self) -> None:
        """脚本 JSON 输出应显示 ok=true"""
        project_root = get_project_root()
        script_path = (
            project_root / "scripts" / "ci" / "check_gateway_correlation_id_single_source.py"
        )

        result = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--project-root",
                str(project_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        assert result.returncode == 0

        import json

        output = json.loads(result.stdout)
        assert output["ok"] is True, f"JSON 输出应显示 ok=true: {output}"
        assert output["violation_count"] == 0


# ============================================================================
# 负例测试：验证重复定义会被检测
# ============================================================================


class TestFunctionDefinitionDetection:
    """验证重复函数定义会被检测（负例测试）"""

    def test_generate_correlation_id_definition_detected(self) -> None:
        """重复定义 generate_correlation_id 应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

import uuid


def generate_correlation_id() -> str:
    """重复定义，应被检测"""
    return f"corr-{uuid.uuid4().hex[:16]}"
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            # 应检测到函数定义违规
            func_def_violations = [
                v for v in violations if v.violation_type == "function_definition"
            ]
            assert len(func_def_violations) >= 1
            assert any(v.pattern_name == "generate_correlation_id" for v in func_def_violations)
        finally:
            file_path.unlink()

    def test_is_valid_correlation_id_definition_detected(self) -> None:
        """重复定义 is_valid_correlation_id 应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

import re


def is_valid_correlation_id(corr_id: str) -> bool:
    """重复定义，应被检测"""
    return bool(re.match(r"^corr-[a-fA-F0-9]{16}$", corr_id))
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            func_def_violations = [
                v for v in violations if v.violation_type == "function_definition"
            ]
            assert len(func_def_violations) >= 1
            assert any(v.pattern_name == "is_valid_correlation_id" for v in func_def_violations)
        finally:
            file_path.unlink()

    def test_normalize_correlation_id_definition_detected(self) -> None:
        """重复定义 normalize_correlation_id 应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations


def normalize_correlation_id(corr_id: str) -> str:
    """重复定义，应被检测"""
    if corr_id and corr_id.startswith("corr-"):
        return corr_id
    return "corr-0000000000000000"
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            func_def_violations = [
                v for v in violations if v.violation_type == "function_definition"
            ]
            assert len(func_def_violations) >= 1
            assert any(v.pattern_name == "normalize_correlation_id" for v in func_def_violations)
        finally:
            file_path.unlink()


class TestForbiddenPatternDetection:
    """验证禁止的实现模式会被检测（负例测试）"""

    def test_uuid4_hex_slice_detected(self) -> None:
        """使用 uuid.uuid4().hex[:16] 应被检测"""
        content = '''\
"""Test module"""
from __future__ import annotations

import uuid


def make_id() -> str:
    """使用禁止的实现模式"""
    return f"corr-{uuid.uuid4().hex[:16]}"
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            pattern_violations = [v for v in violations if v.violation_type == "forbidden_pattern"]
            assert len(pattern_violations) >= 1
            assert any(v.pattern_name == "uuid4_hex_slice" for v in pattern_violations)
        finally:
            file_path.unlink()


# ============================================================================
# 允许情况测试
# ============================================================================


class TestAllowedPatterns:
    """验证允许的使用模式不会触发违规"""

    def test_import_from_correlation_id_allowed(self) -> None:
        """从 correlation_id.py 导入应被允许"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 这是允许的：从单一来源导入
from .correlation_id import generate_correlation_id, is_valid_correlation_id

__all__ = ["generate_correlation_id", "is_valid_correlation_id"]
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations, f"从 correlation_id 导入不应触发违规: {violations}"
        finally:
            file_path.unlink()

    def test_reexport_with_as_allowed(self) -> None:
        """带 as 的 re-export 应被允许"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 这是允许的：re-export
from .correlation_id import generate_correlation_id as generate_correlation_id
from .correlation_id import CORRELATION_ID_PATTERN as CORRELATION_ID_PATTERN
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations, f"re-export 形式不应触发违规: {violations}"
        finally:
            file_path.unlink()

    def test_allow_marker_bypasses_check(self) -> None:
        """带有 ALLOW 标记的行应被豁免"""
        # 注意：ALLOW 标记只豁免当前行和下一行，每个违规行都需要单独标记
        content = '''\
"""Test module"""
from __future__ import annotations

import uuid


# CORRELATION-ID-SINGLE-SOURCE-ALLOW: legacy compatibility
def generate_correlation_id() -> str:
    # CORRELATION-ID-SINGLE-SOURCE-ALLOW: implementation detail
    return f"corr-{uuid.uuid4().hex[:16]}"
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations, f"带 ALLOW 标记的行不应触发违规: {violations}"
        finally:
            file_path.unlink()

    def test_type_checking_block_allowed(self) -> None:
        """TYPE_CHECKING 块内的内容应被允许"""
        content = '''\
"""Test module"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 类型注解中提到这些函数名是允许的
    from .correlation_id import generate_correlation_id

def use_correlation_id() -> None:
    pass
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations, f"TYPE_CHECKING 块不应触发违规: {violations}"
        finally:
            file_path.unlink()

    def test_docstring_content_allowed(self) -> None:
        """文档字符串中的内容应被允许"""
        content = '''\
"""
Test module

Example:
    # 文档中提到 uuid.uuid4().hex[:16] 是允许的
    corr_id = generate_correlation_id()
"""
from __future__ import annotations


def some_function() -> None:
    """
    This function uses correlation_id.

    Note: The format is corr-{uuid.uuid4().hex[:16]}
    """
    pass
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations, f"文档字符串内容不应触发违规: {violations}"
        finally:
            file_path.unlink()

    def test_comment_line_allowed(self) -> None:
        """注释行应被允许"""
        content = '''\
"""Test module"""
from __future__ import annotations

# 注释中提到 uuid.uuid4().hex[:16] 是允许的
# def generate_correlation_id(): ...

def some_function() -> None:
    pass
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations, f"注释行不应触发违规: {violations}"
        finally:
            file_path.unlink()


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_file(self) -> None:
        """空文件不应报错"""
        content = ""
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations
        finally:
            file_path.unlink()

    def test_file_without_imports(self) -> None:
        """没有导入的文件不应报错"""
        content = '''\
"""Test module"""

__version__ = "1.0.0"

def some_function() -> None:
    pass
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert not violations
        finally:
            file_path.unlink()

    def test_result_to_dict(self) -> None:
        """CheckResult.to_dict() 应返回正确的格式"""
        content = '''\
"""Test module"""
from __future__ import annotations

import uuid


def generate_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex[:16]}"
'''
        file_path = create_temp_file(content)
        try:
            violations = scan_file(file_path, "test_module.py")

            assert len(violations) >= 1

            # 验证违规记录有正确的字段
            v = violations[0]
            assert hasattr(v, "file")
            assert hasattr(v, "line_number")
            assert hasattr(v, "violation_type")
            assert hasattr(v, "pattern_name")
            assert hasattr(v, "message")
        finally:
            file_path.unlink()


# ============================================================================
# 配置验证测试
# ============================================================================


class TestConfiguration:
    """验证配置正确性"""

    def test_forbidden_patterns_compile(self) -> None:
        """所有禁止模式的正则表达式应能编译"""
        import re

        for pattern_regex, pattern_name, _ in FORBIDDEN_PATTERNS:
            try:
                re.compile(pattern_regex)
            except re.error as e:
                pytest.fail(f"正则表达式编译失败: {pattern_name} - {e}")

    def test_scan_directory_exists(self) -> None:
        """扫描目录配置应指向存在的目录"""
        project_root = get_project_root()
        scan_dir = project_root / SCAN_DIRECTORY

        assert scan_dir.exists(), f"扫描目录不存在: {scan_dir}"
        assert scan_dir.is_dir(), f"扫描路径不是目录: {scan_dir}"

    def test_single_source_file_path_valid(self) -> None:
        """单一来源文件路径配置应有效"""
        project_root = get_project_root()
        file_path = project_root / SCAN_DIRECTORY / SINGLE_SOURCE_FILE

        assert file_path.exists(), f"单一来源文件不存在: {file_path}"


# ============================================================================
# 实际模块验证测试
# ============================================================================


class TestActualModulesUseCorrectImport:
    """验证实际模块都从 correlation_id.py 正确导入"""

    @pytest.mark.parametrize(
        "module_path",
        [
            "src/engram/gateway/mcp_rpc.py",
            "src/engram/gateway/di.py",
            "src/engram/gateway/dependencies.py",
            "src/engram/gateway/middleware.py",
        ],
    )
    def test_module_imports_from_correlation_id(self, module_path: str) -> None:
        """各模块应从 correlation_id.py 导入"""
        project_root = get_project_root()
        file_path = project_root / module_path

        if not file_path.exists():
            pytest.skip(f"文件不存在: {file_path}")

        content = file_path.read_text()

        # 检查是否有从 correlation_id 导入的语句
        has_correct_import = (
            "from .correlation_id import" in content
            or "from engram.gateway.correlation_id import" in content
        )

        # 如果文件中使用了 correlation_id 相关功能，应该有正确的导入
        uses_correlation_id = any(
            func_name in content for func_name in FORBIDDEN_FUNCTION_DEFINITIONS
        )

        if uses_correlation_id:
            assert has_correct_import, (
                f"{module_path} 使用了 correlation_id 相关功能但未从 correlation_id.py 导入"
            )
