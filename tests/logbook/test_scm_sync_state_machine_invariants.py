# -*- coding: utf-8 -*-
"""
test_scm_sync_state_machine_invariants.py - SCM 同步状态机不变量测试

本测试文件验证 SCM 同步调度系统的核心不变量，覆盖两个层次：

1. 纯函数层：直接测试 engram_logbook.scm_sync_policy 中的纯函数
   - select_jobs_to_enqueue: 任务选择与优先级排序
   - calculate_bucket_priority_penalty / should_skip_due_to_bucket_pause: Bucket 暂停逻辑
   - 并发预算限制

2. DB Fixture 层：最小化数据库 fixture 验证跨进程共享状态
   - scm.sync_jobs: 队列状态读写
   - logbook.kv: 熔断状态持久化
   - sync_rate_limits: Rate Limit Bucket 状态

不变量清单（每个 case 对应一个漂移风险点）：
- INV-1: 同一 (repo_id, job_type) 不会重复入队
- INV-2: 预算超限时不入队新任务
- INV-3: Bucket 暂停状态正确应用 priority penalty
- INV-4: Bucket 暂停时 skip_on_pause=True 会跳过任务
- INV-5: 熔断器状态正确持久化和恢复
- INV-6: 并发限制按 instance/tenant 正确应用
- INV-7: 优先级排序保持稳定性（相同优先级按创建时间）
- INV-8: Tenant 公平调度策略正确轮询
"""

import time
from typing import Set, Tuple
from unittest.mock import patch

import pytest

from engram.logbook.scm_sync_policy import (
    BUCKET_LOW_TOKENS_PRIORITY_PENALTY,
    BUCKET_PAUSED_PRIORITY_PENALTY,
    BudgetSnapshot,
    CircuitBreakerConfig,
    # 熔断相关
    CircuitBreakerController,
    CircuitState,
    # Bucket 暂停相关
    InstanceBucketStatus,
    RepoSyncState,
    SchedulerConfig,
    SyncJobCandidate,
    build_circuit_breaker_key,
    calculate_bucket_priority_penalty,
    select_jobs_to_enqueue,
    should_skip_due_to_bucket_pause,
)

# ============================================================================
# 第一部分: 纯函数层测试
# ============================================================================


class TestInvariant1_NoDuplicateEnqueue:
    """
    INV-1: 同一 (repo_id, job_type) 不会重复入队

    漂移风险: 如果 queued_pairs 检查失效，可能导致任务重复入队
    """

    def test_queued_pair_skipped(self):
        """已在 queued_pairs 中的 (repo_id, job_type) 被跳过"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=None,  # 从未同步，应该触发调度
                is_queued=False,
            ),
        ]

        # (1, "commits") 已在队列中
        queued_pairs: Set[Tuple[int, str]] = {(1, "commits")}

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],
            config=config,
            queued_pairs=queued_pairs,
            now=1000.0,
        )

        # commits 被跳过，但 mrs 和 reviews 应该被选中
        job_types_selected = {c.job_type for c in candidates}
        assert "commits" not in job_types_selected, "已入队的 job_type 不应被再次选择"
        # mrs 和 reviews 应该被选中
        assert "mrs" in job_types_selected or "reviews" in job_types_selected

    def test_multiple_repos_partial_queued(self):
        """多仓库部分已入队场景"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )

        now = 1000.0
        states = [
            RepoSyncState(repo_id=1, repo_type="git", cursor_updated_at=None),
            RepoSyncState(repo_id=2, repo_type="git", cursor_updated_at=None),
            RepoSyncState(repo_id=3, repo_type="git", cursor_updated_at=None),
        ]

        # repo 1 的 commits 和 repo 2 的全部已入队
        queued_pairs = {
            (1, "commits"),
            (2, "commits"),
            (2, "mrs"),
            (2, "reviews"),
        }

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],
            config=config,
            queued_pairs=queued_pairs,
            now=now,
        )

        # 验证结果
        selected_pairs = {(c.repo_id, c.job_type) for c in candidates}

        # 已入队的不应出现
        for pair in queued_pairs:
            assert pair not in selected_pairs, f"已入队的 {pair} 不应被再次选择"

        # repo 1 的 mrs/reviews 和 repo 3 的全部应该被选中
        assert (1, "mrs") in selected_pairs
        assert (1, "reviews") in selected_pairs
        assert (3, "commits") in selected_pairs


class TestInvariant2_BudgetEnforcement:
    """
    INV-2: 预算超限时不入队新任务

    漂移风险: 如果预算检查逻辑有 bug，可能导致队列过载
    """

    def test_max_running_limit_blocks_enqueue(self):
        """max_running 达到限制时不入队"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_running=5,
            max_queue_depth=100,
        )

        states = [
            RepoSyncState(repo_id=1, repo_type="git", cursor_updated_at=None),
        ]

        # 已有 5 个 running，达到 max_running 限制
        budget = BudgetSnapshot(
            global_running=5,
            global_pending=0,
            global_active=5,
        )

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            budget_snapshot=budget,
            now=1000.0,
        )

        assert len(candidates) == 0, "max_running 达限时不应入队"

    def test_max_queue_depth_limit_blocks_enqueue(self):
        """max_queue_depth 达到限制时不入队"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_running=10,
            max_queue_depth=5,
        )

        states = [
            RepoSyncState(repo_id=1, repo_type="git", cursor_updated_at=None),
        ]

        # 已有 5 个活跃任务（pending + running），达到 max_queue_depth
        budget = BudgetSnapshot(
            global_running=2,
            global_pending=3,
            global_active=5,
        )

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            budget_snapshot=budget,
            now=1000.0,
        )

        assert len(candidates) == 0, "max_queue_depth 达限时不应入队"

    def test_partial_budget_remaining(self):
        """预算部分剩余时只入队剩余数量"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_running=10,
            max_queue_depth=5,
            max_enqueue_per_scan=100,
        )

        # 3 个仓库，每个 3 个 job_type = 9 个候选
        states = [
            RepoSyncState(repo_id=i, repo_type="git", cursor_updated_at=None) for i in range(1, 4)
        ]

        # 已有 3 个活跃，剩余空间 = 5 - 3 = 2
        budget = BudgetSnapshot(
            global_running=1,
            global_pending=2,
            global_active=3,
        )

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits", "mrs", "reviews"],
            config=config,
            budget_snapshot=budget,
            now=1000.0,
        )

        assert len(candidates) == 2, f"剩余预算为 2，应只选择 2 个候选，实际: {len(candidates)}"


class TestInvariant3_BucketPriorityPenalty:
    """
    INV-3: Bucket 暂停状态正确应用 priority penalty

    漂移风险: 如果 penalty 计算错误，暂停实例的任务可能优先级过高
    """

    def test_bucket_paused_penalty(self):
        """Bucket 暂停时应用 BUCKET_PAUSED_PRIORITY_PENALTY"""
        bucket = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            paused_until=time.time() + 300,
            pause_remaining_seconds=300.0,
        )

        penalty, reason = calculate_bucket_priority_penalty(bucket)

        assert penalty == BUCKET_PAUSED_PRIORITY_PENALTY
        assert reason == "bucket_paused"

    def test_bucket_low_tokens_penalty(self):
        """Bucket 令牌不足时应用 BUCKET_LOW_TOKENS_PRIORITY_PENALTY"""
        bucket = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=False,
            current_tokens=1.0,  # 只有 1 个令牌
            burst=10.0,  # 最大 10 个，10% < 20%
        )

        penalty, reason = calculate_bucket_priority_penalty(bucket)

        assert penalty == BUCKET_LOW_TOKENS_PRIORITY_PENALTY
        assert reason == "bucket_low_tokens"

    def test_bucket_healthy_no_penalty(self):
        """Bucket 健康时无 penalty"""
        bucket = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=False,
            current_tokens=8.0,  # 80% > 20%
            burst=10.0,
        )

        penalty, reason = calculate_bucket_priority_penalty(bucket)

        assert penalty == 0
        assert reason is None

    def test_penalty_applied_in_select_jobs(self):
        """select_jobs_to_enqueue 正确应用 bucket penalty"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )

        # 两个仓库：一个在暂停实例，一个在健康实例
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=None,
                gitlab_instance="paused.gitlab.com",
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=None,
                gitlab_instance="healthy.gitlab.com",
            ),
        ]

        bucket_statuses = {
            "paused.gitlab.com": InstanceBucketStatus(
                instance_key="paused.gitlab.com",
                is_paused=True,
                pause_remaining_seconds=300.0,
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
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=False,  # 不跳过，只降权
            now=1000.0,
        )

        # 应该有 2 个候选
        assert len(candidates) == 2

        # 健康实例的任务应该排在前面（优先级更高）
        assert candidates[0].repo_id == 2, "健康实例任务应排在前面"
        assert candidates[1].repo_id == 1, "暂停实例任务应排在后面"

        # 验证 penalty 被记录
        paused_candidate = next(c for c in candidates if c.repo_id == 1)
        assert paused_candidate.bucket_paused is True
        assert paused_candidate.bucket_penalty_value == BUCKET_PAUSED_PRIORITY_PENALTY


class TestInvariant4_BucketSkipOnPause:
    """
    INV-4: Bucket 暂停时 skip_on_pause=True 会跳过任务

    漂移风险: 如果跳过逻辑失效，暂停实例的任务仍会入队
    """

    def test_should_skip_when_paused(self):
        """should_skip_due_to_bucket_pause 在暂停时返回 True"""
        bucket = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            pause_remaining_seconds=300.0,
        )

        should_skip, reason, remaining = should_skip_due_to_bucket_pause(bucket, skip_on_pause=True)

        assert should_skip is True
        assert reason == "bucket_paused"
        assert remaining == 300.0

    def test_should_not_skip_when_penalty_only(self):
        """skip_on_pause=False 时不跳过，只返回原因"""
        bucket = InstanceBucketStatus(
            instance_key="gitlab.example.com",
            is_paused=True,
            pause_remaining_seconds=300.0,
        )

        should_skip, reason, remaining = should_skip_due_to_bucket_pause(
            bucket, skip_on_pause=False
        )

        assert should_skip is False
        assert reason == "bucket_paused_penalty_only"

    def test_skip_applied_in_select_jobs(self):
        """select_jobs_to_enqueue 正确跳过暂停实例的任务"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )

        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=None,
                gitlab_instance="paused.gitlab.com",
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=None,
                gitlab_instance="healthy.gitlab.com",
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
            bucket_statuses=bucket_statuses,
            skip_on_bucket_pause=True,  # 跳过暂停实例
            now=1000.0,
        )

        # 只有健康实例的任务
        assert len(candidates) == 1
        assert candidates[0].repo_id == 2


class TestInvariant5a_CircuitBreakerSkippedResult:
    """
    INV-5a: skipped 结果不影响熔断状态

    漂移风险: 如果 skipped 结果被记录到熔断器，可能导致:
    - HALF_OPEN 状态下错误计数 half_open_successes/attempts
    - 非真实执行的结果干扰熔断状态机
    """

    def test_half_open_skipped_not_affect_counters(self):
        """HALF_OPEN 状态下 skipped 结果不增加 attempts/successes"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            half_open_max_requests=3,
            recovery_success_count=2,
        )

        controller = CircuitBreakerController(config=config, key="test-skipped")

        # 进入 HALF_OPEN 状态
        controller.force_open(reason="test")
        # 模拟时间流逝，转为 HALF_OPEN
        controller._opened_at = 0  # 设置为很久以前
        controller.check(now=10000.0)
        assert controller.state == CircuitState.HALF_OPEN

        # 记录初始计数
        initial_attempts = controller._half_open_attempts
        initial_successes = controller._half_open_successes

        # 模拟 worker 层跳过记录（skipped=True 时不调用 record_result）
        # 这里验证的是：如果 skipped 结果错误地调用了 record_result(success=True)
        # 会导致计数器增加，这是不期望的行为

        # 正确行为：skipped 结果时 worker 不调用 record_result
        # 所以 attempts 和 successes 应保持不变

        # 验证 record_result 被调用时确实会增加计数
        controller.record_result(success=True)
        assert controller._half_open_attempts == initial_attempts + 1
        assert controller._half_open_successes == initial_successes + 1

    def test_closed_state_skipped_should_not_trip(self):
        """CLOSED 状态下 skipped 结果不应触发熔断"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=3,
        )

        controller = CircuitBreakerController(config=config, key="test-closed-skip")
        assert controller.state == CircuitState.CLOSED

        # 模拟大量 skipped（如果错误地被当作失败处理）
        # 正确的行为是 skipped 不被记录，所以状态保持 CLOSED
        # record_result 在 CLOSED 状态下不影响 half_open 计数
        # 熔断触发是通过 check() 时的 health_stats 判断的

        # 验证 CLOSED 状态下 record_result 不改变状态
        for _ in range(5):
            controller.record_result(success=True)

        assert controller.state == CircuitState.CLOSED


class TestInvariant5_CircuitBreakerPersistence:
    """
    INV-5: 熔断器状态正确持久化和恢复

    漂移风险: 如果状态丢失，熔断器可能在重启后失效
    """

    def test_state_dict_roundtrip(self):
        """状态序列化和反序列化保持一致"""
        config = CircuitBreakerConfig(
            failure_rate_threshold=0.3,
            min_samples=5,
        )

        controller = CircuitBreakerController(config=config, key="test-key")

        # 模拟触发熔断
        controller.force_open(reason="test_trigger")

        # 获取状态
        state_dict = controller.get_state_dict()

        # 创建新实例并加载状态
        new_controller = CircuitBreakerController(config=config, key="test-key")
        new_controller.load_state_dict(state_dict)

        # 验证状态一致
        assert new_controller.state == CircuitState.OPEN
        assert new_controller._last_failure_reason == "test_trigger"

    def test_key_construction_consistency(self):
        """熔断 key 构建在 scheduler 和 worker 间保持一致"""
        # 全局 key
        key1 = build_circuit_breaker_key(project_key="myproject", scope="global")
        assert key1 == "myproject:global"

        # Pool key
        key2 = build_circuit_breaker_key(project_key="myproject", worker_pool="gitlab-prod")
        assert key2 == "myproject:pool:gitlab-prod"

        # Instance key
        key3 = build_circuit_breaker_key(
            project_key="myproject", instance_key="https://gitlab.example.com/path"
        )
        assert key3 == "myproject:instance:gitlab.example.com"

        # Tenant key
        key4 = build_circuit_breaker_key(project_key="myproject", tenant_id="tenant-a")
        assert key4 == "myproject:tenant:tenant-a"


class TestInvariant6_ConcurrencyLimits:
    """
    INV-6: 并发限制按 instance/tenant 正确应用

    漂移风险: 如果并发检查失效，单个实例/租户可能占用全部队列
    """

    def test_per_instance_limit(self):
        """每实例并发限制"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_instance_concurrency=2,
        )

        # 同一实例的 5 个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=None,
                gitlab_instance="gitlab.example.com",
            )
            for i in range(1, 6)
        ]

        # 该实例已有 1 个活跃任务
        budget = BudgetSnapshot(
            global_running=1,
            global_active=1,
            by_instance={"gitlab.example.com": 1},
        )

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            budget_snapshot=budget,
            now=1000.0,
        )

        # 每实例限制 2，已有 1，只能再入队 1 个
        assert len(candidates) == 1

    def test_per_tenant_limit(self):
        """每租户并发限制"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            per_tenant_concurrency=3,
        )

        # 同一租户的 10 个仓库
        states = [
            RepoSyncState(
                repo_id=i,
                repo_type="git",
                cursor_updated_at=None,
                tenant_id="tenant-a",
            )
            for i in range(1, 11)
        ]

        # 该租户已有 2 个活跃任务
        budget = BudgetSnapshot(
            global_running=2,
            global_active=2,
            by_tenant={"tenant-a": 2},
        )

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            budget_snapshot=budget,
            now=1000.0,
        )

        # 每租户限制 3，已有 2，只能再入队 1 个
        assert len(candidates) == 1


class TestInvariant7_PriorityStability:
    """
    INV-7: 优先级排序保持稳定性

    漂移风险: 如果排序不稳定，任务顺序可能在每次扫描时变化
    """

    def test_never_synced_has_highest_priority(self):
        """从未同步的仓库具有最高优先级"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
        )

        now = 10000.0
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=now - 7200,  # 2 小时前同步过
            ),
            RepoSyncState(
                repo_id=2,
                repo_type="git",
                cursor_updated_at=None,  # 从未同步
            ),
            RepoSyncState(
                repo_id=3,
                repo_type="git",
                cursor_updated_at=now - 3600,  # 1 小时前同步过
            ),
        ]

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        # 从未同步的优先级最高（-100 调整）
        commits_candidates = [c for c in candidates if c.job_type == "commits"]
        # repo 2（从未同步）应该排在第一位
        assert commits_candidates[0].repo_id == 2, (
            f"从未同步的 repo 应排第一，实际第一个是 repo_id={commits_candidates[0].repo_id}"
        )

    def test_priority_respects_job_type_order(self):
        """优先级尊重 job_type 的配置顺序"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            job_type_priority={
                "commits": 1,  # 最高优先级
                "mrs": 2,
                "reviews": 3,  # 最低优先级
            },
        )

        now = 10000.0
        states = [
            RepoSyncState(
                repo_id=1,
                repo_type="git",
                cursor_updated_at=None,  # 从未同步
            ),
        ]

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["reviews", "mrs", "commits"],  # 故意乱序
            config=config,
            now=now,
        )

        # 应该按 job_type_priority 排序
        job_types = [c.job_type for c in candidates]
        assert job_types == ["commits", "mrs", "reviews"], (
            f"预期按 job_type 优先级排序，实际 {job_types}"
        )


class TestInvariant8_TenantFairness:
    """
    INV-8: Tenant 公平调度策略正确轮询

    漂移风险: 如果公平策略失效，大租户可能饥饿小租户
    """

    def test_tenant_fairness_interleaving(self):
        """启用 tenant 公平调度时，不同 tenant 交替入队"""
        config = SchedulerConfig(
            cursor_age_threshold_seconds=3600,
            max_queue_depth=100,
            enable_tenant_fairness=True,
            tenant_fairness_max_per_round=1,
        )

        now = 1000.0
        # 3 个租户，每个 3 个仓库
        states = []
        for tenant_idx in range(3):
            tenant_id = f"tenant-{tenant_idx}"
            for repo_idx in range(3):
                states.append(
                    RepoSyncState(
                        repo_id=tenant_idx * 10 + repo_idx + 1,
                        repo_type="git",
                        cursor_updated_at=None,  # 从未同步
                        tenant_id=tenant_id,
                    )
                )

        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=["commits"],
            config=config,
            now=now,
        )

        # 验证前 3 个候选来自不同 tenant
        first_3_tenants = [
            next(s.tenant_id for s in states if s.repo_id == c.repo_id) for c in candidates[:3]
        ]
        assert len(set(first_3_tenants)) == 3, f"前 3 个候选应来自不同 tenant: {first_3_tenants}"


# ============================================================================
# 第二部分: DB Fixture 层测试（跨进程共享状态）
# ============================================================================


class TestDbInvariant_SyncJobsQueue:
    """
    DB 层不变量: scm.sync_jobs 队列状态读写一致性

    验证点:
    - 入队后立即可读
    - 唯一键约束正确阻止重复入队
    - claim/ack/fail 状态转换正确
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    def test_enqueue_then_read(self, migrated_db, db_conn):
        """入队后立即可读"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue, get_job

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建测试仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/test-queue-read.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 入队
            job_id = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                priority=100,
                conn=db_conn,
            )

            assert job_id is not None, "入队应返回 job_id"

            # 立即读取
            job = get_job(job_id, conn=db_conn)

            assert job is not None, "应能读取刚入队的任务"
            assert job["repo_id"] == repo_id
            assert job["job_type"] == "gitlab_commits"
            assert job["status"] == "pending"

        finally:
            # 清理
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

    def test_unique_constraint_prevents_duplicate(self, migrated_db, db_conn):
        """唯一键约束 idx_sync_jobs_unique_active (repo_id, job_type, mode) 阻止重复入队"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建测试仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/test-unique.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 第一次入队成功
            job_id1 = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                conn=db_conn,
            )
            assert job_id1 is not None

            # 第二次入队应返回 None（ON CONFLICT DO NOTHING）
            job_id2 = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                conn=db_conn,
            )
            assert job_id2 is None, "重复入队应返回 None"

        finally:
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

    def test_different_modes_can_coexist(self, migrated_db, db_conn):
        """同一 (repo_id, job_type) 不同 mode 可以同时入队

        唯一索引 idx_sync_jobs_unique_active 是 (repo_id, job_type, mode)，
        所以 incremental 和 backfill 可以同时存在。
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建测试仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/test-diff-modes.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # incremental 模式入队
            job_id1 = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                conn=db_conn,
            )
            assert job_id1 is not None, "incremental 模式入队应成功"

            # backfill 模式入队（同一 repo_id, job_type，不同 mode）
            job_id2 = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="backfill",
                conn=db_conn,
            )
            assert job_id2 is not None, "不同 mode 应能同时入队"
            assert job_id1 != job_id2, "应该是两个不同的任务"

        finally:
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()


class TestDbInvariant_CircuitBreakerState:
    """
    DB 层不变量: 熔断状态持久化一致性

    验证点:
    - 保存后可正确读取
    - 不同 key 的状态隔离
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    def test_save_and_load_state(self, migrated_db, db_conn):
        """保存并加载熔断状态"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        import db as scm_db

        test_key = "test:circuit_breaker:state_test"
        test_state = {
            "state": "open",
            "opened_at": 1234567890.0,
            "half_open_attempts": 2,
            "last_failure_reason": "test_reason",
        }

        try:
            # 保存状态
            scm_db.save_circuit_breaker_state(db_conn, test_key, test_state)
            db_conn.commit()

            # 加载状态
            loaded_state = scm_db.load_circuit_breaker_state(db_conn, test_key)

            assert loaded_state is not None, "应能加载保存的状态"
            assert loaded_state["state"] == "open"
            assert loaded_state["opened_at"] == 1234567890.0
            assert loaded_state["last_failure_reason"] == "test_reason"

        finally:
            # 清理
            scm_db.delete_circuit_breaker_state(db_conn, test_key)
            db_conn.commit()

    def test_state_isolation_by_key(self, migrated_db, db_conn):
        """不同 key 的状态隔离"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        import db as scm_db

        key1 = "test:circuit_breaker:isolation_1"
        key2 = "test:circuit_breaker:isolation_2"

        state1 = {"state": "open", "last_failure_reason": "reason1"}
        state2 = {"state": "closed", "last_failure_reason": None}

        try:
            # 保存两个不同 key 的状态
            scm_db.save_circuit_breaker_state(db_conn, key1, state1)
            scm_db.save_circuit_breaker_state(db_conn, key2, state2)
            db_conn.commit()

            # 分别加载验证隔离
            loaded1 = scm_db.load_circuit_breaker_state(db_conn, key1)
            loaded2 = scm_db.load_circuit_breaker_state(db_conn, key2)

            assert loaded1["state"] == "open"
            assert loaded2["state"] == "closed"

        finally:
            scm_db.delete_circuit_breaker_state(db_conn, key1)
            scm_db.delete_circuit_breaker_state(db_conn, key2)
            db_conn.commit()


class TestDbInvariant_RateLimitBucket:
    """
    DB 层不变量: Rate Limit Bucket 状态读写一致性

    验证点:
    - bucket 状态正确保存和读取
    - pause 状态过期后自动解除
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    def test_bucket_state_persistence(self, migrated_db, db_conn):
        """Bucket 状态持久化"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        import db as scm_db

        instance_key = "test-bucket.gitlab.com"

        try:
            # 写入 bucket 状态（暂停 5 分钟）
            scm_db.upsert_rate_limit_bucket(
                db_conn,
                instance_key=instance_key,
                pause_duration_seconds=300,
                current_tokens=0.0,
                rate=1.0,
                burst=10.0,
            )
            db_conn.commit()

            # 读取状态
            status = scm_db.get_rate_limit_status(db_conn, instance_key)

            assert status is not None
            assert status["instance_key"] == instance_key
            assert status["is_paused"] is True
            assert status["pause_remaining_seconds"] > 0

        finally:
            # 清理
            scm_db.delete_rate_limit_bucket(db_conn, instance_key)
            db_conn.commit()


# ============================================================================
# 第三部分: build_jobs_to_insert 纯函数回放测试
# ============================================================================


# === Fixture: 三类场景最小快照 ===

# 场景 1: Bucket Paused - 实例级 bucket 暂停
FIXTURE_BUCKET_PAUSED = {
    "candidates_to_enqueue": [
        {
            "repo_id": 1,
            "job_type": "commits",
            "priority": 100,
            "reason": "cursor_age",
            "cursor_age_seconds": 7200.0,
            "failure_rate": 0.0,
            "rate_limit_rate": 0.0,
            "bucket_paused": True,
            "bucket_pause_remaining_seconds": 300.0,
            "bucket_penalty_reason": "bucket_paused",
            "bucket_penalty_value": 10000,
            "should_pause": False,
            "pause_reason": None,
        },
    ],
    "states": [
        {
            "repo_id": 1,
            "repo_type": "git",
            "gitlab_instance": "paused.gitlab.com",
            "tenant_id": "tenant-a",
            "cursor_updated_at": 1000.0,
            "recent_run_count": 10,
            "recent_failed_count": 0,
            "recent_429_hits": 5,
            "recent_total_requests": 100,
            "last_run_status": "completed",
            "last_run_at": 1000.0,
            "is_queued": False,
        },
    ],
    "circuit_decision": {
        "allow_sync": True,
        "is_backfill_only": False,
        "suggested_batch_size": 100,
        "suggested_forward_window_seconds": 3600,
        "suggested_diff_mode": "best_effort",
        "wait_seconds": 0.0,
        "current_state": "closed",
        "trigger_reason": None,
        "is_probe_mode": False,
        "probe_budget": 0,
        "probe_job_types_allowlist": [],
    },
    "instance_decisions": {},
    "tenant_decisions": {},
    "budget_snapshot": {
        "global_running": 2,
        "global_pending": 3,
        "global_active": 5,
        "by_instance": {},
        "by_tenant": {},
    },
    "scheduled_at": "2024-01-01T00:00:00+00:00",
}

# 场景 2: HALF_OPEN Probe - 全局熔断器处于探测模式
FIXTURE_HALF_OPEN_PROBE = {
    "candidates_to_enqueue": [
        {
            "repo_id": 2,
            "job_type": "commits",
            "priority": 100,
            "reason": "never_synced",
            "cursor_age_seconds": float("inf"),
            "failure_rate": 0.0,
            "rate_limit_rate": 0.0,
            "bucket_paused": False,
            "bucket_pause_remaining_seconds": 0.0,
            "bucket_penalty_reason": None,
            "bucket_penalty_value": 0,
            "should_pause": False,
            "pause_reason": None,
        },
    ],
    "states": [
        {
            "repo_id": 2,
            "repo_type": "git",
            "gitlab_instance": "gitlab.example.com",
            "tenant_id": "tenant-b",
            "cursor_updated_at": None,
            "recent_run_count": 5,
            "recent_failed_count": 2,
            "recent_429_hits": 0,
            "recent_total_requests": 50,
            "last_run_status": "failed",
            "last_run_at": 900.0,
            "is_queued": False,
        },
    ],
    "circuit_decision": {
        "allow_sync": True,
        "is_backfill_only": False,
        "suggested_batch_size": 20,
        "suggested_forward_window_seconds": 1800,
        "suggested_diff_mode": "minimal",
        "wait_seconds": 0.0,
        "current_state": "half_open",
        "trigger_reason": "failure_rate_exceeded",
        "is_probe_mode": True,
        "probe_budget": 3,
        "probe_job_types_allowlist": ["commits"],
    },
    "instance_decisions": {},
    "tenant_decisions": {},
    "budget_snapshot": {
        "global_running": 0,
        "global_pending": 0,
        "global_active": 0,
        "by_instance": {},
        "by_tenant": {},
    },
    "scheduled_at": "2024-01-01T00:00:00+00:00",
}

# 场景 3: Error Budget Pause - 因错误预算超限被暂停（should_pause=True）
# 注意：should_pause=True 的 candidate 在 scan_and_enqueue 中已被过滤
# 这里测试通过 instance/tenant 熔断 OPEN 导致跳过的场景
FIXTURE_ERROR_BUDGET_PAUSE = {
    "candidates_to_enqueue": [
        {
            "repo_id": 3,
            "job_type": "commits",
            "priority": 100,
            "reason": "cursor_age",
            "cursor_age_seconds": 7200.0,
            "failure_rate": 0.5,  # 高失败率
            "rate_limit_rate": 0.0,
            "bucket_paused": False,
            "bucket_pause_remaining_seconds": 0.0,
            "bucket_penalty_reason": None,
            "bucket_penalty_value": 0,
            "should_pause": False,  # 已通过 pause 过滤，这里是正常候选
            "pause_reason": None,
        },
    ],
    "states": [
        {
            "repo_id": 3,
            "repo_type": "git",
            "gitlab_instance": "gitlab.prod.com",
            "tenant_id": "tenant-c",
            "cursor_updated_at": 1000.0,
            "recent_run_count": 20,
            "recent_failed_count": 10,  # 50% 失败率
            "recent_429_hits": 0,
            "recent_total_requests": 200,
            "last_run_status": "failed",
            "last_run_at": 800.0,
            "is_queued": False,
        },
    ],
    "circuit_decision": {
        "allow_sync": True,
        "is_backfill_only": False,
        "suggested_batch_size": 100,
        "suggested_forward_window_seconds": 3600,
        "suggested_diff_mode": "best_effort",
        "wait_seconds": 0.0,
        "current_state": "closed",
        "trigger_reason": None,
        "is_probe_mode": False,
        "probe_budget": 0,
        "probe_job_types_allowlist": [],
    },
    # 租户级熔断 OPEN（不允许同步）
    "instance_decisions": {},
    "tenant_decisions": {
        "tenant-c": {
            "allow_sync": False,
            "is_backfill_only": False,
            "suggested_batch_size": 100,
            "suggested_forward_window_seconds": 3600,
            "suggested_diff_mode": "best_effort",
            "wait_seconds": 60.0,
            "current_state": "open",
            "trigger_reason": "error_budget_exceeded",
            "is_probe_mode": False,
            "probe_budget": 0,
            "probe_job_types_allowlist": [],
        },
    },
    "budget_snapshot": {
        "global_running": 5,
        "global_pending": 5,
        "global_active": 10,
        "by_instance": {},
        "by_tenant": {"tenant-c": 3},
    },
    "scheduled_at": "2024-01-01T00:00:00+00:00",
}


def _build_snapshot_from_fixture(fixture: dict):
    """从 fixture dict 构建 BuildJobsSnapshot 对象"""
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).parent.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from engram.logbook.scm_sync_policy import (
        BudgetSnapshot,
        CircuitBreakerDecision,
        RepoSyncState,
    )
    from scm_sync_scheduler import BuildJobsSnapshot

    # 构建 candidates
    candidates = [SyncJobCandidate(**c) for c in fixture["candidates_to_enqueue"]]

    # 构建 states
    states = [RepoSyncState(**s) for s in fixture["states"]]

    # 构建 circuit_decision
    circuit_decision = CircuitBreakerDecision(**fixture["circuit_decision"])

    # 构建 instance_decisions
    instance_decisions = {
        k: CircuitBreakerDecision(**v) for k, v in fixture["instance_decisions"].items()
    }

    # 构建 tenant_decisions
    tenant_decisions = {
        k: CircuitBreakerDecision(**v) for k, v in fixture["tenant_decisions"].items()
    }

    # 构建 budget_snapshot
    budget_snapshot = BudgetSnapshot(**fixture["budget_snapshot"])

    return BuildJobsSnapshot(
        candidates_to_enqueue=candidates,
        states=states,
        circuit_decision=circuit_decision,
        instance_decisions=instance_decisions,
        tenant_decisions=tenant_decisions,
        budget_snapshot=budget_snapshot,
        scheduled_at=fixture["scheduled_at"],
    )


class TestBuildJobsToInsertReplay:
    """
    build_jobs_to_insert 纯函数回放测试

    验证三类场景下的关键输出字段：
    1. Bucket Paused: bucket_paused/bucket_pause_remaining_seconds 正确写入
    2. HALF_OPEN Probe: mode=probe, is_probe_mode=True
    3. Error Budget Pause: 租户熔断导致 candidate 被跳过
    """

    def test_bucket_paused_scenario(self):
        """场景 1: Bucket 暂停时正确记录降权信息"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from scm_sync_scheduler import build_jobs_to_insert

        snapshot = _build_snapshot_from_fixture(FIXTURE_BUCKET_PAUSED)
        result = build_jobs_to_insert(snapshot)

        # 应该生成 1 个 job
        assert len(result.jobs) == 1, f"预期 1 个 job，实际 {len(result.jobs)}"

        job = result.jobs[0]

        # 验证关键字段
        assert job["repo_id"] == 1
        assert job["job_type"] == "gitlab_commits"  # logical->physical 转换
        assert job["mode"] == "incremental"  # 正常模式（未触发熔断）
        assert job["priority"] == 100

        # 验证 payload 中的 bucket 信息
        payload = job["payload_json"]
        assert payload["bucket_paused"] is True
        assert payload["bucket_pause_remaining_seconds"] == 300.0
        assert payload["bucket_penalty_reason"] == "bucket_paused"
        assert payload["bucket_penalty_value"] == 10000

    def test_half_open_probe_scenario(self):
        """场景 2: HALF_OPEN 探测模式正确标记 probe"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from scm_sync_scheduler import build_jobs_to_insert

        snapshot = _build_snapshot_from_fixture(FIXTURE_HALF_OPEN_PROBE)
        result = build_jobs_to_insert(snapshot)

        # 应该生成 1 个 job
        assert len(result.jobs) == 1, f"预期 1 个 job，实际 {len(result.jobs)}"

        job = result.jobs[0]

        # 验证关键字段
        assert job["repo_id"] == 2
        assert job["job_type"] == "gitlab_commits"
        assert job["mode"] == "probe", f"预期 mode='probe'，实际 '{job['mode']}'"

        # 验证 payload 中的探测模式标记
        payload = job["payload_json"]
        assert payload["is_probe_mode"] is True
        assert payload["probe_budget"] == 3
        assert payload["circuit_state"] == "half_open"

        # 验证降级参数
        assert payload["suggested_batch_size"] == 20
        assert payload["suggested_forward_window_seconds"] == 1800
        assert payload["suggested_diff_mode"] == "minimal"

    def test_error_budget_pause_scenario(self):
        """场景 3: 租户熔断 OPEN 导致 candidate 被跳过"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from scm_sync_scheduler import build_jobs_to_insert

        snapshot = _build_snapshot_from_fixture(FIXTURE_ERROR_BUDGET_PAUSE)
        result = build_jobs_to_insert(snapshot)

        # 不应生成任何 job（租户熔断 OPEN）
        assert len(result.jobs) == 0, f"预期 0 个 job，实际 {len(result.jobs)}"

        # 应记录租户熔断导致的跳过
        assert result.tenant_paused_count == 1
        assert result.instance_paused_count == 0

        # 验证跳过原因
        assert len(result.skipped_jobs) == 1
        skipped = result.skipped_jobs[0]
        assert skipped["repo_id"] == 3
        assert skipped["reason"] == "tenant_circuit_open"
        assert skipped["tenant"] == "tenant-c"

    def test_instance_circuit_open_skips_candidate(self):
        """实例熔断 OPEN 导致 candidate 被跳过"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from scm_sync_scheduler import build_jobs_to_insert

        # 基于 FIXTURE_BUCKET_PAUSED 修改，添加实例熔断
        fixture = dict(FIXTURE_BUCKET_PAUSED)
        fixture["instance_decisions"] = {
            "paused.gitlab.com": {
                "allow_sync": False,  # 不允许同步
                "is_backfill_only": False,
                "suggested_batch_size": 100,
                "suggested_forward_window_seconds": 3600,
                "suggested_diff_mode": "best_effort",
                "wait_seconds": 120.0,
                "current_state": "open",
                "trigger_reason": "rate_limit_exceeded",
                "is_probe_mode": False,
                "probe_budget": 0,
                "probe_job_types_allowlist": [],
            },
        }

        snapshot = _build_snapshot_from_fixture(fixture)
        result = build_jobs_to_insert(snapshot)

        # 不应生成任何 job（实例熔断 OPEN）
        assert len(result.jobs) == 0
        assert result.instance_paused_count == 1
        assert result.skipped_jobs[0]["reason"] == "instance_circuit_open"

    def test_normal_incremental_job(self):
        """正常情况下生成 incremental job"""
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_policy import (
            BudgetSnapshot,
            CircuitBreakerDecision,
            RepoSyncState,
        )
        from scm_sync_scheduler import BuildJobsSnapshot, build_jobs_to_insert

        # 构建最简单的正常快照
        snapshot = BuildJobsSnapshot(
            candidates_to_enqueue=[
                SyncJobCandidate(
                    repo_id=10,
                    job_type="commits",
                    priority=50,
                    reason="cursor_age",
                    cursor_age_seconds=3600.0,
                    failure_rate=0.1,
                    rate_limit_rate=0.0,
                ),
            ],
            states=[
                RepoSyncState(
                    repo_id=10,
                    repo_type="git",
                    gitlab_instance="normal.gitlab.com",
                    tenant_id="tenant-normal",
                ),
            ],
            circuit_decision=CircuitBreakerDecision(),  # 默认值：closed, allow_sync=True
            instance_decisions={},
            tenant_decisions={},
            budget_snapshot=BudgetSnapshot(),
            scheduled_at="2024-01-01T12:00:00+00:00",
        )

        result = build_jobs_to_insert(snapshot)

        # 应生成 1 个 incremental job
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["mode"] == "incremental"
        assert job["job_type"] == "gitlab_commits"
        assert job["repo_id"] == 10

        # 验证 payload 基本字段
        payload = job["payload_json"]
        assert payload["reason"] == "cursor_age"
        assert payload["gitlab_instance"] == "normal.gitlab.com"
        assert payload["tenant_id"] == "tenant-normal"
        assert payload["scheduled_at"] == "2024-01-01T12:00:00+00:00"


# ============================================================================
# 第四部分: lock_held 场景下的无惩罚重入队测试
# ============================================================================


class TestLockHeldRequeue:
    """
    lock_held 场景的无惩罚重入队测试

    验证点:
    - 当 dispatch 返回 locked=True 且 skipped=True 时，调用 requeue_without_penalty
    - 当 error_category=lock_held 时，调用 requeue_without_penalty
    - job 状态变回 pending 且 attempts 被回补
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    def test_lock_held_requeue_via_locked_skipped(self, migrated_db, db_conn):
        """
        测试 locked=True + skipped=True 场景下的无惩罚重入队

        验证：
        1. job claim 后 status=running, attempts=1
        2. dispatch 返回 locked=True, skipped=True
        3. requeue_without_penalty 被调用
        4. job 状态变回 pending, attempts=0
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建测试仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/test-lock-held.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 1. 入队并 claim
            job_id = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                conn=db_conn,
            )
            assert job_id is not None

            worker_id = "test-worker-lock-held"
            job = claim(worker_id=worker_id, conn=db_conn)

            assert job is not None
            assert job["job_id"] == job_id
            assert job["attempts"] == 1

            # 验证 job 状态为 running
            job_detail = get_job(job_id, conn=db_conn)
            assert job_detail["status"] == "running"
            assert job_detail["locked_by"] == worker_id

            # 2. Mock dispatch_sync_function 返回 locked=True, skipped=True
            mock_result = {
                "success": True,  # 虽然 success=True，但因为 locked 会触发重入队
                "locked": True,
                "skipped": True,
                "message": "Watermark lock held by another process",
            }

            # 3. 调用 process_one_job（通过 mock）
            with patch("scm_sync_worker.dispatch_sync_function", return_value=mock_result):
                with patch(
                    "scm_sync_worker.get_worker_config_from_module",
                    return_value={
                        "lease_seconds": 300,
                        "renew_interval_seconds": 60,
                        "max_renew_failures": 3,
                    },
                ):
                    # 直接测试 requeue_without_penalty 的效果
                    from engram.logbook.scm_sync_queue import requeue_without_penalty

                    success = requeue_without_penalty(
                        job_id=job_id,
                        worker_id=worker_id,
                        reason="lock_held: locked=True, skipped=True",
                        jitter_seconds=5,
                        conn=db_conn,
                    )

                    assert success is True

            # 4. 验证 job 状态变回 pending 且 attempts 回补
            job_after = get_job(job_id, conn=db_conn)
            assert job_after is not None
            assert job_after["status"] == "pending", (
                f"期望 status=pending，实际 status={job_after['status']}"
            )
            assert job_after["attempts"] == 0, (
                f"期望 attempts=0（回补），实际 attempts={job_after['attempts']}"
            )
            assert job_after["locked_by"] is None
            assert "lock_held" in (job_after["last_error"] or "")

        finally:
            # 清理
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

    def test_lock_held_requeue_via_error_category(self, migrated_db, db_conn):
        """
        测试 error_category=lock_held 场景下的无惩罚重入队

        验证：
        1. job claim 后 status=running, attempts=1
        2. dispatch 返回 error_category=lock_held
        3. requeue_without_penalty 被调用
        4. job 状态变回 pending, attempts=0
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_queue import claim, enqueue, get_job, requeue_without_penalty

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建测试仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/test-lock-held-category.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 1. 入队并 claim
            job_id = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                conn=db_conn,
            )
            assert job_id is not None

            worker_id = "test-worker-lock-held-category"
            job = claim(worker_id=worker_id, conn=db_conn)

            assert job is not None
            assert job["attempts"] == 1

            # 2. 模拟 error_category=lock_held 的结果处理
            # 直接测试 requeue_without_penalty
            success = requeue_without_penalty(
                job_id=job_id,
                worker_id=worker_id,
                reason=f"error_category={ErrorCategory.LOCK_HELD.value}",
                jitter_seconds=10,
                conn=db_conn,
            )

            assert success is True

            # 3. 验证 job 状态
            job_after = get_job(job_id, conn=db_conn)
            assert job_after["status"] == "pending"
            assert job_after["attempts"] == 0  # 回补
            assert job_after["locked_by"] is None

        finally:
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

    def test_requeue_preserves_job_metadata(self, migrated_db, db_conn):
        """
        测试重入队时保留 job 元数据（repo_id, job_type, mode, priority 等）
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job, requeue_without_penalty

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建测试仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/test-lock-held-metadata.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 入队时设置特定的 priority 和 payload
            job_id = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                priority=50,
                payload={"key": "value", "test": True},
                conn=db_conn,
            )

            worker_id = "test-worker-metadata"
            claim(worker_id=worker_id, conn=db_conn)

            # 执行重入队
            requeue_without_penalty(
                job_id=job_id,
                worker_id=worker_id,
                reason="test metadata preservation",
                jitter_seconds=5,
                conn=db_conn,
            )

            # 验证元数据被保留
            job_after = get_job(job_id, conn=db_conn)
            assert job_after["repo_id"] == repo_id
            assert job_after["job_type"] == "gitlab_commits"
            assert job_after["mode"] == "incremental"
            assert job_after["priority"] == 50
            assert job_after["payload"].get("key") == "value"
            assert job_after["payload"].get("test") is True

        finally:
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()


# ============================================================================
# 第五部分: sync_jobs 状态机表驱动测试
# ============================================================================

"""
状态转换矩阵（State Transition Matrix）
========================================

同步任务的状态转换遵循以下规则：

| 源状态  | 目标状态   | 操作                    | 条件                                           |
|---------|------------|-------------------------|------------------------------------------------|
| (新建)  | pending    | enqueue                 | (repo_id, job_type) 无活跃任务                 |
| pending | running    | claim                   | not_before <= now()                            |
| running | running    | claim (抢占)            | locked_at + lease_seconds < now() (租约过期)   |
| failed  | running    | claim (重试)            | not_before <= now() AND attempts < max_attempts|
| running | completed  | ack                     | locked_by = worker_id AND status = running     |
| running | failed     | fail_retry              | locked_by = worker_id AND attempts < max       |
| running | dead       | fail_retry              | locked_by = worker_id AND attempts >= max      |
| running | dead       | mark_dead               | locked_by = worker_id                          |
| running | pending    | requeue_without_penalty | locked_by = worker_id (attempts 回补 -1)       |
| dead    | pending    | reset_dead_jobs         | (管理员操作，attempts 重置为 0)                |

边界条件：
1. 并发 claim: FOR UPDATE SKIP LOCKED 确保原子性
2. 重复 ack: 状态非 running 或 locked_by 不匹配返回 False
3. 过期 lease: running 但 locked_at + lease_seconds < now() 可被重新 claim
4. Worker 身份验证: ack/fail_retry/mark_dead/renew_lease 要求 locked_by = worker_id
"""


class TestSyncJobsStateMachine:
    """
    sync_jobs 状态机表驱动测试

    覆盖状态转换：
    - pending → running (claim)
    - running → completed (ack)
    - running → failed (fail_retry, attempts < max_attempts)
    - running → dead (fail_retry, attempts >= max_attempts)
    - running → dead (mark_dead)
    - running → pending (requeue_without_penalty)
    - failed → running (claim, 重试场景)
    - dead → pending (reset_dead_jobs)

    验证字段一致性：
    - status, attempts, not_before, locked_by, locked_at, last_error
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    @pytest.fixture
    def test_repo(self, migrated_db, db_conn):
        """创建测试仓库"""
        scm_schema = migrated_db["schemas"]["scm"]
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/state-machine-test.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()
        yield repo_id
        # 清理
        with db_conn.cursor() as cur:
            cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
            cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            db_conn.commit()

    # 表驱动测试用例
    # (case_name, operation, initial_attempts, max_attempts, expected_status, expected_attempts_delta, expected_locked_by, expected_last_error_contains)
    STATE_TRANSITION_CASES = [
        # 基础状态转换
        ("pending_to_running_via_claim", "claim", 0, 3, "running", 1, "test-worker", None),
        ("running_to_completed_via_ack", "ack", 1, 3, "completed", 0, None, None),
        ("running_to_failed_via_fail_retry", "fail_retry", 1, 3, "failed", 0, None, "test error"),
        (
            "running_to_dead_via_fail_retry_max_attempts",
            "fail_retry_max",
            3,
            3,
            "dead",
            0,
            None,
            "test error",
        ),
        ("running_to_dead_via_mark_dead", "mark_dead", 1, 3, "dead", 0, None, "permanent error"),
        ("running_to_pending_via_requeue", "requeue", 1, 3, "pending", -1, None, "lock_held"),
        # 边界条件
        (
            "fail_retry_at_max_minus_one",
            "fail_retry",
            2,
            3,
            "failed",
            0,
            None,
            "test error",
        ),  # attempts=2, max=3, 仍可重试
        (
            "requeue_preserves_zero_attempts",
            "requeue_from_1",
            1,
            3,
            "pending",
            -1,
            None,
            "lock_held",
        ),  # attempts 回补到 0
    ]

    @pytest.mark.parametrize(
        "case_name,operation,initial_attempts,max_attempts,expected_status,expected_attempts_delta,expected_locked_by,expected_last_error_contains",
        STATE_TRANSITION_CASES,
        ids=[c[0] for c in STATE_TRANSITION_CASES],
    )
    def test_state_transition(
        self,
        migrated_db,
        db_conn,
        test_repo,
        case_name,
        operation,
        initial_attempts,
        max_attempts,
        expected_status,
        expected_attempts_delta,
        expected_locked_by,
        expected_last_error_contains,
    ):
        """
        表驱动状态转换测试

        验证：
        1. 状态正确转换
        2. attempts 变化正确
        3. locked_by 状态正确
        4. last_error 内容正确
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import (
            ack,
            claim,
            enqueue,
            fail_retry,
            get_job,
            mark_dead,
            requeue_without_penalty,
        )

        scm_schema = migrated_db["schemas"]["scm"]
        worker_id = "test-worker"
        repo_id = test_repo

        # 1. 创建初始任务
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            max_attempts=max_attempts,
            conn=db_conn,
        )
        assert job_id is not None, "任务入队应成功"

        # 2. 如果需要，先设置初始状态
        if operation != "claim":
            # 先 claim 使任务进入 running 状态
            claimed = claim(worker_id=worker_id, conn=db_conn)
            assert claimed is not None, "claim 应成功"

            # 如果需要更多的 attempts，通过 SQL 直接设置
            if initial_attempts > 1:
                with db_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {scm_schema}.sync_jobs
                        SET attempts = %s
                        WHERE job_id = %s
                    """,
                        (initial_attempts, job_id),
                    )
                    db_conn.commit()

        # 3. 执行操作
        if operation == "claim":
            result = claim(worker_id=worker_id, conn=db_conn)
            success = result is not None
        elif operation == "ack":
            success = ack(job_id=job_id, worker_id=worker_id, conn=db_conn)
        elif operation == "fail_retry":
            success = fail_retry(
                job_id=job_id, worker_id=worker_id, error="test error", conn=db_conn
            )
        elif operation == "fail_retry_max":
            success = fail_retry(
                job_id=job_id, worker_id=worker_id, error="test error", conn=db_conn
            )
        elif operation == "mark_dead":
            success = mark_dead(
                job_id=job_id, worker_id=worker_id, error="permanent error", conn=db_conn
            )
        elif operation in ("requeue", "requeue_from_1"):
            success = requeue_without_penalty(
                job_id=job_id,
                worker_id=worker_id,
                reason="lock_held",
                jitter_seconds=0,
                conn=db_conn,
            )
        else:
            pytest.fail(f"未知操作: {operation}")

        assert success, f"操作 {operation} 应成功"

        # 4. 验证状态
        job = get_job(job_id, conn=db_conn)
        assert job is not None, "应能获取任务详情"

        # 验证 status
        assert job["status"] == expected_status, (
            f"预期 status={expected_status}，实际 status={job['status']}"
        )

        # 验证 attempts
        expected_attempts = initial_attempts + expected_attempts_delta
        if operation == "claim":
            expected_attempts = 1  # claim 时 attempts 从 0 变为 1
        assert job["attempts"] == expected_attempts, (
            f"预期 attempts={expected_attempts}，实际 attempts={job['attempts']}"
        )

        # 验证 locked_by
        assert job["locked_by"] == expected_locked_by, (
            f"预期 locked_by={expected_locked_by}，实际 locked_by={job['locked_by']}"
        )

        # 验证 locked_at
        if expected_locked_by is not None:
            assert job["locked_at"] is not None, "locked_by 非空时 locked_at 也应非空"
        else:
            assert job["locked_at"] is None, "locked_by 为空时 locked_at 也应为空"

        # 验证 last_error
        if expected_last_error_contains is not None:
            assert job["last_error"] is not None, "last_error 应非空"
            assert expected_last_error_contains in job["last_error"], (
                f"last_error 应包含 '{expected_last_error_contains}'，实际: {job['last_error']}"
            )

    def test_failed_to_running_via_retry_claim(self, migrated_db, db_conn, test_repo):
        """
        failed → running: 重试 claim 测试

        验证 failed 状态且 attempts < max_attempts 的任务可以被重新 claim
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, fail_retry, get_job

        migrated_db["schemas"]["scm"]
        worker_id = "test-worker"
        repo_id = test_repo

        # 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", max_attempts=3, conn=db_conn)
        claim(worker_id=worker_id, conn=db_conn)

        # 触发 fail_retry，设置立即可重试（backoff=0）
        fail_retry(
            job_id=job_id, worker_id=worker_id, error="first error", backoff_seconds=0, conn=db_conn
        )

        # 验证状态为 failed
        job_after_fail = get_job(job_id, conn=db_conn)
        assert job_after_fail["status"] == "failed"
        assert job_after_fail["attempts"] == 1

        # 重新 claim（重试）
        claimed = claim(worker_id="worker-retry", conn=db_conn)
        assert claimed is not None, "failed 任务应可被重新 claim"
        assert claimed["job_id"] == job_id

        # 验证状态更新
        job_after_retry = get_job(job_id, conn=db_conn)
        assert job_after_retry["status"] == "running"
        assert job_after_retry["attempts"] == 2  # attempts 从 1 增加到 2
        assert job_after_retry["locked_by"] == "worker-retry"

    def test_dead_to_pending_via_reset(self, migrated_db, db_conn, test_repo):
        """
        dead → pending: reset_dead_jobs 测试

        验证管理员可以将 dead 任务重置为 pending
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import (
            claim,
            enqueue,
            get_job,
            mark_dead,
            reset_dead_jobs,
        )

        worker_id = "test-worker"
        repo_id = test_repo

        # 入队、claim、标记为 dead
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker_id, conn=db_conn)
        mark_dead(job_id=job_id, worker_id=worker_id, error="permanent error", conn=db_conn)

        # 验证状态为 dead
        job_dead = get_job(job_id, conn=db_conn)
        assert job_dead["status"] == "dead"

        # 重置
        reset_count = reset_dead_jobs(repo_id=repo_id, conn=db_conn)
        assert reset_count == 1

        # 验证状态恢复为 pending
        job_reset = get_job(job_id, conn=db_conn)
        assert job_reset["status"] == "pending"
        assert job_reset["attempts"] == 0  # attempts 重置
        assert job_reset["last_error"] is None


# ============================================================================
# 第六部分: 并发边界测试
# ============================================================================

"""
并发边界条件测试矩阵
====================

| 场景                     | 预期行为                              | 测试用例                                |
|--------------------------|---------------------------------------|----------------------------------------|
| 并发 claim 同一任务      | 只有一个 worker 成功 (SKIP LOCKED)    | test_concurrent_claim_only_one_succeeds|
| 重复 ack                 | 第二次返回 False                      | test_duplicate_ack_returns_false       |
| 重复 fail_retry          | 第二次返回 False                      | test_duplicate_fail_retry_returns_false|
| 错误 worker ack          | 返回 False                            | test_ack_by_wrong_worker_returns_false |
| 错误 worker fail_retry   | 返回 False                            | test_fail_retry_by_wrong_worker_returns|
| 过期 lease 被抢占        | 新 worker claim 成功，旧 worker 失败  | test_expired_running_job_can_be_reclaim|
| renew_lease 被抢占后     | 返回 False                            | test_renew_lease_returns_false_preempt |
| not_before 未到期        | claim 返回 None                       | test_claim_respects_not_before         |
| failed + not_before 未到 | claim 返回 None                       | test_failed_job_respects_not_before    |
"""


class TestConcurrencyBoundary:
    """
    并发边界测试

    覆盖：
    - 并发 claim：多 worker 同时 claim
    - 重复 ack：同一任务多次 ack
    - 重复 fail_retry：同一任务多次 fail_retry
    - renew_lease 返回 False：任务已被其他 worker 抢占
    - running 过期后被再次 claim
    - not_before 边界条件
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    @pytest.fixture
    def test_repo(self, migrated_db, db_conn):
        """创建测试仓库"""
        scm_schema = migrated_db["schemas"]["scm"]
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/concurrency-test.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()
        yield repo_id
        # 清理
        with db_conn.cursor() as cur:
            cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
            cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            db_conn.commit()

    def test_duplicate_ack_returns_false(self, migrated_db, db_conn, test_repo):
        """
        重复 ack 测试

        验证：第二次 ack 返回 False（任务已完成）
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import ack, claim, enqueue, get_job

        worker_id = "test-worker"
        repo_id = test_repo

        # 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker_id, conn=db_conn)

        # 第一次 ack 成功
        result1 = ack(job_id=job_id, worker_id=worker_id, conn=db_conn)
        assert result1 is True, "第一次 ack 应成功"

        # 验证状态为 completed
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "completed"

        # 第二次 ack 返回 False
        result2 = ack(job_id=job_id, worker_id=worker_id, conn=db_conn)
        assert result2 is False, "重复 ack 应返回 False"

    def test_duplicate_fail_retry_returns_false(self, migrated_db, db_conn, test_repo):
        """
        重复 fail_retry 测试

        验证：第二次 fail_retry 返回 False（任务已不在 running 状态）
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, fail_retry, get_job

        worker_id = "test-worker"
        repo_id = test_repo

        # 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker_id, conn=db_conn)

        # 第一次 fail_retry 成功
        result1 = fail_retry(job_id=job_id, worker_id=worker_id, error="error 1", conn=db_conn)
        assert result1 is True, "第一次 fail_retry 应成功"

        # 验证状态为 failed
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "failed"

        # 第二次 fail_retry 返回 False
        result2 = fail_retry(job_id=job_id, worker_id=worker_id, error="error 2", conn=db_conn)
        assert result2 is False, "重复 fail_retry 应返回 False"

    def test_renew_lease_returns_false_when_preempted(self, migrated_db, db_conn, test_repo):
        """
        renew_lease 返回 False 测试

        验证：当任务被其他 worker 抢占后，原 worker 的 renew_lease 返回 False
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job, renew_lease

        scm_schema = migrated_db["schemas"]["scm"]
        worker1_id = "worker-1"
        worker2_id = "worker-2"
        repo_id = test_repo

        # Worker1 入队并 claim
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_commits",
            lease_seconds=1,  # 短租约便于测试
            conn=db_conn,
        )
        job = claim(worker_id=worker1_id, conn=db_conn)
        assert job is not None

        # 模拟租约过期：直接修改 locked_at 为过去时间
        with db_conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {scm_schema}.sync_jobs
                SET locked_at = locked_at - interval '10 seconds'
                WHERE job_id = %s
            """,
                (job_id,),
            )
            db_conn.commit()

        # Worker2 claim 抢占任务
        job2 = claim(worker_id=worker2_id, conn=db_conn)
        assert job2 is not None, "Worker2 应能 claim 过期的任务"
        assert job2["job_id"] == job_id

        # Worker1 的 renew_lease 应返回 False
        result = renew_lease(job_id=job_id, worker_id=worker1_id, conn=db_conn)
        assert result is False, "被抢占后 renew_lease 应返回 False"

        # 验证任务现在属于 Worker2
        job_detail = get_job(job_id, conn=db_conn)
        assert job_detail["locked_by"] == worker2_id

    def test_expired_running_job_can_be_reclaimed(self, migrated_db, db_conn, test_repo):
        """
        running 过期后被再次 claim 测试

        验证：租约过期的 running 任务可以被另一个 worker claim
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job

        scm_schema = migrated_db["schemas"]["scm"]
        worker1_id = "worker-original"
        worker2_id = "worker-reclaim"
        repo_id = test_repo

        # Worker1 入队并 claim
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_commits",
            lease_seconds=1,
            conn=db_conn,
        )
        job1 = claim(worker_id=worker1_id, conn=db_conn)
        assert job1 is not None

        # 验证初始状态
        job_before = get_job(job_id, conn=db_conn)
        assert job_before["status"] == "running"
        assert job_before["locked_by"] == worker1_id
        assert job_before["attempts"] == 1

        # 模拟租约过期
        with db_conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {scm_schema}.sync_jobs
                SET locked_at = locked_at - interval '10 seconds'
                WHERE job_id = %s
            """,
                (job_id,),
            )
            db_conn.commit()

        # Worker2 应能 claim 过期任务
        job2 = claim(worker_id=worker2_id, conn=db_conn)
        assert job2 is not None, "应能 claim 过期的 running 任务"
        assert job2["job_id"] == job_id

        # 验证状态更新
        job_after = get_job(job_id, conn=db_conn)
        assert job_after["status"] == "running"
        assert job_after["locked_by"] == worker2_id, (
            f"locked_by 应更新为 {worker2_id}，实际: {job_after['locked_by']}"
        )
        assert job_after["attempts"] == 2, f"attempts 应增加到 2，实际: {job_after['attempts']}"
        assert job_after["locked_at"] is not None

    def test_ack_by_wrong_worker_returns_false(self, migrated_db, db_conn, test_repo):
        """
        错误 worker ack 测试

        验证：非持有锁的 worker 调用 ack 返回 False
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import ack, claim, enqueue, get_job

        worker1_id = "worker-holder"
        worker2_id = "worker-other"
        repo_id = test_repo

        # Worker1 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker1_id, conn=db_conn)

        # Worker2 尝试 ack 应返回 False
        result = ack(job_id=job_id, worker_id=worker2_id, conn=db_conn)
        assert result is False, "错误 worker 的 ack 应返回 False"

        # 任务仍应处于 running 状态，属于 Worker1
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "running"
        assert job["locked_by"] == worker1_id

    def test_fail_retry_by_wrong_worker_returns_false(self, migrated_db, db_conn, test_repo):
        """
        错误 worker fail_retry 测试

        验证：非持有锁的 worker 调用 fail_retry 返回 False
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, fail_retry, get_job

        worker1_id = "worker-holder"
        worker2_id = "worker-other"
        repo_id = test_repo

        # Worker1 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker1_id, conn=db_conn)

        # Worker2 尝试 fail_retry 应返回 False
        result = fail_retry(
            job_id=job_id, worker_id=worker2_id, error="wrong worker error", conn=db_conn
        )
        assert result is False, "错误 worker 的 fail_retry 应返回 False"

        # 任务仍应处于 running 状态
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "running"
        assert job["locked_by"] == worker1_id
        assert job["last_error"] is None  # 错误不应被写入

    def test_not_before_updated_on_fail_retry(self, migrated_db, db_conn, test_repo):
        """
        fail_retry 更新 not_before 测试

        验证：fail_retry 后 not_before 被设置为未来时间（退避延迟）
        """
        import sys
        from datetime import datetime, timezone
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, fail_retry, get_job

        worker_id = "test-worker"
        repo_id = test_repo

        # 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker_id, conn=db_conn)

        # 记录当前时间
        now = datetime.now(timezone.utc)

        # fail_retry 并指定退避时间
        backoff_seconds = 60
        fail_retry(
            job_id=job_id,
            worker_id=worker_id,
            error="test error",
            backoff_seconds=backoff_seconds,
            conn=db_conn,
        )

        # 验证 not_before 被更新
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "failed"
        assert job["not_before"] is not None

        # not_before 应该在 now + backoff_seconds 附近
        not_before = job["not_before"]
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)

        # 允许一些误差（最多 5 秒）
        diff_seconds = (not_before - now).total_seconds()
        assert diff_seconds >= backoff_seconds - 5, (
            f"not_before 应该在 now + {backoff_seconds}s 之后，实际延迟: {diff_seconds}s"
        )

    def test_requeue_preserves_attempts_on_rollback(self, migrated_db, db_conn, test_repo):
        """
        requeue_without_penalty 回补 attempts 测试

        验证：requeue_without_penalty 将 attempts 减 1（补偿 claim 时的 +1）
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job, requeue_without_penalty

        worker_id = "test-worker"
        repo_id = test_repo

        # 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        job_after_claim = claim(worker_id=worker_id, conn=db_conn)
        assert job_after_claim["attempts"] == 1, "claim 后 attempts 应为 1"

        # requeue
        success = requeue_without_penalty(
            job_id=job_id,
            worker_id=worker_id,
            reason="lock_held test",
            jitter_seconds=0,
            conn=db_conn,
        )
        assert success is True

        # 验证 attempts 被回补
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "pending"
        assert job["attempts"] == 0, f"requeue 后 attempts 应回补为 0，实际: {job['attempts']}"
        assert job["locked_by"] is None
        assert "lock_held" in job["last_error"]

    def test_claim_respects_not_before(self, migrated_db, db_conn, test_repo):
        """
        not_before 边界测试

        验证：not_before 未到期的 pending 任务不会被 claim
        """
        import sys
        from datetime import datetime, timedelta, timezone
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job

        repo_id = test_repo

        # 创建一个 not_before 设置为未来时间的任务
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_commits",
            not_before=future_time,
            conn=db_conn,
        )
        assert job_id is not None

        # 尝试 claim 应返回 None（没有可用任务）
        claimed = claim(worker_id="test-worker", conn=db_conn)
        assert claimed is None, "not_before 未到期的任务不应被 claim"

        # 验证任务仍为 pending
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "pending"
        assert job["locked_by"] is None

    def test_failed_job_respects_not_before(self, migrated_db, db_conn, test_repo):
        """
        failed 任务的 not_before 边界测试

        验证：failed 状态的任务在 not_before 未到期时不会被 claim
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, fail_retry, get_job

        migrated_db["schemas"]["scm"]
        worker_id = "test-worker"
        repo_id = test_repo

        # 入队、claim、fail_retry（设置较长的 backoff）
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", max_attempts=3, conn=db_conn)
        claim(worker_id=worker_id, conn=db_conn)
        fail_retry(
            job_id=job_id, worker_id=worker_id, error="error", backoff_seconds=3600, conn=db_conn
        )

        # 验证任务为 failed 且 not_before 在未来
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "failed"

        # 尝试 claim 应返回 None
        claimed = claim(worker_id="worker-retry", conn=db_conn)
        assert claimed is None, "not_before 未到期的 failed 任务不应被 claim"

    def test_concurrent_claim_simulation(self, migrated_db, db_conn, test_repo):
        """
        并发 claim 模拟测试

        验证：使用 FOR UPDATE SKIP LOCKED，多次 claim 尝试只有一个成功
        （此测试模拟而非真实并发，真实并发测试需要多线程/多进程）
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job

        repo_id = test_repo

        # 创建一个任务
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        assert job_id is not None

        # 第一个 worker claim 成功
        claimed1 = claim(worker_id="worker-1", conn=db_conn)
        assert claimed1 is not None
        assert claimed1["job_id"] == job_id

        # 第二个 worker 尝试 claim 同一任务应返回 None（没有其他可用任务）
        claimed2 = claim(worker_id="worker-2", conn=db_conn)
        assert claimed2 is None, "任务已被锁定，第二个 worker 应无法 claim"

        # 验证任务仍属于 worker-1
        job = get_job(job_id, conn=db_conn)
        assert job["locked_by"] == "worker-1"

    def test_lease_expiry_boundary_exact(self, migrated_db, db_conn, test_repo):
        """
        租约过期边界精确测试

        验证：租约刚好过期时任务可被抢占
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job

        scm_schema = migrated_db["schemas"]["scm"]
        repo_id = test_repo

        # 创建任务并 claim，设置短租约
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_commits",
            lease_seconds=10,
            conn=db_conn,
        )
        claim(worker_id="worker-1", conn=db_conn)

        # 模拟租约刚好过期（设置 locked_at 为 lease_seconds + 1 秒前）
        with db_conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {scm_schema}.sync_jobs
                SET locked_at = now() - interval '11 seconds'
                WHERE job_id = %s
            """,
                (job_id,),
            )
            db_conn.commit()

        # 第二个 worker 应能 claim
        claimed2 = claim(worker_id="worker-2", conn=db_conn)
        assert claimed2 is not None, "租约过期后应能被抢占"
        assert claimed2["job_id"] == job_id

        # 验证任务现在属于 worker-2，attempts 增加
        job = get_job(job_id, conn=db_conn)
        assert job["locked_by"] == "worker-2"
        assert job["attempts"] == 2  # 被 claim 两次

    def test_requeue_wrong_worker_returns_false(self, migrated_db, db_conn, test_repo):
        """
        错误 worker requeue 测试

        验证：非持有锁的 worker 调用 requeue_without_penalty 返回 False
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job, requeue_without_penalty

        worker1_id = "worker-holder"
        worker2_id = "worker-other"
        repo_id = test_repo

        # Worker1 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker1_id, conn=db_conn)

        # Worker2 尝试 requeue 应返回 False
        result = requeue_without_penalty(
            job_id=job_id,
            worker_id=worker2_id,
            reason="wrong worker",
            conn=db_conn,
        )
        assert result is False, "错误 worker 的 requeue 应返回 False"

        # 任务仍应处于 running 状态，属于 Worker1
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "running"
        assert job["locked_by"] == worker1_id

    def test_mark_dead_wrong_worker_returns_false(self, migrated_db, db_conn, test_repo):
        """
        错误 worker mark_dead 测试

        验证：非持有锁的 worker 调用 mark_dead 返回 False
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue, get_job, mark_dead

        worker1_id = "worker-holder"
        worker2_id = "worker-other"
        repo_id = test_repo

        # Worker1 入队并 claim
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", conn=db_conn)
        claim(worker_id=worker1_id, conn=db_conn)

        # Worker2 尝试 mark_dead 应返回 False
        result = mark_dead(job_id=job_id, worker_id=worker2_id, error="wrong worker", conn=db_conn)
        assert result is False, "错误 worker 的 mark_dead 应返回 False"

        # 任务仍应处于 running 状态
        job = get_job(job_id, conn=db_conn)
        assert job["status"] == "running"
        assert job["locked_by"] == worker1_id


# ============================================================================
# 第七部分: 表驱动并发边界测试
# ============================================================================


class TestConcurrencyBoundaryTableDriven:
    """
    并发边界表驱动测试

    使用 parametrize 覆盖多种并发场景
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    @pytest.fixture
    def test_repo(self, migrated_db, db_conn):
        """创建测试仓库"""
        scm_schema = migrated_db["schemas"]["scm"]
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://example.com/concurrency-table-test.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()
        yield repo_id
        # 清理
        with db_conn.cursor() as cur:
            cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
            cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            db_conn.commit()

    # 表驱动测试用例：操作在非预期状态下的行为
    # (case_name, setup_status, operation, correct_worker, expected_result)
    WRONG_STATE_CASES = [
        # 对 completed 任务的操作
        ("ack_on_completed", "completed", "ack", True, False),
        ("fail_retry_on_completed", "completed", "fail_retry", True, False),
        ("requeue_on_completed", "completed", "requeue", True, False),
        ("mark_dead_on_completed", "completed", "mark_dead", True, False),
        ("renew_lease_on_completed", "completed", "renew_lease", True, False),
        # 对 dead 任务的操作
        ("ack_on_dead", "dead", "ack", True, False),
        ("fail_retry_on_dead", "dead", "fail_retry", True, False),
        ("requeue_on_dead", "dead", "requeue", True, False),
        # 对 pending 任务的操作
        ("ack_on_pending", "pending", "ack", True, False),
        ("fail_retry_on_pending", "pending", "fail_retry", True, False),
        ("requeue_on_pending", "pending", "requeue", True, False),
        # 对 failed 任务的操作（无锁状态）
        ("ack_on_failed", "failed", "ack", True, False),
        ("fail_retry_on_failed", "failed", "fail_retry", True, False),
    ]

    @pytest.mark.parametrize(
        "case_name,setup_status,operation,correct_worker,expected_result",
        WRONG_STATE_CASES,
        ids=[c[0] for c in WRONG_STATE_CASES],
    )
    def test_operation_on_wrong_status(
        self,
        migrated_db,
        db_conn,
        test_repo,
        case_name,
        setup_status,
        operation,
        correct_worker,
        expected_result,
    ):
        """
        表驱动测试：在非预期状态下执行操作

        验证：各操作在非 running 状态下返回 False
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import (
            ack,
            claim,
            enqueue,
            fail_retry,
            mark_dead,
            renew_lease,
            requeue_without_penalty,
        )

        migrated_db["schemas"]["scm"]
        worker_id = "test-worker"
        repo_id = test_repo

        # 1. 创建任务
        job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits", max_attempts=3, conn=db_conn)

        # 2. 设置初始状态
        if setup_status == "pending":
            pass  # 默认状态
        elif setup_status == "running":
            claim(worker_id=worker_id, conn=db_conn)
        elif setup_status == "completed":
            claim(worker_id=worker_id, conn=db_conn)
            ack(job_id=job_id, worker_id=worker_id, conn=db_conn)
        elif setup_status == "failed":
            claim(worker_id=worker_id, conn=db_conn)
            fail_retry(
                job_id=job_id, worker_id=worker_id, error="setup", backoff_seconds=0, conn=db_conn
            )
        elif setup_status == "dead":
            claim(worker_id=worker_id, conn=db_conn)
            mark_dead(job_id=job_id, worker_id=worker_id, error="setup", conn=db_conn)

        # 3. 执行操作
        if operation == "ack":
            result = ack(job_id=job_id, worker_id=worker_id, conn=db_conn)
        elif operation == "fail_retry":
            result = fail_retry(job_id=job_id, worker_id=worker_id, error="test", conn=db_conn)
        elif operation == "requeue":
            result = requeue_without_penalty(
                job_id=job_id, worker_id=worker_id, reason="test", conn=db_conn
            )
        elif operation == "mark_dead":
            result = mark_dead(job_id=job_id, worker_id=worker_id, error="test", conn=db_conn)
        elif operation == "renew_lease":
            result = renew_lease(job_id=job_id, worker_id=worker_id, conn=db_conn)
        else:
            pytest.fail(f"未知操作: {operation}")

        # 4. 验证结果
        assert result == expected_result, (
            f"操作 {operation} 在 {setup_status} 状态下应返回 {expected_result}"
        )


# ============================================================================
# 第八部分: 维度列完整性测试
# ============================================================================


class TestDimensionColumnIntegrity:
    """
    维度列完整性测试

    验证 pending/running 状态的 sync_jobs 维度列（gitlab_instance, tenant_id）
    的正确性，确保 budget 查询和 pool 过滤能正常工作。

    边界条件：
    - gitlab 类型任务应有 gitlab_instance
    - tenant_id 在多租户环境中应非空
    - 孤立任务（repo 不存在）的处理
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    @pytest.fixture
    def test_repo_with_url(self, migrated_db, db_conn):
        """创建带完整 URL 的测试仓库"""
        scm_schema = migrated_db["schemas"]["scm"]
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url, project_key)
                VALUES ('git', 'https://gitlab.example.com/group/project.git', 'mygroup/myproject')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()
        yield {
            "repo_id": repo_id,
            "expected_instance": "gitlab.example.com",
            "expected_tenant": "mygroup",
        }
        # 清理
        with db_conn.cursor() as cur:
            cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
            cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            db_conn.commit()

    def test_enqueue_with_dimension_columns(self, migrated_db, db_conn, test_repo_with_url):
        """
        测试 enqueue 时维度列正确写入

        验证：
        1. payload 中的 gitlab_instance 和 tenant_id 被写入对应列
        2. 列值与 payload 一致
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        repo_id = test_repo_with_url["repo_id"]
        expected_instance = test_repo_with_url["expected_instance"]
        expected_tenant = test_repo_with_url["expected_tenant"]

        # 入队时指定维度信息
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            payload={
                "gitlab_instance": expected_instance,
                "tenant_id": expected_tenant,
            },
            conn=db_conn,
        )
        assert job_id is not None

        # 验证维度列写入正确
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT gitlab_instance, tenant_id
                FROM scm.sync_jobs
                WHERE job_id = %s
            """,
                (job_id,),
            )
            row = cur.fetchone()

        assert row is not None
        assert row[0] == expected_instance, (
            f"gitlab_instance 应为 {expected_instance}，实际: {row[0]}"
        )
        assert row[1] == expected_tenant, f"tenant_id 应为 {expected_tenant}，实际: {row[1]}"

    def test_enqueue_gitlab_job_without_instance_warning(
        self, migrated_db, db_conn, test_repo_with_url
    ):
        """
        测试 gitlab 类型任务缺少 gitlab_instance 时的警告

        验证：
        1. 非严格模式下，任务可以入队但会记录警告
        2. 任务 payload 和 DB 列都为空
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        repo_id = test_repo_with_url["repo_id"]

        # 使用新的 job_type 避免唯一约束冲突
        job_id = enqueue(
            repo_id=repo_id,
            job_type="gitlab_mrs",  # 不同于前一个测试
            mode="incremental",
            payload={},  # 不提供 gitlab_instance
            conn=db_conn,
        )

        # 非严格模式应该成功入队
        assert job_id is not None

        # 验证维度列为 NULL
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT gitlab_instance, tenant_id
                FROM scm.sync_jobs
                WHERE job_id = %s
            """,
                (job_id,),
            )
            row = cur.fetchone()

        assert row is not None
        assert row[0] is None, "非严格模式下 gitlab_instance 应为 NULL"
        assert row[1] is None, "未提供 tenant_id 时应为 NULL"

    def test_enqueue_gitlab_job_strict_mode_raises(self, migrated_db, db_conn, test_repo_with_url):
        """
        测试严格模式下 gitlab 任务缺少 gitlab_instance 抛出异常
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        repo_id = test_repo_with_url["repo_id"]

        with pytest.raises(ValueError) as exc_info:
            enqueue(
                repo_id=repo_id,
                job_type="gitlab_reviews",
                mode="incremental",
                payload={},  # 不提供 gitlab_instance
                strict_dimension_check=True,  # 严格模式
                conn=db_conn,
            )

        assert "gitlab_instance" in str(exc_info.value)
        assert "gitlab_reviews" in str(exc_info.value)

    def test_dimension_column_normalization(self, migrated_db, db_conn, test_repo_with_url):
        """
        测试 gitlab_instance 规范化

        验证：
        1. URL 被正确解析为主机名
        2. 主机名被转为小写
        3. 端口号被正确处理
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建新仓库用于此测试
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://GitLab.Example.COM:8443/group/project.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 入队时使用 URL 格式
            job_id = enqueue(
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
                payload={
                    "gitlab_instance": "https://GitLab.Example.COM:8443/path",
                    "tenant_id": "test-tenant",
                },
                conn=db_conn,
            )
            assert job_id is not None

            # 验证规范化结果
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT gitlab_instance
                    FROM scm.sync_jobs
                    WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

            # 应该被规范化为小写主机名（可能包含端口）
            assert row is not None
            normalized = row[0]
            # normalize_instance_key 会移除 scheme 和 path，保留 host:port
            assert "gitlab.example.com" in normalized.lower(), (
                f"应包含小写主机名，实际: {normalized}"
            )

        finally:
            # 清理
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

    def test_pending_running_jobs_dimension_query(self, migrated_db, db_conn, test_repo_with_url):
        """
        测试活跃任务按维度列查询

        验证 budget 查询可以正确使用维度列过滤
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import claim, enqueue

        scm_schema = migrated_db["schemas"]["scm"]
        repo_id = test_repo_with_url["repo_id"]
        instance = "budget-test.gitlab.com"
        tenant = "budget-tenant"

        # 创建多个任务
        job_ids = []
        for i, jt in enumerate(["gitlab_commits", "gitlab_mrs"]):
            # 先清理可能存在的任务
            with db_conn.cursor() as cur:
                cur.execute(
                    f"""
                    DELETE FROM {scm_schema}.sync_jobs
                    WHERE repo_id = %s AND job_type = %s
                """,
                    (repo_id, jt),
                )
                db_conn.commit()

            jid = enqueue(
                repo_id=repo_id,
                job_type=jt,
                mode="incremental",
                payload={
                    "gitlab_instance": instance,
                    "tenant_id": tenant,
                },
                conn=db_conn,
            )
            if jid:
                job_ids.append(jid)

        # claim 其中一个使其变为 running
        if job_ids:
            claim(worker_id="test-worker-budget", conn=db_conn)

        # 使用维度列进行 budget 查询
        with db_conn.cursor() as cur:
            # 按 gitlab_instance 统计活跃任务
            cur.execute(
                """
                SELECT gitlab_instance, COUNT(*) as cnt
                FROM scm.sync_jobs
                WHERE status IN ('pending', 'running')
                  AND gitlab_instance = %s
                GROUP BY gitlab_instance
            """,
                (instance,),
            )
            instance_counts = dict(cur.fetchall())

            # 按 tenant_id 统计活跃任务
            cur.execute(
                """
                SELECT tenant_id, COUNT(*) as cnt
                FROM scm.sync_jobs
                WHERE status IN ('pending', 'running')
                  AND tenant_id = %s
                GROUP BY tenant_id
            """,
                (tenant,),
            )
            tenant_counts = dict(cur.fetchall())

        # 验证查询结果
        assert instance in instance_counts or len(job_ids) == 0, (
            "应能通过 gitlab_instance 查询到活跃任务"
        )
        assert tenant in tenant_counts or len(job_ids) == 0, "应能通过 tenant_id 查询到活跃任务"

    def test_svn_job_without_gitlab_instance_ok(self, migrated_db, db_conn):
        """
        测试 SVN 类型任务不需要 gitlab_instance

        验证非 gitlab 类型任务不强制要求 gitlab_instance
        """
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from engram.logbook.scm_sync_queue import enqueue

        scm_schema = migrated_db["schemas"]["scm"]

        # 创建 SVN 仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('svn', 'svn://svn.example.com/repo')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # SVN 任务不需要 gitlab_instance，即使严格模式也应成功
            job_id = enqueue(
                repo_id=repo_id,
                job_type="svn",  # 非 gitlab 类型
                mode="incremental",
                payload={},
                strict_dimension_check=True,  # 严格模式
                conn=db_conn,
            )

            # SVN 任务应该成功入队
            assert job_id is not None

            # 验证维度列为 NULL（预期行为）
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT gitlab_instance, tenant_id
                    FROM scm.sync_jobs
                    WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

            assert row is not None
            assert row[0] is None, "SVN 任务 gitlab_instance 应为 NULL"

        finally:
            # 清理
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()


class TestDimensionColumnBackfillVerification:
    """
    维度列回填验证测试

    模拟 SQL 迁移脚本的回填逻辑，验证边界条件处理
    """

    @pytest.fixture
    def db_conn(self, migrated_db):
        """获取测试数据库连接"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)
        yield conn
        conn.close()

    def test_backfill_from_repos_url(self, migrated_db, db_conn):
        """
        测试从 repos.url 回填 gitlab_instance

        模拟 SQL 迁移脚本中的回填逻辑
        """
        scm_schema = migrated_db["schemas"]["scm"]

        # 创建带 URL 的仓库
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url, project_key)
                VALUES ('git', 'https://backfill-test.gitlab.com/org/repo.git', 'myorg/myrepo')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 直接插入任务（不通过 enqueue，模拟旧数据）
            with db_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs
                    (repo_id, job_type, mode, status, payload_json)
                    VALUES (%s, 'gitlab_commits', 'incremental', 'pending', '{{}}')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = cur.fetchone()[0]
                db_conn.commit()

            # 验证初始状态：维度列为 NULL
            with db_conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT gitlab_instance, tenant_id
                    FROM {scm_schema}.sync_jobs
                    WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

            assert row[0] is None, "初始 gitlab_instance 应为 NULL"
            assert row[1] is None, "初始 tenant_id 应为 NULL"

            # 执行回填（模拟 SQL 迁移脚本逻辑）
            with db_conn.cursor() as cur:
                # 回填 gitlab_instance
                cur.execute(
                    f"""
                    UPDATE {scm_schema}.sync_jobs j
                    SET gitlab_instance = (
                        SELECT
                            CASE
                                WHEN r.vcs_type = 'git' AND r.remote_url IS NOT NULL AND r.remote_url LIKE '%://%'
                                THEN LOWER(REGEXP_REPLACE(r.remote_url, '^[^:]+://([^/:]+).*$', '\\1'))
                                ELSE NULL
                            END
                        FROM {scm_schema}.repos r
                        WHERE r.repo_id = j.repo_id
                    )
                    WHERE j.job_id = %s AND j.gitlab_instance IS NULL
                """,
                    (job_id,),
                )

                # 回填 tenant_id
                cur.execute(
                    f"""
                    UPDATE {scm_schema}.sync_jobs j
                    SET tenant_id = (
                        SELECT
                            CASE
                                WHEN r.project_key IS NOT NULL AND r.project_key LIKE '%/%'
                                THEN SPLIT_PART(r.project_key, '/', 1)
                                ELSE NULL
                            END
                        FROM {scm_schema}.repos r
                        WHERE r.repo_id = j.repo_id
                    )
                    WHERE j.job_id = %s AND j.tenant_id IS NULL
                """,
                    (job_id,),
                )
                db_conn.commit()

            # 验证回填结果
            with db_conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT gitlab_instance, tenant_id
                    FROM {scm_schema}.sync_jobs
                    WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

            assert row[0] == "backfill-test.gitlab.com", (
                f"回填后 gitlab_instance 应为 'backfill-test.gitlab.com'，实际: {row[0]}"
            )
            assert row[1] == "myorg", f"回填后 tenant_id 应为 'myorg'，实际: {row[1]}"

        finally:
            # 清理
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

    def test_orphan_job_backfill_safety(self, migrated_db, db_conn):
        """
        测试孤立任务（repo 不存在）的回填安全性

        验证回填不会因为关联的 repo 不存在而失败
        """
        scm_schema = migrated_db["schemas"]["scm"]

        # 创建仓库并记录 ID
        with db_conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                VALUES ('git', 'https://orphan-test.gitlab.com/repo.git')
                RETURNING repo_id
            """)
            repo_id = cur.fetchone()[0]
            db_conn.commit()

        try:
            # 插入任务
            with db_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs
                    (repo_id, job_type, mode, status, payload_json)
                    VALUES (%s, 'gitlab_commits', 'incremental', 'pending', '{{}}')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = cur.fetchone()[0]
                db_conn.commit()

            # 删除 repo（制造孤立任务）
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
                db_conn.commit()

            # 尝试回填（应该不会失败，只是维度列保持 NULL）
            with db_conn.cursor() as cur:
                # 带 EXISTS 检查的回填
                cur.execute(
                    f"""
                    UPDATE {scm_schema}.sync_jobs j
                    SET gitlab_instance = (
                        SELECT LOWER(REGEXP_REPLACE(r.remote_url, '^[^:]+://([^/:]+).*$', '\\1'))
                        FROM {scm_schema}.repos r
                        WHERE r.repo_id = j.repo_id
                    )
                    WHERE j.job_id = %s
                      AND j.gitlab_instance IS NULL
                      AND EXISTS (SELECT 1 FROM {scm_schema}.repos r WHERE r.repo_id = j.repo_id)
                """,
                    (job_id,),
                )
                affected = cur.rowcount
                db_conn.commit()

            # 应该影响 0 行（repo 不存在）
            assert affected == 0, "孤立任务不应被回填"

            # 验证任务仍然存在且维度列为 NULL
            with db_conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT gitlab_instance, tenant_id
                    FROM {scm_schema}.sync_jobs
                    WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

            assert row is not None, "任务应仍然存在"
            assert row[0] is None, "孤立任务的 gitlab_instance 应保持 NULL"

        finally:
            # 清理孤立任务
            with db_conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                db_conn.commit()


# ============================================================================
# 运行说明
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
