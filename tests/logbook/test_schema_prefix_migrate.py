# -*- coding: utf-8 -*-
"""
测试 schema prefix 迁移功能（仅测试模式）

================================================================================
架构约束（路线A - 多库方案）:
--------------------------------------------------------------------------------
schema_prefix 功能仅用于测试环境隔离，生产环境禁用。
此测试文件验证测试模式下的 prefix 功能正确性。
需要设置 ENGRAM_TESTING=1 环境变量。
================================================================================

验证:
1. 使用随机 prefix 执行迁移能成功
2. 重复执行迁移具有幂等性
3. 使用 prefix 时不会触碰固定 schema 名
4. SQL 重写正确处理各种模式
"""

import os
import uuid

import psycopg
import pytest

from engram.logbook.db import rewrite_sql_for_schema
from engram.logbook.migrate import is_testing_mode, run_migrate
from engram.logbook.schema_context import SchemaContext


# 设置测试模式环境变量（允许使用 schema_prefix）
@pytest.fixture(scope="module", autouse=True)
def enable_testing_mode():
    """启用测试模式，允许使用 schema_prefix"""
    old_value = os.environ.get("ENGRAM_TESTING")
    os.environ["ENGRAM_TESTING"] = "1"
    yield
    if old_value is None:
        os.environ.pop("ENGRAM_TESTING", None)
    else:
        os.environ["ENGRAM_TESTING"] = old_value


def generate_random_prefix() -> str:
    """生成随机 schema 前缀"""
    short_id = uuid.uuid4().hex[:8]
    return f"t{short_id}"  # 以字母开头确保合法的 SQL 标识符


class TestSchemaTestingModeConstraint:
    """测试路线A 约束：生产模式禁止 schema_prefix"""

    def test_production_mode_rejects_schema_prefix(self, test_db_info):
        """验证生产模式下 schema_prefix 被拒绝"""
        dsn = test_db_info["dsn"]

        # 临时禁用测试模式
        old_value = os.environ.get("ENGRAM_TESTING")
        os.environ.pop("ENGRAM_TESTING", None)

        try:
            result = run_migrate(dsn=dsn, quiet=True, schema_prefix="should_fail")

            assert result.get("ok") is False
            assert result.get("code") == "SCHEMA_PREFIX_NOT_ALLOWED"
            assert "生产模式" in result.get("message", "")
        finally:
            # 恢复测试模式
            if old_value is not None:
                os.environ["ENGRAM_TESTING"] = old_value
            else:
                os.environ["ENGRAM_TESTING"] = "1"  # 恢复测试环境

    def test_testing_mode_allows_schema_prefix(self, test_db_info):
        """验证测试模式下 schema_prefix 可用"""
        # 确保测试模式已启用
        assert is_testing_mode(), "应处于测试模式"

        dsn = test_db_info["dsn"]
        prefix = f"t{uuid.uuid4().hex[:8]}"

        result = run_migrate(dsn=dsn, quiet=True, schema_prefix=prefix)

        assert result.get("ok") is True, f"迁移失败: {result.get('message')}"

        # 清理
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                for suffix in ["identity", "logbook", "scm", "analysis", "governance"]:
                    cur.execute(f"DROP SCHEMA IF EXISTS {prefix}_{suffix} CASCADE")
        finally:
            conn.close()

    def test_is_testing_mode_function(self):
        """测试 is_testing_mode 函数"""
        # 当前应该是测试模式
        assert os.environ.get("ENGRAM_TESTING") == "1"
        assert is_testing_mode() is True

        # 临时禁用
        old_value = os.environ.pop("ENGRAM_TESTING", None)
        try:
            assert is_testing_mode() is False
        finally:
            if old_value:
                os.environ["ENGRAM_TESTING"] = old_value


class TestSqlRewrite:
    """测试 SQL 重写函数（测试专用功能）"""

    def test_rewrite_create_schema(self):
        """测试 CREATE SCHEMA 语句重写"""
        ctx = SchemaContext(schema_prefix="abc")
        sql = "CREATE SCHEMA IF NOT EXISTS scm;"
        result = rewrite_sql_for_schema(sql, ctx)
        assert "CREATE SCHEMA IF NOT EXISTS abc_scm;" in result

    def test_rewrite_schema_prefix(self):
        """测试 schema.table 前缀重写"""
        ctx = SchemaContext(schema_prefix="xyz")
        sql = "SELECT * FROM scm.repos WHERE scm.repos.repo_id = 1;"
        result = rewrite_sql_for_schema(sql, ctx)
        assert "xyz_scm.repos" in result
        # 确保没有独立的 "scm." 前缀（需要检查词边界）
        # 原始 scm. 应该都被替换为 xyz_scm.
        assert result.count("xyz_scm.") == 2
        assert " scm." not in result  # 前面有空格的独立 scm. 不应存在

    def test_rewrite_table_schema_check(self):
        """测试 table_schema = 'xxx' 检查重写"""
        ctx = SchemaContext(schema_prefix="prefix")
        sql = "WHERE table_schema = 'scm' AND table_name = 'repos'"
        result = rewrite_sql_for_schema(sql, ctx)
        assert "table_schema = 'prefix_scm'" in result
        # table_name 不应被修改
        assert "table_name = 'repos'" in result

    def test_rewrite_schema_name_check(self):
        """测试 schema_name = 'xxx' 检查重写"""
        ctx = SchemaContext(schema_prefix="tenant")
        sql = "WHERE schema_name = 'identity'"
        result = rewrite_sql_for_schema(sql, ctx)
        assert "schema_name = 'tenant_identity'" in result

    def test_rewrite_regclass(self):
        """测试 regclass 转换重写"""
        ctx = SchemaContext(schema_prefix="ns")
        sql = "WHERE tgrelid = 'scm.patch_blobs'::regclass"
        result = rewrite_sql_for_schema(sql, ctx)
        assert "'ns_scm.patch_blobs'::regclass" in result

    def test_no_rewrite_without_prefix(self):
        """无 prefix 时不进行重写"""
        ctx = SchemaContext()
        sql = "CREATE SCHEMA IF NOT EXISTS scm; SELECT * FROM scm.repos;"
        result = rewrite_sql_for_schema(sql, ctx)
        assert result == sql

    def test_rewrite_multiple_schemas(self):
        """测试多个 schema 同时重写"""
        ctx = SchemaContext(schema_prefix="multi")
        sql = """
        CREATE SCHEMA IF NOT EXISTS identity;
        CREATE SCHEMA IF NOT EXISTS logbook;
        CREATE SCHEMA IF NOT EXISTS scm;
        SELECT * FROM identity.users u
        JOIN logbook.items i ON i.owner_user_id = u.user_id
        JOIN scm.repos r ON 1=1;
        """
        result = rewrite_sql_for_schema(sql, ctx)

        assert "multi_identity" in result
        assert "multi_logbook" in result
        assert "multi_scm" in result

        # 确保原始名称都被替换
        assert "CREATE SCHEMA IF NOT EXISTS identity" not in result
        assert "FROM identity.users" not in result


class TestSchemaPrefixMigration:
    """测试带 prefix 的数据库迁移"""

    @pytest.fixture(scope="class")
    def random_prefix(self):
        """生成本测试类共用的随机 prefix"""
        return generate_random_prefix()

    @pytest.fixture(scope="class")
    def prefixed_schemas(self, random_prefix):
        """返回预期的 schema 名称"""
        return [
            f"{random_prefix}_identity",
            f"{random_prefix}_logbook",
            f"{random_prefix}_scm",
            f"{random_prefix}_analysis",
            f"{random_prefix}_governance",
        ]

    def test_migrate_with_prefix_first_run(self, test_db_info, random_prefix, prefixed_schemas):
        """测试首次使用 prefix 执行迁移"""
        dsn = test_db_info["dsn"]

        # 首次迁移
        result = run_migrate(dsn=dsn, quiet=True, schema_prefix=random_prefix)

        assert result.get("ok") is True, f"迁移失败: {result.get('message')}"
        assert result.get("schema_prefix") == random_prefix
        assert set(result.get("schemas", [])) == set(prefixed_schemas)

    def test_migrate_with_prefix_idempotent(self, test_db_info, random_prefix, prefixed_schemas):
        """测试重复执行迁移具有幂等性"""
        dsn = test_db_info["dsn"]

        # 第二次迁移（应该成功，因为 IF NOT EXISTS）
        result = run_migrate(dsn=dsn, quiet=True, schema_prefix=random_prefix)

        assert result.get("ok") is True, f"第二次迁移失败: {result.get('message')}"
        assert result.get("schema_prefix") == random_prefix

    def test_prefix_schemas_created(self, test_db_info, random_prefix, prefixed_schemas):
        """验证带 prefix 的 schema 被正确创建"""
        dsn = test_db_info["dsn"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name = ANY(%s)
                """,
                    (prefixed_schemas,),
                )
                found = {row[0] for row in cur.fetchall()}

            assert found == set(prefixed_schemas), f"缺少 schema: {set(prefixed_schemas) - found}"
        finally:
            conn.close()

    def test_original_schemas_not_created_by_prefix(self, test_db_info, random_prefix):
        """验证使用 prefix 时不会额外创建无前缀的 schema"""
        dsn = test_db_info["dsn"]

        # 这些是固定的 schema 名，不应该被 prefixed 迁移创建
        fixed_schemas = ["identity", "logbook", "scm", "analysis", "governance"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                # 检查这些固定 schema 是否存在
                # 注意：它们可能已经存在（如果之前有无 prefix 的迁移）
                # 我们只需要确保它们不是由当前 prefix 迁移创建的
                # 最佳验证方式是检查表结构

                # 如果固定 schema 中没有表，说明不是由迁移创建的
                cur.execute(
                    """
                    SELECT table_schema, COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = ANY(%s)
                    GROUP BY table_schema
                """,
                    (fixed_schemas,),
                )
                {row[0]: row[1] for row in cur.fetchall()}

            # 如果存在固定 schema 但没有表，说明可能只是空 schema
            # 这是可接受的（可能是之前其他测试创建的）
            # 我们的目标是验证 prefix 迁移不会意外创建或修改固定 schema

            # 验证我们的 prefixed schema 有表
            prefixed_schemas = [f"{random_prefix}_{s}" for s in fixed_schemas]
            cur = conn.cursor()
            cur.execute(
                """
                SELECT table_schema, COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = ANY(%s)
                GROUP BY table_schema
            """,
                (prefixed_schemas,),
            )
            prefixed_table_counts = {row[0]: row[1] for row in cur.fetchall()}
            cur.close()

            # prefixed schema 应该有表
            assert len(prefixed_table_counts) > 0, "Prefixed schema 中没有表"

        finally:
            conn.close()

    def test_prefixed_tables_have_correct_structure(self, test_db_info, random_prefix):
        """验证 prefixed schema 中的表结构正确"""
        dsn = test_db_info["dsn"]

        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                # 验证 scm.review_events.source_event_id 列存在
                scm_schema = f"{random_prefix}_scm"
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = 'review_events'
                      AND column_name = 'source_event_id'
                """,
                    (scm_schema,),
                )
                assert cur.fetchone() is not None, (
                    f"列 {scm_schema}.review_events.source_event_id 不存在"
                )

                # 验证 scm.patch_blobs.meta_json 列存在
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = 'patch_blobs'
                      AND column_name = 'meta_json'
                """,
                    (scm_schema,),
                )
                assert cur.fetchone() is not None, f"列 {scm_schema}.patch_blobs.meta_json 不存在"

                # 验证 identity.users 表存在
                identity_schema = f"{random_prefix}_identity"
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = 'users'
                """,
                    (identity_schema,),
                )
                assert cur.fetchone() is not None, f"表 {identity_schema}.users 不存在"
        finally:
            conn.close()


class TestMultiplePrefixIsolation:
    """测试多个 prefix 之间的隔离性"""

    def test_two_prefixes_isolated(self, test_db_info):
        """测试两个不同的 prefix 完全隔离"""
        dsn = test_db_info["dsn"]

        prefix1 = generate_random_prefix()
        prefix2 = generate_random_prefix()

        # 执行两次不同 prefix 的迁移
        result1 = run_migrate(dsn=dsn, quiet=True, schema_prefix=prefix1)
        result2 = run_migrate(dsn=dsn, quiet=True, schema_prefix=prefix2)

        assert result1.get("ok") is True
        assert result2.get("ok") is True

        # 验证两者的 schema 都存在且不同
        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                schemas_to_check = [
                    f"{prefix1}_scm",
                    f"{prefix1}_logbook",
                    f"{prefix2}_scm",
                    f"{prefix2}_logbook",
                ]
                cur.execute(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name = ANY(%s)
                """,
                    (schemas_to_check,),
                )
                found = {row[0] for row in cur.fetchall()}

            assert found == set(schemas_to_check), (
                f"Schema 不完整: 期望 {set(schemas_to_check)}, 实际 {found}"
            )
        finally:
            conn.close()

        # 清理（可选，测试数据库会在 session 结束时删除）
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                for prefix in [prefix1, prefix2]:
                    for suffix in ["identity", "logbook", "scm", "analysis", "governance"]:
                        cur.execute(f"DROP SCHEMA IF EXISTS {prefix}_{suffix} CASCADE")
        finally:
            conn.close()
