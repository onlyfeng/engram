# Windows 部署（原生 / WSL2）

目标：在 Windows 环境下完成 **Engram + OpenMemory + Postgres(+pgvector)** 的可用部署，给 Cursor/Agent 提供 MCP 接入。

---

## 方案 A：Windows 原生全栈

适合：希望全部在 Windows 原生环境运行，且愿意处理 pgvector 编译安装。

> 说明：本方案默认你主要在 **PowerShell** 操作（除 pgvector 编译需在 “x64 Native Tools Command Prompt”）。  
> 下文如未特别说明，命令均在 **engram 仓库根目录** 执行。

### A.1 前置依赖
- PostgreSQL 18+（建议 18）
- Visual Studio C++ Build Tools（用于编译 pgvector）
- Python 3.10+（建议 3.11）
- Node.js（需 >=18，建议最新 LTS）
- NSSM（把 Engram/OpenMemory 注册为服务）
- Git（用于 clone 仓库）

### A.1.1 获取 Engram 并安装依赖（PowerShell）

```powershell
git clone https://github.com/onlyfeng/engram.git
cd engram

python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装完整依赖（Gateway + SCM 等）
pip install -e ".[full]"
```

### A.2 安装 Postgres + pgvector
1) 安装 PostgreSQL（官方安装包）并确保 `psql.exe` 在 PATH。建议在 Windows 设置 `PGUSER=postgres`（默认连接用户），后续示例可不写 `-U postgres`。  
   - 临时（仅当前终端）：PowerShell 用 `$env:PGUSER="postgres"`；cmd 用 `set "PGUSER=postgres"`  
   - 永久（推荐）：系统设置 → 搜索“环境变量” → 环境变量 → 用户变量中新建 `PGUSER`=`postgres`（保存后重开终端生效）  
2) 以管理员打开 **x64 Native Tools Command Prompt**，执行：  
```
set "PGROOT=C:\Program Files\PostgreSQL\18"
cd %TEMP%
git clone --branch v0.8.1 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```
3) 启用扩展并验证：  
> 说明：示例默认使用安装包自带超级用户 `postgres`（建议已设置 `PGUSER=postgres`；如未设置，请在命令中加回 `-U postgres`）。  
> 如提示找不到 `psql`：将 PostgreSQL 安装目录的 `bin` 加入 PATH（例如 `%PGROOT%\bin`），或用 `"%PGROOT%\bin\psql.exe"` 替代 `psql`。  
```
psql -d postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -d postgres -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
```

### A.3 初始化数据库与角色
1) 创建数据库并启用 pgvector 扩展（示例以 `engram` 为库名）：

```powershell
# 建议先设置 PGUSER=postgres（见 A.2），否则请在命令中加回 -U postgres
createdb engram
psql -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

2) 初始化服务账号 + 迁移 + 权限（推荐使用 CLI；需要能以管理员身份连接 Postgres）：

```powershell
# unified-stack 模式：必填（4 个密码缺一不可）
# Windows 建议用环境变量提供（避免把密码写进脚本并提交）：
# - 临时（仅当前终端）：直接设置 $env:...（如下）
# - 永久（用户环境变量）：设置一次后，后续可省略这 4 行（需重开终端生效）
#   setx LOGBOOK_MIGRATOR_PASSWORD "changeme1"
#   setx LOGBOOK_SVC_PASSWORD "changeme2"
#   setx OPENMEMORY_MIGRATOR_PASSWORD "changeme3"
#   setx OPENMEMORY_SVC_PASSWORD "changeme4"
$env:LOGBOOK_MIGRATOR_PASSWORD="changeme1"
$env:LOGBOOK_SVC_PASSWORD="changeme2"
$env:OPENMEMORY_MIGRATOR_PASSWORD="changeme3"
$env:OPENMEMORY_SVC_PASSWORD="changeme4"

engram-bootstrap-roles --dsn "postgresql://<admin_user>@localhost:5432/postgres"
engram-migrate --dsn "postgresql://<admin_user>@localhost:5432/engram" --apply-roles --apply-openmemory-grants
engram-migrate --dsn "postgresql://<admin_user>@localhost:5432/engram" --verify
```

> 可选（venv 场景，不污染系统环境变量）：把上面 4 行 `$env:...` 保存到本地脚本（例如 `.\.env.ps1`，**不要提交到 git**），每次激活 venv 后执行：`. .\.env.ps1`。也可以直接运行 `scripts/windows/setup_db.ps1` 交互式生成/更新 `.\.env.ps1`。

也可以使用 `scripts/windows/setup_db.ps1`（类似 `make setup-db`：可交互输入密码、生成/更新 `.\.env.ps1`，并调用 `scripts/windows/install_db.ps1`；读取 `scripts/windows/config.ps1`）一键执行上述步骤。

> 注意：`scripts/windows/config.ps1` 是本地配置文件（从 `scripts/windows/config.ps1.example` 复制生成并按需修改），仓库已在 `.gitignore` 中忽略，请勿提交。

### A.4 部署 OpenMemory
- 拉取 OpenMemory 后端（参考官方仓库），设置 `OM_METADATA_BACKEND=postgres` 和 `OM_PG_*` 变量  
- 启动后端服务（HTTP + MCP + Dashboard），默认 8080

### A.5 部署 Engram Gateway
```powershell
# 如尚未安装依赖（未执行 A.1.1），先执行：
# pip install -e ".[full]"

# 如已生成/加载 `.\.env.ps1`，可省略下方 $env:... 赋值
$env:POSTGRES_DSN="postgresql://logbook_svc:<pwd>@localhost:5432/<db>"
$env:OPENMEMORY_BASE_URL="http://localhost:8080"
$env:OM_API_KEY="<your_om_key>"
$env:PROJECT_KEY="default"

# 开发模式（热重载）
python -m uvicorn engram.gateway.main:app --host 0.0.0.0 --port 8787 --reload

# 或生产入口（无热重载）
# engram-gateway
```

### A.6 运行方式（前台/后台）

**OpenMemory（示例）**：
```powershell
# 如已生成/加载 `.\.env.ps1`，可省略下方 $env:OM_* 赋值（仅保留 cd/npm 命令即可）
cd C:\openmemory\backend
$env:OM_METADATA_BACKEND="postgres"
$env:OM_PG_HOST="127.0.0.1"
$env:OM_PG_PORT="5432"
$env:OM_PG_DB="engram"
$env:OM_PG_USER="openmemory_svc"
$env:OM_PG_PASSWORD="<pwd>"
$env:OM_PG_SCHEMA="openmemory"
$env:OM_API_KEY="<your_om_key>"
$env:OM_PORT="8080"
npm install
npm run start
```

**Outbox Worker**：
```
python -m engram.gateway.outbox_worker --loop
```

### A.7 服务托管（NSSM）
使用 `scripts/windows/install_services.ps1` 注册服务，日志落盘到 `logs` 目录。

也可手动使用 NSSM（示例，按实际路径调整）：
```
# Gateway
nssm install engram_gateway "C:\path\to\engram-gateway.exe"
nssm set engram_gateway AppDirectory "C:\engram"
nssm set engram_gateway AppStdout "C:\engram\logs\gateway.out.log"
nssm set engram_gateway AppStderr "C:\engram\logs\gateway.err.log"
nssm set engram_gateway AppEnvironmentExtra "PROJECT_KEY=default`nPOSTGRES_DSN=postgresql://logbook_svc:<pwd>@127.0.0.1:5432/engram`nOPENMEMORY_BASE_URL=http://127.0.0.1:8080`nOM_API_KEY=<your_om_key>"
nssm set engram_gateway Start SERVICE_AUTO_START
nssm start engram_gateway

# Outbox Worker
nssm install engram_outbox "C:\path\to\python.exe" "-m engram.gateway.outbox_worker --loop"
nssm set engram_outbox AppDirectory "C:\engram"
nssm set engram_outbox AppEnvironmentExtra "POSTGRES_DSN=postgresql://logbook_svc:<pwd>@127.0.0.1:5432/engram`nOPENMEMORY_BASE_URL=http://127.0.0.1:8080`nOM_API_KEY=<your_om_key>"
nssm set engram_outbox Start SERVICE_AUTO_START
nssm start engram_outbox

# OpenMemory（示例，按上游启动命令替换）
nssm install openmemory "C:\Program Files\nodejs\node.exe" "C:\openmemory\backend\server.js"
nssm set openmemory AppDirectory "C:\openmemory\backend"
nssm set openmemory AppEnvironmentExtra "OM_METADATA_BACKEND=postgres`nOM_PG_HOST=127.0.0.1`nOM_PG_PORT=5432`nOM_PG_DB=engram`nOM_PG_USER=openmemory_svc`nOM_PG_PASSWORD=<pwd>`nOM_PG_SCHEMA=openmemory`nOM_API_KEY=<your_om_key>`nOM_PORT=8080"
nssm set openmemory Start SERVICE_AUTO_START
nssm start openmemory
```

### A.8 Event Viewer 诊断

- 打开 `eventvwr.msc` → **Windows Logs** → **Application/System**
- 关注 Source：`Service Control Manager` / `NSSM`

PowerShell 快速筛选：
```
Get-WinEvent -LogName System -MaxEvents 50 |
  Where-Object { $_.ProviderName -eq "Service Control Manager" } |
  Select-Object TimeCreated, Id, LevelDisplayName, Message
```

### A.9 服务恢复策略

使用 `sc` 设置自动重启策略（示例，按服务名调整）：
```
sc failure engram_gateway reset= 86400 actions= restart/5000/restart/5000/restart/5000
sc failureflag engram_gateway 1

sc failure engram_outbox reset= 86400 actions= restart/5000/restart/5000/restart/5000
sc failureflag engram_outbox 1

sc failure openmemory reset= 86400 actions= restart/5000/restart/5000/restart/5000
sc failureflag openmemory 1
```

---

## 方案 B：WSL2 + Debian 全栈

适合：你当前的环境（WSL2 + Debian），希望所有组件 **原生运行在 WSL2**。

### B.1 启用 WSL2 与 systemd
1) 启用 WSL2 并安装 Debian  
2) 打开 systemd（Debian）：  
```
sudo tee /etc/wsl.conf <<'EOF'
[boot]
systemd=true
EOF
```
3) 退出 WSL，Windows 侧执行 `wsl --shutdown` 后重启

### B.2 安装 Postgres + pgvector
```
sudo apt update
sudo apt install -y postgresql-18 postgresql-18-pgvector
```
> 如需其他版本，请按 PGDG 官方仓库安装 `postgresql-<version>` 与 `postgresql-<version>-pgvector`。

### B.3 配置 Postgres
```
sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses='localhost';"
sudo -u postgres psql -c "ALTER SYSTEM SET port=5432;"
sudo systemctl restart postgresql
```
默认仅在 WSL2 内访问（更安全）。如需 **Windows 主机** 访问 Postgres（例如 Windows 上用 `psql`/GUI 客户端连库），可按下述方式放行（不建议对局域网/公网开放 `5432`）：

```bash
# 1) 让 Postgres 在 WSL2 内监听所有网卡
sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses='0.0.0.0';"
sudo systemctl restart postgresql

# 2) 获取 Windows 主机 IP（从 WSL2 视角，通常是 resolv.conf 的 nameserver）
WIN_IP=$(awk '/^nameserver / {print $2; exit}' /etc/resolv.conf)
echo "WIN_IP=$WIN_IP"

# 3) 找到 pg_hba.conf 路径（不同发行版/版本路径可能不同）
sudo -u postgres psql -Atc "SHOW hba_file;"
```

编辑 `pg_hba.conf`（在文件较靠前位置添加更具体的 allow 规则；把 `<WIN_IP>` 换成上一步的值）：

```conf
# 仅放行 Windows 主机访问（推荐：按库/用户收敛，不要写 0.0.0.0/0）
host    engram    logbook_svc     <WIN_IP>/32    scram-sha-256
host    engram    openmemory_svc  <WIN_IP>/32    scram-sha-256

# （可选）Windows 上用 postgres 管理时再放开
# host  all       postgres        <WIN_IP>/32    scram-sha-256
```

保存后重载：

```bash
sudo systemctl reload postgresql
```

> 备注：若客户端不支持 `scram-sha-256`，可将 `METHOD` 改为 `md5`；但不要使用 `trust`。

### B.4 初始化数据库与角色
```bash
# Python 环境建议（推荐选一种即可）：
#
# ✅ 推荐：系统 Python + venv（或 pyenv 安装 Python 后再 venv）
# python3 -m venv .venv
# source .venv/bin/activate
#
# ⚠️ 不推荐：conda（在 WSL2/Linux 下常见会遇到 sudo -u postgres 无法访问 conda 环境）
# conda create -n engram python=3.11 -y
# conda activate engram
#
# 说明：我们建议“尽量避免让 postgres 用户去执行你的 conda/python 环境”，因此优先用 make 串起来：
# - make 侧通过 DB_ADMIN_PREFIX 控制管理员操作（psql/createdb 走 postgres 用户）
# - Python CLI（engram-*）仍在当前用户的 venv 中运行
#
# 安装（确保当前 shell 已激活环境后再安装）：
pip install -e ".[full]"

# 解析当前环境的 python 路径（venv / conda）
if [ -n "${VIRTUAL_ENV:-}" ]; then
  PYTHON_BIN="$VIRTUAL_ENV/bin/python"
elif [ -n "${CONDA_PREFIX:-}" ]; then
  PYTHON_BIN="$CONDA_PREFIX/bin/python"
else
  PYTHON_BIN="python3"
fi

# 若你使用 conda 且安装在 $HOME 下，常见问题是：
#   sudo -u postgres 无法执行/读取 $CONDA_PREFIX（Permission denied）
# 原因：postgres 用户无法 traverse 你的家目录或 miniconda 目录（权限 700/750）。
#
# 推荐修复（更安全，使用 ACL 仅放行给 postgres 用户）：
#   sudo apt-get update && sudo apt-get install -y acl
#   sudo setfacl -m u:postgres:rx "$HOME" "$CONDA_PREFIX" \
#     "$(dirname "$CONDA_PREFIX")" "$(dirname "$(dirname "$CONDA_PREFIX")")"
#   sudo setfacl -R -m u:postgres:rX "$CONDA_PREFIX"
#   # 若你使用了 `pip install -e`（editable），postgres 还需要读取本仓库目录：
#   sudo setfacl -R -m u:postgres:rX "$(pwd)"
#
# 快速但更“粗”的修复（不推荐，可能过度放宽权限）：
#   chmod o+rx "$HOME" && chmod -R o+rX "$CONDA_PREFIX"

# 推荐：使用 make 一键初始化（原生）
DB_ADMIN_PREFIX="sudo -u postgres" make setup-db

# 如需重置（危险操作：删除数据库与服务账号）
DB_ADMIN_PREFIX="sudo -u postgres" FORCE=1 make reset-native

# 以下为手动分步方式（等价）
# 必须设置服务账号密码
export LOGBOOK_MIGRATOR_PASSWORD=changeme1
export LOGBOOK_SVC_PASSWORD=changeme2
export OPENMEMORY_MIGRATOR_PASSWORD=changeme3
export OPENMEMORY_SVC_PASSWORD=changeme4
export OM_PG_SCHEMA="openmemory"

sudo -u postgres createdb engram
sudo -u postgres psql -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 创建/更新服务账号（需要 superuser 或 CREATEROLE 权限）
# - 建议在 WSL2 本机 PostgreSQL 下使用 unix socket + peer auth：sudo -u postgres + postgresql:///...
# - 不要使用 postgresql://postgres:postgres@... 这种“默认密码=postgres”的假设（Debian/Ubuntu 默认并非如此）
# 注意：`sudo` 默认会重置 PATH，因此这里用绝对路径调用当前环境的 python（不依赖 PATH）。
sudo -u postgres "$PYTHON_BIN" -m engram.logbook.cli.db_bootstrap \
  --dsn "postgresql:///postgres" \
  --om-schema "$OM_PG_SCHEMA"

# 执行迁移 + 角色/权限脚本（04 + 05）
# 其中 05_openmemory_roles_and_grants.sql 会：
# - CREATE SCHEMA IF NOT EXISTS <OM_PG_SCHEMA>
# - 设置 schema owner=openmemory_migrator
# - 用 "ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator ..." 正确配置默认权限
sudo -u postgres "$PYTHON_BIN" -m engram.logbook.cli.db_migrate \
  --dsn "postgresql:///engram" \
  --apply-roles --apply-openmemory-grants

# 如需“只重跑 OpenMemory schema/权限脚本”，可以单独执行：
# sudo -u postgres psql -d engram -v ON_ERROR_STOP=1 \
#   -c "SET om.target_schema = '$OM_PG_SCHEMA';" \
#   -f sql/05_openmemory_roles_and_grants.sql
```

### B.5 部署 OpenMemory（WSL2 内）
```bash
# 安装 Node.js（>=18）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# 克隆并构建 OpenMemory
git clone https://github.com/caviraoss/openmemory.git ~/openmemory
cd ~/openmemory/packages/openmemory-js
npm install
npm run build
sudo npm link

# 配置环境变量
export OM_METADATA_BACKEND=postgres
export OM_PG_HOST=localhost
export OM_PG_PORT=5432
export OM_PG_DB=engram
export OM_PG_USER=openmemory_svc
export OM_PG_PASSWORD=$OPENMEMORY_SVC_PASSWORD
export OM_PG_SCHEMA=openmemory
export OM_API_KEY=change_me
export OM_PORT=8080
export OM_VEC_DIM=1536          # vector 维度，需与 pgvector 列定义一致
export OM_TIER=hybrid           # 可选: hybrid/fast/smart/deep

# 修复 pgvector 列维度（PostgreSQL 18 必需）
OM_VEC_DIM=1536 make openmemory-fix-vector-dim
# 或手动执行：
sudo -u postgres psql -d engram -c \
  "ALTER TABLE openmemory.openmemory_vectors ALTER COLUMN v TYPE vector(1536);" 2>/dev/null || true

# 启动服务
opm serve
```

### B.6 部署 Engram Gateway（WSL2 内）
```
export POSTGRES_DSN="postgresql://logbook_svc:<pwd>@localhost:5432/engram"
export OPENMEMORY_BASE_URL="http://localhost:8080"
export OM_API_KEY="<your_om_key>"
engram-gateway
```

### B.7 systemd 服务托管（WSL2 内）

**环境文件**：
```
sudo mkdir -p /etc/engram
sudo tee /etc/engram/engram.env <<'EOF'
PROJECT_KEY=default
POSTGRES_DSN=postgresql://logbook_svc:<pwd>@localhost:5432/engram
OPENMEMORY_BASE_URL=http://localhost:8080
OM_API_KEY=<your_om_key>
EOF

sudo tee /etc/engram/openmemory.env <<'EOF'
OM_METADATA_BACKEND=postgres
OM_PG_HOST=localhost
OM_PG_PORT=5432
OM_PG_DB=engram
OM_PG_USER=openmemory_svc
OM_PG_PASSWORD=<pwd>
OM_PG_SCHEMA=openmemory
OM_API_KEY=<your_om_key>
OM_PORT=8080
OM_VEC_DIM=1536
OM_TIER=hybrid
EOF
```

**systemd 单元（示例，按路径调整）**：
```
sudo tee /etc/systemd/system/openmemory.service <<'EOF'
[Unit]
Description=OpenMemory Backend
After=network.target postgresql.service

[Service]
Type=simple
WorkingDirectory=/home/$USER/openmemory/packages/openmemory-js
EnvironmentFile=/etc/engram/openmemory.env
ExecStart=/usr/bin/opm serve
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/engram-gateway.service <<'EOF'
[Unit]
Description=Engram Gateway
After=network.target postgresql.service openmemory.service
Requires=openmemory.service

[Service]
Type=simple
WorkingDirectory=/opt/engram
EnvironmentFile=/etc/engram/engram.env
ExecStart=/usr/bin/engram-gateway
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/engram-outbox.service <<'EOF'
[Unit]
Description=Engram Outbox Worker
After=network.target postgresql.service openmemory.service
Requires=openmemory.service

[Service]
Type=simple
WorkingDirectory=/opt/engram
EnvironmentFile=/etc/engram/engram.env
ExecStart=/usr/bin/python3 -m engram.gateway.outbox_worker --loop
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now openmemory engram-gateway engram-outbox
```

### B.8 常用管理命令

```
sudo systemctl status openmemory engram-gateway engram-outbox
sudo systemctl restart openmemory engram-gateway engram-outbox
sudo journalctl -u engram-gateway -n 200 --no-pager
```

### B.9 Windows / 局域网访问说明

#### B.9.1 Windows 本机访问（从 Windows 访问 WSL2 服务）
- Windows 侧可优先使用 `http://localhost:8787`（Gateway）/ `http://localhost:8080`（OpenMemory）
- 若 `localhost` 不通：在 WSL2 内执行 `hostname -I` 拿到 WSL2 IP，然后在 Windows 侧访问 `http://<wsl-ip>:8787`
- 注意 Windows 防火墙放行端口（至少 `8787`；如需访问 OpenMemory 再放行 `8080`）

#### B.9.2 局域网其它机器访问（让其它电脑能连到 Gateway/MCP）

WSL2 默认网络模式是 NAT，**WSL2 的 IP 通常只对 Windows 主机可达**。要让局域网其它机器访问 Gateway，需要把端口暴露到 **Windows 主机的局域网 IP**（例如 `192.168.x.x`）。

**客户端（其它机器）要改什么？**
- `.cursor/mcp.json`（或 `~/.cursor/mcp.json`）把 `url` 改为：`http://<windows-lan-ip>:8787/mcp`

**Windows 主机的局域网 IP 怎么看？**
- Windows 侧执行 `ipconfig`，找当前网卡（Wi-Fi/以太网）的 `IPv4 Address`

**方案 1（优先）：启用 WSL2 mirrored networking（Windows 11 新版 WSL）**

1) Windows 侧创建/编辑 `C:\Users\<你>\.wslconfig`：

```ini
[wsl2]
networkingMode=mirrored
firewall=true
```

2) Windows 侧执行 `wsl --shutdown`，再重新启动 Debian/WSL
3) 确认 Gateway 已启动并监听 `8787`（本项目默认绑定 `0.0.0.0:8787`）
4) 在其它机器验证：

```bash
curl -sf http://<windows-lan-ip>:8787/health && echo "Gateway OK"
```

**方案 2（兼容性最好）：Windows 端口转发（portproxy）**

以 **管理员 PowerShell** 执行（注意：WSL2 IP 每次重启可能变化，必要时需重跑）：

```powershell
# 1) 获取 WSL2 IP（每次 wsl --shutdown / 重启后可能变化）
$WslIp = (wsl -d Debian -e sh -lc "hostname -I | awk '{print $1}'").Trim()
Write-Host "WSL IP = $WslIp"

# 2) 将 Windows 侧 8787 转发到 WSL2 的 8787（Gateway）
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=8787 | Out-Null
netsh interface portproxy add    v4tov4 listenaddress=0.0.0.0 listenport=8787 connectaddress=$WslIp connectport=8787

# （可选）如需从局域网访问 OpenMemory Web/API，再转发 8080
# netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=8080 | Out-Null
# netsh interface portproxy add    v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=$WslIp connectport=8080

# 3) Windows 防火墙放行（建议仅 Private 网络 + LocalSubnet）
New-NetFirewallRule -DisplayName "Engram Gateway (8787) from LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8787 -Profile Private -RemoteAddress LocalSubnet | Out-Null
# （可选）OpenMemory
# New-NetFirewallRule -DisplayName "OpenMemory (8080) from LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -Profile Private -RemoteAddress LocalSubnet | Out-Null

# 4) 查看当前 portproxy 规则
netsh interface portproxy show v4tov4
```

其它机器验证：

```bash
curl -sf http://<windows-lan-ip>:8787/health && echo "Gateway OK"
```

> 注意：
> - 若执行过 `wsl --shutdown` / 重启后无法访问，请重新运行上述脚本更新 `connectaddress`。
> - 不建议暴露到公网；至少把 Windows 防火墙限制为 `Profile Private` + `LocalSubnet`，并按本文 “安全建议” 启用 API Key。

---

## 常见问题排查

<details>
<summary><b>db_bootstrap 报错 "服务账号创建失败"</b></summary>

确保设置了 4 个密码环境变量：
```bash
export LOGBOOK_MIGRATOR_PASSWORD=xxx
export LOGBOOK_SVC_PASSWORD=xxx
export OPENMEMORY_MIGRATOR_PASSWORD=xxx
export OPENMEMORY_SVC_PASSWORD=xxx
```
</details>

<details>
<summary><b>OpenMemory 报错 "permission denied for schema openmemory"</b></summary>

优先使用 Makefile 兜底授权：
```bash
make openmemory-grant-svc-full
# 或指定 schema
OM_PG_SCHEMA=custom_openmemory make openmemory-grant-svc-full
```

Windows PowerShell（无 make）：

```powershell
# 需要 psql 在 PATH；密码可用 $env:PGPASSWORD 或 pgpass.conf 提供
.\scripts\windows\openmemory_grant_svc_full.ps1
# .\scripts\windows\openmemory_grant_svc_full.ps1 -Schema "openmemory"
# .\scripts\windows\openmemory_grant_svc_full.ps1 -AdminDsn "postgresql://postgres@127.0.0.1:5432/engram"
```

执行后重启 OpenMemory 服务（例如重新运行 `opm serve`，或重启 systemd/nssm 服务）。

或执行补充授权（Windows 原生用 `psql`，WSL2 用 `sudo -u postgres psql`）：
```sql
GRANT ALL PRIVILEGES ON SCHEMA openmemory TO openmemory_svc;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA openmemory TO openmemory_svc;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA openmemory TO openmemory_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA openmemory GRANT ALL ON TABLES TO openmemory_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE openmemory_migrator IN SCHEMA openmemory GRANT ALL ON SEQUENCES TO openmemory_svc;
```
</details>

<details>
<summary><b>OpenMemory 报错 "column does not have dimensions"（PostgreSQL 18）</b></summary>

pgvector HNSW 索引要求 vector 列必须指定维度：
```bash
# 推荐：用 Makefile 修复（维度需与 embeddings 一致）
OM_VEC_DIM=1536 make openmemory-fix-vector-dim
```

Windows PowerShell（无 make）：

```powershell
# 需要 psql 在 PATH；密码可用 $env:PGPASSWORD 或 pgpass.conf 提供
.\scripts\windows\openmemory_fix_vector_dim.ps1 -VecDim 1536
# .\scripts\windows\openmemory_fix_vector_dim.ps1 -VecDim 1536 -Schema "openmemory"
# .\scripts\windows\openmemory_fix_vector_dim.ps1 -VecDim 1536 -AdminDsn "postgresql://postgres@127.0.0.1:5432/engram"
```

或手动修复：
```bash
# Windows 原生：建议已设置 PGUSER=postgres（见 A.2），否则请在命令中加回 -U postgres
psql -d engram -c "DROP INDEX IF EXISTS openmemory.openmemory_vectors_v_idx;"
psql -d engram -c "ALTER TABLE openmemory.openmemory_vectors ALTER COLUMN v TYPE vector(1536);"
# WSL2：更推荐用 sudo -u postgres psql（避免本地 peer 认证问题）
# sudo -u postgres psql -d engram -c "DROP INDEX IF EXISTS openmemory.openmemory_vectors_v_idx;"
# sudo -u postgres psql -d engram -c "ALTER TABLE openmemory.openmemory_vectors ALTER COLUMN v TYPE vector(1536);"
```
然后重启 OpenMemory 服务。
</details>

<details>
<summary><b>OpenMemory 警告 "OM_TIER not set"</b></summary>

设置环境变量：
```bash
export OM_TIER=hybrid  # 可选: hybrid/fast/smart/deep
```

如需持久化（systemd 场景），可写入 OpenMemory 的 env 文件（例如 `/etc/engram/openmemory.env`），再重启服务。
</details>

---

## 安全建议（即便内网）
- 启用 API Key（OpenMemory 与 Gateway）
- 限制端口访问网段（Windows 防火墙 / WSL2 iptables）
- 日志保留至少 7 天，用于审计与故障定位

备注：OpenMemory 为 Apache-2.0 协议，适合企业内部二次集成。
