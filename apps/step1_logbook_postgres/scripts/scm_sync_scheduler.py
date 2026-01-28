#!/usr/bin/env python3
"""
scm_sync_scheduler.py - SCM 同步调度器

周期性扫描 scm.repos、logbook.kv 游标、scm.sync_runs 最近运行状态，
计算 lag（cursor age、failure rate、429 命中率等），并 enqueue scm.sync_jobs。

特性:
- 支持配置项：全局并发、每 GitLab 实例并发、每租户并发
- 支持 backfill repair_window、最大回填窗口
- 支持错误预算阈值、暂停时长配置
- 使用纯函数化策略，便于单元测试

配置示例:
    [scm.scheduler]
    global_concurrency = 10
    per_instance_concurrency = 3
    per_tenant_concurrency = 5
    backfill_repair_window_hours = 24
    max_backfill_window_hours = 168
    cursor_age_threshold_seconds = 3600
    error_budget_threshold = 0.3
    pause_duration_seconds = 300
    scan_interval_seconds = 60
    max_enqueue_per_scan = 100

使用:
    # 单次扫描并入队
    python scm_sync_scheduler.py scan

    # 持续循环调度
    python scm_sync_scheduler.py run --loop

    # 显示当前配置
    python scm_sync_scheduler.py config

    # 查看队列状态
    python scm_sync_scheduler.py status
"""

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from engram_step1.config import (
    Config,
    add_config_argument,
    get_config,
)
from engram_step1.cursor import (
    Cursor,
    get_cursor_type_for_job,
    load_cursor,
    get_cursor_updated_at_timestamp,
)
from engram_step1.db import get_connection
from engram_step1.scm_sync_policy import (
    SchedulerConfig,
    RepoSyncState,
    SyncJobCandidate,
    BudgetSnapshot,
    select_jobs_to_enqueue,
    calculate_cursor_age,
    calculate_failure_rate,
    calculate_rate_limit_rate,
    CircuitBreakerController,
    CircuitBreakerConfig,
    CircuitBreakerDecision,
    CircuitState,
    # Backfill 相关
    BackfillWindow,
    compute_time_backfill_window,
    compute_svn_backfill_window,
    should_generate_backfill,
    # Bucket 暂停相关
    InstanceBucketStatus,
    calculate_bucket_priority_penalty,
    should_skip_due_to_bucket_pause,
)

import db as scm_db
from db import (
    build_circuit_breaker_key,
    # Pause 相关函数
    set_repo_job_pause,
    get_paused_repo_job_pairs,
    batch_check_and_auto_unpause,
    clear_expired_pauses,
    # Rate Limit Bucket 相关函数
    list_rate_limit_buckets,
    get_rate_limit_status,
)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# === 常量 ===

# 导入 job_type 归一化模块
from engram_step1.scm_sync_job_types import (
    LogicalJobType,
    PhysicalJobType,
    logical_to_physical,
    get_physical_job_types_for_repo,
    get_logical_job_types_for_repo,
    GIT_PHYSICAL_JOB_TYPES,
    SVN_PHYSICAL_JOB_TYPES,
    GIT_LOGICAL_JOB_TYPES,
    SVN_LOGICAL_JOB_TYPES,
    get_job_type_priority,
)

# 支持的 logical job 类型（用于 policy 层，按默认优先级排序）
# 注意：scheduler 入队时会转换为 physical_job_type
DEFAULT_LOGICAL_JOB_TYPES = GIT_LOGICAL_JOB_TYPES  # ["commits", "mrs", "reviews"]

# SVN 仓库支持的 logical job 类型
SVN_LOGICAL_JOB_TYPES_LIST = SVN_LOGICAL_JOB_TYPES  # ["commits"]

# 兼容旧代码的常量（已废弃，使用 physical 类型替代）
# @deprecated 请使用 GIT_PHYSICAL_JOB_TYPES 或 get_physical_job_types_for_repo()
DEFAULT_JOB_TYPES = DEFAULT_LOGICAL_JOB_TYPES

# @deprecated 请使用 SVN_PHYSICAL_JOB_TYPES 或 get_physical_job_types_for_repo()
SVN_JOB_TYPES = SVN_LOGICAL_JOB_TYPES_LIST


@dataclass
class ScanResult:
    """扫描结果"""
    scanned_repos: int = 0
    candidates_found: int = 0
    jobs_enqueued: int = 0
    jobs_skipped: int = 0  # 因已存在而跳过
    paused_repos: int = 0  # 因错误预算暂停的仓库
    scan_duration_seconds: float = 0.0
    errors: List[str] = None
    
    def __post_init__(self):
        if self.errors is None:
            self.errors = []
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


class SyncScheduler:
    """
    SCM 同步调度器
    
    负责:
    1. 扫描仓库状态
    2. 计算调度优先级
    3. 入队同步任务
    4. 熔断保护（基于 sync_runs 健康统计）
    """
    
    def __init__(
        self,
        config: Optional[Config] = None,
        scheduler_config: Optional[SchedulerConfig] = None,
        circuit_breaker_config: Optional[CircuitBreakerConfig] = None,
    ):
        self._config = config or get_config()
        self._scheduler_config = scheduler_config or SchedulerConfig.from_config(self._config)
        self._circuit_breaker_config = circuit_breaker_config or CircuitBreakerConfig.from_config(self._config)
        self._shutdown_requested = False
        
        # 获取 project_key 用于构建熔断 key
        # 优先从配置读取，默认 'default'
        project_key = self._config.get("project.project_key", "default") or "default"
        
        # 构建规范化的熔断 key: <project_key>:global
        # 例如: default:global, myproject:global
        self._circuit_breaker_key = build_circuit_breaker_key(
            project_key=project_key,
            scope="global",
        )
        
        # 全局熔断控制器（使用规范化的 key）
        self._circuit_breaker = CircuitBreakerController(
            config=self._circuit_breaker_config,
            key=self._circuit_breaker_key,
        )
        
        # 实例级熔断控制器字典 {instance_name: CircuitBreakerController}
        self._instance_breakers: Dict[str, CircuitBreakerController] = {}
        
        # 租户级熔断控制器字典 {tenant_id: CircuitBreakerController}
        self._tenant_breakers: Dict[str, CircuitBreakerController] = {}
        
        # 保存 project_key 用于构建实例级熔断 key
        self._project_key = project_key
        
        logger.info(
            "调度器熔断控制器初始化: key=%s",
            self._circuit_breaker_key
        )
        
        # 尝试从持久化恢复熔断状态
        self._load_circuit_breaker_state()
        self._load_instance_breaker_states()
        
        # 注册信号处理
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
    
    def _load_circuit_breaker_state(self) -> None:
        """从持久化存储加载熔断状态"""
        try:
            conn = get_connection(config=self._config)
            try:
                state_dict = scm_db.load_circuit_breaker_state(conn, self._circuit_breaker._key)
                if state_dict:
                    self._circuit_breaker.load_state_dict(state_dict)
                    logger.info(
                        "已加载熔断状态: state=%s",
                        self._circuit_breaker.state.value
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.warning("加载熔断状态失败: %s", e)
    
    def _save_circuit_breaker_state(self, conn) -> None:
        """保存熔断状态到持久化存储"""
        try:
            state_dict = self._circuit_breaker.get_state_dict()
            scm_db.save_circuit_breaker_state(conn, self._circuit_breaker._key, state_dict)
        except Exception as e:
            logger.warning("保存熔断状态失败: %s", e)
    
    def _load_instance_breaker_states(self) -> None:
        """
        从持久化存储加载已知实例的熔断状态
        
        注意：实例级熔断控制器是惰性创建的，此方法主要用于恢复已持久化的状态
        """
        # 实例级熔断状态在首次使用时惰性加载，此处不主动加载
        # 避免启动时查询所有可能的实例
        pass
    
    def _get_instance_breaker(self, instance: str) -> CircuitBreakerController:
        """
        获取或创建实例级熔断控制器
        
        Args:
            instance: 实例标识（如 GitLab 的 host）
        
        Returns:
            该实例的熔断控制器
        """
        if instance not in self._instance_breakers:
            # 构建实例级熔断 key: <project_key>:instance:<instance_name>
            key = build_circuit_breaker_key(
                project_key=self._project_key,
                scope=f"instance:{instance}",
            )
            
            breaker = CircuitBreakerController(
                config=self._circuit_breaker_config,
                key=key,
            )
            
            # 尝试从持久化加载状态
            try:
                conn = get_connection(config=self._config)
                try:
                    state_dict = scm_db.load_circuit_breaker_state(conn, key)
                    if state_dict:
                        breaker.load_state_dict(state_dict)
                        logger.debug(
                            "已加载实例熔断状态: instance=%s, state=%s",
                            instance, breaker.state.value
                        )
                finally:
                    conn.close()
            except Exception as e:
                logger.warning("加载实例熔断状态失败: instance=%s, error=%s", instance, e)
            
            self._instance_breakers[instance] = breaker
        
        return self._instance_breakers[instance]
    
    def _get_tenant_breaker(self, tenant_id: str) -> CircuitBreakerController:
        """
        获取或创建租户级熔断控制器
        
        Args:
            tenant_id: 租户标识
        
        Returns:
            该租户的熔断控制器
        """
        if tenant_id not in self._tenant_breakers:
            # 构建租户级熔断 key: <project_key>:tenant:<tenant_id>
            key = build_circuit_breaker_key(
                project_key=self._project_key,
                scope=f"tenant:{tenant_id}",
            )
            
            breaker = CircuitBreakerController(
                config=self._circuit_breaker_config,
                key=key,
            )
            
            # 尝试从持久化加载状态
            try:
                conn = get_connection(config=self._config)
                try:
                    state_dict = scm_db.load_circuit_breaker_state(conn, key)
                    if state_dict:
                        breaker.load_state_dict(state_dict)
                        logger.debug(
                            "已加载租户熔断状态: tenant=%s, state=%s",
                            tenant_id, breaker.state.value
                        )
                finally:
                    conn.close()
            except Exception as e:
                logger.warning("加载租户熔断状态失败: tenant=%s, error=%s", tenant_id, e)
            
            self._tenant_breakers[tenant_id] = breaker
        
        return self._tenant_breakers[tenant_id]
    
    def _save_instance_breaker_states(self, conn) -> None:
        """保存所有实例级熔断状态到持久化存储"""
        for instance, breaker in self._instance_breakers.items():
            try:
                state_dict = breaker.get_state_dict()
                scm_db.save_circuit_breaker_state(conn, breaker._key, state_dict)
            except Exception as e:
                logger.warning("保存实例熔断状态失败: instance=%s, error=%s", instance, e)
    
    def _save_tenant_breaker_states(self, conn) -> None:
        """保存所有租户级熔断状态到持久化存储"""
        for tenant_id, breaker in self._tenant_breakers.items():
            try:
                state_dict = breaker.get_state_dict()
                scm_db.save_circuit_breaker_state(conn, breaker._key, state_dict)
            except Exception as e:
                logger.warning("保存租户熔断状态失败: tenant=%s, error=%s", tenant_id, e)
    
    def _handle_shutdown(self, signum, frame):
        """处理关闭信号"""
        logger.info("收到关闭信号 (%s)，准备退出...", signum)
        self._shutdown_requested = True
    
    @property
    def scheduler_config(self) -> SchedulerConfig:
        return self._scheduler_config
    
    def _build_repo_sync_states(
        self,
        conn,
        repos: List[Dict[str, Any]],
        queued_pairs: set,
    ) -> List[RepoSyncState]:
        """
        构建仓库同步状态列表
        
        使用批量查询优化，仅需 1~3 次核心查询即可构建全量 states:
        1. 批量获取游标 updated_at（按 repo_type 分组查询）
        2. 批量获取 sync_runs 统计
        
        Args:
            conn: 数据库连接
            repos: 仓库信息列表
            queued_pairs: 当前队列中的 (repo_id, job_type) 集合
        
        Returns:
            RepoSyncState 列表
        """
        if not repos:
            return []
        
        # 收集所有 repo_ids，按类型分组
        all_repo_ids = [r["repo_id"] for r in repos]
        git_repo_ids = [r["repo_id"] for r in repos if r["repo_type"] == "git"]
        svn_repo_ids = [r["repo_id"] for r in repos if r["repo_type"] == "svn"]
        
        # === 批量查询 1: 获取 sync_runs 统计 ===
        stats_map = scm_db.get_recent_sync_runs_stats_batch(
            conn,
            all_repo_ids,
            window_size=self._scheduler_config.error_budget_window_size,
        )
        
        # === 批量查询 2 & 3: 获取游标 updated_at（按类型分组）===
        # Git 仓库使用 gitlab_cursor
        git_cursor_map: Dict[int, Optional[float]] = {}
        if git_repo_ids:
            git_cursor_map = scm_db.get_kv_cursors_updated_at_for_repos(
                conn, git_repo_ids, "gitlab"
            )
        
        # SVN 仓库使用 svn_cursor
        svn_cursor_map: Dict[int, Optional[float]] = {}
        if svn_repo_ids:
            svn_cursor_map = scm_db.get_kv_cursors_updated_at_for_repos(
                conn, svn_repo_ids, "svn"
            )
        
        # 构建 states
        states: List[RepoSyncState] = []
        
        for repo in repos:
            repo_id = repo["repo_id"]
            repo_type = repo["repo_type"]
            
            # 从批量结果获取统计
            stats = stats_map.get(repo_id, {})
            
            # 从批量结果获取游标时间戳
            cursor_ts = None
            if repo_type == "git":
                cursor_ts = git_cursor_map.get(repo_id)
            elif repo_type == "svn":
                cursor_ts = svn_cursor_map.get(repo_id)
            
            # 解析 GitLab 实例和租户 ID
            gitlab_instance = None
            tenant_id = None
            
            if repo_type == "git":
                # 从 URL 解析实例
                url = repo.get("url", "")
                if "://" in url:
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(url)
                        gitlab_instance = parsed.netloc
                    except Exception:
                        pass
                
                # 租户 ID 可以从 project_key 解析
                project_key = repo.get("project_key", "")
                if "/" in project_key:
                    tenant_id = project_key.split("/")[0]
            
            # 注意：is_queued 不再在 repo 级别设置
            # 现在 per-job_type 的队列检查在 select_jobs_to_enqueue() 中通过 queued_pairs 参数完成
            
            state = RepoSyncState(
                repo_id=repo_id,
                repo_type=repo_type,
                gitlab_instance=gitlab_instance,
                tenant_id=tenant_id,
                cursor_updated_at=cursor_ts,
                recent_run_count=stats.get("total_runs", 0),
                recent_failed_count=stats.get("failed_runs", 0),
                recent_429_hits=stats.get("total_429_hits", 0),
                recent_total_requests=stats.get("total_requests", 0),
                last_run_status=stats.get("last_run_status"),
                last_run_at=stats.get("last_run_at"),
                is_queued=False,  # 保持兼容，实际检查通过 queued_pairs 完成
            )
            states.append(state)
        
        return states
    
    def _get_last_successful_cursor(
        self,
        conn,
        repo_id: int,
        job_type: str,
        repo_type: str,
    ) -> tuple:
        """
        获取仓库的最后成功同步游标
        
        Args:
            conn: 数据库连接
            repo_id: 仓库 ID
            job_type: 任务类型（logical）
            repo_type: 仓库类型 ('git' | 'svn')
        
        Returns:
            对于 time-based: (last_cursor_ts: float or None, None)
            对于 rev-based (SVN): (None, last_rev: int or None)
        """
        # 获取最近一次成功的 sync_run
        latest_run = scm_db.get_latest_sync_run(conn, repo_id, job_type)
        
        if latest_run is None or latest_run.get("status") != "completed":
            # 没有成功的运行记录，尝试从游标获取
            cursor_type = get_cursor_type_for_job(job_type, repo_type)
            if cursor_type:
                cursor = load_cursor(cursor_type, repo_id, config=self._config)
                if cursor:
                    cursor_ts = get_cursor_updated_at_timestamp(cursor)
                    # 对于 SVN，尝试从游标数据中获取 last_rev
                    if repo_type == "svn" and hasattr(cursor, "data"):
                        last_rev = cursor.data.get("last_rev") if cursor.data else None
                        return (cursor_ts, last_rev)
                    return (cursor_ts, None)
            return (None, None)
        
        # 从 cursor_after 解析
        cursor_after = latest_run.get("cursor_after")
        if cursor_after:
            if isinstance(cursor_after, str):
                import json
                try:
                    cursor_after = json.loads(cursor_after)
                except (json.JSONDecodeError, TypeError):
                    cursor_after = {}
            
            if repo_type == "svn":
                # SVN 使用 revision 号
                last_rev = cursor_after.get("last_rev") or cursor_after.get("rev")
                return (None, last_rev)
            else:
                # Git/MR/Review 使用时间戳
                cursor_ts = cursor_after.get("updated_at") or cursor_after.get("since")
                if isinstance(cursor_ts, str):
                    # 解析 ISO 格式时间
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(cursor_ts.replace("Z", "+00:00"))
                        cursor_ts = dt.timestamp()
                    except (ValueError, AttributeError):
                        cursor_ts = None
                return (cursor_ts, None)
        
        return (None, None)
    
    def _generate_backfill_jobs(
        self,
        conn,
        states: List[RepoSyncState],
        circuit_decision: CircuitBreakerDecision,
        queued_pairs: set,
    ) -> List[Dict[str, Any]]:
        """
        生成 backfill 任务
        
        当熔断器处于 OPEN/HALF_OPEN 状态时，生成 backfill 任务代替正常的增量任务。
        
        Args:
            conn: 数据库连接
            states: 仓库同步状态列表
            circuit_decision: 熔断决策
            queued_pairs: 当前队列中的 (repo_id, job_type) 集合
        
        Returns:
            待入队的 backfill 任务列表
        """
        backfill_jobs = []
        
        for state in states:
            # 获取该仓库支持的 job 类型
            logical_job_types = get_logical_job_types_for_repo(state.repo_type)
            
            for logical_job_type in logical_job_types:
                # 转换为 physical_job_type
                physical_job_type = logical_to_physical(logical_job_type, state.repo_type)
                
                # 检查是否已在队列中
                if (state.repo_id, physical_job_type) in queued_pairs:
                    continue
                
                # 获取最后成功的游标
                cursor_ts, last_rev = self._get_last_successful_cursor(
                    conn, state.repo_id, logical_job_type, state.repo_type
                )
                
                # 根据仓库类型计算 backfill 窗口
                if state.repo_type == "svn":
                    # SVN 使用 revision 窗口
                    # 获取当前 HEAD revision（如果可能）
                    current_head_rev = scm_db.get_latest_svn_revision(conn, state.repo_id)
                    
                    backfill_window = compute_svn_backfill_window(
                        last_successful_rev=last_rev,
                        current_head_rev=current_head_rev,
                        config=self._scheduler_config,
                        max_rev_window=1000,  # 最大 1000 个 revision
                        chunk_size=100,       # 每 100 个为一块
                    )
                    
                    # 检查是否有需要回填的内容
                    if backfill_window.total_chunks == 0:
                        continue
                else:
                    # Git/MR/Review 使用时间窗口
                    backfill_window = compute_time_backfill_window(
                        last_successful_cursor_ts=cursor_ts,
                        config=self._scheduler_config,
                        chunk_hours=24,  # 每 24 小时为一块
                    )
                
                # 构建 payload
                payload = backfill_window.to_payload()
                payload.update({
                    "reason": "backfill",
                    "scheduled_at": datetime.now(timezone.utc).isoformat(),
                    "logical_job_type": logical_job_type,
                    "physical_job_type": physical_job_type,
                    # 熔断状态信息
                    "circuit_state": circuit_decision.current_state,
                    "is_backfill_only": True,
                    "suggested_batch_size": circuit_decision.suggested_batch_size,
                    "suggested_diff_mode": circuit_decision.suggested_diff_mode,
                    # === 写入 instance/tenant 供 worker 快速过滤 ===
                    "gitlab_instance": state.gitlab_instance,
                    "tenant_id": state.tenant_id,
                })
                
                # 计算优先级（backfill 任务优先级略低）
                base_priority = self._scheduler_config.job_type_priority.get(logical_job_type, 10) * 100
                priority = base_priority + 50  # backfill 任务优先级 +50
                
                backfill_jobs.append({
                    "repo_id": state.repo_id,
                    "job_type": physical_job_type,
                    "priority": priority,
                    "mode": "backfill",
                    "payload_json": payload,
                })
        
        return backfill_jobs
    
    @property
    def circuit_breaker(self) -> CircuitBreakerController:
        """熔断控制器"""
        return self._circuit_breaker
    
    def scan_and_enqueue(self) -> ScanResult:
        """
        执行一次扫描并入队任务
        
        集成熔断检查：
        - 检查全局健康统计
        - 根据熔断状态决定是否入队、入队模式（正常/backfill）
        - 更新熔断状态
        
        Returns:
            ScanResult 扫描结果
        """
        result = ScanResult()
        start_time = time.time()
        
        try:
            conn = get_connection(config=self._config)
            
            try:
                # 获取全局健康统计（用于熔断检查）
                health_stats = scm_db.get_sync_runs_health_stats(
                    conn,
                    window_count=self._circuit_breaker_config.window_count,
                    window_minutes=self._circuit_breaker_config.window_minutes,
                )
                
                # 检查全局熔断状态
                circuit_decision = self._circuit_breaker.check(health_stats)
                
                # 保存熔断状态（每次检查后更新）
                self._save_circuit_breaker_state(conn)
                
                # === 获取按实例聚合的健康统计并检查实例级熔断 ===
                instance_health_stats = scm_db.get_sync_runs_health_stats_by_dimension(
                    conn,
                    dimension="instance",
                    window_count=self._circuit_breaker_config.window_count,
                    window_minutes=self._circuit_breaker_config.window_minutes,
                )
                
                # 为每个实例检查熔断状态
                # {instance_name: CircuitBreakerDecision}
                instance_decisions: Dict[str, CircuitBreakerDecision] = {}
                for instance_name, instance_stats in instance_health_stats.items():
                    breaker = self._get_instance_breaker(instance_name)
                    decision = breaker.check(instance_stats)
                    instance_decisions[instance_name] = decision
                    
                    if decision.current_state != CircuitState.CLOSED.value:
                        logger.info(
                            "实例熔断状态: instance=%s, state=%s, reason=%s",
                            instance_name, decision.current_state, decision.trigger_reason
                        )
                
                # 保存实例级熔断状态
                self._save_instance_breaker_states(conn)
                
                # 如果完全熔断且不允许同步
                if not circuit_decision.allow_sync:
                    logger.warning(
                        "熔断器触发，跳过本次扫描: state=%s, reason=%s, wait=%.1fs",
                        circuit_decision.current_state,
                        circuit_decision.trigger_reason,
                        circuit_decision.wait_seconds,
                    )
                    result.errors.append(f"circuit_breaker_open: {circuit_decision.trigger_reason}")
                    return result
                
                # 获取当前队列状态
                current_queue_size = scm_db.get_pending_sync_jobs_count(conn)
                queued_pairs = set(scm_db.get_queued_repo_job_pairs(conn))
                
                # 获取预算占用快照
                db_budget_snapshot = scm_db.get_active_jobs_budget_snapshot(conn, include_pending=True)
                
                # 转换为策略层的 BudgetSnapshot
                budget_snapshot = BudgetSnapshot(
                    global_running=db_budget_snapshot.global_running,
                    global_pending=db_budget_snapshot.global_pending,
                    global_active=db_budget_snapshot.global_active,
                    by_instance=dict(db_budget_snapshot.by_instance),
                    by_tenant=dict(db_budget_snapshot.by_tenant),
                )
                
                logger.debug(
                    "当前队列状态: size=%d, pairs=%d, running=%d, active=%d",
                    current_queue_size, len(queued_pairs),
                    budget_snapshot.global_running, budget_snapshot.global_active
                )
                
                # 获取所有仓库
                repos = scm_db.list_repos_for_scheduling(conn)
                result.scanned_repos = len(repos)
                
                if not repos:
                    logger.info("没有找到需要调度的仓库")
                    return result
                
                # === Pause 自动解除检查 ===
                # 在调度前检查健康恢复的 repo/job_type，自动解除暂停
                all_repo_ids = [r["repo_id"] for r in repos]
                auto_unpaused = batch_check_and_auto_unpause(
                    conn,
                    all_repo_ids,
                    failure_rate_threshold=self._scheduler_config.error_budget_threshold,
                    window_size=self._scheduler_config.error_budget_window_size,
                )
                if auto_unpaused:
                    logger.info(
                        "自动解除 %d 个暂停记录（健康恢复）: %s",
                        len(auto_unpaused),
                        [(r.repo_id, r.job_type) for r in auto_unpaused[:5]],  # 只显示前5个
                    )
                
                # 清理过期的暂停记录
                expired_count = clear_expired_pauses(conn)
                if expired_count > 0:
                    logger.debug("清理 %d 个过期暂停记录", expired_count)
                
                # 获取当前暂停的 (repo_id, job_type) 集合
                paused_pairs = get_paused_repo_job_pairs(conn, repo_ids=all_repo_ids)
                if paused_pairs:
                    logger.debug("当前暂停的 repo/job_type: %d 个", len(paused_pairs))
                
                # === 获取所有实例的 bucket 状态 ===
                # 读取 rate limit bucket 状态，用于检查 instance 级别的暂停
                bucket_statuses: Dict[str, InstanceBucketStatus] = {}
                try:
                    raw_buckets = list_rate_limit_buckets(conn)
                    for raw_bucket in raw_buckets:
                        bucket_status = InstanceBucketStatus.from_db_status(raw_bucket)
                        bucket_statuses[bucket_status.instance_key] = bucket_status
                        
                        if bucket_status.is_paused:
                            logger.debug(
                                "Bucket 暂停中: instance=%s, remaining=%.1fs",
                                bucket_status.instance_key,
                                bucket_status.pause_remaining_seconds,
                            )
                    
                    if bucket_statuses:
                        paused_bucket_count = sum(1 for b in bucket_statuses.values() if b.is_paused)
                        logger.debug(
                            "读取 %d 个 bucket 状态，其中 %d 个暂停",
                            len(bucket_statuses), paused_bucket_count
                        )
                except Exception as e:
                    logger.warning("读取 bucket 状态失败: %s", e)
                    # 失败时使用空字典，不影响调度
                
                # 构建仓库状态
                states = self._build_repo_sync_states(conn, repos, queued_pairs)
                
                # 确定每个仓库的 job 类型
                # 扩展 states 为每个仓库的每个 job 类型创建状态
                # 注意：这里使用 logical_job_type，在入队时转换为 physical_job_type
                expanded_states = []
                for state in states:
                    # 获取该仓库类型支持的 logical job 类型
                    logical_job_types = get_logical_job_types_for_repo(state.repo_type)
                    
                    for logical_job_type in logical_job_types:
                        # 转换为 physical_job_type 用于检查队列
                        physical_job_type = logical_to_physical(logical_job_type, state.repo_type)
                        
                        # 检查此 (repo_id, physical_job_type) 是否已在队列
                        is_queued = (state.repo_id, physical_job_type) in queued_pairs
                        
                        # 获取特定 job 类型的游标
                        cursor_type = get_cursor_type_for_job(logical_job_type, state.repo_type)
                        cursor_ts = state.cursor_updated_at
                        
                        if cursor_type and logical_job_type != "commits":
                            # 对于 mrs/reviews，加载各自的游标
                            cursor = load_cursor(cursor_type, state.repo_id, config=self._config)
                            cursor_ts = get_cursor_updated_at_timestamp(cursor)
                        
                        new_state = RepoSyncState(
                            repo_id=state.repo_id,
                            repo_type=state.repo_type,
                            gitlab_instance=state.gitlab_instance,
                            tenant_id=state.tenant_id,
                            cursor_updated_at=cursor_ts,
                            recent_run_count=state.recent_run_count,
                            recent_failed_count=state.recent_failed_count,
                            recent_429_hits=state.recent_429_hits,
                            recent_total_requests=state.recent_total_requests,
                            last_run_status=state.last_run_status,
                            last_run_at=state.last_run_at,
                            is_queued=is_queued,
                        )
                        expanded_states.append((new_state, logical_job_type))
                
                # 构建 per-job_type 的 queued 集合
                # 将 queued_pairs 中的 physical_job_type 映射回 logical_job_type
                # 以便 policy 层按 (repo_id, logical_job_type) 判断
                logical_queued_pairs = set()
                for repo_id, physical_job_type in queued_pairs:
                    # 从 physical 反推 logical（简化映射）
                    if physical_job_type == "gitlab_commits" or physical_job_type == "svn":
                        logical_queued_pairs.add((repo_id, "commits"))
                    elif physical_job_type == "gitlab_mrs":
                        logical_queued_pairs.add((repo_id, "mrs"))
                    elif physical_job_type == "gitlab_reviews":
                        logical_queued_pairs.add((repo_id, "reviews"))
                    else:
                        # 未知类型，保留原样
                        logical_queued_pairs.add((repo_id, physical_job_type))
                
                # 使用策略选择需要入队的任务（policy 层使用 logical_job_type）
                all_logical_job_types = list(set(GIT_LOGICAL_JOB_TYPES + SVN_LOGICAL_JOB_TYPES_LIST))
                
                # 合并 queued_pairs 和 paused_pairs
                # paused_pairs 已经是 logical_job_type 格式（commits/mrs/reviews）
                combined_skip_pairs = logical_queued_pairs | paused_pairs
                
                candidates = select_jobs_to_enqueue(
                    states=states,
                    job_types=all_logical_job_types,
                    config=self._scheduler_config,
                    current_queue_size=current_queue_size,
                    queued_pairs=combined_skip_pairs,
                    budget_snapshot=budget_snapshot,
                    bucket_statuses=bucket_statuses,
                    skip_on_bucket_pause=False,  # 不跳过，仅降权
                )
                
                result.candidates_found = len(candidates)
                
                # 统计暂停的仓库
                result.paused_repos = sum(1 for c in candidates if c.should_pause)
                
                # 为需要暂停的 candidates 设置暂停记录
                for c in candidates:
                    if c.should_pause:
                        set_repo_job_pause(
                            conn,
                            repo_id=c.repo_id,
                            job_type=c.job_type,
                            pause_duration_seconds=self._scheduler_config.pause_duration_seconds,
                            reason=c.pause_reason or "error_budget_exceeded",
                            failure_rate=c.failure_rate,
                        )
                        logger.info(
                            "设置暂停: repo_id=%d, job_type=%s, duration=%ds, reason=%s, failure_rate=%.2f",
                            c.repo_id, c.job_type,
                            self._scheduler_config.pause_duration_seconds,
                            c.pause_reason, c.failure_rate,
                        )
                
                # 过滤掉需要暂停的
                candidates_to_enqueue = [c for c in candidates if not c.should_pause]
                
                if not candidates_to_enqueue:
                    logger.info(
                        "扫描完成: repos=%d, candidates=%d, paused=%d, 无需入队",
                        result.scanned_repos, result.candidates_found, result.paused_repos
                    )
                    return result
                
                # 批量入队
                # 注意：candidate.job_type 是 logical_job_type，需要转换为 physical_job_type
                jobs_to_insert = []
                instance_paused_count = 0  # 因实例熔断而暂停的任务数
                
                for candidate in candidates_to_enqueue:
                    # 查找对应的 state 以获取 repo_type
                    state = next((s for s in states if s.repo_id == candidate.repo_id), None)
                    if state is None:
                        logger.warning(f"找不到 repo_id={candidate.repo_id} 的状态，跳过")
                        continue
                    
                    # === 实例级熔断检查 ===
                    instance_decision = None
                    if state.gitlab_instance and state.gitlab_instance in instance_decisions:
                        instance_decision = instance_decisions[state.gitlab_instance]
                        
                        # 如果实例已完全熔断（OPEN）且不允许同步，跳过该候选
                        if not instance_decision.allow_sync:
                            logger.debug(
                                "实例熔断，跳过候选: repo_id=%d, instance=%s, state=%s",
                                candidate.repo_id, state.gitlab_instance, instance_decision.current_state
                            )
                            instance_paused_count += 1
                            continue
                    
                    # 将 logical_job_type 转换为 physical_job_type
                    try:
                        physical_job_type = logical_to_physical(candidate.job_type, state.repo_type)
                    except ValueError as e:
                        logger.warning(f"job_type 转换失败: {e}，跳过")
                        continue
                    
                    # 构建任务 payload（包含调度原因与熔断状态信息）
                    payload = {
                        "reason": candidate.reason,
                        "cursor_age_seconds": candidate.cursor_age_seconds,
                        "failure_rate": candidate.failure_rate,
                        "rate_limit_rate": candidate.rate_limit_rate,
                        "scheduled_at": datetime.now(timezone.utc).isoformat(),
                        # 记录 logical 和 physical job_type 用于追踪
                        "logical_job_type": candidate.job_type,
                        "physical_job_type": physical_job_type,
                        # === 写入 instance/tenant 供 worker 快速过滤 ===
                        # 允许 worker 按 pool 过滤任务，无需 claim 时 join repos 表
                        "gitlab_instance": state.gitlab_instance,
                        "tenant_id": state.tenant_id,
                    }
                    
                    # === 写入 bucket 降权/跳过原因（便于排障）===
                    if candidate.bucket_paused:
                        payload["bucket_paused"] = True
                        payload["bucket_pause_remaining_seconds"] = candidate.bucket_pause_remaining_seconds
                    if candidate.bucket_penalty_reason:
                        payload["bucket_penalty_reason"] = candidate.bucket_penalty_reason
                        payload["bucket_penalty_value"] = candidate.bucket_penalty_value
                    
                    # 确定任务模式（incremental / backfill）
                    task_mode = "incremental"
                    
                    # 如果处于全局熔断降级模式，添加降级参数
                    if circuit_decision.is_backfill_only:
                        payload["circuit_state"] = circuit_decision.current_state
                        payload["is_backfill_only"] = True
                        payload["suggested_batch_size"] = circuit_decision.suggested_batch_size
                        payload["suggested_diff_mode"] = circuit_decision.suggested_diff_mode
                        task_mode = "backfill"
                    
                    # === 实例级熔断降级模式 ===
                    # 如果实例处于 OPEN/HALF_OPEN 但允许同步（backfill 模式），应用降级参数
                    if instance_decision and instance_decision.is_backfill_only:
                        payload["instance_circuit_state"] = instance_decision.current_state
                        payload["instance_is_backfill_only"] = True
                        payload["instance_suggested_batch_size"] = instance_decision.suggested_batch_size
                        payload["instance_suggested_diff_mode"] = instance_decision.suggested_diff_mode
                        # 优先使用更保守的参数
                        payload["suggested_batch_size"] = min(
                            payload.get("suggested_batch_size", 100),
                            instance_decision.suggested_batch_size
                        )
                        payload["suggested_diff_mode"] = instance_decision.suggested_diff_mode
                        task_mode = "backfill"
                    
                    # 使用 physical_job_type 入队，确保队列唯一键语义清晰
                    jobs_to_insert.append({
                        "repo_id": candidate.repo_id,
                        "job_type": physical_job_type,  # 使用 physical_job_type
                        "priority": candidate.priority,
                        "mode": task_mode,
                        "payload_json": payload,
                    })
                
                # 记录因实例熔断而暂停的任务
                if instance_paused_count > 0:
                    logger.info(
                        "因实例熔断暂停 %d 个候选任务",
                        instance_paused_count
                    )
                    result.paused_repos += instance_paused_count
                
                # === Backfill 任务生成逻辑 ===
                # 当熔断器处于 OPEN/HALF_OPEN 状态时，生成 backfill 任务
                backfill_jobs = []
                if circuit_decision.is_backfill_only or circuit_decision.current_state in (
                    CircuitState.OPEN.value, CircuitState.HALF_OPEN.value
                ):
                    logger.info(
                        "熔断器处于 %s 状态，优先生成 backfill 任务",
                        circuit_decision.current_state
                    )
                    
                    # 更新 queued_pairs 以包含即将入队的增量任务
                    updated_queued_pairs = set(queued_pairs)
                    for job in jobs_to_insert:
                        updated_queued_pairs.add((job["repo_id"], job["job_type"]))
                    
                    # 生成 backfill 任务
                    backfill_jobs = self._generate_backfill_jobs(
                        conn, states, circuit_decision, updated_queued_pairs
                    )
                    
                    if backfill_jobs:
                        logger.info(
                            "生成 %d 个 backfill 任务 (mode='backfill')",
                            len(backfill_jobs)
                        )
                        
                        # 在熔断模式下，优先入队 backfill 任务，减少增量任务
                        if circuit_decision.current_state == CircuitState.OPEN.value:
                            # OPEN 状态：只入队 backfill 任务
                            jobs_to_insert = backfill_jobs
                        else:
                            # HALF_OPEN 状态：混合入队，backfill 优先
                            jobs_to_insert = backfill_jobs + jobs_to_insert
                
                job_ids = scm_db.enqueue_sync_jobs_batch(conn, jobs_to_insert)
                conn.commit()
                
                # 统计结果
                result.jobs_enqueued = sum(1 for jid in job_ids if jid is not None)
                result.jobs_skipped = len(job_ids) - result.jobs_enqueued
                
                logger.info(
                    "扫描完成: repos=%d, candidates=%d, enqueued=%d, skipped=%d, paused=%d",
                    result.scanned_repos,
                    result.candidates_found,
                    result.jobs_enqueued,
                    result.jobs_skipped,
                    result.paused_repos,
                )
                
            finally:
                conn.close()
        
        except Exception as e:
            logger.exception("扫描失败: %s", e)
            result.errors.append(str(e))
        
        finally:
            result.scan_duration_seconds = round(time.time() - start_time, 3)
        
        return result
    
    def run_loop(
        self,
        max_iterations: int = 0,
    ) -> None:
        """
        持续循环执行扫描
        
        Args:
            max_iterations: 最大迭代次数，0 表示无限
        """
        iteration = 0
        interval = self._scheduler_config.scan_interval_seconds
        
        logger.info(
            "启动调度循环: interval=%ds, max_iterations=%s",
            interval, max_iterations if max_iterations > 0 else "无限"
        )
        
        while not self._shutdown_requested:
            iteration += 1
            
            logger.debug("调度循环 #%d", iteration)
            
            try:
                result = self.scan_and_enqueue()
                
                if result.errors:
                    for err in result.errors:
                        logger.error("扫描错误: %s", err)
            
            except Exception as e:
                logger.exception("调度循环异常: %s", e)
            
            # 检查是否达到最大迭代次数
            if max_iterations > 0 and iteration >= max_iterations:
                logger.info("已达到最大迭代次数 (%d)，退出循环", max_iterations)
                break
            
            # 等待下一次扫描（分段等待以响应关闭信号）
            for _ in range(interval):
                if self._shutdown_requested:
                    break
                time.sleep(1)
        
        logger.info("调度循环结束，共执行 %d 次扫描", iteration)
    
    def get_status(self) -> Dict[str, Any]:
        """
        获取调度器状态
        
        Returns:
            状态信息字典
        """
        try:
            conn = get_connection(config=self._config)
            
            try:
                # 队列统计
                pending_count = scm_db.get_pending_sync_jobs_count(conn)
                queued_pairs = scm_db.get_queued_repo_job_pairs(conn)
                
                # 仓库统计
                repos = scm_db.list_repos_for_scheduling(conn)
                
                # 健康统计（用于显示熔断状态）
                health_stats = scm_db.get_sync_runs_health_stats(
                    conn,
                    window_count=self._circuit_breaker_config.window_count,
                    window_minutes=self._circuit_breaker_config.window_minutes,
                )
                
                # 熔断状态
                circuit_state = self._circuit_breaker.get_state_dict()
                
                return {
                    "queue": {
                        "pending_jobs": pending_count,
                        "queued_pairs": len(queued_pairs),
                    },
                    "repos": {
                        "total": len(repos),
                        "git": sum(1 for r in repos if r["repo_type"] == "git"),
                        "svn": sum(1 for r in repos if r["repo_type"] == "svn"),
                    },
                    "health": {
                        "total_runs": health_stats.get("total_runs", 0),
                        "failed_rate": health_stats.get("failed_rate", 0.0),
                        "rate_limit_rate": health_stats.get("rate_limit_rate", 0.0),
                        "avg_duration_seconds": health_stats.get("avg_duration_seconds", 0.0),
                    },
                    "circuit_breaker": circuit_state,
                    "config": asdict(self._scheduler_config),
                }
            
            finally:
                conn.close()
        
        except Exception as e:
            return {"error": str(e)}


# === CLI ===


def create_parser() -> argparse.ArgumentParser:
    """创建命令行解析器"""
    parser = argparse.ArgumentParser(
        prog="scm_sync_scheduler",
        description="SCM 同步调度器 - 周期性扫描仓库状态并入队同步任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 执行一次扫描
    %(prog)s scan

    # 持续循环调度
    %(prog)s run --loop

    # 显示配置
    %(prog)s config

    # 查看状态
    %(prog)s status
""",
    )
    
    # 全局参数
    add_config_argument(parser)
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出模式",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="输出 JSON 格式结果",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # === scan 子命令 ===
    scan_parser = subparsers.add_parser(
        "scan",
        help="执行一次扫描并入队",
    )
    
    # === run 子命令 ===
    run_parser = subparsers.add_parser(
        "run",
        help="运行调度器",
    )
    run_parser.add_argument(
        "--loop",
        action="store_true",
        help="持续循环模式",
    )
    run_parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        metavar="N",
        help="最大迭代次数（默认: 无限）",
    )
    run_parser.add_argument(
        "--interval",
        type=int,
        metavar="SECONDS",
        help="扫描间隔秒数（覆盖配置）",
    )
    
    # === config 子命令 ===
    config_parser = subparsers.add_parser(
        "config",
        help="显示配置信息",
    )
    
    # === status 子命令 ===
    status_parser = subparsers.add_parser(
        "status",
        help="显示调度器状态",
    )
    
    return parser


def main(args: Optional[List[str]] = None) -> int:
    """主入口"""
    parser = create_parser()
    parsed = parser.parse_args(args)
    
    if not parsed.command:
        parser.print_help()
        return 1
    
    # 设置日志级别
    if parsed.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 加载配置
    try:
        config = get_config(getattr(parsed, "config_path", None), reload=True)
        config.load()
    except Exception as e:
        logger.error("配置加载失败: %s", e)
        return 1
    
    # 创建调度器
    scheduler = SyncScheduler(config=config)
    
    # 处理命令
    if parsed.command == "scan":
        result = scheduler.scan_and_enqueue()
        
        if getattr(parsed, "json_output", False):
            print(result.to_json())
        else:
            print(f"扫描完成:")
            print(f"  仓库数: {result.scanned_repos}")
            print(f"  候选数: {result.candidates_found}")
            print(f"  入队数: {result.jobs_enqueued}")
            print(f"  跳过数: {result.jobs_skipped}")
            print(f"  暂停数: {result.paused_repos}")
            print(f"  耗时: {result.scan_duration_seconds:.3f}s")
            
            if result.errors:
                print(f"  错误:")
                for err in result.errors:
                    print(f"    - {err}")
        
        return 0 if not result.errors else 1
    
    elif parsed.command == "run":
        if not parsed.loop:
            # 单次执行
            result = scheduler.scan_and_enqueue()
            if getattr(parsed, "json_output", False):
                print(result.to_json())
            return 0 if not result.errors else 1
        
        # 覆盖扫描间隔
        if parsed.interval:
            scheduler._scheduler_config.scan_interval_seconds = parsed.interval
        
        # 循环执行
        scheduler.run_loop(max_iterations=parsed.max_iterations)
        return 0
    
    elif parsed.command == "config":
        cfg = asdict(scheduler.scheduler_config)
        if getattr(parsed, "json_output", False):
            print(json.dumps(cfg, indent=2, ensure_ascii=False))
        else:
            print("调度器配置:")
            for key, value in cfg.items():
                print(f"  {key}: {value}")
        return 0
    
    elif parsed.command == "status":
        status = scheduler.get_status()
        if getattr(parsed, "json_output", False):
            print(json.dumps(status, indent=2, ensure_ascii=False))
        else:
            print("调度器状态:")
            if "error" in status:
                print(f"  错误: {status['error']}")
            else:
                print(f"  队列:")
                print(f"    待处理任务: {status['queue']['pending_jobs']}")
                print(f"    活跃任务对: {status['queue']['queued_pairs']}")
                print(f"  仓库:")
                print(f"    总数: {status['repos']['total']}")
                print(f"    Git: {status['repos']['git']}")
                print(f"    SVN: {status['repos']['svn']}")
                print(f"  健康统计:")
                health = status.get('health', {})
                print(f"    总运行数: {health.get('total_runs', 0)}")
                print(f"    失败率: {health.get('failed_rate', 0.0):.1%}")
                print(f"    429命中率: {health.get('rate_limit_rate', 0.0):.1%}")
                print(f"    平均耗时: {health.get('avg_duration_seconds', 0.0):.1f}s")
                print(f"  熔断器:")
                cb = status.get('circuit_breaker', {})
                print(f"    状态: {cb.get('state', 'unknown')}")
                if cb.get('last_failure_reason'):
                    print(f"    触发原因: {cb.get('last_failure_reason')}")
        return 0
    
    else:
        logger.error("未知命令: %s", parsed.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
