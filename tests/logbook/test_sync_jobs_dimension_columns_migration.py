# -*- coding: utf-8 -*-
"""
测试 scm.sync_jobs 维度列迁移一致性

验证 11_sync_jobs_dimension_columns.sql 脚本的正确性：
1. 列定义：gitlab_instance, tenant_id
2. 索引定义：idx_sync_jobs_gitlab_instance_active, idx_sync_jobs_tenant_id_active
3. 回填逻辑：仅更新活跃任务（pending/running），不影响历史任务
4. 幂等性：脚本可重复执行

SSOT: sql/11_sync_jobs_dimension_columns.sql
"""

import sys
from pathlib import Path

import pytest

# 确保可以导入 engram
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))


class TestSyncJobsDimensionColumnsDefinitions:
    """测试 sync_jobs 维度列定义正确性"""

    @pytest.fixture
    def sql_content(self):
        """读取 11_sync_jobs_dimension_columns.sql 内容"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_cols_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        assert dimension_cols_file.exists(), "11_sync_jobs_dimension_columns.sql 应存在"
        return dimension_cols_file.read_text()

    def test_11_script_exists(self):
        """验证 11_sync_jobs_dimension_columns.sql 存在"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_cols_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        assert dimension_cols_file.exists(), "11_sync_jobs_dimension_columns.sql 应存在"

    def test_no_09_sync_jobs_dimension_columns(self):
        """验证不存在 09_sync_jobs_dimension_columns.sql（已合并到 11）"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        old_file = sql_dir / "09_sync_jobs_dimension_columns.sql"
        assert not old_file.exists(), (
            "09_sync_jobs_dimension_columns.sql 不应存在（已合并到 11_sync_jobs_dimension_columns.sql）"
        )

    def test_defines_gitlab_instance_column(self, sql_content):
        """验证 11 脚本定义 gitlab_instance 列"""
        assert "gitlab_instance" in sql_content
        # 验证 ADD COLUMN 定义
        assert "ADD COLUMN gitlab_instance text" in sql_content, (
            "gitlab_instance 应定义为 text 类型"
        )

    def test_defines_tenant_id_column(self, sql_content):
        """验证 11 脚本定义 tenant_id 列"""
        assert "tenant_id" in sql_content
        # 验证 ADD COLUMN 定义
        assert "ADD COLUMN tenant_id text" in sql_content, "tenant_id 应定义为 text 类型"

    def test_column_has_comment(self, sql_content):
        """验证列有注释说明"""
        assert "COMMENT ON COLUMN scm.sync_jobs.gitlab_instance" in sql_content
        assert "COMMENT ON COLUMN scm.sync_jobs.tenant_id" in sql_content

    def test_column_addition_uses_if_not_exists_pattern(self, sql_content):
        """验证使用 IF NOT EXISTS 模式添加列"""
        # 应使用 information_schema.columns 检查列是否存在
        assert "information_schema.columns" in sql_content
        assert "column_name = 'gitlab_instance'" in sql_content
        assert "column_name = 'tenant_id'" in sql_content


class TestSyncJobsDimensionIndexDefinitions:
    """测试 sync_jobs 维度索引定义"""

    @pytest.fixture
    def sql_content(self):
        """读取 11_sync_jobs_dimension_columns.sql 内容"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_cols_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        return dimension_cols_file.read_text()

    def test_defines_gitlab_instance_active_index(self, sql_content):
        """验证定义 idx_sync_jobs_gitlab_instance_active 索引"""
        assert "idx_sync_jobs_gitlab_instance_active" in sql_content
        # 验证索引使用条件索引（仅活跃任务）
        assert "pending" in sql_content.lower()
        assert "running" in sql_content.lower()

    def test_defines_tenant_id_active_index(self, sql_content):
        """验证定义 idx_sync_jobs_tenant_id_active 索引"""
        assert "idx_sync_jobs_tenant_id_active" in sql_content

    def test_index_uses_if_not_exists(self, sql_content):
        """验证索引使用 CREATE INDEX IF NOT EXISTS"""
        # 应使用 IF NOT EXISTS 确保幂等
        assert "CREATE INDEX IF NOT EXISTS idx_sync_jobs_gitlab_instance_active" in sql_content
        assert "CREATE INDEX IF NOT EXISTS idx_sync_jobs_tenant_id_active" in sql_content

    def test_index_is_conditional_on_active_status(self, sql_content):
        """验证索引为条件索引，仅针对活跃任务"""
        # 找到索引定义并验证 WHERE 子句
        lines = sql_content.split("\n")

        for i, line in enumerate(lines):
            if "idx_sync_jobs_gitlab_instance_active" in line and "CREATE INDEX" in line:
                # 检查接下来几行的 WHERE 子句
                index_block = "\n".join(lines[i : i + 3])
                assert "pending" in index_block.lower() or "running" in index_block.lower(), (
                    "gitlab_instance 索引应仅针对活跃任务"
                )
                break

    def test_index_filters_null_values(self, sql_content):
        """验证索引过滤 NULL 值"""
        # 索引应该包含 IS NOT NULL 条件
        assert "gitlab_instance IS NOT NULL" in sql_content
        assert "tenant_id IS NOT NULL" in sql_content


class TestSyncJobsDimensionBackfillLogic:
    """测试回填逻辑正确性"""

    @pytest.fixture
    def sql_content(self):
        """读取 11_sync_jobs_dimension_columns.sql 内容"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_cols_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        return dimension_cols_file.read_text()

    def test_backfill_only_active_jobs(self, sql_content):
        """验证回填逻辑仅更新活跃任务"""
        # UPDATE 语句应包含 status IN ('pending', 'running') 条件
        update_sections = sql_content.split("UPDATE scm.sync_jobs")

        for section in update_sections[1:]:  # 跳过第一部分
            # 找到这个 UPDATE 的 WHERE 子句部分
            where_end = section.find(";")
            if where_end > 0:
                update_stmt = section[:where_end]
                # 验证包含状态过滤
                assert "status IN" in update_stmt or "'pending'" in update_stmt, (
                    "回填 UPDATE 应仅针对活跃任务（pending/running）"
                )

    def test_backfill_preserves_existing_values(self, sql_content):
        """验证回填不会覆盖已有值"""
        # UPDATE 语句应包含 IS NULL 条件
        assert "gitlab_instance IS NULL" in sql_content, (
            "回填应仅更新 gitlab_instance 为 NULL 的记录"
        )
        assert "tenant_id IS NULL" in sql_content, "回填应仅更新 tenant_id 为 NULL 的记录"

    def test_gitlab_instance_extraction_from_url(self, sql_content):
        """验证 gitlab_instance 从 URL 提取"""
        # 应使用 REGEXP_REPLACE 或类似函数从 repos.url 提取主机名
        assert "repos" in sql_content.lower()
        # 应处理 URL 格式
        assert "://" in sql_content or "url" in sql_content.lower()

    def test_tenant_id_extraction_from_project_key(self, sql_content):
        """验证 tenant_id 从 project_key 提取"""
        # 应使用 SPLIT_PART 或类似函数从 project_key 提取
        assert "project_key" in sql_content
        # 应处理 "group/project" 格式
        assert "SPLIT_PART" in sql_content or "/" in sql_content

    def test_backfill_fallback_to_payload_json(self, sql_content):
        """验证回填回退到 payload_json"""
        # 应有从 payload_json 读取 tenant_id 的回退逻辑
        assert "payload_json" in sql_content, "应支持从 payload_json 回退读取 tenant_id"

    def test_backfill_handles_missing_repos(self, sql_content):
        """验证回填处理 repos 记录不存在的情况"""
        # 应检查 repos 记录是否存在（边界保护）
        assert "EXISTS" in sql_content, "回填应检查关联的 repos 记录是否存在"


class TestSyncJobsDimensionScriptIdempotency:
    """测试脚本幂等性"""

    @pytest.fixture
    def sql_content(self):
        """读取 11_sync_jobs_dimension_columns.sql 内容"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_cols_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        return dimension_cols_file.read_text()

    def test_script_uses_transaction(self, sql_content):
        """验证脚本在事务中执行"""
        assert "BEGIN;" in sql_content
        assert "COMMIT;" in sql_content

    def test_column_addition_is_idempotent(self, sql_content):
        """验证列添加是幂等的"""
        # 使用 DO $$ 块和 IF NOT EXISTS 检查
        assert "DO $$" in sql_content
        assert "IF NOT EXISTS" in sql_content

    def test_index_creation_is_idempotent(self, sql_content):
        """验证索引创建是幂等的"""
        assert "CREATE INDEX IF NOT EXISTS" in sql_content

    def test_backfill_is_idempotent(self, sql_content):
        """验证回填是幂等的（仅更新 NULL 值）"""
        # UPDATE 应仅在目标列为 NULL 时执行
        # 找到所有 UPDATE 语句并验证
        update_count = sql_content.count("UPDATE scm.sync_jobs")
        null_check_count = sql_content.count("IS NULL")

        # 每个回填 UPDATE 应该有对应的 NULL 检查
        assert null_check_count >= update_count - 1, "每个回填 UPDATE 应检查目标列是否为 NULL"


class TestSyncJobsDimensionDataIntegrity:
    """测试数据完整性检查"""

    @pytest.fixture
    def sql_content(self):
        """读取 11_sync_jobs_dimension_columns.sql 内容"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"
        dimension_cols_file = sql_dir / "11_sync_jobs_dimension_columns.sql"
        return dimension_cols_file.read_text()

    def test_includes_orphan_check(self, sql_content):
        """验证包含孤立任务检查"""
        # 应检查 repo_id 不存在于 repos 表的情况
        assert "orphan" in sql_content.lower() or "NOT EXISTS" in sql_content, (
            "应检查孤立的 sync_jobs 记录"
        )

    def test_orphan_check_is_warning_not_error(self, sql_content):
        """验证孤立检查是警告而非错误"""
        # 应使用 RAISE WARNING 而非 RAISE EXCEPTION
        assert "RAISE WARNING" in sql_content, "孤立任务检查应发出警告而非阻塞迁移"

    def test_script_has_verification_comments(self, sql_content):
        """验证脚本包含验证 SQL 注释"""
        # 应包含验证查询（被注释）
        assert "验证" in sql_content or "verify" in sql_content.lower()


class TestSyncJobsDimensionMigratePyIntegration:
    """测试与 migrate.py 的集成"""

    def test_11_in_ddl_script_prefixes(self):
        """验证 11 在 DDL_SCRIPT_PREFIXES 中"""
        from engram.logbook.migrate import DDL_SCRIPT_PREFIXES

        assert "11" in DDL_SCRIPT_PREFIXES, "11 应在 DDL_SCRIPT_PREFIXES 中以确保自动执行"

    def test_migrate_comment_mentions_11(self):
        """验证 migrate.py 注释说明 11 的用途"""
        migrate_file = (
            Path(__file__).parent.parent.parent / "src" / "engram" / "logbook" / "migrate.py"
        )
        content = migrate_file.read_text()

        assert "11" in content
        assert "sync_jobs_dimension" in content.lower() or "dimension" in content.lower(), (
            "migrate.py 应注明 11 对应 sync_jobs_dimension_columns"
        )


class TestSyncJobsDimensionNoOld09File:
    """测试 09 版本文件已被清理"""

    def test_09_is_evidence_uri_not_dimension_columns(self):
        """验证 09 现在是 evidence_uri_column 而非 dimension_columns"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"

        # 09 应该是 evidence_uri_column
        evidence_uri_file = sql_dir / "09_evidence_uri_column.sql"
        assert evidence_uri_file.exists(), "09_evidence_uri_column.sql 应存在"

        # 09 不应该是 sync_jobs_dimension_columns
        old_dim_file = sql_dir / "09_sync_jobs_dimension_columns.sql"
        assert not old_dim_file.exists(), (
            "09_sync_jobs_dimension_columns.sql 不应存在（已合并到 11）"
        )

    def test_sql_dir_has_no_duplicate_dimension_columns_files(self):
        """验证 sql 目录没有重复的 dimension_columns 文件"""
        sql_dir = Path(__file__).parent.parent.parent / "sql"

        dimension_files = list(sql_dir.glob("*dimension_columns*.sql"))
        assert len(dimension_files) == 1, (
            f"应只有一个 dimension_columns 文件，实际: {[f.name for f in dimension_files]}"
        )
        assert dimension_files[0].name == "11_sync_jobs_dimension_columns.sql"
