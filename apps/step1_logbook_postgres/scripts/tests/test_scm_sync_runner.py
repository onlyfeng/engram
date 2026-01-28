#!/usr/bin/env python3
"""
test_scm_sync_runner.py - SCM 同步运行器单元测试

测试内容:
- 参数解析
- 仓库规格解析
- 任务规格解析
- 回填配置
- Watermark 约束验证
- 时间窗口计算
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import sys
import os

# 确保 scripts 目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scm_sync_runner import (
    # 解析器
    parse_args,
    create_parser,
    # 规格类
    RepoSpec,
    JobSpec,
    # 配置类
    BackfillConfig,
    IncrementalConfig,
    RunnerContext,
    SyncResult,
    # 窗口切分相关
    TimeWindowChunk,
    RevisionWindowChunk,
    split_time_window,
    split_revision_window,
    # 辅助函数
    calculate_backfill_window,
    validate_watermark_constraint,
    get_script_path,
    build_sync_command,
    # 常量
    REPO_TYPE_GITLAB,
    REPO_TYPE_SVN,
    VALID_REPO_TYPES,
    JOB_TYPE_COMMITS,
    JOB_TYPE_MRS,
    JOB_TYPE_REVIEWS,
    VALID_JOB_TYPES,
    DEFAULT_REPAIR_WINDOW_HOURS,
    DEFAULT_LOOP_INTERVAL_SECONDS,
    DEFAULT_WINDOW_CHUNK_HOURS,
    DEFAULT_WINDOW_CHUNK_REVS,
    # 枚举
    RunnerPhase,
    RunnerStatus,
    # 异常
    WatermarkConstraintError,
)


class TestRepoSpec:
    """仓库规格解析测试"""

    def test_parse_gitlab_numeric_id(self):
        """测试解析 GitLab 数字 ID"""
        spec = RepoSpec.parse("gitlab:123")
        assert spec.repo_type == REPO_TYPE_GITLAB
        assert spec.repo_id == "123"

    def test_parse_gitlab_namespace_project(self):
        """测试解析 GitLab namespace/project 格式"""
        spec = RepoSpec.parse("gitlab:namespace/project")
        assert spec.repo_type == REPO_TYPE_GITLAB
        assert spec.repo_id == "namespace/project"

    def test_parse_svn_url(self):
        """测试解析 SVN URL"""
        spec = RepoSpec.parse("svn:https://svn.example.com/repo/trunk")
        assert spec.repo_type == REPO_TYPE_SVN
        assert spec.repo_id == "https://svn.example.com/repo/trunk"

    def test_parse_case_insensitive(self):
        """测试大小写不敏感"""
        spec = RepoSpec.parse("GITLAB:123")
        assert spec.repo_type == REPO_TYPE_GITLAB

        spec = RepoSpec.parse("GitLab:456")
        assert spec.repo_type == REPO_TYPE_GITLAB

    def test_parse_invalid_format_no_colon(self):
        """测试无效格式：缺少冒号"""
        with pytest.raises(ValueError) as exc_info:
            RepoSpec.parse("gitlab123")
        assert "格式应为 <type>:<id>" in str(exc_info.value)

    def test_parse_invalid_repo_type(self):
        """测试无效仓库类型"""
        with pytest.raises(ValueError) as exc_info:
            RepoSpec.parse("github:123")
        assert "不支持的仓库类型" in str(exc_info.value)

    def test_parse_empty_repo_id(self):
        """测试空仓库 ID"""
        with pytest.raises(ValueError) as exc_info:
            RepoSpec.parse("gitlab:")
        assert "仓库 ID 不能为空" in str(exc_info.value)

    def test_str_representation(self):
        """测试字符串表示"""
        spec = RepoSpec(repo_type="gitlab", repo_id="123")
        assert str(spec) == "gitlab:123"


class TestJobSpec:
    """任务规格解析测试"""

    def test_parse_commits(self):
        """测试解析 commits 任务"""
        spec = JobSpec.parse("commits")
        assert spec.job_type == JOB_TYPE_COMMITS

    def test_parse_mrs(self):
        """测试解析 mrs 任务"""
        spec = JobSpec.parse("mrs")
        assert spec.job_type == JOB_TYPE_MRS

    def test_parse_reviews(self):
        """测试解析 reviews 任务"""
        spec = JobSpec.parse("reviews")
        assert spec.job_type == JOB_TYPE_REVIEWS

    def test_parse_case_insensitive(self):
        """测试大小写不敏感"""
        spec = JobSpec.parse("COMMITS")
        assert spec.job_type == JOB_TYPE_COMMITS

    def test_parse_invalid_job_type(self):
        """测试无效任务类型"""
        with pytest.raises(ValueError) as exc_info:
            JobSpec.parse("branches")
        assert "不支持的任务类型" in str(exc_info.value)

    def test_str_representation(self):
        """测试字符串表示"""
        spec = JobSpec(job_type="commits")
        assert str(spec) == "commits"


class TestParseArgs:
    """命令行参数解析测试"""

    def test_incremental_basic(self):
        """测试基本增量同步参数"""
        args = parse_args(["incremental", "--repo", "gitlab:123"])
        assert args.command == "incremental"
        assert args.repo == "gitlab:123"
        assert args.job == JOB_TYPE_COMMITS
        assert args.loop is False

    def test_incremental_with_loop(self):
        """测试循环模式参数"""
        args = parse_args(["incremental", "--repo", "gitlab:123", "--loop"])
        assert args.loop is True

    def test_incremental_with_loop_interval(self):
        """测试循环间隔参数"""
        args = parse_args([
            "incremental", "--repo", "gitlab:123",
            "--loop", "--loop-interval", "120"
        ])
        assert args.loop_interval == 120

    def test_incremental_with_max_iterations(self):
        """测试最大迭代次数参数"""
        args = parse_args([
            "incremental", "--repo", "gitlab:123",
            "--loop", "--max-iterations", "10"
        ])
        assert args.max_iterations == 10

    def test_backfill_basic(self):
        """测试基本回填参数"""
        args = parse_args(["backfill", "--repo", "gitlab:123"])
        assert args.command == "backfill"
        assert args.repo == "gitlab:123"
        assert args.update_watermark is False

    def test_backfill_last_hours(self):
        """测试回填小时数参数"""
        args = parse_args([
            "backfill", "--repo", "gitlab:123",
            "--last-hours", "48"
        ])
        assert args.last_hours == 48
        assert args.last_days is None

    def test_backfill_last_days(self):
        """测试回填天数参数"""
        args = parse_args([
            "backfill", "--repo", "gitlab:123",
            "--last-days", "7"
        ])
        assert args.last_days == 7
        assert args.last_hours is None

    def test_backfill_update_watermark(self):
        """测试更新 watermark 参数"""
        args = parse_args([
            "backfill", "--repo", "gitlab:123",
            "--update-watermark"
        ])
        assert args.update_watermark is True

    def test_backfill_mutually_exclusive_time(self):
        """测试时间参数互斥"""
        with pytest.raises(SystemExit):
            parse_args([
                "backfill", "--repo", "gitlab:123",
                "--last-hours", "24", "--last-days", "7"
            ])

    def test_global_verbose(self):
        """测试全局 verbose 参数"""
        args = parse_args(["-v", "incremental", "--repo", "gitlab:123"])
        assert args.verbose is True

    def test_global_dry_run(self):
        """测试全局 dry-run 参数"""
        args = parse_args(["--dry-run", "incremental", "--repo", "gitlab:123"])
        assert args.dry_run is True

    def test_global_json_output(self):
        """测试全局 JSON 输出参数"""
        args = parse_args(["--json", "incremental", "--repo", "gitlab:123"])
        assert args.json_output is True

    def test_config_command(self):
        """测试 config 子命令"""
        args = parse_args(["config", "--show-backfill"])
        assert args.command == "config"
        assert args.show_backfill is True

    def test_job_type_parameter(self):
        """测试任务类型参数"""
        args = parse_args([
            "incremental", "--repo", "gitlab:123",
            "--job", "mrs"
        ])
        assert args.job == "mrs"


class TestWatermarkConstraint:
    """Watermark 约束验证测试"""

    def test_no_update_watermark_skips_validation(self):
        """测试不更新 watermark 时跳过验证"""
        # 即使 watermark 回退，也不应该报错
        validate_watermark_constraint(
            watermark_before="2025-01-27T12:00:00Z",
            watermark_after="2025-01-27T10:00:00Z",
            update_watermark=False,
        )

    def test_watermark_forward_allowed(self):
        """测试 watermark 前进是允许的"""
        validate_watermark_constraint(
            watermark_before="2025-01-27T10:00:00Z",
            watermark_after="2025-01-27T12:00:00Z",
            update_watermark=True,
        )

    def test_watermark_same_allowed(self):
        """测试 watermark 不变是允许的"""
        validate_watermark_constraint(
            watermark_before="2025-01-27T10:00:00Z",
            watermark_after="2025-01-27T10:00:00Z",
            update_watermark=True,
        )

    def test_watermark_backward_rejected(self):
        """测试 watermark 回退被拒绝"""
        with pytest.raises(WatermarkConstraintError) as exc_info:
            validate_watermark_constraint(
                watermark_before="2025-01-27T12:00:00Z",
                watermark_after="2025-01-27T10:00:00Z",
                update_watermark=True,
            )
        assert "Watermark 回退被禁止" in str(exc_info.value)

    def test_none_watermark_skips_validation(self):
        """测试 None watermark 跳过验证"""
        validate_watermark_constraint(
            watermark_before=None,
            watermark_after="2025-01-27T12:00:00Z",
            update_watermark=True,
        )
        validate_watermark_constraint(
            watermark_before="2025-01-27T10:00:00Z",
            watermark_after=None,
            update_watermark=True,
        )


class TestBackfillWindow:
    """回填时间窗口计算测试"""

    def test_calculate_with_hours(self):
        """测试按小时计算回填窗口"""
        since, until = calculate_backfill_window(hours=24)
        
        # 验证时间差约为 24 小时
        delta = until - since
        assert abs(delta.total_seconds() - 24 * 3600) < 10  # 允许 10 秒误差

    def test_calculate_with_days(self):
        """测试按天计算回填窗口"""
        since, until = calculate_backfill_window(days=7)
        
        # 验证时间差约为 7 天
        delta = until - since
        assert abs(delta.total_seconds() - 7 * 24 * 3600) < 10

    def test_calculate_with_config(self):
        """测试从配置计算回填窗口"""
        config = BackfillConfig(repair_window_hours=48)
        since, until = calculate_backfill_window(config=config)
        
        delta = until - since
        assert abs(delta.total_seconds() - 48 * 3600) < 10

    def test_calculate_default(self):
        """测试默认回填窗口"""
        since, until = calculate_backfill_window()
        
        delta = until - since
        assert abs(delta.total_seconds() - DEFAULT_REPAIR_WINDOW_HOURS * 3600) < 10


class TestBackfillConfig:
    """回填配置测试"""

    def test_default_values(self):
        """测试默认值"""
        config = BackfillConfig()
        assert config.repair_window_hours == DEFAULT_REPAIR_WINDOW_HOURS
        assert config.cron_hint == "0 2 * * *"
        assert config.max_concurrent_jobs == 4
        assert config.default_update_watermark is False

    @patch("scm_sync_runner.get_config")
    def test_from_config(self, mock_get_config):
        """测试从配置文件加载"""
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.backfill.repair_window_hours": 48,
            "scm.backfill.cron_hint": "0 3 * * *",
            "scm.backfill.max_concurrent_jobs": 8,
            "scm.backfill.default_update_watermark": True,
        }.get(key, default)
        mock_get_config.return_value = mock_config

        config = BackfillConfig.from_config(mock_config)
        assert config.repair_window_hours == 48
        assert config.cron_hint == "0 3 * * *"
        assert config.max_concurrent_jobs == 8
        assert config.default_update_watermark is True


class TestIncrementalConfig:
    """增量同步配置测试"""

    def test_default_values(self):
        """测试默认值"""
        config = IncrementalConfig()
        assert config.loop is False
        assert config.loop_interval_seconds == DEFAULT_LOOP_INTERVAL_SECONDS
        assert config.max_iterations == 0


class TestSyncResult:
    """同步结果测试"""

    def test_to_json(self):
        """测试 JSON 序列化"""
        result = SyncResult(
            phase="incremental",
            repo="gitlab:123",
            status="success",
            items_synced=100,
        )
        json_str = result.to_json()
        data = json.loads(json_str)
        
        assert data["phase"] == "incremental"
        assert data["repo"] == "gitlab:123"
        assert data["status"] == "success"
        assert data["items_synced"] == 100

    def test_to_dict(self):
        """测试字典转换"""
        result = SyncResult(
            phase="backfill",
            repo="svn:https://example.com",
            job="commits",
        )
        data = result.to_dict()
        
        assert data["phase"] == "backfill"
        assert data["repo"] == "svn:https://example.com"
        assert data["job"] == "commits"


class TestGetScriptPath:
    """脚本路径获取测试"""

    def test_gitlab_commits(self):
        """测试 GitLab commits 脚本路径"""
        path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_COMMITS)
        assert "scm_sync_gitlab_commits.py" in path

    def test_gitlab_mrs(self):
        """测试 GitLab MRs 脚本路径"""
        path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_MRS)
        assert "scm_sync_gitlab_mrs.py" in path

    def test_gitlab_reviews(self):
        """测试 GitLab Reviews 脚本路径"""
        path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_REVIEWS)
        assert "scm_sync_gitlab_reviews.py" in path

    def test_svn_commits(self):
        """测试 SVN commits 脚本路径"""
        path = get_script_path(REPO_TYPE_SVN, JOB_TYPE_COMMITS)
        assert "scm_sync_svn.py" in path

    def test_invalid_combination(self):
        """测试无效组合"""
        with pytest.raises(ValueError) as exc_info:
            get_script_path(REPO_TYPE_SVN, JOB_TYPE_MRS)
        assert "不支持的仓库/任务组合" in str(exc_info.value)


class TestBuildSyncCommand:
    """构建同步命令测试"""

    def test_basic_command(self):
        """测试基本命令构建"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        job = JobSpec.parse("commits")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            job=job,
        )
        
        cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
        assert "python" in cmd[0] or "python3" in cmd[0]
        assert any("scm_sync_gitlab_commits.py" in c for c in cmd)

    def test_command_with_config_path(self):
        """测试带配置路径的命令"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            config_path="/path/to/config.toml",
        )
        
        cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
        assert "--config" in cmd
        assert "/path/to/config.toml" in cmd

    def test_command_with_verbose(self):
        """测试带 verbose 的命令"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            verbose=True,
        )
        
        cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
        assert "--verbose" in cmd

    def test_command_with_dry_run(self):
        """测试带 dry-run 的命令"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            dry_run=True,
        )
        
        cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
        assert "--dry-run" in cmd

    def test_backfill_command_with_time_range(self):
        """测试回填命令带时间范围"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=False,
        )
        
        since = datetime(2025, 1, 26, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 27, 0, 0, 0, tzinfo=timezone.utc)
        
        cmd = build_sync_command(
            ctx,
            RunnerPhase.BACKFILL,
            since_time=since,
            until_time=until,
        )
        
        assert "--since" in cmd
        assert "--until" in cmd
        assert "--no-update-cursor" in cmd

    def test_backfill_command_with_update_watermark(self):
        """测试回填命令更新 watermark"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=True,
        )
        
        since = datetime(2025, 1, 26, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 27, 0, 0, 0, tzinfo=timezone.utc)
        
        cmd = build_sync_command(
            ctx,
            RunnerPhase.BACKFILL,
            since_time=since,
            until_time=until,
        )
        
        # 更新 watermark 时不应包含 --no-update-cursor
        assert "--no-update-cursor" not in cmd


class TestRunnerStatus:
    """运行器状态枚举测试"""

    def test_status_values(self):
        """测试状态值"""
        assert RunnerStatus.SUCCESS.value == "success"
        assert RunnerStatus.PARTIAL.value == "partial"
        assert RunnerStatus.FAILED.value == "failed"
        assert RunnerStatus.SKIPPED.value == "skipped"
        assert RunnerStatus.CANCELLED.value == "cancelled"


class TestRunnerPhase:
    """运行器阶段枚举测试"""

    def test_phase_values(self):
        """测试阶段值"""
        assert RunnerPhase.INCREMENTAL.value == "incremental"
        assert RunnerPhase.BACKFILL.value == "backfill"


class TestTimeWindowSplit:
    """时间窗口切分测试"""

    def test_split_basic(self):
        """测试基本时间窗口切分"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        
        chunks = split_time_window(since, until, chunk_hours=4)
        
        # 12 小时 / 4 小时 = 3 个窗口
        assert len(chunks) == 3
        assert chunks[0].index == 0
        assert chunks[0].total == 3
        assert chunks[-1].index == 2

    def test_split_no_overlap_no_gap(self):
        """测试窗口切分不漏不重"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)  # 24 小时
        
        chunks = split_time_window(since, until, chunk_hours=6)
        
        # 验证不漏不重
        assert len(chunks) == 4
        
        # 验证第一个窗口从 since 开始
        assert chunks[0].since == since
        
        # 验证最后一个窗口到 until 结束
        assert chunks[-1].until == until
        
        # 验证窗口连续（前一个的 until 等于后一个的 since）
        for i in range(len(chunks) - 1):
            assert chunks[i].until == chunks[i + 1].since, \
                f"窗口 {i} 和 {i+1} 之间有间隙或重叠"

    def test_split_uneven_division(self):
        """测试不能整除的切分"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)  # 10 小时
        
        chunks = split_time_window(since, until, chunk_hours=4)
        
        # 10 小时 / 4 小时 = 3 个窗口（最后一个窗口只有 2 小时）
        assert len(chunks) == 3
        
        # 验证覆盖完整
        assert chunks[0].since == since
        assert chunks[-1].until == until
        
        # 验证最后一个窗口较短
        last_chunk_hours = (chunks[-1].until - chunks[-1].since).total_seconds() / 3600
        assert last_chunk_hours == 2

    def test_split_empty_range(self):
        """测试空范围"""
        since = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)  # since >= until
        
        chunks = split_time_window(since, until, chunk_hours=4)
        
        assert len(chunks) == 0

    def test_split_single_chunk(self):
        """测试只需一个窗口的情况"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 2, 0, 0, tzinfo=timezone.utc)  # 2 小时
        
        chunks = split_time_window(since, until, chunk_hours=4)
        
        assert len(chunks) == 1
        assert chunks[0].since == since
        assert chunks[0].until == until


class TestRevisionWindowSplit:
    """Revision 窗口切分测试"""

    def test_split_basic(self):
        """测试基本 revision 窗口切分"""
        chunks = split_revision_window(1, 300, chunk_size=100)
        
        # 300 / 100 = 3 个窗口
        assert len(chunks) == 3
        assert chunks[0].start_rev == 1
        assert chunks[0].end_rev == 100
        assert chunks[1].start_rev == 101
        assert chunks[1].end_rev == 200
        assert chunks[2].start_rev == 201
        assert chunks[2].end_rev == 300

    def test_split_no_overlap_no_gap(self):
        """测试 revision 切分不漏不重"""
        chunks = split_revision_window(100, 500, chunk_size=100)
        
        # 验证不漏不重
        assert len(chunks) == 5  # (500 - 100 + 1) / 100 = 4.01 -> 5 个窗口
        
        # 验证第一个窗口从 start_rev 开始
        assert chunks[0].start_rev == 100
        
        # 验证最后一个窗口到 end_rev 结束
        assert chunks[-1].end_rev == 500
        
        # 验证窗口连续（前一个的 end_rev + 1 等于后一个的 start_rev）
        for i in range(len(chunks) - 1):
            assert chunks[i].end_rev + 1 == chunks[i + 1].start_rev, \
                f"窗口 {i} 和 {i+1} 之间有间隙或重叠"
        
        # 验证所有 revision 都被覆盖
        all_revs = set()
        for chunk in chunks:
            for rev in range(chunk.start_rev, chunk.end_rev + 1):
                assert rev not in all_revs, f"Revision {rev} 被重复覆盖"
                all_revs.add(rev)
        
        expected_revs = set(range(100, 501))
        assert all_revs == expected_revs, "有 revision 未被覆盖"

    def test_split_uneven_division(self):
        """测试不能整除的切分"""
        chunks = split_revision_window(1, 250, chunk_size=100)
        
        # 250 / 100 = 3 个窗口（最后一个窗口只有 50 个）
        assert len(chunks) == 3
        assert chunks[-1].start_rev == 201
        assert chunks[-1].end_rev == 250

    def test_split_empty_range(self):
        """测试空范围"""
        chunks = split_revision_window(100, 50, chunk_size=100)  # start > end
        
        assert len(chunks) == 0

    def test_split_single_chunk(self):
        """测试只需一个窗口的情况"""
        chunks = split_revision_window(1, 50, chunk_size=100)
        
        assert len(chunks) == 1
        assert chunks[0].start_rev == 1
        assert chunks[0].end_rev == 50


class TestBackfillWatermarkBehavior:
    """回填模式 watermark 行为测试"""

    def test_backfill_default_no_update_watermark(self):
        """测试回填模式默认不更新 watermark"""
        args = parse_args([
            "backfill", "--repo", "gitlab:123",
            "--last-hours", "24"
        ])
        assert args.update_watermark is False

    def test_backfill_explicit_update_watermark(self):
        """测试回填模式显式更新 watermark"""
        args = parse_args([
            "backfill", "--repo", "gitlab:123",
            "--last-hours", "24", "--update-watermark"
        ])
        assert args.update_watermark is True

    def test_watermark_monotonic_increase_only(self):
        """测试 watermark 只能单调递增"""
        # 前进是允许的
        validate_watermark_constraint(
            watermark_before="2025-01-27T10:00:00Z",
            watermark_after="2025-01-27T12:00:00Z",
            update_watermark=True,
        )
        
        # 后退被拒绝
        with pytest.raises(WatermarkConstraintError):
            validate_watermark_constraint(
                watermark_before="2025-01-27T12:00:00Z",
                watermark_after="2025-01-27T10:00:00Z",
                update_watermark=True,
            )

    def test_no_update_watermark_allows_any_value(self):
        """测试不更新 watermark 时允许任意值（不检查回退）"""
        # 即使 watermark 回退，只要 update_watermark=False 就不报错
        validate_watermark_constraint(
            watermark_before="2025-01-27T12:00:00Z",
            watermark_after="2025-01-27T08:00:00Z",
            update_watermark=False,
        )


class TestBuildSyncCommandWithRevision:
    """构建同步命令测试（包含 SVN revision 参数）"""

    def test_svn_backfill_with_revisions(self):
        """测试 SVN 回填命令包含 revision 参数"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("svn:https://svn.example.com/repo")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=False,
        )
        
        cmd = build_sync_command(
            ctx,
            RunnerPhase.BACKFILL,
            start_rev=100,
            end_rev=200,
        )
        
        assert "--backfill" in cmd
        assert "--start-rev" in cmd
        assert "100" in cmd
        assert "--end-rev" in cmd
        assert "200" in cmd
        # 不更新 watermark 时不应包含 --update-watermark
        assert "--update-watermark" not in cmd

    def test_svn_backfill_with_update_watermark(self):
        """测试 SVN 回填命令更新 watermark"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("svn:https://svn.example.com/repo")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=True,
        )
        
        cmd = build_sync_command(
            ctx,
            RunnerPhase.BACKFILL,
            start_rev=100,
            end_rev=200,
        )
        
        assert "--backfill" in cmd
        assert "--update-watermark" in cmd

    def test_gitlab_backfill_with_until(self):
        """测试 GitLab 回填命令包含 until 参数"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        
        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=False,
        )
        
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 31, 23, 59, 59, tzinfo=timezone.utc)
        
        cmd = build_sync_command(
            ctx,
            RunnerPhase.BACKFILL,
            since_time=since,
            until_time=until,
        )
        
        assert "--since" in cmd
        assert "--until" in cmd
        assert "--no-update-cursor" in cmd


class TestHttpConfigDSNFallback:
    """HttpConfig DSN 回退测试"""

    def test_postgres_dsn_from_config_priority(self):
        """测试配置中的 postgres_rate_limit_dsn 优先级最高"""
        from engram_step1.gitlab_client import HttpConfig
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.postgres_rate_limit_enabled": True,
            "scm.gitlab.postgres_rate_limit_dsn": "postgresql://config:pwd@config-host:5432/db",
        }.get(key, default)
        
        # 设置环境变量（应被忽略，因为配置中有值）
        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://env:pwd@env-host:5432/db"}):
            http_config = HttpConfig.from_config(mock_config)
        
        assert http_config.postgres_rate_limit_dsn == "postgresql://config:pwd@config-host:5432/db"

    def test_postgres_dsn_fallback_to_env_var(self):
        """测试配置中没有 postgres_rate_limit_dsn 时回退到 POSTGRES_DSN 环境变量"""
        from engram_step1.gitlab_client import HttpConfig
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.postgres_rate_limit_enabled": True,
            "scm.gitlab.postgres_rate_limit_dsn": None,  # 配置中没有
        }.get(key, default)
        
        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://env:pwd@env-host:5432/db"}):
            http_config = HttpConfig.from_config(mock_config)
        
        assert http_config.postgres_rate_limit_dsn == "postgresql://env:pwd@env-host:5432/db"

    def test_postgres_dsn_none_when_both_missing(self):
        """测试配置和环境变量都没有时 DSN 为 None"""
        from engram_step1.gitlab_client import HttpConfig
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.postgres_rate_limit_enabled": True,
            "scm.gitlab.postgres_rate_limit_dsn": None,
        }.get(key, default)
        
        # 确保环境变量不存在
        env_copy = os.environ.copy()
        env_copy.pop("POSTGRES_DSN", None)
        with patch.dict(os.environ, env_copy, clear=True):
            http_config = HttpConfig.from_config(mock_config)
        
        assert http_config.postgres_rate_limit_dsn is None

    def test_postgres_dsn_empty_string_in_config_uses_env(self):
        """测试配置中空字符串时不回退到环境变量（空字符串是有效值）"""
        from engram_step1.gitlab_client import HttpConfig
        
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.gitlab.postgres_rate_limit_enabled": True,
            "scm.gitlab.postgres_rate_limit_dsn": "",  # 空字符串
        }.get(key, default)
        
        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://env:pwd@env-host:5432/db"}):
            http_config = HttpConfig.from_config(mock_config)
        
        # 空字符串是 falsy 的但不是 None，应该回退到环境变量
        # 因为我们用的是 `if postgres_dsn is None` 判断
        assert http_config.postgres_rate_limit_dsn == ""


class TestInstanceKeyGeneration:
    """instance_key 生成规则测试"""

    def test_instance_key_format_gitlab_prefix(self):
        """测试 instance_key 使用 gitlab: 前缀"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig()
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="test-token",
            http_config=http_config,
        )
        
        instance_key = client._extract_instance_key("https://gitlab.example.com")
        assert instance_key == "gitlab:gitlab.example.com"

    def test_instance_key_stability_same_host(self):
        """测试同一 host 生成相同的 instance_key"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig()
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="test-token",
            http_config=http_config,
        )
        
        # 不同路径但同一 host
        key1 = client._extract_instance_key("https://gitlab.example.com")
        key2 = client._extract_instance_key("https://gitlab.example.com/api/v4")
        key3 = client._extract_instance_key("https://gitlab.example.com:443")
        
        assert key1 == key2 == "gitlab:gitlab.example.com"
        # 带端口的 URL 会生成不同的 key（这是预期行为）
        assert key3 == "gitlab:gitlab.example.com:443"

    def test_instance_key_different_hosts(self):
        """测试不同 host 生成不同的 instance_key"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig()
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="test-token",
            http_config=http_config,
        )
        
        key1 = client._extract_instance_key("https://gitlab.example.com")
        key2 = client._extract_instance_key("https://gitlab.company.com")
        key3 = client._extract_instance_key("https://gitlab.com")
        
        assert key1 != key2 != key3
        assert key1 == "gitlab:gitlab.example.com"
        assert key2 == "gitlab:gitlab.company.com"
        assert key3 == "gitlab:gitlab.com"

    def test_instance_key_with_port(self):
        """测试带端口的 URL 生成包含端口的 instance_key"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig()
        client = GitLabClient(
            base_url="https://gitlab.example.com:8443",
            private_token="test-token",
            http_config=http_config,
        )
        
        instance_key = client._extract_instance_key("https://gitlab.example.com:8443")
        assert instance_key == "gitlab:gitlab.example.com:8443"

    def test_instance_key_preserves_subdomain(self):
        """测试子域名被保留在 instance_key 中"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig()
        client = GitLabClient(
            base_url="https://internal.gitlab.company.com",
            private_token="test-token",
            http_config=http_config,
        )
        
        instance_key = client._extract_instance_key("https://internal.gitlab.company.com")
        assert instance_key == "gitlab:internal.gitlab.company.com"

    def test_instance_key_fallback_for_invalid_url(self):
        """测试无效 URL 时的回退处理"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig()
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="test-token",
            http_config=http_config,
        )
        
        # 无效 URL 应该使用原始字符串
        instance_key = client._extract_instance_key("not-a-valid-url")
        assert instance_key == "gitlab:not-a-valid-url"


class TestClientStatsCountsContract:
    """ClientStats counts 字段契约测试
    
    验证 ClientStats.to_dict() 输出的字段与 db.get_sync_runs_health_stats() 读取的字段一致。
    这些字段是同步运行健康统计的关键，用于熔断决策。
    """

    def test_stats_to_dict_contains_required_counts_fields(self):
        """测试 to_dict() 包含 counts 必需的字段"""
        from engram_step1.gitlab_client import ClientStats
        
        stats = ClientStats()
        result = stats.to_dict()
        
        # 验证必需字段存在
        required_fields = [
            "total_requests",
            "total_429_hits",
            "timeout_count",
        ]
        for field in required_fields:
            assert field in result, f"缺少必需字段: {field}"

    def test_stats_to_dict_fields_are_integers(self):
        """测试 counts 字段类型为 int"""
        from engram_step1.gitlab_client import ClientStats, RequestStats
        
        stats = ClientStats()
        # 记录一些请求
        stats.record(RequestStats(
            endpoint="/test",
            method="GET",
            status_code=200,
            duration_ms=100,
            success=True,
        ))
        stats.record(RequestStats(
            endpoint="/test",
            method="GET",
            status_code=429,
            duration_ms=50,
            hit_429=True,
            success=False,
        ))
        
        result = stats.to_dict()
        
        # 验证类型
        assert isinstance(result["total_requests"], int), "total_requests 应为 int"
        assert isinstance(result["total_429_hits"], int), "total_429_hits 应为 int"
        assert isinstance(result["timeout_count"], int), "timeout_count 应为 int"

    def test_stats_to_dict_429_hit_increments_counter(self):
        """测试 429 命中正确增加计数器"""
        from engram_step1.gitlab_client import ClientStats, RequestStats
        
        stats = ClientStats()
        
        # 记录一个 429 请求
        stats.record(RequestStats(
            endpoint="/test",
            method="GET",
            status_code=429,
            duration_ms=50,
            hit_429=True,
            success=False,
        ))
        
        result = stats.to_dict()
        
        assert result["total_requests"] == 1
        assert result["total_429_hits"] == 1
        assert result["failed_requests"] == 1

    def test_stats_to_dict_timeout_count_from_limiter(self):
        """测试 timeout_count 来自 limiter 统计"""
        from engram_step1.gitlab_client import ClientStats
        
        stats = ClientStats()
        
        # 模拟 limiter 统计
        stats.set_limiter_stats(
            timeout_count=5,
            avg_wait_time_ms=123.45,
        )
        
        result = stats.to_dict()
        
        assert result["timeout_count"] == 5
        assert result["avg_wait_time_ms"] == 123.45

    def test_stats_default_values_are_zero(self):
        """测试默认值为 0（不是 None）"""
        from engram_step1.gitlab_client import ClientStats
        
        stats = ClientStats()
        result = stats.to_dict()
        
        # 验证默认值为 0 而不是 None
        assert result["total_requests"] == 0
        assert result["total_429_hits"] == 0
        assert result["timeout_count"] == 0
        assert result["avg_wait_time_ms"] == 0


class TestRateLimiter429Notification:
    """429 通知 Rate Limiter 测试
    
    验证当收到 429 响应时，rate limiter 被正确通知。
    """

    def test_rate_limiter_notify_on_429(self):
        """测试 RateLimiter 在 429 时被通知"""
        from engram_step1.gitlab_client import RateLimiter
        import time
        
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 获取初始 paused_until
        stats_before = limiter.get_stats()
        assert stats_before["paused_until"] is None
        
        # 通知 429
        limiter.notify_rate_limit(retry_after=5.0)
        
        # 验证 paused_until 被设置
        stats_after = limiter.get_stats()
        assert stats_after["paused_until"] is not None
        assert stats_after["paused_until"] > time.time()

    def test_rate_limiter_notify_with_reset_time(self):
        """测试 RateLimiter 使用 reset_time 通知"""
        from engram_step1.gitlab_client import RateLimiter
        import time
        
        limiter = RateLimiter(requests_per_second=10.0)
        
        # 使用 reset_time（Unix 时间戳）
        future_time = time.time() + 10.0
        limiter.notify_rate_limit(reset_time=future_time)
        
        # 验证 paused_until 被设置为 reset_time
        stats = limiter.get_stats()
        assert stats["paused_until"] is not None
        # 允许 1 秒误差
        assert abs(stats["paused_until"] - future_time) < 1.0

    def test_composed_rate_limiter_notifies_all(self):
        """测试 ComposedRateLimiter 通知所有子 limiter"""
        from engram_step1.gitlab_client import ComposedRateLimiter, RateLimiter
        
        limiter1 = RateLimiter(requests_per_second=10.0)
        limiter2 = RateLimiter(requests_per_second=5.0)
        
        composed = ComposedRateLimiter([limiter1, limiter2])
        
        # 通知 429
        composed.notify_rate_limit(retry_after=3.0)
        
        # 验证两个 limiter 都被通知
        stats1 = limiter1.get_stats()
        stats2 = limiter2.get_stats()
        
        assert stats1["paused_until"] is not None
        assert stats2["paused_until"] is not None

    def test_composed_rate_limiter_stats_contains_429_hits(self):
        """测试 ComposedRateLimiter 统计包含 429 命中"""
        from engram_step1.gitlab_client import ComposedRateLimiter, RateLimiter
        
        limiter = RateLimiter(requests_per_second=10.0)
        composed = ComposedRateLimiter([limiter])
        
        # 多次通知 429
        composed.notify_rate_limit(retry_after=1.0)
        composed.notify_rate_limit(retry_after=2.0)
        composed.notify_rate_limit(retry_after=3.0)
        
        stats = composed.get_stats()
        
        assert stats["total_429_hits"] == 3


class TestSyncRunsCountsConsistency:
    """sync_runs counts 字段一致性测试
    
    验证同步脚本写入的 counts 字段与 get_sync_runs_health_stats 读取的字段一致。
    """

    def test_counts_field_names_match_health_stats_query(self):
        """测试 counts 字段名与健康统计查询匹配"""
        # db.get_sync_runs_health_stats 读取的字段
        health_stats_fields = [
            "total_429_hits",
            "timeout_count",
            "total_requests",
        ]
        
        # ClientStats.to_dict() 输出的字段
        from engram_step1.gitlab_client import ClientStats
        
        stats = ClientStats()
        stats_dict = stats.to_dict()
        
        # 验证所有健康统计需要的字段都存在于 stats 输出中
        for field in health_stats_fields:
            assert field in stats_dict, \
                f"ClientStats.to_dict() 缺少 get_sync_runs_health_stats 需要的字段: {field}"

    def test_request_stats_tracks_429(self):
        """测试 RequestStats 正确跟踪 429"""
        from engram_step1.gitlab_client import RequestStats
        
        # 模拟一个 429 请求
        stats = RequestStats(
            endpoint="/api/v4/projects/123/commits",
            method="GET",
            status_code=429,
            duration_ms=50.0,
            attempt_count=3,
            hit_429=True,
            success=False,
            error_category="rate_limited",
            retry_after=60.0,
            rate_limit_reset=1706400000.0,
            rate_limit_remaining=0,
        )
        
        assert stats.hit_429 is True
        assert stats.status_code == 429
        assert stats.retry_after == 60.0
        assert stats.rate_limit_reset == 1706400000.0


class TestPostgresRateLimiterSharedBucket:
    """PostgresRateLimiter 共享 bucket 测试"""

    def test_same_host_shares_instance_key(self):
        """测试同一 host 的多个客户端共享相同的 instance_key"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig, PostgresRateLimiter
        
        http_config = HttpConfig(
            postgres_rate_limit_enabled=True,
            postgres_rate_limit_dsn="postgresql://test:test@localhost:5432/test",
        )
        
        # 创建两个客户端指向同一 GitLab 实例
        client1 = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="token1",
            http_config=http_config,
        )
        
        client2 = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="token2",
            http_config=http_config,
        )
        
        # 两个客户端应该有相同的 instance_key
        assert client1._postgres_rate_limiter is not None
        assert client2._postgres_rate_limiter is not None
        assert client1._postgres_rate_limiter.instance_key == client2._postgres_rate_limiter.instance_key
        assert client1._postgres_rate_limiter.instance_key == "gitlab:gitlab.example.com"

    def test_different_hosts_different_instance_keys(self):
        """测试不同 host 的客户端使用不同的 instance_key"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        http_config = HttpConfig(
            postgres_rate_limit_enabled=True,
            postgres_rate_limit_dsn="postgresql://test:test@localhost:5432/test",
        )
        
        client1 = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="token1",
            http_config=http_config,
        )
        
        client2 = GitLabClient(
            base_url="https://gitlab.company.com",
            private_token="token2",
            http_config=http_config,
        )
        
        # 两个客户端应该有不同的 instance_key
        assert client1._postgres_rate_limiter.instance_key != client2._postgres_rate_limiter.instance_key
        assert client1._postgres_rate_limiter.instance_key == "gitlab:gitlab.example.com"
        assert client2._postgres_rate_limiter.instance_key == "gitlab:gitlab.company.com"

    def test_postgres_rate_limiter_uses_correct_dsn(self):
        """测试 PostgresRateLimiter 使用正确的 DSN"""
        from engram_step1.gitlab_client import GitLabClient, HttpConfig
        
        expected_dsn = "postgresql://test:test@localhost:5432/test"
        http_config = HttpConfig(
            postgres_rate_limit_enabled=True,
            postgres_rate_limit_dsn=expected_dsn,
        )
        
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            private_token="test-token",
            http_config=http_config,
        )
        
        assert client._postgres_rate_limiter is not None
        assert client._postgres_rate_limiter._dsn == expected_dsn


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
