下面给你一份 **Step2（用 OpenMemory 替代 memU 的落地方案）**，目标是把“记录/查询/复用信息”的能力从 `grep + edit + csv/md` 升级为 **可被 Cursor Agent 直接调用的长期记忆工具**，同时保持你前面 Step1（SQLite 事件/状态账本）作为可审计的“事实来源”。

---

## Step2 总体定位（你要的“赋能 Agent 记录能力”）

* **Step1（SQLite）**：记录“可审计、可回放”的结构化事实（事件日志、状态机、产物索引、失败原因、命令执行结果）。
* **Step2（OpenMemory）**：记录“可召回、可演化”的长期记忆（偏好/约束、流程要点、踩坑规律、决策理由、复用 SOP），并提供更强的召回机制（多“脑区”记忆、时间推理、衰减/强化、可解释召回）。([GitHub][1])

OpenMemory 本身支持 **本地优先（SQLite / Postgres）**、Python/Node SDK、以及 **MCP**（Claude/Cursor/Windsurf 可以把它当工具直接调用）。([GitHub][1])

---

## Step2-A（推荐）：用 OpenMemory Backend + MCP，把“记忆”变成 Cursor 的工具

这是最贴合你“让 Cursor Agent 高效记录/查询”的路径：**Cursor 不需要改模型、不需要你手工写入文件**，只要能调用 MCP 工具即可。

### A1. 启动 OpenMemory 后端（本地开发方式）

OpenMemory README 给的本地启动方式（backend 默认 `:8080`，并暴露 `/mcp`）：([GitHub][1])

```bash
git clone https://github.com/CaviraOSS/OpenMemory.git
cd OpenMemory
cp .env.example .env

cd backend
npm install
npm run dev   # default :8080
```

也可以用 Docker：([GitHub][1])

```bash
docker compose up --build -d
```

启动后关键入口：

* `/mcp`：给 Cursor/Claude 用的 MCP Server
* `/api/memory/*`：记忆 CRUD
* dashboard UI：可视化查看与调试([GitHub][1])

---

### A2. 在你的项目根目录接入 Cursor MCP（.mcp.json）

把 OpenMemory 作为 MCP 工具接入 Cursor（README 给了标准配置）：([GitHub][1])

```json
{
  "mcpServers": {
    "openmemory": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

接入成功后，Cursor Agent 将能直接调用这些工具：([GitHub][1])

* `openmemory_store`
* `openmemory_query`
* `openmemory_list`
* `openmemory_get`
* `openmemory_reinforce`

---

### A3. 给 Cursor Agent 的“写入/读取契约”（最关键）

你之前 memU Step2 关注的就是“接入点 + 契约 + 数据裁剪 + 失败降级”。这里直接落到 **工具调用规范**，让 Agent 行为稳定可控。

#### 1) 写入契约（什么时候写、写什么、写多长）

**写入时机（建议强制）**

* 每次完成一个“可复用工作单元”后写入：

  * 完成一次流程（例如一次 SVN 合并/同步任务）
  * 解决一个典型故障（例如合并冲突模式、某类脚本报错）
  * 固化一个 SOP（例如“如何生成补丁清单+回放验证”）
  * 形成一条“约束/偏好”（例如目录规范、命名规则、验证门槛）

**写入内容结构（建议用“记忆卡片”格式，纯文本即可）**

* 控制长度：**200–600 字**优先（大于 600 字先裁剪）
* 不要写入：大段 diff、完整日志、长堆栈（只存“指针 + 摘要 + 关键哈希/ID”）

推荐模板（你可以让 Agent 固定产出这个格式再调用 `openmemory_store`）：

```text
[Kind] PROCEDURE | FACT | PITFALL | DECISION
[Scope] user:<你> repo:<仓库/项目> workflow:<工作流名>
[When] 2026-01-21
[Summary] 一句话结论（可检索）
[Details] 3-5 条要点（含关键命令/规则/边界条件）
[Evidence] event_id=<Step1 sqlite> path=<产物路径> hash=<可选>
[Confidence] high|mid|low
[TTL] long|mid|short
```

> 说明：OpenMemory 支持多“脑区”记忆、衰减/强化、时间推理，你用这种半结构化文本能显著提升可检索性与复用性。([GitHub][1])

---

#### 2) 读取契约（什么时候查、怎么查、查多少）

**查询时机（建议强制）**

* Agent 开始执行一个工作流前，先 `openmemory_query`：

  * query 用：`<工作流名> + <本次关键变量>`（例如 “svn merge patch apply 冲突”）
* 遇到失败/不确定决策点时，再 query 一次：

  * query 用：`错误关键词 + 工具名 + 平台`（例如 “patch fuzz failed svn windows”）

**召回条数建议**

* 默认取 Top 5，超出让 Agent 归纳后再行动（避免上下文膨胀）

---

### A4. 数据裁剪策略（防止“记忆污染”与“可用性下降”）

你之前担心的 csv/md 方案弊端，本质就是：

* 重复、噪声、不可检索
* 太长、不可复用
* 没有衰减与强化，记忆会“越写越烂”

OpenMemory 内建 **衰减/强化** 与更丰富的记忆模型；你这边要做的是“输入治理”：([GitHub][1])

**裁剪规则（建议硬编码为 Agent 规则）**

1. 单条记忆 ≤ 600 字，超过则只保留：结论 + 关键步骤 + 指针
2. 日志/patch/diff 永远不入记忆，只入 Step1 SQLite 或产物目录（OpenMemory 只存引用）
3. 同一类坑/规则重复出现：不要 store 新条，改为 `openmemory_reinforce`（强化已有记忆）([GitHub][1])
4. “短期波动信息”（临时分支名、一次性环境问题）标记 TTL=short，避免长期污染

---

### A5. 失败降级（OpenMemory 不可用时怎么办）

给你一个可落地的分层降级（不影响工作流推进）：

* **L0（正常）**：Cursor 直接 MCP 调用 `openmemory_*`
* **L1（OpenMemory 后端挂了）**：

  * 仍然写 Step1 SQLite（事件账本照常）
  * 同时把“待写入记忆卡片”追加到 `memory_outbox`（Step1 的一张表/队列）
* **L2（恢复后补偿）**：

  * 提供一个脚本把 `memory_outbox` 批量 flush 到 OpenMemory（成功后标记 done）
* **L3（极端兜底）**：

  * 仅追加 `fallback.ndjson`（纯追加，不做编辑），保证信息不丢；后续人工/脚本再回灌

这样你不会再退回 `grep + edit` 的脆弱模式。

---

## Step2-B：进程内 Python（openmemory-py）集成，用于“脚本侧”写/查记忆

如果你有一些自动化脚本（比如 Step1 的记录器、CI 里做摘要），可以在脚本里直接用 Python SDK。

OpenMemory README 的 Python 用法与注意点：`add/search/get/delete` 是 async。([GitHub][1])

```python
from openmemory.client import Memory

mem = Memory()
mem.add("user prefers dark mode", user_id="u1")
results = mem.search("preferences", user_id="u1")
# await mem.delete("memory_id")  # async
```

它也提供 OpenAI client 的“注册包装”，方便你在脚本里把对话/调用串起来记录（这不等于 Cursor 模型，而是你脚本自己调用 OpenAI 时可用）：([GitHub][1])

```python
mem = Memory()
client = mem.openai.register(OpenAI(), user_id="u1")
resp = client.chat.completions.create(...)
```

> Cursor 侧的最佳接入仍然是 MCP（因为 Cursor 的模型调用不在你脚本进程里）。Python 集成更适合“流水线/脚本/CI 摘要回灌”。

---

## 你关心的“为什么 OpenMemory 更适合 Step2”

相对你现在的 csv/md 维护方式，OpenMemory 提供的是“记忆系统”而不只是“存储”：

* 多类型记忆（事件/事实/流程/情绪/反思）
* 时间推理（什么在什么时间段为真）
* 衰减与强化（避免永久污染）
* 可解释召回（为什么被召回）
* 本地优先（SQLite/Postgres），MCP 直接给 IDE 用([GitHub][1])

许可协议为 Apache-2.0（对企业内用与二次开发相对友好）。([GitHub][1])

---

## 与 Step3（seekdb / RAG）的“混合推荐”（给你留好接口）

你后续要上 seekdb（更像“可检索知识库/代码库 RAG”），建议职责切分：

* **OpenMemory（Step2）**：存“经验/决策/流程规律/约束”，以及“seekdb 查询入口策略”
* **seekdb（Step3）**：存“可引用事实材料”（代码片段、规范文档、patch 产物、测试报告），并返回可验证证据

混合方式很简单：OpenMemory 记忆里只存指针：

* `seekdb_doc_id=...`
* `path=...`
* `commit=...`
* `event_id=<Step1>`
  这样 Agent 召回“该查什么”，再去 seekdb 拉“证据”。

---

## 3–5 个对齐问题（不影响你先做，但决定上限）

1. 你希望 Step2 的记忆 **scope** 怎么划分：按“个人 / 仓库 / 团队（多人共享）”？（这决定用 SDK 还是 server 为主）
2. 你更在意记忆的 **可控性** 还是 **自动化程度**：是“Agent 必须按模板写入”，还是“允许自由文本但有清洗器”？
3. 你能接受记忆写入失败时的行为：是“任务继续但进入 outbox”，还是“关键工作流必须阻塞提示”？
4. 你希望哪些信息绝不入记忆（例如分支名、内部路径、账号 token、项目代号）以便做输入过滤？

如果你认可上面 Step2-A 的方向，我可以把它进一步固化成你仓库里的 **规则文件（Agent 运行时强制执行）+ outbox 回灌脚本（Python）** 两个最小交付件，确保 Cursor Agent 的写/查行为稳定可持续。

[1]: https://github.com/CaviraOSS/OpenMemory "GitHub - CaviraOSS/OpenMemory: Local persistent memory store for LLM applications including claude desktop, github copilot, codex, antigravity, etc."
