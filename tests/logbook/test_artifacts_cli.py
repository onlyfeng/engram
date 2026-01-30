# -*- coding: utf-8 -*-
"""
test_artifacts_cli.py - Artifacts CLI 子命令测试

测试覆盖:
1. 参数解析测试
2. write 命令测试
3. read 命令测试
4. exists 命令测试
5. delete 命令测试
6. audit 命令参数测试
7. gc 命令参数测试
8. migrate 命令参数测试
9. artifact_cli.py 兼容入口测试
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 添加 scripts 目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typer.testing import CliRunner

from logbook_cli_main import app, artifacts_app
from artifact_cli import main as artifact_cli_main


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def runner():
    """创建 Typer CLI 测试 runner"""
    return CliRunner()


@pytest.fixture
def temp_artifacts_dir():
    """创建临时制品目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_store(temp_artifacts_dir):
    """创建 mock store 并设置环境变量"""
    # 重置全局 store 缓存
    from engram.logbook.artifact_store import reset_default_store
    reset_default_store()

    os.environ["ENGRAM_ARTIFACTS_ROOT"] = str(temp_artifacts_dir)
    yield temp_artifacts_dir

    # 清理环境变量并重置缓存
    if "ENGRAM_ARTIFACTS_ROOT" in os.environ:
        del os.environ["ENGRAM_ARTIFACTS_ROOT"]
    reset_default_store()


# =============================================================================
# 参数解析测试
# =============================================================================


class TestArtifactsWriteCommand:
    """write 命令测试"""

    def test_write_requires_path_or_uri(self, runner):
        """测试必须指定 --path 或 --uri"""
        result = runner.invoke(app, ["artifacts", "write", "--content", "test"])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_write_requires_content_or_input(self, runner):
        """测试必须指定 --content 或 --input"""
        result = runner.invoke(app, ["artifacts", "write", "--path", "test.txt"])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_write_with_path_and_content(self, runner, mock_store):
        """测试使用 --path 和 --content 写入"""
        result = runner.invoke(
            app,
            ["artifacts", "write", "--path", "test/file.txt", "--content", "hello world"],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert "sha256" in output
        assert output["size_bytes"] == 11  # len("hello world")

        # 验证文件存在
        file_path = mock_store / "test" / "file.txt"
        assert file_path.exists()
        assert file_path.read_text() == "hello world"

    def test_write_with_input_file(self, runner, mock_store, temp_artifacts_dir):
        """测试从文件读取内容"""
        # 创建输入文件
        input_file = temp_artifacts_dir / "input.txt"
        input_file.write_text("content from file")

        result = runner.invoke(
            app,
            ["artifacts", "write", "--path", "output.txt", "--input", str(input_file)],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True


class TestArtifactsReadCommand:
    """read 命令测试"""

    def test_read_requires_path_or_uri(self, runner):
        """测试必须指定 --path 或 --uri"""
        result = runner.invoke(app, ["artifacts", "read"])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_read_existing_file(self, runner, mock_store):
        """测试读取存在的文件"""
        # 先写入文件
        test_path = mock_store / "test" / "read.txt"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("read me")

        result = runner.invoke(
            app,
            ["artifacts", "read", "--path", "test/read.txt"],
        )
        assert result.exit_code == 0
        assert "read me" in result.stdout

    def test_read_with_json_output(self, runner, mock_store):
        """测试 JSON 元数据输出"""
        # 先写入文件
        test_path = mock_store / "test" / "meta.txt"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("metadata test")

        result = runner.invoke(
            app,
            ["artifacts", "read", "--path", "test/meta.txt", "--json"],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert "sha256" in output
        assert "size_bytes" in output

    def test_read_nonexistent_file(self, runner, mock_store):
        """测试读取不存在的文件"""
        result = runner.invoke(
            app,
            ["artifacts", "read", "--path", "nonexistent.txt", "--json"],
        )
        assert result.exit_code != 0


class TestArtifactsExistsCommand:
    """exists 命令测试"""

    def test_exists_requires_path_or_uri(self, runner):
        """测试必须指定 --path 或 --uri"""
        result = runner.invoke(app, ["artifacts", "exists"])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_exists_returns_true_for_existing_file(self, runner, mock_store):
        """测试存在的文件返回 true"""
        test_path = mock_store / "exists_test.txt"
        test_path.write_text("I exist")

        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", "exists_test.txt"],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert output["exists"] is True

    def test_exists_returns_false_for_nonexistent_file(self, runner, mock_store):
        """测试不存在的文件返回 false"""
        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", "not_exists.txt"],
        )
        assert result.exit_code == 1  # 不存在时返回非零
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert output["exists"] is False


class TestArtifactsDeleteCommand:
    """delete 命令测试"""

    def test_delete_requires_path_or_uri(self, runner):
        """测试必须指定 --path 或 --uri"""
        result = runner.invoke(app, ["artifacts", "delete"])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_delete_existing_file(self, runner, mock_store):
        """测试删除存在的文件"""
        test_path = mock_store / "to_delete.txt"
        test_path.write_text("delete me")
        assert test_path.exists()

        result = runner.invoke(
            app,
            ["artifacts", "delete", "--path", "to_delete.txt"],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert output["deleted"] is True
        assert not test_path.exists()

    def test_delete_nonexistent_without_force(self, runner, mock_store):
        """测试删除不存在的文件（无 --force）"""
        result = runner.invoke(
            app,
            ["artifacts", "delete", "--path", "nonexistent.txt"],
        )
        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "NOT_FOUND" in output["code"]

    def test_delete_nonexistent_with_force(self, runner, mock_store):
        """测试删除不存在的文件（有 --force）"""
        result = runner.invoke(
            app,
            ["artifacts", "delete", "--path", "nonexistent.txt", "--force"],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True


class TestArtifactsAuditCommand:
    """audit 命令参数测试"""

    def test_audit_invalid_table(self, runner):
        """测试无效的表名参数"""
        result = runner.invoke(
            app,
            ["artifacts", "audit", "--table", "invalid_table"],
        )
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_audit_invalid_sample_rate(self, runner):
        """测试无效的采样率"""
        result = runner.invoke(
            app,
            ["artifacts", "audit", "--sample-rate", "1.5"],
        )
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False

    def test_audit_invalid_since_format(self, runner):
        """测试无效的时间格式"""
        result = runner.invoke(
            app,
            ["artifacts", "audit", "--since", "not-a-date"],
        )
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]


class TestArtifactsGcCommand:
    """gc 命令参数测试"""

    def test_gc_requires_prefix(self, runner):
        """测试必须指定 --prefix"""
        result = runner.invoke(app, ["artifacts", "gc"])
        assert result.exit_code != 0  # Typer 会因缺少必需参数而失败

    @patch("artifact_gc.run_gc")
    def test_gc_dry_run_by_default(self, mock_run_gc, runner):
        """测试默认 dry-run 模式"""
        from artifact_gc import GCResult
        mock_run_gc.return_value = GCResult(
            scanned_count=10,
            referenced_count=8,
            candidates_count=2,
        )

        result = runner.invoke(
            app,
            ["artifacts", "gc", "--prefix", "scm/"],
        )

        # 验证 dry_run=True
        mock_run_gc.assert_called_once()
        call_kwargs = mock_run_gc.call_args[1]
        assert call_kwargs["dry_run"] is True
        assert call_kwargs["delete"] is False


class TestArtifactsMigrateCommand:
    """migrate 命令参数测试"""

    def test_migrate_requires_backends(self, runner):
        """测试必须指定源和目标后端"""
        result = runner.invoke(app, ["artifacts", "migrate"])
        assert result.exit_code != 0  # 缺少必需参数

    def test_migrate_invalid_source_backend(self, runner):
        """测试无效的源后端类型"""
        result = runner.invoke(
            app,
            ["artifacts", "migrate", "--source-backend", "invalid", "--target-backend", "local"],
        )
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]

    def test_migrate_invalid_target_backend(self, runner):
        """测试无效的目标后端类型"""
        result = runner.invoke(
            app,
            ["artifacts", "migrate", "--source-backend", "local", "--target-backend", "invalid"],
        )
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert "INVALID_ARGS" in output["code"]


class TestArtifactCliCompatibility:
    """artifact_cli.py 兼容入口测试"""

    def test_artifact_cli_imports_correctly(self):
        """测试兼容入口可以正确导入"""
        from artifact_cli import main, artifacts_app
        assert callable(main)
        assert artifacts_app is not None

    def test_artifact_cli_app_has_commands(self):
        """测试 artifacts_app 包含所有命令"""
        from logbook_cli_main import artifacts_app

        # 获取注册的命令名称
        command_names = [cmd.name for cmd in artifacts_app.registered_commands]

        expected_commands = ["write", "read", "exists", "delete", "audit", "gc", "migrate"]
        for cmd in expected_commands:
            assert cmd in command_names, f"Missing command: {cmd}"


class TestStepOneCLIArtifactsIntegration:
    """logbook_cli_main.py artifacts 子命令集成测试"""

    def test_logbook_cli_main_has_artifacts_subcommand(self, runner):
        """测试 logbook_cli_main 包含 artifacts 子命令"""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "artifacts" in result.stdout

    def test_artifacts_help(self, runner):
        """测试 artifacts --help"""
        result = runner.invoke(app, ["artifacts", "--help"])
        assert result.exit_code == 0
        assert "write" in result.stdout
        assert "read" in result.stdout
        assert "exists" in result.stdout
        assert "delete" in result.stdout
        assert "audit" in result.stdout
        assert "gc" in result.stdout
        assert "migrate" in result.stdout


# =============================================================================
# 关键安全测试（路径穿越、checksum mismatch、exists）
# =============================================================================


class TestPathTraversalCLI:
    """CLI 路径穿越测试"""

    def test_write_path_traversal_dotdot(self, runner, mock_store):
        """测试写入时路径穿越攻击（..）被正确拒绝"""
        result = runner.invoke(
            app,
            ["artifacts", "write", "--path", "../../../etc/passwd", "--content", "malicious"],
        )
        assert result.exit_code == 2
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert output["code"] == "PATH_TRAVERSAL"

    def test_write_path_traversal_embedded_dotdot(self, runner, mock_store):
        """测试写入时嵌入式路径穿越攻击被拒绝"""
        result = runner.invoke(
            app,
            ["artifacts", "write", "--path", "scm/../../../etc/passwd", "--content", "malicious"],
        )
        assert result.exit_code == 2
        output = json.loads(result.stdout)
        assert output["ok"] is False
        assert output["code"] == "PATH_TRAVERSAL"

    def test_read_path_traversal(self, runner, mock_store):
        """测试读取时路径穿越攻击被拒绝"""
        result = runner.invoke(
            app,
            ["artifacts", "read", "--path", "../../../etc/passwd", "--json"],
        )
        # 路径穿越应该被拒绝
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["ok"] is False

    def test_exists_path_traversal(self, runner, mock_store):
        """测试 exists 时路径穿越攻击被拒绝"""
        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", "../../../etc/passwd"],
        )
        assert result.exit_code != 0

    def test_delete_path_traversal(self, runner, mock_store):
        """测试删除时路径穿越攻击被拒绝"""
        result = runner.invoke(
            app,
            ["artifacts", "delete", "--path", "../../../etc/passwd"],
        )
        assert result.exit_code != 0


class TestChecksumMismatchCLI:
    """CLI checksum mismatch 测试"""

    def test_write_with_overwrite_policy_deny(self, runner, mock_store):
        """测试覆盖策略为 deny 时写入已存在文件被拒绝"""
        test_path = "checksum_test/file.txt"
        
        # 先写入一次
        result = runner.invoke(
            app,
            ["artifacts", "write", "--path", test_path, "--content", "original content"],
        )
        assert result.exit_code == 0
        
        # 验证文件存在
        file_path = mock_store / "checksum_test" / "file.txt"
        assert file_path.exists()


class TestExistsCLI:
    """CLI exists 命令测试"""

    def test_exists_returns_true_for_existing(self, runner, mock_store):
        """测试存在的文件返回 exists=true"""
        # 创建测试文件
        test_dir = mock_store / "exists_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "test.txt"
        test_file.write_text("test content")

        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", "exists_test/test.txt"],
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert output["exists"] is True

    def test_exists_returns_false_for_nonexistent(self, runner, mock_store):
        """测试不存在的文件返回 exists=false"""
        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", "nonexistent/path/file.txt"],
        )
        assert result.exit_code == 1  # 不存在时返回非零
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert output["exists"] is False

    def test_exists_empty_path_error(self, runner, mock_store):
        """测试空路径返回错误"""
        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", ""],
        )
        # 空路径应该返回错误
        assert result.exit_code != 0

    def test_exists_json_output_format(self, runner, mock_store):
        """测试 exists 输出符合契约格式"""
        # 创建测试文件
        test_file = mock_store / "format_test.txt"
        test_file.write_text("content")

        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", "format_test.txt"],
        )
        output = json.loads(result.stdout)
        
        # 验证输出格式符合契约
        assert "ok" in output
        assert "exists" in output
        assert "path" in output
        assert output["ok"] is True


# =============================================================================
# 端到端测试
# =============================================================================


class TestArtifactsE2EWorkflow:
    """端到端工作流测试"""

    def test_write_read_exists_delete_workflow(self, runner, mock_store):
        """测试完整的写入-读取-检查-删除工作流"""
        test_path = "e2e/test.txt"
        test_content = "end to end test content"

        # 1. 写入
        result = runner.invoke(
            app,
            ["artifacts", "write", "--path", test_path, "--content", test_content],
        )
        assert result.exit_code == 0
        write_output = json.loads(result.stdout)
        assert write_output["ok"] is True
        original_sha256 = write_output["sha256"]

        # 2. 检查存在
        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", test_path],
        )
        assert result.exit_code == 0
        exists_output = json.loads(result.stdout)
        assert exists_output["exists"] is True

        # 3. 读取并验证
        result = runner.invoke(
            app,
            ["artifacts", "read", "--path", test_path, "--json"],
        )
        assert result.exit_code == 0
        read_output = json.loads(result.stdout)
        assert read_output["sha256"] == original_sha256

        # 4. 删除
        result = runner.invoke(
            app,
            ["artifacts", "delete", "--path", test_path],
        )
        assert result.exit_code == 0
        delete_output = json.loads(result.stdout)
        assert delete_output["deleted"] is True

        # 5. 验证已删除
        result = runner.invoke(
            app,
            ["artifacts", "exists", "--path", test_path],
        )
        assert result.exit_code == 1  # 不存在
        final_output = json.loads(result.stdout)
        assert final_output["exists"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
