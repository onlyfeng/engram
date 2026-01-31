#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SCM Sync 一致性验证脚本

验证 SCM 同步子系统的文档、配置和代码之间的一致性。

检查项:
A) README/ops guide 提到的命令在 pyproject.toml 与模块中存在
B) engram-scm 不再是 TODO（或文档不再引用 TODO 功能）
C) runner 的实际执行路径不依赖将被移除的入口
D) 若文档声称支持 docker profile，则 compose 文件存在对应服务
E) docs/architecture/cli_entrypoints.md 对照表与现实一致

用法:
    python scripts/verify_scm_sync_consistency.py [--verbose]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CheckResult:
    """检查结果"""

    check_id: str
    name: str
    passed: bool
    message: str
    details: List[str] = field(default_factory=list)


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


class SCMSyncConsistencyVerifier:
    """SCM Sync 一致性验证器"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.report = VerificationReport()
        self.pyproject_data: Optional[Dict] = None
        self.console_scripts: Dict[str, str] = {}

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

    def check_a_commands_in_docs_exist(self) -> CheckResult:
        """
        检查 A: README/ops guide 提到的命令在 pyproject.toml 与模块中存在
        """
        check_id = "A"
        name = "文档命令与 pyproject.toml 一致"

        if not self.pyproject_data:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="无法加载 pyproject.toml",
            )

        # 从文档中提取提到的命令
        docs_to_check = [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "docs" / "logbook" / "07_scm_sync_ops_guide.md",
        ]

        # 匹配 engram-scm-* 命令模式
        command_pattern = re.compile(r"\b(engram-(?:scm-)?[\w-]+)\b")
        documented_commands: Set[str] = set()

        for doc_path in docs_to_check:
            if doc_path.exists():
                content = doc_path.read_text(encoding="utf-8")
                matches = command_pattern.findall(content)
                documented_commands.update(matches)

        # 过滤出 SCM 相关命令
        scm_commands = {
            cmd
            for cmd in documented_commands
            if cmd.startswith("engram-scm")
        }

        # 检查命令是否在 pyproject.toml 中定义
        missing_commands: List[str] = []
        for cmd in sorted(scm_commands):
            if cmd not in self.console_scripts:
                missing_commands.append(cmd)
            else:
                self.log(f"[OK] {cmd} 在 pyproject.toml 中定义")

        if missing_commands:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"文档中提到的 {len(missing_commands)} 个命令未在 pyproject.toml 中定义",
                details=[f"缺失: {cmd}" for cmd in missing_commands],
            )

        # 验证模块是否可导入
        import_errors: List[str] = []
        for cmd, entry_point in self.console_scripts.items():
            if not cmd.startswith("engram-scm"):
                continue

            module_path, func_name = entry_point.rsplit(":", 1)
            try:
                spec = importlib.util.find_spec(module_path)
                if spec is None:
                    import_errors.append(f"{cmd}: 模块 {module_path} 未找到")
                else:
                    self.log(f"[OK] {cmd} -> {entry_point} 模块可导入")
            except ModuleNotFoundError as e:
                import_errors.append(f"{cmd}: {e}")

        if import_errors:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"{len(import_errors)} 个入口点模块无法导入",
                details=import_errors,
            )

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message=f"所有 {len(scm_commands)} 个 SCM 命令已正确定义",
        )

    def check_b_no_todo_references(self) -> CheckResult:
        """
        检查 B: engram-scm 不再是 TODO（或文档不再引用 TODO 功能）
        """
        check_id = "B"
        name = "engram-scm 不再引用 TODO 功能"

        # 检查关键文件中是否存在 TODO 引用 engram-scm
        files_to_check = [
            PROJECT_ROOT / "src" / "engram" / "logbook" / "cli" / "scm.py",
            PROJECT_ROOT / "src" / "engram" / "logbook" / "cli" / "scm_sync.py",
            PROJECT_ROOT / "docs" / "logbook" / "07_scm_sync_ops_guide.md",
            PROJECT_ROOT / "docs" / "architecture" / "cli_entrypoints.md",
        ]

        # 模式：查找标记为 TODO 的 engram-scm 相关内容
        todo_patterns = [
            re.compile(r"#\s*TODO.*engram-scm", re.IGNORECASE),
            re.compile(r"engram-scm.*TODO", re.IGNORECASE),
            re.compile(r"尚未实现.*engram-scm", re.IGNORECASE),
            re.compile(r"engram-scm.*尚未实现", re.IGNORECASE),
            # 检查 scm.py 中 sync 命令是否标记为未实现
            re.compile(r"NOT_IMPLEMENTED.*sync", re.IGNORECASE),
        ]

        todo_findings: List[Tuple[str, int, str]] = []

        for file_path in files_to_check:
            if not file_path.exists():
                continue

            content = file_path.read_text(encoding="utf-8")
            lines = content.split("\n")

            for i, line in enumerate(lines, 1):
                for pattern in todo_patterns:
                    if pattern.search(line):
                        todo_findings.append(
                            (str(file_path.relative_to(PROJECT_ROOT)), i, line.strip())
                        )

        # 检查 scm.py 中 sync 命令的实现状态
        scm_cli_path = PROJECT_ROOT / "src" / "engram" / "logbook" / "cli" / "scm.py"
        if scm_cli_path.exists():
            content = scm_cli_path.read_text(encoding="utf-8")
            if "NOT_IMPLEMENTED" in content and "sync" in content:
                # 这是已知的未实现功能，但文档可能不应该引用它
                self.log("[WARN] scm.py 中 sync 命令标记为 NOT_IMPLEMENTED")

        if todo_findings:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"发现 {len(todo_findings)} 处 TODO/未实现引用",
                details=[
                    f"{f}:{line}: {text}" for f, line, text in todo_findings[:10]
                ],
            )

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message="未发现关键 TODO 引用",
        )

    def check_c_runner_no_deprecated_deps(self) -> CheckResult:
        """
        检查 C: runner 的实际执行路径不依赖将被移除的入口

        验证内容:
        1. 新入口文件不通过 subprocess/os.system 调用根目录脚本
        2. 核心执行路径（SyncRunner._run_sync_once）使用 SyncExecutor 而非命令行
        3. get_script_path/build_sync_command 已标记为 deprecated
        4. 核心模块存在且可导入
        """
        check_id = "C"
        name = "Runner 不依赖将被移除的入口"

        issues: List[str] = []

        # 将被移除的根目录脚本
        deprecated_scripts = [
            "scm_sync_runner.py",
            "scm_sync_scheduler.py",
            "scm_sync_status.py",
            "scm_sync_reaper.py",
            "scm_sync_worker.py",
            "scm_sync_gitlab_commits.py",
            "scm_sync_gitlab_mrs.py",
            "scm_sync_svn.py",
        ]

        # 检查新入口是否通过 subprocess 调用旧脚本
        new_entry_files = [
            PROJECT_ROOT / "src" / "engram" / "logbook" / "cli" / "scm_sync.py",
            PROJECT_ROOT / "src" / "engram" / "logbook" / "scm_sync_scheduler_core.py",
            PROJECT_ROOT / "src" / "engram" / "logbook" / "scm_sync_worker_core.py",
            PROJECT_ROOT / "src" / "engram" / "logbook" / "scm_sync_reaper_core.py",
            PROJECT_ROOT / "src" / "engram" / "logbook" / "scm_sync_executor.py",
        ]

        for entry_file in new_entry_files:
            if not entry_file.exists():
                continue

            content = entry_file.read_text(encoding="utf-8")

            for deprecated in deprecated_scripts:
                # 检查是否通过 subprocess 调用已弃用脚本
                patterns = [
                    rf"subprocess.*{deprecated}",
                    rf"os\.system.*{deprecated}",
                    rf"Popen.*{deprecated}",
                ]

                for pattern in patterns:
                    if re.search(pattern, content):
                        issues.append(
                            f"{entry_file.name} 通过 subprocess 调用了已弃用脚本 {deprecated}"
                        )

        # 检查 scm_sync_runner.py 中 deprecated 函数是否已正确标记
        runner_file = PROJECT_ROOT / "src" / "engram" / "logbook" / "scm_sync_runner.py"
        if runner_file.exists():
            content = runner_file.read_text(encoding="utf-8")

            # 检查 get_script_path 是否标记为 deprecated
            if "def get_script_path" in content:
                if "deprecated" not in content.lower() or "DeprecationWarning" not in content:
                    issues.append(
                        "get_script_path() 未正确标记为 deprecated"
                    )
                else:
                    self.log("[OK] get_script_path() 已标记为 deprecated")

            # 检查 build_sync_command 是否标记为 deprecated
            if "def build_sync_command" in content:
                if "deprecated" not in content.lower() or "DeprecationWarning" not in content:
                    issues.append(
                        "build_sync_command() 未正确标记为 deprecated"
                    )
                else:
                    self.log("[OK] build_sync_command() 已标记为 deprecated")

            # 检查 SyncRunner._run_sync_once 是否使用 SyncExecutor
            if "_run_sync_once" in content:
                # 检查是否调用 executor
                if "executor" in content.lower() and "get_default_executor" in content:
                    self.log("[OK] SyncRunner._run_sync_once 使用 SyncExecutor")
                else:
                    issues.append(
                        "SyncRunner._run_sync_once 未使用 SyncExecutor 执行同步"
                    )

            # 检查核心执行路径是否调用 build_sync_command
            # 排除定义本身和测试代码，只检查实际调用
            run_sync_match = re.search(
                r"def _run_sync_once\(.*?\).*?(?=\n    def |\nclass |\Z)",
                content,
                re.DOTALL,
            )
            if run_sync_match:
                run_sync_body = run_sync_match.group(0)
                if "build_sync_command(" in run_sync_body:
                    issues.append(
                        "SyncRunner._run_sync_once 仍在调用已弃用的 build_sync_command()"
                    )
                else:
                    self.log("[OK] SyncRunner._run_sync_once 不依赖 build_sync_command()")

        if issues:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"发现 {len(issues)} 处问题",
                details=issues,
            )

        # 检查核心模块是否存在
        core_modules = [
            "engram.logbook.scm_sync_runner",
            "engram.logbook.scm_sync_scheduler_core",
            "engram.logbook.scm_sync_worker_core",
            "engram.logbook.scm_sync_reaper_core",
            "engram.logbook.scm_sync_status",
            "engram.logbook.scm_sync_executor",
        ]

        missing_modules: List[str] = []
        for module in core_modules:
            try:
                spec = importlib.util.find_spec(module)
                if spec is None:
                    missing_modules.append(module)
                else:
                    self.log(f"[OK] 核心模块 {module} 存在")
            except ModuleNotFoundError:
                missing_modules.append(module)

        if missing_modules:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"{len(missing_modules)} 个核心模块缺失",
                details=[f"缺失: {m}" for m in missing_modules],
            )

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message="Runner 不依赖将被移除的入口，deprecated 函数已正确标记，核心模块完整",
        )

    def check_d_docker_profile_services(self) -> CheckResult:
        """
        检查 D: 若文档声称支持 docker profile，则 compose 文件存在对应服务
        """
        check_id = "D"
        name = "Docker Compose 服务与文档一致"

        # 查找所有 compose 文件
        compose_files = list(PROJECT_ROOT.glob("compose/*.yml")) + list(
            PROJECT_ROOT.glob("docker-compose*.yml")
        )

        if not compose_files:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=True,
                message="未发现 compose 文件，跳过检查",
            )

        # 从文档中提取提到的 docker 服务/profile
        docs_to_check = [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "docs" / "logbook" / "07_scm_sync_ops_guide.md",
            PROJECT_ROOT / "docs" / "logbook" / "03_deploy_verify_troubleshoot.md",
        ]

        # 匹配 docker compose 服务引用
        service_pattern = re.compile(
            r"docker\s+compose.*(?:up|start|restart)\s+(?:-d\s+)?(\w+)"
        )
        profile_pattern = re.compile(r"--profile\s+(\w+)")

        documented_services: Set[str] = set()
        documented_profiles: Set[str] = set()

        for doc_path in docs_to_check:
            if not doc_path.exists():
                continue

            content = doc_path.read_text(encoding="utf-8")

            # 提取服务名
            for match in service_pattern.finditer(content):
                service = match.group(1)
                if service not in ("--", "-f", "-d"):
                    documented_services.add(service)

            # 提取 profile 名
            for match in profile_pattern.finditer(content):
                documented_profiles.add(match.group(1))

        # 解析 compose 文件获取实际服务
        actual_services: Set[str] = set()

        try:
            import yaml
        except ImportError:
            # 如果没有 yaml 库，使用简单的正则解析
            for compose_file in compose_files:
                content = compose_file.read_text(encoding="utf-8")
                # 简单匹配 services: 下的服务名
                in_services = False
                for line in content.split("\n"):
                    if line.strip() == "services:":
                        in_services = True
                        continue
                    if in_services and line and not line.startswith(" ") and not line.startswith("\t"):
                        in_services = False
                    if in_services and line.strip() and not line.strip().startswith("#"):
                        # 服务名是 2 空格缩进的 key
                        if line.startswith("  ") and not line.startswith("    "):
                            service_name = line.strip().rstrip(":")
                            if service_name:
                                actual_services.add(service_name)
        else:
            for compose_file in compose_files:
                with open(compose_file) as f:
                    try:
                        data = yaml.safe_load(f)
                        if data and "services" in data:
                            actual_services.update(data["services"].keys())
                    except yaml.YAMLError:
                        pass

        # 验证文档提到的服务是否存在
        # 注意：我们只检查 SCM 相关服务
        scm_related_services = {
            s for s in documented_services
            if "scm" in s.lower() or "sync" in s.lower() or "worker" in s.lower()
        }

        missing_services: List[str] = []
        for service in scm_related_services:
            if service not in actual_services:
                missing_services.append(service)

        if missing_services:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message=f"文档提到的 {len(missing_services)} 个服务在 compose 文件中不存在",
                details=[f"缺失: {s}" for s in missing_services],
            )

        self.log(f"Compose 文件: {[f.name for f in compose_files]}")
        self.log(f"实际服务: {actual_services}")
        self.log(f"文档提到的 SCM 服务: {scm_related_services}")

        return CheckResult(
            check_id=check_id,
            name=name,
            passed=True,
            message=f"Docker Compose 配置与文档一致 ({len(actual_services)} 个服务)",
        )

    def check_e_cli_entrypoints_table(self) -> CheckResult:
        """
        检查 E: docs/architecture/cli_entrypoints.md 对照表与现实一致
        """
        check_id = "E"
        name = "CLI 入口对照表与 pyproject.toml 一致"

        cli_doc_path = PROJECT_ROOT / "docs" / "architecture" / "cli_entrypoints.md"

        if not cli_doc_path.exists():
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="cli_entrypoints.md 文件不存在",
            )

        if not self.pyproject_data:
            return CheckResult(
                check_id=check_id,
                name=name,
                passed=False,
                message="无法加载 pyproject.toml",
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
            # 清理可能的反引号
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
        for cmd, actual_entry in self.console_scripts.items():
            if cmd not in documented_entries:
                # 只警告主要的 engram 命令
                if cmd.startswith("engram-"):
                    self.log(f"[WARN] {cmd} 在 pyproject.toml 中定义但文档未列出")

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

    def run_all_checks(self) -> VerificationReport:
        """运行所有检查"""
        print("=" * 60)
        print("SCM Sync 一致性验证")
        print("=" * 60)

        # 加载 pyproject.toml
        if not self.load_pyproject():
            print("[ERROR] 无法加载 pyproject.toml")
            return self.report

        # 运行检查
        checks = [
            ("A", "文档命令与代码一致", self.check_a_commands_in_docs_exist),
            ("B", "无 TODO 引用", self.check_b_no_todo_references),
            ("C", "Runner 不依赖弃用入口", self.check_c_runner_no_deprecated_deps),
            ("D", "Docker Compose 服务一致", self.check_d_docker_profile_services),
            ("E", "CLI 入口对照表一致", self.check_e_cli_entrypoints_table),
        ]

        for check_id, desc, check_func in checks:
            print(f"\n[{check_id}] {desc}...")
            try:
                result = check_func()
                self.report.add_result(result)

                status = "PASS" if result.passed else "FAIL"
                print(f"    [{status}] {result.message}")

                if not result.passed and result.details:
                    for detail in result.details[:5]:
                        print(f"      - {detail}")
                    if len(result.details) > 5:
                        print(f"      ... 还有 {len(result.details) - 5} 项")

            except Exception as e:
                result = CheckResult(
                    check_id=check_id,
                    name=desc,
                    passed=False,
                    message=f"检查过程中出错: {e}",
                )
                self.report.add_result(result)
                print(f"    [ERROR] {e}")

        # 汇总
        print("\n" + "=" * 60)
        print("验证结果汇总")
        print("=" * 60)
        print(f"总检查数: {self.report.total_checks}")
        print(f"通过: {self.report.passed_checks}")
        print(f"失败: {self.report.failed_checks}")

        if self.report.is_success():
            print("\n[OK] 所有检查通过")
        else:
            print("\n[FAIL] 存在检查失败项")

        return self.report


def main() -> int:
    """主函数"""
    parser = argparse.ArgumentParser(
        description="SCM Sync 一致性验证脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
检查项:
  A) README/ops guide 提到的命令在 pyproject.toml 与模块中存在
  B) engram-scm 不再是 TODO（或文档不再引用 TODO 功能）
  C) runner 的实际执行路径不依赖将被移除的入口
  D) 若文档声称支持 docker profile，则 compose 文件存在对应服务
  E) docs/architecture/cli_entrypoints.md 对照表与现实一致

示例:
  python scripts/verify_scm_sync_consistency.py
  python scripts/verify_scm_sync_consistency.py --verbose
        """,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出",
    )

    args = parser.parse_args()

    verifier = SCMSyncConsistencyVerifier(verbose=args.verbose)
    report = verifier.run_all_checks()

    return 0 if report.is_success() else 1


if __name__ == "__main__":
    sys.exit(main())
