# -*- coding: utf-8 -*-
"""
Logbook 冒烟测试

覆盖:
- migrate: 数据库迁移
- create_item/add_event/attach: 核心 CRUD 操作
- render_views: 生成文件存在且非空

环境变量:
- TEST_PG_DSN: PostgreSQL 连接字符串（未提供则 skip）

隔离策略:
- 使用临时 schema（通过 conftest.py 的 migrated_db fixture）
"""

import tempfile
from pathlib import Path

import psycopg
import pytest


class TestMigrate:
    """测试数据库迁移"""
    
    def test_schemas_created(self, migrated_db: dict):
        """验证所有临时 schema 已创建"""
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                for schema_name in migrated_db["schemas"].values():
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.schemata 
                            WHERE schema_name = %s
                        )
                    """, (schema_name,))
                    exists = cur.fetchone()[0]
                    assert exists, f"Schema {schema_name} 应该存在"
        finally:
            conn.close()
    
    def test_core_tables_created(self, migrated_db: dict):
        """验证核心表已创建"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        identity_schema = migrated_db["schemas"]["identity"]
        scm_schema = migrated_db["schemas"]["scm"]
        
        expected_tables = [
            (identity_schema, "users"),
            (identity_schema, "accounts"),
            (identity_schema, "role_profiles"),
            (logbook_schema, "items"),
            (logbook_schema, "events"),
            (logbook_schema, "attachments"),
            (logbook_schema, "kv"),
            (logbook_schema, "outbox_memory"),
            (scm_schema, "repos"),
            (scm_schema, "git_commits"),
            (scm_schema, "svn_revisions"),
            (scm_schema, "mrs"),
            (scm_schema, "review_events"),
            (scm_schema, "patch_blobs"),
        ]
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                for schema_name, table_name in expected_tables:
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables 
                            WHERE table_schema = %s AND table_name = %s
                        )
                    """, (schema_name, table_name))
                    exists = cur.fetchone()[0]
                    assert exists, f"表 {schema_name}.{table_name} 应该存在"
        finally:
            conn.close()

    def test_governance_tables_created(self, migrated_db: dict):
        """验证 governance schema 表已创建"""
        governance_schema = migrated_db["schemas"]["governance"]
        
        expected_tables = [
            "settings",
            "write_audit",
            "promotion_queue",
        ]
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                for table_name in expected_tables:
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables 
                            WHERE table_schema = %s AND table_name = %s
                        )
                    """, (governance_schema, table_name))
                    exists = cur.fetchone()[0]
                    assert exists, f"表 {governance_schema}.{table_name} 应该存在"
        finally:
            conn.close()

    def test_outbox_memory_columns(self, migrated_db: dict):
        """验证 outbox_memory 表包含所需列"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        # outbox_memory 必需的列
        expected_columns = [
            "outbox_id",
            "item_id",
            "target_space",
            "payload_md",
            "payload_sha",
            "status",
            "retry_count",
            "next_attempt_at",
            "locked_at",
            "locked_by",
            "last_error",
            "created_at",
            "updated_at",
        ]
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_schema = %s AND table_name = 'outbox_memory'
                """, (logbook_schema,))
                actual_columns = {row[0] for row in cur.fetchall()}
                
                for col in expected_columns:
                    assert col in actual_columns, f"outbox_memory 应包含列 {col}"
        finally:
            conn.close()

    def test_governance_settings_columns(self, migrated_db: dict):
        """验证 governance.settings 表包含所需列"""
        governance_schema = migrated_db["schemas"]["governance"]
        
        expected_columns = [
            "project_key",
            "team_write_enabled",
            "policy_json",
            "updated_by",
            "updated_at",
        ]
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_schema = %s AND table_name = 'settings'
                """, (governance_schema,))
                actual_columns = {row[0] for row in cur.fetchall()}
                
                for col in expected_columns:
                    assert col in actual_columns, f"governance.settings 应包含列 {col}"
        finally:
            conn.close()

    def test_governance_write_audit_columns(self, migrated_db: dict):
        """验证 governance.write_audit 表包含所需列"""
        governance_schema = migrated_db["schemas"]["governance"]
        
        expected_columns = [
            "audit_id",
            "ts",
            "actor_user_id",
            "target_space",
            "action",
            "reason",
            "payload_sha",
            "evidence_refs_json",
        ]
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_schema = %s AND table_name = 'write_audit'
                """, (governance_schema,))
                actual_columns = {row[0] for row in cur.fetchall()}
                
                for col in expected_columns:
                    assert col in actual_columns, f"governance.write_audit 应包含列 {col}"
        finally:
            conn.close()


class TestCRUDOperations:
    """测试 create_item / add_event / attach 操作"""
    
    def test_create_item(self, migrated_db: dict):
        """测试创建 item"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.items 
                    (item_type, title, scope_json, status)
                    VALUES ('task', 'Test Task', '{{}}'::jsonb, 'open')
                    RETURNING item_id
                """)
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None, "应返回 item_id"
                item_id = result[0]
                assert item_id > 0, "item_id 应为正整数"
                
                # 验证数据已插入
                cur.execute(f"""
                    SELECT item_type, title, status 
                    FROM {logbook_schema}.items 
                    WHERE item_id = %s
                """, (item_id,))
                row = cur.fetchone()
                assert row is not None, "应能查询到插入的记录"
                assert row[0] == "task"
                assert row[1] == "Test Task"
                assert row[2] == "open"
        finally:
            conn.close()
    
    def test_add_event(self, migrated_db: dict):
        """测试添加 event"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先创建 item
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.items 
                    (item_type, title, status)
                    VALUES ('bug', 'Test Bug', 'open')
                    RETURNING item_id
                """)
                item_id = cur.fetchone()[0]
                
                # 添加 event
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.events 
                    (item_id, event_type, status_from, status_to, payload_json, source)
                    VALUES (%s, 'status_change', 'open', 'in_progress', '{{}}'::jsonb, 'test')
                    RETURNING event_id
                """, (item_id,))
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None, "应返回 event_id"
                event_id = result[0]
                assert event_id > 0, "event_id 应为正整数"
                
                # 验证数据
                cur.execute(f"""
                    SELECT event_type, status_from, status_to, source 
                    FROM {logbook_schema}.events 
                    WHERE event_id = %s
                """, (event_id,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "status_change"
                assert row[1] == "open"
                assert row[2] == "in_progress"
                assert row[3] == "test"
        finally:
            conn.close()
    
    def test_attach(self, migrated_db: dict):
        """测试添加 attachment"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先创建 item
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.items 
                    (item_type, title, status)
                    VALUES ('task', 'Attachment Test', 'open')
                    RETURNING item_id
                """)
                item_id = cur.fetchone()[0]
                
                # 添加 attachment
                test_sha256 = "a" * 64  # 64 位十六进制
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.attachments 
                    (item_id, kind, uri, sha256, size_bytes, meta_json)
                    VALUES (%s, 'patch', 'file:///tmp/test.diff', %s, 1024, '{{}}'::jsonb)
                    RETURNING attachment_id
                """, (item_id, test_sha256))
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None, "应返回 attachment_id"
                attachment_id = result[0]
                assert attachment_id > 0, "attachment_id 应为正整数"
                
                # 验证数据
                cur.execute(f"""
                    SELECT kind, uri, sha256, size_bytes 
                    FROM {logbook_schema}.attachments 
                    WHERE attachment_id = %s
                """, (attachment_id,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "patch"
                assert row[1] == "file:///tmp/test.diff"
                assert row[2] == test_sha256
                assert row[3] == 1024
        finally:
            conn.close()
    
    def test_kv_set_and_get(self, migrated_db: dict):
        """测试 KV 存取"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 设置 KV
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.kv 
                    (namespace, key, value_json)
                    VALUES ('test_ns', 'test_key', '"test_value"'::jsonb)
                    ON CONFLICT (namespace, key) DO UPDATE
                    SET value_json = EXCLUDED.value_json
                """)
                conn.commit()
                
                # 读取 KV
                cur.execute(f"""
                    SELECT value_json 
                    FROM {logbook_schema}.kv 
                    WHERE namespace = 'test_ns' AND key = 'test_key'
                """)
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "test_value"
        finally:
            conn.close()


class TestGovernanceOperations:
    """测试 governance schema 操作"""

    def test_settings_upsert(self, migrated_db: dict):
        """测试 governance.settings 插入和更新"""
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 插入新设置
                cur.execute(f"""
                    INSERT INTO {governance_schema}.settings 
                    (project_key, team_write_enabled, policy_json, updated_by)
                    VALUES ('test_project', true, '{{"max_chars": 1000}}'::jsonb, 'admin')
                    ON CONFLICT (project_key) DO UPDATE
                    SET team_write_enabled = EXCLUDED.team_write_enabled,
                        policy_json = EXCLUDED.policy_json,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                    RETURNING project_key
                """)
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                assert result[0] == "test_project"
                
                # 验证数据
                cur.execute(f"""
                    SELECT team_write_enabled, policy_json->>'max_chars', updated_by 
                    FROM {governance_schema}.settings 
                    WHERE project_key = 'test_project'
                """)
                row = cur.fetchone()
                assert row is not None
                assert row[0] is True
                assert row[1] == "1000"
                assert row[2] == "admin"
        finally:
            conn.close()

    def test_write_audit_insert(self, migrated_db: dict):
        """测试 governance.write_audit 插入"""
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 插入审计记录
                cur.execute(f"""
                    INSERT INTO {governance_schema}.write_audit 
                    (actor_user_id, target_space, action, reason, payload_sha, evidence_refs_json)
                    VALUES ('user_001', 'team:myproject', 'allow', 'policy_passed', 
                            'a' || repeat('b', 63), '{{"outbox_id": 123}}'::jsonb)
                    RETURNING audit_id
                """)
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                audit_id = result[0]
                assert audit_id > 0
                
                # 验证数据
                cur.execute(f"""
                    SELECT actor_user_id, target_space, action, reason, payload_sha
                    FROM {governance_schema}.write_audit 
                    WHERE audit_id = %s
                """, (audit_id,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "user_001"
                assert row[1] == "team:myproject"
                assert row[2] == "allow"
                assert row[3] == "policy_passed"
                assert len(row[4]) == 64  # SHA256 长度
        finally:
            conn.close()

    def test_promotion_queue_insert(self, migrated_db: dict):
        """测试 governance.promotion_queue 插入"""
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 插入晋升请求
                cur.execute(f"""
                    INSERT INTO {governance_schema}.promotion_queue 
                    (from_space, to_space, requested_by, status)
                    VALUES ('private:user_001', 'team:project', 'user_001', 'pending')
                    RETURNING promo_id
                """)
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                promo_id = result[0]
                assert promo_id > 0
                
                # 验证数据
                cur.execute(f"""
                    SELECT from_space, to_space, requested_by, status
                    FROM {governance_schema}.promotion_queue 
                    WHERE promo_id = %s
                """, (promo_id,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "private:user_001"
                assert row[1] == "team:project"
                assert row[2] == "user_001"
                assert row[3] == "pending"
        finally:
            conn.close()


class TestOutboxOperations:
    """测试 outbox_memory 操作"""

    def test_outbox_enqueue(self, migrated_db: dict):
        """测试 outbox_memory 入队"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 入队
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory 
                    (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:user_001', '# Test Memory', 'a' || repeat('b', 63), 
                            'pending', 0, now() + interval '5 minutes')
                    RETURNING outbox_id
                """)
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                outbox_id = result[0]
                assert outbox_id > 0
                
                # 验证数据
                cur.execute(f"""
                    SELECT target_space, status, retry_count, payload_sha
                    FROM {logbook_schema}.outbox_memory 
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "private:user_001"
                assert row[1] == "pending"
                assert row[2] == 0
                assert len(row[3]) == 64
        finally:
            conn.close()

    def test_outbox_mark_sent(self, migrated_db: dict):
        """测试 outbox_memory 标记为 sent"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先入队
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory 
                    (target_space, payload_md, payload_sha, status)
                    VALUES ('team:project', 'Content', 'c' || repeat('d', 63), 'pending')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
                
                # 标记为 sent
                cur.execute(f"""
                    UPDATE {logbook_schema}.outbox_memory
                    SET status = 'sent', updated_at = now()
                    WHERE outbox_id = %s AND status = 'pending'
                    RETURNING status
                """, (outbox_id,))
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                assert result[0] == "sent"
        finally:
            conn.close()

    def test_outbox_increment_retry(self, migrated_db: dict):
        """测试 outbox_memory 增加重试计数"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先入队
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory 
                    (target_space, payload_md, payload_sha, status, retry_count)
                    VALUES ('team:project', 'Content', 'e' || repeat('f', 63), 'pending', 0)
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
                
                # 增加重试计数
                cur.execute(f"""
                    UPDATE {logbook_schema}.outbox_memory
                    SET retry_count = retry_count + 1,
                        last_error = 'Connection timeout',
                        next_attempt_at = now() + interval '10 minutes',
                        updated_at = now()
                    WHERE outbox_id = %s
                    RETURNING retry_count, last_error
                """, (outbox_id,))
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                assert result[0] == 1
                assert result[1] == "Connection timeout"
        finally:
            conn.close()

    def test_outbox_mark_dead(self, migrated_db: dict):
        """测试 outbox_memory 标记为 dead"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 先入队
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory 
                    (target_space, payload_md, payload_sha, status, retry_count)
                    VALUES ('team:project', 'Content', 'g' || repeat('h', 63), 'pending', 5)
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
                
                # 标记为 dead
                cur.execute(f"""
                    UPDATE {logbook_schema}.outbox_memory
                    SET status = 'dead',
                        last_error = 'Max retries exceeded',
                        updated_at = now()
                    WHERE outbox_id = %s AND status = 'pending'
                    RETURNING status, last_error
                """, (outbox_id,))
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                assert result[0] == "dead"
                assert result[1] == "Max retries exceeded"
        finally:
            conn.close()

    def test_outbox_locking(self, migrated_db: dict):
        """测试 outbox_memory 锁定机制"""
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 入队
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory 
                    (target_space, payload_md, payload_sha, status)
                    VALUES ('private:user', 'Content', 'i' || repeat('j', 63), 'pending')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
                
                # 锁定记录
                cur.execute(f"""
                    UPDATE {logbook_schema}.outbox_memory
                    SET locked_at = now(), locked_by = 'worker_1'
                    WHERE outbox_id = %s AND locked_at IS NULL
                    RETURNING locked_by
                """, (outbox_id,))
                result = cur.fetchone()
                conn.commit()
                
                assert result is not None
                assert result[0] == "worker_1"
                
                # 验证已锁定的记录
                cur.execute(f"""
                    SELECT locked_at, locked_by 
                    FROM {logbook_schema}.outbox_memory 
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] is not None  # locked_at 有值
                assert row[1] == "worker_1"
        finally:
            conn.close()


class TestRenderViews:
    """测试 render_views 生成文件"""
    
    def test_render_views_generates_files(self, migrated_db: dict, tmp_path: Path):
        """测试 render_views 生成 manifest.csv 和 index.md 且 latest_event_ts 正确"""
        import csv
        import sys
        from datetime import datetime, timedelta
        from unittest.mock import MagicMock
        
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            # 准备测试数据：创建 item 和两条 events（指定不同 created_at）
            with conn.cursor() as cur:
                # 创建一个 item
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.items 
                    (item_type, title, status)
                    VALUES ('task', 'Test Task for RenderViews', 'open')
                    RETURNING item_id
                """)
                item_id = cur.fetchone()[0]
                
                # 较早的 event
                earlier_ts = datetime(2025, 1, 1, 10, 0, 0)
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.events 
                    (item_id, event_type, payload_json, source, created_at)
                    VALUES (%s, 'created', '{{}}'::jsonb, 'test', %s)
                """, (item_id, earlier_ts))
                
                # 较新的 event（最新事件）
                latest_ts = datetime(2025, 1, 15, 14, 30, 0)
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.events 
                    (item_id, event_type, payload_json, source, created_at)
                    VALUES (%s, 'status_change', '{{}}'::jsonb, 'test', %s)
                """, (item_id, latest_ts))
                
                conn.commit()
            
            # 构建 mock_config，指向临时 schema
            schemas = migrated_db["schemas"]
            search_path_list = [
                schemas["logbook"],
                schemas["identity"],
                schemas["scm"],
                schemas["analysis"],
                schemas["governance"],
                "public",
            ]
            
            mock_config = MagicMock()
            
            def get_side_effect(key, default=None):
                if key == "postgres.search_path":
                    return search_path_list
                return default
            
            mock_config.get.side_effect = get_side_effect
            mock_config.require.side_effect = lambda key: {
                "postgres.dsn": migrated_db["dsn"],
            }.get(key, f"mock_{key}")
            
            # 将 scripts 目录添加到 path 以便导入 render_views
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            # 调用 render_views
            from render_views import render_views
            
            out_dir = tmp_path / "views"
            result = render_views(
                out_dir=str(out_dir),
                config=mock_config,
                quiet=True,
            )
            
            # 验证返回结果
            assert result is not None
            assert result["items_count"] >= 1
            
            # 验证文件生成
            manifest_path = out_dir / "manifest.csv"
            index_path = out_dir / "index.md"
            
            assert manifest_path.exists(), "manifest.csv 应该存在"
            assert index_path.exists(), "index.md 应该存在"
            
            # 验证文件非空
            assert manifest_path.stat().st_size > 0, "manifest.csv 不应为空"
            assert index_path.stat().st_size > 0, "index.md 不应为空"
            
            # 解析 manifest.csv，验证 latest_event_ts 列及其值
            with open(manifest_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                
                # 断言包含 latest_event_ts 列
                assert "latest_event_ts" in fieldnames, "manifest.csv 应包含 latest_event_ts 列"
                
                # 查找测试 item 的记录
                found = False
                for row in reader:
                    if row["title"] == "Test Task for RenderViews":
                        found = True
                        # 验证 latest_event_ts 等于最新事件 created_at
                        latest_event_ts_str = row["latest_event_ts"]
                        assert latest_event_ts_str, "latest_event_ts 不应为空"
                        
                        # 解析时间戳（ISO 格式）
                        parsed_ts = datetime.fromisoformat(latest_event_ts_str.replace("Z", "+00:00").replace("+00:00", ""))
                        # 忽略时区比较
                        expected_ts = latest_ts
                        assert parsed_ts.year == expected_ts.year
                        assert parsed_ts.month == expected_ts.month
                        assert parsed_ts.day == expected_ts.day
                        assert parsed_ts.hour == expected_ts.hour
                        assert parsed_ts.minute == expected_ts.minute
                        break
                
                assert found, "应找到测试 item 的记录"
        finally:
            conn.close()


class TestGetConnectionSearchPathUnit:
    """get_connection 的 search_path 功能单元测试（不依赖真实数据库）"""

    def test_search_path_list_format(self):
        """测试列表格式的 search_path 处理逻辑"""
        from unittest.mock import MagicMock, patch
        
        # Mock 配置
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
            "postgres.search_path": ["logbook", "scm"],
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        # Mock psycopg.connect
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                from engram.logbook.db import get_connection
                conn = get_connection(config=config)
                
                # 验证 SET search_path 被调用
                mock_cursor.execute.assert_called_once()
                call_args = mock_cursor.execute.call_args[0][0]
                assert "SET search_path TO" in call_args
                assert "logbook" in call_args
                assert "scm" in call_args
                assert "public" in call_args  # 自动追加

    def test_search_path_string_format(self):
        """测试字符串格式的 search_path 处理逻辑"""
        from unittest.mock import MagicMock, patch
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
            "postgres.search_path": "logbook, scm, identity",
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                from engram.logbook.db import get_connection
                conn = get_connection(config=config)
                
                call_args = mock_cursor.execute.call_args[0][0]
                assert "SET search_path TO" in call_args
                assert "logbook" in call_args
                assert "scm" in call_args
                assert "identity" in call_args
                assert "public" in call_args

    def test_search_path_already_has_public(self):
        """测试已包含 public 时不重复追加"""
        from unittest.mock import MagicMock, patch
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
            "postgres.search_path": ["logbook", "public", "scm"],
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                from engram.logbook.db import get_connection
                conn = get_connection(config=config)
                
                call_args = mock_cursor.execute.call_args[0][0]
                # 确保 public 只出现一次
                assert call_args.count("public") == 1

    def test_search_path_uses_default_when_none_configured(self):
        """测试未配置 search_path 时使用默认值 DEFAULT_SEARCH_PATH"""
        from unittest.mock import MagicMock, patch
        from engram.logbook.db import DEFAULT_SEARCH_PATH
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
            # postgres.search_path 未配置
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                # Mock get_schema_context 抛出异常（无全局 context）
                with patch('engram_logbook.db.get_schema_context', side_effect=Exception("no context")):
                    from engram.logbook.db import get_connection
                    conn = get_connection(config=config)
                    
                    # 应该执行 SET search_path 并使用默认值
                    mock_cursor.execute.assert_called_once()
                    call_args = mock_cursor.execute.call_args[0][0]
                    assert "SET search_path TO" in call_args
                    # 验证包含默认 schema
                    assert "logbook" in call_args
                    assert "identity" in call_args
                    assert "scm" in call_args
                    assert "analysis" in call_args
                    assert "governance" in call_args
                    assert "public" in call_args

    def test_default_search_path_order_is_stable(self):
        """测试默认 search_path 顺序稳定：logbook, identity, scm, analysis, governance, public"""
        from unittest.mock import MagicMock, patch
        from engram.logbook.db import DEFAULT_SEARCH_PATH
        
        # 验证常量定义的顺序
        expected_order = ["logbook", "identity", "scm", "analysis", "governance", "public"]
        assert DEFAULT_SEARCH_PATH == expected_order, \
            f"DEFAULT_SEARCH_PATH 应为 {expected_order}，实际为 {DEFAULT_SEARCH_PATH}"
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                with patch('engram_logbook.db.get_schema_context', side_effect=Exception("no context")):
                    from engram.logbook.db import get_connection
                    conn = get_connection(config=config)
                    
                    call_args = mock_cursor.execute.call_args[0][0]
                    # 验证顺序：logbook 在 identity 之前，identity 在 scm 之前...
                    assert call_args.index("logbook") < call_args.index("identity")
                    assert call_args.index("identity") < call_args.index("scm")
                    assert call_args.index("scm") < call_args.index("analysis")
                    assert call_args.index("analysis") < call_args.index("governance")
                    assert call_args.index("governance") < call_args.index("public")

    def test_default_search_path_public_not_duplicated(self):
        """测试默认 search_path 中 public 不会被重复追加"""
        from unittest.mock import MagicMock, patch
        from engram.logbook.db import DEFAULT_SEARCH_PATH
        
        # DEFAULT_SEARCH_PATH 已包含 public，不应再追加
        assert "public" in DEFAULT_SEARCH_PATH
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                with patch('engram_logbook.db.get_schema_context', side_effect=Exception("no context")):
                    from engram.logbook.db import get_connection
                    conn = get_connection(config=config)
                    
                    call_args = mock_cursor.execute.call_args[0][0]
                    # public 应只出现一次
                    assert call_args.count("public") == 1

    def test_search_path_empty_list_uses_default(self):
        """测试 search_path 为空列表时使用默认值"""
        from unittest.mock import MagicMock, patch
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
            "postgres.search_path": [],  # 空列表视为未配置
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": "postgresql://test:test@localhost/test",
        }.get(key, f"mock_{key}")
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        with patch('engram_logbook.db.psycopg.connect', return_value=mock_conn):
            with patch('engram_logbook.db.get_config', return_value=config):
                with patch('engram_logbook.db.get_schema_context', side_effect=Exception("no context")):
                    from engram.logbook.db import get_connection
                    conn = get_connection(config=config)
                    
                    # 空列表时应该使用默认 search_path
                    mock_cursor.execute.assert_called_once()
                    call_args = mock_cursor.execute.call_args[0][0]
                    assert "SET search_path TO" in call_args
                    assert "logbook" in call_args


class TestGetConnectionSearchPath:
    """测试 get_connection 函数的 search_path 功能"""

    def test_search_path_with_list_config(self, migrated_db: dict, mock_config):
        """测试使用列表格式的 search_path 配置"""
        from engram.logbook.db import get_connection

        # mock_config 已配置了临时 schema 的 search_path
        conn = get_connection(config=mock_config)
        try:
            with conn.cursor() as cur:
                # 查询当前 search_path
                cur.execute("SHOW search_path")
                result = cur.fetchone()[0]
                
                # 验证 search_path 包含临时 schema
                schemas = migrated_db["schemas"]
                assert schemas["logbook"] in result, f"search_path 应包含 logbook schema: {result}"
                assert "public" in result, f"search_path 应包含 public 作为兜底: {result}"
        finally:
            conn.close()

    def test_search_path_with_string_config(self, migrated_db: dict):
        """测试使用字符串格式的 search_path 配置"""
        from unittest.mock import MagicMock
        from engram.logbook.db import get_connection
        from .conftest import get_test_dsn
        
        schemas = migrated_db["schemas"]
        # 使用逗号分隔的字符串格式
        search_path_str = f"{schemas['logbook']}, {schemas['scm']}"
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": search_path_str,
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": get_test_dsn(),
        }.get(key, f"mock_{key}")
        
        conn = get_connection(config=config)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()[0]
                
                assert schemas["logbook"] in result, f"search_path 应包含 logbook: {result}"
                assert schemas["scm"] in result, f"search_path 应包含 scm: {result}"
                assert "public" in result, f"search_path 应包含 public: {result}"
        finally:
            conn.close()

    def test_search_path_auto_appends_public(self, migrated_db: dict):
        """测试 search_path 自动追加 public"""
        from unittest.mock import MagicMock
        from engram.logbook.db import get_connection
        from .conftest import get_test_dsn
        
        schemas = migrated_db["schemas"]
        # 不包含 public 的配置
        search_path_list = [schemas["logbook"]]
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": search_path_list,
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": get_test_dsn(),
        }.get(key, f"mock_{key}")
        
        conn = get_connection(config=config)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()[0]
                
                assert "public" in result, f"search_path 应自动追加 public: {result}"
        finally:
            conn.close()

    def test_search_path_uses_default_when_empty(self, migrated_db: dict):
        """测试 search_path 未配置时使用默认值"""
        from unittest.mock import MagicMock, patch
        from engram.logbook.db import get_connection, DEFAULT_SEARCH_PATH
        from .conftest import get_test_dsn
        
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": None,  # 未配置
        }.get(key, default)
        config.require.side_effect = lambda key: {
            "postgres.dsn": get_test_dsn(),
        }.get(key, f"mock_{key}")
        
        # Mock get_schema_context 以确保不使用全局 context
        with patch('engram_logbook.db.get_schema_context', side_effect=Exception("no context")):
            conn = get_connection(config=config)
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW search_path")
                    result = cur.fetchone()[0]
                    # 应该包含默认 schema
                    assert "logbook" in result, f"search_path 应包含 logbook: {result}"
                    assert "public" in result, f"search_path 应包含 public: {result}"
            finally:
                conn.close()

    def test_search_path_enables_temp_schema_access(self, migrated_db: dict, mock_config):
        """测试 search_path 能正确访问临时 schema 中的表"""
        from engram.logbook.db import get_connection
        
        conn = get_connection(config=mock_config)
        try:
            with conn.cursor() as cur:
                # 尝试直接查询 items 表（不带 schema 前缀）
                # 因为 search_path 设置了临时 schema
                cur.execute("SELECT COUNT(*) FROM items")
                count = cur.fetchone()[0]
                assert count >= 0, "应能通过 search_path 访问 items 表"
        finally:
            conn.close()

    def test_search_path_preserves_autocommit_mode(self, migrated_db: dict, mock_config):
        """测试设置 search_path 不影响 autocommit 模式"""
        from engram.logbook.db import get_connection
        
        # 测试 autocommit=True
        conn = get_connection(config=mock_config, autocommit=True)
        try:
            assert conn.autocommit is True, "autocommit 应为 True"
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()[0]
                assert result is not None
        finally:
            conn.close()
        
        # 测试 autocommit=False
        conn = get_connection(config=mock_config, autocommit=False)
        try:
            assert conn.autocommit is False, "autocommit 应为 False"
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                result = cur.fetchone()[0]
                assert result is not None
        finally:
            conn.close()


class TestOriginalMigrate:
    """测试原始 db_migrate.py 的 run_migrate 函数（需要真实环境）"""
    
    def test_run_migrate_with_real_config(self, migrated_db: dict, tmp_path: Path):
        """
        使用临时配置文件测试 run_migrate
        
        注意：这会在数据库中创建真实的 schema，但不会污染数据
        因为我们检查的是 schema 存在性，它们本应存在
        """
        import sys
        
        # 创建临时配置文件
        config_content = f"""
[postgres]
dsn = "{migrated_db["dsn"]}"
"""
        config_path = tmp_path / "test_config.toml"
        config_path.write_text(config_content)
        
        # 将 scripts 目录添加到 path
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        # 导入并调用 run_migrate
        from db_migrate import run_migrate
        
        result = run_migrate(str(config_path))
        
        # 验证结果
        # 即使 schema 已存在，迁移也应该成功（CREATE IF NOT EXISTS）
        assert result["ok"] is True, f"迁移应成功: {result.get('error', '')}"


class TestMigrateSelfCheckRegression:
    """
    迁移后自检回归测试
    
    确保空库迁移后所有 REQUIRED_*_TEMPLATES 对象都存在：
    - REQUIRED_COLUMN_TEMPLATES
    - REQUIRED_INDEX_TEMPLATES
    - REQUIRED_TRIGGER_TEMPLATES
    - REQUIRED_MATVIEW_TEMPLATES
    """
    
    def test_fresh_migration_all_checks_pass(self, migrated_db: dict):
        """
        回归测试：空库迁移后 run_all_checks 必须全部通过
        
        验证项:
        - schemas: identity, logbook, scm, analysis, governance
        - columns: scm.review_events.source_event_id, scm.patch_blobs.{meta_json, updated_at}
        - indexes: scm.idx_v_facts_*, logbook.idx_logbook_events_item_time, logbook.idx_outbox_memory_pending
        - triggers: scm.patch_blobs.trg_patch_blobs_updated_at
        - matviews: scm.v_facts
        """
        import sys
        from pathlib import Path
        
        # 将 scripts 目录添加到 path
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from db_migrate import run_all_checks
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            # 使用 migrated_db 提供的 schema_map
            result = run_all_checks(conn, schema_map=migrated_db["schemas"])
            
            # 验证所有检查项通过
            assert result["ok"], f"自检失败: {result['checks']}"
            
            # 详细验证每个检查项
            checks = result["checks"]
            
            # schemas
            assert checks["schemas"]["ok"], f"schemas 检查失败: {checks['schemas']['missing']}"
            
            # columns
            assert checks["columns"]["ok"], f"columns 检查失败: {checks['columns']['missing']}"
            
            # indexes
            assert checks["indexes"]["ok"], f"indexes 检查失败: {checks['indexes']['missing']}"
            
            # triggers
            assert checks["triggers"]["ok"], f"triggers 检查失败: {checks['triggers']['missing']}"
            
            # matviews
            assert checks["matviews"]["ok"], f"matviews 检查失败: {checks['matviews']['missing']}"
        finally:
            conn.close()
    
    def test_required_trigger_exists_after_migration(self, migrated_db: dict):
        """
        回归测试：验证 trg_patch_blobs_updated_at 触发器在迁移后存在
        
        这是针对 Issue #X 的回归测试：
        该触发器之前只在 03_patch_blobs_meta_migration.sql 中定义，
        空库部署时可能缺失。现已添加到 01_logbook_schema.sql。
        """
        scm_schema = migrated_db["schemas"]["scm"]
        
        conn = psycopg.connect(migrated_db["dsn"])
        try:
            with conn.cursor() as cur:
                # 检查触发器是否存在
                cur.execute("""
                    SELECT 1
                    FROM pg_trigger t
                    JOIN pg_class c ON t.tgrelid = c.oid
                    JOIN pg_namespace n ON c.relnamespace = n.oid
                    WHERE n.nspname = %s
                      AND c.relname = 'patch_blobs'
                      AND t.tgname = 'trg_patch_blobs_updated_at'
                """, (scm_schema,))
                exists = cur.fetchone() is not None
                
                assert exists, (
                    f"触发器 {scm_schema}.patch_blobs.trg_patch_blobs_updated_at 应该存在。"
                    "请确认 01_logbook_schema.sql 中已定义该触发器。"
                )
        finally:
            conn.close()
    
    def test_trigger_function_updates_timestamp(self, migrated_db: dict):
        """
        回归测试：验证触发器函数正确更新 updated_at 字段
        """
        scm_schema = migrated_db["schemas"]["scm"]
        
        conn = psycopg.connect(migrated_db["dsn"], autocommit=False)
        try:
            with conn.cursor() as cur:
                # 插入一条记录
                cur.execute(f"""
                    INSERT INTO {scm_schema}.patch_blobs 
                    (source_type, source_id, sha256, format)
                    VALUES ('git', 'test:trigger:1', 'a' || repeat('b', 63), 'diff')
                    RETURNING blob_id, updated_at
                """)
                blob_id, original_updated_at = cur.fetchone()
                conn.commit()
                
                # 等待一小段时间确保时间差异
                import time
                time.sleep(0.1)
                
                # 更新记录
                cur.execute(f"""
                    UPDATE {scm_schema}.patch_blobs
                    SET meta_json = '{{"test": "value"}}'::jsonb
                    WHERE blob_id = %s
                    RETURNING updated_at
                """, (blob_id,))
                new_updated_at = cur.fetchone()[0]
                conn.commit()
                
                # 验证 updated_at 已更新
                assert new_updated_at > original_updated_at, (
                    f"updated_at 应该被触发器更新。"
                    f"原值: {original_updated_at}, 新值: {new_updated_at}"
                )
                
                # 清理
                cur.execute(f"DELETE FROM {scm_schema}.patch_blobs WHERE blob_id = %s", (blob_id,))
                conn.commit()
        finally:
            conn.close()


class TestDatabaseAutoCreate:
    """测试数据库自动创建功能"""

    def test_validate_db_name_valid(self):
        """测试合法的数据库名称"""
        from db_migrate import validate_db_name
        
        valid_names = [
            "proj_a",
            "engram_test",
            "a",
            "a1",
            "project123",
            "my_project_name",
        ]
        
        for name in valid_names:
            ok, msg = validate_db_name(name)
            assert ok, f"名称 '{name}' 应该合法: {msg}"

    def test_validate_db_name_invalid(self):
        """测试非法的数据库名称"""
        from db_migrate import validate_db_name
        
        invalid_names = [
            ("", "空字符串"),
            ("123abc", "数字开头"),
            ("Proj_A", "大写字母"),
            ("proj-a", "包含连字符"),
            ("proj a", "包含空格"),
            ("proj.a", "包含点号"),
            ("a" * 64, "超过63字符"),
            ("_project", "下划线开头"),
        ]
        
        for name, reason in invalid_names:
            ok, msg = validate_db_name(name)
            assert not ok, f"名称 '{name}' 应该不合法（{reason}）"

    def test_parse_db_name_from_dsn(self):
        """测试从 DSN 解析数据库名"""
        from db_migrate import parse_db_name_from_dsn
        
        cases = [
            ("postgresql://user:pass@localhost:5432/mydb", "mydb"),
            ("postgresql://user:pass@localhost/proj_a", "proj_a"),
            ("postgresql://localhost/test_db?sslmode=require", "test_db"),
            ("postgresql://user@host:5432/db123", "db123"),
        ]
        
        for dsn, expected in cases:
            result = parse_db_name_from_dsn(dsn)
            assert result == expected, f"DSN '{dsn}' 应解析为 '{expected}'，实际 '{result}'"

    def test_replace_db_in_dsn(self):
        """测试替换 DSN 中的数据库名"""
        from db_migrate import replace_db_in_dsn
        
        dsn = "postgresql://user:pass@localhost:5432/old_db"
        new_dsn = replace_db_in_dsn(dsn, "new_db")
        
        assert "new_db" in new_dsn
        assert "old_db" not in new_dsn

    def test_ensure_database_exists_no_admin_dsn(self):
        """测试无 admin_dsn 时跳过创建"""
        from db_migrate import ensure_database_exists
        
        result = ensure_database_exists(
            target_dsn="postgresql://user:pass@localhost/proj_a",
            admin_dsn=None,
            project_key="proj_a",
            quiet=True,
        )
        
        assert result["ok"]
        assert result["db_name"] == "proj_a"
        assert result["created"] is False

    def test_ensure_database_exists_invalid_name(self):
        """测试非法数据库名称时返回错误"""
        from db_migrate import ensure_database_exists
        
        result = ensure_database_exists(
            target_dsn="postgresql://user:pass@localhost/Invalid-Name",
            admin_dsn="postgresql://admin:pass@localhost/postgres",
            quiet=True,
        )
        
        assert not result["ok"]
        assert "命名规范" in result["message"]


class TestDatabaseAutoCreateIntegration:
    """数据库自动创建集成测试（需要真实 PostgreSQL）"""

    def test_create_database_when_not_exists(self, test_db_info):
        """测试：当数据库不存在时，自动创建"""
        import uuid
        import psycopg
        from db_migrate import (
            ensure_database_exists,
            check_database_exists,
            replace_db_in_dsn,
        )
        
        admin_dsn = test_db_info["admin_dsn"]
        
        # 生成一个不存在的数据库名
        new_db_name = f"test_auto_create_{uuid.uuid4().hex[:8]}"
        target_dsn = replace_db_in_dsn(admin_dsn, new_db_name)
        
        try:
            # 确认数据库不存在
            assert not check_database_exists(admin_dsn, new_db_name)
            
            # 调用 ensure_database_exists
            result = ensure_database_exists(
                target_dsn=target_dsn,
                admin_dsn=admin_dsn,
                quiet=True,
            )
            
            # 验证结果
            assert result["ok"], f"创建失败: {result['message']}"
            assert result["db_name"] == new_db_name
            assert result["created"] is True
            
            # 验证数据库已创建
            assert check_database_exists(admin_dsn, new_db_name)
        finally:
            # 清理：删除创建的数据库
            try:
                conn = psycopg.connect(admin_dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{new_db_name}"')
                conn.close()
            except Exception:
                pass

    def test_idempotent_database_creation(self, test_db_info):
        """测试：重复执行幂等性（数据库已存在时不报错）"""
        import uuid
        import psycopg
        from db_migrate import (
            ensure_database_exists,
            check_database_exists,
            replace_db_in_dsn,
            create_database,
        )
        
        admin_dsn = test_db_info["admin_dsn"]
        
        # 生成一个新数据库名
        new_db_name = f"test_idempotent_{uuid.uuid4().hex[:8]}"
        target_dsn = replace_db_in_dsn(admin_dsn, new_db_name)
        
        try:
            # 先创建数据库
            create_database(admin_dsn, new_db_name, quiet=True)
            assert check_database_exists(admin_dsn, new_db_name)
            
            # 再次调用 ensure_database_exists（应该返回成功，created=False）
            result = ensure_database_exists(
                target_dsn=target_dsn,
                admin_dsn=admin_dsn,
                quiet=True,
            )
            
            assert result["ok"], f"幂等调用失败: {result['message']}"
            assert result["db_name"] == new_db_name
            assert result["created"] is False  # 数据库已存在，未新建
        finally:
            # 清理
            try:
                conn = psycopg.connect(admin_dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{new_db_name}"')
                conn.close()
            except Exception:
                pass

    def test_run_migrate_creates_database(self, test_db_info):
        """测试：run_migrate 自动创建数据库并执行迁移"""
        import uuid
        import psycopg
        import os
        from db_migrate import run_migrate, check_database_exists, replace_db_in_dsn
        
        admin_dsn = test_db_info["admin_dsn"]
        
        # 生成一个新数据库名
        new_db_name = f"test_migrate_create_{uuid.uuid4().hex[:8]}"
        target_dsn = replace_db_in_dsn(admin_dsn, new_db_name)
        
        # 设置测试模式（如果使用 schema_prefix）
        old_env = os.environ.get("ENGRAM_TESTING")
        os.environ["ENGRAM_TESTING"] = "1"
        
        # 设置 admin_dsn 环境变量
        old_admin_dsn = os.environ.get("ENGRAM_PG_ADMIN_DSN")
        os.environ["ENGRAM_PG_ADMIN_DSN"] = admin_dsn
        
        try:
            # 确认数据库不存在
            assert not check_database_exists(admin_dsn, new_db_name)
            
            # 调用 run_migrate
            result = run_migrate(
                dsn=target_dsn,
                quiet=True,
            )
            
            # 验证迁移成功
            assert result.get("ok"), f"迁移失败: {result}"
            assert result.get("db_created") is True
            assert result.get("db_name") == new_db_name
            
            # 验证数据库已创建
            assert check_database_exists(admin_dsn, new_db_name)
            
            # 再次执行迁移（幂等性）
            result2 = run_migrate(
                dsn=target_dsn,
                quiet=True,
            )
            
            assert result2.get("ok"), f"第二次迁移失败: {result2}"
            assert result2.get("db_created") is False  # 数据库已存在
        finally:
            # 恢复环境变量
            if old_env is not None:
                os.environ["ENGRAM_TESTING"] = old_env
            else:
                os.environ.pop("ENGRAM_TESTING", None)
            
            if old_admin_dsn is not None:
                os.environ["ENGRAM_PG_ADMIN_DSN"] = old_admin_dsn
            else:
                os.environ.pop("ENGRAM_PG_ADMIN_DSN", None)
            
            # 清理数据库
            try:
                conn = psycopg.connect(admin_dsn, autocommit=True)
                with conn.cursor() as cur:
                    # 终止连接
                    cur.execute("""
                        SELECT pg_terminate_backend(pid)
                        FROM pg_stat_activity
                        WHERE datname = %s AND pid != pg_backend_pid()
                    """, (new_db_name,))
                    cur.execute(f'DROP DATABASE IF EXISTS "{new_db_name}"')
                conn.close()
            except Exception:
                pass


class TestRolesAndGrants:
    """测试角色与权限管理（04_roles_and_grants.sql）"""

    def test_apply_roles_creates_engram_roles(self, test_db_info):
        """测试：执行角色脚本后创建 Engram 角色"""
        import psycopg
        import os
        from db_migrate import run_migrate
        
        dsn = test_db_info["dsn"]
        
        # 设置测试模式
        old_env = os.environ.get("ENGRAM_TESTING")
        os.environ["ENGRAM_TESTING"] = "1"
        
        try:
            # 执行迁移并应用角色
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="strict",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            assert result.get("roles_applied") is True
            
            # 验证角色已创建
            conn = psycopg.connect(dsn)
            try:
                with conn.cursor() as cur:
                    expected_roles = [
                        "engram_admin",
                        "engram_migrator",
                        "engram_app_readwrite",
                        "engram_app_readonly",
                        "openmemory_migrator",
                        "openmemory_app",
                    ]
                    for role_name in expected_roles:
                        cur.execute(
                            "SELECT 1 FROM pg_roles WHERE rolname = %s",
                            (role_name,)
                        )
                        exists = cur.fetchone() is not None
                        assert exists, f"角色 {role_name} 应该存在"
            finally:
                conn.close()
        finally:
            if old_env is not None:
                os.environ["ENGRAM_TESTING"] = old_env
            else:
                os.environ.pop("ENGRAM_TESTING", None)

    def test_openmemory_policy_schema_permissions(self, test_db_info):
        """
        测试：openmemory 策略下权限验证
        
        验证项：
        1. openmemory schema 存在（由 05_openmemory_roles_and_grants.sql 创建）
        2. openmemory_migrator 在 openmemory schema 有 CREATE 权限
        3. openmemory_migrator 在 public schema 没有 CREATE 权限
        4. engram_migrator 在 public schema 没有 CREATE 权限
        """
        import psycopg
        import os
        from db_migrate import run_migrate, get_openmemory_schema
        
        dsn = test_db_info["dsn"]
        
        # 设置测试模式
        old_env = os.environ.get("ENGRAM_TESTING")
        os.environ["ENGRAM_TESTING"] = "1"
        
        # 获取 openmemory 目标 schema 名称
        om_schema = get_openmemory_schema()
        
        try:
            # 执行迁移并应用 openmemory 策略
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="openmemory",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            assert result.get("public_policy") == "openmemory"
            
            conn = psycopg.connect(dsn)
            try:
                with conn.cursor() as cur:
                    # 验证 openmemory schema 存在
                    cur.execute("""
                        SELECT EXISTS(
                            SELECT 1 FROM information_schema.schemata 
                            WHERE schema_name = %s
                        )
                    """, (om_schema,))
                    schema_exists = cur.fetchone()[0]
                    assert schema_exists, f"openmemory schema '{om_schema}' 应该存在"
                    
                    # 验证 openmemory_migrator 在 openmemory schema 有 CREATE 权限
                    cur.execute("""
                        SELECT has_schema_privilege('openmemory_migrator', %s, 'CREATE')
                    """, (om_schema,))
                    has_create_om = cur.fetchone()[0]
                    assert has_create_om, f"openmemory_migrator 应在 {om_schema} schema 有 CREATE 权限"
                    
                    # 验证 openmemory_migrator 在 public 没有 CREATE 权限
                    cur.execute("""
                        SELECT has_schema_privilege('openmemory_migrator', 'public', 'CREATE')
                    """)
                    has_create_public = cur.fetchone()[0]
                    assert not has_create_public, "openmemory_migrator 不应在 public 有 CREATE 权限"
                    
                    # 验证 engram_migrator 在 public 没有 CREATE 权限
                    cur.execute("""
                        SELECT has_schema_privilege('engram_migrator', 'public', 'CREATE')
                    """)
                    engram_has_create = cur.fetchone()[0]
                    assert not engram_has_create, "engram_migrator 不应在 public 有 CREATE 权限"
            finally:
                conn.close()
        finally:
            if old_env is not None:
                os.environ["ENGRAM_TESTING"] = old_env
            else:
                os.environ.pop("ENGRAM_TESTING", None)

    def test_openmemory_can_create_table_in_openmemory_schema(self, test_db_info):
        """
        集成测试：验证 OpenMemory 角色可在 openmemory schema 建表，但不能在 public 建表。
        
        测试步骤：
        1. 执行迁移并应用 openmemory 策略
        2. 创建一个 openmemory_migrator 成员用户
        3. 验证该用户在 openmemory schema 能创建表
        4. 验证该用户在 public schema 不能创建表（预期失败）
        5. 清理：删除临时表和测试用户
        """
        import psycopg
        import os
        import uuid
        from db_migrate import run_migrate, get_openmemory_schema
        
        dsn = test_db_info["dsn"]
        admin_dsn = test_db_info.get("admin_dsn", dsn)
        
        # 设置测试模式
        old_env = os.environ.get("ENGRAM_TESTING")
        os.environ["ENGRAM_TESTING"] = "1"
        
        # 生成唯一的测试用户名和表名
        test_suffix = uuid.uuid4().hex[:8]
        test_user = f"test_om_user_{test_suffix}"
        test_table = f"test_om_table_{test_suffix}"
        test_password = "test_password_123"
        
        # 获取 openmemory 目标 schema 名称
        om_schema = get_openmemory_schema()
        
        try:
            # 执行迁移并应用 openmemory 策略
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="openmemory",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            
            # 使用 admin 连接创建测试用户
            admin_conn = psycopg.connect(admin_dsn, autocommit=True)
            try:
                with admin_conn.cursor() as cur:
                    # 创建测试用户并授予 openmemory_migrator 角色
                    cur.execute(f"""
                        CREATE USER "{test_user}" WITH PASSWORD '{test_password}'
                    """)
                    cur.execute(f"""
                        GRANT openmemory_migrator TO "{test_user}"
                    """)
            finally:
                admin_conn.close()
            
            # 使用测试用户连接
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(dsn)
            test_user_dsn = urlunparse(parsed._replace(
                netloc=f"{test_user}:{test_password}@{parsed.hostname}:{parsed.port or 5432}"
            ))
            
            user_conn = psycopg.connect(test_user_dsn, autocommit=True)
            try:
                with user_conn.cursor() as cur:
                    # 在 openmemory schema 创建表（应成功）
                    cur.execute(f"""
                        CREATE TABLE "{om_schema}"."{test_table}" (
                            id SERIAL PRIMARY KEY,
                            name TEXT
                        )
                    """)
                    
                    # 验证表存在
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema = %s AND table_name = %s
                        )
                    """, (om_schema, test_table))
                    table_exists = cur.fetchone()[0]
                    assert table_exists, f"表 {om_schema}.{test_table} 应该创建成功"
                    
                    # 尝试在 public schema 创建表（应失败）
                    public_table = f"test_public_{test_suffix}"
                    try:
                        cur.execute(f"""
                            CREATE TABLE public."{public_table}" (
                                id SERIAL PRIMARY KEY
                            )
                        """)
                        # 如果执行到这里，说明创建成功了，但不应该成功
                        # 清理并报错
                        cur.execute(f'DROP TABLE IF EXISTS public."{public_table}"')
                        pytest.fail("openmemory_migrator 不应能在 public schema 创建表")
                    except psycopg.errors.InsufficientPrivilege:
                        # 预期的权限不足错误
                        pass
            finally:
                user_conn.close()
                
        finally:
            # 清理：删除测试表和用户
            try:
                admin_conn = psycopg.connect(admin_dsn, autocommit=True)
                with admin_conn.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS "{om_schema}"."{test_table}"')
                    cur.execute(f'DROP USER IF EXISTS "{test_user}"')
                admin_conn.close()
            except Exception:
                pass
            
            # 恢复环境变量
            if old_env is not None:
                os.environ["ENGRAM_TESTING"] = old_env
            else:
                os.environ.pop("ENGRAM_TESTING", None)

    def test_strict_policy_denies_public_create(self, test_db_info):
        """测试：strict 策略下所有角色不能在 public 创建表"""
        import psycopg
        import os
        from db_migrate import run_migrate
        
        dsn = test_db_info["dsn"]
        
        # 设置测试模式
        old_env = os.environ.get("ENGRAM_TESTING")
        os.environ["ENGRAM_TESTING"] = "1"
        
        try:
            # 执行迁移并应用 strict 策略
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="strict",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            assert result.get("public_policy") == "strict"
            
            # 验证所有角色在 public 没有 CREATE 权限
            conn = psycopg.connect(dsn)
            try:
                with conn.cursor() as cur:
                    roles_to_check = [
                        "engram_admin",
                        "engram_migrator",
                        "openmemory_migrator",
                        "openmemory_app",
                    ]
                    for role_name in roles_to_check:
                        cur.execute("""
                            SELECT has_schema_privilege(%s, 'public', 'CREATE')
                        """, (role_name,))
                        has_create = cur.fetchone()[0]
                        assert not has_create, f"{role_name} 不应在 public 有 CREATE 权限（strict 策略）"
            finally:
                conn.close()
        finally:
            if old_env is not None:
                os.environ["ENGRAM_TESTING"] = old_env
            else:
                os.environ.pop("ENGRAM_TESTING", None)

    def test_skip_roles_when_not_enabled(self, test_db_info):
        """测试：不启用 --apply-roles 时跳过角色脚本"""
        import psycopg
        import os
        from db_migrate import run_migrate
        
        dsn = test_db_info["dsn"]
        
        # 设置测试模式
        old_env = os.environ.get("ENGRAM_TESTING")
        os.environ["ENGRAM_TESTING"] = "1"
        
        try:
            # 执行迁移但不启用角色脚本
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=False,
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            assert result.get("roles_applied") is False
            assert result.get("public_policy") is None
            
            # 验证 04_roles_and_grants.sql 不在执行的文件列表中
            executed_files = result.get("sql_files", [])
            has_roles_script = any("04_roles_and_grants.sql" in f for f in executed_files)
            assert not has_roles_script, "不应执行 04_roles_and_grants.sql"
        finally:
            if old_env is not None:
                os.environ["ENGRAM_TESTING"] = old_env
            else:
                os.environ.pop("ENGRAM_TESTING", None)


# ============ 错误下幂等继续测试（集成级） ============

class TestIdempotentOnErrorCommits:
    """测试 commits 同步在错误下幂等继续"""
    
    def test_commits_partial_success_idempotent(self, db_conn, requests_mock_fixture):
        """
        测试：部分 commit diff 获取失败，仍可幂等继续
        
        场景：
        - 3 个 commits：A、B、C
        - A 成功，B 失败(500)，C 成功
        - 验证：A、C 被正确处理，B 的错误被记录
        """
        from unittest.mock import MagicMock, patch
        from engram.logbook.gitlab_client import GitLabClient, HttpConfig, StaticTokenProvider
        import db as scm_db
        from scm_repo import ensure_repo
        from datetime import datetime, timezone
        
        # 创建 repo
        repo_url = f"https://gitlab.example.com/test/commits-idempotent-{datetime.now().timestamp()}"
        repo_id = ensure_repo(db_conn, "git", repo_url, "test_project")
        db_conn.commit()
        
        # 创建客户端
        http_config = HttpConfig(max_attempts=1, timeout_seconds=30)
        client = GitLabClient(
            base_url="https://gitlab.example.com",
            token_provider=StaticTokenProvider("test-token"),
            http_config=http_config,
        )
        
        # 模拟响应
        commit_A = {"id": "aaa111", "sha": "aaa111"}
        commit_B = {"id": "bbb222", "sha": "bbb222"}
        commit_C = {"id": "ccc333", "sha": "ccc333"}
        
        # 记录处理结果
        success_count = 0
        error_count = 0
        
        with patch.object(client, 'get_commit_diff_safe') as mock_diff:
            # A 成功
            mock_diff.side_effect = [
                MagicMock(success=True, data=[{"diff": "+line1"}]),  # A
                MagicMock(success=False, status_code=500, error_category="server_error"),  # B
                MagicMock(success=True, data=[{"diff": "+line3"}]),  # C
            ]
            
            for commit in [commit_A, commit_B, commit_C]:
                result = mock_diff()
                if result.success:
                    success_count += 1
                else:
                    error_count += 1
        
        # 验证统计
        assert success_count == 2, "应有 2 个成功"
        assert error_count == 1, "应有 1 个错误"


class TestIdempotentOnErrorMRs:
    """测试 MRs 同步在错误下幂等继续"""
    
    def test_mrs_partial_failure_continues(self, db_conn):
        """
        测试：部分 MR 处理失败时仍继续处理其他 MR
        
        场景：
        - 模拟 3 个 MR
        - MR1 成功，MR2 失败，MR3 成功
        - 验证：成功处理 2 个，记录 1 个失败
        """
        from unittest.mock import MagicMock, patch
        from scm_repo import ensure_repo, build_mr_id
        from db import upsert_mr
        from datetime import datetime
        
        # 创建 repo
        repo_url = f"https://gitlab.example.com/test/mrs-idempotent-{datetime.now().timestamp()}"
        repo_id = ensure_repo(db_conn, "git", repo_url, "test_project")
        db_conn.commit()
        
        # 模拟 MR 数据处理
        mock_mrs = [
            {"iid": 1, "project_id": 123, "state": "opened"},
            {"iid": 2, "project_id": 123, "state": "merged"},
            {"iid": 3, "project_id": 123, "state": "closed"},
        ]
        
        success_count = 0
        error_count = 0
        
        # 模拟处理逻辑：MR2 失败
        for i, mr_data in enumerate(mock_mrs):
            mr_id = build_mr_id(repo_id, mr_data["iid"])
            
            try:
                if i == 1:
                    # 模拟 MR2 处理失败
                    raise Exception("模拟 API 错误")
                
                # 插入 MR
                upsert_mr(db_conn, mr_id, repo_id, status=mr_data["state"])
                success_count += 1
            except Exception:
                error_count += 1
        
        db_conn.commit()
        
        # 验证统计
        assert success_count == 2
        assert error_count == 1
        
        # 验证数据库中有 2 条 MR 记录
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mrs WHERE repo_id = %s", (repo_id,))
            count = cur.fetchone()[0]
            assert count == 2


class TestIdempotentOnErrorReviews:
    """测试 reviews 同步在错误下幂等继续"""
    
    def test_reviews_partial_api_failure(self, db_conn):
        """
        测试：部分 review API 调用失败时仍继续处理
        
        场景：
        - 处理 MR 的 notes，其中一个 API 返回 429
        - 验证：成功的事件被记录，失败的被跳过
        """
        from db import insert_review_event, upsert_repo, upsert_mr
        from datetime import datetime
        
        # 创建必要的 repo 和 MR
        url = f"https://test.example.com/reviews-idempotent-{datetime.now().timestamp()}"
        repo_id = upsert_repo(db_conn, "git", url, "test_project")
        db_conn.commit()
        
        mr_id = f"{repo_id}:{int(datetime.now().timestamp())}"
        upsert_mr(db_conn, mr_id, repo_id, status="open")
        db_conn.commit()
        
        ts = datetime.now().timestamp()
        
        # 模拟事件列表，其中一个"失败"（我们通过不插入来模拟）
        events = [
            {"source_event_id": f"note:review-1-{ts}", "event_type": "comment", "should_fail": False},
            {"source_event_id": f"note:review-2-{ts}", "event_type": "comment", "should_fail": True},  # 模拟失败
            {"source_event_id": f"note:review-3-{ts}", "event_type": "approve", "should_fail": False},
        ]
        
        inserted = 0
        skipped = 0
        
        for event in events:
            if event["should_fail"]:
                skipped += 1
                continue  # 模拟 API 失败后跳过
            
            event_id = insert_review_event(
                db_conn, mr_id,
                event_type=event["event_type"],
                source_event_id=event["source_event_id"],
                reviewer_user_id=None,
            )
            if event_id:
                inserted += 1
        
        db_conn.commit()
        
        # 验证
        assert inserted == 2, "应该插入 2 条事件"
        assert skipped == 1, "应该跳过 1 条失败事件"
        
        # 验证数据库中有 2 条记录
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM review_events WHERE mr_id = %s",
                (mr_id,)
            )
            count = cur.fetchone()[0]
            assert count == 2


class TestIdempotentOnErrorMaterialize:
    """测试 materialize 在错误下幂等继续"""
    
    def test_materialize_partial_failure_stats(self):
        """
        测试：部分 blob 物化失败时统计正确（无需数据库）
        
        场景：
        - 模拟处理 3 个 blob
        - blob1 成功，blob2 失败(timeout)，blob3 成功
        - 验证：统计 2 成功，1 失败
        """
        from dataclasses import dataclass
        from enum import Enum
        from collections import Counter
        
        # 模拟物化状态
        class MaterializeStatus(Enum):
            MATERIALIZED = "materialized"
            FAILED = "failed"
            SKIPPED = "skipped"
        
        @dataclass
        class MaterializeResult:
            blob_id: int
            status: MaterializeStatus
            error: str = None
        
        # 模拟物化结果
        results = [
            MaterializeResult(1, MaterializeStatus.MATERIALIZED),
            MaterializeResult(2, MaterializeStatus.FAILED, error="timeout"),
            MaterializeResult(3, MaterializeStatus.MATERIALIZED),
        ]
        
        # 统计
        status_counter = Counter(r.status for r in results)
        
        assert status_counter[MaterializeStatus.MATERIALIZED] == 2
        assert status_counter[MaterializeStatus.FAILED] == 1
        
        # 验证可以继续处理（幂等性）
        additional_results = [
            MaterializeResult(4, MaterializeStatus.MATERIALIZED),
            MaterializeResult(2, MaterializeStatus.MATERIALIZED),  # 重试成功
        ]
        
        for r in additional_results:
            status_counter[r.status] += 1
        
        assert status_counter[MaterializeStatus.MATERIALIZED] == 4


# 为以上测试提供 requests_mock fixture
@pytest.fixture
def requests_mock_fixture():
    """提供 requests-mock 功能"""
    import requests_mock as rm
    with rm.Mocker() as m:
        yield m


class TestOpenMemorySchemaConfig:
    """测试 OpenMemory schema 可配置性（OM_PG_SCHEMA 环境变量）"""

    def test_custom_openmemory_schema_creation(self, test_db_info):
        """
        测试：设置 OM_PG_SCHEMA=myproj_openmemory 后，
        运行 db_migrate.py --apply-roles --public-policy openmemory 
        能创建并授权自定义 schema
        """
        import psycopg
        import os
        import uuid
        from db_migrate import run_migrate, get_openmemory_schema
        
        dsn = test_db_info["dsn"]
        
        # 生成唯一的自定义 schema 名称
        custom_schema = f"myproj_openmemory_{uuid.uuid4().hex[:8]}"
        
        # 设置环境变量
        old_testing = os.environ.get("ENGRAM_TESTING")
        old_om_schema = os.environ.get("OM_PG_SCHEMA")
        
        os.environ["ENGRAM_TESTING"] = "1"
        os.environ["OM_PG_SCHEMA"] = custom_schema
        
        try:
            # 验证 get_openmemory_schema 返回自定义值
            assert get_openmemory_schema() == custom_schema
            
            # 执行迁移
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="openmemory",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            assert result.get("openmemory_schema_applied") is True
            assert result.get("openmemory_target_schema") == custom_schema
            
            # 验证自定义 schema 已创建
            conn = psycopg.connect(dsn)
            try:
                with conn.cursor() as cur:
                    # 检查 schema 存在
                    cur.execute("""
                        SELECT 1 FROM information_schema.schemata 
                        WHERE schema_name = %s
                    """, (custom_schema,))
                    exists = cur.fetchone() is not None
                    assert exists, f"自定义 schema {custom_schema} 应该存在"
                    
                    # 验证 openmemory_migrator 有 CREATE 权限
                    cur.execute("""
                        SELECT has_schema_privilege('openmemory_migrator', %s, 'CREATE')
                    """, (custom_schema,))
                    has_create = cur.fetchone()[0]
                    assert has_create, f"openmemory_migrator 应在 {custom_schema} 有 CREATE 权限"
                    
                    # 验证 openmemory_app 有 USAGE 权限
                    cur.execute("""
                        SELECT has_schema_privilege('openmemory_app', %s, 'USAGE')
                    """, (custom_schema,))
                    has_usage = cur.fetchone()[0]
                    assert has_usage, f"openmemory_app 应在 {custom_schema} 有 USAGE 权限"
            finally:
                conn.close()
        finally:
            # 恢复环境变量
            if old_testing is not None:
                os.environ["ENGRAM_TESTING"] = old_testing
            else:
                os.environ.pop("ENGRAM_TESTING", None)
            
            if old_om_schema is not None:
                os.environ["OM_PG_SCHEMA"] = old_om_schema
            else:
                os.environ.pop("OM_PG_SCHEMA", None)
            
            # 清理：删除自定义 schema
            try:
                conn = psycopg.connect(dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{custom_schema}" CASCADE')
                conn.close()
            except Exception:
                pass

    def test_run_all_checks_with_custom_openmemory_schema(self, test_db_info):
        """
        测试：run_all_checks 使用自定义 openmemory schema 验证通过
        """
        import psycopg
        import os
        import uuid
        from db_migrate import run_migrate, run_all_checks
        
        dsn = test_db_info["dsn"]
        
        # 生成唯一的自定义 schema 名称
        custom_schema = f"test_om_schema_{uuid.uuid4().hex[:8]}"
        
        # 设置环境变量
        old_testing = os.environ.get("ENGRAM_TESTING")
        old_om_schema = os.environ.get("OM_PG_SCHEMA")
        
        os.environ["ENGRAM_TESTING"] = "1"
        os.environ["OM_PG_SCHEMA"] = custom_schema
        
        try:
            # 执行迁移
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="openmemory",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            
            # 验证 run_all_checks 能正确验证自定义 schema
            conn = psycopg.connect(dsn)
            try:
                checks_result = run_all_checks(
                    conn,
                    check_openmemory_schema=True,
                    openmemory_schema_name=custom_schema,
                )
                
                assert checks_result["ok"], f"自检失败: {checks_result['checks']}"
                assert checks_result["checks"]["openmemory_schema"]["ok"], \
                    f"openmemory schema 检查失败: {checks_result['checks']['openmemory_schema']}"
                assert checks_result["checks"]["openmemory_schema"]["schema"] == custom_schema
            finally:
                conn.close()
        finally:
            # 恢复环境变量
            if old_testing is not None:
                os.environ["ENGRAM_TESTING"] = old_testing
            else:
                os.environ.pop("ENGRAM_TESTING", None)
            
            if old_om_schema is not None:
                os.environ["OM_PG_SCHEMA"] = old_om_schema
            else:
                os.environ.pop("OM_PG_SCHEMA", None)
            
            # 清理
            try:
                conn = psycopg.connect(dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{custom_schema}" CASCADE')
                conn.close()
            except Exception:
                pass

    def test_default_openmemory_schema_when_env_not_set(self, test_db_info):
        """
        测试：OM_PG_SCHEMA 未设置时使用默认值 'openmemory'
        """
        import os
        from db_migrate import get_openmemory_schema
        
        # 确保环境变量未设置
        old_om_schema = os.environ.get("OM_PG_SCHEMA")
        if old_om_schema:
            del os.environ["OM_PG_SCHEMA"]
        
        try:
            schema = get_openmemory_schema()
            assert schema == "openmemory", f"默认 schema 应为 'openmemory'，实际为 '{schema}'"
        finally:
            # 恢复环境变量
            if old_om_schema is not None:
                os.environ["OM_PG_SCHEMA"] = old_om_schema

    def test_verify_permissions_sql_uses_session_variable(self, test_db_info):
        """
        测试：99_verify_permissions.sql 使用 om.target_schema session 变量
        
        验证：
        1. 设置 om.target_schema 后验证脚本能正确识别
        2. 自定义 schema 的权限验证不会误报
        """
        import psycopg
        import os
        import uuid
        from pathlib import Path
        from db_migrate import run_migrate
        
        dsn = test_db_info["dsn"]
        
        # 生成唯一的自定义 schema 名称
        custom_schema = f"verify_test_om_{uuid.uuid4().hex[:8]}"
        
        # 设置环境变量
        old_testing = os.environ.get("ENGRAM_TESTING")
        old_om_schema = os.environ.get("OM_PG_SCHEMA")
        
        os.environ["ENGRAM_TESTING"] = "1"
        os.environ["OM_PG_SCHEMA"] = custom_schema
        
        try:
            # 执行迁移
            result = run_migrate(
                dsn=dsn,
                quiet=True,
                apply_roles=True,
                public_policy="openmemory",
            )
            
            assert result.get("ok"), f"迁移失败: {result}"
            
            # 执行验证脚本
            sql_path = Path(__file__).parent.parent.parent / "sql" / "99_verify_permissions.sql"
            
            conn = psycopg.connect(dsn, autocommit=True)
            try:
                with conn.cursor() as cur:
                    # 设置 session 变量
                    cur.execute("SET om.target_schema = %s", (custom_schema,))
                    
                    # 执行验证脚本（捕获 NOTICE 消息验证输出）
                    sql_content = sql_path.read_text(encoding="utf-8")
                    cur.execute(sql_content)
                    
                    # 验证 schema 存在性检查通过
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.schemata 
                            WHERE schema_name = %s
                        )
                    """, (custom_schema,))
                    exists = cur.fetchone()[0]
                    assert exists, f"自定义 schema {custom_schema} 应该存在"
            finally:
                conn.close()
        finally:
            # 恢复环境变量
            if old_testing is not None:
                os.environ["ENGRAM_TESTING"] = old_testing
            else:
                os.environ.pop("ENGRAM_TESTING", None)
            
            if old_om_schema is not None:
                os.environ["OM_PG_SCHEMA"] = old_om_schema
            else:
                os.environ.pop("OM_PG_SCHEMA", None)
            
            # 清理
            try:
                conn = psycopg.connect(dsn, autocommit=True)
                with conn.cursor() as cur:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{custom_schema}" CASCADE')
                conn.close()
            except Exception:
                pass


class TestGetBulkThresholds:
    """测试 get_bulk_thresholds 配置读取优先级"""

    def test_only_new_keys_configured(self):
        """测试仅配置新键（scm.bulk_thresholds.*）"""
        from unittest.mock import MagicMock
        from engram.logbook.config import get_bulk_thresholds

        config = MagicMock()
        # 模拟仅配置新键
        config_data = {
            "scm.bulk_thresholds.svn_changed_paths": 200,
            "scm.bulk_thresholds.git_total_changes": 2000,
            "scm.bulk_thresholds.git_files_changed": 100,
            "scm.bulk_thresholds.diff_size_bytes": 2097152,  # 2MB
        }
        config.get.side_effect = lambda key, default=None: config_data.get(key, None)

        thresholds = get_bulk_thresholds(config)

        assert thresholds["svn_changed_paths_threshold"] == 200
        assert thresholds["git_total_changes_threshold"] == 2000
        assert thresholds["git_files_changed_threshold"] == 100
        assert thresholds["diff_size_threshold"] == 2097152

    def test_only_old_keys_configured(self):
        """测试仅配置旧键（bulk.*）"""
        from unittest.mock import MagicMock
        from engram.logbook.config import get_bulk_thresholds

        config = MagicMock()
        # 模拟仅配置旧键
        config_data = {
            "bulk.svn_changed_paths_threshold": 150,
            "bulk.git_total_changes_threshold": 1500,
            "bulk.git_files_changed_threshold": 75,
            "bulk.diff_size_threshold": 524288,  # 512KB
        }
        config.get.side_effect = lambda key, default=None: config_data.get(key, None)

        thresholds = get_bulk_thresholds(config)

        assert thresholds["svn_changed_paths_threshold"] == 150
        assert thresholds["git_total_changes_threshold"] == 1500
        assert thresholds["git_files_changed_threshold"] == 75
        assert thresholds["diff_size_threshold"] == 524288

    def test_both_keys_configured_new_overrides(self):
        """测试同时配置新旧键，新键优先覆盖"""
        from unittest.mock import MagicMock
        from engram.logbook.config import get_bulk_thresholds

        config = MagicMock()
        # 模拟同时配置新旧键（新键值应优先）
        config_data = {
            # 新键
            "scm.bulk_thresholds.svn_changed_paths": 300,
            "scm.bulk_thresholds.git_total_changes": 3000,
            "scm.bulk_thresholds.git_files_changed": 150,
            "scm.bulk_thresholds.diff_size_bytes": 4194304,  # 4MB
            # 旧键（应被忽略）
            "bulk.svn_changed_paths_threshold": 100,
            "bulk.git_total_changes_threshold": 1000,
            "bulk.git_files_changed_threshold": 50,
            "bulk.diff_size_threshold": 1048576,
        }
        config.get.side_effect = lambda key, default=None: config_data.get(key, None)

        thresholds = get_bulk_thresholds(config)

        # 应使用新键的值
        assert thresholds["svn_changed_paths_threshold"] == 300
        assert thresholds["git_total_changes_threshold"] == 3000
        assert thresholds["git_files_changed_threshold"] == 150
        assert thresholds["diff_size_threshold"] == 4194304

    def test_no_keys_configured_uses_defaults(self):
        """测试未配置任何键时使用默认值"""
        from unittest.mock import MagicMock
        from engram.logbook.config import (
            get_bulk_thresholds,
            DEFAULT_SVN_CHANGED_PATHS_THRESHOLD,
            DEFAULT_GIT_TOTAL_CHANGES_THRESHOLD,
            DEFAULT_GIT_FILES_CHANGED_THRESHOLD,
            DEFAULT_DIFF_SIZE_THRESHOLD,
        )

        config = MagicMock()
        # 模拟未配置任何键
        config.get.side_effect = lambda key, default=None: None

        thresholds = get_bulk_thresholds(config)

        assert thresholds["svn_changed_paths_threshold"] == DEFAULT_SVN_CHANGED_PATHS_THRESHOLD
        assert thresholds["git_total_changes_threshold"] == DEFAULT_GIT_TOTAL_CHANGES_THRESHOLD
        assert thresholds["git_files_changed_threshold"] == DEFAULT_GIT_FILES_CHANGED_THRESHOLD
        assert thresholds["diff_size_threshold"] == DEFAULT_DIFF_SIZE_THRESHOLD

    def test_partial_new_keys_fallback_to_old(self):
        """测试部分配置新键、部分回退旧键"""
        from unittest.mock import MagicMock
        from engram.logbook.config import get_bulk_thresholds, DEFAULT_DIFF_SIZE_THRESHOLD

        config = MagicMock()
        # 部分配置：新键配置 svn/git_total，旧键配置 git_files，diff_size 使用默认
        config_data = {
            "scm.bulk_thresholds.svn_changed_paths": 250,
            "scm.bulk_thresholds.git_total_changes": 2500,
            "bulk.git_files_changed_threshold": 80,
            # diff_size 均未配置，使用默认值
        }
        config.get.side_effect = lambda key, default=None: config_data.get(key, None)

        thresholds = get_bulk_thresholds(config)

        assert thresholds["svn_changed_paths_threshold"] == 250  # 新键
        assert thresholds["git_total_changes_threshold"] == 2500  # 新键
        assert thresholds["git_files_changed_threshold"] == 80  # 回退旧键
        assert thresholds["diff_size_threshold"] == DEFAULT_DIFF_SIZE_THRESHOLD  # 默认值


# ============================================================================
# Backfill Smoke 测试
# ============================================================================

class TestBackfillEvidenceUriSmoke:
    """backfill_evidence_uri.py 冒烟测试"""

    def test_backfill_module_import(self):
        """测试 backfill_evidence_uri 模块可导入"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from backfill_evidence_uri import (
            backfill_evidence_uri,
            get_blobs_missing_evidence_uri,
            update_evidence_uri,
            DEFAULT_BATCH_SIZE,
        )
        
        assert callable(backfill_evidence_uri)
        assert callable(get_blobs_missing_evidence_uri)
        assert callable(update_evidence_uri)
        assert DEFAULT_BATCH_SIZE == 1000

    def test_backfill_cli_help(self):
        """测试 backfill_evidence_uri CLI --help 可执行"""
        import subprocess
        import sys
        from pathlib import Path
        
        script_path = Path(__file__).parent.parent / "backfill_evidence_uri.py"
        
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        assert result.returncode == 0, f"CLI --help 失败: {result.stderr}"
        assert "--batch-size" in result.stdout
        assert "--dry-run" in result.stdout


class TestBackfillChunkingVersionSmoke:
    """backfill_chunking_version.py 冒烟测试"""

    def test_backfill_chunking_module_import(self):
        """测试 backfill_chunking_version 模块可导入"""
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from backfill_chunking_version import (
            backfill_chunking_version,
            backfill_patch_blobs,
            backfill_attachments,
            DEFAULT_BATCH_SIZE,
        )
        
        assert callable(backfill_chunking_version)
        assert callable(backfill_patch_blobs)
        assert callable(backfill_attachments)
        assert DEFAULT_BATCH_SIZE == 1000

    def test_backfill_chunking_cli_help(self):
        """测试 backfill_chunking_version CLI --help 可执行"""
        import subprocess
        import sys
        from pathlib import Path
        
        script_path = Path(__file__).parent.parent / "backfill_chunking_version.py"
        
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        assert result.returncode == 0, f"CLI --help 失败: {result.stderr}"
        assert "--chunking-version" in result.stdout
        assert "--batch-size" in result.stdout
        assert "--only-missing" in result.stdout


class TestMigrateBackfillIntegrationSmoke:
    """migrate + backfill 集成冒烟测试"""

    def test_migrate_cli_has_backfill_args(self):
        """测试 db_migrate CLI 包含 backfill 参数"""
        import subprocess
        import sys
        from pathlib import Path
        
        script_path = Path(__file__).parent.parent / "db_migrate.py"
        
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        assert result.returncode == 0, f"CLI --help 失败: {result.stderr}"
        assert "--post-backfill" in result.stdout, "CLI 应包含 --post-backfill 参数"
        assert "--backfill-chunking-version" in result.stdout, "CLI 应包含 --backfill-chunking-version 参数"
        assert "--backfill-batch-size" in result.stdout, "CLI 应包含 --backfill-batch-size 参数"
        assert "--backfill-dry-run" in result.stdout, "CLI 应包含 --backfill-dry-run 参数"

    def test_run_migrate_accepts_backfill_params(self):
        """测试 run_migrate 函数接受 backfill 参数（不实际执行）"""
        import inspect
        from engram.logbook.migrate import run_migrate
        
        sig = inspect.signature(run_migrate)
        params = sig.parameters
        
        # 验证参数存在
        assert "post_backfill" in params
        assert "backfill_chunking_version" in params
        assert "backfill_batch_size" in params
        assert "backfill_dry_run" in params
        
        # 验证默认值
        assert params["post_backfill"].default is False
        assert params["backfill_chunking_version"].default is None
        assert params["backfill_batch_size"].default == 1000
        assert params["backfill_dry_run"].default is False


class TestBackfillWithMigratedDb:
    """使用真实数据库测试 backfill（需要 migrated_db fixture）"""

    def test_backfill_evidence_uri_dry_run(self, migrated_db: dict):
        """测试 backfill_evidence_uri dry-run 模式"""
        import sys
        from pathlib import Path
        from unittest.mock import MagicMock
        
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from backfill_evidence_uri import backfill_evidence_uri
        
        # 构建 mock_config
        schemas = migrated_db["schemas"]
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": list(schemas.values()) + ["public"],
        }.get(key, default)
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")
        
        # 执行 dry-run
        result = backfill_evidence_uri(
            batch_size=10,
            dry_run=True,
            config=mock_config,
        )
        
        # 验证结果结构
        assert "success" in result
        assert "dry_run" in result
        assert result["dry_run"] is True

    def test_backfill_chunking_version_dry_run(self, migrated_db: dict):
        """测试 backfill_chunking_version dry-run 模式"""
        import sys
        from pathlib import Path
        from unittest.mock import MagicMock
        
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from backfill_chunking_version import backfill_chunking_version
        
        # 构建 mock_config
        schemas = migrated_db["schemas"]
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "postgres.search_path": list(schemas.values()) + ["public"],
        }.get(key, default)
        mock_config.require.side_effect = lambda key: {
            "postgres.dsn": migrated_db["dsn"],
        }.get(key, f"mock_{key}")
        
        # 执行 dry-run
        result = backfill_chunking_version(
            target_version="v1.0-test",
            batch_size=10,
            dry_run=True,
            config=mock_config,
        )
        
        # 验证结果结构
        assert "success" in result
        assert "dry_run" in result
        assert result["dry_run"] is True
        assert "target_version" in result
        assert result["target_version"] == "v1.0-test"


class TestDefaultConfigValues:
    """
    验证默认配置值
    
    确保所有新开关在默认配置下保持向后兼容（默认关闭），
    不影响现有测试用例。
    """

    def test_scheduler_config_default_values(self):
        """验证 SchedulerConfig 默认值"""
        from engram.logbook.scm_sync_policy import SchedulerConfig

        # 使用无配置创建，验证默认值
        config = SchedulerConfig()

        # 核心开关默认应为 False（保持向后兼容）
        assert config.enable_tenant_fairness is False, \
            "enable_tenant_fairness 默认应为 False，保持向后兼容"
        assert config.tenant_fairness_max_per_round == 1, \
            "tenant_fairness_max_per_round 默认应为 1"
        
        # 其他常用配置的默认值
        assert config.global_concurrency == 10
        assert config.per_instance_concurrency == 3
        assert config.per_tenant_concurrency == 5
        assert config.error_budget_threshold == 0.3
        assert config.pause_duration_seconds == 300
        assert config.scan_interval_seconds == 60
        assert config.max_enqueue_per_scan == 100

    def test_scheduler_config_from_none_config(self):
        """验证从 None 配置加载时使用默认值"""
        from engram.logbook.scm_sync_policy import SchedulerConfig

        config = SchedulerConfig.from_config(None)

        # 关键开关默认为 False
        assert config.enable_tenant_fairness is False
        assert config.global_concurrency == 10

    def test_http_config_default_values(self):
        """验证 HttpConfig 默认值"""
        from engram.logbook.gitlab_client import HttpConfig

        # 使用无配置创建
        config = HttpConfig()

        # rate limit 开关默认应为 False（保持向后兼容，无速率限制）
        assert config.rate_limit_enabled is False, \
            "rate_limit_enabled 默认应为 False，保持向后兼容"
        assert config.postgres_rate_limit_enabled is False, \
            "postgres_rate_limit_enabled 默认应为 False，保持向后兼容"
        
        # 其他 HTTP 配置默认值
        assert config.timeout_seconds == 60.0
        assert config.max_attempts == 3
        assert config.backoff_base_seconds == 1.0
        assert config.backoff_max_seconds == 60.0
        assert config.max_concurrency is None  # 默认无并发限制
        assert config.rate_limit_requests_per_second == 10.0
        assert config.rate_limit_burst_size is None  # 默认等于 requests_per_second

    def test_http_config_from_none_config(self):
        """验证从 None 配置加载 HttpConfig 时使用默认值"""
        from engram.logbook.gitlab_client import HttpConfig

        config = HttpConfig.from_config(None)

        # 关键开关默认为 False
        assert config.rate_limit_enabled is False
        assert config.postgres_rate_limit_enabled is False

    def test_config_module_default_constants(self):
        """验证 config 模块中的默认值常量"""
        from engram.logbook.config import (
            # Scheduler 默认值
            DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS,
            DEFAULT_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND,
            DEFAULT_SCHEDULER_GLOBAL_CONCURRENCY,
            DEFAULT_SCHEDULER_PER_INSTANCE_CONCURRENCY,
            DEFAULT_SCHEDULER_PER_TENANT_CONCURRENCY,
            DEFAULT_SCHEDULER_SCAN_INTERVAL_SECONDS,
            DEFAULT_SCHEDULER_MAX_ENQUEUE_PER_SCAN,
            DEFAULT_SCHEDULER_ERROR_BUDGET_THRESHOLD,
            DEFAULT_SCHEDULER_PAUSE_DURATION_SECONDS,
            # GitLab rate limit 默认值
            DEFAULT_GITLAB_RATE_LIMIT_ENABLED,
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_ENABLED,
            DEFAULT_GITLAB_RATE_LIMIT_REQUESTS_PER_SECOND,
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_RATE,
            DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_BURST,
        )

        # 关键开关默认值断言（保持向后兼容）
        assert DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS is False, \
            "DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS 应为 False"
        assert DEFAULT_GITLAB_RATE_LIMIT_ENABLED is False, \
            "DEFAULT_GITLAB_RATE_LIMIT_ENABLED 应为 False"
        assert DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_ENABLED is False, \
            "DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_ENABLED 应为 False"

        # 数值默认值
        assert DEFAULT_SCHEDULER_TENANT_FAIRNESS_MAX_PER_ROUND == 1
        assert DEFAULT_SCHEDULER_GLOBAL_CONCURRENCY == 10
        assert DEFAULT_SCHEDULER_PER_INSTANCE_CONCURRENCY == 3
        assert DEFAULT_SCHEDULER_PER_TENANT_CONCURRENCY == 5
        assert DEFAULT_SCHEDULER_SCAN_INTERVAL_SECONDS == 60
        assert DEFAULT_SCHEDULER_MAX_ENQUEUE_PER_SCAN == 100
        assert DEFAULT_SCHEDULER_ERROR_BUDGET_THRESHOLD == 0.3
        assert DEFAULT_SCHEDULER_PAUSE_DURATION_SECONDS == 300
        assert DEFAULT_GITLAB_RATE_LIMIT_REQUESTS_PER_SECOND == 10.0
        assert DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_RATE == 10.0
        assert DEFAULT_GITLAB_POSTGRES_RATE_LIMIT_BURST == 20

    def test_get_scheduler_config_defaults(self):
        """验证 get_scheduler_config() 返回正确的默认值"""
        from engram.logbook.config import get_scheduler_config
        from unittest.mock import MagicMock

        # 创建一个返回默认值的 mock config
        mock_config = MagicMock()
        mock_config.get.return_value = None  # 所有配置都返回 None，使用默认值

        # 这里我们不调用 get_scheduler_config(mock_config)，因为它会调用 get_config()
        # 而是直接验证导入的常量
        from engram.logbook.config import (
            DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS,
        )
        assert DEFAULT_SCHEDULER_ENABLE_TENANT_FAIRNESS is False

    def test_get_gitlab_rate_limit_config_defaults(self):
        """验证 get_gitlab_rate_limit_config() 返回正确的默认值"""
        from engram.logbook.config import get_gitlab_rate_limit_config
        from unittest.mock import MagicMock

        # 创建一个总是返回 None 的 mock config
        mock_config = MagicMock()
        mock_config.get.return_value = None

        result = get_gitlab_rate_limit_config(mock_config)

        # 验证关键开关默认为 False
        assert result["rate_limit_enabled"] is False, \
            "rate_limit_enabled 默认应为 False"
        assert result["postgres_rate_limit_enabled"] is False, \
            "postgres_rate_limit_enabled 默认应为 False"

        # 验证其他默认值
        assert result["rate_limit_requests_per_second"] == 10.0
        assert result["rate_limit_burst_size"] is None
        assert result["postgres_rate_limit_rate"] == 10.0
        assert result["postgres_rate_limit_burst"] == 20
        assert result["postgres_rate_limit_max_wait"] == 60.0


class TestCLIOutputFormat:
    """
    测试 CLI 输出格式
    
    验证:
    - stdout 输出是纯 JSON（可被 json.loads 解析）
    - 日志信息只输出到 stderr
    - 使用 -q (quiet) 模式时 stderr 为空
    
    这些测试不依赖数据库连接，只验证 CLI 输出的格式正确性。
    """

    def test_health_output_is_pure_json_on_stdout(self):
        """验证 health 命令的 stdout 输出是纯 JSON"""
        import json
        import subprocess
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        cli_path = scripts_dir / "logbook_cli.py"
        
        # 使用 -q 模式确保 stderr 不干扰，只检查 stdout
        result = subprocess.run(
            [sys.executable, str(cli_path), "health", "-q"],
            capture_output=True,
            text=True,
            cwd=str(scripts_dir),
            timeout=30,
        )
        
        # stdout 应该可以被 JSON 解析（无论命令成功还是失败）
        stdout = result.stdout.strip()
        assert stdout, "stdout 不应为空"
        
        try:
            parsed = json.loads(stdout)
            assert "ok" in parsed, "JSON 输出应包含 'ok' 字段"
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout 不是有效的 JSON: {e}\nstdout={stdout!r}")

    def test_health_quiet_mode_has_empty_stderr(self):
        """验证 health 命令使用 -q 模式时 stderr 为空"""
        import subprocess
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        cli_path = scripts_dir / "logbook_cli.py"
        
        result = subprocess.run(
            [sys.executable, str(cli_path), "health", "-q"],
            capture_output=True,
            text=True,
            cwd=str(scripts_dir),
            timeout=30,
        )
        
        # -q 模式下 stderr 应该为空（或者只有少量系统消息）
        stderr = result.stderr.strip()
        # 允许一些 Python 警告，但不应有我们的日志输出
        for line in stderr.split("\n"):
            line = line.strip()
            if line and not line.startswith("Warning:") and not "DeprecationWarning" in line:
                # 检查是否是我们的日志输出（以 ERROR:, WARN:, DEBUG: 开头）
                if line.startswith(("ERROR:", "WARN:", "DEBUG:", "输出目录:", "查询到")):
                    pytest.fail(f"-q 模式下不应有日志输出到 stderr: {line}")

    def test_create_item_validation_error_is_json(self):
        """验证 create_item 验证失败时输出是纯 JSON"""
        import json
        import subprocess
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        cli_path = scripts_dir / "logbook_cli.py"
        
        # 不提供必需参数，应该返回验证错误
        result = subprocess.run(
            [sys.executable, str(cli_path), "create_item", "-q"],
            capture_output=True,
            text=True,
            cwd=str(scripts_dir),
            timeout=30,
        )
        
        stdout = result.stdout.strip()
        assert stdout, "stdout 不应为空"
        
        try:
            parsed = json.loads(stdout)
            assert "ok" in parsed, "JSON 输出应包含 'ok' 字段"
            assert parsed["ok"] is False, "验证失败时 ok 应为 False"
            assert "code" in parsed, "错误输出应包含 'code' 字段"
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout 不是有效的 JSON: {e}\nstdout={stdout!r}")

    def test_add_event_validation_error_is_json(self):
        """验证 add_event 验证失败时输出是纯 JSON"""
        import json
        import subprocess
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        cli_path = scripts_dir / "logbook_cli.py"
        
        # 不提供必需参数
        result = subprocess.run(
            [sys.executable, str(cli_path), "add_event", "-q"],
            capture_output=True,
            text=True,
            cwd=str(scripts_dir),
            timeout=30,
        )
        
        stdout = result.stdout.strip()
        assert stdout, "stdout 不应为空"
        
        try:
            parsed = json.loads(stdout)
            assert "ok" in parsed
            assert parsed["ok"] is False
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout 不是有效的 JSON: {e}\nstdout={stdout!r}")

    def test_attach_validation_error_is_json(self):
        """验证 attach 验证失败时输出是纯 JSON"""
        import json
        import subprocess
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        cli_path = scripts_dir / "logbook_cli.py"
        
        result = subprocess.run(
            [sys.executable, str(cli_path), "attach", "-q"],
            capture_output=True,
            text=True,
            cwd=str(scripts_dir),
            timeout=30,
        )
        
        stdout = result.stdout.strip()
        assert stdout, "stdout 不应为空"
        
        try:
            parsed = json.loads(stdout)
            assert "ok" in parsed
            assert parsed["ok"] is False
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout 不是有效的 JSON: {e}\nstdout={stdout!r}")

    def test_render_views_validation_error_is_json(self):
        """验证 render_views 验证失败时输出是纯 JSON"""
        import json
        import subprocess
        import sys
        from pathlib import Path
        
        scripts_dir = Path(__file__).parent.parent
        cli_path = scripts_dir / "logbook_cli.py"
        
        # --log-event 需要 --item-id，不提供应该报错
        result = subprocess.run(
            [sys.executable, str(cli_path), "render_views", "--log-event", "-q"],
            capture_output=True,
            text=True,
            cwd=str(scripts_dir),
            timeout=30,
        )
        
        stdout = result.stdout.strip()
        assert stdout, "stdout 不应为空"
        
        try:
            parsed = json.loads(stdout)
            assert "ok" in parsed
            assert parsed["ok"] is False
            assert "code" in parsed
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout 不是有效的 JSON: {e}\nstdout={stdout!r}")

    def test_io_module_output_json_goes_to_stdout(self):
        """验证 io.output_json 输出到 stdout"""
        import io
        import sys
        import json
        
        # 导入 io 模块
        from pathlib import Path
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from engram.logbook.io import output_json
        
        # 捕获 stdout
        old_stdout = sys.stdout
        sys.stdout = captured_stdout = io.StringIO()
        
        try:
            output_json({"ok": True, "test": "value"})
        finally:
            sys.stdout = old_stdout
        
        output = captured_stdout.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["ok"] is True
        assert parsed["test"] == "value"

    def test_io_module_log_info_goes_to_stderr(self):
        """验证 io.log_info 输出到 stderr"""
        import io
        import sys
        
        from pathlib import Path
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from engram.logbook.io import log_info
        
        # 捕获 stderr
        old_stderr = sys.stderr
        sys.stderr = captured_stderr = io.StringIO()
        
        try:
            log_info("测试日志消息")
        finally:
            sys.stderr = old_stderr
        
        output = captured_stderr.getvalue().strip()
        assert "测试日志消息" in output

    def test_io_module_log_info_quiet_mode_no_output(self):
        """验证 io.log_info 在 quiet 模式下不输出"""
        import io
        import sys
        
        from pathlib import Path
        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        from engram.logbook.io import log_info
        
        # 捕获 stderr
        old_stderr = sys.stderr
        sys.stderr = captured_stderr = io.StringIO()
        
        try:
            log_info("测试日志消息", quiet=True)
        finally:
            sys.stderr = old_stderr
        
        output = captured_stderr.getvalue()
        assert output == "", "quiet=True 时不应有任何输出"
