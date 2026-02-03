#!/usr/bin/env python3
"""更新 iteration 相关 fixtures。

用法:
    python scripts/iteration/update_iteration_fixtures.py --all
    python scripts/iteration/update_iteration_fixtures.py --min-gate
    python scripts/iteration/update_iteration_fixtures.py --sync-regression
    python scripts/iteration/update_iteration_fixtures.py --iteration-cycle
    python scripts/iteration/update_iteration_fixtures.py --smoke
    python scripts/iteration/update_iteration_fixtures.py --evidence-snippet
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List

# 添加脚本目录到 path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import sync_iteration_regression as sync_module  # noqa: E402
from generated_blocks import (  # noqa: E402
    find_evidence_block,
    find_evidence_insert_position,
    generate_evidence_block_with_markers,
    render_evidence_snippet as render_evidence_snippet_v2,
)
from render_iteration_evidence_snippet import (  # noqa: E402
    EvidenceParseError,
    parse_evidence_data,
    render_evidence_snippet,
    render_iteration_evidence_snippet,
)
from render_min_gate_block import render_min_gate_block  # noqa: E402
from update_render_min_gate_block_fixtures import (  # noqa: E402
    _validate_output,
    update_fixtures as update_render_min_gate_fixtures,
)

REPO_ROOT = SCRIPT_DIR.parent.parent
FIXTURES_ROOT = REPO_ROOT / "tests" / "iteration" / "fixtures"

# 默认迭代编号（固定值，保证输出稳定）
DEFAULT_MIN_GATE_ITERATION = 13
DEFAULT_SYNC_ITERATION = 13
DEFAULT_CYCLE_ITERATION = 20
DEFAULT_SMOKE_ITERATION = 21
DEFAULT_EVIDENCE_SNAPSHOT_ITERATION = 13

SYNC_PROFILE = "regression"
CYCLE_PROFILE = "regression"
SMOKE_PROFILE = "docs-only"

FIXED_RECORDED_AT = "2026-02-02T12:00:00Z"
SMOKE_RECORDED_AT = "2026-02-02T12:30:00Z"

SYNC_REGRESSION_BASE = """# Iteration 13 Regression

## 执行信息

| 项目 | 值 |
|------|-----|
| 日期 | 2026-02-02 |
## 执行结果总览

| 序号 | 测试 |
|------|------|
| 1 | 示例 |
## 验收证据

旧内容

## 相关文档

- Link 1
"""

CYCLE_REGRESSION_BASE = """# Iteration 20 Regression

## 执行信息

| 项目 | 值 |
|------|-----|
| 日期 | 2026-02-02 |
## 执行结果总览

| 序号 | 测试 |
|------|------|
| 1 | smoke |
## 验收证据

占位内容

## 相关文档

- Link 1
- Link 2
"""

CYCLE_EVIDENCE = {
    "iteration_number": DEFAULT_CYCLE_ITERATION,
    "recorded_at": FIXED_RECORDED_AT,
    "commit_sha": "deadbeef1234567890abcdef1234567890abcdef",
    "overall_result": "PASS",
    "commands": [
        {
            "name": "lint",
            "command": "make lint",
            "result": "PASS",
            "summary": "ruff check passed",
            "duration_seconds": 4.2,
        },
        {
            "name": "typecheck",
            "command": "make typecheck",
            "result": "PASS",
            "summary": "mypy passed",
            "duration_seconds": 9.8,
        },
    ],
    "links": {"ci_run_url": "https://github.com/org/repo/actions/runs/999"},
    "notes": "迭代验证通过。",
}

SMOKE_EVIDENCE = {
    "iteration_number": DEFAULT_SMOKE_ITERATION,
    "recorded_at": SMOKE_RECORDED_AT,
    "commit_sha": "feedface1234567890abcdef1234567890abcdef",
    "commands": [
        {
            "name": "ci",
            "command": "make ci",
            "result": "PASS",
            "summary": "All checks passed",
            "duration_seconds": 12.3,
        }
    ],
    "overall_result": "PASS",
    "notes": "冒烟验证通过",
}


def _ensure_trailing_newline(content: str) -> str:
    if content.endswith("\n"):
        return content
    return content + "\n"


def _write_fixture(path: Path, content: str) -> None:
    content = _ensure_trailing_newline(content)
    _validate_output(content, context=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _resolve_fixtures_root(fixtures_root: Path | None) -> Path:
    return fixtures_root if fixtures_root is not None else FIXTURES_ROOT


def _sync_evidence_block_with_data(content: str, evidence: dict) -> str:
    new_block = generate_evidence_block_with_markers(evidence)
    block = find_evidence_block(content)

    if block:
        return content[: block.begin_pos] + new_block + content[block.end_pos :]

    evidence_section_pattern = re.compile(r"^##\s+验收证据\s*$", re.MULTILINE)
    match = evidence_section_pattern.search(content)
    if match:
        next_section = re.search(r"^##\s+", content[match.end() :], re.MULTILINE)
        if next_section:
            end_pos = match.end() + next_section.start()
        else:
            end_pos = len(content)
        return content[: match.start()] + new_block + "\n\n" + content[end_pos:].lstrip()

    insert_pos = find_evidence_insert_position(content)
    prefix = "\n\n" if insert_pos > 0 and content[insert_pos - 1] != "\n" else "\n"
    suffix = "\n"
    return content[:insert_pos] + prefix + new_block + suffix + content[insert_pos:]


def _build_cycle_execute_report(iteration_number: int, profile: str, evidence: dict) -> str:
    return "\n".join(
        [
            f"# Iteration {iteration_number} 迭代循环执行记录",
            "",
            f"- iteration_number: {iteration_number}",
            f"- profile: {profile}",
            f"- recorded_at: {evidence['recorded_at']}",
            f"- commit_sha: {evidence['commit_sha']}",
            "",
            "## 步骤",
            "",
            f"1. 渲染最小门禁命令块（profile={profile}）",
            f"2. 渲染验收证据片段（commands={len(evidence.get('commands', []))})",
            "3. 同步回归文档（min_gate_block + evidence_snippet）",
            "",
            "## 结果",
            "",
            "- 状态: PASS",
            "- 输出文件: verify_regression_expected.md",
        ]
    )


def _build_cycle_summary(iteration_number: int, profile: str, evidence: dict) -> str:
    commit_short = evidence["commit_sha"][:7]
    commands_count = len(evidence.get("commands", []))
    ci_run_url = evidence.get("links", {}).get("ci_run_url", "-")

    return "\n".join(
        [
            f"# Iteration {iteration_number} 迭代循环摘要",
            "",
            "| 项目 | 值 |",
            "|------|-----|",
            f"| Iteration | {iteration_number} |",
            f"| Profile | {profile} |",
            f"| Commit | `{commit_short}` |",
            f"| Recorded At | {evidence['recorded_at']} |",
            f"| Commands | {commands_count} |",
            f"| Overall Result | ✅ {evidence['overall_result']} |",
            f"| CI Run | {ci_run_url} |",
        ]
    )


def _build_cycle_verify_report(
    iteration_number: int, profile: str, evidence: dict, content: str
) -> str:
    checks = [
        ("min_gate_block marker", "<!-- BEGIN GENERATED: min_gate_block" in content),
        ("evidence_snippet marker", "<!-- BEGIN GENERATED: evidence_snippet -->" in content),
        ("Iteration 标识", f"Iteration {iteration_number}" in content),
    ]
    ci_url = evidence.get("links", {}).get("ci_run_url")
    if ci_url:
        checks.append(("CI 运行链接", ci_url in content))
    checks.append(("命令数量", True))

    lines = [f"# Iteration {iteration_number} 回归文档校验报告", ""]
    for name, ok in checks:
        icon = "✅" if ok else "❌"
        if name == "min_gate_block marker":
            lines.append(f"- {icon} {name}: found (profile={profile})")
        elif name == "CI 运行链接" and ci_url:
            lines.append(f"- {icon} {name}: {ci_url}")
        elif name == "Iteration 标识":
            lines.append(f"- {icon} {name}: Iteration {iteration_number}")
        elif name == "命令数量":
            lines.append(f"- {icon} {name}: {len(evidence.get('commands', []))}")
        else:
            lines.append(f"- {icon} {name}: found")

    lines.extend(["", "结论: PASS"])
    return "\n".join(lines)


def update_sync_regression_fixture(fixtures_root: Path | None = None) -> Path:
    content, _, _ = sync_module.sync_min_gate_block(
        SYNC_REGRESSION_BASE, DEFAULT_SYNC_ITERATION, SYNC_PROFILE
    )
    content, _, _ = sync_module.sync_evidence_block(content, DEFAULT_SYNC_ITERATION)

    root = _resolve_fixtures_root(fixtures_root)
    path = root / "sync_iteration_regression" / "expected_regression.md"
    _write_fixture(path, content)
    return path


def update_iteration_cycle_fixtures(fixtures_root: Path | None = None) -> List[Path]:
    updated, _, _ = sync_module.sync_min_gate_block(
        CYCLE_REGRESSION_BASE, DEFAULT_CYCLE_ITERATION, CYCLE_PROFILE
    )
    updated = _sync_evidence_block_with_data(updated, CYCLE_EVIDENCE)

    outputs = {
        "execute.md": _build_cycle_execute_report(
            DEFAULT_CYCLE_ITERATION, CYCLE_PROFILE, CYCLE_EVIDENCE
        ),
        "summary.md": _build_cycle_summary(DEFAULT_CYCLE_ITERATION, CYCLE_PROFILE, CYCLE_EVIDENCE),
        "verify_report.md": _build_cycle_verify_report(
            DEFAULT_CYCLE_ITERATION, CYCLE_PROFILE, CYCLE_EVIDENCE, updated
        ),
        "verify_regression_expected.md": updated,
    }

    root = _resolve_fixtures_root(fixtures_root)
    written: List[Path] = []
    for filename, content in outputs.items():
        path = root / "iteration_cycle" / filename
        _write_fixture(path, content)
        written.append(path)

    return written


def update_iteration_cycle_smoke_fixtures(fixtures_root: Path | None = None) -> List[Path]:
    outputs = {}
    min_gate = render_min_gate_block(DEFAULT_SMOKE_ITERATION, SMOKE_PROFILE)
    outputs["min_gate_block_docs_only.md"] = min_gate

    evidence = parse_evidence_data(SMOKE_EVIDENCE)
    evidence_filename = f"iteration_{DEFAULT_SMOKE_ITERATION}_evidence.json"
    outputs["evidence_snippet.md"] = render_evidence_snippet(evidence, evidence_filename)

    root = _resolve_fixtures_root(fixtures_root)
    written: List[Path] = []
    for filename, content in outputs.items():
        path = root / "iteration_cycle_smoke" / filename
        _write_fixture(path, content)
        written.append(path)

    return written


def update_evidence_snapshot_fixture(
    fixtures_root: Path | None = None,
    evidence_dir: Path | None = None,
) -> Path:
    evidence_dir = evidence_dir or (REPO_ROOT / "docs" / "acceptance" / "evidence")
    output = render_iteration_evidence_snippet(DEFAULT_EVIDENCE_SNAPSHOT_ITERATION, evidence_dir)
    path = (
        _resolve_fixtures_root(fixtures_root)
        / "render_iteration_evidence_snippet"
        / f"iteration_{DEFAULT_EVIDENCE_SNAPSHOT_ITERATION}.md"
    )
    _write_fixture(path, output)
    return path


def update_evidence_snippet_v2_snapshot_fixture(fixtures_root: Path | None = None) -> Path:
    root = _resolve_fixtures_root(fixtures_root)
    evidence_path = root / "iteration_evidence_v2_minimal.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    output = render_evidence_snippet_v2(evidence)
    path = root / "evidence_snippet_v2_snapshot.md"
    _write_fixture(path, output)
    return path


def update_min_gate_fixtures(fixtures_root: Path | None = None) -> List[Path]:
    output_dir = _resolve_fixtures_root(fixtures_root) / "render_min_gate_block"
    return update_render_min_gate_fixtures(
        iteration_number=DEFAULT_MIN_GATE_ITERATION,
        output_dir=output_dir,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="更新 iteration fixtures")
    parser.add_argument("--all", action="store_true", help="更新全部 fixtures")
    parser.add_argument("--min-gate", action="store_true", help="更新最小门禁 fixtures")
    parser.add_argument("--sync-regression", action="store_true", help="更新回归同步 fixtures")
    parser.add_argument("--iteration-cycle", action="store_true", help="更新迭代循环 fixtures")
    parser.add_argument("--smoke", action="store_true", help="更新 smoke fixtures")
    parser.add_argument(
        "--evidence-snippet",
        action="store_true",
        help="更新验收证据片段 snapshot fixtures",
    )
    parser.add_argument(
        "--fixtures-root",
        type=Path,
        default=FIXTURES_ROOT,
        help=f"输出目录（默认: {FIXTURES_ROOT}）",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="证据文件目录（默认: docs/acceptance/evidence）",
    )

    args = parser.parse_args()

    if not (
        args.all
        or args.min_gate
        or args.sync_regression
        or args.iteration_cycle
        or args.smoke
        or args.evidence_snippet
    ):
        args.all = True

    written: List[Path] = []
    try:
        if args.all or args.min_gate:
            written.extend(update_min_gate_fixtures(args.fixtures_root))
        if args.all or args.sync_regression:
            written.append(update_sync_regression_fixture(args.fixtures_root))
        if args.all or args.iteration_cycle:
            written.extend(update_iteration_cycle_fixtures(args.fixtures_root))
        if args.all or args.smoke:
            written.extend(update_iteration_cycle_smoke_fixtures(args.fixtures_root))
        if args.all or args.evidence_snippet:
            written.append(
                update_evidence_snapshot_fixture(
                    args.fixtures_root,
                    args.evidence_dir,
                )
            )
            written.append(update_evidence_snippet_v2_snapshot_fixture(args.fixtures_root))
    except EvidenceParseError as exc:
        print(f"❌ 错误: 证据文件解析失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 输出错误信息
        print(f"❌ 错误: {exc}", file=sys.stderr)
        return 1

    for path in written:
        try:
            rel_path = path.relative_to(REPO_ROOT)
        except ValueError:
            rel_path = path
        print(f"[OK] 写入 {rel_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
