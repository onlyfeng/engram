([Past chat][1])([Past chat][1])([Past chat][1])([Past chat][1])([Past chat][2])([Past chat][1])([Past chat][1])([Past chat][1])([Past chat][2])([Past chat][1])([Past chat][3])([Past chat][3])([Past chat][3])([Past chat][3])([Past chat][3])([Past chat][1])([Past chat][1])([Past chat][1])([Past chat][2])([Past chat][2])

下面给出你“Step 2：memU 接入点（写入/读取契约、数据裁剪策略、失败降级）”的**完整落地方案**。我会把它设计成：**在 Step 1（SQLite 事件账本/索引）之上，为 Agent 增加一层可持续演进的长期记忆能力**，且与业界 memU 的主流用法保持一致（Python 集成优先），并为后续接入 seekdb/RAG 留好接口。

---

## Step 2 的目标与边界

### 目标

1. **写入**：Agent 在执行过程中，把“可复用的信息”高效沉淀为长期记忆（而不是散落在 `manifest.csv/index.md` 里靠 grep）。
2. **读取**：Agent 在新任务开始/关键步骤前，能够按作用域（项目/工作流/用户/Agent）检索相关记忆，注入上下文。
3. **裁剪**：控制成本与噪声，避免把日志、diff、长输出无脑塞进记忆系统。
4. **降级**：memU 不可用时，系统仍可工作（至少退回 SQLite 的检索能力），并可事后补写。

### 非目标

* 不把 memU 绑定到“SVN merge”这个单一用例；SVN merge 只是你现有的最佳验证场景。

---

## memU 能力与接入形态选择

memU 的核心特征是：将输入（对话、文档、日志等）抽取为 **Resource → Item → Category** 三层结构，并支持两种检索：**Embedding/RAG**（快）与 **LLM 文件直读**（深）。([PyPI][4])

你在工程上有两种落地形态：

### 形态 A（推荐先做）：进程内 Python 集成 memu-py

* 直接在你的工具脚本/Agent 工具层调用 `memorize()` / `retrieve()`。
* 可先用 In-Memory/本地存储做 PoC，再切 PostgreSQL + pgvector。([PyPI][4])
  适合你“偏向 Python 集成、低摩擦落地”的要求。

### 形态 B（团队化/可视化）：上 memU-server（可选）

* memU-server 提供 HTTP API（`POST /memorize`, `POST /retrieve`）与 Docker 快速部署。([GitHub][5])
* 可接 memU-ui 做可视化管理。([GitHub][6])
* 注意：memU-server 的许可证是 **AGPL-3.0**，内部部署与分发要评估合规。([GitHub][5])

> 你的“Step2 接入点设计”应同时兼容 A/B：先用 A 验证契约与裁剪策略，再决定是否上 B。

---

## 一、写入契约（Write Contract）

写入契约的核心是：**Agent 只写“可复用的最小充分信息”**，并且要能被 memU 的三层结构吃下去（Resource/Item/Category 自动生成）。

### 1) 写入输入类型：统一为“Episode（回合包）”

每次写入对应一个 Episode：例如一次工作流 run、一个关键阶段（如：分析/生成/验证/提交）。

**Episode = 头部元信息（可索引） + 正文内容（可抽取） + 引用（可追溯）**

#### Episode 头部（强制）

* `scope`：用于 where-filter（user_id / agent_id / project_id / workflow_id）
* `episode_id`：UUID 或 `run_id:step_id`
* `episode_type`：`decision` / `bugfix` / `postmortem` / `spec_update` / `howto` / `incident` 等
* `tags`：领域标签（e.g. `svn`, `patch`, `compat`, `performance`, `pipeline`）
* `source_refs`：指向 SQLite 的 event_id 列表（实现“可追溯闭环”）

#### Episode 正文（强制）

建议使用**结构化 Markdown**（更适合 memU 抽取与后续 LLM 直读），并包含固定小节：

* Context（背景/约束）
* Actions（做了什么）
* Decisions（关键决策与理由）
* Pitfalls（坑/失败模式）
* Fix/Recipe（可复用步骤）
* Verification（验证方式与结果）
* Links（相关文件/patch/命令/规范）

> memU 本身也支持把日志转成 `skill.md` 这类可复用 SOP，你的 Episode 结构与其思路一致。([MemU][7])

### 2) 与 memU 的对接方式（两条路）

#### 路 A：memu-py 的 `memorize()`

`memorize()` 以 `resource_url + modality` 的方式摄入（对话/文档/图片等），并可带 user 作用域。([PyPI][4])
落地建议：把 Episode Markdown 落盘到你们 `.agentx/memory/resources/...`，再把路径作为 `resource_url` 送入。

#### 路 B：memU-server 的 `POST /memorize`

memU-server README 给了一个“对话型 payload”示例：`content` 为 role/message 列表，含 `created_at`。([GitHub][5])
落地建议：把 Episode Markdown 放在一条 `role=user` 的 `content.text` 里（或拆成多条），以保持契约简单稳定。

---

## 二、读取契约（Read Contract）

读取的目标是：**把 memU 的结果变成“可控大小、可解释、可引用”的 Context Pack**，注入给 Agent。

### 1) 查询输入：QueryPack（查询包）

每次读取输入包含：

* `queries`：你要问的自然语言问题（可多条）
* `scope where`：限定检索范围（项目/工作流/用户/Agent）
* `mode`：`rag`（快）或 `llm`（深）

memu-py 的 `retrieve()` 支持：

* `queries=[{role, content:{text}}...]`
* `where={...}` 做 scope filter
* 支持两种 retrieval 方法并可返回 categories/items/resources。([PyPI][4])

memU-server 的简化形态是：

* `POST /retrieve`：`{"query": "..."}`([GitHub][5])
  如果你要 scope/filter，更建议走 memu-py 或在 server 前加一层你自己的 gateway。

### 2) 输出：Context Pack（注入包）

建议固定输出三段，且强制预算（例如总字数/总 tokens 上限）：

1. **Category 摘要**：最多 N 条（主题级概览）
2. **Item 列表**：每条一句话 + 置信/相关度 + `provenance(resource_id / episode_id)`（可追溯）
3. **Resource 引用**：仅在需要时附上关键段落（严格裁剪）

这与 memU 的“可追溯三层结构”完全对齐：Category → Item → Resource 回溯。([MemU][8])

---

## 三、数据裁剪策略（Data Clipping / Pruning）

这是 Step2 成败关键：**写入侧裁剪**解决“噪声与成本”，**读取侧裁剪**解决“上下文爆炸”。

### 1) 写入侧裁剪（最重要）

对每个 Episode，采用“三段式裁剪”：

#### (a) 规则过滤（Deterministic Filter）

直接丢弃：

* 大段可再生内容：完整 diff、二进制、重复日志、构建全量输出
* 明显无价值噪声：进度条、心跳日志、重复 stacktrace（保留第一份 + 结论）
* 敏感信息：token、账号、内部 URL（先脱敏再写）

保留并加权：

* 失败模式（error + root cause）
* 决策点（trade-off）
* 可复用步骤（recipe）
* 验证结果（how to verify）
* “下次别踩”的坑（pitfall）

#### (b) 结构压缩（Structured Summarize）

把工具输出压缩为：

* “输入/输出摘要 + 关键行 TopK + 结论”
  例如：命令输出只保留末尾错误段 + 影响范围 + 修复动作。

#### (c) 去重与合并（Dedup / Merge）

* 同一 `episode_type + tags` 在短周期内重复出现：合并为一个 Episode（追加“变体/差异点”）。
* 这是避免 memU 记忆膨胀的关键。

### 2) 读取侧裁剪（注入预算）

* 默认 `rag`：先快检索；若结果不足，再升级 `llm` 深检索（成本可控）。([PyPI][4])
* 强制输出预算：例如 Category ≤ 10 条，Item ≤ 30 条，Resource 引用 ≤ 3 段。
* “不足即停”：把缺口变成 `next_step_query` 或“建议追问”，让 Agent 继续检索，而不是一次塞满。memu-py 的返回里也有 `next_step_query` 这类概念。([PyPI][4])

---

## 四、失败降级（Failure / Degradation Strategy）

把 memU 视为“增强层”，绝不能成为工作流单点。

### 1) 写入失败：进入 SQLite Outbox（离线待投递）

当 memU 调用失败（网络/限流/Key 缺失/服务挂了）：

1. Episode 仍然落盘（resources 文件）
2. 在 SQLite 写一条 `outbox` 记录：`status=pending, retry_count, last_error`
3. 后台或下一次工作流启动时重试（指数退避 + 上限）
4. 超过上限进入 `dead_letter`，但不阻塞主流程

> 这样你永远不会丢“可复用经验”，只是延迟进入 memU。

### 2) 读取失败：退回 SQLite 检索

当 memU 不可用：

* 走 SQLite 的 **FTS5/关键词检索**（Step1 已具备）
* 产出一个简化 Context Pack：按 tag/episode_type/time 排序的最近若干条 Episode 摘要
* 并在上下文中标注“memory backend degraded”，让 Agent 调整策略（例如：更依赖当前输入与显式约束）

### 3) 熔断与健康检查（Circuit Breaker）

* 连续失败 N 次：进入熔断窗口 T 分钟，避免每步都卡 API
* 每次工作流启动时做一次轻量 health-check（memU-server 可直接探测 HTTP；进程内 memu-py 则探测依赖如 pgvector 连通性）
* 成本与稳定性会明显提升

---

## 五、与 Step1（SQLite）以及后续 seekdb 的混合方案建议

### Step1 + Step2 的分工（推荐）

* **SQLite**：事实层（事实记录、状态机、可审计、可回放、强一致）
* **memU**：经验层（从事实中抽取“可复用知识/技能/偏好/坑”，并可语义检索）([MemU][7])

### 为 Step3（seekdb/RAG）预留的接口

你后续要上 seekdb，本质是要一个“更强/更可控”的向量/检索后端。混合建议：

* memU 继续负责 **结构化与演进**（Category/Item 的生成与更新）
* seekdb 负责 **检索加速与企业级过滤/权限/多租户**
  做法：把 memU 的 Item/Category 同步到 seekdb 作为索引（异步管道即可），最终实现：
* 查询 → seekdb 粗召回（fast topK）→ memU 做深读/追溯（Category/Resource）→ 输出 Context Pack

这符合 memU “Embedding（快）+ LLM（深）”的双模设计理念，只是把 embedding 的存储/检索后端替换为你更想要的 seekdb。([MemU][8])

---

## 六、按“可执行顺序”的 Step2 落地清单（你可以直接让 Cursor Agent 按此实施）

### 0) 决策：先走形态 A（memu-py）

原因：契约与裁剪策略先跑通最关键；部署摩擦最小。memu-py 的 Quick Start 与 API 已覆盖 memorize/retrieve 主路径。([PyPI][4])

### 1) 引入目录与命名（与工作流解耦）

* `.agentx/memory/resources/`：Episode 原文（Markdown/JSON）
* `.agentx/memory/context_packs/`：读取侧产物（供调试与回放）
* SQLite 增表：

  * `episodes`（元信息）
  * `outbox`（待投递）
  * `memory_links`（episode ↔ memU resource/item/category 的映射）

### 2) 实现“写入适配器”（MemoryWriter）

输入：`(run_id, step_id, sqlite_event_ids, raw_logs, artifacts)`
输出：

1. 生成 Episode.md（按裁剪策略）
2. 写 SQLite（episodes + outbox）
3. 尝试 memU `memorize()`；成功则更新映射，失败则保持 outbox pending
   （memU 的 memorize 会产出 resource/items/categories 结构，适合做映射与追溯）([PyPI][4])

### 3) 实现“读取适配器”（MemoryReader）

输入：`(queries, scope where, mode)`
输出：Context Pack（严格预算）

* 优先 `rag`，不足升级 `llm`（可配置）([PyPI][4])
* memU 不可用 → SQLite FTS fallback

### 4) 在 Cursor 工作流中插入两个稳定 Hook

* **Pre-Run Hook**：读取（给 Agent 上下文）
* **Post-Run Hook**：写入（沉淀经验）

> 这两个 Hook 是“与具体工作流无关”的通用能力点，你未来给任何 workflow 都能复用。

### 5) 可选：部署 memU-server（团队化阶段）

当你想要：

* 多 Agent/多用户共享记忆
* 可视化管理（memU-ui）
* 统一鉴权与运维

再上 memU-server：支持 Docker 一键起服务，并提供 `/memorize`、`/retrieve` API。([GitHub][5])
但务必先评估 AGPL-3.0 合规边界。([GitHub][5])

---

如果你愿意把 Step2 做到“可复制的业界标准包”级别，我建议你下一步让 Cursor Agent 产出三样东西（完全通用，不绑定 SVN）：

1. SQLite 表结构迁移（episodes/outbox/links）
2. `memory_writer.py / memory_reader.py` 两个工具入口（CLI + 可被 workflow 调用）
3. 一份 `memory_contract.md`（写入/读取/裁剪/降级的规范）

你只要告诉我：你 Step1 的 SQLite 现在是否已经启用了 FTS5（或你计划用 datasette/sqlalchemy/duckdb 哪一套），我可以把上面“表结构与两个工具入口”的接口进一步收敛到你现有栈里，保证最小改动即可接入。

[1]: https://chatgpt.com/c/69677656-bc48-8330-9de2-29420c06555c "Agent 状态存储优化"
[2]: https://chatgpt.com/c/696d21ea-71bc-8321-95da-9026333b5ef3 "多代理架构选型"
[3]: https://chatgpt.com/c/6963c278-3c24-8320-a561-4e1f3bdaae95 "Agent 工作流与实践"
[4]: https://pypi.org/project/memu-py/0.9.0/ "memu-py · PyPI"
[5]: https://github.com/NevaMind-AI/memU-server "GitHub - NevaMind-AI/memU-server: backend wrapper for memU"
[6]: https://github.com/NevaMind-AI/memU-ui?utm_source=chatgpt.com "NevaMind-AI/memU-ui"
[7]: https://memu.pro/blog/turning-logs-into-agent-memory-and-skills "Turning Logs into Agent Memory & Skills - MemU Blog | MemU"
[8]: https://memu.pro/docs "Agent Memory System | MemU - Agentic Memory for AI"
