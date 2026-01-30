#!/usr/bin/env python3
"""
verify_logbook_consistency.py - Logbook 部署配置一致性检查

功能:
  A) 检查 compose/logbook.yml 的 initdb 是否会在缺省 .env 下致命失败
  B) 检查 Makefile 的 acceptance-logbook-only 是否只依赖 stepwise compose
  C) 检查 verify-permissions 是否按 SEEKDB_ENABLE 注入 seek.enabled
  D) 检查 docs/logbook/03_deploy_verify_troubleshoot.md 中的 up-logbook 描述与 Makefile 实现一致
  E) 检查 README.md Logbook-only 分步验收命令使用 migrate-logbook-stepwise 与 verify-permissions-logbook
  F) 检查 docs/logbook/04_acceptance_criteria.md Logbook-only 章节命令对齐

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
        details.append(f"以下变量未使用 ${{VAR:-default}} 语法提供默认值:")
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
        details.append(f"服务账号变量应使用 ${{VAR:-}} 语法允许空值:")
        for var in missing_empty_default:
            details.append(f"  - {var}")
    
    # 检查 4: 验证文档中描述的 SKIP 模式注释存在
    skip_mode_indicators = [
        'SKIP',
        '不设置.*PASSWORD',
        '服务账号创建被跳过',
    ]
    
    has_skip_mode_doc = any(
        re.search(pattern, content, re.IGNORECASE) 
        for pattern in skip_mode_indicators
    )
    
    if not has_skip_mode_doc:
        details.append("警告: 缺少 SKIP 模式的文档说明注释")
    
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
    检查 B: Makefile 的 acceptance-logbook-only 是否只依赖 stepwise compose
    
    验证策略：
    1. 检查 acceptance-logbook-only 目标的实现
    2. 确认它只使用 $(LOGBOOK_COMPOSE)（即 compose/logbook.yml）
    3. 确认它不使用 $(DOCKER_COMPOSE)（即 docker-compose.unified.yml）
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
    
    # 找到 acceptance-logbook-only 目标的实现
    # Makefile 目标格式: target: deps \n\t commands
    acceptance_pattern = re.compile(
        r'^acceptance-logbook-only:.*?(?=^[a-zA-Z_-]+:|^\Z)',
        re.MULTILINE | re.DOTALL
    )
    
    match = acceptance_pattern.search(content)
    if not match:
        return CheckResult(
            check_id="B",
            check_name="acceptance_logbook_compose_dependency",
            passed=False,
            message="未找到 acceptance-logbook-only 目标",
            severity="error",
        )
    
    target_content = match.group(0)
    
    # 检查使用的 compose 变量
    uses_logbook_compose = '$(LOGBOOK_COMPOSE)' in target_content or 'LOGBOOK_COMPOSE' in target_content
    uses_docker_compose = '$(DOCKER_COMPOSE)' in target_content
    
    # 检查调用的子目标
    # 从 Makefile 内容中提取子目标的 compose 使用
    subtargets = [
        'up-logbook',
        'migrate-logbook-stepwise',
        'verify-permissions-logbook',
        'logbook-smoke',
        'test-logbook-unit',
    ]
    
    subtarget_compose_usage = {}
    for subtarget in subtargets:
        # 查找子目标定义
        subtarget_pattern = re.compile(
            rf'^{re.escape(subtarget)}:.*?(?=^[a-zA-Z_-]+:|^\Z)',
            re.MULTILINE | re.DOTALL
        )
        subtarget_match = subtarget_pattern.search(content)
        if subtarget_match:
            subtarget_content = subtarget_match.group(0)
            uses_unified = '$(DOCKER_COMPOSE)' in subtarget_content
            uses_stepwise = '$(LOGBOOK_COMPOSE)' in subtarget_content
            subtarget_compose_usage[subtarget] = {
                'unified': uses_unified,
                'stepwise': uses_stepwise,
            }
    
    # 分析结果
    # 策略说明：
    #   - 仅使用 DOCKER_COMPOSE → 不适用于 logbook-only（FAIL）
    #   - 同时使用两者 → 检测模式，可接受（先检查 LOGBOOK_COMPOSE，再 DOCKER_COMPOSE）
    #   - 仅使用 LOGBOOK_COMPOSE → 最佳实践
    #   - 两者都不用 → 无 compose 依赖（如 test-logbook-unit）
    mixed_usage = []
    for subtarget, usage in subtarget_compose_usage.items():
        if usage['unified'] and not usage['stepwise']:
            mixed_usage.append(f"{subtarget} 使用 $(DOCKER_COMPOSE) 而非 $(LOGBOOK_COMPOSE)")
        # 同时使用两者是可接受的检测模式（先检查 logbook-only，再检查 unified）
        # elif usage['unified'] and usage['stepwise']:
        #     mixed_usage.append(f"{subtarget} 混用了两种 compose")
        details.append(f"{subtarget}: stepwise={usage['stepwise']}, unified={usage['unified']}")
    
    if mixed_usage:
        details.extend(mixed_usage)
        return CheckResult(
            check_id="B",
            check_name="acceptance_logbook_compose_dependency",
            passed=False,
            message="acceptance-logbook-only 的子目标混用了 compose 文件",
            severity="error",
            details=details,
        )
    
    # 验证 LOGBOOK_COMPOSE 定义指向 compose/logbook.yml
    logbook_compose_def = re.search(
        r'LOGBOOK_COMPOSE\s*:?=\s*docker\s+compose.*-f\s+(\S+)',
        content
    )
    
    if logbook_compose_def:
        compose_file = logbook_compose_def.group(1)
        if 'compose/logbook.yml' in compose_file or '$(LOGBOOK_COMPOSE_FILE)' in compose_file:
            details.append(f"LOGBOOK_COMPOSE 正确指向 stepwise compose 文件")
        else:
            details.append(f"警告: LOGBOOK_COMPOSE 指向 {compose_file}")
    
    return CheckResult(
        check_id="B",
        check_name="acceptance_logbook_compose_dependency",
        passed=True,
        message="acceptance-logbook-only 只依赖 stepwise compose (compose/logbook.yml)",
        severity="info",
        details=details if verbose else [],
    )


def check_c_verify_permissions_seekdb_enable(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 C: verify-permissions 是否按 SEEKDB_ENABLE 注入 seek.enabled
    
    验证策略：
    1. 检查 verify-permissions 目标中是否有 SET seek.enabled 命令
    2. 验证它根据 SEEKDB_ENABLE_EFFECTIVE 设置 true/false
    """
    makefile = project_root / "Makefile"
    details = []
    
    if not makefile.exists():
        return CheckResult(
            check_id="C",
            check_name="verify_permissions_seekdb_enable",
            passed=False,
            message="Makefile 文件不存在",
            severity="error",
        )
    
    content = makefile.read_text()
    
    # 找到 verify-permissions 目标的实现
    verify_pattern = re.compile(
        r'^verify-permissions:.*?(?=^[a-zA-Z_-]+:|^\Z)',
        re.MULTILINE | re.DOTALL
    )
    
    match = verify_pattern.search(content)
    if not match:
        return CheckResult(
            check_id="C",
            check_name="verify_permissions_seekdb_enable",
            passed=False,
            message="未找到 verify-permissions 目标",
            severity="error",
        )
    
    target_content = match.group(0)
    
    # 检查 1: 是否有 SET seek.enabled 命令
    has_seek_enabled_set = 'seek.enabled' in target_content
    
    if not has_seek_enabled_set:
        return CheckResult(
            check_id="C",
            check_name="verify_permissions_seekdb_enable",
            passed=False,
            message="verify-permissions 未设置 seek.enabled 配置",
            severity="error",
            details=["未找到 SET seek.enabled 命令"],
        )
    
    # 检查 2: 是否根据 SEEKDB_ENABLE_EFFECTIVE 条件设置
    # 预期模式: SET seek.enabled = '$(if $(filter 1,$(SEEKDB_ENABLE_EFFECTIVE)),true,false)'
    conditional_pattern = re.search(
        r"seek\.enabled\s*=\s*'\$\(if\s+\$\(filter\s+1,\s*\$\(SEEKDB_ENABLE_EFFECTIVE\)\),\s*true,\s*false\)'",
        target_content
    )
    
    if conditional_pattern:
        details.append("verify-permissions 正确根据 SEEKDB_ENABLE_EFFECTIVE 设置 seek.enabled")
        details.append("  - SEEKDB_ENABLE=1 → seek.enabled='true'")
        details.append("  - SEEKDB_ENABLE=0 → seek.enabled='false'")
    else:
        # 检查是否有其他形式的条件设置
        if 'SEEKDB_ENABLE' in target_content and 'seek.enabled' in target_content:
            details.append("发现 SEEKDB_ENABLE 和 seek.enabled，但模式不完全匹配预期")
            details.append("预期模式: $(if $(filter 1,$(SEEKDB_ENABLE_EFFECTIVE)),true,false)")
        else:
            return CheckResult(
                check_id="C",
                check_name="verify_permissions_seekdb_enable",
                passed=False,
                message="verify-permissions 未按 SEEKDB_ENABLE 条件注入 seek.enabled",
                severity="error",
                details=details,
            )
    
    # 检查 3: 验证 SEEKDB_ENABLE_EFFECTIVE 的定义
    effective_def = re.search(
        r'SEEKDB_ENABLE_EFFECTIVE\s*:?=\s*\$\(or\s+\$\(SEEKDB_ENABLE\)',
        content
    )
    
    if effective_def:
        details.append("SEEKDB_ENABLE_EFFECTIVE 定义正确（支持 SEEK_ENABLE 别名）")
    else:
        details.append("警告: 未找到 SEEKDB_ENABLE_EFFECTIVE 的标准定义")
    
    # 检查 4: 同时检查 verify-permissions-logbook 目标
    verify_logbook_pattern = re.compile(
        r'^verify-permissions-logbook:.*?(?=^[a-zA-Z_-]+:|^\Z)',
        re.MULTILINE | re.DOTALL
    )
    
    logbook_match = verify_logbook_pattern.search(content)
    if logbook_match:
        logbook_content = logbook_match.group(0)
        if "seek.enabled = 'false'" in logbook_content:
            details.append("verify-permissions-logbook 正确硬编码 seek.enabled='false'")
        else:
            details.append("警告: verify-permissions-logbook 可能未正确设置 seek.enabled")
    
    return CheckResult(
        check_id="C",
        check_name="verify_permissions_seekdb_enable",
        passed=True,
        message="verify-permissions 正确按 SEEKDB_ENABLE 注入 seek.enabled",
        severity="info",
        details=details if verbose else [],
    )


def check_d_docs_makefile_consistency(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 D: docs/logbook/03_deploy_verify_troubleshoot.md 中的 up-logbook 描述与 Makefile 实现一致
    
    验证策略：
    1. 从文档中提取 up-logbook 的描述
    2. 从 Makefile 中提取 up-logbook 的实现
    3. 验证两者是否一致
    """
    docs_file = project_root / "docs" / "logbook" / "03_deploy_verify_troubleshoot.md"
    makefile = project_root / "Makefile"
    details = []
    
    if not docs_file.exists():
        return CheckResult(
            check_id="D",
            check_name="docs_makefile_consistency",
            passed=False,
            message="docs/logbook/03_deploy_verify_troubleshoot.md 文件不存在",
            severity="error",
        )
    
    if not makefile.exists():
        return CheckResult(
            check_id="D",
            check_name="docs_makefile_consistency",
            passed=False,
            message="Makefile 文件不存在",
            severity="error",
        )
    
    docs_content = docs_file.read_text()
    makefile_content = makefile.read_text()
    
    # 从文档中提取 up-logbook 相关描述
    # 文档中描述 up-logbook 执行的步骤
    docs_steps = []
    
    # 查找文档中关于 up-logbook 的描述
    # 预期描述类似: "启动 PostgreSQL 容器"、"执行迁移"、"等待健康检查"
    if 'up-logbook' in docs_content:
        # 查找 up-logbook 相关段落
        up_logbook_section = re.search(
            r'make up-logbook.*?(?=###|\Z)',
            docs_content,
            re.DOTALL | re.IGNORECASE
        )
        
        if up_logbook_section:
            section_text = up_logbook_section.group(0)
            
            # 检查关键步骤描述
            step_keywords = {
                'postgresql': '启动 PostgreSQL',
                'postgres': '启动 PostgreSQL',
                'migrate': '执行迁移',
                '迁移': '执行迁移',
                'health': '健康检查',
                '健康': '健康检查',
            }
            
            for keyword, step_name in step_keywords.items():
                if keyword.lower() in section_text.lower():
                    docs_steps.append(step_name)
            
            docs_steps = list(set(docs_steps))  # 去重
    
    details.append(f"文档描述的步骤: {docs_steps}")
    
    # 从 Makefile 中提取 up-logbook 实现
    makefile_steps = []
    
    up_logbook_pattern = re.compile(
        r'^up-logbook:.*?(?=^[a-zA-Z_-]+:|^\Z)',
        re.MULTILINE | re.DOTALL
    )
    
    match = up_logbook_pattern.search(makefile_content)
    if match:
        target_content = match.group(0)
        
        # 分析实现中的步骤
        if 'up -d' in target_content:
            makefile_steps.append('启动 PostgreSQL')
        
        if 'migrate' in target_content.lower() or '--profile migrate' in target_content:
            makefile_steps.append('执行迁移')
        
        if 'sleep' in target_content or 'wait' in target_content.lower():
            makefile_steps.append('等待启动')
        
        if 'ps' in target_content:
            makefile_steps.append('显示状态')
    
    details.append(f"Makefile 实现的步骤: {makefile_steps}")
    
    # 验证一致性
    # 检查文档中提到的关键步骤是否在 Makefile 中实现
    missing_in_makefile = []
    
    # 核心一致性检查点
    consistency_checks = [
        ('启动 PostgreSQL', 'up -d' in (match.group(0) if match else '')),
        ('执行迁移', 'migrate' in (match.group(0) if match else '').lower()),
    ]
    
    inconsistencies = []
    for check_name, check_result in consistency_checks:
        if check_name in docs_steps and not check_result:
            inconsistencies.append(f"文档描述'{check_name}'但 Makefile 未实现")
        elif check_name not in docs_steps and check_result:
            inconsistencies.append(f"Makefile 实现了'{check_name}'但文档未描述")
    
    if inconsistencies:
        details.extend(inconsistencies)
        return CheckResult(
            check_id="D",
            check_name="docs_makefile_consistency",
            passed=False,
            message="docs up-logbook 描述与 Makefile 实现不一致",
            severity="error",
            details=details,
        )
    
    # 额外检查: 验证文档中的命令示例是否正确
    if 'make up-logbook' in docs_content:
        details.append("文档中包含正确的命令示例: make up-logbook")
    
    # 检查文档中是否正确描述了 up-logbook 的输出
    if match:
        target_content = match.group(0)
        if '[OK]' in target_content and 'Logbook 服务已启动' in target_content:
            if '[OK] Logbook 服务已启动' in docs_content or '已启动' in docs_content:
                details.append("文档正确描述了成功输出")
    
    return CheckResult(
        check_id="D",
        check_name="docs_makefile_consistency",
        passed=True,
        message="docs up-logbook 描述与 Makefile 实现一致",
        severity="info",
        details=details if verbose else [],
    )


def check_e_readme_logbook_only_stepwise_commands(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 E: README.md Logbook-only 分步验收命令使用 migrate-logbook-stepwise 与 verify-permissions-logbook
    
    验证策略：
    1. 定位 README.md 中的 "Logbook-only" 分步验收章节
    2. 验证必须包含 migrate-logbook-stepwise 命令
    3. 验证必须包含 verify-permissions-logbook 命令
    """
    readme_file = project_root / "README.md"
    details = []
    
    if not readme_file.exists():
        return CheckResult(
            check_id="E",
            check_name="readme_logbook_only_stepwise_commands",
            passed=False,
            message="README.md 文件不存在",
            severity="error",
        )
    
    content = readme_file.read_text()
    
    # 定位 Logbook-only 分步验收章节
    # 查找 "分步验收（Logbook-only）" 或类似标题
    logbook_only_section_pattern = re.compile(
        r'###\s*分步验收[（(]Logbook-only[)）].*?(?=###|##|\Z)',
        re.DOTALL | re.IGNORECASE
    )
    
    match = logbook_only_section_pattern.search(content)
    if not match:
        return CheckResult(
            check_id="E",
            check_name="readme_logbook_only_stepwise_commands",
            passed=False,
            message="README.md 中未找到 Logbook-only 分步验收章节",
            severity="error",
            details=[
                "修复提示: 在 README.md 中添加 '### 分步验收（Logbook-only）' 章节",
                "该章节应包含:",
                "  - make migrate-logbook-stepwise",
                "  - make verify-permissions-logbook",
            ],
        )
    
    section_content = match.group(0)
    details.append("找到 Logbook-only 分步验收章节")
    
    # 必需的命令
    required_commands = {
        'migrate-logbook-stepwise': {
            'pattern': r'make\s+migrate-logbook-stepwise',
            'description': '数据库迁移（stepwise）',
            'fix': "添加: make migrate-logbook-stepwise  # 数据库迁移",
        },
        'verify-permissions-logbook': {
            'pattern': r'make\s+verify-permissions-logbook',
            'description': '权限验证（Logbook-only）',
            'fix': "添加: make verify-permissions-logbook  # 权限验证",
        },
    }
    
    missing_commands = []
    found_commands = []
    
    for cmd_name, cmd_info in required_commands.items():
        if re.search(cmd_info['pattern'], section_content):
            found_commands.append(f"✓ {cmd_name} ({cmd_info['description']})")
        else:
            missing_commands.append(cmd_name)
    
    details.extend(found_commands)
    
    if missing_commands:
        details.append("")
        details.append("缺失的必需命令:")
        for cmd in missing_commands:
            cmd_info = required_commands[cmd]
            details.append(f"  ✗ {cmd} - {cmd_info['fix']}")
        
        return CheckResult(
            check_id="E",
            check_name="readme_logbook_only_stepwise_commands",
            passed=False,
            message=f"README.md Logbook-only 分步验收缺失命令: {', '.join(missing_commands)}",
            severity="error",
            details=details,
        )
    
    # 检查命令顺序是否合理（up-logbook → migrate → verify-permissions → smoke → unit）
    expected_order = ['up-logbook', 'migrate-logbook-stepwise', 'verify-permissions-logbook']
    positions = {}
    for cmd in expected_order:
        match_pos = re.search(rf'make\s+{re.escape(cmd)}', section_content)
        if match_pos:
            positions[cmd] = match_pos.start()
    
    order_issues = []
    for i in range(len(expected_order) - 1):
        cmd1, cmd2 = expected_order[i], expected_order[i + 1]
        if cmd1 in positions and cmd2 in positions:
            if positions[cmd1] > positions[cmd2]:
                order_issues.append(f"'{cmd1}' 应在 '{cmd2}' 之前")
    
    if order_issues:
        details.append("")
        details.append("命令顺序建议:")
        details.extend(f"  - {issue}" for issue in order_issues)
    
    return CheckResult(
        check_id="E",
        check_name="readme_logbook_only_stepwise_commands",
        passed=True,
        message="README.md Logbook-only 分步验收命令正确（含 migrate-logbook-stepwise 与 verify-permissions-logbook）",
        severity="info",
        details=details if verbose else [],
    )


def check_f_acceptance_criteria_logbook_only_alignment(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """
    检查 F: docs/logbook/04_acceptance_criteria.md Logbook-only 章节命令对齐
    
    验证策略：
    1. 定位 04_acceptance_criteria.md 中的 Logbook-only 验收相关章节
    2. 验证必须包含 migrate-logbook-stepwise 命令
    3. 验证必须包含 verify-permissions-logbook 命令
    4. 与 README.md 的 Logbook-only 章节保持一致
    """
    acceptance_file = project_root / "docs" / "logbook" / "04_acceptance_criteria.md"
    readme_file = project_root / "README.md"
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
    
    # 定位 Logbook-only 相关章节
    # 可能的章节: "Logbook-only 验收"、验收表格中的命令
    logbook_only_sections = []
    
    # 查找 "Logbook-only 验收" 章节
    logbook_only_section_pattern = re.compile(
        r'###\s*Logbook-only\s+验收.*?(?=###|##|\Z)',
        re.DOTALL | re.IGNORECASE
    )
    match = logbook_only_section_pattern.search(content)
    if match:
        logbook_only_sections.append(('Logbook-only 验收章节', match.group(0)))
    
    # 查找表格中的 Logbook-only 相关行（数据库迁移、权限验证行）
    table_pattern = re.compile(
        r'\|\s*\*\*(?:数据库迁移|权限验证)\*\*\s*\|[^\|]*\|',
        re.IGNORECASE
    )
    table_matches = table_pattern.findall(content)
    if table_matches:
        logbook_only_sections.append(('验收表格', ' '.join(table_matches)))
    
    if not logbook_only_sections:
        return CheckResult(
            check_id="F",
            check_name="acceptance_criteria_logbook_only_alignment",
            passed=False,
            message="04_acceptance_criteria.md 中未找到 Logbook-only 相关章节",
            severity="error",
            details=[
                "修复提示: 确保 04_acceptance_criteria.md 包含 Logbook-only 验收章节",
                "该章节应包含:",
                "  - make migrate-logbook-stepwise",
                "  - make verify-permissions-logbook",
            ],
        )
    
    details.append(f"找到 {len(logbook_only_sections)} 个 Logbook-only 相关章节")
    
    # 必需的命令（在整个文档中查找）
    required_commands = {
        'migrate-logbook-stepwise': {
            'pattern': r'make\s+migrate-logbook-stepwise|`migrate-logbook-stepwise`',
            'description': '数据库迁移（stepwise）',
            'fix': "在 Logbook-only 验收表格中添加: `make migrate-logbook-stepwise`",
        },
        'verify-permissions-logbook': {
            'pattern': r'make\s+verify-permissions-logbook|`verify-permissions-logbook`',
            'description': '权限验证（Logbook-only）',
            'fix': "在 Logbook-only 验收表格中添加: `make verify-permissions-logbook`",
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
        details.append("缺失的必需命令:")
        for cmd in missing_commands:
            cmd_info = required_commands[cmd]
            details.append(f"  ✗ {cmd} - {cmd_info['fix']}")
        
        return CheckResult(
            check_id="F",
            check_name="acceptance_criteria_logbook_only_alignment",
            passed=False,
            message=f"04_acceptance_criteria.md Logbook-only 章节缺失命令: {', '.join(missing_commands)}",
            severity="error",
            details=details,
        )
    
    # 交叉验证: 与 README.md 对齐
    if readme_file.exists():
        readme_content = readme_file.read_text()
        readme_section_pattern = re.compile(
            r'###\s*分步验收[（(]Logbook-only[)）].*?(?=###|##|\Z)',
            re.DOTALL | re.IGNORECASE
        )
        readme_match = readme_section_pattern.search(readme_content)
        
        if readme_match:
            readme_section = readme_match.group(0)
            # 检查两个文档中的命令是否一致
            readme_has_migrate = bool(re.search(r'migrate-logbook-stepwise', readme_section))
            readme_has_verify = bool(re.search(r'verify-permissions-logbook', readme_section))
            
            acceptance_has_migrate = bool(re.search(r'migrate-logbook-stepwise', content))
            acceptance_has_verify = bool(re.search(r'verify-permissions-logbook', content))
            
            if readme_has_migrate == acceptance_has_migrate and readme_has_verify == acceptance_has_verify:
                details.append("")
                details.append("✓ 与 README.md Logbook-only 章节命令对齐")
            else:
                alignment_issues = []
                if readme_has_migrate != acceptance_has_migrate:
                    alignment_issues.append("migrate-logbook-stepwise")
                if readme_has_verify != acceptance_has_verify:
                    alignment_issues.append("verify-permissions-logbook")
                details.append("")
                details.append(f"警告: 与 README.md 命令不对齐: {', '.join(alignment_issues)}")
    
    return CheckResult(
        check_id="F",
        check_name="acceptance_criteria_logbook_only_alignment",
        passed=True,
        message="04_acceptance_criteria.md Logbook-only 章节命令正确对齐",
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
    
    # 检查 C: verify-permissions SEEKDB_ENABLE 注入
    report.checks.append(check_c_verify_permissions_seekdb_enable(project_root, verbose))
    
    # 检查 D: docs/logbook/03_deploy_verify_troubleshoot.md 与 Makefile 一致性
    report.checks.append(check_d_docs_makefile_consistency(project_root, verbose))
    
    # 检查 E: README.md Logbook-only 分步验收命令
    report.checks.append(check_e_readme_logbook_only_stepwise_commands(project_root, verbose))
    
    # 检查 F: 04_acceptance_criteria.md Logbook-only 章节命令对齐
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
  C) verify-permissions 是否按 SEEKDB_ENABLE 注入 seek.enabled
  D) docs/logbook/03_deploy_verify_troubleshoot.md 与 Makefile 一致性
  E) README.md Logbook-only 分步验收命令使用 migrate-logbook-stepwise 与 verify-permissions-logbook
  F) docs/logbook/04_acceptance_criteria.md Logbook-only 章节命令对齐

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
            print(f"错误: 无法确定项目根目录，请使用 --project-root 指定", file=sys.stderr)
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
