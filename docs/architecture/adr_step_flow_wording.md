# ADR: Step 文案治理 — 组件旧命名与流程编号区分

> 状态: **已批准**  
> 创建日期: 2026-01-30  
> 决策者: Engram Core Team

---

## 1. 背景与问题

### 1.1 历史命名由来

Engram 项目早期采用数字阶段命名（`Step1`/`Step2`/`Step3`）表示系统构建的三个阶段：

| 历史编号 | 阶段含义 | 现官方名称 | 模块路径 |
|----------|----------|------------|----------|
| Step1 | 事实账本（日志存储与追踪） | **Logbook** | `src/engram/logbook/` |
| Step2 | 记忆网关（MCP 协议、策略校验） | **Gateway** | `src/engram/gateway/` |
| Step3 | 检索索引（RAG、向量搜索） | **SeekDB** | `docs/seekdb/` |

### 1.2 存在的问题

随着项目成熟，数字阶段命名产生以下问题：

| 问题 | 影响 |
|------|------|
| **语义模糊** | "Step1" 无法直观表达"事实账本"职责 |
| **易与流程编号混淆** | 文档中 `Step 1: 安装依赖` 与组件名 `Step1` 视觉相似 |
| **新成员学习成本** | 需记忆 `StepN ↔ 官方组件名` 映射 |
| **代码维护困难** | 搜索 `step1` 无法区分组件引用还是流程步骤 |
| **国际化障碍** | 数字编号无法本地化为语义化名称 |

### 1.3 核心矛盾

存在两种 `Step` 相关写法的合法场景：

| 场景 | 示例 | 语义 |
|------|------|------|
| **组件旧命名（禁止）** | `Step1`, `step2`, `STEP3` | 指代组件，需替换为官方名称 |
| **流程编号（允许）** | `Step 1:`, `step 2`, `步骤 3` | 操作步骤编号，带空格 |

**区分规则**：`StepN`（无空格）→ 组件旧名；`Step N`（带空格）→ 流程编号。

---

## 2. 决策

**采用方案 (B)**: 禁止组件旧命名，允许流程编号

### 2.1 禁止模式

使用正则模式识别组件旧命名：

```
禁止模式: (?i)step[1-3](?!\s)
含义: 匹配任意大小写的 step 后紧跟数字 1/2/3，且后面没有空格
```

| 匹配示例（禁止） | 替代写法 |
|------------------|----------|
| `Step1`, `step1`, `STEP1` | `Logbook` |
| `Step2`, `step2`, `STEP2` | `Gateway` |
| `Step3`, `step3`, `STEP3` | `SeekDB` |

### 2.2 允许模式

| 允许写法 | 示例 | 说明 |
|----------|------|------|
| `Step N`（英文带空格） | `Step 1: 环境准备` | 流程编号 |
| `step N`（小写带空格） | `see step 1 above` | 行内引用 |
| `步骤 N`（中文带空格） | `步骤 1：安装依赖` | 中文流程编号 |

---

## 3. 备选方案对比

### 方案 (A): 保留组件旧命名兼容层

**做法**：
- 代码中保留 `step1`/`step2`/`step3` 别名
- 环境变量保留 `ENGRAM_STEP1_CONFIG` 等旧名
- 文档中允许新旧命名并存

**优点**：
- 零迁移成本，向后兼容
- 现有脚本无需修改

**缺点**：
- 长期维护双套命名的认知负担
- 新成员需学习新旧映射
- CI 检查复杂度增加
- 文档一致性难以保证

### 方案 (B): 禁止组件旧命名 ✅ **推荐**

**做法**：
- 代码/文档/注释中禁止 `StepN` 形式的组件旧名
- 保留 `Step N`（带空格）作为流程编号
- 通过 CI 自动检查强制执行

**优点**：
- **一致性**：全项目统一使用语义化组件名
- **可搜索性**：搜索 `Logbook` 直接定位组件相关代码
- **可维护性**：无需维护新旧名称映射逻辑
- **新成员友好**：组件名自解释

**缺点**：
- 需一次性清理存量旧命名
- 极少数外部引用可能需更新

---

## 4. 治理范围

### 4.1 治理范围边界（Governance Scope）

本文案治理规则的适用范围遵循以下边界定义：

#### 4.1.1 受治理目录（IN SCOPE）

| 目录 | 说明 | 治理级别 |
|------|------|----------|
| `docs/` | 项目文档 | **强制** |
| `scripts/` | 脚本和工具 | **强制** |
| `src/` | 应用代码 | **强制** |
| `logbook_postgres/` | 迁移与工具脚本 | **强制** |
| `.github/` | CI/CD 配置 | **强制** |

#### 4.1.2 排除目录（OUT OF SCOPE）

以下目录**不受**本治理规则约束：

| 排除目录 | 排除原因 | 同步策略 |
|----------|----------|----------|
| `vendor/`, `third_party/` | 第三方 vendored 代码 | 不治理 |
| `node_modules/`, `.venv/` | 依赖安装目录 | 不治理 |

**设计原则**：
- 第三方依赖保持原样，避免引入额外治理成本
- 治理范围仅覆盖自研代码与文档

#### 4.1.3 上游镜像说明

OpenMemory 通过 `OPENMEMORY_IMAGE` 引入，不在仓库内，因此不参与目录治理。

### 4.2 受影响的文件类型

| 范围 | 文件类型 | 检查方式 |
|------|----------|----------|
| **代码文件** | `*.py`, `*.js`, `*.ts`, `*.go`, `*.rs` | CI lint |
| **配置文件** | `*.yml`, `*.yaml`, `*.toml`, `*.json`, `*.env` | CI lint |
| **文档文件** | `*.md`, `*.rst`, `*.txt` | CI lint |
| **Shell 脚本** | `*.sh`, `*.bash` | CI lint |
| **SQL 脚本** | `*.sql` | CI lint |
| **CI Workflow** | `.github/workflows/*.yml` | CI lint |
| **日志输出** | 运行时日志、CLI 输出 | 人工审查 |
| **注释** | 代码注释、TODO 注释 | CI lint |

### 4.3 CI 检查实现

```bash
# 检查禁止词（匹配无空格的 stepN 组件旧名，N=1,2,3）
# 注意：排除 vendored/依赖目录（见 §4.1.2）
rg -iP '(?i)step[1-3](?!\s)' \
  --type-add 'code:*.{py,js,ts,go,rs,yml,yaml,toml,json,md,sh,sql}' \
  -t code \
  --glob '!vendor/**' \
  --glob '!third_party/**' \
  --glob '!node_modules/**'
```

**检查脚本**：`scripts/check_no_step_flow_numbers.py` 实现了上述边界检查，详见脚本中的 `EXCLUDE_DIRS` 配置。

---

## 5. 替代写法规范

### 5.1 组件称谓替代表

| 旧写法（禁止） | 新写法（官方） | 适用场景 |
|---------------|----------------|----------|
| `Step1`, `step1`, `STEP1` | `Logbook` | 代码/文档/注释 |
| `Step2`, `step2`, `STEP2` | `Gateway` 或 `Memory Gateway` | 代码/文档/注释 |
| `Step3`, `step3`, `STEP3` | `SeekDB` 或 `Seek Index` | 代码/文档/注释 |

### 5.2 目录/模块替代表

| 旧路径形式（已弃用） | 新路径（官方） |
|---------------------|----------------|
| 数字阶段 1 旧目录（`apps/step{N}_*/`） | `src/engram/logbook/` |
| 数字阶段 2 旧目录（`apps/step{N}_*/`） | `src/engram/gateway/` |
| 数字阶段 3 旧目录（`apps/step{N}_*/`） | `docs/seekdb/` |
| 数字阶段 1 旧模块（`engram_step{N}`） | `engram_logbook` |

### 5.3 环境变量替代表

| 旧变量（禁止） | 新变量（官方） |
|---------------|----------------|
| `ENGRAM_STEP1_CONFIG` | `ENGRAM_LOGBOOK_CONFIG` |
| `STEP1_*` 系列 | `LOGBOOK_*` 系列 |

### 5.4 文档写法示例

**正确写法**：

```markdown
## Logbook 部署指南

### Step 1: 环境准备

配置 Logbook 数据库连接参数...

### Step 2: 安装依赖

运行 pip install 安装 Logbook 依赖...
```

**禁止写法**：

```markdown
## Step1 部署指南            <!-- 禁止：组件旧名 -->

### Step1: 环境准备          <!-- 禁止：无空格 -->

配置 step1 数据库连接参数... <!-- 禁止：小写组件旧名 -->
```

---

## 6. 迁移策略

### 6.1 策略选择：一次性清理

**决策**：采用一次性清理策略，而非渐进式迁移。

| 策略 | 优点 | 缺点 | 采用 |
|------|------|------|------|
| **一次性清理** | 快速达成一致性；避免长期维护双态 | 单次变更量大 | ✅ |
| **渐进式迁移** | 变更分散，风险低 | 长期维护双套命名；一致性保证困难 | ✗ |

**理由**：
1. 组件旧命名仅存在于代码/文档，不涉及数据库 schema 迁移
2. 变更为纯文本替换，可通过脚本批量处理
3. 一次性清理后 CI 可立即启用强制检查

### 6.2 迁移步骤

| 阶段 | 动作 | 验证 |
|------|------|------|
| **1. 盘点** | 运行 `rg -iP '(?i)step[1-3](?!\s)'` 统计存量 | 记录总数和分布 |
| **2. 批量替换** | 使用 sed/脚本批量替换组件旧名 | 人工抽检 |
| **3. CI 启用** | 添加禁止词检查到 CI pipeline | CI 绿灯 |
| **4. 文档发布** | 发布命名约束文档、本 ADR | 团队周知 |

### 6.3 迁移工具

```bash
#!/bin/bash
# 批量替换组件旧名（示例）

# Logbook
sed -i '' -E 's/([Ss])tep1([^[:space:]])/Logbook\2/g' **/*.md
sed -i '' -E 's/([Ss])tep1$/Logbook/g' **/*.md

# Gateway  
sed -i '' -E 's/([Ss])tep2([^[:space:]])/Gateway\2/g' **/*.md
sed -i '' -E 's/([Ss])tep2$/Gateway/g' **/*.md

# SeekDB
sed -i '' -E 's/([Ss])tep3([^[:space:]])/SeekDB\2/g' **/*.md
sed -i '' -E 's/([Ss])tep3$/SeekDB/g' **/*.md
```

---

## 7. 例外策略

### 7.1 豁免场景

以下场景可豁免禁止词检查：

| 场景 | 示例 | 豁免原因 |
|------|------|----------|
| **历史引用说明** | "原 Step1 现更名为 Logbook" | 迁移文档需说明历史 |
| **治理文档本身** | `legacy_naming_governance.md`、本 ADR | 定义禁止词需引用 |
| **测试用例** | 测试禁止词检测功能的代码 | 验证 CI 检查有效性 |
| **vendored 目录** | `vendor/`、`third_party/` | 第三方代码不可修改 |
| **patch 文件** | `*.patch`、`*.diff` | 历史补丁保留原貌 |

### 7.2 豁免配置

在 CI 检查中排除豁免目录（与 §4.1.2 边界定义一致）：

```bash
rg -iP '(?i)step[1-3](?!\s)' \
  --type-add 'code:*.{py,js,ts,go,rs,yml,yaml,toml,json,md,sh,sql}' \
  -t code \
  # === 第三方 vendored 代码 ===
  --glob '!vendor/**' \
  --glob '!third_party/**' \
  --glob '!node_modules/**' \
  # === 文件类型排除 ===
  --glob '!*.patch' \
  --glob '!*.diff' \
  # === 治理文档本身（需要引用被禁模式）===
  --glob '!docs/architecture/adr_step_flow_wording.md' \
  --glob '!docs/architecture/legacy_naming_governance.md'
```

**注意**：检查脚本 `scripts/check_no_step_flow_numbers.py` 中的 `EXCLUDE_DIRS` 已实现上述排除逻辑。

### 7.3 新增豁免审批

新增豁免场景需满足：

1. **PR 标题前缀**：`[Naming-Exception]`
2. **PR 描述**：说明豁免原因和范围
3. **审批要求**：CODEOWNERS 审批
4. **更新本 ADR**：在 §7.1 豁免场景表中记录

---

## 8. CI 落地约束清单

### 8.1 Workflow 文件

| 文件 | 检查项 | 实现方式 |
|------|--------|----------|
| `.github/workflows/ci.yml` | 禁止词检查 | `rg` 命令 + 退出码 |
| `.github/workflows/pr-lint.yml` | PR 标题检查 | 确保不含组件旧名 |

### 8.2 预期 CI 步骤

```yaml
# .github/workflows/ci.yml 示例
- name: Check forbidden naming patterns
  run: |
    if rg -iP '(?i)step[1-3](?!\s)' \
         --type-add 'code:*.{py,js,ts,go,rs,yml,yaml,toml,json,md,sh,sql}' \
         -t code \
         --glob '!vendor/**' \
         --glob '!third_party/**' \
         --glob '!*.patch' \
         --glob '!docs/architecture/adr_step_flow_wording.md' \
         --glob '!docs/architecture/legacy_naming_governance.md'; then
      echo "ERROR: Found forbidden naming patterns (stepN without space)"
      exit 1
    fi
    echo "OK: No forbidden naming patterns found"
```

### 8.3 Baseline 管理

| 规则 | 说明 |
|------|------|
| 普通 PR 不可修改 baseline | 防止新代码引入禁止词 |
| 清理专项 PR 可修改 baseline | 需 `[Legacy-Cleanup]` 标题前缀 |
| baseline 归零后删除 baseline 文件 | 表示迁移完成 |

---

## 9. 验收标准

### 9.1 迁移完成标准

- [ ] `rg -iP '(?i)step[1-3](?!\s)'` 在非豁免目录返回空
- [ ] CI 禁止词检查步骤通过
- [ ] `naming.md` 发布并引用本 ADR
- [ ] `legacy_naming_governance.md` 更新

### 9.2 长期维护标准

- [ ] 新代码不引入组件旧命名（CI 强制）
- [ ] PR review 检查命名合规性
- [ ] 新成员 onboarding 包含命名规范培训

---

## 10. 参考文档

- [命名约束文档](./naming.md) - 完整命名规范
- [旧组件命名治理规范](./legacy_naming_governance.md) - 详细替换示例
- [ADR: SeekDB Schema 与 Role 命名统一](./adr_seekdb_schema_role_naming.md) - 相关命名决策

---

## 11. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-30 | v1.0 | 初始版本：定义组件旧命名禁止规则、流程编号区分、迁移策略、例外策略 |
| 2026-01-30 | v1.1 | 新增 §4.1 治理范围边界：明确 vendor/third_party 等第三方目录不受治理 |
