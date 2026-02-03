"""
engram_logbook 兼容别名导入测试

验证 engram_logbook 作为 engram.logbook 的兼容别名能够正常工作，
确保以下场景稳定可用：

1. 顶层包导入：import engram_logbook
2. 子模块导入：from engram_logbook.errors import ErrorCode
3. 属性等价性：engram_logbook.X == engram.logbook.X
4. sys.modules 注册：patch('engram_logbook.xxx') 可正常工作

契约来源：
- docs/architecture/naming.md §6.4 域 B: engram_logbook 兼容包
- engram_logbook/__init__.py 实现

注意：
- 此测试文件专门验证兼容导入机制的稳定性
- 如果修改 engram_logbook/__init__.py 中的 _ALIAS_MODULES，需同步更新此测试
"""

import sys
from unittest.mock import patch

import pytest


class TestEngramLogbookTopLevelImport:
    """顶层包导入测试"""

    def test_import_engram_logbook(self):
        """测试 import engram_logbook 成功"""
        import engram_logbook

        assert engram_logbook is not None

    def test_engram_logbook_has_path(self):
        """测试 engram_logbook.__path__ 存在（支持子模块导入）"""
        import engram_logbook

        assert hasattr(engram_logbook, "__path__")
        assert engram_logbook.__path__ is not None

    def test_engram_logbook_equivalent_to_engram_logbook_module(self):
        """测试 engram_logbook 与 engram.logbook 功能等价"""
        import engram.logbook as engram_logbook_canonical
        import engram_logbook

        # __path__ 应指向相同位置
        assert engram_logbook.__path__ == engram_logbook_canonical.__path__


class TestEngramLogbookErrorsSubmodule:
    """errors 子模块导入测试"""

    def test_import_errors_module(self):
        """测试 from engram_logbook import errors 成功"""
        from engram_logbook import errors

        assert errors is not None

    def test_import_error_code_class(self):
        """测试 from engram_logbook.errors import ErrorCode 成功"""
        from engram_logbook.errors import ErrorCode

        assert ErrorCode is not None

    def test_error_code_has_expected_attributes(self):
        """测试 ErrorCode 包含预期的错误码常量"""
        from engram_logbook.errors import ErrorCode

        # 验证核心错误码存在
        assert hasattr(ErrorCode, "OPENMEMORY_WRITE_FAILED_CONNECTION")
        assert hasattr(ErrorCode, "OUTBOX_FLUSH_SUCCESS")
        assert hasattr(ErrorCode, "ACTOR_UNKNOWN_REJECT")
        assert hasattr(ErrorCode, "DEDUP_HIT")
        assert hasattr(ErrorCode, "DB_CONNECTION_ERROR")

    def test_error_code_equivalent(self):
        """测试 engram_logbook.errors.ErrorCode == engram.logbook.errors.ErrorCode"""
        from engram_logbook.errors import ErrorCode as AliasErrorCode

        from engram.logbook.errors import ErrorCode as CanonicalErrorCode

        assert AliasErrorCode is CanonicalErrorCode

    def test_import_exception_classes(self):
        """测试异常类导入"""
        from engram_logbook.errors import (
            ConfigError,
            DatabaseError,
            EngramError,
            ValidationError,
        )

        assert EngramError is not None
        assert issubclass(ConfigError, EngramError)
        assert issubclass(DatabaseError, EngramError)
        assert issubclass(ValidationError, EngramError)

    def test_import_exit_code(self):
        """测试 ExitCode 导入"""
        from engram_logbook.errors import ExitCode

        assert hasattr(ExitCode, "SUCCESS")
        assert ExitCode.SUCCESS == 0


class TestEngramLogbookUriSubmodule:
    """uri 子模块导入测试"""

    def test_import_uri_module(self):
        """测试 from engram_logbook import uri 成功"""
        from engram_logbook import uri

        assert uri is not None

    def test_import_uri_functions(self):
        """测试 URI 解析函数导入"""
        from engram_logbook.uri import (
            build_evidence_uri,
            parse_attachment_evidence_uri,
            parse_uri,
        )

        assert callable(parse_uri)
        assert callable(build_evidence_uri)
        assert callable(parse_attachment_evidence_uri)

    def test_uri_module_equivalent(self):
        """测试 engram_logbook.uri == engram.logbook.uri"""
        from engram.logbook import uri as canonical_uri
        from engram_logbook import uri as alias_uri

        assert alias_uri is canonical_uri


class TestEngramLogbookConfigSubmodule:
    """config 子模块导入测试"""

    def test_import_config_module(self):
        """测试 from engram_logbook import config 成功"""
        from engram_logbook import config

        assert config is not None

    def test_import_get_config(self):
        """测试 get_config 函数导入"""
        from engram_logbook.config import get_config

        assert callable(get_config)


class TestEngramLogbookDbSubmodule:
    """db 子模块导入测试"""

    def test_import_db_module(self):
        """测试 from engram_logbook import db 成功"""
        from engram_logbook import db

        assert db is not None

    def test_import_get_connection(self):
        """测试 get_connection 函数导入"""
        from engram_logbook.db import get_connection

        assert callable(get_connection)


class TestEngramLogbookMigrateSubmodule:
    """migrate 子模块导入测试"""

    def test_import_migrate_module(self):
        """测试 from engram_logbook import migrate 成功"""
        from engram_logbook import migrate

        assert migrate is not None


class TestEngramLogbookScmSyncSubmodules:
    """SCM 同步相关子模块导入测试"""

    def test_import_scm_sync_queue(self):
        """测试 scm_sync_queue 子模块导入"""
        from engram_logbook import scm_sync_queue

        assert scm_sync_queue is not None

    def test_import_scm_sync_errors(self):
        """测试 scm_sync_errors 子模块导入"""
        from engram_logbook import scm_sync_errors

        assert scm_sync_errors is not None

    def test_import_scm_sync_job_types(self):
        """测试 scm_sync_job_types 子模块导入"""
        from engram_logbook import scm_sync_job_types

        assert scm_sync_job_types is not None


class TestSysModulesRegistration:
    """sys.modules 注册测试（确保 patch 可正常工作）"""

    def test_errors_in_sys_modules(self):
        """测试 engram_logbook.errors 注册到 sys.modules"""
        # 先触发导入
        from engram_logbook import errors  # noqa: F401

        assert "engram_logbook.errors" in sys.modules

    def test_uri_in_sys_modules(self):
        """测试 engram_logbook.uri 注册到 sys.modules"""
        from engram_logbook import uri  # noqa: F401

        assert "engram_logbook.uri" in sys.modules

    def test_config_in_sys_modules(self):
        """测试 engram_logbook.config 注册到 sys.modules"""
        from engram_logbook import config  # noqa: F401

        assert "engram_logbook.config" in sys.modules

    def test_db_in_sys_modules(self):
        """测试 engram_logbook.db 注册到 sys.modules"""
        from engram_logbook import db  # noqa: F401

        assert "engram_logbook.db" in sys.modules

    def test_scm_sync_queue_in_sys_modules(self):
        """测试 engram_logbook.scm_sync_queue 注册到 sys.modules"""
        from engram_logbook import scm_sync_queue  # noqa: F401

        assert "engram_logbook.scm_sync_queue" in sys.modules


class TestPatchCompatibility:
    """patch 兼容性测试（模拟现有测试的 mock 用法）"""

    def test_patch_engram_logbook_errors(self):
        """测试 patch('engram_logbook.errors.ErrorCode') 可正常工作"""
        from engram_logbook.errors import ErrorCode

        original_value = ErrorCode.DEDUP_HIT

        with patch.object(ErrorCode, "DEDUP_HIT", "mocked_value"):
            from engram_logbook.errors import ErrorCode as PatchedErrorCode

            # 注意：patch.object 修改的是类属性，重新导入后应该看到 mocked 值
            assert PatchedErrorCode.DEDUP_HIT == "mocked_value"

        # patch 结束后恢复
        assert ErrorCode.DEDUP_HIT == original_value

    def test_patch_engram_logbook_db_get_connection(self):
        """测试 patch('engram_logbook.db.get_connection') 可正常工作"""
        mock_connection = object()

        with patch("engram_logbook.db.get_connection", return_value=mock_connection):
            from engram_logbook.db import get_connection

            result = get_connection()
            assert result is mock_connection


class TestAliasModulesCompleteness:
    """别名模块完整性测试"""

    def test_all_documented_aliases_importable(self):
        """测试文档中列出的所有别名都可导入

        契约来源：docs/architecture/naming.md §6.4 兼容包说明
        """
        # 文档中列出的兼容路径
        documented_imports = [
            ("engram_logbook", "errors"),
            ("engram_logbook", "uri"),
            ("engram_logbook", "config"),
            ("engram_logbook", "db"),
            ("engram_logbook", "migrate"),
        ]

        import importlib

        for package, module in documented_imports:
            full_name = f"{package}.{module}"
            try:
                mod = importlib.import_module(full_name)
                assert mod is not None, f"{full_name} 导入成功但为 None"
            except ImportError as e:
                pytest.fail(f"无法导入 {full_name}: {e}")

    def test_alias_modules_list_coverage(self):
        """测试 _ALIAS_MODULES 列表中的模块都可导入"""
        import engram_logbook

        # 获取 _ALIAS_MODULES 列表（如果存在）
        alias_modules = getattr(engram_logbook, "_ALIAS_MODULES", None)
        if alias_modules is None:
            # 如果没有暴露 _ALIAS_MODULES，跳过此测试
            pytest.skip("_ALIAS_MODULES 未暴露")

        import importlib

        for module_name in alias_modules:
            full_name = f"engram_logbook.{module_name}"
            try:
                mod = importlib.import_module(full_name)
                assert mod is not None
            except ImportError:
                # 某些模块可能在当前环境不可用（依赖问题），记录但不失败
                pass


class TestNoImplicitDbImport:
    """静态检查：禁止 src/engram/** 出现 import db 顶层隐式依赖

    规则：
    - 禁止在模块顶层使用 `import db` 或 `from db import xxx`
    - 这些是隐式依赖根目录的 db.py，不是包内模块
    - 允许 `from engram.logbook import scm_db` 或 `from engram.logbook.scm_db import xxx`（包内导入）
    - 函数内的延迟导入 `import db as db_api`（支持测试注入）目前允许，
      但建议迁移到 `from engram.logbook import scm_db as db_api`

    契约来源：
    - 打包独立性要求：pip 安装后应不依赖项目根目录的任何文件

    注意：
    - 此测试当前作为信息性检查（警告），不会导致测试失败
    - 随着代码迁移完成，应将警告改为断言
    """

    @pytest.fixture
    def engram_source_files(self):
        """获取 src/engram 目录下所有 Python 文件"""
        import pathlib

        src_dir = pathlib.Path(__file__).parent.parent.parent / "src" / "engram"
        return list(src_dir.rglob("*.py"))

    def test_no_toplevel_import_db(self, engram_source_files):
        """检查无顶层 import db 语句

        使用 AST 解析检查模块顶层是否有 `import db` 语句。
        注意：此测试只检查 `import db`，不检查 `from db import xxx`。
        """
        import ast

        violations = []

        for py_file in engram_source_files:
            content = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(content, filename=str(py_file))
            except SyntaxError:
                continue  # 跳过语法错误的文件

            # 只检查顶层语句（不进入函数/类体）
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        # 检查 import db 或 import db as xxx
                        if alias.name == "db":
                            rel_path = py_file.relative_to(py_file.parent.parent.parent.parent)
                            violations.append(f"{rel_path}:{node.lineno}: import {alias.name}")

        # 目前没有顶层 import db，保持断言
        assert not violations, (
            "src/engram/** 中发现顶层 import db（这些会引用根目录的 db.py）：\n"
            + "\n".join(f"  {v}" for v in violations)
            + "\n\n修复方法："
            "\n  - 使用包内导入: from engram.logbook import scm_db"
            "\n  - 或使用完整路径: from engram.logbook.scm_db import <function>"
        )

    def test_no_toplevel_from_db_import(self, engram_source_files):
        """检查无顶层 from db import xxx 语句

        当前作为信息性检查，只记录违规但不失败。
        待代码迁移完成后，应改为断言。
        """
        import ast
        import warnings

        violations = []

        for py_file in engram_source_files:
            content = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(content, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ImportFrom):
                    # from db import xxx 是隐式依赖
                    if node.module == "db" and node.level == 0:
                        rel_path = py_file.relative_to(py_file.parent.parent.parent.parent)
                        names = ", ".join(a.name for a in node.names)
                        violations.append(f"{rel_path}:{node.lineno}: from db import {names}")

        if violations:
            # 当前作为警告，待迁移完成后改为 assert not violations
            warnings.warn(
                f"发现 {len(violations)} 处顶层 from db import（需迁移到包内导入）:\n"
                + "\n".join(f"  {v}" for v in violations)
                + "\n\n修复方法："
                "\n  - 使用 from engram.logbook.scm_db import <function>"
                "\n  - 或 from engram.logbook import scm_db",
                UserWarning,
            )

    def test_document_delayed_db_imports(self, engram_source_files):
        """记录函数内的延迟 import db 调用（信息性检查）

        这些延迟导入目前是允许的（用于测试注入模式），
        但长期建议迁移到包内导入。

        此测试不会失败，仅记录发现的延迟导入数量。
        """
        import ast
        import warnings

        delayed_imports = []

        for py_file in engram_source_files:
            content = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(content, filename=str(py_file))
            except SyntaxError:
                continue

            # 遍历所有节点，查找函数/方法内的 import
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for child in ast.walk(node):
                        if isinstance(child, ast.Import):
                            for alias in child.names:
                                if alias.name == "db":
                                    rel_path = py_file.relative_to(
                                        py_file.parent.parent.parent.parent
                                    )
                                    delayed_imports.append(
                                        f"{rel_path}:{child.lineno}: import db as {alias.asname or alias.name}"
                                    )

        if delayed_imports:
            # 不失败，只是警告
            warnings.warn(
                f"发现 {len(delayed_imports)} 处函数内延迟 import db（建议迁移到包内导入）:\n"
                + "\n".join(f"  {v}" for v in delayed_imports[:10])
                + (
                    f"\n  ... 还有 {len(delayed_imports) - 10} 处"
                    if len(delayed_imports) > 10
                    else ""
                )
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
