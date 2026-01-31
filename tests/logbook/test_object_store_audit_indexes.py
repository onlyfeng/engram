# -*- coding: utf-8 -*-
"""
测试 object_store_audit_events 表索引定义

验证内容：
1. SQL 文件中定义了关键索引
2. 关键索引包含预期的列
3. 数据库集成测试验证索引存在

SSOT: sql/13_governance_object_store_audit_events.sql
"""

import re
from pathlib import Path

import pytest

# ---------- 测试常量 ----------

SQL_DIR = Path(__file__).parent.parent.parent / "sql"
OBJECT_STORE_AUDIT_SQL = SQL_DIR / "13_governance_object_store_audit_events.sql"

# 关键索引定义（索引名 -> 期望包含的列关键词）
CRITICAL_OBJECT_STORE_AUDIT_INDEXES = {
    "idx_object_store_audit_bucket_key_ts": ["bucket", "object_key", "event_ts"],
    "idx_object_store_audit_bucket_ts": ["bucket", "event_ts"],
    "idx_object_store_audit_operation": ["operation", "event_ts"],
    "idx_object_store_audit_provider": ["provider", "event_ts"],
    "idx_object_store_audit_request_id": ["request_id"],
    "idx_object_store_audit_event_ts": ["event_ts"],
    "idx_object_store_audit_principal": ["principal", "event_ts"],
    "idx_object_store_audit_ingested_at": ["ingested_at"],
}


# ---------- 辅助函数 ----------


def extract_index_definitions(content: str) -> dict[str, str]:
    """
    从 SQL 内容中提取索引定义

    返回: {索引名: 完整定义}
    """
    index_defs: dict[str, str] = {}

    pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\w+)\s+ON\s+([^;]+);",
        re.IGNORECASE | re.MULTILINE,
    )

    for match in pattern.finditer(content):
        index_name = match.group(1)
        definition = match.group(2).strip()
        index_defs[index_name] = definition

    return index_defs


# ---------- 测试：SQL 文件存在 ----------


class TestObjectStoreAuditSqlFile:
    """测试 object_store_audit SQL 文件"""

    def test_sql_file_exists(self):
        """验证 13_governance_object_store_audit_events.sql 存在"""
        assert OBJECT_STORE_AUDIT_SQL.exists(), f"缺少文件: {OBJECT_STORE_AUDIT_SQL}"

    def test_table_definition_exists(self):
        """验证 SQL 文件包含表定义"""
        content = OBJECT_STORE_AUDIT_SQL.read_text(encoding="utf-8")

        assert "CREATE TABLE IF NOT EXISTS governance.object_store_audit_events" in content, (
            "SQL 文件应包含 governance.object_store_audit_events 表定义"
        )


# ---------- 测试：关键索引定义 ----------


class TestObjectStoreAuditIndexDefinitions:
    """测试 object_store_audit_events 索引定义"""

    @pytest.fixture
    def sql_content(self):
        """读取 SQL 文件内容"""
        return OBJECT_STORE_AUDIT_SQL.read_text(encoding="utf-8")

    @pytest.fixture
    def index_defs(self, sql_content):
        """提取索引定义"""
        return extract_index_definitions(sql_content)

    def test_all_critical_indexes_defined(self, index_defs):
        """验证所有关键索引都已定义"""
        missing = []
        for index_name in CRITICAL_OBJECT_STORE_AUDIT_INDEXES:
            if index_name not in index_defs:
                missing.append(index_name)

        assert not missing, (
            f"以下关键索引缺失定义：{missing}\n已定义的索引：{list(index_defs.keys())}"
        )

    @pytest.mark.parametrize(
        "index_name,expected_cols", list(CRITICAL_OBJECT_STORE_AUDIT_INDEXES.items())
    )
    def test_index_contains_expected_columns(self, index_defs, index_name, expected_cols):
        """验证索引包含预期的列"""
        if index_name not in index_defs:
            pytest.skip(f"索引 {index_name} 未定义")

        definition = index_defs[index_name].lower()

        missing_cols = [col for col in expected_cols if col not in definition]

        assert not missing_cols, (
            f"索引 {index_name} 缺少列：{missing_cols}\n实际定义：{index_defs[index_name]}"
        )

    def test_bucket_key_ts_has_where_clause(self, index_defs):
        """验证 bucket_key_ts 索引有 WHERE 子句"""
        index_name = "idx_object_store_audit_bucket_key_ts"
        if index_name not in index_defs:
            pytest.skip(f"索引 {index_name} 未定义")

        definition = index_defs[index_name].lower()
        assert "where" in definition and "object_key is not null" in definition, (
            f"索引 {index_name} 应有 WHERE object_key IS NOT NULL 子句\n"
            f"实际定义：{index_defs[index_name]}"
        )

    def test_principal_index_has_where_clause(self, index_defs):
        """验证 principal 索引有 WHERE 子句"""
        index_name = "idx_object_store_audit_principal"
        if index_name not in index_defs:
            pytest.skip(f"索引 {index_name} 未定义")

        definition = index_defs[index_name].lower()
        assert "where" in definition and "principal is not null" in definition, (
            f"索引 {index_name} 应有 WHERE principal IS NOT NULL 子句\n"
            f"实际定义：{index_defs[index_name]}"
        )

    def test_request_id_index_has_where_clause(self, index_defs):
        """验证 request_id 索引有 WHERE 子句"""
        index_name = "idx_object_store_audit_request_id"
        if index_name not in index_defs:
            pytest.skip(f"索引 {index_name} 未定义")

        definition = index_defs[index_name].lower()
        assert "where" in definition and "request_id is not null" in definition, (
            f"索引 {index_name} 应有 WHERE request_id IS NOT NULL 子句\n"
            f"实际定义：{index_defs[index_name]}"
        )


# ---------- 测试：数据库集成（需要 migrated_db fixture）----------


class TestObjectStoreAuditIndexesInDatabase:
    """测试数据库中的 object_store_audit_events 索引"""

    @pytest.fixture
    def audit_indexes(self, migrated_db):
        """获取数据库中 object_store_audit_events 表的索引"""
        import psycopg

        dsn = migrated_db["dsn"]
        conn = psycopg.connect(dsn)

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT indexname, pg_get_indexdef(i.indexrelid) as indexdef
                    FROM pg_indexes i
                    JOIN pg_class c ON c.relname = i.indexname
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE i.schemaname = 'governance'
                      AND i.tablename = 'object_store_audit_events'
                    ORDER BY indexname
                """)
                return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            conn.close()

    def test_critical_indexes_exist_in_db(self, audit_indexes):
        """验证关键索引存在于数据库中"""
        missing = []
        for index_name in CRITICAL_OBJECT_STORE_AUDIT_INDEXES:
            if index_name not in audit_indexes:
                missing.append(index_name)

        assert not missing, (
            f"以下关键索引在数据库中不存在：{missing}\n"
            f"数据库中已有索引：{list(audit_indexes.keys())}"
        )

    def test_all_indexes_are_valid(self, audit_indexes):
        """验证所有索引定义有效"""
        for index_name, indexdef in audit_indexes.items():
            assert indexdef.strip().startswith("CREATE"), (
                f"索引 {index_name} 定义格式无效：{indexdef}"
            )
            assert "object_store_audit_events" in indexdef, (
                f"索引 {index_name} 应引用 object_store_audit_events 表：{indexdef}"
            )
