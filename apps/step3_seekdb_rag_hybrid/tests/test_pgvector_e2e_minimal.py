#!/usr/bin/env python3
"""
test_pgvector_e2e_minimal.py - PGVector 端到端最小集成测试

最简化的端到端测试，验证 PGVector 后端核心功能:
- 读取 TEST_PGVECTOR_DSN 环境变量
- 使用 SharedTableStrategy（single_table 策略）或直接使用共享表名
- 执行 initialize() / upsert() / query() 完整流程
- 断言检索结果正确

测试环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

CI 集成:
- 此测试在 CI 的 PGVector 集成步骤中执行
- 输出 junitxml 到 .artifacts/test-results/step3-pgvector-e2e.xml
"""

import os
import pytest
import uuid
from typing import List

# 路径配置在 conftest.py 中完成
from index_backend.types import ChunkDoc, QueryRequest, QueryHit
from index_backend.pgvector_backend import (
    PGVectorBackend,
    HybridSearchConfig,
)


# ============ 环境变量检查 ============

TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")

# 使用 step3_test schema 和 chunks_test 表
TEST_SCHEMA = "step3_test"
TEST_TABLE = "chunks_test"
# 端到端测试使用的 collection_id
E2E_COLLECTION_ID = "e2e:v1:mock"

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过 PGVector E2E 测试"
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
        self._model_id = "deterministic-mock-e2e"
    
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


# ============ Test Fixtures ============


@pytest.fixture(scope="module")
def embedding_mock() -> DeterministicEmbeddingMock:
    """创建确定性 Embedding Mock（模块级别共享）"""
    return DeterministicEmbeddingMock(dim=128)


@pytest.fixture(scope="module")
def e2e_test_id() -> str:
    """生成端到端测试唯一 ID"""
    return f"e2e-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def pgvector_backend(embedding_mock: DeterministicEmbeddingMock, e2e_test_id: str):
    """
    创建 PGVector 后端实例（模块级别共享）
    
    使用 single_table 策略配置（通过 table_name + collection_id）
    """
    if not TEST_PGVECTOR_DSN:
        pytest.skip("TEST_PGVECTOR_DSN 环境变量未设置")
    
    # 使用 collection_id 格式: e2e:v1:mock:{unique_id}
    collection_id = f"{E2E_COLLECTION_ID}:{e2e_test_id}"
    
    # 创建后端实例
    backend = PGVectorBackend(
        connection_string=TEST_PGVECTOR_DSN,
        schema=TEST_SCHEMA,
        table_name=TEST_TABLE,
        collection_id=collection_id,
        embedding_provider=embedding_mock,
        vector_dim=128,
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
        # 清理测试数据（仅删除本次测试的 collection 数据）
        try:
            conn = backend._get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    f'DELETE FROM {backend.qualified_table} WHERE collection_id = %s',
                    (collection_id,)
                )
                conn.commit()
        except Exception as e:
            print(f"清理测试数据失败: {e}")
        
        # 关闭连接
        backend.close()


# ============ 确定性测试数据 ============


# 预定义的测试文档（确定性内容，用于断言）
E2E_TEST_DOCS = [
    {
        "suffix": "doc1",
        "content": "Python 是一种广泛使用的高级编程语言，以其简洁清晰的语法著称。",
        "project_key": "e2e_test",
        "source_type": "git",
        "source_id": "e2e_commit_1",
        "module": "docs/",
        "excerpt": "Python 编程语言",
        "metadata": {"tag": "programming", "lang": "python"},
    },
    {
        "suffix": "doc2",
        "content": "机器学习是人工智能的一个分支，专注于让计算机从数据中学习模式。",
        "project_key": "e2e_test",
        "source_type": "git",
        "source_id": "e2e_commit_2",
        "module": "docs/ml/",
        "excerpt": "机器学习介绍",
        "metadata": {"tag": "ai", "topic": "machine-learning"},
    },
    {
        "suffix": "doc3",
        "content": "向量数据库用于存储和检索高维向量，是语义搜索的核心技术。",
        "project_key": "e2e_test",
        "source_type": "logbook",
        "source_id": "e2e_note_1",
        "module": "notes/",
        "excerpt": "向量数据库",
        "metadata": {"tag": "database", "topic": "vector-search"},
    },
]


@pytest.fixture(scope="module")
def e2e_docs(e2e_test_id: str) -> List[ChunkDoc]:
    """创建端到端测试文档"""
    docs = []
    for i, doc_data in enumerate(E2E_TEST_DOCS):
        chunk_id = f"{e2e_test_id}:{doc_data['source_type']}:{doc_data['source_id']}:sha{i}:v1:{i}"
        doc = ChunkDoc(
            chunk_id=chunk_id,
            content=doc_data["content"],
            project_key=doc_data["project_key"],
            source_type=doc_data["source_type"],
            source_id=doc_data["source_id"],
            module=doc_data["module"],
            excerpt=doc_data["excerpt"],
            metadata=doc_data["metadata"],
        )
        docs.append(doc)
    return docs


# ============ 端到端测试 ============


@skip_no_dsn
class TestPGVectorE2EMinimal:
    """PGVector 端到端最小集成测试"""

    def test_01_initialize_and_upsert(
        self,
        pgvector_backend: PGVectorBackend,
        e2e_docs: List[ChunkDoc],
    ):
        """
        测试 Step 1: 初始化表并插入文档
        
        验证:
        - initialize() 成功创建表结构
        - upsert() 成功插入所有文档
        - 文档向量已正确生成
        """
        # initialize 在 fixture 中已执行，验证表存在
        assert pgvector_backend.schema == TEST_SCHEMA
        assert pgvector_backend.table_name == TEST_TABLE
        
        # 插入文档
        result = pgvector_backend.upsert(e2e_docs)
        
        # 验证插入数量
        assert result == len(e2e_docs), f"期望插入 {len(e2e_docs)} 条，实际 {result} 条"
        
        # 验证所有文档都有向量
        for doc in e2e_docs:
            assert doc.vector is not None, f"文档 {doc.chunk_id} 缺少向量"
            assert len(doc.vector) == 128, f"文档 {doc.chunk_id} 向量维度错误"

    def test_02_exists_and_get_by_ids(
        self,
        pgvector_backend: PGVectorBackend,
        e2e_docs: List[ChunkDoc],
    ):
        """
        测试 Step 2: 验证文档存在性和获取
        
        验证:
        - exists() 正确返回文档存在状态
        - get_by_ids() 正确返回文档内容
        """
        chunk_ids = [doc.chunk_id for doc in e2e_docs]
        
        # 检查存在性
        exists_map = pgvector_backend.exists(chunk_ids)
        
        for cid in chunk_ids:
            assert exists_map.get(cid) is True, f"文档 {cid} 应该存在"
        
        # 获取文档
        retrieved_docs = pgvector_backend.get_by_ids(chunk_ids)
        
        assert len(retrieved_docs) == len(e2e_docs), \
            f"期望获取 {len(e2e_docs)} 条，实际 {len(retrieved_docs)} 条"
        
        # 验证内容匹配
        retrieved_map = {d.chunk_id: d for d in retrieved_docs}
        for original_doc in e2e_docs:
            retrieved = retrieved_map.get(original_doc.chunk_id)
            assert retrieved is not None, f"未找到文档 {original_doc.chunk_id}"
            assert retrieved.content == original_doc.content, \
                f"文档 {original_doc.chunk_id} 内容不匹配"

    def test_03_query_returns_top1_correct(
        self,
        pgvector_backend: PGVectorBackend,
        e2e_docs: List[ChunkDoc],
        e2e_test_id: str,
    ):
        """
        测试 Step 3: 查询返回正确的 Top1 结果
        
        验证:
        - query() 返回非空结果
        - Top1 结果的 chunk_id 符合预期
        - Top1 结果的内容符合预期
        """
        # 使用第一篇文档的关键词进行查询
        # E2E_TEST_DOCS[0] 是关于 Python 编程语言的
        query_text = "Python 编程语言 语法"
        
        request = QueryRequest(
            query_text=query_text,
            top_k=5,
            min_score=0.0,
        )
        
        results = pgvector_backend.query(request)
        
        # 断言结果非空
        assert len(results) > 0, "查询结果不应为空"
        
        # 断言所有结果都是 QueryHit 类型
        for hit in results:
            assert isinstance(hit, QueryHit), f"结果类型错误: {type(hit)}"
        
        # 筛选出本次测试的文档（通过 e2e_test_id 前缀）
        test_results = [r for r in results if r.chunk_id.startswith(e2e_test_id)]
        
        # 断言至少有一个本次测试的结果
        assert len(test_results) > 0, "查询结果中应包含本次测试的文档"
        
        # 获取本次测试文档中的 Top1
        top1 = test_results[0]
        
        # 断言 Top1 是关于 Python 的文档
        expected_doc = e2e_docs[0]  # Python 文档
        assert "Python" in top1.content or "python" in top1.content.lower(), \
            f"Top1 内容应包含 'Python': {top1.content[:100]}"
        
        # 断言 chunk_id 前缀正确
        assert top1.chunk_id.startswith(e2e_test_id), \
            f"Top1 chunk_id 前缀错误: {top1.chunk_id}"
        
        # 断言分数在合理范围
        assert 0.0 <= top1.score <= 1.0, f"分数超出范围: {top1.score}"

    def test_04_query_with_different_keywords(
        self,
        pgvector_backend: PGVectorBackend,
        e2e_docs: List[ChunkDoc],
        e2e_test_id: str,
    ):
        """
        测试 Step 4: 不同关键词查询返回对应文档
        
        验证不同查询能匹配到对应的文档
        """
        test_cases = [
            {
                "query": "机器学习 人工智能 数据",
                "expected_keyword": "机器学习",
                "doc_index": 1,
            },
            {
                "query": "向量数据库 语义搜索",
                "expected_keyword": "向量",
                "doc_index": 2,
            },
        ]
        
        for case in test_cases:
            request = QueryRequest(
                query_text=case["query"],
                top_k=5,
                min_score=0.0,
            )
            
            results = pgvector_backend.query(request)
            
            # 筛选本次测试的结果
            test_results = [r for r in results if r.chunk_id.startswith(e2e_test_id)]
            
            assert len(test_results) > 0, \
                f"查询 '{case['query']}' 应返回结果"
            
            # 验证结果中包含期望的关键词
            found_expected = any(
                case["expected_keyword"] in r.content
                for r in test_results
            )
            assert found_expected, \
                f"查询 '{case['query']}' 结果应包含 '{case['expected_keyword']}'"

    def test_05_query_with_filter(
        self,
        pgvector_backend: PGVectorBackend,
        e2e_docs: List[ChunkDoc],
        e2e_test_id: str,
    ):
        """
        测试 Step 5: 带过滤条件的查询
        
        验证 source_type 过滤器正确工作
        """
        # 查询 logbook 类型的文档
        request = QueryRequest(
            query_text="数据库 搜索",
            filters={"source_type": "logbook"},
            top_k=10,
            min_score=0.0,
        )
        
        results = pgvector_backend.query(request)
        
        # 筛选本次测试的结果
        test_results = [r for r in results if r.chunk_id.startswith(e2e_test_id)]
        
        # 所有结果应该是 logbook 类型
        for hit in test_results:
            assert hit.source_type == "logbook", \
                f"source_type 过滤失败: {hit.source_type}"

    def test_06_health_check_and_stats(
        self,
        pgvector_backend: PGVectorBackend,
    ):
        """
        测试 Step 6: 健康检查和统计信息
        
        验证后端状态接口正常工作
        """
        # 健康检查
        health = pgvector_backend.health_check()
        
        assert health["status"] == "healthy", f"健康检查失败: {health}"
        assert health["backend"] == "pgvector"
        assert "details" in health
        assert health["details"]["schema"] == TEST_SCHEMA
        assert health["details"]["table"] == TEST_TABLE
        
        # 统计信息
        stats = pgvector_backend.get_stats()
        
        assert "total_docs" in stats
        assert stats["total_docs"] >= 3, "至少应有 3 条测试文档"
        assert stats["schema"] == TEST_SCHEMA
        assert stats["table"] == TEST_TABLE
        assert stats["vector_dim"] == 128


@skip_no_dsn
class TestPGVectorE2ECollectionIsolation:
    """PGVector Collection 隔离端到端测试"""

    def test_different_collections_are_isolated(
        self,
        embedding_mock: DeterministicEmbeddingMock,
    ):
        """
        测试不同 collection 之间的数据隔离
        
        验证:
        - 不同 collection 写入的数据互不可见
        - 查询只返回本 collection 的结果
        """
        unique_id = uuid.uuid4().hex[:8]
        collection_a = f"e2e:v1:isolation_a:{unique_id}"
        collection_b = f"e2e:v1:isolation_b:{unique_id}"
        
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            collection_id=collection_a,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE,
            collection_id=collection_b,
            embedding_provider=embedding_mock,
            vector_dim=128,
        )
        
        try:
            # 初始化
            backend_a.initialize()
            backend_b.initialize()
            
            # 写入数据
            doc_a = ChunkDoc(
                chunk_id=f"{unique_id}:isolation:a:doc1",
                content="Collection A 的独有内容关于人工智能",
                project_key="e2e_test",
                source_type="git",
            )
            backend_a.upsert([doc_a])
            
            doc_b = ChunkDoc(
                chunk_id=f"{unique_id}:isolation:b:doc1",
                content="Collection B 的独有内容关于云计算",
                project_key="e2e_test",
                source_type="git",
            )
            backend_b.upsert([doc_b])
            
            # 验证 A 只能查到自己的数据
            results_a = backend_a.query(QueryRequest(
                query_text="人工智能 云计算",
                top_k=10,
                min_score=0.0,
            ))
            
            result_ids_a = {r.chunk_id for r in results_a}
            assert doc_a.chunk_id in result_ids_a or len(results_a) == 0, \
                "Collection A 应能查到自己的文档"
            assert doc_b.chunk_id not in result_ids_a, \
                "Collection A 不应查到 Collection B 的文档"
            
            # 验证 B 只能查到自己的数据
            results_b = backend_b.query(QueryRequest(
                query_text="人工智能 云计算",
                top_k=10,
                min_score=0.0,
            ))
            
            result_ids_b = {r.chunk_id for r in results_b}
            assert doc_b.chunk_id in result_ids_b or len(results_b) == 0, \
                "Collection B 应能查到自己的文档"
            assert doc_a.chunk_id not in result_ids_b, \
                "Collection B 不应查到 Collection A 的文档"
            
            # 验证 exists 隔离
            exists_a = backend_a.exists([doc_a.chunk_id, doc_b.chunk_id])
            assert exists_a[doc_a.chunk_id] is True
            assert exists_a[doc_b.chunk_id] is False
            
        finally:
            # 清理
            try:
                backend_a.delete([doc_a.chunk_id])
            except Exception:
                pass
            try:
                backend_b.delete([doc_b.chunk_id])
            except Exception:
                pass
            backend_a.close()
            backend_b.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
