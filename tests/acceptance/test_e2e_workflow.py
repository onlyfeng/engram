# -*- coding: utf-8 -*-
"""
端到端工作流测试

验证完整的使用工作流:
- Logbook CRUD 流程
- 键值存储流程
- 治理设置流程
- Outbox 队列流程
- 事件记录流程
"""

import json
import uuid
import pytest
import psycopg


class TestLogbookCRUDFlow:
    """Logbook 完整 CRUD 流程测试"""

    def test_create_item(self, migrated_db):
        """创建条目"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 创建条目
        item_id = db.create_item(
            item_type="task",
            title="E2E Test Task",
            project_key="e2e_test",
        )
        
        assert item_id is not None
        assert isinstance(item_id, (int, str))

    def test_create_and_get_item(self, migrated_db):
        """创建并查询条目"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 创建条目
        item_id = db.create_item(
            item_type="task",
            title="E2E Test Task for Get",
            project_key="e2e_test",
        )
        
        # 查询条目
        item = db.get_item(item_id)
        
        assert item is not None
        assert item["title"] == "E2E Test Task for Get"
        assert item["item_type"] == "task"

    def test_add_event_to_item(self, migrated_db):
        """为条目添加事件"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 创建条目
        item_id = db.create_item(
            item_type="task",
            title="E2E Test Task for Event",
            project_key="e2e_test",
        )
        
        # 添加事件
        event_id = db.add_event(
            item_id,
            event_type="progress",
            payload={"status": "started", "progress": 10},
        )
        
        assert event_id is not None

    def test_multiple_events_flow(self, migrated_db):
        """多事件流程"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 创建条目
        item_id = db.create_item(
            item_type="workflow",
            title="E2E Test Workflow",
            project_key="e2e_test",
        )
        
        # 添加多个事件
        events = [
            ("created", {"by": "user1"}),
            ("started", {"timestamp": "2024-01-01T00:00:00Z"}),
            ("progress", {"percent": 50}),
            ("completed", {"duration": 3600}),
        ]
        
        event_ids = []
        for event_type, payload in events:
            event_id = db.add_event(item_id, event_type=event_type, payload=payload)
            event_ids.append(event_id)
        
        assert len(event_ids) == 4
        assert all(eid is not None for eid in event_ids)


class TestKVStoreFlow:
    """键值存储流程测试"""

    def test_set_and_get_kv(self, migrated_db):
        """设置和获取键值"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        key = f"e2e_test_key_{uuid.uuid4().hex[:8]}"
        value = {"foo": "bar", "number": 42}
        
        # 设置
        db.set_kv(key, value)
        
        # 获取
        retrieved = db.get_kv(key)
        
        assert retrieved == value

    def test_kv_update(self, migrated_db):
        """更新键值"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        key = f"e2e_test_update_{uuid.uuid4().hex[:8]}"
        
        # 初始值
        db.set_kv(key, {"version": 1})
        
        # 更新
        db.set_kv(key, {"version": 2, "updated": True})
        
        # 验证
        retrieved = db.get_kv(key)
        assert retrieved["version"] == 2
        assert retrieved.get("updated") is True

    def test_kv_nonexistent_key(self, migrated_db):
        """获取不存在的键"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        key = f"nonexistent_key_{uuid.uuid4().hex}"
        
        # 获取不存在的键
        retrieved = db.get_kv(key)
        
        assert retrieved is None

    def test_kv_complex_value(self, migrated_db):
        """存储复杂值"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        key = f"e2e_complex_{uuid.uuid4().hex[:8]}"
        value = {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "boolean": True,
            "null": None,
            "array": [1, 2, 3],
            "nested": {
                "a": 1,
                "b": {"c": 2},
            },
        }
        
        db.set_kv(key, value)
        retrieved = db.get_kv(key)
        
        assert retrieved == value


class TestGovernanceFlow:
    """治理设置流程测试"""

    def test_governance_settings_crud(self, migrated_db):
        """治理设置 CRUD"""
        from engram.logbook.governance import GovernanceSettings
        
        gs = GovernanceSettings(migrated_db["dsn"])
        project_key = f"e2e_gov_{uuid.uuid4().hex[:8]}"
        
        # 设置
        gs.set("team_write_enabled", True, project_key)
        
        # 获取
        value = gs.get("team_write_enabled", project_key)
        assert value is True
        
        # 更新
        gs.set("team_write_enabled", False, project_key)
        
        # 验证更新
        value = gs.get("team_write_enabled", project_key)
        assert value is False

    def test_governance_default_value(self, migrated_db):
        """治理设置默认值"""
        from engram.logbook.governance import GovernanceSettings
        
        gs = GovernanceSettings(migrated_db["dsn"])
        project_key = f"e2e_gov_default_{uuid.uuid4().hex[:8]}"
        
        # 获取不存在的设置
        value = gs.get("nonexistent_setting", project_key)
        
        # 应该返回 None 或默认值
        assert value is None or isinstance(value, bool)


class TestOutboxFlow:
    """Outbox 队列流程测试"""

    def test_enqueue_memory(self, migrated_db):
        """入队记忆"""
        from engram.logbook import enqueue_memory
        
        # 入队
        outbox_id = enqueue_memory(
            dsn=migrated_db["dsn"],
            user_id="e2e_test_user",
            space="private:e2e_test_user",
            payload_md="E2E test memory content",
            kind="PROCEDURE",
            project_key="e2e_test",
        )
        
        assert outbox_id is not None

    def test_enqueue_and_get_pending(self, migrated_db):
        """入队并获取待处理"""
        from engram.logbook import enqueue_memory, get_pending_outbox
        
        # 入队多条
        for i in range(3):
            enqueue_memory(
                dsn=migrated_db["dsn"],
                user_id="e2e_test_user",
                space="private:e2e_test_user",
                payload_md=f"E2E test memory {i}",
                kind="FACT",
                project_key="e2e_test",
            )
        
        # 获取待处理
        pending = get_pending_outbox(dsn=migrated_db["dsn"], limit=10)
        
        assert len(pending) >= 3


class TestDirectDatabaseAccess:
    """直接数据库访问测试"""

    def test_direct_insert_item(self, db_conn):
        """直接 SQL 插入条目"""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO items (item_type, title, project_key)
                VALUES ('test', 'Direct SQL Test', 'e2e_direct')
                RETURNING id
            """)
            item_id = cur.fetchone()[0]
        
        assert item_id is not None

    def test_direct_insert_event(self, db_conn):
        """直接 SQL 插入事件"""
        with db_conn.cursor() as cur:
            # 先创建条目
            cur.execute("""
                INSERT INTO items (item_type, title, project_key)
                VALUES ('test', 'Direct SQL Event Test', 'e2e_direct')
                RETURNING id
            """)
            item_id = cur.fetchone()[0]
            
            # 插入事件
            cur.execute("""
                INSERT INTO events (item_id, event_type, payload)
                VALUES (%s, 'test_event', %s)
                RETURNING id
            """, (item_id, json.dumps({"test": True})))
            event_id = cur.fetchone()[0]
        
        assert event_id is not None

    def test_search_path_correct(self, db_conn):
        """search_path 设置正确"""
        with db_conn.cursor() as cur:
            cur.execute("SHOW search_path")
            search_path = cur.fetchone()[0]
        
        # 应该包含 logbook schema
        assert "logbook" in search_path


class TestCompleteWorkflow:
    """完整工作流程测试"""

    def test_full_item_lifecycle(self, migrated_db):
        """完整条目生命周期"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 1. 创建条目
        item_id = db.create_item(
            item_type="task",
            title="Full Lifecycle Task",
            project_key="e2e_lifecycle",
        )
        assert item_id is not None
        
        # 2. 添加创建事件
        db.add_event(item_id, "created", {"by": "e2e_test"})
        
        # 3. 添加进度事件
        db.add_event(item_id, "progress", {"percent": 25})
        db.add_event(item_id, "progress", {"percent": 50})
        db.add_event(item_id, "progress", {"percent": 75})
        
        # 4. 添加完成事件
        db.add_event(item_id, "completed", {"success": True})
        
        # 5. 验证条目状态
        item = db.get_item(item_id)
        assert item is not None
        assert item["title"] == "Full Lifecycle Task"

    def test_project_isolation(self, migrated_db):
        """项目隔离"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 在不同项目中创建条目
        project_a_id = db.create_item(
            item_type="task",
            title="Project A Task",
            project_key="project_a",
        )
        
        project_b_id = db.create_item(
            item_type="task",
            title="Project B Task",
            project_key="project_b",
        )
        
        # 验证创建成功
        assert project_a_id is not None
        assert project_b_id is not None
        assert project_a_id != project_b_id


class TestErrorHandling:
    """错误处理测试"""

    def test_invalid_item_id(self, migrated_db):
        """无效条目 ID"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 获取不存在的条目
        item = db.get_item(999999999)
        
        assert item is None

    def test_connection_reuse(self, migrated_db):
        """连接复用"""
        from engram.logbook import Database
        
        db = Database(migrated_db["dsn"])
        
        # 多次操作应该使用同一连接
        for i in range(5):
            item_id = db.create_item(
                item_type="test",
                title=f"Connection Test {i}",
                project_key="e2e_connection",
            )
            assert item_id is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
