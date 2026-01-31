# -*- coding: utf-8 -*-
"""
SCM CLI 仓库管理命令单元测试

测试 engram-scm 的 ensure-repo、list-repos、get-repo 子命令。

覆盖:
1. CLI 参数解析
2. 数据库写入/读取（ensure-repo, list-repos, get-repo）
3. 错误处理（无 DSN、无效参数）
4. JSON 输出格式
"""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from engram.logbook.cli.scm import (
    EXIT_ERROR,
    EXIT_INVALID_ARGS,
    EXIT_NO_DSN,
    EXIT_SUCCESS,
    _get_dsn,
    _handle_ensure_repo,
    _handle_get_repo,
    _handle_list_repos,
    main,
)
from engram.logbook.errors import EngramConfigError


class TestGetDsn:
    """测试 _get_dsn 函数"""

    def test_dsn_from_args(self):
        """--dsn 参数优先"""
        args = MagicMock()
        args.dsn = "postgresql://user:pass@localhost/db"
        args.config = None
        dsn = _get_dsn(args)
        assert dsn == "postgresql://user:pass@localhost/db"

    def test_dsn_from_env(self, monkeypatch):
        """POSTGRES_DSN 环境变量"""
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://env@localhost/envdb")
        args = MagicMock()
        args.dsn = None
        args.config = None
        dsn = _get_dsn(args)
        assert dsn == "postgresql://env@localhost/envdb"

    def test_no_dsn_raises_error(self, monkeypatch):
        """无 DSN 时抛出 EngramConfigError"""
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        args = MagicMock()
        args.dsn = None
        args.config = None
        with pytest.raises(EngramConfigError) as exc_info:
            _get_dsn(args)
        assert exc_info.value.exit_code == EXIT_NO_DSN

    def test_dsn_from_config_file(self, tmp_path, monkeypatch):
        """从配置文件读取 DSN"""
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        config_file = tmp_path / "config.toml"
        config_file.write_text('[postgres]\ndsn = "postgresql://config@localhost/configdb"\n')

        args = MagicMock()
        args.dsn = None
        args.config = str(config_file)
        dsn = _get_dsn(args)
        assert dsn == "postgresql://config@localhost/configdb"


class TestEnsureRepoCommand:
    """测试 ensure-repo 子命令"""

    def test_ensure_repo_creates_new_repo(self, migrated_db):
        """ensure-repo 创建新仓库"""
        dsn = migrated_db["dsn"]
        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_type = "git"
        args.repo_url = "https://gitlab.com/test/new-repo"
        args.project_key = "test_project"
        args.default_branch = "main"

        opts = {"pretty": False, "quiet": True, "json_out": None}

        # 捕获输出
        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_ensure_repo(args, opts)

        assert result == EXIT_SUCCESS
        # 验证输出
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is True
        assert "repo_id" in call_args
        assert call_args["repo_type"] == "git"
        assert call_args["url"] == "https://gitlab.com/test/new-repo"

    def test_ensure_repo_idempotent(self, migrated_db):
        """ensure-repo 是幂等操作"""
        dsn = migrated_db["dsn"]
        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_type = "git"
        args.repo_url = "https://gitlab.com/test/idempotent-repo"
        args.project_key = "test_project"
        args.default_branch = "main"

        opts = {"pretty": False, "quiet": True, "json_out": None}

        # 第一次调用
        with patch("engram.logbook.cli.scm.output_json") as mock_output1:
            result1 = _handle_ensure_repo(args, opts)
        assert result1 == EXIT_SUCCESS
        repo_id_1 = mock_output1.call_args[0][0]["repo_id"]

        # 第二次调用（相同参数）
        with patch("engram.logbook.cli.scm.output_json") as mock_output2:
            result2 = _handle_ensure_repo(args, opts)
        assert result2 == EXIT_SUCCESS
        repo_id_2 = mock_output2.call_args[0][0]["repo_id"]

        # 应该返回相同的 repo_id
        assert repo_id_1 == repo_id_2

    def test_ensure_repo_no_dsn(self, monkeypatch):
        """ensure-repo 无 DSN 时返回清晰错误"""
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        args = MagicMock()
        args.dsn = None
        args.config = None
        args.repo_type = "git"
        args.repo_url = "https://gitlab.com/test/no-dsn"
        args.project_key = None
        args.default_branch = None

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_ensure_repo(args, opts)

        assert result == EXIT_NO_DSN
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is False
        assert "hint" in call_args.get("detail", {})


class TestListReposCommand:
    """测试 list-repos 子命令"""

    def test_list_repos_empty(self, migrated_db):
        """list-repos 空列表"""
        dsn = migrated_db["dsn"]
        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_type = None
        args.limit = 100

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_list_repos(args, opts)

        assert result == EXIT_SUCCESS
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is True
        assert "repos" in call_args
        assert "count" in call_args

    def test_list_repos_with_data(self, migrated_db):
        """list-repos 返回仓库数据"""
        dsn = migrated_db["dsn"]

        # 先创建一些仓库
        from engram.logbook.scm_db import get_conn, upsert_repo

        with get_conn(dsn) as conn:
            upsert_repo(conn, "git", "https://gitlab.com/test/list-repo-1")
            upsert_repo(conn, "svn", "svn://example.com/list-repo-2")
            conn.commit()

        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_type = None
        args.limit = 100

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_list_repos(args, opts)

        assert result == EXIT_SUCCESS
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is True
        assert call_args["count"] >= 2

    def test_list_repos_filter_by_type(self, migrated_db):
        """list-repos 按类型过滤"""
        dsn = migrated_db["dsn"]

        # 先创建仓库
        from engram.logbook.scm_db import get_conn, upsert_repo

        with get_conn(dsn) as conn:
            upsert_repo(conn, "git", "https://gitlab.com/test/filter-git")
            upsert_repo(conn, "svn", "svn://example.com/filter-svn")
            conn.commit()

        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_type = "git"
        args.limit = 100

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_list_repos(args, opts)

        assert result == EXIT_SUCCESS
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is True
        # 所有返回的仓库都应该是 git 类型
        for repo in call_args["repos"]:
            assert repo["repo_type"] == "git"

    def test_list_repos_no_dsn(self, monkeypatch):
        """list-repos 无 DSN 时返回清晰错误"""
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        args = MagicMock()
        args.dsn = None
        args.config = None
        args.repo_type = None
        args.limit = 100

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_list_repos(args, opts)

        assert result == EXIT_NO_DSN


class TestGetRepoCommand:
    """测试 get-repo 子命令"""

    def test_get_repo_by_url(self, migrated_db):
        """get-repo 按 URL 查询"""
        dsn = migrated_db["dsn"]

        # 先创建仓库
        from engram.logbook.scm_db import get_conn, upsert_repo

        with get_conn(dsn) as conn:
            repo_id = upsert_repo(
                conn, "git", "https://gitlab.com/test/get-by-url", project_key="test_key"
            )
            conn.commit()

        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_id = None
        args.repo_type = "git"
        args.repo_url = "https://gitlab.com/test/get-by-url"

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_get_repo(args, opts)

        assert result == EXIT_SUCCESS
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is True
        assert call_args["repo"]["repo_id"] == repo_id
        assert call_args["repo"]["project_key"] == "test_key"

    def test_get_repo_by_id(self, migrated_db):
        """get-repo 按 ID 查询"""
        dsn = migrated_db["dsn"]

        # 先创建仓库
        from engram.logbook.scm_db import get_conn, upsert_repo

        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/get-by-id")
            conn.commit()

        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_id = repo_id
        args.repo_type = None
        args.repo_url = None

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_get_repo(args, opts)

        assert result == EXIT_SUCCESS
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is True
        assert call_args["repo"]["repo_id"] == repo_id

    def test_get_repo_not_found(self, migrated_db):
        """get-repo 仓库不存在"""
        dsn = migrated_db["dsn"]

        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_id = 999999  # 不存在的 ID
        args.repo_type = None
        args.repo_url = None

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_get_repo(args, opts)

        assert result == EXIT_ERROR
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is False
        assert call_args["code"] == "REPO_NOT_FOUND"

    def test_get_repo_invalid_args(self, migrated_db):
        """get-repo 参数不完整"""
        dsn = migrated_db["dsn"]

        args = MagicMock()
        args.dsn = dsn
        args.config = None
        args.repo_id = None  # 没有 repo_id
        args.repo_type = "git"  # 只有 repo_type 没有 repo_url
        args.repo_url = None

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_get_repo(args, opts)

        assert result == EXIT_INVALID_ARGS
        call_args = mock_output.call_args[0][0]
        assert call_args["ok"] is False
        assert call_args["code"] == "INVALID_ARGS"

    def test_get_repo_no_dsn(self, monkeypatch):
        """get-repo 无 DSN 时返回清晰错误"""
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        args = MagicMock()
        args.dsn = None
        args.config = None
        args.repo_id = 1
        args.repo_type = None
        args.repo_url = None

        opts = {"pretty": False, "quiet": True, "json_out": None}

        with patch("engram.logbook.cli.scm.output_json") as mock_output:
            result = _handle_get_repo(args, opts)

        assert result == EXIT_NO_DSN


class TestCLIArgumentParsing:
    """测试 CLI 参数解析"""

    def test_main_help(self):
        """--help 显示帮助信息"""
        with patch("sys.argv", ["engram-scm", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_main_no_command(self, capsys):
        """无命令时显示帮助"""
        with patch("sys.argv", ["engram-scm"]):
            result = main()
        assert result == EXIT_SUCCESS

    def test_ensure_repo_requires_args(self, capsys):
        """ensure-repo 需要必需参数"""
        with patch("sys.argv", ["engram-scm", "ensure-repo"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # argparse 缺少必需参数返回 2
            assert exc_info.value.code == 2


class TestCLIIntegration:
    """CLI 集成测试（通过 subprocess 调用）"""

    def test_help_via_subprocess(self):
        """通过 subprocess 调用 --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "engram-scm" in result.stdout.lower() or "usage" in result.stdout.lower()

    def test_ensure_repo_via_subprocess(self, migrated_db):
        """通过 subprocess 调用 ensure-repo"""
        dsn = migrated_db["dsn"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm",
                "ensure-repo",
                "--dsn",
                dsn,
                "--repo-type",
                "git",
                "--repo-url",
                "https://gitlab.com/test/subprocess-test",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "repo_id" in data

    def test_list_repos_via_subprocess(self, migrated_db):
        """通过 subprocess 调用 list-repos"""
        dsn = migrated_db["dsn"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm",
                "list-repos",
                "--dsn",
                dsn,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "repos" in data

    def test_get_repo_via_subprocess(self, migrated_db):
        """通过 subprocess 调用 get-repo"""
        dsn = migrated_db["dsn"]

        # 先创建仓库
        from engram.logbook.scm_db import get_conn, upsert_repo

        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/subprocess-get")
            conn.commit()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm",
                "get-repo",
                "--dsn",
                dsn,
                "--repo-id",
                str(repo_id),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["repo"]["repo_id"] == repo_id

    def test_no_dsn_error_message(self, monkeypatch):
        """无 DSN 时输出清晰的错误信息"""
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm",
                "list-repos",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={k: v for k, v in monkeypatch._ENV.items() if k != "POSTGRES_DSN"},
        )
        assert result.returncode == EXIT_NO_DSN
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "hint" in data.get("detail", {}) or "dsn" in data.get("message", "").lower()


class TestScmDbGetRepoById:
    """测试 scm_db.get_repo_by_id 函数"""

    def test_get_repo_by_id_exists(self, migrated_db):
        """get_repo_by_id 查询存在的仓库"""
        from engram.logbook.scm_db import get_conn, get_repo_by_id, upsert_repo

        dsn = migrated_db["dsn"]

        with get_conn(dsn) as conn:
            repo_id = upsert_repo(
                conn, "git", "https://gitlab.com/test/get-by-id-exists", project_key="test_key"
            )
            conn.commit()

            repo = get_repo_by_id(conn, repo_id)

        assert repo is not None
        assert repo["repo_id"] == repo_id
        assert repo["repo_type"] == "git"
        assert repo["url"] == "https://gitlab.com/test/get-by-id-exists"
        assert repo["project_key"] == "test_key"

    def test_get_repo_by_id_not_exists(self, migrated_db):
        """get_repo_by_id 查询不存在的仓库返回 None"""
        from engram.logbook.scm_db import get_conn, get_repo_by_id

        dsn = migrated_db["dsn"]

        with get_conn(dsn) as conn:
            repo = get_repo_by_id(conn, 999999)

        assert repo is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
