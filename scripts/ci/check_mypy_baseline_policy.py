#!/usr/bin/env python3
"""
mypy baseline 变更策略检查脚本

功能:
1. 读取 git diff 对比 base/head 的 baseline 文件变更
2. 计算新增/减少/净增错误数
3. 根据净增数量执行不同的检查策略：
   - 净增 > 0: 要求 PR body 含 "### CI Baseline 变更检查" 小节与 issue 编号
   - 净增 > 5: 要求 PR labels 含 `tech-debt` 或 `type-coverage`
   - 净增 > 10: 额外严格警告，需要明确审批

环境变量:
- GITHUB_EVENT_PATH: GitHub 事件 JSON 文件路径（PR 环境）
- GITHUB_BASE_REF:   PR 的 base 分支（如 main/master）
- GITHUB_HEAD_REF:   PR 的 head 分支
- GITHUB_PR_BODY:    PR body 内容（用于检查说明）
- GITHUB_PR_LABELS:  PR labels（逗号分隔）

用法:
    # 在 GitHub Actions PR 环境中运行
    python scripts/ci/check_mypy_baseline_policy.py

    # 手动指定 base/head SHA
    python scripts/ci/check_mypy_baseline_policy.py --base-sha origin/main --head-sha HEAD

    # 使用本地 diff 文件（用于测试）
    python scripts/ci/check_mypy_baseline_policy.py --diff-file tests/fixtures/mypy_baseline_policy/sample_diff.txt

退出码:
    0 - 检查通过
    1 - 检查失败（需要额外说明或 labels）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ============================================================================
# 配置常量
# ============================================================================

DEFAULT_BASELINE_FILE = "scripts/ci/mypy_baseline.txt"

# PR body 中必须包含的 section 标记
REQUIRED_PR_SECTION = "### CI Baseline 变更检查"
REQUIRED_ISSUE_PATTERN = re.compile(r"#\d+|https://github\.com/.+/issues/\d+")

# 净增阈值
THRESHOLD_REQUIRE_EXPLANATION = 0  # 净增 > 0 需要说明
THRESHOLD_REQUIRE_LABELS = 5       # 净增 > 5 需要 labels
THRESHOLD_STRICT_REVIEW = 10       # 净增 > 10 严格审核

# 允许的 labels（净增 > 5 时需要其中之一）
ALLOWED_LABELS = {"tech-debt", "type-coverage"}


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class BaselineDiff:
    """Baseline diff 统计数据."""

    added_lines: int      # 新增行数（新增错误）
    removed_lines: int    # 删除行数（修复错误）
    net_change: int       # 净增（added - removed）
    added_errors: list[str]    # 新增的错误列表
    removed_errors: list[str]  # 删除的错误列表


@dataclass
class PRContext:
    """PR 上下文信息."""

    body: str               # PR body 内容
    labels: set[str]        # PR labels
    base_ref: str           # base 分支
    head_ref: str           # head 分支


@dataclass
class PolicyResult:
    """策略检查结果."""

    passed: bool            # 是否通过
    messages: list[str]     # 消息列表
    warnings: list[str]     # 警告列表


# ============================================================================
# Git diff 解析
# ============================================================================


def run_git_diff(base_sha: str, head_sha: str, file_path: str) -> str:
    """
    运行 git diff 获取指定文件的差异。

    Args:
        base_sha: base commit SHA 或分支引用
        head_sha: head commit SHA 或分支引用
        file_path: 要比较的文件路径

    Returns:
        git diff 输出字符串

    Raises:
        subprocess.CalledProcessError: git 命令失败时
    """
    cmd = ["git", "diff", f"{base_sha}...{head_sha}", "--", file_path]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def parse_diff(diff_text: str) -> BaselineDiff:
    """
    解析 git diff 输出，计算新增和删除行数。

    Args:
        diff_text: git diff 输出

    Returns:
        BaselineDiff 统计对象
    """
    added_errors: list[str] = []
    removed_errors: list[str] = []

    for line in diff_text.splitlines():
        # 跳过 diff header 行
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("diff "):
            continue
        if line.startswith("index "):
            continue

        # 统计新增和删除
        if line.startswith("+") and len(line) > 1:
            # 新增行（不包括空行）
            content = line[1:].strip()
            if content:
                added_errors.append(content)
        elif line.startswith("-") and len(line) > 1:
            # 删除行（不包括空行）
            content = line[1:].strip()
            if content:
                removed_errors.append(content)

    return BaselineDiff(
        added_lines=len(added_errors),
        removed_lines=len(removed_errors),
        net_change=len(added_errors) - len(removed_errors),
        added_errors=added_errors,
        removed_errors=removed_errors,
    )


# ============================================================================
# GitHub 事件解析
# ============================================================================


def load_github_event(event_path: str) -> dict:
    """
    加载 GitHub 事件 JSON 文件。

    Args:
        event_path: 事件 JSON 文件路径

    Returns:
        事件数据字典
    """
    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_pr_context_from_event(event: dict) -> PRContext:
    """
    从 GitHub 事件中提取 PR 上下文。

    Args:
        event: GitHub 事件数据

    Returns:
        PRContext 对象
    """
    pr = event.get("pull_request", {})

    body = pr.get("body", "") or ""
    labels_data = pr.get("labels", [])
    labels = {label.get("name", "") for label in labels_data if label.get("name")}

    base_ref = pr.get("base", {}).get("ref", "")
    head_ref = pr.get("head", {}).get("ref", "")

    return PRContext(
        body=body,
        labels=labels,
        base_ref=base_ref,
        head_ref=head_ref,
    )


def extract_pr_context_from_env() -> PRContext:
    """
    从环境变量中提取 PR 上下文。

    用于测试或非标准环境。

    Returns:
        PRContext 对象
    """
    body = os.environ.get("GITHUB_PR_BODY", "")
    labels_str = os.environ.get("GITHUB_PR_LABELS", "")
    labels = {l.strip() for l in labels_str.split(",") if l.strip()}

    base_ref = os.environ.get("GITHUB_BASE_REF", "")
    head_ref = os.environ.get("GITHUB_HEAD_REF", "")

    return PRContext(
        body=body,
        labels=labels,
        base_ref=base_ref,
        head_ref=head_ref,
    )


def get_pr_context() -> Optional[PRContext]:
    """
    获取 PR 上下文（优先使用 GITHUB_EVENT_PATH）。

    Returns:
        PRContext 对象，或 None（非 PR 环境）
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    if event_path and os.path.exists(event_path):
        try:
            event = load_github_event(event_path)
            if "pull_request" in event:
                return extract_pr_context_from_event(event)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] 无法解析 GitHub 事件文件: {e}", file=sys.stderr)

    # Fallback: 从环境变量提取
    if os.environ.get("GITHUB_BASE_REF"):
        return extract_pr_context_from_env()

    return None


# ============================================================================
# 策略检查
# ============================================================================


def check_pr_section(body: str) -> bool:
    """检查 PR body 是否包含 baseline 变更说明 section."""
    return REQUIRED_PR_SECTION in body


def check_issue_reference(body: str) -> bool:
    """检查 PR body 是否包含 issue 引用."""
    return REQUIRED_ISSUE_PATTERN.search(body) is not None


def check_labels(labels: set[str]) -> bool:
    """检查 PR 是否包含允许的 labels."""
    return bool(labels & ALLOWED_LABELS)


def check_policy(diff: BaselineDiff, pr_context: Optional[PRContext]) -> PolicyResult:
    """
    执行策略检查。

    Args:
        diff: baseline diff 统计
        pr_context: PR 上下文（如果有）

    Returns:
        PolicyResult 对象
    """
    messages: list[str] = []
    warnings: list[str] = []
    passed = True

    # 统计信息
    messages.append(f"Baseline 变更统计:")
    messages.append(f"  - 新增错误: {diff.added_lines}")
    messages.append(f"  - 修复错误: {diff.removed_lines}")
    messages.append(f"  - 净增: {diff.net_change:+d}")

    # 净减少或不变：直接通过
    if diff.net_change <= 0:
        messages.append("")
        if diff.net_change < 0:
            messages.append(f"[OK] 净减少 {abs(diff.net_change)} 个错误，感谢修复！")
        else:
            messages.append("[OK] Baseline 无净增，检查通过")
        return PolicyResult(passed=True, messages=messages, warnings=warnings)

    # 净增 > 0：需要检查
    messages.append("")
    messages.append(f"[WARN] Baseline 净增 {diff.net_change} 个错误")

    if pr_context is None:
        # 非 PR 环境：仅警告
        warnings.append("无法获取 PR 上下文，跳过策略检查")
        messages.append("[INFO] 非 PR 环境，跳过策略检查")
        return PolicyResult(passed=True, messages=messages, warnings=warnings)

    # 检查 PR body 是否包含说明 section
    if not check_pr_section(pr_context.body):
        passed = False
        messages.append("")
        messages.append(f"[FAIL] PR body 缺少 '{REQUIRED_PR_SECTION}' section")
        messages.append("       请在 PR 描述中添加 baseline 变更说明")

    # 检查是否有 issue 引用
    if diff.net_change > THRESHOLD_REQUIRE_EXPLANATION and not check_issue_reference(pr_context.body):
        passed = False
        messages.append("")
        messages.append("[FAIL] Baseline 净增时需要关联 Issue")
        messages.append("       请在 PR body 的 '关联 Issue' 字段填写相关 issue 编号")
        messages.append("       格式: #123 或完整 GitHub issue URL")

    # 净增 > 5：需要 labels
    if diff.net_change > THRESHOLD_REQUIRE_LABELS:
        if not check_labels(pr_context.labels):
            passed = False
            messages.append("")
            messages.append(f"[FAIL] 净增 > {THRESHOLD_REQUIRE_LABELS} 需要添加标签")
            messages.append(f"       请添加以下标签之一: {', '.join(sorted(ALLOWED_LABELS))}")

    # 净增 > 10：严格警告
    if diff.net_change > THRESHOLD_STRICT_REVIEW:
        warnings.append(
            f"[严重警告] 净增 > {THRESHOLD_STRICT_REVIEW}，需要额外审批"
        )
        messages.append("")
        messages.append(f"[WARN] 净增 > {THRESHOLD_STRICT_REVIEW}，建议：")
        messages.append("       1. 拆分 PR，减少单次 baseline 增量")
        messages.append("       2. 确保有明确的后续修复计划")
        messages.append("       3. 获取 Tech Lead 审批")

    return PolicyResult(passed=passed, messages=messages, warnings=warnings)


# ============================================================================
# 主函数
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mypy baseline 变更策略检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base-sha",
        type=str,
        default=None,
        help="Base commit SHA 或分支引用（默认从 GITHUB_BASE_REF 获取）",
    )
    parser.add_argument(
        "--head-sha",
        type=str,
        default=None,
        help="Head commit SHA 或分支引用（默认: HEAD）",
    )
    parser.add_argument(
        "--baseline-file",
        type=str,
        default=DEFAULT_BASELINE_FILE,
        help=f"Baseline 文件路径（默认: {DEFAULT_BASELINE_FILE}）",
    )
    parser.add_argument(
        "--diff-file",
        type=str,
        default=None,
        help="使用预生成的 diff 文件（用于测试）",
    )
    parser.add_argument(
        "--pr-body",
        type=str,
        default=None,
        help="PR body 内容（用于测试）",
    )
    parser.add_argument(
        "--pr-labels",
        type=str,
        default=None,
        help="PR labels（逗号分隔，用于测试）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("mypy Baseline 变更策略检查")
    print("=" * 70)
    print()

    # 获取 diff
    diff_text = ""

    if args.diff_file:
        # 使用预生成的 diff 文件
        print(f"使用 diff 文件: {args.diff_file}")
        diff_path = Path(args.diff_file)
        if not diff_path.exists():
            print(f"[FAIL] diff 文件不存在: {args.diff_file}", file=sys.stderr)
            return 1
        diff_text = diff_path.read_text(encoding="utf-8")
    else:
        # 从 git diff 获取
        base_sha = args.base_sha or os.environ.get("GITHUB_BASE_REF")
        head_sha = args.head_sha or "HEAD"

        if not base_sha:
            print("[INFO] 未指定 base SHA，且无 GITHUB_BASE_REF 环境变量")
            print("[INFO] 尝试使用 origin/main 作为 base")
            base_sha = "origin/main"

        print(f"Base: {base_sha}")
        print(f"Head: {head_sha}")
        print(f"文件: {args.baseline_file}")
        print()

        try:
            diff_text = run_git_diff(base_sha, head_sha, args.baseline_file)
        except subprocess.CalledProcessError as e:
            print(f"[WARN] git diff 失败: {e}", file=sys.stderr)
            print("[INFO] Baseline 文件可能未修改或不存在")
            diff_text = ""

    # 解析 diff
    diff = parse_diff(diff_text)

    if args.verbose and diff_text:
        print("--- diff 内容 ---")
        print(diff_text)
        print("--- diff 结束 ---")
        print()

    # 获取 PR 上下文
    if args.pr_body is not None or args.pr_labels is not None:
        # 使用命令行参数（测试模式）
        pr_body = args.pr_body or ""
        pr_labels = set()
        if args.pr_labels:
            pr_labels = {l.strip() for l in args.pr_labels.split(",") if l.strip()}
        pr_context = PRContext(
            body=pr_body,
            labels=pr_labels,
            base_ref=args.base_sha or "",
            head_ref=args.head_sha or "",
        )
    else:
        pr_context = get_pr_context()

    # 执行策略检查
    result = check_policy(diff, pr_context)

    # 输出结果
    for msg in result.messages:
        print(msg)

    if result.warnings:
        print()
        for warn in result.warnings:
            print(f"[WARN] {warn}")

    print()
    print("=" * 70)

    if result.passed:
        print("[OK] Baseline 策略检查通过")
        return 0
    else:
        print("[FAIL] Baseline 策略检查失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
