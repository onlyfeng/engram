#!/usr/bin/env python3
"""
CI 测试隔离与导入卫生检查脚本

检查以下两类问题：

1. tests/**/*.py 测试隔离问题：
   - 模块级 sys.path.insert() 或 sys.path.append()
   - 从顶层名导入 CI 脚本模块（如 `from validate_workflows import`）

2. scripts/ci/**/*.py 导入卫生问题：
   - dual-mode import 模式（try/except ImportError 中回退到顶层名导入）

这些做法会污染 sys.path 和 sys.modules，导致测试间相互影响。

用法:
    python scripts/ci/check_ci_test_isolation.py
    python scripts/ci/check_ci_test_isolation.py --json          # JSON 输出
    python scripts/ci/check_ci_test_isolation.py --verbose       # 详细输出
    python scripts/ci/check_ci_test_isolation.py --scan-scripts  # 扫描 scripts/ci
    python scripts/ci/check_ci_test_isolation.py --scan-tests    # 扫描 tests
    python scripts/ci/check_ci_test_isolation.py --scan-all      # 扫描全部（默认）
    python scripts/ci/check_ci_test_isolation.py --allowlist FILE  # 使用豁免列表
    python scripts/ci/check_ci_test_isolation.py --strict        # 严格模式（忽略豁免）

退出码:
    0 - 检查通过
    1 - 发现违规（未被豁免的）
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# ============================================================================
# 配置
# ============================================================================

# 默认扫描目录
DEFAULT_TESTS_SCAN_DIR = "tests"
DEFAULT_SCRIPTS_SCAN_DIR = "scripts/ci"

# 默认 allowlist 文件路径（相对于项目根）
DEFAULT_ALLOWLIST_FILE = "configs/ci_test_isolation_allowlist.json"
DEFAULT_SCHEMA_FILE = "schemas/ci_test_isolation_allowlist_v1.schema.json"

# 保留旧配置名以保持向后兼容
DEFAULT_SCAN_DIR = DEFAULT_TESTS_SCAN_DIR


def _discover_ci_modules(scripts_ci_dir: Path | None = None) -> frozenset[str]:
    """
    自动发现 scripts/ci/ 目录下的 Python 模块名。

    这些模块禁止作为顶层模块导入，必须通过 scripts.ci.xxx 命名空间导入。
    自动发现避免手工列表漂移。

    Args:
        scripts_ci_dir: scripts/ci 目录路径，None 时自动检测

    Returns:
        frozenset[str]: 模块名集合（不带 .py 后缀）
    """
    if scripts_ci_dir is None:
        # 自动检测：当前脚本所在目录即为 scripts/ci
        scripts_ci_dir = Path(__file__).resolve().parent

    if not scripts_ci_dir.exists():
        return frozenset()

    modules: set[str] = set()
    for py_file in scripts_ci_dir.glob("*.py"):
        # 排除 __init__.py
        if py_file.name == "__init__.py":
            continue
        # 提取模块名（去掉 .py 后缀）
        module_name = py_file.stem
        modules.add(module_name)

    return frozenset(modules)


# 禁止作为顶层模块导入的 CI 脚本模块名
# 自动发现 scripts/ci/*.py，避免手工列表漂移
FORBIDDEN_TOPLEVEL_MODULES: frozenset[str] = _discover_ci_modules()


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class Violation:
    """单个违规记录"""

    file_path: str
    line_number: int
    violation_type: (
        str  # "sys_path_insert", "sys_path_append", "toplevel_import", "dual_mode_import"
    )
    message: str
    code_snippet: str = ""
    fix_hint: str = ""
    suggested_fix: str = ""  # 具体的修复建议代码


@dataclass
class AllowlistEntry:
    """Allowlist 条目"""

    id: str
    file_glob: str
    violation_type: (
        str  # "sys_path_insert", "sys_path_append", "toplevel_import", "dual_mode_import", "any"
    )
    reason: str
    owner: str
    expires_on: str  # YYYY-MM-DD
    jira_ticket: str = ""
    notes: str = ""
    created_at: str = ""


@dataclass
class Allowlist:
    """Allowlist 数据"""

    version: str
    description: str
    entries: list[AllowlistEntry] = field(default_factory=list)
    expired_entries: list[str] = field(default_factory=list)  # 过期条目的 id 列表

    def is_exempted(self, file_path: str, violation_type: str) -> tuple[bool, str | None]:
        """
        检查违规是否被豁免。

        Args:
            file_path: 文件相对路径
            violation_type: 违规类型

        Returns:
            (是否豁免, 豁免条目 id 或 None)
        """
        for entry in self.entries:
            # 检查文件匹配
            if not fnmatch.fnmatch(file_path, entry.file_glob):
                continue

            # 检查违规类型匹配
            if entry.violation_type != "any" and entry.violation_type != violation_type:
                continue

            # 检查是否过期
            if entry.id in self.expired_entries:
                continue

            return True, entry.id

        return False, None


@dataclass
class CheckResult:
    """检查结果"""

    violations: list[Violation] = field(default_factory=list)
    exempted_violations: list[tuple[Violation, str]] = field(
        default_factory=list
    )  # (violation, entry_id)
    files_checked: int = 0
    files_with_violations: int = 0
    tests_files_checked: int = 0
    scripts_files_checked: int = 0
    allowlist_used: bool = False
    expired_allowlist_entries: list[str] = field(default_factory=list)

    def has_violations(self) -> bool:
        """只检查未豁免的违规"""
        return len(self.violations) > 0

    def has_any_violations(self) -> bool:
        """检查是否有任何违规（包括已豁免的）"""
        return len(self.violations) > 0 or len(self.exempted_violations) > 0

    def has_expired_entries(self) -> bool:
        """检查是否有过期的 allowlist 条目"""
        return len(self.expired_allowlist_entries) > 0

    def to_dict(self) -> dict[str, Any]:
        # 按文件分组违规
        violations_by_file: dict[str, list[dict[str, Any]]] = {}
        for v in self.violations:
            if v.file_path not in violations_by_file:
                violations_by_file[v.file_path] = []
            violations_by_file[v.file_path].append(
                {
                    "line_number": v.line_number,
                    "violation_type": v.violation_type,
                    "message": v.message,
                    "code_snippet": v.code_snippet,
                    "fix_hint": v.fix_hint,
                    "suggested_fix": v.suggested_fix,
                }
            )

        # 按文件分组已豁免违规
        exempted_by_file: dict[str, list[dict[str, Any]]] = {}
        for v, entry_id in self.exempted_violations:
            if v.file_path not in exempted_by_file:
                exempted_by_file[v.file_path] = []
            exempted_by_file[v.file_path].append(
                {
                    "line_number": v.line_number,
                    "violation_type": v.violation_type,
                    "message": v.message,
                    "exempted_by": entry_id,
                }
            )

        return {
            "ok": not self.has_violations() and not self.has_expired_entries(),
            "summary": {
                "files_checked": self.files_checked,
                "tests_files_checked": self.tests_files_checked,
                "scripts_files_checked": self.scripts_files_checked,
                "files_with_violations": self.files_with_violations,
                "violation_count": len(self.violations),
                "exempted_count": len(self.exempted_violations),
                "expired_allowlist_entries": len(self.expired_allowlist_entries),
            },
            "violations_by_file": violations_by_file,
            "exempted_by_file": exempted_by_file,
            "expired_allowlist_entries": self.expired_allowlist_entries,
            # 保留扁平列表以保持向后兼容
            "violations": [
                {
                    "file_path": v.file_path,
                    "line_number": v.line_number,
                    "violation_type": v.violation_type,
                    "message": v.message,
                    "code_snippet": v.code_snippet,
                    "fix_hint": v.fix_hint,
                    "suggested_fix": v.suggested_fix,
                }
                for v in self.violations
            ],
        }


# ============================================================================
# Allowlist 加载
# ============================================================================


def load_allowlist(allowlist_path: Path) -> Allowlist | None:
    """
    加载 allowlist 文件。

    Args:
        allowlist_path: allowlist 文件路径

    Returns:
        Allowlist 对象，如果文件不存在或加载失败则返回 None
    """
    if not allowlist_path.exists():
        return None

    try:
        with open(allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] 无法加载 allowlist 文件 {allowlist_path}: {e}", file=sys.stderr)
        return None

    # 解析条目
    entries: list[AllowlistEntry] = []
    expired_entries: list[str] = []
    today = date.today()

    for entry_data in data.get("entries", []):
        entry = AllowlistEntry(
            id=entry_data.get("id", ""),
            file_glob=entry_data.get("file_glob", ""),
            violation_type=entry_data.get("violation_type", "any"),
            reason=entry_data.get("reason", ""),
            owner=entry_data.get("owner", ""),
            expires_on=entry_data.get("expires_on", ""),
            jira_ticket=entry_data.get("jira_ticket", ""),
            notes=entry_data.get("notes", ""),
            created_at=entry_data.get("created_at", ""),
        )
        entries.append(entry)

        # 检查是否过期
        if entry.expires_on:
            try:
                expiry_date = date.fromisoformat(entry.expires_on)
                if today > expiry_date:
                    expired_entries.append(entry.id)
            except ValueError:
                pass  # 无效日期格式

    return Allowlist(
        version=data.get("version", "1"),
        description=data.get("description", ""),
        entries=entries,
        expired_entries=expired_entries,
    )


# ============================================================================
# AST 分析器
# ============================================================================


class IsolationViolationVisitor(ast.NodeVisitor):
    """
    AST 访问器，检测以下违规：
    1. 模块级 sys.path.insert() / sys.path.append()
    2. 从顶层名导入 CI 脚本模块
    """

    def __init__(self, file_path: str, source_lines: list[str]) -> None:
        self.file_path = file_path
        self.source_lines = source_lines
        self.violations: list[Violation] = []
        self._in_function_or_class = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """进入函数时设置标志"""
        old_flag = self._in_function_or_class
        self._in_function_or_class = True
        self.generic_visit(node)
        self._in_function_or_class = old_flag

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """进入异步函数时设置标志"""
        old_flag = self._in_function_or_class
        self._in_function_or_class = True
        self.generic_visit(node)
        self._in_function_or_class = old_flag

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """进入类时设置标志"""
        old_flag = self._in_function_or_class
        self._in_function_or_class = True
        self.generic_visit(node)
        self._in_function_or_class = old_flag

    def visit_Expr(self, node: ast.Expr) -> None:
        """检查表达式语句（包括函数调用）"""
        if not self._in_function_or_class:
            self._check_sys_path_mutation(node.value, node.lineno)
        self.generic_visit(node)

    def _check_sys_path_mutation(self, node: ast.expr, line_number: int) -> None:
        """检查 sys.path.insert / sys.path.append 调用"""
        if not isinstance(node, ast.Call):
            return

        func = node.func
        if not isinstance(func, ast.Attribute):
            return

        # 检查是否是 sys.path.insert 或 sys.path.append
        if func.attr not in ("insert", "append"):
            return

        # 检查是否是 sys.path.xxx
        value = func.value
        if not isinstance(value, ast.Attribute):
            return

        if value.attr != "path":
            return

        # 检查是否是 sys.path
        if not isinstance(value.value, ast.Name):
            return

        if value.value.id != "sys":
            return

        # 发现违规
        code_snippet = self._get_line(line_number)
        violation_type = f"sys_path_{func.attr}"
        self.violations.append(
            Violation(
                file_path=self.file_path,
                line_number=line_number,
                violation_type=violation_type,
                message=f"模块级 sys.path.{func.attr}() 调用会污染 sys.path",
                code_snippet=code_snippet,
                fix_hint=self._get_sys_path_fix_hint(func.attr),
                suggested_fix="# 移除 sys.path 操作，改用 from scripts.ci.xxx import ...",
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        """检查 import 语句"""
        if self._in_function_or_class:
            return

        for alias in node.names:
            module_name = alias.name.split(".")[0]
            if module_name in FORBIDDEN_TOPLEVEL_MODULES:
                code_snippet = self._get_line(node.lineno)
                self.violations.append(
                    Violation(
                        file_path=self.file_path,
                        line_number=node.lineno,
                        violation_type="toplevel_import",
                        message=f"禁止从顶层名导入 CI 脚本模块: {alias.name}",
                        code_snippet=code_snippet,
                        fix_hint=self._get_import_fix_hint(alias.name),
                        suggested_fix=f"from scripts.ci.{module_name} import ...",
                    )
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """检查 from ... import 语句"""
        if self._in_function_or_class:
            return

        if node.module is None:
            return

        # 检查是否从禁止的顶层模块导入
        module_name = node.module.split(".")[0]
        if module_name in FORBIDDEN_TOPLEVEL_MODULES:
            code_snippet = self._get_line(node.lineno)
            import_names = [alias.name for alias in node.names]
            self.violations.append(
                Violation(
                    file_path=self.file_path,
                    line_number=node.lineno,
                    violation_type="toplevel_import",
                    message=f"禁止从顶层名导入 CI 脚本模块: from {node.module} import ...",
                    code_snippet=code_snippet,
                    fix_hint=self._get_import_fix_hint(node.module),
                    suggested_fix=self._get_import_suggested_fix(node.module, import_names),
                )
            )

    def _get_line(self, line_number: int) -> str:
        """获取指定行的代码"""
        if 1 <= line_number <= len(self.source_lines):
            return self.source_lines[line_number - 1].rstrip()
        return ""

    def _get_sys_path_fix_hint(self, method: str) -> str:
        """获取 sys.path 修复建议"""
        return (
            f"移除模块级 sys.path.{method}() 调用。\n"
            "正确做法：\n"
            "  1. 使用 scripts.ci.xxx 命名空间导入 CI 脚本\n"
            "  2. 或在 fixture/函数内部进行路径操作\n"
            "\n"
            "示例（推荐）:\n"
            "  from scripts.ci.validate_workflows import validate_contract\n"
            "\n"
            "示例（fixture 内部）:\n"
            "  @pytest.fixture\n"
            "  def setup_path():\n"
            "      sys.path.insert(0, ...)\n"
            "      yield\n"
            "      sys.path.remove(...)"
        )

    def _get_import_fix_hint(self, module_name: str) -> str:
        """获取导入修复建议"""
        base_module = module_name.split(".")[0]
        return (
            f"使用 scripts.ci 命名空间导入：\n"
            f"  from scripts.ci.{base_module} import ...\n"
            f"\n"
            f"而非：\n"
            f"  from {module_name} import ...\n"
            f"  import {module_name}"
        )

    def _get_import_suggested_fix(
        self, module_name: str, import_names: list[str] | None = None
    ) -> str:
        """获取具体的修复代码建议"""
        base_module = module_name.split(".")[0]
        if import_names:
            names_str = ", ".join(import_names)
            return f"from scripts.ci.{base_module} import {names_str}"
        return f"from scripts.ci.{base_module} import ..."


# ============================================================================
# Dual-mode Import 检测器（用于 scripts/ci）
# ============================================================================


class DualModeImportVisitor(ast.NodeVisitor):
    """
    AST 访问器，检测 dual-mode import 模式：
    - try/except ImportError 块中回退到顶层名导入
    """

    def __init__(self, file_path: str, source_lines: list[str]) -> None:
        self.file_path = file_path
        self.source_lines = source_lines
        self.violations: list[Violation] = []

    def visit_Try(self, node: ast.Try) -> None:
        """检查 try/except ImportError 块"""
        # 检查是否有 except ImportError
        has_import_error_handler = False
        for handler in node.handlers:
            if handler.type is None:
                continue
            if isinstance(handler.type, ast.Name) and handler.type.id == "ImportError":
                has_import_error_handler = True
                self._check_except_block(handler, node.lineno)
            elif isinstance(handler.type, ast.Tuple):
                for elt in handler.type.elts:
                    if isinstance(elt, ast.Name) and elt.id == "ImportError":
                        has_import_error_handler = True
                        self._check_except_block(handler, node.lineno)
                        break

        if has_import_error_handler:
            # 也检查 try 块中是否有从 scripts.ci 导入后回退
            self._check_try_block_for_fallback(node)

        self.generic_visit(node)

    def _check_except_block(self, handler: ast.ExceptHandler, try_line: int) -> None:
        """检查 except 块中是否有顶层名导入回退"""
        for stmt in handler.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    module_name = alias.name.split(".")[0]
                    if module_name in FORBIDDEN_TOPLEVEL_MODULES:
                        code_snippet = self._get_line(stmt.lineno)
                        self.violations.append(
                            Violation(
                                file_path=self.file_path,
                                line_number=stmt.lineno,
                                violation_type="dual_mode_import",
                                message=f"except ImportError 块中回退到顶层名导入: {alias.name}",
                                code_snippet=code_snippet,
                                fix_hint=self._get_dual_mode_fix_hint(alias.name),
                                suggested_fix=f"from scripts.ci.{module_name} import ...",
                            )
                        )
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.module is None:
                    continue
                module_name = stmt.module.split(".")[0]
                if module_name in FORBIDDEN_TOPLEVEL_MODULES:
                    code_snippet = self._get_line(stmt.lineno)
                    import_names = [alias.name for alias in stmt.names]
                    self.violations.append(
                        Violation(
                            file_path=self.file_path,
                            line_number=stmt.lineno,
                            violation_type="dual_mode_import",
                            message=f"except ImportError 块中回退到顶层名导入: from {stmt.module} import ...",
                            code_snippet=code_snippet,
                            fix_hint=self._get_dual_mode_fix_hint(stmt.module),
                            suggested_fix=f"from scripts.ci.{module_name} import {', '.join(import_names)}",
                        )
                    )

    def _check_try_block_for_fallback(self, node: ast.Try) -> None:
        """检查 try 块是否尝试从 scripts.ci 导入，然后在 except 中回退"""
        # 这里可以添加更复杂的逻辑来检测 try/except 对
        pass

    def _get_line(self, line_number: int) -> str:
        """获取指定行的代码"""
        if 1 <= line_number <= len(self.source_lines):
            return self.source_lines[line_number - 1].rstrip()
        return ""

    def _get_dual_mode_fix_hint(self, module_name: str) -> str:
        """获取 dual-mode import 修复建议"""
        base_module = module_name.split(".")[0]
        return (
            f"移除 dual-mode import 模式。\n"
            f"正确做法：\n"
            f"  1. 始终使用 scripts.ci.{base_module} 命名空间导入\n"
            f"  2. 或改用 python -m 子进程调用\n"
            f"\n"
            f"示例（推荐）:\n"
            f"  from scripts.ci.{base_module} import ...\n"
            f"\n"
            f"示例（子进程调用）:\n"
            f"  import subprocess\n"
            f"  result = subprocess.run(\n"
            f"      ['python', '-m', 'scripts.ci.{base_module}', ...],\n"
            f"      capture_output=True\n"
            f"  )"
        )


# ============================================================================
# 核心检查函数
# ============================================================================


def check_file_isolation(file_path: Path) -> list[Violation]:
    """检查单个文件的隔离问题（用于 tests 目录）"""
    if not file_path.exists():
        return []

    try:
        source = file_path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError) as e:
        # 语法错误的文件跳过
        print(f"[WARN] 无法解析文件 {file_path}: {e}", file=sys.stderr)
        return []

    visitor = IsolationViolationVisitor(str(file_path), source_lines)
    visitor.visit(tree)
    return visitor.violations


# 保留向后兼容的别名
check_file = check_file_isolation


def check_file_hygiene(file_path: Path) -> list[Violation]:
    """检查单个文件的导入卫生问题（用于 scripts/ci 目录）"""
    if not file_path.exists():
        return []

    try:
        source = file_path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError) as e:
        # 语法错误的文件跳过
        print(f"[WARN] 无法解析文件 {file_path}: {e}", file=sys.stderr)
        return []

    visitor = DualModeImportVisitor(str(file_path), source_lines)
    visitor.visit(tree)
    return visitor.violations


def check_tests_directory(
    scan_dir: Path,
    project_root: Path,
) -> tuple[list[Violation], int]:
    """检查 tests 目录下所有 Python 文件的隔离问题"""
    violations: list[Violation] = []
    files_checked = 0

    # 收集所有 .py 文件
    py_files = list(scan_dir.rglob("*.py"))
    files_checked = len(py_files)

    for py_file in py_files:
        file_violations = check_file_isolation(py_file)
        if file_violations:
            # 转换为相对路径
            try:
                rel_path = str(py_file.relative_to(project_root))
            except ValueError:
                rel_path = str(py_file)

            for v in file_violations:
                v.file_path = rel_path

            violations.extend(file_violations)

    return violations, files_checked


def check_scripts_directory(
    scan_dir: Path,
    project_root: Path,
) -> tuple[list[Violation], int]:
    """检查 scripts/ci 目录下所有 Python 文件的导入卫生问题"""
    violations: list[Violation] = []
    files_checked = 0

    # 收集所有 .py 文件
    py_files = list(scan_dir.rglob("*.py"))
    files_checked = len(py_files)

    for py_file in py_files:
        file_violations = check_file_hygiene(py_file)
        if file_violations:
            # 转换为相对路径
            try:
                rel_path = str(py_file.relative_to(project_root))
            except ValueError:
                rel_path = str(py_file)

            for v in file_violations:
                v.file_path = rel_path

            violations.extend(file_violations)

    return violations, files_checked


def check_directory(
    scan_dir: Path,
    project_root: Path,
) -> CheckResult:
    """检查目录下所有 Python 文件（保留向后兼容）"""
    result = CheckResult()

    # 收集所有 .py 文件
    py_files = list(scan_dir.rglob("*.py"))
    result.files_checked = len(py_files)
    result.tests_files_checked = len(py_files)

    files_with_violations: set[str] = set()

    for py_file in py_files:
        violations = check_file_isolation(py_file)
        if violations:
            # 转换为相对路径
            try:
                rel_path = str(py_file.relative_to(project_root))
            except ValueError:
                rel_path = str(py_file)

            for v in violations:
                v.file_path = rel_path

            result.violations.extend(violations)
            files_with_violations.add(rel_path)

    result.files_with_violations = len(files_with_violations)
    return result


def check_all(
    tests_dir: Path | None,
    scripts_dir: Path | None,
    project_root: Path,
    allowlist: Allowlist | None = None,
    strict: bool = False,
) -> CheckResult:
    """
    检查所有目录。

    Args:
        tests_dir: tests 目录路径
        scripts_dir: scripts/ci 目录路径
        project_root: 项目根目录
        allowlist: 豁免列表（None 表示不使用豁免）
        strict: 严格模式（忽略豁免，所有违规都报告为错误）

    Returns:
        CheckResult 对象
    """
    result = CheckResult()
    files_with_violations: set[str] = set()
    all_violations: list[Violation] = []

    # 检查 tests 目录
    if tests_dir and tests_dir.exists():
        violations, files_checked = check_tests_directory(tests_dir, project_root)
        result.tests_files_checked = files_checked
        result.files_checked += files_checked
        all_violations.extend(violations)

    # 检查 scripts/ci 目录
    if scripts_dir and scripts_dir.exists():
        violations, files_checked = check_scripts_directory(scripts_dir, project_root)
        result.scripts_files_checked = files_checked
        result.files_checked += files_checked
        all_violations.extend(violations)

    # 应用豁免
    if allowlist and not strict:
        result.allowlist_used = True
        result.expired_allowlist_entries = allowlist.expired_entries.copy()

        for v in all_violations:
            is_exempted, entry_id = allowlist.is_exempted(v.file_path, v.violation_type)
            if is_exempted and entry_id:
                result.exempted_violations.append((v, entry_id))
            else:
                result.violations.append(v)
                files_with_violations.add(v.file_path)
    else:
        # 严格模式或无 allowlist：所有违规都是错误
        for v in all_violations:
            result.violations.append(v)
            files_with_violations.add(v.file_path)

    result.files_with_violations = len(files_with_violations)
    return result


# ============================================================================
# 输出格式化函数
# ============================================================================


def format_human_readable(
    result: CheckResult,
    project_root: Path,
    tests_dir: Path | None,
    scripts_dir: Path | None,
    verbose: bool = False,
    allowlist_path: Path | None = None,
) -> str:
    """格式化人类可读的输出"""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("CI 测试隔离与导入卫生检查")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"项目根目录: {project_root}")
    if tests_dir:
        lines.append(f"测试目录: {tests_dir} (扫描 {result.tests_files_checked} 个文件)")
    if scripts_dir:
        lines.append(f"脚本目录: {scripts_dir} (扫描 {result.scripts_files_checked} 个文件)")
    lines.append(f"总扫描文件数: {result.files_checked}")
    if result.allowlist_used and allowlist_path:
        lines.append(f"豁免列表: {allowlist_path}")
    lines.append("")

    # 显示过期的 allowlist 条目（错误）
    if result.expired_allowlist_entries:
        lines.append(f"[ERROR] 发现 {len(result.expired_allowlist_entries)} 个过期的豁免条目：")
        for entry_id in result.expired_allowlist_entries:
            lines.append(f"  - {entry_id}")
        lines.append("")
        lines.append("请更新或删除 allowlist 中的过期条目。")
        lines.append("")

    if result.violations:
        lines.append(f"[ERROR] 发现 {len(result.violations)} 处未豁免的违规：")
        lines.append("")

        # 按文件分组显示
        violations_by_file: dict[str, list[Violation]] = {}
        for v in result.violations:
            violations_by_file.setdefault(v.file_path, []).append(v)

        for file_path, violations in sorted(violations_by_file.items()):
            lines.append(f"  文件: {file_path}")
            for v in sorted(violations, key=lambda x: x.line_number):
                lines.append(f"    行 {v.line_number}: [{v.violation_type}] {v.message}")
                if v.code_snippet:
                    lines.append(f"      代码: {v.code_snippet}")
                if v.suggested_fix:
                    lines.append(f"      建议改为: {v.suggested_fix}")
                if v.fix_hint and verbose:
                    lines.append("      详细修复建议:")
                    for line in v.fix_hint.split("\n"):
                        lines.append(f"        {line}")
            lines.append("")

        lines.append("-" * 70)
        lines.append("按文件汇总的建议改法:")
        lines.append("")

        for file_path, violations in sorted(violations_by_file.items()):
            lines.append(f"  {file_path}:")
            seen_fixes: set[str] = set()
            for v in sorted(violations, key=lambda x: x.line_number):
                if v.suggested_fix and v.suggested_fix not in seen_fixes:
                    lines.append(f"    - {v.suggested_fix}")
                    seen_fixes.add(v.suggested_fix)
            lines.append("")

        lines.append("-" * 70)
        lines.append(
            f"统计: 扫描 {result.files_checked} 个文件 | "
            f"违规文件 {result.files_with_violations} 个 | "
            f"未豁免违规 {len(result.violations)} 处"
        )
        if result.exempted_violations:
            lines.append(f"       已豁免 {len(result.exempted_violations)} 处（不阻断 CI）")
        lines.append("")
        lines.append("[FAIL] CI 测试隔离与导入卫生检查失败")
        lines.append("")
        lines.append("修复指南:")
        lines.append("  1. 使用 scripts.ci.xxx 命名空间导入 CI 脚本模块")
        lines.append("  2. 移除模块级 sys.path.insert/append 调用")
        lines.append("  3. 如需路径操作，在 fixture 或函数内部进行")
        lines.append("  4. 或改用 python -m 子进程调用")
        if result.allowlist_used:
            lines.append("")
            lines.append("如需临时豁免，请在 allowlist 中添加条目：")
            lines.append(f"  {allowlist_path or DEFAULT_ALLOWLIST_FILE}")
    else:
        # 显示已豁免的违规统计
        if result.exempted_violations and verbose:
            lines.append(f"[INFO] 已豁免 {len(result.exempted_violations)} 处历史违规：")
            exempted_by_entry: dict[str, int] = {}
            for _, entry_id in result.exempted_violations:
                exempted_by_entry[entry_id] = exempted_by_entry.get(entry_id, 0) + 1
            for entry_id, count in sorted(exempted_by_entry.items()):
                lines.append(f"  - {entry_id}: {count} 处")
            lines.append("")

        if result.has_expired_entries():
            lines.append("[FAIL] CI 测试隔离检查失败（存在过期豁免条目）")
        else:
            exempted_note = ""
            if result.exempted_violations:
                exempted_note = f"，已豁免 {len(result.exempted_violations)} 处历史违规"
            lines.append(
                f"[OK] CI 测试隔离与导入卫生检查通过（扫描 {result.files_checked} 个文件{exempted_note}）"
            )

    return "\n".join(lines)


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 CI 测试隔离与导入卫生问题（sys.path 污染、顶层模块导入、dual-mode import）"
    )
    parser.add_argument(
        "--scan-dir",
        type=Path,
        default=None,
        help=f"扫描目录（默认: {DEFAULT_TESTS_SCAN_DIR}，向后兼容选项）",
    )
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=None,
        help=f"测试目录（默认: {DEFAULT_TESTS_SCAN_DIR}）",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=None,
        help=f"脚本目录（默认: {DEFAULT_SCRIPTS_SCAN_DIR}）",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认自动检测）",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=f"豁免列表文件路径（默认: {DEFAULT_ALLOWLIST_FILE}）",
    )
    parser.add_argument(
        "--no-allowlist",
        action="store_true",
        help="禁用豁免列表",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式（忽略豁免，所有违规都是错误）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细信息",
    )

    # 扫描模式选项
    scan_group = parser.add_mutually_exclusive_group()
    scan_group.add_argument(
        "--scan-tests",
        action="store_true",
        help="只扫描 tests 目录",
    )
    scan_group.add_argument(
        "--scan-scripts",
        action="store_true",
        help="只扫描 scripts/ci 目录",
    )
    scan_group.add_argument(
        "--scan-all",
        action="store_true",
        help="扫描所有目录（默认行为）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent  # scripts/ci/ 的父父目录

    if not project_root.exists():
        print(f"[ERROR] 项目根目录不存在: {project_root}", file=sys.stderr)
        return 1

    # 加载 allowlist
    allowlist: Allowlist | None = None
    allowlist_path: Path | None = None

    if not args.no_allowlist and not args.strict:
        if args.allowlist:
            allowlist_path = args.allowlist.resolve()
        else:
            allowlist_path = project_root / DEFAULT_ALLOWLIST_FILE

        if allowlist_path.exists():
            allowlist = load_allowlist(allowlist_path)
            if allowlist and args.verbose:
                print(f"[INFO] 已加载豁免列表: {allowlist_path}", file=sys.stderr)
                print(f"       条目数: {len(allowlist.entries)}", file=sys.stderr)
                if allowlist.expired_entries:
                    print(f"       过期条目: {len(allowlist.expired_entries)}", file=sys.stderr)
        elif args.allowlist:
            # 用户明确指定了 allowlist 文件但不存在
            print(f"[WARN] 指定的豁免列表文件不存在: {allowlist_path}", file=sys.stderr)

    # 向后兼容: 如果指定了 --scan-dir，使用原有行为
    if args.scan_dir:
        scan_dir = args.scan_dir.resolve()
        if not scan_dir.exists():
            print(f"[ERROR] 扫描目录不存在: {scan_dir}", file=sys.stderr)
            return 1
        result = check_directory(scan_dir, project_root)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        else:
            output = format_human_readable(result, project_root, scan_dir, None, args.verbose)
            print(output)
        return 1 if result.has_violations() else 0

    # 确定扫描目录
    tests_dir: Path | None = None
    scripts_dir: Path | None = None

    if args.scan_tests:
        tests_dir = (
            args.tests_dir.resolve() if args.tests_dir else project_root / DEFAULT_TESTS_SCAN_DIR
        )
    elif args.scan_scripts:
        scripts_dir = (
            args.scripts_dir.resolve()
            if args.scripts_dir
            else project_root / DEFAULT_SCRIPTS_SCAN_DIR
        )
    else:
        # 默认扫描所有
        tests_dir = (
            args.tests_dir.resolve() if args.tests_dir else project_root / DEFAULT_TESTS_SCAN_DIR
        )
        scripts_dir = (
            args.scripts_dir.resolve()
            if args.scripts_dir
            else project_root / DEFAULT_SCRIPTS_SCAN_DIR
        )

    # 验证目录存在
    if tests_dir and not tests_dir.exists():
        print(f"[WARN] 测试目录不存在: {tests_dir}", file=sys.stderr)
        tests_dir = None

    if scripts_dir and not scripts_dir.exists():
        print(f"[WARN] 脚本目录不存在: {scripts_dir}", file=sys.stderr)
        scripts_dir = None

    if not tests_dir and not scripts_dir:
        print("[ERROR] 没有可扫描的目录", file=sys.stderr)
        return 1

    # 执行检查
    result = check_all(
        tests_dir,
        scripts_dir,
        project_root,
        allowlist=allowlist,
        strict=args.strict,
    )

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        output = format_human_readable(
            result, project_root, tests_dir, scripts_dir, args.verbose, allowlist_path
        )
        print(output)

    # 检查是否有未豁免的违规或过期条目
    if result.has_violations() or result.has_expired_entries():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
