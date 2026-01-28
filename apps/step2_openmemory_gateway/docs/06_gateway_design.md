# Memory Gateway（MCP）设计

## 目标
- Cursor 只连 Gateway（/mcp）
- Gateway 负责：
  1) team_write_enabled + policy 校验
  2) 写入裁剪（长度/证据链/去重）
  3) 写入审计（governance.write_audit）
  4) 失败降级（logbook.outbox_memory）
  5) promotion：个人/团队/公共提升队列（可选）

## 对外暴露的 MCP 工具（建议）
- memory_store(payload_md, target_space?, meta_json?) -> {action, space_written, memory_id?, evidence_refs}
- memory_query(query, spaces=[...], filters={owner,module,kind}, topk=...) -> results
- memory_promote(candidate_id, to_space, reason) -> promo_id
- memory_reinforce(memory_id, delta_md) -> ok

## 关键治理逻辑（写入）
- 默认 target_space = team:<project>
- 若 team_write_enabled=false：redirect -> private:<actor>
- 若策略不满足：redirect -> private:<actor> 或 reject（建议优先 redirect）

## Step1/Step2 边界与数据流

### 架构边界
- **Step1 (engram_step1)**: 本地 PostgreSQL 数据库层，负责治理设置、审计日志、失败补偿队列（outbox）
- **Step2 (Gateway)**: MCP 网关层，负责策略校验、写入裁剪、与 OpenMemory 的交互
- **数据流向**: Cursor → Gateway(Step2) → OpenMemory + Step1(持久化/降级)

### Step2 依赖 Step1 的原语接口

| 序号 | 接口名称 | 用途说明 | 对应表/Schema |
|------|----------|----------|---------------|
| 1 | `get_or_create_settings` | 获取或创建项目的治理设置（team_write_enabled, policy_json） | governance.settings |
| 2 | `upsert_settings` | 更新治理设置（管理员操作） | governance.settings |
| 3 | `insert_write_audit` | 记录写入审计日志（action: allow/redirect/reject） | governance.write_audit |
| 4 | `outbox_enqueue` | 将失败的记忆写入请求入队到 outbox 补偿队列 | logbook.outbox_memory |
| 5 | `outbox_claim_lease` | 获取待处理的 outbox 记录（支持并发安全的租约机制） | logbook.outbox_memory |
| 6 | `outbox_ack_sent` | 确认 outbox 记录已成功发送到 OpenMemory | logbook.outbox_memory |
| 7 | `outbox_fail_retry` | 标记 outbox 发送失败，增加重试计数（指数退避） | logbook.outbox_memory |
| 8 | `outbox_mark_dead` | 超过重试阈值后标记为死信，触发报警 | logbook.outbox_memory |

### 可选扩展接口

| 接口名称 | 用途说明 |
|----------|----------|
| `outbox_dedupe_check` | 基于 payload_sha 的去重检查，避免重复入队 |
| `get_degraded_memories` | 读取降级时从 logbook 获取最近知识候选 |

### 数据流示意

```
┌─────────┐    memory_store     ┌──────────────┐
│ Cursor  │ ─────────────────>  │   Gateway    │
│  (MCP)  │ <─────────────────  │   (Step2)    │
└─────────┘    response         └──────┬───────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  ▼
            ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
            │  OpenMemory  │   │   Step1 DB   │   │   Step1 DB   │
            │   (写入)     │   │ write_audit  │   │   outbox     │
            └──────────────┘   └──────────────┘   └──────────────┘
                  ↑                                      │
                  │       失败重试                        │
                  └──────────────────────────────────────┘
```
