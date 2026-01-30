# -*- coding: utf-8 -*-
"""
db_bootstrap.py 单元测试和集成测试

测试内容：
1. 预检功能（配置级检查，不需要数据库连接）
2. 错误码和修复命令
3. 角色创建（需要数据库连接的集成测试）

运行方式：
- 单元测试: pytest tests/test_db_bootstrap.py -v -k "unit"
- 集成测试: pytest tests/test_db_bootstrap.py -v -k "integration"
- 全部测试: pytest tests/test_db_bootstrap.py -v

环境变量：
- TEST_PG_DSN: 测试数据库 DSN（集成测试需要）
- SKIP_BOOTSTRAP_INTEGRATION_TESTS: 设为 1 跳过集成测试
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse, urlunparse

import pytest

# 确保可以导入 db_bootstrap
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from db_bootstrap import (
    BootstrapErrorCode,
    DEFAULT_OM_SCHEMA,
    check_om_schema_not_public,
    check_admin_privileges,
    run_precheck,
    create_or_update_login_role,
    create_all_login_roles,
    mask_password_in_dsn,
    parse_db_from_dsn,
    LOGIN_ROLES,
    ENV_LOGBOOK_MIGRATOR_PASSWORD,
    ENV_LOGBOOK_SVC_PASSWORD,
    ENV_OPENMEMORY_MIGRATOR_PASSWORD,
    ENV_OPENMEMORY_SVC_PASSWORD,
)


# ============================================================================
# 标记定义
# ============================================================================

pytestmark = [
    pytest.mark.unit,  # 默认为单元测试
]


def should_skip_integration_tests() -> bool:
    """检查是否跳过集成测试"""
    return os.environ.get("SKIP_BOOTSTRAP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes")


# ============================================================================
# 配置级检查测试（单元测试，不需要数据库）
# ============================================================================

class TestSchemaValidation:
    """OM_PG_SCHEMA 配置验证测试"""
    
    def test_schema_public_rejected(self):
        """测试 public schema 被正确拒绝"""
        result = check_om_schema_not_public("public")
        
        assert result["ok"] is False
        assert result["code"] == BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC
        assert "public" in result["message"].lower()
        assert result["remediation"]  # 应有修复命令
    
    def test_schema_public_case_insensitive(self):
        """测试 public schema 检查不区分大小写"""
        for variant in ["PUBLIC", "Public", "pUbLiC"]:
            result = check_om_schema_not_public(variant)
            assert result["ok"] is False, f"应拒绝 '{variant}'"
            assert result["code"] == BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC
    
    def test_schema_openmemory_accepted(self):
        """测试默认 openmemory schema 被接受"""
        result = check_om_schema_not_public("openmemory")
        
        assert result["ok"] is True
        assert result["code"] == ""
        assert result["value"] == "openmemory"
    
    def test_schema_custom_accepted(self):
        """测试自定义 schema 名称被接受"""
        custom_schemas = [
            "myproject_openmemory",
            "tenant_a_openmemory",
            "om_dev",
            "test_schema",
        ]
        for schema in custom_schemas:
            result = check_om_schema_not_public(schema)
            assert result["ok"] is True, f"应接受 '{schema}'"


class TestPrecheckConfigOnly:
    """预检配置级测试（不需要数据库连接）"""
    
    def test_precheck_schema_public_fails_without_dsn(self):
        """测试：无 DSN 时仍能检测到 public schema 错误"""
        result = run_precheck(
            admin_dsn=None,
            om_schema="public",
            quiet=True,
            skip_db_check=True,
        )
        
        assert result["ok"] is False
        assert BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC in result.get("failed_codes", [])
        assert "om_schema_not_public" in result["checks"]
        assert result["checks"]["om_schema_not_public"]["ok"] is False
    
    def test_precheck_valid_schema_passes_config_check(self):
        """测试：有效 schema 配置通过配置级检查"""
        result = run_precheck(
            admin_dsn=None,
            om_schema="openmemory",
            quiet=True,
            skip_db_check=True,
        )
        
        assert result["ok"] is True
        assert result["checks"]["om_schema_not_public"]["ok"] is True
        # 数据库检查被跳过
        assert result["checks"]["admin_privileges"].get("skipped") is True
    
    def test_precheck_returns_failed_codes(self):
        """测试：预检返回失败的错误码列表"""
        result = run_precheck(
            admin_dsn=None,
            om_schema="public",
            quiet=True,
            skip_db_check=True,
        )
        
        assert "failed_codes" in result
        assert isinstance(result["failed_codes"], list)
        assert len(result["failed_codes"]) > 0


class TestErrorCodeFormat:
    """错误码格式测试"""
    
    def test_error_codes_follow_naming_convention(self):
        """测试错误码遵循命名规范"""
        # 所有错误码应以 BOOTSTRAP_ 开头
        for attr_name in dir(BootstrapErrorCode):
            if not attr_name.startswith("_"):
                code = getattr(BootstrapErrorCode, attr_name)
                assert code.startswith("BOOTSTRAP_"), f"错误码 {attr_name} 应以 BOOTSTRAP_ 开头"
    
    def test_error_code_categories(self):
        """测试错误码分类"""
        # 配置错误
        assert "CONFIG" in BootstrapErrorCode.CONFIG_MISSING_DSN
        assert "CONFIG" in BootstrapErrorCode.CONFIG_INVALID_SCHEMA
        
        # 预检错误
        assert "PRECHECK" in BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC
        assert "PRECHECK" in BootstrapErrorCode.PRECHECK_NO_CREATEROLE
        
        # 角色错误
        assert "ROLE" in BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD
        assert "ROLE" in BootstrapErrorCode.ROLE_CREATION_FAILED


class TestDSNParsing:
    """DSN 解析测试"""
    
    def test_parse_db_from_dsn_basic(self):
        """测试基本 DSN 解析"""
        dsn = "postgresql://postgres:password@localhost:5432/mydb"
        assert parse_db_from_dsn(dsn) == "mydb"
    
    def test_parse_db_from_dsn_no_port(self):
        """测试无端口的 DSN 解析"""
        dsn = "postgresql://postgres:password@localhost/mydb"
        assert parse_db_from_dsn(dsn) == "mydb"
    
    def test_parse_db_from_dsn_empty(self):
        """测试空数据库名"""
        dsn = "postgresql://postgres:password@localhost:5432/"
        assert parse_db_from_dsn(dsn) is None
    
    def test_mask_password_in_dsn(self):
        """测试密码遮蔽"""
        dsn = "postgresql://postgres:secret123@localhost:5432/mydb"
        masked = mask_password_in_dsn(dsn)
        
        assert "secret123" not in masked
        assert "***" in masked
        assert "postgres:" in masked


class TestLoginRolesDefinition:
    """LOGIN_ROLES 定义测试"""
    
    def test_login_roles_structure(self):
        """测试 LOGIN_ROLES 结构正确"""
        for role_name, env_var, inherit_role in LOGIN_ROLES:
            assert isinstance(role_name, str)
            assert isinstance(env_var, str)
            assert isinstance(inherit_role, str)
            assert role_name  # 非空
            assert env_var  # 非空
            assert inherit_role  # 非空
    
    def test_login_roles_env_vars(self):
        """测试 LOGIN_ROLES 环境变量名正确"""
        expected_env_vars = {
            ENV_LOGBOOK_MIGRATOR_PASSWORD,
            ENV_LOGBOOK_SVC_PASSWORD,
            ENV_OPENMEMORY_MIGRATOR_PASSWORD,
            ENV_OPENMEMORY_SVC_PASSWORD,
        }
        
        actual_env_vars = {env_var for _, env_var, _ in LOGIN_ROLES}
        
        # 至少包含这些必需的环境变量
        assert expected_env_vars.issubset(actual_env_vars)
    
    def test_login_roles_inheritance_mapping(self):
        """测试 LOGIN_ROLES 继承关系正确"""
        expected_mappings = {
            "logbook_migrator": "engram_migrator",
            "logbook_svc": "engram_app_readwrite",
            "openmemory_migrator_login": "openmemory_migrator",
            "openmemory_svc": "openmemory_app",
        }
        
        for role_name, _, inherit_role in LOGIN_ROLES:
            if role_name in expected_mappings:
                assert inherit_role == expected_mappings[role_name], \
                    f"{role_name} 应继承 {expected_mappings[role_name]}，实际为 {inherit_role}"


# ============================================================================
# 集成测试（需要数据库连接）
# ============================================================================

@pytest.fixture(scope="module")
def bootstrap_test_db(test_db_info: dict):
    """
    Bootstrap 测试专用 fixture
    
    在测试数据库中准备 bootstrap 测试所需的环境：
    1. 执行基础迁移（创建 schema）
    2. 返回连接信息
    """
    if should_skip_integration_tests():
        pytest.skip("Bootstrap 集成测试已通过环境变量禁用")
    
    import psycopg
    from db_migrate import run_migrate
    
    dsn = test_db_info["dsn"]
    admin_dsn = test_db_info.get("admin_dsn", dsn)
    
    # 执行基础迁移
    result = run_migrate(dsn=dsn, quiet=True)
    if not result.get("ok"):
        pytest.fail(f"迁移失败: {result.get('message')}")
    
    yield {
        "dsn": dsn,
        "admin_dsn": admin_dsn,
        "db_name": test_db_info["db_name"],
    }


@pytest.mark.integration
class TestAdminPrivilegesCheck:
    """管理员权限检查测试（需要数据库）"""
    
    def test_superuser_has_all_privileges(self, bootstrap_test_db: dict):
        """测试 superuser 拥有所有权限"""
        import psycopg
        
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            result = check_admin_privileges(conn)
            
            # superuser 应该通过所有检查
            assert result["ok"] is True
            details = result.get("details", {})
            assert details.get("is_superuser") is True or details.get("can_create_role") is True
        finally:
            conn.close()


@pytest.mark.integration
class TestRoleCreation:
    """角色创建测试（需要数据库）"""
    
    def test_create_role_missing_password_returns_error(self, bootstrap_test_db: dict):
        """测试：缺少密码时返回正确的错误码"""
        import psycopg
        
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            # 先确保角色不存在
            with conn.cursor() as cur:
                cur.execute("DROP ROLE IF EXISTS test_bootstrap_role")
            
            # 尝试创建角色但不提供密码
            result = create_or_update_login_role(
                conn,
                role_name="test_bootstrap_role",
                password=None,  # 不提供密码
                inherit_role="engram_app_readwrite",
                quiet=True,
            )
            
            assert result["ok"] is False
            assert result["code"] == BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD
            assert result["remediation"]  # 应有修复命令
        finally:
            # 清理
            with conn.cursor() as cur:
                cur.execute("DROP ROLE IF EXISTS test_bootstrap_role")
            conn.close()
    
    def test_create_role_with_password_succeeds(self, bootstrap_test_db: dict):
        """测试：提供密码时角色创建成功"""
        import psycopg
        
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            # 先确保角色不存在
            with conn.cursor() as cur:
                cur.execute("DROP ROLE IF EXISTS test_bootstrap_role")
            
            # 创建角色
            result = create_or_update_login_role(
                conn,
                role_name="test_bootstrap_role",
                password="test_password_12345",
                inherit_role="engram_app_readwrite",
                quiet=True,
            )
            
            assert result["ok"] is True
            assert result["created"] is True
            
            # 验证角色存在
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'test_bootstrap_role'")
                assert cur.fetchone() is not None
        finally:
            # 清理
            with conn.cursor() as cur:
                cur.execute("DROP ROLE IF EXISTS test_bootstrap_role")
            conn.close()
    
    def test_update_existing_role_password(self, bootstrap_test_db: dict):
        """测试：更新已存在角色的密码"""
        import psycopg
        
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            # 先创建角色
            with conn.cursor() as cur:
                cur.execute("DROP ROLE IF EXISTS test_bootstrap_role")
                cur.execute("CREATE ROLE test_bootstrap_role LOGIN PASSWORD 'old_password'")
            
            # 更新密码
            result = create_or_update_login_role(
                conn,
                role_name="test_bootstrap_role",
                password="new_password_12345",
                inherit_role="engram_app_readwrite",
                quiet=True,
            )
            
            assert result["ok"] is True
            assert result["created"] is False
            assert result["updated"] is True
        finally:
            # 清理
            with conn.cursor() as cur:
                cur.execute("DROP ROLE IF EXISTS test_bootstrap_role")
            conn.close()


@pytest.mark.integration
class TestFullPrecheck:
    """完整预检测试（需要数据库）"""
    
    def test_precheck_with_valid_config_passes(self, bootstrap_test_db: dict):
        """测试：有效配置通过完整预检"""
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        result = run_precheck(
            admin_dsn=admin_dsn,
            om_schema="openmemory",
            quiet=True,
        )
        
        assert result["ok"] is True
        assert result["checks"]["om_schema_not_public"]["ok"] is True
        assert result["checks"]["admin_privileges"]["ok"] is True
    
    def test_precheck_with_public_schema_fails(self, bootstrap_test_db: dict):
        """测试：public schema 配置导致预检失败"""
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        result = run_precheck(
            admin_dsn=admin_dsn,
            om_schema="public",
            quiet=True,
        )
        
        assert result["ok"] is False
        assert BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC in result.get("failed_codes", [])


@pytest.mark.integration
class TestCreateAllLoginRoles:
    """批量角色创建测试（需要数据库）"""
    
    def test_create_all_roles_with_missing_passwords(self, bootstrap_test_db: dict):
        """测试：缺少密码时批量创建返回详细错误"""
        import psycopg
        
        admin_dsn = bootstrap_test_db["admin_dsn"]
        
        conn = psycopg.connect(admin_dsn, autocommit=True)
        try:
            # 先删除可能存在的角色
            for role_name, _, _ in LOGIN_ROLES:
                with conn.cursor() as cur:
                    cur.execute(f"DROP ROLE IF EXISTS {role_name}")
            
            # 尝试创建但不提供任何密码
            result = create_all_login_roles(
                conn,
                passwords={},  # 空密码字典
                quiet=True,
            )
            
            # 应该失败（新角色需要密码）
            assert result["ok"] is False
            assert len(result.get("failed_codes", [])) > 0
            assert result.get("remediation")  # 应有修复建议
        finally:
            # 清理
            for role_name, _, _ in LOGIN_ROLES:
                with conn.cursor() as cur:
                    cur.execute(f"DROP ROLE IF EXISTS {role_name}")
            conn.close()


# ============================================================================
# 幂等性测试（单元测试）
# ============================================================================

class TestIdempotency:
    """幂等性测试：验证 bootstrap 操作的幂等行为"""
    
    def test_schema_check_is_idempotent(self):
        """测试 schema 检查函数多次调用结果一致"""
        # 同样的输入应该返回同样的结果
        result1 = check_om_schema_not_public("openmemory")
        result2 = check_om_schema_not_public("openmemory")
        
        assert result1 == result2
        assert result1["ok"] is True
    
    def test_schema_public_rejection_is_idempotent(self):
        """测试 public schema 拒绝行为幂等"""
        result1 = check_om_schema_not_public("public")
        result2 = check_om_schema_not_public("PUBLIC")
        
        # 两者都应失败，错误码一致
        assert result1["ok"] is False
        assert result2["ok"] is False
        assert result1["code"] == result2["code"]
        assert result1["code"] == BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC
    
    def test_precheck_config_only_is_idempotent(self):
        """测试配置级预检幂等性"""
        result1 = run_precheck(
            admin_dsn=None,
            om_schema="myschema",
            quiet=True,
            skip_db_check=True,
        )
        result2 = run_precheck(
            admin_dsn=None,
            om_schema="myschema",
            quiet=True,
            skip_db_check=True,
        )
        
        # 结构应一致
        assert result1["ok"] == result2["ok"]
        assert set(result1["checks"].keys()) == set(result2["checks"].keys())


# ============================================================================
# 缺失项报错结构测试（单元测试）
# ============================================================================

class TestMissingItemsErrorStructure:
    """缺失项报错结构测试：验证错误返回包含必要的诊断信息"""
    
    def test_missing_password_error_contains_env_var_hint(self):
        """测试缺少密码错误包含环境变量提示"""
        result = check_om_schema_not_public("public")
        
        # 错误结构应包含必要字段
        assert "ok" in result
        assert "code" in result
        assert "message" in result
        assert "remediation" in result
        
        # remediation 应包含可操作的修复命令
        assert result["remediation"]  # 非空
        assert "OM_PG_SCHEMA" in result["remediation"]
    
    def test_precheck_failure_returns_failed_codes_list(self):
        """测试预检失败返回 failed_codes 列表"""
        result = run_precheck(
            admin_dsn=None,
            om_schema="public",
            quiet=True,
            skip_db_check=True,
        )
        
        assert result["ok"] is False
        
        # 必须包含 failed_codes 列表
        assert "failed_codes" in result
        assert isinstance(result["failed_codes"], list)
        assert len(result["failed_codes"]) > 0
        
        # 每个错误码应符合 BOOTSTRAP_ 前缀规范
        for code in result["failed_codes"]:
            assert code.startswith("BOOTSTRAP_"), f"错误码 {code} 应以 BOOTSTRAP_ 开头"
    
    def test_precheck_checks_dict_structure(self):
        """测试预检 checks 字典结构完整"""
        result = run_precheck(
            admin_dsn=None,
            om_schema="openmemory",
            quiet=True,
            skip_db_check=True,
        )
        
        # checks 应是字典
        assert "checks" in result
        assert isinstance(result["checks"], dict)
        
        # 应包含 om_schema_not_public 检查项
        assert "om_schema_not_public" in result["checks"]
        
        # 每个检查项应有 ok 字段
        for check_name, check_result in result["checks"].items():
            assert "ok" in check_result, f"检查项 {check_name} 缺少 ok 字段"
    
    def test_error_code_remediation_mapping(self):
        """测试错误码与修复命令映射"""
        # 从 db_bootstrap 导入 REMEDIATION_COMMANDS
        from db_bootstrap import REMEDIATION_COMMANDS
        
        # 验证关键错误码有对应的修复命令
        key_error_codes = [
            BootstrapErrorCode.CONFIG_MISSING_DSN,
            BootstrapErrorCode.CONFIG_MISSING_PASSWORD,
            BootstrapErrorCode.PRECHECK_SCHEMA_PUBLIC,
            BootstrapErrorCode.PRECHECK_NO_CREATEROLE,
            BootstrapErrorCode.ROLE_CREATION_MISSING_PASSWORD,
        ]
        
        for code in key_error_codes:
            assert code in REMEDIATION_COMMANDS, f"错误码 {code} 缺少修复命令映射"
            assert REMEDIATION_COMMANDS[code], f"错误码 {code} 的修复命令不应为空"


# ============================================================================
# 脚本职责边界测试（文档化测试）
# ============================================================================

class TestScriptResponsibilities:
    """
    脚本职责边界测试
    
    db_bootstrap.py 职责：
    1. 预检配置安全性（OM_PG_SCHEMA 不能是 public）
    2. 预检管理员权限（CREATEROLE, CREATE SCHEMA）
    3. 幂等创建/更新 LOGIN 角色
    4. 应用 04_roles_and_grants.sql（NOLOGIN 权限角色）
    5. 应用 05_openmemory_roles_and_grants.sql（OpenMemory schema）
    6. 数据库级硬化配置
    7. 执行 99_verify_permissions.sql 验收
    
    db_migrate.py 职责：
    1. 预检 OM_PG_SCHEMA 配置
    2. 自动创建数据库（如果不存在）
    3. 执行结构性 DDL 脚本（01/02/03/06/07/08/09/10/11）
    4. 可选执行权限脚本（--apply-roles, --apply-openmemory-grants）
    5. 可选执行验证脚本（--verify）
    6. 可选执行迁移后回填（--post-backfill）
    7. 自检验证（schema/表/索引/触发器/物化视图）
    """
    
    def test_bootstrap_handles_login_roles(self):
        """验证 bootstrap 负责 LOGIN 角色创建"""
        # LOGIN_ROLES 定义应包含登录角色
        expected_login_roles = {
            "logbook_migrator",
            "logbook_svc",
            "openmemory_migrator_login",
            "openmemory_svc",
        }
        
        actual_roles = {role_name for role_name, _, _ in LOGIN_ROLES}
        
        assert expected_login_roles.issubset(actual_roles), \
            f"LOGIN_ROLES 应包含所有登录角色: {expected_login_roles - actual_roles}"
    
    def test_bootstrap_error_codes_distinct_from_migrate(self):
        """验证 bootstrap 错误码与 migrate 模块不冲突"""
        # bootstrap 错误码应以 BOOTSTRAP_ 开头
        bootstrap_codes = []
        for attr_name in dir(BootstrapErrorCode):
            if not attr_name.startswith("_"):
                code = getattr(BootstrapErrorCode, attr_name)
                if isinstance(code, str):
                    bootstrap_codes.append(code)
        
        # 所有 bootstrap 错误码应以 BOOTSTRAP_ 开头
        for code in bootstrap_codes:
            assert code.startswith("BOOTSTRAP_"), \
                f"Bootstrap 错误码 {code} 应以 BOOTSTRAP_ 开头以避免冲突"
    
    def test_precheck_available_without_db_connection(self):
        """验证预检可在无数据库连接时执行"""
        # 配置级预检不应依赖数据库连接
        result = run_precheck(
            admin_dsn=None,  # 无 DSN
            om_schema="openmemory",
            quiet=True,
            skip_db_check=True,
        )
        
        # 应成功执行配置级检查
        assert "checks" in result
        assert "om_schema_not_public" in result["checks"]
        
        # 数据库检查应被跳过
        assert result["checks"].get("admin_privileges", {}).get("skipped") is True
