#!/usr/bin/env python3
"""
[HISTORICAL] 文档迁移工具

================================================================================
状态: 已完成迁移，仅供审计
迁移日期: 2026-01-30
说明: 本脚本用于将分散在 apps/*/docs/ 下的文档集中迁移到 docs/ 目录。
      迁移工作已完成，本文件保留用于：
      1. 审计追溯 - 了解文档迁移的历史过程
      2. 参考实现 - 如需类似迁移可参考本实现

      请勿再次运行本脚本执行迁移操作。
================================================================================

读取 scripts/docs_migration_map.json 映射配置，执行文档迁移并重写链接。

功能:
- --dry-run: 仅输出计划变更，不实际执行
- --apply: 实际移动文件并重写链接
- 迁移后自动调用 check_links.py 验证

用法:
    python migrate_docs.py --dry-run   # 预览变更
    python migrate_docs.py --apply     # 执行迁移
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Markdown 链接正则表达式
MD_LINK_PATTERN = re.compile(
    r'(\[([^\]]*)\]\()([^)]+)(\))',
    re.MULTILINE
)


def get_repo_root() -> Path:
    """获取仓库根目录"""
    current = Path(__file__).resolve()
    for parent in [current] + list(current.parents):
        if (parent / '.git').exists() or (parent / 'Makefile').exists():
            return parent
    return Path.cwd()


def load_migration_map(repo_root: Path) -> dict:
    """加载迁移映射配置"""
    map_path = repo_root / 'scripts' / 'docs' / 'legacy' / 'docs_migration_map.json'
    if not map_path.exists():
        raise FileNotFoundError(f"迁移映射文件不存在: {map_path}")

    with open(map_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_path_mapping(migration_map: dict) -> Dict[str, str]:
    """
    构建路径映射字典

    返回: {source_path: target_path}
    """
    mapping = {}
    for item in migration_map.get('file_mappings', []):
        source = item['source']
        target = item['target']
        mapping[source] = target
    return mapping


def compute_relative_path(from_file: str, to_file: str) -> str:
    """
    计算从一个文件到另一个文件的相对路径

    Args:
        from_file: 源文件路径（相对于仓库根）
        to_file: 目标文件路径（相对于仓库根）

    Returns:
        相对路径字符串
    """
    from_path = Path(from_file).parent
    to_path = Path(to_file)

    try:
        rel_path = os.path.relpath(to_path, from_path)
        # 确保路径格式一致（使用正斜杠）
        rel_path = rel_path.replace('\\', '/')
        return rel_path
    except ValueError:
        # 在 Windows 上跨驱动器时可能失败
        return to_file


def rewrite_md_link(
    link_target: str,
    current_file_old_path: str,
    current_file_new_path: str,
    path_mapping: Dict[str, str],
    repo_root: Path
) -> Tuple[str, bool]:
    """
    重写单个 Markdown 链接

    Args:
        link_target: 原始链接目标
        current_file_old_path: 当前文件的原路径（相对于仓库根）
        current_file_new_path: 当前文件的新路径（相对于仓库根）
        path_mapping: 文件迁移映射 {old: new}
        repo_root: 仓库根目录

    Returns:
        (新链接目标, 是否被修改)
    """
    # 跳过外部链接
    if any(link_target.startswith(prefix) for prefix in ('http://', 'https://', 'mailto:', 'ftp://')):
        return link_target, False

    # 跳过纯锚点链接
    if link_target.startswith('#'):
        return link_target, False

    # 分离锚点
    if '#' in link_target:
        path_part, anchor = link_target.split('#', 1)
        anchor = '#' + anchor
    else:
        path_part, anchor = link_target, ''

    if not path_part:
        return link_target, False

    # 解析链接指向的绝对路径（相对于仓库根）
    old_file_dir = Path(current_file_old_path).parent
    linked_file_path = (old_file_dir / path_part).as_posix()

    # 规范化路径（处理 ../ 等）
    try:
        linked_file_path = os.path.normpath(linked_file_path).replace('\\', '/')
    except Exception:
        return link_target, False

    # 检查链接目标是否在迁移映射中
    if linked_file_path in path_mapping:
        # 目标文件也被迁移了，计算新的相对路径
        new_linked_path = path_mapping[linked_file_path]
        new_rel_path = compute_relative_path(current_file_new_path, new_linked_path)
        return new_rel_path + anchor, True

    # 目标文件未迁移，但当前文件迁移了，需要重新计算相对路径
    if current_file_old_path != current_file_new_path:
        # 检查原始链接指向的文件是否存在
        old_target_abs = repo_root / linked_file_path
        if old_target_abs.exists():
            new_rel_path = compute_relative_path(current_file_new_path, linked_file_path)
            return new_rel_path + anchor, True

    return link_target, False


def rewrite_file_content(
    content: str,
    old_path: str,
    new_path: str,
    path_mapping: Dict[str, str],
    repo_root: Path
) -> Tuple[str, List[Tuple[str, str]]]:
    """
    重写文件内容中的所有链接

    Returns:
        (新内容, [(原链接, 新链接), ...])
    """
    changes = []

    def replace_link(match):
        prefix = match.group(1)  # [text](
        match.group(2)  # text
        link_target = match.group(3)  # path
        suffix = match.group(4)  # )

        new_target, changed = rewrite_md_link(
            link_target, old_path, new_path, path_mapping, repo_root
        )

        if changed:
            changes.append((link_target, new_target))

        return f"{prefix}{new_target}{suffix}"

    new_content = MD_LINK_PATTERN.sub(replace_link, content)
    return new_content, changes


def rewrite_reference_in_code(
    content: str,
    rewrite_rules: List[dict],
    file_path: str
) -> Tuple[str, List[Tuple[str, str]]]:
    """
    根据重写规则处理代码/配置文件中的文档引用

    Returns:
        (新内容, [(原引用, 新引用), ...])
    """
    changes = []

    # 查找适用于当前文件的规则
    for rule in rewrite_rules:
        if rule.get('path') != file_path:
            continue

        current_ref = rule.get('current_reference')
        new_ref = rule.get('new_reference')

        if not current_ref or not new_ref:
            continue

        if current_ref in content:
            content = content.replace(current_ref, new_ref)
            changes.append((current_ref, new_ref))

    return content, changes


def find_all_md_files(repo_root: Path, exclude_dirs: Set[str] = None) -> List[Path]:
    """查找所有 Markdown 文件"""
    if exclude_dirs is None:
        exclude_dirs = {'node_modules', '.git', 'archives', '__pycache__'}

    md_files = []
    for root, dirs, files in os.walk(repo_root):
        # 排除目录
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for file in files:
            if file.endswith('.md'):
                md_files.append(Path(root) / file)

    return md_files


def run_link_check(repo_root: Path, migration_map: dict) -> bool:
    """
    运行链接检查脚本

    Returns:
        True 如果检查通过
    """
    check_script = repo_root / 'scripts' / 'docs' / 'check_links.py'

    if not check_script.exists():
        print("Warning: 链接检查脚本不存在，跳过验证", file=sys.stderr)
        return True

    # 收集迁移后的目标目录
    target_dirs = set()
    for item in migration_map.get('file_mappings', []):
        target_path = Path(item['target'])
        target_dirs.add(str(target_path.parent))

    # 构建命令行参数（使用位置参数）
    cmd = [sys.executable, str(check_script)]
    cmd.extend(sorted(target_dirs))

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print("链接检查失败:", file=sys.stderr)
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            return False

        print(result.stdout)
        return True

    except Exception as e:
        print(f"运行链接检查时出错: {e}", file=sys.stderr)
        return False


def dry_run(repo_root: Path, migration_map: dict) -> int:
    """
    预览模式：显示计划的变更
    """
    path_mapping = build_path_mapping(migration_map)

    print("=" * 60)
    print("文档迁移预览 (--dry-run)")
    print("=" * 60)
    print()

    # 1. 文件移动计划
    print("📁 文件移动计划:")
    print("-" * 40)

    for old_path, new_path in path_mapping.items():
        old_abs = repo_root / old_path
        status = "✓ 存在" if old_abs.exists() else "✗ 不存在"
        print(f"  {old_path}")
        print(f"    → {new_path}")
        print(f"      [{status}]")
        print()

    # 2. 链接重写预览
    print()
    print("🔗 链接重写预览:")
    print("-" * 40)

    total_link_changes = 0

    for old_path, new_path in path_mapping.items():
        old_abs = repo_root / old_path
        if not old_abs.exists():
            continue

        try:
            content = old_abs.read_text(encoding='utf-8')
        except Exception:
            continue

        _, changes = rewrite_file_content(
            content, old_path, new_path, path_mapping, repo_root
        )

        if changes:
            print(f"  {old_path}:")
            for old_link, new_link in changes:
                print(f"    {old_link} → {new_link}")
            total_link_changes += len(changes)

    if total_link_changes == 0:
        print("  (无链接需要重写)")

    # 3. 引用重写预览
    print()
    print("📝 代码引用重写预览:")
    print("-" * 40)

    rewrite_rules = migration_map.get('reference_rewrite_rules', {}).get('files', [])
    ref_changes = 0

    for rule in rewrite_rules:
        if rule.get('current_reference') and rule.get('new_reference'):
            print(f"  {rule['path']}:")
            print(f"    {rule['current_reference']} → {rule['new_reference']}")
            ref_changes += 1
        elif rule.get('action') == 'review':
            print(f"  {rule['path']}: [需要人工审查]")

    if ref_changes == 0:
        print("  (无引用需要自动重写)")

    # 4. 汇总
    print()
    print("=" * 60)
    print("汇总:")
    print(f"  - 待移动文件: {len(path_mapping)}")
    print(f"  - 待重写链接: {total_link_changes}")
    print(f"  - 待重写引用: {ref_changes}")
    print()
    print("使用 --apply 执行实际迁移")
    print("=" * 60)

    return 0


def apply_migration(repo_root: Path, migration_map: dict) -> int:
    """
    执行实际迁移
    """
    path_mapping = build_path_mapping(migration_map)

    print("=" * 60)
    print("执行文档迁移 (--apply)")
    print("=" * 60)
    print()

    errors = []

    # 1. 创建目标目录
    print("📁 创建目标目录...")
    target_dirs = migration_map.get('target_directories', {})
    for name, dir_path in target_dirs.items():
        target_dir = repo_root / dir_path
        target_dir.mkdir(parents=True, exist_ok=True)
        print(f"  ✓ {dir_path}")
    print()

    # 2. 移动文件并重写链接
    print("📄 迁移文件并重写链接...")

    for old_path, new_path in path_mapping.items():
        old_abs = repo_root / old_path
        new_abs = repo_root / new_path

        if not old_abs.exists():
            print(f"  ✗ 跳过（源文件不存在）: {old_path}")
            errors.append(f"源文件不存在: {old_path}")
            continue

        try:
            # 读取内容
            content = old_abs.read_text(encoding='utf-8')

            # 重写链接
            new_content, changes = rewrite_file_content(
                content, old_path, new_path, path_mapping, repo_root
            )

            # 确保目标目录存在
            new_abs.parent.mkdir(parents=True, exist_ok=True)

            # 写入新位置
            new_abs.write_text(new_content, encoding='utf-8')

            # 删除原文件
            old_abs.unlink()

            status = f"({len(changes)} 链接重写)" if changes else ""
            print(f"  ✓ {old_path} → {new_path} {status}")

        except Exception as e:
            print(f"  ✗ 迁移失败: {old_path} - {e}")
            errors.append(f"迁移失败 {old_path}: {e}")

    print()

    # 3. 重写其他文件中的引用
    print("🔗 更新其他文件中的引用...")

    rewrite_rules = migration_map.get('reference_rewrite_rules', {}).get('files', [])

    for rule in rewrite_rules:
        file_path = rule.get('path')
        current_ref = rule.get('current_reference')
        new_ref = rule.get('new_reference')

        if not current_ref or not new_ref:
            if rule.get('action') == 'review':
                print(f"  ⚠ 需要人工审查: {file_path}")
            continue

        file_abs = repo_root / file_path
        if not file_abs.exists():
            print(f"  ✗ 文件不存在: {file_path}")
            continue

        try:
            content = file_abs.read_text(encoding='utf-8')

            if current_ref in content:
                new_content = content.replace(current_ref, new_ref)
                file_abs.write_text(new_content, encoding='utf-8')
                print(f"  ✓ {file_path}: {current_ref} → {new_ref}")
            else:
                print(f"  - {file_path}: 未找到引用 '{current_ref}'")

        except Exception as e:
            print(f"  ✗ 更新失败: {file_path} - {e}")
            errors.append(f"更新引用失败 {file_path}: {e}")

    print()

    # 4. 更新所有已迁移目录中的 MD 文件的跨文件引用
    print("🔄 更新已迁移目录中的交叉引用...")

    new_docs_dirs = set()
    for new_path in path_mapping.values():
        new_docs_dirs.add(Path(new_path).parent)

    for docs_dir in new_docs_dirs:
        docs_abs = repo_root / docs_dir
        if not docs_abs.exists():
            continue

        for md_file in docs_abs.glob('*.md'):
            try:
                content = md_file.read_text(encoding='utf-8')
                rel_path = str(md_file.relative_to(repo_root)).replace('\\', '/')

                # 这个文件如果在映射中，已经处理过了
                if rel_path in path_mapping.values():
                    continue

                # 检查是否有指向旧路径的链接
                new_content, changes = rewrite_file_content(
                    content, rel_path, rel_path, path_mapping, repo_root
                )

                if changes:
                    md_file.write_text(new_content, encoding='utf-8')
                    print(f"  ✓ {rel_path}: {len(changes)} 链接更新")

            except Exception as e:
                print(f"  ✗ 处理失败: {md_file} - {e}")

    print()

    # 5. 运行链接检查
    print("=" * 60)
    print("🔍 运行链接检查验证...")
    print()

    if not run_link_check(repo_root, migration_map):
        print()
        print("❌ 链接检查失败！请检查上述错误。")
        return 1

    print()
    print("=" * 60)

    if errors:
        print(f"⚠ 迁移完成，但有 {len(errors)} 个警告:")
        for err in errors:
            print(f"  - {err}")
        return 1
    else:
        print("✅ 迁移完成！")

    print("=" * 60)
    return 0


def main():
    print("=" * 60)
    print("[HISTORICAL] 本脚本已完成历史使命，仅供审计参考")
    print("迁移工作已于 2026-01-30 完成")
    print("=" * 60)
    print()

    parser = argparse.ArgumentParser(
        description='[HISTORICAL] 文档迁移工具 - 已完成迁移，仅供审计'
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--dry-run',
        action='store_true',
        help='预览模式：仅显示计划变更，不实际执行'
    )
    group.add_argument(
        '--apply',
        action='store_true',
        help='执行模式：实际移动文件并重写链接'
    )

    args = parser.parse_args()

    repo_root = get_repo_root()
    print(f"仓库根目录: {repo_root}")
    print()

    try:
        migration_map = load_migration_map(repo_root)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"错误: 迁移映射文件格式错误 - {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        sys.exit(dry_run(repo_root, migration_map))
    elif args.apply:
        sys.exit(apply_migration(repo_root, migration_map))


if __name__ == '__main__':
    main()
