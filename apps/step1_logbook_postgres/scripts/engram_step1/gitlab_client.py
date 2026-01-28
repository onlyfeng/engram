"""
engram_step1.gitlab_client - 可复用的 GitLab REST API 客户端

功能:
- 统一封装 _request() 方法，支持自动重试和退避策略
- 对 429 读取 Retry-After，否则使用指数退避（含随机抖动）
- 对 5xx/连接错误按策略重试
- 对 401/403 触发 TokenProvider.invalidate() 后重试一次
- 返回结构化错误信息供上层降级/计数

配置项:
    [scm.http]
    timeout_seconds = 60          # 请求超时
    max_attempts = 3              # 最大重试次数
    backoff_base_seconds = 1.0    # 退避基础秒数
    backoff_max_seconds = 60.0    # 退避最大秒数

    [scm.gitlab]
    max_concurrency = 5           # 可选，最大并发数
"""

import json
import logging
import random
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol
from urllib.parse import quote

import requests

from .errors import EngramError


# ============ 速率限制器协议 ============


class RateLimiterProtocol(Protocol):
    """
    速率限制器协议（抽象接口）
    
    所有速率限制器（local/distributed）都应实现此协议。
    支持请求前 acquire、429 通知、统计获取。
    """
    
    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        获取一个请求令牌
        
        Args:
            timeout: 超时时间（秒），None 表示使用默认策略
            
        Returns:
            True 如果成功获取，False 如果超时或被拒绝
        """
        ...
    
    def notify_rate_limit(
        self,
        retry_after: Optional[float] = None,
        reset_time: Optional[float] = None,
    ) -> None:
        """
        通知收到了速率限制响应（429）
        
        Args:
            retry_after: Retry-After 头的值（秒）
            reset_time: RateLimit-Reset 头的值（Unix 时间戳）
        """
        ...
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        ...


# ============ 并发与速率限制器 ============


class ConcurrencyLimiter:
    """
    并发限制器（基于 Semaphore）
    
    用于控制同时进行的 HTTP 请求数量，避免对 GitLab 服务造成过大压力。
    """
    
    def __init__(self, max_concurrency: int):
        """
        初始化并发限制器
        
        Args:
            max_concurrency: 最大并发数
        """
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency 必须为正整数，当前: {max_concurrency}")
        self._max_concurrency = max_concurrency
        self._semaphore = threading.Semaphore(max_concurrency)
        self._lock = threading.Lock()
        self._active_count = 0
        self._waiting_count = 0
        self._total_acquired = 0
        self._total_wait_time_ms = 0.0
    
    @property
    def max_concurrency(self) -> int:
        """最大并发数"""
        return self._max_concurrency
    
    @property
    def active_count(self) -> int:
        """当前活跃请求数"""
        with self._lock:
            return self._active_count
    
    @property
    def waiting_count(self) -> int:
        """当前等待请求数"""
        with self._lock:
            return self._waiting_count
    
    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        获取一个并发槽位
        
        Args:
            timeout: 超时时间（秒），None 表示无限等待
            
        Returns:
            True 如果成功获取，False 如果超时
        """
        start_time = time.time()
        
        with self._lock:
            self._waiting_count += 1
        
        try:
            acquired = self._semaphore.acquire(blocking=True, timeout=timeout)
            
            with self._lock:
                self._waiting_count -= 1
                if acquired:
                    self._active_count += 1
                    self._total_acquired += 1
                    self._total_wait_time_ms += (time.time() - start_time) * 1000
            
            return acquired
        except Exception:
            with self._lock:
                self._waiting_count -= 1
            raise
    
    def release(self) -> None:
        """释放一个并发槽位"""
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1
        self._semaphore.release()
    
    def __enter__(self):
        """上下文管理器入口"""
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.release()
        return False
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                "max_concurrency": self._max_concurrency,
                "active_count": self._active_count,
                "waiting_count": self._waiting_count,
                "total_acquired": self._total_acquired,
                "avg_wait_time_ms": round(self._total_wait_time_ms / self._total_acquired, 2) if self._total_acquired > 0 else 0,
            }


class RateLimiter:
    """
    速率限制器（基于令牌桶算法）
    
    用于控制 HTTP 请求速率，避免触发 GitLab 的 429 限流。
    支持根据 429 响应中的 Retry-After 或 RateLimit-Reset 头自动调整。
    """
    
    def __init__(
        self,
        requests_per_second: float = 10.0,
        burst_size: Optional[int] = None,
    ):
        """
        初始化速率限制器
        
        Args:
            requests_per_second: 每秒允许的请求数
            burst_size: 突发容量（默认等于 requests_per_second）
        """
        if requests_per_second <= 0:
            raise ValueError(f"requests_per_second 必须为正数，当前: {requests_per_second}")
        
        self._rate = requests_per_second
        self._burst_size = burst_size or int(max(1, requests_per_second))
        self._tokens = float(self._burst_size)
        self._last_update = time.time()
        self._lock = threading.Lock()
        
        # 统计信息
        self._total_requests = 0
        self._total_wait_time_ms = 0.0
        self._throttled_count = 0
        
        # 429 响应时暂停直到的时间
        self._paused_until: Optional[float] = None
    
    @property
    def rate(self) -> float:
        """每秒请求数"""
        return self._rate
    
    @property
    def burst_size(self) -> int:
        """突发容量"""
        return self._burst_size
    
    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        获取一个请求令牌
        
        如果令牌不足，会等待直到有可用令牌或超时。
        
        Args:
            timeout: 超时时间（秒），None 表示无限等待
            
        Returns:
            True 如果成功获取，False 如果超时
        """
        start_time = time.time()
        deadline = start_time + timeout if timeout else None
        
        while True:
            with self._lock:
                now = time.time()
                
                # 检查是否因 429 被暂停
                if self._paused_until and now < self._paused_until:
                    wait_time = self._paused_until - now
                    if deadline and now + wait_time > deadline:
                        return False
                else:
                    self._paused_until = None
                
                # 补充令牌
                elapsed = now - self._last_update
                self._tokens = min(self._burst_size, self._tokens + elapsed * self._rate)
                self._last_update = now
                
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._total_requests += 1
                    self._total_wait_time_ms += (now - start_time) * 1000
                    return True
                
                # 计算需要等待的时间
                wait_time = (1.0 - self._tokens) / self._rate
                self._throttled_count += 1
            
            # 检查是否超时
            if deadline and time.time() + wait_time > deadline:
                return False
            
            # 等待令牌补充
            time.sleep(min(wait_time, 0.1))  # 最多等待 0.1 秒后重新检查
    
    def pause_until(self, resume_time: float) -> None:
        """
        暂停请求直到指定时间（用于处理 429 响应）
        
        Args:
            resume_time: 恢复请求的时间戳
        """
        with self._lock:
            if self._paused_until is None or resume_time > self._paused_until:
                self._paused_until = resume_time
    
    def notify_rate_limit(self, retry_after: Optional[float] = None, reset_time: Optional[float] = None) -> None:
        """
        通知收到了速率限制响应（429）
        
        Args:
            retry_after: Retry-After 头的值（秒）
            reset_time: RateLimit-Reset 头的值（Unix 时间戳）
        """
        now = time.time()
        
        if reset_time and reset_time > now:
            self.pause_until(reset_time)
        elif retry_after:
            self.pause_until(now + retry_after)
        else:
            # 默认暂停 1 秒
            self.pause_until(now + 1.0)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                "rate": self._rate,
                "burst_size": self._burst_size,
                "tokens": round(self._tokens, 2),
                "total_requests": self._total_requests,
                "throttled_count": self._throttled_count,
                "avg_wait_time_ms": round(self._total_wait_time_ms / self._total_requests, 2) if self._total_requests > 0 else 0,
                "paused_until": self._paused_until,
            }


class PostgresRateLimiter:
    """
    基于 Postgres 的分布式速率限制器
    
    与内存版 RateLimiter 相比，此限制器：
    - 支持多进程/多 worker 共享限流状态
    - 使用 Postgres 行锁保证原子性
    - 支持 429 Retry-After 反馈机制
    
    适用场景：
    - 多 worker 并发同步
    - 需要跨进程共享限流状态
    - 需要持久化限流配置
    """
    
    def __init__(
        self,
        instance_key: str,
        dsn: Optional[str] = None,
        rate: float = 10.0,
        burst: int = 20,
        max_wait_seconds: float = 60.0,
    ):
        """
        初始化 Postgres 限流器
        
        Args:
            instance_key: 实例标识（如 GitLab 域名）
            dsn: 数据库连接字符串，为 None 时从环境变量 POSTGRES_DSN 读取
            rate: 令牌补充速率（tokens/sec）
            burst: 最大令牌容量
            max_wait_seconds: 最大等待时间
        """
        self._instance_key = instance_key
        self._dsn = dsn
        self._rate = rate
        self._burst = burst
        self._max_wait_seconds = max_wait_seconds
        self._lock = threading.Lock()
        
        # 统计信息
        self._total_requests = 0
        self._total_wait_time_ms = 0.0
        self._throttled_count = 0
        self._rejected_count = 0
    
    @property
    def instance_key(self) -> str:
        """实例标识"""
        return self._instance_key
    
    @property
    def rate(self) -> float:
        """每秒请求数"""
        return self._rate
    
    @property
    def burst_size(self) -> int:
        """突发容量"""
        return self._burst
    
    def _get_conn(self):
        """获取数据库连接"""
        import os
        dsn = self._dsn or os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise ValueError("POSTGRES_DSN 环境变量未设置，且未提供 dsn 参数")
        
        import psycopg
        return psycopg.connect(dsn, autocommit=True)
    
    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        获取一个请求令牌（从 Postgres 消费）
        
        如果令牌不足或被暂停，会等待直到有可用令牌或超时。
        
        Args:
            timeout: 超时时间（秒），None 表示使用 max_wait_seconds
            
        Returns:
            True 如果成功获取，False 如果超时或被拒绝
        """
        # 延迟导入避免循环依赖
        from ..db import consume_rate_limit_token
        
        max_wait = timeout if timeout is not None else self._max_wait_seconds
        start_time = time.time()
        
        while True:
            elapsed = time.time() - start_time
            if elapsed >= max_wait:
                with self._lock:
                    self._rejected_count += 1
                logger.debug(f"Postgres 限流器: 等待超时 ({elapsed:.2f}s >= {max_wait}s)")
                return False
            
            try:
                with self._get_conn() as conn:
                    result = consume_rate_limit_token(
                        conn,
                        self._instance_key,
                        tokens_needed=1.0,
                        default_rate=self._rate,
                        default_burst=self._burst,
                    )
                
                if result.allowed:
                    with self._lock:
                        self._total_requests += 1
                        self._total_wait_time_ms += elapsed * 1000
                    return True
                
                # 需要等待
                wait_seconds = min(result.wait_seconds, max_wait - elapsed, 1.0)
                if wait_seconds <= 0:
                    with self._lock:
                        self._rejected_count += 1
                    return False
                
                with self._lock:
                    self._throttled_count += 1
                
                logger.debug(f"Postgres 限流器: 等待 {wait_seconds:.2f}s")
                time.sleep(wait_seconds)
                
            except Exception as e:
                logger.warning(f"Postgres 限流器操作失败: {e}，允许请求通过")
                return True  # 失败时允许通过，避免阻塞
    
    def notify_rate_limit(
        self,
        retry_after: Optional[float] = None,
        reset_time: Optional[float] = None,
    ) -> None:
        """
        通知收到了速率限制响应（429）
        
        将暂停信息写入 Postgres，影响所有使用相同 instance_key 的 worker。
        
        Args:
            retry_after: Retry-After 头的值（秒）
            reset_time: RateLimit-Reset 头的值（Unix 时间戳）
        """
        from ..db import pause_rate_limit_bucket
        
        now = time.time()
        
        # 计算暂停秒数
        if reset_time and reset_time > now:
            pause_seconds = reset_time - now
        elif retry_after:
            pause_seconds = retry_after
        else:
            pause_seconds = 1.0  # 默认暂停 1 秒
        
        try:
            with self._get_conn() as conn:
                pause_rate_limit_bucket(
                    conn,
                    self._instance_key,
                    pause_seconds,
                    record_429=True,
                )
            logger.info(f"Postgres 限流器: 收到 429，暂停 {pause_seconds:.1f}s")
        except Exception as e:
            logger.warning(f"Postgres 限流器: 记录 429 失败: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                "type": "postgres",
                "instance_key": self._instance_key,
                "rate": self._rate,
                "burst_size": self._burst,
                "total_requests": self._total_requests,
                "throttled_count": self._throttled_count,
                "rejected_count": self._rejected_count,
                "avg_wait_time_ms": round(
                    self._total_wait_time_ms / self._total_requests, 2
                ) if self._total_requests > 0 else 0,
            }
    
    def get_bucket_status(self) -> Optional[Dict[str, Any]]:
        """从 Postgres 获取当前桶状态"""
        from ..db import get_rate_limit_status
        
        try:
            with self._get_conn() as conn:
                return get_rate_limit_status(conn, self._instance_key)
        except Exception as e:
            logger.warning(f"Postgres 限流器: 获取状态失败: {e}")
            return None


class ComposedRateLimiter:
    """
    组合速率限制器
    
    将多个限制器（local + distributed）组合为一个，支持：
    - acquire 时依次调用所有 limiter
    - 429 通知时通知所有 limiter
    - 统计信息聚合
    
    使用场景：
    - 同时使用本地令牌桶 + Postgres 分布式限流
    - 多层限流策略
    """
    
    def __init__(self, limiters: Optional[List[Any]] = None):
        """
        初始化组合限流器
        
        Args:
            limiters: 限流器列表（可包含 RateLimiter、PostgresRateLimiter 或任何实现 RateLimiterProtocol 的对象）
        """
        self._limiters: List[Any] = limiters or []
        self._lock = threading.Lock()
        
        # 聚合统计
        self._total_requests = 0
        self._total_429_hits = 0
        self._timeout_count = 0
        self._total_wait_time_ms = 0.0
    
    def add_limiter(self, limiter: Any) -> None:
        """添加一个限流器"""
        with self._lock:
            self._limiters.append(limiter)
    
    @property
    def limiters(self) -> List[Any]:
        """获取所有限流器"""
        with self._lock:
            return list(self._limiters)
    
    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        获取一个请求令牌（依次调用所有 limiter）
        
        如果任一 limiter 拒绝，则整体拒绝。
        
        Args:
            timeout: 超时时间（秒）
            
        Returns:
            True 如果所有 limiter 都允许，False 如果任一拒绝
        """
        start_time = time.time()
        
        with self._lock:
            self._total_requests += 1
            limiters_copy = list(self._limiters)
        
        acquired_limiters = []
        
        try:
            for limiter in limiters_copy:
                if hasattr(limiter, 'acquire'):
                    if not limiter.acquire(timeout=timeout):
                        # 某个 limiter 拒绝，记录超时
                        with self._lock:
                            self._timeout_count += 1
                        return False
                    acquired_limiters.append(limiter)
            
            # 所有 limiter 都通过，记录等待时间
            wait_time_ms = (time.time() - start_time) * 1000
            with self._lock:
                self._total_wait_time_ms += wait_time_ms
            
            return True
            
        except Exception as e:
            logger.warning(f"ComposedRateLimiter.acquire 异常: {e}")
            with self._lock:
                self._timeout_count += 1
            return False
    
    def notify_rate_limit(
        self,
        retry_after: Optional[float] = None,
        reset_time: Optional[float] = None,
    ) -> None:
        """
        通知收到了速率限制响应（429），通知所有 limiter
        
        Args:
            retry_after: Retry-After 头的值（秒）
            reset_time: RateLimit-Reset 头的值（Unix 时间戳）
        """
        with self._lock:
            self._total_429_hits += 1
            limiters_copy = list(self._limiters)
        
        for limiter in limiters_copy:
            if hasattr(limiter, 'notify_rate_limit'):
                try:
                    limiter.notify_rate_limit(
                        retry_after=retry_after,
                        reset_time=reset_time,
                    )
                except Exception as e:
                    logger.warning(f"ComposedRateLimiter: 通知 limiter 失败: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取聚合统计信息
        
        包含组合器自身统计 + 各子 limiter 统计
        """
        with self._lock:
            stats = {
                "type": "composed",
                "total_requests": self._total_requests,
                "total_429_hits": self._total_429_hits,
                "timeout_count": self._timeout_count,
                "avg_wait_time_ms": round(
                    self._total_wait_time_ms / self._total_requests, 2
                ) if self._total_requests > 0 else 0,
                "limiter_count": len(self._limiters),
            }
            
            # 收集各子 limiter 的统计
            sub_stats = []
            for i, limiter in enumerate(self._limiters):
                if hasattr(limiter, 'get_stats'):
                    try:
                        sub_stat = limiter.get_stats()
                        sub_stat["index"] = i
                        sub_stats.append(sub_stat)
                    except Exception as e:
                        logger.warning(f"ComposedRateLimiter: 获取子 limiter 统计失败: {e}")
            
            if sub_stats:
                stats["sub_limiters"] = sub_stats
            
            return stats


logger = logging.getLogger(__name__)


# ============ 请求统计 ============


@dataclass
class RequestStats:
    """单次请求统计"""
    endpoint: str
    method: str
    status_code: Optional[int] = None
    duration_ms: float = 0.0
    attempt_count: int = 1
    hit_429: bool = False
    success: bool = False
    error_category: Optional[str] = None
    # 429 相关的额外信息
    retry_after: Optional[float] = None  # Retry-After 头的值（秒）
    rate_limit_reset: Optional[float] = None  # RateLimit-Reset 头的值（Unix 时间戳）
    rate_limit_remaining: Optional[int] = None  # RateLimit-Remaining 头的值


@dataclass
class ClientStats:
    """客户端级别的请求统计汇总"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_429_hits: int = 0
    total_retries: int = 0
    total_duration_ms: float = 0.0
    request_history: List[RequestStats] = field(default_factory=list)
    
    # 429 限流详细信息
    last_retry_after: Optional[float] = None  # 最后一次 Retry-After 值
    last_rate_limit_reset: Optional[float] = None  # 最后一次 RateLimit-Reset 值
    last_rate_limit_remaining: Optional[int] = None  # 最后一次 RateLimit-Remaining 值
    
    # limiter 统计（通过 set_limiter_stats 设置）
    timeout_count: int = 0  # 超时次数（limiter 等待超时）
    avg_wait_time_ms: float = 0.0  # 平均等待时间（limiter acquire）
    
    # 线程安全锁
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    
    # limiter stats 缓存（由 GitLabClient 设置）
    _limiter_stats: Optional[Dict[str, Any]] = field(default=None, repr=False)
    
    def record(self, stats: RequestStats) -> None:
        """记录一次请求统计"""
        with self._lock:
            self.total_requests += 1
            self.total_duration_ms += stats.duration_ms
            if stats.success:
                self.successful_requests += 1
            else:
                self.failed_requests += 1
            if stats.hit_429:
                self.total_429_hits += 1
                # 记录 429 相关的详细信息
                if stats.retry_after is not None:
                    self.last_retry_after = stats.retry_after
                if stats.rate_limit_reset is not None:
                    self.last_rate_limit_reset = stats.rate_limit_reset
                if stats.rate_limit_remaining is not None:
                    self.last_rate_limit_remaining = stats.rate_limit_remaining
            if stats.attempt_count > 1:
                self.total_retries += (stats.attempt_count - 1)
            self.request_history.append(stats)
    
    def set_limiter_stats(
        self,
        timeout_count: int = 0,
        avg_wait_time_ms: float = 0.0,
        limiter_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        设置 limiter 相关统计（由 GitLabClient 调用）
        
        Args:
            timeout_count: 超时次数
            avg_wait_time_ms: 平均等待时间（毫秒）
            limiter_stats: 完整的 limiter 统计字典
        """
        with self._lock:
            self.timeout_count = timeout_count
            self.avg_wait_time_ms = avg_wait_time_ms
            self._limiter_stats = limiter_stats
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典（用于 logbook event payload 和 sync_runs.counts）
        
        包含以下稳定字段（向后兼容）：
        - total_requests: 总请求数
        - successful_requests: 成功请求数
        - failed_requests: 失败请求数
        - total_429_hits: 429 命中次数
        - total_retries: 重试次数
        - total_duration_ms: 总耗时（毫秒）
        - avg_duration_ms: 平均耗时（毫秒）
        - timeout_count: 超时次数（limiter）
        - avg_wait_time_ms: 平均等待时间（limiter）
        """
        with self._lock:
            result = {
                # 基础请求统计（稳定字段）
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "total_429_hits": self.total_429_hits,
                "total_retries": self.total_retries,
                "total_duration_ms": round(self.total_duration_ms, 2),
                "avg_duration_ms": round(self.total_duration_ms / self.total_requests, 2) if self.total_requests > 0 else 0,
                # limiter 统计（稳定字段）
                "timeout_count": self.timeout_count,
                "avg_wait_time_ms": round(self.avg_wait_time_ms, 2),
            }
            # 添加限流详细信息（仅在有值时）
            if self.last_retry_after is not None:
                result["last_retry_after"] = self.last_retry_after
            if self.last_rate_limit_reset is not None:
                result["last_rate_limit_reset"] = self.last_rate_limit_reset
            if self.last_rate_limit_remaining is not None:
                result["last_rate_limit_remaining"] = self.last_rate_limit_remaining
            # 添加详细 limiter 统计（可选，用于调试）
            if self._limiter_stats:
                result["limiter_stats"] = self._limiter_stats
            return result
    
    def reset(self) -> None:
        """重置统计数据"""
        with self._lock:
            self.total_requests = 0
            self.successful_requests = 0
            self.failed_requests = 0
            self.total_429_hits = 0
            self.total_retries = 0
            self.total_duration_ms = 0.0
            self.last_retry_after = None
            self.last_rate_limit_reset = None
            self.last_rate_limit_remaining = None
            self.timeout_count = 0
            self.avg_wait_time_ms = 0.0
            self._limiter_stats = None
            self.request_history.clear()


# ============ 错误分类 ============


class GitLabErrorCategory(str, Enum):
    """GitLab 错误分类"""
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"
    SERVER_ERROR = "server_error"
    CLIENT_ERROR = "client_error"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    CONTENT_TOO_LARGE = "content_too_large"
    UNKNOWN = "unknown"


# ============ 结构化错误 ============


@dataclass
class GitLabAPIResult:
    """GitLab API 请求结果（支持成功和失败场景）"""
    success: bool
    data: Optional[Any] = None  # 成功时的 JSON 响应
    response: Optional[requests.Response] = None  # 原始响应对象
    
    # 错误信息（失败时填充）
    status_code: Optional[int] = None
    endpoint: Optional[str] = None
    error_category: Optional[GitLabErrorCategory] = None
    error_message: Optional[str] = None
    retry_after: Optional[float] = None  # 429 时的 Retry-After 值（秒）
    rate_limit_reset: Optional[float] = None  # RateLimit-Reset 头的值（Unix 时间戳）
    rate_limit_remaining: Optional[int] = None  # RateLimit-Remaining 头的值
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于日志/序列化），对敏感信息进行脱敏"""
        result = {
            "success": self.success,
            "status_code": self.status_code,
            "endpoint": redact(self.endpoint) if self.endpoint else None,
            "error_category": self.error_category.value if self.error_category else None,
            "error_message": redact(self.error_message) if self.error_message else None,
        }
        # 添加限流信息（仅在有值时）
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        if self.rate_limit_reset is not None:
            result["rate_limit_reset"] = self.rate_limit_reset
        if self.rate_limit_remaining is not None:
            result["rate_limit_remaining"] = self.rate_limit_remaining
        return result


class GitLabAPIError(EngramError):
    """GitLab API 错误"""
    exit_code = 11
    error_type = "GITLAB_API_ERROR"

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
        endpoint: Optional[str] = None,
        category: Optional[GitLabErrorCategory] = None,
    ):
        super().__init__(message, details)
        self.status_code = status_code
        self.endpoint = endpoint
        self.category = category or GitLabErrorCategory.UNKNOWN


class GitLabRateLimitError(GitLabAPIError):
    """GitLab API 限流错误 (429)"""
    error_type = "GITLAB_RATE_LIMIT_ERROR"

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        **kwargs
    ):
        super().__init__(message, category=GitLabErrorCategory.RATE_LIMITED, **kwargs)
        self.retry_after = retry_after


class GitLabAuthError(GitLabAPIError):
    """GitLab 认证错误 (401/403)"""
    error_type = "GITLAB_AUTH_ERROR"

    def __init__(self, message: str, **kwargs):
        super().__init__(message, category=GitLabErrorCategory.AUTH_ERROR, **kwargs)


class GitLabServerError(GitLabAPIError):
    """GitLab 服务器错误 (5xx)"""
    error_type = "GITLAB_SERVER_ERROR"

    def __init__(self, message: str, **kwargs):
        super().__init__(message, category=GitLabErrorCategory.SERVER_ERROR, **kwargs)


class GitLabNetworkError(GitLabAPIError):
    """GitLab 网络/连接错误"""
    error_type = "GITLAB_NETWORK_ERROR"

    def __init__(self, message: str, **kwargs):
        super().__init__(message, category=GitLabErrorCategory.NETWORK_ERROR, **kwargs)


class GitLabTimeoutError(GitLabAPIError):
    """GitLab 请求超时错误"""
    error_type = "GITLAB_TIMEOUT_ERROR"

    def __init__(self, message: str, **kwargs):
        super().__init__(message, category=GitLabErrorCategory.TIMEOUT, **kwargs)


# ============ Token Provider ============

# 复用 scm_auth 模块中的 TokenProvider（避免重复定义）
try:
    from .scm_auth import TokenProvider, StaticTokenProvider, mask_token, redact, redact_headers
except ImportError:
    # Fallback: 如果 scm_auth 不存在，使用 Protocol 定义
    from typing import Protocol

    class TokenProvider(Protocol):
        """Token 提供者协议（用于 token 失效时刷新/重试）"""

        def get_token(self) -> str:
            """获取当前有效的 token"""
            ...

        def invalidate(self) -> None:
            """标记当前 token 为无效，下次 get_token 应返回新 token"""
            ...

    class StaticTokenProvider:
        """静态 Token 提供者（最简单的实现）"""

        def __init__(self, token: str):
            self._token = token
            self._invalidated = False

        def get_token(self) -> str:
            return self._token

        def invalidate(self) -> None:
            self._invalidated = True
            logger.warning("Token 已被标记为无效（静态 token 无法自动刷新）")

    def mask_token(token: Optional[str]) -> str:
        """简单的 token 遮蔽"""
        if not token:
            return "empty"
        return f"len={len(token)}, ***"

    def redact(text) -> str:
        """简单的敏感信息脱敏"""
        if not text:
            return ""
        import re
        result = str(text)
        # GitLab token 模式
        result = re.sub(r'\b(glp[a-z]{1,2}-[A-Za-z0-9_-]{10,})\b', '[GITLAB_TOKEN]', result)
        # PRIVATE-TOKEN header 值
        result = re.sub(r'(PRIVATE-TOKEN[:\s]+)[^\s,;]+', r'\1[REDACTED]', result, flags=re.IGNORECASE)
        return result

    def redact_headers(headers) -> dict:
        """简单的 header 脱敏"""
        if not headers:
            return {}
        sensitive = {'authorization', 'private-token', 'x-private-token', 'cookie'}
        return {k: '[REDACTED]' if k.lower() in sensitive else v for k, v in headers.items()}


# ============ HTTP 配置 ============


@dataclass
class HttpConfig:
    """HTTP 请求配置"""
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    max_concurrency: Optional[int] = None  # 可选并发限制
    # 速率限制配置（内存版）
    rate_limit_enabled: bool = False  # 是否启用速率限制
    rate_limit_requests_per_second: float = 10.0  # 每秒请求数
    rate_limit_burst_size: Optional[int] = None  # 突发容量
    # Postgres 限流配置（分布式版）
    postgres_rate_limit_enabled: bool = False  # 是否启用 Postgres 限流
    postgres_rate_limit_dsn: Optional[str] = None  # Postgres DSN（默认从环境变量读取）
    postgres_rate_limit_rate: float = 10.0  # 令牌补充速率
    postgres_rate_limit_burst: int = 20  # 最大令牌容量
    postgres_rate_limit_max_wait: float = 60.0  # 最大等待时间

    @classmethod
    def from_config(cls, config: Optional["Config"] = None) -> "HttpConfig":
        """从配置对象加载 HTTP 配置"""
        import os
        
        if config is None:
            return cls()
        
        # postgres_rate_limit_dsn: 优先 scm.gitlab.postgres_rate_limit_dsn，否则回退 POSTGRES_DSN
        postgres_dsn = config.get("scm.gitlab.postgres_rate_limit_dsn")
        if postgres_dsn is None:
            postgres_dsn = os.environ.get("POSTGRES_DSN")

        return cls(
            timeout_seconds=config.get("scm.http.timeout_seconds", 60.0),
            max_attempts=config.get("scm.http.max_attempts", 3),
            backoff_base_seconds=config.get("scm.http.backoff_base_seconds", 1.0),
            backoff_max_seconds=config.get("scm.http.backoff_max_seconds", 60.0),
            max_concurrency=config.get("scm.gitlab.max_concurrency"),
            rate_limit_enabled=config.get("scm.gitlab.rate_limit_enabled", False),
            rate_limit_requests_per_second=config.get("scm.gitlab.rate_limit_requests_per_second", 10.0),
            rate_limit_burst_size=config.get("scm.gitlab.rate_limit_burst_size"),
            # Postgres 限流配置
            postgres_rate_limit_enabled=config.get("scm.gitlab.postgres_rate_limit_enabled", False),
            postgres_rate_limit_dsn=postgres_dsn,
            postgres_rate_limit_rate=config.get("scm.gitlab.postgres_rate_limit_rate", 10.0),
            postgres_rate_limit_burst=config.get("scm.gitlab.postgres_rate_limit_burst", 20),
            postgres_rate_limit_max_wait=config.get("scm.gitlab.postgres_rate_limit_max_wait", 60.0),
        )


# 前向声明（避免循环导入）
Config = Any


# ============ GitLab 客户端 ============


class GitLabClient:
    """
    GitLab REST API 客户端

    特性:
    - 自动重试：支持指数退避和随机抖动
    - 429 处理：读取 Retry-After 头
    - 5xx/网络错误：按配置策略重试
    - 401/403：触发 TokenProvider.invalidate() 后重试一次
    - 结构化错误：返回详细的错误分类信息
    - 请求统计：记录 endpoint、耗时、status_code、重试次数、429 命中
    """

    def __init__(
        self,
        base_url: str,
        token_provider: Optional[TokenProvider] = None,
        private_token: Optional[str] = None,
        http_config: Optional[HttpConfig] = None,
        config: Optional[Config] = None,
        concurrency_limiter: Optional[ConcurrencyLimiter] = None,
        rate_limiter: Optional[RateLimiter] = None,
        postgres_rate_limiter: Optional["PostgresRateLimiter"] = None,
    ):
        """
        初始化 GitLab 客户端

        Args:
            base_url: GitLab 实例 URL（如 https://gitlab.example.com）
            token_provider: Token 提供者（支持 token 失效刷新）
            private_token: 静态 Private Token（与 token_provider 二选一）
            http_config: HTTP 配置（可选，默认从 config 加载）
            config: 配置对象（用于加载 HTTP 配置）
            concurrency_limiter: 并发限制器（可选，默认根据配置创建）
            rate_limiter: 速率限制器（可选，默认根据配置创建）
            postgres_rate_limiter: Postgres 限流器（可选，默认根据配置创建）
        """
        self.base_url = base_url.rstrip("/")

        # Token 提供者
        if token_provider:
            self.token_provider = token_provider
        elif private_token:
            self.token_provider = StaticTokenProvider(private_token)
        else:
            raise ValueError("必须提供 token_provider 或 private_token")

        # HTTP 配置
        if http_config:
            self.http_config = http_config
        else:
            self.http_config = HttpConfig.from_config(config)

        # 创建 Session
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })
        
        # 请求统计
        self.stats = ClientStats()
        
        # 并发限制器
        if concurrency_limiter is not None:
            self._concurrency_limiter = concurrency_limiter
        elif self.http_config.max_concurrency:
            self._concurrency_limiter = ConcurrencyLimiter(self.http_config.max_concurrency)
        else:
            self._concurrency_limiter = None
        
        # 速率限制器（内存版）
        if rate_limiter is not None:
            self._rate_limiter = rate_limiter
        elif self.http_config.rate_limit_enabled:
            self._rate_limiter = RateLimiter(
                requests_per_second=self.http_config.rate_limit_requests_per_second,
                burst_size=self.http_config.rate_limit_burst_size,
            )
        else:
            self._rate_limiter = None
        
        # Postgres 限流器（分布式版）
        if postgres_rate_limiter is not None:
            self._postgres_rate_limiter = postgres_rate_limiter
        elif self.http_config.postgres_rate_limit_enabled:
            # 从 base_url 提取实例标识
            instance_key = self._extract_instance_key(base_url)
            self._postgres_rate_limiter = PostgresRateLimiter(
                instance_key=instance_key,
                dsn=self.http_config.postgres_rate_limit_dsn,
                rate=self.http_config.postgres_rate_limit_rate,
                burst=self.http_config.postgres_rate_limit_burst,
                max_wait_seconds=self.http_config.postgres_rate_limit_max_wait,
            )
        else:
            self._postgres_rate_limiter = None
    
    def _extract_instance_key(self, url: str) -> str:
        """
        从 URL 提取实例标识
        
        生成规则: gitlab:<host>
        例如: https://gitlab.example.com -> gitlab:gitlab.example.com
        
        这确保同一 GitLab host 的所有请求共享同一个限流桶。
        
        Args:
            url: GitLab 实例 URL
            
        Returns:
            格式为 "gitlab:<host>" 的实例标识
        """
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            host = parsed.netloc or url
            return f"gitlab:{host}"
        except Exception:
            return f"gitlab:{url}"
    
    def _notify_all_rate_limiters(
        self,
        retry_after: Optional[float] = None,
        reset_time: Optional[float] = None,
    ) -> None:
        """
        通知所有速率限制器收到了 429 响应
        
        同时通知 local 和 distributed limiter，确保所有限流器状态同步。
        
        Args:
            retry_after: Retry-After 头的值（秒）
            reset_time: RateLimit-Reset 头的值（Unix 时间戳）
        """
        # 通知本地速率限制器
        if self._rate_limiter:
            try:
                self._rate_limiter.notify_rate_limit(
                    retry_after=retry_after,
                    reset_time=reset_time,
                )
            except Exception as e:
                logger.warning(f"通知本地速率限制器失败: {e}")
        
        # 通知分布式速率限制器（Postgres）
        if self._postgres_rate_limiter:
            try:
                self._postgres_rate_limiter.notify_rate_limit(
                    retry_after=retry_after,
                    reset_time=reset_time,
                )
            except Exception as e:
                logger.warning(f"通知 Postgres 限流器失败: {e}")
    
    @property
    def concurrency_limiter(self) -> Optional[ConcurrencyLimiter]:
        """获取并发限制器"""
        return self._concurrency_limiter
    
    @property
    def rate_limiter(self) -> Optional[RateLimiter]:
        """获取速率限制器（内存版）"""
        return self._rate_limiter
    
    @property
    def postgres_rate_limiter(self) -> Optional[PostgresRateLimiter]:
        """获取 Postgres 限流器（分布式版）"""
        return self._postgres_rate_limiter
    
    def get_limiter_stats(self) -> Dict[str, Any]:
        """
        获取所有 limiter 的统计信息
        
        聚合 local、distributed limiter 的统计数据。
        
        Returns:
            包含 timeout_count、avg_wait_time_ms 等统计的字典
        """
        stats: Dict[str, Any] = {
            "timeout_count": 0,
            "avg_wait_time_ms": 0.0,
            "total_limiter_requests": 0,
        }
        
        sub_stats = []
        total_wait_time_ms = 0.0
        total_requests = 0
        
        # 收集本地速率限制器统计
        if self._rate_limiter:
            try:
                local_stats = self._rate_limiter.get_stats()
                local_stats["type"] = "local"
                sub_stats.append(local_stats)
                
                total_requests += local_stats.get("total_requests", 0)
                total_wait_time_ms += local_stats.get("avg_wait_time_ms", 0) * local_stats.get("total_requests", 0)
                stats["timeout_count"] += local_stats.get("throttled_count", 0)
            except Exception as e:
                logger.debug(f"获取本地限流器统计失败: {e}")
        
        # 收集 Postgres 限流器统计
        if self._postgres_rate_limiter:
            try:
                pg_stats = self._postgres_rate_limiter.get_stats()
                sub_stats.append(pg_stats)
                
                total_requests += pg_stats.get("total_requests", 0)
                total_wait_time_ms += pg_stats.get("avg_wait_time_ms", 0) * pg_stats.get("total_requests", 0)
                stats["timeout_count"] += pg_stats.get("rejected_count", 0)
                stats["timeout_count"] += pg_stats.get("throttled_count", 0)
            except Exception as e:
                logger.debug(f"获取 Postgres 限流器统计失败: {e}")
        
        # 收集并发限制器统计
        if self._concurrency_limiter:
            try:
                conc_stats = self._concurrency_limiter.get_stats()
                conc_stats["type"] = "concurrency"
                sub_stats.append(conc_stats)
                
                # 并发限制器也有等待时间
                conc_wait_time = conc_stats.get("avg_wait_time_ms", 0) * conc_stats.get("total_acquired", 0)
                total_wait_time_ms += conc_wait_time
            except Exception as e:
                logger.debug(f"获取并发限制器统计失败: {e}")
        
        # 计算平均等待时间
        stats["total_limiter_requests"] = total_requests
        if total_requests > 0:
            stats["avg_wait_time_ms"] = round(total_wait_time_ms / total_requests, 2)
        
        if sub_stats:
            stats["sub_limiters"] = sub_stats
        
        return stats
    
    def update_stats_with_limiter_info(self) -> None:
        """
        将 limiter 统计更新到 ClientStats
        
        在需要获取完整统计信息前调用此方法。
        """
        limiter_stats = self.get_limiter_stats()
        self.stats.set_limiter_stats(
            timeout_count=limiter_stats.get("timeout_count", 0),
            avg_wait_time_ms=limiter_stats.get("avg_wait_time_ms", 0.0),
            limiter_stats=limiter_stats,
        )

    def _encode_project_id(self, project_id: str) -> str:
        """URL 编码项目 ID（处理 namespace/project 格式）"""
        if "/" in str(project_id):
            return quote(str(project_id), safe="")
        return str(project_id)

    def _calculate_backoff(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """
        计算退避等待时间（指数退避 + 随机抖动）

        Args:
            attempt: 当前尝试次数（从 1 开始）
            retry_after: 429 响应的 Retry-After 值（优先使用）

        Returns:
            等待时间（秒）
        """
        if retry_after is not None and retry_after > 0:
            # 使用服务器返回的 Retry-After，加上少量抖动
            jitter = random.uniform(0, 1)
            return retry_after + jitter

        # 指数退避：base * 2^(attempt-1) + jitter
        base = self.http_config.backoff_base_seconds
        max_backoff = self.http_config.backoff_max_seconds

        exp_backoff = base * (2 ** (attempt - 1))
        jitter = random.uniform(0, base)
        backoff = min(exp_backoff + jitter, max_backoff)

        return backoff

    def _parse_retry_after(self, response: requests.Response) -> Optional[float]:
        """解析 Retry-After 响应头"""
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None

        try:
            # 尝试解析为秒数
            return float(retry_after)
        except ValueError:
            # 可能是 HTTP 日期格式，暂不支持
            logger.warning(f"无法解析 Retry-After 头: {retry_after}")
            return None
    
    def _parse_rate_limit_headers(self, response: requests.Response) -> tuple:
        """
        解析 RateLimit 相关响应头
        
        GitLab 可能返回以下头:
        - RateLimit-Reset: Unix 时间戳，表示限流重置时间
        - RateLimit-Remaining: 剩余请求数
        - RateLimit-Limit: 时间窗口内的总请求限制
        
        Returns:
            (rate_limit_reset, rate_limit_remaining)
        """
        rate_limit_reset: Optional[float] = None
        rate_limit_remaining: Optional[int] = None
        
        # 解析 RateLimit-Reset（Unix 时间戳）
        reset_header = response.headers.get("RateLimit-Reset")
        if reset_header:
            try:
                rate_limit_reset = float(reset_header)
            except ValueError:
                logger.debug(f"无法解析 RateLimit-Reset 头: {reset_header}")
        
        # 解析 RateLimit-Remaining
        remaining_header = response.headers.get("RateLimit-Remaining")
        if remaining_header:
            try:
                rate_limit_remaining = int(remaining_header)
            except ValueError:
                logger.debug(f"无法解析 RateLimit-Remaining 头: {remaining_header}")
        
        return rate_limit_reset, rate_limit_remaining

    def _classify_error(
        self,
        exception: Exception,
        response: Optional[requests.Response] = None,
    ) -> tuple:
        """
        分类错误

        Returns:
            (GitLabErrorCategory, error_message, status_code, retry_after)
        """
        status_code = None
        retry_after = None

        if response is not None:
            status_code = response.status_code

        if isinstance(exception, requests.exceptions.Timeout):
            return (GitLabErrorCategory.TIMEOUT, "请求超时", status_code, None)

        if isinstance(exception, requests.exceptions.ConnectionError):
            return (GitLabErrorCategory.NETWORK_ERROR, f"连接错误: {redact(str(exception))}", status_code, None)

        if isinstance(exception, requests.exceptions.HTTPError) and response is not None:
            status_code = response.status_code

            # 提取错误消息
            error_msg = ""
            try:
                error_data = response.json()
                if isinstance(error_data, dict):
                    error_msg = error_data.get("message", error_data.get("error", str(error_data)))
                else:
                    error_msg = str(error_data)
            except Exception:
                error_msg = response.text[:500] if response.text else str(exception)

            if status_code == 429:
                retry_after = self._parse_retry_after(response)
                return (GitLabErrorCategory.RATE_LIMITED, f"限流 (429): {redact(error_msg)}", status_code, retry_after)

            if status_code in (401, 403):
                return (GitLabErrorCategory.AUTH_ERROR, f"认证错误 ({status_code}): {redact(error_msg)}", status_code, None)

            if 500 <= status_code < 600:
                return (GitLabErrorCategory.SERVER_ERROR, f"服务器错误 ({status_code}): {redact(error_msg)}", status_code, None)

            if 400 <= status_code < 500:
                return (GitLabErrorCategory.CLIENT_ERROR, f"客户端错误 ({status_code}): {redact(error_msg)}", status_code, None)

        if isinstance(exception, requests.exceptions.RequestException):
            return (GitLabErrorCategory.NETWORK_ERROR, f"请求失败: {redact(str(exception))}", status_code, None)

        return (GitLabErrorCategory.UNKNOWN, redact(str(exception)), status_code, None)

    def _should_retry(self, category: GitLabErrorCategory, attempt: int) -> bool:
        """判断是否应该重试"""
        if attempt >= self.http_config.max_attempts:
            return False

        # 可重试的错误类型
        retryable = {
            GitLabErrorCategory.TIMEOUT,
            GitLabErrorCategory.RATE_LIMITED,
            GitLabErrorCategory.SERVER_ERROR,
            GitLabErrorCategory.NETWORK_ERROR,
        }
        return category in retryable

    def _request(
        self,
        method: str,
        endpoint: str,
        raise_on_error: bool = True,
        **kwargs
    ) -> GitLabAPIResult:
        """
        发送 HTTP 请求（带自动重试）

        Args:
            method: HTTP 方法
            endpoint: API 端点（如 /projects/:id/commits）
            raise_on_error: 失败时是否抛出异常（False 时返回错误结果）
            **kwargs: 传递给 requests 的参数

        Returns:
            GitLabAPIResult 对象

        Raises:
            GitLabAPIError: 当 raise_on_error=True 且请求失败时
        """
        url = f"{self.base_url}/api/v4{endpoint}"
        kwargs.setdefault("timeout", self.http_config.timeout_seconds)

        # 获取并发槽位
        if self._concurrency_limiter:
            self._concurrency_limiter.acquire()
        
        # 获取速率限制令牌（优先使用 Postgres 限流器）
        if self._postgres_rate_limiter:
            if not self._postgres_rate_limiter.acquire():
                # Postgres 限流器拒绝请求
                if self._concurrency_limiter:
                    self._concurrency_limiter.release()
                
                result = GitLabAPIResult(
                    success=False,
                    endpoint=url,
                    error_category=GitLabErrorCategory.RATE_LIMITED,
                    error_message="Postgres 限流器: 请求被限流（等待超时）",
                )
                if raise_on_error:
                    self._raise_error(result)
                return result
        elif self._rate_limiter:
            self._rate_limiter.acquire()
        
        try:
            return self._do_request(method, endpoint, url, raise_on_error, **kwargs)
        finally:
            # 释放并发槽位
            if self._concurrency_limiter:
                self._concurrency_limiter.release()
    
    def _do_request(
        self,
        method: str,
        endpoint: str,
        url: str,
        raise_on_error: bool,
        **kwargs
    ) -> GitLabAPIResult:
        """实际执行 HTTP 请求（带重试逻辑）"""
        attempt = 0
        last_result: Optional[GitLabAPIResult] = None
        auth_retry_attempted = False
        hit_429 = False
        last_retry_after: Optional[float] = None
        last_rate_limit_reset: Optional[float] = None
        last_rate_limit_remaining: Optional[int] = None
        start_time = time.time()

        while attempt < self.http_config.max_attempts:
            attempt += 1

            # 更新 token
            token = self.token_provider.get_token()
            headers = kwargs.pop("headers", {})
            headers["PRIVATE-TOKEN"] = token
            kwargs["headers"] = headers

            response = None
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()

                # 解析 JSON 响应
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    data = response.text

                result = GitLabAPIResult(
                    success=True,
                    data=data,
                    response=response,
                    status_code=response.status_code,
                    endpoint=url,
                )
                
                # 记录成功的请求统计
                duration_ms = (time.time() - start_time) * 1000
                self.stats.record(RequestStats(
                    endpoint=endpoint,
                    method=method,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                    attempt_count=attempt,
                    hit_429=hit_429,
                    success=True,
                    retry_after=last_retry_after,
                    rate_limit_reset=last_rate_limit_reset,
                    rate_limit_remaining=last_rate_limit_remaining,
                ))
                
                return result

            except Exception as e:
                category, error_msg, status_code, retry_after = self._classify_error(e, response)
                
                # 解析更多限流头信息
                rate_limit_reset: Optional[float] = None
                rate_limit_remaining: Optional[int] = None
                if response is not None:
                    rate_limit_reset, rate_limit_remaining = self._parse_rate_limit_headers(response)
                
                # 记录是否命中 429
                if category == GitLabErrorCategory.RATE_LIMITED:
                    hit_429 = True
                    last_retry_after = retry_after
                    last_rate_limit_reset = rate_limit_reset
                    last_rate_limit_remaining = rate_limit_remaining
                    
                    # 通知所有速率限制器（local + distributed 都要通知）
                    self._notify_all_rate_limiters(
                        retry_after=retry_after,
                        reset_time=rate_limit_reset,
                    )

                last_result = GitLabAPIResult(
                    success=False,
                    response=response,
                    status_code=status_code,
                    endpoint=url,
                    error_category=category,
                    error_message=error_msg,
                    retry_after=retry_after,
                    rate_limit_reset=rate_limit_reset,
                    rate_limit_remaining=rate_limit_remaining,
                )

                # 认证错误特殊处理：invalidate token 后重试一次
                if category == GitLabErrorCategory.AUTH_ERROR and not auth_retry_attempted:
                    logger.warning(f"认证错误，尝试刷新 token 后重试: {redact(error_msg)}")
                    self.token_provider.invalidate()
                    auth_retry_attempted = True
                    # 不增加 attempt 计数，立即重试
                    continue

                # 判断是否应该重试
                if self._should_retry(category, attempt):
                    backoff = self._calculate_backoff(attempt, retry_after)
                    logger.warning(
                        f"请求失败 (attempt={attempt}/{self.http_config.max_attempts}), "
                        f"category={category.value}, {backoff:.2f}s 后重试: {redact(error_msg)}"
                    )
                    time.sleep(backoff)
                    continue

                # 不可重试或已达最大重试次数
                break

        # 记录失败的请求统计
        duration_ms = (time.time() - start_time) * 1000
        error_cat = last_result.error_category.value if last_result and last_result.error_category else "unknown"
        self.stats.record(RequestStats(
            endpoint=endpoint,
            method=method,
            status_code=last_result.status_code if last_result else None,
            duration_ms=duration_ms,
            attempt_count=attempt,
            hit_429=hit_429,
            success=False,
            error_category=error_cat,
            retry_after=last_retry_after,
            rate_limit_reset=last_rate_limit_reset,
            rate_limit_remaining=last_rate_limit_remaining,
        ))

        # 返回最后一次结果
        if last_result and not last_result.success:
            if raise_on_error:
                # 根据错误类型抛出对应异常
                self._raise_error(last_result)
            return last_result

        # 不应该到达这里
        return last_result or GitLabAPIResult(
            success=False,
            endpoint=url,
            error_category=GitLabErrorCategory.UNKNOWN,
            error_message="未知错误",
        )

    def _raise_error(self, result: GitLabAPIResult) -> None:
        """根据结果抛出对应的异常"""
        kwargs = {
            "details": result.to_dict(),
            "status_code": result.status_code,
            "endpoint": result.endpoint,
        }

        if result.error_category == GitLabErrorCategory.RATE_LIMITED:
            raise GitLabRateLimitError(
                result.error_message or "限流",
                retry_after=result.retry_after,
                **kwargs,
            )
        elif result.error_category == GitLabErrorCategory.AUTH_ERROR:
            raise GitLabAuthError(result.error_message or "认证失败", **kwargs)
        elif result.error_category == GitLabErrorCategory.SERVER_ERROR:
            raise GitLabServerError(result.error_message or "服务器错误", **kwargs)
        elif result.error_category == GitLabErrorCategory.TIMEOUT:
            raise GitLabTimeoutError(result.error_message or "请求超时", **kwargs)
        elif result.error_category == GitLabErrorCategory.NETWORK_ERROR:
            raise GitLabNetworkError(result.error_message or "网络错误", **kwargs)
        else:
            raise GitLabAPIError(
                result.error_message or "API 错误",
                category=result.error_category,
                **kwargs,
            )

    def request_safe(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> GitLabAPIResult:
        """
        发送 HTTP 请求（不抛异常版本）

        适用于需要自行处理错误的场景（如降级逻辑）。

        Args:
            method: HTTP 方法
            endpoint: API 端点
            **kwargs: 传递给 requests 的参数

        Returns:
            GitLabAPIResult 对象（检查 .success 判断是否成功）
        """
        return self._request(method, endpoint, raise_on_error=False, **kwargs)

    # ============ 高层 API 方法 ============

    def get_commits(
        self,
        project_id: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        ref_name: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        获取项目的 commits

        API: GET /projects/:id/repository/commits

        Args:
            project_id: 项目 ID 或路径
            since: ISO 8601 格式的起始时间
            until: ISO 8601 格式的结束时间
            ref_name: 分支/tag 名称
            per_page: 每页数量
            page: 页码

        Returns:
            commits 列表
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/repository/commits"

        params = {
            "per_page": per_page,
            "page": page,
            "with_stats": "true",
        }
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if ref_name:
            params["ref_name"] = ref_name

        result = self._request("GET", endpoint, params=params)
        return result.data or []

    def get_commit_diff(self, project_id: str, sha: str) -> List[Dict[str, Any]]:
        """
        获取 commit 的 diff

        API: GET /projects/:id/repository/commits/:sha/diff

        Args:
            project_id: 项目 ID 或路径
            sha: commit SHA

        Returns:
            diff 列表
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/repository/commits/{sha}/diff"

        result = self._request("GET", endpoint)
        return result.data or []

    def get_commit_diff_safe(
        self,
        project_id: str,
        sha: str,
        max_size_bytes: int = 10 * 1024 * 1024,
    ) -> GitLabAPIResult:
        """
        安全获取 commit 的 diff（支持降级）

        返回 GitLabAPIResult 而非抛出异常，便于降级处理。

        Args:
            project_id: 项目 ID 或路径
            sha: commit SHA
            max_size_bytes: 内容最大字节数

        Returns:
            GitLabAPIResult 对象
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/repository/commits/{sha}/diff"

        result = self.request_safe("GET", endpoint)

        if not result.success:
            return result

        # 检查响应大小
        if result.response:
            content_length = len(result.response.content)
            if content_length > max_size_bytes:
                return GitLabAPIResult(
                    success=False,
                    response=result.response,
                    status_code=result.status_code,
                    endpoint=result.endpoint,
                    error_category=GitLabErrorCategory.CONTENT_TOO_LARGE,
                    error_message=f"diff 内容过大: {content_length} bytes > {max_size_bytes} bytes",
                )

        return result

    def get_merge_requests(
        self,
        project_id: str,
        updated_after: Optional[str] = None,
        state: Optional[str] = None,
        per_page: int = 100,
        page: int = 1,
        order_by: str = "updated_at",
        sort: str = "asc",
    ) -> List[Dict[str, Any]]:
        """
        获取项目的 merge requests

        API: GET /projects/:id/merge_requests

        Args:
            project_id: 项目 ID 或路径
            updated_after: ISO 8601 格式的起始时间
            state: MR 状态 (all/opened/closed/merged)
            per_page: 每页数量
            page: 页码
            order_by: 排序字段
            sort: 排序方向

        Returns:
            merge requests 列表
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests"

        params = {
            "per_page": per_page,
            "page": page,
            "order_by": order_by,
            "sort": sort,
        }
        if updated_after:
            params["updated_after"] = updated_after
        if state:
            params["state"] = state

        result = self._request("GET", endpoint, params=params)
        return result.data or []

    def get_merge_request_detail(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        """
        获取单个 merge request 的详细信息

        API: GET /projects/:id/merge_requests/:merge_request_iid
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}"

        result = self._request("GET", endpoint)
        return result.data or {}

    def get_merge_request_changes(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        """
        获取 merge request 的变更信息

        API: GET /projects/:id/merge_requests/:merge_request_iid/changes
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}/changes"

        result = self._request("GET", endpoint)
        return result.data or {}

    def get_mr_discussions(
        self,
        project_id: str,
        mr_iid: int,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        获取 MR 的 discussions

        API: GET /projects/:id/merge_requests/:merge_request_iid/discussions
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}/discussions"

        params = {"per_page": per_page, "page": page}
        result = self._request("GET", endpoint, params=params)
        return result.data or []

    def get_mr_notes(
        self,
        project_id: str,
        mr_iid: int,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        获取 MR 的 notes

        API: GET /projects/:id/merge_requests/:merge_request_iid/notes
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}/notes"

        params = {"per_page": per_page, "page": page, "sort": "asc"}
        result = self._request("GET", endpoint, params=params)
        return result.data or []

    def get_mr_approvals(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        """
        获取 MR 的 approval 状态

        API: GET /projects/:id/merge_requests/:merge_request_iid/approvals
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}/approvals"

        result = self._request("GET", endpoint)
        return result.data or {}

    def get_mr_resource_state_events(
        self,
        project_id: str,
        mr_iid: int,
        per_page: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        获取 MR 的状态变更事件

        API: GET /projects/:id/merge_requests/:merge_request_iid/resource_state_events
        """
        encoded_id = self._encode_project_id(project_id)
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}/resource_state_events"

        params = {"per_page": per_page, "page": page}
        result = self._request("GET", endpoint, params=params)
        return result.data or []


# ============ 工厂函数 ============


def create_gitlab_client(
    base_url: Optional[str] = None,
    private_token: Optional[str] = None,
    config: Optional[Config] = None,
) -> GitLabClient:
    """
    创建 GitLab 客户端（工厂函数）

    优先使用传入参数，其次从配置读取。

    Args:
        base_url: GitLab URL（可选，从配置读取）
        private_token: Private Token（可选，从配置/环境变量读取）
        config: 配置对象

    Returns:
        GitLabClient 实例
    """
    import os

    # 延迟导入避免循环依赖
    from .config import get_gitlab_config

    if config is None:
        from .config import get_config
        config = get_config()

    gitlab_cfg = get_gitlab_config(config)

    url = base_url or gitlab_cfg.get("url")
    token = private_token or gitlab_cfg.get("private_token") or os.environ.get("GITLAB_TOKEN")

    if not url:
        raise ValueError("缺少 GitLab URL，请配置 scm.gitlab.url 或传入 base_url 参数")
    if not token:
        raise ValueError("缺少 GitLab Token，请配置 scm.gitlab.token、设置 GITLAB_TOKEN 环境变量或传入 private_token 参数")

    return GitLabClient(
        base_url=url,
        private_token=token,
        config=config,
    )
