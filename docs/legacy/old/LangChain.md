下面给你一套**可直接落地**的工程方案：以“**Skills（目录化能力包）+ Router（入口分流）+ Subagents（大上下文专家隔离/并行）+ 状态机（handoffs 语义的阶段约束）**”为主干，默认先跑通单智能体+工具，再按瓶颈渐进升级。该方案严格对齐 LangChain 文章中对四类模式的定义与取舍（Subagents / Skills / Handoffs / Router）。([blog.langchain.com][1])

---

## 1) 目标与落地原则

### 你要解决的两类“必然会爆”的工程问题

* **上下文管理**：能力越多、规范越多，塞进同一个 prompt 会膨胀；需要“按需披露/隔离上下文”。([blog.langchain.com][1])
* **分布式开发**：不同小组维护不同能力，需要清晰边界与所有权，避免“一个巨 prompt 谁都不敢改”。([blog.langchain.com][1])

### 落地原则（强制）

1. **把上下文从对话里移出去**：运行记录、证据链、复盘知识落到 `artifacts/` + `SQLite`；会话只保留指针/摘要。
2. **一切能力先做成 Skill**：目录化、可 CODEOWNERS、可版本化；Router/工作流只依赖 skill 元数据与 schema（契约）。
3. **需要强隔离/并行时再上 Subagents**：Subagents 无状态，主控汇总（避免污染与 token bloat）。([blog.langchain.com][1])
4. **需要阶段约束时再上 Handoffs/状态机**：把“收集→执行→验证→提交”做成可追踪状态流。([blog.langchain.com][1])

---

## 2) 目录结构（可直接拷贝到仓库根目录）

```text
.agentx/
  workflows/                      # K0：工作流入口（状态机/步骤编排）
skills/                           # 技能包（目录化能力）
router/                           # 路由分流（选择 workflow / skill / subagent）
agents/
  subagents/                      # 子代理（大上下文专家域）
memory/                           # SQLite schema & 适配
knowledge/                        # K1：知识沉淀（规范、复盘、FAQ）
artifacts/                        # 每次运行的证据链产物（run_id 分目录）
scripts/                          # bootstrap / runner / 工具胶水
docs/                             # 架构、维护规范、扩展指南
```

这套结构对应文中“Skills 是目录化的提示词/脚本/资源包，按需加载”；Router 用来避免把所有技能塞进一个 prompt；Subagents 用于隔离大上下文域并并行。([blog.langchain.com][1])

---

## 3) Skill 规范（团队协作的最小单元）

每个技能目录固定如下（**强制**）：

```text
skills/<skill_name>/
  skill.yaml                       # 元数据（Router/工作流只看它）
  prompt.md                        # 给 Cursor Agent/LLM 的“完整指令”
  schemas/
    input.schema.json
    output.schema.json
  tools/                           # 确定性脚本（bash/python）
  resources/                        # 更深层资料、例子、常见失败
```

### 3.1 skill.yaml（模板）

```yaml
name: patch_review
version: 1.0.0
owner: client-team
kind: skill
summary: "对 patch/diff 做风险点与冲突点分析，产出审查清单"
entrypoint: ""   # 分析类技能可为空；执行类技能写命令，例如：bash tools/run.sh
interfaces:
  input_schema: "schemas/input.schema.json"
  output_schema: "schemas/output.schema.json"
```

> 这就是“Skills：渐进披露”的工程化落点：启动时只暴露 name/summary，Router 决定是否加载 prompt/resources。([blog.langchain.com][1])

---

## 4) Router（入口分流）规范

### 4.1 routes.yaml（人工可维护 + 可被 LLM 分类器使用）

```yaml
version: 1
routes:
  - id: svn_merge_patchflow
    kind: workflow
    description: "SVN 合并补丁流：导出→评审→应用→验证→生成提交信息（人工确认后再提交）"
    match:
      any_keywords: ["svn", "合并", "补丁", "patch", "diff"]
      confidence: 0.7

  - id: performance_triage
    kind: subagent
    description: "性能/渲染专家域"
    match:
      any_keywords: ["帧率", "卡顿", "发热", "性能", "cpu", "gpu", "profiler"]
      confidence: 0.75

  - id: patch_review
    kind: skill
    description: "补丁评审"
    match:
      any_keywords: ["review", "评审", "风险", "冲突"]
      confidence: 0.65

fallback:
  kind: dialogue
  description: "默认：通用对话/澄清问题"
```

### 4.2 router_prompt.md（LLM 分类输出严格 JSON）

```json
{
  "kind": "workflow | skill | subagent | dialogue",
  "id": "routes.yaml 的 id（或 dialogue）",
  "confidence": 0.0,
  "reason": "一句话理由",
  "required_params": { "k": "能推断则填" },
  "suggested_next_question": "缺参则给一个最关键问题，否则空字符串"
}
```

Router 的角色与取舍与文中一致：先分类/分解，再并行派发并综合；常见做法是把 Router 包成“工具节点”挂在一个有状态主会话之下。([blog.langchain.com][1])

---

## 5) Subagents（大上下文专家域）规范

Subagents 采用“**主控（Supervisor）调用子代理如同工具**”的模式：子代理无状态，主控汇总，强隔离，可并行。([blog.langchain.com][1])

目录模板：

```text
agents/subagents/perf_expert/
  system.md              # 子代理系统指令（专注性能域）
  interfaces.json        # 输入输出 schema（JSON）
  playbook.md            # 该域排查路径与证据清单
```

**子代理输出必须结构化**（JSON），建议字段：

* `summary`：一句话结论
* `hypotheses[]`：假设列表（证据、置信度、下一步）
* `commands[]`：建议命令（可直接复制执行）
* `artifacts_needed[]`：需要收集哪些日志/trace

---

## 6) 状态机工作流（handoffs 语义的落地）

工作流负责“阶段解锁、顺序、人工确认点”。这对应文中 Handoffs：状态驱动的 agent/阶段切换，适合分阶段对话与顺序约束。([blog.langchain.com][1])

### 6.1 workflow.yaml（示例：SVN 合并补丁流）

```yaml
version: 1
id: svn_merge_patchflow
name: SVN 合并补丁流（示例）
params:
  a_url: { type: string, required: true }
  b_wc:  { type: string, required: true }
  revs:  { type: string, required: true }
  patch_dir: { type: string, required: false, default: "artifacts/${run_id}/patches" }

steps:
  - id: export_patch
    kind: skill
    ref: skills/svn_export_patch
    inputs: { a_url: ${a_url}, revs: ${revs}, out_dir: ${patch_dir} }

  - id: review_patch
    kind: skill
    ref: skills/patch_review
    inputs: { patch_dir: ${patch_dir}, b_wc: ${b_wc} }

  - id: apply_patch
    kind: skill
    ref: skills/apply_patch
    inputs: { patch_dir: ${patch_dir}, b_wc: ${b_wc} }

  - id: build_verify
    kind: skill
    ref: skills/build_verify
    inputs: { b_wc: ${b_wc} }

  - id: commit_log
    kind: skill
    ref: skills/commit_log
    inputs: { patch_dir: ${patch_dir}, b_wc: ${b_wc} }

  - id: human_confirm
    kind: gate
    message: "人工确认点：检查 report.md 与工作副本状态，yes 继续 / no 终止"

outputs:
  - artifacts/${run_id}/report.md
  - artifacts/${run_id}/run.json
  - artifacts/${run_id}/patches/
```

### 6.2 state_machine.yaml（示例）

```yaml
workflow_id: svn_merge_patchflow
states:
  - id: init
    transitions: [{ to: exported, when: step_success:export_patch }]

  - id: exported
    transitions: [{ to: reviewed, when: step_success:review_patch }]

  - id: reviewed
    transitions: [{ to: applied, when: step_success:apply_patch }]

  - id: applied
    transitions: [{ to: verified, when: step_success:build_verify }]

  - id: verified
    transitions: [{ to: ready_to_commit, when: step_success:commit_log }]

  - id: ready_to_commit
    transitions:
      - { to: done, when: gate_yes:human_confirm }
      - { to: aborted, when: gate_no:human_confirm }
```

---

## 7) 证据链与外置记忆（SQLite）

**强制**每次运行生成 `run_id`，所有产物落到 `artifacts/<run_id>/...`，并把关键索引写入 SQLite：

* `runs(run_id, workflow_id, params, status, started_at, ended_at)`
* `steps(run_id, step_id, status, message, details_json)`
* `artifacts(run_id, path, kind, meta_json)`

这能有效降低“Skills 加载导致历史累积”的 token bloat 风险（把大量细节移到 artifacts/DB，仅在需要时按指针拉取）。该权衡与文中对 Skills 的 tradeoff 描述一致。([blog.langchain.com][1])

---

## 8) 最小可运行 Runner（CLI 方式）

你可以用一个极简 runner 做到：

* 读取 workflow.yaml
* 渲染 `${run_id}` 等变量
* 对“执行类 skill”调用 `entrypoint`
* 对“分析类 skill（无 entrypoint）”输出指令：让 Cursor Agent @引用 `prompt.md` 在 artifacts 下产出结果
* gate 节点强制人工确认

实现语言建议 Python（PyYAML + jsonschema），但也可替换为你们内部的任务编排器。

---

## 9) 你现在可以立刻做的落地步骤（不需要额外澄清）

1. 先把上述目录结构建起来，并把你们最常用的 3 个能力做成 skills：

   * 例如：`patch_review`（分析类，无 entrypoint）
   * `apply_patch`（执行类，有 entrypoint）
   * `build_verify`（执行类，有 entrypoint，build_cmd 可配置）

2. 写一个 `router/routes.yaml`，先用关键词/规则跑通；后续再用 LLM 分类替换规则即可（输出仍是同一 JSON 契约）。

3. 选 1 条“必须有人工确认”的流程做成工作流（例如合并/发布/权限操作），把 gate 放在最后一步。

4. 将“复盘知识、规范、FAQ”沉淀到 `knowledge/`，让技能 prompt 只引用它们的路径，不直接粘贴全部内容。

---

## 我已完成与未完成说明（为了避免你踩坑）

* 上面给出的**结构、契约、Router、工作流/状态机模板**是完整可落地的，并与 LangChain 对四类模式的定义一致。([blog.langchain.com][1])
* 由于本轮消息长度限制，我没有在正文里把每个示例 skill（如 `build_verify` / `commit_log`）的脚本与 schema 全量贴出；但你按本方案的“Skill 规范 + entrypoint 规则”补齐即可。

如果你希望我把**“一套完整样例（含 5 个技能 + runner + SQLite schema）”**以“可直接复制”的形式一次性贴全（或按文件逐个输出），我可以在你下一条消息里直接给出。

[1]: https://www.blog.langchain.com/choosing-the-right-multi-agent-architecture/ "Choosing the Right Multi-Agent Architecture"
