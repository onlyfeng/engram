# Agent Rule SSOT（多 IDE/CLI 统一）

本仓库将 Agent 规则定义收敛为 **单一来源（SSOT）**，避免 Cursor 规则、Codex 规则、文档说明重复维护产生漂移。

## SSOT 文件

- `.agentx/ssot/engram_memory_recall.rule.json`

> 该文件是规则语义的唯一真实来源；修改规则时只改这个文件。

## 生成产物

- `configs/agent_rules/engram-memory-recall.mdc`（可提交、可共享的 Cursor 规则产物）
- `AGENTS.md` 受控块（Codex/CLI 规则摘要）
- `docs/dev/agents.md` 受控块（开发指南中的对应规则）

可选本地安装产物（默认不写）：

- `.cursor/rules/engram-memory-recall.mdc`（仅本机 Cursor 使用）

## 同步命令

```bash
# 写入/更新仓库内产物
python scripts/docs/sync_agent_rules.py

# 只检查是否同步（CI/提交前推荐）
python scripts/docs/sync_agent_rules.py --check

# 额外安装本地 Cursor 规则副本
python scripts/docs/sync_agent_rules.py --install-local-cursor-rule
```

## 约束

1. 不要直接手改 `AGENTS.md` 与 `docs/dev/agents.md` 中 `BEGIN/END GENERATED` 标记块内容。
2. 不要手改 `configs/agent_rules/engram-memory-recall.mdc`。
3. 若需改规则，先改 SSOT，再运行同步脚本。
