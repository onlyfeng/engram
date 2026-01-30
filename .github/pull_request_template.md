## Summary

<!-- 简要描述本 PR 的变更内容 -->

## Changes

<!-- 列出主要变更点 -->

- 

## Test Plan

<!-- 描述测试方案或验证步骤 -->

- [ ] 本地测试通过
- [ ] CI 检查通过

## Checklist

- [ ] 代码遵循项目命名规范
- [ ] **无阶段编号式旧别名引入**（详见 [旧组件命名治理](docs/architecture/legacy_naming_governance.md)）
- [ ] 文档已更新（如适用）
- [ ] 文档流程编号格式正确（详见 [流程编号格式规范](docs/architecture/naming.md#24-流程编号格式规范)）
- [ ] 未修改 CI baseline 文件（除非是清理专项 PR）

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
- [ ] **变更说明**：<!-- 简述变更内容及影响范围 -->

> 如未修改上述文件，可跳过此节。

### 提交前检查

提交前运行以下命令验证无旧阶段别名/历史命名：

```bash
python scripts/check_no_legacy_stage_aliases.py --fail
```

## Related Issues

<!-- 关联的 Issue 编号，如 #123 -->

