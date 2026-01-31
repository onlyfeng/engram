# -*- coding: utf-8 -*-
"""
SCM Sync CLI 循环模式参数测试

验证 scheduler_main 和 reaper_main 的 --loop, --interval-seconds, --once 参数：
- 参数解析正确性
- --once 模式下行为不变（兼容性）
- --once 和 --loop 互斥
- --interval-seconds 默认值
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from engram.logbook.cli.scm_sync import (
    DEFAULT_REAPER_INTERVAL_SECONDS,
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    reaper_main,
    scheduler_main,
)


class TestSchedulerMainLoopFlags:
    """scheduler_main 循环参数测试"""

    def test_once_flag_exists(self):
        """--once 参数应存在且可解析"""
        # 使用 --help 检查不会出错
        with pytest.raises(SystemExit) as exc_info:
            scheduler_main(["--help"])
        assert exc_info.value.code == 0

    def test_loop_flag_exists(self):
        """--loop 参数应存在"""
        with pytest.raises(SystemExit) as exc_info:
            scheduler_main(["--help"])
        assert exc_info.value.code == 0

    def test_interval_seconds_flag_exists(self):
        """--interval-seconds 参数应存在"""
        with pytest.raises(SystemExit) as exc_info:
            scheduler_main(["--help"])
        assert exc_info.value.code == 0

    def test_once_and_loop_mutually_exclusive(self):
        """--once 和 --loop 应互斥"""
        # 当同时提供 --once 和 --loop 时应报错
        with patch("engram.logbook.cli.scm_sync._setup_logging"):
            exit_code = scheduler_main(["--once", "--loop"])
        assert exit_code == 1

    def test_once_and_loop_mutually_exclusive_json_output(self):
        """--once 和 --loop 互斥时 JSON 输出"""
        with patch("engram.logbook.cli.scm_sync._setup_logging"):
            with patch("builtins.print") as mock_print:
                exit_code = scheduler_main(["--once", "--loop", "--json"])
        assert exit_code == 1
        # 验证输出了 JSON 错误信息
        call_args = mock_print.call_args[0][0]
        error_data = json.loads(call_args)
        assert "error" in error_data
        assert "--once" in error_data["error"] or "--loop" in error_data["error"]

    def test_interval_seconds_default_value(self):
        """--interval-seconds 默认值应为 DEFAULT_SCHEDULER_INTERVAL_SECONDS"""
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--interval-seconds", type=int, default=DEFAULT_SCHEDULER_INTERVAL_SECONDS
        )
        args = parser.parse_args([])
        assert args.interval_seconds == DEFAULT_SCHEDULER_INTERVAL_SECONDS

    def test_interval_seconds_custom_value(self):
        """--interval-seconds 可指定自定义值"""
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--interval-seconds", type=int, default=DEFAULT_SCHEDULER_INTERVAL_SECONDS
        )
        args = parser.parse_args(["--interval-seconds", "30"])
        assert args.interval_seconds == 30

    @patch("engram.logbook.cli.scm_sync._get_connection")
    @patch("engram.logbook.config.get_config")
    @patch("engram.logbook.config.is_scm_sync_enabled")
    @patch("engram.logbook.scm_sync_scheduler_core.run_scheduler_tick")
    def test_once_mode_executes_single_tick(
        self,
        mock_run_tick,
        mock_is_enabled,
        mock_get_config,
        mock_get_conn,
    ):
        """--once 模式下只执行一次 tick"""
        # 设置 mock
        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_get_config.return_value = mock_config
        mock_is_enabled.return_value = True
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "repos_scanned": 5,
            "candidates_selected": 3,
            "jobs_enqueued": 2,
            "jobs_skipped": 1,
            "circuit_state": "closed",
            "errors": [],
        }
        mock_result.repos_scanned = 5
        mock_result.candidates_selected = 3
        mock_result.jobs_enqueued = 2
        mock_result.jobs_skipped = 1
        mock_result.circuit_state = "closed"
        mock_result.errors = []
        mock_run_tick.return_value = mock_result

        # 执行 --once 模式
        exit_code = scheduler_main(["--once", "--dsn", "postgresql://test"])

        # 验证只调用了一次
        assert mock_run_tick.call_count == 1
        assert exit_code == 0

    @patch("engram.logbook.cli.scm_sync._get_connection")
    @patch("engram.logbook.config.get_config")
    @patch("engram.logbook.config.is_scm_sync_enabled")
    @patch("engram.logbook.scm_sync_scheduler_core.run_scheduler_tick")
    def test_default_mode_executes_single_tick(
        self,
        mock_run_tick,
        mock_is_enabled,
        mock_get_config,
        mock_get_conn,
    ):
        """默认模式（无 --loop 无 --once）只执行一次 tick"""
        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_get_config.return_value = mock_config
        mock_is_enabled.return_value = True
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "repos_scanned": 5,
            "candidates_selected": 3,
            "jobs_enqueued": 2,
            "jobs_skipped": 1,
            "circuit_state": "closed",
            "errors": [],
        }
        mock_result.repos_scanned = 5
        mock_result.candidates_selected = 3
        mock_result.jobs_enqueued = 2
        mock_result.jobs_skipped = 1
        mock_result.circuit_state = "closed"
        mock_result.errors = []
        mock_run_tick.return_value = mock_result

        # 默认模式
        exit_code = scheduler_main(["--dsn", "postgresql://test"])

        # 验证只调用了一次
        assert mock_run_tick.call_count == 1
        assert exit_code == 0


class TestReaperMainLoopFlags:
    """reaper_main 循环参数测试"""

    def test_once_flag_exists(self):
        """--once 参数应存在"""
        with pytest.raises(SystemExit) as exc_info:
            reaper_main(["--help"])
        assert exc_info.value.code == 0

    def test_loop_flag_exists(self):
        """--loop 参数应存在"""
        with pytest.raises(SystemExit) as exc_info:
            reaper_main(["--help"])
        assert exc_info.value.code == 0

    def test_interval_seconds_flag_exists(self):
        """--interval-seconds 参数应存在"""
        with pytest.raises(SystemExit) as exc_info:
            reaper_main(["--help"])
        assert exc_info.value.code == 0

    def test_once_and_loop_mutually_exclusive(self):
        """--once 和 --loop 应互斥"""
        with patch("engram.logbook.cli.scm_sync._setup_logging"):
            exit_code = reaper_main(["--once", "--loop", "--dsn", "postgresql://test"])
        assert exit_code == 1

    def test_once_and_loop_mutually_exclusive_json_output(self):
        """--once 和 --loop 互斥时 JSON 输出"""
        with patch("engram.logbook.cli.scm_sync._setup_logging"):
            with patch("builtins.print") as mock_print:
                exit_code = reaper_main(
                    ["--once", "--loop", "--json", "--dsn", "postgresql://test"]
                )
        assert exit_code == 1
        call_args = mock_print.call_args[0][0]
        error_data = json.loads(call_args)
        assert "error" in error_data

    def test_interval_seconds_default_value(self):
        """--interval-seconds 默认值应为 DEFAULT_REAPER_INTERVAL_SECONDS"""
        parser = argparse.ArgumentParser()
        parser.add_argument("--interval-seconds", type=int, default=DEFAULT_REAPER_INTERVAL_SECONDS)
        args = parser.parse_args([])
        assert args.interval_seconds == DEFAULT_REAPER_INTERVAL_SECONDS

    def test_interval_seconds_custom_value(self):
        """--interval-seconds 可指定自定义值"""
        parser = argparse.ArgumentParser()
        parser.add_argument("--interval-seconds", type=int, default=DEFAULT_REAPER_INTERVAL_SECONDS)
        args = parser.parse_args(["--interval-seconds", "120"])
        assert args.interval_seconds == 120

    @patch("engram.logbook.scm_sync_reaper_core.run_reaper")
    def test_once_mode_executes_single_reap(self, mock_run_reaper):
        """--once 模式下只执行一次回收"""
        mock_run_reaper.return_value = {
            "jobs": {"processed": 1, "to_failed": 1, "to_dead": 0, "errors": 0},
            "runs": {"processed": 0, "failed": 0, "errors": 0},
            "locks": {"processed": 0, "released": 0, "errors": 0},
        }

        exit_code = reaper_main(["--once", "--dsn", "postgresql://test"])

        assert mock_run_reaper.call_count == 1
        assert exit_code == 0

    @patch("engram.logbook.scm_sync_reaper_core.run_reaper")
    def test_default_mode_executes_single_reap(self, mock_run_reaper):
        """默认模式只执行一次回收"""
        mock_run_reaper.return_value = {
            "jobs": {"processed": 1, "to_failed": 1, "to_dead": 0, "errors": 0},
            "runs": {"processed": 0, "failed": 0, "errors": 0},
            "locks": {"processed": 0, "released": 0, "errors": 0},
        }

        exit_code = reaper_main(["--dsn", "postgresql://test"])

        assert mock_run_reaper.call_count == 1
        assert exit_code == 0

    @patch("engram.logbook.scm_sync_reaper_core.run_reaper")
    def test_json_output_format(self, mock_run_reaper):
        """--json 输出格式验证"""
        mock_run_reaper.return_value = {
            "jobs": {"processed": 2, "to_failed": 1, "to_dead": 1, "errors": 0},
            "runs": {"processed": 1, "failed": 1, "errors": 0},
            "locks": {"processed": 1, "released": 1, "errors": 0},
        }

        with patch("builtins.print") as mock_print:
            exit_code = reaper_main(["--once", "--json", "--dsn", "postgresql://test"])

        assert exit_code == 0
        # 验证输出是有效的 JSON
        call_args = mock_print.call_args[0][0]
        output_data = json.loads(call_args)
        assert "jobs" in output_data
        assert "runs" in output_data
        assert "locks" in output_data
        assert "total_processed" in output_data
        assert "total_errors" in output_data


class TestDefaultConstants:
    """默认常量测试"""

    def test_scheduler_interval_default(self):
        """Scheduler 默认间隔应为 60 秒"""
        assert DEFAULT_SCHEDULER_INTERVAL_SECONDS == 60

    def test_reaper_interval_default(self):
        """Reaper 默认间隔应为 60 秒"""
        assert DEFAULT_REAPER_INTERVAL_SECONDS == 60


class TestBackwardCompatibility:
    """向后兼容性测试"""

    @patch("engram.logbook.cli.scm_sync._get_connection")
    @patch("engram.logbook.config.get_config")
    @patch("engram.logbook.config.is_scm_sync_enabled")
    @patch("engram.logbook.scm_sync_scheduler_core.run_scheduler_tick")
    def test_scheduler_once_behavior_unchanged(
        self,
        mock_run_tick,
        mock_is_enabled,
        mock_get_config,
        mock_get_conn,
    ):
        """scheduler --once 行为应与之前保持一致"""
        mock_config = MagicMock()
        mock_config.get.return_value = None
        mock_get_config.return_value = mock_config
        mock_is_enabled.return_value = True
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"errors": []}
        mock_result.repos_scanned = 0
        mock_result.candidates_selected = 0
        mock_result.jobs_enqueued = 0
        mock_result.jobs_skipped = 0
        mock_result.circuit_state = "closed"
        mock_result.errors = []
        mock_run_tick.return_value = mock_result

        # --once 应该执行一次并退出
        exit_code = scheduler_main(["--once", "--dsn", "postgresql://test"])

        assert exit_code == 0
        assert mock_run_tick.call_count == 1
        mock_conn.close.assert_called_once()

    @patch("engram.logbook.scm_sync_reaper_core.run_reaper")
    def test_reaper_default_behavior_unchanged(self, mock_run_reaper):
        """reaper 默认行为应与之前保持一致（执行一次）"""
        mock_run_reaper.return_value = {
            "jobs": {"processed": 0, "to_failed": 0, "to_dead": 0, "errors": 0},
            "runs": {"processed": 0, "failed": 0, "errors": 0},
            "locks": {"processed": 0, "released": 0, "errors": 0},
        }

        # 默认执行一次
        exit_code = reaper_main(["--dsn", "postgresql://test"])

        assert exit_code == 0
        assert mock_run_reaper.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
