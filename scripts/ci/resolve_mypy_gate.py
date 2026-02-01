#!/usr/bin/env python3
"""
mypy 门禁解析脚本

功能:
根据环境变量和迁移阶段，解析出最终的 mypy 门禁级别。

输入（环境变量或命令行参数）:
- phase:          迁移阶段 (0/1/2/3)
- override:       强制覆盖值 (baseline/strict/warn/off)
- threshold:      PR 提升为 strict 的 baseline 错误阈值
- branch:         当前分支名
- ref:            git ref (如 refs/heads/master)
- baseline_count: 当前 baseline 错误数量（可选）

规则优先级:
1. override 最高优先（如果指定 baseline/strict/warn/off，直接返回）
2. phase=0: 默认 baseline
3. phase=1: main/master 分支 = strict，其他分支 = baseline
           当 baseline_count <= threshold 时，PR 可提升为 strict
4. phase=2: 全部 strict
5. phase=3: baseline 已归档，仅 strict

输出:
- 输出到 stdout: 最终 gate 值（baseline/strict/warn/off）
- 若指定 --github-output，同时写入 GITHUB_OUTPUT 文件

用法:
    # 基本用法
    python scripts/ci/resolve_mypy_gate.py --phase 1 --branch master

    # 使用环境变量
    export ENGRAM_MYPY_MIGRATION_PHASE=1
    export GITHUB_REF=refs/heads/master
    python scripts/ci/resolve_mypy_gate.py

    # 强制覆盖
    python scripts/ci/resolve_mypy_gate.py --override baseline

    # 检查阈值提升
    python scripts/ci/resolve_mypy_gate.py --phase 1 --branch feature-x --baseline-count 0 --threshold 0

    # GitHub Actions 中使用
    python scripts/ci/resolve_mypy_gate.py --github-output

    # 输出决策理由（CI 日志审计）
    python scripts/ci/resolve_mypy_gate.py --phase 1 --branch feature-x --explain
    # 输出示例:
    # [DECISION] gate=baseline
    # [REASON]   Phase 1: 非默认分支 (feature-x) 使用 baseline 模式，仅检测新增错误
    # [CONTEXT]  phase=1
    # [CONTEXT]  branch=feature-x

退出码:
    0 - 解析成功
    1 - 参数错误
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# 有效的门禁级别
# - baseline: 对比基线，仅新增错误时失败（当前默认）
# - strict: 任何 mypy 错误都失败（目标状态）
# - warn: 运行 mypy 并输出错误，但不阻断 CI
# - off: 跳过检查，不运行 mypy
VALID_GATES = {"strict", "baseline", "warn", "off"}

# 默认分支列表（这些分支在 phase=1 时使用 strict）
DEFAULT_BRANCHES = {"main", "master"}

# 环境变量名称
ENV_MIGRATION_PHASE = "ENGRAM_MYPY_MIGRATION_PHASE"
ENV_GATE_OVERRIDE = "ENGRAM_MYPY_GATE_OVERRIDE"
ENV_STRICT_THRESHOLD = "ENGRAM_MYPY_STRICT_THRESHOLD"
ENV_GITHUB_REF = "GITHUB_REF"
ENV_GITHUB_HEAD_REF = "GITHUB_HEAD_REF"
ENV_GITHUB_OUTPUT = "GITHUB_OUTPUT"


def extract_branch_from_ref(ref: str) -> str:
    """
    从 git ref 中提取分支名。

    Args:
        ref: git ref，如 refs/heads/master 或 refs/pull/123/merge

    Returns:
        分支名，如 master
    """
    if ref.startswith("refs/heads/"):
        return ref.replace("refs/heads/", "")
    if ref.startswith("refs/pull/"):
        # PR ref 格式: refs/pull/123/merge
        # 实际分支应从 GITHUB_HEAD_REF 获取
        return ""
    return ref


class GateDecision:
    """门禁决策结果，包含决策值和理由说明。"""

    def __init__(self, gate: str, reason: str, details: dict[str, str | int | None]):
        self.gate = gate
        self.reason = reason
        self.details = details

    def explain(self) -> str:
        """返回决策理由的详细说明（用于 CI 日志审计）。"""
        lines = [
            f"[DECISION] gate={self.gate}",
            f"[REASON]   {self.reason}",
        ]
        for key, value in self.details.items():
            lines.append(f"[CONTEXT]  {key}={value}")
        return "\n".join(lines)


def resolve_gate(
    phase: int,
    override: Optional[str] = None,
    threshold: int = 0,
    branch: Optional[str] = None,
    ref: Optional[str] = None,
    baseline_count: Optional[int] = None,
    verbose: bool = False,
    explain: bool = False,
) -> str:
    """
    解析最终的 mypy 门禁级别。

    Args:
        phase:          迁移阶段 (0/1/2/3)
        override:       强制覆盖值 (baseline/strict/warn/off)
        threshold:      PR 提升 strict 的阈值
        branch:         当前分支名
        ref:            git ref
        baseline_count: 当前 baseline 错误数量
        verbose:        是否输出详细信息
        explain:        是否输出决策理由（CI 审计用）

    Returns:
        门禁级别: "baseline", "strict", "warn" 或 "off"

    优先级:
        override > phase 逻辑

    Gate 级别说明:
        - baseline: 对比基线，仅新增错误时失败（当前默认）
        - strict: 任何 mypy 错误都失败（目标状态）
        - warn: 运行 mypy 并输出错误，但不阻断 CI
        - off: 跳过检查，不运行 mypy
    """
    decision: GateDecision | None = None

    # 1. override 最高优先（优先级: override > phase）
    if override and override in VALID_GATES:
        if verbose:
            print(f"[resolve] override={override} → 使用覆盖值", file=sys.stderr)
        # 根据 override 值生成更具体的原因说明
        reason_suffix = {
            "baseline": "使用基线对比模式",
            "strict": "使用严格模式（任何错误阻断）",
            "warn": "仅警告模式（不阻断 CI）",
            "off": "跳过 mypy 检查",
        }.get(override, "")
        decision = GateDecision(
            gate=override,
            reason=f"ENGRAM_MYPY_GATE_OVERRIDE 覆盖值生效（{reason_suffix}），跳过 phase 逻辑",
            details={"override": override, "phase": phase},
        )
        if explain:
            print(decision.explain(), file=sys.stderr)
        return override

    # 2. 从 ref 提取分支名（如果未直接提供 branch）
    if not branch and ref:
        branch = extract_branch_from_ref(ref)
        if verbose:
            print(f"[resolve] 从 ref={ref} 提取 branch={branch}", file=sys.stderr)

    # 3. 根据 phase 解析
    if phase == 0:
        # 阶段 0: 默认 baseline
        if verbose:
            print("[resolve] phase=0 → baseline", file=sys.stderr)
        decision = GateDecision(
            gate="baseline",
            reason="Phase 0: 默认 baseline 模式，mypy 错误通过基线对比检测",
            details={"phase": phase, "branch": branch},
        )
        if explain:
            print(decision.explain(), file=sys.stderr)
        return "baseline"

    elif phase == 1:
        # 阶段 1: main/master = strict，其他 = baseline
        is_default_branch = branch in DEFAULT_BRANCHES

        if is_default_branch:
            if verbose:
                print(f"[resolve] phase=1, branch={branch} (默认分支) → strict", file=sys.stderr)
            decision = GateDecision(
                gate="strict",
                reason=f"Phase 1: 默认分支 ({branch}) 强制 strict 模式，任何 mypy 错误都会阻断",
                details={"phase": phase, "branch": branch, "is_default_branch": "true"},
            )
            if explain:
                print(decision.explain(), file=sys.stderr)
            return "strict"

        # 非默认分支，检查阈值提升
        if baseline_count is not None and baseline_count <= threshold:
            if verbose:
                print(
                    f"[resolve] phase=1, branch={branch}, "
                    f"baseline_count={baseline_count} <= threshold={threshold} → strict (阈值提升)",
                    file=sys.stderr,
                )
            decision = GateDecision(
                gate="strict",
                reason=f"Phase 1 阈值提升: baseline_count ({baseline_count}) <= threshold ({threshold})，PR 自动提升为 strict",
                details={
                    "phase": phase,
                    "branch": branch,
                    "baseline_count": baseline_count,
                    "threshold": threshold,
                    "upgrade_triggered": "true",
                },
            )
            if explain:
                print(decision.explain(), file=sys.stderr)
            return "strict"

        if verbose:
            print(f"[resolve] phase=1, branch={branch} (非默认分支) → baseline", file=sys.stderr)
        decision = GateDecision(
            gate="baseline",
            reason=f"Phase 1: 非默认分支 ({branch}) 使用 baseline 模式，仅检测新增错误",
            details={
                "phase": phase,
                "branch": branch,
                "baseline_count": baseline_count,
                "threshold": threshold,
                "upgrade_triggered": "false"
                if baseline_count is not None
                else "n/a (baseline_count not provided)",
            },
        )
        if explain:
            print(decision.explain(), file=sys.stderr)
        return "baseline"

    elif phase == 2:
        # 阶段 2: 全部 strict
        if verbose:
            print("[resolve] phase=2 → strict", file=sys.stderr)
        decision = GateDecision(
            gate="strict",
            reason="Phase 2: 全仓 strict 模式，所有分支任何 mypy 错误都会阻断",
            details={"phase": phase, "branch": branch},
        )
        if explain:
            print(decision.explain(), file=sys.stderr)
        return "strict"

    elif phase == 3:
        # 阶段 3: baseline 已归档，仅 strict
        if verbose:
            print("[resolve] phase=3 → strict (baseline 已归档)", file=sys.stderr)
        decision = GateDecision(
            gate="strict",
            reason="Phase 3: baseline 已归档，仅支持 strict 模式",
            details={"phase": phase, "branch": branch, "baseline_archived": "true"},
        )
        if explain:
            print(decision.explain(), file=sys.stderr)
        return "strict"

    else:
        # 未知阶段，默认 baseline
        if verbose:
            print(f"[resolve] phase={phase} (未知) → baseline (默认)", file=sys.stderr)
        decision = GateDecision(
            gate="baseline",
            reason=f"未知 Phase ({phase}): 安全回退到 baseline 模式",
            details={"phase": phase, "branch": branch, "fallback": "true"},
        )
        if explain:
            print(decision.explain(), file=sys.stderr)
        return "baseline"


def write_github_output(gate: str) -> None:
    """
    将 gate 值写入 GITHUB_OUTPUT 文件。

    Args:
        gate: 门禁级别
    """
    github_output = os.environ.get(ENV_GITHUB_OUTPUT)
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"gate={gate}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mypy 门禁解析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        type=int,
        default=None,
        help=f"迁移阶段 (0/1/2/3)，默认从环境变量 {ENV_MIGRATION_PHASE} 读取",
    )
    parser.add_argument(
        "--override",
        type=str,
        default=None,
        help=f"强制覆盖值 (baseline/strict/warn/off)，默认从环境变量 {ENV_GATE_OVERRIDE} 读取。优先级: override > phase",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help=f"PR 提升 strict 的阈值，默认从环境变量 {ENV_STRICT_THRESHOLD} 读取",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default=None,
        help="当前分支名",
    )
    parser.add_argument(
        "--ref",
        type=str,
        default=None,
        help=f"git ref，默认从环境变量 {ENV_GITHUB_REF} 读取",
    )
    parser.add_argument(
        "--baseline-count",
        type=int,
        default=None,
        help="当前 baseline 错误数量（用于阈值检查）",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="将结果写入 GITHUB_OUTPUT 文件",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细解析过程",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="输出决策理由说明（CI 日志审计用），包含 [DECISION]/[REASON]/[CONTEXT] 字段",
    )
    args = parser.parse_args()

    # 从环境变量读取默认值
    phase = args.phase
    if phase is None:
        phase_env = os.environ.get(ENV_MIGRATION_PHASE, "0")
        try:
            phase = int(phase_env)
        except ValueError:
            print(f"[ERROR] 无效的 phase 值: {phase_env}", file=sys.stderr)
            return 1

    override = args.override
    if override is None:
        override = os.environ.get(ENV_GATE_OVERRIDE)

    threshold = args.threshold
    if threshold is None:
        threshold_env = os.environ.get(ENV_STRICT_THRESHOLD, "0")
        try:
            threshold = int(threshold_env)
        except ValueError:
            threshold = 0

    ref = args.ref
    if ref is None:
        ref = os.environ.get(ENV_GITHUB_REF, "")

    branch = args.branch
    if branch is None:
        # 对于 PR，使用 GITHUB_HEAD_REF
        branch = os.environ.get(ENV_GITHUB_HEAD_REF, "")

    # 解析门禁级别
    gate = resolve_gate(
        phase=phase,
        override=override,
        threshold=threshold,
        branch=branch,
        ref=ref,
        baseline_count=args.baseline_count,
        verbose=args.verbose,
        explain=args.explain,
    )

    # 输出结果
    print(gate)

    # 写入 GITHUB_OUTPUT
    if args.github_output:
        write_github_output(gate)

    return 0


if __name__ == "__main__":
    sys.exit(main())
