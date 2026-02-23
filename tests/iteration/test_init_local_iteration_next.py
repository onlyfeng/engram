#!/usr/bin/env python3
"""
init_local_iteration.py --next 参数单元测试

覆盖功能:
1. --next 与显式编号互斥
2. --next 调用 get_next_available_number() 自动选择编号
3. --next 输出打印实际使用的编号
4. --next 与 --force/--refresh-readme 组合使用
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.iteration.init_local_iteration import (
    get_next_available_number,
    main,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构，模拟完整的项目布局。"""
    with tempfile.TemporaryDirectory(prefix="test_init_next_") as tmpdir:
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


@pytest.fixture
def temp_project_with_iterations(temp_project: Path) -> Path:
    """创建带有已存在迭代文件的临时项目。"""
    ssot_dir = temp_project / "docs" / "acceptance"

    # 创建一些迭代文件（模拟 SSOT 中已存在的迭代）
    for n in [5, 10, 12]:
        (ssot_dir / f"iteration_{n}_plan.md").write_text(f"# Plan {n}", encoding="utf-8")
        (ssot_dir / f"iteration_{n}_regression.md").write_text(
            f"# Regression {n}", encoding="utf-8"
        )

    return temp_project


# ============================================================================
# get_next_available_number 函数测试
# ============================================================================


class TestGetNextAvailableNumber:
    """get_next_available_number 函数测试"""

    def test_returns_1_for_empty_ssot(self, temp_project: Path, monkeypatch):
        """测试空 SSOT 目录返回 1"""
        monkeypatch.setattr("init_local_iteration.SSOT_DIR", temp_project / "docs" / "acceptance")

        result = get_next_available_number()
        assert result == 1

    def test_returns_max_plus_1(self, temp_project_with_iterations: Path, monkeypatch):
        """测试返回最大编号 + 1"""
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )

        result = get_next_available_number()
        # 已存在 5, 10, 12，下一个应该是 13
        assert result == 13

    def test_handles_non_contiguous_numbers(self, temp_project: Path, monkeypatch):
        """测试处理不连续的编号"""
        ssot_dir = temp_project / "docs" / "acceptance"
        monkeypatch.setattr("init_local_iteration.SSOT_DIR", ssot_dir)

        # 创建非连续编号: 1, 5, 100
        (ssot_dir / "iteration_1_regression.md").write_text("# 1", encoding="utf-8")
        (ssot_dir / "iteration_5_regression.md").write_text("# 5", encoding="utf-8")
        (ssot_dir / "iteration_100_regression.md").write_text("# 100", encoding="utf-8")

        result = get_next_available_number()
        # 最大是 100，下一个应该是 101
        assert result == 101


# ============================================================================
# 命令行参数互斥测试
# ============================================================================


class TestArgumentMutualExclusion:
    """--next 与显式编号互斥测试"""

    def test_next_alone_works(self, temp_project_with_iterations: Path, monkeypatch, capsys):
        """测试单独使用 --next"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            exit_code = main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "自动选择下一可用编号: 13" in captured.out

    def test_explicit_number_alone_works(self, temp_project: Path, monkeypatch, capsys):
        """测试单独使用显式编号"""
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

        with patch("sys.argv", ["init_local_iteration.py", "5"]):
            exit_code = main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Iteration 5 本地草稿已初始化" in captured.out

    def test_both_arguments_fails(self, monkeypatch, capsys):
        """测试同时使用 --next 和显式编号会失败"""
        # argparse 会在解析阶段就报错，直接捕获 SystemExit
        with patch("sys.argv", ["init_local_iteration.py", "5", "--next"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            # argparse 互斥组冲突时退出码为 2
            assert exc_info.value.code == 2

    def test_no_arguments_fails(self, capsys):
        """测试不提供任何参数会失败"""
        with patch("sys.argv", ["init_local_iteration.py"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            # argparse 缺少必需参数时退出码为 2
            assert exc_info.value.code == 2


# ============================================================================
# --next 输出测试
# ============================================================================


class TestNextOutputFormat:
    """--next 输出格式测试"""

    def test_prints_selected_number(self, temp_project_with_iterations: Path, monkeypatch, capsys):
        """测试输出包含自动选择的编号"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            main()

        captured = capsys.readouterr()
        # 验证输出格式
        assert "📌 自动选择下一可用编号: 13" in captured.out
        assert "✅ Iteration 13 本地草稿已初始化" in captured.out

    def test_output_shows_correct_paths(
        self, temp_project_with_iterations: Path, monkeypatch, capsys
    ):
        """测试输出显示正确的路径"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            main()

        captured = capsys.readouterr()
        # 验证路径引用使用了正确的编号
        assert ".iteration/13/plan.md" in captured.out
        assert ".iteration/13/regression.md" in captured.out


# ============================================================================
# --next 与其他参数组合测试
# ============================================================================


class TestNextWithOtherFlags:
    """--next 与其他参数组合测试"""

    def test_next_with_force(self, temp_project_with_iterations: Path, monkeypatch, capsys):
        """测试 --next 与 --force 组合"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        # 先创建一次
        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            main()

        # 再次使用 --force 覆盖
        with patch("sys.argv", ["init_local_iteration.py", "--next", "--force"]):
            exit_code = main()

        assert exit_code == 0
        captured = capsys.readouterr()
        # 第二次仍然选择 13（因为还没晋升到 SSOT）
        assert "自动选择下一可用编号: 13" in captured.out

    def test_next_with_refresh_readme(
        self, temp_project_with_iterations: Path, monkeypatch, capsys
    ):
        """测试 --next 与 --refresh-readme 组合"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next", "--refresh-readme"]):
            exit_code = main()

        assert exit_code == 0

    def test_short_flag_n_works(self, temp_project_with_iterations: Path, monkeypatch, capsys):
        """测试短参数 -n 等同于 --next"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "-n"]):
            exit_code = main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "自动选择下一可用编号: 13" in captured.out


# ============================================================================
# 文件创建验证测试
# ============================================================================


class TestNextCreatesCorrectFiles:
    """--next 创建文件验证测试"""

    def test_creates_iteration_directory(self, temp_project_with_iterations: Path, monkeypatch):
        """测试 --next 创建正确的迭代目录"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            main()

        # 验证创建的目录
        iteration_dir = temp_project_with_iterations / ".iteration" / "13"
        assert iteration_dir.exists()
        assert (iteration_dir / "plan.md").exists()
        assert (iteration_dir / "regression.md").exists()

    def test_creates_readme(self, temp_project_with_iterations: Path, monkeypatch):
        """测试 --next 创建 README.md"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            main()

        readme = temp_project_with_iterations / ".iteration" / "README.md"
        assert readme.exists()
        content = readme.read_text(encoding="utf-8")
        assert "本地迭代草稿目录" in content


# ============================================================================
# 边界情况测试
# ============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_next_with_empty_ssot(self, temp_project: Path, monkeypatch, capsys):
        """测试 SSOT 为空时 --next 返回 1"""
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

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            exit_code = main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "自动选择下一可用编号: 1" in captured.out

    def test_next_ignores_local_iteration_dirs(
        self, temp_project_with_iterations: Path, monkeypatch, capsys
    ):
        """测试 --next 只考虑 SSOT，忽略本地 .iteration 目录"""
        monkeypatch.setattr("init_local_iteration.REPO_ROOT", temp_project_with_iterations)
        monkeypatch.setattr(
            "init_local_iteration.SSOT_DIR",
            temp_project_with_iterations / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "init_local_iteration.ITERATION_DIR",
            temp_project_with_iterations / ".iteration",
        )
        monkeypatch.setattr(
            "init_local_iteration.TEMPLATES_DIR",
            temp_project_with_iterations / "docs" / "acceptance" / "_templates",
        )

        # 在本地创建一个高编号的迭代目录
        (temp_project_with_iterations / ".iteration" / "999").mkdir(parents=True)
        (temp_project_with_iterations / ".iteration" / "999" / "plan.md").write_text(
            "# 999", encoding="utf-8"
        )

        with patch("sys.argv", ["init_local_iteration.py", "--next"]):
            main()

        captured = capsys.readouterr()
        # 应该基于 SSOT（最大 12）选择 13，而不是基于本地 999
        assert "自动选择下一可用编号: 13" in captured.out
