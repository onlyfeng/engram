# 迭代操作手册（快速参考）

> 本文档为迭代工作流的快速命令参考。详细说明请参阅 [迭代文档本地草稿工作流](iteration_local_drafts.md)。

---

## 命令速查表

| 阶段 | 命令 | 说明 |
|------|------|------|
| **起草** | `make iteration-init-next` | 初始化下一可用编号的本地草稿 |
| **编辑** | 编辑 `.iteration/<N>/{plan,regression}.md` | 在本地编辑草稿内容 |
| **晋升** | `make iteration-promote N=<N>` | 将草稿晋升到 SSOT |
| **取代** | `python scripts/iteration/promote_iteration.py <N> --supersede <OLD>` | 晋升并标记旧迭代为已取代 |
| **快照** | `make iteration-snapshot N=<old>` | 将 SSOT 复制到本地只读副本 |
| **证据** | `python scripts/iteration/record_iteration_evidence.py ...` | 记录验收证据 |
| **验证** | `make check-iteration-docs` | 验证迭代文档规范 |
| **回归建议** | `make iteration-rerun-advice` | 从 PR diff 生成最小重跑建议 |
| **最小回归** | `make iteration-min-regression TYPES=cycle DRY_RUN=1` | 预览或执行最小迭代回归 |

---

## 1. 起草新迭代

### 初始化本地草稿

```bash
# 自动选择下一可用编号（推荐）
make iteration-init-next

# 或指定编号
make iteration-init N=14

# 或直接调用脚本
python scripts/iteration/init_local_iteration.py --next
python scripts/iteration/init_local_iteration.py 14
```

**输出示例**：

```
📌 自动选择下一可用编号: 14

✅ Iteration 14 本地草稿已初始化
   - .iteration/14/plan.md
   - .iteration/14/regression.md
```

---

## 2. 编辑草稿

草稿文件位于 `.iteration/<N>/` 目录（不纳入版本控制）：

```
.iteration/
├── README.md           # 目录说明
└── <N>/                # 迭代 N 草稿
    ├── plan.md         # 迭代计划
    └── regression.md   # 回归记录
```

**编辑要点**：

- 填写所有 `{PLACEHOLDER}` 占位符
- 移除模板说明区块（晋升前）
- 确保内容完整、验收门禁明确

---

## 3. 晋升到 SSOT

### 基本晋升

```bash
# 使用 Makefile 快捷命令
make iteration-promote N=14

# 或直接调用脚本
python scripts/iteration/promote_iteration.py 14

# 指定状态和日期
python scripts/iteration/promote_iteration.py 14 --date 2026-02-01 --status PARTIAL

# 预览模式（不实际执行）
python scripts/iteration/promote_iteration.py 14 --dry-run
```

### 晋升并取代旧迭代（supersede）

当新迭代替代旧迭代时，使用 `--supersede` 参数：

```bash
# 晋升 Iteration 14，同时将 Iteration 13 标记为 SUPERSEDED
python scripts/iteration/promote_iteration.py 14 --supersede 13

# 预览模式
python scripts/iteration/promote_iteration.py 14 --supersede 13 --dry-run
```

**脚本自动完成**：

1. 复制草稿文件到 `docs/acceptance/`
2. 更新 `00_acceptance_matrix.md` 索引表
3. 将旧迭代状态改为 `🔄 SUPERSEDED`
4. 在旧迭代文件顶部添加 SUPERSEDED 声明

### 晋升参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `iteration_number` | 目标迭代编号（必须） | - |
| `--date`, `-d` | 日期（YYYY-MM-DD 格式） | 今天 |
| `--status`, `-s` | 状态（PLANNING/PARTIAL/PASS/FAIL） | PLANNING |
| `--description` | 说明文字 | 自动生成 |
| `--supersede OLD_N` | 标记旧迭代 OLD_N 为已取代 | - |
| `--dry-run`, `-n` | 预览模式，不实际修改文件 | false |

---

## 4. 快照 SSOT 到本地

将已晋升的迭代复制到本地只读副本（用于阅读参考）：

```bash
# 快照 Iteration 10
make iteration-snapshot N=10

# 快照到自定义目录
make iteration-snapshot N=10 OUT=.iteration/ssot/10/

# 强制覆盖已存在的快照
make iteration-snapshot N=10 FORCE=1

# 列出 SSOT 中可用的迭代编号
python scripts/iteration/snapshot_ssot_iteration.py --list
```

**⚠️ 重要**：快照副本**不可 promote 覆盖旧编号**，仅供阅读和参考。

---

## 5. 记录验收证据

### 推荐命令调用

```bash
# ===== 推荐用法 =====

# 基本用法：自动获取当前 commit sha，输出到 canonical 文件
python scripts/iteration/record_iteration_evidence.py 13

# 指定 CI 运行 URL（推荐：便于追溯）
python scripts/iteration/record_iteration_evidence.py 13 \
  --ci-run-url https://github.com/org/repo/actions/runs/123

# 同步回归文档受控区块（min_gate_block + evidence_snippet）
python scripts/iteration/sync_iteration_regression.py 13 --write

# ===== 可选参数 =====

# 指定 commit sha（用于非 HEAD 状态）
python scripts/iteration/record_iteration_evidence.py 13 --commit abc1234

# 添加单个命令记录（NAME:COMMAND:RESULT 格式，可多次使用）
python scripts/iteration/record_iteration_evidence.py 13 \
  --add-command 'lint:make lint:PASS' \
  --add-command 'typecheck:make typecheck:PASS' \
  --add-command 'test:make test:PASS'

# 传入命令结果 JSON 字符串
python scripts/iteration/record_iteration_evidence.py 13 \
  --commands '{"make ci": {"exit_code": 0, "summary": "passed"}}'

# 从 JSON 文件读取命令结果
python scripts/iteration/record_iteration_evidence.py 13 \
  --commands-json .artifacts/acceptance-runs/run_123.json

# 添加备注说明
python scripts/iteration/record_iteration_evidence.py 13 \
  --notes "所有门禁通过，验收完成"

# 预览模式（不实际写入）
python scripts/iteration/record_iteration_evidence.py 13 --dry-run
```

### 输出文件命名

| 命名类型 | 文件名格式 | 说明 |
|----------|-----------|------|
| **Canonical（推荐）** | `iteration_<N>_evidence.json` | 固定文件名，每次覆盖 |
| Snapshot（可选） | `iteration_<N>_<YYYYMMDD_HHMMSS>.json` | 带时间戳，用于历史记录 |
| Snapshot+SHA（可选） | `iteration_<N>_<YYYYMMDD_HHMMSS>_<sha7>.json` | 带时间戳和 commit SHA |

**输出位置**：`docs/acceptance/evidence/iteration_<N>_evidence.json`

脚本默认使用 canonical 命名策略，生成固定文件名 `iteration_<N>_evidence.json`，每次执行会覆盖同一文件。

### 在 regression 文档中引用证据

在 `iteration_<N>_regression.md` 的末尾添加"验收证据"段落。

**推荐方式**：使用脚本同步受控区块（读取 evidence JSON 的 commands，禁止手动编辑内容或 marker）：

```bash
# 预览同步结果
python scripts/iteration/sync_iteration_regression.py <N>

# 写入同步（更新 min_gate_block + evidence_snippet）
python scripts/iteration/sync_iteration_regression.py <N> --write
```

**生成后的段落示例**：

```markdown
## 验收证据

<!-- AUTO-GENERATED EVIDENCE BLOCK START -->
<!-- 此段落由脚本自动生成/受控，禁止手动编辑内容或 marker -->

| 项目 | 值 |
|------|-----|
| **证据文件** | [`iteration_13_evidence.json`](evidence/iteration_13_evidence.json) |
| **Schema 版本** | `iteration_evidence_v2.schema.json` |
| **记录时间** | 2026-02-02T14:30:22Z |
| **Commit SHA** | `abc1234` |

### 门禁命令执行摘要

| 命令 | 结果 | 耗时 | 摘要 |
|------|------|------|------|
| `make ci` | PASS | 45s | All checks passed |

### 整体验收结果

- **结果**: PASS
- **说明**: 所有门禁通过

<!-- AUTO-GENERATED EVIDENCE BLOCK END -->
```

**引用规范**：
- 使用相对路径 `evidence/iteration_<N>_evidence.json`（从 regression 文件所在目录）
- **禁止**使用 `.artifacts/` 路径引用（该目录不纳入版本控制）
- 完整模板参见 [iteration_evidence_snippet.template.md](../acceptance/_templates/iteration_evidence_snippet.template.md)

### Schema 校验命令

```bash
# 校验证据文件是否符合 schema（推荐在提交前运行）
python -m jsonschema -i docs/acceptance/evidence/iteration_<N>_evidence.json schemas/iteration_evidence_v2.schema.json

# 校验成功无输出，失败会显示具体错误

# 使用 CI 门禁校验（推荐）
make check-iteration-evidence

# 批量校验所有证据文件
for f in docs/acceptance/evidence/iteration_*_evidence.json; do
  echo "校验: $f"
  python -m jsonschema -i "$f" schemas/iteration_evidence_v2.schema.json && echo "✅ 通过" || echo "❌ 失败"
done
```

**校验要点**：
- 必须字段：`iteration_number`、`recorded_at`、`commit_sha`、`runner`、`commands`
- `commands` 数组至少包含 1 个命令记录
- `result` 必须为 `PASS`、`FAIL`、`SKIP` 或 `ERROR`
- **禁止**包含敏感信息（密码、API 密钥、DSN 等）

### Evidence v2 演进策略（简版）

- 当前默认 Schema 为 v2（见 `scripts/iteration/iteration_evidence_schema.py`），v1 仅用于历史兼容。
- **non-breaking**：可选字段新增/校验收紧 → 更新 v2 schema + 模板 + fixtures。
- **breaking**：结构或字段变更 → 新增 v3 schema，更新脚本默认指向 v3，保留 v2；禁止覆盖旧版本。
- 如需升级历史证据：用 `record_iteration_evidence.py` 重新生成，避免手工编辑 JSON。

### 推荐的完整流程（生成 → 校验 → 引用）

```bash
# 1. 运行门禁并确保通过
make ci

# 2. 生成证据文件（推荐带 CI URL）
python scripts/iteration/record_iteration_evidence.py <N> \
  --ci-run-url https://github.com/<org>/<repo>/actions/runs/<run_id> \
  --add-command 'ci:make ci:PASS'

# 3. 校验 Schema 合规性
python -m jsonschema -i docs/acceptance/evidence/iteration_<N>_evidence.json \
  schemas/iteration_evidence_v2.schema.json

# 4. 同步 regression 文档受控区块
python scripts/iteration/sync_iteration_regression.py <N> --write

# 5. 验证迭代文档完整性
make check-iteration-docs

# 6. 提交证据文件
git add docs/acceptance/evidence/iteration_<N>_evidence.json
git add docs/acceptance/iteration_<N>_regression.md  # 如有更新
git commit -m "evidence: Iteration <N> 验收证据"
```

> **命名规范**：参见 [ADR 3.5 版本化证据文件](../architecture/adr_iteration_docs_workflow.md#35-版本化证据文件)

---

## 6. 验证

```bash
# 全量检查（.iteration/ 链接 + SUPERSEDED 一致性）
make check-iteration-docs

# 仅检查 SUPERSEDED 一致性
make check-iteration-docs-superseded-only
```

---

## Fixtures 漂移处理

入口： [迭代 fixtures 漂移治理规范](iteration_fixtures_drift_governance.md)

> 受控块契约如有 breaking 变更，必须新增 `docs/contracts/iteration_regression_generated_blocks_v3.md`，禁止覆盖 v2。

**最短路径命令示例**（仅处理 fixtures 漂移）：

```bash
make iteration-rerun-advice
make iteration-min-regression TYPES="profiles blocks evidence schema cycle" DRY_RUN=1
python scripts/iteration/update_iteration_fixtures.py --min-gate --sync-regression --evidence-snippet --iteration-cycle
pytest tests/iteration/test_render_min_gate_block.py -q
pytest tests/iteration/test_render_iteration_evidence_snippet.py -q
pytest tests/iteration/test_sync_iteration_regression.py -q
pytest tests/iteration/test_update_iteration_fixtures.py -q
```

### PR diff 场景的最小集合（按 change_type）

> 推荐先执行 `make iteration-rerun-advice RANGE=origin/master...HEAD` 获取类型集合。

| change_type | 适用 diff | 最小集合 |
|---|---|---|
| `profiles` | gate profiles / min gate block | `make iteration-min-regression TYPES=profiles` |
| `blocks` | generated blocks / sync regression | `make iteration-min-regression TYPES=blocks` |
| `evidence` | evidence snippet / evidence 数据 | `make iteration-min-regression TYPES=evidence` |
| `schema` | evidence schema | `make iteration-min-regression TYPES=schema` |
| `cycle` | iteration cycle / fixtures refresh | `make iteration-min-regression TYPES=cycle` |

可组合多个类型：`make iteration-min-regression TYPES="profiles blocks evidence schema cycle" DRY_RUN=0`

---

## 典型工作流

### 新建迭代

```bash
# 1. 初始化草稿
make iteration-init-next

# 2. 编辑草稿
# 编辑 .iteration/<N>/plan.md
# 编辑 .iteration/<N>/regression.md

# 3. 晋升
make iteration-promote N=<N>

# 4. 验证
make check-iteration-docs

# 5. 提交
git add docs/acceptance/ && git commit -m "docs: 添加 Iteration <N>"
```

### 替代旧迭代

```bash
# 1. 初始化新迭代草稿
make iteration-init-next

# 2. 编辑草稿...

# 3. 晋升并取代旧迭代
python scripts/iteration/promote_iteration.py <N> --supersede <OLD>

# 4. 验证
make check-iteration-docs

# 5. 提交
git add docs/acceptance/ && git commit -m "docs: Iteration <N> 取代 Iteration <OLD>"
```

### 记录验收证据

```bash
# 1. 运行门禁
make ci

# 2. 记录证据（推荐：指定 CI 运行 URL）
python scripts/iteration/record_iteration_evidence.py <N> \
  --ci-run-url https://github.com/<org>/<repo>/actions/runs/<run_id>

# 或传入命令执行结果
python scripts/iteration/record_iteration_evidence.py <N> \
  --commands '{"make ci": {"exit_code": 0, "summary": "passed"}}'

# 3. 同步回归文档受控区块
python scripts/iteration/sync_iteration_regression.py <N> --write

# 4. 提交
git add docs/acceptance/evidence/iteration_<N>_evidence.json
git commit -m "evidence: Iteration <N> 验收证据"
```

> **注意**：❌ 禁止手动创建或修改 evidence JSON；回归文档受控区块（`min_gate_block` / `evidence_snippet`）内容与 marker 也禁止手改，应使用脚本同步。

---

## 7. 历史文件批量迁移（Migration Runbook）

> **背景**：由于迭代工作流在 Iteration 8 之后才引入 evidence 文件和标准化模板，历史 regression 文件（Iteration 2-7, 10-12）缺失 evidence 文件和标准化段落。本章节提供批量修复策略。

### 当前状态盘点

截至 2026-02-02，文件状态如下：

| 迭代编号 | regression 文件 | evidence 文件 | 状态 |
|----------|-----------------|---------------|------|
| 2-7 | ✅ 存在 | ❌ 缺失 | 需补充 evidence |
| 8, 9 | ✅ 存在 | ✅ 存在 | 已完成 |
| 10-12 | ✅ 存在 | ❌ 缺失 | 需补充 evidence |
| 13, 14 | ✅ 存在 | ✅ 存在 | 已完成 |

**缺失 evidence 的迭代**：2, 3, 4, 5, 6, 7, 10, 11, 12（共 9 个）

### 批量执行策略

#### 步骤 1：生成最小 evidence 文件

对缺失 evidence 的迭代，使用 `record_iteration_evidence.py` 生成最小 evidence：

```bash
# 批量生成最小 evidence（使用当前 commit，标记为历史补录）
for N in 2 3 4 5 6 7 10 11 12; do
  echo "=== 生成 Iteration $N evidence ==="
  python scripts/iteration/record_iteration_evidence.py $N \
    --add-command "historical_record:(historical backfill):PASS" \
    --notes "历史迭代补录：原始验收时未记录 evidence 文件，此为 2026-02-02 补录。"
done
```

**说明**：
- `--add-command` 格式：`NAME:COMMAND:RESULT`
- 使用 `historical_record` 作为命令名，标识这是历史补录
- `--notes` 记录补录原因和时间

#### 步骤 2：补充真实命令结果（可选）

如有历史 CI 运行记录，可补充真实命令结果：

```bash
# 示例：补充真实的 make ci 结果
python scripts/iteration/record_iteration_evidence.py <N> \
  --add-command 'ci:make ci:PASS' \
  --ci-run-url https://github.com/<org>/<repo>/actions/runs/<run_id> \
  --notes "补充历史 CI 运行结果"
```

#### 步骤 3：同步 regression 文档的证据段落

使用同步脚本批量更新 regression 文档：

```bash
# 批量同步证据段落到 regression 文档
for N in 2 3 4 5 6 7 10 11 12; do
  echo "=== 同步 Iteration $N regression 文档 ==="
  python scripts/iteration/sync_iteration_regression.py $N
done
```

**同步内容**：
- 补充"验收证据"段落（如缺失）
- 更新 evidence 文件引用路径
- 确保段落格式符合模板规范

#### 步骤 4：验证与提交

```bash
# 1. 校验所有 evidence 文件
make check-iteration-evidence

# 2. 校验迭代文档规范（warn-only 模式）
make check-iteration-docs

# 3. 查看变更
git status
git diff docs/acceptance/

# 4. 分批提交（建议按迭代编号分组）
git add docs/acceptance/evidence/iteration_{2,3,4}_evidence.json
git add docs/acceptance/iteration_{2,3,4}_regression.md
git commit -m "evidence: 补录 Iteration 2-4 历史证据文件"

git add docs/acceptance/evidence/iteration_{5,6,7}_evidence.json
git add docs/acceptance/iteration_{5,6,7}_regression.md
git commit -m "evidence: 补录 Iteration 5-7 历史证据文件"

git add docs/acceptance/evidence/iteration_{10,11,12}_evidence.json
git add docs/acceptance/iteration_{10,11,12}_regression.md
git commit -m "evidence: 补录 Iteration 10-12 历史证据文件"
```

### CI 门禁切换策略

#### 当前状态（warn-only）

Makefile 中 `check-iteration-docs` 使用 `--warn-only` 模式：

```makefile
check-iteration-docs:
	$(PYTHON) -m scripts.ci.check_iteration_docs_placeholders --verbose --warn-only
```

此模式下，历史文件的占位符/标题缺失仅产生警告，不阻断 CI。

#### 切换为阻断模式

当所有历史文件补齐后，修改 Makefile 切换为阻断模式：

```bash
# 1. 验证所有文件已补齐
make check-iteration-docs-headings  # 阻断模式测试

# 2. 如无错误，更新 Makefile
# 将 check-iteration-docs 目标中的 --warn-only 移除：
# 旧：$(PYTHON) -m scripts.ci.check_iteration_docs_placeholders --verbose --warn-only
# 新：$(PYTHON) -m scripts.ci.check_iteration_docs_placeholders --verbose

# 3. 验证 CI 通过
make ci
```

**切换条件**：
- 所有 evidence 文件已生成（`docs/acceptance/evidence/iteration_*_evidence.json`）
- 所有 regression 文档包含"验收证据"段落
- `make check-iteration-docs-headings` 无错误

### 一键修复脚本（推荐）

可创建一次性批量修复脚本：

```bash
#!/bin/bash
# scripts/ops/backfill_historical_evidence.sh
# 一次性批量补录历史迭代 evidence 文件

set -e

MISSING_ITERATIONS="2 3 4 5 6 7 10 11 12"

echo "========== 历史迭代 Evidence 批量补录 =========="
echo "将为以下迭代生成 evidence 文件: $MISSING_ITERATIONS"
echo ""

for N in $MISSING_ITERATIONS; do
  echo ">>> Iteration $N"
  
  # 生成最小 evidence
  python scripts/iteration/record_iteration_evidence.py $N \
    --add-command "historical_record:(historical backfill):PASS" \
    --notes "历史迭代补录：原始验收时未记录 evidence 文件，此为 $(date +%Y-%m-%d) 补录。"
  
  # 同步 regression 文档（如脚本存在）
  if [ -f scripts/iteration/sync_iteration_regression.py ]; then
    python scripts/iteration/sync_iteration_regression.py $N || echo "  [WARN] 同步脚本执行失败，请手动检查"
  fi
  
  echo ""
done

echo "========== 批量补录完成 =========="
echo ""
echo "下一步："
echo "  1. make check-iteration-evidence  # 校验 evidence 文件"
echo "  2. make check-iteration-docs      # 校验文档规范"
echo "  3. git status && git diff         # 查看变更"
echo "  4. 分批提交变更"
```

### 注意事项

1. **不要伪造历史**：evidence 文件的 `commit_sha` 使用当前 commit，`notes` 中明确标注为补录
2. **分批提交**：建议按迭代编号分组提交，便于代码审查和回滚
3. **保持一致性**：使用相同的补录格式和说明文字
4. **验证后再切换**：确保所有文件补齐后再移除 `--warn-only`，避免 CI 频繁失败

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [迭代文档本地草稿工作流](iteration_local_drafts.md) | 详细的草稿管理指南 |
| [迭代文档 ADR](../architecture/adr_iteration_docs_workflow.md) | 迭代文档工作流架构决策记录 |
| [验收测试矩阵](../acceptance/00_acceptance_matrix.md) | 迭代状态索引表 |
| [迭代计划模板](../acceptance/_templates/iteration_plan.template.md) | 计划模板 |
| [回归记录模板](../acceptance/_templates/iteration_regression.template.md) | 回归模板 |

---

更新时间：2026-02-02（新增历史文件批量迁移章节）
