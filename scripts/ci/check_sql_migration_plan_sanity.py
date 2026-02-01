#!/usr/bin/env python3
"""
SQL 迁移计划 Sanity 检查脚本

调用 db_migrate --plan --no-precheck 获取迁移计划，然后执行以下断言：
1. 计划中 DDL/Permission/Verify 分类非空且符合约束
2. sql/ 主目录不包含 99_*.sql（验证脚本应在 sql/verify/）
3. sql/verify/ 仅包含 99 前缀文件
4. 关键脚本存在（基础 schema + roles/grants + 核心 DDL）

用法：
    python scripts/ci/check_sql_migration_plan_sanity.py
    python scripts/ci/check_sql_migration_plan_sanity.py --verbose
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# ============================================================================
# 常量定义（与 migrate.py 中的 SSOT 对齐）
# ============================================================================

# 关键脚本集合 - 必须存在的脚本
REQUIRED_SCRIPTS = {
    # 基础 schema
    "01_logbook_schema.sql",
    # 角色权限
    "04_roles_and_grants.sql",
    "05_openmemory_roles_and_grants.sql",
    # 核心 DDL
    "02_scm_migration.sql",
    "06_scm_sync_runs.sql",
    "07_scm_sync_locks.sql",
    "08_scm_sync_jobs.sql",
}

# 验证脚本前缀（必须在 verify/ 子目录）
VERIFY_PREFIX = "99"


def log_info(msg: str, verbose: bool = False) -> None:
    """打印信息"""
    if verbose:
        print(f"[INFO] {msg}")


def log_ok(msg: str) -> None:
    """打印成功信息"""
    print(f"[OK] {msg}")


def log_error(msg: str) -> None:
    """打印错误信息"""
    print(f"[ERROR] {msg}", file=sys.stderr)


def log_warn(msg: str) -> None:
    """打印警告信息"""
    print(f"[WARN] {msg}")


def get_migration_plan() -> dict:
    """
    调用 db_migrate --plan --no-precheck 获取迁移计划

    Returns:
        迁移计划字典
    """
    cmd = [
        sys.executable,
        "-m",
        "engram.logbook.cli.db_migrate",
        "--plan",
        "--no-precheck",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent.parent,  # 项目根目录
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"获取迁移计划失败 (exit code {result.returncode}):\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"解析迁移计划 JSON 失败: {e}\nOutput: {result.stdout}")


def check_categories_non_empty(plan: dict, verbose: bool = False) -> list[str]:
    """
    检查 DDL/Permission/Verify 分类非空

    Returns:
        错误消息列表
    """
    errors = []

    ddl_files = plan.get("ddl", [])
    permission_files = plan.get("permissions", [])
    verify_files = plan.get("verify", [])

    log_info(f"DDL 脚本数量: {len(ddl_files)}", verbose)
    log_info(f"权限脚本数量: {len(permission_files)}", verbose)
    log_info(f"验证脚本数量: {len(verify_files)}", verbose)

    if not ddl_files:
        errors.append("DDL 脚本分类为空，预期至少包含 01_logbook_schema.sql 等核心脚本")

    if not permission_files:
        errors.append(
            "Permission 脚本分类为空，预期至少包含 04_roles_and_grants.sql"
        )

    if not verify_files:
        errors.append("Verify 脚本分类为空，预期至少包含 sql/verify/99_verify_permissions.sql")

    return errors


def check_script_prefixes(plan: dict, verbose: bool = False) -> list[str]:
    """
    检查脚本前缀分类是否符合约束

    Returns:
        错误消息列表
    """
    errors = []

    script_prefixes = plan.get("script_prefixes", {})
    ddl_prefixes = set(script_prefixes.get("ddl", []))
    permission_prefixes = set(script_prefixes.get("permissions", []))
    verify_prefixes = set(script_prefixes.get("verify", []))

    log_info(f"DDL 前缀: {sorted(ddl_prefixes)}", verbose)
    log_info(f"权限前缀: {sorted(permission_prefixes)}", verbose)
    log_info(f"验证前缀: {sorted(verify_prefixes)}", verbose)

    # 检查 01 必须在 DDL 前缀中
    if "01" not in ddl_prefixes:
        errors.append("DDL 前缀集合应包含 '01'（核心 schema）")

    # 检查 04, 05 必须在 permission 前缀中
    for prefix in ["04", "05"]:
        if prefix not in permission_prefixes:
            errors.append(f"Permission 前缀集合应包含 '{prefix}'")

    # 检查 99 必须在 verify 前缀中
    if "99" not in verify_prefixes:
        errors.append("Verify 前缀集合应包含 '99'")

    # 检查前缀不重叠
    all_prefixes = [ddl_prefixes, permission_prefixes, verify_prefixes]
    for i, p1 in enumerate(all_prefixes):
        for j, p2 in enumerate(all_prefixes):
            if i < j:
                overlap = p1 & p2
                if overlap:
                    errors.append(f"前缀分类重叠: {sorted(overlap)}")

    return errors


def check_verify_directory_structure(sql_dir: Path, verbose: bool = False) -> list[str]:
    """
    检查 sql/ 目录结构约束：
    - sql/ 主目录不包含 99_*.sql
    - sql/verify/ 仅包含 99 前缀文件

    Returns:
        错误消息列表
    """
    errors = []

    # 检查 sql/ 主目录不包含 99_*.sql
    main_dir_99_files = list(sql_dir.glob("99_*.sql"))
    if main_dir_99_files:
        file_names = [f.name for f in main_dir_99_files]
        errors.append(
            f"sql/ 主目录不应包含 99_*.sql 文件，验证脚本应放在 sql/verify/: {file_names}"
        )
    else:
        log_info("sql/ 主目录不包含 99_*.sql ✓", verbose)

    # 检查 sql/verify/ 目录
    verify_dir = sql_dir / "verify"
    if verify_dir.is_dir():
        verify_files = list(verify_dir.glob("*.sql"))
        for f in verify_files:
            if not f.name.startswith(VERIFY_PREFIX):
                errors.append(
                    f"sql/verify/ 目录只能包含 99 前缀文件，发现: {f.name}"
                )
        if not errors:
            log_info(f"sql/verify/ 目录仅包含 99 前缀文件 ({len(verify_files)} 个) ✓", verbose)
    else:
        errors.append("sql/verify/ 目录不存在")

    return errors


def check_required_scripts_exist(plan: dict, verbose: bool = False) -> list[str]:
    """
    检查关键脚本是否存在

    Returns:
        错误消息列表
    """
    errors = []

    # 合并所有脚本路径
    all_files = set()
    for category in ["ddl", "permissions", "verify"]:
        for path_str in plan.get(category, []):
            # 提取文件名
            path = Path(path_str)
            all_files.add(path.name)

    log_info(f"发现脚本总数: {len(all_files)}", verbose)

    # 检查必需脚本
    for required in REQUIRED_SCRIPTS:
        if required in all_files:
            log_info(f"关键脚本存在: {required} ✓", verbose)
        else:
            errors.append(f"缺失关键脚本: {required}")

    return errors


# 允许跨目录的前缀重复（主目录和 verify 子目录之间）
ALLOWED_CROSS_DIR_PREFIX_OVERLAP: set[str] = {"99"}


def check_no_prefix_duplicates(plan: dict, verbose: bool = False) -> list[str]:
    """
    检查是否存在不允许的前缀重复

    同一前缀在同一目录下存在多个文件通常是配置错误。
    允许的例外：99 前缀允许在主目录和 verify 子目录之间重复。

    Returns:
        错误消息列表
    """
    errors = []

    duplicates = plan.get("duplicates", {})

    if not duplicates:
        log_info("无重复前缀 ✓", verbose)
        return errors

    log_info(f"检测到重复前缀数量: {len(duplicates)}", verbose)

    # 检查每个重复前缀
    disallowed_duplicates: dict[str, list[str]] = {}

    for prefix, files in duplicates.items():
        if prefix in ALLOWED_CROSS_DIR_PREFIX_OVERLAP:
            # 检查是否是跨目录的允许重复
            main_dir_files = [f for f in files if not f.startswith("verify/")]
            verify_dir_files = [f for f in files if f.startswith("verify/")]

            # 如果主目录或 verify 目录内部有多个同前缀文件，则不允许
            if len(main_dir_files) > 1 or len(verify_dir_files) > 1:
                disallowed_duplicates[prefix] = files
            else:
                log_info(f"前缀 {prefix} 跨目录重复已允许（主目录 vs verify/）✓", verbose)
        else:
            # 非允许的前缀重复
            disallowed_duplicates[prefix] = files

    if disallowed_duplicates:
        errors.append("检测到不允许的前缀重复")
        for prefix, files in sorted(disallowed_duplicates.items()):
            errors.append(f"  前缀 {prefix}: {files}")

    return errors


def print_duplicate_remediation(plan: dict) -> None:
    """
    打印重复前缀的修复建议
    """
    duplicates = plan.get("duplicates", {})

    if not duplicates:
        return

    # 过滤出不允许的重复
    disallowed = {}
    for prefix, files in duplicates.items():
        if prefix in ALLOWED_CROSS_DIR_PREFIX_OVERLAP:
            main_dir_files = [f for f in files if not f.startswith("verify/")]
            verify_dir_files = [f for f in files if f.startswith("verify/")]
            if len(main_dir_files) > 1 or len(verify_dir_files) > 1:
                disallowed[prefix] = files
        else:
            disallowed[prefix] = files

    if not disallowed:
        return

    print()
    print("=" * 50)
    print("[REMEDIATION] 重复前缀修复建议")
    print("=" * 50)
    print()
    print("检测到以下重复前缀：")
    for prefix, files in sorted(disallowed.items()):
        print(f"  前缀 {prefix}:")
        for f in files:
            print(f"    - {f}")
    print()
    print("修复步骤：")
    print("  1. 不要混用两套编号 - 确保每个前缀在同一目录内只有一个文件")
    print("  2. 用 Git 重置 sql/ 目录 - 如果是意外修改：git checkout -- sql/")
    print("  3. 验证目录隔离 - 99 前缀脚本应仅存在于 sql/verify/ 子目录")
    print()
    print("如果是有意的多文件设计（如 05_openmemory_* 和 05_scm_*），")
    print("请确保它们在 migrate.py 中被正确分类到不同类别。")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="SQL 迁移计划 Sanity 检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细输出",
    )
    parser.add_argument(
        "--sql-dir",
        type=str,
        default=None,
        help="SQL 目录路径（默认自动检测）",
    )

    args = parser.parse_args()

    print("=== SQL 迁移计划 Sanity 检查 ===")
    print()

    all_errors: list[str] = []

    # Step 1: 获取迁移计划
    print("[1/5] 获取迁移计划...")
    try:
        plan = get_migration_plan()
        log_ok("迁移计划获取成功")
        if args.verbose:
            print(f"  sql_dir: {plan.get('sql_dir')}")
            summary = plan.get("summary", {})
            print(f"  总文件数: {summary.get('total_files', 'N/A')}")
            print(f"  DDL: {summary.get('ddl_count', 'N/A')}")
            print(f"  权限: {summary.get('permissions_count', 'N/A')}")
            print(f"  验证: {summary.get('verify_count', 'N/A')}")
            duplicates = plan.get("duplicates", {})
            print(f"  重复前缀: {len(duplicates)} 个")
    except Exception as e:
        log_error(f"获取迁移计划失败: {e}")
        sys.exit(1)

    print()

    # Step 2: 检查分类非空
    print("[2/5] 检查 DDL/Permission/Verify 分类...")
    errors = check_categories_non_empty(plan, args.verbose)
    if errors:
        for err in errors:
            log_error(err)
        all_errors.extend(errors)
    else:
        log_ok("所有分类非空")

    # 检查前缀约束
    errors = check_script_prefixes(plan, args.verbose)
    if errors:
        for err in errors:
            log_error(err)
        all_errors.extend(errors)
    else:
        log_ok("前缀分类符合约束")

    print()

    # Step 3: 检查目录结构
    print("[3/5] 检查目录结构约束...")
    sql_dir_path = Path(args.sql_dir) if args.sql_dir else Path(plan.get("sql_dir", "sql"))
    errors = check_verify_directory_structure(sql_dir_path, args.verbose)
    if errors:
        for err in errors:
            log_error(err)
        all_errors.extend(errors)
    else:
        log_ok("目录结构符合约束")

    print()

    # Step 4: 检查关键脚本存在
    print("[4/5] 检查关键脚本存在性...")
    errors = check_required_scripts_exist(plan, args.verbose)
    if errors:
        for err in errors:
            log_error(err)
        all_errors.extend(errors)
    else:
        log_ok("所有关键脚本存在")

    print()

    # Step 5: 检查重复前缀
    print("[5/5] 检查重复前缀...")
    errors = check_no_prefix_duplicates(plan, args.verbose)
    if errors:
        for err in errors:
            log_error(err)
        all_errors.extend(errors)
        # 打印修复建议
        print_duplicate_remediation(plan)
    else:
        log_ok("无不允许的重复前缀")

    print()

    # 汇总结果
    if all_errors:
        print("=" * 50)
        log_error(f"检查失败，共 {len(all_errors)} 个错误")
        print("=" * 50)
        sys.exit(1)
    else:
        print("=" * 50)
        log_ok("SQL 迁移计划 Sanity 检查通过")
        print("=" * 50)
        sys.exit(0)


if __name__ == "__main__":
    main()
