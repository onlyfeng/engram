# Cursor MCP Config Template v2

本文件定义 Cursor MCP 配置模板（V2）的结构约束与使用规范。

## SSOT 与 Schema

- SSOT（权威配置示例）：`configs/mcp/.mcp.json.example`
- Schema：`schemas/cursor_mcp_config_template_v2.schema.json`

> **说明**：文档中的配置片段通过 SSOT 渲染生成，详见 `docs/dev/mcp_config_ssot_invariants.md`。

## 配置结构

顶层对象仅包含 `mcpServers`（必须），可选 `_comment` 和 `$schema` 用于说明与校验。

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mcpServers` | object | 是 | MCP Server 映射表，key 为 server 名称 |
| `type` | string | 是 | 传输类型，当前仅支持 `"http"` |
| `url` | string | 是 | MCP 端点 URL（必须以 `/mcp` 结尾） |
| `headers` | object | 否 | 追加请求头（如 `Authorization`） |

## 示例（最小配置）

示例配置请参见：

- [Cursor MCP 集成指南](../gateway/02_mcp_integration_cursor.md)（SSOT 受控块）

## 版本策略

- V2 保持字段语义稳定，仅新增可选字段。
- 若出现破坏性变更，将发布 V3 并提供迁移说明。
