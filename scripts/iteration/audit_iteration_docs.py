#!/usr/bin/env python3
"""
迭代文档审计脚本

功能:
1. 扫描 docs/acceptance/ 目录中的迭代文件
2. 解析 00_acceptance_matrix.md 索引表
3. 检查 SUPERSEDED 声明一致性
4. 生成审计报告（Markdown 格式）

输出:
- 默认输出到 stdout
- 使用 --output-dir 输出到 .artifacts/iteration-audit/

用法:
    # 输出到 stdout
    python scripts/iteration/audit_iteration_docs.py

    # 输出到文件
    python scripts/iteration/audit_iteration_docs.py --output-dir .artifacts/iteration-audit

    # 详细模式
    python scripts/iteration/audit_iteration_docs.py --verbose

定位说明:
- 本脚本用于生成一次性审计报告
- 审计报告为非 SSOT，仅作为临时参考
- CI 门禁检查请使用 scripts/ci/check_no_iteration_links_in_docs.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class IterationFile:
    """迭代文件信息。"""

    iteration_number: int
    file_type: str  # "plan" or "regression"
    path: Path
    has_superseded_header: bool = False
    superseded_successor: Optional[int] = None


@dataclass
class IterationIndexEntry:
    """索引表条目。"""

    iteration_number: int
    date: str
    status: str
    plan_link: Optional[str]
    regression_link: Optional[str]
    description: str
    row_index: int

    @property
    def is_superseded(self) -> bool:
        return "SUPERSEDED" in self.status.upper()

    def get_successor_number(self) -> Optional[int]:
        """从描述中提取后继迭代编号。"""
        match = re.search(
            r"已被\s*Iteration\s*(\d+)\s*取代|Superseded\s+by\s+Iteration\s*(\d+)",
            self.description,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1) or match.group(2))
        return None


@dataclass
class AuditResult:
    """审计结果。"""

    files: list[IterationFile]
    index_entries: list[IterationIndexEntry]
    inconsistencies: list[tuple[str, int, str]]  # (类型, 迭代号, 描述)
    missing_files: list[str]
    orphan_files: list[str]


# ============================================================================
# 扫描与解析
# ============================================================================


def scan_iteration_files(acceptance_dir: Path) -> list[IterationFile]:
    """扫描迭代文件。"""
    files: list[IterationFile] = []
    pattern = re.compile(r"iteration_(\d+)_(plan|regression)\.md$")

    if not acceptance_dir.exists():
        return files

    for filepath in sorted(acceptance_dir.glob("iteration_*_*.md")):
        match = pattern.match(filepath.name)
        if not match:
            continue

        iter_num = int(match.group(1))
        file_type = match.group(2)

        # 检查文件头部是否有 superseded 声明
        # 注意：以下检查逻辑与 scripts/ci/check_no_iteration_links_in_docs.py 的
        # check_regression_file_superseded_header 函数保持一致（同一 regex / 同一位置约束）
        # - 位置约束：检查前 20 行
        # - 正则表达式：r"Superseded\s+by\s+Iteration\s*(\d+)"（忽略大小写）
        has_header = False
        successor = None
        try:
            content = filepath.read_text(encoding="utf-8")
            for line in content.splitlines()[:20]:
                if re.search(r"Superseded\s+by\s+Iteration\s*(\d+)", line, re.IGNORECASE):
                    has_header = True
                    m = re.search(r"Iteration\s*(\d+)", line, re.IGNORECASE)
                    if m:
                        successor = int(m.group(1))
                    break
        except Exception:
            pass

        files.append(
            IterationFile(
                iteration_number=iter_num,
                file_type=file_type,
                path=filepath,
                has_superseded_header=has_header,
                superseded_successor=successor,
            )
        )

    return files


def parse_acceptance_matrix(matrix_path: Path) -> list[IterationIndexEntry]:
    """解析索引表。"""
    if not matrix_path.exists():
        return []

    content = matrix_path.read_text(encoding="utf-8")
    entries: list[IterationIndexEntry] = []

    lines = content.splitlines()
    in_index_section = False
    in_table = False
    row_index = 0

    for line in lines:
        if "迭代回归记录索引" in line and line.strip().startswith("#"):
            in_index_section = True
            continue

        if not in_index_section:
            continue

        if line.strip().startswith("#") and "迭代回归记录索引" not in line:
            break

        stripped = line.strip()
        if not stripped.startswith("|"):
            continue

        if "迭代" in stripped and "日期" in stripped:
            in_table = True
            continue
        if re.match(r"^\|[\s\-:]+\|", stripped):
            continue

        if not in_table:
            continue

        cells = [c.strip() for c in stripped.split("|")]
        if len(cells) < 7:
            continue

        iter_cell = cells[1]
        date_cell = cells[2]
        status_cell = cells[3]
        plan_cell = cells[4]
        regression_cell = cells[5]
        desc_cell = cells[6] if len(cells) > 6 else ""

        iter_match = re.search(r"Iteration\s*(\d+)", iter_cell, re.IGNORECASE)
        if not iter_match:
            continue

        iteration_number = int(iter_match.group(1))

        plan_link_match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", plan_cell)
        regression_link_match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", regression_cell)

        entry = IterationIndexEntry(
            iteration_number=iteration_number,
            date=date_cell,
            status=status_cell,
            plan_link=plan_link_match.group(2) if plan_link_match else None,
            regression_link=regression_link_match.group(2) if regression_link_match else None,
            description=desc_cell,
            row_index=row_index,
        )
        entries.append(entry)
        row_index += 1

    return entries


def run_audit(project_root: Path) -> AuditResult:
    """执行审计。"""
    acceptance_dir = project_root / "docs" / "acceptance"
    matrix_path = acceptance_dir / "00_acceptance_matrix.md"

    # 扫描文件
    files = scan_iteration_files(acceptance_dir)

    # 解析索引
    index_entries = parse_acceptance_matrix(matrix_path)

    # 构建索引映射
    indexed_iters = {e.iteration_number: e for e in index_entries}

    # 检查不一致
    inconsistencies: list[tuple[str, int, str]] = []
    missing_files: list[str] = []
    orphan_files: list[str] = []

    # 检查 SUPERSEDED 一致性
    for entry in index_entries:
        if not entry.is_superseded:
            continue

        successor = entry.get_successor_number()
        if successor is None:
            inconsistencies.append(
                (
                    "SUPERSEDED_NO_SUCCESSOR",
                    entry.iteration_number,
                    "索引标记为 SUPERSEDED 但未声明后继",
                )
            )
            continue

        # 检查 regression 文件是否有 superseded 声明
        regression_files = [
            f
            for f in files
            if f.iteration_number == entry.iteration_number and f.file_type == "regression"
        ]
        if regression_files:
            rf = regression_files[0]
            if not rf.has_superseded_header:
                inconsistencies.append(
                    (
                        "SUPERSEDED_MISSING_HEADER",
                        entry.iteration_number,
                        f"regression 文件缺少 superseded 声明（期望后继: Iteration {successor}）",
                    )
                )
            elif rf.superseded_successor != successor:
                inconsistencies.append(
                    (
                        "SUPERSEDED_MISMATCH",
                        entry.iteration_number,
                        f"regression 文件声明后继 ({rf.superseded_successor}) 与索引 ({successor}) 不一致",
                    )
                )

    # 检查文件存在性
    for entry in index_entries:
        if entry.regression_link and entry.regression_link != "-":
            if not (acceptance_dir / entry.regression_link).exists():
                missing_files.append(entry.regression_link)
        if entry.plan_link and entry.plan_link != "-":
            if not (acceptance_dir / entry.plan_link).exists():
                missing_files.append(entry.plan_link)

    # 检查孤儿文件
    for f in files:
        if f.iteration_number not in indexed_iters:
            orphan_files.append(f.path.name)

    return AuditResult(
        files=files,
        index_entries=index_entries,
        inconsistencies=inconsistencies,
        missing_files=missing_files,
        orphan_files=orphan_files,
    )


# ============================================================================
# 报告生成
# ============================================================================


def generate_report(result: AuditResult, project_root: Path) -> str:
    """生成 Markdown 格式的审计报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# 迭代文档审计报告",
        "",
        "> **生成时间**: " + now,
        ">",
        "> **非 SSOT**: 本报告为一次性审计快照，不作为权威来源。",
        "> 请以 `docs/acceptance/00_acceptance_matrix.md` 为准。",
        "",
        "---",
        "",
        "## 1. 审计范围",
        "",
        "- **索引文件**: `docs/acceptance/00_acceptance_matrix.md`",
        "- **扫描目录**: `docs/acceptance/`",
        "- **扫描模式**: `iteration_*_{plan,regression}.md`",
        "",
        "---",
        "",
        "## 2. 文件扫描结果",
        "",
        "### 2.1 发现的迭代文件",
        "",
        "| 迭代 | Plan 文件 | Regression 文件 | Superseded 声明 |",
        "|------|-----------|-----------------|-----------------|",
    ]

    # 按迭代号分组
    iter_nums = sorted(set(f.iteration_number for f in result.files))
    for iter_num in iter_nums:
        plan_files = [
            f for f in result.files if f.iteration_number == iter_num and f.file_type == "plan"
        ]
        regression_files = [
            f
            for f in result.files
            if f.iteration_number == iter_num and f.file_type == "regression"
        ]

        plan_status = f"✅ `{plan_files[0].path.name}`" if plan_files else "❌ 无"
        regression_status = f"✅ `{regression_files[0].path.name}`" if regression_files else "❌ 无"

        superseded_status = "-"
        if regression_files and regression_files[0].has_superseded_header:
            superseded_status = f"✅ Iteration {regression_files[0].superseded_successor}"
        elif regression_files:
            superseded_status = "❌ 无"

        lines.append(
            f"| Iteration {iter_num} | {plan_status} | {regression_status} | {superseded_status} |"
        )

    plan_count = len([f for f in result.files if f.file_type == "plan"])
    regression_count = len([f for f in result.files if f.file_type == "regression"])
    lines.append("")
    lines.append(f"**共计**: {regression_count} 个 regression 文件，{plan_count} 个 plan 文件")

    # 索引与文件对照
    lines.extend(
        [
            "",
            "---",
            "",
            "## 3. 索引与文件一致性对照",
            "",
            "### 3.1 迭代回归记录索引（来自 `00_acceptance_matrix.md`）",
            "",
            "| 迭代 | 日期 | 索引状态 | 索引说明 |",
            "|------|------|----------|----------|",
        ]
    )

    for entry in result.index_entries:
        lines.append(
            f"| Iteration {entry.iteration_number} | {entry.date} | {entry.status} | {entry.description} |"
        )

    # SUPERSEDED 检查结果
    superseded_entries = [e for e in result.index_entries if e.is_superseded]
    if superseded_entries:
        lines.extend(
            [
                "",
                "### 3.2 Superseded 声明检查结果",
                "",
                "| 迭代 | 索引状态 | 文件 Superseded 声明 | 一致性 | 备注 |",
                "|------|----------|----------------------|--------|------|",
            ]
        )

        for entry in result.index_entries:
            successor = entry.get_successor_number()
            regression_files = [
                f
                for f in result.files
                if f.iteration_number == entry.iteration_number and f.file_type == "regression"
            ]

            if entry.is_superseded:
                if regression_files:
                    rf = regression_files[0]
                    if rf.has_superseded_header:
                        if rf.superseded_successor == successor:
                            consistency = "✅ 一致"
                            note = f'声明: "Superseded by Iteration {rf.superseded_successor}"'
                        else:
                            consistency = "❌ **不一致**"
                            note = f"文件声明 Iteration {rf.superseded_successor}，索引声明 Iteration {successor}"
                        file_status = "✅ 有声明"
                    else:
                        consistency = "❌ **不一致**"
                        note = "索引标记为 SUPERSEDED，但文件缺少声明"
                        file_status = "❌ **无声明**"
                else:
                    consistency = "⚠️ 未知"
                    note = "regression 文件不存在"
                    file_status = "-"
            else:
                consistency = "✅ 一致"
                note = "非 SUPERSEDED 状态，无需声明"
                file_status = (
                    "❌ 无声明"
                    if regression_files and not regression_files[0].has_superseded_header
                    else "-"
                )

            lines.append(
                f"| Iteration {entry.iteration_number} | {entry.status} | {file_status} | {consistency} | {note} |"
            )

    # 发现的问题
    lines.extend(
        [
            "",
            "---",
            "",
            "## 4. 发现的问题",
            "",
        ]
    )

    if result.inconsistencies or result.missing_files or result.orphan_files:
        if result.inconsistencies:
            lines.append("### 4.1 🔴 不一致项")
            lines.append("")
            lines.append("| # | 问题描述 | 迭代 | 详情 |")
            lines.append("|---|----------|------|------|")
            for i, (type_, iter_num, desc) in enumerate(result.inconsistencies, 1):
                lines.append(f"| {i} | **{type_}** | Iteration {iter_num} | {desc} |")
            lines.append("")

        if result.missing_files:
            lines.append("### 4.2 🟡 缺失文件")
            lines.append("")
            for f in result.missing_files:
                lines.append(f"- `{f}`")
            lines.append("")

        if result.orphan_files:
            lines.append("### 4.3 🟠 孤儿文件（未被索引）")
            lines.append("")
            for f in result.orphan_files:
                lines.append(f"- `{f}`")
            lines.append("")
    else:
        lines.append("✅ 未发现问题")
        lines.append("")

    # 审计总结
    lines.extend(
        [
            "---",
            "",
            "## 5. 审计总结",
            "",
            "| 指标 | 结果 |",
            "|------|------|",
            f"| 总迭代数（索引中） | {len(result.index_entries)} |",
            f"| Regression 文件数 | {regression_count} |",
            f"| Plan 文件数 | {plan_count} |",
            f"| SUPERSEDED 状态迭代 | {len(superseded_entries)} |",
            f"| **一致性问题数** | **{len(result.inconsistencies)}** |",
            f"| 缺失文件数 | {len(result.missing_files)} |",
            f"| 孤儿文件数 | {len(result.orphan_files)} |",
            "",
            "---",
            "",
            "*报告生成完成*",
        ]
    )

    return "\n".join(lines)


# ============================================================================
# 主函数
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="迭代文档审计脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="输出目录（默认输出到 stdout）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细模式",
    )

    args = parser.parse_args()

    project_root = get_project_root()

    if args.verbose:
        print(f"[INFO] 项目根目录: {project_root}", file=sys.stderr)
        print("[INFO] 执行审计...", file=sys.stderr)

    result = run_audit(project_root)

    if args.verbose:
        print(f"[INFO] 扫描到 {len(result.files)} 个迭代文件", file=sys.stderr)
        print(f"[INFO] 解析到 {len(result.index_entries)} 个索引条目", file=sys.stderr)
        print(f"[INFO] 发现 {len(result.inconsistencies)} 个不一致项", file=sys.stderr)

    report = generate_report(result, project_root)

    if args.output_dir:
        output_dir = project_root / args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        output_file.write_text(report, encoding="utf-8")
        print(f"[INFO] 报告已写入: {output_file}", file=sys.stderr)
    else:
        print(report)

    # 如果有问题则返回非零退出码
    if result.inconsistencies or result.missing_files:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
