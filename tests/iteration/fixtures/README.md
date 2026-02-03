# Iteration fixtures 说明

本目录保存迭代脚本的输出快照，用于稳定性测试与回归验证。除非明确说明，禁止手工修改文件内容。

## 目录说明

- `render_min_gate_block/`：`render_min_gate_block.py` 输出快照
- `render_iteration_evidence_snippet/`：验收证据片段输出快照
- `sync_iteration_regression/`：回归文档同步结果快照
- `iteration_cycle/`：端到端迭代循环快照（生成、同步、校验）
- `iteration_cycle_smoke/`：轻量冒烟快照

## 刷新 fixtures

统一通过脚本刷新（禁止手工编辑），主入口为 `scripts/iteration/update_iteration_fixtures.py`。
仅需刷新最小门禁时可使用 `scripts/iteration/update_render_min_gate_block_fixtures.py`。

**推荐固定 iteration_number（保持快照稳定）**：
- `min_gate_block` / `sync_iteration_regression` / 证据片段：使用 `13`（与当前基线证据与回归文档对齐）
- `iteration_cycle`：使用 `20`（保持端到端循环快照稳定）
- `iteration_cycle_smoke`：使用 `21`（轻量冒烟快照专用）

**原因**：固定迭代编号可避免 fixtures 随新迭代滚动而频繁变更，确保回归对比结果稳定且可复现。

```bash
# 推荐：按需刷新
python scripts/iteration/update_iteration_fixtures.py --min-gate --sync-regression --evidence-snippet

# 或刷新全部
python scripts/iteration/update_iteration_fixtures.py --all

# 仅刷新最小门禁快照
python scripts/iteration/update_render_min_gate_block_fixtures.py
```

## 验证

```bash
pytest tests/iteration/test_render_min_gate_block.py -q
pytest tests/iteration/test_render_iteration_evidence_snippet.py -q
pytest tests/iteration/test_sync_iteration_regression.py -q
```

更多规则与分流策略参见 [迭代 fixtures 漂移治理规范](../../../docs/dev/iteration_fixtures_drift_governance.md)。
