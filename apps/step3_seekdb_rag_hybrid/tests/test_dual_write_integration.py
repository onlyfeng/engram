#!/usr/bin/env python3
"""
test_dual_write_integration.py - 双写功能集成测试

测试环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

测试内容:
- 双写配置解析
- Shadow 后端创建
- 双写功能（primary + shadow 同时写入）
- Shadow 失败不阻断主写入
- dry-run 模式
- 双写后行数一致性验证
"""

import os
import pytest
from typing import List
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

# 路径配置在 conftest.py 中完成
from step3_backend_factory import (
    DualWriteConfig,
    PGVectorConfig,
    create_shadow_backend,
    create_dual_write_backends,
    get_dual_write_config,
    COLLECTION_STRATEGY_PER_TABLE,
    COLLECTION_STRATEGY_SINGLE_TABLE,
    BackendType,
)
from index_backend.pgvector_backend import (
    PGVectorBackend,
    HybridSearchConfig,
)
from index_backend.pgvector_collection_strategy import (
    DefaultCollectionStrategy,
    SharedTableStrategy,
)
from index_backend.types import ChunkDoc


# ============ 环境变量检查 ============

TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")

# 使用专用的测试 schema 和表
TEST_SCHEMA = "step3_test"
TEST_TABLE_PRIMARY = "chunks_test"
TEST_TABLE_SHADOW = "chunks_test_shadow"

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过双写集成测试"
)


# ============ Mock ============


class MockEmbeddingProvider:
    """简单的 Embedding Mock"""
    
    def __init__(self, dim: int = 128):
        self._dim = dim
        self._model_id = "mock-embedding"
    
    @property
    def model_id(self) -> str:
        return self._model_id
    
    @property
    def dim(self) -> int:
        return self._dim
    
    @property
    def normalize(self) -> bool:
        return True
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """生成确定性向量"""
        vectors = []
        for text in texts:
            # 简单的确定性向量生成
            hash_val = hash(text) % 10000
            vector = [(hash_val + i) % 100 / 100.0 for i in range(self._dim)]
            vectors.append(vector)
        return vectors


# ============ 单元测试：配置解析 ============


class TestDualWriteConfig:
    """双写配置测试"""
    
    def test_from_env_disabled_by_default(self):
        """默认情况下双写关闭"""
        with patch.dict(os.environ, {}, clear=True):
            # 清除相关环境变量
            for key in ["STEP3_PGVECTOR_DUAL_WRITE", "STEP3_PGVECTOR_SHADOW_STRATEGY"]:
                os.environ.pop(key, None)
            
            config = DualWriteConfig.from_env()
            assert config.enabled is False
    
    def test_from_env_enabled(self):
        """启用双写"""
        with patch.dict(os.environ, {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
            "STEP3_PGVECTOR_SHADOW_STRATEGY": "single_table",
        }):
            config = DualWriteConfig.from_env()
            assert config.enabled is True
            assert config.shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE
    
    def test_from_env_auto_opposite_strategy(self):
        """自动选择相反的策略"""
        with patch.dict(os.environ, {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
        }):
            # 如果 primary 是 per_table，shadow 应该是 single_table
            os.environ.pop("STEP3_PGVECTOR_SHADOW_STRATEGY", None)
            config = DualWriteConfig.from_env(primary_strategy=COLLECTION_STRATEGY_PER_TABLE)
            assert config.shadow_strategy == COLLECTION_STRATEGY_SINGLE_TABLE
            
            # 如果 primary 是 single_table，shadow 应该是 per_table
            config = DualWriteConfig.from_env(primary_strategy=COLLECTION_STRATEGY_SINGLE_TABLE)
            assert config.shadow_strategy == COLLECTION_STRATEGY_PER_TABLE
    
    def test_from_env_dry_run(self):
        """dry-run 模式"""
        with patch.dict(os.environ, {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
            "STEP3_PGVECTOR_DUAL_WRITE_DRY_RUN": "1",
        }):
            config = DualWriteConfig.from_env()
            assert config.enabled is True
            assert config.dry_run is True
    
    def test_to_dict(self):
        """序列化为字典"""
        config = DualWriteConfig(
            enabled=True,
            shadow_strategy=COLLECTION_STRATEGY_SINGLE_TABLE,
            dry_run=True,
            shadow_table="test_shadow",
        )
        d = config.to_dict()
        assert d["enabled"] is True
        assert d["shadow_strategy"] == COLLECTION_STRATEGY_SINGLE_TABLE
        assert d["dry_run"] is True
        assert d["shadow_table"] == "test_shadow"
    
    def test_shadow_table_explicit_override(self):
        """显式设置 STEP3_PGVECTOR_SHADOW_TABLE 优先"""
        with patch.dict(os.environ, {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
            "STEP3_PGVECTOR_SHADOW_STRATEGY": "single_table",
            "STEP3_PGVECTOR_SHADOW_TABLE": "my_explicit_shadow",
            "STEP3_PGVECTOR_ROUTING_SHARED_TABLE": "shared_chunks",
        }, clear=False):
            config = DualWriteConfig.from_env()
            # 显式设置的值应优先
            assert config.shadow_table == "my_explicit_shadow"
    
    def test_shadow_table_fallback_to_routing_shared_table(self):
        """未设置 SHADOW_TABLE 且 single_table 策略时，复用 ROUTING_SHARED_TABLE"""
        env_vars = {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
            "STEP3_PGVECTOR_SHADOW_STRATEGY": "single_table",
            "STEP3_PGVECTOR_ROUTING_SHARED_TABLE": "chunks_shared_routing",
        }
        # 确保 SHADOW_TABLE 未设置
        with patch.dict(os.environ, env_vars, clear=False):
            os.environ.pop("STEP3_PGVECTOR_SHADOW_TABLE", None)
            config = DualWriteConfig.from_env()
            # 应该复用 routing_shared_table
            assert config.shadow_table == "chunks_shared_routing"
    
    def test_shadow_table_fallback_to_default_when_no_routing(self):
        """未设置 SHADOW_TABLE 且无 ROUTING_SHARED_TABLE 时，回退到 chunks_shadow"""
        env_vars = {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
            "STEP3_PGVECTOR_SHADOW_STRATEGY": "single_table",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            # 确保两个表名相关变量都未设置
            os.environ.pop("STEP3_PGVECTOR_SHADOW_TABLE", None)
            os.environ.pop("STEP3_PGVECTOR_ROUTING_SHARED_TABLE", None)
            config = DualWriteConfig.from_env()
            # 应该回退到默认值
            assert config.shadow_table == "chunks_shadow"
    
    def test_shadow_table_per_table_strategy_ignores_routing(self):
        """per_table 策略时不使用 routing_shared_table"""
        env_vars = {
            "STEP3_PGVECTOR_DUAL_WRITE": "1",
            "STEP3_PGVECTOR_SHADOW_STRATEGY": "per_table",
            "STEP3_PGVECTOR_ROUTING_SHARED_TABLE": "should_not_use_this",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            os.environ.pop("STEP3_PGVECTOR_SHADOW_TABLE", None)
            config = DualWriteConfig.from_env()
            # per_table 策略不复用 routing_shared_table，应回退到默认
            assert config.shadow_table == "chunks_shadow"


# ============ 单元测试：Shadow 后端创建 ============


class TestShadowBackendCreation:
    """Shadow 后端创建测试"""
    
    def test_create_shadow_backend_disabled(self):
        """双写禁用时返回 None"""
        config = DualWriteConfig(enabled=False)
        result = create_shadow_backend(dual_write_config=config)
        assert result is None
    
    @skip_no_dsn
    def test_create_shadow_backend_single_table_strategy(self):
        """创建使用 single_table 策略的 shadow 后端"""
        primary_config = PGVectorConfig(
            dsn=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table=TEST_TABLE_PRIMARY,
            vector_dim=128,
            collection_strategy=COLLECTION_STRATEGY_PER_TABLE,
        )
        dual_config = DualWriteConfig(
            enabled=True,
            shadow_strategy=COLLECTION_STRATEGY_SINGLE_TABLE,
            shadow_table=TEST_TABLE_SHADOW,
        )
        
        backend = create_shadow_backend(
            primary_config=primary_config,
            dual_write_config=dual_config,
            collection_id="test:v1:mock",
        )
        
        assert backend is not None
        assert backend.backend_name == "pgvector"
        # 验证使用的是 SharedTableStrategy
        assert isinstance(backend._collection_strategy, SharedTableStrategy)
    
    @skip_no_dsn
    def test_create_shadow_backend_per_table_strategy(self):
        """创建使用 per_table 策略的 shadow 后端"""
        primary_config = PGVectorConfig(
            dsn=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table=TEST_TABLE_PRIMARY,
            vector_dim=128,
            collection_strategy=COLLECTION_STRATEGY_SINGLE_TABLE,
        )
        dual_config = DualWriteConfig(
            enabled=True,
            shadow_strategy=COLLECTION_STRATEGY_PER_TABLE,
        )
        
        backend = create_shadow_backend(
            primary_config=primary_config,
            dual_write_config=dual_config,
            collection_id="test:v1:mock",
        )
        
        assert backend is not None
        assert isinstance(backend._collection_strategy, DefaultCollectionStrategy)


# ============ 集成测试：双写功能 ============


@skip_no_dsn
class TestDualWriteIntegration:
    """双写功能集成测试"""
    
    @pytest.fixture
    def embedding_provider(self):
        return MockEmbeddingProvider(dim=128)
    
    @pytest.fixture
    def primary_backend(self, embedding_provider):
        """创建 primary 后端（per_table 策略）"""
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE_PRIMARY,
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id="dual_test:v1:mock",
            collection_strategy=DefaultCollectionStrategy(),
        )
        backend.initialize()
        yield backend
        # 清理
        backend.delete_by_filter({})
    
    @pytest.fixture
    def shadow_backend(self, embedding_provider):
        """创建 shadow 后端（single_table 策略）"""
        backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=TEST_TABLE_SHADOW,
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id="dual_test:v1:mock",
            collection_strategy=SharedTableStrategy(
                collection_id_column="collection_id",
                expected_vector_dim=128,
            ),
        )
        backend.initialize()
        yield backend
        # 清理
        backend.delete_by_filter({})
    
    def _create_test_docs(self, count: int = 3) -> List[ChunkDoc]:
        """创建测试文档"""
        docs = []
        for i in range(count):
            doc = ChunkDoc(
                chunk_id=f"dual_test_chunk_{i}",
                content=f"This is test content {i} for dual write testing",
                project_key="dual_test",
                source_type="test",
                source_id=f"test:{i}",
                chunk_idx=i,
            )
            docs.append(doc)
        return docs
    
    def test_dual_write_both_backends_receive_data(self, primary_backend, shadow_backend, embedding_provider):
        """验证双写时两个后端都接收到数据"""
        docs = self._create_test_docs(3)
        
        # 生成向量
        texts = [doc.content for doc in docs]
        vectors = embedding_provider.embed_texts(texts)
        for doc, vector in zip(docs, vectors):
            doc.vector = vector
        
        # 写入 primary
        primary_count = primary_backend.upsert(docs)
        assert primary_count == 3
        
        # 写入 shadow
        shadow_count = shadow_backend.upsert(docs)
        assert shadow_count == 3
        
        # 验证两边数据一致
        primary_stats = primary_backend.get_stats()
        shadow_stats = shadow_backend.get_stats()
        
        # 检查文档数
        assert primary_stats.get("total_docs", 0) >= 3
        assert shadow_stats.get("total_docs", 0) >= 3
    
    def test_dual_write_count_consistency(self, primary_backend, shadow_backend, embedding_provider):
        """验证双写后两边行数一致"""
        docs = self._create_test_docs(5)
        
        # 生成向量
        texts = [doc.content for doc in docs]
        vectors = embedding_provider.embed_texts(texts)
        for doc, vector in zip(docs, vectors):
            doc.vector = vector
        
        # 写入两边
        primary_backend.upsert(docs)
        shadow_backend.upsert(docs)
        
        # 获取所有文档
        primary_ids = [doc.chunk_id for doc in docs]
        primary_docs = primary_backend.get_by_ids(primary_ids)
        shadow_docs = shadow_backend.get_by_ids(primary_ids)
        
        # 验证数量一致
        assert len(primary_docs) == len(shadow_docs) == 5
        
        # 验证内容一致
        primary_content_set = {d.content for d in primary_docs}
        shadow_content_set = {d.content for d in shadow_docs}
        assert primary_content_set == shadow_content_set


# ============ 单元测试：upsert_to_index 双写逻辑 ============


class TestUpsertToIndexDualWrite:
    """测试 upsert_to_index 的双写逻辑"""
    
    def test_shadow_failure_does_not_block_primary(self):
        """Shadow 写入失败不阻断主写入"""
        from seek_indexer import (
            upsert_to_index,
            DualWriteStats,
            ChunkResult,
        )
        from step3_chunking import ChunkResult
        
        # 创建 mock primary 后端
        primary_backend = MagicMock()
        primary_backend.backend_name = "pgvector"
        primary_backend.upsert.return_value = 3
        
        # 创建 mock shadow 后端（会抛出异常）
        shadow_backend = MagicMock()
        shadow_backend.backend_name = "pgvector"
        shadow_backend.upsert.side_effect = Exception("Shadow write failed")
        
        # 创建双写配置和统计
        dual_write_config = DualWriteConfig(enabled=True, shadow_strategy="single_table")
        dual_write_stats = DualWriteStats(enabled=True, shadow_strategy="single_table")
        
        # 创建测试 chunks
        chunks = [
            ChunkResult(
                chunk_id=f"test_chunk_{i}",
                content=f"Test content {i}",
                source_type="test",
                source_id="test:1",
                sha256="abc123",
                artifact_uri="test://uri",
                chunk_idx=i,
            )
            for i in range(3)
        ]
        
        # 执行 upsert（不使用 embedding provider）
        with patch('seek_indexer.get_embedding_provider_instance', return_value=None):
            indexed = upsert_to_index(
                chunks=chunks,
                dry_run=False,
                backend=primary_backend,
                shadow_backend=shadow_backend,
                dual_write_config=dual_write_config,
                dual_write_stats=dual_write_stats,
            )
        
        # 验证 primary 写入成功
        assert indexed == 3
        primary_backend.upsert.assert_called_once()
        
        # 验证 shadow 失败被记录但不阻断
        assert dual_write_stats.shadow_errors > 0
        assert len(dual_write_stats.shadow_error_records) > 0
    
    def test_shadow_dry_run_mode(self):
        """Shadow dry-run 模式不实际写入"""
        from seek_indexer import (
            upsert_to_index,
            DualWriteStats,
        )
        from step3_chunking import ChunkResult
        
        # 创建 mock primary 后端
        primary_backend = MagicMock()
        primary_backend.backend_name = "pgvector"
        primary_backend.upsert.return_value = 3
        
        # 创建 mock shadow 后端
        shadow_backend = MagicMock()
        shadow_backend.backend_name = "pgvector"
        
        # 创建双写配置（dry-run 模式）
        dual_write_config = DualWriteConfig(
            enabled=True,
            shadow_strategy="single_table",
            dry_run=True,  # dry-run 模式
        )
        dual_write_stats = DualWriteStats(enabled=True, shadow_strategy="single_table")
        
        # 创建测试 chunks
        chunks = [
            ChunkResult(
                chunk_id=f"test_chunk_{i}",
                content=f"Test content {i}",
                source_type="test",
                source_id="test:1",
                sha256="abc123",
                artifact_uri="test://uri",
                chunk_idx=i,
            )
            for i in range(3)
        ]
        
        # 执行 upsert
        with patch('seek_indexer.get_embedding_provider_instance', return_value=None):
            indexed = upsert_to_index(
                chunks=chunks,
                dry_run=False,  # primary 不是 dry-run
                backend=primary_backend,
                shadow_backend=shadow_backend,
                dual_write_config=dual_write_config,
                dual_write_stats=dual_write_stats,
            )
        
        # 验证 primary 写入成功
        assert indexed == 3
        primary_backend.upsert.assert_called_once()
        
        # 验证 shadow 没有实际调用（dry-run 模式）
        shadow_backend.upsert.assert_not_called()
        
        # 验证统计
        assert dual_write_stats.dry_run is True
        assert dual_write_stats.shadow_indexed == 3  # 记录了数量但没有实际写入


# ============ 单元测试：DualWriteStats ============


class TestDualWriteStats:
    """双写统计测试"""
    
    def test_add_error(self):
        """测试错误记录"""
        from seek_indexer import DualWriteStats
        
        stats = DualWriteStats(enabled=True, shadow_strategy="single_table")
        
        stats.add_error("chunk_1", "Connection timeout")
        stats.add_error("chunk_2", "Write failed")
        
        assert stats.shadow_errors == 2
        assert len(stats.shadow_error_records) == 2
        assert stats.shadow_error_records[0]["chunk_id"] == "chunk_1"
        assert "timeout" in stats.shadow_error_records[0]["error"].lower()
    
    def test_add_error_max_limit(self):
        """测试错误记录数量限制"""
        from seek_indexer import DualWriteStats
        
        stats = DualWriteStats(enabled=True, shadow_strategy="single_table")
        
        # 添加超过限制的错误
        for i in range(100):
            stats.add_error(f"chunk_{i}", f"Error {i}", max_errors=10)
        
        # 计数正确
        assert stats.shadow_errors == 100
        # 但记录只保留 10 条
        assert len(stats.shadow_error_records) == 10
    
    def test_to_dict(self):
        """测试序列化"""
        from seek_indexer import DualWriteStats
        
        stats = DualWriteStats(
            enabled=True,
            shadow_strategy="single_table",
            dry_run=False,
            shadow_indexed=10,
            shadow_errors=2,
        )
        stats.add_error("chunk_1", "Error 1")
        
        d = stats.to_dict()
        assert d["enabled"] is True
        assert d["shadow_strategy"] == "single_table"
        assert d["shadow_indexed"] == 10
        assert d["shadow_errors"] == 3  # 2 + 1 from add_error


# ============ 主入口 ============


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
