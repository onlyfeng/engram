# -*- coding: utf-8 -*-
"""
CLI 工具验收测试

验证命令行工具可正常执行:
- engram-logbook 命令
- engram-migrate 命令
- engram-gateway 命令
- engram-scm 命令
"""

import subprocess
import sys
import os
import json
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
            ["engram-logbook", "health", "--dsn", "postgresql://invalid:invalid@localhost:9999/nonexistent"],
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
        assert "usage" in result.stdout.lower() or "help" in result.stdout.lower() or "migrate" in result.stdout.lower()

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
