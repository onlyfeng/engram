# 迭代 fixtures 漂移治理规范

## 目标与范围

- `tests/iteration/fixtures/` 是迭代脚本输出的快照基线，用于保证行为稳定。
- 覆盖脚本：`render_min_gate_block.py`、`render_iteration_evidence_snippet.py`、`sync_iteration_regression.py` 及其受控块逻辑（`generated_blocks.py`）。
- 回归文档中的受控块（`min_gate_block` / `evidence_snippet`）必须通过脚本刷新，禁止手工编辑区块内容。
- 受控块格式契约见 [iteration_regression_generated_blocks_v1.md](../contracts/iteration_regression_generated_blocks_v1.md)，包含 marker/H2-H3/表格列/空行/emoji 的稳定性承诺与 breaking 判定。

## 变更分级（non-breaking vs breaking）

变更分级遵循 [迭代 Schema 演进策略](iteration_schema_evolution_policy.md)。

- **non-breaking**：新增可选字段、文案/排序调整、默认值变化、输出格式微调。要求刷新对应 fixtures，并确保最小命令集通过。
- **breaking**：字段删除/重命名、marker 格式变更、目录迁移、受控块协议变更。需要按策略完成兼容与迁移，并同步更新 fixtures 与相关文档模板。
- **不确定时**：按 breaking 处理，先补齐策略与兼容说明再提交。
- **受控块契约变更**：按契约文档定义，默认视为 breaking（需同步刷新 fixtures 并补齐迁移说明）；如需变更契约，新增 v2 文档，保留 v1 历史兼容。

## Evidence v2 演进策略（Schema / 模板 / 脚本）

- 当前证据 Schema 以 `schemas/iteration_evidence_v2.schema.json` 为默认（见 `scripts/iteration/iteration_evidence_schema.py`）。
- v1 证据文件仍被支持（历史兼容），严禁在 v1 上做 breaking 变更；新增字段仅在 v2 演进。
- **non-breaking**：可选字段新增、校验规则细化、文案调整 → 更新 v2 schema + 模板 + fixtures。
- **breaking**：字段重命名/删除、结构变更 → 新增 `iteration_evidence_v3.schema.json`，同时更新 `iteration_evidence_schema.py` 默认指向 v3；保留 v2 作为 legacy，不覆盖旧证据。
- 迁移策略：优先通过 `record_iteration_evidence.py` 重新生成需要升级的 evidence；回归文档区块用 `sync_iteration_regression.py` 刷新，禁止手动改 JSON。

## 触发 → 刷新 → 验证矩阵

| 变更范围（文件/目录） | 触发 | 刷新动作 | 验证命令 |
|---|---|---|---|
| `configs/iteration_gate_profiles.v1.json`、`schemas/iteration_gate_profiles_v1.schema.json`、`scripts/iteration/render_min_gate_block.py`、`scripts/iteration/update_render_min_gate_block_fixtures.py` | 最小门禁输出变化 | `python scripts/iteration/update_render_min_gate_block_fixtures.py` 或 `python scripts/iteration/update_iteration_fixtures.py --min-gate` | `pytest tests/iteration/test_render_min_gate_block.py -q` + `python scripts/ci/check_iteration_gate_profiles_contract.py` + `python scripts/ci/check_min_gate_profiles_consistency.py` + `make check-schemas` |
| `scripts/iteration/render_iteration_evidence_snippet.py`、`docs/acceptance/evidence/`、`schemas/iteration_evidence_v2.schema.json`（含 v1 legacy） | 证据片段渲染或 Schema 变更 | `python scripts/iteration/update_iteration_fixtures.py --evidence-snippet` | `pytest tests/iteration/test_render_iteration_evidence_snippet.py -q` + `make check-iteration-evidence` |
| `scripts/iteration/sync_iteration_regression.py`、`scripts/iteration/generated_blocks.py` | 受控块插入/同步逻辑变更 | `python scripts/iteration/update_iteration_fixtures.py --sync-regression --iteration-cycle` | `pytest tests/iteration/test_sync_iteration_regression.py -q` |
| `docs/acceptance/iteration_*_regression.md`、`docs/acceptance/_templates/` | 受控块内容与脚本输出不一致 | `python scripts/iteration/sync_iteration_regression.py <N> --write` 或 `python scripts/iteration/update_min_gate_block_in_regression.py <N>` | `make check-iteration-docs` |
| `configs/iteration_toolchain_drift_map.v1.json`、`schemas/iteration_toolchain_drift_map_v1.schema.json` | rerun 建议映射变更 | 无需刷新 fixtures | `python scripts/ci/check_iteration_toolchain_drift_map_contract.py` + `make check-schemas` |

## 推荐入口（PR diff → rerun 建议）

优先从 PR diff 自动生成 rerun 建议，先确认需要重跑的最小集合：

```bash
make iteration-rerun-advice
make iteration-rerun-advice RANGE=origin/master...HEAD FORMAT=markdown
make iteration-rerun-advice RANGE=origin/master...HEAD FORMAT=json
```

### PR diff 最小集合（profiles / blocks / evidence / schema / cycle）

当 PR diff 覆盖多个领域时，按变更类型组合最小集合；优先用 `make iteration-rerun-advice` 输出的类型作为准绳：

| diff 触及 | change_type | 最小集合示例 |
|---|---|---|
| `configs/iteration_gate_profiles.*` / `render_min_gate_block.py` | `profiles` | `make iteration-min-regression TYPES=profiles` |
| `generated_blocks.py` / `sync_iteration_regression.py` / regression 模板 | `blocks` | `make iteration-min-regression TYPES=blocks` |
| `render_iteration_evidence_snippet.py` / `docs/acceptance/evidence/` | `evidence` | `make iteration-min-regression TYPES=evidence` |
| `schemas/iteration_evidence_v2.schema.json`（或升级 v3） | `schema` | `make iteration-min-regression TYPES=schema` |
| `iteration_cycle.py` / `update_iteration_fixtures.py` | `cycle` | `make iteration-min-regression TYPES=cycle` |

合并多个类型：`make iteration-min-regression TYPES="profiles blocks evidence schema cycle" DRY_RUN=0`

## 推荐入口（最小回归命令集）

确认 change_type 后，优先使用 Make 入口执行或预览最小回归：

```bash
make iteration-min-regression TYPES=profiles DRY_RUN=1
make iteration-min-regression TYPES="profiles blocks" DRY_RUN=0
```

当变更类型已明确或需要绕过 git diff（例如未提交的本地改动、对比范围无法覆盖）时，直接使用脚本入口：

```bash
python scripts/iteration/run_min_iteration_regression.py <change_type>
```

支持的 change_type 与命令映射：

| change_type | 适用变更 | pytest 子集 | make targets |
|---|---|---|---|
| `profiles` | gate profiles / min gate block | `pytest tests/iteration/test_render_min_gate_block.py -q` | `make check-iteration-docs` |
| `blocks` | generated blocks / sync regression | `pytest tests/iteration/test_sync_iteration_regression.py -q` | `make check-iteration-docs` |
| `evidence` | evidence snippet / evidence data | `pytest tests/iteration/test_render_iteration_evidence_snippet.py -q` | `make check-iteration-evidence` |
| `schema` | evidence schema | `pytest tests/iteration/test_render_iteration_evidence_snippet.py -q` | `make check-iteration-evidence` |
| `cycle` | iteration cycle / fixtures refresh | `pytest tests/iteration/test_update_iteration_fixtures.py -q` | `make check-iteration-docs` |

可组合多个类型：`python scripts/iteration/run_min_iteration_regression.py profiles blocks`  
仅查看命令：`python scripts/iteration/run_min_iteration_regression.py <change_type> --dry-run`

## 最小命令集（pytest 子集 + make 子集）

**pytest 子集**（fixtures 漂移自检）：

```bash
pytest tests/iteration/test_render_min_gate_block.py -q
pytest tests/iteration/test_render_iteration_evidence_snippet.py -q
pytest tests/iteration/test_sync_iteration_regression.py -q
```

若 change_type 包含 `cycle`，追加：

```bash
pytest tests/iteration/test_update_iteration_fixtures.py -q
```

**make 子集**（文档与证据约束）：

```bash
make check-iteration-docs
make check-iteration-evidence
```

## 迁移脚本推荐用法（fixtures / evidence / 受控块）

```bash
# 仅刷新指定领域的 fixtures（优先小范围）
python scripts/iteration/update_iteration_fixtures.py --min-gate
python scripts/iteration/update_iteration_fixtures.py --sync-regression
python scripts/iteration/update_iteration_fixtures.py --evidence-snippet
python scripts/iteration/update_iteration_fixtures.py --iteration-cycle

# 如需全量刷新（谨慎使用）
python scripts/iteration/update_iteration_fixtures.py --all

# 回归文档受控块同步（min_gate_block + evidence_snippet）
python scripts/iteration/sync_iteration_regression.py <N> --write

# evidence 重新生成（Schema 升级/字段调整时）
python scripts/iteration/record_iteration_evidence.py <N> --ci-run-url <url>
```

## 失败分流（常见错误与处理）

| 类型 | 常见报错/症状 | 处理动作 | 复验 |
|---|---|---|---|
| 快照差异 | pytest 断言输出与 fixtures 不一致 | 使用 `update_iteration_fixtures.py` 或 `update_render_min_gate_block_fixtures.py` 刷新，确认 diff 仅为预期变化 | 对应 pytest 子集 |
| Schema error | `jsonschema` 校验失败或 `EvidenceParseError` | 修复证据 JSON 或 Schema；如为 breaking 变更先按策略处理 | `make check-iteration-evidence` |
| link error | `make check-iteration-docs` 报告链接错误或 `.iteration/` 引用 | 修正文档链接与引用路径，避免指向本地草稿 | `make check-iteration-docs` |
| 受控块 mismatch | 受控块 marker 缺失或内容滞后 | 使用 `sync_iteration_regression.py` 或 `update_min_gate_block_in_regression.py` 刷新区块 | `make check-iteration-docs` + 对应 pytest |

## 备注

- Fixtures 只通过脚本刷新，不直接手工编辑。
- 若多处文件同步变化，优先按矩阵逐行处理，保证每一步验证可复现。
