#!/usr/bin/env python3
"""
根目录 wrapper 禁止导入检查脚本

扫描 src/**/*.py 与 tests/**/*.py，禁止出现对根目录 wrappers 的 import。

禁止的导入模式:
- import scm_sync_runner
- from scm_sync_runner import ...
- 等根目录 wrapper 模块

这些根目录脚本是遗留的 CLI 入口 wrapper，新代码应该：
1. 直接使用 engram.logbook.* 或 engram.gateway.* 下的模块
2. 使用 pyproject.toml 中定义的 console_scripts 入口

例外机制（两种方式）:
1. Allowlist 引用: `# ROOT-WRAPPER-ALLOW: <allowlist_id>`
   - allowlist 文件中定义的有效条目
   - 适用于需要集中管理的持久例外

2. Inline 声明: `# ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>`
   - 直接在代码中声明例外
   - 必须包含过期日期和负责人
   - 过期后视为违规，CI 将报错

模块分类:
- ROOT_WRAPPER_MODULES: 明确弃用且计划移除的模块
- LONG_TERM_PRESERVED_MODULES: 长期保留的模块（不在禁止列表中）

详见 docs/architecture/cli_entrypoints.md

用法:
    python scripts/ci/check_no_root_wrappers_usage.py [--verbose] [--json]
    python scripts/ci/check_no_root_wrappers_usage.py --allowlist-file path/to/allowlist.json

退出码:
    0 - 检查通过，无违规
    1 - 发现违规导入或过期的 inline marker
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ============================================================================
# 配置区
# ============================================================================

# SSOT 映射文件路径（相对于项目根）
IMPORT_MIGRATION_MAP_FILE = "configs/import_migration_map.json"

# ============================================================================
# 模块分类定义 - 从 SSOT 文件加载
# ============================================================================


def _load_migration_map(project_root: Path) -> Dict[str, Any]:
    """
    从 SSOT 文件加载迁移映射数据

    Args:
        project_root: 项目根目录

    Returns:
        JSON 数据字典，如果加载失败则返回空字典
    """
    map_path = project_root / IMPORT_MIGRATION_MAP_FILE
    if not map_path.exists():
        print(
            f"[WARN] 迁移映射文件不存在: {map_path}，使用内置默认值",
            file=sys.stderr,
        )
        return {}
    try:
        with open(map_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"[WARN] 无法加载迁移映射文件: {e}，使用内置默认值",
            file=sys.stderr,
        )
        return {}


def _extract_module_lists(
    migration_data: Dict[str, Any],
) -> Tuple[List[str], Dict[str, str], List[str]]:
    """
    从迁移映射数据中提取模块列表

    Returns:
        (root_wrapper_modules, migration_map, long_term_preserved_modules)
    """
    root_wrapper_modules: List[str] = []
    migration_map: Dict[str, str] = {}
    long_term_preserved_modules: List[str] = []

    for entry in migration_data.get("modules", []):
        old_module = entry.get("old_module", "")
        if not old_module:
            continue

        deprecated = entry.get("deprecated", True)
        import_target = entry.get("import_target")
        cli_target = entry.get("cli_target")

        if deprecated:
            root_wrapper_modules.append(old_module)
            # 构建迁移建议字符串
            suggestions = []
            if import_target:
                suggestions.append(import_target)
            if cli_target:
                suggestions.append(cli_target)
            if suggestions:
                migration_map[old_module] = " 或 ".join(suggestions)
        else:
            long_term_preserved_modules.append(old_module)

    return root_wrapper_modules, migration_map, long_term_preserved_modules


# 内置默认值（当 SSOT 文件不可用时使用）
_DEFAULT_ROOT_WRAPPER_MODULES: List[str] = [
    # SCM 同步相关（根目录脚本已移除，scripts/ 下的弃用警告入口）
    "scm_sync_runner",
    "scm_sync_scheduler",
    "scm_sync_status",
    "scm_sync_reaper",
    "scm_sync_worker",
    "scm_sync_gitlab_commits",
    "scm_sync_gitlab_mrs",
    "scm_sync_svn",
    "scm_materialize_patch_blob",
    # Artifact 相关（待移除，使用 engram-artifacts 替代）
    "artifact_audit",
    "artifact_cli",
    "artifact_gc",
    "artifact_migrate",
    # 数据库相关（待移除，使用 engram-migrate / engram-bootstrap-roles 替代）
    "db_bootstrap",
    "db_migrate",
    # Logbook CLI 相关（待移除，使用 engram-logbook 替代）
    "logbook_cli_main",
    "logbook_cli",
    # 其他（待评估迁移路径）
    "identity_sync",
]

_DEFAULT_MIGRATION_MAP: Dict[str, str] = {
    # SCM Sync 相关
    "scm_sync_runner": "engram.logbook.cli.scm_sync:runner_main 或 engram-scm-runner",
    "scm_sync_scheduler": "engram.logbook.cli.scm_sync:scheduler_main 或 engram-scm-scheduler",
    "scm_sync_status": "engram.logbook.cli.scm_sync:status_main 或 engram-scm-status",
    "scm_sync_reaper": "engram.logbook.cli.scm_sync:reaper_main 或 engram-scm-reaper",
    "scm_sync_worker": "engram.logbook.cli.scm_sync:worker_main 或 engram-scm-worker",
    "scm_sync_gitlab_commits": "engram-scm-sync runner incremental --repo gitlab:<id>",
    "scm_sync_gitlab_mrs": "engram-scm-sync runner incremental --repo gitlab:<id> --job mrs",
    "scm_sync_svn": "engram-scm-sync runner incremental --repo svn:<id>",
    "scm_materialize_patch_blob": "engram.logbook.materialize_patch_blob",
    # Artifact 相关
    "artifact_audit": "scripts/artifact_audit.py",
    "artifact_cli": "engram.logbook.cli.artifacts:main 或 engram-artifacts",
    "artifact_gc": "engram.logbook.cli.artifacts gc 或 engram-artifacts gc",
    "artifact_migrate": "engram.logbook.cli.artifacts migrate 或 engram-artifacts migrate",
    # 数据库相关
    "db_bootstrap": "engram.logbook.cli.db_bootstrap:main 或 engram-bootstrap-roles",
    "db_migrate": "engram.logbook.cli.db_migrate:main 或 engram-migrate",
    # Logbook CLI 相关
    "logbook_cli_main": "engram.logbook.cli.logbook:main 或 engram-logbook",
    "logbook_cli": "engram.logbook.cli.logbook:main 或 engram-logbook",
    # 其他
    "identity_sync": "engram.logbook.identity_sync:main 或 engram-identity-sync",
}

_DEFAULT_LONG_TERM_PRESERVED_MODULES: List[str] = [
    "db",        # 数据库连接工具模块，被多个脚本依赖
    "kv",        # KV 存储工具模块，被多个脚本依赖
    "artifacts", # SCM 路径与制品工具，工具模块保留
]

# 模块级变量（延迟初始化，由 main() 填充）
# 详见 docs/architecture/no_root_wrappers_migration_map.md
ROOT_WRAPPER_MODULES: List[str] = []
MIGRATION_MAP: Dict[str, str] = {}
LONG_TERM_PRESERVED_MODULES: List[str] = []


def initialize_module_lists(project_root: Path) -> None:
    """
    从 SSOT 文件初始化模块列表

    如果 SSOT 文件不可用，则使用内置默认值。
    """
    global ROOT_WRAPPER_MODULES, MIGRATION_MAP, LONG_TERM_PRESERVED_MODULES

    migration_data = _load_migration_map(project_root)

    if migration_data and migration_data.get("modules"):
        ROOT_WRAPPER_MODULES, MIGRATION_MAP, LONG_TERM_PRESERVED_MODULES = (
            _extract_module_lists(migration_data)
        )
    else:
        # 使用内置默认值
        ROOT_WRAPPER_MODULES = _DEFAULT_ROOT_WRAPPER_MODULES.copy()
        MIGRATION_MAP = _DEFAULT_MIGRATION_MAP.copy()
        LONG_TERM_PRESERVED_MODULES = _DEFAULT_LONG_TERM_PRESERVED_MODULES.copy()

# 扫描目标目录（相对于项目根）
SCAN_DIRECTORIES: List[str] = [
    "src",
    "tests",
]

# 允许例外的标记格式
# 支持两种格式：
# 1. Allowlist 引用: # ROOT-WRAPPER-ALLOW: <allowlist_id>
# 2. Inline 声明:    # ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>
ROOT_WRAPPER_ALLOW_MARKER = "# ROOT-WRAPPER-ALLOW:"

# Inline marker 正则模式: <reason>; expires=YYYY-MM-DD; owner=<team>
INLINE_MARKER_PATTERN = re.compile(
    r"^(?P<reason>[^;]+);\s*expires=(?P<expires>\d{4}-\d{2}-\d{2});\s*owner=(?P<owner>\S+)$"
)

# 默认 allowlist 文件路径（相对于项目根）
DEFAULT_ALLOWLIST_FILE = "scripts/ci/no_root_wrappers_allowlist.json"

# Schema 文件路径（相对于项目根）
ALLOWLIST_SCHEMA_FILE = "schemas/no_root_wrappers_allowlist_v1.schema.json"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class InlineMarker:
    """Inline 例外标记（代码行内声明）"""

    reason: str
    expires: str
    owner: str

    def is_expired(self) -> bool:
        """检查标记是否已过期"""
        try:
            expiry_date = date.fromisoformat(self.expires)
            return date.today() > expiry_date
        except ValueError:
            # 无效日期格式视为过期
            return True


@dataclass
class AllowlistEntry:
    """Allowlist 条目"""

    id: str
    file_pattern: str
    module: str
    owner: str
    expiry: str
    reason: str
    ticket: Optional[str] = None

    def is_expired(self) -> bool:
        """检查条目是否已过期"""
        try:
            expiry_date = date.fromisoformat(self.expiry)
            return date.today() > expiry_date
        except ValueError:
            # 无效日期格式视为过期
            return True

    def matches(self, file_path: str, module: str) -> bool:
        """检查文件路径和模块是否匹配此条目"""
        if self.module != module:
            return False
        # 支持 glob 模式匹配
        return fnmatch.fnmatch(file_path, self.file_pattern)


@dataclass
class AllowedHit:
    """被 allowlist 放行的命中记录"""

    file: str
    line_number: int
    line_content: str
    module: str
    allowlist_id: str


@dataclass
class InlineAllowedHit:
    """被 inline marker 放行的命中记录"""

    file: str
    line_number: int
    line_content: str
    module: str
    reason: str
    expires: str
    owner: str


@dataclass
class Violation:
    """单个违规记录"""

    file: str
    line_number: int
    line_content: str
    module: str
    message: str


@dataclass
class CheckResult:
    """检查结果"""

    violations: List[Violation] = field(default_factory=list)
    allowed_hits: List[AllowedHit] = field(default_factory=list)
    inline_allowed_hits: List[InlineAllowedHit] = field(default_factory=list)
    expired_allowlist_entries: List[str] = field(default_factory=list)
    expired_inline_markers: List[Dict[str, Any]] = field(default_factory=list)
    invalid_inline_markers: List[Dict[str, Any]] = field(default_factory=list)
    missing_metadata_entries: List[str] = field(default_factory=list)
    invalid_marker_refs: List[Dict[str, Any]] = field(default_factory=list)
    files_scanned: int = 0
    files_with_violations: Set[str] = field(default_factory=set)

    def has_violations(self) -> bool:
        # 过期的 inline marker 也视为违规
        return len(self.violations) > 0 or len(self.expired_inline_markers) > 0

    def to_dict(self) -> dict:
        return {
            "ok": not self.has_violations(),
            "violation_count": len(self.violations),
            "expired_inline_marker_count": len(self.expired_inline_markers),
            "files_scanned": self.files_scanned,
            "files_with_violations": sorted(self.files_with_violations),
            "allowed_hits": [
                {
                    "file": h.file,
                    "line_number": h.line_number,
                    "line_content": h.line_content.strip(),
                    "module": h.module,
                    "allowlist_id": h.allowlist_id,
                }
                for h in self.allowed_hits
            ],
            "inline_allowed_hits": [
                {
                    "file": h.file,
                    "line_number": h.line_number,
                    "line_content": h.line_content.strip(),
                    "module": h.module,
                    "reason": h.reason,
                    "expires": h.expires,
                    "owner": h.owner,
                }
                for h in self.inline_allowed_hits
            ],
            "expired_allowlist_entries": self.expired_allowlist_entries,
            "expired_inline_markers": self.expired_inline_markers,
            "invalid_inline_markers": self.invalid_inline_markers,
            "missing_metadata_entries": self.missing_metadata_entries,
            "invalid_marker_refs": self.invalid_marker_refs,
            "violations": [
                {
                    "file": v.file,
                    "line_number": v.line_number,
                    "line_content": v.line_content.strip(),
                    "module": v.module,
                    "message": v.message,
                }
                for v in self.violations
            ],
        }


# ============================================================================
# Allowlist 处理
# ============================================================================


def load_allowlist_schema(project_root: Path) -> Optional[Dict[str, Any]]:
    """加载 allowlist schema"""
    schema_path = project_root / ALLOWLIST_SCHEMA_FILE
    if not schema_path.exists():
        return None
    try:
        with open(schema_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def validate_allowlist_entry(entry: Dict[str, Any]) -> tuple[bool, List[str]]:
    """
    校验单个 allowlist 条目是否包含必要字段

    返回 (is_valid, missing_fields)
    """
    required_fields = ["id", "file_pattern", "module", "owner", "expiry", "reason"]
    missing = [f for f in required_fields if f not in entry or not entry[f]]
    return len(missing) == 0, missing


def load_allowlist(
    allowlist_path: Path,
    result: CheckResult,
    verbose: bool = False,
) -> Dict[str, AllowlistEntry]:
    """
    加载并校验 allowlist 文件

    返回 {id: AllowlistEntry} 映射
    """
    entries: Dict[str, AllowlistEntry] = {}

    if not allowlist_path.exists():
        if verbose:
            print(f"[INFO] Allowlist 文件不存在: {allowlist_path}", file=sys.stderr)
        return entries

    try:
        with open(allowlist_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Allowlist JSON 解析失败: {e}", file=sys.stderr)
        return entries
    except OSError as e:
        print(f"[ERROR] 无法读取 allowlist 文件: {e}", file=sys.stderr)
        return entries

    # 校验 version
    if data.get("version") != "1":
        print(
            f"[WARN] Allowlist version 不匹配，期望 '1'，实际 '{data.get('version')}'",
            file=sys.stderr,
        )

    # 处理每个条目
    for entry_data in data.get("entries", []):
        entry_id = entry_data.get("id", "<unknown>")

        # 校验必要字段
        is_valid, missing_fields = validate_allowlist_entry(entry_data)
        if not is_valid:
            result.missing_metadata_entries.append(entry_id)
            if verbose:
                print(
                    f"[WARN] Allowlist 条目 '{entry_id}' 缺少必要字段: {missing_fields}",
                    file=sys.stderr,
                )
            continue

        entry = AllowlistEntry(
            id=entry_data["id"],
            file_pattern=entry_data["file_pattern"],
            module=entry_data["module"],
            owner=entry_data["owner"],
            expiry=entry_data["expiry"],
            reason=entry_data["reason"],
            ticket=entry_data.get("ticket"),
        )

        # 检查是否过期
        if entry.is_expired():
            result.expired_allowlist_entries.append(entry_id)
            if verbose:
                print(
                    f"[WARN] Allowlist 条目 '{entry_id}' 已过期 (expiry: {entry.expiry})",
                    file=sys.stderr,
                )
            # 过期条目不加入有效条目映射
            continue

        entries[entry.id] = entry

    return entries


def _extract_marker_content(line: str) -> Optional[str]:
    """从行中提取 ROOT-WRAPPER-ALLOW: 后的内容"""
    if ROOT_WRAPPER_ALLOW_MARKER not in line:
        return None
    marker_pos = line.find(ROOT_WRAPPER_ALLOW_MARKER)
    content = line[marker_pos + len(ROOT_WRAPPER_ALLOW_MARKER) :].strip()
    return content if content else None


def parse_inline_marker(content: str) -> Optional[InlineMarker]:
    """
    解析 inline marker 内容

    格式: <reason>; expires=YYYY-MM-DD; owner=<team>
    """
    match = INLINE_MARKER_PATTERN.match(content)
    if not match:
        return None
    return InlineMarker(
        reason=match.group("reason").strip(),
        expires=match.group("expires"),
        owner=match.group("owner"),
    )


def extract_marker(
    lines: List[str], line_index: int
) -> tuple[Optional[str], Optional[InlineMarker]]:
    """
    提取行中 ROOT-WRAPPER-ALLOW 标记

    支持两种格式:
    1. Allowlist 引用: # ROOT-WRAPPER-ALLOW: <allowlist_id>
    2. Inline 声明:    # ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>

    返回:
    - (allowlist_id, None): 如果是 allowlist 引用
    - (None, InlineMarker): 如果是 inline 声明
    - (None, None): 没有找到标记

    检查位置:
    - 当前行末尾: import foo  # ROOT-WRAPPER-ALLOW: ...
    - 上一行注释: # ROOT-WRAPPER-ALLOW: ...
    """
    current_line = lines[line_index]

    # 检查当前行是否包含标记
    content = _extract_marker_content(current_line)
    if content is None and line_index > 0:
        # 检查上一行是否是允许标记注释行
        prev_line = lines[line_index - 1].strip()
        content = _extract_marker_content(prev_line)

    if content is None:
        return None, None

    # 尝试解析为 inline marker
    inline_marker = parse_inline_marker(content)
    if inline_marker:
        return None, inline_marker

    # 否则视为 allowlist id（第一个非空 token）
    allowlist_id = content.split()[0] if content else None
    return allowlist_id, None


def extract_marker_id(lines: List[str], line_index: int) -> Optional[str]:
    """
    提取行中 ROOT-WRAPPER-ALLOW 标记引用的 allowlist id

    兼容旧接口，仅返回 allowlist id。对于 inline marker 返回 None。

    支持格式:
    - 当前行末尾: import foo  # ROOT-WRAPPER-ALLOW: some-id
    - 上一行注释: # ROOT-WRAPPER-ALLOW: some-id
    """
    allowlist_id, _ = extract_marker(lines, line_index)
    return allowlist_id


def check_allowlist_match(
    file_path: str,
    module: str,
    marker_id: Optional[str],
    allowlist: Dict[str, AllowlistEntry],
) -> Optional[AllowlistEntry]:
    """
    检查是否匹配有效的 allowlist 条目

    匹配逻辑:
    1. 如果有 marker_id，必须在 allowlist 中存在且文件/模块匹配
    2. 否则检查是否有匹配文件/模块的条目
    """
    if marker_id:
        # 有 marker，必须匹配指定的 allowlist id
        entry = allowlist.get(marker_id)
        if entry and entry.matches(file_path, module):
            return entry
        return None

    # 无 marker，检查是否有任意匹配的条目
    for entry in allowlist.values():
        if entry.matches(file_path, module):
            return entry

    return None


# ============================================================================
# AST 导入检测
# ============================================================================


@dataclass
class ImportInfo:
    """导入语句信息"""

    module: str
    line_number: int
    line_content: str
    is_type_checking: bool = False


class ImportVisitor(ast.NodeVisitor):
    """
    AST 访问器，提取所有导入语句

    优点（相比正则表达式）:
    - 正确忽略字符串中的伪 import（如 docstring、注释字符串）
    - 正确处理多行 from-import（括号包裹的导入列表）
    - 正确处理 TYPE_CHECKING 块内的导入
    """

    def __init__(
        self, lines: List[str], forbidden_modules: Set[str], source_code: str
    ):
        self.lines = lines
        self.forbidden_modules = forbidden_modules
        self.source_code = source_code
        self.imports: List[ImportInfo] = []
        self._type_checking_ranges: List[Tuple[int, int]] = []
        self._precompute_type_checking_blocks()

    def _precompute_type_checking_blocks(self) -> None:
        """预计算 TYPE_CHECKING 块的行范围"""
        try:
            tree = ast.parse(self.source_code)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # 检查是否是 if TYPE_CHECKING: 或 if typing.TYPE_CHECKING:
                test = node.test
                is_type_checking = False

                if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                    is_type_checking = True
                elif isinstance(test, ast.Attribute):
                    if (
                        test.attr == "TYPE_CHECKING"
                        and isinstance(test.value, ast.Name)
                        and test.value.id == "typing"
                    ):
                        is_type_checking = True

                if is_type_checking:
                    # 计算 TYPE_CHECKING 块覆盖的行范围
                    start_line = node.lineno
                    end_line = node.end_lineno or node.lineno
                    # 包含 body 中所有语句的范围
                    for stmt in node.body:
                        if hasattr(stmt, "end_lineno") and stmt.end_lineno:
                            end_line = max(end_line, stmt.end_lineno)
                    self._type_checking_ranges.append((start_line, end_line))

    def _is_in_type_checking(self, lineno: int) -> bool:
        """检查给定行号是否在 TYPE_CHECKING 块内"""
        for start, end in self._type_checking_ranges:
            if start < lineno <= end:
                return True
        return False

    def _get_line_content(self, lineno: int) -> str:
        """获取指定行的内容"""
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def visit_Import(self, node: ast.Import) -> None:
        """处理 import xxx 语句"""
        for alias in node.names:
            # 获取顶层模块名（如 import a.b.c 时取 a）
            top_module = alias.name.split(".")[0]
            if top_module in self.forbidden_modules:
                self.imports.append(
                    ImportInfo(
                        module=top_module,
                        line_number=node.lineno,
                        line_content=self._get_line_content(node.lineno),
                        is_type_checking=self._is_in_type_checking(node.lineno),
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """处理 from xxx import ... 语句"""
        if node.module:
            # 获取顶层模块名
            top_module = node.module.split(".")[0]
            if top_module in self.forbidden_modules:
                self.imports.append(
                    ImportInfo(
                        module=top_module,
                        line_number=node.lineno,
                        line_content=self._get_line_content(node.lineno),
                        is_type_checking=self._is_in_type_checking(node.lineno),
                    )
                )
        self.generic_visit(node)


def extract_imports_via_ast(
    source_code: str, forbidden_modules: Set[str]
) -> List[ImportInfo]:
    """
    使用 AST 从源代码中提取禁止的导入

    Args:
        source_code: Python 源代码内容
        forbidden_modules: 禁止导入的模块集合

    Returns:
        ImportInfo 列表，包含所有禁止模块的导入信息
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        # 语法错误时返回空列表（由其他工具处理语法问题）
        return []

    lines = source_code.splitlines()
    visitor = ImportVisitor(lines, forbidden_modules, source_code)
    visitor.visit(tree)
    return visitor.imports


def get_migration_suggestion(module: str) -> str:
    """获取模块的迁移建议"""
    suggestion = MIGRATION_MAP.get(module)
    if suggestion:
        return f"建议改用: {suggestion}"
    return "请改用 engram.logbook.* 或 engram.gateway.* 下的模块"


def scan_file(
    file_path: Path,
    relative_path: str,
    forbidden_modules: Set[str],
    allowlist: Dict[str, AllowlistEntry],
    result: CheckResult,
) -> None:
    """
    扫描单个文件中的违规导入

    使用 AST 解析，正确处理:
    - 字符串中的伪 import（自动忽略）
    - 多行 from-import（括号包裹的导入列表）
    - TYPE_CHECKING 块内的导入（自动跳过）
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return

    lines = content.splitlines()

    # 使用 AST 提取禁止的导入
    imports = extract_imports_via_ast(content, forbidden_modules)

    for import_info in imports:
        line_number = import_info.line_number
        line_index = line_number - 1
        line = import_info.line_content
        module = import_info.module

        # 跳过 TYPE_CHECKING 块内的 import
        if import_info.is_type_checking:
            continue

        # 提取标记（支持 allowlist 引用和 inline 声明）
        marker_id, inline_marker = extract_marker(lines, line_index)

        # 检查是否有 inline marker
        if inline_marker:
            if inline_marker.is_expired():
                # Inline marker 已过期，视为违规
                result.expired_inline_markers.append(
                    {
                        "file": relative_path,
                        "line_number": line_number,
                        "line_content": line.strip(),
                        "module": module,
                        "reason": inline_marker.reason,
                        "expires": inline_marker.expires,
                        "owner": inline_marker.owner,
                    }
                )
                result.files_with_violations.add(relative_path)
            else:
                # Inline marker 有效，放行
                result.inline_allowed_hits.append(
                    InlineAllowedHit(
                        file=relative_path,
                        line_number=line_number,
                        line_content=line,
                        module=module,
                        reason=inline_marker.reason,
                        expires=inline_marker.expires,
                        owner=inline_marker.owner,
                    )
                )
            continue  # 处理下一个导入

        # 检查是否有无效的 marker 引用（marker 存在但不在 allowlist 中）
        if marker_id and marker_id not in allowlist:
            # 检查是否是格式错误的 inline marker
            marker_content = _extract_marker_content(line)
            if marker_content is None and line_index > 0:
                marker_content = _extract_marker_content(lines[line_index - 1].strip())
            if marker_content and ";" in marker_content:
                # 可能是格式错误的 inline marker
                result.invalid_inline_markers.append(
                    {
                        "file": relative_path,
                        "line_number": line_number,
                        "marker_content": marker_content,
                        "reason": (
                            "Inline marker 格式错误，正确格式: "
                            "# ROOT-WRAPPER-ALLOW: <reason>; "
                            "expires=YYYY-MM-DD; owner=<team>"
                        ),
                    }
                )
            else:
                result.invalid_marker_refs.append(
                    {
                        "file": relative_path,
                        "line_number": line_number,
                        "marker_id": marker_id,
                        "reason": "marker 引用的 allowlist id 不存在或已过期",
                    }
                )

        # 检查 allowlist 匹配
        matched_entry = check_allowlist_match(
            relative_path, module, marker_id, allowlist
        )

        if matched_entry:
            # 被 allowlist 放行
            result.allowed_hits.append(
                AllowedHit(
                    file=relative_path,
                    line_number=line_number,
                    line_content=line,
                    module=module,
                    allowlist_id=matched_entry.id,
                )
            )
        else:
            # 违规 - 提供精准的迁移建议
            migration_hint = get_migration_suggestion(module)
            result.violations.append(
                Violation(
                    file=relative_path,
                    line_number=line_number,
                    line_content=line,
                    module=module,
                    message=(
                        f"禁止导入根目录 wrapper 模块 '{module}'。"
                        f"{migration_hint}"
                    ),
                )
            )
            result.files_with_violations.add(relative_path)


def scan_directories(
    project_root: Path,
    allowlist: Dict[str, AllowlistEntry],
    verbose: bool = False,
) -> CheckResult:
    """扫描指定目录中的所有 Python 文件"""
    result = CheckResult()
    forbidden_modules = set(ROOT_WRAPPER_MODULES)

    for scan_dir in SCAN_DIRECTORIES:
        dir_path = project_root / scan_dir
        if not dir_path.exists():
            if verbose:
                print(f"[WARN] 目录不存在: {dir_path}", file=sys.stderr)
            continue

        # 递归查找所有 .py 文件
        py_files = list(dir_path.rglob("*.py"))
        result.files_scanned += len(py_files)

        for py_file in py_files:
            relative_path = str(py_file.relative_to(project_root))
            scan_file(py_file, relative_path, forbidden_modules, allowlist, result)

    return result


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 src/ 和 tests/ 中禁止的根目录 wrapper 模块导入"
    )
    parser.add_argument(
        "--verbose",
        "-v",
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
    parser.add_argument(
        "--allowlist-file",
        type=Path,
        default=None,
        help=f"Allowlist 文件路径（默认: {DEFAULT_ALLOWLIST_FILE}）",
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

    # 从 SSOT 文件初始化模块列表
    initialize_module_lists(project_root)

    # 确定 allowlist 文件路径
    if args.allowlist_file:
        allowlist_path = args.allowlist_file.resolve()
    else:
        allowlist_path = project_root / DEFAULT_ALLOWLIST_FILE

    # 创建临时 result 用于收集 allowlist 加载时的警告
    temp_result = CheckResult()

    # 加载 allowlist
    allowlist = load_allowlist(allowlist_path, temp_result, verbose=args.verbose)

    # 执行扫描
    result = scan_directories(project_root, allowlist, verbose=args.verbose)

    # 合并 allowlist 加载时发现的问题
    result.expired_allowlist_entries = temp_result.expired_allowlist_entries
    result.missing_metadata_entries = temp_result.missing_metadata_entries

    # 输出结果
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("根目录 Wrapper 导入检查")
        print("=" * 70)
        print()
        print(f"项目根目录: {project_root}")
        print(f"Allowlist 文件: {allowlist_path}")
        print(f"扫描目录: {', '.join(SCAN_DIRECTORIES)}")
        print(f"扫描文件数: {result.files_scanned}")
        print(f"禁止的模块数: {len(ROOT_WRAPPER_MODULES)}")
        print(f"有效 Allowlist 条目数: {len(allowlist)}")
        print()

        # 显示 allowlist 相关警告
        if result.expired_allowlist_entries:
            print(f"[WARN] 过期的 Allowlist 条目: {result.expired_allowlist_entries}")
        if result.missing_metadata_entries:
            print(
                f"[WARN] 缺少必要字段的条目: {result.missing_metadata_entries}"
            )
        if result.invalid_marker_refs:
            print(f"[WARN] 无效的 marker 引用: {len(result.invalid_marker_refs)} 处")
            for ref in result.invalid_marker_refs:
                print(f"  {ref['file']}:{ref['line_number']} - {ref['marker_id']}")
        if result.invalid_inline_markers:
            print(
                f"[WARN] 格式错误的 inline marker: {len(result.invalid_inline_markers)} 处"
            )
            for m in result.invalid_inline_markers:
                print(f"  {m['file']}:{m['line_number']}")
                if args.verbose:
                    print(f"    内容: {m['marker_content']}")
                    print(f"    原因: {m['reason']}")

        if (
            result.expired_allowlist_entries
            or result.missing_metadata_entries
            or result.invalid_marker_refs
            or result.invalid_inline_markers
        ):
            print()

        # 显示被放行的命中
        if result.allowed_hits and args.verbose:
            print(f"[INFO] 被 Allowlist 放行的导入: {len(result.allowed_hits)} 处")
            for h in result.allowed_hits:
                print(f"  {h.file}:{h.line_number} [{h.allowlist_id}]")
            print()

        if result.inline_allowed_hits and args.verbose:
            print(
                f"[INFO] 被 Inline Marker 放行的导入: {len(result.inline_allowed_hits)} 处"
            )
            for h in result.inline_allowed_hits:
                print(f"  {h.file}:{h.line_number}")
                print(f"    原因: {h.reason}")
                print(f"    过期: {h.expires}, 负责人: {h.owner}")
            print()

        # 显示过期的 inline marker（视为违规）
        if result.expired_inline_markers:
            print(
                f"[ERROR] 发现 {len(result.expired_inline_markers)} 处过期的 Inline Marker:"
            )
            print()
            for m in result.expired_inline_markers:
                print(f"  {m['file']}:{m['line_number']}")
                print(f"    模块: {m['module']}")
                print(f"    代码: {m['line_content']}")
                print(f"    原因: {m['reason']}")
                print(f"    过期日期: {m['expires']}, 负责人: {m['owner']}")
                print()

        if not result.has_violations():
            print("[OK] 未发现禁止的根目录 wrapper 导入")
        else:
            if result.violations:
                print(f"[ERROR] 发现 {len(result.violations)} 处违规导入:")
                print()

                for v in result.violations:
                    print(f"  {v.file}:{v.line_number}")
                    print(f"    模块: {v.module}")
                    print(f"    代码: {v.line_content.strip()}")
                    if args.verbose:
                        print(f"    说明: {v.message}")
                    print()

        print("-" * 70)
        print(f"违规总数: {len(result.violations)}")
        print(f"过期 Inline Marker: {len(result.expired_inline_markers)}")
        print(f"涉及文件: {len(result.files_with_violations)}")
        total_allowed = len(result.allowed_hits) + len(result.inline_allowed_hits)
        print(f"被放行数: {total_allowed} (Allowlist: {len(result.allowed_hits)}, Inline: {len(result.inline_allowed_hits)})")
        print()

        if result.has_violations():
            print("[FAIL] 根目录 wrapper 导入检查失败")
            print()
            print("修复建议:")
            print()
            # 收集所有违规模块并显示精准迁移路径
            violated_modules = set(v.module for v in result.violations)
            for mod in sorted(violated_modules):
                suggestion = MIGRATION_MAP.get(mod)
                if suggestion:
                    print(f"  • {mod} -> {suggestion}")
                else:
                    print(f"  • {mod} -> engram.logbook.* 或 engram.gateway.* 下的对应模块")
            print()
            print("  如确有需要，可选择以下方式添加例外:")
            print("    a) 在 allowlist 文件中添加条目并使用 '# ROOT-WRAPPER-ALLOW: <allowlist_id>'")
            print("    b) 使用 inline marker: '# ROOT-WRAPPER-ALLOW: <reason>; expires=YYYY-MM-DD; owner=<team>'")
            if result.expired_inline_markers:
                print()
                print("  过期的 inline marker 需要更新过期日期或移除依赖")
        else:
            print("[OK] 根目录 wrapper 导入检查通过")

    # 退出码
    return 1 if result.has_violations() else 0


if __name__ == "__main__":
    sys.exit(main())
