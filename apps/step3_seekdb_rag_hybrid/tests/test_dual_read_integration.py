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
)
from dual_read_compare import (
    CompareThresholds,
    CompareReport,
    ScoreDriftMetrics,
    RankingDriftMetrics,
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


# ============ 主入口 ============


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
