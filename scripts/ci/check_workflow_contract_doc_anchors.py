#!/usr/bin/env python3
"""
Workflow Contract 文档锚点检查脚本

校验 validate_workflows.py 错误消息中引用的文档锚点是否在对应 Markdown 文档中存在。

功能：
1. 自动从 validate_workflows.py 源码中提取锚点引用（contract.md#... / maintenance.md#...）
2. 可选的显式附加锚点列表（用于补充或兼容）
3. 解析 contract.md 与 maintenance.md 的标题生成 slug（与 GitHub Markdown anchor 规则一致）
4. 验证所有引用的锚点都存在

使用方式：
    python scripts/ci/check_workflow_contract_doc_anchors.py
    python scripts/ci/check_workflow_contract_doc_anchors.py --json
    python scripts/ci/check_workflow_contract_doc_anchors.py --verbose
    python scripts/ci/check_workflow_contract_doc_anchors.py --export-anchors  # 导出 anchor 清单（JSON）
    python scripts/ci/check_workflow_contract_doc_anchors.py --list-anchors    # 列出可用锚点

退出码：
    0: 校验通过
    1: 校验失败（存在缺失的锚点）
    2: 文件读取/解析错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ============================================================================
# Constants
# ============================================================================

# 默认文档路径
DEFAULT_CONTRACT_DOC_PATH = "docs/ci_nightly_workflow_refactor/contract.md"
DEFAULT_MAINTENANCE_DOC_PATH = "docs/ci_nightly_workflow_refactor/maintenance.md"

# 默认 validate_workflows.py 路径
DEFAULT_VALIDATE_WORKFLOWS_PATH = "scripts/ci/validate_workflows.py"

# 显式附加锚点（用于补充自动提取可能遗漏的锚点，或兼容性目的）
# 格式: (doc_path_key, anchor_without_hash)
# doc_path_key: "contract" 或 "maintenance"
# 注意：自动提取优先，此列表仅作为补充
EXPLICIT_ANCHORS: list[tuple[str, str]] = [
    # 可选：如有自动提取无法覆盖的锚点，可在此添加
    # ("contract", "some-explicit-anchor"),
]


# ============================================================================
# Anchor Extraction from validate_workflows.py
# ============================================================================


def extract_anchors_from_source(source_path: Path) -> list[tuple[str, str]]:
    """
    从 validate_workflows.py 源码中提取锚点引用。

    扫描形如 contract.md#anchor 或 maintenance.md#anchor 的引用，
    返回 (doc_key, anchor) 元组列表。

    Args:
        source_path: validate_workflows.py 文件路径

    Returns:
        锚点列表，每项为 (doc_key, anchor)，doc_key 为 "contract" 或 "maintenance"
    """
    if not source_path.exists():
        return []

    try:
        content = source_path.read_text(encoding="utf-8")
    except Exception:
        return []

    anchors: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # 匹配 contract.md#anchor 或 maintenance.md#anchor
    # anchor 部分允许：字母、数字、中文、连字符、下划线
    pattern = re.compile(r"(contract|maintenance)\.md#([a-z0-9\u4e00-\u9fff_-]+)", re.IGNORECASE)

    for match in pattern.finditer(content):
        doc_key = match.group(1).lower()  # "contract" 或 "maintenance"
        anchor = match.group(2)

        # 去重
        key = (doc_key, anchor)
        if key not in seen:
            seen.add(key)
            anchors.append(key)

    return anchors


def get_required_anchors(
    validate_workflows_path: Path,
    explicit_anchors: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """
    获取需要验证的锚点列表。

    合并自动提取的锚点和显式附加的锚点（去重）。

    Args:
        validate_workflows_path: validate_workflows.py 文件路径
        explicit_anchors: 显式附加的锚点列表

    Returns:
        合并后的锚点列表（已去重）
    """
    # 自动提取
    extracted = extract_anchors_from_source(validate_workflows_path)

    # 合并显式锚点
    if explicit_anchors is None:
        explicit_anchors = EXPLICIT_ANCHORS

    # 使用 set 去重，保持顺序（提取的在前，显式的在后）
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []

    for anchor in extracted:
        if anchor not in seen:
            seen.add(anchor)
            result.append(anchor)

    for anchor in explicit_anchors:
        if anchor not in seen:
            seen.add(anchor)
            result.append(anchor)

    return result


# ============================================================================
# GitHub Markdown Anchor Generation
# ============================================================================


def generate_github_anchor(heading: str) -> str:
    """
    生成 GitHub 风格的 Markdown 锚点 slug。

    规则（与 GitHub Markdown anchor 规则一致）：
    - 转小写
    - 空格替换为 `-`
    - 保留字母、数字、中文字符、连字符、下划线
    - 移除其他特殊字符（包括标点符号、括号等）
    - 合并连续连字符
    - 移除首尾连字符

    注意：此函数仅生成 base anchor，不处理重复标题的 disambiguation。
    重复标题的处理（添加 -1, -2 等后缀）由 extract_headings_with_anchors() 负责。

    Args:
        heading: Markdown 标题文本（不含 # 前缀）

    Returns:
        生成的 anchor slug

    Examples:
        >>> generate_github_anchor("Hello World")
        'hello-world'
        >>> generate_github_anchor("第一章 介绍")
        '第一章-介绍'
        >>> generate_github_anchor("5.2 Frozen Step Names")
        '52-frozen-step-names'
        >>> generate_github_anchor("Hello (World)")
        'hello-world'
        >>> generate_github_anchor("特殊字符: `code` & <tag>")
        '特殊字符-code--tag'
    """
    # 转小写
    slug = heading.lower()

    # 空格替换为连字符
    slug = slug.replace(" ", "-")

    # 保留字母、数字、中文字符、连字符、下划线
    # 中文字符范围: \u4e00-\u9fff（基本汉字区）
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff_-]", "", slug)

    # 移除连续的连字符
    slug = re.sub(r"-+", "-", slug)

    # 移除首尾连字符
    slug = slug.strip("-")

    return slug


def extract_headings_with_anchors(content: str) -> dict[str, str]:
    """
    从 Markdown 内容中提取所有标题及其对应的 anchor slug。

    处理重复标题（Disambiguation 规则）：
    - 第一次出现：使用 base anchor（无后缀）
    - 第二次出现：添加 `-1` 后缀
    - 第三次出现：添加 `-2` 后缀
    - 依此类推...

    此规则与 GitHub Markdown 的行为一致。

    Args:
        content: Markdown 文档内容

    Returns:
        字典，key 为 anchor slug，value 为原始标题文本

    Examples:
        对于以下 Markdown 内容:
        ```
        ## Introduction
        ## Details
        ## Introduction
        ## Introduction
        ```
        返回:
        {
            "introduction": "Introduction",
            "details": "Details",
            "introduction-1": "Introduction",
            "introduction-2": "Introduction"
        }
    """
    headings: dict[str, str] = {}
    anchor_counts: dict[str, int] = {}

    # 匹配 Markdown 标题 (# 到 ######)
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    for match in heading_pattern.finditer(content):
        heading_text = match.group(2).strip()
        base_anchor = generate_github_anchor(heading_text)

        if not base_anchor:
            continue

        # 处理重复标题（GitHub disambiguation 规则）
        # 第一次出现: count=0, 使用 base_anchor
        # 第二次出现: count=1, 使用 base_anchor-1
        # 第三次出现: count=2, 使用 base_anchor-2
        if base_anchor in anchor_counts:
            anchor_counts[base_anchor] += 1
            final_anchor = f"{base_anchor}-{anchor_counts[base_anchor]}"
        else:
            anchor_counts[base_anchor] = 0
            final_anchor = base_anchor

        headings[final_anchor] = heading_text

    return headings


def export_anchor_list(
    content: str,
    include_heading_text: bool = False,
) -> list[str] | list[dict[str, str]]:
    """
    导出 Markdown 内容中的 anchor 清单。

    此函数用于测试断言和调试，提供可导出的 anchor 列表格式。

    Args:
        content: Markdown 文档内容
        include_heading_text: 如果为 True，返回包含 anchor 和 heading_text 的字典列表；
                              否则仅返回 anchor 字符串列表

    Returns:
        如果 include_heading_text=False: anchor 字符串列表 (按文档顺序)
        如果 include_heading_text=True: [{"anchor": str, "heading": str}, ...] 列表

    Examples:
        >>> content = "# Title\\n## Section"
        >>> export_anchor_list(content)
        ['title', 'section']
        >>> export_anchor_list(content, include_heading_text=True)
        [{'anchor': 'title', 'heading': 'Title'}, {'anchor': 'section', 'heading': 'Section'}]
    """
    anchors_with_headings: list[dict[str, str]] = []
    anchor_counts: dict[str, int] = {}

    # 匹配 Markdown 标题 (# 到 ######)
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    for match in heading_pattern.finditer(content):
        heading_text = match.group(2).strip()
        base_anchor = generate_github_anchor(heading_text)

        if not base_anchor:
            continue

        # 处理重复标题（GitHub disambiguation 规则）
        if base_anchor in anchor_counts:
            anchor_counts[base_anchor] += 1
            final_anchor = f"{base_anchor}-{anchor_counts[base_anchor]}"
        else:
            anchor_counts[base_anchor] = 0
            final_anchor = base_anchor

        anchors_with_headings.append({
            "anchor": final_anchor,
            "heading": heading_text,
        })

    if include_heading_text:
        return anchors_with_headings
    else:
        return [item["anchor"] for item in anchors_with_headings]


def export_doc_anchors_json(
    contract_doc_path: Path,
    maintenance_doc_path: Path,
) -> dict[str, Any]:
    """
    导出文档中所有 anchor 的 JSON 格式清单。

    此函数用于测试断言，提供完整的 anchor 清单输出。

    Args:
        contract_doc_path: contract.md 文件路径
        maintenance_doc_path: maintenance.md 文件路径

    Returns:
        JSON 格式的 anchor 清单:
        {
            "contract": {
                "file": str,
                "anchors": [{"anchor": str, "heading": str}, ...]
            },
            "maintenance": {
                "file": str,
                "anchors": [{"anchor": str, "heading": str}, ...]
            }
        }
    """
    result: dict[str, Any] = {}

    for doc_key, doc_path in [("contract", contract_doc_path), ("maintenance", maintenance_doc_path)]:
        if doc_path.exists():
            content = doc_path.read_text(encoding="utf-8")
            anchors = export_anchor_list(content, include_heading_text=True)
            result[doc_key] = {
                "file": str(doc_path),
                "anchor_count": len(anchors),
                "anchors": anchors,
            }
        else:
            result[doc_key] = {
                "file": str(doc_path),
                "anchor_count": 0,
                "anchors": [],
                "error": "file_not_found",
            }

    return result


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class AnchorError:
    """锚点错误"""

    error_type: str  # "anchor_missing", "file_not_found", "file_read_error"
    doc: str  # "contract" 或 "maintenance"
    anchor: str
    message: str


@dataclass
class AnchorCheckResult:
    """锚点检查结果"""

    success: bool = True
    errors: list[AnchorError] = field(default_factory=list)
    checked_anchors: list[tuple[str, str]] = field(default_factory=list)
    available_anchors: dict[str, list[str]] = field(default_factory=dict)

    def add_error(self, error: AnchorError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False


# ============================================================================
# Core Logic
# ============================================================================


class WorkflowContractDocAnchorChecker:
    """Workflow Contract 文档锚点检查器"""

    def __init__(
        self,
        contract_doc_path: Path,
        maintenance_doc_path: Path,
        validate_workflows_path: Path | None = None,
        explicit_anchors: list[tuple[str, str]] | None = None,
        verbose: bool = False,
    ) -> None:
        self.contract_doc_path = contract_doc_path
        self.maintenance_doc_path = maintenance_doc_path
        self.validate_workflows_path = validate_workflows_path
        self.explicit_anchors = explicit_anchors
        self.verbose = verbose
        self.result = AnchorCheckResult()
        self.doc_anchors: dict[str, dict[str, str]] = {}  # doc_key -> {anchor: heading}
        self.required_anchors: list[tuple[str, str]] = []  # 运行时确定的锚点列表

    def load_doc(self, doc_key: str, doc_path: Path) -> bool:
        """加载文档并提取锚点"""
        if not doc_path.exists():
            self.result.add_error(
                AnchorError(
                    error_type="file_not_found",
                    doc=doc_key,
                    anchor="",
                    message=f"Documentation file not found: {doc_path}",
                )
            )
            return False

        try:
            content = doc_path.read_text(encoding="utf-8")
            self.doc_anchors[doc_key] = extract_headings_with_anchors(content)

            if self.verbose:
                print(f"Loaded {doc_key}.md: {len(self.doc_anchors[doc_key])} anchors found")

            return True
        except Exception as e:
            self.result.add_error(
                AnchorError(
                    error_type="file_read_error",
                    doc=doc_key,
                    anchor="",
                    message=f"Failed to read documentation file: {e}",
                )
            )
            return False

    def check_anchor(self, doc_key: str, anchor: str) -> bool:
        """检查单个锚点是否存在"""
        if doc_key not in self.doc_anchors:
            return False

        return anchor in self.doc_anchors[doc_key]

    def check(self) -> AnchorCheckResult:
        """执行完整校验"""
        # 获取需要验证的锚点列表（自动提取 + 显式附加）
        if self.validate_workflows_path:
            self.required_anchors = get_required_anchors(
                self.validate_workflows_path,
                self.explicit_anchors,
            )
        else:
            # 仅使用显式锚点
            self.required_anchors = list(self.explicit_anchors or EXPLICIT_ANCHORS)

        if self.verbose:
            print(f"Auto-extracted + explicit anchors: {len(self.required_anchors)} total")

        # 加载文档
        if self.verbose:
            print(f"Loading contract.md: {self.contract_doc_path}")
        contract_loaded = self.load_doc("contract", self.contract_doc_path)

        if self.verbose:
            print(f"Loading maintenance.md: {self.maintenance_doc_path}")
        maintenance_loaded = self.load_doc("maintenance", self.maintenance_doc_path)

        # 如果任何文档加载失败，继续检查（但会有文件错误）
        if not contract_loaded or not maintenance_loaded:
            # 已经添加了文件错误，继续检查剩余的
            pass

        if self.verbose:
            print(f"\nChecking {len(self.required_anchors)} required anchors...")

        # 检查所有需要的锚点
        for doc_key, anchor in self.required_anchors:
            self.result.checked_anchors.append((doc_key, anchor))

            if doc_key not in self.doc_anchors:
                # 文档未加载成功，跳过（已有文件错误）
                continue

            if not self.check_anchor(doc_key, anchor):
                doc_filename = (
                    "contract.md" if doc_key == "contract" else "maintenance.md"
                )
                self.result.add_error(
                    AnchorError(
                        error_type="anchor_missing",
                        doc=doc_key,
                        anchor=anchor,
                        message=(
                            f"Anchor '#{anchor}' not found in {doc_filename}. "
                            f"This anchor is referenced in validate_workflows.py error messages. "
                            f"Please ensure the corresponding heading exists in the documentation."
                        ),
                    )
                )
                if self.verbose:
                    print(f"  [FAIL] {doc_key}.md#{anchor}")
            else:
                if self.verbose:
                    print(f"  [OK] {doc_key}.md#{anchor}")

        # 记录可用的锚点（用于调试）
        self.result.available_anchors = {
            doc_key: list(anchors.keys())
            for doc_key, anchors in self.doc_anchors.items()
        }

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: AnchorCheckResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        lines.append("Workflow Contract Doc Anchors Check: PASSED")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("Workflow Contract Doc Anchors Check: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Checked anchors: {len(result.checked_anchors)}")
    lines.append(f"  - Errors: {len(result.errors)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.doc}: #{error.anchor}")
            lines.append(f"    {error.message}")

    return "\n".join(lines)


def format_json_output(result: AnchorCheckResult) -> str:
    """格式化 JSON 输出"""
    output: dict[str, Any] = {
        "success": result.success,
        "error_count": len(result.errors),
        "checked_anchors": [
            {"doc": doc, "anchor": anchor}
            for doc, anchor in result.checked_anchors
        ],
        "errors": [
            {
                "error_type": e.error_type,
                "doc": e.doc,
                "anchor": e.anchor,
                "message": e.message,
            }
            for e in result.errors
        ],
        "available_anchors": result.available_anchors,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 validate_workflows.py 错误消息中引用的文档锚点是否存在",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--contract-doc",
        type=str,
        default=DEFAULT_CONTRACT_DOC_PATH,
        help=f"contract.md 文件路径 (default: {DEFAULT_CONTRACT_DOC_PATH})",
    )
    parser.add_argument(
        "--maintenance-doc",
        type=str,
        default=DEFAULT_MAINTENANCE_DOC_PATH,
        help=f"maintenance.md 文件路径 (default: {DEFAULT_MAINTENANCE_DOC_PATH})",
    )
    parser.add_argument(
        "--validate-workflows",
        type=str,
        default=DEFAULT_VALIDATE_WORKFLOWS_PATH,
        help=f"validate_workflows.py 文件路径 (default: {DEFAULT_VALIDATE_WORKFLOWS_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细检查过程",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="项目根目录（默认使用当前工作目录）",
    )
    parser.add_argument(
        "--list-anchors",
        action="store_true",
        help="列出文档中所有可用的锚点（用于调试）",
    )
    parser.add_argument(
        "--export-anchors",
        action="store_true",
        help="以 JSON 格式导出文档中所有锚点的详细清单（用于测试断言）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = Path(args.project_root)
    else:
        # 尝试从脚本位置推断项目根目录
        script_path = Path(__file__).resolve()
        # scripts/ci/check_workflow_contract_doc_anchors.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    contract_doc_path = project_root / args.contract_doc
    maintenance_doc_path = project_root / args.maintenance_doc
    validate_workflows_path = project_root / args.validate_workflows

    if args.verbose and not args.json and not args.export_anchors:
        print(f"Project root: {project_root}")
        print(f"Contract doc path: {contract_doc_path}")
        print(f"Maintenance doc path: {maintenance_doc_path}")
        print(f"Validate workflows path: {validate_workflows_path}")
        print()

    # 如果请求导出锚点清单，直接输出并退出
    if args.export_anchors:
        export_result = export_doc_anchors_json(contract_doc_path, maintenance_doc_path)
        print(json.dumps(export_result, indent=2, ensure_ascii=False))
        return 0

    checker = WorkflowContractDocAnchorChecker(
        contract_doc_path=contract_doc_path,
        maintenance_doc_path=maintenance_doc_path,
        validate_workflows_path=validate_workflows_path,
        verbose=args.verbose and not args.json,
    )

    result = checker.check()

    # 列出所有可用锚点（调试模式）
    if args.list_anchors:
        print("\nAvailable anchors in contract.md:")
        for anchor in sorted(result.available_anchors.get("contract", [])):
            print(f"  #{anchor}")
        print("\nAvailable anchors in maintenance.md:")
        for anchor in sorted(result.available_anchors.get("maintenance", [])):
            print(f"  #{anchor}")
        print()

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    # 返回退出码
    if result.errors:
        # 区分文件错误和锚点错误
        file_errors = [
            e for e in result.errors if e.error_type in ("file_not_found", "file_read_error")
        ]
        if file_errors and len(file_errors) == len(result.errors):
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
