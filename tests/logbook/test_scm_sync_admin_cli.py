# -*- coding: utf-8 -*-
"""
SCM Sync Admin CLI 单元测试

测试 engram-scm-sync admin 命令的所有子命令:
- jobs: list/reset-dead/mark-dead
- locks: list/force-release/list-expired
- pauses: set/unset/list
- cursors: list/get/set/delete
- rate-limit: buckets list/pause/unpause

覆盖:
1. CLI 参数解析
2. 数据库操作
3. 敏感信息脱敏
4. JSON 输出格式
5. 错误处理
"""

import json
import subprocess
import sys
from io import StringIO
from unittest.mock import patch

import pytest

from engram.logbook.cli.scm_sync import (
    _redact_cursor_info,
    _redact_job_info,
    _redact_lock_info,
    _redact_pause_info,
    admin_main,
)


class TestRedactFunctions:
    """测试敏感信息脱敏函数"""

    def test_redact_job_info_payload_json(self):
        """测试 job 信息脱敏 - payload_json"""
        job = {
            "job_id": "job-123",
            "repo_id": 1,
            "payload_json": {"private_token": "glpat-secret123", "url": "/api/v4"},
        }
        redacted = _redact_job_info(job)
        # private_token 应该被脱敏
        assert "secret123" not in str(redacted.get("payload_json", {}))

    def test_redact_job_info_last_error(self):
        """测试 job 信息脱敏 - last_error"""
        job = {
            "job_id": "job-123",
            "last_error": "PRIVATE-TOKEN: glpat-secret123 failed",
        }
        redacted = _redact_job_info(job)
        # 敏感信息应该被脱敏
        assert "secret123" not in redacted.get("last_error", "")

    def test_redact_lock_info(self):
        """测试锁信息脱敏"""
        lock = {
            "lock_id": 1,
            "locked_by": "worker-1:Bearer token123",
        }
        redacted = _redact_lock_info(lock)
        # locked_by 中的 token 应该被脱敏
        assert "token123" not in str(redacted.get("locked_by", ""))

    def test_redact_pause_info_dict(self):
        """测试暂停信息脱敏 - 字典输入"""
        pause = {
            "repo_id": 1,
            "job_type": "commits",
            "reason": "PRIVATE-TOKEN: glpat-secret123 rate limited",
        }
        redacted = _redact_pause_info(pause)
        assert "secret123" not in redacted.get("reason", "")

    def test_redact_cursor_info(self):
        """测试游标信息脱敏"""
        cursor = {
            "key": "cursor:1:commits",
            "value_json": {"watermark": "2025-01-01", "token": "secret"},
        }
        redacted = _redact_cursor_info(cursor)
        # 敏感 key 应该被脱敏
        assert redacted is not None


class TestAdminJobsCommand:
    """测试 admin jobs 子命令"""

    def test_jobs_list_dead_empty(self, migrated_db, monkeypatch):
        """jobs list --status dead 空列表"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        # 捕获输出
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "jobs", "list", "--status", "dead"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert "jobs" in data
        assert "count" in data

    def test_jobs_list_with_data(self, migrated_db, monkeypatch):
        """jobs list 返回任务数据"""
        from engram.logbook.scm_db import enqueue_sync_job, get_conn, upsert_repo

        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        # 创建测试数据
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/admin-jobs")
            enqueue_sync_job(conn, repo_id=repo_id, job_type="commits")
            conn.commit()

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "jobs", "list", "--status", "pending"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert data["count"] >= 1

    def test_jobs_reset_dead_dry_run(self, migrated_db, monkeypatch):
        """jobs reset-dead --dry-run"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "jobs", "reset-dead", "--dry-run"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert data.get("dry_run") is True

    def test_jobs_mark_dead(self, migrated_db, monkeypatch):
        """jobs mark-dead"""
        from engram.logbook.scm_db import enqueue_sync_job, get_conn, upsert_repo

        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        # 创建测试数据
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/admin-mark-dead")
            job_id = enqueue_sync_job(conn, repo_id=repo_id, job_type="commits")
            conn.commit()

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                ["--json", "jobs", "mark-dead", "--job-id", job_id, "--reason", "test"]
            )

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert data.get("success") is True


class TestAdminLocksCommand:
    """测试 admin locks 子命令"""

    def test_locks_list_empty(self, migrated_db, monkeypatch):
        """locks list 空列表"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "locks", "list"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert "locks" in data
        assert "count" in data

    def test_locks_list_expired_empty(self, migrated_db, monkeypatch):
        """locks list-expired 空列表"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "locks", "list-expired"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert "expired_locks" in data

    def test_locks_force_release_not_found(self, migrated_db, monkeypatch):
        """locks force-release 不存在的锁"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "locks", "force-release", "--lock-id", "99999"])

        # 应该返回失败（锁不存在）
        assert result == 1
        output = captured_output.getvalue()
        data = json.loads(output)
        assert data.get("success") is False


class TestAdminPausesCommand:
    """测试 admin pauses 子命令"""

    def test_pauses_list_empty(self, migrated_db, monkeypatch):
        """pauses list 空列表"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "pauses", "list"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert "pauses" in data

    def test_pauses_set_and_unset(self, migrated_db, monkeypatch):
        """pauses set 和 unset"""
        from engram.logbook.scm_db import get_conn, upsert_repo

        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        # 创建测试仓库
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/admin-pauses")
            conn.commit()

        # 设置暂停
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "pauses",
                    "set",
                    "--repo-id",
                    str(repo_id),
                    "--job-type",
                    "commits",
                    "--duration",
                    "3600",
                    "--reason",
                    "test_pause",
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("success") is True

        # 取消暂停
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "pauses",
                    "unset",
                    "--repo-id",
                    str(repo_id),
                    "--job-type",
                    "commits",
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("success") is True


class TestAdminCursorsCommand:
    """测试 admin cursors 子命令"""

    def test_cursors_list_empty(self, migrated_db, monkeypatch):
        """cursors list 空列表"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "cursors", "list"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert "cursors" in data

    def test_cursors_get_not_found(self, migrated_db, monkeypatch):
        """cursors get 不存在的游标"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "cursors",
                    "get",
                    "--repo-id",
                    "99999",
                    "--job-type",
                    "commits",
                ]
            )

        # 不存在返回非零
        assert result == 1
        data = json.loads(captured_output.getvalue())
        assert data.get("found") is False

    def test_cursors_set_and_delete(self, migrated_db, monkeypatch):
        """cursors set 和 delete"""
        from engram.logbook.scm_db import get_conn, upsert_repo

        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        # 创建测试仓库
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/admin-cursors")
            conn.commit()

        # 设置游标
        cursor_value = '{"watermark": "2025-01-01T00:00:00Z"}'
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "cursors",
                    "set",
                    "--repo-id",
                    str(repo_id),
                    "--job-type",
                    "commits",
                    "--value",
                    cursor_value,
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("success") is True

        # 获取游标验证
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "cursors",
                    "get",
                    "--repo-id",
                    str(repo_id),
                    "--job-type",
                    "commits",
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("found") is True

        # 删除游标
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "cursors",
                    "delete",
                    "--repo-id",
                    str(repo_id),
                    "--job-type",
                    "commits",
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("success") is True

    def test_cursors_set_invalid_json(self, migrated_db, monkeypatch):
        """cursors set 无效 JSON"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "cursors",
                    "set",
                    "--repo-id",
                    "1",
                    "--job-type",
                    "commits",
                    "--value",
                    "invalid json",
                ]
            )

        assert result == 1
        data = json.loads(captured_output.getvalue())
        assert "error" in data


class TestAdminRateLimitCommand:
    """测试 admin rate-limit 子命令"""

    def test_buckets_list_empty(self, migrated_db, monkeypatch):
        """rate-limit buckets list 空列表"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "rate-limit", "buckets", "list"])

        assert result == 0
        output = captured_output.getvalue()
        data = json.loads(output)
        assert "buckets" in data

    def test_buckets_pause_and_unpause(self, migrated_db, monkeypatch):
        """rate-limit buckets pause 和 unpause"""
        dsn = migrated_db["dsn"]
        monkeypatch.setenv("LOGBOOK_DSN", dsn)

        instance_key = "test.gitlab.com"

        # 暂停桶
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "rate-limit",
                    "buckets",
                    "pause",
                    "--instance-key",
                    instance_key,
                    "--duration",
                    "300",
                    "--reason",
                    "test_pause",
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("success") is True

        # 取消暂停
        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(
                [
                    "--json",
                    "rate-limit",
                    "buckets",
                    "unpause",
                    "--instance-key",
                    instance_key,
                ]
            )

        assert result == 0
        data = json.loads(captured_output.getvalue())
        assert data.get("success") is True


class TestAdminCLIHelp:
    """测试 admin CLI 帮助信息"""

    def test_admin_help(self, monkeypatch):
        """admin --help"""
        monkeypatch.delenv("LOGBOOK_DSN", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            admin_main(["--help"])
        assert exc_info.value.code == 0

    def test_admin_no_command(self, monkeypatch):
        """admin 无命令时显示帮助"""
        monkeypatch.delenv("LOGBOOK_DSN", raising=False)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main([])

        assert result == 0

    def test_admin_no_dsn_error(self, monkeypatch):
        """admin 无 DSN 时返回错误"""
        monkeypatch.delenv("LOGBOOK_DSN", raising=False)
        monkeypatch.delenv("POSTGRES_DSN", raising=False)

        captured_output = StringIO()
        with patch("sys.stdout", captured_output):
            result = admin_main(["--json", "jobs", "list"])

        assert result == 1
        data = json.loads(captured_output.getvalue())
        assert "error" in data


class TestAdminCLIIntegration:
    """Admin CLI 集成测试（通过 subprocess 调用）"""

    def test_admin_help_via_subprocess(self):
        """通过 subprocess 调用 admin --help"""
        result = subprocess.run(
            [sys.executable, "-m", "engram.logbook.cli.scm_sync", "admin", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "jobs" in result.stdout.lower() or "locks" in result.stdout.lower()

    def test_admin_jobs_list_via_subprocess(self, migrated_db):
        """通过 subprocess 调用 admin jobs list"""
        dsn = migrated_db["dsn"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm_sync",
                "admin",
                "--dsn",
                dsn,
                "--json",
                "jobs",
                "list",
                "--status",
                "dead",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "jobs" in data

    def test_admin_pauses_list_via_subprocess(self, migrated_db):
        """通过 subprocess 调用 admin pauses list"""
        dsn = migrated_db["dsn"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.logbook.cli.scm_sync",
                "admin",
                "--dsn",
                dsn,
                "--json",
                "pauses",
                "list",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "pauses" in data


class TestScmDbAdminFunctions:
    """测试 scm_db 中新增的 admin 辅助函数"""

    def test_unset_repo_job_pause(self, migrated_db):
        """测试 unset_repo_job_pause"""
        from engram.logbook.scm_db import (
            get_conn,
            get_repo_job_pause,
            set_repo_job_pause,
            unset_repo_job_pause,
            upsert_repo,
        )

        dsn = migrated_db["dsn"]
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/unset-pause")
            conn.commit()

            # 设置暂停
            set_repo_job_pause(
                conn,
                repo_id=repo_id,
                job_type="commits",
                pause_duration_seconds=3600,
                reason="test",
            )
            conn.commit()

            # 验证暂停存在
            pause = get_repo_job_pause(conn, repo_id=repo_id, job_type="commits")
            assert pause is not None

            # 取消暂停
            result = unset_repo_job_pause(conn, repo_id=repo_id, job_type="commits")
            conn.commit()
            assert result is True

            # 验证暂停已删除
            pause = get_repo_job_pause(conn, repo_id=repo_id, job_type="commits")
            assert pause is None

    def test_set_and_delete_cursor_value(self, migrated_db):
        """测试 set_cursor_value 和 delete_cursor_value"""
        from engram.logbook.scm_db import (
            delete_cursor_value,
            get_conn,
            get_cursor_value,
            set_cursor_value,
            upsert_repo,
        )

        dsn = migrated_db["dsn"]
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/cursor-ops")
            conn.commit()

            # 设置游标
            cursor_value = {"watermark": "2025-01-01T00:00:00Z", "run_id": "test"}
            result = set_cursor_value(conn, repo_id, "commits", cursor_value)
            conn.commit()
            assert result is True

            # 获取游标
            cursor = get_cursor_value(conn, repo_id, "commits")
            assert cursor is not None
            assert cursor["value"]["watermark"] == "2025-01-01T00:00:00Z"

            # 删除游标
            result = delete_cursor_value(conn, repo_id, "commits")
            conn.commit()
            assert result is True

            # 验证游标已删除
            cursor = get_cursor_value(conn, repo_id, "commits")
            assert cursor is None

    def test_list_rate_limit_buckets(self, migrated_db):
        """测试 list_rate_limit_buckets"""
        from engram.logbook.scm_db import get_conn, list_rate_limit_buckets, pause_rate_limit_bucket

        dsn = migrated_db["dsn"]
        with get_conn(dsn) as conn:
            # 创建一个桶
            pause_rate_limit_bucket(conn, "test.gitlab.com", 300, reason="test")
            conn.commit()

            # 列出桶
            buckets = list_rate_limit_buckets(conn)
            assert len(buckets) >= 1

            # 验证桶信息
            test_bucket = next((b for b in buckets if b["instance_key"] == "test.gitlab.com"), None)
            assert test_bucket is not None
            assert test_bucket["is_paused"] is True

    def test_reset_dead_jobs(self, migrated_db):
        """测试 reset_dead_jobs"""
        from engram.logbook.scm_db import (
            enqueue_sync_job,
            get_conn,
            list_jobs_by_status,
            mark_job_dead,
            reset_dead_jobs,
            upsert_repo,
        )

        dsn = migrated_db["dsn"]
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/reset-dead")
            job_id = enqueue_sync_job(conn, repo_id=repo_id, job_type="commits")
            conn.commit()

            # 标记为 dead
            mark_job_dead(conn, job_id, reason="test")
            conn.commit()

            # 验证任务为 dead
            dead_jobs = list_jobs_by_status(conn, "dead")
            assert any(j["job_id"] == int(job_id) for j in dead_jobs)

            # 重置
            reset_result = reset_dead_jobs(conn, job_ids=[job_id])
            conn.commit()
            assert len(reset_result) >= 1

            # 验证任务变为 pending
            pending_jobs = list_jobs_by_status(conn, "pending")
            assert any(j["job_id"] == int(job_id) for j in pending_jobs)

    def test_list_jobs_by_status(self, migrated_db):
        """测试 list_jobs_by_status"""
        from engram.logbook.scm_db import (
            enqueue_sync_job,
            get_conn,
            list_jobs_by_status,
            upsert_repo,
        )

        dsn = migrated_db["dsn"]
        with get_conn(dsn) as conn:
            repo_id = upsert_repo(conn, "git", "https://gitlab.com/test/list-by-status")
            enqueue_sync_job(conn, repo_id=repo_id, job_type="commits")
            enqueue_sync_job(conn, repo_id=repo_id, job_type="mrs")
            conn.commit()

            # 按状态列出
            jobs = list_jobs_by_status(conn, "pending")
            assert len(jobs) >= 2

            # 按 repo_id 过滤
            jobs = list_jobs_by_status(conn, "pending", repo_id=repo_id)
            assert len(jobs) >= 2
            assert all(j["repo_id"] == repo_id for j in jobs)

            # 按 job_type 过滤
            jobs = list_jobs_by_status(conn, "pending", job_type="commits")
            assert all(j["job_type"] == "commits" for j in jobs)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
