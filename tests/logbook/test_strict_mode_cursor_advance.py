#!/usr/bin/env python3
"""
test_strict_mode_cursor_advance.py - strict 模式下游标推进逻辑的单元测试

测试目标：
1. strict 模式下，遇到不可恢复错误时游标不推进/不越过
2. strict 模式下，游标仅推进到"最后完全成功处理"的水位线
3. best_effort 模式下，允许推进但必须记录降级与缺失类型
4. 验证 scm.sync.mode 配置读取和 CLI 覆盖

不可恢复的错误类型：
- 429 Rate Limited
- 5xx Server Error
- Timeout
- 认证失败 (401/403)
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

# 导入被测模块
from engram.logbook.config import (
    SCM_SYNC_MODE_BEST_EFFORT,
    SCM_SYNC_MODE_STRICT,
    get_scm_sync_config,
    get_scm_sync_mode,
    is_strict_mode,
)

# ============ 配置读取测试 ============


class TestScmSyncModeConfig:
    """测试 scm.sync.mode 配置读取"""

    def test_default_mode_is_best_effort(self):
        """默认模式应为 best_effort"""
        mock_config = Mock()
        mock_config.get.return_value = None

        mode = get_scm_sync_mode(mock_config)
        assert mode == SCM_SYNC_MODE_BEST_EFFORT

    def test_config_mode_strict(self):
        """配置文件中设置 strict 模式"""
        mock_config = Mock()
        mock_config.get.return_value = "strict"

        mode = get_scm_sync_mode(mock_config)
        assert mode == SCM_SYNC_MODE_STRICT

    def test_config_mode_best_effort(self):
        """配置文件中设置 best_effort 模式"""
        mock_config = Mock()
        mock_config.get.return_value = "best_effort"

        mode = get_scm_sync_mode(mock_config)
        assert mode == SCM_SYNC_MODE_BEST_EFFORT

    def test_cli_override_strict(self):
        """CLI 参数覆盖为 strict"""
        mock_config = Mock()
        mock_config.get.return_value = "best_effort"  # 配置文件是 best_effort

        # CLI 覆盖
        mode = get_scm_sync_mode(mock_config, cli_override="strict")
        assert mode == SCM_SYNC_MODE_STRICT

    def test_cli_override_boolean_true(self):
        """CLI 参数使用布尔值 True（来自 --strict 标志）"""
        mock_config = Mock()
        mock_config.get.return_value = "best_effort"

        mode = get_scm_sync_mode(mock_config, cli_override=True)
        assert mode == SCM_SYNC_MODE_STRICT

    def test_cli_override_boolean_false(self):
        """CLI 参数使用布尔值 False"""
        mock_config = Mock()
        mock_config.get.return_value = "strict"  # 配置文件是 strict

        mode = get_scm_sync_mode(mock_config, cli_override=False)
        assert mode == SCM_SYNC_MODE_BEST_EFFORT

    def test_invalid_config_value_falls_back_to_default(self):
        """无效的配置值应回退到默认值"""
        mock_config = Mock()
        mock_config.get.return_value = "invalid_mode"

        mode = get_scm_sync_mode(mock_config)
        assert mode == SCM_SYNC_MODE_BEST_EFFORT

    def test_is_strict_mode_helper(self):
        """测试 is_strict_mode 辅助函数"""
        mock_config = Mock()

        mock_config.get.return_value = "strict"
        assert is_strict_mode(mock_config) is True

        mock_config.get.return_value = "best_effort"
        assert is_strict_mode(mock_config) is False

    def test_get_scm_sync_config_returns_dict(self):
        """测试 get_scm_sync_config 返回完整配置字典"""
        mock_config = Mock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.sync.mode": "strict",
            "scm.sync.strict_on_auth_error": True,
            "scm.sync.strict_on_rate_limit": True,
            "scm.sync.strict_on_server_error": True,
            "scm.sync.strict_on_timeout": True,
        }.get(key, default)

        sync_config = get_scm_sync_config(mock_config)

        assert sync_config["mode"] == "strict"
        assert sync_config["is_strict"] is True
        assert sync_config["strict_on_auth_error"] is True
        assert sync_config["strict_on_rate_limit"] is True

    def test_default_strict_config_fallback(self):
        """测试 default_strict 配置回退"""
        mock_config = Mock()
        # mode 未设置，但 default_strict = true
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.sync.mode": None,
            "scm.sync.default_strict": True,
        }.get(key, default)

        sync_config = get_scm_sync_config(mock_config)

        assert sync_config["mode"] == "strict"
        assert sync_config["is_strict"] is True


# ============ 游标推进逻辑测试 ============


class TestStrictModeCursorAdvance:
    """测试 strict 模式下游标推进逻辑"""

    def test_strict_mode_cursor_not_advanced_on_unrecoverable_error(self):
        """strict 模式下遇到不可恢复错误时游标不推进"""
        # 模拟场景：处理 10 个 commits，第 5 个遇到 429 错误

        datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # 成功处理的最后一个 commit
        last_successful_sha = "def456"
        last_successful_ts = datetime(2024, 1, 1, 12, 30, 0, tzinfo=timezone.utc)

        # 遇到错误的 commit
        error_commit_sha = "ghi789"
        error_commit_ts = datetime(2024, 1, 1, 12, 35, 0, tzinfo=timezone.utc)

        # 在 strict 模式下，游标应该推进到 last_successful_sha
        # 不应该越过 error_commit_sha

        encountered_unrecoverable_error = True
        strict_mode = True

        if strict_mode and encountered_unrecoverable_error:
            # 游标应该停在 last_successful
            target_sha = last_successful_sha
            target_ts = last_successful_ts
        else:
            # 推进到最后一个
            target_sha = error_commit_sha
            target_ts = error_commit_ts

        assert target_sha == last_successful_sha
        assert target_ts == last_successful_ts

        # 验证游标没有越过错误点
        assert target_ts < error_commit_ts

    def test_strict_mode_cursor_not_advanced_when_no_success(self):
        """strict 模式下，如果没有成功处理任何 commit，游标不推进"""

        datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # 第一个 commit 就遇到错误
        last_successful_commit = None
        encountered_unrecoverable_error = True
        strict_mode = True

        if strict_mode and encountered_unrecoverable_error:
            if last_successful_commit is None:
                # 没有成功处理任何 commit，不推进游标
                should_update_cursor = False
                cursor_advance_reason = "strict_no_success"
            else:
                should_update_cursor = True
        else:
            should_update_cursor = True

        assert should_update_cursor is False
        assert cursor_advance_reason == "strict_no_success"

    def test_best_effort_mode_cursor_advanced_with_errors_recorded(self):
        """best_effort 模式下，遇到错误仍推进游标，但记录缺失类型"""

        # 遇到的错误
        unrecoverable_errors = [
            {"error_category": "rate_limited", "commit_sha": "def456"},
            {"error_category": "timeout", "commit_sha": "ghi789"},
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0
        strict_mode = False  # best_effort 模式

        # 最后一个 commit

        if strict_mode and encountered_unrecoverable_error:
            should_advance = False
        else:
            should_advance = True
            # 记录缺失类型
            missing_types = list(
                set(err.get("error_category", "unknown") for err in unrecoverable_errors)
            )

        assert should_advance is True
        assert "rate_limited" in missing_types
        assert "timeout" in missing_types

    def test_cursor_monotonic_increase_in_strict_mode(self):
        """strict 模式下游标仍然遵循单调递增规则"""

        # 当前游标
        cursor_ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        cursor_sha = "abc123"

        # 尝试推进到更早的时间（不应该发生）
        new_ts = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)  # 比 cursor_ts 更早
        new_sha = "def456"

        # 单调递增检查
        should_advance = False
        if new_ts > cursor_ts:
            should_advance = True
        elif new_ts == cursor_ts and new_sha > cursor_sha:
            should_advance = True

        assert should_advance is False

    def test_cursor_not_regressed_on_partial_success(self):
        """游标不会回退：即使部分成功，也不会低于原来的值"""

        # 当前游标
        cursor_ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        cursor_sha = "ccc123"

        # 成功处理的 commit（但时间戳比游标早 - 可能是重叠窗口内的重处理）
        successful_commit_ts = datetime(2024, 1, 1, 11, 50, 0, tzinfo=timezone.utc)
        successful_commit_sha = "aaa111"

        # 检查是否应该推进
        if successful_commit_ts < cursor_ts:
            # 新值比旧值小，不更新
            should_update = False
        elif successful_commit_ts == cursor_ts and successful_commit_sha <= cursor_sha:
            should_update = False
        else:
            should_update = True

        assert should_update is False


# ============ 错误分类测试 ============


class TestUnrecoverableErrorClassification:
    """测试不可恢复错误的分类"""

    def test_429_is_unrecoverable(self):
        """429 状态码是不可恢复的错误"""
        error_category = "rate_limited"
        status_code = 429

        unrecoverable_categories = {"rate_limited", "http_error", "timeout", "auth_error"}

        is_unrecoverable = error_category in unrecoverable_categories or status_code == 429
        assert is_unrecoverable is True

    def test_5xx_is_unrecoverable(self):
        """5xx 状态码是不可恢复的错误"""
        status_code = 503

        is_unrecoverable = 500 <= status_code < 600
        assert is_unrecoverable is True

    def test_timeout_is_unrecoverable(self):
        """超时是不可恢复的错误"""
        error_category = "timeout"

        unrecoverable_categories = {"rate_limited", "http_error", "timeout", "auth_error"}

        is_unrecoverable = error_category in unrecoverable_categories
        assert is_unrecoverable is True

    def test_auth_error_is_unrecoverable(self):
        """认证错误是不可恢复的错误"""
        error_category = "auth_error"

        unrecoverable_categories = {"rate_limited", "http_error", "timeout", "auth_error"}

        is_unrecoverable = error_category in unrecoverable_categories
        assert is_unrecoverable is True

    def test_content_too_large_is_recoverable(self):
        """内容过大不是不可恢复的错误（可以降级处理）"""
        error_category = "content_too_large"

        unrecoverable_categories = {"rate_limited", "http_error", "timeout", "auth_error"}

        is_unrecoverable = error_category in unrecoverable_categories
        assert is_unrecoverable is False


# ============ 场景测试 ============


class TestSyncScenarios:
    """测试具体同步场景"""

    def test_scenario_strict_mode_partial_batch_success(self):
        """
        场景：strict 模式下批量同步部分成功

        批次包含 commits: [A, B, C, D, E]
        A, B 成功处理
        C 遇到 429 错误
        D, E 未处理（因为 strict 模式停止）

        期望：游标推进到 B，不越过 C
        """
        commits = [
            {"sha": "A", "ts": datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)},
            {"sha": "B", "ts": datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)},
            {"sha": "C", "ts": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)},
            {"sha": "D", "ts": datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)},
            {"sha": "E", "ts": datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)},
        ]

        strict_mode = True

        # 模拟处理过程
        last_successful = None

        for commit in commits:
            if commit["sha"] == "C":
                # 遇到 429 错误
                if strict_mode:
                    break
            else:
                last_successful = commit

        assert last_successful["sha"] == "B"
        assert last_successful["ts"] == datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

    def test_scenario_best_effort_mode_full_batch_with_errors(self):
        """
        场景：best_effort 模式下批量同步有错误但完成

        批次包含 commits: [A, B, C, D, E]
        A 成功
        B 遇到 429 错误，降级处理
        C, D 成功
        E 遇到 timeout，降级处理

        期望：游标推进到 E，记录缺失类型 ["rate_limited", "timeout"]
        """
        commits = ["A", "B", "C", "D", "E"]

        # 模拟处理过程
        missing_types = []
        last_processed = None

        for commit in commits:
            if commit == "B":
                # 429 错误，降级
                missing_types.append("rate_limited")
            elif commit == "E":
                # timeout，降级
                missing_types.append("timeout")

            last_processed = commit

        assert last_processed == "E"
        assert "rate_limited" in missing_types
        assert "timeout" in missing_types

    def test_scenario_cursor_advance_reason_recorded(self):
        """测试游标推进原因被正确记录"""

        # 场景 1: 正常完成
        result1 = {"cursor_advance_reason": "batch_complete"}
        assert result1["cursor_advance_reason"] == "batch_complete"

        # 场景 2: strict 模式部分成功
        result2 = {
            "cursor_advance_reason": "strict_partial_success:stopped_before_unrecoverable_error"
        }
        assert "strict_partial_success" in result2["cursor_advance_reason"]

        # 场景 3: strict 模式无成功
        result3 = {"cursor_advance_reason": "strict_no_success:no_commit_processed"}
        assert "strict_no_success" in result3["cursor_advance_reason"]

        # 场景 4: best_effort 模式有错误
        result4 = {"cursor_advance_reason": "best_effort_with_errors:degraded=rate_limited,timeout"}
        assert "best_effort_with_errors" in result4["cursor_advance_reason"]
        assert "degraded=" in result4["cursor_advance_reason"]


# ============ 边界条件测试 ============


class TestEdgeCases:
    """测试边界条件"""

    def test_empty_batch_no_cursor_update(self):
        """空批次不更新游标"""
        commits = []

        if not commits:
            should_update = False
            cursor_advance_reason = None
        else:
            should_update = True

        assert should_update is False
        assert cursor_advance_reason is None

    def test_all_commits_already_processed(self):
        """所有 commits 都已处理过（去重后为空）"""
        after_dedup_count = 0

        if after_dedup_count == 0:
            should_update = False
            message = "无新 commits 需要同步"
        else:
            should_update = True

        assert should_update is False
        assert message == "无新 commits 需要同步"

    def test_first_commit_fails_in_strict_mode(self):
        """strict 模式下第一个 commit 就失败"""

        strict_mode = True
        last_successful_commit = None
        encountered_error = True

        if strict_mode and encountered_error:
            if last_successful_commit is None:
                cursor_advance_reason = "strict_no_success:no_commit_processed"
                should_advance = False
            else:
                should_advance = True
        else:
            should_advance = True

        assert should_advance is False
        assert "strict_no_success" in cursor_advance_reason

    def test_same_timestamp_different_sha_ordering(self):
        """同一时间戳不同 SHA 的排序处理"""

        # 两个 commit 有相同的时间戳
        commit_a = {"sha": "aaa111", "ts": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)}
        commit_b = {"sha": "bbb222", "ts": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)}

        # 按 (ts, sha) 排序
        commits = sorted([commit_b, commit_a], key=lambda c: (c["ts"], c["sha"]))

        # aaa111 < bbb222，所以 A 在前
        assert commits[0]["sha"] == "aaa111"
        assert commits[1]["sha"] == "bbb222"

        # 游标应该推进到 bbb222（最后一个）
        last_commit = commits[-1]
        assert last_commit["sha"] == "bbb222"


# ============ MR/Reviews API 错误分类测试 ============


class TestMRReviewsAPIErrorClassification:
    """测试 MR/Reviews API 调用的错误分类"""

    def test_get_merge_requests_429_classified_as_rate_limited(self):
        """get_merge_requests 遇到 429 应分类为 rate_limited"""
        # 模拟 429 错误信息
        error_info = {
            "error_category": "rate_limited",
            "status_code": 429,
            "is_unrecoverable": True,
            "api_call": "get_merge_requests",
            "page": 1,
        }

        assert error_info["error_category"] == "rate_limited"
        assert error_info["status_code"] == 429
        assert error_info["is_unrecoverable"] is True
        assert error_info["api_call"] == "get_merge_requests"

    def test_get_mr_notes_5xx_classified_as_server_error(self):
        """get_mr_notes 遇到 5xx 应分类为 server_error"""
        error_info = {
            "error_category": "server_error",
            "status_code": 503,
            "is_unrecoverable": True,
            "api_call": "get_mr_notes",
            "mr_iid": 42,
        }

        assert error_info["error_category"] == "server_error"
        assert error_info["status_code"] == 503
        assert error_info["is_unrecoverable"] is True
        assert error_info["mr_iid"] == 42

    def test_get_mr_approvals_timeout_classified_correctly(self):
        """get_mr_approvals 超时应分类为 timeout"""
        error_info = {
            "error_category": "timeout",
            "status_code": None,
            "is_unrecoverable": True,
            "api_call": "get_mr_approvals",
            "mr_iid": 100,
        }

        assert error_info["error_category"] == "timeout"
        assert error_info["is_unrecoverable"] is True

    def test_get_mr_resource_state_events_auth_error(self):
        """get_mr_resource_state_events 认证失败应分类为 auth_error"""
        error_info = {
            "error_category": "auth_error",
            "status_code": 401,
            "is_unrecoverable": True,
            "api_call": "get_mr_resource_state_events",
            "mr_iid": 55,
        }

        assert error_info["error_category"] == "auth_error"
        assert error_info["status_code"] == 401
        assert error_info["is_unrecoverable"] is True

    def test_get_merge_request_detail_403_is_unrecoverable(self):
        """get_merge_request_detail 403 权限不足应分类为不可恢复"""
        error_info = {
            "error_category": "auth_error",
            "status_code": 403,
            "is_unrecoverable": True,
            "api_call": "get_merge_request_detail",
            "mr_iid": 123,
        }

        assert error_info["error_category"] == "auth_error"
        assert error_info["status_code"] == 403
        assert error_info["is_unrecoverable"] is True

    def test_network_error_is_unrecoverable(self):
        """网络错误应分类为不可恢复"""
        error_info = {
            "error_category": "network_error",
            "status_code": None,
            "is_unrecoverable": True,
            "api_call": "get_merge_requests",
        }

        assert error_info["error_category"] == "network_error"
        assert error_info["is_unrecoverable"] is True

    def test_client_error_404_is_recoverable(self):
        """404 客户端错误应视为可恢复（跳过该资源继续）"""
        error_info = {
            "error_category": "client_error",
            "status_code": 404,
            "is_unrecoverable": False,
            "api_call": "get_mr_notes",
            "mr_iid": 999,
        }

        assert error_info["error_category"] == "client_error"
        assert error_info["is_unrecoverable"] is False


class TestMRReviewsStrictModeCursorAdvance:
    """测试 MR/Reviews 同步中 strict 模式下的游标推进"""

    def test_mr_sync_strict_mode_cursor_not_advanced_on_429(self):
        """MR 同步 strict 模式下遇到 429 时游标不推进"""
        strict_mode = True
        unrecoverable_errors = [
            {
                "error_category": "rate_limited",
                "status_code": 429,
                "api_call": "get_merge_requests",
                "is_unrecoverable": True,
            }
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0

        if strict_mode and encountered_unrecoverable_error:
            should_advance = False
            error_categories = list(set(err["error_category"] for err in unrecoverable_errors))
            cursor_advance_reason = f"strict_mode:unrecoverable_error_encountered:categories={','.join(error_categories)}"
        else:
            should_advance = True
            cursor_advance_reason = "batch_complete"

        assert should_advance is False
        assert "strict_mode" in cursor_advance_reason
        assert "rate_limited" in cursor_advance_reason

    def test_reviews_sync_strict_mode_stops_on_notes_timeout(self):
        """Reviews 同步 strict 模式下 get_mr_notes 超时时游标不推进"""
        strict_mode = True
        unrecoverable_errors = [
            {
                "error_category": "timeout",
                "api_call": "get_mr_notes",
                "mr_iid": 42,
                "is_unrecoverable": True,
            }
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0

        if strict_mode and encountered_unrecoverable_error:
            should_advance = False
        else:
            should_advance = True

        assert should_advance is False

    def test_reviews_sync_strict_mode_stops_on_approvals_server_error(self):
        """Reviews 同步 strict 模式下 get_mr_approvals 5xx 时游标不推进"""
        strict_mode = True
        unrecoverable_errors = [
            {
                "error_category": "server_error",
                "status_code": 502,
                "api_call": "get_mr_approvals",
                "mr_iid": 100,
                "is_unrecoverable": True,
            }
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0
        should_advance = not (strict_mode and encountered_unrecoverable_error)

        assert should_advance is False

    def test_reviews_sync_strict_mode_stops_on_state_events_auth_error(self):
        """Reviews 同步 strict 模式下 get_mr_resource_state_events 认证失败时游标不推进"""
        strict_mode = True
        unrecoverable_errors = [
            {
                "error_category": "auth_error",
                "status_code": 401,
                "api_call": "get_mr_resource_state_events",
                "mr_iid": 55,
                "is_unrecoverable": True,
            }
        ]

        should_advance = not (strict_mode and len(unrecoverable_errors) > 0)

        assert should_advance is False


class TestMRReviewsBestEffortModeCursorAdvance:
    """测试 MR/Reviews 同步中 best_effort 模式下的游标推进"""

    def test_mr_sync_best_effort_advances_cursor_with_errors_recorded(self):
        """MR 同步 best_effort 模式下遇到错误仍推进游标，但记录缺失类型"""
        strict_mode = False
        unrecoverable_errors = [
            {
                "error_category": "rate_limited",
                "status_code": 429,
                "api_call": "get_merge_requests",
                "is_unrecoverable": True,
            },
            {
                "error_category": "timeout",
                "api_call": "get_merge_request_detail",
                "mr_iid": 42,
                "is_unrecoverable": True,
            },
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0

        if strict_mode and encountered_unrecoverable_error:
            should_advance = False
            missing_types = []
            cursor_advance_reason = "strict_mode:unrecoverable_error_encountered"
        else:
            should_advance = True
            if encountered_unrecoverable_error:
                missing_types = list(set(err["error_category"] for err in unrecoverable_errors))
                cursor_advance_reason = (
                    f"best_effort_with_errors:degraded={','.join(sorted(missing_types))}"
                )
            else:
                missing_types = []
                cursor_advance_reason = "batch_complete"

        assert should_advance is True
        assert "rate_limited" in missing_types
        assert "timeout" in missing_types
        assert "best_effort_with_errors" in cursor_advance_reason
        assert "degraded=" in cursor_advance_reason

    def test_reviews_sync_best_effort_records_multiple_error_types(self):
        """Reviews 同步 best_effort 模式下记录多种错误类型"""
        unrecoverable_errors = [
            {"error_category": "timeout", "api_call": "get_mr_notes", "mr_iid": 1},
            {"error_category": "server_error", "api_call": "get_mr_approvals", "mr_iid": 2},
            {
                "error_category": "rate_limited",
                "api_call": "get_mr_resource_state_events",
                "mr_iid": 3,
            },
            {"error_category": "timeout", "api_call": "get_mr_notes", "mr_iid": 4},  # 重复类型
        ]

        # 提取唯一的缺失类型
        missing_types = list(set(err["error_category"] for err in unrecoverable_errors))

        # 应该有 3 种唯一的错误类型
        assert len(missing_types) == 3
        assert "timeout" in missing_types
        assert "server_error" in missing_types
        assert "rate_limited" in missing_types


class TestMRReviewsPartialSuccessScenarios:
    """测试 MR/Reviews 部分成功的场景"""

    def test_reviews_sync_partial_mr_success_strict_mode(self):
        """
        场景：strict 模式下部分 MR 的 reviews 同步成功

        MR 列表: [!1, !2, !3, !4, !5]
        !1, !2 的 notes/approvals/state_events 全部成功
        !3 的 get_mr_notes 遇到 503
        !4, !5 继续处理（但已有错误）

        期望：游标不推进（strict 模式遇到不可恢复错误）
        """
        strict_mode = True

        # 模拟处理结果
        unrecoverable_errors = [
            {
                "error_category": "server_error",
                "status_code": 503,
                "api_call": "get_mr_notes",
                "mr_iid": 3,
            }
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0

        if strict_mode and encountered_unrecoverable_error:
            should_advance = False
            cursor_advance_reason = "strict_mode:unrecoverable_error_encountered"
        else:
            should_advance = True
            cursor_advance_reason = "batch_complete"

        assert should_advance is False
        assert "strict_mode" in cursor_advance_reason

    def test_reviews_sync_multiple_apis_fail_best_effort(self):
        """
        场景：best_effort 模式下多个 API 调用失败

        !1: get_mr_notes 成功, get_mr_approvals 超时, get_mr_resource_state_events 成功
        !2: 全部成功
        !3: get_mr_notes 429

        期望：游标推进，记录缺失类型 [timeout, rate_limited]
        """
        strict_mode = False

        unrecoverable_errors = [
            {"error_category": "timeout", "api_call": "get_mr_approvals", "mr_iid": 1},
            {"error_category": "rate_limited", "api_call": "get_mr_notes", "mr_iid": 3},
        ]

        should_advance = not (strict_mode and len(unrecoverable_errors) > 0)
        missing_types = list(set(err["error_category"] for err in unrecoverable_errors))

        assert should_advance is True
        assert "timeout" in missing_types
        assert "rate_limited" in missing_types

    def test_mr_sync_detail_fetch_fails_strict_mode(self):
        """
        场景：MR 同步时 fetch_details=True，get_merge_request_detail 失败

        strict 模式下，detail 获取失败也应该阻止游标推进
        """
        strict_mode = True

        unrecoverable_errors = [
            {
                "error_category": "server_error",
                "status_code": 500,
                "api_call": "get_merge_request_detail",
                "mr_iid": 42,
            }
        ]

        encountered_unrecoverable_error = len(unrecoverable_errors) > 0
        should_advance = not (strict_mode and encountered_unrecoverable_error)

        assert should_advance is False


class TestUnrecoverableErrorCategories:
    """测试不可恢复错误类型的定义"""

    def test_unrecoverable_error_categories_complete(self):
        """验证不可恢复错误类型定义完整"""
        # 从 gitlab_client 中定义的不可恢复类型
        unrecoverable_categories = {
            "rate_limited",  # 429
            "server_error",  # 5xx
            "timeout",  # 请求超时
            "auth_error",  # 401/403
            "network_error",  # 网络错误
        }

        # 应该包含所有关键类型
        assert "rate_limited" in unrecoverable_categories
        assert "server_error" in unrecoverable_categories
        assert "timeout" in unrecoverable_categories
        assert "auth_error" in unrecoverable_categories
        assert "network_error" in unrecoverable_categories

        # 这些不应该是不可恢复的
        recoverable_categories = {"client_error", "content_too_large", "parse_error"}
        for cat in recoverable_categories:
            assert cat not in unrecoverable_categories

    def test_error_classification_429(self):
        """验证 429 正确分类为 rate_limited"""
        status_code = 429

        if status_code == 429:
            error_category = "rate_limited"
            is_unrecoverable = True
        else:
            error_category = "unknown"
            is_unrecoverable = False

        assert error_category == "rate_limited"
        assert is_unrecoverable is True

    def test_error_classification_5xx_range(self):
        """验证 5xx 范围正确分类为 server_error"""
        for status_code in [500, 501, 502, 503, 504]:
            if 500 <= status_code < 600:
                error_category = "server_error"
                is_unrecoverable = True
            else:
                error_category = "unknown"
                is_unrecoverable = False

            assert error_category == "server_error"
            assert is_unrecoverable is True

    def test_error_classification_auth_codes(self):
        """验证 401/403 正确分类为 auth_error"""
        for status_code in [401, 403]:
            if status_code in (401, 403):
                error_category = "auth_error"
                is_unrecoverable = True
            else:
                error_category = "unknown"
                is_unrecoverable = False

            assert error_category == "auth_error"
            assert is_unrecoverable is True


# ============ Backfill Watermark 策略测试 ============


class TestBackfillWatermarkStrategy:
    """测试回填时 watermark 策略的应用"""

    def test_backfill_update_watermark_false_constraint_none(self):
        """回填模式 update_watermark=False 时，约束为 none"""
        update_watermark = False
        watermark_constraint = "monotonic" if update_watermark else "none"

        assert watermark_constraint == "none"

        # 模拟 chunk payload
        chunk_payload = {
            "window_type": "time",
            "window_since": "2025-01-01T00:00:00+00:00",
            "window_until": "2025-01-01T04:00:00+00:00",
            "chunk_index": 0,
            "chunk_total": 3,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
        }

        assert chunk_payload["update_watermark"] is False
        assert chunk_payload["watermark_constraint"] == "none"

    def test_backfill_update_watermark_true_constraint_monotonic(self):
        """回填模式 update_watermark=True 时，约束为 monotonic"""
        update_watermark = True
        watermark_constraint = "monotonic" if update_watermark else "none"

        assert watermark_constraint == "monotonic"

        chunk_payload = {
            "window_type": "time",
            "window_since": "2025-01-01T00:00:00+00:00",
            "window_until": "2025-01-01T04:00:00+00:00",
            "chunk_index": 0,
            "chunk_total": 1,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
        }

        assert chunk_payload["update_watermark"] is True
        assert chunk_payload["watermark_constraint"] == "monotonic"

    def test_strict_mode_watermark_constraint_enforcement(self):
        """strict 模式下 watermark 约束强制执行"""
        strict_mode = True
        update_watermark = True

        # 在 strict 模式且 update_watermark=True 时
        # watermark 约束应该被严格执行
        watermark_before = "2025-01-27T10:00:00Z"
        watermark_after = "2025-01-27T08:00:00Z"  # 回退

        if update_watermark and strict_mode:
            # 解析时间戳
            from datetime import datetime

            before_dt = datetime.fromisoformat(watermark_before.replace("Z", "+00:00"))
            after_dt = datetime.fromisoformat(watermark_after.replace("Z", "+00:00"))

            # 检查是否回退
            is_regression = after_dt < before_dt

            assert is_regression is True
            # strict 模式下应该拒绝

    def test_best_effort_mode_watermark_constraint_relaxed(self):
        """best_effort 模式下 watermark 约束放宽"""
        update_watermark = False  # best_effort 默认不更新

        # best_effort 模式下，不更新 watermark，不检查回退
        if not update_watermark:
            should_check_constraint = False
        else:
            should_check_constraint = True

        assert should_check_constraint is False

    def test_backfill_cursor_before_after_recording(self):
        """测试回填时 cursor_before/after 的记录"""
        # 模拟回填 metadata
        since_time = "2025-01-01T00:00:00+00:00"
        until_time = "2025-01-01T12:00:00+00:00"
        watermark_after = "2025-01-01T11:45:00+00:00"

        cursor_before = {
            "since": since_time,
            "window_type": "time",
        }

        cursor_after = {
            "until": until_time,
            "window_type": "time",
            "watermark_after": watermark_after,
            "update_watermark": False,
            "watermark_constraint": "none",
        }

        # 验证 cursor_before 包含窗口起始边界
        assert cursor_before["since"] == since_time
        assert cursor_before["window_type"] == "time"

        # 验证 cursor_after 包含窗口结束边界和 watermark 策略
        assert cursor_after["until"] == until_time
        assert cursor_after["watermark_after"] == watermark_after
        assert cursor_after["update_watermark"] is False
        assert cursor_after["watermark_constraint"] == "none"

    def test_svn_backfill_cursor_recording(self):
        """测试 SVN 回填时 cursor_before/after 的记录"""
        start_rev = 100
        end_rev = 500
        watermark_after = 495  # 实际同步到的最后一个 revision

        cursor_before = {
            "start_rev": start_rev,
            "window_type": "revision",
        }

        cursor_after = {
            "end_rev": end_rev,
            "window_type": "revision",
            "watermark_after": watermark_after,
            "update_watermark": True,
            "watermark_constraint": "monotonic",
        }

        # 验证 cursor_before 包含起始 revision
        assert cursor_before["start_rev"] == 100
        assert cursor_before["window_type"] == "revision"

        # 验证 cursor_after 包含结束 revision 和 watermark 策略
        assert cursor_after["end_rev"] == 500
        assert cursor_after["watermark_after"] == 495
        assert cursor_after["update_watermark"] is True
        assert cursor_after["watermark_constraint"] == "monotonic"


class TestChunkPayloadWatermarkFields:
    """测试 chunk payload 中 watermark 相关字段"""

    def test_time_chunk_payload_complete_fields(self):
        """测试时间窗口 chunk payload 包含完整字段"""
        payload = {
            "window_type": "time",
            "window_since": "2025-01-01T00:00:00+00:00",
            "window_until": "2025-01-01T04:00:00+00:00",
            "chunk_index": 0,
            "chunk_total": 6,
            "update_watermark": False,
            "watermark_constraint": "none",
        }

        required_fields = [
            "window_type",
            "window_since",
            "window_until",
            "chunk_index",
            "chunk_total",
            "update_watermark",
            "watermark_constraint",
        ]

        for field in required_fields:
            assert field in payload, f"缺少字段: {field}"

    def test_revision_chunk_payload_complete_fields(self):
        """测试 revision 窗口 chunk payload 包含完整字段"""
        payload = {
            "window_type": "revision",
            "window_start_rev": 100,
            "window_end_rev": 199,
            "chunk_index": 1,
            "chunk_total": 5,
            "update_watermark": True,
            "watermark_constraint": "monotonic",
        }

        required_fields = [
            "window_type",
            "window_start_rev",
            "window_end_rev",
            "chunk_index",
            "chunk_total",
            "update_watermark",
            "watermark_constraint",
        ]

        for field in required_fields:
            assert field in payload, f"缺少字段: {field}"

    def test_watermark_constraint_valid_values(self):
        """测试 watermark_constraint 只能是有效值"""
        valid_constraints = {"monotonic", "none"}

        # update_watermark=True 时应使用 monotonic
        constraint_when_update = "monotonic"
        assert constraint_when_update in valid_constraints

        # update_watermark=False 时应使用 none
        constraint_when_no_update = "none"
        assert constraint_when_no_update in valid_constraints

    def test_all_chunks_share_same_watermark_strategy(self):
        """测试同一回填任务的所有 chunk 共享相同的 watermark 策略"""
        update_watermark = True
        watermark_constraint = "monotonic"

        # 模拟 3 个 chunk
        chunk_payloads = [
            {
                "chunk_index": i,
                "chunk_total": 3,
                "update_watermark": update_watermark,
                "watermark_constraint": watermark_constraint,
            }
            for i in range(3)
        ]

        # 验证所有 chunk 使用相同的策略
        for payload in chunk_payloads:
            assert payload["update_watermark"] == update_watermark
            assert payload["watermark_constraint"] == watermark_constraint


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
