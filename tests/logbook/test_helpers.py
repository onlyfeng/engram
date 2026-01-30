# -*- coding: utf-8 -*-
"""
可复用测试 Helper 模块

提供:
- 数据创建 helpers（创建测试 repos, jobs, runs）
- 状态断言 helpers（验证任务状态、错误信息、脱敏）
- Mock 设置 helpers（常用 mock 模式）
- 清理 helpers（测试数据清理）

使用方式:
    from tests.test_helpers import (
        TestDataFactory,
        JobAssertions,
        MockHelpers,
        SensitiveDataAssertions,
    )
"""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Generator, Callable
from unittest.mock import patch, MagicMock

import psycopg


# ============ 数据创建 Helpers ============


@dataclass
class MockRepo:
    """测试仓库数据"""
    repo_id: int
    vcs_type: str
    remote_url: str
    
    def cleanup_sql(self, schema: str) -> str:
        """返回清理 SQL"""
        return f"DELETE FROM {schema}.repos WHERE repo_id = {self.repo_id}"


@dataclass
class MockJob:
    """测试同步任务数据"""
    job_id: str
    repo_id: int
    job_type: str
    status: str
    
    def cleanup_sql(self, schema: str) -> str:
        """返回清理 SQL"""
        return f"DELETE FROM {schema}.sync_jobs WHERE job_id = '{self.job_id}'"


@dataclass
class MockRun:
    """测试同步运行数据"""
    run_id: str
    repo_id: int
    job_type: str
    status: str
    
    def cleanup_sql(self, schema: str) -> str:
        """返回清理 SQL"""
        return f"DELETE FROM {schema}.sync_runs WHERE run_id = '{self.run_id}'"


class DataFactory:
    """
    测试数据工厂
    
    用于创建和管理测试数据，支持自动清理。
    
    使用示例:
        factory = DataFactory(conn, scm_schema="scm")
        
        # 创建测试数据
        repo = factory.create_repo(vcs_type="git", remote_url="https://example.com/test.git")
        job = factory.create_job(repo.repo_id, job_type="gitlab_commits")
        
        # 测试结束后自动清理（使用 context manager）
        # 或手动调用 factory.cleanup()
    """
    
    def __init__(self, conn: psycopg.Connection, scm_schema: str = "scm"):
        self.conn = conn
        self.scm_schema = scm_schema
        self._repos: List[MockRepo] = []
        self._jobs: List[MockJob] = []
        self._runs: List[MockRun] = []
    
    def create_repo(
        self,
        vcs_type: str = "git",
        remote_url: Optional[str] = None,
        **kwargs
    ) -> MockRepo:
        """
        创建测试仓库
        
        Args:
            vcs_type: 仓库类型（git, svn）
            remote_url: 远程 URL（自动生成如果为 None）
            **kwargs: 其他列的值
        
        Returns:
            TestRepo 实例
        """
        if remote_url is None:
            remote_url = f"https://example.com/test_{uuid.uuid4().hex[:8]}.git"
        
        with self.conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {self.scm_schema}.repos (vcs_type, remote_url)
                VALUES (%s, %s)
                RETURNING repo_id
            """, (vcs_type, remote_url))
            repo_id = cur.fetchone()[0]
        
        repo = MockRepo(repo_id=repo_id, vcs_type=vcs_type, remote_url=remote_url)
        self._repos.append(repo)
        return repo
    
    def create_job(
        self,
        repo_id: int,
        job_type: str = "gitlab_commits",
        mode: str = "incremental",
        priority: int = 100,
        status: str = "pending",
        locked_by: Optional[str] = None,
        locked_at: Optional[datetime] = None,
        lease_seconds: int = 300,
        attempts: int = 1,
        max_attempts: int = 3,
        last_error: Optional[str] = None,
        not_before: Optional[datetime] = None,
        **kwargs
    ) -> MockJob:
        """
        创建测试同步任务
        
        Args:
            repo_id: 仓库 ID
            job_type: 任务类型
            mode: 同步模式
            priority: 优先级
            status: 状态
            locked_by: 锁定者 worker ID
            locked_at: 锁定时间
            lease_seconds: 租约秒数
            attempts: 当前尝试次数
            max_attempts: 最大尝试次数
            last_error: 最后错误信息
            not_before: 最早可执行时间
            **kwargs: 其他列的值
        
        Returns:
            TestJob 实例
        """
        with self.conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {self.scm_schema}.sync_jobs (
                    repo_id, job_type, mode, priority, status,
                    locked_by, locked_at, lease_seconds, attempts, max_attempts,
                    last_error, not_before
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING job_id
            """, (
                repo_id, job_type, mode, priority, status,
                locked_by, locked_at, lease_seconds, attempts, max_attempts,
                last_error, not_before
            ))
            job_id = str(cur.fetchone()[0])
        
        job = MockJob(job_id=job_id, repo_id=repo_id, job_type=job_type, status=status)
        self._jobs.append(job)
        return job
    
    def create_expired_job(
        self,
        repo_id: int,
        minutes_expired: int = 10,
        lease_seconds: int = 300,
        **kwargs
    ) -> MockJob:
        """
        创建过期的 running 任务
        
        Args:
            repo_id: 仓库 ID
            minutes_expired: 过期时间（分钟）
            lease_seconds: 租约秒数
            **kwargs: 传递给 create_job 的其他参数
        
        Returns:
            TestJob 实例
        """
        locked_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_expired)
        return self.create_job(
            repo_id=repo_id,
            status="running",
            locked_by="dead-worker",
            locked_at=locked_at,
            lease_seconds=lease_seconds,
            **kwargs
        )
    
    def create_run(
        self,
        repo_id: int,
        job_type: str = "gitlab_commits",
        mode: str = "incremental",
        status: str = "running",
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        **kwargs
    ) -> MockRun:
        """
        创建测试同步运行
        
        Args:
            repo_id: 仓库 ID
            job_type: 任务类型
            mode: 同步模式
            status: 状态
            started_at: 开始时间
            finished_at: 结束时间
            **kwargs: 其他列的值
        
        Returns:
            TestRun 实例
        """
        run_id = str(uuid.uuid4())
        if started_at is None:
            started_at = datetime.now(timezone.utc)
        
        with self.conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {self.scm_schema}.sync_runs (
                    run_id, repo_id, job_type, mode, status, started_at, finished_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (run_id, repo_id, job_type, mode, status, started_at, finished_at))
        
        run = MockRun(run_id=run_id, repo_id=repo_id, job_type=job_type, status=status)
        self._runs.append(run)
        return run
    
    def create_expired_run(
        self,
        repo_id: int,
        minutes_running: int = 45,
        **kwargs
    ) -> MockRun:
        """
        创建超时的 running run
        
        Args:
            repo_id: 仓库 ID
            minutes_running: 运行时间（分钟）
            **kwargs: 传递给 create_run 的其他参数
        
        Returns:
            TestRun 实例
        """
        started_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_running)
        return self.create_run(
            repo_id=repo_id,
            status="running",
            started_at=started_at,
            **kwargs
        )
    
    def cleanup(self):
        """清理所有创建的测试数据"""
        with self.conn.cursor() as cur:
            # 按依赖顺序删除：runs -> jobs -> repos
            for run in self._runs:
                cur.execute(run.cleanup_sql(self.scm_schema))
            for job in self._jobs:
                cur.execute(job.cleanup_sql(self.scm_schema))
            for repo in self._repos:
                cur.execute(repo.cleanup_sql(self.scm_schema))
        
        self._runs.clear()
        self._jobs.clear()
        self._repos.clear()


@contextmanager
def data_factory_context(
    conn: psycopg.Connection,
    scm_schema: str = "scm"
) -> Generator[DataFactory, None, None]:
    """
    测试数据上下文管理器
    
    自动在退出时清理创建的测试数据。
    
    使用示例:
        with data_factory_context(conn, "scm") as factory:
            repo = factory.create_repo()
            job = factory.create_job(repo.repo_id)
            # ... 测试代码 ...
        # 自动清理
    """
    factory = DataFactory(conn, scm_schema)
    try:
        yield factory
    finally:
        factory.cleanup()


# 为向后兼容保留别名
TestDataFactory = DataFactory
# 注意：不再使用 test_data_context 别名，因为 pytest 会尝试将其作为测试收集
# 使用 data_factory_context 代替


# ============ 状态断言 Helpers ============


class JobAssertions:
    """
    任务状态断言 Helper
    
    提供常用的任务状态验证方法。
    """
    
    def __init__(self, conn: psycopg.Connection, scm_schema: str = "scm"):
        self.conn = conn
        self.scm_schema = scm_schema
    
    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """获取任务当前状态"""
        with self.conn.cursor() as cur:
            cur.execute(f"""
                SELECT status, locked_by, locked_at, attempts, last_error, not_before
                FROM {self.scm_schema}.sync_jobs
                WHERE job_id = %s
            """, (job_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Job {job_id} not found")
            return {
                "status": row[0],
                "locked_by": row[1],
                "locked_at": row[2],
                "attempts": row[3],
                "last_error": row[4],
                "not_before": row[5],
            }
    
    def assert_status(self, job_id: str, expected_status: str, msg: str = None):
        """断言任务状态"""
        state = self.get_job_status(job_id)
        actual = state["status"]
        assert actual == expected_status, (
            msg or f"Expected job {job_id} status to be '{expected_status}', got '{actual}'"
        )
    
    def assert_is_pending(self, job_id: str, msg: str = None):
        """断言任务为 pending 状态"""
        self.assert_status(job_id, "pending", msg)
    
    def assert_is_running(self, job_id: str, msg: str = None):
        """断言任务为 running 状态"""
        self.assert_status(job_id, "running", msg)
    
    def assert_is_failed(self, job_id: str, msg: str = None):
        """断言任务为 failed 状态"""
        self.assert_status(job_id, "failed", msg)
    
    def assert_is_dead(self, job_id: str, msg: str = None):
        """断言任务为 dead 状态"""
        self.assert_status(job_id, "dead", msg)
    
    def assert_is_done(self, job_id: str, msg: str = None):
        """断言任务为 done 状态"""
        self.assert_status(job_id, "done", msg)
    
    def assert_unlocked(self, job_id: str, msg: str = None):
        """断言任务未被锁定"""
        state = self.get_job_status(job_id)
        assert state["locked_by"] is None, (
            msg or f"Expected job {job_id} to be unlocked, but locked_by={state['locked_by']}"
        )
    
    def assert_locked_by(self, job_id: str, worker_id: str, msg: str = None):
        """断言任务被指定 worker 锁定"""
        state = self.get_job_status(job_id)
        assert state["locked_by"] == worker_id, (
            msg or f"Expected job {job_id} to be locked by '{worker_id}', got '{state['locked_by']}'"
        )
    
    def assert_error_contains(self, job_id: str, substring: str, msg: str = None):
        """断言任务 last_error 包含指定子串"""
        state = self.get_job_status(job_id)
        last_error = state["last_error"] or ""
        assert substring.lower() in last_error.lower(), (
            msg or f"Expected job {job_id} last_error to contain '{substring}', got '{last_error}'"
        )
    
    def assert_attempts(self, job_id: str, expected: int, msg: str = None):
        """断言任务尝试次数"""
        state = self.get_job_status(job_id)
        actual = state["attempts"]
        assert actual == expected, (
            msg or f"Expected job {job_id} attempts to be {expected}, got {actual}"
        )
    
    def assert_has_backoff(self, job_id: str, min_seconds: int = 0, msg: str = None):
        """断言任务设置了退避时间（not_before 在未来）"""
        state = self.get_job_status(job_id)
        not_before = state["not_before"]
        if not_before is None:
            if min_seconds == 0:
                return  # min_seconds=0 且 not_before 为 None 是合法的
            assert False, (
                msg or f"Expected job {job_id} to have backoff, but not_before is None"
            )
        
        now = datetime.now(timezone.utc)
        min_expected = now + timedelta(seconds=min_seconds - 5)  # 允许 5 秒误差
        assert not_before >= min_expected, (
            msg or f"Expected job {job_id} not_before to be at least {min_seconds}s from now"
        )


class RunAssertions:
    """
    运行状态断言 Helper
    """
    
    def __init__(self, conn: psycopg.Connection, scm_schema: str = "scm"):
        self.conn = conn
        self.scm_schema = scm_schema
    
    def get_run_status(self, run_id: str) -> Dict[str, Any]:
        """获取运行当前状态"""
        with self.conn.cursor() as cur:
            cur.execute(f"""
                SELECT status, started_at, finished_at, error_summary_json
                FROM {self.scm_schema}.sync_runs
                WHERE run_id = %s
            """, (run_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Run {run_id} not found")
            return {
                "status": row[0],
                "started_at": row[1],
                "finished_at": row[2],
                "error_summary_json": row[3],
            }
    
    def assert_status(self, run_id: str, expected_status: str, msg: str = None):
        """断言运行状态"""
        state = self.get_run_status(run_id)
        actual = state["status"]
        assert actual == expected_status, (
            msg or f"Expected run {run_id} status to be '{expected_status}', got '{actual}'"
        )
    
    def assert_is_failed(self, run_id: str, msg: str = None):
        """断言运行为 failed 状态"""
        self.assert_status(run_id, "failed", msg)
    
    def assert_finished(self, run_id: str, msg: str = None):
        """断言运行已完成（finished_at 不为 None）"""
        state = self.get_run_status(run_id)
        assert state["finished_at"] is not None, (
            msg or f"Expected run {run_id} to be finished, but finished_at is None"
        )


# ============ 敏感数据断言 Helpers ============


class SensitiveDataAssertions:
    """
    敏感数据脱敏断言 Helper
    
    验证敏感信息（tokens, passwords, auth headers）已被正确脱敏。
    """
    
    # 常见的敏感信息模式
    SENSITIVE_PATTERNS = [
        # GitLab Personal Access Token (glpat-xxx)
        r"glpat-[a-zA-Z0-9_-]{10,}",
        # GitLab Project Token (glptt-xxx)
        r"glptt-[a-zA-Z0-9_-]{10,}",
        # Bearer Token
        r"Bearer\s+[a-zA-Z0-9._-]{20,}",
        # JWT Token (eyJxxx.xxx.xxx)
        r"eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*",
        # Basic Auth Base64
        r"Basic\s+[A-Za-z0-9+/=]{20,}",
    ]
    
    # 应该被脱敏替换的关键词
    REDACTION_MARKERS = ["[REDACTED]", "[GITLAB_TOKEN]", "[TOKEN]", "****"]
    
    @classmethod
    def assert_no_sensitive_data(cls, text: str, msg: str = None):
        """
        断言文本中不包含敏感数据
        
        检查常见的 token 和凭证模式。
        """
        import re
        
        for pattern in cls.SENSITIVE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                assert False, (
                    msg or f"Found sensitive data pattern '{pattern}' in text: {match.group()[:20]}..."
                )
    
    @classmethod
    def assert_is_redacted(cls, text: str, msg: str = None):
        """
        断言文本已被脱敏（包含脱敏标记）
        """
        has_marker = any(marker in text for marker in cls.REDACTION_MARKERS)
        assert has_marker, (
            msg or f"Expected text to contain redaction marker, got: {text[:100]}..."
        )
    
    @classmethod
    def assert_token_redacted(
        cls,
        original_token: str,
        processed_text: str,
        msg: str = None
    ):
        """
        断言原始 token 已从处理后的文本中被脱敏
        """
        assert original_token not in processed_text, (
            msg or f"Token '{original_token[:10]}...' should be redacted but found in text"
        )


# ============ Mock Helpers ============


class MockHelpers:
    """
    常用 Mock 设置 Helper
    """
    
    @staticmethod
    def make_claim_result(
        job_id: str = "test-job",
        repo_id: int = 1,
        job_type: str = "gitlab_commits",
        mode: str = "incremental",
        attempts: int = 1,
        max_attempts: int = 3,
        lease_seconds: int = 300,
        payload: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """创建 claim 返回结果"""
        return {
            "job_id": job_id,
            "repo_id": repo_id,
            "job_type": job_type,
            "mode": mode,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "lease_seconds": lease_seconds,
            "payload": payload or {},
        }
    
    @staticmethod
    def make_success_result(
        run_id: str = "run-123",
        synced_count: int = 10,
        **extra
    ) -> Dict[str, Any]:
        """创建成功的执行结果"""
        result = {
            "success": True,
            "run_id": run_id,
            "counts": {"synced_count": synced_count},
        }
        result.update(extra)
        return result
    
    @staticmethod
    def make_failure_result(
        error: str = "Test error",
        error_category: str = "unknown",
        retry_after: Optional[int] = None,
        **extra
    ) -> Dict[str, Any]:
        """创建失败的执行结果"""
        result = {
            "success": False,
            "error": error,
            "error_category": error_category,
            "counts": {"synced_count": 0},
        }
        if retry_after is not None:
            result["retry_after"] = retry_after
        result.update(extra)
        return result
    
    @staticmethod
    def make_skipped_result(
        message: str = "Already running on another worker",
        **extra
    ) -> Dict[str, Any]:
        """创建 skipped 的执行结果"""
        result = {
            "success": True,
            "skipped": True,
            "message": message,
        }
        result.update(extra)
        return result
    
    @staticmethod
    @contextmanager
    def mock_process_one_job_deps() -> Generator[Dict[str, MagicMock], None, None]:
        """
        Mock process_one_job 的所有依赖
        
        返回包含所有 mock 的字典。
        
        使用示例:
            with MockHelpers.mock_process_one_job_deps() as mocks:
                mocks["claim"].return_value = MockHelpers.make_claim_result()
                mocks["execute"].return_value = MockHelpers.make_success_result()
                
                from scm_sync_worker import process_one_job
                result = process_one_job(worker_id="test")
                
                mocks["ack"].assert_called_once()
        """
        with patch('scm_sync_worker.claim') as mock_claim, \
             patch('scm_sync_worker.ack') as mock_ack, \
             patch('scm_sync_worker.fail_retry') as mock_fail_retry, \
             patch('scm_sync_worker.mark_dead') as mock_mark_dead, \
             patch('scm_sync_worker.requeue_without_penalty') as mock_requeue, \
             patch('scm_sync_worker.renew_lease') as mock_renew, \
             patch('scm_sync_worker.execute_sync_job') as mock_execute:
            
            # 设置默认返回值
            mock_renew.return_value = True
            mock_ack.return_value = True
            mock_fail_retry.return_value = True
            mock_mark_dead.return_value = True
            mock_requeue.return_value = True
            
            yield {
                "claim": mock_claim,
                "ack": mock_ack,
                "fail_retry": mock_fail_retry,
                "mark_dead": mock_mark_dead,
                "requeue": mock_requeue,
                "renew_lease": mock_renew,
                "execute": mock_execute,
            }


# ============ 错误分类断言 Helpers ============


class ErrorCategoryAssertions:
    """
    错误分类断言 Helper
    
    验证错误被正确分类为临时性/永久性错误。
    """
    
    # 永久性错误类别
    PERMANENT_CATEGORIES = {
        "auth_error", "auth_missing", "auth_invalid",
        "repo_not_found", "repo_type_unknown", "permission_denied",
    }
    
    # 临时性错误类别
    TRANSIENT_CATEGORIES = {
        "rate_limit", "timeout", "network", "server_error",
        "connection", "lease_lost",
    }
    
    @classmethod
    def assert_is_permanent(cls, error_category: str, msg: str = None):
        """断言错误类别为永久性错误"""
        assert error_category in cls.PERMANENT_CATEGORIES, (
            msg or f"Expected '{error_category}' to be permanent error"
        )
    
    @classmethod
    def assert_is_transient(cls, error_category: str, msg: str = None):
        """断言错误类别为临时性错误"""
        assert error_category in cls.TRANSIENT_CATEGORIES, (
            msg or f"Expected '{error_category}' to be transient error"
        )
    
    @classmethod
    def assert_triggers_mark_dead(cls, error_category: str, msg: str = None):
        """断言该错误类别应触发 mark_dead"""
        cls.assert_is_permanent(error_category, msg)
    
    @classmethod
    def assert_triggers_fail_retry(cls, error_category: str, msg: str = None):
        """断言该错误类别应触发 fail_retry"""
        # 非永久性错误都应该 fail_retry
        assert error_category not in cls.PERMANENT_CATEGORIES, (
            msg or f"Expected '{error_category}' to trigger fail_retry, not mark_dead"
        )


# ============ 退避时间断言 Helpers ============


class BackoffAssertions:
    """
    退避时间断言 Helper
    """
    
    # 默认退避配置（与 scm_sync_errors 模块保持一致）
    DEFAULT_BACKOFFS = {
        "rate_limit": 120,
        "timeout": 30,
        "server_error": 90,
        "network": 60,
        "connection": 45,
        "lease_lost": 0,
        "default": 60,
    }
    
    @classmethod
    def get_expected_backoff(cls, error_category: str) -> int:
        """获取期望的退避时间"""
        return cls.DEFAULT_BACKOFFS.get(error_category, cls.DEFAULT_BACKOFFS["default"])
    
    @classmethod
    def assert_backoff_seconds(
        cls,
        actual: int,
        error_category: str,
        msg: str = None,
        tolerance: int = 5
    ):
        """断言退避时间符合预期"""
        expected = cls.get_expected_backoff(error_category)
        assert abs(actual - expected) <= tolerance, (
            msg or f"Expected backoff for '{error_category}' to be ~{expected}s, got {actual}s"
        )
    
    @classmethod
    def assert_fail_retry_called_with_backoff(
        cls,
        mock_fail_retry: MagicMock,
        error_category: str,
        msg: str = None
    ):
        """断言 fail_retry 使用了正确的 backoff_seconds"""
        mock_fail_retry.assert_called_once()
        call_kwargs = mock_fail_retry.call_args
        actual_backoff = call_kwargs[1].get("backoff_seconds", 0)
        expected_backoff = cls.get_expected_backoff(error_category)
        
        assert actual_backoff == expected_backoff, (
            msg or f"Expected fail_retry backoff={expected_backoff}s for {error_category}, got {actual_backoff}s"
        )


# ============ 熔断器断言 Helpers ============


class CircuitBreakerAssertions:
    """
    熔断器断言 Helper
    """
    
    @staticmethod
    def assert_state(controller, expected_state: str, msg: str = None):
        """断言熔断器状态"""
        actual = controller.state.value if hasattr(controller.state, 'value') else str(controller.state)
        assert actual == expected_state, (
            msg or f"Expected circuit breaker state '{expected_state}', got '{actual}'"
        )
    
    @staticmethod
    def assert_is_closed(controller, msg: str = None):
        """断言熔断器为 CLOSED 状态"""
        CircuitBreakerAssertions.assert_state(controller, "closed", msg)
    
    @staticmethod
    def assert_is_open(controller, msg: str = None):
        """断言熔断器为 OPEN 状态"""
        CircuitBreakerAssertions.assert_state(controller, "open", msg)
    
    @staticmethod
    def assert_is_half_open(controller, msg: str = None):
        """断言熔断器为 HALF_OPEN 状态"""
        CircuitBreakerAssertions.assert_state(controller, "half_open", msg)
    
    @staticmethod
    def assert_allows_sync(decision, msg: str = None):
        """断言决策允许同步"""
        assert decision.allow_sync is True, (
            msg or f"Expected decision to allow sync, got allow_sync={decision.allow_sync}"
        )
    
    @staticmethod
    def assert_blocks_sync(decision, msg: str = None):
        """断言决策阻止同步"""
        assert decision.allow_sync is False, (
            msg or f"Expected decision to block sync, got allow_sync={decision.allow_sync}"
        )
    
    @staticmethod
    def assert_degraded(decision, msg: str = None):
        """断言决策为降级模式"""
        assert decision.is_backfill_only is True, (
            msg or f"Expected decision to be degraded (backfill_only), got is_backfill_only={decision.is_backfill_only}"
        )
