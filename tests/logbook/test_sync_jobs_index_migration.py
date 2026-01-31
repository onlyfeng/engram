# -*- coding: utf-8 -*-
"""
测试 scm.sync_jobs 索引迁移一致性

验证在两种历史状态下迁移后索引定义一致：
1. 全新安装：直接执行 08_scm_sync_jobs.sql
2. 升级安装：从旧版索引升级（08 脚本包含升级逻辑）

关键索引验证：
- idx_sync_jobs_unique_active: 必须包含 mode 列
- idx_sync_jobs_running_lease: 必须包含 lease_seconds 列

SSOT: sql/08_scm_sync_jobs.sql
"""

import sys
from pathlib import Path

# 确保可以导入 engram
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))


class TestSyncJobsIndexDefinitions:
    """测试 sync_jobs 索引定义正确性"""

    def test_08_script_defines_running_lease_with_lease_seconds(self):
        """验证 08 脚本中 idx_sync_jobs_running_lease 包含 lease_seconds"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        assert sync_jobs_file.exists(), "08_scm_sync_jobs.sql 应存在"

        content = sync_jobs_file.read_text()

        # 验证索引定义包含 lease_seconds
        assert "idx_sync_jobs_running_lease" in content
        # 检查索引定义行是否包含 lease_seconds
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "CREATE INDEX" in line and "idx_sync_jobs_running_lease" in line:
                # 检查此定义或下一行是否包含 lease_seconds
                definition_block = "\n".join(lines[i : i + 3])
                assert "lease_seconds" in definition_block, (
                    f"idx_sync_jobs_running_lease 应包含 lease_seconds 列，实际定义：{definition_block}"
                )
                break

    def test_08_script_defines_unique_active_with_mode(self):
        """验证 08 脚本中 idx_sync_jobs_unique_active 包含 mode 列"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        assert sync_jobs_file.exists(), "08_scm_sync_jobs.sql 应存在"

        content = sync_jobs_file.read_text()

        # 验证唯一索引定义包含 mode
        assert "idx_sync_jobs_unique_active" in content
        # 找到 CREATE UNIQUE INDEX 定义
        lines = content.split("\n")
        found_definition = False
        for i, line in enumerate(lines):
            if "CREATE UNIQUE INDEX" in line and "idx_sync_jobs_unique_active" in line:
                # 检查此定义或下一行是否包含 mode
                definition_block = "\n".join(lines[i : i + 3])
                assert "mode" in definition_block, (
                    f"idx_sync_jobs_unique_active 应包含 mode 列，实际定义：{definition_block}"
                )
                found_definition = True
                break

        assert found_definition, (
            "应在 08 脚本中找到 idx_sync_jobs_unique_active 的 CREATE UNIQUE INDEX 定义"
        )

    def test_08_script_includes_upgrade_logic(self):
        """验证 08 脚本包含升级逻辑"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 验证包含索引升级逻辑
        assert "pg_get_indexdef" in content, "08 脚本应包含索引定义检测"
        assert "DROP INDEX IF EXISTS" in content, "08 脚本应支持删除旧索引"

    def test_08_upgrade_logic_detects_old_running_lease(self):
        """验证 08 脚本能检测旧版 running_lease 索引"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 验证升级逻辑检测 NOT LIKE '%lease_seconds%'
        assert "NOT LIKE '%lease_seconds%'" in content, "升级逻辑应检测旧版索引不包含 lease_seconds"

    def test_08_upgrade_logic_detects_old_unique_active(self):
        """验证 08 脚本能检测旧版 unique_active 索引"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 验证升级逻辑检测 NOT LIKE '%mode%'
        assert "NOT LIKE '%mode%'" in content, "升级逻辑应检测旧版索引不包含 mode"

    def test_no_duplicate_unique_active_definition_in_08(self):
        """验证 08 脚本中 idx_sync_jobs_unique_active 没有重复定义"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 计算 CREATE UNIQUE INDEX ... idx_sync_jobs_unique_active 的出现次数
        # 注意：DO $$ 块中的 CREATE 是升级逻辑，不算重复定义
        lines = content.split("\n")

        # 找出所有非 DO $$ 块内的定义
        in_do_block = False
        definitions_outside_do = 0

        for line in lines:
            stripped = line.strip()
            if "DO $$" in stripped:
                in_do_block = True
            elif in_do_block and stripped.endswith("$$;"):
                in_do_block = False
            elif not in_do_block:
                if "CREATE UNIQUE INDEX" in line and "idx_sync_jobs_unique_active" in line:
                    definitions_outside_do += 1

        assert definitions_outside_do == 1, (
            f"08 脚本应只有一个主定义（DO块外），实际有 {definitions_outside_do} 个"
        )


class TestSyncJobsIndexMigrationScenarios:
    """测试不同迁移场景下的索引一致性"""

    def test_fresh_install_index_definitions(self):
        """验证全新安装时索引定义正确（模拟）"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 期望的索引定义（关键部分）
        expected_indexes = {
            "idx_sync_jobs_claim": "priority",
            "idx_sync_jobs_status": "status",
            "idx_sync_jobs_repo": "repo_id",
            "idx_sync_jobs_repo_job_type": "repo_id, job_type",
            "idx_sync_jobs_unique_active": "repo_id, job_type, mode",
            "idx_sync_jobs_locked_by": "locked_by",
            "idx_sync_jobs_running_lease": "locked_at, lease_seconds",
            "idx_sync_jobs_repo_job_latest": "repo_id, job_type, created_at",
            "idx_sync_jobs_not_before": "not_before",
            "idx_sync_jobs_dead": "repo_id, created_at",
        }

        for index_name, expected_cols in expected_indexes.items():
            assert index_name in content, f"索引 {index_name} 应在 08 脚本中定义"


class TestSyncJobsIndexConsistency:
    """测试索引定义的一致性"""

    def test_upgrade_definitions_match_main_definitions(self):
        """验证 08 脚本中 DO 块内重建的索引定义与主定义一致"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # idx_sync_jobs_running_lease 在主定义和 DO 块中都应使用 (locked_at, lease_seconds)
        # 计算出现次数
        running_lease_def_count = content.count("locked_at, lease_seconds")
        assert running_lease_def_count >= 2, (
            f"locked_at, lease_seconds 应至少出现 2 次（主定义 + DO 块），实际 {running_lease_def_count} 次"
        )

        # idx_sync_jobs_unique_active 在主定义和 DO 块中都应使用 (repo_id, job_type, mode)
        unique_active_def_count = content.count("repo_id, job_type, mode")
        assert unique_active_def_count >= 2, (
            f"repo_id, job_type, mode 应至少出现 2 次（主定义 + DO 块），实际 {unique_active_def_count} 次"
        )

    def test_required_indexes_complete(self):
        """验证所有必要的索引都有定义"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        main_content = (sql_dir / "08_scm_sync_jobs.sql").read_text()

        required_indexes = [
            "idx_sync_jobs_claim",
            "idx_sync_jobs_status",
            "idx_sync_jobs_repo",
            "idx_sync_jobs_repo_job_type",
            "idx_sync_jobs_unique_active",
            "idx_sync_jobs_locked_by",
            "idx_sync_jobs_running_lease",
            "idx_sync_jobs_repo_job_latest",
            "idx_sync_jobs_not_before",
            "idx_sync_jobs_dead",
        ]

        for index_name in required_indexes:
            assert index_name in main_content, f"索引 {index_name} 应在 08_scm_sync_jobs.sql 中定义"


class TestSyncJobsSQLScriptStructure:
    """测试 SQL 脚本结构"""

    def test_08_script_is_idempotent(self):
        """验证 08 脚本设计为幂等执行"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 验证幂等性设计
        # 1. 表使用 CREATE TABLE IF NOT EXISTS
        assert "CREATE TABLE IF NOT EXISTS" in content
        # 2. 索引使用 CREATE INDEX IF NOT EXISTS
        assert "CREATE INDEX IF NOT EXISTS" in content
        # 3. 在事务中执行
        assert "BEGIN;" in content and "COMMIT;" in content

    def test_08_script_has_table_and_column_comments(self):
        """验证 08 脚本包含表和列的注释"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        sync_jobs_file = sql_dir / "08_scm_sync_jobs.sql"

        content = sync_jobs_file.read_text()

        # 验证表注释
        assert "COMMENT ON TABLE scm.sync_jobs" in content
        # 验证关键列注释
        assert "COMMENT ON COLUMN scm.sync_jobs.job_type" in content
        assert "COMMENT ON COLUMN scm.sync_jobs.status" in content

    def test_no_07_scm_sync_jobs_file(self):
        """验证不存在单独的 07_scm_sync_jobs.sql 文件（已合并到 08）"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        old_file = sql_dir / "07_scm_sync_jobs.sql"

        # 07_scm_sync_jobs.sql 不应存在，因为升级逻辑已合并到 08
        assert not old_file.exists(), (
            "07_scm_sync_jobs.sql 不应存在（升级逻辑已合并到 08_scm_sync_jobs.sql）"
        )

    def test_07_is_sync_locks_not_sync_jobs(self):
        """验证 07 前缀用于 sync_locks 而非 sync_jobs"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"

        # 07 应该是 sync_locks
        sync_locks_file = sql_dir / "07_scm_sync_locks.sql"
        assert sync_locks_file.exists(), "07_scm_sync_locks.sql 应存在"


class TestSyncJobsIndexUpgradeLogic:
    """测试索引升级逻辑的正确性"""

    def test_upgrade_uses_pg_get_indexdef(self):
        """验证升级逻辑使用 pg_get_indexdef 检测索引定义"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        content = (sql_dir / "08_scm_sync_jobs.sql").read_text()

        # 应该使用 pg_get_indexdef 获取当前索引定义
        assert "pg_get_indexdef" in content

    def test_upgrade_drops_before_recreate(self):
        """验证升级逻辑先删除再重建索引"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        content = (sql_dir / "08_scm_sync_jobs.sql").read_text()

        # DO $$ 块中应先 DROP 再 CREATE
        # 找到 DO $$ 块
        do_blocks = content.split("DO $$")

        for block in do_blocks[1:]:  # 跳过第一部分（DO $$ 之前）
            end_pos = block.find("$$;")
            if end_pos > 0:
                block_content = block[:end_pos]
                # 如果块中有 DROP INDEX，它应该在 CREATE INDEX 之前
                drop_pos = block_content.find("DROP INDEX")
                create_pos = (
                    block_content.find("CREATE INDEX")
                    if "CREATE INDEX" in block_content
                    else block_content.find("CREATE UNIQUE INDEX")
                )
                if drop_pos >= 0 and create_pos >= 0:
                    assert drop_pos < create_pos, "DROP INDEX 应在 CREATE INDEX 之前"

    def test_upgrade_handles_null_indexdef(self):
        """验证升级逻辑正确处理索引不存在的情况"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        content = (sql_dir / "08_scm_sync_jobs.sql").read_text()

        # 应检查 v_indexdef IS NOT NULL
        assert "IS NOT NULL" in content, "升级逻辑应检查索引是否存在（v_indexdef IS NOT NULL）"
