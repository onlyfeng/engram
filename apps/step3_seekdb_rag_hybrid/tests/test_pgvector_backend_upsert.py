#!/usr/bin/env python3
"""
test_pgvector_backend_upsert.py - PGVector 后端 upsert 幂等性和 search 排序稳定性测试

测试内容：
- upsert 幂等性：多次 upsert 相同文档，结果一致
- search 排序稳定性：相同查询返回相同排序结果
- 使用 deterministic embedding mock 确保测试可重复

注意：这些测试不需要真实的 PostgreSQL 连接，使用 mock 进行单元测试。
如需集成测试，请配置 TEST_PG_CONNECTION_STRING 环境变量。
"""

import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass
import json

# 路径配置在 conftest.py 中完成
from index_backend.types import ChunkDoc, QueryRequest, QueryHit
from index_backend.pgvector_backend import (
    PGVectorBackend,
    HybridSearchConfig,
    create_pgvector_backend,
    FilterDSLTranslator,
)


# ============ Deterministic Embedding Mock ============


class DeterministicEmbeddingMock:
    """
    确定性 Embedding Mock
    
    对于相同的文本，总是返回相同的向量。
    使用简单的 hash 算法生成确定性向量。
    """
    
    def __init__(self, dim: int = 8):
        """
        初始化 Mock
        
        Args:
            dim: 向量维度（测试用小维度）
        """
        self.dim = dim
        self._model_id = "deterministic-mock"
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    def embed_text(self, text: str) -> List[float]:
        """生成确定性向量"""
        return self._text_to_vector(text)
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量生成确定性向量"""
        return [self._text_to_vector(t) for t in texts]
    
    def _text_to_vector(self, text: str) -> List[float]:
        """
        将文本转换为确定性向量
        
        使用字符的 ASCII 值生成向量，确保相同文本产生相同向量。
        """
        # 使用文本的 hash 值作为种子
        hash_val = hash(text)
        vector = []
        for i in range(self.dim):
            # 生成 -1 到 1 之间的确定性值
            val = ((hash_val + i * 31) % 1000) / 500.0 - 1.0
            vector.append(val)
        
        # 归一化
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        
        return vector


# ============ Mock Database Cursor ============


class MockCursor:
    """模拟数据库游标"""
    
    def __init__(self, data: Optional[Dict] = None):
        self._data = data or {}
        self._stored_docs: Dict[str, Dict] = {}
        self._last_query = None
        self._last_params = None
        self._fetchall_result = []
        self._fetchone_result = None
        self._rowcount = 0
    
    def execute(self, query, params=None):
        self._last_query = query
        self._last_params = params
        
        # 将 Composed 对象转换为字符串以便检查
        query_str = str(query) if hasattr(query, 'as_string') or hasattr(query, '__iter__') else query
        query_upper = query_str.upper() if isinstance(query_str, str) else str(query_str).upper()
        
        # 模拟 INSERT/UPDATE (upsert)
        if "INSERT" in query_upper and "ON CONFLICT" in query_upper:
            if params:
                # 断言参数数量与 SQL 字段列表一致（15 个字段）
                # 字段顺序: chunk_id, content, vector, project_key, module,
                #           source_type, source_id, owner_user_id, commit_ts,
                #           artifact_uri, sha256, chunk_idx, excerpt, metadata,
                #           collection_id
                assert len(params) == 15, f"upsert params 应为 15 个，实际: {len(params)}"
                
                chunk_id = params[0]
                self._stored_docs[chunk_id] = {
                    "chunk_id": chunk_id,
                    "content": params[1],
                    "vector": params[2],
                    "project_key": params[3],
                    "module": params[4],
                    "source_type": params[5],
                    "source_id": params[6],
                    "owner_user_id": params[7],
                    "commit_ts": params[8],
                    "artifact_uri": params[9],
                    "sha256": params[10],
                    "chunk_idx": params[11],
                    "excerpt": params[12],
                    "metadata": params[13],
                    "collection_id": params[14],  # 第15个参数
                }
                self._rowcount = 1
        
        # 模拟 DELETE
        elif "DELETE" in query_upper:
            if params and "IN" in query_upper:
                for chunk_id in params:
                    if chunk_id in self._stored_docs:
                        del self._stored_docs[chunk_id]
                        self._rowcount += 1
        
        # 模拟 SELECT COUNT
        elif "COUNT" in query_upper:
            self._fetchone_result = {"count": len(self._stored_docs)}
        
        # 模拟 SELECT 1 (health check)
        elif query_str.strip() == "SELECT 1" if isinstance(query_str, str) else False:
            self._fetchone_result = {"result": 1}
    
    def fetchall(self):
        return self._fetchall_result
    
    def fetchone(self):
        return self._fetchone_result
    
    @property
    def rowcount(self):
        return self._rowcount
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        pass


class MockConnection:
    """模拟数据库连接"""
    
    def __init__(self):
        self._cursor = MockCursor()
        self._committed = False
        self._rolledback = False
    
    def cursor(self):
        return self._cursor
    
    def commit(self):
        self._committed = True
    
    def rollback(self):
        self._rolledback = True
    
    def close(self):
        pass


# ============ 测试类 ============


class TestUpsertIdempotency:
    """upsert 幂等性测试"""

    @pytest.fixture
    def embedding_mock(self) -> DeterministicEmbeddingMock:
        """创建确定性 Embedding Mock"""
        return DeterministicEmbeddingMock(dim=8)

    @pytest.fixture
    def sample_doc(self) -> ChunkDoc:
        """创建示例文档"""
        return ChunkDoc(
            chunk_id="test:git:abc123:sha256:v1:0",
            content="这是一段测试内容，用于验证 upsert 幂等性。",
            project_key="test_project",
            module="src/test/",
            source_type="git",
            source_id="abc123",
            owner_user_id="user001",
            commit_ts="2024-06-15T10:30:00Z",
            artifact_uri="memory://test/git/abc123/sha256",
            sha256="sha256hash",
            chunk_idx=0,
            excerpt="测试摘要",
            metadata={"tag": "unit-test"},
        )

    def test_deterministic_embedding_consistency(self, embedding_mock: DeterministicEmbeddingMock):
        """验证 Embedding Mock 的确定性"""
        text = "测试文本"
        
        # 多次调用应该返回相同的向量
        vec1 = embedding_mock.embed_text(text)
        vec2 = embedding_mock.embed_text(text)
        vec3 = embedding_mock.embed_text(text)
        
        assert vec1 == vec2 == vec3
        assert len(vec1) == embedding_mock.dim

    def test_different_texts_different_vectors(self, embedding_mock: DeterministicEmbeddingMock):
        """验证不同文本产生不同向量"""
        vec1 = embedding_mock.embed_text("文本A")
        vec2 = embedding_mock.embed_text("文本B")
        
        assert vec1 != vec2

    def test_upsert_generates_vector_if_missing(
        self,
        sample_doc: ChunkDoc,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 upsert 自动生成向量"""
        # 文档没有向量
        assert sample_doc.vector is None
        
        # 创建 mock 后端，直接注入 mock 连接
        mock_conn = MockConnection()
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
        )
        backend._conn = mock_conn
        
        # 执行 upsert
        result = backend.upsert([sample_doc])
        
        assert result == 1
        # 文档应该被赋予向量
        assert sample_doc.vector is not None
        assert len(sample_doc.vector) == embedding_mock.dim

    def test_upsert_idempotent_same_result(
        self,
        sample_doc: ChunkDoc,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试多次 upsert 相同文档，结果一致"""
        mock_conn = MockConnection()
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
        )
        backend._conn = mock_conn
        
        # 第一次 upsert
        doc1 = ChunkDoc(
            chunk_id="idempotent:test:1",
            content="幂等性测试内容",
            project_key="test",
        )
        result1 = backend.upsert([doc1])
        vector1 = doc1.vector.copy() if doc1.vector else None
        
        # 重置向量，模拟重新 upsert
        doc1.vector = None
        result2 = backend.upsert([doc1])
        vector2 = doc1.vector.copy() if doc1.vector else None
        
        # 结果应该一致
        assert result1 == result2 == 1
        assert vector1 == vector2

    def test_upsert_updates_existing_doc(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 upsert 更新已存在的文档"""
        mock_conn = MockConnection()
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
        )
        backend._conn = mock_conn
        
        # 第一次插入
        doc_v1 = ChunkDoc(
            chunk_id="update:test:1",
            content="版本1内容",
            project_key="test",
        )
        backend.upsert([doc_v1])
        
        # 更新内容
        doc_v2 = ChunkDoc(
            chunk_id="update:test:1",  # 相同 ID
            content="版本2内容（更新）",
            project_key="test",
        )
        result = backend.upsert([doc_v2])
        
        # 应该成功更新
        assert result == 1
        # 向量应该根据新内容生成
        assert doc_v2.vector is not None

    def test_upsert_batch_processing(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试批量 upsert"""
        mock_conn = MockConnection()
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
        )
        backend._conn = mock_conn
        
        # 批量文档
        docs = [
            ChunkDoc(chunk_id=f"batch:test:{i}", content=f"内容{i}", project_key="test")
            for i in range(5)
        ]
        
        result = backend.upsert(docs)
        
        assert result == 5
        # 所有文档应该有向量
        for doc in docs:
            assert doc.vector is not None

    def test_upsert_stores_collection_id_from_backend(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 upsert 存储 backend 级别的 collection_id"""
        mock_conn = MockConnection()
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
            collection_id="test_collection_001",  # 设置 backend 的 collection_id
        )
        backend._conn = mock_conn
        
        doc = ChunkDoc(
            chunk_id="coll:test:1",
            content="测试 collection_id 存储",
            project_key="test",
        )
        
        result = backend.upsert([doc])
        
        assert result == 1
        # 验证存储的记录包含正确的 collection_id
        stored_doc = mock_conn._cursor._stored_docs.get("coll:test:1")
        assert stored_doc is not None
        assert stored_doc["collection_id"] == "test_collection_001"

    def test_upsert_allows_none_collection_id_when_not_set(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """测试 backend 未设置 collection_id 时允许为 None"""
        mock_conn = MockConnection()
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
            # 不设置 collection_id，默认为 None
        )
        backend._conn = mock_conn
        
        doc = ChunkDoc(
            chunk_id="no_coll:test:1",
            content="测试无 collection_id",
            project_key="test",
        )
        
        result = backend.upsert([doc])
        
        assert result == 1
        # 验证存储的记录 collection_id 为 None
        stored_doc = mock_conn._cursor._stored_docs.get("no_coll:test:1")
        assert stored_doc is not None
        assert stored_doc["collection_id"] is None


class TestSearchStability:
    """search 排序稳定性测试"""

    @pytest.fixture
    def embedding_mock(self) -> DeterministicEmbeddingMock:
        return DeterministicEmbeddingMock(dim=8)

    def test_hybrid_config_normalization(self):
        """测试 HybridSearchConfig 权重归一化"""
        # 权重总和为 1
        config1 = HybridSearchConfig(vector_weight=0.7, text_weight=0.3)
        assert abs(config1.vector_weight + config1.text_weight - 1.0) < 0.001
        
        # 权重总和不为 1，应该自动归一化
        config2 = HybridSearchConfig(vector_weight=7, text_weight=3)
        assert abs(config2.vector_weight - 0.7) < 0.001
        assert abs(config2.text_weight - 0.3) < 0.001

    def test_query_vector_deterministic(self, embedding_mock: DeterministicEmbeddingMock):
        """测试查询向量的确定性"""
        query_text = "测试查询"
        
        # 多次生成查询向量
        vec1 = embedding_mock.embed_text(query_text)
        vec2 = embedding_mock.embed_text(query_text)
        
        assert vec1 == vec2

    def test_search_same_query_same_order(self, embedding_mock: DeterministicEmbeddingMock):
        """
        测试相同查询返回相同排序
        
        由于使用确定性 embedding 和固定的排序规则（hybrid_score DESC, chunk_id ASC），
        相同查询应该总是返回相同顺序的结果。
        """
        # 准备模拟查询结果
        mock_rows = [
            {
                "chunk_id": "test:1",
                "content": "内容1",
                "project_key": "test",
                "module": "src/",
                "source_type": "git",
                "source_id": "abc",
                "owner_user_id": "user1",
                "commit_ts": "2024-01-01T00:00:00Z",
                "artifact_uri": "uri1",
                "sha256": "sha1",
                "chunk_idx": 0,
                "excerpt": "摘要1",
                "metadata": None,
                "vector_score": 0.95,
                "text_score": 0.8,
                "hybrid_score": 0.905,
            },
            {
                "chunk_id": "test:2",
                "content": "内容2",
                "project_key": "test",
                "module": "src/",
                "source_type": "git",
                "source_id": "def",
                "owner_user_id": "user1",
                "commit_ts": "2024-01-02T00:00:00Z",
                "artifact_uri": "uri2",
                "sha256": "sha2",
                "chunk_idx": 0,
                "excerpt": "摘要2",
                "metadata": None,
                "vector_score": 0.90,
                "text_score": 0.85,
                "hybrid_score": 0.885,
            },
        ]
        
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = mock_rows
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
        )
        backend._conn = mock_conn
        
        request = QueryRequest(
            query_text="测试查询",
            top_k=10,
        )
        
        # 多次查询
        results1 = backend.query(request)
        results2 = backend.query(request)
        results3 = backend.query(request)
        
        # 结果顺序应该一致
        assert len(results1) == len(results2) == len(results3) == 2
        
        for r1, r2, r3 in zip(results1, results2, results3):
            assert r1.chunk_id == r2.chunk_id == r3.chunk_id
            assert r1.score == r2.score == r3.score

    def test_search_tiebreaker_by_chunk_id(self, embedding_mock: DeterministicEmbeddingMock):
        """测试分数相同时按 chunk_id 排序"""
        # 两个文档分数相同
        mock_rows = [
            {
                "chunk_id": "test:a",  # 字母序在前
                "content": "内容A",
                "project_key": "test",
                "module": None,
                "source_type": "git",
                "source_id": None,
                "owner_user_id": None,
                "commit_ts": None,
                "artifact_uri": None,
                "sha256": None,
                "chunk_idx": 0,
                "excerpt": None,
                "metadata": None,
                "vector_score": 0.9,
                "text_score": 0.8,
                "hybrid_score": 0.87,
            },
            {
                "chunk_id": "test:b",  # 字母序在后
                "content": "内容B",
                "project_key": "test",
                "module": None,
                "source_type": "git",
                "source_id": None,
                "owner_user_id": None,
                "commit_ts": None,
                "artifact_uri": None,
                "sha256": None,
                "chunk_idx": 0,
                "excerpt": None,
                "metadata": None,
                "vector_score": 0.9,
                "text_score": 0.8,
                "hybrid_score": 0.87,  # 相同分数
            },
        ]
        
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = mock_rows
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)
        
        backend = PGVectorBackend(
            connection_string="mock://",
            embedding_provider=embedding_mock,
        )
        backend._conn = mock_conn
        
        request = QueryRequest(query_text="测试", top_k=10)
        results = backend.query(request)
        
        # 应该按 chunk_id 字母序排序
        assert results[0].chunk_id == "test:a"
        assert results[1].chunk_id == "test:b"


class TestHybridSearchConfig:
    """Hybrid 检索配置测试"""

    def test_default_config(self):
        """测试默认配置"""
        config = HybridSearchConfig()
        
        assert config.vector_weight == 0.7
        assert config.text_weight == 0.3
        assert config.normalize_scores is True
        assert config.min_score == 0.0

    def test_custom_weights(self):
        """测试自定义权重"""
        config = HybridSearchConfig(vector_weight=0.5, text_weight=0.5)
        
        assert config.vector_weight == 0.5
        assert config.text_weight == 0.5

    def test_weight_normalization(self):
        """测试权重自动归一化"""
        # 总和大于 1
        config1 = HybridSearchConfig(vector_weight=0.8, text_weight=0.4)
        total1 = config1.vector_weight + config1.text_weight
        assert abs(total1 - 1.0) < 0.001
        
        # 总和小于 1
        config2 = HybridSearchConfig(vector_weight=0.3, text_weight=0.2)
        total2 = config2.vector_weight + config2.text_weight
        assert abs(total2 - 1.0) < 0.001


class TestDeleteByVersion:
    """delete_by_version 测试"""

    def test_delete_by_version_pattern(self):
        """测试版本删除的 LIKE 模式"""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_cursor.rowcount = 3
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)
        
        backend = PGVectorBackend(connection_string="mock://")
        backend._conn = mock_conn
        
        result = backend.delete_by_version("v1-2026-01")
        
        # 验证调用参数
        mock_cursor.execute.assert_called()
        call_args = mock_cursor.execute.call_args
        
        # 检查 SQL 包含 LIKE（支持 Composed 对象或字符串）
        sql_obj = call_args[0][0]
        sql_str = str(sql_obj) if hasattr(sql_obj, '__str__') else sql_obj
        assert "LIKE" in sql_str
        # 检查参数是正确的模式（可能是列表或元组）
        params = call_args[0][1]
        assert "%:v1-2026-01:%" in params
        
        assert result == 3


class TestCreatePGVectorBackend:
    """工厂函数测试"""

    def test_create_with_defaults(self):
        """测试使用默认参数创建"""
        backend = create_pgvector_backend(connection_string="postgresql://localhost/test")
        
        assert backend.backend_name == "pgvector"
        assert backend._table_name == "chunks"
        assert backend._schema == "step3"
        assert backend._vector_dim == 1536

    def test_create_with_custom_config(self):
        """测试使用自定义配置创建"""
        backend = create_pgvector_backend(
            connection_string="postgresql://localhost/test",
            table_name="chunks_test",  # 使用白名单内的表名
            vector_dim=768,
            vector_weight=0.6,
            text_weight=0.4,
        )
        
        assert backend._table_name == "chunks_test"
        assert backend._vector_dim == 768
        assert abs(backend._hybrid_config.vector_weight - 0.6) < 0.001
        assert abs(backend._hybrid_config.text_weight - 0.4) < 0.001

    def test_create_with_explicit_schema_and_table(self):
        """测试显式传递 schema 和 table_name"""
        backend = create_pgvector_backend(
            connection_string="postgresql://localhost/test",
            schema="step3_dev",
            table_name="chunks_dev",
        )
        
        assert backend._schema == "step3_dev"
        assert backend._table_name == "chunks_dev"
        assert backend._qualified_table == '"step3_dev"."chunks_dev"'


class TestPGVectorConfigValidation:
    """PGVectorConfig 配置校验测试"""

    def test_from_env_normal_schema_table(self, monkeypatch):
        """测试正常的 schema 和 table 配置"""
        # 需要导入配置类
        import sys
        sys.path.insert(0, str(__file__).rsplit('/tests/', 1)[0])
        from step3_backend_factory import PGVectorConfig
        
        monkeypatch.setenv("STEP3_PG_SCHEMA", "step3_test")
        monkeypatch.setenv("STEP3_PG_TABLE", "chunks_test")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://localhost/test")
        
        config = PGVectorConfig.from_env()
        
        assert config.schema == "step3_test"
        assert config.table == "chunks_test"
        assert config.full_table_name == "step3_test.chunks_test"

    def test_from_env_rejects_dotted_table_name(self, monkeypatch):
        """测试拒绝包含点号的 table 名称（schema.table 格式错误输入）"""
        import sys
        sys.path.insert(0, str(__file__).rsplit('/tests/', 1)[0])
        from step3_backend_factory import PGVectorConfig
        
        # 模拟用户错误地在 STEP3_PG_TABLE 中设置了 schema.table 格式
        monkeypatch.setenv("STEP3_PG_TABLE", "step3.chunks")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://localhost/test")
        
        with pytest.raises(ValueError) as exc_info:
            PGVectorConfig.from_env()
        
        error_msg = str(exc_info.value)
        # 验证错误信息包含关键提示
        assert "step3.chunks" in error_msg
        assert "STEP3_PG_SCHEMA" in error_msg
        assert "不应包含点号" in error_msg

    def test_from_env_rejects_complex_dotted_table(self, monkeypatch):
        """测试拒绝多个点号的表名"""
        import sys
        sys.path.insert(0, str(__file__).rsplit('/tests/', 1)[0])
        from step3_backend_factory import PGVectorConfig
        
        monkeypatch.setenv("STEP3_PG_TABLE", "public.step3.chunks")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://localhost/test")
        
        with pytest.raises(ValueError) as exc_info:
            PGVectorConfig.from_env()
        
        assert "public.step3.chunks" in str(exc_info.value)


class TestBackendFactorySchemaTable:
    """step3_backend_factory 工厂函数 schema/table 参数测试"""

    def test_factory_creates_backend_with_correct_schema_and_table(self):
        """验证工厂函数显式传 schema 和 table_name，不拼成 schema.table"""
        from step3_backend_factory import create_pgvector_backend, PGVectorConfig
        
        # 使用白名单中的 schema 和 table
        config = PGVectorConfig(
            dsn="postgresql://mock:mock@localhost:5432/mockdb",
            schema="step3_test",
            table="chunks_test",
            vector_dim=768,
        )
        
        backend = create_pgvector_backend(config=config)
        
        # 验证 schema 和 table_name 被正确分离传递（不是拼成 schema.table）
        assert backend._schema == "step3_test"
        assert backend._table_name == "chunks_test"
        # 验证完整表名格式（带引号的安全格式）
        assert '"step3_test"' in backend._qualified_table
        assert '"chunks_test"' in backend._qualified_table

    def test_factory_default_schema_and_table(self):
        """验证工厂函数使用默认 schema 和 table"""
        from step3_backend_factory import create_pgvector_backend, PGVectorConfig
        
        config = PGVectorConfig(
            dsn="postgresql://mock:mock@localhost:5432/mockdb",
        )
        
        backend = create_pgvector_backend(config=config)
        
        # 默认值应为 step3 和 chunks
        assert backend._schema == "step3"
        assert backend._table_name == "chunks"
        # 完整表名带引号（安全格式）
        assert '"step3"' in backend._qualified_table
        assert '"chunks"' in backend._qualified_table


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
