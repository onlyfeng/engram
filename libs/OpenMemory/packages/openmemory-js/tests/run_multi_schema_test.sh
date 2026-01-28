#!/bin/bash
# 多 Schema 隔离集成测试运行脚本
#
# 使用方法:
#   1. 设置 PostgreSQL 连接参数
#   2. 运行 ./tests/run_multi_schema_test.sh
#
# 环境变量说明:
#   OM_PG_HOST     - PostgreSQL 主机地址 (默认: localhost)
#   OM_PG_PORT     - PostgreSQL 端口 (默认: 5432)
#   OM_PG_DB       - 测试数据库名称 (默认: openmemory_test)
#   OM_PG_USER     - 数据库用户 (默认: postgres)
#   OM_PG_PASSWORD - 数据库密码 (必填)

set -e

cd "$(dirname "$0")/.."

# 默认配置
export OM_PG_HOST="${OM_PG_HOST:-localhost}"
export OM_PG_PORT="${OM_PG_PORT:-5432}"
export OM_PG_DB="${OM_PG_DB:-openmemory_test}"
export OM_PG_USER="${OM_PG_USER:-postgres}"
export OM_METADATA_BACKEND="postgres"
export OM_PG_SSL="${OM_PG_SSL:-disable}"

# 检查密码
if [ -z "$OM_PG_PASSWORD" ]; then
    echo "[ERROR] 请设置 OM_PG_PASSWORD 环境变量"
    echo ""
    echo "使用方法:"
    echo "  export OM_PG_PASSWORD=your_password"
    echo "  ./tests/run_multi_schema_test.sh"
    exit 1
fi

echo "=================================="
echo "OpenMemory 多 Schema 隔离测试"
echo "=================================="
echo "PostgreSQL: ${OM_PG_HOST}:${OM_PG_PORT}/${OM_PG_DB}"
echo "User: ${OM_PG_USER}"
echo ""

# 运行测试
npx ts-node tests/test_multi_schema.ts
