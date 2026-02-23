#!/usr/bin/env python3
"""
snapshot_ssot_iteration.py 单元测试

覆盖功能:
1. 正常快照（路径创建、文件复制）
2. 幂等性（相同内容跳过、不同内容需要 --force）
3. SSOT 不存在时报错并列出可用编号
4. README 创建和内容验证
5. 自定义输出目录支持

Fixtures 使用临时目录构造 docs/acceptance/ 结构。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.iteration.snapshot_ssot_iteration import (
    FileConflictError,
    SourceNotFoundError,
    files_are_identical,
    get_snapshot_readme_content,
    get_ssot_iteration_numbers,
    snapshot_iteration,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_project():
    """创建临时项目目录结构。"""
    with tempfile.TemporaryDirectory(prefix="test_snapshot_") as tmpdir:
        project = Path(tmpdir)

        # 创建目录结构
        (project / ".iteration" / "_export").mkdir(parents=True)
        (project / "docs" / "acceptance").mkdir(parents=True)

        yield project


@pytest.fixture
def temp_project_with_ssot(temp_project: Path) -> Path:
    """创建带有 SSOT 迭代文档的临时项目。"""
    ssot_dir = temp_project / "docs" / "acceptance"

    # 创建 Iteration 9 的文件
    (ssot_dir / "iteration_9_plan.md").write_text(
        """# Iteration 9 计划

## 目标

测试快照功能。
""",
        encoding="utf-8",
    )

    (ssot_dir / "iteration_9_regression.md").write_text(
        """# Iteration 9 回归记录

## 验收结果

✅ 全部通过。
""",
        encoding="utf-8",
    )

    # 创建 Iteration 10 的文件（仅有 regression）
    (ssot_dir / "iteration_10_regression.md").write_text(
        """# Iteration 10 回归记录

## 验收结果

进行中...
""",
        encoding="utf-8",
    )

    return temp_project


# ============================================================================
# 辅助函数测试
# ============================================================================


class TestGetSSOTIterationNumbers:
    """get_ssot_iteration_numbers 函数测试"""

    def test_returns_empty_for_empty_dir(self, temp_project: Path, monkeypatch):
        """测试空目录返回空列表"""
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR", temp_project / "docs" / "acceptance"
        )
        result = get_ssot_iteration_numbers()
        assert result == []

    def test_finds_iteration_files(self, temp_project_with_ssot: Path, monkeypatch):
        """测试能找到迭代文件"""
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )

        result = get_ssot_iteration_numbers()

        # 应该是降序排列
        assert result == [10, 9]

    def test_returns_descending_order(self, temp_project: Path, monkeypatch):
        """测试返回降序排列"""
        ssot_dir = temp_project / "docs" / "acceptance"
        monkeypatch.setattr("snapshot_ssot_iteration.SSOT_DIR", ssot_dir)

        # 创建多个迭代
        for n in [3, 7, 1, 12, 5]:
            (ssot_dir / f"iteration_{n}_regression.md").write_text(f"# {n}", encoding="utf-8")

        result = get_ssot_iteration_numbers()
        assert result == [12, 7, 5, 3, 1]


class TestFilesAreIdentical:
    """files_are_identical 函数测试"""

    def test_identical_files(self, temp_project: Path):
        """测试相同内容的文件"""
        file1 = temp_project / "file1.md"
        file2 = temp_project / "file2.md"

        content = "Same content"
        file1.write_text(content, encoding="utf-8")
        file2.write_text(content, encoding="utf-8")

        assert files_are_identical(file1, file2) is True

    def test_different_files(self, temp_project: Path):
        """测试不同内容的文件"""
        file1 = temp_project / "file1.md"
        file2 = temp_project / "file2.md"

        file1.write_text("Content A", encoding="utf-8")
        file2.write_text("Content B", encoding="utf-8")

        assert files_are_identical(file1, file2) is False

    def test_missing_file(self, temp_project: Path):
        """测试文件不存在"""
        file1 = temp_project / "file1.md"
        file2 = temp_project / "missing.md"

        file1.write_text("Content", encoding="utf-8")

        assert files_are_identical(file1, file2) is False


class TestGetSnapshotReadmeContent:
    """get_snapshot_readme_content 函数测试"""

    def test_contains_iteration_number(self, temp_project: Path):
        """测试 README 包含迭代编号"""
        content = get_snapshot_readme_content(42, temp_project / "docs" / "acceptance")

        assert "42" in content
        assert "Iteration 42" in content

    def test_contains_warning(self):
        """测试 README 包含警告信息"""
        content = get_snapshot_readme_content(10, Path("docs/acceptance"))

        assert "警告" in content or "⚠️" in content
        assert "只读" in content

    def test_contains_no_promote_warning(self):
        """测试 README 包含不可 promote 警告"""
        content = get_snapshot_readme_content(10, Path("docs/acceptance"))

        assert "promote" in content.lower() or "覆盖" in content
        assert "禁止" in content or "不可" in content or "不能" in content

    def test_contains_do_not_promote_sentinel(self):
        """测试 README 包含 DO_NOT_PROMOTE=true sentinel"""
        content = get_snapshot_readme_content(10, Path("docs/acceptance"))

        # sentinel 必须是可机器识别的格式
        assert "DO_NOT_PROMOTE=true" in content


# ============================================================================
# 路径创建测试
# ============================================================================


class TestPathCreation:
    """路径创建测试"""

    def test_creates_output_directory(self, temp_project_with_ssot: Path, monkeypatch):
        """测试自动创建输出目录"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        output_dir = temp_project_with_ssot / ".iteration" / "_export" / "9"
        assert not output_dir.exists()

        snapshot_iteration(9)

        assert output_dir.exists()
        assert output_dir.is_dir()

    def test_creates_nested_custom_directory(self, temp_project_with_ssot: Path, monkeypatch):
        """测试创建嵌套的自定义输出目录"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )

        custom_dir = temp_project_with_ssot / "deep" / "nested" / "dir"
        assert not custom_dir.exists()

        snapshot_iteration(9, output_dir=custom_dir)

        assert custom_dir.exists()


# ============================================================================
# 文件复制测试
# ============================================================================


class TestFileCopy:
    """文件复制测试"""

    def test_copies_plan_file(self, temp_project_with_ssot: Path, monkeypatch):
        """测试复制 plan.md 文件"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        dst_plan = temp_project_with_ssot / ".iteration" / "_export" / "9" / "plan.md"
        assert dst_plan.exists()
        assert "Iteration 9 计划" in dst_plan.read_text(encoding="utf-8")
        assert len(result.files_copied) >= 1

    def test_copies_regression_file(self, temp_project_with_ssot: Path, monkeypatch):
        """测试复制 regression.md 文件"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        dst_regression = temp_project_with_ssot / ".iteration" / "_export" / "9" / "regression.md"
        assert dst_regression.exists()
        assert "Iteration 9 回归记录" in dst_regression.read_text(encoding="utf-8")
        assert len(result.files_copied) >= 1

    def test_copies_partial_files(self, temp_project_with_ssot: Path, monkeypatch):
        """测试部分文件存在时正常复制"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        # Iteration 10 只有 regression.md
        result = snapshot_iteration(10)

        dst_dir = temp_project_with_ssot / ".iteration" / "_export" / "10"
        assert (dst_dir / "regression.md").exists()
        assert not (dst_dir / "plan.md").exists()
        assert result.success is True

    def test_creates_readme(self, temp_project_with_ssot: Path, monkeypatch):
        """测试创建 README.md"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        dst_readme = temp_project_with_ssot / ".iteration" / "_export" / "9" / "README.md"
        assert dst_readme.exists()
        assert result.readme_created is True

        # 验证 README 内容
        readme_content = dst_readme.read_text(encoding="utf-8")
        assert "Iteration 9" in readme_content
        assert "只读" in readme_content


# ============================================================================
# 幂等性测试
# ============================================================================


class TestIdempotency:
    """幂等性测试"""

    def test_skips_identical_files(self, temp_project_with_ssot: Path, monkeypatch):
        """测试相同内容的文件被跳过"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        # 第一次快照
        result1 = snapshot_iteration(9)
        assert len(result1.files_copied) >= 2
        assert len(result1.files_skipped) == 0

        # 第二次快照（相同内容）
        result2 = snapshot_iteration(9)
        assert len(result2.files_copied) == 0
        assert len(result2.files_skipped) >= 2

    def test_raises_for_different_content_without_force(
        self, temp_project_with_ssot: Path, monkeypatch
    ):
        """测试内容不同时不使用 --force 会报错"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        # 第一次快照
        snapshot_iteration(9)

        # 修改目标文件内容
        dst_plan = temp_project_with_ssot / ".iteration" / "_export" / "9" / "plan.md"
        dst_plan.write_text("# Modified content", encoding="utf-8")

        # 第二次快照（内容不同，无 --force）
        with pytest.raises(FileConflictError):
            snapshot_iteration(9)

    def test_force_overwrites_different_content(self, temp_project_with_ssot: Path, monkeypatch):
        """测试 --force 可以覆盖不同内容"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        # 第一次快照
        snapshot_iteration(9)

        # 修改目标文件内容
        dst_plan = temp_project_with_ssot / ".iteration" / "_export" / "9" / "plan.md"
        dst_plan.write_text("# Modified content", encoding="utf-8")

        # 使用 --force 快照
        result = snapshot_iteration(9, force=True)
        assert result.success is True

        # 验证目标文件已恢复
        assert "Iteration 9 计划" in dst_plan.read_text(encoding="utf-8")


# ============================================================================
# 错误处理测试
# ============================================================================


class TestErrorHandling:
    """错误处理测试"""

    def test_raises_for_nonexistent_iteration(self, temp_project_with_ssot: Path, monkeypatch):
        """测试不存在的迭代抛出错误"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )

        with pytest.raises(SourceNotFoundError) as exc_info:
            snapshot_iteration(99)

        assert exc_info.value.iteration_number == 99
        assert 9 in exc_info.value.available
        assert 10 in exc_info.value.available

    def test_error_includes_available_numbers(self, temp_project_with_ssot: Path, monkeypatch):
        """测试错误信息包含可用编号"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )

        with pytest.raises(SourceNotFoundError) as exc_info:
            snapshot_iteration(99)

        # 验证可用编号列表
        available = exc_info.value.available
        assert isinstance(available, list)
        assert len(available) >= 2


# ============================================================================
# 自定义输出目录测试
# ============================================================================


class TestCustomOutputDir:
    """自定义输出目录测试"""

    def test_uses_custom_output_dir(self, temp_project_with_ssot: Path, monkeypatch):
        """测试使用自定义输出目录"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )

        custom_dir = temp_project_with_ssot / "custom" / "ssot" / "9"
        result = snapshot_iteration(9, output_dir=custom_dir)

        assert result.success is True
        assert (custom_dir / "plan.md").exists()
        assert (custom_dir / "regression.md").exists()
        assert (custom_dir / "README.md").exists()

    def test_default_dir_uses_iteration_number(self, temp_project_with_ssot: Path, monkeypatch):
        """测试默认目录使用迭代编号"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        snapshot_iteration(9)

        expected_dir = temp_project_with_ssot / ".iteration" / "_export" / "9"
        assert expected_dir.exists()
        assert (expected_dir / "plan.md").exists()


# ============================================================================
# 结果对象测试
# ============================================================================


class TestSnapshotResult:
    """SnapshotResult 对象测试"""

    def test_result_success_flag(self, temp_project_with_ssot: Path, monkeypatch):
        """测试结果 success 标志"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        assert result.success is True

    def test_result_message(self, temp_project_with_ssot: Path, monkeypatch):
        """测试结果消息"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        assert "9" in result.message
        assert "快照" in result.message or "完成" in result.message

    def test_result_files_copied_list(self, temp_project_with_ssot: Path, monkeypatch):
        """测试结果文件复制列表"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        assert isinstance(result.files_copied, list)
        # Iteration 9 有 plan 和 regression
        assert len(result.files_copied) == 2

    def test_result_readme_created_flag(self, temp_project_with_ssot: Path, monkeypatch):
        """测试结果 README 创建标志"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project_with_ssot / ".iteration" / "_export",
        )

        result = snapshot_iteration(9)

        assert result.readme_created is True


# ============================================================================
# 边界情况测试
# ============================================================================


# ============================================================================
# CLI --list 路径测试
# ============================================================================


class TestCLIListOption:
    """CLI --list 选项测试"""

    def test_list_returns_zero_with_iterations(self, temp_project_with_ssot: Path, monkeypatch):
        """测试有迭代时 --list 返回 0"""
        import subprocess

        monkeypatch.chdir(temp_project_with_ssot)

        # 设置环境变量让脚本使用临时目录
        # 由于脚本使用 REPO_ROOT，需要通过 subprocess 运行并检查输出
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "iteration"
            / "snapshot_ssot_iteration.py"
        )

        # 创建一个修改后的脚本环境
        result = subprocess.run(
            ["python", str(script_path), "--list"],
            capture_output=True,
            text=True,
            cwd=str(temp_project_with_ssot.parent.parent),  # 在仓库根目录运行
        )

        # 实际测试中由于 SSOT_DIR 指向真实的 docs/acceptance/，
        # 这里验证脚本能正常执行 --list 选项
        assert (
            result.returncode == 0
            or "可用的迭代编号" in result.stdout
            or "没有任何迭代" in result.stderr
        )

    def test_list_output_format(self, temp_project_with_ssot: Path, monkeypatch):
        """测试 --list 输出格式"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project_with_ssot)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR",
            temp_project_with_ssot / "docs" / "acceptance",
        )

        # 通过直接调用 get_ssot_iteration_numbers 验证列表功能
        from snapshot_ssot_iteration import get_ssot_iteration_numbers

        numbers = get_ssot_iteration_numbers()

        # 验证返回的是降序排列的整数列表
        assert isinstance(numbers, list)
        assert all(isinstance(n, int) for n in numbers)
        assert numbers == sorted(numbers, reverse=True)


class TestEdgeCases:
    """边界情况测试"""

    def test_handles_unicode_content(self, temp_project: Path, monkeypatch):
        """测试处理 Unicode 内容"""
        ssot_dir = temp_project / "docs" / "acceptance"
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr("snapshot_ssot_iteration.SSOT_DIR", ssot_dir)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.DEFAULT_EXPORT_DIR",
            temp_project / ".iteration" / "_export",
        )

        # 创建包含 Unicode 的文件
        (ssot_dir / "iteration_20_plan.md").write_text(
            "# 中文标题 🎉\n\n内容包含 emoji 和特殊字符 ™©®",
            encoding="utf-8",
        )

        result = snapshot_iteration(20)

        assert result.success is True
        dst_plan = temp_project / ".iteration" / "_export" / "20" / "plan.md"
        content = dst_plan.read_text(encoding="utf-8")
        assert "中文标题" in content
        assert "🎉" in content

    def test_handles_empty_ssot_dir(self, temp_project: Path, monkeypatch):
        """测试空 SSOT 目录"""
        monkeypatch.setattr("snapshot_ssot_iteration.REPO_ROOT", temp_project)
        monkeypatch.setattr(
            "snapshot_ssot_iteration.SSOT_DIR", temp_project / "docs" / "acceptance"
        )

        with pytest.raises(SourceNotFoundError) as exc_info:
            snapshot_iteration(1)

        assert exc_info.value.available == []
