# -*- coding: utf-8 -*-
"""
SCM Sync CLI 验收测试

验证 SCM Sync 子系统的 CLI 命令：
- engram-scm-sync （统一入口）
- engram-scm-scheduler
- engram-scm-worker
- engram-scm-reaper
- engram-scm-status
- engram-scm-runner

测试重点：
1. --help 可正常执行
2. python -m engram.logbook.cli.scm_sync <subcmd> --help 在临时目录可运行
3. 缺失 DSN 时的错误信息与退出码稳定
"""

import os
import subprocess
import sys

import pytest


def run_cli(
    cmd: list[str], env: dict = None, timeout: int = 30, cwd: str = None
) -> subprocess.CompletedProcess:
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
        cwd=cwd,
    )


class TestSCMSyncUnifiedEntry:
    """engram-scm-sync 统一入口测试"""

    def test_help(self):
        """engram-scm-sync --help 可执行"""
        try:
            result = run_cli(["engram-scm-sync", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-sync 命令未找到（可能未安装 console_scripts）")
        assert result.returncode == 0
        # 验证帮助内容包含子命令
        assert "scheduler" in result.stdout.lower()
        assert "worker" in result.stdout.lower()
        assert "reaper" in result.stdout.lower()
        assert "status" in result.stdout.lower()
        assert "runner" in result.stdout.lower()

    def test_help_flag_short(self):
        """engram-scm-sync -h 可执行"""
        try:
            result = run_cli(["engram-scm-sync", "-h"])
        except FileNotFoundError:
            pytest.skip("engram-scm-sync 命令未找到")
        assert result.returncode == 0

    def test_no_args_shows_help(self):
        """无参数时显示帮助"""
        try:
            result = run_cli(["engram-scm-sync"])
        except FileNotFoundError:
            pytest.skip("engram-scm-sync 命令未找到")
        # 应该返回 0 并显示帮助
        assert result.returncode == 0
        assert "scheduler" in result.stdout.lower() or "usage" in result.stdout.lower()

    def test_version(self):
        """engram-scm-sync --version 可执行"""
        try:
            result = run_cli(["engram-scm-sync", "--version"])
        except FileNotFoundError:
            pytest.skip("engram-scm-sync 命令未找到")
        assert result.returncode == 0
        assert "engram-scm-sync" in result.stdout.lower() or "0.1" in result.stdout


class TestSCMSchedulerCLI:
    """engram-scm-scheduler CLI 测试"""

    def test_help(self):
        """engram-scm-scheduler --help 可执行"""
        try:
            result = run_cli(["engram-scm-scheduler", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-scheduler 命令未找到")
        assert result.returncode == 0
        # 验证帮助内容
        assert "--dsn" in result.stdout or "--loop" in result.stdout
        assert "--once" in result.stdout

    def test_help_flag_short(self):
        """engram-scm-scheduler -h 可执行"""
        try:
            result = run_cli(["engram-scm-scheduler", "-h"])
        except FileNotFoundError:
            pytest.skip("engram-scm-scheduler 命令未找到")
        assert result.returncode == 0

    def test_help_shows_loop_flags(self):
        """scheduler --help 应显示 --loop 和 --interval-seconds 参数"""
        try:
            result = run_cli(["engram-scm-scheduler", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-scheduler 命令未找到")
        assert result.returncode == 0
        assert "--loop" in result.stdout
        assert "--interval-seconds" in result.stdout
        assert "--once" in result.stdout

    def test_missing_dsn_returns_error(self):
        """缺失 DSN 时返回错误退出码"""
        # 清空 DSN 环境变量
        try:
            result = run_cli(
                ["engram-scm-scheduler", "--once"],
                env={"LOGBOOK_DSN": "", "POSTGRES_DSN": ""},
            )
        except FileNotFoundError:
            pytest.skip("engram-scm-scheduler 命令未找到")
        # 应该返回非 0 退出码
        assert result.returncode != 0
        # 错误信息应包含 DSN 相关提示
        combined = result.stdout + result.stderr
        assert (
            "dsn" in combined.lower()
            or "数据库" in combined
            or "连接" in combined
            or "scm" in combined.lower()
        )

    def test_once_and_loop_mutually_exclusive(self):
        """--once 和 --loop 互斥"""
        try:
            result = run_cli(["engram-scm-scheduler", "--once", "--loop"])
        except FileNotFoundError:
            pytest.skip("engram-scm-scheduler 命令未找到")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "--once" in combined or "--loop" in combined or "互斥" in combined


class TestSCMWorkerCLI:
    """engram-scm-worker CLI 测试"""

    def test_help(self):
        """engram-scm-worker --help 可执行"""
        try:
            result = run_cli(["engram-scm-worker", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-worker 命令未找到")
        assert result.returncode == 0
        # 验证帮助内容
        assert "--worker-id" in result.stdout
        assert "--dsn" in result.stdout

    def test_help_flag_short(self):
        """engram-scm-worker -h 可执行"""
        try:
            result = run_cli(["engram-scm-worker", "-h"])
        except FileNotFoundError:
            pytest.skip("engram-scm-worker 命令未找到")
        assert result.returncode == 0

    def test_missing_worker_id_returns_error(self):
        """缺失 --worker-id 时返回错误退出码"""
        try:
            result = run_cli(["engram-scm-worker"])
        except FileNotFoundError:
            pytest.skip("engram-scm-worker 命令未找到")
        # 缺少必需参数应返回非 0
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "worker-id" in combined.lower() or "required" in combined.lower()

    def test_missing_dsn_returns_error(self):
        """缺失 DSN 时返回错误退出码"""
        try:
            result = run_cli(
                ["engram-scm-worker", "--worker-id", "test-worker"],
                env={"LOGBOOK_DSN": "", "POSTGRES_DSN": ""},
            )
        except FileNotFoundError:
            pytest.skip("engram-scm-worker 命令未找到")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "dsn" in combined.lower() or "数据库" in combined or "连接" in combined


class TestSCMReaperCLI:
    """engram-scm-reaper CLI 测试"""

    def test_help(self):
        """engram-scm-reaper --help 可执行"""
        try:
            result = run_cli(["engram-scm-reaper", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-reaper 命令未找到")
        assert result.returncode == 0
        # 验证帮助内容
        assert "--dsn" in result.stdout
        assert "--dry-run" in result.stdout

    def test_help_flag_short(self):
        """engram-scm-reaper -h 可执行"""
        try:
            result = run_cli(["engram-scm-reaper", "-h"])
        except FileNotFoundError:
            pytest.skip("engram-scm-reaper 命令未找到")
        assert result.returncode == 0

    def test_help_shows_loop_flags(self):
        """reaper --help 应显示 --loop 和 --interval-seconds 参数"""
        try:
            result = run_cli(["engram-scm-reaper", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-reaper 命令未找到")
        assert result.returncode == 0
        assert "--loop" in result.stdout
        assert "--interval-seconds" in result.stdout
        assert "--once" in result.stdout

    def test_missing_dsn_returns_error(self):
        """缺失 DSN 时返回错误退出码"""
        try:
            result = run_cli(
                ["engram-scm-reaper", "--once"],
                env={"LOGBOOK_DSN": "", "POSTGRES_DSN": ""},
            )
        except FileNotFoundError:
            pytest.skip("engram-scm-reaper 命令未找到")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "dsn" in combined.lower() or "数据库" in combined or "连接" in combined

    def test_once_and_loop_mutually_exclusive(self):
        """--once 和 --loop 互斥"""
        try:
            result = run_cli(
                ["engram-scm-reaper", "--once", "--loop", "--dsn", "postgresql://test"],
            )
        except FileNotFoundError:
            pytest.skip("engram-scm-reaper 命令未找到")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "--once" in combined or "--loop" in combined or "互斥" in combined


class TestSCMStatusCLI:
    """engram-scm-status CLI 测试"""

    def test_help(self):
        """engram-scm-status --help 可执行"""
        try:
            result = run_cli(["engram-scm-status", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-status 命令未找到")
        assert result.returncode == 0
        # 验证帮助内容
        assert "--dsn" in result.stdout
        assert "--prometheus" in result.stdout or "--json" in result.stdout

    def test_help_flag_short(self):
        """engram-scm-status -h 可执行"""
        try:
            result = run_cli(["engram-scm-status", "-h"])
        except FileNotFoundError:
            pytest.skip("engram-scm-status 命令未找到")
        assert result.returncode == 0

    def test_missing_dsn_returns_error(self):
        """缺失 DSN 时返回错误退出码"""
        try:
            result = run_cli(
                ["engram-scm-status"],
                env={"LOGBOOK_DSN": "", "POSTGRES_DSN": ""},
            )
        except FileNotFoundError:
            pytest.skip("engram-scm-status 命令未找到")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "dsn" in combined.lower() or "数据库" in combined or "连接" in combined


class TestSCMRunnerCLI:
    """engram-scm-runner CLI 测试"""

    def test_help(self):
        """engram-scm-runner --help 可执行"""
        try:
            result = run_cli(["engram-scm-runner", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-runner 命令未找到")
        assert result.returncode == 0
        # 验证帮助内容
        assert "incremental" in result.stdout.lower() or "backfill" in result.stdout.lower()

    def test_help_flag_short(self):
        """engram-scm-runner -h 可执行"""
        try:
            result = run_cli(["engram-scm-runner", "-h"])
        except FileNotFoundError:
            pytest.skip("engram-scm-runner 命令未找到")
        assert result.returncode == 0

    def test_incremental_help(self):
        """engram-scm-runner incremental --help 可执行"""
        try:
            result = run_cli(["engram-scm-runner", "incremental", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-runner 命令未找到")
        assert result.returncode == 0
        assert "--repo" in result.stdout

    def test_backfill_help(self):
        """engram-scm-runner backfill --help 可执行"""
        try:
            result = run_cli(["engram-scm-runner", "backfill", "--help"])
        except FileNotFoundError:
            pytest.skip("engram-scm-runner 命令未找到")
        assert result.returncode == 0
        assert "--repo" in result.stdout


class TestSCMSyncCLIModulesInTempDir:
    """在临时工作目录（非仓库根）运行 SCM Sync CLI 模块测试

    验证 CLI 模块可以在任意目录下运行，不依赖于当前工作目录。
    这是打包发布后的典型使用场景。
    """

    @pytest.fixture
    def temp_work_dir(self, tmp_path):
        """创建一个与项目无关的临时工作目录"""
        work_dir = tmp_path / "test_work_dir"
        work_dir.mkdir()
        return work_dir

    def test_scm_sync_main_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm_sync --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "scheduler" in result.stdout.lower()

    def test_scm_sync_scheduler_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm_sync scheduler --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "scheduler", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "--loop" in result.stdout or "--once" in result.stdout

    def test_scm_sync_worker_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm_sync worker --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "worker", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "--worker-id" in result.stdout

    def test_scm_sync_reaper_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm_sync reaper --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "reaper", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "--dry-run" in result.stdout

    def test_scm_sync_status_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm_sync status --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "status", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "--dsn" in result.stdout or "--prometheus" in result.stdout

    def test_scm_sync_runner_help_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 python -m engram.logbook.cli.scm_sync runner --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "runner", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(temp_work_dir),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # runner 子命令应该有 incremental 和 backfill
        combined = result.stdout.lower()
        assert "incremental" in combined or "backfill" in combined

    def test_console_script_engram_scm_sync_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm-sync --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm-sync", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm-sync 命令未找到（可能未安装 console_scripts）")

    def test_console_script_engram_scm_scheduler_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm-scheduler --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm-scheduler", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm-scheduler 命令未找到")

    def test_console_script_engram_scm_worker_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm-worker --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm-worker", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm-worker 命令未找到")

    def test_console_script_engram_scm_reaper_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm-reaper --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm-reaper", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm-reaper 命令未找到")

    def test_console_script_engram_scm_status_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm-status --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm-status", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm-status 命令未找到")

    def test_console_script_engram_scm_runner_in_temp_dir(self, temp_work_dir):
        """在临时目录运行 engram-scm-runner --help（console_scripts 入口）"""
        try:
            result = subprocess.run(
                ["engram-scm-runner", "--help"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(temp_work_dir),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
        except FileNotFoundError:
            pytest.skip("engram-scm-runner 命令未找到")


class TestSCMSyncCLIMissingDSNErrorCodes:
    """缺失 DSN 时的错误退出码稳定性测试

    验证各命令在缺失 DSN 时返回一致的错误退出码（非 0）且不会崩溃。
    使用 python -m 方式调用以避免 console_scripts 未安装的问题。
    """

    @pytest.fixture
    def no_dsn_env(self):
        """清空 DSN 环境变量的配置"""
        return {"LOGBOOK_DSN": "", "POSTGRES_DSN": ""}

    def test_scheduler_missing_dsn_exit_code_stable(self, no_dsn_env):
        """scheduler 缺失 DSN 时退出码稳定"""
        result1 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "scheduler", "--once"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        result2 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "scheduler", "--once"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        # 退出码应一致且非 0
        assert result1.returncode == result2.returncode
        assert result1.returncode != 0

    def test_worker_missing_dsn_exit_code_stable(self, no_dsn_env):
        """worker 缺失 DSN 时退出码稳定"""
        result1 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "worker", "--worker-id", "test"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        result2 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "worker", "--worker-id", "test"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        assert result1.returncode == result2.returncode
        assert result1.returncode != 0

    def test_reaper_missing_dsn_exit_code_stable(self, no_dsn_env):
        """reaper 缺失 DSN 时退出码稳定"""
        result1 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "reaper", "--once"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        result2 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "reaper", "--once"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        assert result1.returncode == result2.returncode
        assert result1.returncode != 0

    def test_status_missing_dsn_exit_code_stable(self, no_dsn_env):
        """status 缺失 DSN 时退出码稳定"""
        result1 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        result2 = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **no_dsn_env},
        )
        assert result1.returncode == result2.returncode
        assert result1.returncode != 0

    def test_missing_dsn_error_message_contains_hint(self, no_dsn_env):
        """缺失 DSN 时的错误信息应包含配置相关提示

        注意：某些命令（如 scheduler）可能先检查 SCM 同步功能是否启用，
        然后才检查 DSN。因此错误信息可能是关于 SCM 同步未启用的提示，
        而非 DSN 提示。两种情况都是可接受的。
        """
        commands = [
            ["scheduler", "--once"],
            ["worker", "--worker-id", "test"],
            ["reaper", "--once"],
            ["status"],
        ]

        for subcmd in commands:
            result = subprocess.run(
                [sys.executable, "-m", "engram.logbook.cli.scm_sync"] + subcmd,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, **no_dsn_env},
            )
            combined = result.stdout + result.stderr
            # 错误信息应包含配置相关的关键词（DSN 或 SCM 同步启用）
            has_hint = any(
                keyword in combined.lower()
                for keyword in [
                    "dsn",
                    "数据库",
                    "连接",
                    "logbook_dsn",
                    "postgres_dsn",
                    "环境变量",
                    "scm",
                    "同步",
                    "启用",
                    "enabled",
                ]
            )
            assert has_hint, (
                f"命令 {subcmd} 的错误信息应包含配置提示，实际输出:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )


class TestSCMSyncCLIModuleDirectExec:
    """直接通过 python -m 执行模块测试"""

    def test_module_exec_without_args_shows_help(self):
        """python -m engram.logbook.cli.scm_sync 无参数显示帮助"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "scheduler" in result.stdout.lower()

    def test_module_exec_scheduler_help(self):
        """python -m engram.logbook.cli.scm_sync scheduler --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "scheduler", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "--loop" in result.stdout

    def test_module_exec_admin_help(self):
        """python -m engram.logbook.cli.scm_sync admin --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "admin", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        # admin 子命令应该有 jobs/locks/pauses/cursors
        combined = result.stdout.lower()
        assert "jobs" in combined or "locks" in combined


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
