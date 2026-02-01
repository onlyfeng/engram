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

**推荐方式**：使用脚本自动渲染（读取 evidence JSON 的 commands）：

```bash
# 渲染最小门禁块到 regression 文档
python scripts/iteration/render_min_gate_block.py <N>

# 或更新已有文档中的证据块
python scripts/iteration/update_min_gate_block_in_regression.py <N>
```

**生成后的段落示例**：

```markdown
## 验收证据

<!-- AUTO-GENERATED EVIDENCE BLOCK START -->
<!-- 此段落由脚本自动生成，请勿手动编辑 -->

| 项目 | 值 |
|------|-----|
| **证据文件** | [`iteration_13_evidence.json`](evidence/iteration_13_evidence.json) |
| **Schema 版本** | `iteration_evidence_v1.schema.json` |
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
python -m jsonschema -i docs/acceptance/evidence/iteration_<N>_evidence.json schemas/iteration_evidence_v1.schema.json

# 校验成功无输出，失败会显示具体错误

# 使用 CI 门禁校验（推荐）
make check-iteration-evidence

# 批量校验所有证据文件
for f in docs/acceptance/evidence/iteration_*_evidence.json; do
  echo "校验: $f"
  python -m jsonschema -i "$f" schemas/iteration_evidence_v1.schema.json && echo "✅ 通过" || echo "❌ 失败"
done
```

**校验要点**：
- 必须字段：`iteration_number`、`recorded_at`、`commit_sha`、`runner`、`commands`
- `commands` 数组至少包含 1 个命令记录
- `result` 必须为 `PASS`、`FAIL`、`SKIP` 或 `ERROR`
- **禁止**包含敏感信息（密码、API 密钥、DSN 等）

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
  schemas/iteration_evidence_v1.schema.json

# 4. 在 regression 文档中添加引用（参照上方模板或 iteration_evidence_snippet.template.md）

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

# 3. 提交
git add docs/acceptance/evidence/iteration_<N>_evidence.json
git commit -m "evidence: Iteration <N> 验收证据"
```

> **注意**：❌ 禁止手动创建草稿证据文件提交，应使用脚本生成。

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

更新时间：2026-02-02（补充 evidence 生成/校验/引用推荐流程）
