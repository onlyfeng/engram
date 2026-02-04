# CI/Nightly Workflow 系统索引

> 本文档是 CI/Nightly workflow 合约体系的“系统地图”，用于快速定位 SSOT、生成链路、门禁链路、排障顺序与强耦合文件集合。

---

## 1. SSOT 与 Schema

- **SSOT**：`scripts/ci/workflow_contract.v2.json`
- **Schema**：`scripts/ci/workflow_contract.v2.schema.json`
- **受控文档（由 SSOT 驱动）**：
  - `docs/ci_nightly_workflow_refactor/contract.md`
  - `docs/ci_nightly_workflow_refactor/coupling_map.md`

> 说明：受控块由渲染器生成，手写部分需与 SSOT 同步更新。

---

## 2. 生成链路（工具与产物）

- **受控块渲染**：`scripts/ci/render_workflow_contract_docs.py`
  - 产物：`contract.md` / `coupling_map.md` 中的受控块
- **快照生成**：`scripts/ci/generate_workflow_contract_snapshot.py`
  - 产物：结构化 snapshot JSON（便于前后对比）
- **漂移报告**：`scripts/ci/workflow_contract_drift_report.py`
  - 产物：drift JSON/Markdown
- **更新建议**：`scripts/ci/suggest_workflow_contract_updates.py`
  - 产物：建议清单（JSON/Markdown）

---

## 3. 门禁链路（严格模式优先）

> 对应脚本与 Make target 一览，建议按下列顺序执行或使用 `make workflow-contract-preflight`。

1. **严格校验**：`python scripts/ci/validate_workflows.py --strict`
   - Make target：`make validate-workflows-strict`
2. **Docs Sync**：`scripts/ci/check_workflow_contract_docs_sync.py`
   - Make target：`make check-workflow-contract-docs-sync`
3. **Coupling Map Sync**：`scripts/ci/check_workflow_contract_coupling_map_sync.py`
   - Make target：`make check-workflow-contract-coupling-map-sync`
4. **Doc Anchors**：`scripts/ci/check_workflow_contract_doc_anchors.py`
   - Make target：`make check-workflow-contract-doc-anchors`
5. **Internal Consistency**：`scripts/ci/check_workflow_contract_internal_consistency.py`
   - Make target：`make check-workflow-contract-internal-consistency`
6. **Make Targets Consistency**：`scripts/ci/check_workflow_make_targets_consistency.py`
   - Make target：`make check-workflow-make-targets-consistency`
7. **Version Policy**：`scripts/ci/check_workflow_contract_version_policy.py`
   - Make target：`make check-workflow-contract-version-policy`

---

## 4. 推荐排障顺序（基于 trigger_reasons）

1. **先运行版本策略检查（JSON）**：
   - `python scripts/ci/check_workflow_contract_version_policy.py --json`
2. **查看输出的 `trigger_reasons`**，按触发原因选择对应检查与修复文件集合。

### 4.1 常见 trigger_reasons → 对应检查与修复范围

| trigger_reasons（示例） | 优先检查 | 常见修复文件 |
|---|---|---|
| Phase 1 workflow 文件（ci.yml/nightly.yml） | `validate_workflows.py --strict` + docs/coupling 同步 | `.github/workflows/ci.yml` / `.github/workflows/nightly.yml` / `workflow_contract.v2.json` / `contract.md` / `coupling_map.md` |
| 合约定义 JSON 文件 | internal-consistency + docs/coupling 同步 | `scripts/ci/workflow_contract.v2.json` / `contract.md` / `coupling_map.md` |
| 合约文档（docs/ci_nightly_workflow_refactor/） | docs-sync + doc-anchors | `contract.md` / `coupling_map.md` |
| 合约 JSON Schema | `validate_workflows.py --strict` + 相关 schema 校验 | `scripts/ci/workflow_contract.v2.schema.json` / `validate_workflows.py` / `workflow_contract.v2.json` |
| 合约校验器核心脚本 | `pytest tests/ci/ -q` + strict 校验 | `scripts/ci/validate_workflows.py` / `tests/ci/*.py` |
| 文档同步校验脚本 | `pytest tests/ci/ -q` + docs-sync | `scripts/ci/check_workflow_contract_docs_sync.py` / `tests/ci/*.py` |
| 漂移报告生成脚本 | `pytest tests/ci/ -q` + drift report | `scripts/ci/workflow_contract_drift_report.py` / `tests/ci/*.py` |
| 快照生成脚本 | `pytest tests/ci/ -q` + snapshot | `scripts/ci/generate_workflow_contract_snapshot.py` / `tests/ci/*.py` |
| Makefile CI/workflow 相关目标变更 | make-targets-consistency + version-policy | `Makefile` / `workflow_contract.v2.json` / `coupling_map.md` |

> 实操建议：先定位触发原因，再执行最小必要门禁，最后补齐受控块与版本策略校验。

---

## 5. 强耦合文件与“建议单独 PR”范围

> 以下组合与 `maintenance.md` 的 PR 拆分规则一致，建议单独 PR 或集中处理，避免耦合扩散。

1. **受控块修复（仅文档）**
   - 文件：`contract.md` / `coupling_map.md`
   - 工具：`render_workflow_contract_docs.py`
2. **合约字段/校验逻辑变更（必须 bump）**
   - 文件：`workflow_contract.v2.json` / `workflow_contract.v2.schema.json` / `validate_workflows.py` / 相关 check 脚本
3. **CI/Nightly workflow 编排变更（必须 bump，附 drift/suggest）**
   - 文件：`.github/workflows/ci.yml` / `.github/workflows/nightly.yml`
   - 建议附：`workflow_contract_drift_report.py` / `suggest_workflow_contract_updates.py` 输出
4. **Makefile 目标新增/改名（同步两处）**
   - 文件：`Makefile` / `workflow_contract.v2.json` / `coupling_map.md`

---

## 6. 关联文档

- 维护流程与拆分规则：`docs/ci_nightly_workflow_refactor/maintenance.md`
- 合约详情：`docs/ci_nightly_workflow_refactor/contract.md`
- 产物耦合图：`docs/ci_nightly_workflow_refactor/coupling_map.md`
