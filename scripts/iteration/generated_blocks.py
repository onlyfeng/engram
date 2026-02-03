#!/usr/bin/env python3
"""迭代回归文档受控块的生成与解析逻辑。"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from engram.common.redaction import redact_sensitive_text
from render_min_gate_block import (  # noqa: E402
    GateProfile,
    SUPPORTED_PROFILES,
    render_min_gate_block,
)
from iteration_evidence_schema import resolve_schema_name  # noqa: E402

# ============================================================================
# 路径配置
# ============================================================================

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


# ============================================================================
# 基础文件操作
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
# 区块查找与提取
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


def extract_block(content: str, block: BlockInfo) -> str:
    """提取区块内容（含 markers）。"""

    return content[block.begin_pos : block.end_pos]


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
    """渲染验收证据片段。"""

    iteration_number = evidence.get("iteration_number", "N/A")
    recorded_at = evidence.get("recorded_at", "N/A")
    commit_sha = evidence.get("commit_sha", "N/A")
    overall_result = evidence.get("overall_result", "N/A")
    commands: List[dict] = evidence.get("commands", [])
    notes = evidence.get("notes")
    links = evidence.get("links", {})
    schema_value = evidence.get("$schema") if isinstance(evidence.get("$schema"), str) else None
    schema_name = resolve_schema_name(schema_value)
    schema_display = schema_name if schema_value else "v2"

    evidence_file_name = f"iteration_{iteration_number}_evidence.json"
    evidence_file_link = f"[`{evidence_file_name}`](evidence/{evidence_file_name})"

    lines = [
        "## 验收证据",
        "",
        "<!-- 此段落由脚本自动生成，请勿手动编辑 -->",
        "",
        "| 项目 | 值 |",
        "|------|-----|",
        f"| **证据文件** | {evidence_file_link} |",
        f"| **Schema 版本** | `{schema_display}` |",
        f"| **记录时间** | {recorded_at} |",
        f"| **Commit SHA** | `{commit_sha[:7] if len(commit_sha) >= 7 else commit_sha}` |",
    ]

    ci_run_url = links.get("ci_run_url")
    if ci_run_url:
        safe_ci_run_url = redact_sensitive_text(ci_run_url)
        lines.append(f"| **CI 运行** | [{safe_ci_run_url}]({safe_ci_run_url}) |")

    lines.append("")

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
            cmd_name = redact_sensitive_text(cmd_name)
            result = cmd.get("result", "N/A")
            duration = cmd.get("duration_seconds")
            summary = cmd.get("summary") or "-"
            summary = redact_sensitive_text(summary)

            duration_str = f"{duration:.1f}s" if duration is not None else "-"
            result_icon = "✅" if result == "PASS" else "❌" if result == "FAIL" else "⏭️"

            lines.append(f"| `{cmd_name}` | {result_icon} {result} | {duration_str} | {summary} |")

        lines.append("")

    result_icon = "✅" if overall_result == "PASS" else "⚠️" if overall_result == "PARTIAL" else "❌"
    lines.extend(
        [
            "### 整体验收结果",
            "",
            f"- **结果**: {result_icon} {overall_result}",
        ]
    )

    if notes:
        safe_notes = redact_sensitive_text(notes)
        if safe_notes:
            lines.append(f"- **说明**: {safe_notes}")

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

