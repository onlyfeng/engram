#!/usr/bin/env python3
"""
test_dual_read_integration.py - 双读功能集成测试

测试环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

测试内容:
- 使用真实 pgvector 后端测试双读功能
- step3_test schema 下隔离表测试
- QueryRequest 构造与双读入口运行
- 结果与诊断字段断言
- DualReadStats 与 CompareReport 完整性验证
- 双读策略（compare/fallback/shadow_only）测试
- CI pgvector 容器兼容性

运行方式:
    cd apps/step3_seekdb_rag_hybrid
    TEST_PGVECTOR_DSN=postgresql://... pytest tests/test_dual_read_integration.py -v
"""

import os
import pytest
import logging
from typing import List, Optional
from dataclasses import dataclass

# 路径配置在 conftest.py 中完成
from index_backend.pgvector_backend import (
    PGVectorBackend,
    HybridSearchConfig,
)
from index_backend.pgvector_collection_strategy import (
    DefaultCollectionStrategy,
    SharedTableStrategy,
)
from index_backend.types import ChunkDoc, QueryRequest, QueryHit
from step3_backend_factory import (
    DualReadConfig,
    PGVectorConfig,
    COLLECTION_STRATEGY_PER_TABLE,
    COLLECTION_STRATEGY_SINGLE_TABLE,
    DUAL_READ_STRATEGY_COMPARE,
    DUAL_READ_STRATEGY_FALLBACK,
    DUAL_READ_STRATEGY_SHADOW_ONLY,
)
from seek_query import (
    query_evidence,
    query_evidence_dual_read,
    run_query,
    run_batch_query,
    compute_dual_read_stats,
    generate_compare_report,
    EvidenceResult,
    QueryFilters,
    DualReadStats,
    set_index_backend,
    set_shadow_backend,
    set_dual_read_config,
    get_index_backend,
    get_shadow_backend,
    execute_with_timeout,
    ShadowQueryTimeoutError,
)
from dual_read_compare import (
    CompareThresholds,
    CompareReport,
    CompareDecision,
    ScoreDriftMetrics,
    RankingDriftMetrics,
)
from seek_indexer import (
    aggregate_compare_reports,
    validate_collection,
    ValidateSwitchResult,
)

# 配置日志
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# ============ 环境变量与测试配置 ============

TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")

# 使用专用的测试 schema 和表（隔离测试数据）
TEST_SCHEMA = "step3_test"
TEST_TABLE_PRIMARY = "chunks_dual_read_primary"
TEST_TABLE_SHADOW = "chunks_dual_read_shadow"
TEST_COLLECTION_ID = "dual_read_test:v1:mock"

# 测试向量维度（使用较小的维度加速测试）
TEST_VECTOR_DIM = 128

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过双读集成测试"
)


# ============ Mock Embedding Provider ============


class MockEmbeddingProvider:
    """
    简单的 Embedding Mock Provider
    
    生成确定性向量，便于测试结果可重复验证。
    """
    
    def __init__(self, dim: int = TEST_VECTOR_DIM):
        self._dim = dim
        self._model_id = "mock-embedding-dual-read"
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    @property
    def dim(self) -> int:
        return self._dim
    
    @property
    def normalize(self) -> bool:
        return True
    
    def embed_text(self, text: str) -> List[float]:
        """生成单个文本的确定性向量"""
        return self.embed_texts([text])[0]
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """生成确定性向量"""
        vectors = []
        for text in texts:
            # 使用 hash 生成确定性向量，确保相同文本生成相同向量
            hash_val = abs(hash(text)) % 10000
            vector = [(hash_val + i) % 100 / 100.0 for i in range(self._dim)]
            # 简单归一化
            norm = sum(v * v for v in vector) ** 0.5
            if norm > 0:
                vector = [v / norm for v in vector]
            vectors.append(vector)
        return vectors


# ============ Fixture 定义 ============


@pytest.fixture(scope="module")
def embedding_provider():
    """模块级共享的 Embedding Provider"""
    return MockEmbeddingProvider(dim=TEST_VECTOR_DIM)


@pytest.fixture
def primary_backend(embedding_provider):
    """
    创建 primary 后端（per_table 策略）
    
    使用 DefaultCollectionStrategy，每个 collection 使用独立表。
    """
    if not TEST_PGVECTOR_DSN:
        pytest.skip("TEST_PGVECTOR_DSN 未设置")
    
    backend = PGVectorBackend(
        connection_string=TEST_PGVECTOR_DSN,
        schema=TEST_SCHEMA,
        table_name=TEST_TABLE_PRIMARY,
        embedding_provider=embedding_provider,
        vector_dim=TEST_VECTOR_DIM,
        collection_id=TEST_COLLECTION_ID,
        collection_strategy=DefaultCollectionStrategy(),
    )
    backend.initialize()
    
    yield backend
    
    # 清理测试数据
    try:
        backend.delete_by_filter({})
        backend.close()
    except Exception as e:
        logger.warning(f"清理 primary 后端失败: {e}")


@pytest.fixture
def shadow_backend(embedding_provider):
    """
    创建 shadow 后端（single_table 策略）
    
    使用 SharedTableStrategy，多个 collection 共享同一表，
    通过 collection_id 列区分数据。
    """
    if not TEST_PGVECTOR_DSN:
        pytest.skip("TEST_PGVECTOR_DSN 未设置")
    
    backend = PGVectorBackend(
        connection_string=TEST_PGVECTOR_DSN,
        schema=TEST_SCHEMA,
        table_name=TEST_TABLE_SHADOW,
        embedding_provider=embedding_provider,
        vector_dim=TEST_VECTOR_DIM,
        collection_id=TEST_COLLECTION_ID,
        collection_strategy=SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=TEST_VECTOR_DIM,
        ),
    )
    backend.initialize()
    
    yield backend
    
    # 清理测试数据
    try:
        backend.delete_by_filter({})
        backend.close()
    except Exception as e:
        logger.warning(f"清理 shadow 后端失败: {e}")


@pytest.fixture
def dual_read_config_compare():
    """compare 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_COMPARE,
        shadow_strategy=COLLECTION_STRATEGY_SINGLE_TABLE,
        shadow_table=TEST_TABLE_SHADOW,
        log_diff=True,
        diff_threshold=0.1,
    )


@pytest.fixture
def dual_read_config_fallback():
    """fallback 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_FALLBACK,
        shadow_strategy=COLLECTION_STRATEGY_SINGLE_TABLE,
        shadow_table=TEST_TABLE_SHADOW,
    )


@pytest.fixture
def dual_read_config_shadow_only():
    """shadow_only 模式的双读配置"""
    return DualReadConfig(
        enabled=True,
        strategy=DUAL_READ_STRATEGY_SHADOW_ONLY,
        shadow_strategy=COLLECTION_STRATEGY_SINGLE_TABLE,
        shadow_table=TEST_TABLE_SHADOW,
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


# ============ 辅助函数 ============


def create_test_docs(
    prefix: str = "dual_read_test",
    count: int = 5,
    embedding_provider: Optional[MockEmbeddingProvider] = None,
) -> List[ChunkDoc]:
    """
    创建测试文档
    
    Args:
        prefix: chunk_id 前缀
        count: 文档数量
        embedding_provider: 可选的 embedding provider 用于生成向量
    
    Returns:
        ChunkDoc 列表
    """
    docs = []
    for i in range(count):
        content = f"Test content for dual read integration test. Document index: {i}. " \
                  f"This contains keywords like performance optimization and database query."
        doc = ChunkDoc(
            chunk_id=f"{prefix}_chunk_{i}",
            content=content,
            project_key="dual_read_test_project",
            module=f"test/module_{i % 3}",
            source_type="test",
            source_id=f"test_source:{i}",
            owner_user_id="test_user",
            artifact_uri=f"memory://{prefix}_chunk_{i}",
            sha256=f"sha256_{prefix}_{i}",
            chunk_idx=i,
            excerpt=content[:100],
            collection_id=TEST_COLLECTION_ID,
        )
        docs.append(doc)
    
    # 生成向量
    if embedding_provider:
        texts = [doc.content for doc in docs]
        vectors = embedding_provider.embed_texts(texts)
        for doc, vector in zip(docs, vectors):
            doc.vector = vector
    
    return docs


def insert_test_data(
    primary_backend: PGVectorBackend,
    shadow_backend: PGVectorBackend,
    embedding_provider: MockEmbeddingProvider,
    count: int = 5,
) -> List[ChunkDoc]:
    """
    向两个后端插入相同的测试数据
    
    Returns:
        插入的文档列表
    """
    docs = create_test_docs(
        prefix="dual_read_test",
        count=count,
        embedding_provider=embedding_provider,
    )
    
    # 写入 primary
    primary_count = primary_backend.upsert(docs)
    assert primary_count == count, f"Primary upsert 失败: expected {count}, got {primary_count}"
    
    # 写入 shadow
    shadow_count = shadow_backend.upsert(docs)
    assert shadow_count == count, f"Shadow upsert 失败: expected {count}, got {shadow_count}"
    
    logger.info(f"插入 {count} 条测试数据到 primary 和 shadow")
    return docs


# ============ 集成测试类 ============


@skip_no_dsn
class TestDualReadIntegration:
    """双读功能集成测试"""
    
    def test_backend_initialization(self, primary_backend, shadow_backend):
        """测试后端初始化成功"""
        # 验证 primary 后端
        assert primary_backend is not None
        assert primary_backend.backend_name == "pgvector"
        assert primary_backend.table_name == TEST_TABLE_PRIMARY
        assert primary_backend.schema == TEST_SCHEMA
        
        # 验证 shadow 后端
        assert shadow_backend is not None
        assert shadow_backend.backend_name == "pgvector"
        assert shadow_backend.table_name == TEST_TABLE_SHADOW
        
        # 验证策略
        assert isinstance(primary_backend.collection_strategy, DefaultCollectionStrategy)
        assert isinstance(shadow_backend.collection_strategy, SharedTableStrategy)
    
    def test_health_check(self, primary_backend, shadow_backend):
        """测试健康检查"""
        primary_health = primary_backend.health_check()
        shadow_health = shadow_backend.health_check()
        
        assert primary_health["status"] == "healthy"
        assert shadow_health["status"] == "healthy"
        
        # 验证健康检查包含必要字段
        assert "details" in primary_health
        assert "schema" in primary_health["details"]
        assert "table" in primary_health["details"]
    
    def test_insert_and_query_both_backends(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试数据写入和查询两个后端一致性"""
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 构造查询请求
        query_text = "performance optimization database query"
        query_vector = embedding_provider.embed_text(query_text)
        
        request = QueryRequest(
            query_text=query_text,
            query_vector=query_vector,
            top_k=5,
        )
        
        # 查询 primary
        primary_results = primary_backend.query(request)
        assert len(primary_results) > 0, "Primary 应返回结果"
        
        # 查询 shadow
        shadow_results = shadow_backend.query(request)
        assert len(shadow_results) > 0, "Shadow 应返回结果"
        
        # 结果数量应一致
        assert len(primary_results) == len(shadow_results), \
            f"结果数量不一致: primary={len(primary_results)}, shadow={len(shadow_results)}"
        
        # chunk_id 集合应一致
        primary_ids = {r.chunk_id for r in primary_results}
        shadow_ids = {r.chunk_id for r in shadow_results}
        assert primary_ids == shadow_ids, \
            f"chunk_id 集合不一致: only_primary={primary_ids - shadow_ids}, only_shadow={shadow_ids - primary_ids}"


@skip_no_dsn
class TestDualReadQueryFlow:
    """双读查询流程集成测试"""
    
    def test_query_evidence_dual_read_compare_mode(
        self, primary_backend, shadow_backend, embedding_provider, dual_read_config_compare
    ):
        """测试 compare 模式的双读查询"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 设置全局后端
        set_index_backend(primary_backend)
        set_shadow_backend(shadow_backend)
        set_dual_read_config(dual_read_config_compare)
        
        # 执行双读查询
        results = query_evidence_dual_read(
            query_text="performance optimization database",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_compare,
            embedding_provider=embedding_provider,
            top_k=5,
        )
        
        # 验证返回结果
        assert len(results) > 0, "应返回结果"
        assert all(isinstance(r, EvidenceResult) for r in results)
        
        # 验证结果来自 primary（compare 模式返回 primary 结果）
        primary_ids = {doc.chunk_id for doc in docs}
        result_ids = {r.chunk_id for r in results}
        assert result_ids.issubset(primary_ids), "结果应来自测试数据"
    
    def test_query_evidence_dual_read_fallback_mode(
        self, primary_backend, shadow_backend, embedding_provider, dual_read_config_fallback
    ):
        """测试 fallback 模式的双读查询"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行双读查询
        results = query_evidence_dual_read(
            query_text="test content integration",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_fallback,
            embedding_provider=embedding_provider,
            top_k=3,
        )
        
        # 验证返回结果（primary 成功时返回 primary 结果）
        assert len(results) > 0, "应返回结果"
    
    def test_query_evidence_dual_read_shadow_only_mode(
        self, primary_backend, shadow_backend, embedding_provider, dual_read_config_shadow_only
    ):
        """测试 shadow_only 模式的双读查询"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行双读查询
        results = query_evidence_dual_read(
            query_text="database query optimization",
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            dual_read_config=dual_read_config_shadow_only,
            embedding_provider=embedding_provider,
            top_k=3,
        )
        
        # 验证返回结果
        assert len(results) > 0, "应返回结果"


@skip_no_dsn
class TestDualReadStatsIntegration:
    """DualReadStats 集成测试"""
    
    def test_compute_dual_read_stats_real_data(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """使用真实数据测试 DualReadStats 计算"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 执行查询
        query_text = "test content dual read"
        query_vector = embedding_provider.embed_text(query_text)
        request = QueryRequest(
            query_text=query_text,
            query_vector=query_vector,
            top_k=5,
        )
        
        import time
        
        # 查询 primary 并计时
        start = time.time()
        primary_hits = primary_backend.query(request)
        primary_latency_ms = (time.time() - start) * 1000
        
        # 查询 shadow 并计时
        start = time.time()
        shadow_hits = shadow_backend.query(request)
        shadow_latency_ms = (time.time() - start) * 1000
        
        # 转换为 EvidenceResult
        primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
        shadow_results = [EvidenceResult.from_query_hit(h) for h in shadow_hits]
        
        # 计算统计
        stats = compute_dual_read_stats(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_backend=primary_backend,
            shadow_backend=shadow_backend,
            primary_latency_ms=primary_latency_ms,
            shadow_latency_ms=shadow_latency_ms,
        )
        
        # 验证统计字段
        assert stats.primary_count == len(primary_results)
        assert stats.shadow_count == len(shadow_results)
        assert stats.primary_latency_ms > 0
        assert stats.shadow_latency_ms > 0
        
        # 由于写入相同数据，overlap 应该很高
        assert stats.overlap_ratio >= 0.5, \
            f"相同数据的 overlap_ratio 应该较高: {stats.overlap_ratio}"
        
        # 验证健康信息
        assert stats.primary_table == TEST_TABLE_PRIMARY
        assert stats.shadow_table == TEST_TABLE_SHADOW
        
        # 验证 to_dict 输出
        stats_dict = stats.to_dict()
        assert "health" in stats_dict
        assert "metrics" in stats_dict
        assert "latency" in stats_dict
        assert stats_dict["metrics"]["overlap_ratio"] >= 0.5
    
    def test_run_query_with_dual_read_stats(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 run_query 返回 DualReadStats"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行查询并启用双读统计
        result = run_query(
            query_text="performance optimization",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_dual_read=True,
            top_k=3,
        )
        
        # 验证基础结果
        assert result.error is None, f"查询失败: {result.error}"
        assert len(result.evidences) > 0, "应返回结果"
        
        # 验证 dual_read_stats
        assert result.dual_read_stats is not None, "应返回 DualReadStats"
        stats = result.dual_read_stats
        
        assert stats.primary_count > 0
        assert stats.shadow_count > 0
        assert stats.overlap_ratio >= 0, "overlap_ratio 应为非负数"
        
        # 验证时间信息
        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.duration_ms > 0


@skip_no_dsn
class TestCompareReportIntegration:
    """CompareReport 集成测试"""
    
    def test_generate_compare_report_real_data(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """使用真实数据测试 CompareReport 生成"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 执行查询
        query_text = "database query optimization"
        query_vector = embedding_provider.embed_text(query_text)
        request = QueryRequest(
            query_text=query_text,
            query_vector=query_vector,
            top_k=5,
        )
        
        import time
        
        # 查询两个后端
        start = time.time()
        primary_hits = primary_backend.query(request)
        primary_latency_ms = (time.time() - start) * 1000
        
        start = time.time()
        shadow_hits = shadow_backend.query(request)
        shadow_latency_ms = (time.time() - start) * 1000
        
        # 转换为 EvidenceResult
        primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
        shadow_results = [EvidenceResult.from_query_hit(h) for h in shadow_hits]
        
        # 生成比较报告
        report = generate_compare_report(
            primary_results=primary_results,
            shadow_results=shadow_results,
            primary_latency_ms=primary_latency_ms,
            shadow_latency_ms=shadow_latency_ms,
            request_id="test-integration-001",
            compare_mode="detailed",
        )
        
        # 验证报告结构
        assert report.request_id == "test-integration-001"
        assert report.metrics is not None
        assert report.decision is not None
        
        # 验证指标
        metrics = report.metrics
        assert metrics.primary_hit_count == len(primary_results)
        assert metrics.secondary_hit_count == len(shadow_results)
        assert metrics.hit_overlap_ratio >= 0 and metrics.hit_overlap_ratio <= 1
        assert metrics.primary_latency_ms > 0
        assert metrics.secondary_latency_ms > 0
        
        # 验证 score_drift 相关指标（使用 evaluate_with_report 后应该填充这些字段）
        assert metrics.p95_score_diff >= 0, "p95_score_diff 应该被填充"
        assert metrics.avg_score_diff >= 0, "avg_score_diff 应该被填充"
        
        # 验证决策（相同数据应该通过）
        decision = report.decision
        assert decision.passed is True, \
            f"相同数据的比较应该通过: {decision.reason}"
        assert decision.recommendation in ["safe_to_switch", "investigate_required"]
        
        # 验证 to_dict 输出
        report_dict = report.to_dict()
        assert "request_id" in report_dict
        assert "metrics" in report_dict
        assert "decision" in report_dict
        
        # 验证 detailed 模式包含 metadata（score_drift 和 ranking_drift）
        assert "metadata" in report_dict
        assert "score_drift" in report_dict["metadata"], \
            "detailed 模式应包含 score_drift 元数据"
        assert "ranking_drift" in report_dict["metadata"], \
            "detailed 模式应包含 ranking_drift 元数据"
    
    def test_run_query_with_compare_report(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 run_query 返回 CompareReport"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行查询并启用比较
        result = run_query(
            query_text="test content integration",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,
            compare_mode="summary",
            top_k=3,
        )
        
        # 验证基础结果
        assert result.error is None, f"查询失败: {result.error}"
        assert len(result.evidences) > 0, "应返回结果"
        
        # 验证 compare_report
        assert result.compare_report is not None, "应返回 CompareReport"
        report = result.compare_report
        
        assert report.metrics is not None
        assert report.decision is not None
        assert report.decision.passed is True, \
            f"相同数据的比较应该通过: {report.decision.reason}"


@skip_no_dsn
class TestQueryFiltersIntegration:
    """带过滤条件的双读查询测试"""
    
    def test_dual_read_with_filters(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试带过滤条件的双读查询"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 构造过滤条件
        filters = QueryFilters(
            project_key="dual_read_test_project",
            source_type="test",
        )
        
        # 执行查询
        result = run_query(
            query_text="performance optimization",
            filters=filters,
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_dual_read=True,
            top_k=5,
        )
        
        # 验证结果
        assert result.error is None, f"查询失败: {result.error}"
        assert len(result.evidences) > 0, "应返回结果"
        
        # 验证过滤条件生效
        for ev in result.evidences:
            # 验证返回的数据符合过滤条件
            assert ev.source_type == "test" or ev.metadata.get("source_type") == "test"


@skip_no_dsn
class TestDiagnosticFields:
    """诊断字段完整性测试"""
    
    def test_evidence_result_diagnostic_fields(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 EvidenceResult 包含完整诊断字段"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行查询
        result = run_query(
            query_text="test content database",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_dual_read=True,
            top_k=3,
        )
        
        assert len(result.evidences) > 0, "应返回结果"
        
        # 验证第一个结果的诊断字段
        ev = result.evidences[0]
        
        # 必需字段
        assert ev.chunk_id is not None and ev.chunk_id != ""
        assert ev.content is not None and ev.content != ""
        assert ev.relevance_score >= 0 and ev.relevance_score <= 1
        
        # 来源信息
        assert ev.source_type != ""
        assert ev.source_id != ""
        assert ev.artifact_uri != ""
        
        # 验证 to_evidence_dict 输出
        ev_dict = ev.to_evidence_dict(include_content=True)
        assert "chunk_id" in ev_dict
        assert "content" in ev_dict
        assert "relevance_score" in ev_dict
        assert "artifact_uri" in ev_dict
        assert "excerpt" in ev_dict
    
    def test_query_result_diagnostic_fields(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 QueryResult 包含完整诊断字段"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行查询
        result = run_query(
            query_text="optimization database",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,
            compare_mode="detailed",
            top_k=3,
        )
        
        # 验证 QueryResult 字段
        assert result.query == "optimization database"
        assert result.top_k == 3
        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.duration_ms > 0
        
        # 验证 to_dict 输出
        result_dict = result.to_dict(include_compare=True)
        assert result_dict["success"] is True
        assert result_dict["query"] == "optimization database"
        assert result_dict["result_count"] == len(result.evidences)
        assert "timing" in result_dict
        assert "evidences" in result_dict
        assert "compare_report" in result_dict
        
        # 验证 to_evidence_packet 输出
        packet = result.to_evidence_packet(include_compare=True)
        assert "query" in packet
        assert "evidences" in packet
        assert "generated_at" in packet
        assert "compare_report" in packet


@skip_no_dsn
class TestEdgeCasesIntegration:
    """边界情况集成测试"""
    
    def test_empty_query_result(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试空查询结果处理"""
        # 不插入测试数据，直接查询
        # 先清理可能存在的数据
        primary_backend.delete_by_filter({})
        shadow_backend.delete_by_filter({})
        
        result = run_query(
            query_text="completely nonexistent topic xyz123",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_dual_read=True,
            top_k=5,
        )
        
        # 应该成功返回但无结果
        assert result.error is None
        # 注意：即使没有数据，pgvector 也可能返回空结果
        # 关键是不应该报错
        
        # 验证统计正常
        if result.dual_read_stats:
            assert result.dual_read_stats.primary_count >= 0
            assert result.dual_read_stats.shadow_count >= 0
    
    def test_large_result_set(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试较大结果集处理"""
        # 插入较多测试数据
        docs = create_test_docs(
            prefix="large_test",
            count=20,
            embedding_provider=embedding_provider,
        )
        
        primary_backend.upsert(docs)
        shadow_backend.upsert(docs)
        
        try:
            # 查询并请求较多结果
            result = run_query(
                query_text="test content document",
                backend=primary_backend,
                shadow_backend=shadow_backend,
                embedding_provider=embedding_provider,
                enable_dual_read=True,
                top_k=15,
            )
            
            assert result.error is None
            assert len(result.evidences) <= 15
            
            # 验证统计
            if result.dual_read_stats:
                assert result.dual_read_stats.primary_count <= 15
                assert result.dual_read_stats.shadow_count <= 15
        finally:
            # 清理大量数据
            chunk_ids = [doc.chunk_id for doc in docs]
            primary_backend.delete(chunk_ids)
            shadow_backend.delete(chunk_ids)


@skip_no_dsn
class TestTimeoutPathIntegration:
    """超时路径集成测试
    
    使用极低 statement_timeout 验证超时处理路径。
    注意：这些测试依赖数据库端的 statement_timeout 支持。
    """
    
    def test_query_with_statement_timeout(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试使用 statement_timeout 的查询超时行为"""
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 使用极低的 statement_timeout（1ms）测试超时路径
        # 注意：实际上查询可能仍然完成，因为 1ms 可能足够快
        # 这里主要验证超时参数被正确传递
        from index_backend.types import QueryRequest
        
        query_text = "performance optimization"
        query_vector = embedding_provider.embed_text(query_text)
        
        request = QueryRequest(
            query_text=query_text,
            query_vector=query_vector,
            top_k=5,
        )
        
        # 测试 primary backend 的 statement_timeout 参数
        # 使用较长超时确保正常完成
        try:
            results = primary_backend.query(request, statement_timeout_ms=30000)
            # 查询应该完成
            assert isinstance(results, list)
        except Exception as e:
            # 如果超时发生，应该包含 timeout 信息
            error_msg = str(e).lower()
            if 'timeout' in error_msg:
                logger.info(f"查询超时（预期行为）: {e}")
            else:
                raise
    
    def test_shadow_query_timeout_handling(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 shadow 查询超时的处理
        
        当 shadow 查询超时时，应该优雅降级而不影响 primary 结果返回。
        """
        # 插入测试数据
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 导入 ShadowQueryTimeoutError
        from seek_query import ShadowQueryTimeoutError, execute_with_timeout
        
        # 测试 execute_with_timeout 函数的超时行为
        import time
        
        def slow_function():
            time.sleep(0.5)  # 模拟慢操作
            return "completed"
        
        # 使用极短超时测试
        try:
            result = execute_with_timeout(slow_function, timeout_ms=10)
            # 如果 10ms 内完成，说明函数执行很快
            logger.info(f"函数在超时前完成: {result}")
        except ShadowQueryTimeoutError as e:
            # 预期的超时行为
            assert e.timeout_ms == 10
            logger.info(f"超时异常（预期）: timeout_ms={e.timeout_ms}")


@skip_no_dsn
class TestFailOpenBehaviorIntegration:
    """fail_open 行为集成测试
    
    验证 fail_open=false 时 shadow 失败会导致 gate 失败。
    """
    
    def test_fail_open_false_shadow_error_causes_gate_failure(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 fail_open=False 时 shadow 失败导致 gate 失败"""
        # 插入测试数据到 primary
        docs = create_test_docs(
            prefix="fail_open_test",
            count=3,
            embedding_provider=embedding_provider,
        )
        primary_backend.upsert(docs)
        
        # 故意不写入 shadow，但使用 fail_open=False
        # 这样当 shadow 返回空/差异大时，应该导致门禁失败
        
        # 使用 MockFailingShadowBackend 模拟 shadow 失败
        class MockFailingShadowBackend:
            """模拟失败的 shadow 后端"""
            backend_name = "mock_failing"
            table_name = "mock_table"
            
            def query(self, request, **kwargs):
                raise Exception("模拟的 shadow 查询失败")
            
            def health_check(self):
                return {"status": "unhealthy", "backend": "mock_failing"}
        
        failing_shadow = MockFailingShadowBackend()
        
        # 创建 fail_open=False 的配置
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=False,  # 关键：shadow 失败时门禁应该失败
        )
        
        # 设置全局后端
        set_index_backend(primary_backend)
        set_shadow_backend(failing_shadow)
        set_dual_read_config(dual_read_config)
        
        # 执行查询
        result = run_query(
            query_text="test content",
            backend=primary_backend,
            shadow_backend=failing_shadow,
            dual_read_config=dual_read_config,
            embedding_provider=embedding_provider,
            enable_compare=True,
            top_k=3,
        )
        
        # 验证：primary 结果应该存在
        assert len(result.evidences) > 0, "primary 应返回结果"
        
        # 验证：compare_report 应该显示失败
        if result.compare_report is not None:
            # fail_open=False 时，shadow 失败应导致门禁失败
            assert result.compare_report.decision is not None
            assert result.compare_report.decision.passed is False, \
                "fail_open=False 时 shadow 失败应导致门禁失败"
            assert "fail_open=False" in result.compare_report.decision.reason
        
        # 清理
        primary_backend.delete([doc.chunk_id for doc in docs])
    
    def test_fail_open_true_shadow_error_allows_primary_result(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 fail_open=True 时 shadow 失败不影响 primary 结果"""
        # 插入测试数据到 primary
        docs = create_test_docs(
            prefix="fail_open_true_test",
            count=3,
            embedding_provider=embedding_provider,
        )
        primary_backend.upsert(docs)
        
        # 模拟 shadow 失败
        class MockFailingShadowBackend:
            backend_name = "mock_failing"
            table_name = "mock_table"
            
            def query(self, request, **kwargs):
                raise Exception("模拟的 shadow 查询失败")
            
            def health_check(self):
                return {"status": "unhealthy", "backend": "mock_failing"}
        
        failing_shadow = MockFailingShadowBackend()
        
        # 创建 fail_open=True 的配置（默认值）
        dual_read_config = DualReadConfig(
            enabled=True,
            strategy=DUAL_READ_STRATEGY_COMPARE,
            fail_open=True,  # shadow 失败不影响 primary
        )
        
        # 执行查询
        result = run_query(
            query_text="test content",
            backend=primary_backend,
            shadow_backend=failing_shadow,
            dual_read_config=dual_read_config,
            embedding_provider=embedding_provider,
            enable_compare=True,
            top_k=3,
        )
        
        # 验证：primary 结果应该存在
        assert len(result.evidences) > 0, "primary 应返回结果"
        
        # 验证：fail_open=True 时，门禁应该通过（带警告）
        if result.compare_report is not None:
            assert result.compare_report.decision is not None
            # fail_open=True 时，shadow 失败应该导致通过（带警告）
            assert result.compare_report.decision.passed is True, \
                "fail_open=True 时 shadow 失败应允许通过（带警告）"
        
        # 清理
        primary_backend.delete([doc.chunk_id for doc in docs])


class TestBatchQueryGateAggregationUnit:
    """批量查询门禁聚合单元测试（不需要数据库连接）
    
    验证 aggregate_compare_reports 的聚合逻辑。
    """
    
    def test_batch_query_aggregate_with_failure(self):
        """测试批量查询有失败时的聚合结果"""
        # 手动构造包含失败的报告列表
        reports = [
            # 通过的报告
            CompareReport(
                request_id="test-1",
                decision=CompareDecision(
                    passed=True,
                    recommendation="safe_to_switch",
                    violated_checks=[],
                    reason="all checks passed",
                ),
            ),
            # 失败的报告
            CompareReport(
                request_id="test-2",
                decision=CompareDecision(
                    passed=False,
                    recommendation="investigate_required",
                    violated_checks=["hit_overlap_below_threshold"],
                    reason="hit overlap too low",
                ),
            ),
            # 警告的报告
            CompareReport(
                request_id="test-3",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=True,
                    recommendation="safe_with_warnings",
                    violated_checks=["rbo_below_warn"],
                    reason="RBO below warning threshold",
                ),
            ),
        ]
        
        # 聚合门禁判定
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        # 有失败时应该返回 fail
        assert gate_result == "fail", f"有失败时应返回 fail: {gate_reason}"
        assert "1/3" in gate_reason  # 1 个失败
        assert len(violations) > 0
        assert any("hit_overlap" in v for v in violations)
    
    def test_batch_query_aggregate_empty_reports(self):
        """测试空报告列表的聚合结果"""
        gate_result, gate_reason, violations = aggregate_compare_reports([])
        
        assert gate_result == "pass"
        assert "无查询需要验证" in gate_reason
        assert len(violations) == 0
    
    def test_batch_query_aggregate_only_warnings(self):
        """测试仅有警告时的聚合结果"""
        reports = [
            CompareReport(
                request_id="test-1",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=True,
                    recommendation="safe_with_warnings",
                    violated_checks=["latency_ratio_warn"],
                    reason="latency ratio above warning",
                ),
            ),
            CompareReport(
                request_id="test-2",
                decision=CompareDecision(
                    passed=True,
                    has_warnings=True,
                    recommendation="safe_with_warnings",
                    violated_checks=["score_drift_warn"],
                    reason="score drift above warning",
                ),
            ),
        ]
        
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        # 仅有警告时应该返回 warn
        assert gate_result == "warn", f"仅有警告时应返回 warn: {gate_reason}"
        assert "警告" in gate_reason or "warn" in gate_reason.lower()
        assert len(violations) == 2
    
    def test_batch_query_aggregate_all_pass(self):
        """测试全部通过时的聚合结果"""
        reports = [
            CompareReport(
                request_id="test-1",
                decision=CompareDecision(
                    passed=True,
                    recommendation="safe_to_switch",
                    violated_checks=[],
                    reason="all checks passed",
                ),
            ),
            CompareReport(
                request_id="test-2",
                decision=CompareDecision(
                    passed=True,
                    recommendation="safe_to_switch",
                    violated_checks=[],
                    reason="all checks passed",
                ),
            ),
        ]
        
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        assert gate_result == "pass", f"全部通过时应返回 pass: {gate_reason}"
        assert "2/2" in gate_reason
        assert len(violations) == 0


@skip_no_dsn
class TestBatchQueryGateAggregation:
    """批量查询门禁聚合集成测试（需要数据库连接）
    
    验证 run_batch_query 配合 aggregate_compare_reports 的实际聚合行为。
    """
    
    def test_batch_query_aggregate_all_pass(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试批量查询全部通过时的聚合结果"""
        # 插入相同数据到两个后端
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 执行批量查询
        queries = [
            "performance optimization",
            "database query",
            "test content",
        ]
        
        results = run_batch_query(
            queries=queries,
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,
            compare_mode="summary",
            top_k=5,
        )
        
        assert len(results) == len(queries)
        
        # 收集 compare_reports
        reports = [r.compare_report for r in results if r.compare_report is not None]
        
        if reports:
            # 聚合门禁判定
            gate_result, gate_reason, violations = aggregate_compare_reports(reports)
            
            # 相同数据应该全部通过
            assert gate_result == "pass", f"相同数据应全部通过: {gate_reason}"
            assert "通过" in gate_reason
            assert len(violations) == 0


@skip_no_dsn
class TestValidateSwitchIntegration:
    """validate-switch 集成测试
    
    测试 validate-switch 模式的核心逻辑，在测试 schema 下执行。
    """
    
    def test_validate_switch_candidate_available(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 validate-switch 验证候选 collection 可用性"""
        from seek_indexer import validate_collection
        
        # 验证 primary_backend 的 collection 是否可用
        # 使用已初始化的后端作为候选
        result = validate_collection(
            conn=None,  # validate_collection 内部会创建后端连接
            collection_id=TEST_COLLECTION_ID,
            backend_name="pgvector",
        )
        
        # 基本检查：验证结果结构正确
        assert hasattr(result, 'collection_id')
        assert hasattr(result, 'valid')
        assert hasattr(result, 'available')
        assert hasattr(result, 'preflight_passed')
        
        # to_dict 输出检查
        result_dict = result.to_dict()
        assert "collection_id" in result_dict
        assert "valid" in result_dict
        assert "preflight" in result_dict
    
    def test_validate_switch_with_test_queries(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 validate-switch 配合测试查询的完整流程
        
        注意：此测试仅验证逻辑流程，不实际执行 KV 写入。
        """
        from seek_indexer import (
            ValidateSwitchResult,
            aggregate_compare_reports,
        )
        from seek_query import run_batch_query
        
        # 插入相同数据到两个后端
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=5
        )
        
        # 模拟 validate-switch 的测试查询流程
        test_queries = ["performance", "database", "test"]
        
        # 执行批量查询比较
        results = run_batch_query(
            queries=test_queries,
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,
            compare_mode="summary",
            top_k=5,
        )
        
        # 收集 compare_reports
        reports = [r.compare_report for r in results if r.compare_report is not None]
        
        # 聚合门禁判定
        gate_result, gate_reason, violations = aggregate_compare_reports(reports)
        
        # 构造 ValidateSwitchResult 验证结构
        vs_result = ValidateSwitchResult(
            backend_name="pgvector",
            candidate_collection=TEST_COLLECTION_ID,
            candidate_available=True,
            queries_tested=len(test_queries),
            compare_reports=reports,
            gate_result=gate_result,
            gate_reason=gate_reason,
            gate_violations=violations,
        )
        
        # 验证结构
        assert vs_result.queries_tested == len(test_queries)
        assert vs_result.gate_result in ("pass", "warn", "fail")
        
        # to_dict 输出检查
        vs_dict = vs_result.to_dict()
        assert "gate" in vs_dict
        assert vs_dict["gate"]["result"] == gate_result
        assert vs_dict["comparison"]["queries_tested"] == len(test_queries)
        
        # 相同数据应该通过门禁
        assert vs_result.gate_result == "pass", \
            f"相同数据的 validate-switch 应通过: {vs_result.gate_reason}"
    
    def test_validate_switch_dry_run_no_activation(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """测试 validate-switch dry-run 模式不执行激活"""
        from seek_indexer import ValidateSwitchResult
        
        # 模拟 dry-run 结果
        vs_result = ValidateSwitchResult(
            backend_name="pgvector",
            candidate_collection="test:v1:mock:new_version",
            candidate_available=True,
            gate_result="pass",
            gate_reason="all checks passed",
            activate_requested=True,  # 请求激活
            activated=False,  # 但因为 dry-run 未激活
        )
        
        # dry-run 时不应该激活
        assert vs_result.activate_requested is True
        assert vs_result.activated is False
        
        # to_dict 正确反映状态
        vs_dict = vs_result.to_dict()
        assert vs_dict["activation"]["requested"] is True
        assert vs_dict["activation"]["activated"] is False


# =============================================================================
# CompareReport.to_dict() 模式字段存在性集成测试
# =============================================================================


@skip_no_dsn
class TestCompareReportToDictModesIntegration:
    """测试 CompareReport.to_dict() 在真实数据下的字段存在性"""
    
    def test_summary_mode_fields_with_real_data(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """summary 模式下的字段存在性（真实数据）"""
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        report = generate_compare_report(
            primary_results=[EvidenceResult.from_query_hit(h) for h in 
                primary_backend.query(QueryRequest(
                    query_text="test",
                    query_vector=embedding_provider.embed_text("test"),
                    top_k=3,
                ))],
            shadow_results=[EvidenceResult.from_query_hit(h) for h in 
                shadow_backend.query(QueryRequest(
                    query_text="test",
                    query_vector=embedding_provider.embed_text("test"),
                    top_k=3,
                ))],
            compare_mode="summary",
            request_id="test-summary-integration",
        )
        
        report_dict = report.to_dict()
        
        # summary 模式必须包含的字段
        assert "request_id" in report_dict
        assert "metrics" in report_dict
        assert "decision" in report_dict
        assert "timestamp" in report_dict
        
        # summary 模式不应包含 thresholds
        assert "thresholds" not in report_dict, \
            "summary 模式不应包含 thresholds"
    
    def test_detailed_mode_fields_with_real_data(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """detailed 模式下的字段存在性（真实数据）"""
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        report = generate_compare_report(
            primary_results=[EvidenceResult.from_query_hit(h) for h in 
                primary_backend.query(QueryRequest(
                    query_text="test",
                    query_vector=embedding_provider.embed_text("test"),
                    top_k=3,
                ))],
            shadow_results=[EvidenceResult.from_query_hit(h) for h in 
                shadow_backend.query(QueryRequest(
                    query_text="test",
                    query_vector=embedding_provider.embed_text("test"),
                    top_k=3,
                ))],
            compare_mode="detailed",
            request_id="test-detailed-integration",
        )
        
        report_dict = report.to_dict()
        
        # detailed 模式应包含 thresholds
        assert "thresholds" in report_dict, \
            "detailed 模式应包含 thresholds"
        
        # detailed 模式 metadata 应包含额外信息
        assert "metadata" in report_dict
        assert "ranking_drift" in report_dict["metadata"]
        assert "score_drift" in report_dict["metadata"]


# =============================================================================
# decision.violation_details 结构集成测试
# =============================================================================


@skip_no_dsn
class TestViolationDetailsStructureIntegration:
    """测试 decision.violation_details 的结构（真实数据）"""
    
    def test_violation_details_fields_with_real_data(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """violation_details 字段结构验证（真实数据，构造差异场景）"""
        # 只在 primary 插入数据，制造差异
        docs = create_test_docs(
            prefix="violation_test",
            count=5,
            embedding_provider=embedding_provider,
        )
        primary_backend.upsert(docs)
        
        # shadow 只插入部分数据
        shadow_backend.upsert(docs[:2])
        
        try:
            # 执行查询
            query_text = "test content"
            query_vector = embedding_provider.embed_text(query_text)
            request = QueryRequest(
                query_text=query_text,
                query_vector=query_vector,
                top_k=5,
            )
            
            primary_hits = primary_backend.query(request)
            shadow_hits = shadow_backend.query(request)
            
            primary_results = [EvidenceResult.from_query_hit(h) for h in primary_hits]
            shadow_results = [EvidenceResult.from_query_hit(h) for h in shadow_hits]
            
            # 使用严格阈值触发违规
            strict_thresholds = CompareThresholds(
                hit_overlap_min_warn=0.95,
                hit_overlap_min_fail=0.9,
            )
            
            report = generate_compare_report(
                primary_results=primary_results,
                shadow_results=shadow_results,
                thresholds=strict_thresholds,
                compare_mode="detailed",
                request_id="test-violation-integration",
            )
            
            # 验证决策结构
            assert report.decision is not None
            decision_dict = report.decision.to_dict()
            
            # 应该有违规（因为数据不完全一致）
            if not report.decision.passed or report.decision.has_warnings:
                assert "violation_details" in decision_dict or \
                       len(report.decision.violation_details) > 0, \
                    "有违规时应包含 violation_details"
                
                # 验证 violation_details 结构
                for v in report.decision.violation_details:
                    v_dict = v.to_dict()
                    assert "check_name" in v_dict
                    assert "actual_value" in v_dict
                    assert "threshold_value" in v_dict
                    assert "level" in v_dict
                    assert "reason" in v_dict
                    assert v_dict["level"] in ("warn", "fail")
        finally:
            # 清理
            primary_backend.delete([doc.chunk_id for doc in docs])
            shadow_backend.delete([doc.chunk_id for doc in docs[:2]])


# =============================================================================
# CompareThresholds.from_env() 环境变量测试
# =============================================================================


class TestCompareThresholdsFromEnvIntegration:
    """测试 CompareThresholds.from_env() 的优先级与容错行为"""
    
    def test_from_env_override_in_real_scenario(self):
        """真实场景中环境变量覆盖测试"""
        # 保存原始值
        env_keys = [
            "STEP3_DUAL_READ_OVERLAP_MIN_WARN",
            "STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX",
        ]
        original_values = {k: os.environ.get(k) for k in env_keys}
        
        try:
            # 设置自定义环境变量
            os.environ["STEP3_DUAL_READ_OVERLAP_MIN_WARN"] = "0.9"
            os.environ["STEP3_DUAL_READ_SCORE_DRIFT_P95_MAX"] = "0.05"
            
            thresholds = CompareThresholds.from_env()
            
            # 验证环境变量被读取
            assert thresholds.hit_overlap_min_warn == 0.9, \
                "环境变量应覆盖默认值"
            assert thresholds.score_drift_p95_max == 0.05, \
                "环境变量应覆盖默认值"
            
            # to_dict 应反映正确值
            thresholds_dict = thresholds.to_dict()
            assert thresholds_dict["hit_overlap_min_warn"] == 0.9
            assert thresholds_dict["score_drift_p95_max"] == 0.05
            
        finally:
            # 恢复原始环境变量
            for key, value in original_values.items():
                if value is not None:
                    os.environ[key] = value
                elif key in os.environ:
                    del os.environ[key]
    
    def test_from_env_invalid_value_fallback(self):
        """无效环境变量值应回退到默认值"""
        key = "STEP3_DUAL_READ_LATENCY_RATIO_MAX"
        original = os.environ.get(key)
        
        try:
            # 设置无效值
            os.environ[key] = "invalid_number"
            
            thresholds = CompareThresholds.from_env()
            
            # 应回退到默认值
            assert thresholds.latency_ratio_max == 2.0, \
                "无效值应回退到默认值 2.0"
            
        finally:
            if original is not None:
                os.environ[key] = original
            elif key in os.environ:
                del os.environ[key]


# =============================================================================
# include_compare 关闭时不输出 compare 字段集成测试
# =============================================================================


@skip_no_dsn
class TestIncludeCompareDisabledIntegration:
    """测试 include_compare 关闭时不输出 compare 字段（真实数据）"""
    
    def test_run_query_without_include_compare(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """run_query 默认不输出 compare_report"""
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        # 执行查询，启用 compare 但输出时不包含
        result = run_query(
            query_text="test content",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,  # 内部会生成 compare_report
            top_k=3,
        )
        
        # compare_report 应该存在于 result 对象
        assert result.compare_report is not None, \
            "enable_compare=True 时应生成 compare_report"
        
        # 但 to_dict(include_compare=False) 不应输出
        result_dict = result.to_dict(include_compare=False)
        assert "compare_report" not in result_dict, \
            "include_compare=False 时不应输出 compare_report"
        
        # 默认 to_dict() 也不应输出
        default_dict = result.to_dict()
        assert "compare_report" not in default_dict, \
            "默认 to_dict() 不应输出 compare_report"
    
    def test_run_query_with_include_compare(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """run_query 显式启用 include_compare 时输出 compare_report"""
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        result = run_query(
            query_text="test content",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,
            top_k=3,
        )
        
        # to_dict(include_compare=True) 应输出
        result_dict = result.to_dict(include_compare=True)
        assert "compare_report" in result_dict, \
            "include_compare=True 时应输出 compare_report"
        
        # 验证 compare_report 结构
        compare_report = result_dict["compare_report"]
        assert "metrics" in compare_report
        assert "decision" in compare_report
    
    def test_evidence_packet_without_include_compare(
        self, primary_backend, shadow_backend, embedding_provider
    ):
        """to_evidence_packet 默认不输出 compare_report"""
        docs = insert_test_data(
            primary_backend, shadow_backend, embedding_provider, count=3
        )
        
        result = run_query(
            query_text="test content",
            backend=primary_backend,
            shadow_backend=shadow_backend,
            embedding_provider=embedding_provider,
            enable_compare=True,
            top_k=3,
        )
        
        # 默认 to_evidence_packet() 不应输出
        packet = result.to_evidence_packet()
        assert "compare_report" not in packet, \
            "默认 to_evidence_packet() 不应输出 compare_report"
        
        # 显式启用时应输出
        packet_with_compare = result.to_evidence_packet(include_compare=True)
        assert "compare_report" in packet_with_compare, \
            "include_compare=True 时 to_evidence_packet() 应输出 compare_report"


# ============ 主入口 ============


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
