# CLI 入口清单与调用规范

> **状态**：当前  
> **更新日期**：2026-02-03  
> **关联文档**：[iteration_2_plan.md](iteration_2_plan.md) (M1: 脚本入口收敛)

---

## 概述

本文档记录 Engram 项目的所有 CLI 入口点，定义推荐的调用路径与优先级，并规划遗留入口的兼容期与移除条件。

---

## 入口职责划分

| 入口类型 | 位置 | 职责定义 | 状态 |
|----------|------|----------|------|
| **Console Scripts** | `pyproject.toml [project.scripts]` | 官方入口，推荐的调用方式 | 当前 |
| **scripts/** | `scripts/` 目录 | 运维工具、CI 脚本、验证工具 | 长期保留 |
| **根目录脚本** | 项目根目录 `*.py` | 历史兼容入口（deprecated wrapper） | 已移除（v2.0） |
| **logbook_postgres/scripts/** | 遗留目录 | 历史遗留，已无使用价值 | 待移除 |

**职责原则**：

- `console scripts`：用户和 CI 的官方调用入口，通过 `pip install` 后可直接使用
- `scripts/`：不依赖包安装的独立运维脚本，主要用于 CI 流水线、一致性验证、本地调试
- 根目录脚本：v2.0 已移除，请使用 console scripts 或 `scripts/`

---

## 1. 当前入口清单

### 1.1 Console Scripts（推荐入口）

定义于 `pyproject.toml` 的 `[project.scripts]`，通过 `pip install -e .` 或 `pip install engram` 后可直接使用：

| 命令 | 入口模块 | 功能描述 | 依赖 |
|------|----------|----------|------|
| `engram-logbook` | `engram.logbook.cli.logbook:main` | Logbook 主操作 CLI | 核心 |
| `engram-migrate` | `engram.logbook.cli.db_migrate:main` | 数据库迁移 | 核心 |
| `engram-scm` | `engram.logbook.cli.scm:main` | SCM 操作入口 | scm |
| `engram-gateway` | `engram.gateway.main:main` | Gateway 服务启动 | gateway |
| `engram-iteration` | `engram.iteration.cli:main` | Iteration 工具入口（rerun-advice） | core |
| `engram-scm-sync` | `engram.logbook.cli.scm_sync:main` | SCM Sync 统一入口 | scm |
| `engram-scm-scheduler` | `engram.logbook.cli.scm_sync:scheduler_main` | 调度器快捷入口 | scm |
| `engram-scm-worker` | `engram.logbook.cli.scm_sync:worker_main` | Worker 快捷入口 | scm |
| `engram-scm-reaper` | `engram.logbook.cli.scm_sync:reaper_main` | 清理器快捷入口 | scm |
| `engram-scm-status` | `engram.logbook.cli.scm_sync:status_main` | 状态查询快捷入口 | scm |
| `engram-scm-runner` | `engram.logbook.cli.scm_sync:runner_main` | Runner 快捷入口 | scm |

### 1.2 根目录脚本（已移除）

v2.0 已移除根目录兼容入口，以下清单仅用于历史追溯：

| 脚本 | 功能描述 | 对应新入口 | 状态 |
|------|----------|------------|------|
| `db.py` | 数据库连接工具 | `engram.logbook.db` | 兼容保留 |
| `db_bootstrap.py` | 数据库初始化 | `engram-bootstrap-roles` | 已移除（v2.0） |
| `db_migrate.py` | 数据库迁移 | `engram-migrate` | 已移除（v2.0） |
| `logbook_cli.py` | Logbook CLI | `engram-logbook` | 已移除（v2.0） |
| `logbook_cli_main.py` | Logbook CLI 入口 | `engram-logbook` | 已移除（v2.0） |
| `artifact_cli.py` | Artifact 操作 | `engram-artifacts` | 已移除（v2.0） |
| `artifact_gc.py` | Artifact 垃圾回收 | `engram-artifacts gc` | 已移除（v2.0） |
| `artifact_migrate.py` | Artifact 迁移 | `engram-artifacts migrate` | 已移除（v2.0） |
| `artifact_audit.py` | Artifact 审计 | `scripts/artifact_audit.py` | 已移除（v2.0） |
| `identity_sync.py` | 身份同步工具 | `engram-identity-sync` | 已移除（v2.0） |
| `kv.py` | KV 存储工具 | `engram.logbook.kv` | 兼容保留 |
| `scm_repo.py` | SCM 仓库工具 | `engram.logbook.scm_repo` | 兼容保留 |
| `scm_sync_runner.py` | SCM Sync 运行器 | `engram-scm-runner` | 已移除（v2.0） |
| `scm_sync_status.py` | SCM Sync 状态 | `engram-scm-status` | 已移除（v2.0） |
| `scm_sync_reaper.py` | SCM Sync 清理器 | `engram-scm-reaper` | 已移除（v2.0） |
| `scm_sync_worker.py` | SCM Sync Worker | `engram-scm-worker` | 已移除（v2.0） |
| `scm_sync_gitlab_commits.py` | GitLab Commits 同步 | `engram-scm-sync` | 已移除（v2.0） |
| `scm_sync_gitlab_mrs.py` | GitLab MRs 同步 | `engram-scm-sync` | 已移除（v2.0） |
| `scm_sync_svn.py` | SVN 同步 | `engram-scm-sync` | 已移除（v2.0） |

### 1.3 scripts/ 目录脚本

位于 `scripts/` 目录，用于开发、CI 和运维任务：

| 脚本路径 | 功能描述 | 调用方式 |
|----------|----------|----------|
| `scripts/db_bootstrap.py` | 数据库初始化 | `python scripts/db_bootstrap.py` |
| `scripts/logbook_cli_main.py` | Logbook CLI（镜像） | `python scripts/logbook_cli_main.py` |
| `scripts/artifact_cli.py` | Artifact CLI（镜像） | `python scripts/artifact_cli.py` |
| `scripts/artifact_gc.py` | Artifact 垃圾回收 | `python scripts/artifact_gc.py` |
| `scripts/artifact_audit.py` | Artifact 审计 | `python scripts/artifact_audit.py` |
| `scripts/artifact_migrate.py` | Artifact 迁移 | `python scripts/artifact_migrate.py` |
| `scripts/scm_sync_scheduler.py` | SCM Sync 调度器 | `python scripts/scm_sync_scheduler.py` |
| `scripts/scm_sync_worker.py` | SCM Sync Worker | `python scripts/scm_sync_worker.py` |
| `scripts/scm_sync_reaper.py` | SCM Sync 清理器 | `python scripts/scm_sync_reaper.py` |
| `scripts/scm_sync_gitlab_commits.py` | GitLab Commits 同步 | `python scripts/scm_sync_gitlab_commits.py` |
| `scripts/scm_sync_gitlab_mrs.py` | GitLab MRs 同步 | `python scripts/scm_sync_gitlab_mrs.py` |
| `scripts/scm_sync_svn.py` | SVN 同步 | `python scripts/scm_sync_svn.py` |
| `scripts/verify_logbook_consistency.py` | 一致性校验 | `python scripts/verify_logbook_consistency.py` |
| `scripts/verify_scm_sync_consistency.py` | SCM Sync 一致性验证 | `python scripts/verify_scm_sync_consistency.py` |
| `scripts/check_env_var_drift.py` | 环境变量漂移检查 | `python scripts/check_env_var_drift.py` |
| `scripts/ci/*.py` | CI 相关工具 | CI 流水线调用 |
| `scripts/docs/*.py` | 文档工具 | 开发时调用 |

### 1.4 logbook_postgres/scripts/ 目录（遗留入口）

历史遗留目录，计划完全移除：

| 脚本 | 功能描述 | 状态 |
|------|----------|------|
| `logbook_postgres/scripts/db_bootstrap.py` | 数据库初始化 | 待移除 |
| `logbook_postgres/scripts/db_migrate.py` | 数据库迁移 | 待移除 |
| `logbook_postgres/scripts/logbook_cli.py` | Logbook CLI | 待移除 |
| `logbook_postgres/scripts/logbook_cli_main.py` | Logbook CLI 入口 | 待移除 |
| `logbook_postgres/scripts/artifact_cli.py` | Artifact CLI | 待移除 |
| `logbook_postgres/scripts/scm_materialize_patch_blob.py` | Patch Blob 物化 | 待移除 |

---

## 2. 推荐调用路径与优先级

调用 Engram CLI 功能时，请按以下优先级选择入口：

### 优先级 1: Console Scripts（首选）

```bash
# 数据库迁移
engram-migrate --help
engram-migrate apply

# Logbook 操作
engram-logbook --help
engram-logbook list

# SCM Sync 操作
engram-scm-sync --help
engram-scm-sync scheduler start
engram-scm-worker  # 快捷入口

# Gateway 启动
engram-gateway
```

**优点**：
- 无需知道模块路径
- 自动处理 PYTHONPATH
- 支持 shell 自动补全
- 版本管理清晰

### 优先级 2: Python -m 模块调用

```bash
# 数据库迁移
python -m engram.logbook.cli.db_migrate --help

# Logbook CLI
python -m engram.logbook.cli.logbook --help

# SCM Sync CLI
python -m engram.logbook.cli.scm_sync --help

# Gateway
python -m engram.gateway.main
```

**适用场景**：
- 未安装包但在开发环境中
- 需要明确 Python 解释器版本
- CI 环境中显式调用

### 优先级 3: scripts/ 目录脚本

```bash
# 用于 CI 或特定运维任务
python scripts/db_bootstrap.py
python scripts/verify_logbook_consistency.py
```

**适用场景**：
- CI 流水线中的一次性任务
- 运维脚本（不依赖包安装）
- 开发调试

### 优先级 4: 兼容入口（已移除）

```bash
# v2.0 起已移除（历史兼容入口）
python db_migrate.py --help
python logbook_cli.py --help
```

**注意**：这些入口已在 v2.0 移除，请改用 console scripts。

---

## 3. 旧命令到新命令对照表

### 3.1 数据库操作

| 旧命令 | 新命令 |
|--------|--------|
| `python db_migrate.py` | `engram-migrate` |
| `python db_migrate.py --help` | `engram-migrate --help` |
| `python db_bootstrap.py` | `engram-bootstrap-roles` |
| `python scripts/db_bootstrap.py` | `engram-bootstrap-roles` |
| `python logbook_postgres/scripts/db_migrate.py` | `engram-migrate` |
| `python logbook_postgres/scripts/db_bootstrap.py` | `engram-bootstrap-roles` |

### 3.2 Logbook 操作

| 旧命令 | 新命令 |
|--------|--------|
| `python logbook_cli.py` | `engram-logbook` |
| `python logbook_cli_main.py` | `engram-logbook` |
| `python logbook_postgres/scripts/logbook_cli.py` | `engram-logbook` |
| `python logbook_postgres/scripts/logbook_cli_main.py` | `engram-logbook` |

### 3.3 Artifact 操作

| 旧命令 | 新命令 |
|--------|--------|
| `python artifact_cli.py` | `engram-artifacts` |
| `python artifact_migrate.py` | `engram-artifacts migrate` |
| `python artifact_gc.py` | `engram-artifacts gc` |
| `python logbook_postgres/scripts/artifact_cli.py` | `engram-artifacts` |

### 3.4 SCM Sync 操作

| 旧命令 | 新命令 |
|--------|--------|
| `python scm_sync_runner.py` | `engram-scm run` |
| `python scm_sync_scheduler.py` | `engram-scm-scheduler` |
| `python scm_sync_status.py` | `engram-scm-status` |
| `python scm_sync_reaper.py` | `engram-scm-reaper` |
| `python scm_sync_worker.py` | `engram-scm-worker` |
| `python scm_sync_gitlab_commits.py` | `engram-scm run gitlab-commits` |
| `python scm_sync_gitlab_mrs.py` | `engram-scm run gitlab-mrs` |
| `python scm_sync_svn.py` | `engram-scm run svn` |
| `python scripts/scm_sync_scheduler.py` | `engram-scm-scheduler` |
| `python scripts/scm_sync_worker.py` | `engram-scm-worker` |
| `python scripts/scm_sync_reaper.py` | `engram-scm-reaper` |

### 3.5 SCM 仓库管理

| 命令 | 说明 |
|------|------|
| `engram-scm ensure-repo` | 确保仓库存在（幂等） |
| `engram-scm list-repos` | 列出仓库 |
| `engram-scm get-repo` | 查询仓库详情 |

#### ensure-repo 示例

```bash
# 创建/更新 Git 仓库
engram-scm ensure-repo \
    --dsn "postgresql://user:pass@localhost/db" \
    --repo-type git \
    --repo-url https://gitlab.com/ns/proj \
    --project-key my_project \
    --default-branch main

# 使用配置文件
engram-scm ensure-repo \
    --config /path/to/config.toml \
    --repo-type svn \
    --repo-url svn://example.com/repo
```

#### list-repos 示例

```bash
# 列出所有仓库
engram-scm list-repos --dsn "postgresql://user:pass@localhost/db"

# 按类型过滤
engram-scm list-repos --dsn "..." --repo-type git --limit 50
```

#### get-repo 示例

```bash
# 按 ID 查询
engram-scm get-repo --dsn "..." --repo-id 123

# 按 URL 查询
engram-scm get-repo --dsn "..." --repo-type git --repo-url https://gitlab.com/ns/proj
```

### 3.5 Gateway 操作

| 旧命令 | 新命令 |
|--------|--------|
| `uvicorn engram.gateway.main:app` | `engram-gateway` |
| `python -m engram.gateway.main` | `engram-gateway` |

---

## 4. 兼容期与移除条件

### 4.1 兼容期定义

| 阶段 | 行为 | 持续条件 |
|------|------|----------|
| **阶段 1: 完全兼容** | 旧入口正常工作，输出 deprecation 警告 | 新 CLI 功能对齐前 |
| **阶段 2: 警告升级** | 旧入口工作，输出更明显的警告（stderr） | 新 CLI 测试覆盖达标前 |
| **阶段 3: 移除** | 删除旧入口文件 | 移除条件全部满足 |

### 4.2 移除条件

旧入口将在满足**所有以下条件**后移除：

| # | 条件 | 验证方式 |
|---|------|----------|
| 1 | 新 CLI 功能完全对齐 | 功能对照表中所有命令可用 |
| 2 | 新 CLI 测试覆盖率 ≥ 80% | `pytest --cov` 输出 |
| 3 | 文档更新完成 | README 和 docs/ 引用新命令 |
| 4 | CI 流水线迁移完成 | `.github/workflows/` 使用新命令 |
| 5 | Makefile 迁移完成 | `Makefile` 使用新命令 |
| 6 | 至少一个完整版本周期 | 发布包含新 CLI 的版本后 |
| 7 | CI 门禁检查通过 | `check_no_legacy_imports.py` 脚本验证无新增遗留导入 |

### 4.3 CI 门禁约束

为防止新代码引入对根目录兼容模块的依赖，CI 流水线应包含以下检查：

```yaml
# .github/workflows/ci.yml
- name: Check no legacy db/kv imports
  run: |
    python scripts/ci/check_no_legacy_imports.py
```

**门禁规则**：

1. **禁止新增 `from db import` 或 `import db`**
   - 新代码必须使用 `from engram.logbook.scm_db import ...`
   - 例外：`tests/logbook/test_scm_sync_integration.py` 作为验收测试保留

2. **禁止新增 `from kv import` 或 `import kv`**
   - KV 操作应使用 `engram.logbook.scm_db` 中的相关函数
   - 例外：同上验收测试

3. **允许列表**（不受门禁约束的文件）：
   - `db.py` - 兼容包装器本身
   - `kv.py` - 兼容包装器本身
   - `tests/logbook/test_scm_sync_integration.py` - 验收测试

**检查脚本示例**：

```python
# scripts/ci/check_no_legacy_imports.py
LEGACY_PATTERNS = [
    r'^from db import',
    r'^import db\s*$',
    r'^import db as',
    r'^from kv import',
    r'^import kv\s*$',
]

ALLOWED_FILES = [
    'db.py',
    'kv.py',
    'tests/logbook/test_scm_sync_integration.py',
]

# 扫描所有 .py 文件，排除 allowed_files，检查是否有 legacy patterns
```

### 4.3 移除时间表（预估）

| 入口类型 | 预计移除时间 |
|----------|--------------|
| `logbook_postgres/scripts/*.py` | M1 完成后立即移除 |
| 根目录 SCM Sync 脚本 | M1 完成 + 1 版本周期 |
| 根目录 Logbook/Migrate 脚本 | M1 完成 + 1 版本周期 |
| `scripts/*.py` 镜像脚本 | 长期保留（CI/运维用途） |

---

## 5. 迁移指南

### 5.1 开发者迁移

1. **更新开发环境**：
   ```bash
   pip install -e ".[dev,full]"
   ```

2. **更新命令别名**（可选）：
   ```bash
   # ~/.bashrc 或 ~/.zshrc
   alias em='engram-migrate'
   alias el='engram-logbook'
   alias ess='engram-scm-sync'
   ```

3. **更新脚本引用**：
   将所有 `python xxx.py` 替换为对应的 console script。

### 5.2 CI/CD 迁移

```yaml
# 旧
- run: python db_migrate.py apply

# 新（推荐）
- run: engram-migrate apply

# 或使用模块调用
- run: python -m engram.logbook.cli.db_migrate apply
```

### 5.3 Docker 镜像迁移

```dockerfile
# 旧
CMD ["python", "logbook_cli.py", "serve"]

# 新
CMD ["engram-logbook", "serve"]
```

---

## 6. Runner CLI/解析器统一

### 6.1 设计原则

Runner 的 CLI 解析器统一在 `engram.logbook.scm_sync_runner` 模块中：

| 函数 | 说明 |
|------|------|
| `create_parser()` | 创建配置好的 ArgumentParser 对象 |
| `parse_args(argv)` | 解析命令行参数，内部调用 `create_parser()` |

### 6.2 入口点复用

所有 runner 相关入口点共享同一解析器定义：

| 入口点 | 调用方式 |
|--------|----------|
| `engram-scm-runner` | 调用 `runner_main()` -> 内部使用 `create_parser()` |
| `engram-scm-sync runner` | 调用 `runner_main()` -> 内部使用 `create_parser()` |
| `python scm_sync_runner.py` | 已移除（v2.0），请使用 `engram-scm-runner` |
| 直接调用 `parse_args()` | 测试和脚本可直接使用 |

### 6.3 状态（scm_sync_runner.py 根目录脚本）

根目录的 `scm_sync_runner.py` 已移除（v2.0），请使用 `engram-scm-runner` 或
`engram-scm-sync runner`。

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [iteration_2_plan.md](iteration_2_plan.md) | Iteration 2 计划（M1 详细任务） |
| [naming.md](naming.md) | 命名规范 |
| [../reference/environment_variables.md](../reference/environment_variables.md) | 环境变量参考 |

---

更新时间：2026-02-03
