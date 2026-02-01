#!/usr/bin/env python3
"""
apply_import_migration - 导入语句自动迁移工具

读取 configs/import_migration_map.json 中的 SSOT 映射，使用 AST 解析识别
待迁移的 import 语句，输出改写建议或自动应用改写。

用法:
    # 1. 预览模式 (dry-run) - 查看所有待迁移的导入
    python scripts/codemods/apply_import_migration.py --dry-run

    # 2. 对单个目录应用
    python scripts/codemods/apply_import_migration.py --apply --target tests/logbook/

    # 3. 全量应用
    python scripts/codemods/apply_import_migration.py --apply

    # 4. 应用后验证
    ruff check .
    pytest tests/ -x
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass
class ImportMatch:
    """单个待迁移的导入匹配"""
    file_path: Path
    line_number: int
    original_line: str
    old_module: str
    new_module: str
    symbols: List[str]
    is_from_import: bool
    has_allowlist_marker: bool = False


@dataclass
class MigrationRule:
    """迁移规则"""
    old_module: str
    new_module: str
    symbols: List[str]
    description: str = ""
    note: str = ""


@dataclass
class MigrationStats:
    """迁移统计"""
    files_scanned: int = 0
    files_with_matches: int = 0
    total_imports_found: int = 0
    imports_migrated: int = 0
    imports_skipped_allowlist: int = 0
    files_modified: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def get_repo_root() -> Path:
    """获取仓库根目录"""
    current = Path(__file__).resolve()
    for parent in [current] + list(current.parents):
        if (parent / '.git').exists() or (parent / 'Makefile').exists():
            return parent
    return Path.cwd()


def load_migration_map(repo_root: Path) -> dict:
    """加载迁移映射配置"""
    map_path = repo_root / 'configs' / 'import_migration_map.json'
    if not map_path.exists():
        raise FileNotFoundError(f"迁移映射文件不存在: {map_path}")

    with open(map_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_migration_rules(config: dict) -> Dict[str, MigrationRule]:
    """构建迁移规则字典 {old_module: MigrationRule}"""
    rules = {}

    # 支持新格式 (modules) 和旧格式 (migration_rules)
    items = config.get('modules', []) or config.get('migration_rules', [])

    for item in items:
        old_module = item.get('old_module')
        # 新格式使用 import_target，旧格式使用 new_module
        new_module = item.get('import_target') or item.get('new_module')

        # 跳过没有 import_target 的项（如 CLI-only 迁移）
        if not old_module or not new_module:
            continue

        # 处理带冒号的 import_target（如 "engram.logbook.cli.scm_sync:runner_main"）
        # 只取模块部分
        if ':' in new_module:
            new_module = new_module.split(':')[0]

        rule = MigrationRule(
            old_module=old_module,
            new_module=new_module,
            symbols=item.get('symbols', []),
            description=item.get('description', item.get('notes', '')),
            note=item.get('note', ''),
        )
        rules[rule.old_module] = rule
    return rules


def should_exclude_file(file_path: Path, exclude_patterns: List[str], repo_root: Path) -> bool:
    """检查文件是否应被排除"""
    rel_path = str(file_path.relative_to(repo_root))

    for pattern in exclude_patterns:
        # 简单的 glob 匹配
        if '**' in pattern:
            # 处理 **/ 模式
            pattern_parts = pattern.replace('**/', '').replace('/**', '')
            if pattern_parts in rel_path:
                return True
        elif pattern == file_path.name:
            return True

    return False


def find_python_files(target_dir: Path, exclude_patterns: List[str], repo_root: Path) -> List[Path]:
    """查找所有 Python 文件"""
    python_files = []

    for py_file in target_dir.rglob('*.py'):
        if not should_exclude_file(py_file, exclude_patterns, repo_root):
            python_files.append(py_file)

    return sorted(python_files)


def extract_imports_from_ast(
    file_path: Path,
    rules: Dict[str, MigrationRule],
    allowlist_markers: List[str],
) -> List[ImportMatch]:
    """使用 AST 解析文件中的导入语句"""
    matches = []

    try:
        content = file_path.read_text(encoding='utf-8')
        lines = content.splitlines()
        tree = ast.parse(content, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        # 无法解析的文件跳过
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # import db, import kv 等
            for alias in node.names:
                module_name = alias.name
                if module_name in rules:
                    line_idx = node.lineno - 1
                    original_line = lines[line_idx] if line_idx < len(lines) else ""
                    has_marker = any(marker in original_line for marker in allowlist_markers)

                    matches.append(ImportMatch(
                        file_path=file_path,
                        line_number=node.lineno,
                        original_line=original_line,
                        old_module=module_name,
                        new_module=rules[module_name].new_module,
                        symbols=[],
                        is_from_import=False,
                        has_allowlist_marker=has_marker,
                    ))

        elif isinstance(node, ast.ImportFrom):
            # from db import xxx
            # 只匹配绝对导入 (level == 0)，跳过相对导入 (from .db import)
            if node.level != 0:
                continue

            module_name = node.module or ""
            if module_name in rules:
                line_idx = node.lineno - 1
                original_line = lines[line_idx] if line_idx < len(lines) else ""
                has_marker = any(marker in original_line for marker in allowlist_markers)

                # 收集导入的符号
                symbols = []
                for alias in node.names:
                    symbols.append(alias.name)

                matches.append(ImportMatch(
                    file_path=file_path,
                    line_number=node.lineno,
                    original_line=original_line,
                    old_module=module_name,
                    new_module=rules[module_name].new_module,
                    symbols=symbols,
                    is_from_import=True,
                    has_allowlist_marker=has_marker,
                ))

    return matches


def generate_new_import_line(match: ImportMatch) -> str:
    """生成新的导入语句"""
    if match.is_from_import:
        if match.symbols:
            symbols_str = ", ".join(match.symbols)
            return f"from {match.new_module} import {symbols_str}"
        else:
            return f"from {match.new_module} import *"
    else:
        return f"import {match.new_module}"


def apply_migration_to_file(
    file_path: Path,
    matches: List[ImportMatch],
    dry_run: bool,
) -> Tuple[bool, List[str]]:
    """
    对单个文件应用迁移

    Returns:
        (是否修改, 修改详情列表)
    """
    # 过滤掉有 allowlist marker 的匹配
    actionable_matches = [m for m in matches if not m.has_allowlist_marker]

    if not actionable_matches:
        return False, []

    content = file_path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    changes = []

    # 按行号降序处理，避免行号偏移
    sorted_matches = sorted(actionable_matches, key=lambda m: m.line_number, reverse=True)

    for match in sorted_matches:
        line_idx = match.line_number - 1
        if line_idx >= len(lines):
            continue

        old_line = lines[line_idx]
        new_import = generate_new_import_line(match)

        # 保留原有的缩进和注释
        indent_match = re.match(r'^(\s*)', old_line)
        indent = indent_match.group(1) if indent_match else ""

        # 检查是否有尾部注释
        comment_match = re.search(r'(\s*#.*)$', old_line.rstrip('\n\r'))
        comment = comment_match.group(1) if comment_match else ""

        # 构建新行
        new_line = f"{indent}{new_import}{comment}\n"
        lines[line_idx] = new_line

        changes.append(f"  L{match.line_number}: {match.original_line.strip()}")
        changes.append(f"       -> {new_import}")

    if not dry_run:
        new_content = ''.join(lines)
        file_path.write_text(new_content, encoding='utf-8')

    return True, changes


def print_dry_run_report(all_matches: List[ImportMatch], rules: Dict[str, MigrationRule]) -> None:
    """输出 dry-run 报告"""
    print("=" * 70)
    print("导入迁移预览 (--dry-run)")
    print("=" * 70)
    print()

    # 按文件分组
    by_file: Dict[Path, List[ImportMatch]] = {}
    for match in all_matches:
        if match.file_path not in by_file:
            by_file[match.file_path] = []
        by_file[match.file_path].append(match)

    if not by_file:
        print("未发现需要迁移的导入语句。")
        print()
        return

    # 统计
    total_actionable = sum(1 for m in all_matches if not m.has_allowlist_marker)
    total_skipped = sum(1 for m in all_matches if m.has_allowlist_marker)

    print(f"发现 {len(by_file)} 个文件中的 {len(all_matches)} 处导入")
    print(f"  - 可迁移: {total_actionable}")
    print(f"  - 跳过 (allowlist): {total_skipped}")
    print()

    # 按模块统计
    print("按模块统计:")
    print("-" * 40)
    by_module: Dict[str, int] = {}
    for match in all_matches:
        if not match.has_allowlist_marker:
            by_module[match.old_module] = by_module.get(match.old_module, 0) + 1

    for old_module, count in sorted(by_module.items(), key=lambda x: -x[1]):
        rule = rules.get(old_module)
        new_module = rule.new_module if rule else "?"
        print(f"  {old_module} -> {new_module}: {count} 处")

    print()
    print("详细列表:")
    print("-" * 40)

    for file_path, matches in sorted(by_file.items()):
        print(f"\n{file_path}:")
        for match in sorted(matches, key=lambda m: m.line_number):
            marker = " [SKIP: allowlist]" if match.has_allowlist_marker else ""
            new_import = generate_new_import_line(match)
            print(f"  L{match.line_number}: {match.original_line.strip()}{marker}")
            if not match.has_allowlist_marker:
                print(f"       -> {new_import}")

    print()
    print("=" * 70)
    print("使用 --apply 执行实际迁移")
    print("使用 --apply --target <dir> 仅对指定目录执行")
    print("=" * 70)


def print_apply_report(stats: MigrationStats) -> None:
    """输出应用报告"""
    print()
    print("=" * 70)
    print("导入迁移完成")
    print("=" * 70)
    print()
    print(f"扫描文件数: {stats.files_scanned}")
    print(f"匹配文件数: {stats.files_with_matches}")
    print(f"发现导入数: {stats.total_imports_found}")
    print(f"已迁移数:   {stats.imports_migrated}")
    print(f"跳过数 (allowlist): {stats.imports_skipped_allowlist}")
    print()

    if stats.files_modified:
        print("已修改文件:")
        for f in stats.files_modified:
            print(f"  - {f}")
        print()

    if stats.errors:
        print("错误:")
        for err in stats.errors:
            print(f"  - {err}")
        print()

    print("后续步骤:")
    print("  1. 运行 ruff check . 检查代码风格")
    print("  2. 运行 pytest tests/ -x 验证测试")
    print("  3. 审查修改并提交")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='导入语句自动迁移工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 预览所有待迁移的导入
  python scripts/codemods/apply_import_migration.py --dry-run

  # 对单个目录应用迁移
  python scripts/codemods/apply_import_migration.py --apply --target tests/logbook/

  # 全量应用迁移
  python scripts/codemods/apply_import_migration.py --apply

  # 应用后验证
  ruff check .
  pytest tests/ -x
""",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--dry-run',
        action='store_true',
        help='预览模式：仅显示计划变更，不实际修改文件',
    )
    group.add_argument(
        '--apply',
        action='store_true',
        help='执行模式：实际修改文件中的导入语句',
    )

    parser.add_argument(
        '--target',
        type=str,
        default=None,
        help='目标目录（相对于仓库根）。不指定则扫描整个仓库。',
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='详细输出',
    )

    args = parser.parse_args()

    repo_root = get_repo_root()
    print(f"仓库根目录: {repo_root}")

    # 加载配置
    try:
        config = load_migration_map(repo_root)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"错误: 配置文件格式错误 - {e}", file=sys.stderr)
        return 1

    rules = build_migration_rules(config)
    # 支持单个 marker（兼容旧配置）或多个 markers
    allowlist_markers = config.get('allowlist_markers', [])
    if not allowlist_markers:
        # 兼容旧配置格式或使用默认值
        single_marker = config.get('allowlist_marker', 'ROOT-WRAPPER-ALLOW')
        allowlist_markers = [single_marker, 'allowlist:']  # 默认支持两种格式
    exclude_patterns = config.get('exclude_patterns', [])
    # 默认排除模式
    if not exclude_patterns:
        exclude_patterns = [
            '**/__pycache__/**',
            '**/node_modules/**',
            '**/.git/**',
            '**/venv/**',
            '**/.venv/**',
            '**/archives/**',
            # 排除根目录 wrapper 文件本身
            'db.py', 'kv.py', 'scm_repo.py', 'artifacts.py',
            'db_bootstrap.py', 'db_migrate.py', 'identity_sync.py',
        ]

    # 确定扫描目录
    if args.target:
        target_dir = repo_root / args.target
        if not target_dir.exists():
            print(f"错误: 目标目录不存在: {target_dir}", file=sys.stderr)
            return 1
    else:
        target_dir = repo_root

    print(f"扫描目录: {target_dir}")
    print(f"迁移规则数: {len(rules)}")
    print()

    # 查找 Python 文件
    python_files = find_python_files(target_dir, exclude_patterns, repo_root)
    print(f"发现 {len(python_files)} 个 Python 文件")

    # 收集所有匹配
    all_matches: List[ImportMatch] = []
    stats = MigrationStats()

    for py_file in python_files:
        stats.files_scanned += 1
        matches = extract_imports_from_ast(py_file, rules, allowlist_markers)

        if matches:
            stats.files_with_matches += 1
            stats.total_imports_found += len(matches)
            all_matches.extend(matches)

            if args.verbose:
                print(f"  {py_file}: {len(matches)} 处匹配")

    # Dry-run 模式
    if args.dry_run:
        print_dry_run_report(all_matches, rules)
        return 0

    # Apply 模式
    print()
    print("正在应用迁移...")

    by_file: Dict[Path, List[ImportMatch]] = {}
    for match in all_matches:
        if match.file_path not in by_file:
            by_file[match.file_path] = []
        by_file[match.file_path].append(match)

    for file_path, matches in by_file.items():
        try:
            modified, changes = apply_migration_to_file(file_path, matches, dry_run=False)

            if modified:
                stats.files_modified.append(str(file_path.relative_to(repo_root)))
                stats.imports_migrated += len([m for m in matches if not m.has_allowlist_marker])
                stats.imports_skipped_allowlist += len([m for m in matches if m.has_allowlist_marker])

                if args.verbose:
                    print(f"\n修改: {file_path}")
                    for change in changes:
                        print(change)
            else:
                stats.imports_skipped_allowlist += len(matches)

        except Exception as e:
            stats.errors.append(f"{file_path}: {e}")

    print_apply_report(stats)

    if stats.errors:
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
