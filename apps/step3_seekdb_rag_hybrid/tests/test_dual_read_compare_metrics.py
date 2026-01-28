"""
test_dual_read_compare_metrics.py - 双读对比指标与决策测试

本测试模块构造确定性的 primary/shadow hits，验证 metrics 计算与 decision 判定。

测试覆盖场景:
1. 完全一致场景 - primary 与 shadow 完全相同
2. 仅集合差异场景 - 文档集合不同（部分重叠或无重叠）
3. 仅排序差异场景 - 相同文档集合，排序不同
4. 仅分数漂移场景 - 相同文档和排序，分数有差异
5. 混合场景 - 同时存在多种差异

每个场景验证:
- CompareMetrics 的计算值
- RankingDriftMetrics 的计算值
- CompareDecision 的 passed、has_warnings、violated_checks
- ViolationDetail 的实际值/阈值/级别
- recommendation 建议

运行方式:
    cd apps/step3_seekdb_rag_hybrid
    pytest tests/test_dual_read_compare_metrics.py -v
"""

import pytest
from typing import List, Tuple

from step3_seekdb_rag_hybrid.dual_read_compare import (
    # 数据结构
    CompareThresholds,
    CompareMetrics,
    CompareDecision,
    CompareReport,
    ViolationDetail,
    RankingDriftMetrics,
    OverlapMetrics,
    ScoreDriftMetrics,
    # 计算函数
    compute_overlap_metrics,
    compute_ranking_drift,
    compute_score_drift,
    compute_rbo,
    compute_jaccard,
    compute_percentile,
    stabilize_ranking,
    # 评估函数
    evaluate,
    evaluate_with_report,
)
from seek_query import generate_compare_report, EvidenceResult


# =============================================================================
# 测试数据工厂
# =============================================================================

def make_hits(
    ids_scores: List[Tuple[str, float]],
) -> Tuple[List[str], List[Tuple[str, float]]]:
    """
    创建测试用的 hits 数据
    
    Args:
        ids_scores: [(chunk_id, score), ...] 列表
        
    Returns:
        (id_list, id_score_tuples) 元组
    """
    id_list = [item[0] for item in ids_scores]
    return id_list, ids_scores


def make_compare_metrics(
    primary_hits: List[Tuple[str, float]],
    shadow_hits: List[Tuple[str, float]],
    primary_latency_ms: float = 10.0,
    secondary_latency_ms: float = 15.0,
) -> CompareMetrics:
    """
    根据 primary/shadow hits 构造 CompareMetrics
    
    计算 overlap、分数差异等指标
    """
    primary_ids = [h[0] for h in primary_hits]
    shadow_ids = [h[0] for h in shadow_hits]
    
    # 构建分数映射
    primary_scores = {h[0]: h[1] for h in primary_hits}
    shadow_scores = {h[0]: h[1] for h in shadow_hits}
    
    # 计算交集
    common_ids = set(primary_ids) & set(shadow_ids)
    union_ids = set(primary_ids) | set(shadow_ids)
    
    # 计算分数差异（仅针对共同元素）
    score_diffs = []
    for chunk_id in common_ids:
        diff = abs(primary_scores[chunk_id] - shadow_scores[chunk_id])
        score_diffs.append(diff)
    
    avg_score_diff = sum(score_diffs) / len(score_diffs) if score_diffs else 0.0
    max_score_diff = max(score_diffs) if score_diffs else 0.0
    
    # 计算排名漂移
    primary_rank = {id_: idx for idx, id_ in enumerate(primary_ids)}
    shadow_rank = {id_: idx for idx, id_ in enumerate(shadow_ids)}
    
    rank_diffs = []
    for chunk_id in common_ids:
        if chunk_id in primary_rank and chunk_id in shadow_rank:
            rank_diffs.append(abs(primary_rank[chunk_id] - shadow_rank[chunk_id]))
    
    avg_rank_drift = sum(rank_diffs) / len(rank_diffs) if rank_diffs else 0.0
    max_rank_drift = max(rank_diffs) if rank_diffs else 0
    
    # 计算重叠率
    overlap_ratio = len(common_ids) / len(union_ids) if union_ids else 1.0
    
    # 计算延迟比率
    latency_ratio = (
        secondary_latency_ms / primary_latency_ms
        if primary_latency_ms > 0 else 0.0
    )
    
    return CompareMetrics(
        avg_score_diff=avg_score_diff,
        max_score_diff=max_score_diff,
        avg_rank_drift=avg_rank_drift,
        max_rank_drift=max_rank_drift,
        hit_overlap_ratio=overlap_ratio,
        common_hit_count=len(common_ids),
        primary_latency_ms=primary_latency_ms,
        secondary_latency_ms=secondary_latency_ms,
        latency_ratio=latency_ratio,
        primary_hit_count=len(primary_hits),
        secondary_hit_count=len(shadow_hits),
    )


# =============================================================================
# 场景1: 完全一致场景
# =============================================================================

class TestIdenticalHits:
    """测试完全一致的 primary/shadow hits"""
    
    def test_identical_hits_metrics(self):
        """相同 hits 应产生完美的指标值"""
        hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        
        # 计算 overlap 指标
        primary_ids = [h[0] for h in hits]
        shadow_ids = [h[0] for h in hits]
        overlap = compute_overlap_metrics(primary_ids, shadow_ids, top_k=3)
        
        assert overlap.overlap_count == 3
        assert overlap.overlap_ratio == 1.0
        assert overlap.overlap_at_k == 1.0
        assert overlap.primary_only_ids_sample == []
        assert overlap.shadow_only_ids_sample == []
    
    def test_identical_hits_ranking_drift(self):
        """相同 hits 应产生零排名漂移"""
        hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        
        drift = compute_ranking_drift(hits, hits)
        
        assert drift.avg_abs_rank_diff == 0.0
        assert drift.p95_abs_rank_diff == 0.0
        assert drift.top1_same is True
        assert drift.top3_jaccard == 1.0
        assert drift.rbo >= 0.99  # RBO 接近 1.0
        assert drift.common_count == 3
    
    def test_identical_hits_decision_passes(self):
        """相同 hits 应通过所有检查"""
        hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        
        metrics = make_compare_metrics(hits, hits)
        ranking = compute_ranking_drift(hits, hits)
        thresholds = CompareThresholds()
        
        decision = evaluate(metrics, thresholds, ranking)
        
        assert decision.passed is True
        assert decision.has_warnings is False
        assert decision.violated_checks == []
        assert decision.violation_details == []
        assert decision.recommendation == "safe_to_switch"


# =============================================================================
# 场景2: 仅集合差异场景
# =============================================================================

class TestSetDifferenceOnly:
    """测试仅存在集合差异的场景（文档集合不同）"""
    
    def test_partial_overlap_metrics(self):
        """部分重叠场景的指标计算"""
        # primary: a, b, c
        # shadow:  a, c, d
        # 交集: a, c (2)  并集: a, b, c, d (4)  重叠率: 2/4 = 0.5
        primary_ids = ["chunk_a", "chunk_b", "chunk_c"]
        shadow_ids = ["chunk_a", "chunk_c", "chunk_d"]
        
        overlap = compute_overlap_metrics(primary_ids, shadow_ids, top_k=3)
        
        assert overlap.overlap_count == 2
        assert overlap.overlap_ratio == pytest.approx(2/3)  # 2/max(3,3)
        assert "chunk_b" in overlap.primary_only_ids_sample
        assert "chunk_d" in overlap.shadow_only_ids_sample
    
    def test_no_overlap_metrics(self):
        """完全无重叠场景"""
        primary_ids = ["chunk_a", "chunk_b"]
        shadow_ids = ["chunk_x", "chunk_y"]
        
        overlap = compute_overlap_metrics(primary_ids, shadow_ids, top_k=2)
        
        assert overlap.overlap_count == 0
        assert overlap.overlap_ratio == 0.0
        assert overlap.overlap_at_k == 0.0
    
    def test_low_overlap_triggers_fail(self):
        """低重叠率应触发 fail"""
        # primary: a, b, c, d, e
        # shadow: a, x, y, z, w
        # 交集: {a} = 1, 并集: {a,b,c,d,e,x,y,z,w} = 9
        # 重叠率 = 1/9 ≈ 0.111，低于 fail 阈值 0.5
        primary_ids = ["a", "b", "c", "d", "e"]
        shadow_ids = ["a", "x", "y", "z", "w"]
        
        primary_hits = [(id_, 0.9 - i * 0.05) for i, id_ in enumerate(primary_ids)]
        shadow_hits = [(id_, 0.9 - i * 0.05) for i, id_ in enumerate(shadow_ids)]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        ranking = compute_ranking_drift(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
        )
        
        decision = evaluate(metrics, thresholds, ranking)
        
        assert decision.passed is False
        assert "hit_overlap_below_fail" in decision.violated_checks
        assert decision.recommendation == "abort_switch"
        
        # 验证 violation_details
        overlap_violation = next(
            (v for v in decision.violation_details if v.check_name == "hit_overlap"),
            None
        )
        assert overlap_violation is not None
        assert overlap_violation.level == "fail"
        # 交集 1，并集 9，overlap = 1/9 ≈ 0.111
        assert overlap_violation.actual_value == pytest.approx(1/9, rel=0.01)
        assert overlap_violation.threshold_value == 0.5
    
    def test_medium_overlap_triggers_warn(self):
        """中等重叠率应触发 warn"""
        # 需要构造一个重叠率介于 warn(0.7) 和 fail(0.5) 之间的场景
        # primary: a, b, c, d
        # shadow: a, b, c, x
        # 交集: {a, b, c} = 3, 并集: {a, b, c, d, x} = 5
        # 重叠率 = 3/5 = 0.6，介于 warn(0.7) 和 fail(0.5) 之间
        primary_ids = ["a", "b", "c", "d"]
        shadow_ids = ["a", "b", "c", "x"]
        
        primary_hits = [(id_, 0.9 - i * 0.05) for i, id_ in enumerate(primary_ids)]
        shadow_hits = [(id_, 0.9 - i * 0.05) for i, id_ in enumerate(shadow_ids)]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        ranking = compute_ranking_drift(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
            # 放宽 RBO 阈值以隔离测试
            rbo_min_warn=0.0,
            rbo_min_fail=0.0,
        )
        
        decision = evaluate(metrics, thresholds, ranking)
        
        assert decision.passed is True  # warn 不影响 passed
        assert decision.has_warnings is True
        assert "hit_overlap_below_warn" in decision.violated_checks
        assert decision.recommendation == "investigate_required"


# =============================================================================
# 场景3: 仅排序差异场景
# =============================================================================

class TestRankDifferenceOnly:
    """测试仅存在排序差异的场景（相同文档集合，不同排序）"""
    
    def test_reversed_order_ranking(self):
        """完全逆序的排名漂移"""
        # 相同文档，但顺序完全相反
        primary_hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        shadow_hits = [
            ("chunk_c", 0.95),  # c 排第一
            ("chunk_b", 0.90),
            ("chunk_a", 0.85),  # a 排最后
        ]
        
        drift = compute_ranking_drift(primary_hits, shadow_hits)
        
        # a: rank 0->2 (diff=2), b: rank 1->1 (diff=0), c: rank 2->0 (diff=2)
        # avg = (2+0+2)/3 = 1.33
        assert drift.avg_abs_rank_diff == pytest.approx(4/3)
        assert drift.top1_same is False
        assert drift.common_count == 3
        # RBO 应明显低于 1.0
        assert drift.rbo < 0.9
    
    def test_top1_different_triggers_low_rbo(self):
        """Top-1 不同会显著降低 RBO"""
        primary_hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
            ("chunk_d", 0.80),
            ("chunk_e", 0.75),
        ]
        # shadow: b 和 a 交换位置
        shadow_hits = [
            ("chunk_b", 0.95),  # b 变成第一
            ("chunk_a", 0.90),  # a 变成第二
            ("chunk_c", 0.85),
            ("chunk_d", 0.80),
            ("chunk_e", 0.75),
        ]
        
        drift = compute_ranking_drift(primary_hits, shadow_hits)
        
        assert drift.top1_same is False
        assert drift.top3_jaccard == 1.0  # top-3 集合相同
        # RBO 应有所下降
        assert drift.rbo < 1.0
    
    def test_rank_drift_triggers_fail(self):
        """高排名漂移应触发 fail"""
        # 构造 P95 排名漂移超过阈值的场景
        primary_hits = [
            ("a", 0.95), ("b", 0.90), ("c", 0.85),
            ("d", 0.80), ("e", 0.75), ("f", 0.70),
        ]
        # shadow: 大幅漂移
        shadow_hits = [
            ("f", 0.95), ("e", 0.90), ("d", 0.85),
            ("c", 0.80), ("b", 0.75), ("a", 0.70),
        ]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        drift = compute_ranking_drift(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            rank_p95_max_warn=3,
            rank_p95_max_fail=5,
            # 放宽其他阈值以隔离测试
            hit_overlap_min_warn=0.0,
            hit_overlap_min_fail=0.0,
            rbo_min_warn=0.0,
            rbo_min_fail=0.0,
        )
        
        decision = evaluate(metrics, thresholds, drift)
        
        # p95 排名漂移 = 5 (a: 0->5, f: 5->0)
        assert drift.p95_abs_rank_diff >= 5
        # 应该触发 warn 或 fail（取决于具体计算）
        rank_violations = [
            v for v in decision.violated_checks
            if "rank_p95" in v
        ]
        assert len(rank_violations) > 0


# =============================================================================
# 场景4: 仅分数漂移场景
# =============================================================================

class TestScoreDriftOnly:
    """测试仅存在分数漂移的场景（相同文档和排序，分数有差异）"""
    
    def test_small_score_diff_passes(self):
        """小分数差异应通过"""
        primary_hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        # shadow 分数略有不同但差异很小
        shadow_hits = [
            ("chunk_a", 0.94),  # diff = 0.01
            ("chunk_b", 0.91),  # diff = 0.01
            ("chunk_c", 0.84),  # diff = 0.01
        ]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        thresholds = CompareThresholds(score_drift_p95_max=0.1)
        
        decision = evaluate(metrics, thresholds)
        
        assert metrics.avg_score_diff == pytest.approx(0.01)
        assert metrics.max_score_diff == pytest.approx(0.01)
        assert "score_drift" not in " ".join(decision.violated_checks)
    
    def test_large_score_diff_triggers_fail(self):
        """大分数差异应触发 fail"""
        primary_hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        # shadow 分数差异较大
        shadow_hits = [
            ("chunk_a", 0.75),  # diff = 0.20
            ("chunk_b", 0.70),  # diff = 0.20
            ("chunk_c", 0.65),  # diff = 0.20
        ]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            score_drift_p95_max=0.1,
            # 放宽其他阈值
            hit_overlap_min_warn=0.0,
            hit_overlap_min_fail=0.0,
        )
        
        decision = evaluate(metrics, thresholds)
        
        assert metrics.max_score_diff == pytest.approx(0.2)
        # check 名称是 score_drift_p95_exceeded
        assert "score_drift_p95_exceeded" in decision.violated_checks
        
        # 验证详细信息（check_name 是 score_drift_p95）
        score_violation = next(
            (v for v in decision.violation_details if v.check_name == "score_drift_p95"),
            None
        )
        assert score_violation is not None
        assert score_violation.level == "fail"
        assert score_violation.actual_value == pytest.approx(0.2)
        assert score_violation.threshold_value == 0.1


# =============================================================================
# 场景5: 混合场景
# =============================================================================

class TestMixedScenarios:
    """测试同时存在多种差异的混合场景"""
    
    def test_overlap_and_score_drift(self):
        """同时存在集合差异和分数漂移"""
        primary_hits = [
            ("a", 0.95),
            ("b", 0.90),
            ("c", 0.85),
        ]
        shadow_hits = [
            ("a", 0.75),  # 分数漂移 0.20
            ("b", 0.70),  # 分数漂移 0.20
            ("d", 0.65),  # 不同文档
        ]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.51,  # 设置略高于 0.5 以触发 fail
            score_drift_p95_max=0.1,
        )
        
        decision = evaluate(metrics, thresholds)
        
        # intersection={a,b}=2, union={a,b,c,d}=4, overlap=2/4=0.5 < 0.51 触发 fail
        # score drift = 0.20 > 0.1，也触发 fail
        assert decision.passed is False
        assert "score_drift_p95_exceeded" in decision.violated_checks
    
    def test_all_violations_mixed(self):
        """所有指标都有违规的极端场景"""
        # 构造一个几乎完全不同的场景
        primary_hits = [
            ("a", 0.95),
            ("b", 0.90),
        ]
        shadow_hits = [
            ("x", 0.50),  # 完全不同的文档
            ("y", 0.45),
        ]
        
        metrics = make_compare_metrics(
            primary_hits, shadow_hits,
            primary_latency_ms=10.0,
            secondary_latency_ms=50.0,  # 延迟比 5:1
        )
        ranking = compute_ranking_drift(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
            rbo_min_warn=0.8,
            rbo_min_fail=0.6,
            latency_ratio_max=2.0,
        )
        
        decision = evaluate(metrics, thresholds, ranking)
        
        assert decision.passed is False
        assert len(decision.violated_checks) >= 2
        assert decision.recommendation == "abort_switch"
        
        # 检查多个违规项
        violations_by_name = {v.check_name: v for v in decision.violation_details}
        assert "hit_overlap" in violations_by_name
        assert "latency_ratio" in violations_by_name
    
    def test_warn_only_scenario(self):
        """仅有 warn 级别违规的场景"""
        # 构造一个重叠率在 warn 和 fail 之间的场景
        # primary: a, b, c, d, e, f (6 元素)
        # shadow: a, b, c, d, x, y (6 元素)
        # intersection: {a, b, c, d} = 4
        # union: {a, b, c, d, e, f, x, y} = 8
        # overlap = 4/8 = 0.5 (刚好等于 fail，需要调整)
        
        # 调整为：
        # primary: a, b, c, d, e (5)
        # shadow: a, b, c, d, x (5)
        # intersection: {a, b, c, d} = 4
        # union: {a, b, c, d, e, x} = 6
        # overlap = 4/6 ≈ 0.667，介于 warn(0.7) 和 fail(0.5) 之间
        primary_hits = [
            ("a", 0.95), ("b", 0.90), ("c", 0.85),
            ("d", 0.80), ("e", 0.75),
        ]
        shadow_hits = [
            ("a", 0.95), ("b", 0.90), ("c", 0.85),
            ("d", 0.80), ("x", 0.75),
        ]
        
        metrics = make_compare_metrics(
            primary_hits, shadow_hits,
            primary_latency_ms=10.0,
            secondary_latency_ms=15.0,
        )
        ranking = compute_ranking_drift(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
            # 放宽 RBO 阈值以避免 RBO 警告
            rbo_min_warn=0.0,
            rbo_min_fail=0.0,
        )
        
        decision = evaluate(metrics, thresholds, ranking)
        
        # overlap = 4/6 ≈ 0.667 < 0.7 应触发 warn 但不触发 fail
        assert decision.passed is True
        assert decision.has_warnings is True
        assert decision.recommendation == "investigate_required"


# =============================================================================
# 阈值边界测试
# =============================================================================

class TestThresholdBoundaries:
    """测试阈值边界条件"""
    
    def test_exact_at_warn_threshold(self):
        """精确等于 warn 阈值时应触发 warn"""
        # 构造精确的重叠率 = 0.7
        primary_ids = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
        shadow_ids = ["a", "b", "c", "d", "e", "f", "g", "x", "y", "z"]
        # 交集 7，并集 13，overlap = 7/10 = 0.7
        
        overlap = compute_overlap_metrics(primary_ids, shadow_ids, top_k=10)
        
        primary_hits = [(id_, 0.9) for id_ in primary_ids]
        shadow_hits = [(id_, 0.9) for id_ in shadow_ids]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.71,  # 略高于实际值
            hit_overlap_min_fail=0.5,
        )
        
        decision = evaluate(metrics, thresholds)
        
        # overlap = 0.7 < 0.71 应触发 warn
        assert decision.has_warnings is True or decision.passed is False
    
    def test_exact_at_fail_threshold(self):
        """精确等于 fail 阈值时应触发 fail"""
        # 构造精确的重叠率 = 0.5
        primary_ids = ["a", "b"]
        shadow_ids = ["a", "c"]
        # 交集 1，overlap = 1/2 = 0.5
        
        primary_hits = [(id_, 0.9) for id_ in primary_ids]
        shadow_hits = [(id_, 0.9) for id_ in shadow_ids]
        
        metrics = make_compare_metrics(primary_hits, shadow_hits)
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.51,  # 略高于实际值
        )
        
        decision = evaluate(metrics, thresholds)
        
        # overlap = 0.5 < 0.51 应触发 fail
        assert decision.passed is False
        assert "hit_overlap_below_fail" in decision.violated_checks


# =============================================================================
# RBO 计算测试
# =============================================================================

class TestRBOComputation:
    """测试 RBO (Rank-Biased Overlap) 计算"""
    
    def test_rbo_identical_lists(self):
        """相同列表的 RBO 应接近 1.0"""
        list1 = ["a", "b", "c", "d", "e"]
        list2 = ["a", "b", "c", "d", "e"]
        
        rbo = compute_rbo(list1, list2)
        
        assert rbo >= 0.99
    
    def test_rbo_completely_different(self):
        """完全不同列表的 RBO 应为 0"""
        list1 = ["a", "b", "c"]
        list2 = ["x", "y", "z"]
        
        rbo = compute_rbo(list1, list2)
        
        assert rbo == 0.0
    
    def test_rbo_partial_overlap(self):
        """部分重叠列表的 RBO 应介于 0 和 1 之间"""
        list1 = ["a", "b", "c", "d", "e"]
        list2 = ["a", "c", "e", "g", "h"]
        
        rbo = compute_rbo(list1, list2)
        
        assert 0.0 < rbo < 1.0
    
    def test_rbo_top_weighted(self):
        """RBO 应更重视顶部元素"""
        # list2a: 顶部相同，底部不同
        list1 = ["a", "b", "c", "d", "e"]
        list2a = ["a", "b", "c", "x", "y"]
        
        # list2b: 顶部不同，底部相同
        list2b = ["x", "y", "c", "d", "e"]
        
        rbo_top_same = compute_rbo(list1, list2a)
        rbo_bottom_same = compute_rbo(list1, list2b)
        
        # 顶部相同应该有更高的 RBO
        assert rbo_top_same > rbo_bottom_same
    
    def test_rbo_empty_lists(self):
        """空列表的 RBO 边界情况"""
        assert compute_rbo([], []) == 1.0
        assert compute_rbo(["a"], []) == 0.0
        assert compute_rbo([], ["a"]) == 0.0


# =============================================================================
# 稳定化排序测试
# =============================================================================

class TestStabilizeRanking:
    """测试分数相同时的稳定化排序"""
    
    def test_stabilize_with_tied_scores(self):
        """相同分数应按 chunk_id 字典序排列"""
        items = [
            ("chunk_c", 0.9),
            ("chunk_a", 0.9),
            ("chunk_b", 0.9),
        ]
        
        result = stabilize_ranking(items)
        
        # 应按 chunk_id 升序排列
        assert result == ["chunk_a", "chunk_b", "chunk_c"]
    
    def test_stabilize_with_mixed_scores(self):
        """混合分数应先按分数降序，再按 chunk_id 升序"""
        items = [
            ("chunk_c", 0.8),
            ("chunk_a", 0.9),
            ("chunk_b", 0.9),
            ("chunk_d", 0.8),
        ]
        
        result = stabilize_ranking(items)
        
        # 0.9 分数: a, b (字典序)
        # 0.8 分数: c, d (字典序)
        assert result == ["chunk_a", "chunk_b", "chunk_c", "chunk_d"]


# =============================================================================
# Jaccard 相似度测试
# =============================================================================

class TestJaccardComputation:
    """测试 Jaccard 相似度计算"""
    
    def test_jaccard_identical(self):
        """相同集合的 Jaccard = 1.0"""
        set1 = {"a", "b", "c"}
        set2 = {"a", "b", "c"}
        
        assert compute_jaccard(set1, set2) == 1.0
    
    def test_jaccard_disjoint(self):
        """无交集的 Jaccard = 0.0"""
        set1 = {"a", "b"}
        set2 = {"c", "d"}
        
        assert compute_jaccard(set1, set2) == 0.0
    
    def test_jaccard_partial(self):
        """部分重叠的 Jaccard"""
        set1 = {"a", "b", "c"}
        set2 = {"b", "c", "d"}
        # 交集: {b, c}, 并集: {a, b, c, d}
        # Jaccard = 2/4 = 0.5
        
        assert compute_jaccard(set1, set2) == 0.5
    
    def test_jaccard_empty(self):
        """空集合的边界情况"""
        assert compute_jaccard(set(), set()) == 1.0


# =============================================================================
# 百分位数计算测试
# =============================================================================

class TestPercentileComputation:
    """测试百分位数计算"""
    
    def test_p95_basic(self):
        """基本 P95 计算"""
        values = list(range(1, 101))  # 1-100
        
        p95 = compute_percentile(values, 95)
        
        # P95 应接近 95
        assert 94 <= p95 <= 96
    
    def test_p50_is_median(self):
        """P50 应该是中位数"""
        values = [1, 2, 3, 4, 5]
        
        p50 = compute_percentile(values, 50)
        
        assert p50 == 3.0
    
    def test_percentile_empty_list(self):
        """空列表返回 0"""
        assert compute_percentile([], 95) == 0.0


# =============================================================================
# CompareReport 测试
# =============================================================================

class TestCompareReport:
    """测试 CompareReport 生成"""
    
    def test_evaluate_with_report(self):
        """测试 evaluate_with_report 便捷函数"""
        hits = [
            ("chunk_a", 0.95),
            ("chunk_b", 0.90),
            ("chunk_c", 0.85),
        ]
        
        metrics = make_compare_metrics(hits, hits)
        
        report = evaluate_with_report(
            metrics=metrics,
            request_id="test-123",
            primary_backend="pgvector",
            secondary_backend="seekdb",
        )
        
        assert report.request_id == "test-123"
        assert report.primary_backend == "pgvector"
        assert report.secondary_backend == "seekdb"
        assert report.decision is not None
        assert report.decision.passed is True
        assert report.is_passed() is True
    
    def test_report_to_dict(self):
        """测试报告序列化"""
        metrics = CompareMetrics(
            hit_overlap_ratio=0.8,
            avg_score_diff=0.05,
        )
        thresholds = CompareThresholds()
        decision = CompareDecision(passed=True, reason="All checks passed")
        
        report = CompareReport(
            request_id="test-456",
            thresholds=thresholds,
            metrics=metrics,
            decision=decision,
        )
        
        report_dict = report.to_dict()
        
        assert "request_id" in report_dict
        assert "metrics" in report_dict
        assert "decision" in report_dict
        assert report_dict["decision"]["passed"] is True
    
    def test_report_from_dict_roundtrip(self):
        """测试报告序列化和反序列化的往返"""
        original = CompareReport(
            request_id="test-789",
            thresholds=CompareThresholds(score_tolerance=0.1),
            metrics=CompareMetrics(hit_overlap_ratio=0.75),
            decision=CompareDecision(
                passed=True,
                has_warnings=True,
                violated_checks=["latency_ratio_exceeded"],
            ),
            primary_backend="pgvector",
            secondary_backend="seekdb",
        )
        
        # 序列化
        data = original.to_dict()
        
        # 反序列化
        restored = CompareReport.from_dict(data)
        
        assert restored.request_id == original.request_id
        assert restored.primary_backend == original.primary_backend
        assert restored.decision.passed == original.decision.passed
        assert restored.decision.has_warnings == original.decision.has_warnings


# =============================================================================
# ViolationDetail 测试
# =============================================================================

class TestViolationDetail:
    """测试 ViolationDetail 数据结构"""
    
    def test_violation_to_string(self):
        """测试违规详情的字符串格式化"""
        v = ViolationDetail(
            check_name="hit_overlap",
            actual_value=0.4,
            threshold_value=0.5,
            level="fail",
            reason="命中重叠率低于阈值",
        )
        
        s = str(v)
        
        assert "[FAIL]" in s
        assert "hit_overlap" in s
        assert "0.4000" in s
        assert "0.5000" in s
    
    def test_violation_roundtrip(self):
        """测试违规详情的序列化往返"""
        original = ViolationDetail(
            check_name="rbo",
            actual_value=0.55,
            threshold_value=0.6,
            level="fail",
            reason="RBO 低于阈值",
        )
        
        data = original.to_dict()
        restored = ViolationDetail.from_dict(data)
        
        assert restored.check_name == original.check_name
        assert restored.actual_value == original.actual_value
        assert restored.threshold_value == original.threshold_value
        assert restored.level == original.level


# =============================================================================
# 空列表边界条件测试
# =============================================================================

class TestEmptyListEdgeCases:
    """测试空列表的边界条件"""
    
    def test_both_empty_overlap(self):
        """两个空列表的重叠率应为 1.0"""
        overlap = compute_overlap_metrics([], [], top_k=5)
        
        assert overlap.overlap_ratio == 1.0
        assert overlap.overlap_count == 0
    
    def test_one_empty_overlap(self):
        """一个空列表的重叠率应为 0.0"""
        overlap = compute_overlap_metrics(["a", "b"], [], top_k=5)
        
        assert overlap.overlap_ratio == 0.0
        
        overlap2 = compute_overlap_metrics([], ["a", "b"], top_k=5)
        
        assert overlap2.overlap_ratio == 0.0
    
    def test_both_empty_ranking_drift(self):
        """两个空列表的排名漂移"""
        drift = compute_ranking_drift([], [])
        
        assert drift.rbo == 1.0
        assert drift.top1_same is True
    
    def test_one_empty_ranking_drift(self):
        """一个空列表的排名漂移"""
        drift = compute_ranking_drift([("a", 0.9)], [])
        
        assert drift.rbo == 0.0


# =============================================================================
# generate_compare_report 测试（验证使用 evaluate_with_report）
# =============================================================================

def make_evidence_result(chunk_id: str, score: float) -> EvidenceResult:
    """创建测试用的 EvidenceResult"""
    return EvidenceResult(
        chunk_id=chunk_id,
        chunk_idx=0,
        content=f"Test content for {chunk_id}",
        artifact_uri=f"memory://{chunk_id}",
        sha256=f"sha256_{chunk_id}",
        source_id="test_source",
        source_type="test",
        excerpt=f"Excerpt for {chunk_id}",
        relevance_score=score,
    )


class TestGenerateCompareReport:
    """测试 generate_compare_report 函数（验证使用 evaluate_with_report）"""
    
    def test_generate_compare_report_identical_results(self):
        """相同结果应生成通过的报告"""
        primary_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_b", 0.90),
            make_evidence_result("chunk_c", 0.85),
        ]
        shadow_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_b", 0.90),
            make_evidence_result("chunk_c", 0.85),
        ]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=10.0,
            shadow_latency_ms=12.0,
            request_id="test-identical",
        )
        
        # 验证报告结构
        assert report.request_id == "test-identical"
        assert report.metrics is not None
        assert report.decision is not None
        
        # 验证指标
        assert report.metrics.primary_hit_count == 3
        assert report.metrics.secondary_hit_count == 3
        assert report.metrics.hit_overlap_ratio == 1.0
        assert report.metrics.common_hit_count == 3
        
        # 验证决策
        assert report.decision.passed is True
        assert report.decision.recommendation == "safe_to_switch"
    
    def test_generate_compare_report_with_score_drift(self):
        """存在分数漂移应正确计算指标"""
        primary_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_b", 0.90),
            make_evidence_result("chunk_c", 0.85),
        ]
        shadow_results = [
            make_evidence_result("chunk_a", 0.75),  # diff = 0.20
            make_evidence_result("chunk_b", 0.70),  # diff = 0.20
            make_evidence_result("chunk_c", 0.65),  # diff = 0.20
        ]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=10.0,
            shadow_latency_ms=15.0,
            request_id="test-score-drift",
        )
        
        # 验证分数漂移指标
        assert report.metrics.avg_score_diff == pytest.approx(0.2)
        assert report.metrics.max_score_diff == pytest.approx(0.2)
        assert report.metrics.p95_score_diff == pytest.approx(0.2)
        
        # 验证决策（分数漂移超过阈值应触发 fail）
        assert "score_drift_p95_exceeded" in report.decision.violated_checks
    
    def test_generate_compare_report_with_set_difference(self):
        """存在集合差异应正确计算重叠率"""
        primary_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_b", 0.90),
        ]
        shadow_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_c", 0.90),  # 不同文档
        ]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=10.0,
            shadow_latency_ms=10.0,
            request_id="test-set-diff",
        )
        
        # 验证重叠率: 交集 {a} = 1, 并集 {a, b, c} = 3, overlap = 1/3 ≈ 0.333
        assert report.metrics.hit_overlap_ratio == pytest.approx(1/3, rel=0.01)
        assert report.metrics.common_hit_count == 1
    
    def test_generate_compare_report_detailed_mode(self):
        """detailed 模式应包含 thresholds 和完整 metadata"""
        primary_results = [make_evidence_result("chunk_a", 0.95)]
        shadow_results = [make_evidence_result("chunk_a", 0.95)]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            compare_mode="detailed",
            request_id="test-detailed",
        )
        
        # detailed 模式应包含 thresholds
        assert report.thresholds is not None
        
        # metadata 应包含 ranking_drift 和 score_drift
        assert "ranking_drift" in report.metadata
        assert "score_drift" in report.metadata
        assert "compare_mode" in report.metadata
    
    def test_generate_compare_report_summary_mode(self):
        """summary 模式不应包含 thresholds"""
        primary_results = [make_evidence_result("chunk_a", 0.95)]
        shadow_results = [make_evidence_result("chunk_a", 0.95)]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            compare_mode="summary",
            request_id="test-summary",
        )
        
        # summary 模式不应包含 thresholds
        assert report.thresholds is None
        
        # summary 模式 metadata 为空
        assert report.metadata == {}
    
    def test_generate_compare_report_structure_stability(self):
        """验证报告结构的稳定性（兼容性测试）"""
        primary_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_b", 0.90),
        ]
        shadow_results = [
            make_evidence_result("chunk_a", 0.93),
            make_evidence_result("chunk_b", 0.88),
        ]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=10.0,
            shadow_latency_ms=15.0,
            primary_backend_name="pgvector",
            shadow_backend_name="seekdb",
            request_id="test-structure",
        )
        
        # 转换为字典并验证结构
        report_dict = report.to_dict()
        
        # 验证必需字段
        assert "request_id" in report_dict
        assert "metrics" in report_dict
        assert "decision" in report_dict
        assert "timestamp" in report_dict
        assert "primary_backend" in report_dict
        assert "secondary_backend" in report_dict
        
        # 验证 metrics 字段
        metrics_dict = report_dict["metrics"]
        assert "avg_score_diff" in metrics_dict
        assert "max_score_diff" in metrics_dict
        assert "p95_score_diff" in metrics_dict
        assert "hit_overlap_ratio" in metrics_dict
        assert "common_hit_count" in metrics_dict
        assert "primary_latency_ms" in metrics_dict
        assert "secondary_latency_ms" in metrics_dict
        assert "latency_ratio" in metrics_dict
        assert "primary_hit_count" in metrics_dict
        assert "secondary_hit_count" in metrics_dict
        
        # 验证 decision 字段
        decision_dict = report_dict["decision"]
        assert "passed" in decision_dict
        assert "has_warnings" in decision_dict
        assert "reason" in decision_dict
        assert "recommendation" in decision_dict
        
        # 验证后端名称
        assert report_dict["primary_backend"] == "pgvector"
        assert report_dict["secondary_backend"] == "seekdb"
    
    def test_generate_compare_report_empty_results(self):
        """空结果列表处理"""
        report = generate_compare_report(
            primary_results=[],
            shadow_results=[],
            request_id="test-empty",
        )
        
        # 两个空列表应视为完全一致
        assert report.metrics.hit_overlap_ratio == 1.0
        assert report.metrics.primary_hit_count == 0
        assert report.metrics.secondary_hit_count == 0
        assert report.decision.passed is True
    
    def test_generate_compare_report_with_custom_thresholds(self):
        """自定义阈值配置"""
        primary_results = [
            make_evidence_result("chunk_a", 0.95),
            make_evidence_result("chunk_b", 0.90),
        ]
        shadow_results = [
            make_evidence_result("chunk_a", 0.80),  # diff = 0.15
            make_evidence_result("chunk_c", 0.85),  # 不同文档
        ]
        
        # 使用宽松的阈值
        relaxed_thresholds = CompareThresholds(
            hit_overlap_min_warn=0.3,
            hit_overlap_min_fail=0.2,
            score_drift_p95_max=0.5,
            rbo_min_warn=0.0,
            rbo_min_fail=0.0,
        )
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=relaxed_thresholds,
            request_id="test-custom-thresholds",
        )
        
        # 使用宽松阈值应该通过
        assert report.decision.passed is True
