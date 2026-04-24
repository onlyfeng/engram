# Engram

AI 友好的事实账本与记忆管理模块 - 为 AI Agent 提供可审计、可回放的证据链与可演化知识沉淀。

## 特性

- **Gateway（MCP 网关）**: 连接 Cursor IDE 与 OpenMemory，提供策略校验、审计落库、失败降级
- **Logbook（事实账本）**: 基于 PostgreSQL 的结构化事件日志，支持 SCM 同步、证据链追溯
- **多项目/多用户**: 支持团队空间与私有空间隔离，每项目独立数据库
- **AI 友好**: 结构化 JSON 输出，易于 LLM 理解和处理

## 推荐使用方式

**服务端部署 Gateway，客户端通过 MCP 协议连接**

```
┌─────────────────────────────────────────────────────────┐
│  Cursor IDE / MCP Client（多个客户端）                   │
└────────────────────────┬────────────────────────────────┘
                         │ MCP JSON-RPC (HTTP)
┌────────────────────────▼────────────────────────────────┐
│  服务器：Gateway + Logbook + OpenMemory                  │
│  - 统一部署，集中管理                                    │
│  - 多项目隔离（PROJECT_KEY）                            │
│  - 多用户支持（actor_user_id + Space）                  │
└─────────────────────────────────────────────────────────┘
```

客户端只需配置 MCP 连接，无需安装 Engram 库。

---

## 快速开始

### 一、服务端部署

#### 1. 环境准备

```bash
# 克隆仓库
git clone https://github.com/onlyfeng/engram.git
cd engram

# macOS 原生开发：先准备系统依赖
brew install postgresql@18 pgvector node
brew services start postgresql@18
export PATH="$(brew --prefix postgresql@18)/bin:$PATH"

# Python 环境（推荐：venv）
python3 -m venv .venv
# 激活虚拟环境（二选一）：
source .venv/bin/activate   # Linux/macOS/WSL
# .\.venv\Scripts\Activate.ps1   # Windows PowerShell

# 安装依赖（服务端部署建议 full）
make install-full
# Windows 无 make 时可直接执行：
# pip install -e ".[full]"
```

**Windows 原生安装 GNU Make（推荐）**

```powershell
# 推荐（Scoop）
scoop install make

# 验证
make --version
```

> 若终端提示找不到 `make`，请关闭并重新打开 PowerShell 再试。
> WSL2 / Linux 原生安装 PostgreSQL、pgvector、Node.js 的命令请参考 [安装指南](docs/installation.md)。

#### 2. 一键初始化数据库

```bash
# 初始化数据库（需要 PostgreSQL 18+ 已安装）
# - 交互终端：会检测 4 个服务账号密码环境变量，并询问是否重设/切换部署模式（logbook-only / unified-stack）
# - 无 TTY（CI/脚本）：不会询问，按当前环境变量直接执行（不完整会报错）
make setup-db

# Linux/WSL2 常见：使用 postgres 账号执行管理员操作（peer auth / unix socket）
DB_ADMIN_PREFIX="sudo -u postgres" make setup-db
```

**Windows 无 make 时**：推荐使用 `.\scripts\windows\setup_db.ps1` 完成原生初始化；如需查看分步操作，再参考 [安装指南](docs/installation.md) 中的 Windows 章节（db-create → bootstrap-roles → migrate-ddl → apply-roles → apply-openmemory-grants → verify-permissions）。

> macOS / WSL2 原生推荐：执行 `make setup-db` 时选择 `2) unified-stack`，并在结束时将配置写入 `.env.local`。后续启动 OpenMemory / Gateway 时就可以直接加载 `.env.local`，不需要重复手敲 `POSTGRES_DSN` 和 `OM_PG_*`。
> 详细安装（PostgreSQL、pgvector、多平台）请参考 [安装指南](docs/installation.md)

#### 3. 启动服务

`make install-full` 只安装 Engram 依赖；`make gateway` **只启动 Gateway**，不会自动拉起 OpenMemory。  
推荐先明确部署方式，再执行对应步骤：

- **方式 A（原生部署）**：手动启动 OpenMemory，再启动 Gateway
- **方式 B（Docker Compose 统一栈）**：一条命令拉起 OpenMemory + Gateway + PostgreSQL

##### 3.1 推荐路径：macOS / Windows 原生 / WSL2（不走 Docker）

如果你日常是在 macOS、Windows 原生 PowerShell，或 Windows 的 WSL2(Debian) 下开发，推荐把流程分成“一次性初始化”和“日常启动”两部分。

**一次性初始化**

macOS / WSL2：

```bash
# 仓库内：激活 Python 环境
cd /path/to/engram
source .venv/bin/activate

# 初始化数据库、角色、权限
# 推荐选择 2) unified-stack，并在结束时写入 .env.local
make setup-db

# 如果 .env.local 里还没有 OpenMemory 的 API Key / TIER，可补充一次
printf '\nOM_API_KEY="change_me"\nOM_TIER="hybrid"\n' >> .env.local
```

Windows PowerShell：

```powershell
cd C:\path\to\engram
.\.venv\Scripts\Activate.ps1

# 建议直接用 Windows 脚本完成数据库初始化，并生成本地 .env.ps1
.\scripts\windows\setup_db.ps1

# 如需补充 OpenMemory API Key / TIER，可写入本地 .env.ps1
Add-Content -Path ".\.env.ps1" -Value '$env:OM_API_KEY="change_me"'
Add-Content -Path ".\.env.ps1" -Value '$env:OM_TIER="hybrid"'
```

如果你还没有安装 OpenMemory 的 `opm` 命令，再额外执行一次：

macOS / WSL2：

```bash
git clone https://github.com/caviraoss/openmemory.git ~/openmemory
cd ~/openmemory
git checkout v1.3.3
cd packages/openmemory-js
npm install
npm run build
npm link
```

Windows PowerShell：

```powershell
git clone https://github.com/caviraoss/openmemory.git $env:USERPROFILE\openmemory
cd $env:USERPROFILE\openmemory
git checkout v1.3.3
cd packages\openmemory-js
npm install
npm run build
npm link
```

> macOS / WSL2 下，`make setup-db` 生成的 `.env.local` 默认会包含 `POSTGRES_DSN`、`OPENMEMORY_BASE_URL` 以及常用的 `OM_PG_*` 配置；`make openmemory` / `make gateway` 会自动加载它。
> Windows 原生下，推荐使用 `.\scripts\windows\setup_db.ps1` 生成并维护本地 `.\.env.ps1`，后续终端直接加载它。

**日常启动**

终端 A：启动 OpenMemory

macOS / WSL2：

```bash
make openmemory
```

> `make openmemory` 现在会按以下顺序自动探测并启动 OpenMemory：
> 1. `OPENMEMORY_DIR`
> 2. `../openmemory/packages/openmemory-js`（推荐：Engram 同级目录的 `openmemory` checkout，可以是官方仓库、官方分支或任意 fork）
> 3. 常见的本地源码目录（如 `~/openmemory/...`、`~/Documents/openmemory/...`、`~/Documents/ai/openmemory/...`）
> 4. 若未找到可运行的本地源码目录，则回退到 PATH 里的全局 `opm`
>
> 如需显式指定目录：
>
> ```bash
> OPENMEMORY_DIR=/path/to/openmemory/packages/openmemory-js make openmemory
> ```

> 如需首次启动（临时切到 migrator + `OM_PG_AUTO_DDL=true`）：
>
> ```bash
> OPENMEMORY_FIRST_RUN=1 make openmemory
> ```
>
> 如需显式指定自定义 `opm` 包装器或非 PATH 安装位置：
>
> ```bash
> OPM=/path/to/opm make openmemory
> ```

Windows PowerShell：

```powershell
.\scripts\windows\start_openmemory.ps1
```

**使用同级 `openmemory` 源码目录**

推荐目录布局如下：

- `../openmemory`：放希望优先启动的 OpenMemory checkout，既可以是官方仓库，也可以是任意分支或 fork
- 其他 OpenMemory checkout：如需同时保留，可放到别处，并在需要时通过 `OPENMEMORY_DIR` 显式指定

也就是说，本次约定不再对某个特定 fork 或目录命名做强绑定。启动顺序统一为：`OPENMEMORY_DIR` -> 同级 `../openmemory` -> 常见本地目录 -> 全局 `opm`。其中同级 `../openmemory` 只是推荐优先约定路径。

如果你希望显式指定路径、绕过 Makefile 自动探测，或单独启动同级源码版 OpenMemory，也可以直接使用本仓库附带的启动脚本：

macOS / Linux / WSL2(Debian)：

```bash
# 默认按上述顺序自动探测
scripts/ops/start_openmemory.sh

# 首次建表 / 补索引时，可临时切换到 migrator
scripts/ops/start_openmemory.sh --first-run
```

Windows PowerShell：

```powershell
.\scripts\windows\start_openmemory.ps1

# 首次建表 / 补索引时，可临时切换到 migrator
.\scripts\windows\start_openmemory.ps1 -FirstRun
```

这两个脚本会：

- 自动加载 `.env` / `.env.local` / `.env.ps1`
- 启动顺序与 `make openmemory` 保持一致：指定目录 -> 同级 `../openmemory` -> 常见本地目录 -> 全局 `opm`
- 本地源码目录可运行时，直接执行 `node bin/opm.js serve`
- 若未找到本地源码目录，或源码目录缺少 `bin/opm.js` / 编译产物，则回退到全局 `opm`（如已安装）

**可选：如你不希望回退到全局 OpenMemory，可先移除全局安装**

如果你以前通过 `npm install -g` 或 `npm link` 安装过 OpenMemory，而你又希望始终命中本地 `../openmemory` 源码目录，可以先清掉全局命令，避免后续在本地源码不可运行时自动回退到旧版本：

```bash
npm uninstall -g openmemory-js
npm unlink -g openmemory-js   # 若之前通过 npm link 安装
```

Windows PowerShell：

```powershell
npm uninstall -g openmemory-js
npm unlink -g openmemory-js
```

如需确认当前 `opm` 指向哪里：

```bash
which opm
```

```powershell
Get-Command opm
```

终端 B：启动 Gateway

macOS / WSL2：

```bash
make gateway
```

Windows PowerShell：

```powershell
.\scripts\windows\start_gateway.ps1
```

**启动后验证**

macOS / WSL2：

```bash
curl -fsS http://127.0.0.1:8080/health && echo "OpenMemory OK"
curl -fsS http://127.0.0.1:8787/health && echo "Gateway OK"
make mcp-doctor
# 可选：make stack-doctor
```

Windows PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health | Out-Null
Invoke-RestMethod http://127.0.0.1:8787/health | Out-Null
python scripts/ops/mcp_doctor.py
# 可选：python scripts/ops/stack_doctor.py
```

**首次启动 OpenMemory 遇到权限问题**

如果 `opm serve` 报 `permission denied for schema openmemory`，优先用仓库里现成的兜底方式：

macOS / WSL2：

```bash
cd /path/to/engram
source scripts/ops/load_env_local.sh
eval "$(make --no-print-directory env-openmemory-first-run)"
```

然后重新执行：

```bash
# 默认你已按上文将 ~/openmemory 固定到 v1.3.3
cd ~/openmemory/packages/openmemory-js
opm serve
```

Windows PowerShell：

```powershell
# 默认你已按上文将 $env:USERPROFILE\openmemory 固定到 v1.3.3
cd C:\path\to\engram
if (Test-Path ".\.env.ps1") { . .\.env.ps1 }
$env:OM_PG_USER = "openmemory_migrator_login"
if (-not [string]::IsNullOrWhiteSpace($env:OPENMEMORY_MIGRATOR_PASSWORD)) {
  $env:OM_PG_PASSWORD = $env:OPENMEMORY_MIGRATOR_PASSWORD
}
$env:OM_PG_AUTO_DDL = "true"
cd $env:USERPROFILE\openmemory\packages\openmemory-js
opm serve
```

如果你希望后续一直使用 `openmemory_svc` 运行，可补一次授权：

macOS / WSL2：

```bash
cd /path/to/engram
source .venv/bin/activate
make openmemory-grant-svc-full
```

Windows PowerShell：

```powershell
.\scripts\windows\openmemory_grant_svc_full.ps1
```

##### 3.2 初始化环境变量（通用）

推荐优先使用仓库内脚本加载 `.env` / `.env.local`：

```bash
# Linux / macOS / WSL
source scripts/ops/load_env_local.sh
```

```powershell
# Windows PowerShell
.\scripts\windows\load_env_local.ps1
```

若你不使用脚本，最少需要以下变量：

```bash
export POSTGRES_DSN="postgresql://logbook_svc:<pwd>@localhost:5432/engram"
export OPENMEMORY_BASE_URL="http://localhost:8080"
export PROJECT_KEY="default"
```

```powershell
$env:POSTGRES_DSN="postgresql://logbook_svc:<pwd>@localhost:5432/engram"
$env:OPENMEMORY_BASE_URL="http://localhost:8080"
$env:PROJECT_KEY="default"
```

##### 3.3 Windows 原生补充

如果你不想使用 `start_openmemory.ps1` / `start_gateway.ps1`，可按你实际命中的启动方式手动执行等价命令。

Windows PowerShell（两个终端）：

```powershell
# 终端 A：OpenMemory（同级 openmemory 源码目录可运行时）
.\scripts\windows\load_env_local.ps1
cd ..\openmemory\packages\openmemory-js
node bin\opm.js serve
```

```powershell
# 终端 A：OpenMemory（若本地源码未命中，则回退到全局 opm）
.\scripts\windows\load_env_local.ps1
opm serve
```

```powershell
# 终端 B：Gateway
.\scripts\windows\load_env_local.ps1
uvicorn engram.gateway.main:app --host 0.0.0.0 --port 8787 --reload

# 可选验证
python scripts/ops/mcp_doctor.py
python scripts/ops/stack_doctor.py
# python scripts/ops/stack_doctor.py --full
# 观测性（Prometheus 指标）
Invoke-WebRequest http://127.0.0.1:8787/metrics | Select-Object -ExpandProperty Content
```

##### 3.4 Docker Compose 统一栈启动

```bash
docker compose -f docker-compose.unified.yml up -d --build

# 可选：查看状态
docker compose -f docker-compose.unified.yml ps

# 可选：停止
docker compose -f docker-compose.unified.yml down -v
```

##### 3.5 各平台便携脚本与服务注册

| 平台 | 场景 | 脚本 / 方式 |
|------|------|-------------|
| Linux/macOS/WSL | 加载环境变量 | `source scripts/ops/load_env_local.sh` |
| Linux/macOS/WSL | 启动 OpenMemory（顺序：指定目录 > 同级源码 > 常见本地目录 > 全局 opm） | `scripts/ops/start_openmemory.sh` |
| Windows PowerShell | 加载环境变量 | `.\scripts\windows\load_env_local.ps1` |
| Windows PowerShell | 一键初始化数据库 | `.\scripts\windows\setup_db.ps1` |
| Windows PowerShell | 启动 OpenMemory（顺序：指定目录 > 同级源码 > 常见本地目录 > 全局 opm） | `.\scripts\windows\start_openmemory.ps1` |
| Windows PowerShell | 启动 Gateway | `.\scripts\windows\start_gateway.ps1` |
| Windows PowerShell | 无 make 运行 CI 门禁 | `.\scripts\windows\ci.ps1` |
| Windows PowerShell | 全栈诊断 | `.\scripts\windows\stack_doctor.ps1` |
| Windows PowerShell | 注册 Windows 服务（NSSM） | `.\scripts\windows\install_services.ps1` |

**Windows 服务注册（NSSM）**

1. 以管理员 PowerShell 运行  
2. 准备 `nssm.exe`（放到你本机约定的工具目录即可）  
3. 复制并编辑本地配置：
   - 以 [`scripts/windows/config.ps1.example`](scripts/windows/config.ps1.example) 为模板准备本地 `config.ps1`
4. 执行：

```powershell
.\scripts\windows\install_services.ps1
```

更详细参数说明见：
- `scripts/windows/00_prereqs.md`
- `docs/gateway/01_openmemory_deploy_windows.md`

**WSL2 / Linux systemd 托管（可选）**

建议按文档示例创建 `/etc/engram/*.env` 和 `openmemory.service / engram-gateway.service / engram-outbox.service` 后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openmemory engram-gateway engram-outbox
```

完整示例见 `docs/gateway/01_openmemory_deploy_windows.md`（B.7）。

**macOS launchd 托管（可选）**

README 下方已提供 `LaunchAgents` 示例，或直接参考 `docs/installation.md` 的 launchd 章节。

服务默认监听 `http://0.0.0.0:8787`（MCP: `/mcp`）。  
Cursor MCP 只连接 Gateway，不直连 OpenMemory；因此 OpenMemory 只需对 Gateway 可达。

> 常见提示：
> - `opm serve` 报 `permission denied for schema openmemory`：可先用 `eval "$(make --no-print-directory env-openmemory-first-run)"` 临时切到 migrator 登录，或执行 `make openmemory-grant-svc-full` 后重启
> - `opm serve` 警告 `OM_TIER not set`：在 `.env.local` 设置 `OM_TIER="hybrid"`（或 fast/smart/deep）
> - WSL2 下若 Windows 访问 `localhost:8080` 不通，可用 `hostname -I` 获取 WSL IP 并改用 `http://<wsl-ip>:8080`

### 二、客户端配置

在 Cursor IDE 的 MCP 配置中添加（参考 `configs/mcp/.mcp.json.example`）：

**MCP 配置 SSOT**：`configs/mcp/.mcp.json.example`；README 片段由 `scripts/docs/render_mcp_config_snippet.py` 生成，更新用 `make update-mcp-config-docs`，校验用 `make check-mcp-config-docs-sync`。

<!-- BEGIN GENERATED: mcp_config_snippet -->
<!-- AUTO-GENERATED BY render_mcp_config_snippet.py; DO NOT EDIT -->

```json
{
  "mcpServers": {
    "engram": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```
<!-- END GENERATED -->

> 说明：
> - 如果 Cursor 不在运行 Gateway 的同一台机器上，请把 `url` 里的 `127.0.0.1` 替换成 **Gateway 所在机器的 IP/域名**（例如 `http://192.168.1.100:8787/mcp`）
> - 若 Gateway 跑在 Windows 的 WSL2 中并希望局域网其它机器可访问，请参考 `docs/gateway/01_openmemory_deploy_windows.md` 的 “B.9 Windows / 局域网访问说明”

如果你通过 Codex CLI 连接同一个 Gateway，可在 `~/.codex/config.toml` 中添加：

```toml
[mcp_servers.engram]
url = "http://127.0.0.1:8787/mcp"
startup_timeout_sec = 45
tool_timeout_sec = 120
required = false
enabled = true
```

> Codex CLI 多代理排障建议：
> - 若长期停留在 `Booting MCP Server: engram`，先提高 `startup_timeout_sec` 到 `30-60` 秒
> - 排障阶段建议 `required = false`，确认稳定后再评估是否收紧为 `true`
> - Cursor / Codex 对 `GET /mcp` 或 `/.well-known/oauth-protected-resource*` 的启动探测属于已知行为；Engram 已对这些访问日志做降噪处理，`initialize` / `tools/list` 仍是判断可用性的核心信号

配置完成后，AI Agent 即可使用记忆管理功能。

---

## 多项目 / 多用户

### 项目隔离

通过 `PROJECT_KEY` 区分不同项目，每个项目使用独立数据库：

```bash
# 部署项目 A
PROJECT_KEY=proj_a POSTGRES_DB=proj_a make gateway

# 部署项目 B（另一个实例）
PROJECT_KEY=proj_b POSTGRES_DB=proj_b GATEWAY_PORT=8788 make gateway
```

**Windows PowerShell**：先设置环境变量再启动，例如 `$env:PROJECT_KEY="proj_a"; $env:POSTGRES_DB="proj_a"; uvicorn engram.gateway.main:app --host 0.0.0.0 --port 8787 --reload`。

新增一个项目（推荐流程：每项目一库 + 独立实例）：

```bash
# 1) 初始化该项目的数据库/角色/权限（建议让 PROJECT_KEY 与 POSTGRES_DB 保持一致）
PROJECT_KEY=proj_c POSTGRES_DB=proj_c make setup-db

# 2) 为该项目写一份独立 env 文件（避免覆盖当前 .env.local）
ENV_LOCAL_FILE=.env.local.proj_c PROJECT_KEY=proj_c POSTGRES_DB=proj_c make env-write-local

# 3) 启动该项目的 OpenMemory（建议使用不同端口）
set -a; . ./.env.local.proj_c; set +a
OM_PORT=8081 opm serve

# 4) 启动该项目的 Gateway（指向对应的 OpenMemory）
set -a; . ./.env.local.proj_c; set +a
GATEWAY_PORT=8788 OPENMEMORY_BASE_URL=http://localhost:8081 make gateway
```

**Windows PowerShell**：步骤 2 的 `make env-write-local` 可改为 `python scripts/ops/write_env_local.py`（需先设置 `ENV_LOCAL_FILE`、`PROJECT_KEY` 等）。步骤 3/4 中加载 env 改为在 PowerShell 中逐行设置变量后执行 `opm serve` / `uvicorn engram.gateway.main:app --host 0.0.0.0 --port 8788 --reload`。

### 用户隔离（Space 机制）

| 空间类型 | 格式 | 说明 |
|----------|------|------|
| 团队空间 | `team:<project_key>` | 项目成员共享，默认写入目标 |
| 私有空间 | `private:<user_id>` | 用户个人数据 |

MCP 调用时通过 `actor_user_id` 参数标识用户身份。

> 注意：团队空间写入受治理开关控制（`team_write_enabled`），默认可能被降级到私有空间；需通过 `governance_update` 开启并满足证据链策略。身份映射与账号关联参见 [docs/logbook/08_identity_config.md](docs/logbook/08_identity_config.md)。

> 详见 [记忆契约](docs/gateway/03_memory_contract.md) 和 [治理开关](docs/gateway/04_governance_switch.md)

---

## Makefile 快速命令

```bash
make help          # 查看所有命令

# 安装
make install-full  # 安装完整依赖
make install-dev   # 安装开发依赖

# 数据库（一键初始化）
make setup-db      # 一键初始化数据库（自动识别/交互；创建库 + DDL + 角色 + 权限 + 验证）

# 数据库（分步操作）
make db-create              # 创建数据库并启用 pgvector
make bootstrap-roles        # 初始化服务账号
make migrate-ddl            # 仅执行 DDL 迁移（Schema/表/索引）
make apply-roles            # 应用 Logbook 角色和权限
make apply-openmemory-grants # 应用 OpenMemory 权限
make verify-permissions     # 验证数据库权限配置

# 服务
make openmemory    # 启动 OpenMemory（自动加载 .env/.env.local）
make gateway       # 启动 Gateway（带热重载）

# 测试
make test          # 运行所有测试
make test-quick    # 快速冒烟测试

# 代码质量
make lint          # 代码检查
make format        # 代码格式化
make ci            # 运行全部 CI 门禁
make check-iteration-docs  # 迭代文档规范检查
```

**Windows 无 make 时常用等价命令**（需先激活 venv 并设置 `POSTGRES_DSN`、`OPENMEMORY_BASE_URL` 等环境变量）：

| make 命令 | Windows PowerShell / 命令行 |
|-----------|-----------------------------|
| `make install-full` | `pip install -e ".[full]"` |
| `make install-dev` | `pip install -e ".[full,dev]"` |
| `make setup-db` | 建议使用 WSL 或参考 [安装指南](docs/installation.md) 分步执行 |
| `make openmemory` | `.\scripts\windows\start_openmemory.ps1` |
| `make gateway` | `.\scripts\windows\start_gateway.ps1` |
| `make ci` | `.\scripts\windows\ci.ps1`（或 `python scripts/ops/ci_no_make.py`） |
| `make mcp-doctor` | `python scripts/ops/mcp_doctor.py` |
| `make stack-doctor` | `python scripts/ops/stack_doctor.py` |
| `make test` | `pytest` |
| `make test-quick` | `pytest tests/acceptance/test_installation.py -v` |

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│  Cursor IDE / MCP Client                                │
└────────────────────────┬────────────────────────────────┘
                         │ MCP JSON-RPC
┌────────────────────────▼────────────────────────────────┐
│  Gateway (engram.gateway)                               │
│  - 策略校验                                             │
│  - 写入审计                                             │
│  - 失败降级                                             │
└────────────────┬──────────────────┬─────────────────────┘
                 │                  │
    ┌────────────▼──────┐   ┌──────▼────────────┐
    │  Logbook          │   │  OpenMemory       │
    │  (PostgreSQL)     │   │  (语义记忆服务)   │
    │  - 事实账本       │   │  - 向量检索       │
    │  - 治理设置       │   │  - 记忆存储       │
    │  - Outbox 队列    │   │                   │
    └───────────────────┘   └───────────────────┘
```

---

## 项目结构

```
engram/
├── src/engram/              # 源代码
│   ├── gateway/             # Gateway 模块（MCP 网关）
│   └── logbook/             # Logbook 模块（事实账本）
├── sql/                     # 数据库迁移脚本
├── configs/mcp/             # MCP 配置示例
├── docs/                    # 文档
├── tests/                   # 测试
├── Makefile                 # 快速命令
└── pyproject.toml           # 项目配置
```

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_DSN` | PostgreSQL 连接字符串 | - |
| `PROJECT_KEY` | 项目标识（多项目隔离） | `default` |
| `OPENMEMORY_BASE_URL` | OpenMemory 服务地址 | - |
| `GATEWAY_PORT` | Gateway 端口 | `8787` |
| `ENGRAM_PG_USE_POOL` | 启用 `psycopg_pool` 连接池（`1/0`） | `0` |
| `ENGRAM_PG_POOL_MIN_SIZE` | 连接池最小连接数 | `1` |
| `ENGRAM_PG_POOL_MAX_SIZE` | 连接池最大连接数 | `10` |
| `ENGRAM_PG_POOL_TIMEOUT_SEC` | 连接池借还超时（秒） | `10` |
| `GATEWAY_METRICS_ENABLED` | 是否启用 `/metrics` 指标输出（`1/0`） | `1` |
| `GATEWAY_OTEL_ENABLED` | 是否启用基础 tracing（`1/0`） | `0` |
| `GATEWAY_OTEL_EXPORTER` | tracing 导出器（`console/none`） | `console` |
| `GATEWAY_OTEL_SERVICE_NAME` | OTel service.name | `engram-gateway` |

> 完整变量列表见 [环境变量参考](docs/reference/environment_variables.md)

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [安装指南](docs/installation.md) | 详细安装步骤（多平台、PostgreSQL、pgvector） |
| [Gateway 文档](docs/gateway/) | MCP 集成、治理开关、降级策略 |
| [Logbook 文档](docs/logbook/) | 架构设计、工具契约、部署运维 |
| [环境变量参考](docs/reference/environment_variables.md) | 所有环境变量说明 |
| [架构文档](docs/architecture/) | 架构决策记录（ADR）、命名规范 |
| [迭代文档 SSOT（验收矩阵）](docs/acceptance/00_acceptance_matrix.md) | 迭代计划/回归/证据索引（SSOT） |
| [迭代本地草稿工作流](docs/dev/iteration_local_drafts.md) | `.iteration/` 草稿与晋升规则 |

---

## 其他使用方式

### 作为 Python 库使用

如需在自己的项目中编程式调用 Logbook：

```bash
pip install engram
```

```python
from engram.logbook import Database, Config

config = Config.from_env()
db = Database(config.postgres_dsn)

# 创建条目
item_id = db.create_item(
    item_type="task",
    title="My Task",
    project_key="my_project"
)

# 添加事件
db.add_event(item_id, event_type="progress", payload={"status": "done"})
```

### CLI 命令

```bash
engram-logbook health              # 健康检查
engram-logbook create_item ...     # 创建条目
engram-artifacts --help            # Artifact CLI
engram-migrate --help              # 数据库迁移 CLI
engram-bootstrap-roles --help      # 初始化服务账号
engram-gateway                     # 启动 Gateway
engram-iteration rerun-advice --help  # 迭代工具入口
```

### SCM 同步工具

SCM 同步子系统提供以下 CLI 工具：

```bash
# 调度器 - 扫描仓库并入队同步任务
engram-scm-scheduler --once              # 执行一次调度
engram-scm-scheduler --once --dry-run    # 干运行（不实际入队）
engram-scm-scheduler --loop              # 循环模式运行

# Worker - 从队列处理同步任务
engram-scm-worker --worker-id worker-1          # 启动 worker
engram-scm-worker --worker-id worker-1 --once   # 处理单个任务

# Reaper - 回收过期任务和锁
engram-scm-reaper --once                 # 执行一次清理
engram-scm-reaper --loop                 # 循环模式运行

# 状态查看 - 查看同步健康状态
engram-scm-status --json                 # JSON 格式输出
engram-scm-status --prometheus           # Prometheus 指标格式

# 运行器 - 手动执行同步（通过 engram-scm-sync）
engram-scm-sync runner incremental --repo gitlab:123
engram-scm-sync runner backfill --repo gitlab:123 --last-hours 24

# 管理操作（通过 engram-scm-sync admin）
engram-scm-sync admin jobs list --status dead    # 查看 dead 任务
engram-scm-sync admin jobs reset-dead            # 重置 dead 任务
engram-scm-sync admin locks list-expired         # 查看过期锁
```

> **弃用说明**: 根目录的 `python scm_sync_*.py` 脚本已移除，请使用 `engram-scm-*` 命令。

> 详细配置参见 [SCM Sync 运维指南](docs/logbook/07_scm_sync_ops_guide.md) 和 [环境变量参考](docs/reference/environment_variables.md#scm-同步服务)
> 
> 仅通过 MCP 的客户端可使用 `scm_patch_blob_resolve` / `scm_materialize_patch_blob` 获取 patch 证据，编排示例见 [docs/gateway/08_workflow_orchestration_template.md](docs/gateway/08_workflow_orchestration_template.md)。

---

## 许可证

MIT License
