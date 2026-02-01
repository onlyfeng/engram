# 本地迭代草稿管理指南

本文档说明 `.iteration/` 目录的结构建议、如何从模板初始化以及何时晋升到 `docs/acceptance/`。

---

## 概述

`.iteration/` 是本地化的迭代计划草稿目录，**不纳入版本控制**。它用于：

- 在正式提交前起草迭代计划
- 记录临时的回归测试笔记
- 个人工作追踪和备忘

当迭代计划成熟并准备好与团队共享时，再晋升到 `docs/acceptance/`。

---

## 目录结构

```
.iteration/
├── README.md           # 目录说明（自动生成）
├── <N>/                # Iteration N 草稿（N 为目标迭代编号）
│   ├── plan.md         # 迭代计划草稿
│   └── regression.md   # 回归记录草稿
└── ...
```

> **编号说明**: 草稿目录的编号 `<N>` 应为**尚未在 SSOT 中使用**的编号。
> 若 `docs/acceptance/` 中已存在 Iteration N（无论状态），草稿应使用新编号。
> 晋升前务必查询当前最高编号，确保目标编号可用。

---

## 从模板初始化

### 使用脚本初始化（推荐）

```bash
# 直接指定目标迭代编号
python scripts/iteration/init_local_iteration.py 12

# 如果编号已在 SSOT 中存在，脚本会报错并建议下一可用编号
# 示例输出:
# ❌ 错误: Iteration 11 已在 docs/acceptance/ 中存在（SSOT 冲突）
#
# SSOT 中已存在以下文件:
#   - docs/acceptance/iteration_11_plan.md
#   - docs/acceptance/iteration_11_regression.md
#
# 💡 建议: 使用下一可用编号 12
#    python scripts/iteration/init_local_iteration.py 12
```

脚本会自动：

1. **检测 SSOT 冲突**（若 `docs/acceptance/iteration_<N>_{plan,regression}.md` 已存在则报错并建议下一可用编号）
2. 创建 `.iteration/` 目录（如不存在）
3. 创建 `.iteration/README.md`（如不存在）
4. 创建 `.iteration/<N>/plan.md`（从模板填充）
5. 创建 `.iteration/<N>/regression.md`（从模板填充）

> **注意**: 脚本内置了 SSOT 冲突检测，无需手动查询可用编号。若指定的编号已被使用，脚本会给出明确的错误信息和建议。

### 修复 README 内容异常

如果 `.iteration/README.md` 内容被意外修改或损坏，可使用 `--refresh-readme` 强制刷新：

```bash
# 强制刷新 README（同时初始化指定迭代）
python scripts/iteration/init_local_iteration.py 12 --refresh-readme

# 使用 --force 时也会自动刷新 README
python scripts/iteration/init_local_iteration.py 12 --force
```

> **使用场景**: 当 `.iteration/README.md` 内容与预期不符（如模板被误编辑、格式损坏等），使用 `--refresh-readme` 可将其重置为标准内容。

### 手动初始化

如果需要手动创建（不推荐，除非脚本不可用）：

```bash
# 先查询下一可用编号
NEXT_N=$(( $(ls docs/acceptance/iteration_*_*.md 2>/dev/null | \
  sed -E 's/.*iteration_([0-9]+)_.*/\1/' | sort -n | tail -1 || echo 0) + 1 ))
echo "下一可用编号: $NEXT_N"

# 确认编号未被使用
ls docs/acceptance/iteration_${NEXT_N}_*.md 2>/dev/null && echo "⚠️ 编号已存在！" || echo "✅ 编号可用"

# 创建目录结构
mkdir -p .iteration/$NEXT_N

# 复制模板
cp docs/acceptance/_templates/iteration_plan.template.md .iteration/$NEXT_N/plan.md
cp docs/acceptance/_templates/iteration_regression.template.md .iteration/$NEXT_N/regression.md
```

> **推荐使用脚本**: 脚本会自动检测编号冲突并给出建议，避免手动查询可能的遗漏。

---

## 晋升到 docs/acceptance/

当满足以下条件时，应将本地草稿晋升到 `docs/acceptance/`：

### 使用脚本晋升（推荐）

```bash
# 基本晋升
python scripts/iteration/promote_iteration.py 13

# 指定日期和状态
python scripts/iteration/promote_iteration.py 13 --date 2026-02-01 --status PARTIAL

# 晋升并标记旧迭代为已取代
python scripts/iteration/promote_iteration.py 13 --supersede 12

# 预览模式（不实际执行）
python scripts/iteration/promote_iteration.py 13 --dry-run
```

脚本会自动：

1. **检测 SSOT 冲突**（若目标编号已在 `docs/acceptance/` 存在则报错）
2. **复制草稿文件**（从 `.iteration/<N>/` 到 `docs/acceptance/`）
3. **更新索引表**（在 `00_acceptance_matrix.md` 插入新行，置顶）
4. **处理 SUPERSEDED**（若指定 `--supersede`，自动更新旧迭代的状态和声明）

#### 晋升脚本参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `iteration_number` | 目标迭代编号（必须） | - |
| `--date`, `-d` | 日期（YYYY-MM-DD 格式） | 今天 |
| `--status`, `-s` | 状态（PLANNING/PARTIAL/PASS/FAIL） | PLANNING |
| `--description` | 说明文字 | 自动生成 |
| `--supersede OLD_N` | 标记旧迭代 OLD_N 为已被取代 | - |
| `--dry-run`, `-n` | 预览模式，不实际修改文件 | false |

#### 晋升后的后续步骤

晋升脚本完成后，仍需手动完成以下步骤：

1. **编辑晋升后的文件**：移除模板说明区块、替换所有 `{PLACEHOLDER}` 占位符
2. **运行验证**：`make check-iteration-docs`
3. **提交变更**：`git add docs/acceptance/ && git commit`
4. **清理草稿**（可选）：`rm -rf .iteration/<N>/`

### 晋升条件

| 条件 | 说明 |
|------|------|
| **计划成熟** | 迭代目标、范围边界、验收门禁已明确 |
| **团队对齐** | 计划已与相关人员讨论并达成共识 |
| **准备执行** | 迭代即将开始或已开始执行 |
| **需要版本化** | 计划需要作为正式记录保存 |

### 编号分配规则

> **重要**: 晋升前必须检查目标编号是否可用。详见 [ADR: 编号与晋升决策](../architecture/adr_iteration_docs_workflow.md#5-编号与晋升决策)。

| 规则 | 说明 |
|------|------|
| **不复用 SSOT 编号** | `docs/acceptance/` 中已存在的编号（无论状态）不可复用 |
| **查询下一可用编号** | `ls docs/acceptance/iteration_*_*.md \| sed -E 's/.*iteration_([0-9]+)_.*/\1/' \| sort -n \| tail -1` |
| **草稿编号冲突** | 若 `.iteration/<N>/` 的 N 已在 SSOT 中出现，必须重命名为新编号后再晋升 |

### 晋升步骤

> **示例说明**: 以下示例假设当前 SSOT 最高编号为 Iteration 10，故使用 Iteration 11 作为新迭代编号。
> 实际操作前请先查询当前最高编号，选择 next available N。

**步骤 0：确认目标编号**

```bash
# 查询当前最高编号
CURRENT_MAX=$(ls docs/acceptance/iteration_*_*.md 2>/dev/null | \
  sed -E 's/.*iteration_([0-9]+)_.*/\1/' | sort -n | tail -1)
echo "当前最高编号: ${CURRENT_MAX:-0}"
NEXT_N=$((${CURRENT_MAX:-0} + 1))
echo "下一可用编号: $NEXT_N"

# 示例输出:
# 当前最高编号: 10
# 下一可用编号: 11
```

**步骤 1：复制文件到目标位置**

```bash
# 假设草稿在 .iteration/11/，晋升到 Iteration 11
cp .iteration/11/plan.md docs/acceptance/iteration_11_plan.md
cp .iteration/11/regression.md docs/acceptance/iteration_11_regression.md

# ⚠️ 若草稿编号与 SSOT 冲突（如 .iteration/9/ 但 Iteration 9 已存在），
#    应先重命名草稿目录，或直接复制到新编号:
#    cp .iteration/9/plan.md docs/acceptance/iteration_11_plan.md
```

**步骤 2：更新文件内容**

- 移除模板说明区块（文件顶部的使用说明）
- 填写所有必须字段
- 替换所有 `{PLACEHOLDER}` 占位符
- 若从冲突编号晋升，更新文档内部的编号引用

**步骤 3：更新索引**

在 `docs/acceptance/00_acceptance_matrix.md` 的「迭代回归记录索引」表中添加条目（新迭代置于表格最上方）：

```markdown
| 迭代 | 日期 | 状态 | 计划 | 详细记录 | 说明 |
|------|------|------|------|----------|------|
| **Iteration 11** | 2026-02-01 | 🔄 PLANNING | [iteration_11_plan.md](iteration_11_plan.md) | [iteration_11_regression.md](iteration_11_regression.md) | 迭代 11 计划 |
```

**步骤 4：提交变更**

```bash
git add docs/acceptance/iteration_11_plan.md docs/acceptance/iteration_11_regression.md docs/acceptance/00_acceptance_matrix.md
git commit -m "docs: 添加 Iteration 11 计划和回归记录"
```

**步骤 5：清理本地草稿**（可选）

```bash
rm -rf .iteration/11/
```

---

## 晋升 SOP（强制步骤）

> **本章节定义晋升的强制操作步骤，必须全部完成才能视为晋升成功。**

### SSOT 与草稿边界

| 类别 | 位置 | 版本控制 | 可链接 | 说明 |
|------|------|----------|--------|------|
| **SSOT（权威来源）** | `docs/acceptance/00_acceptance_matrix.md` | 是 | 是 | 迭代索引表，跨迭代状态跟踪 |
| **SSOT（权威来源）** | `docs/acceptance/iteration_<N>_plan.md` | 是 | 是 | 迭代计划正式文档 |
| **SSOT（权威来源）** | `docs/acceptance/iteration_<N>_regression.md` | 是 | 是 | 迭代回归记录正式文档 |
| **草稿（非 SSOT）** | `.iteration/<N>/...` | **否** | **否** | 仅本地，禁止 Markdown 链接引用 |

### 晋升必做清单

晋升本地草稿到 SSOT 时，**必须完成以下全部步骤**：

| # | 步骤 | 命令/操作 | 验证方式 |
|---|------|-----------|----------|
| 1 | **复制文件** | `cp .iteration/<N>/plan.md docs/acceptance/iteration_<N>_plan.md` | 文件存在 |
| | | `cp .iteration/<N>/regression.md docs/acceptance/iteration_<N>_regression.md` | 文件存在 |
| 2 | **去掉模板说明区** | 删除文件顶部的 `<!-- 模板说明 -->` 区块 | 无模板说明残留 |
| 3 | **替换占位符** | 替换所有 `{PLACEHOLDER}` | `grep -q '{' docs/acceptance/iteration_<N>_*.md && echo "FAIL"` |
| 4 | **更新索引表** | 在 `00_acceptance_matrix.md` 的「迭代回归记录索引」表中添加条目 | 新迭代置于表格**最上方** |
| 5 | **运行检查** | `make check-iteration-docs` | 退出码 0 |
| 6 | **提交变更** | `git add && git commit` | commit 包含所有新文件和索引更新 |

### 涉及 SUPERSEDED 时的附加步骤

当晋升涉及将旧迭代标记为 `🔄 SUPERSEDED` 时，**必须同步完成**：

| # | 附加步骤 | 操作 | 验证方式 |
|---|----------|------|----------|
| S1 | **更新旧 regression 头部声明** | 在旧 `iteration_<OLD>_regression.md` 顶部添加标准 SUPERSEDED 声明 | 符合 R6 格式规范 |
| S2 | **更新索引表状态** | 将旧迭代的「状态」改为 `🔄 SUPERSEDED` | 索引表状态正确 |
| S3 | **更新索引表说明** | 在旧迭代的「说明」字段添加 `已被 Iteration <N> 取代` | 说明字段正确 |
| S4 | **验证一致性** | `make check-iteration-docs` | 退出码 0（含 SUPERSEDED 规则检查） |

#### SUPERSEDED 头部声明格式（R6 规范）

```markdown
> **🔄 SUPERSEDED**
>
> 本迭代已被 [Iteration M](iteration_M_regression.md) 取代，不再维护。
> 请参阅后续迭代的回归记录获取最新验收状态。

---
```

### 晋升验证命令速查

```bash
# 1. 检查文件是否正确复制
ls -la docs/acceptance/iteration_<N>_*.md

# 2. 检查占位符残留
grep -rn '{' docs/acceptance/iteration_<N>_*.md && echo "❌ 占位符残留" || echo "✅ 无占位符"

# 3. 检查模板说明区残留
grep -n '<!-- 模板' docs/acceptance/iteration_<N>_*.md && echo "❌ 模板说明残留" || echo "✅ 无模板说明"

# 4. 运行完整检查（.iteration/ 链接 + SUPERSEDED 一致性）
make check-iteration-docs

# 5. 仅检查 SUPERSEDED 一致性（快速验证）
make check-iteration-docs-superseded-only
```

---

## 版本控制说明

`.iteration/` 目录已在 `.gitignore` 中排除，**不会被纳入版本控制**。

这意味着：

- 本地草稿不会出现在 `git status` 中
- 草稿不会被意外提交
- 每个开发者可以有自己的本地草稿

如果需要共享草稿，请使用其他方式（如 Slack、邮件）或直接晋升到 `docs/acceptance/`。

---

## 最佳实践

### 推荐做法

- 在开始新迭代前，先创建本地草稿
- 逐步完善计划内容，不必一次性写完
- 使用脚本初始化以确保模板一致性
- 及时晋升已确定的计划，避免草稿过期

### 不推荐做法

- 不要在 `.iteration/` 中存放重要的唯一副本
- 不要跳过晋升步骤直接引用本地草稿
- 不要修改 `.gitignore` 以包含 `.iteration/`

---

## CI 检查命令

`.iteration/` 链接禁止规则和 SUPERSEDED 一致性规则已集成到 CI 门禁：

```bash
# 全量检查（.iteration/ 链接 + SUPERSEDED 一致性）
make check-iteration-docs

# 仅检查 SUPERSEDED 一致性
make check-iteration-docs-superseded-only
```

> 详细的检查规则参见 [ADR: 迭代文档工作流](../architecture/adr_iteration_docs_workflow.md#合规检查)

---

## 审计与检查

### 审计工具

| 工具 | 用途 | 命令 |
|------|------|------|
| **CI 门禁检查** | 自动化检查 SUPERSEDED 一致性（阻断式） | `make check-iteration-docs` |
| **审计报告脚本** | 生成完整审计报告（非阻断） | `python scripts/iteration/audit_iteration_docs.py` |

```bash
# 生成审计报告到 stdout
python scripts/iteration/audit_iteration_docs.py

# 生成审计报告到文件
python scripts/iteration/audit_iteration_docs.py --output-dir .artifacts/iteration-audit
```

> **注意**：审计报告为一次性快照，**不是 SSOT**。
> `docs/acceptance/_drafts/` 中的报告仅作为历史样例保留。

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_plan.template.md](../acceptance/_templates/iteration_plan.template.md) | 迭代计划模板 |
| [iteration_regression.template.md](../acceptance/_templates/iteration_regression.template.md) | 回归记录模板 |
| [00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) | 验收测试矩阵 |
| [adr_iteration_docs_workflow.md](../architecture/adr_iteration_docs_workflow.md) | 迭代文档工作流 ADR |
| [scripts/iteration/init_local_iteration.py](../../scripts/iteration/init_local_iteration.py) | 初始化脚本 |
| [scripts/iteration/promote_iteration.py](../../scripts/iteration/promote_iteration.py) | 晋升脚本 |
| [scripts/iteration/audit_iteration_docs.py](../../scripts/iteration/audit_iteration_docs.py) | 审计报告脚本 |

---

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-02-01 | 初始版本 |
| 2026-02-01 | 增补「编号分配规则」，更新晋升步骤示例避免编号复用误导 |
| 2026-02-01 | 脚本新增 SSOT 冲突检测功能，自动建议下一可用编号；更新示例命令 |
| 2026-02-01 | 新增 `--refresh-readme` 参数，支持强制刷新 README；`--force` 同时刷新 README |
| 2026-02-01 | 新增「晋升 SOP（强制步骤）」章节：定义 SSOT 边界、晋升必做清单、SUPERSEDED 附加步骤 |
| 2026-02-02 | 新增 `promote_iteration.py` 晋升脚本：自动复制草稿、更新索引、处理 SUPERSEDED |
| 2026-02-02 | 新增「审计与检查」章节：介绍 `audit_iteration_docs.py` 脚本和 CI 门禁检查 |

_更新时间：2026-02-02_
