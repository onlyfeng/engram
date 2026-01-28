#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scm_sync_worker - SCM 同步任务 Worker CLI

最小可用的 worker 实现，循环执行:
1. claim 获取任务
2. 执行对应的同步函数（带 heartbeat 续租）
3. 写入 sync_runs 记录
4. ack/fail 完成任务

特性:
- 执行期 heartbeat：后台线程定期续租，防止长时间任务被抢占
- 续租失败保护：超过 N 次续租失败自动中止任务
- 优雅关闭：SIGTERM/SIGINT 时停止拉取新任务，等待当前任务完成

用法:
    python scm_sync_worker.py [OPTIONS]
    
    # 单次执行模式（适合 cron）
    python scm_sync_worker.py --once
    
    # 持续运行模式
    python scm_sync_worker.py --loop --poll-interval 10
    
    # 只处理特定类型的任务
    python scm_sync_worker.py --job-types gitlab_commits,gitlab_mrs
    
    # Pool 模式：只处理特定 GitLab 实例的任务（独立扩容）
    python scm_sync_worker.py --instance-allowlist gitlab.example.com
    
    # Pool 模式：只处理特定租户的任务
    python scm_sync_worker.py --tenant-allowlist tenant1,tenant2
    
    # 使用预定义的 pool 配置
    python scm_sync_worker.py --pool gitlab-prod

环境变量:
    POSTGRES_DSN / TEST_PG_DSN: 数据库连接字符串
    WORKER_ID: 自定义 worker 标识符（默认自动生成）
    POLL_INTERVAL: 轮询间隔秒数（默认 10）
    SCM_WORKER_LEASE_SECONDS: 任务租约时长（默认 300 秒）
    SCM_WORKER_RENEW_INTERVAL_SECONDS: 续租间隔（默认 60 秒）
    SCM_WORKER_MAX_RENEW_FAILURES: 最大续租失败次数（默认 3）

配置文件:
    [scm.worker]
    lease_seconds = 300              # 任务租约时长
    renew_interval_seconds = 60      # 续租间隔
    max_renew_failures = 3           # 最大续租失败次数
    
    # Pool 配置示例（支持不同 pool 独立扩容，不互相影响）
    [scm.worker.pools.gitlab-prod]
    job_types = gitlab_commits,gitlab_mrs,gitlab_reviews
    instance_allowlist = gitlab.example.com,gitlab.corp.com
    
    [scm.worker.pools.gitlab-staging]
    job_types = gitlab_commits,gitlab_mrs
    instance_allowlist = gitlab-staging.example.com
    
    [scm.worker.pools.svn-only]
    job_types = svn
    
    [scm.worker.pools.tenant-a]
    tenant_allowlist = tenant-a,tenant-a-sub
"""

import argparse
import logging
import os
import signal
import socket
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Callable, Tuple
from urllib.parse import urlparse

# engram_step1 包和 db 模块通过 pip install -e 安装
# 参见 pyproject.toml 中的 py-modules 配置
from engram_step1.config import get_config, Config, get_worker_config as get_worker_config_from_module
from engram_step1.db import get_connection
from engram_step1.scm_sync_queue import (
    claim, ack, fail_retry, mark_dead, renew_lease, requeue_without_penalty,
    STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_DEAD,
)
from engram_step1.scm_auth import redact, redact_dict
from engram_step1.scm_sync_policy import (
    CircuitBreakerController,
    CircuitBreakerConfig,
    CircuitState,
)
import db as scm_db
from db import build_circuit_breaker_key

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scm_sync_worker")

# 全局配置缓存（避免每个 job 重复解析配置文件）
_worker_config: Optional[Config] = None


# 全局停止标志
_shutdown_requested = False

# 当前正在执行的 heartbeat 管理器（用于优雅关闭）
_current_heartbeat_manager: Optional["HeartbeatManager"] = None


class HeartbeatManager:
    """
    任务执行期 Heartbeat 管理器
    
    在任务执行期间定期调用 renew_lease 续租，防止长时间任务被其他 worker 抢占。
    超过 max_failures 次续租失败则标记应中止执行。
    
    用法:
        with HeartbeatManager(job_id, worker_id, ...) as hb:
            # 执行任务
            if hb.should_abort:
                # 中止执行
                raise RuntimeError("Lease lost")
    """
    
    def __init__(
        self,
        job_id: str,
        worker_id: str,
        renew_interval_seconds: int = 60,
        lease_seconds: int = 300,
        max_failures: int = 3,
    ):
        """
        Args:
            job_id: 任务 ID
            worker_id: Worker 标识符
            renew_interval_seconds: 续租间隔（秒）
            lease_seconds: 租约时长（秒）
            max_failures: 最大连续续租失败次数
        """
        self.job_id = job_id
        self.worker_id = worker_id
        self.renew_interval_seconds = renew_interval_seconds
        self.lease_seconds = lease_seconds
        self.max_failures = max_failures
        
        # 状态
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failure_count = 0
        self._should_abort = False
        self._last_renew_time: Optional[datetime] = None
        self._lock = threading.Lock()
    
    @property
    def should_abort(self) -> bool:
        """检查是否应中止执行（续租失败次数过多）"""
        with self._lock:
            return self._should_abort
    
    @property
    def failure_count(self) -> int:
        """获取当前续租失败次数"""
        with self._lock:
            return self._failure_count
    
    def start(self) -> None:
        """启动后台续租线程"""
        if self._thread is not None:
            return
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        logger.debug(f"Heartbeat 已启动: job_id={self.job_id}, interval={self.renew_interval_seconds}s")
    
    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """
        停止后台续租线程
        
        Args:
            wait: 是否等待线程结束
            timeout: 等待超时时间（秒）
        """
        self._stop_event.set()
        if wait and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.debug(f"Heartbeat 已停止: job_id={self.job_id}")
    
    def do_final_renew(self) -> bool:
        """
        执行最后一次续租（用于优雅关闭时确保任务不被抢占）
        
        Returns:
            续租是否成功
        """
        return self._do_renew()
    
    def _heartbeat_loop(self) -> None:
        """后台续租循环"""
        while not self._stop_event.is_set():
            # 等待指定间隔，可被中断
            if self._stop_event.wait(timeout=self.renew_interval_seconds):
                break
            
            # 执行续租
            success = self._do_renew()
            
            with self._lock:
                if success:
                    self._failure_count = 0
                    self._last_renew_time = datetime.now(timezone.utc)
                else:
                    self._failure_count += 1
                    logger.warning(
                        f"续租失败 ({self._failure_count}/{self.max_failures}): "
                        f"job_id={self.job_id}"
                    )
                    
                    if self._failure_count >= self.max_failures:
                        self._should_abort = True
                        logger.error(
                            f"续租失败次数超过阈值，标记任务中止: "
                            f"job_id={self.job_id}, failures={self._failure_count}"
                        )
                        break
    
    def _do_renew(self) -> bool:
        """
        执行一次续租
        
        Returns:
            续租是否成功
        """
        try:
            success = renew_lease(
                job_id=self.job_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if success:
                logger.debug(f"续租成功: job_id={self.job_id}")
            else:
                logger.warning(f"续租返回 False（任务可能已被抢占）: job_id={self.job_id}")
            return success
        except Exception as e:
            logger.error(f"续租异常: job_id={self.job_id}, error={e}")
            return False
    
    def __enter__(self) -> "HeartbeatManager":
        """Context manager 入口，启动心跳"""
        global _current_heartbeat_manager
        self.start()
        _current_heartbeat_manager = self
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager 出口，停止心跳"""
        global _current_heartbeat_manager
        self.stop()
        _current_heartbeat_manager = None


def signal_handler(signum, frame):
    """处理 SIGINT/SIGTERM 信号"""
    global _shutdown_requested, _current_heartbeat_manager
    logger.info(f"收到信号 {signum}，准备优雅关闭...")
    _shutdown_requested = True
    
    # 如果有正在运行的任务，执行最后一次续租
    if _current_heartbeat_manager is not None:
        logger.info("执行最后一次续租...")
        try:
            _current_heartbeat_manager.do_final_renew()
        except Exception as e:
            logger.warning(f"最后一次续租失败: {e}")


def generate_worker_id() -> str:
    """生成唯一的 worker 标识符"""
    hostname = socket.gethostname()
    pid = os.getpid()
    short_uuid = uuid.uuid4().hex[:8]
    return f"{hostname}-{pid}-{short_uuid}"


def get_worker_config() -> Config:
    """
    获取并缓存 worker 配置。
    配置来源遵循 engram_step1.config 的优先级规则（--config 之外主要靠 ENGRAM_STEP1_CONFIG）。
    """
    global _worker_config
    if _worker_config is None:
        cfg = get_config()
        cfg.load()
        _worker_config = cfg
    return _worker_config


def _normalize_payload(payload: Any) -> Dict[str, Any]:
    """将 payload 规范化为 dict（兼容 json 字符串 / None）。"""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        # 容错：某些驱动/历史实现可能返回 JSON 字符串
        import json

        try:
            obj = json.loads(payload)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


# 临时性错误分类
TRANSIENT_ERROR_CATEGORIES = {
    "rate_limit",      # API 速率限制（429）
    "timeout",         # 请求超时
    "network",         # 网络错误
    "server_error",    # 服务器错误（5xx）
    "connection",      # 连接错误
}

# 临时性错误关键词匹配
TRANSIENT_ERROR_KEYWORDS = [
    "429", "rate limit", "too many requests",
    "timeout", "timed out",
    "connection", "connect", "network",
    "502", "503", "504", "bad gateway", "service unavailable", "gateway timeout",
    "temporary", "retry",
]

# 不同错误类型的退避时间配置（秒）
TRANSIENT_ERROR_BACKOFF = {
    "rate_limit": 120,    # 速率限制：2 分钟
    "timeout": 30,        # 超时：30 秒
    "network": 60,        # 网络错误：1 分钟
    "server_error": 90,   # 服务器错误：1.5 分钟
    "connection": 45,     # 连接错误：45 秒
    "default": 60,        # 默认：1 分钟
}

# 永久性错误分类（不应重试，直接 mark_dead）
PERMANENT_ERROR_CATEGORIES = {
    "auth_error",         # 认证错误
    "auth_missing",       # 认证凭证缺失
    "auth_invalid",       # 认证凭证无效
    "repo_not_found",     # 仓库不存在
    "repo_type_unknown",  # 仓库类型未知
    "permission_denied",  # 权限不足
}


def _is_transient_error(error_category: str, error_message: str) -> bool:
    """
    判断是否为临时性外部错误。
    
    临时性错误包括：
    - 429 速率限制
    - 请求超时
    - 网络错误
    - 服务器错误（5xx）
    
    Args:
        error_category: 错误分类标签
        error_message: 错误消息
    
    Returns:
        True 表示是临时性错误
    """
    # 检查错误分类
    if error_category and error_category.lower() in TRANSIENT_ERROR_CATEGORIES:
        return True
    
    # 检查错误消息中的关键词
    error_lower = (error_message or "").lower()
    for keyword in TRANSIENT_ERROR_KEYWORDS:
        if keyword in error_lower:
            return True
    
    return False


def _is_permanent_error(error_category: str) -> bool:
    """
    判断是否为永久性错误（不应重试）。
    
    永久性错误包括：
    - auth_error: 认证错误
    - auth_missing: 认证凭证缺失
    - auth_invalid: 认证凭证无效
    - repo_not_found: 仓库不存在
    - repo_type_unknown: 仓库类型未知
    - permission_denied: 权限不足
    
    Args:
        error_category: 错误分类标签
    
    Returns:
        True 表示是永久性错误
    """
    if error_category and error_category.lower() in PERMANENT_ERROR_CATEGORIES:
        return True
    return False


def _get_transient_error_backoff(error_category: str, error_message: str) -> int:
    """
    根据临时性错误类型获取退避时间。
    
    Args:
        error_category: 错误分类标签
        error_message: 错误消息
    
    Returns:
        退避秒数
    """
    # 优先使用错误分类
    if error_category and error_category.lower() in TRANSIENT_ERROR_BACKOFF:
        return TRANSIENT_ERROR_BACKOFF[error_category.lower()]
    
    # 根据错误消息推断类型
    error_lower = (error_message or "").lower()
    
    if "429" in error_lower or "rate limit" in error_lower or "too many requests" in error_lower:
        return TRANSIENT_ERROR_BACKOFF["rate_limit"]
    
    if "timeout" in error_lower or "timed out" in error_lower:
        return TRANSIENT_ERROR_BACKOFF["timeout"]
    
    if "502" in error_lower or "503" in error_lower or "504" in error_lower:
        return TRANSIENT_ERROR_BACKOFF["server_error"]
    
    if "connection" in error_lower or "network" in error_lower:
        return TRANSIENT_ERROR_BACKOFF["network"]
    
    return TRANSIENT_ERROR_BACKOFF["default"]


def _get_repo_info(repo_id: int) -> Optional[Dict[str, Any]]:
    """从 scm.repos 读取仓库信息，用于 job_type → 实际同步实现映射。"""
    cfg = get_worker_config()
    conn = get_connection(config=cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT repo_id, repo_type, url, project_key, default_branch
                FROM scm.repos
                WHERE repo_id = %s
                """,
                (repo_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "repo_id": row[0],
                "repo_type": row[1],
                "url": row[2],
                "project_key": row[3],
                "default_branch": row[4],
            }
    finally:
        conn.close()


def _parse_gitlab_repo(url: str) -> Tuple[str, str]:
    """
    从 GitLab 项目 URL 解析 (gitlab_base_url, project_id_or_path)。
    例: https://gitlab.example.com/group/sub/project -> ("https://gitlab.example.com", "group/sub/project")
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    project_path = parsed.path.lstrip("/")
    # 规范化：去掉尾随斜杠与可选的 .git 后缀
    project_path = project_path.rstrip("/")
    if project_path.endswith(".git"):
        project_path = project_path[:-4]
    return base_url, project_path


def _run_gitlab_commits(repo_info: Dict[str, Any], mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_worker_config()
    gitlab_url, project_id = _parse_gitlab_repo(repo_info["url"])

    # 延迟导入：减少 worker 启动时的依赖/导入开销
    from engram_step1.config import get_incremental_config, get_gitlab_config, is_strict_mode, get_gitlab_auth
    from engram_step1.scm_auth import create_gitlab_token_provider, TokenValidationError
    import scm_sync_gitlab_commits as mod

    gitlab_cfg = get_gitlab_config(cfg)
    inc_cfg = get_incremental_config(cfg)

    # 优先检查 GitLab 认证凭证是否可用
    if not payload.get("token"):
        gitlab_auth = get_gitlab_auth(cfg)
        if gitlab_auth is None:
            # 凭证缺失，返回可读错误（不泄漏明文）
            return {
                "success": False,
                "error": "GitLab 认证凭证未配置。请设置环境变量 GITLAB_TOKEN 或 GITLAB_PRIVATE_TOKEN，"
                         "或在配置文件中配置 [scm.gitlab.auth] 区块。",
                "error_category": "auth_missing",
            }

    try:
        token_provider = create_gitlab_token_provider(cfg, private_token=payload.get("token"))
    except TokenValidationError as e:
        return {
            "success": False,
            "error": f"GitLab 认证凭证无效: {e}",
            "error_category": "auth_invalid",
        }

    suggested_batch_size = payload.get("suggested_batch_size")
    suggested_diff_mode = payload.get("suggested_diff_mode")

    batch_size = int(
        suggested_batch_size
        or payload.get("batch_size")
        or gitlab_cfg.get("batch_size")
        or mod.DEFAULT_BATCH_SIZE
    )

    # DiffMode: always | best_effort | none
    diff_mode = (
        suggested_diff_mode
        or payload.get("diff_mode")
        or gitlab_cfg.get("diff_mode")
        or mod.DiffMode.BEST_EFFORT
    )

    sync_config = mod.SyncConfig(
        gitlab_url=gitlab_url,
        project_id=str(project_id),
        token_provider=token_provider,
        batch_size=batch_size,
        ref_name=repo_info.get("default_branch") or gitlab_cfg.get("ref_name"),
        request_timeout=int(gitlab_cfg.get("request_timeout") or mod.DEFAULT_REQUEST_TIMEOUT),
        overlap_seconds=int(inc_cfg["overlap_seconds"]),
        time_window_days=int(inc_cfg["time_window_days"]),
        forward_window_seconds=int(inc_cfg["forward_window_seconds"]),
        forward_window_min_seconds=int(inc_cfg["forward_window_min_seconds"]),
        adaptive_shrink_factor=float(inc_cfg["adaptive_shrink_factor"]),
        adaptive_grow_factor=float(inc_cfg["adaptive_grow_factor"]),
        adaptive_commit_threshold=int(inc_cfg["adaptive_commit_threshold"]),
        strict=bool(is_strict_mode(cfg)),
        diff_mode=str(diff_mode),
    )

    project_key = repo_info.get("project_key") or cfg.get("project.project_key", "default")

    # 处理 backfill 模式的 time window 参数
    backfill_since = None
    backfill_until = None
    update_watermark = True
    
    if mode == "backfill":
        # 从 payload 读取 time window 参数
        since_ts = payload.get("since")
        until_ts = payload.get("until")
        
        if since_ts is not None:
            # 转换时间戳为 ISO 格式字符串（如果是浮点数）
            if isinstance(since_ts, (int, float)):
                from datetime import datetime, timezone as tz
                backfill_since = datetime.fromtimestamp(since_ts, tz.utc).isoformat()
            else:
                backfill_since = str(since_ts)
        
        if until_ts is not None:
            if isinstance(until_ts, (int, float)):
                from datetime import datetime, timezone as tz
                backfill_until = datetime.fromtimestamp(until_ts, tz.utc).isoformat()
            else:
                backfill_until = str(until_ts)
        
        # backfill 模式默认不更新 watermark
        update_watermark = bool(payload.get("update_watermark", False))
        
        logger.info(
            f"GitLab commits backfill 模式: since={backfill_since}, until={backfill_until}, "
            f"update_watermark={update_watermark}"
        )

    # diff_mode=none 时脚本内部会禁用 diff 获取
    # 注意：如果 scm_sync_gitlab_commits 支持 backfill 参数，在此传递
    # 目前假设它使用 since/until 覆盖增量窗口
    result = mod.sync_gitlab_commits(
        sync_config=sync_config,
        project_key=project_key,
        config=cfg,
        verbose=bool(payload.get("verbose", False)),
        fetch_diffs=True,
        # 如果模块支持这些参数，取消注释
        # backfill_since=backfill_since,
        # backfill_until=backfill_until,
        # update_watermark=update_watermark,
    )
    return result


def _run_gitlab_mrs(repo_info: Dict[str, Any], mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_worker_config()
    gitlab_url, project_id = _parse_gitlab_repo(repo_info["url"])

    from engram_step1.config import get_incremental_config, get_gitlab_config, is_strict_mode, get_gitlab_auth
    from engram_step1.scm_auth import create_gitlab_token_provider, TokenValidationError
    import scm_sync_gitlab_mrs as mod

    gitlab_cfg = get_gitlab_config(cfg)
    inc_cfg = get_incremental_config(cfg)

    # 优先检查 GitLab 认证凭证是否可用
    if not payload.get("token"):
        gitlab_auth = get_gitlab_auth(cfg)
        if gitlab_auth is None:
            return {
                "success": False,
                "error": "GitLab 认证凭证未配置。请设置环境变量 GITLAB_TOKEN 或 GITLAB_PRIVATE_TOKEN，"
                         "或在配置文件中配置 [scm.gitlab.auth] 区块。",
                "error_category": "auth_missing",
            }

    try:
        token_provider = create_gitlab_token_provider(cfg, private_token=payload.get("token"))
    except TokenValidationError as e:
        return {
            "success": False,
            "error": f"GitLab 认证凭证无效: {e}",
            "error_category": "auth_invalid",
        }
    
    # 使用 suggested_batch_size（来自熔断降级）或 payload/配置值
    suggested_batch_size = payload.get("suggested_batch_size")
    batch_size = int(
        suggested_batch_size
        or payload.get("batch_size")
        or gitlab_cfg.get("batch_size")
        or mod.DEFAULT_BATCH_SIZE
    )

    sync_config = mod.SyncConfig(
        gitlab_url=gitlab_url,
        project_id=str(project_id),
        token_provider=token_provider,
        batch_size=batch_size,
        request_timeout=int(gitlab_cfg.get("request_timeout") or mod.DEFAULT_REQUEST_TIMEOUT),
        state_filter=payload.get("mr_state_filter") or gitlab_cfg.get("mr_state_filter"),
        overlap_seconds=int(inc_cfg.get("overlap_seconds", 300)),
        strict=bool(is_strict_mode(cfg)),
    )

    project_key = repo_info.get("project_key") or cfg.get("project.project_key", "default")

    # 处理 backfill 模式的 time window 参数
    backfill_since = None
    backfill_until = None
    update_watermark = True
    
    if mode == "backfill":
        since_ts = payload.get("since")
        until_ts = payload.get("until")
        
        # 转换时间戳为 ISO 格式字符串（如果是浮点数）
        if since_ts is not None:
            if isinstance(since_ts, (int, float)):
                from datetime import datetime, timezone as tz
                backfill_since = datetime.fromtimestamp(since_ts, tz.utc).isoformat()
            else:
                backfill_since = str(since_ts)
        
        if until_ts is not None:
            if isinstance(until_ts, (int, float)):
                from datetime import datetime, timezone as tz
                backfill_until = datetime.fromtimestamp(until_ts, tz.utc).isoformat()
            else:
                backfill_until = str(until_ts)
        
        update_watermark = bool(payload.get("update_watermark", False))
        
        logger.info(
            f"GitLab MRs backfill 模式: since={backfill_since}, until={backfill_until}, "
            f"update_watermark={update_watermark}"
        )

    result = mod.sync_gitlab_mrs(
        sync_config=sync_config,
        project_key=project_key,
        config=cfg,
        verbose=bool(payload.get("verbose", False)),
        fetch_details=bool(payload.get("fetch_details", False)),
        backfill_since=backfill_since,
        backfill_until=backfill_until,
        update_watermark=update_watermark,
    )
    return result


def _run_gitlab_reviews(repo_info: Dict[str, Any], mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_worker_config()
    gitlab_url, project_id = _parse_gitlab_repo(repo_info["url"])

    from engram_step1.config import get_incremental_config, get_gitlab_config, is_strict_mode, get_gitlab_auth
    from engram_step1.scm_auth import create_gitlab_token_provider, TokenValidationError
    import scm_sync_gitlab_reviews as mod

    gitlab_cfg = get_gitlab_config(cfg)
    inc_cfg = get_incremental_config(cfg)

    # 优先检查 GitLab 认证凭证是否可用
    if not payload.get("token"):
        gitlab_auth = get_gitlab_auth(cfg)
        if gitlab_auth is None:
            return {
                "success": False,
                "error": "GitLab 认证凭证未配置。请设置环境变量 GITLAB_TOKEN 或 GITLAB_PRIVATE_TOKEN，"
                         "或在配置文件中配置 [scm.gitlab.auth] 区块。",
                "error_category": "auth_missing",
            }

    try:
        token_provider = create_gitlab_token_provider(cfg, private_token=payload.get("token"))
    except TokenValidationError as e:
        return {
            "success": False,
            "error": f"GitLab 认证凭证无效: {e}",
            "error_category": "auth_invalid",
        }
    
    # 使用 suggested_batch_size（来自熔断降级）或 payload/配置值
    suggested_batch_size = payload.get("suggested_batch_size")
    batch_size = int(
        suggested_batch_size
        or payload.get("batch_size")
        or gitlab_cfg.get("batch_size")
        or mod.DEFAULT_BATCH_SIZE
    )

    sync_config = mod.SyncConfig(
        gitlab_url=gitlab_url,
        project_id=str(project_id),
        token_provider=token_provider,
        batch_size=batch_size,
        request_timeout=int(gitlab_cfg.get("request_timeout") or mod.DEFAULT_REQUEST_TIMEOUT),
        overlap_seconds=int(inc_cfg.get("overlap_seconds", 300)),
        strict=bool(is_strict_mode(cfg)),
    )

    project_key = repo_info.get("project_key") or cfg.get("project.project_key", "default")

    # 处理 backfill 模式的 time window 参数
    backfill_since = None
    backfill_until = None
    update_watermark = True
    
    if mode == "backfill":
        since_ts = payload.get("since")
        until_ts = payload.get("until")
        
        # 转换时间戳为 ISO 格式字符串（如果是浮点数）
        if since_ts is not None:
            if isinstance(since_ts, (int, float)):
                from datetime import datetime, timezone as tz
                backfill_since = datetime.fromtimestamp(since_ts, tz.utc).isoformat()
            else:
                backfill_since = str(since_ts)
        
        if until_ts is not None:
            if isinstance(until_ts, (int, float)):
                from datetime import datetime, timezone as tz
                backfill_until = datetime.fromtimestamp(until_ts, tz.utc).isoformat()
            else:
                backfill_until = str(until_ts)
        
        update_watermark = bool(payload.get("update_watermark", False))
        
        logger.info(
            f"GitLab Reviews backfill 模式: since={backfill_since}, until={backfill_until}, "
            f"update_watermark={update_watermark}"
        )

    result = mod.sync_gitlab_reviews(
        sync_config=sync_config,
        project_key=project_key,
        config=cfg,
        verbose=bool(payload.get("verbose", False)),
        backfill_since=backfill_since,
        backfill_until=backfill_until,
        update_watermark=update_watermark,
    )
    return result


def _run_svn(repo_info: Dict[str, Any], mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_worker_config()

    from engram_step1.config import get_svn_config, is_strict_mode
    import scm_sync_svn as mod

    svn_cfg = get_svn_config(cfg)
    
    # 使用 suggested_batch_size（来自熔断降级）或 payload/配置值
    suggested_batch_size = payload.get("suggested_batch_size")
    batch_size = int(
        suggested_batch_size
        or payload.get("batch_size")
        or svn_cfg.get("batch_size")
        or mod.DEFAULT_BATCH_SIZE
    )
    overlap = int(payload.get("overlap") if payload.get("overlap") is not None else (svn_cfg.get("overlap") or mod.DEFAULT_OVERLAP))

    sync_config = mod.SyncConfig(
        svn_url=repo_info["url"],
        batch_size=batch_size,
        overlap=overlap,
        strict=bool(is_strict_mode(cfg)),
    )

    project_key = repo_info.get("project_key") or cfg.get("project.project_key", "default")

    # 默认：非熔断降级时同步 patches；熔断降级（is_backfill_only）时只同步日志不抓 diff
    is_backfill_only = bool(payload.get("is_backfill_only", False))
    fetch_patches = bool(payload.get("fetch_patches", not is_backfill_only))

    if mode == "backfill":
        # svn backfill 使用 start_rev/end_rev 表示 revision 范围
        start_rev = payload.get("start_rev")
        end_rev = payload.get("end_rev")
        update_watermark = bool(payload.get("update_watermark", False))
        
        if start_rev is None:
            # 缺少 backfill 参数，退化为增量
            logger.warning("SVN backfill 模式缺少 start_rev 参数，退化为增量模式")
            mode = "incremental"
        else:
            logger.info(
                f"SVN backfill 模式: start_rev={start_rev}, end_rev={end_rev}, "
                f"update_watermark={update_watermark}, fetch_patches={fetch_patches}"
            )
            
            return mod.backfill_svn_revisions(
                sync_config=sync_config,
                project_key=project_key,
                start_rev=int(start_rev),
                end_rev=int(end_rev) if end_rev is not None else None,
                update_watermark=update_watermark,
                dry_run=bool(payload.get("dry_run", False)),
                config=cfg,
                verbose=bool(payload.get("verbose", False)),
                fetch_patches=fetch_patches,
                patch_path_filter=payload.get("patch_path_filter"),
            )

    return mod.sync_svn_revisions(
        sync_config=sync_config,
        project_key=project_key,
        config=cfg,
        verbose=bool(payload.get("verbose", False)),
        fetch_patches=fetch_patches,
        patch_path_filter=payload.get("patch_path_filter"),
    )


def create_sync_run(
    repo_id: int,
    job_type: str,
    mode: str,
    conn=None,
) -> str:
    """
    创建 sync_run 记录
    
    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        mode: 同步模式
        conn: 数据库连接
    
    Returns:
        run_id (UUID 字符串)
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection(config=get_worker_config())
    
    try:
        run_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scm.sync_runs (
                    run_id, repo_id, job_type, mode, status, started_at
                )
                VALUES (%s, %s, %s, %s, 'running', now())
            """, (run_id, repo_id, job_type, mode))
            conn.commit()
        return run_id
    finally:
        if should_close:
            conn.close()


def complete_sync_run(
    run_id: str,
    status: str,
    counts: Optional[Dict] = None,
    error_summary: Optional[Dict] = None,
    conn=None,
) -> None:
    """
    完成 sync_run 记录
    
    Args:
        run_id: 运行 ID
        status: 最终状态 ('completed', 'failed', 'no_data')
        counts: 同步计数
        error_summary: 错误摘要
        conn: 数据库连接
    """
    import json
    
    should_close = conn is None
    if conn is None:
        conn = get_connection(config=get_worker_config())
    
    # 对 error_summary 进行脱敏，防止敏感信息泄露
    redacted_error_summary = redact_dict(error_summary) if error_summary else None
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scm.sync_runs
                SET 
                    status = %s,
                    finished_at = now(),
                    counts = %s,
                    error_summary_json = %s
                WHERE run_id = %s
            """, (
                status,
                json.dumps(counts or {}),
                json.dumps(redacted_error_summary) if redacted_error_summary else None,
                run_id,
            ))
            conn.commit()
    finally:
        if should_close:
            conn.close()


def execute_sync_job(job: Dict[str, Any], worker_id: str) -> Dict[str, Any]:
    """
    执行同步任务
    
    这是一个最小实现，实际项目中应该调用具体的同步函数。
    
    Args:
        job: 任务信息
        worker_id: worker 标识符
    
    Returns:
        执行结果，包含 success, counts, error 等字段
    """
    job_id = job["job_id"]
    repo_id = job["repo_id"]
    job_type = job["job_type"]
    mode = job["mode"]
    payload = job.get("payload", {})
    
    logger.info(f"执行任务: job_id={job_id}, repo_id={repo_id}, type={job_type}, mode={mode}")
    
    try:
        # 统一 payload 形态
        payload = _normalize_payload(payload)

        # 根据任务类型分发到具体的同步函数（调用真实同步脚本入口）
        result = dispatch_sync_function(job_type, repo_id, mode, payload, worker_id)

        # 保持兼容：确保至少返回 success 字段
        if "success" not in result:
            result["success"] = bool(result.get("ok", False))
        return result

    except Exception as e:
        logger.error(f"任务执行异常: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "error_category": "exception",
        }


def dispatch_sync_function(
    job_type: str,
    repo_id: int,
    mode: str,
    payload: Dict,
    worker_id: str,
) -> Dict[str, Any]:
    """
    根据任务类型分发到具体的同步函数
    
    这是一个最小实现，实际项目中应该:
    1. 导入具体的同步模块
    2. 调用对应的同步函数
    3. 返回同步结果
    
    支持熔断降级参数:
    - payload['is_backfill_only']: 是否仅执行 backfill
    - payload['suggested_batch_size']: 建议的 batch_size
    - payload['suggested_diff_mode']: 建议的 diff_mode
    
    Args:
        job_type: 任务类型
        repo_id: 仓库 ID
        mode: 同步模式
        payload: 任务参数（可能包含熔断降级参数）
        worker_id: worker 标识符
    
    Returns:
        同步结果
    """
    # 获取同步函数映射
    sync_functions = get_sync_function_registry()
    
    if job_type not in sync_functions:
        logger.warning(f"未知的任务类型: {job_type}，使用默认处理")
        return default_sync_handler(job_type, repo_id, mode, payload)
    
    # 处理熔断降级参数
    if payload.get("is_backfill_only"):
        logger.info(
            f"熔断降级模式: job_type={job_type}, batch_size={payload.get('suggested_batch_size')}, "
            f"diff_mode={payload.get('suggested_diff_mode')}"
        )
        # 实际同步函数应该读取这些参数并调整行为
    
    sync_func = sync_functions[job_type]
    return sync_func(repo_id, mode, payload, worker_id)


def get_sync_function_registry() -> Dict[str, Callable]:
    """
    获取同步函数注册表
    
    注意：worker 以 physical_job_type 为主处理任务
    - gitlab_commits: GitLab 提交记录
    - gitlab_mrs: GitLab Merge Requests
    - gitlab_reviews: GitLab Review 事件
    - svn: SVN 提交记录
    
    同时保留 logical_job_type 的向后兼容支持：
    - commits: 根据 repo_type 分发到 gitlab_commits 或 svn
    - mrs: 映射到 gitlab_mrs
    - reviews: 映射到 gitlab_reviews
    
    Returns:
        job_type -> sync_function 的映射
    """
    return {
        # === 主要：physical_job_type（scheduler 入队时使用）===
        "gitlab_commits": sync_commits,      # GitLab 提交
        "gitlab_mrs": sync_mrs,              # GitLab MRs
        "gitlab_reviews": sync_reviews,      # GitLab Reviews
        "svn": sync_svn,                     # SVN 提交

        # === 兼容：logical_job_type（向后兼容旧任务）===
        # 这些会根据 repo_type 自动分发到正确的实现
        "commits": sync_commits,
        "mrs": sync_mrs,
        "reviews": sync_reviews,
    }


def sync_commits(repo_id: int, mode: str, payload: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Commits 同步（根据 repo_type 分发到 GitLab commits 或 SVN）
    """
    repo_info = _get_repo_info(repo_id)
    if not repo_info:
        return {"success": False, "error": f"repo_id 不存在: {repo_id}", "error_category": "repo_not_found"}

    repo_type = repo_info.get("repo_type")
    if repo_type == "svn":
        return _run_svn(repo_info, mode, payload)
    if repo_type == "git":
        return _run_gitlab_commits(repo_info, mode, payload)

    return {"success": False, "error": f"未知 repo_type: {repo_type}", "error_category": "repo_type_unknown"}


def sync_mrs(repo_id: int, mode: str, payload: Dict, worker_id: str) -> Dict[str, Any]:
    """
    GitLab MRs 同步（SVN 仓库将直接跳过并返回成功）
    """
    repo_info = _get_repo_info(repo_id)
    if not repo_info:
        return {"success": False, "error": f"repo_id 不存在: {repo_id}", "error_category": "repo_not_found"}

    if repo_info.get("repo_type") != "git":
        return {"success": True, "skipped": True, "message": "非 git 仓库，无 MR 同步"}
    return _run_gitlab_mrs(repo_info, mode, payload)


def sync_reviews(repo_id: int, mode: str, payload: Dict, worker_id: str) -> Dict[str, Any]:
    """
    GitLab reviews 同步（SVN 仓库将直接跳过并返回成功）
    """
    repo_info = _get_repo_info(repo_id)
    if not repo_info:
        return {"success": False, "error": f"repo_id 不存在: {repo_id}", "error_category": "repo_not_found"}

    if repo_info.get("repo_type") != "git":
        return {"success": True, "skipped": True, "message": "非 git 仓库，无 reviews 同步"}
    return _run_gitlab_reviews(repo_info, mode, payload)


def sync_svn(repo_id: int, mode: str, payload: Dict, worker_id: str) -> Dict[str, Any]:
    """
    SVN 同步（显式 job_type=svn 时使用）
    """
    repo_info = _get_repo_info(repo_id)
    if not repo_info:
        return {"success": False, "error": f"repo_id 不存在: {repo_id}", "error_category": "repo_not_found"}
    return _run_svn(repo_info, mode, payload)


def default_sync_handler(job_type: str, repo_id: int, mode: str, payload: Dict) -> Dict[str, Any]:
    """
    默认同步处理器（用于未知任务类型）
    """
    return {
        "success": False,
        "error": f"未知的任务类型: {job_type}",
    }


def process_one_job(
    worker_id: str,
    job_types: Optional[List[str]] = None,
    circuit_breaker: Optional[CircuitBreakerController] = None,
    worker_cfg: Optional[Dict[str, Any]] = None,
    instance_allowlist: Optional[List[str]] = None,
    tenant_allowlist: Optional[List[str]] = None,
) -> bool:
    """
    处理一个任务
    
    Args:
        worker_id: worker 标识符
        job_types: 限制处理的任务类型
        circuit_breaker: 熔断控制器（可选，用于记录结果）
        worker_cfg: worker 配置（含 lease_seconds, renew_interval_seconds, max_renew_failures）
        instance_allowlist: 只处理指定 GitLab 实例的任务
        tenant_allowlist: 只处理指定租户的任务
    
    Returns:
        True 如果处理了一个任务，False 如果没有可用任务
    """
    global _shutdown_requested
    
    # 获取 worker 配置
    if worker_cfg is None:
        worker_cfg = get_worker_config_from_module(get_worker_config())
    
    # 1. claim 获取任务（支持 pool 过滤）
    job = claim(
        worker_id=worker_id,
        job_types=job_types,
        instance_allowlist=instance_allowlist,
        tenant_allowlist=tenant_allowlist,
    )
    
    if job is None:
        logger.debug("没有可用任务")
        return False
    
    job_id = job["job_id"]
    payload = job.get("payload", {})
    logger.info(f"获取到任务: job_id={job_id}, type={job['job_type']}, attempts={job['attempts']}")
    
    # 检查任务是否携带熔断降级参数
    is_degraded = payload.get("is_backfill_only", False)
    if is_degraded:
        logger.info(f"任务处于熔断降级模式: circuit_state={payload.get('circuit_state')}")
    
    # 使用任务自身的 lease_seconds，或配置的默认值
    lease_seconds = job.get("lease_seconds") or worker_cfg["lease_seconds"]
    renew_interval = worker_cfg["renew_interval_seconds"]
    max_renew_failures = worker_cfg["max_renew_failures"]
    
    # 创建心跳管理器
    heartbeat = HeartbeatManager(
        job_id=job_id,
        worker_id=worker_id,
        renew_interval_seconds=renew_interval,
        lease_seconds=lease_seconds,
        max_failures=max_renew_failures,
    )
    
    try:
        # 2. 启动心跳并执行同步
        with heartbeat:
            result = execute_sync_job(job, worker_id)
            
            # 检查是否因续租失败需要中止
            if heartbeat.should_abort:
                logger.error(f"任务因续租失败被中止: job_id={job_id}")
                result = {
                    "success": False,
                    "error": f"Lease lost after {heartbeat.failure_count} renewal failures",
                    "error_category": "lease_lost",
                }
        
        # 3. 更新熔断状态（如果提供了熔断控制器）
        if circuit_breaker is not None:
            success = result.get("success", False)
            error_category = result.get("error_category")
            circuit_breaker.record_result(success=success, error_category=error_category)
            
            # 持久化熔断状态
            try:
                conn = get_connection(config=get_worker_config())
                try:
                    scm_db.save_circuit_breaker_state(
                        conn,
                        circuit_breaker._key,
                        circuit_breaker.get_state_dict()
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                logger.warning(f"保存熔断状态失败: {e}")
        
        # 4. 根据结果 ack 或 fail
        # 如果是因为 shutdown 请求，执行最后一次续租确保状态
        if _shutdown_requested:
            logger.info("收到关闭请求，执行最后一次续租...")
            heartbeat.do_final_renew()
        
        if result.get("success"):
            ack(job_id, worker_id, run_id=result.get("run_id"))
            logger.info(f"任务完成: job_id={job_id}")
        else:
            error = result.get("error", "未知错误")
            error_category = result.get("error_category", "")
            # 对错误信息进行脱敏，防止敏感信息（如 token）泄露到数据库
            redacted_error = redact(error)
            
            # 根据错误类型决定处理策略
            if _is_permanent_error(error_category):
                # 永久性错误：直接标记为 dead，不再重试
                mark_dead(job_id, worker_id, redacted_error)
                logger.warning(
                    f"任务永久性失败，标记为 dead: job_id={job_id}, "
                    f"error_category={error_category}, error={redacted_error}"
                )
            elif _is_transient_error(error_category, error):
                # 临时性错误：根据错误类型计算退避时间
                backoff_seconds = _get_transient_error_backoff(error_category, error)
                fail_retry(job_id, worker_id, redacted_error, backoff_seconds=backoff_seconds)
                logger.warning(
                    f"任务临时性失败，安排重试: job_id={job_id}, "
                    f"error_category={error_category}, backoff={backoff_seconds}s, error={redacted_error}"
                )
            else:
                # 未知错误类型：使用默认退避策略
                backoff_seconds = _get_transient_error_backoff(error_category, error)
                fail_retry(job_id, worker_id, redacted_error, backoff_seconds=backoff_seconds)
                logger.warning(
                    f"任务失败，安排重试: job_id={job_id}, "
                    f"backoff={backoff_seconds}s, error={redacted_error}"
                )
        
        return True
        
    except Exception as e:
        # 异常情况下也要 fail_retry
        logger.error(f"任务处理异常: {e}", exc_info=True)
        
        # 更新熔断状态（失败）
        if circuit_breaker is not None:
            circuit_breaker.record_result(success=False, error_category="exception")
        
        try:
            # 对错误信息进行脱敏，防止敏感信息泄露
            redacted_error = redact(str(e))
            error_str = str(e)
            # 根据异常消息推断错误类型并计算退避时间
            backoff_seconds = _get_transient_error_backoff("exception", error_str)
            fail_retry(job_id, worker_id, redacted_error, backoff_seconds=backoff_seconds)
        except Exception:
            pass
        return True


def _build_worker_circuit_breaker_key(
    cfg: Config,
    pool_name: Optional[str] = None,
    instance_allowlist: Optional[List[str]] = None,
    tenant_allowlist: Optional[List[str]] = None,
) -> str:
    """
    构建 worker 使用的熔断 key
    
    Key 规范: <project_key>:<scope>
    - 有 pool 配置时: <project_key>:pool:<pool_name>
    - 无 pool 配置时: <project_key>:global
    
    Args:
        cfg: 配置对象
        pool_name: 预定义的 pool 名称（从 --pool 参数）
        instance_allowlist: GitLab 实例过滤列表
        tenant_allowlist: 租户过滤列表
    
    Returns:
        规范化的熔断 key
    """
    # 获取 project_key
    project_key = cfg.get("project.project_key", "default") or "default"
    
    # 确定 scope
    # 优先使用 pool_name，其次根据 allowlist 推断
    if pool_name:
        # 使用预定义的 pool 名称
        return build_circuit_breaker_key(project_key=project_key, pool_name=pool_name)
    
    if instance_allowlist:
        # 根据 instance 列表构建 pool 名称
        # 使用第一个 instance 作为 pool 标识（简化）
        pool_id = instance_allowlist[0].replace(".", "-").replace(":", "-")
        return build_circuit_breaker_key(project_key=project_key, pool_name=f"instance-{pool_id}")
    
    if tenant_allowlist:
        # 根据 tenant 列表构建 pool 名称
        pool_id = tenant_allowlist[0]
        return build_circuit_breaker_key(project_key=project_key, pool_name=f"tenant-{pool_id}")
    
    # 没有 pool 配置，使用全局 key
    return build_circuit_breaker_key(project_key=project_key, scope="global")


def run_loop(
    worker_id: str,
    job_types: Optional[List[str]] = None,
    poll_interval: int = 10,
    max_iterations: Optional[int] = None,
    enable_circuit_breaker: bool = True,
    instance_allowlist: Optional[List[str]] = None,
    tenant_allowlist: Optional[List[str]] = None,
    pool_name: Optional[str] = None,
) -> None:
    """
    运行 worker 循环
    
    Args:
        worker_id: worker 标识符
        job_types: 限制处理的任务类型
        poll_interval: 轮询间隔（秒）
        max_iterations: 最大迭代次数（用于测试）
        enable_circuit_breaker: 是否启用熔断控制器
        instance_allowlist: 只处理指定 GitLab 实例的任务
        tenant_allowlist: 只处理指定租户的任务
        pool_name: 预定义的 pool 名称（用于构建稳定的熔断 key）
    """
    global _shutdown_requested
    
    # 获取 worker 配置
    cfg = get_worker_config()
    worker_cfg = get_worker_config_from_module(cfg)
    
    # 构建 pool 过滤信息用于日志
    pool_info = []
    if pool_name:
        pool_info.append(f"pool={pool_name}")
    if instance_allowlist:
        pool_info.append(f"instances={instance_allowlist}")
    if tenant_allowlist:
        pool_info.append(f"tenants={tenant_allowlist}")
    pool_str = f", {', '.join(pool_info)}" if pool_info else ""
    
    logger.info(
        f"Worker 启动: id={worker_id}, types={job_types or 'all'}{pool_str}, poll={poll_interval}s, "
        f"lease={worker_cfg['lease_seconds']}s, renew_interval={worker_cfg['renew_interval_seconds']}s"
    )
    
    # 初始化熔断控制器（使用稳定的 pool key 而非随机 worker_id）
    circuit_breaker = None
    if enable_circuit_breaker:
        # 构建规范化的熔断 key: <project_key>:pool:<pool_name> 或 <project_key>:global
        cb_key = _build_worker_circuit_breaker_key(
            cfg=cfg,
            pool_name=pool_name,
            instance_allowlist=instance_allowlist,
            tenant_allowlist=tenant_allowlist,
        )
        circuit_breaker = CircuitBreakerController(key=cb_key)
        
        logger.info(f"Worker 熔断控制器初始化: key={cb_key}")
        
        # 尝试从持久化加载状态（支持旧 key 回退）
        try:
            conn = get_connection(config=cfg)
            try:
                state_dict = scm_db.load_circuit_breaker_state(conn, circuit_breaker._key)
                if state_dict:
                    circuit_breaker.load_state_dict(state_dict)
                    logger.info(f"已加载熔断状态: state={circuit_breaker.state.value}")
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"加载熔断状态失败: {e}")
    
    iteration = 0
    while not _shutdown_requested:
        try:
            # 如果熔断器打开，检查是否需要等待
            if circuit_breaker is not None and circuit_breaker.is_open:
                # 获取健康统计以检查熔断状态
                try:
                    conn = get_connection(config=cfg)
                    try:
                        health_stats = scm_db.get_sync_runs_health_stats(conn, window_count=20)
                        decision = circuit_breaker.check(health_stats)
                        
                        if not decision.allow_sync and decision.wait_seconds > 0:
                            wait_time = min(decision.wait_seconds, poll_interval)
                            logger.info(f"熔断器打开，等待 {wait_time:.1f}s...")
                            time.sleep(wait_time)
                            continue
                    finally:
                        conn.close()
                except Exception as e:
                    logger.warning(f"检查熔断状态失败: {e}")
            
            processed = process_one_job(
                worker_id, job_types, circuit_breaker, worker_cfg,
                instance_allowlist=instance_allowlist,
                tenant_allowlist=tenant_allowlist,
            )
            
            if not processed:
                # 没有任务，等待后重试（支持中断）
                logger.debug(f"等待 {poll_interval} 秒后重试...")
                for _ in range(poll_interval):
                    if _shutdown_requested:
                        break
                    time.sleep(1)
            else:
                # 处理了任务，检查是否需要关闭
                if _shutdown_requested:
                    logger.info("任务完成后收到关闭请求，停止拉取新任务")
                    break
                
        except KeyboardInterrupt:
            logger.info("收到 KeyboardInterrupt，退出...")
            break
        except Exception as e:
            logger.error(f"Worker 循环异常: {e}", exc_info=True)
            time.sleep(poll_interval)
        
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            logger.info(f"达到最大迭代次数 {max_iterations}，退出")
            break
    
    logger.info("Worker 已停止")


def run_once(
    worker_id: str,
    job_types: Optional[List[str]] = None,
    enable_circuit_breaker: bool = True,
    instance_allowlist: Optional[List[str]] = None,
    tenant_allowlist: Optional[List[str]] = None,
    pool_name: Optional[str] = None,
) -> bool:
    """
    单次执行模式：处理一个任务后退出
    
    Args:
        worker_id: worker 标识符
        job_types: 限制处理的任务类型
        enable_circuit_breaker: 是否启用熔断控制器
        instance_allowlist: 只处理指定 GitLab 实例的任务
        tenant_allowlist: 只处理指定租户的任务
        pool_name: 预定义的 pool 名称（用于构建稳定的熔断 key）
    
    Returns:
        True 如果处理了任务，False 如果没有任务
    """
    # 获取 worker 配置
    cfg = get_worker_config()
    worker_cfg = get_worker_config_from_module(cfg)
    
    # 构建 pool 过滤信息用于日志
    pool_info = []
    if pool_name:
        pool_info.append(f"pool={pool_name}")
    if instance_allowlist:
        pool_info.append(f"instances={instance_allowlist}")
    if tenant_allowlist:
        pool_info.append(f"tenants={tenant_allowlist}")
    pool_str = f", {', '.join(pool_info)}" if pool_info else ""
    
    logger.info(
        f"Worker 单次执行: id={worker_id}, types={job_types or 'all'}{pool_str}, "
        f"lease={worker_cfg['lease_seconds']}s, renew_interval={worker_cfg['renew_interval_seconds']}s"
    )
    
    # 初始化熔断控制器（使用稳定的 pool key 而非随机 worker_id）
    circuit_breaker = None
    if enable_circuit_breaker:
        # 构建规范化的熔断 key
        cb_key = _build_worker_circuit_breaker_key(
            cfg=cfg,
            pool_name=pool_name,
            instance_allowlist=instance_allowlist,
            tenant_allowlist=tenant_allowlist,
        )
        circuit_breaker = CircuitBreakerController(key=cb_key)
        
        logger.info(f"Worker 熔断控制器初始化: key={cb_key}")
        
        # 尝试从持久化加载状态（支持旧 key 回退）
        try:
            conn = get_connection(config=cfg)
            try:
                state_dict = scm_db.load_circuit_breaker_state(conn, circuit_breaker._key)
                if state_dict:
                    circuit_breaker.load_state_dict(state_dict)
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"加载熔断状态失败: {e}")
    
    return process_one_job(
        worker_id, job_types, circuit_breaker, worker_cfg,
        instance_allowlist=instance_allowlist,
        tenant_allowlist=tenant_allowlist,
    )


def main():
    parser = argparse.ArgumentParser(
        description="SCM 同步任务 Worker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("WORKER_ID"),
        help="Worker 标识符（默认自动生成）",
    )
    
    parser.add_argument(
        "--job-types",
        help="限制处理的任务类型（逗号分隔，如 gitlab_commits,gitlab_mrs）",
    )
    
    # === Pool 过滤参数（实现不同 pool 独立扩容）===
    parser.add_argument(
        "--pool",
        help="预定义的 pool 名称（会从配置文件读取对应的 job-types/instance-allowlist/tenant-allowlist）",
    )
    
    parser.add_argument(
        "--instance-allowlist",
        help="只处理指定 GitLab 实例的任务（逗号分隔，如 gitlab.example.com,gitlab.corp.com）",
    )
    
    parser.add_argument(
        "--tenant-allowlist",
        help="只处理指定租户的任务（逗号分隔，如 tenant1,tenant2）",
    )
    
    parser.add_argument(
        "--once",
        action="store_true",
        help="单次执行模式：处理一个任务后退出",
    )
    
    parser.add_argument(
        "--loop",
        action="store_true",
        help="持续运行模式（默认）",
    )
    
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL", "10")),
        help="轮询间隔秒数（默认 10）",
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试日志",
    )
    
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=None,
        help="任务租约时长（秒），覆盖配置文件和环境变量",
    )
    
    parser.add_argument(
        "--renew-interval",
        type=int,
        default=None,
        help="续租间隔（秒），覆盖配置文件和环境变量",
    )
    
    parser.add_argument(
        "--max-renew-failures",
        type=int,
        default=None,
        help="最大续租失败次数，超过则中止任务",
    )
    
    args = parser.parse_args()
    
    # 处理 CLI 覆盖的 worker 配置
    if args.lease_seconds is not None:
        os.environ["SCM_WORKER_LEASE_SECONDS"] = str(args.lease_seconds)
    if args.renew_interval is not None:
        os.environ["SCM_WORKER_RENEW_INTERVAL_SECONDS"] = str(args.renew_interval)
    if args.max_renew_failures is not None:
        os.environ["SCM_WORKER_MAX_RENEW_FAILURES"] = str(args.max_renew_failures)
    
    # 配置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 生成 worker ID
    worker_id = args.worker_id or generate_worker_id()
    
    # 解析任务类型
    job_types = None
    if args.job_types:
        job_types = [t.strip() for t in args.job_types.split(",") if t.strip()]
    
    # 解析 pool 过滤参数
    instance_allowlist = None
    tenant_allowlist = None
    
    # 如果指定了 --pool，从配置文件读取预定义的 pool 配置
    if args.pool:
        cfg = get_worker_config()
        pool_config = cfg.get(f"scm.worker.pools.{args.pool}", {})
        
        if not pool_config:
            logger.warning(f"未找到 pool 配置: {args.pool}，使用命令行参数")
        else:
            # 从 pool 配置读取 job_types（如果未在命令行指定）
            if job_types is None and pool_config.get("job_types"):
                pool_job_types = pool_config.get("job_types")
                if isinstance(pool_job_types, str):
                    job_types = [t.strip() for t in pool_job_types.split(",") if t.strip()]
                elif isinstance(pool_job_types, list):
                    job_types = pool_job_types
            
            # 从 pool 配置读取 instance_allowlist
            if pool_config.get("instance_allowlist"):
                pool_instances = pool_config.get("instance_allowlist")
                if isinstance(pool_instances, str):
                    instance_allowlist = [i.strip() for i in pool_instances.split(",") if i.strip()]
                elif isinstance(pool_instances, list):
                    instance_allowlist = pool_instances
            
            # 从 pool 配置读取 tenant_allowlist
            if pool_config.get("tenant_allowlist"):
                pool_tenants = pool_config.get("tenant_allowlist")
                if isinstance(pool_tenants, str):
                    tenant_allowlist = [t.strip() for t in pool_tenants.split(",") if t.strip()]
                elif isinstance(pool_tenants, list):
                    tenant_allowlist = pool_tenants
            
            logger.info(f"使用 pool 配置 '{args.pool}': job_types={job_types}, instances={instance_allowlist}, tenants={tenant_allowlist}")
    
    # 命令行参数覆盖 pool 配置
    if args.instance_allowlist:
        instance_allowlist = [i.strip() for i in args.instance_allowlist.split(",") if i.strip()]
    
    if args.tenant_allowlist:
        tenant_allowlist = [t.strip() for t in args.tenant_allowlist.split(",") if t.strip()]
    
    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 执行
    if args.once:
        success = run_once(
            worker_id, job_types,
            instance_allowlist=instance_allowlist,
            tenant_allowlist=tenant_allowlist,
            pool_name=args.pool,  # 传递 pool 名称用于构建稳定的熔断 key
        )
        sys.exit(0 if success else 1)
    else:
        run_loop(
            worker_id, job_types, args.poll_interval,
            instance_allowlist=instance_allowlist,
            tenant_allowlist=tenant_allowlist,
            pool_name=args.pool,  # 传递 pool 名称用于构建稳定的熔断 key
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
