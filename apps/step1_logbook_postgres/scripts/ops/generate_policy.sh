#!/bin/sh
# generate_policy.sh - 生成 IAM/MinIO 兼容的 S3 Bucket Policy (Shell 版本)
#
# 用于 minio_init 容器中生成 policy JSON，因为 mc 镜像没有 Python 环境
# Python 版本 (generate_s3_policy.py) 用于开发调试和 CI 验证
#
# 用法:
#   generate_policy.sh <type> <bucket> <prefixes_csv> [output_file]
#
# 参数:
#   type: app | ops
#   bucket: S3 bucket 名称
#   prefixes_csv: 逗号分隔的前缀列表 (如 scm/,attachments/)
#   output_file: 可选，输出文件路径 (默认输出到 stdout)
#
# 示例:
#   generate_policy.sh app engram "scm/,attachments/,exports/,tmp/"
#   generate_policy.sh ops engram "scm/,attachments/" /tmp/ops_policy.json

set -e

TYPE="$1"
BUCKET="$2"
PREFIXES_CSV="$3"
OUTPUT_FILE="$4"

if [ -z "$TYPE" ] || [ -z "$BUCKET" ] || [ -z "$PREFIXES_CSV" ]; then
    echo "用法: $0 <type> <bucket> <prefixes_csv> [output_file]" >&2
    echo "  type: app | ops" >&2
    echo "  bucket: S3 bucket 名称" >&2
    echo "  prefixes_csv: 逗号分隔的前缀列表" >&2
    exit 1
fi

# 构建前缀 JSON 数组
PREFIXES_JSON=""
RESOURCES_JSON=""
IFS=','
for prefix in $PREFIXES_CSV; do
    prefix=$(echo "$prefix" | tr -d ' ')
    # 确保以 / 结尾
    case "$prefix" in
        */) ;;
        *) prefix="$prefix/" ;;
    esac
    if [ -n "$prefix" ]; then
        PREFIXES_JSON="${PREFIXES_JSON}\"${prefix}*\","
        RESOURCES_JSON="${RESOURCES_JSON}\"arn:aws:s3:::${BUCKET}/${prefix}*\","
    fi
done
unset IFS

# 移除末尾逗号
PREFIXES_JSON="${PREFIXES_JSON%,}"
RESOURCES_JSON="${RESOURCES_JSON%,}"

# 根据类型生成不同的 policy
generate_app_policy() {
    cat << POLICY_EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "appAllowListBucketWithPrefix",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${BUCKET}"],
      "Condition": {
        "StringLike": {
          "s3:prefix": [${PREFIXES_JSON}]
        }
      }
    },
    {
      "Sid": "appAllowObjectOperations",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": [${RESOURCES_JSON}]
    }
  ]
}
POLICY_EOF
}

generate_ops_policy() {
    cat << POLICY_EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "opsAllowListAllBuckets",
      "Effect": "Allow",
      "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::*"]
    },
    {
      "Sid": "opsAllowListBucketWithPrefix",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${BUCKET}"],
      "Condition": {
        "StringLike": {
          "s3:prefix": [${PREFIXES_JSON}]
        }
      }
    },
    {
      "Sid": "opsAllowObjectOperations",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:GetObjectVersion",
        "s3:DeleteObjectVersion"
      ],
      "Resource": [${RESOURCES_JSON}]
    }
  ]
}
POLICY_EOF
}

# 生成 policy
case "$TYPE" in
    app)
        POLICY=$(generate_app_policy)
        ;;
    ops)
        POLICY=$(generate_ops_policy)
        ;;
    *)
        echo "错误: type 必须是 app 或 ops" >&2
        exit 1
        ;;
esac

# 输出
if [ -n "$OUTPUT_FILE" ]; then
    echo "$POLICY" > "$OUTPUT_FILE"
    echo "Policy 已写入: $OUTPUT_FILE" >&2
else
    echo "$POLICY"
fi
