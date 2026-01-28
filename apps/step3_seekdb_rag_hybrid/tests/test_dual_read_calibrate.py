"""
test_dual_read_calibrate.py - 双读阈值校准工具单元测试

测试覆盖：
1. QueryResult 解析（parse_query_result）
   - compare_report.metrics 格式
   - dual_read.metrics 格式
   - 混合格式和边界情况
2. 结果文件解析（parse_results_file）
   - 多种 JSON 格式支持
3. 指标样本提取（extract_samples）
4. 统计分布计算（compute_distribution, compute_all_distributions）
5. 阈值推荐生成（generate_recommendations）
6. 完整校准流程（calibrate）

运行方式:
    cd apps/step3_seekdb_rag_hybrid
    pytest tests/test_dual_read_calibrate.py -v
"""

import json
import math
import pytest
import tempfile
from pathlib import Path
from typing import Dict, Any, List

from scripts.dual_read_calibrate import (
    # 数据结构
    MetricsSample,
    MetricsDistribution,
    ThresholdRecommendation,
    CalibrationResult,
    # 解析函数
    parse_query_result,
    parse_results_file,
    extract_samples,
    # 统计函数
    compute_percentile,
    compute_distribution,
    compute_all_distributions,
    # 推荐函数
    generate_recommendations,
    # 主函数
    calibrate,
    format_output,
)


# =============================================================================
# 测试数据工厂
# =============================================================================

def make_compare_report_query_result(
    hit_overlap_ratio: float = 0.8,
    avg_score_diff: float = 0.05,
    max_score_diff: float = 0.1,
    p95_score_diff: float = 0.08,
    avg_rank_drift: float = 1.5,
    max_rank_drift: int = 3,
    latency_ratio: float = 1.2,
    rbo: float = None,  # 可选，在 metadata 中
    error: str = None,
) -> Dict[str, Any]:
    """创建带 compare_report.metrics 的 QueryResult 字典"""
    result = {
        "query": "test query",
        "success": error is None,
        "evidences": [],
    }
    
    if error:
        result["error"] = error
        return result
    
    result["compare_report"] = {
        "request_id": "test-123",
        "metrics": {
            "hit_overlap_ratio": hit_overlap_ratio,
            "avg_score_diff": avg_score_diff,
            "max_score_diff": max_score_diff,
            "p95_score_diff": p95_score_diff,
            "avg_rank_drift": avg_rank_drift,
            "max_rank_drift": max_rank_drift,
            "latency_ratio": latency_ratio,
            "primary_latency_ms": 10.0,
            "secondary_latency_ms": 10.0 * latency_ratio,
            "primary_hit_count": 10,
            "secondary_hit_count": 10,
            "common_hit_count": int(10 * hit_overlap_ratio),
        },
        "decision": {
            "passed": True,
            "has_warnings": False,
            "recommendation": "safe_to_switch",
        },
    }
    
    if rbo is not None:
        result["compare_report"]["metadata"] = {
            "compare_mode": "detailed",
            "ranking_drift": {
                "rbo": rbo,
                "avg_abs_rank_diff": avg_rank_drift,
            },
        }
    
    return result


def make_dual_read_query_result(
    overlap_ratio: float = 0.8,
    score_diff_mean: float = 0.05,
    score_diff_max: float = 0.1,
    primary_ms: float = 10.0,
    shadow_ms: float = 12.0,
    error: str = None,
) -> Dict[str, Any]:
    """创建带 dual_read.metrics 的 QueryResult 字典"""
    result = {
        "query": "test query",
        "success": error is None,
        "evidences": [],
    }
    
    if error:
        result["error"] = error
        return result
    
    result["dual_read"] = {
        "health": {
            "primary": {"table": "chunks", "strategy": "per_table"},
            "shadow": {"table": "chunks_shadow", "strategy": "single_table"},
        },
        "metrics": {
            "overlap_ratio": overlap_ratio,
            "primary_count": 10,
            "shadow_count": 10,
            "common_count": int(10 * overlap_ratio),
            "only_primary_count": int(10 * (1 - overlap_ratio) / 2),
            "only_shadow_count": int(10 * (1 - overlap_ratio) / 2),
            "score_diff_mean": score_diff_mean,
            "score_diff_max": score_diff_max,
        },
        "latency": {
            "primary_ms": primary_ms,
            "shadow_ms": shadow_ms,
        },
        "only_primary": [],
        "only_shadow": [],
    }
    
    return result


# =============================================================================
# parse_query_result 测试
# =============================================================================

class TestParseQueryResult:
    """测试 parse_query_result 函数"""
    
    def test_parse_compare_report_metrics(self):
        """解析 compare_report.metrics 格式"""
        data = make_compare_report_query_result(
            hit_overlap_ratio=0.85,
            avg_score_diff=0.03,
            max_score_diff=0.08,
            p95_score_diff=0.06,
            avg_rank_drift=1.2,
            latency_ratio=1.5,
        )
        
        sample = parse_query_result(data)
        
        assert sample is not None
        assert sample.source == "compare_report"
        assert sample.hit_overlap_ratio == 0.85
        assert sample.avg_score_diff == 0.03
        assert sample.max_score_diff == 0.08
        assert sample.p95_score_diff == 0.06
        assert sample.avg_rank_drift == 1.2
        assert sample.latency_ratio == 1.5
    
    def test_parse_compare_report_with_rbo(self):
        """解析带 RBO 的 compare_report（detailed 模式）"""
        data = make_compare_report_query_result(
            hit_overlap_ratio=0.9,
            rbo=0.85,
        )
        
        sample = parse_query_result(data)
        
        assert sample is not None
        assert sample.rbo == 0.85
    
    def test_parse_dual_read_metrics(self):
        """解析 dual_read.metrics 格式"""
        data = make_dual_read_query_result(
            overlap_ratio=0.75,
            score_diff_mean=0.04,
            score_diff_max=0.12,
            primary_ms=10.0,
            shadow_ms=15.0,
        )
        
        sample = parse_query_result(data)
        
        assert sample is not None
        assert sample.source == "dual_read"
        assert sample.hit_overlap_ratio == 0.75
        assert sample.avg_score_diff == 0.04
        assert sample.max_score_diff == 0.12
        # 延迟比率应自动计算
        assert sample.latency_ratio == pytest.approx(1.5)
    
    def test_parse_compare_report_priority(self):
        """compare_report 优先于 dual_read"""
        data = make_compare_report_query_result(hit_overlap_ratio=0.9)
        # 添加 dual_read 数据
        data["dual_read"] = {
            "metrics": {"overlap_ratio": 0.5},
        }
        
        sample = parse_query_result(data)
        
        assert sample is not None
        assert sample.source == "compare_report"
        assert sample.hit_overlap_ratio == 0.9  # 使用 compare_report 的值
    
    def test_parse_error_result_returns_none(self):
        """错误结果返回 None"""
        data = make_compare_report_query_result(error="Connection timeout")
        
        sample = parse_query_result(data)
        
        # 有错误但无 metrics 的结果应返回 None
        assert sample is None
    
    def test_parse_empty_result_returns_none(self):
        """无 metrics 的结果返回 None"""
        data = {"query": "test", "success": True, "evidences": []}
        
        sample = parse_query_result(data)
        
        assert sample is None
    
    def test_parse_partial_metrics(self):
        """部分 metrics 字段"""
        data = {
            "query": "test",
            "compare_report": {
                "metrics": {
                    "hit_overlap_ratio": 0.8,
                    # 缺少其他字段
                },
            },
        }
        
        sample = parse_query_result(data)
        
        assert sample is not None
        assert sample.hit_overlap_ratio == 0.8
        assert sample.avg_score_diff is None
        assert sample.rbo is None


# =============================================================================
# parse_results_file 测试
# =============================================================================

class TestParseResultsFile:
    """测试 parse_results_file 函数"""
    
    def test_parse_array_format(self, tmp_path):
        """解析数组格式 [QueryResult, ...]"""
        data = [
            make_compare_report_query_result(),
            make_compare_report_query_result(),
        ]
        
        file_path = tmp_path / "results.json"
        file_path.write_text(json.dumps(data))
        
        results, errors = parse_results_file(str(file_path))
        
        assert len(results) == 2
        assert errors == 0
    
    def test_parse_wrapper_format(self, tmp_path):
        """解析 {"results": [...]} 格式"""
        data = {
            "results": [
                make_compare_report_query_result(),
                make_compare_report_query_result(),
            ]
        }
        
        file_path = tmp_path / "results.json"
        file_path.write_text(json.dumps(data))
        
        results, errors = parse_results_file(str(file_path))
        
        assert len(results) == 2
        assert errors == 0
    
    def test_parse_batch_output_format(self, tmp_path):
        """解析 batch 输出格式 {"success": bool, "results": [...], "aggregate_gate": {...}}"""
        data = {
            "success": True,
            "total_queries": 3,
            "aggregate_gate": {"passed": True},
            "results": [
                make_compare_report_query_result(),
                make_compare_report_query_result(),
                make_compare_report_query_result(error="timeout"),
            ],
        }
        
        file_path = tmp_path / "batch_results.json"
        file_path.write_text(json.dumps(data))
        
        results, errors = parse_results_file(str(file_path))
        
        assert len(results) == 3
        assert errors == 1  # 一个有错误
    
    def test_parse_single_result(self, tmp_path):
        """解析单个 QueryResult"""
        data = make_compare_report_query_result()
        
        file_path = tmp_path / "single.json"
        file_path.write_text(json.dumps(data))
        
        results, errors = parse_results_file(str(file_path))
        
        assert len(results) == 1
        assert errors == 0
    
    def test_count_errors(self, tmp_path):
        """正确统计错误数量"""
        data = [
            make_compare_report_query_result(),
            make_compare_report_query_result(error="error1"),
            make_compare_report_query_result(),
            make_compare_report_query_result(error="error2"),
        ]
        
        file_path = tmp_path / "with_errors.json"
        file_path.write_text(json.dumps(data))
        
        results, errors = parse_results_file(str(file_path))
        
        assert len(results) == 4
        assert errors == 2


# =============================================================================
# extract_samples 测试
# =============================================================================

class TestExtractSamples:
    """测试 extract_samples 函数"""
    
    def test_extract_valid_samples(self):
        """提取有效样本"""
        results = [
            make_compare_report_query_result(hit_overlap_ratio=0.9),
            make_compare_report_query_result(hit_overlap_ratio=0.8),
            make_dual_read_query_result(overlap_ratio=0.7),
        ]
        
        samples = extract_samples(results)
        
        assert len(samples) == 3
        assert samples[0].hit_overlap_ratio == 0.9
        assert samples[1].hit_overlap_ratio == 0.8
        assert samples[2].hit_overlap_ratio == 0.7
    
    def test_skip_invalid_results(self):
        """跳过无效结果"""
        results = [
            make_compare_report_query_result(hit_overlap_ratio=0.9),
            {"query": "no metrics"},  # 无 metrics
            make_compare_report_query_result(error="failed"),  # 有错误但无 metrics
            make_compare_report_query_result(hit_overlap_ratio=0.8),
        ]
        
        samples = extract_samples(results)
        
        assert len(samples) == 2
    
    def test_empty_results(self):
        """空结果列表"""
        samples = extract_samples([])
        
        assert len(samples) == 0


# =============================================================================
# 统计计算测试
# =============================================================================

class TestComputePercentile:
    """测试 compute_percentile 函数"""
    
    def test_p50_is_median(self):
        """P50 应该是中位数"""
        values = [1, 2, 3, 4, 5]
        
        p50 = compute_percentile(values, 50)
        
        assert p50 == 3.0
    
    def test_p95_basic(self):
        """基本 P95 计算"""
        values = list(range(1, 101))  # 1-100
        
        p95 = compute_percentile(values, 95)
        
        assert 94 <= p95 <= 96
    
    def test_empty_list(self):
        """空列表返回 0"""
        assert compute_percentile([], 50) == 0.0
    
    def test_single_value(self):
        """单值列表"""
        assert compute_percentile([5.0], 50) == 5.0
        assert compute_percentile([5.0], 95) == 5.0


class TestComputeDistribution:
    """测试 compute_distribution 函数"""
    
    def test_basic_distribution(self):
        """基本分布计算"""
        values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        
        dist = compute_distribution("test_metric", values)
        
        assert dist.name == "test_metric"
        assert dist.count == 10
        assert dist.min_val == 0.1
        assert dist.max_val == 1.0
        assert dist.mean == pytest.approx(0.55)
        assert 0.4 <= dist.p50 <= 0.6
    
    def test_empty_values(self):
        """空值列表"""
        dist = compute_distribution("empty", [])
        
        assert dist.count == 0
        assert dist.min_val == 0.0
        assert dist.max_val == 0.0
    
    def test_single_value(self):
        """单值分布"""
        dist = compute_distribution("single", [0.5])
        
        assert dist.count == 1
        assert dist.min_val == 0.5
        assert dist.max_val == 0.5
        assert dist.p50 == 0.5
        assert dist.std == 0.0  # 单值标准差为 0


class TestComputeAllDistributions:
    """测试 compute_all_distributions 函数"""
    
    def test_compute_all(self):
        """计算所有指标分布"""
        samples = [
            MetricsSample(
                hit_overlap_ratio=0.9,
                avg_score_diff=0.05,
                p95_score_diff=0.08,
                latency_ratio=1.2,
                source="compare_report",
            ),
            MetricsSample(
                hit_overlap_ratio=0.8,
                avg_score_diff=0.06,
                p95_score_diff=0.09,
                latency_ratio=1.3,
                source="compare_report",
            ),
            MetricsSample(
                hit_overlap_ratio=0.85,
                avg_score_diff=0.04,
                p95_score_diff=0.07,
                latency_ratio=1.1,
                source="compare_report",
            ),
        ]
        
        distributions = compute_all_distributions(samples)
        
        assert "hit_overlap_ratio" in distributions
        assert "avg_score_diff" in distributions
        assert "p95_score_diff" in distributions
        assert "latency_ratio" in distributions
        
        # 验证值
        assert distributions["hit_overlap_ratio"].count == 3
        assert distributions["hit_overlap_ratio"].min_val == 0.8
        assert distributions["hit_overlap_ratio"].max_val == 0.9
    
    def test_partial_samples(self):
        """部分字段的样本"""
        samples = [
            MetricsSample(hit_overlap_ratio=0.9, source="a"),
            MetricsSample(hit_overlap_ratio=0.8, rbo=0.85, source="b"),
            MetricsSample(rbo=0.9, source="c"),
        ]
        
        distributions = compute_all_distributions(samples)
        
        assert distributions["hit_overlap_ratio"].count == 2
        assert distributions["rbo"].count == 2


# =============================================================================
# 阈值推荐测试
# =============================================================================

class TestGenerateRecommendations:
    """测试 generate_recommendations 函数"""
    
    def test_overlap_recommendations(self):
        """生成重叠率阈值推荐"""
        distributions = {
            "hit_overlap_ratio": MetricsDistribution(
                name="hit_overlap_ratio",
                count=100,
                min_val=0.7,
                max_val=1.0,
                p50=0.9,
                p90=0.85,
                p95=0.82,
                p99=0.75,
            ),
        }
        
        recommendations = generate_recommendations(distributions)
        
        assert "STEP3_DUAL_READ_OVERLAP_MIN_WARN" in recommendations
        assert "STEP3_DUAL_READ_OVERLAP_MIN_FAIL" in recommendations
        
        warn = recommendations["STEP3_DUAL_READ_OVERLAP_MIN_WARN"]
        fail = recommendations["STEP3_DUAL_READ_OVERLAP_MIN_FAIL"]
        
        assert warn.level == "warn"
        assert fail.level == "fail"
        assert warn.value > fail.value  # warn 应该比 fail 更严格
    
    def test_score_drift_recommendations(self):
        """生成分数漂移阈值推荐"""
        distributions = {
            "p95_score_diff": MetricsDistribution(
                name="p95_score_diff",
                count=100,
                min_val=0.01,
                max_val=0.15,
                p50=0.05,
                p90=0.08,
                p95=0.1,
                p99=0.12,
            ),
        }
        
        recommendations = generate_recommendations(distributions)
        
        assert "STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX" in recommendations
        
        rec = recommendations["STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"]
        assert rec.level == "fail"
        # 值应该是 P95 * margin_factor
        assert rec.value >= 0.1
    
    def test_rbo_recommendations(self):
        """生成 RBO 阈值推荐"""
        distributions = {
            "rbo": MetricsDistribution(
                name="rbo",
                count=100,
                min_val=0.7,
                max_val=0.99,
                p50=0.9,
                p90=0.85,
                p95=0.8,
                p99=0.75,
            ),
        }
        
        recommendations = generate_recommendations(distributions)
        
        assert "STEP3_DUAL_READ_RBO_MIN_WARN" in recommendations
        assert "STEP3_DUAL_READ_RBO_MIN_FAIL" in recommendations
    
    def test_rank_drift_recommendations(self):
        """生成排名漂移阈值推荐"""
        distributions = {
            "avg_rank_drift": MetricsDistribution(
                name="avg_rank_drift",
                count=100,
                min_val=0.0,
                max_val=5.0,
                p50=1.0,
                p90=2.5,
                p95=3.5,
                p99=4.5,
            ),
        }
        
        recommendations = generate_recommendations(distributions)
        
        assert "STEP3_DUAL_READ_RANK_P95_MAX_WARN" in recommendations
        assert "STEP3_DUAL_READ_RANK_P95_MAX_FAIL" in recommendations
        
        warn = recommendations["STEP3_DUAL_READ_RANK_P95_MAX_WARN"]
        fail = recommendations["STEP3_DUAL_READ_RANK_P95_MAX_FAIL"]
        
        assert isinstance(warn.value, int)
        assert isinstance(fail.value, int)
        assert fail.value >= warn.value
    
    def test_latency_ratio_recommendations(self):
        """生成延迟比率阈值推荐"""
        distributions = {
            "latency_ratio": MetricsDistribution(
                name="latency_ratio",
                count=100,
                min_val=0.8,
                max_val=3.0,
                p50=1.2,
                p90=1.8,
                p95=2.0,
                p99=2.5,
            ),
        }
        
        recommendations = generate_recommendations(distributions)
        
        assert "STEP3_DUAL_READ_LATENCY_RATIO_MAX" in recommendations
    
    def test_custom_margin_factor(self):
        """自定义安全边际系数"""
        distributions = {
            "p95_score_diff": MetricsDistribution(
                name="p95_score_diff",
                count=100,
                p95=0.1,
            ),
        }
        
        rec_default = generate_recommendations(distributions, margin_factor=1.0)
        rec_higher = generate_recommendations(distributions, margin_factor=1.5)
        
        # 更高的 margin_factor 应产生更宽松的阈值
        default_val = rec_default["STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"].value
        higher_val = rec_higher["STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"].value
        
        assert higher_val >= default_val
    
    def test_empty_distributions(self):
        """空分布"""
        recommendations = generate_recommendations({})
        
        assert len(recommendations) == 0


# =============================================================================
# 完整校准流程测试
# =============================================================================

class TestCalibrate:
    """测试 calibrate 函数"""
    
    def test_calibrate_single_file(self, tmp_path):
        """单文件校准"""
        data = [
            make_compare_report_query_result(
                hit_overlap_ratio=0.9,
                p95_score_diff=0.05,
                rbo=0.95,
            ),
            make_compare_report_query_result(
                hit_overlap_ratio=0.85,
                p95_score_diff=0.06,
                rbo=0.9,
            ),
            make_compare_report_query_result(
                hit_overlap_ratio=0.8,
                p95_score_diff=0.07,
                rbo=0.85,
            ),
        ]
        
        file_path = tmp_path / "results.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)])
        
        assert result.total_queries == 3
        assert result.error_count == 0
        assert result.metrics_available == 3
        assert "hit_overlap_ratio" in result.distributions
        assert len(result.recommendations) > 0
    
    def test_calibrate_multiple_files(self, tmp_path):
        """多文件校准"""
        data1 = [make_compare_report_query_result(hit_overlap_ratio=0.9)]
        data2 = [make_compare_report_query_result(hit_overlap_ratio=0.8)]
        
        file1 = tmp_path / "results1.json"
        file2 = tmp_path / "results2.json"
        file1.write_text(json.dumps(data1))
        file2.write_text(json.dumps(data2))
        
        result = calibrate([str(file1), str(file2)])
        
        assert result.total_queries == 2
        assert result.metrics_available == 2
    
    def test_calibrate_with_errors(self, tmp_path):
        """包含错误的校准"""
        data = [
            make_compare_report_query_result(hit_overlap_ratio=0.9),
            make_compare_report_query_result(error="timeout"),
            make_compare_report_query_result(hit_overlap_ratio=0.8),
        ]
        
        file_path = tmp_path / "results.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)])
        
        assert result.total_queries == 3
        assert result.error_count == 1
        assert result.metrics_available == 2
    
    def test_calibrate_include_samples(self, tmp_path):
        """包含原始样本"""
        data = [make_compare_report_query_result(hit_overlap_ratio=0.9)]
        
        file_path = tmp_path / "results.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)], include_samples=True)
        
        assert len(result.samples) == 1


# =============================================================================
# CalibrationResult 测试
# =============================================================================

class TestCalibrationResult:
    """测试 CalibrationResult 数据结构"""
    
    def test_to_dict(self):
        """测试序列化"""
        result = CalibrationResult(
            total_queries=10,
            error_count=1,
            metrics_available=9,
            timestamp="2024-01-01T00:00:00Z",
            distributions={
                "hit_overlap_ratio": MetricsDistribution(
                    name="hit_overlap_ratio",
                    count=9,
                    min_val=0.7,
                    max_val=1.0,
                    p50=0.85,
                    p90=0.8,
                    p95=0.75,
                    p99=0.72,
                ),
            },
            recommendations={
                "STEP3_DUAL_READ_OVERLAP_MIN_WARN": ThresholdRecommendation(
                    env_var="STEP3_DUAL_READ_OVERLAP_MIN_WARN",
                    value=0.7,
                    level="warn",
                    reason="test",
                ),
            },
        )
        
        d = result.to_dict()
        
        assert d["summary"]["total_queries"] == 10
        assert d["summary"]["error_count"] == 1
        assert "hit_overlap_ratio" in d["distributions"]
        assert "STEP3_DUAL_READ_OVERLAP_MIN_WARN" in d["recommendations"]
        assert "env_export" in d
    
    def test_env_export_generation(self):
        """测试环境变量导出命令生成"""
        result = CalibrationResult(
            recommendations={
                "VAR1": ThresholdRecommendation(env_var="VAR1", value=0.5),
                "VAR2": ThresholdRecommendation(env_var="VAR2", value=3),
            },
        )
        
        export_cmd = result._generate_env_export()
        
        assert "export VAR1=" in export_cmd
        assert "export VAR2=" in export_cmd
        assert "&&" in export_cmd


# =============================================================================
# format_output 测试
# =============================================================================

class TestFormatOutput:
    """测试 format_output 函数"""
    
    def test_json_format(self):
        """JSON 格式输出"""
        result = CalibrationResult(total_queries=5, metrics_available=5)
        
        output = format_output(result, "json", include_samples=False)
        
        parsed = json.loads(output)
        assert parsed["summary"]["total_queries"] == 5
    
    def test_env_format(self):
        """环境变量格式输出"""
        result = CalibrationResult(
            recommendations={
                "TEST_VAR": ThresholdRecommendation(env_var="TEST_VAR", value=0.5),
            },
        )
        
        output = format_output(result, "env", include_samples=False)
        
        assert "export TEST_VAR=0.5" in output
    
    def test_summary_format(self):
        """摘要格式输出"""
        result = CalibrationResult(
            total_queries=10,
            metrics_available=9,
            distributions={
                "hit_overlap_ratio": MetricsDistribution(
                    name="hit_overlap_ratio",
                    count=9,
                    min_val=0.7,
                    max_val=1.0,
                    p50=0.85,
                    p90=0.8,
                    p95=0.75,
                    p99=0.72,
                ),
            },
        )
        
        output = format_output(result, "summary", include_samples=False)
        
        assert "双读阈值校准结果摘要" in output
        assert "总查询数: 10" in output
        assert "hit_overlap_ratio" in output


# =============================================================================
# 边界条件测试
# =============================================================================

class TestEdgeCases:
    """测试边界条件"""
    
    def test_all_errors(self, tmp_path):
        """所有结果都有错误"""
        data = [
            make_compare_report_query_result(error="error1"),
            make_compare_report_query_result(error="error2"),
        ]
        
        file_path = tmp_path / "all_errors.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)])
        
        assert result.total_queries == 2
        assert result.error_count == 2
        assert result.metrics_available == 0
        assert len(result.distributions) == 0
    
    def test_no_common_metrics(self, tmp_path):
        """样本没有共同指标"""
        data = [
            {"query": "q1", "compare_report": {"metrics": {"hit_overlap_ratio": 0.9}}},
            {"query": "q2", "dual_read": {"metrics": {"overlap_ratio": 0.8}, "latency": {}}},
        ]
        
        file_path = tmp_path / "no_common.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)])
        
        assert result.metrics_available == 2
        # 两个样本都有 overlap 相关字段
        assert "hit_overlap_ratio" in result.distributions
    
    def test_single_sample(self, tmp_path):
        """单个样本"""
        data = [make_compare_report_query_result(hit_overlap_ratio=0.9)]
        
        file_path = tmp_path / "single.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)])
        
        assert result.metrics_available == 1
        # 单样本分布的所有百分位都应该相等
        dist = result.distributions.get("hit_overlap_ratio")
        if dist:
            assert dist.min_val == dist.max_val == dist.p50 == dist.p95
    
    def test_extreme_values(self, tmp_path):
        """极端值处理"""
        data = [
            make_compare_report_query_result(hit_overlap_ratio=0.0),
            make_compare_report_query_result(hit_overlap_ratio=1.0),
        ]
        
        file_path = tmp_path / "extreme.json"
        file_path.write_text(json.dumps(data))
        
        result = calibrate([str(file_path)])
        
        dist = result.distributions.get("hit_overlap_ratio")
        assert dist.min_val == 0.0
        assert dist.max_val == 1.0
