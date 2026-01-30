# -*- coding: utf-8 -*-
"""
test_sync_runs_counts_persistence.py - sync_runs.counts 持久化测试

测试内容:
- 构造 RunFinishPayload 并走实际 DB 写入路径
- 读回 scm.sync_runs.counts 并按 schema 校验关键字段存在/类型正确
- 验证 counts 字段在数据库往返后保持契约一致

测试策略:
- PG-only 集成测试：依赖真实 PostgreSQL 数据库
- 使用 conftest.py 中的 db_conn fixture（自动起 PG 并执行迁移）
- 使用 validate_counts_schema 进行契约校验
"""

import uuid
import pytest

import sys
from pathlib import Path

# 确保可以导入 db 模块和 engram_logbook 包
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from db import (
    insert_sync_run_start,
    insert_sync_run_finish,
    get_sync_run,
    upsert_repo,
)

from engram.logbook.scm_sync_run_contract import (
    RunFinishPayload,
    RunCounts,
    ErrorSummary,
    DegradationSnapshot,
    build_run_finish_payload,
    build_run_finish_payload_from_result,
    build_payload_for_success,
    build_payload_for_no_data,
    build_payload_for_exception,
    RunStatus,
    validate_run_finish_payload,
)

from engram.logbook.sync_run_counts import (
    build_counts,
    validate_counts_schema,
    COUNTS_REQUIRED_FIELDS,
    COUNTS_OPTIONAL_FIELDS,
    COUNTS_LIMITER_FIELDS,
)


# ============ counts 持久化基本测试 ============


class TestCountsPersistenceBasic:
    """测试 counts 字段的基本持久化"""

    def test_counts_persistence_roundtrip(self, db_conn):
        """
        测试 counts 字段的完整往返：写入 -> 读回 -> schema 校验
        
        验证:
        1. counts 字典可以正确写入数据库
        2. 读回的 counts 包含所有写入的字段
        3. 读回的 counts 通过 validate_counts_schema 校验
        """
        # Arrange: 创建 repo 和 run
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/counts-persistence-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # 构造 counts（使用 build_counts 确保字段名正确）
        input_counts = build_counts(
            synced_count=100,
            diff_count=95,
            bulk_count=5,
            degraded_count=2,
            total_requests=150,
            total_429_hits=3,
            timeout_count=1,
            avg_wait_time_ms=200,
        )
        
        # Act: 写入 run start
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        
        # Act: 写入 run finish（包含 counts）
        result = insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=input_counts,
            cursor_after={"last_commit_sha": "abc123"},
        )
        
        assert result is True, "insert_sync_run_finish 应返回 True"
        
        # Act: 读回记录
        record = get_sync_run(db_conn, run_id)
        
        # Assert: 记录存在
        assert record is not None, "应能读回 sync_run 记录"
        
        # Assert: counts 字段存在且是字典
        persisted_counts = record.get("counts")
        assert persisted_counts is not None, "counts 字段不应为 None"
        assert isinstance(persisted_counts, dict), f"counts 应为 dict，实际: {type(persisted_counts)}"
        
        # Assert: 使用 validate_counts_schema 验证
        is_valid, errors, warnings = validate_counts_schema(persisted_counts)
        assert is_valid, f"持久化的 counts 应通过 schema 校验，错误: {errors}"
        
        # Assert: 关键字段值正确
        assert persisted_counts["synced_count"] == 100
        assert persisted_counts["diff_count"] == 95
        assert persisted_counts["total_requests"] == 150

    def test_counts_persistence_with_payload_builder(self, db_conn):
        """
        测试使用 RunFinishPayload builder 构造的 counts 持久化
        
        验证通过 build_run_finish_payload 构造的 payload 可以正确持久化。
        """
        # Arrange
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/payload-builder-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # 使用 build_run_finish_payload 构造 payload
        payload = build_run_finish_payload(
            status=RunStatus.COMPLETED.value,
            counts={"synced_count": 50, "diff_count": 45, "scanned_count": 100},
            cursor_after={"last_mr_iid": 999},
        )
        
        # Act: 写入
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_mrs",
            mode="incremental",
        )
        
        payload_dict = payload.to_dict()
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status=payload_dict["status"],
            counts=payload_dict["counts"],
            cursor_after=payload_dict.get("cursor_after"),
        )
        
        # Act: 读回
        record = get_sync_run(db_conn, run_id)
        
        # Assert
        assert record is not None
        persisted_counts = record["counts"]
        
        is_valid, errors, warnings = validate_counts_schema(persisted_counts)
        assert is_valid, f"使用 builder 的 counts 应通过校验，错误: {errors}"
        
        assert persisted_counts["synced_count"] == 50
        assert persisted_counts["scanned_count"] == 100


# ============ counts 各脚本场景测试 ============


class TestCountsPersistenceScripts:
    """测试各同步脚本场景的 counts 持久化"""

    def test_gitlab_commits_counts_persistence(self, db_conn):
        """
        GitLab Commits 脚本的 counts 持久化
        
        模拟 scm_sync_gitlab_commits.py 的调用场景。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/gitlab-commits-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # GitLab Commits 典型的 counts
        counts = build_counts(
            synced_count=100,
            diff_count=95,
            bulk_count=5,
            degraded_count=3,
            diff_none_count=2,
            skipped_count=10,
            total_requests=200,
            total_429_hits=2,
            timeout_count=1,
            avg_wait_time_ms=150,
        )
        
        # 写入
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        # 读回验证
        record = get_sync_run(db_conn, run_id)
        persisted_counts = record["counts"]
        
        is_valid, errors, warnings = validate_counts_schema(persisted_counts)
        assert is_valid, f"GitLab Commits counts 应通过校验，错误: {errors}"
        
        # 验证关键字段
        assert persisted_counts["synced_count"] == 100
        assert persisted_counts["diff_count"] == 95
        assert persisted_counts["bulk_count"] == 5
        assert persisted_counts["degraded_count"] == 3

    def test_gitlab_mrs_counts_persistence(self, db_conn):
        """
        GitLab MRs 脚本的 counts 持久化
        
        模拟 scm_sync_gitlab_mrs.py 的调用场景。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/gitlab-mrs-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # GitLab MRs 典型的 counts
        counts = build_counts(
            synced_count=30,
            scanned_count=100,
            inserted_count=25,
            skipped_count=5,
            total_requests=150,
            total_429_hits=0,
            timeout_count=0,
            avg_wait_time_ms=100,
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_mrs",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        record = get_sync_run(db_conn, run_id)
        persisted_counts = record["counts"]
        
        is_valid, errors, _ = validate_counts_schema(persisted_counts)
        assert is_valid, f"GitLab MRs counts 应通过校验，错误: {errors}"
        
        assert persisted_counts["scanned_count"] == 100
        assert persisted_counts["inserted_count"] == 25

    def test_gitlab_reviews_counts_persistence(self, db_conn):
        """
        GitLab Reviews 脚本的 counts 持久化
        
        模拟 scm_sync_gitlab_reviews.py 的调用场景。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/gitlab-reviews-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # GitLab Reviews 典型的 counts
        counts = build_counts(
            synced_count=0,  # reviews 使用 synced_event_count
            synced_mr_count=10,
            synced_event_count=50,
            skipped_event_count=5,
            total_requests=100,
            total_429_hits=0,
            timeout_count=0,
            avg_wait_time_ms=80,
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_reviews",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        record = get_sync_run(db_conn, run_id)
        persisted_counts = record["counts"]
        
        is_valid, errors, _ = validate_counts_schema(persisted_counts)
        assert is_valid, f"GitLab Reviews counts 应通过校验，错误: {errors}"
        
        assert persisted_counts["synced_mr_count"] == 10
        assert persisted_counts["synced_event_count"] == 50

    def test_svn_counts_persistence(self, db_conn):
        """
        SVN 脚本的 counts 持久化
        
        模拟 scm_sync_svn.py 的调用场景。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="svn",
            url=f"svn://svn.example.com/test/svn-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # SVN 典型的 counts
        counts = build_counts(
            synced_count=200,
            diff_count=180,
            bulk_count=10,
            degraded_count=5,
            patch_success=175,
            patch_failed=5,
            skipped_by_controller=10,
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="svn",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        record = get_sync_run(db_conn, run_id)
        persisted_counts = record["counts"]
        
        is_valid, errors, _ = validate_counts_schema(persisted_counts)
        assert is_valid, f"SVN counts 应通过校验，错误: {errors}"
        
        assert persisted_counts["patch_success"] == 175
        assert persisted_counts["patch_failed"] == 5
        assert persisted_counts["skipped_by_controller"] == 10


# ============ counts 状态场景测试 ============


class TestCountsPersistenceStatus:
    """测试不同状态下的 counts 持久化"""

    def test_no_data_status_counts_persistence(self, db_conn):
        """
        无数据状态 (no_data) 的 counts 持久化
        
        验证 synced_count=0 时 counts 正确持久化。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/no-data-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # 使用 build_payload_for_no_data 构造
        payload = build_payload_for_no_data()
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        
        payload_dict = payload.to_dict()
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status=payload_dict["status"],
            counts=payload_dict["counts"],
        )
        
        record = get_sync_run(db_conn, run_id)
        
        assert record["status"] == "no_data"
        
        persisted_counts = record["counts"]
        is_valid, errors, _ = validate_counts_schema(persisted_counts)
        assert is_valid, f"no_data 状态的 counts 应通过校验，错误: {errors}"
        
        assert persisted_counts["synced_count"] == 0

    def test_failed_status_counts_persistence(self, db_conn):
        """
        失败状态 (failed) 的 counts 持久化
        
        验证失败时 counts（可能部分完成）正确持久化。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/failed-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # 使用 build_payload_for_exception 构造
        exc = TimeoutError("Connection timed out")
        payload = build_payload_for_exception(
            exc,
            counts={"synced_count": 50, "diff_count": 45},  # 部分完成
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        
        payload_dict = payload.to_dict()
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status=payload_dict["status"],
            counts=payload_dict["counts"],
            error_summary_json=payload_dict.get("error_summary_json"),
        )
        
        record = get_sync_run(db_conn, run_id)
        
        assert record["status"] == "failed"
        
        persisted_counts = record["counts"]
        is_valid, errors, _ = validate_counts_schema(persisted_counts)
        assert is_valid, f"failed 状态的 counts 应通过校验，错误: {errors}"
        
        # 部分完成的数量应该保留
        assert persisted_counts["synced_count"] == 50


# ============ counts 生成列测试 ============


class TestCountsPersistenceGeneratedColumn:
    """测试 synced_count 生成列"""

    def test_synced_count_generated_column(self, db_conn):
        """
        测试 synced_count 生成列从 counts JSONB 中正确提取
        
        验证数据库的 GENERATED ALWAYS AS 功能正常工作。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/generated-column-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        counts = build_counts(synced_count=123, diff_count=100)
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        record = get_sync_run(db_conn, run_id)
        
        # 验证生成列与 counts.synced_count 一致
        assert record["synced_count"] == 123, "生成列 synced_count 应与 counts.synced_count 一致"
        assert record["counts"]["synced_count"] == record["synced_count"]


# ============ counts schema 完整性测试 ============


class TestCountsSchemaIntegrity:
    """测试 counts schema 完整性（持久化往返后）"""

    def test_all_known_fields_preserved(self, db_conn):
        """
        测试所有已知字段在持久化往返后保持完整
        
        验证 COUNTS_REQUIRED_FIELDS, COUNTS_OPTIONAL_FIELDS, COUNTS_LIMITER_FIELDS
        中定义的所有字段都能正确往返。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/all-fields-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # 构造包含所有字段的 counts（使用 build_counts 的默认值）
        counts = build_counts(
            # Required
            synced_count=100,
            # Optional - commits
            diff_count=95,
            bulk_count=5,
            degraded_count=3,
            diff_none_count=2,
            skipped_count=10,
            # Optional - mrs
            scanned_count=50,
            inserted_count=30,
            # Optional - reviews
            synced_mr_count=10,
            synced_event_count=50,
            skipped_event_count=5,
            # Optional - svn
            patch_success=90,
            patch_failed=5,
            skipped_by_controller=5,
            # Limiter
            total_requests=200,
            total_429_hits=3,
            timeout_count=1,
            avg_wait_time_ms=150,
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        record = get_sync_run(db_conn, run_id)
        persisted_counts = record["counts"]
        
        # 验证 schema
        is_valid, errors, warnings = validate_counts_schema(persisted_counts)
        assert is_valid, f"包含所有字段的 counts 应通过校验，错误: {errors}"
        
        # 验证所有已知字段都存在
        all_known_fields = COUNTS_REQUIRED_FIELDS | COUNTS_OPTIONAL_FIELDS | COUNTS_LIMITER_FIELDS
        for field in all_known_fields:
            assert field in persisted_counts, f"字段 {field} 应存在于持久化的 counts 中"

    def test_field_types_preserved(self, db_conn):
        """
        测试字段类型在持久化往返后保持为 int
        
        验证 JSONB 序列化/反序列化不会改变字段类型。
        """
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/field-types-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        counts = build_counts(
            synced_count=100,
            diff_count=0,  # 测试零值
            total_requests=999999,  # 测试大值
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        record = get_sync_run(db_conn, run_id)
        persisted_counts = record["counts"]
        
        # 验证类型
        for key, value in persisted_counts.items():
            assert isinstance(value, int), f"字段 {key} 应为 int 类型，实际: {type(value).__name__}"


# ============ counts 落库后可查询的最小集成测试 ============


class TestCountsQueryableAfterPersistence:
    """
    counts 落库后可查询的最小集成测试
    
    验证:
    1. counts JSONB 字段可以通过 SQL 直接查询
    2. 生成列 synced_count 可用于索引和查询
    3. 可以按 counts 中的特定字段进行筛选和排序
    """

    def test_counts_queryable_via_jsonb_operator(self, db_conn):
        """
        测试 counts 可以通过 JSONB 操作符直接查询
        
        验证 SQL: SELECT counts->>'synced_count' FROM sync_runs
        """
        # Arrange
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/jsonb-query-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        counts = build_counts(
            synced_count=123,
            diff_count=100,
            total_requests=200,
        )
        
        # Act: 写入
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts=counts,
        )
        
        # Assert: 通过 JSONB 操作符直接查询
        with db_conn.cursor() as cur:
            # 使用 ->> 操作符提取文本值
            cur.execute("""
                SELECT 
                    counts->>'synced_count' as synced_count_text,
                    (counts->>'synced_count')::int as synced_count_int,
                    counts->>'diff_count' as diff_count_text,
                    counts->>'total_requests' as total_requests_text
                FROM sync_runs
                WHERE run_id = %s
            """, (run_id,))
            row = cur.fetchone()
        
        assert row is not None, "应能通过 JSONB 操作符查询到记录"
        assert row[0] == "123", f"synced_count 文本值应为 '123'，实际: {row[0]}"
        assert row[1] == 123, f"synced_count 整数值应为 123，实际: {row[1]}"
        assert row[2] == "100", f"diff_count 应为 '100'，实际: {row[2]}"
        assert row[3] == "200", f"total_requests 应为 '200'，实际: {row[3]}"

    def test_generated_column_synced_count_queryable(self, db_conn):
        """
        测试生成列 synced_count 可以直接查询和用于条件过滤
        
        验证 SQL: SELECT synced_count FROM sync_runs WHERE synced_count > 50
        """
        # Arrange: 创建多个 run，不同的 synced_count
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/generated-col-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        
        run_ids = []
        synced_counts = [10, 50, 100, 200]
        
        for sc in synced_counts:
            run_id = str(uuid.uuid4())
            run_ids.append(run_id)
            
            insert_sync_run_start(
                conn=db_conn,
                run_id=run_id,
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
            )
            insert_sync_run_finish(
                conn=db_conn,
                run_id=run_id,
                status="completed",
                counts=build_counts(synced_count=sc),
            )
        
        # Assert: 使用生成列进行条件查询
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, synced_count
                FROM sync_runs
                WHERE repo_id = %s AND synced_count > 50
                ORDER BY synced_count ASC
            """, (repo_id,))
            rows = cur.fetchall()
        
        assert len(rows) == 2, f"应有 2 条 synced_count > 50 的记录，实际: {len(rows)}"
        assert rows[0][1] == 100, f"第一条应为 100，实际: {rows[0][1]}"
        assert rows[1][1] == 200, f"第二条应为 200，实际: {rows[1][1]}"

    def test_counts_filter_by_specific_field(self, db_conn):
        """
        测试可以按 counts 中的特定字段进行筛选
        
        验证 SQL: WHERE (counts->>'total_429_hits')::int > 0
        """
        # Arrange: 创建有/无 429 命中的 run
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/filter-field-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        
        # run 1: 无 429
        run_id_1 = str(uuid.uuid4())
        insert_sync_run_start(db_conn, run_id_1, repo_id, "gitlab_commits")
        insert_sync_run_finish(
            db_conn, run_id_1, "completed",
            counts=build_counts(synced_count=100, total_429_hits=0),
        )
        
        # run 2: 有 429
        run_id_2 = str(uuid.uuid4())
        insert_sync_run_start(db_conn, run_id_2, repo_id, "gitlab_commits")
        insert_sync_run_finish(
            db_conn, run_id_2, "completed",
            counts=build_counts(synced_count=50, total_429_hits=5),
        )
        
        # Assert: 筛选有 429 命中的记录
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, (counts->>'total_429_hits')::int as hits
                FROM sync_runs
                WHERE repo_id = %s 
                  AND (counts->>'total_429_hits')::int > 0
            """, (repo_id,))
            rows = cur.fetchall()
        
        assert len(rows) == 1, f"应只有 1 条有 429 命中的记录，实际: {len(rows)}"
        assert str(rows[0][0]) == run_id_2, "应为 run_id_2"
        assert rows[0][1] == 5, f"429 命中数应为 5，实际: {rows[0][1]}"

    def test_counts_aggregation_query(self, db_conn):
        """
        测试 counts 字段可以进行聚合查询
        
        验证 SQL: SUM((counts->>'synced_count')::int)
        """
        # Arrange: 创建多个 run
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/aggregation-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        
        synced_counts = [100, 200, 300]
        for sc in synced_counts:
            run_id = str(uuid.uuid4())
            insert_sync_run_start(db_conn, run_id, repo_id, "gitlab_commits")
            insert_sync_run_finish(
                db_conn, run_id, "completed",
                counts=build_counts(synced_count=sc, diff_count=sc - 10),
            )
        
        # Assert: 聚合查询
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    COUNT(*) as run_count,
                    SUM(synced_count) as total_synced,
                    SUM((counts->>'diff_count')::int) as total_diff,
                    AVG(synced_count)::int as avg_synced
                FROM sync_runs
                WHERE repo_id = %s AND status = 'completed'
            """, (repo_id,))
            row = cur.fetchone()
        
        assert row[0] == 3, f"应有 3 条记录，实际: {row[0]}"
        assert row[1] == 600, f"总 synced_count 应为 600，实际: {row[1]}"
        assert row[2] == 570, f"总 diff_count 应为 570，实际: {row[2]}"
        assert row[3] == 200, f"平均 synced_count 应为 200，实际: {row[3]}"

    def test_counts_minimal_contract_queryable(self, db_conn):
        """
        counts 最小契约测试: 验证 synced_count 是必需的且可查询
        
        这是 counts 契约的核心保证：synced_count 字段必须存在且可查询
        """
        # Arrange
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url=f"https://gitlab.example.com/test/minimal-contract-{uuid.uuid4().hex[:8]}",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # 只提供最小 counts (synced_count)
        minimal_counts = {"synced_count": 42}
        
        insert_sync_run_start(db_conn, run_id, repo_id, "gitlab_commits")
        insert_sync_run_finish(db_conn, run_id, "completed", counts=minimal_counts)
        
        # Assert: 最小契约字段可查询
        with db_conn.cursor() as cur:
            # 1. 通过 JSONB 操作符查询
            cur.execute("""
                SELECT counts->>'synced_count' 
                FROM sync_runs 
                WHERE run_id = %s
            """, (run_id,))
            jsonb_result = cur.fetchone()[0]
            
            # 2. 通过生成列查询
            cur.execute("""
                SELECT synced_count 
                FROM sync_runs 
                WHERE run_id = %s
            """, (run_id,))
            generated_result = cur.fetchone()[0]
            
            # 3. 通过 get_sync_run 函数查询
            record = get_sync_run(db_conn, run_id)
        
        assert jsonb_result == "42", f"JSONB 查询结果应为 '42'，实际: {jsonb_result}"
        assert generated_result == 42, f"生成列查询结果应为 42，实际: {generated_result}"
        assert record["counts"]["synced_count"] == 42, "API 查询结果应为 42"
        
        # 验证 schema 校验通过
        is_valid, errors, warnings = validate_counts_schema(record["counts"])
        assert is_valid, f"最小 counts 应通过 schema 校验，错误: {errors}"
