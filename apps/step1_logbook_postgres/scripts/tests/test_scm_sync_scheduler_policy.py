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

import time
import pytest
from unittest.mock import MagicMock, patch

from engram_step1.scm_sync_policy import (
    SchedulerConfig,
    RepoSyncState,
    SyncJobCandidate,
    BudgetSnapshot,
    calculate_cursor_age,
    calculate_failure_rate,
    calculate_rate_limit_rate,
    should_schedule_repo,
    should_schedule_repo_health,
    compute_job_priority,
    select_jobs_to_enqueue,
    compute_backfill_window,
    # Bucket 暂停相关
    InstanceBucketStatus,
    calculate_bucket_priority_penalty,
    should_skip_due_to_bucket_pause,
    BUCKET_PAUSED_PRIORITY_PENALTY,
    BUCKET_LOW_TOKENS_PRIORITY_PENALTY,
)


class TestCalculateCursorAge:
    """游标年龄计算测试"""

    def test_returns_inf_when_never_synced(self):
        """从未同步时返回无穷大"""
        age = calculate_cursor_age(None, now=1000.0)
        assert age == float('inf')

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
        adjusted_priority = compute_job_priority(1, "commits", state, config, priority_adjustment=-50)
        
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
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
            states, ["commits", "mrs"], config, now=now,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
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
            states, ["commits", "mrs"], config, now=now,
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
        candidates = select_jobs_to_enqueue(
            states, ["commits"], config, now=now
        )
        
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
        
        candidates = select_jobs_to_enqueue(
            states, ["commits"], config, now=now
        )
        
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
        
        candidates = select_jobs_to_enqueue(
            states, ["commits"], config, now=now
        )
        
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
            budget_snapshot=budget_snapshot,
        )
        
        # 分析预期：
        # - repo 1, 2: gitlab-a 只能再加 1 个，tenant-1 只能再加 1 个
        # - repo 3: gitlab-b 可以，但 tenant-1 只能再加 1 个
        # - repo 4, 5: gitlab-b 可以，tenant-2 可以
        
        # 验证结果
        repo_ids = [c.repo_id for c in candidates]
        
        # gitlab-a 的任务最多 1 个额外
        gitlab_a_count = sum(1 for c in candidates 
            if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance == "gitlab-a.com")
        assert gitlab_a_count <= 1
        
        # tenant-1 的任务最多 1 个额外
        tenant_1_count = sum(1 for c in candidates 
            if next((s for s in states if s.repo_id == c.repo_id)).tenant_id == "tenant-1")
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
            states, ["commits"], config, now=now,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
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
            states, ["commits", "mrs"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            states, ["commits"], config, now=now,
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
            RepoSyncState(repo_id=1, repo_type="git", gitlab_instance="instance-a", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=2, repo_type="git", gitlab_instance="instance-a", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=3, repo_type="git", gitlab_instance="instance-a", cursor_updated_at=now - 5000),
            # instance B: 3 个仓库
            RepoSyncState(repo_id=4, repo_type="git", gitlab_instance="instance-b", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=5, repo_type="git", gitlab_instance="instance-b", cursor_updated_at=now - 5000),
            RepoSyncState(repo_id=6, repo_type="git", gitlab_instance="instance-b", cursor_updated_at=now - 5000),
        ]
        
        candidates = select_jobs_to_enqueue(
            states, ["commits"], config, now=now,
            budget_snapshot=BudgetSnapshot.empty(),
        )
        
        # 每个实例最多 2 个，共 4 个
        assert len(candidates) == 4
        
        # 验证每个实例的数量
        instance_a_count = sum(1 for c in candidates 
            if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance == "instance-a")
        instance_b_count = sum(1 for c in candidates 
            if next((s for s in states if s.repo_id == c.repo_id)).gitlab_instance == "instance-b")
        
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
        from engram_step1.scm_sync_policy import (
            CircuitBreakerController,
            CircuitBreakerConfig,
            CircuitBreakerDecision,
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
        assert instance_a_decision.current_state == CircuitState.OPEN.value, "instance-a 应处于 OPEN 状态"
        # 注意：默认配置下 OPEN 状态可能允许 backfill_only，检查实际行为
        
        # 检查 instance-b 熔断决策
        instance_b_decision = instance_b_breaker.check(instance_b_health)
        assert instance_b_decision.allow_sync is True, "instance-b 应允许同步"
        assert instance_b_decision.current_state == CircuitState.CLOSED.value, "instance-b 应处于 CLOSED 状态"

    def test_instance_breaker_backfill_mode_applies_degradation(self):
        """
        测试：instance 熔断时进入 backfill 模式，应用降级参数
        
        当 instance 处于 OPEN 状态但 backfill_only_mode=True 时：
        - allow_sync 可能为 True（允许 backfill）
        - is_backfill_only 应为 True
        - suggested_batch_size 应为降级值
        """
        from engram_step1.scm_sync_policy import (
            CircuitBreakerController,
            CircuitBreakerConfig,
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
        from engram_step1.scm_sync_policy import (
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
        from engram_step1.scm_sync_policy import (
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
            
            jobs.append({
                "repo_id": candidate.repo_id,
                "mode": task_mode,
                "gitlab_instance": state.gitlab_instance,
            })
        
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
        from engram_step1.scm_sync_policy import (
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


# ============ Pause 记录相关测试 ============


# 导入 db 模块中的 pause 相关函数和类
try:
    import sys
    import os
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
        """paused_pairs 应过滤掉对应的候选"""
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
        
        # repo 1 的 commits 被暂停
        paused_pairs = {(1, "commits")}
        queued_pairs = set()
        
        # 合并 queued 和 paused
        combined_skip_pairs = queued_pairs | paused_pairs
        
        candidates = select_jobs_to_enqueue(
            states, ["commits", "mrs"], config, now=now,
            queued_pairs=combined_skip_pairs,
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
        """paused_pairs 和 queued_pairs 应组合过滤"""
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
        combined_skip_pairs = queued_pairs | paused_pairs
        
        candidates = select_jobs_to_enqueue(
            states, ["commits", "mrs", "reviews"], config, now=now,
            queued_pairs=combined_skip_pairs,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
            queued_pairs=paused_pairs,
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
            states, ["commits", "mrs"], config, now=now,
            queued_pairs=paused_pairs,
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
        
        # repo 1 的 commits 被暂停
        paused_pairs = {(1, "commits")}
        
        candidates = select_jobs_to_enqueue(
            states, ["commits", "mrs"], config, now=now,
            queued_pairs=paused_pairs,  # 通过 queued_pairs 传递 paused
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
            states, ["commits", "mrs", "reviews"], config, now=now,
            queued_pairs=paused_pairs,
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
            states, ["commits", "mrs", "reviews"], config, now=now,
            queued_pairs=paused_pairs,
        )
        
        # repo 1 完全跳过
        repo1_jobs = [c for c in candidates if c.repo_id == 1]
        assert len(repo1_jobs) == 0
        
        # repo 2 正常入队
        repo2_jobs = [c for c in candidates if c.repo_id == 2]
        assert len(repo2_jobs) == 3

    def test_paused_and_queued_combined(self):
        """paused 和 queued 组合过滤"""
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
        combined_skip = paused_pairs | queued_pairs
        
        candidates = select_jobs_to_enqueue(
            states, ["commits", "mrs", "reviews"], config, now=now,
            queued_pairs=combined_skip,
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
            states, ["commits"], config, now=now,
            queued_pairs=paused_pairs,
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
            states, ["commits", "mrs"], config, now=now,
            queued_pairs=paused_pairs,
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
            states, ["commits"], config, now=now,
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
        
        should_skip, reason, remaining = should_skip_due_to_bucket_pause(
            status, skip_on_pause=True
        )
        
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
        
        should_skip, reason, remaining = should_skip_due_to_bucket_pause(
            status, skip_on_pause=True
        )
        
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
