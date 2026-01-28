#!/usr/bin/env python3
"""
scm_sync_runner.py - SCM 同步运行器

支持按 repo/job 执行两阶段同步:
1. incremental --loop: 持续增量同步
2. backfill last_N_hours/days: 回填历史数据

特性:
- 回填默认不更新 watermark，仅在显式 flag 下更新
- 修复窗口参数化 (scm.backfill.repair_window_hours, scm.backfill.cron_hint)
- 输出结构化结果 (JSON) 供外部调度器 (cron/K8s) 使用
- 支持 dry-run 模式

使用:
    # 增量同步（持续循环）
    python scm_sync_runner.py incremental --repo gitlab:123 --loop

    # 回填最近 24 小时
    python scm_sync_runner.py backfill --repo gitlab:123 --last-hours 24

    # 回填最近 7 天，并更新 watermark
    python scm_sync_runner.py backfill --repo gitlab:123 --last-days 7 --update-watermark

配置示例:
    [scm.backfill]
    repair_window_hours = 24      # 默认修复窗口
    cron_hint = "0 2 * * *"       # 建议的 cron 表达式（仅供参考）
    max_concurrent_jobs = 4       # 最大并发任务数
    default_update_watermark = false  # 回填默认不更新 watermark
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from engram_step1.config import (
    Config,
    add_config_argument,
    get_config,
    get_app_config,
)
from engram_step1.db import get_connection
from engram_step1.errors import EngramError, ConfigError

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# === 常量定义 ===

# 默认配置值
DEFAULT_REPAIR_WINDOW_HOURS = 24
DEFAULT_CRON_HINT = "0 2 * * *"
DEFAULT_MAX_CONCURRENT_JOBS = 4
DEFAULT_UPDATE_WATERMARK = False
DEFAULT_LOOP_INTERVAL_SECONDS = 60
DEFAULT_MAX_LOOP_ITERATIONS = 0  # 0 表示无限循环

# 窗口切分相关默认值
DEFAULT_WINDOW_CHUNK_HOURS = 4  # 默认每个窗口块的小时数
DEFAULT_WINDOW_CHUNK_REVS = 100  # SVN 默认每个窗口块的 revision 数

# 支持的仓库类型
REPO_TYPE_GITLAB = "gitlab"
REPO_TYPE_SVN = "svn"
VALID_REPO_TYPES = {REPO_TYPE_GITLAB, REPO_TYPE_SVN}

# 同步任务类型
JOB_TYPE_COMMITS = "commits"
JOB_TYPE_MRS = "mrs"
JOB_TYPE_REVIEWS = "reviews"
VALID_JOB_TYPES = {JOB_TYPE_COMMITS, JOB_TYPE_MRS, JOB_TYPE_REVIEWS}


class RunnerPhase(str, Enum):
    """运行器阶段枚举"""
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"


class RunnerStatus(str, Enum):
    """运行器状态枚举"""
    SUCCESS = "success"
    PARTIAL = "partial"  # 部分成功
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    LOCKED = "locked"  # 锁被其他 worker 持有


@dataclass
class RepoSpec:
    """仓库规格"""
    repo_type: str  # gitlab, svn
    repo_id: str    # project_id 或 repo URL
    
    @classmethod
    def parse(cls, spec: str) -> "RepoSpec":
        """
        解析仓库规格字符串
        
        格式: <type>:<id>
        示例: gitlab:123, gitlab:namespace/project, svn:https://svn.example.com/repo
        """
        if ":" not in spec:
            raise ValueError(f"无效的仓库规格: {spec}，格式应为 <type>:<id>")
        
        repo_type, repo_id = spec.split(":", 1)
        repo_type = repo_type.lower()
        
        if repo_type not in VALID_REPO_TYPES:
            raise ValueError(
                f"不支持的仓库类型: {repo_type}，有效值: {', '.join(sorted(VALID_REPO_TYPES))}"
            )
        
        if not repo_id:
            raise ValueError(f"仓库 ID 不能为空: {spec}")
        
        return cls(repo_type=repo_type, repo_id=repo_id)
    
    def __str__(self) -> str:
        return f"{self.repo_type}:{self.repo_id}"


@dataclass
class JobSpec:
    """任务规格"""
    job_type: str  # commits, mrs, reviews
    
    @classmethod
    def parse(cls, spec: str) -> "JobSpec":
        """解析任务规格字符串"""
        job_type = spec.lower()
        if job_type not in VALID_JOB_TYPES:
            raise ValueError(
                f"不支持的任务类型: {job_type}，有效值: {', '.join(sorted(VALID_JOB_TYPES))}"
            )
        return cls(job_type=job_type)
    
    def __str__(self) -> str:
        return self.job_type


@dataclass
class BackfillConfig:
    """回填配置"""
    repair_window_hours: int = DEFAULT_REPAIR_WINDOW_HOURS
    cron_hint: str = DEFAULT_CRON_HINT
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS
    default_update_watermark: bool = DEFAULT_UPDATE_WATERMARK
    
    @classmethod
    def from_config(cls, config: Optional[Config] = None) -> "BackfillConfig":
        """从配置文件加载回填配置"""
        if config is None:
            config = get_config()
        
        return cls(
            repair_window_hours=config.get(
                "scm.backfill.repair_window_hours", 
                DEFAULT_REPAIR_WINDOW_HOURS
            ),
            cron_hint=config.get(
                "scm.backfill.cron_hint",
                DEFAULT_CRON_HINT
            ),
            max_concurrent_jobs=config.get(
                "scm.backfill.max_concurrent_jobs",
                DEFAULT_MAX_CONCURRENT_JOBS
            ),
            default_update_watermark=config.get(
                "scm.backfill.default_update_watermark",
                DEFAULT_UPDATE_WATERMARK
            ),
        )


@dataclass
class IncrementalConfig:
    """增量同步配置"""
    loop: bool = False
    loop_interval_seconds: int = DEFAULT_LOOP_INTERVAL_SECONDS
    max_iterations: int = DEFAULT_MAX_LOOP_ITERATIONS  # 0 表示无限
    
    @classmethod
    def from_config(cls, config: Optional[Config] = None) -> "IncrementalConfig":
        """从配置文件加载增量同步配置"""
        if config is None:
            config = get_config()
        
        return cls(
            loop_interval_seconds=config.get(
                "scm.incremental.loop_interval_seconds",
                DEFAULT_LOOP_INTERVAL_SECONDS
            ),
            max_iterations=config.get(
                "scm.incremental.max_iterations",
                DEFAULT_MAX_LOOP_ITERATIONS
            ),
        )


@dataclass
class SyncResult:
    """同步结果（JSON 输出结构）"""
    phase: str                        # incremental, backfill
    repo: str                         # 仓库规格
    job: Optional[str] = None         # 任务类型
    status: str = RunnerStatus.SUCCESS.value
    started_at: Optional[str] = None  # ISO8601
    finished_at: Optional[str] = None # ISO8601
    duration_seconds: float = 0.0
    items_synced: int = 0
    items_skipped: int = 0
    items_failed: int = 0
    watermark_before: Optional[str] = None
    watermark_after: Optional[str] = None
    watermark_updated: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # 物化视图刷新结果
    vfacts_refreshed: bool = False
    vfacts_refresh_info: Optional[Dict[str, Any]] = None
    # 扩展信息
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_json(self, indent: int = 2) -> str:
        """转换为 JSON 字符串"""
        return json.dumps(asdict(self), indent=indent, ensure_ascii=False)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)


@dataclass
class RunnerContext:
    """运行器上下文"""
    config: Config
    repo: RepoSpec
    job: Optional[JobSpec] = None
    dry_run: bool = False
    verbose: bool = False
    config_path: Optional[str] = None
    
    # 回填特定配置
    backfill_hours: Optional[int] = None
    backfill_days: Optional[int] = None
    update_watermark: bool = False
    
    # 窗口切分配置
    window_chunk_hours: int = DEFAULT_WINDOW_CHUNK_HOURS  # GitLab 时间窗口块小时数
    window_chunk_revs: int = DEFAULT_WINDOW_CHUNK_REVS    # SVN revision 窗口块大小
    
    # SVN 回填特定配置
    start_rev: Optional[int] = None
    end_rev: Optional[int] = None
    
    # 增量同步特定配置
    loop: bool = False
    loop_interval_seconds: int = DEFAULT_LOOP_INTERVAL_SECONDS
    max_iterations: int = DEFAULT_MAX_LOOP_ITERATIONS
    
    # 物化视图刷新配置
    auto_refresh_vfacts: bool = True  # 默认同步成功后自动刷新
    refresh_concurrently: bool = False  # 默认不使用 CONCURRENTLY


class SyncRunnerError(EngramError):
    """同步运行器错误"""
    exit_code = 30
    error_type = "SYNC_RUNNER_ERROR"


class WatermarkConstraintError(SyncRunnerError):
    """Watermark 约束错误（禁止回退）"""
    exit_code = 31
    error_type = "WATERMARK_CONSTRAINT_ERROR"


# === 辅助函数 ===

def get_script_path(repo_type: str, job_type: str) -> str:
    """
    获取同步脚本路径
    
    Args:
        repo_type: 仓库类型 (gitlab, svn)
        job_type: 任务类型 (commits, mrs, reviews)
    
    Returns:
        脚本文件名
    """
    script_dir = Path(__file__).parent
    
    if repo_type == REPO_TYPE_GITLAB:
        if job_type == JOB_TYPE_COMMITS:
            return str(script_dir / "scm_sync_gitlab_commits.py")
        elif job_type == JOB_TYPE_MRS:
            return str(script_dir / "scm_sync_gitlab_mrs.py")
        elif job_type == JOB_TYPE_REVIEWS:
            return str(script_dir / "scm_sync_gitlab_reviews.py")
    elif repo_type == REPO_TYPE_SVN:
        if job_type == JOB_TYPE_COMMITS:
            return str(script_dir / "scm_sync_svn.py")
    
    raise ValueError(f"不支持的仓库/任务组合: {repo_type}/{job_type}")


def build_sync_command(
    ctx: RunnerContext,
    phase: RunnerPhase,
    since_time: Optional[datetime] = None,
    until_time: Optional[datetime] = None,
    start_rev: Optional[int] = None,
    end_rev: Optional[int] = None,
) -> List[str]:
    """
    构建同步命令
    
    Args:
        ctx: 运行器上下文
        phase: 运行阶段
        since_time: 开始时间（GitLab 回填模式）
        until_time: 结束时间（GitLab 回填模式）
        start_rev: 起始 revision（SVN 回填模式）
        end_rev: 结束 revision（SVN 回填模式）
    
    Returns:
        命令参数列表
    """
    job_type = ctx.job.job_type if ctx.job else JOB_TYPE_COMMITS
    script_path = get_script_path(ctx.repo.repo_type, job_type)
    
    cmd = [sys.executable, script_path]
    
    # 配置文件参数
    if ctx.config_path:
        cmd.extend(["--config", ctx.config_path])
    
    # verbose 模式
    if ctx.verbose:
        cmd.append("--verbose")
    
    # dry-run 模式
    if ctx.dry_run:
        cmd.append("--dry-run")
    
    # 回填模式参数
    if phase == RunnerPhase.BACKFILL:
        if ctx.repo.repo_type == REPO_TYPE_SVN:
            # SVN 使用 start_rev/end_rev 参数
            cmd.append("--backfill")
            if start_rev is not None:
                cmd.extend(["--start-rev", str(start_rev)])
            if end_rev is not None:
                cmd.extend(["--end-rev", str(end_rev)])
            # SVN backfill 模式的 watermark 控制
            if ctx.update_watermark:
                cmd.append("--update-watermark")
        else:
            # GitLab 使用 since/until 参数
            if since_time:
                cmd.extend(["--since", since_time.isoformat()])
            if until_time:
                cmd.extend(["--until", until_time.isoformat()])
            
            # 回填模式下，如果不更新 watermark，需要添加 --no-update-cursor 参数
            if not ctx.update_watermark:
                cmd.append("--no-update-cursor")
    
    return cmd


def validate_watermark_constraint(
    watermark_before: Optional[str],
    watermark_after: Optional[str],
    update_watermark: bool,
) -> None:
    """
    验证 watermark 约束：禁止回退
    
    Args:
        watermark_before: 同步前的 watermark
        watermark_after: 同步后的 watermark
        update_watermark: 是否允许更新 watermark
    
    Raises:
        WatermarkConstraintError: 如果 watermark 发生回退
    """
    if not update_watermark:
        # 不更新 watermark，无需验证
        return
    
    if watermark_before is None or watermark_after is None:
        # 缺少 watermark 信息，跳过验证
        return
    
    try:
        before_dt = datetime.fromisoformat(watermark_before.replace("Z", "+00:00"))
        after_dt = datetime.fromisoformat(watermark_after.replace("Z", "+00:00"))
        
        if after_dt < before_dt:
            raise WatermarkConstraintError(
                f"Watermark 回退被禁止: {watermark_before} -> {watermark_after}",
                {
                    "watermark_before": watermark_before,
                    "watermark_after": watermark_after,
                },
            )
    except ValueError as e:
        logger.warning("Watermark 时间戳解析失败: %s", e)


def refresh_vfacts(
    config: Optional[Config] = None,
    concurrently: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    刷新 scm.v_facts 物化视图
    
    Args:
        config: 配置对象（可选）
        concurrently: 是否使用 CONCURRENTLY 模式（需要唯一索引）
        dry_run: 是否为试运行模式
    
    Returns:
        刷新结果字典，包含:
        - refreshed: 是否已刷新
        - concurrently: 是否使用 CONCURRENTLY
        - before_row_count: 刷新前行数
        - after_row_count: 刷新后行数
        - duration_ms: 刷新耗时（毫秒）
        - refreshed_at: 刷新完成时间
        - error: 错误信息（如果有）
    """
    result = {
        "refreshed": False,
        "concurrently": concurrently,
        "dry_run": dry_run,
    }
    
    if dry_run:
        logger.info("[DRY-RUN] 将刷新 scm.v_facts (concurrently=%s)", concurrently)
        return result
    
    try:
        conn = get_connection(config=config)
        try:
            with conn.cursor() as cur:
                # 获取刷新前的行数
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                before_row = cur.fetchone()
                result["before_row_count"] = before_row[0] if before_row else 0
                
                # 执行刷新
                start_time = time.time()
                
                if concurrently:
                    cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY scm.v_facts")
                else:
                    cur.execute("REFRESH MATERIALIZED VIEW scm.v_facts")
                
                conn.commit()
                
                end_time = time.time()
                duration_ms = (end_time - start_time) * 1000
                
                # 获取刷新后的行数
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                after_row = cur.fetchone()
                result["after_row_count"] = after_row[0] if after_row else 0
                
                result["refreshed"] = True
                result["duration_ms"] = round(duration_ms, 2)
                result["refreshed_at"] = datetime.now(timezone.utc).isoformat()
                
                logger.info(
                    "已刷新 scm.v_facts: before=%d, after=%d, duration=%.2fms",
                    result["before_row_count"],
                    result["after_row_count"],
                    result["duration_ms"],
                )
                
        finally:
            conn.close()
            
    except Exception as e:
        logger.error("刷新 scm.v_facts 失败: %s", e)
        result["error"] = str(e)
    
    return result


def calculate_backfill_window(
    hours: Optional[int] = None,
    days: Optional[int] = None,
    config: Optional[BackfillConfig] = None,
) -> tuple:
    """
    计算回填时间窗口
    
    Args:
        hours: 回填小时数
        days: 回填天数
        config: 回填配置
    
    Returns:
        (since_time, until_time) 元组
    """
    now = datetime.now(timezone.utc)
    
    if hours is not None:
        delta = timedelta(hours=hours)
    elif days is not None:
        delta = timedelta(days=days)
    elif config is not None:
        delta = timedelta(hours=config.repair_window_hours)
    else:
        delta = timedelta(hours=DEFAULT_REPAIR_WINDOW_HOURS)
    
    since_time = now - delta
    until_time = now
    
    return since_time, until_time


@dataclass
class TimeWindowChunk:
    """时间窗口块（用于 GitLab commits/mrs/reviews）"""
    since: datetime
    until: datetime
    index: int  # 窗口索引（从 0 开始）
    total: int  # 总窗口数
    
    def __str__(self) -> str:
        return f"[{self.index + 1}/{self.total}] {self.since.isoformat()} ~ {self.until.isoformat()}"


@dataclass  
class RevisionWindowChunk:
    """Revision 窗口块（用于 SVN）"""
    start_rev: int
    end_rev: int
    index: int  # 窗口索引（从 0 开始）
    total: int  # 总窗口数
    
    def __str__(self) -> str:
        return f"[{self.index + 1}/{self.total}] r{self.start_rev} ~ r{self.end_rev}"


def split_time_window(
    since: datetime,
    until: datetime,
    chunk_hours: int = DEFAULT_WINDOW_CHUNK_HOURS,
) -> List[TimeWindowChunk]:
    """
    将时间范围切分为多个窗口块
    
    确保窗口切分：
    - 不漏：所有时间点都被覆盖
    - 不重：窗口边界不重叠（使用 [since, until) 左闭右开）
    
    Args:
        since: 开始时间
        until: 结束时间
        chunk_hours: 每个窗口块的小时数
    
    Returns:
        TimeWindowChunk 列表（按时间顺序）
    """
    if since >= until:
        return []
    
    chunks = []
    total_seconds = (until - since).total_seconds()
    chunk_seconds = chunk_hours * 3600
    
    # 计算总窗口数
    total_chunks = max(1, int((total_seconds + chunk_seconds - 1) // chunk_seconds))
    
    current_since = since
    for i in range(total_chunks):
        # 计算当前块的 until
        current_until = min(current_since + timedelta(seconds=chunk_seconds), until)
        
        chunks.append(TimeWindowChunk(
            since=current_since,
            until=current_until,
            index=i,
            total=total_chunks,
        ))
        
        current_since = current_until
        
        # 如果已经到达 until，退出
        if current_since >= until:
            break
    
    return chunks


def split_revision_window(
    start_rev: int,
    end_rev: int,
    chunk_size: int = DEFAULT_WINDOW_CHUNK_REVS,
) -> List[RevisionWindowChunk]:
    """
    将 revision 范围切分为多个窗口块
    
    确保窗口切分：
    - 不漏：所有 revision 都被覆盖
    - 不重：窗口边界不重叠（使用 [start, end] 闭区间，下一窗口 start = 前窗口 end + 1）
    
    Args:
        start_rev: 起始 revision（包含）
        end_rev: 结束 revision（包含）
        chunk_size: 每个窗口块的 revision 数
    
    Returns:
        RevisionWindowChunk 列表（按 revision 顺序）
    """
    if start_rev > end_rev:
        return []
    
    chunks = []
    total_revs = end_rev - start_rev + 1
    
    # 计算总窗口数
    total_chunks = max(1, (total_revs + chunk_size - 1) // chunk_size)
    
    current_start = start_rev
    for i in range(total_chunks):
        # 计算当前块的 end
        current_end = min(current_start + chunk_size - 1, end_rev)
        
        chunks.append(RevisionWindowChunk(
            start_rev=current_start,
            end_rev=current_end,
            index=i,
            total=total_chunks,
        ))
        
        current_start = current_end + 1
        
        # 如果已经超过 end_rev，退出
        if current_start > end_rev:
            break
    
    return chunks


# === 运行器核心逻辑 ===

class SyncRunner:
    """SCM 同步运行器"""
    
    def __init__(self, ctx: RunnerContext):
        self.ctx = ctx
        self.backfill_config = BackfillConfig.from_config(ctx.config)
        self.incremental_config = IncrementalConfig.from_config(ctx.config)
        self._shutdown_requested = False
        
        # 注册信号处理
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
    
    def _handle_shutdown(self, signum, frame):
        """处理关闭信号"""
        logger.info("收到关闭信号 (%s)，准备优雅退出...", signum)
        self._shutdown_requested = True
    
    def run_incremental(self) -> SyncResult:
        """
        执行增量同步
        
        Returns:
            SyncResult 同步结果
        """
        result = SyncResult(
            phase=RunnerPhase.INCREMENTAL.value,
            repo=str(self.ctx.repo),
            job=str(self.ctx.job) if self.ctx.job else None,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        
        start_time = time.time()
        iteration = 0
        max_iterations = self.ctx.max_iterations or self.incremental_config.max_iterations
        
        try:
            while not self._shutdown_requested:
                iteration += 1
                logger.info(
                    "增量同步 [%s] 第 %d 次迭代",
                    self.ctx.repo, iteration
                )
                
                # 执行一次同步
                iter_result = self._run_sync_once(RunnerPhase.INCREMENTAL)
                
                # 累计结果
                result.items_synced += iter_result.get("items_synced", 0)
                result.items_skipped += iter_result.get("items_skipped", 0)
                result.items_failed += iter_result.get("items_failed", 0)
                
                if iter_result.get("errors"):
                    result.errors.extend(iter_result["errors"])
                if iter_result.get("warnings"):
                    result.warnings.extend(iter_result["warnings"])
                
                # 检查是否因锁被跳过
                if iter_result.get("locked"):
                    result.status = RunnerStatus.LOCKED.value
                    result.warnings.append("锁被其他 worker 持有，本次迭代跳过")
                    # 如果是单次运行（非循环模式），直接退出
                    if not self.ctx.loop:
                        break
                    # 循环模式下，等待一段时间后重试
                    continue
                
                # 检查是否需要退出循环
                if not self.ctx.loop:
                    break
                
                if max_iterations > 0 and iteration >= max_iterations:
                    logger.info("已达到最大迭代次数 (%d)，退出循环", max_iterations)
                    break
                
                # 等待下一次迭代
                interval = self.ctx.loop_interval_seconds or self.incremental_config.loop_interval_seconds
                logger.debug("等待 %d 秒后进行下一次同步...", interval)
                
                # 分段等待，以便响应关闭信号
                for _ in range(interval):
                    if self._shutdown_requested:
                        break
                    time.sleep(1)
            
            if self._shutdown_requested:
                result.status = RunnerStatus.CANCELLED.value
                result.warnings.append("同步被取消")
            elif result.status == RunnerStatus.LOCKED.value:
                # 保持 LOCKED 状态
                pass
            elif result.items_failed > 0:
                result.status = RunnerStatus.PARTIAL.value
            else:
                result.status = RunnerStatus.SUCCESS.value
                
                # 同步成功后刷新物化视图
                if self.ctx.auto_refresh_vfacts and result.items_synced > 0:
                    refresh_result = self._refresh_vfacts_if_needed()
                    result.vfacts_refreshed = refresh_result.get("refreshed", False)
                    result.vfacts_refresh_info = refresh_result
        
        except Exception as e:
            logger.exception("增量同步失败: %s", e)
            result.status = RunnerStatus.FAILED.value
            result.errors.append(str(e))
        
        finally:
            result.finished_at = datetime.now(timezone.utc).isoformat()
            result.duration_seconds = round(time.time() - start_time, 2)
            result.metadata["iterations"] = iteration
        
        return result
    
    def _refresh_vfacts_if_needed(self) -> Dict[str, Any]:
        """
        在同步成功后刷新 scm.v_facts 物化视图
        
        Returns:
            刷新结果字典
        """
        logger.info("同步成功，刷新 scm.v_facts 物化视图...")
        return refresh_vfacts(
            config=self.ctx.config,
            concurrently=self.ctx.refresh_concurrently,
            dry_run=self.ctx.dry_run,
        )
    
    def run_backfill(self) -> SyncResult:
        """
        执行回填同步
        
        支持窗口切分：大范围回填自动拆分为多个小窗口块。
        
        Returns:
            SyncResult 同步结果
        """
        result = SyncResult(
            phase=RunnerPhase.BACKFILL.value,
            repo=str(self.ctx.repo),
            job=str(self.ctx.job) if self.ctx.job else None,
            started_at=datetime.now(timezone.utc).isoformat(),
            watermark_updated=self.ctx.update_watermark,
        )
        
        start_time = time.time()
        
        try:
            if self.ctx.repo.repo_type == REPO_TYPE_SVN:
                # SVN 回填：使用 start_rev/end_rev
                chunks_result = self._run_backfill_svn_chunks()
            else:
                # GitLab 回填：使用 since/until 时间窗口
                chunks_result = self._run_backfill_time_chunks()
            
            # 汇总窗口结果
            result.items_synced = chunks_result.get("items_synced", 0)
            result.items_skipped = chunks_result.get("items_skipped", 0)
            result.items_failed = chunks_result.get("items_failed", 0)
            result.watermark_before = chunks_result.get("watermark_before")
            result.watermark_after = chunks_result.get("watermark_after")
            result.metadata = chunks_result.get("metadata", {})
            
            if chunks_result.get("errors"):
                result.errors.extend(chunks_result["errors"])
            if chunks_result.get("warnings"):
                result.warnings.extend(chunks_result["warnings"])
            
            # 检查是否因锁被跳过
            if chunks_result.get("locked"):
                result.status = RunnerStatus.LOCKED.value
                result.warnings.append("锁被其他 worker 持有，回填跳过")
                return result
            
            # 验证 watermark 约束（仅在 update_watermark=True 时）
            if self.ctx.update_watermark:
                try:
                    validate_watermark_constraint(
                        result.watermark_before,
                        result.watermark_after,
                        self.ctx.update_watermark,
                    )
                except WatermarkConstraintError as e:
                    result.errors.append(str(e))
                    result.status = RunnerStatus.FAILED.value
                    raise
            
            if result.items_failed > 0:
                result.status = RunnerStatus.PARTIAL.value
            else:
                result.status = RunnerStatus.SUCCESS.value
                
                # 同步成功后刷新物化视图
                if self.ctx.auto_refresh_vfacts and result.items_synced > 0:
                    refresh_result = self._refresh_vfacts_if_needed()
                    result.vfacts_refreshed = refresh_result.get("refreshed", False)
                    result.vfacts_refresh_info = refresh_result
        
        except WatermarkConstraintError:
            raise
        except Exception as e:
            logger.exception("回填同步失败: %s", e)
            result.status = RunnerStatus.FAILED.value
            result.errors.append(str(e))
        
        finally:
            result.finished_at = datetime.now(timezone.utc).isoformat()
            result.duration_seconds = round(time.time() - start_time, 2)
        
        return result
    
    def _run_backfill_time_chunks(self) -> Dict[str, Any]:
        """
        执行 GitLab 时间窗口切分回填
        
        Returns:
            汇总的同步结果
        """
        # 计算回填时间窗口
        since_time, until_time = calculate_backfill_window(
            hours=self.ctx.backfill_hours,
            days=self.ctx.backfill_days,
            config=self.backfill_config,
        )
        
        # 切分时间窗口
        chunks = split_time_window(
            since_time,
            until_time,
            chunk_hours=self.ctx.window_chunk_hours,
        )
        
        logger.info(
            "回填同步 [%s] 时间范围: %s -> %s, 切分为 %d 个窗口 (update_watermark=%s)",
            self.ctx.repo,
            since_time.isoformat(),
            until_time.isoformat(),
            len(chunks),
            self.ctx.update_watermark,
        )
        
        # 汇总结果
        total_result = {
            "items_synced": 0,
            "items_skipped": 0,
            "items_failed": 0,
            "errors": [],
            "warnings": [],
            "metadata": {
                "since_time": since_time.isoformat(),
                "until_time": until_time.isoformat(),
                "update_watermark": self.ctx.update_watermark,
                "chunk_count": len(chunks),
                "chunk_hours": self.ctx.window_chunk_hours,
            },
        }
        
        watermark_before = None
        watermark_after = None
        
        for chunk in chunks:
            if self._shutdown_requested:
                total_result["warnings"].append(f"回填在窗口 {chunk} 处被取消")
                break
            
            logger.info("处理窗口 %s", chunk)
            
            # 执行单个窗口的同步
            sync_result = self._run_sync_once(
                RunnerPhase.BACKFILL,
                since_time=chunk.since,
                until_time=chunk.until,
            )
            
            # 累计结果
            total_result["items_synced"] += sync_result.get("items_synced", 0)
            total_result["items_skipped"] += sync_result.get("items_skipped", 0)
            total_result["items_failed"] += sync_result.get("items_failed", 0)
            
            if sync_result.get("errors"):
                total_result["errors"].extend(sync_result["errors"])
            if sync_result.get("warnings"):
                total_result["warnings"].extend(sync_result["warnings"])
            
            # 跟踪 watermark
            if watermark_before is None:
                watermark_before = sync_result.get("watermark_before")
            watermark_after = sync_result.get("watermark_after")
            
            # 如果某个窗口被锁，标记并跳过
            if sync_result.get("locked"):
                total_result["locked"] = True
                total_result["warnings"].append(f"窗口 {chunk} 因锁被跳过")
        
        total_result["watermark_before"] = watermark_before
        total_result["watermark_after"] = watermark_after
        
        return total_result
    
    def _run_backfill_svn_chunks(self) -> Dict[str, Any]:
        """
        执行 SVN revision 窗口切分回填
        
        Returns:
            汇总的同步结果
        """
        start_rev = self.ctx.start_rev or 1
        end_rev = self.ctx.end_rev  # 可能为 None，表示 HEAD
        
        # 如果 end_rev 未指定，需要获取 HEAD（这里假设子脚本会处理）
        if end_rev is None:
            # 使用一个较大的值，让子脚本自己确定 HEAD
            logger.info("SVN end_rev 未指定，将由子脚本确定 HEAD")
            # 不切分，直接执行单个任务
            chunks = [RevisionWindowChunk(
                start_rev=start_rev,
                end_rev=0,  # 特殊标记，表示使用 HEAD
                index=0,
                total=1,
            )]
        else:
            # 切分 revision 窗口
            chunks = split_revision_window(
                start_rev,
                end_rev,
                chunk_size=self.ctx.window_chunk_revs,
            )
        
        logger.info(
            "回填同步 [%s] revision 范围: r%s -> r%s, 切分为 %d 个窗口 (update_watermark=%s)",
            self.ctx.repo,
            start_rev,
            end_rev or "HEAD",
            len(chunks),
            self.ctx.update_watermark,
        )
        
        # 汇总结果
        total_result = {
            "items_synced": 0,
            "items_skipped": 0,
            "items_failed": 0,
            "errors": [],
            "warnings": [],
            "metadata": {
                "start_rev": start_rev,
                "end_rev": end_rev,
                "update_watermark": self.ctx.update_watermark,
                "chunk_count": len(chunks),
                "chunk_revs": self.ctx.window_chunk_revs,
            },
        }
        
        watermark_before = None
        watermark_after = None
        
        for chunk in chunks:
            if self._shutdown_requested:
                total_result["warnings"].append(f"回填在窗口 {chunk} 处被取消")
                break
            
            logger.info("处理窗口 %s", chunk)
            
            # 执行单个窗口的同步
            sync_result = self._run_sync_once(
                RunnerPhase.BACKFILL,
                start_rev=chunk.start_rev,
                end_rev=chunk.end_rev if chunk.end_rev > 0 else None,
            )
            
            # 累计结果
            total_result["items_synced"] += sync_result.get("items_synced", 0)
            total_result["items_skipped"] += sync_result.get("items_skipped", 0)
            total_result["items_failed"] += sync_result.get("items_failed", 0)
            
            if sync_result.get("errors"):
                total_result["errors"].extend(sync_result["errors"])
            if sync_result.get("warnings"):
                total_result["warnings"].extend(sync_result["warnings"])
            
            # 跟踪 watermark
            if watermark_before is None:
                watermark_before = sync_result.get("watermark_before")
            watermark_after = sync_result.get("watermark_after")
            
            # 如果某个窗口被锁，标记并跳过
            if sync_result.get("locked"):
                total_result["locked"] = True
                total_result["warnings"].append(f"窗口 {chunk} 因锁被跳过")
        
        total_result["watermark_before"] = watermark_before
        total_result["watermark_after"] = watermark_after
        
        return total_result
    
    def _run_sync_once(
        self,
        phase: RunnerPhase,
        since_time: Optional[datetime] = None,
        until_time: Optional[datetime] = None,
        start_rev: Optional[int] = None,
        end_rev: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        执行一次同步
        
        Args:
            phase: 运行阶段
            since_time: 开始时间（GitLab 回填模式）
            until_time: 结束时间（GitLab 回填模式）
            start_rev: 起始 revision（SVN 回填模式）
            end_rev: 结束 revision（SVN 回填模式）
        
        Returns:
            同步结果字典
        """
        cmd = build_sync_command(
            self.ctx,
            phase,
            since_time=since_time,
            until_time=until_time,
            start_rev=start_rev,
            end_rev=end_rev,
        )
        
        logger.debug("执行命令: %s", " ".join(cmd))
        
        if self.ctx.dry_run:
            logger.info("[DRY-RUN] 将执行: %s", " ".join(cmd))
            return {
                "items_synced": 0,
                "items_skipped": 0,
                "items_failed": 0,
                "dry_run": True,
            }
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 小时超时
            )
            
            # 解析输出（假设子脚本输出 JSON 结果）
            output = result.stdout.strip()
            if output:
                try:
                    parsed_result = json.loads(output)
                    # 检查是否因锁被跳过
                    if parsed_result.get("locked") or parsed_result.get("skipped"):
                        logger.info(
                            "子脚本因锁被跳过: %s",
                            parsed_result.get("message", "锁被其他 worker 持有")
                        )
                        return {
                            "items_synced": 0,
                            "items_skipped": 1,
                            "items_failed": 0,
                            "locked": True,
                            "skipped": True,
                            "message": parsed_result.get("message"),
                        }
                    return parsed_result
                except json.JSONDecodeError:
                    logger.debug("子脚本输出非 JSON: %s", output[:200])
            
            if result.returncode != 0:
                logger.warning(
                    "子脚本退出码非零: %d, stderr: %s",
                    result.returncode,
                    result.stderr[:500] if result.stderr else "(empty)"
                )
                return {
                    "items_synced": 0,
                    "items_failed": 1,
                    "errors": [f"子脚本退出码: {result.returncode}"],
                }
            
            return {"items_synced": 0, "items_skipped": 0, "items_failed": 0}
        
        except subprocess.TimeoutExpired as e:
            logger.error("同步超时: %s", e)
            return {"items_failed": 1, "errors": ["同步超时"]}
        except Exception as e:
            logger.exception("执行同步命令失败: %s", e)
            return {"items_failed": 1, "errors": [str(e)]}


# === CLI 入口 ===

def create_parser() -> argparse.ArgumentParser:
    """创建命令行解析器"""
    parser = argparse.ArgumentParser(
        prog="scm_sync_runner",
        description="SCM 同步运行器 - 支持增量同步和回填两阶段",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 单次增量同步
    %(prog)s incremental --repo gitlab:123

    # 持续增量同步（循环模式）
    %(prog)s incremental --repo gitlab:123 --loop

    # 回填最近 24 小时
    %(prog)s backfill --repo gitlab:123 --last-hours 24

    # 回填最近 7 天并更新 watermark
    %(prog)s backfill --repo gitlab:123 --last-days 7 --update-watermark

    # 显示配置信息
    %(prog)s config --show-backfill
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
        "--dry-run",
        action="store_true",
        help="试运行模式，不执行实际同步",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="输出 JSON 格式结果",
    )
    parser.add_argument(
        "--no-refresh-vfacts",
        action="store_true",
        help="同步成功后不自动刷新 scm.v_facts 物化视图",
    )
    parser.add_argument(
        "--refresh-concurrently",
        action="store_true",
        help="使用 CONCURRENTLY 模式刷新物化视图（需要唯一索引）",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # === incremental 子命令 ===
    incr_parser = subparsers.add_parser(
        "incremental",
        help="增量同步模式",
        description="执行增量同步，可选择持续循环模式",
    )
    incr_parser.add_argument(
        "--repo", "-r",
        required=True,
        metavar="SPEC",
        help="仓库规格，格式: <type>:<id>，如 gitlab:123",
    )
    incr_parser.add_argument(
        "--job", "-j",
        metavar="TYPE",
        default=JOB_TYPE_COMMITS,
        help=f"任务类型: {', '.join(sorted(VALID_JOB_TYPES))}（默认: {JOB_TYPE_COMMITS}）",
    )
    incr_parser.add_argument(
        "--loop",
        action="store_true",
        help="持续循环模式",
    )
    incr_parser.add_argument(
        "--loop-interval",
        type=int,
        metavar="SECONDS",
        help=f"循环间隔秒数（默认: {DEFAULT_LOOP_INTERVAL_SECONDS}）",
    )
    incr_parser.add_argument(
        "--max-iterations",
        type=int,
        metavar="N",
        help="最大迭代次数（默认: 无限）",
    )
    
    # === backfill 子命令 ===
    bf_parser = subparsers.add_parser(
        "backfill",
        help="回填同步模式",
        description="执行回填同步，默认不更新 watermark",
    )
    bf_parser.add_argument(
        "--repo", "-r",
        required=True,
        metavar="SPEC",
        help="仓库规格，格式: <type>:<id>，如 gitlab:123",
    )
    bf_parser.add_argument(
        "--job", "-j",
        metavar="TYPE",
        default=JOB_TYPE_COMMITS,
        help=f"任务类型: {', '.join(sorted(VALID_JOB_TYPES))}（默认: {JOB_TYPE_COMMITS}）",
    )
    
    # 时间窗口参数（互斥）
    time_group = bf_parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--last-hours",
        type=int,
        metavar="N",
        help="回填最近 N 小时",
    )
    time_group.add_argument(
        "--last-days",
        type=int,
        metavar="N",
        help="回填最近 N 天",
    )
    
    bf_parser.add_argument(
        "--update-watermark",
        action="store_true",
        help="更新 watermark（默认: 不更新）",
    )
    
    # === config 子命令 ===
    cfg_parser = subparsers.add_parser(
        "config",
        help="显示配置信息",
        description="显示当前配置信息",
    )
    cfg_parser.add_argument(
        "--show-backfill",
        action="store_true",
        help="显示回填配置",
    )
    cfg_parser.add_argument(
        "--show-incremental",
        action="store_true",
        help="显示增量同步配置",
    )
    
    return parser


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数"""
    parser = create_parser()
    return parser.parse_args(args)


def run_config_command(args: argparse.Namespace, config: Config) -> int:
    """执行 config 子命令"""
    result = {}
    
    if args.show_backfill or (not args.show_backfill and not args.show_incremental):
        bf_config = BackfillConfig.from_config(config)
        result["backfill"] = asdict(bf_config)
    
    if args.show_incremental or (not args.show_backfill and not args.show_incremental):
        incr_config = IncrementalConfig.from_config(config)
        result["incremental"] = asdict(incr_config)
    
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(args: Optional[List[str]] = None) -> int:
    """主入口"""
    parsed = parse_args(args)
    
    if not parsed.command:
        print("错误: 请指定子命令 (incremental, backfill, config)", file=sys.stderr)
        print("使用 --help 查看帮助", file=sys.stderr)
        return 1
    
    # 设置日志级别
    if parsed.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 加载配置
    try:
        config = get_config(getattr(parsed, "config_path", None), reload=True)
        config.load()
    except ConfigError as e:
        logger.error("配置加载失败: %s", e)
        return 1
    
    # 处理 config 子命令
    if parsed.command == "config":
        return run_config_command(parsed, config)
    
    # 解析仓库和任务规格
    try:
        repo = RepoSpec.parse(parsed.repo)
        job = JobSpec.parse(parsed.job) if parsed.job else None
    except ValueError as e:
        logger.error("参数解析失败: %s", e)
        return 1
    
    # 构建运行器上下文
    ctx = RunnerContext(
        config=config,
        repo=repo,
        job=job,
        dry_run=parsed.dry_run,
        verbose=parsed.verbose,
        config_path=getattr(parsed, "config_path", None),
    )
    
    # 设置物化视图刷新参数
    if getattr(parsed, "no_refresh_vfacts", False):
        ctx.auto_refresh_vfacts = False
    if getattr(parsed, "refresh_concurrently", False):
        ctx.refresh_concurrently = True
    
    # 设置特定参数
    if parsed.command == "incremental":
        ctx.loop = parsed.loop
        if parsed.loop_interval:
            ctx.loop_interval_seconds = parsed.loop_interval
        if parsed.max_iterations:
            ctx.max_iterations = parsed.max_iterations
    
    elif parsed.command == "backfill":
        ctx.backfill_hours = parsed.last_hours
        ctx.backfill_days = parsed.last_days
        ctx.update_watermark = parsed.update_watermark
    
    # 创建运行器并执行
    runner = SyncRunner(ctx)
    
    try:
        if parsed.command == "incremental":
            result = runner.run_incremental()
        elif parsed.command == "backfill":
            result = runner.run_backfill()
        else:
            logger.error("未知命令: %s", parsed.command)
            return 1
        
        # 输出结果
        if getattr(parsed, "json_output", False):
            print(result.to_json())
        else:
            # 简洁输出
            logger.info(
                "同步完成 [%s]: status=%s, synced=%d, skipped=%d, failed=%d, duration=%.2fs",
                result.repo,
                result.status,
                result.items_synced,
                result.items_skipped,
                result.items_failed,
                result.duration_seconds,
            )
            if result.errors:
                for err in result.errors:
                    logger.error("错误: %s", err)
            if result.warnings:
                for warn in result.warnings:
                    logger.warning("警告: %s", warn)
        
        # 返回退出码
        if result.status == RunnerStatus.SUCCESS.value:
            return 0
        elif result.status == RunnerStatus.LOCKED.value:
            return 75  # EX_TEMPFAIL (临时失败，可重试)
        elif result.status == RunnerStatus.PARTIAL.value:
            return 2
        elif result.status == RunnerStatus.CANCELLED.value:
            return 130  # 标准 SIGINT 退出码
        else:
            return 1
    
    except WatermarkConstraintError as e:
        logger.error("Watermark 约束错误: %s", e)
        return e.exit_code
    except Exception as e:
        logger.exception("运行器异常: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
