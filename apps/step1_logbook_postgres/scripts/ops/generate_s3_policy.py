#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_s3_policy.py - 生成 IAM/MinIO 兼容的 S3 Bucket Policy

输入参数:
  - bucket: bucket 名称
  - prefix: 策略名称前缀 (如 engram-app, engram-ops)
  - allowed_prefixes: 允许访问的路径前缀列表 (如 scm/,attachments/)
  - allow_delete: 是否允许删除操作 (用于 ops 用户)
  - deny_insecure_transport: 是否拒绝非 HTTPS 传输

输出:
  - 标准 IAM/MinIO 兼容的 policy JSON

使用示例:
  # 生成 app policy (无删除权限)
  python generate_s3_policy.py --bucket engram --prefix app \\
    --allowed-prefixes "scm/,attachments/,exports/,tmp/"

  # 生成 ops policy (含删除权限)
  python generate_s3_policy.py --bucket engram --prefix ops \\
    --allowed-prefixes "scm/,attachments/,exports/,tmp/" --allow-delete

  # 生成强制 HTTPS policy
  python generate_s3_policy.py --bucket engram --prefix secure \\
    --allowed-prefixes "scm/" --deny-insecure-transport
"""

import argparse
import json
import sys
from typing import List, Optional


def generate_s3_policy(
    bucket: str,
    prefix: str,
    allowed_prefixes: List[str],
    allow_delete: bool = False,
    deny_insecure_transport: bool = False,
) -> dict:
    """
    生成 IAM/MinIO 兼容的 S3 Bucket Policy

    Args:
        bucket: S3/MinIO bucket 名称
        prefix: 策略名称前缀 (用于 Sid 标识)
        allowed_prefixes: 允许访问的路径前缀列表
        allow_delete: 是否允许 DeleteObject 权限
        deny_insecure_transport: 是否拒绝非 HTTPS 请求

    Returns:
        dict: IAM Policy 格式的字典
    """
    if not bucket:
        raise ValueError("bucket 参数不能为空")
    if not allowed_prefixes:
        raise ValueError("allowed_prefixes 参数不能为空")

    # 规范化前缀列表 (确保以 / 结尾但不以 / 开头)
    normalized_prefixes = []
    for p in allowed_prefixes:
        p = p.strip()
        if p:
            # 去除开头的 /
            p = p.lstrip("/")
            # 确保以 / 结尾
            if not p.endswith("/"):
                p = p + "/"
            normalized_prefixes.append(p)

    if not normalized_prefixes:
        raise ValueError("allowed_prefixes 中没有有效前缀")

    statements = []

    # Statement 1: AllowListBucketWithPrefix
    # 允许在特定前缀下 list bucket
    list_prefixes = [f"{p}*" for p in normalized_prefixes]
    statements.append({
        "Sid": f"{prefix}AllowListBucketWithPrefix",
        "Effect": "Allow",
        "Action": ["s3:ListBucket"],
        "Resource": [f"arn:aws:s3:::{bucket}"],
        "Condition": {
            "StringLike": {
                "s3:prefix": list_prefixes
            }
        }
    })

    # Statement 2: AllowObjectOperations
    # 构建资源列表
    resources = [f"arn:aws:s3:::{bucket}/{p}*" for p in normalized_prefixes]

    # 基础读写操作
    actions = [
        "s3:GetObject",
        "s3:PutObject",
    ]

    # ops 用户需要额外权限
    if allow_delete:
        # 添加 ListAllMyBuckets 语句 (ops 需要)
        statements.insert(0, {
            "Sid": f"{prefix}AllowListAllBuckets",
            "Effect": "Allow",
            "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
            "Resource": ["arn:aws:s3:::*"]
        })

        # 添加删除和版本管理权限
        actions.extend([
            "s3:DeleteObject",
            "s3:GetObjectVersion",
            "s3:DeleteObjectVersion",
        ])

    statements.append({
        "Sid": f"{prefix}AllowObjectOperations",
        "Effect": "Allow",
        "Action": actions,
        "Resource": resources
    })

    # Statement 3 (可选): DenyInsecureTransport
    # 强制 HTTPS 传输
    if deny_insecure_transport:
        statements.append({
            "Sid": f"{prefix}DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                f"arn:aws:s3:::{bucket}",
                f"arn:aws:s3:::{bucket}/*"
            ],
            "Condition": {
                "Bool": {
                    "aws:SecureTransport": "false"
                }
            }
        })

    policy = {
        "Version": "2012-10-17",
        "Statement": statements
    }

    return policy


def parse_prefixes(prefixes_str: str) -> List[str]:
    """
    解析逗号分隔的前缀字符串

    Args:
        prefixes_str: 逗号分隔的前缀列表，如 "scm/,attachments/,exports/"

    Returns:
        List[str]: 前缀列表
    """
    if not prefixes_str:
        return []
    return [p.strip() for p in prefixes_str.split(",") if p.strip()]


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description="生成 IAM/MinIO 兼容的 S3 Bucket Policy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成 app policy (无删除权限)
  %(prog)s --bucket engram --prefix app --allowed-prefixes "scm/,attachments/"

  # 生成 ops policy (含删除权限)
  %(prog)s --bucket engram --prefix ops --allowed-prefixes "scm/,attachments/" --allow-delete

  # 生成强制 HTTPS policy
  %(prog)s --bucket engram --prefix secure --allowed-prefixes "scm/" --deny-insecure-transport
        """
    )

    parser.add_argument(
        "--bucket",
        required=True,
        help="S3/MinIO bucket 名称"
    )
    parser.add_argument(
        "--prefix",
        required=True,
        help="策略名称前缀 (如 app, ops)"
    )
    parser.add_argument(
        "--allowed-prefixes",
        required=True,
        help="允许访问的路径前缀列表，逗号分隔 (如 scm/,attachments/,exports/,tmp/)"
    )
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        default=False,
        help="是否允许 DeleteObject 权限 (用于 ops 用户)"
    )
    parser.add_argument(
        "--deny-insecure-transport",
        action="store_true",
        default=False,
        help="是否拒绝非 HTTPS 传输"
    )
    parser.add_argument(
        "--output", "-o",
        help="输出文件路径 (默认输出到 stdout)"
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON 缩进空格数 (默认: 2)"
    )

    args = parser.parse_args()

    try:
        # 解析前缀列表
        prefixes = parse_prefixes(args.allowed_prefixes)
        if not prefixes:
            print("错误: --allowed-prefixes 不能为空", file=sys.stderr)
            sys.exit(1)

        # 生成 policy
        policy = generate_s3_policy(
            bucket=args.bucket,
            prefix=args.prefix,
            allowed_prefixes=prefixes,
            allow_delete=args.allow_delete,
            deny_insecure_transport=args.deny_insecure_transport,
        )

        # 输出 JSON
        policy_json = json.dumps(policy, indent=args.indent, ensure_ascii=False)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(policy_json)
                f.write("\n")
            print(f"Policy 已写入: {args.output}", file=sys.stderr)
        else:
            print(policy_json)

    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"生成 policy 失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
