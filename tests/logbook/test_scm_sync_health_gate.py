# -*- coding: utf-8 -*-
"""
test_scm_sync_health_gate.py - SCM 同步健康检查不变量测试

测试内容:
- check_invariants() 函数的各个检查项
- 健康检查结果输出结构
- CLI --health 选项的集成测试

测试策略:
- 使用 mock db_api 注入测试数据
- 验证每个检查项的触发条件和输出 schema
- 验证 remediation hint 存在
"""

import json
import time
from unittest.mock import MagicMock

from engram.logbook.scm_sync_status import (
    HealthCheckResult,
    InvariantSeverity,
    InvariantViolation,
    check_invariants,
    format_health_check_output,
)

# ============ 数据类测试 ============


class TestInvariantViolation:
    """测试 InvariantViolation 数据类"""

    def test_to_dict_schema(self):
        """验证 to_dict 输出 schema"""
        violation = InvariantViolation(
            check_id="test_check",
            name="测试检查项",
            severity=InvariantSeverity.WARNING,
            count=5,
            description="测试描述",
            remediation_hint="测试修复建议",
            details=[{"key": "value"}],
        )

        result = violation.to_dict()

        # 验证必需字段
        assert result["check_id"] == "test_check"
        assert result["name"] == "测试检查项"
        assert result["severity"] == "warning"
        assert result["count"] == 5
        assert result["description"] == "测试描述"
        assert result["remediation_hint"] == "测试修复建议"
        assert result["details"] == [{"key": "value"}]

    def test_severity_values(self):
        """验证 severity 枚举值"""
        assert InvariantSeverity.CRITICAL.value == "critical"
        assert InvariantSeverity.WARNING.value == "warning"
        assert InvariantSeverity.INFO.value == "info"


class TestHealthCheckResult:
    """测试 HealthCheckResult 数据类"""

    def test_exit_code_healthy(self):
        """验证健康时退出码为 0"""
        result = HealthCheckResult(
            healthy=True,
            violations=[],
            total_checks=5,
            passed_checks=5,
            failed_checks=0,
        )
        assert result.exit_code == 0

    def test_exit_code_warning(self):
        """验证有 warning 违规时退出码为 1"""
        warning_violation = InvariantViolation(
            check_id="test",
            name="Test",
            severity=InvariantSeverity.WARNING,
            count=1,
            description="desc",
            remediation_hint="hint",
        )
        result = HealthCheckResult(
            healthy=True,
            violations=[warning_violation],
            total_checks=5,
            passed_checks=4,
            failed_checks=1,
        )
        assert result.exit_code == 1

    def test_exit_code_critical(self):
        """验证有 critical 违规时退出码为 2"""
        critical_violation = InvariantViolation(
            check_id="test",
            name="Test",
            severity=InvariantSeverity.CRITICAL,
            count=1,
            description="desc",
            remediation_hint="hint",
        )
        result = HealthCheckResult(
            healthy=False,
            violations=[critical_violation],
            total_checks=5,
            passed_checks=4,
            failed_checks=1,
        )
        assert result.exit_code == 2

    def test_exit_code_critical_overrides_warning(self):
        """验证 critical 优先级高于 warning"""
        violations = [
            InvariantViolation(
                check_id="warn",
                name="Warning",
                severity=InvariantSeverity.WARNING,
                count=1,
                description="desc",
                remediation_hint="hint",
            ),
            InvariantViolation(
                check_id="crit",
                name="Critical",
                severity=InvariantSeverity.CRITICAL,
                count=1,
                description="desc",
                remediation_hint="hint",
            ),
        ]
        result = HealthCheckResult(
            healthy=False,
            violations=violations,
            total_checks=5,
            passed_checks=3,
            failed_checks=2,
        )
        assert result.exit_code == 2

    def test_to_dict_schema(self):
        """验证 to_dict 输出 schema"""
        result = HealthCheckResult(
            healthy=True,
            violations=[],
            checked_at=1700000000.0,
            total_checks=5,
            passed_checks=5,
            failed_checks=0,
        )

        output = result.to_dict()

        # 验证必需字段
        assert "healthy" in output
        assert "exit_code" in output
        assert "checked_at" in output
        assert "total_checks" in output
        assert "passed_checks" in output
        assert "failed_checks" in output
        assert "violations" in output

        assert output["healthy"] is True
        assert output["exit_code"] == 0
        assert isinstance(output["violations"], list)


# ============ check_invariants 函数测试 ============


class TestCheckInvariantsExpiredRunningJobs:
    """测试 expired_running_jobs 检查项"""

    def test_no_expired_jobs_passes(self):
        """无过期任务时检查通过"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        assert result.healthy is True
        assert result.passed_checks == 5
        assert not any(v.check_id == "expired_running_jobs" for v in result.violations)

    def test_expired_jobs_triggers_critical(self):
        """有过期任务时触发 critical 违规"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 3
        mock_db.list_expired_running_jobs.return_value = [
            {"job_id": 1, "repo_id": 100, "job_type": "gitlab_commits"},
            {"job_id": 2, "repo_id": 101, "job_type": "gitlab_mrs"},
            {"job_id": 3, "repo_id": 102, "job_type": "gitlab_commits"},
        ]
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=True)

        assert result.healthy is False
        assert result.exit_code == 2

        violation = next(v for v in result.violations if v.check_id == "expired_running_jobs")
        assert violation.severity == InvariantSeverity.CRITICAL
        assert violation.count == 3
        assert "reaper" in violation.remediation_hint.lower()
        assert len(violation.details) == 3

    def test_expired_jobs_remediation_hint_format(self):
        """验证 expired_running_jobs 的 remediation_hint 格式"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 1
        mock_db.list_expired_running_jobs.return_value = []
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        violation = next(v for v in result.violations if v.check_id == "expired_running_jobs")
        # 验证 remediation_hint 包含建议命令
        assert "engram-scm-sync reaper" in violation.remediation_hint


class TestCheckInvariantsOrphanLocks:
    """测试 orphan_locks 检查项"""

    def test_no_orphan_locks_passes(self):
        """无孤立锁时检查通过"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        assert not any(v.check_id == "orphan_locks" for v in result.violations)

    def test_orphan_locks_triggers_warning(self):
        """有孤立锁时触发 warning 违规"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 2
        mock_db.list_orphan_locks.return_value = [
            {"lock_id": 1, "repo_id": 100, "job_type": "gitlab_commits"},
            {"lock_id": 2, "repo_id": 101, "job_type": "gitlab_mrs"},
        ]
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=True)

        # orphan_locks 是 warning，不影响 healthy
        assert result.healthy is True
        assert result.exit_code == 1

        violation = next(v for v in result.violations if v.check_id == "orphan_locks")
        assert violation.severity == InvariantSeverity.WARNING
        assert violation.count == 2
        assert "force-release" in violation.remediation_hint.lower()


class TestCheckInvariantsGitlabJobsMissingDimensions:
    """测试 gitlab_jobs_missing_dimensions 检查项"""

    def test_no_missing_dimensions_passes(self):
        """无缺失维度时检查通过"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        assert not any(v.check_id == "gitlab_jobs_missing_dimensions" for v in result.violations)

    def test_missing_dimensions_triggers_warning(self):
        """有缺失维度时触发 warning 违规"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 5
        mock_db.list_gitlab_jobs_missing_dimensions.return_value = [
            {
                "job_id": 1,
                "repo_id": 100,
                "job_type": "gitlab_commits",
                "gitlab_instance": None,
                "tenant_id": "",
            },
        ]
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=True)

        violation = next(
            v for v in result.violations if v.check_id == "gitlab_jobs_missing_dimensions"
        )
        assert violation.severity == InvariantSeverity.WARNING
        assert violation.count == 5
        assert (
            "scheduler" in violation.remediation_hint.lower()
            or "sql" in violation.remediation_hint.lower()
        )


class TestCheckInvariantsExpiredPauses:
    """测试 expired_pauses 检查项"""

    def test_no_expired_pauses_passes(self):
        """无过期暂停时检查通过"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        assert not any(v.check_id == "expired_pauses" for v in result.violations)

    def test_expired_pauses_triggers_info(self):
        """有过期暂停时触发 info 违规"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 10
        mock_db.list_expired_pauses.return_value = [
            {"repo_id": 100, "job_type": "commits", "expired_seconds_ago": 3600.0},
        ]
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=True)

        # info 级别不影响 healthy 和 exit_code
        assert result.healthy is True
        assert result.exit_code == 0

        violation = next(v for v in result.violations if v.check_id == "expired_pauses")
        assert violation.severity == InvariantSeverity.INFO
        assert violation.count == 10


class TestCheckInvariantsCircuitBreakerInconsistencies:
    """测试 circuit_breaker_inconsistencies 检查项"""

    def test_no_inconsistencies_passes(self):
        """无不一致时检查通过"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        assert not any(v.check_id == "circuit_breaker_inconsistencies" for v in result.violations)

    def test_circuit_open_no_samples_triggers_warning(self):
        """熔断器 open 但无样本时触发 warning"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = [
            {
                "key": "test:global",
                "issue": "circuit_open_no_samples",
                "description": "熔断器处于 open 状态但没有样本数据",
                "state": "open",
                "failure_count": 0,
                "success_count": 0,
            }
        ]

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=True)

        violation = next(
            v for v in result.violations if v.check_id == "circuit_breaker_inconsistencies"
        )
        assert violation.severity == InvariantSeverity.WARNING
        assert violation.count == 1
        assert (
            "reset" in violation.remediation_hint.lower()
            or "check" in violation.remediation_hint.lower()
        )


# ============ 输出格式测试 ============


class TestFormatHealthCheckOutput:
    """测试健康检查输出格式"""

    def test_healthy_output_format(self):
        """验证健康状态的输出格式"""
        result = HealthCheckResult(
            healthy=True,
            violations=[],
            checked_at=1700000000.0,
            total_checks=5,
            passed_checks=5,
            failed_checks=0,
        )

        output = format_health_check_output(result)

        assert "健康" in output
        assert "5/5" in output or "5 通过" in output
        assert "所有检查项均通过" in output

    def test_unhealthy_output_format(self):
        """验证不健康状态的输出格式"""
        violations = [
            InvariantViolation(
                check_id="test_critical",
                name="测试 Critical",
                severity=InvariantSeverity.CRITICAL,
                count=3,
                description="测试描述",
                remediation_hint="运行 `engram-scm-sync reaper`",
            ),
        ]
        result = HealthCheckResult(
            healthy=False,
            violations=violations,
            checked_at=1700000000.0,
            total_checks=5,
            passed_checks=4,
            failed_checks=1,
        )

        output = format_health_check_output(result)

        assert "不健康" in output
        assert "CRITICAL" in output
        assert "测试 Critical" in output
        assert "数量: 3" in output
        assert "reaper" in output

    def test_verbose_output_includes_details(self):
        """验证 verbose 模式包含详情"""
        violations = [
            InvariantViolation(
                check_id="test",
                name="Test",
                severity=InvariantSeverity.WARNING,
                count=2,
                description="desc",
                remediation_hint="hint",
                details=[{"id": 1}, {"id": 2}],
            ),
        ]
        result = HealthCheckResult(
            healthy=True,
            violations=violations,
            total_checks=5,
            passed_checks=4,
            failed_checks=1,
        )

        output = format_health_check_output(result, verbose=True)

        assert "详情" in output


# ============ JSON 输出 Schema 测试 ============


class TestHealthCheckJsonSchema:
    """测试健康检查 JSON 输出 schema"""

    def test_json_output_schema(self):
        """验证 JSON 输出的完整 schema"""
        violations = [
            InvariantViolation(
                check_id="expired_running_jobs",
                name="过期的 Running 任务",
                severity=InvariantSeverity.CRITICAL,
                count=1,
                description="有 1 个 running 状态的任务租约已过期",
                remediation_hint="运行 `engram-scm-sync reaper --once`",
                details=[{"job_id": "1", "repo_id": 100, "job_type": "gitlab_commits"}],
            ),
        ]
        result = HealthCheckResult(
            healthy=False,
            violations=violations,
            checked_at=1700000000.0,
            total_checks=5,
            passed_checks=4,
            failed_checks=1,
        )

        output = result.to_dict()

        # 验证顶层 schema
        assert isinstance(output["healthy"], bool)
        assert isinstance(output["exit_code"], int)
        assert isinstance(output["checked_at"], float)
        assert isinstance(output["total_checks"], int)
        assert isinstance(output["passed_checks"], int)
        assert isinstance(output["failed_checks"], int)
        assert isinstance(output["violations"], list)

        # 验证 violation schema
        v = output["violations"][0]
        assert isinstance(v["check_id"], str)
        assert isinstance(v["name"], str)
        assert v["severity"] in ("critical", "warning", "info")
        assert isinstance(v["count"], int)
        assert isinstance(v["description"], str)
        assert isinstance(v["remediation_hint"], str)
        assert isinstance(v["details"], list)

    def test_json_serializable(self):
        """验证输出可以序列化为 JSON"""
        result = HealthCheckResult(
            healthy=True,
            violations=[],
            checked_at=time.time(),
            total_checks=5,
            passed_checks=5,
            failed_checks=0,
        )

        # 不应抛出异常
        json_str = json.dumps(result.to_dict())
        assert json_str is not None

        # 反序列化检查
        parsed = json.loads(json_str)
        assert parsed["healthy"] is True


# ============ 检查项覆盖完整性测试 ============


class TestAllChecksExecuted:
    """测试所有检查项都被执行"""

    def test_all_five_checks_executed(self):
        """验证执行了所有 5 个检查项"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        # 验证 total_checks = 5
        assert result.total_checks == 5
        assert result.passed_checks == 5
        assert result.failed_checks == 0

        # 验证所有 db 函数都被调用
        mock_db.count_expired_running_jobs.assert_called_once()
        mock_db.count_orphan_locks.assert_called_once()
        mock_db.count_gitlab_jobs_missing_dimensions.assert_called_once()
        mock_db.count_expired_pauses_affecting_scheduling.assert_called_once()
        mock_db.get_circuit_breaker_inconsistencies.assert_called_once()


class TestGraceSecondsParameter:
    """测试 grace_seconds 参数传递"""

    def test_grace_seconds_passed_to_db(self):
        """验证 grace_seconds 正确传递给 db 函数"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        check_invariants(conn, db_api=mock_db, grace_seconds=120)

        # 验证 grace_seconds 被传递
        mock_db.count_expired_running_jobs.assert_called_once_with(conn, grace_seconds=120)


# ============ 真实数据库集成测试 ============


class TestCheckInvariantsIntegration:
    """集成测试 - 使用真实数据库 fixture"""

    def test_check_invariants_with_real_db(self, db_conn):
        """使用真实数据库连接测试 check_invariants"""
        # 不注入 mock，使用真实 db_api
        result = check_invariants(db_conn)

        # 验证返回类型
        assert isinstance(result, HealthCheckResult)
        assert isinstance(result.healthy, bool)
        assert result.total_checks == 5

        # 验证 violations 结构
        for v in result.violations:
            assert isinstance(v, InvariantViolation)
            assert v.check_id in (
                "expired_running_jobs",
                "orphan_locks",
                "gitlab_jobs_missing_dimensions",
                "expired_pauses",
                "circuit_breaker_inconsistencies",
            )
            assert v.remediation_hint  # 不为空

    def test_check_invariants_with_include_details(self, db_conn):
        """测试 include_details 参数"""
        result = check_invariants(db_conn, include_details=True)

        # 即使没有违规，也应该正常返回
        assert isinstance(result, HealthCheckResult)

    def test_check_invariants_json_serializable(self, db_conn):
        """验证真实数据库返回结果可序列化"""
        result = check_invariants(db_conn, include_details=True)

        # 不应抛出异常
        json_str = json.dumps(result.to_dict())
        assert json_str is not None


# ============ 边界情况测试 ============


class TestEdgeCases:
    """边界情况测试"""

    def test_all_checks_fail(self):
        """所有检查都失败的情况"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 10
        mock_db.list_expired_running_jobs.return_value = []
        mock_db.count_orphan_locks.return_value = 5
        mock_db.list_orphan_locks.return_value = []
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 3
        mock_db.list_gitlab_jobs_missing_dimensions.return_value = []
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 2
        mock_db.list_expired_pauses.return_value = []
        mock_db.get_circuit_breaker_inconsistencies.return_value = [{"issue": "test"}]

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        assert result.healthy is False
        assert result.failed_checks == 5
        assert len(result.violations) == 5

    def test_empty_details_when_not_requested(self):
        """不请求详情时 details 为空"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 1
        mock_db.list_expired_running_jobs.return_value = [
            {"job_id": 1, "repo_id": 1, "job_type": "test"}
        ]
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=False)

        violation = result.violations[0]
        assert violation.details == []

    def test_large_count_handling(self):
        """大数量违规的处理"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 10000
        mock_db.list_expired_running_jobs.return_value = [
            {"job_id": i, "repo_id": i, "job_type": "test"} for i in range(10)
        ]
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db, include_details=True)

        violation = result.violations[0]
        assert violation.count == 10000
        # details 应该被限制（取决于实现）
        assert len(violation.details) <= 10


# ============ Remediation Hint 验证 ============


class TestRemediationHints:
    """测试 remediation hint 包含可执行命令"""

    def test_expired_running_jobs_hint_contains_reaper(self):
        """expired_running_jobs 的 hint 包含 reaper 命令"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 1
        mock_db.list_expired_running_jobs.return_value = []
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        violation = next(v for v in result.violations if v.check_id == "expired_running_jobs")
        assert "reaper" in violation.remediation_hint

    def test_orphan_locks_hint_contains_force_release(self):
        """orphan_locks 的 hint 包含 force-release 命令"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 1
        mock_db.list_orphan_locks.return_value = []
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = []

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        violation = next(v for v in result.violations if v.check_id == "orphan_locks")
        assert "force-release" in violation.remediation_hint

    def test_circuit_breaker_hint_contains_reset(self):
        """circuit_breaker 的 hint 包含 reset 相关命令"""
        mock_db = MagicMock()
        mock_db.count_expired_running_jobs.return_value = 0
        mock_db.count_orphan_locks.return_value = 0
        mock_db.count_gitlab_jobs_missing_dimensions.return_value = 0
        mock_db.count_expired_pauses_affecting_scheduling.return_value = 0
        mock_db.get_circuit_breaker_inconsistencies.return_value = [{"issue": "test"}]

        conn = MagicMock()
        result = check_invariants(conn, db_api=mock_db)

        violation = next(
            v for v in result.violations if v.check_id == "circuit_breaker_inconsistencies"
        )
        assert (
            "reset" in violation.remediation_hint.lower()
            or "check" in violation.remediation_hint.lower()
        )
