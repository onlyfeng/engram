# -*- coding: utf-8 -*-
"""
scm_sync_runner - SCM 同步运行器核心实现

功能:
- 提供增量同步和回填同步的核心逻辑
- 时间窗口/版本窗口分片
- Watermark 约束校验
- vfacts 刷新

设计原则:
- 纯业务逻辑，不包含 argparse/打印
- 所有配置通过参数传入
- CLI 入口在根目录 scm_sync_runner.py

使用示例:
    from engram.logbook.scm_sync_runner import (
        RepoSpec, JobSpec, RunnerContext, SyncRunner,
        split_time_window, split_revision_window,
        refresh_vfacts,
    )

    repo = RepoSpec.parse("gitlab:123")
    ctx = RunnerContext(config=config, repo=repo)
    runner = SyncRunner(ctx)
    result = runner.run_incremental()
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from engram.logbook.config import get_config
from engram.logbook.db import get_connection as _get_db_connection

if TYPE_CHECKING:
    from engram.logbook.scm_sync_executor import SyncExecutor

__all__ = [
    # 常量
    "REPO_TYPE_GITLAB",
    "REPO_TYPE_SVN",
    "VALID_REPO_TYPES",
    "JOB_TYPE_COMMITS",
    "JOB_TYPE_MRS",
    "VALID_JOB_TYPES",
    "DEFAULT_REPAIR_WINDOW_HOURS",
    "DEFAULT_LOOP_INTERVAL_SECONDS",
    "DEFAULT_WINDOW_CHUNK_HOURS",
    "DEFAULT_WINDOW_CHUNK_REVS",
    "EXIT_SUCCESS",
    "EXIT_PARTIAL",
    "EXIT_FAILED",
    # 枚举
    "RunnerStatus",
    "RunnerPhase",
    # 异常
    "WatermarkConstraintError",
    # 数据类
    "RepoSpec",
    "JobSpec",
    "BackfillConfig",
    "IncrementalConfig",
    "RunnerContext",
    "SyncResult",
    "TimeWindowChunk",
    "RevisionWindowChunk",
    "AggregatedResult",
    # 函数
    "split_time_window",
    "split_revision_window",
    "calculate_backfill_window",
    "validate_watermark_constraint",
    "get_script_path",
    "build_sync_command",
    "get_connection",
    "refresh_vfacts",
    "get_exit_code",
    "create_parser",
    "parse_args",
    # 类
    "SyncRunner",
]


# ============ 常量定义 ============

REPO_TYPE_GITLAB = "gitlab"
REPO_TYPE_SVN = "svn"
VALID_REPO_TYPES = {REPO_TYPE_GITLAB, REPO_TYPE_SVN}

JOB_TYPE_COMMITS = "commits"
JOB_TYPE_MRS = "mrs"
VALID_JOB_TYPES = {JOB_TYPE_COMMITS, JOB_TYPE_MRS}

DEFAULT_REPAIR_WINDOW_HOURS = 24
DEFAULT_LOOP_INTERVAL_SECONDS = 60
DEFAULT_WINDOW_CHUNK_HOURS = 4
DEFAULT_WINDOW_CHUNK_REVS = 100


# ============ 枚举定义 ============


class RunnerStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RunnerPhase(str, Enum):
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"


# ============ 异常定义 ============


class WatermarkConstraintError(Exception):
    """Watermark 回退约束错误"""

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


# ============ 数据类定义 ============


@dataclass
class RepoSpec:
    """仓库规格"""

    repo_type: str
    repo_id: str

    @classmethod
    def parse(cls, value: str) -> "RepoSpec":
        """
        解析仓库规格字符串

        Args:
            value: 格式为 <type>:<id>，如 "gitlab:123"

        Returns:
            RepoSpec 实例

        Raises:
            ValueError: 格式错误或不支持的类型
        """
        if ":" not in value:
            raise ValueError("格式应为 <type>:<id>")
        repo_type, repo_id = value.split(":", 1)
        repo_type = repo_type.lower().strip()
        repo_id = repo_id.strip()
        if repo_type not in VALID_REPO_TYPES:
            raise ValueError("不支持的仓库类型")
        if not repo_id:
            raise ValueError("仓库 ID 不能为空")
        return cls(repo_type=repo_type, repo_id=repo_id)

    def __str__(self) -> str:
        return f"{self.repo_type}:{self.repo_id}"


@dataclass
class JobSpec:
    """任务规格"""

    job_type: str

    @classmethod
    def parse(cls, value: str) -> "JobSpec":
        """
        解析任务类型字符串

        Args:
            value: 任务类型，如 "commits"

        Returns:
            JobSpec 实例

        Raises:
            ValueError: 不支持的任务类型
        """
        job_type = value.lower().strip()
        if job_type not in VALID_JOB_TYPES:
            raise ValueError("不支持的任务类型")
        return cls(job_type=job_type)

    def __str__(self) -> str:
        return self.job_type


@dataclass
class BackfillConfig:
    """回填配置"""

    repair_window_hours: int = DEFAULT_REPAIR_WINDOW_HOURS
    cron_hint: str = "0 2 * * *"
    max_concurrent_jobs: int = 4
    default_update_watermark: bool = False

    @classmethod
    def from_config(cls, config=None) -> "BackfillConfig":
        """从配置对象创建"""
        if config is None:
            config = get_config()
        return cls(
            repair_window_hours=int(
                config.get("scm.backfill.repair_window_hours", DEFAULT_REPAIR_WINDOW_HOURS)
            ),
            cron_hint=str(config.get("scm.backfill.cron_hint", "0 2 * * *")),
            max_concurrent_jobs=int(config.get("scm.backfill.max_concurrent_jobs", 4)),
            default_update_watermark=bool(
                config.get("scm.backfill.default_update_watermark", False)
            ),
        )


@dataclass
class IncrementalConfig:
    """增量同步配置"""

    loop: bool = False
    loop_interval_seconds: int = DEFAULT_LOOP_INTERVAL_SECONDS
    max_iterations: int = 0


@dataclass
class RunnerContext:
    """运行器上下文"""

    config: object
    repo: RepoSpec
    job: JobSpec = field(default_factory=lambda: JobSpec(job_type=JOB_TYPE_COMMITS))
    config_path: Optional[str] = None
    verbose: bool = False
    dry_run: bool = False
    update_watermark: bool = False
    window_chunk_hours: int = DEFAULT_WINDOW_CHUNK_HOURS
    window_chunk_revs: int = DEFAULT_WINDOW_CHUNK_REVS
    auto_refresh_vfacts: bool = True
    refresh_concurrently: bool = False


@dataclass
class SyncResult:
    """同步结果"""

    phase: str
    repo: str
    status: str = RunnerStatus.SUCCESS.value
    job: Optional[str] = None
    items_synced: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    vfacts_refreshed: bool = False
    vfacts_refresh_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "repo": self.repo,
            "status": self.status,
            "job": self.job,
            "items_synced": self.items_synced,
            "message": self.message,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "vfacts_refreshed": self.vfacts_refreshed,
            "vfacts_refresh_info": self.vfacts_refresh_info,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class TimeWindowChunk:
    """时间窗口分片"""

    since: datetime
    until: datetime
    index: int
    total: int

    def to_payload(self, *, update_watermark: bool, watermark_constraint: str) -> dict:
        return {
            "window_type": "time",
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "window_since": self.since.isoformat(),
            "window_until": self.until.isoformat(),
            "index": self.index,
            "total": self.total,
            "chunk_index": self.index,
            "chunk_total": self.total,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
        }


@dataclass
class RevisionWindowChunk:
    """版本窗口分片"""

    start_rev: int
    end_rev: int
    index: int
    total: int

    def to_payload(self, *, update_watermark: bool, watermark_constraint: str) -> dict:
        return {
            "window_type": "revision",
            "start_rev": self.start_rev,
            "end_rev": self.end_rev,
            "window_start_rev": self.start_rev,
            "window_end_rev": self.end_rev,
            "index": self.index,
            "total": self.total,
            "chunk_index": self.index,
            "chunk_total": self.total,
            "update_watermark": update_watermark,
            "watermark_constraint": watermark_constraint,
        }


# ============ 窗口分片函数 ============


def split_time_window(
    since: datetime,
    until: datetime,
    *,
    chunk_hours: int = DEFAULT_WINDOW_CHUNK_HOURS,
) -> List[TimeWindowChunk]:
    """
    将时间窗口分割为多个分片

    Args:
        since: 开始时间
        until: 结束时间
        chunk_hours: 每个分片的小时数

    Returns:
        TimeWindowChunk 列表
    """
    if since >= until:
        return []
    chunks: List[TimeWindowChunk] = []
    delta = timedelta(hours=chunk_hours)
    cursor = since
    while cursor < until:
        next_cursor = min(cursor + delta, until)
        chunks.append(TimeWindowChunk(since=cursor, until=next_cursor, index=0, total=0))
        cursor = next_cursor
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        chunk.index = idx
        chunk.total = total
    return chunks


def split_revision_window(
    start_rev: int,
    end_rev: int,
    *,
    chunk_size: int = DEFAULT_WINDOW_CHUNK_REVS,
) -> List[RevisionWindowChunk]:
    """
    将版本窗口分割为多个分片

    Args:
        start_rev: 起始版本号
        end_rev: 结束版本号
        chunk_size: 每个分片的版本数

    Returns:
        RevisionWindowChunk 列表
    """
    if start_rev > end_rev:
        return []
    chunks: List[RevisionWindowChunk] = []
    cursor = start_rev
    while cursor <= end_rev:
        chunk_end = min(cursor + chunk_size - 1, end_rev)
        chunks.append(RevisionWindowChunk(start_rev=cursor, end_rev=chunk_end, index=0, total=0))
        cursor = chunk_end + 1
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        chunk.index = idx
        chunk.total = total
    return chunks


def calculate_backfill_window(
    *,
    hours: Optional[int] = None,
    days: Optional[int] = None,
    config: Optional[BackfillConfig] = None,
) -> Tuple[datetime, datetime]:
    """
    计算回填时间窗口

    Args:
        hours: 回填小时数
        days: 回填天数（优先级高于 hours）
        config: 回填配置

    Returns:
        (since, until) 时间元组
    """
    if config is None:
        config = BackfillConfig()
    if hours is None and days is None:
        hours = config.repair_window_hours
    if days is not None:
        hours = int(days) * 24
    since = datetime.now(timezone.utc) - timedelta(hours=int(hours or 0))
    until = datetime.now(timezone.utc)
    return since, until


def validate_watermark_constraint(
    *,
    watermark_before: Optional[str],
    watermark_after: Optional[str],
    update_watermark: bool = True,
) -> None:
    """
    校验 Watermark 约束（不允许回退）

    Args:
        watermark_before: 同步前的 watermark
        watermark_after: 同步后的 watermark
        update_watermark: 是否更新 watermark

    Raises:
        WatermarkConstraintError: watermark 回退时
    """
    if not update_watermark:
        return
    if watermark_before is None or watermark_after is None:
        return
    before_dt = datetime.fromisoformat(watermark_before.replace("Z", "+00:00"))
    after_dt = datetime.fromisoformat(watermark_after.replace("Z", "+00:00"))
    if after_dt < before_dt:
        raise WatermarkConstraintError(
            "Watermark 回退被禁止",
            details={"watermark_before": watermark_before, "watermark_after": watermark_after},
        )


# ============ 脚本路径与命令构建（已弃用） ============
#
# 注意：以下函数已弃用（deprecated），保留仅为向后兼容。
# 新代码应使用 SyncRunner + SyncExecutor 直接执行同步，无需构建命令行。
#
# 弃用原因：
# 1. SyncRunner 已通过 SyncExecutor 在进程内直接执行同步
# 2. 避免对根目录脚本（如 scm_sync_gitlab_commits.py）的依赖
# 3. 新入口统一使用 `python -m engram.logbook.cli.scm_sync` 形式
#
# 迁移指南：
# - 旧方式: subprocess.run(build_sync_command(ctx, phase, ...))
# - 新方式: SyncRunner(ctx).run_incremental() 或 .run_backfill(...)


import warnings


def get_script_path(repo_type: str, job_type: str) -> str:
    """
    获取同步脚本路径

    .. deprecated::
        此函数已弃用，将在未来版本移除。
        新代码应使用 SyncRunner + SyncExecutor 直接执行同步。

    Args:
        repo_type: 仓库类型
        job_type: 任务类型

    Returns:
        脚本文件路径

    Raises:
        ValueError: 不支持的仓库/任务组合
    """
    warnings.warn(
        "get_script_path() 已弃用，新代码应使用 SyncRunner + SyncExecutor 直接执行同步。"
        "此函数依赖的根目录脚本将被移除。",
        DeprecationWarning,
        stacklevel=2,
    )
    repo_type = repo_type.lower()
    job_type = job_type.lower()
    # 计算项目根目录
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    if repo_type == REPO_TYPE_GITLAB:
        if job_type == JOB_TYPE_COMMITS:
            return str(repo_root / "scm_sync_gitlab_commits.py")
        if job_type == JOB_TYPE_MRS:
            return str(repo_root / "scm_sync_gitlab_mrs.py")
    if repo_type == REPO_TYPE_SVN and job_type == JOB_TYPE_COMMITS:
        return str(repo_root / "scm_sync_svn.py")
    raise ValueError("不支持的仓库/任务组合")


def build_sync_command(
    ctx: RunnerContext,
    phase: RunnerPhase,
    *,
    since_time: Optional[datetime] = None,
    until_time: Optional[datetime] = None,
    start_rev: Optional[int] = None,
    end_rev: Optional[int] = None,
) -> List[str]:
    """
    构建同步命令行

    .. deprecated::
        此函数已弃用，将在未来版本移除。
        新代码应使用 SyncRunner + SyncExecutor 直接执行同步，而非构建命令行。

    Args:
        ctx: 运行器上下文
        phase: 运行阶段
        since_time: 开始时间（回填用）
        until_time: 结束时间（回填用）
        start_rev: 起始版本号（回填用）
        end_rev: 结束版本号（回填用）

    Returns:
        命令行参数列表
    """
    warnings.warn(
        "build_sync_command() 已弃用，新代码应使用 SyncRunner.run_incremental() 或 "
        "SyncRunner.run_backfill() 直接执行同步。此函数依赖的根目录脚本将被移除。",
        DeprecationWarning,
        stacklevel=2,
    )
    import sys

    # 注意：此处仍调用 get_script_path()，但会触发其自身的弃用警告
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        script_path = get_script_path(ctx.repo.repo_type, ctx.job.job_type)

    cmd = [sys.executable, script_path]
    if ctx.config_path:
        cmd += ["--config", ctx.config_path]
    if ctx.verbose:
        cmd.append("--verbose")
    if ctx.dry_run:
        cmd.append("--dry-run")

    if phase == RunnerPhase.INCREMENTAL:
        cmd += ["--repo", str(ctx.repo)]
    else:
        cmd.append("--backfill")
        if start_rev is not None or end_rev is not None:
            if start_rev is not None:
                cmd += ["--start-rev", str(start_rev)]
            if end_rev is not None:
                cmd += ["--end-rev", str(end_rev)]
        if since_time is not None:
            cmd += ["--since", since_time.isoformat()]
        if until_time is not None:
            cmd += ["--until", until_time.isoformat()]
        if ctx.update_watermark:
            cmd.append("--update-watermark")
        else:
            cmd.append("--no-update-cursor")
    return cmd


# ============ 数据库连接 ============


def get_connection():
    """获取数据库连接"""
    dsn = os.environ.get("LOGBOOK_DSN") or os.environ.get("POSTGRES_DSN")
    return _get_db_connection(dsn=dsn)


# ============ vfacts 刷新 ============


def refresh_vfacts(*, dry_run: bool = False, concurrently: bool = False) -> dict:
    """
    刷新物化视图 scm.v_facts

    Args:
        dry_run: 是否模拟运行
        concurrently: 是否并发刷新

    Returns:
        刷新结果字典
    """
    from typing import Any

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "refreshed": False,
        "concurrently": concurrently,
        "before_row_count": 0,
        "after_row_count": 0,
    }
    if dry_run:
        return result
    started = time.monotonic()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                before = cur.fetchone()[0] or 0
                if concurrently:
                    cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY scm.v_facts")
                else:
                    cur.execute("REFRESH MATERIALIZED VIEW scm.v_facts")
                cur.execute("SELECT COUNT(*) FROM scm.v_facts")
                after = cur.fetchone()[0] or 0
            conn.commit()
        finally:
            conn.close()
        duration_ms = (time.monotonic() - started) * 1000.0
        refreshed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result.update(
            {
                "refreshed": True,
                "before_row_count": int(before),
                "after_row_count": int(after),
                "duration_ms": duration_ms,
                "refreshed_at": refreshed_at,
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
    return result


# ============ 聚合结果类 ============


@dataclass
class AggregatedResult:
    """回填聚合结果"""

    phase: str
    repo: str
    job: Optional[str] = None
    status: str = RunnerStatus.SUCCESS.value
    total_chunks: int = 0
    success_chunks: int = 0
    partial_chunks: int = 0
    failed_chunks: int = 0
    total_items_synced: int = 0
    total_items_skipped: int = 0
    total_items_failed: int = 0
    chunk_results: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    vfacts_refreshed: bool = False
    vfacts_refresh_info: Optional[dict] = None
    watermark_updated: bool = False
    watermark_before: Optional[str] = None
    watermark_after: Optional[str] = None

    def compute_status(self) -> str:
        """根据 chunk 结果计算总体状态"""
        if self.total_chunks == 0:
            return RunnerStatus.SKIPPED.value
        if self.failed_chunks == self.total_chunks:
            return RunnerStatus.FAILED.value
        if self.failed_chunks > 0 or self.partial_chunks > 0:
            return RunnerStatus.PARTIAL.value
        return RunnerStatus.SUCCESS.value

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "repo": self.repo,
            "job": self.job,
            "status": self.status,
            "total_chunks": self.total_chunks,
            "success_chunks": self.success_chunks,
            "partial_chunks": self.partial_chunks,
            "failed_chunks": self.failed_chunks,
            "total_items_synced": self.total_items_synced,
            "total_items_skipped": self.total_items_skipped,
            "total_items_failed": self.total_items_failed,
            "chunk_results": self.chunk_results,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "vfacts_refreshed": self.vfacts_refreshed,
            "vfacts_refresh_info": self.vfacts_refresh_info,
            "watermark_updated": self.watermark_updated,
            "watermark_before": self.watermark_before,
            "watermark_after": self.watermark_after,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ============ 返回码常量 ============

EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2


def get_exit_code(status: str) -> int:
    """根据状态获取退出码"""
    if status == RunnerStatus.SUCCESS.value:
        return EXIT_SUCCESS
    if status == RunnerStatus.PARTIAL.value:
        return EXIT_PARTIAL
    return EXIT_FAILED


# ============ SyncRunner 类 ============


class SyncRunner:
    """SCM 同步运行器

    支持两种模式:
    - 增量同步 (incremental): 从 watermark 开始同步新数据
    - 回填同步 (backfill): 按时间/版本窗口分片同步历史数据

    优先使用 SyncExecutor 执行同步，避免 subprocess 调用。
    """

    def __init__(self, ctx: RunnerContext) -> None:
        self.ctx = ctx
        self._executor: Optional["SyncExecutor"] = None

    def _get_executor(self) -> "SyncExecutor":
        """获取或创建 SyncExecutor 实例"""
        if self._executor is None:
            from engram.logbook.scm_sync_executor import get_default_executor

            self._executor = get_default_executor()
        return self._executor

    def _build_job_dict(
        self,
        *,
        mode: str = "incremental",
        payload: Optional[dict] = None,
    ) -> dict:
        """构建 job 字典用于 executor"""
        from engram.logbook.scm_sync_job_types import (
            PhysicalJobType,
        )

        # 映射 repo_type + job_type 到 physical_job_type
        repo_type = self.ctx.repo.repo_type
        job_type = self.ctx.job.job_type

        if repo_type == REPO_TYPE_GITLAB:
            if job_type == JOB_TYPE_COMMITS:
                physical_job_type = PhysicalJobType.GITLAB_COMMITS.value
            elif job_type == JOB_TYPE_MRS:
                physical_job_type = PhysicalJobType.GITLAB_MRS.value
            else:
                physical_job_type = PhysicalJobType.GITLAB_COMMITS.value
        elif repo_type == REPO_TYPE_SVN:
            physical_job_type = PhysicalJobType.SVN.value
        else:
            physical_job_type = PhysicalJobType.GITLAB_COMMITS.value

        # 构建基础 payload
        base_payload = {
            "repo_type": repo_type,
            "repo_id": self.ctx.repo.repo_id,
            "dry_run": self.ctx.dry_run,
            "verbose": self.ctx.verbose,
        }
        if payload:
            base_payload.update(payload)

        return {
            "job_type": physical_job_type,
            "repo_id": int(self.ctx.repo.repo_id) if self.ctx.repo.repo_id.isdigit() else 0,
            "mode": mode,
            "payload": base_payload,
        }

    def _run_sync_once(self, *, mode: str = "incremental", payload: Optional[dict] = None) -> dict:
        """执行一次同步

        Args:
            mode: 同步模式 (incremental/backfill/probe)
            payload: 额外的 payload 参数

        Returns:
            同步结果字典
        """
        if self.ctx.dry_run:
            return {
                "success": True,
                "synced_count": 0,
                "skipped_count": 0,
                "dry_run": True,
                "message": "dry_run mode, no actual sync performed",
            }

        executor = self._get_executor()
        job = self._build_job_dict(mode=mode, payload=payload)

        exec_result = executor.execute(job)
        result_dict: dict = exec_result.to_dict()
        return result_dict

    def run_incremental(self) -> SyncResult:
        """执行增量同步

        Returns:
            SyncResult 对象
        """
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        stats = self._run_sync_once(mode="incremental")

        finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # 确定状态
        if stats.get("success", False):
            status = RunnerStatus.SUCCESS.value
        elif stats.get("error"):
            status = RunnerStatus.FAILED.value
        else:
            status = RunnerStatus.PARTIAL.value

        result = SyncResult(
            phase=RunnerPhase.INCREMENTAL.value,
            repo=str(self.ctx.repo),
            job=str(self.ctx.job),
            status=status,
            items_synced=int(stats.get("synced_count", 0) or 0),
            message=stats.get("message"),
            error=stats.get("error"),
            started_at=started_at,
            finished_at=finished_at,
        )

        # 自动刷新 vfacts
        if self.ctx.auto_refresh_vfacts and result.items_synced > 0:
            info = refresh_vfacts(
                dry_run=self.ctx.dry_run,
                concurrently=self.ctx.refresh_concurrently,
            )
            result.vfacts_refreshed = bool(info.get("refreshed"))
            result.vfacts_refresh_info = info

        return result

    def run_backfill(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        start_rev: Optional[int] = None,
        end_rev: Optional[int] = None,
    ) -> AggregatedResult:
        """执行回填同步

        回填模式会将时间/版本窗口分片为多个 chunk，逐个执行同步并聚合结果。

        Args:
            since: 开始时间（时间窗口回填）
            until: 结束时间（时间窗口回填）
            start_rev: 起始版本号（SVN 版本窗口回填）
            end_rev: 结束版本号（SVN 版本窗口回填）

        Returns:
            AggregatedResult 对象
        """
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        aggregated = AggregatedResult(
            phase=RunnerPhase.BACKFILL.value,
            repo=str(self.ctx.repo),
            job=str(self.ctx.job),
            started_at=started_at,
        )

        # 确定 chunk 类型
        # 使用 Sequence 而不是 List，因为 Sequence 是协变的
        from typing import Sequence, Union

        chunks: Sequence[Union[TimeWindowChunk, RevisionWindowChunk]]
        if start_rev is not None or end_rev is not None:
            # SVN revision 窗口
            chunks = self._generate_revision_chunks(start_rev, end_rev)
        elif since is not None or until is not None:
            # 时间窗口
            chunks = self._generate_time_chunks(since, until)
        else:
            # 默认使用配置中的回填窗口
            bf_config = BackfillConfig.from_config(self.ctx.config)
            since, until = calculate_backfill_window(config=bf_config)
            chunks = self._generate_time_chunks(since, until)

        aggregated.total_chunks = len(chunks)

        if aggregated.total_chunks == 0:
            aggregated.status = RunnerStatus.SKIPPED.value
            aggregated.finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return aggregated

        # 确定 watermark 约束
        watermark_constraint = "monotonic" if self.ctx.update_watermark else "none"

        # 逐个执行 chunk
        for chunk in chunks:
            chunk_result = self._execute_chunk(chunk, watermark_constraint)
            aggregated.chunk_results.append(chunk_result)

            # 统计
            chunk_status = chunk_result.get("status", "failed")
            if chunk_status == "success":
                aggregated.success_chunks += 1
            elif chunk_status == "partial":
                aggregated.partial_chunks += 1
            else:
                aggregated.failed_chunks += 1
                if chunk_result.get("error"):
                    aggregated.errors.append(chunk_result["error"])

            aggregated.total_items_synced += int(chunk_result.get("synced_count", 0) or 0)
            aggregated.total_items_skipped += int(chunk_result.get("skipped_count", 0) or 0)
            aggregated.total_items_failed += int(chunk_result.get("failed_count", 0) or 0)

        # 计算总体状态
        aggregated.status = aggregated.compute_status()
        aggregated.finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # 更新 watermark（仅当配置允许且全部成功时）
        if self.ctx.update_watermark and aggregated.status == RunnerStatus.SUCCESS.value:
            aggregated.watermark_updated = True

        # 自动刷新 vfacts
        if self.ctx.auto_refresh_vfacts and aggregated.total_items_synced > 0:
            info = refresh_vfacts(
                dry_run=self.ctx.dry_run,
                concurrently=self.ctx.refresh_concurrently,
            )
            aggregated.vfacts_refreshed = bool(info.get("refreshed"))
            aggregated.vfacts_refresh_info = info

        return aggregated

    def _generate_time_chunks(
        self,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[TimeWindowChunk]:
        """生成时间窗口 chunks"""
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_REPAIR_WINDOW_HOURS)
        if until is None:
            until = datetime.now(timezone.utc)
        return split_time_window(since, until, chunk_hours=self.ctx.window_chunk_hours)

    def _generate_revision_chunks(
        self,
        start_rev: Optional[int],
        end_rev: Optional[int],
    ) -> List[RevisionWindowChunk]:
        """生成版本窗口 chunks"""
        if start_rev is None:
            start_rev = 1
        if end_rev is None:
            # 如果未指定 end_rev，默认同步到最新版本
            # 这里需要查询实际的最新版本，暂时设为 start_rev + 1000
            end_rev = start_rev + 1000
        return split_revision_window(start_rev, end_rev, chunk_size=self.ctx.window_chunk_revs)

    def _execute_chunk(self, chunk, watermark_constraint: str) -> dict:
        """执行单个 chunk 的同步

        Args:
            chunk: TimeWindowChunk 或 RevisionWindowChunk
            watermark_constraint: watermark 约束类型

        Returns:
            chunk 同步结果字典
        """
        # 构建 chunk payload
        payload = chunk.to_payload(
            update_watermark=self.ctx.update_watermark,
            watermark_constraint=watermark_constraint,
        )

        # 执行同步
        stats = self._run_sync_once(mode="backfill", payload=payload)

        # 构建 chunk 结果
        chunk_result = {
            "chunk_index": chunk.index,
            "chunk_total": chunk.total,
            "status": "success" if stats.get("success", False) else "failed",
            "synced_count": int(stats.get("synced_count", 0) or 0),
            "skipped_count": int(stats.get("skipped_count", 0) or 0),
            "failed_count": int(stats.get("failed_count", 0) or 0),
            "error": stats.get("error"),
        }

        # 添加 window 信息
        if isinstance(chunk, TimeWindowChunk):
            chunk_result["window_type"] = "time"
            chunk_result["since"] = chunk.since.isoformat()
            chunk_result["until"] = chunk.until.isoformat()
        elif isinstance(chunk, RevisionWindowChunk):
            chunk_result["window_type"] = "revision"
            chunk_result["start_rev"] = chunk.start_rev
            chunk_result["end_rev"] = chunk.end_rev

        return chunk_result


# ============ CLI 参数解析 ============


def create_parser() -> argparse.ArgumentParser:
    """
    创建 SCM sync runner 命令行解析器

    Returns:
        配置好的 ArgumentParser 对象

    Note:
        此函数被 parse_args() 和 engram.logbook.cli.scm_sync.runner_main() 复用，
        确保所有入口点使用一致的参数定义。
    """
    parser = argparse.ArgumentParser(
        description="SCM sync runner - 增量同步与回填工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 增量同步
    engram-scm-sync runner incremental --repo gitlab:123

    # 回填最近 24 小时
    engram-scm-sync runner backfill --repo gitlab:123 --last-hours 24

    # 回填指定时间范围
    engram-scm-sync runner backfill --repo gitlab:123 \\
        --since 2025-01-01T00:00:00Z --until 2025-01-31T23:59:59Z

    # SVN 回填指定版本范围
    engram-scm-sync runner backfill --repo svn:https://svn.example.com/repo \\
        --start-rev 100 --end-rev 500

    # 回填并更新游标
    engram-scm-sync runner backfill --repo gitlab:123 --last-hours 24 --update-watermark

    # 查看回填配置
    engram-scm-sync runner config --show-backfill

返回码:
    0  成功 (全部 chunk 成功)
    1  部分成功 (部分 chunk 失败)
    2  失败 (全部 chunk 失败或严重错误)
        """,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不执行实际操作")
    parser.add_argument("--config", metavar="PATH", help="配置文件路径")
    parser.add_argument("--json", dest="json_output", action="store_true", help="JSON 格式输出")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # incremental 子命令
    inc = subparsers.add_parser("incremental", help="增量同步")
    inc.add_argument("--repo", required=True, help="仓库规格 (格式: <type>:<id>，如 gitlab:123)")
    inc.add_argument(
        "--job",
        default=JOB_TYPE_COMMITS,
        choices=sorted(VALID_JOB_TYPES),
        help=f"任务类型 (默认: {JOB_TYPE_COMMITS})",
    )
    inc.add_argument("--loop", action="store_true", help="循环模式")
    inc.add_argument(
        "--loop-interval",
        type=int,
        default=DEFAULT_LOOP_INTERVAL_SECONDS,
        help=f"循环间隔秒数 (默认: {DEFAULT_LOOP_INTERVAL_SECONDS})",
    )
    inc.add_argument("--max-iterations", type=int, default=0, help="最大迭代次数 (0=无限)")

    # backfill 子命令
    bf = subparsers.add_parser("backfill", help="回填同步")
    bf.add_argument("--repo", required=True, help="仓库规格 (格式: <type>:<id>)")
    bf.add_argument(
        "--job",
        default=JOB_TYPE_COMMITS,
        choices=sorted(VALID_JOB_TYPES),
        help=f"任务类型 (默认: {JOB_TYPE_COMMITS})",
    )
    time_group = bf.add_mutually_exclusive_group()
    time_group.add_argument("--last-hours", type=int, help="回填最近 N 小时")
    time_group.add_argument("--last-days", type=int, help="回填最近 N 天")
    bf.add_argument("--update-watermark", action="store_true", help="更新游标位置")
    bf.add_argument("--start-rev", type=int, help="起始版本号 (SVN)")
    bf.add_argument("--end-rev", type=int, help="结束版本号 (SVN)")
    bf.add_argument("--since", help="开始时间 (ISO8601)")
    bf.add_argument("--until", help="结束时间 (ISO8601)")

    # config 子命令
    cfg = subparsers.add_parser("config", help="显示配置")
    cfg.add_argument("--show-backfill", action="store_true", help="显示回填配置")

    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """
    解析命令行参数

    Args:
        argv: 命令行参数列表（默认使用 sys.argv[1:]）

    Returns:
        argparse.Namespace 对象

    Note:
        使用 create_parser() 创建解析器，确保与其他入口点参数定义一致。
    """
    parser = create_parser()
    return parser.parse_args(argv)
