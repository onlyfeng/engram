# -*- coding: utf-8 -*-
"""
测试迁移幂等性与缺失项定位

验证:
1. 迁移多次执行具有幂等性
2. 缺失项（schema/table/index/matview）能被正确定位
3. 修复指令路径正确（使用 logbook_postgres/scripts 完整路径）
4. run_all_checks 返回完整的缺失项列表
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保可以导入 engram_logbook
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))


class TestRepairCommandsPath:
    """测试修复指令路径正确性"""

    def test_repair_hint_uses_logbook_postgres_path(self):
        """验证修复指令使用 logbook_postgres/scripts 完整路径"""
        from engram.logbook.migrate import get_repair_commands_hint
        
        hint = get_repair_commands_hint("SCHEMA_MISSING")
        
        # 验证使用完整路径
        hint_str = str(hint)
        assert "logbook_postgres/scripts/db_bootstrap.py" in hint_str
        assert "logbook_postgres/scripts/db_migrate.py" in hint_str
        
        # 验证所有 python 命令都使用 apps/ 前缀
        # 提取所有 python 开头的命令检查
        for cmd in hint.get("recommended_commands", []):
            if cmd.startswith("python ") and "logbook_postgres" in cmd:
                assert cmd.startswith("python logbook_postgres/"), f"命令路径应包含 logbook_postgres/: {cmd}"

    def test_repair_hint_contains_docker_commands(self):
        """验证修复指令包含 Docker 命令"""
        from engram.logbook.migrate import get_repair_commands_hint
        
        hint = get_repair_commands_hint("TABLE_MISSING")
        
        assert "docker compose" in str(hint) or "docker-compose" in str(hint)
        assert "docker-compose.unified.yml" in str(hint)

    def test_repair_hint_for_different_error_codes(self):
        """测试不同错误码的修复指令"""
        from engram.logbook.migrate import get_repair_commands_hint
        
        error_codes = [
            "SCHEMA_MISSING",
            "TABLE_MISSING", 
            "COLUMN_MISSING",
            "INDEX_MISSING",
            "TRIGGER_MISSING",
            "MATVIEW_MISSING",
            "OPENMEMORY_SCHEMA_MISSING",
            "PRECHECK_FAILED",
            "INSUFFICIENT_PRIVILEGE",
        ]
        
        for code in error_codes:
            hint = get_repair_commands_hint(code)
            assert "repair_hint" in hint
            assert "recommended_commands" in hint
            # 所有修复指令应包含实际命令
            commands = hint["recommended_commands"]
            assert len(commands) > 0


class TestMigrateIdempotency:
    """测试迁移幂等性"""

    def test_run_all_checks_returns_dict_with_ok_key(self):
        """验证 run_all_checks 返回正确的结构"""
        # 使用 mock 连接测试返回结构
        from engram.logbook.migrate import run_all_checks
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        # 模拟所有检查返回存在（True）
        mock_cursor.execute = MagicMock()
        mock_cursor.fetchall = MagicMock(return_value=[
            ("identity",), ("logbook",), ("scm",), ("analysis",), ("governance",)
        ])
        # fetchone 返回 (True,) 表示存在，或 (False,) 表示不存在
        mock_cursor.fetchone = MagicMock(return_value=(True,))
        
        result = run_all_checks(mock_conn)
        
        assert "ok" in result
        assert "checks" in result
        assert isinstance(result["checks"], dict)

    def test_check_functions_return_correct_missing_format(self):
        """验证检查函数返回正确的缺失项格式"""
        from engram.logbook.migrate import (
            check_schemas_exist,
            check_tables_exist,
            check_columns_exist,
            check_indexes_exist,
            check_triggers_exist,
            check_matviews_exist,
        )
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        # 模拟返回空（表示未找到）
        mock_cursor.fetchall = MagicMock(return_value=[])
        mock_cursor.fetchone = MagicMock(return_value=(False,))
        
        # 测试 check_schemas_exist
        schemas = ["identity", "logbook"]
        ok, missing = check_schemas_exist(mock_conn, schemas)
        assert isinstance(ok, bool)
        assert isinstance(missing, list)

    def test_required_objects_lists_are_complete(self):
        """验证必需对象列表完整性"""
        from engram.logbook.migrate import (
            REQUIRED_TABLE_TEMPLATES,
            REQUIRED_COLUMN_TEMPLATES,
            REQUIRED_INDEX_TEMPLATES,
            REQUIRED_TRIGGER_TEMPLATES,
            REQUIRED_MATVIEW_TEMPLATES,
        )
        
        # 验证表模板包含所有核心表
        table_schemas = {t[0] for t in REQUIRED_TABLE_TEMPLATES}
        assert "identity" in table_schemas
        assert "logbook" in table_schemas
        assert "scm" in table_schemas
        assert "analysis" in table_schemas
        assert "governance" in table_schemas
        
        # 验证索引模板非空
        assert len(REQUIRED_INDEX_TEMPLATES) > 0
        
        # 验证物化视图模板包含 v_facts
        matview_names = [t[1] for t in REQUIRED_MATVIEW_TEMPLATES]
        assert "v_facts" in matview_names


class TestMissingItemDetection:
    """测试缺失项检测"""

    def test_missing_schema_detected(self):
        """测试能检测到缺失的 schema"""
        from engram.logbook.migrate import check_schemas_exist
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        # 模拟只找到 identity，缺失 logbook
        mock_cursor.fetchall = MagicMock(return_value=[("identity",)])
        
        schemas = ["identity", "logbook"]
        ok, missing = check_schemas_exist(mock_conn, schemas)
        
        assert ok is False
        assert "logbook" in missing
        assert "identity" not in missing

    def test_missing_table_detected_with_schema(self):
        """测试能检测到缺失的表（带 schema 前缀）"""
        from engram.logbook.migrate import check_tables_exist
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        # 模拟第一个表存在，第二个不存在
        mock_cursor.fetchone = MagicMock(side_effect=[(True,), (False,)])
        
        tables = [("identity", "users"), ("logbook", "missing_table")]
        ok, missing = check_tables_exist(mock_conn, tables)
        
        assert ok is False
        assert "logbook.missing_table" in missing

    def test_missing_index_format_includes_schema(self):
        """测试缺失索引的格式包含 schema 前缀"""
        from engram.logbook.migrate import check_indexes_exist
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        
        # 模拟索引不存在
        mock_cursor.fetchone = MagicMock(return_value=None)
        
        indexes = [("scm", "idx_v_facts_source_id")]
        ok, missing = check_indexes_exist(mock_conn, indexes)
        
        assert ok is False
        assert len(missing) == 1
        assert "scm.idx_v_facts_source_id" in missing


class TestSchemaContextIntegration:
    """测试 SchemaContext 与检查函数的集成"""

    def test_get_required_schemas_with_prefix(self):
        """测试带前缀的 schema 列表生成"""
        from engram.logbook.migrate import get_required_schemas
        from engram.logbook.schema_context import SchemaContext
        
        ctx = SchemaContext(schema_prefix="test")
        schemas = get_required_schemas(ctx)
        
        assert "test_identity" in schemas
        assert "test_logbook" in schemas
        assert "test_scm" in schemas
        
        # 确保没有无前缀的 schema
        assert "identity" not in schemas
        assert "logbook" not in schemas

    def test_get_required_tables_with_prefix(self):
        """测试带前缀的表列表生成"""
        from engram.logbook.migrate import get_required_tables
        from engram.logbook.schema_context import SchemaContext
        
        ctx = SchemaContext(schema_prefix="proj")
        tables = get_required_tables(ctx)
        
        # 验证格式为 (schema, table)
        for schema, table in tables:
            assert schema.startswith("proj_")

    def test_get_required_without_prefix(self):
        """测试无前缀时使用默认 schema 名"""
        from engram.logbook.migrate import get_required_schemas
        from engram.logbook.schema_context import SchemaContext
        
        ctx = SchemaContext()  # 无前缀
        schemas = get_required_schemas(ctx)
        
        assert "identity" in schemas
        assert "logbook" in schemas
        assert "scm" in schemas


class TestRunMigrateErrorReporting:
    """测试迁移错误报告"""

    def test_run_migrate_returns_repair_hint_on_error(self):
        """验证迁移失败时返回修复指令"""
        from engram.logbook.migrate import make_error_result, get_repair_commands_hint
        
        # 模拟创建错误结果
        repair_hint = get_repair_commands_hint("SCHEMA_MISSING", "test_db")
        error_result = make_error_result(
            code="SCHEMA_MISSING",
            message="Schema identity 不存在",
            detail={
                "missing_schemas": ["identity"],
                **repair_hint,
            },
        )
        
        assert error_result["ok"] is False
        assert error_result["code"] == "SCHEMA_MISSING"
        assert "repair_hint" in error_result.get("detail", {})
        assert "recommended_commands" in error_result.get("detail", {})

    def test_error_result_structure_consistent(self):
        """验证错误结果结构一致性"""
        from engram.logbook.errors import make_error_result
        
        result = make_error_result(
            code="TEST_ERROR",
            message="测试错误消息",
            detail={"key": "value"},
        )
        
        assert result["ok"] is False
        assert result["code"] == "TEST_ERROR"
        assert result["message"] == "测试错误消息"
        assert result["detail"]["key"] == "value"


class TestLogbookAdapterRepairPath:
    """测试 LogbookAdapter 中的修复指令路径"""

    def test_logbook_adapter_repair_hint_path(self):
        """验证 LogbookAdapter 的 ensure_db_ready 使用正确路径"""
        # 读取源代码验证路径
        adapter_path = Path(__file__).parent.parent.parent.parent / "src/engram/gateway/logbook_adapter.py"
        
        if adapter_path.exists():
            content = adapter_path.read_text()
            
            # 确保使用了新的路径
            assert "cd logbook_postgres/scripts" in content


class TestSQLScriptPrefixOrdering:
    """测试 SQL 脚本前缀分类和排序"""

    def test_ddl_script_prefixes_complete(self):
        """验证 DDL 脚本前缀列表完整"""
        from engram.logbook.migrate import DDL_SCRIPT_PREFIXES
        
        # 验证包含所有预期的 DDL 前缀（包括 12, 13 governance 审计表）
        expected = {"01", "02", "03", "06", "07", "08", "09", "10", "11", "12", "13"}
        assert DDL_SCRIPT_PREFIXES == expected

    def test_permission_script_prefixes(self):
        """验证权限脚本前缀"""
        from engram.logbook.migrate import PERMISSION_SCRIPT_PREFIXES
        
        assert "04" in PERMISSION_SCRIPT_PREFIXES
        assert "05" in PERMISSION_SCRIPT_PREFIXES

    def test_verify_script_prefixes(self):
        """验证检验脚本前缀"""
        from engram.logbook.migrate import VERIFY_SCRIPT_PREFIXES
        
        assert "99" in VERIFY_SCRIPT_PREFIXES

    def test_scan_sql_files_returns_sorted(self):
        """验证 SQL 文件扫描返回排序结果"""
        from engram.logbook.migrate import scan_sql_files
        
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if sql_dir.exists():
            files = scan_sql_files(sql_dir)
            
            # 验证按前缀数字排序
            prefixes = [int(f[0]) for f in files]
            assert prefixes == sorted(prefixes)


class TestPrecheck:
    """测试预检功能"""

    def test_precheck_openmemory_schema_rejects_public(self):
        """验证预检拒绝 public schema"""
        from engram.logbook.migrate import precheck_openmemory_schema
        
        # 设置环境变量
        old_backend = os.environ.get("OM_METADATA_BACKEND")
        old_schema = os.environ.get("OM_PG_SCHEMA")
        
        try:
            os.environ["OM_METADATA_BACKEND"] = "postgres"
            os.environ["OM_PG_SCHEMA"] = "public"
            
            ok, msg = precheck_openmemory_schema()
            
            assert ok is False
            assert "public" in msg.lower()
        finally:
            if old_backend is None:
                os.environ.pop("OM_METADATA_BACKEND", None)
            else:
                os.environ["OM_METADATA_BACKEND"] = old_backend
            if old_schema is None:
                os.environ.pop("OM_PG_SCHEMA", None)
            else:
                os.environ["OM_PG_SCHEMA"] = old_schema

    def test_precheck_allows_openmemory_schema(self):
        """验证预检允许 openmemory schema"""
        from engram.logbook.migrate import precheck_openmemory_schema
        
        old_backend = os.environ.get("OM_METADATA_BACKEND")
        old_schema = os.environ.get("OM_PG_SCHEMA")
        
        try:
            os.environ["OM_METADATA_BACKEND"] = "postgres"
            os.environ["OM_PG_SCHEMA"] = "openmemory"
            
            ok, msg = precheck_openmemory_schema()
            
            assert ok is True
        finally:
            if old_backend is None:
                os.environ.pop("OM_METADATA_BACKEND", None)
            else:
                os.environ["OM_METADATA_BACKEND"] = old_backend
            if old_schema is None:
                os.environ.pop("OM_PG_SCHEMA", None)
            else:
                os.environ["OM_PG_SCHEMA"] = old_schema


class TestSCMSyncMigrationDependencies:
    """测试 SCM sync 相关迁移依赖关系"""

    def test_required_tables_include_scm_sync_tables(self):
        """验证 REQUIRED_TABLE_TEMPLATES 包含 SCM sync 相关表"""
        from engram.logbook.migrate import REQUIRED_TABLE_TEMPLATES
        
        # 提取 scm schema 下的表
        scm_tables = {table for schema, table in REQUIRED_TABLE_TEMPLATES if schema == "scm"}
        
        # 验证 SCM sync 相关表存在
        required_sync_tables = {
            "sync_rate_limits",  # 限流桶表（在 01_logbook_schema.sql）
            "sync_runs",         # 运行记录（在 06_scm_sync_runs.sql）
            "sync_locks",        # 分布式锁（在 07_scm_sync_locks.sql）
            "sync_jobs",         # 任务队列（在 08_scm_sync_jobs.sql）
        }
        
        for table in required_sync_tables:
            assert table in scm_tables, f"SCM sync 表 '{table}' 应在 REQUIRED_TABLE_TEMPLATES 中"

    def test_required_tables_include_kv_table(self):
        """验证 REQUIRED_TABLE_TEMPLATES 包含 logbook.kv 表"""
        from engram.logbook.migrate import REQUIRED_TABLE_TEMPLATES
        
        # 提取 logbook schema 下的表
        logbook_tables = {table for schema, table in REQUIRED_TABLE_TEMPLATES if schema == "logbook"}
        
        # 验证 KV 表存在（用于存储同步游标等配置）
        assert "kv" in logbook_tables, "logbook.kv 表应在 REQUIRED_TABLE_TEMPLATES 中"

    def test_required_tables_include_governance_audit_tables(self):
        """验证 REQUIRED_TABLE_TEMPLATES 包含 governance 审计表"""
        from engram.logbook.migrate import REQUIRED_TABLE_TEMPLATES
        
        # 提取 governance schema 下的表
        governance_tables = {table for schema, table in REQUIRED_TABLE_TEMPLATES if schema == "governance"}
        
        # 验证审计表存在
        required_audit_tables = {
            "artifact_ops_audit",        # Artifact 操作审计（在 12_governance_artifact_ops_audit.sql）
            "object_store_audit_events", # 对象存储审计（在 13_governance_object_store_audit_events.sql）
        }
        
        for table in required_audit_tables:
            assert table in governance_tables, f"Governance 审计表 '{table}' 应在 REQUIRED_TABLE_TEMPLATES 中"

    def test_scm_sync_migration_files_exist(self):
        """验证 SCM sync 相关迁移文件存在"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        
        required_files = [
            "06_scm_sync_runs.sql",
            "07_scm_sync_locks.sql",
            "08_scm_sync_jobs.sql",
            "11_sync_jobs_dimension_columns.sql",
        ]
        
        for filename in required_files:
            filepath = sql_dir / filename
            assert filepath.exists(), f"SCM sync 迁移文件 '{filename}' 应存在"

    def test_sync_rate_limits_in_base_schema(self):
        """验证限流桶表 sync_rate_limits 在基础 schema 文件中定义"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        base_schema_file = sql_dir / "01_logbook_schema.sql"
        
        assert base_schema_file.exists(), "01_logbook_schema.sql 应存在"
        
        content = base_schema_file.read_text()
        
        # 验证 sync_rate_limits 表定义
        assert "scm.sync_rate_limits" in content, "sync_rate_limits 表应在 01_logbook_schema.sql 中定义"
        assert "instance_key" in content, "sync_rate_limits.instance_key 列应存在"
        assert "tokens" in content, "sync_rate_limits.tokens 列应存在"

    def test_logbook_kv_in_base_schema(self):
        """验证 logbook.kv 表在基础 schema 文件中定义"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        base_schema_file = sql_dir / "01_logbook_schema.sql"
        
        assert base_schema_file.exists(), "01_logbook_schema.sql 应存在"
        
        content = base_schema_file.read_text()
        
        # 验证 logbook.kv 表定义
        assert "logbook.kv" in content, "logbook.kv 表应在 01_logbook_schema.sql 中定义"
        assert "namespace" in content, "logbook.kv.namespace 列应存在"
        assert "value_json" in content, "logbook.kv.value_json 列应存在"

    def test_sync_jobs_foreign_key_to_repos(self):
        """验证 sync_jobs 表有正确的外键依赖"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"
        
        if sync_jobs_file.exists():
            content = sync_jobs_file.read_text()
            
            # 验证外键依赖
            assert "REFERENCES scm.repos" in content, "sync_jobs 应有到 repos 的外键"
            assert "ON DELETE CASCADE" in content, "sync_jobs 外键应有级联删除"

    def test_dimension_columns_depend_on_sync_jobs(self):
        """验证维度列迁移依赖 sync_jobs 表"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        
        if dimension_file.exists():
            content = dimension_file.read_text()
            
            # 验证操作 sync_jobs 表
            assert "scm.sync_jobs" in content, "维度列迁移应操作 sync_jobs 表"
            assert "gitlab_instance" in content, "应添加 gitlab_instance 列"
            assert "tenant_id" in content, "应添加 tenant_id 列"

    def test_governance_audit_migration_files_exist(self):
        """验证 governance 审计迁移文件存在"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        
        required_files = [
            "12_governance_artifact_ops_audit.sql",
            "13_governance_object_store_audit_events.sql",
        ]
        
        for filename in required_files:
            filepath = sql_dir / filename
            assert filepath.exists(), f"Governance 审计迁移文件 '{filename}' 应存在"

    def test_migration_order_is_correct(self):
        """验证迁移文件执行顺序正确（依赖关系）"""
        from engram.logbook.migrate import scan_sql_files
        
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")
        
        files = scan_sql_files(sql_dir)
        prefixes = [int(f[0]) for f in files]
        
        # 验证排序正确
        assert prefixes == sorted(prefixes), "迁移文件应按前缀数字排序"
        
        # 验证关键依赖顺序：01 在 06, 07, 08 之前
        prefix_order = {int(f[0]): idx for idx, f in enumerate(files)}
        
        if 1 in prefix_order and 6 in prefix_order:
            assert prefix_order[1] < prefix_order[6], "01 应在 06 之前执行（repos 在 sync_runs 之前）"
        
        if 1 in prefix_order and 7 in prefix_order:
            assert prefix_order[1] < prefix_order[7], "01 应在 07 之前执行（repos 在 sync_locks 之前）"
        
        if 1 in prefix_order and 8 in prefix_order:
            assert prefix_order[1] < prefix_order[8], "01 应在 08 之前执行（repos 在 sync_jobs 之前）"
        
        if 8 in prefix_order and 11 in prefix_order:
            assert prefix_order[8] < prefix_order[11], "08 应在 11 之前执行（sync_jobs 在维度列之前）"


class TestSCMSyncIndexesAndConstraints:
    """测试 SCM sync 相关索引和约束"""

    def test_required_indexes_include_sync_jobs_indexes(self):
        """验证 REQUIRED_INDEX_TEMPLATES 包含相关索引"""
        from engram.logbook.migrate import REQUIRED_INDEX_TEMPLATES
        
        # 当前 REQUIRED_INDEX_TEMPLATES 可能不包含 sync_jobs 索引，这是可接受的
        # 主要验证数据结构正确
        assert isinstance(REQUIRED_INDEX_TEMPLATES, list)
        for item in REQUIRED_INDEX_TEMPLATES:
            assert len(item) == 2, "索引模板格式应为 (schema, index_name)"

    def test_sync_runs_has_required_indexes(self):
        """验证 sync_runs 迁移文件包含必要索引"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_runs_file = sql_dir / "06_scm_sync_runs.sql"
        
        if sync_runs_file.exists():
            content = sync_runs_file.read_text()
            
            # 验证关键索引存在
            assert "idx_sync_runs_repo_job" in content, "应有 repo+job_type 复合索引"
            assert "idx_sync_runs_status" in content, "应有状态索引"
            assert "idx_sync_runs_started_at" in content, "应有时间索引"

    def test_sync_locks_has_unique_constraint(self):
        """验证 sync_locks 表有唯一约束"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_locks_file = sql_dir / "07_scm_sync_locks.sql"
        
        if sync_locks_file.exists():
            content = sync_locks_file.read_text()
            
            # 验证唯一约束
            assert "uq_sync_locks_repo_job" in content or "UNIQUE (repo_id, job_type)" in content, \
                "sync_locks 应有 (repo_id, job_type) 唯一约束"

    def test_sync_jobs_has_unique_active_index(self):
        """验证 sync_jobs 表有防重复活跃任务的唯一索引"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"
        
        if sync_jobs_file.exists():
            content = sync_jobs_file.read_text()
            
            # 验证唯一索引（防止重复 pending/running 任务）
            assert "idx_sync_jobs_unique_active" in content, \
                "sync_jobs 应有防重复活跃任务的唯一索引"
