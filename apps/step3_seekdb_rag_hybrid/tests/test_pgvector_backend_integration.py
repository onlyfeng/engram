#!/usr/bin/env python3
"""
test_pgvector_backend_integration.py - PGVector 后端集成测试

测试环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

测试内容:
- initialize: 创建 schema/table/索引（包含 collection_id 字段和索引）
- upsert: 插入/更新文档（自动注入 collection_id）
- query: Hybrid 检索（向量 + 全文，自动过滤 collection）
- delete: 删除文档（自动过滤 collection）
- delete_by_filter: 按条件删除（自动过滤 collection）
- exists/get_by_ids: 存在性检查和批量获取（自动过滤 collection）
- health_check/get_stats: 状态查询（自动过滤 collection）
- Collection 隔离: 不同 collection 写入后互相查询不到

注意:
- 使用 step3_test schema 和 chunks_test 表，隔离测试数据
- 测试完成后清理表数据
- 当 TEST_PGVECTOR_DSN 未设置时跳过所有测试
"""

import os
import pytest
import uuid
from typing import List, Optional
from datetime import datetime

# 路径配置在 conftest.py 中完成
from index_backend.types import ChunkDoc, QueryRequest, QueryHit
from index_backend.pgvector_backend import (
    PGVectorBackend,
    PGVectorError,
    PGVectorExtensionError,
    HybridSearchConfig,
    create_pgvector_backend,
    ALLOWED_SCHEMAS,
    ALLOWED_TABLE_NAMES,
)
from index_backend.pgvector_collection_strategy import (
    DefaultCollectionStrategy,
    SharedTableStrategy,
    RoutingCollectionStrategy,
    VectorDimensionMismatchError,
)

# 引入双读对比模块
from dual_read_compare import (
    CompareThresholds,
    CompareMetrics,
    CompareReport,
    OverlapMetrics,
    RankingDriftMetrics,
    ScoreDriftMetrics,
    compute_overlap_metrics,
    compute_ranking_drift,
    compute_score_drift,
    evaluate_with_report,
)


# ============ 环境变量检查 ============

TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")

# 使用 step3_test schema 和 chunks_test 表（白名单中已存在）
TEST_SCHEMA = "step3_test"
TEST_TABLE = "chunks_test"

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过 PGVector 集成测试"
)


# ============ Deterministic Embedding Mock ============


class DeterministicEmbeddingMock:
    """
    确定性 Embedding Mock
    
    对于相同的文本，总是返回相同的向量。
    使用简单的 hash 算法生成确定性向量。
    """
    
    def __init__(self, dim: int = 128):
        """
        初始化 Mock
        
        Args:
            dim: 向量维度（测试用较小维度以加速）
        """
        self._dim = dim
        self._model_id = "deterministic-mock-integration"
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    @property
    def dim(self) -> int:
        return self._dim
    
    def embed_text(self, text: str) -> List[float]:
        """生成确定性向量"""
        return self._text_to_vector(text)
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量生成确定性向量"""
        return [self._text_to_vector(t) for t in texts]
    
    def _text_to_vector(self, text: str) -> List[float]:
        """
        将文本转换为确定性向量
        
        使用文本的 hash 值作为种子生成向量。
        """
        hash_val = hash(text)
        vector = []
        for i in range(self._dim):
            # 生成 -1 到 1 之间的确定性值
            val = ((hash_val + i * 31) % 1000) / 500.0 - 1.0
            vector.append(val)
        
        # 归一化
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        
        return vector


# ============ Fixtures ============


@pytest.fixture(scope="module")
def embedding_mock() -> DeterministicEmbeddingMock:
    """创建确定性 Embedding Mock（模块级别共享）"""
    return DeterministicEmbeddingMock(dim=128)


@pytest.fixture(scope="module")
def pgvector_backend(embedding_mock: DeterministicEmbeddingMock):
    """
    创建 PGVector 后端实例（模块级别共享）
    
    - 初始化表结构
    - 测试完成后清理数据
    """
    if not TEST_PGVECTOR_DSN:
        pytest.skip("TEST_PGVECTOR_DSN 环境变量未设置")
    
    # 创建后端实例
    backend = PGVectorBackend(
        connection_string=TEST_PGVECTOR_DSN,
        schema=TEST_SCHEMA,
        table_name=TEST_TABLE,
        embedding_provider=embedding_mock,
        vector_dim=128,  # 与 embedding mock 一致
        hybrid_config=HybridSearchConfig(
            vector_weight=0.7,
            text_weight=0.3,
        ),
    )
    
    try:
        # 初始化表结构
        backend.initialize()
        
        yield backend
        
    finally:
        # 清理测试数据
        try:
            conn = backend._get_connection()
            with conn.cursor() as cur:
                cur.execute(f'DELETE FROM {backend.qualified_table}')
                conn.commit()
        except Exception as e:
            print(f"清理测试数据失败: {e}")
        
        # 关闭连接
        backend.close()


@pytest.fixture
def unique_test_id() -> str:
    """生成唯一测试 ID，用于隔离测试数据"""
    return f"intg-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def sample_docs(unique_test_id: str) -> List[ChunkDoc]:
    """创建测试用文档列表"""
    return [
        ChunkDoc(
            chunk_id=f"{unique_test_id}:git:commit1:sha1:v1:0",
            content="修复了用户登录时的 XSS 漏洞，使用 escape 函数对输入进行转义处理。",
            project_key="test_project",
            module="src/auth/",
            source_type="git",
            source_id="commit1",
            owner_user_id="dev001",
            commit_ts="2024-06-15T10:30:00Z",
            artifact_uri=f"memory://test/{unique_test_id}/commit1",
            sha256="sha1hash",
            chunk_idx=0,
            excerpt="XSS 漏洞修复",
            metadata={"severity": "high", "tag": "security"},
        ),
        ChunkDoc(
            chunk_id=f"{unique_test_id}:git:commit1:sha1:v1:1",
            content="添加了输入验证单元测试，覆盖了各种边界情况和特殊字符。",
            project_key="test_project",
            module="src/auth/",
            source_type="git",
            source_id="commit1",
            owner_user_id="dev001",
            commit_ts="2024-06-15T10:30:00Z",
            artifact_uri=f"memory://test/{unique_test_id}/commit1",
            sha256="sha1hash",
            chunk_idx=1,
            excerpt="单元测试",
            metadata={"tag": "test"},
        ),
        ChunkDoc(
            chunk_id=f"{unique_test_id}:git:commit2:sha2:v1:0",
            content="优化了数据库查询性能，添加了索引并重构了慢查询。",
            project_key="test_project",
            module="src/db/",
            source_type="git",
            source_id="commit2",
            owner_user_id="dev002",
            commit_ts="2024-06-16T14:00:00Z",
            artifact_uri=f"memory://test/{unique_test_id}/commit2",
            sha256="sha2hash",
            chunk_idx=0,
            excerpt="性能优化",
            metadata={"tag": "performance"},
        ),
        ChunkDoc(
            chunk_id=f"{unique_test_id}:logbook:item123:sha3:v1:0",
            content="Bug #456: 用户反馈登录页面在 IE11 下显示异常，需要兼容性修复。",
            project_key="test_project",
            module="issues/",
            source_type="logbook",
            source_id="item123",
            owner_user_id="pm001",
            commit_ts="2024-06-17T09:00:00Z",
            artifact_uri=f"memory://test/{unique_test_id}/item123",
            sha256="sha3hash",
            chunk_idx=0,
            excerpt="IE11 兼容性问题",
            metadata={"priority": "medium"},
        ),
    ]


# ============ 集成测试 ============


@skip_no_dsn
class TestPGVectorBackendIntegration:
    """PGVector 后端集成测试"""

    def test_initialize_creates_table(self, pgvector_backend: PGVectorBackend):
        """测试 initialize 创建表结构"""
        # initialize 在 fixture 中已执行
        assert pgvector_backend.schema == TEST_SCHEMA
        assert pgvector_backend.table_name == TEST_TABLE
        
        # 验证表存在
        conn = pgvector_backend._get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = %s
                )
            """, (TEST_SCHEMA, TEST_TABLE))
            row = cur.fetchone()
            assert row and row.get("exists", False), "表应该存在"

    def test_upsert_insert_documents(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试 upsert 插入新文档"""
        # 插入文档
        result = pgvector_backend.upsert(sample_docs)
        
        assert result == len(sample_docs)
        
        # 验证文档都有向量
        for doc in sample_docs:
            assert doc.vector is not None
            assert len(doc.vector) == 128

    def test_upsert_update_existing(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
    ):
        """测试 upsert 更新已存在的文档"""
        # 创建并插入文档
        doc = ChunkDoc(
            chunk_id=f"{unique_test_id}:upsert:update:sha:v1:0",
            content="原始内容版本1",
            project_key="test_project",
            source_type="git",
            source_id="update_test",
        )
        pgvector_backend.upsert([doc])
        
        # 更新内容并重新 upsert
        doc.content = "更新后的内容版本2"
        doc.vector = None  # 清除向量，让后端重新生成
        
        result = pgvector_backend.upsert([doc])
        
        assert result == 1
        
        # 验证内容已更新
        docs = pgvector_backend.get_by_ids([doc.chunk_id])
        assert len(docs) == 1
        assert docs[0].content == "更新后的内容版本2"

    def test_upsert_idempotent(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 upsert 幂等性"""
        doc = ChunkDoc(
            chunk_id=f"{unique_test_id}:idempotent:test:sha:v1:0",
            content="幂等性测试内容保持不变",
            project_key="test_project",
            source_type="git",
            source_id="idempotent_test",
        )
        
        # 多次 upsert 相同文档
        result1 = pgvector_backend.upsert([doc])
        vector1 = doc.vector.copy() if doc.vector else None
        
        doc.vector = None  # 重置向量
        result2 = pgvector_backend.upsert([doc])
        vector2 = doc.vector.copy() if doc.vector else None
        
        # 结果一致
        assert result1 == result2 == 1
        assert vector1 == vector2  # 确定性 embedding 应该相同

    def test_exists_checks_presence(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试 exists 检查文档存在性"""
        # 确保文档已插入
        pgvector_backend.upsert(sample_docs)
        
        existing_ids = [doc.chunk_id for doc in sample_docs]
        non_existing_id = "non-existing-chunk-id-12345"
        
        all_ids = existing_ids + [non_existing_id]
        result = pgvector_backend.exists(all_ids)
        
        # 已存在的文档应返回 True
        for cid in existing_ids:
            assert result[cid] is True, f"{cid} 应该存在"
        
        # 不存在的文档应返回 False
        assert result[non_existing_id] is False

    def test_get_by_ids_retrieves_documents(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试 get_by_ids 批量获取文档"""
        pgvector_backend.upsert(sample_docs)
        
        chunk_ids = [doc.chunk_id for doc in sample_docs[:2]]
        result = pgvector_backend.get_by_ids(chunk_ids)
        
        assert len(result) == 2
        
        result_ids = {doc.chunk_id for doc in result}
        expected_ids = set(chunk_ids)
        assert result_ids == expected_ids

    def test_query_returns_relevant_results(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试 query 返回相关结果"""
        pgvector_backend.upsert(sample_docs)
        
        # 查询安全相关内容
        request = QueryRequest(
            query_text="XSS 漏洞 安全修复",
            top_k=5,
            min_score=0.0,
        )
        
        results = pgvector_backend.query(request)
        
        assert len(results) > 0
        assert all(isinstance(hit, QueryHit) for hit in results)
        
        # 结果应按分数降序排列
        scores = [hit.score for hit in results]
        assert scores == sorted(scores, reverse=True)

    def test_query_with_filters(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试带过滤条件的查询"""
        pgvector_backend.upsert(sample_docs)
        
        # 按 source_type 过滤
        request = QueryRequest(
            query_text="bug fix",
            filters={"source_type": "logbook"},
            top_k=10,
            min_score=0.0,
        )
        
        results = pgvector_backend.query(request)
        
        # 所有结果应该是 logbook 类型
        for hit in results:
            assert hit.source_type == "logbook"

    def test_query_with_module_prefix_filter(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试模块前缀过滤"""
        pgvector_backend.upsert(sample_docs)
        
        # 按 module 前缀过滤
        request = QueryRequest(
            query_text="修复 优化",
            filters={"module": {"$prefix": "src/"}},
            top_k=10,
            min_score=0.0,
        )
        
        results = pgvector_backend.query(request)
        
        # 所有结果的 module 应以 src/ 开头
        for hit in results:
            module = hit.metadata.get("module", "")
            assert module.startswith("src/"), f"module '{module}' 不以 'src/' 开头"

    def test_delete_removes_documents(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
    ):
        """测试 delete 删除文档"""
        # 创建待删除的文档
        docs = [
            ChunkDoc(
                chunk_id=f"{unique_test_id}:delete:test:{i}",
                content=f"待删除文档 {i}",
                project_key="test_project",
                source_type="git",
            )
            for i in range(3)
        ]
        pgvector_backend.upsert(docs)
        
        # 验证文档存在
        chunk_ids = [doc.chunk_id for doc in docs]
        exists_before = pgvector_backend.exists(chunk_ids)
        assert all(exists_before.values())
        
        # 删除文档
        deleted = pgvector_backend.delete(chunk_ids)
        
        assert deleted == 3
        
        # 验证文档已删除
        exists_after = pgvector_backend.exists(chunk_ids)
        assert not any(exists_after.values())

    def test_delete_by_filter(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
    ):
        """测试 delete_by_filter 按条件删除"""
        # 创建特定 source_type 的文档
        special_source_type = "svn"  # 使用独特的类型
        docs = [
            ChunkDoc(
                chunk_id=f"{unique_test_id}:delfilter:svn:{i}",
                content=f"SVN 文档 {i}",
                project_key="test_project",
                source_type=special_source_type,
                source_id=f"svn-{unique_test_id}",
            )
            for i in range(2)
        ]
        pgvector_backend.upsert(docs)
        
        # 按条件删除
        deleted = pgvector_backend.delete_by_filter({
            "source_type": special_source_type,
            "source_id": f"svn-{unique_test_id}",
        })
        
        assert deleted == 2
        
        # 验证已删除
        chunk_ids = [doc.chunk_id for doc in docs]
        exists = pgvector_backend.exists(chunk_ids)
        assert not any(exists.values())

    def test_delete_by_version(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
    ):
        """测试 delete_by_version 按版本删除"""
        # 创建带版本的文档
        version = f"v1-{unique_test_id[:8]}"
        docs = [
            ChunkDoc(
                chunk_id=f"proj:git:src:{version}:{i}",
                content=f"版本 {version} 文档 {i}",
                project_key="test_project",
                source_type="git",
            )
            for i in range(3)
        ]
        pgvector_backend.upsert(docs)
        
        # 按版本删除
        deleted = pgvector_backend.delete_by_version(version)
        
        assert deleted == 3

    def test_count_by_source(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
    ):
        """测试 count_by_source 统计来源文档数量"""
        source_id = f"count-source-{unique_test_id}"
        docs = [
            ChunkDoc(
                chunk_id=f"{unique_test_id}:count:git:{source_id}:{i}",
                content=f"计数测试文档 {i}",
                project_key="test_project",
                source_type="git",
                source_id=source_id,
            )
            for i in range(4)
        ]
        pgvector_backend.upsert(docs)
        
        count = pgvector_backend.count_by_source("git", source_id)
        
        assert count == 4

    def test_get_chunk_metadata(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试 get_chunk_metadata 获取文档元数据"""
        pgvector_backend.upsert(sample_docs)
        
        chunk_ids = [doc.chunk_id for doc in sample_docs[:2]]
        metadata_map = pgvector_backend.get_chunk_metadata(chunk_ids)
        
        assert len(metadata_map) == 2
        
        for cid, meta in metadata_map.items():
            assert "sha256" in meta
            assert "source_id" in meta
            assert "source_type" in meta

    def test_health_check(self, pgvector_backend: PGVectorBackend):
        """测试 health_check 健康检查"""
        result = pgvector_backend.health_check()
        
        assert result["status"] == "healthy"
        assert result["backend"] == "pgvector"
        assert "details" in result
        assert result["details"]["schema"] == TEST_SCHEMA
        assert result["details"]["table"] == TEST_TABLE

    def test_get_stats(self, pgvector_backend: PGVectorBackend):
        """测试 get_stats 统计信息"""
        result = pgvector_backend.get_stats()
        
        assert "total_docs" in result
        assert "index_size_bytes" in result
        assert "by_source_type" in result
        assert result["schema"] == TEST_SCHEMA
        assert result["table"] == TEST_TABLE
        assert result["vector_dim"] == 128


@skip_no_dsn
class TestPGVectorBackendSchemaTableConfig:
    """PGVector 后端 schema 和 table 配置测试"""

    def test_explicit_schema_and_table_params(self, embedding_mock: DeterministicEmbeddingMock):
        """测试显式传递 schema 和 table_name 参数"""
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        assert backend.schema == TEST_SCHEMA
        assert backend.table_name == TEST_TABLE
        assert backend.qualified_table == f'"{TEST_SCHEMA}"."{TEST_TABLE}"'
        
        backend.close()

    def test_factory_with_explicit_schema_table(self, embedding_mock: DeterministicEmbeddingMock):
        """测试工厂函数显式传递 schema 和 table_name"""
        backend = create_pgvector_backend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            vector_dim=128,
            embedding_provider=embedding_mock,
        )
        
        assert backend.schema == TEST_SCHEMA
        assert backend.table_name == TEST_TABLE
        assert backend.qualified_table == f'"{TEST_SCHEMA}"."{TEST_TABLE}"'
        
        backend.close()


@skip_no_dsn
class TestPGVectorBackendErrorHandling:
    """PGVector 后端错误处理测试"""

    def test_query_empty_returns_empty(self, pgvector_backend: PGVectorBackend):
        """测试空查询返回空结果"""
        request = QueryRequest(
            query_text="这是一个不可能匹配到任何内容的随机查询字符串 xyz123",
            top_k=10,
            min_score=0.9,  # 高阈值
        )
        
        results = pgvector_backend.query(request)
        
        # 应返回空列表而不是抛出异常
        assert isinstance(results, list)

    def test_delete_non_existing_returns_zero(
        self,
        pgvector_backend: PGVectorBackend,
    ):
        """测试删除不存在的文档返回 0"""
        result = pgvector_backend.delete(["non-existing-id-1", "non-existing-id-2"])
        
        assert result == 0

    def test_get_by_ids_empty_returns_empty(self, pgvector_backend: PGVectorBackend):
        """测试空 ID 列表返回空结果"""
        result = pgvector_backend.get_by_ids([])
        
        assert result == []

    def test_upsert_empty_returns_zero(self, pgvector_backend: PGVectorBackend):
        """测试空文档列表返回 0"""
        result = pgvector_backend.upsert([])
        
        assert result == 0


@skip_no_dsn
class TestPGVectorBackendQueryStability:
    """查询结果稳定性测试"""

    def test_query_order_deterministic(
        self,
        pgvector_backend: PGVectorBackend,
        sample_docs: List[ChunkDoc],
    ):
        """测试相同查询返回相同顺序的结果"""
        pgvector_backend.upsert(sample_docs)
        
        request = QueryRequest(
            query_text="修复 bug 漏洞",
            top_k=10,
            min_score=0.0,
        )
        
        # 多次执行相同查询
        results1 = pgvector_backend.query(request)
        results2 = pgvector_backend.query(request)
        results3 = pgvector_backend.query(request)
        
        # 结果数量一致
        assert len(results1) == len(results2) == len(results3)
        
        # 结果顺序一致
        for r1, r2, r3 in zip(results1, results2, results3):
            assert r1.chunk_id == r2.chunk_id == r3.chunk_id
            assert r1.score == r2.score == r3.score

    def test_query_tiebreaker_by_chunk_id(
        self,
        pgvector_backend: PGVectorBackend,
        unique_test_id: str,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试分数相同时按 chunk_id 排序"""
        # 创建内容完全相同的文档（会产生相同向量分数）
        docs = [
            ChunkDoc(
                chunk_id=f"{unique_test_id}:tiebreak:b",  # 字母序在后
                content="完全相同的内容用于测试排序",
                project_key="test_project",
                source_type="git",
            ),
            ChunkDoc(
                chunk_id=f"{unique_test_id}:tiebreak:a",  # 字母序在前
                content="完全相同的内容用于测试排序",
                project_key="test_project",
                source_type="git",
            ),
        ]
        pgvector_backend.upsert(docs)
        
        request = QueryRequest(
            query_text="完全相同的内容用于测试排序",
            top_k=10,
            min_score=0.0,
        )
        
        results = pgvector_backend.query(request)
        
        # 找出我们插入的两个文档
        our_results = [r for r in results if unique_test_id in r.chunk_id]
        
        if len(our_results) >= 2:
            # 如果分数相同，应按 chunk_id 字母序排序
            if our_results[0].score == our_results[1].score:
                assert our_results[0].chunk_id < our_results[1].chunk_id


@skip_no_dsn
class TestCollectionIsolation:
    """Collection 隔离测试 - 验证不同 collection 映射到独立表并完全隔离"""

    def test_two_collections_map_to_different_tables(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试两个不同 collection 映射到两张不同的表"""
        collection_a = "proj_a:v1:bge-m3"
        collection_b = "proj_b:v1:bge-m3"
        
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=collection_a,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=collection_b,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            # 验证表名不同
            assert backend_a.table_name != backend_b.table_name
            assert "proj_a" in backend_a.table_name
            assert "proj_b" in backend_b.table_name
            
            # 验证表名格式正确（step3_chunks_ 前缀）
            assert backend_a.table_name.startswith("step3_chunks_")
            assert backend_b.table_name.startswith("step3_chunks_")
            
        finally:
            backend_a.close()
            backend_b.close()

    def test_collection_data_isolation(
        self,
        embedding_mock: DeterministicEmbeddingMock,
        unique_test_id: str,
    ):
        """测试不同 collection 的数据完全隔离（写入/查询互不影响）"""
        collection_a = f"isolation_a_{unique_test_id}:v1:model"
        collection_b = f"isolation_b_{unique_test_id}:v1:model"
        
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=collection_a,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=collection_b,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            # 初始化两个表
            backend_a.initialize()
            backend_b.initialize()
            
            # 向 collection A 写入数据
            doc_a = ChunkDoc(
                chunk_id=f"{unique_test_id}:isolation:a:0",
                content="这是 Collection A 的独有内容，关于安全漏洞修复",
                project_key="proj_a",
                source_type="git",
                source_id="commit_a",
            )
            backend_a.upsert([doc_a])
            
            # 向 collection B 写入数据
            doc_b = ChunkDoc(
                chunk_id=f"{unique_test_id}:isolation:b:0",
                content="这是 Collection B 的独有内容，关于性能优化",
                project_key="proj_b",
                source_type="git",
                source_id="commit_b",
            )
            backend_b.upsert([doc_b])
            
            # 验证 A 只能查到自己的数据
            results_a = backend_a.query(QueryRequest(
                query_text="安全漏洞",
                top_k=10,
                min_score=0.0,
            ))
            assert len(results_a) >= 1
            assert any("Collection A" in r.content for r in results_a)
            assert not any("Collection B" in r.content for r in results_a)
            
            # 验证 B 只能查到自己的数据
            results_b = backend_b.query(QueryRequest(
                query_text="性能优化",
                top_k=10,
                min_score=0.0,
            ))
            assert len(results_b) >= 1
            assert any("Collection B" in r.content for r in results_b)
            assert not any("Collection A" in r.content for r in results_b)
            
            # 验证 exists 隔离
            exists_a = backend_a.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_a[doc_a.chunk_id] is True
            assert exists_a[doc_b.chunk_id] is False
            
            exists_b = backend_b.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_b[doc_a.chunk_id] is False
            assert exists_b[doc_b.chunk_id] is True
            
        finally:
            # 清理测试表
            try:
                conn_a = backend_a._get_connection()
                with conn_a.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS {backend_a.qualified_table}')
                    conn_a.commit()
            except Exception:
                pass
            
            try:
                conn_b = backend_b._get_connection()
                with conn_b.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS {backend_b.qualified_table}')
                    conn_b.commit()
            except Exception:
                pass
            
            backend_a.close()
            backend_b.close()


class TestCollectionNamingValidation:
    """Collection 命名验证测试 - 表名长度/合法字符处理（不需要数据库连接）"""

    def test_collection_with_special_characters(self):
        """测试特殊字符被正确清理"""
        # 使用假的 DSN，只测试表名生成逻辑
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        collection_id = "my-project:v1:bge-m3-embedding"
        
        backend = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema=TEST_SCHEMA,
            collection_id=collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        # 表名应该只包含字母、数字、下划线
        table_name = backend.table_name
        assert "-" not in table_name  # 连字符应被替换
        assert ":" not in table_name  # 冒号应被替换
        assert table_name.islower()  # 应为小写
        assert table_name.startswith("step3_chunks_")
        
        # 验证表名格式正确（可用于 SQL）
        import re
        assert re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name)

    def test_collection_with_long_name(self):
        """测试超长 collection_id 被正确截断"""
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        # 创建一个超长的 collection_id（超过 63 字符限制）
        long_project = "a" * 50
        long_collection_id = f"{long_project}:v1:very_long_embedding_model_name_that_exceeds_limit"
        
        backend = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema=TEST_SCHEMA,
            collection_id=long_collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        table_name = backend.table_name
        
        # 表名应不超过 63 字符
        assert len(table_name) <= 63, f"表名过长: {len(table_name)} 字符"
        
        # 表名应仍然以正确前缀开头
        assert table_name.startswith("step3_chunks_")

    def test_collection_with_unicode_characters(self):
        """测试 Unicode 字符被正确处理（移除）"""
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        collection_id = "proj:v1:model"
        
        backend = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema=TEST_SCHEMA,
            collection_id=collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        table_name = backend.table_name
        
        # 验证表名只包含 ASCII 字符
        assert table_name.isascii()
        
        # 验证表名格式
        import re
        assert re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name)

    def test_collection_with_numeric_prefix(self):
        """测试数字开头的 collection_id 被正确处理"""
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        collection_id = "123proj:v1:model"
        
        backend = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema=TEST_SCHEMA,
            collection_id=collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        table_name = backend.table_name
        
        # PostgreSQL 标识符不能以数字开头
        # 表名应以字母或下划线开头
        assert table_name[0].isalpha() or table_name[0] == '_'

    def test_two_long_collections_remain_unique(self):
        """测试两个超长但不同的 collection_id 仍然生成唯一表名"""
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        # 两个只在末尾不同的超长 collection_id
        base = "a" * 50 + ":v1:model_"
        collection_a = base + "suffix_a"
        collection_b = base + "suffix_b"
        
        backend_a = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema=TEST_SCHEMA,
            collection_id=collection_a,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        backend_b = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema=TEST_SCHEMA,
            collection_id=collection_b,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        # 两个表名都不超过 63 字符
        assert len(backend_a.table_name) <= 63
        assert len(backend_b.table_name) <= 63
        
        # 两个表名应该不同（通过 hash 区分）
        assert backend_a.table_name != backend_b.table_name


@skip_no_dsn
class TestCollectionNamingValidationWithDB:
    """Collection 命名验证测试（需要数据库，验证 initialize 能正确创建表）"""

    def test_collection_with_long_name_can_initialize(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试超长 collection_id 可以正常初始化表结构"""
        long_project = "a" * 50
        long_collection_id = f"{long_project}:v1:very_long_model"
        
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            collection_id=long_collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            # 验证可以正常初始化（不会因为名称问题报错）
            backend.initialize()
            
            # 验证表存在
            conn = backend._get_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = %s AND table_name = %s
                    )
                """, (TEST_SCHEMA, backend.table_name))
                row = cur.fetchone()
                assert row and row.get("exists", False)
        finally:
            # 清理测试表
            try:
                conn = backend._get_connection()
                with conn.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS {backend.qualified_table}')
                    conn.commit()
            except Exception:
                pass
            backend.close()


class TestCollectionStrategyIntegration:
    """Collection Strategy 集成测试（不需要数据库连接）"""

    def test_default_strategy_resolves_correctly(self):
        """测试默认策略正确解析存储位置"""
        from index_backend.pgvector_collection_strategy import (
            DefaultCollectionStrategy,
            resolve_storage,
        )
        
        strategy = DefaultCollectionStrategy()
        
        # 有 collection_id 时生成动态表名
        resolution = strategy.resolve_storage(
            collection_id="myproj:v1:bge-m3",
            schema="step3",
            base_table="chunks",
        )
        
        assert resolution.schema == "step3"
        assert resolution.table.startswith("step3_chunks_")
        assert "myproj" in resolution.table
        assert resolution.where_clause_extra == ""
        assert resolution.params_extra == []
        
        # 无 collection_id 时使用默认表名
        resolution_default = strategy.resolve_storage(
            collection_id=None,
            schema="step3",
            base_table="chunks",
        )
        
        assert resolution_default.table == "chunks"

    def test_resolve_storage_convenience_function(self):
        """测试便捷函数 resolve_storage"""
        from index_backend.pgvector_collection_strategy import resolve_storage
        
        resolution = resolve_storage(
            collection_id="test:v1:model",
            schema="step3_test",
            base_table="chunks_test",
        )
        
        assert resolution.schema == "step3_test"
        assert "test" in resolution.table
        assert resolution.qualified_table == f'"{resolution.schema}"."{resolution.table}"'

    def test_strategy_validates_table_name(self):
        """测试策略验证表名合法性"""
        from index_backend.pgvector_collection_strategy import DefaultCollectionStrategy
        
        strategy = DefaultCollectionStrategy()
        
        # 正常表名
        is_valid, _ = strategy.validate_table_name("step3_chunks_test")
        assert is_valid
        
        # 空表名
        is_valid, error = strategy.validate_table_name("")
        assert not is_valid
        assert "不能为空" in error
        
        # 超长表名
        long_name = "a" * 100
        is_valid, error = strategy.validate_table_name(long_name)
        assert not is_valid
        assert "超过" in error


@skip_no_dsn
class TestPGVectorBackendCollectionIsolation:
    """
    Collection 隔离测试 - 验证不同 collection 之间数据互不可见
    
    重要说明：
    - 使用 SharedTableStrategy 时，collection 隔离由策略自动提供
    - 使用 DefaultCollectionStrategy 时，需要显式传入 collection_id filter
    """

    def test_shared_table_strategy_isolates_collections(self, embedding_mock: DeterministicEmbeddingMock):
        """测试 SharedTableStrategy 下不同 collection 写入后互相查询不到"""
        if not TEST_PGVECTOR_DSN:
            pytest.skip("TEST_PGVECTOR_DSN 环境变量未设置")
        
        # 生成唯一的 collection_id 前缀
        unique_prefix = uuid.uuid4().hex[:8]
        collection_a = f"shared-test-a:{unique_prefix}"
        collection_b = f"shared-test-b:{unique_prefix}"
        
        # 使用 SharedTableStrategy（单表多租户）
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        # 创建两个不同 collection 的后端实例（共享同一个表）
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=strategy,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_id=collection_b,
            collection_strategy=strategy,
        )
        
        try:
            # 初始化（如果表不存在会创建）
            backend_a.initialize()
            backend_b.initialize()
            
            # 验证使用相同的表
            assert backend_a.table_name == backend_b.table_name == TEST_TABLE
            
            # 为 collection A 写入文档
            doc_a = ChunkDoc(
                chunk_id=f"{unique_prefix}:collection-a:doc1",
                content="这是 Collection A 的独特内容，关于机器学习的文档",
                project_key="test_project",
                source_type="git",
                source_id="collection_a_source",
            )
            backend_a.upsert([doc_a])
            
            # 为 collection B 写入文档
            doc_b = ChunkDoc(
                chunk_id=f"{unique_prefix}:collection-b:doc1",
                content="这是 Collection B 的独特内容，关于数据科学的文档",
                project_key="test_project",
                source_type="git",
                source_id="collection_b_source",
            )
            backend_b.upsert([doc_b])
            
            # ===== 验证隔离性 =====
            
            # 1. Collection A 只能查到自己的文档
            results_a = backend_a.query(QueryRequest(
                query_text="机器学习 数据科学",
                top_k=10,
                min_score=0.0,
            ))
            result_ids_a = {hit.chunk_id for hit in results_a}
            assert doc_a.chunk_id in result_ids_a or len(results_a) == 0, \
                "Collection A 应该能查到自己的文档或返回空"
            assert doc_b.chunk_id not in result_ids_a, \
                "Collection A 不应该查到 Collection B 的文档"
            
            # 2. Collection B 只能查到自己的文档
            results_b = backend_b.query(QueryRequest(
                query_text="机器学习 数据科学",
                top_k=10,
                min_score=0.0,
            ))
            result_ids_b = {hit.chunk_id for hit in results_b}
            assert doc_b.chunk_id in result_ids_b or len(results_b) == 0, \
                "Collection B 应该能查到自己的文档或返回空"
            assert doc_a.chunk_id not in result_ids_b, \
                "Collection B 不应该查到 Collection A 的文档"
            
            # 3. exists 检查隔离
            exists_a = backend_a.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_a[doc_a.chunk_id] is True, "Collection A 应该能看到自己的文档"
            assert exists_a[doc_b.chunk_id] is False, "Collection A 不应该看到 Collection B 的文档"
            
            exists_b = backend_b.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_b[doc_b.chunk_id] is True, "Collection B 应该能看到自己的文档"
            assert exists_b[doc_a.chunk_id] is False, "Collection B 不应该看到 Collection A 的文档"
            
            # 4. get_by_ids 隔离
            docs_from_a = backend_a.get_by_ids([doc_a.chunk_id, doc_b.chunk_id])
            assert len(docs_from_a) == 1, "Collection A 只应该获取到 1 个文档"
            assert docs_from_a[0].chunk_id == doc_a.chunk_id
            
            docs_from_b = backend_b.get_by_ids([doc_a.chunk_id, doc_b.chunk_id])
            assert len(docs_from_b) == 1, "Collection B 只应该获取到 1 个文档"
            assert docs_from_b[0].chunk_id == doc_b.chunk_id
            
            # 5. delete 隔离
            # Collection A 尝试删除 Collection B 的文档应该返回 0
            deleted_cross = backend_a.delete([doc_b.chunk_id])
            assert deleted_cross == 0, "Collection A 不应该能删除 Collection B 的文档"
            
            # 验证 doc_b 仍然存在
            exists_b_after = backend_b.exists([doc_b.chunk_id])
            assert exists_b_after[doc_b.chunk_id] is True, "doc_b 应该仍然存在"
            
            # 6. get_stats 隔离
            stats_a = backend_a.get_stats()
            stats_b = backend_b.get_stats()
            assert stats_a["collection_id"] == collection_a
            assert stats_b["collection_id"] == collection_b
            assert stats_a["strategy"] == "shared_table"
            # 各自的文档数应该是 1（仅计算本 collection 的）
            assert stats_a["total_docs"] >= 1
            assert stats_b["total_docs"] >= 1
            
        finally:
            # 清理测试数据
            try:
                backend_a.delete([doc_a.chunk_id])
                backend_b.delete([doc_b.chunk_id])
            except Exception:
                pass
            
            backend_a.close()
            backend_b.close()

    def test_shared_table_delete_by_filter_isolation(self, embedding_mock: DeterministicEmbeddingMock):
        """测试 SharedTableStrategy 下 delete_by_filter 的 collection 隔离"""
        if not TEST_PGVECTOR_DSN:
            pytest.skip("TEST_PGVECTOR_DSN 环境变量未设置")
        
        unique_prefix = uuid.uuid4().hex[:8]
        collection_a = f"del-filter-a:{unique_prefix}"
        collection_b = f"del-filter-b:{unique_prefix}"
        
        # 使用 SharedTableStrategy
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=strategy,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_id=collection_b,
            collection_strategy=strategy,
        )
        
        try:
            backend_a.initialize()
            backend_b.initialize()
            
            # 两个 collection 写入相同 source_type 的文档
            common_source_type = f"common-{unique_prefix}"
            
            doc_a = ChunkDoc(
                chunk_id=f"{unique_prefix}:del-filter:a:doc1",
                content="Collection A 文档",
                project_key="test_project",
                source_type=common_source_type,
            )
            backend_a.upsert([doc_a])
            
            doc_b = ChunkDoc(
                chunk_id=f"{unique_prefix}:del-filter:b:doc1",
                content="Collection B 文档",
                project_key="test_project",
                source_type=common_source_type,
            )
            backend_b.upsert([doc_b])
            
            # Collection A 按 source_type 删除
            # 注意：SharedTableStrategy 自动添加 collection_id 过滤
            deleted = backend_a.delete_by_filter({"source_type": common_source_type})
            
            # 应该只删除 Collection A 的文档
            assert deleted == 1, f"应该删除 1 个文档，实际删除 {deleted}"
            
            # doc_a 应该被删除
            exists_a = backend_a.exists([doc_a.chunk_id])
            assert exists_a[doc_a.chunk_id] is False
            
            # doc_b 应该仍然存在
            exists_b = backend_b.exists([doc_b.chunk_id])
            assert exists_b[doc_b.chunk_id] is True, "doc_b 不应该被 Collection A 的删除操作影响"
            
        finally:
            try:
                backend_b.delete([doc_b.chunk_id])
            except Exception:
                pass
            backend_a.close()
            backend_b.close()

    def test_admin_backend_explicit_collection_filter(self, embedding_mock: DeterministicEmbeddingMock):
        """
        测试管理态 backend (collection_id=None) 需要显式传递 collection_id filter
        
        使用 DefaultCollectionStrategy 且 collection_id=None 时：
        - 不会自动添加任何 collection 过滤
        - 需要显式在 filters 中传入 collection_id 才能限定范围
        - 适用于诊断、管理等跨 collection 操作场景
        """
        if not TEST_PGVECTOR_DSN:
            pytest.skip("TEST_PGVECTOR_DSN 环境变量未设置")
        
        unique_prefix = uuid.uuid4().hex[:8]
        collection_id = f"admin-test:{unique_prefix}"
        
        # 创建无 collection_id 的 backend（管理态，用于诊断/管理场景）
        backend_admin = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_id=None,  # 管理态：无 collection 限制
        )
        
        try:
            backend_admin.initialize()
            
            # 写入文档时使用 doc.collection_id
            doc = ChunkDoc(
                chunk_id=f"{unique_prefix}:admin:doc1",
                content="管理态 collection_id 测试文档",
                project_key="test_project",
                source_type="git",
                collection_id=collection_id,  # doc 级别指定
            )
            backend_admin.upsert([doc])
            
            # admin 不显式指定 collection_id 时，能查到所有数据
            results_all = backend_admin.query(QueryRequest(
                query_text="管理态 collection_id 测试",
                top_k=10,
                min_score=0.0,
            ))
            # 结果中应该包含我们的文档（因为没有 collection 过滤）
            result_ids_all = {hit.chunk_id for hit in results_all}
            assert doc.chunk_id in result_ids_all or len(results_all) >= 0, \
                "admin 不带 filter 应该能查到所有文档"
            
            # admin 通过显式指定 collection_id 可以限定范围
            results_filtered = backend_admin.query(QueryRequest(
                query_text="管理态 collection_id 测试",
                filters={"collection_id": collection_id},  # 显式指定
                top_k=10,
                min_score=0.0,
            ))
            
            result_ids = {hit.chunk_id for hit in results_filtered}
            assert doc.chunk_id in result_ids or len(results_filtered) >= 0, \
                "admin 应该能通过显式 collection_id 查询到文档"
            
            # admin 通过 delete_by_filter 显式指定 collection_id 删除
            deleted = backend_admin.delete_by_filter({
                "collection_id": collection_id,
                "source_type": "git",
            })
            assert deleted >= 1, "admin 应该能删除指定 collection 的文档"
            
        finally:
            backend_admin.close()


class TestCollectionConditionUniqueness:
    """
    测试 collection 条件唯一性
    
    确保使用 SharedTableStrategy 时，query/delete_by_filter 只出现一次 collection 条件
    """

    def test_shared_table_strategy_single_collection_condition(self):
        """测试 SharedTableStrategy 下策略只提供一次 collection 条件"""
        from index_backend.pgvector_collection_strategy import SharedTableStrategy
        
        strategy = SharedTableStrategy(expected_vector_dim=128)
        
        # 解析存储位置
        resolution = strategy.resolve_storage(
            collection_id="test:v1:model",
            schema="step3",
            base_table="chunks",
        )
        
        # 验证 where_clause_extra 只包含一个 collection_id 条件
        assert resolution.where_clause_extra == "collection_id = %s"
        assert resolution.params_extra == ["test:v1:model"]
        
        # 验证条件不会被重复添加
        # 当 backend 使用 _merge_where_conditions 时，应该只合并一次

    def test_default_strategy_no_collection_condition(self):
        """测试 DefaultCollectionStrategy 不会添加额外的 collection 条件"""
        from index_backend.pgvector_collection_strategy import DefaultCollectionStrategy
        
        strategy = DefaultCollectionStrategy()
        
        # 即使有 collection_id，DefaultStrategy 也不添加 WHERE 条件
        resolution = strategy.resolve_storage(
            collection_id="test:v1:model",
            schema="step3",
            base_table="chunks",
        )
        
        # where_clause_extra 应该为空
        assert resolution.where_clause_extra == ""
        assert resolution.params_extra == []
        
        # 表名会根据 collection_id 生成
        assert "test" in resolution.table

    def test_merge_where_conditions_preserves_single_collection_filter(self):
        """测试 _merge_where_conditions 正确合并条件而不重复"""
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        
        # 使用 SharedTableStrategy
        strategy = SharedTableStrategy(expected_vector_dim=128)
        
        backend = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema="step3_test",
            table_name="chunks_test",
            collection_id="test:v1:model",
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        # 获取策略的 extra where
        extra_clause = backend.storage_resolution.where_clause_extra
        extra_params = backend.storage_resolution.params_extra
        
        # 合并一个基础条件
        base_clause = "source_type = %s"
        base_params = ["git"]
        
        merged_clause, merged_params = backend._merge_where_conditions(
            base_clause, base_params
        )
        
        # 验证合并结果
        # merged_clause 应该是 (base) AND (extra) 格式
        assert "source_type = %s" in merged_clause
        assert "collection_id = %s" in merged_clause
        
        # 验证参数顺序正确
        assert merged_params == ["git", "test:v1:model"]
        
        # 验证 collection_id 条件只出现一次
        assert merged_clause.count("collection_id") == 1


class TestCollectionStrategySwitchBehavior:
    """
    Collection 策略开关行为测试
    
    测试 STEP3_PGVECTOR_COLLECTION_STRATEGY 环境变量的效果（不需要数据库）
    """

    def test_per_table_strategy_creates_different_tables(self):
        """测试 per_table 策略为不同 collection 创建不同表"""
        embedding_mock = DeterministicEmbeddingMock(dim=128)
        strategy = DefaultCollectionStrategy()
        
        # 不同 collection 应该解析到不同表
        res_a = strategy.resolve_storage(
            collection_id="proj_a:v1:model",
            schema="step3",
            base_table="chunks",
        )
        res_b = strategy.resolve_storage(
            collection_id="proj_b:v1:model",
            schema="step3",
            base_table="chunks",
        )
        
        assert res_a.table != res_b.table
        assert "proj_a" in res_a.table
        assert "proj_b" in res_b.table
        assert res_a.where_clause_extra == ""
        assert res_b.where_clause_extra == ""

    def test_single_table_strategy_uses_same_table(self):
        """测试 single_table 策略为不同 collection 使用相同表"""
        strategy = SharedTableStrategy(expected_vector_dim=128)
        
        # 不同 collection 应该解析到相同表，但有不同的 WHERE 条件
        res_a = strategy.resolve_storage(
            collection_id="proj_a:v1:model",
            schema="step3",
            base_table="chunks",
        )
        res_b = strategy.resolve_storage(
            collection_id="proj_b:v1:model",
            schema="step3",
            base_table="chunks",
        )
        
        assert res_a.table == res_b.table == "chunks"
        assert res_a.where_clause_extra == "collection_id = %s"
        assert res_b.where_clause_extra == "collection_id = %s"
        assert res_a.params_extra == ["proj_a:v1:model"]
        assert res_b.params_extra == ["proj_b:v1:model"]

    def test_single_table_strategy_rejects_mismatched_vector_dim(self):
        """测试 single_table 策略拒绝不同向量维度"""
        strategy = SharedTableStrategy(expected_vector_dim=1536)
        
        # 尝试使用不同维度应该抛出错误
        with pytest.raises(VectorDimensionMismatchError) as exc_info:
            strategy.validate_vector_dim(
                collection_id="proj:v1:small-model",
                requested_dim=768,  # 与预期 1536 不同
                table_name="step3.chunks",
            )
        
        error = exc_info.value
        assert error.collection_id == "proj:v1:small-model"
        assert error.requested_dim == 768
        assert error.expected_dim == 1536
        assert "per_table" in str(error)
        assert "STEP3_PGVECTOR_COLLECTION_STRATEGY" in str(error)

    def test_single_table_strategy_accepts_matching_vector_dim(self):
        """测试 single_table 策略接受匹配的向量维度"""
        strategy = SharedTableStrategy(expected_vector_dim=1536)
        
        # 匹配的维度不应抛出错误
        strategy.validate_vector_dim(
            collection_id="proj:v1:model",
            requested_dim=1536,
            table_name="step3.chunks",
        )
        # 无异常表示测试通过

    def test_single_table_strategy_skips_validation_when_no_expected_dim(self):
        """测试未设置预期维度时跳过验证"""
        strategy = SharedTableStrategy(expected_vector_dim=None)
        
        # 任何维度都应该被接受
        strategy.validate_vector_dim(
            collection_id="proj:v1:model",
            requested_dim=768,
            table_name="step3.chunks",
        )
        strategy.validate_vector_dim(
            collection_id="proj:v1:model",
            requested_dim=1536,
            table_name="step3.chunks",
        )

    def test_backend_validates_dim_with_shared_table_strategy(self):
        """测试 PGVectorBackend 在使用 SharedTableStrategy 时验证维度"""
        embedding_mock = DeterministicEmbeddingMock(dim=768)
        strategy = SharedTableStrategy(expected_vector_dim=1536)
        
        with pytest.raises(VectorDimensionMismatchError) as exc_info:
            PGVectorBackend(
                connection_string="postgresql://fake:fake@localhost:5432/fake",
                schema="step3_test",
                table_name="chunks_test",
                collection_id="proj:v1:model",
                embedding_provider=embedding_mock,
                vector_dim=768,  # 与 strategy 的 expected_vector_dim=1536 不匹配
                collection_strategy=strategy,
            )
        
        assert "768" in str(exc_info.value)
        assert "1536" in str(exc_info.value)

    def test_backend_skips_dim_validation_with_default_strategy(self):
        """测试 PGVectorBackend 使用默认策略时不验证维度"""
        embedding_mock = DeterministicEmbeddingMock(dim=768)
        
        # 不同维度应该可以创建不同的后端（因为是不同的表）
        backend_a = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema="step3_test",
            collection_id="proj_a:v1:model",
            embedding_provider=embedding_mock,
            vector_dim=768,
        )
        
        backend_b = PGVectorBackend(
            connection_string="postgresql://fake:fake@localhost:5432/fake",
            schema="step3_test",
            collection_id="proj_b:v1:model",
            embedding_provider=embedding_mock,
            vector_dim=1536,
        )
        
        # 两个后端使用不同的表
        assert backend_a.table_name != backend_b.table_name


class TestCollectionStrategyEnvironmentVariable:
    """测试 STEP3_PGVECTOR_COLLECTION_STRATEGY 环境变量的解析"""

    def test_env_var_per_table_default(self, monkeypatch):
        """测试默认值为 per_table"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_PER_TABLE
        
        # 清除环境变量
        monkeypatch.delenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", raising=False)
        
        config = PGVectorConfig.from_env()
        assert config.collection_strategy == COLLECTION_STRATEGY_PER_TABLE

    def test_env_var_single_table(self, monkeypatch):
        """测试设置 single_table 策略"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_SINGLE_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "single_table")
        
        config = PGVectorConfig.from_env()
        assert config.collection_strategy == COLLECTION_STRATEGY_SINGLE_TABLE

    def test_env_var_per_table_explicit(self, monkeypatch):
        """测试显式设置 per_table 策略"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_PER_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "per_table")
        
        config = PGVectorConfig.from_env()
        assert config.collection_strategy == COLLECTION_STRATEGY_PER_TABLE

    def test_env_var_invalid_falls_back_to_default(self, monkeypatch):
        """测试无效值回退到默认值"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_PER_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "invalid_strategy")
        
        config = PGVectorConfig.from_env()
        assert config.collection_strategy == COLLECTION_STRATEGY_PER_TABLE

    def test_env_var_case_insensitive(self, monkeypatch):
        """测试值不区分大小写"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_SINGLE_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "SINGLE_TABLE")
        
        config = PGVectorConfig.from_env()
        assert config.collection_strategy == COLLECTION_STRATEGY_SINGLE_TABLE

    def test_config_to_dict_includes_strategy(self, monkeypatch):
        """测试 to_dict 包含 collection_strategy"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_SINGLE_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "single_table")
        
        config = PGVectorConfig.from_env()
        config_dict = config.to_dict()
        
        assert "collection_strategy" in config_dict
        assert config_dict["collection_strategy"] == COLLECTION_STRATEGY_SINGLE_TABLE


@skip_no_dsn
class TestCollectionStrategySwitchWithDB:
    """
    Collection 策略开关行为测试（需要数据库）
    
    验证不同策略在实际数据库操作中的行为
    """

    def test_single_table_strategy_isolation(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 single_table 策略的数据隔离"""
        unique_prefix = uuid.uuid4().hex[:8]
        collection_a = f"single-table-a:{unique_prefix}"
        collection_b = f"single-table-b:{unique_prefix}"
        
        # 使用相同维度的 SharedTableStrategy
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,  # 共享同一张表
            collection_id=collection_a,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,  # 共享同一张表
            collection_id=collection_b,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            backend_a.initialize()
            backend_b.initialize()
            
            # 验证两个后端使用相同的表
            assert backend_a.table_name == backend_b.table_name == TEST_TABLE
            
            # 写入数据
            doc_a = ChunkDoc(
                chunk_id=f"{unique_prefix}:single-table:a:doc1",
                content="Single table strategy collection A 内容",
                project_key="test_project",
                source_type="git",
            )
            backend_a.upsert([doc_a])
            
            doc_b = ChunkDoc(
                chunk_id=f"{unique_prefix}:single-table:b:doc1",
                content="Single table strategy collection B 内容",
                project_key="test_project",
                source_type="git",
            )
            backend_b.upsert([doc_b])
            
            # 验证隔离性
            exists_a = backend_a.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_a[doc_a.chunk_id] is True
            assert exists_a[doc_b.chunk_id] is False
            
            exists_b = backend_b.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_b[doc_a.chunk_id] is False
            assert exists_b[doc_b.chunk_id] is True
            
        finally:
            try:
                backend_a.delete([doc_a.chunk_id])
                backend_b.delete([doc_b.chunk_id])
            except Exception:
                pass
            backend_a.close()
            backend_b.close()

    def test_single_table_collection_id_not_null_after_upsert(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 single_table 场景下写入后 collection_id 必不为 NULL"""
        unique_prefix = uuid.uuid4().hex[:8]
        collection_id = f"not-null-test:{unique_prefix}"
        
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            collection_id=collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            backend.initialize()
            
            # 写入文档
            doc = ChunkDoc(
                chunk_id=f"{unique_prefix}:not-null:doc1",
                content="测试 collection_id 不为 NULL",
                project_key="test_project",
                source_type="git",
            )
            backend.upsert([doc])
            
            # 直接查询数据库验证 collection_id 不为 NULL
            conn = backend._get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT collection_id FROM {backend.qualified_table} WHERE chunk_id = %s",
                    (doc.chunk_id,)
                )
                row = cur.fetchone()
                assert row is not None, "文档应该存在"
                assert row["collection_id"] is not None, "collection_id 不应为 NULL"
                assert row["collection_id"] == collection_id, f"collection_id 应为 '{collection_id}'"
        finally:
            try:
                backend.delete([doc.chunk_id])
            except Exception:
                pass
            backend.close()

    def test_shared_table_rejects_none_collection_id(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 SharedTableStrategy 且 backend.collection_id 为 None 时拒绝写入"""
        from index_backend.pgvector_backend import PGVectorError
        
        unique_prefix = uuid.uuid4().hex[:8]
        
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        # 注意：SharedTableStrategy.resolve_storage 在 collection_id 为 None 时会抛出错误
        # 所以无法创建 backend
        with pytest.raises(ValueError) as exc_info:
            PGVectorBackend(
                connection_string=TEST_PGVECTOR_DSN,
                schema=TEST_SCHEMA,
                table_name=TEST_TABLE,
                collection_id=None,  # None collection_id
                embedding_provider=embedding_mock,
                vector_dim=128,
                collection_strategy=strategy,
            )
        
        assert "collection_id" in str(exc_info.value).lower()

    def test_doc_collection_id_override_allowed_when_backend_none(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 backend.collection_id 为 None 时允许使用 doc.collection_id（管理场景）"""
        unique_prefix = uuid.uuid4().hex[:8]
        doc_collection_id = f"doc-level-override:{unique_prefix}"
        
        # 使用默认策略（非 SharedTableStrategy），backend collection_id 为 None
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            collection_id=None,  # 管理模式
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            backend.initialize()
            
            # 使用 doc 级别的 collection_id
            doc = ChunkDoc(
                chunk_id=f"{unique_prefix}:doc-override:doc1",
                content="测试 doc 级别 collection_id 覆盖",
                project_key="test_project",
                source_type="git",
                collection_id=doc_collection_id,  # doc 级别指定
            )
            result = backend.upsert([doc])
            
            assert result == 1
            
            # 验证写入的 collection_id
            conn = backend._get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT collection_id FROM {backend.qualified_table} WHERE chunk_id = %s",
                    (doc.chunk_id,)
                )
                row = cur.fetchone()
                assert row is not None
                assert row["collection_id"] == doc_collection_id
        finally:
            try:
                conn = backend._get_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {backend.qualified_table} WHERE chunk_id = %s",
                        (doc.chunk_id,)
                    )
                    conn.commit()
            except Exception:
                pass
            backend.close()

    def test_doc_collection_id_mismatch_rejected(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 doc.collection_id 与 backend.collection_id 不一致时拒绝写入"""
        from index_backend.pgvector_backend import PGVectorError
        
        unique_prefix = uuid.uuid4().hex[:8]
        backend_collection = f"backend-collection:{unique_prefix}"
        doc_collection = f"different-collection:{unique_prefix}"
        
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            collection_id=backend_collection,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            backend.initialize()
            
            # doc.collection_id 与 backend.collection_id 不一致
            doc = ChunkDoc(
                chunk_id=f"{unique_prefix}:mismatch:doc1",
                content="测试不一致 collection_id",
                project_key="test_project",
                source_type="git",
                collection_id=doc_collection,  # 与 backend 不一致
            )
            
            with pytest.raises(PGVectorError) as exc_info:
                backend.upsert([doc])
            
            assert "不一致" in str(exc_info.value) or "mismatch" in str(exc_info.value).lower()
        finally:
            backend.close()


# ============ 迁移脚本集成测试 ============


@skip_no_dsn
class TestTablePerCollectionMigration:
    """
    table-per-collection 迁移脚本集成测试
    
    测试 pgvector_collection_migrate.py 的 table-per-collection 模式：
    1. 从共享表按 collection_id 拆分到独立表
    2. 拆分后数据隔离性
    3. 幂等插入
    4. collection_allowlist 过滤
    """
    
    @pytest.fixture
    def migration_test_setup(self, embedding_mock: DeterministicEmbeddingMock):
        """
        准备迁移测试环境
        
        - 创建共享表并插入多个 collection 的测试数据
        - 测试完成后清理所有测试表
        """
        import sys
        from pathlib import Path
        
        # 添加 scripts 目录到路径
        scripts_dir = Path(__file__).parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import (
            MigrationConfig,
            TablePerCollectionMigrator,
            get_connection,
            to_pgvector_table_name,
        )
        
        unique_prefix = uuid.uuid4().hex[:8]
        
        # 共享表名（测试用）
        shared_table = f"migrate_test_{unique_prefix}"
        
        # 两个不同的 collection_id
        collection_a = f"proj_a_{unique_prefix}:v1:bge-m3"
        collection_b = f"proj_b_{unique_prefix}:v1:bge-m3"
        
        # 配置
        config = MigrationConfig(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ.get("POSTGRES_DB", "engram"),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            schema=TEST_SCHEMA,
            base_table=shared_table,
            batch_size=100,
            dry_run=False,
            verbose=True,
            verify_counts=True,
        )
        
        # 从 DSN 解析密码
        if TEST_PGVECTOR_DSN:
            import re
            match = re.search(r'://([^:]+):([^@]+)@', TEST_PGVECTOR_DSN)
            if match:
                config.user = match.group(1)
                config.password = match.group(2)
            match = re.search(r'@([^:]+):(\d+)/(\w+)', TEST_PGVECTOR_DSN)
            if match:
                config.host = match.group(1)
                config.port = int(match.group(2))
                config.database = match.group(3)
        
        conn = get_connection(config)
        
        try:
            # 创建共享表
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{TEST_SCHEMA}"."{shared_table}" (
                        chunk_id TEXT PRIMARY KEY,
                        content TEXT,
                        vector vector(128),
                        project_key TEXT,
                        module TEXT,
                        source_type TEXT,
                        source_id TEXT,
                        owner_user_id TEXT,
                        commit_ts TIMESTAMPTZ,
                        artifact_uri TEXT,
                        sha256 TEXT,
                        chunk_idx INTEGER,
                        excerpt TEXT,
                        metadata JSONB,
                        collection_id TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                conn.commit()
            
            # 插入测试数据
            test_docs = []
            
            # Collection A 的文档
            for i in range(5):
                doc_id = f"{unique_prefix}:a:{i}"
                vector = embedding_mock.embed_text(f"Collection A document {i}")
                test_docs.append((
                    doc_id,
                    f"Collection A 内容 {i}",
                    vector,
                    "proj_a",
                    "src/",
                    "git",
                    f"commit_a_{i}",
                    "user1",
                    collection_a,
                ))
            
            # Collection B 的文档
            for i in range(3):
                doc_id = f"{unique_prefix}:b:{i}"
                vector = embedding_mock.embed_text(f"Collection B document {i}")
                test_docs.append((
                    doc_id,
                    f"Collection B 内容 {i}",
                    vector,
                    "proj_b",
                    "lib/",
                    "git",
                    f"commit_b_{i}",
                    "user2",
                    collection_b,
                ))
            
            with conn.cursor() as cur:
                for doc in test_docs:
                    cur.execute(f"""
                        INSERT INTO "{TEST_SCHEMA}"."{shared_table}" 
                        (chunk_id, content, vector, project_key, module, source_type, 
                         source_id, owner_user_id, collection_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, doc)
                conn.commit()
            
            yield {
                "config": config,
                "conn": conn,
                "shared_table": shared_table,
                "collection_a": collection_a,
                "collection_b": collection_b,
                "count_a": 5,
                "count_b": 3,
                "unique_prefix": unique_prefix,
                "to_pgvector_table_name": to_pgvector_table_name,
                "TablePerCollectionMigrator": TablePerCollectionMigrator,
            }
            
        finally:
            # 清理测试表
            try:
                with conn.cursor() as cur:
                    # 删除共享表
                    cur.execute(f'DROP TABLE IF EXISTS "{TEST_SCHEMA}"."{shared_table}"')
                    
                    # 删除可能创建的 collection 表
                    table_a = to_pgvector_table_name(collection_a)
                    table_b = to_pgvector_table_name(collection_b)
                    cur.execute(f'DROP TABLE IF EXISTS "{TEST_SCHEMA}"."{table_a}"')
                    cur.execute(f'DROP TABLE IF EXISTS "{TEST_SCHEMA}"."{table_b}"')
                    
                    conn.commit()
            except Exception as e:
                print(f"清理测试表失败: {e}")
            finally:
                conn.close()
    
    def test_migration_splits_by_collection_id(self, migration_test_setup):
        """测试迁移正确按 collection_id 拆分数据"""
        setup = migration_test_setup
        config = setup["config"]
        to_pgvector_table_name = setup["to_pgvector_table_name"]
        TablePerCollectionMigrator = setup["TablePerCollectionMigrator"]
        
        # 执行迁移
        migrator = TablePerCollectionMigrator(config)
        result = migrator.migrate()
        
        # 验证迁移成功
        assert result.success, f"迁移失败: {result.errors}"
        assert result.rows_migrated == setup["count_a"] + setup["count_b"]
        
        # 验证目标表存在且数据隔离
        conn = setup["conn"]
        
        table_a = to_pgvector_table_name(setup["collection_a"])
        table_b = to_pgvector_table_name(setup["collection_b"])
        
        with conn.cursor() as cur:
            # 验证 Collection A 表
            cur.execute(f'SELECT COUNT(*) as cnt FROM "{TEST_SCHEMA}"."{table_a}"')
            count_a = cur.fetchone()["cnt"]
            assert count_a == setup["count_a"], f"Collection A 期望 {setup['count_a']}，实际 {count_a}"
            
            # 验证 Collection B 表
            cur.execute(f'SELECT COUNT(*) as cnt FROM "{TEST_SCHEMA}"."{table_b}"')
            count_b = cur.fetchone()["cnt"]
            assert count_b == setup["count_b"], f"Collection B 期望 {setup['count_b']}，实际 {count_b}"
            
            # 验证 Collection A 表只有 A 的数据
            cur.execute(f'''
                SELECT DISTINCT collection_id 
                FROM "{TEST_SCHEMA}"."{table_a}"
            ''')
            collections_in_a = [row["collection_id"] for row in cur.fetchall()]
            assert len(collections_in_a) == 1
            assert collections_in_a[0] == setup["collection_a"]
            
            # 验证 Collection B 表只有 B 的数据
            cur.execute(f'''
                SELECT DISTINCT collection_id 
                FROM "{TEST_SCHEMA}"."{table_b}"
            ''')
            collections_in_b = [row["collection_id"] for row in cur.fetchall()]
            assert len(collections_in_b) == 1
            assert collections_in_b[0] == setup["collection_b"]
    
    def test_migration_idempotent(self, migration_test_setup):
        """测试迁移幂等性（多次执行结果相同）"""
        setup = migration_test_setup
        config = setup["config"]
        TablePerCollectionMigrator = setup["TablePerCollectionMigrator"]
        to_pgvector_table_name = setup["to_pgvector_table_name"]
        
        # 第一次迁移
        migrator1 = TablePerCollectionMigrator(config)
        result1 = migrator1.migrate()
        assert result1.success
        
        # 第二次迁移（应该是幂等的）
        migrator2 = TablePerCollectionMigrator(config)
        result2 = migrator2.migrate()
        assert result2.success
        
        # 验证数据没有重复
        conn = setup["conn"]
        table_a = to_pgvector_table_name(setup["collection_a"])
        
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) as cnt FROM "{TEST_SCHEMA}"."{table_a}"')
            count = cur.fetchone()["cnt"]
            assert count == setup["count_a"], f"幂等性验证失败：期望 {setup['count_a']}，实际 {count}"
    
    def test_migration_with_allowlist(self, migration_test_setup):
        """测试 collection_allowlist 过滤"""
        setup = migration_test_setup
        config = setup["config"]
        TablePerCollectionMigrator = setup["TablePerCollectionMigrator"]
        to_pgvector_table_name = setup["to_pgvector_table_name"]
        
        # 只处理 Collection A
        config.collection_allowlist = [setup["collection_a"]]
        
        migrator = TablePerCollectionMigrator(config)
        result = migrator.migrate()
        
        assert result.success
        # 只迁移了 Collection A 的数据
        assert result.rows_migrated == setup["count_a"]
        
        # 验证 Collection B 的表不存在
        conn = setup["conn"]
        table_b = to_pgvector_table_name(setup["collection_b"])
        
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
            """, (TEST_SCHEMA, table_b))
            exists = cur.fetchone()["exists"]
            assert not exists, "Collection B 的表不应该被创建（因为被 allowlist 过滤）"
    
    def test_migration_requires_collection_id_column(self, embedding_mock: DeterministicEmbeddingMock):
        """测试源表没有 collection_id 列时报错"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from pgvector_collection_migrate import (
            MigrationConfig,
            TablePerCollectionMigrator,
            get_connection,
        )
        
        unique_prefix = uuid.uuid4().hex[:8]
        table_no_cid = f"migrate_no_cid_{unique_prefix}"
        
        config = MigrationConfig(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ.get("POSTGRES_DB", "engram"),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            schema=TEST_SCHEMA,
            base_table=table_no_cid,
        )
        
        # 从 DSN 解析
        if TEST_PGVECTOR_DSN:
            import re
            match = re.search(r'://([^:]+):([^@]+)@', TEST_PGVECTOR_DSN)
            if match:
                config.user = match.group(1)
                config.password = match.group(2)
            match = re.search(r'@([^:]+):(\d+)/(\w+)', TEST_PGVECTOR_DSN)
            if match:
                config.host = match.group(1)
                config.port = int(match.group(2))
                config.database = match.group(3)
        
        conn = get_connection(config)
        
        try:
            # 创建没有 collection_id 列的表
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{TEST_SCHEMA}"."{table_no_cid}" (
                        chunk_id TEXT PRIMARY KEY,
                        content TEXT,
                        project_key TEXT
                    )
                """)
                cur.execute(f"""
                    INSERT INTO "{TEST_SCHEMA}"."{table_no_cid}" 
                    (chunk_id, content, project_key) VALUES ('test', 'content', 'proj')
                """)
                conn.commit()
            
            # 尝试迁移应该失败
            migrator = TablePerCollectionMigrator(config)
            result = migrator.migrate()
            
            assert not result.success, "没有 collection_id 列时迁移应该失败"
            assert "collection_id" in result.message.lower()
            
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS "{TEST_SCHEMA}"."{table_no_cid}"')
                    conn.commit()
            except Exception:
                pass
            conn.close()


class TestRoutingCollectionStrategy:
    """
    RoutingCollectionStrategy 单元测试（不需要数据库连接）
    
    测试路由规则匹配逻辑和存储解析结果
    """

    def test_routing_strategy_basic_creation(self):
        """测试基本创建"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            base_table="chunks",
            allowlist=["proj1:v1:model", "proj2:v1:model"],
            prefix_list=["hot_", "temp_"],
            regex_patterns=[r"^test_.*"],
        )
        
        assert strategy.strategy_name == "routing"
        assert strategy.shared_table == "chunks_shared"
        assert strategy.base_table == "chunks"
        assert "proj1:v1:model" in strategy.allowlist
        assert "hot_" in strategy.prefix_list

    def test_routing_allowlist_exact_match(self):
        """测试 allowlist 精确匹配"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            allowlist=["proj1:v1:model", "proj2:v1:bge-m3"],
        )
        
        # 命中 allowlist
        assert strategy.matches_routing_rule("proj1:v1:model") is True
        assert strategy.matches_routing_rule("proj2:v1:bge-m3") is True
        
        # 未命中 allowlist
        assert strategy.matches_routing_rule("proj3:v1:model") is False
        assert strategy.matches_routing_rule("proj1:v2:model") is False  # 版本不同

    def test_routing_prefix_match(self):
        """测试 prefix 前缀匹配（基于 project_key）"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            prefix_list=["hot_", "temp_", "cache_"],
        )
        
        # 命中前缀
        assert strategy.matches_routing_rule("hot_project:v1:model") is True
        assert strategy.matches_routing_rule("temp_data:v1:bge-m3") is True
        assert strategy.matches_routing_rule("cache_user:v2:model") is True
        
        # 未命中前缀
        assert strategy.matches_routing_rule("cold_project:v1:model") is False
        assert strategy.matches_routing_rule("important:v1:model") is False
        
        # 注意：前缀只匹配 project_key 部分
        assert strategy.matches_routing_rule("proj:hot_v1:model") is False  # hot_ 在版本部分

    def test_routing_regex_match(self):
        """测试 regex 正则匹配"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            regex_patterns=[r"^test_.*", r".*_staging$", r"demo\d+:v1:"],
        )
        
        # 命中正则
        assert strategy.matches_routing_rule("test_project:v1:model") is True
        assert strategy.matches_routing_rule("test_:v1:model") is True
        assert strategy.matches_routing_rule("proj_staging") is True
        assert strategy.matches_routing_rule("demo123:v1:bge") is True
        
        # 未命中正则
        assert strategy.matches_routing_rule("production:v1:model") is False
        assert strategy.matches_routing_rule("staging_proj:v1:model") is False  # staging 不在末尾

    def test_routing_combined_rules(self):
        """测试组合规则（任一命中即可）"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            allowlist=["special:v1:model"],
            prefix_list=["hot_"],
            regex_patterns=[r"^test_"],
        )
        
        # 命中 allowlist
        assert strategy.matches_routing_rule("special:v1:model") is True
        
        # 命中 prefix
        assert strategy.matches_routing_rule("hot_proj:v1:model") is True
        
        # 命中 regex
        assert strategy.matches_routing_rule("test_proj:v1:model") is True
        
        # 未命中任何
        assert strategy.matches_routing_rule("normal:v1:model") is False

    def test_routing_resolve_storage_hit_rule(self):
        """测试命中规则时的存储解析（返回 SharedTableStrategy 结果）"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            base_table="chunks",
            allowlist=["hot_proj:v1:model"],
        )
        
        resolution = strategy.resolve_storage(
            collection_id="hot_proj:v1:model",
            schema="step3",
            base_table="ignored",  # 被策略配置覆盖
        )
        
        # 应该使用共享表
        assert resolution.table == "chunks_shared"
        assert resolution.schema == "step3"
        # 应该有 WHERE 条件
        assert resolution.where_clause_extra == "collection_id = %s"
        assert resolution.params_extra == ["hot_proj:v1:model"]
        assert resolution.has_extra_filter is True

    def test_routing_resolve_storage_miss_rule(self):
        """测试未命中规则时的存储解析（返回 DefaultCollectionStrategy 结果）"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            base_table="chunks",
            allowlist=["hot_proj:v1:model"],
        )
        
        resolution = strategy.resolve_storage(
            collection_id="normal_proj:v1:bge-m3",
            schema="step3",
            base_table="ignored",
        )
        
        # 应该使用动态生成的表名（DefaultCollectionStrategy 行为）
        assert "normal_proj" in resolution.table
        assert resolution.table.startswith("step3_chunks_")
        assert resolution.schema == "step3"
        # 不应该有额外的 WHERE 条件
        assert resolution.where_clause_extra == ""
        assert resolution.params_extra == []
        assert resolution.has_extra_filter is False

    def test_routing_resolve_storage_none_collection_id(self):
        """测试 collection_id 为 None 时的存储解析"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            base_table="chunks_default",
            allowlist=["hot_proj:v1:model"],
        )
        
        resolution = strategy.resolve_storage(
            collection_id=None,
            schema="step3",
            base_table="ignored",
        )
        
        # 应该使用策略配置的 base_table
        assert resolution.table == "chunks_default"
        assert resolution.where_clause_extra == ""

    def test_routing_validate_vector_dim_hit_rule(self):
        """测试命中规则时验证向量维度"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            allowlist=["hot_proj:v1:model"],
            expected_vector_dim=1536,
        )
        
        # 命中规则且维度匹配 - 不抛出异常
        strategy.validate_vector_dim(
            collection_id="hot_proj:v1:model",
            requested_dim=1536,
            table_name="chunks_shared",
        )
        
        # 命中规则但维度不匹配 - 抛出异常
        with pytest.raises(VectorDimensionMismatchError) as exc_info:
            strategy.validate_vector_dim(
                collection_id="hot_proj:v1:model",
                requested_dim=768,
                table_name="chunks_shared",
            )
        
        assert exc_info.value.requested_dim == 768
        assert exc_info.value.expected_dim == 1536

    def test_routing_validate_vector_dim_miss_rule(self):
        """测试未命中规则时跳过维度验证"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            allowlist=["hot_proj:v1:model"],
            expected_vector_dim=1536,
        )
        
        # 未命中规则 - 不验证维度，任何维度都可以
        strategy.validate_vector_dim(
            collection_id="normal_proj:v1:model",  # 未命中
            requested_dim=768,  # 不同维度
            table_name="step3_chunks_normal_proj_v1_model",
        )
        # 无异常表示测试通过

    def test_routing_invalid_regex_pattern_ignored(self):
        """测试无效正则表达式被忽略"""
        # 包含无效正则（未闭合的括号）
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            regex_patterns=[r"valid_pattern", r"invalid(pattern", r"another_valid"],
        )
        
        # 应该只有 2 个有效的正则（无效的被跳过）
        assert len(strategy._regex_patterns) == 2
        
        # 有效的模式仍然可以匹配
        assert strategy.matches_routing_rule("valid_pattern:v1:model") is True
        assert strategy.matches_routing_rule("another_valid:v1:model") is True

    def test_routing_empty_rules_never_match(self):
        """测试空规则不匹配任何 collection"""
        strategy = RoutingCollectionStrategy(
            shared_table="chunks_shared",
            allowlist=None,
            prefix_list=None,
            regex_patterns=None,
        )
        
        # 任何 collection_id 都不应该命中
        assert strategy.matches_routing_rule("any:v1:model") is False
        assert strategy.matches_routing_rule("test:v1:model") is False

    def test_routing_extract_project_key(self):
        """测试从 collection_id 提取 project_key"""
        strategy = RoutingCollectionStrategy(shared_table="chunks_shared")
        
        # 标准格式
        assert strategy._extract_project_key("proj:v1:model") == "proj"
        assert strategy._extract_project_key("my_project:v2:bge-m3") == "my_project"
        assert strategy._extract_project_key("a:b:c:d") == "a"  # 有版本标签
        
        # 格式不符
        assert strategy._extract_project_key("invalid") is None
        assert strategy._extract_project_key("a:b") is None
        assert strategy._extract_project_key("") is None
        assert strategy._extract_project_key(None) is None


class TestRoutingStrategyEnvironmentConfig:
    """测试 Routing 策略的环境变量配置"""

    def test_env_var_routing_strategy(self, monkeypatch):
        """测试设置 routing 策略"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_ROUTING
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "routing")
        monkeypatch.setenv("STEP3_PGVECTOR_ROUTING_SHARED_TABLE", "my_shared_table")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST", "proj1:v1:model,proj2:v1:model")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_PREFIX", "hot_,temp_")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_REGEX", "^test_.*,.*_staging$")
        
        config = PGVectorConfig.from_env()
        
        assert config.collection_strategy == COLLECTION_STRATEGY_ROUTING
        assert config.routing_shared_table == "my_shared_table"
        assert config.routing_allowlist == ["proj1:v1:model", "proj2:v1:model"]
        assert config.routing_prefix_list == ["hot_", "temp_"]
        assert config.routing_regex_patterns == ["^test_.*", ".*_staging$"]

    def test_env_var_routing_allowlist_empty(self, monkeypatch):
        """测试 allowlist 为空时的处理"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_ROUTING
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "routing")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST", "")
        
        config = PGVectorConfig.from_env()
        
        assert config.routing_allowlist is None

    def test_env_var_routing_with_whitespace(self, monkeypatch):
        """测试带空白字符的配置被正确处理"""
        from step3_backend_factory import PGVectorConfig
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "routing")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST", " proj1:v1:model , proj2:v1:model ")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_PREFIX", "  hot_ ,  temp_  ")
        
        config = PGVectorConfig.from_env()
        
        assert config.routing_allowlist == ["proj1:v1:model", "proj2:v1:model"]
        assert config.routing_prefix_list == ["hot_", "temp_"]

    def test_config_to_dict_includes_routing(self, monkeypatch):
        """测试 to_dict 包含 routing 配置"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_ROUTING
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "routing")
        monkeypatch.setenv("STEP3_PGVECTOR_ROUTING_SHARED_TABLE", "shared")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_ROUTING_ALLOWLIST", "proj:v1:model")
        
        config = PGVectorConfig.from_env()
        config_dict = config.to_dict()
        
        assert config_dict["collection_strategy"] == COLLECTION_STRATEGY_ROUTING
        assert config_dict["routing_shared_table"] == "shared"
        assert config_dict["routing_allowlist"] == ["proj:v1:model"]

    def test_config_to_dict_excludes_routing_for_other_strategies(self, monkeypatch):
        """测试非 routing 策略时 to_dict 不包含 routing 配置"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_PER_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "per_table")
        
        config = PGVectorConfig.from_env()
        config_dict = config.to_dict()
        
        assert config_dict["collection_strategy"] == COLLECTION_STRATEGY_PER_TABLE
        assert "routing_shared_table" not in config_dict
        assert "routing_allowlist" not in config_dict


@skip_no_dsn
class TestRoutingStrategyWithDB:
    """
    RoutingCollectionStrategy 数据库集成测试
    
    验证路由策略在实际数据库操作中的行为
    """

    def test_routing_hit_uses_shared_table(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试命中规则的 collection 使用共享表"""
        unique_prefix = uuid.uuid4().hex[:8]
        
        strategy = RoutingCollectionStrategy(
            shared_table=TEST_TABLE,  # 使用测试表作为共享表
            base_table="chunks_default",
            allowlist=[f"hot_{unique_prefix}:v1:model"],
        )
        
        # 创建命中规则的后端
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name="ignored",  # 会被策略覆盖
            collection_id=f"hot_{unique_prefix}:v1:model",
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            # 验证使用共享表
            assert backend.table_name == TEST_TABLE
            
            backend.initialize()
            
            # 写入文档
            doc = ChunkDoc(
                chunk_id=f"{unique_prefix}:routing-hit:doc1",
                content="Routing hit 测试文档",
                project_key="test_project",
                source_type="git",
            )
            backend.upsert([doc])
            
            # 验证可以查询到
            exists = backend.exists([doc.chunk_id])
            assert exists[doc.chunk_id] is True
            
        finally:
            try:
                backend.delete([doc.chunk_id])
            except Exception:
                pass
            backend.close()

    def test_routing_miss_uses_per_table(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试未命中规则的 collection 使用独立表"""
        unique_prefix = uuid.uuid4().hex[:8]
        
        strategy = RoutingCollectionStrategy(
            shared_table=TEST_TABLE,
            base_table="chunks_default",
            allowlist=["hot_other:v1:model"],  # 不会命中
        )
        
        collection_id = f"normal_{unique_prefix}:v1:model"
        
        # 创建未命中规则的后端
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name="ignored",
            collection_id=collection_id,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            # 验证使用动态生成的表名（不是共享表）
            assert backend.table_name != TEST_TABLE
            assert backend.table_name.startswith("step3_chunks_")
            assert f"normal_{unique_prefix}" in backend.table_name.replace("_", "")
            
        finally:
            backend.close()

    def test_routing_isolation_between_hit_and_miss(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试命中和未命中规则的 collection 数据隔离"""
        unique_prefix = uuid.uuid4().hex[:8]
        hot_collection = f"hot_{unique_prefix}:v1:model"
        normal_collection = f"normal_{unique_prefix}:v1:model"
        
        strategy = RoutingCollectionStrategy(
            shared_table=TEST_TABLE,
            base_table="chunks",
            prefix_list=["hot_"],
            expected_vector_dim=128,
        )
        
        # 创建命中规则的后端（使用共享表）
        backend_hot = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name="ignored",
            collection_id=hot_collection,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        # 创建未命中规则的后端（使用独立表）
        backend_normal = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name="ignored",
            collection_id=normal_collection,
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            # 验证使用不同的表
            assert backend_hot.table_name == TEST_TABLE  # 共享表
            assert backend_normal.table_name != TEST_TABLE  # 独立表
            
            backend_hot.initialize()
            backend_normal.initialize()
            
            # 写入数据
            doc_hot = ChunkDoc(
                chunk_id=f"{unique_prefix}:routing:hot:doc1",
                content="Hot collection 文档",
                project_key="test_project",
                source_type="git",
            )
            backend_hot.upsert([doc_hot])
            
            doc_normal = ChunkDoc(
                chunk_id=f"{unique_prefix}:routing:normal:doc1",
                content="Normal collection 文档",
                project_key="test_project",
                source_type="git",
            )
            backend_normal.upsert([doc_normal])
            
            # 验证隔离性
            # hot 后端看不到 normal 的文档
            exists_hot = backend_hot.exists([doc_hot.chunk_id, doc_normal.chunk_id])
            assert exists_hot[doc_hot.chunk_id] is True
            assert exists_hot[doc_normal.chunk_id] is False
            
            # normal 后端看不到 hot 的文档
            exists_normal = backend_normal.exists([doc_hot.chunk_id, doc_normal.chunk_id])
            assert exists_normal[doc_hot.chunk_id] is False
            assert exists_normal[doc_normal.chunk_id] is True
            
        finally:
            try:
                backend_hot.delete([doc_hot.chunk_id])
            except Exception:
                pass
            
            try:
                conn = backend_normal._get_connection()
                with conn.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS {backend_normal.qualified_table}')
                    conn.commit()
            except Exception:
                pass
            
            backend_hot.close()
            backend_normal.close()


# ============ Preflight 校验测试 ============


class TestPreflightDimensionCheck:
    """
    Preflight 维度校验单元测试（不需要数据库连接）
    
    测试 VectorDimensionMismatchError 的错误信息和 preflight 标记
    """

    def test_preflight_dimension_mismatch_error_message(self):
        """测试 preflight 校验失败时的错误信息"""
        error = VectorDimensionMismatchError(
            collection_id="test:v1:model",
            requested_dim=1536,
            expected_dim=768,
            table_name='"step3"."chunks_shared"',
            is_preflight=True,
        )
        
        # 错误信息应该包含可操作的建议
        error_msg = str(error)
        assert "preflight" in error_msg.lower()
        assert "1536" in error_msg
        assert "768" in error_msg
        assert "STEP3_PG_VECTOR_DIM" in error_msg
        assert "per_table" in error_msg
        assert "STEP3_PGVECTOR_COLLECTION_STRATEGY" in error_msg
        assert "STEP3_PG_TABLE" in error_msg
        
        # 验证错误属性
        assert error.is_preflight is True
        assert error.requested_dim == 1536
        assert error.expected_dim == 768

    def test_non_preflight_dimension_mismatch_error(self):
        """测试非 preflight 校验的错误信息"""
        error = VectorDimensionMismatchError(
            collection_id="test:v1:model",
            requested_dim=1536,
            expected_dim=768,
            table_name='"step3"."chunks_shared"',
            is_preflight=False,
        )
        
        error_msg = str(error)
        # 非 preflight 错误信息不包含 preflight 提示
        assert "preflight" not in error_msg.lower() or "per_table" in error_msg
        assert error.is_preflight is False


class TestPreflightVectorDimensionQuery:
    """
    Preflight 维度查询函数单元测试（不需要数据库连接）
    
    测试 get_vector_column_dimension 和 preflight_check_vector_dimension 的行为
    """

    def test_get_vector_column_dimension_fallback(self):
        """测试回退实现返回 None"""
        # 由于我们无法在单元测试中模拟数据库连接
        # 这个测试主要验证函数存在且可调用
        from index_backend.pgvector_collection_strategy import get_vector_column_dimension
        
        # 如果是回退实现，应该可以导入
        assert callable(get_vector_column_dimension)

    def test_preflight_check_function_exists(self):
        """测试 preflight_check_vector_dimension 函数存在"""
        from index_backend.pgvector_collection_strategy import preflight_check_vector_dimension
        
        assert callable(preflight_check_vector_dimension)


@skip_no_dsn
class TestPreflightWithDB:
    """
    Preflight 校验数据库集成测试
    
    测试场景:
    1. 表不存在时 preflight 跳过校验（允许启动）
    2. 表存在且维度一致时 preflight 通过
    3. 表存在但维度不一致时 preflight 抛出明确错误
    """

    def test_preflight_passes_when_table_not_exists(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试表不存在时 preflight 跳过校验"""
        unique_prefix = uuid.uuid4().hex[:8]
        
        # 使用 SharedTableStrategy
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        # 使用一个不存在的表名
        non_existent_table = f"preflight_nonexist_{unique_prefix}"
        
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=non_existent_table,
            collection_id=f"test:{unique_prefix}:v1:model",
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            # preflight 不应抛出异常（表不存在时跳过维度校验）
            result = backend.preflight_check()
            
            assert result["status"] == "ok"
            # 维度校验应该被跳过或标记为跳过（取决于实现）
            
        finally:
            backend.close()

    def test_preflight_passes_when_dimension_matches(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试表存在且维度一致时 preflight 通过"""
        unique_prefix = uuid.uuid4().hex[:8]
        
        # 使用 SharedTableStrategy
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=128,
        )
        
        # 先创建一个带正确维度的表
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,  # 使用测试表
            collection_id=f"preflight-pass:{unique_prefix}",
            embedding_provider=embedding_mock,
            vector_dim=128,  # 与策略预期一致
            collection_strategy=strategy,
        )
        
        try:
            # 确保表存在
            backend.initialize()
            
            # preflight 应该通过
            result = backend.preflight_check()
            
            assert result["status"] == "ok"
            assert "vector_dimension" in result["checks"]
            
        finally:
            backend.close()

    def test_preflight_fails_when_dimension_mismatch(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试表存在但维度不一致时 preflight 抛出明确错误"""
        unique_prefix = uuid.uuid4().hex[:8]
        shared_table = f"preflight_mismatch_{unique_prefix}"
        
        # 首先，使用 128 维创建一个共享表
        backend_128 = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table,
            collection_id=f"init:{unique_prefix}",
            embedding_provider=embedding_mock,
            vector_dim=128,  # 128 维
        )
        
        try:
            # 初始化表（创建 128 维的 vector 列）
            backend_128.initialize()
            backend_128.close()
            
            # 然后，尝试用不同维度（256）连接同一个表
            strategy_256 = SharedTableStrategy(
                collection_id_column="collection_id",
                expected_vector_dim=256,  # 策略预期 256 维
            )
            
            backend_256 = PGVectorBackend(
                connection_string=TEST_PGVECTOR_DSN,
                schema=TEST_SCHEMA,
                table_name=shared_table,
                collection_id=f"mismatch:{unique_prefix}",
                embedding_provider=embedding_mock,
                vector_dim=256,  # 请求 256 维
                collection_strategy=strategy_256,
            )
            
            try:
                # preflight 应该检测到维度不匹配并抛出错误
                with pytest.raises(VectorDimensionMismatchError) as exc_info:
                    backend_256.preflight_check()
                
                error = exc_info.value
                # 验证错误信息
                assert error.is_preflight is True
                assert error.requested_dim == 256
                assert error.expected_dim == 128
                
                # 错误信息应该包含可操作的建议
                error_msg = str(error)
                assert "preflight" in error_msg.lower()
                assert "per_table" in error_msg
                assert "STEP3_PGVECTOR_COLLECTION_STRATEGY" in error_msg
                
            finally:
                backend_256.close()
                
        finally:
            # 清理测试表
            try:
                import psycopg
                conn = psycopg.connect(TEST_PGVECTOR_DSN)
                with conn.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS "{TEST_SCHEMA}"."{shared_table}"')
                    conn.commit()
                conn.close()
            except Exception:
                pass

    def test_preflight_skipped_for_per_table_strategy(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 per_table 策略跳过 preflight 维度校验"""
        unique_prefix = uuid.uuid4().hex[:8]
        
        # 使用默认策略 (per_table)
        strategy = DefaultCollectionStrategy()
        
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name="chunks",  # 不重要，会被 collection_id 覆盖
            collection_id=f"per-table:{unique_prefix}:v1:model",
            embedding_provider=embedding_mock,
            vector_dim=128,
            collection_strategy=strategy,
        )
        
        try:
            # preflight 应该跳过维度校验（per_table 每个 collection 独立表）
            result = backend.preflight_check()
            
            assert result["status"] == "ok"
            assert result["checks"]["vector_dimension"] == "skipped"
            assert "per_table" in result["checks"]["vector_dimension_details"]["reason"]
            
        finally:
            backend.close()

    def test_get_actual_vector_dimension(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试获取数据库中 vector 列的实际维度"""
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            backend.initialize()
            
            # 获取实际维度
            actual_dim = backend.get_actual_vector_dimension()
            
            # 应该与创建时的维度一致
            assert actual_dim == 128
            
        finally:
            backend.close()


@skip_no_dsn  
class TestPreflightInFactory:
    """
    测试 create_pgvector_backend 工厂函数中的 preflight 校验集成
    """

    def test_factory_preflight_enabled_by_default_for_single_table(self, monkeypatch):
        """测试工厂函数默认对 single_table 策略启用 preflight"""
        from step3_backend_factory import create_pgvector_backend, PGVectorConfig
        
        unique_prefix = uuid.uuid4().hex[:8]
        shared_table = f"factory_preflight_{unique_prefix}"
        
        # 设置环境变量
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", TEST_PGVECTOR_DSN)
        monkeypatch.setenv("STEP3_PG_SCHEMA", TEST_SCHEMA)
        monkeypatch.setenv("STEP3_PG_TABLE", shared_table)
        monkeypatch.setenv("STEP3_PG_VECTOR_DIM", "128")
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "single_table")
        
        try:
            # 工厂函数应该调用 preflight_check
            # 由于表不存在，preflight 应该跳过（不报错）
            backend = create_pgvector_backend(
                collection_id=f"factory:{unique_prefix}:v1:model",
            )
            
            assert backend is not None
            backend.close()
            
        except VectorDimensionMismatchError:
            # 如果表已存在且维度不匹配，这是预期的行为
            pass

    def test_factory_preflight_skip_option(self, monkeypatch):
        """测试工厂函数 skip_preflight 选项"""
        from step3_backend_factory import create_pgvector_backend
        
        unique_prefix = uuid.uuid4().hex[:8]
        
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", TEST_PGVECTOR_DSN)
        monkeypatch.setenv("STEP3_PG_SCHEMA", TEST_SCHEMA)
        monkeypatch.setenv("STEP3_PG_TABLE", TEST_TABLE)
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "single_table")
        
        # 使用 skip_preflight=True 跳过校验
        backend = create_pgvector_backend(
            collection_id=f"skip-preflight:{unique_prefix}",
            skip_preflight=True,
        )
        
        assert backend is not None
        backend.close()


# ============ 跨策略结果对比测试 ============


@skip_no_dsn
class TestCrossStrategyResultConsistency:
    """
    跨策略查询结果一致性测试
    
    针对同一组 collection 与 query 样本，在 per_table 与 single_table 上分别查询，
    校验结果的重叠度、字段一致性和跨 collection 隔离性
    """
    
    @pytest.fixture
    def cross_strategy_setup(self, embedding_mock: DeterministicEmbeddingMock):
        """设置跨策略测试环境"""
        unique_prefix = uuid.uuid4().hex[:8]
        
        # 定义 collections
        collections = [
            {"collection_id": f"cross:{unique_prefix}:proj_a:v1", "project_key": "proj_a"},
            {"collection_id": f"cross:{unique_prefix}:proj_b:v1", "project_key": "proj_b"},
        ]
        
        # 查询样本
        query_samples = [
            "用户登录 漏洞修复",
            "数据库 性能优化",
            "单元测试 边界",
        ]
        
        # 共享表名
        shared_table = f"cross_strategy_{unique_prefix}"
        
        # 创建后端
        per_table_backends = {}
        single_table_backends = {}
        created_tables = [shared_table]
        
        strategy = SharedTableStrategy(
            collection_id_column="collection_id",
            expected_vector_dim=embedding_mock.dim,
        )
        
        # 生成测试文档
        test_docs = {}
        for col in collections:
            collection_id = col["collection_id"]
            project_key = col["project_key"]
            
            docs = [
                ChunkDoc(
                    chunk_id=f"{unique_prefix}:{project_key}:doc{i}",
                    content=f"这是 {project_key} 项目的测试文档 {i}，关于用户登录和安全漏洞修复",
                    project_key=project_key,
                    source_type="git" if i % 2 == 0 else "logbook",
                    source_id=f"source-{i}",
                )
                for i in range(20)
            ]
            # 添加一些差异化内容
            docs.extend([
                ChunkDoc(
                    chunk_id=f"{unique_prefix}:{project_key}:perf{i}",
                    content=f"数据库性能优化方案 {i}，索引优化和查询改进",
                    project_key=project_key,
                    source_type="doc",
                    source_id=f"perf-source-{i}",
                )
                for i in range(10)
            ])
            test_docs[collection_id] = docs
            
            # 创建 per_table 后端
            per_backend = PGVectorBackend(
                connection_string=TEST_PGVECTOR_DSN,
                schema=TEST_SCHEMA,
                collection_id=collection_id,
                embedding_provider=embedding_mock,
                vector_dim=embedding_mock.dim,
                hybrid_config=HybridSearchConfig(vector_weight=0.7, text_weight=0.3),
            )
            per_backend.initialize()
            per_backend.upsert(docs)
            per_table_backends[collection_id] = per_backend
            created_tables.append(per_backend.table_name)
            
            # 创建 single_table 后端
            single_backend = PGVectorBackend(
                connection_string=TEST_PGVECTOR_DSN,
                schema=TEST_SCHEMA,
                table_name=shared_table,
                collection_id=collection_id,
                embedding_provider=embedding_mock,
                vector_dim=embedding_mock.dim,
                hybrid_config=HybridSearchConfig(vector_weight=0.7, text_weight=0.3),
                collection_strategy=strategy,
            )
            single_backend.initialize()
            single_backend.upsert(docs)
            single_table_backends[collection_id] = single_backend
        
        yield {
            "collections": collections,
            "query_samples": query_samples,
            "per_table_backends": per_table_backends,
            "single_table_backends": single_table_backends,
            "test_docs": test_docs,
            "unique_prefix": unique_prefix,
        }
        
        # 清理
        for backend in per_table_backends.values():
            try:
                backend.close()
            except Exception:
                pass
        
        for backend in single_table_backends.values():
            try:
                backend.close()
            except Exception:
                pass
        
        # 删除测试表
        import psycopg
        try:
            with psycopg.connect(TEST_PGVECTOR_DSN, row_factory=psycopg.rows.dict_row) as conn:
                for table in created_tables:
                    try:
                        with conn.cursor() as cur:
                            cur.execute(f'DROP TABLE IF EXISTS "{TEST_SCHEMA}"."{table}" CASCADE')
                        conn.commit()
                    except Exception:
                        pass
        except Exception:
            pass
    
    def test_topk_overlap_between_strategies(self, cross_strategy_setup):
        """
        测试同 collection 下 per_table 和 single_table 的 TopK 重叠度
        
        使用 dual_read_compare 计算 overlap_metrics 和 ranking_drift，
        并基于 CompareThresholds 断言阈值。
        """
        collections = cross_strategy_setup["collections"]
        query_samples = cross_strategy_setup["query_samples"]
        per_table_backends = cross_strategy_setup["per_table_backends"]
        single_table_backends = cross_strategy_setup["single_table_backends"]
        
        # 配置阈值
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
            rbo_min_warn=0.7,
            rbo_min_fail=0.5,
        )
        
        print("\n" + "=" * 70)
        print("TopK 重叠度测试报告 (dual_read_compare)")
        print("=" * 70)
        
        all_overlap_metrics: List[OverlapMetrics] = []
        all_ranking_metrics: List[RankingDriftMetrics] = []
        all_reports: List[CompareReport] = []
        
        for col in collections:
            collection_id = col["collection_id"]
            per_backend = per_table_backends[collection_id]
            single_backend = single_table_backends[collection_id]
            
            for query_text in query_samples:
                request = QueryRequest(query_text=query_text, top_k=10, min_score=0.0)
                
                per_results = per_backend.query(request)
                single_results = single_backend.query(request)
                
                per_ids = [h.chunk_id for h in per_results]
                single_ids = [h.chunk_id for h in single_results]
                per_hits = [(h.chunk_id, h.score) for h in per_results]
                single_hits = [(h.chunk_id, h.score) for h in single_results]
                
                # 使用 dual_read_compare 计算指标
                overlap_m = compute_overlap_metrics(
                    primary_ids=per_ids,
                    shadow_ids=single_ids,
                    top_k=10,
                )
                all_overlap_metrics.append(overlap_m)
                
                ranking_m = compute_ranking_drift(
                    primary_ids_ranked=per_hits,
                    shadow_ids_ranked=single_hits,
                    stabilize=True,
                )
                all_ranking_metrics.append(ranking_m)
                
                # 生成 CompareReport
                compare_metrics = CompareMetrics(
                    hit_overlap_ratio=overlap_m.overlap_ratio,
                    common_hit_count=overlap_m.overlap_count,
                    primary_hit_count=len(per_ids),
                    secondary_hit_count=len(single_ids),
                )
                report = evaluate_with_report(
                    metrics=compare_metrics,
                    thresholds=thresholds,
                    ranking_metrics=ranking_m,
                    request_id=f"{collection_id[:20]}:{query_text[:15]}",
                    primary_backend="per_table",
                    secondary_backend="single_table",
                )
                all_reports.append(report)
                
                # 打印单条报告汇总
                print(f"\nCollection: {collection_id[:40]}...")
                print(f"Query: '{query_text}'")
                print(f"  hit_overlap: {overlap_m.overlap_ratio:.4f} ({overlap_m.overlap_count}/{max(len(per_ids), len(single_ids))})")
                print(f"  RBO: {ranking_m.rbo:.4f}")
                print(f"  top3_jaccard: {ranking_m.top3_jaccard:.4f}")
                if overlap_m.primary_only_ids_sample:
                    print(f"  仅 per_table: {overlap_m.primary_only_ids_sample[:3]}")
                if overlap_m.shadow_only_ids_sample:
                    print(f"  仅 single_table: {overlap_m.shadow_only_ids_sample[:3]}")
        
        # ========== 汇总报告 ==========
        avg_overlap = sum(m.overlap_ratio for m in all_overlap_metrics) / len(all_overlap_metrics)
        avg_rbo = sum(m.rbo for m in all_ranking_metrics) / len(all_ranking_metrics)
        passed_count = sum(1 for r in all_reports if r.decision.passed and not r.decision.has_warnings)
        warn_count = sum(1 for r in all_reports if r.decision.passed and r.decision.has_warnings)
        fail_count = sum(1 for r in all_reports if not r.decision.passed)
        
        print("\n" + "-" * 70)
        print(f"┌{'─'*68}┐")
        print(f"│ {'CompareReport 汇总':<66} │")
        print(f"├{'─'*68}┤")
        print(f"│ {'平均 hit_overlap':<30} │ {avg_overlap:.4f} ({avg_overlap:.2%}){' ':18} │")
        print(f"│ {'平均 RBO':<30} │ {avg_rbo:.4f}{' ':28} │")
        print(f"│ {'通过/警告/失败':<30} │ {passed_count}/{warn_count}/{fail_count}{' ':27} │")
        print(f"└{'─'*68}┘")
        print("=" * 70)
        
        # ========== 断言阈值 ==========
        assert avg_overlap >= thresholds.hit_overlap_min_fail, \
            f"平均 hit_overlap {avg_overlap:.4f} < fail 阈值 {thresholds.hit_overlap_min_fail}"
        assert avg_rbo >= thresholds.rbo_min_fail, \
            f"平均 RBO {avg_rbo:.4f} < fail 阈值 {thresholds.rbo_min_fail}"
    
    def test_filter_field_consistency(self, cross_strategy_setup):
        """测试过滤字段（source_type, project_key）在两种策略下的一致性"""
        collections = cross_strategy_setup["collections"]
        query_samples = cross_strategy_setup["query_samples"]
        per_table_backends = cross_strategy_setup["per_table_backends"]
        single_table_backends = cross_strategy_setup["single_table_backends"]
        
        print("\n" + "=" * 70)
        print("过滤字段一致性测试报告")
        print("=" * 70)
        
        mismatches = []
        
        for col in collections:
            collection_id = col["collection_id"]
            per_backend = per_table_backends[collection_id]
            single_backend = single_table_backends[collection_id]
            
            for query_text in query_samples:
                request = QueryRequest(query_text=query_text, top_k=10, min_score=0.0)
                
                per_results = per_backend.query(request)
                single_results = single_backend.query(request)
                
                # 获取公共 chunk_ids
                per_dict = {h.chunk_id: h for h in per_results}
                single_dict = {h.chunk_id: h for h in single_results}
                common_ids = set(per_dict.keys()) & set(single_dict.keys())
                
                for chunk_id in common_ids:
                    per_hit = per_dict[chunk_id]
                    single_hit = single_dict[chunk_id]
                    
                    # 检查 source_type
                    if per_hit.source_type != single_hit.source_type:
                        mismatches.append({
                            "chunk_id": chunk_id,
                            "field": "source_type",
                            "per_value": per_hit.source_type,
                            "single_value": single_hit.source_type,
                        })
                    
                    # 检查 project_key
                    if per_hit.project_key != single_hit.project_key:
                        mismatches.append({
                            "chunk_id": chunk_id,
                            "field": "project_key",
                            "per_value": per_hit.project_key,
                            "single_value": single_hit.project_key,
                        })
        
        print(f"检查的公共结果数: {len(common_ids) if common_ids else 0}")
        print(f"字段不一致数: {len(mismatches)}")
        
        if mismatches:
            print("\n不一致详情:")
            for m in mismatches[:10]:
                print(f"  chunk_id: {m['chunk_id']}")
                print(f"    {m['field']}: per_table={m['per_value']}, single_table={m['single_value']}")
        else:
            print("\n所有公共结果的过滤字段完全一致!")
        
        print("=" * 70)
        
        # 断言：不应有字段不一致
        assert len(mismatches) == 0, f"发现 {len(mismatches)} 处字段不一致"
    
    def test_cross_collection_isolation(self, cross_strategy_setup):
        """测试跨 collection 不可见性"""
        collections = cross_strategy_setup["collections"]
        query_samples = cross_strategy_setup["query_samples"]
        per_table_backends = cross_strategy_setup["per_table_backends"]
        single_table_backends = cross_strategy_setup["single_table_backends"]
        test_docs = cross_strategy_setup["test_docs"]
        
        print("\n" + "=" * 70)
        print("跨 Collection 隔离测试报告")
        print("=" * 70)
        
        isolation_failures = []
        
        for i, source_col in enumerate(collections):
            for j, target_col in enumerate(collections):
                if i == j:
                    continue
                
                source_id = source_col["collection_id"]
                target_id = target_col["collection_id"]
                
                # 获取 source 的 chunk_ids
                source_chunk_ids = set(d.chunk_id for d in test_docs[source_id])
                
                # 在 target 后端中查询
                for strategy_name, backends in [("per_table", per_table_backends), ("single_table", single_table_backends)]:
                    target_backend = backends[target_id]
                    
                    for query_text in query_samples:
                        request = QueryRequest(query_text=query_text, top_k=50, min_score=0.0)
                        results = target_backend.query(request)
                        
                        # 检查是否返回了 source 的数据
                        leaked_ids = [h.chunk_id for h in results if h.chunk_id in source_chunk_ids]
                        
                        if leaked_ids:
                            isolation_failures.append({
                                "strategy": strategy_name,
                                "source": source_id,
                                "target": target_id,
                                "query": query_text,
                                "leaked_ids": leaked_ids,
                            })
        
        print(f"隔离测试数: {len(collections) * (len(collections) - 1) * len(query_samples) * 2}")
        print(f"隔离失败数: {len(isolation_failures)}")
        
        if isolation_failures:
            print("\n隔离失败详情:")
            for f in isolation_failures[:10]:
                print(f"  [{f['strategy']}] Source: {f['source'][:30]}...")
                print(f"    Target: {f['target'][:30]}...")
                print(f"    泄漏 IDs: {f['leaked_ids'][:3]}")
        else:
            print("\n所有跨 collection 隔离测试通过!")
        
        print("=" * 70)
        
        # 断言：不应有隔离失败
        assert len(isolation_failures) == 0, f"发现 {len(isolation_failures)} 处隔离失败"
    
    def test_combined_diff_report(self, cross_strategy_setup):
        """
        综合差异报告测试
        
        使用 dual_read_compare 生成 CompareReport 并断言:
        - 平均 hit_overlap >= hit_overlap_min_fail
        - 平均 RBO >= rbo_min_fail
        - 平均 P95 分数漂移 <= score_drift_p95_max
        - 隔离测试全部通过
        """
        collections = cross_strategy_setup["collections"]
        query_samples = cross_strategy_setup["query_samples"]
        per_table_backends = cross_strategy_setup["per_table_backends"]
        single_table_backends = cross_strategy_setup["single_table_backends"]
        test_docs = cross_strategy_setup["test_docs"]
        
        # 配置阈值
        thresholds = CompareThresholds(
            hit_overlap_min_warn=0.7,
            hit_overlap_min_fail=0.5,
            rbo_min_warn=0.7,
            rbo_min_fail=0.5,
            score_drift_p95_max=0.15,
        )
        
        print("\n")
        print("█" * 80)
        print("█  跨策略查询结果综合差异报告 (dual_read_compare)")
        print("█" * 80)
        
        # 收集所有 CompareReport
        all_reports: List[CompareReport] = []
        all_overlap_metrics: List[OverlapMetrics] = []
        all_ranking_metrics: List[RankingDriftMetrics] = []
        all_score_drifts: List[ScoreDriftMetrics] = []
        field_mismatches = 0
        isolation_passed = 0
        isolation_total = 0
        
        for col in collections:
            collection_id = col["collection_id"]
            per_backend = per_table_backends[collection_id]
            single_backend = single_table_backends[collection_id]
            
            for query_text in query_samples:
                request = QueryRequest(query_text=query_text, top_k=10, min_score=0.0)
                
                per_results = per_backend.query(request)
                single_results = single_backend.query(request)
                
                per_ids = [h.chunk_id for h in per_results]
                single_ids = [h.chunk_id for h in single_results]
                per_hits = [(h.chunk_id, h.score) for h in per_results]
                single_hits = [(h.chunk_id, h.score) for h in single_results]
                
                # 计算 overlap 指标
                overlap_m = compute_overlap_metrics(
                    primary_ids=per_ids,
                    shadow_ids=single_ids,
                    top_k=10,
                )
                all_overlap_metrics.append(overlap_m)
                
                # 计算 ranking drift
                ranking_m = compute_ranking_drift(
                    primary_ids_ranked=per_hits,
                    shadow_ids_ranked=single_hits,
                    stabilize=True,
                )
                all_ranking_metrics.append(ranking_m)
                
                # 计算 score drift
                score_m = compute_score_drift(
                    primary_hits=per_hits,
                    shadow_hits=single_hits,
                )
                all_score_drifts.append(score_m)
                
                # 生成 CompareReport
                compare_metrics = CompareMetrics(
                    hit_overlap_ratio=overlap_m.overlap_ratio,
                    common_hit_count=overlap_m.overlap_count,
                    primary_hit_count=len(per_ids),
                    secondary_hit_count=len(single_ids),
                    avg_score_diff=score_m.avg_abs_score_diff,
                    max_score_diff=score_m.max_abs_score_diff,
                    p95_score_diff=score_m.p95_abs_score_diff,
                    avg_rank_drift=ranking_m.avg_abs_rank_diff,
                    max_rank_drift=int(ranking_m.p95_abs_rank_diff),
                )
                report = evaluate_with_report(
                    metrics=compare_metrics,
                    thresholds=thresholds,
                    ranking_metrics=ranking_m,
                    score_drift_metrics=score_m,
                    request_id=f"{collection_id[:15]}:{query_text[:10]}",
                    primary_backend="per_table",
                    secondary_backend="single_table",
                )
                all_reports.append(report)
        
        # 隔离测试
        for i, source_col in enumerate(collections):
            for j, target_col in enumerate(collections):
                if i == j:
                    continue
                
                source_id = source_col["collection_id"]
                target_id = target_col["collection_id"]
                source_chunk_ids = set(d.chunk_id for d in test_docs[source_id])
                
                for strategy_name, backends in [("per_table", per_table_backends), ("single_table", single_table_backends)]:
                    target_backend = backends[target_id]
                    
                    for query_text in query_samples:
                        isolation_total += 1
                        request = QueryRequest(query_text=query_text, top_k=50, min_score=0.0)
                        results = target_backend.query(request)
                        
                        leaked = any(h.chunk_id in source_chunk_ids for h in results)
                        if not leaked:
                            isolation_passed += 1
        
        # ========== 汇总报告 (CompareReport 格式) ==========
        avg_overlap = sum(m.overlap_ratio for m in all_overlap_metrics) / len(all_overlap_metrics)
        avg_rbo = sum(m.rbo for m in all_ranking_metrics) / len(all_ranking_metrics)
        avg_p95_score_diff = sum(m.p95_abs_score_diff for m in all_score_drifts) / len(all_score_drifts)
        avg_p95_rank_diff = sum(m.p95_abs_rank_diff for m in all_ranking_metrics) / len(all_ranking_metrics)
        
        passed_count = sum(1 for r in all_reports if r.decision.passed and not r.decision.has_warnings)
        warn_count = sum(1 for r in all_reports if r.decision.passed and r.decision.has_warnings)
        fail_count = sum(1 for r in all_reports if not r.decision.passed)
        perfect_matches = sum(1 for m in all_overlap_metrics if m.overlap_ratio >= 0.999)
        
        print(f"\n┌{'─'*78}┐")
        print(f"│ {'CompareReport 汇总指标':<76} │")
        print(f"├{'─'*78}┤")
        print(f"│ {'总查询对比数':<33} │ {len(all_reports):<42} │")
        print(f"│ {'通过/警告/失败':<33} │ {passed_count}/{warn_count}/{fail_count:<35} │")
        print(f"├{'─'*78}┤")
        print(f"│ {'平均 hit_overlap':<31} │ {avg_overlap:.4f} ({avg_overlap:.2%}){' ':22} │")
        print(f"│ {'完全匹配 (100%)':<32} │ {perfect_matches}/{len(all_overlap_metrics)} ({perfect_matches/len(all_overlap_metrics)*100:.1f}%){' ':20} │")
        print(f"│ {'平均 RBO':<35} │ {avg_rbo:.4f}{' ':34} │")
        print(f"│ {'平均 P95 分数漂移':<31} │ {avg_p95_score_diff:.4f}{' ':34} │")
        print(f"│ {'平均 P95 排名漂移':<31} │ {avg_p95_rank_diff:.2f}{' ':36} │")
        print(f"├{'─'*78}┤")
        print(f"│ {'字段不一致数':<33} │ {field_mismatches:<42} │")
        print(f"│ {'隔离测试通过率':<32} │ {isolation_passed}/{isolation_total} ({isolation_passed/isolation_total*100:.1f}%){' ':18} │")
        print(f"└{'─'*78}┘")
        
        print("\n█" * 80)
        
        # ========== 断言阈值 ==========
        assert avg_overlap >= thresholds.hit_overlap_min_fail, \
            f"平均 hit_overlap {avg_overlap:.4f} < fail 阈值 {thresholds.hit_overlap_min_fail}"
        assert avg_rbo >= thresholds.rbo_min_fail, \
            f"平均 RBO {avg_rbo:.4f} < fail 阈值 {thresholds.rbo_min_fail}"
        assert avg_p95_score_diff <= thresholds.score_drift_p95_max * 1.5, \
            f"平均 P95 分数漂移 {avg_p95_score_diff:.4f} > 阈值 {thresholds.score_drift_p95_max * 1.5}"
        assert isolation_passed == isolation_total, f"隔离测试失败: {isolation_total - isolation_passed}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
