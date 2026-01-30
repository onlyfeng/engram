# CI/Nightly/Release Workflow 耦合映射

> 本文档记录 workflow 与 Makefile targets 的耦合关系，以及 detect-changes.outputs 的引用位置。

---

## 1. detect-changes.outputs 引用位置（按 Job 分组）

### 1.1 schema-validate

```yaml
if: >
  schemas_changed == 'true' ||
  logbook_changed == 'true' ||
  gateway_changed == 'true' ||
  seek_changed == 'true'
```

### 1.2 python-logbook-unit

```yaml
if: logbook_changed == 'true'
```

### 1.3 python-gateway-unit

```yaml
if: gateway_changed == 'true'
```

### 1.4 python-seek

```yaml
if: seek_changed == 'true'
```

### 1.5 openmemory-governance-check

```yaml
if: openmemory_governance_changed == 'true'
```

**内部 step 引用:**
- `Check OpenMemory freeze status`: `has_freeze_override_label`
- `Run OpenMemory schema validation`: `upstream_ref_changed`
- `Run OpenMemory sync check`: `upstream_ref_changed` (通过 env `OPENMEMORY_PATCH_FILES_REQUIRED`)
- `Check for overall_status error`: `upstream_ref_changed`
- `Run lock consistency check`: `upstream_ref_changed`

### 1.6 unified-standard

```yaml
if: >
  !failure() && !cancelled() &&
  (stack_changed == 'true' ||
   logbook_changed == 'true' ||
   gateway_changed == 'true' ||
   seek_changed == 'true' ||
   openmemory_governance_changed == 'true' ||
   upstream_ref_changed == 'true')
```

**内部 step 引用:**
- `Check OpenMemory freeze status`: `openmemory_governance_changed`, `has_freeze_override_label`
- `Set up Node.js`: `openmemory_governance_changed`
- `Cache npm dependencies`: `openmemory_governance_changed`
- `Run OpenMemory release preflight`: `openmemory_governance_changed`, `upstream_ref_changed`
- `Run OpenMemory patch check`: `openmemory_governance_changed`, `upstream_ref_changed`
- `Upload OpenMemory patch conflicts`: `openmemory_governance_changed`, `upstream_ref_changed`
- `Generate OpenMemory patch bundle`: `upstream_ref_changed`
- `Upload OpenMemory patch bundle`: `upstream_ref_changed`
- `Parse/Fail OpenMemory patch check`: `openmemory_governance_changed`, `upstream_ref_changed`
- `Run OpenMemory multi-schema isolation test`: `openmemory_governance_changed`, `upstream_ref_changed`
- `Verify upstream_ref change requirements`: `upstream_ref_changed`
- `Run Seek smoke test`: `seek_changed`, `stack_changed`
- `Run Seek Nightly Rebuild Gate (DRY_RUN)`: `seek_changed`, `stack_changed`
- `Save Seek Gate Profile Snapshot`: `seek_changed`, `stack_changed`
- `Collect Seek smoke test results`: `seek_changed`, `stack_changed`
- `Collect Seek diagnostics on failure`: `seek_changed`, `stack_changed`
- `Upload Seek diagnostics on failure`: `seek_changed`, `stack_changed`

### 1.7 openmemory-sdk

```yaml
if: openmemory_sdk_changed == 'true'
```

### 1.8 seek-migrate-dry-run

```yaml
if: >
  !failure() && !cancelled() &&
  (seek_changed == 'true' || stack_changed == 'true') &&
  (has_migrate_dry_run_label == 'true' || inputs.run_seek_migrate_dry_run == 'true')
```

**内部 step 引用:**
- `Run dual-read integration test`: `has_dual_read_label`

---

## 2. Makefile Targets 清单（按 Workflow 分组）

### 2.1 CI Workflow 使用的 Targets

| Target | 使用 Job | 用途 |
|--------|----------|------|
| `ci-precheck` | precheck-static | CI 预检 |
| `verify-build-static` | precheck-static | Dockerfile/compose 静态检查 |
| `openmemory-vendor-check` | precheck-static | OpenMemory vendor 结构检查 |
| `openmemory-lock-format-check` | precheck-static | Lock 文件格式检查 |
| `test-logbook-unit` | python-logbook-unit | Logbook 单元测试 |
| `test-seek-unit` | python-seek | Seek 单元测试 |
| `deploy` | unified-standard | 部署统一栈 |
| `verify-unified` | unified-standard | 统一栈验证 |
| `openmemory-audit` | unified-standard | OpenMemory 制品审计 |
| `openmemory-release-preflight` | unified-standard | 发布前置检查（可选） |
| `openmemory-patches-strict-bundle` | unified-standard | 生成严格模式补丁包 |
| `test-gateway-integration` | unified-standard | Gateway 集成测试 |
| `openmemory-test-multi-schema` | unified-standard | 多 Schema 隔离测试 |
| `seek-run-smoke` | unified-standard | Seek 冒烟测试 |
| `test-seek-pgvector` | unified-standard | Seek PGVector 集成测试 |
| `seek-nightly-rebuild` | unified-standard | Nightly Rebuild (DRY_RUN) |

### 2.2 Nightly Workflow 使用的 Targets

| Target | 使用 Step | 用途 |
|--------|-----------|------|
| `openmemory-vendor-check` | Verify OpenMemory vendor structure | vendor 结构检查 |
| `openmemory-lock-format-check` | Verify OpenMemory.upstream.lock.json format | Lock 文件格式检查 |
| `deploy` | Deploy unified stack | 部署统一栈 |
| `test-logbook-integration` | Run Logbook Integration Tests | MinIO 集成测试 |
| `verify-unified` | (由 acceptance-unified-full 内部调用) | 统一栈验证 |
| `openmemory-release-preflight` | Run OpenMemory release preflight | 发布前置检查（可选） |
| `test-gateway-integration` | (由 acceptance-unified-full 内部调用) | Gateway 集成测试 |
| `test-seek-pgvector` | Run Seek PGVector integration tests | PGVector 集成测试 |
| `seek-migrate-dry-run` | Run Seek Collection Migrate (dry-run) | 迁移脚本验证 |
| `seek-run-smoke` | Run Seek Smoke Test | Seek 冒烟测试 |
| `seek-nightly-rebuild` | Run Seek Nightly Rebuild | Nightly Rebuild 流程 |
| `test-seek-pgvector-migration-drill` | Run Seek PGVector Migration Drill Test | 迁移演练测试 |
| `verify-build-static` | Docker Build Verification | 静态构建检查 |
| `verify-build` | Docker Build Verification | Docker 构建验证 |
| **`acceptance-unified-full`** | **Run acceptance-unified-full** | **完整验收测试（核心验证链）** |

> **说明**: v1.11.0+ 架构中，`verify-unified` 和 `test-gateway-integration` 已收敛到 `acceptance-unified-full` 内部执行。

### 2.3 Release Workflow 使用的 Targets

| Target | 使用 Step | 用途 |
|--------|-----------|------|
| `verify-build-static` | Verify build static | 静态构建检查 |
| `verify-build` | Verify build (Docker) | Docker 构建验证 |
| `deploy` | Deploy unified stack | 部署统一栈 |
| `verify-unified` | Verify unified stack (FULL mode) | 统一栈验证 (FULL) |
| `test-gateway-integration` | Run Gateway integration tests | Gateway 集成测试 |

---

## 3. Makefile Targets 完整清单

### 3.1 部署与服务管理

| Target | 说明 |
|--------|------|
| `deploy` | 完整部署（预检 + 启动所有服务） |
| `up` | 启动所有服务（含自动迁移） |
| `down` | 停止所有服务 |
| `restart` | 重启所有服务 |
| `up-logbook` | 启动 Logbook（PostgreSQL + 迁移） |
| `down-logbook` | 停止 Logbook 服务 |
| `up-openmemory` | 启动 OpenMemory |
| `down-openmemory` | 停止 OpenMemory 服务 |
| `up-gateway` | 启动 Gateway + Worker |
| `down-gateway` | 停止 Gateway 服务 |

### 3.2 迁移与数据管理

| Target | 说明 |
|--------|------|
| `migrate` | 执行所有迁移 |
| `migrate-logbook` | Logbook 数据库迁移 |
| `migrate-om` | OpenMemory 数据库迁移 |
| `migrate-seek` | Seek Index 数据库迁移 |
| `migrate-precheck` | 迁移预检 |
| `verify-permissions` | 验证数据库权限 |
| `backup` | 备份 Engram schema |
| `backup-om` | 备份 OpenMemory schema |
| `backup-full` | 全库备份 |
| `restore` | 恢复备份 |
| `cleanup-om` | 清理 OpenMemory schema |

### 3.3 测试相关

| Target | 说明 |
|--------|------|
| `test` | 运行所有测试 |
| `test-precheck` | 测试预检功能 |
| `test-logbook` | Logbook 所有测试 |
| `test-logbook-unit` | Logbook 单元测试 |
| `test-logbook-integration` | Logbook 集成测试（需 Docker） |
| `test-seek` | Seek 分块稳定性测试 |
| `test-seek-unit` | Seek 单元测试 |
| `test-seek-all` | Seek 所有测试 |
| `test-seek-pgvector` | Seek PGVector 集成测试 |
| `test-seek-pgvector-e2e` | Seek PGVector E2E 测试 |
| `test-seek-pgvector-migration-drill` | Seek 迁移演练测试 |
| `test-gateway-integration` | Gateway 集成测试 |
| `test-gateway-integration-full` | Gateway 完整集成测试 |

### 3.4 验证相关

| Target | 说明 |
|--------|------|
| `verify-build` | Docker 构建边界校验 |
| `verify-build-static` | 静态检查（Dockerfile/compose） |
| `verify-bucket-governance` | MinIO/S3 Bucket 治理策略验证 |
| `verify-pgvector` | pgvector 扩展验证 |
| `verify-unified` | 统一栈验证（自动模式） |
| `verify-stepwise` | 统一栈验证（stepwise 模式） |
| `verify-all` | 综合验证 |
| `verify-local` | 本地聚合验证 |
| `validate-schemas` | JSON Schema 校验 |
| `validate-schemas-json` | JSON Schema 校验（JSON 输出） |
| `validate-workflows` | Workflow 文件校验 |
| `validate-workflows-json` | Workflow 校验（JSON 输出） |
| `validate-workflows-strict` | Workflow 校验（严格模式） |

### 3.5 OpenMemory 相关

| Target | 说明 |
|--------|------|
| `openmemory-sync` | OpenMemory 同步（检查 + 建议） |
| `openmemory-sync-check` | 检查 OpenMemory 一致性 |
| `openmemory-sync-apply` | 应用补丁 |
| `openmemory-sync-verify` | 校验补丁是否落地 |
| `openmemory-sync-suggest` | 输出升级建议 |
| `openmemory-schema-validate` | JSON Schema 校验 |
| `openmemory-lock-format-check` | Lock 文件格式检查 |
| `openmemory-vendor-check` | Vendor 结构检查 |
| `openmemory-upstream-fetch` | 获取上游代码 |
| `openmemory-upstream-sync` | 同步上游代码 |
| `openmemory-base-snapshot` | 下载基线快照 |
| `openmemory-patches-generate` | 生成补丁文件 |
| `openmemory-patches-backfill` | 回填补丁 SHA256 |
| `openmemory-patches-strict-bundle` | 生成严格模式补丁包 |
| `openmemory-upgrade-preview` | 预览上游同步变更 |
| `openmemory-upgrade-sync` | 执行上游同步 |
| `openmemory-upgrade-promote` | 更新 lock 文件 |
| `openmemory-build` | 构建 OpenMemory 镜像 |
| `openmemory-pre-upgrade-backup` | 升级前备份（开发） |
| `openmemory-pre-upgrade-backup-full` | 升级前备份（生产） |
| `openmemory-pre-upgrade-snapshot-lib` | 升级前归档 libs |
| `openmemory-upgrade-check` | OpenMemory 升级验证 |
| `openmemory-upgrade-prod` | 生产环境升级 |
| `openmemory-rollback` | 回滚 OpenMemory 升级 |
| `openmemory-test-multi-schema` | 多 Schema 隔离测试 |
| `openmemory-audit` | 审计镜像 digest |
| `openmemory-release-preflight` | 发布前置检查聚合 |

### 3.6 Seek 相关

| Target | 说明 |
|--------|------|
| `seek-deps` | 安装 Seek 依赖 |
| `seek-index` | Seek 索引同步 |
| `seek-query` | Seek 证据检索 |
| `seek-check` | Seek 一致性校验 |
| `seek-run-smoke` | Seek 冒烟测试 |
| `seek-nightly-rebuild` | Nightly Rebuild 流程 |
| `seek-migrate-dry-run` | 迁移 dry-run |
| `seek-migrate-replay-small` | 小批量迁移回放 |

### 3.7 CI/Release 相关

| Target | 说明 |
|--------|------|
| `ci-precheck` | CI 预检 |
| `ci-backup` | CI 备份 |
| `ci-deploy` | CI 完整部署 |
| `release-precheck` | 发布预检 |
| `release-backup-dev` | 发布前备份（开发） |
| `release-backup-prod` | 发布前备份（生产） |
| `release-rollback-db` | 发布回滚 |

### 3.8 验收测试

| Target | 说明 |
|--------|------|
| `acceptance-unified-min` | 最小验收测试 |
| `acceptance-unified-full` | 完整验收测试 |
| `logbook-smoke` | Logbook 冒烟测试 |

### 3.9 回填相关

| Target | 说明 |
|--------|------|
| `logbook-backfill-evidence` | 回填 evidence_uri |
| `logbook-backfill-chunking` | 回填 chunking_version |
| `logbook-backfill-all` | 执行所有回填 |

### 3.10 日志与清理

| Target | 说明 |
|--------|------|
| `logs` | 查看服务日志 |
| `logs-migrate` | 查看迁移日志 |
| `logs-logbook` | 查看 Logbook 日志 |
| `logs-openmemory` | 查看 OpenMemory 日志 |
| `logs-gateway` | 查看 Gateway 日志 |
| `ps` | 查看服务状态 |
| `ps-logbook` | 查看 Logbook 状态 |
| `ps-openmemory` | 查看 OpenMemory 状态 |
| `ps-gateway` | 查看 Gateway 状态 |
| `clean-logbook` | 清理 Logbook 缓存 |
| `clean-gateway` | 清理 Gateway 缓存 |
| `clean-all` | 清理所有缓存 |

---

## 4. 环境变量与 Workflow 耦合

### 4.1 upstream_ref_changed 触发的严格检查

当 `upstream_ref_changed == 'true'` 时，以下检查变为强制：

1. **schema-validate**: `--schema-strict` 模式
2. **openmemory-sync-check**: `OPENMEMORY_PATCH_FILES_REQUIRED=true`
3. **lock-consistency-check**: `--strict` 模式
4. **openmemory-multi-schema-test**: 强制执行
5. **generate-patch-bundle**: 生成 CI artifact

### 4.2 has_freeze_override_label 触发的流程

当 `has_freeze_override_label == 'true'` 时：

1. 跳过冻结状态检查
2. 强制校验 Override Reason（≥20 字符）
3. 在 Summary 中显示 Override 状态

---

---

## 5. Acceptance 产物归档路径

### 5.1 CI Workflow Acceptance 产物

| 产物目录 | 归档 Artifact 名称 | 保留天数 | 说明 |
|----------|-------------------|----------|------|
| `.artifacts/acceptance-unified-min/` | `unified-verification-results-{profile}` | 14 | acceptance-unified-min 核心产物 |
| `.artifacts/acceptance-runs/` | `acceptance-run-records-{profile}-{run_number}` | 30 | 验收运行记录（JSON） |
| `.artifacts/acceptance-matrix.md` | `acceptance-matrix-{profile}-{run_number}` | 30 | 验收矩阵摘要（Markdown） |
| `.artifacts/acceptance-matrix.json` | `acceptance-matrix-{profile}-{run_number}` | 30 | 验收矩阵数据（JSON） |
| `.artifacts/verify-results.json` | `unified-verification-results-{profile}` | 14 | verify-unified 结果 |

### 5.2 Nightly Workflow Acceptance 产物

| 产物目录 | 归档 Artifact 名称 | 保留天数 | 说明 |
|----------|-------------------|----------|------|
| `.artifacts/acceptance-unified-full/` | `nightly-acceptance-unified-full-{run_number}` | 30 | acceptance-unified-full 完整产物 |
| `.artifacts/acceptance-runs/` | `nightly-acceptance-unified-full-{run_number}` | 30 | 验收运行记录（JSON） |
| `.artifacts/verify-results.json` | `nightly-acceptance-unified-full-{run_number}` | 30 | verify-unified 结果 |
| `.artifacts/acceptance-matrix.md` | `nightly-acceptance-matrix-{run_number}` | 30 | 验收矩阵摘要 |
| `.artifacts/acceptance-matrix.json` | `nightly-acceptance-matrix-{run_number}` | 30 | 验收矩阵数据 |

### 5.3 产物文件内容说明

| 文件 | 格式 | 内容 |
|------|------|------|
| `summary.json` | JSON | 验收摘要：name、result、failed_step、start/end、duration_seconds、environment |
| `steps.log` | Text | 步骤执行日志（带时间戳） |
| `verify-results.json` | JSON | verify-unified 详细结果（健康检查、API 测试、降级测试） |
| `test-results-index.json` | JSON | 测试报告文件索引 |
| `<timestamp>_<name>.json` | JSON | 标准化验收记录（record_acceptance_run.py 生成） |

---

## 6. 版本控制

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v1.1 | 2026-01-30 | 新增 Acceptance 产物归档路径章节：CI/Nightly acceptance 产物目录、归档名称、保留天数 |
| v1.0 | 2026-01-30 | 初始版本，记录耦合关系 |
