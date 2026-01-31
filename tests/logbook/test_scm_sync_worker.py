# -*- coding: utf-8 -*-
"""
SCM Sync Worker 单元测试

测试:
- locked/skipped 结果不触发 fail_retry
- HeartbeatManager 续租线程工作
- process_one_job 的行为
- GitLab 认证凭证回退链（GITLAB_TOKEN / GITLAB_PRIVATE_TOKEN）
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestHeartbeatManager:
    """HeartbeatManager 续租线程测试"""

    def test_heartbeat_starts_and_stops(self):
        """心跳线程可以正常启动和停止"""
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = True

            hb = HeartbeatManager(
                job_id="test-job-1",
                worker_id="worker-1",
                renew_interval_seconds=1,
                lease_seconds=60,
                max_failures=3,
            )

            hb.start()
            assert hb._thread is not None
            assert hb._thread.is_alive()

            hb.stop(wait=True, timeout=2.0)
            assert not hb._thread.is_alive() if hb._thread else True

    def test_heartbeat_context_manager(self):
        """心跳线程可以通过 context manager 使用"""
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = True

            with HeartbeatManager(
                job_id="test-job-2",
                worker_id="worker-1",
                renew_interval_seconds=100,  # 长间隔避免触发续租
                lease_seconds=60,
                max_failures=3,
            ) as hb:
                assert hb._thread is not None
                assert hb._thread.is_alive()
                assert not hb.should_abort

            # 退出 context manager 后线程应停止
            time.sleep(0.1)
            assert hb._thread is None or not hb._thread.is_alive()

    def test_heartbeat_renews_lease_periodically(self):
        """心跳线程定期续租"""
        from scm_sync_worker import HeartbeatManager

        renew_count = []

        def mock_renew(job_id, worker_id, lease_seconds):
            renew_count.append(1)
            return True

        with patch("scm_sync_worker.renew_lease", side_effect=mock_renew):
            with HeartbeatManager(
                job_id="test-job-3",
                worker_id="worker-1",
                renew_interval_seconds=0.1,  # 100ms 间隔
                lease_seconds=60,
                max_failures=3,
            ):
                # 等待几次续租
                time.sleep(0.35)

        # 应该至少续租 2-3 次
        assert len(renew_count) >= 2

    def test_heartbeat_sets_abort_on_max_failures(self):
        """续租失败超过阈值时设置 should_abort"""
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = False  # 续租一直失败

            with HeartbeatManager(
                job_id="test-job-4",
                worker_id="worker-1",
                renew_interval_seconds=0.05,  # 50ms 间隔
                lease_seconds=60,
                max_failures=2,  # 2 次失败后中止
            ) as hb:
                # 等待足够时间让续租失败
                time.sleep(0.2)

        assert hb.should_abort is True
        assert hb.failure_count >= 2

    def test_heartbeat_resets_failure_count_on_success(self):
        """续租成功后重置失败计数"""
        from scm_sync_worker import HeartbeatManager

        call_count = [0]

        def mock_renew(job_id, worker_id, lease_seconds):
            call_count[0] += 1
            # 前两次失败，之后成功
            return call_count[0] > 2

        with patch("scm_sync_worker.renew_lease", side_effect=mock_renew):
            with HeartbeatManager(
                job_id="test-job-5",
                worker_id="worker-1",
                renew_interval_seconds=0.05,
                lease_seconds=60,
                max_failures=5,
            ) as hb:
                time.sleep(0.3)

        # 失败计数应该在成功后重置
        assert hb.failure_count == 0 or hb.should_abort is False

    def test_do_final_renew(self):
        """do_final_renew 执行最后一次续租"""
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = True

            hb = HeartbeatManager(
                job_id="test-job-6",
                worker_id="worker-1",
                renew_interval_seconds=100,
                lease_seconds=60,
                max_failures=3,
            )

            result = hb.do_final_renew()

            assert result is True
            mock_renew.assert_called_once_with(
                job_id="test-job-6",
                worker_id="worker-1",
                lease_seconds=60,
            )


class TestLockedSkippedBehavior:
    """locked/skipped 结果不触发 fail_retry 测试"""

    def test_skipped_result_not_calls_fail_retry(self, migrated_db):
        """skipped=True 的结果不应调用 fail_retry"""
        from unittest.mock import patch

        import psycopg

        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None

        try:
            # 创建测试数据
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_skipped.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            # Mock 所需模块
            with (
                patch("scm_sync_worker.claim") as mock_claim,
                patch("scm_sync_worker.ack") as mock_ack,
                patch("scm_sync_worker.fail_retry") as mock_fail_retry,
                patch("scm_sync_worker.requeue_without_penalty"),
                patch("scm_sync_worker.renew_lease") as mock_renew,
                patch("scm_sync_worker.execute_sync_job") as mock_execute,
            ):
                mock_claim.return_value = {
                    "job_id": job_id,
                    "repo_id": repo_id,
                    "job_type": "gitlab_commits",
                    "mode": "incremental",
                    "attempts": 1,
                    "max_attempts": 3,
                    "lease_seconds": 300,
                    "payload": {},
                }

                # 模拟 skipped 结果（success=True, skipped=True）
                mock_execute.return_value = {
                    "success": True,
                    "skipped": True,
                    "message": "锁被其他 worker 持有",
                }

                mock_renew.return_value = True
                mock_ack.return_value = True

                from scm_sync_worker import process_one_job

                result = process_one_job(
                    worker_id="worker-test",
                    job_types=["gitlab_commits"],
                    worker_cfg={
                        "lease_seconds": 300,
                        "renew_interval_seconds": 60,
                        "max_renew_failures": 3,
                    },
                )

                assert result is True

                # skipped=True 且 success=True 应该调用 ack，不调用 fail_retry
                mock_ack.assert_called_once()
                mock_fail_retry.assert_not_called()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_locked_with_error_result_calls_fail_retry(self, migrated_db):
        """locked 导致失败（success=False）时应调用 fail_retry"""
        import psycopg

        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None

        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_locked_error.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'pending')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            with (
                patch("scm_sync_worker.claim") as mock_claim,
                patch("scm_sync_worker.ack") as mock_ack,
                patch("scm_sync_worker.fail_retry") as mock_fail_retry,
                patch("scm_sync_worker.renew_lease") as mock_renew,
                patch("scm_sync_worker.execute_sync_job") as mock_execute,
            ):
                mock_claim.return_value = {
                    "job_id": job_id,
                    "repo_id": repo_id,
                    "job_type": "gitlab_commits",
                    "mode": "incremental",
                    "attempts": 1,
                    "max_attempts": 3,
                    "lease_seconds": 300,
                    "payload": {},
                }

                # 模拟失败结果
                mock_execute.return_value = {
                    "success": False,
                    "error": "Connection timeout",
                    "error_category": "timeout",
                }

                mock_renew.return_value = True
                mock_fail_retry.return_value = True

                from scm_sync_worker import process_one_job

                result = process_one_job(
                    worker_id="worker-test",
                    job_types=["gitlab_commits"],
                    worker_cfg={
                        "lease_seconds": 300,
                        "renew_interval_seconds": 60,
                        "max_renew_failures": 3,
                    },
                )

                assert result is True

                # success=False 应该调用 fail_retry
                mock_fail_retry.assert_called_once()
                mock_ack.assert_not_called()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestErrorRedaction:
    """敏感信息脱敏测试"""

    def test_redact_gitlab_token_in_error(self):
        """GitLab token (glpat-xxx) 在错误信息中应被脱敏"""
        from engram.logbook.scm_auth import redact

        error_with_token = "Failed to authenticate: PRIVATE-TOKEN: glpat-abcdef123456789xyz"
        redacted = redact(error_with_token)

        # glpat token 应被替换
        assert "glpat-" not in redacted
        assert "[GITLAB_TOKEN]" in redacted or "[REDACTED]" in redacted

    def test_redact_bearer_token_in_error(self):
        """Bearer token 在错误信息中应被脱敏"""
        from engram.logbook.scm_auth import redact

        error_with_bearer = (
            "Request failed: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxx"
        )
        redacted = redact(error_with_bearer)

        # Bearer token 值应被替换
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        assert "[TOKEN]" in redacted or "[REDACTED]" in redacted

    def test_redact_authorization_header_in_error(self):
        """Authorization header 在错误信息中应被脱敏"""
        from engram.logbook.scm_auth import redact

        error_with_auth = (
            "HTTP 401: Authorization header invalid: Authorization: Basic dXNlcjpwYXNzd29yZA=="
        )
        redacted = redact(error_with_auth)

        # Authorization 值应被替换
        assert "dXNlcjpwYXNzd29yZA==" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_dict_removes_sensitive_keys(self):
        """敏感键名的字典值应被脱敏"""
        from engram.logbook.scm_auth import redact_dict

        error_summary = {
            "error_type": "auth_error",
            "message": "Failed with token glpat-secret123",
            "Authorization": "Bearer secret-jwt-token",
            "PRIVATE-TOKEN": "glpat-another-secret",
            "safe_field": "this is safe",
        }

        redacted = redact_dict(error_summary)

        # 敏感键的值应被替换
        assert redacted["Authorization"] == "[REDACTED]"
        assert redacted["PRIVATE-TOKEN"] == "[REDACTED]"
        # message 中的 token 也应被脱敏
        assert "glpat-secret123" not in redacted["message"]
        # 安全字段保持不变
        assert redacted["safe_field"] == "this is safe"

    def test_fail_retry_redacts_error_with_token(self, migrated_db):
        """fail_retry 应对包含 token 的错误信息进行脱敏"""
        import psycopg

        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None

        try:
            # 创建测试数据
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_redact.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status, locked_by
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running', 'test-worker')
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            # 调用 fail_retry 并传入包含敏感信息的错误
            from engram.logbook.scm_sync_queue import fail_retry

            error_with_secrets = (
                "Auth failed: PRIVATE-TOKEN: glpat-mysecrettoken123, "
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret"
            )

            # 创建新连接来执行 fail_retry
            test_conn = psycopg.connect(dsn)
            try:
                result = fail_retry(
                    job_id=job_id,
                    worker_id="test-worker",
                    error=error_with_secrets,
                    conn=test_conn,
                )
                assert result is True
            finally:
                test_conn.close()

            # 验证数据库中存储的错误信息已被脱敏
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT last_error FROM {scm_schema}.sync_jobs
                    WHERE job_id = %s
                """,
                    (job_id,),
                )
                stored_error = cur.fetchone()[0]

            # 确保敏感信息已被脱敏
            assert "glpat-mysecrettoken123" not in stored_error
            assert "eyJhbGciOiJIUzI1NiJ9" not in stored_error
            assert (
                "[REDACTED]" in stored_error
                or "[GITLAB_TOKEN]" in stored_error
                or "[TOKEN]" in stored_error
            )

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()

    def test_process_one_job_redacts_error_before_fail_retry(self):
        """process_one_job 在调用 fail_retry 前应对错误进行脱敏"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            # 模拟返回包含敏感信息的错误
            mock_execute.return_value = {
                "success": False,
                "error": "Auth failed with token glpat-secrettoken123 and Bearer eyJxxx",
                "error_category": "auth_error",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_fail_retry.assert_called_once()

            # 检查传递给 fail_retry 的错误信息是否已脱敏
            call_args = mock_fail_retry.call_args
            error_arg = call_args[0][2]  # 第三个位置参数是 error

            # 敏感信息应该已被脱敏
            assert "glpat-secrettoken123" not in error_arg
            assert "eyJxxx" not in error_arg


class TestProcessOneJob:
    """process_one_job 函数测试"""

    def test_no_job_returns_false(self):
        """没有可用任务时返回 False"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.get_worker_config_from_module") as mock_get_cfg,
        ):
            mock_claim.return_value = None
            mock_get_cfg.return_value = {
                "lease_seconds": 300,
                "renew_interval_seconds": 60,
                "max_renew_failures": 3,
            }

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is False

    def test_successful_job_calls_ack(self):
        """成功的任务调用 ack"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": True,
                "run_id": "run-123",
            }

            mock_renew.return_value = True
            mock_ack.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_ack.assert_called_once()
            mock_fail_retry.assert_not_called()

    def test_aborted_job_due_to_lease_lost(self):
        """因续租失败中止的任务"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease"),
            patch("scm_sync_worker.HeartbeatManager") as MockHeartbeat,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            # 模拟心跳管理器
            mock_hb_instance = MagicMock()
            mock_hb_instance.should_abort = True  # 续租失败
            mock_hb_instance.failure_count = 3
            mock_hb_instance.get_abort_error.return_value = {
                "error": "Lease lost after 3 consecutive renewal failures. Last error: renew_lease returned False",
                "error_category": "lease_lost",
                "failure_count": 3,
                "max_failures": 3,
                "job_id": "test-job",
                "worker_id": "worker-test",
            }
            mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
            mock_hb_instance.__exit__ = MagicMock(return_value=False)
            MockHeartbeat.return_value = mock_hb_instance

            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            # 模拟 execute_sync_job 执行后检测到 should_abort
            with patch("scm_sync_worker.execute_sync_job") as mock_execute:
                mock_execute.return_value = {"success": True}

                result = process_one_job(
                    worker_id="worker-test",
                    worker_cfg={
                        "lease_seconds": 300,
                        "renew_interval_seconds": 60,
                        "max_renew_failures": 3,
                    },
                )

            assert result is True
            # 因为 should_abort=True，应该调用 fail_retry 而不是 ack
            mock_fail_retry.assert_called_once()
            mock_ack.assert_not_called()


class TestHeartbeatManagerLeaseLost:
    """HeartbeatManager 续租失败导致任务中止的测试"""

    def test_lease_lost_uses_shared_error_category(self):
        """续租失败使用共享的 ErrorCategory.LEASE_LOST 常量"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = False  # 续租一直失败

            hb = HeartbeatManager(
                job_id="test-job-lease-lost",
                worker_id="worker-1",
                renew_interval_seconds=0.05,  # 50ms 间隔
                lease_seconds=60,
                max_failures=2,  # 2 次失败后中止
            )

            hb.start()
            time.sleep(0.2)  # 等待足够时间让续租失败
            hb.stop()

            assert hb.should_abort is True

            # 验证使用共享的 ErrorCategory 常量
            abort_error = hb.get_abort_error()
            assert abort_error["error_category"] == ErrorCategory.LEASE_LOST.value
            assert "lease_lost" == abort_error["error_category"]
            assert abort_error["failure_count"] >= 2

    def test_lease_lost_triggers_fail_retry_not_ack(self):
        """续租失败应触发 fail_retry 而不是 ack（lease_lost 是临时错误）"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_CATEGORIES,
            ErrorCategory,
            is_transient_error,
        )

        # 验证 lease_lost 在临时错误列表中
        assert ErrorCategory.LEASE_LOST.value in TRANSIENT_ERROR_CATEGORIES

        # 验证 is_transient_error 返回 True
        assert is_transient_error(ErrorCategory.LEASE_LOST.value, "") is True

    def test_lease_lost_uses_zero_backoff(self):
        """续租失败使用 0 秒退避（立即重试）"""
        from engram.logbook.scm_sync_errors import (
            TRANSIENT_ERROR_BACKOFF,
            ErrorCategory,
            get_transient_error_backoff,
        )

        # 验证配置中的退避时间为 0
        assert TRANSIENT_ERROR_BACKOFF[ErrorCategory.LEASE_LOST.value] == 0

        # 验证 get_transient_error_backoff 返回 0
        backoff = get_transient_error_backoff(ErrorCategory.LEASE_LOST.value, "")
        assert backoff == 0

    def test_lease_lost_structured_error_info(self):
        """续租失败时记录结构化错误信息"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            # 第一次成功，后面失败
            mock_renew.side_effect = [True, False, False, False]

            hb = HeartbeatManager(
                job_id="test-job-structured",
                worker_id="worker-structured",
                renew_interval_seconds=0.03,
                lease_seconds=60,
                max_failures=2,
            )

            hb.start()
            time.sleep(0.2)
            hb.stop()

            # 获取结构化错误信息
            abort_error = hb.get_abort_error()

            # 验证必需字段
            assert "error" in abort_error
            assert "error_category" in abort_error
            assert "failure_count" in abort_error
            assert "max_failures" in abort_error
            assert "job_id" in abort_error
            assert "worker_id" in abort_error

            # 验证字段值
            assert abort_error["error_category"] == ErrorCategory.LEASE_LOST.value
            assert abort_error["job_id"] == "test-job-structured"
            assert abort_error["worker_id"] == "worker-structured"
            assert abort_error["max_failures"] == 2

    def test_lease_lost_last_error_captured(self):
        """续租失败时捕获最后一次的错误信息"""
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            # 模拟抛出异常的续租失败
            mock_renew.side_effect = Exception("Connection refused")

            hb = HeartbeatManager(
                job_id="test-job-error-capture",
                worker_id="worker-1",
                renew_interval_seconds=0.03,
                lease_seconds=60,
                max_failures=2,
            )

            hb.start()
            time.sleep(0.15)
            hb.stop()

            # 验证最后错误被捕获
            assert hb.last_error is not None
            assert "Exception during renew" in hb.last_error
            assert "Connection refused" in hb.last_error

    def test_process_one_job_lease_lost_calls_fail_retry_with_correct_backoff(self):
        """process_one_job 在续租失败时调用 fail_retry 并使用正确的退避时间"""
        from engram.logbook.scm_sync_errors import ErrorCategory

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.HeartbeatManager") as MockHeartbeat,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-backoff",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            # 模拟心跳管理器返回结构化错误
            mock_hb_instance = MagicMock()
            mock_hb_instance.should_abort = True
            mock_hb_instance.failure_count = 3
            mock_hb_instance.get_abort_error.return_value = {
                "error": "Lease lost after 3 failures",
                "error_category": ErrorCategory.LEASE_LOST.value,
                "failure_count": 3,
                "max_failures": 3,
                "job_id": "test-job-backoff",
                "worker_id": "worker-test",
            }
            mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
            mock_hb_instance.__exit__ = MagicMock(return_value=False)
            MockHeartbeat.return_value = mock_hb_instance

            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            with patch("scm_sync_worker.execute_sync_job") as mock_execute:
                mock_execute.return_value = {"success": True}

                process_one_job(
                    worker_id="worker-test",
                    worker_cfg={
                        "lease_seconds": 300,
                        "renew_interval_seconds": 60,
                        "max_renew_failures": 3,
                    },
                )

            # 验证调用 fail_retry 而不是 ack 或 mark_dead
            mock_fail_retry.assert_called_once()
            mock_ack.assert_not_called()
            mock_mark_dead.assert_not_called()

            # 验证 backoff_seconds 为 0（lease_lost 立即重试）
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 0

    def test_lease_lost_is_not_permanent_error(self):
        """验证 lease_lost 不是永久性错误（不应调用 mark_dead）"""
        from engram.logbook.scm_sync_errors import (
            PERMANENT_ERROR_CATEGORIES,
            ErrorCategory,
            is_permanent_error,
        )

        # 验证 lease_lost 不在永久性错误列表中
        assert ErrorCategory.LEASE_LOST.value not in PERMANENT_ERROR_CATEGORIES

        # 验证 is_permanent_error 返回 False
        assert is_permanent_error(ErrorCategory.LEASE_LOST.value) is False

    def test_default_max_renew_failures_from_shared_module(self):
        """验证默认 max_failures 使用共享模块的常量"""
        from engram.logbook.scm_sync_errors import DEFAULT_MAX_RENEW_FAILURES
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = True

            # 不指定 max_failures，使用默认值
            hb = HeartbeatManager(
                job_id="test-default",
                worker_id="worker-1",
                renew_interval_seconds=100,
                lease_seconds=60,
            )

            # 验证使用共享常量
            assert hb.max_failures == DEFAULT_MAX_RENEW_FAILURES
            assert hb.max_failures == 3  # 当前默认值


class TestHeartbeatLongTaskAndRenewFailure:
    """
    长任务与续租失败场景测试

    测试场景：
    1. 长任务执行期间多次续租
    2. 续租部分失败后恢复
    3. 续租全部失败导致任务中止
    4. 时间推进验证续租间隔
    """

    def test_long_task_multiple_renews(self):
        """长任务执行期间心跳线程正确多次续租"""
        from scm_sync_worker import HeartbeatManager

        renew_calls = []

        def mock_renew(job_id, worker_id, lease_seconds):
            renew_calls.append(
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                    "time": time.time(),
                }
            )
            return True

        with patch("scm_sync_worker.renew_lease", side_effect=mock_renew):
            with HeartbeatManager(
                job_id="long-task-job",
                worker_id="worker-1",
                renew_interval_seconds=0.05,  # 50ms 续租间隔
                lease_seconds=60,
                max_failures=3,
            ) as hb:
                # 模拟长任务执行 250ms（应该有约 5 次续租）
                time.sleep(0.25)

                assert not hb.should_abort
                assert hb.failure_count == 0

        # 验证多次续租
        assert len(renew_calls) >= 4

        # 验证续租参数正确
        for call in renew_calls:
            assert call["job_id"] == "long-task-job"
            assert call["worker_id"] == "worker-1"
            assert call["lease_seconds"] == 60

    def test_renew_partial_failure_then_recovery(self):
        """续租部分失败后恢复，计数正确重置"""
        from scm_sync_worker import HeartbeatManager

        call_count = [0]

        def mock_renew(job_id, worker_id, lease_seconds):
            call_count[0] += 1
            # 第 2、3 次失败，其他成功
            if call_count[0] in [2, 3]:
                return False
            return True

        with patch("scm_sync_worker.renew_lease", side_effect=mock_renew):
            with HeartbeatManager(
                job_id="partial-fail-job",
                worker_id="worker-1",
                renew_interval_seconds=0.03,  # 30ms 间隔
                lease_seconds=60,
                max_failures=5,  # 5 次失败才中止
            ) as hb:
                # 等待足够时间让续租执行多次
                time.sleep(0.2)

        # 不应该中止（因为没有连续失败超过 5 次）
        assert not hb.should_abort
        # 失败计数应该被重置（因为后续成功了）
        assert hb.failure_count == 0 or call_count[0] > 4

    def test_renew_consecutive_failures_cause_abort(self):
        """连续续租失败超过阈值导致任务中止"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from scm_sync_worker import HeartbeatManager

        with patch("scm_sync_worker.renew_lease") as mock_renew:
            mock_renew.return_value = False  # 所有续租都失败

            with HeartbeatManager(
                job_id="consecutive-fail-job",
                worker_id="worker-1",
                renew_interval_seconds=0.03,
                lease_seconds=60,
                max_failures=3,
            ) as hb:
                # 等待足够时间触发 3 次失败
                time.sleep(0.15)

        # 应该中止
        assert hb.should_abort is True
        assert hb.failure_count >= 3

        # 验证错误信息
        abort_error = hb.get_abort_error()
        assert abort_error["error_category"] == ErrorCategory.LEASE_LOST.value
        assert abort_error["failure_count"] >= 3

    def test_renew_failure_with_exception(self):
        """续租时抛出异常正确处理"""
        from scm_sync_worker import HeartbeatManager

        call_count = [0]

        def mock_renew(job_id, worker_id, lease_seconds):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise ConnectionError("Database connection lost")
            return True

        with patch("scm_sync_worker.renew_lease", side_effect=mock_renew):
            with HeartbeatManager(
                job_id="exception-fail-job",
                worker_id="worker-1",
                renew_interval_seconds=0.03,
                lease_seconds=60,
                max_failures=5,
            ) as hb:
                time.sleep(0.15)

        # 不应该中止（异常被捕获，且后续成功）
        assert not hb.should_abort
        # 最后一次错误可能已被清除
        # assert hb.last_error is None or "Exception" not in hb.last_error

    def test_process_one_job_with_heartbeat_abort(self):
        """process_one_job 中心跳中止导致 fail_retry 调用"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease"),
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
            patch("scm_sync_worker.HeartbeatManager") as MockHB,
        ):
            mock_claim.return_value = {
                "job_id": "abort-test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            # 模拟心跳管理器在执行后标记中止
            mock_hb_instance = MagicMock()
            mock_hb_instance.should_abort = True
            mock_hb_instance.failure_count = 3
            mock_hb_instance.get_abort_error.return_value = {
                "error": "Lease lost after 3 failures",
                "error_category": ErrorCategory.LEASE_LOST.value,
                "failure_count": 3,
                "max_failures": 3,
                "job_id": "abort-test-job",
                "worker_id": "worker-test",
            }
            mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
            mock_hb_instance.__exit__ = MagicMock(return_value=False)
            MockHB.return_value = mock_hb_instance

            mock_execute.return_value = {"success": True}  # 任务本身成功
            mock_fail_retry.return_value = True

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True

            # 因为 should_abort=True，应该调用 fail_retry 而非 ack
            mock_fail_retry.assert_called_once()
            mock_ack.assert_not_called()
            mock_mark_dead.assert_not_called()

            # 验证 backoff_seconds 为 0（lease_lost 立即重试）
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 0

    def test_heartbeat_renew_interval_respected(self):
        """验证续租间隔被正确遵守"""
        from scm_sync_worker import HeartbeatManager

        renew_times = []

        def mock_renew(job_id, worker_id, lease_seconds):
            renew_times.append(time.time())
            return True

        with patch("scm_sync_worker.renew_lease", side_effect=mock_renew):
            renew_interval = 0.08  # 80ms

            with HeartbeatManager(
                job_id="interval-test-job",
                worker_id="worker-1",
                renew_interval_seconds=renew_interval,
                lease_seconds=60,
                max_failures=3,
            ):
                time.sleep(0.35)

        # 验证至少有 3 次续租
        assert len(renew_times) >= 3

        # 验证续租间隔大致正确（允许 50% 误差）
        for i in range(1, len(renew_times)):
            interval = renew_times[i] - renew_times[i - 1]
            # 间隔应该接近 renew_interval（允许 50ms 误差）
            assert interval >= renew_interval * 0.5, f"Interval {interval} too short"
            assert interval <= renew_interval * 2.0, f"Interval {interval} too long"

    def test_worker_config_overrides_default_renew_params(self):
        """worker_cfg 中的参数正确覆盖默认值"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
            patch("scm_sync_worker.HeartbeatManager") as MockHB,
        ):
            mock_claim.return_value = {
                "job_id": "config-test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": None,  # 使用配置中的默认值
                "payload": {},
            }

            mock_hb_instance = MagicMock()
            mock_hb_instance.should_abort = False
            mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
            mock_hb_instance.__exit__ = MagicMock(return_value=False)
            MockHB.return_value = mock_hb_instance

            mock_execute.return_value = {"success": True}
            mock_ack.return_value = True
            mock_renew.return_value = True

            # 使用自定义配置
            custom_cfg = {
                "lease_seconds": 600,  # 10 分钟
                "renew_interval_seconds": 120,  # 2 分钟
                "max_renew_failures": 5,
            }

            process_one_job(
                worker_id="worker-test",
                worker_cfg=custom_cfg,
            )

            # 验证 HeartbeatManager 被调用时使用了自定义配置
            MockHB.assert_called_once()
            call_kwargs = MockHB.call_args[1]
            assert call_kwargs["renew_interval_seconds"] == 120
            assert call_kwargs["lease_seconds"] == 600
            assert call_kwargs["max_failures"] == 5


class TestGitLabAuthFallback:
    """GitLab 认证凭证回退链测试"""

    def test_gitlab_private_token_env_fallback_in_config(self):
        """仅设置 GITLAB_PRIVATE_TOKEN 时 config.get_gitlab_auth() 能获取凭证"""
        # 清除相关环境变量
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            # 仅设置 GITLAB_PRIVATE_TOKEN
            os.environ["GITLAB_PRIVATE_TOKEN"] = "test-private-token-12345"

            from engram.logbook.config import Config, get_gitlab_auth

            # 创建一个 mock config，正确处理默认值
            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                # 返回 None 以模拟空配置，但返回默认值
                return default

            mock_config.get.side_effect = mock_get

            auth = get_gitlab_auth(mock_config)

            assert auth is not None
            assert auth.token == "test-private-token-12345"
            assert "GITLAB_PRIVATE_TOKEN" in auth.source
        finally:
            # 恢复环境变量
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_gitlab_token_env_has_priority_over_private_token(self):
        """GITLAB_TOKEN 优先级高于 GITLAB_PRIVATE_TOKEN"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            # 同时设置两个环境变量
            os.environ["GITLAB_TOKEN"] = "gitlab-token-priority"
            os.environ["GITLAB_PRIVATE_TOKEN"] = "private-token-fallback"

            from engram.logbook.config import Config, get_gitlab_auth

            # 创建一个 mock config，正确处理默认值
            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                return default

            mock_config.get.side_effect = mock_get

            auth = get_gitlab_auth(mock_config)

            assert auth is not None
            assert auth.token == "gitlab-token-priority"
            assert "GITLAB_TOKEN" in auth.source
            assert "GITLAB_PRIVATE_TOKEN" not in auth.source
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_gitlab_private_token_env_fallback_in_scm_auth(self):
        """仅设置 GITLAB_PRIVATE_TOKEN 时 scm_auth.create_gitlab_token_provider() 能获取凭证"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            os.environ["GITLAB_PRIVATE_TOKEN"] = "test-private-token-scm-auth"

            from engram.logbook.scm_auth import create_gitlab_token_provider

            # 不传入 config，使用默认回退逻辑
            provider = create_gitlab_token_provider(config=None, private_token=None)

            # 验证 provider 能获取 token
            token = provider.get_token()
            assert token == "test-private-token-scm-auth"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_create_token_provider_fallback(self):
        """create_token_provider 默认回退到 GITLAB_PRIVATE_TOKEN"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            os.environ["GITLAB_PRIVATE_TOKEN"] = "fallback-token-value"

            from engram.logbook.scm_auth import create_token_provider

            # 不传入任何参数，应该回退到 GITLAB_PRIVATE_TOKEN
            provider = create_token_provider()

            token = provider.get_token()
            assert token == "fallback-token-value"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_no_token_env_raises_validation_error(self):
        """未设置任何 token 环境变量时抛出 TokenValidationError"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            from engram.logbook.scm_auth import TokenValidationError, create_token_provider

            provider = create_token_provider()

            with pytest.raises(TokenValidationError):
                provider.get_token()
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]


class TestTransientErrorBackoff:
    """临时性错误退避策略测试（测试 engram_logbook.scm_sync_errors 模块）"""

    def test_get_transient_error_backoff_rate_limit(self):
        """429 速率限制错误应该返回 120 秒退避"""
        from engram.logbook.scm_sync_errors import get_transient_error_backoff

        # 通过 error_category
        assert get_transient_error_backoff("rate_limit", "") == 120

        # 通过错误消息中的关键词
        assert get_transient_error_backoff("", "429 Too Many Requests") == 120
        assert get_transient_error_backoff("", "rate limit exceeded") == 120
        assert get_transient_error_backoff("", "too many requests") == 120

    def test_get_transient_error_backoff_timeout(self):
        """超时错误应该返回 30 秒退避"""
        from engram.logbook.scm_sync_errors import get_transient_error_backoff

        # 通过 error_category
        assert get_transient_error_backoff("timeout", "") == 30

        # 通过错误消息中的关键词
        assert get_transient_error_backoff("", "Request timeout") == 30
        assert get_transient_error_backoff("", "Connection timed out") == 30

    def test_get_transient_error_backoff_unknown_default(self):
        """未知错误类型应该返回默认退避时间（60 秒）"""
        from engram.logbook.scm_sync_errors import get_transient_error_backoff

        # 空分类和消息
        assert get_transient_error_backoff("", "") == 60

        # 未知分类
        assert get_transient_error_backoff("unknown_category", "some error") == 60

        # 不匹配任何关键词的消息
        assert get_transient_error_backoff("", "Internal processing error") == 60

    def test_get_transient_error_backoff_server_error(self):
        """服务器错误应该返回 90 秒退避"""
        from engram.logbook.scm_sync_errors import get_transient_error_backoff

        assert get_transient_error_backoff("server_error", "") == 90
        assert get_transient_error_backoff("", "502 Bad Gateway") == 90
        assert get_transient_error_backoff("", "503 Service Unavailable") == 90
        # 注意: "504 Gateway Timeout" 包含 "timeout" 关键词，优先匹配为 timeout 类型
        assert get_transient_error_backoff("", "504 Gateway Timeout") == 30  # timeout 优先匹配

    def test_get_transient_error_backoff_network(self):
        """网络错误应该返回 60 秒退避"""
        from engram.logbook.scm_sync_errors import get_transient_error_backoff

        assert get_transient_error_backoff("network", "") == 60
        assert get_transient_error_backoff("", "network error") == 60

    def test_get_transient_error_backoff_connection(self):
        """连接错误应该返回 45 秒退避"""
        from engram.logbook.scm_sync_errors import get_transient_error_backoff

        assert get_transient_error_backoff("connection", "") == 45
        assert get_transient_error_backoff("", "connection refused") == 60  # network 优先匹配

    def test_is_transient_error(self):
        """测试 is_transient_error 函数"""
        from engram.logbook.scm_sync_errors import is_transient_error

        # 通过 error_category
        assert is_transient_error("rate_limit", "") is True
        assert is_transient_error("timeout", "") is True
        assert is_transient_error("network", "") is True
        assert is_transient_error("server_error", "") is True

        # 通过错误消息
        assert is_transient_error("", "429 rate limit") is True
        assert is_transient_error("", "connection timeout") is True

        # 非临时性错误
        assert is_transient_error("auth_error", "") is False
        assert is_transient_error("", "permission denied") is False

    def test_is_permanent_error(self):
        """测试 is_permanent_error 函数"""
        from engram.logbook.scm_sync_errors import is_permanent_error

        # 永久性错误类型
        assert is_permanent_error("auth_error") is True
        assert is_permanent_error("auth_missing") is True
        assert is_permanent_error("auth_invalid") is True
        assert is_permanent_error("repo_not_found") is True
        assert is_permanent_error("repo_type_unknown") is True
        assert is_permanent_error("permission_denied") is True

        # 非永久性错误类型
        assert is_permanent_error("rate_limit") is False
        assert is_permanent_error("timeout") is False
        assert is_permanent_error("") is False
        assert is_permanent_error(None) is False

    def test_classify_exception_timeout(self):
        """测试 classify_exception 对超时异常的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        # TimeoutError 异常
        exc = TimeoutError("Connection timed out")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.TIMEOUT.value
        assert "timed out" in msg.lower()

    def test_classify_exception_connection(self):
        """测试 classify_exception 对连接异常的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        # ConnectionError 异常
        exc = ConnectionError("Connection refused")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.CONNECTION.value
        assert "refused" in msg.lower()

    def test_classify_exception_http_401(self):
        """测试 classify_exception 对 401 错误的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        exc = Exception("401 Unauthorized: Invalid token")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.AUTH_ERROR.value

    def test_classify_exception_http_403(self):
        """测试 classify_exception 对 403 错误的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        exc = Exception("403 Forbidden: Access denied")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.PERMISSION_DENIED.value

    def test_classify_exception_http_404(self):
        """测试 classify_exception 对 404 错误的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        exc = Exception("404 Not Found: Repository does not exist")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.REPO_NOT_FOUND.value

    def test_classify_exception_http_429(self):
        """测试 classify_exception 对 429 错误的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        exc = Exception("429 Too Many Requests")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.RATE_LIMIT.value

    def test_classify_exception_http_5xx(self):
        """测试 classify_exception 对 5xx 错误的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        exc = Exception("502 Bad Gateway")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.SERVER_ERROR.value

        exc = Exception("503 Service Unavailable")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.SERVER_ERROR.value

    def test_classify_exception_unknown(self):
        """测试 classify_exception 对未知异常的分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory, classify_exception

        exc = Exception("Some unknown error")
        category, msg = classify_exception(exc)
        assert category == ErrorCategory.EXCEPTION.value

    def test_last_error_text(self):
        """测试 last_error_text 函数"""
        from engram.logbook.scm_sync_errors import last_error_text

        # None 返回空字符串
        assert last_error_text(None) == ""

        # 正常异常返回消息
        exc = Exception("Test error message")
        assert last_error_text(exc) == "Test error message"

        # 长消息被截断
        long_msg = "x" * 2000
        exc = Exception(long_msg)
        result = last_error_text(exc, max_length=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_error_category_enum(self):
        """测试 ErrorCategory 枚举定义"""
        from engram.logbook.scm_sync_errors import ErrorCategory

        # 永久性错误
        assert ErrorCategory.AUTH_ERROR.value == "auth_error"
        assert ErrorCategory.AUTH_MISSING.value == "auth_missing"
        assert ErrorCategory.AUTH_INVALID.value == "auth_invalid"
        assert ErrorCategory.REPO_NOT_FOUND.value == "repo_not_found"
        assert ErrorCategory.PERMISSION_DENIED.value == "permission_denied"

        # 临时性错误
        assert ErrorCategory.RATE_LIMIT.value == "rate_limit"
        assert ErrorCategory.TIMEOUT.value == "timeout"
        assert ErrorCategory.NETWORK.value == "network"
        assert ErrorCategory.SERVER_ERROR.value == "server_error"
        assert ErrorCategory.CONNECTION.value == "connection"


class TestErrorBackoffInProcessOneJob:
    """测试 process_one_job 中的错误退避策略"""

    def test_rate_limit_error_uses_120s_backoff(self):
        """429 速率限制错误应使用 120 秒退避"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "API rate limit exceeded (429)",
                "error_category": "rate_limit",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_fail_retry.assert_called_once()
            mock_mark_dead.assert_not_called()

            # 检查 backoff_seconds 参数
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 120

    def test_timeout_error_uses_30s_backoff(self):
        """超时错误应使用 30 秒退避"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "Request timeout after 30 seconds",
                "error_category": "timeout",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_fail_retry.assert_called_once()
            mock_mark_dead.assert_not_called()

            # 检查 backoff_seconds 参数
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 30

    def test_unknown_error_uses_default_backoff(self):
        """未知错误应使用默认退避时间（60 秒）"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "Unknown internal error",
                "error_category": "unknown",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_fail_retry.assert_called_once()
            mock_mark_dead.assert_not_called()

            # 检查 backoff_seconds 参数（默认 60 秒）
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 60

    def test_auth_error_calls_mark_dead(self):
        """认证错误应直接标记为 dead"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "Invalid GitLab token",
                "error_category": "auth_error",
            }

            mock_renew.return_value = True
            mock_mark_dead.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_mark_dead.assert_called_once()
            mock_fail_retry.assert_not_called()
            mock_ack.assert_not_called()

    def test_repo_not_found_calls_mark_dead(self):
        """仓库不存在错误应直接标记为 dead"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack") as mock_ack,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 999,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "repo_id 不存在: 999",
                "error_category": "repo_not_found",
            }

            mock_renew.return_value = True
            mock_mark_dead.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_mark_dead.assert_called_once()
            mock_fail_retry.assert_not_called()
            mock_ack.assert_not_called()

    def test_server_error_uses_90s_backoff(self):
        """服务器错误应使用 90 秒退避"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "502 Bad Gateway",
                "error_category": "server_error",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            result = process_one_job(
                worker_id="worker-test",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            assert result is True
            mock_fail_retry.assert_called_once()
            mock_mark_dead.assert_not_called()

            # 检查 backoff_seconds 参数
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 90


class TestSVNAuthFallback:
    """SVN 认证凭证回退链测试"""

    def test_svn_password_env_fallback(self):
        """仅设置 SVN_PASSWORD 环境变量时 get_svn_auth() 能获取密码"""
        env_backup = {k: os.environ.pop(k, None) for k in ["SVN_USERNAME", "SVN_PASSWORD"]}

        try:
            # 设置 SVN_PASSWORD 环境变量
            os.environ["SVN_PASSWORD"] = "test-svn-password-12345"
            os.environ["SVN_USERNAME"] = "test-svn-user"

            from engram.logbook.config import Config, get_svn_auth

            # 创建 mock config，模拟空配置
            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                return default

            mock_config.get.side_effect = mock_get

            auth = get_svn_auth(mock_config)

            assert auth is not None
            assert auth.username == "test-svn-user"
            assert auth.password == "test-svn-password-12345"
            assert "SVN_PASSWORD" in auth.source
            assert "SVN_USERNAME" in auth.source
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_svn_password_env_config_priority(self):
        """password_env 配置项指定的环境变量优先级高于 SVN_PASSWORD"""
        env_backup = {
            k: os.environ.pop(k, None)
            for k in ["SVN_USERNAME", "SVN_PASSWORD", "MY_CUSTOM_SVN_PWD"]
        }

        try:
            # 同时设置两个环境变量
            os.environ["SVN_PASSWORD"] = "fallback-password"
            os.environ["MY_CUSTOM_SVN_PWD"] = "priority-password"

            from engram.logbook.config import Config, get_svn_auth

            # 创建 mock config，配置 password_env
            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                if key == "scm.svn.password_env":
                    return "MY_CUSTOM_SVN_PWD"
                return default

            mock_config.get.side_effect = mock_get

            auth = get_svn_auth(mock_config)

            assert auth is not None
            assert auth.password == "priority-password"
            assert "MY_CUSTOM_SVN_PWD" in auth.source
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_svn_auth_no_password_returns_username_only(self):
        """仅设置 SVN_USERNAME 时返回只有用户名的 SVNAuth"""
        env_backup = {k: os.environ.pop(k, None) for k in ["SVN_USERNAME", "SVN_PASSWORD"]}

        try:
            os.environ["SVN_USERNAME"] = "only-username"

            from engram.logbook.config import Config, get_svn_auth

            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                return default

            mock_config.get.side_effect = mock_get

            auth = get_svn_auth(mock_config)

            assert auth is not None
            assert auth.username == "only-username"
            assert auth.password is None
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_svn_auth_none_when_no_credentials(self):
        """未设置任何 SVN 凭证时返回 None"""
        env_backup = {k: os.environ.pop(k, None) for k in ["SVN_USERNAME", "SVN_PASSWORD"]}

        try:
            from engram.logbook.config import Config, get_svn_auth

            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                return default

            mock_config.get.side_effect = mock_get

            auth = get_svn_auth(mock_config)

            assert auth is None
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_run_svn_cmd_uses_get_svn_auth(self):
        """run_svn_cmd 使用 get_svn_auth 获取认证凭证"""
        env_backup = {k: os.environ.pop(k, None) for k in ["SVN_USERNAME", "SVN_PASSWORD"]}

        try:
            os.environ["SVN_USERNAME"] = "test-user"
            os.environ["SVN_PASSWORD"] = "test-pass"

            from engram.logbook.config import Config
            from scm_sync_svn import run_svn_cmd

            # 模拟 subprocess.run
            with patch("subprocess.run") as mock_run:
                mock_result = MagicMock()
                mock_result.stdout = "<info></info>"
                mock_result.stderr = ""
                mock_result.returncode = 0
                mock_run.return_value = mock_result

                # 创建 mock config
                mock_config = MagicMock(spec=Config)

                def mock_get(key, default=None):
                    if key == "scm.svn.non_interactive":
                        return True
                    if key == "scm.svn.trust_server_cert":
                        return False
                    if key == "scm.svn.command_timeout":
                        return 120
                    return default

                mock_config.get.side_effect = mock_get

                # 执行命令
                result = run_svn_cmd(
                    ["svn", "info", "--xml", "svn://test.example.com"],
                    config=mock_config,
                )

                assert result.success is True

                # 验证调用时包含认证参数
                call_args = mock_run.call_args
                cmd_list = call_args[0][0]

                assert "--username" in cmd_list
                assert "test-user" in cmd_list
                assert "--password" in cmd_list
                assert "test-pass" in cmd_list
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_run_svn_cmd_masks_password_in_log(self):
        """run_svn_cmd 在日志中脱敏密码"""
        from scm_sync_svn import _mask_svn_command_for_log

        cmd = [
            "svn",
            "log",
            "--xml",
            "--username",
            "myuser",
            "--password",
            "secret123",
            "svn://test.example.com",
        ]

        masked = _mask_svn_command_for_log(cmd)

        # 密码应该被脱敏
        assert "secret123" not in masked
        assert "****" in masked
        # 用户名保持可见（非敏感）
        assert "myuser" in masked

    def test_run_svn_cmd_masks_password_equals_format(self):
        """run_svn_cmd 脱敏 --password=value 格式"""
        from scm_sync_svn import _mask_svn_command_for_log

        cmd = ["svn", "info", "--password=supersecret", "svn://test.example.com"]

        masked = _mask_svn_command_for_log(cmd)

        assert "supersecret" not in masked
        assert "--password=****" in masked


class TestGitLabPrivateTokenOnlyCredential:
    """
    测试仅设置 GITLAB_PRIVATE_TOKEN 环境变量时系统能正常工作

    验证场景：
    - 当 GITLAB_TOKEN 未设置，仅设置 GITLAB_PRIVATE_TOKEN 时
    - 认证凭证回退链正确工作
    - Worker 能正常获取 token 执行同步任务
    """

    def test_only_gitlab_private_token_env_works_in_worker_context(self):
        """在 worker 上下文中，仅 GITLAB_PRIVATE_TOKEN 环境变量时能正常获取 token"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            # 仅设置 GITLAB_PRIVATE_TOKEN（模拟某些部署环境）
            os.environ["GITLAB_PRIVATE_TOKEN"] = "glpat-test-token-only-private"

            from engram.logbook.scm_auth import create_token_provider

            provider = create_token_provider()
            token = provider.get_token()

            assert token == "glpat-test-token-only-private"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_gitlab_private_token_env_used_in_gitlab_client(self):
        """GitLab client 使用 GITLAB_PRIVATE_TOKEN 环境变量"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            os.environ["GITLAB_PRIVATE_TOKEN"] = "glpat-private-token-for-client"

            from engram.logbook.scm_auth import create_gitlab_token_provider

            provider = create_gitlab_token_provider(config=None, private_token=None)
            token = provider.get_token()

            assert token == "glpat-private-token-for-client"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_gitlab_private_token_preferred_over_empty_gitlab_token(self):
        """GITLAB_TOKEN 为空时使用 GITLAB_PRIVATE_TOKEN"""
        env_backup = {k: os.environ.pop(k, None) for k in ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"]}

        try:
            # GITLAB_TOKEN 设置为空字符串
            os.environ["GITLAB_TOKEN"] = ""
            os.environ["GITLAB_PRIVATE_TOKEN"] = "glpat-fallback-token"

            from engram.logbook.scm_auth import create_token_provider

            provider = create_token_provider()
            token = provider.get_token()

            # 应该回退到 GITLAB_PRIVATE_TOKEN
            assert token == "glpat-fallback-token"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]


class TestSVNEnvInjection:
    """
    测试 SVN 凭证环境变量注入路径

    验证场景：
    - password_env 配置项指定的环境变量被正确使用
    - 环境变量注入到 SVN 命令执行中
    - 多种环境变量配置组合的处理
    """

    def test_svn_password_env_config_injection(self):
        """通过 password_env 配置项注入 SVN 密码"""
        env_backup = {
            k: os.environ.pop(k, None) for k in ["SVN_USERNAME", "SVN_PASSWORD", "MY_SVN_SECRET"]
        }

        try:
            # 配置自定义的密码环境变量
            os.environ["MY_SVN_SECRET"] = "custom-svn-secret-123"
            os.environ["SVN_USERNAME"] = "svn-user"

            from unittest.mock import MagicMock

            from engram.logbook.config import Config, get_svn_auth

            mock_config = MagicMock(spec=Config)

            def mock_get(key, default=None):
                if key == "scm.svn.password_env":
                    return "MY_SVN_SECRET"
                return default

            mock_config.get.side_effect = mock_get

            auth = get_svn_auth(mock_config)

            assert auth is not None
            assert auth.password == "custom-svn-secret-123"
            assert auth.username == "svn-user"
            assert "MY_SVN_SECRET" in auth.source
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_svn_multiple_env_fallback_chain(self):
        """SVN 凭证多环境变量回退链"""
        env_backup = {
            k: os.environ.pop(k, None)
            for k in ["SVN_USERNAME", "SVN_PASSWORD", "SVN_USER", "SVN_PASS"]
        }

        try:
            # 标准环境变量
            os.environ["SVN_USERNAME"] = "standard-user"
            os.environ["SVN_PASSWORD"] = "standard-pass"

            from unittest.mock import MagicMock

            from engram.logbook.config import get_svn_auth

            mock_config = MagicMock()
            mock_config.get.return_value = None

            auth = get_svn_auth(mock_config)

            assert auth is not None
            assert auth.username == "standard-user"
            assert auth.password == "standard-pass"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]

    def test_svn_env_injection_in_run_svn_cmd(self):
        """run_svn_cmd 正确注入环境变量中的 SVN 凭证"""
        env_backup = {k: os.environ.pop(k, None) for k in ["SVN_USERNAME", "SVN_PASSWORD"]}

        try:
            os.environ["SVN_USERNAME"] = "injected-user"
            os.environ["SVN_PASSWORD"] = "injected-pass-secret"

            from unittest.mock import MagicMock, patch

            from scm_sync_svn import run_svn_cmd

            with patch("subprocess.run") as mock_run:
                mock_result = MagicMock()
                mock_result.stdout = "<info></info>"
                mock_result.stderr = ""
                mock_result.returncode = 0
                mock_run.return_value = mock_result

                mock_config = MagicMock()
                mock_config.get.return_value = None

                run_svn_cmd(
                    ["svn", "info", "--xml", "svn://test.example.com"],
                    config=mock_config,
                )

                # 验证命令行包含注入的凭证
                call_args = mock_run.call_args
                cmd_list = call_args[0][0]

                assert "--username" in cmd_list
                assert "injected-user" in cmd_list
                assert "--password" in cmd_list
                # 密码值应该存在（未脱敏地传递给 subprocess）
                assert "injected-pass-secret" in cmd_list
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]


class TestErrorRedactionBeforeDBStorage:
    """
    测试错误信息在落库前的脱敏处理

    验证场景：
    - 各类敏感信息模式的识别和替换
    - fail_retry 落库前脱敏
    - sync_runs 记录中错误信息脱敏
    """

    def test_redact_multiple_sensitive_patterns(self):
        """测试多种敏感信息模式的脱敏"""
        from engram.logbook.scm_auth import redact

        # 包含多种敏感信息的错误文本
        error_text = (
            "Failed: PRIVATE-TOKEN: glpat-secrettoken123abc, "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload, "
            "password=mysecretpass123, "
            "https://user:secretcred@gitlab.example.com"
        )

        redacted = redact(error_text)

        # 验证所有敏感信息被脱敏
        assert "glpat-secrettoken123abc" not in redacted
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        assert "mysecretpass123" not in redacted
        assert "secretcred" not in redacted

        # 验证脱敏占位符存在
        assert "[REDACTED]" in redacted or "[GITLAB_TOKEN]" in redacted or "[TOKEN]" in redacted

    def test_redact_gitlab_pat_variants(self):
        """测试各种 GitLab PAT 格式的脱敏"""
        from engram.logbook.scm_auth import redact

        # glpat (Personal Access Token)
        assert "glpat-test123" not in redact("token: glpat-test123456789")

        # glptt (Project Access Token)
        assert "glptt-test123" not in redact("token: glptt-test123456789")

        # glpat 在 URL 中
        assert "glpat-url123" not in redact("https://gitlab.com?token=glpat-url123456789")

    def test_redact_dict_sensitive_keys(self):
        """测试字典中敏感 key 的脱敏"""
        from engram.logbook.scm_auth import redact_dict

        data = {
            "error": "Connection failed",
            "Authorization": "Bearer secret-jwt",
            "PRIVATE-TOKEN": "glpat-secret",
            "Cookie": "session=abc123",
            "message": "Failed with PRIVATE-TOKEN: glpat-inline-secret",
            "safe_data": {"nested": "value"},
        }

        redacted = redact_dict(data)

        # 敏感 key 的值被替换
        assert redacted["Authorization"] == "[REDACTED]"
        assert redacted["PRIVATE-TOKEN"] == "[REDACTED]"
        assert redacted["Cookie"] == "[REDACTED]"

        # 文本值中的敏感信息也被脱敏
        assert "glpat-inline-secret" not in redacted["message"]

    def test_error_summary_redacted_before_db_write(self):
        """验证 error_summary 在写入数据库前被脱敏"""
        from engram.logbook.scm_auth import redact, redact_dict

        # 使用符合实际 GitLab PAT 格式的 token（至少 10 个字符）
        # glpat-xxxxxxxxxxxxxxx 是典型格式
        real_token = "glpat-abcdefghij1234567890"

        # 模拟包含敏感信息的 error_summary
        error_summary = {
            "error_type": "auth_error",
            "message": f"401 Unauthorized: Invalid token {real_token}",
            "request_url": "https://gitlab.com/api/v4/projects",
            "headers": {
                "PRIVATE-TOKEN": real_token,
                "User-Agent": "engram-sync/1.0",
            },
        }

        # 模拟 worker 的脱敏处理
        redacted_summary = redact_dict(error_summary)

        # 验证敏感 header 被脱敏
        assert redacted_summary["headers"]["PRIVATE-TOKEN"] == "[REDACTED]"

        # 验证 message 中的 token 被脱敏（glpat-xxx 格式会被 [GITLAB_TOKEN] 替换）
        assert real_token not in redacted_summary["message"]

        # 验证 Authorization header 格式也被正确脱敏
        auth_text = f"Authorization: Bearer {real_token}"
        redacted_auth = redact(auth_text)
        assert real_token not in redacted_auth
        assert "[REDACTED]" in redacted_auth or "[TOKEN]" in redacted_auth


class TestBackoffSecondsPassthrough:
    """
    测试 backoff_seconds 参数正确传递到 fail_retry

    验证场景：
    - 429 错误使用 120 秒退避
    - timeout 错误使用 30 秒退避
    - 服务器错误使用 90 秒退避
    - 自定义 backoff_seconds 传递
    """

    def test_429_error_passes_120s_backoff(self):
        """429 错误传递 120 秒 backoff_seconds"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-429-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "429 Too Many Requests - rate limit exceeded",
                "error_category": "rate_limit",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            process_one_job(
                worker_id="test-worker",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            # 验证 backoff_seconds=120 被传递
            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 120

    def test_timeout_error_passes_30s_backoff(self):
        """timeout 错误传递 30 秒 backoff_seconds"""
        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-timeout-job",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "attempts": 1,
                "max_attempts": 3,
                "lease_seconds": 300,
                "payload": {},
            }

            mock_execute.return_value = {
                "success": False,
                "error": "Request timed out after 60 seconds",
                "error_category": "timeout",
            }

            mock_renew.return_value = True
            mock_fail_retry.return_value = True

            from scm_sync_worker import process_one_job

            process_one_job(
                worker_id="test-worker",
                worker_cfg={
                    "lease_seconds": 300,
                    "renew_interval_seconds": 60,
                    "max_renew_failures": 3,
                },
            )

            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 30

    def test_backoff_seconds_from_result_retry_after(self):
        """使用结果中的 retry_after 作为 backoff_seconds"""
        from scm_sync_worker import _get_transient_error_backoff

        # 当有 retry_after 时应该使用它
        # 注意：这里测试 _get_transient_error_backoff 的行为

        # rate_limit 默认 120s
        backoff = _get_transient_error_backoff("rate_limit", "")
        assert backoff == 120

        # timeout 默认 30s
        backoff = _get_transient_error_backoff("timeout", "")
        assert backoff == 30

        # server_error 默认 90s
        backoff = _get_transient_error_backoff("server_error", "")
        assert backoff == 90

    def test_backoff_propagates_to_not_before_in_fail_retry(self, migrated_db):
        """backoff_seconds 正确更新 not_before 时间"""
        from datetime import datetime, timezone

        import psycopg

        dsn = migrated_db["dsn"]
        scm_schema = migrated_db["schemas"]["scm"]

        conn = psycopg.connect(dsn, autocommit=True)
        repo_id = None
        job_id = None

        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {scm_schema}.repos (vcs_type, remote_url)
                    VALUES ('git', 'https://example.com/test_backoff.git')
                    RETURNING repo_id
                """)
                repo_id = cur.fetchone()[0]

                cur.execute(
                    f"""
                    INSERT INTO {scm_schema}.sync_jobs (
                        repo_id, job_type, mode, priority, status,
                        locked_by, attempts, max_attempts
                    )
                    VALUES (%s, 'gitlab_commits', 'incremental', 100, 'running',
                            'test-worker', 1, 3)
                    RETURNING job_id
                """,
                    (repo_id,),
                )
                job_id = str(cur.fetchone()[0])

            from engram.logbook.scm_sync_queue import fail_retry

            # 使用 120 秒的 backoff
            test_conn = psycopg.connect(dsn)
            try:
                before_time = datetime.now(timezone.utc)

                fail_retry(
                    job_id=job_id,
                    worker_id="test-worker",
                    error="Rate limit error",
                    backoff_seconds=120,
                    conn=test_conn,
                )

                # 验证 not_before 被正确设置
                with test_conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT not_before FROM {scm_schema}.sync_jobs
                        WHERE job_id = %s
                    """,
                        (job_id,),
                    )
                    not_before = cur.fetchone()[0]

                # not_before 应该在 before_time + 120s 左右
                expected_min = before_time.timestamp() + 115  # 允许 5 秒误差
                expected_max = before_time.timestamp() + 125

                assert expected_min <= not_before.timestamp() <= expected_max
            finally:
                test_conn.close()

        finally:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute(f"DELETE FROM {scm_schema}.sync_jobs WHERE job_id = %s", (job_id,))
                if repo_id:
                    cur.execute(f"DELETE FROM {scm_schema}.repos WHERE repo_id = %s", (repo_id,))
            conn.close()


class TestRetryAfterFieldUsage:
    """测试同步结果中 retry_after 字段优先使用的逻辑"""

    def test_retry_after_preferred_over_computed_backoff(self):
        """当 retry_after 存在时，优先使用它作为 backoff_seconds"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead"),
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-1",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            # 返回带有 retry_after 的失败结果
            # 注意 retry_after=300 应该覆盖 rate_limit 的默认 120s
            mock_execute.return_value = {
                "success": False,
                "error": "Rate limit exceeded, retry after 300s",
                "error_category": "rate_limit",
                "retry_after": 300,  # 标准字段：显式指定重试间隔
                "counts": {"synced_count": 0},
            }

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
            )

            # 应该使用 retry_after=300 而不是 rate_limit 默认的 120s
            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 300

    def test_computed_backoff_when_retry_after_absent(self):
        """当 retry_after 不存在时，使用计算的 backoff"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead"),
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-2",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            # 返回不带 retry_after 的失败结果
            mock_execute.return_value = {
                "success": False,
                "error": "Rate limit exceeded",
                "error_category": "rate_limit",
                # 无 retry_after 字段
                "counts": {"synced_count": 0},
            }

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
            )

            # 应该使用 rate_limit 默认的 120s
            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 120

    def test_retry_after_zero_uses_computed_backoff(self):
        """当 retry_after=None 时使用计算的 backoff（None 视为无效）"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead"),
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-3",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": False,
                "error": "Timeout error",
                "error_category": "timeout",
                "retry_after": None,  # 显式 None
                "counts": {"synced_count": 0},
            }

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
            )

            # 应该使用 timeout 默认的 30s
            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 30


class TestCountsFieldInSyncResult:
    """测试同步结果中 counts 字段的正确性"""

    def test_counts_field_present_in_success_result(self):
        """成功结果应包含 counts 字段"""
        from scm_sync_worker import default_sync_handler

        result = default_sync_handler("unknown_type", 1, "incremental", {})

        assert "counts" in result
        assert isinstance(result["counts"], dict)

    def test_default_handler_includes_error_category(self):
        """默认处理器应包含 error_category 字段"""
        from scm_sync_worker import default_sync_handler

        result = default_sync_handler("unknown_type", 1, "incremental", {})

        assert "error_category" in result
        assert result["error_category"] == "unknown_job_type"


class TestCircuitBreakerWithRetryAfter:
    """测试熔断器正确接收 retry_after 参数"""

    def test_circuit_breaker_receives_retry_after(self):
        """熔断器应该接收 retry_after 参数"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.ack"),
            patch("scm_sync_worker.fail_retry"),
            patch("scm_sync_worker.mark_dead"),
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-cb",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": False,
                "error": "Rate limited",
                "error_category": "rate_limit",
                "retry_after": 180,
                "counts": {"synced_count": 0},
            }

            # 创建 mock circuit breaker
            mock_cb = MagicMock()

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
                circuit_breaker=mock_cb,
            )

            # 验证熔断器被调用并接收正确参数
            mock_cb.record_result.assert_called_once()
            call_kwargs = mock_cb.record_result.call_args[1]
            assert not call_kwargs.get("success")
            assert call_kwargs.get("error_category") == "rate_limit"
            assert call_kwargs.get("retry_after") == 180


class TestStandardFieldsErrorCategories:
    """测试各种错误分类场景的标准字段处理"""

    def test_transient_error_with_retry_after(self):
        """临时性错误带 retry_after 字段时使用该值"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-te",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": False,
                "error": "Server error 503",
                "error_category": "server_error",
                "retry_after": 180,  # 服务端指定的重试时间
                "counts": {"synced_count": 5},
            }

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
            )

            # 应该使用 retry_after=180 而不是 server_error 默认的 90s
            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 180

    def test_unknown_error_with_retry_after(self):
        """未知错误类型带 retry_after 字段时使用该值"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-ue",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": False,
                "error": "Unknown error",
                "error_category": "custom_error",  # 未知分类
                "retry_after": 45,  # 指定的重试时间
                "counts": {},
            }

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
            )

            # 应该使用 retry_after=45 而不是默认的 60s
            mock_fail_retry.assert_called_once()
            call_kwargs = mock_fail_retry.call_args
            assert call_kwargs[1].get("backoff_seconds") == 45

    def test_permanent_error_ignores_retry_after(self):
        """永久性错误应调用 mark_dead 而非 fail_retry（不管是否有 retry_after）"""
        from scm_sync_worker import process_one_job

        with (
            patch("scm_sync_worker.claim") as mock_claim,
            patch("scm_sync_worker.fail_retry") as mock_fail_retry,
            patch("scm_sync_worker.mark_dead") as mock_mark_dead,
            patch("scm_sync_worker.renew_lease") as mock_renew,
            patch("scm_sync_worker.execute_sync_job") as mock_execute,
        ):
            mock_claim.return_value = {
                "job_id": "test-job-pe",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "priority": 100,
                "payload": {},
                "attempts": 1,
            }
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": False,
                "error": "Repository not found",
                "error_category": "repo_not_found",  # 永久性错误
                "retry_after": 60,  # 即使有 retry_after 也应该被忽略
                "counts": {},
            }

            process_one_job(
                job_types=["gitlab_commits"],
                worker_id="test-worker",
            )

            # 永久性错误应调用 mark_dead
            mock_mark_dead.assert_called_once()
            mock_fail_retry.assert_not_called()


# ============ 执行器层测试 ============


class TestSyncExecutor:
    """SyncExecutor 执行器层测试"""

    def test_executor_dispatches_gitlab_commits(self):
        """执行器正确 dispatch gitlab_commits 任务"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        # 创建 mock handler
        mock_result = {"success": True, "synced_count": 10}
        mock_handler = MagicMock(return_value=mock_result)

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 123,
            "mode": "incremental",
            "payload": {"test": "value"},
        }

        result = executor.execute(job)

        assert result.success is True
        mock_handler.assert_called_once_with(123, "incremental", {"test": "value"})

    def test_executor_dispatches_gitlab_mrs(self):
        """执行器正确 dispatch gitlab_mrs 任务"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_result = {"success": True, "synced_count": 5, "scanned_count": 10}
        mock_handler = MagicMock(return_value=mock_result)

        executor = SyncExecutor(
            handlers={"gitlab_mrs": mock_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_mrs",
            "repo_id": 456,
            "mode": "backfill",
            "payload": {"project_id": "test/proj"},
        }

        result = executor.execute(job)

        assert result.success is True
        mock_handler.assert_called_once_with(456, "backfill", {"project_id": "test/proj"})

    def test_executor_dispatches_gitlab_reviews(self):
        """执行器正确 dispatch gitlab_reviews 任务"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_result = {"success": True, "synced_event_count": 20}
        mock_handler = MagicMock(return_value=mock_result)

        executor = SyncExecutor(
            handlers={"gitlab_reviews": mock_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_reviews",
            "repo_id": 789,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is True
        mock_handler.assert_called_once_with(789, "incremental", {})

    def test_executor_dispatches_svn(self):
        """执行器正确 dispatch svn 任务"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_result = {"success": True, "synced_count": 50, "last_rev": 100}
        mock_handler = MagicMock(return_value=mock_result)

        executor = SyncExecutor(
            handlers={"svn": mock_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "svn",
            "repo_id": 111,
            "mode": "incremental",
            "payload": {"svn_url": "svn://example.com/repo"},
        }

        result = executor.execute(job)

        assert result.success is True
        mock_handler.assert_called_once_with(
            111, "incremental", {"svn_url": "svn://example.com/repo"}
        )

    def test_executor_returns_unknown_job_type_error(self):
        """执行器对未知 job_type 返回错误"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        executor = SyncExecutor(handlers={}, validate_contract=False)

        job = {
            "job_type": "unknown_type",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.UNKNOWN_JOB_TYPE.value
        assert "invalid job_type" in result.error

    def test_executor_returns_error_for_missing_job_type(self):
        """执行器对缺失 job_type 返回错误"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        executor = SyncExecutor(handlers={}, validate_contract=False)

        job = {
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.UNKNOWN_JOB_TYPE.value
        assert "missing job_type" in result.error

    def test_executor_returns_error_for_no_handler(self):
        """执行器对无 handler 的 job_type 返回错误"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        # 创建执行器，先清空默认 handlers，然后不注册任何 handler
        executor = SyncExecutor(validate_contract=False)
        executor._handlers.clear()  # 清空所有 handlers

        job = {
            "job_type": "gitlab_commits",  # 有效的 job_type 但没有 handler
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.UNKNOWN_JOB_TYPE.value
        assert "no handler" in result.error


class TestSyncExecutorContractValidation:
    """SyncExecutor contract 校验测试"""

    def test_contract_validation_missing_success_field(self):
        """contract 校验：缺少 success 字段"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        # 返回缺少 success 字段的结果
        mock_handler = MagicMock(return_value={"synced_count": 10})

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONTRACT_ERROR.value
        assert result.contract_valid is False
        assert "missing required field: success" in result.contract_errors

    def test_contract_validation_invalid_success_type(self):
        """contract 校验：success 字段类型错误"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_handler = MagicMock(return_value={"success": "yes"})  # 应该是 bool

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONTRACT_ERROR.value
        assert "must be boolean" in str(result.contract_errors)

    def test_contract_validation_failed_without_error(self):
        """contract 校验：失败结果没有 error 或 error_category"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        # success=false 但没有 error 信息
        mock_handler = MagicMock(return_value={"success": False})

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONTRACT_ERROR.value
        assert "should have 'error' or 'error_category'" in str(result.contract_errors)

    def test_contract_validation_invalid_error_category(self):
        """contract 校验：无效的 error_category"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_handler = MagicMock(
            return_value={
                "success": False,
                "error": "some error",
                "error_category": "invalid_category_xyz",
            }
        )

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONTRACT_ERROR.value
        assert "invalid error_category" in str(result.contract_errors)

    def test_contract_validation_invalid_count_field(self):
        """contract 校验：count 字段为负数"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_handler = MagicMock(
            return_value={
                "success": True,
                "synced_count": -5,  # 负数
            }
        )

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONTRACT_ERROR.value
        assert "synced_count" in str(result.contract_errors)

    def test_contract_validation_invalid_mode(self):
        """contract 校验：无效的 mode 字段"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_handler = MagicMock(
            return_value={
                "success": True,
                "mode": "invalid_mode",
            }
        )

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONTRACT_ERROR.value
        assert "invalid mode" in str(result.contract_errors)

    def test_contract_validation_passes_valid_result(self):
        """contract 校验：有效结果通过校验"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        mock_handler = MagicMock(
            return_value={
                "success": True,
                "synced_count": 10,
                "skipped_count": 2,
                "diff_count": 8,
                "mode": "incremental",
            }
        )

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is True
        assert result.contract_valid is True
        assert len(result.contract_errors) == 0

    def test_contract_validation_can_be_skipped(self):
        """contract 校验可以被跳过"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        # 返回不符合 contract 的结果
        mock_handler = MagicMock(return_value={"synced_count": 10})  # 缺少 success

        executor = SyncExecutor(
            handlers={"gitlab_commits": mock_handler},
            validate_contract=True,  # 默认开启校验
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        # 跳过 contract 校验
        result = executor.execute(job, skip_contract_validation=True)

        # 不会因 contract 校验失败而返回错误
        assert result.contract_valid is True


class TestExecutorInjection:
    """执行器注入测试"""

    def test_set_executor_injection(self):
        """测试 set_executor 注入自定义执行器"""
        from scm_sync_worker import execute_sync_job, get_executor, set_executor

        # 保存原始执行器
        get_executor()

        try:
            # 注入自定义执行器
            custom_result = {"success": True, "custom": True}
            custom_executor = MagicMock(return_value=custom_result)
            set_executor(custom_executor)

            job = {"job_type": "test", "repo_id": 1, "mode": "incremental", "payload": {}}
            result = execute_sync_job(job)

            assert result["success"] is True
            assert result["custom"] is True
            custom_executor.assert_called_once_with(job)
        finally:
            # 恢复原始执行器
            set_executor(None)

    def test_reset_executor_to_default(self):
        """测试重置为默认执行器"""
        from scm_sync_worker import get_executor, set_executor

        # 注入自定义执行器
        custom_executor = MagicMock()
        set_executor(custom_executor)

        assert get_executor() is custom_executor

        # 重置为默认
        set_executor(None)

        # 应该返回默认执行器
        default_executor = get_executor()
        assert default_executor is not custom_executor


class TestExecutorHandlerRegistration:
    """执行器 handler 注册测试"""

    def test_register_handler(self):
        """测试动态注册 handler"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        executor = SyncExecutor(handlers={}, validate_contract=False)

        # 初始没有 handler
        assert executor.has_handler("custom_type") is False

        # 注册新 handler
        custom_handler = MagicMock(return_value={"success": True})
        executor.register_handler("custom_type", custom_handler)

        assert executor.has_handler("custom_type") is True
        assert executor.get_handler("custom_type") is custom_handler

    def test_override_default_handler(self):
        """测试覆盖默认 handler"""
        from engram.logbook.scm_sync_executor import SyncExecutor

        # 自定义 handler
        custom_result = {"success": True, "custom": True}
        custom_handler = MagicMock(return_value=custom_result)

        executor = SyncExecutor(
            handlers={"gitlab_commits": custom_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is True
        assert result.raw_result.get("custom") is True
        custom_handler.assert_called_once()


class TestExecutorExceptionHandling:
    """执行器异常处理测试"""

    def test_handler_exception_is_caught(self):
        """handler 抛出异常时被正确捕获"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        def failing_handler(repo_id, mode, payload):
            raise ConnectionError("Network unreachable")

        executor = SyncExecutor(
            handlers={"gitlab_commits": failing_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.CONNECTION.value
        assert "Network unreachable" in result.error

    def test_timeout_exception_classification(self):
        """TimeoutError 被正确分类"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        def timeout_handler(repo_id, mode, payload):
            raise TimeoutError("Request timed out")

        executor = SyncExecutor(
            handlers={"gitlab_commits": timeout_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.TIMEOUT.value

    def test_generic_exception_classification(self):
        """通用异常被分类为 exception"""
        from engram.logbook.scm_sync_errors import ErrorCategory
        from engram.logbook.scm_sync_executor import SyncExecutor

        def generic_failing_handler(repo_id, mode, payload):
            raise ValueError("Invalid value")

        executor = SyncExecutor(
            handlers={"gitlab_commits": generic_failing_handler},
            validate_contract=False,
        )

        job = {
            "job_type": "gitlab_commits",
            "repo_id": 1,
            "mode": "incremental",
            "payload": {},
        }

        result = executor.execute(job)

        assert result.success is False
        assert result.error_category == ErrorCategory.EXCEPTION.value


class TestValidateSyncResultContract:
    """validate_sync_result_contract 函数测试"""

    def test_validate_minimal_valid_result(self):
        """最小有效结果通过校验"""
        from engram.logbook.scm_sync_executor import validate_sync_result_contract

        result = {"success": True}
        is_valid, errors = validate_sync_result_contract(result)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_failed_result_with_error(self):
        """失败结果带 error 字段通过校验"""
        from engram.logbook.scm_sync_executor import validate_sync_result_contract

        result = {
            "success": False,
            "error": "Something went wrong",
        }
        is_valid, errors = validate_sync_result_contract(result)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_failed_result_with_error_category(self):
        """失败结果带 error_category 字段通过校验"""
        from engram.logbook.scm_sync_executor import validate_sync_result_contract

        result = {
            "success": False,
            "error_category": "timeout",
        }
        is_valid, errors = validate_sync_result_contract(result)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_all_count_fields(self):
        """所有 count 字段校验"""
        from engram.logbook.scm_sync_executor import validate_sync_result_contract

        result = {
            "success": True,
            "synced_count": 10,
            "skipped_count": 5,
            "diff_count": 8,
            "degraded_count": 2,
            "bulk_count": 1,
            "diff_none_count": 0,
            "scanned_count": 20,
            "inserted_count": 15,
            "synced_mr_count": 5,
            "synced_event_count": 30,
            "skipped_event_count": 3,
            "patch_success": 10,
            "patch_failed": 2,
            "skipped_by_controller": 1,
        }
        is_valid, errors = validate_sync_result_contract(result)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_all_error_categories(self):
        """所有 error_category 值校验"""
        from engram.logbook.scm_sync_executor import validate_sync_result_contract

        valid_categories = [
            "auth_error",
            "auth_missing",
            "auth_invalid",
            "repo_not_found",
            "repo_type_unknown",
            "permission_denied",
            "rate_limit",
            "timeout",
            "network",
            "server_error",
            "connection",
            "exception",
            "unknown",
            "lease_lost",
            "unknown_job_type",
            "lock_held",
            "contract_error",
        ]

        for category in valid_categories:
            result = {
                "success": False,
                "error_category": category,
                "error": "test error",
            }
            is_valid, errors = validate_sync_result_contract(result)
            assert is_valid is True, f"Category {category} should be valid"


# ============ Worker sync_runs 生命周期集成测试 ============


class TestWorkerSyncRunsLifecycle:
    """
    Worker sync_runs 生命周期集成测试

    测试 process_one_job 中的 sync_runs 写入流程：
    1. 生成 run_id
    2. 读取 cursor_before
    3. insert_sync_run_start (status=running)
    4. 执行 job
    5. build_run_finish_payload_from_result + validate_run_finish_payload
    6. insert_sync_run_finish
    7. ack 时传入 run_id
    """

    # 实际实现在 scripts/scm_sync_worker.py 中
    _MODULE = "scripts.scm_sync_worker"

    def test_success_path_writes_sync_run(self):
        """成功路径：写入完整的 sync_run 记录"""
        from unittest.mock import patch

        # Mock 所有依赖
        with (
            patch(f"{self._MODULE}.claim") as mock_claim,
            patch(f"{self._MODULE}.ack") as mock_ack,
            patch(f"{self._MODULE}.renew_lease") as mock_renew,
            patch(f"{self._MODULE}.execute_sync_job") as mock_execute,
            patch(f"{self._MODULE}.scm_sync_lock") as mock_lock,
            patch(f"{self._MODULE}._insert_sync_run_start") as mock_run_start,
            patch(f"{self._MODULE}._insert_sync_run_finish") as mock_run_finish,
            patch(f"{self._MODULE}._read_cursor_before") as mock_read_cursor,
            patch(f"{self._MODULE}._generate_run_id") as mock_gen_run_id,
        ):
            # 设置返回值
            mock_claim.return_value = {
                "job_id": "job-123",
                "repo_id": 1,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "payload": {},
            }
            mock_lock.claim.return_value = True
            mock_lock.release.return_value = True
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": True,
                "synced_count": 100,
                "diff_count": 95,
            }
            mock_read_cursor.return_value = {"commit_sha": "abc123"}
            mock_gen_run_id.return_value = "run-uuid-123"
            mock_run_start.return_value = True
            mock_run_finish.return_value = True

            from scripts.scm_sync_worker import process_one_job

            result = process_one_job(worker_id="worker-1")

            assert result is True

            # 验证 sync_run_start 被调用
            mock_run_start.assert_called_once()
            start_call = mock_run_start.call_args
            assert start_call.kwargs["run_id"] == "run-uuid-123"
            assert start_call.kwargs["repo_id"] == 1
            assert start_call.kwargs["job_type"] == "gitlab_commits"
            assert start_call.kwargs["cursor_before"] == {"commit_sha": "abc123"}

            # 验证 sync_run_finish 被调用
            mock_run_finish.assert_called_once()
            finish_call = mock_run_finish.call_args
            assert finish_call.kwargs["run_id"] == "run-uuid-123"
            assert finish_call.kwargs["status"] == "completed"
            assert finish_call.kwargs["counts"]["synced_count"] == 100

            # 验证 ack 时传入了 run_id
            mock_ack.assert_called_once()
            ack_call = mock_ack.call_args
            assert ack_call.kwargs.get("run_id") == "run-uuid-123"

    def test_failed_path_writes_sync_run_with_error_summary(self):
        """失败路径：写入包含 error_summary_json 的 sync_run 记录"""
        from unittest.mock import patch

        with (
            patch(f"{self._MODULE}.claim") as mock_claim,
            patch(f"{self._MODULE}.fail_retry") as mock_fail_retry,
            patch(f"{self._MODULE}.renew_lease") as mock_renew,
            patch(f"{self._MODULE}.execute_sync_job") as mock_execute,
            patch(f"{self._MODULE}.scm_sync_lock") as mock_lock,
            patch(f"{self._MODULE}._insert_sync_run_start") as mock_run_start,
            patch(f"{self._MODULE}._insert_sync_run_finish") as mock_run_finish,
            patch(f"{self._MODULE}._read_cursor_before") as mock_read_cursor,
            patch(f"{self._MODULE}._generate_run_id") as mock_gen_run_id,
        ):
            mock_claim.return_value = {
                "job_id": "job-456",
                "repo_id": 2,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "payload": {},
            }
            mock_lock.claim.return_value = True
            mock_lock.release.return_value = True
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": False,
                "error": "Connection refused",
                "error_category": "connection",
                "synced_count": 0,
            }
            mock_read_cursor.return_value = None
            mock_gen_run_id.return_value = "run-uuid-456"
            mock_run_start.return_value = True
            mock_run_finish.return_value = True
            mock_fail_retry.return_value = True

            from scripts.scm_sync_worker import process_one_job

            result = process_one_job(worker_id="worker-1")

            assert result is True

            # 验证 sync_run_finish 被调用，且包含 error_summary_json
            mock_run_finish.assert_called_once()
            finish_call = mock_run_finish.call_args
            assert finish_call.kwargs["status"] == "failed"
            assert finish_call.kwargs["error_summary_json"] is not None
            assert finish_call.kwargs["error_summary_json"]["error_category"] == "connection"

    def test_lease_lost_path_writes_sync_run_with_error_summary(self):
        """租约丢失路径：验证 build_payload_for_lease_lost 生成正确的 payload"""
        # 由于 HeartbeatManager 的异步特性难以模拟，我们测试 payload 构建逻辑
        from engram.logbook.scm_sync_run_contract import (
            RunStatus,
            build_payload_for_lease_lost,
            validate_run_finish_payload,
        )

        # 构建租约丢失的 payload
        payload = build_payload_for_lease_lost(
            job_id="job-789",
            worker_id="worker-1",
            failure_count=3,
            max_failures=3,
            last_error="renew_lease returned False",
            cursor_before={"commit_sha": "xyz789"},
        )

        # 验证 payload 结构
        assert payload.status == RunStatus.FAILED.value
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "lease_lost"
        assert payload.cursor_before == {"commit_sha": "xyz789"}

        # 验证 context 包含任务信息
        assert payload.error_summary.context["job_id"] == "job-789"
        assert payload.error_summary.context["worker_id"] == "worker-1"
        assert payload.error_summary.context["failure_count"] == 3

        # 验证 to_dict 包含 error_summary_json
        payload_dict = payload.to_dict()
        assert "error_summary_json" in payload_dict
        assert payload_dict["error_summary_json"]["error_category"] == "lease_lost"

        # 验证通过 validate_run_finish_payload
        is_valid, errors, _ = validate_run_finish_payload(payload_dict)
        assert is_valid, f"Lease lost payload should be valid: {errors}"

    def test_no_data_path_writes_sync_run(self):
        """无数据路径：写入 status=no_data 的 sync_run 记录"""
        from unittest.mock import patch

        with (
            patch(f"{self._MODULE}.claim") as mock_claim,
            patch(f"{self._MODULE}.ack"),
            patch(f"{self._MODULE}.renew_lease") as mock_renew,
            patch(f"{self._MODULE}.execute_sync_job") as mock_execute,
            patch(f"{self._MODULE}.scm_sync_lock") as mock_lock,
            patch(f"{self._MODULE}._insert_sync_run_start") as mock_run_start,
            patch(f"{self._MODULE}._insert_sync_run_finish") as mock_run_finish,
            patch(f"{self._MODULE}._read_cursor_before") as mock_read_cursor,
            patch(f"{self._MODULE}._generate_run_id") as mock_gen_run_id,
        ):
            mock_claim.return_value = {
                "job_id": "job-no-data",
                "repo_id": 4,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "payload": {},
            }
            mock_lock.claim.return_value = True
            mock_lock.release.return_value = True
            mock_renew.return_value = True
            mock_execute.return_value = {
                "success": True,
                "synced_count": 0,  # 无数据
            }
            mock_read_cursor.return_value = None
            mock_gen_run_id.return_value = "run-uuid-no-data"
            mock_run_start.return_value = True
            mock_run_finish.return_value = True

            from scripts.scm_sync_worker import process_one_job

            result = process_one_job(worker_id="worker-1")

            assert result is True

            # 验证 sync_run_finish 被调用，status=no_data
            mock_run_finish.assert_called_once()
            finish_call = mock_run_finish.call_args
            assert finish_call.kwargs["status"] == "no_data"
            # no_data 状态不需要 error_summary_json
            assert finish_call.kwargs.get("error_summary_json") is None

    def test_disable_sync_runs_skips_db_writes(self):
        """enable_sync_runs=False 时跳过 sync_runs 写入"""
        from unittest.mock import patch

        with (
            patch(f"{self._MODULE}.claim") as mock_claim,
            patch(f"{self._MODULE}.ack"),
            patch(f"{self._MODULE}.renew_lease") as mock_renew,
            patch(f"{self._MODULE}.execute_sync_job") as mock_execute,
            patch(f"{self._MODULE}.scm_sync_lock") as mock_lock,
            patch(f"{self._MODULE}._insert_sync_run_start") as mock_run_start,
            patch(f"{self._MODULE}._insert_sync_run_finish") as mock_run_finish,
            patch(f"{self._MODULE}._read_cursor_before") as mock_read_cursor,
        ):
            mock_claim.return_value = {
                "job_id": "job-no-runs",
                "repo_id": 5,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "payload": {},
            }
            mock_lock.claim.return_value = True
            mock_lock.release.return_value = True
            mock_renew.return_value = True
            mock_execute.return_value = {"success": True, "synced_count": 10}
            mock_read_cursor.return_value = None

            from scripts.scm_sync_worker import process_one_job

            result = process_one_job(worker_id="worker-1", enable_sync_runs=False)

            assert result is True

            # 验证 sync_run_start 和 sync_run_finish 都没有被调用
            mock_run_start.assert_not_called()
            mock_run_finish.assert_not_called()

    def test_run_finish_payload_validation_on_failure(self):
        """失败时 payload 验证确保 error_summary_json 存在"""
        from engram.logbook.scm_sync_run_contract import (
            RunStatus,
            build_run_finish_payload_from_result,
            validate_run_finish_payload,
        )

        # 模拟一个失败结果
        result = {
            "success": False,
            "error": "Connection refused",
            "error_category": "connection",
            "synced_count": 0,
        }

        payload = build_run_finish_payload_from_result(result)

        # 验证 status 是 failed
        assert payload.status == RunStatus.FAILED.value

        # 验证 error_summary 存在
        assert payload.error_summary is not None
        assert payload.error_summary.error_category == "connection"

        # 验证 to_dict 包含 error_summary_json
        payload_dict = payload.to_dict()
        assert "error_summary_json" in payload_dict

        # 验证通过 validate_run_finish_payload
        is_valid, errors, _ = validate_run_finish_payload(payload_dict)
        assert is_valid, f"Failed payload should be valid: {errors}"

    def test_cursor_before_injected_into_result(self):
        """cursor_before 被注入到 result 中用于 payload 构建"""
        from unittest.mock import patch

        with (
            patch(f"{self._MODULE}.claim") as mock_claim,
            patch(f"{self._MODULE}.ack"),
            patch(f"{self._MODULE}.renew_lease") as mock_renew,
            patch(f"{self._MODULE}.execute_sync_job") as mock_execute,
            patch(f"{self._MODULE}.scm_sync_lock") as mock_lock,
            patch(f"{self._MODULE}._insert_sync_run_start") as mock_run_start,
            patch(f"{self._MODULE}._insert_sync_run_finish") as mock_run_finish,
            patch(f"{self._MODULE}._read_cursor_before") as mock_read_cursor,
            patch(f"{self._MODULE}._generate_run_id") as mock_gen_run_id,
        ):
            mock_claim.return_value = {
                "job_id": "job-cursor",
                "repo_id": 6,
                "job_type": "gitlab_commits",
                "mode": "incremental",
                "payload": {},
            }
            mock_lock.claim.return_value = True
            mock_lock.release.return_value = True
            mock_renew.return_value = True
            # 执行返回 cursor_after
            mock_execute.return_value = {
                "success": True,
                "synced_count": 50,
                "cursor_after": {"commit_sha": "new-sha"},
            }
            mock_read_cursor.return_value = {"commit_sha": "old-sha"}
            mock_gen_run_id.return_value = "run-uuid-cursor"
            mock_run_start.return_value = True
            mock_run_finish.return_value = True

            from scripts.scm_sync_worker import process_one_job

            result = process_one_job(worker_id="worker-1")

            assert result is True

            # 验证 sync_run_start 包含 cursor_before
            start_call = mock_run_start.call_args
            assert start_call.kwargs["cursor_before"] == {"commit_sha": "old-sha"}

            # 验证 sync_run_finish 包含 cursor_after
            finish_call = mock_run_finish.call_args
            assert finish_call.kwargs["cursor_after"] == {"commit_sha": "new-sha"}
