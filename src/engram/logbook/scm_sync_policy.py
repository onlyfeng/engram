"""
engram_logbook.scm_sync_policy - SCM 同步降级策略控制器

功能:
- 根据 client.stats、unrecoverable_errors、bulk/degraded 分布，输出下一轮同步建议
- 支持动态调整 diff_mode、batch_size、sleep_seconds、forward_window_seconds 等参数
- 实现自适应退避策略，在连续错误时自动降级
- 提供统一的熔断 key 构建函数，确保 scheduler/worker 使用一致的 key

使用示例:
    controller = DegradationController()
    
    # 每轮同步后更新控制器
    suggestion = controller.update(
        request_stats=client.stats.to_dict(),
        unrecoverable_errors=result.get("unrecoverable_errors", []),
        degraded_count=result.get("degraded_count", 0),
        bulk_count=result.get("bulk_count", 0),
    )
    
    # 根据建议调整下一轮参数
    if suggestion.diff_mode == "none":
        sync_config.diff_mode = DiffMode.NONE
    sync_config.batch_size = suggestion.batch_size
    time.sleep(suggestion.sleep_seconds)
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ============ 错误类型枚举 ============


class ErrorType(str, Enum):
    """可识别的错误类型"""
    RATE_LIMITED = "rate_limited"       # 429 限流
    TIMEOUT = "timeout"                  # 请求超时
    CONTENT_TOO_LARGE = "content_too_large"  # 内容过大
    SERVER_ERROR = "server_error"        # 5xx 错误
    AUTH_ERROR = "auth_error"            # 认证错误
    NETWORK_ERROR = "network_error"      # 网络错误
    UNKNOWN = "unknown"


# ============ 降级建议 ============


@dataclass
class DegradationSuggestion:
    """
    降级建议数据结构
    
    包含下一轮同步应使用的参数建议
    """
    # 核心参数建议
    diff_mode: str = "best_effort"       # always / best_effort / none
    batch_size: int = 100                # 批量大小
    sleep_seconds: float = 0.0           # 下一轮前应等待的秒数
    forward_window_seconds: int = 3600   # 前向窗口秒数
    
    # 附加标志
    should_pause: bool = False           # 是否应暂停同步
    pause_reason: Optional[str] = None   # 暂停原因
    
    # 调整原因（用于日志）
    adjustment_reasons: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "diff_mode": self.diff_mode,
            "batch_size": self.batch_size,
            "sleep_seconds": self.sleep_seconds,
            "forward_window_seconds": self.forward_window_seconds,
            "should_pause": self.should_pause,
            "pause_reason": self.pause_reason,
            "adjustment_reasons": self.adjustment_reasons,
        }


# ============ 控制器配置 ============


@dataclass
class DegradationConfig:
    """
    降级控制器配置
    
    可通过配置文件加载，或使用默认值
    """
    # 批量大小边界
    min_batch_size: int = 10
    max_batch_size: int = 500
    default_batch_size: int = 100
    
    # 批量大小调整因子
    batch_shrink_factor: float = 0.5     # 缩小因子
    batch_grow_factor: float = 1.2       # 增长因子
    
    # 前向窗口边界
    min_forward_window_seconds: int = 300       # 5 分钟
    max_forward_window_seconds: int = 86400     # 24 小时
    default_forward_window_seconds: int = 3600  # 1 小时
    
    # 窗口调整因子
    window_shrink_factor: float = 0.5
    window_grow_factor: float = 1.5
    
    # 连续错误阈值
    rate_limit_threshold: int = 3        # 连续 429 次数触发 diff_mode=none
    timeout_threshold: int = 3           # 连续超时次数触发暂停
    content_too_large_threshold: int = 5 # 连续内容过大次数触发 diff_mode=none
    
    # 退避配置
    base_sleep_seconds: float = 1.0
    max_sleep_seconds: float = 300.0     # 最大等待 5 分钟
    
    # 恢复阈值
    recovery_success_count: int = 5      # 连续成功多少次后恢复
    
    @classmethod
    def from_config(cls, config: Optional[Any] = None) -> "DegradationConfig":
        """从配置对象加载"""
        if config is None:
            return cls()
        
        return cls(
            min_batch_size=config.get("scm.degradation.min_batch_size", 10),
            max_batch_size=config.get("scm.degradation.max_batch_size", 500),
            default_batch_size=config.get("scm.degradation.default_batch_size", 100),
            batch_shrink_factor=config.get("scm.degradation.batch_shrink_factor", 0.5),
            batch_grow_factor=config.get("scm.degradation.batch_grow_factor", 1.2),
            min_forward_window_seconds=config.get("scm.degradation.min_forward_window_seconds", 300),
            max_forward_window_seconds=config.get("scm.degradation.max_forward_window_seconds", 86400),
            default_forward_window_seconds=config.get("scm.degradation.default_forward_window_seconds", 3600),
            window_shrink_factor=config.get("scm.degradation.window_shrink_factor", 0.5),
            window_grow_factor=config.get("scm.degradation.window_grow_factor", 1.5),
            rate_limit_threshold=config.get("scm.degradation.rate_limit_threshold", 3),
            timeout_threshold=config.get("scm.degradation.timeout_threshold", 3),
            content_too_large_threshold=config.get("scm.degradation.content_too_large_threshold", 5),
            base_sleep_seconds=config.get("scm.degradation.base_sleep_seconds", 1.0),
            max_sleep_seconds=config.get("scm.degradation.max_sleep_seconds", 300.0),
            recovery_success_count=config.get("scm.degradation.recovery_success_count", 5),
        )


# ============ 降级控制器 ============


class DegradationController:
    """
    SCM 同步降级策略控制器
    
    根据同步结果统计，动态调整下一轮同步参数：
    - diff_mode: always -> best_effort -> none
    - batch_size: 根据错误率调整
    - sleep_seconds: 指数退避
    - forward_window_seconds: 自适应窗口
    
    状态跟踪:
    - 连续错误计数（分类型）
    - 连续成功计数
    - 当前降级级别
    """
    
    def __init__(
        self,
        config: Optional[DegradationConfig] = None,
        initial_diff_mode: str = "best_effort",
        initial_batch_size: Optional[int] = None,
        initial_forward_window_seconds: Optional[int] = None,
    ):
        """
        初始化控制器
        
        Args:
            config: 降级配置
            initial_diff_mode: 初始 diff 模式
            initial_batch_size: 初始批量大小
            initial_forward_window_seconds: 初始前向窗口
        """
        self._config = config or DegradationConfig()
        
        # 当前参数状态
        self._current_diff_mode = initial_diff_mode
        self._current_batch_size = initial_batch_size or self._config.default_batch_size
        self._current_forward_window = initial_forward_window_seconds or self._config.default_forward_window_seconds
        
        # 错误计数器（分类型）
        self._consecutive_errors: Dict[str, int] = {
            ErrorType.RATE_LIMITED.value: 0,
            ErrorType.TIMEOUT.value: 0,
            ErrorType.CONTENT_TOO_LARGE.value: 0,
            ErrorType.SERVER_ERROR.value: 0,
        }
        
        # 成功计数器（用于恢复）
        self._consecutive_success_count = 0
        
        # 累计统计
        self._total_429_hits = 0
        self._total_timeouts = 0
        self._total_degraded = 0
        self._total_bulk = 0
        self._update_count = 0
        
        # 暂停状态
        self._paused_until: Optional[float] = None
        self._pause_reason: Optional[str] = None
    
    @property
    def current_diff_mode(self) -> str:
        """当前 diff 模式"""
        return self._current_diff_mode
    
    @property
    def current_batch_size(self) -> int:
        """当前批量大小"""
        return self._current_batch_size
    
    @property
    def current_forward_window_seconds(self) -> int:
        """当前前向窗口"""
        return self._current_forward_window
    
    @property
    def is_paused(self) -> bool:
        """是否处于暂停状态"""
        if self._paused_until is None:
            return False
        return time.time() < self._paused_until
    
    @property
    def consecutive_rate_limit_count(self) -> int:
        """连续 429 计数"""
        return self._consecutive_errors.get(ErrorType.RATE_LIMITED.value, 0)
    
    @property
    def consecutive_timeout_count(self) -> int:
        """连续超时计数"""
        return self._consecutive_errors.get(ErrorType.TIMEOUT.value, 0)
    
    @property
    def consecutive_content_too_large_count(self) -> int:
        """连续内容过大计数"""
        return self._consecutive_errors.get(ErrorType.CONTENT_TOO_LARGE.value, 0)
    
    def _classify_errors(self, unrecoverable_errors: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        对错误进行分类计数
        
        Args:
            unrecoverable_errors: 不可恢复错误列表
            
        Returns:
            错误类型到计数的映射
        """
        counts: Dict[str, int] = {}
        
        for error in unrecoverable_errors:
            category = error.get("error_category", "unknown")
            status_code = error.get("status_code")
            
            # 根据 category 和 status_code 分类
            if category == "rate_limited" or status_code == 429:
                key = ErrorType.RATE_LIMITED.value
            elif category == "timeout":
                key = ErrorType.TIMEOUT.value
            elif category == "content_too_large":
                key = ErrorType.CONTENT_TOO_LARGE.value
            elif category in ("http_error", "server_error") or (status_code and 500 <= status_code < 600):
                key = ErrorType.SERVER_ERROR.value
            else:
                key = ErrorType.UNKNOWN.value
            
            counts[key] = counts.get(key, 0) + 1
        
        return counts
    
    def _update_consecutive_counts(
        self,
        error_counts: Dict[str, int],
        had_success: bool,
    ) -> None:
        """
        更新连续错误/成功计数
        
        Args:
            error_counts: 本轮错误类型计数
            had_success: 本轮是否有成功处理
        """
        for error_type in self._consecutive_errors:
            if error_counts.get(error_type, 0) > 0:
                # 有此类错误，增加计数
                self._consecutive_errors[error_type] += error_counts[error_type]
            elif had_success:
                # 无此类错误且有成功，重置计数
                self._consecutive_errors[error_type] = 0
        
        # 更新成功计数
        if had_success and sum(error_counts.values()) == 0:
            self._consecutive_success_count += 1
        else:
            self._consecutive_success_count = 0
    
    def _calculate_sleep(self, error_counts: Dict[str, int], retry_after: Optional[float] = None) -> float:
        """
        计算下一轮前应等待的秒数
        
        Args:
            error_counts: 本轮错误类型计数
            retry_after: 服务端返回的 Retry-After 值
            
        Returns:
            等待秒数
        """
        # 优先使用服务端的 Retry-After
        if retry_after and retry_after > 0:
            return min(retry_after, self._config.max_sleep_seconds)
        
        # 根据连续错误计数计算退避
        rate_limit_count = self._consecutive_errors.get(ErrorType.RATE_LIMITED.value, 0)
        
        if rate_limit_count > 0:
            # 指数退避: base * 2^(count-1)
            sleep = self._config.base_sleep_seconds * (2 ** (rate_limit_count - 1))
            return min(sleep, self._config.max_sleep_seconds)
        
        return 0.0
    
    def _adjust_diff_mode(self, reasons: List[str]) -> str:
        """
        根据连续错误调整 diff_mode
        
        策略:
        - 连续 rate_limit >= threshold: 降级到 none
        - 连续 content_too_large >= threshold: 降级到 none
        - 连续成功 >= recovery_threshold: 尝试恢复
        
        Args:
            reasons: 调整原因列表（会被修改）
            
        Returns:
            新的 diff_mode
        """
        rate_limit_count = self._consecutive_errors.get(ErrorType.RATE_LIMITED.value, 0)
        content_large_count = self._consecutive_errors.get(ErrorType.CONTENT_TOO_LARGE.value, 0)
        
        new_mode = self._current_diff_mode
        
        # 检查是否应降级到 none
        if rate_limit_count >= self._config.rate_limit_threshold:
            if new_mode != "none":
                new_mode = "none"
                reasons.append(f"rate_limit_count={rate_limit_count}>=threshold={self._config.rate_limit_threshold}")
                logger.warning(f"连续 429 达到阈值 ({rate_limit_count})，diff_mode 降级为 none")
        
        if content_large_count >= self._config.content_too_large_threshold:
            if new_mode != "none":
                new_mode = "none"
                reasons.append(f"content_too_large_count={content_large_count}>=threshold={self._config.content_too_large_threshold}")
                logger.warning(f"连续 content_too_large 达到阈值 ({content_large_count})，diff_mode 降级为 none")
        
        # 检查是否可以恢复
        if new_mode == "none" and self._consecutive_success_count >= self._config.recovery_success_count:
            new_mode = "best_effort"
            reasons.append(f"consecutive_success={self._consecutive_success_count}>=recovery_threshold={self._config.recovery_success_count}")
            logger.info(f"连续成功 {self._consecutive_success_count} 次，diff_mode 恢复为 best_effort")
        
        return new_mode
    
    def _adjust_batch_size(self, error_counts: Dict[str, int], reasons: List[str]) -> int:
        """
        根据错误情况调整 batch_size
        
        策略:
        - 有 429 错误: 缩小
        - 有超时错误: 缩小
        - 连续成功: 逐渐恢复
        
        Args:
            error_counts: 本轮错误类型计数
            reasons: 调整原因列表（会被修改）
            
        Returns:
            新的 batch_size
        """
        new_batch_size = self._current_batch_size
        
        # 有 429 或超时错误，缩小 batch_size
        rate_limit_count = error_counts.get(ErrorType.RATE_LIMITED.value, 0)
        timeout_count = error_counts.get(ErrorType.TIMEOUT.value, 0)
        
        if rate_limit_count > 0 or timeout_count > 0:
            new_batch_size = int(self._current_batch_size * self._config.batch_shrink_factor)
            new_batch_size = max(new_batch_size, self._config.min_batch_size)
            if new_batch_size != self._current_batch_size:
                reasons.append(f"batch_size shrink: {self._current_batch_size}->{new_batch_size} (429={rate_limit_count}, timeout={timeout_count})")
        
        # 连续成功，尝试恢复 batch_size
        elif self._consecutive_success_count >= self._config.recovery_success_count:
            if self._current_batch_size < self._config.default_batch_size:
                new_batch_size = int(self._current_batch_size * self._config.batch_grow_factor)
                new_batch_size = min(new_batch_size, self._config.default_batch_size)
                if new_batch_size != self._current_batch_size:
                    reasons.append(f"batch_size grow: {self._current_batch_size}->{new_batch_size} (consecutive_success={self._consecutive_success_count})")
        
        return new_batch_size
    
    def _adjust_forward_window(self, error_counts: Dict[str, int], reasons: List[str]) -> int:
        """
        根据错误情况调整 forward_window
        
        策略:
        - 有 429 错误: 缩小窗口（减少单次请求量）
        - 连续成功: 逐渐恢复
        
        Args:
            error_counts: 本轮错误类型计数
            reasons: 调整原因列表（会被修改）
            
        Returns:
            新的 forward_window_seconds
        """
        new_window = self._current_forward_window
        
        rate_limit_count = error_counts.get(ErrorType.RATE_LIMITED.value, 0)
        
        if rate_limit_count > 0:
            new_window = int(self._current_forward_window * self._config.window_shrink_factor)
            new_window = max(new_window, self._config.min_forward_window_seconds)
            if new_window != self._current_forward_window:
                reasons.append(f"forward_window shrink: {self._current_forward_window}->{new_window}")
        
        elif self._consecutive_success_count >= self._config.recovery_success_count:
            if self._current_forward_window < self._config.default_forward_window_seconds:
                new_window = int(self._current_forward_window * self._config.window_grow_factor)
                new_window = min(new_window, self._config.default_forward_window_seconds)
                if new_window != self._current_forward_window:
                    reasons.append(f"forward_window grow: {self._current_forward_window}->{new_window}")
        
        return new_window
    
    def _check_pause_condition(self, error_counts: Dict[str, int]) -> tuple:
        """
        检查是否应暂停同步
        
        策略:
        - 连续超时 >= threshold: 暂停
        - 连续服务器错误 >= threshold: 暂停
        
        Args:
            error_counts: 本轮错误类型计数
            
        Returns:
            (should_pause, pause_reason)
        """
        timeout_count = self._consecutive_errors.get(ErrorType.TIMEOUT.value, 0)
        server_error_count = self._consecutive_errors.get(ErrorType.SERVER_ERROR.value, 0)
        
        if timeout_count >= self._config.timeout_threshold:
            return (True, f"consecutive_timeout={timeout_count}>=threshold={self._config.timeout_threshold}")
        
        if server_error_count >= self._config.timeout_threshold:
            return (True, f"consecutive_server_error={server_error_count}>=threshold={self._config.timeout_threshold}")
        
        return (False, None)
    
    def update(
        self,
        request_stats: Optional[Dict[str, Any]] = None,
        unrecoverable_errors: Optional[List[Dict[str, Any]]] = None,
        degraded_count: int = 0,
        bulk_count: int = 0,
        synced_count: int = 0,
        retry_after: Optional[float] = None,
    ) -> DegradationSuggestion:
        """
        根据本轮同步结果更新控制器状态，返回下一轮建议
        
        Args:
            request_stats: 请求统计（来自 client.stats.to_dict()）
            unrecoverable_errors: 不可恢复错误列表
            degraded_count: 降级处理数量
            bulk_count: bulk commit 数量
            synced_count: 成功同步数量
            retry_after: 服务端返回的 Retry-After 值
            
        Returns:
            DegradationSuggestion 对象
        """
        self._update_count += 1
        unrecoverable_errors = unrecoverable_errors or []
        request_stats = request_stats or {}
        
        # 从 request_stats 提取信息
        total_429_hits = request_stats.get("total_429_hits", 0)
        last_retry_after = request_stats.get("last_retry_after") or retry_after
        
        # 累计统计
        self._total_429_hits += total_429_hits
        self._total_degraded += degraded_count
        self._total_bulk += bulk_count
        
        # 分类错误
        error_counts = self._classify_errors(unrecoverable_errors)
        
        # 判断是否有成功处理
        had_success = synced_count > 0
        
        # 更新连续计数
        self._update_consecutive_counts(error_counts, had_success)
        
        # 构建调整原因列表
        reasons: List[str] = []
        
        # 调整各参数
        new_diff_mode = self._adjust_diff_mode(reasons)
        new_batch_size = self._adjust_batch_size(error_counts, reasons)
        new_forward_window = self._adjust_forward_window(error_counts, reasons)
        sleep_seconds = self._calculate_sleep(error_counts, last_retry_after)
        
        # 检查暂停条件
        should_pause, pause_reason = self._check_pause_condition(error_counts)
        
        if should_pause:
            # 设置暂停时间
            pause_duration = min(
                self._config.base_sleep_seconds * (2 ** self._consecutive_errors.get(ErrorType.TIMEOUT.value, 1)),
                self._config.max_sleep_seconds,
            )
            self._paused_until = time.time() + pause_duration
            self._pause_reason = pause_reason
            sleep_seconds = max(sleep_seconds, pause_duration)
            reasons.append(f"pause: {pause_reason}, duration={pause_duration:.1f}s")
            logger.warning(f"触发暂停策略: {pause_reason}，暂停 {pause_duration:.1f}s")
        
        # 更新当前状态
        self._current_diff_mode = new_diff_mode
        self._current_batch_size = new_batch_size
        self._current_forward_window = new_forward_window
        
        # 构建建议
        suggestion = DegradationSuggestion(
            diff_mode=new_diff_mode,
            batch_size=new_batch_size,
            sleep_seconds=sleep_seconds,
            forward_window_seconds=new_forward_window,
            should_pause=should_pause,
            pause_reason=pause_reason,
            adjustment_reasons=reasons,
        )
        
        if reasons:
            logger.info(f"DegradationController 调整: {', '.join(reasons)}")
        
        return suggestion
    
    def reset(self) -> None:
        """重置控制器状态"""
        for key in self._consecutive_errors:
            self._consecutive_errors[key] = 0
        self._consecutive_success_count = 0
        self._paused_until = None
        self._pause_reason = None
        self._current_diff_mode = "best_effort"
        self._current_batch_size = self._config.default_batch_size
        self._current_forward_window = self._config.default_forward_window_seconds
        logger.info("DegradationController 已重置")
    
    def get_state(self) -> Dict[str, Any]:
        """获取当前状态（用于调试/日志）"""
        return {
            "current_diff_mode": self._current_diff_mode,
            "current_batch_size": self._current_batch_size,
            "current_forward_window_seconds": self._current_forward_window,
            "consecutive_errors": dict(self._consecutive_errors),
            "consecutive_success_count": self._consecutive_success_count,
            "is_paused": self.is_paused,
            "pause_reason": self._pause_reason,
            "total_429_hits": self._total_429_hits,
            "total_degraded": self._total_degraded,
            "total_bulk": self._total_bulk,
            "update_count": self._update_count,
        }


# ============ SVN 专用控制器 ============


# ============ 调度器配置 ============


@dataclass
class SchedulerConfig:
    """
    SCM 同步调度器配置
    
    支持多级并发控制和错误预算管理
    
    并发控制语义说明:
    - max_running: 全局最大同时运行的任务数（status='running'）
    - max_queue_depth: 全局最大队列深度（status='pending' + 'running'）
    - global_concurrency: 向后兼容，等价于 max_queue_depth
    
    调度决策逻辑:
    1. 如果当前 running_count >= max_running，不入队新任务（避免过载）
    2. 如果当前 active_count >= max_queue_depth，不入队新任务（队列已满）
    3. 每个 GitLab 实例最多 per_instance_concurrency 个活跃任务
    4. 每个租户最多 per_tenant_concurrency 个活跃任务
    """
    # 全局最大运行任务数（仅统计 status='running'）
    max_running: int = 5
    
    # 全局最大队列深度（统计 status IN ('pending', 'running')）
    max_queue_depth: int = 10
    
    # 向后兼容: global_concurrency 等价于 max_queue_depth
    # @deprecated 请使用 max_queue_depth
    global_concurrency: int = 10
    
    # 每 GitLab 实例并发限制（活跃任务数）
    per_instance_concurrency: int = 3
    
    # 每租户并发限制（活跃任务数）
    per_tenant_concurrency: int = 5
    
    # 回填修复窗口（小时）
    backfill_repair_window_hours: int = 24
    
    # 最大回填窗口（小时）- 限制单次回填的最大时间跨度
    max_backfill_window_hours: int = 168  # 7 天
    
    # 游标年龄阈值（秒）- 超过此值认为需要同步
    cursor_age_threshold_seconds: int = 3600  # 1 小时
    
    # 错误预算阈值 - 最近运行中失败率超过此值则暂停
    error_budget_threshold: float = 0.3  # 30%
    
    # 错误预算计算的窗口大小（最近多少次运行）
    error_budget_window_size: int = 10
    
    # 429 命中率阈值 - 超过此值则降低优先级
    rate_limit_hit_threshold: float = 0.1  # 10%
    
    # 暂停时长（秒）- 触发暂停后等待时间
    pause_duration_seconds: int = 300  # 5 分钟
    
    # 扫描间隔（秒）
    scan_interval_seconds: int = 60
    
    # 每次扫描最大 enqueue 数量
    max_enqueue_per_scan: int = 100
    
    # === Tenant 公平调度配置 ===
    # 启用按 tenant 分桶轮询策略
    # 当启用时，不同 tenant 的任务将交替入队，避免单个大 tenant 占用全部队列容量
    enable_tenant_fairness: bool = False
    
    # 启用 tenant 公平调度时，每轮每个 tenant 最多入队的任务数
    # 例如设为 1 表示每个 tenant 轮流入队 1 个任务
    tenant_fairness_max_per_round: int = 1
    
    # logical_job_type 优先级（越小越优先）
    # 注意：policy 层使用 logical_job_type，scheduler 入队时会转换为 physical_job_type
    # logical_job_type: commits/mrs/reviews（与 SCM 类型无关的抽象任务类型）
    # physical_job_type: gitlab_commits/gitlab_mrs/gitlab_reviews/svn（与具体 SCM 实现绑定）
    # 参见 engram_logbook.scm_sync_job_types 模块
    job_type_priority: Dict[str, int] = field(default_factory=lambda: {
        "commits": 1,   # logical: 提交记录（Git/SVN）
        "mrs": 2,       # logical: Merge Requests（仅 Git）
        "reviews": 3,   # logical: Review 事件（仅 Git）
    })
    
    # === MVP 模式配置 ===
    # 启用 MVP 模式时，仅调度 mvp_job_type_allowlist 中指定的任务类型
    # 用于 MVP 阶段限制功能范围，或在特定场景下仅同步部分数据类型
    mvp_mode_enabled: bool = False
    
    # MVP 模式下允许调度的 logical_job_type 列表
    # 仅当 mvp_mode_enabled=True 时生效
    # 空列表表示不允许任何任务（相当于禁用调度）
    mvp_job_type_allowlist: List[str] = field(default_factory=lambda: ["commits"])
    
    def __post_init__(self):
        """后初始化处理：同步 global_concurrency 和 max_queue_depth"""
        # 如果用户设置了 global_concurrency 但未设置 max_queue_depth，同步值
        # 这里使用 global_concurrency 的值作为 max_queue_depth 的默认值
        if self.max_queue_depth == 10 and self.global_concurrency != 10:
            self.max_queue_depth = self.global_concurrency
        # 始终保持 global_concurrency 与 max_queue_depth 同步
        self.global_concurrency = self.max_queue_depth
    
    @classmethod
    def from_config(cls, config: Optional[Any] = None) -> "SchedulerConfig":
        """
        从配置对象加载，支持环境变量覆盖
        
        环境变量优先级高于配置文件:
        - SCM_SCHEDULER_GLOBAL_CONCURRENCY
        - SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY
        - SCM_SCHEDULER_PER_TENANT_CONCURRENCY
        - SCM_SCHEDULER_SCAN_INTERVAL_SECONDS
        - SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN
        - SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD
        - SCM_SCHEDULER_PAUSE_DURATION_SECONDS
        """
        import os
        
        def _get_env_or_config(env_key: str, config_key: str, default, value_type=int):
            """优先环境变量，否则配置文件，最后默认值"""
            env_val = os.environ.get(env_key)
            if env_val:
                if value_type == float:
                    return float(env_val)
                elif value_type == bool:
                    return env_val.lower() in ("true", "1", "yes")
                return int(env_val)
            if config is not None:
                return config.get(config_key, default)
            return default
        
        def _get_list_env_or_config(env_key: str, config_key: str, default: List[str]) -> List[str]:
            """获取列表类型配置，环境变量用逗号分隔"""
            env_val = os.environ.get(env_key)
            if env_val:
                # 环境变量用逗号分隔，去除空白
                return [s.strip() for s in env_val.split(",") if s.strip()]
            if config is not None:
                val = config.get(config_key, default)
                if isinstance(val, list):
                    return val
                elif isinstance(val, str):
                    return [s.strip() for s in val.split(",") if s.strip()]
            return default
        
        # 优先使用新参数名，向后兼容旧参数名
        max_running = _get_env_or_config(
            "SCM_SCHEDULER_MAX_RUNNING",
            "scm.scheduler.max_running", 5
        )
        
        # global_concurrency 环境变量覆盖 max_queue_depth
        max_queue_depth = _get_env_or_config(
            "SCM_SCHEDULER_GLOBAL_CONCURRENCY",
            "scm.scheduler.max_queue_depth", 10
        )
        if config is not None:
            # 向后兼容：如果未设置环境变量且配置了旧键名
            if os.environ.get("SCM_SCHEDULER_GLOBAL_CONCURRENCY") is None:
                max_queue_depth = config.get(
                    "scm.scheduler.max_queue_depth",
                    config.get("scm.scheduler.global_concurrency", 10)
                )
        
        return cls(
            max_running=max_running,
            max_queue_depth=max_queue_depth,
            global_concurrency=max_queue_depth,  # 向后兼容
            per_instance_concurrency=_get_env_or_config(
                "SCM_SCHEDULER_PER_INSTANCE_CONCURRENCY",
                "scm.scheduler.per_instance_concurrency", 3
            ),
            per_tenant_concurrency=_get_env_or_config(
                "SCM_SCHEDULER_PER_TENANT_CONCURRENCY",
                "scm.scheduler.per_tenant_concurrency", 5
            ),
            backfill_repair_window_hours=_get_env_or_config(
                "SCM_SCHEDULER_BACKFILL_REPAIR_WINDOW_HOURS",
                "scm.scheduler.backfill_repair_window_hours", 24
            ),
            max_backfill_window_hours=_get_env_or_config(
                "SCM_SCHEDULER_MAX_BACKFILL_WINDOW_HOURS",
                "scm.scheduler.max_backfill_window_hours", 168
            ),
            cursor_age_threshold_seconds=_get_env_or_config(
                "SCM_SCHEDULER_CURSOR_AGE_THRESHOLD_SECONDS",
                "scm.scheduler.cursor_age_threshold_seconds", 3600
            ),
            error_budget_threshold=_get_env_or_config(
                "SCM_SCHEDULER_ERROR_BUDGET_THRESHOLD",
                "scm.scheduler.error_budget_threshold", 0.3,
                value_type=float
            ),
            error_budget_window_size=_get_env_or_config(
                "SCM_SCHEDULER_ERROR_BUDGET_WINDOW_SIZE",
                "scm.scheduler.error_budget_window_size", 10
            ),
            rate_limit_hit_threshold=_get_env_or_config(
                "SCM_SCHEDULER_RATE_LIMIT_HIT_THRESHOLD",
                "scm.scheduler.rate_limit_hit_threshold", 0.1,
                value_type=float
            ),
            pause_duration_seconds=_get_env_or_config(
                "SCM_SCHEDULER_PAUSE_DURATION_SECONDS",
                "scm.scheduler.pause_duration_seconds", 300
            ),
            scan_interval_seconds=_get_env_or_config(
                "SCM_SCHEDULER_SCAN_INTERVAL_SECONDS",
                "scm.scheduler.scan_interval_seconds", 60
            ),
            max_enqueue_per_scan=_get_env_or_config(
                "SCM_SCHEDULER_MAX_ENQUEUE_PER_SCAN",
                "scm.scheduler.max_enqueue_per_scan", 100
            ),
            # Tenant 公平调度配置
            enable_tenant_fairness=_get_env_or_config(
                "SCM_SCHEDULER_ENABLE_TENANT_FAIRNESS",
                "scm.scheduler.enable_tenant_fairness", False,
                value_type=bool
            ),
            tenant_fairness_max_per_round=_get_env_or_config(
                "SCM_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND",
                "scm.scheduler.tenant_fairness_max_per_round", 1
            ),
            # MVP 模式配置
            mvp_mode_enabled=_get_env_or_config(
                "SCM_SCHEDULER_MVP_MODE_ENABLED",
                "scm.scheduler.mvp_mode_enabled", False,
                value_type=bool
            ),
            mvp_job_type_allowlist=_get_list_env_or_config(
                "SCM_SCHEDULER_MVP_JOB_TYPE_ALLOWLIST",
                "scm.scheduler.mvp_job_type_allowlist",
                ["commits"]
            ),
        )


# ============ 调度策略纯函数 ============


@dataclass
class RepoSyncState:
    """
    仓库同步状态数据结构（用于策略计算）
    
    这是一个纯数据结构，从数据库查询结果构造
    """
    repo_id: int
    repo_type: str  # 'git' | 'svn'
    gitlab_instance: Optional[str] = None  # GitLab 实例标识（如 host）
    tenant_id: Optional[str] = None  # 租户标识
    
    # 游标状态
    cursor_updated_at: Optional[float] = None  # 游标最后更新时间（Unix timestamp）
    
    # 最近同步运行统计
    recent_run_count: int = 0
    recent_failed_count: int = 0
    recent_429_hits: int = 0
    recent_total_requests: int = 0
    last_run_status: Optional[str] = None  # 'completed' | 'failed' | 'no_data'
    last_run_at: Optional[float] = None  # 最后运行时间（Unix timestamp）
    
    # 是否已在队列中
    is_queued: bool = False


@dataclass
class SyncJobCandidate:
    """
    同步任务候选项（调度策略输出）
    
    注意：job_type 在 policy 层使用 logical_job_type，
    scheduler 入队时会根据 repo_type 转换为 physical_job_type。
    """
    repo_id: int
    job_type: str  # logical_job_type: 'commits' | 'mrs' | 'reviews'
                   # scheduler 会转换为 physical: 'gitlab_commits' | 'gitlab_mrs' | 'gitlab_reviews' | 'svn'
    priority: int = 0  # 优先级分数（越小越优先）
    
    # 调度原因
    reason: str = ""
    
    # 附加信息
    cursor_age_seconds: float = 0.0
    failure_rate: float = 0.0
    rate_limit_rate: float = 0.0
    
    # 是否建议暂停
    should_pause: bool = False
    pause_reason: Optional[str] = None
    
    # === Bucket 暂停相关字段 ===
    # 是否因 bucket 暂停而被跳过或降权
    bucket_paused: bool = False
    bucket_penalty_reason: Optional[str] = None  # 'bucket_paused' | 'bucket_low_tokens' | None
    bucket_penalty_value: int = 0  # 实际应用的优先级惩罚值
    bucket_pause_remaining_seconds: float = 0.0  # bucket 剩余暂停秒数


@dataclass
class BudgetSnapshot:
    """
    预算占用快照（用于策略层决策）
    
    记录调度器扫描时刻的活跃任务计数。
    策略层在选择候选时从这些初始值开始递增计数，
    确保新入队的任务不会超过预算限制。
    
    Attributes:
        global_running: 全局正在运行的任务数（status='running'）
        global_pending: 全局待处理的任务数（status='pending'）
        global_active: 全局活跃任务数（running + pending）
        by_instance: 按 gitlab_instance 分组的活跃任务计数
        by_tenant: 按 tenant_id 分组的活跃任务计数
    """
    global_running: int = 0
    global_pending: int = 0
    global_active: int = 0
    by_instance: Dict[str, int] = field(default_factory=dict)
    by_tenant: Dict[str, int] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "global_running": self.global_running,
            "global_pending": self.global_pending,
            "global_active": self.global_active,
            "by_instance": dict(self.by_instance),
            "by_tenant": dict(self.by_tenant),
        }
    
    @classmethod
    def empty(cls) -> "BudgetSnapshot":
        """创建一个空的预算快照"""
        return cls()


@dataclass
class InstanceBucketStatus:
    """
    实例级 Rate Limit Bucket 状态（用于策略层决策）
    
    记录某个 GitLab 实例的限流桶暂停状态。
    当 bucket 被暂停（因 429 触发）时，该实例的任务应被过滤或降权。
    
    Attributes:
        instance_key: 实例标识（如 GitLab 域名）
        is_paused: 是否被暂停
        paused_until: 暂停到期时间戳（Unix timestamp），None 表示未暂停
        pause_remaining_seconds: 剩余暂停秒数
        current_tokens: 当前可用令牌数
        rate: 令牌恢复速率（每秒）
        burst: 最大令牌数
    """
    instance_key: str
    is_paused: bool = False
    paused_until: Optional[float] = None
    pause_remaining_seconds: float = 0.0
    current_tokens: float = 0.0
    rate: float = 1.0
    burst: float = 10.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_key": self.instance_key,
            "is_paused": self.is_paused,
            "paused_until": self.paused_until,
            "pause_remaining_seconds": self.pause_remaining_seconds,
            "current_tokens": self.current_tokens,
            "rate": self.rate,
            "burst": self.burst,
        }
    
    @classmethod
    def from_db_status(cls, db_status: Dict[str, Any]) -> "InstanceBucketStatus":
        """
        从 db.get_rate_limit_status() 返回的字典创建实例
        
        Args:
            db_status: get_rate_limit_status() 返回的字典
        
        Returns:
            InstanceBucketStatus 实例
        """
        # 将 paused_until (datetime) 转换为 Unix timestamp
        paused_until_ts = None
        paused_until = db_status.get("paused_until")
        if paused_until:
            try:
                paused_until_ts = paused_until.timestamp()
            except (AttributeError, TypeError):
                pass
        
        return cls(
            instance_key=db_status.get("instance_key", ""),
            is_paused=db_status.get("is_paused", False),
            paused_until=paused_until_ts,
            pause_remaining_seconds=db_status.get("pause_remaining_seconds", 0.0),
            current_tokens=db_status.get("current_tokens", 0.0),
            rate=db_status.get("rate", 1.0),
            burst=db_status.get("burst", 10.0),
        )


# === Bucket 暂停相关的 Priority Penalty 常量 ===
# 当 bucket 被暂停时，对该实例任务的优先级惩罚值
BUCKET_PAUSED_PRIORITY_PENALTY = 1000  # 显著降低优先级

# 当 bucket 令牌不足（低于 burst 的 20%）时的优先级惩罚
BUCKET_LOW_TOKENS_PRIORITY_PENALTY = 200


def calculate_bucket_priority_penalty(
    bucket_status: Optional[InstanceBucketStatus],
) -> tuple:
    """
    根据 bucket 状态计算优先级惩罚（纯函数）
    
    策略：
    - bucket 被暂停：返回 (BUCKET_PAUSED_PRIORITY_PENALTY, "bucket_paused")
    - bucket 令牌不足（<20% burst）：返回 (BUCKET_LOW_TOKENS_PRIORITY_PENALTY, "bucket_low_tokens")
    - 正常：返回 (0, None)
    
    Args:
        bucket_status: 实例的 bucket 状态，None 表示无状态记录
    
    Returns:
        (priority_penalty: int, reason: str or None)
    """
    if bucket_status is None:
        return (0, None)
    
    # 检查是否被暂停
    if bucket_status.is_paused:
        return (BUCKET_PAUSED_PRIORITY_PENALTY, "bucket_paused")
    
    # 检查令牌是否不足（低于 burst 的 20%）
    if bucket_status.burst > 0:
        token_ratio = bucket_status.current_tokens / bucket_status.burst
        if token_ratio < 0.2:
            return (BUCKET_LOW_TOKENS_PRIORITY_PENALTY, "bucket_low_tokens")
    
    return (0, None)


def should_skip_due_to_bucket_pause(
    bucket_status: Optional[InstanceBucketStatus],
    skip_on_pause: bool = True,
) -> tuple:
    """
    判断是否应因 bucket 暂停而跳过任务（纯函数）
    
    Args:
        bucket_status: 实例的 bucket 状态
        skip_on_pause: 如果为 True，暂停时跳过；如果为 False，仅降权不跳过
    
    Returns:
        (should_skip: bool, reason: str or None, remaining_seconds: float)
    """
    if bucket_status is None:
        return (False, None, 0.0)
    
    if bucket_status.is_paused:
        if skip_on_pause:
            return (True, "bucket_paused", bucket_status.pause_remaining_seconds)
        else:
            # 不跳过，但返回原因供降权使用
            return (False, "bucket_paused_penalty_only", bucket_status.pause_remaining_seconds)
    
    return (False, None, 0.0)


def calculate_cursor_age(
    cursor_updated_at: Optional[float],
    now: Optional[float] = None,
) -> float:
    """
    计算游标年龄（纯函数）
    
    Args:
        cursor_updated_at: 游标最后更新时间（Unix timestamp），None 表示从未同步
        now: 当前时间（Unix timestamp），None 时使用 time.time()
    
    Returns:
        游标年龄（秒），如果从未同步返回 float('inf')
    """
    if cursor_updated_at is None:
        return float('inf')
    
    if now is None:
        now = time.time()
    
    return max(0.0, now - cursor_updated_at)


def calculate_failure_rate(
    failed_count: int,
    total_count: int,
) -> float:
    """
    计算失败率（纯函数）
    
    Args:
        failed_count: 失败次数
        total_count: 总次数
    
    Returns:
        失败率 (0.0 ~ 1.0)
    """
    if total_count <= 0:
        return 0.0
    return min(1.0, failed_count / total_count)


def calculate_rate_limit_rate(
    rate_limit_hits: int,
    total_requests: int,
) -> float:
    """
    计算 429 命中率（纯函数）
    
    Args:
        rate_limit_hits: 429 命中次数
        total_requests: 总请求次数
    
    Returns:
        429 命中率 (0.0 ~ 1.0)
    """
    if total_requests <= 0:
        return 0.0
    return min(1.0, rate_limit_hits / total_requests)


def should_schedule_repo_health(
    state: RepoSyncState,
    config: SchedulerConfig,
    now: Optional[float] = None,
) -> tuple:
    """
    判断仓库健康状态是否允许调度（纯函数）
    
    此函数不检查 is_queued，因为队列检查已移到 per-job_type 层级。
    用于 select_jobs_to_enqueue() 内部调用。
    
    返回 (should_schedule, reason, priority_adjustment)
    
    检查顺序：
    1. 错误预算超限 -> 不调度（保护性暂停）
    2. 游标年龄超过阈值 -> 调度
    3. 429 命中率超限但游标在阈值内 -> 降级调度
    4. 其他 -> 不调度
    
    Args:
        state: 仓库同步状态
        config: 调度器配置
        now: 当前时间戳
    
    Returns:
        (should_schedule: bool, reason: str, priority_adjustment: int)
    """
    if now is None:
        now = time.time()
    
    # 计算错误率（优先检查错误预算，保护性暂停）
    failure_rate = calculate_failure_rate(
        state.recent_failed_count,
        state.recent_run_count,
    )
    
    # 如果错误率超过阈值，暂停调度（无论游标年龄）
    if failure_rate >= config.error_budget_threshold:
        return (False, f"error_budget_exceeded:{failure_rate:.2%}", 0)
    
    # 计算游标年龄
    cursor_age = calculate_cursor_age(state.cursor_updated_at, now)
    
    # 如果游标年龄超过阈值，需要同步
    if cursor_age >= config.cursor_age_threshold_seconds:
        priority_adj = 0
        
        # 如果从未同步过，优先级最高
        if cursor_age == float('inf'):
            return (True, "never_synced", -100)
        
        # 游标年龄越大，优先级越高
        age_hours = cursor_age / 3600
        priority_adj = -int(min(age_hours, 24))  # 最多 -24
        
        return (True, f"cursor_age={cursor_age:.0f}s", priority_adj)
    
    # 计算 429 命中率
    rate_limit_rate = calculate_rate_limit_rate(
        state.recent_429_hits,
        state.recent_total_requests,
    )
    
    # 如果 429 命中率超过阈值，降低优先级但仍调度
    if rate_limit_rate >= config.rate_limit_hit_threshold:
        return (True, f"rate_limited:{rate_limit_rate:.2%}", 50)  # 降低优先级
    
    # 默认不需要调度
    return (False, "within_threshold", 0)


def should_schedule_repo(
    state: RepoSyncState,
    config: SchedulerConfig,
    now: Optional[float] = None,
) -> tuple:
    """
    判断是否应该调度仓库同步（纯函数）
    
    返回 (should_schedule, reason, priority_adjustment)
    
    检查顺序：
    1. 已在队列 -> 不调度（注意：此为 repo 级别检查，建议使用 queued_pairs 进行 per-job_type 检查）
    2. 错误预算超限 -> 不调度（保护性暂停）
    3. 游标年龄超过阈值 -> 调度
    4. 429 命中率超限但游标在阈值内 -> 降级调度
    5. 其他 -> 不调度
    
    Args:
        state: 仓库同步状态
        config: 调度器配置
        now: 当前时间戳
    
    Returns:
        (should_schedule: bool, reason: str, priority_adjustment: int)
    """
    if now is None:
        now = time.time()
    
    # 如果已在队列中，跳过（repo 级别检查，向后兼容）
    if state.is_queued:
        return (False, "already_queued", 0)
    
    # 委托给 health 检查函数
    return should_schedule_repo_health(state, config, now)


def compute_job_priority(
    repo_id: int,
    job_type: str,
    state: RepoSyncState,
    config: SchedulerConfig,
    priority_adjustment: int = 0,
) -> int:
    """
    计算任务优先级分数（纯函数，越小越优先）
    
    优先级计算规则：
    1. 基础分 = job_type_priority * 100
    2. + priority_adjustment（来自 should_schedule_repo）
    3. + 失败率惩罚（失败率高的仓库优先级降低）
    4. + 429 命中率惩罚
    
    Args:
        repo_id: 仓库 ID
        job_type: 任务类型
        state: 仓库同步状态
        config: 调度器配置
        priority_adjustment: 优先级调整值
    
    Returns:
        优先级分数（整数，越小越优先）
    """
    # 基础分
    base_priority = config.job_type_priority.get(job_type, 10) * 100
    
    # 应用调整
    priority = base_priority + priority_adjustment
    
    # 失败率惩罚（每 10% 失败率 +10 分）
    failure_rate = calculate_failure_rate(
        state.recent_failed_count,
        state.recent_run_count,
    )
    priority += int(failure_rate * 100)
    
    # 429 命中率惩罚（每 10% 命中率 +20 分）
    rate_limit_rate = calculate_rate_limit_rate(
        state.recent_429_hits,
        state.recent_total_requests,
    )
    priority += int(rate_limit_rate * 200)
    
    return priority


def _apply_tenant_fairness_ordering(
    candidates: List[SyncJobCandidate],
    states: List[RepoSyncState],
    max_per_round: int = 1,
) -> List[SyncJobCandidate]:
    """
    对候选任务应用按 tenant 分桶轮询排序（纯函数）
    
    实现公平调度：不同 tenant 的任务交替出现，避免单个大 tenant 占用全部队列容量。
    
    算法：
    1. 按 tenant_id 将候选任务分组（保持每组内按优先级顺序）
    2. 使用轮询方式从各 tenant 交替取任务
    3. 每轮每个 tenant 最多取 max_per_round 个任务
    4. 对于没有 tenant_id 的任务，归入 "__none__" 组
    
    Args:
        candidates: 已按优先级排序的候选任务列表
        states: 仓库同步状态列表（用于获取 tenant_id）
        max_per_round: 每轮每个 tenant 最多取的任务数
    
    Returns:
        重新排序后的候选任务列表
    """
    if not candidates or max_per_round <= 0:
        return candidates
    
    # 构建 repo_id -> tenant_id 映射
    repo_to_tenant: Dict[int, Optional[str]] = {}
    for state in states:
        repo_to_tenant[state.repo_id] = state.tenant_id
    
    # 按 tenant_id 分组，保持每组内的优先级顺序
    tenant_buckets: Dict[str, List[SyncJobCandidate]] = {}
    for candidate in candidates:
        tenant_id = repo_to_tenant.get(candidate.repo_id) or "__none__"
        if tenant_id not in tenant_buckets:
            tenant_buckets[tenant_id] = []
        tenant_buckets[tenant_id].append(candidate)
    
    # 如果只有一个 tenant（或所有任务都没有 tenant_id），保持原顺序
    if len(tenant_buckets) <= 1:
        return candidates
    
    # 按各 bucket 的第一个任务的优先级排序 bucket 顺序
    # 这样优先级高的 tenant 会先被处理
    sorted_tenant_ids = sorted(
        tenant_buckets.keys(),
        key=lambda tid: tenant_buckets[tid][0].priority if tenant_buckets[tid] else float('inf')
    )
    
    # 轮询取任务
    result: List[SyncJobCandidate] = []
    bucket_indices: Dict[str, int] = {tid: 0 for tid in sorted_tenant_ids}
    
    while True:
        added_this_round = 0
        
        for tenant_id in sorted_tenant_ids:
            bucket = tenant_buckets[tenant_id]
            start_idx = bucket_indices[tenant_id]
            
            # 每轮每个 tenant 最多取 max_per_round 个
            for _ in range(max_per_round):
                if bucket_indices[tenant_id] < len(bucket):
                    result.append(bucket[bucket_indices[tenant_id]])
                    bucket_indices[tenant_id] += 1
                    added_this_round += 1
        
        # 如果这一轮没有添加任何任务，说明所有 bucket 都已耗尽
        if added_this_round == 0:
            break
    
    return result


def select_jobs_to_enqueue(
    states: List[RepoSyncState],
    job_types: List[str],
    config: SchedulerConfig,
    now: Optional[float] = None,
    current_queue_size: int = 0,
    queued_pairs: Optional[set] = None,
    budget_snapshot: Optional[BudgetSnapshot] = None,
    bucket_statuses: Optional[Dict[str, "InstanceBucketStatus"]] = None,
    skip_on_bucket_pause: bool = False,
) -> List[SyncJobCandidate]:
    """
    选择需要入队的同步任务（纯函数）
    
    核心调度策略：
    1. 遍历所有仓库，判断是否需要同步
    2. 对每个需要同步的仓库，生成各 job_type 的候选
    3. 按 (repo_id, job_type) 组合判断是否已在队列，避免重复入队
    4. 检查 bucket 暂停状态，应用 priority penalty 或跳过
    5. 按优先级排序
    6. 应用预算限制裁剪（基于 budget_snapshot 的初始占用）
    
    预算限制检查顺序:
    1. max_running: 如果当前 running 数 >= max_running，不入队新任务
    2. max_queue_depth: 如果当前活跃数 >= max_queue_depth，不入队新任务
    3. per_instance_concurrency: 每个 GitLab 实例的活跃任务限制
    4. per_tenant_concurrency: 每个租户的活跃任务限制
    5. max_enqueue_per_scan: 单次扫描最大入队数量
    
    Args:
        states: 所有仓库的同步状态
        job_types: 需要考虑的任务类型列表
        config: 调度器配置
        now: 当前时间戳
        current_queue_size: 当前队列中的任务数（向后兼容，建议使用 budget_snapshot）
        queued_pairs: 当前队列中的 (repo_id, job_type) 集合，用于跳过已入队的任务
                      若为 None 则回退到使用 state.is_queued（兼容旧行为）
        budget_snapshot: 预算占用快照，包含当前活跃任务的分组计数
                        若为 None 则使用 current_queue_size 向后兼容
        bucket_statuses: 实例级 bucket 状态映射 {instance_key: InstanceBucketStatus}
                        用于检查 bucket 暂停状态并应用 priority penalty
        skip_on_bucket_pause: 如果为 True，bucket 暂停时跳过任务；
                             如果为 False（默认），仅降权不跳过
    
    Returns:
        需要入队的任务候选列表（已排序、已裁剪）
    """
    if now is None:
        now = time.time()
    
    if queued_pairs is None:
        queued_pairs = set()
    
    if bucket_statuses is None:
        bucket_statuses = {}
    
    # 初始化预算计数器（从快照开始）
    if budget_snapshot is not None:
        initial_running = budget_snapshot.global_running
        initial_active = budget_snapshot.global_active
        instance_counts = dict(budget_snapshot.by_instance)
        tenant_counts = dict(budget_snapshot.by_tenant)
    else:
        # 向后兼容：没有快照时使用 current_queue_size
        initial_running = 0  # 无法区分，假设为 0
        initial_active = current_queue_size
        instance_counts = {}
        tenant_counts = {}
    
    # 检查是否已经超过 max_running 限制
    if initial_running >= config.max_running:
        logger.debug(
            "已达 max_running 限制: running=%d >= max_running=%d, 不入队新任务",
            initial_running, config.max_running
        )
        return []
    
    # 检查是否已经超过 max_queue_depth 限制
    if initial_active >= config.max_queue_depth:
        logger.debug(
            "已达 max_queue_depth 限制: active=%d >= max_queue_depth=%d, 不入队新任务",
            initial_active, config.max_queue_depth
        )
        return []
    
    candidates: List[SyncJobCandidate] = []
    
    for state in states:
        # 判断是否需要调度（基于 repo 级别的健康状态）
        # 注意：这里不再使用 state.is_queued 判断，改为 per-job_type 检查
        should_schedule, reason, priority_adj = should_schedule_repo_health(state, config, now)
        
        if not should_schedule:
            continue
        
        # 检查是否因错误预算超限需要暂停
        failure_rate = calculate_failure_rate(
            state.recent_failed_count,
            state.recent_run_count,
        )
        should_pause = failure_rate >= config.error_budget_threshold
        
        # 计算各指标
        cursor_age = calculate_cursor_age(state.cursor_updated_at, now)
        rate_limit_rate = calculate_rate_limit_rate(
            state.recent_429_hits,
            state.recent_total_requests,
        )
        
        # === 检查 bucket 暂停状态 ===
        bucket_status = None
        bucket_penalty = 0
        bucket_penalty_reason = None
        bucket_pause_remaining = 0.0
        bucket_paused = False
        
        if state.gitlab_instance and state.gitlab_instance in bucket_statuses:
            bucket_status = bucket_statuses[state.gitlab_instance]
            
            # 检查是否应跳过
            skip_due_to_bucket, skip_reason, remaining_seconds = should_skip_due_to_bucket_pause(
                bucket_status, skip_on_pause=skip_on_bucket_pause
            )
            
            if skip_due_to_bucket:
                # 跳过该实例的所有任务
                logger.debug(
                    "跳过任务（bucket 暂停）: repo_id=%d, instance=%s, remaining=%.1fs",
                    state.repo_id, state.gitlab_instance, remaining_seconds
                )
                continue
            
            # 计算 bucket priority penalty
            bucket_penalty, bucket_penalty_reason = calculate_bucket_priority_penalty(bucket_status)
            bucket_pause_remaining = remaining_seconds
            bucket_paused = bucket_status.is_paused
        
        # 为每个 job_type 生成候选
        for job_type in job_types:
            # === MVP 模式过滤 ===
            # 当 mvp_mode_enabled=True 时，仅允许 mvp_job_type_allowlist 中的任务类型
            if config.mvp_mode_enabled:
                if job_type not in config.mvp_job_type_allowlist:
                    logger.debug(
                        "MVP 模式跳过非允许任务类型: repo_id=%d, job_type=%s, allowlist=%s",
                        state.repo_id, job_type, config.mvp_job_type_allowlist
                    )
                    continue
            
            # 检查此 (repo_id, job_type) 是否已在队列中
            if (state.repo_id, job_type) in queued_pairs:
                continue  # 跳过已入队的任务
            
            # 计算基础优先级
            base_priority = compute_job_priority(
                state.repo_id,
                job_type,
                state,
                config,
                priority_adj,
            )
            
            # 应用 bucket penalty
            final_priority = base_priority + bucket_penalty
            
            candidate = SyncJobCandidate(
                repo_id=state.repo_id,
                job_type=job_type,
                priority=final_priority,
                reason=reason,
                cursor_age_seconds=cursor_age if cursor_age != float('inf') else -1,
                failure_rate=failure_rate,
                rate_limit_rate=rate_limit_rate,
                should_pause=should_pause,
                pause_reason=reason if should_pause else None,
                # === Bucket 相关字段 ===
                bucket_paused=bucket_paused,
                bucket_penalty_reason=bucket_penalty_reason,
                bucket_penalty_value=bucket_penalty,
                bucket_pause_remaining_seconds=bucket_pause_remaining,
            )
            candidates.append(candidate)
    
    # 按优先级排序（越小越优先）
    candidates.sort(key=lambda c: c.priority)
    
    # === Tenant 公平调度策略 ===
    # 如果启用，按 tenant 分桶轮询，确保不同 tenant 交替入队
    if config.enable_tenant_fairness and candidates:
        candidates = _apply_tenant_fairness_ordering(
            candidates, 
            states, 
            config.tenant_fairness_max_per_round,
        )
    
    # 计算可入队的最大数量
    # 1. max_enqueue_per_scan 限制
    max_by_scan = config.max_enqueue_per_scan
    
    # 2. max_queue_depth 限制（从初始活跃数开始计算剩余空间）
    max_by_queue_depth = config.max_queue_depth - initial_active
    
    # 3. 综合限制
    max_to_enqueue = min(max_by_scan, max_by_queue_depth)
    
    if max_to_enqueue <= 0:
        return []
    
    # 应用并发限制裁剪
    selected: List[SyncJobCandidate] = []
    current_active = initial_active  # 从初始占用开始递增
    
    for candidate in candidates:
        # 检查全局队列深度限制
        if current_active >= config.max_queue_depth:
            logger.debug(
                "达到 max_queue_depth 限制: current=%d >= limit=%d",
                current_active, config.max_queue_depth
            )
            break
        
        # 检查单次扫描入队限制
        if len(selected) >= max_to_enqueue:
            break
        
        # 查找对应的 state
        state = next((s for s in states if s.repo_id == candidate.repo_id), None)
        if state is None:
            continue
        
        # 检查实例并发限制
        if state.gitlab_instance:
            instance_count = instance_counts.get(state.gitlab_instance, 0)
            if instance_count >= config.per_instance_concurrency:
                continue
            instance_counts[state.gitlab_instance] = instance_count + 1
        
        # 检查租户并发限制
        if state.tenant_id:
            tenant_count = tenant_counts.get(state.tenant_id, 0)
            if tenant_count >= config.per_tenant_concurrency:
                continue
            tenant_counts[state.tenant_id] = tenant_count + 1
        
        selected.append(candidate)
        current_active += 1
    
    return selected


def compute_backfill_window(
    last_successful_cursor_ts: Optional[float],
    config: SchedulerConfig,
    now: Optional[float] = None,
) -> tuple:
    """
    计算回填时间窗口（纯函数）- 适用于 Git/MR/Review 的时间窗口
    
    Args:
        last_successful_cursor_ts: 最后成功同步的游标时间戳
        config: 调度器配置
        now: 当前时间戳
    
    Returns:
        (since_ts, until_ts) 回填时间窗口
    """
    if now is None:
        now = time.time()
    
    # 默认使用配置的修复窗口
    repair_window_seconds = config.backfill_repair_window_hours * 3600
    max_window_seconds = config.max_backfill_window_hours * 3600
    
    # 计算 since 时间
    if last_successful_cursor_ts is not None:
        # 从最后成功的游标时间开始
        since_ts = last_successful_cursor_ts
        
        # 但不能超过最大回填窗口
        min_since_ts = now - max_window_seconds
        since_ts = max(since_ts, min_since_ts)
    else:
        # 如果从未同步，使用默认修复窗口
        since_ts = now - repair_window_seconds
    
    return (since_ts, now)


@dataclass
class BackfillWindow:
    """
    Backfill 窗口数据结构
    
    支持两种类型：
    - time: 时间窗口，用于 Git commits/MRs/Reviews
    - rev: revision 窗口，用于 SVN
    """
    window_type: str  # 'time' | 'rev'
    
    # 时间窗口字段（window_type='time' 时使用）
    since_ts: Optional[float] = None
    until_ts: Optional[float] = None
    
    # Revision 窗口字段（window_type='rev' 时使用）
    start_rev: Optional[int] = None
    end_rev: Optional[int] = None
    
    # 窗口切分参数
    chunk_size: Optional[int] = None  # SVN: revision 数量; Git: 时间秒数
    total_chunks: int = 1
    current_chunk: int = 0
    
    # 是否需要更新 watermark
    update_watermark: bool = False
    
    def to_payload(self) -> Dict[str, Any]:
        """转换为任务 payload 格式"""
        payload: Dict[str, Any] = {
            "window_type": self.window_type,
            "update_watermark": self.update_watermark,
            "total_chunks": self.total_chunks,
            "current_chunk": self.current_chunk,
        }
        
        if self.window_type == "time":
            if self.since_ts is not None:
                payload["since"] = self.since_ts
            if self.until_ts is not None:
                payload["until"] = self.until_ts
        elif self.window_type == "rev":
            if self.start_rev is not None:
                payload["start_rev"] = self.start_rev
            if self.end_rev is not None:
                payload["end_rev"] = self.end_rev
        
        if self.chunk_size is not None:
            payload["chunk_size"] = self.chunk_size
        
        return payload


def compute_time_backfill_window(
    last_successful_cursor_ts: Optional[float],
    config: SchedulerConfig,
    now: Optional[float] = None,
    chunk_hours: Optional[int] = None,
) -> BackfillWindow:
    """
    计算时间回填窗口（用于 Git/MR/Review）
    
    Args:
        last_successful_cursor_ts: 最后成功同步的游标时间戳（Unix timestamp）
        config: 调度器配置
        now: 当前时间戳
        chunk_hours: 可选的分块小时数（用于大窗口切分）
    
    Returns:
        BackfillWindow 对象
    """
    if now is None:
        now = time.time()
    
    # 计算基础窗口
    since_ts, until_ts = compute_backfill_window(last_successful_cursor_ts, config, now)
    
    # 计算窗口时长
    window_seconds = until_ts - since_ts
    
    # 如果需要切分，计算分块参数
    total_chunks = 1
    chunk_size_seconds = None
    
    if chunk_hours and chunk_hours > 0:
        chunk_size_seconds = chunk_hours * 3600
        total_chunks = max(1, int((window_seconds + chunk_size_seconds - 1) / chunk_size_seconds))
    
    return BackfillWindow(
        window_type="time",
        since_ts=since_ts,
        until_ts=until_ts,
        chunk_size=chunk_size_seconds,
        total_chunks=total_chunks,
        current_chunk=0,
        update_watermark=False,
    )


def compute_svn_backfill_window(
    last_successful_rev: Optional[int],
    current_head_rev: Optional[int],
    config: SchedulerConfig,
    max_rev_window: int = 1000,
    chunk_size: int = 100,
) -> BackfillWindow:
    """
    计算 SVN revision 回填窗口
    
    Args:
        last_successful_rev: 最后成功同步的 revision 号
        current_head_rev: 当前仓库 HEAD revision 号（可选）
        config: 调度器配置
        max_rev_window: 最大 revision 窗口大小（默认 1000）
        chunk_size: 分块大小（默认 100 个 revision）
    
    Returns:
        BackfillWindow 对象
    """
    # 计算 start_rev
    if last_successful_rev is not None:
        start_rev = last_successful_rev + 1  # 从下一个 revision 开始
    else:
        start_rev = 1  # 从 revision 1 开始
    
    # 计算 end_rev
    if current_head_rev is not None:
        # 限制窗口大小
        max_end_rev = start_rev + max_rev_window - 1
        end_rev = min(current_head_rev, max_end_rev)
    else:
        # 如果不知道 HEAD，使用一个合理的默认窗口
        end_rev = start_rev + max_rev_window - 1
    
    # 确保有效窗口
    if end_rev < start_rev:
        # 没有需要回填的内容
        return BackfillWindow(
            window_type="rev",
            start_rev=start_rev,
            end_rev=start_rev - 1,  # 空窗口
            chunk_size=chunk_size,
            total_chunks=0,
            current_chunk=0,
            update_watermark=False,
        )
    
    # 计算分块数量
    window_size = end_rev - start_rev + 1
    total_chunks = max(1, int((window_size + chunk_size - 1) / chunk_size))
    
    return BackfillWindow(
        window_type="rev",
        start_rev=start_rev,
        end_rev=end_rev,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        current_chunk=0,
        update_watermark=False,
    )


def should_generate_backfill(
    circuit_state: str,
    last_successful_cursor_ts: Optional[float] = None,
    last_successful_rev: Optional[int] = None,
    config: Optional[SchedulerConfig] = None,
    now: Optional[float] = None,
) -> bool:
    """
    判断是否应该生成 backfill 任务
    
    条件：
    1. 熔断器处于 OPEN 或 HALF_OPEN 状态
    2. 或者游标/revision 滞后超过阈值
    
    Args:
        circuit_state: 当前熔断状态字符串（'open' | 'half_open' | 'closed'）
        last_successful_cursor_ts: 最后成功同步的游标时间戳
        last_successful_rev: 最后成功同步的 revision 号
        config: 调度器配置
        now: 当前时间戳
    
    Returns:
        是否应该生成 backfill 任务
    """
    # OPEN 或 HALF_OPEN 状态时优先 backfill
    if circuit_state in ("open", "half_open"):
        return True
    
    # 检查游标滞后
    if config is not None and now is None:
        now = time.time()
    
    if last_successful_cursor_ts is not None and config is not None:
        cursor_age = now - last_successful_cursor_ts
        # 如果滞后超过 backfill_repair_window_hours，需要 backfill
        if cursor_age > config.backfill_repair_window_hours * 3600:
            return True
    
    return False


class SvnPatchFetchController:
    """
    SVN Patch 获取控制器
    
    专门用于控制 SVN 同步中 fetch_patches 阶段的降级策略。
    当连续遇到 timeout/content_too_large 错误时，暂停 patch 获取。
    """
    
    def __init__(
        self,
        timeout_threshold: int = 3,
        content_too_large_threshold: int = 5,
        pause_duration_seconds: float = 60.0,
    ):
        """
        初始化控制器
        
        Args:
            timeout_threshold: 连续超时触发暂停的阈值
            content_too_large_threshold: 连续内容过大触发暂停的阈值
            pause_duration_seconds: 暂停持续时间
        """
        self._timeout_threshold = timeout_threshold
        self._content_too_large_threshold = content_too_large_threshold
        self._pause_duration_seconds = pause_duration_seconds
        
        self._consecutive_timeout = 0
        self._consecutive_content_too_large = 0
        self._should_skip_patches = False
        self._skip_reason: Optional[str] = None
    
    @property
    def should_skip_patches(self) -> bool:
        """是否应跳过 patch 获取"""
        return self._should_skip_patches
    
    @property
    def skip_reason(self) -> Optional[str]:
        """跳过原因"""
        return self._skip_reason
    
    def record_success(self) -> None:
        """记录成功获取"""
        # 成功时重置计数器
        self._consecutive_timeout = 0
        self._consecutive_content_too_large = 0
        self._should_skip_patches = False
        self._skip_reason = None
    
    def record_error(self, error_category: str) -> bool:
        """
        记录错误
        
        Args:
            error_category: 错误类型 (timeout / content_too_large / 其他)
            
        Returns:
            是否应暂停 patch 获取
        """
        if error_category == "timeout":
            self._consecutive_timeout += 1
            # 重置其他计数
            self._consecutive_content_too_large = 0
            
            if self._consecutive_timeout >= self._timeout_threshold:
                self._should_skip_patches = True
                self._skip_reason = f"consecutive_timeout={self._consecutive_timeout}>=threshold={self._timeout_threshold}"
                logger.warning(f"连续超时达到阈值，暂停 patch 获取: {self._skip_reason}")
                return True
        
        elif error_category == "content_too_large":
            self._consecutive_content_too_large += 1
            # 重置其他计数
            self._consecutive_timeout = 0
            
            if self._consecutive_content_too_large >= self._content_too_large_threshold:
                self._should_skip_patches = True
                self._skip_reason = f"consecutive_content_too_large={self._consecutive_content_too_large}>=threshold={self._content_too_large_threshold}"
                logger.warning(f"连续内容过大达到阈值，暂停 patch 获取: {self._skip_reason}")
                return True
        
        else:
            # 其他错误不累计，但会重置成功相关的状态
            pass
        
        return False
    
    def reset(self) -> None:
        """重置控制器"""
        self._consecutive_timeout = 0
        self._consecutive_content_too_large = 0
        self._should_skip_patches = False
        self._skip_reason = None
    
    def get_state(self) -> Dict[str, Any]:
        """获取当前状态"""
        return {
            "consecutive_timeout": self._consecutive_timeout,
            "consecutive_content_too_large": self._consecutive_content_too_large,
            "should_skip_patches": self._should_skip_patches,
            "skip_reason": self._skip_reason,
        }


# ============ 熔断状态枚举 ============


class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "closed"          # 正常状态，允许全量同步
    HALF_OPEN = "half_open"    # 半开状态，低频探测恢复
    OPEN = "open"              # 熔断状态，仅 backfill repair 或完全暂停


# ============ 熔断控制器配置 ============


@dataclass
class CircuitBreakerConfig:
    """
    熔断控制器配置
    
    阈值说明:
    - failure_rate_threshold: 失败率阈值，超过此值触发熔断
    - rate_limit_threshold: 429 命中率阈值，超过此值触发熔断
    - timeout_rate_threshold: 超时率阈值
    - window_count: 统计窗口大小（最近多少次运行）
    - window_minutes: 统计时间窗口（分钟）
    - open_duration_seconds: 熔断持续时间（秒）
    - half_open_max_requests: 半开状态最大探测请求数
    - recovery_success_count: 半开状态恢复所需连续成功次数
    
    小样本保护参数:
    - min_samples: 最小样本数，低于此值不触发熔断，避免因少量数据导致误判
    
    平滑策略参数:
    - smoothing_alpha: EMA（指数移动平均）平滑系数，范围 (0, 1]
                       值越小平滑效果越强，对历史数据依赖越大，减少抖动
                       值为 1.0 时无平滑效果，直接使用当前值
                       典型值：0.3（较强平滑）、0.5（中等）、0.7（较弱平滑）
    - enable_smoothing: 是否启用平滑策略，默认 True
    
    HALF_OPEN 探测参数说明:
    - probe_budget_per_interval: 每个扫描周期允许放行的探测任务数
    - probe_job_types_allowlist: 仅允许这些 job_type 作为探测任务（轻量任务优先）
                                 为空列表时表示允许所有类型
    """
    # 触发阈值
    failure_rate_threshold: float = 0.3       # 30% 失败率触发熔断
    rate_limit_threshold: float = 0.2         # 20% 429 命中率触发熔断
    timeout_rate_threshold: float = 0.2       # 20% 超时率触发熔断
    
    # 小样本保护
    min_samples: int = 5                      # 至少需要 5 次运行才能评估，避免小样本误判
    
    # 平滑策略（减少抖动）
    enable_smoothing: bool = True             # 启用平滑策略
    smoothing_alpha: float = 0.5              # EMA 平滑系数，值越小平滑越强
    
    # 统计窗口
    window_count: int = 20                    # 最近 20 次运行
    window_minutes: Optional[int] = 30        # 或最近 30 分钟
    
    # 熔断状态参数
    open_duration_seconds: int = 300          # 熔断持续 5 分钟
    half_open_max_requests: int = 3           # 半开状态最多探测 3 次
    recovery_success_count: int = 2           # 连续 2 次成功恢复到 closed
    
    # 降级参数（熔断时使用）
    degraded_batch_size: int = 10             # 熔断时的 batch_size
    degraded_forward_window_seconds: int = 300  # 熔断时的前向窗口（5分钟）
    
    # 低频 backfill repair 参数
    backfill_only_mode: bool = True           # 熔断时是否仅执行 backfill
    backfill_interval_seconds: int = 600      # backfill 间隔（10分钟）
    
    # HALF_OPEN 探测参数
    # 每个调度周期允许放行的探测任务数（用于限制 HALF_OPEN 状态下的并发）
    probe_budget_per_interval: int = 2
    # 仅允许这些 job_type 作为探测任务（轻量任务优先）
    # 例如：["commits"] 表示仅允许 commits 类型的任务用于探测
    # 为空列表时表示允许所有类型
    probe_job_types_allowlist: List[str] = field(default_factory=lambda: ["commits"])
    
    @classmethod
    def from_config(cls, config: Optional[Any] = None) -> "CircuitBreakerConfig":
        """
        从配置对象加载，支持环境变量覆盖
        
        环境变量优先级高于配置文件:
        - SCM_CB_FAILURE_RATE_THRESHOLD
        - SCM_CB_RATE_LIMIT_THRESHOLD
        - SCM_CB_TIMEOUT_RATE_THRESHOLD
        - SCM_CB_MIN_SAMPLES
        - SCM_CB_ENABLE_SMOOTHING
        - SCM_CB_SMOOTHING_ALPHA
        - SCM_CB_WINDOW_COUNT
        - SCM_CB_WINDOW_MINUTES
        - SCM_CB_OPEN_DURATION_SECONDS
        - SCM_CB_HALF_OPEN_MAX_REQUESTS
        - SCM_CB_RECOVERY_SUCCESS_COUNT
        - SCM_CB_DEGRADED_BATCH_SIZE
        - SCM_CB_DEGRADED_FORWARD_WINDOW_SECONDS
        - SCM_CB_BACKFILL_ONLY_MODE
        - SCM_CB_BACKFILL_INTERVAL_SECONDS
        - SCM_CB_PROBE_BUDGET_PER_INTERVAL
        - SCM_CB_PROBE_JOB_TYPES_ALLOWLIST (逗号分隔)
        """
        import os
        
        def _get_env_or_config(env_key: str, config_key: str, default, value_type=int):
            """优先环境变量，否则配置文件，最后默认值"""
            env_val = os.environ.get(env_key)
            if env_val:
                if value_type == float:
                    return float(env_val)
                elif value_type == bool:
                    return env_val.lower() in ("true", "1", "yes")
                return int(env_val)
            if config is not None:
                return config.get(config_key, default)
            return default
        
        def _get_list_env_or_config(env_key: str, config_key: str, default: List[str]) -> List[str]:
            """获取列表类型配置，环境变量用逗号分隔"""
            env_val = os.environ.get(env_key)
            if env_val:
                # 环境变量用逗号分隔，去除空白
                return [s.strip() for s in env_val.split(",") if s.strip()]
            if config is not None:
                val = config.get(config_key, default)
                if isinstance(val, list):
                    return val
                elif isinstance(val, str):
                    return [s.strip() for s in val.split(",") if s.strip()]
            return default
        
        return cls(
            failure_rate_threshold=_get_env_or_config(
                "SCM_CB_FAILURE_RATE_THRESHOLD",
                "scm.circuit_breaker.failure_rate_threshold", 0.3,
                value_type=float
            ),
            rate_limit_threshold=_get_env_or_config(
                "SCM_CB_RATE_LIMIT_THRESHOLD",
                "scm.circuit_breaker.rate_limit_threshold", 0.2,
                value_type=float
            ),
            timeout_rate_threshold=_get_env_or_config(
                "SCM_CB_TIMEOUT_RATE_THRESHOLD",
                "scm.circuit_breaker.timeout_rate_threshold", 0.2,
                value_type=float
            ),
            # 小样本保护参数
            min_samples=_get_env_or_config(
                "SCM_CB_MIN_SAMPLES",
                "scm.circuit_breaker.min_samples", 5
            ),
            # 平滑策略参数
            enable_smoothing=_get_env_or_config(
                "SCM_CB_ENABLE_SMOOTHING",
                "scm.circuit_breaker.enable_smoothing", True,
                value_type=bool
            ),
            smoothing_alpha=_get_env_or_config(
                "SCM_CB_SMOOTHING_ALPHA",
                "scm.circuit_breaker.smoothing_alpha", 0.5,
                value_type=float
            ),
            window_count=_get_env_or_config(
                "SCM_CB_WINDOW_COUNT",
                "scm.circuit_breaker.window_count", 20
            ),
            window_minutes=_get_env_or_config(
                "SCM_CB_WINDOW_MINUTES",
                "scm.circuit_breaker.window_minutes", 30
            ),
            open_duration_seconds=_get_env_or_config(
                "SCM_CB_OPEN_DURATION_SECONDS",
                "scm.circuit_breaker.open_duration_seconds", 300
            ),
            half_open_max_requests=_get_env_or_config(
                "SCM_CB_HALF_OPEN_MAX_REQUESTS",
                "scm.circuit_breaker.half_open_max_requests", 3
            ),
            recovery_success_count=_get_env_or_config(
                "SCM_CB_RECOVERY_SUCCESS_COUNT",
                "scm.circuit_breaker.recovery_success_count", 2
            ),
            degraded_batch_size=_get_env_or_config(
                "SCM_CB_DEGRADED_BATCH_SIZE",
                "scm.circuit_breaker.degraded_batch_size", 10
            ),
            degraded_forward_window_seconds=_get_env_or_config(
                "SCM_CB_DEGRADED_FORWARD_WINDOW_SECONDS",
                "scm.circuit_breaker.degraded_forward_window_seconds", 300
            ),
            backfill_only_mode=_get_env_or_config(
                "SCM_CB_BACKFILL_ONLY_MODE",
                "scm.circuit_breaker.backfill_only_mode", True,
                value_type=bool
            ),
            backfill_interval_seconds=_get_env_or_config(
                "SCM_CB_BACKFILL_INTERVAL_SECONDS",
                "scm.circuit_breaker.backfill_interval_seconds", 600
            ),
            probe_budget_per_interval=_get_env_or_config(
                "SCM_CB_PROBE_BUDGET_PER_INTERVAL",
                "scm.circuit_breaker.probe_budget_per_interval", 2
            ),
            probe_job_types_allowlist=_get_list_env_or_config(
                "SCM_CB_PROBE_JOB_TYPES_ALLOWLIST",
                "scm.circuit_breaker.probe_job_types_allowlist",
                ["commits"]
            ),
        )


# ============ 熔断决策结果 ============


@dataclass
class CircuitBreakerDecision:
    """
    熔断决策数据结构
    
    包含是否允许同步、建议的降级参数等
    
    HALF_OPEN 探测模式说明:
    - is_probe_mode: 是否为探测模式（HALF_OPEN 状态下为 True）
    - probe_budget: 本次允许放行的探测任务数
    - probe_job_types_allowlist: 允许作为探测任务的 job_type 列表
    """
    # 核心决策
    allow_sync: bool = True                   # 是否允许同步
    is_backfill_only: bool = False            # 是否仅执行 backfill
    
    # 降级参数
    suggested_batch_size: int = 100
    suggested_forward_window_seconds: int = 3600
    suggested_diff_mode: str = "best_effort"
    
    # 等待时间
    wait_seconds: float = 0.0                 # 需要等待的秒数
    next_allowed_at: Optional[float] = None   # 下次允许同步的时间戳
    
    # 状态信息
    current_state: str = "closed"
    trigger_reason: Optional[str] = None
    health_stats: Optional[Dict[str, Any]] = None
    
    # HALF_OPEN 探测模式相关
    is_probe_mode: bool = False               # 是否为探测模式
    probe_budget: int = 0                     # 本次允许放行的探测任务数
    probe_job_types_allowlist: List[str] = field(default_factory=list)  # 允许的 job_type
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "allow_sync": self.allow_sync,
            "is_backfill_only": self.is_backfill_only,
            "suggested_batch_size": self.suggested_batch_size,
            "suggested_forward_window_seconds": self.suggested_forward_window_seconds,
            "suggested_diff_mode": self.suggested_diff_mode,
            "wait_seconds": self.wait_seconds,
            "next_allowed_at": self.next_allowed_at,
            "current_state": self.current_state,
            "trigger_reason": self.trigger_reason,
            "health_stats": self.health_stats,
            "is_probe_mode": self.is_probe_mode,
            "probe_budget": self.probe_budget,
            "probe_job_types_allowlist": self.probe_job_types_allowlist,
        }


# ============ 熔断控制器 ============


class CircuitBreakerController:
    """
    SCM 同步熔断控制器
    
    基于 scm.sync_runs 的健康统计，实现熔断保护机制：
    
    状态流转:
    - CLOSED: 正常状态，允许全量同步
    - OPEN: 熔断状态，禁止同步或仅低频 backfill
    - HALF_OPEN: 半开状态，探测性恢复
    
    触发条件（任一满足即触发熔断）:
    - 失败率 >= failure_rate_threshold
    - 429 命中率 >= rate_limit_threshold
    - 超时率 >= timeout_rate_threshold
    
    恢复机制:
    - 熔断持续 open_duration_seconds 后进入 HALF_OPEN
    - HALF_OPEN 状态下连续 recovery_success_count 次成功后恢复到 CLOSED
    - HALF_OPEN 状态下失败则重新进入 OPEN
    
    状态持久化:
    - 使用 logbook.kv namespace='scm.sync_health' 存储状态
    - 支持跨进程共享熔断状态
    
    使用示例:
        # 初始化（通常在 worker/scheduler 启动时）
        controller = CircuitBreakerController()
        
        # 检查是否允许同步
        decision = controller.check(health_stats)
        if not decision.allow_sync:
            if decision.wait_seconds > 0:
                time.sleep(decision.wait_seconds)
                # 重新检查或执行低频 backfill
        
        # 同步完成后记录结果
        controller.record_result(success=True)
    """
    
    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        key: str = "global",
    ):
        """
        初始化熔断控制器
        
        Args:
            config: 熔断配置
            key: 状态存储键名（用于区分全局或仓库级别）
        """
        self._config = config or CircuitBreakerConfig()
        self._key = key
        
        # 内存状态（可从持久化加载）
        self._state = CircuitState.CLOSED
        self._opened_at: Optional[float] = None          # 进入 OPEN 状态的时间
        self._half_open_attempts: int = 0                # 半开状态的尝试次数
        self._half_open_successes: int = 0               # 半开状态的连续成功次数
        self._last_failure_reason: Optional[str] = None  # 最后一次触发熔断的原因
        
        # 平滑状态存储（EMA 平滑后的历史值）
        self._smoothed_failure_rate: Optional[float] = None
        self._smoothed_rate_limit_rate: Optional[float] = None
        self._smoothed_timeout_rate: Optional[float] = None
        
        # 降级控制器（用于渐进恢复）
        self._degradation_controller: Optional[DegradationController] = None
    
    @property
    def state(self) -> CircuitState:
        """当前熔断状态"""
        return self._state
    
    @property
    def is_open(self) -> bool:
        """是否处于熔断状态"""
        return self._state == CircuitState.OPEN
    
    @property
    def is_half_open(self) -> bool:
        """是否处于半开状态"""
        return self._state == CircuitState.HALF_OPEN
    
    @property
    def is_closed(self) -> bool:
        """是否处于正常状态"""
        return self._state == CircuitState.CLOSED
    
    def _apply_smoothing(self, current_value: float, smoothed_value: Optional[float]) -> float:
        """
        应用 EMA（指数移动平均）平滑
        
        公式: smoothed = alpha * current + (1 - alpha) * previous_smoothed
        
        Args:
            current_value: 当前值
            smoothed_value: 上一次平滑后的值（None 表示第一次）
        
        Returns:
            平滑后的值
        """
        if not self._config.enable_smoothing:
            return current_value
        
        if smoothed_value is None:
            # 第一次，直接使用当前值
            return current_value
        
        alpha = self._config.smoothing_alpha
        # 确保 alpha 在有效范围内
        alpha = max(0.01, min(1.0, alpha))
        
        return alpha * current_value + (1.0 - alpha) * smoothed_value
    
    def _should_trip(self, health_stats: Dict[str, Any]) -> tuple:
        """
        检查是否应触发熔断
        
        使用 min_samples 进行小样本保护，使用 EMA 平滑减少抖动。
        
        Args:
            health_stats: 健康统计数据
        
        Returns:
            (should_trip: bool, reason: str or None)
        """
        # 小样本保护：如果没有足够的运行记录，不触发熔断
        total_runs = health_stats.get("total_runs", 0)
        min_samples = self._config.min_samples
        if total_runs < min_samples:
            logger.debug(
                "熔断检查跳过（样本不足）: total_runs=%d < min_samples=%d",
                total_runs, min_samples
            )
            return (False, None)
        
        # 获取原始指标值
        raw_failure_rate = health_stats.get("failed_rate", 0.0)
        raw_rate_limit_rate = health_stats.get("rate_limit_rate", 0.0)
        
        # 计算超时率
        total_requests = health_stats.get("total_requests", 0)
        timeout_count = health_stats.get("total_timeout_count", 0)
        raw_timeout_rate = timeout_count / total_requests if total_requests > 0 else 0.0
        
        # 应用平滑策略（减少抖动）
        smoothed_failure_rate = self._apply_smoothing(raw_failure_rate, self._smoothed_failure_rate)
        smoothed_rate_limit_rate = self._apply_smoothing(raw_rate_limit_rate, self._smoothed_rate_limit_rate)
        smoothed_timeout_rate = self._apply_smoothing(raw_timeout_rate, self._smoothed_timeout_rate)
        
        # 更新平滑状态
        self._smoothed_failure_rate = smoothed_failure_rate
        self._smoothed_rate_limit_rate = smoothed_rate_limit_rate
        self._smoothed_timeout_rate = smoothed_timeout_rate
        
        # 使用平滑后的值进行阈值判断
        use_raw = not self._config.enable_smoothing
        failure_rate = raw_failure_rate if use_raw else smoothed_failure_rate
        rate_limit_rate = raw_rate_limit_rate if use_raw else smoothed_rate_limit_rate
        timeout_rate = raw_timeout_rate if use_raw else smoothed_timeout_rate
        
        # 检查失败率
        if failure_rate >= self._config.failure_rate_threshold:
            reason = f"failure_rate={failure_rate:.2%}>=threshold={self._config.failure_rate_threshold:.2%}"
            if self._config.enable_smoothing:
                reason += f" (raw={raw_failure_rate:.2%}, smoothed={smoothed_failure_rate:.2%})"
            return (True, reason)
        
        # 检查 429 命中率
        if rate_limit_rate >= self._config.rate_limit_threshold:
            reason = f"rate_limit_rate={rate_limit_rate:.2%}>=threshold={self._config.rate_limit_threshold:.2%}"
            if self._config.enable_smoothing:
                reason += f" (raw={raw_rate_limit_rate:.2%}, smoothed={smoothed_rate_limit_rate:.2%})"
            return (True, reason)
        
        # 检查超时率（仅在有请求统计时）
        if total_requests > 0:
            if timeout_rate >= self._config.timeout_rate_threshold:
                reason = f"timeout_rate={timeout_rate:.2%}>=threshold={self._config.timeout_rate_threshold:.2%}"
                if self._config.enable_smoothing:
                    reason += f" (raw={raw_timeout_rate:.2%}, smoothed={smoothed_timeout_rate:.2%})"
                return (True, reason)
        
        return (False, None)
    
    def _should_transition_to_half_open(self, now: Optional[float] = None) -> bool:
        """
        检查是否应从 OPEN 转换到 HALF_OPEN
        
        Args:
            now: 当前时间戳
        
        Returns:
            是否应转换
        """
        if self._state != CircuitState.OPEN:
            return False
        
        if self._opened_at is None:
            return True  # 没有记录开启时间，允许探测
        
        if now is None:
            now = time.time()
        
        elapsed = now - self._opened_at
        return elapsed >= self._config.open_duration_seconds
    
    def check(
        self,
        health_stats: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> CircuitBreakerDecision:
        """
        检查当前是否允许同步
        
        根据熔断状态和健康统计，返回同步决策。
        
        Args:
            health_stats: 健康统计数据（来自 get_sync_runs_health_stats）
            now: 当前时间戳
        
        Returns:
            CircuitBreakerDecision 决策对象
        """
        if now is None:
            now = time.time()
        
        health_stats = health_stats or {}
        
        # 根据当前状态处理
        if self._state == CircuitState.CLOSED:
            # 正常状态：检查是否应触发熔断
            should_trip, reason = self._should_trip(health_stats)
            
            if should_trip:
                # 触发熔断
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._last_failure_reason = reason
                self._half_open_attempts = 0
                self._half_open_successes = 0
                
                logger.warning(f"熔断器触发: key={self._key}, reason={reason}")
                
                return CircuitBreakerDecision(
                    allow_sync=self._config.backfill_only_mode,  # 可选允许低频 backfill
                    is_backfill_only=True,
                    suggested_batch_size=self._config.degraded_batch_size,
                    suggested_forward_window_seconds=self._config.degraded_forward_window_seconds,
                    suggested_diff_mode="none",
                    wait_seconds=self._config.backfill_interval_seconds,
                    next_allowed_at=now + self._config.backfill_interval_seconds,
                    current_state=CircuitState.OPEN.value,
                    trigger_reason=reason,
                    health_stats=health_stats,
                )
            
            # 正常允许同步
            return CircuitBreakerDecision(
                allow_sync=True,
                is_backfill_only=False,
                current_state=CircuitState.CLOSED.value,
                health_stats=health_stats,
            )
        
        elif self._state == CircuitState.OPEN:
            # 熔断状态：检查是否应转换到半开
            if self._should_transition_to_half_open(now):
                self._state = CircuitState.HALF_OPEN
                self._half_open_attempts = 0
                self._half_open_successes = 0
                
                logger.info(f"熔断器进入半开状态: key={self._key}")
                
                # 允许探测性同步，使用 probe 模式
                return CircuitBreakerDecision(
                    allow_sync=True,
                    is_backfill_only=True,  # 先用 backfill 模式探测
                    suggested_batch_size=self._config.degraded_batch_size,
                    suggested_forward_window_seconds=self._config.degraded_forward_window_seconds,
                    suggested_diff_mode="none",
                    current_state=CircuitState.HALF_OPEN.value,
                    trigger_reason=self._last_failure_reason,
                    health_stats=health_stats,
                    # HALF_OPEN 探测模式相关
                    is_probe_mode=True,
                    probe_budget=self._config.probe_budget_per_interval,
                    probe_job_types_allowlist=list(self._config.probe_job_types_allowlist),
                )
            
            # 仍在熔断中，计算剩余等待时间
            if self._opened_at is not None:
                elapsed = now - self._opened_at
                remaining = self._config.open_duration_seconds - elapsed
                wait_seconds = max(0.0, remaining)
            else:
                wait_seconds = self._config.open_duration_seconds
            
            return CircuitBreakerDecision(
                allow_sync=self._config.backfill_only_mode,
                is_backfill_only=True,
                suggested_batch_size=self._config.degraded_batch_size,
                suggested_forward_window_seconds=self._config.degraded_forward_window_seconds,
                suggested_diff_mode="none",
                wait_seconds=wait_seconds,
                next_allowed_at=now + wait_seconds,
                current_state=CircuitState.OPEN.value,
                trigger_reason=self._last_failure_reason,
                health_stats=health_stats,
            )
        
        else:  # HALF_OPEN
            # 半开状态：检查是否超过最大探测次数
            if self._half_open_attempts >= self._config.half_open_max_requests:
                # 如果探测次数用完但没有足够成功，重新熔断
                if self._half_open_successes < self._config.recovery_success_count:
                    self._state = CircuitState.OPEN
                    self._opened_at = now
                    
                    logger.warning(f"熔断器探测失败，重新熔断: key={self._key}")
                    
                    return CircuitBreakerDecision(
                        allow_sync=self._config.backfill_only_mode,
                        is_backfill_only=True,
                        suggested_batch_size=self._config.degraded_batch_size,
                        suggested_forward_window_seconds=self._config.degraded_forward_window_seconds,
                        suggested_diff_mode="none",
                        wait_seconds=self._config.open_duration_seconds,
                        next_allowed_at=now + self._config.open_duration_seconds,
                        current_state=CircuitState.OPEN.value,
                        trigger_reason="half_open_probe_failed",
                        health_stats=health_stats,
                    )
            
            # 允许探测性同步（渐进恢复参数）
            # 随着成功次数增加，逐步恢复参数
            recovery_factor = (self._half_open_successes + 1) / self._config.recovery_success_count
            suggested_batch_size = int(
                self._config.degraded_batch_size + 
                (100 - self._config.degraded_batch_size) * min(1.0, recovery_factor)
            )
            
            return CircuitBreakerDecision(
                allow_sync=True,
                is_backfill_only=self._half_open_successes < 1,  # 第一次成功后允许增量
                suggested_batch_size=suggested_batch_size,
                suggested_forward_window_seconds=self._config.degraded_forward_window_seconds,
                suggested_diff_mode="best_effort" if self._half_open_successes > 0 else "none",
                current_state=CircuitState.HALF_OPEN.value,
                trigger_reason=self._last_failure_reason,
                health_stats=health_stats,
                # HALF_OPEN 探测模式相关
                is_probe_mode=True,
                probe_budget=self._config.probe_budget_per_interval,
                probe_job_types_allowlist=list(self._config.probe_job_types_allowlist),
            )
    
    def record_result(self, success: bool, error_category: Optional[str] = None) -> None:
        """
        记录同步结果（用于半开状态的恢复判断）
        
        Args:
            success: 是否成功
            error_category: 失败时的错误类别
        """
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_attempts += 1
            
            if success:
                self._half_open_successes += 1
                
                # 检查是否达到恢复条件
                if self._half_open_successes >= self._config.recovery_success_count:
                    self._state = CircuitState.CLOSED
                    self._opened_at = None
                    self._half_open_attempts = 0
                    self._half_open_successes = 0
                    self._last_failure_reason = None
                    
                    logger.info(f"熔断器恢复到正常状态: key={self._key}")
            else:
                # 失败，重新熔断
                self._half_open_successes = 0
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                self._last_failure_reason = error_category or "half_open_probe_failed"
                
                logger.warning(f"熔断器探测失败，重新熔断: key={self._key}, error={error_category}")
    
    def force_open(self, reason: str = "manual") -> None:
        """
        强制打开熔断器
        
        Args:
            reason: 熔断原因
        """
        self._state = CircuitState.OPEN
        self._opened_at = time.time()
        self._last_failure_reason = reason
        self._half_open_attempts = 0
        self._half_open_successes = 0
        
        logger.warning(f"熔断器被强制打开: key={self._key}, reason={reason}")
    
    def force_close(self) -> None:
        """强制关闭熔断器（恢复正常）"""
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._half_open_attempts = 0
        self._half_open_successes = 0
        self._last_failure_reason = None
        
        logger.info(f"熔断器被强制关闭: key={self._key}")
    
    def get_state_dict(self) -> Dict[str, Any]:
        """
        获取可序列化的状态字典（用于持久化）
        
        Returns:
            状态字典
        """
        return {
            "state": self._state.value,
            "opened_at": self._opened_at,
            "half_open_attempts": self._half_open_attempts,
            "half_open_successes": self._half_open_successes,
            "last_failure_reason": self._last_failure_reason,
            "key": self._key,
            # 平滑状态
            "smoothed_failure_rate": self._smoothed_failure_rate,
            "smoothed_rate_limit_rate": self._smoothed_rate_limit_rate,
            "smoothed_timeout_rate": self._smoothed_timeout_rate,
        }
    
    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        """
        从字典加载状态（用于持久化恢复）
        
        Args:
            state_dict: 状态字典
        """
        state_value = state_dict.get("state", "closed")
        try:
            self._state = CircuitState(state_value)
        except ValueError:
            self._state = CircuitState.CLOSED
        
        self._opened_at = state_dict.get("opened_at")
        self._half_open_attempts = state_dict.get("half_open_attempts", 0)
        self._half_open_successes = state_dict.get("half_open_successes", 0)
        self._last_failure_reason = state_dict.get("last_failure_reason")
        
        # 加载平滑状态
        self._smoothed_failure_rate = state_dict.get("smoothed_failure_rate")
        self._smoothed_rate_limit_rate = state_dict.get("smoothed_rate_limit_rate")
        self._smoothed_timeout_rate = state_dict.get("smoothed_timeout_rate")
    
    def reset(self) -> None:
        """重置熔断器到初始状态"""
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._half_open_attempts = 0
        self._half_open_successes = 0
        self._last_failure_reason = None
        
        # 重置平滑状态
        self._smoothed_failure_rate = None
        self._smoothed_rate_limit_rate = None
        self._smoothed_timeout_rate = None
        
        logger.info(f"熔断器已重置: key={self._key}")


# ============ 熔断 Key 构建 ============


def normalize_instance_key_for_cb(instance_key: Optional[str]) -> Optional[str]:
    """
    规范化实例标识，用于构建熔断 key
    
    从 URL 或 hostname 提取统一的实例标识符。
    
    Args:
        instance_key: 实例标识（可以是 URL 或 hostname）
    
    Returns:
        规范化后的实例标识（hostname），或 None
    
    Examples:
        >>> normalize_instance_key_for_cb("https://gitlab.example.com/group/project")
        'gitlab.example.com'
        >>> normalize_instance_key_for_cb("gitlab.example.com")
        'gitlab.example.com'
        >>> normalize_instance_key_for_cb(None)
        None
    """
    if not instance_key:
        return None
    
    instance_key = instance_key.strip()
    if not instance_key:
        return None
    
    # 如果是 URL，解析出 hostname
    if "://" in instance_key:
        try:
            parsed = urlparse(instance_key)
            return parsed.netloc.lower() if parsed.netloc else None
        except Exception:
            return None
    
    # 否则当作 hostname 直接使用
    return instance_key.lower()


def build_circuit_breaker_key(
    project_key: str = "default",
    scope: str = "global",
    pool_name: Optional[str] = None,
    *,
    instance_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
    worker_pool: Optional[str] = None,
) -> str:
    """
    构建熔断状态的规范化 key（统一入口）
    
    这是 scheduler 和 worker 共用的 key 构建函数，确保对于相同的
    instance/tenant/pool 配置，生成的 key 完全一致。
    
    Key 规范: <project_key>:<scope>
    
    scope 可选值:
    - 'global': 全局熔断状态
    - 'instance:<instance_key>': 特定 GitLab 实例的熔断状态
    - 'tenant:<tenant_id>': 特定租户的熔断状态
    - 'pool:<pool_name>': 特定 worker pool 的熔断状态
    
    参数优先级:
    - 如果提供 scope 且非 'global'，直接使用（向后兼容）
    - 如果提供 worker_pool/pool_name，生成 pool:xxx scope
    - 如果提供 instance_key，生成 instance:xxx scope
    - 如果提供 tenant_id，生成 tenant:xxx scope
    - 否则使用 'global'
    
    向后兼容:
    - 前三个位置参数保持与旧签名兼容：(project_key, scope, pool_name)
    - 新增的 instance_key/tenant_id/worker_pool 为仅限关键字参数
    
    Args:
        project_key: 项目标识（默认 'default'）
        scope: 范围标识（默认 'global'，可被其他参数覆盖）
        pool_name: pool 名称（向后兼容）
        instance_key: GitLab 实例标识（URL 或 hostname，仅限关键字参数）
        tenant_id: 租户标识（仅限关键字参数）
        worker_pool: Worker pool 名称（仅限关键字参数，等价于 pool_name）
    
    Returns:
        规范化的 key 字符串
    
    Examples:
        >>> build_circuit_breaker_key()
        'default:global'
        >>> build_circuit_breaker_key('myproject')
        'myproject:global'
        >>> build_circuit_breaker_key('myproject', 'global')
        'myproject:global'
        >>> build_circuit_breaker_key(project_key='myproject')
        'myproject:global'
        >>> build_circuit_breaker_key(worker_pool='gitlab-prod')
        'default:pool:gitlab-prod'
        >>> build_circuit_breaker_key(instance_key='https://gitlab.example.com')
        'default:instance:gitlab.example.com'
        >>> build_circuit_breaker_key(tenant_id='tenant-a')
        'default:tenant:tenant-a'
        >>> build_circuit_breaker_key('myproject', 'pool:custom')
        'myproject:pool:custom'
    """
    # 规范化 project_key
    project_key = project_key or "default"
    
    # 合并 worker_pool 和 pool_name（worker_pool 优先）
    effective_pool = worker_pool or pool_name
    
    # 确定 scope
    # 如果 scope 已经是具体值（非 global），保持不变
    if scope and scope != "global":
        final_scope = scope
    elif effective_pool:
        # 有 pool 配置，使用 pool scope
        final_scope = f"pool:{effective_pool}"
    elif instance_key:
        # 有 instance 配置，使用 instance scope
        normalized_instance = normalize_instance_key_for_cb(instance_key)
        if normalized_instance:
            final_scope = f"instance:{normalized_instance}"
        else:
            final_scope = "global"
    elif tenant_id:
        # 有 tenant 配置，使用 tenant scope
        final_scope = f"tenant:{tenant_id}"
    else:
        # 默认使用 global
        final_scope = "global"
    
    return f"{project_key}:{final_scope}"


def get_legacy_key_fallbacks(key: str) -> List[str]:
    """
    获取旧 key 格式的回退列表（用于兼容读取）
    
    当使用新的 key 格式读取失败时，可以尝试这些旧格式的 key。
    这确保了从旧版本升级时，已存储的熔断状态不会丢失。
    
    Args:
        key: 新格式的 key（如 'default:global'）
    
    Returns:
        可能的旧 key 格式列表
    
    Examples:
        >>> get_legacy_key_fallbacks('default:global')
        ['global']
        >>> get_legacy_key_fallbacks('default:pool:gitlab-prod')
        ['pool:gitlab-prod', 'gitlab-prod']
        >>> get_legacy_key_fallbacks('myproject:instance:gitlab.example.com')
        ['instance:gitlab.example.com', 'gitlab.example.com']
    """
    fallbacks = []
    
    # 解析新 key 格式
    parts = key.split(":", 1) if key else []
    
    if len(parts) == 2:
        project_key, scope = parts
        
        # 旧全局 key 格式
        if scope == "global":
            fallbacks.append("global")
        
        # 如果 scope 是 pool:<name>，也尝试直接用 pool name
        elif scope.startswith("pool:"):
            pool_name = scope[5:]  # 去掉 'pool:' 前缀
            fallbacks.append(f"pool:{pool_name}")
            fallbacks.append(pool_name)
        
        # 如果 scope 是 instance:<name>，也尝试直接用 instance name
        elif scope.startswith("instance:"):
            instance_name = scope[9:]  # 去掉 'instance:' 前缀
            fallbacks.append(f"instance:{instance_name}")
            fallbacks.append(instance_name)
        
        # 如果 scope 是 tenant:<name>，也尝试直接用 tenant name
        elif scope.startswith("tenant:"):
            tenant_name = scope[7:]  # 去掉 'tenant:' 前缀
            fallbacks.append(f"tenant:{tenant_name}")
            fallbacks.append(tenant_name)
    
    # 也尝试原始 key 作为旧格式（但排除 worker:xxx 格式）
    if key and ":" in key:
        if not key.startswith("worker:"):
            fallbacks.append(key)
    
    return fallbacks
