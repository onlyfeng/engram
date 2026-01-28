#!/usr/bin/env python3
"""
数据库迁移脚本 - CLI 入口

读取配置获取 DSN，执行 SQL schema 文件，并进行自检。

核心迁移逻辑已移至 engram_step1.migrate 模块，
本脚本仅保留 CLI 入口。

使用方法:
    python db_migrate.py
    python db_migrate.py --config /path/to/config.toml
    python db_migrate.py --apply-roles                    # 执行角色权限脚本（需要 admin/superuser）
    python db_migrate.py --apply-openmemory-grants        # 执行 OpenMemory schema 权限脚本
    python db_migrate.py --verify                         # 执行权限验证脚本（99）
    python db_migrate.py --post-backfill                  # 迁移后回填 evidence_uri
    python db_migrate.py --backfill-chunking-version v1.0 # 迁移后回填 chunking_version

环境变量:
    ENGRAM_STEP1_CONFIG: 配置文件路径
    ENGRAM_TESTING: 设置为 "1" 启用测试模式（允许使用 --schema-prefix）

架构约束（路线A - 多库方案）:
    - 生产模式下不允许使用 schema_prefix
    - schema 名固定为: identity, logbook, scm, analysis, governance
    - --schema-prefix 仅在测试模式下可用（需设置 ENGRAM_TESTING=1）

SQL 脚本执行规则:
    - 默认执行（结构性 DDL）: 01/02/03/06/07/08
    - 可选执行（权限脚本，需要 admin/superuser）: 04/05
      - --apply-roles: 执行 04_roles_and_grants.sql
      - --apply-openmemory-grants: 执行 05_openmemory_roles_and_grants.sql
    - 验证脚本: 99（仅通过 --verify 执行）

OpenMemory 同库部署:
    当满足以下任一条件时，脚本会自动发现并执行:
    - sql/05_openmemory_roles_and_grants.sql（若存在）
    
    触发条件（二选一）:
    1. 显式开关：指定 --apply-openmemory-grants 参数
    2. 自动探测：环境变量 OM_METADATA_BACKEND=postgres
    
    目标 schema 通过 OM_PG_SCHEMA 环境变量配置（默认 'openmemory'）
    迁移后自检会验证目标 OM schema 是否创建成功，
    失败时返回错误码 OPENMEMORY_SCHEMA_MISSING

迁移后回填（Post-Backfill）:
    迁移完成后可选执行数据回填操作：
    
    --post-backfill:
        回填 patch_blobs.meta_json.evidence_uri
        为缺失 evidence_uri 的记录生成 canonical URI
        格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
    
    --backfill-chunking-version VERSION:
        回填 patch_blobs.chunking_version 和 attachments.meta_json.chunking_version
        将版本号统一更新为指定值
    
    --backfill-batch-size N:
        回填每批处理记录数（默认 1000）
    
    --backfill-dry-run:
        仅模拟回填，不实际修改数据
"""

import argparse
import sys

from engram_step1.config import add_config_argument
from engram_step1.io import (
    add_output_arguments,
    get_output_options,
    output_json,
)

# 从 engram_step1.migrate 导入核心函数
from engram_step1.migrate import (
    run_migrate,
    run_all_checks,
    run_precheck,
    is_testing_mode,
    get_repair_commands_hint,
    # 检查函数（供向后兼容导出）
    check_schemas_exist,
    check_tables_exist,
    check_columns_exist,
    check_indexes_exist,
    check_triggers_exist,
    check_matviews_exist,
    check_openmemory_schema_exists,
    check_search_path,
    # 常量（供向后兼容导出）
    DEFAULT_SCHEMA_SUFFIXES,
    REQUIRED_TABLE_TEMPLATES,
    REQUIRED_COLUMN_TEMPLATES,
    REQUIRED_INDEX_TEMPLATES,
    REQUIRED_TRIGGER_TEMPLATES,
    REQUIRED_MATVIEW_TEMPLATES,
)


def main():
    parser = argparse.ArgumentParser(
        description="执行数据库 schema 迁移",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_config_argument(parser)
    add_output_arguments(parser)
    
    # 添加 schema-prefix 参数（仅测试模式可用）
    parser.add_argument(
        "--schema-prefix",
        type=str,
        default=None,
        help="[仅测试模式] Schema 前缀，用于测试隔离。需设置 ENGRAM_TESTING=1 环境变量。生产环境禁用此参数（路线A 多库方案）",
    )
    
    # 添加 --dsn 参数用于 Docker 环境直接传入连接字符串
    parser.add_argument(
        "--dsn",
        type=str,
        default=None,
        help="直接指定 PostgreSQL DSN（优先于配置文件）。格式: postgresql://user:pass@host:port/dbname",
    )
    
    # 添加 --apply-roles 参数控制是否执行角色权限脚本
    parser.add_argument(
        "--apply-roles",
        action="store_true",
        default=None,
        help="执行角色权限脚本 04_roles_and_grants.sql。如不指定，从配置 postgres.apply_roles 读取",
    )
    
    # 添加 --apply-openmemory-grants 参数控制是否执行 OpenMemory schema 权限脚本
    parser.add_argument(
        "--apply-openmemory-grants",
        action="store_true",
        default=None,
        help="执行 OpenMemory schema 权限脚本 05_openmemory_roles_and_grants.sql。"
             "当环境变量 OM_METADATA_BACKEND=postgres 时会自动启用。"
             "目标 schema 通过 OM_PG_SCHEMA 指定（默认 'openmemory'）",
    )
    
    # 添加 --precheck-only 参数仅执行预检
    parser.add_argument(
        "--precheck-only",
        action="store_true",
        default=False,
        help="仅执行预检，不执行实际迁移。用于 CI/CD 流水线或手动验证配置",
    )
    
    # 添加 --verify 参数执行权限验证脚本
    parser.add_argument(
        "--verify",
        action="store_true",
        default=False,
        help="执行权限验证脚本 99_verify_permissions.sql。"
             "用于验证角色和权限配置是否正确",
    )
    
    # ============ 迁移后回填（post-backfill）参数 ============
    parser.add_argument(
        "--post-backfill",
        action="store_true",
        default=False,
        help="迁移后执行 evidence_uri 回填。"
             "为 patch_blobs 表中缺失 evidence_uri 的记录生成 canonical URI",
    )
    
    parser.add_argument(
        "--backfill-chunking-version",
        type=str,
        default=None,
        metavar="VERSION",
        help="迁移后执行 chunking_version 回填，指定目标版本号。"
             "例如: --backfill-chunking-version v1.0",
    )
    
    parser.add_argument(
        "--backfill-batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="回填每批处理的记录数（默认: 1000）",
    )
    
    parser.add_argument(
        "--backfill-dry-run",
        action="store_true",
        default=False,
        help="回填时仅模拟执行，不实际修改数据",
    )
    
    args = parser.parse_args()

    opts = get_output_options(args)
    result = run_migrate(
        args.config_path,
        quiet=opts["quiet"],
        dsn=args.dsn,
        schema_prefix=args.schema_prefix,
        apply_roles=args.apply_roles,
        apply_openmemory_grants=args.apply_openmemory_grants,
        precheck_only=args.precheck_only,
        verify=args.verify,
        post_backfill=args.post_backfill,
        backfill_chunking_version=args.backfill_chunking_version,
        backfill_batch_size=args.backfill_batch_size,
        backfill_dry_run=args.backfill_dry_run,
    )
    output_json(result, pretty=opts["pretty"])

    # 根据 ok 字段决定退出码
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
