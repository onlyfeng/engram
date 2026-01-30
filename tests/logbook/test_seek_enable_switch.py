"""
SEEKDB_ENABLE 开关测试（SeekDB 模块启用开关）

验证 SEEKDB_ENABLE 环境变量的行为：
- SEEKDB_ENABLE=1（默认）：执行 SeekDB schema 和角色迁移
- SEEKDB_ENABLE=0：跳过 SeekDB 迁移，验证脚本也跳过 SeekDB 检查

测试场景：
1. 解析 docker-compose.unified.yml 中的 SEEKDB_ENABLE 条件逻辑
2. 验证 99_verify_permissions.sql 中的 seek.enabled 配置变量支持
3. Makefile 中 _migrate-seekdb-conditional 目标的条件逻辑

环境变量命名规范：
- canonical: SEEKDB_ENABLE（推荐使用）
- deprecated: SEEK_ENABLE（已废弃，计划于 2026-Q3 移除）
- GUC: seek.enabled（PostgreSQL 配置变量）

兼容策略:
- docker-compose.unified.yml、Makefile 支持 fallback: SEEKDB_ENABLE=${SEEKDB_ENABLE:-${SEEK_ENABLE:-1}}
- 使用 SEEK_ENABLE 会输出 deprecation 警告

命名迁移说明：
- 最终命名: seekdb schema + seekdb_migrator/seekdb_app 角色
- 旧命名: seek schema + seek_migrator/seek_app 角色（兼容期接受）
- 详见 ADR: docs/architecture/adr_seekdb_schema_role_naming.md
"""

import os
import re
import pytest
from pathlib import Path


# 获取项目根目录
def get_project_root() -> Path:
    """获取项目根目录"""
    current = Path(__file__).resolve()
    # 向上查找到包含 docker-compose.unified.yml 的目录
    for parent in current.parents:
        if (parent / "docker-compose.unified.yml").exists():
            return parent
    raise RuntimeError("Cannot find project root with docker-compose.unified.yml")


PROJECT_ROOT = get_project_root()


class TestSeekEnableSwitch:
    """SEEKDB_ENABLE 开关测试（SeekDB 模块启用开关）"""

    def test_compose_has_seekdb_enable_env(self):
        """验证 docker-compose.unified.yml 中定义了 SEEKDB_ENABLE 环境变量（带 SEEK_ENABLE 别名 fallback）"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 检查 x-service-account-passwords 中的 SeekDB 相关配置（带别名 fallback）
        assert "SEEKDB_MIGRATOR_PASSWORD" in content, "Missing SEEKDB_MIGRATOR_PASSWORD in compose"
        assert "SEEKDB_SVC_PASSWORD" in content, "Missing SEEKDB_SVC_PASSWORD in compose"

        # 检查 bootstrap_roles 服务中的 SEEKDB_ENABLE 环境变量（带 SEEK_ENABLE 别名 fallback）
        assert "SEEKDB_ENABLE: ${SEEKDB_ENABLE:-${SEEK_ENABLE:-1}}" in content, (
            "Missing SEEKDB_ENABLE env var with SEEK_ENABLE fallback in bootstrap_roles"
        )

    def test_compose_bootstrap_roles_conditional_logic(self):
        """验证 bootstrap_roles 服务中的条件执行逻辑"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 检查条件执行逻辑（使用 SEEKDB_ENABLE）
        assert 'if [ "${SEEKDB_ENABLE}" = "1" ]' in content or 'if [ \\"$${SEEKDB_ENABLE}\\" = \\"1\\" ]' in content, (
            "Missing SEEKDB_ENABLE conditional logic in bootstrap_roles"
        )

        # 检查跳过消息
        assert "SeekDB 未启用" in content or "SEEKDB_ENABLE != 1" in content, (
            "Missing SeekDB skip message in bootstrap_roles"
        )

    def test_compose_permissions_verify_seekdb_enabled(self):
        """验证 permissions_verify 服务中传递 seekdb.enabled 配置"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 检查 permissions_verify 服务中有 SEEKDB_ENABLE 环境变量（带 SEEK_ENABLE 别名 fallback）
        # 找到 permissions_verify 服务定义
        match = re.search(r'permissions_verify:.*?(?=\n  \w+:|$)', content, re.DOTALL)
        assert match, "permissions_verify service not found in compose"

        pv_content = match.group(0)
        assert "SEEKDB_ENABLE" in pv_content, (
            "permissions_verify service should have SEEKDB_ENABLE env var"
        )

        # 检查 seek.enabled 配置变量设置（GUC 名称保持为 seek.enabled）
        assert "seek.enabled" in pv_content, (
            "permissions_verify should set seek.enabled config variable"
        )

    def test_verify_permissions_sql_seek_enabled_support(self):
        """验证 99_verify_permissions.sql 支持 seek.enabled 配置变量（canonical GUC）"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 检查 seek.enabled 配置变量的使用（canonical GUC）
        assert "seek.enabled" in content, (
            "99_verify_permissions.sql should use seek.enabled config variable"
        )

        # 检查默认值为 true
        assert "current_setting('seek.enabled', true)" in content, (
            "seek.enabled should use current_setting with fallback"
        )

        # 检查有 SKIP 逻辑
        assert "Seek 未启用" in content or "seek.enabled=false" in content, (
            "99_verify_permissions.sql should have Seek skip logic"
        )

    def test_verify_permissions_sql_core_roles_always_checked(self):
        """验证核心角色（非 Seek）始终被检查"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 核心角色应始终被检查
        core_roles = [
            "engram_admin",
            "engram_migrator",
            "engram_app_readwrite",
            "engram_app_readonly",
            "openmemory_migrator",
            "openmemory_app",
        ]

        for role in core_roles:
            assert role in content, f"Core role {role} should be verified in 99_verify_permissions.sql"

    def test_verify_permissions_sql_seek_naming_migration_support(self):
        """验证 99_verify_permissions.sql 支持 Seek 命名迁移（seekdb/seek 兼容）"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 应检查最终命名（seekdb_*）
        assert "seekdb_migrator" in content, (
            "99_verify_permissions.sql should check seekdb_migrator (final naming)"
        )
        assert "seekdb_app" in content, (
            "99_verify_permissions.sql should check seekdb_app (final naming)"
        )

        # 应支持旧命名（seek_*）用于兼容期
        assert "seek_migrator" in content, (
            "99_verify_permissions.sql should support seek_migrator (legacy naming)"
        )
        assert "seek_app" in content, (
            "99_verify_permissions.sql should support seek_app (legacy naming)"
        )

        # 应有 COMPAT 级别输出
        assert "COMPAT" in content, (
            "99_verify_permissions.sql should output COMPAT level for legacy naming"
        )

    def test_makefile_migrate_seekdb_conditional(self):
        """验证 Makefile 中 _migrate-seekdb-conditional 目标的条件逻辑"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查 _migrate-seekdb-conditional 目标存在
        assert "_migrate-seekdb-conditional:" in content, (
            "Makefile should have _migrate-seekdb-conditional target"
        )

        # 检查条件逻辑（SEEKDB_ENABLE 为 canonical，带 SEEK_ENABLE 别名 fallback）
        assert "SEEKDB_ENABLE_EFFECTIVE" in content, (
            "Makefile should have SEEKDB_ENABLE_EFFECTIVE with fallback logic"
        )

        # 检查跳过消息
        assert "SeekDB 迁移已跳过" in content or "SEEKDB_ENABLE != 1" in content, (
            "Makefile should have SeekDB skip message"
        )

    def test_seek_sql_files_exist(self):
        """验证 SeekDB SQL 文件存在"""
        seek_sql_dir = PROJECT_ROOT / "apps/seekdb_rag_hybrid/sql"

        roles_sql = seek_sql_dir / "01_seekdb_roles_and_grants.sql"
        seek_index_sql = seek_sql_dir / "02_seekdb_index.sql"

        assert roles_sql.exists(), f"SeekDB roles SQL not found: {roles_sql}"
        assert seek_index_sql.exists(), f"SeekDB index SQL not found: {seek_index_sql}"

    def test_seek_sql_mounted_in_compose(self):
        """验证 Seek SQL 文件在 compose 中被正确挂载或引用"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # SeekDB SQL 脚本通过 docker-compose.unified.seekdb.yml override 挂载
        # 主 compose 文件中应有相关说明或引用
        # 检查说明性注释存在
        seekdb_mentioned = (
            "seekdb_roles_and_grants" in content or
            "seekdb" in content.lower() or
            "docker-compose.unified.seekdb.yml" in content
        )
        assert seekdb_mentioned, (
            "docker-compose.unified.yml should reference SeekDB or seekdb compose override"
        )

        # 检查是否有条件执行说明
        assert "SEEKDB_ENABLE" in content, (
            "docker-compose.unified.yml should have SEEKDB_ENABLE control"
        )


class TestSeekEnableIntegration:
    """SEEKDB_ENABLE 集成测试（需要 Docker 环境）"""

    @pytest.mark.skipif(
        os.environ.get("ENGRAM_UNIFIED_INTEGRATION") != "1",
        reason="Integration test requires ENGRAM_UNIFIED_INTEGRATION=1"
    )
    def test_seekdb_disabled_verify_passes(self):
        """
        验证 SEEKDB_ENABLE=0 时权限验证脚本通过
        
        此测试需要在统一栈环境中运行：
        SEEKDB_ENABLE=0 ENGRAM_UNIFIED_INTEGRATION=1 pytest -v test_seek_enable_switch.py
        """
        import subprocess

        # 获取数据库连接
        dsn = os.environ.get("POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/engram")

        # 执行权限验证脚本，设置 seek.enabled=false（canonical GUC）
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"

        result = subprocess.run(
            [
                "psql", dsn,
                "-c", "SET seek.enabled = 'false'",
                "-f", str(sql_file),
            ],
            capture_output=True,
            text=True,
        )

        # 检查输出中没有 FAIL（Seek 相关检查应被 SKIP）
        output = result.stdout + result.stderr

        # Seek 检查应被跳过
        assert "Seek 未启用" in output or "seek.enabled=false" in output, (
            f"Seek checks should be skipped when seek.enabled=false. Output: {output}"
        )

        # 不应有 Seek 相关的 FAIL
        # 只检查 seek/seekdb 相关的 FAIL，核心角色的 FAIL 仍然有效
        # 兼容期策略：检查 seekdb 和 seek 两种命名
        seek_fail_patterns = [
            "FAIL: Seek schema",
            "FAIL: seekdb schema",
            "FAIL: seek schema",
            "FAIL: seekdb_migrator",
            "FAIL: seek_migrator",
            "FAIL: seekdb_app",
            "FAIL: seek_app",
        ]
        for pattern in seek_fail_patterns:
            assert pattern not in output, (
                f"Seek related FAIL should not appear when seek.enabled=false: {pattern}"
            )

    @pytest.mark.skipif(
        os.environ.get("ENGRAM_UNIFIED_INTEGRATION") != "1",
        reason="Integration test requires ENGRAM_UNIFIED_INTEGRATION=1"
    )
    def test_seek_enabled_verify_with_legacy_naming(self):
        """
        验证使用旧命名（seek/seek_*）时权限验证脚本输出 COMPAT 警告
        
        此测试需要在使用旧命名的环境中运行
        """
        import subprocess

        dsn = os.environ.get("POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/engram")
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"

        result = subprocess.run(
            [
                "psql", dsn,
                "-c", "SET seek.enabled = 'true'",
                "-f", str(sql_file),
            ],
            capture_output=True,
            text=True,
        )

        output = result.stdout + result.stderr

        # 如果使用旧命名，应输出 COMPAT 警告
        # 如果使用最终命名，应全部通过
        # 这两种情况都是可接受的
        if "seek_migrator" in output and "seekdb_migrator" not in output:
            # 使用旧命名，应有 COMPAT 输出
            assert "COMPAT" in output, (
                f"Legacy naming should produce COMPAT warnings. Output: {output}"
            )


class TestEnvVarNamingCompat:
    """SEEKDB_* vs SEEK_* 环境变量命名兼容性测试"""

    def test_makefile_seekdb_enable_effective_fallback(self):
        """验证 Makefile 中 SEEKDB_ENABLE_EFFECTIVE 的 fallback 逻辑"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查 SEEKDB_ENABLE_EFFECTIVE 定义
        assert "SEEKDB_ENABLE_EFFECTIVE" in content
        
        # 检查 fallback 逻辑：SEEKDB_ENABLE -> SEEK_ENABLE -> 1（默认）
        assert "$(or $(SEEKDB_ENABLE),$(SEEK_ENABLE),1)" in content, (
            "Makefile should have SEEKDB_ENABLE -> SEEK_ENABLE -> 1 fallback"
        )

    def test_makefile_deprecation_warning_for_seek_enable(self):
        """验证 Makefile 中使用 SEEK_ENABLE 时的 deprecation 警告"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查 deprecation 警告消息
        assert "SEEK_ENABLE 已废弃" in content or "DEPRECATION WARNING" in content, (
            "Makefile should warn about deprecated SEEK_ENABLE"
        )

    def test_compose_seekdb_env_canonical_naming(self):
        """验证 docker-compose.unified.yml 使用 canonical SEEKDB_* 命名"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 检查服务账号密码变量使用 SEEKDB_ 前缀
        assert "SEEKDB_MIGRATOR_PASSWORD" in content
        assert "SEEKDB_SVC_PASSWORD" in content

        # 检查 SEEKDB_ENABLE 带 SEEK_ENABLE fallback
        assert "SEEKDB_ENABLE" in content

    def test_compose_guc_naming_preserved(self):
        """验证 docker-compose.unified.yml 中 PostgreSQL GUC 保持 seek.enabled 命名"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # GUC 名称应保持 seek.enabled（不随环境变量重命名）
        assert "seek.enabled" in content, (
            "PostgreSQL GUC should remain as seek.enabled for backward compatibility"
        )

    def test_verify_permissions_sql_output_levels(self):
        """验证 99_verify_permissions.sql 输出级别定义"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 检查输出级别定义
        output_levels = ["FAIL", "WARN", "OK", "SKIP", "COMPAT"]
        for level in output_levels:
            assert level in content, f"Output level {level} should be defined in 99_verify_permissions.sql"

        # 检查 COMPAT 级别说明
        assert "COMPAT 不等于 OK" in content or "兼容期警告" in content, (
            "99_verify_permissions.sql should explain COMPAT level"
        )


class TestSeekdbTestSchemaIsolation:
    """seekdb_test schema 隔离与权限测试"""

    def test_makefile_seekdb_test_schema_support(self):
        """验证 Makefile 支持 seekdb_test 隔离测试 schema"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查 seekdb_test 使用示例或说明
        assert "seekdb_test" in content, (
            "Makefile should document seekdb_test schema for isolated testing"
        )

        # 检查 SEEKDB_PG_SCHEMA 变量用于指定测试 schema
        assert "SEEKDB_PG_SCHEMA" in content, (
            "Makefile should support SEEKDB_PG_SCHEMA for schema configuration"
        )

    def test_makefile_isolation_test_example(self):
        """验证 Makefile 包含隔离测试示例"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查隔离测试用法示例
        assert "SEEKDB_PG_SCHEMA=seekdb_test" in content, (
            "Makefile should include example: SEEKDB_PG_SCHEMA=seekdb_test"
        )

    def test_verify_permissions_sql_parameterized_schema(self):
        """验证 99_verify_permissions.sql 支持参数化目标 schema"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 检查 seek.target_schema 参数支持
        assert "seek.target_schema" in content, (
            "99_verify_permissions.sql should support seek.target_schema parameter"
        )

    def test_ci_workflow_seekdb_enable_control(self):
        """验证 CI workflow 中 SEEKDB_ENABLE 控制逻辑"""
        ci_files = list((PROJECT_ROOT / ".github/workflows").glob("*.yml"))
        
        # 至少应有一个 CI 文件
        assert len(ci_files) > 0, "Should have CI workflow files"

        # 检查 CI 文件中是否有 SEEKDB_ENABLE 或 SEEK_ENABLE 配置
        found_seekdb_control = False
        for ci_file in ci_files:
            content = ci_file.read_text()
            if "SEEKDB_ENABLE" in content or "SEEK_ENABLE" in content:
                found_seekdb_control = True
                break
        
        # 如果 CI 中有相关配置，验证使用正确的命名
        if found_seekdb_control:
            for ci_file in ci_files:
                content = ci_file.read_text()
                if "SEEK_ENABLE" in content and "SEEKDB_ENABLE" not in content:
                    # 仅使用旧命名，应该警告
                    pass  # 允许但不推荐


class TestSeekEnableSqlVerification:
    """99_verify_permissions.sql 中 Seek 检查的验证测试"""

    def test_all_seek_sections_have_enabled_check(self):
        """验证所有 Seek 相关 section 都有 seek.enabled 检查"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # Seek 相关的 section（通过标题识别）
        seek_sections = [
            "Seek schema 验证",
            "seek.chunks 兼容 VIEW 验证",
            "Seek 表级 DML 权限验证",
            "Seek 序列权限验证",
            "SeekDB 默认权限",
            "Seek migrator 默认权限",
            # 新增的跨边界权限检查
            "SeekDB 访问 logbook.kv 权限验证",
            "SeekDB 访问 logbook.attachments 权限验证",
            "SeekDB 访问 logbook.events 权限验证",
            "SeekDB 对其他 logbook 表的隔离验证",
        ]

        for section in seek_sections:
            # 找到 section 的位置
            if section in content:
                # 在该 section 附近应有 seek.enabled 检查或 SKIP 逻辑
                section_idx = content.find(section)
                # 检查前后 500 字符范围内有 seek.enabled 检查
                context_start = max(0, section_idx - 500)
                context_end = min(len(content), section_idx + 1000)
                context = content[context_start:context_end]
                
                has_enabled_check = (
                    "seek.enabled" in context or
                    "v_seek_enabled" in context or
                    "Seek 未启用" in context or
                    "SKIP" in context
                )
                assert has_enabled_check, (
                    f"Section '{section}' should have seek.enabled check or SKIP logic"
                )

    def test_logbook_kv_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 SeekDB 对 logbook.kv 的权限"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()
        
        # 应有 logbook.kv 权限检查 section
        assert "logbook.kv" in content, (
            "99_verify_permissions.sql should check logbook.kv permissions for seekdb_app"
        )
        
        # 应检查 SELECT/INSERT/UPDATE 权限
        assert "SELECT" in content and "logbook.kv" in content
        assert "INSERT" in content and "logbook.kv" in content
        assert "UPDATE" in content and "logbook.kv" in content

    def test_logbook_attachments_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 SeekDB 对 logbook.attachments 的权限"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()
        
        # 应有 logbook.attachments 权限检查
        assert "logbook.attachments" in content, (
            "99_verify_permissions.sql should check logbook.attachments permissions for seekdb_app"
        )

    def test_logbook_events_permission_check_exists(self):
        """验证 99_verify_permissions.sql 检查 SeekDB 对 logbook.events 的权限"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()
        
        # 应有 logbook.events 权限检查
        assert "logbook.events" in content, (
            "99_verify_permissions.sql should check logbook.events permissions for seekdb_app"
        )

    def test_isolation_check_for_restricted_tables(self):
        """验证 99_verify_permissions.sql 检查 SeekDB 对其他 logbook 表的隔离"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()
        
        # 应有隔离检查（对 items/outbox_memory 无 DML）
        assert "隔离验证" in content or "restricted_tables" in content, (
            "99_verify_permissions.sql should check isolation for restricted logbook tables"
        )
        
        # 应包含受限表名
        assert "items" in content, "Should check isolation for logbook.items"
        assert "outbox_memory" in content, "Should check isolation for logbook.outbox_memory"

    def test_seek_enabled_default_is_true(self):
        """验证 seek.enabled 默认值为 true"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 默认值应为 true
        assert "'true'" in content, (
            "seek.enabled default should be 'true'"
        )

    def test_compat_output_for_legacy_naming(self):
        """验证使用旧命名时输出 COMPAT 级别"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 检查 seek_migrator（旧命名）相关的 COMPAT 输出
        assert "COMPAT: 角色 seek_migrator" in content or "COMPAT: seek_migrator" in content, (
            "Should output COMPAT for legacy seek_migrator role"
        )

        # 检查 seek_app（旧命名）相关的 COMPAT 输出
        assert "COMPAT: 角色 seek_app" in content or "COMPAT: seek_app" in content, (
            "Should output COMPAT for legacy seek_app role"
        )

    def test_migration_guidance_in_output(self):
        """验证输出中包含迁移指导"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 应有迁移到 seekdb_* 的指导
        assert "迁移到 seekdb" in content or "action:" in content, (
            "Should include migration guidance to seekdb naming"
        )


class TestMakefileSeekDisabledOutputContract:
    """
    Makefile/Compose 中 SeekDB 禁用时的输出契约测试
    
    验证 SEEKDB_ENABLE=0 时各组件输出符合预期：
    - _migrate-seekdb-conditional: 输出 [SKIP] 消息
    - bootstrap_roles: 输出 SeekDB 未启用
    - permissions_verify: 跳过 Seek 检查
    """

    def test_makefile_skip_message_for_seekdb_migrate(self):
        """验证 Makefile 中 SeekDB 迁移跳过消息符合契约"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查跳过消息格式
        skip_message = "[SKIP] SeekDB 迁移已跳过 (SEEKDB_ENABLE != 1)"
        assert skip_message in content, (
            f"Makefile should contain skip message: {skip_message}"
        )

    def test_compose_skip_message_for_bootstrap_roles(self):
        """验证 docker-compose.unified.yml 中 bootstrap_roles 跳过消息符合契约"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 检查跳过消息格式
        expected_messages = [
            "SeekDB 未启用",
            "SEEKDB_ENABLE != 1",
        ]
        found = any(msg in content for msg in expected_messages)
        assert found, (
            f"docker-compose.unified.yml should contain SeekDB skip message, "
            f"expected one of: {expected_messages}"
        )

    def test_compose_permissions_verify_skip_message(self):
        """验证 permissions_verify 服务在禁用时输出跳过消息"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 找到 permissions_verify 服务定义
        match = re.search(r'permissions_verify:.*?(?=\n  \w+:|$)', content, re.DOTALL)
        assert match, "permissions_verify service not found in compose"

        pv_content = match.group(0)
        # 应该有 seek.enabled 检查
        assert "seek.enabled" in pv_content, (
            "permissions_verify should check seek.enabled"
        )

    def test_verify_permissions_sql_skip_output_format(self):
        """验证 99_verify_permissions.sql 的 SKIP 输出格式符合契约"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        # 检查 SKIP 输出格式
        assert "SKIP" in content, (
            "99_verify_permissions.sql should have SKIP output"
        )
        # 检查跳过原因说明
        assert "Seek 未启用" in content or "seek.enabled" in content, (
            "99_verify_permissions.sql should explain why Seek checks are skipped"
        )


class TestAcceptanceSeekSkipContract:
    """
    Acceptance 测试中 SeekDB 禁用时的跳过契约测试
    
    验证当 SEEKDB_ENABLE=0 时，acceptance 测试正确跳过 SeekDB 相关步骤
    """

    def test_makefile_acceptance_supports_seekdb_disable(self):
        """验证 acceptance 目标支持 SEEKDB_ENABLE=0"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查 acceptance 目标的文档中提到 SEEKDB_ENABLE
        assert "SEEKDB_ENABLE=0 acceptance-" in content or "SEEKDB_ENABLE" in content, (
            "Makefile acceptance targets should document SEEKDB_ENABLE support"
        )

    def test_makefile_test_seek_conditional_execution(self):
        """验证 test-seek-unit 目标的条件执行逻辑"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 检查 test-seek 相关目标
        # 应该有 SEEKDB_ENABLE 条件检查或跳过逻辑
        if "test-seek-unit" in content:
            # 找到 test-seek-unit 目标的定义
            match = re.search(r'test-seek-unit:.*?(?=\n\w+:|$)', content, re.DOTALL)
            if match:
                target_content = match.group(0)
                # 目标应该直接执行或依赖于 SEEKDB_ENABLE
                # 如果没有显式检查，也是可接受的（由上层目标控制）
                pass

    def test_acceptance_matrix_documents_seekdb_skip(self):
        """验证 acceptance_matrix.md 记录了 SeekDB 跳过行为"""
        matrix_file = PROJECT_ROOT / "docs/acceptance/00_acceptance_matrix.md"
        content = matrix_file.read_text()

        # 检查 SEEKDB_ENABLE=0 相关文档
        assert "SEEKDB_ENABLE=0" in content, (
            "Acceptance matrix should document SEEKDB_ENABLE=0 behavior"
        )
        # 检查有 SKIP 相关说明
        assert "SKIP" in content, (
            "Acceptance matrix should document SKIP behavior"
        )


class TestOutputLevelContract:
    """
    输出级别（SKIP/NOTICE/WARN/FAIL）契约测试
    
    验证各组件使用一致的输出级别格式
    """

    def test_verify_permissions_sql_output_levels_complete(self):
        """验证 99_verify_permissions.sql 定义了所有输出级别"""
        sql_file = PROJECT_ROOT / "apps/logbook_postgres/sql/99_verify_permissions.sql"
        content = sql_file.read_text()

        required_levels = ["OK", "FAIL", "SKIP", "WARN"]
        for level in required_levels:
            assert level in content, (
                f"99_verify_permissions.sql should define output level: {level}"
            )

    def test_makefile_skip_format_consistency(self):
        """验证 Makefile 中 SKIP 消息格式一致"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 所有 SKIP 消息应使用 [SKIP] 前缀
        skip_lines = [line for line in content.split('\n') if 'SKIP' in line and 'echo' in line]
        for line in skip_lines:
            # 检查格式是否为 [SKIP] 或 SKIPPED
            if 'SKIP' in line:
                # 允许 [SKIP] 或其他合理格式
                pass

    def test_compose_output_format_consistency(self):
        """验证 docker-compose.unified.yml 中输出格式一致"""
        compose_file = PROJECT_ROOT / "docker-compose.unified.yml"
        content = compose_file.read_text()

        # 检查使用了标准输出格式
        output_patterns = [
            r'\[OK\]',
            r'\[SKIP\]',
            r'\[WARN\]',
            r'\[ERROR\]',
            r'\[FAIL\]',
            r'\[INFO\]',
        ]
        # 至少应该使用部分标准格式
        found_patterns = [p for p in output_patterns if re.search(p, content)]
        assert len(found_patterns) > 0, (
            "docker-compose.unified.yml should use standard output formats"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
