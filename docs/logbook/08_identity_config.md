# 身份配置（User YAML）

本文件说明如何通过 **本地 YAML** 配置用户身份（`user_id` / `display_name` / `accounts` 等），并同步到 Logbook 数据库的 `identity.*` schema。

> 适用场景：
> - 让 Gateway/MCP 写入审计里能显示“我是谁”（`actor_user_id` 可校验）
> - 为个人/团队准备稳定的 `user_id` 与账号映射（Git/SVN/GitLab/Email）
> - 为后续权限治理（allowlist）与空间隔离（`private:<user_id>`）提供基础数据

---

## 配置文件位置

### 仓库内（推荐，本地私有）

- **用户配置**：`./.agentx/users/<user_id>.yaml`
- **角色画像（可选）**：`./.agentx/roles/<user_id>.md`

> 默认情况下，仓库的 `.gitignore` 已忽略 `./.agentx/`，适合每个开发者维护自己的本地身份配置。

### 主目录（可选，个人覆盖）

- `~/.agentx/user.yaml`

说明：代码中提供了 `load_home_user_config()` 用于读取该文件（适合自定义脚本做覆盖合并）。是否纳入默认同步流程取决于上层调用方式。

---

## YAML 字段说明

以下字段由 `src/engram/logbook/identity_sync.py::parse_user_config()` 解析。

### 必填

- **`user_id`**：用户唯一标识（稳定主键）
  - 用途：写入 `identity.users.user_id`；同时建议作为 MCP 调用参数 `actor_user_id`

### 可选

- **`display_name`**：展示名（默认等于 `user_id`）
  - 用途：写入 `identity.users.display_name`

- **`aliases`**：别名列表（默认空列表）
  - 用途：写入 `identity.accounts.aliases_json`（同步时会给该用户的每个 account 记录写同一份 aliases）

- **`accounts`**：账号映射（默认空）
  - 结构：`accounts: { <account_type>: <value> }`
  - `account_type` 受数据库约束，允许值见 `sql/01_logbook_schema.sql`：
    - `svn` / `gitlab` / `git` / `email`
  - `value` 支持两种写法：
    - 字符串：表示 `username`
    - 对象：支持 `username`、`email`（以及未来扩展字段）
  - 落库规则（同步时）：
    - `account_name = username 优先，否则使用 email`
    - `email = value.email（若提供）`

- **`roles`**：角色列表（默认空列表）
  - 用途：写入 `identity.users.roles_json`

- **`is_active`**：是否启用（默认 `true`）
  - 用途：写入 `identity.users.is_active`

- **`visibility_default`**：默认可见性（默认 `team`）
  - 说明：当前仅作为配置字段解析保留；是否参与落库/策略取决于上层实现版本。

---

## 示例（最小可用）

保存为 `./.agentx/users/xxx.yaml`：

```yaml
user_id: xxx
display_name: xxx
aliases:
  - xxxx
accounts:
  git: xxxx          # 也可以写成 { username: xxxx, email: xxx@xx.com }
roles:
  - dev
is_active: true
```

---

## 如何让配置生效（同步到数据库）

身份 YAML 需要同步到 Postgres 后，Gateway 才能基于 `identity.users` 校验 `actor_user_id`。

在已安装项目依赖的环境中执行：

```powershell
# 进入仓库根目录后执行
mkdir .agentx\users -Force | Out-Null

# 设置数据库连接（示例）
$env:POSTGRES_DSN="postgresql://user:password@127.0.0.1:5432/proj_a"

# 执行身份同步
python -m engram.logbook.identity_sync --repo-root . --dsn $env:POSTGRES_DSN
```

> 提示：若你在 MCP 调用中传入 `actor_user_id`，但数据库里尚无该用户，Gateway 会按 `UNKNOWN_ACTOR_POLICY`（默认 `degrade`）进行降级/拒绝处理。因此建议在首次使用前先完成一次同步。

---

## 与 MCP/记忆写入的关系（要点）

- **个人记忆空间**：推荐写入 `target_space = private:<user_id>`，例如 `private:xxx`
- **审计操作者**：在 MCP 调用里传 `actor_user_id = <user_id>`（与本 YAML 的 `user_id` 一致）

更新时间：2026-02-05

