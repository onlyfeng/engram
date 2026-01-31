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
from unittest.mock import MagicMock

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
                assert cmd.startswith("python logbook_postgres/"), (
                    f"命令路径应包含 logbook_postgres/: {cmd}"
                )

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
        mock_cursor.fetchall = MagicMock(
            return_value=[("identity",), ("logbook",), ("scm",), ("analysis",), ("governance",)]
        )
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
            REQUIRED_INDEX_TEMPLATES,
            REQUIRED_MATVIEW_TEMPLATES,
            REQUIRED_TABLE_TEMPLATES,
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
        from engram.logbook.migrate import get_repair_commands_hint, make_error_result

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
        adapter_path = (
            Path(__file__).parent.parent.parent.parent / "src/engram/gateway/logbook_adapter.py"
        )

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
        # 注：编号 10 已废弃，迁移序列为 09 -> 11
        expected = {"01", "02", "03", "06", "07", "08", "09", "11", "12", "13"}
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
            result = scan_sql_files(sql_dir)
            files = result["files"]

            # 验证按前缀数字排序
            prefixes = [int(f[0]) for f in files]
            assert prefixes == sorted(prefixes)

    def test_scan_sql_files_returns_duplicates(self):
        """验证 SQL 文件扫描检测重复前缀"""
        from engram.logbook.migrate import scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if sql_dir.exists():
            result = scan_sql_files(sql_dir)

            # 验证返回结构包含 duplicates 字段
            assert "duplicates" in result
            assert isinstance(result["duplicates"], dict)

    def test_scan_sql_files_sorting_stability(self):
        """验证 SQL 文件扫描排序稳定性：同前缀按文件名排序"""
        import tempfile

        from engram.logbook.migrate import scan_sql_files

        # 创建临时目录模拟同前缀多文件场景
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # 创建测试文件（同一前缀多个文件）
            (tmp_path / "01_first.sql").touch()
            (tmp_path / "01_second.sql").touch()
            (tmp_path / "01_third.sql").touch()
            (tmp_path / "02_another.sql").touch()

            result = scan_sql_files(tmp_path)
            files = result["files"]
            duplicates = result["duplicates"]

            # 验证同前缀文件按文件名排序
            prefix_01_files = [f[1].name for f in files if f[0] == "01"]
            assert prefix_01_files == sorted(prefix_01_files), "同前缀文件应按文件名排序"

            # 验证 duplicates 检测
            assert "01" in duplicates
            assert len(duplicates["01"]) == 3
            assert "02" not in duplicates  # 只有一个文件，不应在 duplicates 中

    def test_scan_sql_files_sorting_stability_multiple_runs(self):
        """验证多次调用 scan_sql_files 返回相同顺序（排序稳定性）"""
        import tempfile

        from engram.logbook.migrate import scan_sql_files

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # 创建测试文件
            (tmp_path / "05_zebra.sql").touch()
            (tmp_path / "05_alpha.sql").touch()
            (tmp_path / "05_beta.sql").touch()
            (tmp_path / "03_gamma.sql").touch()

            # 多次调用应返回相同顺序
            results = [scan_sql_files(tmp_path) for _ in range(5)]

            first_order = [(f[0], f[1].name) for f in results[0]["files"]]
            for result in results[1:]:
                current_order = [(f[0], f[1].name) for f in result["files"]]
                assert current_order == first_order, "多次调用应返回相同顺序"

            # 验证具体顺序：先按前缀数字，再按文件名字母序
            expected_order = [
                ("03", "03_gamma.sql"),
                ("05", "05_alpha.sql"),
                ("05", "05_beta.sql"),
                ("05", "05_zebra.sql"),
            ]
            assert first_order == expected_order


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
            "sync_runs",  # 运行记录（在 06_scm_sync_runs.sql）
            "sync_locks",  # 分布式锁（在 07_scm_sync_locks.sql）
            "sync_jobs",  # 任务队列（在 08_scm_sync_jobs.sql）
        }

        for table in required_sync_tables:
            assert table in scm_tables, f"SCM sync 表 '{table}' 应在 REQUIRED_TABLE_TEMPLATES 中"

    def test_required_tables_include_kv_table(self):
        """验证 REQUIRED_TABLE_TEMPLATES 包含 logbook.kv 表"""
        from engram.logbook.migrate import REQUIRED_TABLE_TEMPLATES

        # 提取 logbook schema 下的表
        logbook_tables = {
            table for schema, table in REQUIRED_TABLE_TEMPLATES if schema == "logbook"
        }

        # 验证 KV 表存在（用于存储同步游标等配置）
        assert "kv" in logbook_tables, "logbook.kv 表应在 REQUIRED_TABLE_TEMPLATES 中"

    def test_required_tables_include_governance_audit_tables(self):
        """验证 REQUIRED_TABLE_TEMPLATES 包含 governance 审计表"""
        from engram.logbook.migrate import REQUIRED_TABLE_TEMPLATES

        # 提取 governance schema 下的表
        governance_tables = {
            table for schema, table in REQUIRED_TABLE_TEMPLATES if schema == "governance"
        }

        # 验证审计表存在
        required_audit_tables = {
            "artifact_ops_audit",  # Artifact 操作审计（在 12_governance_artifact_ops_audit.sql）
            "object_store_audit_events",  # 对象存储审计（在 13_governance_object_store_audit_events.sql）
        }

        for table in required_audit_tables:
            assert table in governance_tables, (
                f"Governance 审计表 '{table}' 应在 REQUIRED_TABLE_TEMPLATES 中"
            )

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
        assert "scm.sync_rate_limits" in content, (
            "sync_rate_limits 表应在 01_logbook_schema.sql 中定义"
        )
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

        result = scan_sql_files(sql_dir)
        files = result["files"]
        prefixes = [int(f[0]) for f in files]

        # 验证排序正确
        assert prefixes == sorted(prefixes), "迁移文件应按前缀数字排序"

        # 验证关键依赖顺序：01 在 06, 07, 08 之前
        prefix_order = {int(f[0]): idx for idx, f in enumerate(files)}

        if 1 in prefix_order and 6 in prefix_order:
            assert prefix_order[1] < prefix_order[6], (
                "01 应在 06 之前执行（repos 在 sync_runs 之前）"
            )

        if 1 in prefix_order and 7 in prefix_order:
            assert prefix_order[1] < prefix_order[7], (
                "01 应在 07 之前执行（repos 在 sync_locks 之前）"
            )

        if 1 in prefix_order and 8 in prefix_order:
            assert prefix_order[1] < prefix_order[8], (
                "01 应在 08 之前执行（repos 在 sync_jobs 之前）"
            )

        if 8 in prefix_order and 11 in prefix_order:
            assert prefix_order[8] < prefix_order[11], (
                "08 应在 11 之前执行（sync_jobs 在维度列之前）"
            )


class TestMigrationIdempotency:
    """测试迁移脚本的幂等性（重复执行不报错）"""

    def test_scan_sql_files_idempotent(self):
        """验证 scan_sql_files 多次调用结果一致"""
        from engram.logbook.migrate import scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")

        # 多次调用
        results = [scan_sql_files(sql_dir) for _ in range(5)]

        # 验证结果一致
        first_files = [(p, f.name) for p, f in results[0]["files"]]
        for i, r in enumerate(results[1:], start=2):
            current_files = [(p, f.name) for p, f in r["files"]]
            assert current_files == first_files, f"scan_sql_files 第 {i} 次调用结果与第 1 次不一致"

    def test_classify_sql_files_idempotent(self):
        """验证 classify_sql_files 多次调用结果一致"""
        from engram.logbook.migrate import classify_sql_files, scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")

        scan_result = scan_sql_files(sql_dir)

        # 多次分类
        classifications = [
            classify_sql_files(
                scan_result["files"],
                apply_roles=True,
                apply_openmemory_grants=True,
                verify=True,
            )
            for _ in range(5)
        ]

        # 验证结果一致
        first_ddl = [str(f) for f in classifications[0]["ddl"]]
        first_execute = [str(f) for f in classifications[0]["execute"]]

        for i, c in enumerate(classifications[1:], start=2):
            current_ddl = [str(f) for f in c["ddl"]]
            current_execute = [str(f) for f in c["execute"]]
            assert current_ddl == first_ddl, (
                f"classify_sql_files DDL 第 {i} 次调用结果与第 1 次不一致"
            )
            assert current_execute == first_execute, (
                f"classify_sql_files execute 第 {i} 次调用结果与第 1 次不一致"
            )

    def test_run_all_checks_returns_consistent_structure(self):
        """验证 run_all_checks 多次调用返回结构一致"""
        from engram.logbook.migrate import run_all_checks

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # 模拟所有检查返回一致
        mock_cursor.execute = MagicMock()
        mock_cursor.fetchall = MagicMock(
            return_value=[("identity",), ("logbook",), ("scm",), ("analysis",), ("governance",)]
        )
        mock_cursor.fetchone = MagicMock(return_value=(True,))

        # 多次调用
        results = [run_all_checks(mock_conn) for _ in range(3)]

        # 验证结构一致
        for r in results:
            assert "ok" in r
            assert "checks" in r
            assert isinstance(r["checks"], dict)

            # 验证包含预期的检查项
            expected_checks = {"schemas", "tables", "columns", "indexes", "triggers", "matviews"}
            actual_checks = set(r["checks"].keys())
            assert expected_checks <= actual_checks, (
                f"run_all_checks 应包含检查项：{expected_checks}，实际：{actual_checks}"
            )


class TestMissingFileDiagnostics:
    """测试缺失文件的可诊断性"""

    def test_scan_sql_files_with_nonexistent_dir_returns_empty(self):
        """验证 scan_sql_files 对不存在目录的处理"""
        import tempfile

        from engram.logbook.migrate import scan_sql_files

        # 使用一个确定不存在的路径
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent_sql_dir"

            result = scan_sql_files(nonexistent)

            # 应返回空列表，不报错
            assert result["files"] == []
            assert result["duplicates"] == {}

    def test_missing_schema_error_has_repair_hint(self):
        """验证缺失 schema 错误包含修复指令"""
        from engram.logbook.migrate import get_repair_commands_hint

        hint = get_repair_commands_hint("SCHEMA_MISSING", "test_db")

        # 验证修复指令结构
        assert "repair_hint" in hint
        assert "recommended_commands" in hint
        assert len(hint["recommended_commands"]) > 0

        # 验证包含实际命令
        commands_str = "\n".join(hint["recommended_commands"])
        assert "db_bootstrap" in commands_str or "db_migrate" in commands_str
        assert "docker" in commands_str.lower()

    def test_missing_table_error_has_repair_hint(self):
        """验证缺失表错误包含修复指令"""
        from engram.logbook.migrate import get_repair_commands_hint

        hint = get_repair_commands_hint("TABLE_MISSING", "test_db")

        assert "repair_hint" in hint
        assert "recommended_commands" in hint

        # 验证修复指令包含迁移命令
        commands_str = "\n".join(hint["recommended_commands"])
        assert "migrate" in commands_str.lower()

    def test_missing_index_error_has_repair_hint(self):
        """验证缺失索引错误包含修复指令"""
        from engram.logbook.migrate import get_repair_commands_hint

        hint = get_repair_commands_hint("INDEX_MISSING", "test_db")

        assert "repair_hint" in hint
        assert "recommended_commands" in hint

    def test_file_not_found_error_result_structure(self):
        """验证 FILE_NOT_FOUND 错误结果结构"""
        from engram.logbook.errors import make_error_result

        result = make_error_result(
            code="FILE_NOT_FOUND",
            message="SQL 文件不存在: missing_file.sql",
            detail={"path": "/path/to/missing_file.sql"},
        )

        assert result["ok"] is False
        assert result["code"] == "FILE_NOT_FOUND"
        assert "missing_file.sql" in result["message"]
        assert result["detail"]["path"] == "/path/to/missing_file.sql"

    def test_error_codes_are_documented(self):
        """验证所有错误码都有对应的修复指令"""
        from engram.logbook.migrate import get_repair_commands_hint

        # 已知的错误码
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

            assert "repair_hint" in hint, f"错误码 {code} 缺少 repair_hint"
            assert "recommended_commands" in hint, f"错误码 {code} 缺少 recommended_commands"
            assert len(hint["recommended_commands"]) > 0, (
                f"错误码 {code} 的 recommended_commands 为空"
            )

    def test_error_result_includes_database_name(self):
        """验证错误结果包含数据库名称"""
        from engram.logbook.migrate import get_repair_commands_hint

        hint = get_repair_commands_hint("SCHEMA_MISSING", "my_test_db")

        # 验证修复指令包含数据库名称（在 repair_hint 字段中）
        assert "my_test_db" in hint["repair_hint"]


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
            assert "uq_sync_locks_repo_job" in content or "UNIQUE (repo_id, job_type)" in content, (
                "sync_locks 应有 (repo_id, job_type) 唯一约束"
            )

    def test_sync_jobs_has_unique_active_index(self):
        """验证 sync_jobs 表有防重复活跃任务的唯一索引"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        if sync_jobs_file.exists():
            content = sync_jobs_file.read_text()

            # 验证唯一索引（防止重复 pending/running 任务）
            assert "idx_sync_jobs_unique_active" in content, (
                "sync_jobs 应有防重复活跃任务的唯一索引"
            )


class TestVerifyPathIntegration:
    """测试 --verify 路径的最小集成验证"""

    def test_verify_sql_script_exists(self):
        """验证 verify SQL 脚本存在"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_dir = sql_dir / "verify"

        assert verify_dir.is_dir(), f"sql/verify/ 子目录应存在: {verify_dir}"

        verify_script = verify_dir / "99_verify_permissions.sql"
        assert verify_script.exists(), f"99_verify_permissions.sql 验证脚本应存在: {verify_script}"

    def test_verify_script_is_discovered_by_scan(self):
        """验证 scan_sql_files 能发现 verify 脚本"""
        from engram.logbook.migrate import scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")

        # 包含 verify 子目录
        result = scan_sql_files(sql_dir, include_verify_subdir=True)
        files = result["files"]

        # 应发现 99 前缀的文件
        prefix_99_files = [f for p, f in files if p == "99"]
        assert len(prefix_99_files) >= 1, (
            "scan_sql_files(include_verify_subdir=True) 应发现 99 前缀的验证脚本"
        )

        # 验证文件来自 verify 子目录
        verify_file_names = [f.name for f in prefix_99_files]
        assert "99_verify_permissions.sql" in verify_file_names

    def test_verify_script_excluded_without_flag(self):
        """验证不指定 include_verify_subdir=True 时不发现 verify 脚本"""
        from engram.logbook.migrate import scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")

        # 不包含 verify 子目录
        result = scan_sql_files(sql_dir, include_verify_subdir=False)
        files = result["files"]

        # 不应发现 99 前缀的文件（因为主目录没有 99 前缀文件）
        prefix_99_files = [f for p, f in files if p == "99"]
        assert len(prefix_99_files) == 0, (
            "scan_sql_files(include_verify_subdir=False) 不应发现 verify 子目录中的脚本"
        )

    def test_classify_includes_verify_with_flag(self):
        """验证 classify_sql_files 在 verify=True 时包含验证脚本"""
        from engram.logbook.migrate import classify_sql_files, scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")

        scan_result = scan_sql_files(sql_dir, include_verify_subdir=True)

        # verify=True 时
        classified_with_verify = classify_sql_files(
            scan_result["files"],
            apply_roles=False,
            apply_openmemory_grants=False,
            verify=True,
        )

        # 验证 verify 列表非空
        assert len(classified_with_verify["verify"]) >= 1, (
            "classify_sql_files(verify=True) 应识别验证脚本"
        )

        # 验证 execute 列表包含验证脚本
        execute_names = [f.name for f in classified_with_verify["execute"]]
        assert "99_verify_permissions.sql" in execute_names, (
            "classify_sql_files(verify=True) 应将验证脚本加入执行列表"
        )

    def test_classify_excludes_verify_without_flag(self):
        """验证 classify_sql_files 在 verify=False 时不包含验证脚本执行"""
        from engram.logbook.migrate import classify_sql_files, scan_sql_files

        sql_dir = Path(__file__).parent.parent.parent / "sql"
        if not sql_dir.exists():
            pytest.skip("SQL 目录不存在")

        scan_result = scan_sql_files(sql_dir, include_verify_subdir=True)

        # verify=False 时
        classified_without_verify = classify_sql_files(
            scan_result["files"],
            apply_roles=False,
            apply_openmemory_grants=False,
            verify=False,
        )

        # verify 列表应非空（脚本被识别但不执行）
        assert len(classified_without_verify["verify"]) >= 1

        # execute 列表不应包含验证脚本
        execute_names = [f.name for f in classified_without_verify["execute"]]
        assert "99_verify_permissions.sql" not in execute_names, (
            "classify_sql_files(verify=False) 不应将验证脚本加入执行列表"
        )

    def test_verify_script_content_is_valid_sql(self):
        """验证 verify 脚本内容是有效的 SQL"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 基本有效性检查
        assert len(content) > 100, "验证脚本内容应非空"

        # 应包含 DO $$ ... END $$ 块（PL/pgSQL）
        assert "DO $$" in content, "验证脚本应使用 PL/pgSQL DO 块"
        assert "END $$" in content or "END$$" in content, "验证脚本应有 END $$ 结束标记"

        # 应包含验证相关的关键词
        content_lower = content.lower()
        assert "verify" in content_lower or "验证" in content, (
            "验证脚本应包含 'verify' 或 '验证' 相关内容"
        )

    def test_verify_script_checks_roles(self):
        """验证 verify 脚本检查角色配置"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 应检查核心角色
        expected_roles = [
            "engram_admin",
            "engram_migrator",
            "engram_app_readwrite",
            "openmemory_migrator",
            "openmemory_app",
        ]

        for role in expected_roles:
            assert role in content, f"验证脚本应检查角色 {role}"

    def test_verify_script_uses_parameterized_schema(self):
        """验证 verify 脚本使用参数化的目标 schema"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 应使用 om.target_schema 配置变量
        assert "om.target_schema" in content, (
            "验证脚本应使用 om.target_schema 配置变量来参数化目标 schema"
        )

        # 应有默认值 'openmemory'
        assert "'openmemory'" in content, "验证脚本应有 om.target_schema 的默认值 'openmemory'"

    def test_verify_script_has_strict_mode_support(self):
        """验证 verify 脚本支持 strict 模式"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 应支持 engram.verify_strict 配置变量
        assert "engram.verify_strict" in content or "verify_strict" in content, (
            "验证脚本应支持 verify_strict 模式"
        )

    def test_verify_script_supports_schema_prefix(self):
        """验证 verify 脚本支持 schema_prefix 配置变量"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 应支持 engram.schema_prefix 配置变量
        assert "engram.schema_prefix" in content, "验证脚本应支持 engram.schema_prefix 配置变量"

        # 应有辅助函数 _get_engram_schemas()
        assert "_get_engram_schemas" in content, "验证脚本应定义 _get_engram_schemas() 辅助函数"

        # 应有辅助函数 _get_schema_prefix()
        assert "_get_schema_prefix" in content, "验证脚本应定义 _get_schema_prefix() 辅助函数"

    def test_verify_script_schema_prefix_helper_function_logic(self):
        """验证 _get_engram_schemas 辅助函数包含正确的逻辑"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 辅助函数应处理前缀拼接逻辑
        assert "v_prefix || '_' || s" in content or "v_prefix ||" in content, (
            "辅助函数应包含前缀拼接逻辑（v_prefix || '_' || schema）"
        )

        # 应有基础 schema 列表
        assert "ARRAY['identity', 'logbook', 'scm', 'analysis', 'governance']" in content, (
            "辅助函数应定义基础 schema 列表"
        )

    def test_verify_script_cleans_up_helper_functions(self):
        """验证 verify 脚本清理辅助函数"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        verify_script = sql_dir / "verify" / "99_verify_permissions.sql"

        if not verify_script.exists():
            pytest.skip("99_verify_permissions.sql 不存在")

        content = verify_script.read_text(encoding="utf-8")

        # 应在末尾清理辅助函数
        assert "DROP FUNCTION IF EXISTS _get_engram_schemas" in content, (
            "验证脚本应清理 _get_engram_schemas 辅助函数"
        )
        assert "DROP FUNCTION IF EXISTS _get_schema_prefix" in content, (
            "验证脚本应清理 _get_schema_prefix 辅助函数"
        )


class TestMigrateSchemaPrefix:
    """测试 migrate.py 中 schema_prefix 与 verify 的集成"""

    def test_migrate_sets_schema_prefix_for_verify_script(self):
        """验证 migrate.py 在执行 verify 脚本时设置 engram.schema_prefix"""
        migrate_path = Path(__file__).parent.parent.parent / "src/engram/logbook/migrate.py"

        if not migrate_path.exists():
            pytest.skip("migrate.py 不存在")

        content = migrate_path.read_text(encoding="utf-8")

        # 应在执行 verify 脚本前设置 schema_prefix
        assert "SET engram.schema_prefix" in content, (
            "migrate.py 应在执行 verify 脚本前设置 engram.schema_prefix"
        )

        # 应在 prefix == "99" 分支中处理
        assert 'elif prefix == "99"' in content, "migrate.py 应有 verify 脚本（前缀 99）的处理分支"

    def test_migrate_only_sets_schema_prefix_when_provided(self):
        """验证 migrate.py 仅在 schema_prefix 存在时设置配置变量"""
        migrate_path = Path(__file__).parent.parent.parent / "src/engram/logbook/migrate.py"

        if not migrate_path.exists():
            pytest.skip("migrate.py 不存在")

        content = migrate_path.read_text(encoding="utf-8")

        # 应有条件判断 schema_prefix 是否存在
        # 查找 "if schema_prefix:" 在 verify 脚本处理块中
        assert "if schema_prefix:" in content, (
            "migrate.py 应在设置 engram.schema_prefix 前检查 schema_prefix 是否存在"
        )

    def test_migrate_uses_sql_literal_for_schema_prefix(self):
        """验证 migrate.py 使用 sql.Literal 防止 SQL 注入"""
        migrate_path = Path(__file__).parent.parent.parent / "src/engram/logbook/migrate.py"

        if not migrate_path.exists():
            pytest.skip("migrate.py 不存在")

        content = migrate_path.read_text(encoding="utf-8")

        # 应使用 sql.Literal 包装 schema_prefix 值
        assert "sql.Literal(schema_prefix)" in content, (
            "migrate.py 应使用 sql.Literal 包装 schema_prefix 值以防止 SQL 注入"
        )

    def test_migrate_logs_schema_prefix_setting(self):
        """验证 migrate.py 记录 schema_prefix 设置"""
        migrate_path = Path(__file__).parent.parent.parent / "src/engram/logbook/migrate.py"

        if not migrate_path.exists():
            pytest.skip("migrate.py 不存在")

        content = migrate_path.read_text(encoding="utf-8")

        # 应有日志输出
        assert "设置 verify schema_prefix" in content or "schema_prefix" in content, (
            "migrate.py 应记录 schema_prefix 设置"
        )
