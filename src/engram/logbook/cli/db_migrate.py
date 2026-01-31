#!/usr/bin/env python3
"""
数据库迁移脚本 - CLI 入口

读取配置获取 DSN，执行 SQL schema 文件，并进行自检。

核心迁移逻辑已移至 engram_logbook.migrate 模块，
本脚本仅保留 CLI 入口。

使用方法:
    python -m engram_logbook.cli.db_migrate
    python -m engram_logbook.cli.db_migrate --config /path/to/config.toml
    python -m engram_logbook.cli.db_migrate --apply-roles                    # 执行角色权限脚本（需要 admin/superuser）
    python -m engram_logbook.cli.db_migrate --apply-openmemory-grants        # 执行 OpenMemory schema 权限脚本
    python -m engram_logbook.cli.db_migrate --verify                         # 执行权限验证脚本（99）
    python -m engram_logbook.cli.db_migrate --plan                           # 查看迁移计划（不连接数据库）

    或使用 console script:
    engram-migrate
    engram-migrate --config /path/to/config.toml
    engram-migrate --plan                                                    # 查看迁移计划

环境变量:
    ENGRAM_LOGBOOK_CONFIG: 配置文件路径
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

迁移计划模式（--plan）:
    使用 --plan 参数可在不连接数据库的情况下查看迁移计划：
    - 列出所有 DDL/权限/验证脚本
    - 显示本次将执行的脚本列表
    - 显示重复前缀警告
    - 执行配置预检（不需要数据库连接）

    输出 JSON 格式，包含完整的迁移信息。

OpenMemory 同库部署:
    当满足以下任一条件时，脚本会自动发现并执行:
    - sql/05_openmemory_roles_and_grants.sql（若存在）

    触发条件（二选一）:
    1. 显式开关：指定 --apply-openmemory-grants 参数
    2. 自动探测：环境变量 OM_METADATA_BACKEND=postgres

    目标 schema 通过 OM_PG_SCHEMA 环境变量配置（默认 'openmemory'）
    迁移后自检会验证目标 OM schema 是否创建成功，
    失败时返回错误码 OPENMEMORY_SCHEMA_MISSING
"""

import argparse
import sys

from engram.logbook.config import add_config_argument
from engram.logbook.io import (
    add_output_arguments,
    get_output_options,
    output_json,
)

# 从 engram_logbook.migrate 导入核心函数
from engram.logbook.migrate import generate_migration_plan, run_migrate


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
        help="执行权限验证脚本 99_verify_permissions.sql。用于验证角色和权限配置是否正确",
    )

    # 添加 --verify-strict 参数（严格模式，验证失败时抛出异常）
    parser.add_argument(
        "--verify-strict",
        action="store_true",
        default=False,
        help="启用验证严格模式。当验证脚本检测到 FAIL 时抛出异常导致迁移失败。"
        "也可通过环境变量 ENGRAM_VERIFY_STRICT=1 启用",
    )

    # 添加 --plan 参数（查看迁移计划，不连接数据库）
    parser.add_argument(
        "--plan",
        action="store_true",
        default=False,
        help="查看迁移计划，不执行实际迁移。"
        "输出 JSON 格式的 SQL 脚本列表、分类信息和预检结果。"
        "此模式不需要数据库连接。",
    )

    # 添加 --no-precheck 参数（与 --plan 配合使用）
    parser.add_argument(
        "--no-precheck",
        action="store_true",
        default=False,
        help="跳过配置预检（仅在 --plan 模式下有效）",
    )

    # 添加 --sql-dir 参数（特殊打包/兼容场景使用）
    parser.add_argument(
        "--sql-dir",
        type=str,
        default=None,
        help="指定 SQL 文件目录路径。默认使用项目根目录 sql/。仅在特殊打包或兼容场景下使用此参数。",
    )

    # 迁移后回填参数
    parser.add_argument(
        "--post-backfill",
        action="store_true",
        default=False,
        help="迁移完成后执行 evidence_uri 回填（patch_blobs）",
    )
    parser.add_argument(
        "--backfill-chunking-version",
        type=str,
        default=None,
        help="迁移完成后回填 chunking_version（同时处理 patch_blobs 和 attachments）",
    )
    parser.add_argument(
        "--backfill-batch-size",
        type=int,
        default=1000,
        help="backfill 每批处理记录数（默认 1000）",
    )
    parser.add_argument(
        "--backfill-dry-run",
        action="store_true",
        default=False,
        help="backfill dry-run 模式（仅统计不写入）",
    )

    args = parser.parse_args()

    opts = get_output_options(args)

    # 处理 sql_dir 参数
    from pathlib import Path

    sql_dir = Path(args.sql_dir) if args.sql_dir else None

    # --plan 模式：仅查看迁移计划，不连接数据库
    if args.plan:
        result = generate_migration_plan(
            sql_dir=sql_dir,
            apply_roles=args.apply_roles or False,
            apply_openmemory_grants=args.apply_openmemory_grants or False,
            verify=args.verify,
            do_precheck=not args.no_precheck,
        )
        output_json(result, pretty=opts["pretty"])
        sys.exit(0 if result.get("ok") else 1)

    # 正常迁移模式
    result = run_migrate(
        args.config_path,
        quiet=opts["quiet"],
        dsn=args.dsn,
        schema_prefix=args.schema_prefix,
        apply_roles=args.apply_roles,
        apply_openmemory_grants=args.apply_openmemory_grants,
        precheck_only=args.precheck_only,
        verify=args.verify,
        verify_strict=args.verify_strict,
        post_backfill=args.post_backfill,
        backfill_chunking_version=args.backfill_chunking_version,
        backfill_batch_size=args.backfill_batch_size,
        backfill_dry_run=args.backfill_dry_run,
        sql_dir=sql_dir,
    )
    output_json(result, pretty=opts["pretty"])

    # 根据 ok 字段决定退出码
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
