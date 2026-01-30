#!/usr/bin/env python3
"""
verify_bucket_governance - bucket 策略与生命周期检查（简化版）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


DEFAULT_EXPECTED_LIFECYCLE_RULES = [
    "tmp-cleanup-7d",
    "exports-cleanup-90d",
    "trash-cleanup-30d",
    "abort-incomplete-multipart-1d",
]


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class GovernanceReport:
    bucket: str
    mode: str
    passed: bool
    checks: List[CheckResult]
    errors: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bucket": self.bucket,
            "mode": self.mode,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "errors": self.errors,
        }


def verify_lifecycle_rule_ids(
    found_rule_ids: List[str],
    *,
    expected_rule_ids: Optional[List[str]] = None,
) -> CheckResult:
    expected = expected_rule_ids or DEFAULT_EXPECTED_LIFECYCLE_RULES
    found_set = set(found_rule_ids)
    expected_set = set(expected)
    missing = sorted(expected_set - found_set)
    extra = sorted(found_set - expected_set)
    passed = len(missing) == 0
    message = "所有预期规则均存在" if passed else "缺少规则: " + ", ".join(missing)
    return CheckResult(
        name="lifecycle_rules",
        passed=passed,
        message=message,
        details={
            "expected": expected,
            "found": found_rule_ids,
            "missing": missing,
            "extra": extra,
        },
    )


def _extract_actions(statements: List[Dict[str, Any]]) -> List[str]:
    actions: List[str] = []
    for stmt in statements:
        action = stmt.get("Action", [])
        if isinstance(action, str):
            actions.append(action)
        else:
            actions.extend(action)
    return actions


def verify_policy_actions_from_statements(
    statements: List[Dict[str, Any]],
    policy_type: str,
    principal: str,
) -> CheckResult:
    all_actions = _extract_actions(statements)
    has_delete_object = any(a in ("s3:DeleteObject", "s3:DeleteObjectVersion") for a in all_actions)
    has_list_bucket = "s3:ListBucket" in all_actions
    has_list_all = "s3:ListAllMyBuckets" in all_actions

    details = {
        "principal": principal,
        "all_actions": all_actions,
        "has_delete_object": has_delete_object,
        "has_list_bucket": has_list_bucket,
        "has_list_all_buckets": has_list_all,
    }

    if policy_type == "app":
        passed = not has_delete_object
        message = "不包含 DeleteObject" if passed else "包含 DeleteObject"
        return CheckResult(name="policy_app", passed=passed, message=message, details=details)

    if policy_type == "ops":
        passed = has_delete_object and has_list_bucket
        if passed:
            message = "包含 DeleteObject 和 ListBucket"
        elif not has_delete_object and not has_list_bucket:
            message = "缺少 DeleteObject 与 ListBucket"
        elif not has_delete_object:
            message = "缺少 DeleteObject"
        else:
            message = "缺少 ListBucket"
        return CheckResult(name="policy_ops", passed=passed, message=message, details=details)

    return CheckResult(
        name=f"policy_{policy_type}",
        passed=False,
        message="未知策略类型",
        details=details,
    )
