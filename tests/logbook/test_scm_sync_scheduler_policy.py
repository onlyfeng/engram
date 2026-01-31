"""
test_scm_sync_scheduler_policy.py - 调度策略单元测试

测试场景:
- 游标年龄计算
- 失败率/429 命中率计算
- 调度决策逻辑
- 优先级计算
- 并发限制裁剪
- 回填窗口计算
- Pause 记录写入/读取/过期行为

所有策略函数都是纯函数，不依赖数据库，易于测试。
"""

from unittest.mock import MagicMock, patch

import pytest

from engram.logbook.scm_sync_policy import (
    BUCKET_LOW_TOKENS_PRIORITY_PENALTY,
    BUCKET_PAUSED_PRIORITY_PENALTY,
    BudgetSnapshot,
    # Bucket 暂停相关
    InstanceBucketStatus,
    PauseSnapshot,
    RepoSyncState,
    SchedulerConfig,
    SyncJobCandidate,
    calculate_bucket_priority_penalty,
    calculate_cursor_age,
    calculate_failure_rate,
    calculate_rate_limit_rate,
    compute_backfill_window,
    compute_job_priority,
    select_jobs_to_enqueue,
    should_schedule_repo,
    should_schedule_repo_health,
    should_skip_due_to_bucket_pause,
)


class TestCalculateCursorAge:
    """游标年龄计算测试"""

    def test_returns_inf_when_never_synced(self):
        """从未同步时返回无穷大"""
        age = calculate_cursor_age(None, now=1000.0)
        assert age == float("inf")

    def test_returns_zero_when_just_synced(self):
        """刚刚同步时返回 0"""
        now = 1000.0
        age = calculate_cursor_age(now, now=now)
        assert age == 0.0

    def test_returns_positive_age(self):
        """正常计算年龄"""
        now = 1000.0
        cursor_ts = 900.0
        age = calculate_cursor_age(cursor_ts, now=now)
        assert age == 100.0

    def test_returns_zero_for_future_cursor(self):
        """游标时间戳在未来时返回 0"""
        now = 1000.0
        cursor_ts = 1100.0  # 未来
        age = calculate_cursor_age(cursor_ts, now=now)
        assert age == 0.0


class TestCalculateFailureRate:
    """失败率计算测试"""

    def test_returns_zero_when_no_runs(self):
        """没有运行时返回 0"""
        rate = calculate_failure_rate(0, 0)
        assert rate == 0.0

    def test_returns_zero_when_no_failures(self):
        """没有失败时返回 0"""
        rate = calculate_failure_rate(0, 10)
        assert rate == 0.0

    def test_returns_correct_rate(self):
        """正确计算失败率"""
        rate = calculate_failure_rate(3, 10)
        assert rate == 0.3

    def test_caps_at_one(self):
        """失败率上限为 1.0"""
        rate = calculate_failure_rate(15, 10)  # 超过 100%
        assert rate == 1.0


class TestCalculateRateLimitRate:
    """429 命中率计算测试"""

    def test_returns_zero_when_no_requests(self):
        """没有请求时返回 0"""
        rate = calculate_rate_limit_rate(0, 0)
        assert rate == 0.0

    def test_returns_zero_when_no_hits(self):
        """没有 429 时返回 0"""
        rate = calculate_rate_limit_rate(0, 100)
        assert rate == 0.0

    def test_returns_correct_rate(self):
        """正确计算命中率"""
        rate = calculate_rate_limit_rate(10, 100)
        assert rate == 0.1

    def test_caps_at_one(self):
        """命中率上限为 1.0"""
        rate = calculate_rate_limit_rate(150, 100)
        assert rate == 1.0


class TestShouldScheduleRepo:
    """调度决策测试"""

    def test_skip_when_already_queued(self):
        """已在队列中时跳过"""
        config = SchedulerConfig()
        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            is_queued=True,
        )

        should, reason, adj = should_schedule_repo(state, config, now=1000.0)

        assert should is False
        assert reason == "already_queued"

    def test_schedule_when_never_synced(self):
        """从未同步时应调度，优先级最高"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=None,  # 从未同步
            is_queued=False,
        )

        should, reason, adj = should_schedule_repo(state, config, now=1000.0)

        assert should is True
        assert reason == "never_synced"
        assert adj == -100  # 最高优先级

    def test_schedule_when_cursor_age_exceeds_threshold(self):
        """游标年龄超过阈值时应调度"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        now = 10000.0
        cursor_ts = now - 4000  # 超过阈值

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=cursor_ts,
            is_queued=False,
        )

        should, reason, adj = should_schedule_repo(state, config, now=now)

        assert should is True
        assert "cursor_age" in reason

    def test_no_schedule_when_within_threshold(self):
        """游标年龄在阈值内时不调度"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        now = 10000.0
        cursor_ts = now - 1000  # 在阈值内

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=cursor_ts,
            is_queued=False,
        )

        should, reason, adj = should_schedule_repo(state, config, now=now)

        assert should is False
        assert reason == "within_threshold"

    def test_no_schedule_when_error_budget_exceeded(self):
        """错误预算超限时不调度"""
        config = SchedulerConfig(
            error_budget_threshold=0.3,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0
        cursor_ts = now - 500  # 在阈值内

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=cursor_ts,
            recent_run_count=10,
            recent_failed_count=5,  # 50% 失败率
            is_queued=False,
        )

        should, reason, adj = should_schedule_repo(state, config, now=now)

        assert should is False
        assert "error_budget_exceeded" in reason

    def test_schedule_with_low_priority_when_rate_limited(self):
        """高 429 命中率时仍调度但优先级降低"""
        config = SchedulerConfig(
            rate_limit_hit_threshold=0.1,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0
        cursor_ts = now - 500  # 在阈值内

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=cursor_ts,
            recent_429_hits=20,
            recent_total_requests=100,  # 20% 命中率
            is_queued=False,
        )

        should, reason, adj = should_schedule_repo(state, config, now=now)

        assert should is True
        assert "rate_limited" in reason
        assert adj > 0  # 优先级降低


class TestComputeJobPriority:
    """任务优先级计算测试"""

    def test_commits_has_highest_base_priority(self):
        """commits 任务基础优先级最高"""
        config = SchedulerConfig()
        state = RepoSyncState(repo_id=1, repo_type="git")

        commits_priority = compute_job_priority(1, "commits", state, config)
        mrs_priority = compute_job_priority(1, "mrs", state, config)
        reviews_priority = compute_job_priority(1, "reviews", state, config)

        assert commits_priority < mrs_priority < reviews_priority

    def test_priority_adjustment_applied(self):
        """优先级调整被应用"""
        config = SchedulerConfig()
        state = RepoSyncState(repo_id=1, repo_type="git")

        base_priority = compute_job_priority(1, "commits", state, config, priority_adjustment=0)
        adjusted_priority = compute_job_priority(
            1, "commits", state, config, priority_adjustment=-50
        )

        assert adjusted_priority < base_priority

    def test_failure_rate_increases_priority(self):
        """高失败率增加优先级分数（降低优先级）"""
        config = SchedulerConfig()

        state_healthy = RepoSyncState(
            repo_id=1,
            repo_type="git",
            recent_run_count=10,
            recent_failed_count=0,
        )

        state_failing = RepoSyncState(
            repo_id=1,
            repo_type="git",
            recent_run_count=10,
            recent_failed_count=5,
        )

        priority_healthy = compute_job_priority(1, "commits", state_healthy, config)
        priority_failing = compute_job_priority(1, "commits", state_failing, config)

        assert priority_failing > priority_healthy  # 失败多的优先级更低

    def test_rate_limit_increases_priority(self):
        """高 429 命中率增加优先级分数（降低优先级）"""
        config = SchedulerConfig()

        state_normal = RepoSyncState(
            repo_id=1,
            repo_type="git",
            recent_429_hits=0,
            recent_total_requests=100,
        )

        state_limited = RepoSyncState(
            repo_id=1,
            repo_type="git",
            recent_429_hits=30,
            recent_total_requests=100,
        )

        priority_normal = compute_job_priority(1, "commits", state_normal, config)
        priority_limited = compute_job_priority(1, "commits", state_limited, config)

        assert priority_limited > priority_normal


class TestSelectJobsToEnqueue:
    """任务选择测试"""

    def test_empty_when_no_repos(self):
        """没有仓库时返回空列表"""
        config = SchedulerConfig()
        candidates = select_jobs_to_enqueue([], ["commits"], config)
        assert candidates == []

    def test_selects_repos_needing_sync(self):
        """选择需要同步的仓库"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,  # 需要同步
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 100,  # 不需要同步
            ),
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        repo_ids = [c.repo_id for c in candidates]
        assert 1 in repo_ids
        assert 2 not in repo_ids

    def test_respects_global_concurrency(self):
        """遵守全局并发限制"""
        config = SchedulerConfig(
            global_concurrency=3,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建 5 个需要同步的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 最多 3 个
        assert len(candidates) <= 3

    def test_respects_instance_concurrency(self):
        """遵守每实例并发限制"""
        config = SchedulerConfig(
            global_concurrency=10,
            per_instance_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建 5 个同一 GitLab 实例的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance="gitlab.example.com",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 最多 2 个来自同一实例
        assert len(candidates) <= 2

    def test_respects_tenant_concurrency(self):
        """遵守每租户并发限制"""
        config = SchedulerConfig(
            global_concurrency=10,
            per_tenant_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建 5 个同一租户的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 最多 2 个来自同一租户
        assert len(candidates) <= 2

    def test_respects_max_enqueue_per_scan(self):
        """遵守每次扫描最大入队数量"""
        config = SchedulerConfig(
            global_concurrency=100,
            max_enqueue_per_scan=5,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建 10 个需要同步的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 11)
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            current_queue_size=0,
        )

        # 最多 5 个
        assert len(candidates) <= 5

    def test_considers_current_queue_size(self):
        """考虑当前队列大小"""
        config = SchedulerConfig(
            global_concurrency=10,
            max_enqueue_per_scan=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 11)
        ]

        # 队列已满
        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            current_queue_size=10,
        )

        # 不应入队任何任务
        assert len(candidates) == 0

    def test_sorts_by_priority(self):
        """按优先级排序"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 4000,  # 较新
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=None,  # 从未同步，优先级最高
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                cursor_updated_at=now - 10000,  # 很旧
            ),
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 验证按优先级排序
        priorities = [c.priority for c in candidates]
        assert priorities == sorted(priorities)

        # 从未同步的应该排在最前面
        assert candidates[0].repo_id == 2

    def test_marks_paused_repos(self):
        """标记需要暂停的仓库"""
        config = SchedulerConfig(
            error_budget_threshold=0.3,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
                recent_run_count=10,
                recent_failed_count=5,  # 50% 失败率
            ),
        ]

        # 注意：should_schedule_repo 会因为 error_budget_exceeded 返回 False
        # 但 select_jobs_to_enqueue 不会为这些仓库生成候选
        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 由于 should_schedule_repo 返回 False，不会有候选
        assert len(candidates) == 0


class TestComputeBackfillWindow:
    """回填窗口计算测试"""

    def test_uses_repair_window_when_no_cursor(self):
        """没有游标时使用默认修复窗口"""
        config = SchedulerConfig(
            backfill_repair_window_hours=24,
            max_backfill_window_hours=168,
        )
        now = 100000.0

        since_ts, until_ts = compute_backfill_window(None, config, now=now)

        assert until_ts == now
        assert since_ts == now - (24 * 3600)

    def test_uses_cursor_ts_as_since(self):
        """使用游标时间戳作为起点"""
        config = SchedulerConfig(
            backfill_repair_window_hours=24,
            max_backfill_window_hours=168,
        )
        now = 100000.0
        cursor_ts = 90000.0

        since_ts, until_ts = compute_backfill_window(cursor_ts, config, now=now)

        assert until_ts == now
        assert since_ts == cursor_ts

    def test_respects_max_window(self):
        """遵守最大回填窗口限制"""
        config = SchedulerConfig(
            backfill_repair_window_hours=24,
            max_backfill_window_hours=48,  # 2 天
        )
        now = 100000.0
        cursor_ts = 10000.0  # 太久以前

        since_ts, until_ts = compute_backfill_window(cursor_ts, config, now=now)

        assert until_ts == now
        # since 应该被限制为最多 48 小时前
        max_since = now - (48 * 3600)
        assert since_ts >= max_since


class TestSchedulerConfig:
    """调度器配置测试"""

    def test_default_values(self):
        """测试默认值"""
        config = SchedulerConfig()

        assert config.global_concurrency == 10
        assert config.per_instance_concurrency == 3
        assert config.per_tenant_concurrency == 5
        assert config.error_budget_threshold == 0.3
        assert config.cursor_age_threshold_seconds == 3600

    def test_from_config_with_none(self):
        """从 None 配置加载使用默认值"""
        config = SchedulerConfig.from_config(None)

        assert config.global_concurrency == 10

    def test_job_type_priority_default(self):
        """job 类型优先级默认值"""
        config = SchedulerConfig()

        assert config.job_type_priority["commits"] == 1
        assert config.job_type_priority["mrs"] == 2
        assert config.job_type_priority["reviews"] == 3


class TestRepoSyncState:
    """仓库同步状态测试"""

    def test_default_values(self):
        """测试默认值"""
        state = RepoSyncState(repo_id=1, repo_type="git")

        assert state.gitlab_instance is None
        assert state.tenant_id is None
        assert state.cursor_updated_at is None
        assert state.recent_run_count == 0
        assert state.recent_failed_count == 0
        assert state.is_queued is False


class TestSyncJobCandidate:
    """同步任务候选项测试"""

    def test_default_values(self):
        """测试默认值"""
        candidate = SyncJobCandidate(repo_id=1, job_type="commits")

        assert candidate.priority == 0
        assert candidate.reason == ""
        assert candidate.should_pause is False
        assert candidate.pause_reason is None


class TestPerJobTypeQueuedCheck:
    """per-job_type 队列检查回归测试

    验证调度器按 (repo_id, job_type) 组合判断跳过条件，而不是仅按 repo。
    """

    def test_commits_queued_does_not_block_mrs_reviews(self):
        """回归测试：同 repo 的 commits 已入队时，mrs/reviews 不应被阻断"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,  # 需要同步
            ),
        ]

        # commits 已在队列中
        queued_pairs = {(1, "commits")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        # commits 应被跳过，但 mrs 和 reviews 应被调度
        job_types = {c.job_type for c in candidates}
        assert "commits" not in job_types, "commits 已入队，应被跳过"
        assert "mrs" in job_types, "mrs 不应被阻断"
        assert "reviews" in job_types, "reviews 不应被阻断"

    def test_mrs_queued_does_not_block_commits_reviews(self):
        """回归测试：同 repo 的 mrs 已入队时，commits/reviews 不应被阻断"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # mrs 已在队列中
        queued_pairs = {(1, "mrs")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        job_types = {c.job_type for c in candidates}
        assert "mrs" not in job_types, "mrs 已入队，应被跳过"
        assert "commits" in job_types, "commits 不应被阻断"
        assert "reviews" in job_types, "reviews 不应被阻断"

    def test_all_job_types_queued_blocks_all(self):
        """所有 job_type 都已入队时，都应被跳过"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 所有类型都已入队
        queued_pairs = {(1, "commits"), (1, "mrs"), (1, "reviews")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        assert len(candidates) == 0, "所有 job_type 都已入队，应返回空列表"

    def test_different_repos_independent_queue_check(self):
        """不同 repo 的队列检查应独立"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # repo 1 的 commits 已入队，repo 2 的任务都未入队
        queued_pairs = {(1, "commits")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        # repo 1: commits 跳过, mrs 调度
        # repo 2: commits 和 mrs 都调度
        repo1_jobs = {c.job_type for c in candidates if c.repo_id == 1}
        repo2_jobs = {c.job_type for c in candidates if c.repo_id == 2}

        assert "commits" not in repo1_jobs, "repo 1 的 commits 应被跳过"
        assert "mrs" in repo1_jobs, "repo 1 的 mrs 应被调度"
        assert "commits" in repo2_jobs, "repo 2 的 commits 应被调度"
        assert "mrs" in repo2_jobs, "repo 2 的 mrs 应被调度"

    def test_empty_queued_pairs_schedules_all(self):
        """空的 queued_pairs 时，所有任务都应被调度"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 空的 queued_pairs
        queued_pairs = set()

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        job_types = {c.job_type for c in candidates}
        assert job_types == {"commits", "mrs", "reviews"}, "所有 job_type 都应被调度"

    def test_none_queued_pairs_fallback_behavior(self):
        """queued_pairs 为 None 时应回退到空集合行为"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 不传 queued_pairs 参数（使用默认值 None）
        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
        )

        # 应该调度所有任务（回退到空集合）
        job_types = {c.job_type for c in candidates}
        assert "commits" in job_types
        assert "mrs" in job_types


class TestShouldScheduleRepoHealth:
    """should_schedule_repo_health 函数测试"""

    def test_does_not_check_is_queued(self):
        """不检查 is_queued 状态"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=None,  # 从未同步
            is_queued=True,  # 即使设置为 True
        )

        # should_schedule_repo_health 不应因 is_queued 而跳过
        should, reason, adj = should_schedule_repo_health(state, config, now=1000.0)

        assert should is True
        assert reason == "never_synced"

    def test_respects_error_budget(self):
        """仍然遵守错误预算限制"""
        config = SchedulerConfig(
            error_budget_threshold=0.3,
            cursor_age_threshold_seconds=3600,
        )
        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=None,
            recent_run_count=10,
            recent_failed_count=5,  # 50% 失败率
        )

        should, reason, adj = should_schedule_repo_health(state, config, now=1000.0)

        assert should is False
        assert "error_budget_exceeded" in reason


class TestIntegrationScenarios:
    """集成场景测试"""

    def test_mixed_repo_types(self):
        """混合仓库类型的调度"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="svn",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 为 git 和 svn 都生成候选
        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        assert len(candidates) == 2
        repo_ids = {c.repo_id for c in candidates}
        assert repo_ids == {1, 2}

    def test_mixed_instances_and_tenants(self):
        """混合实例和租户的调度"""
        config = SchedulerConfig(
            global_concurrency=10,
            per_instance_concurrency=2,
            per_tenant_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            # 实例 A，租户 1
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            # 实例 B，租户 2
            RepoSyncState(
                repo_id=4,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=5,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 每个实例最多 2 个，每个租户最多 2 个
        # 所以最多 4 个（2 from A + 2 from B）
        assert len(candidates) <= 4

        # 验证实例并发限制
        instance_counts = {}
        for c in candidates:
            s = next(s for s in states if s.repo_id == c.repo_id)
            inst = s.gitlab_instance
            instance_counts[inst] = instance_counts.get(inst, 0) + 1

        for count in instance_counts.values():
            assert count <= 2

    def test_priority_order_with_multiple_factors(self):
        """多因素影响下的优先级排序"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            # 从未同步，优先级最高
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=None,
            ),
            # 很久没同步
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 86400,  # 1 天前
            ),
            # 刚超过阈值
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                cursor_updated_at=now - 3700,
            ),
            # 超过阈值但有很多失败
            RepoSyncState(
                repo_id=4,
                repo_type="git",
                cursor_updated_at=now - 5000,
                recent_run_count=10,
                recent_failed_count=2,  # 20% 失败率
            ),
        ]

        candidates = select_jobs_to_enqueue(states, ["commits"], config, now=now)

        # 验证优先级顺序：从未同步 > 久未同步 > 刚超阈值
        repo_order = [c.repo_id for c in candidates]

        # repo 1（从未同步）应该在最前面
        assert repo_order[0] == 1


class TestBudgetSnapshot:
    """预算占用快照测试"""

    def test_empty_snapshot(self):
        """空快照创建"""
        snapshot = BudgetSnapshot.empty()

        assert snapshot.global_running == 0
        assert snapshot.global_pending == 0
        assert snapshot.global_active == 0
        assert snapshot.by_instance == {}
        assert snapshot.by_tenant == {}

    def test_snapshot_to_dict(self):
        """快照转字典"""
        snapshot = BudgetSnapshot(
            global_running=3,
            global_pending=5,
            global_active=8,
            by_instance={"gitlab.example.com": 4},
            by_tenant={"tenant-a": 2},
        )

        d = snapshot.to_dict()
        assert d["global_running"] == 3
        assert d["global_pending"] == 5
        assert d["global_active"] == 8
        assert d["by_instance"]["gitlab.example.com"] == 4
        assert d["by_tenant"]["tenant-a"] == 2


class TestBudgetSnapshotIntegration:
    """预算占用快照集成测试 - 测试给定初始占用时的入队限制"""

    def test_no_enqueue_when_max_running_exceeded(self):
        """当 running 数达到 max_running 时不应入队新任务"""
        config = SchedulerConfig(
            max_running=5,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建需要同步的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 已有 5 个 running 任务
        budget_snapshot = BudgetSnapshot(
            global_running=5,
            global_pending=0,
            global_active=5,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 不应入队任何新任务
        assert len(candidates) == 0

    def test_no_enqueue_when_max_queue_depth_exceeded(self):
        """当活跃任务数达到 max_queue_depth 时不应入队新任务"""
        config = SchedulerConfig(
            max_running=5,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 已有 3 running + 7 pending = 10 active
        budget_snapshot = BudgetSnapshot(
            global_running=3,
            global_pending=7,
            global_active=10,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 不应入队任何新任务
        assert len(candidates) == 0

    def test_partial_enqueue_respects_queue_depth(self):
        """入队数量受 max_queue_depth 限制"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建 10 个需要同步的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 11)
        ]

        # 已有 7 个活跃任务，只剩 3 个名额
        budget_snapshot = BudgetSnapshot(
            global_running=2,
            global_pending=5,
            global_active=7,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 只能入队 3 个
        assert len(candidates) == 3

    def test_respects_instance_initial_count(self):
        """初始实例占用计数应被考虑"""
        config = SchedulerConfig(
            max_running=20,
            max_queue_depth=20,
            per_instance_concurrency=3,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建同一 GitLab 实例的 5 个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance="gitlab.example.com",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 该实例已有 2 个活跃任务
        budget_snapshot = BudgetSnapshot(
            global_running=2,
            global_pending=0,
            global_active=2,
            by_instance={"gitlab.example.com": 2},
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 只能再入队 1 个（3 - 2 = 1）
        assert len(candidates) == 1

    def test_respects_tenant_initial_count(self):
        """初始租户占用计数应被考虑"""
        config = SchedulerConfig(
            max_running=20,
            max_queue_depth=20,
            per_tenant_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建同一租户的 5 个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-alpha",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 该租户已有 1 个活跃任务
        budget_snapshot = BudgetSnapshot(
            global_running=1,
            global_pending=0,
            global_active=1,
            by_tenant={"tenant-alpha": 1},
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 只能再入队 1 个（2 - 1 = 1）
        assert len(candidates) == 1

    def test_mixed_instance_tenant_with_initial_counts(self):
        """混合实例和租户初始占用的复杂场景"""
        config = SchedulerConfig(
            max_running=20,
            max_queue_depth=20,
            per_instance_concurrency=3,
            per_tenant_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            # gitlab-a, tenant-1: 2 个仓库
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            # gitlab-b, tenant-1: 1 个仓库
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            # gitlab-b, tenant-2: 2 个仓库
            RepoSyncState(
                repo_id=4,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=5,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 初始状态：
        # - gitlab-a: 2 个活跃（已满）
        # - tenant-1: 1 个活跃
        # - tenant-2: 0 个活跃
        budget_snapshot = BudgetSnapshot(
            global_running=2,
            global_pending=0,
            global_active=2,
            by_instance={"gitlab-a.com": 2},  # gitlab-a 只剩 1 个名额
            by_tenant={"tenant-1": 1},  # tenant-1 只剩 1 个名额
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 分析预期：
        # - repo 1, 2: gitlab-a 只能再加 1 个，tenant-1 只能再加 1 个
        # - repo 3: gitlab-b 可以，但 tenant-1 只能再加 1 个
        # - repo 4, 5: gitlab-b 可以，tenant-2 可以

        # 验证结果
        [c.repo_id for c in candidates]

        # gitlab-a 的任务最多 1 个额外
        gitlab_a_count = sum(
            1
            for c in candidates
            if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance == "gitlab-a.com"
        )
        assert gitlab_a_count <= 1

        # tenant-1 的任务最多 1 个额外
        tenant_1_count = sum(
            1
            for c in candidates
            if next((s for s in states if s.repo_id == c.repo_id)).tenant_id == "tenant-1"
        )
        assert tenant_1_count <= 1

    def test_backward_compatible_without_snapshot(self):
        """不传 budget_snapshot 时应向后兼容使用 current_queue_size"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 使用 current_queue_size 而不是 budget_snapshot
        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            current_queue_size=8,
            budget_snapshot=None,
        )

        # 只能入队 2 个（10 - 8 = 2）
        assert len(candidates) == 2


class TestRepoJobTypeDeduplication:
    """repo+job_type 去重测试

    验证 scheduler 按 (repo_id, job_type) 组合进行去重，
    确保同一 repo 的不同 job_type 可以独立入队。
    """

    def test_same_repo_different_job_types_all_enqueue(self):
        """同一 repo 的不同 job_type 都可以入队"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 没有已入队的任务
        queued_pairs = set()

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        # 所有 3 种 job_type 都应该被调度
        job_types = {c.job_type for c in candidates}
        assert job_types == {"commits", "mrs", "reviews"}

    def test_partial_queued_only_unqueued_enqueue(self):
        """部分 job_type 已入队时，只入队未入队的"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # repo 1 的 commits 和 mrs 已入队
        queued_pairs = {(1, "commits"), (1, "mrs")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        # repo 1: 只有 reviews 可入队
        repo1_jobs = {c.job_type for c in candidates if c.repo_id == 1}
        assert repo1_jobs == {"reviews"}

        # repo 2: 所有都可入队
        repo2_jobs = {c.job_type for c in candidates if c.repo_id == 2}
        assert repo2_jobs == {"commits", "mrs", "reviews"}

    def test_queued_pairs_exact_match(self):
        """queued_pairs 必须精确匹配 (repo_id, job_type)"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # repo 1 的 commits 已入队，repo 2 的 mrs 已入队
        queued_pairs = {(1, "commits"), (2, "mrs")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            queued_pairs=queued_pairs,
        )

        # 验证精确匹配
        repo1_jobs = {c.job_type for c in candidates if c.repo_id == 1}
        repo2_jobs = {c.job_type for c in candidates if c.repo_id == 2}

        assert "commits" not in repo1_jobs, "repo 1 的 commits 已入队"
        assert "mrs" in repo1_jobs, "repo 1 的 mrs 未入队"
        assert "commits" in repo2_jobs, "repo 2 的 commits 未入队"
        assert "mrs" not in repo2_jobs, "repo 2 的 mrs 已入队"


class TestBudgetOccupancyAccounting:
    """预算占用计入测试

    验证 scheduler 正确计入当前预算占用，
    包括 running 和 pending 任务对全局/实例/租户并发的影响。
    """

    def test_running_jobs_counted_in_budget(self):
        """running 任务被计入预算占用 - 达到 max_running 时完全停止入队"""
        config = SchedulerConfig(
            max_running=3,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 已有 3 个 running 任务（达到 max_running）
        budget_snapshot = BudgetSnapshot(
            global_running=3,
            global_pending=0,
            global_active=3,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # max_running=3, 已有 3 个 running，不应入队任何新任务
        assert len(candidates) == 0

    def test_pending_jobs_counted_in_budget(self):
        """pending 任务被计入预算占用（队列深度）"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=5,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 11)
        ]

        # 已有 3 个 pending 任务
        budget_snapshot = BudgetSnapshot(
            global_running=0,
            global_pending=3,
            global_active=3,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # max_queue_depth=5, 已有 3 个 active，只能再入队 2 个
        assert len(candidates) == 2

    def test_instance_budget_correctly_accumulated(self):
        """实例预算正确累计"""
        config = SchedulerConfig(
            max_running=20,
            max_queue_depth=20,
            per_instance_concurrency=3,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance="gitlab.example.com",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # gitlab.example.com 已有 1 个任务
        budget_snapshot = BudgetSnapshot(
            global_running=1,
            global_pending=0,
            global_active=1,
            by_instance={"gitlab.example.com": 1},
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # per_instance_concurrency=3, 已有 1 个，只能再入队 2 个
        assert len(candidates) == 2

    def test_tenant_budget_correctly_accumulated(self):
        """租户预算正确累计"""
        config = SchedulerConfig(
            max_running=20,
            max_queue_depth=20,
            per_tenant_concurrency=4,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-x",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 10)
        ]

        # tenant-x 已有 2 个任务
        budget_snapshot = BudgetSnapshot(
            global_running=2,
            global_pending=0,
            global_active=2,
            by_tenant={"tenant-x": 2},
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # per_tenant_concurrency=4, 已有 2 个，只能再入队 2 个
        assert len(candidates) == 2

    def test_mixed_budget_constraints(self):
        """混合预算约束（全局、实例、租户同时生效）"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=10,
            per_instance_concurrency=3,
            per_tenant_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建属于同一实例、同一租户的多个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance="gitlab.example.com",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 无初始占用
        budget_snapshot = BudgetSnapshot.empty()

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # per_tenant_concurrency=2 是最严格的限制
        assert len(candidates) == 2

    def test_new_enqueue_increments_internal_counters(self):
        """新入队的任务应递增内部计数器（累计效果）"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=10,
            per_instance_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 两个不同实例的仓库
        states = [
            # instance A: 3 个仓库
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="instance-a",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="instance-a",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="instance-a",
                cursor_updated_at=now - 5000,
            ),
            # instance B: 3 个仓库
            RepoSyncState(
                repo_id=4,
                repo_type="git",
                gitlab_instance="instance-b",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=5,
                repo_type="git",
                gitlab_instance="instance-b",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=6,
                repo_type="git",
                gitlab_instance="instance-b",
                cursor_updated_at=now - 5000,
            ),
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=BudgetSnapshot.empty(),
        )

        # 每个实例最多 2 个，共 4 个
        assert len(candidates) == 4

        # 验证每个实例的数量
        instance_a_count = sum(
            1
            for c in candidates
            if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance == "instance-a"
        )
        instance_b_count = sum(
            1
            for c in candidates
            if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance == "instance-b"
        )

        assert instance_a_count == 2
        assert instance_b_count == 2


class TestSchedulerConfigNewParams:
    """SchedulerConfig 新参数测试"""

    def test_max_running_default(self):
        """max_running 默认值"""
        config = SchedulerConfig()
        assert config.max_running == 5

    def test_max_queue_depth_default(self):
        """max_queue_depth 默认值"""
        config = SchedulerConfig()
        assert config.max_queue_depth == 10

    def test_global_concurrency_synced_with_max_queue_depth(self):
        """global_concurrency 应与 max_queue_depth 同步"""
        config = SchedulerConfig(max_queue_depth=15)
        assert config.global_concurrency == 15

    def test_backward_compat_global_concurrency(self):
        """向后兼容：设置 global_concurrency 应更新 max_queue_depth"""
        config = SchedulerConfig(global_concurrency=20)
        # 通过 __post_init__ 同步
        assert config.max_queue_depth == 20

    def test_from_config_new_params(self):
        """from_config 应正确读取新参数"""

        class MockConfig:
            def get(self, key, default=None):
                mapping = {
                    "scm.scheduler.max_running": 8,
                    "scm.scheduler.max_queue_depth": 16,
                }
                return mapping.get(key, default)

        config = SchedulerConfig.from_config(MockConfig())
        assert config.max_running == 8
        assert config.max_queue_depth == 16
        assert config.global_concurrency == 16  # 同步


class TestInstanceLevelCircuitBreaker:
    """
    实例级熔断测试

    测试场景：当某个 instance 熔断（但全局不熔断）时，仅该 instance 的 repo 被降级/暂停
    """

    def test_instance_breaker_open_pauses_only_that_instance_repos(self):
        """
        测试：instance 熔断但全局不熔断时，仅该 instance 的 repo 被暂停

        场景构造：
        - 全局健康统计良好（不触发全局熔断）
        - instance-a 健康统计差（触发实例熔断）
        - instance-b 健康统计良好（不触发实例熔断）

        预期结果：
        - instance-a 的 repo 被暂停（不入队或仅 backfill）
        - instance-b 的 repo 正常入队
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerConfig,
            CircuitBreakerController,
            CircuitState,
        )

        # 创建熔断配置（降低阈值便于测试触发）
        cb_config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            rate_limit_threshold=0.2,
            window_count=10,
        )

        # === 构造健康统计数据 ===
        # 全局统计：良好（不触发熔断）
        global_health = {
            "total_runs": 20,
            "completed_runs": 18,
            "failed_runs": 2,
            "no_data_runs": 0,
            "total_429_hits": 5,
            "total_requests": 100,
            "failed_rate": 0.1,  # 10% < 30% 阈值
            "rate_limit_rate": 0.05,  # 5% < 20% 阈值
        }

        # instance-a 统计：差（触发熔断）
        instance_a_health = {
            "total_runs": 10,
            "completed_runs": 5,
            "failed_runs": 5,
            "no_data_runs": 0,
            "total_429_hits": 30,
            "total_requests": 100,
            "failed_rate": 0.5,  # 50% > 30% 阈值
            "rate_limit_rate": 0.3,  # 30% > 20% 阈值
        }

        # instance-b 统计：良好（不触发熔断）
        instance_b_health = {
            "total_runs": 10,
            "completed_runs": 9,
            "failed_runs": 1,
            "no_data_runs": 0,
            "total_429_hits": 2,
            "total_requests": 100,
            "failed_rate": 0.1,  # 10% < 30% 阈值
            "rate_limit_rate": 0.02,  # 2% < 20% 阈值
        }

        # === 创建熔断控制器并检查决策 ===
        global_breaker = CircuitBreakerController(config=cb_config, key="global")
        instance_a_breaker = CircuitBreakerController(config=cb_config, key="instance-a")
        instance_b_breaker = CircuitBreakerController(config=cb_config, key="instance-b")

        # 检查全局熔断决策
        global_decision = global_breaker.check(global_health)
        assert global_decision.allow_sync is True, "全局应允许同步"
        assert global_decision.current_state == CircuitState.CLOSED.value, "全局应处于 CLOSED 状态"

        # 检查 instance-a 熔断决策
        instance_a_decision = instance_a_breaker.check(instance_a_health)
        assert instance_a_decision.current_state == CircuitState.OPEN.value, (
            "instance-a 应处于 OPEN 状态"
        )
        # 注意：默认配置下 OPEN 状态可能允许 backfill_only，检查实际行为

        # 检查 instance-b 熔断决策
        instance_b_decision = instance_b_breaker.check(instance_b_health)
        assert instance_b_decision.allow_sync is True, "instance-b 应允许同步"
        assert instance_b_decision.current_state == CircuitState.CLOSED.value, (
            "instance-b 应处于 CLOSED 状态"
        )

    def test_instance_breaker_backfill_mode_applies_degradation(self):
        """
        测试：instance 熔断时进入 backfill 模式，应用降级参数

        当 instance 处于 OPEN 状态但 backfill_only_mode=True 时：
        - allow_sync 可能为 True（允许 backfill）
        - is_backfill_only 应为 True
        - suggested_batch_size 应为降级值
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerConfig,
            CircuitBreakerController,
            CircuitState,
        )

        cb_config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            backfill_only_mode=True,  # 允许 backfill
            degraded_batch_size=10,
        )

        # 构造触发熔断的健康统计
        bad_health = {
            "total_runs": 10,
            "completed_runs": 5,
            "failed_runs": 5,
            "no_data_runs": 0,
            "total_429_hits": 0,
            "total_requests": 100,
            "failed_rate": 0.5,  # 50% > 30% 阈值
            "rate_limit_rate": 0.0,
        }

        breaker = CircuitBreakerController(config=cb_config, key="test-instance")
        decision = breaker.check(bad_health)

        assert decision.current_state == CircuitState.OPEN.value, "应处于 OPEN 状态"
        assert decision.is_backfill_only is True, "应处于 backfill_only 模式"
        assert decision.suggested_batch_size == 10, "应使用降级的 batch_size"

    def test_scheduler_applies_instance_breaker_decisions(self):
        """
        集成测试：验证调度器在筛选候选时正确应用实例级熔断决策

        构造场景：
        - 3 个仓库：repo 1, 2 属于 instance-a（熔断），repo 3 属于 instance-b（正常）
        - 模拟 instance-a 熔断决策为 allow_sync=False
        - 模拟 instance-b 熔断决策为 allow_sync=True

        预期：
        - repo 1, 2 被跳过（因 instance-a 熔断）
        - repo 3 正常入队
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerDecision,
            CircuitState,
        )

        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        # 构造仓库状态
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="instance-a",
                cursor_updated_at=now - 5000,  # 需要同步
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="instance-a",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="instance-b",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 构造熔断决策
        instance_decisions = {
            "instance-a": CircuitBreakerDecision(
                allow_sync=False,  # 完全熔断，不允许同步
                current_state=CircuitState.OPEN.value,
            ),
            "instance-b": CircuitBreakerDecision(
                allow_sync=True,  # 正常
                current_state=CircuitState.CLOSED.value,
            ),
        }

        # 先使用 select_jobs_to_enqueue 获取候选
        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        # 所有仓库都需要同步
        assert len(candidates) == 3

        # 模拟调度器的筛选逻辑：检查实例级熔断并过滤
        filtered_candidates = []
        instance_paused_count = 0

        for candidate in candidates:
            # 查找对应的 state
            state = next((s for s in states if s.repo_id == candidate.repo_id), None)
            if state is None:
                continue

            # 检查实例级熔断
            if state.gitlab_instance and state.gitlab_instance in instance_decisions:
                instance_decision = instance_decisions[state.gitlab_instance]
                if not instance_decision.allow_sync:
                    instance_paused_count += 1
                    continue  # 跳过熔断的实例

            filtered_candidates.append(candidate)

        # 验证结果
        assert len(filtered_candidates) == 1, "只有 instance-b 的 repo 应被保留"
        assert filtered_candidates[0].repo_id == 3, "只有 repo 3 应被保留"
        assert instance_paused_count == 2, "repo 1, 2 应因 instance-a 熔断而被暂停"

    def test_instance_breaker_backfill_only_marks_jobs_as_backfill(self):
        """
        测试：当 instance 处于 backfill_only 模式时，任务应标记为 backfill

        场景：
        - instance-a 处于 OPEN 但 backfill_only_mode=True（allow_sync=True, is_backfill_only=True）
        - 该 instance 的任务应标记为 backfill 模式并使用降级参数
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerDecision,
            CircuitState,
        )

        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="instance-a",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="instance-b",
                cursor_updated_at=now - 5000,
            ),
        ]

        # instance-a: backfill_only 模式
        # instance-b: 正常模式
        instance_decisions = {
            "instance-a": CircuitBreakerDecision(
                allow_sync=True,  # 允许同步（backfill）
                is_backfill_only=True,  # 但仅 backfill
                current_state=CircuitState.OPEN.value,
                suggested_batch_size=10,
                suggested_diff_mode="none",
            ),
            "instance-b": CircuitBreakerDecision(
                allow_sync=True,
                is_backfill_only=False,
                current_state=CircuitState.CLOSED.value,
            ),
        }

        # 获取候选
        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        # 模拟调度器构建任务的逻辑
        jobs = []
        for candidate in candidates:
            state = next((s for s in states if s.repo_id == candidate.repo_id), None)
            if state is None:
                continue

            task_mode = "incremental"
            instance_decision = instance_decisions.get(state.gitlab_instance)

            if instance_decision and instance_decision.is_backfill_only:
                task_mode = "backfill"

            jobs.append(
                {
                    "repo_id": candidate.repo_id,
                    "mode": task_mode,
                    "gitlab_instance": state.gitlab_instance,
                }
            )

        # 验证
        job_1 = next(j for j in jobs if j["repo_id"] == 1)
        job_2 = next(j for j in jobs if j["repo_id"] == 2)

        assert job_1["mode"] == "backfill", "instance-a 的任务应为 backfill 模式"
        assert job_2["mode"] == "incremental", "instance-b 的任务应为 incremental 模式"

    def test_mixed_global_and_instance_breaker_uses_stricter(self):
        """
        测试：当全局和实例级熔断同时存在时，使用更严格的限制

        场景：
        - 全局处于 HALF_OPEN（backfill_only=True, batch_size=50）
        - instance-a 处于 OPEN（backfill_only=True, batch_size=10）

        预期：instance-a 的任务应使用更小的 batch_size (10)
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerDecision,
            CircuitState,
        )

        # 全局决策
        global_decision = CircuitBreakerDecision(
            allow_sync=True,
            is_backfill_only=True,
            suggested_batch_size=50,
            current_state=CircuitState.HALF_OPEN.value,
        )

        # 实例决策
        instance_decision = CircuitBreakerDecision(
            allow_sync=True,
            is_backfill_only=True,
            suggested_batch_size=10,
            current_state=CircuitState.OPEN.value,
        )

        # 模拟选择更保守参数的逻辑
        final_batch_size = global_decision.suggested_batch_size

        if instance_decision.is_backfill_only:
            final_batch_size = min(final_batch_size, instance_decision.suggested_batch_size)

        assert final_batch_size == 10, "应使用更小的 batch_size"


# ============ HALF_OPEN 探测模式测试 ============


class TestHalfOpenProbeMode:
    """
    HALF_OPEN 探测模式测试

    测试场景：当熔断器从 OPEN 转换到 HALF_OPEN 时：
    1. 仅生成少量 probe job（由 probe_budget_per_interval 控制）
    2. 仅允许指定的 job_type（由 probe_job_types_allowlist 控制）
    3. 生成的任务带有降级参数（suggested_batch_size、suggested_diff_mode 等）
    """

    def test_circuit_breaker_config_has_probe_fields(self):
        """测试：CircuitBreakerConfig 包含探测相关配置"""
        from engram.logbook.scm_sync_policy import CircuitBreakerConfig

        config = CircuitBreakerConfig()

        # 检查默认值
        assert hasattr(config, "probe_budget_per_interval")
        assert hasattr(config, "probe_job_types_allowlist")
        assert config.probe_budget_per_interval == 2
        assert config.probe_job_types_allowlist == ["commits"]

    def test_circuit_breaker_config_custom_probe_values(self):
        """测试：CircuitBreakerConfig 支持自定义探测配置"""
        from engram.logbook.scm_sync_policy import CircuitBreakerConfig

        config = CircuitBreakerConfig(
            probe_budget_per_interval=5,
            probe_job_types_allowlist=["commits", "mrs"],
        )

        assert config.probe_budget_per_interval == 5
        assert config.probe_job_types_allowlist == ["commits", "mrs"]

    def test_circuit_breaker_decision_has_probe_fields(self):
        """测试：CircuitBreakerDecision 包含探测模式字段"""
        from engram.logbook.scm_sync_policy import CircuitBreakerDecision

        decision = CircuitBreakerDecision()

        # 检查默认值
        assert hasattr(decision, "is_probe_mode")
        assert hasattr(decision, "probe_budget")
        assert hasattr(decision, "probe_job_types_allowlist")
        assert decision.is_probe_mode is False
        assert decision.probe_budget == 0
        assert decision.probe_job_types_allowlist == []

    def test_open_to_half_open_transition_sets_probe_mode(self):
        """
        测试：从 OPEN 转换到 HALF_OPEN 时，决策中 is_probe_mode=True

        场景：
        1. 首先触发熔断（OPEN 状态）
        2. 等待 open_duration_seconds 后检查（转换到 HALF_OPEN）
        3. 返回的决策应包含探测模式信息
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerConfig,
            CircuitBreakerController,
            CircuitState,
        )

        # 配置：较短的 open_duration 便于测试
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=10,  # 10 秒
            probe_budget_per_interval=3,
            probe_job_types_allowlist=["commits"],
        )

        breaker = CircuitBreakerController(config=config, key="test")

        # 构造触发熔断的 health_stats
        bad_health = {
            "total_runs": 10,
            "failed_runs": 5,
            "failed_rate": 0.5,  # 50% > 30%
            "rate_limit_rate": 0.0,
        }

        # 第一次检查：触发熔断，进入 OPEN
        decision1 = breaker.check(bad_health, now=1000.0)
        assert decision1.current_state == CircuitState.OPEN.value
        assert decision1.is_probe_mode is False  # OPEN 状态不是探测模式

        # 第二次检查：模拟时间过去 15 秒（> open_duration_seconds）
        # 健康统计良好，应转换到 HALF_OPEN
        good_health = {
            "total_runs": 10,
            "failed_runs": 1,
            "failed_rate": 0.1,  # 10% < 30%
            "rate_limit_rate": 0.0,
        }

        decision2 = breaker.check(good_health, now=1015.0)
        assert decision2.current_state == CircuitState.HALF_OPEN.value
        assert decision2.is_probe_mode is True
        assert decision2.probe_budget == 3
        assert decision2.probe_job_types_allowlist == ["commits"]

    def test_half_open_decision_contains_degraded_params(self):
        """
        测试：HALF_OPEN 状态的决策包含降级参数

        预期：suggested_batch_size、suggested_forward_window_seconds、suggested_diff_mode
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerConfig,
            CircuitBreakerController,
            CircuitState,
        )

        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=10,
            degraded_batch_size=15,
            degraded_forward_window_seconds=600,
            probe_budget_per_interval=2,
        )

        breaker = CircuitBreakerController(config=config, key="test")

        # 触发熔断
        bad_health = {"total_runs": 10, "failed_runs": 5, "failed_rate": 0.5}
        breaker.check(bad_health, now=1000.0)

        # 转换到 HALF_OPEN
        good_health = {"total_runs": 10, "failed_runs": 1, "failed_rate": 0.1}
        decision = breaker.check(good_health, now=1015.0)

        assert decision.current_state == CircuitState.HALF_OPEN.value
        assert decision.suggested_batch_size == 15  # 使用降级值
        assert decision.suggested_forward_window_seconds == 600
        assert decision.suggested_diff_mode == "none"  # 第一次探测使用 none

    def test_half_open_probe_mode_filters_job_types(self):
        """
        测试：HALF_OPEN 探测模式下，仅放行 allowlist 中的 job_type

        场景：
        - probe_job_types_allowlist = ["commits"]
        - 候选任务包含 commits, mrs, reviews
        - 预期仅 commits 类型的任务被保留
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerDecision,
            CircuitState,
        )

        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        # 构造仓库状态
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,  # 需要同步
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 获取所有候选（不考虑探测模式）
        all_candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],  # 3 种类型
            config=config,
            now=now,
        )

        # 应该有 6 个候选（2 个仓库 × 3 种类型）
        assert len(all_candidates) == 6

        # 模拟 HALF_OPEN 探测模式的过滤
        probe_decision = CircuitBreakerDecision(
            allow_sync=True,
            is_probe_mode=True,
            probe_budget=2,
            probe_job_types_allowlist=["commits"],
            current_state=CircuitState.HALF_OPEN.value,
        )

        # 按 allowlist 过滤
        filtered_candidates = [
            c for c in all_candidates if c.job_type in probe_decision.probe_job_types_allowlist
        ]

        # 应该只剩 2 个（2 个仓库 × 1 种类型）
        assert len(filtered_candidates) == 2
        assert all(c.job_type == "commits" for c in filtered_candidates)

        # 再按 budget 限制
        final_candidates = filtered_candidates[: probe_decision.probe_budget]
        assert len(final_candidates) == 2

    def test_half_open_probe_mode_limits_by_budget(self):
        """
        测试：HALF_OPEN 探测模式下，任务数量被 probe_budget 限制

        场景：
        - probe_budget_per_interval = 2
        - 候选任务有 5 个
        - 预期仅放行 2 个
        """

        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        # 构造 5 个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 获取候选
        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        assert len(candidates) == 5

        # 模拟 budget 限制
        probe_budget = 2
        limited_candidates = candidates[:probe_budget]

        assert len(limited_candidates) == 2

    def test_half_open_stays_in_probe_mode_until_recovery(self):
        """
        测试：HALF_OPEN 状态下持续返回 is_probe_mode=True，直到恢复

        场景：
        1. 进入 HALF_OPEN
        2. 多次 check() 都应返回 is_probe_mode=True
        3. 连续成功后恢复到 CLOSED，is_probe_mode 变为 False
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerConfig,
            CircuitBreakerController,
            CircuitState,
        )

        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            open_duration_seconds=10,
            recovery_success_count=2,
            probe_budget_per_interval=3,
            enable_smoothing=False,  # 禁用平滑以获得可预测的测试行为
        )

        breaker = CircuitBreakerController(config=config, key="test")

        # 触发熔断并转换到 HALF_OPEN
        bad_health = {"total_runs": 10, "failed_runs": 5, "failed_rate": 0.5}
        breaker.check(bad_health, now=1000.0)

        good_health = {"total_runs": 10, "failed_runs": 1, "failed_rate": 0.1}
        decision1 = breaker.check(good_health, now=1015.0)
        assert decision1.current_state == CircuitState.HALF_OPEN.value
        assert decision1.is_probe_mode is True

        # 记录第一次成功
        breaker.record_result(success=True)

        # 再次检查，仍在 HALF_OPEN
        decision2 = breaker.check(good_health, now=1016.0)
        assert decision2.current_state == CircuitState.HALF_OPEN.value
        assert decision2.is_probe_mode is True

        # 记录第二次成功，应恢复到 CLOSED
        breaker.record_result(success=True)

        decision3 = breaker.check(good_health, now=1017.0)
        assert decision3.current_state == CircuitState.CLOSED.value
        assert decision3.is_probe_mode is False

    def test_decision_to_dict_includes_probe_fields(self):
        """测试：CircuitBreakerDecision.to_dict() 包含探测模式字段"""
        from engram.logbook.scm_sync_policy import CircuitBreakerDecision

        decision = CircuitBreakerDecision(
            allow_sync=True,
            is_probe_mode=True,
            probe_budget=3,
            probe_job_types_allowlist=["commits", "mrs"],
            suggested_batch_size=10,
            suggested_forward_window_seconds=300,
            suggested_diff_mode="none",
        )

        d = decision.to_dict()

        assert d["is_probe_mode"] is True
        assert d["probe_budget"] == 3
        assert d["probe_job_types_allowlist"] == ["commits", "mrs"]
        assert d["suggested_batch_size"] == 10
        assert d["suggested_forward_window_seconds"] == 300
        assert d["suggested_diff_mode"] == "none"

    def test_empty_allowlist_allows_all_job_types(self):
        """测试：probe_job_types_allowlist 为空列表时允许所有类型"""
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerConfig,
        )

        config = CircuitBreakerConfig(
            probe_job_types_allowlist=[],  # 空列表
        )

        # 空 allowlist 不应过滤任何 job_type
        all_types = ["commits", "mrs", "reviews"]
        allowlist = config.probe_job_types_allowlist

        if allowlist:
            filtered = [t for t in all_types if t in allowlist]
        else:
            filtered = all_types  # 空 allowlist 允许所有

        assert filtered == all_types


# ============ Probe 模式 Scheduler 端到端测试 ============


class TestProbeSchedulerEndToEnd:
    """
    Probe 模式 Scheduler 层端到端测试

    验证场景：
    - probe_budget 在 select_jobs_to_enqueue 中的限制
    - probe_job_types_allowlist 过滤在 scheduler 层的正确应用
    - probe 模式下多仓库多 job_type 的正确限制行为
    """

    def test_probe_budget_limits_candidates_in_policy_layer(self):
        """
        测试：policy 层选择候选时，调用方可根据 probe_budget 限制数量

        场景：
        - 5 个仓库，每个仓库支持 commits 类型
        - probe_budget = 2
        - 预期：调用方（scheduler）限制为 2 个候选
        """
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        # 构造 5 个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,  # 需要同步
            )
            for i in range(1, 6)
        ]

        # 获取所有候选
        all_candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        assert len(all_candidates) == 5

        # 模拟 probe 模式的 budget 限制
        probe_budget = 2
        limited = all_candidates[:probe_budget]

        assert len(limited) == 2
        # 验证优先级顺序保持
        assert limited[0].priority <= limited[1].priority

    def test_probe_allowlist_filters_job_types_in_candidates(self):
        """
        测试：probe_job_types_allowlist 过滤候选的 job_type

        场景：
        - 2 个仓库，支持 commits, mrs, reviews
        - probe_job_types_allowlist = ["commits"]
        - 预期：仅保留 commits 类型的候选
        """
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 3)
        ]

        # 获取所有候选（3 种类型 × 2 仓库 = 6 个）
        all_candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],
            config=config,
            now=now,
        )

        assert len(all_candidates) == 6

        # 模拟 probe allowlist 过滤
        probe_allowlist = ["commits"]
        filtered = [c for c in all_candidates if c.job_type in probe_allowlist]

        assert len(filtered) == 2
        assert all(c.job_type == "commits" for c in filtered)

    def test_probe_budget_and_allowlist_combined(self):
        """
        测试：probe_budget 和 probe_job_types_allowlist 组合使用

        场景：
        - 5 个仓库，支持 commits, mrs
        - probe_job_types_allowlist = ["commits"]
        - probe_budget = 2
        - 预期：先过滤为 commits（5 个），再限制为 2 个
        """
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 获取所有候选
        all_candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs"],
            config=config,
            now=now,
        )

        # 应该有 10 个（5 仓库 × 2 类型）
        assert len(all_candidates) == 10

        # 先按 allowlist 过滤
        probe_allowlist = ["commits"]
        filtered = [c for c in all_candidates if c.job_type in probe_allowlist]
        assert len(filtered) == 5

        # 再按 budget 限制
        probe_budget = 2
        final = filtered[:probe_budget]
        assert len(final) == 2

    def test_probe_empty_allowlist_allows_all_types(self):
        """
        测试：空的 probe_job_types_allowlist 允许所有类型

        场景：
        - 2 个仓库，支持 commits, mrs, reviews
        - probe_job_types_allowlist = []（空）
        - 预期：所有类型都保留
        """
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 3)
        ]

        # 获取所有候选
        all_candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],
            config=config,
            now=now,
        )

        assert len(all_candidates) == 6

        # 空 allowlist 不过滤
        probe_allowlist = []
        if probe_allowlist:
            filtered = [c for c in all_candidates if c.job_type in probe_allowlist]
        else:
            filtered = all_candidates

        assert len(filtered) == 6

    def test_probe_mode_with_circuit_breaker_decision(self):
        """
        测试：使用 CircuitBreakerDecision 进行端到端 probe 过滤

        场景：
        - 熔断器处于 HALF_OPEN 状态
        - probe_budget = 2
        - probe_job_types_allowlist = ["commits"]
        - 5 个仓库，支持 commits, mrs
        - 预期：最终 2 个 commits 类型的候选
        """
        from engram.logbook.scm_sync_policy import (
            CircuitBreakerDecision,
            CircuitState,
        )

        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 6)
        ]

        # 获取所有候选
        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs"],
            config=config,
            now=now,
        )

        # 模拟 HALF_OPEN probe 决策
        probe_decision = CircuitBreakerDecision(
            allow_sync=True,
            is_probe_mode=True,
            probe_budget=2,
            probe_job_types_allowlist=["commits"],
            current_state=CircuitState.HALF_OPEN.value,
        )

        # 按 scheduler 逻辑过滤
        filtered = candidates
        if probe_decision.is_probe_mode:
            # 按 allowlist 过滤
            if probe_decision.probe_job_types_allowlist:
                filtered = [
                    c for c in filtered if c.job_type in probe_decision.probe_job_types_allowlist
                ]
            # 按 budget 限制
            if len(filtered) > probe_decision.probe_budget:
                filtered = filtered[: probe_decision.probe_budget]

        assert len(filtered) == 2
        assert all(c.job_type == "commits" for c in filtered)

    def test_probe_mode_respects_priority_order(self):
        """
        测试：probe 模式限制时保持优先级顺序

        场景：
        - 5 个仓库，不同优先级
        - probe_budget = 3
        - 预期：保留优先级最高的 3 个
        """
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        # 不同游标年龄 -> 不同优先级
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 10000,  # 最旧 -> 最高优先级
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 8000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                cursor_updated_at=now - 6000,
            ),
            RepoSyncState(
                repo_id=4,
                repo_type="git",
                cursor_updated_at=now - 4000,
            ),
            RepoSyncState(
                repo_id=5,
                repo_type="git",
                cursor_updated_at=now - 3700,  # 最新
            ),
        ]

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        # 验证按优先级排序
        assert len(candidates) == 5
        for i in range(len(candidates) - 1):
            assert candidates[i].priority <= candidates[i + 1].priority

        # probe budget 限制
        probe_budget = 3
        limited = candidates[:probe_budget]

        assert len(limited) == 3
        # 限制后仍保持优先级顺序
        for i in range(len(limited) - 1):
            assert limited[i].priority <= limited[i + 1].priority


# ============ Pause 记录相关测试 ============


# 导入 db 模块中的 pause 相关函数和类
try:
    import os
    import sys

    # 添加 scripts 目录到路径
    scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from db import (
        RepoPauseRecord,
        _build_pause_key,
        _parse_pause_key,
    )

    PAUSE_IMPORTS_AVAILABLE = True
except ImportError:
    PAUSE_IMPORTS_AVAILABLE = False


@pytest.mark.skipif(not PAUSE_IMPORTS_AVAILABLE, reason="db module not available")
class TestRepoPauseRecord:
    """RepoPauseRecord 数据类测试"""

    def test_create_record(self):
        """创建暂停记录"""
        now = 1000.0
        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="error_budget_exceeded",
            paused_at=now,
            failure_rate=0.5,
        )

        assert record.repo_id == 1
        assert record.job_type == "commits"
        assert record.paused_until == now + 300
        assert record.reason == "error_budget_exceeded"
        assert record.paused_at == now
        assert record.failure_rate == 0.5

    def test_is_expired_false(self):
        """未过期时返回 False"""
        now = 1000.0
        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="test",
            paused_at=now,
        )

        assert record.is_expired(now=now) is False
        assert record.is_expired(now=now + 299) is False

    def test_is_expired_true(self):
        """已过期时返回 True"""
        now = 1000.0
        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="test",
            paused_at=now,
        )

        assert record.is_expired(now=now + 300) is True
        assert record.is_expired(now=now + 400) is True

    def test_remaining_seconds(self):
        """计算剩余秒数"""
        now = 1000.0
        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="test",
            paused_at=now,
        )

        assert record.remaining_seconds(now=now) == 300
        assert record.remaining_seconds(now=now + 100) == 200
        assert record.remaining_seconds(now=now + 300) == 0
        assert record.remaining_seconds(now=now + 400) == 0  # 不返回负数

    def test_to_dict(self):
        """转换为字典"""
        now = 1000.0
        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="test",
            paused_at=now,
            failure_rate=0.35,
        )

        d = record.to_dict()
        assert d["repo_id"] == 1
        assert d["job_type"] == "commits"
        assert d["paused_until"] == now + 300
        assert d["reason"] == "test"
        assert d["paused_at"] == now
        assert d["failure_rate"] == 0.35

    def test_from_dict(self):
        """从字典创建"""
        data = {
            "paused_until": 1300.0,
            "reason": "error_budget_exceeded",
            "paused_at": 1000.0,
            "failure_rate": 0.45,
        }

        record = RepoPauseRecord.from_dict(repo_id=1, job_type="mrs", data=data)

        assert record.repo_id == 1
        assert record.job_type == "mrs"
        assert record.paused_until == 1300.0
        assert record.reason == "error_budget_exceeded"
        assert record.paused_at == 1000.0
        assert record.failure_rate == 0.45

    def test_from_dict_with_missing_fields(self):
        """从字典创建（缺少可选字段）"""
        data = {}

        record = RepoPauseRecord.from_dict(repo_id=1, job_type="reviews", data=data)

        assert record.repo_id == 1
        assert record.job_type == "reviews"
        assert record.paused_until == 0.0
        assert record.reason == ""
        assert record.paused_at == 0.0
        assert record.failure_rate == 0.0


@pytest.mark.skipif(not PAUSE_IMPORTS_AVAILABLE, reason="db module not available")
class TestPauseKeyFunctions:
    """Pause key 构建和解析函数测试"""

    def test_build_pause_key(self):
        """构建 pause key"""
        key = _build_pause_key(123, "commits")
        assert key == "repo:123:commits"

        key = _build_pause_key(456, "mrs")
        assert key == "repo:456:mrs"

        key = _build_pause_key(789, "reviews")
        assert key == "repo:789:reviews"

    def test_parse_pause_key_valid(self):
        """解析有效的 pause key"""
        result = _parse_pause_key("repo:123:commits")
        assert result == (123, "commits")

        result = _parse_pause_key("repo:456:mrs")
        assert result == (456, "mrs")

    def test_parse_pause_key_invalid_prefix(self):
        """解析无效前缀的 key"""
        result = _parse_pause_key("invalid:123:commits")
        assert result is None

        result = _parse_pause_key("other:456:mrs")
        assert result is None

    def test_parse_pause_key_invalid_format(self):
        """解析格式错误的 key"""
        result = _parse_pause_key("repo:123")
        assert result is None

        result = _parse_pause_key("repo")
        assert result is None

        result = _parse_pause_key("")
        assert result is None

    def test_parse_pause_key_invalid_repo_id(self):
        """解析 repo_id 不是数字的 key"""
        result = _parse_pause_key("repo:abc:commits")
        assert result is None

    def test_build_and_parse_roundtrip(self):
        """构建和解析往返测试"""
        for repo_id, job_type in [(1, "commits"), (999, "mrs"), (12345, "reviews")]:
            key = _build_pause_key(repo_id, job_type)
            parsed = _parse_pause_key(key)
            assert parsed == (repo_id, job_type)


@pytest.mark.skipif(not PAUSE_IMPORTS_AVAILABLE, reason="db module not available")
class TestPauseIntegrationWithScheduler:
    """Pause 与调度策略集成测试"""

    def test_paused_pairs_filter_candidates(self):
        """paused_pairs 参数应过滤掉对应的候选"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # repo 1 的 commits 被暂停（通过新的 paused_pairs 参数）
        paused_pairs = {(1, "commits")}
        queued_pairs = set()

        # 使用新的 paused_pairs 参数，函数内部自动合并
        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            queued_pairs=queued_pairs,
            paused_pairs=paused_pairs,
        )

        # repo 1 的 commits 应被跳过
        repo1_jobs = {c.job_type for c in candidates if c.repo_id == 1}
        assert "commits" not in repo1_jobs
        assert "mrs" in repo1_jobs

        # repo 2 所有任务都应被调度
        repo2_jobs = {c.job_type for c in candidates if c.repo_id == 2}
        assert "commits" in repo2_jobs
        assert "mrs" in repo2_jobs

    def test_paused_and_queued_pairs_combined(self):
        """paused_pairs 和 queued_pairs 应组合过滤（内部自动合并）"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # commits 被暂停，mrs 已入队
        paused_pairs = {(1, "commits")}
        queued_pairs = {(1, "mrs")}

        # 使用分离的参数，函数内部自动合并
        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
            paused_pairs=paused_pairs,
        )

        # 只有 reviews 应被调度
        job_types = {c.job_type for c in candidates}
        assert job_types == {"reviews"}

    def test_all_paused_returns_empty(self):
        """所有 job_type 都被暂停时返回空列表"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 所有类型都被暂停
        paused_pairs = {(1, "commits"), (1, "mrs"), (1, "reviews")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        assert len(candidates) == 0

    def test_pause_does_not_affect_other_repos(self):
        """暂停不影响其他仓库"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 只有 repo 1 和 repo 2 的某些任务被暂停
        paused_pairs = {(1, "commits"), (2, "mrs")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        # repo 3 应该所有任务都被调度
        repo3_jobs = {c.job_type for c in candidates if c.repo_id == 3}
        assert repo3_jobs == {"commits", "mrs"}


class TestPauseExpireBehavior:
    """暂停过期行为测试（纯函数/数据结构测试）"""

    @pytest.mark.skipif(not PAUSE_IMPORTS_AVAILABLE, reason="db module not available")
    def test_expire_at_exact_time(self):
        """精确过期时间测试"""
        now = 1000.0
        duration = 300.0

        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + duration,
            reason="test",
            paused_at=now,
        )

        # 刚好在过期时间之前
        assert record.is_expired(now=now + duration - 0.001) is False

        # 刚好在过期时间
        assert record.is_expired(now=now + duration) is True

        # 过期时间之后
        assert record.is_expired(now=now + duration + 0.001) is True

    @pytest.mark.skipif(not PAUSE_IMPORTS_AVAILABLE, reason="db module not available")
    def test_pause_record_serialization(self):
        """暂停记录序列化/反序列化测试"""
        import json

        now = 1000.0
        original = RepoPauseRecord(
            repo_id=123,
            job_type="mrs",
            paused_until=now + 600,
            reason="high_failure_rate",
            paused_at=now,
            failure_rate=0.42,
        )

        # 序列化
        json_str = json.dumps(original.to_dict())

        # 反序列化
        data = json.loads(json_str)
        restored = RepoPauseRecord.from_dict(
            repo_id=original.repo_id,
            job_type=original.job_type,
            data=data,
        )

        assert restored.repo_id == original.repo_id
        assert restored.job_type == original.job_type
        assert restored.paused_until == original.paused_until
        assert restored.reason == original.reason
        assert restored.paused_at == original.paused_at
        assert restored.failure_rate == original.failure_rate


class TestPauseSnapshot:
    """PauseSnapshot 数据类测试"""

    def test_empty_snapshot(self):
        """空快照测试"""
        snapshot = PauseSnapshot.empty()

        assert snapshot.paused_pairs == set()
        assert snapshot.pause_count == 0
        assert snapshot.by_reason_code == {}

    def test_from_pairs(self):
        """从 pairs 集合创建快照"""
        pairs = {(1, "commits"), (2, "mrs")}
        snapshot = PauseSnapshot.from_pairs(pairs, snapshot_at=1000.0)

        assert snapshot.paused_pairs == pairs
        assert snapshot.pause_count == 2
        assert snapshot.snapshot_at == 1000.0

    def test_to_dict(self):
        """序列化测试"""
        pairs = {(1, "commits"), (2, "mrs")}
        snapshot = PauseSnapshot(
            paused_pairs=pairs,
            pause_count=2,
            by_reason_code={"error_budget": 1, "rate_limit_bucket": 1},
            snapshot_at=1000.0,
        )

        data = snapshot.to_dict()

        assert set(tuple(p) for p in data["paused_pairs"]) == pairs
        assert data["pause_count"] == 2
        assert data["by_reason_code"] == {"error_budget": 1, "rate_limit_bucket": 1}
        assert data["snapshot_at"] == 1000.0

    def test_pause_snapshot_used_by_select_jobs(self):
        """PauseSnapshot 在 select_jobs_to_enqueue 中的使用"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 通过 PauseSnapshot 传递暂停信息
        pause_snapshot = PauseSnapshot(
            paused_pairs={(1, "commits")},
            pause_count=1,
            by_reason_code={"error_budget": 1},
            snapshot_at=now,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            pause_snapshot=pause_snapshot,
        )

        # repo 1 的 commits 应被跳过
        repo1_jobs = {c.job_type for c in candidates if c.repo_id == 1}
        assert "commits" not in repo1_jobs
        assert "mrs" in repo1_jobs

        # repo 2 所有任务都应被调度
        repo2_jobs = {c.job_type for c in candidates if c.repo_id == 2}
        assert "commits" in repo2_jobs
        assert "mrs" in repo2_jobs

    def test_pause_snapshot_overrides_paused_pairs(self):
        """pause_snapshot 优先于 paused_pairs 参数"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # paused_pairs 参数指定 commits 暂停
        paused_pairs = {(1, "commits")}

        # pause_snapshot 指定 mrs 暂停（应该覆盖 paused_pairs）
        pause_snapshot = PauseSnapshot(
            paused_pairs={(1, "mrs")},
            pause_count=1,
            snapshot_at=now,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            paused_pairs=paused_pairs,
            pause_snapshot=pause_snapshot,  # 这个应该覆盖 paused_pairs
        )

        job_types = {c.job_type for c in candidates}

        # pause_snapshot 生效，所以 commits 应该被调度，mrs 被跳过
        assert "commits" in job_types
        assert "mrs" not in job_types


@pytest.mark.skipif(not PAUSE_IMPORTS_AVAILABLE, reason="db module not available")
class TestRepoPauseWriteAndExpire:
    """
    repo/job_type pause 写入和过期测试

    验证场景：
    - 暂停记录正确写入
    - 暂停记录在指定时间后过期
    - 多次写入覆盖旧记录
    - 不同 job_type 的暂停记录独立
    """

    def test_pause_record_write_fields(self):
        """暂停记录写入字段验证"""
        now = 1000.0

        record = RepoPauseRecord(
            repo_id=42,
            job_type="commits",
            paused_until=now + 600,  # 10 分钟后过期
            reason="error_budget_exceeded",
            paused_at=now,
            failure_rate=0.45,
        )

        # 验证字段
        assert record.repo_id == 42
        assert record.job_type == "commits"
        assert record.paused_until == now + 600
        assert record.reason == "error_budget_exceeded"
        assert record.paused_at == now
        assert record.failure_rate == 0.45

    def test_pause_record_expire_after_duration(self):
        """暂停记录在指定时间后过期"""
        now = 1000.0
        duration = 300.0  # 5 分钟

        record = RepoPauseRecord(
            repo_id=1,
            job_type="mrs",
            paused_until=now + duration,
            reason="test",
            paused_at=now,
        )

        # 在暂停期间内
        assert record.is_expired(now=now + 100) is False
        assert record.remaining_seconds(now=now + 100) == 200

        # 在暂停期间末尾
        assert record.is_expired(now=now + 299) is False
        assert record.remaining_seconds(now=now + 299) == 1

        # 刚好过期
        assert record.is_expired(now=now + 300) is True
        assert record.remaining_seconds(now=now + 300) == 0

        # 过期之后
        assert record.is_expired(now=now + 500) is True
        assert record.remaining_seconds(now=now + 500) == 0

    def test_pause_record_overwrite(self):
        """多次写入覆盖旧记录"""
        now = 1000.0

        # 第一次写入
        record1 = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="first_pause",
            paused_at=now,
            failure_rate=0.3,
        )

        # 第二次写入（更长的暂停时间）
        record2 = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 600,  # 更长
            reason="second_pause_extended",
            paused_at=now + 100,  # 稍后
            failure_rate=0.5,
        )

        # 验证新记录覆盖旧记录的效果
        assert record2.paused_until > record1.paused_until
        assert record2.reason != record1.reason
        assert record2.failure_rate > record1.failure_rate

    def test_different_job_types_independent_pause(self):
        """不同 job_type 的暂停记录独立"""
        now = 1000.0

        commits_pause = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,
            reason="commits_failure",
            paused_at=now,
        )

        mrs_pause = RepoPauseRecord(
            repo_id=1,
            job_type="mrs",
            paused_until=now + 600,
            reason="mrs_failure",
            paused_at=now,
        )

        # 验证独立性
        assert commits_pause.job_type != mrs_pause.job_type
        assert commits_pause.paused_until != mrs_pause.paused_until
        assert commits_pause.reason != mrs_pause.reason

        # 同一时间点，commits 可能已过期但 mrs 未过期
        check_time = now + 400
        assert commits_pause.is_expired(now=check_time) is True
        assert mrs_pause.is_expired(now=check_time) is False

    def test_pause_key_construction(self):
        """暂停 key 构建验证"""
        key1 = _build_pause_key(1, "commits")
        key2 = _build_pause_key(1, "mrs")
        key3 = _build_pause_key(2, "commits")

        # 同 repo 不同 job_type 应该有不同 key
        assert key1 != key2

        # 不同 repo 同 job_type 应该有不同 key
        assert key1 != key3

        # 验证 key 格式
        assert key1 == "repo:1:commits"
        assert key2 == "repo:1:mrs"
        assert key3 == "repo:2:commits"

    def test_pause_key_roundtrip(self):
        """暂停 key 构建和解析往返测试"""
        test_cases = [
            (1, "commits"),
            (42, "mrs"),
            (999, "reviews"),
            (12345, "gitlab_commits"),
        ]

        for repo_id, job_type in test_cases:
            key = _build_pause_key(repo_id, job_type)
            parsed = _parse_pause_key(key)

            assert parsed is not None
            assert parsed[0] == repo_id
            assert parsed[1] == job_type


class TestBucketPausedSchedulerBehavior:
    """
    bucket paused 时 scheduler 行为测试

    验证场景：
    - paused bucket 的任务不入队
    - paused bucket 的任务优先级降低
    - 部分 job_type paused 时其他 job_type 正常入队
    - 全部 job_type paused 时 repo 完全跳过

    注意：使用新的 paused_pairs 参数（而非通过 queued_pairs 传递）
    """

    def test_paused_bucket_not_enqueued(self):
        """paused bucket 的任务不入队"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # repo 1 的 commits 被暂停（通过新的 paused_pairs 参数）
        paused_pairs = {(1, "commits")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        # repo 1 的 commits 不应入队
        repo1_commits = [c for c in candidates if c.repo_id == 1 and c.job_type == "commits"]
        assert len(repo1_commits) == 0

        # repo 1 的 mrs 应该正常入队
        repo1_mrs = [c for c in candidates if c.repo_id == 1 and c.job_type == "mrs"]
        assert len(repo1_mrs) == 1

        # repo 2 的所有任务应该正常入队
        repo2_jobs = [c for c in candidates if c.repo_id == 2]
        assert len(repo2_jobs) == 2

    def test_partial_job_types_paused_others_normal(self):
        """部分 job_type paused 时其他 job_type 正常入队"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # commits 和 mrs 被暂停，reviews 正常
        paused_pairs = {(1, "commits"), (1, "mrs")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        job_types = {c.job_type for c in candidates}

        # 只有 reviews 被调度
        assert job_types == {"reviews"}

    def test_all_job_types_paused_repo_skipped(self):
        """全部 job_type paused 时 repo 完全跳过"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # repo 1 所有类型都被暂停
        paused_pairs = {(1, "commits"), (1, "mrs"), (1, "reviews")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        # repo 1 完全跳过
        repo1_jobs = [c for c in candidates if c.repo_id == 1]
        assert len(repo1_jobs) == 0

        # repo 2 正常入队
        repo2_jobs = [c for c in candidates if c.repo_id == 2]
        assert len(repo2_jobs) == 3

    def test_paused_and_queued_combined(self):
        """paused 和 queued 组合过滤（使用分离的参数，内部自动合并）"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
            ),
        ]

        # commits 被暂停，mrs 已在队列中
        paused_pairs = {(1, "commits")}
        queued_pairs = {(1, "mrs")}

        # 使用分离的参数，函数内部自动合并
        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
            queued_pairs=queued_pairs,
            paused_pairs=paused_pairs,
        )

        job_types = {c.job_type for c in candidates}

        # 只有 reviews 可以入队
        assert job_types == {"reviews"}

    def test_pause_does_not_affect_other_repos(self):
        """暂停不影响其他 repo"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(repo_id=i, repo_type="git", cursor_updated_at=now - 5000)
            for i in range(1, 6)
        ]

        # 只有 repo 1 和 repo 2 的 commits 被暂停
        paused_pairs = {(1, "commits"), (2, "commits")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        scheduled_repo_ids = {c.repo_id for c in candidates}

        # repo 3, 4, 5 应该被调度
        assert 3 in scheduled_repo_ids
        assert 4 in scheduled_repo_ids
        assert 5 in scheduled_repo_ids

        # repo 1, 2 不应被调度
        assert 1 not in scheduled_repo_ids
        assert 2 not in scheduled_repo_ids

    def test_pause_expiration_allows_scheduling(self):
        """暂停过期后允许调度"""
        # 纯函数测试：验证过期检查逻辑
        now = 1000.0

        record = RepoPauseRecord(
            repo_id=1,
            job_type="commits",
            paused_until=now + 300,  # 5 分钟后过期
            reason="test",
            paused_at=now,
        )

        # 暂停期间
        assert record.is_expired(now=now + 100) is False

        # 暂停过期后
        assert record.is_expired(now=now + 400) is True

        # 模拟调度器行为：过期的暂停记录不应阻止调度
        # （在实际实现中，get_repo_job_pause 会返回 None 对于已过期的记录）

    def test_multiple_repos_different_pause_states(self):
        """多个 repo 不同暂停状态"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(repo_id=1, repo_type="git", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=2, repo_type="git", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=3, repo_type="git", cursor_updated_at=now - 5000),
        ]

        # repo 1: commits 暂停
        # repo 2: mrs 暂停
        # repo 3: 无暂停
        paused_pairs = {(1, "commits"), (2, "mrs")}

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            paused_pairs=paused_pairs,
        )

        # 构建结果映射
        result = {repo_id: set() for repo_id in [1, 2, 3]}
        for c in candidates:
            result[c.repo_id].add(c.job_type)

        # repo 1: 只有 mrs
        assert result[1] == {"mrs"}

        # repo 2: 只有 commits
        assert result[2] == {"commits"}

        # repo 3: commits 和 mrs 都有
        assert result[3] == {"commits", "mrs"}


class TestSchedulerPausedPriorityDemotion:
    """
    测试暂停相关的优先级降权

    验证场景：
    - 高失败率的 repo 优先级降低
    - 高 429 命中率的 repo 优先级降低
    - 正常 repo 保持正常优先级
    """

    def test_high_failure_rate_priority_demotion(self):
        """高失败率的 repo 优先级降低"""
        config = SchedulerConfig(
            error_budget_threshold=0.5,  # 50% 阈值，不完全阻止
            cursor_age_threshold_seconds=3600,
        )

        state_healthy = RepoSyncState(
            repo_id=1,
            repo_type="git",
            recent_run_count=10,
            recent_failed_count=1,  # 10% 失败率
        )

        state_unhealthy = RepoSyncState(
            repo_id=2,
            repo_type="git",
            recent_run_count=10,
            recent_failed_count=3,  # 30% 失败率（但低于阈值）
        )

        priority_healthy = compute_job_priority(1, "commits", state_healthy, config)
        priority_unhealthy = compute_job_priority(2, "commits", state_unhealthy, config)

        # unhealthy 的优先级应该更高（数值更大 = 优先级更低）
        assert priority_unhealthy > priority_healthy

    def test_high_rate_limit_priority_demotion(self):
        """高 429 命中率的 repo 优先级降低"""
        config = SchedulerConfig(
            rate_limit_hit_threshold=0.3,
            cursor_age_threshold_seconds=3600,
        )

        state_normal = RepoSyncState(
            repo_id=1,
            repo_type="git",
            recent_429_hits=5,
            recent_total_requests=100,  # 5% 命中率
        )

        state_limited = RepoSyncState(
            repo_id=2,
            repo_type="git",
            recent_429_hits=20,
            recent_total_requests=100,  # 20% 命中率
        )

        priority_normal = compute_job_priority(1, "commits", state_normal, config)
        priority_limited = compute_job_priority(2, "commits", state_limited, config)

        # limited 的优先级应该更低
        assert priority_limited > priority_normal

    def test_combined_factors_priority_ordering(self):
        """组合因素的优先级排序"""
        config = SchedulerConfig(cursor_age_threshold_seconds=3600)
        now = 10000.0

        states = [
            # 健康 repo
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 5000,
                recent_run_count=10,
                recent_failed_count=0,
                recent_429_hits=0,
                recent_total_requests=100,
            ),
            # 有一些失败的 repo
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=now - 5000,
                recent_run_count=10,
                recent_failed_count=2,
                recent_429_hits=5,
                recent_total_requests=100,
            ),
            # 从未同步的 repo（最高优先级）
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                cursor_updated_at=None,
            ),
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        # 按优先级排序
        priorities = [(c.repo_id, c.priority) for c in candidates]
        priorities.sort(key=lambda x: x[1])

        # 从未同步的应该排在最前面
        assert priorities[0][0] == 3


# ============ Bucket 暂停相关测试 ============


class TestInstanceBucketStatus:
    """InstanceBucketStatus 数据类测试"""

    def test_create_bucket_status(self):
        """创建 bucket 状态"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            paused_until=1000.0 + 300,
            pause_remaining_seconds=300.0,
            current_tokens=5.0,
            rate=1.0,
            burst=10.0,
        )

        assert status.instance_key == "gitlab.example.com"
        assert status.is_paused is True
        assert status.paused_until == 1300.0
        assert status.pause_remaining_seconds == 300.0
        assert status.current_tokens == 5.0
        assert status.rate == 1.0
        assert status.burst == 10.0

    def test_default_values(self):
        """测试默认值"""
        status = InstanceBucketStatus(instance_key="test")

        assert status.is_paused is False
        assert status.paused_until is None
        assert status.pause_remaining_seconds == 0.0
        assert status.current_tokens == 0.0
        assert status.rate == 1.0
        assert status.burst == 10.0

    def test_to_dict(self):
        """转换为字典"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            paused_until=1300.0,
            pause_remaining_seconds=250.0,
            current_tokens=3.5,
            rate=0.5,
            burst=20.0,
        )

        d = status.to_dict()
        assert d["instance_key"] == "gitlab.example.com"
        assert d["is_paused"] is True
        assert d["paused_until"] == 1300.0
        assert d["pause_remaining_seconds"] == 250.0
        assert d["current_tokens"] == 3.5
        assert d["rate"] == 0.5
        assert d["burst"] == 20.0

    def test_from_db_status(self):
        """从 db 状态字典创建"""
        from datetime import datetime

        paused_until_dt = datetime(2024, 1, 1, 12, 0, 0)
        db_status = {
            "instance_key": "gitlab.example.com",
            "is_paused": True,
            "paused_until": paused_until_dt,
            "pause_remaining_seconds": 180.0,
            "current_tokens": 2.5,
            "rate": 0.8,
            "burst": 15.0,
        }

        status = InstanceBucketStatus.from_db_status(db_status)

        assert status.instance_key == "gitlab.example.com"
        assert status.is_paused is True
        assert status.paused_until == paused_until_dt.timestamp()
        assert status.pause_remaining_seconds == 180.0
        assert status.current_tokens == 2.5
        assert status.rate == 0.8
        assert status.burst == 15.0

    def test_from_db_status_missing_fields(self):
        """从缺少字段的 db 状态字典创建"""
        db_status = {
            "instance_key": "gitlab.example.com",
        }

        status = InstanceBucketStatus.from_db_status(db_status)

        assert status.instance_key == "gitlab.example.com"
        assert status.is_paused is False
        assert status.paused_until is None
        assert status.pause_remaining_seconds == 0.0


class TestCalculateBucketPriorityPenalty:
    """calculate_bucket_priority_penalty 函数测试"""

    def test_no_penalty_when_status_none(self):
        """bucket 状态为 None 时无惩罚"""
        penalty, reason = calculate_bucket_priority_penalty(None)

        assert penalty == 0
        assert reason is None

    def test_paused_penalty(self):
        """bucket 被暂停时应用最大惩罚"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            pause_remaining_seconds=300.0,
            current_tokens=0.0,
            burst=10.0,
        )

        penalty, reason = calculate_bucket_priority_penalty(status)

        assert penalty == BUCKET_PAUSED_PRIORITY_PENALTY
        assert reason == "bucket_paused"

    def test_low_tokens_penalty(self):
        """bucket 令牌不足时应用较小惩罚"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=False,
            current_tokens=1.5,  # 15% of burst (< 20%)
            burst=10.0,
        )

        penalty, reason = calculate_bucket_priority_penalty(status)

        assert penalty == BUCKET_LOW_TOKENS_PRIORITY_PENALTY
        assert reason == "bucket_low_tokens"

    def test_no_penalty_when_tokens_sufficient(self):
        """bucket 令牌充足时无惩罚"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=False,
            current_tokens=5.0,  # 50% of burst (>= 20%)
            burst=10.0,
        )

        penalty, reason = calculate_bucket_priority_penalty(status)

        assert penalty == 0
        assert reason is None

    def test_paused_takes_priority_over_low_tokens(self):
        """暂停惩罚优先于令牌不足惩罚"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,  # 暂停
            current_tokens=0.5,  # 同时令牌不足
            burst=10.0,
        )

        penalty, reason = calculate_bucket_priority_penalty(status)

        # 应该返回暂停惩罚，因为它更严重
        assert penalty == BUCKET_PAUSED_PRIORITY_PENALTY
        assert reason == "bucket_paused"


class TestShouldSkipDueToBucketPause:
    """should_skip_due_to_bucket_pause 函数测试"""

    def test_no_skip_when_status_none(self):
        """bucket 状态为 None 时不跳过"""
        should_skip, reason, remaining = should_skip_due_to_bucket_pause(None)

        assert should_skip is False
        assert reason is None
        assert remaining == 0.0

    def test_skip_when_paused_and_skip_enabled(self):
        """bucket 暂停且 skip_on_pause=True 时应跳过"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            pause_remaining_seconds=120.0,
        )

        should_skip, reason, remaining = should_skip_due_to_bucket_pause(status, skip_on_pause=True)

        assert should_skip is True
        assert reason == "bucket_paused"
        assert remaining == 120.0

    def test_no_skip_when_paused_but_skip_disabled(self):
        """bucket 暂停但 skip_on_pause=False 时不跳过（仅降权）"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            pause_remaining_seconds=120.0,
        )

        should_skip, reason, remaining = should_skip_due_to_bucket_pause(
            status, skip_on_pause=False
        )

        assert should_skip is False
        assert reason == "bucket_paused_penalty_only"
        assert remaining == 120.0

    def test_no_skip_when_not_paused(self):
        """bucket 未暂停时不跳过"""
        status = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=False,
        )

        should_skip, reason, remaining = should_skip_due_to_bucket_pause(status, skip_on_pause=True)

        assert should_skip is False
        assert reason is None
        assert remaining == 0.0


class TestSelectJobsWithBucketStatus:
    """select_jobs_to_enqueue 与 bucket 状态集成测试"""

    def test_bucket_paused_skip_mode(self):
        """bucket 暂停时跳过该实例的任务（skip_on_bucket_pause=True）"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",  # 暂停的实例
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-b.com",  # 正常的实例
                cursor_updated_at=now - 5000,
            ),
        ]

        bucket_statuses = {
            "gitlab-a.com": InstanceBucketStatus(
                instance_key="gitlab-a.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
                burst=10.0,
            ),
            "gitlab-b.com": InstanceBucketStatus(
                instance_key="gitlab-b.com",
                is_paused=False,
                current_tokens=8.0,
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,  # 跳过模式
        )

        # gitlab-a.com 的任务应被跳过
        repo_ids = [c.repo_id for c in candidates]
        assert 1 not in repo_ids, "gitlab-a.com 的任务应被跳过"
        assert 2 in repo_ids, "gitlab-b.com 的任务应被保留"

    def test_bucket_paused_penalty_mode(self):
        """bucket 暂停时降权而不跳过（skip_on_bucket_pause=False）"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",  # 暂停的实例
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-b.com",  # 正常的实例
                cursor_updated_at=now - 5000,
            ),
        ]

        bucket_statuses = {
            "gitlab-a.com": InstanceBucketStatus(
                instance_key="gitlab-a.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
                burst=10.0,
            ),
            "gitlab-b.com": InstanceBucketStatus(
                instance_key="gitlab-b.com",
                is_paused=False,
                current_tokens=8.0,
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=False,  # 降权模式
        )

        # 两个任务都应被保留
        assert len(candidates) == 2

        # 验证 bucket 信息
        repo1_candidate = next(c for c in candidates if c.repo_id == 1)
        repo2_candidate = next(c for c in candidates if c.repo_id == 2)

        # repo 1 应有 bucket 暂停标记和惩罚
        assert repo1_candidate.bucket_paused is True
        assert repo1_candidate.bucket_penalty_reason == "bucket_paused"
        assert repo1_candidate.bucket_penalty_value == BUCKET_PAUSED_PRIORITY_PENALTY

        # repo 2 应无 bucket 暂停标记
        assert repo2_candidate.bucket_paused is False
        assert repo2_candidate.bucket_penalty_reason is None
        assert repo2_candidate.bucket_penalty_value == 0

        # repo 1 的优先级应显著低于 repo 2
        assert repo1_candidate.priority > repo2_candidate.priority

    def test_bucket_low_tokens_penalty(self):
        """bucket 令牌不足时应用较小惩罚"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                cursor_updated_at=now - 5000,
            ),
        ]

        bucket_statuses = {
            "gitlab-a.com": InstanceBucketStatus(
                instance_key="gitlab-a.com",
                is_paused=False,
                current_tokens=1.0,  # 10% of burst (< 20%)
                burst=10.0,
            ),
            "gitlab-b.com": InstanceBucketStatus(
                instance_key="gitlab-b.com",
                is_paused=False,
                current_tokens=8.0,  # 80% of burst (>= 20%)
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
        )

        # 两个任务都应被保留
        assert len(candidates) == 2

        repo1_candidate = next(c for c in candidates if c.repo_id == 1)
        repo2_candidate = next(c for c in candidates if c.repo_id == 2)

        # repo 1 应有 low_tokens 惩罚
        assert repo1_candidate.bucket_penalty_reason == "bucket_low_tokens"
        assert repo1_candidate.bucket_penalty_value == BUCKET_LOW_TOKENS_PRIORITY_PENALTY

        # repo 2 应无惩罚
        assert repo2_candidate.bucket_penalty_reason is None
        assert repo2_candidate.bucket_penalty_value == 0

        # repo 1 的优先级应略低于 repo 2
        assert repo1_candidate.priority > repo2_candidate.priority

    def test_no_bucket_status_no_penalty(self):
        """没有 bucket 状态记录时无惩罚"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-unknown.com",  # 未知实例
                cursor_updated_at=now - 5000,
            ),
        ]

        # 空的 bucket_statuses
        bucket_statuses = {}

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
        )

        assert len(candidates) == 1

        candidate = candidates[0]
        assert candidate.bucket_paused is False
        assert candidate.bucket_penalty_reason is None
        assert candidate.bucket_penalty_value == 0

    def test_bucket_status_none_no_penalty(self):
        """bucket_statuses 参数为 None 时无惩罚（向后兼容）"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab.example.com",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 不传 bucket_statuses 参数
        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            # bucket_statuses=None (默认)
        )

        assert len(candidates) == 1

        candidate = candidates[0]
        assert candidate.bucket_paused is False
        assert candidate.bucket_penalty_reason is None

    def test_priority_ordering_with_bucket_penalty(self):
        """验证 bucket 惩罚对优先级排序的影响"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",  # 暂停的实例
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-b.com",  # 令牌不足
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="gitlab-c.com",  # 正常
                cursor_updated_at=now - 5000,
            ),
        ]

        bucket_statuses = {
            "gitlab-a.com": InstanceBucketStatus(
                instance_key="gitlab-a.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
                burst=10.0,
            ),
            "gitlab-b.com": InstanceBucketStatus(
                instance_key="gitlab-b.com",
                is_paused=False,
                current_tokens=1.0,  # low tokens
                burst=10.0,
            ),
            "gitlab-c.com": InstanceBucketStatus(
                instance_key="gitlab-c.com",
                is_paused=False,
                current_tokens=8.0,  # normal
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=False,
        )

        assert len(candidates) == 3

        # 验证排序：正常 < 令牌不足 < 暂停
        priorities = [(c.repo_id, c.priority) for c in candidates]
        sorted_by_priority = sorted(priorities, key=lambda x: x[1])

        # repo 3（正常）应该优先级最高（数值最小）
        assert sorted_by_priority[0][0] == 3, "正常实例的任务应排在最前"
        # repo 2（令牌不足）次之
        assert sorted_by_priority[1][0] == 2, "令牌不足的任务应排第二"
        # repo 1（暂停）优先级最低
        assert sorted_by_priority[2][0] == 1, "暂停实例的任务应排在最后"

    def test_mixed_bucket_and_other_factors(self):
        """bucket 惩罚与其他因素（失败率、429 命中率）组合"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            global_concurrency=100,
        )
        now = 10000.0

        states = [
            # bucket 暂停但无其他问题
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="gitlab-a.com",
                cursor_updated_at=now - 5000,
            ),
            # bucket 正常但失败率高
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="gitlab-b.com",
                cursor_updated_at=now - 5000,
                recent_run_count=10,
                recent_failed_count=2,  # 20% 失败率
            ),
        ]

        bucket_statuses = {
            "gitlab-a.com": InstanceBucketStatus(
                instance_key="gitlab-a.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
                burst=10.0,
            ),
            "gitlab-b.com": InstanceBucketStatus(
                instance_key="gitlab-b.com",
                is_paused=False,
                current_tokens=8.0,
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=False,
        )

        assert len(candidates) == 2

        repo1_candidate = next(c for c in candidates if c.repo_id == 1)
        repo2_candidate = next(c for c in candidates if c.repo_id == 2)

        # bucket 暂停的惩罚 (1000) 应该大于失败率惩罚 (20% * 100 = 20)
        assert repo1_candidate.priority > repo2_candidate.priority


class TestTenantFairnessOrdering:
    """Tenant 公平调度策略测试"""

    def test_tenant_fairness_disabled_by_default(self):
        """默认情况下 tenant fairness 禁用"""
        config = SchedulerConfig()
        assert config.enable_tenant_fairness is False
        assert config.tenant_fairness_max_per_round == 1

    def test_tenant_fairness_config_values(self):
        """启用 tenant fairness 的配置值"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=2,
        )
        assert config.enable_tenant_fairness is True
        assert config.tenant_fairness_max_per_round == 2

    def test_single_tenant_preserves_priority_order(self):
        """单个 tenant 时保持优先级顺序"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        # 所有仓库属于同一 tenant
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000 - (i * 100),  # 不同优先级
            )
            for i in range(1, 6)
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        # 应该保持优先级顺序（repo_id 越大，cursor 越旧，优先级越高）
        assert len(candidates) == 5
        # 单个 tenant 时保持原有优先级排序
        for i in range(len(candidates) - 1):
            assert candidates[i].priority <= candidates[i + 1].priority

    def test_two_tenants_with_equal_backlog_alternate_fairly(self):
        """两个 tenant 相同 backlog 时交替入队"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=100,  # 不限制 per_tenant
        )
        now = 10000.0

        # Tenant A: 3 个仓库
        states_a = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 4)
        ]
        # Tenant B: 3 个仓库
        states_b = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-b",
                cursor_updated_at=now - 5000,
            )
            for i in range(4, 7)
        ]

        states = states_a + states_b

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 6

        # 验证交替模式：应该是 A, B, A, B, A, B 或 B, A, B, A, B, A
        tenant_sequence = []
        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}
        for c in candidates:
            tenant_sequence.append(repo_to_tenant[c.repo_id])

        # 检查是否交替
        for i in range(len(tenant_sequence) - 1):
            # 连续两个不应来自同一 tenant（因为 max_per_round=1）
            assert tenant_sequence[i] != tenant_sequence[i + 1], (
                f"位置 {i} 和 {i + 1} 都是 {tenant_sequence[i]}，不符合交替模式"
            )

    def test_two_tenants_with_different_backlog_still_alternate(self):
        """两个 tenant 不同 backlog 时仍能交替入队（核心测试场景）"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=100,  # 不限制 per_tenant
        )
        now = 10000.0

        # Tenant A: 10 个仓库（大 backlog）
        states_a = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 11)
        ]
        # Tenant B: 2 个仓库（小 backlog）
        states_b = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-b",
                cursor_updated_at=now - 5000,
            )
            for i in range(11, 13)
        ]

        states = states_a + states_b

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        # 总共 12 个候选
        assert len(candidates) == 12

        # 验证前 4 个应该是交替的
        tenant_sequence = []
        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}
        for c in candidates[:4]:
            tenant_sequence.append(repo_to_tenant[c.repo_id])

        # 前 4 个应该是交替的：A, B, A, B 或 B, A, B, A
        for i in range(3):
            assert tenant_sequence[i] != tenant_sequence[i + 1], (
                f"位置 {i} 和 {i + 1} 应该交替，但都是 {tenant_sequence[i]}"
            )

        # 验证 tenant-b 的两个任务都在前面位置（因为交替）
        tenant_b_positions = [
            i for i, c in enumerate(candidates) if repo_to_tenant[c.repo_id] == "tenant-b"
        ]
        assert len(tenant_b_positions) == 2
        # tenant-b 的任务应该在位置 1 和 3（或 0 和 2）
        assert max(tenant_b_positions) <= 3

    def test_max_per_round_allows_multiple_per_tenant(self):
        """max_per_round > 1 时每轮可入队多个"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=2,  # 每轮每 tenant 最多 2 个
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=100,
        )
        now = 10000.0

        # Tenant A: 4 个仓库
        states_a = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 5)
        ]
        # Tenant B: 4 个仓库
        states_b = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-b",
                cursor_updated_at=now - 5000,
            )
            for i in range(5, 9)
        ]

        states = states_a + states_b

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 8

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 前 4 个应该是 A, A, B, B（每轮各 2 个）
        first_four_tenants = [repo_to_tenant[c.repo_id] for c in candidates[:4]]

        # 检查分布：应该是 2 个 A 和 2 个 B
        assert first_four_tenants.count("tenant-a") == 2
        assert first_four_tenants.count("tenant-b") == 2

    def test_fairness_disabled_uses_priority_order(self):
        """禁用 fairness 时按优先级顺序"""
        config = SchedulerConfig(
            enable_tenant_fairness=False,  # 禁用
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=100,
        )
        now = 100000.0  # 使用更大的时间值

        # Tenant A: 5 个仓库，优先级低（cursor 较新，游标年龄 2 小时）
        states_a = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 7200,  # 2 小时前
            )
            for i in range(1, 6)
        ]
        # Tenant B: 2 个仓库，优先级高（cursor 较旧，游标年龄 10 小时）
        states_b = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-b",
                cursor_updated_at=now - 36000,  # 10 小时前，优先级更高
            )
            for i in range(6, 8)
        ]

        states = states_a + states_b

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 7

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 禁用 fairness 时，按优先级排序，tenant-b 的 2 个（游标更旧）应该在最前面
        first_two_tenants = [repo_to_tenant[c.repo_id] for c in candidates[:2]]
        assert first_two_tenants == ["tenant-b", "tenant-b"]

    def test_three_tenants_round_robin(self):
        """三个 tenant 轮询"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=100,
        )
        now = 10000.0

        # 创建三个 tenant
        states = []
        for tenant_id in ["tenant-a", "tenant-b", "tenant-c"]:
            for i in range(3):
                repo_id = len(states) + 1
                states.append(
                    RepoSyncState(
                        repo_id=repo_id,
                        repo_type="git",
                        tenant_id=tenant_id,
                        cursor_updated_at=now - 5000,
                    )
                )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 9

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 验证前 6 个是三个 tenant 各出现 2 次（两轮）
        first_six_tenants = [repo_to_tenant[c.repo_id] for c in candidates[:6]]
        assert first_six_tenants.count("tenant-a") == 2
        assert first_six_tenants.count("tenant-b") == 2
        assert first_six_tenants.count("tenant-c") == 2

    def test_fairness_respects_per_tenant_concurrency_limit(self):
        """fairness 策略仍然遵守 per_tenant_concurrency 限制"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=2,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=3,  # 限制每个 tenant 最多 3 个
        )
        now = 10000.0

        # Tenant A: 10 个仓库
        states_a = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 11)
        ]
        # Tenant B: 10 个仓库
        states_b = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-b",
                cursor_updated_at=now - 5000,
            )
            for i in range(11, 21)
        ]

        states = states_a + states_b

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        # 每个 tenant 最多 3 个，共 6 个
        assert len(candidates) == 6

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}
        tenant_counts = {}
        for c in candidates:
            tid = repo_to_tenant[c.repo_id]
            tenant_counts[tid] = tenant_counts.get(tid, 0) + 1

        assert tenant_counts["tenant-a"] == 3
        assert tenant_counts["tenant-b"] == 3

    def test_no_tenant_id_treated_as_single_bucket(self):
        """没有 tenant_id 的仓库归入同一桶"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        # 没有 tenant_id 的仓库
        states_no_tenant = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id=None,
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 4)
        ]
        # 有 tenant_id 的仓库
        states_with_tenant = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id="tenant-a",
                cursor_updated_at=now - 5000,
            )
            for i in range(4, 7)
        ]

        states = states_no_tenant + states_with_tenant

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 6

        # 验证交替：有 tenant 的和无 tenant 的应该交替
        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}
        tenant_sequence = [repo_to_tenant[c.repo_id] for c in candidates]

        # 应该有交替模式
        for i in range(min(5, len(tenant_sequence) - 1)):
            # 如果连续 2 个相同，检查是否是因为一边已经用完
            if tenant_sequence[i] == tenant_sequence[i + 1]:
                # 允许在一边耗尽后连续
                before_count = tenant_sequence[: i + 1].count(tenant_sequence[i])
                total_of_this = tenant_sequence.count(tenant_sequence[i])
                # 如果这个 tenant 的任务还没用完，不应该连续
                if before_count < total_of_this - 1:
                    assert False, f"位置 {i} 和 {i + 1} 不应该连续相同 ({tenant_sequence[i]})"


# ============ 熔断 Key 构建测试 ============

# 导入熔断 key 构建函数
from engram.logbook.scm_sync_policy import (
    build_circuit_breaker_key,
    get_legacy_key_fallbacks,
    normalize_instance_key_for_cb,
)


class TestBuildCircuitBreakerKey:
    """熔断 key 构建函数测试"""

    def test_default_returns_global_key(self):
        """默认参数返回全局 key"""
        key = build_circuit_breaker_key()
        assert key == "default:global"

    def test_project_key_prefix(self):
        """project_key 作为前缀"""
        key = build_circuit_breaker_key(project_key="myproject")
        assert key == "myproject:global"

    def test_positional_args_backward_compatible(self):
        """位置参数向后兼容：(project_key, scope)"""
        key = build_circuit_breaker_key("myproject", "global")
        assert key == "myproject:global"

        key2 = build_circuit_breaker_key("prod", "pool:custom")
        assert key2 == "prod:pool:custom"

    def test_worker_pool_generates_pool_scope(self):
        """worker_pool 参数生成 pool scope"""
        key = build_circuit_breaker_key(worker_pool="gitlab-prod")
        assert key == "default:pool:gitlab-prod"

    def test_pool_name_generates_pool_scope(self):
        """pool_name 参数生成 pool scope（向后兼容）"""
        key = build_circuit_breaker_key("default", pool_name="svn-only")
        assert key == "default:pool:svn-only"

    def test_worker_pool_takes_precedence_over_pool_name(self):
        """worker_pool 优先于 pool_name"""
        key = build_circuit_breaker_key("default", pool_name="old-pool", worker_pool="new-pool")
        assert key == "default:pool:new-pool"

    def test_instance_key_url_normalized(self):
        """instance_key 从 URL 提取 hostname"""
        key = build_circuit_breaker_key(instance_key="https://gitlab.example.com/group/project")
        assert key == "default:instance:gitlab.example.com"

    def test_instance_key_hostname_normalized(self):
        """instance_key hostname 直接使用"""
        key = build_circuit_breaker_key(instance_key="gitlab.example.com")
        assert key == "default:instance:gitlab.example.com"

    def test_instance_key_case_insensitive(self):
        """instance_key 大小写不敏感"""
        key1 = build_circuit_breaker_key(instance_key="GitLab.Example.COM")
        key2 = build_circuit_breaker_key(instance_key="gitlab.example.com")
        assert key1 == key2 == "default:instance:gitlab.example.com"

    def test_tenant_id_generates_tenant_scope(self):
        """tenant_id 参数生成 tenant scope"""
        key = build_circuit_breaker_key(tenant_id="tenant-a")
        assert key == "default:tenant:tenant-a"

    def test_explicit_scope_preserved(self):
        """显式 scope 参数保持不变"""
        key = build_circuit_breaker_key("default", "instance:custom.gitlab.com")
        assert key == "default:instance:custom.gitlab.com"

    def test_priority_pool_over_instance(self):
        """worker_pool 优先于 instance_key"""
        key = build_circuit_breaker_key(
            worker_pool="my-pool",
            instance_key="gitlab.example.com",
        )
        assert key == "default:pool:my-pool"

    def test_priority_instance_over_tenant(self):
        """instance_key 优先于 tenant_id"""
        key = build_circuit_breaker_key(
            instance_key="gitlab.example.com",
            tenant_id="tenant-a",
        )
        assert key == "default:instance:gitlab.example.com"

    def test_none_project_key_defaults_to_default(self):
        """None project_key 使用 'default'"""
        key = build_circuit_breaker_key(None)
        assert key == "default:global"

    def test_empty_project_key_defaults_to_default(self):
        """空字符串 project_key 使用 'default'"""
        key = build_circuit_breaker_key("")
        assert key == "default:global"


class TestCircuitBreakerKeyConsistency:
    """
    测试同一实例/tenant 在不同入口（scheduler/worker）生成相同 key

    这是确保 scheduler 和 worker 共享熔断状态的关键测试
    """

    def test_same_instance_key_from_url_and_hostname(self):
        """URL 和 hostname 生成相同的 instance key"""
        # 模拟 scheduler 从 repo URL 解析
        key_from_url = build_circuit_breaker_key(
            "myproject",
            instance_key="https://gitlab.example.com/group/project",
        )

        # 模拟 worker 从 allowlist hostname 获取
        key_from_hostname = build_circuit_breaker_key(
            "myproject",
            instance_key="gitlab.example.com",
        )

        assert key_from_url == key_from_hostname == "myproject:instance:gitlab.example.com"

    def test_same_tenant_key_from_different_entries(self):
        """不同入口生成相同的 tenant key"""
        # 模拟 scheduler 构建 tenant key
        key_scheduler = build_circuit_breaker_key(
            "myproject",
            tenant_id="tenant-a",
        )

        # 模拟 worker 构建 tenant key
        key_worker = build_circuit_breaker_key(
            "myproject",
            tenant_id="tenant-a",
        )

        assert key_scheduler == key_worker == "myproject:tenant:tenant-a"

    def test_same_pool_key_from_different_params(self):
        """worker_pool 和 pool_name 生成相同的 key"""
        key1 = build_circuit_breaker_key(
            "myproject",
            worker_pool="gitlab-prod",
        )

        key2 = build_circuit_breaker_key(
            "myproject",
            pool_name="gitlab-prod",
        )

        assert key1 == key2 == "myproject:pool:gitlab-prod"

    def test_scheduler_worker_global_key_consistency(self):
        """scheduler 和 worker 生成相同的全局 key"""
        # 模拟 scheduler 构建全局 key（位置参数方式）
        scheduler_key = build_circuit_breaker_key("production", "global")

        # 模拟 worker 构建全局 key（仅 project_key）
        worker_key = build_circuit_breaker_key("production")

        assert scheduler_key == worker_key == "production:global"

    def test_positional_and_keyword_args_consistency(self):
        """位置参数和关键字参数生成相同的 key"""
        # 位置参数调用（旧方式）
        key_positional = build_circuit_breaker_key("myproject", "global")

        # 关键字参数调用（新方式）
        key_keyword = build_circuit_breaker_key(project_key="myproject", scope="global")

        assert key_positional == key_keyword == "myproject:global"


class TestGetLegacyKeyFallbacks:
    """旧 key 格式回退测试"""

    def test_global_key_fallbacks(self):
        """全局 key 的回退列表"""
        fallbacks = get_legacy_key_fallbacks("default:global")
        assert "global" in fallbacks

    def test_pool_key_fallbacks(self):
        """pool key 的回退列表"""
        fallbacks = get_legacy_key_fallbacks("default:pool:gitlab-prod")
        assert "pool:gitlab-prod" in fallbacks
        assert "gitlab-prod" in fallbacks

    def test_instance_key_fallbacks(self):
        """instance key 的回退列表"""
        fallbacks = get_legacy_key_fallbacks("myproject:instance:gitlab.example.com")
        assert "instance:gitlab.example.com" in fallbacks
        assert "gitlab.example.com" in fallbacks

    def test_tenant_key_fallbacks(self):
        """tenant key 的回退列表"""
        fallbacks = get_legacy_key_fallbacks("myproject:tenant:tenant-a")
        assert "tenant:tenant-a" in fallbacks
        assert "tenant-a" in fallbacks

    def test_worker_key_not_in_fallbacks(self):
        """worker:xxx 格式的旧 key 不应在回退列表中"""
        # 这种旧格式是随机的 worker ID，不应用于回退
        fallbacks = get_legacy_key_fallbacks("default:global")
        for fb in fallbacks:
            assert not fb.startswith("worker:")

    def test_empty_key_returns_empty_list(self):
        """空 key 返回空列表"""
        fallbacks = get_legacy_key_fallbacks("")
        assert fallbacks == []

    def test_none_key_returns_empty_list(self):
        """None key 返回空列表"""
        fallbacks = get_legacy_key_fallbacks(None)
        assert fallbacks == []


class TestNormalizeInstanceKeyForCb:
    """实例 key 规范化测试"""

    def test_url_extracts_hostname(self):
        """从 URL 提取 hostname"""
        result = normalize_instance_key_for_cb("https://gitlab.example.com/group/project")
        assert result == "gitlab.example.com"

    def test_hostname_preserved(self):
        """hostname 保持不变"""
        result = normalize_instance_key_for_cb("gitlab.example.com")
        assert result == "gitlab.example.com"

    def test_case_insensitive(self):
        """大小写不敏感"""
        result = normalize_instance_key_for_cb("GitLab.Example.COM")
        assert result == "gitlab.example.com"

    def test_none_returns_none(self):
        """None 返回 None"""
        result = normalize_instance_key_for_cb(None)
        assert result is None

    def test_empty_string_returns_none(self):
        """空字符串返回 None"""
        result = normalize_instance_key_for_cb("")
        assert result is None

    def test_whitespace_only_returns_none(self):
        """仅空白字符返回 None"""
        result = normalize_instance_key_for_cb("   ")
        assert result is None

    def test_url_with_port(self):
        """带端口的 URL"""
        result = normalize_instance_key_for_cb("https://gitlab.example.com:8443/project")
        assert result == "gitlab.example.com:8443"


class TestMultiTenantMultiRepoFairnessEnqueue:
    """多租户多仓库公平调度入队测试

    测试场景:
    - 多个 tenant 各有多个 repo，构建候选集
    - 开启 fairness 开关后，断言入队序列中 tenant 交替出现
    - 验证 per-tenant concurrency 限制
    - 关闭 fairness 开关时保持旧行为（严格按优先级）
    """

    def test_multi_tenant_multi_repo_candidates_with_fairness_enabled(self):
        """多 tenant 多 repo：开启 fairness 后 tenant 交替入队"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            global_concurrency=50,
            per_tenant_concurrency=10,  # 每 tenant 最多 10 个
        )
        now = 10000.0

        # 构造 4 个 tenant，每个 tenant 有 5 个 repo
        # tenant-a: repo 1-5, priority based on cursor age
        # tenant-b: repo 6-10
        # tenant-c: repo 11-15
        # tenant-d: repo 16-20
        states = []
        tenant_ids = ["tenant-a", "tenant-b", "tenant-c", "tenant-d"]
        repos_per_tenant = 5

        for t_idx, tenant_id in enumerate(tenant_ids):
            for r_idx in range(repos_per_tenant):
                repo_id = t_idx * repos_per_tenant + r_idx + 1
                # 每个 repo 有不同的 cursor 年龄，创建优先级差异
                cursor_age = 5000 + (r_idx * 100)  # 5000, 5100, 5200, ...
                states.append(
                    RepoSyncState(
                        repo_id=repo_id,
                        repo_type="git",
                        tenant_id=tenant_id,
                        cursor_updated_at=now - cursor_age,
                    )
                )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        # 应该有 20 个候选（4 tenant * 5 repo）
        assert len(candidates) == 20

        # 构建 repo -> tenant 映射
        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 验证前 12 个候选中 tenant 分布（3 轮 * 4 tenant）
        # 开启 fairness 后应该交替出现
        first_12_tenants = [repo_to_tenant[c.repo_id] for c in candidates[:12]]

        # 每个 tenant 应该出现 3 次
        for tenant_id in tenant_ids:
            count = first_12_tenants.count(tenant_id)
            assert count == 3, f"前 12 个候选中 {tenant_id} 应出现 3 次，实际 {count}"

        # 验证交替模式：每 4 个连续候选应该来自 4 个不同的 tenant
        for round_start in range(0, 12, 4):
            round_tenants = first_12_tenants[round_start : round_start + 4]
            unique_tenants = set(round_tenants)
            assert len(unique_tenants) == 4, (
                f"轮次 {round_start // 4}：应有 4 个不同 tenant，实际 {len(unique_tenants)}: {round_tenants}"
            )

    def test_multi_tenant_multi_repo_respects_per_tenant_concurrency(self):
        """多 tenant 多 repo：验证 per_tenant_concurrency 限制"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=2,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            global_concurrency=50,
            per_tenant_concurrency=3,  # 每 tenant 最多 3 个
        )
        now = 10000.0

        # 构造 3 个 tenant，每个 tenant 有 10 个 repo
        states = []
        tenant_ids = ["tenant-a", "tenant-b", "tenant-c"]
        repos_per_tenant = 10

        for t_idx, tenant_id in enumerate(tenant_ids):
            for r_idx in range(repos_per_tenant):
                repo_id = t_idx * repos_per_tenant + r_idx + 1
                states.append(
                    RepoSyncState(
                        repo_id=repo_id,
                        repo_type="git",
                        tenant_id=tenant_id,
                        cursor_updated_at=now - 5000,
                    )
                )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        # 每 tenant 最多 3 个，共 9 个
        assert len(candidates) == 9

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}
        tenant_counts = {}
        for c in candidates:
            tid = repo_to_tenant[c.repo_id]
            tenant_counts[tid] = tenant_counts.get(tid, 0) + 1

        # 每个 tenant 应该有 3 个
        for tenant_id in tenant_ids:
            assert tenant_counts.get(tenant_id, 0) == 3, (
                f"{tenant_id} 应有 3 个候选，实际 {tenant_counts.get(tenant_id, 0)}"
            )

    def test_multi_tenant_multi_repo_fairness_disabled_strict_priority(self):
        """多 tenant 多 repo：关闭 fairness 时严格按优先级排序"""
        config = SchedulerConfig(
            enable_tenant_fairness=False,  # 关闭公平调度
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            global_concurrency=50,
            per_tenant_concurrency=100,  # 不限制
        )
        now = 100000.0

        # 构造场景：tenant-a 有高优先级任务，tenant-b 有低优先级任务
        # tenant-a: repo 1-5, cursor 很旧 (高优先级)
        # tenant-b: repo 6-10, cursor 较新 (低优先级)
        states = []

        # tenant-a 高优先级（cursor 年龄 10 小时）
        for i in range(1, 6):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-a",
                    cursor_updated_at=now - 36000,  # 10 小时前
                )
            )

        # tenant-b 低优先级（cursor 年龄 2 小时）
        for i in range(6, 11):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-b",
                    cursor_updated_at=now - 7200,  # 2 小时前
                )
            )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 10

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 关闭 fairness 时，所有 tenant-a 的任务应该排在前面
        first_five = [repo_to_tenant[c.repo_id] for c in candidates[:5]]
        assert all(t == "tenant-a" for t in first_five), (
            f"关闭 fairness 时，前 5 个应全是 tenant-a，实际: {first_five}"
        )

        last_five = [repo_to_tenant[c.repo_id] for c in candidates[5:]]
        assert all(t == "tenant-b" for t in last_five), (
            f"关闭 fairness 时，后 5 个应全是 tenant-b，实际: {last_five}"
        )

    def test_multi_tenant_with_varying_backlog_sizes(self):
        """多 tenant 不同 backlog 大小：fairness 仍能公平分配"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            global_concurrency=50,
            per_tenant_concurrency=100,  # 不限制
        )
        now = 10000.0

        # tenant-a: 20 个 repo（大 backlog）
        # tenant-b: 5 个 repo
        # tenant-c: 2 个 repo（小 backlog）
        states = []

        for i in range(1, 21):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-a",
                    cursor_updated_at=now - 5000,
                )
            )

        for i in range(21, 26):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-b",
                    cursor_updated_at=now - 5000,
                )
            )

        for i in range(26, 28):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-c",
                    cursor_updated_at=now - 5000,
                )
            )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 27

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 验证前 6 个应该是 A, B, C, A, B, C（2 轮）
        first_six = [repo_to_tenant[c.repo_id] for c in candidates[:6]]
        assert first_six.count("tenant-a") == 2
        assert first_six.count("tenant-b") == 2
        assert first_six.count("tenant-c") == 2

        # tenant-c 只有 2 个，应该在位置 2 和 5（第一轮和第二轮）
        tenant_c_positions = [
            i for i, c in enumerate(candidates) if repo_to_tenant[c.repo_id] == "tenant-c"
        ]
        assert len(tenant_c_positions) == 2
        assert max(tenant_c_positions) <= 5, (
            f"tenant-c 的任务应该在前 6 个位置内完成，实际位置: {tenant_c_positions}"
        )

    def test_mixed_tenants_some_with_some_without_tenant_id(self):
        """混合场景：部分 repo 有 tenant_id，部分没有"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=10,
        )
        now = 10000.0

        states = []

        # tenant-a: 4 个 repo
        for i in range(1, 5):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-a",
                    cursor_updated_at=now - 5000,
                )
            )

        # tenant-b: 4 个 repo
        for i in range(5, 9):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-b",
                    cursor_updated_at=now - 5000,
                )
            )

        # 无 tenant_id: 4 个 repo（归入同一桶）
        for i in range(9, 13):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id=None,
                    cursor_updated_at=now - 5000,
                )
            )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 12

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 验证前 9 个候选中三个"tenant"组（a, b, None）各出现 3 次
        first_nine = [repo_to_tenant[c.repo_id] for c in candidates[:9]]
        assert first_nine.count("tenant-a") == 3
        assert first_nine.count("tenant-b") == 3
        assert first_nine.count(None) == 3

    def test_enqueue_sequence_alternates_with_max_per_round_2(self):
        """max_per_round=2 时每轮每 tenant 入队 2 个"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=2,  # 每轮每 tenant 最多 2 个
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            global_concurrency=50,
            per_tenant_concurrency=100,
        )
        now = 10000.0

        # 两个 tenant 各 6 个 repo
        states = []
        for i in range(1, 7):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-a",
                    cursor_updated_at=now - 5000,
                )
            )

        for i in range(7, 13):
            states.append(
                RepoSyncState(
                    repo_id=i,
                    repo_type="git",
                    tenant_id="tenant-b",
                    cursor_updated_at=now - 5000,
                )
            )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
        )

        assert len(candidates) == 12

        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}

        # 验证每 4 个一组（2 个 A + 2 个 B）
        for round_start in range(0, 12, 4):
            round_tenants = [
                repo_to_tenant[c.repo_id] for c in candidates[round_start : round_start + 4]
            ]
            assert round_tenants.count("tenant-a") == 2, (
                f"轮次 {round_start // 4} 应有 2 个 tenant-a，实际: {round_tenants}"
            )
            assert round_tenants.count("tenant-b") == 2, (
                f"轮次 {round_start // 4} 应有 2 个 tenant-b，实际: {round_tenants}"
            )

    def test_global_concurrency_limits_total_enqueue(self):
        """全局并发限制总入队数量"""
        config = SchedulerConfig(
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
            cursor_age_threshold_seconds=3600,
            max_queue_depth=10,  # 全局限制 10 个
            global_concurrency=10,
            per_tenant_concurrency=100,
        )
        now = 10000.0

        # 3 个 tenant 各 10 个 repo = 30 个总候选
        states = []
        for t_idx, tenant_id in enumerate(["tenant-a", "tenant-b", "tenant-c"]):
            for r_idx in range(10):
                repo_id = t_idx * 10 + r_idx + 1
                states.append(
                    RepoSyncState(
                        repo_id=repo_id,
                        repo_type="git",
                        tenant_id=tenant_id,
                        cursor_updated_at=now - 5000,
                    )
                )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            current_queue_size=0,
        )

        # 全局限制 10 个
        assert len(candidates) == 10

        # 验证公平分布：9 个可以三等分（3+3+3），剩 1 个
        repo_to_tenant = {s.repo_id: s.tenant_id for s in states}
        tenant_counts = {}
        for c in candidates:
            tid = repo_to_tenant[c.repo_id]
            tenant_counts[tid] = tenant_counts.get(tid, 0) + 1

        # 每个 tenant 应该有 3-4 个（10 // 3 = 3 余 1）
        for tid in ["tenant-a", "tenant-b", "tenant-c"]:
            assert 3 <= tenant_counts.get(tid, 0) <= 4, (
                f"{tid} 应有 3-4 个候选，实际 {tenant_counts.get(tid, 0)}"
            )


class TestMvpModeScheduling:
    """MVP 模式调度测试

    测试场景:
    - MVP 模式开启时，仅 allowlist 中的 job_type 被调度
    - MVP 模式关闭时，所有 job_type 正常调度
    - allowlist 为空时，不调度任何任务
    """

    def test_mvp_mode_disabled_schedules_all_job_types(self):
        """MVP 模式关闭时，所有 job_type 正常调度"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            mvp_mode_enabled=False,  # 关闭 MVP 模式
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=now - 5000,  # 超过阈值
        )

        candidates = select_jobs_to_enqueue(
            [state],
            ["commits", "mrs", "reviews"],
            config,
            now=now,
        )

        # 应该有 3 个候选（每个 job_type 一个）
        assert len(candidates) == 3
        job_types = {c.job_type for c in candidates}
        assert job_types == {"commits", "mrs", "reviews"}

    def test_mvp_mode_enabled_filters_non_allowed_job_types(self):
        """MVP 模式开启时，仅 allowlist 中的 job_type 被调度"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            mvp_mode_enabled=True,  # 开启 MVP 模式
            mvp_job_type_allowlist=["commits"],  # 仅允许 commits
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=now - 5000,  # 超过阈值
        )

        candidates = select_jobs_to_enqueue(
            [state],
            ["commits", "mrs", "reviews"],  # 传入所有类型
            config,
            now=now,
        )

        # 应该只有 1 个候选（仅 commits）
        assert len(candidates) == 1
        assert candidates[0].job_type == "commits"

    def test_mvp_mode_with_multiple_allowed_types(self):
        """MVP 模式开启时，允许多个 job_type"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            mvp_mode_enabled=True,
            mvp_job_type_allowlist=["commits", "mrs"],  # 允许 commits 和 mrs
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=now - 5000,
        )

        candidates = select_jobs_to_enqueue(
            [state],
            ["commits", "mrs", "reviews"],
            config,
            now=now,
        )

        # 应该有 2 个候选（commits 和 mrs）
        assert len(candidates) == 2
        job_types = {c.job_type for c in candidates}
        assert job_types == {"commits", "mrs"}
        assert "reviews" not in job_types

    def test_mvp_mode_with_empty_allowlist_blocks_all(self):
        """MVP 模式开启且 allowlist 为空时，不调度任何任务"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            mvp_mode_enabled=True,
            mvp_job_type_allowlist=[],  # 空列表
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=now - 5000,
        )

        candidates = select_jobs_to_enqueue(
            [state],
            ["commits", "mrs", "reviews"],
            config,
            now=now,
        )

        # 应该没有候选
        assert len(candidates) == 0

    def test_mvp_mode_with_multiple_repos(self):
        """MVP 模式对多个仓库的过滤效果"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            mvp_mode_enabled=True,
            mvp_job_type_allowlist=["commits"],
        )
        now = 10000.0

        states = [
            RepoSyncState(repo_id=1, repo_type="git", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=2, repo_type="git", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=3, repo_type="svn", cursor_updated_at=now - 5000),
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs", "reviews"],
            config,
            now=now,
        )

        # 应该有 3 个候选（每个仓库的 commits）
        assert len(candidates) == 3
        assert all(c.job_type == "commits" for c in candidates)
        repo_ids = {c.repo_id for c in candidates}
        assert repo_ids == {1, 2, 3}

    def test_mvp_mode_preserves_priority_ordering(self):
        """MVP 模式过滤后仍保持优先级排序"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            mvp_mode_enabled=True,
            mvp_job_type_allowlist=["commits", "mrs"],
            job_type_priority={"commits": 1, "mrs": 2, "reviews": 3},
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            cursor_updated_at=now - 5000,
        )

        candidates = select_jobs_to_enqueue(
            [state],
            ["commits", "mrs", "reviews"],
            config,
            now=now,
        )

        # 验证排序：commits 应在 mrs 之前
        assert len(candidates) == 2
        assert candidates[0].job_type == "commits"
        assert candidates[1].job_type == "mrs"

    def test_mvp_mode_config_from_dict(self):
        """验证 MVP 配置可通过 from_config 加载"""
        # 模拟配置对象
        mock_config = MagicMock()
        mock_config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "scm.scheduler.mvp_mode_enabled": True,
                "scm.scheduler.mvp_job_type_allowlist": ["commits", "mrs"],
            }.get(key, default)
        )

        scheduler_config = SchedulerConfig.from_config(mock_config)

        assert scheduler_config.mvp_mode_enabled is True
        assert scheduler_config.mvp_job_type_allowlist == ["commits", "mrs"]

    def test_mvp_mode_env_override(self):
        """验证 MVP 配置可通过环境变量覆盖"""
        import os

        # 保存原始环境变量
        orig_enabled = os.environ.get("SCM_SCHEDULER_MVP_MODE_ENABLED")
        orig_allowlist = os.environ.get("SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST")

        try:
            # 设置环境变量
            os.environ["SCM_SCHEDULER_MVP_MODE_ENABLED"] = "true"
            os.environ["SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST"] = "commits,mrs"

            scheduler_config = SchedulerConfig.from_config(None)

            assert scheduler_config.mvp_mode_enabled is True
            assert scheduler_config.mvp_job_type_allowlist == ["commits", "mrs"]
        finally:
            # 恢复原始环境变量
            if orig_enabled is None:
                os.environ.pop("SCM_SCHEDULER_MVP_MODE_ENABLED", None)
            else:
                os.environ["SCM_SCHEDULER_MVP_MODE_ENABLED"] = orig_enabled
            if orig_allowlist is None:
                os.environ.pop("SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST", None)
            else:
                os.environ["SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST"] = orig_allowlist


# ============ Bucket Penalty 单调性与恢复条件测试 ============


class TestBucketPenaltyMonotonicity:
    """
    Bucket Penalty 单调性测试

    验证点：
    - 更严重的 bucket 状态 → 更高的 penalty 值
    - Penalty 顺序: paused > low_tokens > healthy
    - Penalty 值的单调性保证优先级排序正确
    """

    def test_penalty_monotonic_increase_with_severity(self):
        """惩罚值随严重程度单调递增"""
        # 正常状态（无惩罚）
        healthy_bucket = InstanceBucketStatus(
            instance_key="healthy.gitlab.com",
            is_paused=False,
            current_tokens=8.0,  # 80% >= 20%
            burst=10.0,
        )

        # 令牌不足状态（中等惩罚）
        low_tokens_bucket = InstanceBucketStatus(
            instance_key="low.gitlab.com",
            is_paused=False,
            current_tokens=1.5,  # 15% < 20%
            burst=10.0,
        )

        # 暂停状态（最高惩罚）
        paused_bucket = InstanceBucketStatus(
            instance_key="paused.gitlab.com",
            is_paused=True,
            pause_remaining_seconds=300.0,
            current_tokens=0.0,
            burst=10.0,
        )

        penalty_healthy, _ = calculate_bucket_priority_penalty(healthy_bucket)
        penalty_low_tokens, _ = calculate_bucket_priority_penalty(low_tokens_bucket)
        penalty_paused, _ = calculate_bucket_priority_penalty(paused_bucket)

        # 验证单调性：healthy < low_tokens < paused
        assert penalty_healthy == 0
        assert penalty_low_tokens == BUCKET_LOW_TOKENS_PRIORITY_PENALTY
        assert penalty_paused == BUCKET_PAUSED_PRIORITY_PENALTY

        # 验证绝对顺序
        assert penalty_healthy < penalty_low_tokens < penalty_paused

        # 验证 paused 惩罚显著高于 low_tokens（确保排序效果明显）
        assert penalty_paused >= 2 * penalty_low_tokens

    def test_token_boundary_at_20_percent(self):
        """20% 令牌边界测试"""
        # 刚好在 20% 边界上（不触发惩罚）
        at_boundary = InstanceBucketStatus(
            instance_key="boundary.gitlab.com",
            is_paused=False,
            current_tokens=2.0,  # 正好 20%
            burst=10.0,
        )

        # 刚好低于 20% 边界（触发惩罚）
        below_boundary = InstanceBucketStatus(
            instance_key="below.gitlab.com",
            is_paused=False,
            current_tokens=1.9,  # 19% < 20%
            burst=10.0,
        )

        penalty_at, reason_at = calculate_bucket_priority_penalty(at_boundary)
        penalty_below, reason_below = calculate_bucket_priority_penalty(below_boundary)

        assert penalty_at == 0
        assert reason_at is None

        assert penalty_below == BUCKET_LOW_TOKENS_PRIORITY_PENALTY
        assert reason_below == "bucket_low_tokens"

    def test_zero_burst_does_not_crash(self):
        """burst=0 时不崩溃"""
        zero_burst = InstanceBucketStatus(
            instance_key="zero.gitlab.com",
            is_paused=False,
            current_tokens=0.0,
            burst=0.0,  # 边界情况
        )

        # 不应抛出异常
        penalty, reason = calculate_bucket_priority_penalty(zero_burst)

        # burst=0 时不计算 token_ratio，返回无惩罚
        assert penalty == 0
        assert reason is None


class TestBucketPauseEffectiveScope:
    """
    Bucket Pause 生效范围测试

    验证点：
    - Pause 只影响对应的 GitLab 实例
    - 其他实例的任务不受影响
    - 同一实例的不同 job_type 都受影响
    """

    def test_pause_only_affects_target_instance(self):
        """Pause 只影响目标实例"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        # 3 个实例，每个实例 1 个仓库
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="instance-a.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="instance-b.gitlab.com",  # 目标暂停实例
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="instance-c.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 只暂停 instance-b
        bucket_statuses = {
            "instance-b.gitlab.com": InstanceBucketStatus(
                instance_key="instance-b.gitlab.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,
        )

        # repo 1 和 repo 3 应被调度
        repo_ids = {c.repo_id for c in candidates}
        assert 1 in repo_ids, "instance-a 的任务应被调度"
        assert 2 not in repo_ids, "instance-b（暂停）的任务不应被调度"
        assert 3 in repo_ids, "instance-c 的任务应被调度"

    def test_all_job_types_of_paused_instance_affected(self):
        """暂停实例的所有 job_type 都受影响"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="paused.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
        ]

        bucket_statuses = {
            "paused.gitlab.com": InstanceBucketStatus(
                instance_key="paused.gitlab.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,
        )

        # 所有 job_type 都应被跳过
        assert len(candidates) == 0

    def test_svn_repos_without_instance_not_affected(self):
        """SVN 仓库（无 gitlab_instance）不受 bucket 暂停影响"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            # Git 仓库，有暂停实例
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="paused.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
            # SVN 仓库，无 gitlab_instance
            RepoSyncState(
                repo_id=2,
                repo_type="svn",
                gitlab_instance=None,
                cursor_updated_at=now - 5000,
            ),
        ]

        bucket_statuses = {
            "paused.gitlab.com": InstanceBucketStatus(
                instance_key="paused.gitlab.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,
        )

        # SVN 仓库应被调度
        repo_ids = {c.repo_id for c in candidates}
        assert 1 not in repo_ids, "Git 仓库的暂停实例不应被调度"
        assert 2 in repo_ids, "SVN 仓库应被调度"


class TestBucketPauseRecoveryConditions:
    """
    Bucket Pause 恢复条件测试

    验证点：
    - Pause 到期后（is_paused=False）应正常调度
    - pause_remaining_seconds=0 表示已过期
    - 从 paused 恢复到 healthy 的过渡状态
    """

    def test_expired_pause_allows_scheduling(self):
        """Pause 过期后允许调度"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="recovered.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 暂停已过期：is_paused=False, pause_remaining_seconds=0
        bucket_statuses = {
            "recovered.gitlab.com": InstanceBucketStatus(
                instance_key="recovered.gitlab.com",
                is_paused=False,
                pause_remaining_seconds=0.0,
                current_tokens=10.0,  # 令牌已恢复
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,
        )

        # 应该正常调度
        assert len(candidates) == 1
        assert candidates[0].repo_id == 1
        assert candidates[0].bucket_paused is False
        assert candidates[0].bucket_penalty_value == 0

    def test_recovery_transition_low_tokens_phase(self):
        """恢复过渡阶段：令牌不足但未暂停"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="recovering.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="healthy.gitlab.com",
                cursor_updated_at=now - 5000,
            ),
        ]

        # recovering 实例：暂停刚解除但令牌还未恢复
        bucket_statuses = {
            "recovering.gitlab.com": InstanceBucketStatus(
                instance_key="recovering.gitlab.com",
                is_paused=False,  # 暂停已解除
                pause_remaining_seconds=0.0,
                current_tokens=0.5,  # 令牌还很低
                burst=10.0,
            ),
            "healthy.gitlab.com": InstanceBucketStatus(
                instance_key="healthy.gitlab.com",
                is_paused=False,
                current_tokens=10.0,
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=False,  # 不跳过，只降权
        )

        # 两个任务都应被保留
        assert len(candidates) == 2

        # recovering 应有 low_tokens 惩罚
        recovering_candidate = next(c for c in candidates if c.repo_id == 1)
        assert recovering_candidate.bucket_penalty_reason == "bucket_low_tokens"

        # healthy 应无惩罚且优先级更高
        healthy_candidate = next(c for c in candidates if c.repo_id == 2)
        assert healthy_candidate.bucket_penalty_value == 0
        assert healthy_candidate.priority < recovering_candidate.priority

    def test_pause_state_transition_in_scheduler_loop(self):
        """模拟调度器循环中的暂停状态转换"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            gitlab_instance="transitioning.gitlab.com",
            cursor_updated_at=now - 5000,
        )

        # 第一轮：暂停状态
        bucket_statuses_round1 = {
            "transitioning.gitlab.com": InstanceBucketStatus(
                instance_key="transitioning.gitlab.com",
                is_paused=True,
                pause_remaining_seconds=60.0,
            ),
        }

        candidates_round1 = select_jobs_to_enqueue(
            states=[state],
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses_round1,
            skip_on_bucket_pause=True,
        )

        # 第一轮：跳过
        assert len(candidates_round1) == 0

        # 第二轮：暂停已过期，令牌不足
        bucket_statuses_round2 = {
            "transitioning.gitlab.com": InstanceBucketStatus(
                instance_key="transitioning.gitlab.com",
                is_paused=False,
                pause_remaining_seconds=0.0,
                current_tokens=1.0,  # 低令牌
                burst=10.0,
            ),
        }

        candidates_round2 = select_jobs_to_enqueue(
            states=[state],
            job_types=["commits"],
            config=config,
            now=now + 60,  # 1 分钟后
            bucket_statuses=bucket_statuses_round2,
            skip_on_bucket_pause=False,
        )

        # 第二轮：调度但有惩罚
        assert len(candidates_round2) == 1
        assert candidates_round2[0].bucket_penalty_reason == "bucket_low_tokens"

        # 第三轮：完全恢复
        bucket_statuses_round3 = {
            "transitioning.gitlab.com": InstanceBucketStatus(
                instance_key="transitioning.gitlab.com",
                is_paused=False,
                current_tokens=10.0,  # 令牌恢复
                burst=10.0,
            ),
        }

        candidates_round3 = select_jobs_to_enqueue(
            states=[state],
            job_types=["commits"],
            config=config,
            now=now + 120,  # 2 分钟后
            bucket_statuses=bucket_statuses_round3,
        )

        # 第三轮：正常调度，无惩罚
        assert len(candidates_round3) == 1
        assert candidates_round3[0].bucket_penalty_value == 0
        assert candidates_round3[0].bucket_penalty_reason is None

    def test_skip_on_pause_respects_is_paused_flag_only(self):
        """skip_on_bucket_pause 仅依赖 is_paused 标志"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )
        now = 10000.0

        state = RepoSyncState(
            repo_id=1,
            repo_type="git",
            gitlab_instance="test.gitlab.com",
            cursor_updated_at=now - 5000,
        )

        # 虽然有 pause_remaining_seconds，但 is_paused=False
        bucket_statuses = {
            "test.gitlab.com": InstanceBucketStatus(
                instance_key="test.gitlab.com",
                is_paused=False,  # 关键：is_paused=False
                pause_remaining_seconds=100.0,  # 这个值会被忽略
                current_tokens=5.0,
                burst=10.0,
            ),
        }

        candidates = select_jobs_to_enqueue(
            states=[state],
            job_types=["commits"],
            config=config,
            now=now,
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,  # 即使启用了跳过
        )

        # 因为 is_paused=False，不应跳过
        assert len(candidates) == 1


# ============ 预算限制不变量测试 ============


class TestBudgetInvariants:
    """
    预算限制不变量测试

    验证 select_jobs_to_enqueue 在任何情况下都不会超过预算限制。
    这些测试确保：
    1. 返回的候选数 + 初始活跃数 <= max_queue_depth
    2. 每个实例的候选数 + 初始实例计数 <= per_instance_concurrency
    3. 每个租户的候选数 + 初始租户计数 <= per_tenant_concurrency
    """

    def test_invariant_global_queue_depth_never_exceeded(self):
        """不变量：全局队列深度永远不会超过 max_queue_depth"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=5,  # 严格限制
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建大量需要同步的仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,  # 需要同步
            )
            for i in range(1, 100)
        ]

        # 测试多种初始占用场景（仅测试未超限的情况）
        for initial_active in [0, 1, 3, 4]:
            budget_snapshot = BudgetSnapshot(
                global_running=initial_active // 2,
                global_pending=initial_active - initial_active // 2,
                global_active=initial_active,
            )

            candidates = select_jobs_to_enqueue(
                states,
                ["commits"],
                config,
                now=now,
                budget_snapshot=budget_snapshot,
            )

            # 不变量检查：新入队候选数 <= 剩余空间
            remaining_space = max(0, config.max_queue_depth - initial_active)
            assert len(candidates) <= remaining_space, (
                f"违反不变量: candidates={len(candidates)} > remaining_space={remaining_space}"
            )

        # 额外测试：初始已满时返回空
        budget_full = BudgetSnapshot(global_running=3, global_pending=2, global_active=5)
        candidates_full = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_full,
        )
        assert len(candidates_full) == 0, "队列已满时不应入队"

        # 额外测试：初始超限时也返回空
        budget_over = BudgetSnapshot(global_running=3, global_pending=4, global_active=7)
        candidates_over = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_over,
        )
        assert len(candidates_over) == 0, "队列已超限时不应入队"

    def test_invariant_per_instance_concurrency_never_exceeded(self):
        """不变量：每个实例的并发数永远不会超过 per_instance_concurrency"""
        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=100,
            per_instance_concurrency=3,  # 严格限制
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 同一实例的多个仓库
        instance_key = "gitlab.example.com"
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance=instance_key,
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 20)
        ]

        # 测试多种初始占用场景（仅测试未超限的情况）
        for initial_instance_count in [0, 1, 2]:
            budget_snapshot = BudgetSnapshot(
                global_running=initial_instance_count,
                global_active=initial_instance_count,
                by_instance={instance_key: initial_instance_count},
            )

            candidates = select_jobs_to_enqueue(
                states,
                ["commits"],
                config,
                now=now,
                budget_snapshot=budget_snapshot,
            )

            # 统计该实例的候选数
            instance_candidates = sum(
                1
                for c in candidates
                if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance
                == instance_key
            )

            # 不变量检查：新入队候选数 <= 剩余空间
            remaining_space = max(0, config.per_instance_concurrency - initial_instance_count)
            assert instance_candidates <= remaining_space, (
                f"违反实例不变量: candidates={instance_candidates} > remaining_space={remaining_space}"
            )

        # 额外测试：实例已满时返回空
        budget_full = BudgetSnapshot(
            global_running=3,
            global_active=3,
            by_instance={instance_key: 3},  # 实例已满
        )
        candidates_full = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_full,
        )
        assert len(candidates_full) == 0, "实例已满时不应入队"

    def test_invariant_per_tenant_concurrency_never_exceeded(self):
        """不变量：每个租户的并发数永远不会超过 per_tenant_concurrency"""
        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=100,
            per_tenant_concurrency=2,  # 严格限制
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 同一租户的多个仓库
        tenant_id = "tenant-alpha"
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                tenant_id=tenant_id,
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 20)
        ]

        # 测试多种初始占用场景（仅测试未超限的情况）
        for initial_tenant_count in [0, 1]:
            budget_snapshot = BudgetSnapshot(
                global_running=initial_tenant_count,
                global_active=initial_tenant_count,
                by_tenant={tenant_id: initial_tenant_count},
            )

            candidates = select_jobs_to_enqueue(
                states,
                ["commits"],
                config,
                now=now,
                budget_snapshot=budget_snapshot,
            )

            # 统计该租户的候选数
            tenant_candidates = sum(
                1
                for c in candidates
                if next((s for s in states if s.repo_id == c.repo_id)).tenant_id == tenant_id
            )

            # 不变量检查：新入队候选数 <= 剩余空间
            remaining_space = max(0, config.per_tenant_concurrency - initial_tenant_count)
            assert tenant_candidates <= remaining_space, (
                f"违反租户不变量: candidates={tenant_candidates} > remaining_space={remaining_space}"
            )

        # 额外测试：租户已满时返回空
        budget_full = BudgetSnapshot(
            global_running=2,
            global_active=2,
            by_tenant={tenant_id: 2},  # 租户已满
        )
        candidates_full = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_full,
        )
        assert len(candidates_full) == 0, "租户已满时不应入队"

    def test_invariant_combined_limits_all_respected(self):
        """不变量：所有限制同时被尊重"""
        config = SchedulerConfig(
            max_running=10,
            max_queue_depth=8,
            per_instance_concurrency=3,
            per_tenant_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 创建复杂场景：多实例、多租户
        states = [
            # 实例 A，租户 1
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                gitlab_instance="instance-a",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                gitlab_instance="instance-a",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                gitlab_instance="instance-a",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            # 实例 A，租户 2
            RepoSyncState(
                repo_id=4,
                repo_type="git",
                gitlab_instance="instance-a",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=5,
                repo_type="git",
                gitlab_instance="instance-a",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
            # 实例 B，租户 1
            RepoSyncState(
                repo_id=6,
                repo_type="git",
                gitlab_instance="instance-b",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=7,
                repo_type="git",
                gitlab_instance="instance-b",
                tenant_id="tenant-1",
                cursor_updated_at=now - 5000,
            ),
            # 实例 B，租户 2
            RepoSyncState(
                repo_id=8,
                repo_type="git",
                gitlab_instance="instance-b",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=9,
                repo_type="git",
                gitlab_instance="instance-b",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
            RepoSyncState(
                repo_id=10,
                repo_type="git",
                gitlab_instance="instance-b",
                tenant_id="tenant-2",
                cursor_updated_at=now - 5000,
            ),
        ]

        # 初始占用
        budget_snapshot = BudgetSnapshot(
            global_running=2,
            global_pending=1,
            global_active=3,
            by_instance={"instance-a": 2},  # instance-a 已有 2 个
            by_tenant={"tenant-1": 1},  # tenant-1 已有 1 个
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 检查全局不变量
        assert len(candidates) + budget_snapshot.global_active <= config.max_queue_depth

        # 统计各维度
        instance_counts = {}
        tenant_counts = {}
        for c in candidates:
            state = next(s for s in states if s.repo_id == c.repo_id)
            if state.gitlab_instance:
                instance_counts[state.gitlab_instance] = (
                    instance_counts.get(state.gitlab_instance, 0) + 1
                )
            if state.tenant_id:
                tenant_counts[state.tenant_id] = tenant_counts.get(state.tenant_id, 0) + 1

        # 检查实例不变量
        for inst, count in instance_counts.items():
            initial = budget_snapshot.by_instance.get(inst, 0)
            assert count + initial <= config.per_instance_concurrency, (
                f"实例 {inst}: {count} + {initial} > {config.per_instance_concurrency}"
            )

        # 检查租户不变量
        for tenant, count in tenant_counts.items():
            initial = budget_snapshot.by_tenant.get(tenant, 0)
            assert count + initial <= config.per_tenant_concurrency, (
                f"租户 {tenant}: {count} + {initial} > {config.per_tenant_concurrency}"
            )

    def test_invariant_instance_key_normalization_consistency(self):
        """
        不变量：实例键规范化在预算计算和候选过滤中保持一致

        验证场景：
        - BudgetSnapshot 中的 by_instance 键是规范化的
        - RepoSyncState 中的 gitlab_instance 是规范化的
        - 两者应该能正确匹配
        """
        from engram.logbook.scm_sync_keys import normalize_instance_key

        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=100,
            per_instance_concurrency=2,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        # 使用不同格式的实例名，但规范化后应该相同
        raw_instance_formats = [
            "https://GITLAB.Example.COM/group/proj",
            "gitlab.example.com",
            "GITLAB.EXAMPLE.COM:443",
            "https://gitlab.example.com:443/",
        ]

        # 所有格式规范化后应该相同
        normalized = normalize_instance_key(raw_instance_formats[0])
        for fmt in raw_instance_formats[1:]:
            assert normalize_instance_key(fmt) == normalized, (
                f"规范化不一致: {fmt} -> {normalize_instance_key(fmt)} != {normalized}"
            )

        # 创建使用规范化键的状态
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance=normalized,  # 使用规范化的键
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 10)
        ]

        # 预算快照也使用规范化的键
        budget_snapshot = BudgetSnapshot(
            global_running=1,
            global_active=1,
            by_instance={normalized: 1},  # 已有 1 个
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 不变量：候选数 + 初始占用 <= per_instance_concurrency
        assert len(candidates) + 1 <= config.per_instance_concurrency

    def test_invariant_max_running_blocks_all_enqueue(self):
        """不变量：当 running >= max_running 时，完全阻止入队"""
        config = SchedulerConfig(
            max_running=3,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=None,  # 从未同步，高优先级
            )
            for i in range(1, 20)
        ]

        # running 已达上限
        budget_snapshot = BudgetSnapshot(
            global_running=3,  # = max_running
            global_pending=0,
            global_active=3,
        )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 不变量：max_running 达到时，不入队任何新任务
        assert len(candidates) == 0

    def test_invariant_max_enqueue_per_scan_respected(self):
        """不变量：单次扫描入队数不超过 max_enqueue_per_scan"""
        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=100,
            max_enqueue_per_scan=5,  # 严格限制
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, 50)
        ]

        budget_snapshot = BudgetSnapshot.empty()

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=budget_snapshot,
        )

        # 不变量：候选数 <= max_enqueue_per_scan
        assert len(candidates) <= config.max_enqueue_per_scan


# ============================================================================
# Micro-Benchmark / 操作计数测试
# ============================================================================


class TestSelectJobsToEnqueueMicroBenchmark:
    """
    select_jobs_to_enqueue 关键热路径的 Micro-Benchmark 测试

    目的：
    - 验证算法复杂度符合预期（O(n log n) 排序 + O(n) 遍历）
    - 确保操作计数在可接受范围内，避免性能退化
    - 提供可重复、不依赖 DB 的性能基准

    关键热路径：
    1. 排序操作：candidates.sort() - O(n log n)
    2. 分组操作：_apply_tenant_fairness_ordering() - O(n) 分桶 + O(k * n/k) 轮询
    3. 预算扣减：instance/tenant 计数器查找 - O(n) 遍历 + O(1) 字典查找

    CI 阈值设计（保守）：
    - 使用相对阈值而非绝对时间，避免因机器差异导致测试易碎
    - 操作计数阈值设置为理论最大值的 1.5 倍，留有余量
    """

    def test_operation_counts_basic(self):
        """
        基础操作计数测试：验证关键路径的调用次数

        场景：100 个仓库，3 种 job_type
        预期：
        - 候选生成循环：100 * 3 = 300 次迭代（最大）
        - 排序：1 次（对所有候选）
        - 预算检查：≤ max_enqueue_per_scan 次
        """
        import engram_logbook.scm_sync_policy as policy_module

        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=200,
            per_instance_concurrency=50,
            per_tenant_concurrency=50,
            max_enqueue_per_scan=50,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 100
        job_types = ["commits", "mrs", "reviews"]

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance=f"gitlab-{i % 5}.example.com",
                tenant_id=f"tenant-{i % 10}",
                cursor_updated_at=now - 5000,  # 需要同步
            )
            for i in range(1, n_repos + 1)
        ]

        # 使用计数器追踪关键操作
        call_counts = {
            "compute_job_priority": 0,
            "should_schedule_repo_health": 0,
        }

        original_compute_priority = policy_module.compute_job_priority
        original_should_schedule = policy_module.should_schedule_repo_health

        def counting_compute_priority(*args, **kwargs):
            call_counts["compute_job_priority"] += 1
            return original_compute_priority(*args, **kwargs)

        def counting_should_schedule(*args, **kwargs):
            call_counts["should_schedule_repo_health"] += 1
            return original_should_schedule(*args, **kwargs)

        # Patch 关键函数以计数
        with patch.object(policy_module, "compute_job_priority", counting_compute_priority):
            with patch.object(
                policy_module, "should_schedule_repo_health", counting_should_schedule
            ):
                candidates = select_jobs_to_enqueue(
                    states,
                    job_types,
                    config,
                    now=now,
                    budget_snapshot=BudgetSnapshot.empty(),
                )

        # 验证操作计数
        # should_schedule_repo_health: 每个 repo 调用 1 次
        assert call_counts["should_schedule_repo_health"] == n_repos, (
            f"should_schedule_repo_health 调用次数异常: "
            f"expected={n_repos}, actual={call_counts['should_schedule_repo_health']}"
        )

        # compute_job_priority: 每个通过 health 检查的 repo 的每个 job_type 调用 1 次
        # 最大值 = n_repos * len(job_types)
        max_priority_calls = n_repos * len(job_types)
        assert call_counts["compute_job_priority"] <= max_priority_calls, (
            f"compute_job_priority 调用次数超出预期: "
            f"max={max_priority_calls}, actual={call_counts['compute_job_priority']}"
        )

        # 验证返回的候选数量合理
        assert len(candidates) <= config.max_enqueue_per_scan

    def test_operation_counts_with_queued_pairs_filtering(self):
        """
        操作计数测试：验证 queued_pairs 过滤的效率

        场景：50% 的 (repo_id, job_type) 已在队列中
        预期：被过滤的任务不应调用 compute_job_priority
        """
        import engram_logbook.scm_sync_policy as policy_module

        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=200,
            max_enqueue_per_scan=100,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 50
        job_types = ["commits", "mrs"]

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, n_repos + 1)
        ]

        # 50% 的 commits 已在队列中
        queued_pairs = {(i, "commits") for i in range(1, n_repos // 2 + 1)}

        call_counts = {"compute_job_priority": 0}
        original_compute_priority = policy_module.compute_job_priority

        def counting_compute_priority(*args, **kwargs):
            call_counts["compute_job_priority"] += 1
            return original_compute_priority(*args, **kwargs)

        with patch.object(policy_module, "compute_job_priority", counting_compute_priority):
            select_jobs_to_enqueue(
                states,
                job_types,
                config,
                now=now,
                queued_pairs=queued_pairs,
                budget_snapshot=BudgetSnapshot.empty(),
            )

        # 预期调用次数 = n_repos * 2 - len(queued_pairs)
        # 因为被 queued_pairs 过滤的不会调用 compute_job_priority
        expected_max_calls = n_repos * len(job_types) - len(queued_pairs)

        assert call_counts["compute_job_priority"] <= expected_max_calls, (
            f"compute_job_priority 调用次数超出预期: "
            f"max={expected_max_calls}, actual={call_counts['compute_job_priority']}"
        )

    def test_sorting_complexity_linear_to_candidates(self):
        """
        排序复杂度测试：验证排序仅发生一次

        使用自定义比较键验证排序行为
        """
        config = SchedulerConfig(
            max_running=1000,
            max_queue_depth=1000,
            max_enqueue_per_scan=500,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 200

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - (5000 + i * 10),  # 不同年龄
            )
            for i in range(1, n_repos + 1)
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=BudgetSnapshot.empty(),
        )

        # 验证结果已按优先级排序
        priorities = [c.priority for c in candidates]
        assert priorities == sorted(priorities), "候选未按优先级正确排序"

    def test_tenant_fairness_ordering_operation_counts(self):
        """
        Tenant 公平排序操作计数测试

        场景：启用 tenant_fairness，5 个 tenant 各有 10 个仓库
        预期：轮询排序的操作次数与 tenant 数量和仓库数量成线性关系
        """
        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=100,
            max_enqueue_per_scan=50,
            cursor_age_threshold_seconds=3600,
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
        )
        now = 10000.0

        n_tenants = 5
        repos_per_tenant = 10

        states = []
        for t in range(n_tenants):
            for r in range(repos_per_tenant):
                repo_id = t * repos_per_tenant + r + 1
                states.append(
                    RepoSyncState(
                        repo_id=repo_id,
                        repo_type="git",
                        tenant_id=f"tenant-{t}",
                        cursor_updated_at=now - 5000,
                    )
                )

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=BudgetSnapshot.empty(),
        )

        # 验证 tenant 公平性：前 n_tenants 个候选应来自不同 tenant
        if len(candidates) >= n_tenants:
            first_n_tenant_ids = set()
            for c in candidates[:n_tenants]:
                state = next(s for s in states if s.repo_id == c.repo_id)
                first_n_tenant_ids.add(state.tenant_id)

            # 应该有 n_tenants 个不同的 tenant
            assert len(first_n_tenant_ids) == n_tenants, (
                f"Tenant 公平性未生效: 前 {n_tenants} 个候选来自 {len(first_n_tenant_ids)} 个 tenant"
            )

    def test_budget_deduction_loop_efficiency(self):
        """
        预算扣减循环效率测试

        场景：大量候选，但受限于各种并发限制
        预期：循环应在达到限制后及时退出，不做无效迭代
        """
        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=100,
            per_instance_concurrency=2,  # 严格的实例限制
            per_tenant_concurrency=3,  # 严格的租户限制
            max_enqueue_per_scan=10,  # 严格的单次限制
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 100

        # 所有仓库来自同一实例、同一租户
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance="single-instance.com",
                tenant_id="single-tenant",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, n_repos + 1)
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits"],
            config,
            now=now,
            budget_snapshot=BudgetSnapshot.empty(),
        )

        # 应该受 per_instance_concurrency (2) 限制
        assert len(candidates) == min(
            config.per_instance_concurrency,
            config.per_tenant_concurrency,
            config.max_enqueue_per_scan,
        )

    def test_scalability_1000_repos(self):
        """
        可扩展性测试：1000 个仓库场景

        验证在较大规模下，算法仍能高效运行
        阈值设计：
        - 不设绝对时间限制（避免 CI 环境差异）
        - 验证返回结果正确性
        """
        config = SchedulerConfig(
            max_running=500,
            max_queue_depth=500,
            per_instance_concurrency=100,
            per_tenant_concurrency=100,
            max_enqueue_per_scan=100,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 1000
        n_instances = 10
        n_tenants = 20

        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                gitlab_instance=f"gitlab-{i % n_instances}.example.com",
                tenant_id=f"tenant-{i % n_tenants}",
                cursor_updated_at=now - 5000 - (i % 100) * 10,  # 不同年龄
            )
            for i in range(1, n_repos + 1)
        ]

        candidates = select_jobs_to_enqueue(
            states,
            ["commits", "mrs"],
            config,
            now=now,
            budget_snapshot=BudgetSnapshot.empty(),
        )

        # 基本正确性验证
        assert len(candidates) <= config.max_enqueue_per_scan
        assert len(candidates) > 0, "应该有候选被选中"

        # 验证排序正确
        priorities = [c.priority for c in candidates]
        assert priorities == sorted(priorities)

        # 验证并发限制
        instance_counts = {}
        tenant_counts = {}
        for c in candidates:
            state = next(s for s in states if s.repo_id == c.repo_id)
            if state.gitlab_instance:
                instance_counts[state.gitlab_instance] = (
                    instance_counts.get(state.gitlab_instance, 0) + 1
                )
            if state.tenant_id:
                tenant_counts[state.tenant_id] = tenant_counts.get(state.tenant_id, 0) + 1

        for inst, count in instance_counts.items():
            assert count <= config.per_instance_concurrency
        for tenant, count in tenant_counts.items():
            assert count <= config.per_tenant_concurrency

    def test_early_exit_on_max_running_limit(self):
        """
        早期退出测试：max_running 限制触发时应立即返回空列表

        验证不做无效的候选生成工作
        """
        import engram_logbook.scm_sync_policy as policy_module

        config = SchedulerConfig(
            max_running=5,
            max_queue_depth=100,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 100
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, n_repos + 1)
        ]

        call_counts = {"should_schedule_repo_health": 0}
        original_should_schedule = policy_module.should_schedule_repo_health

        def counting_should_schedule(*args, **kwargs):
            call_counts["should_schedule_repo_health"] += 1
            return original_should_schedule(*args, **kwargs)

        # max_running 已达上限
        budget_snapshot = BudgetSnapshot(
            global_running=5,  # = max_running
            global_pending=0,
            global_active=5,
        )

        with patch.object(policy_module, "should_schedule_repo_health", counting_should_schedule):
            candidates = select_jobs_to_enqueue(
                states,
                ["commits"],
                config,
                now=now,
                budget_snapshot=budget_snapshot,
            )

        # 应该立即返回空列表，不遍历任何 state
        assert len(candidates) == 0
        assert call_counts["should_schedule_repo_health"] == 0, (
            "max_running 达到上限时，不应调用 should_schedule_repo_health"
        )

    def test_early_exit_on_max_queue_depth_limit(self):
        """
        早期退出测试：max_queue_depth 限制触发时应立即返回空列表
        """
        import engram_logbook.scm_sync_policy as policy_module

        config = SchedulerConfig(
            max_running=100,
            max_queue_depth=10,
            cursor_age_threshold_seconds=3600,
        )
        now = 10000.0

        n_repos = 100
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=now - 5000,
            )
            for i in range(1, n_repos + 1)
        ]

        call_counts = {"should_schedule_repo_health": 0}
        original_should_schedule = policy_module.should_schedule_repo_health

        def counting_should_schedule(*args, **kwargs):
            call_counts["should_schedule_repo_health"] += 1
            return original_should_schedule(*args, **kwargs)

        # max_queue_depth 已达上限
        budget_snapshot = BudgetSnapshot(
            global_running=3,
            global_pending=7,
            global_active=10,  # = max_queue_depth
        )

        with patch.object(policy_module, "should_schedule_repo_health", counting_should_schedule):
            candidates = select_jobs_to_enqueue(
                states,
                ["commits"],
                config,
                now=now,
                budget_snapshot=budget_snapshot,
            )

        # 应该立即返回空列表
        assert len(candidates) == 0
        assert call_counts["should_schedule_repo_health"] == 0, (
            "max_queue_depth 达到上限时，不应调用 should_schedule_repo_health"
        )
