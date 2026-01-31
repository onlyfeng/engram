# ADR: 权限验证门控策略（Verify Permissions Gate Policy）

> 状态: **已采纳**  
> 创建日期: 2026-01-31  
> 决策者: Engram Core Team

---

## 1. 背景与目标

### 1.1 背景

Engram 数据库权限验证脚本 (`99_verify_permissions.sql`) 用于验证数据库角色和权限配置是否正确。该脚本输出四个级别的检查结果：

| 级别 | 含义 | 示例场景 |
|------|------|----------|
| `OK` | 检查通过 | 角色存在、权限正确 |
| `FAIL` | 严重问题，必须修复 | 核心角色缺失、owner 错误 |
| `WARN` | 潜在问题，可能影响安全 | PUBLIC 仍有 CREATE 权限 |
| `SKIP` | 条件不满足，跳过检查 | LOGIN 角色不存在（logbook-only 模式） |

在不同环境（CI、生产、本地开发）中，对这些输出级别应采取不同的门控策略。

### 1.2 目标

1. **定义清晰的门控级别**：明确 CI 和生产环境对 FAIL/WARN 的处理策略
2. **统一实现链路**：从 SQL 脚本到 CI 门禁的完整链路可追溯
3. **提供例外机制**：支持特殊场景下绕过门禁（需显式声明）

### 1.3 非目标

1. **不改变验证脚本核心逻辑**：验证项和输出级别保持不变
2. **不引入新的依赖**：使用现有的 PostgreSQL 和 Python 机制
3. **不影响现有部署流程**：保持向后兼容

---

## 2. 实现链路（SSOT 到 CI 门禁）

### 2.1 链路概览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              实现链路图                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────────────────┐                                               │
│  │ sql/verify/              │   SSOT（唯一真实来源）                         │
│  │ 99_verify_permissions.sql│   - 定义所有验证项                             │
│  │                          │   - 输出 FAIL/WARN/OK/SKIP                    │
│  │                          │   - strict 模式下 RAISE EXCEPTION             │
│  └────────────┬─────────────┘                                               │
│               │                                                             │
│               │ execute_sql_file()                                          │
│               ▼                                                             │
│  ┌──────────────────────────┐                                               │
│  │ src/engram/logbook/      │   Python 执行入口                              │
│  │ migrate.py               │   - scan_sql_files() 发现脚本                  │
│  │                          │   - SET engram.verify_strict = '1'            │
│  │                          │   - SET engram.schema_prefix = 'xxx'          │
│  └────────────┬─────────────┘                                               │
│               │                                                             │
│               │ run_migrate(verify=True, verify_strict=True)                │
│               ▼                                                             │
│  ┌──────────────────────────┐                                               │
│  │ src/engram/logbook/cli/  │   CLI 入口                                    │
│  │ db_migrate.py            │   - --verify: 执行验证脚本                     │
│  │                          │   - --verify-strict: 严格模式                  │
│  │                          │   - ENGRAM_VERIFY_STRICT=1 环境变量            │
│  └────────────┬─────────────┘                                               │
│               │                                                             │
│               │ engram-migrate --verify --verify-strict                     │
│               ▼                                                             │
│  ┌──────────────────────────┐                                               │
│  │ .github/workflows/       │   CI 门禁                                     │
│  │ ci.yml                   │   - 迁移后执行 --verify-strict                 │
│  │                          │   - FAIL 或 WARN 阻断流水线                    │
│  │                          │   - 日志上传供诊断                             │
│  └────────────┬─────────────┘                                               │
│               │                                                             │
│               │ depends_on: permissions_verify                              │
│               ▼                                                             │
│  ┌──────────────────────────┐                                               │
│  │ docker-compose.          │   生产部署                                     │
│  │ unified.yml              │   - permissions_verify 服务                    │
│  │                          │   - 依赖 logbook_migrate 完成                  │
│  │                          │   - 验证失败不阻断（标准模式）                   │
│  └──────────────────────────┘                                               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 各层职责

| 层级 | 文件 | 职责 |
|------|------|------|
| **SQL (SSOT)** | `sql/verify/99_verify_permissions.sql` | 定义验证逻辑、输出级别、strict 模式异常 |
| **Python 执行** | `src/engram/logbook/migrate.py` | 扫描/执行 SQL、设置配置变量、传递 strict 标志 |
| **CLI 入口** | `src/engram/logbook/cli/db_migrate.py` | 解析命令行参数、调用 migrate 函数 |
| **CI 门禁** | `.github/workflows/ci.yml` | 执行 strict 验证、阻断失败流水线 |
| **生产部署** | `docker-compose.unified.yml` | 标准模式验证、日志输出 |

### 2.3 代码引用

#### SQL 层（strict 模式实现）

```sql:1068:1137:sql/verify/99_verify_permissions.sql
-- 11. Strict 模式汇总与异常处理
-- 当 engram.verify_strict = '1' 时，如果有任何 FAIL 则抛出异常
DO $$
DECLARE
    v_total_fail INT;
    v_total_warn INT;
    v_is_strict BOOLEAN;
    ...
BEGIN
    -- 检查是否启用 strict 模式
    v_strict_setting := COALESCE(
        NULLIF(current_setting('engram.verify_strict', true), ''),
        '0'
    );
    v_is_strict := (v_strict_setting = '1');
    
    -- ...汇总 fail_count 和 warn_count...
    
    -- Strict 模式下，有 FAIL 或 WARN 则抛出异常（用于 CI 门禁）
    IF v_is_strict AND (v_total_fail > 0 OR v_total_warn > 0) THEN
        RAISE EXCEPTION 'VERIFY_STRICT_FAILED: 权限验证失败，共 % 项 FAIL，% 项 WARN...', 
            v_total_fail, v_total_warn, v_failed_sections;
    END IF;
END $$;
```

#### Python 层（strict 标志传递）

```python:1619:1626:src/engram/logbook/migrate.py
# 设置 verify_strict 模式（环境变量或参数）
effective_verify_strict = (
    verify_strict or os.environ.get("ENGRAM_VERIFY_STRICT", "") == "1"
)
if effective_verify_strict:
    log_info("启用 verify strict 模式", quiet=quiet)
    with conn.cursor() as cur:
        cur.execute("SET engram.verify_strict = '1'")
```

#### CLI 层（参数定义）

```python:134:140:src/engram/logbook/cli/db_migrate.py
parser.add_argument(
    "--verify-strict",
    action="store_true",
    default=False,
    help="启用验证严格模式。当验证脚本检测到 FAIL 时抛出异常导致迁移失败。"
    "也可通过环境变量 ENGRAM_VERIFY_STRICT=1 启用",
)
```

#### CI 层（门禁配置）

```yaml:78:92:.github/workflows/ci.yml
- name: Verify database migrations (strict mode)
  env:
    POSTGRES_DSN: postgresql://postgres:postgres@localhost:5432/engram_test
    ENGRAM_TESTING: "1"
  run: |
    # 严格模式验证：FAIL 或 WARN 会导致非零退出码，阻断 CI
    python -m engram.logbook.cli.db_migrate \
      --dsn "$POSTGRES_DSN" \
      --verify \
      --verify-strict \
      2>&1 | tee verify-output-${{ matrix.python-version }}.log
    exit ${PIPESTATUS[0]}
```

#### Compose 层（生产部署）

```yaml:102:115:docker-compose.unified.yml
permissions_verify:
  build:
    context: .
    dockerfile: docker/engram.Dockerfile
  depends_on:
    postgres:
      condition: service_healthy
    logbook_migrate:
      condition: service_completed_successfully
  environment:
    <<: *postgres_admin_env
  command: >
    sh -c 'engram-migrate --dsn "$POSTGRES_DSN" --verify'
```

---

## 3. 决策：Gate Level 策略

### 3.1 方案对比

| 方案 | FAIL 处理 | WARN 处理 | 适用场景 |
|------|-----------|-----------|----------|
| **FAIL-only** | 阻断 | 仅提示 | 生产部署（宽松） |
| **FAIL+WARN** | 阻断 | 阻断 | CI 门禁（严格） |

### 3.2 决策

**采用双模式策略**：

| 环境 | 模式 | 触发方式 | 行为 |
|------|------|----------|------|
| **CI** | FAIL+WARN（严格模式） | `--verify-strict` 或 `ENGRAM_VERIFY_STRICT=1` | FAIL 或 WARN 阻断流水线 |
| **生产** | FAIL-only（标准模式） | `--verify`（无 strict） | FAIL 输出警告，WARN 仅提示 |
| **本地开发** | 可选 | 开发者自行决定 | 人工检查输出 |

### 3.3 决策理由

1. **CI 严格要求**：
   - CI 是代码合入的最后门禁，应拒绝任何潜在问题
   - WARN 级别的问题（如 PUBLIC 仍有 CREATE 权限）虽非致命，但可能带来安全风险
   - 早期发现问题比生产环境修复成本低

2. **生产宽松处理**：
   - 生产环境需要优先保证服务可用性
   - WARN 级别的问题可在后续维护窗口修复
   - 避免因非致命问题阻断部署

3. **本地灵活选择**：
   - 开发者可根据需要选择模式
   - 支持快速迭代和调试

---

## 4. 例外机制

### 4.1 CI 例外（跳过 strict 验证）

**场景**：已知的非阻断性问题，需要紧急合入代码

**方式**：

```yaml
# .github/workflows/ci.yml 中临时修改（需要 PR 审批）
- name: Verify database migrations (with known exceptions)
  env:
    ENGRAM_VERIFY_STRICT: "0"  # 临时禁用 strict
  run: |
    python -m engram.logbook.cli.db_migrate \
      --dsn "$POSTGRES_DSN" \
      --verify \
      2>&1 | tee verify-output.log
    # 人工检查日志确认 WARN 是预期的
```

**审批要求**：

- 必须在 PR 描述中说明例外原因
- 必须附带修复计划和时间表
- 需要代码所有者（CODEOWNERS）批准

### 4.2 生产例外（启用 strict）

**场景**：高安全要求的生产环境

**方式**：

```yaml
# docker-compose.unified.yml
permissions_verify:
  command: >
    sh -c 'engram-migrate --dsn "$POSTGRES_DSN" --verify --verify-strict'
```

或环境变量：

```bash
ENGRAM_VERIFY_STRICT=1 docker compose up permissions_verify
```

### 4.3 特定检查项跳过

**当前不支持**。所有验证项作为整体执行，不支持跳过单个检查项。

**未来可考虑**：通过配置变量（如 `engram.verify_skip_checks`）支持跳过特定检查项。

---

## 5. 输出级别定义（SSOT）

### 5.1 级别定义

| 级别 | 语义 | SQL 实现 | 示例 |
|------|------|----------|------|
| `OK` | 检查通过，最终态 | `RAISE NOTICE 'OK: ...'` | 角色存在、权限正确 |
| `FAIL` | 严重问题，必须修复 | `RAISE WARNING 'FAIL: ...'`；增加 `fail_count` | 核心角色缺失 |
| `WARN` | 潜在问题，可能影响安全 | `RAISE WARNING 'WARN: ...'`；增加 `warn_count` | PUBLIC 有 CREATE 权限 |
| `SKIP` | 条件不满足，跳过检查 | `RAISE NOTICE 'SKIP: ...'` | LOGIN 角色不存在 |

### 5.2 核心验证项

| 验证项 | 失败级别 | 说明 |
|--------|----------|------|
| NOLOGIN 角色存在性 | FAIL | `engram_*`、`openmemory_*` 角色 |
| LOGIN 角色 membership | FAIL（目标角色不存在）/ WARN（LOGIN 角色不存在） | 登录角色正确继承 NOLOGIN 角色 |
| public schema 权限 | FAIL（应用角色有 CREATE）/ WARN（PUBLIC 有 CREATE） | 所有应用角色不应有 CREATE 权限 |
| 目标 schema owner | FAIL | OM schema owner 应为 openmemory_migrator |
| 默认权限配置 | FAIL | pg_default_acl 中的 TABLE/SEQUENCE 授权 |
| 数据库级权限硬化 | FAIL（CREATE/TEMP 未撤销） | PUBLIC 不应有 CREATE/TEMP 权限 |

---

## 6. 最佳实践

### 6.1 CI 集成

```yaml
# 推荐的 CI 配置
- name: Run database migrations
  run: |
    engram-migrate --dsn "$POSTGRES_DSN" --apply-roles --apply-openmemory-grants

- name: Verify database migrations (strict mode)
  run: |
    engram-migrate --dsn "$POSTGRES_DSN" --verify --verify-strict
```

### 6.2 本地开发

```bash
# 快速验证（标准模式）
make verify-permissions

# 严格验证（CI 等效）
make verify-permissions-strict
```

### 6.3 生产部署检查清单

| 步骤 | 命令 | 预期结果 |
|------|------|----------|
| 1. 执行迁移 | `engram-migrate --dsn "$DSN" --apply-roles` | `ok: true` |
| 2. 标准验证 | `engram-migrate --dsn "$DSN" --verify` | 无 FAIL |
| 3. 查看 WARN | 检查 verify 日志 | 评估是否需要修复 |

---

## 7. 相关文档

| 文档 | 说明 |
|------|------|
| [部署验证与排错指南](../logbook/03_deploy_verify_troubleshoot.md) | 完整的部署和验证流程 |
| [SQL 文件清单](../logbook/sql_file_inventory.md) | SQL 文件分类和执行规则 |
| [环境变量参考](../reference/environment_variables.md) | `ENGRAM_VERIFY_STRICT` 等环境变量 |

---

## 8. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-31 | v1.0 | 初始版本：定义 Gate Level 策略、实现链路、例外机制 |
