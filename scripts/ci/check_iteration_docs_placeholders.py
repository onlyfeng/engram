#!/usr/bin/env python3
"""
检查迭代文档中的模板占位符和使用说明区块

功能:
1. 扫描 docs/acceptance/iteration_*_{plan,regression}.md 文件（排除 _templates/）
2. 检测未替换的模板变量:
   - {PLACEHOLDER} 格式的占位符
   - {N}, {M}, {K}, {L}, {T} 等单字母变量
   - {N-1}, {N+1} 等表达式变量
   - {YYYY-MM-DD}, {STATUS}, {STATUS_EMOJI} 等常见模板变量
3. 检测文件顶部的模板"使用说明"区块（如 `> **使用说明**`）
4. 检测 regression 文件的标准标题结构（如 `## 执行信息`、`## 最小门禁命令块`）
5. 输出违规列表和修复建议

用法:
    # 检查 docs/acceptance/ 目录
    python scripts/ci/check_iteration_docs_placeholders.py

    # 详细输出
    python scripts/ci/check_iteration_docs_placeholders.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_iteration_docs_placeholders.py --stats-only

    # 仅警告模式（标准标题检查不阻断）
    python scripts/ci/check_iteration_docs_placeholders.py --warn-only

退出码:
    0 - 检查通过或 --stats-only 模式
    1 - 检查失败（存在违规）
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class PlaceholderViolation:
    """模板占位符违规记录。"""

    file: Path
    line_number: int
    line_content: str
    violation_type: str  # "placeholder", "usage_instruction", 或 "missing_heading"
    matched_text: str

    def __str__(self) -> str:
        if self.violation_type == "placeholder":
            return f"{self.file}:{self.line_number}: 模板占位符未替换: {self.matched_text}"
        elif self.violation_type == "usage_instruction":
            return f"{self.file}:{self.line_number}: 模板使用说明未移除: {self.matched_text}"
        elif self.violation_type == "missing_heading":
            return f"{self.file}:{self.line_number}: 缺少标准标题: {self.matched_text}"
        elif self.violation_type == "missing_evidence_link":
            return f"{self.file}:{self.line_number}: 缺少证据文件链接: {self.matched_text}"
        else:  # mismatched_evidence_link
            return f"{self.file}:{self.line_number}: 证据链接编号不匹配: {self.matched_text}"


# ============================================================================
# 正则表达式
# ============================================================================

# 模板占位符模式（大括号包裹的变量）
# 匹配:
#   - {PLACEHOLDER} - 大写占位符
#   - {N}, {M}, {K} 等 - 单字母变量
#   - {N-1}, {N+1}, {N-M} 等 - 表达式变量
#   - {YYYY-MM-DD}, {STATUS}, {STATUS_EMOJI} 等 - 常见模板变量
#   - {目标1名称}, {修复方案A} 等 - 中文占位符
PLACEHOLDER_PATTERN = re.compile(
    r"\{("
    r"PLACEHOLDER"  # 通用占位符
    r"|[A-Z]"  # 单字母大写变量: {N}, {M}, {K}, {L}, {T}
    r"|[A-Z][+-][0-9]+"  # 表达式变量: {N-1}, {N+1}
    r"|[A-Z][+-][A-Z]"  # 字母表达式: {N-M}
    r"|YYYY-MM-DD"  # 日期占位符
    r"|STATUS(?:_EMOJI)?"  # 状态占位符
    r"|[A-Z_]{2,}"  # 多字母大写变量: {PR}, {OS}
    r"|[^\}]{0,20}[名称说明描述内容建议方案路径命令标准原因]"  # 中文占位符
    r")\}"
)

# 模板使用说明区块模式
# 匹配文件顶部的使用说明（通常在前 20 行内）
USAGE_INSTRUCTION_PATTERNS = [
    re.compile(r">\s*\*\*使用说明\*\*", re.IGNORECASE),
    re.compile(r">\s*\*\*Usage\s+Instructions?\*\*", re.IGNORECASE),
    re.compile(r"^>\s*[^>]*复制本模板到", re.MULTILINE),
    re.compile(r"^>\s*[^>]*替换.*占位符", re.MULTILINE),
]

# 代码块边界模式
CODE_BLOCK_PATTERN = re.compile(r"^(`{3}|~{3})")

# Regression 文件标准标题（必须存在）
# 顺序表示推荐的结构顺序
REGRESSION_REQUIRED_HEADINGS = [
    "## 执行信息",
    "## 最小门禁命令块",
    "## 验收证据",
]

# H2 标题模式
H2_HEADING_PATTERN = re.compile(r"^##\s+(.+)$")

# 从文件名提取迭代编号的模式
ITERATION_NUMBER_PATTERN = re.compile(r"iteration_(\d+)_regression\.md$")

# Evidence 链接模式（匹配 evidence/iteration_<N>_evidence.json）
EVIDENCE_LINK_PATTERN = re.compile(r"evidence/iteration_(\d+)_evidence\.json")


# ============================================================================
# 配置
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def get_iteration_files(project_root: Path) -> List[Path]:
    """
    获取需要检查的迭代文档文件。

    扫描 docs/acceptance/iteration_*_{plan,regression}.md，
    排除 _templates/ 目录下的模板文件。
    """
    acceptance_dir = project_root / "docs" / "acceptance"
    if not acceptance_dir.exists():
        return []

    files: List[Path] = []

    # 扫描 plan 和 regression 文件
    for pattern in ["iteration_*_plan.md", "iteration_*_regression.md"]:
        for filepath in acceptance_dir.glob(pattern):
            # 排除 _templates/ 目录
            if "_templates" not in filepath.parts:
                files.append(filepath)

    return sorted(files)


# ============================================================================
# 扫描逻辑
# ============================================================================


def scan_file_for_placeholders(file_path: Path) -> Iterator[PlaceholderViolation]:
    """
    扫描单个文件中的模板占位符。

    跳过 Markdown 代码块（```...```）内的内容，因为代码块中可能包含
    示例占位符用于说明。

    Args:
        file_path: 要扫描的文件路径

    Yields:
        PlaceholderViolation 对象
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    in_code_block = False
    lines = content.splitlines()

    for line_number, line in enumerate(lines, start=1):
        # 检测代码块边界
        stripped = line.strip()
        if CODE_BLOCK_PATTERN.match(stripped):
            in_code_block = not in_code_block
            continue

        # 跳过代码块内的内容
        if in_code_block:
            continue

        # 检测模板占位符
        for match in PLACEHOLDER_PATTERN.finditer(line):
            yield PlaceholderViolation(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                violation_type="placeholder",
                matched_text=match.group(0),
            )


def scan_file_for_usage_instructions(
    file_path: Path,
    check_lines: int = 20,
) -> Iterator[PlaceholderViolation]:
    """
    扫描文件顶部的模板使用说明区块。

    Args:
        file_path: 要扫描的文件路径
        check_lines: 检查的行数（默认前 20 行）

    Yields:
        PlaceholderViolation 对象
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    lines = content.splitlines()[:check_lines]

    for line_number, line in enumerate(lines, start=1):
        for pattern in USAGE_INSTRUCTION_PATTERNS:
            match = pattern.search(line)
            if match:
                yield PlaceholderViolation(
                    file=file_path,
                    line_number=line_number,
                    line_content=line.strip(),
                    violation_type="usage_instruction",
                    matched_text=match.group(0),
                )
                # 每行只报告一次使用说明违规
                break


def scan_file_for_required_headings(
    file_path: Path,
    required_headings: Optional[List[str]] = None,
) -> Iterator[PlaceholderViolation]:
    """
    扫描文件是否包含必需的标准标题。

    仅对 regression 文件执行此检查。

    Args:
        file_path: 要扫描的文件路径
        required_headings: 必需的标题列表（默认使用 REGRESSION_REQUIRED_HEADINGS）

    Yields:
        PlaceholderViolation 对象（缺少的标题）
    """
    # 仅对 regression 文件检查
    if "_regression.md" not in file_path.name:
        return

    if required_headings is None:
        required_headings = REGRESSION_REQUIRED_HEADINGS

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    lines = content.splitlines()

    # 收集文件中所有的 H2 标题
    found_headings: set[str] = set()
    for line in lines:
        stripped = line.strip()
        match = H2_HEADING_PATTERN.match(stripped)
        if match:
            # 保存完整的标题行（## + 标题内容）
            found_headings.add(stripped)

    # 检查必需标题是否存在
    for heading in required_headings:
        if heading not in found_headings:
            yield PlaceholderViolation(
                file=file_path,
                line_number=0,  # 0 表示整个文件层面的问题
                line_content="",
                violation_type="missing_heading",
                matched_text=heading,
            )


def scan_file_for_evidence_link(file_path: Path) -> Iterator[PlaceholderViolation]:
    """
    扫描 regression 文件是否包含正确的 evidence 链接。

    检查规则：
    1. regression 文件必须包含 evidence/iteration_<N>_evidence.json 的链接
    2. 链接中的迭代编号 <N> 必须与文件名中的迭代编号匹配

    仅对 regression 文件执行此检查。

    Args:
        file_path: 要扫描的文件路径

    Yields:
        PlaceholderViolation 对象
    """
    # 仅对 regression 文件检查
    if "_regression.md" not in file_path.name:
        return

    # 从文件名提取迭代编号
    filename_match = ITERATION_NUMBER_PATTERN.search(file_path.name)
    if not filename_match:
        return  # 文件名格式不匹配，跳过

    expected_iteration_number = filename_match.group(1)

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    # 查找所有 evidence 链接
    evidence_links = EVIDENCE_LINK_PATTERN.findall(content)

    if not evidence_links:
        # 缺少 evidence 链接
        yield PlaceholderViolation(
            file=file_path,
            line_number=0,
            line_content="",
            violation_type="missing_evidence_link",
            matched_text=f"evidence/iteration_{expected_iteration_number}_evidence.json",
        )
        return

    # 检查是否有编号匹配的链接
    has_matching_link = any(num == expected_iteration_number for num in evidence_links)

    if not has_matching_link:
        # 有链接但编号不匹配
        found_numbers = sorted(set(evidence_links))
        yield PlaceholderViolation(
            file=file_path,
            line_number=0,
            line_content="",
            violation_type="mismatched_evidence_link",
            matched_text=(
                f"期望 iteration_{expected_iteration_number}_evidence.json，"
                f"但找到 iteration_{'、'.join(found_numbers)}_evidence.json"
            ),
        )


def scan_file(
    file_path: Path,
    check_required_headings: bool = True,
    check_evidence_link: bool = True,
) -> List[PlaceholderViolation]:
    """
    扫描单个文件的所有违规。

    Args:
        file_path: 要扫描的文件路径
        check_required_headings: 是否检查必需标题（默认 True）
        check_evidence_link: 是否检查证据链接（默认 True）

    Returns:
        违规列表
    """
    violations: List[PlaceholderViolation] = []

    # 检测模板占位符
    violations.extend(scan_file_for_placeholders(file_path))

    # 检测使用说明区块
    violations.extend(scan_file_for_usage_instructions(file_path))

    # 检测必需标题（仅 regression 文件）
    if check_required_headings:
        violations.extend(scan_file_for_required_headings(file_path))

    # 检测 evidence 链接（仅 regression 文件）
    if check_evidence_link:
        violations.extend(scan_file_for_evidence_link(file_path))

    return violations


# ============================================================================
# 检查执行
# ============================================================================


def run_check(
    verbose: bool = False,
    project_root: Optional[Path] = None,
    check_required_headings: bool = True,
    check_evidence_link: bool = True,
) -> tuple[List[PlaceholderViolation], int]:
    """
    执行模板占位符检查。

    Args:
        verbose: 是否显示详细输出
        project_root: 项目根目录（None 则自动检测）
        check_required_headings: 是否检查必需标题（默认 True）
        check_evidence_link: 是否检查证据链接（默认 True）

    Returns:
        (违规列表, 总扫描文件数)
    """
    if project_root is None:
        project_root = get_project_root()

    # 获取迭代文档文件
    files = get_iteration_files(project_root)

    if not files:
        if verbose:
            print("[INFO] 未找到任何迭代文档文件")
        return [], 0

    if verbose:
        print(f"[INFO] 将检查 {len(files)} 个迭代文档文件")
        for f in files:
            print(f"       - {f.relative_to(project_root)}")
        print()

    # 扫描
    violations: List[PlaceholderViolation] = []

    for file_path in files:
        file_violations = scan_file(
            file_path,
            check_required_headings=check_required_headings,
            check_evidence_link=check_evidence_link,
        )
        violations.extend(file_violations)

        if verbose and file_violations:
            rel_path = file_path.relative_to(project_root)
            print(f"  ❌ {rel_path}: {len(file_violations)} 个违规")
            for v in file_violations[:5]:  # 最多显示 5 个
                if v.line_number > 0:
                    print(f"     第 {v.line_number} 行: {v.matched_text}")
                else:
                    print(f"     文件级: {v.matched_text}")
            if len(file_violations) > 5:
                print(f"     ... 及其他 {len(file_violations) - 5} 个")

    return violations, len(files)


def print_report(
    violations: List[PlaceholderViolation],
    total_files: int,
    verbose: bool = False,
    project_root: Optional[Path] = None,
    warn_only_headings: bool = False,
) -> None:
    """
    打印检查报告。

    Args:
        violations: 违规列表
        total_files: 总扫描文件数
        verbose: 是否显示详细输出
        project_root: 项目根目录
        warn_only_headings: 是否仅警告标准标题问题（不阻断）
    """
    if project_root is None:
        project_root = get_project_root()

    print()
    print("=" * 70)
    print("迭代文档模板占位符检查报告")
    print("=" * 70)
    print()

    print(f"扫描文件数:      {total_files}")
    print(f"违规条目数:      {len(violations)}")

    # 按类型统计
    placeholder_count = sum(1 for v in violations if v.violation_type == "placeholder")
    instruction_count = sum(1 for v in violations if v.violation_type == "usage_instruction")
    heading_count = sum(1 for v in violations if v.violation_type == "missing_heading")
    missing_evidence_count = sum(
        1 for v in violations if v.violation_type == "missing_evidence_link"
    )
    mismatched_evidence_count = sum(
        1 for v in violations if v.violation_type == "mismatched_evidence_link"
    )
    print(f"  - 模板占位符:  {placeholder_count}")
    print(f"  - 使用说明:    {instruction_count}")
    print(f"  - 缺少标题:    {heading_count}")
    print(f"  - 缺少证据链接: {missing_evidence_count}")
    print(f"  - 证据链接不匹配: {mismatched_evidence_count}")
    if warn_only_headings:
        warn_only_count = heading_count + missing_evidence_count + mismatched_evidence_count
        if warn_only_count > 0:
            print("    (--warn-only 模式: 标准标题和证据链接检查不阻断)")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        # 按文件分组
        by_file: dict[Path, List[PlaceholderViolation]] = {}
        for v in violations:
            by_file.setdefault(v.file, []).append(v)

        for file_path, vlist in sorted(by_file.items()):
            rel_path = file_path.relative_to(project_root)
            print(f"\n【{rel_path}】({len(vlist)} 条)")

            # 分类显示
            placeholders = [v for v in vlist if v.violation_type == "placeholder"]
            instructions = [v for v in vlist if v.violation_type == "usage_instruction"]
            missing_headings = [v for v in vlist if v.violation_type == "missing_heading"]
            missing_evidence = [v for v in vlist if v.violation_type == "missing_evidence_link"]
            mismatched_evidence = [
                v for v in vlist if v.violation_type == "mismatched_evidence_link"
            ]

            if instructions:
                print("  模板使用说明（应移除）:")
                for v in instructions[:5]:
                    print(f"    第 {v.line_number} 行: {v.matched_text}")
                if len(instructions) > 5:
                    print(f"    ... 及其他 {len(instructions) - 5} 条")

            if placeholders:
                print("  模板占位符（应替换）:")
                for v in placeholders[:10]:
                    print(f"    第 {v.line_number} 行: {v.matched_text}")
                if len(placeholders) > 10:
                    print(f"    ... 及其他 {len(placeholders) - 10} 条")

            if missing_headings:
                mode_indicator = " [WARN]" if warn_only_headings else ""
                print(f"  缺少标准标题{mode_indicator}:")
                for v in missing_headings:
                    print(f"    - {v.matched_text}")

            if missing_evidence:
                mode_indicator = " [WARN]" if warn_only_headings else ""
                print(f"  缺少证据文件链接{mode_indicator}:")
                for v in missing_evidence:
                    print(f"    - 应添加: {v.matched_text}")

            if mismatched_evidence:
                mode_indicator = " [WARN]" if warn_only_headings else ""
                print(f"  证据链接编号不匹配{mode_indicator}:")
                for v in mismatched_evidence:
                    print(f"    - {v.matched_text}")

        print()
        print("-" * 70)
        print()
        print("修复指南:")
        print()
        print("  1. 模板占位符未替换:")
        print("     将 {N}, {YYYY-MM-DD}, {STATUS} 等占位符替换为实际值")
        print("     例如: Iteration {N} → Iteration 13")
        print("           {YYYY-MM-DD} → 2026-02-02")
        print()
        print("  2. 模板使用说明未移除:")
        print("     移除文件顶部的使用说明区块:")
        print("     > **使用说明**：复制本模板到 ...")
        print()
        print("  3. 缺少标准标题 (regression 文件):")
        print("     确保 regression 文件包含以下标准标题:")
        for heading in REGRESSION_REQUIRED_HEADINGS:
            print(f"       - {heading}")
        print()
        print("  4. 缺少证据文件链接 (regression 文件):")
        print("     在「## 验收证据」段落添加证据文件链接:")
        print("     - 格式: [iteration_N_evidence.json](evidence/iteration_N_evidence.json)")
        print("     - N 必须与文件名中的迭代编号一致")
        print("     - 使用 record_iteration_evidence.py 生成证据文件")
        print()
        print("  5. 证据链接编号不匹配:")
        print("     检查 evidence/iteration_N_evidence.json 链接中的 N")
        print("     确保与文件名 iteration_N_regression.md 中的 N 一致")
        print()
        print("  参考模板:")
        print("     - docs/acceptance/_templates/iteration_plan.template.md")
        print("     - docs/acceptance/_templates/iteration_regression.template.md")
        print()
    else:
        print("[OK] 未发现模板占位符或使用说明残留")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查迭代文档中的模板占位符和使用说明区块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="仅统计，不阻断（始终返回 0）",
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="仅警告模式：标准标题检查不阻断（占位符和使用说明仍然阻断）",
    )

    args = parser.parse_args()

    project_root = get_project_root()

    print("=" * 70)
    print("迭代文档模板占位符检查")
    print("=" * 70)
    print()

    violations, total_files = run_check(
        verbose=args.verbose,
        project_root=project_root,
        check_required_headings=True,
    )

    print_report(
        violations,
        total_files,
        verbose=args.verbose,
        project_root=project_root,
        warn_only_headings=args.warn_only,
    )

    # 确定退出码
    if args.stats_only:
        print()
        print("[INFO] --stats-only 模式: 仅统计，不阻断")
        print("[OK] 退出码: 0")
        return 0

    # 计算阻断性违规
    blocking_violations = violations
    # --warn-only 模式下不阻断的违规类型
    warn_only_types = {"missing_heading", "missing_evidence_link", "mismatched_evidence_link"}
    if args.warn_only:
        # --warn-only 模式下，标准标题和证据链接问题不阻断
        blocking_violations = [v for v in violations if v.violation_type not in warn_only_types]
        warn_violations = [v for v in violations if v.violation_type in warn_only_types]
        if warn_violations:
            print()
            heading_count = sum(1 for v in warn_violations if v.violation_type == "missing_heading")
            evidence_count = sum(
                1
                for v in warn_violations
                if v.violation_type in ("missing_evidence_link", "mismatched_evidence_link")
            )
            if heading_count > 0:
                print(f"[WARN] 标准标题警告: {heading_count} 条（不阻断）")
            if evidence_count > 0:
                print(f"[WARN] 证据链接警告: {evidence_count} 条（不阻断）")

    if blocking_violations:
        print()
        print(f"[FAIL] 存在 {len(blocking_violations)} 个阻断性违规")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    print("[OK] 所有检查通过")
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
