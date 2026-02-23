from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


BEGIN_MARKER = "<!-- BEGIN GENERATED:AGENT_MEMORY_RECALL_RULE -->"
END_MARKER = "<!-- END GENERATED:AGENT_MEMORY_RECALL_RULE -->"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_ssot(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required_keys = [
        "id",
        "title",
        "description",
        "always_apply",
        "globs",
        "triggers",
        "execution_steps",
        "requirements",
        "knowledge_index",
        "fallback_doc",
    ]
    missing = [key for key in required_keys if key not in data]
    if missing:
        missing_str = ", ".join(missing)
        raise ValueError(f"SSOT 缺少字段: {missing_str}")
    return data


def _render_cursor_rule(rule: dict[str, Any]) -> str:
    globs_lines = "\n".join(f"  - {item}" for item in rule["globs"])
    trigger_lines = "\n".join(
        f"{idx}. **{item}**" for idx, item in enumerate(rule["triggers"], start=1)
    )
    step_lines = "\n\n".join(
        (
            f"### 步骤 {idx}：{step['title']}\n\n"
            f"{step['action']}"
        )
        for idx, step in enumerate(rule["execution_steps"], start=1)
    )
    requirement_lines = "\n".join(f"- {item}" for item in rule["requirements"])
    index_lines = "\n".join(f"- {item}" for item in rule["knowledge_index"])
    always_apply = "true" if rule["always_apply"] else "false"

    return (
        "---\n"
        f"description: {rule['description']}\n"
        "globs:\n"
        f"{globs_lines}\n"
        f"alwaysApply: {always_apply}\n"
        "---\n\n"
        "<!-- GENERATED: DO NOT EDIT DIRECTLY. Source: .agentx/ssot/engram_memory_recall.rule.json -->\n\n"
        f"# {rule['title']}\n\n"
        "当你处理以下任务时，**必须先获取相关经验记忆**，再开始修改。\n\n"
        "## 触发场景\n\n"
        f"{trigger_lines}\n\n"
        "## 操作步骤（优先 Engram → 降级到本地文档）\n\n"
        f"{step_lines}\n\n"
        "## 最低要求\n\n"
        f"{requirement_lines}\n\n"
        "## 关键经验索引（供查询参考）\n\n"
        f"{index_lines}\n"
    )


def _render_agents_block(rule: dict[str, Any], *, heading: str, doc_hint: str) -> str:
    trigger_lines = "\n".join(f"- {item}" for item in rule["triggers"])
    step_lines = "\n".join(
        f"{idx}. {step['title']}：{step['action']}"
        for idx, step in enumerate(rule["execution_steps"], start=1)
    )
    requirement_lines = "\n".join(f"- {item}" for item in rule["requirements"])

    return (
        f"{heading}\n\n"
        f"> 本节由 `scripts/docs/sync_agent_rules.py` 从 "
        f"`.agentx/ssot/engram_memory_recall.rule.json` 生成；"
        f"本地兜底文档：`{rule['fallback_doc']}`。\n\n"
        "触发场景：\n\n"
        f"{trigger_lines}\n\n"
        "执行顺序：\n\n"
        f"{step_lines}\n\n"
        "最低要求：\n\n"
        f"{requirement_lines}\n\n"
        f"> {doc_hint}\n"
    )


def _replace_generated_block(text: str, block_body: str) -> str:
    pattern = re.compile(
        rf"{re.escape(BEGIN_MARKER)}.*?{re.escape(END_MARKER)}",
        re.DOTALL,
    )
    replacement = (
        f"{BEGIN_MARKER}\n"
        f"{block_body.rstrip()}\n"
        f"{END_MARKER}"
    )
    if not pattern.search(text):
        raise ValueError("未找到生成标记块，请先添加 BEGIN/END 标记。")
    return pattern.sub(replacement, text, count=1)


def _sync_file(path: Path, expected: str, *, check: bool) -> tuple[bool, str]:
    if not path.exists():
        if check:
            return False, f"[FAIL] 缺少文件: {path.as_posix()}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
        return True, f"[CREATED] 已创建: {path.as_posix()}"

    current = path.read_text(encoding="utf-8")
    if current == expected:
        return True, f"[OK] 已同步: {path.as_posix()}"
    if check:
        return False, f"[FAIL] 未同步: {path.as_posix()}"
    path.write_text(expected, encoding="utf-8", newline="\n")
    return True, f"[FIXED] 已更新: {path.as_posix()}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync agent rule artifacts from .agentx SSOT.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅检查是否同步，不写入文件",
    )
    parser.add_argument(
        "--install-local-cursor-rule",
        action="store_true",
        help="同时更新本地 .cursor/rules/engram-memory-recall.mdc（默认仅更新仓库内可提交产物）",
    )
    args = parser.parse_args()

    root = _project_root()
    ssot_path = root / ".agentx" / "ssot" / "engram_memory_recall.rule.json"
    cursor_rule_repo_path = (
        root / "configs" / "agent_rules" / "engram-memory-recall.mdc"
    )
    cursor_rule_local_path = (
        root / ".cursor" / "rules" / "engram-memory-recall.mdc"
    )
    agents_path = root / "AGENTS.md"
    dev_agents_path = root / "docs" / "dev" / "agents.md"

    rule = _load_ssot(ssot_path)

    cursor_expected = _render_cursor_rule(rule)

    agents_current = agents_path.read_text(encoding="utf-8")
    agents_block = _render_agents_block(
        rule,
        heading="### Codex 记忆召回规则（CI / 迭代 / Evidence）",
        doc_hint="多 IDE/CLI 场景都以本 SSOT 渲染结果为准，禁止在各端单独维护副本。",
    )
    agents_expected = _replace_generated_block(agents_current, agents_block)

    dev_agents_current = dev_agents_path.read_text(encoding="utf-8")
    dev_agents_block = _render_agents_block(
        rule,
        heading="### Codex 对应规则：CI / 迭代记忆召回",
        doc_hint="如果需要调整规则，请修改 SSOT 后统一生成，不要直接改本段。",
    )
    dev_agents_expected = _replace_generated_block(dev_agents_current, dev_agents_block)

    ok = True
    messages: list[str] = []

    sync_targets: list[tuple[Path, str]] = [
        (cursor_rule_repo_path, cursor_expected),
        (agents_path, agents_expected),
        (dev_agents_path, dev_agents_expected),
    ]
    if args.install_local_cursor_rule:
        sync_targets.append((cursor_rule_local_path, cursor_expected))

    for path, expected in sync_targets:
        item_ok, message = _sync_file(path, expected, check=args.check)
        ok = ok and item_ok
        messages.append(message)

    for message in messages:
        print(message)

    if not ok and args.check:
        print(
            "[HINT] 运行 `python scripts/docs/sync_agent_rules.py` 进行自动修复。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
