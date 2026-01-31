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
import pytest
from pathlib import Path


def get_project_root() -> Path:
    """获取项目根目录"""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "docker-compose.unified.yml").exists():
            return parent
    raise RuntimeError("Cannot find project root with docker-compose.unified.yml")


PROJECT_ROOT = get_project_root()
VERIFY_PERMISSIONS_SQL = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
MAIN_VERIFY_SQL = PROJECT_ROOT / "sql/99_verify_permissions.sql"


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
        assert "TEMP" in self.verify_sql, (
            "99_verify_permissions.sql should check TEMP permission"
        )

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
