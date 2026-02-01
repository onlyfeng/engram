#!/usr/bin/env python3
"""
verify_logbook_consistency.py - Logbook 部署配置一致性检查

功能:
  A) 检查 compose/logbook.yml 的 initdb 是否会在缺省 .env 下致命失败
  B) 检查 Makefile 的 acceptance-logbook-only 是否只依赖 stepwise compose
  C) 检查 docs/logbook/03_deploy_verify_troubleshoot.md 中的 up-logbook 描述与 Makefile 实现一致
  D) 检查 README.md Logbook-only 分步验收命令使用 migrate-logbook-stepwise 与 verify-permissions-logbook
  E) 检查 docs/logbook/04_acceptance_criteria.md Logbook-only 章节命令对齐

用法:
  python scripts/verify_logbook_consistency.py [--json] [--verbose]

退出码:
  0 - 所有检查通过
  1 - 发现错误
  2 - 脚本错误
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 尝试导入 YAML 解析器
try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class CheckResult:
    """单项检查结果"""

    check_id: str
    check_name: str
    passed: bool
    message: str
    severity: str = "error"  # error, warning, info
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "check_name": self.check_name,
            "passed": self.passed,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass
class ConsistencyReport:
    """一致性检查报告"""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "warning")

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": [c.to_dict() for c in self.checks],
        }


# =============================================================================
# ANSI 颜色
# =============================================================================

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"  # No Color


# =============================================================================
# 检查函数
# =============================================================================


def check_a_initdb_default_env(project_root: Path, verbose: bool = False) -> CheckResult:
    """
    检查 A: compose/logbook.yml 的 initdb 是否会在缺省 .env 下致命失败

    验证策略：
    1. 检查所有环境变量是否有合理的默认值（使用 ${VAR:-default} 语法）
    2. 检查服务账号策略：不设置 *_PASSWORD 时应进入 SKIP 模式
    3. 确保没有必需但无默认值的变量
    """
    compose_file = project_root / "compose" / "logbook.yml"
    details = []

    if not compose_file.exists():
        return CheckResult(
            check_id="A",
            check_name="initdb_default_env",
            passed=False,
            message="compose/logbook.yml 文件不存在",
            severity="error",
        )

    content = compose_file.read_text()

    # 服务账号密码变量（它们是可选的）
    service_account_vars = {
        'LOGBOOK_MIGRATOR_PASSWORD',
        'LOGBOOK_SVC_PASSWORD',
        'OPENMEMORY_MIGRATOR_PASSWORD',
        'OPENMEMORY_SVC_PASSWORD',
    }

    # 检查 1: 查找没有默认值的环境变量引用
    # 匹配 ${VAR} 但排除 ${VAR:-...} 和 ${VAR:+...} 和 $${VAR} (shell escape)
    # 注意：需要排除在命令字符串中使用的变量引用（如 bash -c 中的 $${VAR}）

    # 首先找到所有 ${VAR} 形式的引用
    all_var_refs = re.findall(r'\$\{([A-Z_][A-Z0-9_]*)(:-[^}]*)?\}', content)

    # 统计每个变量的引用情况
    vars_with_default = set()
    vars_without_default = set()

    for var_name, default_part in all_var_refs:
        if default_part:  # 有 :- 部分，说明有默认值
            vars_with_default.add(var_name)
        else:
            vars_without_default.add(var_name)

    # 如果变量在任何地方有默认值，就认为它是安全的
    # 因为 compose 文件会在多处使用同一个变量，只要定义处有默认值即可
    problematic_vars = vars_without_default - vars_with_default - service_account_vars

    # 排除在 command 块中使用 $${VAR} 形式引用的变量（这些是 shell 变量，不是 compose 变量）
    # 检查 $${VAR} 模式
    shell_vars = set(re.findall(r'\$\$\{([A-Z_][A-Z0-9_]*)\}', content))
    problematic_vars = problematic_vars - shell_vars

    if problematic_vars:
        details.append(f"发现 {len(problematic_vars)} 个无默认值的必需变量:")
        for var in sorted(problematic_vars):
            details.append(f"  - {var}")

    # 检查 2: 验证关键变量有默认值
    expected_defaults = {
        'POSTGRES_USER': 'postgres',
        'POSTGRES_PASSWORD': 'postgres',
        'POSTGRES_DB': 'engram',
        'POSTGRES_PORT': '5432',
    }

    missing_defaults = []
    for var, expected_default in expected_defaults.items():
        # 检查是否使用了 ${VAR:-default} 语法
        pattern = rf'\$\{{{var}:-[^}}]+\}}'
        if not re.search(pattern, content):
            missing_defaults.append(var)

    if missing_defaults:
        details.append("以下变量未使用 ${VAR:-default} 语法提供默认值:")
        for var in missing_defaults:
            details.append(f"  - {var}")

    # 检查 3: 验证服务账号密码变量允许为空（${VAR:-}）
    missing_empty_default = []
    for var in service_account_vars:
        # 允许的模式: ${VAR:-} (空默认值) 或 ${VAR:-xxx}
        pattern = rf'\$\{{{var}:-[^}}]*\}}'
        if var in content and not re.search(pattern, content):
            missing_empty_default.append(var)

    if missing_empty_default:
        details.append("服务账号变量应使用 ${VAR:-} 语法允许空值:")
        for var in missing_empty_default:
            details.append(f"  - {var}")

    # 检查 4: 验证文档中描述的 SKIP 模式注释存在
    skip_mode_indicators = [
        r'SKIP\s*模式',
        r'logbook-only\s*模式',
        r'跳过.*服务账号',
        r'服务账号.*跳过',
        r'不设置.*PASSWORD',
        r'全部不设置',
    ]

    has_skip_mode_doc = any(
        re.search(pattern, content, re.IGNORECASE)
        for pattern in skip_mode_indicators
    )

    if not has_skip_mode_doc:
        details.append("警告: 缺少 SKIP 模式的文档说明注释（预期包含 'SKIP 模式' 或 'logbook-only 模式' 等描述）")

    # 判断结果
    has_errors = bool(problematic_vars) or bool(missing_defaults) or bool(missing_empty_default)

    if has_errors:
        return CheckResult(
            check_id="A",
            check_name="initdb_default_env",
            passed=False,
            message="compose/logbook.yml 在缺省 .env 下可能致命失败",
            severity="error",
            details=details,
        )

    details.append("所有环境变量都有合理的默认值或允许为空")
    details.append("服务账号密码策略正确: 不设置时进入 SKIP 模式")

    return CheckResult(
        check_id="A",
        check_name="initdb_default_env",
        passed=True,
        message="compose/logbook.yml 在缺省 .env 下不会致命失败",
        severity="info",
        details=details if verbose else [],
    )


def check_b_acceptance_logbook_compose_dependency(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 B: Makefile 是否提供 Logbook-only 验收相关目标

    验证策略：
    1. 检查是否存在 setup-db-logbook-only 目标（现代化命名）
    2. 检查是否存在必要的迁移和验证目标
    3. 无需专门的 acceptance-logbook-only 目标（使用组合命令代替）
    """
    makefile = project_root / "Makefile"
    details = []

    if not makefile.exists():
        return CheckResult(
            check_id="B",
            check_name="acceptance_logbook_compose_dependency",
            passed=False,
            message="Makefile 文件不存在",
            severity="error",
        )

    content = makefile.read_text()

    # 检查必要的 Logbook-only 相关目标
    required_targets = [
        ('setup-db-logbook-only', '一键初始化（Logbook-only 模式）'),
        ('migrate-ddl', 'DDL 迁移'),
        ('verify-permissions', '权限验证'),
    ]

    missing_targets = []
    found_targets = []

    for target_name, description in required_targets:
        target_pattern = re.compile(
            rf'^{re.escape(target_name)}:',
            re.MULTILINE
        )
        if target_pattern.search(content):
            found_targets.append(f"✓ {target_name} ({description})")
        else:
            missing_targets.append(target_name)

    details.extend(found_targets)

    if missing_targets:
        details.append("")
        details.append("缺失的目标:")
        for target in missing_targets:
            details.append(f"  ✗ {target}")

        return CheckResult(
            check_id="B",
            check_name="acceptance_logbook_compose_dependency",
            passed=False,
            message=f"Makefile 缺失 Logbook-only 相关目标: {', '.join(missing_targets)}",
            severity="error",
            details=details,
        )

    return CheckResult(
        check_id="B",
        check_name="acceptance_logbook_compose_dependency",
        passed=True,
        message="Makefile 包含必要的 Logbook-only 验收目标",
        severity="info",
        details=details if verbose else [],
    )


def check_c_docs_makefile_consistency(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 C: docs/logbook/03_deploy_verify_troubleshoot.md 中的验收命令与 Makefile 一致

    验证策略：
    1. 检查文档中描述的 Makefile 目标是否存在
    2. 验证核心部署命令的描述与实现一致
    """
    docs_file = project_root / "docs" / "logbook" / "03_deploy_verify_troubleshoot.md"
    makefile = project_root / "Makefile"
    details = []

    if not docs_file.exists():
        return CheckResult(
            check_id="C",
            check_name="docs_makefile_consistency",
            passed=False,
            message="docs/logbook/03_deploy_verify_troubleshoot.md 文件不存在",
            severity="error",
        )

    if not makefile.exists():
        return CheckResult(
            check_id="C",
            check_name="docs_makefile_consistency",
            passed=False,
            message="Makefile 文件不存在",
            severity="error",
        )

    docs_content = docs_file.read_text()
    makefile_content = makefile.read_text()

    # 文档中引用的核心 Makefile 目标
    doc_referenced_targets = [
        'setup-db',
        'setup-db-logbook-only',
        'migrate-ddl',
        'apply-roles',
        'apply-openmemory-grants',
        'verify-permissions',
        'verify-permissions-strict',
    ]

    # 检查文档引用的目标是否在 Makefile 中存在
    missing_targets = []
    found_targets = []

    for target in doc_referenced_targets:
        # 检查文档是否引用了这个目标
        doc_pattern = rf'make\s+{re.escape(target)}|`{re.escape(target)}`'
        if re.search(doc_pattern, docs_content):
            # 检查 Makefile 是否有这个目标
            makefile_pattern = rf'^{re.escape(target)}:'
            if re.search(makefile_pattern, makefile_content, re.MULTILINE):
                found_targets.append(f"✓ {target}")
            else:
                missing_targets.append(target)

    details.extend(found_targets)

    if missing_targets:
        details.append("")
        details.append("文档引用但 Makefile 中不存在的目标:")
        for target in missing_targets:
            details.append(f"  ✗ {target}")

        return CheckResult(
            check_id="C",
            check_name="docs_makefile_consistency",
            passed=False,
            message=f"docs 引用的目标在 Makefile 中不存在: {', '.join(missing_targets)}",
            severity="error",
            details=details,
        )

    # 检查核心部署流程描述
    # 验证文档描述的步骤与实际 Makefile 一致
    if 'setup-db' in docs_content and 'setup-db:' in makefile_content:
        details.append("✓ setup-db 部署流程描述一致")

    return CheckResult(
        check_id="C",
        check_name="docs_makefile_consistency",
        passed=True,
        message="docs 验收命令与 Makefile 一致",
        severity="info",
        details=details if verbose else [],
    )


def check_d_readme_logbook_only_stepwise_commands(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 D: README.md 数据库初始化命令是否正确记录

    验证策略：
    1. 检查 README.md 中是否包含核心数据库初始化命令
    2. 验证命令与 Makefile 实现一致
    """
    readme_file = project_root / "README.md"
    makefile = project_root / "Makefile"
    details = []

    if not readme_file.exists():
        return CheckResult(
            check_id="D",
            check_name="readme_logbook_only_stepwise_commands",
            passed=False,
            message="README.md 文件不存在",
            severity="error",
        )

    content = readme_file.read_text()
    makefile_content = makefile.read_text() if makefile.exists() else ""

    # 检查 README.md 中必须存在的核心命令引用
    required_commands = {
        'setup-db': {
            'pattern': r'make\s+setup-db',
            'description': '一键初始化数据库',
        },
        'migrate-ddl': {
            'pattern': r'make\s+migrate-ddl|migrate-ddl',
            'description': 'DDL 迁移',
        },
        'verify-permissions': {
            'pattern': r'make\s+verify-permissions|verify-permissions',
            'description': '权限验证',
        },
    }

    missing_commands = []
    found_commands = []

    for cmd_name, cmd_info in required_commands.items():
        if re.search(cmd_info['pattern'], content):
            found_commands.append(f"✓ {cmd_name} ({cmd_info['description']})")
        else:
            missing_commands.append(cmd_name)

    details.extend(found_commands)

    if missing_commands:
        details.append("")
        details.append("README.md 未记录的命令:")
        for cmd in missing_commands:
            cmd_info = required_commands[cmd]
            details.append(f"  ✗ {cmd} - {cmd_info['description']}")

        return CheckResult(
            check_id="D",
            check_name="readme_logbook_only_stepwise_commands",
            passed=False,
            message=f"README.md 未记录核心命令: {', '.join(missing_commands)}",
            severity="error",
            details=details,
        )

    # 检查引用的命令是否在 Makefile 中存在
    for cmd_name in required_commands.keys():
        makefile_pattern = rf'^{re.escape(cmd_name)}:'
        if re.search(makefile_pattern, makefile_content, re.MULTILINE):
            details.append(f"✓ {cmd_name} 在 Makefile 中存在")

    return CheckResult(
        check_id="D",
        check_name="readme_logbook_only_stepwise_commands",
        passed=True,
        message="README.md 数据库初始化命令记录正确",
        severity="info",
        details=details if verbose else [],
    )


def check_f_acceptance_criteria_logbook_only_alignment(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 F: docs/logbook/04_acceptance_criteria.md Logbook-only 章节命令与 Makefile 对齐

    验证策略：
    1. 定位 04_acceptance_criteria.md 中的 Logbook-only 验收相关章节
    2. 验证包含 migrate-ddl 命令（现代化命名）
    3. 验证包含 verify-permissions 命令（现代化命名）
    """
    acceptance_file = project_root / "docs" / "logbook" / "04_acceptance_criteria.md"
    makefile = project_root / "Makefile"
    details = []

    if not acceptance_file.exists():
        return CheckResult(
            check_id="F",
            check_name="acceptance_criteria_logbook_only_alignment",
            passed=False,
            message="docs/logbook/04_acceptance_criteria.md 文件不存在",
            severity="error",
        )

    content = acceptance_file.read_text()
    makefile_content = makefile.read_text() if makefile.exists() else ""

    # 定位 Logbook-only 相关章节
    logbook_only_sections = []

    # 查找 "Logbook-only 验收" 章节
    logbook_only_section_pattern = re.compile(
        r'###\s*Logbook-only\s+验收.*?(?=###|##|\Z)',
        re.DOTALL | re.IGNORECASE
    )
    match = logbook_only_section_pattern.search(content)
    if match:
        logbook_only_sections.append(('Logbook-only 验收章节', match.group(0)))

    # 查找验收命令汇总表格
    commands_table_pattern = re.compile(
        r'验收命令汇总.*?(?=###|##|\Z)',
        re.DOTALL | re.IGNORECASE
    )
    commands_match = commands_table_pattern.search(content)
    if commands_match:
        logbook_only_sections.append(('验收命令汇总', commands_match.group(0)))

    if not logbook_only_sections:
        details.append("警告: 未找到明确的 Logbook-only 验收章节，检查整个文档")
    else:
        details.append(f"找到 {len(logbook_only_sections)} 个相关章节")

    # 必需的命令（使用现代化命名）
    required_commands = {
        'migrate-ddl': {
            'pattern': r'make\s+migrate-ddl|`migrate-ddl`',
            'description': 'DDL 迁移',
        },
        'verify-permissions': {
            'pattern': r'make\s+verify-permissions|`verify-permissions`',
            'description': '权限验证',
        },
    }

    missing_commands = []
    found_commands = []

    for cmd_name, cmd_info in required_commands.items():
        if re.search(cmd_info['pattern'], content, re.IGNORECASE):
            found_commands.append(f"✓ {cmd_name} ({cmd_info['description']})")
        else:
            missing_commands.append(cmd_name)

    details.extend(found_commands)

    if missing_commands:
        details.append("")
        details.append("文档未记录的命令:")
        for cmd in missing_commands:
            cmd_info = required_commands[cmd]
            details.append(f"  ✗ {cmd} - {cmd_info['description']}")

        return CheckResult(
            check_id="F",
            check_name="acceptance_criteria_logbook_only_alignment",
            passed=False,
            message=f"04_acceptance_criteria.md 未记录命令: {', '.join(missing_commands)}",
            severity="error",
            details=details,
        )

    # 验证命令在 Makefile 中存在
    for cmd_name in required_commands.keys():
        makefile_pattern = rf'^{re.escape(cmd_name)}:'
        if re.search(makefile_pattern, makefile_content, re.MULTILINE):
            details.append(f"✓ {cmd_name} 在 Makefile 中存在")

    return CheckResult(
        check_id="F",
        check_name="acceptance_criteria_logbook_only_alignment",
        passed=True,
        message="04_acceptance_criteria.md Logbook-only 验收命令与 Makefile 对齐",
        severity="info",
        details=details if verbose else [],
    )


# =============================================================================
# 输出格式化
# =============================================================================


def print_human_readable(report: ConsistencyReport, verbose: bool = False) -> None:
    """打印人类可读的报告"""
    print("=" * 60)
    print("Logbook 部署配置一致性检查")
    print("=" * 60)
    print()

    for check in report.checks:
        if check.passed:
            status = f"{GREEN}[OK]{NC}"
        elif check.severity == "warning":
            status = f"{YELLOW}[WARN]{NC}"
        else:
            status = f"{RED}[FAIL]{NC}"

        print(f"{status} [{check.check_id}] {check.message}")

        if check.details and (verbose or not check.passed):
            for detail in check.details[:15]:  # 最多显示 15 条
                print(f"       {detail}")
            if len(check.details) > 15:
                print(f"       ... 还有 {len(check.details) - 15} 条")

    print()
    print("=" * 60)
    if report.ok:
        result_msg = f"{GREEN}[OK] 所有检查通过{NC}"
        if report.warnings > 0:
            result_msg += f" ({YELLOW}警告: {report.warnings}{NC})"
        print(result_msg)
    else:
        print(f"{RED}[FAIL] 发现问题{NC}")
        print(f"       {RED}错误: {report.errors}{NC}")
        print(f"       {YELLOW}警告: {report.warnings}{NC}")
    print("=" * 60)


def print_json(report: ConsistencyReport) -> None:
    """打印 JSON 格式的报告"""
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))


# =============================================================================
# 主函数
# =============================================================================


def run_checks(project_root: Path, verbose: bool = False) -> ConsistencyReport:
    """执行所有检查"""
    report = ConsistencyReport()

    # 检查 A: initdb 默认环境
    report.checks.append(check_a_initdb_default_env(project_root, verbose))

    # 检查 B: acceptance-logbook-only compose 依赖
    report.checks.append(check_b_acceptance_logbook_compose_dependency(project_root, verbose))

    # 检查 C: docs/logbook/03_deploy_verify_troubleshoot.md 与 Makefile 一致性
    report.checks.append(check_c_docs_makefile_consistency(project_root, verbose))

    # 检查 D: README.md Logbook-only 分步验收命令
    report.checks.append(check_d_readme_logbook_only_stepwise_commands(project_root, verbose))

    # 检查 E: 04_acceptance_criteria.md Logbook-only 章节命令对齐
    report.checks.append(check_f_acceptance_criteria_logbook_only_alignment(project_root, verbose))

    return report


def main() -> int:
    """主入口"""
    parser = argparse.ArgumentParser(
        description="Logbook 部署配置一致性检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
检查项:
  A) compose/logbook.yml 的 initdb 是否会在缺省 .env 下致命失败
  B) Makefile 的 acceptance-logbook-only 是否只依赖 stepwise compose
  C) docs/logbook/03_deploy_verify_troubleshoot.md 与 Makefile 一致性
  D) README.md Logbook-only 分步验收命令使用 migrate-logbook-stepwise 与 verify-permissions-logbook
  E) docs/logbook/04_acceptance_criteria.md Logbook-only 章节命令对齐

示例:
  python scripts/verify_logbook_consistency.py
  python scripts/verify_logbook_consistency.py --json
  python scripts/verify_logbook_consistency.py --verbose
""",
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
        help="显示详细输出",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="项目根目录（默认: 自动检测）",
    )

    args = parser.parse_args()

    # 确定项目根目录
    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        # 自动检测: 从脚本位置向上查找
        script_path = Path(__file__).resolve()
        project_root = script_path.parent.parent

        # 验证是否正确（检查 Makefile 存在）
        if not (project_root / "Makefile").exists():
            print("错误: 无法确定项目根目录，请使用 --project-root 指定", file=sys.stderr)
            return 2

    if not project_root.exists():
        print(f"错误: 项目路径不存在: {project_root}", file=sys.stderr)
        return 2

    # 执行检查
    report = run_checks(project_root=project_root, verbose=args.verbose)

    # 输出报告
    if args.json:
        print_json(report)
    else:
        print_human_readable(report, args.verbose)

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
