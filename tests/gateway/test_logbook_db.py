"""
logbook_db 模块单元测试

测试覆盖:
- get_db() 单例策略：不同 DSN 使用不同实例
- reset_db() 重置功能
- set_default_dsn() 默认 DSN 设置
"""

import os
import pytest
from unittest.mock import patch, MagicMock


class TestGetDbSingleton:
    """get_db() 单例策略测试"""

    def setup_method(self):
        """每个测试前重置状态"""
        # 导入并重置模块状态
        from engram.gateway import logbook_db
        logbook_db._db_instances.clear()
        logbook_db._default_dsn = None

    def teardown_method(self):
        """每个测试后清理"""
        from engram.gateway import logbook_db
        logbook_db._db_instances.clear()
        logbook_db._default_dsn = None

    def test_same_dsn_returns_same_instance(self):
        """相同 DSN 返回相同实例"""
        from engram.gateway.logbook_db import get_db, reset_db, _db_instances
        
        # Mock LogbookDatabase 以避免实际连接
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            mock_instance = MagicMock()
            MockDB.return_value = mock_instance
            
            dsn = "postgresql://test:test@localhost/test_db"
            
            db1 = get_db(dsn=dsn)
            db2 = get_db(dsn=dsn)
            
            # 应该返回同一实例
            assert db1 is db2
            # 应该只创建一次
            assert MockDB.call_count == 1

    def test_different_dsn_returns_different_instance(self):
        """不同 DSN 返回不同实例"""
        from engram.gateway.logbook_db import get_db, _db_instances
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            mock_instance1 = MagicMock()
            mock_instance2 = MagicMock()
            MockDB.side_effect = [mock_instance1, mock_instance2]
            
            dsn1 = "postgresql://test:test@localhost/db1"
            dsn2 = "postgresql://test:test@localhost/db2"
            
            db1 = get_db(dsn=dsn1)
            db2 = get_db(dsn=dsn2)
            
            # 应该返回不同实例
            assert db1 is not db2
            assert db1 is mock_instance1
            assert db2 is mock_instance2
            # 应该创建两次
            assert MockDB.call_count == 2

    def test_reset_db_clears_all_instances(self):
        """reset_db() 不传参数时清除所有实例"""
        from engram.gateway.logbook_db import get_db, reset_db, _db_instances
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            MockDB.return_value = MagicMock()
            
            get_db(dsn="postgresql://localhost/db1")
            get_db(dsn="postgresql://localhost/db2")
            
            assert len(_db_instances) == 2
            
            reset_db()
            
            from engram.gateway.logbook_db import _db_instances as instances_after
            assert len(instances_after) == 0

    def test_reset_db_with_specific_dsn(self):
        """reset_db(dsn) 只清除指定 DSN 的实例"""
        from engram.gateway import logbook_db
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            MockDB.return_value = MagicMock()
            
            dsn1 = "postgresql://localhost/db1"
            dsn2 = "postgresql://localhost/db2"
            
            logbook_db.get_db(dsn=dsn1)
            logbook_db.get_db(dsn=dsn2)
            
            assert len(logbook_db._db_instances) == 2
            
            logbook_db.reset_db(dsn=dsn1)
            
            assert dsn1 not in logbook_db._db_instances
            assert dsn2 in logbook_db._db_instances

    def test_set_default_dsn(self):
        """set_default_dsn() 设置默认 DSN"""
        from engram.gateway import logbook_db
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            mock_instance = MagicMock()
            MockDB.return_value = mock_instance
            
            dsn = "postgresql://localhost/default_db"
            logbook_db.set_default_dsn(dsn)
            
            assert logbook_db._default_dsn == dsn

    def test_get_db_uses_default_dsn_when_no_param(self):
        """get_db() 无参数时使用默认 DSN"""
        from engram.gateway import logbook_db
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            mock_instance = MagicMock()
            MockDB.return_value = mock_instance
            
            dsn = "postgresql://localhost/default_db"
            logbook_db.set_default_dsn(dsn)
            
            # 先用指定 DSN 创建实例
            logbook_db.get_db(dsn=dsn)
            
            # 无参数调用应该使用默认 DSN
            db = logbook_db.get_db()
            
            # 应该返回同一个实例（因为 DSN 相同）
            assert dsn in logbook_db._db_instances

    def test_config_dsn_overrides_env_var(self):
        """配置的 DSN 可以覆盖环境变量中的 DSN"""
        from engram.gateway import logbook_db
        
        env_dsn = "postgresql://env:env@localhost/env_db"
        config_dsn = "postgresql://config:config@localhost/config_db"
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            mock_env_instance = MagicMock()
            mock_config_instance = MagicMock()
            MockDB.side_effect = [mock_env_instance, mock_config_instance]
            
            # 模拟环境变量
            with patch.dict(os.environ, {"POSTGRES_DSN": env_dsn}):
                # 使用环境变量的 DSN
                db_env = logbook_db.get_db()
                
                # 使用配置的 DSN（应该创建不同实例）
                db_config = logbook_db.get_db(dsn=config_dsn)
                
                # 两个实例应该不同
                assert db_env is not db_config


class TestGatewayConfigIntegration:
    """验证 GatewayConfig.postgres_dsn 与 get_db 集成"""

    def setup_method(self):
        """每个测试前重置状态"""
        from engram.gateway import logbook_db
        from engram.gateway import config as config_module
        logbook_db._db_instances.clear()
        logbook_db._default_dsn = None
        config_module.reset_config()

    def teardown_method(self):
        """每个测试后清理"""
        from engram.gateway import logbook_db
        from engram.gateway import config as config_module
        logbook_db._db_instances.clear()
        logbook_db._default_dsn = None
        config_module.reset_config()

    def test_gateway_uses_config_dsn(self):
        """验证 Gateway 使用配置中的 DSN 而非仅环境变量"""
        from engram.gateway import logbook_db
        from engram.gateway.config import get_config, reset_config
        
        config_dsn = "postgresql://config:pass@confighost:5432/config_db"
        env_dsn = "postgresql://env:pass@envhost:5432/env_db"
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            mock_instance = MagicMock()
            MockDB.return_value = mock_instance
            
            # 设置环境变量
            env_vars = {
                "PROJECT_KEY": "test_project",
                "POSTGRES_DSN": env_dsn,  # 环境变量中的 DSN
                "OPENMEMORY_BASE_URL": "http://localhost:8080",
            }
            
            with patch.dict(os.environ, env_vars, clear=False):
                reset_config()
                
                # 获取配置
                config = get_config()
                
                # 验证配置中的 DSN 是环境变量的值
                assert config.postgres_dsn == env_dsn
                
                # 模拟使用不同的 DSN（如从配置文件覆盖）
                logbook_db.set_default_dsn(config_dsn)
                logbook_db.get_db(dsn=config_dsn)
                
                # 确认使用了 config_dsn
                assert config_dsn in logbook_db._db_instances

    def test_different_config_dsn_creates_separate_db(self):
        """不同配置的 postgres_dsn 创建独立的 DB 实例"""
        from engram.gateway import logbook_db
        
        dsn1 = "postgresql://user1:pass@host1:5432/db1"
        dsn2 = "postgresql://user2:pass@host2:5432/db2"
        
        with patch('engram.gateway.logbook_db.LogbookDatabase') as MockDB:
            instance1 = MagicMock()
            instance2 = MagicMock()
            MockDB.side_effect = [instance1, instance2]
            
            # 模拟两个不同配置场景
            db1 = logbook_db.get_db(dsn=dsn1)
            db2 = logbook_db.get_db(dsn=dsn2)
            
            # 验证创建了两个不同的实例
            assert db1 is instance1
            assert db2 is instance2
            assert db1 is not db2
            
            # 验证缓存中有两个条目
            assert len(logbook_db._db_instances) == 2
            assert dsn1 in logbook_db._db_instances
            assert dsn2 in logbook_db._db_instances


class TestLogbookDBCheck:
    """Logbook DB 检查功能测试"""

    def setup_method(self):
        """每个测试前重置状态"""
        from engram.gateway import logbook_adapter
        logbook_adapter._adapter_instance = None

    def teardown_method(self):
        """每个测试后清理"""
        from engram.gateway import logbook_adapter
        logbook_adapter._adapter_instance = None

    def test_check_db_schema_success(self):
        """测试 DB 检查成功的场景"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckResult,
            LogbookDBErrorCode,
            check_db_schema,
        )
        
        # Mock run_all_checks 返回成功
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": True,
                "checks": {
                    "schemas": {"ok": True, "missing": []},
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
                    
                    assert result.ok is True
                    assert "schemas" in result.checks
                    mock_conn_instance.close.assert_called_once()

    def test_check_db_schema_missing_items(self):
        """测试 DB 检查发现缺失项的场景"""
        from engram.gateway.logbook_adapter import LogbookAdapter, LogbookDBCheckResult
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {
                    "schemas": {"ok": False, "missing": ["governance"]},
                    "columns": {"ok": True, "missing": []},
                    "indexes": {"ok": False, "missing": ["idx_write_audit_actor"]},
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
                    
                    assert result.ok is False
                    assert "governance" in str(result.checks["schemas"]["missing"])
                    
                    # 检查 missing summary
                    summary = result.get_missing_summary()
                    assert "schemas" in summary
                    assert "indexes" in summary

    def test_check_db_schema_module_not_available(self):
        """测试 db_migrate 模块不可用的场景"""
        from engram.gateway.logbook_adapter import LogbookAdapter
        
        with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', False):
            adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
            result = adapter.check_db_schema()
            
            assert result.ok is False
            assert "不可用" in result.message

    def test_ensure_db_ready_auto_migrate_success(self):
        """测试自动迁移成功的场景"""
        from engram.gateway.logbook_adapter import LogbookAdapter, LogbookDBCheckResult
        
        call_count = [0]
        
        def mock_check_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次检查：失败
                return {
                    "ok": False,
                    "checks": {"schemas": {"ok": False, "missing": ["governance"]}},
                }
            else:
                # 第二次检查（迁移后）：成功
                return {
                    "ok": True,
                    "checks": {"schemas": {"ok": True, "missing": []}},
                }
        
        with patch('engram.gateway.logbook_adapter.run_all_checks', side_effect=mock_check_side_effect):
            with patch('engram.gateway.logbook_adapter.run_migrate') as mock_migrate:
                mock_migrate.return_value = {"ok": True}
                
                with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                    mock_conn_instance = MagicMock()
                    mock_conn.return_value = mock_conn_instance
                    
                    with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                        adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                        result = adapter.ensure_db_ready(auto_migrate=True)
                        
                        assert result.ok is True
                        assert "自动迁移修复" in result.message
                        mock_migrate.assert_called_once()

    def test_ensure_db_ready_no_auto_migrate_raises_error(self):
        """测试不自动迁移时抛出错误并包含正确的错误码"""
        from engram.gateway.logbook_adapter import LogbookAdapter, LogbookDBCheckError, LogbookDBErrorCode
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {"schemas": {"ok": False, "missing": ["governance"]}},
            }
            
            with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                mock_conn_instance = MagicMock()
                mock_conn.return_value = mock_conn_instance
                
                with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                    adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                    
                    with pytest.raises(LogbookDBCheckError) as exc_info:
                        adapter.ensure_db_ready(auto_migrate=False)
                    
                    # 验证错误消息
                    assert "不完整" in str(exc_info.value.message)
                    assert "db_migrate.py" in str(exc_info.value.message)
                    assert "AUTO_MIGRATE_ON_STARTUP" in str(exc_info.value.message)
                    
                    # 验证错误码
                    assert exc_info.value.code == LogbookDBErrorCode.SCHEMA_MISSING


class TestCheckLogbookDbOnStartup:
    """测试 main.py 中的 check_logbook_db_on_startup 函数"""

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

    def test_skip_check_when_disabled(self):
        """测试禁用检查时跳过"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=False,
        )
        
        result = check_logbook_db_on_startup(config)
        assert result is True

    def test_skip_check_when_module_not_available(self):
        """测试模块不可用时跳过"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=True,
        )
        
        with patch('engram.gateway.main.is_db_migrate_available', return_value=False):
            result = check_logbook_db_on_startup(config)
            assert result is True

    def test_check_success(self):
        """测试检查成功"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        from engram.gateway.logbook_adapter import LogbookDBCheckResult
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=True,
        )
        
        with patch('engram.gateway.main.is_db_migrate_available', return_value=True):
            with patch('engram.gateway.main.ensure_db_ready') as mock_ensure:
                mock_ensure.return_value = LogbookDBCheckResult(ok=True)
                
                result = check_logbook_db_on_startup(config)
                assert result is True

    def test_check_failure_with_auto_migrate_disabled(self):
        """测试检查失败且未启用自动迁移"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        from engram.gateway.logbook_adapter import LogbookDBCheckError, LogbookDBErrorCode
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=True,
            auto_migrate_on_startup=False,
        )
        
        with patch('engram.gateway.main.is_db_migrate_available', return_value=True):
            with patch('engram.gateway.main.ensure_db_ready') as mock_ensure:
                mock_ensure.side_effect = LogbookDBCheckError(
                    message="DB 结构不完整",
                    code=LogbookDBErrorCode.SCHEMA_MISSING,
                    missing_items={"schemas": ["governance"]},
                )
                
                result = check_logbook_db_on_startup(config)
                assert result is False


class TestLogbookDBErrorCode:
    """Logbook DB 错误码测试"""

    def setup_method(self):
        """每个测试前重置状态"""
        from engram.gateway import logbook_adapter
        logbook_adapter._adapter_instance = None

    def teardown_method(self):
        """每个测试后清理"""
        from engram.gateway import logbook_adapter
        logbook_adapter._adapter_instance = None

    def test_error_code_schema_missing(self):
        """测试 schema 缺失时返回 LOGBOOK_DB_SCHEMA_MISSING 错误码"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckError,
            LogbookDBErrorCode,
        )
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {
                    "schemas": {"ok": False, "missing": ["governance", "logbook"]},
                    "tables": {"ok": True, "missing": []},
                    "indexes": {"ok": True, "missing": []},
                }
            }
            
            with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                mock_conn_instance = MagicMock()
                mock_conn.return_value = mock_conn_instance
                
                with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                    adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                    
                    with pytest.raises(LogbookDBCheckError) as exc_info:
                        adapter.ensure_db_ready(auto_migrate=False)
                    
                    # 验证错误码为 LOGBOOK_DB_SCHEMA_MISSING
                    assert exc_info.value.code == LogbookDBErrorCode.SCHEMA_MISSING
                    assert "governance" in str(exc_info.value.missing_items)

    def test_error_code_table_missing(self):
        """测试表缺失时返回 LOGBOOK_DB_TABLE_MISSING 错误码"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckError,
            LogbookDBErrorCode,
        )
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {
                    "schemas": {"ok": True, "missing": []},
                    "tables": {"ok": False, "missing": ["governance.settings", "logbook.outbox_memory"]},
                    "indexes": {"ok": True, "missing": []},
                }
            }
            
            with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                mock_conn_instance = MagicMock()
                mock_conn.return_value = mock_conn_instance
                
                with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                    adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                    
                    with pytest.raises(LogbookDBCheckError) as exc_info:
                        adapter.ensure_db_ready(auto_migrate=False)
                    
                    # 验证错误码为 LOGBOOK_DB_TABLE_MISSING
                    assert exc_info.value.code == LogbookDBErrorCode.TABLE_MISSING

    def test_error_code_index_missing(self):
        """测试索引缺失时返回 LOGBOOK_DB_INDEX_MISSING 错误码"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckError,
            LogbookDBErrorCode,
        )
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {
                    "schemas": {"ok": True, "missing": []},
                    "tables": {"ok": True, "missing": []},
                    "indexes": {"ok": False, "missing": ["idx_write_audit_actor"]},
                }
            }
            
            with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                mock_conn_instance = MagicMock()
                mock_conn.return_value = mock_conn_instance
                
                with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                    adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                    
                    with pytest.raises(LogbookDBCheckError) as exc_info:
                        adapter.ensure_db_ready(auto_migrate=False)
                    
                    # 验证错误码为 LOGBOOK_DB_INDEX_MISSING
                    assert exc_info.value.code == LogbookDBErrorCode.INDEX_MISSING

    def test_error_code_migrate_failed(self):
        """测试迁移失败时返回 LOGBOOK_DB_MIGRATE_FAILED 错误码"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckError,
            LogbookDBErrorCode,
        )
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {
                    "schemas": {"ok": False, "missing": ["governance"]},
                }
            }
            
            with patch('engram.gateway.logbook_adapter.run_migrate') as mock_migrate:
                # 模拟迁移失败
                mock_migrate.return_value = {"ok": False, "message": "Connection refused"}
                
                with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                    mock_conn_instance = MagicMock()
                    mock_conn.return_value = mock_conn_instance
                    
                    with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                        adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                        
                        with pytest.raises(LogbookDBCheckError) as exc_info:
                            adapter.ensure_db_ready(auto_migrate=True)
                        
                        # 验证错误码为 LOGBOOK_DB_MIGRATE_FAILED
                        assert exc_info.value.code == LogbookDBErrorCode.MIGRATE_FAILED
                        assert "Connection refused" in exc_info.value.message

    def test_error_code_migrate_partial(self):
        """测试迁移后仍有缺失时返回 LOGBOOK_DB_MIGRATE_PARTIAL 错误码"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckError,
            LogbookDBErrorCode,
        )
        
        call_count = [0]
        
        def mock_check_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次检查：失败
                return {
                    "ok": False,
                    "checks": {"schemas": {"ok": False, "missing": ["governance"]}},
                }
            else:
                # 第二次检查（迁移后）：仍然失败
                return {
                    "ok": False,
                    "checks": {"indexes": {"ok": False, "missing": ["idx_missing"]}},
                }
        
        with patch('engram.gateway.logbook_adapter.run_all_checks', side_effect=mock_check_side_effect):
            with patch('engram.gateway.logbook_adapter.run_migrate') as mock_migrate:
                mock_migrate.return_value = {"ok": True}
                
                with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                    mock_conn_instance = MagicMock()
                    mock_conn.return_value = mock_conn_instance
                    
                    with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                        adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                        
                        with pytest.raises(LogbookDBCheckError) as exc_info:
                            adapter.ensure_db_ready(auto_migrate=True)
                        
                        # 验证错误码为 LOGBOOK_DB_MIGRATE_PARTIAL
                        assert exc_info.value.code == LogbookDBErrorCode.MIGRATE_PARTIAL

    def test_error_message_contains_repair_hint(self):
        """测试错误消息包含修复命令提示"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBCheckError,
        )
        
        with patch('engram.gateway.logbook_adapter.run_all_checks') as mock_check:
            mock_check.return_value = {
                "ok": False,
                "checks": {
                    "schemas": {"ok": False, "missing": ["governance"]},
                }
            }
            
            with patch('engram.gateway.logbook_adapter.get_connection') as mock_conn:
                mock_conn_instance = MagicMock()
                mock_conn.return_value = mock_conn_instance
                
                with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', True):
                    adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
                    
                    with pytest.raises(LogbookDBCheckError) as exc_info:
                        adapter.ensure_db_ready(auto_migrate=False)
                    
                    # 验证错误消息包含修复提示
                    error_msg = exc_info.value.message
                    assert "db_migrate.py" in error_msg
                    assert "AUTO_MIGRATE_ON_STARTUP" in error_msg

    def test_run_migration_returns_correct_error_code_when_unavailable(self):
        """测试迁移模块不可用时返回正确错误码"""
        from engram.gateway.logbook_adapter import (
            LogbookAdapter,
            LogbookDBErrorCode,
        )
        
        with patch('engram.gateway.logbook_adapter._DB_MIGRATE_AVAILABLE', False):
            adapter = LogbookAdapter(dsn="postgresql://test@localhost/test")
            result = adapter.run_migration()
            
            assert result["ok"] is False
            assert result["code"] == LogbookDBErrorCode.MIGRATE_NOT_AVAILABLE


class TestCheckLogbookDbOnStartupWithErrorCode:
    """测试 main.py 中的 check_logbook_db_on_startup 函数的错误码处理"""

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

    def test_check_failure_logs_error_code(self):
        """测试检查失败时日志包含错误码"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        from engram.gateway.logbook_adapter import LogbookDBCheckError, LogbookDBErrorCode
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=True,
            auto_migrate_on_startup=False,
        )
        
        with patch('engram.gateway.main.is_db_migrate_available', return_value=True):
            with patch('engram.gateway.main.ensure_db_ready') as mock_ensure:
                mock_ensure.side_effect = LogbookDBCheckError(
                    message="DB 结构不完整",
                    code=LogbookDBErrorCode.SCHEMA_MISSING,
                    missing_items={"schemas": ["governance"]},
                )
                
                result = check_logbook_db_on_startup(config)
                
                # 验证返回失败
                assert result is False
                
                # 验证 ensure_db_ready 被正确调用
                mock_ensure.assert_called_once_with(
                    dsn=config.postgres_dsn,
                    auto_migrate=False,
                )

    def test_auto_migrate_enabled_passes_correct_flag(self):
        """测试启用自动迁移时传递正确的标志"""
        from engram.gateway.main import check_logbook_db_on_startup
        from engram.gateway.config import GatewayConfig
        from engram.gateway.logbook_adapter import LogbookDBCheckResult
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test@localhost/test",
            openmemory_base_url="http://localhost:8080",
            logbook_check_on_startup=True,
            auto_migrate_on_startup=True,  # 启用自动迁移
        )
        
        with patch('engram.gateway.main.is_db_migrate_available', return_value=True):
            with patch('engram.gateway.main.ensure_db_ready') as mock_ensure:
                mock_ensure.return_value = LogbookDBCheckResult(
                    ok=True,
                    message="DB 结构通过自动迁移修复",
                )
                
                result = check_logbook_db_on_startup(config)
                
                # 验证返回成功
                assert result is True
                
                # 验证 ensure_db_ready 被正确调用，auto_migrate=True
                mock_ensure.assert_called_once_with(
                    dsn=config.postgres_dsn,
                    auto_migrate=True,
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
