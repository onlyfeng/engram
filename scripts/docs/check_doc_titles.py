#!/usr/bin/env python3
"""
文档 H1 标题重复检查脚本

功能：
- 扫描 docs/** 与 apps/*/docs/** 中的 Markdown 文件
- 提取 H1 标题（^# ）并统计重复
- 对重复标题输出文件列表与建议

用法：
    python check_doc_titles.py
    python check_doc_titles.py --strict  # 严格模式，发现重复返回非零退出码
    python check_doc_titles.py --output ./custom_output_dir
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 默认扫描目录模式
DEFAULT_SCAN_PATTERNS = [
    "docs",
    "apps/*/docs",
]

# 排除的目录
EXCLUDED_DIRS = [
    "docs/legacy",
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".tmp",
]

# 默认输出目录
DEFAULT_OUTPUT_DIR = ".tmp"

# H1 标题正则表达式（行首 # 后跟空格和标题内容）
H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass
class TitleInfo:
    """文档标题信息"""

    file_path: str  # 相对于项目根目录的文件路径
    title: str  # H1 标题内容
    line_number: int  # 标题所在行号


@dataclass
class DuplicateGroup:
    """重复标题组"""

    title: str  # 标题内容
    files: List[TitleInfo] = field(default_factory=list)
    suggested_canonical: Optional[str] = None  # 建议的规范文件
    suggestion: str = ""  # 处理建议


@dataclass
class TitleCheckReport:
    """标题检查报告"""

    scan_dirs: List[str] = field(default_factory=list)
    files_scanned: int = 0
    titles_found: int = 0
    duplicate_count: int = 0  # 重复标题数（不同标题的数量）
    affected_files: int = 0  # 受影响的文件数
    duplicates: List[DuplicateGroup] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "scan_dirs": self.scan_dirs,
            "files_scanned": self.files_scanned,
            "titles_found": self.titles_found,
            "duplicate_count": self.duplicate_count,
            "affected_files": self.affected_files,
            "duplicates": [
                {
                    "title": dup.title,
                    "files": [
                        {
                            "file_path": f.file_path,
                            "line_number": f.line_number,
                        }
                        for f in dup.files
                    ],
                    "suggested_canonical": dup.suggested_canonical,
                    "suggestion": dup.suggestion,
                }
                for dup in self.duplicates
            ],
            "summary": {
                "status": "ok" if self.duplicate_count == 0 else "warning",
                "message": (
                    f"发现 {self.duplicate_count} 个重复标题，影响 {self.affected_files} 个文件"
                    if self.duplicate_count > 0
                    else "无重复 H1 标题"
                ),
            },
        }


def should_exclude_path(path: Path, project_root: Path) -> bool:
    """检查路径是否应该被排除"""
    try:
        rel_path = path.relative_to(project_root)
        rel_str = str(rel_path)

        for excluded in EXCLUDED_DIRS:
            if rel_str.startswith(excluded + "/") or rel_str.startswith(excluded + "\\"):
                return True
            if rel_str == excluded:
                return True

        return False
    except ValueError:
        return False


def extract_h1_title(file_path: Path, project_root: Path) -> Optional[TitleInfo]:
    """从 Markdown 文件中提取第一个 H1 标题"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"警告: 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return None

    lines = content.split("\n")
    for line_num, line in enumerate(lines, 1):
        match = H1_PATTERN.match(line)
        if match:
            title = match.group(1).strip()
            rel_path = str(file_path.relative_to(project_root))
            return TitleInfo(
                file_path=rel_path,
                title=title,
                line_number=line_num,
            )

    return None


def find_markdown_files(
    project_root: Path, scan_patterns: List[str]
) -> List[Path]:
    """查找所有 Markdown 文件"""
    md_files = []

    for pattern in scan_patterns:
        # 处理 glob 模式
        if "*" in pattern:
            # 例如 apps/*/docs
            for match_path in project_root.glob(pattern):
                if match_path.is_dir():
                    for md_file in match_path.rglob("*.md"):
                        if not should_exclude_path(md_file, project_root):
                            md_files.append(md_file)
        else:
            # 普通目录
            scan_dir = project_root / pattern
            if scan_dir.exists() and scan_dir.is_dir():
                for md_file in scan_dir.rglob("*.md"):
                    if not should_exclude_path(md_file, project_root):
                        md_files.append(md_file)

    # 去重并排序
    md_files = sorted(set(md_files))
    return md_files


def suggest_canonical(files: List[TitleInfo]) -> Tuple[Optional[str], str]:
    """
    为重复标题组建议规范文件和处理方式

    优先级规则：
    1. docs/ 下的文件优先于 apps/*/docs/
    2. 00_overview.md 或 README.md 优先
    3. 路径较短的优先
    4. 按字母顺序排序取第一个
    """
    if not files:
        return None, ""

    def score(info: TitleInfo) -> Tuple[int, int, int, str]:
        path = info.file_path
        # 是否在顶层 docs/ 下
        is_top_docs = path.startswith("docs/") and not path.startswith("docs/legacy")
        # 是否为概览文件
        basename = os.path.basename(path)
        is_overview = basename in ("README.md", "00_overview.md")
        # 路径长度
        path_len = len(path)

        return (
            0 if is_top_docs else 1,  # 顶层 docs 优先
            0 if is_overview else 1,  # 概览文件优先
            path_len,  # 短路径优先
            path,  # 字母顺序
        )

    sorted_files = sorted(files, key=score)
    canonical = sorted_files[0]
    others = sorted_files[1:]

    if len(others) == 1:
        suggestion = f"建议: 保留 {canonical.file_path} 作为规范文档，将 {others[0].file_path} 改为 stub（引用规范文档）或合并内容后删除"
    else:
        other_paths = ", ".join(f.file_path for f in others)
        suggestion = f"建议: 保留 {canonical.file_path} 作为规范文档，其余文件 ({other_paths}) 改为 stub 或合并后删除"

    return canonical.file_path, suggestion


def main():
    parser = argparse.ArgumentParser(
        description="检查文档 H1 标题重复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python check_doc_titles.py                    # 默认扫描 docs/ 和 apps/*/docs/
  python check_doc_titles.py --output ./artifacts
  python check_doc_titles.py --strict           # 发现重复返回非零退出码

扫描范围：
  - docs/** 下的所有 .md 文件
  - apps/*/docs/** 下的所有 .md 文件
  - 排除 docs/legacy/ 目录

处理建议：
  - 对重复标题，脚本会建议选定一个 canonical（规范）文件
  - 其余文件建议改为 stub（仅包含指向规范文件的引用）或合并后删除
        """
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录（默认: {DEFAULT_OUTPUT_DIR}）"
    )
    parser.add_argument(
        "--project-root", "-r",
        default=str(PROJECT_ROOT),
        help="项目根目录（默认: 自动检测）"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细输出"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：发现重复标题时返回非零退出码"
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="仅输出 JSON（不输出人类可读的摘要）"
    )

    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    scan_patterns = DEFAULT_SCAN_PATTERNS.copy()

    if not args.json_only:
        print(f"项目根目录: {project_root}")
        print(f"扫描模式: {scan_patterns}")
        print()

    # 查找所有 Markdown 文件
    md_files = find_markdown_files(project_root, scan_patterns)

    if not args.json_only:
        print(f"发现 {len(md_files)} 个 Markdown 文件")

    # 提取所有 H1 标题
    title_map: Dict[str, List[TitleInfo]] = defaultdict(list)
    files_with_titles = 0

    for md_file in md_files:
        title_info = extract_h1_title(md_file, project_root)
        if title_info:
            # 标准化标题（去除多余空格，转小写用于比较）
            normalized_title = " ".join(title_info.title.split()).lower()
            title_map[normalized_title].append(title_info)
            files_with_titles += 1

    # 构建报告
    report = TitleCheckReport(
        scan_dirs=scan_patterns,
        files_scanned=len(md_files),
        titles_found=files_with_titles,
    )

    # 查找重复
    for normalized_title, files in title_map.items():
        if len(files) > 1:
            # 使用第一个文件的原始标题作为显示标题
            display_title = files[0].title

            canonical, suggestion = suggest_canonical(files)

            dup_group = DuplicateGroup(
                title=display_title,
                files=files,
                suggested_canonical=canonical,
                suggestion=suggestion,
            )
            report.duplicates.append(dup_group)
            report.duplicate_count += 1
            report.affected_files += len(files)

    # 按重复文件数量排序（多的在前）
    report.duplicates.sort(key=lambda x: -len(x.files))

    # 确保输出目录存在
    output_dir = project_root / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    # 写入报告
    report_path = output_dir / "doc_titles_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)

    if args.json_only:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        # 输出摘要
        print()
        print("=" * 60)
        print("H1 标题重复检查报告")
        print("=" * 60)
        print(f"扫描文件数: {report.files_scanned}")
        print(f"含 H1 标题: {report.titles_found}")
        print(f"重复标题数: {report.duplicate_count}")
        print(f"受影响文件: {report.affected_files}")
        print(f"报告路径: {report_path}")

        if report.duplicates:
            print()
            print("重复标题列表:")
            print("-" * 60)
            for dup in report.duplicates:
                print(f"\n标题: \"{dup.title}\"")
                print(f"  文件列表 ({len(dup.files)} 个):")
                for f in dup.files:
                    canonical_mark = " [CANONICAL]" if f.file_path == dup.suggested_canonical else ""
                    print(f"    - {f.file_path}:{f.line_number}{canonical_mark}")
                print(f"  {dup.suggestion}")
        else:
            print()
            print("[OK] 无重复 H1 标题")

    # 退出码
    if args.strict and report.duplicate_count > 0:
        if not args.json_only:
            print()
            print(f"[WARN] 发现 {report.duplicate_count} 个重复标题")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
