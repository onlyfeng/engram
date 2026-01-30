# -*- coding: utf-8 -*-
"""
SCM Sync Queue 任务队列模块单元测试

测试:
- enqueue: 任务入队、防重复
- claim: 获取任务、并发 claim、过期锁回收
- ack: 确认完成
- fail_retry: 失败重试、指数退避
- mark_dead: 标记死信
- renew_lease: 续租
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import psycopg


class TestEnqueue:
    """enqueue 函数测试"""

    def test_enqueue_new_job(self, migrated_db):
        """成功入队新任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            # 创建测试仓库
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_enqueue.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import enqueue, get_job

                # 入队任务
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    mode="incremental",
                    priority=50,
                    payload={"page": 1},
                )
                
                assert job_id is not None
                
                # 验证任务状态
                job = get_job(job_id)
                assert job is not None
                assert job["repo_id"] == repo_id
                assert job["job_type"] == "gitlab_commits"
                assert job["mode"] == "incremental"
                assert job["priority"] == 50
                assert job["payload"] == {"page": 1}
                assert job["status"] == "pending"
                assert job["attempts"] == 0
                
        finally:
            if job_id or repo_id:
                with conn.cursor() as cur:
                    if job_id:
                        cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                    if repo_id:
                        cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_enqueue_duplicate_returns_none(self, migrated_db):
        """重复入队返回 None"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_dup.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import enqueue

                # 第一次入队
                job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits")
                assert job_id is not None

                # 第二次入队同一任务类型
                dup_job_id = enqueue(repo_id=repo_id, job_type="gitlab_commits")
                assert dup_job_id is None, "重复入队应该返回 None"

        finally:
            if job_id or repo_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_enqueue_different_job_types(self, migrated_db):
        """不同任务类型可以同时入队"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_ids = []
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_diff_types.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import enqueue

                for job_type in ["gitlab_commits", "gitlab_mrs", "gitlab_reviews"]:
                    job_id = enqueue(repo_id=repo_id, job_type=job_type)
                    assert job_id is not None
                    job_ids.append(job_id)

                assert len(job_ids) == 3

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestClaim:
    """claim 函数测试"""

    def test_claim_pending_job(self, migrated_db):
        """获取 pending 状态的任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending')
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, get_job

                # claim 任务
                job = claim(worker_id="worker-1")
                
                assert job is not None
                assert job["repo_id"] == repo_id
                assert job["job_type"] == "gitlab_commits"
                assert job["attempts"] == 1  # 已增加

                # 验证状态已更新
                job_info = get_job(job["job_id"])
                assert job_info["status"] == "running"
                assert job_info["locked_by"] == "worker-1"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_respects_priority(self, migrated_db):
        """按优先级获取任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建多个仓库和任务，优先级不同
            priorities = [100, 50, 200]  # 50 应该最先被获取
            for i, priority in enumerate(priorities):
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_priority_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending')
                    """, (repo_id, priority))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim

                # claim 应该获取优先级最高（数值最小）的任务
                job = claim(worker_id="worker-1")
                
                assert job is not None
                assert job["priority"] == 50

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_respects_not_before(self, migrated_db):
        """不获取 not_before 未到的任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_not_before.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 not_before 在未来的任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, not_before
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending', 
                            now() + interval '1 hour')
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim

                # 应该没有可用任务
                job = claim(worker_id="worker-1")
                assert job is None

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_expired_running_job(self, migrated_db):
        """回收锁过期的 running 任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个锁已过期的 running 任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1)
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, get_job

                # 应该能获取这个过期任务
                job = claim(worker_id="worker-2")
                
                assert job is not None
                assert job["attempts"] == 2  # 从 1 增加到 2

                # 验证锁持有者已更新
                job_info = get_job(job["job_id"])
                assert job_info["locked_by"] == "worker-2"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_concurrent_only_one_wins(self, migrated_db):
        """并发 claim：只有一个 worker 成功"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_concurrent.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending')
                """, (repo_id,))

            results = []

            def try_claim(worker_id):
                try:
                    test_conn = psycopg.connect(dsn, autocommit=False)
                    with test_conn.cursor() as cur:
                        cur.execute(f"SET search_path TO {scm_schema}")
                    
                    with patch('engram_logbook.scm_sync_queue.get_connection', return_value=test_conn):
                        from engram.logbook.scm_sync_queue import claim
                        job = claim(worker_id=worker_id)
                        return (worker_id, job is not None)
                except Exception as e:
                    return (worker_id, f"error: {e}")

            # 并发 claim
            num_workers = 5
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(try_claim, f"worker-{i}") for i in range(num_workers)]
                for future in as_completed(futures):
                    results.append(future.result())

            # 只有一个成功
            successful = [r for r in results if r[1] is True]
            failed = [r for r in results if r[1] is False]
            
            assert len(successful) == 1
            assert len(failed) == num_workers - 1

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_with_job_type_filter(self, migrated_db):
        """按任务类型过滤 claim"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建不同类型的任务
            for job_type in ["gitlab_commits", "gitlab_mrs", "svn"]:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_filter_{job_type}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status
                        )
                        VALUES (%s, %s, 'incremental', 100, 'pending')
                    """, (repo_id, job_type))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim

                # 只 claim gitlab 相关任务
                job = claim(worker_id="worker-1", job_types=["gitlab_commits", "gitlab_mrs"])
                
                assert job is not None
                assert job["job_type"] in ["gitlab_commits", "gitlab_mrs"]

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestAck:
    """ack 函数测试"""

    def test_ack_success(self, migrated_db):
        """成功确认任务完成"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_ack.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import ack, get_job

                result = ack(job_id, "worker-1", run_id="run-123")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "completed"
                assert job["locked_by"] is None
                assert job["last_run_id"] == "run-123"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_ack_wrong_worker_fails(self, migrated_db):
        """错误 worker 无法 ack"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_ack_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import ack

                # worker-2 尝试 ack worker-1 的任务
                result = ack(job_id, "worker-2")
                assert result is False

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestFailRetry:
    """fail_retry 函数测试"""

    def test_fail_retry_sets_failed_status(self, migrated_db):
        """失败后设置 failed 状态"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_fail.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import fail_retry, get_job

                result = fail_retry(job_id, "worker-1", "Connection timeout")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "failed"
                assert job["locked_by"] is None
                assert job["last_error"] == "Connection timeout"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fail_retry_marks_dead_when_max_attempts(self, migrated_db):
        """达到最大尝试次数时标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 已尝试 3 次，max_attempts = 3
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 3, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import fail_retry, get_job

                result = fail_retry(job_id, "worker-1", "Final failure")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "dead"
                assert job["last_error"] == "Final failure"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestMarkDead:
    """mark_dead 函数测试"""

    def test_mark_dead_success(self, migrated_db):
        """成功标记为死信"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_mark_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import mark_dead, get_job

                result = mark_dead(job_id, "worker-1", "Unrecoverable error")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "dead"
                assert job["last_error"] == "Unrecoverable error"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRenewLease:
    """renew_lease 函数测试"""

    def test_renew_lease_success(self, migrated_db):
        """成功续租"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now() - interval '2 minutes', 300)
                    RETURNING job_id, locked_at
                """, (repo_id,))
                row = cur.fetchone()
                job_id = str(row[0])
                old_locked_at = row[1]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import renew_lease, get_job

                result = renew_lease(job_id, "worker-1")
                assert result is True

                job = get_job(job_id)
                assert job["locked_at"] > old_locked_at

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_renew_lease_wrong_worker_fails(self, migrated_db):
        """错误 worker 无法续租"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_renew_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now())
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import renew_lease

                result = renew_lease(job_id, "worker-2")
                assert result is False

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestListAndCount:
    """列表和统计函数测试"""

    def test_list_jobs_by_status(self, migrated_db):
        """按状态列出任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建不同状态的任务
            for status in ["pending", "pending", "running", "completed"]:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_list_{status}_{len(repo_ids)}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, status
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s)
                    """, (repo_id, status))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import list_jobs_by_status

                pending_jobs = list_jobs_by_status("pending")
                assert len(pending_jobs) >= 2
                
                for job in pending_jobs:
                    assert job["status"] == "pending"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_count_jobs_by_status(self, migrated_db):
        """统计各状态任务数量"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            for status in ["pending", "pending", "running"]:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_count_{len(repo_ids)}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, status
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s)
                    """, (repo_id, status))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import count_jobs_by_status

                counts = count_jobs_by_status()
                assert counts.get("pending", 0) >= 2
                assert counts.get("running", 0) >= 1

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestCleanupAndReset:
    """清理和重置函数测试"""

    def test_cleanup_completed_jobs(self, migrated_db):
        """清理已完成的旧任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_cleanup.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个旧的已完成任务
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, updated_at
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'completed', 
                            now() - interval '10 days')
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import cleanup_completed_jobs

                deleted = cleanup_completed_jobs(older_than_days=7)
                assert deleted >= 1

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_reset_dead_jobs(self, migrated_db):
        """重置死信任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_reset.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, attempts, last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'dead', 3, 'Some error')
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import reset_dead_jobs, get_job

                reset = reset_dead_jobs(repo_id=repo_id)
                assert reset >= 1

                job = get_job(job_id)
                assert job["status"] == "pending"
                assert job["attempts"] == 0
                assert job["last_error"] is None

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRequeueWithoutPenalty:
    """requeue_without_penalty 函数测试"""

    def test_requeue_without_penalty_success(self, migrated_db):
        """成功重新入队，attempts 不增加"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 状态的任务，attempts = 1
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import requeue_without_penalty, get_job

                result = requeue_without_penalty(
                    job_id, "worker-1", 
                    reason="resource_locked",
                    jitter_seconds=1
                )
                assert result is True

                job = get_job(job_id)
                assert job["status"] == "pending"
                assert job["locked_by"] is None
                # attempts 应该回退到 0（从 1 减 1）
                assert job["attempts"] == 0
                assert "resource_locked" in (job["last_error"] or "")

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_requeue_wrong_worker_fails(self, migrated_db):
        """错误 worker 无法 requeue"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue_wrong.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, status, locked_by, locked_at,
                        attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 'running', 
                            'worker-1', now(), 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import requeue_without_penalty

                # worker-2 尝试 requeue worker-1 的任务
                result = requeue_without_penalty(job_id, "worker-2", reason="test")
                assert result is False

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_locked_skipped_not_push_to_dead(self, migrated_db):
        """locked/skipped 场景多次 requeue 不会推向 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_locked_skipped.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import (
                    enqueue, claim, requeue_without_penalty, get_job,
                    STATUS_PENDING, STATUS_DEAD,
                )

                # 入队，max_attempts = 2（很小的值，容易触发 dead）
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    max_attempts=2,
                )
                assert job_id is not None

                # 模拟多次 locked/skipped 场景
                for i in range(5):  # 超过 max_attempts 的次数
                    # claim 任务
                    job = claim(worker_id=f"worker-{i}")
                    if job is None:
                        # 需要等待 not_before，直接更新
                        with mock_conn.cursor() as cur:
                            cur.execute(f"""
                                UPDATE {scm_schema}.sync_jobs 
                                SET not_before = now() - interval '1 second'
                                WHERE job_id = %s
                            """, (job_id,))
                            mock_conn.commit()
                        job = claim(worker_id=f"worker-{i}")
                    
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    # 使用 requeue_without_penalty（模拟 locked/skipped）
                    result = requeue_without_penalty(
                        job_id, f"worker-{i}",
                        reason=f"locked_skipped_{i}",
                        jitter_seconds=0  # 立即可调度
                    )
                    assert result is True, f"第 {i+1} 次 requeue 失败"

                    # 验证没有变成 dead
                    job_info = get_job(job_id)
                    assert job_info["status"] != STATUS_DEAD, f"第 {i+1} 次后任务不应该是 dead 状态"
                    assert job_info["status"] == STATUS_PENDING, f"第 {i+1} 次后任务应该是 pending 状态"

                # 最终状态验证
                final_job = get_job(job_id)
                assert final_job["status"] == STATUS_PENDING
                # attempts 应该保持为 0（每次 +1 后又 -1）
                assert final_job["attempts"] == 0

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRequeueAttemptsSemantics:
    """requeue/attempts 语义测试
    
    验证:
    - claim 增加 attempts
    - requeue_without_penalty 回退 attempts
    - fail_retry 不改变 attempts（在 fail_retry 前已经被 claim 增加）
    - ack 不改变 attempts
    """

    def test_claim_increments_attempts(self, migrated_db):
        """claim 时 attempts 增加 1"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_inc.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending', 0)
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, get_job

                # claim 前 attempts = 0
                job = claim(worker_id="worker-1")
                assert job is not None
                
                # claim 后 attempts = 1
                assert job["attempts"] == 1, "claim 应该增加 attempts"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_requeue_decrements_attempts(self, migrated_db):
        """requeue_without_penalty 时 attempts 减少 1（补偿 claim 的增加）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue_dec.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 2
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 2, 5)
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import requeue_without_penalty, get_job

                # 获取 job_id
                with mock_conn.cursor() as cur:
                    cur.execute(f"SELECT job_id FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    job_id = str(cur.fetchone()[0])

                # requeue 前 attempts = 2
                result = requeue_without_penalty(job_id, "worker-1", reason="test", jitter_seconds=0)
                assert result is True

                # requeue 后 attempts = 1
                job = get_job(job_id)
                assert job["attempts"] == 1, "requeue_without_penalty 应该减少 attempts"
                assert job["status"] == "pending"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_requeue_attempts_floor_at_zero(self, migrated_db):
        """requeue_without_penalty 时 attempts 最小为 0（不会变负）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_requeue_floor.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 0（边界情况）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 0, 5)
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import requeue_without_penalty, get_job

                with mock_conn.cursor() as cur:
                    cur.execute(f"SELECT job_id FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    job_id = str(cur.fetchone()[0])

                result = requeue_without_penalty(job_id, "worker-1", reason="test", jitter_seconds=0)
                assert result is True

                job = get_job(job_id)
                assert job["attempts"] == 0, "attempts 应该最小为 0，不应变为负数"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_requeue_cycle_maintains_attempts(self, migrated_db):
        """claim + requeue 循环应保持 attempts 不变"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_claim_requeue_cycle.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending', 0, 5)
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, requeue_without_penalty, get_job

                # 执行 3 次 claim + requeue 循环
                for i in range(3):
                    job = claim(worker_id=f"worker-{i}")
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    job_id = job["job_id"]
                    
                    # claim 后 attempts 应为 1
                    assert job["attempts"] == 1, f"第 {i+1} 次 claim 后 attempts 应为 1"
                    
                    # requeue
                    result = requeue_without_penalty(job_id, f"worker-{i}", reason=f"test_{i}", jitter_seconds=0)
                    assert result is True, f"第 {i+1} 次 requeue 失败"
                    
                    # requeue 后 attempts 应为 0
                    job_info = get_job(job_id)
                    assert job_info["attempts"] == 0, f"第 {i+1} 次 requeue 后 attempts 应为 0"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_ack_does_not_change_attempts(self, migrated_db):
        """ack 不改变 attempts"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_ack_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 3
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 3, 5)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import ack, get_job

                result = ack(job_id, "worker-1", run_id="run-123")
                assert result is True

                job = get_job(job_id)
                assert job["attempts"] == 3, "ack 不应改变 attempts"
                assert job["status"] == "completed"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fail_retry_does_not_change_attempts(self, migrated_db):
        """fail_retry 不改变 attempts（claim 时已增加）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_fail_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个 running 任务，attempts = 2
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, 
                        locked_by, locked_at, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 
                            'worker-1', now(), 2, 5)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import fail_retry, get_job

                result = fail_retry(job_id, "worker-1", "Test error", backoff_seconds=0)
                assert result is True

                job = get_job(job_id)
                assert job["attempts"] == 2, "fail_retry 不应改变 attempts"
                assert job["status"] == "failed"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestTenantFairClaim:
    """租户公平调度测试
    
    验证:
    - enable_tenant_fair_claim=True 时，不会长期只 claim 单一 tenant 的任务
    - 多 tenant 混合队列中，各 tenant 都能公平获得执行机会
    """

    def test_tenant_fair_claim_distributes_across_tenants(self, migrated_db):
        """租户公平调度：多 tenant 混合队列时不会只 claim 单一 tenant"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建 3 个 tenant，每个 tenant 有 5 个任务（共 15 个任务）
            # tenant_a: priority 10, 20, 30, 40, 50
            # tenant_b: priority 11, 21, 31, 41, 51
            # tenant_c: priority 12, 22, 32, 42, 52
            # 如果不启用公平调度，会先获取 tenant_a 的所有任务
            tenants = ["tenant_a", "tenant_b", "tenant_c"]
            for tenant_idx, tenant_id in enumerate(tenants):
                for i in range(5):
                    with conn.cursor() as cur:
                        priority = (i + 1) * 10 + tenant_idx  # 10, 11, 12, 20, 21, 22, ...
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                            VALUES ('git', 'https://example.com/test_fair_{tenant_id}_{i}.git')
                            RETURNING repo_id
                        """)
                        repo_id = cur.fetchone()[0]
                        repo_ids.append(repo_id)
                        
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.sync_jobs (
                                repo_id, job_type, mode, priority, status,
                                payload_json
                            )
                            VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                    %s::jsonb)
                        """, (repo_id, priority, f'{{"tenant_id": "{tenant_id}"}}'))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 启用公平调度，连续 claim 9 个任务
                claimed_tenants = []
                for i in range(9):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    tenant_id = job["payload"].get("tenant_id", "")
                    claimed_tenants.append(tenant_id)
                    
                    # 立即 ack，释放任务
                    ack(job["job_id"], f"worker-{i}")

                # 验证：前 9 个任务应该来自 3 个不同的 tenant，每个 tenant 约 3 个
                # 公平调度确保不会只 claim 单一 tenant
                tenant_counts = {}
                for t in claimed_tenants:
                    tenant_counts[t] = tenant_counts.get(t, 0) + 1
                
                # 应该有 3 个不同的 tenant
                assert len(tenant_counts) == 3, f"应该从 3 个 tenant claim 任务，实际: {tenant_counts}"
                
                # 每个 tenant 应该至少被 claim 2 次（公平分布）
                for tenant_id, count in tenant_counts.items():
                    assert count >= 2, f"tenant {tenant_id} 只被 claim {count} 次，公平调度失败"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_without_fair_claim_follows_strict_priority(self, migrated_db):
        """不启用公平调度时：严格按优先级顺序获取"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建 3 个 tenant 的任务，tenant_a 优先级最高
            # tenant_a: priority 10, 20, 30
            # tenant_b: priority 11, 21, 31
            # tenant_c: priority 12, 22, 32
            tenants = ["tenant_a", "tenant_b", "tenant_c"]
            for tenant_idx, tenant_id in enumerate(tenants):
                for i in range(3):
                    with conn.cursor() as cur:
                        priority = (i + 1) * 10 + tenant_idx
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                            VALUES ('git', 'https://example.com/test_nofair_{tenant_id}_{i}.git')
                            RETURNING repo_id
                        """)
                        repo_id = cur.fetchone()[0]
                        repo_ids.append(repo_id)
                        
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.sync_jobs (
                                repo_id, job_type, mode, priority, status,
                                payload_json
                            )
                            VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                    %s::jsonb)
                        """, (repo_id, priority, f'{{"tenant_id": "{tenant_id}"}}'))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 不启用公平调度，连续 claim 9 个任务
                claimed_tenants = []
                claimed_priorities = []
                for i in range(9):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=False,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    tenant_id = job["payload"].get("tenant_id", "")
                    claimed_tenants.append(tenant_id)
                    claimed_priorities.append(job["priority"])
                    
                    # 立即 ack，释放任务
                    ack(job["job_id"], f"worker-{i}")

                # 验证：按严格优先级顺序 claim
                # 预期顺序: 10, 11, 12, 20, 21, 22, 30, 31, 32
                expected_priorities = [10, 11, 12, 20, 21, 22, 30, 31, 32]
                assert claimed_priorities == expected_priorities, \
                    f"优先级顺序不符，期望 {expected_priorities}，实际 {claimed_priorities}"
                
                # 验证：tenant 按优先级交替出现
                expected_tenants = ["tenant_a", "tenant_b", "tenant_c"] * 3
                assert claimed_tenants == expected_tenants, \
                    f"tenant 顺序不符，期望 {expected_tenants}，实际 {claimed_tenants}"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fair_claim_with_single_tenant(self, migrated_db):
        """公平调度：只有单一 tenant 时正常工作"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 只创建一个 tenant 的任务
            for i in range(5):
                with conn.cursor() as cur:
                    priority = (i + 1) * 10
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_single_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "only_tenant"}'::jsonb)
                    """, (repo_id, priority))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 启用公平调度
                claimed_priorities = []
                for i in range(5):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    assert job["payload"].get("tenant_id") == "only_tenant"
                    claimed_priorities.append(job["priority"])
                    ack(job["job_id"], f"worker-{i}")

                # 验证：按优先级顺序获取
                assert claimed_priorities == [10, 20, 30, 40, 50]

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fair_claim_with_null_tenant(self, migrated_db):
        """公平调度：包含无 tenant_id 的任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建混合任务：有 tenant_id 和无 tenant_id
            # tenant_a: priority 10, 20
            # 无 tenant: priority 15, 25 (payload_json 为空或无 tenant_id)
            
            # tenant_a 任务
            for i in range(2):
                with conn.cursor() as cur:
                    priority = (i + 1) * 10
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_null_a_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_a"}'::jsonb)
                    """, (repo_id, priority))
            
            # 无 tenant_id 的任务
            for i in range(2):
                with conn.cursor() as cur:
                    priority = (i + 1) * 10 + 5  # 15, 25
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_null_none_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{{}}'::jsonb)
                    """, (repo_id, priority))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 启用公平调度
                claimed_jobs = []
                for i in range(4):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    claimed_jobs.append({
                        "tenant_id": job["payload"].get("tenant_id"),
                        "priority": job["priority"],
                    })
                    ack(job["job_id"], f"worker-{i}")

                # 验证：公平调度应该在两个"tenant 组"（有 tenant_id 和无 tenant_id）间分配
                # 具体顺序取决于实现，但应该两组都有任务被 claim
                tenant_a_count = sum(1 for j in claimed_jobs if j["tenant_id"] == "tenant_a")
                no_tenant_count = sum(1 for j in claimed_jobs if j["tenant_id"] is None)
                
                assert tenant_a_count == 2, f"tenant_a 应该有 2 个任务，实际 {tenant_a_count}"
                assert no_tenant_count == 2, f"无 tenant 应该有 2 个任务，实际 {no_tenant_count}"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_fair_claim_prevents_single_tenant_starvation(self, migrated_db):
        """公平调度：防止单一 tenant 饥饿（核心验证）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 模拟极端场景：tenant_a 有 10 个高优先级任务，tenant_b 只有 1 个低优先级任务
            # tenant_a: priority 1-10
            # tenant_b: priority 100
            
            # tenant_a 任务
            for i in range(10):
                with conn.cursor() as cur:
                    priority = i + 1
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/test_starve_a_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_a"}'::jsonb)
                    """, (repo_id, priority))
            
            # tenant_b 只有 1 个低优先级任务
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_starve_b.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                repo_ids.append(repo_id)
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        payload_json
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending',
                            '{"tenant_id": "tenant_b"}'::jsonb)
                """, (repo_id,))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 启用公平调度，claim 前 5 个任务
                tenant_b_found = False
                for i in range(5):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    if job["payload"].get("tenant_id") == "tenant_b":
                        tenant_b_found = True
                    
                    ack(job["job_id"], f"worker-{i}")
                
                # 核心验证：在前 5 次 claim 中，tenant_b 应该至少出现一次
                # 这证明公平调度防止了 tenant_b 被 tenant_a 完全压制
                assert tenant_b_found, \
                    "公平调度失败：tenant_b 应该在前 5 次 claim 中出现，但被 tenant_a 完全压制"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestIntegration:
    """集成测试：完整的 claim -> execute -> ack/fail 流程"""

    def test_full_job_lifecycle(self, migrated_db):
        """完整的任务生命周期"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lifecycle.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import (
                    enqueue, claim, ack, get_job,
                    STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED,
                )

                # 1. 入队
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    payload={"since": "2024-01-01"},
                )
                assert job_id is not None

                job = get_job(job_id)
                assert job["status"] == STATUS_PENDING

                # 2. Claim
                claimed = claim(worker_id="worker-1")
                assert claimed is not None
                assert claimed["job_id"] == job_id

                job = get_job(job_id)
                assert job["status"] == STATUS_RUNNING
                assert job["locked_by"] == "worker-1"

                # 3. Ack
                result = ack(job_id, "worker-1", run_id="run-abc")
                assert result is True

                job = get_job(job_id)
                assert job["status"] == STATUS_COMPLETED
                assert job["locked_by"] is None
                assert job["last_run_id"] == "run-abc"

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_retry_then_dead_lifecycle(self, migrated_db):
        """重试直到死信的生命周期"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_retry_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import (
                    enqueue, claim, fail_retry, get_job,
                    STATUS_FAILED, STATUS_DEAD,
                )

                # 入队，max_attempts = 2
                job_id = enqueue(
                    repo_id=repo_id,
                    job_type="gitlab_commits",
                    max_attempts=2,
                )

                # 第一次尝试失败
                job = claim(worker_id="worker-1")
                fail_retry(job_id, "worker-1", "Error 1", backoff_seconds=0)

                job = get_job(job_id)
                assert job["status"] == STATUS_FAILED
                assert job["attempts"] == 1

                # 第二次尝试失败 -> 应该变成 dead
                job = claim(worker_id="worker-1")
                fail_retry(job_id, "worker-1", "Error 2", backoff_seconds=0)

                job = get_job(job_id)
                assert job["status"] == STATUS_DEAD
                assert job["attempts"] == 2

        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestMultiTenantMultiRepoFairClaim:
    """多租户多仓库公平调度 claim 测试
    
    测试场景:
    - 多个 tenant 各有多个 repo 的 pending 任务
    - 开启 fairness 开关后，断言 claim 序列中 tenant 交替出现
    - 验证 per-tenant concurrency 限制
    - 关闭 fairness 开关时保持旧行为（严格按优先级）
    """

    def test_multi_tenant_multi_repo_claim_alternates_with_fairness(self, migrated_db):
        """多 tenant 多 repo：开启 fairness 后 claim 序列中 tenant 交替"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 构造 3 个 tenant，每个 tenant 有 4 个 repo 和任务
            # tenant_a: priority 10, 20, 30, 40
            # tenant_b: priority 11, 21, 31, 41
            # tenant_c: priority 12, 22, 32, 42
            tenants = ["tenant_a", "tenant_b", "tenant_c"]
            repos_per_tenant = 4
            
            for t_idx, tenant_id in enumerate(tenants):
                for r_idx in range(repos_per_tenant):
                    with conn.cursor() as cur:
                        priority = (r_idx + 1) * 10 + t_idx
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                            VALUES ('git', 'https://example.com/fair_claim_{tenant_id}_{r_idx}.git')
                            RETURNING repo_id
                        """)
                        repo_id = cur.fetchone()[0]
                        repo_ids.append(repo_id)
                        
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.sync_jobs (
                                repo_id, job_type, mode, priority, status,
                                payload_json
                            )
                            VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                    %s::jsonb)
                        """, (repo_id, priority, f'{{"tenant_id": "{tenant_id}"}}'))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 启用公平调度，连续 claim 12 个任务
                claimed_tenants = []
                for i in range(12):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    tenant_id = job["payload"].get("tenant_id", "")
                    claimed_tenants.append(tenant_id)
                    
                    # 立即 ack，释放任务
                    ack(job["job_id"], f"worker-{i}")

                # 验证：每 3 个连续 claim 应来自 3 个不同 tenant
                # 因为公平调度会轮询 tenant
                for round_start in range(0, 12, 3):
                    round_tenants = claimed_tenants[round_start:round_start + 3]
                    unique_tenants = set(round_tenants)
                    assert len(unique_tenants) == 3, \
                        f"轮次 {round_start // 3}: 应有 3 个不同 tenant，实际: {round_tenants}"

                # 验证每个 tenant 被 claim 4 次
                for tenant_id in tenants:
                    count = claimed_tenants.count(tenant_id)
                    assert count == 4, f"{tenant_id} 应被 claim 4 次，实际 {count}"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_multi_tenant_claim_without_fairness_strict_priority(self, migrated_db):
        """多 tenant 多 repo：关闭 fairness 时严格按优先级 claim"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 构造场景：tenant_a 有高优先级任务，tenant_b 有低优先级任务
            # tenant_a: priority 1, 2, 3, 4, 5 (高优先级)
            # tenant_b: priority 101, 102, 103, 104, 105 (低优先级)
            
            # tenant_a 高优先级任务
            for i in range(5):
                with conn.cursor() as cur:
                    priority = i + 1
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/nofair_a_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_a"}'::jsonb)
                    """, (repo_id, priority))
            
            # tenant_b 低优先级任务
            for i in range(5):
                with conn.cursor() as cur:
                    priority = 101 + i
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/nofair_b_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_b"}'::jsonb)
                    """, (repo_id, priority))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 关闭公平调度
                claimed_tenants = []
                claimed_priorities = []
                for i in range(10):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=False,  # 关闭公平调度
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    tenant_id = job["payload"].get("tenant_id", "")
                    claimed_tenants.append(tenant_id)
                    claimed_priorities.append(job["priority"])
                    
                    ack(job["job_id"], f"worker-{i}")

                # 验证：关闭 fairness 时，严格按优先级排序
                # 前 5 个应全是 tenant_a
                assert claimed_tenants[:5] == ["tenant_a"] * 5, \
                    f"前 5 个应全是 tenant_a，实际: {claimed_tenants[:5]}"
                
                # 后 5 个应全是 tenant_b
                assert claimed_tenants[5:] == ["tenant_b"] * 5, \
                    f"后 5 个应全是 tenant_b，实际: {claimed_tenants[5:]}"
                
                # 优先级应该递增
                assert claimed_priorities == sorted(claimed_priorities), \
                    f"优先级应递增，实际: {claimed_priorities}"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_multi_tenant_varying_backlog_fairness_prevents_starvation(self, migrated_db):
        """多 tenant 不同 backlog：fairness 防止小 tenant 饥饿"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # tenant_a: 15 个高优先级任务 (大 backlog)
            # tenant_b: 3 个低优先级任务 (小 backlog)
            
            for i in range(15):
                with conn.cursor() as cur:
                    priority = i + 1  # 1-15
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/backlog_a_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_a"}'::jsonb)
                    """, (repo_id, priority))
            
            for i in range(3):
                with conn.cursor() as cur:
                    priority = 100 + i  # 100-102 (低优先级)
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/backlog_b_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_b"}'::jsonb)
                    """, (repo_id, priority))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                # 启用公平调度，claim 前 6 个任务
                tenant_b_found_in_first_6 = False
                tenant_b_count = 0
                
                for i in range(6):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    if job["payload"].get("tenant_id") == "tenant_b":
                        tenant_b_found_in_first_6 = True
                        tenant_b_count += 1
                    
                    ack(job["job_id"], f"worker-{i}")

                # 核心验证：在前 6 次 claim 中，tenant_b 应该至少出现一次
                # 这证明公平调度防止了 tenant_b 被 tenant_a 完全压制
                assert tenant_b_found_in_first_6, \
                    "公平调度失败：tenant_b 应在前 6 次 claim 中出现"
                
                # 更严格的验证：tenant_b 应该出现约 3 次（6 次中两个 tenant 轮流）
                assert tenant_b_count >= 2, \
                    f"公平调度失败：tenant_b 应在前 6 次中出现至少 2 次，实际 {tenant_b_count}"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_four_tenants_round_robin_claim(self, migrated_db):
        """四个 tenant 轮询 claim"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 4 个 tenant 各 3 个任务
            tenants = ["tenant_a", "tenant_b", "tenant_c", "tenant_d"]
            
            for t_idx, tenant_id in enumerate(tenants):
                for r_idx in range(3):
                    with conn.cursor() as cur:
                        priority = (r_idx + 1) * 10 + t_idx
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                            VALUES ('git', 'https://example.com/rr_{tenant_id}_{r_idx}.git')
                            RETURNING repo_id
                        """)
                        repo_id = cur.fetchone()[0]
                        repo_ids.append(repo_id)
                        
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.sync_jobs (
                                repo_id, job_type, mode, priority, status,
                                payload_json
                            )
                            VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                    %s::jsonb)
                        """, (repo_id, priority, f'{{"tenant_id": "{tenant_id}"}}'))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                claimed_tenants = []
                for i in range(12):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    
                    tenant_id = job["payload"].get("tenant_id", "")
                    claimed_tenants.append(tenant_id)
                    ack(job["job_id"], f"worker-{i}")

                # 验证：前 8 个应该是 4 个 tenant 各出现 2 次
                first_eight = claimed_tenants[:8]
                for tenant_id in tenants:
                    count = first_eight.count(tenant_id)
                    assert count == 2, f"前 8 个中 {tenant_id} 应出现 2 次，实际 {count}"

                # 验证每个 tenant 总共被 claim 3 次
                for tenant_id in tenants:
                    count = claimed_tenants.count(tenant_id)
                    assert count == 3, f"{tenant_id} 应被 claim 3 次，实际 {count}"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_with_mixed_null_and_valid_tenant_ids(self, migrated_db):
        """混合场景：部分任务有 tenant_id，部分没有"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # tenant_a: 3 个任务
            for i in range(3):
                with conn.cursor() as cur:
                    priority = (i + 1) * 10
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/mixed_a_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{"tenant_id": "tenant_a"}'::jsonb)
                    """, (repo_id, priority))
            
            # 无 tenant_id: 3 个任务
            for i in range(3):
                with conn.cursor() as cur:
                    priority = (i + 1) * 10 + 5  # 15, 25, 35
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                        VALUES ('git', 'https://example.com/mixed_none_{i}.git')
                        RETURNING repo_id
                    """)
                    repo_id = cur.fetchone()[0]
                    repo_ids.append(repo_id)
                    
                    cur.execute(f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            payload_json
                        )
                        VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                '{{}}'::jsonb)
                    """, (repo_id, priority))

            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack

                claimed_jobs = []
                for i in range(6):
                    job = claim(
                        worker_id=f"worker-{i}",
                        enable_tenant_fair_claim=True,
                    )
                    assert job is not None, f"第 {i+1} 次 claim 失败"
                    claimed_jobs.append({
                        "tenant_id": job["payload"].get("tenant_id"),
                        "priority": job["priority"],
                    })
                    ack(job["job_id"], f"worker-{i}")

                # 验证：两个组（有 tenant_id 和无 tenant_id）都应有任务被 claim
                tenant_a_count = sum(1 for j in claimed_jobs if j["tenant_id"] == "tenant_a")
                no_tenant_count = sum(1 for j in claimed_jobs if j["tenant_id"] is None)
                
                assert tenant_a_count == 3, f"tenant_a 应有 3 个任务，实际 {tenant_a_count}"
                assert no_tenant_count == 3, f"无 tenant 应有 3 个任务，实际 {no_tenant_count}"
                
                # 验证交替模式：前 4 个应该是交替的
                first_four_tenants = [j["tenant_id"] for j in claimed_jobs[:4]]
                for i in range(3):
                    if first_four_tenants[i] == first_four_tenants[i + 1]:
                        # 允许在一边用完后连续
                        before_count = first_four_tenants[:i+1].count(first_four_tenants[i])
                        if before_count < 3:  # 还没用完
                            assert False, f"位置 {i} 和 {i+1} 不应连续相同 ({first_four_tenants[i]})"

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_claim_sequence_consistent_with_fairness_toggle(self, migrated_db):
        """验证 fairness 开关切换前后行为一致性"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_ids = []
        try:
            # 创建测试数据：2 个 tenant 各 2 个任务
            tenants = ["tenant_a", "tenant_b"]
            for t_idx, tenant_id in enumerate(tenants):
                for r_idx in range(2):
                    with conn.cursor() as cur:
                        # tenant_a: priority 10, 20
                        # tenant_b: priority 11, 21
                        priority = (r_idx + 1) * 10 + t_idx
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                            VALUES ('git', 'https://example.com/toggle_{tenant_id}_{r_idx}.git')
                            RETURNING repo_id
                        """)
                        repo_id = cur.fetchone()[0]
                        repo_ids.append(repo_id)
                        
                        cur.execute(f"""
                            INSERT INTO {scm_schema}.sync_jobs (
                                repo_id, job_type, mode, priority, status,
                                payload_json
                            )
                            VALUES (%s, 'gitlab_commits', 'incremental', %s, 'pending',
                                    %s::jsonb)
                        """, (repo_id, priority, f'{{"tenant_id": "{tenant_id}"}}'))

            # 测试 1：开启 fairness
            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.scm_sync_queue import claim, ack, requeue_without_penalty

                # 开启 fairness 时 claim
                job1 = claim(worker_id="w1", enable_tenant_fair_claim=True)
                job2 = claim(worker_id="w2", enable_tenant_fair_claim=True)
                
                # 应该来自不同 tenant
                t1 = job1["payload"].get("tenant_id")
                t2 = job2["payload"].get("tenant_id")
                assert t1 != t2, f"开启 fairness 时前 2 个应来自不同 tenant，实际: {t1}, {t2}"
                
                # 归还任务
                requeue_without_penalty(job1["job_id"], "w1", reason="test", jitter_seconds=0)
                requeue_without_penalty(job2["job_id"], "w2", reason="test", jitter_seconds=0)
                
            # 测试 2：关闭 fairness（使用新连接）
            with patch('engram_logbook.scm_sync_queue.get_connection') as mock_get_conn2:
                mock_conn2 = psycopg.connect(dsn, autocommit=False)
                with mock_conn2.cursor() as cur:
                    cur.execute(f"SET search_path TO {scm_schema}")
                mock_get_conn2.return_value = mock_conn2

                from engram.logbook.scm_sync_queue import claim as claim2, ack as ack2

                # 关闭 fairness 时 claim
                job1 = claim2(worker_id="w3", enable_tenant_fair_claim=False)
                job2 = claim2(worker_id="w4", enable_tenant_fair_claim=False)
                
                # 应该按严格优先级，前 2 个可能来自同一 tenant（取决于优先级）
                p1 = job1["priority"]
                p2 = job2["priority"]
                assert p1 <= p2, f"关闭 fairness 时应按优先级顺序，实际: {p1}, {p2}"
                
                ack2(job1["job_id"], "w3")
                ack2(job2["job_id"], "w4")

        finally:
            for repo_id in repo_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE repo_id = %s", (repo_id,))
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()
