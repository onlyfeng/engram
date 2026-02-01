# 线上升级与回滚策略

> 版本: 2026-02-01  
> 适用版本: 2026-01-31 及以后

本文档详细说明线上环境的升级流程、回滚策略、SQL 来源管理及运维 Runbook。

---

## 1. 线上发布形态与 SQL 来源

### 1.1 SQL 目录的 SSOT 原则

**核心原则**：生产环境必须使用**同一版本的 `sql/` 目录**作为唯一真实来源（Single Source of Truth, SSOT）。

| 发布形态 | SQL 来源 | SSOT 保证方式 | 适用场景 |
|----------|----------|---------------|----------|
| **Git 部署** | `git checkout` 同步 | 版本标签/commit hash 一致 | 开发/测试/小规模生产 |
| **Release Artifact** | 发布包中 `sql/` 目录 | 发布流程保证版本一致 | 正式生产发布 |
| **Docker 镜像** | 镜像内置 `sql/` 目录 | 镜像版本号对应 | 容器化部署 |
| **Volume 挂载** | 主机目录挂载 | 挂载路径与代码版本同步 | 本地开发/调试 |

### 1.2 版本一致性要求

```
┌─────────────────────────────────────────────────────────────────┐
│                       版本一致性检查                             │
├─────────────────────────────────────────────────────────────────┤
│  应用代码版本 ═══════════════╦═══════════════ sql/ 目录版本     │
│                              ║                                   │
│  engram-migrate CLI  ════════╩═══════════════ sql/ 脚本         │
│                                                                  │
│  ⚠️ 不一致会导致：                                                │
│    - 迁移失败（缺失脚本）                                        │
│    - 权限错误（脚本版本不匹配）                                   │
│    - 验证失败（检查项与实际结构不符）                             │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 各发布形态的 SQL 来源配置

#### 1.3.1 Docker Compose（推荐）

```yaml
# docker-compose.unified.yml
services:
  postgres:
    volumes:
      # SSOT: 挂载版本对应的 sql/ 目录
      - ./sql:/docker-entrypoint-initdb.d:ro
  
  logbook_migrate:
    image: engram:${VERSION:-latest}
    # 镜像内置 sql/ 目录，版本与镜像一致
    command: >
      engram-migrate --dsn "$POSTGRES_DSN"
        --apply-roles
        --apply-openmemory-grants
```

**版本一致性保证**：

```bash
# 方式 1: 使用同一版本标签
export VERSION=v1.2.3
docker compose pull
docker compose up -d

# 方式 2: 从 release artifact 解压
tar -xzf engram-v1.2.3.tar.gz
cd engram-v1.2.3
docker compose up -d
```

#### 1.3.2 Kubernetes

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: engram-sql-scripts
  labels:
    app.kubernetes.io/version: "v1.2.3"  # 版本标签
data:
  # 从 release artifact 生成 ConfigMap
  # kubectl create configmap engram-sql-scripts --from-file=sql/
```

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: engram-migrate
spec:
  template:
    spec:
      containers:
      - name: migrate
        image: engram:v1.2.3  # 版本必须一致
        command: ["engram-migrate"]
        args:
          - --dsn=$(POSTGRES_DSN)
          - --apply-roles
          - --apply-openmemory-grants
        volumeMounts:
        - name: sql-scripts
          mountPath: /app/sql
      volumes:
      - name: sql-scripts
        configMap:
          name: engram-sql-scripts  # 版本一致的 ConfigMap
```

#### 1.3.3 裸机/VM 部署

```bash
#!/bin/bash
# deploy.sh - 确保 SQL 目录版本一致

VERSION="v1.2.3"
DEPLOY_DIR="/opt/engram"

# 1. 下载 release artifact
wget "https://releases.example.com/engram-${VERSION}.tar.gz"
tar -xzf "engram-${VERSION}.tar.gz" -C "$DEPLOY_DIR"

# 2. 验证版本一致性
INSTALLED_VERSION=$(cat "$DEPLOY_DIR/sql/.version" 2>/dev/null || echo "unknown")
if [ "$INSTALLED_VERSION" != "$VERSION" ]; then
    echo "[ERROR] SQL 版本不一致: 期望 $VERSION, 实际 $INSTALLED_VERSION"
    exit 1
fi

# 3. 执行迁移（使用同版本的 sql/ 目录）
engram-migrate --dsn "$POSTGRES_DSN" \
    --sql-dir "$DEPLOY_DIR/sql" \
    --apply-roles \
    --apply-openmemory-grants
```

### 1.4 禁止的做法

| ❌ 禁止做法 | 风险 | 正确做法 |
|------------|------|----------|
| 手动复制单个 SQL 文件 | 版本不一致 | 使用完整的 `sql/` 目录 |
| 混用不同版本的 SQL 文件 | 迁移失败/数据损坏 | 始终使用同一版本 |
| 修改 release 包中的 SQL 文件 | 审计困难/回滚失败 | 通过代码提交修改 |
| 跳过某些 SQL 文件执行 | 结构不完整 | 使用 CLI 参数控制执行范围 |

---

## 2. 标准升级流程

### 2.1 完整升级流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          标准升级流程                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ① 准备阶段                                                                  │
│  ┌──────────────────┐                                                       │
│  │ 备份数据库        │ ─── pg_dump -Fc -f backup.dump                        │
│  │ 同步代码/拉取镜像 │ ─── git pull / docker pull                            │
│  │ 查看迁移计划      │ ─── engram-migrate --plan                             │
│  └──────────────────┘                                                       │
│           │                                                                  │
│           ▼                                                                  │
│  ② DDL 迁移                                                                  │
│  ┌──────────────────┐                                                       │
│  │ 执行 DDL 脚本     │ ─── engram-migrate --dsn "$DSN"                       │
│  │ (01-03, 06-14)   │     幂等设计，可安全重复执行                           │
│  └──────────────────┘                                                       │
│           │                                                                  │
│           ▼                                                                  │
│  ③ 权限配置                                                                  │
│  ┌──────────────────┐                                                       │
│  │ 应用角色权限 (04) │ ─── --apply-roles                                     │
│  │ 应用 OM 权限 (05) │ ─── --apply-openmemory-grants                        │
│  └──────────────────┘                                                       │
│           │                                                                  │
│           ▼                                                                  │
│  ④ 验证阶段                                                                  │
│  ┌──────────────────┐                                                       │
│  │ 严格模式验证      │ ─── --verify --verify-strict                          │
│  │ Gate Policy 检查  │ ─── --verify-gate-policy fail_and_warn (CI)          │
│  └──────────────────┘                                                       │
│           │                                                                  │
│           ▼                                                                  │
│  ⑤ 健康检查                                                                  │
│  ┌──────────────────┐                                                       │
│  │ CLI 健康检查      │ ─── engram-logbook health                             │
│  │ 应用服务启动      │ ─── docker compose up -d                              │
│  └──────────────────┘                                                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 命令序列（可复制）

```bash
#!/bin/bash
# ============================================================
# Engram 标准升级流程 - 生产环境 Runbook
# ============================================================

set -euo pipefail

POSTGRES_DSN="${POSTGRES_DSN:?请设置 POSTGRES_DSN 环境变量}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/engram}"
LOG_DIR="${LOG_DIR:-.artifacts/upgrade}"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "=== 步骤 1: 备份数据库 ==="
pg_dump -Fc -d "$POSTGRES_DSN" -f "$BACKUP_DIR/backup_${TIMESTAMP}.dump"
echo "[OK] 备份完成: $BACKUP_DIR/backup_${TIMESTAMP}.dump"

echo ""
echo "=== 步骤 2: 查看迁移计划 ==="
engram-migrate --plan --apply-roles --apply-openmemory-grants --verify \
    2>&1 | tee "$LOG_DIR/plan_${TIMESTAMP}.log"

echo ""
echo "=== 步骤 3: 执行 DDL 迁移 ==="
engram-migrate --dsn "$POSTGRES_DSN" \
    2>&1 | tee "$LOG_DIR/ddl_${TIMESTAMP}.log"
exit_code=${PIPESTATUS[0]}
if [ "$exit_code" -ne 0 ]; then
    echo "[ERROR] DDL 迁移失败，退出码: $exit_code"
    echo "[INFO] 备份位置: $BACKUP_DIR/backup_${TIMESTAMP}.dump"
    exit $exit_code
fi

echo ""
echo "=== 步骤 4: 应用角色权限 ==="
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    2>&1 | tee "$LOG_DIR/roles_${TIMESTAMP}.log"
exit_code=${PIPESTATUS[0]}
if [ "$exit_code" -ne 0 ]; then
    echo "[ERROR] 角色权限应用失败，退出码: $exit_code"
    exit $exit_code
fi

echo ""
echo "=== 步骤 5: 应用 OpenMemory 权限 ==="
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-openmemory-grants \
    2>&1 | tee "$LOG_DIR/om_grants_${TIMESTAMP}.log"
exit_code=${PIPESTATUS[0]}
if [ "$exit_code" -ne 0 ]; then
    echo "[ERROR] OpenMemory 权限应用失败，退出码: $exit_code"
    exit $exit_code
fi

echo ""
echo "=== 步骤 6: 严格模式验证 ==="
engram-migrate --dsn "$POSTGRES_DSN" \
    --verify \
    --verify-strict \
    --verify-gate-policy fail_and_warn \
    2>&1 | tee "$LOG_DIR/verify_${TIMESTAMP}.log"
exit_code=${PIPESTATUS[0]}
if [ "$exit_code" -ne 0 ]; then
    echo "[ERROR] 权限验证失败，退出码: $exit_code"
    echo "[INFO] 请检查日志: $LOG_DIR/verify_${TIMESTAMP}.log"
    exit $exit_code
fi

echo ""
echo "=== 步骤 7: 健康检查 ==="
engram-logbook health 2>&1 | tee "$LOG_DIR/health_${TIMESTAMP}.log"
exit_code=${PIPESTATUS[0]}
if [ "$exit_code" -ne 0 ]; then
    echo "[ERROR] 健康检查失败，退出码: $exit_code"
    exit $exit_code
fi

echo ""
echo "============================================================"
echo "[SUCCESS] 升级完成！"
echo "  备份: $BACKUP_DIR/backup_${TIMESTAMP}.dump"
echo "  日志: $LOG_DIR/"
echo "============================================================"
```

### 2.3 一键升级命令（简化版）

```bash
# 完整迁移（DDL + 权限 + 验证）
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    --apply-openmemory-grants \
    --verify \
    --verify-strict \
    --verify-gate-policy fail_and_warn
```

### 2.4 Docker Compose 升级

```bash
# 1. 停止应用服务（保留数据库）
docker compose -f docker-compose.unified.yml stop gateway worker openmemory

# 2. 拉取最新镜像
docker compose -f docker-compose.unified.yml pull

# 3. 执行迁移服务
docker compose -f docker-compose.unified.yml up bootstrap_roles logbook_migrate openmemory_migrate permissions_verify

# 4. 启动应用服务
docker compose -f docker-compose.unified.yml up -d
```

---

## 3. 三类回滚策略

### 3.1 回滚类型概览

| 回滚类型 | 触发条件 | 影响范围 | 停机时间 | 数据影响 |
|----------|----------|----------|----------|----------|
| **应用版本回滚** | 应用代码问题（逻辑/性能） | 仅应用层 | < 1 分钟 | 无 |
| **权限回滚/重放** | 权限配置错误 | 权限层 | < 1 分钟 | 无 |
| **数据库级恢复** | DDL 破坏性变更/数据损坏 | 全栈 | > 10 分钟 | 可能丢失新数据 |

### 3.2 回滚决策流程

```
问题发生
    │
    ├── 应用逻辑/性能问题？
    │       │
    │       └── Yes ──────────────────────────► 【应用版本回滚】
    │                                            git checkout <tag>
    │                                            docker compose up -d --build
    │
    ├── 权限/授权问题？
    │       │
    │       └── Yes ──────────────────────────► 【权限回滚/重放】
    │                                            重新执行旧版本权限脚本
    │                                            或执行修复脚本
    │
    └── DDL/数据结构问题？
            │
            ├── 向前兼容（新增表/列）？
            │       └── Yes ──────────────────► 无需回滚，旧应用可正常工作
            │
            └── 破坏性变更（删除/修改）？
                    └── Yes ──────────────────► 【数据库级恢复】
                                                 pg_restore 恢复备份
```

### 3.3 应用版本回滚

**触发条件**：
- 应用代码逻辑错误
- 性能问题
- 兼容性问题（与其他服务）
- 功能回退需求

**操作步骤**：

```bash
# 方式 1: Git 部署环境
git checkout <previous_tag>
pip install -e .  # 重新安装依赖
systemctl restart engram-gateway  # 或 supervisorctl restart

# 方式 2: Docker 环境
docker compose -f docker-compose.unified.yml down
export VERSION=<previous_version>
docker compose -f docker-compose.unified.yml pull
docker compose -f docker-compose.unified.yml up -d

# 方式 3: Kubernetes
kubectl set image deployment/engram engram=engram:<previous_version>
kubectl rollout status deployment/engram
```

**注意事项**：
- **不需要回滚数据库**：DDL 变更通常向前兼容
- **验证兼容性**：确认旧版本应用可与当前数据库结构正常工作

### 3.4 权限回滚/重放

**触发条件**：
- 权限配置错误导致服务无法访问
- 角色权限变更导致功能异常
- 安全策略调整后的回退

**操作步骤**：

```bash
# 方式 1: 重新执行旧版本权限脚本
git checkout <previous_tag> -- sql/04_roles_and_grants.sql sql/05_openmemory_roles_and_grants.sql
engram-migrate --dsn "$POSTGRES_DSN" --apply-roles --apply-openmemory-grants

# 方式 2: 直接执行修复 SQL
psql "$POSTGRES_DSN" <<EOF
-- 恢复特定权限
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA logbook TO engram_app_readwrite;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA logbook TO engram_app_readwrite;
EOF

# 方式 3: 完全重新应用权限（推荐）
engram-migrate --dsn "$POSTGRES_DSN" --apply-roles --apply-openmemory-grants
engram-migrate --dsn "$POSTGRES_DSN" --verify --verify-strict
```

**常见权限问题修复**：

| 问题 | 症状 | 修复命令 |
|------|------|----------|
| Role 不存在 | `role "xxx" does not exist` | `--apply-roles` |
| Schema 权限缺失 | `permission denied for schema` | `--apply-roles` |
| 表权限缺失 | `permission denied for table` | `--apply-roles --apply-openmemory-grants` |
| DEFAULT PRIVILEGES 缺失 | 新表无权限 | 重新执行权限脚本 |

### 3.5 数据库级恢复

**触发条件**：
- 破坏性 DDL 变更（删除表/列、修改列类型）
- 数据迁移脚本错误导致数据损坏
- 需要完全回退到某个时间点

**前置条件**：
- **必须有有效的数据库备份**
- 备份时间点在问题发生之前

**操作步骤**：

```bash
#!/bin/bash
# 数据库级恢复流程

POSTGRES_DSN="${POSTGRES_DSN:?请设置 POSTGRES_DSN 环境变量}"
BACKUP_FILE="${1:?请提供备份文件路径}"
PREVIOUS_TAG="${2:?请提供要回滚到的版本标签}"

# 1. 停止所有应用服务
echo "=== 步骤 1: 停止应用服务 ==="
docker compose -f docker-compose.unified.yml down

# 2. 恢复数据库备份
echo "=== 步骤 2: 恢复数据库备份 ==="
# 提取数据库名
DB_NAME=$(echo "$POSTGRES_DSN" | sed -n 's/.*\/\([^?]*\).*/\1/p')

# 恢复备份（-c 清理现有对象）
pg_restore -d "$POSTGRES_DSN" -c "$BACKUP_FILE"

# 3. 回滚应用代码
echo "=== 步骤 3: 回滚应用代码 ==="
git checkout "$PREVIOUS_TAG"

# 4. 重新启动服务
echo "=== 步骤 4: 重新启动服务 ==="
docker compose -f docker-compose.unified.yml up -d

# 5. 验证恢复结果
echo "=== 步骤 5: 验证恢复结果 ==="
engram-logbook health
engram-migrate --dsn "$POSTGRES_DSN" --verify

echo "[SUCCESS] 数据库恢复完成"
```

**恢复后检查清单**：

- [ ] 数据库连接正常
- [ ] 所有 Schema 存在
- [ ] 所有表存在
- [ ] 权限验证通过
- [ ] 应用服务正常启动
- [ ] 业务功能验证通过

### 3.6 回滚风险评估矩阵

| DDL 变更类型 | 向前兼容 | 回滚方式 | 风险等级 | 数据影响 |
|--------------|----------|----------|----------|----------|
| 新增 Schema | ✓ 是 | 无需回滚 | 低 | 无 |
| 新增表 | ✓ 是 | 无需回滚 | 低 | 无 |
| 新增列（可空/有默认值） | ✓ 是 | 无需回滚 | 低 | 无 |
| 新增索引 | ✓ 是 | 无需回滚 | 低 | 无 |
| 新增约束 | ⚠ 部分 | 评估后决定 | 中 | 可能阻止写入 |
| 修改列类型 | ✗ 否 | 数据库恢复 | 高 | 可能丢失数据 |
| 删除列 | ✗ 否 | 数据库恢复 | 高 | 丢失数据 |
| 删除表 | ✗ 否 | 数据库恢复 | 高 | 丢失数据 |
| 重命名列/表 | ✗ 否 | 数据库恢复 | 高 | 旧代码无法工作 |

---

## 4. 日志留存与退出码传递

### 4.1 tee + PIPESTATUS 模式说明

在 CI/CD 和运维脚本中，需要同时实现：
1. **日志留存**：将输出写入文件供后续分析
2. **实时输出**：在终端显示执行过程
3. **正确的退出码**：确保 CI 门禁正确判断成功/失败

**问题**：`tee` 命令始终返回 0，会覆盖前一个命令的退出码。

**解决方案**：使用 `${PIPESTATUS[0]}` 获取管道中第一个命令的退出码。

### 4.2 可复制的代码片段

#### 4.2.1 单步命令（带日志留存）

```bash
# DDL 迁移
engram-migrate --dsn "$POSTGRES_DSN" \
    2>&1 | tee migration.log
exit ${PIPESTATUS[0]}

# 应用角色权限
engram-migrate --dsn "$POSTGRES_DSN" \
    --apply-roles \
    --apply-openmemory-grants \
    2>&1 | tee roles.log
exit ${PIPESTATUS[0]}

# 严格模式验证
engram-migrate --dsn "$POSTGRES_DSN" \
    --verify \
    --verify-strict \
    --verify-gate-policy fail_and_warn \
    2>&1 | tee verify.log
exit ${PIPESTATUS[0]}

# 健康检查
engram-logbook health \
    2>&1 | tee health.log
exit ${PIPESTATUS[0]}
```

#### 4.2.2 完整流程脚本

```bash
#!/bin/bash
# ============================================================
# Engram 迁移流程 - 带日志留存与退出码传递
# ============================================================

set -o pipefail  # 确保管道中任一命令失败时整体失败

POSTGRES_DSN="${POSTGRES_DSN:?请设置 POSTGRES_DSN}"
LOG_DIR="${LOG_DIR:-.artifacts/migrate}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$LOG_DIR"

# 函数：执行命令并正确处理退出码
run_with_log() {
    local log_file="$1"
    shift
    "$@" 2>&1 | tee "$log_file"
    local exit_code=${PIPESTATUS[0]}
    if [ "$exit_code" -ne 0 ]; then
        echo "[ERROR] 命令失败，退出码: $exit_code"
        echo "[ERROR] 日志文件: $log_file"
        return $exit_code
    fi
    return 0
}

# 步骤 1: 迁移计划
echo "=== 步骤 1: 查看迁移计划 ==="
run_with_log "$LOG_DIR/plan_${TIMESTAMP}.log" \
    engram-migrate --plan --apply-roles --verify || exit $?

# 步骤 2: DDL 迁移
echo "=== 步骤 2: DDL 迁移 ==="
run_with_log "$LOG_DIR/ddl_${TIMESTAMP}.log" \
    engram-migrate --dsn "$POSTGRES_DSN" || exit $?

# 步骤 3: 应用权限
echo "=== 步骤 3: 应用权限 ==="
run_with_log "$LOG_DIR/roles_${TIMESTAMP}.log" \
    engram-migrate --dsn "$POSTGRES_DSN" \
        --apply-roles \
        --apply-openmemory-grants || exit $?

# 步骤 4: 严格验证
echo "=== 步骤 4: 严格验证 ==="
run_with_log "$LOG_DIR/verify_${TIMESTAMP}.log" \
    engram-migrate --dsn "$POSTGRES_DSN" \
        --verify \
        --verify-strict \
        --verify-gate-policy fail_and_warn || exit $?

# 步骤 5: 健康检查
echo "=== 步骤 5: 健康检查 ==="
run_with_log "$LOG_DIR/health_${TIMESTAMP}.log" \
    engram-logbook health || exit $?

echo ""
echo "[SUCCESS] 迁移完成！日志目录: $LOG_DIR"
```

#### 4.2.3 CI 配置示例（GitHub Actions）

```yaml
# .github/workflows/migrate.yml
name: Database Migration

on:
  workflow_dispatch:
    inputs:
      environment:
        description: 'Target environment'
        required: true
        type: choice
        options:
          - staging
          - production

jobs:
  migrate:
    runs-on: ubuntu-latest
    environment: ${{ github.event.inputs.environment }}
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: pip install -e .
      
      - name: View migration plan
        run: |
          engram-migrate --plan --apply-roles --verify \
            2>&1 | tee plan.log
          exit ${PIPESTATUS[0]}
      
      - name: Execute DDL migration
        env:
          POSTGRES_DSN: ${{ secrets.POSTGRES_DSN }}
        run: |
          engram-migrate --dsn "$POSTGRES_DSN" \
            2>&1 | tee ddl.log
          exit ${PIPESTATUS[0]}
      
      - name: Apply roles and grants
        env:
          POSTGRES_DSN: ${{ secrets.POSTGRES_DSN }}
        run: |
          engram-migrate --dsn "$POSTGRES_DSN" \
            --apply-roles \
            --apply-openmemory-grants \
            2>&1 | tee roles.log
          exit ${PIPESTATUS[0]}
      
      - name: Verify (strict mode)
        env:
          POSTGRES_DSN: ${{ secrets.POSTGRES_DSN }}
        run: |
          engram-migrate --dsn "$POSTGRES_DSN" \
            --verify \
            --verify-strict \
            --verify-gate-policy fail_and_warn \
            2>&1 | tee verify.log
          exit ${PIPESTATUS[0]}
      
      - name: Health check
        env:
          POSTGRES_DSN: ${{ secrets.POSTGRES_DSN }}
        run: |
          engram-logbook health \
            2>&1 | tee health.log
          exit ${PIPESTATUS[0]}
      
      - name: Upload logs
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: migration-logs
          path: |
            *.log
```

### 4.3 技术要点说明

| 技术点 | 说明 | 示例 |
|--------|------|------|
| `2>&1` | 合并 stderr 到 stdout | 确保错误信息也写入日志 |
| `\| tee file.log` | 同时输出到终端和文件 | 实时查看 + 日志留存 |
| `${PIPESTATUS[0]}` | 获取管道中第一个命令的退出码 | 绕过 tee 返回 0 的问题 |
| `set -o pipefail` | 管道中任一命令失败则整体失败 | 脚本级别的保护 |
| `exit ${PIPESTATUS[0]}` | 传递正确的退出码 | CI 门禁依赖退出码 |

**注意事项**：

1. `${PIPESTATUS[0]}` 是 **Bash 特性**，在 `sh` 中不可用
2. 必须在 `tee` 命令的**同一行或紧接着的下一行**使用
3. 执行其他命令后 `PIPESTATUS` 数组会被覆盖

---

## 5. 相关文档

| 文档 | 说明 |
|------|------|
| [upgrade_after_sql_renumbering.md](upgrade_after_sql_renumbering.md) | SQL 重编号后升级 Runbook |
| [sql_file_inventory.md](sql_file_inventory.md) | SQL 文件完整清单与功能说明 |
| [sql_renumbering_map.md](sql_renumbering_map.md) | SQL 文件重编号映射表 |
| [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署、验收与排错指南 |
| [ADR: 权限验证门控策略](../architecture/adr_verify_permissions_gate_policy.md) | 验证门控策略设计决策 |

---

## 6. 变更历史

| 日期 | 变更内容 |
|------|----------|
| 2026-02-01 | 初始版本，从 upgrade_after_sql_renumbering.md 抽取并扩展 |
