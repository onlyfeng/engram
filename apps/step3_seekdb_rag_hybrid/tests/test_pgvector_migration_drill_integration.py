#!/usr/bin/env python3
"""
test_pgvector_migration_drill_integration.py - PGVector 迁移演练集成测试

测试环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

测试内容:
- 使用 PGVectorBackend + DefaultCollectionStrategy (per_table) 写入多 collection 数据
- 调用 ConsolidateToSharedTableMigrator 执行迁移
- 使用 SharedTableStrategy (single_table) 验证迁移后的数据隔离与行数
- 切回 per_table 模式再次验证数据

注意:
- 使用 step3_test schema 和随机表名，隔离测试数据
- 测试完成后清理创建的所有表
- 当 TEST_PGVECTOR_DSN 未设置时跳过所有测试
"""

import os
import sys
import pytest
import uuid
from typing import List, Dict, Optional
from pathlib import Path

# 路径配置在 conftest.py 中完成
from index_backend.types import ChunkDoc
from index_backend.pgvector_backend import PGVectorBackend, HybridSearchConfig
from index_backend.pgvector_collection_strategy import (
    DefaultCollectionStrategy,
    SharedTableStrategy,
)
from collection_naming import to_pgvector_table_name, make_collection_id

# 导入迁移器
_script_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from pgvector_collection_migrate import (
    ConsolidateConfig,
    ConsolidateToSharedTableMigrator,
    MigrationResult,
    ALLOWED_SCHEMA_PATTERNS,
    ALLOWED_TABLE_PATTERNS,
    PUBLIC_SCHEMA_PATTERN,
    validate_schema_name,
    validate_table_name,
)


# ============ 环境变量检查 ============

TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")

# 使用专用的测试 schema
TEST_SCHEMA = "step3_test"

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过迁移演练集成测试"
)


# ============ 确定性 Embedding Mock ============


class DeterministicEmbeddingMock:
    """
    确定性 Embedding Mock
    
    对于相同的文本，总是返回相同的向量。
    """
    
    def __init__(self, dim: int = 128):
        self._dim = dim
        self._model_id = "mock-migration-test"
    
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
        return self._text_to_vector(text)
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._text_to_vector(t) for t in texts]
    
    def _text_to_vector(self, text: str) -> List[float]:
        hash_val = hash(text)
        vector = []
        for i in range(self._dim):
            val = ((hash_val + i * 31) % 1000) / 500.0 - 1.0
            vector.append(val)
        
        # 归一化
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        
        return vector


# ============ 辅助函数 ============


def get_db_connection(dsn: str):
    """获取数据库连接"""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg (v3) 未安装")
    
    return psycopg.connect(dsn, row_factory=dict_row)


def create_test_docs(
    collection_id: str,
    count: int,
    unique_id: str,
) -> List[ChunkDoc]:
    """创建测试文档"""
    docs = []
    for i in range(count):
        doc = ChunkDoc(
            chunk_id=f"{unique_id}:{collection_id}:chunk_{i}",
            content=f"Test content for {collection_id} chunk {i} - {unique_id}",
            project_key=collection_id.split(":")[0] if ":" in collection_id else collection_id,
            source_type="test",
            source_id=f"test:{unique_id}:{i}",
            chunk_idx=i,
            artifact_uri=f"memory://test/{unique_id}/{collection_id}/{i}",
            sha256=f"sha_{unique_id}_{i}",
        )
        docs.append(doc)
    return docs


def drop_table_if_exists(conn, schema: str, table: str):
    """删除表（如果存在）"""
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')
        conn.commit()


def get_table_row_count(conn, schema: str, table: str) -> int:
    """获取表行数"""
    with conn.cursor() as cur:
        try:
            cur.execute(f'SELECT COUNT(*) as cnt FROM "{schema}"."{table}"')
            row = cur.fetchone()
            return row.get("cnt", 0) if row else 0
        except Exception:
            return 0


def table_exists(conn, schema: str, table: str) -> bool:
    """检查表是否存在"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
        """, (schema, table))
        row = cur.fetchone()
        return row and row.get("exists", False)


# ============ 测试类 ============


@skip_no_dsn
class TestMigrationDrillIntegration:
    """迁移演练集成测试"""
    
    @pytest.fixture
    def unique_test_id(self) -> str:
        """生成唯一测试 ID"""
        return f"mig_{uuid.uuid4().hex[:8]}"
    
    @pytest.fixture
    def embedding_provider(self):
        """Embedding 提供者"""
        return DeterministicEmbeddingMock(dim=128)
    
    @pytest.fixture
    def test_collections(self, unique_test_id: str) -> Dict[str, str]:
        """测试用的 collection 配置"""
        return {
            "collection_a": make_collection_id(
                project_key=f"proj_a_{unique_test_id}",
                chunking_version="v1",
                embedding_model_id="mock",
            ),
            "collection_b": make_collection_id(
                project_key=f"proj_b_{unique_test_id}",
                chunking_version="v1",
                embedding_model_id="mock",
            ),
        }
    
    @pytest.fixture
    def shared_table_name(self, unique_test_id: str) -> str:
        """共享目标表名"""
        return f"chunks_shared_{unique_test_id}"
    
    @pytest.fixture
    def cleanup_tables(
        self,
        test_collections: Dict[str, str],
        shared_table_name: str,
    ):
        """
        测试后清理创建的表
        
        这个 fixture 在测试结束时执行清理。
        """
        yield
        
        if not TEST_PGVECTOR_DSN:
            return
        
        conn = get_db_connection(TEST_PGVECTOR_DSN)
        try:
            # 清理 per_table 策略创建的表
            for cid in test_collections.values():
                table_name = to_pgvector_table_name(cid)
                drop_table_if_exists(conn, TEST_SCHEMA, table_name)
            
            # 清理共享表
            drop_table_if_exists(conn, TEST_SCHEMA, shared_table_name)
        finally:
            conn.close()
    
    def test_per_table_write_then_consolidate_then_verify(
        self,
        unique_test_id: str,
        embedding_provider: DeterministicEmbeddingMock,
        test_collections: Dict[str, str],
        shared_table_name: str,
        cleanup_tables,
    ):
        """
        完整的迁移演练测试：
        1. 使用 per_table 策略写入两个 collection 的数据
        2. 使用 ConsolidateToSharedTableMigrator 合并到共享表
        3. 使用 SharedTableStrategy 验证数据隔离与行数
        4. 切回 per_table 策略验证原表数据仍在
        """
        collection_a = test_collections["collection_a"]
        collection_b = test_collections["collection_b"]
        
        # 每个 collection 的文档数量
        docs_count_a = 5
        docs_count_b = 3
        
        # ============ Step 1: 使用 per_table 策略写入数据 ============
        
        # 创建 collection A 的后端 (per_table 策略)
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=to_pgvector_table_name(collection_a),
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=DefaultCollectionStrategy(),
        )
        backend_a.initialize()
        
        # 创建 collection B 的后端 (per_table 策略)
        backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=to_pgvector_table_name(collection_b),
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_b,
            collection_strategy=DefaultCollectionStrategy(),
        )
        backend_b.initialize()
        
        # 写入数据
        docs_a = create_test_docs(collection_a, docs_count_a, unique_test_id)
        docs_b = create_test_docs(collection_b, docs_count_b, unique_test_id)
        
        # 生成向量并写入
        for doc in docs_a:
            doc.vector = embedding_provider.embed_text(doc.content)
        for doc in docs_b:
            doc.vector = embedding_provider.embed_text(doc.content)
        
        upserted_a = backend_a.upsert(docs_a)
        upserted_b = backend_b.upsert(docs_b)
        
        assert upserted_a == docs_count_a, f"Collection A 写入数量不匹配: {upserted_a} != {docs_count_a}"
        assert upserted_b == docs_count_b, f"Collection B 写入数量不匹配: {upserted_b} != {docs_count_b}"
        
        # 验证 per_table 写入成功
        stats_a = backend_a.get_stats()
        stats_b = backend_b.get_stats()
        assert stats_a.get("total_docs", 0) >= docs_count_a
        assert stats_b.get("total_docs", 0) >= docs_count_b
        
        backend_a.close()
        backend_b.close()
        
        # ============ Step 2: 执行迁移 - 合并到共享表 ============
        
        # 准备共享表
        conn = get_db_connection(TEST_PGVECTOR_DSN)
        
        # 首先创建共享表（复制结构从 collection_a 的表）
        table_a = to_pgvector_table_name(collection_a)
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{TEST_SCHEMA}"."{shared_table_name}"
                (LIKE "{TEST_SCHEMA}"."{table_a}" INCLUDING ALL)
            """)
            # 确保有 collection_id 列
            cur.execute(f"""
                ALTER TABLE "{TEST_SCHEMA}"."{shared_table_name}"
                ADD COLUMN IF NOT EXISTS collection_id TEXT
            """)
            # 创建 collection_id 索引
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS "{shared_table_name}_collection_id_idx"
                ON "{TEST_SCHEMA}"."{shared_table_name}" (collection_id)
            """)
            conn.commit()
        conn.close()
        
        # 从 DSN 解析连接参数
        import re
        dsn_match = re.match(
            r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)',
            TEST_PGVECTOR_DSN
        )
        if not dsn_match:
            pytest.skip("无法解析 TEST_PGVECTOR_DSN")
        
        user, password, host, port, database = dsn_match.groups()
        
        # 配置迁移器
        consolidate_config = ConsolidateConfig(
            host=host,
            port=int(port),
            database=database,
            user=user,
            password=password,
            schema=TEST_SCHEMA,
            target_table=shared_table_name,
            table_pattern="step3_chunks_%",
            table_allowlist=[
                to_pgvector_table_name(collection_a),
                to_pgvector_table_name(collection_b),
            ],
            batch_size=100,
            dry_run=False,
            verbose=True,
            verify_counts=True,
            sample_verify=True,
            sample_size=10,
        )
        
        # 设置显式的 collection_id 映射
        consolidate_config.collection_mapping = {
            to_pgvector_table_name(collection_a): collection_a,
            to_pgvector_table_name(collection_b): collection_b,
        }
        
        # 执行迁移
        migrator = ConsolidateToSharedTableMigrator(consolidate_config)
        result = migrator.migrate(include_plan=True)
        
        assert result.success, f"迁移失败: {result.message}, errors: {result.errors}"
        assert result.rows_migrated == docs_count_a + docs_count_b, (
            f"迁移行数不匹配: {result.rows_migrated} != {docs_count_a + docs_count_b}"
        )
        
        # ============ Step 3: 使用 SharedTableStrategy 验证数据 ============
        
        # 创建使用 SharedTableStrategy 的后端
        shared_backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table_name,
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=SharedTableStrategy(
                collection_id_column="collection_id",
                expected_vector_dim=128,
            ),
        )
        
        shared_backend_b = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table_name,
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_b,
            collection_strategy=SharedTableStrategy(
                collection_id_column="collection_id",
                expected_vector_dim=128,
            ),
        )
        
        # 验证 collection A 的数据
        chunk_ids_a = [doc.chunk_id for doc in docs_a]
        retrieved_a = shared_backend_a.get_by_ids(chunk_ids_a)
        assert len(retrieved_a) == docs_count_a, (
            f"SharedTable collection A 数据不完整: {len(retrieved_a)} != {docs_count_a}"
        )
        
        # 验证 collection B 的数据
        chunk_ids_b = [doc.chunk_id for doc in docs_b]
        retrieved_b = shared_backend_b.get_by_ids(chunk_ids_b)
        assert len(retrieved_b) == docs_count_b, (
            f"SharedTable collection B 数据不完整: {len(retrieved_b)} != {docs_count_b}"
        )
        
        # 验证隔离性：collection A 看不到 collection B 的数据
        cross_check_a = shared_backend_a.get_by_ids(chunk_ids_b)
        assert len(cross_check_a) == 0, (
            f"SharedTable 隔离性失败: collection A 不应看到 B 的数据，但找到了 {len(cross_check_a)} 条"
        )
        
        cross_check_b = shared_backend_b.get_by_ids(chunk_ids_a)
        assert len(cross_check_b) == 0, (
            f"SharedTable 隔离性失败: collection B 不应看到 A 的数据，但找到了 {len(cross_check_b)} 条"
        )
        
        # 验证统计信息
        stats_shared_a = shared_backend_a.get_stats()
        stats_shared_b = shared_backend_b.get_stats()
        assert stats_shared_a.get("total_docs", 0) >= docs_count_a, (
            f"SharedTable collection A 统计错误: {stats_shared_a}"
        )
        assert stats_shared_b.get("total_docs", 0) >= docs_count_b, (
            f"SharedTable collection B 统计错误: {stats_shared_b}"
        )
        
        shared_backend_a.close()
        shared_backend_b.close()
        
        # ============ Step 4: 切回 per_table 验证原表数据仍在 ============
        
        # 重新使用 per_table 策略
        backend_a_verify = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=to_pgvector_table_name(collection_a),
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=DefaultCollectionStrategy(),
        )
        
        backend_b_verify = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=to_pgvector_table_name(collection_b),
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_b,
            collection_strategy=DefaultCollectionStrategy(),
        )
        
        # 验证原表数据仍然存在
        original_a = backend_a_verify.get_by_ids(chunk_ids_a)
        original_b = backend_b_verify.get_by_ids(chunk_ids_b)
        
        assert len(original_a) == docs_count_a, (
            f"per_table 原表 A 数据丢失: {len(original_a)} != {docs_count_a}"
        )
        assert len(original_b) == docs_count_b, (
            f"per_table 原表 B 数据丢失: {len(original_b)} != {docs_count_b}"
        )
        
        backend_a_verify.close()
        backend_b_verify.close()
    
    def test_migration_dry_run(
        self,
        unique_test_id: str,
        embedding_provider: DeterministicEmbeddingMock,
        test_collections: Dict[str, str],
        shared_table_name: str,
        cleanup_tables,
    ):
        """测试 dry-run 模式不实际修改数据"""
        collection_a = test_collections["collection_a"]
        
        # 写入一些数据
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=to_pgvector_table_name(collection_a),
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=DefaultCollectionStrategy(),
        )
        backend_a.initialize()
        
        docs = create_test_docs(collection_a, 3, unique_test_id)
        for doc in docs:
            doc.vector = embedding_provider.embed_text(doc.content)
        
        backend_a.upsert(docs)
        backend_a.close()
        
        # 创建空的共享表
        conn = get_db_connection(TEST_PGVECTOR_DSN)
        table_a = to_pgvector_table_name(collection_a)
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{TEST_SCHEMA}"."{shared_table_name}"
                (LIKE "{TEST_SCHEMA}"."{table_a}" INCLUDING ALL)
            """)
            cur.execute(f"""
                ALTER TABLE "{TEST_SCHEMA}"."{shared_table_name}"
                ADD COLUMN IF NOT EXISTS collection_id TEXT
            """)
            conn.commit()
        
        # 记录共享表的初始行数
        initial_count = get_table_row_count(conn, TEST_SCHEMA, shared_table_name)
        conn.close()
        
        # 从 DSN 解析连接参数
        import re
        dsn_match = re.match(
            r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)',
            TEST_PGVECTOR_DSN
        )
        user, password, host, port, database = dsn_match.groups()
        
        # 配置 dry-run 迁移
        consolidate_config = ConsolidateConfig(
            host=host,
            port=int(port),
            database=database,
            user=user,
            password=password,
            schema=TEST_SCHEMA,
            target_table=shared_table_name,
            table_allowlist=[to_pgvector_table_name(collection_a)],
            dry_run=True,  # dry-run 模式
            verbose=True,
        )
        consolidate_config.collection_mapping = {
            to_pgvector_table_name(collection_a): collection_a,
        }
        
        # 执行 dry-run 迁移
        migrator = ConsolidateToSharedTableMigrator(consolidate_config)
        result = migrator.migrate(include_plan=True)
        
        assert result.success, f"dry-run 迁移失败: {result.message}"
        assert result.dry_run is True
        
        # 验证共享表没有实际写入数据
        conn = get_db_connection(TEST_PGVECTOR_DSN)
        final_count = get_table_row_count(conn, TEST_SCHEMA, shared_table_name)
        conn.close()
        
        assert final_count == initial_count, (
            f"dry-run 不应修改数据: {final_count} != {initial_count}"
        )
    
    def test_migration_conflict_skip(
        self,
        unique_test_id: str,
        embedding_provider: DeterministicEmbeddingMock,
        test_collections: Dict[str, str],
        shared_table_name: str,
        cleanup_tables,
    ):
        """测试冲突策略 skip（重复数据不覆盖）"""
        collection_a = test_collections["collection_a"]
        
        # 写入源数据
        backend_a = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=to_pgvector_table_name(collection_a),
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=DefaultCollectionStrategy(),
        )
        backend_a.initialize()
        
        docs = create_test_docs(collection_a, 3, unique_test_id)
        for doc in docs:
            doc.vector = embedding_provider.embed_text(doc.content)
        
        backend_a.upsert(docs)
        backend_a.close()
        
        # 创建共享表并预先写入部分数据
        conn = get_db_connection(TEST_PGVECTOR_DSN)
        table_a = to_pgvector_table_name(collection_a)
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{TEST_SCHEMA}"."{shared_table_name}"
                (LIKE "{TEST_SCHEMA}"."{table_a}" INCLUDING ALL)
            """)
            cur.execute(f"""
                ALTER TABLE "{TEST_SCHEMA}"."{shared_table_name}"
                ADD COLUMN IF NOT EXISTS collection_id TEXT
            """)
            conn.commit()
        conn.close()
        
        # 预先写入一条数据到共享表
        shared_backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table_name,
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=SharedTableStrategy(
                collection_id_column="collection_id",
                expected_vector_dim=128,
            ),
        )
        
        # 写入第一条（与源表相同的 chunk_id）
        pre_doc = docs[0]
        pre_doc.content = "PRE-EXISTING CONTENT - SHOULD NOT BE OVERWRITTEN"
        pre_doc.vector = embedding_provider.embed_text(pre_doc.content)
        shared_backend.upsert([pre_doc])
        shared_backend.close()
        
        # 从 DSN 解析连接参数
        import re
        dsn_match = re.match(
            r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)',
            TEST_PGVECTOR_DSN
        )
        user, password, host, port, database = dsn_match.groups()
        
        # 执行迁移（conflict_strategy=skip）
        consolidate_config = ConsolidateConfig(
            host=host,
            port=int(port),
            database=database,
            user=user,
            password=password,
            schema=TEST_SCHEMA,
            target_table=shared_table_name,
            table_allowlist=[to_pgvector_table_name(collection_a)],
            conflict_strategy="skip",
            dry_run=False,
            verbose=True,
            verify_counts=False,  # 由于跳过冲突，行数会不匹配
        )
        consolidate_config.collection_mapping = {
            to_pgvector_table_name(collection_a): collection_a,
        }
        
        migrator = ConsolidateToSharedTableMigrator(consolidate_config)
        result = migrator.migrate()
        
        # 迁移应该成功，但实际迁移的行数少于源表
        assert result.success or result.rows_migrated > 0
        
        # 验证预先存在的数据没有被覆盖
        verify_backend = PGVectorBackend(
            connection_string=TEST_PGVECTOR_DSN,
            schema=TEST_SCHEMA,
            table_name=shared_table_name,
            embedding_provider=embedding_provider,
            vector_dim=128,
            collection_id=collection_a,
            collection_strategy=SharedTableStrategy(
                collection_id_column="collection_id",
                expected_vector_dim=128,
            ),
        )
        
        retrieved = verify_backend.get_by_ids([docs[0].chunk_id])
        assert len(retrieved) == 1
        assert "PRE-EXISTING" in retrieved[0].content, (
            f"skip 策略应保留原有数据，但内容被覆盖: {retrieved[0].content}"
        )
        
        verify_backend.close()


# ============ Schema/Table 白名单安全测试 ============


class TestMigrationSchemaWhitelistSecurity:
    """
    迁移脚本的 Schema/Table 白名单安全测试
    
    验证：
    1. 默认配置禁止 public schema
    2. step3_test schema 允许（用于集成测试）
    3. 白名单模式正确排除 public
    """
    
    def test_step3_test_schema_allowed(self):
        """验证：step3_test schema 在白名单中（集成测试使用）"""
        # step3_test 应该被允许（用于本测试文件）
        assert validate_schema_name(TEST_SCHEMA) is True
    
    def test_public_schema_blocked_by_default(self):
        """验证：public schema 默认被阻止"""
        with pytest.raises(ValueError) as excinfo:
            validate_schema_name("public")
        
        error_msg = str(excinfo.value)
        assert "public" in error_msg
        assert "--allow-public-schema" in error_msg
    
    def test_public_schema_with_explicit_allow(self):
        """验证：使用 allow_public=True 可允许 public"""
        result = validate_schema_name("public", allow_public=True)
        assert result is True
    
    def test_chunks_table_in_whitelist(self):
        """验证：chunks 表名在白名单中"""
        assert validate_table_name("chunks") is True
        assert validate_table_name("chunks_shared_test123") is True
    
    def test_step3_chunks_table_in_whitelist(self):
        """验证：step3_chunks_* 表名在白名单中"""
        # 常见的 step3_chunks_* 表名模式
        assert validate_table_name("step3_chunks_projA_v1_mock") is True
        assert validate_table_name("step3_chunks_test") is True
    
    def test_allowed_patterns_constants(self):
        """验证：白名单常量正确定义"""
        import re
        
        # ALLOWED_SCHEMA_PATTERNS 应包含 step3 和 engram
        step3_matched = any(re.match(p, "step3") for p in ALLOWED_SCHEMA_PATTERNS)
        engram_matched = any(re.match(p, "engram") for p in ALLOWED_SCHEMA_PATTERNS)
        
        assert step3_matched, "ALLOWED_SCHEMA_PATTERNS 应包含匹配 step3 的模式"
        assert engram_matched, "ALLOWED_SCHEMA_PATTERNS 应包含匹配 engram 的模式"
        
        # ALLOWED_SCHEMA_PATTERNS 不应包含 public
        public_matched = any(re.match(p, "public") for p in ALLOWED_SCHEMA_PATTERNS)
        assert not public_matched, "ALLOWED_SCHEMA_PATTERNS 不应包含匹配 public 的模式"
        
        # PUBLIC_SCHEMA_PATTERN 应单独匹配 public
        assert re.match(PUBLIC_SCHEMA_PATTERN, "public") is not None
    
    def test_production_safety_public_schema(self):
        """
        生产安全测试：验证 docker-compose.unified.yml 与迁移脚本的 schema 策略一致
        
        docker-compose.unified.yml 明确禁止 OM_PG_SCHEMA=public
        迁移脚本应保持相同的安全策略
        """
        # 模拟生产场景：不应允许 public schema
        with pytest.raises(ValueError):
            validate_schema_name("public", allow_public=False)
        
        # 显式设置 allow_public=False 也应被拒绝
        with pytest.raises(ValueError):
            validate_schema_name("public", allow_public=False)


@skip_no_dsn
class TestMigrationWithTestSchema:
    """
    验证迁移脚本在 step3_test schema 下正常工作
    """
    
    def test_schema_validation_for_test_env(self):
        """验证：测试环境使用的 step3_test schema 通过校验"""
        # 本测试文件使用 step3_test schema
        assert validate_schema_name(TEST_SCHEMA) is True
        
        # 变体也应该通过
        assert validate_schema_name("step3_dev") is True
        assert validate_schema_name("step3_staging") is True


# ============ 主入口 ============


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
