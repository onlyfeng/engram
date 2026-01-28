"""
test_dual_read_unit.py - 双读功能单元测试

使用 MagicMock 模拟 IndexBackend.query() 行为，覆盖所有双读模式分支与关键统计字段。

测试覆盖范围:
1. 双读未启用时的行为
2. compare 模式: 同时查询并比较结果
3. fallback 模式: primary 优先，失败/无结果时 fallback
4. shadow_only 模式: 仅使用 shadow 后端
5. DualReadStats 统计字段计算
6. CompareReport 生成与阈值校验
7. 异常处理路径

运行方式:
    cd apps/step3_seekdb_rag_hybrid
    pytest tests/test_dual_read_unit.py -v
"""

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from typing import List, Optional

# 导入被测试的模块
from step3_seekdb_rag_hybrid.index_backend.types import QueryHit, QueryRequest
from step3_seekdb_rag_hybrid.index_backend.base import IndexBackend
from step3_seekdb_rag_hybrid.seek_query import (
    query_evidence,
    query_evidence_dual_read,
    run_query,
    compute_dual_read_stats,
    generate_compare_report,
    EvidenceResult,
    QueryFilters,
    DualReadStats,
    # 全局状态管理
    set_index_backend,
    set_shadow_backend,
    set_dual_read_config,
    get_index_backend,
    get_shadow_backend,
)
from step3_seekdb_rag_hybrid.step3_backend_factory import (
    DualReadConfig,
    DUAL_READ_STRATEGY_COMPARE,
    DUAL_READ_STRATEGY_FALLBACK,
    DUAL_READ_STRATEGY_SHADOW_ONLY,
)
from step3_seekdb_rag_hybrid.dual_read_compare import (
    CompareThresholds,
    CompareReport,
)


# ============ Mock 数据工厂 ============


def make_query_hit(
    chunk_id: str,
    score: float,
    content: str = "test content",
    source_type: str = "git",
    source_id: str = "repo:commit123",
) -> QueryHit:
    """创建测试用 QueryHit"""
    return QueryHit(
        chunk_id=chunk_id,
        content=content,
        score=score,
        source_type=source_type,
        source_id=source_id,
        artifact_uri=f"memory://{chunk_id}",
        chunk_idx=0,
        sha256="abc123",
        excerpt=content[:50],
        metadata={"project_key": "test_project"},
    )


def make_query_hits(prefix: str = "chunk", count: int = 3, base_score: float = 0.9) -> List[QueryHit]:
    """创建多个测试用 QueryHit"""
    return [
        make_query_hit(
            chunk_id=f"{prefix}_{i}",
            score=base_score - i * 0.1,
        )
        for i in range(count)
    ]


class MockBackend:
    """
    自定义 Mock 后端，用于精确控制 query() 行为
    """
    
    def __init__(
        self,
        name: str = "mock",
        hits: Optional[List[QueryHit]] = None,
        raise_exception: Optional[Exception] = None,
        table_name: str = "chunks_mock",
        collection_strategy_name: str = "per_table",
        canonical_id: str = "test:v1:model",
    ):
        self.backend_name = name
        self._hits = hits or []
        self._raise_exception = raise_exception
        self._query_count = 0
        self.table_name = table_name
        self._table_name = table_name  # 兼容性属性
        self.collection_strategy_name = collection_strategy_name
        self.canonical_id = canonical_id
        self.collection_id = canonical_id
    
    @property
    def supports_vector_search(self) -> bool:
        return True
    
    def query(self, request: QueryRequest) -> List[QueryHit]:
        self._query_count += 1
        if self._raise_exception:
            raise self._raise_exception
        return self._hits
    
    def set_hits(self, hits: List[QueryHit]) -> None:
        """动态设置返回结果"""
        self._hits = hits
    
    def set_exception(self, exc: Optional[Exception]) -> None:
        """动态设置抛出的异常"""
        self._raise_exception = exc
    
    @property
    def query_count(self) -> int:
        """返回 query() 被调用次数"""
        return self._query_count


# ============ Fixture ============


@pytest.fixture
def primary_backend():
    """Primary 后端 mock"""
    return MockBackend(
        name="primary_mock",
        hits=make_query_hits("primary", count=3, base_score=0.95),
        table_name="chunks_primary",
        collection_strategy_name="per_table",
        canonical_id="test:v1:bge-m3",
    )


@pytest.fixture
def shadow_backend():
    """Shadow 后端 mock"""
    return MockBackend(
        name="shadow_mock",
        hits=make_query_hits("shadow", count=3, base_score=0.90),
        table_name="chunks_shadow",
        collection_strategy_name="single_table",
        canonical_id="test:v1:bge-m3",
    )


@pytest.fixture
def dual_read_config_compare():
    """compare 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_COMPARE,
        shadow_strategy="single_table",
        shadow_table="chunks_shadow",
        log_diff=True,
        diff_threshold=0.1,
    )


@pytest.fixture
def dual_read_config_fallback():
    """fallback 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_FALLBACK,
        shadow_strategy="single_table",
        shadow_table="chunks_shadow",
    )


@pytest.fixture
def dual_read_config_shadow_only():
    """shadow_only 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_SHADOW_ONLY,
        shadow_strategy="single_table",
        shadow_table="chunks_shadow",
    )


@pytest.fixture(autouse=True)
def reset_global_state():
    """每个测试前后重置全局状态"""
    # 保存原始状态
    original_backend = get_index_backend()
    original_shadow = get_shadow_backend()
    
    yield
    
    # 恢复原始状态
    set_index_backend(original_backend)
    set_shadow_backend(original_shadow)
    set_dual_read_config(None)


# ============ 基础 query_evidence 测试 ============


class TestQueryEvidence:
    """query_evidence 函数的基础测试"""
    
    def test_query_evidence_returns_results(self, primary_backend):
        """测试正常返回结果"""
        results = query_evidence(
            query_text="test query",
            backend=primary_backend,
            top_k=10,
        )
        
        assert len(results) == 3
        assert all(isinstance(r, EvidenceResult) for r in results)
        assert results[0].chunk_id == "primary_0"
        assert results[0].relevance_score == 0.95
    
    def test_query_evidence_no_backend(self):
        """测试无后端时返回空结果"""
        set_index_backend(None)
        
        results = query_evidence(
            query_text="test query",
            backend=None,
            top_k=10,
        )
        
        assert results == []
    
    def test_query_evidence_with_filters(self, primary_backend):
        """测试带过滤条件的查询"""
        filters = QueryFilters(
            project_key="webapp",
            source_type="git",
        )
        
        results = query_evidence(
            query_text="test query",
            filters=filters,
            backend=primary_backend,
            top_k=5,
        )
        
        assert len(results) == 3
        # 验证后端确实被调用了
        assert primary_backend.query_count == 1
    
    def test_query_evidence_raises_exception(self, primary_backend):
        """测试后端抛出异常时的处理"""
        primary_backend.set_exception(RuntimeError("Database connection failed"))
        
        with pytest.raises(RuntimeError, match="Database connection failed"):
            query_evidence(
                query_text="test query",
                backend=primary_backend,
                top_k=10,
            )


# ============ query_evidence_dual_read 测试 ============


class TestQueryEvidenceDualRead:
    """query_evidence_dual_read 函数的双读策略测试"""
    
    def test_dual_read_disabled(self, primary_backend, shadow_backend):
        """测试双读未启用时直接使用 primary"""
        config = DualReadConfig(enabled=False)
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        # shadow 不应被调用
        assert shadow_backend.query_count == 0
    
    def test_dual_read_no_shadow_backend(self, primary_backend, dual_read_config_compare):
        """测试无 shadow 后端时使用 primary"""
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=None,
            dual_read_config=dual_read_config_compare,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
    
    def test_shadow_only_strategy(
        self, primary_backend, shadow_backend, dual_read_config_shadow_only
    ):
        """测试 shadow_only 策略仅使用 shadow 后端"""
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_shadow_only,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        # primary 不应被调用
        assert primary_backend.query_count == 0
        assert shadow_backend.query_count == 1
    
    def test_fallback_strategy_primary_success(
        self, primary_backend, shadow_backend, dual_read_config_fallback
    ):
        """测试 fallback 策略 - primary 成功时返回 primary 结果"""
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_fallback,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        # shadow 不应被调用
        assert shadow_backend.query_count == 0
    
    def test_fallback_strategy_primary_empty(
        self, primary_backend, shadow_backend, dual_read_config_fallback
    ):
        """测试 fallback 策略 - primary 无结果时 fallback 到 shadow"""
        primary_backend.set_hits([])  # primary 返回空结果
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_fallback,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        assert shadow_backend.query_count == 1
    
    def test_fallback_strategy_primary_exception(
        self, primary_backend, shadow_backend, dual_read_config_fallback
    ):
        """测试 fallback 策略 - primary 抛异常时 fallback 到 shadow"""
        primary_backend.set_exception(RuntimeError("Primary failed"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_fallback,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
    
    def test_compare_strategy_both_success(
        self, primary_backend, shadow_backend, dual_read_config_compare
    ):
        """测试 compare 策略 - 两个后端都成功时返回 primary 结果"""
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_compare,
        )
        
        # 应返回 primary 结果
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        # 两个后端都应被调用
        assert primary_backend.query_count == 1
        assert shadow_backend.query_count == 1
    
    def test_compare_strategy_primary_fails(
        self, primary_backend, shadow_backend, dual_read_config_compare
    ):
        """测试 compare 策略 - primary 失败时返回 shadow 结果"""
        primary_backend.set_exception(RuntimeError("Primary failed"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_compare,
        )
        
        # 应返回 shadow 结果
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
    
    def test_compare_strategy_both_fail(
        self, primary_backend, shadow_backend, dual_read_config_compare
    ):
        """测试 compare 策略 - 两个后端都失败时抛出 primary 异常"""
        primary_backend.set_exception(RuntimeError("Primary failed"))
        shadow_backend.set_exception(RuntimeError("Shadow failed"))
        
        with pytest.raises(RuntimeError, match="Primary failed"):
            query_evidence_dual_read(
                query_text="test",
                primary_backend=primary_backend,
                shadow_backend=shadow_backend,
                dual_read_config=dual_read_config_compare,
            )


# ============ DualReadStats 统计测试 ============


class TestDualReadStats:
    """DualReadStats 统计计算测试"""
    
    def test_compute_stats_identical_results(self):
        """测试完全相同结果时的统计"""
        hits = make_query_hits("chunk", count=3, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in hits]
        shadow_results = [EvidenceResult.from_query_hit(h) for h in hits]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=50.0,
            shadow_latency_ms=60.0,
        )
        
        # 完全重叠
        assert stats.overlap_ratio == 1.0
        assert stats.primary_count == 3
        assert stats.shadow_count == 3
        assert stats.common_count == 3
        assert len(stats.only_primary) == 0
        assert len(stats.only_shadow) == 0
        # 分数差异应为 0
        assert stats.score_diff_mean == 0.0
        assert stats.score_diff_max == 0.0
        # 延迟
        assert stats.primary_latency_ms == 50.0
        assert stats.shadow_latency_ms == 60.0
    
    def test_compute_stats_partial_overlap(self):
        """测试部分重叠结果时的统计"""
        # Primary: chunk_0, chunk_1, chunk_2
        primary_hits = make_query_hits("chunk", count=3, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
        
        # Shadow: chunk_1, chunk_2, chunk_3 (chunk_0 替换为 chunk_3)
        shadow_hits = [
            make_query_hit("chunk_1", score=0.85),  # 与 primary 的 chunk_1 分数不同
            make_query_hit("chunk_2", score=0.7),
            make_query_hit("chunk_3", score=0.6),
        ]
        shadow_results = [EvidenceResult.from_query_hit(h) for h in shadow_hits]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
        )
        
        # 计算 Jaccard: 交集 2 (chunk_1, chunk_2), 并集 4 (chunk_0, chunk_1, chunk_2, chunk_3)
        assert stats.common_count == 2
        assert len(stats.only_primary) == 1  # chunk_0
        assert len(stats.only_shadow) == 1   # chunk_3
        assert stats.overlap_ratio == pytest.approx(2 / 4, rel=0.01)
    
    def test_compute_stats_no_overlap(self):
        """测试无重叠结果时的统计"""
        primary_hits = make_query_hits("primary", count=2, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
        
        shadow_hits = make_query_hits("shadow", count=2, base_score=0.85)
        shadow_results = [EvidenceResult.from_query_hit(h) for h in shadow_hits]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
        )
        
        assert stats.common_count == 0
        assert stats.overlap_ratio == 0.0
        assert len(stats.only_primary) == 2
        assert len(stats.only_shadow) == 2
    
    def test_compute_stats_with_score_diff(self):
        """测试分数差异计算"""
        primary_results = [
            EvidenceResult.from_query_hit(make_query_hit("c1", score=0.9)),
            EvidenceResult.from_query_hit(make_query_hit("c2", score=0.8)),
        ]
        shadow_results = [
            EvidenceResult.from_query_hit(make_query_hit("c1", score=0.85)),  # diff=0.05
            EvidenceResult.from_query_hit(make_query_hit("c2", score=0.7)),   # diff=0.10
        ]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
        )
        
        assert stats.score_diff_mean == pytest.approx(0.075, rel=0.01)  # (0.05+0.10)/2
        assert stats.score_diff_max == pytest.approx(0.10, rel=0.01)
    
    def test_compute_stats_shadow_error(self):
        """测试 shadow 查询失败时的统计"""
        primary_hits = make_query_hits("primary", count=2, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=[],
            shadow_error="Connection timeout",
        )
        
        assert stats.primary_count == 2
        assert stats.shadow_count == 0
        assert stats.shadow_error == "Connection timeout"
    
    def test_compute_stats_both_empty(self):
        """测试两个结果都为空时的统计"""
        stats = compute_dual_read_stats(
            primary_results=[],
            shadow_results=[],
        )
        
        # 注意：当 shadow_results 为空时，compute_dual_read_stats 会提前返回
        # 因此 overlap_ratio 保持默认值 0.0
        assert stats.primary_count == 0
        assert stats.shadow_count == 0
        assert stats.overlap_ratio == 0.0  # 提前返回，保持默认值
        assert stats.common_count == 0
    
    def test_stats_to_dict(self, primary_backend, shadow_backend):
        """测试 DualReadStats.to_dict() 输出格式"""
        primary_hits = make_query_hits("chunk", count=5, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
        shadow_results = primary_results[:3]  # 只有前 3 个
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            primary_latency_ms=50.0,
            shadow_latency_ms=60.0,
        )
        
        result = stats.to_dict()
        
        # 验证输出结构
        assert "health" in result
        assert "primary" in result["health"]
        assert "shadow" in result["health"]
        assert "metrics" in result
        assert "latency" in result
        
        # 验证健康信息
        assert result["health"]["primary"]["table"] == "chunks_primary"
        assert result["health"]["shadow"]["table"] == "chunks_shadow"
        
        # 验证指标
        assert result["metrics"]["primary_count"] == 5
        assert result["metrics"]["shadow_count"] == 3


# ============ CompareReport 生成测试 ============


class TestCompareReport:
    """CompareReport 生成与阈值校验测试"""
    
    def test_generate_report_passing(self):
        """测试生成通过的比较报告"""
        # 两个结果完全一致
        hits = make_query_hits("chunk", count=3, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in hits]
        shadow_results = [EvidenceResult.from_query_hit(h) for h in hits]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=50.0,
            shadow_latency_ms=60.0,
        )
        
        assert report.decision is not None
        assert report.decision.passed is True
        assert report.decision.recommendation == "safe_to_switch"
        assert report.metrics.hit_overlap_ratio == 1.0
    
    def test_generate_report_with_warnings(self):
        """测试生成带警告的比较报告"""
        # 使用相同排名但分数略有差异的结果，确保 RBO 不会太低
        primary_results = [
            EvidenceResult.from_query_hit(make_query_hit("c1", score=0.9)),
            EvidenceResult.from_query_hit(make_query_hit("c2", score=0.8)),
            EvidenceResult.from_query_hit(make_query_hit("c3", score=0.7)),
        ]
        # shadow 有 2 个相同结果，overlap 为 2/4 = 0.5
        shadow_results = [
            EvidenceResult.from_query_hit(make_query_hit("c1", score=0.9)),
            EvidenceResult.from_query_hit(make_query_hit("c2", score=0.8)),
            EvidenceResult.from_query_hit(make_query_hit("c4", score=0.65)),
        ]
        
        # 设置阈值使 overlap 触发警告但不触发失败
        # RBO 也需要调整以避免 fail
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.6,  # 警告阈值（overlap=0.5 会触发）
            hit_overlap_min_fail=0.3,  # 失败阈值
            rbo_min_warn=0.9,          # RBO 警告阈值
            rbo_min_fail=0.5,          # RBO 失败阈值（设低以避免 fail）
        )
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=thresholds,
        )
        
        # overlap = 2/4 = 0.5，高于 fail 阈值(0.3)但低于 warn 阈值(0.6)
        assert report.decision.passed is True
        assert report.decision.has_warnings is True
        assert report.decision.recommendation == "investigate_required"
    
    def test_generate_report_failing(self):
        """测试生成失败的比较报告"""
        primary_results = [
            EvidenceResult.from_query_hit(make_query_hit("p1", score=0.9)),
            EvidenceResult.from_query_hit(make_query_hit("p2", score=0.8)),
        ]
        # 完全不同的结果
        shadow_results = [
            EvidenceResult.from_query_hit(make_query_hit("s1", score=0.9)),
            EvidenceResult.from_query_hit(make_query_hit("s2", score=0.8)),
        ]
        
        thresholds = CompareThresholds(
            hit_overlap_min_fail=0.5,  # 设置较高的失败阈值
        )
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            thresholds=thresholds,
        )
        
        assert report.decision.passed is False
        assert report.decision.recommendation == "abort_switch"
        assert "hit_overlap" in report.decision.violated_checks
    
    def test_generate_report_metrics(self):
        """测试比较报告中的指标计算"""
        primary_results = [
            EvidenceResult.from_query_hit(make_query_hit("c1", score=0.9)),
            EvidenceResult.from_query_hit(make_query_hit("c2", score=0.8)),
            EvidenceResult.from_query_hit(make_query_hit("c3", score=0.7)),
        ]
        shadow_results = [
            EvidenceResult.from_query_hit(make_query_hit("c1", score=0.88)),
            EvidenceResult.from_query_hit(make_query_hit("c2", score=0.78)),
            EvidenceResult.from_query_hit(make_query_hit("c4", score=0.65)),
        ]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=100.0,
            shadow_latency_ms=150.0,
        )
        
        metrics = report.metrics
        
        # 命中统计
        assert metrics.primary_hit_count == 3
        assert metrics.secondary_hit_count == 3
        assert metrics.common_hit_count == 2  # c1, c2
        
        # overlap = 2 / 4 = 0.5
        assert metrics.hit_overlap_ratio == pytest.approx(0.5, rel=0.01)
        
        # 延迟
        assert metrics.primary_latency_ms == 100.0
        assert metrics.secondary_latency_ms == 150.0
        assert metrics.latency_ratio == pytest.approx(1.5, rel=0.01)
    
    def test_report_to_dict(self):
        """测试 CompareReport.to_dict() 输出格式"""
        hits = make_query_hits("chunk", count=2, base_score=0.9)
        primary_results = [EvidenceResult.from_query_hit(h) for h in hits]
        shadow_results = [EvidenceResult.from_query_hit(h) for h in hits]
        
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            request_id="test-123",
            compare_mode="summary",
        )
        
        result = report.to_dict()
        
        assert result["request_id"] == "test-123"
        assert "metrics" in result
        assert "decision" in result
        assert result["decision"]["passed"] is True


# ============ run_query 集成测试 ============


class TestRunQueryDualRead:
    """run_query 函数的双读集成测试"""
    
    def test_run_query_with_dual_read_stats(
        self, primary_backend, shadow_backend
    ):
        """测试 run_query 带 dual_read 统计"""
        # 设置全局后端
        set_index_backend(primary_backend)
        set_shadow_backend(shadow_backend)
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
        )
        
        assert result.error is None
        assert len(result.evidences) == 3
        assert result.dual_read_stats is not None
        
        stats = result.dual_read_stats
        assert stats.primary_count == 3
        assert stats.shadow_count == 3
    
    def test_run_query_with_compare_report(
        self, primary_backend, shadow_backend
    ):
        """测试 run_query 带比较报告"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_compare=True,
            compare_mode="summary",
        )
        
        assert result.error is None
        assert result.compare_report is not None
        assert result.compare_report.decision is not None
    
    def test_run_query_shadow_fails_gracefully(
        self, primary_backend, shadow_backend
    ):
        """测试 shadow 失败时 run_query 能优雅处理"""
        shadow_backend.set_exception(RuntimeError("Shadow unavailable"))
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
        )
        
        # 应该仍返回 primary 结果
        assert result.error is None
        assert len(result.evidences) == 3
        # 统计中应记录 shadow 错误
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.shadow_error is not None
    
    def test_run_query_no_shadow_backend(self, primary_backend):
        """测试无 shadow 后端时正常运行"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=None,
            enable_dual_read=True,
        )
        
        assert result.error is None
        assert len(result.evidences) == 3
        # 无 shadow 时不应有 dual_read_stats
        assert result.dual_read_stats is None
    
    def test_run_query_output_includes_timing(self, primary_backend):
        """测试输出包含时间信息"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
        )
        
        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.duration_ms > 0


# ============ 边界情况测试 ============


class TestEdgeCases:
    """边界情况测试"""
    
    def test_empty_results_from_both(self, dual_read_config_compare):
        """测试两个后端都返回空结果"""
        primary = MockBackend(name="primary", hits=[])
        shadow = MockBackend(name="shadow", hits=[])
        
        results = query_evidence_dual_read(
            query_text="nonexistent topic",
            primary_backend=primary,
            shadow_backend=shadow,
            dual_read_config=dual_read_config_compare,
        )
        
        assert results == []
    
    def test_very_different_result_counts(self):
        """测试结果数量差异很大的情况"""
        primary_results = [
            EvidenceResult.from_query_hit(make_query_hit(f"p{i}", score=0.9-i*0.01))
            for i in range(10)
        ]
        shadow_results = [
            EvidenceResult.from_query_hit(make_query_hit(f"s{i}", score=0.85-i*0.01))
            for i in range(2)
        ]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
        )
        
        assert stats.primary_count == 10
        assert stats.shadow_count == 2
        assert stats.common_count == 0
        assert stats.overlap_ratio == 0.0
    
    def test_single_result(self):
        """测试只有单个结果的情况"""
        primary_results = [
            EvidenceResult.from_query_hit(make_query_hit("only_one", score=0.95))
        ]
        shadow_results = [
            EvidenceResult.from_query_hit(make_query_hit("only_one", score=0.93))
        ]
        
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
        )
        
        assert stats.overlap_ratio == 1.0
        assert stats.common_count == 1
        assert stats.score_diff_mean == pytest.approx(0.02, rel=0.01)
    
    def test_config_from_global_state(self, primary_backend, shadow_backend, dual_read_config_compare):
        """测试从全局状态读取配置"""
        set_index_backend(primary_backend)
        set_shadow_backend(shadow_backend)
        set_dual_read_config(dual_read_config_compare)
        
        # 不传入任何参数，依赖全局状态
        results = query_evidence_dual_read(
            query_text="test",
        )
        
        # 应该使用全局 primary 后端的结果
        assert len(results) == 3
        # 验证两个后端都被调用了（compare 模式）
        assert primary_backend.query_count == 1
        assert shadow_backend.query_count == 1


# ============ 异常处理测试 ============


class TestExceptionHandling:
    """异常处理路径测试"""
    
    def test_primary_timeout_fallback_works(self, shadow_backend, dual_read_config_fallback):
        """测试 primary 超时时 fallback 正常工作"""
        primary = MockBackend(name="primary")
        primary.set_exception(TimeoutError("Primary query timed out"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_fallback,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
    
    def test_shadow_exception_in_compare_mode(
        self, primary_backend, dual_read_config_compare
    ):
        """测试 compare 模式下 shadow 异常不影响返回"""
        shadow = MockBackend(name="shadow")
        shadow.set_exception(ConnectionError("Shadow connection failed"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow,
            dual_read_config=dual_read_config_compare,
        )
        
        # 应返回 primary 结果
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
    
    def test_run_query_captures_exception(self, primary_backend):
        """测试 run_query 捕获并记录异常"""
        primary_backend.set_exception(ValueError("Invalid query"))
        
        result = run_query(
            query_text="bad query",
            backend=primary_backend,
        )
        
        assert result.error is not None
        assert "Invalid query" in result.error
        assert result.evidences == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
