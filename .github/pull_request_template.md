## Summary

<!-- 简要描述本 PR 的变更内容 -->

## Changes

<!-- 列出主要变更点 -->

- 

## Test Plan

<!-- 描述测试方案或验证步骤 -->

- [ ] 本地测试通过
- [ ] CI 检查通过
- [ ] MCP/Gateway 最小回归矩阵已执行（当修改 `src/engram/gateway/api_models.py`/`src/engram/gateway/routes.py`/`src/engram/gateway/middleware.py`/`scripts/ops/mcp_doctor.py` 时必须执行）

<!-- MCP/Gateway 最小回归矩阵（如适用）:
pytest scripts/tests/test_mcp_doctor.py -q
pytest tests/gateway/test_mcp_cors_preflight.py -q
pytest tests/gateway/test_mcp_jsonrpc_contract.py -q

python scripts/ops/mcp_doctor.py --gateway-url http://127.0.0.1:8787
# 需要鉴权时（示例）
python scripts/ops/mcp_doctor.py --gateway-url http://127.0.0.1:8787 --header "Authorization: Bearer <token>"
-->

## Workflow Contract / CI Shared Files Checklist

- [ ] **是否修改以下文件**：`.github/workflows/ci.yml`、`Makefile`、`scripts/ci/workflow_contract.v1.json`
- [ ] **是否执行版本号 bump**：`python scripts/ci/bump_workflow_contract_version.py <major|minor|patch> --message "..."`（或 `make bump-workflow-contract-version ...`）并说明为何选择该等级
- [ ] **是否执行并粘贴**最小验证命令集的通过日志摘要
- [ ] **如文档受控块有变更**，已运行 `make update-workflow-contract-docs` 且 `make check-workflow-contract-docs-generated` 通过
- [ ] （可选）附 `artifacts/workflow_contract_drift.md` 与 `artifacts/workflow_contract_suggestions.md` 关键片段/链接

## Checklist

- [ ] 代码遵循项目命名规范
- [ ] **无阶段编号式旧别名引入**（详见 [旧组件命名治理](docs/architecture/legacy_naming_governance.md)）
- [ ] 文档已更新（如适用）
- [ ] 文档流程编号格式正确（详见 [流程编号格式规范](docs/architecture/naming.md#24-流程编号格式规范)）
- [ ] 未修改 CI baseline 文件（除非是清理专项 PR）
- [ ] **大型变更按主题拆分为独立提交**（详见 [提交拆分策略](docs/architecture/iteration_3_plan.md)）

### CI Baseline 变更检查

> 如修改 `scripts/ci/mypy_baseline.txt` 且导致**净增**错误，请填写此节（门禁脚本 `check_mypy_baseline_policy.py` 会自动检查）。

- **净增原因**：<!-- 简述为何需要增加 baseline 错误，如：新模块引入、外部依赖类型缺失等 -->
- **关联 Issue**：<!-- 填写 #123 或完整 issue URL，用于追踪后续修复 -->
- **Labels**（净增 > 5 时必填）：<!-- 添加 `tech-debt` 或 `type-coverage` 标签 -->

> **阈值说明**：
> - 净增 > 0：需填写本节 + 关联 Issue
> - 净增 > 5：需添加 `tech-debt` 或 `type-coverage` 标签
> - 净增 > 10：需额外审批（建议拆分 PR）
>
> 如未修改 baseline 文件或为净减少，可跳过此节。

### Logbook 相关变更（如适用）

> 详见 [Definition of Done](docs/logbook/05_definition_of_done.md)

- [ ] Schema/DB 变更：SQL 迁移脚本已添加并包含 UP/DOWN
- [ ] Schema/DB 变更：`db_migrate.py --verify` 通过
- [ ] CLI 变更：Makefile target 已添加（如适用）
- [ ] 契约变更：`docs/contracts/*.md` 已更新
- [ ] 验收矩阵已同步更新（如影响验收流程）

### 破坏性变更（如适用）

- [ ] 迁移脚本包含回滚能力
- [ ] Backfill/Repair 命令已提供
- [ ] 版本号已升级（文件名 v1 → v2）
- [ ] 迁移指南已编写

### CI/Workflow Contract 变更检查（如修改以下文件则必填）

> 涉及 `.github/workflows/**`、`Makefile`、`scripts/ci/workflow_contract.v1.json`、`docs/ci_nightly_workflow_refactor/**` 时，请勾选并说明：

- [ ] **已阅读** [CI/Nightly Workflow 维护指南](docs/ci_nightly_workflow_refactor/maintenance.md)
- [ ] **已同步更新** `scripts/ci/workflow_contract.v1.json`（如新增/修改 job、step、output key）
- [ ] **已同步更新** `docs/ci_nightly_workflow_refactor/contract.md`（如影响合约基准）
- [ ] **已运行** `make validate-workflows` 本地验证通过
- [ ] **已生成变更证据**：`python scripts/ci/generate_workflow_contract_snapshot.py --output artifacts/workflow_snapshot_before.json` 与 `python scripts/ci/generate_workflow_contract_snapshot.py --output artifacts/workflow_snapshot_after.json`（或至少 `make workflow-contract-drift-report-all` + `make workflow-contract-suggest`）
- [ ] **PR 描述已粘贴**建议摘要（counts）与关键差异（快照 diff 或 drift report 重点）
- [ ] **变更说明**：<!-- 简述变更内容及影响范围 -->

> 如未修改上述文件，可跳过此节。

### 迭代文档变更（如修改以下文件则必填）

> 涉及 `.iteration/**`、`docs/acceptance/iteration_*_*.md`、`docs/acceptance/00_acceptance_matrix.md` 时，请勾选并说明：
> 详见 [迭代文档本地草稿工作流](docs/dev/iteration_local_drafts.md) 和 [ADR: 迭代文档工作流](docs/architecture/adr_iteration_docs_workflow.md)

- [ ] **已阅读** [迭代文档本地草稿工作流](docs/dev/iteration_local_drafts.md)
- [ ] **已运行** `make check-iteration-docs` 本地验证通过（或确认 CI 会自动检查）
- [ ] **未引入 .iteration/ 链接**（禁止在文档中链接到 .iteration/ 目录）
- [ ] **SUPERSEDED 声明一致**（如标记迭代为已取代，regression 文件顶部有正确声明）
- [ ] **占位符/模板说明已清理**（晋升后的文件已移除 `<!-- 模板说明 -->` 区块，已替换所有 `{PLACEHOLDER}` 占位符）
- [ ] **草稿分享方式正确**（若需共享草稿，使用 `make iteration-export N=<编号>` 导出为 zip 包或目录，或直接晋升为 SSOT PLANNING 状态，禁止在文档中链接 `.iteration/`）

> 如未修改上述文件，可跳过此节。
> 
> **便捷命令**：
> - `make iteration-init N=<编号>` 初始化本地草稿
> - `make iteration-promote N=<编号>` 晋升草稿到 SSOT
> - `make iteration-export N=<编号>` 导出草稿为 zip（推荐用于分享）
> - `make iteration-snapshot N=<编号>` 快照 SSOT 到本地只读副本（⚠️ 不可 promote 覆盖旧编号）

### 提交前检查

提交前运行以下命令验证无旧阶段别名/历史命名：

```bash
python scripts/check_no_legacy_stage_aliases.py --fail
```

## Related Issues

<!-- 关联的 Issue 编号，如 #123 -->

