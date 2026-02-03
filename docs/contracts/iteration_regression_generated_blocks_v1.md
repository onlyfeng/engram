# Iteration Regression Generated Blocks v1
> 版本: v1.0  
> 生效日期: 2026-02-03  
> 适用范围: `scripts/iteration/generated_blocks.py`、`scripts/iteration/sync_iteration_regression.py`、`scripts/iteration/render_min_gate_block.py`

## 概述
本文档定义迭代回归文档的受控块（`min_gate_block` 与 `evidence_snippet`）契约，确保自动生成的内容在结构与格式上保持稳定，并明确 breaking 判定、迁移策略与刷新/测试要求。

> **版本化原则**：如需变更受控块契约（breaking），必须新增 `iteration_regression_generated_blocks_v2.md`；本文档仅允许非破坏性勘误，保持历史兼容。

## 1. 稳定性承诺（必须保持不变）

### 1.1 Marker 规范
- `min_gate_block`：
  - `<!-- BEGIN GENERATED: min_gate_block profile=<profile> -->`
  - `<!-- END GENERATED: min_gate_block -->`
- `evidence_snippet`：
  - `<!-- BEGIN GENERATED: evidence_snippet -->`
  - `<!-- END GENERATED: evidence_snippet -->`
- `profile` 仅允许 `full` / `regression` / `docs-only` / `ci-only` / `gateway-only` / `sql-only`。

### 1.2 H2/H3 标题与层级
- `min_gate_block` 内固定为：
  - `## 最小门禁命令块`
  - `### 命令表格`
  - `### 一键执行`
  - `### 通过标准`
- `evidence_snippet` 内固定为：
  - `## 验收证据`
  - `### 门禁命令执行摘要`（仅当有命令时出现）
  - `### 整体验收结果`

### 1.3 表格列与列序
- 最小门禁命令表：`| 序号 | 检查项 | 命令 | 通过标准 |`
- 证据信息表：`| 项目 | 值 |`
- 命令执行摘要表：`| 命令 | 结果 | 耗时 | 摘要 |`

### 1.4 空行规则
- BEGIN/END marker 与内容之间各保留一行空行（单空行）。
- 同一受控块内各段落之间以单空行分隔，避免额外空行堆叠。
- 受控块输出以换行结尾（保证文件末尾换行一致性）。

### 1.5 Emoji 规则
- 命令结果图标：
  - `PASS` → `✅`
  - `FAIL` → `❌`
  - 其他结果（如 `SKIP`）→ `⏭️`
- 整体验收结果图标：
  - `PASS` → `✅`
  - `PARTIAL` → `⚠️`
  - `FAIL` → `❌`

## 2. Breaking 判定与迁移策略

### 2.1 Breaking 判定（任一发生即为 breaking）
- Marker 格式变化（文本、大小写、空白规则、`profile` 参数名变更）。
- H2/H3 标题文本、级别或顺序变化。
- 表格列名或列顺序变化。
- 空行规则变化（包含去掉/新增空行造成结构差异）。
- Emoji 映射规则变化或替换为非约定符号。

### 2.2 迁移策略（breaking 变更必做）
1. 在脚本层提供兼容路径（例如在 `generated_blocks.py` 中保留旧 marker 解析，至少覆盖一个迭代周期）。
2. 同步更新 fixtures（见 §3）并确保 diff 仅包含预期变化。
3. 更新回归文档模板与说明文档（`docs/acceptance/_templates/`、`docs/dev/`）。
4. 更新或新增契约测试（`tests/iteration/`）以覆盖新规则。
5. 版本升级：新契约必须新建 `iteration_regression_generated_blocks_v2.md`，保留 v1 以兼容历史。

## 3. 变更后必须运行的刷新命令与最小测试集

### 3.1 一键刷新命令（fixtures）
```bash
python scripts/iteration/update_iteration_fixtures.py --all
```

### 3.2 最小测试集
```bash
pytest tests/iteration/test_render_min_gate_block.py -q
pytest tests/iteration/test_render_iteration_evidence_snippet.py -q
pytest tests/iteration/test_sync_iteration_regression.py -q
make check-iteration-docs
make check-iteration-evidence
```

---

更新时间：2026-02-03
