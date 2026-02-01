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
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# 直接从核心模块导入，确保测试的稳定性
# 不再依赖根目录的 scm_sync_runner.py 兼容层
from engram.logbook.scm_sync_runner import (
    DEFAULT_LOOP_INTERVAL_SECONDS,
    DEFAULT_REPAIR_WINDOW_HOURS,
    DEFAULT_WINDOW_CHUNK_HOURS,
    DEFAULT_WINDOW_CHUNK_REVS,
    EXIT_FAILED,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    JOB_TYPE_COMMITS,
    JOB_TYPE_MRS,
    JOB_TYPE_REVIEWS,
    # 常量
    REPO_TYPE_GITLAB,
    REPO_TYPE_SVN,
    AggregatedResult,
    BackfillConfig,
    IncrementalConfig,
    JobSpec,
    # 数据类
    RepoSpec,
    RevisionWindowChunk,
    RunnerContext,
    RunnerPhase,
    # 枚举
    RunnerStatus,
    SyncResult,
    # 类
    SyncRunner,
    TimeWindowChunk,
    # 异常
    WatermarkConstraintError,
    build_sync_command,
    calculate_backfill_window,
    # 解析器
    get_exit_code,
    get_script_path,
    parse_args,
    split_revision_window,
    # 辅助函数
    split_time_window,
    validate_watermark_constraint,
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
        args = parse_args(
            ["incremental", "--repo", "gitlab:123", "--loop", "--loop-interval", "120"]
        )
        assert args.loop_interval == 120

    def test_incremental_with_max_iterations(self):
        """测试最大迭代次数参数"""
        args = parse_args(
            ["incremental", "--repo", "gitlab:123", "--loop", "--max-iterations", "10"]
        )
        assert args.max_iterations == 10

    def test_backfill_basic(self):
        """测试基本回填参数"""
        args = parse_args(["backfill", "--repo", "gitlab:123"])
        assert args.command == "backfill"
        assert args.repo == "gitlab:123"
        assert args.update_watermark is False

    def test_backfill_last_hours(self):
        """测试回填小时数参数"""
        args = parse_args(["backfill", "--repo", "gitlab:123", "--last-hours", "48"])
        assert args.last_hours == 48
        assert args.last_days is None

    def test_backfill_last_days(self):
        """测试回填天数参数"""
        args = parse_args(["backfill", "--repo", "gitlab:123", "--last-days", "7"])
        assert args.last_days == 7
        assert args.last_hours is None

    def test_backfill_update_watermark(self):
        """测试更新 watermark 参数"""
        args = parse_args(["backfill", "--repo", "gitlab:123", "--update-watermark"])
        assert args.update_watermark is True

    def test_backfill_mutually_exclusive_time(self):
        """测试时间参数互斥"""
        with pytest.raises(SystemExit):
            parse_args(
                ["backfill", "--repo", "gitlab:123", "--last-hours", "24", "--last-days", "7"]
            )

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
        args = parse_args(["incremental", "--repo", "gitlab:123", "--job", "mrs"])
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

    def test_from_config(self):
        """测试从配置文件加载"""
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.backfill.repair_window_hours": 48,
            "scm.backfill.cron_hint": "0 3 * * *",
            "scm.backfill.max_concurrent_jobs": 8,
            "scm.backfill.default_update_watermark": True,
        }.get(key, default)

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
    """脚本路径获取测试

    注意：get_script_path() 已标记为 deprecated，这些测试验证其向后兼容性。
    """

    def test_gitlab_commits(self):
        """测试 GitLab commits 脚本路径"""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_COMMITS)
            assert "scm_sync_gitlab_commits.py" in path
            # 验证产生了 DeprecationWarning
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "已弃用" in str(w[0].message)

    def test_gitlab_mrs(self):
        """测试 GitLab MRs 脚本路径"""
        import warnings

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_MRS)
            assert "scm_sync_gitlab_mrs.py" in path

    def test_gitlab_reviews(self):
        """测试 GitLab Reviews 脚本路径"""
        import warnings

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_REVIEWS)
            assert "scm_sync_gitlab_reviews.py" in path

    def test_svn_commits(self):
        """测试 SVN commits 脚本路径"""
        import warnings

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            path = get_script_path(REPO_TYPE_SVN, JOB_TYPE_COMMITS)
            assert "scm_sync_svn.py" in path

    def test_invalid_combination(self):
        """测试无效组合"""
        import warnings

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            with pytest.raises(ValueError) as exc_info:
                get_script_path(REPO_TYPE_SVN, JOB_TYPE_MRS)
            assert "不支持的仓库/任务组合" in str(exc_info.value)

    def test_deprecation_warning_message(self):
        """测试 deprecation 警告消息内容"""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_COMMITS)

            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            warning_msg = str(w[0].message)
            assert "SyncRunner" in warning_msg or "SyncExecutor" in warning_msg
            assert "根目录脚本" in warning_msg or "将被移除" in warning_msg


class TestBuildSyncCommand:
    """构建同步命令测试

    注意：build_sync_command() 已标记为 deprecated，这些测试验证其向后兼容性。
    """

    def test_basic_command(self):
        """测试基本命令构建"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        job = JobSpec.parse("commits")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            job=job,
        )

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
            assert "python" in cmd[0] or "python3" in cmd[0]
            assert any("scm_sync_gitlab_commits.py" in c for c in cmd)
            # 验证产生了 DeprecationWarning
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)

    def test_command_with_config_path(self):
        """测试带配置路径的命令"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            config_path="/path/to/config.toml",
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
            assert "--config" in cmd
            assert "/path/to/config.toml" in cmd

    def test_command_with_verbose(self):
        """测试带 verbose 的命令"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            verbose=True,
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
            assert "--verbose" in cmd

    def test_command_with_dry_run(self):
        """测试带 dry-run 的命令"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            dry_run=True,
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
            assert "--dry-run" in cmd

    def test_backfill_command_with_time_range(self):
        """测试回填命令带时间范围"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=False,
        )

        since = datetime(2025, 1, 26, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 27, 0, 0, 0, tzinfo=timezone.utc)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
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
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=True,
        )

        since = datetime(2025, 1, 26, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 27, 0, 0, 0, tzinfo=timezone.utc)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            cmd = build_sync_command(
                ctx,
                RunnerPhase.BACKFILL,
                since_time=since,
                until_time=until,
            )

            # 更新 watermark 时不应包含 --no-update-cursor
            assert "--no-update-cursor" not in cmd

    def test_deprecation_warning_message(self):
        """测试 deprecation 警告消息内容"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
        )

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_sync_command(ctx, RunnerPhase.INCREMENTAL)

            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            warning_msg = str(w[0].message)
            assert "SyncRunner" in warning_msg
            assert "run_incremental" in warning_msg or "run_backfill" in warning_msg


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
            assert chunks[i].until == chunks[i + 1].since, f"窗口 {i} 和 {i + 1} 之间有间隙或重叠"

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
            assert chunks[i].end_rev + 1 == chunks[i + 1].start_rev, (
                f"窗口 {i} 和 {i + 1} 之间有间隙或重叠"
            )

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
        args = parse_args(["backfill", "--repo", "gitlab:123", "--last-hours", "24"])
        assert args.update_watermark is False

    def test_backfill_explicit_update_watermark(self):
        """测试回填模式显式更新 watermark"""
        args = parse_args(
            ["backfill", "--repo", "gitlab:123", "--last-hours", "24", "--update-watermark"]
        )
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
    """构建同步命令测试（包含 SVN revision 参数）

    注意：build_sync_command() 已标记为 deprecated，这些测试验证其向后兼容性。
    """

    def test_svn_backfill_with_revisions(self):
        """测试 SVN 回填命令包含 revision 参数"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("svn:https://svn.example.com/repo")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=False,
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
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
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("svn:https://svn.example.com/repo")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=True,
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
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
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            update_watermark=False,
        )

        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 31, 23, 59, 59, tzinfo=timezone.utc)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            cmd = build_sync_command(
                ctx,
                RunnerPhase.BACKFILL,
                since_time=since,
                until_time=until,
            )

            assert "--since" in cmd
            assert "--until" in cmd
            assert "--no-update-cursor" in cmd


class TestDeprecatedFunctions:
    """弃用函数测试

    验证 get_script_path() 和 build_sync_command() 的 deprecation 行为。
    这些函数已弃用，新代码应使用 SyncRunner + SyncExecutor。
    """

    def test_get_script_path_emits_deprecation_warning(self):
        """验证 get_script_path 发出 DeprecationWarning"""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_COMMITS)

            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "get_script_path" in str(w[0].message)

    def test_build_sync_command_emits_deprecation_warning(self):
        """验证 build_sync_command 发出 DeprecationWarning"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(config=mock_config, repo=repo)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_sync_command(ctx, RunnerPhase.INCREMENTAL)

            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "build_sync_command" in str(w[0].message)

    def test_build_sync_command_does_not_double_warn(self):
        """验证 build_sync_command 内部调用 get_script_path 时不重复警告"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(config=mock_config, repo=repo)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_sync_command(ctx, RunnerPhase.INCREMENTAL)

            # 只应有 1 个警告（来自 build_sync_command），
            # get_script_path 的警告被内部抑制
            assert len(w) == 1
            assert "build_sync_command" in str(w[0].message)

    def test_deprecated_functions_still_work(self):
        """验证弃用函数仍然正常工作（向后兼容）"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        job = JobSpec.parse("commits")
        ctx = RunnerContext(config=mock_config, repo=repo, job=job)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            # get_script_path 仍返回正确路径
            path = get_script_path(REPO_TYPE_GITLAB, JOB_TYPE_COMMITS)
            assert "scm_sync_gitlab_commits.py" in path

            # build_sync_command 仍生成正确命令
            cmd = build_sync_command(ctx, RunnerPhase.INCREMENTAL)
            assert len(cmd) > 0
            assert "--repo" in cmd

    def test_sync_runner_does_not_use_deprecated_functions(self):
        """验证 SyncRunner 核心执行路径不使用弃用函数"""
        import warnings

        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        ctx = RunnerContext(config=mock_config, repo=repo, dry_run=True)

        runner = SyncRunner(ctx)

        # 运行时不应产生 DeprecationWarning
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            runner.run_incremental()

            # 过滤出 DeprecationWarning
            deprecation_warnings = [
                warning for warning in w if issubclass(warning.category, DeprecationWarning)
            ]
            # SyncRunner 核心路径不应使用弃用函数
            for warning in deprecation_warnings:
                assert "get_script_path" not in str(warning.message)
                assert "build_sync_command" not in str(warning.message)


class TestHttpConfigDSNFallback:
    """HttpConfig DSN 回退测试"""

    def test_postgres_dsn_from_config_priority(self):
        """测试配置中的 postgres_rate_limit_dsn 优先级最高"""
        from engram.logbook.gitlab_client import HttpConfig

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
        from engram.logbook.gitlab_client import HttpConfig

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
        from engram.logbook.gitlab_client import HttpConfig

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
        from engram.logbook.gitlab_client import HttpConfig

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        from engram.logbook.gitlab_client import ClientStats

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
        from engram.logbook.gitlab_client import ClientStats, RequestStats

        stats = ClientStats()
        # 记录一些请求
        stats.record(
            RequestStats(
                endpoint="/test",
                method="GET",
                status_code=200,
                duration_ms=100,
                success=True,
            )
        )
        stats.record(
            RequestStats(
                endpoint="/test",
                method="GET",
                status_code=429,
                duration_ms=50,
                hit_429=True,
                success=False,
            )
        )

        result = stats.to_dict()

        # 验证类型
        assert isinstance(result["total_requests"], int), "total_requests 应为 int"
        assert isinstance(result["total_429_hits"], int), "total_429_hits 应为 int"
        assert isinstance(result["timeout_count"], int), "timeout_count 应为 int"

    def test_stats_to_dict_429_hit_increments_counter(self):
        """测试 429 命中正确增加计数器"""
        from engram.logbook.gitlab_client import ClientStats, RequestStats

        stats = ClientStats()

        # 记录一个 429 请求
        stats.record(
            RequestStats(
                endpoint="/test",
                method="GET",
                status_code=429,
                duration_ms=50,
                hit_429=True,
                success=False,
            )
        )

        result = stats.to_dict()

        assert result["total_requests"] == 1
        assert result["total_429_hits"] == 1
        assert result["failed_requests"] == 1

    def test_stats_to_dict_timeout_count_from_limiter(self):
        """测试 timeout_count 来自 limiter 统计"""
        from engram.logbook.gitlab_client import ClientStats

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
        from engram.logbook.gitlab_client import ClientStats

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
        import time

        from engram.logbook.gitlab_client import RateLimiter

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
        import time

        from engram.logbook.gitlab_client import RateLimiter

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
        from engram.logbook.gitlab_client import ComposedRateLimiter, RateLimiter

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
        from engram.logbook.gitlab_client import ComposedRateLimiter, RateLimiter

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
        from engram.logbook.gitlab_client import ClientStats

        stats = ClientStats()
        stats_dict = stats.to_dict()

        # 验证所有健康统计需要的字段都存在于 stats 输出中
        for field in health_stats_fields:
            assert field in stats_dict, (
                f"ClientStats.to_dict() 缺少 get_sync_runs_health_stats 需要的字段: {field}"
            )

    def test_request_stats_tracks_429(self):
        """测试 RequestStats 正确跟踪 429"""
        from engram.logbook.gitlab_client import RequestStats

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
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        assert (
            client1._postgres_rate_limiter.instance_key
            == client2._postgres_rate_limiter.instance_key
        )
        assert client1._postgres_rate_limiter.instance_key == "gitlab:gitlab.example.com"

    def test_different_hosts_different_instance_keys(self):
        """测试不同 host 的客户端使用不同的 instance_key"""
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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
        assert (
            client1._postgres_rate_limiter.instance_key
            != client2._postgres_rate_limiter.instance_key
        )
        assert client1._postgres_rate_limiter.instance_key == "gitlab:gitlab.example.com"
        assert client2._postgres_rate_limiter.instance_key == "gitlab:gitlab.company.com"

    def test_postgres_rate_limiter_uses_correct_dsn(self):
        """测试 PostgresRateLimiter 使用正确的 DSN"""
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig

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


class TestChunkPayloadGeneration:
    """Chunk Payload 生成测试"""

    def test_time_window_chunk_to_payload_basic(self):
        """测试 TimeWindowChunk.to_payload 基本功能"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)

        chunk = TimeWindowChunk(since=since, until=until, index=0, total=3)
        payload = chunk.to_payload(update_watermark=False, watermark_constraint="none")

        assert payload["window_type"] == "time"
        assert payload["window_since"] == since.isoformat()
        assert payload["window_until"] == until.isoformat()
        assert payload["chunk_index"] == 0
        assert payload["chunk_total"] == 3
        assert payload["update_watermark"] is False
        assert payload["watermark_constraint"] == "none"

    def test_time_window_chunk_to_payload_with_update_watermark(self):
        """测试 TimeWindowChunk.to_payload 更新 watermark 时的策略"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)

        chunk = TimeWindowChunk(since=since, until=until, index=0, total=1)
        payload = chunk.to_payload(update_watermark=True, watermark_constraint="monotonic")

        assert payload["update_watermark"] is True
        assert payload["watermark_constraint"] == "monotonic"

    def test_revision_window_chunk_to_payload_basic(self):
        """测试 RevisionWindowChunk.to_payload 基本功能"""
        chunk = RevisionWindowChunk(start_rev=100, end_rev=200, index=1, total=5)
        payload = chunk.to_payload(update_watermark=False, watermark_constraint="none")

        assert payload["window_type"] == "revision"
        assert payload["window_start_rev"] == 100
        assert payload["window_end_rev"] == 200
        assert payload["chunk_index"] == 1
        assert payload["chunk_total"] == 5
        assert payload["update_watermark"] is False
        assert payload["watermark_constraint"] == "none"

    def test_revision_window_chunk_to_payload_with_update_watermark(self):
        """测试 RevisionWindowChunk.to_payload 更新 watermark 时的策略"""
        chunk = RevisionWindowChunk(start_rev=1, end_rev=100, index=0, total=1)
        payload = chunk.to_payload(update_watermark=True, watermark_constraint="monotonic")

        assert payload["update_watermark"] is True
        assert payload["watermark_constraint"] == "monotonic"


class TestChunkBoundaryStability:
    """Chunk 边界稳定性测试 - 验证相同输入生成稳定的 chunk 边界"""

    def test_time_window_split_deterministic(self):
        """测试时间窗口切分是确定性的：相同输入始终产生相同输出"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)  # 24 小时
        chunk_hours = 4

        # 多次调用
        results = [split_time_window(since, until, chunk_hours=chunk_hours) for _ in range(5)]

        # 验证所有结果相同
        first_result = results[0]
        for result in results[1:]:
            assert len(result) == len(first_result)
            for i, chunk in enumerate(result):
                assert chunk.since == first_result[i].since
                assert chunk.until == first_result[i].until
                assert chunk.index == first_result[i].index
                assert chunk.total == first_result[i].total

    def test_time_window_split_stable_boundaries(self):
        """测试时间窗口边界稳定：固定的 since/until 应产生固定的边界"""
        since = datetime(2025, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 15, 20, 0, 0, tzinfo=timezone.utc)  # 12 小时

        chunks = split_time_window(since, until, chunk_hours=4)

        # 验证确切的边界
        assert len(chunks) == 3

        # 第一个窗口：08:00 -> 12:00
        assert chunks[0].since == datetime(2025, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        assert chunks[0].until == datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        # 第二个窗口：12:00 -> 16:00
        assert chunks[1].since == datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert chunks[1].until == datetime(2025, 1, 15, 16, 0, 0, tzinfo=timezone.utc)

        # 第三个窗口：16:00 -> 20:00
        assert chunks[2].since == datetime(2025, 1, 15, 16, 0, 0, tzinfo=timezone.utc)
        assert chunks[2].until == datetime(2025, 1, 15, 20, 0, 0, tzinfo=timezone.utc)

    def test_revision_window_split_deterministic(self):
        """测试 revision 窗口切分是确定性的：相同输入始终产生相同输出"""
        start_rev = 100
        end_rev = 500
        chunk_size = 100

        # 多次调用
        results = [
            split_revision_window(start_rev, end_rev, chunk_size=chunk_size) for _ in range(5)
        ]

        # 验证所有结果相同
        first_result = results[0]
        for result in results[1:]:
            assert len(result) == len(first_result)
            for i, chunk in enumerate(result):
                assert chunk.start_rev == first_result[i].start_rev
                assert chunk.end_rev == first_result[i].end_rev
                assert chunk.index == first_result[i].index
                assert chunk.total == first_result[i].total

    def test_revision_window_split_stable_boundaries(self):
        """测试 revision 窗口边界稳定：固定的 start/end 应产生固定的边界"""
        chunks = split_revision_window(50, 350, chunk_size=100)

        # 验证确切的边界
        assert len(chunks) == 4

        # 第一个窗口：r50 -> r149
        assert chunks[0].start_rev == 50
        assert chunks[0].end_rev == 149

        # 第二个窗口：r150 -> r249
        assert chunks[1].start_rev == 150
        assert chunks[1].end_rev == 249

        # 第三个窗口：r250 -> r349
        assert chunks[2].start_rev == 250
        assert chunks[2].end_rev == 349

        # 第四个窗口：r350 -> r350（只有 1 个）
        assert chunks[3].start_rev == 350
        assert chunks[3].end_rev == 350

    def test_chunk_payload_serialization_stable(self):
        """测试 chunk payload 序列化稳定"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)

        chunk = TimeWindowChunk(since=since, until=until, index=0, total=1)

        # 多次生成 payload
        payloads = [
            chunk.to_payload(update_watermark=True, watermark_constraint="monotonic")
            for _ in range(5)
        ]

        # 验证所有 payload 相同
        first_payload = payloads[0]
        for payload in payloads[1:]:
            assert payload == first_payload


class TestWatermarkConstraintBehavior:
    """Watermark 约束行为测试 - 验证 strict/best_effort 模式行为"""

    def test_watermark_constraint_monotonic_when_update_enabled(self):
        """测试启用 watermark 更新时约束为 monotonic"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)

        chunk = TimeWindowChunk(since=since, until=until, index=0, total=1)

        # update_watermark=True 时应使用 monotonic 约束
        payload = chunk.to_payload(update_watermark=True, watermark_constraint="monotonic")

        assert payload["update_watermark"] is True
        assert payload["watermark_constraint"] == "monotonic"

    def test_watermark_constraint_none_when_update_disabled(self):
        """测试禁用 watermark 更新时约束为 none"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)

        chunk = TimeWindowChunk(since=since, until=until, index=0, total=1)

        # update_watermark=False 时应使用 none 约束
        payload = chunk.to_payload(update_watermark=False, watermark_constraint="none")

        assert payload["update_watermark"] is False
        assert payload["watermark_constraint"] == "none"

    def test_strict_mode_watermark_behavior(self):
        """测试 strict 模式下的 watermark 行为

        strict 模式下：
        - watermark 只能前进，不能回退（monotonic）
        - 遇到不可恢复错误时停止，不更新 watermark
        """
        # 正常前进：允许
        validate_watermark_constraint(
            watermark_before="2025-01-27T10:00:00Z",
            watermark_after="2025-01-27T12:00:00Z",
            update_watermark=True,  # strict 模式
        )

        # 回退：禁止
        with pytest.raises(WatermarkConstraintError):
            validate_watermark_constraint(
                watermark_before="2025-01-27T12:00:00Z",
                watermark_after="2025-01-27T10:00:00Z",
                update_watermark=True,  # strict 模式
            )

    def test_best_effort_mode_watermark_behavior(self):
        """测试 best_effort 模式下的 watermark 行为

        best_effort 模式下：
        - 不更新 watermark（update_watermark=False）
        - 不检查回退约束
        - 允许继续处理即使有错误
        """
        # best_effort 模式下，即使 watermark 回退也不报错
        validate_watermark_constraint(
            watermark_before="2025-01-27T12:00:00Z",
            watermark_after="2025-01-27T08:00:00Z",
            update_watermark=False,  # best_effort 模式
        )

    def test_chunk_payloads_reflect_watermark_strategy(self):
        """测试所有 chunk 的 payload 都反映正确的 watermark 策略"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # 12 小时

        chunks = split_time_window(since, until, chunk_hours=4)

        # 生成 payloads（模拟 update_watermark=True 场景）
        payloads = [
            chunk.to_payload(update_watermark=True, watermark_constraint="monotonic")
            for chunk in chunks
        ]

        # 验证所有 payload 都包含正确的 watermark 策略
        assert len(payloads) == 3
        for payload in payloads:
            assert payload["update_watermark"] is True
            assert payload["watermark_constraint"] == "monotonic"

    def test_revision_chunks_watermark_strategy(self):
        """测试 revision chunk 的 watermark 策略"""
        chunks = split_revision_window(1, 300, chunk_size=100)

        # update_watermark=False 场景
        payloads_no_update = [
            chunk.to_payload(update_watermark=False, watermark_constraint="none")
            for chunk in chunks
        ]

        for payload in payloads_no_update:
            assert payload["update_watermark"] is False
            assert payload["watermark_constraint"] == "none"

        # update_watermark=True 场景
        payloads_with_update = [
            chunk.to_payload(update_watermark=True, watermark_constraint="monotonic")
            for chunk in chunks
        ]

        for payload in payloads_with_update:
            assert payload["update_watermark"] is True
            assert payload["watermark_constraint"] == "monotonic"


class TestBackfillMetadataContainsWatermarkInfo:
    """测试回填 metadata 包含 watermark 信息"""

    def test_time_chunks_metadata_structure(self):
        """测试时间窗口回填 metadata 结构完整性"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc)

        chunks = split_time_window(since, until, chunk_hours=4)

        # 模拟 metadata 结构
        update_watermark = True
        watermark_constraint = "monotonic" if update_watermark else "none"

        chunk_payloads = [
            chunk.to_payload(
                update_watermark=update_watermark,
                watermark_constraint=watermark_constraint,
            )
            for chunk in chunks
        ]

        metadata = {
            "window_type": "time",
            "since_time": since.isoformat(),
            "until_time": until.isoformat(),
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
            "chunk_count": len(chunks),
            "chunk_hours": 4,
            "chunk_payloads": chunk_payloads,
            "cursor_before": {
                "since": since.isoformat(),
                "window_type": "time",
            },
        }

        # 验证 metadata 包含所有必需字段
        assert metadata["window_type"] == "time"
        assert metadata["since_time"] == since.isoformat()
        assert metadata["until_time"] == until.isoformat()
        assert metadata["update_watermark"] is True
        assert metadata["watermark_constraint"] == "monotonic"
        assert metadata["chunk_count"] == 2
        assert len(metadata["chunk_payloads"]) == 2
        assert "cursor_before" in metadata

    def test_revision_chunks_metadata_structure(self):
        """测试 revision 窗口回填 metadata 结构完整性"""
        start_rev = 100
        end_rev = 300

        chunks = split_revision_window(start_rev, end_rev, chunk_size=100)

        # 模拟 metadata 结构
        update_watermark = False
        watermark_constraint = "none"

        chunk_payloads = [
            chunk.to_payload(
                update_watermark=update_watermark,
                watermark_constraint=watermark_constraint,
            )
            for chunk in chunks
        ]

        metadata = {
            "window_type": "revision",
            "start_rev": start_rev,
            "end_rev": end_rev,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
            "chunk_count": len(chunks),
            "chunk_revs": 100,
            "chunk_payloads": chunk_payloads,
            "cursor_before": {
                "start_rev": start_rev,
                "window_type": "revision",
            },
        }

        # 验证 metadata 包含所有必需字段
        assert metadata["window_type"] == "revision"
        assert metadata["start_rev"] == 100
        assert metadata["end_rev"] == 300
        assert metadata["update_watermark"] is False
        assert metadata["watermark_constraint"] == "none"
        assert metadata["chunk_count"] == 3
        assert len(metadata["chunk_payloads"]) == 3
        assert "cursor_before" in metadata


class TestBackfillWindowLimits:
    """回填窗口限制测试 - 验证超限拒绝与边界值通过"""

    def test_validate_backfill_window_within_limits(self):
        """测试窗口在限制内时通过校验"""
        from engram.logbook.config import (
            validate_backfill_window,
        )

        # 在限制范围内，不应抛出异常
        validate_backfill_window(
            total_window_seconds=86400,  # 1 天
            chunk_count=10,
            config=None,
        )

    def test_validate_backfill_window_at_boundary(self):
        """测试窗口正好等于限制值时通过（边界值测试）"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            validate_backfill_window,
        )

        # 正好等于限制值，应该通过
        validate_backfill_window(
            total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            config=None,
        )

    def test_validate_backfill_window_exceeds_window_seconds(self):
        """测试窗口秒数超限被拒绝"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        # 超过窗口秒数限制
        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS + 1,
                chunk_count=10,
                config=None,
            )

        # 验证错误类型
        assert exc_info.value.error_type == "BACKFILL_WINDOW_EXCEEDED"

        # 验证结构化错误详情
        details = exc_info.value.details
        assert "errors" in details
        assert len(details["errors"]) >= 1
        assert details["errors"][0]["constraint"] == "max_total_window_seconds"
        assert details["errors"][0]["limit"] == DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS
        assert details["errors"][0]["actual"] == DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS + 1

    def test_validate_backfill_window_exceeds_chunks(self):
        """测试 chunk 数量超限被拒绝"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        # 超过 chunk 数量限制
        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=86400,  # 1 天（在限制内）
                chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST + 1,
                config=None,
            )

        # 验证错误类型
        assert exc_info.value.error_type == "BACKFILL_WINDOW_EXCEEDED"

        details = exc_info.value.details
        assert len(details["errors"]) >= 1

        # 找到 chunk 限制相关的错误
        chunk_error = next(
            (e for e in details["errors"] if e["constraint"] == "max_chunks_per_request"), None
        )
        assert chunk_error is not None
        assert chunk_error["limit"] == DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST
        assert chunk_error["actual"] == DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST + 1

    def test_validate_backfill_window_exceeds_both_limits(self):
        """测试同时超过两个限制时包含两个错误"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        # 同时超过两个限制
        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS + 86400,
                chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST + 50,
                config=None,
            )

        # 验证包含两个错误
        details = exc_info.value.details
        assert len(details["errors"]) == 2

        constraints = {e["constraint"] for e in details["errors"]}
        assert "max_total_window_seconds" in constraints
        assert "max_chunks_per_request" in constraints

    def test_backfill_window_exceeded_error_to_dict(self):
        """测试 BackfillWindowExceededError.to_dict() 结构化输出"""
        from engram.logbook.config import BackfillWindowExceededError

        error = BackfillWindowExceededError(
            "测试错误消息",
            details={
                "errors": [{"constraint": "max_total_window_seconds", "limit": 100, "actual": 200}],
                "total_window_seconds": 200,
                "chunk_count": 5,
            },
        )

        error_dict = error.to_dict()

        assert error_dict["error_type"] == "BACKFILL_WINDOW_EXCEEDED"
        assert error_dict["message"] == "测试错误消息"
        assert "details" in error_dict
        assert error_dict["details"]["total_window_seconds"] == 200
        assert error_dict["details"]["chunk_count"] == 5

    def test_get_backfill_config_contains_new_fields(self):
        """测试 get_backfill_config 包含新增的限制配置"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            get_backfill_config,
        )

        config = get_backfill_config(None)

        assert "max_total_window_seconds" in config
        assert "max_chunks_per_request" in config
        assert config["max_total_window_seconds"] == DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS
        assert config["max_chunks_per_request"] == DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST

    @patch("engram_logbook.config.get_config")
    def test_get_backfill_config_custom_limits(self, mock_get_config):
        """测试自定义限制配置读取"""
        from engram.logbook.config import get_backfill_config

        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "scm.backfill.repair_window_hours": 24,
            "scm.backfill.cron_hint": "0 2 * * *",
            "scm.backfill.max_concurrent_jobs": 4,
            "scm.backfill.default_update_watermark": False,
            "scm.backfill.max_total_window_seconds": 604800,  # 7 天
            "scm.backfill.max_chunks_per_request": 50,
        }.get(key, default)
        mock_get_config.return_value = mock_config

        config = get_backfill_config(mock_config)

        assert config["max_total_window_seconds"] == 604800
        assert config["max_chunks_per_request"] == 50

    @patch("engram_logbook.config.get_backfill_config")
    def test_validate_with_custom_config_limits(self, mock_get_backfill_config):
        """测试使用自定义配置限制进行校验"""
        from engram.logbook.config import BackfillWindowExceededError, validate_backfill_window

        # 设置较小的限制
        mock_get_backfill_config.return_value = {
            "max_total_window_seconds": 3600,  # 1 小时
            "max_chunks_per_request": 5,
        }

        # 超过自定义限制
        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=7200,  # 2 小时
                chunk_count=3,
                config=None,
            )

        details = exc_info.value.details
        assert details["limits"]["max_total_window_seconds"] == 3600


class TestBackfillWindowValidationInRunner:
    """Runner 中回填窗口校验集成测试"""

    def test_runner_context_default_values(self):
        """测试 RunnerContext 默认值"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
        )

        # 验证有默认的窗口切分配置
        assert ctx.window_chunk_hours == DEFAULT_WINDOW_CHUNK_HOURS
        assert ctx.window_chunk_revs == DEFAULT_WINDOW_CHUNK_REVS

    def test_time_window_exceeds_limit_in_split(self):
        """测试时间窗口切分后 chunk 数量可能超限"""
        # 31 天窗口，每 4 小时一个 chunk
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc)  # 31 天

        chunks = split_time_window(since, until, chunk_hours=4)

        # 31 天 = 744 小时，744 / 4 = 186 个 chunks
        assert len(chunks) > 100  # 超过默认的 100 个限制

        # 这种情况应该被 validate_backfill_window 拒绝
        from engram.logbook.config import (
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        total_window_seconds = int((until - since).total_seconds())

        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=total_window_seconds,
                chunk_count=len(chunks),
                config=None,
            )

        # 可能同时超过 window_seconds 和 chunk_count 限制
        assert len(exc_info.value.details["errors"]) >= 1

    def test_revision_window_exceeds_limit_in_split(self):
        """测试 revision 窗口切分后 chunk 数量可能超限"""
        # 大范围 revision 回填
        chunks = split_revision_window(1, 20000, chunk_size=100)

        # 20000 / 100 = 200 个 chunks
        assert len(chunks) == 200
        assert len(chunks) > 100  # 超过默认限制

        # 这种情况应该被校验拒绝
        from engram.logbook.config import (
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        # SVN 使用估算的秒数
        estimated_seconds = len(chunks) * 100 * 3600  # 估算

        with pytest.raises(BackfillWindowExceededError):
            validate_backfill_window(
                total_window_seconds=estimated_seconds,
                chunk_count=len(chunks),
                config=None,
            )


class TestBackfillWindowEdgeCases:
    """回填窗口边界情况测试"""

    def test_zero_window_seconds_allowed(self):
        """测试零秒窗口是允许的（空回填）"""
        from engram.logbook.config import validate_backfill_window

        # 零秒窗口应该通过
        validate_backfill_window(
            total_window_seconds=0,
            chunk_count=0,
            config=None,
        )

    def test_single_chunk_allowed(self):
        """测试单个 chunk 是允许的"""
        from engram.logbook.config import validate_backfill_window

        validate_backfill_window(
            total_window_seconds=3600,  # 1 小时
            chunk_count=1,
            config=None,
        )

    def test_exactly_max_window_seconds_minus_one(self):
        """测试正好小于限制 1 秒时通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            validate_backfill_window,
        )

        validate_backfill_window(
            total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS - 1,
            chunk_count=10,
            config=None,
        )

    def test_exactly_max_chunks_minus_one(self):
        """测试正好小于 chunk 限制 1 时通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            validate_backfill_window,
        )

        validate_backfill_window(
            total_window_seconds=86400,
            chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST - 1,
            config=None,
        )

    def test_error_details_contains_limits_info(self):
        """测试错误详情包含限制信息"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS * 2,
                chunk_count=10,
                config=None,
            )

        details = exc_info.value.details

        # 验证 limits 信息
        assert "limits" in details
        assert (
            details["limits"]["max_total_window_seconds"]
            == DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS
        )
        assert (
            details["limits"]["max_chunks_per_request"] == DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST
        )

        # 验证实际值
        assert details["total_window_seconds"] == DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS * 2
        assert details["chunk_count"] == 10


class TestTimeWindowExtremesAndOffByOne:
    """时间窗口极值与 off-by-one 测试"""

    def test_time_window_since_equals_until(self):
        """测试 since == until（零窗口）应返回空列表"""
        since = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        chunks = split_time_window(since, until, chunk_hours=4)

        assert len(chunks) == 0

    def test_time_window_one_second_difference(self):
        """测试 since 与 until 相差 1 秒应返回 1 个 chunk"""
        since = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 12, 0, 1, tzinfo=timezone.utc)  # +1 秒

        chunks = split_time_window(since, until, chunk_hours=4)

        assert len(chunks) == 1
        assert chunks[0].since == since
        assert chunks[0].until == until

    def test_time_window_exactly_chunk_hours(self):
        """测试窗口正好等于 chunk_hours 应返回 1 个 chunk"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)  # 正好 4 小时

        chunks = split_time_window(since, until, chunk_hours=4)

        assert len(chunks) == 1
        assert chunks[0].since == since
        assert chunks[0].until == until

    def test_time_window_chunk_hours_plus_one_second(self):
        """测试窗口比 chunk_hours 多 1 秒应返回 2 个 chunks"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 4, 0, 1, tzinfo=timezone.utc)  # 4 小时 + 1 秒

        chunks = split_time_window(since, until, chunk_hours=4)

        assert len(chunks) == 2
        # 第一个 chunk: 00:00 -> 04:00
        assert chunks[0].since == since
        assert chunks[0].until == datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)
        # 第二个 chunk: 04:00 -> 04:00:01
        assert chunks[1].since == datetime(2025, 1, 1, 4, 0, 0, tzinfo=timezone.utc)
        assert chunks[1].until == until

    def test_time_window_chunk_hours_minus_one_second(self):
        """测试窗口比 chunk_hours 少 1 秒应返回 1 个 chunk"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 3, 59, 59, tzinfo=timezone.utc)  # 4 小时 - 1 秒

        chunks = split_time_window(since, until, chunk_hours=4)

        assert len(chunks) == 1
        assert chunks[0].since == since
        assert chunks[0].until == until

    def test_time_window_since_greater_than_until(self):
        """测试 since > until 应返回空列表"""
        since = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        chunks = split_time_window(since, until, chunk_hours=4)

        assert len(chunks) == 0

    def test_time_window_very_small_chunk_hours(self):
        """测试非常小的 chunk_hours（1 小时）"""
        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 3, 30, 0, tzinfo=timezone.utc)  # 3.5 小时

        chunks = split_time_window(since, until, chunk_hours=1)

        # 3.5 小时 / 1 小时 = 4 个 chunks
        assert len(chunks) == 4
        # 验证最后一个 chunk 只有 30 分钟
        last_chunk_duration = (chunks[-1].until - chunks[-1].since).total_seconds()
        assert last_chunk_duration == 30 * 60  # 30 分钟


class TestRevisionWindowExtremesAndOffByOne:
    """Revision 窗口极值与 off-by-one 测试"""

    def test_revision_window_single_revision(self):
        """测试 start_rev == end_rev（单个 revision）应返回 1 个 chunk"""
        chunks = split_revision_window(100, 100, chunk_size=100)

        assert len(chunks) == 1
        assert chunks[0].start_rev == 100
        assert chunks[0].end_rev == 100

    def test_revision_window_start_greater_than_end(self):
        """测试 start_rev > end_rev 应返回空列表"""
        chunks = split_revision_window(200, 100, chunk_size=100)

        assert len(chunks) == 0

    def test_revision_window_exactly_chunk_size(self):
        """测试 revision 数量正好等于 chunk_size 应返回 1 个 chunk"""
        # 100 个 revisions (1-100)
        chunks = split_revision_window(1, 100, chunk_size=100)

        assert len(chunks) == 1
        assert chunks[0].start_rev == 1
        assert chunks[0].end_rev == 100

    def test_revision_window_chunk_size_plus_one(self):
        """测试 revision 数量比 chunk_size 多 1 应返回 2 个 chunks"""
        # 101 个 revisions (1-101)
        chunks = split_revision_window(1, 101, chunk_size=100)

        assert len(chunks) == 2
        # 第一个 chunk: 1-100
        assert chunks[0].start_rev == 1
        assert chunks[0].end_rev == 100
        # 第二个 chunk: 101-101
        assert chunks[1].start_rev == 101
        assert chunks[1].end_rev == 101

    def test_revision_window_chunk_size_minus_one(self):
        """测试 revision 数量比 chunk_size 少 1 应返回 1 个 chunk"""
        # 99 个 revisions (1-99)
        chunks = split_revision_window(1, 99, chunk_size=100)

        assert len(chunks) == 1
        assert chunks[0].start_rev == 1
        assert chunks[0].end_rev == 99

    def test_revision_window_two_revisions(self):
        """测试只有 2 个 revision"""
        chunks = split_revision_window(50, 51, chunk_size=100)

        assert len(chunks) == 1
        assert chunks[0].start_rev == 50
        assert chunks[0].end_rev == 51

    def test_revision_window_large_chunk_size(self):
        """测试 chunk_size 大于实际 revision 范围"""
        # 50 个 revisions，但 chunk_size 是 1000
        chunks = split_revision_window(1, 50, chunk_size=1000)

        assert len(chunks) == 1
        assert chunks[0].start_rev == 1
        assert chunks[0].end_rev == 50


class TestEstimateSvnWindowSeconds:
    """SVN 窗口秒数估算函数测试"""

    def test_estimate_zero_revisions(self):
        """测试 0 个 revision 返回 0 秒"""
        from engram.logbook.config import estimate_svn_window_seconds

        assert estimate_svn_window_seconds(0) == 0

    def test_estimate_negative_revisions(self):
        """测试负数 revision 返回 0 秒"""
        from engram.logbook.config import estimate_svn_window_seconds

        assert estimate_svn_window_seconds(-10) == 0

    def test_estimate_single_revision(self):
        """测试单个 revision 返回 1 小时（默认）"""
        from engram.logbook.config import DEFAULT_SVN_SECONDS_PER_REV, estimate_svn_window_seconds

        result = estimate_svn_window_seconds(1)

        assert result == DEFAULT_SVN_SECONDS_PER_REV
        assert result == 3600  # 默认 1 小时

    def test_estimate_multiple_revisions(self):
        """测试多个 revision 的估算"""
        from engram.logbook.config import estimate_svn_window_seconds

        result = estimate_svn_window_seconds(100)

        assert result == 100 * 3600  # 100 小时

    def test_estimate_custom_seconds_per_rev(self):
        """测试自定义每 revision 秒数"""
        from engram.logbook.config import estimate_svn_window_seconds

        # 假设每 revision 平均 30 分钟
        result = estimate_svn_window_seconds(10, seconds_per_rev=1800)

        assert result == 10 * 1800  # 5 小时


class TestBackfillWindowValidationOffByOne:
    """回填窗口校验边界值 off-by-one 测试"""

    def test_window_seconds_exactly_at_limit(self):
        """测试窗口秒数正好等于限制值应通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            validate_backfill_window,
        )

        # 正好等于限制，应该通过
        validate_backfill_window(
            total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            chunk_count=10,
            config=None,
        )

    def test_window_seconds_one_over_limit(self):
        """测试窗口秒数超过限制 1 秒应被拒绝"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS + 1,
                chunk_count=10,
                config=None,
            )

        assert (
            exc_info.value.details["errors"][0]["actual"]
            == DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS + 1
        )

    def test_window_seconds_one_under_limit(self):
        """测试窗口秒数少于限制 1 秒应通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            validate_backfill_window,
        )

        validate_backfill_window(
            total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS - 1,
            chunk_count=10,
            config=None,
        )

    def test_chunk_count_exactly_at_limit(self):
        """测试 chunk 数量正好等于限制值应通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            validate_backfill_window,
        )

        validate_backfill_window(
            total_window_seconds=86400,
            chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            config=None,
        )

    def test_chunk_count_one_over_limit(self):
        """测试 chunk 数量超过限制 1 个应被拒绝"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            BackfillWindowExceededError,
            validate_backfill_window,
        )

        with pytest.raises(BackfillWindowExceededError) as exc_info:
            validate_backfill_window(
                total_window_seconds=86400,
                chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST + 1,
                config=None,
            )

        # 找到 chunk 限制相关的错误
        chunk_error = next(
            (
                e
                for e in exc_info.value.details["errors"]
                if e["constraint"] == "max_chunks_per_request"
            ),
            None,
        )
        assert chunk_error is not None
        assert chunk_error["actual"] == DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST + 1

    def test_chunk_count_one_under_limit(self):
        """测试 chunk 数量少于限制 1 个应通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            validate_backfill_window,
        )

        validate_backfill_window(
            total_window_seconds=86400,
            chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST - 1,
            config=None,
        )

    def test_both_limits_exactly_at_boundary(self):
        """测试两个限制同时正好等于边界值应通过"""
        from engram.logbook.config import (
            DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            validate_backfill_window,
        )

        validate_backfill_window(
            total_window_seconds=DEFAULT_BACKFILL_MAX_TOTAL_WINDOW_SECONDS,
            chunk_count=DEFAULT_BACKFILL_MAX_CHUNKS_PER_REQUEST,
            config=None,
        )

    def test_zero_values_allowed(self):
        """测试零值应通过（空回填）"""
        from engram.logbook.config import validate_backfill_window

        validate_backfill_window(
            total_window_seconds=0,
            chunk_count=0,
            config=None,
        )


class TestExitCodes:
    """返回码测试"""

    def test_exit_code_constants(self):
        """测试返回码常量"""
        assert EXIT_SUCCESS == 0
        assert EXIT_PARTIAL == 1
        assert EXIT_FAILED == 2

    def test_get_exit_code_success(self):
        """测试成功状态返回码"""
        assert get_exit_code(RunnerStatus.SUCCESS.value) == EXIT_SUCCESS

    def test_get_exit_code_partial(self):
        """测试部分成功状态返回码"""
        assert get_exit_code(RunnerStatus.PARTIAL.value) == EXIT_PARTIAL

    def test_get_exit_code_failed(self):
        """测试失败状态返回码"""
        assert get_exit_code(RunnerStatus.FAILED.value) == EXIT_FAILED

    def test_get_exit_code_skipped(self):
        """测试跳过状态返回码（应视为失败）"""
        assert get_exit_code(RunnerStatus.SKIPPED.value) == EXIT_FAILED

    def test_get_exit_code_cancelled(self):
        """测试取消状态返回码（应视为失败）"""
        assert get_exit_code(RunnerStatus.CANCELLED.value) == EXIT_FAILED

    def test_get_exit_code_unknown(self):
        """测试未知状态返回码（应视为失败）"""
        assert get_exit_code("unknown_status") == EXIT_FAILED


class TestAggregatedResult:
    """聚合结果测试"""

    def test_default_values(self):
        """测试默认值"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
        )
        assert result.phase == "backfill"
        assert result.repo == "gitlab:123"
        assert result.status == RunnerStatus.SUCCESS.value
        assert result.total_chunks == 0
        assert result.success_chunks == 0
        assert result.failed_chunks == 0
        assert result.total_items_synced == 0
        assert result.errors == []

    def test_compute_status_all_success(self):
        """测试全部成功时计算状态"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
            total_chunks=3,
            success_chunks=3,
            failed_chunks=0,
        )
        assert result.compute_status() == RunnerStatus.SUCCESS.value

    def test_compute_status_all_failed(self):
        """测试全部失败时计算状态"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
            total_chunks=3,
            success_chunks=0,
            failed_chunks=3,
        )
        assert result.compute_status() == RunnerStatus.FAILED.value

    def test_compute_status_partial(self):
        """测试部分成功时计算状态"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
            total_chunks=3,
            success_chunks=2,
            failed_chunks=1,
        )
        assert result.compute_status() == RunnerStatus.PARTIAL.value

    def test_compute_status_with_partial_chunks(self):
        """测试有 partial_chunks 时计算状态"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
            total_chunks=3,
            success_chunks=2,
            partial_chunks=1,
            failed_chunks=0,
        )
        assert result.compute_status() == RunnerStatus.PARTIAL.value

    def test_compute_status_empty(self):
        """测试空结果时计算状态"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
            total_chunks=0,
        )
        assert result.compute_status() == RunnerStatus.SKIPPED.value

    def test_to_dict(self):
        """测试转换为字典"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
            job="commits",
            total_chunks=2,
            success_chunks=1,
            failed_chunks=1,
            total_items_synced=100,
            errors=["error1"],
        )
        data = result.to_dict()

        assert data["phase"] == "backfill"
        assert data["repo"] == "gitlab:123"
        assert data["job"] == "commits"
        assert data["total_chunks"] == 2
        assert data["success_chunks"] == 1
        assert data["failed_chunks"] == 1
        assert data["total_items_synced"] == 100
        assert data["errors"] == ["error1"]

    def test_to_json(self):
        """测试 JSON 序列化"""
        result = AggregatedResult(
            phase="backfill",
            repo="gitlab:123",
        )
        json_str = result.to_json()
        data = json.loads(json_str)

        assert data["phase"] == "backfill"
        assert data["repo"] == "gitlab:123"


class TestSyncRunnerDryRun:
    """SyncRunner dry_run 模式测试"""

    def test_run_incremental_dry_run(self):
        """测试增量同步 dry_run 模式"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            dry_run=True,
        )

        runner = SyncRunner(ctx)
        result = runner.run_incremental()

        assert result.status == RunnerStatus.SUCCESS.value
        assert result.items_synced == 0
        assert result.repo == "gitlab:123"

    def test_run_backfill_dry_run(self):
        """测试回填同步 dry_run 模式"""
        mock_config = MagicMock()
        mock_config.get.return_value = None
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            dry_run=True,
        )

        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc)

        runner = SyncRunner(ctx)
        result = runner.run_backfill(since=since, until=until)

        assert result.phase == "backfill"
        assert result.repo == "gitlab:123"
        assert result.total_chunks == 2  # 8 小时 / 4 小时 = 2 chunks
        assert result.success_chunks == 2


class TestSyncRunnerBackfillChunks:
    """SyncRunner 回填分片测试"""

    def test_generate_time_chunks(self):
        """测试生成时间窗口 chunks"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            window_chunk_hours=4,
        )

        runner = SyncRunner(ctx)

        since = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        until = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        chunks = runner._generate_time_chunks(since, until)

        assert len(chunks) == 3
        assert chunks[0].since == since
        assert chunks[-1].until == until

    def test_generate_revision_chunks(self):
        """测试生成版本窗口 chunks"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("svn:https://svn.example.com/repo")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            window_chunk_revs=100,
        )

        runner = SyncRunner(ctx)

        chunks = runner._generate_revision_chunks(1, 250)

        assert len(chunks) == 3
        assert chunks[0].start_rev == 1
        assert chunks[-1].end_rev == 250


class TestSyncRunnerJobDictBuild:
    """SyncRunner job 字典构建测试"""

    def test_build_job_dict_gitlab_commits(self):
        """测试构建 GitLab commits job 字典"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")
        job = JobSpec.parse("commits")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            job=job,
        )

        runner = SyncRunner(ctx)
        job_dict = runner._build_job_dict(mode="incremental")

        assert job_dict["job_type"] == "gitlab_commits"
        assert job_dict["repo_id"] == 123
        assert job_dict["mode"] == "incremental"
        assert job_dict["payload"]["repo_type"] == "gitlab"

    def test_build_job_dict_gitlab_mrs(self):
        """测试构建 GitLab MRs job 字典"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:456")
        job = JobSpec.parse("mrs")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            job=job,
        )

        runner = SyncRunner(ctx)
        job_dict = runner._build_job_dict(mode="incremental")

        assert job_dict["job_type"] == "gitlab_mrs"
        assert job_dict["repo_id"] == 456

    def test_build_job_dict_svn(self):
        """测试构建 SVN job 字典"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("svn:https://svn.example.com/repo")
        job = JobSpec.parse("commits")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            job=job,
        )

        runner = SyncRunner(ctx)
        job_dict = runner._build_job_dict(mode="backfill")

        assert job_dict["job_type"] == "svn"
        assert job_dict["mode"] == "backfill"
        assert job_dict["payload"]["repo_type"] == "svn"

    def test_build_job_dict_with_payload(self):
        """测试构建带额外 payload 的 job 字典"""
        mock_config = MagicMock()
        repo = RepoSpec.parse("gitlab:123")

        ctx = RunnerContext(
            config=mock_config,
            repo=repo,
            dry_run=True,
            verbose=True,
        )

        runner = SyncRunner(ctx)
        job_dict = runner._build_job_dict(
            mode="backfill",
            payload={"since": "2025-01-01T00:00:00Z"},
        )

        assert job_dict["payload"]["dry_run"] is True
        assert job_dict["payload"]["verbose"] is True
        assert job_dict["payload"]["since"] == "2025-01-01T00:00:00Z"


class TestRunnerMainExitCodes:
    """runner_main 返回码测试"""

    @patch("engram.logbook.scm_sync_runner.SyncRunner")
    @patch("engram.logbook.config.get_config")
    def test_incremental_success_exit_code(self, mock_get_config, mock_runner_class):
        """测试增量同步成功返回码"""
        from engram.logbook.cli.scm_sync import runner_main

        mock_config = MagicMock()
        mock_get_config.return_value = mock_config

        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.to_dict.return_value = {"status": "success", "items_synced": 10}
        mock_result.to_json.return_value = '{"status": "success"}'
        mock_result.repo = "gitlab:123"
        mock_result.job = "commits"
        mock_result.items_synced = 10
        mock_result.error = None
        mock_result.vfacts_refreshed = False

        mock_runner = MagicMock()
        mock_runner.run_incremental.return_value = mock_result
        mock_runner_class.return_value = mock_runner

        exit_code = runner_main(["incremental", "--repo", "gitlab:123"])

        assert exit_code == 0  # EXIT_SUCCESS

    @patch("engram.logbook.scm_sync_runner.SyncRunner")
    @patch("engram.logbook.config.get_config")
    def test_incremental_failed_exit_code(self, mock_get_config, mock_runner_class):
        """测试增量同步失败返回码"""
        from engram.logbook.cli.scm_sync import runner_main

        mock_config = MagicMock()
        mock_get_config.return_value = mock_config

        mock_result = MagicMock()
        mock_result.status = "failed"
        mock_result.to_dict.return_value = {"status": "failed", "error": "test error"}
        mock_result.repo = "gitlab:123"
        mock_result.job = "commits"
        mock_result.items_synced = 0
        mock_result.error = "test error"
        mock_result.vfacts_refreshed = False

        mock_runner = MagicMock()
        mock_runner.run_incremental.return_value = mock_result
        mock_runner_class.return_value = mock_runner

        exit_code = runner_main(["incremental", "--repo", "gitlab:123"])

        assert exit_code == 2  # EXIT_FAILED

    @patch("engram.logbook.scm_sync_runner.SyncRunner")
    @patch("engram.logbook.config.get_config")
    def test_backfill_partial_exit_code(self, mock_get_config, mock_runner_class):
        """测试回填部分成功返回码"""
        from engram.logbook.cli.scm_sync import runner_main

        mock_config = MagicMock()
        mock_get_config.return_value = mock_config

        mock_result = MagicMock()
        mock_result.status = "partial"
        mock_result.to_dict.return_value = {
            "status": "partial",
            "total_chunks": 3,
            "success_chunks": 2,
            "failed_chunks": 1,
        }
        mock_result.repo = "gitlab:123"
        mock_result.job = "commits"
        mock_result.total_chunks = 3
        mock_result.success_chunks = 2
        mock_result.partial_chunks = 0
        mock_result.failed_chunks = 1
        mock_result.total_items_synced = 100
        mock_result.total_items_skipped = 0
        mock_result.total_items_failed = 10
        mock_result.watermark_updated = False
        mock_result.vfacts_refreshed = False
        mock_result.errors = ["chunk 3 failed"]

        mock_runner = MagicMock()
        mock_runner.run_backfill.return_value = mock_result
        mock_runner_class.return_value = mock_runner

        exit_code = runner_main(["backfill", "--repo", "gitlab:123", "--last-hours", "24"])

        assert exit_code == 1  # EXIT_PARTIAL

    def test_invalid_repo_exit_code(self):
        """测试无效仓库规格返回码"""
        from engram.logbook.cli.scm_sync import runner_main

        exit_code = runner_main(["incremental", "--repo", "invalid_repo"])

        assert exit_code == 2  # EXIT_FAILED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
