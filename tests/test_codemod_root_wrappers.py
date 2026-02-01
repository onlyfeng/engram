#!/usr/bin/env python3
"""
codemod_root_wrappers.py 脚本的单元测试

测试覆盖:
- 简单 import 语句改写
- from-import 语句改写
- 多行导入（括号包裹的导入列表）
- TYPE_CHECKING 块跳过
- 字符串误报（不应被改写）
- 带别名的 import
- 迁移目标解析
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

import pytest

from scripts.ci.codemod_root_wrappers import (
    ImportMatch,
    MigrationEntry,
    extract_imports,
    migrate_file,
    parse_import_target,
    rewrite_import_statement,
)

# ============================================================================
# 测试 parse_import_target
# ============================================================================


class TestParseImportTarget:
    """测试 import_target 解析"""

    def test_with_entry_point(self) -> None:
        """测试带入口点的 import_target"""
        module_path, entry_point = parse_import_target("engram.logbook.cli.scm_sync:runner_main")
        assert module_path == "engram.logbook.cli.scm_sync"
        assert entry_point == "runner_main"

    def test_without_entry_point(self) -> None:
        """测试不带入口点的 import_target"""
        module_path, entry_point = parse_import_target("engram.logbook.materialize_patch_blob")
        assert module_path == "engram.logbook.materialize_patch_blob"
        assert entry_point is None

    def test_complex_path(self) -> None:
        """测试复杂路径"""
        module_path, entry_point = parse_import_target("engram.logbook.cli.artifacts:main")
        assert module_path == "engram.logbook.cli.artifacts"
        assert entry_point == "main"


# ============================================================================
# 测试 extract_imports
# ============================================================================


class TestExtractImports:
    """测试 AST 导入提取功能（与 check_no_root_wrappers_usage.py 一致）"""

    def test_simple_import(self) -> None:
        """测试简单的 import 语句"""
        source = "import scm_sync_runner"
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].old_module == "scm_sync_runner"
        assert imports[0].import_type == "import"
        assert imports[0].line_number == 1

    def test_from_import(self) -> None:
        """测试 from ... import 语句"""
        source = "from scm_sync_runner import main"
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].old_module == "scm_sync_runner"
        assert imports[0].import_type == "from"
        assert imports[0].imported_names == ["main"]

    def test_import_with_alias(self) -> None:
        """测试 import ... as 语句"""
        source = "import db_migrate as dm"
        target: Set[str] = {"db_migrate"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].old_module == "db_migrate"
        assert imports[0].alias == "dm"

    def test_ignore_string_fake_import(self) -> None:
        """测试忽略字符串中的伪 import"""
        source = """
msg = "import scm_sync_runner"
x = 'from db_migrate import main'
"""
        target: Set[str] = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports(source, target)
        assert len(imports) == 0

    def test_ignore_docstring_fake_import(self) -> None:
        """测试忽略 docstring 中的伪 import"""
        source = '''
def foo():
    """
    Example:
        import scm_sync_runner
        from db_bootstrap import create_roles
    """
    pass
'''
        target: Set[str] = {"scm_sync_runner", "db_bootstrap"}
        imports = extract_imports(source, target)
        assert len(imports) == 0

    def test_type_checking_block_marked(self) -> None:
        """测试 TYPE_CHECKING 块内的导入被正确标记"""
        source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import scm_sync_runner
    from db_migrate import migrate
"""
        target: Set[str] = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports(source, target)
        assert len(imports) == 2
        assert all(imp.is_type_checking for imp in imports)

    def test_type_checking_with_typing_prefix(self) -> None:
        """测试 typing.TYPE_CHECKING 形式"""
        source = """
import typing

if typing.TYPE_CHECKING:
    import logbook_cli
"""
        target: Set[str] = {"logbook_cli"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].is_type_checking is True

    def test_multiline_from_import(self) -> None:
        """测试多行 from-import（括号包裹）"""
        source = """
from scm_sync_runner import (
    main,
    run_sync,
    cleanup,
)
"""
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].old_module == "scm_sync_runner"
        assert imports[0].imported_names == ["main", "run_sync", "cleanup"]

    def test_multiline_from_import_with_comments(self) -> None:
        """测试带注释的多行 from-import"""
        source = """
from artifact_cli import (
    # CLI 入口
    main,
    # 命令处理
    handle_command,
)
"""
        target: Set[str] = {"artifact_cli"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].old_module == "artifact_cli"
        assert set(imports[0].imported_names or []) == {"main", "handle_command"}

    def test_mixed_imports(self) -> None:
        """测试混合导入场景"""
        source = """
import os
import scm_sync_runner
from pathlib import Path
from db_migrate import main
import json
"""
        target: Set[str] = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports(source, target)
        assert len(imports) == 2
        modules = {imp.old_module for imp in imports}
        assert modules == {"scm_sync_runner", "db_migrate"}

    def test_allowed_import_not_detected(self) -> None:
        """测试非目标模块不被检测"""
        source = """
import os
import sys
from pathlib import Path
from engram.logbook import scm_sync_runner
"""
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        # engram.logbook.scm_sync_runner 的顶层模块是 engram，不是 scm_sync_runner
        assert len(imports) == 0

    def test_syntax_error_returns_empty(self) -> None:
        """测试语法错误时返回空列表"""
        source = "import (syntax error here"
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        assert imports == []

    def test_non_type_checking_import_not_marked(self) -> None:
        """测试非 TYPE_CHECKING 块内的导入不被标记"""
        source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typing_only

# 这是正常的导入
import scm_sync_runner
"""
        target: Set[str] = {"scm_sync_runner", "typing_only"}
        imports = extract_imports(source, target)
        assert len(imports) == 2

        typing_only_import = next(imp for imp in imports if imp.old_module == "typing_only")
        scm_import = next(imp for imp in imports if imp.old_module == "scm_sync_runner")

        assert typing_only_import.is_type_checking is True
        assert scm_import.is_type_checking is False


# ============================================================================
# 测试 rewrite_import_statement
# ============================================================================


class TestRewriteImportStatement:
    """测试 import 语句改写"""

    def test_simple_import_with_entry_point(self) -> None:
        """测试简单 import 改写（有入口点）"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="import scm_sync_runner",
            old_module="scm_sync_runner",
            import_type="import",
        )
        entry = MigrationEntry(
            old_module="scm_sync_runner",
            import_target="engram.logbook.cli.scm_sync:runner_main",
            cli_target="engram-scm-runner",
            deprecated=True,
            status="migrated",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert "from engram.logbook.cli.scm_sync import runner_main as scm_sync_runner" in new_line

    def test_simple_import_without_entry_point(self) -> None:
        """测试简单 import 改写（无入口点）"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="import scm_materialize_patch_blob",
            old_module="scm_materialize_patch_blob",
            import_type="import",
        )
        entry = MigrationEntry(
            old_module="scm_materialize_patch_blob",
            import_target="engram.logbook.materialize_patch_blob",
            cli_target=None,
            deprecated=True,
            status="wrapper_exists",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert (
            "import engram.logbook.materialize_patch_blob as scm_materialize_patch_blob" in new_line
        )

    def test_import_with_alias(self) -> None:
        """测试带别名的 import 改写"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="import db_migrate as dm",
            old_module="db_migrate",
            import_type="import",
            alias="dm",
        )
        entry = MigrationEntry(
            old_module="db_migrate",
            import_target="engram.logbook.cli.db_migrate:main",
            cli_target="engram-migrate",
            deprecated=True,
            status="wrapper_exists",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert "from engram.logbook.cli.db_migrate import main as dm" in new_line

    def test_from_import_main(self) -> None:
        """测试 from import main 改写"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="from artifact_cli import main",
            old_module="artifact_cli",
            import_type="from",
            imported_names=["main"],
        )
        entry = MigrationEntry(
            old_module="artifact_cli",
            import_target="engram.logbook.cli.artifacts:main",
            cli_target="engram-artifacts",
            deprecated=True,
            status="wrapper_exists",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert "from engram.logbook.cli.artifacts import main" in new_line

    def test_from_import_multiple_names(self) -> None:
        """测试 from import 多个名称"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="from db_bootstrap import main, create_roles",
            old_module="db_bootstrap",
            import_type="from",
            imported_names=["main", "create_roles"],
        )
        entry = MigrationEntry(
            old_module="db_bootstrap",
            import_target="engram.logbook.cli.db_bootstrap:main",
            cli_target="engram-bootstrap-roles",
            deprecated=True,
            status="wrapper_exists",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert "from engram.logbook.cli.db_bootstrap import main, create_roles" in new_line

    def test_no_import_target_skip(self) -> None:
        """测试无 import_target 时跳过"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="import scm_sync_gitlab_commits",
            old_module="scm_sync_gitlab_commits",
            import_type="import",
        )
        entry = MigrationEntry(
            old_module="scm_sync_gitlab_commits",
            import_target=None,  # 无 import 目标
            cli_target="engram-scm-sync runner incremental --repo gitlab:<id>",
            deprecated=True,
            status="migrated",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "skip"
        assert change["reason"] == "no import target"

    def test_preserve_indentation(self) -> None:
        """测试保留缩进"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="    import scm_sync_runner",
            old_module="scm_sync_runner",
            import_type="import",
        )
        entry = MigrationEntry(
            old_module="scm_sync_runner",
            import_target="engram.logbook.cli.scm_sync:runner_main",
            cli_target="engram-scm-runner",
            deprecated=True,
            status="migrated",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert new_line.startswith("    ")  # 保留4空格缩进

    def test_preserve_trailing_comment(self) -> None:
        """测试保留行尾注释"""
        match = ImportMatch(
            file_path="test.py",
            line_number=1,
            line_content="import scm_sync_runner  # legacy import",
            old_module="scm_sync_runner",
            import_type="import",
        )
        entry = MigrationEntry(
            old_module="scm_sync_runner",
            import_target="engram.logbook.cli.scm_sync:runner_main",
            cli_target="engram-scm-runner",
            deprecated=True,
            status="migrated",
        )
        new_line, change = rewrite_import_statement(match, entry, [])

        assert change["type"] == "rewrite"
        assert "# legacy import" in new_line


# ============================================================================
# 测试完整文件迁移
# ============================================================================


class TestMigrateFile:
    """测试完整文件迁移"""

    def test_migrate_single_import(self, tmp_path: Path) -> None:
        """测试迁移单个 import"""
        test_file = tmp_path / "test.py"
        test_file.write_text("import scm_sync_runner\n")

        entries = {
            "scm_sync_runner": MigrationEntry(
                old_module="scm_sync_runner",
                import_target="engram.logbook.cli.scm_sync:runner_main",
                cli_target="engram-scm-runner",
                deprecated=True,
                status="migrated",
            )
        }
        target_modules = {"scm_sync_runner"}

        result = migrate_file(test_file, "test.py", entries, target_modules)

        assert result.has_changes()
        assert (
            "from engram.logbook.cli.scm_sync import runner_main as scm_sync_runner"
            in result.modified_content
        )

    def test_skip_type_checking_imports(self, tmp_path: Path) -> None:
        """测试跳过 TYPE_CHECKING 块内的导入"""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import scm_sync_runner

# 这个应该被改
import db_migrate
""")

        entries = {
            "scm_sync_runner": MigrationEntry(
                old_module="scm_sync_runner",
                import_target="engram.logbook.cli.scm_sync:runner_main",
                cli_target="engram-scm-runner",
                deprecated=True,
                status="migrated",
            ),
            "db_migrate": MigrationEntry(
                old_module="db_migrate",
                import_target="engram.logbook.cli.db_migrate:main",
                cli_target="engram-migrate",
                deprecated=True,
                status="wrapper_exists",
            ),
        }
        target_modules = {"scm_sync_runner", "db_migrate"}

        result = migrate_file(test_file, "test.py", entries, target_modules)

        assert result.has_changes()
        # TYPE_CHECKING 内的不变
        assert "if TYPE_CHECKING:" in result.modified_content
        assert "import scm_sync_runner" in result.modified_content
        # 外部的被改了
        assert (
            "from engram.logbook.cli.db_migrate import main as db_migrate"
            in result.modified_content
        )

    def test_preserve_file_ending_newline(self, tmp_path: Path) -> None:
        """测试保留文件末尾换行符"""
        test_file = tmp_path / "test.py"
        test_file.write_text("import scm_sync_runner\n")

        entries = {
            "scm_sync_runner": MigrationEntry(
                old_module="scm_sync_runner",
                import_target="engram.logbook.cli.scm_sync:runner_main",
                cli_target="engram-scm-runner",
                deprecated=True,
                status="migrated",
            )
        }
        target_modules = {"scm_sync_runner"}

        result = migrate_file(test_file, "test.py", entries, target_modules)

        assert result.modified_content.endswith("\n")

    def test_no_changes_for_clean_file(self, tmp_path: Path) -> None:
        """测试无需修改的文件"""
        test_file = tmp_path / "test.py"
        test_file.write_text("import os\nimport sys\n")

        entries = {
            "scm_sync_runner": MigrationEntry(
                old_module="scm_sync_runner",
                import_target="engram.logbook.cli.scm_sync:runner_main",
                cli_target="engram-scm-runner",
                deprecated=True,
                status="migrated",
            )
        }
        target_modules = {"scm_sync_runner"}

        result = migrate_file(test_file, "test.py", entries, target_modules)

        assert not result.has_changes()

    def test_diff_output(self, tmp_path: Path) -> None:
        """测试 diff 输出"""
        test_file = tmp_path / "test.py"
        test_file.write_text("import scm_sync_runner\n")

        entries = {
            "scm_sync_runner": MigrationEntry(
                old_module="scm_sync_runner",
                import_target="engram.logbook.cli.scm_sync:runner_main",
                cli_target="engram-scm-runner",
                deprecated=True,
                status="migrated",
            )
        }
        target_modules = {"scm_sync_runner"}

        result = migrate_file(test_file, "test.py", entries, target_modules)
        diff = result.get_diff()

        assert "---" in diff
        assert "+++" in diff
        assert "-import scm_sync_runner" in diff
        assert "+from engram.logbook.cli.scm_sync import runner_main as scm_sync_runner" in diff


# ============================================================================
# 测试边缘情况
# ============================================================================


class TestEdgeCases:
    """边缘情况测试"""

    def test_comment_line_ignored(self) -> None:
        """测试注释行被忽略"""
        source = """
# import scm_sync_runner
# from db_migrate import main
"""
        target: Set[str] = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports(source, target)
        assert len(imports) == 0

    def test_nested_function_import(self) -> None:
        """测试嵌套函数中的导入"""
        source = """
def outer():
    def inner():
        import scm_sync_runner
"""
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        assert len(imports) == 1
        assert imports[0].line_number == 4

    def test_try_except_import(self) -> None:
        """测试 try-except 块中的导入"""
        source = """
try:
    import artifact_gc
except ImportError:
    artifact_gc = None
"""
        target: Set[str] = {"artifact_gc"}
        imports = extract_imports(source, target)
        assert len(imports) == 1

    def test_fstring_with_import(self) -> None:
        """测试 f-string 中包含 import 字样"""
        source = """
msg = f"Please import scm_sync_runner"
x = f"from {module} import {name}"
"""
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        assert len(imports) == 0

    def test_multiple_imports_same_line(self) -> None:
        """测试同一行多个导入"""
        source = "import scm_sync_runner, db_migrate"
        target: Set[str] = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports(source, target)
        assert len(imports) == 2

    def test_submodule_import_not_matched(self) -> None:
        """测试子模块导入不被匹配"""
        source = "from engram.logbook.scm_sync_runner import main"
        target: Set[str] = {"scm_sync_runner"}
        imports = extract_imports(source, target)
        # 顶层模块是 engram，不是 scm_sync_runner
        assert len(imports) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
