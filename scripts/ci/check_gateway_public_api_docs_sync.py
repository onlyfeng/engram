#!/usr/bin/env python3
"""
Gateway Public API 文档同步检查脚本

检查 src/engram/gateway/public_api.py 中的 __all__ 与
docs/architecture/gateway_public_api_surface.md 文档是否同步。

功能：
1. AST 解析 public_api.py 提取 __all__ 符号列表
2. 解析文档中的导出项表格，提取符号列表
3. 比较两者差异，输出 missing/extra 信息
4. 支持 --json/--verbose 参数

用法:
    python scripts/ci/check_gateway_public_api_docs_sync.py [--verbose] [--json]

退出码:
    0 - 检查通过
    1 - 发现差异（missing 或 extra）
    2 - 文件读取/解析错误

相关文档:
    - docs/architecture/gateway_public_api_surface.md
    - src/engram/gateway/public_api.py
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

# ============================================================================
# 配置区
# ============================================================================

# Public API 文件路径（相对于项目根）
PUBLIC_API_PATH = Path("src/engram/gateway/public_api.py")

# 文档路径（相对于项目根）
DOC_PATH = Path("docs/architecture/gateway_public_api_surface.md")


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class SyncResult:
    """同步校验结果"""

    code_symbols: List[str] = field(default_factory=list)  # 代码中的 __all__
    doc_symbols: List[str] = field(default_factory=list)  # 文档中提取的符号
    missing_in_doc: List[str] = field(default_factory=list)  # 在代码中但不在文档中
    extra_in_doc: List[str] = field(default_factory=list)  # 在文档中但不在代码中
    parse_errors: List[str] = field(default_factory=list)  # 解析错误

    def is_synced(self) -> bool:
        """检查是否同步"""
        return (
            len(self.missing_in_doc) == 0
            and len(self.extra_in_doc) == 0
            and len(self.parse_errors) == 0
        )

    def has_parse_errors(self) -> bool:
        """是否有解析错误"""
        return len(self.parse_errors) > 0

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "ok": self.is_synced(),
            "code_symbol_count": len(self.code_symbols),
            "doc_symbol_count": len(self.doc_symbols),
            "missing_in_doc_count": len(self.missing_in_doc),
            "extra_in_doc_count": len(self.extra_in_doc),
            "parse_error_count": len(self.parse_errors),
            "code_symbols": sorted(self.code_symbols),
            "doc_symbols": sorted(self.doc_symbols),
            "missing_in_doc": sorted(self.missing_in_doc),
            "extra_in_doc": sorted(self.extra_in_doc),
            "parse_errors": self.parse_errors,
        }


# ============================================================================
# AST 解析逻辑
# ============================================================================


def extract_all_from_code(file_path: Path) -> tuple[List[str], List[str]]:
    """从 Python 文件中提取 __all__ 列表

    Args:
        file_path: Python 文件路径

    Returns:
        (符号列表, 错误列表)
    """
    errors: List[str] = []
    symbols: List[str] = []

    if not file_path.exists():
        errors.append(f"文件不存在: {file_path}")
        return symbols, errors

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(f"无法读取文件 {file_path}: {e}")
        return symbols, errors

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as e:
        errors.append(f"语法错误 {file_path}: {e}")
        return symbols, errors

    # 遍历 AST 查找 __all__ 定义
    for node in ast.walk(tree):
        # 处理普通赋值: __all__ = [...]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    symbols = _extract_list_elements(node.value)
                    break
        # 处理带类型注解的赋值: __all__: list[str] = [...]
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                if node.value:
                    symbols = _extract_list_elements(node.value)
                break

    if not symbols:
        errors.append(f"未找到 __all__ 定义: {file_path}")

    return symbols, errors


def _extract_list_elements(node: ast.expr) -> List[str]:
    """从 AST 列表节点提取字符串元素"""
    elements: List[str] = []
    if isinstance(node, ast.List):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                elements.append(elt.value)
    return elements


# ============================================================================
# 文档解析逻辑
# ============================================================================

# 标记区块的起始和结束标记
EXPORT_BLOCK_START = "<!-- public_api_exports:start -->"
EXPORT_BLOCK_END = "<!-- public_api_exports:end -->"


def extract_symbols_from_doc(file_path: Path) -> tuple[List[str], List[str]]:
    """从文档中提取导出项符号列表

    解析策略：
    仅在 <!-- public_api_exports:start --> 和 <!-- public_api_exports:end -->
    标记区块内解析表格，提取第一列的符号名称。

    Args:
        file_path: Markdown 文档路径

    Returns:
        (符号列表, 错误列表)
    """
    errors: List[str] = []
    symbols: Set[str] = set()

    if not file_path.exists():
        errors.append(f"文档不存在: {file_path}")
        return [], errors

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(f"无法读取文档 {file_path}: {e}")
        return [], errors

    # 查找标记区块
    block_content, block_errors = _extract_marked_block(content)
    if block_errors:
        errors.extend(block_errors)
        return [], errors

    if block_content is None:
        errors.append(
            f"未找到导出表标记区块: {file_path}\n"
            f"请在文档中添加 {EXPORT_BLOCK_START} 和 {EXPORT_BLOCK_END} 标记"
        )
        return [], errors

    # 从标记区块内的表格中提取符号
    symbols = _extract_symbols_from_block(block_content)

    if not symbols:
        errors.append(f"标记区块内未找到任何导出项符号: {file_path}")

    return sorted(list(symbols)), errors


def _extract_marked_block(content: str) -> tuple[str | None, List[str]]:
    """提取标记区块内的内容

    Args:
        content: 完整文档内容

    Returns:
        (区块内容, 错误列表)
        如果未找到标记，返回 (None, [])
        如果标记不完整，返回 (None, [错误信息])
    """
    errors: List[str] = []

    start_idx = content.find(EXPORT_BLOCK_START)
    end_idx = content.find(EXPORT_BLOCK_END)

    # 检查标记是否存在
    if start_idx == -1 and end_idx == -1:
        # 两个标记都不存在
        return None, []

    if start_idx == -1:
        errors.append(f"找到结束标记但缺少起始标记 {EXPORT_BLOCK_START}")
        return None, errors

    if end_idx == -1:
        errors.append(f"找到起始标记但缺少结束标记 {EXPORT_BLOCK_END}")
        return None, errors

    if end_idx <= start_idx:
        errors.append("结束标记出现在起始标记之前")
        return None, errors

    # 提取区块内容（不包含标记本身）
    block_start = start_idx + len(EXPORT_BLOCK_START)
    block_content = content[block_start:end_idx].strip()

    return block_content, []


def _extract_symbols_from_block(block_content: str) -> Set[str]:
    """从标记区块内的表格中提取符号

    只提取表格第一列中用反引号包裹的符号名。
    跳过 Tier 标识行（如 **Tier A**）和表头行。
    """
    symbols: Set[str] = set()

    # 跳过的关键字
    skip_keywords = {
        # 表头
        "导出项",
        "类型",
        "说明",
        "依赖",
        "import-time",
        "外部包",
        "符号",
        # Tier 标识
        "tier",
        "a",
        "b",
        "c",
    }

    # 匹配表格行第一列中的反引号符号: | `SymbolName` |
    # 仅匹配以大写或小写字母开头的标识符
    row_pattern = re.compile(r"^\|\s*`([a-zA-Z][a-zA-Z0-9_]*)`\s*\|", re.MULTILINE)

    for match in row_pattern.finditer(block_content):
        symbol = match.group(1)
        # 跳过关键字
        if symbol.lower() in skip_keywords:
            continue
        symbols.add(symbol)

    return symbols


# ============================================================================
# 主检查逻辑
# ============================================================================


def check_docs_sync(
    code_path: Path,
    doc_path: Path,
) -> SyncResult:
    """检查代码与文档的同步状态

    Args:
        code_path: public_api.py 文件路径
        doc_path: 文档路径

    Returns:
        同步检查结果
    """
    result = SyncResult()

    # 提取代码中的符号
    code_symbols, code_errors = extract_all_from_code(code_path)
    result.code_symbols = code_symbols
    result.parse_errors.extend(code_errors)

    # 提取文档中的符号
    doc_symbols, doc_errors = extract_symbols_from_doc(doc_path)
    result.doc_symbols = doc_symbols
    result.parse_errors.extend(doc_errors)

    # 如果有解析错误，提前返回
    if result.has_parse_errors():
        return result

    # 计算差异
    code_set = set(code_symbols)
    doc_set = set(doc_symbols)

    result.missing_in_doc = sorted(list(code_set - doc_set))
    result.extra_in_doc = sorted(list(doc_set - code_set))

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 Gateway public_api.py 的 __all__ 与文档是否同步",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细信息",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )
    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent  # scripts/ci/ 的父父目录

    code_path = project_root / PUBLIC_API_PATH
    doc_path = project_root / DOC_PATH

    # 执行检查
    result = check_docs_sync(code_path, doc_path)

    # 输出结果
    if args.json:
        output = result.to_dict()
        output["code_file"] = str(PUBLIC_API_PATH)
        output["doc_file"] = str(DOC_PATH)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Gateway Public API 文档同步检查")
        print("=" * 70)
        print()
        print(f"代码文件: {code_path}")
        print(f"文档文件: {doc_path}")
        print()

        # 显示解析结果
        print("解析结果:")
        print(f"  代码 __all__ 符号数: {len(result.code_symbols)}")
        print(f"  文档导出项符号数: {len(result.doc_symbols)}")
        print()

        if result.has_parse_errors():
            print("解析错误:")
            for error in result.parse_errors:
                print(f"  [ERROR] {error}")
            print()

        # 显示差异
        if result.missing_in_doc:
            print(f"代码中有但文档中缺失 ({len(result.missing_in_doc)}):")
            for symbol in result.missing_in_doc:
                print(f"  - {symbol}")
            print()

        if result.extra_in_doc:
            print(f"文档中有但代码中缺失 ({len(result.extra_in_doc)}):")
            for symbol in result.extra_in_doc:
                print(f"  - {symbol}")
            print()

        if args.verbose:
            print("代码 __all__ 符号列表:")
            for symbol in sorted(result.code_symbols):
                in_doc = "✓" if symbol in result.doc_symbols else "✗"
                print(f"  [{in_doc}] {symbol}")
            print()

        print("-" * 70)
        if result.is_synced():
            print("[OK] Gateway Public API 文档同步检查通过")
        else:
            print("[FAIL] Gateway Public API 文档同步检查失败")
            print()
            print("修复指南:")
            if result.missing_in_doc:
                print("  - 在文档的导出项表格中添加缺失的符号")
            if result.extra_in_doc:
                print("  - 从文档中移除不再导出的符号，或在代码中添加到 __all__")
            print("  - 参见: docs/architecture/gateway_public_api_surface.md")

    # 返回退出码
    if result.has_parse_errors():
        return 2
    return 0 if result.is_synced() else 1


if __name__ == "__main__":
    sys.exit(main())
