#!/bin/bash
# ===========================================================================
# s3_hardening.sh - S3/MinIO 安全加固脚本模板
# ===========================================================================
#
# 功能说明:
#   1. 启用 Bucket Versioning（对象版本控制）
#   2. 可选启用 Object Lock（对象锁定，防止删除）
#   3. 提供恢复误删除对象的操作指引
#
# 适用环境:
#   - AWS S3
#   - MinIO (需要 MINIO_API_VERSION=S3v4)
#
# 前置要求:
#   - AWS CLI (aws) 或 MinIO Client (mc) 已安装
#   - 已配置访问凭证
#
# 使用方式:
#   # 编辑配置变量后运行
#   chmod +x s3_hardening.sh
#   ./s3_hardening.sh
#
# ===========================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 配置区域 - 请根据实际环境修改
# ---------------------------------------------------------------------------

# 目标 Bucket 名称
BUCKET="${ENGRAM_S3_BUCKET:-artifacts}"

# MinIO 别名（仅 mc 工具使用）
MC_ALIAS="${MC_ALIAS:-myminio}"

# S3 Endpoint（AWS S3 留空，MinIO 需指定）
S3_ENDPOINT="${ENGRAM_S3_ENDPOINT:-}"

# 是否启用 Object Lock（需在创建 Bucket 时启用，无法后期添加）
# 值: true | false
ENABLE_OBJECT_LOCK="${ENABLE_OBJECT_LOCK:-false}"

# Object Lock 默认保留期（天）
OBJECT_LOCK_DAYS="${OBJECT_LOCK_DAYS:-30}"

# Object Lock 模式: GOVERNANCE | COMPLIANCE
# - GOVERNANCE: 特权用户可以覆盖
# - COMPLIANCE: 任何人都不能删除（谨慎使用）
OBJECT_LOCK_MODE="${OBJECT_LOCK_MODE:-GOVERNANCE}"

# ---------------------------------------------------------------------------
# 工具检测
# ---------------------------------------------------------------------------

detect_tool() {
    if command -v mc &> /dev/null; then
        echo "mc"
    elif command -v aws &> /dev/null; then
        echo "aws"
    else
        echo "error"
    fi
}

TOOL=$(detect_tool)

if [ "$TOOL" = "error" ]; then
    echo "错误: 未找到 aws 或 mc 命令行工具"
    echo "请安装 AWS CLI (https://aws.amazon.com/cli/) 或 MinIO Client (https://min.io/docs/minio/linux/reference/minio-mc.html)"
    exit 1
fi

echo "使用工具: $TOOL"
echo "目标 Bucket: $BUCKET"
echo ""

# ---------------------------------------------------------------------------
# 1. 启用 Bucket Versioning
# ---------------------------------------------------------------------------

enable_versioning() {
    echo "========================================="
    echo "1. 启用 Bucket Versioning"
    echo "========================================="
    
    if [ "$TOOL" = "mc" ]; then
        # MinIO Client
        echo "执行: mc version enable ${MC_ALIAS}/${BUCKET}"
        mc version enable "${MC_ALIAS}/${BUCKET}"
        
        echo ""
        echo "验证 Versioning 状态:"
        mc version info "${MC_ALIAS}/${BUCKET}"
    else
        # AWS CLI
        if [ -n "$S3_ENDPOINT" ]; then
            ENDPOINT_OPTS="--endpoint-url $S3_ENDPOINT"
        else
            ENDPOINT_OPTS=""
        fi
        
        echo "执行: aws s3api put-bucket-versioning ..."
        aws s3api put-bucket-versioning \
            $ENDPOINT_OPTS \
            --bucket "$BUCKET" \
            --versioning-configuration Status=Enabled
        
        echo ""
        echo "验证 Versioning 状态:"
        aws s3api get-bucket-versioning $ENDPOINT_OPTS --bucket "$BUCKET"
    fi
    
    echo ""
    echo "✓ Versioning 已启用"
    echo ""
}

# ---------------------------------------------------------------------------
# 2. 配置 Object Lock（可选）
# ---------------------------------------------------------------------------

configure_object_lock() {
    echo "========================================="
    echo "2. 配置 Object Lock（可选）"
    echo "========================================="
    
    if [ "$ENABLE_OBJECT_LOCK" != "true" ]; then
        echo "跳过: ENABLE_OBJECT_LOCK 未设置为 true"
        echo ""
        echo "【重要提示】Object Lock 必须在创建 Bucket 时启用！"
        echo "如需启用，请使用以下命令创建新 Bucket:"
        echo ""
        if [ "$TOOL" = "mc" ]; then
            echo "  mc mb --with-lock ${MC_ALIAS}/${BUCKET}-locked"
        else
            echo "  aws s3api create-bucket --bucket ${BUCKET}-locked --object-lock-enabled-for-bucket"
        fi
        echo ""
        return 0
    fi
    
    echo "配置默认 Object Lock 规则:"
    echo "  模式: $OBJECT_LOCK_MODE"
    echo "  保留期: $OBJECT_LOCK_DAYS 天"
    echo ""
    
    if [ "$TOOL" = "mc" ]; then
        # MinIO Client
        echo "执行: mc retention set --default ${OBJECT_LOCK_MODE} ${OBJECT_LOCK_DAYS}d ${MC_ALIAS}/${BUCKET}"
        mc retention set --default "${OBJECT_LOCK_MODE}" "${OBJECT_LOCK_DAYS}d" "${MC_ALIAS}/${BUCKET}"
        
        echo ""
        echo "验证 Object Lock 配置:"
        mc retention info "${MC_ALIAS}/${BUCKET}"
    else
        # AWS CLI
        if [ -n "$S3_ENDPOINT" ]; then
            ENDPOINT_OPTS="--endpoint-url $S3_ENDPOINT"
        else
            ENDPOINT_OPTS=""
        fi
        
        # 构建 JSON 配置
        LOCK_CONFIG=$(cat <<EOF
{
    "ObjectLockEnabled": "Enabled",
    "Rule": {
        "DefaultRetention": {
            "Mode": "${OBJECT_LOCK_MODE}",
            "Days": ${OBJECT_LOCK_DAYS}
        }
    }
}
EOF
)
        
        echo "执行: aws s3api put-object-lock-configuration ..."
        echo "$LOCK_CONFIG" | aws s3api put-object-lock-configuration \
            $ENDPOINT_OPTS \
            --bucket "$BUCKET" \
            --object-lock-configuration file:///dev/stdin
        
        echo ""
        echo "验证 Object Lock 配置:"
        aws s3api get-object-lock-configuration $ENDPOINT_OPTS --bucket "$BUCKET"
    fi
    
    echo ""
    echo "✓ Object Lock 已配置"
    echo ""
}

# ---------------------------------------------------------------------------
# 3. 查看版本历史（参考命令）
# ---------------------------------------------------------------------------

show_version_commands() {
    echo "========================================="
    echo "3. 版本管理参考命令"
    echo "========================================="
    echo ""
    
    if [ "$TOOL" = "mc" ]; then
        cat <<EOF
# 列出对象的所有版本
mc ls --versions ${MC_ALIAS}/${BUCKET}/scm/

# 恢复删除的对象（从特定版本）
mc cp --version-id <VERSION_ID> ${MC_ALIAS}/${BUCKET}/path/to/object ./restored_object

# 永久删除特定版本（需要权限）
mc rm --version-id <VERSION_ID> ${MC_ALIAS}/${BUCKET}/path/to/object

# 列出已删除对象（delete markers）
mc ls --versions ${MC_ALIAS}/${BUCKET}/ | grep "DEL"

EOF
    else
        cat <<EOF
# 列出对象的所有版本
aws s3api list-object-versions ${S3_ENDPOINT:+--endpoint-url $S3_ENDPOINT} \\
    --bucket ${BUCKET} --prefix scm/

# 恢复删除的对象（复制旧版本到当前）
aws s3api copy-object ${S3_ENDPOINT:+--endpoint-url $S3_ENDPOINT} \\
    --bucket ${BUCKET} \\
    --copy-source "${BUCKET}/path/to/object?versionId=<VERSION_ID>" \\
    --key path/to/object

# 永久删除特定版本
aws s3api delete-object ${S3_ENDPOINT:+--endpoint-url $S3_ENDPOINT} \\
    --bucket ${BUCKET} --key path/to/object --version-id <VERSION_ID>

# 列出删除标记
aws s3api list-object-versions ${S3_ENDPOINT:+--endpoint-url $S3_ENDPOINT} \\
    --bucket ${BUCKET} --query 'DeleteMarkers[]'

EOF
    fi
}

# ---------------------------------------------------------------------------
# 4. artifact_gc.py 安全操作建议
# ---------------------------------------------------------------------------

show_gc_recommendations() {
    echo "========================================="
    echo "4. artifact_gc.py 生产安全建议"
    echo "========================================="
    echo ""
    cat <<EOF
【生产环境 GC 操作建议】

1. 始终使用软删除（--trash-prefix）:
   python artifact_gc.py --prefix scm/ --trash-prefix .trash/ --delete

2. 使用 --require-trash 强制软删除（防止意外硬删除）:
   python artifact_gc.py --prefix scm/ --require-trash --delete
   
3. 先 dry-run 确认删除列表:
   python artifact_gc.py --prefix scm/ --older-than-days 30
   
4. 使用 --older-than-days 限制删除范围:
   python artifact_gc.py --prefix scm/ --older-than-days 90 --trash-prefix .trash/ --delete

5. 生产环境配置 S3 生命周期规则自动清理 .trash/:
   # 30 天后自动删除 .trash/ 下的对象
   mc ilm add ${MC_ALIAS}/${BUCKET} --prefix ".trash/" --expiry-days 30

【版本恢复流程】

如果误删除对象（启用 Versioning 后）:
1. 查找对象版本: mc ls --versions ${MC_ALIAS}/${BUCKET}/path/
2. 恢复特定版本: mc cp --version-id <VID> ${MC_ALIAS}/${BUCKET}/path ./local

EOF
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

main() {
    echo ""
    echo "==========================================="
    echo " S3/MinIO 安全加固脚本"
    echo "==========================================="
    echo ""
    
    enable_versioning
    configure_object_lock
    show_version_commands
    show_gc_recommendations
    
    echo "==========================================="
    echo " 加固完成"
    echo "==========================================="
}

# 运行主流程
main
