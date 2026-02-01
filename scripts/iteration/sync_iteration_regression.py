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
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Tuple

# 添加脚本目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from render_min_gate_block import (  # noqa: E402
    SUPPORTED_PROFILES,
    GateProfile,
    render_min_gate_block,
)

# ============================================================================
# 常量定义
# ============================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGRESSION_DOCS_DIR = REPO_ROOT / "docs" / "acceptance"
EVIDENCE_DIR = REGRESSION_DOCS_DIR / "evidence"

# 区块类型
BlockType = Literal["min_gate_block", "evidence_snippet"]

# ============================================================================
# 区块标记正则表达式
# ============================================================================

# 最小门禁命令块 marker
MIN_GATE_BEGIN_PATTERN = re.compile(
    r"<!--\s*BEGIN\s+GENERATED:\s*min_gate_block\s+profile=(\w+(?:-\w+)?)\s*-->"
)
MIN_GATE_END_PATTERN = re.compile(r"<!--\s*END\s+GENERATED:\s*min_gate_block\s*-->")
# 兼容旧格式: <!-- END GENERATED -->
MIN_GATE_END_LEGACY_PATTERN = re.compile(r"<!--\s*END\s+GENERATED\s*-->")

# 验收证据片段 marker
EVIDENCE_BEGIN_PATTERN = re.compile(r"<!--\s*BEGIN\s+GENERATED:\s*evidence_snippet\s*-->")
EVIDENCE_END_PATTERN = re.compile(r"<!--\s*END\s+GENERATED:\s*evidence_snippet\s*-->")
# 兼容旧格式
EVIDENCE_BEGIN_LEGACY_PATTERN = re.compile(
    r"<!--\s*AUTO-GENERATED\s+EVIDENCE\s+BLOCK\s+START\s*-->"
)
EVIDENCE_END_LEGACY_PATTERN = re.compile(r"<!--\s*AUTO-GENERATED\s+EVIDENCE\s+BLOCK\s+END\s*-->")


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class BlockInfo:
    """区块信息。"""

    block_type: BlockType
    begin_pos: int
    end_pos: int
    profile: Optional[str] = None  # 仅 min_gate_block 使用


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
# 辅助函数
# ============================================================================


def get_regression_doc_path(iteration_number: int) -> Path:
    """获取回归文档路径。"""
    return REGRESSION_DOCS_DIR / f"iteration_{iteration_number}_regression.md"


def get_evidence_path(iteration_number: int) -> Path:
    """获取证据文件路径。"""
    return EVIDENCE_DIR / f"iteration_{iteration_number}_evidence.json"


def load_evidence(iteration_number: int) -> Optional[dict]:
    """加载证据文件。"""
    path = get_evidence_path(iteration_number)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ============================================================================
# 区块查找函数
# ============================================================================


def find_min_gate_block(content: str) -> Optional[BlockInfo]:
    """查找最小门禁命令块。"""
    begin_match = MIN_GATE_BEGIN_PATTERN.search(content)
    if not begin_match:
        return None

    profile = begin_match.group(1)
    begin_pos = begin_match.start()

    # 优先尝试新格式 END marker
    end_match = MIN_GATE_END_PATTERN.search(content, begin_match.end())
    if not end_match:
        # 回退到旧格式
        end_match = MIN_GATE_END_LEGACY_PATTERN.search(content, begin_match.end())
        if not end_match:
            return None

    return BlockInfo(
        block_type="min_gate_block",
        begin_pos=begin_pos,
        end_pos=end_match.end(),
        profile=profile,
    )


def find_evidence_block(content: str) -> Optional[BlockInfo]:
    """查找验收证据片段块。"""
    # 优先尝试新格式
    begin_match = EVIDENCE_BEGIN_PATTERN.search(content)
    end_pattern = EVIDENCE_END_PATTERN

    if not begin_match:
        # 回退到旧格式
        begin_match = EVIDENCE_BEGIN_LEGACY_PATTERN.search(content)
        end_pattern = EVIDENCE_END_LEGACY_PATTERN

    if not begin_match:
        return None

    begin_pos = begin_match.start()

    end_match = end_pattern.search(content, begin_match.end())
    if not end_match:
        return None

    return BlockInfo(
        block_type="evidence_snippet",
        begin_pos=begin_pos,
        end_pos=end_match.end(),
    )


# ============================================================================
# 区块内容生成
# ============================================================================


def generate_min_gate_block_with_markers(
    iteration_number: int,
    profile: GateProfile,
) -> str:
    """生成带 marker 的最小门禁命令块。"""
    content = render_min_gate_block(iteration_number, profile)
    return f"""<!-- BEGIN GENERATED: min_gate_block profile={profile} -->

{content}

<!-- END GENERATED: min_gate_block -->"""


def render_evidence_snippet(evidence: dict) -> str:
    """渲染验收证据片段。

    Args:
        evidence: 证据 JSON 数据

    Returns:
        渲染后的 Markdown 内容
    """
    iteration_number = evidence.get("iteration_number", "N/A")
    recorded_at = evidence.get("recorded_at", "N/A")
    commit_sha = evidence.get("commit_sha", "N/A")
    overall_result = evidence.get("overall_result", "N/A")
    commands: List[dict] = evidence.get("commands", [])
    notes = evidence.get("notes")
    links = evidence.get("links", {})

    # 证据文件相对路径
    evidence_file_name = f"iteration_{iteration_number}_evidence.json"
    evidence_file_link = f"[`{evidence_file_name}`](evidence/{evidence_file_name})"

    # 基本信息表格
    lines = [
        "## 验收证据",
        "",
        "<!-- 此段落由脚本自动生成，请勿手动编辑 -->",
        "",
        "| 项目 | 值 |",
        "|------|-----|",
        f"| **证据文件** | {evidence_file_link} |",
        "| **Schema 版本** | `iteration_evidence_v1.schema.json` |",
        f"| **记录时间** | {recorded_at} |",
        f"| **Commit SHA** | `{commit_sha[:7] if len(commit_sha) >= 7 else commit_sha}` |",
    ]

    # 添加 CI URL（如果有）
    ci_run_url = links.get("ci_run_url")
    if ci_run_url:
        lines.append(f"| **CI 运行** | [{ci_run_url}]({ci_run_url}) |")

    lines.append("")

    # 门禁命令执行摘要
    if commands:
        lines.extend(
            [
                "### 门禁命令执行摘要",
                "",
                "> 以下表格由脚本从证据文件自动渲染。",
                "",
                "| 命令 | 结果 | 耗时 | 摘要 |",
                "|------|------|------|------|",
            ]
        )

        for cmd in commands:
            cmd_name = cmd.get("command", cmd.get("name", "N/A"))
            result = cmd.get("result", "N/A")
            duration = cmd.get("duration_seconds")
            summary = cmd.get("summary", "-")

            duration_str = f"{duration:.1f}s" if duration is not None else "-"

            # 结果图标
            result_icon = "✅" if result == "PASS" else "❌" if result == "FAIL" else "⏭️"

            lines.append(f"| `{cmd_name}` | {result_icon} {result} | {duration_str} | {summary} |")

        lines.append("")

    # 整体验收结果
    result_icon = "✅" if overall_result == "PASS" else "⚠️" if overall_result == "PARTIAL" else "❌"
    lines.extend(
        [
            "### 整体验收结果",
            "",
            f"- **结果**: {result_icon} {overall_result}",
        ]
    )

    if notes:
        lines.append(f"- **说明**: {notes}")

    lines.append("")

    return "\n".join(lines)


def generate_evidence_block_with_markers(evidence: dict) -> str:
    """生成带 marker 的验收证据片段。"""
    content = render_evidence_snippet(evidence)
    return f"""<!-- BEGIN GENERATED: evidence_snippet -->

{content}

<!-- END GENERATED: evidence_snippet -->"""


def generate_evidence_placeholder() -> str:
    """生成验收证据占位符（当证据文件不存在时）。"""
    return """<!-- BEGIN GENERATED: evidence_snippet -->

## 验收证据

> 证据文件尚未生成。使用以下命令生成：
>
> ```bash
> python scripts/iteration/record_iteration_evidence.py <N> --add-command "ci:make ci:PASS"
> ```

<!-- END GENERATED: evidence_snippet -->"""


# ============================================================================
# 区块插入位置
# ============================================================================


def find_min_gate_insert_position(content: str) -> int:
    """查找最小门禁命令块的插入位置。

    优先在 `## 执行信息` 后插入，其次在 `## 执行结果总览` 前插入。
    """
    # 查找 ## 执行信息 之后
    exec_info_pattern = re.compile(r"^##\s+执行信息\s*$", re.MULTILINE)
    match = exec_info_pattern.search(content)
    if match:
        # 找到该 section 的结束位置（下一个 ## 或文件末尾）
        next_section = re.search(r"^##\s+", content[match.end() :], re.MULTILINE)
        if next_section:
            return match.end() + next_section.start()
        return len(content)

    # 查找 ## 执行结果总览 之前
    result_overview_pattern = re.compile(r"^##\s+执行结果总览\s*$", re.MULTILINE)
    match = result_overview_pattern.search(content)
    if match:
        return match.start()

    # 默认在文件开头之后（跳过标题）
    title_pattern = re.compile(r"^#\s+.+$", re.MULTILINE)
    match = title_pattern.search(content)
    if match:
        return match.end() + 1

    return 0


def find_evidence_insert_position(content: str) -> int:
    """查找验收证据片段的插入位置。

    优先在 `## 验收证据` 位置，其次在 `## 相关文档` 前，最后在文件末尾。
    """
    # 查找已有的 ## 验收证据 section
    evidence_section_pattern = re.compile(r"^##\s+验收证据\s*$", re.MULTILINE)
    match = evidence_section_pattern.search(content)
    if match:
        # 找到该 section 的结束位置（下一个 ## 或文件末尾）
        next_section = re.search(r"^##\s+", content[match.end() :], re.MULTILINE)
        if next_section:
            # 替换整个 section
            return match.start()
        # section 在文件末尾
        return match.start()

    # 查找 ## 相关文档 之前
    related_docs_pattern = re.compile(r"^##\s+相关文档\s*$", re.MULTILINE)
    match = related_docs_pattern.search(content)
    if match:
        return match.start()

    # 默认在文件末尾
    return len(content)


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
    block = find_min_gate_block(content)
    new_block = generate_min_gate_block_with_markers(iteration_number, profile)

    if block:
        # 替换已有区块
        old_content = content[block.begin_pos : block.end_pos]
        if old_content.strip() == new_block.strip():
            return content, False, False

        updated = content[: block.begin_pos] + new_block + content[block.end_pos :]
        return updated, True, False
    else:
        # 插入新区块
        insert_pos = find_min_gate_insert_position(content)

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
    evidence = load_evidence(iteration_number)

    if evidence:
        new_block = generate_evidence_block_with_markers(evidence)
    else:
        new_block = generate_evidence_placeholder()

    block = find_evidence_block(content)

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
            insert_pos = find_evidence_insert_position(content)

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
    doc_path = get_regression_doc_path(iteration_number)

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
        choices=SUPPORTED_PROFILES,
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
