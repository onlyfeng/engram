#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_bucket_governance_drift.py - Bucket 治理策略校验单元测试

测试内容:
  1. 生命周期规则 ID 校验逻辑
  2. Policy Actions 校验逻辑 (app 不含 DeleteObject, ops 含 DeleteObject/ListBucket)
  3. CheckResult 与 GovernanceReport 数据结构
"""

import json
import pytest
from pathlib import Path

# 导入被测模块
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from verify_bucket_governance import (
    CheckResult,
    GovernanceReport,
    DEFAULT_EXPECTED_LIFECYCLE_RULES,
    verify_lifecycle_rule_ids,
    verify_policy_actions_from_statements,
)


# ============================================================================
# 生命周期规则 ID 校验测试
# ============================================================================

class TestVerifyLifecycleRuleIds:
    """生命周期规则 ID 校验测试"""

    def test_all_expected_rules_present(self):
        """所有预期规则都存在时应通过"""
        found_rules = [
            "tmp-cleanup-7d",
            "exports-cleanup-90d",
            "trash-cleanup-30d",
            "abort-incomplete-multipart-1d",
        ]
        result = verify_lifecycle_rule_ids(found_rules)

        assert result.passed is True
        assert result.name == "lifecycle_rules"
        assert "所有预期规则均存在" in result.message
        assert result.details["missing"] == []

    def test_missing_one_rule(self):
        """缺少一条规则时应失败"""
        found_rules = [
            "tmp-cleanup-7d",
            "exports-cleanup-90d",
            "trash-cleanup-30d",
            # 缺少 abort-incomplete-multipart-1d
        ]
        result = verify_lifecycle_rule_ids(found_rules)

        assert result.passed is False
        assert "abort-incomplete-multipart-1d" in result.details["missing"]
        assert "缺少规则" in result.message

    def test_missing_multiple_rules(self):
        """缺少多条规则时应失败并列出所有缺失"""
        found_rules = ["tmp-cleanup-7d"]
        result = verify_lifecycle_rule_ids(found_rules)

        assert result.passed is False
        assert len(result.details["missing"]) == 3
        assert "exports-cleanup-90d" in result.details["missing"]
        assert "trash-cleanup-30d" in result.details["missing"]
        assert "abort-incomplete-multipart-1d" in result.details["missing"]

    def test_extra_rules_allowed(self):
        """额外规则存在时仍应通过（不强制限制）"""
        found_rules = [
            "tmp-cleanup-7d",
            "exports-cleanup-90d",
            "trash-cleanup-30d",
            "abort-incomplete-multipart-1d",
            "custom-rule-extra",  # 额外规则
        ]
        result = verify_lifecycle_rule_ids(found_rules)

        assert result.passed is True
        assert "custom-rule-extra" in result.details["extra"]

    def test_empty_found_rules(self):
        """未找到任何规则时应失败"""
        result = verify_lifecycle_rule_ids([])

        assert result.passed is False
        assert len(result.details["missing"]) == 4
        assert result.details["found"] == []

    def test_custom_expected_rules(self):
        """自定义预期规则列表"""
        custom_expected = ["rule-a", "rule-b"]
        found_rules = ["rule-a", "rule-b", "rule-c"]

        result = verify_lifecycle_rule_ids(found_rules, expected_rule_ids=custom_expected)

        assert result.passed is True
        assert result.details["expected"] == ["rule-a", "rule-b"]
        assert "rule-c" in result.details["extra"]

    def test_custom_expected_rules_missing(self):
        """自定义预期规则缺失"""
        custom_expected = ["rule-a", "rule-b", "rule-c"]
        found_rules = ["rule-a"]

        result = verify_lifecycle_rule_ids(found_rules, expected_rule_ids=custom_expected)

        assert result.passed is False
        assert "rule-b" in result.details["missing"]
        assert "rule-c" in result.details["missing"]

    def test_default_expected_rules_value(self):
        """验证默认预期规则列表"""
        assert "tmp-cleanup-7d" in DEFAULT_EXPECTED_LIFECYCLE_RULES
        assert "exports-cleanup-90d" in DEFAULT_EXPECTED_LIFECYCLE_RULES
        assert "trash-cleanup-30d" in DEFAULT_EXPECTED_LIFECYCLE_RULES
        assert "abort-incomplete-multipart-1d" in DEFAULT_EXPECTED_LIFECYCLE_RULES


# ============================================================================
# Policy Actions 校验测试
# ============================================================================

class TestVerifyPolicyActionsFromStatements:
    """Policy Actions 校验测试"""

    # ---- App Policy 测试 ----

    def test_app_policy_without_delete_should_pass(self):
        """App 策略不含 DeleteObject 应通过"""
        statements = [
            {
                "Sid": "appAllowListBucketWithPrefix",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": ["arn:aws:s3:::engram"],
            },
            {
                "Sid": "appAllowObjectOperations",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "app", "test-app-user")

        assert result.passed is True
        assert result.name == "policy_app"
        assert "不包含 DeleteObject" in result.message
        assert result.details["has_delete_object"] is False

    def test_app_policy_with_delete_should_fail(self):
        """App 策略包含 DeleteObject 应失败"""
        statements = [
            {
                "Sid": "appAllowObjectOperations",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "app", "test-app-user")

        assert result.passed is False
        assert "包含 DeleteObject" in result.message
        assert result.details["has_delete_object"] is True

    def test_app_policy_with_delete_object_version_should_fail(self):
        """App 策略包含 DeleteObjectVersion 也应失败"""
        statements = [
            {
                "Sid": "appAllowObjectOperations",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObjectVersion"],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "app", "test-app-user")

        assert result.passed is False
        assert result.details["has_delete_object"] is True

    # ---- Ops Policy 测试 ----

    def test_ops_policy_with_delete_and_list_should_pass(self):
        """Ops 策略包含 DeleteObject 和 ListBucket 应通过"""
        statements = [
            {
                "Sid": "opsAllowListAllBuckets",
                "Effect": "Allow",
                "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
                "Resource": ["arn:aws:s3:::*"],
            },
            {
                "Sid": "opsAllowListBucketWithPrefix",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": ["arn:aws:s3:::engram"],
            },
            {
                "Sid": "opsAllowObjectOperations",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "ops", "test-ops-user")

        assert result.passed is True
        assert result.name == "policy_ops"
        assert "包含 DeleteObject 和 ListBucket" in result.message
        assert result.details["has_delete_object"] is True
        assert result.details["has_list_bucket"] is True
        assert result.details["has_list_all_buckets"] is True

    def test_ops_policy_missing_delete_should_fail(self):
        """Ops 策略缺少 DeleteObject 应失败"""
        statements = [
            {
                "Sid": "opsAllowListBucketWithPrefix",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": ["arn:aws:s3:::engram"],
            },
            {
                "Sid": "opsAllowObjectOperations",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "ops", "test-ops-user")

        assert result.passed is False
        assert "DeleteObject" in result.message
        assert result.details["has_delete_object"] is False
        assert result.details["has_list_bucket"] is True

    def test_ops_policy_missing_list_bucket_should_fail(self):
        """Ops 策略缺少 ListBucket 应失败"""
        statements = [
            {
                "Sid": "opsAllowObjectOperations",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "ops", "test-ops-user")

        assert result.passed is False
        assert "ListBucket" in result.message
        assert result.details["has_delete_object"] is True
        assert result.details["has_list_bucket"] is False

    def test_ops_policy_missing_both_should_fail(self):
        """Ops 策略缺少 DeleteObject 和 ListBucket 应失败"""
        statements = [
            {
                "Sid": "opsAllowObjectOperations",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "ops", "test-ops-user")

        assert result.passed is False
        assert result.details["has_delete_object"] is False
        assert result.details["has_list_bucket"] is False

    # ---- 边界情况测试 ----

    def test_action_as_string(self):
        """Action 为单个字符串（非列表）的情况"""
        statements = [
            {
                "Sid": "singleAction",
                "Effect": "Allow",
                "Action": "s3:GetObject",  # 单个字符串而非列表
                "Resource": ["arn:aws:s3:::engram/scm/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "app", "test-user")

        assert result.passed is True
        assert "s3:GetObject" in result.details["all_actions"]

    def test_empty_statements(self):
        """空 statements 列表"""
        result = verify_policy_actions_from_statements([], "app", "test-user")

        assert result.passed is True  # app 策略不含 Delete 算通过
        assert result.details["all_actions"] == []

    def test_unknown_policy_type(self):
        """未知策略类型"""
        statements = [
            {
                "Sid": "test",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": ["arn:aws:s3:::engram/*"],
            },
        ]
        result = verify_policy_actions_from_statements(statements, "unknown", "test-user")

        assert result.passed is False
        assert "未知策略类型" in result.message


# ============================================================================
# CheckResult 数据结构测试
# ============================================================================

class TestCheckResult:
    """CheckResult 数据结构测试"""

    def test_check_result_to_dict(self):
        """CheckResult 转换为字典"""
        result = CheckResult(
            name="test_check",
            passed=True,
            message="Test passed",
            details={"key": "value"},
        )
        d = result.__dict__

        assert d["name"] == "test_check"
        assert d["passed"] is True
        assert d["message"] == "Test passed"
        assert d["details"] == {"key": "value"}

    def test_check_result_without_details(self):
        """CheckResult 无 details"""
        result = CheckResult(
            name="test_check",
            passed=False,
            message="Test failed",
        )
        assert result.details is None


# ============================================================================
# GovernanceReport 数据结构测试
# ============================================================================

class TestGovernanceReport:
    """GovernanceReport 数据结构测试"""

    def test_report_to_dict(self):
        """GovernanceReport 转换为字典"""
        report = GovernanceReport(
            bucket="test-bucket",
            mode="minio",
            passed=True,
            checks=[
                CheckResult(name="check1", passed=True, message="OK"),
                CheckResult(name="check2", passed=True, message="OK"),
            ],
            errors=[],
        )
        d = report.to_dict()

        assert d["bucket"] == "test-bucket"
        assert d["mode"] == "minio"
        assert d["passed"] is True
        assert len(d["checks"]) == 2
        assert d["errors"] == []

    def test_report_to_json(self):
        """GovernanceReport 转换为 JSON"""
        report = GovernanceReport(
            bucket="test-bucket",
            mode="aws",
            passed=False,
            checks=[
                CheckResult(
                    name="lifecycle_rules",
                    passed=False,
                    message="Missing rules",
                    details={"missing": ["rule-a"]},
                ),
            ],
            errors=["Some error occurred"],
        )
        json_str = json.dumps(report.to_dict(), indent=2)
        parsed = json.loads(json_str)

        assert parsed["bucket"] == "test-bucket"
        assert parsed["passed"] is False
        assert parsed["checks"][0]["details"]["missing"] == ["rule-a"]
        assert "Some error occurred" in parsed["errors"]


# ============================================================================
# 与模板文件的一致性测试
# ============================================================================

class TestLifecycleTemplateConsistency:
    """生命周期模板一致性测试"""

    @pytest.fixture
    def lifecycle_template_path(self):
        """生命周期策略模板路径"""
        return Path(__file__).parent.parent.parent / "templates" / "s3_lifecycle_policy.json"

    def test_template_rule_ids_match_expected(self, lifecycle_template_path):
        """模板中的 rule IDs 应与默认预期一致"""
        if not lifecycle_template_path.exists():
            pytest.skip(f"模板文件不存在: {lifecycle_template_path}")

        with open(lifecycle_template_path) as f:
            template = json.load(f)

        template_rule_ids = {rule["ID"] for rule in template.get("Rules", [])}

        for expected_id in DEFAULT_EXPECTED_LIFECYCLE_RULES:
            assert expected_id in template_rule_ids, f"模板缺少预期规则: {expected_id}"


class TestPolicyTemplateConsistency:
    """Policy 模板一致性测试"""

    @pytest.fixture
    def policy_template_path(self):
        """Policy 模板路径"""
        return Path(__file__).parent.parent / "ops" / "minio_bucket_policy.json"

    def test_app_policy_no_delete_in_template(self, policy_template_path):
        """模板中 app_policy 不应包含 DeleteObject"""
        if not policy_template_path.exists():
            pytest.skip(f"模板文件不存在: {policy_template_path}")

        with open(policy_template_path) as f:
            template = json.load(f)

        app_policy = template.get("app_policy", {})
        statements = app_policy.get("Statement", [])

        result = verify_policy_actions_from_statements(statements, "app", "template-app")
        assert result.passed is True, f"模板 app_policy 包含 DeleteObject: {result.details}"

    def test_ops_policy_has_delete_and_list_in_template(self, policy_template_path):
        """模板中 ops_policy 应包含 DeleteObject 和 ListBucket"""
        if not policy_template_path.exists():
            pytest.skip(f"模板文件不存在: {policy_template_path}")

        with open(policy_template_path) as f:
            template = json.load(f)

        ops_policy = template.get("ops_policy", {})
        statements = ops_policy.get("Statement", [])

        result = verify_policy_actions_from_statements(statements, "ops", "template-ops")
        assert result.passed is True, f"模板 ops_policy 缺少必要权限: {result.details}"


# ============================================================================
# generate_s3_policy.py 生成策略一致性测试
# ============================================================================

class TestGeneratedPolicyConsistency:
    """generate_s3_policy.py 生成策略一致性测试"""

    @pytest.fixture
    def generate_s3_policy_module(self):
        """动态导入 generate_s3_policy 模块"""
        generate_path = Path(__file__).parent.parent / "ops" / "generate_s3_policy.py"
        if not generate_path.exists():
            pytest.skip(f"generate_s3_policy.py 不存在: {generate_path}")

        import importlib.util
        spec = importlib.util.spec_from_file_location("generate_s3_policy", generate_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generated_app_policy_no_delete(self, generate_s3_policy_module):
        """生成的 app policy 不应包含 DeleteObject"""
        policy = generate_s3_policy_module.generate_s3_policy(
            bucket="test-bucket",
            prefix="app",
            allowed_prefixes=["scm/", "attachments/"],
            allow_delete=False,
        )
        statements = policy.get("Statement", [])

        result = verify_policy_actions_from_statements(statements, "app", "generated-app")
        assert result.passed is True, f"生成的 app policy 包含 DeleteObject: {result.details}"

    def test_generated_ops_policy_has_delete_and_list(self, generate_s3_policy_module):
        """生成的 ops policy 应包含 DeleteObject 和 ListBucket"""
        policy = generate_s3_policy_module.generate_s3_policy(
            bucket="test-bucket",
            prefix="ops",
            allowed_prefixes=["scm/", "attachments/"],
            allow_delete=True,
        )
        statements = policy.get("Statement", [])

        result = verify_policy_actions_from_statements(statements, "ops", "generated-ops")
        assert result.passed is True, f"生成的 ops policy 缺少必要权限: {result.details}"
