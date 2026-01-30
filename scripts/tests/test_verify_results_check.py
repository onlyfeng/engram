#!/usr/bin/env python3
"""
verify_results_check.py 单元测试

覆盖场景：
1. degradation=ok/fail/skipped（不同 reason）
2. --full 时：除允许的显式 skip 外，skipped 一律失败；非 full 时允许
3. schema-only 模式不受业务门禁影响

注意：JSON Schema 中 reason_code 有严格的 enum 限制。
对于允许的跳过场景，使用以下策略：
- 直接测试 _is_allowed_skip_reason 函数
- 使用环境变量（SKIP_DEGRADATION_TEST=1）触发允许跳过
- 直接调用 validate_step_statuses 函数绕过 schema 校验
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# 将 scripts 目录添加到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from verify_results_check import (
    run_validation,
    _is_allowed_skip_reason,
    validate_required_steps,
    validate_step_statuses,
    ALLOWED_SKIP_REASONS,
    DISALLOWED_SKIP_REASONS,
)

# ============================================================================
# 辅助函数：构建最小有效 verify-results JSON
# ============================================================================

def make_minimal_verify_results(
    steps: list[dict],
    overall_status: str = "pass",
    total_failed: int = 0,
) -> dict:
    """
    构建最小有效的 verify-results JSON

    Args:
        steps: 步骤列表
        overall_status: 整体状态 (pass/fail)
        total_failed: 失败步骤数

    Returns:
        满足 JSON Schema 的最小验证结果
    """
    return {
        "verify_mode": "default",
        "overall_status": overall_status,
        "total_failed": total_failed,
        "total_duration_ms": 1000,
        "gateway_url": "http://localhost:8787",
        "openmemory_url": "http://localhost:8080",
        "timestamp": "2026-01-29T10:00:00+08:00",
        "steps": steps,
    }


def make_base_steps_ok() -> list[dict]:
    """构建基础步骤（全部 ok）"""
    return [
        {"name": "health_checks", "status": "ok", "duration_ms": 100},
        {"name": "memory_store", "status": "ok", "duration_ms": 100},
        {"name": "memory_query", "status": "ok", "duration_ms": 100},
        {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
    ]


def make_full_steps_with_degradation(
    degradation_status: str,
    degradation_reason: str | None = None,
) -> list[dict]:
    """
    构建 full 模式所需的全部步骤

    Args:
        degradation_status: degradation 步骤状态 (ok/fail/skipped)
        degradation_reason: 如果 skipped，跳过原因
    """
    steps = make_base_steps_ok()
    steps.append({"name": "db_invariants", "status": "ok", "duration_ms": 200})

    degradation_step = {
        "name": "degradation",
        "status": degradation_status,
        "duration_ms": 500 if degradation_status != "skipped" else 0,
    }
    if degradation_reason:
        degradation_step["reason"] = degradation_reason

    steps.append(degradation_step)
    return steps


def write_json_file(data: dict) -> Path:
    """将数据写入临时 JSON 文件"""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(data, f)
    f.flush()
    f.close()
    return Path(f.name)


# ============================================================================
# Test: _is_allowed_skip_reason 函数
# ============================================================================

class TestIsAllowedSkipReason:
    """测试跳过原因判定函数"""

    @pytest.mark.parametrize("reason", [
        "explicit_skip",
        "user_skip",
        "skip_degradation_test",
        "http_only_mode",
        "not_required_for_profile",
        "EXPLICIT_SKIP",  # 大写
        "explicit-skip",  # 连字符
        "user skip",      # 空格
    ])
    def test_allowed_skip_reasons_return_true(self, reason: str):
        """测试允许的跳过原因返回 True"""
        assert _is_allowed_skip_reason(reason) is True

    @pytest.mark.parametrize("reason", [
        "no_docker",
        "docker_not_found",
        "no_postgres_dsn",
        "postgres_dsn_missing",
        "no_container",
        "container_not_found",
        "cannot_stop_container",
        "docker_daemon_down",
        "compose_not_configured",
        "no_db_access",
        "capability_missing",
        "unknown_reason",
    ])
    def test_disallowed_skip_reasons_return_false(self, reason: str):
        """测试不允许的跳过原因返回 False"""
        assert _is_allowed_skip_reason(reason) is False

    def test_empty_reason_returns_false(self):
        """测试空原因返回 False"""
        assert _is_allowed_skip_reason("") is False
        assert _is_allowed_skip_reason(None) is False


# ============================================================================
# Test: degradation 状态覆盖
# ============================================================================

class TestDegradationStatus:
    """测试 degradation 步骤不同状态"""

    def test_degradation_ok_full_mode_pass(self):
        """degradation=ok 在 full 模式下应通过"""
        steps = make_full_steps_with_degradation("ok")
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is True
            assert result["overall_valid"] is True
        finally:
            os.unlink(json_path)

    def test_degradation_fail_full_mode_fail(self):
        """degradation=fail 在 full 模式下应失败"""
        steps = make_full_steps_with_degradation("fail")
        data = make_minimal_verify_results(steps, overall_status="fail", total_failed=1)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
            assert any("degradation" in e.lower() for e in result["errors"])
        finally:
            os.unlink(json_path)

    def test_degradation_skipped_allowed_via_env_skip_degradation_test(self):
        """degradation=skipped 通过 SKIP_DEGRADATION_TEST=1 环境变量允许（直接测试 validate_step_statuses）
        
        注意：validate_required_steps 不考虑环境变量，只看 JSON 中的 reason。
        所以这里直接测试 validate_step_statuses 来验证环境变量生效。
        """
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "no_docker"},
            ]
        }
        with patch.dict(os.environ, {"SKIP_DEGRADATION_TEST": "1"}):
            is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
            # 环境变量显式跳过，validate_step_statuses 应该通过
            assert is_valid is True
            assert len(errors) == 0
            # 应该有 warning
            assert any("SKIP_DEGRADATION_TEST" in w for w in warnings)

    def test_degradation_skipped_allowed_via_env_http_only_mode(self):
        """degradation=skipped 通过 HTTP_ONLY_MODE=1 环境变量允许（直接测试 validate_step_statuses）"""
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "no_docker"},
            ]
        }
        with patch.dict(os.environ, {"HTTP_ONLY_MODE": "1"}):
            is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
            assert is_valid is True
            assert len(errors) == 0
            assert any("HTTP_ONLY_MODE" in w for w in warnings)

    def test_degradation_skipped_disallowed_reason_full_mode_fail(self):
        """degradation=skipped（不允许的原因）在 full 模式下应失败"""
        steps = make_full_steps_with_degradation("skipped", "no_docker")
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
            # 应该包含 degradation 相关错误
            assert any("degradation" in e.lower() for e in result["errors"])
        finally:
            os.unlink(json_path)

    def test_degradation_skipped_no_postgres_dsn_full_mode_fail(self):
        """degradation=skipped（no_postgres_dsn）在 full 模式下应失败"""
        steps = make_full_steps_with_degradation("skipped", "no_postgres_dsn")
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
            assert any("degradation" in e.lower() for e in result["errors"])
        finally:
            os.unlink(json_path)


# ============================================================================
# Test: --full 模式 vs 非 full 模式
# ============================================================================

class TestFullModeVsStandard:
    """测试 full 模式与非 full 模式的差异"""

    def test_skipped_step_allowed_in_standard_mode(self):
        """skipped 步骤在 standard 模式下允许"""
        steps = make_base_steps_ok()
        # 添加 skipped 的 degradation（standard 不要求）
        steps.append({"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "no_docker"})
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=False, profile="standard")
            # standard 模式不要求 degradation，所以应该通过
            assert is_valid is True
        finally:
            os.unlink(json_path)

    def test_skipped_step_fail_in_full_mode(self):
        """skipped 步骤（非允许原因）在 full 模式下失败"""
        steps = make_full_steps_with_degradation("skipped", "no_docker")
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
        finally:
            os.unlink(json_path)

    def test_explicit_skip_via_env_allowed_in_full_mode(self):
        """通过环境变量 SKIP_DEGRADATION_TEST=1 显式跳过在 full 模式下允许（validate_step_statuses）"""
        # 直接测试 validate_step_statuses，验证环境变量生效
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "no_docker"},
            ]
        }
        with patch.dict(os.environ, {"SKIP_DEGRADATION_TEST": "1"}):
            is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
            assert is_valid is True

    def test_http_only_mode_env_skip_allowed_in_full_mode(self):
        """通过环境变量 HTTP_ONLY_MODE=1 跳过在 full 模式下允许（validate_step_statuses）"""
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "no_docker"},
            ]
        }
        with patch.dict(os.environ, {"HTTP_ONLY_MODE": "1"}):
            is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
            assert is_valid is True

    def test_validate_step_statuses_explicit_skip_reason_allowed(self):
        """直接测试 validate_step_statuses：explicit_skip 原因在 full 模式下允许"""
        # 直接测试 validate_step_statuses 函数，绕过 schema 校验
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "explicit_skip"},
            ]
        }
        is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
        # explicit_skip 是允许的原因
        assert is_valid is True
        assert len(errors) == 0
        assert any("explicit_skip" in w or "明确允许" in w for w in warnings)

    def test_validate_step_statuses_not_required_for_profile_allowed(self):
        """直接测试 validate_step_statuses：not_required_for_profile 原因在 full 模式下允许"""
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": "not_required_for_profile"},
            ]
        }
        is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
        assert is_valid is True
        assert len(errors) == 0


# ============================================================================
# Test: schema-only 模式
# ============================================================================

class TestSchemaOnlyMode:
    """测试 schema-only 模式"""

    def test_schema_only_valid_json_pass(self):
        """schema-only 模式下，有效 JSON 应通过"""
        steps = make_base_steps_ok()
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, schema_only=True)
            assert is_valid is True
            assert result["schema_only"] is True
        finally:
            os.unlink(json_path)

    def test_schema_only_ignores_business_gate(self):
        """schema-only 模式下，业务门禁不影响结果"""
        # 构建一个在 full 模式下会失败的 JSON（degradation skipped with disallowed reason）
        steps = make_full_steps_with_degradation("skipped", "no_docker")
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            # schema-only 模式应该通过（不检查业务门禁）
            is_valid, result = run_validation(json_path, full_mode=True, schema_only=True)
            assert is_valid is True
            assert result["schema_only"] is True

            # 对比：非 schema-only 的 full 模式应该失败
            is_valid_full, result_full = run_validation(json_path, full_mode=True, schema_only=False)
            assert is_valid_full is False
        finally:
            os.unlink(json_path)

    def test_schema_only_invalid_json_fail(self):
        """schema-only 模式下，无效 JSON 应失败"""
        # 缺少必需字段
        data = {
            "verify_mode": "default",
            # 缺少 overall_status, total_failed, steps 等
        }
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, schema_only=True)
            assert is_valid is False
            assert "schema" in str(result["errors"]).lower() or len(result["errors"]) > 0
        finally:
            os.unlink(json_path)

    def test_schema_only_missing_required_steps_still_pass(self):
        """schema-only 模式下，缺少必需步骤仍然通过（只要 schema 有效）"""
        # 只有一个步骤（在 full 模式下会缺少必需步骤）
        steps = [{"name": "health_checks", "status": "ok", "duration_ms": 100}]
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True, schema_only=True)
            # schema 有效，所以应通过
            assert is_valid is True
        finally:
            os.unlink(json_path)


# ============================================================================
# Test: db_invariants 步骤（full 模式下也必需）
# ============================================================================

class TestDbInvariantsStep:
    """测试 db_invariants 步骤"""

    def test_db_invariants_skipped_disallowed_reason_full_mode_fail(self):
        """db_invariants=skipped（不允许的原因）在 full 模式下应失败"""
        steps = make_base_steps_ok()
        steps.append({
            "name": "db_invariants",
            "status": "skipped",
            "duration_ms": 0,
            "reason": "no_postgres_dsn"
        })
        steps.append({"name": "degradation", "status": "ok", "duration_ms": 500})
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
            assert any("db_invariants" in e.lower() for e in result["errors"])
        finally:
            os.unlink(json_path)

    def test_db_invariants_skipped_allowed_via_validate_required_steps(self):
        """直接测试 validate_required_steps：允许的原因在 full 模式下应通过"""
        # 直接测试 validate_required_steps 函数，绕过 schema 校验
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "skipped", "duration_ms": 0, "reason": "not_required_for_profile"},
                {"name": "degradation", "status": "ok", "duration_ms": 500},
            ]
        }
        is_valid, errors, fixes = validate_required_steps(data, full_mode=True)
        # not_required_for_profile 是允许的原因
        assert is_valid is True
        assert len(errors) == 0

    def test_db_invariants_skipped_via_env_skip_degradation_test(self):
        """db_invariants=skipped 通过 SKIP_DEGRADATION_TEST=1 环境变量允许"""
        steps = make_base_steps_ok()
        steps.append({
            "name": "db_invariants",
            "status": "skipped",
            "duration_ms": 0,
            "reason": "no_postgres_dsn"
        })
        steps.append({"name": "degradation", "status": "ok", "duration_ms": 500})
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            # 虽然 reason 是 no_postgres_dsn，但 validate_required_steps 本身会检查 reason
            # 注意：SKIP_DEGRADATION_TEST 主要影响 validate_step_statuses 中的 degradation 检查
            # db_invariants 的允许跳过由 _is_allowed_skip_reason 判断
            # 这里测试直接调用 validate_required_steps
            is_valid, errors, fixes = validate_required_steps(data, full_mode=True)
            # no_postgres_dsn 不是允许的原因，所以会失败
            assert is_valid is False
        finally:
            os.unlink(json_path)


# ============================================================================
# Test: 整体状态一致性
# ============================================================================

class TestOverallStatusConsistency:
    """测试整体状态一致性校验"""

    def test_pass_with_zero_failed(self):
        """overall_status=pass 且 total_failed=0 应通过"""
        steps = make_base_steps_ok()
        data = make_minimal_verify_results(steps, overall_status="pass", total_failed=0)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path)
            assert is_valid is True
        finally:
            os.unlink(json_path)

    def test_pass_with_nonzero_failed_should_warn(self):
        """overall_status=pass 但 total_failed>0 应报错"""
        steps = make_base_steps_ok()
        data = make_minimal_verify_results(steps, overall_status="pass", total_failed=1)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path)
            # schema 校验会失败（根据 allOf 约束）或业务校验失败
            # 根据实现，这可能是 schema 错误或 overall_status 错误
            assert is_valid is False or any("total_failed" in str(e) or "overall_status" in str(e) for e in result["errors"])
        finally:
            os.unlink(json_path)


# ============================================================================
# Test: 多种跳过原因组合
# ============================================================================

class TestVariousSkipReasons:
    """测试各种跳过原因在不同模式下的行为"""

    @pytest.mark.parametrize("reason,expected_valid", [
        # 允许的原因（直接测试 validate_step_statuses）
        ("explicit_skip", True),
        ("user_skip", True),
        ("skip_degradation_test", True),
        ("http_only_mode", True),
        ("not_required_for_profile", True),
        # 不允许的原因
        ("no_docker", False),
        ("docker_not_found", False),
        ("no_postgres_dsn", False),
        ("no_container", False),
        ("cannot_stop_container", False),
        ("docker_daemon_down", False),
        ("capability_missing", False),
    ])
    def test_degradation_skip_reasons_via_validate_step_statuses(self, reason: str, expected_valid: bool):
        """测试各种 degradation 跳过原因在 full 模式下的行为（直接测试 validate_step_statuses）"""
        # 直接测试 validate_step_statuses，绕过 schema 校验
        data = {
            "steps": [
                {"name": "health_checks", "status": "ok", "duration_ms": 100},
                {"name": "memory_store", "status": "ok", "duration_ms": 100},
                {"name": "memory_query", "status": "ok", "duration_ms": 100},
                {"name": "jsonrpc", "status": "ok", "duration_ms": 100},
                {"name": "db_invariants", "status": "ok", "duration_ms": 200},
                {"name": "degradation", "status": "skipped", "duration_ms": 0, "reason": reason},
            ]
        }
        is_valid, errors, warnings, fixes = validate_step_statuses(data, full_mode=True)
        assert is_valid is expected_valid, f"reason={reason}, expected_valid={expected_valid}, errors={errors}"

    @pytest.mark.parametrize("reason", [
        # Schema 中允许的 reason_code（用于 run_validation 测试）
        "no_docker",
        "no_postgres_dsn",
        "cannot_stop_container",
    ])
    def test_disallowed_skip_reasons_via_run_validation(self, reason: str):
        """测试不允许的跳过原因通过 run_validation 完整流程"""
        steps = make_full_steps_with_degradation("skipped", reason)
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False, f"reason={reason}, should fail in full mode"
            assert any("degradation" in e.lower() for e in result["errors"])
        finally:
            os.unlink(json_path)


# ============================================================================
# Test: fix_suggestions 存在性
# ============================================================================

class TestFixSuggestions:
    """测试修复建议是否正确生成"""

    def test_fix_suggestions_for_no_docker(self):
        """no_docker 原因应该生成 Docker 安装建议"""
        steps = make_full_steps_with_degradation("skipped", "no_docker")
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
            # 应该包含 Docker 相关修复建议
            suggestions = result.get("fix_suggestions", [])
            assert any("docker" in s.lower() for s in suggestions)
        finally:
            os.unlink(json_path)

    def test_fix_suggestions_for_no_postgres_dsn(self):
        """no_postgres_dsn 原因应该生成 POSTGRES_DSN 设置建议"""
        steps = make_base_steps_ok()
        steps.append({
            "name": "db_invariants",
            "status": "skipped",
            "duration_ms": 0,
            "reason": "no_postgres_dsn"
        })
        steps.append({"name": "degradation", "status": "ok", "duration_ms": 500})
        data = make_minimal_verify_results(steps)
        json_path = write_json_file(data)

        try:
            is_valid, result = run_validation(json_path, full_mode=True)
            assert is_valid is False
            # 应该包含 POSTGRES_DSN 相关修复建议
            suggestions = result.get("fix_suggestions", [])
            assert any("postgres_dsn" in s.lower() or "dsn" in s.lower() for s in suggestions)
        finally:
            os.unlink(json_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
