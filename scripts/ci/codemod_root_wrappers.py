#!/usr/bin/env python3
"""
根目录 wrapper 模块 import 自动迁移工具 (Codemod)

从 configs/import_migration_map.json 读取迁移映射，自动重写 Python 源文件中的
禁用模块导入语句，将其替换为官方包路径。

功能:
- --scan: 扫描并列出所有违规 import 及迁移建议
- --dry-run: 展示修改 diff，不实际写入文件
- --apply: 执行修改并写回文件
- --module <name>: 只处理指定模块
- --all: 处理所有可迁移的弃用模块
- --report: 输出迁移报告（markdown 格式）

设计原则:
- 使用 AST 解析，确保只修改真实的 import 语句
- 不修改字符串内容（docstring、注释字符串等）
- 与 check_no_root_wrappers_usage.py 的 AST 规则保持一致
- 保留原始代码格式（尽可能）

详见 docs/architecture/no_root_wrappers_migration_map.md

用法:
    python scripts/ci/codemod_root_wrappers.py --scan
    python scripts/ci/codemod_root_wrappers.py --module artifact_cli --dry-run
    python scripts/ci/codemod_root_wrappers.py --module artifact_cli --apply
    python scripts/ci/codemod_root_wrappers.py --all --apply
    python scripts/ci/codemod_root_wrappers.py --report
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ============================================================================
# 配置区
# ============================================================================

# SSOT 映射文件路径（相对于项目根）
IMPORT_MIGRATION_MAP_FILE = "configs/import_migration_map.json"

# 扫描目标目录（相对于项目根）
SCAN_DIRECTORIES: List[str] = [
    "src",
    "tests",
]


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class MigrationEntry:
    """迁移条目"""

    old_module: str
    import_target: Optional[str]
    cli_target: Optional[str]
    deprecated: bool
    status: str
    notes: str = ""

    def has_import_target(self) -> bool:
        """是否有可用的 import 迁移目标"""
        return self.import_target is not None and len(self.import_target) > 0


@dataclass
class ImportMatch:
    """匹配到的 import 语句"""

    file_path: str
    line_number: int
    line_content: str
    old_module: str
    import_type: str  # "import" or "from"
    alias: Optional[str] = None
    imported_names: Optional[List[str]] = None
    is_type_checking: bool = False

    def get_original_line(self) -> str:
        """获取原始行内容（去除尾部空白）"""
        return self.line_content.rstrip()


@dataclass
class MigrationResult:
    """单个文件的迁移结果"""

    file_path: str
    original_content: str
    modified_content: str
    changes: List[Dict[str, Any]] = field(default_factory=list)

    def has_changes(self) -> bool:
        return self.original_content != self.modified_content

    def get_diff(self) -> str:
        """生成 unified diff"""
        original_lines = self.original_content.splitlines(keepends=True)
        modified_lines = self.modified_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{self.file_path}",
            tofile=f"b/{self.file_path}",
        )
        return "".join(diff)


@dataclass
class ScanResult:
    """扫描结果汇总"""

    matches: List[ImportMatch] = field(default_factory=list)
    files_scanned: int = 0
    migratable_count: int = 0  # 可迁移的（有 import_target）
    non_migratable_count: int = 0  # 不可迁移的（只有 cli_target）


# ============================================================================
# 迁移映射加载
# ============================================================================


def load_migration_map(project_root: Path) -> Dict[str, MigrationEntry]:
    """
    从 SSOT 文件加载迁移映射

    Args:
        project_root: 项目根目录

    Returns:
        {old_module: MigrationEntry} 映射
    """
    map_path = project_root / IMPORT_MIGRATION_MAP_FILE
    if not map_path.exists():
        print(f"[ERROR] 迁移映射文件不存在: {map_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(map_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ERROR] 无法加载迁移映射文件: {e}", file=sys.stderr)
        sys.exit(1)

    entries: Dict[str, MigrationEntry] = {}
    for entry_data in data.get("modules", []):
        old_module = entry_data.get("old_module", "")
        if not old_module:
            continue

        entry = MigrationEntry(
            old_module=old_module,
            import_target=entry_data.get("import_target"),
            cli_target=entry_data.get("cli_target"),
            deprecated=entry_data.get("deprecated", True),
            status=entry_data.get("status", "unknown"),
            notes=entry_data.get("notes", ""),
        )
        entries[old_module] = entry

    return entries


def get_deprecated_modules(entries: Dict[str, MigrationEntry]) -> Set[str]:
    """获取所有弃用模块名"""
    return {name for name, entry in entries.items() if entry.deprecated}


def get_migratable_modules(entries: Dict[str, MigrationEntry]) -> Set[str]:
    """获取可迁移的模块名（有 import_target）"""
    return {
        name
        for name, entry in entries.items()
        if entry.deprecated and entry.has_import_target()
    }


# ============================================================================
# AST 导入检测（与 check_no_root_wrappers_usage.py 一致）
# ============================================================================


class ImportVisitor(ast.NodeVisitor):
    """
    AST 访问器，提取所有导入语句

    与 check_no_root_wrappers_usage.py 中的实现保持一致。
    """

    def __init__(
        self, lines: List[str], target_modules: Set[str], source_code: str
    ):
        self.lines = lines
        self.target_modules = target_modules
        self.source_code = source_code
        self.imports: List[ImportMatch] = []
        self._type_checking_ranges: List[Tuple[int, int]] = []
        self._precompute_type_checking_blocks()

    def _precompute_type_checking_blocks(self) -> None:
        """预计算 TYPE_CHECKING 块的行范围"""
        try:
            tree = ast.parse(self.source_code)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                is_type_checking = False

                if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                    is_type_checking = True
                elif isinstance(test, ast.Attribute):
                    if (
                        test.attr == "TYPE_CHECKING"
                        and isinstance(test.value, ast.Name)
                        and test.value.id == "typing"
                    ):
                        is_type_checking = True

                if is_type_checking:
                    start_line = node.lineno
                    end_line = node.end_lineno or node.lineno
                    for stmt in node.body:
                        if hasattr(stmt, "end_lineno") and stmt.end_lineno:
                            end_line = max(end_line, stmt.end_lineno)
                    self._type_checking_ranges.append((start_line, end_line))

    def _is_in_type_checking(self, lineno: int) -> bool:
        """检查给定行号是否在 TYPE_CHECKING 块内"""
        for start, end in self._type_checking_ranges:
            if start < lineno <= end:
                return True
        return False

    def _get_line_content(self, lineno: int) -> str:
        """获取指定行的内容"""
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def visit_Import(self, node: ast.Import) -> None:
        """处理 import xxx 语句"""
        for alias in node.names:
            top_module = alias.name.split(".")[0]
            if top_module in self.target_modules:
                self.imports.append(
                    ImportMatch(
                        file_path="",  # 由调用者填充
                        line_number=node.lineno,
                        line_content=self._get_line_content(node.lineno),
                        old_module=top_module,
                        import_type="import",
                        alias=alias.asname,
                        is_type_checking=self._is_in_type_checking(node.lineno),
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """处理 from xxx import ... 语句"""
        if node.module:
            top_module = node.module.split(".")[0]
            if top_module in self.target_modules:
                # 提取导入的名称
                imported_names = [alias.name for alias in node.names]
                self.imports.append(
                    ImportMatch(
                        file_path="",  # 由调用者填充
                        line_number=node.lineno,
                        line_content=self._get_line_content(node.lineno),
                        old_module=top_module,
                        import_type="from",
                        imported_names=imported_names,
                        is_type_checking=self._is_in_type_checking(node.lineno),
                    )
                )
        self.generic_visit(node)


def extract_imports(
    source_code: str, target_modules: Set[str]
) -> List[ImportMatch]:
    """
    使用 AST 从源代码中提取目标模块的导入

    Args:
        source_code: Python 源代码内容
        target_modules: 目标模块集合

    Returns:
        ImportMatch 列表
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    lines = source_code.splitlines()
    visitor = ImportVisitor(lines, target_modules, source_code)
    visitor.visit(tree)
    return visitor.imports


# ============================================================================
# Import 语句改写
# ============================================================================


def parse_import_target(import_target: str) -> Tuple[str, Optional[str]]:
    """
    解析 import_target 字符串

    格式示例:
    - "engram.logbook.cli.scm_sync:runner_main"  -> ("engram.logbook.cli.scm_sync", "runner_main")
    - "engram.logbook.materialize_patch_blob"    -> ("engram.logbook.materialize_patch_blob", None)

    Returns:
        (module_path, entry_point)
    """
    if ":" in import_target:
        module_path, entry_point = import_target.split(":", 1)
        return module_path, entry_point
    return import_target, None


def rewrite_import_statement(
    match: ImportMatch,
    entry: MigrationEntry,
    lines: List[str],
) -> Tuple[str, Dict[str, Any]]:
    """
    重写单个 import 语句

    Args:
        match: 匹配到的 import 语句
        entry: 迁移条目
        lines: 源文件行列表

    Returns:
        (new_line, change_info)
    """
    if not entry.has_import_target():
        # 无法自动迁移
        return match.line_content, {
            "type": "skip",
            "reason": "no import target",
            "old_line": match.line_content,
        }

    module_path, entry_point = parse_import_target(entry.import_target or "")
    old_line = match.line_content
    indent = len(old_line) - len(old_line.lstrip())
    indent_str = old_line[:indent]

    if match.import_type == "import":
        # import scm_sync_runner -> from engram.logbook.cli.scm_sync import runner_main as scm_sync_runner
        # 或者 import scm_materialize_patch_blob -> import engram.logbook.materialize_patch_blob as scm_materialize_patch_blob
        if entry_point:
            # 有入口点，使用 from ... import ... as ...
            if match.alias:
                new_line = f"{indent_str}from {module_path} import {entry_point} as {match.alias}"
            else:
                new_line = f"{indent_str}from {module_path} import {entry_point} as {match.old_module}"
        else:
            # 无入口点，使用 import ... as ...
            if match.alias:
                new_line = f"{indent_str}import {module_path} as {match.alias}"
            else:
                new_line = f"{indent_str}import {module_path} as {match.old_module}"

    elif match.import_type == "from":
        # from scm_sync_runner import main -> from engram.logbook.cli.scm_sync import runner_main
        # 需要处理导入的名称映射
        if match.imported_names:
            if entry_point and len(match.imported_names) == 1:
                # 如果只导入一个名称且有入口点，可能需要映射
                old_name = match.imported_names[0]
                if old_name == "main" or old_name == entry_point:
                    new_line = f"{indent_str}from {module_path} import {entry_point}"
                else:
                    # 保留原导入名
                    new_line = f"{indent_str}from {module_path} import {old_name}"
            else:
                # 多个导入名或无入口点
                names = ", ".join(match.imported_names)
                new_line = f"{indent_str}from {module_path} import {names}"
        else:
            new_line = f"{indent_str}from {module_path} import *"
    else:
        new_line = old_line

    # 保留行尾注释
    comment_match = re.search(r"\s*#.*$", old_line)
    if comment_match and "#" not in new_line:
        new_line = new_line.rstrip() + "  " + comment_match.group().strip()

    return new_line, {
        "type": "rewrite",
        "old_line": old_line.rstrip(),
        "new_line": new_line.rstrip(),
        "line_number": match.line_number,
        "old_module": match.old_module,
        "new_module": module_path,
    }


def migrate_file(
    file_path: Path,
    relative_path: str,
    entries: Dict[str, MigrationEntry],
    target_modules: Set[str],
) -> MigrationResult:
    """
    迁移单个文件

    Args:
        file_path: 文件路径
        relative_path: 相对于项目根的路径
        entries: 迁移映射
        target_modules: 要处理的目标模块集合

    Returns:
        MigrationResult
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return MigrationResult(
            file_path=relative_path,
            original_content="",
            modified_content="",
            changes=[{"type": "error", "message": str(e)}],
        )

    imports = extract_imports(content, target_modules)
    if not imports:
        return MigrationResult(
            file_path=relative_path,
            original_content=content,
            modified_content=content,
        )

    lines = content.splitlines()
    changes: List[Dict[str, Any]] = []

    # 按行号排序，从后往前处理（避免行号偏移）
    imports_sorted = sorted(imports, key=lambda x: x.line_number, reverse=True)

    for match in imports_sorted:
        # 跳过 TYPE_CHECKING 块内的导入
        if match.is_type_checking:
            changes.append({
                "type": "skip",
                "reason": "in TYPE_CHECKING block",
                "line_number": match.line_number,
                "old_line": match.line_content.rstrip(),
            })
            continue

        entry = entries.get(match.old_module)
        if not entry:
            continue

        new_line, change_info = rewrite_import_statement(match, entry, lines)

        if change_info["type"] == "rewrite":
            lines[match.line_number - 1] = new_line
            changes.append(change_info)
        elif change_info["type"] == "skip":
            changes.append(change_info)

    modified_content = "\n".join(lines)
    # 保留原文件末尾换行
    if content.endswith("\n"):
        modified_content += "\n"

    return MigrationResult(
        file_path=relative_path,
        original_content=content,
        modified_content=modified_content,
        changes=list(reversed(changes)),  # 恢复正序
    )


# ============================================================================
# 扫描功能
# ============================================================================


def scan_files(
    project_root: Path,
    entries: Dict[str, MigrationEntry],
    target_modules: Optional[Set[str]] = None,
) -> ScanResult:
    """
    扫描所有文件中的目标模块导入

    Args:
        project_root: 项目根目录
        entries: 迁移映射
        target_modules: 要扫描的模块集合（None 表示所有弃用模块）

    Returns:
        ScanResult
    """
    if target_modules is None:
        target_modules = get_deprecated_modules(entries)

    result = ScanResult()
    migratable = get_migratable_modules(entries)

    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir
        if not dir_path.exists():
            continue

        for py_file in dir_path.rglob("*.py"):
            result.files_scanned += 1
            relative_path = str(py_file.relative_to(project_root))

            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            imports = extract_imports(content, target_modules)
            for match in imports:
                match.file_path = relative_path
                result.matches.append(match)

                if match.old_module in migratable:
                    result.migratable_count += 1
                else:
                    result.non_migratable_count += 1

    return result


# ============================================================================
# 命令行处理
# ============================================================================


def cmd_scan(
    project_root: Path,
    entries: Dict[str, MigrationEntry],
    module_name: Optional[str],
    verbose: bool,
) -> int:
    """--scan 命令：列出违规与建议"""
    if module_name:
        if module_name not in entries:
            print(f"[ERROR] 未知模块: {module_name}", file=sys.stderr)
            return 1
        target_modules = {module_name}
    else:
        target_modules = get_deprecated_modules(entries)

    result = scan_files(project_root, entries, target_modules)

    print("=" * 70)
    print("根目录 Wrapper 导入扫描结果")
    print("=" * 70)
    print()
    print(f"扫描文件数: {result.files_scanned}")
    print(f"发现违规: {len(result.matches)} 处")
    print(f"  - 可自动迁移: {result.migratable_count}")
    print(f"  - 需手动处理: {result.non_migratable_count}")
    print()

    if not result.matches:
        print("[OK] 未发现需要迁移的导入")
        return 0

    # 按文件分组显示
    by_file: Dict[str, List[ImportMatch]] = {}
    for match in result.matches:
        by_file.setdefault(match.file_path, []).append(match)

    for file_path, matches in sorted(by_file.items()):
        print(f"--- {file_path} ---")
        for match in sorted(matches, key=lambda x: x.line_number):
            entry = entries.get(match.old_module)
            status = ""
            if match.is_type_checking:
                status = " [TYPE_CHECKING - 跳过]"
            elif entry and not entry.has_import_target():
                status = " [需手动处理]"

            print(f"  L{match.line_number}: {match.line_content.strip()}{status}")

            if verbose and entry:
                if entry.import_target:
                    print(f"       -> {entry.import_target}")
                elif entry.cli_target:
                    print(f"       -> CLI: {entry.cli_target}")
        print()

    return 0


def cmd_dry_run(
    project_root: Path,
    entries: Dict[str, MigrationEntry],
    module_name: Optional[str],
    all_modules: bool,
) -> int:
    """--dry-run 命令：展示 diff"""
    if module_name:
        if module_name not in entries:
            print(f"[ERROR] 未知模块: {module_name}", file=sys.stderr)
            return 1
        target_modules = {module_name}
    elif all_modules:
        target_modules = get_migratable_modules(entries)
    else:
        print("[ERROR] 请指定 --module <name> 或 --all", file=sys.stderr)
        return 1

    if not target_modules:
        print("[WARN] 没有可迁移的模块", file=sys.stderr)
        return 0

    print("=" * 70)
    print("Dry Run - 以下是将要进行的修改")
    print("=" * 70)
    print()

    total_changes = 0
    files_changed = 0

    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir
        if not dir_path.exists():
            continue

        for py_file in dir_path.rglob("*.py"):
            relative_path = str(py_file.relative_to(project_root))
            result = migrate_file(py_file, relative_path, entries, target_modules)

            if result.has_changes():
                files_changed += 1
                print(result.get_diff())
                total_changes += len([c for c in result.changes if c.get("type") == "rewrite"])

    print("-" * 70)
    print(f"文件数: {files_changed}")
    print(f"修改数: {total_changes}")
    print()
    print("使用 --apply 执行修改")

    return 0


def cmd_apply(
    project_root: Path,
    entries: Dict[str, MigrationEntry],
    module_name: Optional[str],
    all_modules: bool,
) -> int:
    """--apply 命令：执行修改"""
    if module_name:
        if module_name not in entries:
            print(f"[ERROR] 未知模块: {module_name}", file=sys.stderr)
            return 1
        target_modules = {module_name}
    elif all_modules:
        target_modules = get_migratable_modules(entries)
    else:
        print("[ERROR] 请指定 --module <name> 或 --all", file=sys.stderr)
        return 1

    if not target_modules:
        print("[WARN] 没有可迁移的模块", file=sys.stderr)
        return 0

    print("=" * 70)
    print("应用修改")
    print("=" * 70)
    print()

    total_changes = 0
    files_changed = 0
    errors = []

    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir
        if not dir_path.exists():
            continue

        for py_file in dir_path.rglob("*.py"):
            relative_path = str(py_file.relative_to(project_root))
            result = migrate_file(py_file, relative_path, entries, target_modules)

            if result.has_changes():
                try:
                    py_file.write_text(result.modified_content, encoding="utf-8")
                    files_changed += 1
                    change_count = len([c for c in result.changes if c.get("type") == "rewrite"])
                    total_changes += change_count
                    print(f"[OK] {relative_path} ({change_count} 处修改)")
                except Exception as e:
                    errors.append(f"{relative_path}: {e}")
                    print(f"[ERROR] {relative_path}: {e}")

    print()
    print("-" * 70)
    print(f"修改文件数: {files_changed}")
    print(f"总修改数: {total_changes}")
    if errors:
        print(f"错误数: {len(errors)}")
        return 1

    print()
    print("[OK] 迁移完成")
    return 0


def cmd_report(
    project_root: Path,
    entries: Dict[str, MigrationEntry],
) -> int:
    """--report 命令：输出迁移报告"""
    result = scan_files(project_root, entries, None)

    print("# 根目录 Wrapper 迁移报告")
    print()
    print(f"生成时间: {__import__('datetime').datetime.now().isoformat()}")
    print()
    print("## 概览")
    print()
    print(f"- 扫描文件数: {result.files_scanned}")
    print(f"- 发现违规: {len(result.matches)} 处")
    print(f"- 可自动迁移: {result.migratable_count}")
    print(f"- 需手动处理: {result.non_migratable_count}")
    print()

    if not result.matches:
        print("**未发现需要迁移的导入**")
        return 0

    # 按模块分组统计
    by_module: Dict[str, List[ImportMatch]] = {}
    for match in result.matches:
        by_module.setdefault(match.old_module, []).append(match)

    print("## 按模块统计")
    print()
    print("| 模块 | 违规数 | 可自动迁移 | 迁移目标 |")
    print("|------|--------|------------|----------|")

    for module_name in sorted(by_module.keys()):
        matches = by_module[module_name]
        entry = entries.get(module_name)
        count = len(matches)
        migratable = "是" if entry and entry.has_import_target() else "否"
        target = entry.import_target if entry and entry.import_target else entry.cli_target if entry else "-"
        print(f"| `{module_name}` | {count} | {migratable} | `{target}` |")

    print()
    print("## 详细列表")
    print()

    by_file: Dict[str, List[ImportMatch]] = {}
    for match in result.matches:
        by_file.setdefault(match.file_path, []).append(match)

    for file_path in sorted(by_file.keys()):
        matches = by_file[file_path]
        print(f"### `{file_path}`")
        print()
        for match in sorted(matches, key=lambda x: x.line_number):
            status = ""
            if match.is_type_checking:
                status = " *(TYPE_CHECKING)*"
            print(f"- L{match.line_number}: `{match.old_module}`{status}")
        print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="根目录 wrapper 模块 import 自动迁移工具"
    )

    # 操作模式（互斥）
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--scan",
        action="store_true",
        help="扫描并列出所有违规 import 及迁移建议",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="展示修改 diff，不实际写入文件",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="执行修改并写回文件",
    )
    mode_group.add_argument(
        "--report",
        action="store_true",
        help="输出迁移报告（markdown 格式）",
    )

    # 模块选择
    module_group = parser.add_mutually_exclusive_group()
    module_group.add_argument(
        "--module",
        type=str,
        metavar="NAME",
        help="只处理指定模块",
    )
    module_group.add_argument(
        "--all",
        action="store_true",
        help="处理所有可迁移的弃用模块",
    )

    # 其他选项
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细信息",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent  # scripts/ci/ 的父父目录

    if not project_root.exists():
        print(f"[ERROR] 项目根目录不存在: {project_root}", file=sys.stderr)
        return 1

    # 加载迁移映射
    entries = load_migration_map(project_root)

    # 执行命令
    if args.scan:
        return cmd_scan(project_root, entries, args.module, args.verbose)
    elif args.dry_run:
        return cmd_dry_run(project_root, entries, args.module, args.all)
    elif args.apply:
        return cmd_apply(project_root, entries, args.module, args.all)
    elif args.report:
        return cmd_report(project_root, entries)

    return 0


if __name__ == "__main__":
    sys.exit(main())
