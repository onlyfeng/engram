#!/usr/bin/env python3
"""
dual_read_calibrate.py - 双读阈值校准工具

从 Nightly 产物（dual-read-results.json）或 seek_query 的 batch JSON 输出中
解析双读指标，并输出建议的 STEP3_DUAL_READ_* 环境变量值。

功能：
1. 解析 QueryResult 列表中的 compare_report.metrics 和 dual_read.metrics
2. 汇总统计各项指标的分布（P50、P90、P95、P99、max）
3. 根据统计结果建议 warn/fail 阈值
4. 输出 JSON 格式的统计摘要和环境变量配置建议

输入格式支持：
1. Nightly 产物：单个 JSON 文件，包含 {"results": [QueryResult, ...]} 或 [QueryResult, ...]
2. seek_query batch 输出：同上

输出格式：
{
    "summary": {
        "total_queries": N,
        "error_count": N,
        "metrics_available": N,
        "timestamp": "ISO格式时间戳"
    },
    "distributions": {
        "hit_overlap_ratio": {"min": X, "p50": X, "p90": X, "p95": X, "p99": X, "max": X},
        "avg_score_diff": {...},
        "max_score_diff": {...},
        "p95_score_diff": {...},
        "avg_rank_drift": {...},
        "rbo": {...},
        "latency_ratio": {...}
    },
    "recommendations": {
        "STEP3_DUAL_READ_OVERLAP_MIN_WARN": 0.X,
        "STEP3_DUAL_READ_OVERLAP_MIN_FAIL": 0.X,
        "STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX": 0.X,
        "STEP3_DUAL_READ_RBO_MIN_WARN": 0.X,
        "STEP3_DUAL_READ_RBO_MIN_FAIL": 0.X,
        "STEP3_DUAL_READ_RANK_P95_MAX_WARN": N,
        "STEP3_DUAL_READ_RANK_P95_MAX_FAIL": N,
        "STEP3_DUAL_READ_LATENCY_RATIO_MAX": X.X
    },
    "env_export": "export STEP3_DUAL_READ_OVERLAP_MIN_WARN=0.X && ..."
}

使用:
    python -m scripts.dual_read_calibrate --input dual-read-results.json
    python -m scripts.dual_read_calibrate --input results.json --output recommendations.json
    python -m scripts.dual_read_calibrate --input results.json --format env
"""

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# 数据结构定义
# =============================================================================

@dataclass
class MetricsSample:
    """
    从单个 QueryResult 提取的指标样本
    
    支持两种来源：
    1. compare_report.metrics (CompareMetrics)
    2. dual_read.metrics (DualReadStats 中的 metrics 子字典)
    """
    # 命中重叠率
    hit_overlap_ratio: Optional[float] = None
    
    # 分数漂移指标
    avg_score_diff: Optional[float] = None
    max_score_diff: Optional[float] = None
    p95_score_diff: Optional[float] = None
    
    # 排名漂移指标
    avg_rank_drift: Optional[float] = None
    max_rank_drift: Optional[int] = None
    
    # RBO (Rank-Biased Overlap)
    rbo: Optional[float] = None
    
    # 延迟比率
    latency_ratio: Optional[float] = None
    
    # 来源标识
    source: str = ""  # "compare_report" | "dual_read"


@dataclass
class MetricsDistribution:
    """
    指标值的统计分布
    """
    name: str = ""
    count: int = 0
    min_val: float = 0.0
    max_val: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "count": self.count,
            "min": round(self.min_val, 4),
            "max": round(self.max_val, 4),
            "p50": round(self.p50, 4),
            "p90": round(self.p90, 4),
            "p95": round(self.p95, 4),
            "p99": round(self.p99, 4),
            "mean": round(self.mean, 4),
            "std": round(self.std, 4),
        }


@dataclass
class ThresholdRecommendation:
    """
    阈值推荐
    
    包含 warn 和 fail 两级阈值建议。
    """
    env_var: str = ""        # 环境变量名
    value: float = 0.0       # 推荐值
    level: str = "warn"      # "warn" | "fail"
    reason: str = ""         # 推荐理由
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "env_var": self.env_var,
            "value": self.value if isinstance(self.value, int) else round(self.value, 4),
            "level": self.level,
            "reason": self.reason,
        }


@dataclass
class CalibrationResult:
    """
    校准结果汇总
    """
    # 摘要统计
    total_queries: int = 0
    error_count: int = 0
    metrics_available: int = 0
    timestamp: str = ""
    
    # 指标分布
    distributions: Dict[str, MetricsDistribution] = field(default_factory=dict)
    
    # 阈值推荐
    recommendations: Dict[str, ThresholdRecommendation] = field(default_factory=dict)
    
    # 原始样本（可选，用于调试）
    samples: List[MetricsSample] = field(default_factory=list)
    
    def to_dict(self, include_samples: bool = False) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            "summary": {
                "total_queries": self.total_queries,
                "error_count": self.error_count,
                "metrics_available": self.metrics_available,
                "timestamp": self.timestamp,
            },
            "distributions": {
                name: dist.to_dict() for name, dist in self.distributions.items()
            },
            "recommendations": {
                rec.env_var: rec.value if isinstance(rec.value, int) else round(rec.value, 4)
                for rec in self.recommendations.values()
            },
            "recommendation_details": {
                rec.env_var: rec.to_dict() for rec in self.recommendations.values()
            },
            "env_export": self._generate_env_export(),
        }
        
        if include_samples:
            result["samples"] = [
                {k: v for k, v in s.__dict__.items() if v is not None}
                for s in self.samples
            ]
        
        return result
    
    def _generate_env_export(self) -> str:
        """生成 shell export 命令"""
        exports = []
        for rec in self.recommendations.values():
            val = rec.value if isinstance(rec.value, int) else round(rec.value, 4)
            exports.append(f"export {rec.env_var}={val}")
        return " && ".join(exports)


# =============================================================================
# 解析函数
# =============================================================================

def parse_query_result(data: Dict[str, Any]) -> Optional[MetricsSample]:
    """
    从单个 QueryResult 字典解析指标样本
    
    支持两种数据来源：
    1. compare_report.metrics (CompareMetrics)
    2. dual_read.metrics (DualReadStats 中的 metrics)
    
    Args:
        data: QueryResult 的字典表示
        
    Returns:
        MetricsSample 或 None（如果无可用指标）
    """
    sample = MetricsSample()
    metrics_found = False
    
    # 优先从 compare_report.metrics 提取
    compare_report = data.get("compare_report")
    if compare_report and isinstance(compare_report, dict):
        metrics = compare_report.get("metrics")
        if metrics and isinstance(metrics, dict):
            sample.source = "compare_report"
            metrics_found = True
            
            # 提取命中重叠率
            if "hit_overlap_ratio" in metrics:
                sample.hit_overlap_ratio = metrics["hit_overlap_ratio"]
            
            # 提取分数漂移指标
            if "avg_score_diff" in metrics:
                sample.avg_score_diff = metrics["avg_score_diff"]
            if "max_score_diff" in metrics:
                sample.max_score_diff = metrics["max_score_diff"]
            if "p95_score_diff" in metrics:
                sample.p95_score_diff = metrics["p95_score_diff"]
            
            # 提取排名漂移指标
            if "avg_rank_drift" in metrics:
                sample.avg_rank_drift = metrics["avg_rank_drift"]
            if "max_rank_drift" in metrics:
                sample.max_rank_drift = metrics["max_rank_drift"]
            
            # 提取延迟比率
            if "latency_ratio" in metrics:
                sample.latency_ratio = metrics["latency_ratio"]
        
        # 尝试从 metadata 中获取 RBO（如果 compare_mode=detailed）
        metadata = compare_report.get("metadata", {})
        if metadata:
            ranking_drift = metadata.get("ranking_drift", {})
            if ranking_drift and "rbo" in ranking_drift:
                sample.rbo = ranking_drift["rbo"]
    
    # 如果 compare_report 无数据，尝试从 dual_read 提取
    dual_read = data.get("dual_read")
    if dual_read and isinstance(dual_read, dict) and not metrics_found:
        dual_metrics = dual_read.get("metrics")
        if dual_metrics and isinstance(dual_metrics, dict):
            sample.source = "dual_read"
            metrics_found = True
            
            # dual_read.metrics 格式与 compare_report.metrics 略有不同
            if "overlap_ratio" in dual_metrics:
                sample.hit_overlap_ratio = dual_metrics["overlap_ratio"]
            if "score_diff_mean" in dual_metrics:
                sample.avg_score_diff = dual_metrics["score_diff_mean"]
            if "score_diff_max" in dual_metrics:
                sample.max_score_diff = dual_metrics["score_diff_max"]
        
        # 延迟信息在 dual_read.latency 中
        latency = dual_read.get("latency", {})
        if latency:
            primary_ms = latency.get("primary_ms", 0)
            shadow_ms = latency.get("shadow_ms", 0)
            if primary_ms > 0 and shadow_ms > 0:
                sample.latency_ratio = shadow_ms / primary_ms
    
    return sample if metrics_found else None


def parse_results_file(file_path: str) -> Tuple[List[Dict[str, Any]], int]:
    """
    解析结果文件
    
    支持格式：
    1. {"results": [QueryResult, ...]}
    2. [QueryResult, ...]
    3. {"success": bool, "results": [...], "aggregate_gate": {...}}
    
    Args:
        file_path: JSON 文件路径
        
    Returns:
        (query_results_list, error_count) 元组
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    results = []
    error_count = 0
    
    if isinstance(data, list):
        # 格式 2: [QueryResult, ...]
        results = data
    elif isinstance(data, dict):
        if "results" in data:
            # 格式 1 或 3
            results = data["results"]
        else:
            # 单个 QueryResult
            results = [data]
    
    # 统计错误
    for result in results:
        if result.get("error") is not None or result.get("success") is False:
            error_count += 1
    
    return results, error_count


def extract_samples(results: List[Dict[str, Any]]) -> List[MetricsSample]:
    """
    从结果列表中提取所有指标样本
    
    Args:
        results: QueryResult 字典列表
        
    Returns:
        MetricsSample 列表
    """
    samples = []
    for result in results:
        sample = parse_query_result(result)
        if sample is not None:
            samples.append(sample)
    return samples


# =============================================================================
# 统计计算函数
# =============================================================================

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


def compute_distribution(name: str, values: List[float]) -> MetricsDistribution:
    """
    计算指标分布
    
    Args:
        name: 指标名称
        values: 数值列表
        
    Returns:
        MetricsDistribution 实例
    """
    dist = MetricsDistribution(name=name, count=len(values))
    
    if not values:
        return dist
    
    dist.min_val = min(values)
    dist.max_val = max(values)
    dist.mean = sum(values) / len(values)
    
    dist.p50 = compute_percentile(values, 50)
    dist.p90 = compute_percentile(values, 90)
    dist.p95 = compute_percentile(values, 95)
    dist.p99 = compute_percentile(values, 99)
    
    # 计算标准差
    if len(values) > 1:
        variance = sum((x - dist.mean) ** 2 for x in values) / (len(values) - 1)
        dist.std = math.sqrt(variance)
    
    return dist


def compute_all_distributions(samples: List[MetricsSample]) -> Dict[str, MetricsDistribution]:
    """
    计算所有指标的分布
    
    Args:
        samples: MetricsSample 列表
        
    Returns:
        指标名称到 MetricsDistribution 的映射
    """
    # 收集各指标的值
    hit_overlap_values = []
    avg_score_diff_values = []
    max_score_diff_values = []
    p95_score_diff_values = []
    avg_rank_drift_values = []
    rbo_values = []
    latency_ratio_values = []
    
    for sample in samples:
        if sample.hit_overlap_ratio is not None:
            hit_overlap_values.append(sample.hit_overlap_ratio)
        if sample.avg_score_diff is not None:
            avg_score_diff_values.append(sample.avg_score_diff)
        if sample.max_score_diff is not None:
            max_score_diff_values.append(sample.max_score_diff)
        if sample.p95_score_diff is not None:
            p95_score_diff_values.append(sample.p95_score_diff)
        if sample.avg_rank_drift is not None:
            avg_rank_drift_values.append(sample.avg_rank_drift)
        if sample.rbo is not None:
            rbo_values.append(sample.rbo)
        if sample.latency_ratio is not None:
            latency_ratio_values.append(sample.latency_ratio)
    
    distributions = {}
    
    if hit_overlap_values:
        distributions["hit_overlap_ratio"] = compute_distribution(
            "hit_overlap_ratio", hit_overlap_values
        )
    
    if avg_score_diff_values:
        distributions["avg_score_diff"] = compute_distribution(
            "avg_score_diff", avg_score_diff_values
        )
    
    if max_score_diff_values:
        distributions["max_score_diff"] = compute_distribution(
            "max_score_diff", max_score_diff_values
        )
    
    if p95_score_diff_values:
        distributions["p95_score_diff"] = compute_distribution(
            "p95_score_diff", p95_score_diff_values
        )
    
    if avg_rank_drift_values:
        distributions["avg_rank_drift"] = compute_distribution(
            "avg_rank_drift", avg_rank_drift_values
        )
    
    if rbo_values:
        distributions["rbo"] = compute_distribution("rbo", rbo_values)
    
    if latency_ratio_values:
        distributions["latency_ratio"] = compute_distribution(
            "latency_ratio", latency_ratio_values
        )
    
    return distributions


# =============================================================================
# 阈值推荐函数
# =============================================================================

def generate_recommendations(
    distributions: Dict[str, MetricsDistribution],
    margin_factor: float = 1.2,
) -> Dict[str, ThresholdRecommendation]:
    """
    根据指标分布生成阈值推荐
    
    推荐策略：
    - hit_overlap_ratio: warn = P10, fail = P5（越低越差）
    - score_drift_p95: fail = P95 * margin_factor（越高越差）
    - rbo: warn = P10, fail = P5（越低越差）
    - rank_p95: warn = P90, fail = P95（越高越差）
    - latency_ratio: warn = P95 * margin_factor
    
    Args:
        distributions: 指标分布
        margin_factor: 安全边际系数（用于 "越高越差" 的指标）
        
    Returns:
        环境变量名到 ThresholdRecommendation 的映射
    """
    recommendations = {}
    
    # 1. 命中重叠率阈值（越低越差）
    if "hit_overlap_ratio" in distributions:
        dist = distributions["hit_overlap_ratio"]
        
        # 使用 P10 和 P5 作为 warn/fail 阈值
        # P10 近似：min + 0.1 * (P50 - min)
        warn_val = max(0.5, dist.min_val + 0.1 * (dist.p50 - dist.min_val))
        fail_val = max(0.3, dist.min_val * 0.9)
        
        recommendations["STEP3_DUAL_READ_OVERLAP_MIN_WARN"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_OVERLAP_MIN_WARN",
            value=round(min(0.9, warn_val), 2),
            level="warn",
            reason=f"基于 P10-P50 区间，min={dist.min_val:.4f}, p50={dist.p50:.4f}",
        )
        
        recommendations["STEP3_DUAL_READ_OVERLAP_MIN_FAIL"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_OVERLAP_MIN_FAIL",
            value=round(max(0.3, fail_val), 2),
            level="fail",
            reason=f"基于 min 的 90%，确保极端异常触发失败",
        )
    
    # 2. P95 分数漂移阈值（越高越差）
    if "p95_score_diff" in distributions:
        dist = distributions["p95_score_diff"]
        
        # 使用 P95 * margin_factor 作为 fail 阈值
        fail_val = dist.p95 * margin_factor
        
        recommendations["STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX",
            value=round(min(0.5, max(0.05, fail_val)), 4),
            level="fail",
            reason=f"基于 P95={dist.p95:.4f} * {margin_factor}，max={dist.max_val:.4f}",
        )
    elif "max_score_diff" in distributions:
        # 回退到 max_score_diff
        dist = distributions["max_score_diff"]
        fail_val = dist.p95 * margin_factor
        
        recommendations["STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX",
            value=round(min(0.5, max(0.05, fail_val)), 4),
            level="fail",
            reason=f"基于 max_score_diff P95={dist.p95:.4f} * {margin_factor}（无 p95_score_diff 数据）",
        )
    
    # 3. RBO 阈值（越低越差）
    if "rbo" in distributions:
        dist = distributions["rbo"]
        
        # 使用 P10 和 P5 作为 warn/fail 阈值
        warn_val = max(0.6, dist.min_val + 0.1 * (dist.p50 - dist.min_val))
        fail_val = max(0.4, dist.min_val * 0.9)
        
        recommendations["STEP3_DUAL_READ_RBO_MIN_WARN"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_RBO_MIN_WARN",
            value=round(min(0.95, warn_val), 2),
            level="warn",
            reason=f"基于 P10-P50 区间，min={dist.min_val:.4f}, p50={dist.p50:.4f}",
        )
        
        recommendations["STEP3_DUAL_READ_RBO_MIN_FAIL"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_RBO_MIN_FAIL",
            value=round(max(0.4, fail_val), 2),
            level="fail",
            reason=f"基于 min 的 90%，确保极端异常触发失败",
        )
    
    # 4. 排名漂移阈值（越高越差）
    if "avg_rank_drift" in distributions:
        dist = distributions["avg_rank_drift"]
        
        # 使用 P90 和 P95 作为 warn/fail 阈值
        warn_val = int(math.ceil(dist.p90 * margin_factor))
        fail_val = int(math.ceil(dist.p95 * margin_factor))
        
        recommendations["STEP3_DUAL_READ_RANK_P95_MAX_WARN"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_RANK_P95_MAX_WARN",
            value=max(2, min(10, warn_val)),
            level="warn",
            reason=f"基于 P90={dist.p90:.2f} * {margin_factor}",
        )
        
        recommendations["STEP3_DUAL_READ_RANK_P95_MAX_FAIL"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_RANK_P95_MAX_FAIL",
            value=max(3, min(15, fail_val)),
            level="fail",
            reason=f"基于 P95={dist.p95:.2f} * {margin_factor}",
        )
    
    # 5. 延迟比率阈值（越高越差）
    if "latency_ratio" in distributions:
        dist = distributions["latency_ratio"]
        
        # 使用 P95 * margin_factor 作为 warn 阈值
        warn_val = dist.p95 * margin_factor
        
        recommendations["STEP3_DUAL_READ_LATENCY_RATIO_MAX"] = ThresholdRecommendation(
            env_var="STEP3_DUAL_READ_LATENCY_RATIO_MAX",
            value=round(max(1.5, min(10.0, warn_val)), 1),
            level="warn",
            reason=f"基于 P95={dist.p95:.2f} * {margin_factor}",
        )
    
    return recommendations


# =============================================================================
# 校准主函数
# =============================================================================

def calibrate(
    input_files: List[str],
    margin_factor: float = 1.2,
    include_samples: bool = False,
) -> CalibrationResult:
    """
    执行校准流程
    
    Args:
        input_files: 输入文件路径列表
        margin_factor: 安全边际系数
        include_samples: 是否在结果中包含原始样本
        
    Returns:
        CalibrationResult 实例
    """
    all_results = []
    total_errors = 0
    
    # 1. 读取所有输入文件
    for file_path in input_files:
        logger.info(f"读取文件: {file_path}")
        try:
            results, errors = parse_results_file(file_path)
            all_results.extend(results)
            total_errors += errors
            logger.info(f"  - 读取 {len(results)} 条结果，{errors} 条错误")
        except Exception as e:
            logger.error(f"  - 读取失败: {e}")
            continue
    
    # 2. 提取指标样本
    samples = extract_samples(all_results)
    logger.info(f"提取 {len(samples)} 个有效指标样本（共 {len(all_results)} 条结果）")
    
    # 3. 计算分布
    distributions = compute_all_distributions(samples)
    logger.info(f"计算了 {len(distributions)} 个指标的分布")
    
    # 4. 生成推荐
    recommendations = generate_recommendations(distributions, margin_factor)
    logger.info(f"生成了 {len(recommendations)} 个阈值推荐")
    
    # 5. 构建结果
    result = CalibrationResult(
        total_queries=len(all_results),
        error_count=total_errors,
        metrics_available=len(samples),
        timestamp=datetime.now(timezone.utc).isoformat(),
        distributions=distributions,
        recommendations=recommendations,
    )
    
    if include_samples:
        result.samples = samples
    
    return result


# =============================================================================
# CLI 部分
# =============================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="双读阈值校准工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 从单个文件校准
    python -m scripts.dual_read_calibrate --input dual-read-results.json

    # 从多个文件校准
    python -m scripts.dual_read_calibrate --input results1.json results2.json

    # 输出到文件
    python -m scripts.dual_read_calibrate --input results.json --output recommendations.json

    # 仅输出环境变量
    python -m scripts.dual_read_calibrate --input results.json --format env

    # 调整安全边际系数
    python -m scripts.dual_read_calibrate --input results.json --margin 1.5
        """,
    )
    
    parser.add_argument(
        "--input", "-i",
        nargs="+",
        required=True,
        help="输入文件路径（支持多个文件）",
    )
    
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="输出文件路径（默认输出到 stdout）",
    )
    
    parser.add_argument(
        "--format", "-f",
        choices=["json", "env", "summary"],
        default="json",
        help="输出格式: json(完整 JSON)/env(仅环境变量)/summary(摘要)",
    )
    
    parser.add_argument(
        "--margin",
        type=float,
        default=1.2,
        help="安全边际系数（默认 1.2）",
    )
    
    parser.add_argument(
        "--include-samples",
        action="store_true",
        help="在输出中包含原始样本数据（用于调试）",
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细输出",
    )
    
    return parser.parse_args()


def format_output(result: CalibrationResult, format_type: str, include_samples: bool) -> str:
    """
    格式化输出
    
    Args:
        result: 校准结果
        format_type: 输出格式
        include_samples: 是否包含样本
        
    Returns:
        格式化后的字符串
    """
    if format_type == "json":
        return json.dumps(
            result.to_dict(include_samples),
            ensure_ascii=False,
            indent=2,
        )
    
    elif format_type == "env":
        return result._generate_env_export()
    
    elif format_type == "summary":
        lines = []
        lines.append("=" * 60)
        lines.append("双读阈值校准结果摘要")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"【统计信息】")
        lines.append(f"  总查询数: {result.total_queries}")
        lines.append(f"  错误数量: {result.error_count}")
        lines.append(f"  有效样本: {result.metrics_available}")
        lines.append("")
        
        if result.distributions:
            lines.append(f"【指标分布】")
            for name, dist in result.distributions.items():
                lines.append(f"  {name}:")
                lines.append(f"    count={dist.count}, min={dist.min_val:.4f}, max={dist.max_val:.4f}")
                lines.append(f"    P50={dist.p50:.4f}, P90={dist.p90:.4f}, P95={dist.p95:.4f}")
            lines.append("")
        
        if result.recommendations:
            lines.append(f"【阈值推荐】")
            for rec in result.recommendations.values():
                val = rec.value if isinstance(rec.value, int) else f"{rec.value:.4f}"
                lines.append(f"  {rec.env_var}={val}")
                lines.append(f"    级别: {rec.level}, 理由: {rec.reason}")
            lines.append("")
            
            lines.append(f"【环境变量导出命令】")
            lines.append(f"  {result._generate_env_export()}")
        
        lines.append("=" * 60)
        return "\n".join(lines)
    
    return ""


def main() -> int:
    """主入口"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 检查输入文件
    for file_path in args.input:
        if not Path(file_path).exists():
            logger.error(f"输入文件不存在: {file_path}")
            return 1
    
    try:
        # 执行校准
        result = calibrate(
            input_files=args.input,
            margin_factor=args.margin,
            include_samples=args.include_samples,
        )
        
        # 格式化输出
        output = format_output(result, args.format, args.include_samples)
        
        # 写入或打印输出
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            logger.info(f"输出已写入: {args.output}")
        else:
            print(output)
        
        return 0
        
    except Exception as e:
        logger.exception(f"校准失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
