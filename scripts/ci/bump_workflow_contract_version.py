#!/usr/bin/env python3
"""
Workflow Contract 版本升级工具

用于自动更新 workflow_contract.v1.json 的 version 和 last_updated 字段，
并同步更新 contract.md 版本控制表。

功能：
1. 读取 workflow_contract.v1.json
2. 按参数（major/minor/patch 或显式版本）更新 version 与 last_updated
3. 在 docs/ci_nightly_workflow_refactor/contract.md 第 14 章表格顶部插入新行模板
4. 可选：在 JSON 顶层插入 _changelog_vX.Y.Z 的空模板

使用方式：
    # 升级 patch 版本（默认）
    python scripts/ci/bump_workflow_contract_version.py patch

    # 升级 minor 版本
    python scripts/ci/bump_workflow_contract_version.py minor

    # 升级 major 版本
    python scripts/ci/bump_workflow_contract_version.py major

    # 指定显式版本
    python scripts/ci/bump_workflow_contract_version.py --version 3.0.0

    # 不添加 changelog 模板
    python scripts/ci/bump_workflow_contract_version.py minor --no-changelog

    # 自定义变更说明
    python scripts/ci/bump_workflow_contract_version.py minor --message "新增 XXX 功能"

    # 干运行模式（不写入文件）
    python scripts/ci/bump_workflow_contract_version.py minor --dry-run

退出码：
    0: 成功
    1: 参数错误
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# ============================================================================
# Constants
# ============================================================================

DEFAULT_CONTRACT_PATH = "scripts/ci/workflow_contract.v1.json"
DEFAULT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"

# 版本升级类型
BUMP_TYPES = ("major", "minor", "patch")


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class BumpResult:
    """版本升级结果"""

    success: bool
    old_version: str
    new_version: str
    old_last_updated: str
    new_last_updated: str
    message: str
    changelog_key: str = ""
    errors: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_last_updated": self.old_last_updated,
            "new_last_updated": self.new_last_updated,
            "message": self.message,
            "changelog_key": self.changelog_key,
            "errors": self.errors,
        }


# ============================================================================
# Version Operations
# ============================================================================


def parse_semver(version: str) -> tuple[int, int, int] | None:
    """解析 SemVer 版本号

    Args:
        version: 版本字符串（如 "2.6.0"）

    Returns:
        (major, minor, patch) 元组，解析失败返回 None
    """
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def bump_version(current_version: str, bump_type: str) -> str | None:
    """升级版本号

    Args:
        current_version: 当前版本号
        bump_type: 升级类型（major/minor/patch）

    Returns:
        新版本号，解析失败返回 None
    """
    semver = parse_semver(current_version)
    if semver is None:
        return None

    major, minor, patch = semver

    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        return None


def is_valid_version(version: str) -> bool:
    """检查版本号是否有效

    Args:
        version: 版本字符串

    Returns:
        是否有效
    """
    return parse_semver(version) is not None


# ============================================================================
# File Operations
# ============================================================================


def load_contract(contract_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """加载 contract JSON 文件

    Args:
        contract_path: contract 文件路径

    Returns:
        (contract dict, error message)
    """
    if not contract_path.exists():
        return None, f"Contract file not found: {contract_path}"

    try:
        with open(contract_path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"Failed to parse contract JSON: {e}"


def save_contract(contract_path: Path, contract: dict[str, Any]) -> str | None:
    """保存 contract JSON 文件

    Args:
        contract_path: contract 文件路径
        contract: contract 数据

    Returns:
        error message（成功返回 None）
    """
    try:
        with open(contract_path, "w", encoding="utf-8") as f:
            json.dump(contract, f, indent=2, ensure_ascii=False)
            f.write("\n")  # 保持文件末尾换行
        return None
    except Exception as e:
        return f"Failed to save contract JSON: {e}"


def load_doc(doc_path: Path) -> tuple[str | None, str | None]:
    """加载文档文件

    Args:
        doc_path: 文档文件路径

    Returns:
        (文档内容, error message)
    """
    if not doc_path.exists():
        return None, f"Documentation file not found: {doc_path}"

    try:
        with open(doc_path, "r", encoding="utf-8") as f:
            return f.read(), None
    except Exception as e:
        return None, f"Failed to read documentation file: {e}"


def save_doc(doc_path: Path, content: str) -> str | None:
    """保存文档文件

    Args:
        doc_path: 文档文件路径
        content: 文档内容

    Returns:
        error message（成功返回 None）
    """
    try:
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(content)
        return None
    except Exception as e:
        return f"Failed to save documentation file: {e}"


# ============================================================================
# Contract Update Operations
# ============================================================================


def update_contract_version(
    contract: dict[str, Any],
    new_version: str,
    new_date: str,
    changelog_message: str | None = None,
    add_changelog: bool = True,
) -> dict[str, Any]:
    """更新 contract 中的版本信息

    Args:
        contract: contract 数据
        new_version: 新版本号
        new_date: 新日期
        changelog_message: changelog 消息（可选）
        add_changelog: 是否添加 changelog 条目

    Returns:
        更新后的 contract
    """
    # 创建新的 contract，确保字段顺序
    updated: dict[str, Any] = {}

    # 先复制 $schema（如果存在）
    if "$schema" in contract:
        updated["$schema"] = contract["$schema"]

    # 更新 version
    updated["version"] = new_version

    # 复制 description（如果存在）
    if "description" in contract:
        updated["description"] = contract["description"]

    # 更新 last_updated
    updated["last_updated"] = new_date

    # 添加 changelog 条目（如果需要）
    changelog_key = f"_changelog_v{new_version}"
    if add_changelog:
        message = changelog_message or "<在此填写变更说明>"
        updated[changelog_key] = message

    # 复制旧的 changelog 条目和其他字段
    for key, value in contract.items():
        if key in ("$schema", "version", "description", "last_updated"):
            continue
        # 跳过新的 changelog key（已经添加过了）
        if key == changelog_key:
            continue
        updated[key] = value

    return updated


# ============================================================================
# Document Update Operations
# ============================================================================


def insert_version_row_in_doc(
    doc_content: str,
    version: str,
    date_str: str,
    message: str | None = None,
) -> tuple[str, bool]:
    """在文档版本控制表顶部插入新行

    查找第 13/14 章"版本控制"表格，在表头分隔线之后第一行插入新版本行。

    Args:
        doc_content: 文档内容
        version: 版本号
        date_str: 日期
        message: 变更说明（可选）

    Returns:
        (更新后的文档内容, 是否成功)
    """
    # 构建新行
    change_msg = message or "<在此填写变更说明>"
    new_row = f"| v{version} | {date_str} | {change_msg} |"

    def split_table_cells(line: str) -> list[str]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return [cell for cell in cells if cell]

    def is_version_header(line: str) -> bool:
        if "|" not in line:
            return False
        cells = split_table_cells(line)
        return cells == ["版本", "日期", "变更说明"]

    def is_alignment_line(line: str, expected_columns: int) -> bool:
        if "|" not in line:
            return False
        cells = split_table_cells(line)
        if len(cells) != expected_columns:
            return False
        for cell in cells:
            normalized = cell.replace(" ", "")
            if not re.fullmatch(r":?-+:?", normalized):
                return False
        return True

    def detect_line_ending(line: str) -> str:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        return "\n"

    lines = doc_content.splitlines(keepends=True)
    if not lines:
        return doc_content, False

    section_header_pattern = re.compile(r"^##\s+(?:13|14)\.\s+版本控制\s*$")
    section_start = None
    for idx, line in enumerate(lines):
        if section_header_pattern.match(line.strip()):
            section_start = idx
            break

    if section_start is None:
        return doc_content, False

    section_end = len(lines)
    for idx in range(section_start + 1, len(lines)):
        if re.match(r"^##\s+", lines[idx].lstrip()):
            section_end = idx
            break

    for idx in range(section_start + 1, section_end - 1):
        if is_version_header(lines[idx]):
            if is_alignment_line(lines[idx + 1], 3):
                newline = detect_line_ending(lines[idx + 1])
                insert_at = idx + 2
                lines.insert(insert_at, new_row + newline)
                return "".join(lines), True

    return doc_content, False


def check_version_in_doc(doc_content: str, version: str) -> bool:
    """检查版本是否已在文档中

    Args:
        doc_content: 文档内容
        version: 版本号

    Returns:
        版本是否已存在
    """
    # 匹配 | v2.6.0 | 或 | 2.6.0 | 格式
    pattern = rf"\|\s*v?{re.escape(version)}\s*\|"
    return bool(re.search(pattern, doc_content))


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractVersionBumper:
    """Workflow Contract 版本升级器"""

    def __init__(
        self,
        project_root: Path,
        contract_path: str = DEFAULT_CONTRACT_PATH,
        doc_path: str = DEFAULT_DOC_PATH,
    ) -> None:
        self.project_root = project_root
        self.contract_path = project_root / contract_path
        self.doc_path = project_root / doc_path
        self.contract: dict[str, Any] = {}
        self.doc_content: str = ""

    def bump(
        self,
        bump_type: str | None = None,
        explicit_version: str | None = None,
        message: str | None = None,
        add_changelog: bool = True,
        dry_run: bool = False,
    ) -> BumpResult:
        """执行版本升级

        Args:
            bump_type: 升级类型（major/minor/patch）
            explicit_version: 显式指定的版本号
            message: 变更说明
            add_changelog: 是否添加 changelog 条目
            dry_run: 干运行模式（不写入文件）

        Returns:
            升级结果
        """
        errors: list[str] = []

        # 1. 加载 contract
        contract, err = load_contract(self.contract_path)
        if err:
            return BumpResult(
                success=False,
                old_version="",
                new_version="",
                old_last_updated="",
                new_last_updated="",
                message="",
                errors=[err],
            )
        assert contract is not None
        self.contract = contract

        old_version = contract.get("version", "0.0.0")
        old_last_updated = contract.get("last_updated", "")

        # 2. 确定新版本号
        if explicit_version:
            if not is_valid_version(explicit_version):
                return BumpResult(
                    success=False,
                    old_version=old_version,
                    new_version="",
                    old_last_updated=old_last_updated,
                    new_last_updated="",
                    message="",
                    errors=[f"Invalid version format: {explicit_version}"],
                )
            new_version = explicit_version
        elif bump_type:
            bumped = bump_version(old_version, bump_type)
            if bumped is None:
                return BumpResult(
                    success=False,
                    old_version=old_version,
                    new_version="",
                    old_last_updated=old_last_updated,
                    new_last_updated="",
                    message="",
                    errors=[f"Failed to bump version from {old_version}"],
                )
            new_version = bumped
        else:
            # 默认 patch 升级
            bumped = bump_version(old_version, "patch")
            if bumped is None:
                return BumpResult(
                    success=False,
                    old_version=old_version,
                    new_version="",
                    old_last_updated=old_last_updated,
                    new_last_updated="",
                    message="",
                    errors=[f"Failed to bump version from {old_version}"],
                )
            new_version = bumped

        # 3. 确定新日期
        new_date = date.today().isoformat()

        # 4. 加载文档
        doc_content, err = load_doc(self.doc_path)
        if err:
            errors.append(f"Warning: {err}")
            doc_content = ""
        self.doc_content = doc_content or ""

        # 5. 检查版本是否已存在
        version_exists_in_doc = False
        if doc_content and check_version_in_doc(doc_content, new_version):
            errors.append(f"Version {new_version} already exists in documentation")
            version_exists_in_doc = True

        # 6. 更新 contract
        changelog_key = f"_changelog_v{new_version}"
        updated_contract = update_contract_version(
            contract,
            new_version,
            new_date,
            changelog_message=message,
            add_changelog=add_changelog,
        )

        # 7. 更新文档
        updated_doc = self.doc_content
        doc_updated = False
        if self.doc_content and not version_exists_in_doc:
            updated_doc, doc_updated = insert_version_row_in_doc(
                self.doc_content,
                new_version,
                new_date,
                message,
            )
            if not doc_updated:
                errors.append("Failed to insert version row in documentation")

        # 8. 写入文件（除非 dry_run）
        if not dry_run:
            err = save_contract(self.contract_path, updated_contract)
            if err:
                return BumpResult(
                    success=False,
                    old_version=old_version,
                    new_version=new_version,
                    old_last_updated=old_last_updated,
                    new_last_updated=new_date,
                    message=message or "",
                    changelog_key=changelog_key if add_changelog else "",
                    errors=[err],
                )

            if doc_updated:
                err = save_doc(self.doc_path, updated_doc)
                if err:
                    errors.append(err)

        return BumpResult(
            success=True,
            old_version=old_version,
            new_version=new_version,
            old_last_updated=old_last_updated,
            new_last_updated=new_date,
            message=message or "",
            changelog_key=changelog_key if add_changelog else "",
            errors=errors if errors else None,
        )


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: BumpResult, dry_run: bool = False) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        if dry_run:
            lines.append("Workflow Contract Version Bump: DRY RUN")
        else:
            lines.append("Workflow Contract Version Bump: SUCCESS")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("Workflow Contract Version Bump: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Version: {result.old_version} -> {result.new_version}")
    lines.append(f"  - Date: {result.old_last_updated} -> {result.new_last_updated}")
    if result.changelog_key:
        lines.append(f"  - Changelog key: {result.changelog_key}")
    if result.message:
        lines.append(f"  - Message: {result.message}")

    if result.errors:
        lines.append("")
        lines.append("Warnings/Errors:")
        for err in result.errors:
            lines.append(f"  - {err}")

    if result.success:
        lines.append("")
        if dry_run:
            lines.append("No files were modified (dry run mode).")
        else:
            lines.append("Files updated successfully.")
            lines.append("")
            lines.append("Next steps:")
            lines.append("  1. Edit the changelog message in workflow_contract.v1.json")
            lines.append("  2. Edit the change description in contract.md")
            lines.append("  3. Run: make check-workflow-contract-version-policy")

    return "\n".join(lines)


def format_json_output(result: BumpResult) -> str:
    """格式化 JSON 输出"""
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="升级 workflow contract 版本号",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
    # 升级 patch 版本（默认）
    python scripts/ci/bump_workflow_contract_version.py patch

    # 升级 minor 版本
    python scripts/ci/bump_workflow_contract_version.py minor

    # 升级 major 版本
    python scripts/ci/bump_workflow_contract_version.py major

    # 指定显式版本
    python scripts/ci/bump_workflow_contract_version.py --version 3.0.0

    # 带变更说明
    python scripts/ci/bump_workflow_contract_version.py minor --message "新增 XXX 功能"

    # 干运行模式
    python scripts/ci/bump_workflow_contract_version.py minor --dry-run

版本升级规则（SemVer）：
    - Major (X.0.0): 不兼容变更（如删除校验规则、修改错误码含义）
    - Minor (0.X.0): 新增功能（新增校验规则、新增错误类型）
    - Patch (0.0.X): 修复/优化（修复 bug、优化性能、完善错误提示）
        """,
    )
    parser.add_argument(
        "bump_type",
        nargs="?",
        choices=BUMP_TYPES,
        default=None,
        help="升级类型：major/minor/patch（不指定时默认 patch）",
    )
    parser.add_argument(
        "--version",
        "-v",
        type=str,
        default=None,
        dest="explicit_version",
        help="显式指定版本号（如 3.0.0）",
    )
    parser.add_argument(
        "--message",
        "-m",
        type=str,
        default=None,
        help="变更说明（用于 changelog 和文档）",
    )
    parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="不添加 _changelog_vX.Y.Z 条目",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干运行模式（不写入文件）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--contract",
        type=str,
        default=DEFAULT_CONTRACT_PATH,
        help=f"Contract JSON 文件路径 (default: {DEFAULT_CONTRACT_PATH})",
    )
    parser.add_argument(
        "--doc",
        type=str,
        default=DEFAULT_DOC_PATH,
        help=f"Documentation Markdown 文件路径 (default: {DEFAULT_DOC_PATH})",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="项目根目录（默认使用当前工作目录）",
    )

    args = parser.parse_args()

    # 验证参数
    if args.explicit_version and args.bump_type:
        print("Error: Cannot specify both bump_type and --version", file=sys.stderr)
        return 1

    # 确定项目根目录
    if args.project_root:
        project_root = Path(args.project_root)
    else:
        # 尝试从脚本位置推断项目根目录
        script_path = Path(__file__).resolve()
        # scripts/ci/bump_workflow_contract_version.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    bumper = WorkflowContractVersionBumper(
        project_root=project_root,
        contract_path=args.contract,
        doc_path=args.doc,
    )

    result = bumper.bump(
        bump_type=args.bump_type,
        explicit_version=args.explicit_version,
        message=args.message,
        add_changelog=not args.no_changelog,
        dry_run=args.dry_run,
    )

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result, dry_run=args.dry_run))

    return 0 if result.success else 2


if __name__ == "__main__":
    sys.exit(main())
