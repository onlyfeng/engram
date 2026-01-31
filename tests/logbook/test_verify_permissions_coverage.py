"""
99_verify_permissions.sql 权限检查覆盖率测试

验证 99_verify_permissions.sql 中包含所有必要的权限检查断言。

测试内容：
1. engram 角色检查存在性
2. openmemory 角色检查存在性
3. schema 权限检查
4. 输出级别定义
"""

import re
from pathlib import Path

import pytest


def get_project_root() -> Path:
    """获取项目根目录"""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "docker-compose.unified.yml").exists():
            return parent
    raise RuntimeError("Cannot find project root with docker-compose.unified.yml")


PROJECT_ROOT = get_project_root()
# 验证脚本位于 sql/verify/ 子目录（不被 initdb 自动执行）
VERIFY_PERMISSIONS_SQL = PROJECT_ROOT / "sql/verify/99_verify_permissions.sql"
MAIN_VERIFY_SQL = PROJECT_ROOT / "sql/verify/99_verify_permissions.sql"


class TestVerifyPermissionsSqlCoverage:
    """99_verify_permissions.sql 权限检查覆盖率测试"""

    @pytest.fixture(autouse=True)
    def load_sql_content(self):
        """加载 SQL 文件内容"""
        # 使用主 SQL 文件进行测试
        self.verify_sql = MAIN_VERIFY_SQL.read_text()

    def test_engram_admin_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 engram_admin 角色"""
        assert "engram_admin" in self.verify_sql, (
            "99_verify_permissions.sql should verify engram_admin role"
        )

    def test_engram_migrator_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 engram_migrator 角色"""
        assert "engram_migrator" in self.verify_sql, (
            "99_verify_permissions.sql should verify engram_migrator role"
        )

    def test_engram_app_readwrite_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 engram_app_readwrite 角色"""
        assert "engram_app_readwrite" in self.verify_sql, (
            "99_verify_permissions.sql should verify engram_app_readwrite role"
        )

    def test_engram_app_readonly_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 engram_app_readonly 角色"""
        assert "engram_app_readonly" in self.verify_sql, (
            "99_verify_permissions.sql should verify engram_app_readonly role"
        )

    def test_openmemory_migrator_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 openmemory_migrator 角色"""
        assert "openmemory_migrator" in self.verify_sql, (
            "99_verify_permissions.sql should verify openmemory_migrator role"
        )

    def test_openmemory_app_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 openmemory_app 角色"""
        assert "openmemory_app" in self.verify_sql, (
            "99_verify_permissions.sql should verify openmemory_app role"
        )

    def test_login_role_membership_check_exists(self):
        """验证 99_verify_permissions.sql 检查 LOGIN 角色 membership"""
        assert "logbook_migrator" in self.verify_sql, (
            "99_verify_permissions.sql should verify logbook_migrator LOGIN role"
        )
        assert "logbook_svc" in self.verify_sql, (
            "99_verify_permissions.sql should verify logbook_svc LOGIN role"
        )
        assert "openmemory_migrator_login" in self.verify_sql, (
            "99_verify_permissions.sql should verify openmemory_migrator_login LOGIN role"
        )
        assert "openmemory_svc" in self.verify_sql, (
            "99_verify_permissions.sql should verify openmemory_svc LOGIN role"
        )

    def test_public_schema_create_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 public schema CREATE 权限"""
        assert "public" in self.verify_sql.lower(), (
            "99_verify_permissions.sql should check public schema permissions"
        )
        assert "CREATE" in self.verify_sql, (
            "99_verify_permissions.sql should check CREATE permission"
        )

    def test_openmemory_schema_check_exists(self):
        """验证 99_verify_permissions.sql 检查 openmemory schema"""
        assert "om.target_schema" in self.verify_sql, (
            "99_verify_permissions.sql should support om.target_schema config variable"
        )
        assert "openmemory" in self.verify_sql, (
            "99_verify_permissions.sql should check openmemory schema"
        )

    def test_output_levels_defined(self):
        """验证 99_verify_permissions.sql 定义了输出级别"""
        output_levels = ["FAIL", "WARN", "OK", "SKIP"]
        for level in output_levels:
            assert level in self.verify_sql, (
                f"Output level {level} should be defined in 99_verify_permissions.sql"
            )

    def test_remedy_suggestions_included(self):
        """验证 99_verify_permissions.sql 包含修复建议"""
        # 应有 remedy 或 GRANT/REVOKE 建议
        assert "remedy" in self.verify_sql.lower() or "GRANT" in self.verify_sql, (
            "99_verify_permissions.sql should include remedy suggestions"
        )

    def test_database_level_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查数据库级权限"""
        assert "CONNECT" in self.verify_sql, (
            "99_verify_permissions.sql should check CONNECT permission"
        )
        assert "TEMP" in self.verify_sql, "99_verify_permissions.sql should check TEMP permission"

    def test_default_acl_check_exists(self):
        """验证 99_verify_permissions.sql 检查 pg_default_acl"""
        assert "pg_default_acl" in self.verify_sql, (
            "99_verify_permissions.sql should check pg_default_acl"
        )

    def _find_section(self, section_num: str, content: str) -> str:
        """查找指定 section 的内容"""
        pattern = rf"=== {section_num}.*?===.*?(?===|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        return match.group(0) if match else ""


class TestSectionNumbers:
    """验证 SQL 文件 section 编号"""

    def test_section_numbers_exist(self):
        """验证 99_verify_permissions.sql 中存在 section 编号"""
        verify_sql = MAIN_VERIFY_SQL.read_text()

        # 提取所有 section 编号（格式：=== N. 标题 ===）
        # 例如：RAISE NOTICE '=== 1. NOLOGIN 角色验证 ===';
        sections = re.findall(r"===\s*(\d+(?:\.\d+)?)[.\s]", verify_sql)

        # 验证存在多个 section
        assert len(sections) >= 5, f"Should have at least 5 sections, found: {sections}"


class TestStrictModeSupport:
    """验证 99_verify_permissions.sql 支持 strict 模式"""

    @pytest.fixture(autouse=True)
    def load_sql_content(self):
        """加载 SQL 文件内容"""
        self.verify_sql = MAIN_VERIFY_SQL.read_text()

    def test_strict_mode_config_variable_exists(self):
        """验证 99_verify_permissions.sql 支持 engram.verify_strict 配置变量"""
        assert "engram.verify_strict" in self.verify_sql, (
            "99_verify_permissions.sql should support engram.verify_strict config variable"
        )

    def test_strict_mode_documentation_exists(self):
        """验证 99_verify_permissions.sql 包含 strict 模式文档"""
        assert "Strict 模式" in self.verify_sql or "strict" in self.verify_sql.lower(), (
            "99_verify_permissions.sql should document strict mode"
        )

    def test_temp_table_for_fail_count_aggregation(self):
        """验证 99_verify_permissions.sql 使用临时表汇总 fail_count"""
        assert "_verify_fail_counts" in self.verify_sql, (
            "99_verify_permissions.sql should use _verify_fail_counts temp table"
        )

    def test_raise_exception_in_strict_mode(self):
        """验证 99_verify_permissions.sql 在 strict 模式下会 RAISE EXCEPTION"""
        assert "RAISE EXCEPTION" in self.verify_sql, (
            "99_verify_permissions.sql should RAISE EXCEPTION in strict mode"
        )
        assert "VERIFY_STRICT_FAILED" in self.verify_sql, (
            "99_verify_permissions.sql should use VERIFY_STRICT_FAILED error code"
        )

    def test_strict_mode_triggers_on_warn(self):
        """验证 strict 模式下 WARN 也会触发异常（CI 门禁）"""
        # 检查 strict 模式条件包含 warn_count
        assert "v_total_warn > 0" in self.verify_sql, (
            "99_verify_permissions.sql should check warn_count in strict mode condition"
        )
        # 检查条件使用 OR 逻辑（FAIL 或 WARN 任一即触发）
        assert "v_total_fail > 0 OR v_total_warn > 0" in self.verify_sql, (
            "99_verify_permissions.sql should use OR logic for FAIL/WARN in strict mode"
        )

    def test_strict_mode_error_message_includes_warn_count(self):
        """验证 strict 模式错误消息包含 WARN 计数"""
        # 检查 RAISE EXCEPTION 消息中包含 WARN 计数
        assert "% 项 WARN" in self.verify_sql, (
            "99_verify_permissions.sql RAISE EXCEPTION should include WARN count"
        )

    def test_fail_count_inserted_to_temp_table(self):
        """验证各 section 将 fail_count 插入到临时表"""
        # 检查至少有 5 个 INSERT INTO _verify_fail_counts
        insert_count = self.verify_sql.count("INSERT INTO _verify_fail_counts")
        assert insert_count >= 5, (
            f"Expected at least 5 INSERT INTO _verify_fail_counts, found {insert_count}"
        )

    def test_total_fail_count_aggregation(self):
        """验证最终汇总 fail_count"""
        assert "SUM(fail_count)" in self.verify_sql, (
            "99_verify_permissions.sql should aggregate fail_count with SUM"
        )

    def test_total_warn_count_aggregation(self):
        """验证最终汇总 warn_count"""
        assert "SUM(warn_count)" in self.verify_sql, (
            "99_verify_permissions.sql should aggregate warn_count with SUM"
        )

    def test_warn_sections_listed_in_output(self):
        """验证有 WARN 的 section 会在输出中列出"""
        assert "有 WARN 的验证项" in self.verify_sql, (
            "99_verify_permissions.sql should list sections with WARN in output"
        )

    def test_cleanup_temp_table(self):
        """验证清理临时表"""
        assert "DROP TABLE IF EXISTS _verify_fail_counts" in self.verify_sql, (
            "99_verify_permissions.sql should cleanup _verify_fail_counts temp table"
        )


class TestStrictModeCliIntegration:
    """验证 strict 模式 CLI 集成（不需要数据库连接的测试）"""

    def test_verify_strict_parameter_in_migrate_module(self):
        """验证 migrate.py 中 run_migrate 支持 verify_strict 参数"""
        import inspect

        from engram.logbook.migrate import run_migrate

        sig = inspect.signature(run_migrate)
        params = list(sig.parameters.keys())

        assert "verify_strict" in params, "run_migrate should have verify_strict parameter"

    def test_verify_strict_env_var_support(self):
        """验证环境变量 ENGRAM_VERIFY_STRICT 支持文档"""
        from pathlib import Path

        migrate_py = Path(__file__).parent.parent.parent / "src/engram/logbook/migrate.py"
        content = migrate_py.read_text()

        assert "ENGRAM_VERIFY_STRICT" in content, (
            "migrate.py should support ENGRAM_VERIFY_STRICT environment variable"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
