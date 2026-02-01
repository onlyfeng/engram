"""
Gateway Public API 文档同步门禁测试

测试 scripts/ci/check_gateway_public_api_docs_sync.py 的功能，确保：
1. 正例：实际的 public_api.py 与文档同步
2. 负例：不同步的情况会被正确识别

与 CI job 对应关系：
- 本测试对应 CI workflow 中的 gateway-public-api-surface job（合并）
- 门禁脚本路径: scripts/ci/check_gateway_public_api_docs_sync.py
- 被检查文件: src/engram/gateway/public_api.py
- 文档文件: docs/architecture/gateway_public_api_surface.md

运行方式：
    pytest tests/ci/test_gateway_public_api_docs_sync.py -v
    # 或作为 tests/ci 整体运行
    pytest tests/ci -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.ci.check_gateway_public_api_docs_sync import (
    DOC_PATH,
    EXPORT_BLOCK_END,
    EXPORT_BLOCK_START,
    PUBLIC_API_PATH,
    check_docs_sync,
    extract_all_from_code,
    extract_symbols_from_doc,
)
from tests.ci.helpers.subprocess_env import get_subprocess_env

# ============================================================================
# 辅助函数
# ============================================================================


def create_temp_file(content: str, suffix: str = ".py") -> Path:
    """创建临时文件并返回路径"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(content)
        return Path(f.name)


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent.resolve()


# ============================================================================
# 正例测试：验证实际文件通过检查
# ============================================================================


class TestRealFilesPassCheck:
    """验证实际的 public_api.py 与文档同步"""

    def test_real_files_exist(self) -> None:
        """实际文件应存在"""
        project_root = get_project_root()

        code_path = project_root / PUBLIC_API_PATH
        doc_path = project_root / DOC_PATH

        assert code_path.exists(), f"public_api.py 不存在: {code_path}"
        assert doc_path.exists(), f"文档不存在: {doc_path}"

    def test_code_has_all_defined(self) -> None:
        """代码文件应定义 __all__"""
        project_root = get_project_root()
        code_path = project_root / PUBLIC_API_PATH

        symbols, errors = extract_all_from_code(code_path)

        assert not errors, f"提取 __all__ 失败: {errors}"
        assert len(symbols) > 0, "__all__ 应包含符号"

    def test_doc_has_symbols(self) -> None:
        """文档应包含导出项符号"""
        project_root = get_project_root()
        doc_path = project_root / DOC_PATH

        symbols, errors = extract_symbols_from_doc(doc_path)

        assert not errors, f"提取文档符号失败: {errors}"
        assert len(symbols) > 0, "文档应包含导出项符号"

    def test_real_files_synced(self) -> None:
        """实际的 public_api.py 与文档应同步"""
        project_root = get_project_root()
        code_path = project_root / PUBLIC_API_PATH
        doc_path = project_root / DOC_PATH

        result = check_docs_sync(code_path, doc_path)

        # 允许一定程度的差异（文档可能包含更多解释性内容）
        # 但代码中的所有符号都应该在文档中
        assert not result.has_parse_errors(), f"解析错误: {result.parse_errors}"
        assert len(result.missing_in_doc) == 0, (
            f"以下符号在代码中但文档中缺失:\n"
            f"{result.missing_in_doc}\n\n"
            f"请在 docs/architecture/gateway_public_api_surface.md 中添加这些符号的文档"
        )


class TestScriptSubprocess:
    """测试通过子进程运行脚本"""

    def test_script_exits_zero_for_real_files(self) -> None:
        """脚本检查实际文件应返回 0（成功）或有警告"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_docs_sync.py"

        result = subprocess.run(
            [sys.executable, str(script_path), "--project-root", str(project_root)],
            capture_output=True,
            text=True,
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        # 检查是否成功（退出码 0）
        assert result.returncode == 0, (
            f"脚本应返回 0，但返回 {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_script_json_output(self) -> None:
        """脚本 JSON 输出格式正确"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_docs_sync.py"

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

        import json

        output = json.loads(result.stdout)
        assert "ok" in output
        assert "code_symbol_count" in output
        assert "doc_symbol_count" in output
        assert "missing_in_doc" in output
        assert "extra_in_doc" in output


# ============================================================================
# 代码解析测试
# ============================================================================


class TestCodeParsing:
    """测试代码解析功能"""

    def test_extract_simple_all(self) -> None:
        """提取简单的 __all__ 列表"""
        content = '''\
"""Test module"""
__all__ = ["Foo", "Bar", "Baz"]
'''
        file_path = create_temp_file(content)
        try:
            symbols, errors = extract_all_from_code(file_path)

            assert not errors
            assert symbols == ["Foo", "Bar", "Baz"]
        finally:
            file_path.unlink()

    def test_extract_multiline_all(self) -> None:
        """提取多行的 __all__ 列表"""
        content = '''\
"""Test module"""
__all__ = [
    "Foo",
    "Bar",
    "Baz",
]
'''
        file_path = create_temp_file(content)
        try:
            symbols, errors = extract_all_from_code(file_path)

            assert not errors
            assert symbols == ["Foo", "Bar", "Baz"]
        finally:
            file_path.unlink()

    def test_extract_all_with_comments(self) -> None:
        """提取带注释的 __all__ 列表"""
        content = '''\
"""Test module"""
__all__ = [
    # Tier A
    "Foo",
    "Bar",
    # Tier B
    "Baz",
]
'''
        file_path = create_temp_file(content)
        try:
            symbols, errors = extract_all_from_code(file_path)

            assert not errors
            assert symbols == ["Foo", "Bar", "Baz"]
        finally:
            file_path.unlink()

    def test_no_all_defined(self) -> None:
        """没有定义 __all__ 应报错"""
        content = '''\
"""Test module without __all__"""
def foo():
    pass
'''
        file_path = create_temp_file(content)
        try:
            symbols, errors = extract_all_from_code(file_path)

            assert len(errors) > 0
            assert symbols == []
        finally:
            file_path.unlink()

    def test_file_not_exists(self) -> None:
        """文件不存在应报错"""
        file_path = Path("/nonexistent/path/to/file.py")

        symbols, errors = extract_all_from_code(file_path)

        assert len(errors) > 0
        assert "不存在" in errors[0]


# ============================================================================
# 文档解析测试
# ============================================================================


class TestDocParsing:
    """测试文档解析功能"""

    def test_extract_from_marked_block(self) -> None:
        """从标记区块内的表格中提取符号"""
        content = f"""\
# Gateway Public API

## 导出项

{EXPORT_BLOCK_START}
| 导出项 | 类型 | 说明 |
|--------|------|------|
| `RequestContext` | dataclass | 请求上下文 |
| `GatewayDeps` | dataclass | 依赖容器 |
| `McpErrorCode` | class | 错误码 |
{EXPORT_BLOCK_END}

## 其他内容

| 不应被提取 | 说明 |
|------------|------|
| `OtherSymbol` | 不在标记区块内 |
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert not errors, f"不应有错误: {errors}"
            assert "RequestContext" in symbols
            assert "GatewayDeps" in symbols
            assert "McpErrorCode" in symbols
            # 标记区块外的符号不应被提取
            assert "OtherSymbol" not in symbols
        finally:
            file_path.unlink()

    def test_skip_tier_labels(self) -> None:
        """应跳过 Tier 标识行"""
        content = f"""\
# Gateway Public API

{EXPORT_BLOCK_START}
| 导出项 | 导入时机 |
|--------|----------|
| **Tier A** | |
| `RequestContext` | import-time |
| **Tier B** | |
| `LogbookAdapter` | 延迟导入 |
{EXPORT_BLOCK_END}
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert not errors
            assert "RequestContext" in symbols
            assert "LogbookAdapter" in symbols
            # Tier 标识不应被提取
            assert "Tier" not in symbols
            assert "A" not in symbols
            assert "B" not in symbols
        finally:
            file_path.unlink()

    def test_doc_not_exists(self) -> None:
        """文档不存在应报错"""
        file_path = Path("/nonexistent/path/to/doc.md")

        symbols, errors = extract_symbols_from_doc(file_path)

        assert len(errors) > 0
        assert "不存在" in errors[0]


class TestMarkedBlockParsing:
    """测试标记区块解析"""

    def test_missing_both_markers(self) -> None:
        """缺少两个标记应报错"""
        content = """\
# Gateway Public API

| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert len(errors) > 0
            assert "未找到导出表标记区块" in errors[0]
            assert EXPORT_BLOCK_START in errors[0]
        finally:
            file_path.unlink()

    def test_missing_start_marker(self) -> None:
        """只有结束标记应报错"""
        content = f"""\
# Gateway Public API

| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
{EXPORT_BLOCK_END}
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert len(errors) > 0
            assert "缺少起始标记" in errors[0]
        finally:
            file_path.unlink()

    def test_missing_end_marker(self) -> None:
        """只有起始标记应报错"""
        content = f"""\
# Gateway Public API

{EXPORT_BLOCK_START}
| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert len(errors) > 0
            assert "缺少结束标记" in errors[0]
        finally:
            file_path.unlink()

    def test_empty_block(self) -> None:
        """空的标记区块应报错"""
        content = f"""\
# Gateway Public API

{EXPORT_BLOCK_START}
{EXPORT_BLOCK_END}
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert len(errors) > 0
            assert "未找到任何导出项符号" in errors[0]
        finally:
            file_path.unlink()

    def test_block_with_no_table(self) -> None:
        """标记区块内没有表格应报错"""
        content = f"""\
# Gateway Public API

{EXPORT_BLOCK_START}
这里没有表格，只有文字说明。
{EXPORT_BLOCK_END}
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert len(errors) > 0
            assert "未找到任何导出项符号" in errors[0]
        finally:
            file_path.unlink()

    def test_reversed_markers(self) -> None:
        """结束标记在起始标记之前应报错"""
        content = f"""\
# Gateway Public API

{EXPORT_BLOCK_END}
| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
{EXPORT_BLOCK_START}
"""
        file_path = create_temp_file(content, suffix=".md")
        try:
            symbols, errors = extract_symbols_from_doc(file_path)

            assert len(errors) > 0
            assert "结束标记出现在起始标记之前" in errors[0]
        finally:
            file_path.unlink()


# ============================================================================
# 同步检查测试
# ============================================================================


class TestSyncCheck:
    """测试同步检查功能"""

    def test_synced_files(self) -> None:
        """同步的文件应通过检查"""
        code_content = '''\
"""Test module"""
__all__ = ["Foo", "Bar"]
'''
        doc_content = f"""\
# API

{EXPORT_BLOCK_START}
| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
| `Bar` | class |
{EXPORT_BLOCK_END}
"""
        code_path = create_temp_file(code_content)
        doc_path = create_temp_file(doc_content, suffix=".md")
        try:
            result = check_docs_sync(code_path, doc_path)

            assert result.is_synced(), f"应同步但不同步: {result.to_dict()}"
            assert len(result.missing_in_doc) == 0
            assert len(result.extra_in_doc) == 0
        finally:
            code_path.unlink()
            doc_path.unlink()

    def test_missing_in_doc(self) -> None:
        """代码中有但文档中缺失应被检测"""
        code_content = '''\
"""Test module"""
__all__ = ["Foo", "Bar", "Baz"]
'''
        doc_content = f"""\
# API

{EXPORT_BLOCK_START}
| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
| `Bar` | class |
{EXPORT_BLOCK_END}
"""
        code_path = create_temp_file(code_content)
        doc_path = create_temp_file(doc_content, suffix=".md")
        try:
            result = check_docs_sync(code_path, doc_path)

            assert not result.is_synced()
            assert "Baz" in result.missing_in_doc
        finally:
            code_path.unlink()
            doc_path.unlink()

    def test_extra_in_doc(self) -> None:
        """文档中有但代码中缺失应被检测"""
        code_content = '''\
"""Test module"""
__all__ = ["Foo"]
'''
        doc_content = f"""\
# API

{EXPORT_BLOCK_START}
| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
| `Bar` | class |
{EXPORT_BLOCK_END}
"""
        code_path = create_temp_file(code_content)
        doc_path = create_temp_file(doc_content, suffix=".md")
        try:
            result = check_docs_sync(code_path, doc_path)

            assert not result.is_synced()
            assert "Bar" in result.extra_in_doc
        finally:
            code_path.unlink()
            doc_path.unlink()


# ============================================================================
# 结果序列化测试
# ============================================================================


class TestResultSerialization:
    """测试结果序列化"""

    def test_to_dict(self) -> None:
        """to_dict() 应返回正确格式"""
        code_content = '''\
"""Test module"""
__all__ = ["Foo", "Bar", "Baz"]
'''
        doc_content = f"""\
# API

{EXPORT_BLOCK_START}
| 导出项 | 类型 |
|--------|------|
| `Foo` | class |
| `Bar` | class |
{EXPORT_BLOCK_END}
"""
        code_path = create_temp_file(code_content)
        doc_path = create_temp_file(doc_content, suffix=".md")
        try:
            result = check_docs_sync(code_path, doc_path)
            output = result.to_dict()

            assert "ok" in output
            assert output["ok"] is False
            assert "code_symbol_count" in output
            assert output["code_symbol_count"] == 3
            assert "doc_symbol_count" in output
            assert output["doc_symbol_count"] == 2
            assert "missing_in_doc" in output
            assert "Baz" in output["missing_in_doc"]
        finally:
            code_path.unlink()
            doc_path.unlink()


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_all(self) -> None:
        """空的 __all__ 应正常处理"""
        code_content = '''\
"""Test module"""
__all__ = []
'''
        doc_content = f"""\
# API

{EXPORT_BLOCK_START}
没有导出项
{EXPORT_BLOCK_END}
"""
        code_path = create_temp_file(code_content)
        doc_path = create_temp_file(doc_content, suffix=".md")
        try:
            result = check_docs_sync(code_path, doc_path)

            # 空的 __all__ 被视为错误（代码没有符号）
            # 文档也没有符号（区块内无表格）
            assert result.has_parse_errors() or len(result.code_symbols) == 0
        finally:
            code_path.unlink()
            doc_path.unlink()

    @pytest.mark.skip(reason="实际文件可能有差异，此测试仅用于开发调试")
    def test_verbose_output(self) -> None:
        """verbose 模式应显示详细信息"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_gateway_public_api_docs_sync.py"

        result = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--project-root",
                str(project_root),
                "--verbose",
            ],
            capture_output=True,
            text=True,
            env=get_subprocess_env(project_root),
            cwd=str(project_root),
        )

        # verbose 模式应显示符号列表
        assert "代码 __all__ 符号列表:" in result.stdout
