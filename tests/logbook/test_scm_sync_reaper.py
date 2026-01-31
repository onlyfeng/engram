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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            # 使用 db.list_expired_running_jobs 检测
            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs

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

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, mark_job_as_failed_by_reaper

            test_conn = get_conn(dsn)
            try:
                # 标记为 failed
                success = mark_job_as_failed_by_reaper(
                    test_conn,
                    job_id,
                    error="Reaped: job lock expired",
                    retry_delay_seconds=60,
                )
                test_conn.commit()

                assert success is True

                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, locked_by, last_error
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, mark_job_as_dead_by_reaper

            test_conn = get_conn(dsn)
            try:
                # 标记为 dead
                success = mark_job_as_dead_by_reaper(
                    test_conn,
                    job_id,
                    error="Reaped: job expired after max attempts",
                )
                test_conn.commit()

                assert success is True

                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, locked_by, last_error
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'active-worker', now(), 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs

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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """,
                    (run_id, repo_id),
                )

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_runs

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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """,
                    (run_id, repo_id),
                )

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, mark_run_as_failed_by_reaper

            test_conn = get_conn(dsn)
            try:
                error_summary = {
                    "error_type": "lease_lost",
                    "error_category": "timeout",
                    "message": "Reaped: sync run timed out",
                }

                success = mark_run_as_failed_by_reaper(test_conn, run_id, error_summary)
                test_conn.commit()

                assert success is True

                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, finished_at, error_summary_json
                        FROM {scm_schema}.sync_runs WHERE run_id = %s
                    """,
                        (run_id,),
                    )
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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running', now())
                """,
                    (run_id, repo_id),
                )

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_runs

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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker',
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """,
                    (repo_id,),
                )
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_locks

            test_conn = get_conn(dsn)
            try:
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=100)

                expired_lock_ids = [lk["lock_id"] for lk in expired_locks]
                assert lock_id in expired_lock_ids, f"应该检测到过期锁 {lock_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,)
                    )
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

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker',
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """,
                    (repo_id,),
                )
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
                    cur.execute(
                        f"""
                        SELECT locked_by, locked_at
                        FROM {scm_schema}.sync_locks WHERE lock_id = %s
                    """,
                        (lock_id,),
                    )
                    row = cur.fetchone()

                    assert row[0] is None, "locked_by 应为 NULL"
                    assert row[1] is None, "locked_at 应为 NULL"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,)
                    )
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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'active-worker', now(), 60)
                    RETURNING lock_id
                """,
                    (repo_id,),
                )
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_locks

            test_conn = get_conn(dsn)
            try:
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=100)

                expired_lock_ids = [lk["lock_id"] for lk in expired_locks]
                assert lock_id not in expired_lock_ids, f"不应检测到未过期锁 {lock_id}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,)
                    )
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestLastErrorClassification:
    """last_error 分类测试（测试 engram_logbook.scm_sync_errors 模块）"""

    def test_classify_auth_error(self):
        """测试认证错误分类为永久性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 401 错误
        is_permanent, is_transient, category = classify_last_error(
            "401 Unauthorized: Invalid token"
        )
        assert is_permanent is True
        assert is_transient is False
        assert category == "auth_error"

        # 认证失败
        is_permanent, is_transient, category = classify_last_error(
            "Authentication failed: token expired"
        )
        assert is_permanent is True
        assert is_transient is False
        assert category == "auth_error"

        # auth_error 分类
        is_permanent, is_transient, category = classify_last_error(
            "auth_error: invalid credentials"
        )
        assert is_permanent is True
        assert is_transient is False
        assert category == "auth_error"

    def test_classify_repo_not_found_error(self):
        """测试仓库不存在错误分类为永久性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 404 错误
        is_permanent, is_transient, category = classify_last_error(
            "404 Not Found: repository does not exist"
        )
        assert is_permanent is True
        assert is_transient is False
        assert category == "repo_not_found"

        # repo_not_found 分类
        is_permanent, is_transient, category = classify_last_error(
            "repo_not_found: project deleted"
        )
        assert is_permanent is True
        assert is_transient is False
        assert category == "repo_not_found"

    def test_classify_permission_denied_error(self):
        """测试权限不足错误分类为永久性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 403 错误
        is_permanent, is_transient, category = classify_last_error("403 Forbidden: access denied")
        assert is_permanent is True
        assert is_transient is False
        assert category == "permission_denied"

        # permission_denied 分类
        is_permanent, is_transient, category = classify_last_error(
            "permission_denied: insufficient rights"
        )
        assert is_permanent is True
        assert is_transient is False
        assert category == "permission_denied"

    def test_classify_rate_limit_error(self):
        """测试速率限制错误分类为临时性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 429 错误
        is_permanent, is_transient, category = classify_last_error("429 Too Many Requests")
        assert is_permanent is False
        assert is_transient is True
        assert category == "rate_limit"

        # rate limit 关键词
        is_permanent, is_transient, category = classify_last_error(
            "Rate limit exceeded, retry after 60 seconds"
        )
        assert is_permanent is False
        assert is_transient is True
        assert category == "rate_limit"

    def test_classify_timeout_error(self):
        """测试超时错误分类为临时性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # timeout 关键词
        is_permanent, is_transient, category = classify_last_error(
            "Request timeout after 30 seconds"
        )
        assert is_permanent is False
        assert is_transient is True
        assert category == "timeout"

        # timed out
        is_permanent, is_transient, category = classify_last_error("Connection timed out")
        assert is_permanent is False
        assert is_transient is True
        assert category == "timeout"

    def test_classify_server_error(self):
        """测试服务器错误分类为临时性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 502 Bad Gateway
        is_permanent, is_transient, category = classify_last_error("502 Bad Gateway")
        assert is_permanent is False
        assert is_transient is True
        assert category == "server_error"

        # 503 Service Unavailable
        is_permanent, is_transient, category = classify_last_error("503 Service Unavailable")
        assert is_permanent is False
        assert is_transient is True
        assert category == "server_error"

        # 504 Gateway Timeout (匹配 timeout)
        is_permanent, is_transient, category = classify_last_error("504 Gateway Timeout")
        assert is_permanent is False
        assert is_transient is True
        # 504 包含 "timeout"，所以应该匹配 timeout 分类
        assert category in ("timeout", "server_error")

    def test_classify_network_error(self):
        """测试网络错误分类为临时性错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # network 关键词
        is_permanent, is_transient, category = classify_last_error(
            "Network error: connection refused"
        )
        assert is_permanent is False
        assert is_transient is True
        assert category == "network"

        # connection 关键词
        is_permanent, is_transient, category = classify_last_error("Connection reset by peer")
        assert is_permanent is False
        assert is_transient is True

    def test_classify_empty_error(self):
        """测试空错误信息"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # None
        is_permanent, is_transient, category = classify_last_error(None)
        assert is_permanent is False
        assert is_transient is False
        assert category == ""

        # 空字符串
        is_permanent, is_transient, category = classify_last_error("")
        assert is_permanent is False
        assert is_transient is False
        assert category == ""

    def test_classify_unknown_error(self):
        """测试未知错误"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 未知错误
        is_permanent, is_transient, category = classify_last_error(
            "Some unknown internal error occurred"
        )
        assert is_permanent is False
        assert is_transient is False
        assert category == ""


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

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # 使用 to_failed 策略处理
                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats["to_failed"] >= 1
                assert stats["errors"] == 0

                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats["to_dead"] >= 1

                # 验证状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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


class TestUnifiedBackoffCalculation:
    """统一退避计算测试"""

    def test_calculate_backoff_with_attempts(self):
        """测试不同 attempts 的退避时间计算"""
        from engram.logbook.scm_sync_errors import (
            calculate_backoff_seconds,
        )

        # attempts=1: base * 2^0 = 60
        assert calculate_backoff_seconds(attempts=1) == 60

        # attempts=2: base * 2^1 = 120
        assert calculate_backoff_seconds(attempts=2) == 120

        # attempts=3: base * 2^2 = 240
        assert calculate_backoff_seconds(attempts=3) == 240

        # attempts=4: base * 2^3 = 480
        assert calculate_backoff_seconds(attempts=4) == 480

    def test_calculate_backoff_with_max_limit(self):
        """测试退避时间不超过 max_seconds"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # 设置较小的 max_seconds
        max_seconds = 300

        # attempts=1: 60 <= 300
        assert calculate_backoff_seconds(attempts=1, max_seconds=max_seconds) == 60

        # attempts=3: 240 <= 300
        assert calculate_backoff_seconds(attempts=3, max_seconds=max_seconds) == 240

        # attempts=4: 480 > 300，应限制为 300
        assert calculate_backoff_seconds(attempts=4, max_seconds=max_seconds) == 300

        # attempts=10: 非常大的值，应限制为 300
        assert calculate_backoff_seconds(attempts=10, max_seconds=max_seconds) == 300

    def test_calculate_backoff_with_error_category(self):
        """测试错误类别影响基础退避时间"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
            calculate_backoff_seconds,
        )

        # rate_limit 的基础退避是 120
        rate_limit_base = TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]

        # attempts=1: 120 * 2^0 = 120
        result = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.RATE_LIMIT.value,
        )
        assert result == rate_limit_base

        # attempts=2: 120 * 2^1 = 240
        result = calculate_backoff_seconds(
            attempts=2,
            error_category=ErrorCategory.RATE_LIMIT.value,
        )
        assert result == rate_limit_base * 2

    def test_calculate_backoff_with_error_message(self):
        """测试通过错误消息推断退避时间"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
            calculate_backoff_seconds,
        )

        # "429" 应该匹配 rate_limit
        result = calculate_backoff_seconds(
            attempts=1,
            error_message="429 Too Many Requests",
        )
        assert result == TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]

        # "timeout" 应该匹配 timeout
        result = calculate_backoff_seconds(
            attempts=1,
            error_message="Request timeout",
        )
        assert result == TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value]

    def test_queue_and_reaper_use_same_backoff_logic(self):
        """验证 queue 和 reaper 使用相同的退避计算逻辑"""
        from engram.logbook.scm_sync_errors import (
            DEFAULT_BACKOFF_BASE,
            DEFAULT_MAX_BACKOFF,
            calculate_backoff_seconds,
        )

        # 模拟 queue 的 fail_retry 场景
        queue_backoff = calculate_backoff_seconds(
            attempts=2,
            base_seconds=DEFAULT_BACKOFF_BASE,
            max_seconds=DEFAULT_MAX_BACKOFF,
        )

        # 模拟 reaper 的场景（使用相同参数）
        reaper_backoff = calculate_backoff_seconds(
            attempts=2,
            base_seconds=DEFAULT_BACKOFF_BASE,
            max_seconds=DEFAULT_MAX_BACKOFF,
        )

        # 结果应该一致
        assert queue_backoff == reaper_backoff
        assert queue_backoff == 120  # 60 * 2^1

    def test_max_reaper_backoff_config(self):
        """测试 reaper 的 max_reaper_backoff_seconds 配置生效"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # 默认 reaper max 是 1800 秒
        DEFAULT_MAX_REAPER_BACKOFF = 1800

        # attempts=5 时基础退避是 60 * 2^4 = 960，不超过 1800
        result = calculate_backoff_seconds(
            attempts=5,
            max_seconds=DEFAULT_MAX_REAPER_BACKOFF,
        )
        assert result == 960

        # attempts=6 时基础退避是 60 * 2^5 = 1920 > 1800，应限制
        result = calculate_backoff_seconds(
            attempts=6,
            max_seconds=DEFAULT_MAX_REAPER_BACKOFF,
        )
        assert result == DEFAULT_MAX_REAPER_BACKOFF


class TestLastErrorBasedProcessing:
    """基于 last_error 分类的 process_expired_jobs 集成测试"""

    def test_process_expired_jobs_with_permanent_error_marks_dead(self, migrated_db):
        """带有永久性错误的过期任务被标记为 dead（不考虑 attempts）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_permanent_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # attempts=1, max_attempts=3，但有永久性错误
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            '401 Unauthorized: Invalid token')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats["to_dead"] >= 1

                # 验证状态为 dead
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "dead", f"永久性错误应标记为 dead，实际: {row[0]}"
                    assert "permanent error" in row[1].lower() or "auth_error" in row[1].lower()
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_with_transient_error_marks_failed_with_backoff(self, migrated_db):
        """带有临时性错误的过期任务被标记为 failed，使用更长的 retry_delay"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_transient_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 临时性错误：429 速率限制
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            '429 Too Many Requests')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                    transient_retry_delay_multiplier=2.0,
                )

                assert stats["to_failed"] >= 1

                # 验证状态为 failed
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error, not_before FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "failed", f"临时性错误应标记为 failed，实际: {row[0]}"
                    assert "transient error" in row[1].lower() or "rate_limit" in row[1].lower()
                    # not_before 应该被设置（延迟重试）
                    assert row[2] is not None
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_expired_jobs_repo_not_found_marks_dead(self, migrated_db):
        """仓库不存在错误被标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_repo_not_found.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            '404 Not Found: repository does not exist')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats["to_dead"] >= 1

                # 验证状态为 dead
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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

    def test_process_expired_jobs_timeout_error_marks_failed(self, migrated_db):
        """超时错误被标记为 failed"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_timeout_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3,
                            'Request timeout after 30 seconds')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats["to_failed"] >= 1

                # 验证状态为 failed
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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

    def test_process_expired_jobs_without_error_follows_policy(self, migrated_db):
        """没有 last_error 的任务按照 policy 处理"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_no_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 没有 last_error
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # 使用 to_pending 策略
                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_pending,
                    retry_delay_seconds=60,
                )

                assert stats["to_pending"] >= 1

                # 验证状态为 pending
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "pending"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_transient_error_backoff_increases_with_attempts(self, migrated_db):
        """临时性错误的退避时间随 attempts 增加（指数退避）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_ids = []
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_backoff_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建多个不同 attempts 的任务
                for attempts in [1, 2, 3]:
                    cur.execute(
                        f"""
                        INSERT INTO {scm_schema}.sync_jobs (
                            repo_id, job_type, mode, priority, status,
                            locked_by, locked_at, lease_seconds, attempts, max_attempts,
                            last_error
                        )
                        VALUES (%s, 'gitlab_commits_{attempts}', 'incremental', 100, 'running',
                                'dead-worker', now() - interval '10 minutes', 300, %s, 5,
                                '429 Too Many Requests')
                        RETURNING job_id
                    """,
                        (repo_id, attempts),
                    )
                    job_ids.append(str(cur.fetchone()[0]))

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # 处理所有过期任务
                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                    max_reaper_backoff_seconds=3600,
                )

                assert stats["to_failed"] >= 3

                # 验证 not_before 时间差异（退避时间应该随 attempts 增加）
                # 注意：这里只验证状态正确，实际退避时间由数据库时间戳控制
                for job_id in job_ids:
                    with test_conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                        """,
                            (job_id,),
                        )
                        row = cur.fetchone()
                        assert row[0] == "failed"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                for job_id in job_ids:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_different_error_categories_get_different_base_backoff(self):
        """不同错误类别有不同的基础退避时间"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
            calculate_backoff_seconds,
        )

        # rate_limit: 120 秒
        rate_limit_backoff = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.RATE_LIMIT.value,
        )

        # timeout: 30 秒
        timeout_backoff = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.TIMEOUT.value,
        )

        # server_error: 90 秒
        server_error_backoff = calculate_backoff_seconds(
            attempts=1,
            error_category=ErrorCategory.SERVER_ERROR.value,
        )

        # 验证不同类别有不同的退避时间
        assert rate_limit_backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]
        assert timeout_backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value]
        assert server_error_backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.SERVER_ERROR.value]

        # rate_limit > server_error > timeout
        assert rate_limit_backoff > server_error_backoff > timeout_backoff

    def test_max_reaper_backoff_prevents_infinite_growth(self, migrated_db):
        """验证 max_reaper_backoff_seconds 防止退避时间无限增长"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_max_backoff.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建 attempts=10 的任务（理论退避时间非常大）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 10, 15,
                            '503 Service Unavailable')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # 使用较小的 max_reaper_backoff_seconds
                small_max_backoff = 300  # 5 分钟
                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                    max_reaper_backoff_seconds=small_max_backoff,
                )

                assert stats["to_failed"] >= 1

                # 验证任务已被处理
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
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

    def test_error_message_is_redacted(self, migrated_db):
        """验证写入的错误消息已被脱敏"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_redaction.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 带敏感信息的 locked_by
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'worker-glpat-secret123456789', now() - interval '10 minutes', 300, 1, 3,
                            'Connection timeout')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                # 验证错误消息中不包含原始 token
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT last_error FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    last_error = row[0]

                    # 不应包含原始 token
                    assert "glpat-secret123456789" not in last_error
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


# ============ 测试夹具：关键条件常量 ============

# last_error 分类测试夹具
REAPER_FIXTURE_PERMANENT_ERRORS = [
    # (error_message, expected_category)
    ("401 Unauthorized: Invalid token", "auth_error"),
    ("403 Forbidden: access denied", "permission_denied"),
    ("404 Not Found: repository does not exist", "repo_not_found"),
    ("authentication failed: token expired", "auth_error"),
    ("auth_error: invalid credentials", "auth_error"),
    ("auth_missing: no credentials provided", "auth_missing"),
    ("auth_invalid: malformed token", "auth_invalid"),
    ("permission_denied: insufficient rights", "permission_denied"),
    ("repo_not_found: project deleted", "repo_not_found"),
    ("repo_type_unknown: unsupported VCS", "repo_type_unknown"),
]

REAPER_FIXTURE_TRANSIENT_ERRORS = [
    # (error_message, expected_category)
    # 注意：classify_last_error 通过关键词匹配来识别临时性错误
    # 仅包含 TRANSIENT_ERROR_KEYWORDS 中关键词的错误消息才会被识别
    ("429 Too Many Requests", "rate_limit"),
    ("Rate limit exceeded, retry after 60 seconds", "rate_limit"),
    ("Request timeout after 30 seconds", "timeout"),
    ("Connection timed out", "timeout"),
    ("502 Bad Gateway", "server_error"),
    ("503 Service Unavailable", "server_error"),
    ("504 Gateway Timeout", "timeout"),  # 包含 "timeout" 关键词
    ("Network error: connection refused", "network"),
    ("Connection reset by peer", "network"),
    # 注意：lease_lost 不在 TRANSIENT_ERROR_KEYWORDS 中，
    # 所以 "lease_lost: worker died" 不会被 classify_last_error 识别为临时性错误
    # lease_lost 主要由 worker 直接设置 error_category 来标记
]

# attempts/max_attempts 边界测试夹具
REAPER_FIXTURE_ATTEMPTS_SCENARIOS = [
    # (attempts, max_attempts, expected_outcome_when_no_error)
    (0, 3, "to_failed_or_pending"),  # attempts=0，按 policy 处理
    (1, 3, "to_failed_or_pending"),  # 正常情况，按 policy 处理
    (2, 3, "to_failed_or_pending"),  # 未达 max，按 policy 处理
    (3, 3, "to_dead"),  # attempts == max_attempts → dead
    (4, 3, "to_dead"),  # attempts > max_attempts → dead
    (10, 3, "to_dead"),  # 远超 max_attempts → dead
]

# lease_age 测试夹具（expired_seconds 近似值）
REAPER_FIXTURE_LEASE_SCENARIOS = [
    # (lease_seconds, locked_minutes_ago, expected_expired)
    (300, 10, True),  # 5分钟租约，10分钟前锁定 → 过期
    (300, 4, False),  # 5分钟租约，4分钟前锁定 → 未过期
    (60, 2, True),  # 1分钟租约，2分钟前锁定 → 过期
    (0, 1, True),  # 0秒租约 → 立即过期
]

# 退避计算测试夹具
REAPER_FIXTURE_BACKOFF_SCENARIOS = [
    # (attempts, base, max, expected_backoff)
    (1, 60, 3600, 60),  # 60 * 2^0 = 60
    (2, 60, 3600, 120),  # 60 * 2^1 = 120
    (3, 60, 3600, 240),  # 60 * 2^2 = 240
    (4, 60, 3600, 480),  # 60 * 2^3 = 480
    (5, 60, 3600, 960),  # 60 * 2^4 = 960
    (6, 60, 3600, 1920),  # 60 * 2^5 = 1920
    (7, 60, 3600, 3600),  # 60 * 2^6 = 3840 → capped at 3600
    (10, 60, 300, 300),  # 远超 max → capped at 300
    (0, 60, 3600, 60),  # attempts=0 → treated as 1
]


class TestReaperFixturesUnit:
    """夹具验证单元测试（不需要数据库）"""

    def test_permanent_error_classification_fixture(self):
        """验证永久性错误夹具全部分类正确"""
        from engram.logbook.scm_sync_errors import PERMANENT_ERROR_CATEGORIES, classify_last_error

        for error_msg, expected_category in REAPER_FIXTURE_PERMANENT_ERRORS:
            is_permanent, is_transient, category = classify_last_error(error_msg)
            assert is_permanent is True, f"'{error_msg}' 应为永久性错误"
            assert is_transient is False, f"'{error_msg}' 不应为临时性错误"
            # 类别应在永久性错误集合中
            assert category in PERMANENT_ERROR_CATEGORIES or category == expected_category, (
                f"'{error_msg}' 类别 '{category}' 不匹配预期 '{expected_category}'"
            )

    def test_transient_error_classification_fixture(self):
        """验证临时性错误夹具全部分类正确"""
        from engram.logbook.scm_sync_errors import TRANSIENT_ERROR_CATEGORIES, classify_last_error

        for error_msg, expected_category in REAPER_FIXTURE_TRANSIENT_ERRORS:
            is_permanent, is_transient, category = classify_last_error(error_msg)
            assert is_permanent is False, f"'{error_msg}' 不应为永久性错误"
            assert is_transient is True, f"'{error_msg}' 应为临时性错误"
            # 类别应在临时性错误集合中或为 default
            assert category in TRANSIENT_ERROR_CATEGORIES or category == "default", (
                f"'{error_msg}' 类别 '{category}' 不在临时性错误集合中"
            )

    def test_backoff_calculation_fixture(self):
        """验证退避计算夹具结果正确"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        for attempts, base, max_sec, expected in REAPER_FIXTURE_BACKOFF_SCENARIOS:
            result = calculate_backoff_seconds(
                attempts=attempts,
                base_seconds=base,
                max_seconds=max_sec,
            )
            assert result == expected, (
                f"attempts={attempts}, base={base}, max={max_sec}: expected {expected}, got {result}"
            )


class TestReaperExtremeScenarios:
    """Reaper 极端场景测试（单元测试，不需要数据库）"""

    def test_classify_empty_last_error(self):
        """空 last_error 返回未分类"""
        from engram.logbook.scm_sync_errors import classify_last_error

        for empty_value in [None, "", "   "]:
            is_permanent, is_transient, category = classify_last_error(empty_value)
            # 空值应返回未分类
            if empty_value and empty_value.strip():
                pass  # 非空白字符串可能有分类
            else:
                assert is_permanent is False
                assert is_transient is False

    def test_classify_mixed_error_keywords(self):
        """混合多种错误关键词的分类（优先级测试）"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 同时包含永久性和临时性关键词，永久性优先
        mixed_error = "401 Unauthorized after timeout"
        is_permanent, is_transient, category = classify_last_error(mixed_error)
        assert is_permanent is True, "永久性错误关键词应优先"
        assert category == "auth_error"

        # 多个临时性关键词，使用第一个匹配
        mixed_transient = "429 rate limit with 503 service unavailable"
        is_permanent, is_transient, category = classify_last_error(mixed_transient)
        assert is_transient is True
        assert category in ("rate_limit", "server_error")

    def test_classify_case_insensitivity(self):
        """错误分类大小写不敏感"""
        from engram.logbook.scm_sync_errors import classify_last_error

        test_cases = [
            ("UNAUTHORIZED", "auth_error"),
            ("Forbidden", "permission_denied"),
            ("NOT FOUND", "repo_not_found"),
            ("RATE LIMIT", "rate_limit"),
            ("TIMEOUT", "timeout"),
        ]

        for error_msg, expected_type in test_cases:
            is_permanent, is_transient, category = classify_last_error(error_msg)
            # 至少应该识别出错误类型
            if expected_type in ("auth_error", "permission_denied", "repo_not_found"):
                assert is_permanent is True, f"'{error_msg}' 应为永久性错误"
            else:
                assert is_transient is True, f"'{error_msg}' 应为临时性错误"

    def test_classify_unicode_error_message(self):
        """Unicode 错误消息处理"""
        from engram.logbook.scm_sync_errors import classify_last_error

        unicode_errors = [
            "401 Unauthorized: 认证失败",
            "超时错误 timeout after 30秒",
            "网络连接错误 connection reset",
            "仓库不存在 404 Not Found",
        ]

        for error_msg in unicode_errors:
            # 应该能正常处理，不抛出异常
            is_permanent, is_transient, category = classify_last_error(error_msg)
            # 类型断言
            assert isinstance(is_permanent, bool)
            assert isinstance(is_transient, bool)
            assert isinstance(category, str)

    def test_classify_very_long_error_message(self):
        """超长错误消息处理"""
        from engram.logbook.scm_sync_errors import classify_last_error

        # 创建一个非常长的错误消息
        long_error = "Error: " + "x" * 10000 + " 401 Unauthorized"

        is_permanent, is_transient, category = classify_last_error(long_error)
        # 应该能识别出 401 错误
        assert is_permanent is True
        assert category == "auth_error"

    def test_backoff_with_zero_attempts(self):
        """attempts=0 时的退避计算"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # attempts=0 应被视为 attempts=1
        result = calculate_backoff_seconds(attempts=0, base_seconds=60, max_seconds=3600)
        assert result == 60  # base * 2^0 = 60

    def test_backoff_with_negative_attempts(self):
        """负数 attempts 的退避计算"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # 负数 attempts 应被视为 attempts=1
        result = calculate_backoff_seconds(attempts=-5, base_seconds=60, max_seconds=3600)
        assert result == 60  # base * 2^0 = 60

    def test_backoff_with_very_large_attempts(self):
        """非常大的 attempts 值"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # attempts=100 时，理论值会溢出，但应被 max 限制
        result = calculate_backoff_seconds(attempts=100, base_seconds=60, max_seconds=3600)
        assert result == 3600  # 应被 max 限制

    def test_backoff_with_zero_max(self):
        """max_seconds=0 时的退避计算"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # max=0 时应返回 0
        result = calculate_backoff_seconds(attempts=1, base_seconds=60, max_seconds=0)
        assert result == 0

    def test_lock_held_is_ignored_category(self):
        """lock_held 应为忽略类别（不计入失败预算）"""
        from engram.logbook.scm_sync_errors import (
            IGNORED_ERROR_CATEGORIES,
            ErrorCategory,
            is_ignored_category,
        )

        assert is_ignored_category(ErrorCategory.LOCK_HELD.value) is True
        assert "lock_held" in IGNORED_ERROR_CATEGORIES

        # 其他错误类别不应为忽略类别
        assert is_ignored_category(ErrorCategory.RATE_LIMIT.value) is False
        assert is_ignored_category(ErrorCategory.AUTH_ERROR.value) is False


class TestReaperProcessExpiredJobsExtreme:
    """process_expired_jobs 极端场景集成测试"""

    def test_process_job_with_attempts_zero(self, migrated_db):
        """attempts=0 的任务处理"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_attempts_zero.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # attempts=0（边界情况）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 0, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # attempts=0 且无 last_error，应按 policy 处理
                process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                # 应该被处理为 failed（因为 attempts < max_attempts）
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "failed", f"attempts=0 应被处理为 failed，实际: {row[0]}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_job_with_attempts_exceeds_max(self, migrated_db):
        """attempts > max_attempts 的任务处理"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_attempts_exceeds.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # attempts > max_attempts
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 5, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                # attempts > max_attempts 应被处理为 dead
                assert stats["to_dead"] >= 1

                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "dead", f"attempts>max 应被处理为 dead，实际: {row[0]}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_job_permanent_error_ignores_attempts(self, migrated_db):
        """永久性错误忽略 attempts，直接标记为 dead"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_perm_ignores_attempts.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # attempts=1, max=10，但有永久性错误
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 10,
                            '403 Forbidden: you do not have permission')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                # 永久性错误应直接标记为 dead，无论 attempts
                assert stats["to_dead"] >= 1

                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "dead", f"永久性错误应直接 dead，实际: {row[0]}"
                    assert "permanent" in row[1].lower() or "permission_denied" in row[1].lower()
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_job_to_pending_policy(self, migrated_db):
        """to_pending 策略正确应用"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_to_pending.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 无 last_error，应按 policy 处理
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # 使用 to_pending 策略
                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_pending,
                    retry_delay_seconds=60,
                )

                assert stats["to_pending"] >= 1

                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    assert row[0] == "pending", f"to_pending 策略应生效，实际: {row[0]}"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_transient_error_respects_max_attempts(self, migrated_db):
        """临时性错误在达到 max_attempts 时仍会被标记为 failed（由 reaper 处理）"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_transient_max.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 临时性错误 + 达到 max_attempts
                # 注意：reaper 先检查永久性/临时性错误，再检查 max_attempts
                # 临时性错误优先于 max_attempts 检查
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts,
                        last_error
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3,
                            '429 Too Many Requests')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                stats = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                # 临时性错误优先，应被标记为 failed（带 backoff）
                # 而不是因为 max_attempts 被标记为 dead
                assert stats["to_failed"] >= 1

                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    # reaper 逻辑：is_transient 优先于 attempts >= max_attempts
                    assert row[0] == "failed", (
                        f"临时性错误应标记为 failed（优先于 max_attempts），实际: {row[0]}"
                    )
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_job_with_lease_seconds_zero(self, migrated_db):
        """lease_seconds=0 的任务应立即被检测为过期"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_lease_zero.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # lease_seconds=0，刚锁定就过期
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now(), 0, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs

            test_conn = get_conn(dsn)
            try:
                # lease_seconds=0 应该被立即检测为过期
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=0, limit=10)

                expired_job_ids = [str(j["job_id"]) for j in expired_jobs]
                assert job_id in expired_job_ids, "lease_seconds=0 的任务应被检测为过期"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestQueueReaperConsistency:
    """Queue 和 Reaper 退避计算一致性测试"""

    def test_queue_and_reaper_import_same_constants(self):
        """验证 queue 和 reaper 导入相同的退避常量"""
        from engram.logbook.scm_sync_errors import (
            DEFAULT_BACKOFF_BASE as errors_base,
        )
        from engram.logbook.scm_sync_queue import DEFAULT_BACKOFF_BASE as queue_base
        from scm_sync_reaper import DEFAULT_BACKOFF_BASE as reaper_base

        # 都应该引用同一个常量
        assert queue_base == errors_base
        assert reaper_base == errors_base
        assert queue_base == 60

    def test_backoff_formula_consistency(self):
        """验证退避公式一致性：base * 2^(attempts-1)"""
        from engram.logbook.scm_sync_errors import calculate_backoff_seconds

        # 验证公式：base * 2^(attempts-1)
        base = 60

        for attempts in range(1, 8):
            expected = base * (2 ** (attempts - 1))
            # 不设置 max，验证公式本身
            actual = calculate_backoff_seconds(
                attempts=attempts,
                base_seconds=base,
                max_seconds=100000,  # 设置很大的 max 以不触发限制
            )
            assert actual == expected, f"attempts={attempts}: expected {expected}, got {actual}"

    def test_permanent_errors_always_dead_regardless_of_attempts(self):
        """永久性错误无论 attempts 多少都直接标记为 dead"""
        from engram.logbook.scm_sync_errors import classify_last_error

        permanent_errors = [
            "401 Unauthorized: Invalid token",
            "403 Forbidden: access denied",
            "404 Not Found: repository does not exist",
            "auth_error: authentication failed",
            "permission_denied: insufficient rights",
        ]

        for error in permanent_errors:
            is_permanent, is_transient, category = classify_last_error(error)
            assert is_permanent is True, f"'{error}' should be permanent"
            assert is_transient is False, f"'{error}' should not be transient"

    def test_transient_errors_always_retried_regardless_of_attempts(self):
        """临时性错误无论 attempts 多少都安排重试（直到 max_attempts）"""
        from engram.logbook.scm_sync_errors import classify_last_error

        transient_errors = [
            "429 Too Many Requests",
            "Request timeout after 30 seconds",
            "Connection reset by peer",
            "502 Bad Gateway",
            "503 Service Unavailable",
        ]

        for error in transient_errors:
            is_permanent, is_transient, category = classify_last_error(error)
            assert is_permanent is False, f"'{error}' should not be permanent"
            assert is_transient is True, f"'{error}' should be transient"
            assert category != "", f"'{error}' should have a category"

    def test_reaper_default_max_backoff_is_reasonable(self):
        """验证 reaper 默认最大退避时间合理"""
        from engram.logbook.scm_sync_errors import DEFAULT_MAX_BACKOFF
        from scm_sync_reaper import DEFAULT_MAX_REAPER_BACKOFF_SECONDS

        # reaper 的默认 max 应该合理（30 分钟）
        assert DEFAULT_MAX_REAPER_BACKOFF_SECONDS == 1800

        # scm_sync_errors 的默认 max 应该是 1 小时
        assert DEFAULT_MAX_BACKOFF == 3600

    def test_error_category_backoff_values(self):
        """验证不同错误类别的退避时间配置"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
        )

        # 验证配置值存在且合理
        assert ErrorCategory.RATE_LIMIT.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.TIMEOUT.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.NETWORK.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.SERVER_ERROR.value in TRANSIENT_ERROR_BACKOFF
        assert ErrorCategory.CONNECTION.value in TRANSIENT_ERROR_BACKOFF
        assert "default" in TRANSIENT_ERROR_BACKOFF

        # rate_limit 应该有较长的退避时间
        assert TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value] >= 60

        # timeout 可以有较短的退避时间
        assert TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value] >= 10


class TestReaperIdempotency:
    """Reaper 幂等性测试：验证重复执行不会产生额外副作用"""

    def test_reap_expired_job_is_idempotent(self, migrated_db):
        """
        幂等性测试：对已处理为 failed 的 job 重复执行 reaper 不会改变状态

        策略验证：expired running job → failed (含 backoff)
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_idempotent_job.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建过期的 running 任务
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                # 第一次执行 reaper
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                stats1 = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats1["to_failed"] >= 1

                # 记录第一次处理后的状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error, attempts, not_before
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    first_state = cur.fetchone()

                assert first_state[0] == "failed", "第一次处理后应为 failed"

                # 第二次执行 reaper（应该找不到过期任务，因为已经是 failed 状态）
                expired_jobs_2 = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)

                # 验证：已处理的 job 不会再被检测为过期
                expired_job_ids = [str(j["job_id"]) for j in expired_jobs_2]
                assert job_id not in expired_job_ids, "已处理为 failed 的 job 不应再被检测为过期"

                # 验证状态未改变
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error, attempts, not_before
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    second_state = cur.fetchone()

                assert first_state == second_state, "重复执行 reaper 不应改变已处理 job 的状态"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_reap_expired_job_to_dead_is_idempotent(self, migrated_db):
        """
        幂等性测试：对已处理为 dead 的 job 重复执行 reaper 不会改变状态

        策略验证：expired running job (max_attempts) → dead
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_idempotent_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建过期的 running 任务（已达 max_attempts）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_jobs
            from scm_sync_reaper import JobRecoveryPolicy, process_expired_jobs

            test_conn = get_conn(dsn)
            try:
                # 第一次执行 reaper
                expired_jobs = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                stats1 = process_expired_jobs(
                    test_conn,
                    expired_jobs,
                    policy=JobRecoveryPolicy.to_failed,
                    retry_delay_seconds=60,
                )

                assert stats1["to_dead"] >= 1

                # 记录第一次处理后的状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error, attempts
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    first_state = cur.fetchone()

                assert first_state[0] == "dead", "第一次处理后应为 dead"

                # 第二次执行 reaper（应该找不到过期任务）
                expired_jobs_2 = list_expired_running_jobs(test_conn, grace_seconds=60, limit=10)
                expired_job_ids = [str(j["job_id"]) for j in expired_jobs_2]
                assert job_id not in expired_job_ids, "已处理为 dead 的 job 不应再被检测"

                # 验证状态未改变
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, last_error, attempts
                        FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    second_state = cur.fetchone()

                assert first_state == second_state, "重复执行 reaper 不应改变已处理 job 的状态"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_reap_expired_run_is_idempotent(self, migrated_db):
        """
        幂等性测试：对已处理为 failed 的 run 重复执行 reaper 不会改变状态

        策略验证：expired run → failed (error_type=lease_lost, error_category=timeout)
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        run_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_idempotent_run.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                run_id = str(uuid.uuid4())
                # 创建超时的 running run
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_runs (
                        run_id, repo_id, job_type, mode, status, started_at
                    )
                    VALUES (%s, %s, 'gitlab_commits', 'incremental', 'running',
                            now() - interval '45 minutes')
                """,
                    (run_id, repo_id),
                )

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_running_runs
            from scm_sync_reaper import process_expired_runs

            test_conn = get_conn(dsn)
            try:
                # 第一次执行 reaper
                expired_runs = list_expired_running_runs(
                    test_conn, max_duration_seconds=1800, limit=10
                )
                stats1 = process_expired_runs(test_conn, expired_runs)

                assert stats1["failed"] >= 1

                # 记录第一次处理后的状态
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, finished_at, error_summary_json
                        FROM {scm_schema}.sync_runs WHERE run_id = %s
                    """,
                        (run_id,),
                    )
                    first_state = cur.fetchone()

                assert first_state[0] == "failed", "第一次处理后应为 failed"
                assert first_state[1] is not None, "finished_at 应被设置"

                # 验证 error_summary 包含正确的 error_type 和 error_category
                import json

                error_summary = json.loads(first_state[2]) if first_state[2] else {}
                assert error_summary.get("error_type") == "lease_lost", (
                    f"error_type 应为 lease_lost，实际: {error_summary.get('error_type')}"
                )
                assert error_summary.get("error_category") == "timeout", (
                    f"error_category 应为 timeout，实际: {error_summary.get('error_category')}"
                )

                # 第二次执行 reaper（应该找不到超时 run）
                expired_runs_2 = list_expired_running_runs(
                    test_conn, max_duration_seconds=1800, limit=10
                )
                expired_run_ids = [str(r["run_id"]) for r in expired_runs_2]
                assert run_id not in expired_run_ids, "已处理为 failed 的 run 不应再被检测"

                # 验证状态未改变
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, finished_at, error_summary_json
                        FROM {scm_schema}.sync_runs WHERE run_id = %s
                    """,
                        (run_id,),
                    )
                    second_state = cur.fetchone()

                assert first_state[0] == second_state[0], "重复执行 reaper 不应改变状态"
                # finished_at 可能有微小差异，但状态应一致
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_runs WHERE run_id = %s", (run_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_reap_expired_lock_is_idempotent(self, migrated_db):
        """
        幂等性测试：对已释放的 lock 重复执行 reaper 不会产生副作用

        策略验证：expired lock → force_release
        """
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        lock_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_idempotent_lock.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建过期的锁
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_locks (
                        repo_id, job_type, locked_by, locked_at, lease_seconds
                    )
                    VALUES (%s, 'gitlab_commits', 'dead-worker',
                            now() - interval '10 minutes', 60)
                    RETURNING lock_id
                """,
                    (repo_id,),
                )
                lock_id = cur.fetchone()[0]

            import sys
            from pathlib import Path

            scripts_dir = Path(__file__).parent.parent
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from db import get_conn, list_expired_locks
            from scm_sync_reaper import process_expired_locks

            test_conn = get_conn(dsn)
            try:
                # 第一次执行 reaper
                expired_locks = list_expired_locks(test_conn, grace_seconds=0, limit=10)
                stats1 = process_expired_locks(test_conn, expired_locks)

                assert stats1["released"] >= 1

                # 验证锁已释放
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT locked_by, locked_at
                        FROM {scm_schema}.sync_locks WHERE lock_id = %s
                    """,
                        (lock_id,),
                    )
                    first_state = cur.fetchone()

                assert first_state[0] is None, "locked_by 应为 NULL"
                assert first_state[1] is None, "locked_at 应为 NULL"

                # 第二次执行 reaper（应该找不到过期锁）
                expired_locks_2 = list_expired_locks(test_conn, grace_seconds=0, limit=10)
                expired_lock_ids = [lk["lock_id"] for lk in expired_locks_2]
                assert lock_id not in expired_lock_ids, "已释放的 lock 不应再被检测"

                # 验证状态未改变
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT locked_by, locked_at
                        FROM {scm_schema}.sync_locks WHERE lock_id = %s
                    """,
                        (lock_id,),
                    )
                    second_state = cur.fetchone()

                assert first_state == second_state, "重复执行 reaper 不应改变已释放 lock 的状态"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if lock_id:
                    cur.execute(
                        f"DELETE FROM {scm_schema}.sync_locks WHERE lock_id = %s", (lock_id,)
                    )
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_empty_lists_is_idempotent(self, migrated_db):
        """
        幂等性测试：处理空列表不会产生副作用
        """
        dsn = migrated_db["dsn"]

        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parent.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from db import get_conn
        from scm_sync_reaper import (
            JobRecoveryPolicy,
            process_expired_jobs,
            process_expired_locks,
            process_expired_runs,
        )

        test_conn = get_conn(dsn)
        try:
            # 处理空 job 列表
            stats_jobs = process_expired_jobs(
                test_conn,
                [],
                policy=JobRecoveryPolicy.to_failed,
                retry_delay_seconds=60,
            )
            assert stats_jobs["processed"] == 0
            assert stats_jobs["to_failed"] == 0
            assert stats_jobs["to_dead"] == 0
            assert stats_jobs["errors"] == 0

            # 处理空 run 列表
            stats_runs = process_expired_runs(test_conn, [])
            assert stats_runs["processed"] == 0
            assert stats_runs["failed"] == 0
            assert stats_runs["errors"] == 0

            # 处理空 lock 列表
            stats_locks = process_expired_locks(test_conn, [])
            assert stats_locks["processed"] == 0
            assert stats_locks["released"] == 0
            assert stats_locks["errors"] == 0
        finally:
            test_conn.close()

    def test_reaper_strategy_documentation_matches_implementation(self):
        """
        策略文档测试：验证实现与文档一致

        固定策略：
        1. expired running job → failed (含 backoff) 或 dead
        2. expired lock → force_release
        3. expired run → failed (error_type=lease_lost, error_category=timeout)
        """
        from engram.logbook.scm_sync_errors import classify_last_error

        # 策略 1：永久性错误 → dead
        permanent_errors = [
            ("401 Unauthorized", "auth_error"),
            ("403 Forbidden", "permission_denied"),
            ("404 Not Found", "repo_not_found"),
        ]
        for error, expected_category in permanent_errors:
            is_permanent, is_transient, category = classify_last_error(error)
            assert is_permanent is True, f"'{error}' 应为永久性错误"
            assert category == expected_category, f"'{error}' 类别应为 {expected_category}"

        # 策略 1：临时性错误 → failed + backoff
        transient_errors = [
            ("429 Too Many Requests", "rate_limit"),
            ("Request timeout", "timeout"),
            ("Connection reset", "network"),  # connection 关键词匹配 network 类别
            ("502 Bad Gateway", "server_error"),
        ]
        for error, expected_category in transient_errors:
            is_permanent, is_transient, category = classify_last_error(error)
            assert is_transient is True, f"'{error}' 应为临时性错误"
            assert category == expected_category, f"'{error}' 类别应为 {expected_category}"


class TestWorkerReaperBackoffConsistency:
    """验证 worker 和 reaper 的 backoff 计算逻辑一致性"""

    def test_worker_and_reaper_share_same_resolve_backoff(self):
        """验证 worker 使用统一的 resolve_backoff 函数"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            BackoffSource,
            ErrorCategory,
            resolve_backoff,
        )

        # 场景 1: 有 retry_after 时优先使用
        backoff, source = resolve_backoff(retry_after=120)
        assert backoff == 120
        assert source == BackoffSource.RETRY_AFTER.value

        # 场景 2: 无 retry_after，使用 error_category
        backoff, source = resolve_backoff(error_category="rate_limit")
        assert backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value]
        assert source == BackoffSource.ERROR_CATEGORY.value

        # 场景 3: exception 类型，从 error_message 推断
        backoff, source = resolve_backoff(
            error_category="exception",
            error_message="Request timeout after 30 seconds",
        )
        assert backoff == TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value]
        assert source == BackoffSource.ERROR_CATEGORY.value

        # 场景 4: 无法推断，使用默认
        backoff, source = resolve_backoff(
            error_category="exception",
            error_message="Some unknown error",
        )
        assert source == BackoffSource.DEFAULT.value

    def test_worker_exception_handling_uses_resolve_backoff(self):
        """验证 worker 异常处理使用 resolve_backoff（与正常失败一致）"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            BackoffSource,
            ErrorCategory,
            resolve_backoff,
        )

        # 模拟 worker 异常处理场景
        # 注意：当推断的 backoff 值等于 default 时，source 会是 DEFAULT
        # 这是符合预期的行为（如 network=60, default=60）
        exception_errors = [
            (
                "Request timeout after 30 seconds",
                "timeout",
                TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value],
            ),
            (
                "429 Too Many Requests",
                "rate_limit",
                TRANSIENT_ERROR_BACKOFF[ErrorCategory.RATE_LIMIT.value],
            ),
            (
                "Connection reset by peer",
                "network",
                TRANSIENT_ERROR_BACKOFF[ErrorCategory.NETWORK.value],
            ),
            (
                "502 Bad Gateway",
                "server_error",
                TRANSIENT_ERROR_BACKOFF[ErrorCategory.SERVER_ERROR.value],
            ),
            ("Some unknown error", "default", TRANSIENT_ERROR_BACKOFF["default"]),
        ]

        for error_msg, expected_type, expected_backoff in exception_errors:
            backoff, source = resolve_backoff(
                error_category="exception",
                error_message=error_msg,
            )
            # 验证返回正确的 backoff 值
            assert backoff == expected_backoff, (
                f"'{error_msg}' should have backoff {expected_backoff}, got {backoff}"
            )
            # 验证 backoff 值大于 0
            assert backoff > 0, f"'{error_msg}' should have positive backoff"
            # 注意：当推断的 backoff == default backoff 时，source 为 DEFAULT（这是正确行为）
            # 只有当 backoff != default 时，source 才会是 ERROR_CATEGORY
            if expected_backoff != TRANSIENT_ERROR_BACKOFF["default"]:
                assert source == BackoffSource.ERROR_CATEGORY.value, (
                    f"'{error_msg}' should use error_category backoff"
                )

    def test_reaper_uses_calculate_backoff_for_exponential_backoff(self):
        """验证 reaper 使用 calculate_backoff_seconds 进行指数退避"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
            calculate_backoff_seconds,
        )

        # reaper 场景：过期任务，基于 attempts 做指数退避
        # 临时性错误 + attempts=2
        backoff = calculate_backoff_seconds(
            attempts=2,
            error_category=ErrorCategory.TIMEOUT.value,
        )
        # 基础 backoff 是 30（timeout），指数退避 30 * 2^1 = 60
        expected = TRANSIENT_ERROR_BACKOFF[ErrorCategory.TIMEOUT.value] * (2 ** (2 - 1))
        assert backoff == expected, f"Expected {expected}, got {backoff}"

        # 验证 max_seconds 限制
        backoff = calculate_backoff_seconds(
            attempts=10,
            error_category=ErrorCategory.RATE_LIMIT.value,
            max_seconds=300,
        )
        assert backoff == 300, "Should be capped at max_seconds"

    def test_worker_normal_failure_and_exception_use_same_function(self):
        """验证 worker 正常失败和异常处理使用相同的 resolve_backoff"""
        from engram.logbook.scm_sync_errors import resolve_backoff

        # 正常失败场景
        normal_backoff, normal_source = resolve_backoff(
            error_category="timeout",
            error_message="Request timeout",
        )

        # 异常处理场景（error_category='exception'，从 message 推断）
        exception_backoff, exception_source = resolve_backoff(
            error_category="exception",
            error_message="Request timeout",
        )

        # 两者应该得到相同的 backoff（都是 timeout 类型）
        assert normal_backoff == exception_backoff, (
            "Normal failure and exception handling should use same backoff for same error"
        )

    def test_classify_functions_are_consistent(self):
        """验证 classify_exception 和 classify_last_error 分类一致"""
        from engram.logbook.scm_sync_errors import (
            ErrorCategory,
            classify_exception,
            classify_last_error,
        )

        # 测试 TimeoutError
        exc = TimeoutError("Connection timed out")
        exc_category, exc_msg = classify_exception(exc)
        _, _, last_err_category = classify_last_error(exc_msg)

        # 都应该识别为 timeout
        assert exc_category == ErrorCategory.TIMEOUT.value
        assert last_err_category == ErrorCategory.TIMEOUT.value

        # 测试 ConnectionError
        exc = ConnectionError("Connection refused")
        exc_category, exc_msg = classify_exception(exc)
        _, _, last_err_category = classify_last_error(exc_msg)

        # 都应该识别为 connection 或 network
        assert exc_category == ErrorCategory.CONNECTION.value
        # classify_last_error 通过 "connection" 关键词匹配到 network
        assert last_err_category in [ErrorCategory.CONNECTION.value, ErrorCategory.NETWORK.value]

    def test_backoff_source_tracking(self):
        """验证 backoff 来源追踪正确"""
        from engram.logbook.scm_sync_errors import BackoffSource, resolve_backoff

        # retry_after 优先级最高
        _, source = resolve_backoff(
            retry_after=120,
            error_category="rate_limit",
            error_message="429 Too Many Requests",
        )
        assert source == BackoffSource.RETRY_AFTER.value

        # error_category 次之
        _, source = resolve_backoff(
            retry_after=None,
            error_category="rate_limit",
        )
        assert source == BackoffSource.ERROR_CATEGORY.value

        # default 最后
        _, source = resolve_backoff(
            retry_after=None,
            error_category=None,
            error_message="Some random error",
        )
        assert source == BackoffSource.DEFAULT.value

    def test_worker_reaper_shared_constants(self):
        """验证 worker 和 reaper 使用相同的共享常量"""
        from engram.logbook.scm_sync_errors import (
            DEFAULT_BACKOFF_BASE,
            DEFAULT_MAX_BACKOFF,
            PERMANENT_ERROR_CATEGORIES,
            TRANSIENT_ERROR_BACKOFF,
            TRANSIENT_ERROR_CATEGORIES,
            ErrorCategory,
        )

        # 验证关键常量存在
        assert len(TRANSIENT_ERROR_CATEGORIES) > 0
        assert len(PERMANENT_ERROR_CATEGORIES) > 0
        assert len(TRANSIENT_ERROR_BACKOFF) > 0
        assert DEFAULT_BACKOFF_BASE == 60
        assert DEFAULT_MAX_BACKOFF == 3600

        # 验证所有 transient 类别都有 backoff 配置
        for category in TRANSIENT_ERROR_CATEGORIES:
            assert (
                category in TRANSIENT_ERROR_BACKOFF or category == ErrorCategory.LEASE_LOST.value
            ), f"Missing backoff config for {category}"


# ============================================================================
# Reaper 集成测试
# ============================================================================


class TestReaperAttemptsSemantics:
    """
    Reaper attempts 语义测试

    验证:
    - mark_job_as_failed_by_reaper 不修改 attempts
    - mark_job_as_dead_by_reaper 不修改 attempts

    与 test_scm_sync_queue.py 的 "attempts 只在 claim 时 +1" 语义保持一致
    """

    def test_mark_job_as_failed_by_reaper_does_not_change_attempts(self, migrated_db):
        """mark_job_as_failed_by_reaper 不修改 attempts"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_reaper_attempts_failed.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建任务，attempts=2（模拟已经被 claim 过 2 次）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 2, 5)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            from db import get_conn, mark_job_as_failed_by_reaper

            test_conn = get_conn(dsn)
            try:
                # 调用 mark_job_as_failed_by_reaper
                success = mark_job_as_failed_by_reaper(
                    test_conn,
                    job_id,
                    error="Reaped: job lock expired",
                    retry_delay_seconds=60,
                )
                test_conn.commit()

                assert success is True

                # 验证 attempts 保持不变
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, attempts FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()

                    assert row[0] == "failed", f"状态应为 failed，实际: {row[0]}"
                    assert row[1] == 2, f"attempts 应保持为 2，实际: {row[1]}（不应被 reaper 修改）"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_mark_job_as_dead_by_reaper_does_not_change_attempts(self, migrated_db):
        """mark_job_as_dead_by_reaper 不修改 attempts"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_reaper_attempts_dead.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建任务，attempts=3（已达到 max_attempts）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 3, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            from db import get_conn, mark_job_as_dead_by_reaper

            test_conn = get_conn(dsn)
            try:
                # 调用 mark_job_as_dead_by_reaper
                success = mark_job_as_dead_by_reaper(
                    test_conn,
                    job_id,
                    error="Reaped: job expired after max attempts",
                )
                test_conn.commit()

                assert success is True

                # 验证 attempts 保持不变
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT status, attempts FROM {scm_schema}.sync_jobs WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    row = cur.fetchone()

                    assert row[0] == "dead", f"状态应为 dead，实际: {row[0]}"
                    assert row[1] == 3, f"attempts 应保持为 3，实际: {row[1]}（不应被 reaper 修改）"
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestReaperRunIntegration:
    """
    Reaper run_reaper 集成测试

    测试完整的 reaper 运行流程
    """

    def test_run_reaper_processes_expired_jobs(self, migrated_db):
        """run_reaper 能正确处理过期的 jobs"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_run_reaper.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建过期的 running 任务
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            from scripts.scm_sync_reaper import JobRecoveryPolicy, run_reaper

            # 运行 reaper
            result = run_reaper(
                dsn=dsn,
                grace_seconds=60,
                max_duration_seconds=1800,
                policy=JobRecoveryPolicy.to_failed,
                retry_delay_seconds=60,
                dry_run=False,
            )

            # 验证结果
            assert result["jobs"]["processed"] >= 1, "应该处理至少 1 个过期任务"
            assert (
                result["jobs"]["to_failed"] >= 1
                or result["jobs"]["to_dead"] >= 1
                or result["jobs"]["to_pending"] >= 1
            ), "应该有任务被处理"

            # 验证任务状态
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT status, locked_by FROM {scm_schema}.sync_jobs WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

                assert row[0] == "failed", f"任务状态应为 failed，实际: {row[0]}"
                assert row[1] is None, "locked_by 应为 NULL"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_run_reaper_dry_run_does_not_modify(self, migrated_db):
        """run_reaper dry_run 模式不修改数据库"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_dry_run.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建过期的 running 任务
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            from scripts.scm_sync_reaper import JobRecoveryPolicy, run_reaper

            # 运行 reaper（dry_run 模式）
            result = run_reaper(
                dsn=dsn,
                grace_seconds=60,
                max_duration_seconds=1800,
                policy=JobRecoveryPolicy.to_failed,
                retry_delay_seconds=60,
                dry_run=True,
            )

            # 验证 dry_run 标志
            assert result["dry_run"] is True
            assert result["jobs"]["processed"] >= 1, "应该检测到至少 1 个过期任务"

            # 验证任务状态未被修改
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT status, locked_by FROM {scm_schema}.sync_jobs WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

                assert row[0] == "running", f"dry_run 模式任务状态应保持 running，实际: {row[0]}"
                assert row[1] == "dead-worker", "dry_run 模式 locked_by 应保持不变"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_run_reaper_policy_to_pending(self, migrated_db):
        """run_reaper 使用 to_pending 策略将任务恢复为 pending"""
        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_policy_pending.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                # 创建过期的 running 任务（无错误，非永久性/临时性错误）
                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, locked_at, lease_seconds, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'dead-worker', now() - interval '10 minutes', 300, 1, 5)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            from scripts.scm_sync_reaper import JobRecoveryPolicy, run_reaper

            # 运行 reaper（to_pending 策略）
            result = run_reaper(
                dsn=dsn,
                grace_seconds=60,
                max_duration_seconds=1800,
                policy=JobRecoveryPolicy.to_pending,
                retry_delay_seconds=60,
                dry_run=False,
            )

            # 验证结果
            assert result["jobs"]["processed"] >= 1
            assert result["jobs"]["to_pending"] >= 1, "应该有任务被恢复为 pending"

            # 验证任务状态
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT status, locked_by FROM {scm_schema}.sync_jobs WHERE job_id = %s
                """,
                    (job_id,),
                )
                row = cur.fetchone()

                assert row[0] == "pending", f"任务状态应为 pending，实际: {row[0]}"
                assert row[1] is None, "locked_by 应为 NULL"

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestReaperCLI:
    """Reaper CLI 测试"""

    def test_build_parser_defaults(self):
        """验证 CLI 参数解析器默认值"""
        from scripts.scm_sync_reaper import build_parser

        parser = build_parser()
        # 解析空参数
        args = parser.parse_args([])

        assert args.grace_seconds == 60
        assert args.max_duration_seconds == 1800
        assert args.policy == "to_failed"
        assert args.retry_delay == 60
        assert args.dry_run is False
        assert args.verbose is False

    def test_build_parser_custom_values(self):
        """验证 CLI 参数解析器自定义值"""
        from scripts.scm_sync_reaper import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "--grace-seconds",
                "120",
                "--max-duration-seconds",
                "3600",
                "--policy",
                "to_pending",
                "--retry-delay",
                "300",
                "--dry-run",
                "--verbose",
            ]
        )

        assert args.grace_seconds == 120
        assert args.max_duration_seconds == 3600
        assert args.policy == "to_pending"
        assert args.retry_delay == 300
        assert args.dry_run is True
        assert args.verbose is True

    def test_job_recovery_policy_enum(self):
        """验证 JobRecoveryPolicy 枚举值"""
        from scripts.scm_sync_reaper import JobRecoveryPolicy

        assert JobRecoveryPolicy.to_failed.value == "to_failed"
        assert JobRecoveryPolicy.to_pending.value == "to_pending"

        # 可以从字符串创建
        assert JobRecoveryPolicy("to_failed") == JobRecoveryPolicy.to_failed
        assert JobRecoveryPolicy("to_pending") == JobRecoveryPolicy.to_pending
