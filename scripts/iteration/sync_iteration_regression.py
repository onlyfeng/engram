#!/usr/bin/env python3
"""同步 regression 文档中的自动生成区块。

用法:
    python scripts/iteration/sync_iteration_regression.py <iteration_number> [options]

示例:
    # 预览模式（默认输出到 stdout）
    python scripts/iteration/sync_iteration_regression.py 13

    # 使用 regression profile
    python scripts/iteration/sync_iteration_regression.py 13 --profile regression

    # 写回文件
    python scripts/iteration/sync_iteration_regression.py 13 --write

功能:
    1. 同步最小门禁命令块（min_gate_block）
    2. 同步验收证据片段（evidence_snippet）
    3. 若目标区块不存在，在标准位置插入

区块标记:
    最小门禁命令块:
        <!-- BEGIN GENERATED: min_gate_block profile=xxx -->
        ...
        <!-- END GENERATED: min_gate_block -->

    验收证据片段:
        <!-- BEGIN GENERATED: evidence_snippet -->
        ...
        <!-- END GENERATED: evidence_snippet -->
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent))

import generated_blocks as blocks  # noqa: E402
from generated_blocks import GateProfile  # noqa: E402

# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class SyncResult:
    """同步操作结果。"""

    success: bool
    message: str
    updated_content: Optional[str] = None
    min_gate_changed: bool = False
    evidence_changed: bool = False
    min_gate_inserted: bool = False
    evidence_inserted: bool = False


# ============================================================================
# 核心同步逻辑
# ============================================================================


def sync_min_gate_block(
    content: str,
    iteration_number: int,
    profile: GateProfile,
) -> Tuple[str, bool, bool]:
    """同步最小门禁命令块。

    Returns:
        (updated_content, changed, inserted)
    """
    block = blocks.find_min_gate_block(content)
    new_block = blocks.generate_min_gate_block_with_markers(iteration_number, profile)

    if block:
        # 替换已有区块
        old_content = content[block.begin_pos : block.end_pos]
        if old_content.strip() == new_block.strip():
            return content, False, False

        updated = content[: block.begin_pos] + new_block + content[block.end_pos :]
        return updated, True, False
    else:
        # 插入新区块
        insert_pos = blocks.find_min_gate_insert_position(content)

        # 确保有适当的换行
        prefix = "\n\n" if insert_pos > 0 and content[insert_pos - 1] != "\n" else "\n"
        suffix = "\n\n"

        updated = content[:insert_pos] + prefix + new_block + suffix + content[insert_pos:]
        return updated, True, True


def sync_evidence_block(
    content: str,
    iteration_number: int,
) -> Tuple[str, bool, bool]:
    """同步验收证据片段。

    Returns:
        (updated_content, changed, inserted)
    """
    evidence = blocks.load_evidence(iteration_number)

    if evidence:
        new_block = blocks.generate_evidence_block_with_markers(evidence)
    else:
        new_block = blocks.generate_evidence_placeholder()

    block = blocks.find_evidence_block(content)

    if block:
        # 替换已有区块
        old_content = content[block.begin_pos : block.end_pos]
        if old_content.strip() == new_block.strip():
            return content, False, False

        updated = content[: block.begin_pos] + new_block + content[block.end_pos :]
        return updated, True, False
    else:
        # 检查是否有非自动生成的 ## 验收证据 section
        evidence_section_pattern = re.compile(r"^##\s+验收证据\s*$", re.MULTILINE)
        match = evidence_section_pattern.search(content)

        if match:
            # 找到该 section 的结束位置
            next_section = re.search(r"^##\s+", content[match.end() :], re.MULTILINE)
            if next_section:
                end_pos = match.end() + next_section.start()
            else:
                end_pos = len(content)

            # 替换整个 section
            updated = content[: match.start()] + new_block + "\n\n" + content[end_pos:].lstrip()
            return updated, True, True
        else:
            # 插入新区块
            insert_pos = blocks.find_evidence_insert_position(content)

            prefix = "\n\n" if insert_pos > 0 and content[insert_pos - 1] != "\n" else "\n"
            suffix = "\n"

            updated = content[:insert_pos] + prefix + new_block + suffix + content[insert_pos:]
            return updated, True, True


def sync_iteration_regression(
    iteration_number: int,
    profile: GateProfile = "full",
    *,
    write: bool = False,
    sync_min_gate: bool = True,
    sync_evidence: bool = True,
) -> SyncResult:
    """同步迭代回归文档。

    Args:
        iteration_number: 迭代编号
        profile: min gate block profile
        write: 是否写回文件
        sync_min_gate: 是否同步最小门禁命令块
        sync_evidence: 是否同步验收证据片段

    Returns:
        SyncResult 操作结果
    """
    doc_path = blocks.get_regression_doc_path(iteration_number)

    if not doc_path.exists():
        return SyncResult(
            success=False,
            message=f"回归文档不存在: {doc_path}",
        )

    content = doc_path.read_text(encoding="utf-8")
    updated_content = content

    min_gate_changed = False
    min_gate_inserted = False
    evidence_changed = False
    evidence_inserted = False

    # 同步最小门禁命令块
    if sync_min_gate:
        updated_content, min_gate_changed, min_gate_inserted = sync_min_gate_block(
            updated_content, iteration_number, profile
        )

    # 同步验收证据片段
    if sync_evidence:
        updated_content, evidence_changed, evidence_inserted = sync_evidence_block(
            updated_content, iteration_number
        )

    # 检查是否有变更
    any_change = min_gate_changed or evidence_changed

    if not any_change:
        return SyncResult(
            success=True,
            message="内容无变化，无需更新",
            updated_content=content,
        )

    # 构建消息
    changes: List[str] = []
    if min_gate_changed:
        action = "插入" if min_gate_inserted else "更新"
        changes.append(f"min_gate_block ({action}, profile={profile})")
    if evidence_changed:
        action = "插入" if evidence_inserted else "更新"
        changes.append(f"evidence_snippet ({action})")

    message = f"已同步: {', '.join(changes)}"

    if write:
        doc_path.write_text(updated_content, encoding="utf-8")
        message = f"[写入] {message}"
    else:
        message = f"[预览] {message}"

    return SyncResult(
        success=True,
        message=message,
        updated_content=updated_content,
        min_gate_changed=min_gate_changed,
        evidence_changed=evidence_changed,
        min_gate_inserted=min_gate_inserted,
        evidence_inserted=evidence_inserted,
    )


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="同步 regression 文档中的自动生成区块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 预览模式（输出到 stdout）
    python scripts/iteration/sync_iteration_regression.py 13

    # 使用 regression profile
    python scripts/iteration/sync_iteration_regression.py 13 --profile regression

    # 写回文件
    python scripts/iteration/sync_iteration_regression.py 13 --write

    # 仅同步 min_gate_block
    python scripts/iteration/sync_iteration_regression.py 13 --only-min-gate --write

    # 仅同步 evidence_snippet
    python scripts/iteration/sync_iteration_regression.py 13 --only-evidence --write

区块标记:
    最小门禁命令块:
        <!-- BEGIN GENERATED: min_gate_block profile=xxx -->
        <!-- END GENERATED: min_gate_block -->

    验收证据片段:
        <!-- BEGIN GENERATED: evidence_snippet -->
        <!-- END GENERATED: evidence_snippet -->
        """,
    )
    parser.add_argument(
        "iteration_number",
        type=int,
        help="迭代编号",
    )
    parser.add_argument(
        "--profile",
        "-p",
        type=str,
        choices=blocks.SUPPORTED_PROFILES,
        default="full",
        help="min_gate_block 的 profile（默认: full）",
    )
    parser.add_argument(
        "--write",
        "-w",
        action="store_true",
        help="写回文件（默认仅预览）",
    )
    parser.add_argument(
        "--only-min-gate",
        action="store_true",
        help="仅同步 min_gate_block",
    )
    parser.add_argument(
        "--only-evidence",
        action="store_true",
        help="仅同步 evidence_snippet",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="安静模式，仅输出错误",
    )

    args = parser.parse_args()

    # 确定同步范围
    sync_min_gate = not args.only_evidence
    sync_evidence = not args.only_min_gate

    if args.only_min_gate and args.only_evidence:
        sync_min_gate = True
        sync_evidence = True

    # 执行同步
    result = sync_iteration_regression(
        iteration_number=args.iteration_number,
        profile=args.profile,
        write=args.write,
        sync_min_gate=sync_min_gate,
        sync_evidence=sync_evidence,
    )

    if not result.success:
        print(f"❌ 错误: {result.message}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(result.message)

    # 预览模式输出内容
    if not args.write and result.updated_content and not args.quiet:
        if result.min_gate_changed or result.evidence_changed:
            print("\n" + "=" * 60)
            print("预览内容 (使用 --write 写入):")
            print("=" * 60 + "\n")
            print(result.updated_content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
