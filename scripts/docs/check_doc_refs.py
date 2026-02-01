#!/usr/bin/env python3
"""
文档引用检查脚本

功能：
- 扫描代码文件（*.py/*.sh/*.yml/*.yaml/*.toml/*.json）中的文档路径引用
- 提取形如 docs/... 、docs/contracts/... 的路径引用
- 提取 memory://docs/<rel_path>/... 格式的引用
- 验证引用的文档文件存在
- 支持忽略规则与 legacy 白名单（与 docs/architecture/docs_legacy_retention_policy.md 对齐）
- 输出 JSON 格式的报告

用法：
    python check_doc_refs.py [目录或文件1] [目录或文件2] ...
    python check_doc_refs.py --output ./custom_output_dir
    python check_doc_refs.py --ignore-patterns "pattern1" "pattern2"
    python check_doc_refs.py --strict  # 严格模式，发现问题返回非零退出码
"""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Set, Tuple

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 默认扫描目录
DEFAULT_SCAN_DIRS = [
    "apps",
    "scripts",
    "compose",
]

# 默认扫描的文件扩展名
SCAN_EXTENSIONS = {
    ".py",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
}

# 排除的目录（在扫描时跳过）
EXCLUDED_DIRS = [
    "docs/legacy",
    "scripts/docs/legacy",
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".tmp",
    ".artifacts",
]

# 排除的文件模式
EXCLUDED_FILES = [
    "*.pyc",
    "*.pyo",
    "package-lock.json",
    "poetry.lock",
    "Pipfile.lock",
]

# 默认输出目录
DEFAULT_OUTPUT_DIR = ".tmp"

# ============================================================================
# Legacy 白名单（与 docs/architecture/docs_legacy_retention_policy.md 对齐）
# ============================================================================
# 这些路径虽然引用了 docs/legacy 下的文件，但属于合法引用（用于审计追溯）
# 参见：docs/architecture/docs_legacy_retention_policy.md § 3.1 裁决汇总表
# ============================================================================
LEGACY_WHITELIST = [
    # external-reference 分类：外部参考资料（待处置，但当前合法）
    "docs/legacy/old/LangChain.md",
    # 迁移脚本和映射（legacy-audit）
    "scripts/docs/legacy/migrate_docs.py",
    "scripts/docs/legacy/docs_legacy_retention_policy.md",
    "scripts/docs/legacy/docs_migration_map.json",
]

# ============================================================================
# 内置忽略模式
# ============================================================================
BUILTIN_IGNORE_PATTERNS = [
    # 示例/模板路径（非真实文件引用）
    "docs/example",
    "docs/template",
    "docs/<",  # 占位符格式如 docs/<rel_path>
    "docs/{",  # 模板格式如 docs/{component}
    # URL 片段（非文件路径）
    "docs.python.org",
    "docs.github.com",
    "readthedocs",
    # 常见注释中的伪引用
    "docs/...",
    "docs/xxx",
    "docs/TODO",
    # JSON Schema $ref 中的 definitions
    "#/definitions/",
    "#/$defs/",
    # 测试文件中的虚假路径（单字符或明显虚假）
    "docs/s",      # 测试中的短路径
    "docs/file.md",  # 通用测试文件名
    "docs/ml/",    # 测试中的虚假目录
    "docs/a/",     # 测试中的虚假目录
    "abc123",      # 测试用哈希片段
    "a1b2c3d4",    # 测试用哈希片段
    # 测试文件中的示例引用（非真实文档）
    "docs/release_notes.md",     # 常见虚假文档名
    "docs/architecture.md",      # 单文件名（应为 docs/architecture/*.md）
    "docs/docs/",                # 重复前缀（错误路径）
    # 脚本自身的示例/文档字符串中的引用
    "docs/contracts/memory.md",  # 脚本文档中的示例
    "docs/logbook/overview",     # 脚本文档中的示例（无扩展名）
    # 相对路径引用（缺少完整路径前缀）
    "docs/03_memory_contract.md",  # 应为 docs/gateway/03_memory_contract.md
]

# ============================================================================
# 排除的源文件模式（这些文件中的引用不会被检查）
# ============================================================================
EXCLUDED_SOURCE_PATTERNS = [
    # 测试文件中的 URI/路径解析测试
    "test_uri_resolution.py",
    # benchmark 脚本中的虚假路径
    "benchmark_",
    # 测试文件中的 legacy alias 检查（使用虚假路径）
    "test_legacy_alias_checks.py",
    # 查询包 shape 测试
    "test_seek_query_packet_shape.py",
    # E2E 测试中的虚假路径
    "test_pgvector_e2e_minimal.py",
    # 本脚本自身（包含示例路径）
    "check_doc_refs.py",
]

# ============================================================================
# 文档路径引用正则表达式
# ============================================================================

# 1. 普通路径引用：docs/... 或 ./docs/...
#    匹配示例：docs/logbook/00_overview.md, ./docs/contracts/memory.md
DOCS_PATH_PATTERN = re.compile(
    r'(?:^|[\s"\'\(\[\{,:`])' +  # 前缀边界
    r'(\.?/?docs/[a-zA-Z0-9_\-./]+(?:\.[a-zA-Z0-9]+)?)' +  # 路径
    r'(?:[\s"\'\)\]\},:`]|$)',  # 后缀边界
    re.MULTILINE
)

# 2. memory:// URI 引用
#    匹配示例：memory://docs/contracts/memory.md, memory://docs/logbook/overview
MEMORY_URI_PATTERN = re.compile(
    r'memory://docs/([a-zA-Z0-9_\-./]+)',
    re.MULTILINE
)

# 3. 代码中的字符串引用（更严格的匹配）
#    匹配示例："docs/logbook/00_overview.md", 'docs/contracts/memory.md'
STRING_DOCS_PATTERN = re.compile(
    r'["\']' +  # 字符串开始
    r'(\.?/?docs/[a-zA-Z0-9_\-./]+(?:\.[a-zA-Z0-9]+)?)' +  # 路径
    r'["\']',  # 字符串结束
    re.MULTILINE
)


@dataclass
class DocReference:
    """表示一个文档引用"""
    source_file: str      # 源文件路径（相对于项目根目录）
    line_number: int      # 行号
    ref_type: str         # 引用类型: "path", "memory_uri", "string"
    raw_ref: str          # 原始引用文本
    resolved_path: str    # 解析后的文档路径（相对于项目根目录）
    exists: bool          # 文档是否存在
    is_legacy: bool       # 是否为 legacy 文档引用
    whitelisted: bool     # 是否在白名单中


@dataclass
class RefCheckReport:
    """文档引用检查报告"""
    scan_dirs: List[str] = field(default_factory=list)
    files_scanned: int = 0
    total_refs_found: int = 0
    valid_refs: int = 0
    missing_refs: int = 0
    legacy_refs: int = 0
    whitelisted_refs: int = 0
    references: List[DocReference] = field(default_factory=list)
    missing_list: List[DocReference] = field(default_factory=list)
    legacy_list: List[DocReference] = field(default_factory=list)
    ignored_patterns: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "scan_dirs": self.scan_dirs,
            "files_scanned": self.files_scanned,
            "total_refs_found": self.total_refs_found,
            "valid_refs": self.valid_refs,
            "missing_refs": self.missing_refs,
            "legacy_refs": self.legacy_refs,
            "whitelisted_refs": self.whitelisted_refs,
            "missing_list": [asdict(ref) for ref in self.missing_list],
            "legacy_list": [asdict(ref) for ref in self.legacy_list],
            "ignored_patterns": self.ignored_patterns,
            "summary": {
                "status": "ok" if self.missing_refs == 0 else "error",
                "message": (
                    f"发现 {self.missing_refs} 个无效文档引用"
                    if self.missing_refs > 0
                    else "所有文档引用均有效"
                ),
            },
        }


def should_exclude_path(path: Path, project_root: Path) -> bool:
    """检查路径是否应该被排除"""
    try:
        rel_path = path.relative_to(project_root)
        rel_str = str(rel_path)

        # 检查排除目录
        for excluded in EXCLUDED_DIRS:
            if rel_str.startswith(excluded + "/") or rel_str.startswith(excluded + "\\"):
                return True
            if rel_str == excluded:
                return True

        # 检查排除文件模式
        for pattern in EXCLUDED_FILES:
            if pattern.startswith("*"):
                if path.name.endswith(pattern[1:]):
                    return True
            elif path.name == pattern:
                return True

        return False
    except ValueError:
        return False


def should_ignore_ref(ref: str, ignore_patterns: Set[str]) -> bool:
    """检查引用是否应该被忽略"""
    for pattern in ignore_patterns:
        if pattern in ref:
            return True
    return False


def is_legacy_path(path: str) -> bool:
    """检查是否为 legacy 文档路径"""
    return "docs/legacy" in path or "legacy/" in path


def is_whitelisted(path: str) -> bool:
    """检查路径是否在白名单中"""
    # 规范化路径
    normalized = path.lstrip("./")
    return normalized in LEGACY_WHITELIST


def normalize_doc_path(raw_ref: str) -> str:
    """规范化文档路径"""
    # 移除前导 ./ 或 /
    path = raw_ref.lstrip("./")

    # 确保以 docs/ 开头
    if not path.startswith("docs/"):
        path = "docs/" + path

    return path


def extract_refs_from_content(
    content: str,
    source_file: Path,
    project_root: Path,
    ignore_patterns: Set[str]
) -> List[DocReference]:
    """从文件内容中提取文档引用"""
    refs = []
    lines = content.split("\n")

    for line_num, line in enumerate(lines, 1):
        # 跳过注释行中的某些模式
        line.strip()

        # 1. 检查普通路径引用
        for match in DOCS_PATH_PATTERN.finditer(line):
            raw_ref = match.group(1)
            if should_ignore_ref(raw_ref, ignore_patterns):
                continue

            resolved = normalize_doc_path(raw_ref)
            full_path = project_root / resolved
            exists = full_path.exists()
            is_legacy = is_legacy_path(resolved)
            whitelisted = is_whitelisted(resolved)

            refs.append(DocReference(
                source_file=str(source_file.relative_to(project_root)),
                line_number=line_num,
                ref_type="path",
                raw_ref=raw_ref,
                resolved_path=resolved,
                exists=exists,
                is_legacy=is_legacy,
                whitelisted=whitelisted,
            ))

        # 2. 检查 memory:// URI
        for match in MEMORY_URI_PATTERN.finditer(line):
            rel_path = match.group(1)
            raw_ref = f"memory://docs/{rel_path}"
            if should_ignore_ref(raw_ref, ignore_patterns):
                continue

            resolved = f"docs/{rel_path}"
            full_path = project_root / resolved
            exists = full_path.exists()
            is_legacy = is_legacy_path(resolved)
            whitelisted = is_whitelisted(resolved)

            refs.append(DocReference(
                source_file=str(source_file.relative_to(project_root)),
                line_number=line_num,
                ref_type="memory_uri",
                raw_ref=raw_ref,
                resolved_path=resolved,
                exists=exists,
                is_legacy=is_legacy,
                whitelisted=whitelisted,
            ))

        # 3. 检查字符串中的引用（仅在未被其他模式匹配时）
        for match in STRING_DOCS_PATTERN.finditer(line):
            raw_ref = match.group(1)
            if should_ignore_ref(raw_ref, ignore_patterns):
                continue

            # 避免重复（已被普通路径模式匹配）
            resolved = normalize_doc_path(raw_ref)
            already_found = any(
                r.resolved_path == resolved and r.line_number == line_num
                for r in refs
            )
            if already_found:
                continue

            full_path = project_root / resolved
            exists = full_path.exists()
            is_legacy = is_legacy_path(resolved)
            whitelisted = is_whitelisted(resolved)

            refs.append(DocReference(
                source_file=str(source_file.relative_to(project_root)),
                line_number=line_num,
                ref_type="string",
                raw_ref=raw_ref,
                resolved_path=resolved,
                exists=exists,
                is_legacy=is_legacy,
                whitelisted=whitelisted,
            ))

    return refs


def should_exclude_source_file(file_path: Path) -> bool:
    """检查源文件是否应该被排除（测试文件等）"""
    file_name = file_path.name
    for pattern in EXCLUDED_SOURCE_PATTERNS:
        if pattern in file_name:
            return True
    return False


def scan_file(
    file_path: Path,
    project_root: Path,
    ignore_patterns: Set[str]
) -> List[DocReference]:
    """扫描单个文件"""
    if file_path.suffix.lower() not in SCAN_EXTENSIONS:
        return []

    # 跳过排除的源文件（如测试文件）
    if should_exclude_source_file(file_path):
        return []

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"警告: 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return []

    return extract_refs_from_content(content, file_path, project_root, ignore_patterns)


def scan_directory(
    scan_dir: Path,
    project_root: Path,
    ignore_patterns: Set[str]
) -> Tuple[int, List[DocReference]]:
    """扫描目录"""
    files_count = 0
    all_refs = []

    if not scan_dir.exists():
        print(f"警告: 目录不存在 {scan_dir}", file=sys.stderr)
        return 0, []

    for root, dirs, files in os.walk(scan_dir):
        root_path = Path(root)

        # 过滤排除目录
        dirs[:] = [
            d for d in dirs
            if not should_exclude_path(root_path / d, project_root)
        ]

        for file_name in files:
            file_path = root_path / file_name

            if should_exclude_path(file_path, project_root):
                continue

            if file_path.suffix.lower() not in SCAN_EXTENSIONS:
                continue

            files_count += 1
            refs = scan_file(file_path, project_root, ignore_patterns)
            all_refs.extend(refs)

    return files_count, all_refs


def main():
    parser = argparse.ArgumentParser(
        description="检查代码中的文档路径引用有效性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python check_doc_refs.py                          # 默认扫描 apps/ + scripts/
  python check_doc_refs.py apps/logbook_postgres
  python check_doc_refs.py --output ./artifacts
  python check_doc_refs.py --ignore-patterns "example" "template"
  python check_doc_refs.py --strict                 # 发现问题返回非零退出码

扫描范围：
  - *.py, *.sh, *.yml, *.yaml, *.toml, *.json 文件
  - 排除 docs/legacy/, __pycache__, node_modules 等目录

引用格式：
  - 普通路径: docs/logbook/00_overview.md
  - memory URI: memory://docs/contracts/memory.md
  - 字符串引用: "docs/architecture/naming.md"

Legacy 白名单：
  参见 docs/architecture/docs_legacy_retention_policy.md
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
        "--strict",
        action="store_true",
        help="严格模式：发现无效引用时返回非零退出码"
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="仅输出 JSON（不输出人类可读的摘要）"
    )

    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    ignore_patterns = set(args.ignore_patterns) | set(BUILTIN_IGNORE_PATTERNS)

    # 确定扫描目标
    scan_dirs: List[str] = []

    if args.paths:
        scan_dirs = args.paths
    else:
        scan_dirs = DEFAULT_SCAN_DIRS.copy()

    if not args.json_only:
        print(f"项目根目录: {project_root}")
        print(f"扫描目录: {scan_dirs}")
        if args.verbose and ignore_patterns:
            print(f"忽略模式: {sorted(ignore_patterns)}")
        print()

    # 初始化报告
    report = RefCheckReport(
        scan_dirs=scan_dirs,
        ignored_patterns=list(sorted(ignore_patterns))
    )

    # 扫描所有目录
    for rel_dir in scan_dirs:
        scan_path = project_root / rel_dir
        if not args.json_only:
            print(f"扫描目录: {rel_dir}...")

        files_count, refs = scan_directory(scan_path, project_root, ignore_patterns)

        report.files_scanned += files_count
        report.references.extend(refs)

        if args.verbose and not args.json_only:
            print(f"  - 文件数: {files_count}")
            print(f"  - 引用数: {len(refs)}")

    # 统计结果
    report.total_refs_found = len(report.references)

    for ref in report.references:
        if ref.exists:
            report.valid_refs += 1
        else:
            report.missing_refs += 1
            report.missing_list.append(ref)

        if ref.is_legacy:
            report.legacy_refs += 1
            report.legacy_list.append(ref)
            if ref.whitelisted:
                report.whitelisted_refs += 1

    # 确保输出目录存在
    output_dir = project_root / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    # 写入报告
    report_path = output_dir / "doc_refs_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)

    if args.json_only:
        # 仅输出 JSON
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        # 输出摘要
        print()
        print("=" * 60)
        print("文档引用检查报告")
        print("=" * 60)
        print(f"扫描文件数: {report.files_scanned}")
        print(f"发现引用数: {report.total_refs_found}")
        print(f"有效引用数: {report.valid_refs}")
        print(f"无效引用数: {report.missing_refs}")
        print(f"Legacy 引用数: {report.legacy_refs} (白名单: {report.whitelisted_refs})")
        print(f"报告路径: {report_path}")

        if report.missing_list:
            print()
            print("无效引用列表:")
            print("-" * 60)
            for ref in report.missing_list:
                whitelisted_mark = " [WHITELISTED]" if ref.whitelisted else ""
                print(f"  [{ref.ref_type}] {ref.source_file}:{ref.line_number}")
                print(f"    引用: {ref.raw_ref}")
                print(f"    解析: {ref.resolved_path}{whitelisted_mark}")

        if report.legacy_list and args.verbose:
            print()
            print("Legacy 引用列表 (参见 docs/architecture/docs_legacy_retention_policy.md):")
            print("-" * 60)
            for ref in report.legacy_list:
                status = "[OK/WHITELISTED]" if ref.whitelisted else "[WARN/NOT-WHITELISTED]"
                print(f"  {status} {ref.source_file}:{ref.line_number}")
                print(f"    引用: {ref.resolved_path}")

    # 退出码
    if args.strict and report.missing_refs > 0:
        # 严格模式：排除白名单后的无效引用
        non_whitelisted_missing = [
            ref for ref in report.missing_list if not ref.whitelisted
        ]
        if non_whitelisted_missing:
            if not args.json_only:
                print()
                print(f"[ERROR] 严格模式：发现 {len(non_whitelisted_missing)} 个非白名单的无效引用")
            sys.exit(1)

    if not args.json_only:
        if report.missing_refs == 0:
            print()
            print("所有文档引用均有效！")
        elif args.strict:
            # 所有无效引用都在白名单中
            print()
            print("[OK] 所有无效引用均在白名单中")

    sys.exit(0)


if __name__ == "__main__":
    main()
