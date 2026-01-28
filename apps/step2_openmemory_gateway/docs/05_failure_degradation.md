# 失败降级与补偿（Step2）

目标：OpenMemory 或网络异常时，不阻塞主流程且不丢沉淀。

## 写入失败
1. Gateway 生成记忆卡片（payload_md）
2. 调用 OpenMemory 写入失败：
   - 写 Step1：logbook.outbox_memory(status=pending, last_error=...)
   - 写 Step1：governance.write_audit(action=redirect/reject, reason=error)
3. 定时任务或下一次工具启动时 flush outbox：
   - 成功：标记 sent
   - 失败超过阈值：标记 dead 并报警（可选）

## 读取失败
- 退回 Step1：用 analysis.knowledge_candidates 最近结果 + logbook.events 做关键词检索（或 FTS 方案）
- 输出"degraded 模式"标记，提示 Agent 降低依赖历史记忆

## Step1/Step2 边界与数据流

### 降级场景中的接口依赖

Step2 Gateway 在失败降级场景下，依赖以下 Step1 原语接口：

| 序号 | 接口名称 | 降级场景用途 |
|------|----------|--------------|
| 1 | `get_or_create_settings` | 读取治理设置，判断写入权限与策略 |
| 2 | `upsert_settings` | 管理员更新治理策略 |
| 3 | `insert_write_audit` | 记录写入操作审计（含 redirect/reject 原因） |
| 4 | `outbox_enqueue` | OpenMemory 写入失败时，将记忆入队等待重试 |
| 5 | `outbox_claim_lease` | 后台任务获取待重试的记忆（支持并发租约） |
| 6 | `outbox_ack_sent` | 重试成功后确认发送完成 |
| 7 | `outbox_fail_retry` | 重试失败，更新计数与下次尝试时间 |
| 8 | `outbox_mark_dead` | 超限后标记死信，可触发告警 |

### 降级数据流

```
写入失败降级:
Gateway → OpenMemory (失败)
       ↓
       → Step1.insert_write_audit(action=redirect, reason=error)
       → Step1.outbox_enqueue(payload_md, target_space)

重试流程:
Scheduler → Step1.outbox_claim_lease()
         ↓
         → OpenMemory.write()
         ├── 成功 → Step1.outbox_ack_sent()
         └── 失败 → Step1.outbox_fail_retry()
                 └── 超限 → Step1.outbox_mark_dead() + 告警

读取失败降级:
Gateway → OpenMemory (失败)
       ↓
       → Step1.analysis.knowledge_candidates (关键词检索)
       → 返回 {degraded: true, results: [...]}
```

### 接口实现位置

这些原语接口由 `engram_step1` 包提供，Step2 通过 `step1_adapter.py` 适配层调用：

```python
from gateway.step1_adapter import Step1Adapter, get_adapter

adapter = get_adapter()
adapter.get_or_create_settings(project_key)
adapter.insert_audit(...)
adapter.enqueue_outbox(...)
```

---

## SCM 同步侧降级与 Gateway/Outbox 交互边界

本节说明 SCM 同步过程中的降级场景，以及与 Gateway/Outbox 的交互边界。

### 职责边界划分

| 组件 | 职责范围 | 降级处理方式 |
|------|----------|--------------|
| **SCM Sync 工具** | 同步 SVN/Git/GitLab 数据到 Step1 | 写入 `scm.*` 表，失败时自行重试或中止 |
| **Gateway** | 将 Step1 事实转换为记忆卡片，写入 OpenMemory | 失败时使用 `logbook.outbox` 缓冲 |
| **Outbox** | 缓冲待发送记忆，支持异步重试 | 由 Scheduler 消费，超限转死信 |

**核心原则：SCM Sync 不直接与 Outbox 交互**

```
SCM Sync 工具                    Gateway                      OpenMemory
     |                              |                              |
     |-- 写入 scm.* 表 ------------>|                              |
     |   (svn_revisions,            |                              |
     |    git_commits, etc.)        |                              |
     |                              |                              |
     |<---- 同步完成/失败 ----------|                              |
     |   (返回统计，不涉及 outbox)   |                              |
     |                              |                              |
     |                              |-- 转换为记忆卡片 ------------>|
     |                              |   (从 scm.* 读取)             |
     |                              |                              |
     |                              |<---- 写入失败 ----------------|
     |                              |                              |
     |                              |-- 入队 outbox --------------->|
     |                              |   (payload_md, target)       [Step1]
```

### SCM 同步失败场景

SCM 同步工具在以下场景可能失败：

| 失败类型 | 影响范围 | 处理方式 | 与 Gateway 交互 |
|----------|----------|----------|-----------------|
| **网络超时** | 单次 API 调用 | 内部重试 3 次后中止 | 无 |
| **认证失败** | 整个同步任务 | 立即中止，返回错误 | 无 |
| **数据库写入失败** | 单条或批量记录 | 取决于 strict/best_effort 模式 | 无 |
| **Diff 拉取失败** | 单个 patch_blob | 记录空 URI，后续 materialize | 无 |

**SCM Sync 失败不触发 Outbox**

SCM Sync 的降级处理完全在 Step1 层面完成：
- 失败时**不更新 watermark**，下次同步自动重试
- 使用 `best_effort` 模式时跳过错误记录继续
- 所有状态存储在 `scm.*` 表和 `logbook.kv`

### Gateway 消费 SCM 数据的降级

Gateway 可能异步消费 `scm.*` 表数据生成记忆卡片。此过程的降级遵循标准 Gateway 降级流程：

```
[定时/触发] Gateway 检测新 SCM 事件
     |
     v
读取 scm.git_commits / scm.svn_revisions (新增记录)
     |
     v
生成记忆卡片 (payload_md)
     |
     v
调用 OpenMemory 写入
     |
     +-- 成功 --> 更新消费 cursor
     |
     +-- 失败 --> 标准降级流程:
                  |
                  +-- insert_write_audit(action=redirect)
                  +-- outbox_enqueue(payload_md)
```

### 交互边界约束

1. **SCM Sync 工具不感知 Outbox**
   - SCM Sync 只负责将数据写入 `scm.*` 表
   - 不关心数据是否被 Gateway 消费或转换为记忆

2. **Gateway 独立消费 SCM 数据**
   - Gateway 维护自己的消费 cursor（可存储在 `logbook.kv`）
   - SCM 数据的消费与 SCM 同步解耦

3. **Outbox 只服务于 Gateway → OpenMemory 链路**
   - Outbox 用于缓冲 Gateway 无法写入 OpenMemory 的记忆
   - 不用于 SCM 同步的重试（SCM 有自己的 watermark 机制）

4. **错误日志分离**
   - SCM Sync 错误：写入 `logbook.events` 或独立日志
   - Gateway 降级错误：写入 `governance.write_audit`

### 消费 Cursor 与 SCM Watermark 的区别

| 概念 | 存储位置 | 用途 | 更新时机 |
|------|----------|------|----------|
| **SCM Watermark** | `logbook.kv (scm.sync/*)` | 标记 SCM 同步进度 | SCM Sync 完成时 |
| **Gateway 消费 Cursor** | `logbook.kv (gateway.consume/*)` | 标记 Gateway 消费进度 | Gateway 处理完成时 |

两者独立维护，互不影响：
- SCM Sync 更新 `svn_cursor:1` → `{"last_rev": 1000}`
- Gateway 消费后更新 `scm_consume:1` → `{"last_processed_rev": 1000}`

这种设计允许：
- SCM 同步可以快速推进，不等待 Gateway 处理
- Gateway 可以独立重放历史 SCM 数据
- 两个组件的失败不互相阻塞
