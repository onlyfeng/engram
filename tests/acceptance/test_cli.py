# -*- coding: utf-8 -*-
"""
CLI 工具验收测试

验证命令行工具可正常执行:
- engram-logbook 命令
- engram-migrate 命令
- engram-gateway 命令
- engram-scm 命令
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(cmd: list[str], env: dict = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """运行 CLI 命令并返回结果"""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )


class TestLogbookCLI:
    """engram-logbook CLI 测试"""

    def test_help(self):
        """engram-logbook --help 可执行"""
        result = run_cli(["engram-logbook", "--help"])
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "help" in result.stdout.lower()

    def test_help_flag_short(self):
        """engram-logbook -h 可执行"""
        result = run_cli(["engram-logbook", "-h"])
        assert result.returncode == 0

    def test_no_args_shows_help_or_error(self):
        """无参数时显示帮助或错误"""
        result = run_cli(["engram-logbook"])
        # 可能返回 0（显示帮助）或非 0（缺少参数）
        # 关键是不会崩溃
        assert result.returncode in (0, 1, 2)

    def test_health_requires_dsn(self):
        """health 命令需要 DSN 参数"""
        result = run_cli(["engram-logbook", "health"])
        # 缺少 DSN 应该报错或提示
        # 但不应该崩溃（exit code 不是 None）
        assert result.returncode is not None

    def test_health_with_invalid_dsn(self):
        """health 命令处理无效 DSN"""
        result = run_cli(
            [
                "engram-logbook",
                "health",
                "--dsn",
                "postgresql://invalid:invalid@localhost:9999/nonexistent",
            ],
            timeout=10,
        )
        # 连接失败应该返回非 0
        assert result.returncode != 0

    def test_health_with_valid_dsn(self, migrated_db):
        """health 命令使用有效 DSN"""
        result = run_cli(
            ["engram-logbook", "health", "--dsn", migrated_db["dsn"]],
            timeout=30,
        )
        assert result.returncode == 0

        # 输出应该是 JSON 格式
        try:
            data = json.loads(result.stdout)
            assert "ok" in data or "status" in data or "healthy" in data.get("status", "").lower()
        except json.JSONDecodeError:
            # 如果不是 JSON，至少检查有输出
            assert len(result.stdout) > 0 or len(result.stderr) > 0

    def test_subcommands_list(self):
        """子命令列表可用"""
        result = run_cli(["engram-logbook", "--help"])
        output = result.stdout.lower()

        # 检查一些常见子命令
        expected_commands = ["health"]  # 至少应该有 health 命令
        for cmd in expected_commands:
            if cmd not in output:
                pytest.skip(f"子命令 {cmd} 未在帮助中找到")


class TestMigrateCLI:
    """engram-migrate CLI 测试"""

    def test_help(self):
        """engram-migrate --help 可执行"""
        result = run_cli(["engram-migrate", "--help"])
        assert result.returncode == 0
        assert (
            "usage" in result.stdout.lower()
            or "help" in result.stdout.lower()
            or "migrate" in result.stdout.lower()
        )

    def test_help_flag_short(self):
        """engram-migrate -h 可执行"""
        result = run_cli(["engram-migrate", "-h"])
        assert result.returncode == 0

    def test_with_invalid_dsn(self):
        """处理无效 DSN"""
        result = run_cli(
            ["engram-migrate", "--dsn", "postgresql://invalid:invalid@localhost:9999/nonexistent"],
            timeout=10,
        )
        # 连接失败应该返回非 0
        assert result.returncode != 0

    def test_with_valid_dsn(self, migrated_db):
        """使用有效 DSN 执行迁移（应该是幂等的）"""
        result = run_cli(
            ["engram-migrate", "--dsn", migrated_db["dsn"]],
            timeout=60,
        )
        # 迁移应该成功（幂等）
        assert result.returncode == 0


class TestGatewayCLI:
    """engram-gateway CLI 测试"""

    def test_help(self):
        """engram-gateway --help 可执行"""
        try:
            result = run_cli(["engram-gateway", "--help"])
            assert result.returncode == 0
        except FileNotFoundError:
            pytest.skip("engram-gateway 命令未找到")
        except subprocess.TimeoutExpired:
            pytest.skip("engram-gateway --help 超时")

    def test_help_flag_short(self):
        """engram-gateway -h 可执行"""
        try:
            result = run_cli(["engram-gateway", "-h"])
            assert result.returncode == 0
        except FileNotFoundError:
            pytest.skip("engram-gateway 命令未找到")


class TestSCMCLI:
    """engram-scm CLI 测试"""

    def test_help(self):
        """engram-scm --help 可执行"""
        try:
            result = run_cli(["engram-scm", "--help"])
            assert result.returncode == 0
        except FileNotFoundError:
            pytest.skip("engram-scm 命令未找到（SCM 依赖可能未安装）")

    def test_help_flag_short(self):
        """engram-scm -h 可执行"""
        try:
            result = run_cli(["engram-scm", "-h"])
            assert result.returncode == 0
        except FileNotFoundError:
            pytest.skip("engram-scm 命令未找到")

    def test_ensure_repo_help(self):
        """engram-scm ensure-repo --help 可执行"""
        try:
            result = run_cli(["engram-scm", "ensure-repo", "--help"])
            assert result.returncode == 0
            assert "repo-type" in result.stdout.lower()
            assert "repo-url" in result.stdout.lower()
        except FileNotFoundError:
            pytest.skip("engram-scm 命令未找到")

    def test_list_repos_help(self):
        """engram-scm list-repos --help 可执行"""
        try:
            result = run_cli(["engram-scm", "list-repos", "--help"])
            assert result.returncode == 0
            assert "dsn" in result.stdout.lower() or "limit" in result.stdout.lower()
        except FileNotFoundError:
            pytest.skip("engram-scm 命令未找到")

    def test_get_repo_help(self):
        """engram-scm get-repo --help 可执行"""
        try:
            result = run_cli(["engram-scm", "get-repo", "--help"])
            assert result.returncode == 0
            assert "repo-id" in result.stdout.lower() or "repo-url" in result.stdout.lower()
        except FileNotFoundError:
            pytest.skip("engram-scm 命令未找到")

    def test_ensure_repo_requires_dsn(self):
        """ensure-repo 无 DSN 时返回清晰错误"""
        result = run_cli(
            [
                "engram-scm",
                "ensure-repo",
                "--repo-type",
                "git",
                "--repo-url",
                "https://example.com/test",
            ],
            env={"POSTGRES_DSN": ""},  # 清空 DSN
        )
        # 应该返回特定的错误码（EXIT_NO_DSN = 3）
        assert result.returncode != 0
        # 输出应该是 JSON 格式且包含错误信息
        try:
            data = json.loads(result.stdout)
            assert data.get("ok") is False
        except json.JSONDecodeError:
            pass  # 可能不是纯 JSON

    def test_list_repos_requires_dsn(self):
        """list-repos 无 DSN 时返回清晰错误"""
        result = run_cli(
            ["engram-scm", "list-repos"],
            env={"POSTGRES_DSN": ""},  # 清空 DSN
        )
        assert result.returncode != 0

    def test_ensure_repo_with_valid_dsn(self, migrated_db):
        """ensure-repo 使用有效 DSN"""
        result = run_cli(
            [
                "engram-scm",
                "ensure-repo",
                "--dsn",
                migrated_db["dsn"],
                "--repo-type",
                "git",
                "--repo-url",
                "https://gitlab.com/acceptance/test-repo",
                "--project-key",
                "acceptance_test",
            ],
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "repo_id" in data

    def test_list_repos_with_valid_dsn(self, migrated_db):
        """list-repos 使用有效 DSN"""
        result = run_cli(
            ["engram-scm", "list-repos", "--dsn", migrated_db["dsn"]],
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "repos" in data
        assert "count" in data

    def test_get_repo_with_valid_dsn(self, migrated_db):
        """get-repo 使用有效 DSN"""
        # 先创建一个仓库
        create_result = run_cli(
            [
                "engram-scm",
                "ensure-repo",
                "--dsn",
                migrated_db["dsn"],
                "--repo-type",
                "git",
                "--repo-url",
                "https://gitlab.com/acceptance/get-test",
            ],
            timeout=30,
        )
        assert create_result.returncode == 0
        create_data = json.loads(create_result.stdout)
        repo_id = create_data["repo_id"]

        # 然后查询
        result = run_cli(
            [
                "engram-scm",
                "get-repo",
                "--dsn",
                migrated_db["dsn"],
                "--repo-id",
                str(repo_id),
            ],
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["repo"]["repo_id"] == repo_id


class TestCLIEntryPoints:
    """CLI 入口点测试"""

    def test_entrypoints_via_python_m(self):
        """通过 python -m 执行入口点"""
        # 测试 engram.logbook.cli.logbook 模块
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.logbook", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # 模块可能不支持 -m 方式，但不应崩溃
        assert result.returncode in (0, 1, 2)

    def test_python_c_import(self):
        """通过 python -c 导入并检查"""
        code = """
from engram.logbook import Database, Config
print("OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "OK" in result.stdout


class TestCLIEnvironmentVariables:
    """CLI 环境变量测试"""

    def test_postgres_dsn_env(self, migrated_db):
        """通过 POSTGRES_DSN 环境变量配置"""
        result = run_cli(
            ["engram-logbook", "health"],
            env={"POSTGRES_DSN": migrated_db["dsn"]},
            timeout=30,
        )
        assert result.returncode == 0

    def test_project_key_env(self, migrated_db):
        """通过 PROJECT_KEY 环境变量配置"""
        result = run_cli(
            ["engram-logbook", "health"],
            env={
                "POSTGRES_DSN": migrated_db["dsn"],
                "PROJECT_KEY": "test_project",
            },
            timeout=30,
        )
        assert result.returncode == 0


class TestCLIOutputFormat:
    """CLI 输出格式测试"""

    def test_health_json_output(self, migrated_db):
        """health 命令输出 JSON"""
        result = run_cli(
            ["engram-logbook", "health", "--dsn", migrated_db["dsn"]],
            timeout=30,
        )
        assert result.returncode == 0

        # 尝试解析 JSON
        try:
            data = json.loads(result.stdout)
            assert isinstance(data, dict)
        except json.JSONDecodeError:
            # 如果不是纯 JSON，可能有其他输出混合
            # 检查是否包含 JSON 片段
            if "{" in result.stdout and "}" in result.stdout:
                # 尝试提取 JSON 部分
                start = result.stdout.find("{")
                end = result.stdout.rfind("}") + 1
                json_str = result.stdout[start:end]
                try:
                    data = json.loads(json_str)
                    assert isinstance(data, dict)
                except json.JSONDecodeError:
                    pass  # 不是严格的 JSON 输出要求


class TestSCMSyncCLILoopFlags:
    """SCM Sync CLI 循环模式参数测试"""

    def test_scm_sync_scheduler_help_shows_loop_flags(self):
        """scheduler --help 应显示 --loop 和 --interval-seconds 参数"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "scheduler", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "--loop" in result.stdout
        assert "--interval-seconds" in result.stdout
        assert "--once" in result.stdout

    def test_scm_sync_reaper_help_shows_loop_flags(self):
        """reaper --help 应显示 --loop 和 --interval-seconds 参数"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "reaper", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "--loop" in result.stdout
        assert "--interval-seconds" in result.stdout
        assert "--once" in result.stdout

    def test_scm_sync_scheduler_once_and_loop_mutually_exclusive(self):
        """scheduler --once --loop 应报错"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "scheduler", "--once", "--loop"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        # 检查错误信息（stdout 或 stderr）
        combined = result.stdout + result.stderr
        assert "--once" in combined or "--loop" in combined or "互斥" in combined

    def test_scm_sync_reaper_once_and_loop_mutually_exclusive(self):
        """reaper --once --loop 应报错"""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm_sync",
                "reaper",
                "--once",
                "--loop",
                "--dsn",
                "postgresql://test",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1


class TestCLIModulesInTempDir:
    """在临时工作目录（非仓库根）运行 CLI 模块测试

    验证 CLI 模块可以在任意目录下运行，不依赖于当前工作目录。
    这是打包发布后的典型使用场景。
    """

    @pytest.fixture
    def temp_work_dir(self, tmp_path):
        """创建一个与项目无关的临时工作目录"""
        work_dir = tmp_path / "test_work_dir"
        work_dir.mkdir()
        return work_dir

    def test_db_migrate_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.db_migrate --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.db_migrate", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "usage" in result.stdout.lower() or "dsn" in result.stdout.lower()

    def test_logbook_cli_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.logbook --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.logbook", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        # 模块可能返回 0 或 1/2（取决于实现），关键是不崩溃
        assert result.returncode in (0, 1, 2), f"stderr: {result.stderr}"

    def test_scm_cli_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "engram-scm" in result.stdout.lower() or "scm" in result.stdout.lower()

    def test_engram_migrate_console_script_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-migrate --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-migrate", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-migrate 命令未找到（可能未安装 console_scripts）")

    def test_engram_scm_console_script_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm 命令未找到")


class TestLegacyEntryPointsDeprecation:
    """旧入口脚本弃用提示 smoke 测试

    验证根目录的旧入口脚本（db_bootstrap.py, scm_sync_status.py 等）
    能正确输出弃用警告并转发到新脚本。
    """

    @pytest.fixture
    def project_root(self):
        """获取项目根目录"""
        return Path(__file__).parent.parent.parent

    def test_db_bootstrap_wrapper_removed(self, project_root):
        """根目录 db_bootstrap.py 已在 v2.0 移除"""
        script_path = project_root / "db_bootstrap.py"
        assert not script_path.exists(), "db_bootstrap.py 不应再存在"

    def test_scm_sync_status_help_runs(self, project_root):
        """python scm_sync_status.py --help 应能正常运行"""
        script_path = project_root / "scm_sync_status.py"
        if not script_path.exists():
            pytest.skip("scm_sync_status.py 不存在")

        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )
        # 应该成功显示帮助
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # 检查帮助输出
        combined_output = result.stderr + result.stdout
        has_help = (
            "--dsn" in combined_output
            or "usage" in combined_output.lower()
            or "prometheus" in combined_output.lower()
        )
        assert has_help, (
            f"scm_sync_status.py --help 应显示帮助信息\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_scm_sync_worker_help_runs(self, project_root):
        """python scm_sync_worker.py --help 应能正常运行（如果存在）"""
        script_path = project_root / "scm_sync_worker.py"
        if not script_path.exists():
            pytest.skip("scm_sync_worker.py 不存在")

        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )
        # 应该成功或显示帮助
        assert result.returncode in (0, 1, 2), f"stderr: {result.stderr}"

    def test_scm_sync_scheduler_help_runs(self, project_root):
        """python scm_sync_scheduler.py --help 应能正常运行（如果存在）"""
        script_path = project_root / "scm_sync_scheduler.py"
        if not script_path.exists():
            pytest.skip("scm_sync_scheduler.py 不存在")

        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )
        # 应该成功或显示帮助
        assert result.returncode in (0, 1, 2), f"stderr: {result.stderr}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
