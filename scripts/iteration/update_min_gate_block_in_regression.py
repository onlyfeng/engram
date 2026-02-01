#!/usr/bin/env python3
"""更新回归文档中的最小门禁命令块。

用法:
    python scripts/iteration/update_min_gate_block_in_regression.py <iteration_number>

示例:
    # 更新 Iteration 13 的回归文档中的门禁命令块
    python scripts/iteration/update_min_gate_block_in_regression.py 13

    # 预览模式（不写入文件）
    python scripts/iteration/update_min_gate_block_in_regression.py 13 --dry-run

功能:
    1. 读取 docs/acceptance/iteration_<N>_regression.md
    2. 定位 <!-- BEGIN GENERATED: min_gate_block profile=... --> 与 <!-- END GENERATED --> 之间的区块
    3. 使用 render_min_gate_block.py 的输出替换区块内容
    4. 写回文件（或 --dry-run 预览）

生成区块格式:
    <!-- BEGIN GENERATED: min_gate_block profile=full -->
    （自动生成的内容）
    <!-- END GENERATED -->
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from render_min_gate_block import (  # noqa: E402
    SUPPORTED_PROFILES,
    GateProfile,
    render_min_gate_block,
)

# 回归文档目录
REGRESSION_DOCS_DIR = Path(__file__).parent.parent.parent / "docs" / "acceptance"

# 生成区块的正则表达式
# 匹配: <!-- BEGIN GENERATED: min_gate_block profile=xxx -->
BEGIN_MARKER_PATTERN = re.compile(
    r"<!--\s*BEGIN\s+GENERATED:\s*min_gate_block\s+profile=(\w+(?:-\w+)?)\s*-->"
)
# 匹配: <!-- END GENERATED -->
END_MARKER_PATTERN = re.compile(r"<!--\s*END\s+GENERATED\s*-->")


def get_regression_doc_path(iteration_number: int) -> Path:
    """获取回归文档路径。

    Args:
        iteration_number: 迭代编号

    Returns:
        回归文档的 Path 对象
    """
    return REGRESSION_DOCS_DIR / f"iteration_{iteration_number}_regression.md"


def find_generated_block(
    content: str,
) -> Optional[Tuple[int, int, str]]:
    """查找生成区块的位置。

    Args:
        content: 文档内容

    Returns:
        如果找到，返回 (begin_pos, end_pos, profile)，其中：
        - begin_pos: BEGIN 标记的起始位置
        - end_pos: END 标记的结束位置（包含 END 标记）
        - profile: 区块的 profile 值
        如果未找到，返回 None
    """
    begin_match = BEGIN_MARKER_PATTERN.search(content)
    if not begin_match:
        return None

    profile = begin_match.group(1)
    begin_pos = begin_match.start()

    # 从 BEGIN 标记之后开始搜索 END 标记
    end_match = END_MARKER_PATTERN.search(content, begin_match.end())
    if not end_match:
        return None

    end_pos = end_match.end()

    return (begin_pos, end_pos, profile)


def generate_block_with_markers(iteration_number: int, profile: GateProfile) -> str:
    """生成带有 marker 的完整区块。

    Args:
        iteration_number: 迭代编号
        profile: 门禁 profile

    Returns:
        带有 BEGIN/END marker 的完整区块内容
    """
    content = render_min_gate_block(iteration_number, profile)

    return f"""<!-- BEGIN GENERATED: min_gate_block profile={profile} -->

{content}

<!-- END GENERATED -->"""


def update_min_gate_block_in_content(
    content: str,
    iteration_number: int,
    profile_override: Optional[GateProfile] = None,
) -> Tuple[str, bool, str]:
    """更新文档内容中的门禁命令块。

    Args:
        content: 原始文档内容
        iteration_number: 迭代编号
        profile_override: 可选的 profile 覆盖值，如果为 None 则使用文档中的 profile

    Returns:
        (updated_content, changed, profile) 元组：
        - updated_content: 更新后的内容
        - changed: 内容是否发生变化
        - profile: 使用的 profile
    """
    block_info = find_generated_block(content)

    if block_info is None:
        # 未找到生成区块，返回原内容
        return content, False, "full"

    begin_pos, end_pos, doc_profile = block_info

    # 确定使用的 profile
    profile: GateProfile = profile_override if profile_override else doc_profile  # type: ignore[assignment]

    # 验证 profile 有效性
    if profile not in SUPPORTED_PROFILES:
        profile = "full"

    # 生成新的区块内容
    new_block = generate_block_with_markers(iteration_number, profile)

    # 替换区块
    updated_content = content[:begin_pos] + new_block + content[end_pos:]

    # 检查是否有变化
    changed = updated_content != content

    return updated_content, changed, profile


def update_min_gate_block_in_file(
    iteration_number: int,
    profile_override: Optional[GateProfile] = None,
    dry_run: bool = False,
) -> Tuple[bool, str, str]:
    """更新回归文档中的门禁命令块。

    Args:
        iteration_number: 迭代编号
        profile_override: 可选的 profile 覆盖值
        dry_run: 是否为预览模式（不写入文件）

    Returns:
        (success, message, profile) 元组：
        - success: 是否成功
        - message: 操作结果消息
        - profile: 使用的 profile
    """
    doc_path = get_regression_doc_path(iteration_number)

    if not doc_path.exists():
        return False, f"回归文档不存在: {doc_path}", "full"

    # 读取文件内容
    content = doc_path.read_text(encoding="utf-8")

    # 检查是否有生成区块
    if not find_generated_block(content):
        return (
            False,
            "未找到生成区块标记 (<!-- BEGIN GENERATED: min_gate_block profile=... -->)",
            "full",
        )

    # 更新内容
    updated_content, changed, profile = update_min_gate_block_in_content(
        content, iteration_number, profile_override
    )

    if not changed:
        return True, f"内容无变化，无需更新 (profile={profile})", profile

    if dry_run:
        return True, f"[DRY-RUN] 将更新门禁命令块 (profile={profile})", profile

    # 写入文件
    doc_path.write_text(updated_content, encoding="utf-8")

    return True, f"已更新门禁命令块 (profile={profile})", profile


# ============================================================================
# CLI 入口
# ============================================================================


def main() -> int:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="更新回归文档中的最小门禁命令块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 更新 Iteration 13 的回归文档
    python scripts/iteration/update_min_gate_block_in_regression.py 13

    # 预览模式（不写入文件）
    python scripts/iteration/update_min_gate_block_in_regression.py 13 --dry-run

    # 覆盖 profile（忽略文档中的 profile 设置）
    python scripts/iteration/update_min_gate_block_in_regression.py 13 --profile regression
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
        choices=SUPPORTED_PROFILES,
        default=None,
        help="覆盖 profile（默认使用文档中的 profile）",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="预览模式，不写入文件",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="详细输出",
    )

    args = parser.parse_args()

    # 执行更新
    success, message, profile = update_min_gate_block_in_file(
        args.iteration_number,
        profile_override=args.profile,
        dry_run=args.dry_run,
    )

    if args.verbose or not success:
        print(message)

    if success:
        if args.verbose:
            doc_path = get_regression_doc_path(args.iteration_number)
            print(f"文件: {doc_path}")
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
