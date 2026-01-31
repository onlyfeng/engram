# ADR: 文档信息架构与边界决策

| 元数据 | 值 |
|--------|-----|
| **状态** | Accepted |
| **日期** | 2026-01-30 |
| **决策者** | Engram 核心团队 |
| **影响范围** | 所有文档（`README.md`、`docs/`、`apps/*/docs/`） |

---

## 背景

随着 Engram 项目演进，文档散落在多个位置：

- 根目录 `README.md`
- `docs/` 目录各子文件夹
- `apps/*/docs/` 组件内部文档
- `apps/*/README.md` 组件说明

这导致以下问题：

1. **重复内容**：同一主题在多处有不同版本，难以维护一致性
2. **职责不清**：不确定某类内容应放在哪里
3. **查找困难**：缺乏统一入口，新成员难以快速定位文档
4. **更新遗漏**：修改一处忘记同步其他位置

---

## 决策

### 1. 单一权威来源原则（Single Source of Truth）

**每个主题只能有一个 canonical 文档**。其他位置的相关内容必须以 stub/redirect 形式指向 canonical。

### 2. 三层文档架构

```
README.md (根目录)
├── 受众：外部使用者（运维、集成方、首次接触者）
├── 内容：Quickstart、部署、配置、常见问题
├── 原则：保持简洁，深入内容通过链接指向 docs/
└── 长度：控制在 500 行以内

docs/
├── 受众：内部开发者 + 需要深入了解的使用者
├── 内容：
│   ├── architecture/  → ADR、命名规范、全局设计决策
│   ├── contracts/     → 组件间接口契约、数据流定义
│   ├── <component>/   → 组件设计、架构、对外接口
│   └── README.md      → 文档中心总入口
└── 原则：按主题组织，单一权威来源

apps/*/docs/ (可选)
├── 受众：该组件的开发者
├── 内容：组件内部实现细节、本地开发技巧
├── 原则：
│   ├── 仅包含该组件特有的开发细节
│   ├── 跨组件内容必须放在 docs/
│   └── 与 docs/ 重叠内容改为 stub
└── 约束：不得与 docs/<component>/ 重复
```

### 3. 重复文档处理规则

| 场景 | 处理方式 |
|------|----------|
| 发现重复内容 | 选定一个位置为 canonical，其他改为 stub |
| 两处内容有差异 | 合并到 canonical，删除过时内容 |
| 需要在多处展示 | canonical + stub（含链接） |
| 历史遗留文档 | 评估后迁移或删除 |

### 4. Stub 模板

当需要在非 canonical 位置保留文档入口时，使用以下模板：

```markdown
# [文档标题]

> **Canonical 文档**: [docs/README.md](../README.md)
>
> 本文件为 stub，请参阅上述 canonical 文档获取最新内容。

## 快速链接

- [主要内容](../README.md#文档导航)
- [相关章节](../README.md#文档中心)
```

### 5. 内容分类指南

| 内容类型 | Canonical 位置 | 示例 |
|----------|----------------|------|
| 快速开始、部署 | `README.md` | 三行部署命令 |
| 环境变量参考 | `README.md` | 完整 ENV 列表 |
| 组件架构设计 | `docs/<component>/` | Gateway 策略引擎 |
| 组件间契约 | `docs/contracts/` | Gateway ↔ Logbook 接口 |
| 全局命名规范 | `docs/architecture/` | naming.md |
| ADR | `docs/architecture/` | 本文档 |
| 组件本地开发 | `apps/<component>/docs/` | 调试技巧（可选） |
| API 参考 | `docs/<component>/` | REST/MCP 接口定义 |

### 6. 文档入口规范

- `docs/README.md` 为文档中心总入口
- 每个 `docs/<component>/` 目录必须有 `00_overview.md` 作为该组件入口
- `README.md`（根目录）中的"详细文档"章节链接到 `docs/`

---

## 迁移原则

### 现有文档迁移

1. **评估现有文档**
   - 识别所有文档位置
   - 标记重复内容
   - 确定每个主题的 canonical 位置

2. **迁移步骤**
   ```
   1. 将内容迁移到 canonical 位置
   2. 原位置改为 stub（或删除）
   3. 更新所有内部链接
   4. 在 docs/README.md 中注册
   ```

3. **迁移优先级**
   - P0：有重复的高频访问文档
   - P1：架构决策、契约文档
   - P2：组件实现细节

### apps/*/docs/ 迁移清单

对于 `docs/logbook/` 等目录中的文档：

| 文档 | 处理 | 说明 |
|------|------|------|
| `00_overview.md` | 评估 | 与 `docs/logbook/00_overview.md` 合并或 stub |
| `01_architecture.md` | 评估 | 可能与 `docs/logbook/01_architecture.md` 重复 |
| `02_tools_contract.md` | 迁移 | 移至 `docs/contracts/` |
| 本地开发指南 | 保留 | 仅限组件特有内容 |

---

## 后果

### 正面

- **一致性**：每个主题只有一个权威版本
- **可维护性**：更新一处即可，无需同步多份
- **可发现性**：统一入口便于查找
- **清晰边界**：明确什么内容放在哪里

### 负面

- **迁移成本**：需要整理现有文档
- **链接维护**：stub 需要保持链接有效
- **学习成本**：团队需要了解新规范

### 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 迁移遗漏 | CI 检查重复内容、定期审计 |
| 链接失效 | CI 检查死链 |
| 规范遗忘 | PR 模板提醒、Code Review 检查 |

---

## 合规检查

### CI 建议检查项

```bash
# 1. 检查是否有重复标题（潜在重复文档）
rg -l '# (概览|Overview)' docs/ apps/*/docs/

# 2. 检查 stub 链接有效性
# 可通过 markdown-link-check 工具

# 3. 检查 docs/<component>/ 是否有 00_overview.md
for dir in docs/logbook docs/gateway docs/seekdb docs/openmemory; do
  [ -f "$dir/00_overview.md" ] || echo "Missing: $dir/00_overview.md"
done
```

### 定期审计

- **频率**：每季度一次
- **内容**：检查重复、死链、过时内容
- **责任人**：文档 Owner（轮值）

---

## 参考

- [docs/README.md](../README.md) - 文档中心总入口
- [naming.md](naming.md) - 组件命名规范
- [Google Documentation Best Practices](https://google.github.io/styleguide/docguide/best_practices.html)

---

## 变更记录

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-30 | v1.0 | 初始版本：建立文档信息架构 |
