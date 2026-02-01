#!/usr/bin/env python3
"""
Gateway Import Surface 检查脚本

检查 src/engram/gateway/__init__.py 不包含 eager-import，确保懒加载策略正确实现。

禁止的导入模式（在 TYPE_CHECKING 块外）：
- from . import logbook_adapter
- from . import openmemory_client
- from . import outbox_worker
- from .logbook_adapter import ...
- from .openmemory_client import ...
- from .outbox_worker import ...
- import engram.gateway.logbook_adapter
- import engram.gateway.openmemory_client
- import engram.gateway.outbox_worker

设计原则：
- engram.gateway.__init__.py 应使用 __getattr__ 实现懒加载
- TYPE_CHECKING 块内的导入仅用于静态类型提示，不触发实际导入
- 这确保 `import engram.gateway` 不会加载重量级子模块

用法:
    python scripts/ci/check_gateway_import_surface.py [--verbose] [--json]

退出码:
    0 - 检查通过
    1 - 发现 eager-import 违规

相关文档:
    - docs/architecture/gateway_module_boundaries.md
    - docs/architecture/adr_gateway_di_and_entry_boundary.md
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

# ============================================================================
# 配置区
# ============================================================================

# Gateway __init__.py 文件路径（相对于项目根）
GATEWAY_INIT_PATH = Path("src/engram/gateway/__init__.py")

# 禁止 eager-import 的子模块列表
# 这些模块应通过 __getattr__ 懒加载
LAZY_SUBMODULES: Set[str] = {
    "logbook_adapter",
    "openmemory_client",
    "outbox_worker",
}

# 完整的模块名称前缀
GATEWAY_MODULE_PREFIX = "engram.gateway"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class EagerImportViolation:
    """单个 eager-import 违规记录"""

    line_number: int
    line_content: str
    submodule: str
    import_type: str  # "from_import" 或 "import"
    message: str


@dataclass
class CheckResult:
    """检查结果"""

    violations: List[EagerImportViolation] = field(default_factory=list)
    has_type_checking_guard: bool = False
    has_getattr_lazy_load: bool = False

    def has_violations(self) -> bool:
        return len(self.violations) > 0

    def to_dict(self) -> dict:
        return {
            "ok": not self.has_violations(),
            "violation_count": len(self.violations),
            "has_type_checking_guard": self.has_type_checking_guard,
            "has_getattr_lazy_load": self.has_getattr_lazy_load,
            "violations": [
                {
                    "line_number": v.line_number,
                    "line_content": v.line_content.strip(),
                    "submodule": v.submodule,
                    "import_type": v.import_type,
                    "message": v.message,
                }
                for v in self.violations
            ],
        }


# ============================================================================
# AST 扫描逻辑
# ============================================================================


class ImportSurfaceChecker(ast.NodeVisitor):
    """AST 访问者，检查 eager-import 违规"""

    def __init__(self, source_lines: List[str]) -> None:
        self.source_lines = source_lines
        self.violations: List[EagerImportViolation] = []
        self.in_type_checking_block = False
        self.has_type_checking_guard = False
        self.has_getattr_lazy_load = False

    def visit_If(self, node: ast.If) -> None:
        """检测 if TYPE_CHECKING: 块"""
        # 检查是否是 TYPE_CHECKING 条件
        is_type_checking = False

        if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            is_type_checking = True
        elif isinstance(node.test, ast.Attribute):
            # 处理 typing.TYPE_CHECKING
            if (
                isinstance(node.test.value, ast.Name)
                and node.test.value.id == "typing"
                and node.test.attr == "TYPE_CHECKING"
            ):
                is_type_checking = True

        if is_type_checking:
            self.has_type_checking_guard = True
            # 标记进入 TYPE_CHECKING 块
            old_state = self.in_type_checking_block
            self.in_type_checking_block = True
            # 访问 if 块内的节点
            for child in node.body:
                self.visit(child)
            self.in_type_checking_block = old_state
            # 访问 else 块（如果有）
            for child in node.orelse:
                self.visit(child)
        else:
            # 正常访问
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """检测 __getattr__ 函数定义"""
        if node.name == "__getattr__":
            self.has_getattr_lazy_load = True
        # 不检查函数内部的导入（函数内是延迟执行的）
        # 所以不调用 generic_visit

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """检查 from ... import ... 语句"""
        if self.in_type_checking_block:
            # TYPE_CHECKING 块内允许
            return

        # 检查 from . import submodule
        if node.module is None and node.level == 1:
            for alias in node.names:
                if alias.name in LAZY_SUBMODULES:
                    self._add_violation(node, alias.name, "from_import")

        # 检查 from .submodule import ...
        elif node.level == 1 and node.module in LAZY_SUBMODULES:
            self._add_violation(node, node.module, "from_import")

        # 检查 from engram.gateway.submodule import ...
        elif node.module and node.module.startswith(GATEWAY_MODULE_PREFIX + "."):
            submodule = node.module[len(GATEWAY_MODULE_PREFIX) + 1 :].split(".")[0]
            if submodule in LAZY_SUBMODULES:
                self._add_violation(node, submodule, "from_import")

    def visit_Import(self, node: ast.Import) -> None:
        """检查 import ... 语句"""
        if self.in_type_checking_block:
            return

        for alias in node.names:
            # 检查 import engram.gateway.submodule
            if alias.name.startswith(GATEWAY_MODULE_PREFIX + "."):
                submodule = alias.name[len(GATEWAY_MODULE_PREFIX) + 1 :].split(".")[0]
                if submodule in LAZY_SUBMODULES:
                    self._add_violation(node, submodule, "import")

    def _add_violation(self, node: ast.AST, submodule: str, import_type: str) -> None:
        """添加违规记录"""
        line_number = node.lineno
        line_content = (
            self.source_lines[line_number - 1] if line_number <= len(self.source_lines) else ""
        )

        self.violations.append(
            EagerImportViolation(
                line_number=line_number,
                line_content=line_content,
                submodule=submodule,
                import_type=import_type,
                message=(
                    f"禁止 eager-import '{submodule}'，"
                    f"应使用 __getattr__ 懒加载或仅在 TYPE_CHECKING 块内导入"
                ),
            )
        )


def check_gateway_import_surface(file_path: Path) -> CheckResult:
    """检查 Gateway __init__.py 的 import surface

    Args:
        file_path: Gateway __init__.py 文件路径

    Returns:
        检查结果
    """
    result = CheckResult()

    if not file_path.exists():
        # 文件不存在，返回空结果
        return result

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[ERROR] 无法读取文件 {file_path}: {e}", file=sys.stderr)
        return result

    source_lines = content.splitlines()

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as e:
        print(f"[ERROR] 语法错误 {file_path}: {e}", file=sys.stderr)
        return result

    checker = ImportSurfaceChecker(source_lines)
    checker.visit(tree)

    result.violations = checker.violations
    result.has_type_checking_guard = checker.has_type_checking_guard
    result.has_getattr_lazy_load = checker.has_getattr_lazy_load

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 Gateway __init__.py 的 import surface，确保不包含 eager-import"
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

    file_path = project_root / GATEWAY_INIT_PATH

    if not file_path.exists():
        print(f"[ERROR] 文件不存在: {file_path}", file=sys.stderr)
        return 1

    # 执行检查
    result = check_gateway_import_surface(file_path)

    # 输出结果
    if args.json:
        output = result.to_dict()
        output["file"] = str(GATEWAY_INIT_PATH)
        output["lazy_submodules"] = sorted(LAZY_SUBMODULES)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Gateway Import Surface 检查")
        print("=" * 70)
        print()
        print(f"检查文件: {file_path}")
        print(f"懒加载子模块: {', '.join(sorted(LAZY_SUBMODULES))}")
        print()

        # 显示懒加载策略检测结果
        print("懒加载策略检测:")
        if result.has_type_checking_guard:
            print("  [OK] 检测到 TYPE_CHECKING 块（静态类型提示）")
        else:
            print("  [WARN] 未检测到 TYPE_CHECKING 块")

        if result.has_getattr_lazy_load:
            print("  [OK] 检测到 __getattr__ 懒加载函数")
        else:
            print("  [WARN] 未检测到 __getattr__ 懒加载函数")
        print()

        if not result.has_violations():
            print("[OK] 未发现 eager-import 违规")
        else:
            print(f"[ERROR] 发现 {len(result.violations)} 处 eager-import 违规:")
            print()
            for v in result.violations:
                print(f"  第 {v.line_number} 行: {v.line_content.strip()}")
                print(f"    子模块: {v.submodule}")
                print(f"    类型: {v.import_type}")
                if args.verbose:
                    print(f"    说明: {v.message}")
                print()

        print("-" * 70)
        print(f"违规总数: {len(result.violations)}")
        print()

        if result.has_violations():
            print("[FAIL] Gateway import surface 检查失败")
            print()
            print("修复指南:")
            print("  - 将子模块导入移至 TYPE_CHECKING 块内（仅用于静态类型提示）")
            print("  - 使用 __getattr__ 实现运行时懒加载")
            print("  - 参见: docs/architecture/gateway_module_boundaries.md")
        else:
            print("[OK] Gateway import surface 检查通过")

    return 1 if result.has_violations() else 0


if __name__ == "__main__":
    sys.exit(main())
