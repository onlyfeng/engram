#!/usr/bin/env python3
"""
init_local_iteration.py 文件状态（created vs overwritten）单元测试

覆盖功能:
1. 首次创建文件时返回 "created" 状态
2. 重复执行且 --force 时返回 "overwritten" 状态
3. README.md 的状态正确返回 "created"/"refreshed"/"exists"
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "iteration"))

from init_local_iteration import init_iteration

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构，模拟完整的项目布局。"""
    with tempfile.TemporaryDirectory(prefix="test_init_status_") as tmpdir:
        project = Path(tmpdir)

        # 创建目录结构
        (project / ".iteration").mkdir(parents=True)
        (project / "docs" / "acceptance" / "_templates").mkdir(parents=True)

        # 创建模板文件
        (project / "docs" / "acceptance" / "_templates" / "iteration_plan.template.md").write_text(
            "# Iteration Plan Template\n\n{PLACEHOLDER}",
            encoding="utf-8",
        )
        (
            project / "docs" / "acceptance" / "_templates" / "iteration_regression.template.md"
        ).write_text(
            "# Iteration Regression Template\n\n{PLACEHOLDER}",
            encoding="utf-8",
        )

        yield project


# ============================================================================
# 文件状态测试：created vs overwritten
# ============================================================================


class TestFileStatusCreatedVsOverwritten:
    """测试 plan.md/regression.md 的 created/overwritten 状态"""

    def test_first_creation_returns_created_status(self, temp_project: Path, monkeypatch):
        """测试首次创建文件时返回 'created' 状态"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 首次创建
        results = init_iteration(1)

        # 验证 plan.md 状态为 created
        plan_key = str(temp_project / ".iteration" / "1" / "plan.md")
        assert results[plan_key] == "created"

        # 验证 regression.md 状态为 created
        regression_key = str(temp_project / ".iteration" / "1" / "regression.md")
        assert results[regression_key] == "created"

    def test_force_overwrite_returns_overwritten_status(self, temp_project: Path, monkeypatch):
        """测试 --force 重复执行时返回 'overwritten' 状态"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 首次创建
        init_iteration(1)

        # 使用 --force 再次创建
        results = init_iteration(1, force=True)

        # 验证 plan.md 状态为 overwritten
        plan_key = str(temp_project / ".iteration" / "1" / "plan.md")
        assert results[plan_key] == "overwritten"

        # 验证 regression.md 状态为 overwritten
        regression_key = str(temp_project / ".iteration" / "1" / "regression.md")
        assert results[regression_key] == "overwritten"

    def test_without_force_raises_file_exists_error(self, temp_project: Path, monkeypatch):
        """测试不使用 --force 重复执行时抛出 FileExistsError"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 首次创建
        init_iteration(1)

        # 不使用 --force 再次创建应抛出错误
        with pytest.raises(FileExistsError) as exc_info:
            init_iteration(1)

        assert "迭代目录已存在" in str(exc_info.value)
        assert "--force" in str(exc_info.value)


# ============================================================================
# README.md 状态测试
# ============================================================================


class TestReadmeStatus:
    """测试 README.md 的状态返回"""

    def test_readme_first_creation_returns_created(self, temp_project: Path, monkeypatch):
        """测试首次创建 README.md 返回 'created' 状态"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 确保 README.md 不存在
        readme_path = temp_project / ".iteration" / "README.md"
        if readme_path.exists():
            readme_path.unlink()

        # 首次创建
        results = init_iteration(1)

        readme_key = str(temp_project / ".iteration" / "README.md")
        assert results[readme_key] == "created"

    def test_readme_exists_without_force_returns_exists(self, temp_project: Path, monkeypatch):
        """测试 README.md 已存在且不使用 --force 时返回 'exists' 状态"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 首次创建
        init_iteration(1)

        # 创建新迭代（不使用 --force，但不同的迭代编号）
        results = init_iteration(2)

        readme_key = str(temp_project / ".iteration" / "README.md")
        assert results[readme_key] == "exists"

    def test_readme_force_returns_refreshed(self, temp_project: Path, monkeypatch):
        """测试使用 --force 时 README.md 返回 'refreshed' 状态"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 首次创建
        init_iteration(1)

        # 使用 --force 再次创建
        results = init_iteration(1, force=True)

        readme_key = str(temp_project / ".iteration" / "README.md")
        assert results[readme_key] == "refreshed"

    def test_readme_refresh_readme_flag_returns_refreshed(self, temp_project: Path, monkeypatch):
        """测试使用 --refresh-readme 时 README.md 返回 'refreshed' 状态"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 首次创建
        init_iteration(1)

        # 使用 --refresh-readme（不同迭代）
        results = init_iteration(2, refresh_readme=True)

        readme_key = str(temp_project / ".iteration" / "README.md")
        assert results[readme_key] == "refreshed"


# ============================================================================
# 边界情况测试
# ============================================================================


class TestStatusEdgeCases:
    """文件状态边界情况测试"""

    def test_pre_existing_file_without_directory_check(self, temp_project: Path, monkeypatch):
        """测试手动创建的文件在 --force 时也返回 'overwritten'"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 手动创建迭代目录和文件
        iteration_dir = temp_project / ".iteration" / "1"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        (iteration_dir / "plan.md").write_text("# Existing Plan", encoding="utf-8")
        (iteration_dir / "regression.md").write_text("# Existing Regression", encoding="utf-8")

        # 使用 --force 覆盖
        results = init_iteration(1, force=True)

        plan_key = str(temp_project / ".iteration" / "1" / "plan.md")
        regression_key = str(temp_project / ".iteration" / "1" / "regression.md")

        assert results[plan_key] == "overwritten"
        assert results[regression_key] == "overwritten"

    def test_partial_files_with_force(self, temp_project: Path, monkeypatch):
        """测试只存在部分文件时的状态返回"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project / "docs" / "acceptance" / "_templates",
        )

        # 手动创建迭代目录，只创建 plan.md
        iteration_dir = temp_project / ".iteration" / "1"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        (iteration_dir / "plan.md").write_text("# Existing Plan", encoding="utf-8")
        # 不创建 regression.md

        # 使用 --force
        results = init_iteration(1, force=True)

        plan_key = str(temp_project / ".iteration" / "1" / "plan.md")
        regression_key = str(temp_project / ".iteration" / "1" / "regression.md")

        # plan.md 已存在，应为 overwritten
        assert results[plan_key] == "overwritten"
        # regression.md 不存在，应为 created
        assert results[regression_key] == "created"
