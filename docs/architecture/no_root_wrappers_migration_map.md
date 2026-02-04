# 根目录 Wrapper 模块迁移映射表

> **状态**：当前  
> **更新日期**：2026-02-01  
> **权威数据来源 (SSOT)**：`configs/import_migration_map.json`  
> **关联文档**：[cli_entrypoints.md](cli_entrypoints.md)、[no_root_wrappers_exceptions.md](no_root_wrappers_exceptions.md)

---

## 概述

本文档记录 `ROOT_WRAPPER_MODULES` 列表中每个弃用模块的迁移状态、目标和动作计划，确保文档与代码实现保持一致。

---

## 迁移映射总览

| 模块名 | 现状 | 目标 | 迁移动作 | Owner | 到期日期 |
|--------|------|------|----------|-------|----------|
| `scm_sync_runner` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-runner` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_scheduler` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-scheduler` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_status` | 根目录已移除 | `engram-scm-status` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_reaper` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-reaper` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_worker` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-worker` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_gitlab_commits` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-sync runner` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_gitlab_mrs` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-sync runner` | 仅改引用 | @engram-team | v2.0 |
| `scm_sync_svn` | 根目录已移除，scripts/ 弃用入口 | `engram-scm-sync runner` | 仅改引用 | @engram-team | v2.0 |
| `scm_materialize_patch_blob` | 根目录已移除 | `engram.logbook.materialize_patch_blob` | 已移除 | @engram-team | v2.0 |
| `artifact_audit` | 根目录已移除，运维脚本保留 | `scripts/artifact_audit.py` | 已移除 | @engram-team | v2.0 |
| `artifact_cli` | 根目录已移除 | `engram-artifacts` | 已移除 | @engram-team | v2.0 |
| `artifact_gc` | 根目录已移除 | `engram-artifacts gc` | 已移除 | @engram-team | v2.0 |
| `artifact_migrate` | 根目录已移除 | `engram-artifacts migrate` | 已移除 | @engram-team | v2.0 |
| `db_bootstrap` | 根目录已移除 | `engram-bootstrap-roles` | 已移除 | @engram-team | v2.0 |
| `db_migrate` | 根目录已移除 | `engram-migrate` | 已移除 | @engram-team | v2.0 |
| `logbook_cli_main` | 根目录已移除 | `engram-logbook` | 已移除 | @engram-team | v2.0 |
| `logbook_cli` | 根目录已移除 | `engram-logbook` | 已移除 | @engram-team | v2.0 |
| `identity_sync` | 根目录已移除 | `engram-identity-sync` | 已移除 | @engram-team | v2.0 |

---

## 详细迁移计划

> 注：v2.0 已移除根目录 wrappers，若下方细项与「迁移映射总览」不一致，以总览表为准。

### 1. SCM Sync 相关模块

#### 1.1 scm_sync_runner

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `scm_sync_runner.py` 已移除；`scripts/scm_sync_runner.py` 不存在 |
| **目标** | Console Script `engram-scm-runner` 或 `engram.logbook.cli.scm_sync:runner_main` |
| **迁移动作** | 仅改引用 - 更新所有调用处使用新命令 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.2 scm_sync_scheduler

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `scm_sync_scheduler.py` 已移除；`scripts/scm_sync_scheduler.py` 存在，带弃用警告 |
| **目标** | Console Script `engram-scm-scheduler` 或 `engram.logbook.cli.scm_sync:scheduler_main` |
| **迁移动作** | 仅改引用 - v2.0 移除 scripts/ 入口 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.3 scm_sync_status

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `scm_sync_status.py` 已移除 |
| **目标** | Console Script `engram-scm-status` 或 `engram.logbook.cli.scm_sync:status_main` |
| **迁移动作** | 仅改引用 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.4 scm_sync_reaper

| 字段 | 值 |
|------|-----|
| **现状** | 根目录已移除；`scripts/scm_sync_reaper.py` 存在，带弃用警告 |
| **目标** | Console Script `engram-scm-reaper` 或 `engram.logbook.cli.scm_sync:reaper_main` |
| **迁移动作** | 仅改引用 - v2.0 移除 scripts/ 入口 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.5 scm_sync_worker

| 字段 | 值 |
|------|-----|
| **现状** | 根目录已移除；`scripts/scm_sync_worker.py` 存在，带弃用警告 |
| **目标** | Console Script `engram-scm-worker` 或 `engram.logbook.cli.scm_sync:worker_main` |
| **迁移动作** | 仅改引用 - v2.0 移除 scripts/ 入口 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.6 scm_sync_gitlab_commits

| 字段 | 值 |
|------|-----|
| **现状** | 根目录已移除；`scripts/scm_sync_gitlab_commits.py` 存在，带弃用警告 |
| **目标** | `engram-scm-sync runner incremental --repo gitlab:<id>` |
| **迁移动作** | 仅改引用 - v2.0 移除 scripts/ 入口 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.7 scm_sync_gitlab_mrs

| 字段 | 值 |
|------|-----|
| **现状** | 根目录已移除；`scripts/scm_sync_gitlab_mrs.py` 存在，带弃用警告 |
| **目标** | `engram-scm-sync runner incremental --repo gitlab:<id> --job mrs` |
| **迁移动作** | 仅改引用 - v2.0 移除 scripts/ 入口 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.8 scm_sync_svn

| 字段 | 值 |
|------|-----|
| **现状** | 根目录已移除；`scripts/scm_sync_svn.py` 存在，带弃用警告 |
| **目标** | `engram-scm-sync runner incremental --repo svn:<id>` |
| **迁移动作** | 仅改引用 - v2.0 移除 scripts/ 入口 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 1.9 scm_materialize_patch_blob

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `scm_materialize_patch_blob.py` wrapper 存在 |
| **目标** | `engram.logbook.materialize_patch_blob` 模块（已存在） |
| **迁移动作** | 移动代码 - 根目录 wrapper 转为导入代理，添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

---

### 2. Artifact 相关模块

#### 2.1 artifact_audit

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `artifact_audit.py` wrapper 存在 |
| **目标** | `scripts/artifact_audit.py`（运维脚本保留） |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告，指向 scripts/ |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 2.2 artifact_cli

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `artifact_cli.py` wrapper 存在 |
| **目标** | Console Script `engram-artifacts` 或 `engram.logbook.cli.artifacts:main` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 2.3 artifact_gc

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `artifact_gc.py` wrapper 存在 |
| **目标** | Console Script `engram-artifacts gc` 或 `engram.logbook.cli.artifacts gc` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 2.4 artifact_migrate

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `artifact_migrate.py` wrapper 存在 |
| **目标** | Console Script `engram-artifacts migrate` 或 `engram.logbook.cli.artifacts migrate` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

---

### 3. 数据库相关模块

#### 3.1 db_bootstrap

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `db_bootstrap.py` wrapper 存在 |
| **目标** | Console Script `engram-bootstrap-roles` 或 `engram.logbook.cli.db_bootstrap:main` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 3.2 db_migrate

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `db_migrate.py` wrapper 存在 |
| **目标** | Console Script `engram-migrate` 或 `engram.logbook.cli.db_migrate:main` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

---

### 4. Logbook CLI 相关模块

#### 4.1 logbook_cli_main

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `logbook_cli_main.py` wrapper 存在 |
| **目标** | Console Script `engram-logbook` 或 `engram.logbook.cli.logbook:main` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

#### 4.2 logbook_cli

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `logbook_cli.py` wrapper 存在（若有） |
| **目标** | Console Script `engram-logbook` 或 `engram.logbook.cli.logbook:main` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

---

### 5. 其他模块

#### 5.1 identity_sync

| 字段 | 值 |
|------|-----|
| **现状** | 根目录 `identity_sync.py` wrapper 存在 |
| **目标** | Console Script `engram-identity-sync` 或 `engram.logbook.identity_sync:main` |
| **迁移动作** | 仅改引用 - 根目录 wrapper 添加弃用警告 |
| **Owner** | @engram-team |
| **到期** | v2.0 版本 |

---

## 长期保留模块（不在迁移计划内）

以下模块定义在 `LONG_TERM_PRESERVED_MODULES` 列表中，**不在** `ROOT_WRAPPER_MODULES` 禁止列表中，不设移除时间表：

| 模块名 | 功能 | 保留原因 | CI 检查状态 |
|--------|------|----------|-------------|
| `db` | 数据库连接工具模块 | 被多个脚本依赖 | **不检查** |
| `kv` | KV 存储工具模块 | 被多个脚本依赖 | **不检查** |
| `artifacts` | SCM 路径与制品工具 | 工具模块，被多处引用 | **不检查** |

---

## 迁移动作分类说明

| 迁移动作 | 说明 | 典型场景 |
|----------|------|----------|
| **移动代码** | 将根目录脚本的实现代码移入 `src/engram/` 包 | 核心功能模块化 |
| **仅改引用** | 根目录脚本保留为 wrapper，添加弃用警告并转发到新入口 | CLI 入口收敛 |
| **保留 compat test** | 保留测试用例验证兼容层行为 | 验证向后兼容性 |

---

## Allowlist 用途与治理规则

> **权威数据文件**：`scripts/ci/no_root_wrappers_allowlist.json`

### Allowlist 的真实用途

Allowlist 用于管理**弃用模块（deprecated）** 的临时导入例外，主要场景包括：

| 场景 | 典型用例 | 是否需要 allowlist |
|------|----------|-------------------|
| **tests/ 中测试弃用模块的行为** | 验证弃用警告正确发出、向后兼容性测试 | ✅ 需要 |
| **scripts/ 中运维脚本过渡期** | 运维脚本暂未迁移到新 CLI | ✅ 需要 |
| **兼容层内部实现** | wrapper 模块本身的导入 | ✅ 需要 |
| **长期保留模块（preserved）** | 当前无（db/kv/artifacts 已改为 deprecated） | ❌ **不需要** |

### Deprecated vs Preserved 治理差异

| 模块类型 | CI 检查行为 | Allowlist 要求 | 移除计划 |
|----------|-------------|----------------|----------|
| **Deprecated 模块** | 禁止导入，除非有有效 allowlist/inline marker | 必须带 `expires_on` 和 `owner` | 有明确到期日期 |
| **Preserved 模块** | 允许导入，**不检查**（当前无此类模块） | **不需要 allowlist** | 无移除计划 |

**关键规则**：
1. `configs/import_migration_map.json` 中 `deprecated: true` 的模块受 CI 检查约束
2. `deprecated: false`（preserved）的模块不受检查，直接导入即可（当前无此类模块）
3. Allowlist 条目必须设置 `expires_on`（最长 6 个月），过期后 CI 失败
4. Inline marker 同样必须带 `expires=` 和 `owner=`

### 当前 Allowlist 条目概览

当前 allowlist 中的条目主要为 **tests/ 中对弃用模块的测试导入例外**：

| 类别 | 条目数量 | 典型模块 | 到期日期 |
|------|----------|----------|----------|
| 测试 legacy import | 17 | artifact_*, scm_sync_*, db_*, logbook_cli_* | 2026-06-30 |

> **注意**：当前无 preserved 模块；`db/kv/artifacts` 已标记为 `deprecated: true`，需要按弃用模块治理。

### 为何 Preserved 模块不需要 Allowlist？

在 `configs/import_migration_map.json` 的设计中：
- **Deprecated 模块**：标记为 `deprecated: true`，CI 检查脚本会扫描并阻止未授权的导入
- **Preserved 模块**：标记为 `deprecated: false`，CI 检查脚本**跳过**这些模块，因此不需要任何豁免机制

```python
# ✅ 推荐：使用包内导入（不涉及 allowlist）
from engram.logbook.scm_db import upsert_repo
from engram.logbook.kv import kv_set_json
from engram.logbook.scm_artifacts import build_scm_artifact_path

# ⚠️ Deprecated 模块：需要 allowlist 或 inline marker
import artifact_cli  # ROOT-WRAPPER-ALLOW: test-artifact-cli-v1
```

参见 [no_root_wrappers_exceptions.md](no_root_wrappers_exceptions.md) 中的"Deprecated vs Preserved 的治理差异"章节获取详细说明。

---

## 一致性维护规则

1. **新增弃用模块**：在 `configs/import_migration_map.json` 中添加 `deprecated: true` 的条目
2. **模块迁移完成**：在 JSON 文件中将 `status` 改为 `"completed"`，或移除该条目
3. **新增保留模块**：在 `configs/import_migration_map.json` 中添加 `deprecated: false` 的条目
4. **文档同步**：本文档根据 SSOT 文件自动或手动更新，SSOT 为 `configs/import_migration_map.json`

> **注意**：不再需要直接修改 `scripts/ci/check_no_root_wrappers_usage.py` 中的模块列表。

---

## SSOT 变更流程

`configs/import_migration_map.json` 是模块迁移映射的**唯一权威数据来源（Single Source of Truth）**。

### 变更原则

1. **所有模块状态变更必须通过修改 SSOT 文件**，脚本从该文件读取数据
2. **CI 脚本不再硬编码模块列表**，内置默认值仅作为 SSOT 文件不可用时的回退
3. **文档与 SSOT 保持一致**，本文档中的表格应与 SSOT 文件内容对齐

### 变更步骤

#### 新增弃用模块

```bash
# 1. 编辑 SSOT 文件，添加新条目
# configs/import_migration_map.json
{
  "modules": [
    // ... 现有条目 ...
    {
      "old_module": "<模块名>",
      "import_target": "<新的 import 路径>",
      "cli_target": "<CLI 命令>",
      "notes": "<迁移说明>",
      "deprecated": true,
      "status": "wrapper_exists",
      "owner": "@engram-team",
      "target_version": "v2.0"
    }
  ]
}

# 2. 运行 CI 检查验证变更生效
python scripts/ci/check_no_root_wrappers_usage.py --verbose

# 3. 更新本文档中的迁移映射表（可选，保持文档同步）
```

#### 标记模块迁移完成

```bash
# 1. 编辑 SSOT 文件，修改 status 字段
# 将 "status": "wrapper_exists" 改为 "status": "completed"

# 2. 或者移除整个条目（如果确认不再需要追踪）
```

#### 新增长期保留模块

```bash
# 1. 编辑 SSOT 文件，添加 deprecated: false 的条目
{
  "old_module": "<模块名>",
  "import_target": "<对应的包路径>",
  "cli_target": null,
  "notes": "<保留原因>",
  "deprecated": false,
  "status": "preserved",
  "owner": "@engram-team",
  "target_version": null
}
```

### CI 脚本行为说明

`scripts/ci/check_no_root_wrappers_usage.py` 的加载逻辑：

| 场景 | 行为 |
|------|------|
| SSOT 文件存在且有效 | 从文件加载模块列表，覆盖内置默认值 |
| SSOT 文件不存在 | 输出警告，使用内置默认值 |
| SSOT 文件格式错误 | 输出警告，使用内置默认值 |
| SSOT 文件 `modules` 为空 | 使用内置默认值 |

> **设计决策**：内置默认值确保即使 SSOT 文件损坏或缺失，CI 检查仍能提供基本保护。但生产环境应始终维护有效的 SSOT 文件。

### 变更审核要求

| 变更类型 | 审核要求 |
|----------|----------|
| 新增弃用模块 | 需 Tech Lead 审核，确认迁移路径可行 |
| 标记迁移完成 | 需验证所有调用点已迁移 |
| 新增保留模块 | 需说明保留原因和依赖关系 |
| 修改迁移路径 | 需更新相关文档和测试 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [cli_entrypoints.md](cli_entrypoints.md) | CLI 入口清单与调用规范 |
| [no_root_wrappers_exceptions.md](no_root_wrappers_exceptions.md) | 例外管理规范（数据模型、expiry 语义、变更流程） |
| `configs/import_migration_map.json` | **SSOT** - 模块迁移映射数据文件 |
| `scripts/ci/check_no_root_wrappers_usage.py` | CI 门禁检查脚本（从 SSOT 读取数据） |
| `scripts/ci/no_root_wrappers_allowlist.json` | 例外允许列表数据文件 |

---

## Import 模式规范

本节定义 src/ 和 tests/ 目录中的 import 规范，明确哪些 import 模式被允许、禁止或需要特殊处理。

### 禁止的 Import 模式（在 src/ 和 tests/ 中）

以下 import 模式在 `src/` 和 `tests/` 目录中**禁止**使用，会触发 CI 门禁失败：

```python
# ❌ 禁止：直接 import 弃用的根目录模块
import scm_sync_runner
import scm_sync_scheduler
import scm_sync_worker
import artifact_cli
import artifact_gc
import artifact_migrate
import db_migrate
import db_bootstrap
import logbook_cli
import logbook_cli_main
import identity_sync

# ❌ 禁止：from import 弃用的根目录模块
from artifact_cli import main
from db_migrate import run_migrations
from scm_sync_worker import worker_main
```

### 允许的 Import 模式

```python
# ✅ 允许：使用官方包路径
from engram.logbook.cli.artifacts import main
from engram.logbook.cli.db_migrate import main as migrate_main
from engram.logbook.cli.scm_sync import worker_main
from engram.logbook.identity_sync import main as identity_sync_main

# ✅ 允许：使用包内模块（root wrapper 已移除）
from engram.logbook.scm_db import upsert_repo
from engram.logbook.kv import kv_set_json
from engram.logbook.scm_artifacts import build_scm_artifact_path

# ✅ 允许：在 scripts/ 目录中 import 任意模块（不受 CI 检查）
# scripts/ 目录不在 check_no_root_wrappers_usage.py 的检查范围内
```

### 带例外声明的 Import（临时允许）

```python
# ✅ 允许：使用 Allowlist 引用
import artifact_cli  # ROOT-WRAPPER-ALLOW: test-artifact-cli-v1

# ✅ 允许：使用 Inline 声明（需包含 reason、expires、owner）
import db_migrate  # ROOT-WRAPPER-ALLOW: 迁移测试; expires=2026-06-30; owner=@engram-team

# ✅ 允许：上一行声明
# ROOT-WRAPPER-ALLOW: 验收测试需要验证兼容行为; expires=2026-06-30; owner=@engram-team
import db_bootstrap
```

### TYPE_CHECKING 与可选依赖约定

```python
from __future__ import annotations
from typing import TYPE_CHECKING

# ✅ 推荐：在 TYPE_CHECKING 块中 import 仅用于类型注解的依赖
if TYPE_CHECKING:
    from engram.logbook.scm_sync_runner import RunnerConfig
    from engram.gateway.container import Container

# ✅ 推荐：可选依赖使用 try/except 包装
try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    psycopg2 = None  # type: ignore[assignment]

# ✅ 推荐：延迟导入减少启动开销
def get_heavy_module():
    """按需导入重型模块。"""
    from engram.logbook.scm_sync_executor import SyncExecutor
    return SyncExecutor
```

### pytest 测试中的 Import 约定

```python
import pytest

# ✅ 推荐：使用 pytest.importorskip 处理可选依赖
psycopg2 = pytest.importorskip("psycopg2", reason="需要 psycopg2 进行数据库测试")

# ✅ 推荐：测试弃用模块的兼容层行为时使用例外声明
# ROOT-WRAPPER-ALLOW: 验证弃用警告行为; expires=2026-06-30; owner=@engram-team
import artifact_gc  # 测试目标：验证弃用警告正确发出

# ✅ 推荐：使用 fixture 提供模块引用
@pytest.fixture
def artifacts_module():
    """提供 scm_artifacts 模块引用，避免顶层 import。"""
    from engram.logbook import scm_artifacts

    return scm_artifacts

# ❌ 避免：在测试中直接 import 弃用模块（除非是测试弃用行为本身）
```

---

## 批量迁移流程（单 PR 批量改写指南）

本节描述如何在一次 PR 中批量迁移 import 而不破坏测试。

### 前置准备

1. **确认当前 CI 状态**：确保 master 分支 CI 绑色
2. **获取完整变更范围**：运行 codemod 工具扫描
   ```bash
   python scripts/ci/check_no_root_wrappers_usage.py --verbose --json > /tmp/violations.json
   ```
3. **备份测试状态**：记录当前测试通过数
   ```bash
   pytest tests/ --collect-only -q | tail -5
   ```

### 步骤 1：创建特性分支

```bash
git checkout -b refactor/migrate-root-wrappers-batch-$(date +%Y%m%d)
```

### 步骤 2：按模块分批迁移

**推荐迁移顺序**（按依赖关系从低到高）：

| 批次 | 模块分类 | 模块列表 | 预估影响文件数 |
|------|----------|----------|----------------|
| 1 | SCM Sync | `scm_sync_*` | 5-10 |
| 2 | Artifact | `artifact_*` | 3-5 |
| 3 | DB 相关 | `db_migrate`, `db_bootstrap` | 2-3 |
| 4 | CLI | `logbook_cli*`, `identity_sync` | 2-3 |

**每批迁移操作**：

```bash
# 1. 使用 codemod 工具执行迁移（后续任务实现）
python scripts/ci/codemod_root_wrappers.py --module artifact_cli --dry-run
python scripts/ci/codemod_root_wrappers.py --module artifact_cli --apply

# 2. 运行相关测试验证
pytest tests/logbook/test_artifacts_cli.py -v

# 3. 如果测试失败，检查是否需要添加临时例外
# 例如：测试文件需要验证弃用警告的正确发出
```

### 步骤 3：处理测试失败

| 失败类型 | 处理方式 |
|----------|----------|
| **import 路径变更** | 更新为新的包路径 `from engram.logbook.cli.xxx import ...` |
| **弃用警告测试** | 添加 inline 例外声明，保留测试弃用行为 |
| **fixture 依赖** | 更新 conftest.py 中的 fixture 定义 |
| **mock 路径错误** | 更新 `@patch('old.path')` 为 `@patch('engram.logbook.xxx')` |

### 步骤 4：验证完整性

```bash
# 1. 运行完整测试
make test

# 2. 运行 CI 检查
make check-no-root-wrappers

# 3. 验证弃用警告正确输出
python -c "import artifact_cli" 2>&1 | grep -i deprecat
```

### 步骤 5：提交并创建 PR

```bash
# 分批提交，每批一个 commit
git add -p  # 交互式选择变更
git commit -m "refactor(imports): migrate artifact_cli to engram.logbook.cli.artifacts

- Update 5 files to use new import path
- Add inline exception for deprecation test
- All tests passing"

# 推送并创建 PR
git push -u origin HEAD
gh pr create --title "refactor: batch migrate root wrapper imports" --body "$(cat <<'EOF'
## Summary
- Migrate root wrapper imports to official package paths
- Follows no_root_wrappers_migration_map.md guidelines

## Test plan
- [ ] `make test` passes
- [ ] `make check-no-root-wrappers` passes
- [ ] Deprecation warnings verified
EOF
)"
```

### 回滚策略

如果迁移导致问题，可以：

1. **单文件回滚**：`git checkout HEAD~1 -- path/to/file.py`
2. **整批回滚**：`git revert HEAD`
3. **添加临时例外**：使用 inline marker 暂时跳过检查

---

## SSOT 与自动化工具入口

### 权威数据来源（SSOT）

| 数据类型 | 权威文件 | 说明 |
|----------|----------|------|
| **模块迁移映射** | `configs/import_migration_map.json` | **SSOT** - 弃用模块、保留模块、迁移路径的唯一定义 |
| **例外允许列表** | `scripts/ci/no_root_wrappers_allowlist.json` | Allowlist 方式例外的唯一定义 |
| **例外 Schema** | `schemas/no_root_wrappers_allowlist_v2.schema.json` | Allowlist 数据格式定义 |
| **迁移映射解释** | 本文档（`no_root_wrappers_migration_map.md`） | 迁移策略与进度的唯一文档 |
| **CLI 命令定义** | `pyproject.toml [project.scripts]` | CLI 入口的唯一定义 |

> **注意**：`scripts/ci/check_no_root_wrappers_usage.py` 从 `configs/import_migration_map.json` 读取数据，不再硬编码模块列表。

### 自动化工具入口

| 工具 | 入口 | 功能 |
|------|------|------|
| **CI 门禁检查** | `python scripts/ci/check_no_root_wrappers_usage.py` | 检查 src/ 和 tests/ 中的禁止 import |
| **Allowlist 验证** | `python scripts/ci/check_no_root_wrappers_allowlist.py` | 验证 allowlist 格式与过期状态 |
| **Codemod 迁移** | `python scripts/ci/codemod_root_wrappers.py`（待实现） | 自动重写 import 语句 |
| **CLI 入口一致性** | `python scripts/verify_cli_entrypoints_consistency.py` | 验证 pyproject.toml 与文档一致 |

### Codemod 工具规划

> **注意**：Codemod 工具将在后续任务中实现

计划的 codemod 工具功能：

```bash
# 扫描模式（显示将要修改的内容）
python scripts/ci/codemod_root_wrappers.py --scan

# 干运行（显示修改但不实际写入）
python scripts/ci/codemod_root_wrappers.py --module artifact_cli --dry-run

# 执行迁移
python scripts/ci/codemod_root_wrappers.py --module artifact_cli --apply

# 批量迁移所有弃用模块
python scripts/ci/codemod_root_wrappers.py --all --apply

# 生成迁移报告
python scripts/ci/codemod_root_wrappers.py --report > migration_report.md
```

---

更新时间：2026-02-01
