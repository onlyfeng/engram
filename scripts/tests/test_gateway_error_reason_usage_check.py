#!/usr/bin/env python3
"""
check_gateway_error_reason_usage.py 单元测试

覆盖场景：
1. 检测硬编码 reason="..." 字符串
2. 允许 ErrorReason.X 常量使用
3. 白名单场景：测试文件、文档字符串、ErrorReason 类定义、私有常量
"""

import sys
import tempfile
from pathlib import Path

import pytest

# 将 scripts/ci 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent / "ci"))

from check_gateway_error_reason_usage import (
    REASON_CONSTANT_PATTERN,
    REASON_STRING_PATTERN,
    is_in_docstring,
    is_in_error_reason_class_def,
    is_in_mcp_error_context,
    is_in_sql_context,
    is_in_test_file,
    is_private_constant_definition,
    run_check,
    scan_file_for_reason_usage,
)

# ============================================================================
# 辅助函数
# ============================================================================


def create_temp_file(content: str, suffix: str = ".py") -> Path:
    """创建临时文件并返回路径。"""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.flush()
    f.close()
    return Path(f.name)


# ============================================================================
# Test: 正则表达式
# ============================================================================


class TestPatterns:
    """测试正则表达式匹配"""

    def test_reason_string_pattern_double_quotes(self):
        """测试双引号 reason="..." 匹配"""
        line = 'response = make_error(reason="POLICY_REJECT")'
        match = REASON_STRING_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "POLICY_REJECT"

    def test_reason_string_pattern_single_quotes(self):
        """测试单引号 reason='...' 匹配"""
        line = "response = make_error(reason='AUTH_FAILED')"
        match = REASON_STRING_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "AUTH_FAILED"

    def test_reason_string_pattern_with_spaces(self):
        """测试带空格的 reason = "..." 匹配"""
        line = 'response = make_error(reason = "INTERNAL_ERROR")'
        match = REASON_STRING_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "INTERNAL_ERROR"

    def test_reason_constant_pattern_error_reason(self):
        """测试 ErrorReason.X 匹配"""
        line = "response = make_error(reason=ErrorReason.POLICY_REJECT)"
        match = REASON_CONSTANT_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "POLICY_REJECT"

    def test_reason_constant_pattern_mcp_error_reason(self):
        """测试 McpErrorReason.X 匹配"""
        line = "response = make_error(reason=McpErrorReason.AUTH_FAILED)"
        match = REASON_CONSTANT_PATTERN.search(line)
        assert match is not None
        assert match.group(1) == "AUTH_FAILED"


# ============================================================================
# Test: 白名单判定函数
# ============================================================================


class TestWhitelistFunctions:
    """测试白名单判定函数"""

    def test_is_in_test_file_tests_dir(self):
        """tests/ 目录下的文件应被识别为测试文件"""
        project_root = Path("/project")
        file_path = project_root / "tests" / "test_example.py"
        assert is_in_test_file(file_path, project_root) is True

    def test_is_in_test_file_test_prefix(self):
        """test_ 前缀的文件应被识别为测试文件"""
        project_root = Path("/project")
        file_path = project_root / "src" / "test_module.py"
        assert is_in_test_file(file_path, project_root) is True

    def test_is_in_test_file_normal_file(self):
        """普通文件不应被识别为测试文件"""
        project_root = Path("/project")
        file_path = project_root / "src" / "engram" / "gateway" / "mcp_rpc.py"
        assert is_in_test_file(file_path, project_root) is False

    def test_is_private_constant_definition(self):
        """私有常量定义应被识别"""
        assert is_private_constant_definition('_INTERNAL_REASON = "SOME_VALUE"') is True
        assert is_private_constant_definition('    _MY_CONST = "VALUE"') is True
        assert is_private_constant_definition('PUBLIC_CONST = "VALUE"') is False
        assert is_private_constant_definition('reason="VALUE"') is False

    def test_is_in_docstring(self):
        """文档字符串内的内容应被识别"""
        content = '''
def example():
    """
    Example function.

    Use reason="POLICY_REJECT" for policy rejections.
    """
    pass
'''
        # 行 5 在 docstring 内
        assert is_in_docstring(content, 5) is True
        # 行 8 不在 docstring 内
        assert is_in_docstring(content, 8) is False

    def test_is_in_error_reason_class_def(self):
        """ErrorReason 类定义内的常量应被识别"""
        content = '''
class ErrorReason:
    """错误原因码常量"""

    POLICY_REJECT = "POLICY_REJECT"
    AUTH_FAILED = "AUTH_FAILED"

class OtherClass:
    SOME_CONST = "VALUE"
'''
        file_path = Path("/tmp/test.py")
        # 行 5 在 ErrorReason 类内
        assert is_in_error_reason_class_def(file_path, 5, content) is True
        # 行 6 在 ErrorReason 类内
        assert is_in_error_reason_class_def(file_path, 6, content) is True
        # 行 9 在 OtherClass 类内，不是 ErrorReason
        assert is_in_error_reason_class_def(file_path, 9, content) is False


# ============================================================================
# Test: MCP 错误上下文检测
# ============================================================================


class TestMcpErrorContextDetection:
    """测试 MCP 错误上下文检测"""

    def test_is_in_mcp_error_context_error_data(self):
        """ErrorData 构造应被识别为 MCP 错误上下文"""
        line = 'error_data = ErrorData(reason="SOME_VALUE")'
        content = line
        assert is_in_mcp_error_context(line, content, 1) is True

    def test_is_in_mcp_error_context_gateway_error(self):
        """GatewayError 构造应被识别为 MCP 错误上下文"""
        line = 'raise GatewayError(message="error", reason="SOME_VALUE")'
        content = line
        assert is_in_mcp_error_context(line, content, 1) is True

    def test_is_in_mcp_error_context_make_business_error(self):
        """make_business_error_response 调用应被识别为 MCP 错误上下文"""
        line = 'return make_business_error_response(req_id, error_msg, reason="POLICY_REJECT")'
        content = line
        assert is_in_mcp_error_context(line, content, 1) is True

    def test_is_not_in_mcp_error_context_policy_decision(self):
        """PolicyDecision 不应被识别为 MCP 错误上下文"""
        line = 'return PolicyDecision(action=PolicyAction.ALLOW, reason="private_space")'
        content = line
        assert is_in_mcp_error_context(line, content, 1) is False

    def test_is_not_in_mcp_error_context_validate_refs_decision(self):
        """ValidateRefsDecision 不应被识别为 MCP 错误上下文"""
        line = 'return ValidateRefsDecision(effective=True, reason="strict_enforced")'
        content = line
        assert is_in_mcp_error_context(line, content, 1) is False

    def test_is_in_sql_context(self):
        """SQL 语句应被识别为 SQL 上下文"""
        assert is_in_sql_context("WHERE reason = 'value'") is True
        assert is_in_sql_context("WHEN reason = 'outbox_flush_success' THEN 'ok'") is True
        assert is_in_sql_context("SELECT reason FROM table") is True
        assert is_in_sql_context("reason='value'") is False

    def test_multiline_mcp_error_context(self):
        """多行 MCP 错误上下文应被正确识别"""
        content = """error_data = ErrorData(
    category=ErrorCategory.BUSINESS,
    reason="SOME_VALUE",
)
"""
        # 行 3 包含 reason，应该向上查找到 ErrorData
        assert is_in_mcp_error_context('    reason="SOME_VALUE",', content, 3) is True

    def test_multiline_non_mcp_context(self):
        """多行非 MCP 错误上下文应被正确识别"""
        content = """return PolicyDecision(
    action=PolicyAction.ALLOW,
    reason="private_space",
)
"""
        # 行 3 包含 reason，应该向上查找到 PolicyDecision
        assert is_in_mcp_error_context('    reason="private_space",', content, 3) is False


# ============================================================================
# Test: 扫描逻辑
# ============================================================================


class TestScanLogic:
    """测试扫描逻辑"""

    def test_scan_detects_hardcoded_string_in_mcp_context(self):
        """检测 MCP 错误上下文中硬编码的 reason 字符串"""
        content = '''
def make_error():
    return ErrorData(reason="POLICY_REJECT")
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            assert len(results) == 1
            usage, violation = results[0]
            assert usage.value == "POLICY_REJECT"
            assert violation is not None
            assert "硬编码" in violation.violation_type
        finally:
            file_path.unlink()

    def test_scan_whitelist_non_mcp_context(self):
        """白名单：非 MCP 错误上下文中的 reason 不报错"""
        content = '''
def check_policy():
    return PolicyDecision(action=PolicyAction.ALLOW, reason="private_space")
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            assert len(results) == 1
            usage, violation = results[0]
            assert usage.value == "private_space"
            assert violation is None  # 应该被白名单
            assert usage.context == "非 MCP 错误上下文"
        finally:
            file_path.unlink()

    def test_scan_allows_constant_usage(self):
        """允许 ErrorReason.X 常量使用"""
        content = '''
def make_error():
    return ErrorData(reason=ErrorReason.POLICY_REJECT)
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            assert len(results) == 1
            usage, violation = results[0]
            assert usage.value == "POLICY_REJECT"
            assert usage.is_constant is True
            assert violation is None
        finally:
            file_path.unlink()

    def test_scan_whitelist_docstring(self):
        """白名单：文档字符串内的 reason 不报错"""
        content = '''
def example():
    """
    Example function.

    Use reason="POLICY_REJECT" for policy rejections.
    """
    return ErrorData(reason=ErrorReason.POLICY_REJECT)
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            # 应该有 2 个结果：docstring 中的（白名单）和正确使用的常量
            violations = [r[1] for r in results if r[1] is not None]
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_scan_whitelist_error_reason_class(self):
        """白名单：ErrorReason 类定义内的常量不报错"""
        content = '''
class ErrorReason:
    """错误原因码常量"""
    POLICY_REJECT = "POLICY_REJECT"
    AUTH_FAILED = "AUTH_FAILED"
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            violations = [r[1] for r in results if r[1] is not None]
            # ErrorReason 类定义内的常量应该被白名单
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_scan_whitelist_private_constant(self):
        """白名单：私有常量定义不报错"""
        content = '''
_INTERNAL_REASON = "INTERNAL_VALUE"

def use_reason():
    return ErrorData(reason=_INTERNAL_REASON)
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            violations = [r[1] for r in results if r[1] is not None]
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_scan_whitelist_comment(self):
        """白名单：注释行不报错"""
        content = '''
# Example: reason="POLICY_REJECT"
def make_error():
    return ErrorData(reason=ErrorReason.POLICY_REJECT)
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            violations = [r[1] for r in results if r[1] is not None]
            assert len(violations) == 0
        finally:
            file_path.unlink()


# ============================================================================
# Test: run_check 函数
# ============================================================================


class TestRunCheck:
    """测试 run_check 函数"""

    def test_run_check_with_violations(self):
        """有违规时（MCP 错误上下文中）应返回违规列表"""
        content = '''
def make_error():
    return ErrorData(reason="HARDCODED_VALUE")
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            violations, total, correct = run_check(
                paths=[str(file_path)],
                project_root=project_root,
            )
            assert len(violations) == 1
            assert total == 1
            assert correct == 0
        finally:
            file_path.unlink()

    def test_run_check_without_violations(self):
        """无违规时应返回空列表"""
        content = '''
def make_error():
    return ErrorData(reason=ErrorReason.POLICY_REJECT)
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            violations, total, correct = run_check(
                paths=[str(file_path)],
                project_root=project_root,
            )
            assert len(violations) == 0
            assert total == 1
            assert correct == 1
        finally:
            file_path.unlink()

    def test_run_check_mixed(self):
        """混合场景：部分正确、部分违规（都在 MCP 错误上下文中）"""
        content = '''
def make_error():
    # 正确
    return ErrorData(reason=ErrorReason.POLICY_REJECT)

def bad_error():
    # 违规（MCP 错误上下文）
    return GatewayError(message="error", reason="HARDCODED")
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            violations, total, correct = run_check(
                paths=[str(file_path)],
                project_root=project_root,
            )
            assert len(violations) == 1
            assert total == 2
            assert correct == 1
        finally:
            file_path.unlink()

    def test_run_check_non_mcp_context_whitelisted(self):
        """非 MCP 错误上下文的 reason 应被白名单"""
        content = '''
def check_policy():
    return PolicyDecision(action=PolicyAction.ALLOW, reason="private_space")
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            violations, total, correct = run_check(
                paths=[str(file_path)],
                project_root=project_root,
            )
            # 应该没有违规，因为 PolicyDecision 不是 MCP 错误上下文
            assert len(violations) == 0
            assert total == 1
            assert correct == 0  # 不是常量，但被白名单
        finally:
            file_path.unlink()


# ============================================================================
# Test: McpErrorReason 支持
# ============================================================================


class TestMcpErrorReasonSupport:
    """测试 McpErrorReason 常量支持"""

    def test_mcp_error_reason_constant_allowed(self):
        """允许 McpErrorReason.X 常量使用"""
        content = '''
def make_error():
    return ErrorData(reason=McpErrorReason.INTERNAL_ERROR)
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            assert len(results) == 1
            usage, violation = results[0]
            assert usage.value == "INTERNAL_ERROR"
            assert usage.is_constant is True
            assert violation is None
        finally:
            file_path.unlink()

    def test_mcp_error_reason_class_def_whitelisted(self):
        """白名单：McpErrorReason 类定义内的常量不报错"""
        content = '''
class McpErrorReason:
    """MCP 错误原因码常量"""
    PARSE_ERROR = "PARSE_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
'''
        file_path = create_temp_file(content)
        project_root = file_path.parent

        try:
            results = list(scan_file_for_reason_usage(file_path, project_root))
            violations = [r[1] for r in results if r[1] is not None]
            assert len(violations) == 0
        finally:
            file_path.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
