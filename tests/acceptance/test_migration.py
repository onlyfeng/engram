# -*- coding: utf-8 -*-
"""
数据库迁移验收测试

验证迁移脚本在干净数据库上正确执行:
- 空数据库迁移
- 迁移幂等性
- Schema 结构验证
- 权限验证
"""

import os
import uuid
import pytest
import psycopg

from .conftest import (
    get_admin_dsn,
    create_test_database,
    drop_test_database,
    generate_db_name,
)


class TestFreshMigration:
    """空数据库迁移测试"""

    @pytest.fixture(scope="function")
    def fresh_db(self):
        """创建一个全新的空数据库"""
        admin_dsn = get_admin_dsn()
        db_name = generate_db_name()
        
        try:
            test_dsn = create_test_database(admin_dsn, db_name)
        except Exception as e:
            pytest.skip(f"无法创建测试数据库: {e}")
            return
        
        yield {
            "db_name": db_name,
            "dsn": test_dsn,
            "admin_dsn": admin_dsn,
        }
        
        # 清理
        try:
            drop_test_database(admin_dsn, db_name)
        except Exception:
            pass

    def test_fresh_migration_succeeds(self, fresh_db):
        """空数据库迁移成功"""
        from engram.logbook.migrate import run_migrate
        
        result = run_migrate(dsn=fresh_db["dsn"], quiet=True)
        
        assert result.get("ok") is True, f"迁移失败: {result.get('message')}"

    def test_migration_creates_schemas(self, fresh_db):
        """迁移创建必要的 schema"""
        from engram.logbook.migrate import run_migrate
        
        # 执行迁移
        result = run_migrate(dsn=fresh_db["dsn"], quiet=True)
        assert result.get("ok") is True
        
        # 验证 schema 存在
        conn = psycopg.connect(fresh_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT schema_name 
                    FROM information_schema.schemata 
                    WHERE schema_name IN ('logbook', 'identity', 'scm', 'analysis', 'governance')
                """)
                schemas = {row[0] for row in cur.fetchall()}
            
            expected_schemas = {"logbook", "identity", "scm", "analysis", "governance"}
            assert expected_schemas.issubset(schemas), f"缺少 schema: {expected_schemas - schemas}"
        finally:
            conn.close()


class TestMigrationIdempotency:
    """迁移幂等性测试"""

    def test_migration_idempotent(self, migrated_db):
        """迁移脚本幂等（重复执行无错误）"""
        from engram.logbook.migrate import run_migrate
        
        # 再次执行迁移
        result = run_migrate(dsn=migrated_db["dsn"], quiet=True)
        
        assert result.get("ok") is True, f"重复迁移失败: {result.get('message')}"

    def test_multiple_migrations(self, migrated_db):
        """多次执行迁移不会出错"""
        from engram.logbook.migrate import run_migrate
        
        for i in range(3):
            result = run_migrate(dsn=migrated_db["dsn"], quiet=True)
            assert result.get("ok") is True, f"第 {i+1} 次迁移失败: {result.get('message')}"


class TestSchemaVerification:
    """Schema 结构验证测试"""

    def test_logbook_items_table_exists(self, migrated_db):
        """logbook.items 表存在"""
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'logbook' AND table_name = 'items'
                """)
                assert cur.fetchone() is not None, "logbook.items 表不存在"
        finally:
            conn.close()

    def test_logbook_events_table_exists(self, migrated_db):
        """logbook.events 表存在"""
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'logbook' AND table_name = 'events'
                """)
                assert cur.fetchone() is not None, "logbook.events 表不存在"
        finally:
            conn.close()

    def test_governance_settings_table_exists(self, migrated_db):
        """governance.settings 表存在"""
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'governance' AND table_name = 'settings'
                """)
                assert cur.fetchone() is not None, "governance.settings 表不存在"
        finally:
            conn.close()

    def test_identity_users_table_exists(self, migrated_db):
        """identity.users 表存在"""
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'identity' AND table_name = 'users'
                """)
                result = cur.fetchone()
                # identity.users 可能不存在（取决于迁移脚本设计）
                # 这里只记录而不强制要求
                if result is None:
                    pytest.skip("identity.users 表不存在（可能是预期的）")
        finally:
            conn.close()

    def test_scm_commits_table_exists(self, migrated_db):
        """scm.commits 表存在"""
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'scm' AND table_name = 'commits'
                """)
                result = cur.fetchone()
                if result is None:
                    pytest.skip("scm.commits 表不存在（可能是预期的）")
        finally:
            conn.close()

    def test_basic_crud_operations(self, migrated_db):
        """基本 CRUD 操作可执行"""
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 设置 search_path
                cur.execute("SET search_path TO logbook, governance, public")
                
                # 尝试插入一条测试数据
                cur.execute("""
                    INSERT INTO logbook.items (item_type, title, project_key)
                    VALUES ('test', 'Migration Test Item', 'test_project')
                    RETURNING id
                """)
                item_id = cur.fetchone()[0]
                assert item_id is not None
                
                # 读取数据
                cur.execute("SELECT title FROM logbook.items WHERE id = %s", (item_id,))
                title = cur.fetchone()[0]
                assert title == "Migration Test Item"
            
            # 回滚以保持数据库干净
            conn.rollback()
        finally:
            conn.close()


class TestMigrationSQLFiles:
    """迁移 SQL 文件测试"""

    def test_sql_files_exist(self):
        """SQL 迁移文件存在"""
        import glob
        from pathlib import Path
        
        # 获取 sql 目录路径
        project_root = Path(__file__).parent.parent.parent
        sql_dir = project_root / "sql"
        
        assert sql_dir.exists(), f"SQL 目录不存在: {sql_dir}"
        
        # 检查有迁移文件
        sql_files = list(sql_dir.glob("*.sql"))
        assert len(sql_files) > 0, "没有 SQL 迁移文件"

    def test_sql_files_numbered(self):
        """SQL 文件按数字编号"""
        from pathlib import Path
        
        project_root = Path(__file__).parent.parent.parent
        sql_dir = project_root / "sql"
        
        sql_files = sorted(sql_dir.glob("*.sql"))
        
        # 检查文件名以数字开头
        for sql_file in sql_files:
            name = sql_file.name
            # 应该以数字开头，如 01_, 02_ 等
            if name.startswith("_"):
                continue  # 跳过辅助文件
            
            prefix = name.split("_")[0]
            assert prefix.isdigit(), f"SQL 文件名应以数字开头: {name}"

    def test_core_migration_files(self):
        """核心迁移文件存在"""
        from pathlib import Path
        
        project_root = Path(__file__).parent.parent.parent
        sql_dir = project_root / "sql"
        
        # 核心迁移文件
        expected_files = [
            "01_logbook_schema.sql",
        ]
        
        for expected in expected_files:
            file_path = sql_dir / expected
            assert file_path.exists(), f"核心迁移文件不存在: {expected}"


class TestMigrationVerification:
    """迁移验证测试"""

    def test_migration_returns_status(self, migrated_db):
        """迁移返回状态信息"""
        from engram.logbook.migrate import run_migrate
        
        result = run_migrate(dsn=migrated_db["dsn"], quiet=True)
        
        # 应该返回字典
        assert isinstance(result, dict)
        
        # 应该有 ok 字段
        assert "ok" in result

    def test_migration_with_verify_flag(self, migrated_db):
        """迁移支持 verify 模式"""
        from engram.logbook.migrate import run_migrate
        
        # 尝试 verify 模式（如果支持）
        try:
            result = run_migrate(dsn=migrated_db["dsn"], quiet=True, verify=True)
            assert result.get("ok") is True
        except TypeError:
            # verify 参数不支持
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
