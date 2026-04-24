# CI 迭代调整常见陷阱与操作规范

> **定位**：Engram 记忆的本地兜底文档。当 Engram MCP 不可用时，Agent 应参考本文件。
> 记忆同步：本文件内容与 Engram 中 `kind: PITFALL/PROCEDURE` 的 CI 相关记忆保持一致。

---

## 1. 根因：跨文件一致性检查密度极高

`make ci` 包含 **31 个检查目标**，其中至少 **12 个为跨文件一致性检查**。修改任一文件而未同步关联文件，即触发失败。

### 最高耦合区域

| 文件 A | 必须同步的文件 B | 对应检查 |
|--------|-----------------|---------|
| `workflow_contract.v2.json` | `contract.md` + `ci.yml` + `Makefile` | 8 个合约检查 |
| `docs/acceptance/iteration_*_regression.md` | `00_acceptance_matrix.md` + evidence JSON + fixtures | 6 个迭代检查 |
| `.env.example` | `environment_variables.md` + `ci.yml` | `check-env-consistency` |
| Gateway `__init__.py` | `01_public_api.md` | `check-gateway-public-api-docs-sync` |

### 单点负责文件（禁止多代理并行编辑）

- `workflow_contract.v2.json` 及其 8 个关联脚本
- `pyproject.toml`
- `Makefile`
- `mypy_baseline.txt`

---

## 2. 迭代文档变更的六大易错场景

| # | 场景 | 触发的检查 | 预防措施 |
|---|------|-----------|---------|
| 1 | `.iteration/` 链接泄漏到版本化文档 | `check-iteration-docs` | 禁止在 `docs/` 中引用 `.iteration/` 路径 |
| 2 | SUPERSEDED 声明缺失或索引表非降序 | `check-iteration-docs` | 使用晋升脚本自动处理 |
| 3 | 手动编辑 evidence JSON | `check-schemas` | **必须使用** `record_iteration_evidence.py` 生成 |
| 4 | 手动修改受控块内容 | `check-iteration-regression-generated-blocks` | 使用 `sync_iteration_regression.py --write` 同步 |
| 5 | Fixtures 与源数据不同步 | `check-iteration-fixtures-freshness` | 运行 `update_iteration_fixtures.py` |
| 6 | 模板占位符 `{N}` 残留 | `check-iteration-docs-placeholders` | 晋升前全局搜索替换 |

---

## 3. 顺序依赖（鸡生蛋问题）

| 问题 | 正确顺序 |
|------|---------|
| SUPERSEDED 声明需要后继迭代已存在于索引 | 先创建并晋升新迭代 → 再标记旧迭代 SUPERSEDED |
| regression 文档需引用 evidence 文件 | 先 `record_iteration_evidence.py` 生成 → 再同步受控块 |
| 受控块依赖源数据生成 | 先更新源数据 → 再 `sync_iteration_regression.py --write` → 再提交 |

---

## 4. 标准操作流程

### 4.1 新迭代初始化与晋升

```bash
make iteration-init-next                    # 自动选择下一个编号（勿手动创建）
# 编辑 .iteration/<N>/plan.md 和 regression.md
make iteration-promote N=<N>                # 晋升到 docs/acceptance/（勿手动复制）
make check-iteration-docs                   # 立即验证
```

### 4.2 证据文件生成（禁止手动创建 JSON）

```bash
python scripts/ci/record_iteration_evidence.py  # 自动检测环境、填充正确的 os/arch/runner_label
make check-schemas                              # 验证 Schema
```

### 4.3 受控块同步

```bash
python scripts/ci/sync_iteration_regression.py --write  # 自动同步 min_gate_block 和 evidence_snippet
make check-iteration-docs                               # 再次验证
```

### 4.4 修改迭代文档的完整顺序

1. 晋升新迭代
2. 更新索引表（`00_acceptance_matrix.md`）
3. 更新旧迭代 SUPERSEDED 声明
4. 生成证据文件
5. 同步受控块
6. `make check-iteration-docs`
7. `make ci`

### 4.5 修复 CI 失败

```bash
make ci              # 1. 先复现，确认实际错误（不要假设原因）
# 根据错误输出精准修改，避免过度修复
make ci              # 2. 再验证，确保无新增问题
```

### 4.6 分步验证快捷命令

| 改了什么 | 立即运行 |
|---------|---------|
| 迭代文档 | `make check-iteration-docs` |
| JSON / Schema | `make check-schemas` |
| Workflow 合约 | `make validate-workflows-strict && make check-workflow-contract-docs-sync` |
| Makefile `ci` / no-make wrapper / OpenMemory launcher | `make check-local-ci-smoke && python scripts/ops/ci_no_make.py --dry-run --json` |
| 环境变量 | `make check-env-consistency` |
| Gateway 代码 | `make check-gateway-public-api-surface && make check-gateway-di-boundaries` |
| 全量验证 | `make ci` |

---

## 5. 实际案例

### 案例：evidence JSON 中 runner 字段矛盾（Iteration 13）

**问题**：`iteration_13_evidence.json` 中 `runner.os` 为 `windows-11`，而 `runner.runner_label` 为 `ubuntu-latest`，两者矛盾。

**根因**：手动创建 evidence JSON 文件，未使用脚本自动生成。

**修复**：将 `runner_label` 修正为 `windows-latest`。

**教训**：永远使用 `record_iteration_evidence.py` 生成证据文件，脚本会自动检测当前环境。

### 案例：PowerShell 下 git commit heredoc 语法失败

**问题**：在 Windows PowerShell 中使用 bash 风格的 heredoc 提交多行 commit message 会报语法错误：

```powershell
# 失败写法（bash heredoc，PowerShell 不支持）
git commit -m "$(cat <<'EOF'
feat: some feature

- detail 1
- detail 2
EOF
)"
```

**根因**：PowerShell 不支持 `<<'EOF'` heredoc 语法，`<` 被解释为重定向运算符，`&&` 也不是有效的语句分隔符（旧版 PowerShell）。

**正确写法**：

```powershell
# 方法 1：多个 -m 参数（每个 -m 产生一个段落）
git commit -m "feat: some feature" -m "- detail 1" -m "- detail 2"

# 方法 2：用分号替代 && 连接命令
cd e:\project; git add .; git commit -m "message"

# 方法 3：反引号换行（PowerShell 续行符）
git commit -m @"
feat: some feature

- detail 1
- detail 2
"@
```

**教训**：Agent 在 Windows 环境执行 shell 命令时，必须使用 PowerShell 兼容语法。常见差异：
- `&&` → `;`（命令分隔）
- `<<'EOF'...EOF` → 多个 `-m` 参数或 `@"..."@`（多行字符串）
- `dir /s /b` → `Get-ChildItem -Recurse`（目录列表）

### 案例：本地 `make ci` 与 `ci_no_make.py` 漂移

**问题**：`Makefile` 的 `ci:` 依赖增加目标后，`scripts/ops/ci_no_make.py` 未同步，导致 Windows / 无 GNU make 场景的本地 CI 等价入口少跑检查。

**根因**：`make ci`、`ci_no_make.py`、`scripts/windows/ci.ps1` 是同一目标的不同入口；新增本地门禁时只改 Makefile 会破坏等价性。

**修复**：新增/调整 `ci:` 依赖时同步 `CI_TARGET_ORDER` 与 `TARGET_COMMAND_TEMPLATES`，并运行：

```bash
make check-local-ci-smoke
python scripts/ops/ci_no_make.py --dry-run --json
```

**教训**：CI 入口属于契约面。新增 PR 专属的轻量回归（如 OpenMemory launcher wrapper）应接入 `check-local-ci-smoke`，而不是只依赖 GitHub 上的完整 `pytest tests/ci/`。

---

## 6. OpenMemory pg_type 启动竞态（nightly 自愈）

**症状**：nightly "Wait for services to be healthy" 步骤在 OpenMemory 健康检查失败，日志含

```
[DB] Init failed: error: duplicate key value violates unique constraint "pg_type_typname_nsp_index"
detail: 'Key (typname, typnamespace)=(<some_table>, <oid>) already exists.'
```

**根因**：OpenMemory 上游镜像首次 init 偶发竞态——表/类型重复创建。Postgres 为每个 TABLE 隐式创建同名 row type，因此 `pg_type` 上的冲突源往往是上一次半初始化遗留的 TABLE，而不是真正的 TYPE。

**易踩坑**：
- 在自愈 SQL 中硬编码具体表名（如 `openmemory_users`），下次冲突表名一变就失效——2026-04-16 nightly 实际冲突在 `openmemory_memories` 上。
- 用 `DROP TYPE openmemory.<name> CASCADE` 兜底，会触发 `cannot drop type ... because table ... requires it`，整步退出 1。

**正确写法**：枚举 `openmemory` schema 全部 TABLE 做 `DROP TABLE ... CASCADE`，再清理残留的 enum/composite/domain 类型。已抽离至 `scripts/ci/recover_openmemory_init_race.sh`，nightly.yml 直接调用。

**本地校验**：
- `bash scripts/ci/recover_openmemory_init_race.sh --print-sql` 可在无 Docker 环境下输出将执行的 SQL。
- `make recover-openmemory-init-race` 在已起 compose 时手动触发自愈。
- `tests/ci/test_recover_openmemory_init_race.py` 锁死回归契约：禁止重新引入硬编码表名或对表用 `DROP TYPE`，并要求 nightly.yml 调用脚本而非 inline SQL。

---

## 7. 相关文档

| 文档 | 用途 |
|------|------|
| [CI 门禁 Runbook](ci_gate_runbook.md) | 完整门禁参考、回滚策略、例外审批 |
| [迭代操作手册](iteration_runbook.md) | 迭代生命周期详细流程 |
| [迭代本地草稿指南](iteration_local_drafts.md) | `.iteration/` 草稿工作流 |
| [Agent 协作指南](agents.md) | 子代理分工、单点负责规则 |
