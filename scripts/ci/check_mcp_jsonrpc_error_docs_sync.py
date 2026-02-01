#!/usr/bin/env python3
"""
MCP JSON-RPC 错误码文档与 Schema 同步校验脚本

校验 docs/contracts/mcp_jsonrpc_error_v1.md 文档中定义的错误码是否与
schemas/mcp_jsonrpc_error_v1.schema.json 中的定义保持同步。

校验范围：
1. error_reason: 错误原因码（如 PARSE_ERROR, UNKNOWN_TOOL）
2. error_category: 错误分类（protocol, validation, business, dependency, internal）
3. jsonrpc_error.code: JSON-RPC 错误码（如 -32700, -32602）

使用方式：
    python scripts/ci/check_mcp_jsonrpc_error_docs_sync.py
    python scripts/ci/check_mcp_jsonrpc_error_docs_sync.py --json
    python scripts/ci/check_mcp_jsonrpc_error_docs_sync.py --verbose

退出码：
    0: 校验通过
    1: 校验失败（存在不同步项）
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

DEFAULT_DOC_PATH = "docs/contracts/mcp_jsonrpc_error_v1.md"
DEFAULT_SCHEMA_PATH = "schemas/mcp_jsonrpc_error_v1.schema.json"

# 文档中各章节的锚点关键字（用于提取区块）
# 3.1-3.5 节定义了各类 error_reason
DOC_REASON_SECTION_ANCHORS = [
    "### 3.1 protocol",
    "### 3.2 validation",
    "### 3.3 business",
    "### 3.4 dependency",
    "### 3.5 internal",
]

# 第 4 节定义了 JSON-RPC 错误码映射
DOC_JSONRPC_CODE_SECTION_ANCHOR = "## 4. JSON-RPC 错误码映射"

# 第 2 节的 TypeScript 接口定义了 error_category
DOC_ERROR_DATA_SECTION_ANCHOR = "## 2. 错误数据结构"


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class SyncError:
    """同步错误"""

    error_type: str  # "missing_in_doc", "missing_in_schema", "extra_in_doc", "extra_in_schema"
    category: str  # "error_reason", "error_category", "jsonrpc_code"
    value: str
    message: str
    fix_suggestion: str = ""


@dataclass
class SyncResult:
    """同步校验结果"""

    success: bool = True
    errors: list[SyncError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # 校验统计
    schema_reasons: list[str] = field(default_factory=list)
    doc_reasons: list[str] = field(default_factory=list)
    schema_categories: list[str] = field(default_factory=list)
    doc_categories: list[str] = field(default_factory=list)
    schema_codes: list[int] = field(default_factory=list)
    doc_codes: list[int] = field(default_factory=list)

    def add_error(self, error: SyncError) -> None:
        """添加错误"""
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str) -> None:
        """添加警告"""
        self.warnings.append(warning)


# ============================================================================
# Parser Functions
# ============================================================================


def extract_section_text(
    content: str, start_anchor: str, end_anchor: str | None = None
) -> str | None:
    """提取从 start_anchor 到 end_anchor（或下一个 ## 标题）的文本

    Args:
        content: 文档全文
        start_anchor: 起始锚点
        end_anchor: 结束锚点（可选，如果为 None 则到下一个 ## 标题）

    Returns:
        提取的文本，如果找不到锚点则返回 None
    """
    start_pos = content.find(start_anchor)
    if start_pos == -1:
        return None

    if end_anchor:
        end_pos = content.find(end_anchor, start_pos + len(start_anchor))
    else:
        # 查找下一个 ## 或 ### 标题
        next_h2 = content.find("\n## ", start_pos + len(start_anchor))
        end_pos = next_h2 if next_h2 != -1 else len(content)

    return content[start_pos:end_pos]


def parse_doc_reasons(content: str) -> list[str]:
    """从文档中解析 error_reason 列表

    解析 3.1-3.5 节表格中的原因码（第一列）

    Args:
        content: 文档全文

    Returns:
        error_reason 列表
    """
    reasons: list[str] = []

    # 遍历各个 reason 章节
    for anchor in DOC_REASON_SECTION_ANCHORS:
        section_text = extract_section_text(content, anchor)
        if not section_text:
            continue

        # 解析表格中的原因码
        # 表格格式: || 原因码 | 说明 | 可重试 |
        # 匹配类似 `PARSE_ERROR` 的模式（表格中的第一列）
        pattern = r"\|\s*`([A-Z_]+)`\s*\|"
        matches = re.findall(pattern, section_text)
        reasons.extend(matches)

    return reasons


def parse_doc_categories(content: str) -> list[str]:
    """从文档中解析 error_category 列表

    从 TypeScript 接口定义中提取 category 可选值

    Args:
        content: 文档全文

    Returns:
        error_category 列表
    """
    section_text = extract_section_text(content, DOC_ERROR_DATA_SECTION_ANCHOR)
    if not section_text:
        return []

    # 匹配 TypeScript union type: "protocol" | "validation" | "business" | ...
    pattern = r'category:\s*"([^"]+)"(?:\s*\|\s*"([^"]+)")*'
    match = re.search(pattern, section_text)
    if not match:
        # 尝试更宽松的匹配 - 查找所有引号内的 category 值
        # interface ErrorData 中的 category 行
        pattern = r'"(protocol|validation|business|dependency|internal)"'
        matches = re.findall(pattern, section_text)
        return list(set(matches))

    # 从完整匹配中解析所有 category 值
    full_match = match.group(0)
    pattern = r'"([^"]+)"'
    categories = re.findall(pattern, full_match)

    return list(set(categories))


def parse_doc_jsonrpc_codes(content: str) -> list[int]:
    """从文档中解析 JSON-RPC 错误码列表

    从第 4 节表格中提取错误码

    Args:
        content: 文档全文

    Returns:
        JSON-RPC 错误码列表
    """
    section_text = extract_section_text(content, DOC_JSONRPC_CODE_SECTION_ANCHOR)
    if not section_text:
        return []

    # 匹配表格中的错误码（负整数）
    # 格式: || -32700 | PARSE_ERROR | protocol |
    pattern = r"\|\s*(-\d+)\s*\|"
    matches = re.findall(pattern, section_text)

    # 过滤掉 -32000（已废弃）
    codes = []
    for code_str in matches:
        code = int(code_str)
        # 检查是否被标记为废弃
        if code == -32000:
            # 检查文档中是否标记为废弃
            if "废弃" in section_text or "deprecated" in section_text.lower():
                continue
        codes.append(code)

    return codes


def load_schema(schema_path: Path) -> dict[str, Any] | None:
    """加载 JSON Schema 文件

    Args:
        schema_path: Schema 文件路径

    Returns:
        解析后的 JSON 对象，加载失败返回 None
    """
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error loading schema: {e}", file=sys.stderr)
        return None


def parse_schema_reasons(schema: dict[str, Any]) -> list[str]:
    """从 Schema 中解析 error_reason 列表"""
    return schema.get("definitions", {}).get("error_reason", {}).get("enum", [])


def parse_schema_categories(schema: dict[str, Any]) -> list[str]:
    """从 Schema 中解析 error_category 列表"""
    return schema.get("definitions", {}).get("error_category", {}).get("enum", [])


def parse_schema_codes(schema: dict[str, Any]) -> list[int]:
    """从 Schema 中解析 JSON-RPC 错误码列表"""
    return (
        schema.get("definitions", {})
        .get("jsonrpc_error", {})
        .get("properties", {})
        .get("code", {})
        .get("enum", [])
    )


# ============================================================================
# Core Logic
# ============================================================================


class McpErrorDocsSyncChecker:
    """MCP 错误码文档与 Schema 同步校验器"""

    def __init__(
        self,
        doc_path: Path,
        schema_path: Path,
        verbose: bool = False,
    ) -> None:
        self.doc_path = doc_path
        self.schema_path = schema_path
        self.verbose = verbose
        self.result = SyncResult()
        self.doc_content: str = ""
        self.schema: dict[str, Any] = {}

    def load_files(self) -> bool:
        """加载文档和 Schema 文件"""
        # 加载文档
        if not self.doc_path.exists():
            self.result.add_error(
                SyncError(
                    error_type="file_not_found",
                    category="file",
                    value=str(self.doc_path),
                    message=f"Documentation file not found: {self.doc_path}",
                )
            )
            return False

        try:
            with open(self.doc_path, "r", encoding="utf-8") as f:
                self.doc_content = f.read()
        except Exception as e:
            self.result.add_error(
                SyncError(
                    error_type="file_read_error",
                    category="file",
                    value=str(self.doc_path),
                    message=f"Failed to read documentation: {e}",
                )
            )
            return False

        # 加载 Schema
        if not self.schema_path.exists():
            self.result.add_error(
                SyncError(
                    error_type="file_not_found",
                    category="file",
                    value=str(self.schema_path),
                    message=f"Schema file not found: {self.schema_path}",
                )
            )
            return False

        schema = load_schema(self.schema_path)
        if schema is None:
            self.result.add_error(
                SyncError(
                    error_type="file_parse_error",
                    category="file",
                    value=str(self.schema_path),
                    message="Failed to parse JSON Schema",
                )
            )
            return False

        self.schema = schema
        return True

    def check_error_reasons(self) -> None:
        """校验 error_reason 同步"""
        schema_reasons = set(parse_schema_reasons(self.schema))
        doc_reasons = set(parse_doc_reasons(self.doc_content))

        self.result.schema_reasons = sorted(schema_reasons)
        self.result.doc_reasons = sorted(doc_reasons)

        if self.verbose:
            print(f"\n[error_reason] Schema: {len(schema_reasons)}, Doc: {len(doc_reasons)}")

        # 检查 Schema 中有但文档中没有的
        missing_in_doc = schema_reasons - doc_reasons
        for reason in sorted(missing_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="missing_in_doc",
                    category="error_reason",
                    value=reason,
                    message=f"Reason '{reason}' exists in schema but not documented",
                    fix_suggestion=f"Add `{reason}` to the appropriate section (3.1-3.5) in {self.doc_path.name}",
                )
            )

        # 检查文档中有但 Schema 中没有的
        extra_in_doc = doc_reasons - schema_reasons
        for reason in sorted(extra_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="extra_in_doc",
                    category="error_reason",
                    value=reason,
                    message=f"Reason '{reason}' documented but not in schema",
                    fix_suggestion=f"Add '{reason}' to definitions.error_reason.enum in {self.schema_path.name}",
                )
            )

        if self.verbose and not missing_in_doc and not extra_in_doc:
            print("  [OK] All error_reason values are in sync")

    def check_error_categories(self) -> None:
        """校验 error_category 同步"""
        schema_categories = set(parse_schema_categories(self.schema))
        doc_categories = set(parse_doc_categories(self.doc_content))

        self.result.schema_categories = sorted(schema_categories)
        self.result.doc_categories = sorted(doc_categories)

        if self.verbose:
            print(
                f"\n[error_category] Schema: {len(schema_categories)}, Doc: {len(doc_categories)}"
            )

        # 检查 Schema 中有但文档中没有的
        missing_in_doc = schema_categories - doc_categories
        for category in sorted(missing_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="missing_in_doc",
                    category="error_category",
                    value=category,
                    message=f"Category '{category}' exists in schema but not in TypeScript interface",
                    fix_suggestion=f"Add '{category}' to the ErrorData interface in section 2",
                )
            )

        # 检查文档中有但 Schema 中没有的
        extra_in_doc = doc_categories - schema_categories
        for category in sorted(extra_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="extra_in_doc",
                    category="error_category",
                    value=category,
                    message=f"Category '{category}' in TypeScript interface but not in schema",
                    fix_suggestion=f"Add '{category}' to definitions.error_category.enum in {self.schema_path.name}",
                )
            )

        if self.verbose and not missing_in_doc and not extra_in_doc:
            print("  [OK] All error_category values are in sync")

    def check_jsonrpc_codes(self) -> None:
        """校验 JSON-RPC 错误码同步"""
        schema_codes = set(parse_schema_codes(self.schema))
        doc_codes = set(parse_doc_jsonrpc_codes(self.doc_content))

        self.result.schema_codes = sorted(schema_codes)
        self.result.doc_codes = sorted(doc_codes)

        if self.verbose:
            print(f"\n[jsonrpc_code] Schema: {len(schema_codes)}, Doc: {len(doc_codes)}")

        # 检查 Schema 中有但文档中没有的
        missing_in_doc = schema_codes - doc_codes
        for code in sorted(missing_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="missing_in_doc",
                    category="jsonrpc_code",
                    value=str(code),
                    message=f"JSON-RPC code {code} exists in schema but not documented in section 4",
                    fix_suggestion=f"Add {code} to the error code table in section 4",
                )
            )

        # 检查文档中有但 Schema 中没有的
        extra_in_doc = doc_codes - schema_codes
        for code in sorted(extra_in_doc):
            self.result.add_error(
                SyncError(
                    error_type="extra_in_doc",
                    category="jsonrpc_code",
                    value=str(code),
                    message=f"JSON-RPC code {code} documented but not in schema",
                    fix_suggestion=f"Add {code} to definitions.jsonrpc_error.properties.code.enum in {self.schema_path.name}",
                )
            )

        if self.verbose and not missing_in_doc and not extra_in_doc:
            print("  [OK] All jsonrpc_code values are in sync")

    def check(self) -> SyncResult:
        """执行完整校验"""
        if self.verbose:
            print(f"Loading documentation: {self.doc_path}")
            print(f"Loading schema: {self.schema_path}")

        if not self.load_files():
            return self.result

        if self.verbose:
            print("\nChecking error_reason sync...")
        self.check_error_reasons()

        if self.verbose:
            print("\nChecking error_category sync...")
        self.check_error_categories()

        if self.verbose:
            print("\nChecking jsonrpc_code sync...")
        self.check_jsonrpc_codes()

        return self.result


# ============================================================================
# Output Formatting
# ============================================================================


def format_text_output(result: SyncResult) -> str:
    """格式化文本输出"""
    lines: list[str] = []

    if result.success:
        lines.append("=" * 60)
        lines.append("MCP JSON-RPC Error Docs Sync Check: PASSED")
        lines.append("=" * 60)
    else:
        lines.append("=" * 60)
        lines.append("MCP JSON-RPC Error Docs Sync Check: FAILED")
        lines.append("=" * 60)

    lines.append("")
    lines.append("Summary:")
    lines.append(f"  - Schema error_reason: {len(result.schema_reasons)}")
    lines.append(f"  - Doc error_reason: {len(result.doc_reasons)}")
    lines.append(f"  - Schema error_category: {len(result.schema_categories)}")
    lines.append(f"  - Doc error_category: {len(result.doc_categories)}")
    lines.append(f"  - Schema jsonrpc_code: {len(result.schema_codes)}")
    lines.append(f"  - Doc jsonrpc_code: {len(result.doc_codes)}")
    lines.append(f"  - Errors: {len(result.errors)}")
    lines.append(f"  - Warnings: {len(result.warnings)}")

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for error in result.errors:
            lines.append(f"  [{error.error_type}] {error.category}: {error.value}")
            lines.append(f"    {error.message}")
            if error.fix_suggestion:
                lines.append(f"    Fix: {error.fix_suggestion}")

    if result.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in result.warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def format_json_output(result: SyncResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "schema_error_reasons": result.schema_reasons,
        "doc_error_reasons": result.doc_reasons,
        "schema_error_categories": result.schema_categories,
        "doc_error_categories": result.doc_categories,
        "schema_jsonrpc_codes": result.schema_codes,
        "doc_jsonrpc_codes": result.doc_codes,
        "errors": [
            {
                "error_type": e.error_type,
                "category": e.category,
                "value": e.value,
                "message": e.message,
                "fix_suggestion": e.fix_suggestion,
            }
            for e in result.errors
        ],
        "warnings": result.warnings,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 mcp_jsonrpc_error_v1.md 与 mcp_jsonrpc_error_v1.schema.json 的同步一致性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--doc",
        type=str,
        default=DEFAULT_DOC_PATH,
        help=f"Documentation Markdown 文件路径 (default: {DEFAULT_DOC_PATH})",
    )
    parser.add_argument(
        "--schema",
        type=str,
        default=DEFAULT_SCHEMA_PATH,
        help=f"JSON Schema 文件路径 (default: {DEFAULT_SCHEMA_PATH})",
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

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = Path(args.project_root)
    else:
        # 尝试从脚本位置推断项目根目录
        script_path = Path(__file__).resolve()
        # scripts/ci/check_mcp_jsonrpc_error_docs_sync.py -> project_root
        project_root = script_path.parent.parent.parent
        if not (project_root / "scripts" / "ci").exists():
            # 回退到当前工作目录
            project_root = Path.cwd()

    doc_path = project_root / args.doc
    schema_path = project_root / args.schema

    if args.verbose and not args.json:
        print(f"Project root: {project_root}")
        print(f"Doc path: {doc_path}")
        print(f"Schema path: {schema_path}")
        print()

    checker = McpErrorDocsSyncChecker(
        doc_path=doc_path,
        schema_path=schema_path,
        verbose=args.verbose and not args.json,
    )

    result = checker.check()

    if args.json:
        print(format_json_output(result))
    else:
        print(format_text_output(result))

    # 返回退出码
    if result.errors:
        # 区分文件错误和同步错误
        file_errors = [e for e in result.errors if e.category == "file"]
        if file_errors:
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
