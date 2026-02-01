"""
CI 测试隔离检查脚本测试

测试 scripts/ci/check_ci_test_isolation.py 的功能，确保：
1. 正例：符合规范的代码通过检查
2. 负例：模块级 sys.path 污染和顶层模块导入被检测
3. dual-mode import 模式被正确检测

与 CI job 对应关系：
- 本测试对应 CI workflow 中的 ci-test-isolation-check 相关检查
- 门禁脚本路径: scripts/ci/check_ci_test_isolation.py

运行方式：
    pytest tests/ci/test_check_ci_test_isolation.py -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from scripts.ci.check_ci_test_isolation import (
    FORBIDDEN_TOPLEVEL_MODULES,
    Allowlist,
    AllowlistEntry,
    CheckResult,
    check_all,
    check_file,
    check_file_hygiene,
    load_allowlist,
)
from tests.ci.helpers.subprocess_env import get_minimal_subprocess_env, get_subprocess_env

# ============================================================================
# 辅助函数
# ============================================================================


def create_temp_file(content: str, suffix: str = ".py") -> Path:
    """创建临时 Python 文件并返回路径"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(content)
        return Path(f.name)


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent.resolve()


# ============================================================================
# 正例测试：验证符合规范的代码通过检查
# ============================================================================


class TestCleanCodePasses:
    """验证符合规范的代码通过检查"""

    def test_empty_file_passes(self) -> None:
        """空文件应通过检查"""
        file_path = create_temp_file("")
        try:
            violations = check_file(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_normal_imports_pass(self) -> None:
        """普通导入应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            import os
            import sys
            from pathlib import Path
            from typing import Any

            import pytest

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_scripts_ci_namespace_import_passes(self) -> None:
        """通过 scripts.ci 命名空间导入应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            from scripts.ci.validate_workflows import validate_contract
            from scripts.ci.workflow_contract_common import load_contract

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_sys_path_in_function_passes(self) -> None:
        """函数内的 sys.path 操作应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            import sys

            def setup_path():
                sys.path.insert(0, "/some/path")
                return sys.path

            def cleanup_path():
                sys.path.append("/another/path")
                sys.path.remove("/another/path")
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_sys_path_in_fixture_passes(self) -> None:
        """fixture 内的 sys.path 操作应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            import sys
            import pytest

            @pytest.fixture
            def setup_path():
                original = sys.path.copy()
                sys.path.insert(0, "/some/path")
                yield
                sys.path[:] = original

            def test_with_path(setup_path):
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_sys_path_in_class_method_passes(self) -> None:
        """类方法内的 sys.path 操作应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            import sys

            class TestSomething:
                def setup_method(self):
                    sys.path.insert(0, "/some/path")

                def teardown_method(self):
                    sys.path.remove("/some/path")

                def test_something(self):
                    pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()


# ============================================================================
# 负例测试：验证违规代码被检测
# ============================================================================


class TestSysPathViolationDetection:
    """验证模块级 sys.path 操作被检测"""

    def test_module_level_sys_path_insert_detected(self) -> None:
        """模块级 sys.path.insert 应被检测"""
        content = dedent("""\
            from __future__ import annotations

            import sys

            # 违规：模块级 sys.path.insert
            sys.path.insert(0, "/some/path")

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 1
            assert violations[0].violation_type == "sys_path_insert"
            assert "sys.path.insert" in violations[0].message
        finally:
            file_path.unlink()

    def test_module_level_sys_path_append_detected(self) -> None:
        """模块级 sys.path.append 应被检测"""
        content = dedent("""\
            from __future__ import annotations

            import sys

            # 违规：模块级 sys.path.append
            sys.path.append("/some/path")

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 1
            assert violations[0].violation_type == "sys_path_append"
            assert "sys.path.append" in violations[0].message
        finally:
            file_path.unlink()

    def test_multiple_sys_path_violations_detected(self) -> None:
        """多个 sys.path 违规应全部被检测"""
        content = dedent("""\
            from __future__ import annotations

            import sys

            # 违规 1
            sys.path.insert(0, "/path1")
            # 违规 2
            sys.path.append("/path2")
            # 违规 3
            sys.path.insert(0, "/path3")

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 3
            types = {v.violation_type for v in violations}
            assert "sys_path_insert" in types
            assert "sys_path_append" in types
        finally:
            file_path.unlink()


class TestToplevelImportViolationDetection:
    """验证顶层模块导入被检测"""

    def test_direct_import_toplevel_module_detected(self) -> None:
        """import validate_workflows 应被检测"""
        content = dedent("""\
            from __future__ import annotations

            # 违规：直接导入顶层模块
            import validate_workflows

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 1
            assert violations[0].violation_type == "toplevel_import"
            assert "validate_workflows" in violations[0].message
        finally:
            file_path.unlink()

    def test_from_toplevel_module_import_detected(self) -> None:
        """from validate_workflows import ... 应被检测"""
        content = dedent("""\
            from __future__ import annotations

            # 违规：从顶层模块导入
            from validate_workflows import validate_contract

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 1
            assert violations[0].violation_type == "toplevel_import"
            assert "validate_workflows" in violations[0].message
        finally:
            file_path.unlink()

    def test_multiple_toplevel_imports_detected(self) -> None:
        """多个顶层模块导入应全部被检测"""
        content = dedent("""\
            from __future__ import annotations

            # 违规：多个顶层模块导入
            import validate_workflows
            from workflow_contract_common import load_contract
            from check_gateway_di_boundaries import check_handler

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 3
            modules = {v.message.split(":")[-1].strip().split()[0] for v in violations}
            assert "validate_workflows" in modules or any(
                "validate_workflows" in v.message for v in violations
            )
        finally:
            file_path.unlink()

    def test_import_in_function_allowed(self) -> None:
        """函数内的顶层模块导入应被允许（延迟导入）"""
        content = dedent("""\
            from __future__ import annotations

            def test_something():
                # 函数内导入是允许的（延迟执行）
                import validate_workflows
                from workflow_contract_common import load_contract
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 0, f"函数内导入不应触发违规: {violations}"
        finally:
            file_path.unlink()


class TestMixedViolations:
    """测试混合违规场景"""

    def test_both_sys_path_and_import_violations(self) -> None:
        """同时存在 sys.path 和导入违规应全部被检测"""
        content = dedent("""\
            from __future__ import annotations

            import sys

            # 违规 1: sys.path
            sys.path.insert(0, "/scripts/ci")

            # 违规 2: 顶层导入
            import validate_workflows

            # 违规 3: from 导入
            from workflow_contract_common import load_contract

            def test_something():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 3

            violation_types = {v.violation_type for v in violations}
            assert "sys_path_insert" in violation_types
            assert "toplevel_import" in violation_types
        finally:
            file_path.unlink()


# ============================================================================
# Dual-mode Import 检测测试
# ============================================================================


class TestDualModeImportDetection:
    """验证 dual-mode import 模式被检测"""

    def test_dual_mode_import_fallback_detected(self) -> None:
        """try/except ImportError 中回退到顶层名导入应被检测"""
        content = dedent("""\
            from __future__ import annotations

            try:
                from scripts.ci.validate_workflows import validate_contract
            except ImportError:
                # 违规: 回退到顶层名导入
                from validate_workflows import validate_contract

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)

            assert len(violations) == 1
            assert violations[0].violation_type == "dual_mode_import"
            assert "validate_workflows" in violations[0].message
            assert violations[0].suggested_fix is not None
            assert "scripts.ci.validate_workflows" in violations[0].suggested_fix
        finally:
            file_path.unlink()

    def test_dual_mode_import_direct_import_detected(self) -> None:
        """try/except ImportError 中 import 顶层名应被检测"""
        content = dedent("""\
            from __future__ import annotations

            try:
                from scripts.ci.workflow_contract_common import load_contract
            except ImportError:
                # 违规: 回退到顶层名导入
                import workflow_contract_common

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)

            assert len(violations) == 1
            assert violations[0].violation_type == "dual_mode_import"
            assert "workflow_contract_common" in violations[0].message
        finally:
            file_path.unlink()

    def test_legitimate_import_error_handling_allowed(self) -> None:
        """合法的 ImportError 处理应被允许"""
        content = dedent("""\
            from __future__ import annotations

            # 合法: 处理可选依赖
            try:
                from jsonschema import Draft7Validator
                HAS_JSONSCHEMA = True
            except ImportError:
                HAS_JSONSCHEMA = False

            # 合法: tomlib/tomli 兼容
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)

            # 只有非禁止列表中的模块应被允许
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_multiple_dual_mode_imports_detected(self) -> None:
        """多个 dual-mode import 模式应全部被检测"""
        content = dedent("""\
            from __future__ import annotations

            try:
                from scripts.ci.validate_workflows import validate
            except ImportError:
                from validate_workflows import validate

            try:
                from scripts.ci._date_utils import utc_today
            except ImportError:
                from _date_utils import utc_today

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)

            assert len(violations) == 2
            violation_modules = [v.message for v in violations]
            assert any("validate_workflows" in m for m in violation_modules)
            assert any("_date_utils" in m for m in violation_modules)
        finally:
            file_path.unlink()

    def test_suggested_fix_included(self) -> None:
        """违规应包含建议的修复代码"""
        content = dedent("""\
            from __future__ import annotations

            try:
                from scripts.ci.workflow_contract_common import load_contract, parse_yaml
            except ImportError:
                from workflow_contract_common import load_contract, parse_yaml

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)

            assert len(violations) == 1
            assert violations[0].suggested_fix is not None
            assert "from scripts.ci.workflow_contract_common import" in violations[0].suggested_fix
            assert "load_contract" in violations[0].suggested_fix
            assert "parse_yaml" in violations[0].suggested_fix
        finally:
            file_path.unlink()


class TestCleanScriptsPasses:
    """验证符合规范的 scripts/ci 代码通过检查"""

    def test_correct_namespace_import_passes(self) -> None:
        """正确的命名空间导入应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            from scripts.ci.workflow_contract_common import load_contract
            from scripts.ci.validate_workflows import validate

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_optional_dependency_handling_passes(self) -> None:
        """可选依赖的 ImportError 处理应通过检查"""
        content = dedent("""\
            from __future__ import annotations

            try:
                import yaml
                HAS_YAML = True
            except ImportError:
                HAS_YAML = False

            try:
                from jsonschema import validate
            except ImportError:
                validate = None

            def main():
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file_hygiene(file_path)
            assert len(violations) == 0
        finally:
            file_path.unlink()


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_nonexistent_file(self) -> None:
        """不存在的文件应返回空结果"""
        file_path = Path("/nonexistent/path/to/file.py")
        violations = check_file(file_path)
        assert len(violations) == 0

    def test_syntax_error_file_skipped(self) -> None:
        """语法错误的文件应被跳过"""
        content = dedent("""\
            from __future__ import annotations

            # 语法错误
            def test_something(
                pass
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)
            # 语法错误文件跳过，返回空
            assert len(violations) == 0
        finally:
            file_path.unlink()

    def test_fix_hint_included(self) -> None:
        """违规应包含修复建议"""
        content = dedent("""\
            from __future__ import annotations

            from validate_workflows import validate_contract
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 1
            assert violations[0].fix_hint
            assert "scripts.ci" in violations[0].fix_hint
        finally:
            file_path.unlink()

    def test_code_snippet_included(self) -> None:
        """违规应包含代码片段"""
        content = dedent("""\
            from __future__ import annotations

            import validate_workflows
        """)
        file_path = create_temp_file(content)
        try:
            violations = check_file(file_path)

            assert len(violations) == 1
            assert violations[0].code_snippet
            assert "validate_workflows" in violations[0].code_snippet
        finally:
            file_path.unlink()


# ============================================================================
# CheckResult 测试
# ============================================================================


class TestCheckResult:
    """测试 CheckResult 数据结构"""

    def test_empty_result(self) -> None:
        """空结果应正确处理"""
        result = CheckResult()

        assert not result.has_violations()
        assert result.files_checked == 0
        assert result.files_with_violations == 0

        output = result.to_dict()
        assert output["ok"] is True
        assert output["summary"]["violation_count"] == 0

    def test_result_with_violations(self) -> None:
        """包含违规的结果应正确处理"""
        from scripts.ci.check_ci_test_isolation import Violation

        result = CheckResult(
            violations=[
                Violation(
                    file_path="tests/ci/test_foo.py",
                    line_number=10,
                    violation_type="sys_path_insert",
                    message="测试消息",
                    code_snippet="sys.path.insert(0, '/path')",
                    fix_hint="修复建议",
                    suggested_fix="# 移除 sys.path 操作",
                ),
            ],
            files_checked=5,
            files_with_violations=1,
            tests_files_checked=5,
            scripts_files_checked=0,
        )

        assert result.has_violations()

        output = result.to_dict()
        assert output["ok"] is False
        assert output["summary"]["violation_count"] == 1
        assert output["summary"]["files_checked"] == 5
        assert output["summary"]["files_with_violations"] == 1
        assert output["summary"]["tests_files_checked"] == 5
        assert output["summary"]["scripts_files_checked"] == 0
        assert len(output["violations"]) == 1
        assert output["violations"][0]["line_number"] == 10
        assert output["violations"][0]["suggested_fix"] == "# 移除 sys.path 操作"

    def test_violations_by_file_grouping(self) -> None:
        """验证按文件分组输出"""
        from scripts.ci.check_ci_test_isolation import Violation

        result = CheckResult(
            violations=[
                Violation(
                    file_path="tests/ci/test_foo.py",
                    line_number=10,
                    violation_type="sys_path_insert",
                    message="消息1",
                    code_snippet="代码1",
                    fix_hint="建议1",
                    suggested_fix="修复1",
                ),
                Violation(
                    file_path="tests/ci/test_foo.py",
                    line_number=20,
                    violation_type="toplevel_import",
                    message="消息2",
                    code_snippet="代码2",
                    fix_hint="建议2",
                    suggested_fix="修复2",
                ),
                Violation(
                    file_path="tests/ci/test_bar.py",
                    line_number=5,
                    violation_type="toplevel_import",
                    message="消息3",
                    code_snippet="代码3",
                    fix_hint="建议3",
                    suggested_fix="修复3",
                ),
            ],
            files_checked=10,
            files_with_violations=2,
        )

        output = result.to_dict()
        assert "violations_by_file" in output
        assert len(output["violations_by_file"]) == 2
        assert "tests/ci/test_foo.py" in output["violations_by_file"]
        assert "tests/ci/test_bar.py" in output["violations_by_file"]
        assert len(output["violations_by_file"]["tests/ci/test_foo.py"]) == 2
        assert len(output["violations_by_file"]["tests/ci/test_bar.py"]) == 1


# ============================================================================
# 配置验证测试
# ============================================================================


class TestConfiguration:
    """验证配置正确性"""

    def test_forbidden_modules_not_empty(self) -> None:
        """FORBIDDEN_TOPLEVEL_MODULES 不应为空"""
        assert len(FORBIDDEN_TOPLEVEL_MODULES) > 0

    def test_validate_workflows_in_forbidden(self) -> None:
        """validate_workflows 应在禁止列表中"""
        assert "validate_workflows" in FORBIDDEN_TOPLEVEL_MODULES

    def test_common_ci_modules_in_forbidden(self) -> None:
        """常见 CI 模块应在禁止列表中"""
        expected_modules = [
            "validate_workflows",
            "workflow_contract_common",
            "check_gateway_di_boundaries",
            "check_mypy_gate",
            "_date_utils",
        ]
        for module in expected_modules:
            assert module in FORBIDDEN_TOPLEVEL_MODULES, (
                f"期望 {module} 在 FORBIDDEN_TOPLEVEL_MODULES 中"
            )


# ============================================================================
# 子进程测试
# ============================================================================


class TestScriptSubprocess:
    """测试通过子进程运行脚本"""

    def test_script_help(self) -> None:
        """脚本应支持 --help 参数"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_ci_test_isolation.py"

        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env=get_subprocess_env(project_root),
        )

        assert result.returncode == 0
        assert "usage:" in result.stdout.lower() or "--json" in result.stdout
        # 验证新增的命令行参数在帮助中
        assert "--scan-tests" in result.stdout or "--scan-scripts" in result.stdout

    def test_script_json_output(self) -> None:
        """脚本应支持 --json 输出"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_ci_test_isolation.py"

        # 创建临时目录作为扫描目标（确保没有违规）
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个干净的测试文件
            clean_file = Path(tmpdir) / "test_clean.py"
            clean_file.write_text(
                dedent("""\
                from __future__ import annotations

                def test_something():
                    pass
            """)
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--scan-dir",
                    tmpdir,
                    "--json",
                ],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                env=get_subprocess_env(project_root),
            )

            assert result.returncode == 0

            import json

            output = json.loads(result.stdout)
            assert output["ok"] is True
            assert output["summary"]["violation_count"] == 0

    def test_script_scan_tests_mode(self) -> None:
        """脚本应支持 --scan-tests 模式"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_ci_test_isolation.py"

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个干净的测试文件
            clean_file = Path(tmpdir) / "test_clean.py"
            clean_file.write_text(
                dedent("""\
                from __future__ import annotations

                def test_something():
                    pass
            """)
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--scan-tests",
                    "--tests-dir",
                    tmpdir,
                    "--json",
                ],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                env=get_subprocess_env(project_root),
            )

            assert result.returncode == 0

            import json

            output = json.loads(result.stdout)
            assert output["ok"] is True
            assert output["summary"]["tests_files_checked"] == 1
            assert output["summary"]["scripts_files_checked"] == 0

    def test_script_scan_scripts_mode(self) -> None:
        """脚本应支持 --scan-scripts 模式"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_ci_test_isolation.py"

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个干净的脚本文件
            clean_file = Path(tmpdir) / "some_script.py"
            clean_file.write_text(
                dedent("""\
                from __future__ import annotations

                from scripts.ci.workflow_contract_common import load_contract

                def main():
                    pass
            """)
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--scan-scripts",
                    "--scripts-dir",
                    tmpdir,
                    "--json",
                ],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                env=get_subprocess_env(project_root),
            )

            assert result.returncode == 0

            import json

            output = json.loads(result.stdout)
            assert output["ok"] is True
            assert output["summary"]["scripts_files_checked"] == 1
            assert output["summary"]["tests_files_checked"] == 0

    def test_script_violations_by_file_in_json(self) -> None:
        """JSON 输出应包含按文件分组的违规"""
        project_root = get_project_root()
        script_path = project_root / "scripts" / "ci" / "check_ci_test_isolation.py"

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个有违规的测试文件
            violation_file = Path(tmpdir) / "test_violation.py"
            violation_file.write_text(
                dedent("""\
                from __future__ import annotations

                import sys
                sys.path.insert(0, "/some/path")

                import validate_workflows

                def test_something():
                    pass
            """)
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--scan-dir",
                    tmpdir,
                    "--json",
                ],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                env=get_subprocess_env(project_root),
            )

            assert result.returncode == 1  # 有违规

            import json

            output = json.loads(result.stdout)
            assert output["ok"] is False
            assert "violations_by_file" in output
            # 应该有一个文件有违规
            assert len(output["violations_by_file"]) == 1


# ============================================================================
# 关键 CLI 模块 --help 测试
# ============================================================================

# 关键 CLI 模块列表（支持 --help 参数的模块）
CLI_MODULES_WITH_HELP = [
    "scripts.ci.check_ci_test_isolation",
    "scripts.ci.validate_workflows",
    "scripts.ci.check_gateway_di_boundaries",
    "scripts.ci.check_workflow_contract_docs_sync",
    "scripts.ci.workflow_contract_drift_report",
    "scripts.ci.check_mypy_gate",
    "scripts.ci.check_env_consistency",
    "scripts.ci.check_iteration_docs_placeholders",
    "scripts.ci.check_gateway_public_api_import_surface",
]


class TestCLIModulesHelp:
    """测试关键 CLI 模块的 --help 参数"""

    @pytest.mark.parametrize("module_path", CLI_MODULES_WITH_HELP)
    def test_cli_module_help_exits_zero(self, module_path: str) -> None:
        """CLI 模块运行 --help 应返回退出码 0"""
        project_root = get_project_root()

        result = subprocess.run(
            [sys.executable, "-m", module_path, "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env=get_minimal_subprocess_env(project_root),
        )

        assert result.returncode == 0, (
            f"Module {module_path} --help failed with code {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        # 验证输出包含帮助信息
        assert (
            "usage:" in result.stdout.lower() or "--help" in result.stdout or "-h" in result.stdout
        ), f"Module {module_path} --help output doesn't look like help text"

    def test_validate_workflows_help_with_minimal_env(self) -> None:
        """验证 validate_workflows --help 在最小环境下工作"""
        project_root = get_project_root()

        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.validate_workflows", "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env=get_minimal_subprocess_env(project_root),
        )

        assert result.returncode == 0
        assert "--json" in result.stdout or "usage:" in result.stdout.lower()

    def test_check_ci_test_isolation_help_with_minimal_env(self) -> None:
        """验证 check_ci_test_isolation --help 在最小环境下工作"""
        project_root = get_project_root()

        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ci_test_isolation", "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env=get_minimal_subprocess_env(project_root),
        )

        assert result.returncode == 0
        assert "--scan-tests" in result.stdout or "--scan-scripts" in result.stdout

    def test_workflow_contract_drift_report_help_with_minimal_env(self) -> None:
        """验证 workflow_contract_drift_report --help 在最小环境下工作"""
        project_root = get_project_root()

        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.workflow_contract_drift_report", "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env=get_minimal_subprocess_env(project_root),
        )

        assert result.returncode == 0
        assert "usage:" in result.stdout.lower() or "--help" in result.stdout


# ============================================================================
# 环境隔离测试
# ============================================================================


class TestSubprocessEnvIsolation:
    """测试子进程环境隔离"""

    def test_subprocess_env_contains_pythonpath(self) -> None:
        """get_subprocess_env 应包含 PYTHONPATH"""
        project_root = get_project_root()
        env = get_subprocess_env(project_root)

        assert "PYTHONPATH" in env
        assert str(project_root) in env["PYTHONPATH"]

    def test_minimal_subprocess_env_contains_essentials(self) -> None:
        """get_minimal_subprocess_env 应包含最基本的环境变量"""
        project_root = get_project_root()
        env = get_minimal_subprocess_env(project_root)

        assert "PATH" in env
        assert "PYTHONPATH" in env
        assert str(project_root) in env["PYTHONPATH"]

    def test_minimal_env_excludes_extra_vars(self) -> None:
        """get_minimal_subprocess_env 应排除非必要变量"""
        project_root = get_project_root()
        env = get_minimal_subprocess_env(project_root)

        # 最小环境应该比完整环境变量少
        minimal_keys = set(env.keys())

        # 验证只有基本变量
        expected_basic = {"PATH", "HOME", "USER", "LANG", "PYTHONPATH"}
        assert minimal_keys.issubset(expected_basic | {"LC_ALL", "LC_CTYPE"}), (
            f"Unexpected vars in minimal env: {minimal_keys - expected_basic}"
        )

    def test_script_runs_with_isolated_env(self) -> None:
        """脚本应在隔离环境中正常运行"""
        project_root = get_project_root()

        # 使用最小环境运行脚本
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ci.check_ci_test_isolation", "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env=get_minimal_subprocess_env(project_root),
        )

        assert result.returncode == 0, (
            f"Script failed with minimal env\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ============================================================================
# Allowlist 功能测试
# ============================================================================


class TestAllowlistEntry:
    """测试 AllowlistEntry 数据结构"""

    def test_entry_creation(self) -> None:
        """AllowlistEntry 应正确创建"""
        entry = AllowlistEntry(
            id="test-entry",
            file_glob="tests/**/*.py",
            violation_type="sys_path_insert",
            reason="测试原因",
            owner="@test-team",
            expires_on="2030-12-31",
        )

        assert entry.id == "test-entry"
        assert entry.file_glob == "tests/**/*.py"
        assert entry.violation_type == "sys_path_insert"
        assert entry.reason == "测试原因"
        assert entry.owner == "@test-team"
        assert entry.expires_on == "2030-12-31"


class TestAllowlistMatching:
    """测试 Allowlist 匹配逻辑"""

    def test_exact_file_match(self) -> None:
        """精确文件匹配应正确工作"""
        allowlist = Allowlist(
            version="1",
            description="Test",
            entries=[
                AllowlistEntry(
                    id="entry-1",
                    file_glob="tests/test_foo.py",
                    violation_type="sys_path_insert",
                    reason="Test",
                    owner="@test",
                    expires_on="2030-12-31",
                )
            ],
        )

        # 精确匹配
        is_exempted, entry_id = allowlist.is_exempted("tests/test_foo.py", "sys_path_insert")
        assert is_exempted is True
        assert entry_id == "entry-1"

        # 不匹配的文件
        is_exempted, entry_id = allowlist.is_exempted("tests/test_bar.py", "sys_path_insert")
        assert is_exempted is False
        assert entry_id is None

    def test_glob_pattern_match(self) -> None:
        """Glob 模式匹配应正确工作"""
        allowlist = Allowlist(
            version="1",
            description="Test",
            entries=[
                AllowlistEntry(
                    id="entry-1",
                    file_glob="tests/logbook/*.py",
                    violation_type="sys_path_insert",
                    reason="Test",
                    owner="@test",
                    expires_on="2030-12-31",
                )
            ],
        )

        # 匹配
        is_exempted, _ = allowlist.is_exempted("tests/logbook/test_foo.py", "sys_path_insert")
        assert is_exempted is True

        # fnmatch 的 * 会匹配任意字符（包括 /），所以子目录也会匹配
        # 这是 fnmatch 的预期行为
        is_exempted, _ = allowlist.is_exempted("tests/logbook/sub/test_foo.py", "sys_path_insert")
        assert (
            is_exempted is True
        )  # fnmatch("tests/logbook/sub/test_foo.py", "tests/logbook/*.py") = True

        # 不匹配的目录
        is_exempted, _ = allowlist.is_exempted("tests/gateway/test_foo.py", "sys_path_insert")
        assert is_exempted is False

    def test_violation_type_match(self) -> None:
        """违规类型匹配应正确工作"""
        allowlist = Allowlist(
            version="1",
            description="Test",
            entries=[
                AllowlistEntry(
                    id="entry-1",
                    file_glob="tests/*.py",
                    violation_type="sys_path_insert",
                    reason="Test",
                    owner="@test",
                    expires_on="2030-12-31",
                )
            ],
        )

        # 匹配的违规类型
        is_exempted, _ = allowlist.is_exempted("tests/test_foo.py", "sys_path_insert")
        assert is_exempted is True

        # 不匹配的违规类型
        is_exempted, _ = allowlist.is_exempted("tests/test_foo.py", "toplevel_import")
        assert is_exempted is False

    def test_any_violation_type_matches_all(self) -> None:
        """violation_type='any' 应匹配所有类型"""
        allowlist = Allowlist(
            version="1",
            description="Test",
            entries=[
                AllowlistEntry(
                    id="entry-1",
                    file_glob="tests/*.py",
                    violation_type="any",
                    reason="Test",
                    owner="@test",
                    expires_on="2030-12-31",
                )
            ],
        )

        # 匹配所有类型
        is_exempted, _ = allowlist.is_exempted("tests/test_foo.py", "sys_path_insert")
        assert is_exempted is True

        is_exempted, _ = allowlist.is_exempted("tests/test_foo.py", "toplevel_import")
        assert is_exempted is True

        is_exempted, _ = allowlist.is_exempted("tests/test_foo.py", "sys_path_append")
        assert is_exempted is True

    def test_expired_entry_not_matched(self) -> None:
        """过期条目不应匹配"""
        allowlist = Allowlist(
            version="1",
            description="Test",
            entries=[
                AllowlistEntry(
                    id="entry-1",
                    file_glob="tests/*.py",
                    violation_type="sys_path_insert",
                    reason="Test",
                    owner="@test",
                    expires_on="2020-01-01",  # 已过期
                )
            ],
            expired_entries=["entry-1"],
        )

        # 过期条目不匹配
        is_exempted, _ = allowlist.is_exempted("tests/test_foo.py", "sys_path_insert")
        assert is_exempted is False


class TestAllowlistLoading:
    """测试 Allowlist 加载功能"""

    def test_load_valid_allowlist(self) -> None:
        """应正确加载有效的 allowlist 文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowlist_path = Path(tmpdir) / "allowlist.json"
            allowlist_path.write_text(
                dedent("""\
                {
                    "version": "1",
                    "description": "Test allowlist",
                    "entries": [
                        {
                            "id": "test-entry",
                            "file_glob": "tests/*.py",
                            "violation_type": "sys_path_insert",
                            "reason": "Test reason",
                            "owner": "@test",
                            "expires_on": "2030-12-31"
                        }
                    ]
                }
            """),
                encoding="utf-8",
            )

            allowlist = load_allowlist(allowlist_path)

            assert allowlist is not None
            assert allowlist.version == "1"
            assert len(allowlist.entries) == 1
            assert allowlist.entries[0].id == "test-entry"

    def test_load_nonexistent_file(self) -> None:
        """不存在的文件应返回 None"""
        allowlist = load_allowlist(Path("/nonexistent/path/allowlist.json"))
        assert allowlist is None

    def test_load_expired_entries_detected(self) -> None:
        """应检测过期的条目"""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowlist_path = Path(tmpdir) / "allowlist.json"
            allowlist_path.write_text(
                dedent("""\
                {
                    "version": "1",
                    "description": "Test allowlist",
                    "entries": [
                        {
                            "id": "expired-entry",
                            "file_glob": "tests/*.py",
                            "violation_type": "sys_path_insert",
                            "reason": "Test reason",
                            "owner": "@test",
                            "expires_on": "2020-01-01"
                        },
                        {
                            "id": "valid-entry",
                            "file_glob": "tests/*.py",
                            "violation_type": "sys_path_append",
                            "reason": "Test reason",
                            "owner": "@test",
                            "expires_on": "2030-12-31"
                        }
                    ]
                }
            """),
                encoding="utf-8",
            )

            allowlist = load_allowlist(allowlist_path)

            assert allowlist is not None
            assert len(allowlist.entries) == 2
            assert "expired-entry" in allowlist.expired_entries
            assert "valid-entry" not in allowlist.expired_entries


class TestCheckAllWithAllowlist:
    """测试 check_all 与 allowlist 集成"""

    def test_violations_exempted_by_allowlist(self) -> None:
        """allowlist 应正确豁免违规"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建测试文件
            test_file = Path(tmpdir) / "test_foo.py"
            test_file.write_text(
                dedent("""\
                from __future__ import annotations

                import sys
                sys.path.insert(0, "/some/path")

                def test_something():
                    pass
            """),
                encoding="utf-8",
            )

            # 创建 allowlist
            allowlist = Allowlist(
                version="1",
                description="Test",
                entries=[
                    AllowlistEntry(
                        id="test-entry",
                        file_glob="test_foo.py",
                        violation_type="sys_path_insert",
                        reason="Test",
                        owner="@test",
                        expires_on="2030-12-31",
                    )
                ],
            )

            # 执行检查
            result = check_all(
                tests_dir=Path(tmpdir),
                scripts_dir=None,
                project_root=Path(tmpdir),
                allowlist=allowlist,
                strict=False,
            )

            # 违规应被豁免
            assert len(result.violations) == 0
            assert len(result.exempted_violations) == 1
            assert result.exempted_violations[0][1] == "test-entry"

    def test_strict_mode_ignores_allowlist(self) -> None:
        """strict 模式应忽略 allowlist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建测试文件
            test_file = Path(tmpdir) / "test_foo.py"
            test_file.write_text(
                dedent("""\
                from __future__ import annotations

                import sys
                sys.path.insert(0, "/some/path")

                def test_something():
                    pass
            """),
                encoding="utf-8",
            )

            # 创建 allowlist
            allowlist = Allowlist(
                version="1",
                description="Test",
                entries=[
                    AllowlistEntry(
                        id="test-entry",
                        file_glob="test_foo.py",
                        violation_type="sys_path_insert",
                        reason="Test",
                        owner="@test",
                        expires_on="2030-12-31",
                    )
                ],
            )

            # 执行严格模式检查
            result = check_all(
                tests_dir=Path(tmpdir),
                scripts_dir=None,
                project_root=Path(tmpdir),
                allowlist=allowlist,
                strict=True,
            )

            # 违规不应被豁免
            assert len(result.violations) == 1
            assert len(result.exempted_violations) == 0

    def test_new_violations_not_exempted(self) -> None:
        """新违规（不在 allowlist 中）应不被豁免"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建测试文件
            test_file = Path(tmpdir) / "test_new.py"
            test_file.write_text(
                dedent("""\
                from __future__ import annotations

                import sys
                sys.path.insert(0, "/some/path")

                def test_something():
                    pass
            """),
                encoding="utf-8",
            )

            # 创建 allowlist（只豁免 test_old.py）
            allowlist = Allowlist(
                version="1",
                description="Test",
                entries=[
                    AllowlistEntry(
                        id="test-entry",
                        file_glob="test_old.py",
                        violation_type="sys_path_insert",
                        reason="Test",
                        owner="@test",
                        expires_on="2030-12-31",
                    )
                ],
            )

            # 执行检查
            result = check_all(
                tests_dir=Path(tmpdir),
                scripts_dir=None,
                project_root=Path(tmpdir),
                allowlist=allowlist,
                strict=False,
            )

            # 新违规应不被豁免
            assert len(result.violations) == 1
            assert len(result.exempted_violations) == 0


class TestCheckResultWithAllowlist:
    """测试 CheckResult 与 allowlist 相关的方法"""

    def test_has_violations_with_exemptions(self) -> None:
        """has_violations 应只检查未豁免的违规"""
        from scripts.ci.check_ci_test_isolation import Violation

        result = CheckResult(
            violations=[],
            exempted_violations=[
                (
                    Violation(
                        file_path="tests/test_foo.py",
                        line_number=10,
                        violation_type="sys_path_insert",
                        message="Test",
                    ),
                    "entry-1",
                )
            ],
        )

        # 只有豁免的违规，has_violations 应返回 False
        assert result.has_violations() is False
        assert result.has_any_violations() is True

    def test_has_expired_entries(self) -> None:
        """has_expired_entries 应正确检测过期条目"""
        result = CheckResult(expired_allowlist_entries=["entry-1", "entry-2"])

        assert result.has_expired_entries() is True

        result_no_expired = CheckResult()
        assert result_no_expired.has_expired_entries() is False

    def test_to_dict_includes_exempted(self) -> None:
        """to_dict 应包含已豁免的违规信息"""
        from scripts.ci.check_ci_test_isolation import Violation

        result = CheckResult(
            violations=[],
            exempted_violations=[
                (
                    Violation(
                        file_path="tests/test_foo.py",
                        line_number=10,
                        violation_type="sys_path_insert",
                        message="Test",
                    ),
                    "entry-1",
                )
            ],
            allowlist_used=True,
        )

        output = result.to_dict()

        assert output["ok"] is True
        assert output["summary"]["exempted_count"] == 1
        assert "exempted_by_file" in output
        assert "tests/test_foo.py" in output["exempted_by_file"]
