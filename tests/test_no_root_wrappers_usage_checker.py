#!/usr/bin/env python3
"""
check_no_root_wrappers_usage.py 脚本的单元测试

测试覆盖:
- 字符串内伪 import（docstring、字符串常量）
- TYPE_CHECKING 块内的导入
- 多行 from-import（括号包裹的导入列表）
- 正常违规检测
- 迁移映射表建议
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_no_root_wrappers_usage import (
    MIGRATION_MAP,
    ROOT_WRAPPER_MODULES,
    AllowlistEntry,
    CheckResult,
    InlineMarker,
    check_allowlist_match,
    extract_imports_via_ast,
    get_migration_suggestion,
    initialize_module_lists,
    load_allowlist,
    validate_allowlist_entry,
)

# ============================================================================
# 测试辅助函数（原脚本中不存在，仅用于测试）
# ============================================================================


def _get_scope(entry: dict) -> str:
    """从 allowlist entry 字典中提取 scope 字段"""
    return entry.get("scope", "import")


def _get_category(entry: dict) -> str:
    """从 allowlist entry 字典中提取 category 字段"""
    return entry.get("category", "other")


def _get_file_path_exact(entry: dict) -> str | None:
    """从 allowlist entry 字典中提取精确 file_path 字段"""
    return entry.get("file_path")


def _get_file_pattern(entry: dict) -> str | None:
    """从 allowlist entry 字典中获取文件模式

    优先级: file_glob > file_path > file_pattern
    """
    if "file_glob" in entry:
        return entry["file_glob"]
    if "file_path" in entry:
        return entry["file_path"]
    return entry.get("file_pattern")


def _get_expiry(entry: dict) -> str | None:
    """从 allowlist entry 字典中获取过期日期

    优先级: expires_on > expiry
    """
    if "expires_on" in entry:
        return entry["expires_on"]
    return entry.get("expiry")


# 初始化模块列表（需要在测试前完成）
_project_root = Path(__file__).resolve().parent.parent
initialize_module_lists(_project_root)


class TestExtractImportsViaAST:
    """测试 AST 导入提取功能"""

    def test_simple_import(self) -> None:
        """测试简单的 import 语句"""
        source = "import scm_sync_runner"
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].module == "scm_sync_runner"
        assert imports[0].line_number == 1

    def test_from_import(self) -> None:
        """测试 from ... import 语句"""
        source = "from scm_sync_runner import main"
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].module == "scm_sync_runner"

    def test_import_as(self) -> None:
        """测试 import ... as 语句"""
        source = "import db_migrate as dm"
        forbidden = {"db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].module == "db_migrate"

    def test_ignore_string_fake_import(self) -> None:
        """测试忽略字符串中的伪 import"""
        source = """
msg = "import scm_sync_runner"
x = 'from db_migrate import main'
"""
        forbidden = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
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
        forbidden = {"scm_sync_runner", "db_bootstrap"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 0

    def test_ignore_multiline_string_fake_import(self) -> None:
        """测试忽略多行字符串中的伪 import"""
        source = '''
code_example = """
import artifact_cli
from artifact_gc import cleanup
"""
'''
        forbidden = {"artifact_cli", "artifact_gc"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 0

    def test_type_checking_block_marked(self) -> None:
        """测试 TYPE_CHECKING 块内的导入被正确标记"""
        source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import scm_sync_runner
    from db_migrate import migrate
"""
        forbidden = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 2
        assert all(imp.is_type_checking for imp in imports)

    def test_type_checking_with_typing_prefix(self) -> None:
        """测试 typing.TYPE_CHECKING 形式"""
        source = """
import typing

if typing.TYPE_CHECKING:
    import logbook_cli
"""
        forbidden = {"logbook_cli"}
        imports = extract_imports_via_ast(source, forbidden)
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
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].module == "scm_sync_runner"
        assert imports[0].line_number == 2

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
        forbidden = {"artifact_cli"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].module == "artifact_cli"

    def test_mixed_imports(self) -> None:
        """测试混合导入场景"""
        source = """
import os
import scm_sync_runner
from pathlib import Path
from db_migrate import main
import json
"""
        forbidden = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 2
        modules = {imp.module for imp in imports}
        assert modules == {"scm_sync_runner", "db_migrate"}

    def test_allowed_import_not_detected(self) -> None:
        """测试非禁止模块不被检测"""
        source = """
import os
import sys
from pathlib import Path
from engram.logbook import scm_sync_runner
"""
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        # engram.logbook.scm_sync_runner 的顶层模块是 engram，不是 scm_sync_runner
        assert len(imports) == 0

    def test_syntax_error_returns_empty(self) -> None:
        """测试语法错误时返回空列表"""
        source = "import (syntax error here"
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
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
        forbidden = {"scm_sync_runner", "typing_only"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 2

        typing_only_import = next(imp for imp in imports if imp.module == "typing_only")
        scm_import = next(imp for imp in imports if imp.module == "scm_sync_runner")

        assert typing_only_import.is_type_checking is True
        assert scm_import.is_type_checking is False


class TestMigrationSuggestion:
    """测试迁移建议功能"""

    def test_known_module_suggestion(self) -> None:
        """测试已知模块的迁移建议"""
        suggestion = get_migration_suggestion("scm_sync_runner")
        assert "engram.logbook.cli.scm_sync" in suggestion or "engram-scm-runner" in suggestion

    def test_db_migrate_suggestion(self) -> None:
        """测试 db_migrate 的迁移建议"""
        suggestion = get_migration_suggestion("db_migrate")
        assert "engram-migrate" in suggestion or "engram.logbook.cli.db_migrate" in suggestion

    def test_artifact_cli_suggestion(self) -> None:
        """测试 artifact_cli 的迁移建议"""
        suggestion = get_migration_suggestion("artifact_cli")
        assert "engram-artifacts" in suggestion or "engram.logbook.cli.artifacts" in suggestion

    def test_unknown_module_fallback(self) -> None:
        """测试未知模块的回退建议"""
        suggestion = get_migration_suggestion("unknown_module")
        assert "engram.logbook" in suggestion or "engram.gateway" in suggestion

    def test_all_root_wrapper_modules_have_mapping(self) -> None:
        """测试所有 ROOT_WRAPPER_MODULES 都有迁移映射"""
        for module in ROOT_WRAPPER_MODULES:
            assert module in MIGRATION_MAP, f"模块 {module} 缺少迁移映射"


class TestEdgeCases:
    """边缘情况测试"""

    def test_comment_line_ignored(self) -> None:
        """测试注释行被忽略（AST 自动处理）"""
        source = """
# import scm_sync_runner
# from db_migrate import main
"""
        forbidden = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 0

    def test_nested_function_import(self) -> None:
        """测试嵌套函数中的导入"""
        source = """
def outer():
    def inner():
        import scm_sync_runner
"""
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].line_number == 4

    def test_class_level_import(self) -> None:
        """测试类级别的导入"""
        source = """
class MyClass:
    import db_bootstrap
"""
        forbidden = {"db_bootstrap"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1

    def test_try_except_import(self) -> None:
        """测试 try-except 块中的导入"""
        source = """
try:
    import artifact_gc
except ImportError:
    artifact_gc = None
"""
        forbidden = {"artifact_gc"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1

    def test_conditional_import(self) -> None:
        """测试条件导入（非 TYPE_CHECKING）"""
        source = """
import sys
if sys.version_info >= (3, 10):
    import logbook_cli_main
"""
        forbidden = {"logbook_cli_main"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].is_type_checking is False

    def test_multiple_imports_same_line(self) -> None:
        """测试同一行多个导入"""
        source = "import scm_sync_runner, db_migrate"
        forbidden = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 2

    def test_submodule_import_not_matched(self) -> None:
        """测试子模块导入不被匹配"""
        source = "from engram.logbook.scm_sync_runner import main"
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        # 顶层模块是 engram，不是 scm_sync_runner
        assert len(imports) == 0

    def test_fstring_with_import(self) -> None:
        """测试 f-string 中包含 import 字样"""
        source = """
msg = f"Please import scm_sync_runner"
x = f"from {module} import {name}"
"""
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 0


class TestTypeCheckingComplexScenarios:
    """TYPE_CHECKING 复杂场景测试"""

    def test_type_checking_with_else_branch(self) -> None:
        """测试 TYPE_CHECKING 带 else 分支"""
        source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import scm_sync_runner
else:
    scm_sync_runner = None
"""
        forbidden = {"scm_sync_runner"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].is_type_checking is True

    def test_nested_type_checking(self) -> None:
        """测试嵌套在函数中的 TYPE_CHECKING"""
        source = """
def foo():
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        import db_migrate
"""
        forbidden = {"db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 1
        assert imports[0].is_type_checking is True

    def test_multiple_type_checking_blocks(self) -> None:
        """测试多个 TYPE_CHECKING 块"""
        source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import scm_sync_runner

# 正常代码
x = 1

if TYPE_CHECKING:
    import db_migrate
"""
        forbidden = {"scm_sync_runner", "db_migrate"}
        imports = extract_imports_via_ast(source, forbidden)
        assert len(imports) == 2
        assert all(imp.is_type_checking for imp in imports)


class TestAllowlistFieldCompatibility:
    """测试 Allowlist 新旧字段兼容性"""

    def test_validate_entry_with_new_fields(self) -> None:
        """使用新字段名 file_glob/expires_on 的条目应通过校验"""
        entry = {
            "id": "test-entry",
            "file_glob": "tests/**/*.py",
            "module": "db",
            "owner": "@engram-team",
            "expires_on": "2030-12-31",
            "reason": "测试验证",
        }
        is_valid, missing = validate_allowlist_entry(entry)
        assert is_valid, f"应通过校验，但缺少字段: {missing}"
        assert len(missing) == 0

    def test_validate_entry_with_old_fields(self) -> None:
        """使用旧字段名 file_pattern/expiry 的条目应通过校验"""
        entry = {
            "id": "test-entry",
            "file_pattern": "tests/**/*.py",
            "module": "db",
            "owner": "@engram-team",
            "expiry": "2030-12-31",
            "reason": "测试验证",
        }
        is_valid, missing = validate_allowlist_entry(entry)
        assert is_valid, f"应通过校验，但缺少字段: {missing}"
        assert len(missing) == 0

    def test_validate_entry_with_file_path(self) -> None:
        """使用 file_path 字段的条目应通过校验"""
        entry = {
            "id": "test-entry",
            "file_path": "tests/specific_test.py",
            "module": "db",
            "owner": "@engram-team",
            "expires_on": "2030-12-31",
            "reason": "测试验证",
        }
        is_valid, missing = validate_allowlist_entry(entry)
        assert is_valid, f"应通过校验，但缺少字段: {missing}"

    def test_validate_entry_missing_file_pattern(self) -> None:
        """缺少 file_pattern/file_glob/file_path 时应报告缺失"""
        entry = {
            "id": "test-entry",
            "module": "db",
            "owner": "@engram-team",
            "expires_on": "2030-12-31",
            "reason": "测试验证",
        }
        is_valid, missing = validate_allowlist_entry(entry)
        assert not is_valid
        assert "file_pattern" in missing

    def test_get_file_pattern_new_field(self) -> None:
        """_get_file_pattern 应优先返回 file_glob"""
        entry = {"file_glob": "new/*.py", "file_pattern": "old/*.py"}
        assert _get_file_pattern(entry) == "new/*.py"

    def test_get_file_pattern_file_path_priority(self) -> None:
        """_get_file_pattern 应在 file_glob 不存在时返回 file_path"""
        entry = {"file_path": "specific.py", "file_pattern": "old/*.py"}
        assert _get_file_pattern(entry) == "specific.py"

    def test_get_file_pattern_fallback_to_old(self) -> None:
        """_get_file_pattern 应在新字段不存在时返回 file_pattern"""
        entry = {"file_pattern": "old/*.py"}
        assert _get_file_pattern(entry) == "old/*.py"

    def test_get_expiry_new_field(self) -> None:
        """_get_expiry 应优先返回 expires_on"""
        entry = {"expires_on": "2030-01-01", "expiry": "2029-01-01"}
        assert _get_expiry(entry) == "2030-01-01"

    def test_get_expiry_fallback_to_old(self) -> None:
        """_get_expiry 应在新字段不存在时返回 expiry"""
        entry = {"expiry": "2029-01-01"}
        assert _get_expiry(entry) == "2029-01-01"


class TestAllowlistLoading:
    """测试 Allowlist 加载功能"""

    def test_load_allowlist_with_new_fields(self, tmp_path: Path) -> None:
        """使用新字段格式的 allowlist 应能正确加载"""
        import json
        from datetime import date, timedelta

        future_date = (date.today() + timedelta(days=365)).isoformat()

        allowlist_data = {
            "version": "1.0",
            "entries": [
                {
                    "id": "test-new-fields",
                    "file_glob": "tests/**/*.py",
                    "module": "scm_sync_runner",
                    "owner": "@engram-team",
                    "expires_on": future_date,
                    "reason": "测试新字段格式",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps(allowlist_data), encoding="utf-8")

        result = CheckResult()
        entries = load_allowlist(allowlist_file, result, verbose=False)

        assert "test-new-fields" in entries
        entry = entries["test-new-fields"]
        assert entry.file_pattern == "tests/**/*.py"
        assert entry.expiry == future_date
        assert entry.module == "scm_sync_runner"

    def test_load_allowlist_version_1_0(self, tmp_path: Path) -> None:
        """version 为 '1.0' 的 allowlist 应能正确加载"""
        import json
        from datetime import date, timedelta

        future_date = (date.today() + timedelta(days=365)).isoformat()

        allowlist_data = {
            "version": "1.0",  # 新版本号
            "entries": [
                {
                    "id": "test-v1-0",
                    "file_glob": "tests/*.py",
                    "module": "db_migrate",
                    "owner": "@team",
                    "expires_on": future_date,
                    "reason": "测试 version 1.0",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps(allowlist_data), encoding="utf-8")

        result = CheckResult()
        entries = load_allowlist(allowlist_file, result, verbose=False)

        assert "test-v1-0" in entries

    def test_load_allowlist_mixed_fields(self, tmp_path: Path) -> None:
        """混合使用新旧字段的 allowlist 应能正确加载"""
        import json
        from datetime import date, timedelta

        future_date = (date.today() + timedelta(days=365)).isoformat()

        allowlist_data = {
            "version": "1",
            "entries": [
                {
                    "id": "old-format",
                    "file_pattern": "tests/old/*.py",
                    "module": "artifact_cli",
                    "owner": "@old-team",
                    "expiry": future_date,
                    "reason": "旧格式条目",
                },
                {
                    "id": "new-format",
                    "file_glob": "tests/new/*.py",
                    "module": "logbook_cli",
                    "owner": "@new-team",
                    "expires_on": future_date,
                    "reason": "新格式条目",
                },
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps(allowlist_data), encoding="utf-8")

        result = CheckResult()
        entries = load_allowlist(allowlist_file, result, verbose=False)

        assert len(entries) == 2
        assert "old-format" in entries
        assert "new-format" in entries

        # 验证字段正确映射
        assert entries["old-format"].file_pattern == "tests/old/*.py"
        assert entries["new-format"].file_pattern == "tests/new/*.py"


class TestAllowlistEntryMatching:
    """测试 AllowlistEntry 的匹配功能"""

    def test_entry_matches_with_new_fields(self) -> None:
        """使用新字段创建的条目应能正确匹配"""
        entry = AllowlistEntry(
            id="test-match",
            file_pattern="tests/**/*.py",
            module="scm_sync_runner",
            owner="@team",
            expiry="2030-12-31",
            reason="测试匹配",
        )

        # 应匹配
        assert entry.matches("tests/unit/test_foo.py", "scm_sync_runner")
        assert entry.matches("tests/integration/test_bar.py", "scm_sync_runner")

        # 不应匹配
        assert not entry.matches("src/main.py", "scm_sync_runner")  # 路径不匹配
        assert not entry.matches("tests/unit/test_foo.py", "other_module")  # 模块不匹配


class TestSchemaV1Fields:
    """测试 Schema V1 新字段支持"""

    def test_scope_field_default(self) -> None:
        """scope 字段默认值应为 'import'"""
        entry = AllowlistEntry(
            id="test-scope",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="测试 scope",
        )
        assert entry.scope == "import"

    def test_scope_field_subprocess(self) -> None:
        """scope 字段可设置为 'subprocess'"""
        entry = AllowlistEntry(
            id="test-scope",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="测试 scope",
            scope="subprocess",
        )
        assert entry.scope == "subprocess"

    def test_category_field_default(self) -> None:
        """category 字段默认值应为 'other'"""
        entry = AllowlistEntry(
            id="test-category",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="测试 category",
        )
        assert entry.category == "other"

    def test_category_field_acceptance_test(self) -> None:
        """category 字段可设置为 'acceptance_test'"""
        entry = AllowlistEntry(
            id="test-category",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="测试 category",
            category="acceptance_test",
        )
        assert entry.category == "acceptance_test"

    def test_file_path_exact_matching(self) -> None:
        """file_path_exact 应优先于 glob 模式进行精确匹配"""
        entry = AllowlistEntry(
            id="test-exact",
            file_pattern="tests/**/*.py",  # glob 会匹配很多文件
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="测试精确匹配",
            file_path_exact="tests/specific/test_file.py",  # 精确匹配
        )

        # 只应匹配精确路径
        assert entry.matches("tests/specific/test_file.py", "db")
        # 不应匹配其他 glob 能匹配的路径
        assert not entry.matches("tests/other/test_other.py", "db")

    def test_file_path_exact_none_uses_glob(self) -> None:
        """file_path_exact 为 None 时应使用 glob 模式"""
        entry = AllowlistEntry(
            id="test-glob",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="测试 glob",
            file_path_exact=None,
        )

        # 应匹配 glob 模式
        assert entry.matches("tests/unit/test_foo.py", "db")
        assert entry.matches("tests/integration/test_bar.py", "db")

    def test_get_scope_helper(self) -> None:
        """_get_scope 应正确提取 scope 字段"""
        assert _get_scope({"scope": "import"}) == "import"
        assert _get_scope({"scope": "subprocess"}) == "subprocess"
        assert _get_scope({}) == "import"  # 默认值

    def test_get_category_helper(self) -> None:
        """_get_category 应正确提取 category 字段"""
        assert _get_category({"category": "acceptance_test"}) == "acceptance_test"
        assert _get_category({"category": "legacy_migration"}) == "legacy_migration"
        assert _get_category({}) == "other"  # 默认值

    def test_get_file_path_exact_helper(self) -> None:
        """_get_file_path_exact 应正确提取 file_path 字段"""
        assert _get_file_path_exact({"file_path": "tests/specific.py"}) == "tests/specific.py"
        assert _get_file_path_exact({}) is None
        assert _get_file_path_exact({"file_glob": "tests/**/*.py"}) is None


class TestScopeFiltering:
    """测试 scope 过滤功能"""

    def test_check_allowlist_match_filters_by_scope(self) -> None:
        """check_allowlist_match 应只匹配指定 scope 的条目"""
        import_entry = AllowlistEntry(
            id="import-entry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="import 例外",
            scope="import",
        )
        subprocess_entry = AllowlistEntry(
            id="subprocess-entry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="subprocess 例外",
            scope="subprocess",
        )

        allowlist = {
            "import-entry": import_entry,
            "subprocess-entry": subprocess_entry,
        }

        # 默认 scope 为 import，应只匹配 import_entry
        result = check_allowlist_match("tests/unit/test_foo.py", "db", None, allowlist)
        assert result is not None
        assert result.id == "import-entry"

        # 显式指定 scope=subprocess，应只匹配 subprocess_entry
        result = check_allowlist_match(
            "tests/unit/test_foo.py", "db", None, allowlist, scope="subprocess"
        )
        assert result is not None
        assert result.id == "subprocess-entry"

    def test_check_allowlist_match_with_marker_respects_scope(self) -> None:
        """使用 marker_id 时也应检查 scope"""
        entry = AllowlistEntry(
            id="subprocess-only",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry="2030-12-31",
            reason="subprocess 例外",
            scope="subprocess",
        )

        allowlist = {"subprocess-only": entry}

        # marker_id 匹配但 scope 不匹配，应返回 None
        result = check_allowlist_match(
            "tests/unit/test_foo.py", "db", "subprocess-only", allowlist, scope="import"
        )
        assert result is None

        # marker_id 和 scope 都匹配
        result = check_allowlist_match(
            "tests/unit/test_foo.py", "db", "subprocess-only", allowlist, scope="subprocess"
        )
        assert result is not None
        assert result.id == "subprocess-only"


class TestExpirySemantics:
    """测试过期语义"""

    def test_today_is_not_expired(self) -> None:
        """今天日期应视为有效（未过期）"""
        from datetime import date

        today = date.today().isoformat()
        entry = AllowlistEntry(
            id="test-expiry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry=today,
            reason="测试过期",
        )

        # 今天不应被视为过期
        assert not entry.is_expired()

    def test_yesterday_is_expired(self) -> None:
        """昨天日期应视为过期"""
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        entry = AllowlistEntry(
            id="test-expiry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry=yesterday,
            reason="测试过期",
        )

        # 昨天应被视为过期
        assert entry.is_expired()

    def test_tomorrow_is_not_expired(self) -> None:
        """明天日期应视为有效"""
        from datetime import date, timedelta

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        entry = AllowlistEntry(
            id="test-expiry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry=tomorrow,
            reason="测试过期",
        )

        # 明天不应被视为过期
        assert not entry.is_expired()


class TestExpirySemanticsWithTodayInjection:
    """测试过期语义 - today 参数注入"""

    def test_allowlist_entry_today_equals_expires_not_expired(self) -> None:
        """AllowlistEntry: today == expires 应视为有效（边界条件）"""
        from datetime import date

        expires_date = date(2026, 6, 15)
        entry = AllowlistEntry(
            id="test-expiry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry=expires_date.isoformat(),
            reason="测试过期边界",
        )

        # today == expires 应仍有效
        assert not entry.is_expired(today=expires_date)

    def test_allowlist_entry_today_greater_than_expires_is_expired(self) -> None:
        """AllowlistEntry: today > expires 应视为过期"""
        from datetime import date, timedelta

        expires_date = date(2026, 6, 15)
        entry = AllowlistEntry(
            id="test-expiry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry=expires_date.isoformat(),
            reason="测试过期边界",
        )

        # today > expires 应过期
        assert entry.is_expired(today=expires_date + timedelta(days=1))

    def test_allowlist_entry_today_less_than_expires_not_expired(self) -> None:
        """AllowlistEntry: today < expires 应视为有效"""
        from datetime import date, timedelta

        expires_date = date(2026, 6, 15)
        entry = AllowlistEntry(
            id="test-expiry",
            file_pattern="tests/**/*.py",
            module="db",
            owner="@team",
            expiry=expires_date.isoformat(),
            reason="测试过期边界",
        )

        # today < expires 应仍有效
        assert not entry.is_expired(today=expires_date - timedelta(days=1))

    def test_inline_marker_today_equals_expires_not_expired(self) -> None:
        """InlineMarker: today == expires 应视为有效（边界条件）"""
        from datetime import date

        expires_date = date(2026, 6, 15)
        marker = InlineMarker(
            reason="测试边界",
            expires=expires_date.isoformat(),
            owner="@team",
        )

        # today == expires 应仍有效
        assert not marker.is_expired(today=expires_date)

    def test_inline_marker_today_greater_than_expires_is_expired(self) -> None:
        """InlineMarker: today > expires 应视为过期"""
        from datetime import date, timedelta

        expires_date = date(2026, 6, 15)
        marker = InlineMarker(
            reason="测试边界",
            expires=expires_date.isoformat(),
            owner="@team",
        )

        # today > expires 应过期
        assert marker.is_expired(today=expires_date + timedelta(days=1))

    def test_inline_marker_today_less_than_expires_not_expired(self) -> None:
        """InlineMarker: today < expires 应视为有效"""
        from datetime import date, timedelta

        expires_date = date(2026, 6, 15)
        marker = InlineMarker(
            reason="测试边界",
            expires=expires_date.isoformat(),
            owner="@team",
        )

        # today < expires 应仍有效
        assert not marker.is_expired(today=expires_date - timedelta(days=1))

    def test_inline_marker_invalid_date_is_expired(self) -> None:
        """InlineMarker: 无效日期格式应视为过期"""
        from datetime import date

        marker = InlineMarker(
            reason="测试无效日期",
            expires="invalid-date",
            owner="@team",
        )

        # 无效日期格式应视为过期
        assert marker.is_expired(today=date(2026, 1, 1))


class TestLoadAllowlistWithSchemaV1:
    """测试加载 Schema V1 格式的 allowlist"""

    def test_load_allowlist_with_all_v1_fields(self, tmp_path: Path) -> None:
        """使用所有 V1 字段的 allowlist 应能正确加载"""
        import json
        from datetime import date, timedelta

        future_date = (date.today() + timedelta(days=365)).isoformat()

        allowlist_data = {
            "version": "1.0",
            "entries": [
                {
                    "id": "test-v1-full",
                    "scope": "import",
                    "module": "scm_sync_runner",
                    "file_glob": "tests/**/*.py",
                    "reason": "测试 V1 完整字段",
                    "owner": "@engram-team",
                    "expires_on": future_date,
                    "category": "acceptance_test",
                    "created_at": "2026-01-01",
                    "jira_ticket": "ENG-123",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps(allowlist_data), encoding="utf-8")

        result = CheckResult()
        entries = load_allowlist(allowlist_file, result, verbose=False)

        assert "test-v1-full" in entries
        entry = entries["test-v1-full"]
        assert entry.scope == "import"
        assert entry.category == "acceptance_test"
        assert entry.file_pattern == "tests/**/*.py"
        assert entry.expiry == future_date
        assert entry.ticket == "ENG-123"

    def test_load_allowlist_with_file_path(self, tmp_path: Path) -> None:
        """使用 file_path 字段的 allowlist 应能正确加载"""
        import json
        from datetime import date, timedelta

        future_date = (date.today() + timedelta(days=365)).isoformat()

        allowlist_data = {
            "version": "1.0",
            "entries": [
                {
                    "id": "test-file-path",
                    "scope": "import",
                    "module": "db_migrate",
                    "file_path": "tests/specific/test_file.py",
                    "reason": "测试 file_path 精确匹配",
                    "owner": "@engram-team",
                    "expires_on": future_date,
                    "category": "integration_test",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps(allowlist_data), encoding="utf-8")

        result = CheckResult()
        entries = load_allowlist(allowlist_file, result, verbose=False)

        assert "test-file-path" in entries
        entry = entries["test-file-path"]
        assert entry.file_path_exact == "tests/specific/test_file.py"
        assert entry.file_pattern == "tests/specific/test_file.py"  # 也存储在 file_pattern 中

        # 验证匹配行为
        assert entry.matches("tests/specific/test_file.py", "db_migrate")
        assert not entry.matches("tests/other/test_file.py", "db_migrate")

    def test_load_allowlist_subprocess_scope(self, tmp_path: Path) -> None:
        """scope 为 subprocess 的条目应正确加载"""
        import json
        from datetime import date, timedelta

        future_date = (date.today() + timedelta(days=365)).isoformat()

        allowlist_data = {
            "version": "1.0",
            "entries": [
                {
                    "id": "test-subprocess",
                    "scope": "subprocess",
                    "module": "artifact_cli",
                    "file_glob": "scripts/**/*.py",
                    "reason": "CI 脚本调用",
                    "owner": "@ci-team",
                    "expires_on": future_date,
                    "category": "tooling",
                }
            ],
        }

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps(allowlist_data), encoding="utf-8")

        result = CheckResult()
        entries = load_allowlist(allowlist_file, result, verbose=False)

        assert "test-subprocess" in entries
        entry = entries["test-subprocess"]
        assert entry.scope == "subprocess"
        assert entry.category == "tooling"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
