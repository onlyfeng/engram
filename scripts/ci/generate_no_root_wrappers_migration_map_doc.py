#!/usr/bin/env python3
"""
生成 no_root_wrappers_migration_map.md 的迁移映射总览表格。

从 configs/import_migration_map.json 读取数据，更新文档中的表格部分。

Usage:
    python scripts/ci/generate_no_root_wrappers_migration_map_doc.py           # 更新文档
    python scripts/ci/generate_no_root_wrappers_migration_map_doc.py --check   # 仅检查一致性
    python scripts/ci/generate_no_root_wrappers_migration_map_doc.py --dry-run # 打印生成结果
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 文件路径
MIGRATION_MAP_JSON = REPO_ROOT / "configs" / "import_migration_map.json"
MIGRATION_MAP_DOC = REPO_ROOT / "docs" / "architecture" / "no_root_wrappers_migration_map.md"

# 表格标记
TABLE_START_MARKER = "## 迁移映射总览"
TABLE_END_MARKER = "---"


def load_migration_map() -> dict:
    """加载迁移映射 JSON 文件。"""
    with open(MIGRATION_MAP_JSON, encoding="utf-8") as f:
        return json.load(f)


def status_to_display(status: str) -> str:
    """将 status 转换为显示文本。"""
    mapping = {
        "migrated": "根目录已移除",
        "wrapper_exists": "根目录 wrapper",
        "removed": "根目录已移除",
        "preserved": "长期保留",
        "completed": "已完成",
    }
    return mapping.get(status, status)


def derive_migration_action(module: dict) -> str:
    """根据模块信息推导迁移动作。"""
    status = module.get("status", "")
    import_target = module.get("import_target")

    if status == "preserved":
        return "保留"
    if status == "removed":
        return "已移除"
    if status == "migrated":
        return "仅改引用"
    if status == "wrapper_exists":
        # 如果有 import_target 且与 old_module 不同，说明需要移动代码
        if import_target and "materialize_patch_blob" in str(import_target):
            return "移动代码"
        return "仅改引用"
    return "待定"


def generate_table_rows(modules: list[dict]) -> list[str]:
    """生成表格行。"""
    rows = []

    # 仅处理 deprecated=true 的模块
    deprecated_modules = [m for m in modules if m.get("deprecated", False)]

    for module in deprecated_modules:
        old_module = module.get("old_module", "")
        notes = module.get("notes", "")
        cli_target = module.get("cli_target") or module.get("import_target") or ""
        owner = module.get("owner", "@engram-team")
        target_version = module.get("target_version") or ""

        # 状态显示：直接使用 notes 字段
        status_display = notes if notes else status_to_display(module.get("status", ""))

        # 目标显示
        if cli_target:
            # 简化显示长命令
            if "incremental --repo" in str(cli_target):
                target_display = "`engram-scm-sync runner`"
            else:
                target_display = f"`{cli_target}`"
        else:
            target_display = ""

        # 迁移动作
        action = derive_migration_action(module)

        row = f"| `{old_module}` | {status_display} | {target_display} | {action} | {owner} | {target_version} |"
        rows.append(row)

    return rows


def generate_table(modules: list[dict]) -> str:
    """生成完整的表格（包含表头）。"""
    lines = [
        "| 模块名 | 现状 | 目标 | 迁移动作 | Owner | 到期日期 |",
        "|--------|------|------|----------|-------|----------|",
    ]
    lines.extend(generate_table_rows(modules))
    return "\n".join(lines)


def update_document(doc_content: str, new_table: str) -> str:
    """更新文档中的表格部分。"""
    lines = doc_content.split("\n")
    result_lines = []

    in_table_section = False
    table_inserted = False
    skip_until_separator = False

    for i, line in enumerate(lines):
        if TABLE_START_MARKER in line:
            # 找到表格标题
            result_lines.append(line)
            in_table_section = True
            continue

        if in_table_section and not table_inserted:
            # 等待空行后插入表格
            if line.strip() == "":
                result_lines.append(line)
                result_lines.append(new_table)
                table_inserted = True
                skip_until_separator = True
                continue

        if skip_until_separator:
            # 跳过旧表格内容，直到遇到 ---
            if line.strip() == TABLE_END_MARKER:
                result_lines.append("")
                result_lines.append(line)
                skip_until_separator = False
                in_table_section = False
            continue

        result_lines.append(line)

    return "\n".join(result_lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成 no_root_wrappers_migration_map.md 的迁移映射总览表格"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅检查文档是否与 JSON 一致，不修改文件",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="打印生成结果，不修改文件",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出详细信息",
    )
    args = parser.parse_args()

    # 检查文件存在
    if not MIGRATION_MAP_JSON.exists():
        print(f"[ERROR] 找不到迁移映射文件: {MIGRATION_MAP_JSON}", file=sys.stderr)
        return 1

    if not MIGRATION_MAP_DOC.exists():
        print(f"[ERROR] 找不到文档文件: {MIGRATION_MAP_DOC}", file=sys.stderr)
        return 1

    # 加载数据
    migration_map = load_migration_map()
    modules = migration_map.get("modules", [])

    if args.verbose:
        print(f"[INFO] 加载了 {len(modules)} 个模块定义")

    # 生成表格
    new_table = generate_table(modules)

    if args.dry_run:
        print("=== 生成的表格 ===")
        print(new_table)
        print("==================")
        return 0

    # 读取现有文档
    doc_content = MIGRATION_MAP_DOC.read_text(encoding="utf-8")

    # 生成更新后的文档
    updated_content = update_document(doc_content, new_table)

    if args.check:
        # 检查模式：比较差异
        if doc_content == updated_content:
            print("[OK] 文档与 SSOT 一致")
            return 0
        else:
            print("[ERROR] 文档与 SSOT 不一致", file=sys.stderr)
            print("请运行以下命令更新文档:", file=sys.stderr)
            print(
                "  python scripts/ci/generate_no_root_wrappers_migration_map_doc.py",
                file=sys.stderr,
            )
            return 1

    # 写入更新后的文档
    MIGRATION_MAP_DOC.write_text(updated_content, encoding="utf-8")
    print(f"[OK] 已更新文档: {MIGRATION_MAP_DOC}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
