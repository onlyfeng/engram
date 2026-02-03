#!/usr/bin/env python3
"""
检查 iteration fixtures 是否与最新渲染结果一致。

功能：
1. 使用固定输入运行迭代渲染/编排函数
2. 将生成结果与 tests/iteration/fixtures/ 对比
3. 输出文本或 JSON 报告，并提供修复命令

用法：
    python scripts/ci/check_iteration_fixtures_freshness.py
    python scripts/ci/check_iteration_fixtures_freshness.py --json

退出码：
    0 - 检查通过
    1 - fixtures 存在不一致或缺失
    2 - 执行出错（生成失败或文件读取错误）
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

ITERATION_DIR = Path(__file__).resolve().parent.parent / "iteration"
sys.path.insert(0, str(ITERATION_DIR))

import update_iteration_fixtures as update_module  # noqa: E402

FIX_COMMAND = "python scripts/iteration/update_iteration_fixtures.py --all"

STATIC_FIXTURE_PATHS = {
    Path("README.md"),
    Path("iteration_evidence_v2_minimal.json"),
    Path("acceptance_run_artifacts/steps.log"),
}


@dataclass
class FixtureMismatch:
    path: Path
    kind: str
    message: str


@dataclass
class CheckResult:
    mismatches: List[FixtureMismatch]
    generated_files: List[Path]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def get_project_root() -> Path:
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def _copy_static_inputs(fixtures_root: Path, temp_root: Path) -> None:
    source = fixtures_root / "iteration_evidence_v2_minimal.json"
    if not source.exists():
        raise FileNotFoundError(f"缺少 fixture 输入文件: {source}")
    target = temp_root / source.name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _generate_fixtures(temp_root: Path) -> List[Path]:
    written: List[Path] = []
    written.extend(update_module.update_min_gate_fixtures(temp_root))
    written.append(update_module.update_sync_regression_fixture(temp_root))
    written.extend(update_module.update_iteration_cycle_fixtures(temp_root))
    written.extend(update_module.update_iteration_cycle_smoke_fixtures(temp_root))
    written.append(update_module.update_evidence_snapshot_fixture(temp_root))
    written.append(update_module.update_evidence_snippet_v2_snapshot_fixture(temp_root))
    return written


def _collect_fixture_files(fixtures_root: Path) -> List[Path]:
    return [path for path in fixtures_root.rglob("*") if path.is_file()]


def _compare_fixtures(
    fixtures_root: Path,
    temp_root: Path,
    generated_files: Iterable[Path],
) -> List[FixtureMismatch]:
    mismatches: List[FixtureMismatch] = []

    generated_rel_paths = {path.relative_to(temp_root) for path in generated_files}
    fixture_files = _collect_fixture_files(fixtures_root)
    fixture_rel_paths = {path.relative_to(fixtures_root) for path in fixture_files}

    for rel_path in sorted(generated_rel_paths):
        expected_path = fixtures_root / rel_path
        actual_path = temp_root / rel_path
        if not expected_path.exists():
            mismatches.append(
                FixtureMismatch(
                    path=expected_path,
                    kind="missing_fixture",
                    message="fixture 文件缺失",
                )
            )
            continue
        try:
            expected = expected_path.read_text(encoding="utf-8")
            actual = actual_path.read_text(encoding="utf-8")
        except OSError as exc:
            mismatches.append(
                FixtureMismatch(
                    path=expected_path,
                    kind="read_error",
                    message=f"读取失败: {exc}",
                )
            )
            continue
        if expected != actual:
            mismatches.append(
                FixtureMismatch(
                    path=expected_path,
                    kind="content_mismatch",
                    message="内容与渲染结果不一致",
                )
            )

    extra_paths = fixture_rel_paths - generated_rel_paths - STATIC_FIXTURE_PATHS
    for rel_path in sorted(extra_paths):
        mismatches.append(
            FixtureMismatch(
                path=fixtures_root / rel_path,
                kind="extra_fixture",
                message="fixture 未由渲染脚本生成",
            )
        )

    return mismatches


def run_check(project_root: Path, fixtures_root: Path, verbose: bool = False) -> CheckResult:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        _copy_static_inputs(fixtures_root, temp_root)
        generated_files = _generate_fixtures(temp_root)
        mismatches = _compare_fixtures(fixtures_root, temp_root, generated_files)

    if verbose:
        print(f"[INFO] 生成 fixtures 数量: {len(generated_files)}")
        print(f"[INFO] 基准 fixtures 数量: {len(_collect_fixture_files(fixtures_root))}")
        print()

    return CheckResult(mismatches=mismatches, generated_files=generated_files)


def format_text_report(result: CheckResult, project_root: Path) -> str:
    lines = [
        "=" * 70,
        "迭代 fixtures 新鲜度检查报告",
        "=" * 70,
        "",
        f"生成结果数: {len(result.generated_files)}",
        f"不一致条目数: {len(result.mismatches)}",
    ]

    if not result.mismatches:
        lines.extend(["", "[OK] fixtures 均为最新"])
        return "\n".join(lines)

    by_file: dict[Path, List[FixtureMismatch]] = {}
    for mismatch in result.mismatches:
        by_file.setdefault(mismatch.path, []).append(mismatch)

    for file_path, items in sorted(by_file.items()):
        rel_path = file_path
        try:
            rel_path = file_path.relative_to(project_root)
        except ValueError:
            pass
        lines.append(f"\n【{rel_path}】({len(items)} 条)")
        for item in items:
            lines.append(f"  - {item.kind}: {item.message}")

    lines.extend(
        [
            "",
            "修复建议:",
            f"  {FIX_COMMAND}",
        ]
    )

    return "\n".join(lines)


def format_json_report(result: CheckResult, project_root: Path) -> str:
    mismatches = []
    for mismatch in result.mismatches:
        try:
            rel_path = mismatch.path.relative_to(project_root)
        except ValueError:
            rel_path = mismatch.path
        mismatches.append(
            {
                "path": str(rel_path),
                "kind": mismatch.kind,
                "message": mismatch.message,
            }
        )

    payload = {
        "ok": result.ok,
        "generated_count": len(result.generated_files),
        "mismatch_count": len(result.mismatches),
        "mismatches": mismatches,
        "fix_command": FIX_COMMAND,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 iteration fixtures 是否与渲染结果一致",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出详细信息")
    parser.add_argument(
        "--fixtures-root",
        type=Path,
        default=None,
        help="fixtures 根目录（默认: tests/iteration/fixtures）",
    )
    args = parser.parse_args()

    project_root = get_project_root()
    fixtures_root = args.fixtures_root or (project_root / "tests" / "iteration" / "fixtures")

    result = run_check(project_root, fixtures_root, verbose=args.verbose and not args.json)

    if args.json:
        print(format_json_report(result, project_root))
    else:
        print(format_text_report(result, project_root))

    return 0 if result.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
