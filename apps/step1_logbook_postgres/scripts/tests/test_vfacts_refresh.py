#!/usr/bin/env python3
"""
test_vfacts_refresh.py - scm.v_facts 物化视图刷新测试

测试内容:
- refresh_vfacts 函数的正确性
- CLI scm refresh-vfacts 命令
- 刷新后行数/时间戳更新符合预期
"""

import json
import os
import sys
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, ANY

# 确保 scripts 目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRefreshVfactsFunction:
    """测试 refresh_vfacts 函数"""
    
    def test_refresh_dry_run(self):
        """测试 dry-run 模式"""
        from scm_sync_runner import refresh_vfacts
        
        result = refresh_vfacts(dry_run=True)
        
        assert result["dry_run"] is True
        assert result["refreshed"] is False
        assert result["concurrently"] is False
    
    def test_refresh_dry_run_concurrently(self):
        """测试 dry-run + concurrently 模式"""
        from scm_sync_runner import refresh_vfacts
        
        result = refresh_vfacts(dry_run=True, concurrently=True)
        
        assert result["dry_run"] is True
        assert result["refreshed"] is False
        assert result["concurrently"] is True
    
    @patch("scm_sync_runner.get_connection")
    def test_refresh_success(self, mock_get_connection):
        """测试正常刷新成功"""
        from scm_sync_runner import refresh_vfacts
        
        # 模拟数据库连接和游标
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_connection.return_value = mock_conn
        
        # 模拟查询结果
        mock_cursor.fetchone.side_effect = [(100,), (120,)]  # before=100, after=120
        
        result = refresh_vfacts()
        
        assert result["refreshed"] is True
        assert result["before_row_count"] == 100
        assert result["after_row_count"] == 120
        assert result["concurrently"] is False
        assert "duration_ms" in result
        assert "refreshed_at" in result
        
        # 验证执行了正确的 SQL
        calls = mock_cursor.execute.call_args_list
        assert any("REFRESH MATERIALIZED VIEW scm.v_facts" in str(call) for call in calls)
        mock_conn.commit.assert_called_once()
    
    @patch("scm_sync_runner.get_connection")
    def test_refresh_concurrently(self, mock_get_connection):
        """测试 CONCURRENTLY 模式刷新"""
        from scm_sync_runner import refresh_vfacts
        
        # 模拟数据库连接和游标
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_connection.return_value = mock_conn
        
        # 模拟查询结果
        mock_cursor.fetchone.side_effect = [(50,), (55,)]
        
        result = refresh_vfacts(concurrently=True)
        
        assert result["refreshed"] is True
        assert result["concurrently"] is True
        
        # 验证执行了 CONCURRENTLY 模式的 SQL
        calls = mock_cursor.execute.call_args_list
        assert any("REFRESH MATERIALIZED VIEW CONCURRENTLY scm.v_facts" in str(call) for call in calls)
    
    @patch("scm_sync_runner.get_connection")
    def test_refresh_error_handling(self, mock_get_connection):
        """测试刷新失败时的错误处理"""
        from scm_sync_runner import refresh_vfacts
        
        # 模拟连接失败
        mock_get_connection.side_effect = Exception("Connection failed")
        
        result = refresh_vfacts()
        
        assert result["refreshed"] is False
        assert "error" in result
        assert "Connection failed" in result["error"]


class TestRefreshVfactsResultFields:
    """测试刷新结果字段"""
    
    @patch("scm_sync_runner.get_connection")
    def test_result_has_all_fields(self, mock_get_connection):
        """测试结果包含所有必要字段"""
        from scm_sync_runner import refresh_vfacts
        
        # 模拟数据库连接和游标
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_connection.return_value = mock_conn
        mock_cursor.fetchone.side_effect = [(10,), (15,)]
        
        result = refresh_vfacts()
        
        # 验证必要字段
        assert "refreshed" in result
        assert "concurrently" in result
        assert "before_row_count" in result
        assert "after_row_count" in result
        assert "duration_ms" in result
        assert "refreshed_at" in result
        
        # 验证类型
        assert isinstance(result["refreshed"], bool)
        assert isinstance(result["before_row_count"], int)
        assert isinstance(result["after_row_count"], int)
        assert isinstance(result["duration_ms"], float)


class TestSyncResultVfactsFields:
    """测试 SyncResult 中的 vfacts 相关字段"""
    
    def test_sync_result_has_vfacts_fields(self):
        """测试 SyncResult 包含 vfacts 刷新字段"""
        from scm_sync_runner import SyncResult
        
        result = SyncResult(
            phase="incremental",
            repo="gitlab:123",
        )
        
        assert hasattr(result, "vfacts_refreshed")
        assert hasattr(result, "vfacts_refresh_info")
        assert result.vfacts_refreshed is False
        assert result.vfacts_refresh_info is None
    
    def test_sync_result_to_dict_includes_vfacts(self):
        """测试 to_dict 包含 vfacts 字段"""
        from scm_sync_runner import SyncResult
        
        result = SyncResult(
            phase="backfill",
            repo="svn:https://svn.example.com/repo",
            vfacts_refreshed=True,
            vfacts_refresh_info={
                "before_row_count": 100,
                "after_row_count": 110,
            },
        )
        
        d = result.to_dict()
        
        assert "vfacts_refreshed" in d
        assert "vfacts_refresh_info" in d
        assert d["vfacts_refreshed"] is True
        assert d["vfacts_refresh_info"]["before_row_count"] == 100
    
    def test_sync_result_to_json_includes_vfacts(self):
        """测试 to_json 包含 vfacts 字段"""
        from scm_sync_runner import SyncResult
        
        result = SyncResult(
            phase="incremental",
            repo="gitlab:456",
            vfacts_refreshed=True,
        )
        
        json_str = result.to_json()
        data = json.loads(json_str)
        
        assert "vfacts_refreshed" in data
        assert data["vfacts_refreshed"] is True


class TestRunnerContextVfactsConfig:
    """测试 RunnerContext 中的 vfacts 刷新配置"""
    
    def test_default_auto_refresh_enabled(self):
        """测试默认启用自动刷新"""
        from scm_sync_runner import RunnerContext, RepoSpec
        
        # 检查 RunnerContext 的默认字段值
        # 通过查看 dataclass 字段定义
        from dataclasses import fields
        
        ctx_fields = {f.name: f.default for f in fields(RunnerContext) if f.default is not type(f.default)}
        
        # 默认值应该是启用自动刷新
        assert ctx_fields.get("auto_refresh_vfacts", True) is True
        assert ctx_fields.get("refresh_concurrently", False) is False
    
    @patch("scm_sync_runner.get_config")
    def test_runner_context_vfacts_defaults(self, mock_get_config):
        """测试 RunnerContext 默认 vfacts 配置"""
        from scm_sync_runner import RunnerContext, RepoSpec
        
        mock_config = MagicMock()
        mock_get_config.return_value = mock_config
        
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
        )
        
        assert ctx.auto_refresh_vfacts is True
        assert ctx.refresh_concurrently is False
    
    @patch("scm_sync_runner.get_config")
    def test_runner_context_disable_refresh(self, mock_get_config):
        """测试禁用自动刷新"""
        from scm_sync_runner import RunnerContext, RepoSpec
        
        mock_config = MagicMock()
        mock_get_config.return_value = mock_config
        
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            auto_refresh_vfacts=False,
        )
        
        assert ctx.auto_refresh_vfacts is False


class TestCLIRefreshVfactsCommand:
    """测试 CLI scm refresh-vfacts 命令"""
    
    def test_cli_has_refresh_vfacts_command(self):
        """测试 CLI 包含 refresh-vfacts 命令"""
        from step1_cli import scm_app
        
        # 获取已注册的命令
        command_names = [cmd.name for cmd in scm_app.registered_commands]
        
        assert "refresh-vfacts" in command_names
    
    @patch("step1_cli.get_connection")
    @patch("step1_cli.load_config")
    def test_cli_refresh_dry_run(self, mock_load_config, mock_get_conn, capsys):
        """测试 CLI dry-run 模式"""
        from typer.testing import CliRunner
        from step1_cli import app
        
        runner = CliRunner()
        
        # 模拟数据库连接
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn
        mock_cursor.fetchone.side_effect = [(50, 50), (None, None)]
        
        result = runner.invoke(app, ["scm", "refresh-vfacts", "--dry-run"])
        
        # 应该成功退出
        assert result.exit_code == 0
        
        # 输出应该包含 dry_run: true
        output = result.stdout
        data = json.loads(output)
        assert data["ok"] is True
        assert data["dry_run"] is True


class TestVfactsRefreshIntegration:
    """集成测试：物化视图刷新（需要数据库）"""
    
    @pytest.mark.skipif(
        os.environ.get("STEP1_DSN") is None,
        reason="需要设置 STEP1_DSN 环境变量"
    )
    def test_refresh_updates_row_count(self):
        """测试刷新后行数更新符合预期"""
        from scm_sync_runner import refresh_vfacts
        from engram_step1.db import get_connection
        
        result = refresh_vfacts()
        
        assert result["refreshed"] is True
        assert "before_row_count" in result
        assert "after_row_count" in result
        # 行数应该是非负整数
        assert result["before_row_count"] >= 0
        assert result["after_row_count"] >= 0
    
    @pytest.mark.skipif(
        os.environ.get("STEP1_DSN") is None,
        reason="需要设置 STEP1_DSN 环境变量"
    )
    def test_refresh_timestamp_updated(self):
        """测试刷新后时间戳更新"""
        from scm_sync_runner import refresh_vfacts
        
        before_time = datetime.now(timezone.utc)
        
        result = refresh_vfacts()
        
        after_time = datetime.now(timezone.utc)
        
        assert result["refreshed"] is True
        assert "refreshed_at" in result
        
        # 验证刷新时间在合理范围内
        refreshed_at = datetime.fromisoformat(result["refreshed_at"].replace("Z", "+00:00"))
        assert before_time <= refreshed_at <= after_time
    
    @pytest.mark.skipif(
        os.environ.get("STEP1_DSN") is None,
        reason="需要设置 STEP1_DSN 环境变量"
    )
    def test_refresh_concurrently_with_unique_index(self):
        """测试 CONCURRENTLY 模式（需要唯一索引）"""
        from scm_sync_runner import refresh_vfacts
        
        # v_facts 上已有唯一索引 idx_v_facts_source_id
        result = refresh_vfacts(concurrently=True)
        
        assert result["refreshed"] is True
        assert result["concurrently"] is True


class TestRunnerSyncTriggersRefresh:
    """测试同步成功后触发刷新"""
    
    @patch("scm_sync_runner.refresh_vfacts")
    @patch("scm_sync_runner.SyncRunner._run_sync_once")
    @patch("scm_sync_runner.get_config")
    def test_incremental_success_triggers_refresh(
        self,
        mock_get_config,
        mock_run_sync_once,
        mock_refresh_vfacts,
    ):
        """测试增量同步成功后触发刷新"""
        from scm_sync_runner import SyncRunner, RunnerContext, RepoSpec
        
        mock_config = MagicMock()
        mock_config.get.return_value = 60
        mock_get_config.return_value = mock_config
        
        # 模拟同步成功
        mock_run_sync_once.return_value = {
            "items_synced": 10,
            "items_skipped": 0,
            "items_failed": 0,
        }
        
        # 模拟刷新成功
        mock_refresh_vfacts.return_value = {
            "refreshed": True,
            "before_row_count": 100,
            "after_row_count": 110,
        }
        
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            auto_refresh_vfacts=True,
        )
        
        runner = SyncRunner(ctx)
        result = runner.run_incremental()
        
        # 应该触发刷新
        mock_refresh_vfacts.assert_called_once()
        assert result.vfacts_refreshed is True
        assert result.vfacts_refresh_info is not None
    
    @patch("scm_sync_runner.refresh_vfacts")
    @patch("scm_sync_runner.SyncRunner._run_sync_once")
    @patch("scm_sync_runner.get_config")
    def test_incremental_no_items_synced_skips_refresh(
        self,
        mock_get_config,
        mock_run_sync_once,
        mock_refresh_vfacts,
    ):
        """测试增量同步无新数据时跳过刷新"""
        from scm_sync_runner import SyncRunner, RunnerContext, RepoSpec
        
        mock_config = MagicMock()
        mock_config.get.return_value = 60
        mock_get_config.return_value = mock_config
        
        # 模拟同步成功但无新数据
        mock_run_sync_once.return_value = {
            "items_synced": 0,  # 无新数据
            "items_skipped": 0,
            "items_failed": 0,
        }
        
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            auto_refresh_vfacts=True,
        )
        
        runner = SyncRunner(ctx)
        result = runner.run_incremental()
        
        # 不应该触发刷新
        mock_refresh_vfacts.assert_not_called()
        assert result.vfacts_refreshed is False
    
    @patch("scm_sync_runner.refresh_vfacts")
    @patch("scm_sync_runner.SyncRunner._run_sync_once")
    @patch("scm_sync_runner.get_config")
    def test_disable_auto_refresh_skips_refresh(
        self,
        mock_get_config,
        mock_run_sync_once,
        mock_refresh_vfacts,
    ):
        """测试禁用自动刷新时跳过刷新"""
        from scm_sync_runner import SyncRunner, RunnerContext, RepoSpec
        
        mock_config = MagicMock()
        mock_config.get.return_value = 60
        mock_get_config.return_value = mock_config
        
        # 模拟同步成功
        mock_run_sync_once.return_value = {
            "items_synced": 10,
            "items_skipped": 0,
            "items_failed": 0,
        }
        
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            auto_refresh_vfacts=False,  # 禁用自动刷新
        )
        
        runner = SyncRunner(ctx)
        result = runner.run_incremental()
        
        # 不应该触发刷新
        mock_refresh_vfacts.assert_not_called()
        assert result.vfacts_refreshed is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
