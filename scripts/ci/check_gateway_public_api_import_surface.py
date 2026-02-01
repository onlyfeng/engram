#!/usr/bin/env python3
"""
Gateway Public API Import Surface 检查脚本

检查 src/engram/gateway/public_api.py 不包含违规的导入，确保：
1. 只有 allowlist 中的模块可以直接导入（Tier A）
2. Tier B 模块必须通过 __getattr__ 懒加载或在 TYPE_CHECKING 块内导入

禁止的导入模式（在 TYPE_CHECKING 块外且不在 __getattr__ 函数内）：
- from .<非白名单模块> import ...（例如 .container, .config, .middleware）
- import engram.gateway.<非白名单模块>
- Tier B 模块的 eager-import（必须懒加载）

允许的模式：
- TYPE_CHECKING 块内的导入（仅用于静态类型提示）
- __getattr__ 函数内使用 importlib 实现的懒加载
- allowlist 中模块的直接导入（Tier A: di, error_codes, result_error_codes, services.ports）

设计原则：
- public_api.py 是插件作者的稳定 API 入口
- Tier A 符号可以直接导入（核心类型、错误码、Protocol）
- Tier B 符号必须通过 __getattr__ 懒加载（LogbookAdapter, execute_tool 等）
- TYPE_CHECKING 块内的导入仅用于静态类型提示，不触发实际导入
- 非 allowlist 模块（如 container, config）禁止在 public_api.py 中导入

用法:
    python scripts/ci/check_gateway_public_api_import_surface.py [--verbose] [--json]

退出码:
    0 - 检查通过
    1 - 发现导入违规

相关文档:
    - docs/architecture/gateway_public_api_surface.md
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

# Public API 文件路径（相对于项目根）
PUBLIC_API_PATH = Path("src/engram/gateway/public_api.py")

# 允许直接导入的相对模块 allowlist（Tier A）
# 这些模块包含核心类型、错误码、Protocol 等，不依赖重量级组件
ALLOWED_RELATIVE_IMPORTS: Set[str] = {
    "di",
    "error_codes",
    "result_error_codes",
    "services.ports",
}

# Tier B 模块列表 - 必须通过 __getattr__ 懒加载
# 这些模块依赖重量级组件（engram_logbook, MCP 等）
TIER_B_MODULES: Set[str] = {
    "logbook_adapter",
    "mcp_rpc",
    "entrypoints",
}

# Tier B 子模块路径（用于检查 from .entrypoints.tool_executor import ...）
TIER_B_SUBMODULE_PATHS: Set[str] = {
    "entrypoints.tool_executor",
}

# 完整的模块名称前缀
GATEWAY_MODULE_PREFIX = "engram.gateway"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class ImportViolation:
    """单个导入违规记录"""

    line_number: int
    line_content: str
    module_name: str
    import_type: str  # "from_import" 或 "import"
    violation_type: str  # "not_in_allowlist" 或 "tier_b_eager_import"
    message: str


@dataclass
class AllConsistencyResult:
    """__all__ 与 _TIER_B_LAZY_IMPORTS 一致性校验结果"""

    all_symbols: List[str] = field(default_factory=list)
    tier_b_keys: List[str] = field(default_factory=list)
    missing_in_all: List[str] = field(
        default_factory=list
    )  # 在 _TIER_B_LAZY_IMPORTS 但不在 __all__

    def is_consistent(self) -> bool:
        return len(self.missing_in_all) == 0


@dataclass
class InstallHintConsistencyResult:
    """_TIER_B_LAZY_IMPORTS 与 _TIER_B_INSTALL_HINTS 一致性校验结果

    确保每个 module_path 都有对应的 install_hint（按 module_path 必填策略）
    """

    tier_b_module_paths: List[str] = field(default_factory=list)  # _TIER_B_LAZY_IMPORTS 中的 module_path
    install_hint_keys: List[str] = field(default_factory=list)  # _TIER_B_INSTALL_HINTS 的 key
    missing_install_hints: List[str] = field(
        default_factory=list
    )  # module_path 没有对应的 install_hint

    def is_consistent(self) -> bool:
        return len(self.missing_install_hints) == 0


@dataclass
class CheckResult:
    """检查结果"""

    violations: List[ImportViolation] = field(default_factory=list)
    has_type_checking_guard: bool = False
    has_getattr_lazy_load: bool = False
    has_tier_b_lazy_imports_mapping: bool = False
    has_tier_b_install_hints_mapping: bool = False
    all_consistency: AllConsistencyResult = field(default_factory=AllConsistencyResult)
    install_hint_consistency: InstallHintConsistencyResult = field(
        default_factory=InstallHintConsistencyResult
    )

    def has_violations(self) -> bool:
        return len(self.violations) > 0

    def has_consistency_errors(self) -> bool:
        return (
            not self.all_consistency.is_consistent()
            or not self.install_hint_consistency.is_consistent()
        )

    def get_allowlist_violations(self) -> List[ImportViolation]:
        """获取非 allowlist 模块的导入违规"""
        return [v for v in self.violations if v.violation_type == "not_in_allowlist"]

    def get_tier_b_violations(self) -> List[ImportViolation]:
        """获取 Tier B eager-import 违规"""
        return [v for v in self.violations if v.violation_type == "tier_b_eager_import"]

    def to_dict(self) -> dict:
        return {
            "ok": not self.has_violations() and not self.has_consistency_errors(),
            "violation_count": len(self.violations),
            "allowlist_violation_count": len(self.get_allowlist_violations()),
            "tier_b_violation_count": len(self.get_tier_b_violations()),
            "has_type_checking_guard": self.has_type_checking_guard,
            "has_getattr_lazy_load": self.has_getattr_lazy_load,
            "has_tier_b_lazy_imports_mapping": self.has_tier_b_lazy_imports_mapping,
            "has_tier_b_install_hints_mapping": self.has_tier_b_install_hints_mapping,
            "all_consistency": {
                "is_consistent": self.all_consistency.is_consistent(),
                "all_symbols": self.all_consistency.all_symbols,
                "tier_b_keys": self.all_consistency.tier_b_keys,
                "missing_in_all": self.all_consistency.missing_in_all,
            },
            "install_hint_consistency": {
                "is_consistent": self.install_hint_consistency.is_consistent(),
                "tier_b_module_paths": self.install_hint_consistency.tier_b_module_paths,
                "install_hint_keys": self.install_hint_consistency.install_hint_keys,
                "missing_install_hints": self.install_hint_consistency.missing_install_hints,
            },
            "violations": [
                {
                    "line_number": v.line_number,
                    "line_content": v.line_content.strip(),
                    "module_name": v.module_name,
                    "import_type": v.import_type,
                    "violation_type": v.violation_type,
                    "message": v.message,
                }
                for v in self.violations
            ],
        }


# ============================================================================
# AST 扫描逻辑
# ============================================================================


def _is_in_allowlist(module_name: str) -> bool:
    """检查模块是否在 allowlist 中（支持前缀匹配）"""
    # 完全匹配
    if module_name in ALLOWED_RELATIVE_IMPORTS:
        return True
    # 前缀匹配（例如 services.ports.something 允许，因为 services.ports 在白名单）
    for allowed in ALLOWED_RELATIVE_IMPORTS:
        if module_name.startswith(allowed + "."):
            return True
    return False


def _is_tier_b_module(module_name: str) -> bool:
    """检查模块是否是 Tier B 模块（需要懒加载）"""
    # 完全匹配
    if module_name in TIER_B_MODULES:
        return True
    # 子模块路径匹配
    if module_name in TIER_B_SUBMODULE_PATHS:
        return True
    # 前缀匹配（例如 entrypoints.tool_executor）
    for tier_b_mod in TIER_B_MODULES:
        if module_name.startswith(tier_b_mod + "."):
            return True
    return False


class PublicApiImportChecker(ast.NodeVisitor):
    """AST 访问者，检查 public_api.py 的导入违规"""

    def __init__(self, source_lines: List[str]) -> None:
        self.source_lines = source_lines
        self.violations: List[ImportViolation] = []
        self.in_type_checking_block = False
        self.in_getattr_function = False
        self.has_type_checking_guard = False
        self.has_getattr_lazy_load = False
        self.has_tier_b_lazy_imports_mapping = False
        self.has_tier_b_install_hints_mapping = False
        self.all_symbols: List[str] = []  # __all__ 中的符号列表
        self.tier_b_keys: List[str] = []  # _TIER_B_LAZY_IMPORTS 的 key 列表
        self.tier_b_module_paths: List[str] = []  # _TIER_B_LAZY_IMPORTS 的 module_path 列表（去重）
        self.install_hint_keys: List[str] = []  # _TIER_B_INSTALL_HINTS 的 key 列表

    def visit_If(self, node: ast.If) -> None:
        """检测 if TYPE_CHECKING: 块"""
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
            # __getattr__ 内的导入是延迟执行的，不检查
            # 标记进入 __getattr__ 函数
            old_state = self.in_getattr_function
            self.in_getattr_function = True
            # 访问函数体
            for child in node.body:
                self.visit(child)
            self.in_getattr_function = old_state
        # 其他函数不检查内部导入（函数内是延迟执行的）
        # 所以不调用 generic_visit

    def visit_Assign(self, node: ast.Assign) -> None:
        """检测 __all__ 列表、_TIER_B_LAZY_IMPORTS 和 _TIER_B_INSTALL_HINTS 映射表定义（普通赋值）"""
        for target in node.targets:
            if isinstance(target, ast.Name):
                if target.id == "_TIER_B_LAZY_IMPORTS":
                    self.has_tier_b_lazy_imports_mapping = True
                    self._extract_tier_b_keys_and_module_paths(node.value)
                elif target.id == "_TIER_B_INSTALL_HINTS":
                    self.has_tier_b_install_hints_mapping = True
                    self._extract_install_hint_keys(node.value)
                elif target.id == "__all__":
                    self._extract_all_symbols(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """检测 __all__ 列表、_TIER_B_LAZY_IMPORTS 和 _TIER_B_INSTALL_HINTS 映射表定义（带类型注解的赋值）"""
        if isinstance(node.target, ast.Name):
            if node.target.id == "_TIER_B_LAZY_IMPORTS" and node.value:
                self.has_tier_b_lazy_imports_mapping = True
                self._extract_tier_b_keys_and_module_paths(node.value)
            elif node.target.id == "_TIER_B_INSTALL_HINTS" and node.value:
                self.has_tier_b_install_hints_mapping = True
                self._extract_install_hint_keys(node.value)
            elif node.target.id == "__all__" and node.value:
                self._extract_all_symbols(node.value)
        self.generic_visit(node)

    def _extract_all_symbols(self, node: ast.expr) -> None:
        """从 AST 节点提取 __all__ 中的字符串常量"""
        if isinstance(node, ast.List):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    self.all_symbols.append(elt.value)

    def _extract_tier_b_keys_and_module_paths(self, node: ast.expr) -> None:
        """从 AST 节点提取 _TIER_B_LAZY_IMPORTS 的 key 和 module_path

        _TIER_B_LAZY_IMPORTS 格式为:
            {symbol_name: (module_path, attr_name), ...}
        """
        if isinstance(node, ast.Dict):
            module_paths_seen: Set[str] = set()
            for key, value in zip(node.keys, node.values):
                # 提取 symbol_name（key）
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    self.tier_b_keys.append(key.value)
                # 提取 module_path（value 是 tuple 的第一个元素）
                if isinstance(value, ast.Tuple) and len(value.elts) >= 1:
                    first_elt = value.elts[0]
                    if isinstance(first_elt, ast.Constant) and isinstance(first_elt.value, str):
                        module_path = first_elt.value
                        if module_path not in module_paths_seen:
                            module_paths_seen.add(module_path)
                            self.tier_b_module_paths.append(module_path)

    def _extract_install_hint_keys(self, node: ast.expr) -> None:
        """从 AST 节点提取 _TIER_B_INSTALL_HINTS 的 key

        _TIER_B_INSTALL_HINTS 格式为:
            {module_path: install_hint_string, ...}
        """
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    self.install_hint_keys.append(key.value)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """检查 from ... import ... 语句"""
        # TYPE_CHECKING 块内允许
        if self.in_type_checking_block:
            return
        # __getattr__ 函数内允许（延迟执行）
        if self.in_getattr_function:
            return

        # 处理相对导入（level == 1 表示 from . 或 from .xxx）
        if node.level == 1:
            # from . import <module_name>
            if node.module is None:
                for alias in node.names:
                    self._check_relative_import(node, alias.name)
            # from .<module> import ...
            else:
                self._check_relative_import(node, node.module)

        # 处理绝对导入 from engram.gateway.<module> import ...
        elif (
            node.level == 0 and node.module and node.module.startswith(GATEWAY_MODULE_PREFIX + ".")
        ):
            submodule = node.module[len(GATEWAY_MODULE_PREFIX) + 1 :]
            self._check_relative_import(node, submodule)

    def visit_Import(self, node: ast.Import) -> None:
        """检查 import ... 语句"""
        # TYPE_CHECKING 块内允许
        if self.in_type_checking_block:
            return
        # __getattr__ 函数内允许（延迟执行）
        if self.in_getattr_function:
            return

        for alias in node.names:
            # 检查 import engram.gateway.<module>
            if alias.name.startswith(GATEWAY_MODULE_PREFIX + "."):
                submodule = alias.name[len(GATEWAY_MODULE_PREFIX) + 1 :]
                # 获取第一级模块名
                first_part = submodule.split(".")[0]
                self._check_relative_import(node, first_part)

    def _check_relative_import(self, node: ast.AST, module_name: str) -> None:
        """
        检查相对导入是否合规

        违规条件：
        1. 模块不在 allowlist 中（非 Tier A 模块）
        2. 或者模块是 Tier B 模块（必须懒加载）
        """
        # 获取第一级模块名用于 allowlist 检查
        first_part = module_name.split(".")[0]

        # 检查是否在 allowlist 中
        if not _is_in_allowlist(module_name) and first_part not in ALLOWED_RELATIVE_IMPORTS:
            # 判断是 Tier B 还是其他非允许模块
            if _is_tier_b_module(module_name) or _is_tier_b_module(first_part):
                self._add_violation(
                    node,
                    module_name,
                    "from_import" if isinstance(node, ast.ImportFrom) else "import",
                    "tier_b_eager_import",
                    f"禁止在 TYPE_CHECKING 块外 eager-import Tier B 模块 '{module_name}'，"
                    f"应使用 __getattr__ + importlib 懒加载或仅在 TYPE_CHECKING 块内导入",
                )
            else:
                self._add_violation(
                    node,
                    module_name,
                    "from_import" if isinstance(node, ast.ImportFrom) else "import",
                    "not_in_allowlist",
                    f"禁止导入非 allowlist 模块 '{module_name}'，"
                    f"allowlist: {sorted(ALLOWED_RELATIVE_IMPORTS)}",
                )

    def _add_violation(
        self,
        node: ast.AST,
        module_name: str,
        import_type: str,
        violation_type: str,
        message: str,
    ) -> None:
        """添加违规记录"""
        line_number = node.lineno
        line_content = (
            self.source_lines[line_number - 1] if line_number <= len(self.source_lines) else ""
        )

        self.violations.append(
            ImportViolation(
                line_number=line_number,
                line_content=line_content,
                module_name=module_name,
                import_type=import_type,
                violation_type=violation_type,
                message=message,
            )
        )


def check_public_api_import_surface(file_path: Path) -> CheckResult:
    """检查 public_api.py 的 import surface

    Args:
        file_path: public_api.py 文件路径

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

    checker = PublicApiImportChecker(source_lines)
    checker.visit(tree)

    result.violations = checker.violations
    result.has_type_checking_guard = checker.has_type_checking_guard
    result.has_getattr_lazy_load = checker.has_getattr_lazy_load
    result.has_tier_b_lazy_imports_mapping = checker.has_tier_b_lazy_imports_mapping
    result.has_tier_b_install_hints_mapping = checker.has_tier_b_install_hints_mapping

    # 构建 __all__ 一致性校验结果
    all_symbols_set = set(checker.all_symbols)
    missing_in_all = [k for k in checker.tier_b_keys if k not in all_symbols_set]
    result.all_consistency = AllConsistencyResult(
        all_symbols=checker.all_symbols,
        tier_b_keys=checker.tier_b_keys,
        missing_in_all=missing_in_all,
    )

    # 构建 install_hint 一致性校验结果（按 module_path 必填策略）
    install_hint_keys_set = set(checker.install_hint_keys)
    missing_install_hints = [
        mp for mp in checker.tier_b_module_paths if mp not in install_hint_keys_set
    ]
    result.install_hint_consistency = InstallHintConsistencyResult(
        tier_b_module_paths=checker.tier_b_module_paths,
        install_hint_keys=checker.install_hint_keys,
        missing_install_hints=missing_install_hints,
    )

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 Gateway public_api.py 的 import surface，确保 Tier B 模块使用懒加载"
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

    file_path = project_root / PUBLIC_API_PATH

    if not file_path.exists():
        print(f"[ERROR] 文件不存在: {file_path}", file=sys.stderr)
        return 1

    # 执行检查
    result = check_public_api_import_surface(file_path)

    # 输出结果
    if args.json:
        output = result.to_dict()
        output["file"] = str(PUBLIC_API_PATH)
        output["allowed_relative_imports"] = sorted(ALLOWED_RELATIVE_IMPORTS)
        output["tier_b_modules"] = sorted(TIER_B_MODULES)
        output["tier_b_submodule_paths"] = sorted(TIER_B_SUBMODULE_PATHS)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("Gateway Public API Import Surface 检查")
        print("=" * 70)
        print()
        print(f"检查文件: {file_path}")
        print(f"Allowlist 模块: {', '.join(sorted(ALLOWED_RELATIVE_IMPORTS))}")
        print(f"Tier B 模块: {', '.join(sorted(TIER_B_MODULES))}")
        print(f"Tier B 子模块路径: {', '.join(sorted(TIER_B_SUBMODULE_PATHS))}")
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

        if result.has_tier_b_lazy_imports_mapping:
            print("  [OK] 检测到 _TIER_B_LAZY_IMPORTS 映射表")
        else:
            print("  [WARN] 未检测到 _TIER_B_LAZY_IMPORTS 映射表")

        if result.has_tier_b_install_hints_mapping:
            print("  [OK] 检测到 _TIER_B_INSTALL_HINTS 映射表")
        else:
            print("  [WARN] 未检测到 _TIER_B_INSTALL_HINTS 映射表")
        print()

        # 显示 __all__ 一致性校验结果
        print("__all__ 一致性校验:")
        print(f"  __all__ 符号数: {len(result.all_consistency.all_symbols)}")
        print(f"  _TIER_B_LAZY_IMPORTS key 数: {len(result.all_consistency.tier_b_keys)}")
        if result.all_consistency.is_consistent():
            print("  [OK] _TIER_B_LAZY_IMPORTS 所有 key 都在 __all__ 中")
        else:
            print("  [ERROR] 以下 _TIER_B_LAZY_IMPORTS key 不在 __all__ 中:")
            for key in result.all_consistency.missing_in_all:
                print(f"    - {key}")
        print()

        # 显示 install_hint 一致性校验结果
        print("install_hint 一致性校验（按 module_path 必填）:")
        print(
            f"  _TIER_B_LAZY_IMPORTS module_path 数: "
            f"{len(result.install_hint_consistency.tier_b_module_paths)}"
        )
        print(
            f"  _TIER_B_INSTALL_HINTS key 数: "
            f"{len(result.install_hint_consistency.install_hint_keys)}"
        )
        if result.install_hint_consistency.is_consistent():
            print("  [OK] 所有 module_path 都有对应的 install_hint")
        else:
            print("  [ERROR] 以下 module_path 缺失 install_hint:")
            for mp in result.install_hint_consistency.missing_install_hints:
                print(f"    - {mp}")
        print()

        if not result.has_violations():
            print("[OK] 未发现导入违规")
        else:
            allowlist_violations = result.get_allowlist_violations()
            tier_b_violations = result.get_tier_b_violations()

            if allowlist_violations:
                print(f"[ERROR] 发现 {len(allowlist_violations)} 处非 allowlist 模块导入违规:")
                print()
                for v in allowlist_violations:
                    print(f"  第 {v.line_number} 行: {v.line_content.strip()}")
                    print(f"    模块: {v.module_name}")
                    print(f"    类型: {v.import_type}")
                    if args.verbose:
                        print(f"    说明: {v.message}")
                    print()

            if tier_b_violations:
                print(f"[ERROR] 发现 {len(tier_b_violations)} 处 Tier B 模块 eager-import 违规:")
                print()
                for v in tier_b_violations:
                    print(f"  第 {v.line_number} 行: {v.line_content.strip()}")
                    print(f"    模块: {v.module_name}")
                    print(f"    类型: {v.import_type}")
                    if args.verbose:
                        print(f"    说明: {v.message}")
                    print()

        print("-" * 70)
        print(f"违规总数: {len(result.violations)}")
        print(f"  - 非 allowlist 模块导入: {len(result.get_allowlist_violations())}")
        print(f"  - Tier B eager-import: {len(result.get_tier_b_violations())}")
        print(f"__all__ 一致性错误: {len(result.all_consistency.missing_in_all)}")
        print(
            f"install_hint 一致性错误: {len(result.install_hint_consistency.missing_install_hints)}"
        )
        print()

        has_errors = result.has_violations() or result.has_consistency_errors()
        if has_errors:
            print("[FAIL] Gateway Public API import surface 检查失败")
            print()
            print("修复指南:")
            if result.get_allowlist_violations():
                print("  - 非 allowlist 模块不应出现在 public_api.py 中")
                print(f"  - 允许的模块: {sorted(ALLOWED_RELATIVE_IMPORTS)}")
            if result.get_tier_b_violations():
                print("  - 将 Tier B 模块导入移至 TYPE_CHECKING 块内（仅用于静态类型提示）")
                print("  - 使用 __getattr__ + importlib 实现运行时懒加载")
            if not result.all_consistency.is_consistent():
                print("  - 确保 _TIER_B_LAZY_IMPORTS 的所有 key 都出现在 __all__ 中")
            if not result.install_hint_consistency.is_consistent():
                print("  - 确保 _TIER_B_INSTALL_HINTS 包含所有 module_path 的安装指引")
                print("  - 缺失的 module_path 需要在 _TIER_B_INSTALL_HINTS 中添加对应条目")
            print("  - 参见: docs/architecture/gateway_public_api_surface.md")
        else:
            print("[OK] Gateway Public API import surface 检查通过")

    return 1 if (result.has_violations() or result.has_consistency_errors()) else 0


if __name__ == "__main__":
    sys.exit(main())
