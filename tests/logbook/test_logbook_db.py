# -*- coding: utf-8 -*-
"""
tests/logbook/test_logbook_db.py - 数据库模块单元测试

测试 engram.logbook.db 模块的关键函数，特别是：
- Database 类的上下文管理器协议
- get_dsn 函数的配置优先级
- get_connection 的 search_path 优先级
- rewrite_sql_for_schema 的重写行为
- create_item / add_event 的 None 检查和类型返回
- attach 函数的 None 检查
- TypedDict 返回结构字段完整性

================================================================================
这些测试验证了 mypy 类型修复后的运行时行为正确性。
================================================================================
"""

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestDatabaseContextManager:
    """测试 Database 类的上下文管理器协议"""

    def test_enter_returns_database_instance(self) -> None:
        """__enter__ 应返回 Database 实例"""
        from engram.logbook.db import Database

        with patch.object(Database, "connect"):
            with patch.object(Database, "disconnect"):
                db = Database(dsn="postgresql://test@localhost/test")
                result = db.__enter__()
                assert result is db

    def test_exit_returns_none(self) -> None:
        """__exit__ 应返回 None（不抑制异常）"""
        from engram.logbook.db import Database

        with patch.object(Database, "disconnect"):
            db = Database(dsn="postgresql://test@localhost/test")
            db._conn = MagicMock()  # 模拟已连接状态
            result = db.__exit__(None, None, None)
            assert result is None


class TestGetConnectionSearchPathPriority:
    """测试 get_connection 的 search_path 优先级"""

    def test_explicit_search_path_list_highest_priority(self) -> None:
        """显式传入 search_path 列表优先级最高"""
        from engram.logbook.db import get_connection
        from engram.logbook.schema_context import SchemaContext

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        schema_ctx = SchemaContext(schema_prefix="test")  # 优先级 2

        with patch("psycopg.connect", return_value=mock_conn):
            with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test@localhost/db"}):
                get_connection(
                    search_path=["custom_schema", "other_schema"],  # 优先级 1
                    schema_context=schema_ctx,
                )

        # 验证使用了显式传入的 search_path
        execute_calls = mock_cursor.execute.call_args_list
        search_path_call = [c for c in execute_calls if "SET search_path" in str(c)]
        assert len(search_path_call) == 1
        assert "custom_schema" in str(search_path_call[0])
        assert "other_schema" in str(search_path_call[0])

    def test_explicit_search_path_string_parsed_correctly(self) -> None:
        """显式传入逗号分隔字符串正确解析"""
        from engram.logbook.db import get_connection

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("psycopg.connect", return_value=mock_conn):
            with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test@localhost/db"}):
                get_connection(search_path="schema_a, schema_b, schema_c")

        execute_calls = mock_cursor.execute.call_args_list
        search_path_call = [c for c in execute_calls if "SET search_path" in str(c)]
        assert len(search_path_call) == 1
        call_str = str(search_path_call[0])
        assert "schema_a" in call_str
        assert "schema_b" in call_str
        assert "schema_c" in call_str

    def test_schema_context_search_path_second_priority(self) -> None:
        """schema_context 的 search_path 为次优先级"""
        from engram.logbook.db import get_connection
        from engram.logbook.schema_context import SchemaContext

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        schema_ctx = SchemaContext(schema_prefix="myprefix")

        with patch("psycopg.connect", return_value=mock_conn):
            with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test@localhost/db"}):
                get_connection(schema_context=schema_ctx)

        execute_calls = mock_cursor.execute.call_args_list
        search_path_call = [c for c in execute_calls if "SET search_path" in str(c)]
        assert len(search_path_call) == 1
        assert "myprefix_logbook" in str(search_path_call[0])

    def test_public_appended_if_missing(self) -> None:
        """search_path 中不包含 public 时自动追加"""
        from engram.logbook.db import get_connection

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("psycopg.connect", return_value=mock_conn):
            with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test@localhost/db"}):
                get_connection(search_path=["myschema"])

        execute_calls = mock_cursor.execute.call_args_list
        search_path_call = [c for c in execute_calls if "SET search_path" in str(c)]
        assert len(search_path_call) == 1
        call_str = str(search_path_call[0])
        assert "myschema" in call_str
        assert "public" in call_str

    def test_search_path_accepts_sequence(self) -> None:
        """search_path 接受 Sequence[str] 类型（如 tuple）"""
        from engram.logbook.db import get_connection

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("psycopg.connect", return_value=mock_conn):
            with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test@localhost/db"}):
                # 传入 tuple（也是 Sequence）
                get_connection(search_path=("tuple_schema1", "tuple_schema2"))

        execute_calls = mock_cursor.execute.call_args_list
        search_path_call = [c for c in execute_calls if "SET search_path" in str(c)]
        assert len(search_path_call) == 1
        call_str = str(search_path_call[0])
        assert "tuple_schema1" in call_str
        assert "tuple_schema2" in call_str


class TestRewriteSqlForSchema:
    """测试 rewrite_sql_for_schema 的重写行为"""

    def test_no_rewrite_without_prefix(self) -> None:
        """无 prefix 时不进行重写"""
        from engram.logbook.db import rewrite_sql_for_schema
        from engram.logbook.schema_context import SchemaContext

        sql = "SELECT * FROM logbook.items"
        ctx = SchemaContext()  # 无 prefix

        result = rewrite_sql_for_schema(sql, ctx)
        assert result == sql

    def test_rewrite_schema_dot_prefix(self) -> None:
        """重写 schema.table 前缀"""
        from engram.logbook.db import rewrite_sql_for_schema
        from engram.logbook.schema_context import SchemaContext

        sql = "SELECT * FROM logbook.items WHERE logbook.items.id = 1"
        ctx = SchemaContext(schema_prefix="test123")

        result = rewrite_sql_for_schema(sql, ctx)
        assert "test123_logbook.items" in result
        # 原始的 "logbook." 应该被替换，但 test123_logbook.items 中包含 logbook 子串
        # 所以检查不应有未加前缀的独立 logbook.
        import re

        # 查找未加前缀的 logbook. (不包含 test123_ 前缀)
        unmodified_pattern = r"(?<!test123_)(?<!\w)logbook\."
        assert re.search(unmodified_pattern, result) is None

    def test_rewrite_create_schema(self) -> None:
        """重写 CREATE SCHEMA 语句"""
        from engram.logbook.db import rewrite_sql_for_schema
        from engram.logbook.schema_context import SchemaContext

        sql = "CREATE SCHEMA IF NOT EXISTS logbook;"
        ctx = SchemaContext(schema_prefix="mytest")

        result = rewrite_sql_for_schema(sql, ctx)
        assert "CREATE SCHEMA IF NOT EXISTS mytest_logbook" in result

    def test_rewrite_table_schema_check(self) -> None:
        """重写 table_schema = 'xxx' 检查"""
        from engram.logbook.db import rewrite_sql_for_schema
        from engram.logbook.schema_context import SchemaContext

        sql = "SELECT * FROM information_schema.tables WHERE table_schema = 'logbook'"
        ctx = SchemaContext(schema_prefix="prefix")

        result = rewrite_sql_for_schema(sql, ctx)
        assert "table_schema = 'prefix_logbook'" in result

    def test_rewrite_multiple_schemas(self) -> None:
        """重写多个不同 schema"""
        from engram.logbook.db import rewrite_sql_for_schema
        from engram.logbook.schema_context import SchemaContext

        sql = """
        SELECT * FROM logbook.items i
        JOIN scm.commits c ON i.item_id = c.item_id
        JOIN identity.users u ON i.owner_user_id = u.user_id
        """
        ctx = SchemaContext(schema_prefix="t")

        result = rewrite_sql_for_schema(sql, ctx)
        assert "t_logbook.items" in result
        assert "t_scm.commits" in result
        assert "t_identity.users" in result

    def test_rewrite_regclass_cast(self) -> None:
        """重写 regclass 类型转换"""
        from engram.logbook.db import rewrite_sql_for_schema
        from engram.logbook.schema_context import SchemaContext

        sql = "SELECT 'scm.patch_blobs'::regclass"
        ctx = SchemaContext(schema_prefix="test")

        result = rewrite_sql_for_schema(sql, ctx)
        assert "'test_scm.patch_blobs'::regclass" in result

    def test_none_context_returns_original(self) -> None:
        """schema_context 为 None 时返回原始 SQL"""
        from engram.logbook.db import rewrite_sql_for_schema

        sql = "SELECT * FROM logbook.items"

        result = rewrite_sql_for_schema(sql, None)
        assert result == sql


class TestGetDsn:
    """测试 get_dsn 函数的配置优先级"""

    def test_config_dsn_has_highest_priority(self) -> None:
        """配置中的 postgres.dsn 优先级最高"""
        from engram.logbook.config import Config
        from engram.logbook.db import get_dsn

        config = MagicMock(spec=Config)
        config.get.return_value = "postgresql://config@localhost/db"

        result = get_dsn(config)
        assert result == "postgresql://config@localhost/db"

    def test_env_postgres_dsn_second_priority(self) -> None:
        """POSTGRES_DSN 环境变量为次优先级"""
        from engram.logbook.config import Config
        from engram.logbook.db import get_dsn

        config = MagicMock(spec=Config)
        config.get.return_value = None

        with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://env@localhost/db"}, clear=False):
            result = get_dsn(config)
            assert result == "postgresql://env@localhost/db"

    def test_env_test_pg_dsn_third_priority(self) -> None:
        """TEST_PG_DSN 环境变量为第三优先级"""
        from engram.logbook.config import Config
        from engram.logbook.db import get_dsn

        config = MagicMock(spec=Config)
        config.get.return_value = None

        # 清除 POSTGRES_DSN，只设置 TEST_PG_DSN
        env = {"TEST_PG_DSN": "postgresql://test@localhost/db"}
        with patch.dict(os.environ, env, clear=False):
            # 确保 POSTGRES_DSN 不存在
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("POSTGRES_DSN", None)
                result = get_dsn(config)
                assert result == "postgresql://test@localhost/db"

    def test_raises_config_error_when_no_dsn(self) -> None:
        """无可用 DSN 时应抛出 ConfigError"""
        from engram.logbook.config import Config
        from engram.logbook.db import get_dsn
        from engram.logbook.errors import ConfigError

        config = MagicMock(spec=Config)
        config.get.return_value = None

        # 清除所有 DSN 相关环境变量
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError) as exc_info:
                get_dsn(config)
            assert "未找到数据库 DSN 配置" in str(exc_info.value)


class TestCreateItemNoneCheck:
    """测试 create_item 的 None 检查"""

    @pytest.mark.integration
    def test_create_item_returns_int(self, migrated_db: Any) -> None:
        """create_item 应返回 int 类型的 item_id"""
        from engram.logbook import db

        dsn = migrated_db["dsn"]
        item_id = db.create_item(
            item_type="task",
            title="Test Task",
            project_key="test_project",
            dsn=dsn,
        )
        assert isinstance(item_id, int)
        assert item_id > 0


class TestAddEventNoneCheck:
    """测试 add_event 的 None 检查"""

    @pytest.mark.integration
    def test_add_event_returns_int(self, migrated_db: Any) -> None:
        """add_event 应返回 int 类型的 event_id"""
        from engram.logbook import db

        dsn = migrated_db["dsn"]
        # 先创建一个 item
        item_id = db.create_item(
            item_type="task",
            title="Test Task for Event",
            project_key="test_project",
            dsn=dsn,
        )

        # 添加事件
        event_id = db.add_event(
            item_id=item_id,
            event_type="status_change",
            payload={"from": "new", "to": "in_progress"},
            dsn=dsn,
        )
        assert isinstance(event_id, int)
        assert event_id > 0


class TestAttachNoneCheck:
    """测试 attach 的 None 检查"""

    @pytest.mark.integration
    def test_attach_returns_int(self, migrated_db: Any) -> None:
        """attach 应返回 int 类型的 attachment_id"""
        from engram.logbook import db

        dsn = migrated_db["dsn"]
        # 先创建一个 item
        item_id = db.create_item(
            item_type="task",
            title="Test Task for Attachment",
            project_key="test_project",
            dsn=dsn,
        )

        # 添加附件
        attachment_id = db.attach(
            item_id=item_id,
            kind="document",
            uri="file:///path/to/document.txt",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=1024,
            dsn=dsn,
        )
        assert isinstance(attachment_id, int)
        assert attachment_id > 0


class TestTypedDictReturnStructure:
    """测试 TypedDict 返回结构字段完整性"""

    def test_item_row_has_all_required_fields(self) -> None:
        """ItemRow 应包含所有必需字段"""
        from engram.logbook.db import ItemRow

        # 验证 TypedDict 定义的字段
        expected_fields = {
            "item_id",
            "id",
            "item_type",
            "title",
            "project_key",
            "scope_json",
            "status",
            "owner_user_id",
            "created_at",
            "updated_at",
        }
        assert set(ItemRow.__annotations__.keys()) == expected_fields

    def test_item_with_latest_event_row_fields(self) -> None:
        """ItemWithLatestEventRow 应包含 item 和事件字段"""
        from engram.logbook.db import ItemWithLatestEventRow

        expected_fields = {
            "item_id",
            "item_type",
            "title",
            "scope_json",
            "status",
            "owner_user_id",
            "created_at",
            "updated_at",
            "latest_event_id",
            "latest_event_type",
            "latest_event_ts",
        }
        assert set(ItemWithLatestEventRow.__annotations__.keys()) == expected_fields

    def test_event_row_has_all_required_fields(self) -> None:
        """EventRow 应包含所有必需字段"""
        from engram.logbook.db import EventRow

        expected_fields = {
            "event_id",
            "item_id",
            "event_type",
            "status_from",
            "status_to",
            "payload_json",
            "actor_user_id",
            "source",
            "created_at",
        }
        assert set(EventRow.__annotations__.keys()) == expected_fields

    def test_attachment_row_has_all_required_fields(self) -> None:
        """AttachmentRow 应包含所有必需字段"""
        from engram.logbook.db import AttachmentRow

        expected_fields = {
            "attachment_id",
            "item_id",
            "kind",
            "uri",
            "sha256",
            "size_bytes",
            "meta_json",
            "created_at",
        }
        assert set(AttachmentRow.__annotations__.keys()) == expected_fields

    def test_knowledge_candidate_row_has_all_required_fields(self) -> None:
        """KnowledgeCandidateRow 应包含所有必需字段"""
        from engram.logbook.db import KnowledgeCandidateRow

        expected_fields = {
            "candidate_id",
            "run_id",
            "kind",
            "title",
            "content_md",
            "confidence",
            "evidence_refs_json",
            "promote_suggested",
            "created_at",
        }
        assert set(KnowledgeCandidateRow.__annotations__.keys()) == expected_fields

    @pytest.mark.integration
    def test_get_item_by_id_returns_item_row(self, migrated_db: Any) -> None:
        """get_item_by_id 返回的字典包含所有 ItemRow 字段"""
        from engram.logbook import db
        from engram.logbook.db import ItemRow

        dsn = migrated_db["dsn"]
        item_id = db.create_item(
            item_type="test_type",
            title="Test Item for TypedDict",
            project_key="test_proj",
            dsn=dsn,
        )

        result = db.get_item_by_id(item_id=item_id, dsn=dsn)

        assert result is not None
        # 验证返回的字典包含 ItemRow 的所有字段
        for field in ItemRow.__annotations__.keys():
            assert field in result, f"缺少字段: {field}"

    @pytest.mark.integration
    def test_get_items_with_latest_event_returns_typed_list(self, migrated_db: Any) -> None:
        """get_items_with_latest_event 返回类型化列表"""
        from engram.logbook import db
        from engram.logbook.db import ItemWithLatestEventRow

        dsn = migrated_db["dsn"]
        # 创建测试数据
        item_id = db.create_item(
            item_type="test_type",
            title="Test Item for List",
            project_key="test_proj",
            dsn=dsn,
        )
        db.add_event(
            item_id=item_id,
            event_type="test_event",
            payload={"test": "data"},
            dsn=dsn,
        )

        result = db.get_items_with_latest_event(limit=10, dsn=dsn)

        assert isinstance(result, list)
        assert len(result) > 0
        # 验证每个元素包含所有字段
        for item in result:
            for field in ItemWithLatestEventRow.__annotations__.keys():
                assert field in item, f"缺少字段: {field}"


class TestDatabaseClassTypeAnnotations:
    """测试 Database 类的类型注解"""

    def test_database_init_accepts_dsn_or_config(self) -> None:
        """Database 构造函数应接受 dsn 或 config 参数"""
        from engram.logbook.config import Config
        from engram.logbook.db import Database

        # 仅 DSN
        db1 = Database(dsn="postgresql://test@localhost/db1")
        assert db1._dsn_override == "postgresql://test@localhost/db1"

        # 仅 Config
        config = MagicMock(spec=Config)
        db2 = Database(config=config)
        assert db2._config is config

        # 同时提供 DSN 和 Config
        db3 = Database(dsn="postgresql://test@localhost/db3", config=config)
        assert db3._dsn_override == "postgresql://test@localhost/db3"
        assert db3._config is config

    def test_database_connection_property_type(self) -> None:
        """Database.connection 属性应返回 Optional[psycopg.Connection]"""
        from engram.logbook.db import Database

        db = Database(dsn="postgresql://test@localhost/test")
        # 未连接时应为 None
        assert db.connection is None

    def test_kwargs_type_annotation(self) -> None:
        """验证 create_item 和 add_event 的 kwargs 类型注解"""
        import inspect

        from engram.logbook.db import Database

        # 检查 create_item 方法签名
        sig = inspect.signature(Database.create_item)
        params = sig.parameters
        assert "kwargs" in params
        # kwargs 应该有 Any 类型注解
        assert params["kwargs"].annotation == Any or str(params["kwargs"].annotation) == "Any"

        # 检查 add_event 方法签名
        sig = inspect.signature(Database.add_event)
        params = sig.parameters
        assert "kwargs" in params


class TestJsonBoundary:
    """测试 JSON 边界约定"""

    def test_json_value_type_alias_defined(self) -> None:
        """JsonValue 类型别名已定义"""
        from engram.logbook.db import JsonArray, JsonObject, JsonValue

        # 这些类型别名应该可以导入
        assert JsonValue is not None
        assert JsonObject is not None
        assert JsonArray is not None

    @pytest.mark.integration
    def test_create_item_accepts_dict_scope(self, migrated_db: Any) -> None:
        """create_item 接受结构化 dict 作为 scope_json"""
        from engram.logbook import db

        dsn = migrated_db["dsn"]
        scope = {"module": "test", "tags": ["a", "b"], "nested": {"key": 123}}

        item_id = db.create_item(
            item_type="test",
            title="Test JSON Boundary",
            scope_json=scope,
            dsn=dsn,
        )

        result = db.get_item_by_id(item_id=item_id, dsn=dsn)
        assert result is not None
        # 验证 JSON 被正确存储和读取
        assert result["scope_json"] == scope

    @pytest.mark.integration
    def test_add_event_accepts_dict_payload(self, migrated_db: Any) -> None:
        """add_event 接受结构化 dict 作为 payload"""
        from engram.logbook import db

        dsn = migrated_db["dsn"]
        item_id = db.create_item(
            item_type="test",
            title="Test for Event Payload",
            dsn=dsn,
        )

        payload = {"action": "test", "values": [1, 2, 3], "nested": {"flag": True}}
        event_id = db.add_event(
            item_id=item_id,
            event_type="test_event",
            payload_json=payload,
            dsn=dsn,
        )

        assert event_id > 0

    @pytest.mark.integration
    def test_set_kv_accepts_various_json_types(self, migrated_db: Any) -> None:
        """set_kv 接受各种 JSON 可序列化类型"""
        from engram.logbook import db

        dsn = migrated_db["dsn"]

        # 测试不同类型的值
        test_cases = [
            ("test_str", "string_value"),
            ("test_int", 42),
            ("test_float", 3.14),
            ("test_bool", True),
            ("test_null", None),
            ("test_dict", {"key": "value", "nested": {"a": 1}}),
            ("test_list", [1, "two", {"three": 3}]),
        ]

        for key, value in test_cases:
            db.set_kv(namespace="json_test", key=key, value_json=value, dsn=dsn)
            result = db.get_kv(namespace="json_test", key=key, dsn=dsn)
            assert result == value, f"Failed for key={key}, expected {value}, got {result}"


class TestExecuteSqlFilePlaceholders:
    """测试 execute_sql_file 的 placeholders 类型约束"""

    def test_placeholders_type_accepts_mapping(self) -> None:
        """placeholders 参数接受 Mapping[str, str] 类型"""
        import inspect

        from engram.logbook.db import execute_sql_file

        sig = inspect.signature(execute_sql_file)
        params = sig.parameters

        assert "placeholders" in params
        # 检查类型注解
        annotation = params["placeholders"].annotation
        annotation_str = str(annotation)
        assert "Mapping" in annotation_str or "mapping" in annotation_str.lower()


class TestWriteAuditRowTypeAnnotations:
    """测试 WriteAuditRow TypedDict 字段完整性"""

    def test_write_audit_row_has_all_required_fields(self) -> None:
        """WriteAuditRow 应包含所有必需字段，包括新增的 correlation_id/status/updated_at"""
        from engram.logbook.governance import WriteAuditRow

        expected_fields = {
            "audit_id",
            "actor_user_id",
            "target_space",
            "action",
            "reason",
            "payload_sha",
            "evidence_refs_json",
            "correlation_id",
            "status",
            "created_at",
            "updated_at",
        }
        assert set(WriteAuditRow.__annotations__.keys()) == expected_fields


class TestInsertWriteAuditWithCorrelation:
    """测试 insert_write_audit 的 correlation_id 和 status 参数"""

    def test_insert_write_audit_accepts_correlation_id_and_status(self) -> None:
        """insert_write_audit 应接受 correlation_id 和 status 参数"""
        import inspect

        from engram.logbook.governance import insert_write_audit

        sig = inspect.signature(insert_write_audit)
        params = sig.parameters

        # 验证新参数存在
        assert "correlation_id" in params
        assert "status" in params

        # 验证默认值
        assert params["correlation_id"].default is None
        assert params["status"].default == "success"

    def test_insert_write_audit_validates_status(self) -> None:
        """insert_write_audit 应验证 status 值"""
        from engram.logbook.errors import ValidationError
        from engram.logbook.governance import insert_write_audit

        with pytest.raises(ValidationError) as exc_info:
            insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                status="invalid_status",  # 无效状态
            )
        assert "status" in str(exc_info.value)

    @pytest.mark.integration
    def test_insert_write_audit_with_correlation_id(self, migrated_db: Any) -> None:
        """insert_write_audit 应正确写入 correlation_id 和 status"""
        from engram.logbook.governance import (
            get_write_audit_by_correlation_id,
            insert_write_audit,
        )

        dsn = migrated_db["dsn"]
        from engram.logbook.config import Config

        config = MagicMock(spec=Config)
        config.get.return_value = dsn

        # 插入带 correlation_id 的审计记录
        audit_id = insert_write_audit(
            actor_user_id="test_user",
            target_space="team:test_project",
            action="allow",
            reason="test reason",
            correlation_id="test-correlation-123",
            status="pending",
            config=config,
        )

        assert isinstance(audit_id, int)
        assert audit_id > 0

        # 查询并验证
        results = get_write_audit_by_correlation_id(
            correlation_id="test-correlation-123",
            config=config,
        )
        assert len(results) == 1
        assert results[0]["correlation_id"] == "test-correlation-123"
        assert results[0]["status"] == "pending"


class TestUpdateWriteAudit:
    """测试 update_write_audit 函数"""

    def test_update_write_audit_validates_empty_correlation_id(self) -> None:
        """update_write_audit 应验证 correlation_id 不为空"""
        from engram.logbook.errors import ValidationError
        from engram.logbook.governance import update_write_audit

        with pytest.raises(ValidationError) as exc_info:
            update_write_audit(
                correlation_id="",
                status="success",
            )
        assert "correlation_id" in str(exc_info.value)

    def test_update_write_audit_validates_status(self) -> None:
        """update_write_audit 应验证 status 值"""
        from engram.logbook.errors import ValidationError
        from engram.logbook.governance import update_write_audit

        with pytest.raises(ValidationError) as exc_info:
            update_write_audit(
                correlation_id="test-123",
                status="invalid_status",
            )
        assert "status" in str(exc_info.value)

    @pytest.mark.integration
    def test_update_write_audit_updates_status(self, migrated_db: Any) -> None:
        """update_write_audit 应正确更新状态"""
        from engram.logbook.governance import (
            get_write_audit_by_correlation_id,
            insert_write_audit,
            update_write_audit,
        )

        dsn = migrated_db["dsn"]
        from engram.logbook.config import Config

        config = MagicMock(spec=Config)
        config.get.return_value = dsn

        # 插入 pending 状态的记录
        insert_write_audit(
            actor_user_id="test_user",
            target_space="team:test_project",
            action="allow",
            correlation_id="update-test-123",
            status="pending",
            config=config,
        )

        # 更新为 success
        updated_count = update_write_audit(
            correlation_id="update-test-123",
            status="success",
            config=config,
        )
        assert updated_count == 1

        # 验证更新结果
        results = get_write_audit_by_correlation_id(
            correlation_id="update-test-123",
            config=config,
        )
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert results[0]["updated_at"] is not None

    @pytest.mark.integration
    def test_update_write_audit_with_reason_suffix(self, migrated_db: Any) -> None:
        """update_write_audit 应正确追加 reason_suffix"""
        from engram.logbook.governance import (
            get_write_audit_by_correlation_id,
            insert_write_audit,
            update_write_audit,
        )

        dsn = migrated_db["dsn"]
        from engram.logbook.config import Config

        config = MagicMock(spec=Config)
        config.get.return_value = dsn

        # 插入带 reason 的记录
        insert_write_audit(
            actor_user_id="test_user",
            target_space="team:test_project",
            action="allow",
            reason="initial reason",
            correlation_id="reason-test-123",
            status="pending",
            config=config,
        )

        # 更新为 failed 并追加原因
        update_write_audit(
            correlation_id="reason-test-123",
            status="failed",
            reason_suffix="timeout after 30s",
            config=config,
        )

        # 验证 reason 被追加
        results = get_write_audit_by_correlation_id(
            correlation_id="reason-test-123",
            config=config,
        )
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert "initial reason" in str(results[0]["reason"])
        assert "timeout after 30s" in str(results[0]["reason"])

    @pytest.mark.integration
    def test_update_write_audit_with_evidence_refs_json_patch(self, migrated_db: Any) -> None:
        """
        update_write_audit 应正确合并 evidence_refs_json_patch 到顶层

        契约验证（方案 A）：
        - evidence_refs_json_patch 中的字段应合并到 evidence_refs_json 顶层
        - 使用 jsonb || 合并操作，是幂等的
        - 合并后的字段可通过 evidence_refs_json->>'key' 查询

        参见: docs/contracts/gateway_audit_evidence_correlation_contract.md 5.4 节
        """
        from engram.logbook.governance import (
            get_write_audit_by_correlation_id,
            insert_write_audit,
            update_write_audit,
        )

        dsn = migrated_db["dsn"]
        from engram.logbook.config import Config

        config = MagicMock(spec=Config)
        config.get.return_value = dsn

        # 插入 pending 状态的记录，带初始 evidence_refs_json
        initial_evidence_refs_json = {
            "gateway_event": {
                "operation": "memory_store",
                "correlation_id": "patch-test-123",
            }
        }
        insert_write_audit(
            actor_user_id="test_user",
            target_space="team:test_project",
            action="allow",
            reason="policy:passed",
            correlation_id="patch-test-123",
            status="pending",
            evidence_refs_json=initial_evidence_refs_json,
            config=config,
        )

        # 使用 evidence_refs_json_patch 更新（模拟 OpenMemory 失败入队场景）
        update_write_audit(
            correlation_id="patch-test-123",
            status="redirected",
            reason_suffix=":outbox:42",
            evidence_refs_json_patch={
                "outbox_id": 42,
                "intended_action": "allow",
            },
            config=config,
        )

        # 验证合并结果
        results = get_write_audit_by_correlation_id(
            correlation_id="patch-test-123",
            config=config,
        )
        assert len(results) == 1
        result = results[0]

        # 验证状态更新
        assert result["status"] == "redirected"
        assert ":outbox:42" in str(result["reason"])

        # 验证 updated_at 已更新（不为 None，表示更新操作已正确设置时间戳）
        assert result["updated_at"] is not None

        # 验证 evidence_refs_json_patch 已合并到顶层（方案 A 核心契约）
        evidence_refs_json = result["evidence_refs_json"]
        assert evidence_refs_json.get("outbox_id") == 42
        assert evidence_refs_json.get("intended_action") == "allow"

        # 验证原有字段保留
        assert "gateway_event" in evidence_refs_json
        assert evidence_refs_json["gateway_event"]["operation"] == "memory_store"

    @pytest.mark.integration
    def test_update_write_audit_evidence_refs_json_patch_is_idempotent(
        self, migrated_db: Any
    ) -> None:
        """
        update_write_audit 的 evidence_refs_json_patch 合并应是幂等的

        契约验证：多次使用相同 patch 调用，结果应一致（jsonb || 的幂等性）
        """
        from engram.logbook.governance import (
            get_write_audit_by_correlation_id,
            insert_write_audit,
            update_write_audit,
        )

        dsn = migrated_db["dsn"]
        from engram.logbook.config import Config

        config = MagicMock(spec=Config)
        config.get.return_value = dsn

        # 插入 pending 状态的记录
        insert_write_audit(
            actor_user_id="test_user",
            target_space="team:test_project",
            action="allow",
            correlation_id="idempotent-test-123",
            status="pending",
            evidence_refs_json={"initial": "value"},
            config=config,
        )

        # 第一次更新
        count1 = update_write_audit(
            correlation_id="idempotent-test-123",
            status="success",
            evidence_refs_json_patch={"memory_id": "mem-abc123"},
            config=config,
        )
        assert count1 == 1

        # 获取第一次更新后的结果
        results1 = get_write_audit_by_correlation_id(
            correlation_id="idempotent-test-123",
            config=config,
        )
        evidence1 = results1[0]["evidence_refs_json"]
        assert evidence1.get("memory_id") == "mem-abc123"
        assert evidence1.get("initial") == "value"

        # 第二次更新同一记录（已经是 success 状态，WHERE status='pending' 不匹配）
        # 因此不会更新任何行
        count2 = update_write_audit(
            correlation_id="idempotent-test-123",
            status="success",
            evidence_refs_json_patch={"memory_id": "mem-def456"},  # 不同值
            config=config,
        )
        # 由于 WHERE status='pending' 条件，已经是 success 的记录不会被更新
        assert count2 == 0

        # 验证结果不变（幂等性：第二次调用不影响已完成的记录）
        results2 = get_write_audit_by_correlation_id(
            correlation_id="idempotent-test-123",
            config=config,
        )
        evidence2 = results2[0]["evidence_refs_json"]
        assert evidence2.get("memory_id") == "mem-abc123"  # 保持第一次的值


class TestQueryWriteAuditWithNewFilters:
    """测试 query_write_audit 的新过滤参数"""

    def test_query_write_audit_accepts_new_filters(self) -> None:
        """query_write_audit 应接受 correlation_id 和 status 过滤参数"""
        import inspect

        from engram.logbook.governance import query_write_audit

        sig = inspect.signature(query_write_audit)
        params = sig.parameters

        assert "correlation_id" in params
        assert "status" in params

    @pytest.mark.integration
    def test_query_write_audit_filters_by_status(self, migrated_db: Any) -> None:
        """query_write_audit 应正确按 status 筛选"""
        from engram.logbook.governance import insert_write_audit, query_write_audit

        dsn = migrated_db["dsn"]
        from engram.logbook.config import Config

        config = MagicMock(spec=Config)
        config.get.return_value = dsn

        # 插入不同状态的记录
        insert_write_audit(
            actor_user_id="test_user",
            target_space="team:filter_test",
            action="allow",
            status="success",
            config=config,
        )
        insert_write_audit(
            actor_user_id="test_user",
            target_space="team:filter_test",
            action="allow",
            status="pending",
            config=config,
        )

        # 按 status 筛选
        pending_results = query_write_audit(
            target_space="team:filter_test",
            status="pending",
            config=config,
        )
        assert all(r["status"] == "pending" for r in pending_results)

        success_results = query_write_audit(
            target_space="team:filter_test",
            status="success",
            config=config,
        )
        assert all(r["status"] == "success" for r in success_results)
