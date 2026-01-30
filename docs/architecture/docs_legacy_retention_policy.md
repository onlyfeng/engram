# 文档遗留资产保留策略

> **状态**: Accepted  
> **生效日期**: 2026-01-30  
> **适用范围**: `docs/legacy/`、`scripts/docs/legacy/` 及所有标记为遗留的文档

---

## 1. 分类标准

所有文档资产按以下五类进行分类管理：

| 分类 | 定义 | 保留期限 | 典型示例 |
|------|------|----------|----------|
| **canonical** | 单一权威文档，当前生效 | 永久（需持续维护） | `docs/logbook/00_overview.md` |
| **stub** | 重定向文件，指向 canonical 文档 | 直到所有引用更新完成 | `apps/*/docs/` 中的重定向文件 |
| **legacy-audit** | 历史性文档/脚本，保留用于审计追溯 | 永久（只读，不再更新） | 迁移脚本、历史术语表 |
| **external-reference** | 外部参考资料，非项目 canonical | 定期审查（6 个月） | 第三方框架设计参考 |
| **delete** | 无保留价值，应删除 | 立即删除 | 过时草稿、重复内容 |

### 1.1 分类判定规则

```
文档是否为项目的权威说明？
├── 是 → canonical
└── 否 → 是否为指向 canonical 的重定向？
    ├── 是 → stub
    └── 否 → 是否记录历史决策/迁移过程？
        ├── 是 → legacy-audit
        └── 否 → 是否为外部参考资料？
            ├── 是 → external-reference
            └── 否 → delete
```

---

## 2. 处置决策树

处置遗留文档时，必须按以下顺序执行：

```
┌─────────────────────────────────────────────────────────────────┐
│                         处置决策流程                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  步骤 1: 更新引用                                                │
│  ├── 搜索所有指向该文档的链接                                      │
│  ├── 更新链接指向 canonical 文档                                  │
│  └── 运行 `make docs-check` 验证                                 │
│                                                                 │
│          ↓                                                      │
│                                                                 │
│  步骤 2: 创建 Stub 或迁移内容                                     │
│  ├── 如有独特内容 → 迁移到 canonical 文档                          │
│  ├── 如需保留路径 → 转为 stub（含重定向说明）                       │
│  └── 如为审计需求 → 移至 legacy 目录并标记                         │
│                                                                 │
│          ↓                                                      │
│                                                                 │
│  步骤 3: 删除或归档                                               │
│  ├── 无保留价值 → 删除文件                                        │
│  ├── 有审计价值 → 移至 legacy 目录                                │
│  └── 更新索引文件（README.md）                                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 处置前检查清单

- [ ] 已搜索所有引用（`make docs-check`）
- [ ] 已确认 canonical 文档存在
- [ ] 已更新所有指向该文档的链接
- [ ] 已通知相关 CODEOWNERS
- [ ] 已在 PR 中说明处置理由

---

## 3. 当前资产裁决

### 3.1 裁决汇总表

| 资产路径 | 分类 | 裁决 | 理由 |
|----------|------|------|------|
| `docs/legacy/old/LangChain.md` | **external-reference** | 迁移或删除 | 非项目 canonical，为外部框架参考；若需保留应移至参考区并标注 |
| `scripts/docs/legacy/migrate_docs.py` | **legacy-audit** | 保留 | 迁移工具已完成使命，保留用于审计追溯迁移过程 |
| `scripts/docs/legacy/docs_migration_map.json` | **legacy-audit** | 保留 | 迁移映射配置，记录文档迁移源→目标关系，有审计价值 |

### 3.2 详细裁决说明

#### `docs/legacy/old/LangChain.md`

- **分类**: external-reference
- **裁决**: 移至参考区或删除
- **理由**:
  - 内容为 LangChain 多智能体架构参考，非 Engram 项目的 canonical 文档
  - 包含外部链接 `blog.langchain.com`
  - 目录结构 `old/` 暗示已过时
- **后续动作**:
  - 方案 A（推荐）: 删除文件，如需参考可直接链接原文
  - 方案 B: 移至 `docs/reference/external/` 并添加 `> **外部参考**` 标注

#### `scripts/docs/legacy/migrate_docs.py`

- **分类**: legacy-audit
- **裁决**: 保留，维持现状
- **理由**:
  - 文件头已标注 `[HISTORICAL] 已完成迁移，仅供审计`
  - 记录文档迁移的完整实现逻辑
  - 如需类似迁移可参考本实现
- **后续动作**: 无需变更

#### `scripts/docs/legacy/docs_migration_map.json`

- **分类**: legacy-audit
- **裁决**: 保留，维持现状
- **理由**:
  - 文件已标注 `_historical_note` 说明迁移完成
  - 记录完整的源→目标映射关系
  - 提供迁移过程的可追溯性
- **后续动作**: 无需变更

---

## 4. 约束规则

### 4.1 Canonical 文档链接约束

**规则**: Canonical 文档**不得**直接链接到 external-reference 类文档，除非满足以下条件：

1. 链接位于文档末尾的「参考资料」或「外部链接」区域
2. 链接带有明确标注，说明其为外部参考

**合规示例**:

```markdown
## 参考资料

> **外部参考**: 以下链接指向外部资源，非本项目 canonical 文档

- [LangChain Multi-Agent Architecture](https://blog.langchain.com/...) - 多智能体架构设计参考（外部）
```

**违规示例**:

```markdown
## 架构设计

本项目采用 [LangChain 的 Skills 模式](../legacy/old/LangChain.md) 设计...
```

### 4.2 Legacy 目录约束

- `docs/legacy/` 和 `scripts/docs/legacy/` 中的文件默认为**只读**
- 禁止向 legacy 目录添加新的 canonical 文档
- Legacy 文件的修改仅限于：添加历史标注、修复格式问题

### 4.3 Stub 文件约束

Stub 文件必须包含以下内容：

```markdown
# [原文档标题]

> **Canonical 文档**: [链接到 canonical 文档]
>
> 本文件为 stub，请参阅上述 canonical 文档获取最新内容。
> 
> **迁移日期**: YYYY-MM-DD
```

---

## 5. 变更流程

### 5.1 PR 要求

所有涉及文档分类、处置、迁移的变更必须通过 PR 提交，并满足：

| 要求 | 说明 |
|------|------|
| **PR 标题** | 必须包含 `[docs]` 或 `[docs-legacy]` 前缀 |
| **PR 描述** | 必须说明：变更的文档列表、分类判定、处置理由 |
| **关联 Issue** | 如有 Issue 需关联 |

### 5.2 Review 要求

| 变更类型 | 最低 Reviewer 数量 | 必须包含的 Reviewer |
|----------|-------------------|---------------------|
| 新增 canonical 文档 | 1 | 相关组件 CODEOWNER |
| 删除任何文档 | 2 | docs/ CODEOWNER + 相关组件 CODEOWNER |
| 分类裁决变更 | 2 | docs/ CODEOWNER |
| Stub 创建/更新 | 1 | 相关组件 CODEOWNER |
| Legacy 标注更新 | 1 | docs/ CODEOWNER |

### 5.3 CI 门禁项

以下检查必须通过才能合并 PR：

| 检查项 | 命令 | 说明 |
|--------|------|------|
| 链接有效性 | `make docs-check` | 所有内部链接必须有效 |
| 重复检测 | `make docs-lint` | 不得引入重复文档 |
| 命名规范 | `make lint-naming` | 文件名/内容符合命名规范 |

### 5.4 CODEOWNERS 配置

以下为 `.github/CODEOWNERS` 中文档相关的所有权配置：

```
# 文档: 文档中心及各类文档目录
/docs/README.md                                  @engram/docs-owners
/docs/architecture/**                            @engram/docs-owners @engram/architecture-owners
/docs/reference/**                               @engram/docs-owners
/docs/guides/**                                  @engram/docs-owners
/docs/legacy/**                                  @engram/docs-owners
```

**所有权说明**:

| 路径 | Owner | 职责 |
|------|-------|------|
| `/docs/README.md` | `@engram/docs-owners` | 文档中心索引的维护 |
| `/docs/architecture/**` | `@engram/docs-owners` `@engram/architecture-owners` | 架构 ADR、命名规范、治理策略 |
| `/docs/reference/**` | `@engram/docs-owners` | API 参考、环境变量等技术参考文档 |
| `/docs/guides/**` | `@engram/docs-owners` | 用户指南、集成教程 |
| `/docs/legacy/**` | `@engram/docs-owners` | 遗留文档的归档管理 |

> **注意**: `/docs/contracts/**` 由 `@engram/gateway-owners` 和 `@engram/logbook-owners` 共同负责，
> 属于跨边界契约文档，参见 CODEOWNERS 中的「跨边界契约」部分。

---

## 6. 附录

### 6.1 Legacy 目录结构

```
docs/
└── legacy/
    └── old/
        └── LangChain.md          # external-reference: 待处置

scripts/
└── docs/
    └── legacy/
        ├── migrate_docs.py       # legacy-audit: 迁移脚本
        └── docs_migration_map.json  # legacy-audit: 迁移映射
```

### 6.2 相关文档

| 文档 | 说明 |
|------|------|
| [docs/README.md](../README.md) | 文档中心索引 |
| [naming.md](naming.md) | 命名规范（canonical） |
| [adr_docs_information_architecture.md](adr_docs_information_architecture.md) | 文档架构 ADR |

---

## 变更记录

| 日期 | 变更内容 | 作者 |
|------|----------|------|
| 2026-01-30 | 初始版本：定义分类标准、处置流程、资产裁决 | — |
