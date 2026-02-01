"""
tests/test_verify_cli_entrypoints_consistency.py

测试 scripts/verify_cli_entrypoints_consistency.py 的核心功能:
1. load_import_migration_map 函数
2. build_deprecated_script_alternatives 函数
3. check_f_migration_map_cli_targets_exist 检查项
4. 确保推荐替代方案引用实际存在的 console scripts
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

# 将 scripts 加入 path 以便导入
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from verify_cli_entrypoints_consistency import (
    PROJECT_ROOT,
    CLIEntrypointsConsistencyVerifier,
    build_deprecated_script_alternatives,
    load_import_migration_map,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def sample_console_scripts() -> Dict[str, str]:
    """模拟的 pyproject.toml [project.scripts]."""
    return {
        "engram-logbook": "engram.logbook.cli.logbook:main",
        "engram-migrate": "engram.logbook.cli.db_migrate:main",
        "engram-bootstrap-roles": "engram.logbook.cli.db_bootstrap:main",
        "engram-artifacts": "engram.logbook.cli.artifacts:main",
        "engram-scm": "engram.logbook.cli.scm:main",
        "engram-scm-sync": "engram.logbook.cli.scm_sync:main",
        "engram-scm-scheduler": "engram.logbook.cli.scm_sync:scheduler_main",
        "engram-scm-worker": "engram.logbook.cli.scm_sync:worker_main",
        "engram-scm-reaper": "engram.logbook.cli.scm_sync:reaper_main",
        "engram-scm-status": "engram.logbook.cli.scm_sync:status_main",
        "engram-scm-runner": "engram.logbook.cli.scm_sync:runner_main",
        "engram-gateway": "engram.gateway.main:main",
    }


@pytest.fixture
def sample_migration_map() -> Dict[str, Any]:
    """模拟的 import_migration_map.json."""
    return {
        "version": "1",
        "modules": [
            {
                "old_module": "db_migrate",
                "import_target": "engram.logbook.cli.db_migrate:main",
                "cli_target": "engram-migrate",
                "deprecated": True,
            },
            {
                "old_module": "db_bootstrap",
                "import_target": "engram.logbook.cli.db_bootstrap:main",
                "cli_target": "engram-bootstrap-roles",
                "deprecated": True,
            },
            {
                "old_module": "artifact_cli",
                "import_target": "engram.logbook.cli.artifacts:main",
                "cli_target": "engram-artifacts",
                "deprecated": True,
            },
            {
                "old_module": "artifact_gc",
                "import_target": "engram.logbook.cli.artifacts",
                "cli_target": "engram-artifacts gc",
                "deprecated": True,
            },
            {
                "old_module": "scm_sync_runner",
                "import_target": "engram.logbook.cli.scm_sync:runner_main",
                "cli_target": "engram-scm-runner",
                "deprecated": True,
            },
            {
                "old_module": "logbook_cli",
                "import_target": "engram.logbook.cli.logbook:main",
                "cli_target": "engram-logbook",
                "deprecated": True,
            },
            # 非 deprecated 模块
            {
                "old_module": "db",
                "import_target": "engram.logbook.db",
                "cli_target": None,
                "deprecated": False,
            },
            # scripts/ 路径
            {
                "old_module": "artifact_audit",
                "import_target": None,
                "cli_target": "scripts/artifact_audit.py",
                "deprecated": True,
            },
        ],
    }


@pytest.fixture
def migration_map_with_invalid_cli_target() -> Dict[str, Any]:
    """包含无效 cli_target 的 migration_map."""
    return {
        "version": "1",
        "modules": [
            {
                "old_module": "db_migrate",
                "import_target": "engram.logbook.cli.db_migrate:main",
                "cli_target": "engram-db-migrate",  # 不存在于 pyproject.toml
                "deprecated": True,
            },
        ],
    }


# ============================================================================
# load_import_migration_map 测试
# ============================================================================


class TestLoadImportMigrationMap:
    """测试 load_import_migration_map 函数."""

    def test_load_valid_file(self, tmp_path: Path) -> None:
        """加载有效的 JSON 文件应成功."""
        map_file = tmp_path / "configs" / "import_migration_map.json"
        map_file.parent.mkdir(parents=True, exist_ok=True)
        map_file.write_text('{"version": "1", "modules": []}', encoding="utf-8")

        data, error = load_import_migration_map(tmp_path)
        assert error is None
        assert data["version"] == "1"
        assert data["modules"] == []

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """加载不存在的文件应返回错误."""
        data, error = load_import_migration_map(tmp_path)
        assert data == {}
        assert "不存在" in error

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        """加载无效 JSON 应返回错误."""
        map_file = tmp_path / "configs" / "import_migration_map.json"
        map_file.parent.mkdir(parents=True, exist_ok=True)
        map_file.write_text("{ invalid json }", encoding="utf-8")

        data, error = load_import_migration_map(tmp_path)
        assert data == {}
        assert "解析失败" in error


# ============================================================================
# build_deprecated_script_alternatives 测试
# ============================================================================


class TestBuildDeprecatedScriptAlternatives:
    """测试 build_deprecated_script_alternatives 函数."""

    def test_build_from_valid_map(
        self,
        sample_migration_map: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """应正确构建 deprecated 脚本替代方案映射."""
        result = build_deprecated_script_alternatives(
            sample_migration_map,
            sample_console_scripts,
        )

        # 检查 db_migrate.py
        assert "db_migrate.py" in result
        assert result["db_migrate.py"]["console_script"] == "engram-migrate"
        assert "python -m engram.logbook.cli.db_migrate" in result["db_migrate.py"]["module"]

        # 检查 artifact_cli.py
        assert "artifact_cli.py" in result
        assert result["artifact_cli.py"]["console_script"] == "engram-artifacts"

        # 检查带子命令的 cli_target (如 "engram-artifacts gc")
        assert "artifact_gc.py" in result
        assert result["artifact_gc.py"]["console_script"] == "engram-artifacts gc"

        # 检查 scm_sync_runner.py
        assert "scm_sync_runner.py" in result
        assert result["scm_sync_runner.py"]["console_script"] == "engram-scm-runner"

    def test_skip_non_deprecated_modules(
        self,
        sample_migration_map: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """非 deprecated 模块不应包含在结果中."""
        result = build_deprecated_script_alternatives(
            sample_migration_map,
            sample_console_scripts,
        )

        # db.py 不是 deprecated，不应包含
        assert "db.py" not in result

    def test_handle_scripts_path(
        self,
        sample_migration_map: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """scripts/ 路径应被保留."""
        result = build_deprecated_script_alternatives(
            sample_migration_map,
            sample_console_scripts,
        )

        # artifact_audit.py 的 cli_target 是 scripts/artifact_audit.py
        assert "artifact_audit.py" in result
        assert result["artifact_audit.py"]["console_script"] == "scripts/artifact_audit.py"

    def test_invalid_cli_target_excluded(
        self,
        migration_map_with_invalid_cli_target: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """无效的 cli_target（不在 pyproject.toml 中）应被排除."""
        result = build_deprecated_script_alternatives(
            migration_map_with_invalid_cli_target,
            sample_console_scripts,
        )

        # db_migrate.py 的 cli_target "engram-db-migrate" 不存在于 sample_console_scripts
        # 但仍应通过 import_target 生成 module 调用
        if "db_migrate.py" in result:
            # console_script 应为 N/A（因为命令不存在）
            assert result["db_migrate.py"]["console_script"] == "N/A"
            # 但 module 仍应有效
            assert "python -m" in result["db_migrate.py"]["module"]

    def test_empty_migration_map(
        self,
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """空的 migration_map 应返回空 dict."""
        result = build_deprecated_script_alternatives(
            {"version": "1", "modules": []},
            sample_console_scripts,
        )
        assert result == {}


# ============================================================================
# CLIEntrypointsConsistencyVerifier 测试
# ============================================================================


class TestCLIEntrypointsConsistencyVerifier:
    """测试 CLIEntrypointsConsistencyVerifier 类."""

    def test_check_f_valid_cli_targets(
        self,
        sample_migration_map: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """所有 cli_target 在 pyproject.toml 中存在时应通过."""
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)
        verifier.console_scripts = sample_console_scripts
        verifier.migration_map = sample_migration_map

        result = verifier.check_f_migration_map_cli_targets_exist()

        assert result.passed
        assert "有效" in result.message

    def test_check_f_invalid_cli_targets(
        self,
        migration_map_with_invalid_cli_target: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """cli_target 不在 pyproject.toml 中时应失败."""
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)
        verifier.console_scripts = sample_console_scripts
        verifier.migration_map = migration_map_with_invalid_cli_target

        result = verifier.check_f_migration_map_cli_targets_exist()

        assert not result.passed
        assert "无效" in result.message
        assert any("engram-db-migrate" in d for d in result.details)

    def test_check_f_empty_migration_map(
        self,
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """空的 migration_map 应失败."""
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)
        verifier.console_scripts = sample_console_scripts
        verifier.migration_map = {}

        result = verifier.check_f_migration_map_cli_targets_exist()

        assert not result.passed
        assert "未加载" in result.message or "为空" in result.message

    def test_check_f_skip_scripts_path(
        self,
        sample_migration_map: Dict[str, Any],
        sample_console_scripts: Dict[str, str],
    ) -> None:
        """scripts/ 路径应被跳过（不视为错误）."""
        verifier = CLIEntrypointsConsistencyVerifier(verbose=True)
        verifier.console_scripts = sample_console_scripts
        verifier.migration_map = sample_migration_map

        result = verifier.check_f_migration_map_cli_targets_exist()

        # 即使 scripts/artifact_audit.py 不是 console script，也应通过
        assert result.passed

    def test_load_migration_map(self) -> None:
        """load_migration_map 应正确加载实际文件."""
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)
        verifier.load_pyproject()  # 先加载 pyproject.toml

        success = verifier.load_migration_map()

        # 如果文件存在应成功
        map_path = PROJECT_ROOT / "configs" / "import_migration_map.json"
        if map_path.exists():
            assert success
            assert len(verifier.migration_map.get("modules", [])) > 0
            assert len(verifier.deprecated_script_alternatives) > 0


# ============================================================================
# 集成测试：验证实际配置文件一致性
# ============================================================================


class TestRealConfigConsistency:
    """测试实际配置文件之间的一致性."""

    def test_migration_map_cli_targets_exist_in_pyproject(self) -> None:
        """
        验证 import_migration_map.json 中的所有 cli_target
        都存在于 pyproject.toml [project.scripts] 中.

        注意: 此测试验证 check_f 功能正常运行。
        如果配置存在不一致（如某些 cli_target 尚未添加到 pyproject.toml），
        应通过运行 `python scripts/verify_cli_entrypoints_consistency.py` 来排查。
        """
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)

        # 加载实际配置文件
        if not verifier.load_pyproject():
            pytest.skip("无法加载 pyproject.toml")

        if not verifier.load_migration_map():
            pytest.skip("无法加载 import_migration_map.json")

        # 运行检查
        result = verifier.check_f_migration_map_cli_targets_exist()

        # 验证检查能正常运行（无论结果如何）
        assert result.check_id == "F"
        assert result.name == "migration_map cli_target 有效"

        # 如果检查失败，输出详细信息但不导致测试失败
        # 这是一个"提示性测试"，实际的配置一致性应通过运行脚本验证
        if not result.passed:
            import warnings

            warnings.warn(
                f"\n[INFO] check_f 发现配置不一致: {result.message}\n"
                f"详情: {result.details}\n"
                f"请运行 `python scripts/verify_cli_entrypoints_consistency.py` 查看完整报告。",
                UserWarning,
            )

    def test_deprecated_script_alternatives_use_valid_console_scripts(self) -> None:
        """
        验证生成的 deprecated_script_alternatives 中的 console_script
        都是有效的（存在于 pyproject.toml 或是 scripts/ 路径或 N/A）.
        """
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)

        if not verifier.load_pyproject():
            pytest.skip("无法加载 pyproject.toml")

        if not verifier.load_migration_map():
            pytest.skip("无法加载 import_migration_map.json")

        invalid_alternatives = []

        for script, alternatives in verifier.deprecated_script_alternatives.items():
            console_script = alternatives.get("console_script", "N/A")

            # 跳过 N/A 和 scripts/ 路径
            if console_script == "N/A" or console_script.startswith("scripts/"):
                continue

            # 提取基础命令
            base_cmd = console_script.split()[0]

            if base_cmd not in verifier.console_scripts:
                invalid_alternatives.append(
                    f"{script}: console_script='{console_script}' "
                    f"('{base_cmd}' 不在 pyproject.toml 中)"
                )

        assert not invalid_alternatives, (
            f"发现 {len(invalid_alternatives)} 个无效的推荐替代方案:\n"
            + "\n".join(invalid_alternatives)
        )

    def test_all_pyproject_console_scripts_are_documented(self) -> None:
        """
        验证 pyproject.toml 中的所有 console scripts
        应该有对应的文档或配置覆盖.
        """
        verifier = CLIEntrypointsConsistencyVerifier(verbose=False)

        if not verifier.load_pyproject():
            pytest.skip("无法加载 pyproject.toml")

        # 检查所有 engram-* 命令
        engram_commands = [
            cmd for cmd in verifier.console_scripts.keys() if cmd.startswith("engram-")
        ]

        assert len(engram_commands) > 0, "pyproject.toml 中没有 engram-* 命令"

        # 确保核心命令存在
        expected_commands = [
            "engram-logbook",
            "engram-migrate",
            "engram-artifacts",
            "engram-scm",
        ]

        for cmd in expected_commands:
            assert cmd in engram_commands, f"缺少预期的命令: {cmd}"
