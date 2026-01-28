# -*- coding: utf-8 -*-
"""
SCM Sync Reaper 单元测试

测试:
- 过期 running job 的回收
- 过期 running run 的回收
- 过期 lock 的回收
- 回收后的状态转换验证
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import psycopg


class TestExpiredJobRecovery:
    """过期 running job 回收测试"""

    def test_expired_job_detected(self, migrated_db):
        """检测过期的 running 任务"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_job.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个过期的 running 任务（locked_at 在 10 分钟前，lease_seconds=300）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            # 使用 db.list_expired_running_jobs 检测
            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_jobs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=100)
                
                # 应该检测到过期任务
                expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
                assert job_id in expired_job_ids, f"应该检测到过期任务 {job_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_job_marked_failed(self, migrated_db):
        """过期任务被标记为 failed（尚未达到 max_attempts）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import mark_job_as_failed_by_reaper, get_conn
            
            test_conn = get_conn(dsn)
            try:
                # 标记为 failed
                success = mark_job_as_failed_by_reaper(
                    test_conn, job_id, 
                    error="Reaped: job lock expired",
                    retry_delay_seconds=60,
                )
                test_conn.commit()
                
                assert success is True
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, locked_by, last_error 
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    
                    assert row[0] == "failed", f"状态应为 failed，实际: {row[0]}"
                    assert row[1] is None, "locked_by 应为 NULL"
                    assert "Reaped" in row[2], f"last_error 应包含 Reaped: {row[2]}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_job_marked_dead_when_max_attempts(self, migrated_db):
        """过期任务达到 max_attempts 时被标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # attempts = max_attempts
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import mark_job_as_dead_by_reaper, get_conn
            
            test_conn = get_conn(dsn)
            try:
                # 标记为 dead
                success = mark_job_as_dead_by_reaper(
                    test_conn, job_id, 
                    error="Reaped: job expired after max attempts",
                )
                test_conn.commit()
                
                assert success is True
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, locked_by, last_error 
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    
                    assert row[0] == "dead", f"状态应为 dead，实际: {row[0]}"
                    assert row[1] is None, "locked_by 应为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_non_expired_job_not_detected(self, migrated_db):
        """未过期的 running 任务不应被检测"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_non_expired.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个未过期的 running 任务（刚刚锁定）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'active-worker', now(), 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_jobs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=100)
                
                # 不应检测到未过期任务
                expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
                assert job_id not in expired_job_ids, f"不应检测到未过期任务 {job_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestExpiredRunRecovery:
    """过期 running run 回收测试"""

    def test_expired_run_detected(self, migrated_db):
        """检测超时的 running run"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_run.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                run_id = str(uuid.uuid4())
                # 创建一个超时的 running run（started_at 在 45 分钟前）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """, (run_id, repo_id))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_runs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                # max_duration_seconds=1800 (30 分钟)
                expired_runs = list_expired_running_runs(
                    test_conn, max_duration_seconds=1800, limit=100
                )
                
                expired_run_ids = [str(r["run_id"]) for r in expired_runs]
                assert run_id in expired_run_ids, f"应该检测到超时 run {run_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_run_marked_failed(self, migrated_db):
        """超时 run 被标记为 failed"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_run_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                run_id = str(uuid.uuid4())
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """, (run_id, repo_id))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import mark_run_as_failed_by_reaper, get_conn
            
            test_conn = get_conn(dsn)
            try:
                error_summary = {
                    "error_type": "reaper_timeout",
                    "message": "Reaped: sync run timed out",
                }
                
                success = mark_run_as_failed_by_reaper(test_conn, run_id, error_summary)
                test_conn.commit()
                
                assert success is True
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status, finished_at, error_summary_json 
                        FROM {scm_schema}.sync_runs WHERE run_id = %s
                    """, (run_id,))
                    row = cur.fetchone()
                    
                    assert row[0] == "failed", f"状态应为 failed，实际: {row[0]}"
                    assert row[1] is not None, "finished_at 应不为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_non_expired_run_not_detected(self, migrated_db):
        """未超时的 running run 不应被检测"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_non_expired_run.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                run_id = str(uuid.uuid4())
                # 创建一个刚开始的 running run
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running', now())
                """, (run_id, repo_id))

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_running_runs, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_runs = list_expired_running_runs(
                    test_conn, max_duration_seconds=1800, limit=100
                )
                
                expired_run_ids = [str(r["run_id"]) for r in expired_runs]
                assert run_id not in expired_run_ids, f"不应检测到未超时 run {run_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestExpiredLockRecovery:
    """过期 lock 回收测试"""

    def test_expired_lock_detected(self, migrated_db):
        """检测过期的锁"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_expired_lock.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个过期的锁
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker', 
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """, (repo_id,))
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_locks, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=100)
                
                expired_lock_ids = [l["lock_id"] for l in expired_locks]
                assert lock_id in expired_lock_ids, f"应该检测到过期锁 {lock_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_expired_lock_force_released(self, migrated_db):
        """过期锁被强制释放"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lock_released.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker', 
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """, (repo_id,))
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import force_release_lock, get_conn
            
            test_conn = get_conn(dsn)
            try:
                success = force_release_lock(test_conn, lock_id)
                test_conn.commit()
                
                assert success is True
                
                # 验证锁已释放
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT locked_by, locked_at 
                        FROM {scm_schema}.sync_locks WHERE lock_id = %s
                    """, (lock_id,))
                    row = cur.fetchone()
                    
                    assert row[0] is None, "locked_by 应为 NULL"
                    assert row[1] is None, "locked_at 应为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_non_expired_lock_not_detected(self, migrated_db):
        """未过期的锁不应被检测"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_non_expired_lock.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # 创建一个未过期的锁（刚刚锁定）
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'active-worker', now(), 60)
                    RETURNING lock_id
                """, (repo_id,))
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import list_expired_locks, get_conn
            
            test_conn = get_conn(dsn)
            try:
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=100)
                
                expired_lock_ids = [l["lock_id"] for l in expired_locks]
                assert lock_id not in expired_lock_ids, f"不应检测到未过期锁 {lock_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestReaperProcessFunctions:
    """Reaper 处理函数测试"""

    def test_process_expired_jobs_to_failed(self, migrated_db):
        """process_expired_jobs 将任务转为 failed"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_process_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                # 使用 to_failed 策略处理
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_failed"] >= 1
                assert stats["errors"] == 0
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "failed"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_to_dead(self, migrated_db):
        """process_expired_jobs 达到 max_attempts 时转为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_process_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]
                
                # attempts = max_attempts
                cur.execute(f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """, (repo_id,))
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            
            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import process_expired_jobs, JobRecoveryPolicy
            
            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                
                stats = process_expired_jobs(
                    test_conn, expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )
                
                assert stats["to_dead"] >= 1
                
                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    assert row[0] == "dead"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()
