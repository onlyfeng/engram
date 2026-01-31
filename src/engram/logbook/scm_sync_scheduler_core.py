# -*- coding: utf-8 -*-
"""
scm_sync_scheduler_core - SCM 同步调度器核心实现

功能:
- 提供 build_jobs_to_insert(snapshot) 纯函数，将调度快照转换为可入队的 job 列表
- 处理 logical->physical job_type 转换
- 注入 bucket 暂停信息、probe 标记、降级建议
- 支持 tenant/instance 级别熔断跳过
- 提供主循环 run_scheduler_tick()

设计原则:
1. build_jobs_to_insert 是纯函数，不访问数据库或外部状态
2. 所有决策信息通过 BuildJobsSnapshot 传入
3. 输出 BuildJobsResult 包含生成的 jobs 和跳过原因统计
4. run_scheduler_tick() 协调 DB 查询、策略计算、入队操作
5. CLI 入口在根目录 scm_sync_scheduler.py 和 scripts/scm_sync_scheduler.py

使用示例:
    from engram.logbook.scm_sync_scheduler_core import (
        BuildJobsSnapshot,
        BuildJobsResult,
        build_jobs_to_insert,
        run_scheduler_tick,
    )

    snapshot = BuildJobsSnapshot(
        candidates_to_enqueue=[...],
        states=[...],
        circuit_decision=CircuitBreakerDecision(...),
    )

    result = build_jobs_to_insert(snapshot)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from engram.logbook.scm_sync_job_types import (
    logical_to_physical,
)
from engram.logbook.scm_sync_policy import (
    BudgetSnapshot,
    CircuitBreakerConfig,
    CircuitBreakerController,
    CircuitBreakerDecision,
    InstanceBucketStatus,
    PauseSnapshot,
    RepoSyncState,
    SchedulerConfig,
    SyncJobCandidate,
    select_jobs_to_enqueue,
)

__all__ = [
    # 数据类
    "SkippedJob",
    "BuildJobsSnapshot",
    "BuildJobsResult",
    "SchedulerTickResult",
    # 函数
    "build_jobs_to_insert",
    "run_scheduler_tick",
]


# ============ 数据结构定义 ============


@dataclass
class SkippedJob:
    """
    被跳过的任务记录

    记录因熔断或其他原因被跳过的任务信息。
    """

    repo_id: int
    job_type: str
    reason: str
    tenant: Optional[str] = None
    instance: Optional[str] = None
    wait_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "job_type": self.job_type,
            "reason": self.reason,
            "tenant": self.tenant,
            "instance": self.instance,
            "wait_seconds": self.wait_seconds,
        }


@dataclass
class BuildJobsSnapshot:
    """
    构建 jobs 所需的调度快照

    包含 scheduler 扫描阶段收集的所有决策信息，
    作为 build_jobs_to_insert() 纯函数的输入。

    Attributes:
        candidates_to_enqueue: 待入队的任务候选列表
        states: 仓库同步状态列表（用于查找 repo 元数据）
        circuit_decision: 全局熔断决策
        instance_decisions: 实例级熔断决策 {instance_key: CircuitBreakerDecision}
        tenant_decisions: 租户级熔断决策 {tenant_id: CircuitBreakerDecision}
        budget_snapshot: 预算占用快照
        scheduled_at: 调度时间戳（ISO8601 格式）
    """

    candidates_to_enqueue: List[SyncJobCandidate]
    states: List[RepoSyncState]
    circuit_decision: CircuitBreakerDecision
    instance_decisions: Dict[str, CircuitBreakerDecision] = field(default_factory=dict)
    tenant_decisions: Dict[str, CircuitBreakerDecision] = field(default_factory=dict)
    budget_snapshot: BudgetSnapshot = field(default_factory=BudgetSnapshot)
    scheduled_at: str = ""


@dataclass
class BuildJobsResult:
    """
    build_jobs_to_insert() 的返回结果

    Attributes:
        jobs: 生成的 job 字典列表，可直接用于入队
        skipped_jobs: 被跳过的任务列表
        tenant_paused_count: 因租户熔断跳过的数量
        instance_paused_count: 因实例熔断跳过的数量
    """

    jobs: List[Dict[str, Any]] = field(default_factory=list)
    skipped_jobs: List[Dict[str, Any]] = field(default_factory=list)
    tenant_paused_count: int = 0
    instance_paused_count: int = 0


@dataclass
class SchedulerTickResult:
    """
    调度器单次运行结果

    Attributes:
        scheduled_at: 调度时间戳
        repos_scanned: 扫描的仓库数
        candidates_selected: 选中的候选任务数
        jobs_enqueued: 成功入队的任务数
        jobs_skipped: 跳过的任务数
        tenant_paused_count: 租户熔断跳过数
        instance_paused_count: 实例熔断跳过数
        circuit_state: 全局熔断状态
        budget_snapshot: 预算快照
        errors: 错误列表
    """

    scheduled_at: str
    repos_scanned: int = 0
    candidates_selected: int = 0
    jobs_enqueued: int = 0
    jobs_skipped: int = 0
    tenant_paused_count: int = 0
    instance_paused_count: int = 0
    circuit_state: str = "closed"
    budget_snapshot: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)
    enqueued_jobs: List[Dict[str, Any]] = field(default_factory=list)
    skipped_jobs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scheduled_at": self.scheduled_at,
            "repos_scanned": self.repos_scanned,
            "candidates_selected": self.candidates_selected,
            "jobs_enqueued": self.jobs_enqueued,
            "jobs_skipped": self.jobs_skipped,
            "tenant_paused_count": self.tenant_paused_count,
            "instance_paused_count": self.instance_paused_count,
            "circuit_state": self.circuit_state,
            "budget_snapshot": self.budget_snapshot,
            "errors": self.errors,
            "enqueued_jobs": self.enqueued_jobs,
            "skipped_jobs": self.skipped_jobs,
        }


# ============ 核心纯函数 ============


def build_jobs_to_insert(snapshot: BuildJobsSnapshot) -> BuildJobsResult:
    """
    将调度快照转换为可入队的 job 列表（纯函数）

    处理逻辑：
    1. 遍历 candidates_to_enqueue
    2. 检查 tenant/instance 级别熔断，决定是否跳过
    3. 执行 logical->physical job_type 转换
    4. 构建 job 字典，注入 bucket 信息、probe 标记、降级建议
    5. 收集跳过原因统计

    Args:
        snapshot: 调度快照，包含所有决策信息

    Returns:
        BuildJobsResult，包含生成的 jobs 和跳过统计
    """
    result = BuildJobsResult()

    # 构建 repo_id -> state 的映射
    state_map: Dict[int, RepoSyncState] = {state.repo_id: state for state in snapshot.states}

    # 全局熔断决策
    global_decision = snapshot.circuit_decision

    for candidate in snapshot.candidates_to_enqueue:
        repo_id = candidate.repo_id
        logical_job_type = candidate.job_type

        # 查找对应的 state
        state = state_map.get(repo_id)
        if state is None:
            # 没有对应的 state，跳过
            result.skipped_jobs.append(
                {
                    "repo_id": repo_id,
                    "job_type": logical_job_type,
                    "reason": "state_not_found",
                }
            )
            continue

        # 获取 repo 元数据
        repo_type = state.repo_type
        gitlab_instance = state.gitlab_instance
        tenant_id = state.tenant_id

        # ============ 检查租户级熔断 ============
        if tenant_id and tenant_id in snapshot.tenant_decisions:
            tenant_decision = snapshot.tenant_decisions[tenant_id]
            if not tenant_decision.allow_sync:
                # 租户熔断 OPEN，跳过
                result.skipped_jobs.append(
                    {
                        "repo_id": repo_id,
                        "job_type": logical_job_type,
                        "reason": "tenant_circuit_open",
                        "tenant": tenant_id,
                        "wait_seconds": tenant_decision.wait_seconds,
                    }
                )
                result.tenant_paused_count += 1
                continue

        # ============ 检查实例级熔断 ============
        if gitlab_instance and gitlab_instance in snapshot.instance_decisions:
            instance_decision = snapshot.instance_decisions[gitlab_instance]
            if not instance_decision.allow_sync:
                # 实例熔断 OPEN，跳过
                result.skipped_jobs.append(
                    {
                        "repo_id": repo_id,
                        "job_type": logical_job_type,
                        "reason": "instance_circuit_open",
                        "instance": gitlab_instance,
                        "wait_seconds": instance_decision.wait_seconds,
                    }
                )
                result.instance_paused_count += 1
                continue

        # ============ logical -> physical job_type 转换 ============
        try:
            physical_job_type = logical_to_physical(logical_job_type, repo_type)
        except ValueError:
            # 无效的 job_type 组合，跳过
            result.skipped_jobs.append(
                {
                    "repo_id": repo_id,
                    "job_type": logical_job_type,
                    "reason": "invalid_job_type_combination",
                }
            )
            continue

        # ============ 确定 mode 和构建 payload ============
        mode = _determine_mode(global_decision)
        payload = _build_payload(
            candidate=candidate,
            state=state,
            global_decision=global_decision,
            scheduled_at=snapshot.scheduled_at,
            budget_snapshot=snapshot.budget_snapshot,
        )

        # ============ 构建 job 字典 ============
        job = {
            "repo_id": repo_id,
            "job_type": physical_job_type,
            "priority": candidate.priority,
            "mode": mode,
            "payload_json": payload,
        }

        result.jobs.append(job)

    return result


def _determine_mode(global_decision: CircuitBreakerDecision) -> str:
    """
    根据全局熔断决策确定 job 的 mode

    Args:
        global_decision: 全局熔断决策

    Returns:
        mode 字符串: "incremental" | "probe" | "backfill"
    """
    # HALF_OPEN 探测模式
    if global_decision.is_probe_mode:
        return "probe"

    # 仅 backfill 模式
    if global_decision.is_backfill_only:
        return "backfill"

    # 正常增量模式
    return "incremental"


def _build_payload(
    candidate: SyncJobCandidate,
    state: RepoSyncState,
    global_decision: CircuitBreakerDecision,
    scheduled_at: str,
    budget_snapshot: BudgetSnapshot,
) -> Dict[str, Any]:
    """
    构建 job 的 payload 字典

    注入以下信息：
    - 调度元数据（reason, scheduled_at, gitlab_instance, tenant_id）
    - bucket 暂停信息
    - 探测模式标记和降级建议
    - 预算快照（用于排障）

    Args:
        candidate: 任务候选
        state: 仓库状态
        global_decision: 全局熔断决策
        scheduled_at: 调度时间戳
        budget_snapshot: 预算快照

    Returns:
        payload 字典
    """
    payload: Dict[str, Any] = {}

    # ============ 基础调度元数据 ============
    payload["reason"] = candidate.reason
    payload["scheduled_at"] = scheduled_at
    payload["gitlab_instance"] = state.gitlab_instance
    payload["tenant_id"] = state.tenant_id

    # ============ Bucket 暂停信息 ============
    payload["bucket_paused"] = candidate.bucket_paused
    payload["bucket_pause_remaining_seconds"] = candidate.bucket_pause_remaining_seconds
    payload["bucket_penalty_reason"] = candidate.bucket_penalty_reason
    payload["bucket_penalty_value"] = candidate.bucket_penalty_value

    # ============ 游标和统计信息 ============
    payload["cursor_age_seconds"] = candidate.cursor_age_seconds
    payload["failure_rate"] = candidate.failure_rate
    payload["rate_limit_rate"] = candidate.rate_limit_rate

    # ============ 探测模式标记和降级建议 ============
    if global_decision.is_probe_mode:
        payload["is_probe_mode"] = True
        payload["probe_budget"] = global_decision.probe_budget
        payload["circuit_state"] = global_decision.current_state

        # 注入降级参数建议
        payload["suggested_batch_size"] = global_decision.suggested_batch_size
        payload["suggested_forward_window_seconds"] = (
            global_decision.suggested_forward_window_seconds
        )
        payload["suggested_diff_mode"] = global_decision.suggested_diff_mode
    else:
        payload["is_probe_mode"] = False
        payload["circuit_state"] = global_decision.current_state

    # ============ 预算快照（用于排障） ============
    payload["budget_snapshot"] = budget_snapshot.to_dict()

    return payload


# ============ 辅助函数 ============


def _build_repo_sync_states(
    conn,
    repos: List[Dict[str, Any]],
    queued_pairs: Set[Tuple[int, str]],
    db_api=None,
) -> List[RepoSyncState]:
    """
    从仓库列表构建 RepoSyncState 列表

    Args:
        conn: 数据库连接
        repos: 仓库列表
        queued_pairs: 已入队的 (repo_id, job_type) 集合
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        RepoSyncState 列表
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api

    states = []
    for repo in repos:
        repo_id = repo["repo_id"]
        repo_type = repo["repo_type"]

        # 获取同步统计
        stats = db_api.get_repo_sync_stats(conn, repo_id)

        # 获取游标信息（用于计算游标年龄）
        # 使用 commits 作为主游标
        cursor_info = db_api.get_cursor_value(conn, repo_id, "commits")
        cursor_updated_at = cursor_info["updated_at"] if cursor_info else None

        # 检查是否有任务在队列中（repo 级别的标志，向后兼容）
        is_queued = any((repo_id, jt) in queued_pairs for jt in ["commits", "mrs", "reviews"])

        # 提取 gitlab_instance（从 URL 中解析）
        gitlab_instance = None
        url = repo.get("url", "")
        if url and repo_type == "git":
            try:
                from urllib.parse import urlparse

                parsed = urlparse(url)
                if parsed.netloc:
                    gitlab_instance = parsed.netloc.lower()
            except Exception:
                pass

        state = RepoSyncState(
            repo_id=repo_id,
            repo_type=repo_type,
            gitlab_instance=gitlab_instance,
            tenant_id=repo.get("tenant_id"),
            cursor_updated_at=cursor_updated_at,
            recent_run_count=stats.get("total_runs", 0),
            recent_failed_count=stats.get("failed_count", 0),
            recent_429_hits=stats.get("total_429_hits", 0),
            recent_total_requests=stats.get("total_requests", 0),
            last_run_status=stats.get("last_run_status"),
            last_run_at=stats.get("last_run_at"),
            is_queued=is_queued,
        )
        states.append(state)

    return states


def _build_budget_snapshot_from_db(conn, db_api=None) -> BudgetSnapshot:
    """从数据库构建 BudgetSnapshot"""
    if db_api is None:
        from engram.logbook import scm_db as db_api

    budget_dict = db_api.get_budget_snapshot(conn)
    return BudgetSnapshot(
        global_running=budget_dict.get("global_running", 0),
        global_pending=budget_dict.get("global_pending", 0),
        global_active=budget_dict.get("global_active", 0),
        by_instance=budget_dict.get("by_instance", {}),
        by_tenant=budget_dict.get("by_tenant", {}),
    )


def _load_bucket_statuses(
    conn,
    instances: List[str],
    db_api=None,
) -> Dict[str, InstanceBucketStatus]:
    """加载实例级 bucket 状态"""
    if db_api is None:
        from engram.logbook import scm_db as db_api

    statuses = {}
    for instance in instances:
        status_dict = db_api.get_rate_limit_bucket_status(conn, instance)
        if status_dict:
            statuses[instance] = InstanceBucketStatus.from_db_status(status_dict)
    return statuses


def _load_circuit_breaker_decision(
    conn,
    config: CircuitBreakerConfig,
    key: str = "default:global",
    db_api=None,
) -> CircuitBreakerDecision:
    """加载熔断决策"""
    if db_api is None:
        from engram.logbook import scm_db as db_api

    # 加载熔断器状态
    controller = CircuitBreakerController(config=config, key=key)

    # 尝试从 DB 加载持久化状态
    state_dict = db_api.load_circuit_breaker_state(conn, key)
    if state_dict:
        controller.load_state_dict(state_dict)

    # 获取健康统计
    health_stats = db_api.get_sync_runs_health_stats(
        conn,
        window_minutes=config.window_minutes or 30,
        window_count=config.window_count,
    )

    # 检查并返回决策
    decision = controller.check(health_stats)

    # 保存更新后的状态
    db_api.save_circuit_breaker_state(conn, key, controller.get_state_dict())

    return decision


# ============ 调度器主循环 ============


def run_scheduler_tick(
    conn,
    *,
    scheduler_config: Optional[SchedulerConfig] = None,
    cb_config: Optional[CircuitBreakerConfig] = None,
    dry_run: bool = False,
    now: Optional[float] = None,
    logger=None,
    db_api=None,
) -> SchedulerTickResult:
    """
    执行一次调度 tick

    流程：
    1. 检查 SCM 同步是否启用
    2. 从 DB 加载仓库列表
    3. 构造 RepoSyncState 列表与 BudgetSnapshot
    4. 构造 queued_pairs（已入队/运行的任务）
    5. 加载 bucket 状态与 circuit breaker decision
    6. 调用 select_jobs_to_enqueue + build_jobs_to_insert
    7. 入队任务（除非 dry_run）
    8. 返回调度结果摘要

    Args:
        conn: 数据库连接
        scheduler_config: 调度器配置，None 时使用默认配置
        cb_config: 熔断器配置，None 时使用默认配置
        dry_run: 干运行模式，不实际入队
        now: 当前时间戳，None 时使用 time.time()
        logger: 日志记录器
        db_api: 数据库 API 模块（用于测试注入）

    Returns:
        SchedulerTickResult 调度结果
    """
    if db_api is None:
        from engram.logbook import scm_db as db_api
    from engram.logbook.scm_sync_queue import enqueue

    if now is None:
        now = time.time()

    scheduled_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    result = SchedulerTickResult(scheduled_at=scheduled_at)

    # 使用默认配置
    scheduler_config = scheduler_config or SchedulerConfig()
    cb_config = cb_config or CircuitBreakerConfig()

    try:
        # 1. 从 DB 加载仓库列表
        repos = db_api.list_repos_for_scheduling(conn)
        result.repos_scanned = len(repos)

        if not repos:
            if logger:
                logger.info("无仓库需要调度")
            return result

        # 2. 获取当前活跃任务对（用于去重）
        # 注意：queued_pairs 仅表示 DB 中 active jobs
        queued_pairs_list = db_api.get_active_job_pairs(conn)
        queued_pairs: Set[Tuple[int, str]] = set(queued_pairs_list)

        # 3. 获取当前暂停的任务对（从 kv 暂停记录）
        # 注意：paused_pairs 仅表示 kv 中的暂停记录，与 queued_pairs 分离
        pause_snapshot_dict = db_api.get_pause_snapshot(conn)
        pause_snapshot = PauseSnapshot(
            paused_pairs=pause_snapshot_dict["paused_pairs"],
            pause_count=pause_snapshot_dict["pause_count"],
            by_reason_code=pause_snapshot_dict["by_reason_code"],
            snapshot_at=pause_snapshot_dict["snapshot_at"],
        )

        # 4. 构建 BudgetSnapshot
        budget_snapshot = _build_budget_snapshot_from_db(conn, db_api=db_api)
        result.budget_snapshot = budget_snapshot.to_dict()

        # 5. 构建 RepoSyncState 列表
        states = _build_repo_sync_states(conn, repos, queued_pairs, db_api=db_api)

        # 6. 收集所有 gitlab_instance 用于加载 bucket 状态
        instances = list(set(s.gitlab_instance for s in states if s.gitlab_instance))
        bucket_statuses = _load_bucket_statuses(conn, instances, db_api=db_api)

        # 7. 加载熔断决策
        circuit_decision = _load_circuit_breaker_decision(conn, cb_config, db_api=db_api)
        result.circuit_state = circuit_decision.current_state

        # 8. 确定要调度的 job_types（按 repo_type 分组）
        # 对于每个 repo，使用其支持的 logical_job_types
        all_job_types = ["commits", "mrs", "reviews"]  # 全量 logical types

        # 9. 调用 select_jobs_to_enqueue
        # 传入分离的 queued_pairs 和 pause_snapshot，函数内部会合并过滤
        candidates = select_jobs_to_enqueue(
            states=states,
            job_types=all_job_types,
            config=scheduler_config,
            now=now,
            queued_pairs=queued_pairs,
            budget_snapshot=budget_snapshot,
            bucket_statuses=bucket_statuses,
            pause_snapshot=pause_snapshot,
        )

        result.candidates_selected = len(candidates)

        if not candidates:
            if logger:
                logger.info("无候选任务需要入队")
            return result

        # 9. 构建 BuildJobsSnapshot 并调用 build_jobs_to_insert
        snapshot = BuildJobsSnapshot(
            candidates_to_enqueue=candidates,
            states=states,
            circuit_decision=circuit_decision,
            instance_decisions={},  # 可扩展支持实例级熔断
            tenant_decisions={},  # 可扩展支持租户级熔断
            budget_snapshot=budget_snapshot,
            scheduled_at=scheduled_at,
        )

        build_result = build_jobs_to_insert(snapshot)

        result.skipped_jobs = build_result.skipped_jobs
        result.jobs_skipped = len(build_result.skipped_jobs)
        result.tenant_paused_count = build_result.tenant_paused_count
        result.instance_paused_count = build_result.instance_paused_count

        # 10. 入队任务
        if dry_run:
            if logger:
                logger.info("干运行模式，跳过入队: %d 个任务", len(build_result.jobs))
            result.jobs_enqueued = 0
            result.enqueued_jobs = build_result.jobs
        else:
            enqueued_count = 0
            for job in build_result.jobs:
                try:
                    job_id = enqueue(
                        repo_id=job["repo_id"],
                        job_type=job["job_type"],
                        mode=job.get("mode", "incremental"),
                        priority=job.get("priority", 100),
                        payload=job.get("payload_json", {}),
                        conn=conn,
                    )
                    if job_id:
                        enqueued_count += 1
                        job["job_id"] = job_id
                        result.enqueued_jobs.append(job)
                        if logger:
                            logger.debug(
                                "入队成功: job_id=%s, repo_id=%d, job_type=%s",
                                job_id,
                                job["repo_id"],
                                job["job_type"],
                            )
                except Exception as e:
                    error_msg = f"入队失败 repo_id={job['repo_id']}: {e}"
                    if logger:
                        logger.warning(error_msg)
                    result.errors.append(error_msg)

            result.jobs_enqueued = enqueued_count
            conn.commit()

        if logger:
            logger.info(
                "调度完成: scanned=%d, candidates=%d, enqueued=%d, skipped=%d",
                result.repos_scanned,
                result.candidates_selected,
                result.jobs_enqueued,
                result.jobs_skipped,
            )

    except Exception as e:
        error_msg = f"调度异常: {e}"
        if logger:
            logger.error(error_msg, exc_info=True)
        result.errors.append(error_msg)

    return result
