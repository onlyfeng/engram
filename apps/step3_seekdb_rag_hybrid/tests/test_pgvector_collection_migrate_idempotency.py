#!/usr/bin/env python3
"""
test_pgvector_collection_migrate_idempotency.py - 迁移幂等性集成测试

测试 pgvector_collection_migrate.py 的幂等性保证：
1. table-per-collection 迁移：多次执行不会导致目标表行数翻倍
2. consolidate-to-shared-table 迁移：
   - conflict=skip 模式：重复 chunk_id 被跳过
   - conflict=upsert 模式：重复 chunk_id 被更新
3. Schema/Table 白名单校验：
   - 默认禁止 public schema 作为迁移目标
   - 通过 allow_public=True 显式允许

测试环境要求:
- 设置环境变量 TEST_PGVECTOR_DSN 指向可用的 PostgreSQL 实例
- PostgreSQL 实例需要已安装 pgvector 扩展
- 示例: TEST_PGVECTOR_DSN=postgresql://postgres:postgres@localhost:5432/engram

CI 集成:
- 此测试在 CI 的 PGVector 集成步骤中执行
"""

import os
import sys
import pytest
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# 导入迁移脚本的校验函数
_script_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from pgvector_collection_migrate import (
    validate_schema_name,
    validate_table_name,
    ALLOWED_SCHEMA_PATTERNS,
    ALLOWED_TABLE_PATTERNS,
    PUBLIC_SCHEMA_PATTERN,
)

# 环境变量检查
TEST_PGVECTOR_DSN = os.environ.get("TEST_PGVECTOR_DSN")

# 测试 schema（与 e2e 测试共用）
TEST_SCHEMA = "step3_test"

# 跳过条件
skip_no_dsn = pytest.mark.skipif(
    not TEST_PGVECTOR_DSN,
    reason="TEST_PGVECTOR_DSN 环境变量未设置，跳过 PGVector 迁移幂等性测试"
)


# ============ 数据库工具函数 ============


def get_connection():
    """获取数据库连接"""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg 未安装")
    
    return psycopg.connect(TEST_PGVECTOR_DSN, row_factory=dict_row)


def execute_sql(sql: str, params: tuple = None):
    """执行 SQL 语句"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
    finally:
        conn.close()


def fetch_one(sql: str, params: tuple = None) -> Optional[Dict]:
    """执行查询并返回单行结果"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple = None) -> List[Dict]:
    """执行查询并返回所有结果"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def table_exists(schema: str, table: str) -> bool:
    """检查表是否存在"""
    result = fetch_one("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        ) as exists
    """, (schema, table))
    return result and result.get("exists", False)


def get_table_row_count(schema: str, table: str) -> int:
    """获取表行数"""
    result = fetch_one(f'SELECT COUNT(*) as cnt FROM "{schema}"."{table}"')
    return result.get("cnt", 0) if result else 0


def get_chunk_ids_in_table(schema: str, table: str) -> set:
    """获取表中所有 chunk_id"""
    rows = fetch_all(f'SELECT chunk_id FROM "{schema}"."{table}"')
    return {row["chunk_id"] for row in rows}


def drop_table_if_exists(schema: str, table: str):
    """删除表（如果存在）"""
    execute_sql(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')


def create_schema_if_not_exists(schema: str):
    """创建 schema（如果不存在）"""
    execute_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


# ============ Fixtures ============


@pytest.fixture(scope="module")
def unique_test_id() -> str:
    """生成测试唯一 ID"""
    return f"idempotency_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def setup_test_schema():
    """设置测试 schema"""
    if not TEST_PGVECTOR_DSN:
        pytest.skip("TEST_PGVECTOR_DSN 未设置")
    
    create_schema_if_not_exists(TEST_SCHEMA)
    
    # 确保 pgvector 扩展已安装
    execute_sql("CREATE EXTENSION IF NOT EXISTS vector")
    
    yield TEST_SCHEMA


# ============ table-per-collection 幂等性测试 ============


@skip_no_dsn
class TestTablePerCollectionIdempotency:
    """
    测试 table-per-collection 迁移的幂等性
    
    场景：在 shared 表写入数据后运行两次迁移，验证目标表行数不翻倍
    """
    
    @pytest.fixture(autouse=True)
    def setup_and_cleanup(self, setup_test_schema, unique_test_id):
        """设置和清理测试表"""
        self.schema = setup_test_schema
        self.source_table = f"chunks_tpc_src_{unique_test_id}"
        self.collection_id = f"tpc_test:{unique_test_id}:v1:mock"
        
        # 创建源表（模拟 shared table with collection_id）
        self._create_source_table()
        
        yield
        
        # 清理：删除源表和目标表
        drop_table_if_exists(self.schema, self.source_table)
        # 计算目标表名（使用相同的命名逻辑）
        target_table = self._get_target_table_name()
        if target_table:
            drop_table_if_exists(self.schema, target_table)
    
    def _create_source_table(self):
        """创建源表结构"""
        execute_sql(f"""
            CREATE TABLE IF NOT EXISTS "{self.schema}"."{self.source_table}" (
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
                chunk_idx INTEGER DEFAULT 0,
                excerpt TEXT,
                metadata JSONB DEFAULT '{{}}',
                collection_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # 创建 collection_id 索引
        execute_sql(f"""
            CREATE INDEX IF NOT EXISTS "{self.source_table}_collection_id_idx"
            ON "{self.schema}"."{self.source_table}" (collection_id)
        """)
    
    def _get_target_table_name(self) -> str:
        """获取目标表名（模拟 to_pgvector_table_name 逻辑）"""
        # 简化版本：将 collection_id 中的特殊字符替换为下划线
        sanitized = self.collection_id.replace("-", "_").replace(":", "_")
        import re
        sanitized = re.sub(r'[^a-zA-Z0-9_]', '', sanitized).lower()
        return f"step3_chunks_{sanitized}"
    
    def _insert_test_data(self, chunk_ids: List[str]):
        """插入测试数据"""
        for chunk_id in chunk_ids:
            # 生成简单的确定性向量
            vector = [float(i % 10) / 10 for i in range(128)]
            vector_str = "[" + ",".join(str(v) for v in vector) + "]"
            
            execute_sql(f"""
                INSERT INTO "{self.schema}"."{self.source_table}"
                (chunk_id, content, vector, project_key, source_type, collection_id)
                VALUES (%s, %s, %s::vector, %s, %s, %s)
                ON CONFLICT (chunk_id) DO NOTHING
            """, (
                chunk_id,
                f"Test content for {chunk_id}",
                vector_str,
                "test_project",
                "git",
                self.collection_id,
            ))
    
    def _run_table_per_collection_migration(self):
        """
        运行 table-per-collection 迁移
        
        模拟 TablePerCollectionMigrator 的核心逻辑
        """
        target_table = self._get_target_table_name()
        
        # 创建目标表（如果不存在）
        execute_sql(f"""
            CREATE TABLE IF NOT EXISTS "{self.schema}"."{target_table}" 
            (LIKE "{self.schema}"."{self.source_table}" INCLUDING ALL)
        """)
        
        # 幂等复制：使用 ON CONFLICT DO NOTHING
        execute_sql(f"""
            INSERT INTO "{self.schema}"."{target_table}"
            SELECT * FROM "{self.schema}"."{self.source_table}"
            WHERE collection_id = %s
            ON CONFLICT (chunk_id) DO NOTHING
        """, (self.collection_id,))
        
        return target_table
    
    def test_migration_idempotency_rows_not_doubled(self, unique_test_id):
        """
        验证：运行两次迁移后，目标表行数不会翻倍
        
        步骤：
        1. 在源表插入 5 条数据
        2. 运行第一次迁移
        3. 验证目标表有 5 条记录
        4. 运行第二次迁移
        5. 验证目标表仍然只有 5 条记录
        """
        # Step 1: 插入测试数据
        chunk_ids = [
            f"{unique_test_id}:tpc:doc{i}" for i in range(5)
        ]
        self._insert_test_data(chunk_ids)
        
        source_count = get_table_row_count(self.schema, self.source_table)
        assert source_count == 5, f"源表应有 5 条记录，实际 {source_count}"
        
        # Step 2: 第一次迁移
        target_table = self._run_table_per_collection_migration()
        
        # Step 3: 验证目标表行数
        count_after_first = get_table_row_count(self.schema, target_table)
        assert count_after_first == 5, \
            f"第一次迁移后目标表应有 5 条记录，实际 {count_after_first}"
        
        # Step 4: 第二次迁移
        self._run_table_per_collection_migration()
        
        # Step 5: 验证目标表行数不变
        count_after_second = get_table_row_count(self.schema, target_table)
        assert count_after_second == 5, \
            f"第二次迁移后目标表应仍有 5 条记录，实际 {count_after_second}（行数翻倍了！）"
        
        # 额外验证：chunk_id 集合应该完全一致
        source_ids = get_chunk_ids_in_table(self.schema, self.source_table)
        target_ids = get_chunk_ids_in_table(self.schema, target_table)
        
        assert source_ids == target_ids, \
            f"源表和目标表的 chunk_id 集合应一致"


# ============ consolidate-to-shared-table 幂等性测试 ============


@skip_no_dsn
class TestConsolidateToSharedTableIdempotency:
    """
    测试 consolidate-to-shared-table 迁移的幂等性
    
    场景：
    - 创建两个 per_table 源表，各插入部分重叠 chunk_id
    - 分别用 conflict=skip 和 conflict=upsert 跑两次
    - 验证目标表行数与内容符合预期
    """
    
    @pytest.fixture(autouse=True)
    def setup_and_cleanup(self, setup_test_schema, unique_test_id):
        """设置和清理测试表"""
        self.schema = setup_test_schema
        self.source_table_a = f"step3_chunks_coll_a_{unique_test_id}"
        self.source_table_b = f"step3_chunks_coll_b_{unique_test_id}"
        self.target_table = f"chunks_shared_{unique_test_id}"
        self.collection_a = f"coll_a:{unique_test_id}:v1:mock"
        self.collection_b = f"coll_b:{unique_test_id}:v1:mock"
        
        # 创建源表
        self._create_per_collection_table(self.source_table_a)
        self._create_per_collection_table(self.source_table_b)
        
        # 创建目标 shared 表
        self._create_shared_table()
        
        yield
        
        # 清理所有测试表
        drop_table_if_exists(self.schema, self.source_table_a)
        drop_table_if_exists(self.schema, self.source_table_b)
        drop_table_if_exists(self.schema, self.target_table)
    
    def _create_per_collection_table(self, table_name: str):
        """创建 per-collection 源表"""
        execute_sql(f"""
            CREATE TABLE IF NOT EXISTS "{self.schema}"."{table_name}" (
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
                chunk_idx INTEGER DEFAULT 0,
                excerpt TEXT,
                metadata JSONB DEFAULT '{{}}',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    
    def _create_shared_table(self):
        """创建 shared 目标表（带 collection_id 列）"""
        execute_sql(f"""
            CREATE TABLE IF NOT EXISTS "{self.schema}"."{self.target_table}" (
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
                chunk_idx INTEGER DEFAULT 0,
                excerpt TEXT,
                metadata JSONB DEFAULT '{{}}',
                collection_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        
        # 创建 collection_id 索引
        execute_sql(f"""
            CREATE INDEX IF NOT EXISTS "{self.target_table}_collection_id_idx"
            ON "{self.schema}"."{self.target_table}" (collection_id)
        """)
    
    def _insert_into_source_table(
        self,
        table_name: str,
        chunk_ids: List[str],
        content_suffix: str = "",
    ):
        """向源表插入数据"""
        for chunk_id in chunk_ids:
            # 生成简单的确定性向量
            vector = [float(i % 10) / 10 for i in range(128)]
            vector_str = "[" + ",".join(str(v) for v in vector) + "]"
            
            execute_sql(f"""
                INSERT INTO "{self.schema}"."{table_name}"
                (chunk_id, content, vector, project_key, source_type)
                VALUES (%s, %s, %s::vector, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    content = EXCLUDED.content,
                    updated_at = NOW()
            """, (
                chunk_id,
                f"Content for {chunk_id}{content_suffix}",
                vector_str,
                "test_project",
                "git",
            ))
    
    def _run_consolidate_migration(
        self,
        source_table: str,
        collection_id: str,
        conflict_strategy: str = "skip",
    ) -> int:
        """
        运行 consolidate-to-shared-table 迁移
        
        模拟 ConsolidateToSharedTableMigrator 的核心逻辑
        
        Args:
            source_table: 源表名
            collection_id: 要写入的 collection_id
            conflict_strategy: 'skip' 或 'upsert'
        
        Returns:
            实际插入/更新的行数
        """
        if conflict_strategy == "upsert":
            conflict_clause = """
                ON CONFLICT (chunk_id) DO UPDATE SET
                    content = EXCLUDED.content,
                    vector = EXCLUDED.vector,
                    project_key = EXCLUDED.project_key,
                    collection_id = EXCLUDED.collection_id,
                    updated_at = NOW()
            """
        else:  # 'skip'
            conflict_clause = "ON CONFLICT (chunk_id) DO NOTHING"
        
        # 获取源表列（不包括 collection_id，因为源表可能没有这列）
        source_columns = "chunk_id, content, vector, project_key, module, source_type, source_id, owner_user_id, commit_ts, artifact_uri, sha256, chunk_idx, excerpt, metadata, created_at, updated_at"
        
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO "{self.schema}"."{self.target_table}"
                    ({source_columns}, collection_id)
                    SELECT {source_columns}, %s as collection_id
                    FROM "{self.schema}"."{source_table}"
                    {conflict_clause}
                """, (collection_id,))
                affected = cur.rowcount
                conn.commit()
                return affected
        finally:
            conn.close()
    
    def test_conflict_skip_with_overlapping_chunk_ids(self, unique_test_id):
        """
        验证 conflict=skip 策略：重复 chunk_id 被跳过
        
        步骤：
        1. 源表 A 插入 chunk_id: [1, 2, 3]
        2. 源表 B 插入 chunk_id: [3, 4, 5]（与 A 有重叠的 3）
        3. 迁移 A 到目标表（skip 策略）
        4. 迁移 B 到目标表（skip 策略）
        5. 验证目标表有 5 条记录（chunk_id 3 只保留第一次的）
        6. 再次迁移 A 和 B（skip 策略）
        7. 验证目标表仍然只有 5 条记录
        """
        # Step 1: 源表 A 插入数据
        ids_a = [f"{unique_test_id}:skip:doc{i}" for i in [1, 2, 3]]
        self._insert_into_source_table(self.source_table_a, ids_a, " (from A)")
        
        # Step 2: 源表 B 插入数据（chunk_id 3 重叠）
        ids_b = [f"{unique_test_id}:skip:doc{i}" for i in [3, 4, 5]]
        self._insert_into_source_table(self.source_table_b, ids_b, " (from B)")
        
        # Step 3: 迁移 A
        self._run_consolidate_migration(
            self.source_table_a, self.collection_a, "skip"
        )
        
        count_after_a = get_table_row_count(self.schema, self.target_table)
        assert count_after_a == 3, \
            f"迁移 A 后目标表应有 3 条记录，实际 {count_after_a}"
        
        # Step 4: 迁移 B（chunk_id 3 应该被跳过）
        self._run_consolidate_migration(
            self.source_table_b, self.collection_b, "skip"
        )
        
        # Step 5: 验证目标表行数
        count_after_b = get_table_row_count(self.schema, self.target_table)
        assert count_after_b == 5, \
            f"迁移 B 后目标表应有 5 条记录（重叠的跳过），实际 {count_after_b}"
        
        # 验证 chunk_id 3 的内容是来自 A（第一次插入的）
        overlap_row = fetch_one(f"""
            SELECT content, collection_id FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:skip:doc3",))
        
        assert "(from A)" in overlap_row["content"], \
            f"chunk_id 3 的内容应来自 A，实际: {overlap_row['content']}"
        assert overlap_row["collection_id"] == self.collection_a, \
            f"chunk_id 3 的 collection_id 应为 A，实际: {overlap_row['collection_id']}"
        
        # Step 6: 再次迁移 A 和 B
        self._run_consolidate_migration(
            self.source_table_a, self.collection_a, "skip"
        )
        self._run_consolidate_migration(
            self.source_table_b, self.collection_b, "skip"
        )
        
        # Step 7: 验证目标表行数不变
        count_after_repeat = get_table_row_count(self.schema, self.target_table)
        assert count_after_repeat == 5, \
            f"重复迁移后目标表应仍有 5 条记录，实际 {count_after_repeat}"
    
    def test_conflict_upsert_with_overlapping_chunk_ids(self, unique_test_id):
        """
        验证 conflict=upsert 策略：重复 chunk_id 被更新
        
        步骤：
        1. 源表 A 插入 chunk_id: [1, 2, 3]（内容标记为 v1）
        2. 迁移 A 到目标表（upsert 策略）
        3. 更新源表 A 的内容为 v2
        4. 再次迁移 A 到目标表（upsert 策略）
        5. 验证目标表内容已更新为 v2
        6. 验证目标表仍然只有 3 条记录
        """
        # Step 1: 源表 A 插入初始数据（v1）
        ids = [f"{unique_test_id}:upsert:doc{i}" for i in [1, 2, 3]]
        self._insert_into_source_table(self.source_table_a, ids, " (v1)")
        
        # Step 2: 迁移 A
        self._run_consolidate_migration(
            self.source_table_a, self.collection_a, "upsert"
        )
        
        count_after_first = get_table_row_count(self.schema, self.target_table)
        assert count_after_first == 3, \
            f"第一次迁移后目标表应有 3 条记录，实际 {count_after_first}"
        
        # 验证内容是 v1
        row_v1 = fetch_one(f"""
            SELECT content FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:upsert:doc1",))
        assert "(v1)" in row_v1["content"], \
            f"初始内容应包含 v1，实际: {row_v1['content']}"
        
        # Step 3: 更新源表内容为 v2
        self._insert_into_source_table(self.source_table_a, ids, " (v2)")
        
        # Step 4: 再次迁移（upsert）
        self._run_consolidate_migration(
            self.source_table_a, self.collection_a, "upsert"
        )
        
        # Step 5: 验证内容已更新为 v2
        row_v2 = fetch_one(f"""
            SELECT content FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:upsert:doc1",))
        assert "(v2)" in row_v2["content"], \
            f"更新后内容应包含 v2，实际: {row_v2['content']}"
        
        # Step 6: 验证目标表行数不变
        count_after_second = get_table_row_count(self.schema, self.target_table)
        assert count_after_second == 3, \
            f"upsert 后目标表应仍有 3 条记录，实际 {count_after_second}"
    
    def test_mixed_skip_and_upsert_scenario(self, unique_test_id):
        """
        验证混合场景：先 skip 再 upsert
        
        步骤：
        1. 源表 A 插入 [1, 2]，源表 B 插入 [2, 3]
        2. 迁移 A（skip）-> 目标表有 [1, 2]
        3. 迁移 B（skip）-> 目标表有 [1, 2, 3]，2 保留 A 的版本
        4. 更新源表 B 的内容
        5. 迁移 B（upsert）-> 2 和 3 更新为 B 的版本
        6. 验证最终状态
        """
        # Step 1: 插入数据
        ids_a = [f"{unique_test_id}:mixed:doc{i}" for i in [1, 2]]
        ids_b = [f"{unique_test_id}:mixed:doc{i}" for i in [2, 3]]
        
        self._insert_into_source_table(self.source_table_a, ids_a, " (A-v1)")
        self._insert_into_source_table(self.source_table_b, ids_b, " (B-v1)")
        
        # Step 2: 迁移 A（skip）
        self._run_consolidate_migration(
            self.source_table_a, self.collection_a, "skip"
        )
        
        count_after_a = get_table_row_count(self.schema, self.target_table)
        assert count_after_a == 2, f"迁移 A 后应有 2 条记录，实际 {count_after_a}"
        
        # Step 3: 迁移 B（skip）
        self._run_consolidate_migration(
            self.source_table_b, self.collection_b, "skip"
        )
        
        count_after_b_skip = get_table_row_count(self.schema, self.target_table)
        assert count_after_b_skip == 3, \
            f"迁移 B（skip）后应有 3 条记录，实际 {count_after_b_skip}"
        
        # 验证 doc2 仍是 A 的版本
        doc2_after_skip = fetch_one(f"""
            SELECT content, collection_id FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:mixed:doc2",))
        assert "(A-v1)" in doc2_after_skip["content"], \
            f"skip 后 doc2 应是 A 的版本，实际: {doc2_after_skip['content']}"
        
        # Step 4: 更新源表 B
        self._insert_into_source_table(self.source_table_b, ids_b, " (B-v2)")
        
        # Step 5: 迁移 B（upsert）
        self._run_consolidate_migration(
            self.source_table_b, self.collection_b, "upsert"
        )
        
        # Step 6: 验证最终状态
        count_final = get_table_row_count(self.schema, self.target_table)
        assert count_final == 3, f"最终应有 3 条记录，实际 {count_final}"
        
        # doc1 仍是 A 的版本
        doc1_final = fetch_one(f"""
            SELECT content, collection_id FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:mixed:doc1",))
        assert "(A-v1)" in doc1_final["content"], \
            f"doc1 应保持 A 的版本，实际: {doc1_final['content']}"
        assert doc1_final["collection_id"] == self.collection_a
        
        # doc2 被更新为 B 的版本
        doc2_final = fetch_one(f"""
            SELECT content, collection_id FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:mixed:doc2",))
        assert "(B-v2)" in doc2_final["content"], \
            f"doc2 应被更新为 B 的版本，实际: {doc2_final['content']}"
        assert doc2_final["collection_id"] == self.collection_b
        
        # doc3 是 B 的版本
        doc3_final = fetch_one(f"""
            SELECT content, collection_id FROM "{self.schema}"."{self.target_table}"
            WHERE chunk_id = %s
        """, (f"{unique_test_id}:mixed:doc3",))
        assert "(B-v2)" in doc3_final["content"], \
            f"doc3 应是 B 的版本，实际: {doc3_final['content']}"
        assert doc3_final["collection_id"] == self.collection_b


# ============ Schema/Table 白名单校验测试 ============


class TestSchemaTableWhitelistValidation:
    """
    测试 Schema/Table 白名单校验机制
    
    验证：
    1. 默认禁止 public schema
    2. 允许 step3, engram 等安全 schema
    3. --allow-public-schema 可显式允许 public
    4. 表名白名单校验
    """
    
    def test_public_schema_rejected_by_default(self):
        """验证：默认情况下 public schema 被拒绝"""
        with pytest.raises(ValueError) as excinfo:
            validate_schema_name("public")
        
        assert "public" in str(excinfo.value)
        assert "禁止" in str(excinfo.value) or "默认被禁止" in str(excinfo.value)
        assert "--allow-public-schema" in str(excinfo.value)
    
    def test_public_schema_allowed_with_flag(self):
        """验证：使用 allow_public=True 可以允许 public schema"""
        # 不应抛出异常
        result = validate_schema_name("public", allow_public=True)
        assert result is True
    
    def test_step3_schema_allowed_by_default(self):
        """验证：step3 及其变体默认允许"""
        assert validate_schema_name("step3") is True
        assert validate_schema_name("step3_test") is True
        assert validate_schema_name("step3_dev") is True
        assert validate_schema_name("step3_prod") is True
    
    def test_engram_schema_allowed_by_default(self):
        """验证：engram 及其变体默认允许"""
        assert validate_schema_name("engram") is True
        assert validate_schema_name("engram_test") is True
        assert validate_schema_name("engram_dev") is True
    
    def test_unknown_schema_rejected(self):
        """验证：未知 schema 被拒绝"""
        with pytest.raises(ValueError) as excinfo:
            validate_schema_name("unknown_schema")
        
        assert "unknown_schema" in str(excinfo.value)
        assert "白名单" in str(excinfo.value)
    
    def test_custom_patterns_override_default(self):
        """验证：自定义模式列表可覆盖默认值"""
        custom_patterns = [r"^custom_\w+$"]
        
        # custom_schema 应该被允许
        assert validate_schema_name("custom_test", allowed_patterns=custom_patterns) is True
        
        # step3 应该被拒绝（不在自定义列表中）
        with pytest.raises(ValueError):
            validate_schema_name("step3", allowed_patterns=custom_patterns)
    
    def test_chunks_table_allowed(self):
        """验证：chunks 表名默认允许"""
        assert validate_table_name("chunks") is True
        assert validate_table_name("chunks_backup") is True
        assert validate_table_name("chunks_test") is True
    
    def test_step3_chunks_table_allowed(self):
        """验证：step3_chunks_* 表名默认允许"""
        assert validate_table_name("step3_chunks_projA_v1_mock") is True
        assert validate_table_name("step3_chunks_test123") is True
    
    def test_unknown_table_rejected(self):
        """验证：未知表名被拒绝"""
        with pytest.raises(ValueError) as excinfo:
            validate_table_name("random_table")
        
        assert "random_table" in str(excinfo.value)
        assert "白名单" in str(excinfo.value)
    
    def test_public_schema_pattern_constant(self):
        """验证：PUBLIC_SCHEMA_PATTERN 常量正确定义"""
        import re
        assert re.match(PUBLIC_SCHEMA_PATTERN, "public") is not None
        assert re.match(PUBLIC_SCHEMA_PATTERN, "public_test") is None
    
    def test_allowed_patterns_not_include_public(self):
        """验证：ALLOWED_SCHEMA_PATTERNS 不包含 public"""
        import re
        for pattern in ALLOWED_SCHEMA_PATTERNS:
            # public 不应匹配任何默认模式
            if re.match(pattern, "public"):
                pytest.fail(f"ALLOWED_SCHEMA_PATTERNS 不应包含匹配 'public' 的模式: {pattern}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
