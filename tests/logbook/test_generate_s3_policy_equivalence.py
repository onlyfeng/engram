#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_generate_s3_policy_equivalence.py - 验证 Shell 与 Python 版本 S3 Policy 生成器语义等价

测试目标:
  1. 给定相同的 bucket 与 allowed_prefixes，比较 generate_policy.sh 和 generate_s3_policy.py
     的输出在关键语义上等价 (actions/resources/condition)
  2. 覆盖 deny_insecure_transport 可选分支 (仅 Python 支持)
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

# 定位脚本路径
SCRIPTS_DIR = Path(__file__).parent.parent
OPS_DIR = SCRIPTS_DIR / "ops"
GENERATE_POLICY_SH = OPS_DIR / "generate_policy.sh"
GENERATE_S3_POLICY_PY = OPS_DIR / "generate_s3_policy.py"

# 添加 ops 目录到 path，以便直接 import
sys.path.insert(0, str(OPS_DIR))
from generate_s3_policy import generate_s3_policy, parse_prefixes

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def run_shell_script(
    policy_type: str,
    bucket: str,
    prefixes_csv: str,
) -> Dict[str, Any]:
    """
    运行 Shell 脚本并解析 JSON 输出

    Args:
        policy_type: app | ops
        bucket: bucket 名称
        prefixes_csv: 逗号分隔的前缀列表

    Returns:
        解析后的 policy dict
    """
    if not GENERATE_POLICY_SH.exists():
        pytest.skip(f"Shell 脚本不存在: {GENERATE_POLICY_SH}")

    # 使用 sh 命令运行脚本，避免需要可执行权限
    result = subprocess.run(
        ["sh", str(GENERATE_POLICY_SH), policy_type, bucket, prefixes_csv],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        pytest.fail(f"Shell 脚本执行失败: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"Shell 脚本输出不是有效 JSON: {e}\nOutput: {result.stdout}")


def run_python_script(
    bucket: str,
    prefix: str,
    allowed_prefixes: str,
    allow_delete: bool = False,
    deny_insecure_transport: bool = False,
) -> Dict[str, Any]:
    """
    运行 Python 脚本并解析 JSON 输出

    Args:
        bucket: bucket 名称
        prefix: 策略名称前缀 (app|ops)
        allowed_prefixes: 逗号分隔的前缀列表
        allow_delete: 是否允许删除
        deny_insecure_transport: 是否拒绝非 HTTPS

    Returns:
        解析后的 policy dict
    """
    if not GENERATE_S3_POLICY_PY.exists():
        pytest.skip(f"Python 脚本不存在: {GENERATE_S3_POLICY_PY}")

    cmd = [
        sys.executable,
        str(GENERATE_S3_POLICY_PY),
        "--bucket",
        bucket,
        "--prefix",
        prefix,
        "--allowed-prefixes",
        allowed_prefixes,
    ]

    if allow_delete:
        cmd.append("--allow-delete")

    if deny_insecure_transport:
        cmd.append("--deny-insecure-transport")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        pytest.fail(f"Python 脚本执行失败: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"Python 脚本输出不是有效 JSON: {e}\nOutput: {result.stdout}")


def extract_actions(statement: Dict[str, Any]) -> Set[str]:
    """从 statement 中提取 actions 集合"""
    actions = statement.get("Action", [])
    if isinstance(actions, str):
        actions = [actions]
    return set(actions)


def extract_resources(statement: Dict[str, Any]) -> Set[str]:
    """从 statement 中提取 resources 集合"""
    resources = statement.get("Resource", [])
    if isinstance(resources, str):
        resources = [resources]
    return set(resources)


def extract_condition_prefixes(statement: Dict[str, Any]) -> Set[str]:
    """从 statement 中提取 condition 中的 s3:prefix 列表"""
    condition = statement.get("Condition", {})
    string_like = condition.get("StringLike", {})
    prefixes = string_like.get("s3:prefix", [])
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    return set(prefixes)


def find_statement_by_action(
    statements: List[Dict[str, Any]],
    action: str,
) -> Dict[str, Any] | None:
    """根据 action 查找 statement"""
    for stmt in statements:
        if action in extract_actions(stmt):
            return stmt
    return None


def find_list_bucket_statement(statements: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """查找 ListBucket statement"""
    return find_statement_by_action(statements, "s3:ListBucket")


def find_object_ops_statement(statements: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """查找 ObjectOperations statement (包含 GetObject)"""
    return find_statement_by_action(statements, "s3:GetObject")


def find_list_all_buckets_statement(statements: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """查找 ListAllMyBuckets statement"""
    return find_statement_by_action(statements, "s3:ListAllMyBuckets")


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------


class TestPolicyEquivalence:
    """测试 Shell 与 Python 版本 Policy 的语义等价性"""

    @pytest.mark.parametrize(
        "bucket,prefixes_csv",
        [
            ("engram", "scm/,attachments/"),
            ("test-bucket", "data/,logs/,tmp/"),
            ("my-bucket", "prefix1/,prefix2/,prefix3/"),
        ],
    )
    def test_app_policy_equivalence(self, bucket: str, prefixes_csv: str):
        """
        测试 app 类型 policy 的语义等价性

        Shell: generate_policy.sh app <bucket> <prefixes>
        Python: generate_s3_policy.py --prefix app --allow-delete=False
        """
        # 生成两个版本的 policy
        shell_policy = run_shell_script("app", bucket, prefixes_csv)
        python_policy = run_python_script(bucket, "app", prefixes_csv, allow_delete=False)

        # 验证 Version 相同
        assert shell_policy["Version"] == python_policy["Version"] == "2012-10-17"

        # 验证 Statement 数量相同 (app 应该有 2 个 statement)
        assert len(shell_policy["Statement"]) == len(python_policy["Statement"]) == 2

        # 验证 ListBucket statement
        shell_list_bucket = find_list_bucket_statement(shell_policy["Statement"])
        python_list_bucket = find_list_bucket_statement(python_policy["Statement"])

        assert shell_list_bucket is not None, "Shell policy 缺少 ListBucket statement"
        assert python_list_bucket is not None, "Python policy 缺少 ListBucket statement"

        # Actions 等价
        assert extract_actions(shell_list_bucket) == extract_actions(python_list_bucket)

        # Resources 等价 (bucket ARN)
        assert extract_resources(shell_list_bucket) == extract_resources(python_list_bucket)

        # Condition prefixes 等价
        assert extract_condition_prefixes(shell_list_bucket) == extract_condition_prefixes(
            python_list_bucket
        )

        # 验证 ObjectOperations statement
        shell_obj_ops = find_object_ops_statement(shell_policy["Statement"])
        python_obj_ops = find_object_ops_statement(python_policy["Statement"])

        assert shell_obj_ops is not None, "Shell policy 缺少 ObjectOperations statement"
        assert python_obj_ops is not None, "Python policy 缺少 ObjectOperations statement"

        # Actions 等价 (app: GetObject, PutObject)
        assert extract_actions(shell_obj_ops) == extract_actions(python_obj_ops)
        assert extract_actions(shell_obj_ops) == {"s3:GetObject", "s3:PutObject"}

        # Resources 等价
        assert extract_resources(shell_obj_ops) == extract_resources(python_obj_ops)

    @pytest.mark.parametrize(
        "bucket,prefixes_csv",
        [
            ("engram", "scm/,attachments/"),
            ("test-bucket", "data/,logs/,tmp/"),
            ("production", "artifacts/,backups/"),
        ],
    )
    def test_ops_policy_equivalence(self, bucket: str, prefixes_csv: str):
        """
        测试 ops 类型 policy 的语义等价性

        Shell: generate_policy.sh ops <bucket> <prefixes>
        Python: generate_s3_policy.py --prefix ops --allow-delete=True
        """
        # 生成两个版本的 policy
        shell_policy = run_shell_script("ops", bucket, prefixes_csv)
        python_policy = run_python_script(bucket, "ops", prefixes_csv, allow_delete=True)

        # 验证 Version 相同
        assert shell_policy["Version"] == python_policy["Version"] == "2012-10-17"

        # 验证 Statement 数量相同 (ops 应该有 3 个 statement)
        assert len(shell_policy["Statement"]) == len(python_policy["Statement"]) == 3

        # 验证 ListAllMyBuckets statement
        shell_list_all = find_list_all_buckets_statement(shell_policy["Statement"])
        python_list_all = find_list_all_buckets_statement(python_policy["Statement"])

        assert shell_list_all is not None, "Shell policy 缺少 ListAllMyBuckets statement"
        assert python_list_all is not None, "Python policy 缺少 ListAllMyBuckets statement"

        # Actions 等价
        assert extract_actions(shell_list_all) == extract_actions(python_list_all)
        assert extract_actions(shell_list_all) == {"s3:ListAllMyBuckets", "s3:GetBucketLocation"}

        # Resources 等价
        assert extract_resources(shell_list_all) == extract_resources(python_list_all)

        # 验证 ListBucket statement
        shell_list_bucket = find_list_bucket_statement(shell_policy["Statement"])
        python_list_bucket = find_list_bucket_statement(python_policy["Statement"])

        assert extract_actions(shell_list_bucket) == extract_actions(python_list_bucket)
        assert extract_resources(shell_list_bucket) == extract_resources(python_list_bucket)
        assert extract_condition_prefixes(shell_list_bucket) == extract_condition_prefixes(
            python_list_bucket
        )

        # 验证 ObjectOperations statement
        shell_obj_ops = find_object_ops_statement(shell_policy["Statement"])
        python_obj_ops = find_object_ops_statement(python_policy["Statement"])

        # Actions 等价 (ops: GetObject, PutObject, DeleteObject, GetObjectVersion, DeleteObjectVersion)
        assert extract_actions(shell_obj_ops) == extract_actions(python_obj_ops)
        expected_ops_actions = {
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:GetObjectVersion",
            "s3:DeleteObjectVersion",
        }
        assert extract_actions(shell_obj_ops) == expected_ops_actions

        # Resources 等价
        assert extract_resources(shell_obj_ops) == extract_resources(python_obj_ops)

    def test_prefix_normalization_equivalence(self):
        """测试前缀规范化的等价性（带/不带尾部斜杠）"""
        bucket = "test-bucket"

        # Shell 和 Python 都应该将 "data" 规范化为 "data/"
        shell_policy = run_shell_script("app", bucket, "data,logs")
        python_policy = run_python_script(bucket, "app", "data,logs", allow_delete=False)

        shell_list_bucket = find_list_bucket_statement(shell_policy["Statement"])
        python_list_bucket = find_list_bucket_statement(python_policy["Statement"])

        # 验证 condition prefixes 包含规范化后的前缀
        shell_prefixes = extract_condition_prefixes(shell_list_bucket)
        python_prefixes = extract_condition_prefixes(python_list_bucket)

        assert shell_prefixes == python_prefixes
        assert "data/*" in shell_prefixes
        assert "logs/*" in shell_prefixes

    def test_resource_arn_format_equivalence(self):
        """测试资源 ARN 格式的等价性"""
        bucket = "my-bucket"
        prefixes = "scm/,attachments/"

        shell_policy = run_shell_script("app", bucket, prefixes)
        python_policy = run_python_script(bucket, "app", prefixes, allow_delete=False)

        shell_obj_ops = find_object_ops_statement(shell_policy["Statement"])
        python_obj_ops = find_object_ops_statement(python_policy["Statement"])

        shell_resources = extract_resources(shell_obj_ops)
        python_resources = extract_resources(python_obj_ops)

        # 验证格式: arn:aws:s3:::<bucket>/<prefix>*
        expected_resources = {
            f"arn:aws:s3:::{bucket}/scm/*",
            f"arn:aws:s3:::{bucket}/attachments/*",
        }

        assert shell_resources == expected_resources
        assert python_resources == expected_resources


class TestDenyInsecureTransport:
    """测试 deny_insecure_transport 功能（仅 Python 支持）"""

    def test_deny_insecure_transport_disabled(self):
        """测试 deny_insecure_transport=False 时没有 Deny statement"""
        policy = run_python_script(
            "test-bucket",
            "app",
            "data/",
            allow_delete=False,
            deny_insecure_transport=False,
        )

        # 应该没有 Deny statement
        for stmt in policy["Statement"]:
            assert stmt["Effect"] != "Deny", "不应包含 Deny statement"

    def test_deny_insecure_transport_enabled(self):
        """测试 deny_insecure_transport=True 时有正确的 Deny statement"""
        bucket = "secure-bucket"
        policy = run_python_script(
            bucket,
            "secure",
            "data/",
            allow_delete=False,
            deny_insecure_transport=True,
        )

        # 应该有 Deny statement
        deny_stmts = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        assert len(deny_stmts) == 1, "应该有且仅有一个 Deny statement"

        deny_stmt = deny_stmts[0]

        # 验证 Sid
        assert "DenyInsecureTransport" in deny_stmt["Sid"]

        # 验证 Principal
        assert deny_stmt["Principal"] == "*"

        # 验证 Action
        action = deny_stmt["Action"]
        if isinstance(action, list):
            assert "s3:*" in action
        else:
            assert action == "s3:*"

        # 验证 Resource 包含 bucket 和 objects
        resources = extract_resources(deny_stmt)
        assert f"arn:aws:s3:::{bucket}" in resources
        assert f"arn:aws:s3:::{bucket}/*" in resources

        # 验证 Condition
        condition = deny_stmt.get("Condition", {})
        assert "Bool" in condition
        assert condition["Bool"].get("aws:SecureTransport") == "false"

    def test_deny_insecure_transport_with_app_policy(self):
        """测试 app policy + deny_insecure_transport"""
        policy = run_python_script(
            "test-bucket",
            "app",
            "data/,logs/",
            allow_delete=False,
            deny_insecure_transport=True,
        )

        # 应该有 3 个 statements: ListBucket, ObjectOps, DenyInsecure
        assert len(policy["Statement"]) == 3

        # 验证有 Allow 和 Deny
        effects = {s["Effect"] for s in policy["Statement"]}
        assert effects == {"Allow", "Deny"}

    def test_deny_insecure_transport_with_ops_policy(self):
        """测试 ops policy + deny_insecure_transport"""
        policy = run_python_script(
            "test-bucket",
            "ops",
            "data/,logs/",
            allow_delete=True,
            deny_insecure_transport=True,
        )

        # 应该有 4 个 statements: ListAllBuckets, ListBucket, ObjectOps, DenyInsecure
        assert len(policy["Statement"]) == 4

        # 验证有 Allow 和 Deny
        effects = {s["Effect"] for s in policy["Statement"]}
        assert effects == {"Allow", "Deny"}

        # 验证 Deny statement 存在且正确
        deny_stmts = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        assert len(deny_stmts) == 1


class TestPythonApiDirect:
    """直接测试 Python API 函数"""

    def test_generate_s3_policy_basic(self):
        """测试基础 policy 生成"""
        policy = generate_s3_policy(
            bucket="test",
            prefix="app",
            allowed_prefixes=["data/"],
        )

        assert policy["Version"] == "2012-10-17"
        assert len(policy["Statement"]) == 2

    def test_generate_s3_policy_with_delete(self):
        """测试 allow_delete=True"""
        policy = generate_s3_policy(
            bucket="test",
            prefix="ops",
            allowed_prefixes=["data/"],
            allow_delete=True,
        )

        assert len(policy["Statement"]) == 3

        obj_ops = find_object_ops_statement(policy["Statement"])
        actions = extract_actions(obj_ops)
        assert "s3:DeleteObject" in actions
        assert "s3:DeleteObjectVersion" in actions

    def test_generate_s3_policy_deny_insecure(self):
        """测试 deny_insecure_transport=True"""
        policy = generate_s3_policy(
            bucket="test",
            prefix="secure",
            allowed_prefixes=["data/"],
            deny_insecure_transport=True,
        )

        assert len(policy["Statement"]) == 3

        deny_stmts = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        assert len(deny_stmts) == 1

    def test_generate_s3_policy_empty_bucket_raises(self):
        """测试空 bucket 参数抛出异常"""
        with pytest.raises(ValueError, match="bucket.*不能为空"):
            generate_s3_policy(
                bucket="",
                prefix="app",
                allowed_prefixes=["data/"],
            )

    def test_generate_s3_policy_empty_prefixes_raises(self):
        """测试空 prefixes 参数抛出异常"""
        with pytest.raises(ValueError, match="allowed_prefixes.*不能为空"):
            generate_s3_policy(
                bucket="test",
                prefix="app",
                allowed_prefixes=[],
            )

    def test_parse_prefixes(self):
        """测试前缀解析函数"""
        assert parse_prefixes("a/,b/,c/") == ["a/", "b/", "c/"]
        assert parse_prefixes("a, b, c") == ["a", "b", "c"]
        assert parse_prefixes("") == []
        assert parse_prefixes("  ,  ,  ") == []


class TestEdgeCases:
    """边界情况测试"""

    def test_single_prefix(self):
        """测试单个前缀"""
        shell_policy = run_shell_script("app", "bucket", "data/")
        python_policy = run_python_script("bucket", "app", "data/", allow_delete=False)

        shell_obj = find_object_ops_statement(shell_policy["Statement"])
        python_obj = find_object_ops_statement(python_policy["Statement"])

        assert extract_resources(shell_obj) == extract_resources(python_obj)
        assert extract_resources(shell_obj) == {"arn:aws:s3:::bucket/data/*"}

    def test_many_prefixes(self):
        """测试多个前缀"""
        prefixes = "a/,b/,c/,d/,e/"

        shell_policy = run_shell_script("app", "bucket", prefixes)
        python_policy = run_python_script("bucket", "app", prefixes, allow_delete=False)

        shell_obj = find_object_ops_statement(shell_policy["Statement"])
        python_obj = find_object_ops_statement(python_policy["Statement"])

        assert len(extract_resources(shell_obj)) == 5
        assert extract_resources(shell_obj) == extract_resources(python_obj)

    def test_special_characters_in_bucket_name(self):
        """测试 bucket 名称中的特殊字符（连字符）"""
        bucket = "my-test-bucket-123"

        shell_policy = run_shell_script("app", bucket, "data/")
        python_policy = run_python_script(bucket, "app", "data/", allow_delete=False)

        shell_list = find_list_bucket_statement(shell_policy["Statement"])
        python_list = find_list_bucket_statement(python_policy["Statement"])

        assert extract_resources(shell_list) == {f"arn:aws:s3:::{bucket}"}
        assert extract_resources(python_list) == {f"arn:aws:s3:::{bucket}"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
