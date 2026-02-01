#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI 入口点一致性验证脚本

验证 pyproject.toml 中定义的 CLI 入口点与文档、代码引用之间的一致性。

数据来源:
- configs/import_migration_map.json: deprecated 脚本名 → (cli_target, import_target) 映射的 SSOT
- pyproject.toml [project.scripts]: 命令存在性校验来源

检查项:
A) pyproject.toml [project.scripts] 入口点模块可导入
B) docs/architecture/cli_entrypoints.md 对照表与 pyproject.toml 一致
C) docs/ 中引用的 engram-* 命令在 pyproject.toml 中存在
D) src/ 和 tests/ 中无根目录 wrapper 导入（调用 check_no_root_wrappers_usage）
E) subprocess/os.system 调用使用官方 CLI 入口而非根目录脚本
   - 复用 no_root_wrappers_allowlist.json (scope=subprocess) 进行例外管理
   - 过期或无 owner 的例外条目会导致检查失败
F) import_migration_map.json 中的 cli_target 在 pyproject.toml 中存在

用法:
    python scripts/verify_cli_entrypoints_consistency.py [--verbose] [--json]
    python scripts/verify_cli_entrypoints_consistency.py --allowlist-file scripts/ci/no_root_wrappers_allowlist.json

退出码:
    0 - 检查通过
    1 - 发现问题

推荐替代方案:
    根据 configs/import_migration_map.json 中的 cli_target 字段确定推荐替代命令。
    所有推荐的 CLI 命令已验证存在于 pyproject.toml [project.scripts]。
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Allowlist 默认路径
DEFAULT_ALLOWLIST_FILE = "scripts/ci/no_root_wrappers_allowlist.json"

# Import migration map 路径
IMPORT_MIGRATION_MAP_FILE = "configs/import_migration_map.json"


def load_import_migration_map(project_root: Path) -> tuple[Dict[str, Any], Optional[str]]:
    """
    从 configs/import_migration_map.json 加载 deprecated 脚本映射

    Returns:
        (data, error): 成功返回 (dict, None)，失败返回 ({}, error_message)
    """
    map_path = project_root / IMPORT_MIGRATION_MAP_FILE
    if not map_path.exists():
        return {}, f"Import migration map 不存在: {map_path}"

    try:
        with open(map_path, encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return {}, f"Import migration map JSON 解析失败: {e}"
    except OSError as e:
        return {}, f"读取 import migration map 失败: {e}"


def build_deprecated_script_alternatives(
    migration_map: Dict[str, Any],
    console_scripts: Dict[str, str],
) -> Dict[str, Dict[str, str]]:
    """
    根据 import_migration_map.json 构建 deprecated 脚本 → 替代方案映射

    只包含 deprecated=true 且有有效 cli_target 的条目。
    cli_target 优先使用 pyproject.toml 中存在的命令。

    Args:
        migration_map: import_migration_map.json 的内容
        console_scripts: pyproject.toml [project.scripts] 的内容

    Returns:
        Dict[script_name.py, {"console_script": str, "module": str}]
    """
    result: Dict[str, Dict[str, str]] = {}

    modules = migration_map.get("modules", [])
    for entry in modules:
        if not entry.get("deprecated", False):
            continue

        old_module = entry.get("old_module", "")
        if not old_module:
            continue

        script_name = f"{old_module}.py"
        cli_target = entry.get("cli_target")
        import_target = entry.get("import_target")

        # 解析 cli_target，提取命令名称（可能包含子命令）
        # 例如: "engram-artifacts gc" -> 命令是 "engram-artifacts"
        #       "engram-scm-runner" -> 命令是 "engram-scm-runner"
        console_script = None
        module_invoke = None

        if cli_target:
            # 提取基础命令（第一个空格前的部分）
            base_cmd = cli_target.split()[0] if cli_target else None
            # 如果基础命令在 pyproject.toml 中存在，使用它
            if base_cmd and base_cmd in console_scripts:
                console_script = cli_target
            elif cli_target.startswith("scripts/"):
                # 对于 scripts/ 开头的路径，保持原样
                console_script = cli_target
            else:
                # cli_target 不在 pyproject.toml 中，跳过或标记为 N/A
                console_script = None

        if import_target:
            # 构建 python -m 调用
            # import_target 格式: "engram.logbook.cli.artifacts:main"
            # 转换为: "python -m engram.logbook.cli.artifacts"
            module_path = import_target.split(":")[0] if ":" in import_target else import_target
            module_invoke = f"python -m {module_path}"

        # 只有当有有效替代方案时才添加
        if console_script or module_invoke:
            result[script_name] = {
                "console_script": console_script or "N/A",
                "module": module_invoke or "N/A",
            }

    return result


# 延迟初始化的全局变量（将在验证器中动态加载）
DEPRECATED_SCRIPT_ALTERNATIVES: Dict[str, Dict[str, str]] = {}


# ============================================================================
# Allowlist 辅助函数
# ============================================================================


def load_allowlist(allowlist_path: Path) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    加载 allowlist JSON 文件

    Returns:
        (data, error): 成功返回 (dict, None)，失败返回 (None, error_message)
    """
    if not allowlist_path.exists():
        return None, f"Allowlist 文件不存在: {allowlist_path}"
    try:
        with open(allowlist_path, encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"Allowlist JSON 解析失败: {e}"
    except OSError as e:
        return None, f"读取 allowlist 文件失败: {e}"


def get_subprocess_allowlist_entries(
    allowlist_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    从 allowlist 中提取 scope=subprocess 的条目

    Returns:
        scope=subprocess 的条目列表
    """
    entries = allowlist_data.get("entries", [])
    return [e for e in entries if e.get("scope") == "subprocess"]


def is_allowlist_entry_valid(entry: Dict[str, Any]) -> tuple[bool, str]:
    """
    检查 allowlist 条目是否有效（未过期且有 owner）

    Returns:
        (is_valid, reason): is_valid=True 表示有效，reason 说明无效原因
    """
    # 检查 owner
    owner = entry.get("owner", "").strip()
    if not owner:
        return False, "owner 字段为空"

    # 检查 expires_on
    expires_on = entry.get("expires_on")
    if expires_on:
        try:
            expiry_date = date.fromisoformat(expires_on)
            if date.today() > expiry_date:
                return False, f"条目已过期: {expires_on}"
        except ValueError:
            return False, f"expires_on 日期格式无效: {expires_on}"

    return True, ""


def check_subprocess_call_allowlisted(
    file_path: str,
    script_name: str,
    subprocess_entries: List[Dict[str, Any]],
) -> tuple[bool, Optional[Dict[str, Any]], str]:
    """
    检查 subprocess 调用是否在 allowlist 中

    Args:
        file_path: 调用所在文件的相对路径
        script_name: 被调用的脚本名（如 "db_migrate.py"）
        subprocess_entries: scope=subprocess 的 allowlist 条目

    Returns:
        (is_allowed, matched_entry, reason):
        - is_allowed: True 表示允许（在 allowlist 中且有效）
        - matched_entry: 匹配的 allowlist 条目（如果有）
        - reason: 不允许的原因或允许的说明
    """
    # 从脚本名提取模块名（去掉 .py）
    module_name = script_name.replace(".py", "")

    for entry in subprocess_entries:
        entry_module = entry.get("module", "")
        file_glob = entry.get("file_glob", "")
        file_path_exact = entry.get("file_path", "")

        # 检查模块是否匹配
        if entry_module != module_name:
            continue

        # 检查文件是否匹配（精确路径优先，否则用 glob）
        if file_path_exact:
            if file_path != file_path_exact and not file_path.endswith(file_path_exact):
                continue
        elif file_glob:
            if not fnmatch.fnmatch(file_path, file_glob):
                continue
        else:
            continue

        # 找到匹配的条目，检查是否有效
        is_valid, reason = is_allowlist_entry_valid(entry)
        if is_valid:
            return True, entry, f"allowlist 例外: {entry.get('id')}"
        else:
            return False, entry, f"allowlist 条目无效 ({entry.get('id')}): {reason}"

    return False, None, "不在 allowlist 中"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class CheckResult:
    """检查结果"""

    check_id: str
    name: str
    passed: bool
    message: str
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class VerificationReport:
    """验证报告"""

    results: List[CheckResult] = field(default_factory=list)
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0

    def add_result(self, result: CheckResult) -> None:
        self.results.append(result)
        self.total_checks += 1
        if result.passed:
            self.passed_checks += 1
        else:
            self.failed_checks += 1

    def is_success(self) -> bool:
        return self.failed_checks == 0

    def to_dict(self) -> dict:
        return {
            "ok": self.is_success(),
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "results": [r.to_dict() for r in self.results],
        }


# ============================================================================
# 验证器
# ============================================================================


class CLIEntrypointsConsistencyVerifier:
    """CLI 入口点一致性验证器"""

    def __init__(
        self,
        verbose: bool = False,
        allowlist_file: Optional[Path] = None,
    ):
        self.verbose = verbose
        self.report = VerificationReport()
        self.pyproject_data: Optional[Dict] = None
        self.console_scripts: Dict[str, str] = {}
        self.allowlist_file = allowlist_file or (PROJECT_ROOT / DEFAULT_ALLOWLIST_FILE)
        self.allowlist_data: Optional[Dict[str, Any]] = None
        self.subprocess_entries: List[Dict[str, Any]] = []
        self.migration_map: Dict[str, Any] = {}
        self.deprecated_script_alternatives: Dict[str, Dict[str, str]] = {}

    def log(self, message: str) -> None:
        """输出日志"""
        if self.verbose:
            print(f"  {message}")

    def load_pyproject(self) -> bool:
        """加载 pyproject.toml"""
        pyproject_path = PROJECT_ROOT / "pyproject.toml"
        if not pyproject_path.exists():
            return False

        with open(pyproject_path, "rb") as f:
            self.pyproject_data = tomllib.load(f)

        # 提取 console scripts
        self.console_scripts = (
            self.pyproject_data.get("project", {}).get("scripts", {})
        )
        return True

    def load_migration_map(self) -> bool:
        """
        加载 import_migration_map.json 并构建 deprecated 脚本替代方案映射

        Returns:
            True 如果加载成功，False 如果加载失败
        """
        data, error = load_import_migration_map(PROJECT_ROOT)
        if error:
            self.log(f"[WARN] {error}")
            return False

        self.migration_map = data
        # 构建 deprecated 脚本替代方案映射
        self.deprecated_script_alternatives = build_deprecated_script_alternatives(
            data, self.console_scripts
        )
        # 更新全局变量（用于 check_subprocess_call_allowlisted 等函数）
        global DEPRECATED_SCRIPT_ALTERNATIVES
        DEPRECATED_SCRIPT_ALTERNATIVES = self.deprecated_script_alternatives

        self.log(
            f"[INFO] 从 import_migration_map.json 加载了 "
            f"{len(self.deprecated_script_alternatives)} 个 deprecated 脚本映射"
        )
        return True

    def load_allowlist(self) -> bool:
        """
        加载 subprocess allowlist

        Returns:
            True 如果加载成功（或文件不存在视为空 allowlist），False 如果加载失败
        """
        if not self.allowlist_file.exists():
            self.log(f"[INFO] Allowlist 文件不存在: {self.allowlist_file}，将不使用例外")
            self.allowlist_data = {"version": "1", "entries": []}
            self.subprocess_entries = []
            return True

        data, error = load_allowlist(self.allowlist_file)
        if error:
            self.log(f"[WARN] {error}")
            self.allowlist_data = {"version": "1", "entries": []}
            self.subprocess_entries = []
            return True  # 不因 allowlist 加载失败而阻断

        self.allowlist_data = data
        self.subprocess_entries = get_subprocess_allowlist_entries(data)
        self.log(f"[INFO] 加载了 {len(self.subprocess_entries)} 个 scope=subprocess 的 allowlist 条目")
        return True

    def check_a_entrypoints_importable(self) -> CheckResult:
        """
        检查 A: pyproject.toml [project.scripts] 入口点模块可导入
        """
        check_id = "A"
        name = "入口点模块可导入"

        if not self.console_scripts:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="pyproject.toml 中未定义 console scripts",
            )

        import_errors: List[str] = []
        checked_count = 0

        for cmd, entry_point in self.console_scripts.items():
            if ":" not in entry_point:
                import_errors.append(f"{cmd}: 入口点格式错误 '{entry_point}'")
                continue

            module_path, func_name = entry_point.rsplit(":", 1)
            checked_count += 1

            try:
                spec = importlib.util.find_spec(module_path)
                if spec is None:
                    import_errors.append(f"{cmd}: 模块 {module_path} 未找到")
                else:
                    self.log(f"[OK] {cmd} -> {entry_point}")
            except ModuleNotFoundError as e:
                import_errors.append(f"{cmd}: {e}")
            except Exception as e:
                import_errors.append(f"{cmd}: 导入错误 - {e}")

        if import_errors:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"{len(import_errors)} 个入口点模块无法导入",
                details=import_errors[:10],
            )

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message=f"所有 {checked_count} 个入口点模块可导入",
        )

    def check_b_cli_entrypoints_doc_consistency(self) -> CheckResult:
        """
        检查 B: docs/architecture/cli_entrypoints.md 对照表与 pyproject.toml 一致
        """
        check_id = "B"
        name = "CLI 入口对照表与 pyproject.toml 一致"

        cli_doc_path = PROJECT_ROOT / "docs" / "architecture" / "cli_entrypoints.md"

        if not cli_doc_path.exists():
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="cli_entrypoints.md 文件不存在",
            )

        if not self.console_scripts:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="无法加载 pyproject.toml 中的 console scripts",
            )

        content = cli_doc_path.read_text(encoding="utf-8")

        # 从文档表格中提取命令和入口模块
        # 匹配格式: | `engram-xxx` | `module.path:func` | ... |
        table_pattern = re.compile(
            r"\|\s*`?(engram-[\w-]+)`?\s*\|\s*`?([\w.]+:\w+)`?\s*\|"
        )

        documented_entries: Dict[str, str] = {}
        for match in table_pattern.finditer(content):
            cmd, entry_point = match.groups()
            cmd = cmd.strip("`")
            entry_point = entry_point.strip("`")
            documented_entries[cmd] = entry_point

        if not documented_entries:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="未能从 cli_entrypoints.md 中解析出入口点表格",
            )

        # 对比文档与 pyproject.toml
        issues: List[str] = []

        # 检查文档中列出的命令
        for cmd, doc_entry in documented_entries.items():
            actual_entry = self.console_scripts.get(cmd)

            if actual_entry is None:
                issues.append(f"文档列出 {cmd}，但 pyproject.toml 中未定义")
            elif actual_entry != doc_entry:
                issues.append(
                    f"{cmd}: 文档={doc_entry}, pyproject.toml={actual_entry}"
                )
            else:
                self.log(f"[OK] {cmd} -> {doc_entry}")

        # 检查 pyproject.toml 中有但文档未列出的命令
        undocumented: List[str] = []
        for cmd in self.console_scripts:
            if cmd.startswith("engram-") and cmd not in documented_entries:
                undocumented.append(cmd)

        if undocumented:
            issues.append(
                f"pyproject.toml 中定义但文档未列出: {', '.join(undocumented)}"
            )

        if issues:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"发现 {len(issues)} 处不一致",
                details=issues,
            )

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message=f"CLI 入口对照表与 pyproject.toml 一致 ({len(documented_entries)} 个入口)",
        )

    def check_c_docs_command_references(self) -> CheckResult:
        """
        检查 C: docs/ 中引用的 engram-* 命令在 pyproject.toml 中存在
        """
        check_id = "C"
        name = "文档中引用的命令存在于 pyproject.toml"

        if not self.console_scripts:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="无法加载 pyproject.toml",
            )

        docs_dir = PROJECT_ROOT / "docs"
        if not docs_dir.exists():
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=True,
                message="docs/ 目录不存在，跳过检查",
            )

        # 匹配 engram-* 命令模式，但排除 @engram- 开头的（owner 标识如 @engram-team）
        # 使用负向后行断言排除 @ 前缀
        command_pattern = re.compile(r"(?<!@)\b(engram-[\w-]+)\b")

        # 已知的有效命令
        valid_commands = set(self.console_scripts.keys())

        # 已知的非 CLI 命令模式（systemd 服务名、日志文件名等）
        # 这些在文档中出现但不是 pyproject.toml 中定义的 CLI 入口
        non_cli_patterns = {
            "engram-xxx",      # 示例占位符
            "engram-example",  # 示例占位符
            "engram-outbox",   # systemd 服务名/日志文件名，非 CLI 入口
            "engram-team",     # owner 标识的一部分
            "engram-sql-scripts",  # Kubernetes ConfigMap 名称
        }

        # 以 engram-v 开头的通常是版本号（如 engram-v1.2.3），而非 CLI 命令
        def is_version_pattern(cmd: str) -> bool:
            """检查是否为版本号模式（如 engram-v1.2.3）"""
            import re
            return bool(re.match(r"^engram-v\d", cmd))

        # 扫描文档
        unknown_commands: Dict[str, Set[str]] = {}  # command -> files

        for md_file in docs_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

            relative_path = str(md_file.relative_to(PROJECT_ROOT))
            matches = command_pattern.findall(content)

            for cmd in set(matches):
                if cmd not in valid_commands:
                    # 排除已知的非 CLI 命令模式
                    if cmd in non_cli_patterns:
                        continue
                    # 排除版本号模式（如 engram-v1.2.3）
                    if is_version_pattern(cmd):
                        continue
                    if cmd not in unknown_commands:
                        unknown_commands[cmd] = set()
                    unknown_commands[cmd].add(relative_path)

        if unknown_commands:
            details: List[str] = []
            for cmd, files in sorted(unknown_commands.items()):
                files_str = ", ".join(sorted(files)[:3])
                if len(files) > 3:
                    files_str += f" 等 {len(files)} 个文件"
                details.append(f"{cmd}: 出现在 {files_str}")

            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"发现 {len(unknown_commands)} 个未定义的命令引用",
                details=details[:10],
            )

        self.log(f"扫描了 {len(list(docs_dir.rglob('*.md')))} 个文档文件")

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message="文档中引用的所有 engram-* 命令均已定义",
        )

    def check_d_no_root_wrappers_usage(self) -> CheckResult:
        """
        检查 D: src/ 和 tests/ 中无根目录 wrapper 导入

        调用 scripts/ci/check_no_root_wrappers_usage.py 进行检查
        """
        check_id = "D"
        name = "无根目录 wrapper 导入"

        check_script = PROJECT_ROOT / "scripts" / "ci" / "check_no_root_wrappers_usage.py"

        if not check_script.exists():
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="check_no_root_wrappers_usage.py 脚本不存在",
            )

        try:
            result = subprocess.run(
                [sys.executable, str(check_script), "--json"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=PROJECT_ROOT,
            )

            # 解析 JSON 输出
            try:
                output = json.loads(result.stdout)
                if output.get("ok", False):
                    return CheckResult(
                        check_id=check_id,
                        name=name,
                        passed=True,
                        message=f"扫描 {output.get('files_scanned', 0)} 个文件，无违规导入",
                    )
                else:
                    violations = output.get("violations", [])
                    details = [
                        f"{v['file']}:{v['line_number']}: {v['module']}"
                        for v in violations[:10]
                    ]
                    return CheckResult(
                        check_id=check_id,
                        name=name,
                        passed=False,
                        message=f"发现 {output.get('violation_count', 0)} 处违规导入",
                        details=details,
                    )
            except json.JSONDecodeError:
                # 如果 JSON 解析失败，根据退出码判断
                if result.returncode == 0:
                    return CheckResult(
                        check_id=check_id,
                        name=name,
                        passed=True,
                        message="根目录 wrapper 导入检查通过",
                    )
                else:
                    return CheckResult(
                        check_id=check_id,
                        name=name,
                        passed=False,
                        message="根目录 wrapper 导入检查失败",
                        details=[result.stdout[:500] if result.stdout else result.stderr[:500]],
                    )

        except subprocess.TimeoutExpired:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="检查脚本执行超时",
            )
        except Exception as e:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"执行检查脚本时出错: {e}",
            )

    def check_e_subprocess_calls_use_official_cli(self) -> CheckResult:
        """
        检查 E: subprocess/os.system 调用使用官方 CLI 入口而非根目录脚本

        复用 no_root_wrappers_allowlist.json (scope=subprocess) 进行例外管理。
        若发现 subprocess 调用命中 deprecated root 脚本：
        - 先查 allowlist
        - 过期/无 owner 则 FAIL
        - 提供推荐替代方案
        """
        check_id = "E"
        name = "subprocess 调用使用官方 CLI"

        # 加载 allowlist（如果尚未加载）
        if self.allowlist_data is None:
            self.load_allowlist()

        # 根目录脚本列表（从实例变量获取，已从 import_migration_map.json 构建）
        deprecated_scripts = list(self.deprecated_script_alternatives.keys())

        # 构建检测模式：匹配 subprocess.run/call/Popen, os.system/popen 等调用
        # 使用捕获组来提取被调用的脚本名
        subprocess_call_pattern = re.compile(
            r"(?:subprocess\.(?:run|call|Popen|check_output|check_call)"
            r"|os\.(?:system|popen|execv?p?)"
            r"|Popen)\s*\("
        )

        # 扫描目录
        scan_dirs = [
            PROJECT_ROOT / "src",
            PROJECT_ROOT / "tests",
        ]

        violations: List[str] = []  # 未被 allowlist 覆盖的违规
        allowlisted: List[str] = []  # 被 allowlist 覆盖的调用
        expired_or_invalid: List[str] = []  # allowlist 条目过期或无效
        files_scanned = 0

        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue

            for py_file in scan_dir.rglob("*.py"):
                files_scanned += 1
                try:
                    content = py_file.read_text(encoding="utf-8")
                except Exception:
                    continue

                relative_path = str(py_file.relative_to(PROJECT_ROOT))

                for line_num, line in enumerate(content.splitlines(), 1):
                    # 跳过注释
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue

                    # 检查是否是 subprocess/os 调用
                    if not subprocess_call_pattern.search(line):
                        continue

                    # 检查是否调用了 deprecated 脚本
                    matched_script = None
                    for script in deprecated_scripts:
                        if script in line:
                            matched_script = script
                            break

                    if not matched_script:
                        continue

                    # 发现调用 deprecated 脚本，检查 allowlist
                    is_allowed, entry, reason = check_subprocess_call_allowlisted(
                        relative_path,
                        matched_script,
                        self.subprocess_entries,
                    )

                    # 获取推荐替代方案（从实例变量）
                    alt = self.deprecated_script_alternatives.get(matched_script, {})
                    alt_console = alt.get("console_script", "N/A")
                    alt_module = alt.get("module", "N/A")

                    location = f"{relative_path}:{line_num}"
                    code_snippet = stripped[:60] + ("..." if len(stripped) > 60 else "")

                    if is_allowed:
                        # 在 allowlist 中且有效
                        allowlisted.append(
                            f"{location}: {matched_script} (allowlist: {entry.get('id') if entry else 'N/A'})"
                        )
                        self.log(f"[ALLOWLIST] {location}: {matched_script}")
                    elif entry is not None:
                        # 在 allowlist 中但条目无效（过期或无 owner）
                        expired_or_invalid.append(
                            f"{location}: {matched_script} - {reason}\n"
                            f"    推荐替代: {alt_console} 或 {alt_module}"
                        )
                    else:
                        # 不在 allowlist 中
                        violations.append(
                            f"{location}: {code_snippet}\n"
                            f"    脚本: {matched_script}\n"
                            f"    推荐替代: {alt_console} 或 {alt_module}"
                        )

        # 汇总结果
        total_issues = len(violations) + len(expired_or_invalid)
        details: List[str] = []

        if expired_or_invalid:
            details.append("=== 过期/无效的 allowlist 条目 ===")
            details.extend(expired_or_invalid[:5])
            if len(expired_or_invalid) > 5:
                details.append(f"... 还有 {len(expired_or_invalid) - 5} 处")

        if violations:
            details.append("=== 未被 allowlist 覆盖的违规 ===")
            details.extend(violations[:5])
            if len(violations) > 5:
                details.append(f"... 还有 {len(violations) - 5} 处")

        if allowlisted and self.verbose:
            self.log(f"[INFO] {len(allowlisted)} 处 subprocess 调用被 allowlist 覆盖")

        if total_issues > 0:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=(
                    f"发现 {total_issues} 处问题: "
                    f"{len(violations)} 处未覆盖, {len(expired_or_invalid)} 处 allowlist 无效"
                ),
                details=details[:15],
            )

        self.log(f"扫描了 {files_scanned} 个 Python 文件")

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message=(
                f"扫描 {files_scanned} 个文件，"
                f"未发现问题 ({len(allowlisted)} 处被 allowlist 覆盖)"
            ),
        )

    def check_f_migration_map_cli_targets_exist(self) -> CheckResult:
        """
        检查 F: import_migration_map.json 中的 cli_target 在 pyproject.toml 中存在

        确保迁移映射中引用的 CLI 命令是有效的 console scripts。
        """
        check_id = "F"
        name = "migration_map cli_target 有效"

        if not self.migration_map:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="import_migration_map.json 未加载或为空",
            )

        if not self.console_scripts:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="pyproject.toml console scripts 未加载",
            )

        issues: List[str] = []
        valid_count = 0
        skipped_count = 0

        modules = self.migration_map.get("modules", [])
        for entry in modules:
            if not entry.get("deprecated", False):
                continue

            old_module = entry.get("old_module", "")
            cli_target = entry.get("cli_target")

            if not cli_target:
                skipped_count += 1
                continue

            # 跳过 scripts/ 路径（不是 console script）
            if cli_target.startswith("scripts/"):
                self.log(f"[SKIP] {old_module}: {cli_target} (scripts/ 路径)")
                skipped_count += 1
                continue

            # 提取基础命令（第一个空格前的部分）
            # 例如: "engram-artifacts gc" -> "engram-artifacts"
            base_cmd = cli_target.split()[0]

            if base_cmd in self.console_scripts:
                valid_count += 1
                self.log(f"[OK] {old_module}: {cli_target}")
            else:
                issues.append(
                    f"{old_module}: cli_target='{cli_target}' "
                    f"(命令 '{base_cmd}' 不在 pyproject.toml [project.scripts] 中)"
                )

        if issues:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"发现 {len(issues)} 个无效的 cli_target",
                details=issues[:10],
            )

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message=(
                f"所有 cli_target 有效 "
                f"({valid_count} 个验证通过, {skipped_count} 个跳过)"
            ),
        )

    def run_all_checks(self) -> VerificationReport:
        """运行所有检查"""
        # 加载 pyproject.toml
        if not self.load_pyproject():
            print("[ERROR] 无法加载 pyproject.toml", file=sys.stderr)
            self.report.add_result(
                CheckResult(
                    check_id="INIT",
                    name="加载 pyproject.toml",
                    passed=False,
                    message="无法加载 pyproject.toml",
                )
            )
            return self.report

        # 加载 import_migration_map.json（用于检查 E 和 F）
        if not self.load_migration_map():
            self.log("[WARN] 无法加载 import_migration_map.json，部分检查可能不完整")

        # 加载 allowlist（用于检查 E）
        self.load_allowlist()

        # 运行检查
        checks = [
            ("A", "入口点模块可导入", self.check_a_entrypoints_importable),
            ("B", "CLI 入口对照表一致", self.check_b_cli_entrypoints_doc_consistency),
            ("C", "文档命令引用有效", self.check_c_docs_command_references),
            ("D", "无根目录 wrapper 导入", self.check_d_no_root_wrappers_usage),
            ("E", "subprocess 使用官方 CLI", self.check_e_subprocess_calls_use_official_cli),
            ("F", "migration_map cli_target 有效", self.check_f_migration_map_cli_targets_exist),
        ]

        for check_id, desc, check_func in checks:
            try:
                result = check_func()
                self.report.add_result(result)
            except Exception as e:
                result = CheckResult(
                    check_id=check_id,
                    name=desc,
                    passed=False,
                    message=f"检查过程中出错: {e}",
                )
                self.report.add_result(result)

        return self.report

    def print_report(self) -> None:
        """打印人类可读的报告"""
        print("=" * 70)
        print("CLI 入口点一致性验证")
        print("=" * 70)
        print()
        print(f"项目根目录: {PROJECT_ROOT}")
        print(f"Console Scripts 数量: {len(self.console_scripts)}")
        print()

        for result in self.report.results:
            status = "PASS" if result.passed else "FAIL"
            print(f"[{result.check_id}] {result.name}")
            print(f"    [{status}] {result.message}")

            if not result.passed and result.details:
                for detail in result.details[:5]:
                    print(f"      - {detail}")
                if len(result.details) > 5:
                    print(f"      ... 还有 {len(result.details) - 5} 项")
            print()

        print("-" * 70)
        print("验证结果汇总")
        print("-" * 70)
        print(f"总检查数: {self.report.total_checks}")
        print(f"通过: {self.report.passed_checks}")
        print(f"失败: {self.report.failed_checks}")
        print()

        if self.report.is_success():
            print("[OK] 所有检查通过")
        else:
            print("[FAIL] 存在检查失败项")


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    """主函数"""
    parser = argparse.ArgumentParser(
        description="CLI 入口点一致性验证脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
数据来源:
  - configs/import_migration_map.json: deprecated 脚本映射的 SSOT
  - pyproject.toml [project.scripts]: 命令存在性校验来源

检查项:
  A) pyproject.toml [project.scripts] 入口点模块可导入
  B) docs/architecture/cli_entrypoints.md 对照表与 pyproject.toml 一致
  C) docs/ 中引用的 engram-* 命令在 pyproject.toml 中存在
  D) src/ 和 tests/ 中无根目录 wrapper 导入
  E) subprocess/os.system 调用使用官方 CLI 入口而非根目录脚本
     - 复用 no_root_wrappers_allowlist.json (scope=subprocess) 进行例外管理
     - 过期或无 owner 的例外条目会导致检查失败
  F) import_migration_map.json 中的 cli_target 在 pyproject.toml 中存在

推荐替代方案:
  推荐的 CLI 命令基于 configs/import_migration_map.json，示例:
  - scm_sync_runner.py   -> engram-scm-runner
  - scm_sync_scheduler.py -> engram-scm-scheduler
  - db_migrate.py        -> engram-migrate
  - db_bootstrap.py      -> engram-bootstrap-roles
  - artifact_cli.py      -> engram-artifacts
  - logbook_cli.py       -> engram-logbook

示例:
  python scripts/verify_cli_entrypoints_consistency.py
  python scripts/verify_cli_entrypoints_consistency.py --verbose
  python scripts/verify_cli_entrypoints_consistency.py --json
  python scripts/verify_cli_entrypoints_consistency.py --allowlist-file scripts/ci/no_root_wrappers_allowlist.json
        """,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 格式输出",
    )
    parser.add_argument(
        "--allowlist-file",
        type=Path,
        default=None,
        help=f"Allowlist 文件路径（默认: {DEFAULT_ALLOWLIST_FILE}）",
    )

    args = parser.parse_args()

    # 处理 allowlist 路径
    allowlist_file = None
    if args.allowlist_file:
        if args.allowlist_file.is_absolute():
            allowlist_file = args.allowlist_file
        else:
            allowlist_file = PROJECT_ROOT / args.allowlist_file

    verifier = CLIEntrypointsConsistencyVerifier(
        verbose=args.verbose,
        allowlist_file=allowlist_file,
    )
    report = verifier.run_all_checks()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        verifier.print_report()

    return 0 if report.is_success() else 1


if __name__ == "__main__":
    sys.exit(main())
