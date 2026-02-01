"""
tests/test_resolve_mypy_gate.py

resolve_mypy_gate.py 脚本的单元测试

测试覆盖:
- override 最高优先级
- phase=0 默认 baseline
- phase=1 main/master 分支 strict，其他分支 baseline
- phase=1 阈值边界检查（baseline_count <= threshold 时提升 strict）
- phase=2 全 strict
- phase=3 baseline 已归档，仅 strict
- 从 git ref 提取分支名
- develop 分支在 phase=1 时应为 baseline
"""

from __future__ import annotations

# 导入待测试的模块
import sys
from pathlib import Path

# 添加 scripts/ci 到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "ci"))

from resolve_mypy_gate import (
    extract_branch_from_ref,
    resolve_gate,
)


class TestExtractBranchFromRef:
    """测试 extract_branch_from_ref 函数"""

    def test_refs_heads_master(self) -> None:
        """refs/heads/master → master"""
        assert extract_branch_from_ref("refs/heads/master") == "master"

    def test_refs_heads_main(self) -> None:
        """refs/heads/main → main"""
        assert extract_branch_from_ref("refs/heads/main") == "main"

    def test_refs_heads_develop(self) -> None:
        """refs/heads/develop → develop"""
        assert extract_branch_from_ref("refs/heads/develop") == "develop"

    def test_refs_heads_feature_branch(self) -> None:
        """refs/heads/feature/foo → feature/foo"""
        assert extract_branch_from_ref("refs/heads/feature/foo") == "feature/foo"

    def test_refs_pull_merge(self) -> None:
        """refs/pull/123/merge → '' (PR ref 无法提取分支)"""
        assert extract_branch_from_ref("refs/pull/123/merge") == ""

    def test_plain_branch_name(self) -> None:
        """直接的分支名应原样返回"""
        assert extract_branch_from_ref("master") == "master"


class TestResolveGateOverride:
    """测试 override 最高优先级"""

    def test_override_baseline(self) -> None:
        """override=baseline 应返回 baseline，忽略其他参数"""
        assert resolve_gate(phase=2, override="baseline") == "baseline"
        assert resolve_gate(phase=3, override="baseline", branch="master") == "baseline"

    def test_override_strict(self) -> None:
        """override=strict 应返回 strict，忽略其他参数"""
        assert resolve_gate(phase=0, override="strict") == "strict"
        assert resolve_gate(phase=1, override="strict", branch="develop") == "strict"

    def test_override_invalid_ignored(self) -> None:
        """无效的 override 应被忽略"""
        assert resolve_gate(phase=0, override="invalid") == "baseline"
        # 注意："warn" 是有效的 gate 值，应返回 "warn"（不是 "strict"）
        # 无效值示例：空字符串、拼写错误、大小写错误
        assert resolve_gate(phase=2, override="") == "strict"  # 空字符串被忽略

    def test_override_warn_is_valid(self) -> None:
        """override=warn 是有效值，应返回 warn（不阻断 CI）"""
        assert resolve_gate(phase=2, override="warn") == "warn"
        assert resolve_gate(phase=0, override="warn") == "warn"

    def test_override_off_is_valid(self) -> None:
        """override=off 是有效值，应返回 off（跳过检查）"""
        assert resolve_gate(phase=2, override="off") == "off"
        assert resolve_gate(phase=0, override="off") == "off"


class TestResolveGatePhase0:
    """测试 phase=0 默认 baseline"""

    def test_phase0_master(self) -> None:
        """phase=0, master 分支 → baseline"""
        assert resolve_gate(phase=0, branch="master") == "baseline"

    def test_phase0_main(self) -> None:
        """phase=0, main 分支 → baseline"""
        assert resolve_gate(phase=0, branch="main") == "baseline"

    def test_phase0_develop(self) -> None:
        """phase=0, develop 分支 → baseline"""
        assert resolve_gate(phase=0, branch="develop") == "baseline"

    def test_phase0_feature_branch(self) -> None:
        """phase=0, feature 分支 → baseline"""
        assert resolve_gate(phase=0, branch="feature/foo") == "baseline"

    def test_phase0_with_ref(self) -> None:
        """phase=0, 使用 ref → baseline"""
        assert resolve_gate(phase=0, ref="refs/heads/master") == "baseline"


class TestResolveGatePhase1:
    """测试 phase=1 分支策略"""

    def test_phase1_master_strict(self) -> None:
        """phase=1, master 分支 → strict"""
        assert resolve_gate(phase=1, branch="master") == "strict"

    def test_phase1_main_strict(self) -> None:
        """phase=1, main 分支 → strict"""
        assert resolve_gate(phase=1, branch="main") == "strict"

    def test_phase1_develop_baseline(self) -> None:
        """phase=1, develop 分支 → baseline（非默认分支）"""
        assert resolve_gate(phase=1, branch="develop") == "baseline"

    def test_phase1_feature_branch_baseline(self) -> None:
        """phase=1, feature 分支 → baseline"""
        assert resolve_gate(phase=1, branch="feature/foo") == "baseline"

    def test_phase1_pr_branch_baseline(self) -> None:
        """phase=1, PR 分支 → baseline"""
        assert resolve_gate(phase=1, branch="fix/bug-123") == "baseline"

    def test_phase1_master_from_ref(self) -> None:
        """phase=1, 从 ref 提取 master → strict"""
        assert resolve_gate(phase=1, ref="refs/heads/master") == "strict"

    def test_phase1_main_from_ref(self) -> None:
        """phase=1, 从 ref 提取 main → strict"""
        assert resolve_gate(phase=1, ref="refs/heads/main") == "strict"

    def test_phase1_develop_from_ref(self) -> None:
        """phase=1, 从 ref 提取 develop → baseline"""
        assert resolve_gate(phase=1, ref="refs/heads/develop") == "baseline"


class TestResolveGatePhase1Threshold:
    """测试 phase=1 阈值边界"""

    def test_threshold_baseline_count_zero(self) -> None:
        """baseline_count=0, threshold=0 → strict（PR 提升）"""
        assert (
            resolve_gate(phase=1, branch="feature/foo", baseline_count=0, threshold=0) == "strict"
        )

    def test_threshold_baseline_count_below(self) -> None:
        """baseline_count < threshold → strict（PR 提升）"""
        assert (
            resolve_gate(phase=1, branch="feature/foo", baseline_count=5, threshold=10) == "strict"
        )

    def test_threshold_baseline_count_equal(self) -> None:
        """baseline_count = threshold → strict（边界情况）"""
        assert (
            resolve_gate(phase=1, branch="feature/foo", baseline_count=10, threshold=10) == "strict"
        )

    def test_threshold_baseline_count_above(self) -> None:
        """baseline_count > threshold → baseline"""
        assert (
            resolve_gate(phase=1, branch="feature/foo", baseline_count=11, threshold=10)
            == "baseline"
        )

    def test_threshold_not_affect_default_branch(self) -> None:
        """默认分支不受阈值影响，始终 strict"""
        assert resolve_gate(phase=1, branch="master", baseline_count=100, threshold=0) == "strict"

    def test_threshold_none_baseline_count(self) -> None:
        """未提供 baseline_count 时，非默认分支使用 baseline"""
        assert (
            resolve_gate(phase=1, branch="feature/foo", baseline_count=None, threshold=0)
            == "baseline"
        )


class TestResolveGatePhase2:
    """测试 phase=2 全 strict"""

    def test_phase2_master(self) -> None:
        """phase=2, master 分支 → strict"""
        assert resolve_gate(phase=2, branch="master") == "strict"

    def test_phase2_develop(self) -> None:
        """phase=2, develop 分支 → strict"""
        assert resolve_gate(phase=2, branch="develop") == "strict"

    def test_phase2_feature_branch(self) -> None:
        """phase=2, feature 分支 → strict"""
        assert resolve_gate(phase=2, branch="feature/foo") == "strict"

    def test_phase2_no_branch(self) -> None:
        """phase=2, 无分支 → strict"""
        assert resolve_gate(phase=2) == "strict"


class TestResolveGatePhase3:
    """
    测试 phase=3 baseline 已归档（strict-only 语义）

    Phase 3 核心语义：
    - baseline 文件已归档到 scripts/ci/archived/mypy_baseline.txt.archived
    - 所有分支、所有场景都返回 strict
    - 不再支持 baseline 对比模式

    防回退要求：
    - 修改 Phase 3 行为前，必须更新此测试类
    - 确保所有分支测试都断言返回 strict
    - 保留 override 回滚机制（紧急场景使用）

    相关文档：
    - docs/architecture/adr_mypy_baseline_and_gating.md §5.6 阶段 3
    """

    def test_phase3_master(self) -> None:
        """phase=3, master 分支 → strict"""
        assert resolve_gate(phase=3, branch="master") == "strict"

    def test_phase3_develop(self) -> None:
        """phase=3, develop 分支 → strict"""
        assert resolve_gate(phase=3, branch="develop") == "strict"

    def test_phase3_feature_branch(self) -> None:
        """phase=3, feature 分支 → strict"""
        assert resolve_gate(phase=3, branch="feature/foo") == "strict"

    def test_phase3_no_branch(self) -> None:
        """phase=3, 无分支 → strict"""
        assert resolve_gate(phase=3) == "strict"

    def test_phase3_strict_only_regression_guard(self) -> None:
        """
        Phase 3 防回退：验证 strict-only 语义的完整性。

        此测试作为回归防护，确保 Phase 3 的 strict-only 语义
        不会被未来的代码修改意外破坏。
        """
        # 所有可能的分支名都应返回 strict
        test_branches = [
            "main",
            "master",
            "develop",
            "feature/foo",
            "fix/bar",
            "release/v1.0",
            "hotfix/urgent",
            "",  # 空分支名
        ]

        for branch in test_branches:
            result = resolve_gate(phase=3, branch=branch)
            assert result == "strict", (
                f"Phase 3 防回退失败: branch='{branch}' 期望 strict，"
                f"实际 {result}。Phase 3 语义要求所有分支都为 strict"
            )

    def test_phase3_ignores_baseline_parameters(self) -> None:
        """
        Phase 3 防回退：baseline_count/threshold 参数应被忽略。

        Phase 3 中 baseline 已归档，这些参数不应影响结果。
        """
        # 即使 baseline_count > threshold，也应返回 strict
        result = resolve_gate(
            phase=3,
            branch="feature/x",
            baseline_count=999,
            threshold=0,
        )
        assert result == "strict", "Phase 3 防回退失败: baseline 参数应被忽略，但结果受到了影响"

    def test_phase3_override_emergency_rollback(self) -> None:
        """
        Phase 3 防回退：override 紧急回滚机制仍可用。

        即使在 Phase 3，override 应仍然生效，用于紧急回滚场景。
        """
        # baseline override（常规回滚）
        assert resolve_gate(phase=3, override="baseline") == "baseline"

        # warn override（仅警告不阻断）
        assert resolve_gate(phase=3, override="warn") == "warn"

        # off override（跳过检查）
        assert resolve_gate(phase=3, override="off") == "off"


class TestResolveGateUnknownPhase:
    """测试未知 phase 值"""

    def test_phase_negative(self) -> None:
        """phase=-1 → baseline（默认）"""
        assert resolve_gate(phase=-1) == "baseline"

    def test_phase_4(self) -> None:
        """phase=4 → baseline（默认）"""
        assert resolve_gate(phase=4) == "baseline"

    def test_phase_100(self) -> None:
        """phase=100 → baseline（默认）"""
        assert resolve_gate(phase=100) == "baseline"


class TestResolveGateEdgeCases:
    """测试边界情况"""

    def test_empty_branch(self) -> None:
        """空分支名"""
        # phase=1, 空分支名 → baseline（非默认分支）
        assert resolve_gate(phase=1, branch="") == "baseline"

    def test_branch_priority_over_ref(self) -> None:
        """branch 参数优先于 ref"""
        # branch=develop 应覆盖 ref=refs/heads/master
        assert resolve_gate(phase=1, branch="develop", ref="refs/heads/master") == "baseline"

    def test_verbose_mode(self) -> None:
        """verbose 模式不影响结果"""
        assert resolve_gate(phase=1, branch="master", verbose=True) == "strict"
        assert resolve_gate(phase=1, branch="develop", verbose=True) == "baseline"


class TestResolveGatePRScenarios:
    """测试真实 PR 场景"""

    def test_pr_to_master_phase1(self) -> None:
        """PR 到 master 分支（phase=1）→ baseline（PR 源分支非默认）"""
        # 在 PR 中，branch 应为 PR 源分支（如 feature/xxx）
        assert resolve_gate(phase=1, branch="feature/add-auth") == "baseline"

    def test_pr_to_master_phase1_with_threshold_met(self) -> None:
        """PR 到 master 分支（phase=1），baseline 错误数满足阈值 → strict"""
        assert (
            resolve_gate(phase=1, branch="feature/add-auth", baseline_count=0, threshold=0)
            == "strict"
        )

    def test_push_to_master_phase1(self) -> None:
        """直接推送到 master（phase=1）→ strict"""
        assert resolve_gate(phase=1, branch="master") == "strict"

    def test_pr_to_develop_phase1(self) -> None:
        """PR 到 develop 分支（phase=1）→ baseline"""
        assert resolve_gate(phase=1, branch="feature/fix") == "baseline"
