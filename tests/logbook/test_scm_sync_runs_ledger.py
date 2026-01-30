# -*- coding: utf-8 -*-
"""
test_scm_sync_runs_ledger.py - scm.sync_runs 表的单元测试

测试内容:
- insert_sync_run_start: 正确插入运行开始记录
- insert_sync_run_finish: 正确更新运行结束状态
- get_sync_run: 正确获取运行记录
- get_latest_sync_run: 正确获取最新运行记录
- 幂等性和边界条件测试

依赖: conftest.py 中的 db_conn fixture
"""

import uuid
import pytest
from datetime import datetime, timezone

import sys
from pathlib import Path

# 确保可以导入 db 模块
scripts_dir = Path(__file__).parent.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from db import (
    insert_sync_run_start,
    insert_sync_run_finish,
    get_sync_run,
    get_latest_sync_run,
    upsert_repo,
)


class TestInsertSyncRunStart:
    """测试 insert_sync_run_start 函数"""

    def test_insert_sync_run_start_basic(self, db_conn):
        """测试基本的同步运行开始记录"""
        # Arrange: 先创建一个 repo
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/repo",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        # Act
        result = insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            cursor_before={"last_commit_sha": "abc123", "last_commit_ts": "2024-01-01T00:00:00Z"},
            meta_json={"batch_size": 100},
        )
        
        # Assert
        assert result == run_id
        
        # 验证记录已插入
        record = get_sync_run(db_conn, run_id)
        assert record is not None
        assert record["run_id"] == uuid.UUID(run_id)
        assert record["repo_id"] == repo_id
        assert record["job_type"] == "gitlab_commits"
        assert record["mode"] == "incremental"
        assert record["status"] == "running"
        assert record["started_at"] is not None
        assert record["finished_at"] is None

    def test_insert_sync_run_start_all_job_types(self, db_conn):
        """测试所有任务类型的同步运行记录"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/repo2",
            project_key="test_project",
        )
        
        job_types = ["gitlab_commits", "gitlab_mrs", "gitlab_reviews", "svn"]
        
        for job_type in job_types:
            run_id = str(uuid.uuid4())
            result = insert_sync_run_start(
                conn=db_conn,
                run_id=run_id,
                repo_id=repo_id,
                job_type=job_type,
                mode="incremental",
            )
            
            assert result == run_id
            
            record = get_sync_run(db_conn, run_id)
            assert record["job_type"] == job_type

    def test_insert_sync_run_start_backfill_mode(self, db_conn):
        """测试 backfill 模式的同步运行记录"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="svn",
            url="svn://svn.example.com/repo",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        result = insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="svn",
            mode="backfill",
            cursor_before={"last_rev": 100},
        )
        
        record = get_sync_run(db_conn, run_id)
        assert record["mode"] == "backfill"


class TestInsertSyncRunFinish:
    """测试 insert_sync_run_finish 函数"""

    def test_insert_sync_run_finish_completed(self, db_conn):
        """测试正常完成的同步运行"""
        # Arrange
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/finish-test",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            cursor_before={"last_commit_sha": "abc123"},
        )
        
        # Act
        result = insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            cursor_after={"last_commit_sha": "def456", "last_commit_ts": "2024-01-02T00:00:00Z"},
            counts={"synced_count": 10, "diff_count": 8, "bulk_count": 2},
            logbook_item_id=12345,
        )
        
        # Assert
        assert result is True
        
        record = get_sync_run(db_conn, run_id)
        assert record["status"] == "completed"
        assert record["finished_at"] is not None
        assert record["cursor_after"]["last_commit_sha"] == "def456"
        assert record["counts"]["synced_count"] == 10
        assert record["logbook_item_id"] == 12345
        assert record["synced_count"] == 10  # 生成列

    def test_insert_sync_run_finish_failed(self, db_conn):
        """测试失败的同步运行"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/failed-test",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_mrs",
            mode="incremental",
        )
        
        result = insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="failed",
            error_summary_json={"error_type": "API_ERROR", "message": "Rate limited"},
        )
        
        assert result is True
        
        record = get_sync_run(db_conn, run_id)
        assert record["status"] == "failed"
        assert record["error_summary_json"]["error_type"] == "API_ERROR"

    def test_insert_sync_run_finish_no_data(self, db_conn):
        """测试无数据的同步运行（status='no_data'）"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/no-data-test",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_reviews",
            mode="incremental",
        )
        
        result = insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="no_data",
            counts={"synced_count": 0},
        )
        
        assert result is True
        
        record = get_sync_run(db_conn, run_id)
        assert record["status"] == "no_data"
        assert record["synced_count"] == 0

    def test_insert_sync_run_finish_with_degradation(self, db_conn):
        """测试带降级信息的同步运行"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/degrade-test",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        
        result = insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            counts={"synced_count": 100, "degraded_count": 5},
            degradation_json={
                "degraded_reasons": {"timeout": 3, "content_too_large": 2},
            },
        )
        
        assert result is True
        
        record = get_sync_run(db_conn, run_id)
        assert record["degradation_json"]["degraded_reasons"]["timeout"] == 3

    def test_insert_sync_run_finish_nonexistent(self, db_conn):
        """测试更新不存在的运行记录"""
        result = insert_sync_run_finish(
            conn=db_conn,
            run_id=str(uuid.uuid4()),
            status="completed",
        )
        
        assert result is False


class TestGetSyncRun:
    """测试 get_sync_run 函数"""

    def test_get_sync_run_exists(self, db_conn):
        """测试获取存在的运行记录"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="svn",
            url="svn://svn.example.com/get-test",
            project_key="test_project",
        )
        run_id = str(uuid.uuid4())
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="svn",
            mode="incremental",
        )
        
        record = get_sync_run(db_conn, run_id)
        
        assert record is not None
        assert record["run_id"] == uuid.UUID(run_id)

    def test_get_sync_run_not_exists(self, db_conn):
        """测试获取不存在的运行记录"""
        record = get_sync_run(db_conn, str(uuid.uuid4()))
        assert record is None


class TestGetLatestSyncRun:
    """测试 get_latest_sync_run 函数"""

    def test_get_latest_sync_run_by_repo(self, db_conn):
        """测试按仓库获取最新运行记录"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/latest-test",
            project_key="test_project",
        )
        
        # 插入多条记录
        run_ids = []
        for i in range(3):
            run_id = str(uuid.uuid4())
            run_ids.append(run_id)
            insert_sync_run_start(
                conn=db_conn,
                run_id=run_id,
                repo_id=repo_id,
                job_type="gitlab_commits",
                mode="incremental",
            )
        
        # 获取最新的
        record = get_latest_sync_run(db_conn, repo_id)
        
        assert record is not None
        assert record["run_id"] == uuid.UUID(run_ids[-1])

    def test_get_latest_sync_run_by_job_type(self, db_conn):
        """测试按仓库和任务类型获取最新运行记录"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/latest-job-type-test",
            project_key="test_project",
        )
        
        # 插入不同类型的记录
        commits_run_id = str(uuid.uuid4())
        mrs_run_id = str(uuid.uuid4())
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=commits_run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=mrs_run_id,
            repo_id=repo_id,
            job_type="gitlab_mrs",
            mode="incremental",
        )
        
        # 按任务类型获取
        commits_record = get_latest_sync_run(db_conn, repo_id, job_type="gitlab_commits")
        mrs_record = get_latest_sync_run(db_conn, repo_id, job_type="gitlab_mrs")
        
        assert commits_record["run_id"] == uuid.UUID(commits_run_id)
        assert mrs_record["run_id"] == uuid.UUID(mrs_run_id)

    def test_get_latest_sync_run_empty(self, db_conn):
        """测试仓库无运行记录时返回 None"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/empty-repo",
            project_key="test_project",
        )
        
        record = get_latest_sync_run(db_conn, repo_id)
        assert record is None


class TestSyncRunIntegration:
    """集成测试：模拟完整的同步运行生命周期"""

    def test_full_sync_lifecycle(self, db_conn):
        """测试完整的同步运行生命周期"""
        # 1. 创建仓库
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/lifecycle",
            project_key="test_project",
        )
        
        # 2. 开始同步
        run_id = str(uuid.uuid4())
        cursor_before = {
            "last_commit_sha": "initial_sha",
            "last_commit_ts": "2024-01-01T00:00:00Z",
        }
        
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
            cursor_before=cursor_before,
            meta_json={"batch_size": 100, "strict": True},
        )
        
        # 验证运行中状态
        record = get_sync_run(db_conn, run_id)
        assert record["status"] == "running"
        assert record["finished_at"] is None
        
        # 3. 完成同步
        cursor_after = {
            "last_commit_sha": "new_sha",
            "last_commit_ts": "2024-01-02T00:00:00Z",
        }
        counts = {
            "synced_count": 50,
            "diff_count": 45,
            "bulk_count": 5,
            "degraded_count": 2,
        }
        
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="completed",
            cursor_after=cursor_after,
            counts=counts,
            degradation_json={"degraded_reasons": {"timeout": 2}},
            logbook_item_id=99999,
        )
        
        # 4. 验证最终状态
        record = get_sync_run(db_conn, run_id)
        assert record["status"] == "completed"
        assert record["finished_at"] is not None
        assert record["cursor_before"]["last_commit_sha"] == "initial_sha"
        assert record["cursor_after"]["last_commit_sha"] == "new_sha"
        assert record["synced_count"] == 50
        assert record["logbook_item_id"] == 99999

    def test_no_data_sync_lifecycle(self, db_conn):
        """测试无新数据的同步运行生命周期（确保写入 no_data 记录）"""
        repo_id = upsert_repo(
            db_conn,
            repo_type="git",
            url="https://gitlab.example.com/test/no-data-lifecycle",
            project_key="test_project",
        )
        
        run_id = str(uuid.uuid4())
        
        # 开始同步
        insert_sync_run_start(
            conn=db_conn,
            run_id=run_id,
            repo_id=repo_id,
            job_type="gitlab_commits",
            mode="incremental",
        )
        
        # 没有新数据，直接完成
        insert_sync_run_finish(
            conn=db_conn,
            run_id=run_id,
            status="no_data",
            counts={"synced_count": 0},
        )
        
        # 验证记录存在且状态正确
        record = get_sync_run(db_conn, run_id)
        assert record["status"] == "no_data"
        assert record["synced_count"] == 0
        assert record["finished_at"] is not None
