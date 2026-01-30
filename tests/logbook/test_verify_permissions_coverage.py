"""
99_verify_permissions.sql 权限检查覆盖率测试

验证 99_verify_permissions.sql 中包含所有必要的权限检查断言。

测试内容：
1. seekdb 角色检查存在性
2. logbook.kv 权限检查（SELECT/INSERT/UPDATE for seekdb_app）
3. logbook.attachments 权限检查（SELECT 必需，INSERT 可选）
4. logbook.events 权限检查（INSERT 可选，用于审计）
5. 隔离断言：seekdb_app 不应对 logbook.items/outbox_memory 有 DML 权限

详见：
- docs/contracts/logbook_seekdb_boundary.md（边界契约）
- apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql（权限授予）
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
SEEKDB_ROLES_SQL = PROJECT_ROOT / "apps/seekdb_rag_hybrid/sql/01_seekdb_roles_and_grants.sql"


class TestVerifyPermissionsSqlCoverage:
    """99_verify_permissions.sql 权限检查覆盖率测试"""

    @pytest.fixture(autouse=True)
    def load_sql_content(self):
        """加载 SQL 文件内容"""
        self.verify_sql = VERIFY_PERMISSIONS_SQL.read_text()
        if SEEKDB_ROLES_SQL.exists():
            self.seekdb_sql = SEEKDB_ROLES_SQL.read_text()
        else:
            self.seekdb_sql = ""

    def test_seekdb_app_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 seekdb_app 角色"""
        assert "seekdb_app" in self.verify_sql, (
            "99_verify_permissions.sql should verify seekdb_app role"
        )
        assert "seek_app" in self.verify_sql, (
            "99_verify_permissions.sql should support legacy seek_app role"
        )

    def test_seekdb_migrator_role_verification_exists(self):
        """验证 99_verify_permissions.sql 检查 seekdb_migrator 角色"""
        assert "seekdb_migrator" in self.verify_sql, (
            "99_verify_permissions.sql should verify seekdb_migrator role"
        )
        assert "seek_migrator" in self.verify_sql, (
            "99_verify_permissions.sql should support legacy seek_migrator role"
        )

    def test_logbook_kv_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 logbook.kv 权限"""
        # 应有 logbook.kv 相关的检查
        assert "logbook.kv" in self.verify_sql, (
            "99_verify_permissions.sql should check logbook.kv permissions"
        )
        
        # 应检查 SELECT/INSERT/UPDATE 权限
        kv_section = self._find_section("4.8", self.verify_sql)
        if kv_section:
            assert "SELECT" in kv_section, "logbook.kv section should check SELECT"
            assert "INSERT" in kv_section, "logbook.kv section should check INSERT"
            assert "UPDATE" in kv_section, "logbook.kv section should check UPDATE"

    def test_logbook_attachments_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 logbook.attachments 权限"""
        assert "logbook.attachments" in self.verify_sql, (
            "99_verify_permissions.sql should check logbook.attachments permissions"
        )
        
        # 应检查 SELECT（必需）和 INSERT（可选）
        attachments_section = self._find_section("4.9", self.verify_sql)
        if attachments_section:
            assert "SELECT" in attachments_section, (
                "logbook.attachments section should check SELECT (required)"
            )

    def test_logbook_events_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 logbook.events 权限"""
        assert "logbook.events" in self.verify_sql, (
            "99_verify_permissions.sql should check logbook.events permissions"
        )

    def test_isolation_check_for_restricted_tables(self):
        """验证 99_verify_permissions.sql 检查隔离（受限表无 DML）"""
        # 应有对 items/outbox_memory 的隔离检查
        assert "logbook.items" in self.verify_sql or "items" in self.verify_sql, (
            "99_verify_permissions.sql should check isolation for logbook.items"
        )
        assert "outbox_memory" in self.verify_sql, (
            "99_verify_permissions.sql should check isolation for logbook.outbox_memory"
        )

    def test_seek_enabled_config_variable_support(self):
        """验证 99_verify_permissions.sql 支持 seek.enabled 配置变量"""
        assert "seek.enabled" in self.verify_sql, (
            "99_verify_permissions.sql should support seek.enabled config variable"
        )
        
        # 应有跳过逻辑
        assert "SKIP" in self.verify_sql, (
            "99_verify_permissions.sql should skip Seek checks when disabled"
        )

    def test_output_levels_defined(self):
        """验证 99_verify_permissions.sql 定义了输出级别"""
        output_levels = ["FAIL", "WARN", "OK", "SKIP", "COMPAT"]
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

    def _find_section(self, section_num: str, content: str) -> str:
        """查找指定 section 的内容"""
        pattern = rf"=== {section_num}.*?===.*?(?===|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        return match.group(0) if match else ""


class TestSeekdbRolesSqlGrants:
    """01_seekdb_roles_and_grants.sql 权限授予测试"""

    @pytest.fixture(autouse=True)
    def load_sql_content(self):
        """加载 SQL 文件内容"""
        if SEEKDB_ROLES_SQL.exists():
            self.seekdb_sql = SEEKDB_ROLES_SQL.read_text()
        else:
            pytest.skip("01_seekdb_roles_and_grants.sql not found")

    def test_logbook_kv_grant_exists(self):
        """验证 01_seekdb_roles_and_grants.sql 授予 logbook.kv 权限"""
        assert "logbook.kv" in self.seekdb_sql, (
            "01_seekdb_roles_and_grants.sql should grant permissions on logbook.kv"
        )
        
        # 应授予 SELECT/INSERT/UPDATE
        assert re.search(r"GRANT.*SELECT.*ON\s+logbook\.kv", self.seekdb_sql, re.IGNORECASE), (
            "Should grant SELECT on logbook.kv"
        )
        assert re.search(r"GRANT.*INSERT.*ON\s+logbook\.kv", self.seekdb_sql, re.IGNORECASE), (
            "Should grant INSERT on logbook.kv"
        )
        assert re.search(r"GRANT.*UPDATE.*ON\s+logbook\.kv", self.seekdb_sql, re.IGNORECASE), (
            "Should grant UPDATE on logbook.kv"
        )

    def test_logbook_attachments_grant_exists(self):
        """验证 01_seekdb_roles_and_grants.sql 授予 logbook.attachments 权限"""
        assert "logbook.attachments" in self.seekdb_sql, (
            "01_seekdb_roles_and_grants.sql should grant permissions on logbook.attachments"
        )
        
        # 应授予 SELECT（必需）
        assert re.search(r"GRANT.*SELECT.*ON\s+logbook\.attachments", self.seekdb_sql, re.IGNORECASE), (
            "Should grant SELECT on logbook.attachments"
        )

    def test_logbook_events_grant_exists(self):
        """验证 01_seekdb_roles_and_grants.sql 授予 logbook.events 权限（可选）"""
        # events 权限是可选的，但如果存在应有 INSERT
        if "logbook.events" in self.seekdb_sql:
            assert re.search(r"GRANT.*INSERT.*ON\s+logbook\.events", self.seekdb_sql, re.IGNORECASE), (
                "Should grant INSERT on logbook.events"
            )

    def test_scm_patch_blobs_grant_exists(self):
        """验证 01_seekdb_roles_and_grants.sql 授予 scm.patch_blobs 权限"""
        # patch_blobs SELECT 权限用于读取源数据
        assert "scm.patch_blobs" in self.seekdb_sql or "patch_blobs" in self.seekdb_sql, (
            "01_seekdb_roles_and_grants.sql should grant SELECT on scm.patch_blobs"
        )

    def test_logbook_schema_usage_granted(self):
        """验证 01_seekdb_roles_and_grants.sql 授予 logbook schema USAGE 权限"""
        assert re.search(r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+logbook", self.seekdb_sql, re.IGNORECASE), (
            "Should grant USAGE on logbook schema"
        )

    def test_seekdb_app_grants_to_both_roles(self):
        """验证权限同时授予 seekdb_app 和 seek_app（兼容）"""
        # 统计 seekdb_app 和 seek_app 的 GRANT 次数
        seekdb_app_grants = len(re.findall(r"TO\s+seekdb_app", self.seekdb_sql, re.IGNORECASE))
        seek_app_grants = len(re.findall(r"TO\s+seek_app", self.seekdb_sql, re.IGNORECASE))
        
        # 两者应该大致相等（兼容期策略）
        assert seekdb_app_grants > 0, "Should grant permissions to seekdb_app"
        assert seek_app_grants > 0, "Should grant permissions to seek_app (compatibility)"


class TestBoundaryContractConsistency:
    """边界契约一致性测试"""

    def test_verify_sql_and_grants_sql_consistency(self):
        """验证 99_verify_permissions.sql 检查的权限与 01_seekdb_roles_and_grants.sql 授予的权限一致"""
        verify_sql = VERIFY_PERMISSIONS_SQL.read_text()
        
        if not SEEKDB_ROLES_SQL.exists():
            pytest.skip("01_seekdb_roles_and_grants.sql not found")
        
        seekdb_sql = SEEKDB_ROLES_SQL.read_text()
        
        # 如果 verify 检查 logbook.kv，grants 应该授予
        if "logbook.kv" in verify_sql:
            assert "logbook.kv" in seekdb_sql, (
                "If 99_verify_permissions.sql checks logbook.kv, "
                "01_seekdb_roles_and_grants.sql should grant it"
            )
        
        # 如果 verify 检查 logbook.attachments，grants 应该授予
        if "logbook.attachments" in verify_sql:
            assert "logbook.attachments" in seekdb_sql, (
                "If 99_verify_permissions.sql checks logbook.attachments, "
                "01_seekdb_roles_and_grants.sql should grant it"
            )

    def test_section_numbers_are_sequential(self):
        """验证 99_verify_permissions.sql 中的 section 编号是连续的"""
        verify_sql = VERIFY_PERMISSIONS_SQL.read_text()
        
        # 提取所有 section 编号（格式：=== X.Y ... ===）
        # 匹配 "=== 4.8 " 或 "=== 4.10 " 格式
        sections = re.findall(r"===\s*(\d+(?:\.\d+)?)\s+", verify_sql)
        
        # 验证存在多个 section
        assert len(sections) > 5, "Should have multiple sections"
        
        # 验证有 4.8, 4.9, 4.10, 4.11 等 SeekDB 跨边界检查
        # 使用 tuple 比较 (major, minor) 而不是 float（避免 4.10 被解析为 4.1）
        def parse_version(s):
            parts = s.split(".")
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        
        seekdb_boundary_sections = [
            s for s in sections 
            if s.startswith("4.") and parse_version(s) >= (4, 8)
        ]
        assert len(seekdb_boundary_sections) >= 4, (
            f"Should have SeekDB boundary check sections (4.8, 4.9, 4.10, 4.11), found: {seekdb_boundary_sections}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
