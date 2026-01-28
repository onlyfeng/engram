"""
dual_read_compare.py - 双读对比数据结构定义

本模块定义用于双读（Dual Read）场景下对比两个后端查询结果的数据结构。
适用于 PGVector 与 SeekDB 后端切换过程中的结果一致性验证。

## 字段含义说明

### CompareThresholds（对比阈值）
用于配置对比判定的容差和阈值：
- score_tolerance: 相似度分数容差（0.0-1.0），两后端分数差异在此范围内视为一致
- rank_drift_max: 最大排名漂移，同一文档在两后端排名差异的容许上限
- hit_overlap_min: 最小命中重叠率（0.0-1.0），两后端 top_k 结果的交集占比下限
- latency_ratio_max: 延迟比率上限，次要后端延迟/主后端延迟的最大允许值

### CompareMetrics（对比指标）
实际计算出的对比指标值：
- avg_score_diff: 平均分数差异（对齐文档的平均 |score_a - score_b|）
- max_score_diff: 最大分数差异
- avg_rank_drift: 平均排名漂移
- max_rank_drift: 最大排名漂移
- hit_overlap_ratio: 命中重叠率（交集数量 / 并集数量）
- latency_ratio: 延迟比率（secondary_ms / primary_ms）
- primary_latency_ms: 主后端延迟（毫秒）
- secondary_latency_ms: 次要后端延迟（毫秒）
- primary_hit_count: 主后端命中数
- secondary_hit_count: 次要后端命中数

### CompareDecision（对比决策）
基于阈值和指标得出的判定结果：
- passed: 是否通过一致性检查（True=两后端结果一致性可接受）
- reason: 判定原因说明
- violated_checks: 未通过的检查项列表

### CompareReport（对比报告）
完整的对比报告，汇总上述所有信息：
- request_id: 请求标识（用于追踪）
- thresholds: 使用的阈值配置
- metrics: 计算出的指标
- decision: 最终决策
- timestamp: 报告生成时间（ISO 格式）
- metadata: 扩展元数据

## 兼容策略

为确保与现有消费者的兼容性，本模块遵循以下策略：

1. **默认不输出 compare 字段**
   - 查询响应默认不包含对比信息
   - 需显式启用（如 include_compare=True）才会输出

2. **输出时只追加字段**
   - 对比信息作为新字段追加到响应中
   - 不修改、不覆盖现有字段
   - 字段名使用 `compare_` 前缀避免冲突

3. **字段可选性**
   - CompareReport 的所有子字段均可独立省略
   - 消费者应使用 `.get()` 或类似方式安全访问

4. **版本兼容**
   - 新增字段向后兼容，旧消费者可忽略
   - 字段语义一旦定义不变更，只可追加
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
import math
import os


# =============================================================================
# 环境变量命名常量
# =============================================================================

# 命中重叠率阈值（0.0-1.0）
ENV_OVERLAP_MIN_WARN = "STEP3_DUAL_READ_OVERLAP_MIN_WARN"  # 警告级别，默认 0.7
ENV_OVERLAP_MIN_FAIL = "STEP3_DUAL_READ_OVERLAP_MIN_FAIL"  # 失败级别，默认 0.5

# 分数漂移阈值
ENV_SCORE_DRIFT_P95_MAX = "STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"  # P95 分数差异上限，默认 0.1

# RBO 阈值（0.0-1.0）
ENV_RBO_MIN_WARN = "STEP3_DUAL_READ_RBO_MIN_WARN"  # RBO 警告级别，默认 0.8
ENV_RBO_MIN_FAIL = "STEP3_DUAL_READ_RBO_MIN_FAIL"  # RBO 失败级别，默认 0.6

# 排名漂移阈值
ENV_RANK_P95_MAX_WARN = "STEP3_DUAL_READ_RANK_P95_MAX_WARN"  # P95 排名漂移警告级别，默认 3
ENV_RANK_P95_MAX_FAIL = "STEP3_DUAL_READ_RANK_P95_MAX_FAIL"  # P95 排名漂移失败级别，默认 5

# 延迟比率阈值
ENV_LATENCY_RATIO_MAX = "STEP3_DUAL_READ_LATENCY_RATIO_MAX"  # 延迟比率上限，默认 2.0

# =============================================================================
# 废弃的环境变量别名（deprecated）
# 这些旧名称仍然支持，但会映射到 canonical 名称
# =============================================================================

# 旧的命中重叠率变量名（HIT_OVERLAP -> OVERLAP）
# DEPRECATED: 请使用 STEP3_DUAL_READ_OVERLAP_MIN_WARN / STEP3_DUAL_READ_OVERLAP_MIN_FAIL
DEPRECATED_ENV_HIT_OVERLAP_MIN_WARN = "STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"
DEPRECATED_ENV_HIT_OVERLAP_MIN_FAIL = "STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL"

# 废弃别名映射：{canonical_key: [deprecated_key1, deprecated_key2, ...]}
DEPRECATED_ALIASES: Dict[str, List[str]] = {
    ENV_OVERLAP_MIN_WARN: [DEPRECATED_ENV_HIT_OVERLAP_MIN_WARN],
    ENV_OVERLAP_MIN_FAIL: [DEPRECATED_ENV_HIT_OVERLAP_MIN_FAIL],
}


# 阈值来源常量
THRESHOLD_SOURCE_DEFAULT = "default"
THRESHOLD_SOURCE_ENV = "env"
THRESHOLD_SOURCE_CLI = "cli"


@dataclass
class ThresholdsSource:
    """
    阈值来源追踪
    
    记录 CompareThresholds 各字段的来源，用于审计和调试。
    """
    # 主来源标识（default/env/cli）
    primary_source: str = THRESHOLD_SOURCE_DEFAULT
    
    # 各字段的来源追踪（字段名 -> 来源）
    field_sources: Dict[str, str] = field(default_factory=dict)
    
    # 版本信息
    version: str = ""  # 时间戳或 git SHA
    
    # 环境变量中实际设置的键
    env_keys_used: List[str] = field(default_factory=list)
    
    # CLI 参数覆盖的字段
    cli_overrides: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "primary_source": self.primary_source,
            "field_sources": self.field_sources,
            "version": self.version,
            "env_keys_used": self.env_keys_used,
            "cli_overrides": self.cli_overrides,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ThresholdsSource":
        """从字典构建 ThresholdsSource"""
        return cls(
            primary_source=data.get("primary_source", THRESHOLD_SOURCE_DEFAULT),
            field_sources=data.get("field_sources", {}),
            version=data.get("version", ""),
            env_keys_used=data.get("env_keys_used", []),
            cli_overrides=data.get("cli_overrides", []),
        )


@dataclass
class CompareThresholds:
    """
    对比阈值配置
    
    定义双读对比时的各项容差和阈值，用于判定两后端结果是否一致。
    所有阈值均有合理默认值，可按需调整。
    
    支持两级阈值（warn/fail）：
    - warn 级别触发警告但不阻断
    - fail 级别触发失败判定
    
    环境变量配置示例：
    - STEP3_DUAL_READ_OVERLAP_MIN_WARN=0.7
    - STEP3_DUAL_READ_OVERLAP_MIN_FAIL=0.5
    - STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX=0.1
    - STEP3_DUAL_READ_RBO_MIN_WARN=0.8
    - STEP3_DUAL_READ_RBO_MIN_FAIL=0.6
    - STEP3_DUAL_READ_RANK_P95_MAX_WARN=3
    - STEP3_DUAL_READ_RANK_P95_MAX_FAIL=5
    - STEP3_DUAL_READ_LATENCY_RATIO_MAX=2.0
    """
    # 分数容差：两后端同一文档的相似度分数差异上限
    # 默认 0.05 表示 5% 的容差
    score_tolerance: float = 0.05
    
    # P95 分数漂移上限
    score_drift_p95_max: float = 0.1
    
    # 最大排名漂移：同一文档在两后端排名差异的上限
    # 默认 3 表示允许前后漂移 3 位
    rank_drift_max: int = 3
    
    # P95 排名漂移上限（warn/fail 级别）
    rank_p95_max_warn: int = 3
    rank_p95_max_fail: int = 5
    
    # 最小命中重叠率：top_k 结果交集占比的下限（warn/fail 级别）
    # 默认 0.7 表示至少 70% 的结果相同
    hit_overlap_min: float = 0.7
    hit_overlap_min_warn: float = 0.7
    hit_overlap_min_fail: float = 0.5
    
    # RBO 最小值（warn/fail 级别）
    rbo_min_warn: float = 0.8
    rbo_min_fail: float = 0.6
    
    # 延迟比率上限：secondary_latency / primary_latency 的最大值
    # 默认 2.0 表示次要后端延迟不超过主后端 2 倍
    latency_ratio_max: float = 2.0
    
    # 来源追踪（不参与阈值比较）
    source: Optional[ThresholdsSource] = field(default=None, compare=False)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            "score_tolerance": self.score_tolerance,
            "score_drift_p95_max": self.score_drift_p95_max,
            "rank_drift_max": self.rank_drift_max,
            "rank_p95_max_warn": self.rank_p95_max_warn,
            "rank_p95_max_fail": self.rank_p95_max_fail,
            "hit_overlap_min": self.hit_overlap_min,
            "hit_overlap_min_warn": self.hit_overlap_min_warn,
            "hit_overlap_min_fail": self.hit_overlap_min_fail,
            "rbo_min_warn": self.rbo_min_warn,
            "rbo_min_fail": self.rbo_min_fail,
            "latency_ratio_max": self.latency_ratio_max,
        }
        if self.source is not None:
            result["source"] = self.source.to_dict()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: str = THRESHOLD_SOURCE_CLI) -> "CompareThresholds":
        """
        从字典构建 CompareThresholds
        
        Args:
            data: 阈值配置字典
            source: 来源标识（默认 cli，表示从 CLI 参数解析）
        """
        # 追踪哪些字段被显式设置
        cli_overrides = [k for k in data.keys() if k != "source"]
        
        thresholds_source = ThresholdsSource(
            primary_source=source,
            field_sources={k: source for k in cli_overrides},
            cli_overrides=cli_overrides,
            version=datetime.now().strftime("%Y%m%dT%H%M%S"),
        )
        
        return cls(
            score_tolerance=data.get("score_tolerance", 0.05),
            score_drift_p95_max=data.get("score_drift_p95_max", 0.1),
            rank_drift_max=data.get("rank_drift_max", 3),
            rank_p95_max_warn=data.get("rank_p95_max_warn", 3),
            rank_p95_max_fail=data.get("rank_p95_max_fail", 5),
            hit_overlap_min=data.get("hit_overlap_min", 0.7),
            hit_overlap_min_warn=data.get("hit_overlap_min_warn", 0.7),
            hit_overlap_min_fail=data.get("hit_overlap_min_fail", 0.5),
            rbo_min_warn=data.get("rbo_min_warn", 0.8),
            rbo_min_fail=data.get("rbo_min_fail", 0.6),
            latency_ratio_max=data.get("latency_ratio_max", 2.0),
            source=thresholds_source,
        )
    
    @classmethod
    def from_env(cls) -> "CompareThresholds":
        """
        从环境变量加载阈值配置
        
        环境变量命名规则（canonical）：
        - STEP3_DUAL_READ_OVERLAP_MIN_WARN: 命中重叠率警告阈值
        - STEP3_DUAL_READ_OVERLAP_MIN_FAIL: 命中重叠率失败阈值
        - STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX: P95 分数漂移上限
        - STEP3_DUAL_READ_RBO_MIN_WARN: RBO 警告阈值
        - STEP3_DUAL_READ_RBO_MIN_FAIL: RBO 失败阈值
        - STEP3_DUAL_READ_RANK_P95_MAX_WARN: P95 排名漂移警告阈值
        - STEP3_DUAL_READ_RANK_P95_MAX_FAIL: P95 排名漂移失败阈值
        - STEP3_DUAL_READ_LATENCY_RATIO_MAX: 延迟比率上限
        
        废弃别名（仍然支持，映射到 canonical）：
        - STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN -> STEP3_DUAL_READ_OVERLAP_MIN_WARN
        - STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL -> STEP3_DUAL_READ_OVERLAP_MIN_FAIL
        
        Returns:
            从环境变量加载的 CompareThresholds 实例（含来源追踪）
        """
        import warnings
        
        env_keys_used: List[str] = []
        field_sources: Dict[str, str] = {}
        
        def _get_env_with_aliases(
            canonical_key: str,
            deprecated_keys: Optional[List[str]] = None,
        ) -> tuple:
            """
            获取环境变量值，支持废弃别名
            
            优先级: canonical > deprecated (按顺序)
            
            Returns:
                (value, used_key): value 是字符串或 None，used_key 是实际使用的键名
            """
            # 1. 先检查 canonical 键
            val = os.environ.get(canonical_key)
            if val is not None:
                return (val, canonical_key)
            
            # 2. 检查废弃别名
            if deprecated_keys:
                for dep_key in deprecated_keys:
                    val = os.environ.get(dep_key)
                    if val is not None:
                        # 发出废弃警告
                        warnings.warn(
                            f"环境变量 {dep_key} 已废弃，请改用 {canonical_key}。",
                            DeprecationWarning,
                            stacklevel=4,
                        )
                        return (val, dep_key)
            
            return (None, None)
        
        def _get_float(key: str, field_name: str, default: float) -> float:
            deprecated_keys = DEPRECATED_ALIASES.get(key)
            val, used_key = _get_env_with_aliases(key, deprecated_keys)
            if val is None:
                field_sources[field_name] = THRESHOLD_SOURCE_DEFAULT
                return default
            try:
                env_keys_used.append(used_key)
                field_sources[field_name] = THRESHOLD_SOURCE_ENV
                return float(val)
            except ValueError:
                field_sources[field_name] = THRESHOLD_SOURCE_DEFAULT
                return default
        
        def _get_int(key: str, field_name: str, default: int) -> int:
            deprecated_keys = DEPRECATED_ALIASES.get(key)
            val, used_key = _get_env_with_aliases(key, deprecated_keys)
            if val is None:
                field_sources[field_name] = THRESHOLD_SOURCE_DEFAULT
                return default
            try:
                env_keys_used.append(used_key)
                field_sources[field_name] = THRESHOLD_SOURCE_ENV
                return int(val)
            except ValueError:
                field_sources[field_name] = THRESHOLD_SOURCE_DEFAULT
                return default
        
        # 加载各字段值
        score_tolerance = _get_float("STEP3_DUAL_READ_SCORE_TOLERANCE", "score_tolerance", 0.05)
        score_drift_p95_max = _get_float(ENV_SCORE_DRIFT_P95_MAX, "score_drift_p95_max", 0.1)
        rank_drift_max = _get_int("STEP3_DUAL_READ_RANK_DRIFT_MAX", "rank_drift_max", 3)
        rank_p95_max_warn = _get_int(ENV_RANK_P95_MAX_WARN, "rank_p95_max_warn", 3)
        rank_p95_max_fail = _get_int(ENV_RANK_P95_MAX_FAIL, "rank_p95_max_fail", 5)
        hit_overlap_min = _get_float("STEP3_DUAL_READ_OVERLAP_MIN", "hit_overlap_min", 0.7)
        hit_overlap_min_warn = _get_float(ENV_OVERLAP_MIN_WARN, "hit_overlap_min_warn", 0.7)
        hit_overlap_min_fail = _get_float(ENV_OVERLAP_MIN_FAIL, "hit_overlap_min_fail", 0.5)
        rbo_min_warn = _get_float(ENV_RBO_MIN_WARN, "rbo_min_warn", 0.8)
        rbo_min_fail = _get_float(ENV_RBO_MIN_FAIL, "rbo_min_fail", 0.6)
        latency_ratio_max = _get_float(ENV_LATENCY_RATIO_MAX, "latency_ratio_max", 2.0)
        
        # 确定主来源
        primary_source = THRESHOLD_SOURCE_ENV if env_keys_used else THRESHOLD_SOURCE_DEFAULT
        
        thresholds_source = ThresholdsSource(
            primary_source=primary_source,
            field_sources=field_sources,
            version=datetime.now().strftime("%Y%m%dT%H%M%S"),
            env_keys_used=env_keys_used,
        )
        
        return cls(
            score_tolerance=score_tolerance,
            score_drift_p95_max=score_drift_p95_max,
            rank_drift_max=rank_drift_max,
            rank_p95_max_warn=rank_p95_max_warn,
            rank_p95_max_fail=rank_p95_max_fail,
            hit_overlap_min=hit_overlap_min,
            hit_overlap_min_warn=hit_overlap_min_warn,
            hit_overlap_min_fail=hit_overlap_min_fail,
            rbo_min_warn=rbo_min_warn,
            rbo_min_fail=rbo_min_fail,
            latency_ratio_max=latency_ratio_max,
            source=thresholds_source,
        )
    
    @classmethod
    def default(cls) -> "CompareThresholds":
        """
        返回默认阈值配置（含来源追踪）
        
        Returns:
            默认的 CompareThresholds 实例
        """
        thresholds_source = ThresholdsSource(
            primary_source=THRESHOLD_SOURCE_DEFAULT,
            field_sources={
                "score_tolerance": THRESHOLD_SOURCE_DEFAULT,
                "score_drift_p95_max": THRESHOLD_SOURCE_DEFAULT,
                "rank_drift_max": THRESHOLD_SOURCE_DEFAULT,
                "rank_p95_max_warn": THRESHOLD_SOURCE_DEFAULT,
                "rank_p95_max_fail": THRESHOLD_SOURCE_DEFAULT,
                "hit_overlap_min": THRESHOLD_SOURCE_DEFAULT,
                "hit_overlap_min_warn": THRESHOLD_SOURCE_DEFAULT,
                "hit_overlap_min_fail": THRESHOLD_SOURCE_DEFAULT,
                "rbo_min_warn": THRESHOLD_SOURCE_DEFAULT,
                "rbo_min_fail": THRESHOLD_SOURCE_DEFAULT,
                "latency_ratio_max": THRESHOLD_SOURCE_DEFAULT,
            },
            version=datetime.now().strftime("%Y%m%dT%H%M%S"),
        )
        
        return cls(source=thresholds_source)


@dataclass
class CompareMetrics:
    """
    对比指标
    
    双读查询后计算出的实际对比指标值。
    包含分数差异、排名漂移、命中重叠和延迟信息。
    """
    # 分数差异指标
    avg_score_diff: float = 0.0   # 平均分数差异
    max_score_diff: float = 0.0   # 最大分数差异
    p95_score_diff: float = 0.0   # P95 分数差异（基于交集文档）
    std_score_diff: float = 0.0   # 分数差异标准差
    
    # 排名漂移指标
    avg_rank_drift: float = 0.0   # 平均排名漂移
    max_rank_drift: int = 0       # 最大排名漂移
    
    # 命中重叠指标
    hit_overlap_ratio: float = 0.0  # 命中重叠率 (intersection / union)
    common_hit_count: int = 0       # 共同命中数量
    
    # 延迟指标
    primary_latency_ms: float = 0.0    # 主后端延迟（毫秒）
    secondary_latency_ms: float = 0.0  # 次要后端延迟（毫秒）
    latency_ratio: float = 0.0         # 延迟比率
    
    # 命中数量
    primary_hit_count: int = 0     # 主后端命中数
    secondary_hit_count: int = 0   # 次要后端命中数
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "avg_score_diff": self.avg_score_diff,
            "max_score_diff": self.max_score_diff,
            "p95_score_diff": self.p95_score_diff,
            "std_score_diff": self.std_score_diff,
            "avg_rank_drift": self.avg_rank_drift,
            "max_rank_drift": self.max_rank_drift,
            "hit_overlap_ratio": self.hit_overlap_ratio,
            "common_hit_count": self.common_hit_count,
            "primary_latency_ms": self.primary_latency_ms,
            "secondary_latency_ms": self.secondary_latency_ms,
            "latency_ratio": self.latency_ratio,
            "primary_hit_count": self.primary_hit_count,
            "secondary_hit_count": self.secondary_hit_count,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompareMetrics":
        """从字典构建 CompareMetrics"""
        return cls(
            avg_score_diff=data.get("avg_score_diff", 0.0),
            max_score_diff=data.get("max_score_diff", 0.0),
            p95_score_diff=data.get("p95_score_diff", 0.0),
            std_score_diff=data.get("std_score_diff", 0.0),
            avg_rank_drift=data.get("avg_rank_drift", 0.0),
            max_rank_drift=data.get("max_rank_drift", 0),
            hit_overlap_ratio=data.get("hit_overlap_ratio", 0.0),
            common_hit_count=data.get("common_hit_count", 0),
            primary_latency_ms=data.get("primary_latency_ms", 0.0),
            secondary_latency_ms=data.get("secondary_latency_ms", 0.0),
            latency_ratio=data.get("latency_ratio", 0.0),
            primary_hit_count=data.get("primary_hit_count", 0),
            secondary_hit_count=data.get("secondary_hit_count", 0),
        )


@dataclass
class ViolationDetail:
    """
    单项违规详情
    
    记录某项检查的违规详情，包含实际值、阈值和触发原因。
    """
    # 检查项名称（如 "hit_overlap", "rbo", "rank_p95"）
    check_name: str = ""
    
    # 实际测量值
    actual_value: float = 0.0
    
    # 触发的阈值
    threshold_value: float = 0.0
    
    # 违规级别：warn 或 fail
    level: str = "warn"  # "warn" | "fail"
    
    # 触发原因描述
    reason: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "check_name": self.check_name,
            "actual_value": self.actual_value,
            "threshold_value": self.threshold_value,
            "level": self.level,
            "reason": self.reason,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ViolationDetail":
        """从字典构建 ViolationDetail"""
        return cls(
            check_name=data.get("check_name", ""),
            actual_value=data.get("actual_value", 0.0),
            threshold_value=data.get("threshold_value", 0.0),
            level=data.get("level", "warn"),
            reason=data.get("reason", ""),
        )
    
    def __str__(self) -> str:
        """格式化为可读字符串"""
        return f"[{self.level.upper()}] {self.check_name}: {self.actual_value:.4f} (threshold: {self.threshold_value:.4f}) - {self.reason}"


@dataclass
class CompareDecision:
    """
    对比决策
    
    基于阈值和指标得出的判定结果。
    包含详细的违规信息（实际值/阈值/触发原因）。
    """
    # 是否通过一致性检查（无 fail 级别违规）
    passed: bool = True
    
    # 是否有警告（有 warn 级别违规但无 fail）
    has_warnings: bool = False
    
    # 判定原因说明
    reason: str = ""
    
    # 未通过的检查项列表（简化版，向后兼容）
    # 例如: ["score_tolerance_exceeded", "hit_overlap_below_min"]
    violated_checks: List[str] = field(default_factory=list)
    
    # 详细违规信息列表（包含实际值/阈值/触发原因）
    violation_details: List[ViolationDetail] = field(default_factory=list)
    
    # 建议操作
    # "safe_to_switch": 安全切换
    # "investigate_required": 需要调查（有警告）
    # "abort_switch": 中止切换（有失败）
    recommendation: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            "passed": self.passed,
            "has_warnings": self.has_warnings,
            "reason": self.reason,
        }
        if self.violated_checks:
            result["violated_checks"] = self.violated_checks
        if self.violation_details:
            result["violation_details"] = [v.to_dict() for v in self.violation_details]
        if self.recommendation:
            result["recommendation"] = self.recommendation
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompareDecision":
        """从字典构建 CompareDecision"""
        violation_details = []
        if "violation_details" in data:
            violation_details = [
                ViolationDetail.from_dict(v) for v in data["violation_details"]
            ]
        return cls(
            passed=data.get("passed", True),
            has_warnings=data.get("has_warnings", False),
            reason=data.get("reason", ""),
            violated_checks=data.get("violated_checks", []),
            violation_details=violation_details,
            recommendation=data.get("recommendation", ""),
        )


@dataclass
class CompareReport:
    """
    对比报告
    
    汇总双读对比的完整信息，包括阈值、指标、决策和元数据。
    
    使用示例:
    ```python
    # 创建报告
    report = CompareReport(
        request_id="req-123",
        thresholds=CompareThresholds(),
        metrics=CompareMetrics(avg_score_diff=0.02, hit_overlap_ratio=0.85),
        decision=CompareDecision(passed=True, reason="All checks passed"),
    )
    
    # 转换为字典（用于 JSON 序列化）
    report_dict = report.to_dict()
    
    # 追加到查询响应（兼容策略：只追加，不修改现有字段）
    response["compare_report"] = report_dict
    ```
    """
    # 请求标识（用于追踪和关联）
    request_id: str = ""
    
    # 阈值配置
    thresholds: Optional[CompareThresholds] = None
    
    # 计算指标
    metrics: Optional[CompareMetrics] = None
    
    # 决策结果
    decision: Optional[CompareDecision] = None
    
    # 报告生成时间（ISO 格式）
    timestamp: str = ""
    
    # 主后端标识
    primary_backend: str = ""
    
    # 次要后端标识
    secondary_backend: str = ""
    
    # 扩展元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """初始化后处理"""
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat() + "Z"
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式
        
        遵循兼容策略：
        - 空值字段不输出（减少传输体积）
        - 使用 compare_ 前缀字段名（如需嵌入到其他结构）
        """
        result: Dict[str, Any] = {}
        
        if self.request_id:
            result["request_id"] = self.request_id
        
        if self.thresholds:
            result["thresholds"] = self.thresholds.to_dict()
        
        if self.metrics:
            result["metrics"] = self.metrics.to_dict()
        
        if self.decision:
            result["decision"] = self.decision.to_dict()
        
        if self.timestamp:
            result["timestamp"] = self.timestamp
        
        if self.primary_backend:
            result["primary_backend"] = self.primary_backend
        
        if self.secondary_backend:
            result["secondary_backend"] = self.secondary_backend
        
        if self.metadata:
            result["metadata"] = self.metadata
        
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompareReport":
        """从字典构建 CompareReport"""
        thresholds = None
        if "thresholds" in data:
            thresholds = CompareThresholds.from_dict(data["thresholds"])
        
        metrics = None
        if "metrics" in data:
            metrics = CompareMetrics.from_dict(data["metrics"])
        
        decision = None
        if "decision" in data:
            decision = CompareDecision.from_dict(data["decision"])
        
        return cls(
            request_id=data.get("request_id", ""),
            thresholds=thresholds,
            metrics=metrics,
            decision=decision,
            timestamp=data.get("timestamp", ""),
            primary_backend=data.get("primary_backend", ""),
            secondary_backend=data.get("secondary_backend", ""),
            metadata=data.get("metadata", {}),
        )
    
    def is_passed(self) -> bool:
        """快捷方法：检查对比是否通过"""
        return self.decision.passed if self.decision else True


# =============================================================================
# Ranking Drift 计算函数
# =============================================================================

@dataclass
class ScoreDriftMetrics:
    """
    分数漂移指标
    
    包含基于交集文档计算的分数差异统计量，用于评估两个后端返回的
    相同文档的分数一致性。
    """
    # 平均绝对分数差异（仅针对共同元素）
    avg_abs_score_diff: float = 0.0
    
    # 最大绝对分数差异
    max_abs_score_diff: float = 0.0
    
    # 95 百分位绝对分数差异
    p95_abs_score_diff: float = 0.0
    
    # 标准差
    std_score_diff: float = 0.0
    
    # 共同元素数量
    common_count: int = 0
    
    # 主后端命中数
    primary_count: int = 0
    
    # 次后端命中数
    shadow_count: int = 0
    
    # 所有差异值列表（用于进一步分析）
    all_diffs: List[float] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "avg_abs_score_diff": self.avg_abs_score_diff,
            "max_abs_score_diff": self.max_abs_score_diff,
            "p95_abs_score_diff": self.p95_abs_score_diff,
            "std_score_diff": self.std_score_diff,
            "common_count": self.common_count,
            "primary_count": self.primary_count,
            "shadow_count": self.shadow_count,
        }
    
    def check_threshold(
        self,
        p95_max: Optional[float] = None,
        avg_max: Optional[float] = None,
        max_max: Optional[float] = None,
    ) -> Tuple[bool, List[str]]:
        """
        检查分数漂移是否满足阈值要求
        
        Args:
            p95_max: P95 绝对差异上限
            avg_max: 平均绝对差异上限
            max_max: 最大绝对差异上限
            
        Returns:
            (passed, violations): 是否通过检查，以及违规项列表
        """
        violations = []
        
        if p95_max is not None and self.p95_abs_score_diff > p95_max:
            violations.append(
                f"p95_abs_score_diff={self.p95_abs_score_diff:.4f} > {p95_max:.4f}"
            )
        
        if avg_max is not None and self.avg_abs_score_diff > avg_max:
            violations.append(
                f"avg_abs_score_diff={self.avg_abs_score_diff:.4f} > {avg_max:.4f}"
            )
        
        if max_max is not None and self.max_abs_score_diff > max_max:
            violations.append(
                f"max_abs_score_diff={self.max_abs_score_diff:.4f} > {max_max:.4f}"
            )
        
        return len(violations) == 0, violations


@dataclass
class RankingDriftMetrics:
    """
    排名漂移指标
    
    包含多种排名一致性度量指标，用于评估两个排序列表的差异程度。
    """
    # 平均绝对排名差异（仅针对共同元素）
    avg_abs_rank_diff: float = 0.0
    
    # 95 百分位绝对排名差异
    p95_abs_rank_diff: float = 0.0
    
    # Top-1 是否相同
    top1_same: bool = False
    
    # Top-3 Jaccard 相似度
    top3_jaccard: float = 0.0
    
    # Top-5 Jaccard 相似度
    top5_jaccard: float = 0.0
    
    # Top-10 Jaccard 相似度
    top10_jaccard: float = 0.0
    
    # Rank-Biased Overlap (p=0.9)
    rbo: float = 0.0
    
    # 共同元素数量
    common_count: int = 0
    
    # 主列表长度
    primary_len: int = 0
    
    # 次列表长度
    shadow_len: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "avg_abs_rank_diff": self.avg_abs_rank_diff,
            "p95_abs_rank_diff": self.p95_abs_rank_diff,
            "top1_same": self.top1_same,
            "top3_jaccard": self.top3_jaccard,
            "top5_jaccard": self.top5_jaccard,
            "top10_jaccard": self.top10_jaccard,
            "rbo": self.rbo,
            "common_count": self.common_count,
            "primary_len": self.primary_len,
            "shadow_len": self.shadow_len,
        }


def stabilize_ranking(
    items: List[Tuple[str, float]],
) -> List[str]:
    """
    稳定化排序：对相同分数的项按 chunk_id 二次排序
    
    用于消除 score tie 导致的随机漂移噪声。
    
    Args:
        items: 列表，每个元素为 (chunk_id, score) 元组
        
    Returns:
        按 score 降序、score 相同时按 chunk_id 升序排列后的 chunk_id 列表
    """
    # 按 (-score, chunk_id) 排序：score 降序，chunk_id 升序（字典序）
    sorted_items = sorted(items, key=lambda x: (-x[1], x[0]))
    return [item[0] for item in sorted_items]


def compute_rbo(
    list1: List[str],
    list2: List[str],
    p: float = 0.9,
) -> float:
    """
    计算 Rank-Biased Overlap (RBO) - 带外推的版本 (RBO_EXT)
    
    RBO 是一种考虑排名位置权重的列表相似度度量。
    排名靠前的元素拥有更高的权重，参数 p 控制权重衰减速度。
    
    此实现使用 RBO_EXT（带外推），对有限列表进行外推估计，
    使得完全相同的有限列表能得到接近 1.0 的值。
    
    参考: Webber, Moffat, Zobel (2010) "A Similarity Measure for Indefinite Rankings"
    
    Args:
        list1: 第一个排序列表（元素 ID）
        list2: 第二个排序列表（元素 ID）
        p: 持久性参数（0 < p < 1），值越大，底部元素权重越大
           p=0.9 意味着 top-10 贡献约 86% 的权重
           
    Returns:
        RBO 值，范围 [0, 1]，1 表示完全一致
    """
    if not list1 or not list2:
        return 0.0 if (list1 or list2) else 1.0
    
    # 短列表和长列表
    s_len = min(len(list1), len(list2))
    l_len = max(len(list1), len(list2))
    
    # 构建集合用于计算交集
    set1 = set()
    set2 = set()
    
    # 存储每个深度的交集大小和重叠率
    x_d = []  # 交集大小
    a_d = []  # 重叠率 (agreement)
    
    for d in range(1, l_len + 1):
        if d <= len(list1):
            set1.add(list1[d - 1])
        if d <= len(list2):
            set2.add(list2[d - 1])
        
        intersection_size = len(set1 & set2)
        x_d.append(intersection_size)
        a_d.append(intersection_size / d)
    
    # 计算 RBO_MIN：已观察部分的下界
    # RBO_MIN = (1-p) * Σ_{d=1}^{l} p^{d-1} * A_d
    rbo_min = 0.0
    for d in range(1, l_len + 1):
        rbo_min += math.pow(p, d - 1) * a_d[d - 1]
    rbo_min *= (1 - p)
    
    # 计算外推项
    # 外推假设：在深度 k 之后，重叠率保持为 A_s（短列表末尾的重叠率）
    # RBO_EXT = RBO_MIN + p^l * A_l
    # 这里使用更精确的外推公式
    
    # 短列表末尾的重叠率作为外推基础
    a_s = a_d[s_len - 1] if s_len > 0 else 0.0
    
    # 计算外推残差
    # 残差 = p^s * (A_s + (X_s - s*A_s)/(s*(1-p)))
    # 简化版本：直接用 p^l * A_l 作为外推
    x_s = x_d[s_len - 1] if s_len > 0 else 0
    
    # 使用论文中的外推公式
    # RBO_EXT ≈ RBO_MIN + p^s * ((x_s + 1) / (2 * s) + (1 - p) / p * x_s / s)
    # 简化为: RBO_EXT = RBO_MIN + p^s * A_s
    # 这给出了基于当前重叠率的外推
    
    rbo_ext = rbo_min + math.pow(p, s_len) * a_s
    
    # 确保值在 [0, 1] 范围内
    return min(1.0, max(0.0, rbo_ext))


def compute_jaccard(set1: set, set2: set) -> float:
    """
    计算 Jaccard 相似度
    
    Args:
        set1: 第一个集合
        set2: 第二个集合
        
    Returns:
        Jaccard 系数 = |A ∩ B| / |A ∪ B|
    """
    if not set1 and not set2:
        return 1.0
    union_size = len(set1 | set2)
    if union_size == 0:
        return 1.0
    return len(set1 & set2) / union_size


def compute_percentile(values: List[float], percentile: float) -> float:
    """
    计算百分位数
    
    Args:
        values: 数值列表
        percentile: 百分位（0-100）
        
    Returns:
        对应百分位的值
    """
    if not values:
        return 0.0
    
    sorted_values = sorted(values)
    n = len(sorted_values)
    
    # 使用线性插值计算百分位
    idx = (percentile / 100.0) * (n - 1)
    lower_idx = int(idx)
    upper_idx = min(lower_idx + 1, n - 1)
    
    # 线性插值
    fraction = idx - lower_idx
    return sorted_values[lower_idx] * (1 - fraction) + sorted_values[upper_idx] * fraction


@dataclass
class OverlapMetrics:
    """
    重叠度指标
    
    用于衡量两个 ID 列表之间的重叠情况。
    """
    # 交集数量
    overlap_count: int = 0
    
    # 重叠率（按 max(len_primary, len_shadow) 计算）
    overlap_ratio: float = 0.0
    
    # Top-K 重叠率（截取前 k 个元素后计算的重叠率）
    overlap_at_k: float = 0.0
    
    # Top-K 参数（用于计算 overlap_at_k）
    top_k: int = 0
    
    # 仅在主后端出现的 ID 样本
    primary_only_ids_sample: List[str] = field(default_factory=list)
    
    # 仅在次后端出现的 ID 样本
    shadow_only_ids_sample: List[str] = field(default_factory=list)
    
    # 主后端 ID 数量
    primary_count: int = 0
    
    # 次后端 ID 数量
    shadow_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "overlap_count": self.overlap_count,
            "overlap_ratio": self.overlap_ratio,
            "overlap_at_k": self.overlap_at_k,
            "top_k": self.top_k,
            "primary_only_ids_sample": self.primary_only_ids_sample,
            "shadow_only_ids_sample": self.shadow_only_ids_sample,
            "primary_count": self.primary_count,
            "shadow_count": self.shadow_count,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OverlapMetrics":
        """从字典构建 OverlapMetrics"""
        return cls(
            overlap_count=data.get("overlap_count", 0),
            overlap_ratio=data.get("overlap_ratio", 0.0),
            overlap_at_k=data.get("overlap_at_k", 0.0),
            top_k=data.get("top_k", 0),
            primary_only_ids_sample=data.get("primary_only_ids_sample", []),
            shadow_only_ids_sample=data.get("shadow_only_ids_sample", []),
            primary_count=data.get("primary_count", 0),
            shadow_count=data.get("shadow_count", 0),
        )


# 默认采样上限
DEFAULT_SAMPLE_LIMIT = 10


def compute_overlap_metrics(
    primary_ids: List[str],
    shadow_ids: List[str],
    top_k: int,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> OverlapMetrics:
    """
    计算两个 ID 列表的重叠度指标
    
    计算主后端和次后端返回结果的重叠情况，用于评估结果一致性。
    
    Args:
        primary_ids: 主后端返回的 ID 列表
        shadow_ids: 次后端返回的 ID 列表
        top_k: 用于计算 overlap_at_k 的截取数量
        sample_limit: 采样上限，用于限制 primary_only_ids_sample 和
                     shadow_only_ids_sample 的长度，默认 10
    
    Returns:
        OverlapMetrics 对象，包含：
        - overlap_count: 交集数量
        - overlap_ratio: 重叠率（按 max(len_primary, len_shadow) 计算）
        - overlap_at_k: 截取前 top_k 个元素后的重叠率
        - primary_only_ids_sample: 仅在主后端出现的 ID 样本（最多 sample_limit 个）
        - shadow_only_ids_sample: 仅在次后端出现的 ID 样本（最多 sample_limit 个）
    
    Example:
        >>> primary = ["a", "b", "c", "d", "e"]
        >>> shadow = ["a", "c", "e", "f", "g"]
        >>> metrics = compute_overlap_metrics(primary, shadow, top_k=3)
        >>> print(f"overlap_count={metrics.overlap_count}")  # 3 (a, c, e)
        >>> print(f"overlap_ratio={metrics.overlap_ratio}")  # 0.6 (3/5)
        >>> print(f"overlap_at_k={metrics.overlap_at_k}")    # 0.667 (2/3, top-3: a,b,c vs a,c,e)
    """
    # 处理空列表情况
    if not primary_ids and not shadow_ids:
        return OverlapMetrics(
            overlap_count=0,
            overlap_ratio=1.0,  # 两个空列表视为完全一致
            overlap_at_k=1.0,
            top_k=top_k,
            primary_only_ids_sample=[],
            shadow_only_ids_sample=[],
            primary_count=0,
            shadow_count=0,
        )
    
    if not primary_ids or not shadow_ids:
        # 一个为空，一个不为空，重叠率为 0
        primary_set = set(primary_ids)
        shadow_set = set(shadow_ids)
        
        primary_only = list(primary_set)[:sample_limit]
        shadow_only = list(shadow_set)[:sample_limit]
        
        return OverlapMetrics(
            overlap_count=0,
            overlap_ratio=0.0,
            overlap_at_k=0.0,
            top_k=top_k,
            primary_only_ids_sample=primary_only,
            shadow_only_ids_sample=shadow_only,
            primary_count=len(primary_ids),
            shadow_count=len(shadow_ids),
        )
    
    # 转换为集合
    primary_set = set(primary_ids)
    shadow_set = set(shadow_ids)
    
    # 计算交集
    intersection = primary_set & shadow_set
    overlap_count = len(intersection)
    
    # 计算重叠率（按 max(len_primary, len_shadow)）
    max_len = max(len(primary_ids), len(shadow_ids))
    overlap_ratio = overlap_count / max_len if max_len > 0 else 1.0
    
    # 计算 overlap_at_k（截取前 top_k 个元素）
    primary_top_k = set(primary_ids[:top_k])
    shadow_top_k = set(shadow_ids[:top_k])
    intersection_at_k = primary_top_k & shadow_top_k
    max_len_at_k = max(len(primary_top_k), len(shadow_top_k))
    overlap_at_k = len(intersection_at_k) / max_len_at_k if max_len_at_k > 0 else 1.0
    
    # 计算仅在一方出现的 ID
    primary_only = primary_set - shadow_set
    shadow_only = shadow_set - primary_set
    
    # 采样（保持原始顺序，取前 sample_limit 个）
    primary_only_sample = [id_ for id_ in primary_ids if id_ in primary_only][:sample_limit]
    shadow_only_sample = [id_ for id_ in shadow_ids if id_ in shadow_only][:sample_limit]
    
    return OverlapMetrics(
        overlap_count=overlap_count,
        overlap_ratio=overlap_ratio,
        overlap_at_k=overlap_at_k,
        top_k=top_k,
        primary_only_ids_sample=primary_only_sample,
        shadow_only_ids_sample=shadow_only_sample,
        primary_count=len(primary_ids),
        shadow_count=len(shadow_ids),
    )


def compute_ranking_drift(
    primary_ids_ranked: Union[List[str], List[Tuple[str, float]]],
    shadow_ids_ranked: Union[List[str], List[Tuple[str, float]]],
    stabilize: bool = True,
) -> RankingDriftMetrics:
    """
    计算两个排名列表之间的漂移指标
    
    输出指标包括：
    - avg_abs_rank_diff: 共同元素的平均绝对排名差异
    - p95_abs_rank_diff: 共同元素的 95 百分位绝对排名差异
    - top1_same: Top-1 结果是否相同
    - top3_jaccard: Top-3 的 Jaccard 相似度
    - rbo: Rank-Biased Overlap (p=0.9)
    
    Args:
        primary_ids_ranked: 主后端排名列表，可以是：
            - List[str]: 已排序的 chunk_id 列表
            - List[Tuple[str, float]]: (chunk_id, score) 元组列表（将进行稳定化排序）
        shadow_ids_ranked: 次后端排名列表，格式同上
        stabilize: 是否对 (chunk_id, score) 输入进行稳定化排序
                   稳定化排序会按 score 降序、chunk_id 升序排列，
                   以消除 score tie 导致的随机漂移噪声
        
    Returns:
        RankingDriftMetrics 对象，包含各项漂移指标
    """
    # 处理输入：如果是 (id, score) 元组列表，进行稳定化排序
    if primary_ids_ranked and isinstance(primary_ids_ranked[0], tuple):
        if stabilize:
            primary_list = stabilize_ranking(primary_ids_ranked)
        else:
            primary_list = [item[0] for item in primary_ids_ranked]
    else:
        primary_list = list(primary_ids_ranked) if primary_ids_ranked else []
    
    if shadow_ids_ranked and isinstance(shadow_ids_ranked[0], tuple):
        if stabilize:
            shadow_list = stabilize_ranking(shadow_ids_ranked)
        else:
            shadow_list = [item[0] for item in shadow_ids_ranked]
    else:
        shadow_list = list(shadow_ids_ranked) if shadow_ids_ranked else []
    
    # 初始化结果
    metrics = RankingDriftMetrics(
        primary_len=len(primary_list),
        shadow_len=len(shadow_list),
    )
    
    # 处理空列表情况
    if not primary_list and not shadow_list:
        metrics.top1_same = True
        metrics.top3_jaccard = 1.0
        metrics.top5_jaccard = 1.0
        metrics.top10_jaccard = 1.0
        metrics.rbo = 1.0
        return metrics
    
    if not primary_list or not shadow_list:
        # 一个为空，一个不为空
        metrics.rbo = 0.0
        return metrics
    
    # 构建排名映射（1-based rank）
    primary_rank = {id_: idx + 1 for idx, id_ in enumerate(primary_list)}
    shadow_rank = {id_: idx + 1 for idx, id_ in enumerate(shadow_list)}
    
    # 找出共同元素
    common_ids = set(primary_list) & set(shadow_list)
    metrics.common_count = len(common_ids)
    
    # 计算排名差异（仅针对共同元素）
    rank_diffs = []
    for id_ in common_ids:
        diff = abs(primary_rank[id_] - shadow_rank[id_])
        rank_diffs.append(diff)
    
    if rank_diffs:
        metrics.avg_abs_rank_diff = sum(rank_diffs) / len(rank_diffs)
        metrics.p95_abs_rank_diff = compute_percentile(rank_diffs, 95)
    
    # 计算 Top-1 是否相同
    metrics.top1_same = (
        len(primary_list) > 0 and 
        len(shadow_list) > 0 and 
        primary_list[0] == shadow_list[0]
    )
    
    # 计算 Top-K Jaccard 相似度
    def get_top_k_set(lst: List[str], k: int) -> set:
        return set(lst[:min(k, len(lst))])
    
    metrics.top3_jaccard = compute_jaccard(
        get_top_k_set(primary_list, 3),
        get_top_k_set(shadow_list, 3)
    )
    
    metrics.top5_jaccard = compute_jaccard(
        get_top_k_set(primary_list, 5),
        get_top_k_set(shadow_list, 5)
    )
    
    metrics.top10_jaccard = compute_jaccard(
        get_top_k_set(primary_list, 10),
        get_top_k_set(shadow_list, 10)
    )
    
    # 计算 RBO (p=0.9)
    metrics.rbo = compute_rbo(primary_list, shadow_list, p=0.9)
    
    return metrics


# =============================================================================
# Score Drift 计算函数
# =============================================================================

def compute_score_drift(
    primary_hits: List[Tuple[str, float]],
    shadow_hits: List[Tuple[str, float]],
) -> ScoreDriftMetrics:
    """
    计算两个后端返回结果之间的分数漂移指标
    
    基于两个后端返回的共同文档（交集），计算分数差异的各项统计量。
    
    算法步骤：
    1. 构建 id->score 映射
    2. 计算两个后端的文档 ID 交集
    3. 针对交集中的每个文档，计算分数绝对差异
    4. 计算统计量：平均值、最大值、P95、标准差
    
    Args:
        primary_hits: 主后端命中结果，每个元素为 (chunk_id, score) 元组
        shadow_hits: 次后端命中结果，每个元素为 (chunk_id, score) 元组
        
    Returns:
        ScoreDriftMetrics 对象，包含分数漂移各项指标
        
    Example:
        >>> primary = [("doc1", 0.95), ("doc2", 0.85), ("doc3", 0.75)]
        >>> shadow = [("doc1", 0.92), ("doc2", 0.88), ("doc4", 0.70)]
        >>> metrics = compute_score_drift(primary, shadow)
        >>> print(f"P95 漂移: {metrics.p95_abs_score_diff:.4f}")
        >>> # 检查阈值
        >>> passed, violations = metrics.check_threshold(p95_max=0.1)
    """
    # 初始化结果
    metrics = ScoreDriftMetrics(
        primary_count=len(primary_hits),
        shadow_count=len(shadow_hits),
    )
    
    # 处理空输入
    if not primary_hits and not shadow_hits:
        return metrics
    
    if not primary_hits or not shadow_hits:
        return metrics
    
    # 1. 构建 id->score 映射
    primary_score_map: Dict[str, float] = {
        chunk_id: score for chunk_id, score in primary_hits
    }
    shadow_score_map: Dict[str, float] = {
        chunk_id: score for chunk_id, score in shadow_hits
    }
    
    # 2. 计算交集
    primary_ids = set(primary_score_map.keys())
    shadow_ids = set(shadow_score_map.keys())
    common_ids = primary_ids & shadow_ids
    
    metrics.common_count = len(common_ids)
    
    # 无交集时返回
    if not common_ids:
        return metrics
    
    # 3. 计算分数差异
    score_diffs: List[float] = []
    for chunk_id in common_ids:
        primary_score = primary_score_map[chunk_id]
        shadow_score = shadow_score_map[chunk_id]
        abs_diff = abs(primary_score - shadow_score)
        score_diffs.append(abs_diff)
    
    metrics.all_diffs = score_diffs
    
    # 4. 计算统计量
    n = len(score_diffs)
    
    # 平均值
    metrics.avg_abs_score_diff = sum(score_diffs) / n
    
    # 最大值
    metrics.max_abs_score_diff = max(score_diffs)
    
    # P95
    metrics.p95_abs_score_diff = compute_percentile(score_diffs, 95)
    
    # 标准差
    if n > 1:
        mean = metrics.avg_abs_score_diff
        variance = sum((x - mean) ** 2 for x in score_diffs) / (n - 1)
        metrics.std_score_diff = math.sqrt(variance)
    else:
        metrics.std_score_diff = 0.0
    
    return metrics


def apply_score_drift_to_metrics(
    compare_metrics: CompareMetrics,
    score_drift: ScoreDriftMetrics,
) -> CompareMetrics:
    """
    将分数漂移指标应用到 CompareMetrics
    
    更新 CompareMetrics 中的分数相关字段。
    
    Args:
        compare_metrics: 要更新的 CompareMetrics 对象
        score_drift: 计算出的分数漂移指标
        
    Returns:
        更新后的 CompareMetrics 对象（就地修改）
    """
    compare_metrics.avg_score_diff = score_drift.avg_abs_score_diff
    compare_metrics.max_score_diff = score_drift.max_abs_score_diff
    compare_metrics.p95_score_diff = score_drift.p95_abs_score_diff
    compare_metrics.std_score_diff = score_drift.std_score_diff
    compare_metrics.common_hit_count = score_drift.common_count
    
    return compare_metrics


# =============================================================================
# 阈值评估函数
# =============================================================================

def evaluate(
    metrics: CompareMetrics,
    thresholds: CompareThresholds,
    ranking_metrics: Optional[RankingDriftMetrics] = None,
    score_drift_metrics: Optional[ScoreDriftMetrics] = None,
) -> CompareDecision:
    """
    评估对比指标是否满足阈值要求
    
    根据 metrics 和 thresholds 进行各项检查，返回包含详细违规信息的决策结果。
    每个违规项都会记录：实际值、阈值、触发原因。
    
    检查项说明：
    1. hit_overlap: 命中重叠率检查
       - warn: actual < hit_overlap_min_warn
       - fail: actual < hit_overlap_min_fail
    
    2. rbo: RBO (Rank-Biased Overlap) 检查
       - warn: actual < rbo_min_warn
       - fail: actual < rbo_min_fail
    
    3. rank_p95: P95 排名漂移检查
       - warn: actual > rank_p95_max_warn
       - fail: actual > rank_p95_max_fail
    
    4. score_drift_p95: P95 分数漂移检查
       - fail: actual > score_drift_p95_max
       - 优先使用 score_drift_metrics.p95_abs_score_diff
       - 其次使用 metrics.p95_score_diff
       - 回退到 metrics.max_score_diff
    
    5. latency_ratio: 延迟比率检查
       - warn: actual > latency_ratio_max
    
    Args:
        metrics: 对比指标（CompareMetrics）
        thresholds: 阈值配置（CompareThresholds）
        ranking_metrics: 可选的排名漂移指标（RankingDriftMetrics），
                        如果提供，将使用其中的 rbo 和 p95_abs_rank_diff
        score_drift_metrics: 可选的分数漂移指标（ScoreDriftMetrics），
                            如果提供，将使用其中的 p95_abs_score_diff 进行检查
    
    Returns:
        CompareDecision 对象，包含：
        - passed: 是否通过（无 fail 级别违规）
        - has_warnings: 是否有警告
        - violated_checks: 违规检查项名称列表（向后兼容）
        - violation_details: 详细违规信息列表
        - recommendation: 建议操作
        - reason: 综合判定原因
    
    Example:
        >>> metrics = CompareMetrics(hit_overlap_ratio=0.4, max_score_diff=0.15)
        >>> thresholds = CompareThresholds.from_env()
        >>> decision = evaluate(metrics, thresholds)
        >>> if not decision.passed:
        ...     for detail in decision.violation_details:
        ...         print(f"{detail.check_name}: {detail.actual_value} vs {detail.threshold_value}")
        
        >>> # 使用 ScoreDriftMetrics 进行更精确的 P95 检查
        >>> score_drift = compute_score_drift(primary_hits, shadow_hits)
        >>> decision = evaluate(metrics, thresholds, score_drift_metrics=score_drift)
    """
    violations: List[ViolationDetail] = []
    violated_checks: List[str] = []
    
    # 1. 检查命中重叠率 (hit_overlap)
    overlap = metrics.hit_overlap_ratio
    
    if overlap < thresholds.hit_overlap_min_fail:
        v = ViolationDetail(
            check_name="hit_overlap",
            actual_value=overlap,
            threshold_value=thresholds.hit_overlap_min_fail,
            level="fail",
            reason=f"命中重叠率 {overlap:.4f} 低于失败阈值 {thresholds.hit_overlap_min_fail:.4f}",
        )
        violations.append(v)
        violated_checks.append("hit_overlap_below_fail")
    elif overlap < thresholds.hit_overlap_min_warn:
        v = ViolationDetail(
            check_name="hit_overlap",
            actual_value=overlap,
            threshold_value=thresholds.hit_overlap_min_warn,
            level="warn",
            reason=f"命中重叠率 {overlap:.4f} 低于警告阈值 {thresholds.hit_overlap_min_warn:.4f}",
        )
        violations.append(v)
        violated_checks.append("hit_overlap_below_warn")
    
    # 2. 检查 RBO
    # 优先使用 ranking_metrics 中的 rbo，否则尝试从 metrics 获取
    rbo_value = None
    if ranking_metrics is not None:
        rbo_value = ranking_metrics.rbo
    elif hasattr(metrics, 'rbo'):
        rbo_value = getattr(metrics, 'rbo', None)
    
    if rbo_value is not None:
        if rbo_value < thresholds.rbo_min_fail:
            v = ViolationDetail(
                check_name="rbo",
                actual_value=rbo_value,
                threshold_value=thresholds.rbo_min_fail,
                level="fail",
                reason=f"RBO {rbo_value:.4f} 低于失败阈值 {thresholds.rbo_min_fail:.4f}",
            )
            violations.append(v)
            violated_checks.append("rbo_below_fail")
        elif rbo_value < thresholds.rbo_min_warn:
            v = ViolationDetail(
                check_name="rbo",
                actual_value=rbo_value,
                threshold_value=thresholds.rbo_min_warn,
                level="warn",
                reason=f"RBO {rbo_value:.4f} 低于警告阈值 {thresholds.rbo_min_warn:.4f}",
            )
            violations.append(v)
            violated_checks.append("rbo_below_warn")
    
    # 3. 检查 P95 排名漂移
    rank_p95 = None
    if ranking_metrics is not None:
        rank_p95 = ranking_metrics.p95_abs_rank_diff
    
    if rank_p95 is not None:
        if rank_p95 > thresholds.rank_p95_max_fail:
            v = ViolationDetail(
                check_name="rank_p95",
                actual_value=rank_p95,
                threshold_value=float(thresholds.rank_p95_max_fail),
                level="fail",
                reason=f"P95 排名漂移 {rank_p95:.2f} 超过失败阈值 {thresholds.rank_p95_max_fail}",
            )
            violations.append(v)
            violated_checks.append("rank_p95_above_fail")
        elif rank_p95 > thresholds.rank_p95_max_warn:
            v = ViolationDetail(
                check_name="rank_p95",
                actual_value=rank_p95,
                threshold_value=float(thresholds.rank_p95_max_warn),
                level="warn",
                reason=f"P95 排名漂移 {rank_p95:.2f} 超过警告阈值 {thresholds.rank_p95_max_warn}",
            )
            violations.append(v)
            violated_checks.append("rank_p95_above_warn")
    
    # 4. 检查 P95 分数漂移
    # 优先使用 score_drift_metrics，其次使用 metrics.p95_score_diff，最后回退到 max_score_diff
    p95_score_diff = None
    if score_drift_metrics is not None:
        p95_score_diff = score_drift_metrics.p95_abs_score_diff
    elif metrics.p95_score_diff > 0:
        p95_score_diff = metrics.p95_score_diff
    else:
        # 回退到 max_score_diff 作为近似值
        p95_score_diff = metrics.max_score_diff
    
    if p95_score_diff > thresholds.score_drift_p95_max:
        v = ViolationDetail(
            check_name="score_drift_p95",
            actual_value=p95_score_diff,
            threshold_value=thresholds.score_drift_p95_max,
            level="fail",
            reason=f"P95 分数漂移 {p95_score_diff:.4f} 超过阈值 {thresholds.score_drift_p95_max:.4f}",
        )
        violations.append(v)
        violated_checks.append("score_drift_p95_exceeded")
    
    # 5. 检查延迟比率
    if metrics.latency_ratio > thresholds.latency_ratio_max:
        v = ViolationDetail(
            check_name="latency_ratio",
            actual_value=metrics.latency_ratio,
            threshold_value=thresholds.latency_ratio_max,
            level="warn",
            reason=f"延迟比率 {metrics.latency_ratio:.2f} 超过阈值 {thresholds.latency_ratio_max:.2f}",
        )
        violations.append(v)
        violated_checks.append("latency_ratio_exceeded")
    
    # 统计违规级别
    fail_count = sum(1 for v in violations if v.level == "fail")
    warn_count = sum(1 for v in violations if v.level == "warn")
    
    passed = fail_count == 0
    has_warnings = warn_count > 0
    
    # 生成建议
    if passed and not has_warnings:
        recommendation = "safe_to_switch"
        reason = "所有检查项通过，可安全切换"
    elif passed and has_warnings:
        recommendation = "investigate_required"
        warn_names = [v.check_name for v in violations if v.level == "warn"]
        reason = f"存在 {warn_count} 个警告项 ({', '.join(warn_names)})，建议调查后再决定"
    else:
        recommendation = "abort_switch"
        fail_names = [v.check_name for v in violations if v.level == "fail"]
        reason = f"存在 {fail_count} 个失败项 ({', '.join(fail_names)})，建议中止切换"
    
    return CompareDecision(
        passed=passed,
        has_warnings=has_warnings,
        reason=reason,
        violated_checks=violated_checks,
        violation_details=violations,
        recommendation=recommendation,
    )


def evaluate_with_report(
    metrics: CompareMetrics,
    thresholds: Optional[CompareThresholds] = None,
    ranking_metrics: Optional[RankingDriftMetrics] = None,
    score_drift_metrics: Optional[ScoreDriftMetrics] = None,
    request_id: str = "",
    primary_backend: str = "",
    secondary_backend: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> CompareReport:
    """
    评估指标并生成完整的对比报告
    
    这是一个便捷函数，结合 evaluate() 和 CompareReport 创建。
    
    Args:
        metrics: 对比指标
        thresholds: 阈值配置，如果为 None 则从环境变量加载
        ranking_metrics: 可选的排名漂移指标
        score_drift_metrics: 可选的分数漂移指标（由 compute_score_drift 计算）
        request_id: 请求标识
        primary_backend: 主后端标识
        secondary_backend: 次要后端标识
        metadata: 扩展元数据
    
    Returns:
        包含完整评估结果的 CompareReport
    """
    if thresholds is None:
        thresholds = CompareThresholds.from_env()
    
    decision = evaluate(metrics, thresholds, ranking_metrics, score_drift_metrics)
    
    return CompareReport(
        request_id=request_id,
        thresholds=thresholds,
        metrics=metrics,
        decision=decision,
        primary_backend=primary_backend,
        secondary_backend=secondary_backend,
        metadata=metadata or {},
    )
