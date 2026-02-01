#!/usr/bin/env python3
"""
检查文档中的 .iteration/ 链接 和 SUPERSEDED 一致性

功能:
1. 扫描 docs/**/*.md 目录中的 Markdown 文件
2. 检测链接到 .iteration/ 目录的相对路径链接
3. 验证 SUPERSEDED 迭代的一致性:
   - 索引表中 SUPERSEDED 条目必须声明后继
   - 后继必须存在于索引表中且排序在上方
   - regression 文件顶部必须有标准 superseded 声明
4. 输出违反规则的位置列表和修复建议

策略规则:
- 禁止在文档中使用指向 .iteration/ 目录的相对链接
- 原因：.iteration/ 是临时工作目录，不应被文档引用
- 建议：将内容迁移到 docs/acceptance/ 或使用行内代码引用

SUPERSEDED 规则 (参见 docs/acceptance/00_acceptance_matrix.md):
- R1: 后继链接必须存在 - 说明字段必须包含"已被 Iteration X 取代"
- R2: 后继必须在索引表中 - 被引用的后继迭代必须已在索引表中存在
- R3: 后继排序在上方 - 后继迭代在表格中的位置必须在被取代迭代上方
- R4: 禁止环形引用 - 不允许 A→B→A 的循环取代链
- R5: 禁止多后继 - 每个迭代只能有一个直接后继
- R6: regression 声明必须存在 - regression 文件前 20 行内必须包含 `Superseded by Iteration M`，且后继编号 M 与索引表一致

索引完整性规则:
- R7: 链接文件必须存在 - 索引表中 plan_link/regression_link 指向的文件必须存在
- R8: 文件必须被索引 - docs/acceptance/iteration_*_regression.md 必须在索引表中
- R9: 索引降序排列 - 索引表中 iteration 编号必须降序（最新迭代在最前）

用法:
    # 检查 docs/ 目录
    python scripts/ci/check_no_iteration_links_in_docs.py

    # 检查指定路径
    python scripts/ci/check_no_iteration_links_in_docs.py --paths docs/gateway/

    # 详细输出
    python scripts/ci/check_no_iteration_links_in_docs.py --verbose

    # 仅统计（不阻断）
    python scripts/ci/check_no_iteration_links_in_docs.py --stats-only

    # 跳过 SUPERSEDED 检查
    python scripts/ci/check_no_iteration_links_in_docs.py --skip-superseded-check

    # 输出机器可读的 JSON 修复建议（快速定位 R3/R9 等排序问题）
    python scripts/ci/check_no_iteration_links_in_docs.py --suggest-fixes

退出码:
    0 - 检查通过或 --stats-only 模式
    1 - 检查失败（存在违规）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class IterationLinkViolation:
    """迭代链接违规记录。"""

    file: Path
    line_number: int
    line_content: str
    matched_link: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line_number}: 包含 .iteration/ 链接: {self.matched_link}"


@dataclass
class IterationIndexEntry:
    """索引表中的迭代条目。"""

    iteration_number: int
    date: str
    status: str
    plan_link: Optional[str]
    regression_link: Optional[str]
    description: str
    row_index: int  # 在表格中的行号（用于验证排序）

    @property
    def is_superseded(self) -> bool:
        return "SUPERSEDED" in self.status.upper()

    def get_successor_number(self) -> Optional[int]:
        """从描述中提取后继迭代编号。"""
        # 匹配 "已被 Iteration X 取代" 或 "Superseded by Iteration X"
        match = re.search(
            r"已被\s*Iteration\s*(\d+)\s*取代|Superseded\s+by\s+Iteration\s*(\d+)",
            self.description,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1) or match.group(2))
        return None


@dataclass
class SupersededViolation:
    """SUPERSEDED 一致性违规记录。"""

    rule_id: str  # R1, R2, R3, R4, R5, R6
    iteration_number: int
    message: str
    file: Optional[Path] = None
    line_number: Optional[int] = None

    def __str__(self) -> str:
        location = ""
        if self.file:
            location = f"{self.file}"
            if self.line_number:
                location += f":{self.line_number}"
            location += ": "
        return f"[{self.rule_id}] Iteration {self.iteration_number}: {location}{self.message}"


@dataclass
class SupersededCheckResult:
    """SUPERSEDED 检查结果。"""

    violations: List[SupersededViolation] = field(default_factory=list)
    iterations: Dict[int, IterationIndexEntry] = field(default_factory=dict)
    superseded_count: int = 0
    checked_count: int = 0


@dataclass
class IndexIntegrityCheckResult:
    """索引完整性检查结果。"""

    violations: List[SupersededViolation] = field(default_factory=list)
    missing_files: List[str] = field(default_factory=list)
    orphan_files: List[str] = field(default_factory=list)
    order_violations: List[tuple[int, int]] = field(default_factory=list)  # (prev, curr)


@dataclass
class FixSuggestion:
    """修复建议。"""

    rule_id: str
    iteration_number: int
    action: str  # "move_above", "add_successor", "remove_cycle", etc.
    description: str
    target_iteration: Optional[int] = None  # 目标迭代编号（如需移动）
    file: Optional[str] = None  # 需要修改的文件

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式。"""
        result: Dict[str, Any] = {
            "rule_id": self.rule_id,
            "iteration_number": self.iteration_number,
            "action": self.action,
            "description": self.description,
        }
        if self.target_iteration is not None:
            result["target_iteration"] = self.target_iteration
        if self.file is not None:
            result["file"] = self.file
        return result


@dataclass
class SuggestFixesReport:
    """修复建议报告。"""

    violations_count: int = 0
    suggestions: List[FixSuggestion] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式。"""
        return {
            "violations_count": self.violations_count,
            "suggestions_count": len(self.suggestions),
            "suggestions": [s.to_dict() for s in self.suggestions],
        }

    def to_json(self, indent: int = 2) -> str:
        """转换为 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ============================================================================
# 修复建议生成
# ============================================================================


def generate_fix_suggestions(
    superseded_result: Optional[SupersededCheckResult],
    integrity_result: Optional[IndexIntegrityCheckResult],
    project_root: Path,
) -> SuggestFixesReport:
    """
    根据违规结果生成修复建议。

    Args:
        superseded_result: SUPERSEDED 检查结果
        integrity_result: 索引完整性检查结果
        project_root: 项目根目录

    Returns:
        修复建议报告
    """
    report = SuggestFixesReport()
    matrix_file = "docs/acceptance/00_acceptance_matrix.md"

    # 处理 SUPERSEDED 违规
    if superseded_result:
        report.violations_count += len(superseded_result.violations)

        for violation in superseded_result.violations:
            suggestion: Optional[FixSuggestion] = None

            if violation.rule_id == "R1":
                # R1: 缺后继链接
                suggestion = FixSuggestion(
                    rule_id="R1",
                    iteration_number=violation.iteration_number,
                    action="add_successor_declaration",
                    description=(
                        f"在索引表 Iteration {violation.iteration_number} 的说明字段添加 "
                        f"'已被 Iteration X 取代'（将 X 替换为实际后继迭代编号）"
                    ),
                    file=matrix_file,
                )

            elif violation.rule_id == "R2":
                # R2: 后继不存在
                # 从消息中提取后继编号
                import re as re_module

                match = re_module.search(r"Iteration\s+(\d+)", violation.message)
                target = int(match.group(1)) if match else None
                suggestion = FixSuggestion(
                    rule_id="R2",
                    iteration_number=violation.iteration_number,
                    action="add_successor_to_index",
                    description=(
                        f"先在索引表中添加 Iteration {target} 条目，"
                        f"然后再将 Iteration {violation.iteration_number} 标记为 SUPERSEDED"
                    ),
                    target_iteration=target,
                    file=matrix_file,
                )

            elif violation.rule_id == "R3":
                # R3: 后继排序错误 - 需要移动行
                # 从消息中提取后继编号
                import re as re_module

                match = re_module.search(
                    r"后继 Iteration (\d+) \(行 \d+\) 应排在当前迭代 \(行 \d+\) 的上方",
                    violation.message,
                )
                if match:
                    successor = int(match.group(1))
                    suggestion = FixSuggestion(
                        rule_id="R3",
                        iteration_number=violation.iteration_number,
                        action="move_above",
                        description=(
                            f"将 Iteration {successor} 行移动到 "
                            f"Iteration {violation.iteration_number} 行的上方"
                        ),
                        target_iteration=successor,
                        file=matrix_file,
                    )

            elif violation.rule_id == "R4":
                # R4: 环形引用
                suggestion = FixSuggestion(
                    rule_id="R4",
                    iteration_number=violation.iteration_number,
                    action="break_cycle",
                    description=(
                        "检查取代链并打破环形引用，确保 SUPERSEDED 关系形成有向无环图 (DAG)"
                    ),
                    file=matrix_file,
                )

            elif violation.rule_id == "R5":
                # R5: 多后继
                suggestion = FixSuggestion(
                    rule_id="R5",
                    iteration_number=violation.iteration_number,
                    action="remove_extra_successor",
                    description=(
                        f"在 Iteration {violation.iteration_number} 的说明字段中"
                        f"保留一个后继声明，移除多余的后继声明"
                    ),
                    file=matrix_file,
                )

            elif violation.rule_id == "R6":
                # R6: regression 文件缺声明
                file_path = str(violation.file) if violation.file else None
                if file_path:
                    # 转换为相对路径
                    try:
                        rel_path = Path(file_path).relative_to(project_root)
                        file_path = str(rel_path)
                    except ValueError:
                        pass

                suggestion = FixSuggestion(
                    rule_id="R6",
                    iteration_number=violation.iteration_number,
                    action="add_superseded_header",
                    description=(
                        "在 regression 文件顶部添加 '> **⚠️ Superseded by Iteration X**' 声明"
                    ),
                    file=file_path,
                )

            if suggestion:
                report.suggestions.append(suggestion)

    # 处理索引完整性违规
    if integrity_result:
        report.violations_count += len(integrity_result.violations)

        for violation in integrity_result.violations:
            suggestion: Optional[FixSuggestion] = None

            if violation.rule_id == "R7":
                # R7: 链接文件不存在
                suggestion = FixSuggestion(
                    rule_id="R7",
                    iteration_number=violation.iteration_number,
                    action="create_or_remove_link",
                    description="创建缺失的文件，或将索引表中的链接改为 '-'",
                    file=matrix_file,
                )

            elif violation.rule_id == "R8":
                # R8: 孤儿文件
                file_path = str(violation.file) if violation.file else None
                if file_path:
                    try:
                        rel_path = Path(file_path).relative_to(project_root)
                        file_path = str(rel_path)
                    except ValueError:
                        pass

                suggestion = FixSuggestion(
                    rule_id="R8",
                    iteration_number=violation.iteration_number,
                    action="add_to_index_or_delete",
                    description=(
                        f"在索引表中添加 Iteration {violation.iteration_number} 条目，"
                        f"或删除孤儿文件"
                    ),
                    file=file_path,
                )

            elif violation.rule_id == "R9":
                # R9: 索引排序错误 - 需要移动行
                # 从 order_violations 中获取详细信息
                import re as re_module

                match = re_module.search(
                    r"Iteration (\d+) \(行 \d+\) 应在 Iteration (\d+) 之前",
                    violation.message,
                )
                if match:
                    current = int(match.group(1))
                    prev = int(match.group(2))
                    suggestion = FixSuggestion(
                        rule_id="R9",
                        iteration_number=current,
                        action="move_above",
                        description=(
                            f"将 Iteration {current} 行移动到 "
                            f"Iteration {prev} 行的上方（索引应按迭代编号降序排列）"
                        ),
                        target_iteration=prev,
                        file=matrix_file,
                    )

            if suggestion:
                report.suggestions.append(suggestion)

    return report


# ============================================================================
# 正则表达式
# ============================================================================

# 匹配 Markdown 链接中包含 .iteration/ 的相对路径
# 支持格式:
#   [text](../.iteration/foo.md)
#   [text](.iteration/bar.md)
#   [text](../../.iteration/baz/qux.md)
#   [text](path/to/.iteration/file.md)
ITERATION_LINK_PATTERN = re.compile(
    r"\]\("  # 链接开始 ](
    r"([^)]*"  # 捕获组：链接内容
    r"\.iteration/"  # 必须包含 .iteration/
    r"[^)]*)"  # 继续匹配到链接结束
    r"\)"  # 链接结束 )
)


# ============================================================================
# SUPERSEDED 检查逻辑
# ============================================================================


def parse_acceptance_matrix(matrix_path: Path) -> List[IterationIndexEntry]:
    """
    解析 00_acceptance_matrix.md 中的迭代回归记录索引表。

    返回: 按表格顺序排列的迭代条目列表（从上到下）
    """
    if not matrix_path.exists():
        return []

    content = matrix_path.read_text(encoding="utf-8")
    entries: List[IterationIndexEntry] = []

    # 查找索引表（在 "## 迭代回归记录索引" 之后）
    lines = content.splitlines()
    in_index_section = False
    in_table = False
    row_index = 0

    for line in lines:
        # 检测索引节开始
        if "迭代回归记录索引" in line and line.strip().startswith("#"):
            in_index_section = True
            continue

        if not in_index_section:
            continue

        # 检测下一个 section 开始（索引节结束）
        if line.strip().startswith("#") and "迭代回归记录索引" not in line:
            break

        # 检测表格行
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue

        # 跳过表头和分隔行
        if "迭代" in stripped and "日期" in stripped:
            in_table = True
            continue
        if re.match(r"^\|[\s\-:]+\|", stripped):
            continue

        if not in_table:
            continue

        # 解析表格行
        # 格式: | 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
        cells = [c.strip() for c in stripped.split("|")]
        if len(cells) < 7:  # 空格 + 6 列 + 空格
            continue

        # cells[0] 和 cells[-1] 是空的（|分隔）
        iter_cell = cells[1]  # 迭代
        date_cell = cells[2]  # 日期
        status_cell = cells[3]  # 状态
        plan_cell = cells[4]  # 计划
        regression_cell = cells[5]  # 详细记录
        desc_cell = cells[6] if len(cells) > 6 else ""  # 说明

        # 提取迭代编号
        iter_match = re.search(r"Iteration\s*(\d+)", iter_cell, re.IGNORECASE)
        if not iter_match:
            continue

        iteration_number = int(iter_match.group(1))

        # 提取链接
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


def check_regression_file_superseded_header(
    regression_path: Path,
    expected_successor: int,
) -> Optional[SupersededViolation]:
    """
    检查 regression 文件顶部是否有标准的 superseded 声明。

    期望格式:
    > **⚠️ Superseded by Iteration X**
    """
    if not regression_path.exists():
        return SupersededViolation(
            rule_id="R6",
            iteration_number=0,  # 会在调用处设置
            message=f"regression 文件不存在: {regression_path}",
            file=regression_path,
        )

    content = regression_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    # 检查前 20 行是否有 superseded 声明
    for i, line in enumerate(lines[:20], start=1):
        # 匹配 "> **⚠️ Superseded by Iteration X**" 或类似格式
        if re.search(
            r"Superseded\s+by\s+Iteration\s*(\d+)",
            line,
            re.IGNORECASE,
        ):
            # 提取声明中的后继编号
            match = re.search(r"Iteration\s*(\d+)", line, re.IGNORECASE)
            if match:
                declared_successor = int(match.group(1))
                if declared_successor != expected_successor:
                    return SupersededViolation(
                        rule_id="R6",
                        iteration_number=0,
                        message=f"superseded 声明的后继编号 ({declared_successor}) "
                        f"与索引表 ({expected_successor}) 不一致",
                        file=regression_path,
                        line_number=i,
                    )
            return None  # 找到有效声明

    return SupersededViolation(
        rule_id="R6",
        iteration_number=0,
        message=f"regression 文件顶部缺少标准 superseded 声明 "
        f"(期望: '> **⚠️ Superseded by Iteration {expected_successor}**')",
        file=regression_path,
    )


def check_superseded_consistency(
    project_root: Path,
    verbose: bool = False,
) -> SupersededCheckResult:
    """
    检查 SUPERSEDED 迭代的一致性。

    验证规则:
    - R1: 后继链接必须存在
    - R2: 后继必须在索引表中
    - R3: 后继排序在上方
    - R4: 禁止环形引用
    - R5: 禁止多后继
    - R6: regression 文件必须有 superseded 声明
    """
    result = SupersededCheckResult()

    matrix_path = project_root / "docs" / "acceptance" / "00_acceptance_matrix.md"
    if not matrix_path.exists():
        if verbose:
            print(f"[WARN] 验收矩阵文件不存在: {matrix_path}")
        return result

    entries = parse_acceptance_matrix(matrix_path)
    if not entries:
        if verbose:
            print("[WARN] 未能解析到任何迭代条目")
        return result

    # 构建迭代映射
    for entry in entries:
        result.iterations[entry.iteration_number] = entry

    # 用于检测环形引用
    successor_chain: Dict[int, int] = {}  # iteration -> successor

    # 检查每个 SUPERSEDED 条目
    for entry in entries:
        if not entry.is_superseded:
            continue

        result.superseded_count += 1
        result.checked_count += 1
        iter_num = entry.iteration_number

        if verbose:
            print(f"[INFO] 检查 SUPERSEDED 迭代: Iteration {iter_num}")

        # R1: 后继链接必须存在
        successor = entry.get_successor_number()
        if successor is None:
            result.violations.append(
                SupersededViolation(
                    rule_id="R1",
                    iteration_number=iter_num,
                    message="说明字段缺少后继声明（期望格式: '已被 Iteration X 取代'）",
                    file=matrix_path,
                )
            )
            continue

        successor_chain[iter_num] = successor

        # R5: 禁止多后继（检查是否有多个后继声明）
        multi_match = re.findall(
            r"已被\s*Iteration\s*(\d+)",
            entry.description,
            re.IGNORECASE,
        )
        if len(multi_match) > 1:
            result.violations.append(
                SupersededViolation(
                    rule_id="R5",
                    iteration_number=iter_num,
                    message=f"声明了多个后继: Iteration {', '.join(multi_match)}",
                    file=matrix_path,
                )
            )

        # R2: 后继必须在索引表中
        if successor not in result.iterations:
            result.violations.append(
                SupersededViolation(
                    rule_id="R2",
                    iteration_number=iter_num,
                    message=f"后继 Iteration {successor} 不在索引表中",
                    file=matrix_path,
                )
            )
            continue

        # R3: 后继排序在上方（row_index 更小）
        successor_entry = result.iterations[successor]
        if successor_entry.row_index >= entry.row_index:
            result.violations.append(
                SupersededViolation(
                    rule_id="R3",
                    iteration_number=iter_num,
                    message=f"后继 Iteration {successor} (行 {successor_entry.row_index + 1}) "
                    f"应排在当前迭代 (行 {entry.row_index + 1}) 的上方",
                    file=matrix_path,
                )
            )

        # R6: regression 文件必须有 superseded 声明
        if entry.regression_link:
            regression_path = project_root / "docs" / "acceptance" / entry.regression_link
            violation = check_regression_file_superseded_header(regression_path, successor)
            if violation:
                violation.iteration_number = iter_num
                result.violations.append(violation)

    # R4: 禁止环形引用
    visited: Set[int] = set()
    for start in successor_chain:
        path: List[int] = []
        current = start
        while current in successor_chain and current not in visited:
            if current in path:
                # 找到环
                cycle_start = path.index(current)
                cycle = path[cycle_start:] + [current]
                result.violations.append(
                    SupersededViolation(
                        rule_id="R4",
                        iteration_number=start,
                        message=f"存在环形引用: {' → '.join(map(str, cycle))}",
                        file=matrix_path,
                    )
                )
                break
            path.append(current)
            current = successor_chain[current]
        visited.update(path)

    return result


def check_index_integrity(
    project_root: Path,
    verbose: bool = False,
) -> IndexIntegrityCheckResult:
    """
    检查索引完整性。

    验证规则:
    - R7: 链接文件必须存在 - 索引表中引用的文件必须存在
    - R8: 文件必须被索引 - 存在的 iteration_*_regression.md 必须在索引中
    - R9: 索引降序排列 - iteration 编号必须降序排列
    """
    result = IndexIntegrityCheckResult()

    matrix_path = project_root / "docs" / "acceptance" / "00_acceptance_matrix.md"
    acceptance_dir = project_root / "docs" / "acceptance"

    if not matrix_path.exists():
        if verbose:
            print(f"[WARN] 验收矩阵文件不存在: {matrix_path}")
        return result

    entries = parse_acceptance_matrix(matrix_path)
    if not entries:
        if verbose:
            print("[WARN] 未能解析到任何迭代条目")
        return result

    # R7: 检查链接文件存在性
    for entry in entries:
        # 检查 plan_link
        if entry.plan_link and entry.plan_link != "-":
            plan_path = acceptance_dir / entry.plan_link
            if not plan_path.exists():
                result.missing_files.append(entry.plan_link)
                result.violations.append(
                    SupersededViolation(
                        rule_id="R7",
                        iteration_number=entry.iteration_number,
                        message=f"plan_link 指向的文件不存在: {entry.plan_link}",
                        file=matrix_path,
                    )
                )

        # 检查 regression_link
        if entry.regression_link and entry.regression_link != "-":
            regression_path = acceptance_dir / entry.regression_link
            if not regression_path.exists():
                result.missing_files.append(entry.regression_link)
                result.violations.append(
                    SupersededViolation(
                        rule_id="R7",
                        iteration_number=entry.iteration_number,
                        message=f"regression_link 指向的文件不存在: {entry.regression_link}",
                        file=matrix_path,
                    )
                )

    # R8: 检查文件覆盖（孤儿文件检测）
    # 获取索引中所有 iteration 编号
    indexed_iterations = {entry.iteration_number for entry in entries}

    # 扫描 docs/acceptance/iteration_*_(plan|regression).md
    iteration_file_pattern = re.compile(r"iteration_(\d+)_(plan|regression)\.md$")

    if acceptance_dir.exists():
        for filepath in acceptance_dir.glob("iteration_*_*.md"):
            match = iteration_file_pattern.match(filepath.name)
            if not match:
                continue

            iter_num = int(match.group(1))
            file_type = match.group(2)

            # regression 文件必须在索引中
            if file_type == "regression" and iter_num not in indexed_iterations:
                result.orphan_files.append(filepath.name)
                result.violations.append(
                    SupersededViolation(
                        rule_id="R8",
                        iteration_number=iter_num,
                        message=f"regression 文件 {filepath.name} 未在索引表中引用",
                        file=filepath,
                    )
                )
            # plan 文件可选，但如果存在且 iteration 不在索引中也应警告
            elif file_type == "plan" and iter_num not in indexed_iterations:
                result.orphan_files.append(filepath.name)
                result.violations.append(
                    SupersededViolation(
                        rule_id="R8",
                        iteration_number=iter_num,
                        message=f"plan 文件 {filepath.name} 的迭代 {iter_num} 未在索引表中",
                        file=filepath,
                    )
                )

    # R9: 检查索引降序排列
    prev_iter_num: Optional[int] = None
    for entry in entries:
        if prev_iter_num is not None:
            if entry.iteration_number > prev_iter_num:
                result.order_violations.append((prev_iter_num, entry.iteration_number))
                result.violations.append(
                    SupersededViolation(
                        rule_id="R9",
                        iteration_number=entry.iteration_number,
                        message=(
                            f"索引表未按降序排列: Iteration {entry.iteration_number} "
                            f"(行 {entry.row_index + 1}) 应在 Iteration {prev_iter_num} 之前。"
                            f"\n  修复建议: 将 Iteration {entry.iteration_number} 行移到表格更上方位置"
                        ),
                        file=matrix_path,
                    )
                )
        prev_iter_num = entry.iteration_number

    return result


# ============================================================================
# 配置
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录。"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def get_default_paths() -> List[str]:
    """获取默认检查路径。"""
    return ["docs/"]


def expand_paths(paths: List[str], project_root: Path) -> List[Path]:
    """
    展开路径列表为具体的 Markdown 文件列表。

    Args:
        paths: 路径列表（可包含文件或目录）
        project_root: 项目根目录

    Returns:
        Markdown 文件路径列表
    """
    files: List[Path] = []

    for path_str in paths:
        path = project_root / path_str

        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir() or path_str.endswith("/"):
            # 目录：递归查找所有 .md 文件
            dir_path = path if path.is_dir() else project_root / path_str.rstrip("/")
            if dir_path.exists():
                files.extend(dir_path.rglob("*.md"))

    return sorted(set(files))


# ============================================================================
# 扫描逻辑
# ============================================================================


def scan_file_for_iteration_links(file_path: Path) -> Iterator[IterationLinkViolation]:
    """
    扫描单个文件中的 .iteration/ 链接。

    跳过 Markdown 代码块（```...```）内的内容，因为代码块中的示例不应被检测。

    Args:
        file_path: 要扫描的文件路径

    Yields:
        IterationLinkViolation 对象
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return

    in_code_block = False
    for line_number, line in enumerate(content.splitlines(), start=1):
        # 检测代码块边界（支持 ``` 和 ~~~）
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue

        # 跳过代码块内的内容
        if in_code_block:
            continue

        for match in ITERATION_LINK_PATTERN.finditer(line):
            yield IterationLinkViolation(
                file=file_path,
                line_number=line_number,
                line_content=line.strip(),
                matched_link=match.group(1),
            )


# ============================================================================
# 检查执行
# ============================================================================


def run_check(
    paths: List[str] | None = None,
    verbose: bool = False,
    project_root: Path | None = None,
    quiet: bool = False,
) -> tuple[List[IterationLinkViolation], int]:
    """
    执行 .iteration/ 链接检查。

    Args:
        paths: 要检查的路径列表（None 则使用默认路径 docs/）
        verbose: 是否显示详细输出
        project_root: 项目根目录（None 则自动检测）
        quiet: 是否静默模式（抑制所有输出）

    Returns:
        (违规列表, 总扫描文件数)
    """
    if project_root is None:
        project_root = get_project_root()

    # 获取要检查的路径
    if paths is None:
        paths = get_default_paths()
        if not quiet:
            print(f"[INFO] 使用默认路径: {', '.join(paths)}")

    # 展开路径为文件列表
    files = expand_paths(paths, project_root)

    if not files:
        if not quiet:
            print("[WARN] 未找到任何 Markdown 文件")
        return [], 0

    if verbose and not quiet:
        print(f"[INFO] 将检查 {len(files)} 个文件")
        for f in files[:10]:
            print(f"       - {f.relative_to(project_root)}")
        if len(files) > 10:
            print(f"       ... 及其他 {len(files) - 10} 个文件")
        print()

    # 扫描
    violations: List[IterationLinkViolation] = []

    for file_path in files:
        for violation in scan_file_for_iteration_links(file_path):
            violations.append(violation)

            if verbose and not quiet:
                rel_path = file_path.relative_to(project_root)
                print(f"  ❌ {rel_path}:{violation.line_number}")
                print(f"     链接: {violation.matched_link}")

    return violations, len(files)


def print_report(
    violations: List[IterationLinkViolation],
    total_files: int,
    verbose: bool = False,
    superseded_result: Optional[SupersededCheckResult] = None,
    integrity_result: Optional[IndexIntegrityCheckResult] = None,
) -> None:
    """
    打印检查报告。

    Args:
        violations: .iteration/ 链接违规列表
        total_files: 总扫描文件数
        verbose: 是否显示详细输出
        superseded_result: SUPERSEDED 检查结果
        integrity_result: 索引完整性检查结果
    """
    project_root = get_project_root()

    print()
    print("=" * 70)
    print(".iteration/ 链接检查报告")
    print("=" * 70)
    print()

    print(f"扫描文件数:      {total_files}")
    print(f"违规条目数:      {len(violations)}")
    print()

    if violations:
        print("违规列表:")
        print("-" * 70)

        # 按文件分组
        by_file: dict[Path, List[IterationLinkViolation]] = {}
        for v in violations:
            by_file.setdefault(v.file, []).append(v)

        for file_path, vlist in sorted(by_file.items()):
            rel_path = file_path.relative_to(project_root)
            print(f"\n【{rel_path}】({len(vlist)} 条)")
            for v in vlist[:20]:  # 最多显示 20 条
                print(f"  第 {v.line_number} 行: {v.matched_link}")
                if verbose:
                    print(f"    {v.line_content[:80]}")
            if len(vlist) > 20:
                print(f"  ... 及其他 {len(vlist) - 20} 条")

        print()
        print("-" * 70)
        print()
        print("修复指南:")
        print("  .iteration/ 是临时工作目录，不应在文档中被引用。")
        print()
        print("  建议修复方式:")
        print()
        print("  1. 若内容需要长期引用：晋升到 docs/acceptance/")
        print("     使用 promote_iteration.py 将迭代文档正式化:")
        print("     $ python scripts/iteration/promote_iteration.py N")
        print("     ❌ [计划](../.iteration/plan.md)")
        print("     ✓  [计划](../acceptance/iteration_N_plan.md)")
        print()
        print("  2. 若只是分享草稿：使用 export_local_iteration.py")
        print("     $ python scripts/iteration/export_local_iteration.py N --output-dir /tmp/")
        print()
        print("  3. 若仅需提及路径：改为 inline code 或纯文本，不要 Markdown 链接")
        print("     ❌ [详见](.iteration/notes.md)")
        print("     ✓  详见 `.iteration/notes.md`")
        print()
    else:
        print("[OK] 未发现 .iteration/ 链接")

    # SUPERSEDED 检查报告
    if superseded_result is not None:
        print()
        print("=" * 70)
        print("SUPERSEDED 一致性检查报告")
        print("=" * 70)
        print()

        print(f"检查迭代数:      {superseded_result.checked_count}")
        print(f"SUPERSEDED 数:   {superseded_result.superseded_count}")
        print(f"违规条目数:      {len(superseded_result.violations)}")
        print()

        if superseded_result.violations:
            print("违规列表:")
            print("-" * 70)

            # 按规则分组
            by_rule: dict[str, List[SupersededViolation]] = {}
            for v in superseded_result.violations:
                by_rule.setdefault(v.rule_id, []).append(v)

            rule_descriptions = {
                "R1": "后继链接必须存在",
                "R2": "后继必须在索引表中",
                "R3": "后继排序在上方",
                "R4": "禁止环形引用",
                "R5": "禁止多后继",
                "R6": "regression 文件必须有 superseded 声明",
            }

            for rule_id in sorted(by_rule.keys()):
                vlist = by_rule[rule_id]
                desc = rule_descriptions.get(rule_id, "未知规则")
                print(f"\n【{rule_id}: {desc}】({len(vlist)} 条)")
                for v in vlist:
                    print(f"  ❌ {v}")

            print()
            print("-" * 70)
            print()
            print("修复指南:")
            print("  参见 docs/acceptance/00_acceptance_matrix.md 的 'SUPERSEDED 一致性规则'")
            print()
            print("  常见修复方式:")
            print("  1. R1 违规: 在索引表说明字段添加 '已被 Iteration X 取代'")
            print("  2. R2 违规: 确保后继迭代已在索引表中")
            print("  3. R3 违规: 调整索引表行顺序，后继应在上方")
            print("  4. R6 违规: 在 regression 文件顶部添加:")
            print("     > **⚠️ Superseded by Iteration X**")
            print("     >")
            print("     > 本文档已被 [Iteration X 回归记录](...) 取代。")
            print()
        else:
            print("[OK] SUPERSEDED 一致性检查通过")

    # 索引完整性检查报告
    if integrity_result is not None:
        print()
        print("=" * 70)
        print("索引完整性检查报告")
        print("=" * 70)
        print()

        print(f"缺失文件数:      {len(integrity_result.missing_files)}")
        print(f"孤儿文件数:      {len(integrity_result.orphan_files)}")
        print(f"排序违规数:      {len(integrity_result.order_violations)}")
        print(f"违规条目数:      {len(integrity_result.violations)}")
        print()

        if integrity_result.violations:
            print("违规列表:")
            print("-" * 70)

            # 按规则分组
            by_rule: dict[str, List[SupersededViolation]] = {}
            for v in integrity_result.violations:
                by_rule.setdefault(v.rule_id, []).append(v)

            rule_descriptions = {
                "R7": "链接文件必须存在",
                "R8": "文件必须被索引",
                "R9": "索引降序排列",
            }

            for rule_id in sorted(by_rule.keys()):
                vlist = by_rule[rule_id]
                desc = rule_descriptions.get(rule_id, "未知规则")
                print(f"\n【{rule_id}: {desc}】({len(vlist)} 条)")
                for v in vlist:
                    print(f"  ❌ {v}")

            print()
            print("-" * 70)
            print()
            print("修复指南:")
            print("  R7 违规: 创建缺失的文件或从索引表中移除无效链接")
            print("  R8 违规: 在索引表中添加对应的迭代条目")
            print("  R9 违规: 调整索引表行顺序，确保迭代编号降序（最新在最前）")
            print()
        else:
            print("[OK] 索引完整性检查通过")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="迭代文档检查工具 (.iteration/ 链接 + SUPERSEDED 一致性)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="要检查的路径列表（默认: docs/）",
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
        "--skip-superseded-check",
        action="store_true",
        help="跳过 SUPERSEDED 一致性检查",
    )
    parser.add_argument(
        "--superseded-only",
        action="store_true",
        help="仅执行 SUPERSEDED 一致性检查",
    )
    parser.add_argument(
        "--skip-integrity-check",
        action="store_true",
        help="跳过索引完整性检查",
    )
    parser.add_argument(
        "--integrity-only",
        action="store_true",
        help="仅执行索引完整性检查",
    )
    parser.add_argument(
        "--suggest-fixes",
        action="store_true",
        help="输出机器可读的 JSON 格式修复建议",
    )

    args = parser.parse_args()

    project_root = get_project_root()
    total_violations = 0
    link_violations: List[IterationLinkViolation] = []
    total_files = 0
    superseded_result: Optional[SupersededCheckResult] = None
    integrity_result: Optional[IndexIntegrityCheckResult] = None

    # --suggest-fixes 模式：静默执行检查，仅输出 JSON
    quiet_mode = args.suggest_fixes

    # 执行 .iteration/ 链接检查
    if not args.superseded_only and not args.integrity_only:
        if not quiet_mode:
            print("=" * 70)
            print(".iteration/ 链接检查")
            print("=" * 70)
            print()

        link_violations, total_files = run_check(
            paths=args.paths,
            verbose=args.verbose,
            project_root=project_root,
            quiet=quiet_mode,
        )
        total_violations += len(link_violations)

    # 执行 SUPERSEDED 一致性检查
    if not args.skip_superseded_check and not args.integrity_only:
        if not quiet_mode:
            if not args.superseded_only:
                print()
            print("=" * 70)
            print("SUPERSEDED 一致性检查")
            print("=" * 70)
            print()

        superseded_result = check_superseded_consistency(
            project_root=project_root,
            verbose=args.verbose and not quiet_mode,
        )
        total_violations += len(superseded_result.violations)

    # 执行索引完整性检查
    if not args.skip_integrity_check and not args.superseded_only:
        if not quiet_mode:
            if not args.integrity_only:
                print()
            print("=" * 70)
            print("索引完整性检查")
            print("=" * 70)
            print()

        integrity_result = check_index_integrity(
            project_root=project_root,
            verbose=args.verbose and not quiet_mode,
        )
        total_violations += len(integrity_result.violations)

    # --suggest-fixes 模式：输出 JSON 格式的修复建议
    if args.suggest_fixes:
        fix_report = generate_fix_suggestions(
            superseded_result=superseded_result,
            integrity_result=integrity_result,
            project_root=project_root,
        )
        print(fix_report.to_json())
        # suggest-fixes 模式下，根据是否有违规决定退出码
        if args.stats_only:
            return 0
        return 1 if fix_report.violations_count > 0 else 0

    # 打印报告
    if not args.superseded_only and not args.integrity_only:
        print_report(
            link_violations,
            total_files,
            verbose=args.verbose,
            superseded_result=superseded_result,
            integrity_result=integrity_result,
        )
    elif args.superseded_only:
        # 仅 SUPERSEDED 检查时，直接打印 SUPERSEDED 报告
        print_report(
            [],
            0,
            verbose=args.verbose,
            superseded_result=superseded_result,
        )
    elif args.integrity_only:
        # 仅索引完整性检查时，直接打印完整性报告
        print_report(
            [],
            0,
            verbose=args.verbose,
            integrity_result=integrity_result,
        )

    # 确定退出码
    if args.stats_only:
        print()
        print("[INFO] --stats-only 模式: 仅统计，不阻断")
        print("[OK] 退出码: 0")
        return 0

    if total_violations > 0:
        print()
        print(f"[FAIL] 存在 {total_violations} 个违规")
        if link_violations:
            print(f"       - .iteration/ 链接违规: {len(link_violations)}")
        if superseded_result and superseded_result.violations:
            print(f"       - SUPERSEDED 一致性违规: {len(superseded_result.violations)}")
        if integrity_result and integrity_result.violations:
            print(f"       - 索引完整性违规: {len(integrity_result.violations)}")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    print("[OK] 所有检查通过")
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
