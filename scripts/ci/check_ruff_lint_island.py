#!/usr/bin/env python3
"""
Ruff Lint-Island 门禁检查脚本

功能：
1. 读取 pyproject.toml 中的 lint_island_paths 与 p1_rules
2. 对 lint-island 模块执行 P1 规则检查（B, UP, SIM, PERF, PTH）
3. 支持 Phase 分级控制

Phase 读取优先级：
1. CLI 参数 --phase（最高优先级，用于本地调试/测试）
2. 环境变量 ENGRAM_RUFF_PHASE（CI 使用此方式注入）
3. pyproject.toml [tool.engram.ruff] current_phase
4. 默认值 0

Phase 定义与实现状态：
- Phase 0: 跳过 lint-island 检查（仅运行基础规则）【已实现】
- Phase 1: 对 lint-island 路径执行 P1 规则检查，失败阻断【已实现】
- Phase 2: 对全仓执行 P1 规则检查【已实现】
    * 扫描路径: DEFAULT_SCAN_ROOT (src/, tests/)
    * 与 Phase 1 共享 P1 规则集
- Phase 3: 清理旧 noqa + RUF100 检查【已实现】
    * 基于 Phase 2 全仓 P1 检查
    * 额外启用 RUF100 规则检测冗余 noqa
    * 分工边界: check_noqa_policy.py 检查裸 noqa 语法，本脚本检查冗余 noqa 语义

CI 配置（.github/workflows/ci.yml）：
    python scripts/ci/check_ruff_lint_island.py \
      --phase "${{ vars.ENGRAM_RUFF_PHASE || '0' }}" \
      --verbose

环境变量：
- ENGRAM_RUFF_PHASE: Phase 级别 (0/1/2/3)

用法：
    # 默认模式（读取 pyproject phase）
    python scripts/ci/check_ruff_lint_island.py

    # 强制指定 phase
    python scripts/ci/check_ruff_lint_island.py --phase 1

    # 详细输出
    python scripts/ci/check_ruff_lint_island.py --verbose

    # JSON 输出（CI artifact）
    python scripts/ci/check_ruff_lint_island.py --json

退出码：
    0 - 检查通过（或 Phase 0 跳过）
    1 - 检查失败（存在 violation）
    2 - 配置错误

相关文档：
- docs/architecture/adr_ruff_gate_and_rollout.md
- docs/dev/ci_gate_runbook.md Section 1.2
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ============================================================================
# 常量配置
# ============================================================================

# 环境变量名称
ENV_RUFF_PHASE = "ENGRAM_RUFF_PHASE"

# 默认 Phase
DEFAULT_PHASE = 0

# 默认 P1 规则（fallback，优先从 pyproject 读取）
DEFAULT_P1_RULES = ["B", "UP", "SIM", "PERF", "PTH"]

# 默认扫描根路径
DEFAULT_SCAN_ROOT = ["src/", "tests/"]

# Phase 3 额外规则（检测冗余 noqa）
PHASE3_EXTRA_RULES = ["RUF100"]


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class LintIslandConfig:
    """Lint-Island 配置"""

    lint_island_paths: list[str] = field(default_factory=list)
    p1_rules: list[str] = field(default_factory=lambda: DEFAULT_P1_RULES.copy())
    current_phase: int = DEFAULT_PHASE


@dataclass
class CheckResult:
    """检查结果"""

    success: bool
    phase: int
    phase_source: str  # 'env' | 'pyproject' | 'default' | 'cli'
    lint_island_paths: list[str]
    p1_rules: list[str]
    violations: list[dict[str, Any]]
    violation_count: int
    by_code: dict[str, int] = field(default_factory=dict)
    by_file: dict[str, int] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str = ""
    error_message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============================================================================
# 配置加载
# ============================================================================


def get_project_root() -> Path:
    """获取项目根目录"""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent  # scripts/ci/ -> 项目根目录


def load_pyproject_config(project_root: Path) -> LintIslandConfig:
    """
    从 pyproject.toml 加载 lint-island 配置。

    Returns:
        LintIslandConfig 实例
    """
    pyproject_path = project_root / "pyproject.toml"

    if not pyproject_path.exists():
        return LintIslandConfig()

    try:
        # Python 3.11+ 内置 tomllib，3.10 需要 tomli
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomli as tomllib
            except ImportError:
                # fallback: 简单解析
                return _parse_pyproject_fallback(pyproject_path)

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        engram_ruff = data.get("tool", {}).get("engram", {}).get("ruff", {})

        return LintIslandConfig(
            lint_island_paths=engram_ruff.get("lint_island_paths", []),
            p1_rules=engram_ruff.get("p1_rules", DEFAULT_P1_RULES.copy()),
            current_phase=engram_ruff.get("current_phase", DEFAULT_PHASE),
        )
    except Exception as e:
        print(f"[WARN] 无法解析 pyproject.toml: {e}", file=sys.stderr)
        return LintIslandConfig()


def _parse_pyproject_fallback(pyproject_path: Path) -> LintIslandConfig:
    """
    简单的 pyproject.toml 解析 fallback。

    仅解析关键字段，不做完整 TOML 解析。
    """
    content = pyproject_path.read_text(encoding="utf-8")
    config = LintIslandConfig()

    # 简单正则解析
    import re

    # 解析 lint_island_paths
    paths_match = re.search(r"lint_island_paths\s*=\s*\[(.*?)\]", content, re.DOTALL)
    if paths_match:
        paths_str = paths_match.group(1)
        paths = re.findall(r'"([^"]+)"', paths_str)
        config.lint_island_paths = paths

    # 解析 p1_rules
    rules_match = re.search(r"p1_rules\s*=\s*\[(.*?)\]", content, re.DOTALL)
    if rules_match:
        rules_str = rules_match.group(1)
        rules = re.findall(r'"([^"]+)"', rules_str)
        config.p1_rules = rules

    # 解析 current_phase
    phase_match = re.search(r"current_phase\s*=\s*(\d+)", content)
    if phase_match:
        config.current_phase = int(phase_match.group(1))

    return config


def resolve_phase(cli_phase: int | None, pyproject_phase: int) -> tuple[int, str]:
    """
    解析 Phase 值，按优先级返回。

    优先级：CLI 参数 > 环境变量 > pyproject > 默认值

    Args:
        cli_phase: CLI 指定的 phase（可选）
        pyproject_phase: pyproject.toml 中的 phase

    Returns:
        (phase, source) 元组
    """
    # 1. CLI 参数最高优先级
    if cli_phase is not None:
        return cli_phase, "cli"

    # 2. 环境变量
    env_phase = os.environ.get(ENV_RUFF_PHASE)
    if env_phase is not None:
        try:
            return int(env_phase), "env"
        except ValueError:
            print(
                f"[WARN] 无效的 {ENV_RUFF_PHASE} 值 '{env_phase}'，忽略",
                file=sys.stderr,
            )

    # 3. pyproject.toml
    if pyproject_phase != DEFAULT_PHASE:
        return pyproject_phase, "pyproject"

    # 4. 默认值
    return DEFAULT_PHASE, "default"


# ============================================================================
# Ruff 执行
# ============================================================================


def normalize_filepath(filepath: str, project_root: Path) -> str:
    """将文件路径规范化为相对路径"""
    try:
        path = Path(filepath)
        if path.is_absolute():
            return str(path.relative_to(project_root))
        return filepath
    except ValueError:
        return filepath


def run_ruff_with_rules(
    paths: list[str],
    rules: list[str],
    project_root: Path,
) -> tuple[list[dict[str, Any]], int]:
    """
    运行 ruff check 并指定额外规则。

    Args:
        paths: 扫描路径列表
        rules: P1 规则列表
        project_root: 项目根目录

    Returns:
        (violations_list, return_code) 元组
    """
    # 构建 --extend-select 参数
    extend_select = ",".join(rules)

    # 过滤出存在的路径
    valid_paths = []
    for p in paths:
        full_path = project_root / p
        if full_path.exists():
            valid_paths.append(p)
        else:
            print(f"[WARN] 路径不存在，跳过: {p}", file=sys.stderr)

    if not valid_paths:
        return [], 0

    cmd = [
        "ruff",
        "check",
        "--output-format=json",
        f"--extend-select={extend_select}",
    ] + valid_paths

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    output = result.stdout
    if not output.strip():
        if result.returncode != 0 and result.stderr:
            # 忽略 "No files to check" 警告
            if "No files to check" not in result.stderr:
                print(f"[WARN] ruff 命令输出: {result.stderr}", file=sys.stderr)
        return [], result.returncode

    try:
        violations = json.loads(output)
        if not isinstance(violations, list):
            violations = []

        # 规范化文件路径
        for v in violations:
            if "filename" in v:
                v["filename"] = normalize_filepath(v["filename"], project_root)

        # 过滤出 P1 规则的 violations
        p1_violations = []
        for v in violations:
            code = v.get("code", "")
            # 检查 code 是否属于 P1 规则（前缀匹配）
            for rule in rules:
                if code.startswith(rule):
                    p1_violations.append(v)
                    break

        return p1_violations, result.returncode
    except json.JSONDecodeError as e:
        print(f"[ERROR] 无法解析 ruff JSON 输出: {e}", file=sys.stderr)
        return [], 1


def aggregate_violations(violations: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    """
    聚合 violations 统计。

    Returns:
        (by_code, by_file) 元组
    """
    by_code: dict[str, int] = defaultdict(int)
    by_file: dict[str, int] = defaultdict(int)

    for v in violations:
        code = v.get("code", "UNKNOWN")
        filename = v.get("filename", "UNKNOWN")
        by_code[code] += 1
        by_file[filename] += 1

    # 排序
    by_code = dict(sorted(by_code.items(), key=lambda x: x[1], reverse=True))
    by_file = dict(sorted(by_file.items(), key=lambda x: x[1], reverse=True))

    return by_code, by_file


# ============================================================================
# 输出格式化
# ============================================================================


def format_text_output(result: CheckResult, verbose: bool = False) -> str:
    """格式化文本输出"""
    lines = []
    lines.append("=" * 70)

    # 根据 Phase 显示不同标题
    if result.phase == 2:
        lines.append("Ruff 全仓 P1 规则检查 (Phase 2)")
    elif result.phase == 3:
        lines.append("Ruff 全仓 P1 规则 + noqa 清理检查 (Phase 3)")
    else:
        lines.append("Ruff Lint-Island 门禁检查")

    lines.append("=" * 70)
    lines.append("")

    # 基本信息
    lines.append(f"Phase:              {result.phase} (来源: {result.phase_source})")
    lines.append(f"规则集:             {', '.join(result.p1_rules)}")

    # 根据 Phase 显示不同的路径描述
    if result.phase >= 2:
        lines.append(f"扫描路径:           {', '.join(result.lint_island_paths)}")
    else:
        lines.append(f"Lint-Island 路径:   {len(result.lint_island_paths)} 个")

    if verbose and result.lint_island_paths and result.phase < 2:
        for path in result.lint_island_paths:
            lines.append(f"                    - {path}")

    lines.append("")

    # 跳过检查
    if result.skipped:
        lines.append(f"[SKIP] {result.skip_reason}")
        lines.append("")
        lines.append("=" * 70)
        lines.append("[OK] 退出码: 0")
        return "\n".join(lines)

    # 配置错误
    if result.error_message:
        lines.append(f"[ERROR] {result.error_message}")
        lines.append("")
        lines.append("=" * 70)
        lines.append("[FAIL] 退出码: 2")
        return "\n".join(lines)

    # 检查结果
    lines.append(f"Violation 数:       {result.violation_count}")

    # Phase 3 额外显示 RUF100 数量
    if result.phase == 3 and result.by_code:
        ruf100_count = result.by_code.get("RUF100", 0)
        p1_count = result.violation_count - ruf100_count
        lines.append(f"  - P1 Violations:  {p1_count}")
        lines.append(f"  - RUF100 (冗余noqa): {ruf100_count}")

    lines.append("")

    if result.success:
        if result.phase == 2:
            lines.append("[OK] 全仓 P1 规则检查通过")
        elif result.phase == 3:
            lines.append("[OK] 全仓 P1 规则 + noqa 清理检查通过")
        else:
            lines.append("[OK] Lint-Island 检查通过，无 P1 violation")
    else:
        lines.append(f"[FAIL] 存在 {result.violation_count} 个 P1 violation")
        lines.append("")

        # 按 code 统计
        if result.by_code:
            lines.append("Violation 统计（按 code）:")
            for code, count in list(result.by_code.items())[:10]:
                lines.append(f"  {code}: {count}")

        # 详细列表
        if verbose and result.violations:
            lines.append("")
            lines.append("详细列表（前 20 条）:")
            for v in result.violations[:20]:
                filename = v.get("filename", "")
                location = v.get("location", {})
                line = location.get("row", 0)
                code = v.get("code", "")
                message = v.get("message", "")[:60]
                lines.append(f"  {filename}:{line} [{code}] {message}")
            if len(result.violations) > 20:
                lines.append(f"  ... 及其他 {len(result.violations) - 20} 条")

    lines.append("")
    lines.append("=" * 70)
    exit_code = 0 if result.success else 1
    lines.append(f"[{'OK' if result.success else 'FAIL'}] 退出码: {exit_code}")

    return "\n".join(lines)


def format_json_output(result: CheckResult) -> str:
    """格式化 JSON 输出"""
    output = {
        "success": result.success,
        "phase": result.phase,
        "phase_source": result.phase_source,
        "lint_island_paths": result.lint_island_paths,
        "p1_rules": result.p1_rules,
        "violation_count": result.violation_count,
        "by_code": result.by_code,
        "by_file": dict(list(result.by_file.items())[:20]),  # 限制文件数
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "error_message": result.error_message,
        "timestamp": result.timestamp,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)


# ============================================================================
# 主函数
# ============================================================================


def run_lint_island_check(
    phase: int,
    phase_source: str,
    config: LintIslandConfig,
    project_root: Path,
    verbose: bool = False,
) -> CheckResult:
    """
    执行 Lint-Island 检查。

    Args:
        phase: 当前 phase
        phase_source: phase 来源
        config: lint-island 配置
        project_root: 项目根目录
        verbose: 详细输出

    Returns:
        CheckResult 实例
    """
    # Phase 0: 跳过检查
    if phase == 0:
        return CheckResult(
            success=True,
            phase=phase,
            phase_source=phase_source,
            lint_island_paths=config.lint_island_paths,
            p1_rules=config.p1_rules,
            violations=[],
            violation_count=0,
            skipped=True,
            skip_reason="Phase 0: 跳过 lint-island 检查",
        )

    # 检查 p1_rules 配置
    if not config.p1_rules:
        return CheckResult(
            success=False,
            phase=phase,
            phase_source=phase_source,
            lint_island_paths=config.lint_island_paths,
            p1_rules=[],
            violations=[],
            violation_count=0,
            error_message="p1_rules 为空，无法执行检查",
        )

    # Phase 1: 对 lint-island 路径执行 P1 规则检查
    if phase == 1:
        # Phase 1 需要 lint_island_paths
        if not config.lint_island_paths:
            return CheckResult(
                success=True,
                phase=phase,
                phase_source=phase_source,
                lint_island_paths=[],
                p1_rules=config.p1_rules,
                violations=[],
                violation_count=0,
                skipped=True,
                skip_reason="lint_island_paths 为空，跳过检查",
            )

        violations, _ = run_ruff_with_rules(
            config.lint_island_paths,
            config.p1_rules,
            project_root,
        )

        by_code, by_file = aggregate_violations(violations)

        return CheckResult(
            success=len(violations) == 0,
            phase=phase,
            phase_source=phase_source,
            lint_island_paths=config.lint_island_paths,
            p1_rules=config.p1_rules,
            violations=violations,
            violation_count=len(violations),
            by_code=by_code,
            by_file=by_file,
        )

    # Phase 2: 对全仓执行 P1 规则检查
    if phase == 2:
        scan_paths = DEFAULT_SCAN_ROOT

        violations, _ = run_ruff_with_rules(
            scan_paths,
            config.p1_rules,
            project_root,
        )

        by_code, by_file = aggregate_violations(violations)

        return CheckResult(
            success=len(violations) == 0,
            phase=phase,
            phase_source=phase_source,
            lint_island_paths=scan_paths,  # Phase 2 使用全仓路径
            p1_rules=config.p1_rules,
            violations=violations,
            violation_count=len(violations),
            by_code=by_code,
            by_file=by_file,
        )

    # Phase 3: 对全仓执行 P1 规则检查 + RUF100 冗余 noqa 检查
    if phase == 3:
        scan_paths = DEFAULT_SCAN_ROOT

        # 合并 P1 规则和 RUF100 规则
        all_rules = config.p1_rules + PHASE3_EXTRA_RULES

        violations, _ = run_ruff_with_rules(
            scan_paths,
            all_rules,
            project_root,
        )

        by_code, by_file = aggregate_violations(violations)

        return CheckResult(
            success=len(violations) == 0,
            phase=phase,
            phase_source=phase_source,
            lint_island_paths=scan_paths,  # Phase 3 使用全仓路径
            p1_rules=all_rules,  # 包含 RUF100
            violations=violations,
            violation_count=len(violations),
            by_code=by_code,
            by_file=by_file,
        )

    # 不应到达
    return CheckResult(
        success=False,
        phase=phase,
        phase_source=phase_source,
        lint_island_paths=config.lint_island_paths,
        p1_rules=config.p1_rules,
        violations=[],
        violation_count=0,
        error_message=f"未知的 phase 值: {phase}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ruff Lint-Island 门禁检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help=(
            f"强制指定 Phase 级别 "
            f"(默认: 读取 {ENV_RUFF_PHASE} 或 pyproject.toml)"
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="输出 JSON 格式（便于 CI artifact）",
    )

    args = parser.parse_args()

    # 获取项目根目录
    project_root = get_project_root()

    # 加载 pyproject 配置
    config = load_pyproject_config(project_root)

    # 解析 phase
    phase, phase_source = resolve_phase(args.phase, config.current_phase)

    # 执行检查
    result = run_lint_island_check(
        phase=phase,
        phase_source=phase_source,
        config=config,
        project_root=project_root,
        verbose=args.verbose,
    )

    # 输出结果
    if args.json_output:
        print(format_json_output(result))
    else:
        print(format_text_output(result, verbose=args.verbose))

    # 返回退出码
    if result.error_message:
        return 2
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
