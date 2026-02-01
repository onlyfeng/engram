#!/usr/bin/env python3
"""
文档链接检查脚本

功能：
- 扫描 Markdown 文件中的本地链接
- 验证引用的文件是否存在
- 支持 Markdown 链接和纯文本路径引用
- 支持扫描目录或单个文件
- 区分来源类型（entrypoints vs docs）
- 输出 JSON 格式的报告

用法：
    python check_links.py [目录或文件1] [目录或文件2] ...
    python check_links.py --output ./custom_output_dir
    python check_links.py --ignore-patterns "pattern1" "pattern2"
    python check_links.py --entrypoints  # 仅扫描入口文件（README.md 系列）
"""

import argparse
import glob as glob_module
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Literal, Optional, Set

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 来源类型定义
SourceType = Literal["entrypoint", "docs"]

# 默认扫描目录（用于 docs 类型）
DEFAULT_SCAN_DIRS = [
    "docs/logbook",
    "docs/gateway",
    "docs/seekdb",
    "docs/contracts",
    "docs/architecture",
    "docs/openmemory",
    "docs/reference",
    "docs/guides",
    "docs/acceptance",
    "docs/ci_nightly_workflow_refactor",
]

# 默认扫描文件（独立文件，不属于默认目录）
DEFAULT_SCAN_FILES = [
    "docs/README.md",
]

# 排除的目录（在扫描时跳过）
EXCLUDED_DIRS = [
    "docs/legacy",
]

# 默认入口文件（entrypoints）：README.md 系列
DEFAULT_ENTRYPOINT_FILES = [
    "README.md",
    "docs/README.md",
    "apps/logbook_postgres/README.md",
    "apps/openmemory_gateway/README.md",
    "apps/seekdb_rag_hybrid/README.md",
]

# 入口文件 glob 模式（用于自动发现 README.md）
ENTRYPOINT_GLOB_PATTERNS = [
    "README.md",
    "docs/README.md",
    "docs/*/README.md",
    "apps/*/README.md",
    "apps/*/docs/README.md",
]

# apps 目录下 docs 子目录的 glob 模式
APPS_DOCS_GLOB_PATTERN = "apps/*/docs"

# 默认输出目录
DEFAULT_OUTPUT_DIR = ".tmp"

# 忽略的 URL 模式
IGNORED_URL_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "ftp://",
    "data:",
    "javascript:",
)

# 内置忽略模式列表（用于过滤误报）
BUILTIN_IGNORE_PATTERNS = [
    # 产品/技术名称（非文件引用）
    "Node.js",
    "node.js",
    # 示例/占位路径
    "/path/to/",
    # 待实现的脚本（标记为未来计划）
    "scripts/ops/s3_hardening.sh",
    # 代码块内的脚本调用（无路径前缀的 CLI 工具名）
    "logbook_cli.py",
    "artifact_cli.py",
    "artifact_migrate.py",
    "artifact_gc.py",
    "db_migrate.py",
    "identity_sync.py",
    "render_views.py",
    "scm_materialize_patch_blob.py",
    "scm_sync_svn.py",
    "scm_sync_gitlab.py",
    "logbook_adapter.py",
    "memory_writer.py",
    "memory_reader.py",
    "migrate.ts",
    "test_multi_schema.ts",
    # 运行时/产物路径（不在仓库内）
    ".artifacts/",
    ".agentx/",
    "smoke_test/",
]

# Markdown 链接正则：匹配 [text](path) 和 ![alt](path)
MD_LINK_PATTERN = re.compile(r'!?\[([^\]]*)\]\(([^)]+)\)')

# 纯文本路径引用的前缀模式
PATH_PREFIXES = [
    "templates/",
    "gateway/",
    "docs/",
    "apps/",
    "scripts/",
    "libs/",
    "sql/",
    "compose/",
    "tests/",
    "./",   # 相对当前目录
    "../",  # 相对上级目录
]

# 常见文件扩展名
COMMON_EXTENSIONS = [
    ".md", ".py", ".sh", ".json", ".yaml", ".yml",
    ".sql", ".toml", ".txt", ".html", ".css",
    ".ts", ".tsx", ".jsx", ".env", ".example",
]

# 需要排除的误报模式（常见自然语言中的扩展名引用）
FALSE_POSITIVE_PATTERNS = [
    r'^Node\.js$',           # Node.js 是产品名称
    r'^\.js$',               # 纯扩展名
    r'^\.py$',
    r'^\.md$',
    r'^index\.md$',          # 常见占位符
    r'^readme\.md$',
    r'^\w+\.example$',       # 通用示例文件引用
]

# 纯文本路径引用正则模式
# 匹配以特定前缀开头的路径，或包含 / 且以常见扩展名结尾的路径
def build_text_path_pattern() -> re.Pattern:
    """构建纯文本路径检测的正则表达式"""
    # 前缀匹配：templates/xxx, gateway/xxx 等
    prefix_patterns = [re.escape(p) + r'[a-zA-Z0-9_\-./]+' for p in PATH_PREFIXES]

    # 扩展名匹配：仅匹配包含路径分隔符的引用（如 path/to/file.py）
    # 这样可以避免将 "artifact_gc.py" 这类仅文件名的引用误判
    ext_patterns = [r'[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-./]+' + re.escape(ext) for ext in COMMON_EXTENSIONS]

    # 合并所有模式
    all_patterns = prefix_patterns + ext_patterns
    combined = r'(?:' + '|'.join(all_patterns) + r')'

    # 确保路径不是 URL 的一部分，使用单词边界或特定分隔符
    # 匹配 ` ` (反引号包围)、空格后、或行首的路径
    return re.compile(r'(?:^|[\s`"\'\(])(' + combined + r')(?:[\s`"\'\)]|$)', re.MULTILINE)


TEXT_PATH_PATTERN = build_text_path_pattern()
FALSE_POSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in FALSE_POSITIVE_PATTERNS]


def is_false_positive(path: str) -> bool:
    """检查是否为误报的路径"""
    return any(p.match(path) for p in FALSE_POSITIVE_RE)


@dataclass
class BrokenLink:
    """表示一个失效链接"""
    source_file: str  # 源文件路径（相对于项目根目录）
    line_number: int  # 行号
    link_type: str    # 链接类型: "markdown" 或 "text_reference"
    target_path: str  # 目标路径（原始文本）
    resolved_path: str  # 解析后的完整路径
    reason: str       # 失效原因
    source_type: str = "docs"  # 来源类型: "entrypoint" 或 "docs"


@dataclass
class LinkReport:
    """链接检查报告"""
    scan_dirs: List[str] = field(default_factory=list)
    scan_files: List[str] = field(default_factory=list)  # 新增：单独扫描的文件
    files_scanned: int = 0
    total_links_checked: int = 0
    broken_links: List[BrokenLink] = field(default_factory=list)
    ignored_patterns: List[str] = field(default_factory=list)
    # 按来源类型统计
    entrypoint_files_scanned: int = 0
    docs_files_scanned: int = 0
    entrypoint_broken_count: int = 0
    docs_broken_count: int = 0

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "scan_dirs": self.scan_dirs,
            "scan_files": self.scan_files,
            "files_scanned": self.files_scanned,
            "total_links_checked": self.total_links_checked,
            "broken_count": len(self.broken_links),
            "broken_links": [asdict(link) for link in self.broken_links],
            "ignored_patterns": self.ignored_patterns,
            # 分类统计
            "by_source_type": {
                "entrypoint": {
                    "files_scanned": self.entrypoint_files_scanned,
                    "broken_count": self.entrypoint_broken_count,
                },
                "docs": {
                    "files_scanned": self.docs_files_scanned,
                    "broken_count": self.docs_broken_count,
                },
            },
        }


def is_anchor_only(path: str) -> bool:
    """检查是否为纯锚点链接"""
    return path.startswith("#")


def should_ignore_url(path: str) -> bool:
    """检查是否应该忽略的 URL"""
    path_lower = path.lower()
    return any(path_lower.startswith(prefix) for prefix in IGNORED_URL_PREFIXES)


def resolve_link_path(source_file: Path, target_path: str, project_root: Path) -> Path:
    """
    解析链接路径

    Args:
        source_file: 源文件路径
        target_path: 目标路径（可能是相对路径或绝对路径）
        project_root: 项目根目录

    Returns:
        解析后的完整路径
    """
    # 移除锚点
    clean_path = target_path.split("#")[0] if "#" in target_path else target_path

    # 移除查询字符串
    clean_path = clean_path.split("?")[0] if "?" in clean_path else clean_path

    if not clean_path:
        return source_file  # 纯锚点链接，返回源文件

    # 如果是绝对路径（相对于项目根目录）
    if clean_path.startswith("/"):
        return project_root / clean_path.lstrip("/")

    # 相对路径，相对于源文件所在目录
    return (source_file.parent / clean_path).resolve()


def extract_markdown_links(content: str) -> List[tuple]:
    """
    提取 Markdown 链接

    Returns:
        List of (line_number, link_text, target_path)
    """
    links = []
    for line_num, line in enumerate(content.split("\n"), 1):
        for match in MD_LINK_PATTERN.finditer(line):
            link_text = match.group(1)
            target_path = match.group(2).strip()
            links.append((line_num, link_text, target_path))
    return links


def extract_text_references(content: str) -> List[tuple]:
    """
    提取纯文本路径引用

    Returns:
        List of (line_number, target_path)
    """
    references = []
    for line_num, line in enumerate(content.split("\n"), 1):
        # 跳过 Markdown 链接行（已单独处理）
        # 但仍然检查行内的纯文本引用
        for match in TEXT_PATH_PATTERN.finditer(line):
            path = match.group(1).strip()
            # 过滤掉已在 Markdown 链接中的路径
            if f"]({path})" not in line and f"({path})" not in line:
                # 过滤误报
                if not is_false_positive(path):
                    references.append((line_num, path))
    return references


def is_entrypoint_file(file_path: Path, project_root: Path) -> bool:
    """
    判断文件是否为入口文件（entrypoint）

    入口文件包括：
    - 项目根目录的 README.md
    - docs/README.md
    - docs/*/README.md
    - apps/*/README.md
    - apps/*/docs/README.md
    """
    try:
        rel_path = file_path.relative_to(project_root)
        rel_str = str(rel_path)
        parts = rel_path.parts

        # 项目根目录的 README.md
        if rel_str == "README.md":
            return True

        # docs/README.md
        if rel_str == "docs/README.md":
            return True

        # docs/*/README.md 模式（如 docs/logbook/README.md）
        if len(parts) == 3 and parts[0] == "docs" and parts[2] == "README.md":
            return True

        # apps/*/README.md 模式
        if len(parts) == 3 and parts[0] == "apps" and parts[2] == "README.md":
            return True

        # apps/*/docs/README.md 模式
        if len(parts) == 4 and parts[0] == "apps" and parts[2] == "docs" and parts[3] == "README.md":
            return True

        return False
    except ValueError:
        return False


def check_file_links(
    file_path: Path,
    project_root: Path,
    ignore_patterns: Set[str],
    source_type: Optional[str] = None
) -> tuple:
    """
    检查单个文件中的链接

    Args:
        file_path: 文件路径
        project_root: 项目根目录
        ignore_patterns: 忽略的模式集合
        source_type: 来源类型，如果为 None 则自动检测

    Returns:
        (checked_count, broken_links)
    """
    broken_links = []
    checked_count = 0

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"警告: 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return 0, []

    relative_source = file_path.relative_to(project_root)

    # 自动检测来源类型
    if source_type is None:
        source_type = "entrypoint" if is_entrypoint_file(file_path, project_root) else "docs"

    # 检查 Markdown 链接
    md_links = extract_markdown_links(content)
    for line_num, link_text, target_path in md_links:
        # 跳过忽略的模式
        if any(pattern in target_path for pattern in ignore_patterns):
            continue

        # 跳过外部 URL 和纯锚点
        if should_ignore_url(target_path) or is_anchor_only(target_path):
            continue

        checked_count += 1
        resolved = resolve_link_path(file_path, target_path, project_root)

        if not resolved.exists():
            broken_links.append(BrokenLink(
                source_file=str(relative_source),
                line_number=line_num,
                link_type="markdown",
                target_path=target_path,
                resolved_path=str(resolved.relative_to(project_root) if resolved.is_relative_to(project_root) else resolved),
                reason="文件不存在",
                source_type=source_type
            ))

    # 检查纯文本路径引用
    text_refs = extract_text_references(content)
    for line_num, target_path in text_refs:
        # 跳过忽略的模式
        if any(pattern in target_path for pattern in ignore_patterns):
            continue

        checked_count += 1

        # 纯文本引用通常是相对于项目根目录
        resolved = project_root / target_path

        # 也尝试相对于源文件目录
        if not resolved.exists():
            alt_resolved = (file_path.parent / target_path).resolve()
            if alt_resolved.exists():
                resolved = alt_resolved

        if not resolved.exists():
            broken_links.append(BrokenLink(
                source_file=str(relative_source),
                line_number=line_num,
                link_type="text_reference",
                target_path=target_path,
                resolved_path=str(resolved.relative_to(project_root) if resolved.is_relative_to(project_root) else resolved),
                reason="文件不存在",
                source_type=source_type
            ))

    return checked_count, broken_links


def scan_directory(
    scan_dir: Path,
    project_root: Path,
    ignore_patterns: Set[str],
    source_type: Optional[str] = None
) -> tuple:
    """
    扫描目录中的所有 Markdown 文件

    Args:
        scan_dir: 扫描目录
        project_root: 项目根目录
        ignore_patterns: 忽略的模式集合
        source_type: 来源类型，如果为 None 则自动检测每个文件

    Returns:
        (files_count, total_checked, all_broken_links, entrypoint_count, docs_count)
    """
    files_count = 0
    total_checked = 0
    all_broken = []
    entrypoint_count = 0
    docs_count = 0

    if not scan_dir.exists():
        print(f"警告: 目录不存在 {scan_dir}", file=sys.stderr)
        return 0, 0, [], 0, 0

    for md_file in scan_dir.rglob("*.md"):
        # 检查是否在排除目录中
        try:
            rel_path = md_file.relative_to(project_root)
            skip = False
            for excluded in EXCLUDED_DIRS:
                if str(rel_path).startswith(excluded + "/") or str(rel_path).startswith(excluded + "\\"):
                    skip = True
                    break
            if skip:
                continue
        except ValueError:
            pass

        files_count += 1

        # 确定来源类型
        file_source_type = source_type
        if file_source_type is None:
            file_source_type = "entrypoint" if is_entrypoint_file(md_file, project_root) else "docs"

        if file_source_type == "entrypoint":
            entrypoint_count += 1
        else:
            docs_count += 1

        checked, broken = check_file_links(md_file, project_root, ignore_patterns, file_source_type)
        total_checked += checked
        all_broken.extend(broken)

    return files_count, total_checked, all_broken, entrypoint_count, docs_count


def scan_file(
    file_path: Path,
    project_root: Path,
    ignore_patterns: Set[str],
    source_type: Optional[str] = None
) -> tuple:
    """
    扫描单个文件

    Args:
        file_path: 文件路径
        project_root: 项目根目录
        ignore_patterns: 忽略的模式集合
        source_type: 来源类型，如果为 None 则自动检测

    Returns:
        (checked_count, broken_links, detected_source_type)
    """
    if not file_path.exists():
        print(f"警告: 文件不存在 {file_path}", file=sys.stderr)
        return 0, [], None

    if not file_path.suffix.lower() == ".md":
        print(f"警告: 非 Markdown 文件 {file_path}", file=sys.stderr)
        return 0, [], None

    # 确定来源类型
    if source_type is None:
        source_type = "entrypoint" if is_entrypoint_file(file_path, project_root) else "docs"

    checked, broken = check_file_links(file_path, project_root, ignore_patterns, source_type)
    return checked, broken, source_type


def discover_apps_docs_dirs(project_root: Path) -> List[str]:
    """
    自动发现 apps/*/docs/ 目录

    Returns:
        apps 下存在的 docs 目录相对路径列表
    """
    dirs = []
    full_pattern = str(project_root / APPS_DOCS_GLOB_PATTERN)
    matches = glob_module.glob(full_pattern)
    for match in matches:
        path = Path(match)
        if path.is_dir():
            try:
                rel_path = str(path.relative_to(project_root))
                dirs.append(rel_path)
            except ValueError:
                pass
    return sorted(dirs)


def discover_entrypoint_files(project_root: Path) -> List[Path]:
    """
    自动发现入口文件（README.md 系列）

    使用 ENTRYPOINT_GLOB_PATTERNS 中定义的 glob 模式

    Returns:
        入口文件路径列表
    """
    files = []
    for pattern in ENTRYPOINT_GLOB_PATTERNS:
        full_pattern = str(project_root / pattern)
        matches = glob_module.glob(full_pattern)
        for match in matches:
            path = Path(match)
            if path.is_file() and path.suffix.lower() == ".md":
                files.append(path)
    return sorted(set(files))


def main():
    parser = argparse.ArgumentParser(
        description="检查文档中的本地链接有效性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python check_links.py                          # 默认扫描 docs/ + apps/*/docs/
  python check_links.py apps/logbook_postgres/docs
  python check_links.py README.md                # 扫描单个文件
  python check_links.py --entrypoints            # 仅扫描入口文件（README.md 系列）
  python check_links.py --output ./artifacts
  python check_links.py --ignore-patterns "example" "template"

来源类型：
  entrypoint - 入口文件（README.md, docs/README.md, docs/*/README.md,
               apps/*/README.md, apps/*/docs/README.md）
  docs       - 文档目录中的其他 Markdown 文件

默认扫描范围：
  - docs/ 下的子目录（排除 docs/legacy/）
  - docs/README.md
  - apps/*/docs/（自动发现）
        """
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="要扫描的目录或文件列表（相对于项目根目录）"
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录（默认: {DEFAULT_OUTPUT_DIR}）"
    )
    parser.add_argument(
        "--ignore-patterns", "-i",
        nargs="*",
        default=[],
        help="要忽略的路径模式列表"
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
        "--entrypoints", "-e",
        action="store_true",
        help="仅扫描入口文件（README.md 系列），自动发现 README.md 和 apps/*/README.md"
    )
    parser.add_argument(
        "--include-entrypoints",
        action="store_true",
        help="在扫描目录时也包含默认入口文件"
    )

    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    # 合并命令行忽略模式和内置忽略模式
    ignore_patterns = set(args.ignore_patterns) | set(BUILTIN_IGNORE_PATTERNS)

    # 确定扫描目标
    scan_dirs: List[str] = []
    scan_files: List[str] = []

    if args.entrypoints:
        # 仅扫描入口文件
        entrypoint_paths = discover_entrypoint_files(project_root)
        scan_files = [str(p.relative_to(project_root)) for p in entrypoint_paths]
        print("模式: 仅扫描入口文件（entrypoints）")
    elif args.paths:
        # 区分目录和文件
        for path in args.paths:
            full_path = project_root / path
            if full_path.is_dir():
                scan_dirs.append(path)
            elif full_path.is_file():
                scan_files.append(path)
            else:
                print(f"警告: 路径不存在 {path}", file=sys.stderr)
    else:
        # 默认扫描 docs 目录 + 自动发现的 apps/*/docs/
        scan_dirs = DEFAULT_SCAN_DIRS.copy()

        # 自动发现 apps/*/docs/ 目录
        apps_docs_dirs = discover_apps_docs_dirs(project_root)
        for apps_dir in apps_docs_dirs:
            if apps_dir not in scan_dirs:
                scan_dirs.append(apps_dir)

        # 添加默认扫描文件
        scan_files = DEFAULT_SCAN_FILES.copy()

    # 如果指定了 --include-entrypoints，添加默认入口文件
    if args.include_entrypoints and not args.entrypoints:
        entrypoint_paths = discover_entrypoint_files(project_root)
        for ep in entrypoint_paths:
            rel_path = str(ep.relative_to(project_root))
            if rel_path not in scan_files:
                scan_files.append(rel_path)

    print(f"项目根目录: {project_root}")
    if scan_dirs:
        print(f"扫描目录: {scan_dirs}")
    if scan_files:
        print(f"扫描文件: {scan_files}")
    if args.verbose and ignore_patterns:
        print(f"忽略模式: {ignore_patterns}")
    print()

    # 初始化报告
    report = LinkReport(
        scan_dirs=scan_dirs,
        scan_files=scan_files,
        ignored_patterns=list(ignore_patterns)
    )

    # 扫描所有目录
    for rel_dir in scan_dirs:
        scan_path = project_root / rel_dir
        print(f"扫描目录: {rel_dir}...")

        files_count, checked_count, broken, ep_count, docs_count = scan_directory(
            scan_path, project_root, ignore_patterns
        )

        report.files_scanned += files_count
        report.total_links_checked += checked_count
        report.broken_links.extend(broken)
        report.entrypoint_files_scanned += ep_count
        report.docs_files_scanned += docs_count

        if args.verbose:
            print(f"  - 文件数: {files_count} (entrypoint: {ep_count}, docs: {docs_count})")
            print(f"  - 链接数: {checked_count}")
            print(f"  - 失效数: {len(broken)}")

    # 扫描所有单独的文件
    for rel_file in scan_files:
        file_path = project_root / rel_file
        print(f"扫描文件: {rel_file}...")

        checked_count, broken, source_type = scan_file(
            file_path, project_root, ignore_patterns
        )

        if source_type is not None:
            report.files_scanned += 1
            report.total_links_checked += checked_count
            report.broken_links.extend(broken)

            if source_type == "entrypoint":
                report.entrypoint_files_scanned += 1
            else:
                report.docs_files_scanned += 1

            if args.verbose:
                print(f"  - 类型: {source_type}")
                print(f"  - 链接数: {checked_count}")
                print(f"  - 失效数: {len(broken)}")

    # 统计各类型的失效链接数
    report.entrypoint_broken_count = sum(1 for link in report.broken_links if link.source_type == "entrypoint")
    report.docs_broken_count = sum(1 for link in report.broken_links if link.source_type == "docs")

    # 确保输出目录存在
    output_dir = project_root / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    # 写入报告
    report_path = output_dir / "docs_link_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)

    # 输出摘要
    print()
    print("=" * 60)
    print("链接检查报告摘要")
    print("=" * 60)
    print(f"扫描文件数: {report.files_scanned}")
    print(f"  - entrypoint (入口文件): {report.entrypoint_files_scanned}")
    print(f"  - docs (文档文件): {report.docs_files_scanned}")
    print(f"检查链接数: {report.total_links_checked}")
    print(f"失效链接数: {len(report.broken_links)}")
    print(f"  - entrypoint: {report.entrypoint_broken_count}")
    print(f"  - docs: {report.docs_broken_count}")
    print(f"报告路径: {report_path}")

    if report.broken_links:
        print()
        print("失效链接列表:")
        print("-" * 60)

        # 按来源类型分组显示
        entrypoint_broken = [link for link in report.broken_links if link.source_type == "entrypoint"]
        docs_broken = [link for link in report.broken_links if link.source_type == "docs"]

        if entrypoint_broken:
            print()
            print("[ENTRYPOINT] 入口文件中的失效链接:")
            for link in entrypoint_broken:
                print(f"  [{link.link_type}] {link.source_file}:{link.line_number}")
                print(f"    目标: {link.target_path}")
                print(f"    原因: {link.reason}")

        if docs_broken:
            print()
            print("[DOCS] 文档文件中的失效链接:")
            for link in docs_broken:
                print(f"  [{link.link_type}] {link.source_file}:{link.line_number}")
                print(f"    目标: {link.target_path}")
                print(f"    原因: {link.reason}")

        # 返回非零退出码表示有失效链接
        sys.exit(1)
    else:
        print()
        print("所有链接均有效！")
        sys.exit(0)


if __name__ == "__main__":
    main()
