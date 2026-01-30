#!/usr/bin/env python3
"""
generate_s3_policy.py - 生成 S3 bucket 策略（测试用）
"""

from __future__ import annotations

import argparse
import json
from typing import List


def parse_prefixes(prefixes_csv: str) -> List[str]:
    if not prefixes_csv:
        return []
    parts = [p.strip() for p in prefixes_csv.split(",")]
    return [p for p in parts if p]


def _normalize_prefix(prefix: str) -> str:
    prefix = prefix.strip()
    if not prefix:
        return ""
    while prefix.startswith("/"):
        prefix = prefix[1:]
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


def generate_s3_policy(
    *,
    bucket: str,
    prefix: str,
    allowed_prefixes: List[str],
    allow_delete: bool = False,
    deny_insecure_transport: bool = False,
) -> dict:
    if not bucket:
        raise ValueError("bucket 不能为空")
    if not allowed_prefixes:
        raise ValueError("allowed_prefixes 不能为空")

    prefixes = []
    for raw in allowed_prefixes:
        normalized = _normalize_prefix(raw)
        if normalized:
            prefixes.append(normalized)
    if not prefixes:
        raise ValueError("allowed_prefixes 不能为空")
    list_prefixes = [f"{p}*" for p in prefixes]

    statements = []

    if allow_delete:
        statements.append(
            {
                "Sid": f"{prefix}AllowListAllBuckets",
                "Effect": "Allow",
                "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
                "Resource": ["arn:aws:s3:::*"],
            }
        )

    statements.append(
        {
            "Sid": f"{prefix}AllowListBucketWithPrefix",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [f"arn:aws:s3:::{bucket}"],
            "Condition": {"StringLike": {"s3:prefix": list_prefixes}},
        }
    )

    object_actions = ["s3:GetObject", "s3:PutObject", "s3:GetObjectVersion"]
    if allow_delete:
        object_actions.extend(["s3:DeleteObject", "s3:DeleteObjectVersion"])

    object_resources = [f"arn:aws:s3:::{bucket}/{p}*" for p in prefixes]
    statements.append(
        {
            "Sid": f"{prefix}AllowObjectOperations",
            "Effect": "Allow",
            "Action": object_actions,
            "Resource": object_resources,
        }
    )

    if deny_insecure_transport:
        statements.append(
            {
                "Sid": f"{prefix}DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [
                    f"arn:aws:s3:::{bucket}",
                    f"arn:aws:s3:::{bucket}/*",
                ],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }
        )

    return {"Version": "2012-10-17", "Statement": statements}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate S3 policy")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--allowed-prefixes", required=True)
    parser.add_argument("--allow-delete", action="store_true")
    parser.add_argument("--deny-insecure-transport", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()

    policy = generate_s3_policy(
        bucket=args.bucket,
        prefix=args.prefix,
        allowed_prefixes=parse_prefixes(args.allowed_prefixes),
        allow_delete=args.allow_delete,
        deny_insecure_transport=args.deny_insecure_transport,
    )
    policy_json = json.dumps(policy)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(policy_json)
    print(policy_json)


if __name__ == "__main__":
    _main()
