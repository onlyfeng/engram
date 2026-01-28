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
    run_batch_query,
    compute_dual_read_stats,
    generate_compare_report,
    aggregate_gate_results,
    AggregateGateResult,
    EvidenceResult,
    QueryFilters,
    QueryResult,
    DualReadStats,
    DualReadGateThresholds,
    DualReadGateViolation,
    DualReadGateResult,
    ShadowQueryTimeoutError,
    execute_with_timeout,
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
    DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
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


@pytest.fixture
def dual_read_config_shadow_only_compare():
    """shadow_only_compare 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
        shadow_strategy="single_table",
        shadow_table="chunks_shadow",
        log_diff=True,
        diff_threshold=0.1,
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
        # violated_checks 格式为 "{check_name}_below_fail"，检查是否包含 hit_overlap 相关项
        assert any("hit_overlap" in check for check in report.decision.violated_checks)
    
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
    
    def test_run_query_shadow_only_compare_with_dual_read(
        self, primary_backend, shadow_backend
    ):
        """测试 shadow_only_compare 策略在 run_query enable_dual_read 中返回 shadow 结果"""
        # 设置不同的结果以便区分
        primary_backend.set_hits(make_query_hits("primary", count=3, base_score=0.95))
        shadow_backend.set_hits(make_query_hits("shadow", count=3, base_score=0.90))
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
            log_diff=True,
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_dual_read=True,
        )
        
        assert result.error is None
        assert len(result.evidences) == 3
        # 应返回 shadow 结果
        assert result.evidences[0].chunk_id == "shadow_0"
        # dual_read_stats 应该存在
        assert result.dual_read_stats is not None
    
    def test_run_query_shadow_only_compare_with_compare(
        self, primary_backend, shadow_backend
    ):
        """测试 shadow_only_compare 策略在 run_query enable_compare 中返回 shadow 结果"""
        # 设置不同的结果以便区分
        primary_backend.set_hits(make_query_hits("primary", count=3, base_score=0.95))
        shadow_backend.set_hits(make_query_hits("shadow", count=3, base_score=0.90))
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
            log_diff=True,
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_compare=True,
        )
        
        assert result.error is None
        assert len(result.evidences) == 3
        # 应返回 shadow 结果
        assert result.evidences[0].chunk_id == "shadow_0"
        # compare_report 应该存在
        assert result.compare_report is not None


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


# ============ DualReadConfig 语义测试 ============


class TestDualReadConfigSemantics:
    """
    DualReadConfig 语义测试
    
    覆盖 DualReadConfig 真值表中的所有场景：
    
    +--------+--------+--------------+--------------------------------------+
    | enabled| shadow | strategy     | 行为                                 |
    +--------+--------+--------------+--------------------------------------+
    | None/F | -      | -            | → primary_only                       |
    | True   | None   | -            | → primary_only (无 shadow 可用)      |
    | True   | 有     | shadow_only  | → 仅查询 shadow                      |
    | True   | 有     | fallback     | → primary 优先；失败/空 → shadow     |
    | True   | 有     | compare      | → 同时查询 primary + shadow          |
    +--------+--------+--------------+--------------------------------------+
    """
    
    # ---- 真值表行 1: enabled=False/None → primary_only ----
    
    def test_config_none_uses_primary_only(self, primary_backend, shadow_backend):
        """config=None 时仅使用 primary"""
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=None,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        assert shadow_backend.query_count == 0
    
    def test_enabled_false_uses_primary_only(self, primary_backend, shadow_backend):
        """enabled=False 时仅使用 primary（即使 shadow 可用）"""
        config = DualReadConfig(
            enabled=False,
            strategy=DUAL_READ_STRATEGY_COMPARE,  # 策略被忽略
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        assert shadow_backend.query_count == 0
    
    # ---- 真值表行 2: enabled=True + shadow=None → primary_only ----
    
    def test_enabled_true_no_shadow_uses_primary_only(self, primary_backend):
        """enabled=True 但 shadow=None 时仅使用 primary"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=None,  # 无 shadow
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
    
    # ---- 真值表行 3: shadow_only 策略 ----
    
    def test_shadow_only_ignores_primary(self, primary_backend, shadow_backend):
        """shadow_only 策略完全忽略 primary"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY,
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        # primary 不应被调用
        assert primary_backend.query_count == 0
        assert shadow_backend.query_count == 1
    
    def test_shadow_only_with_primary_exception_still_uses_shadow(
        self, primary_backend, shadow_backend
    ):
        """shadow_only 策略下，即使 primary 会抛异常也不影响"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY,
        )
        primary_backend.set_exception(RuntimeError("Primary exploded"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        # primary 不应被调用（所以不会抛异常）
        assert primary_backend.query_count == 0
    
    # ---- 真值表行 3.5: shadow_only_compare 策略 ----
    
    def test_shadow_only_compare_returns_shadow_but_queries_both(
        self, primary_backend, shadow_backend
    ):
        """shadow_only_compare 策略返回 shadow 结果，但同时查询 primary 做对比"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
            log_diff=True,
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        # 应返回 shadow 结果
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        # 两个后端都应被调用
        assert shadow_backend.query_count == 1
        assert primary_backend.query_count == 1
    
    def test_shadow_only_compare_primary_fail_still_returns_shadow(
        self, primary_backend, shadow_backend
    ):
        """shadow_only_compare 策略下 primary 失败不影响 shadow 结果"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
            log_diff=True,
        )
        primary_backend.set_exception(RuntimeError("Primary failed"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        # 应返回 shadow 结果
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
    
    def test_shadow_only_compare_shadow_fail_raises_exception(
        self, primary_backend, shadow_backend
    ):
        """shadow_only_compare 策略下 shadow 失败会抛出异常"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
            log_diff=True,
        )
        shadow_backend.set_exception(RuntimeError("Shadow failed"))
        
        with pytest.raises(RuntimeError, match="Shadow failed"):
            query_evidence_dual_read(
                query_text="test",
                primary_backend=primary_backend,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
    
    def test_shadow_only_compare_with_different_results(
        self, primary_backend, shadow_backend
    ):
        """shadow_only_compare 策略下不同结果应记录差异日志（返回 shadow）"""
        # 设置不同的结果
        primary_backend.set_hits(make_query_hits("primary", count=3, base_score=0.95))
        shadow_backend.set_hits(make_query_hits("shadow", count=3, base_score=0.90))
        
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
            log_diff=True,
            diff_threshold=0.1,
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        # 应返回 shadow 结果，而不是 primary
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        assert all(r.chunk_id.startswith("shadow_") for r in results)
    
    # ---- 真值表行 4: fallback 策略 ----
    
    def test_fallback_primary_success_no_shadow_call(
        self, primary_backend, shadow_backend
    ):
        """fallback 策略: primary 成功时不调用 shadow"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_FALLBACK,
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        assert primary_backend.query_count == 1
        assert shadow_backend.query_count == 0  # shadow 未被调用
    
    def test_fallback_primary_empty_triggers_shadow(
        self, primary_backend, shadow_backend
    ):
        """fallback 策略: primary 返回空列表时触发 shadow"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_FALLBACK,
        )
        primary_backend.set_hits([])  # primary 返回空
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
        assert primary_backend.query_count == 1
        assert shadow_backend.query_count == 1  # shadow 被调用
    
    def test_fallback_primary_exception_triggers_shadow(
        self, primary_backend, shadow_backend
    ):
        """fallback 策略: primary 抛异常时触发 shadow"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_FALLBACK,
        )
        primary_backend.set_exception(ConnectionError("Primary down"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"
    
    def test_fallback_both_fail_raises_shadow_exception(
        self, primary_backend, shadow_backend
    ):
        """fallback 策略: 两者都失败时抛出 shadow 的异常（因为最后执行）"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_FALLBACK,
        )
        primary_backend.set_exception(RuntimeError("Primary failed"))
        shadow_backend.set_exception(RuntimeError("Shadow failed"))
        
        with pytest.raises(RuntimeError, match="Shadow failed"):
            query_evidence_dual_read(
                query_text="test",
                primary_backend=primary_backend,
                shadow_backend=shadow_backend,
                dual_read_config=config,
            )
    
    # ---- 真值表行 5: compare 策略 ----
    
    def test_compare_calls_both_backends(self, primary_backend, shadow_backend):
        """compare 策略: 同时调用 primary 和 shadow"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
        )
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        assert primary_backend.query_count == 1
        assert shadow_backend.query_count == 1
    
    def test_compare_returns_primary_even_when_shadow_has_more(
        self, primary_backend, shadow_backend
    ):
        """compare 策略: 即使 shadow 结果更多也返回 primary"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
        )
        # shadow 有更多结果
        shadow_backend.set_hits(make_query_hits("shadow", count=10, base_score=0.95))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        # 返回 primary 的 3 条结果，不是 shadow 的 10 条
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
    
    def test_compare_shadow_fail_returns_primary(
        self, primary_backend, shadow_backend
    ):
        """compare 策略: shadow 失败时仍返回 primary"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
        )
        shadow_backend.set_exception(TimeoutError("Shadow timeout"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
    
    def test_compare_primary_fail_returns_shadow(
        self, primary_backend, shadow_backend
    ):
        """compare 策略: primary 失败时返回 shadow"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
        )
        primary_backend.set_exception(RuntimeError("Primary failed"))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "shadow_0"


class TestDualReadConfigFromEnv:
    """
    DualReadConfig.from_env() 语义测试
    
    测试环境变量解析和默认值行为。
    """
    
    def test_default_values(self, monkeypatch):
        """测试默认值"""
        # 清除所有相关环境变量
        for key in [
            "STEP3_PGVECTOR_DUAL_READ",
            "STEP3_PGVECTOR_DUAL_READ_STRATEGY",
            "STEP3_PGVECTOR_DUAL_READ_SHADOW_STRATEGY",
            "STEP3_PGVECTOR_DUAL_READ_SHADOW_TABLE",
            "STEP3_PGVECTOR_DUAL_READ_LOG_DIFF",
            "STEP3_PGVECTOR_DUAL_READ_DIFF_THRESHOLD",
            "STEP3_PGVECTOR_DUAL_READ_SHADOW_TIMEOUT_MS",
            "STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN",
        ]:
            monkeypatch.delenv(key, raising=False)
        
        config = DualReadConfig.from_env()
        
        assert config.enabled is False
        assert config.strategy == DUAL_READ_STRATEGY_COMPARE
        assert config.shadow_table == "chunks_shadow"
        assert config.log_diff is True
        assert config.diff_threshold == 0.1
        assert config.shadow_timeout_ms == 5000
        assert config.fail_open is True
    
    def test_enabled_parsing(self, monkeypatch):
        """测试 enabled 字段的各种布尔值解析"""
        test_cases = [
            ("1", True),
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("yes", True),
            ("YES", True),
            ("0", False),
            ("false", False),
            ("False", False),
            ("no", False),
            ("", False),
            ("invalid", False),
        ]
        
        for value, expected in test_cases:
            monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ", value)
            config = DualReadConfig.from_env()
            assert config.enabled is expected, f"STEP3_PGVECTOR_DUAL_READ={value} should be {expected}"
    
    def test_strategy_fallback_for_invalid(self, monkeypatch):
        """测试无效策略值回退到默认值"""
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ", "1")
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_STRATEGY", "invalid_strategy")
        
        config = DualReadConfig.from_env()
        
        assert config.strategy == DUAL_READ_STRATEGY_COMPARE  # 回退到默认值
    
    def test_shadow_strategy_auto_opposite(self, monkeypatch):
        """测试 shadow_strategy 自动选择与 primary 相反的策略"""
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ", "1")
        monkeypatch.delenv("STEP3_PGVECTOR_DUAL_READ_SHADOW_STRATEGY", raising=False)
        
        # primary 使用 per_table → shadow 应自动选择 single_table
        from step3_seekdb_rag_hybrid.step3_backend_factory import COLLECTION_STRATEGY_PER_TABLE, COLLECTION_STRATEGY_SINGLE_TABLE
        
        config = DualReadConfig.from_env(primary_strategy=COLLECTION_STRATEGY_PER_TABLE)
        assert config.shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE
        
        # primary 使用 single_table → shadow 应自动选择 per_table
        config = DualReadConfig.from_env(primary_strategy=COLLECTION_STRATEGY_SINGLE_TABLE)
        assert config.shadow_strategy == COLLECTION_STRATEGY_PER_TABLE
    
    def test_all_strategies_recognized(self, monkeypatch):
        """测试所有有效策略值都能被正确解析"""
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ", "1")
        
        for strategy in [
            DUAL_READ_STRATEGY_COMPARE,
            DUAL_READ_STRATEGY_FALLBACK,
            DUAL_READ_STRATEGY_SHADOW_ONLY,
            DUAL_READ_STRATEGY_SHADOW_ONLY_COMPARE,
        ]:
            monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_STRATEGY", strategy)
            config = DualReadConfig.from_env()
            assert config.strategy == strategy
    
    def test_numeric_fields_parsing(self, monkeypatch):
        """测试数值字段解析"""
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ", "1")
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_DIFF_THRESHOLD", "0.25")
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_SHADOW_TIMEOUT_MS", "10000")
        
        config = DualReadConfig.from_env()
        
        assert config.diff_threshold == 0.25
        assert config.shadow_timeout_ms == 10000


class TestDualReadConfigFieldInteractions:
    """
    DualReadConfig 字段交互语义测试
    
    测试多个字段组合时的行为。
    """
    
    def test_log_diff_only_matters_in_compare_mode(
        self, primary_backend, shadow_backend
    ):
        """log_diff 仅在 compare 模式下有意义"""
        # 在 shadow_only 模式下，log_diff 配置被忽略
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_SHADOW_ONLY,
            log_diff=True,  # 这个配置在 shadow_only 模式下无效
        )
        
        # 设置不同的结果
        primary_backend.set_hits(make_query_hits("p", count=2, base_score=0.9))
        shadow_backend.set_hits(make_query_hits("s", count=3, base_score=0.85))
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config,
        )
        
        # 返回 shadow 结果，primary 未被调用，无差异比较发生
        assert len(results) == 3
        assert results[0].chunk_id == "s_0"
        assert primary_backend.query_count == 0
    
    def test_diff_threshold_sensitivity(self, primary_backend, shadow_backend):
        """测试 diff_threshold 对差异判定的影响"""
        # 创建分数差异较小的结果
        primary_hits = [
            make_query_hit("c1", score=0.90),
            make_query_hit("c2", score=0.80),
        ]
        shadow_hits = [
            make_query_hit("c1", score=0.88),  # diff=0.02
            make_query_hit("c2", score=0.78),  # diff=0.02
        ]
        primary_backend.set_hits(primary_hits)
        shadow_backend.set_hits(shadow_hits)
        
        # 阈值 0.1: 差异 0.02 不触发警告
        config_loose = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            log_diff=True,
            diff_threshold=0.1,
        )
        
        # 阈值 0.01: 差异 0.02 会触发警告
        config_strict = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            log_diff=True,
            diff_threshold=0.01,
        )
        
        # 两种配置都应返回正确结果
        results1 = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config_loose,
        )
        assert len(results1) == 2
        
        # 重置调用计数
        primary_backend._query_count = 0
        shadow_backend._query_count = 0
        
        results2 = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=config_strict,
        )
        assert len(results2) == 2
    
    def test_fail_open_behavior_in_run_query(self, primary_backend, shadow_backend):
        """测试 fail_open 对 run_query 的影响"""
        shadow_backend.set_exception(ConnectionError("Shadow unavailable"))
        
        # fail_open=True (默认): shadow 失败时仍返回 primary 结果
        result = run_query(
            query_text="test",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
        )
        
        assert result.error is None
        assert len(result.evidences) == 3
        # dual_read_stats 应记录 shadow 错误
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.shadow_error is not None
        assert "Shadow unavailable" in result.dual_read_stats.shadow_error


class TestDualReadConfigToDict:
    """DualReadConfig.to_dict() 测试"""
    
    def test_to_dict_contains_all_fields(self):
        """to_dict() 应包含所有配置字段"""
        config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_FALLBACK,
            shadow_strategy="per_table",
            shadow_table="my_shadow",
            log_diff=False,
            diff_threshold=0.2,
            shadow_timeout_ms=3000,
            fail_open=False,
        )
        
        d = config.to_dict()
        
        assert d["enabled"] is True
        assert d["strategy"] == DUAL_READ_STRATEGY_FALLBACK
        assert d["shadow_strategy"] == "per_table"
        assert d["shadow_table"] == "my_shadow"
        assert d["log_diff"] is False
        assert d["diff_threshold"] == 0.2
        assert d["shadow_timeout_ms"] == 3000
        assert d["fail_open"] is False
    
    def test_to_dict_default_config(self):
        """默认配置的 to_dict() 输出"""
        config = DualReadConfig()
        d = config.to_dict()
        
        assert d["enabled"] is False
        assert d["strategy"] == DUAL_READ_STRATEGY_COMPARE
        assert d["shadow_table"] == "chunks_shadow"


# ============ fail_open 行为测试 ============


class TestFailOpenBehavior:
    """
    fail_open 参数行为测试
    
    测试 compare 和 dual_read 路径中 shadow 失败时的行为：
    - fail_open=True（默认）: shadow 失败不影响 primary 返回，记录 shadow_error
    - fail_open=False: shadow 失败导致 compare/gate 失败（用于 Nightly/切换门禁）
    """
    
    def test_compare_fail_open_true_shadow_fails(self, primary_backend, shadow_backend):
        """
        测试 fail_open=True 时 shadow 失败不影响 compare 结果
        
        期望：
        - compare_report.decision.passed = True（带警告）
        - compare_report.metadata 包含 shadow_error
        - result.evidences 返回 primary 结果
        """
        shadow_backend.set_exception(RuntimeError("Shadow connection failed"))
        
        # 创建 fail_open=True 的配置
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=True,  # 默认值
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_compare=True,
        )
        
        # 应该返回 primary 结果
        assert result.error is None
        assert len(result.evidences) == 3
        assert result.evidences[0].chunk_id == "primary_0"
        
        # compare_report 应该通过（带警告）
        assert result.compare_report is not None
        assert result.compare_report.decision is not None
        assert result.compare_report.decision.passed is True
        assert result.compare_report.decision.has_warnings is True
        assert "Shadow 查询失败" in result.compare_report.decision.reason
        assert "fail_open=True" in result.compare_report.decision.reason
        
        # metadata 应该记录 shadow_error 和 fail_open 状态
        assert result.compare_report.metadata is not None
        assert "shadow_error" in result.compare_report.metadata
        assert result.compare_report.metadata.get("fail_open") is True
    
    def test_compare_fail_open_false_shadow_fails(self, primary_backend, shadow_backend):
        """
        测试 fail_open=False 时 shadow 失败导致 compare 失败
        
        期望：
        - compare_report.decision.passed = False
        - compare_report.decision.violated_checks 包含 shadow_query_failed
        - result.evidences 仍返回 primary 结果（但门禁失败）
        """
        shadow_backend.set_exception(RuntimeError("Shadow connection failed"))
        
        # 创建 fail_open=False 的配置
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=False,  # 严格模式
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_compare=True,
        )
        
        # 应该返回 primary 结果（数据仍可用）
        assert result.error is None
        assert len(result.evidences) == 3
        assert result.evidences[0].chunk_id == "primary_0"
        
        # compare_report 应该失败
        assert result.compare_report is not None
        assert result.compare_report.decision is not None
        assert result.compare_report.decision.passed is False
        assert "Shadow 查询失败" in result.compare_report.decision.reason
        assert "fail_open=False" in result.compare_report.decision.reason
        
        # violated_checks 应该包含 shadow_query_failed
        assert "shadow_query_failed" in result.compare_report.decision.violated_checks
        
        # recommendation 应该是 abort_switch
        assert result.compare_report.decision.recommendation == "abort_switch"
        
        # metadata 应该记录 fail_open=False
        assert result.compare_report.metadata.get("fail_open") is False
    
    def test_dual_read_fail_open_true_shadow_fails(self, primary_backend, shadow_backend):
        """
        测试 fail_open=True 时 dual_read 模式下 shadow 失败不影响门禁
        
        期望：
        - dual_read_stats.shadow_error 记录错误
        - dual_read_stats.gate 不会因 shadow 失败而失败（如果配置了门禁阈值）
        """
        shadow_backend.set_exception(RuntimeError("Shadow unavailable"))
        
        # 创建 fail_open=True 的配置
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=True,
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_dual_read=True,
        )
        
        # 应该返回 primary 结果
        assert result.error is None
        assert len(result.evidences) == 3
        
        # dual_read_stats 应该记录 shadow_error
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.shadow_error is not None
        assert "Shadow unavailable" in result.dual_read_stats.shadow_error
        
        # gate 不应因 shadow 失败而自动失败（fail_open=True）
        # 注意：如果没有配置门禁阈值，gate 为 None
        if result.dual_read_stats.gate is not None:
            # 如果有门禁结果，不应该包含 shadow_query_failed 违规
            violation_names = [v.check_name for v in result.dual_read_stats.gate.violations]
            assert "shadow_query_failed" not in violation_names
    
    def test_dual_read_fail_open_false_shadow_fails(self, primary_backend, shadow_backend):
        """
        测试 fail_open=False 时 dual_read 模式下 shadow 失败导致门禁失败
        
        期望：
        - dual_read_stats.shadow_error 记录错误
        - dual_read_stats.gate.passed = False
        - dual_read_stats.gate.violations 包含 shadow_query_failed
        """
        shadow_backend.set_exception(RuntimeError("Shadow unavailable"))
        
        # 创建 fail_open=False 的配置
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=False,  # 严格模式
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_dual_read=True,
        )
        
        # 应该返回 primary 结果（数据仍可用）
        assert result.error is None
        assert len(result.evidences) == 3
        
        # dual_read_stats 应该记录 shadow_error
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.shadow_error is not None
        
        # gate 应该失败（即使没有配置门禁阈值）
        assert result.dual_read_stats.gate is not None
        assert result.dual_read_stats.gate.passed is False
        
        # violations 应该包含 shadow_query_failed
        violation_names = [v.check_name for v in result.dual_read_stats.gate.violations]
        assert "shadow_query_failed" in violation_names
    
    def test_dual_read_fail_open_false_with_gate_thresholds(self, primary_backend, shadow_backend):
        """
        测试 fail_open=False 配合门禁阈值时的行为
        
        期望：
        - 门禁阈值检查正常执行
        - shadow 失败的违规被追加到门禁结果中
        """
        shadow_backend.set_exception(RuntimeError("Shadow timeout"))
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=False,
        )
        
        # 配置门禁阈值
        from step3_seekdb_rag_hybrid.seek_query import DualReadGateThresholds
        gate_thresholds = DualReadGateThresholds(
            min_overlap=0.5,  # 这个阈值不会触发，因为 shadow 直接失败了
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config,
            enable_dual_read=True,
            dual_read_gate_thresholds=gate_thresholds,
        )
        
        # 门禁应该失败
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.gate is not None
        assert result.dual_read_stats.gate.passed is False
        
        # violations 应该包含 shadow_query_failed
        violation_names = [v.check_name for v in result.dual_read_stats.gate.violations]
        assert "shadow_query_failed" in violation_names
    
    def test_fail_open_default_is_true(self):
        """测试 fail_open 默认值为 True"""
        config = DualReadConfig()
        assert config.fail_open is True
    
    def test_fail_open_env_parsing(self, monkeypatch):
        """测试 fail_open 环境变量解析"""
        # 测试 fail_open=0 (False)
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ", "1")
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN", "0")
        
        config = DualReadConfig.from_env()
        assert config.fail_open is False
        
        # 测试 fail_open=false (False)
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN", "false")
        config = DualReadConfig.from_env()
        assert config.fail_open is False
        
        # 测试 fail_open=1 (True)
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN", "1")
        config = DualReadConfig.from_env()
        assert config.fail_open is True
        
        # 测试 fail_open=true (True)
        monkeypatch.setenv("STEP3_PGVECTOR_DUAL_READ_FAIL_OPEN", "true")
        config = DualReadConfig.from_env()
        assert config.fail_open is True


# ============ 聚合门禁测试 ============


class TestAggregateGate:
    """聚合门禁功能测试"""
    
    def test_aggregate_all_passed(self, primary_backend, shadow_backend):
        """测试所有查询都通过时的聚合结果"""
        # 创建 3 个通过的查询结果
        results = []
        for i in range(3):
            result = run_query(
                query_text=f"test query {i}",
                backend=primary_backend,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
            )
            results.append(result)
        
        agg = aggregate_gate_results(results)
        
        assert agg.passed is True
        assert agg.total_queries == 3
        assert agg.pass_count == 3
        assert agg.fail_count == 0
        assert agg.warn_count == 0
        assert agg.error_count == 0
        assert agg.worst_recommendation == "safe_to_switch"
        assert agg.failed_query_indices == []
    
    def test_aggregate_with_errors(self, primary_backend, shadow_backend):
        """测试有查询错误时的聚合结果"""
        # 创建混合结果
        results = []
        
        # 正常查询
        result1 = run_query(
            query_text="test query 1",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
        )
        results.append(result1)
        
        # 创建有错误的结果
        result2 = QueryResult(query="test query 2", error="Simulated error")
        results.append(result2)
        
        agg = aggregate_gate_results(results)
        
        assert agg.passed is False
        assert agg.total_queries == 2
        assert agg.error_count == 1
        assert 1 in agg.failed_query_indices
    
    def test_aggregate_with_gate_failures(self, primary_backend, shadow_backend):
        """测试有门禁失败时的聚合结果"""
        # 使用严格的门禁阈值，使查询失败
        strict_thresholds = DualReadGateThresholds(
            min_overlap=0.99,  # 几乎不可能达到
        )
        
        # Primary 和 Shadow 返回不同结果
        primary_backend.set_hits(make_query_hits("primary", count=3, base_score=0.9))
        shadow_backend.set_hits(make_query_hits("shadow", count=3, base_score=0.85))
        
        results = []
        for i in range(2):
            result = run_query(
                query_text=f"test query {i}",
                backend=primary_backend,
                shadow_backend=shadow_backend,
                enable_dual_read=True,
                dual_read_gate_thresholds=strict_thresholds,
            )
            results.append(result)
        
        agg = aggregate_gate_results(results)
        
        # 由于 overlap=0 (完全不同的 chunk_id)，门禁应该失败
        assert agg.passed is False
        assert agg.fail_count == 2
        assert len(agg.failed_query_indices) == 2
    
    def test_aggregate_empty_results(self):
        """测试空结果列表的聚合"""
        agg = aggregate_gate_results([])
        
        assert agg.passed is True
        assert agg.total_queries == 0
        assert agg.pass_count == 0
    
    def test_aggregate_to_dict(self, primary_backend, shadow_backend):
        """测试聚合结果的 to_dict 输出"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
        )
        
        agg = aggregate_gate_results([result])
        agg_dict = agg.to_dict()
        
        assert "passed" in agg_dict
        assert "worst_recommendation" in agg_dict
        assert "total_queries" in agg_dict
        assert "fail_count" in agg_dict
        assert "warn_count" in agg_dict
        assert "pass_count" in agg_dict
        assert "error_count" in agg_dict
        assert "failed_query_indices" in agg_dict
        assert "warned_query_indices" in agg_dict


class TestDualReadReport:
    """测试 --dual-read-report 功能"""
    
    def test_dual_read_with_report(self, primary_backend, shadow_backend):
        """测试 dual_read_report=True 时生成 CompareReport"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
            dual_read_report=True,
            dual_read_report_mode="summary",
        )
        
        # 应同时有 dual_read_stats 和 compare_report
        assert result.dual_read_stats is not None
        assert result.compare_report is not None
        assert result.compare_report.decision is not None
    
    def test_dual_read_without_report(self, primary_backend, shadow_backend):
        """测试 dual_read_report=False 时不生成 CompareReport"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
            dual_read_report=False,
        )
        
        # 应只有 dual_read_stats，无 compare_report
        assert result.dual_read_stats is not None
        assert result.compare_report is None
    
    def test_dual_read_report_detailed_mode(self, primary_backend, shadow_backend):
        """测试 detailed 模式的报告包含 thresholds"""
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
            dual_read_report=True,
            dual_read_report_mode="detailed",
        )
        
        assert result.compare_report is not None
        assert result.compare_report.thresholds is not None
        assert "compare_mode" in result.compare_report.metadata
        assert result.compare_report.metadata["compare_mode"] == "detailed"
    
    def test_dual_read_report_with_custom_thresholds(self, primary_backend, shadow_backend):
        """测试使用自定义阈值的报告"""
        # 让 primary 和 shadow 返回相同的结果
        same_hits = make_query_hits("common", count=3, base_score=0.9)
        primary_backend.set_hits(same_hits)
        shadow_backend.set_hits(same_hits)
        
        custom_thresholds = CompareThresholds(
            hit_overlap_min_warn=0.3,
            hit_overlap_min_fail=0.2,
            score_drift_p95_max=0.5,
            rbo_min_warn=0.0,
            rbo_min_fail=0.0,
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
            dual_read_report=True,
            compare_thresholds=custom_thresholds,
        )
        
        assert result.compare_report is not None
        # 相同结果 + 宽松阈值应该通过
        assert result.compare_report.decision.passed is True


class TestBatchQueryAggregateGate:
    """批量查询聚合门禁测试"""
    
    def test_batch_query_all_pass(self, primary_backend, shadow_backend):
        """测试批量查询全部通过"""
        queries = ["query 1", "query 2", "query 3"]
        
        results = run_batch_query(
            queries=queries,
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
        )
        
        assert len(results) == 3
        
        agg = aggregate_gate_results(results)
        assert agg.passed is True
        assert agg.total_queries == 3
        assert agg.pass_count == 3
    
    def test_batch_query_with_dual_read_report(self, primary_backend, shadow_backend):
        """测试批量查询带 dual_read_report"""
        queries = ["query 1", "query 2"]
        
        results = run_batch_query(
            queries=queries,
            backend=primary_backend,
            shadow_backend=shadow_backend,
            enable_dual_read=True,
            dual_read_report=True,
        )
        
        assert len(results) == 2
        
        # 每个结果都应有 compare_report
        for result in results:
            assert result.compare_report is not None
        
        # 聚合结果应基于 compare_report
        agg = aggregate_gate_results(results)
        assert agg.total_queries == 2


# ============ Validate-Switch 测试 ============


class TestValidateSwitch:
    """
    validate-switch 模式单元测试
    
    覆盖范围:
    1. active_collection 读取失败
    2. 候选 collection 不可用
    3. compare fail/warn/pass 三类路径
    4. 激活逻辑
    """
    
    def test_aggregate_compare_reports_all_pass(self):
        """测试聚合报告 - 全部通过"""
        from step3_seekdb_rag_hybrid.seek_indexer import aggregate_compare_reports
        from step3_seekdb_rag_hybrid.dual_read_compare import (
            CompareReport,
            CompareDecision,
        )
        
        reports = [
            CompareReport(
                request_id="r1",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=False,
                    reason="All checks passed",
                ),
            ),
            CompareReport(
                request_id="r2",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=False,
                    reason="All checks passed",
                ),
            ),
        ]
        
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        assert gate_result == "pass"
        assert "2/2" in gate_reason
        assert len(violations) == 0
    
    def test_aggregate_compare_reports_with_warnings(self):
        """测试聚合报告 - 有警告"""
        from step3_seekdb_rag_hybrid.seek_indexer import aggregate_compare_reports
        from step3_seekdb_rag_hybrid.dual_read_compare import (
            CompareReport,
            CompareDecision,
        )
        
        reports = [
            CompareReport(
                request_id="r1",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=True,
                    reason="Warning on overlap",
                    violated_checks=["hit_overlap_below_warn"],
                ),
            ),
            CompareReport(
                request_id="r2",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=False,
                    reason="All checks passed",
                ),
            ),
        ]
        
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        assert gate_result == "warn"
        assert "1/2" in gate_reason
        assert len(violations) == 1
        assert "hit_overlap_below_warn" in violations[0]
    
    def test_aggregate_compare_reports_with_fail(self):
        """测试聚合报告 - 有失败"""
        from step3_seekdb_rag_hybrid.seek_indexer import aggregate_compare_reports
        from step3_seekdb_rag_hybrid.dual_read_compare import (
            CompareReport,
            CompareDecision,
        )
        
        reports = [
            CompareReport(
                request_id="r1",
                decision=CompareDecision(
                    passed=False,
                    has_warnings=False,
                    reason="Overlap too low",
                    violated_checks=["hit_overlap_below_fail", "rbo_below_fail"],
                ),
            ),
            CompareReport(
                request_id="r2",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=False,
                    reason="All checks passed",
                ),
            ),
        ]
        
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        assert gate_result == "fail"
        assert "1/2" in gate_reason
        assert len(violations) == 2
    
    def test_aggregate_compare_reports_empty(self):
        """测试聚合报告 - 空列表"""
        from step3_seekdb_rag_hybrid.seek_indexer import aggregate_compare_reports
        
        gate_result, gate_reason, violations = aggregate_compare_reports([])
        
        assert gate_result == "pass"
        assert "无查询" in gate_reason
        assert len(violations) == 0


class TestValidateSwitchResult:
    """ValidateSwitchResult 数据结构测试"""
    
    def test_to_dict_basic(self):
        """测试 to_dict 基本输出"""
        from step3_seekdb_rag_hybrid.seek_indexer import ValidateSwitchResult
        
        result = ValidateSwitchResult(
            backend_name="pgvector",
            project_key="webapp",
            candidate_collection="webapp:v1:bge-m3:20260128",
            current_active_collection="webapp:v1:bge-m3:20260115",
            candidate_available=True,
            gate_result="pass",
            gate_reason="All checks passed",
            activate_requested=True,
            activated=True,
        )
        
        d = result.to_dict()
        
        assert d["success"] is True
        assert d["backend_name"] == "pgvector"
        assert d["candidate_collection"] == "webapp:v1:bge-m3:20260128"
        assert d["current_state"]["active_collection"] == "webapp:v1:bge-m3:20260115"
        assert d["gate"]["result"] == "pass"
        assert d["activation"]["activated"] is True
    
    def test_to_dict_with_errors(self):
        """测试 to_dict 带错误信息"""
        from step3_seekdb_rag_hybrid.seek_indexer import ValidateSwitchResult
        
        result = ValidateSwitchResult(
            backend_name="pgvector",
            candidate_collection="test:v1:bge-m3",
            current_active_read_error="Connection refused",
            candidate_available=False,
            candidate_validation_error="Backend unavailable",
            gate_result="fail",
            gate_reason="Candidate validation failed",
            gate_violations=["candidate_unavailable"],
        )
        
        d = result.to_dict()
        
        assert d["success"] is False
        assert d["current_state"]["read_error"] == "Connection refused"
        assert d["candidate_validation"]["available"] is False
        assert d["gate"]["result"] == "fail"
        assert "candidate_unavailable" in d["gate"]["violations"]


class TestValidateSwitchIntegration:
    """
    validate-switch 集成测试
    
    使用 Mock 模拟各种场景。
    """
    
    def test_active_collection_read_failure(self, monkeypatch):
        """测试 active_collection 读取失败场景"""
        from step3_seekdb_rag_hybrid.seek_indexer import (
            run_validate_switch,
            validate_collection,
            get_active_collection,
        )
        import step3_seekdb_rag_hybrid.seek_indexer as indexer_module
        from step3_seekdb_rag_hybrid.seek_indexer import CollectionValidationResult
        
        # Mock get_active_collection 抛出异常
        # 注意：需要 patch seek_indexer 模块中导入的函数
        def mock_get_active_collection(*args, **kwargs):
            raise RuntimeError("Database connection failed")
        
        monkeypatch.setattr(indexer_module, "get_active_collection", mock_get_active_collection)
        
        # Mock validate_collection 返回可用
        def mock_validate_collection(*args, **kwargs):
            return CollectionValidationResult(
                collection_id="test:v1:bge-m3",
                backend_name="pgvector",
                valid=True,
                available=True,
                preflight_passed=True,
                backend_healthy=True,
            )
        
        monkeypatch.setattr(indexer_module, "validate_collection", mock_validate_collection)
        
        # 创建一个 mock connection
        class MockConnection:
            def commit(self): pass
            def rollback(self): pass
        
        result = run_validate_switch(
            conn=MockConnection(),
            candidate_collection="test:v1:bge-m3",
            backend_name="pgvector",
            test_queries=None,  # 无测试查询，跳过比较
            activate=False,
        )
        
        # 应该记录读取错误但继续执行
        assert result.current_active_read_error is not None
        assert "Database connection failed" in result.current_active_read_error
        # 候选 collection 验证应通过
        assert result.candidate_available is True
        # 无测试查询时，门禁应通过
        assert result.gate_result == "pass"
    
    def test_candidate_collection_unavailable(self, monkeypatch):
        """测试候选 collection 不可用场景"""
        from step3_seekdb_rag_hybrid.seek_indexer import (
            run_validate_switch,
            CollectionValidationResult,
        )
        import step3_seekdb_rag_hybrid.seek_indexer as indexer_module
        
        # Mock get_active_collection 正常返回
        def mock_get_active_collection(*args, **kwargs):
            return "current:v1:bge-m3"
        
        monkeypatch.setattr(indexer_module, "get_active_collection", mock_get_active_collection)
        
        # Mock validate_collection 返回不可用
        def mock_validate_collection(*args, **kwargs):
            return CollectionValidationResult(
                collection_id="bad:v1:bge-m3",
                backend_name="pgvector",
                valid=False,
                available=False,
                preflight_passed=False,
                backend_healthy=False,
                preflight_errors=["Table does not exist"],
                recommendations=["Create the collection first"],
            )
        
        monkeypatch.setattr(indexer_module, "validate_collection", mock_validate_collection)
        
        class MockConnection:
            def commit(self): pass
            def rollback(self): pass
        
        result = run_validate_switch(
            conn=MockConnection(),
            candidate_collection="bad:v1:bge-m3",
            backend_name="pgvector",
        )
        
        # 候选 collection 不可用
        assert result.candidate_available is False
        assert result.candidate_validation_error is not None
        # 门禁应失败
        assert result.gate_result == "fail"
        assert "candidate_unavailable" in result.gate_violations
    
    def test_gate_pass_with_activate(self, monkeypatch):
        """测试门禁通过时的激活逻辑"""
        from step3_seekdb_rag_hybrid.seek_indexer import (
            run_validate_switch,
            CollectionValidationResult,
        )
        import step3_seekdb_rag_hybrid.seek_indexer as indexer_module
        
        # 启用 STEP3_ALLOW_ACTIVE_COLLECTION_SWITCH
        monkeypatch.setattr(indexer_module, "ALLOW_ACTIVE_COLLECTION_SWITCH", True)
        
        # Mock get_active_collection
        def mock_get_active_collection(*args, **kwargs):
            return "old:v1:bge-m3"
        
        monkeypatch.setattr(indexer_module, "get_active_collection", mock_get_active_collection)
        
        # Mock validate_collection
        def mock_validate_collection(*args, **kwargs):
            return CollectionValidationResult(
                collection_id="new:v1:bge-m3",
                backend_name="pgvector",
                valid=True,
                available=True,
                preflight_passed=True,
                backend_healthy=True,
            )
        
        monkeypatch.setattr(indexer_module, "validate_collection", mock_validate_collection)
        
        # 追踪 set_active_collection 调用
        set_active_calls = []
        
        def mock_set_active_collection(conn, backend_name, collection_id, project_key=None):
            set_active_calls.append((backend_name, collection_id, project_key))
        
        monkeypatch.setattr(indexer_module, "set_active_collection", mock_set_active_collection)
        
        class MockConnection:
            def commit(self): pass
            def rollback(self): pass
        
        result = run_validate_switch(
            conn=MockConnection(),
            candidate_collection="new:v1:bge-m3",
            backend_name="pgvector",
            project_key="webapp",
            test_queries=None,  # 无测试查询
            activate=True,
            dry_run=False,
        )
        
        # 门禁应通过
        assert result.gate_result == "pass"
        # 应该调用了 set_active_collection
        assert len(set_active_calls) == 1
        assert set_active_calls[0] == ("pgvector", "new:v1:bge-m3", "webapp")
        # 应该标记为已激活
        assert result.activated is True
    
    def test_gate_fail_skips_activate(self, monkeypatch):
        """测试门禁失败时跳过激活"""
        from step3_seekdb_rag_hybrid.seek_indexer import (
            run_validate_switch,
            CollectionValidationResult,
        )
        import step3_seekdb_rag_hybrid.seek_indexer as indexer_module
        from step3_seekdb_rag_hybrid.dual_read_compare import (
            CompareReport,
            CompareDecision,
        )
        
        # Mock get_active_collection
        monkeypatch.setattr(
            indexer_module, 
            "get_active_collection", 
            lambda *args, **kwargs: "current:v1:bge-m3"
        )
        
        # Mock validate_collection
        def mock_validate_collection(*args, **kwargs):
            return CollectionValidationResult(
                collection_id="new:v1:bge-m3",
                backend_name="pgvector",
                valid=True,
                available=True,
                preflight_passed=True,
                backend_healthy=True,
            )
        
        monkeypatch.setattr(indexer_module, "validate_collection", mock_validate_collection)
        
        # Mock run_batch_query 返回失败的比较报告
        def mock_run_batch_query(*args, **kwargs):
            from step3_seekdb_rag_hybrid.seek_query import QueryResult
            return [
                QueryResult(
                    query="test query",
                    compare_report=CompareReport(
                        request_id="r1",
                        decision=CompareDecision(
                            passed=False,
                            reason="Overlap too low",
                            violated_checks=["hit_overlap_below_fail"],
                        ),
                    ),
                ),
            ]
        
        # Mock seek_query 模块
        import step3_seekdb_rag_hybrid.seek_query as seek_query_module
        monkeypatch.setattr(seek_query_module, "run_batch_query", mock_run_batch_query)
        
        # Mock backend 创建
        def mock_create_backend(*args, **kwargs):
            return MockBackend(name="mock")
        
        from step3_seekdb_rag_hybrid import step3_backend_factory
        monkeypatch.setattr(step3_backend_factory, "create_backend_from_env", mock_create_backend)
        
        # Mock embedding provider
        monkeypatch.setattr(indexer_module, "get_embedding_provider_instance", lambda: None)
        
        # 追踪 set_active_collection 调用
        set_active_calls = []
        monkeypatch.setattr(
            indexer_module,
            "set_active_collection",
            lambda *args, **kwargs: set_active_calls.append(args)
        )
        
        class MockConnection:
            def commit(self): pass
            def rollback(self): pass
        
        result = run_validate_switch(
            conn=MockConnection(),
            candidate_collection="new:v1:bge-m3",
            backend_name="pgvector",
            test_queries=["test query"],
            activate=True,
        )
        
        # 门禁应失败
        assert result.gate_result == "fail"
        # 不应该调用 set_active_collection
        assert len(set_active_calls) == 0
        # 不应该标记为已激活
        assert result.activated is False
        # 应该记录跳过原因
        assert result.activation_error is not None
        assert "门禁未通过" in result.activation_error


class TestValidateSwitchDryRun:
    """validate-switch dry-run 模式测试"""
    
    def test_dry_run_skips_actual_activation(self, monkeypatch):
        """测试 dry-run 模式不实际激活"""
        from step3_seekdb_rag_hybrid.seek_indexer import (
            run_validate_switch,
            CollectionValidationResult,
        )
        import step3_seekdb_rag_hybrid.seek_indexer as indexer_module
        
        monkeypatch.setattr(
            indexer_module,
            "get_active_collection",
            lambda *args, **kwargs: "old:v1:bge-m3"
        )
        
        def mock_validate_collection(*args, **kwargs):
            return CollectionValidationResult(
                collection_id="new:v1:bge-m3",
                backend_name="pgvector",
                valid=True,
                available=True,
                preflight_passed=True,
                backend_healthy=True,
            )
        
        monkeypatch.setattr(indexer_module, "validate_collection", mock_validate_collection)
        
        # 追踪 set_active_collection 调用
        set_active_calls = []
        monkeypatch.setattr(
            indexer_module,
            "set_active_collection",
            lambda *args, **kwargs: set_active_calls.append(args)
        )
        
        class MockConnection:
            def commit(self): pass
            def rollback(self): pass
        
        result = run_validate_switch(
            conn=MockConnection(),
            candidate_collection="new:v1:bge-m3",
            backend_name="pgvector",
            activate=True,
            dry_run=True,  # dry-run 模式
        )
        
        # 门禁应通过
        assert result.gate_result == "pass"
        # dry-run 模式不应该实际调用 set_active_collection
        assert len(set_active_calls) == 0
        # 不应该标记为已激活
        assert result.activated is False


# ============ 超时功能测试 ============


class TestShadowQueryTimeout:
    """
    Shadow 查询超时测试
    
    覆盖范围:
    1. execute_with_timeout 辅助函数
    2. ShadowQueryTimeoutError 异常类
    3. compare 模式下超时处理
    4. fallback 模式下超时处理
    5. 超时触发后 gate 行为
    """
    
    def test_execute_with_timeout_success(self):
        """测试 execute_with_timeout 正常执行"""
        from step3_seekdb_rag_hybrid.seek_query import execute_with_timeout
        
        def fast_func(x, y):
            return x + y
        
        result = execute_with_timeout(fast_func, 1000, 1, 2)
        assert result == 3
    
    def test_execute_with_timeout_with_kwargs(self):
        """测试 execute_with_timeout 支持关键字参数"""
        from step3_seekdb_rag_hybrid.seek_query import execute_with_timeout
        
        def func_with_kwargs(a, b=10):
            return a * b
        
        result = execute_with_timeout(func_with_kwargs, 1000, 5, b=3)
        assert result == 15
    
    def test_execute_with_timeout_raises_on_timeout(self):
        """测试 execute_with_timeout 超时抛出 ShadowQueryTimeoutError"""
        import time
        from step3_seekdb_rag_hybrid.seek_query import (
            execute_with_timeout,
            ShadowQueryTimeoutError,
        )
        
        def slow_func():
            time.sleep(2)  # 休眠 2 秒
            return "done"
        
        with pytest.raises(ShadowQueryTimeoutError) as exc_info:
            execute_with_timeout(slow_func, 100)  # 100ms 超时
        
        assert exc_info.value.timeout_ms == 100
        assert "超时" in str(exc_info.value)
    
    def test_execute_with_timeout_zero_no_timeout(self):
        """测试 timeout_ms=0 时不设超时限制"""
        from step3_seekdb_rag_hybrid.seek_query import execute_with_timeout
        
        def func():
            return "result"
        
        # timeout_ms <= 0 应直接执行，不使用线程池
        result = execute_with_timeout(func, 0)
        assert result == "result"
        
        result = execute_with_timeout(func, -1)
        assert result == "result"
    
    def test_execute_with_timeout_propagates_exception(self):
        """测试 execute_with_timeout 传播原始异常"""
        from step3_seekdb_rag_hybrid.seek_query import execute_with_timeout
        
        def func_that_raises():
            raise ValueError("test error")
        
        with pytest.raises(ValueError, match="test error"):
            execute_with_timeout(func_that_raises, 1000)
    
    def test_shadow_query_timeout_error_attributes(self):
        """测试 ShadowQueryTimeoutError 属性"""
        from step3_seekdb_rag_hybrid.seek_query import ShadowQueryTimeoutError
        
        error = ShadowQueryTimeoutError(5000, "自定义消息")
        
        assert error.timeout_ms == 5000
        assert error.message == "自定义消息"
        assert "5000" in str(error)
        assert "自定义消息" in str(error)
    
    def test_shadow_timeout_in_compare_mode_returns_primary(
        self, primary_backend, dual_read_config_compare
    ):
        """测试 compare 模式下 shadow 超时仍返回 primary 结果"""
        import time
        
        # 创建一个会超时的 shadow backend
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.5)  # 休眠 500ms
                return super().query(request)
        
        slow_shadow = SlowBackend(
            name="slow_shadow",
            hits=make_query_hits("shadow", count=3, base_score=0.85),
        )
        
        # 设置非常短的超时时间
        dual_read_config_compare.shadow_timeout_ms = 100  # 100ms
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=slow_shadow,
            dual_read_config=dual_read_config_compare,
        )
        
        # 应返回 primary 结果
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
    
    def test_shadow_timeout_in_run_query_records_error(
        self, primary_backend
    ):
        """测试 run_query 中 shadow 超时被正确记录"""
        import time
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.5)  # 休眠 500ms
                return super().query(request)
        
        slow_shadow = SlowBackend(
            name="slow_shadow",
            hits=make_query_hits("shadow", count=3, base_score=0.85),
        )
        
        # 创建带有短超时的 DualReadConfig
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            shadow_timeout_ms=100,  # 100ms 超时
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=slow_shadow,
            dual_read_config=dual_read_config,
            enable_dual_read=True,
        )
        
        # 应返回 primary 结果
        assert result.error is None
        assert len(result.evidences) == 3
        assert result.evidences[0].chunk_id == "primary_0"
        
        # 应记录 shadow 超时错误
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.shadow_error is not None
        assert result.dual_read_stats.shadow_timed_out is True
        assert "超时" in result.dual_read_stats.shadow_error
    
    def test_shadow_timeout_gate_behavior_with_fail_open_true(
        self, primary_backend
    ):
        """测试 shadow 超时时 fail_open=True 的门禁行为"""
        import time
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.5)
                return super().query(request)
        
        slow_shadow = SlowBackend(name="slow_shadow", hits=[])
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            shadow_timeout_ms=100,
            fail_open=True,  # fail_open=True
        )
        
        # 设置严格门禁
        gate_thresholds = DualReadGateThresholds(
            min_overlap=0.5,
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=slow_shadow,
            dual_read_config=dual_read_config,
            enable_dual_read=True,
            dual_read_gate_thresholds=gate_thresholds,
        )
        
        # fail_open=True: shadow 超时不应导致门禁失败（因为 shadow_results 为空，跳过门禁检查）
        assert result.error is None
        assert result.dual_read_stats is not None
        assert result.dual_read_stats.shadow_timed_out is True
    
    def test_shadow_timeout_in_fallback_mode_returns_primary(
        self, primary_backend, dual_read_config_fallback
    ):
        """测试 fallback 模式下 primary 成功时不受 shadow 超时影响"""
        import time
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.5)
                return super().query(request)
        
        slow_shadow = SlowBackend(name="slow_shadow", hits=[])
        
        # fallback 模式下，如果 primary 成功，不会调用 shadow
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=slow_shadow,
            dual_read_config=dual_read_config_fallback,
        )
        
        # primary 成功，应返回 primary 结果
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        # fallback 模式下 shadow 不应被调用（primary 有结果）
        assert slow_shadow.query_count == 0
    
    def test_shadow_timeout_in_fallback_mode_primary_empty(
        self, primary_backend, dual_read_config_fallback
    ):
        """测试 fallback 模式下 primary 空结果、shadow 超时的处理"""
        import time
        
        primary_backend.set_hits([])  # primary 返回空
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.5)
                return super().query(request)
        
        slow_shadow = SlowBackend(
            name="slow_shadow",
            hits=make_query_hits("shadow", count=2, base_score=0.8),
        )
        
        dual_read_config_fallback.shadow_timeout_ms = 100  # 100ms 超时
        
        # primary 为空，fallback 到 shadow，但 shadow 超时
        # 由于使用 query_evidence_dual_read，超时会抛出异常（在 fallback 路径中）
        from step3_seekdb_rag_hybrid.seek_query import ShadowQueryTimeoutError
        
        with pytest.raises(ShadowQueryTimeoutError):
            query_evidence_dual_read(
                query_text="test",
                primary_backend=primary_backend,
                shadow_backend=slow_shadow,
                dual_read_config=dual_read_config_fallback,
            )
    
    def test_dual_read_stats_to_dict_includes_timeout_info(self):
        """测试 DualReadStats.to_dict() 包含超时信息"""
        stats = DualReadStats(
            primary_count=3,
            shadow_count=0,
            shadow_error="Shadow 查询超时 (timeout_ms=100)",
            shadow_timed_out=True,
        )
        
        result = stats.to_dict()
        
        assert result["shadow_error"] is not None
        assert result["shadow_timed_out"] is True
        assert "超时" in result["shadow_error"]
    
    def test_compare_report_with_shadow_timeout(
        self, primary_backend
    ):
        """测试 shadow 超时时的 CompareReport 生成"""
        import time
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.5)
                return super().query(request)
        
        slow_shadow = SlowBackend(name="slow_shadow", hits=[])
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            shadow_timeout_ms=100,
            fail_open=True,
        )
        
        result = run_query(
            query_text="test query",
            backend=primary_backend,
            shadow_backend=slow_shadow,
            enable_compare=True,
            dual_read_config=dual_read_config,
        )
        
        # 应有 compare_report 记录 shadow 失败
        assert result.compare_report is not None
        assert result.compare_report.decision is not None
        # fail_open=True 时，shadow 失败不影响 passed（但有 warning）
        assert result.compare_report.decision.passed is True
        assert result.compare_report.decision.has_warnings is True
        assert "超时" in result.compare_report.decision.reason or "失败" in result.compare_report.decision.reason


class TestTimeoutTruthTable:
    """
    超时真值表测试
    
    ========================================================================
    超时行为真值表
    ========================================================================
    
    +----------+----------+-------------+-----------------+-------------------+
    | 模式     | primary  | shadow      | fail_open       | 返回结果          |
    +----------+----------+-------------+-----------------+-------------------+
    | compare  | 成功     | 超时        | True            | primary + warning |
    | compare  | 成功     | 超时        | False           | primary + fail    |
    | compare  | 失败     | 超时        | -               | 抛出异常          |
    | fallback | 成功     | (不调用)    | -               | primary           |
    | fallback | 空       | 超时        | -               | 抛出超时异常      |
    | fallback | 失败     | 超时        | -               | 抛出超时异常      |
    +----------+----------+-------------+-----------------+-------------------+
    """
    
    def test_compare_primary_success_shadow_timeout_fail_open_true(
        self, primary_backend
    ):
        """compare: primary 成功 + shadow 超时 + fail_open=True → primary + warning"""
        import time
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.3)
                return []
        
        slow_shadow = SlowBackend(name="slow")
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            shadow_timeout_ms=100,
            fail_open=True,
        )
        
        result = run_query(
            query_text="test",
            backend=primary_backend,
            shadow_backend=slow_shadow,
            enable_compare=True,
            dual_read_config=dual_read_config,
        )
        
        assert result.error is None
        assert len(result.evidences) == 3
        assert result.compare_report.decision.passed is True
        assert result.compare_report.decision.has_warnings is True
    
    def test_compare_primary_success_shadow_timeout_fail_open_false(
        self, primary_backend
    ):
        """compare: primary 成功 + shadow 超时 + fail_open=False → primary + fail"""
        import time
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.3)
                return []
        
        slow_shadow = SlowBackend(name="slow")
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            shadow_timeout_ms=100,
            fail_open=False,
        )
        
        result = run_query(
            query_text="test",
            backend=primary_backend,
            shadow_backend=slow_shadow,
            enable_compare=True,
            dual_read_config=dual_read_config,
        )
        
        # primary 结果应返回
        assert result.error is None
        assert len(result.evidences) == 3
        # 但 compare_report 应标记失败
        assert result.compare_report.decision.passed is False
        assert "shadow_query_failed" in result.compare_report.decision.violated_checks
    
    def test_compare_primary_fail_shadow_timeout_raises(self, shadow_backend):
        """compare: primary 失败 + shadow 超时 → 抛出 primary 异常"""
        import time
        
        failing_primary = MockBackend(name="failing")
        failing_primary.set_exception(RuntimeError("Primary failed"))
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.3)
                return []
        
        slow_shadow = SlowBackend(name="slow")
        
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            shadow_timeout_ms=100,
        )
        
        # 两者都失败，应抛出 primary 异常
        with pytest.raises(RuntimeError, match="Primary failed"):
            query_evidence_dual_read(
                query_text="test",
                primary_backend=failing_primary,
                shadow_backend=slow_shadow,
                dual_read_config=dual_read_config,
            )
    
    def test_fallback_primary_success_shadow_not_called(
        self, primary_backend, shadow_backend, dual_read_config_fallback
    ):
        """fallback: primary 成功 → 不调用 shadow"""
        dual_read_config_fallback.shadow_timeout_ms = 100
        
        results = query_evidence_dual_read(
            query_text="test",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_fallback,
        )
        
        assert len(results) == 3
        assert results[0].chunk_id == "primary_0"
        assert shadow_backend.query_count == 0
    
    def test_fallback_primary_empty_shadow_timeout_raises(
        self, dual_read_config_fallback
    ):
        """fallback: primary 空 + shadow 超时 → 抛出超时异常"""
        import time
        from step3_seekdb_rag_hybrid.seek_query import ShadowQueryTimeoutError
        
        empty_primary = MockBackend(name="empty", hits=[])
        
        class SlowBackend(MockBackend):
            def query(self, request):
                time.sleep(0.3)
                return []
        
        slow_shadow = SlowBackend(name="slow")
        dual_read_config_fallback.shadow_timeout_ms = 100
        
        with pytest.raises(ShadowQueryTimeoutError):
            query_evidence_dual_read(
                query_text="test",
                primary_backend=empty_primary,
                shadow_backend=slow_shadow,
                dual_read_config=dual_read_config_fallback,
            )


class TestPGVectorStatementTimeout:
    """
    PGVectorBackend statement_timeout 测试
    
    注意：这些测试需要模拟数据库行为，因为实际数据库不可用。
    """
    
    def test_query_method_accepts_timeout_parameter(self):
        """测试 query 方法接受 statement_timeout_ms 参数"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import PGVectorBackend
        import inspect
        
        # 检查 query 方法签名包含 statement_timeout_ms 参数
        sig = inspect.signature(PGVectorBackend.query)
        param_names = list(sig.parameters.keys())
        
        assert "statement_timeout_ms" in param_names
    
    def test_pgvector_error_contains_timeout_details(self):
        """测试 PGVectorError 包含超时详情"""
        from step3_seekdb_rag_hybrid.index_backend.pgvector_backend import PGVectorError
        
        error = PGVectorError(
            "查询超时",
            details={
                "error_type": "QueryTimeoutError",
                "timeout_ms": 5000,
            }
        )
        
        assert error.details["error_type"] == "QueryTimeoutError"
        assert error.details["timeout_ms"] == 5000


class TestCompareThresholdsDeprecatedEnvAliases:
    """
    测试 CompareThresholds.from_env() 对废弃环境变量别名的支持
    
    验证旧的环境变量名（如 STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN）
    能正确映射到 canonical 名称并读取值。
    """
    
    def test_deprecated_hit_overlap_min_warn_alias(self):
        """测试废弃的 HIT_OVERLAP_MIN_WARN 别名"""
        import os
        import warnings
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        # 保存原始环境变量
        old_canonical = os.environ.pop("STEP3_DUAL_READ_OVERLAP_MIN_WARN", None)
        old_deprecated = os.environ.pop("STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN", None)
        
        try:
            # 设置废弃的环境变量
            os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"] = "0.85"
            
            # 捕获废弃警告
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                thresholds = CompareThresholds.from_env()
            
            # 验证值被正确读取
            assert thresholds.hit_overlap_min_warn == 0.85, \
                f"期望 0.85，实际 {thresholds.hit_overlap_min_warn}"
            
            # 验证 source.env_keys_used 记录了使用的键
            assert thresholds.source is not None
            assert "STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN" in thresholds.source.env_keys_used, \
                f"env_keys_used 应包含废弃键，实际: {thresholds.source.env_keys_used}"
            
            # 验证触发了废弃警告
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1, \
                "应触发至少一个 DeprecationWarning"
            assert "STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN" in str(deprecation_warnings[0].message)
            
        finally:
            # 恢复原始环境变量
            os.environ.pop("STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN", None)
            if old_canonical is not None:
                os.environ["STEP3_DUAL_READ_OVERLAP_MIN_WARN"] = old_canonical
            if old_deprecated is not None:
                os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"] = old_deprecated
    
    def test_deprecated_hit_overlap_min_fail_alias(self):
        """测试废弃的 HIT_OVERLAP_MIN_FAIL 别名"""
        import os
        import warnings
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        # 保存原始环境变量
        old_canonical = os.environ.pop("STEP3_DUAL_READ_OVERLAP_MIN_FAIL", None)
        old_deprecated = os.environ.pop("STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL", None)
        
        try:
            # 设置废弃的环境变量
            os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL"] = "0.45"
            
            # 捕获废弃警告
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                thresholds = CompareThresholds.from_env()
            
            # 验证值被正确读取
            assert thresholds.hit_overlap_min_fail == 0.45, \
                f"期望 0.45，实际 {thresholds.hit_overlap_min_fail}"
            
            # 验证 source.env_keys_used 记录了使用的键
            assert thresholds.source is not None
            assert "STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL" in thresholds.source.env_keys_used, \
                f"env_keys_used 应包含废弃键，实际: {thresholds.source.env_keys_used}"
            
        finally:
            # 恢复原始环境变量
            os.environ.pop("STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL", None)
            if old_canonical is not None:
                os.environ["STEP3_DUAL_READ_OVERLAP_MIN_FAIL"] = old_canonical
            if old_deprecated is not None:
                os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL"] = old_deprecated
    
    def test_canonical_takes_precedence_over_deprecated(self):
        """测试 canonical 环境变量优先于废弃别名"""
        import os
        import warnings
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        # 保存原始环境变量
        old_canonical = os.environ.pop("STEP3_DUAL_READ_OVERLAP_MIN_WARN", None)
        old_deprecated = os.environ.pop("STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN", None)
        
        try:
            # 同时设置 canonical 和 deprecated
            os.environ["STEP3_DUAL_READ_OVERLAP_MIN_WARN"] = "0.90"
            os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"] = "0.75"
            
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                thresholds = CompareThresholds.from_env()
            
            # canonical 应优先
            assert thresholds.hit_overlap_min_warn == 0.90, \
                f"canonical 应优先，期望 0.90，实际 {thresholds.hit_overlap_min_warn}"
            
            # env_keys_used 应只包含 canonical 键
            assert "STEP3_DUAL_READ_OVERLAP_MIN_WARN" in thresholds.source.env_keys_used
            assert "STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN" not in thresholds.source.env_keys_used
            
        finally:
            # 恢复原始环境变量
            os.environ.pop("STEP3_DUAL_READ_OVERLAP_MIN_WARN", None)
            os.environ.pop("STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN", None)
            if old_canonical is not None:
                os.environ["STEP3_DUAL_READ_OVERLAP_MIN_WARN"] = old_canonical
            if old_deprecated is not None:
                os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"] = old_deprecated
    
    def test_both_deprecated_aliases_work_together(self):
        """测试同时使用两个废弃别名"""
        import os
        import warnings
        from step3_seekdb_rag_hybrid.dual_read_compare import CompareThresholds
        
        # 保存原始环境变量
        keys_to_save = [
            "STEP3_DUAL_READ_OVERLAP_MIN_WARN",
            "STEP3_DUAL_READ_OVERLAP_MIN_FAIL",
            "STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN",
            "STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL",
        ]
        saved = {k: os.environ.pop(k, None) for k in keys_to_save}
        
        try:
            # 设置两个废弃的环境变量
            os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN"] = "0.88"
            os.environ["STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL"] = "0.55"
            
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                thresholds = CompareThresholds.from_env()
            
            # 验证两个值都被正确读取
            assert thresholds.hit_overlap_min_warn == 0.88
            assert thresholds.hit_overlap_min_fail == 0.55
            
            # 验证 env_keys_used 包含两个废弃键
            assert "STEP3_DUAL_READ_HIT_OVERLAP_MIN_WARN" in thresholds.source.env_keys_used
            assert "STEP3_DUAL_READ_HIT_OVERLAP_MIN_FAIL" in thresholds.source.env_keys_used
            
        finally:
            # 恢复原始环境变量
            for k in keys_to_save:
                os.environ.pop(k, None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
