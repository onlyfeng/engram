#!/usr/bin/env python3
"""
SQL 迁移清单一致性验证脚本

扫描 sql/ 和 sql/verify/ 目录生成当前编号清单，
与 docs/logbook/sql_renumbering_map.json (SSOT) 和 docs/logbook/sql_file_inventory.md 做一致性对比。

检查项：
1. JSON 未覆盖前缀：检查实际文件是否都在 JSON renumbering_map 中记录
2. MD 与 JSON 不一致：检查 Markdown 表格与 JSON 的 new_prefix/new_path/status 是否一致
3. 旧文件残留：检查 deprecated_files 中的旧文件是否仍然存在
4. verify 目录约束违规：检查 99 前缀是否在 sql/verify/ 目录

用法：
    python scripts/verify_sql_migration_inventory.py
    python scripts/verify_sql_migration_inventory.py --verbose
    python scripts/verify_sql_migration_inventory.py --output report.json

门禁模式（CI 使用）：
    python scripts/verify_sql_migration_inventory.py --strict
    # 任何错误或警告都会导致退出码非零

文档生成模式：
    python scripts/verify_sql_migration_inventory.py --emit-inventory-md
    # 从 JSON SSOT 生成 sql_file_inventory.md 的表格段

    python scripts/verify_sql_migration_inventory.py --emit-renumbering-md
    # 从 JSON SSOT 生成 sql_renumbering_map.md 的对照表段
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ============================================================================
# 常量定义
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent
SQL_DIR = PROJECT_ROOT / "sql"
VERIFY_DIR = SQL_DIR / "verify"
INVENTORY_DOC = PROJECT_ROOT / "docs" / "logbook" / "sql_file_inventory.md"
RENUMBERING_DOC = PROJECT_ROOT / "docs" / "logbook" / "sql_renumbering_map.md"
RENUMBERING_JSON = PROJECT_ROOT / "docs" / "logbook" / "sql_renumbering_map.json"

# 分类前缀定义（与 migrate.py 保持一致）
# 这些常量必须与 src/engram/logbook/migrate.py 中的定义一致
# 脚本启动时会进行一致性断言检查（若能导入 engram.logbook.migrate）
DDL_SCRIPT_PREFIXES = {"01", "02", "03", "06", "07", "08", "09", "11", "12", "13", "14"}
PERMISSION_SCRIPT_PREFIXES = {"04", "05"}
VERIFY_SCRIPT_PREFIXES = {"99"}

# 已知废弃的前缀
DEPRECATED_PREFIXES = {"10"}


# ============================================================================
# 前缀常量一致性检查（与 migrate.py 的 SSOT）
# ============================================================================


def verify_prefix_constants_consistency() -> tuple[bool, list[str]]:
    """
    验证本脚本的前缀常量与 engram.logbook.migrate 模块中的定义一致。

    这是防止"迁移执行逻辑已更新但 inventory 检查仍按旧分类"漂移的关键检查。

    Returns:
        (ok, error_messages) - ok 为 True 表示一致，error_messages 为不一致项列表
    """
    errors: list[str] = []

    try:
        from engram.logbook.migrate import (
            DDL_SCRIPT_PREFIXES as MIGRATE_DDL,
        )
        from engram.logbook.migrate import (
            PERMISSION_SCRIPT_PREFIXES as MIGRATE_PERMISSION,
        )
        from engram.logbook.migrate import (
            VERIFY_SCRIPT_PREFIXES as MIGRATE_VERIFY,
        )
    except ImportError as e:
        # 无法导入时返回警告，但不阻断（允许在未安装 engram 的环境运行）
        return True, [f"[WARN] 无法导入 engram.logbook.migrate，跳过前缀一致性检查: {e}"]

    # 比对 DDL_SCRIPT_PREFIXES
    if DDL_SCRIPT_PREFIXES != MIGRATE_DDL:
        local_only = DDL_SCRIPT_PREFIXES - MIGRATE_DDL
        migrate_only = MIGRATE_DDL - DDL_SCRIPT_PREFIXES
        errors.append(
            f"DDL_SCRIPT_PREFIXES 不一致:\n"
            f"  本脚本: {sorted(DDL_SCRIPT_PREFIXES)}\n"
            f"  migrate.py: {sorted(MIGRATE_DDL)}\n"
            f"  仅本脚本有: {sorted(local_only) if local_only else '无'}\n"
            f"  仅 migrate.py 有: {sorted(migrate_only) if migrate_only else '无'}"
        )

    # 比对 PERMISSION_SCRIPT_PREFIXES
    if PERMISSION_SCRIPT_PREFIXES != MIGRATE_PERMISSION:
        local_only = PERMISSION_SCRIPT_PREFIXES - MIGRATE_PERMISSION
        migrate_only = MIGRATE_PERMISSION - PERMISSION_SCRIPT_PREFIXES
        errors.append(
            f"PERMISSION_SCRIPT_PREFIXES 不一致:\n"
            f"  本脚本: {sorted(PERMISSION_SCRIPT_PREFIXES)}\n"
            f"  migrate.py: {sorted(MIGRATE_PERMISSION)}\n"
            f"  仅本脚本有: {sorted(local_only) if local_only else '无'}\n"
            f"  仅 migrate.py 有: {sorted(migrate_only) if migrate_only else '无'}"
        )

    # 比对 VERIFY_SCRIPT_PREFIXES
    if VERIFY_SCRIPT_PREFIXES != MIGRATE_VERIFY:
        local_only = VERIFY_SCRIPT_PREFIXES - MIGRATE_VERIFY
        migrate_only = MIGRATE_VERIFY - VERIFY_SCRIPT_PREFIXES
        errors.append(
            f"VERIFY_SCRIPT_PREFIXES 不一致:\n"
            f"  本脚本: {sorted(VERIFY_SCRIPT_PREFIXES)}\n"
            f"  migrate.py: {sorted(MIGRATE_VERIFY)}\n"
            f"  仅本脚本有: {sorted(local_only) if local_only else '无'}\n"
            f"  仅 migrate.py 有: {sorted(migrate_only) if migrate_only else '无'}"
        )

    if errors:
        errors.append(
            "\n修复方法: 同步更新 scripts/verify_sql_migration_inventory.py 中的前缀常量，"
            "使其与 src/engram/logbook/migrate.py 保持一致。"
        )

    return len(errors) == 0, errors


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class SqlFileEntry:
    """SQL 文件条目"""

    prefix: str
    filename: str
    directory: str  # "main" 或 "verify"
    category: str  # "DDL", "Permission", "Verify"

    @property
    def full_path(self) -> str:
        if self.directory == "verify":
            return f"verify/{self.filename}"
        return self.filename


@dataclass
class DocEntry:
    """文档中记录的条目"""

    prefix: str
    filename: str
    domain: str
    file_type: str
    deprecated: bool = False
    directory: str = "main"  # "main" 或 "verify"


@dataclass
class DeprecatedFileEntry:
    """废弃文件条目（旧文件名）"""

    old_path: str
    new_path: str
    status: str  # "integrated", "renamed", "relocated"
    reason: str
    action: str


@dataclass
class RenumberingMapEntry:
    """重编号映射表条目"""

    old_prefix: str  # 旧编号，"-" 表示新增
    old_path: str | None  # 旧路径，None 表示新增
    new_prefix: str
    new_path: str  # 新路径（含目录前缀如 verify/）
    status: str  # retained, integrated, renamed, relocated, added
    notes: str  # 人类可读说明

    @property
    def old_filename(self) -> str:
        """从 old_path 提取文件名（向后兼容）"""
        if self.old_path is None:
            return ""
        return Path(self.old_path).name

    @property
    def new_filename(self) -> str:
        """从 new_path 提取文件名"""
        return Path(self.new_path).name

    @property
    def new_directory(self) -> str:
        """从 new_path 判断目录位置"""
        if "verify/" in self.new_path:
            return "verify"
        return "main"


@dataclass
class ConsistencyReport:
    """一致性检查报告"""

    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # 详细信息
    actual_files: list[SqlFileEntry] = field(default_factory=list)
    doc_entries: list[DocEntry] = field(default_factory=list)

    # 不一致项
    missing_in_dir: list[str] = field(default_factory=list)  # 文档有，目录没有
    missing_in_doc: list[str] = field(default_factory=list)  # 目录有，文档没有
    mismatched_entries: list[dict[str, Any]] = field(default_factory=list)  # 信息不匹配

    # 旧文件检测
    deprecated_files_found: list[dict[str, Any]] = field(default_factory=list)  # 检测到的旧文件

    # renumbering map 检查结果
    renumbering_missing_prefixes: list[str] = field(default_factory=list)  # 映射表未覆盖的前缀
    renumbering_filename_mismatches: list[dict[str, Any]] = field(
        default_factory=list
    )  # 文件名不匹配
    verify_directory_violations: list[str] = field(default_factory=list)  # verify 目录违规

    # MD/JSON 一致性检查结果
    md_json_inconsistencies: list[dict[str, Any]] = field(default_factory=list)  # MD 与 JSON 不一致

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "summary": {
                "actual_file_count": len(self.actual_files),
                "doc_entry_count": len(self.doc_entries),
                "missing_in_dir": len(self.missing_in_dir),
                "missing_in_doc": len(self.missing_in_doc),
                "mismatched_entries": len(self.mismatched_entries),
                "deprecated_files_found": len(self.deprecated_files_found),
                "renumbering_missing_prefixes": len(self.renumbering_missing_prefixes),
                "renumbering_filename_mismatches": len(self.renumbering_filename_mismatches),
                "verify_directory_violations": len(self.verify_directory_violations),
                "md_json_inconsistencies": len(self.md_json_inconsistencies),
            },
            "missing_in_dir": self.missing_in_dir,
            "missing_in_doc": self.missing_in_doc,
            "mismatched_entries": self.mismatched_entries,
            "deprecated_files_found": self.deprecated_files_found,
            "renumbering_missing_prefixes": self.renumbering_missing_prefixes,
            "renumbering_filename_mismatches": self.renumbering_filename_mismatches,
            "verify_directory_violations": self.verify_directory_violations,
            "md_json_inconsistencies": self.md_json_inconsistencies,
            "actual_files": [
                {
                    "prefix": f.prefix,
                    "filename": f.filename,
                    "directory": f.directory,
                    "category": f.category,
                }
                for f in self.actual_files
            ],
        }


# ============================================================================
# 扫描函数
# ============================================================================


def classify_prefix(prefix: str) -> str:
    """根据前缀分类脚本类型"""
    if prefix in DDL_SCRIPT_PREFIXES:
        return "DDL"
    elif prefix in PERMISSION_SCRIPT_PREFIXES:
        return "Permission"
    elif prefix in VERIFY_SCRIPT_PREFIXES:
        return "Verify"
    else:
        return "Unknown"


def scan_sql_directory() -> list[SqlFileEntry]:
    """
    扫描 sql/ 和 sql/verify/ 目录，生成文件清单

    Returns:
        SqlFileEntry 列表，按 (prefix数值, filename) 排序
    """
    entries: list[SqlFileEntry] = []

    # 扫描主目录
    if SQL_DIR.is_dir():
        for sql_file in SQL_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                prefix = match.group(1)
                entries.append(
                    SqlFileEntry(
                        prefix=prefix,
                        filename=sql_file.name,
                        directory="main",
                        category=classify_prefix(prefix),
                    )
                )

    # 扫描 verify 子目录
    if VERIFY_DIR.is_dir():
        for sql_file in VERIFY_DIR.glob("*.sql"):
            match = re.match(r"^(\d{2})_", sql_file.name)
            if match:
                prefix = match.group(1)
                entries.append(
                    SqlFileEntry(
                        prefix=prefix,
                        filename=sql_file.name,
                        directory="verify",
                        category=classify_prefix(prefix),
                    )
                )

    # 排序：按前缀数值升序，然后按文件名字典序
    entries.sort(key=lambda e: (int(e.prefix), e.filename))

    return entries


# ============================================================================
# 文档解析函数
# ============================================================================


def parse_inventory_doc() -> list[DocEntry]:
    """
    解析 sql_file_inventory.md 中的文件清单表格

    Returns:
        DocEntry 列表
    """
    if not INVENTORY_DOC.exists():
        return []

    content = INVENTORY_DOC.read_text(encoding="utf-8")
    entries: list[DocEntry] = []

    # 匹配表格行：| 前缀 | 文件名 | 功能域 | 类型 | 说明 |
    # 格式示例：| 01 | 01_logbook_schema.sql | Core | DDL | 核心 schema 与表定义 |
    # 支持废弃标记：| ~~10~~ | -- 已废弃 | - | - | 编号保留，不再使用 |
    table_pattern = re.compile(
        r"^\|\s*(?:~~)?(\d{2})(?:~~)?\s*\|\s*([^\|]+)\s*\|\s*([^\|]+)\s*\|\s*([^\|]+)\s*\|",
        re.MULTILINE,
    )

    for match in table_pattern.finditer(content):
        prefix = match.group(1)
        filename = match.group(2).strip()
        domain = match.group(3).strip()
        file_type = match.group(4).strip()

        # 跳过表头和分隔行
        if filename.startswith("-") or filename == "文件名":
            continue

        # 检测废弃标记
        is_deprecated = "~~" in match.group(0) or "已废弃" in filename

        # 检测是否在 verify 子目录
        directory = "main"
        if "verify/" in filename:
            directory = "verify"
            filename = filename.replace("verify/", "")

        entries.append(
            DocEntry(
                prefix=prefix,
                filename=filename,
                domain=domain,
                file_type=file_type,
                deprecated=is_deprecated,
                directory=directory,
            )
        )

    return entries


def parse_renumbering_doc() -> dict[str, dict[str, Any]]:
    """
    解析 sql_renumbering_map.md 中的编号对照表

    Returns:
        {new_prefix: {"old_prefix": str, "status": str, "filename": str}, ...}
    """
    if not RENUMBERING_DOC.exists():
        return {}

    content = RENUMBERING_DOC.read_text(encoding="utf-8")
    mapping: dict[str, dict[str, Any]] = {}

    # 匹配编号对照表：| 旧编号 | 旧文件名 | 新编号 | 新文件名 | 状态 |
    # 格式示例：| 05 | 05_scm_sync_runs.sql | 06 | 06_scm_sync_runs.sql | **整合** |
    table_pattern = re.compile(
        r"^\|\s*(?:-|(\d{2}))\s*\|\s*([^\|]*)\s*\|\s*(\d{2})\s*\|\s*([^\|]+)\s*\|\s*([^\|]+)\s*\|",
        re.MULTILINE,
    )

    for match in table_pattern.finditer(content):
        old_prefix = match.group(1) or "-"
        old_filename = match.group(2).strip()
        new_prefix = match.group(3)
        new_filename = match.group(4).strip()
        status = match.group(5).strip()

        # 跳过表头
        if new_filename == "新文件名" or "---" in new_filename:
            continue

        mapping[new_prefix] = {
            "old_prefix": old_prefix,
            "old_filename": old_filename,
            "new_filename": new_filename,
            "status": status,
        }

    return mapping


def parse_renumbering_doc_entries() -> list[RenumberingMapEntry]:
    """
    解析 sql_renumbering_map.md 中的编号对照表，返回结构化条目列表
    （降级模式：当 JSON 不可用时使用）

    Returns:
        RenumberingMapEntry 列表
    """
    if not RENUMBERING_DOC.exists():
        return []

    content = RENUMBERING_DOC.read_text(encoding="utf-8")
    entries: list[RenumberingMapEntry] = []

    # 匹配编号对照表：| 旧编号 | 旧文件名 | 新编号 | 新文件名 | 状态 |
    # 格式示例：| 05 | 05_scm_sync_runs.sql | 06 | 06_scm_sync_runs.sql | **整合** |
    # 支持 verify/ 前缀：| 99 | 99_verify_permissions.sql | 99 | verify/99_verify_permissions.sql | **迁移到子目录** |
    table_pattern = re.compile(
        r"^\|\s*(?:-|(\d{2}))\s*\|\s*([^\|]*)\s*\|\s*(\d{2})\s*\|\s*([^\|]+)\s*\|\s*([^\|]+)\s*\|",
        re.MULTILINE,
    )

    for match in table_pattern.finditer(content):
        old_prefix = match.group(1) or "-"
        old_filename = match.group(2).strip()
        new_prefix = match.group(3)
        new_filename = match.group(4).strip()
        status = match.group(5).strip()

        # 跳过表头
        if new_filename == "新文件名" or "---" in new_filename:
            continue

        # 清理状态字符串（去掉 markdown 加粗符号）
        status_clean = status.replace("**", "").replace("*", "").strip()

        # 从 Markdown 构建完整路径
        old_path: str | None = None
        if old_filename and old_filename != "（新增）":
            old_path = f"sql/{old_filename}"

        # 新路径：处理 verify/ 前缀
        if new_filename.startswith("verify/"):
            new_path = f"sql/{new_filename}"
        else:
            new_path = f"sql/{new_filename}"

        # 映射状态到机器标识
        status_map = {
            "保留": "retained",
            "保留（修改）": "retained",
            "整合": "integrated",
            "重命名": "renamed",
            "新增": "added",
            "迁移到子目录": "relocated",
        }
        status_code = status_map.get(status_clean, status_clean)

        entries.append(
            RenumberingMapEntry(
                old_prefix=old_prefix,
                old_path=old_path,
                new_prefix=new_prefix,
                new_path=new_path,
                status=status_code,
                notes=status_clean,
            )
        )

    return entries


# ============================================================================
# 旧文件检测函数
# ============================================================================


def load_deprecated_files_mapping() -> list[DeprecatedFileEntry]:
    """
    从 sql_renumbering_map.json 加载废弃文件映射表。

    Returns:
        DeprecatedFileEntry 列表
    """
    if not RENUMBERING_JSON.exists():
        return []

    try:
        data = json.loads(RENUMBERING_JSON.read_text(encoding="utf-8"))
        entries = []
        for item in data.get("deprecated_files", []):
            entries.append(
                DeprecatedFileEntry(
                    old_path=item.get("old_path", ""),
                    new_path=item.get("new_path", ""),
                    status=item.get("status", ""),
                    reason=item.get("reason", ""),
                    action=item.get("action", ""),
                )
            )
        return entries
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[WARN] 解析 {RENUMBERING_JSON} 失败: {e}")
        return []


def load_renumbering_map_from_json() -> list[RenumberingMapEntry]:
    """
    从 sql_renumbering_map.json 加载完整的重编号映射表（SSOT）。

    Returns:
        RenumberingMapEntry 列表
    """
    if not RENUMBERING_JSON.exists():
        return []

    try:
        data = json.loads(RENUMBERING_JSON.read_text(encoding="utf-8"))
        entries = []
        for item in data.get("renumbering_map", []):
            entries.append(
                RenumberingMapEntry(
                    old_prefix=item.get("old_prefix", "-"),
                    old_path=item.get("old_path"),
                    new_prefix=item.get("new_prefix", ""),
                    new_path=item.get("new_path", ""),
                    status=item.get("status", ""),
                    notes=item.get("notes", ""),
                )
            )
        return entries
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[WARN] 解析 {RENUMBERING_JSON} renumbering_map 失败: {e}")
        return []


def check_deprecated_files(
    deprecated_mapping: list[DeprecatedFileEntry],
) -> list[dict[str, Any]]:
    """
    检测是否存在已废弃的旧文件。

    Args:
        deprecated_mapping: 废弃文件映射列表

    Returns:
        检测到的旧文件列表，每项包含 old_path, new_path, status, action
    """
    found = []
    for entry in deprecated_mapping:
        old_file = PROJECT_ROOT / entry.old_path
        if old_file.exists():
            found.append(
                {
                    "old_path": entry.old_path,
                    "new_path": entry.new_path,
                    "status": entry.status,
                    "reason": entry.reason,
                    "action": entry.action,
                }
            )
    return found


# ============================================================================
# Renumbering Map 检查函数
# ============================================================================


def check_renumbering_map_coverage(
    actual_files: list[SqlFileEntry],
    renumbering_entries: list[RenumberingMapEntry],
    verbose: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    检查 renumbering map 是否覆盖所有现存前缀，并验证文件名匹配

    Args:
        actual_files: 扫描得到的实际文件列表
        renumbering_entries: renumbering map 条目列表
        verbose: 是否输出详细信息

    Returns:
        (missing_prefixes, filename_mismatches) 元组
    """
    missing_prefixes: list[str] = []
    filename_mismatches: list[dict[str, Any]] = []

    # 构建实际文件索引: prefix -> list of (filename, directory)
    actual_by_prefix: dict[str, list[tuple[str, str]]] = {}
    for f in actual_files:
        if f.prefix not in actual_by_prefix:
            actual_by_prefix[f.prefix] = []
        actual_by_prefix[f.prefix].append((f.filename, f.directory))

    # 构建 renumbering map 索引: new_prefix -> entry
    map_by_new_prefix: dict[str, RenumberingMapEntry] = {}
    for entry in renumbering_entries:
        map_by_new_prefix[entry.new_prefix] = entry

    # 检查每个实际前缀是否在 renumbering map 中有记录
    for prefix in sorted(actual_by_prefix.keys(), key=lambda p: int(p)):
        # 跳过已废弃的前缀
        if prefix in DEPRECATED_PREFIXES:
            continue

        if prefix not in map_by_new_prefix:
            missing_prefixes.append(prefix)
            if verbose:
                print(f"[DEBUG] 前缀 {prefix} 未在 renumbering map 中记录")
            continue

        # 检查文件名是否匹配
        entry = map_by_new_prefix[prefix]
        expected_filename = entry.new_filename
        expected_directory = entry.new_directory

        for actual_filename, actual_directory in actual_by_prefix[prefix]:
            if actual_filename != expected_filename:
                filename_mismatches.append(
                    {
                        "prefix": prefix,
                        "actual_filename": actual_filename,
                        "expected_filename": expected_filename,
                        "actual_directory": actual_directory,
                        "expected_directory": expected_directory,
                    }
                )
                if verbose:
                    print(
                        f"[DEBUG] 前缀 {prefix}: 文件名不匹配 "
                        f"(实际: {actual_filename}, 期望: {expected_filename})"
                    )
            elif actual_directory != expected_directory:
                filename_mismatches.append(
                    {
                        "prefix": prefix,
                        "actual_filename": actual_filename,
                        "expected_filename": expected_filename,
                        "actual_directory": actual_directory,
                        "expected_directory": expected_directory,
                        "issue": "directory_mismatch",
                    }
                )
                if verbose:
                    print(
                        f"[DEBUG] 前缀 {prefix}: 目录不匹配 "
                        f"(实际: {actual_directory}, 期望: {expected_directory})"
                    )

    return missing_prefixes, filename_mismatches


def check_verify_directory_constraints(
    actual_files: list[SqlFileEntry],
    verbose: bool = False,
) -> list[str]:
    """
    检查 verify 目录约束：
    1. 99 前缀必须在 sql/verify/ 目录
    2. sql/verify/ 仅允许 99 前缀

    Args:
        actual_files: 扫描得到的实际文件列表
        verbose: 是否输出详细信息

    Returns:
        违规项列表
    """
    violations: list[str] = []

    for f in actual_files:
        # 规则 1: 99 前缀必须在 verify 目录
        if f.prefix == "99" and f.directory != "verify":
            violation = f"前缀 99 必须位于 sql/verify/ 目录，但发现在主目录: {f.filename}"
            violations.append(violation)
            if verbose:
                print(f"[DEBUG] {violation}")

        # 规则 2: verify 目录仅允许 99 前缀
        if f.directory == "verify" and f.prefix != "99":
            violation = f"sql/verify/ 目录仅允许 99 前缀，但发现: {f.prefix}_{f.filename}"
            violations.append(violation)
            if verbose:
                print(f"[DEBUG] {violation}")

    return violations


def check_md_json_consistency(
    json_entries: list[RenumberingMapEntry],
    md_entries: list[RenumberingMapEntry],
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """
    检查 Markdown 表格与 JSON 中的 renumbering_map 一致性。

    比较维度：new_prefix, new_path, status

    Args:
        json_entries: 从 JSON 加载的条目（SSOT）
        md_entries: 从 Markdown 解析的条目
        verbose: 是否输出详细信息

    Returns:
        不一致项列表，每项包含 new_prefix, field, json_value, md_value
    """
    inconsistencies: list[dict[str, Any]] = []

    # 构建索引：按 new_prefix 分组
    json_by_prefix: dict[str, RenumberingMapEntry] = {}
    for e in json_entries:
        json_by_prefix[e.new_prefix] = e

    md_by_prefix: dict[str, RenumberingMapEntry] = {}
    for e in md_entries:
        md_by_prefix[e.new_prefix] = e

    # 检查 JSON 中有但 MD 中没有的前缀
    for prefix in json_by_prefix:
        if prefix not in md_by_prefix:
            inconsistencies.append(
                {
                    "new_prefix": prefix,
                    "field": "存在性",
                    "json_value": f"存在 ({json_by_prefix[prefix].new_path})",
                    "md_value": "不存在",
                    "issue": "json_only",
                }
            )
            if verbose:
                print(f"[DEBUG] 前缀 {prefix}: JSON 中存在但 MD 中不存在")

    # 检查 MD 中有但 JSON 中没有的前缀
    for prefix in md_by_prefix:
        if prefix not in json_by_prefix:
            inconsistencies.append(
                {
                    "new_prefix": prefix,
                    "field": "存在性",
                    "json_value": "不存在",
                    "md_value": f"存在 ({md_by_prefix[prefix].new_path})",
                    "issue": "md_only",
                }
            )
            if verbose:
                print(f"[DEBUG] 前缀 {prefix}: MD 中存在但 JSON 中不存在")

    # 检查两边都有的前缀，比较字段
    for prefix in json_by_prefix:
        if prefix not in md_by_prefix:
            continue

        json_e = json_by_prefix[prefix]
        md_e = md_by_prefix[prefix]

        # 比较 new_path
        if json_e.new_path != md_e.new_path:
            inconsistencies.append(
                {
                    "new_prefix": prefix,
                    "field": "new_path",
                    "json_value": json_e.new_path,
                    "md_value": md_e.new_path,
                    "issue": "value_mismatch",
                }
            )
            if verbose:
                print(
                    f"[DEBUG] 前缀 {prefix}: new_path 不匹配 "
                    f"(JSON: {json_e.new_path}, MD: {md_e.new_path})"
                )

        # 比较 status（需要规范化比较）
        if json_e.status != md_e.status:
            inconsistencies.append(
                {
                    "new_prefix": prefix,
                    "field": "status",
                    "json_value": json_e.status,
                    "md_value": md_e.status,
                    "issue": "value_mismatch",
                }
            )
            if verbose:
                print(
                    f"[DEBUG] 前缀 {prefix}: status 不匹配 "
                    f"(JSON: {json_e.status}, MD: {md_e.status})"
                )

    return inconsistencies


# ============================================================================
# 一致性检查函数
# ============================================================================


def check_consistency(
    actual_files: list[SqlFileEntry],
    doc_entries: list[DocEntry],
    verbose: bool = False,
) -> ConsistencyReport:
    """
    检查实际文件与文档记录的一致性

    Args:
        actual_files: 扫描得到的实际文件列表
        doc_entries: 文档中记录的条目列表
        verbose: 是否输出详细信息

    Returns:
        ConsistencyReport 一致性检查报告
    """
    report = ConsistencyReport()
    report.actual_files = actual_files
    report.doc_entries = doc_entries

    # 构建查找索引
    actual_by_prefix: dict[str, list[SqlFileEntry]] = {}
    for f in actual_files:
        if f.prefix not in actual_by_prefix:
            actual_by_prefix[f.prefix] = []
        actual_by_prefix[f.prefix].append(f)

    doc_by_prefix: dict[str, list[DocEntry]] = {}
    for e in doc_entries:
        if e.prefix not in doc_by_prefix:
            doc_by_prefix[e.prefix] = []
        doc_by_prefix[e.prefix].append(e)

    # 收集所有前缀
    all_prefixes = set(actual_by_prefix.keys()) | set(doc_by_prefix.keys())

    for prefix in sorted(all_prefixes, key=lambda p: int(p)):
        actual_list = actual_by_prefix.get(prefix, [])
        doc_list = doc_by_prefix.get(prefix, [])

        # 过滤掉废弃的文档条目（用于比较）
        doc_non_deprecated = [d for d in doc_list if not d.deprecated]

        if verbose:
            print(
                f"[DEBUG] 前缀 {prefix}: 实际 {len(actual_list)} 个, 文档 {len(doc_non_deprecated)} 个"
            )

        # 情况 1: 文档有，目录没有（非废弃）
        if doc_non_deprecated and not actual_list:
            for doc in doc_non_deprecated:
                msg = f"前缀 {prefix}: 文档记录 {doc.filename}，但目录中不存在"
                report.missing_in_dir.append(msg)
                report.add_error(msg)

        # 情况 2: 目录有，文档没有
        if actual_list and not doc_list:
            for actual in actual_list:
                msg = f"前缀 {prefix}: 目录存在 {actual.full_path}，但文档中未记录"
                report.missing_in_doc.append(msg)
                report.add_error(msg)

        # 情况 3: 都有，检查文件名是否匹配
        if actual_list and doc_non_deprecated:
            actual_names = {f.filename for f in actual_list}
            doc_names = {d.filename for d in doc_non_deprecated}

            # 文档有但目录没有的文件名
            for name in doc_names - actual_names:
                msg = f"前缀 {prefix}: 文档记录 {name}，但目录中不存在该文件"
                report.missing_in_dir.append(msg)
                report.add_error(msg)

            # 目录有但文档没有的文件名
            for name in actual_names - doc_names:
                msg = f"前缀 {prefix}: 目录存在 {name}，但文档中未记录"
                report.missing_in_doc.append(msg)
                report.add_error(msg)

            # 检查分类是否匹配
            for actual in actual_list:
                for doc in doc_non_deprecated:
                    if actual.filename == doc.filename:
                        # 检查目录位置
                        if actual.directory != doc.directory:
                            report.mismatched_entries.append(
                                {
                                    "prefix": prefix,
                                    "filename": actual.filename,
                                    "issue": "目录位置不匹配",
                                    "actual": actual.directory,
                                    "expected": doc.directory,
                                }
                            )
                            report.add_error(
                                f"前缀 {prefix}: {actual.filename} 目录位置不匹配 "
                                f"(实际: {actual.directory}, 文档: {doc.directory})"
                            )

        # 情况 4: 废弃前缀但目录中存在文件
        deprecated_docs = [d for d in doc_list if d.deprecated]
        if deprecated_docs and actual_list:
            for actual in actual_list:
                msg = f"前缀 {prefix}: 文档标记为废弃，但目录中存在文件 {actual.full_path}"
                report.add_warning(msg)

    # 检查前缀连续性（仅对已使用范围内的缺口警告）
    # 只检查 01-19 范围内的缺口（Feature DDL 范围），不检查预留范围
    actual_prefixes = sorted(actual_by_prefix.keys(), key=lambda p: int(p))
    if actual_prefixes:
        # 只检查到已使用的最大前缀
        max_used = max(int(p) for p in actual_prefixes if int(p) < 20)  # 排除 99
        for i in range(1, max_used + 1):
            prefix_str = f"{i:02d}"
            if (
                prefix_str not in actual_by_prefix
                and prefix_str not in DEPRECATED_PREFIXES
                and prefix_str not in doc_by_prefix
            ):
                # 只对 DDL/Permission 范围（01-19）内未记录的缺口警告
                report.add_warning(f"前缀 {prefix_str} 缺失（未在文档中记录为废弃）")

    return report


# ============================================================================
# 输出函数
# ============================================================================


def log_info(msg: str) -> None:
    print(f"[INFO] {msg}")


def log_ok(msg: str) -> None:
    print(f"[OK] {msg}")


def log_error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def log_warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def print_inventory(entries: list[SqlFileEntry], title: str = "SQL 文件清单") -> None:
    """打印文件清单"""
    print(f"\n=== {title} ===")
    print(f"{'前缀':<6} {'文件名':<50} {'目录':<10} {'分类':<12}")
    print("-" * 80)
    for e in entries:
        print(f"{e.prefix:<6} {e.filename:<50} {e.directory:<10} {e.category:<12}")
    print(f"\n总计: {len(entries)} 个文件")


# ============================================================================
# 文档生成标记区块定义
# ============================================================================

# sql_file_inventory.md 的表格标记
INVENTORY_TABLE_BEGIN = "<!-- BEGIN_INVENTORY_TABLE -->"
INVENTORY_TABLE_END = "<!-- END_INVENTORY_TABLE -->"

# sql_renumbering_map.md 的对照表标记
RENUMBERING_TABLE_BEGIN = "<!-- BEGIN_RENUMBERING_TABLE -->"
RENUMBERING_TABLE_END = "<!-- END_RENUMBERING_TABLE -->"


# ============================================================================
# 文档生成函数
# ============================================================================


def generate_inventory_table(
    actual_files: list[SqlFileEntry],
    renumbering_entries: list[RenumberingMapEntry],
) -> str:
    """
    生成 sql_file_inventory.md 的文件清单表格内容。

    Args:
        actual_files: 扫描得到的实际文件列表
        renumbering_entries: renumbering map 条目列表

    Returns:
        表格的 Markdown 文本（不含标记区块）
    """
    # 注意：renumbering_entries 参数保留用于未来扩展（如添加额外元数据）
    _ = renumbering_entries

    lines: list[str] = []

    # 表头
    lines.append("| 前缀 | 文件名 | 功能域 | 类型 | 说明 |")
    lines.append("|-----|-------|--------|------|------|")

    # 功能域和类型映射（基于前缀分类）
    def get_domain_and_type(prefix: str, filename: str) -> tuple[str, str]:
        """根据前缀和文件名推断功能域和类型"""
        if prefix in DDL_SCRIPT_PREFIXES:
            file_type = "DDL"
        elif prefix in PERMISSION_SCRIPT_PREFIXES:
            file_type = "Permission"
        elif prefix in VERIFY_SCRIPT_PREFIXES:
            file_type = "Verify"
        else:
            file_type = "-"

        # 功能域推断（基于文件名关键词）
        fname_lower = filename.lower()
        if "logbook" in fname_lower or "schema" in fname_lower:
            domain = "Core"
        elif "sync" in fname_lower:
            # sync_runs, sync_locks, sync_jobs, sync_jobs_dimension_columns 都属于 SCM Sync
            domain = "SCM Sync"
        elif "scm" in fname_lower:
            domain = "SCM"
        elif "pgvector" in fname_lower or "extension" in fname_lower:
            domain = "Extension"
        elif "role" in fname_lower or "grant" in fname_lower:
            if "openmemory" in fname_lower:
                domain = "OpenMemory"
            else:
                domain = "Roles/Grants"
        elif "governance" in fname_lower or "audit" in fname_lower:
            domain = "Governance"
        elif "verify" in fname_lower or "permission" in fname_lower:
            domain = "Verification"
        elif "evidence" in fname_lower:
            domain = "SCM Migration"
        else:
            domain = "-"

        return domain, file_type

    # 构建表格行（按前缀排序）
    sorted_files = sorted(actual_files, key=lambda f: (int(f.prefix), f.filename))

    # 记录已处理的前缀，用于检测废弃编号
    processed_prefixes: set[str] = set()

    for f in sorted_files:
        processed_prefixes.add(f.prefix)

        # 获取功能域和类型
        domain, file_type = get_domain_and_type(f.prefix, f.filename)

        # 文件名（含目录前缀）
        if f.directory == "verify":
            display_filename = f"verify/{f.filename}"
        else:
            display_filename = f.filename

        # 获取说明：优先使用从文件名生成的简洁说明
        # JSON 的 notes 字段主要用于 renumbering_map，对 inventory 不够友好
        notes = _generate_description_from_filename(f.filename)

        lines.append(f"| {f.prefix} | {display_filename} | {domain} | {file_type} | {notes} |")

    # 检测废弃编号（如 10）
    for prefix in sorted(DEPRECATED_PREFIXES, key=int):
        if prefix not in processed_prefixes:
            # 在正确位置插入废弃行
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                if line.startswith("|") and not line.startswith("|-"):
                    parts = line.split("|")
                    if len(parts) > 1:
                        row_prefix = parts[1].strip().replace("~", "")
                        if row_prefix.isdigit() and int(row_prefix) > int(prefix):
                            insert_idx = i
                            break

            deprecated_line = f"| ~~{prefix}~~ | -- 已废弃 | - | - | 编号保留，不再使用 |"
            lines.insert(insert_idx, deprecated_line)

    return "\n".join(lines)


def _generate_description_from_filename(filename: str) -> str:
    """根据文件名生成简短说明"""
    # 移除前缀和扩展名
    name = filename
    if "_" in name:
        name = "_".join(name.split("_")[1:])
    name = name.replace(".sql", "")

    # 映射常见文件名到说明
    desc_map = {
        "logbook_schema": "核心 schema 与表定义",
        "scm_migration": "SCM 表结构升级迁移",
        "pgvector_extension": "pgvector 扩展初始化",
        "roles_and_grants": "Engram 角色与权限",
        "openmemory_roles_and_grants": "OpenMemory schema 权限",
        "scm_sync_runs": "sync_runs 同步运行记录表",
        "scm_sync_locks": "sync_locks 分布式锁表 + security_events",
        "scm_sync_jobs": "sync_jobs 任务队列表",
        "evidence_uri_column": "patch_blobs 添加 evidence_uri 列",
        "sync_jobs_dimension_columns": "sync_jobs 添加维度列",
        "governance_artifact_ops_audit": "artifact 操作审计表",
        "governance_object_store_audit_events": "对象存储审计事件表",
        "write_audit_status": "write_audit 表状态追踪扩展",
        "verify_permissions": "权限验证脚本（位于 verify/ 子目录）",
    }

    return desc_map.get(name, "")


def generate_renumbering_table(
    renumbering_entries: list[RenumberingMapEntry],
    deprecated_prefixes: dict[str, Any],
) -> str:
    """
    生成 sql_renumbering_map.md 的编号对照表内容。

    Args:
        renumbering_entries: renumbering map 条目列表（从 JSON 加载）
        deprecated_prefixes: 废弃前缀映射

    Returns:
        表格的 Markdown 文本（不含标记区块）
    """
    lines: list[str] = []

    # 表头
    lines.append("| 旧编号 | 旧文件名 | 新编号 | 新文件名 | 状态 |")
    lines.append("|--------|----------|--------|----------|------|")

    # 状态映射（机器标识 -> 显示文本）
    status_display = {
        "retained": "保留",
        "integrated": "**整合**",
        "renamed": "**重命名**",
        "relocated": "**迁移到子目录**",
        "added": "**新增**",
    }

    # 按 new_prefix 排序
    sorted_entries = sorted(renumbering_entries, key=lambda e: int(e.new_prefix))

    for entry in sorted_entries:
        old_prefix = entry.old_prefix if entry.old_prefix != "-" else "-"
        old_filename = Path(entry.old_path).name if entry.old_path else "（新增）"
        new_prefix = entry.new_prefix
        new_filename = entry.new_path.replace("sql/", "")  # 移除 sql/ 前缀

        # 获取显示状态
        # 对于"保留（修改）"情况，需要特殊处理
        if entry.status == "retained" and "修改" in entry.notes:
            status = "保留（修改）"
        else:
            status = status_display.get(entry.status, entry.notes)

        lines.append(
            f"| {old_prefix} | {old_filename} | {new_prefix} | {new_filename} | {status} |"
        )

    return "\n".join(lines)


def generate_deprecated_prefixes_section(
    deprecated_prefixes: dict[str, Any],
) -> str:
    """
    生成 sql_renumbering_map.md 的缺失编号说明表格。

    Args:
        deprecated_prefixes: 废弃前缀映射

    Returns:
        表格的 Markdown 文本
    """
    lines: list[str] = []

    lines.append("| 编号 | 状态 | 说明 |")
    lines.append("|------|------|------|")

    for prefix, info in sorted(deprecated_prefixes.items(), key=lambda x: int(x[0])):
        status = info.get("status", "reserved")
        reason = info.get("reason", "")

        # 状态显示
        status_display = {
            "reserved": "保留未用",
            "deprecated": "已废弃",
        }
        display_status = status_display.get(status, status)

        lines.append(f"| {prefix} | {display_status} | {reason} |")

    return "\n".join(lines)


def emit_inventory_md(
    actual_files: list[SqlFileEntry],
    renumbering_entries: list[RenumberingMapEntry],
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    生成并更新 sql_file_inventory.md 的表格段。

    使用标记区块 <!-- BEGIN_INVENTORY_TABLE --> 和 <!-- END_INVENTORY_TABLE --> 来标识可替换区域。
    如果文档中不存在标记区块，会在"## 1. 文件清单总览"后自动插入。

    Args:
        actual_files: 扫描得到的实际文件列表
        renumbering_entries: renumbering map 条目列表
        dry_run: 如果为 True，只打印生成内容，不写入文件

    Returns:
        (success, message) 元组
    """
    if not INVENTORY_DOC.exists():
        return False, f"文档不存在: {INVENTORY_DOC}"

    content = INVENTORY_DOC.read_text(encoding="utf-8")

    # 生成新表格
    new_table = generate_inventory_table(actual_files, renumbering_entries)

    # 检查是否存在标记区块
    if INVENTORY_TABLE_BEGIN in content and INVENTORY_TABLE_END in content:
        # 替换标记区块之间的内容
        pattern = re.compile(
            rf"{re.escape(INVENTORY_TABLE_BEGIN)}.*?{re.escape(INVENTORY_TABLE_END)}",
            re.DOTALL,
        )
        new_block = f"{INVENTORY_TABLE_BEGIN}\n{new_table}\n{INVENTORY_TABLE_END}\n"
        new_content = pattern.sub(new_block, content)
    else:
        # 查找"## 1. 文件清单总览"并在其后的表格位置插入标记区块
        # 策略：找到该标题后的第一个表格，用标记区块包裹
        section_pattern = re.compile(
            r"(## 1\. 文件清单总览\s*\n+)"
            r"(\|[^\n]+\|\s*\n\|[-\s|]+\|\s*\n(?:\|[^\n]+\|\s*\n)*)",
            re.MULTILINE,
        )

        match = section_pattern.search(content)
        if match:
            # 用标记区块包裹的新表格替换原表格
            section_header = match.group(1)
            new_block = f"{section_header}{INVENTORY_TABLE_BEGIN}\n{new_table}\n{INVENTORY_TABLE_END}\n"
            new_content = content[: match.start()] + new_block + content[match.end() :]
        else:
            return False, "无法找到 '## 1. 文件清单总览' 章节或其表格"

    if dry_run:
        print("=" * 60)
        print("生成的表格内容 (dry-run):")
        print("=" * 60)
        print(new_table)
        print("=" * 60)
        return True, "Dry-run 模式，未写入文件"

    # 写入文件
    INVENTORY_DOC.write_text(new_content, encoding="utf-8")
    return True, f"已更新 {INVENTORY_DOC}"


def emit_renumbering_md(
    renumbering_entries: list[RenumberingMapEntry],
    deprecated_prefixes: dict[str, Any],
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    生成并更新 sql_renumbering_map.md 的编号对照表段。

    使用标记区块 <!-- BEGIN_RENUMBERING_TABLE --> 和 <!-- END_RENUMBERING_TABLE --> 来标识可替换区域。

    Args:
        renumbering_entries: renumbering map 条目列表（从 JSON 加载）
        deprecated_prefixes: 废弃前缀映射
        dry_run: 如果为 True，只打印生成内容，不写入文件

    Returns:
        (success, message) 元组
    """
    if not RENUMBERING_DOC.exists():
        return False, f"文档不存在: {RENUMBERING_DOC}"

    content = RENUMBERING_DOC.read_text(encoding="utf-8")

    # 生成新表格
    new_table = generate_renumbering_table(renumbering_entries, deprecated_prefixes)

    # 检查是否存在标记区块
    if RENUMBERING_TABLE_BEGIN in content and RENUMBERING_TABLE_END in content:
        # 替换标记区块之间的内容
        pattern = re.compile(
            rf"{re.escape(RENUMBERING_TABLE_BEGIN)}.*?{re.escape(RENUMBERING_TABLE_END)}",
            re.DOTALL,
        )
        new_block = f"{RENUMBERING_TABLE_BEGIN}\n{new_table}\n{RENUMBERING_TABLE_END}\n"
        new_content = pattern.sub(new_block, content)
    else:
        # 查找"### 7.1 旧编号 → 新编号映射"并在其后的表格位置插入标记区块
        section_pattern = re.compile(
            r"(### 7\.1 旧编号 → 新编号映射\s*\n+)"
            r"(\|[^\n]+\|\s*\n\|[-\s|]+\|\s*\n(?:\|[^\n]+\|\s*\n)*)",
            re.MULTILINE,
        )

        match = section_pattern.search(content)
        if match:
            # 用标记区块包裹的新表格替换原表格
            section_header = match.group(1)
            new_block = f"{section_header}{RENUMBERING_TABLE_BEGIN}\n{new_table}\n{RENUMBERING_TABLE_END}\n"
            new_content = content[: match.start()] + new_block + content[match.end() :]
        else:
            return False, "无法找到 '### 7.1 旧编号 → 新编号映射' 章节或其表格"

    if dry_run:
        print("=" * 60)
        print("生成的表格内容 (dry-run):")
        print("=" * 60)
        print(new_table)
        print("=" * 60)
        return True, "Dry-run 模式，未写入文件"

    # 写入文件
    RENUMBERING_DOC.write_text(new_content, encoding="utf-8")
    return True, f"已更新 {RENUMBERING_DOC}"


def load_deprecated_prefixes_from_json() -> dict[str, Any]:
    """
    从 sql_renumbering_map.json 加载废弃前缀映射。

    Returns:
        废弃前缀映射字典
    """
    if not RENUMBERING_JSON.exists():
        return {}

    try:
        data = json.loads(RENUMBERING_JSON.read_text(encoding="utf-8"))
        return data.get("deprecated_prefixes", {})
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[WARN] 解析 {RENUMBERING_JSON} deprecated_prefixes 失败: {e}")
        return {}


# ============================================================================
# 输出函数
# ============================================================================


def print_report(report: ConsistencyReport, verbose: bool = False) -> None:
    """打印一致性检查报告"""
    print("\n" + "=" * 60)
    print("一致性检查报告")
    print("=" * 60)

    print(f"\n实际文件数: {len(report.actual_files)}")
    print(f"文档记录数: {len(report.doc_entries)}")

    # 旧文件检测结果（优先显示，因为这是严重问题）
    if report.deprecated_files_found:
        print(f"\n[严重] 检测到已废弃的旧文件 ({len(report.deprecated_files_found)} 项):")
        for item in report.deprecated_files_found:
            print(f"\n  旧文件: {item['old_path']}")
            print(f"    应替换为: {item['new_path']}")
            print(f"    状态: {item['status']}")
            print(f"    推荐动作: {item['action']}")

    # verify 目录约束违规
    if report.verify_directory_violations:
        print(f"\n[严重] verify 目录约束违规 ({len(report.verify_directory_violations)} 项):")
        for violation in report.verify_directory_violations:
            log_error(f"  - {violation}")

    # renumbering map 覆盖检查结果
    if report.renumbering_missing_prefixes:
        print(
            f"\n[严重] renumbering map (JSON) 未覆盖的前缀 ({len(report.renumbering_missing_prefixes)} 项):"
        )
        for prefix in report.renumbering_missing_prefixes:
            log_error(f"  - 前缀 {prefix} 未在 sql_renumbering_map.json 的 renumbering_map 中记录")

    if report.renumbering_filename_mismatches:
        print(
            f"\n[严重] renumbering map 文件名不匹配 ({len(report.renumbering_filename_mismatches)} 项):"
        )
        for item in report.renumbering_filename_mismatches:
            issue = item.get("issue", "filename_mismatch")
            if issue == "directory_mismatch":
                log_error(
                    f"  - 前缀 {item['prefix']}: 目录不匹配 "
                    f"(实际: {item['actual_directory']}, 期望: {item['expected_directory']})"
                )
            else:
                log_error(
                    f"  - 前缀 {item['prefix']}: 文件名不匹配 "
                    f"(实际: {item['actual_filename']}, 期望: {item['expected_filename']})"
                )

    # MD/JSON 一致性检查结果
    if report.md_json_inconsistencies:
        print(f"\n[严重] MD 与 JSON 不一致 ({len(report.md_json_inconsistencies)} 项):")
        for item in report.md_json_inconsistencies:
            issue = item.get("issue", "value_mismatch")
            if issue == "json_only":
                log_error(f"  - 前缀 {item['new_prefix']}: JSON 中存在但 MD 中不存在")
            elif issue == "md_only":
                log_error(f"  - 前缀 {item['new_prefix']}: MD 中存在但 JSON 中不存在")
            else:
                log_error(
                    f"  - 前缀 {item['new_prefix']}: {item['field']} 不匹配 "
                    f"(JSON: {item['json_value']}, MD: {item['md_value']})"
                )

    if report.missing_in_dir:
        print(f"\n文档有但目录缺失 ({len(report.missing_in_dir)} 项):")
        for msg in report.missing_in_dir:
            log_error(f"  - {msg}")

    if report.missing_in_doc:
        print(f"\n目录有但文档缺失 ({len(report.missing_in_doc)} 项):")
        for msg in report.missing_in_doc:
            log_error(f"  - {msg}")

    if report.mismatched_entries:
        print(f"\n信息不匹配 ({len(report.mismatched_entries)} 项):")
        for entry in report.mismatched_entries:
            log_error(f"  - {entry['prefix']}/{entry['filename']}: {entry['issue']}")

    if report.warnings:
        print(f"\n警告 ({len(report.warnings)} 项):")
        for msg in report.warnings:
            log_warn(f"  - {msg}")

    if report.errors:
        print(f"\n错误 ({len(report.errors)} 项):")
        for msg in report.errors:
            log_error(f"  - {msg}")

    print("\n" + "=" * 60)
    if report.ok:
        log_ok("一致性检查通过")
    else:
        log_error(f"一致性检查失败，共 {len(report.errors)} 个错误")
    print("=" * 60)


# ============================================================================
# 生成模式处理函数
# ============================================================================


def handle_emit_mode(args: argparse.Namespace) -> int:
    """
    处理文档生成模式（--emit-inventory-md / --emit-renumbering-md）。

    Args:
        args: 命令行参数

    Returns:
        退出码（0 成功，非 0 失败）
    """
    print("=== SQL 迁移文档生成模式 ===\n")

    exit_code = 0

    # 扫描目录获取实际文件
    print("[1/3] 扫描 SQL 目录...")
    actual_files = scan_sql_directory()
    log_ok(f"扫描到 {len(actual_files)} 个 SQL 文件")

    # 加载 JSON SSOT
    print("\n[2/3] 加载 JSON SSOT...")
    renumbering_entries = load_renumbering_map_from_json()
    if renumbering_entries:
        log_ok(f"从 sql_renumbering_map.json 加载到 {len(renumbering_entries)} 条记录")
    else:
        log_error("无法加载 renumbering_map，请检查 JSON 文件")
        return 1

    deprecated_prefixes = load_deprecated_prefixes_from_json()
    log_ok(f"加载到 {len(deprecated_prefixes)} 个废弃前缀")

    # 生成文档
    print("\n[3/3] 生成文档...")

    if args.emit_inventory_md:
        print("\n--- 生成 sql_file_inventory.md 表格 ---")
        success, message = emit_inventory_md(
            actual_files, renumbering_entries, dry_run=args.dry_run
        )
        if success:
            log_ok(message)
        else:
            log_error(message)
            exit_code = 1

    if args.emit_renumbering_md:
        print("\n--- 生成 sql_renumbering_map.md 对照表 ---")
        success, message = emit_renumbering_md(
            renumbering_entries, deprecated_prefixes, dry_run=args.dry_run
        )
        if success:
            log_ok(message)
        else:
            log_error(message)
            exit_code = 1

    if exit_code == 0:
        print("\n" + "=" * 60)
        log_ok("文档生成完成")
        if not args.dry_run:
            print("\n提示：请检查生成的文档内容，确保无误后提交。")
            print("验收方式：更新 SSOT JSON/SQL 文件后运行生成器即可更新文档表格。")
        print("=" * 60)

    return exit_code


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SQL 迁移清单一致性验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 基本检查
  python scripts/verify_sql_migration_inventory.py

  # 详细输出
  python scripts/verify_sql_migration_inventory.py --verbose

  # CI 门禁模式（严格检查）
  python scripts/verify_sql_migration_inventory.py --strict

  # 输出 JSON 报告
  python scripts/verify_sql_migration_inventory.py --output report.json

文档生成模式：
  # 生成 sql_file_inventory.md 表格（预览，不写入文件）
  python scripts/verify_sql_migration_inventory.py --emit-inventory-md --dry-run

  # 生成 sql_file_inventory.md 表格（写入文件）
  python scripts/verify_sql_migration_inventory.py --emit-inventory-md

  # 生成 sql_renumbering_map.md 对照表
  python scripts/verify_sql_migration_inventory.py --emit-renumbering-md

  # 同时生成两个文档的表格
  python scripts/verify_sql_migration_inventory.py --emit-inventory-md --emit-renumbering-md

工作流：
  1. 更新 SQL 文件或 sql_renumbering_map.json (SSOT)
  2. 运行生成器更新文档表格
  3. 提交所有变更
        """,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：任何不一致都会导致失败（CI 门禁使用）",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="输出 JSON 报告到指定文件",
    )
    parser.add_argument(
        "--print-inventory",
        action="store_true",
        help="打印当前文件清单",
    )
    parser.add_argument(
        "--skip-prefix-check",
        action="store_true",
        help="跳过与 migrate.py 的前缀常量一致性检查（仅用于特殊场景）",
    )
    parser.add_argument(
        "--emit-inventory-md",
        action="store_true",
        help="从 JSON SSOT 和扫描结果生成 sql_file_inventory.md 的表格段",
    )
    parser.add_argument(
        "--emit-renumbering-md",
        action="store_true",
        help="从 JSON SSOT 生成 sql_renumbering_map.md 的编号对照表段",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="与 --emit-* 配合使用，只打印生成内容，不写入文件",
    )

    args = parser.parse_args()

    # 生成模式：如果指定了 --emit-* 参数，执行生成逻辑后退出
    if args.emit_inventory_md or args.emit_renumbering_md:
        return handle_emit_mode(args)

    print("=== SQL 迁移清单一致性验证 ===")

    # Step 0: 前缀常量一致性检查（与 migrate.py 的 SSOT）
    if not args.skip_prefix_check:
        print("\n[0/7] 检查前缀常量与 migrate.py 的一致性...")
        prefix_ok, prefix_messages = verify_prefix_constants_consistency()
        for msg in prefix_messages:
            if msg.startswith("[WARN]"):
                log_warn(msg)
            elif not prefix_ok:
                log_error(msg)
            else:
                log_info(msg)
        if not prefix_ok:
            log_error("前缀常量一致性检查失败！脚本已中止。")
            log_error("请更新 scripts/verify_sql_migration_inventory.py 中的前缀常量，")
            log_error("使其与 src/engram/logbook/migrate.py 保持一致。")
            return 1
        if prefix_messages and prefix_messages[0].startswith("[WARN]"):
            log_warn("前缀常量一致性检查被跳过（无法导入 engram.logbook.migrate）")
        else:
            log_ok("前缀常量与 migrate.py 一致")
    print()

    # Step 1: 扫描目录
    print("[1/7] 扫描 SQL 目录...")
    actual_files = scan_sql_directory()
    log_ok(f"扫描到 {len(actual_files)} 个 SQL 文件")

    if args.print_inventory or args.verbose:
        print_inventory(actual_files)

    # Step 2: 解析文档
    print("\n[2/7] 解析文档...")
    doc_entries = parse_inventory_doc()
    if doc_entries:
        log_ok(f"从 sql_file_inventory.md 解析到 {len(doc_entries)} 条记录")
    else:
        log_warn(f"无法解析 {INVENTORY_DOC}")

    renumbering_map = parse_renumbering_doc()
    if renumbering_map:
        log_ok(f"从 sql_renumbering_map.md 解析到 {len(renumbering_map)} 条映射")
    else:
        log_warn(f"无法解析 {RENUMBERING_DOC}")

    # 优先从 JSON 加载 renumbering_map（SSOT）
    renumbering_entries_json = load_renumbering_map_from_json()
    if renumbering_entries_json:
        log_ok(
            f"从 sql_renumbering_map.json 加载到 {len(renumbering_entries_json)} 条编号对照记录（SSOT）"
        )
        renumbering_entries = renumbering_entries_json
    else:
        # 降级：从 Markdown 解析
        log_warn("JSON 无 renumbering_map，降级从 Markdown 解析")
        renumbering_entries = parse_renumbering_doc_entries()
        renumbering_entries_json = []
        if renumbering_entries:
            log_ok(f"从 sql_renumbering_map.md 解析到 {len(renumbering_entries)} 条编号对照记录")
        else:
            log_warn("无法解析 renumbering map 编号对照表")

    # 解析 Markdown 条目（用于一致性对比）
    renumbering_entries_md = parse_renumbering_doc_entries()
    if renumbering_entries_md:
        log_ok(
            f"从 sql_renumbering_map.md 解析到 {len(renumbering_entries_md)} 条编号对照记录（用于一致性对比）"
        )

    # Step 3: 检测已废弃的旧文件
    print("\n[3/7] 检测已废弃的旧文件...")
    deprecated_mapping = load_deprecated_files_mapping()
    if deprecated_mapping:
        log_ok(f"从 sql_renumbering_map.json 加载 {len(deprecated_mapping)} 条废弃文件映射")
    else:
        log_warn(f"无法加载废弃文件映射 {RENUMBERING_JSON}")

    deprecated_files_found = check_deprecated_files(deprecated_mapping)
    if deprecated_files_found:
        log_error(f"检测到 {len(deprecated_files_found)} 个已废弃的旧文件存在！")
    else:
        log_ok("未检测到废弃的旧文件")

    # Step 4: verify 目录约束检查
    print("\n[4/7] 检查 verify 目录约束...")
    verify_violations = check_verify_directory_constraints(actual_files, verbose=args.verbose)
    if verify_violations:
        log_error(f"检测到 {len(verify_violations)} 个 verify 目录约束违规！")
    else:
        log_ok("verify 目录约束检查通过（99 前缀位于 sql/verify/）")

    # Step 5: renumbering map 覆盖检查（基于 JSON SSOT）
    print("\n[5/7] 检查 renumbering map 覆盖（JSON SSOT）...")
    renumbering_missing, renumbering_mismatches = check_renumbering_map_coverage(
        actual_files, renumbering_entries, verbose=args.verbose
    )
    if renumbering_missing:
        log_error(f"renumbering map (JSON) 未覆盖 {len(renumbering_missing)} 个前缀")
    else:
        log_ok("renumbering map (JSON) 覆盖所有现存前缀")

    if renumbering_mismatches:
        log_error(f"renumbering map (JSON) 文件名/目录不匹配 {len(renumbering_mismatches)} 项")
    else:
        log_ok("renumbering map (JSON) 文件名与实际一致")

    # Step 6: MD/JSON 一致性检查
    print("\n[6/7] 检查 MD/JSON 一致性...")
    md_json_inconsistencies: list[dict[str, Any]] = []
    if renumbering_entries_json and renumbering_entries_md:
        md_json_inconsistencies = check_md_json_consistency(
            renumbering_entries_json, renumbering_entries_md, verbose=args.verbose
        )
        if md_json_inconsistencies:
            log_error(f"检测到 {len(md_json_inconsistencies)} 项 MD/JSON 不一致")
        else:
            log_ok("MD 与 JSON 一致")
    else:
        log_warn("无法执行 MD/JSON 一致性检查（缺少数据源）")

    # Step 7: 一致性检查
    print("\n[7/7] 执行一致性检查...")
    report = check_consistency(actual_files, doc_entries, verbose=args.verbose)

    # 将旧文件检测结果添加到报告
    report.deprecated_files_found = deprecated_files_found
    if deprecated_files_found:
        for item in deprecated_files_found:
            report.add_error(
                f"存在已废弃的旧文件: {item['old_path']} -> 应替换为 {item['new_path']}"
            )

    # 将 verify 目录约束违规添加到报告
    report.verify_directory_violations = verify_violations
    for violation in verify_violations:
        report.add_error(violation)

    # 将 renumbering map 检查结果添加到报告
    report.renumbering_missing_prefixes = renumbering_missing
    report.renumbering_filename_mismatches = renumbering_mismatches
    for prefix in renumbering_missing:
        report.add_error(f"前缀 {prefix} 未在 sql_renumbering_map.json 的 renumbering_map 中记录")
    for mismatch in renumbering_mismatches:
        issue = mismatch.get("issue", "filename_mismatch")
        if issue == "directory_mismatch":
            report.add_error(
                f"前缀 {mismatch['prefix']}: 目录不匹配 "
                f"(实际: {mismatch['actual_directory']}, 期望: {mismatch['expected_directory']})"
            )
        else:
            report.add_error(
                f"前缀 {mismatch['prefix']}: 文件名不匹配 "
                f"(实际: {mismatch['actual_filename']}, 期望: {mismatch['expected_filename']})"
            )

    # 将 MD/JSON 一致性检查结果添加到报告
    report.md_json_inconsistencies = md_json_inconsistencies
    for item in md_json_inconsistencies:
        issue = item.get("issue", "value_mismatch")
        if issue == "json_only":
            report.add_error(f"MD/JSON 不一致: 前缀 {item['new_prefix']} 仅在 JSON 中存在")
        elif issue == "md_only":
            report.add_error(f"MD/JSON 不一致: 前缀 {item['new_prefix']} 仅在 MD 中存在")
        else:
            report.add_error(
                f"MD/JSON 不一致: 前缀 {item['new_prefix']} 的 {item['field']} 不匹配 "
                f"(JSON: {item['json_value']}, MD: {item['md_value']})"
            )

    # 打印报告
    print_report(report, verbose=args.verbose)

    # 输出 JSON
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        log_ok(f"报告已保存到 {args.output}")

    # 返回退出码
    if args.strict:
        # 严格模式：任何错误或警告都失败
        if report.errors or report.warnings:
            return 1
    else:
        # 普通模式：只有错误才失败
        if report.errors:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
