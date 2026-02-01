#!/usr/bin/env python3
"""将本地迭代草稿晋升到 SSOT (docs/acceptance/)。

用法:
    python scripts/iteration/promote_iteration.py <iteration_number> [options]

示例:
    # 基本晋升
    python scripts/iteration/promote_iteration.py 13

    # 指定日期和状态
    python scripts/iteration/promote_iteration.py 13 --date 2026-02-01 --status PARTIAL

    # 晋升并标记旧迭代为已取代
    python scripts/iteration/promote_iteration.py 13 --supersede 12

    # 预览模式（不实际执行）
    python scripts/iteration/promote_iteration.py 13 --dry-run

功能:
    1. 检测 SSOT 冲突（若目标编号已在 docs/acceptance/ 存在则报错）
    2. 将 .iteration/<N>/plan.md 复制到 docs/acceptance/iteration_<N>_plan.md
    3. 将 .iteration/<N>/regression.md 复制到 docs/acceptance/iteration_<N>_regression.md
    4. 在 00_acceptance_matrix.md 索引表顶部插入新迭代条目（置顶）
    5. 可选：--supersede 标记旧迭代为 SUPERSEDED 并更新其 regression 文件头部

参数:
    iteration_number  目标迭代编号（必须）
    --date, -d        日期（YYYY-MM-DD 格式，默认今天）
    --status, -s      状态（PLANNING/PARTIAL/PASS/FAIL，默认 PLANNING）
    --description     说明文字（默认自动生成）
    --supersede OLD_N 标记旧迭代 OLD_N 为已被取代
    --force, -f       强制覆盖已存在的文件
    --dry-run, -n     预览模式，不实际修改文件

幂等策略:
    - 如果目标文件已存在且与源文件内容相同，跳过复制
    - 如果目标文件已存在但内容不同，报错并要求使用 --force 覆盖
    - 如果索引表已包含该迭代，跳过索引更新
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 目录定义
ITERATION_DIR = REPO_ROOT / ".iteration"
SSOT_DIR = REPO_ROOT / "docs" / "acceptance"
MATRIX_FILE = SSOT_DIR / "00_acceptance_matrix.md"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class PromoteResult:
    """晋升操作结果。"""

    success: bool
    message: str
    files_copied: List[str]
    files_skipped: List[str]
    index_updated: bool
    superseded_updated: bool


class SSOTConflictError(Exception):
    """当目标迭代编号已在 SSOT 中存在时抛出。"""

    def __init__(self, iteration_number: int, suggested_number: int) -> None:
        self.iteration_number = iteration_number
        self.suggested_number = suggested_number
        super().__init__(f"Iteration {iteration_number} 已在 docs/acceptance/ 中存在（SSOT 冲突）")


class SourceNotFoundError(Exception):
    """当源文件不存在时抛出。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"源文件不存在: {path}")


class FileConflictError(Exception):
    """当目标文件已存在且内容不同时抛出。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"目标文件已存在且内容不同: {path}\n使用 --force 参数强制覆盖")


class SupersedeValidationError(Exception):
    """当 --supersede 目标迭代不满足前置条件时抛出。

    前置条件（与 check_no_iteration_links_in_docs.py R6/R7 对齐）：
    - OLD_N 必须在 00_acceptance_matrix.md 索引表中存在
    - OLD_N 的 regression 文件必须存在
    """

    def __init__(self, old_iteration: int, reason: str, suggestion: str) -> None:
        self.old_iteration = old_iteration
        self.reason = reason
        self.suggestion = suggestion
        super().__init__(f"--supersede {old_iteration} 前置校验失败: {reason}")


class InvalidSourceError(Exception):
    """当源目录路径不合法时抛出。

    仅允许 .iteration/<N>/ 作为源目录，禁止:
    - .iteration/_export/<N>/ (快照目录，防止"快照覆盖 SSOT"误用)
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"无效的源目录: {path}\n{reason}")


class SnapshotPromoteError(Exception):
    """当尝试将快照目录晋升到 SSOT 时抛出。

    快照目录包含 DO_NOT_PROMOTE=true sentinel，不允许 promote。
    """

    def __init__(self, path: Path, iteration_number: int) -> None:
        self.path = path
        self.iteration_number = iteration_number
        super().__init__(
            f"目录 {path} 是 SSOT 快照，不能用于晋升。\n"
            f"快照仅供只读参考，如需创建新迭代，请使用:\n"
            f"  1. python scripts/iteration/init_local_iteration.py <next_N>\n"
            f"  2. python scripts/iteration/promote_iteration.py <next_N>"
        )


# sentinel 标识符，用于标记快照目录不可 promote
DO_NOT_PROMOTE_SENTINEL = "DO_NOT_PROMOTE=true"


# ============================================================================
# 辅助函数
# ============================================================================


def validate_source_directory(iteration_number: int) -> Path:
    """校验并返回有效的源目录路径。

    仅允许 .iteration/<N>/ 作为源目录，禁止:
    - .iteration/_export/<N>/ (快照目录，防止"快照覆盖 SSOT"误用)

    Args:
        iteration_number: 迭代编号

    Returns:
        有效的源目录路径

    Raises:
        InvalidSourceError: 如果源目录路径不合法或位于禁止区域
    """
    # 正确的源目录
    valid_src = ITERATION_DIR / str(iteration_number)

    # 禁止的源目录模式
    export_dir = ITERATION_DIR / "_export" / str(iteration_number)
    if export_dir.exists():
        raise InvalidSourceError(
            export_dir,
            reason=(
                f".iteration/_export/{iteration_number}/ 是快照目录，不能作为晋升来源。\n"
                "快照仅供只读参考，晋升操作必须从 .iteration/<N>/ 源目录执行。\n"
                f"请使用: .iteration/{iteration_number}/"
            ),
        )

    return valid_src


def check_snapshot_sentinel(src_dir: Path, iteration_number: int) -> None:
    """检查源目录或其父目录是否包含 DO_NOT_PROMOTE sentinel。

    快照目录的 README.md 中包含 DO_NOT_PROMOTE=true 标记，
    用于防止用户误将快照晋升到 SSOT。

    Args:
        src_dir: 源目录路径
        iteration_number: 迭代编号

    Raises:
        SnapshotPromoteError: 如果检测到 sentinel 标记
    """
    if not src_dir.exists():
        return

    # 检查源目录及其父目录中的 README.md
    dirs_to_check = [src_dir]

    # 也检查父目录（例如 .iteration/_export/10 的父目录 .iteration/_export）
    # 最多向上检查 3 级
    current = src_dir
    for _ in range(3):
        parent = current.parent
        if parent == current or parent == REPO_ROOT:
            break
        dirs_to_check.append(parent)
        current = parent

    for check_dir in dirs_to_check:
        readme_path = check_dir / "README.md"
        if readme_path.exists():
            try:
                content = readme_path.read_text(encoding="utf-8")
                if DO_NOT_PROMOTE_SENTINEL in content:
                    raise SnapshotPromoteError(src_dir, iteration_number)
            except (OSError, UnicodeDecodeError):
                # 读取失败时跳过检查
                continue


def validate_supersede_target(old_iteration: int) -> None:
    """校验 --supersede 目标迭代是否满足前置条件。

    前置条件（与 check_no_iteration_links_in_docs.py R6/R7 对齐）：
    1. OLD_N 必须在 00_acceptance_matrix.md 索引表中存在
    2. OLD_N 的 regression 文件必须存在（索引中有链接且文件实际存在）

    Args:
        old_iteration: 要被取代的旧迭代编号

    Raises:
        SupersedeValidationError: 如果前置条件不满足
    """
    # 检查索引表是否存在
    if not MATRIX_FILE.exists():
        raise SupersedeValidationError(
            old_iteration,
            reason="索引表 00_acceptance_matrix.md 不存在",
            suggestion="请先创建 docs/acceptance/00_acceptance_matrix.md",
        )

    # 解析索引表获取已索引的迭代
    indexed = get_indexed_iteration_numbers()

    # 条件 1: OLD_N 必须在索引表中
    if old_iteration not in indexed:
        raise SupersedeValidationError(
            old_iteration,
            reason=f"Iteration {old_iteration} 不在索引表中",
            suggestion=(
                f"请先将 Iteration {old_iteration} 添加到 docs/acceptance/00_acceptance_matrix.md，\n"
                f"或使用 promote_iteration.py {old_iteration} 晋升该迭代"
            ),
        )

    # 条件 2: OLD_N 的 regression 文件必须存在
    # 首先检查索引中是否有 regression_link
    content = MATRIX_FILE.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"\|\s*\*{{0,2}}Iteration\s+{old_iteration}\*{{0,2}}\s*\|",
        re.IGNORECASE,
    )

    regression_file = SSOT_DIR / f"iteration_{old_iteration}_regression.md"
    has_regression_link = False

    for line in content.splitlines():
        if pattern.match(line):
            # 检查该行是否有 regression 链接
            if f"iteration_{old_iteration}_regression.md" in line:
                has_regression_link = True
            break

    # 检查 regression 文件是否实际存在
    if not regression_file.exists():
        if has_regression_link:
            # R7 违规：索引中有链接但文件不存在
            raise SupersedeValidationError(
                old_iteration,
                reason=f"索引表中有 regression 链接但文件不存在: {regression_file.name}",
                suggestion=(
                    f"请创建 docs/acceptance/iteration_{old_iteration}_regression.md，\n"
                    "或修复索引表中的链接"
                ),
            )
        else:
            # 索引中无链接且文件不存在
            raise SupersedeValidationError(
                old_iteration,
                reason=f"Iteration {old_iteration} 的 regression 文件不存在",
                suggestion=(
                    f"请创建 docs/acceptance/iteration_{old_iteration}_regression.md，\n"
                    f"并在索引表中添加对应的 regression_link"
                ),
            )


def get_ssot_iteration_numbers() -> set[int]:
    """扫描 docs/acceptance/ 获取已存在的迭代编号。

    Returns:
        已在 SSOT 中使用的迭代编号集合
    """
    numbers: set[int] = set()
    pattern = re.compile(r"^iteration_(\d+)_(plan|regression)\.md$")

    if not SSOT_DIR.exists():
        return numbers

    for file_path in SSOT_DIR.iterdir():
        if file_path.is_file():
            match = pattern.match(file_path.name)
            if match:
                numbers.add(int(match.group(1)))

    return numbers


def get_next_available_number() -> int:
    """获取下一个可用的迭代编号。

    Returns:
        当前最大编号 + 1，若无已存在编号则返回 1
    """
    existing = get_ssot_iteration_numbers()
    if not existing:
        return 1
    return max(existing) + 1


def get_indexed_iteration_numbers() -> set[int]:
    """从 00_acceptance_matrix.md 索引表获取已索引的迭代编号。

    Returns:
        已在索引表中的迭代编号集合
    """
    numbers: set[int] = set()

    if not MATRIX_FILE.exists():
        return numbers

    content = MATRIX_FILE.read_text(encoding="utf-8")
    # 匹配 "| Iteration N" 或 "| **Iteration N**"
    pattern = re.compile(r"\|\s*\*{0,2}Iteration\s+(\d+)\*{0,2}\s*\|", re.IGNORECASE)

    for match in pattern.finditer(content):
        numbers.add(int(match.group(1)))

    return numbers


def check_ssot_conflict(iteration_number: int) -> None:
    """检查迭代编号是否与 SSOT 冲突。

    Args:
        iteration_number: 要检查的迭代编号

    Raises:
        SSOTConflictError: 如果编号已在 SSOT 中存在
    """
    existing = get_ssot_iteration_numbers()
    if iteration_number in existing:
        suggested = get_next_available_number()
        raise SSOTConflictError(iteration_number, suggested)


def files_are_identical(file1: Path, file2: Path) -> bool:
    """检查两个文件内容是否相同。

    Args:
        file1: 第一个文件路径
        file2: 第二个文件路径

    Returns:
        True 如果内容相同，否则 False
    """
    if not file1.exists() or not file2.exists():
        return False

    return file1.read_text(encoding="utf-8") == file2.read_text(encoding="utf-8")


# ============================================================================
# 索引更新
# ============================================================================


def parse_index_table_position(content: str) -> tuple[int, int]:
    """解析索引表的位置（表头后的第一行和表格结束行）。

    Args:
        content: 文件内容

    Returns:
        (insert_position, table_end_position) 行号元组
    """
    lines = content.splitlines()
    in_index_section = False
    in_table = False
    header_line = -1
    separator_line = -1
    table_end = -1

    for i, line in enumerate(lines):
        # 检测索引节开始
        if "迭代回归记录索引" in line and line.strip().startswith("#"):
            in_index_section = True
            continue

        if not in_index_section:
            continue

        # 检测下一个 section 开始
        if line.strip().startswith("#") and "迭代回归记录索引" not in line:
            table_end = i
            break

        # 检测表头行
        stripped = line.strip()
        if stripped.startswith("|") and ("迭代" in stripped or "Iteration" in stripped):
            header_line = i
            continue

        # 检测分隔行
        if header_line >= 0 and re.match(r"^\|[\s\-:]+\|", stripped):
            separator_line = i
            in_table = True
            continue

        # 检测表格数据行
        if in_table:
            if not stripped.startswith("|"):
                table_end = i
                break

    if table_end == -1:
        table_end = len(lines)

    # 插入位置是分隔行之后
    insert_position = separator_line + 1 if separator_line >= 0 else -1

    return insert_position, table_end


def status_to_display(status: str) -> str:
    """将状态码转换为显示格式。

    Args:
        status: 状态码（PLANNING/PARTIAL/PASS/FAIL/SUPERSEDED）

    Returns:
        带 emoji 的状态显示字符串
    """
    status_map = {
        "PLANNING": "🔄 PLANNING",
        "PARTIAL": "⚠️ PARTIAL",
        "PASS": "✅ PASS",
        "FAIL": "❌ FAIL",
        "SUPERSEDED": "🔄 SUPERSEDED",
    }
    return status_map.get(status.upper(), f"⚠️ {status}")


def create_index_entry(
    iteration_number: int,
    date: str,
    status: str = "PLANNING",
    plan_link: Optional[str] = None,
    regression_link: Optional[str] = None,
    description: str = "当前活跃迭代",
) -> str:
    """创建索引表条目。

    Args:
        iteration_number: 迭代编号
        date: 日期（YYYY-MM-DD 格式）
        status: 状态码（PLANNING/PARTIAL/PASS/FAIL）
        plan_link: 计划文件链接（None 表示无）
        regression_link: 回归记录链接（None 表示无）
        description: 说明

    Returns:
        格式化的表格行
    """
    plan_cell = (
        f"[iteration_{iteration_number}_plan.md](iteration_{iteration_number}_plan.md)"
        if plan_link
        else "-"
    )
    regression_cell = (
        f"[iteration_{iteration_number}_regression.md](iteration_{iteration_number}_regression.md)"
        if regression_link
        else "-"
    )
    status_display = status_to_display(status)

    return f"| **Iteration {iteration_number}** | {date} | {status_display} | {plan_cell} | {regression_cell} | {description} |"


def insert_index_entry(content: str, entry: str, position: int) -> str:
    """在索引表中插入新条目。

    Args:
        content: 文件内容
        entry: 要插入的条目
        position: 插入位置（行号）

    Returns:
        更新后的内容
    """
    lines = content.splitlines()
    lines.insert(position, entry)
    return "\n".join(lines)


def update_matrix_for_supersede(
    content: str,
    old_iteration: int,
    new_iteration: int,
) -> str:
    """更新索引表中旧迭代的状态为 SUPERSEDED。

    6 列表格格式: | 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
    split("|") 后: [空, 迭代, 日期, 状态, 计划, 详细记录, 说明, 空]
    索引:           0    1     2     3     4      5        6     7

    Args:
        content: 文件内容
        old_iteration: 被取代的迭代编号
        new_iteration: 新迭代编号

    Returns:
        更新后的内容
    """
    lines = content.splitlines()
    pattern = re.compile(
        rf"^\|\s*\*{{0,2}}Iteration\s+{old_iteration}\*{{0,2}}\s*\|",
        re.IGNORECASE,
    )

    for i, line in enumerate(lines):
        if pattern.match(line):
            # 解析并更新该行
            cells = line.split("|")
            # 6 列表格: | 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
            # cells: ["", " 迭代 ", " 日期 ", " 状态 ", " 计划 ", " 详细记录 ", " 说明 ", ""]
            if len(cells) >= 8:  # 空 + 6列 + 空 = 8
                # 更新状态列 (index 3)
                cells[3] = " 🔄 SUPERSEDED "
                # 更新说明列 (index 6) - 包含唯一后继声明
                cells[6] = f" 已被 Iteration {new_iteration} 取代 "
                lines[i] = "|".join(cells)
            break

    return "\n".join(lines)


# ============================================================================
# Regression 文件更新
# ============================================================================


def add_superseded_header(content: str, successor: int) -> str:
    """在 regression 文件顶部添加 superseded 声明。

    如果已存在声明，则更新后继编号。

    格式来源（SSOT）:
        docs/acceptance/_templates/iteration_regression.template.md 的 "Superseded by …" 章节

    格式要求:
        - 位置：文件最开头，必须在首个非空内容（包括标题）之前
        - 包含 `⚠️ Superseded by Iteration X` 标识符
        - 包含后继链接 `[Iteration X](iteration_X_regression.md)`
        - 以 `---` 分隔线结尾

    Args:
        content: 文件内容
        successor: 后继迭代编号

    Returns:
        更新后的内容
    """
    # R6 规范格式（与 iteration_regression.template.md 一致）
    # 包含: blockquote 包裹 + 后继链接 + --- 分隔线
    superseded_header = f"""> **⚠️ Superseded by Iteration {successor}**
>
> 本迭代已被 [Iteration {successor}](iteration_{successor}_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---

"""

    # 检查是否已有 superseded 声明（匹配 R6 规则检查的格式）
    existing_match = re.search(
        r"Superseded\s+by\s+Iteration\s*(\d+)",
        content,
        re.IGNORECASE,
    )
    if existing_match:
        # 已存在声明，更新后继编号
        old_successor = existing_match.group(1)
        content = re.sub(
            rf"Superseded\s+by\s+Iteration\s*{old_successor}",
            f"Superseded by Iteration {successor}",
            content,
            flags=re.IGNORECASE,
        )
        # 同时更新链接中的迭代编号
        content = re.sub(
            rf"iteration_{old_successor}_regression\.md",
            f"iteration_{successor}_regression.md",
            content,
        )
    else:
        # 插入到首个非空内容之前
        lines = content.splitlines(keepends=True)
        leading_empty_count = 0
        for line in lines:
            if line.strip() == "":
                leading_empty_count += 1
            else:
                break

        if leading_empty_count > 0:
            # 保留开头的空行，在其后插入
            leading_empty = "".join(lines[:leading_empty_count])
            rest_content = "".join(lines[leading_empty_count:])
            content = leading_empty + superseded_header + rest_content
        else:
            # 无开头空行，直接在最前面插入
            content = superseded_header + content

    return content


# ============================================================================
# 核心晋升逻辑
# ============================================================================


def promote_iteration(
    iteration_number: int,
    *,
    date: Optional[str] = None,
    status: str = "PLANNING",
    description: Optional[str] = None,
    supersede: Optional[int] = None,
    force: bool = False,
    dry_run: bool = False,
) -> PromoteResult:
    """将本地迭代晋升到 SSOT。

    Args:
        iteration_number: 要晋升的迭代编号
        date: 日期（YYYY-MM-DD 格式，默认今天）
        status: 状态（PLANNING/PARTIAL/PASS/FAIL，默认 PLANNING）
        description: 说明文字（默认自动生成）
        supersede: 要标记为已取代的旧迭代编号
        force: 是否强制覆盖已存在的文件
        dry_run: 是否仅预览操作

    Returns:
        PromoteResult 操作结果

    Raises:
        SSOTConflictError: 如果迭代已在 SSOT 中存在
        SourceNotFoundError: 如果源文件不存在
        FileConflictError: 如果目标文件已存在且内容不同（未使用 --force）
        InvalidSourceError: 如果源目录路径不合法
    """
    # 默认日期为今天
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # 默认说明
    if description is None:
        description = f"Iteration {iteration_number} 计划"
    files_copied: List[str] = []
    files_skipped: List[str] = []
    index_updated = False
    superseded_updated = False

    # 校验源目录（防止 _export 快照目录误用）
    src_dir = validate_source_directory(iteration_number)

    # 源文件路径
    src_plan = src_dir / "plan.md"
    src_regression = src_dir / "regression.md"

    # 目标文件路径
    dst_plan = SSOT_DIR / f"iteration_{iteration_number}_plan.md"
    dst_regression = SSOT_DIR / f"iteration_{iteration_number}_regression.md"

    # 检查源目录是否存在
    if not src_dir.exists():
        raise SourceNotFoundError(src_dir)

    # 检查源目录是否为快照（包含 DO_NOT_PROMOTE sentinel）
    check_snapshot_sentinel(src_dir, iteration_number)

    # 检查 SSOT 冲突（仅当目标文件不存在时）
    existing_ssot = get_ssot_iteration_numbers()
    if iteration_number in existing_ssot and not force:
        # 检查是否为幂等操作（内容相同）
        plan_identical = (
            files_are_identical(src_plan, dst_plan)
            if src_plan.exists() and dst_plan.exists()
            else False
        )
        regression_identical = (
            files_are_identical(src_regression, dst_regression)
            if src_regression.exists() and dst_regression.exists()
            else False
        )

        if not (plan_identical and regression_identical):
            suggested = get_next_available_number()
            raise SSOTConflictError(iteration_number, suggested)

    # 复制文件
    file_pairs = []
    if src_plan.exists():
        file_pairs.append((src_plan, dst_plan))
    if src_regression.exists():
        file_pairs.append((src_regression, dst_regression))

    if not file_pairs:
        raise SourceNotFoundError(src_dir)

    for src, dst in file_pairs:
        if dst.exists():
            if files_are_identical(src, dst):
                files_skipped.append(str(dst.relative_to(REPO_ROOT)))
                continue
            elif not force:
                raise FileConflictError(dst)

        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        files_copied.append(str(dst.relative_to(REPO_ROOT)))

    # 更新索引表
    indexed = get_indexed_iteration_numbers()
    if iteration_number not in indexed:
        if MATRIX_FILE.exists():
            content = MATRIX_FILE.read_text(encoding="utf-8")
            insert_pos, _ = parse_index_table_position(content)

            if insert_pos >= 0:
                entry = create_index_entry(
                    iteration_number,
                    date,
                    status=status,
                    plan_link="plan" if src_plan.exists() else None,
                    regression_link="regression" if src_regression.exists() else None,
                    description=description,
                )

                content = insert_index_entry(content, entry, insert_pos)

                if not dry_run:
                    MATRIX_FILE.write_text(content, encoding="utf-8")

                index_updated = True

    # 处理 --supersede
    if supersede is not None:
        # 前置校验（与 check_no_iteration_links_in_docs.py R6/R7 对齐）
        # 仅在非 dry-run 模式下强制校验
        if not dry_run:
            validate_supersede_target(supersede)

        # 更新索引表中旧迭代的状态
        if MATRIX_FILE.exists():
            content = MATRIX_FILE.read_text(encoding="utf-8")
            content = update_matrix_for_supersede(content, supersede, iteration_number)

            if not dry_run:
                MATRIX_FILE.write_text(content, encoding="utf-8")

        # 更新旧迭代的 regression 文件
        old_regression = SSOT_DIR / f"iteration_{supersede}_regression.md"
        if old_regression.exists():
            content = old_regression.read_text(encoding="utf-8")
            content = add_superseded_header(content, iteration_number)

            if not dry_run:
                old_regression.write_text(content, encoding="utf-8")

            superseded_updated = True

    action = "将" if not dry_run else "[DRY-RUN] 将"
    return PromoteResult(
        success=True,
        message=f"{action} Iteration {iteration_number} 晋升到 docs/acceptance/",
        files_copied=files_copied,
        files_skipped=files_skipped,
        index_updated=index_updated,
        superseded_updated=superseded_updated,
    )


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="将本地迭代草稿晋升到 SSOT (docs/acceptance/)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 晋升 Iteration 13
    python scripts/iteration/promote_iteration.py 13

    # 指定日期和状态
    python scripts/iteration/promote_iteration.py 13 --date 2026-02-01 --status PARTIAL

    # 晋升 Iteration 13 并标记 Iteration 12 为已取代
    python scripts/iteration/promote_iteration.py 13 --supersede 12

    # 预览晋升操作
    python scripts/iteration/promote_iteration.py 13 --dry-run

    # 强制覆盖已存在的文件
    python scripts/iteration/promote_iteration.py 13 --force

幂等策略:
    - 如果目标文件已存在且与源文件内容相同，跳过复制
    - 如果目标文件已存在但内容不同，报错并要求使用 --force 覆盖
    - 如果索引表已包含该迭代，跳过索引更新
        """,
    )
    parser.add_argument(
        "iteration_number",
        type=int,
        help="要晋升的迭代编号",
    )
    parser.add_argument(
        "--date",
        "-d",
        type=str,
        default=None,
        help="日期（YYYY-MM-DD 格式，默认今天）",
    )
    parser.add_argument(
        "--status",
        "-s",
        type=str,
        choices=["PLANNING", "PARTIAL", "PASS", "FAIL"],
        default="PLANNING",
        help="状态（默认 PLANNING）",
    )
    parser.add_argument(
        "--description",
        type=str,
        default=None,
        help="说明文字（默认自动生成）",
    )
    parser.add_argument(
        "--supersede",
        type=int,
        default=None,
        metavar="OLD_N",
        help="标记旧迭代 OLD_N 为已被当前迭代取代",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="强制覆盖已存在的文件",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="仅预览操作，不实际执行",
    )

    args = parser.parse_args()

    # 验证日期格式
    if args.date is not None:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"❌ 错误: 日期格式无效: {args.date}（需要 YYYY-MM-DD 格式）", file=sys.stderr)
            return 1

    # 验证 supersede 不能是自己
    if args.supersede is not None and args.supersede == args.iteration_number:
        print("❌ 错误: --supersede 不能指定为当前迭代编号", file=sys.stderr)
        return 1

    try:
        result = promote_iteration(
            args.iteration_number,
            date=args.date,
            status=args.status,
            description=args.description,
            supersede=args.supersede,
            force=args.force,
            dry_run=args.dry_run,
        )

        prefix = "[DRY-RUN] " if args.dry_run else ""
        print(f"✅ {prefix}Iteration {args.iteration_number} 晋升完成")
        print()

        if result.files_copied:
            print(f"{prefix}复制的文件:")
            for f in result.files_copied:
                print(f"  📄 {f}")

        if result.files_skipped:
            print(f"\n{prefix}跳过的文件（内容相同）:")
            for f in result.files_skipped:
                print(f"  ✓ {f}")

        if result.index_updated:
            print(f"\n{prefix}📋 索引表已更新: docs/acceptance/00_acceptance_matrix.md")

        if result.superseded_updated:
            print(f"\n{prefix}🔄 Iteration {args.supersede} 已标记为 SUPERSEDED")

        if args.dry_run:
            print("\n[DRY-RUN] 以上操作未实际执行，移除 --dry-run 参数以执行晋升")

        return 0

    except SSOTConflictError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("SSOT 中已存在以下文件:", file=sys.stderr)
        plan_file = SSOT_DIR / f"iteration_{e.iteration_number}_plan.md"
        regression_file = SSOT_DIR / f"iteration_{e.iteration_number}_regression.md"
        if plan_file.exists():
            print(f"  - {plan_file.relative_to(REPO_ROOT)}", file=sys.stderr)
        if regression_file.exists():
            print(f"  - {regression_file.relative_to(REPO_ROOT)}", file=sys.stderr)
        print(file=sys.stderr)
        print(f"💡 建议: 使用下一可用编号 {e.suggested_number}", file=sys.stderr)
        print(
            f"   python scripts/iteration/promote_iteration.py {e.suggested_number}",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        print("或使用 --force 参数强制覆盖:", file=sys.stderr)
        print(
            f"   python scripts/iteration/promote_iteration.py {e.iteration_number} --force",
            file=sys.stderr,
        )
        return 1

    except SourceNotFoundError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("请确保本地迭代目录存在:", file=sys.stderr)
        print(f"  .iteration/{args.iteration_number}/", file=sys.stderr)
        print(file=sys.stderr)
        print("使用以下命令初始化本地迭代:", file=sys.stderr)
        print(
            f"   python scripts/iteration/init_local_iteration.py {args.iteration_number}",
            file=sys.stderr,
        )
        return 1

    except FileConflictError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return 1

    except SupersedeValidationError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("--supersede 前置校验失败（与 R6/R7 规则对齐）:", file=sys.stderr)
        print(f"  原因: {e.reason}", file=sys.stderr)
        print(file=sys.stderr)
        print("💡 建议:", file=sys.stderr)
        for line in e.suggestion.split("\n"):
            print(f"   {line}", file=sys.stderr)
        print(file=sys.stderr)
        print("参考文档:", file=sys.stderr)
        print(
            "  - docs/acceptance/00_acceptance_matrix.md (SUPERSEDED 一致性规则)", file=sys.stderr
        )
        print(
            "  - scripts/ci/check_no_iteration_links_in_docs.py (R6/R7 规则)",
            file=sys.stderr,
        )
        return 1

    except InvalidSourceError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("源目录校验失败:", file=sys.stderr)
        print(f"  {e.reason}", file=sys.stderr)
        return 1

    except SnapshotPromoteError as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("检测到 DO_NOT_PROMOTE sentinel，此目录是 SSOT 快照。", file=sys.stderr)
        print(file=sys.stderr)
        print("💡 正确用法:", file=sys.stderr)
        print("   快照仅供只读参考，如需创建新迭代，请使用:", file=sys.stderr)
        print(file=sys.stderr)
        next_num = get_next_available_number()
        print(f"   1. python scripts/iteration/init_local_iteration.py {next_num}", file=sys.stderr)
        print(f"   2. python scripts/iteration/promote_iteration.py {next_num}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
