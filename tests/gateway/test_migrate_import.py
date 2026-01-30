"""
migrate 模块导入测试

验证 engram_logbook.migrate 模块可以被正确导入，
确保在容器与本地环境均可执行。

测试覆盖:
- engram_logbook.migrate 模块导入
- run_all_checks / run_migrate 函数可调用
- logbook_adapter 不再使用 sys.path 注入
- Gateway 启动期 DB check 路径
"""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestMigrateModuleImport:
    """engram_logbook.migrate 模块导入测试"""

    def test_import_migrate_module(self):
        """测试 engram_logbook.migrate 模块可以正常导入"""
        # 确保 sys.path 中包含 logbook scripts 目录
        scripts_dir = Path(__file__).parent.parent.parent.parent / "logbook_postgres" / "scripts"
        if scripts_dir.exists() and str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        # 导入模块
        from engram.logbook import migrate
        
        # 验证核心函数存在
        assert hasattr(migrate, 'run_all_checks')
        assert hasattr(migrate, 'run_migrate')
        assert hasattr(migrate, 'run_precheck')
        assert callable(migrate.run_all_checks)
        assert callable(migrate.run_migrate)
        assert callable(migrate.run_precheck)

    def test_import_run_all_checks(self):
        """测试 run_all_checks 函数可以正常导入"""
        from engram.logbook.migrate import run_all_checks
        assert callable(run_all_checks)

    def test_import_run_migrate(self):
        """测试 run_migrate 函数可以正常导入"""
        from engram.logbook.migrate import run_migrate
        assert callable(run_migrate)

    def test_import_helper_functions(self):
        """测试辅助函数可以正常导入"""
        from engram.logbook.migrate import (
            is_testing_mode,
            get_repair_commands_hint,
            check_schemas_exist,
            check_tables_exist,
            check_columns_exist,
            check_indexes_exist,
            check_triggers_exist,
            check_matviews_exist,
        )
        assert callable(is_testing_mode)
        assert callable(get_repair_commands_hint)
        assert callable(check_schemas_exist)
        assert callable(check_tables_exist)

    def test_import_constants(self):
        """测试常量可以正常导入"""
        from engram.logbook.migrate import (
            DEFAULT_SCHEMA_SUFFIXES,
            REQUIRED_TABLE_TEMPLATES,
            REQUIRED_COLUMN_TEMPLATES,
            REQUIRED_INDEX_TEMPLATES,
        )
        assert isinstance(DEFAULT_SCHEMA_SUFFIXES, list)
        assert len(DEFAULT_SCHEMA_SUFFIXES) == 5
        assert "identity" in DEFAULT_SCHEMA_SUFFIXES
        assert "logbook" in DEFAULT_SCHEMA_SUFFIXES
        assert "governance" in DEFAULT_SCHEMA_SUFFIXES


class TestLogbookAdapterNoSysPathInjection:
    """验证 logbook_adapter 不再使用 sys.path 注入"""

    def test_no_sys_path_manipulation_in_adapter(self):
        """验证 logbook_adapter 不包含 sys.path 操作"""
        from engram.gateway import logbook_adapter
        import inspect
        
        # 获取模块源代码
        source = inspect.getsource(logbook_adapter)
        
        # 验证不再包含 sys.path.insert 操作
        assert "sys.path.insert(0, str(_scripts_dir))" not in source
        # 验证不再有 _scripts_dir 变量
        assert "_scripts_dir = Path(__file__)" not in source

    def test_adapter_imports_from_engram_logbook(self):
        """验证 adapter 从 engram_logbook 导入"""
        from engram.gateway import logbook_adapter
        import inspect
        
        source = inspect.getsource(logbook_adapter)
        
        # 验证从 engram_logbook 导入
        assert "from engram.logbook.migrate import run_all_checks, run_migrate" in source

    def test_db_migrate_available_flag(self):
        """测试 _DB_MIGRATE_AVAILABLE 标志正确设置"""
        from engram.gateway.logbook_adapter import _DB_MIGRATE_AVAILABLE
        # 如果 engram_logbook.migrate 可用，标志应为 True
        # 注意：这依赖于测试环境中 engram_logbook 包可用
        assert isinstance(_DB_MIGRATE_AVAILABLE, bool)


class TestDbMigrateCliBackwardCompatibility:
    """db_migrate.py CLI 向后兼容性测试"""

    def test_db_migrate_imports_from_migrate_module(self):
        """测试 db_migrate.py 从 engram_logbook.migrate 导入"""
        scripts_dir = Path(__file__).parent.parent.parent.parent / "logbook_postgres" / "scripts"
        db_migrate_path = scripts_dir / "db_migrate.py"
        
        assert db_migrate_path.exists(), f"db_migrate.py 不存在: {db_migrate_path}"
        
        # 读取源代码
        source = db_migrate_path.read_text()
        
        # 验证导入语句
        assert "from engram.logbook.migrate import" in source
        assert "run_migrate" in source
        assert "run_all_checks" in source

    def test_db_migrate_exports_backward_compatible_names(self):
        """测试 db_migrate.py 导出向后兼容的名称"""
        scripts_dir = Path(__file__).parent.parent.parent.parent / "logbook_postgres" / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        
        import db_migrate
        
        # 验证可以从 db_migrate 访问这些函数（向后兼容）
        assert hasattr(db_migrate, 'run_migrate')
        assert hasattr(db_migrate, 'run_all_checks')
        assert hasattr(db_migrate, 'run_precheck')
        assert hasattr(db_migrate, 'check_schemas_exist')
        assert hasattr(db_migrate, 'check_tables_exist')


class TestGatewayDbCheckPath:
    """Gateway 启动期 DB check 路径测试"""

    def setup_method(self):
        """每个测试前重置状态"""
        from engram.gateway import logbook_adapter
        from engram.gateway import config as config_module
        logbook_adapter._adapter_instance = None
        config_module.reset_config()

    def teardown_method(self):
        """每个测试后清理"""
        from engram.gateway import logbook_adapter
        from engram.gateway import config as config_module
        logbook_adapter._adapter_instance = None
        config_module.reset_config()

    def test_check_db_schema_uses_migrate_module(self):
        """测试 check_db_schema 使用 engram_logbook.migrate 模块"""
        from engram.gateway.logbook_adapter import LogbookAdapter
        
        # Mock run_all_checks
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": True,
                "checks": {
                    "schemas": {"ok": True, "missing": []},
                    "tables": {"ok": True, "missing": []},
                    "columns": {"ok": True, "missing": []},
                    "indexes": {"ok": True, "missing": []},
                    "triggers": {"ok": True, "missing": []},
                    "matviews": {"ok": True, "missing": []},
                }
            }
            
            with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                mock_conn_instance = MagicMock()
                mock_conn.return_value = mock_conn_instance
                
                with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                    adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                    result = adapter.check_db_schema()
                    
                    # 验证调用了 run_all_checks
                    mock_check.assert_called_once()
                    assert result.ok is True

    def test_run_migration_uses_migrate_module(self):
        """测试 run_migration 使用 engram_logbook.migrate 模块"""
        from engram.gateway.logbook_adapter import LogbookAdapter
        
        with patch('engram.gateway.logbook_adapter.run_migrate') as mock_migrate:
            mock_migrate.return_value = {
                "ok": True,
                "schemas": ["identity", "logbook", "scm", "analysis", "governance"],
            }
            
            with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                result = adapter.run_migration(quiet=True)
                
                # 验证调用了 run_migrate
                mock_migrate.assert_called_once()
                assert result["ok"] is True

    def test_ensure_db_ready_with_auto_migrate(self):
        """测试 ensure_db_ready 使用自动迁移"""
        from engram.gateway.logbook_adapter import LogbookAdapter
        
        call_count = [0]
        
        def mock_check_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"ok": False, "checks": {"schemas": {"ok": False, "missing": ["governance"]}}}
            else:
                return {"ok": True, "checks": {"schemas": {"ok": True, "missing": []}}}
        
        with patch('engram.gateway.logbook_adapter.run_all_checks', side_effect=mock_check_side_effect):
            with patch('engram.gateway.logbook_adapter.run_migrate') as mock_migrate:
                mock_migrate.return_value = {"ok": True}
                
                with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                    mock_conn_instance = MagicMock()
                    mock_conn.return_value = mock_conn_instance
                    
                    with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                        adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                        result = adapter.ensure_db_ready(auto_migrate=True)
                        
                        # 验证迁移被调用
                        mock_migrate.assert_called_once()
                        assert result.ok is True

    def test_main_check_logbook_db_on_startup(self):
        """测试 main.py 的 check_logbook_db_on_startup 函数"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        from engram.gateway.logbook_adapter import LogbookDBCheckResult
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=True,
            auto_migrate_on_startup=False,
        )
        
        with patch('engram.gateway.main.is_db_migrate_available', return_value=True):
            with patch('engram.gateway.main.ensure_db_ready') as mock_ensure:
                mock_ensure.return_value = LogbookDBCheckResult(
                    ok=True,
                    checks={"schemas": {"ok": True, "missing": []}},
                )
                
                result = check_logbook_db_on_startup(config)
                
                # 验证检查被调用
                mock_ensure.assert_called_once()
                assert result is True


class TestMigrateModuleFunctionSignatures:
    """验证 migrate 模块函数签名正确"""

    def test_run_all_checks_signature(self):
        """测试 run_all_checks 函数签名"""
        from engram.logbook.migrate import run_all_checks
        import inspect
        
        sig = inspect.signature(run_all_checks)
        params = list(sig.parameters.keys())
        
        # 验证必需参数
        assert 'conn' in params
        # 验证可选参数
        assert 'schema_context' in params
        assert 'schema_map' in params
        assert 'check_openmemory_schema' in params

    def test_run_migrate_signature(self):
        """测试 run_migrate 函数签名"""
        from engram.logbook.migrate import run_migrate
        import inspect
        
        sig = inspect.signature(run_migrate)
        params = list(sig.parameters.keys())
        
        # 验证参数
        assert 'config_path' in params
        assert 'quiet' in params
        assert 'dsn' in params
        assert 'schema_prefix' in params
        assert 'apply_roles' in params
        assert 'apply_openmemory_grants' in params
        assert 'verify' in params

    def test_run_precheck_signature(self):
        """测试 run_precheck 函数签名"""
        from engram.logbook.migrate import run_precheck
        import inspect
        
        sig = inspect.signature(run_precheck)
        params = list(sig.parameters.keys())
        
        # 验证参数
        assert 'quiet' in params


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
