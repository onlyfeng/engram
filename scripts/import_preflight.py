#!/usr/bin/env python3
"""
import_preflight.py - Engram 项目导入预检工具

功能:
  1. 静态解析 docker-compose.*.yml 文件
  2. 检查 build.context 目录是否存在
  3. 检查 volume bind mount 源路径是否存在
  4. 复用 verify_build_boundaries.sh 的检查逻辑
  5. 支持 JSON 和人类可读两种输出模式

用法:
  # 基本用法（检查当前目录）
  python scripts/import_preflight.py /path/to/project

  # 使用 manifest 文件
  python scripts/import_preflight.py /path/to/project --manifest manifest.json

  # JSON 输出
  python scripts/import_preflight.py /path/to/project --json

  # 详细模式
  python scripts/import_preflight.py /path/to/project --verbose

退出码:
  0 - 预检通过
  1 - 发现错误
  2 - 脚本错误（参数错误等）
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

    check_name: str
    passed: bool
    message: str
    severity: str = "error"  # error, warning, info
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "passed": self.passed,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass
class PreflightReport:
    """预检报告"""

    project_root: str
    compose_files: list[str]
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
            "project_root": self.project_root,
            "compose_files": self.compose_files,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": [c.to_dict() for c in self.checks],
        }


# =============================================================================
# YAML 解析（支持简单和完整两种模式）
# =============================================================================


def parse_yaml_simple(content: str) -> dict[str, Any]:
    """
    简单 YAML 解析（不依赖 PyYAML）
    仅支持提取 build.context 和 volumes 信息
    """
    result: dict[str, Any] = {"services": {}}

    current_service = None
    current_section = None
    in_build = False
    in_volumes = False
    indent_level = 0

    lines = content.split("\n")
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue

        # 计算缩进
        current_indent = len(line) - len(stripped)

        # 检测 services 块
        if stripped.startswith("services:"):
            current_section = "services"
            continue

        # 检测服务名（services 下的一级缩进）
        if current_section == "services" and current_indent == 2 and stripped.endswith(":"):
            service_name = stripped.rstrip(":")
            if not service_name.startswith("-"):
                current_service = service_name
                result["services"][current_service] = {"build": {}, "volumes": []}
                in_build = False
                in_volumes = False
            continue

        # 检测 build 块
        if current_service and current_indent == 4:
            if stripped.startswith("build:"):
                in_build = True
                in_volumes = False
                # 检查是否是单行 build: ./path
                if ":" in stripped and stripped != "build:":
                    context = stripped.split(":", 1)[1].strip()
                    result["services"][current_service]["build"]["context"] = context
                continue
            elif stripped.startswith("volumes:"):
                in_volumes = True
                in_build = False
                continue
            else:
                in_build = False
                in_volumes = False

        # 解析 build.context
        if current_service and in_build and current_indent == 6:
            if stripped.startswith("context:"):
                context = stripped.split(":", 1)[1].strip()
                # 移除引号
                context = context.strip("'\"")
                result["services"][current_service]["build"]["context"] = context

        # 解析 volumes
        if current_service and in_volumes and current_indent == 6:
            if stripped.startswith("-"):
                volume = stripped[1:].strip()
                # 移除引号
                volume = volume.strip("'\"")
                result["services"][current_service]["volumes"].append(volume)

    return result


def parse_yaml(content: str) -> dict[str, Any]:
    """解析 YAML 内容"""
    if yaml is not None:
        return yaml.safe_load(content) or {}
    return parse_yaml_simple(content)


# =============================================================================
# 检查函数
# =============================================================================


def find_compose_files(project_root: Path) -> list[Path]:
    """查找项目中的 docker-compose 文件"""
    patterns = [
        "docker-compose.yml",
        "docker-compose.yaml",
        "docker-compose.*.yml",
        "docker-compose.*.yaml",
        "compose/docker-compose.*.yml",
        "compose/*.yml",
    ]

    files = []
    for pattern in patterns:
        # 处理 glob 模式
        if "*" in pattern:
            files.extend(project_root.glob(pattern))
        else:
            f = project_root / pattern
            if f.exists():
                files.append(f)

    # 去重并排序
    return sorted(set(files))


def check_compose_file_exists(
    project_root: Path, compose_files: list[Path]
) -> CheckResult:
    """检查 Compose 文件是否存在"""
    if not compose_files:
        return CheckResult(
            check_name="compose_files_exist",
            passed=False,
            message="未找到 docker-compose 文件",
            severity="error",
            details=["请确保项目中包含 docker-compose.*.yml 文件"],
        )

    return CheckResult(
        check_name="compose_files_exist",
        passed=True,
        message=f"找到 {len(compose_files)} 个 Compose 文件",
        severity="info",
        details=[str(f.relative_to(project_root)) for f in compose_files],
    )


def check_build_contexts(
    project_root: Path, compose_files: list[Path], verbose: bool = False
) -> CheckResult:
    """检查 Compose 文件中的 build.context 配置"""
    missing_contexts = []
    found_contexts = []

    for compose_file in compose_files:
        try:
            content = compose_file.read_text()
            data = parse_yaml(content)

            services = data.get("services", {})
            for service_name, service_config in services.items():
                if not isinstance(service_config, dict):
                    continue

                build_config = service_config.get("build")
                if not build_config:
                    continue

                # build 可以是字符串或字典
                if isinstance(build_config, str):
                    context = build_config
                elif isinstance(build_config, dict):
                    context = build_config.get("context", ".")
                else:
                    continue

                # 解析相对路径（相对于 compose 文件所在目录）
                compose_dir = compose_file.parent
                if context.startswith("./"):
                    context_path = compose_dir / context[2:]
                elif context.startswith("../"):
                    context_path = compose_dir / context
                elif context == ".":
                    context_path = compose_dir
                else:
                    context_path = compose_dir / context

                context_path = context_path.resolve()

                # 检查路径是否存在
                rel_compose = str(compose_file.relative_to(project_root))
                rel_context = str(context_path.relative_to(project_root)) if context_path.is_relative_to(project_root) else str(context_path)

                if context_path.exists():
                    found_contexts.append(
                        f"{rel_compose}: {service_name} -> {rel_context}"
                    )
                else:
                    missing_contexts.append(
                        f"{rel_compose}: {service_name} -> {rel_context} (不存在)"
                    )

        except Exception as e:
            missing_contexts.append(f"{compose_file.name}: 解析错误 - {e}")

    if missing_contexts:
        return CheckResult(
            check_name="build_contexts",
            passed=False,
            message=f"发现 {len(missing_contexts)} 个缺失的 build context",
            severity="error",
            details=missing_contexts + (found_contexts if verbose else []),
        )

    return CheckResult(
        check_name="build_contexts",
        passed=True,
        message=f"所有 {len(found_contexts)} 个 build context 路径有效",
        severity="info",
        details=found_contexts if verbose else [],
    )


def check_volume_mounts(
    project_root: Path, compose_files: list[Path], verbose: bool = False
) -> CheckResult:
    """检查 Compose 文件中的 volume bind mount 路径"""
    missing_mounts = []
    found_mounts = []

    for compose_file in compose_files:
        try:
            content = compose_file.read_text()
            data = parse_yaml(content)

            services = data.get("services", {})
            for service_name, service_config in services.items():
                if not isinstance(service_config, dict):
                    continue

                volumes = service_config.get("volumes", [])
                if not isinstance(volumes, list):
                    continue

                for volume in volumes:
                    if not isinstance(volume, str):
                        continue

                    # 跳过命名 volume（不包含 : 或以 / 开头）
                    if ":" not in volume:
                        continue

                    # 解析 bind mount 格式: source:target[:options]
                    parts = volume.split(":")
                    source = parts[0]

                    # 跳过命名 volume 引用
                    if not source.startswith(".") and not source.startswith("/"):
                        continue

                    # 解析相对路径
                    compose_dir = compose_file.parent
                    if source.startswith("./"):
                        source_path = compose_dir / source[2:]
                    elif source.startswith("../"):
                        source_path = compose_dir / source
                    elif source.startswith("/"):
                        source_path = Path(source)
                    else:
                        source_path = compose_dir / source

                    source_path = source_path.resolve()

                    # 检查路径是否存在
                    rel_compose = str(compose_file.relative_to(project_root))
                    try:
                        rel_source = str(source_path.relative_to(project_root))
                    except ValueError:
                        rel_source = str(source_path)

                    if source_path.exists():
                        found_mounts.append(
                            f"{rel_compose}: {service_name} -> {rel_source}"
                        )
                    else:
                        missing_mounts.append(
                            f"{rel_compose}: {service_name} -> {rel_source} (不存在)"
                        )

        except Exception as e:
            missing_mounts.append(f"{compose_file.name}: 解析错误 - {e}")

    if missing_mounts:
        return CheckResult(
            check_name="volume_mounts",
            passed=False,
            message=f"发现 {len(missing_mounts)} 个缺失的 volume 源路径",
            severity="error",
            details=missing_mounts + (found_mounts if verbose else []),
        )

    return CheckResult(
        check_name="volume_mounts",
        passed=True,
        message=f"所有 {len(found_mounts)} 个 volume 源路径有效",
        severity="info",
        details=found_mounts if verbose else [],
    )


def check_dockerfile_patterns(
    project_root: Path, verbose: bool = False
) -> CheckResult:
    """检查 Dockerfile 中的不当模式（对齐 verify_build_boundaries.sh）"""
    issues = []
    checked_files = []

    # 查找所有 Dockerfile
    dockerfiles = list(project_root.rglob("Dockerfile*"))
    dockerfiles = [
        f
        for f in dockerfiles
        if "node_modules" not in str(f) and "__pycache__" not in str(f)
    ]

    for dockerfile in dockerfiles:
        try:
            content = dockerfile.read_text()
            rel_path = str(dockerfile.relative_to(project_root))
            checked_files.append(rel_path)

            # 检查 1: COPY .. 模式（危险）
            if re.search(r"^COPY\s+\.\.", content, re.MULTILINE):
                issues.append(f"{rel_path}: 发现危险的 COPY .. 模式")

            # 检查 2: 根目录 COPY . . 且缺少 .dockerignore
            if re.search(r"^COPY\s+\.\s+\.", content, re.MULTILINE):
                dockerfile_dir = dockerfile.parent
                dockerignore = dockerfile_dir / ".dockerignore"
                if not dockerignore.exists():
                    issues.append(
                        f"{rel_path}: 使用 COPY . . 但缺少 .dockerignore"
                    )

        except Exception as e:
            issues.append(f"{dockerfile.name}: 读取错误 - {e}")

    if issues:
        return CheckResult(
            check_name="dockerfile_patterns",
            passed=False,
            message=f"发现 {len(issues)} 个 Dockerfile 问题",
            severity="warning",
            details=issues,
        )

    return CheckResult(
        check_name="dockerfile_patterns",
        passed=True,
        message=f"检查了 {len(checked_files)} 个 Dockerfile，未发现问题",
        severity="info",
        details=checked_files if verbose else [],
    )


def check_dockerignore(project_root: Path, verbose: bool = False) -> CheckResult:
    """检查 .dockerignore 配置（对齐 verify_build_boundaries.sh）"""
    issues = []
    details = []

    dockerignore = project_root / ".dockerignore"

    # 关键应排除的模式
    critical_patterns = [".git", "node_modules", "__pycache__", ".venv", "venv"]

    if dockerignore.exists():
        content = dockerignore.read_text()
        details.append("根目录 .dockerignore 存在")

        for pattern in critical_patterns:
            if pattern not in content:
                issues.append(f".dockerignore 缺少关键模式: {pattern}")
    else:
        # 检查是否有根目录构建
        compose_files = find_compose_files(project_root)
        has_root_context = False

        for compose_file in compose_files:
            try:
                content = compose_file.read_text()
                if re.search(r'context:\s*["\']?\./?["\']?\s*$', content, re.MULTILINE):
                    has_root_context = True
                    break
            except Exception:
                pass

        if has_root_context:
            issues.append("根目录缺少 .dockerignore（存在 context: . 配置）")

    if issues:
        return CheckResult(
            check_name="dockerignore",
            passed=False,
            message=f"发现 {len(issues)} 个 .dockerignore 问题",
            severity="warning",
            details=issues,
        )

    return CheckResult(
        check_name="dockerignore",
        passed=True,
        message=".dockerignore 配置正确",
        severity="info",
        details=details if verbose else [],
    )


def check_manifest_paths(
    project_root: Path, manifest_path: Path | None, verbose: bool = False
) -> CheckResult:
    """检查 manifest 中声明的路径是否存在"""
    if manifest_path is None:
        return CheckResult(
            check_name="manifest_paths",
            passed=True,
            message="未指定 manifest 文件，跳过检查",
            severity="info",
        )

    if not manifest_path.exists():
        return CheckResult(
            check_name="manifest_paths",
            passed=False,
            message=f"Manifest 文件不存在: {manifest_path}",
            severity="error",
        )

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return CheckResult(
            check_name="manifest_paths",
            passed=False,
            message=f"Manifest JSON 解析失败: {e}",
            severity="error",
        )

    missing_paths = []
    found_paths = []

    # 检查 manifest 中的路径
    def check_paths_in_dict(d: dict[str, Any], prefix: str = "") -> None:
        for key, value in d.items():
            current_path = f"{prefix}.{key}" if prefix else key

            if isinstance(value, dict):
                check_paths_in_dict(value, current_path)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        check_paths_in_dict(item, f"{current_path}[{i}]")
                    elif isinstance(item, str) and (
                        item.startswith("./") or item.startswith("apps/") or item.startswith("libs/")
                    ):
                        full_path = project_root / item
                        if full_path.exists():
                            found_paths.append(f"{current_path}[{i}]: {item}")
                        else:
                            missing_paths.append(f"{current_path}[{i}]: {item}")
            elif isinstance(value, str):
                # 检查看起来像路径的字符串
                if value.startswith("./") or value.startswith("apps/") or value.startswith("libs/"):
                    full_path = project_root / value
                    if full_path.exists():
                        found_paths.append(f"{current_path}: {value}")
                    else:
                        missing_paths.append(f"{current_path}: {value}")

    check_paths_in_dict(manifest)

    if missing_paths:
        return CheckResult(
            check_name="manifest_paths",
            passed=False,
            message=f"Manifest 中 {len(missing_paths)} 个路径不存在",
            severity="error",
            details=missing_paths + (found_paths if verbose else []),
        )

    return CheckResult(
        check_name="manifest_paths",
        passed=True,
        message=f"Manifest 中所有 {len(found_paths)} 个路径有效",
        severity="info",
        details=found_paths if verbose else [],
    )


# =============================================================================
# 输出格式化
# =============================================================================


# ANSI 颜色
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"  # No Color


def print_human_readable(report: PreflightReport, verbose: bool = False) -> None:
    """打印人类可读的报告"""
    print("=" * 50)
    print("Engram 项目导入预检")
    print("=" * 50)
    print(f"\n项目路径: {report.project_root}")
    print(f"Compose 文件: {len(report.compose_files)}")
    print()

    for check in report.checks:
        if check.passed:
            status = f"{GREEN}[OK]{NC}"
        elif check.severity == "warning":
            status = f"{YELLOW}[WARN]{NC}"
        else:
            status = f"{RED}[FAIL]{NC}"

        print(f"{status} {check.message}")

        if check.details and (verbose or not check.passed):
            for detail in check.details[:10]:  # 最多显示 10 条
                print(f"       {detail}")
            if len(check.details) > 10:
                print(f"       ... 还有 {len(check.details) - 10} 条")

    print()
    print("=" * 50)
    if report.ok:
        result_msg = f"{GREEN}[OK] 预检通过{NC}"
        if report.warnings > 0:
            result_msg += f" ({YELLOW}警告: {report.warnings}{NC})"
        print(result_msg)
    else:
        print(f"{RED}[FAIL] 预检失败{NC}")
        print(f"       {RED}错误: {report.errors}{NC}")
        print(f"       {YELLOW}警告: {report.warnings}{NC}")
    print("=" * 50)


def print_json(report: PreflightReport) -> None:
    """打印 JSON 格式的报告"""
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))


# =============================================================================
# 主函数
# =============================================================================


def run_preflight(
    project_root: Path,
    manifest_path: Path | None = None,
    verbose: bool = False,
) -> PreflightReport:
    """执行预检"""
    # 查找 compose 文件
    compose_files = find_compose_files(project_root)

    # 创建报告
    report = PreflightReport(
        project_root=str(project_root),
        compose_files=[str(f.relative_to(project_root)) for f in compose_files],
    )

    # 执行检查
    report.checks.append(check_compose_file_exists(project_root, compose_files))

    if compose_files:
        report.checks.append(check_build_contexts(project_root, compose_files, verbose))
        report.checks.append(check_volume_mounts(project_root, compose_files, verbose))

    report.checks.append(check_dockerfile_patterns(project_root, verbose))
    report.checks.append(check_dockerignore(project_root, verbose))
    report.checks.append(check_manifest_paths(project_root, manifest_path, verbose))

    return report


def main() -> int:
    """主入口"""
    parser = argparse.ArgumentParser(
        description="Engram 项目导入预检工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 检查当前目录
  python scripts/import_preflight.py .

  # 检查指定项目目录
  python scripts/import_preflight.py /path/to/project

  # 使用 manifest 文件
  python scripts/import_preflight.py /path/to/project --manifest manifest.json

  # JSON 输出
  python scripts/import_preflight.py /path/to/project --json

  # 详细模式
  python scripts/import_preflight.py /path/to/project --verbose
""",
    )

    parser.add_argument(
        "project_root",
        type=Path,
        help="项目根目录路径",
    )
    parser.add_argument(
        "--manifest",
        "-m",
        type=Path,
        default=None,
        help="Manifest 文件路径（可选）",
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

    args = parser.parse_args()

    # 验证项目路径
    project_root = args.project_root.resolve()
    if not project_root.exists():
        print(f"错误: 项目路径不存在: {project_root}", file=sys.stderr)
        return 2

    if not project_root.is_dir():
        print(f"错误: 项目路径不是目录: {project_root}", file=sys.stderr)
        return 2

    # 执行预检
    report = run_preflight(
        project_root=project_root,
        manifest_path=args.manifest,
        verbose=args.verbose,
    )

    # 输出报告
    if args.json:
        print_json(report)
    else:
        print_human_readable(report, args.verbose)

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
