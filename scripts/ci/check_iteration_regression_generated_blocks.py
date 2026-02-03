#!/usr/bin/env python3
"""
检查迭代回归文档中的受控块是否与生成结果一致。

功能:
1. 扫描 docs/acceptance/iteration_*_regression.md
2. 当检测到受控块 marker 时，渲染期望内容并比对
3. 发现 mismatch 时给出修复建议

用法:
    python scripts/ci/check_iteration_regression_generated_blocks.py --verbose

退出码:
    0 - 检查通过
    1 - 存在 mismatch 或受控块结构错误
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# 注入 iteration 脚本路径，便于复用生成逻辑
ITERATION_DIR = Path(__file__).resolve().parent.parent / "iteration"
sys.path.insert(0, str(ITERATION_DIR))

from generated_blocks import (  # noqa: E402
    SUPPORTED_PROFILES,
    extract_block,
    find_evidence_block,
    find_min_gate_block,
    generate_evidence_block_with_markers,
    generate_evidence_placeholder,
    generate_min_gate_block_with_markers,
    load_evidence,
)

# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class BlockMismatch:
    """受控块不一致记录。"""

    file: Path
    block_type: str
    message: str
    profile: Optional[str] = None


# ============================================================================
# 配置
# ============================================================================


ITERATION_DOC_PATTERN = re.compile(r"^iteration_(\d+)_regression\.md$")


def get_project_root() -> Path:
    """获取项目根目录。"""

    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def get_regression_docs(project_root: Path) -> List[Path]:
    """获取所有回归文档路径。"""

    acceptance_dir = project_root / "docs" / "acceptance"
    if not acceptance_dir.exists():
        return []

    return sorted(acceptance_dir.glob("iteration_*_regression.md"))


def extract_iteration_number(file_path: Path) -> Optional[int]:
    """从文件名中提取迭代编号。"""

    match = ITERATION_DOC_PATTERN.match(file_path.name)
    if not match:
        return None
    return int(match.group(1))


def has_min_gate_marker(content: str) -> bool:
    """判断是否包含 min_gate_block marker。"""

    return "BEGIN GENERATED: min_gate_block" in content


def has_evidence_marker(content: str) -> bool:
    """判断是否包含 evidence_snippet marker。"""

    return "BEGIN GENERATED: evidence_snippet" in content or "AUTO-GENERATED EVIDENCE BLOCK START" in content


# ============================================================================
# 核心检查逻辑
# ============================================================================


def check_min_gate_block(
    file_path: Path, iteration_number: int, content: str
) -> List[BlockMismatch]:
    """检查 min_gate_block 是否一致。"""

    mismatches: List[BlockMismatch] = []

    if not has_min_gate_marker(content):
        return mismatches

    block = find_min_gate_block(content)
    if not block:
        mismatches.append(
            BlockMismatch(
                file=file_path,
                block_type="min_gate_block",
                message="检测到 BEGIN marker，但未找到匹配的 END marker",
            )
        )
        return mismatches

    profile = block.profile or "full"
    if profile not in SUPPORTED_PROFILES:
        mismatches.append(
            BlockMismatch(
                file=file_path,
                block_type="min_gate_block",
                profile=profile,
                message=f"不支持的 profile: {profile}",
            )
        )
        return mismatches

    expected = generate_min_gate_block_with_markers(iteration_number, profile)
    actual = extract_block(content, block)

    if actual.strip() != expected.strip():
        mismatches.append(
            BlockMismatch(
                file=file_path,
                block_type="min_gate_block",
                profile=profile,
                message="内容与渲染结果不一致",
            )
        )

    return mismatches


def check_evidence_block(
    file_path: Path, iteration_number: int, content: str
) -> List[BlockMismatch]:
    """检查 evidence_snippet 是否一致。"""

    mismatches: List[BlockMismatch] = []

    if not has_evidence_marker(content):
        return mismatches

    block = find_evidence_block(content)
    if not block:
        mismatches.append(
            BlockMismatch(
                file=file_path,
                block_type="evidence_snippet",
                message="检测到 BEGIN marker，但未找到匹配的 END marker",
            )
        )
        return mismatches

    evidence = load_evidence(iteration_number)
    if evidence:
        expected = generate_evidence_block_with_markers(evidence)
    else:
        expected = generate_evidence_placeholder()

    actual = extract_block(content, block)

    if actual.strip() != expected.strip():
        mismatches.append(
            BlockMismatch(
                file=file_path,
                block_type="evidence_snippet",
                message="内容与渲染结果不一致",
            )
        )

    return mismatches


def run_check(project_root: Path, verbose: bool = False) -> List[BlockMismatch]:
    """运行受控块一致性检查。"""

    mismatches: List[BlockMismatch] = []
    docs = get_regression_docs(project_root)

    if verbose:
        print(f"[INFO] 将检查 {len(docs)} 个回归文档")
        for doc in docs:
            print(f"       - {doc.relative_to(project_root)}")
        print()

    for doc_path in docs:
        iteration_number = extract_iteration_number(doc_path)
        if iteration_number is None:
            continue

        try:
            content = doc_path.read_text(encoding="utf-8")
        except OSError as exc:
            mismatches.append(
                BlockMismatch(
                    file=doc_path,
                    block_type="file",
                    message=f"读取失败: {exc}",
                )
            )
            continue

        mismatches.extend(check_min_gate_block(doc_path, iteration_number, content))
        mismatches.extend(check_evidence_block(doc_path, iteration_number, content))

    return mismatches


def print_report(mismatches: List[BlockMismatch], project_root: Path) -> None:
    """打印检查报告。"""

    print()
    print("=" * 70)
    print("迭代回归文档受控块检查报告")
    print("=" * 70)
    print()
    print(f"违规条目数:      {len(mismatches)}")

    if not mismatches:
        print()
        print("[OK] 受控块内容全部一致")
        return

    by_file: dict[Path, List[BlockMismatch]] = {}
    for mismatch in mismatches:
        by_file.setdefault(mismatch.file, []).append(mismatch)

    for file_path, items in sorted(by_file.items()):
        rel_path = file_path.relative_to(project_root)
        print(f"\n【{rel_path}】({len(items)} 条)")
        for item in items:
            profile_info = f" (profile={item.profile})" if item.profile else ""
            print(f"  - {item.block_type}{profile_info}: {item.message}")

    print()
    print("修复建议:")
    print("  - 使用脚本同步受控块（避免手工修改）:")
    print("    python scripts/iteration/sync_iteration_regression.py <N> --profile <profile> --write")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查迭代回归文档中的受控块是否与生成结果一致",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细输出",
    )

    args = parser.parse_args()
    project_root = get_project_root()

    print("=" * 70)
    print("迭代回归文档受控块检查")
    print("=" * 70)

    mismatches = run_check(project_root, verbose=args.verbose)
    print_report(mismatches, project_root)

    if mismatches:
        print()
        print(f"[FAIL] 存在 {len(mismatches)} 条受控块不一致")
        print("[FAIL] 退出码: 1")
        return 1

    print()
    print("[OK] 所有检查通过")
    print("[OK] 退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
