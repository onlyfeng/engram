# Windows 部署（原生 / WSL2）

目标：在 Windows 环境下完成 **Engram + OpenMemory + Postgres(+pgvector)** 的可用部署，给 Cursor/Agent 提供 MCP 接入。

---

## 方案 A：Windows 原生全栈

适合：希望全部在 Windows 原生环境运行，且愿意处理 pgvector 编译安装。

### A.1 前置依赖
- PostgreSQL 18+（建议 18）
- Visual Studio C++ Build Tools（用于编译 pgvector）
- Python 3.10+（建议 3.11）
- Node.js（需 >=18，建议最新 LTS）
- NSSM（把 Engram/OpenMemory 注册为服务）

### A.2 安装 Postgres + pgvector
1) 安装 PostgreSQL（官方安装包）并确保 `psql.exe` 在 PATH。  
2) 以管理员打开 **x64 Native Tools Command Prompt**，执行：  
```
set "PGROOT=C:\Program Files\PostgreSQL\16"
cd %TEMP%
git clone --branch v0.8.1 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```
3) 启用扩展并验证：  
```
psql -d <db> -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -d <db> -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

### A.3 初始化数据库与角色
- 使用 `scripts/windows/install_db.ps1` 执行迁移与角色脚本  
- 或手动执行：  
```
python logbook_postgres\scripts\db_migrate.py --dsn "<admin_dsn>" --apply-roles --apply-openmemory-grants
```

### A.4 部署 OpenMemory
- 拉取 OpenMemory 后端（参考官方仓库），设置 `OM_METADATA_BACKEND=postgres` 和 `OM_PG_*` 变量  
- 启动后端服务（HTTP + MCP + Dashboard），默认 8080

### A.5 部署 Engram Gateway
```
pip install -e ".[full]"
set POSTGRES_DSN=postgresql://logbook_svc:<pwd>@localhost:5432/<db>
set OPENMEMORY_BASE_URL=http://localhost:8080
set OM_API_KEY=<your_om_key>
engram-gateway
```

### A.6 运行方式（前台/后台）

**OpenMemory（示例）**：
```
cd C:\openmemory\backend
set OM_METADATA_BACKEND=postgres
set OM_PG_HOST=127.0.0.1
set OM_PG_PORT=5432
set OM_PG_DB=engram
set OM_PG_USER=openmemory_svc
set OM_PG_PASSWORD=<pwd>
set OM_PG_SCHEMA=openmemory
set OM_API_KEY=<your_om_key>
set OM_PORT=8080
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
如需 Windows 访问，可将 `listen_addresses` 设为 `0.0.0.0` 并在 `pg_hba.conf` 添加允许规则。

### B.4 初始化数据库与角色
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full]"

# 必须设置服务账号密码
export LOGBOOK_MIGRATOR_PASSWORD=changeme1
export LOGBOOK_SVC_PASSWORD=changeme2
export OPENMEMORY_MIGRATOR_PASSWORD=changeme3
export OPENMEMORY_SVC_PASSWORD=changeme4
export OM_PG_SCHEMA="openmemory"

sudo -u postgres createdb engram
sudo -u postgres psql -d engram -c "CREATE EXTENSION IF NOT EXISTS vector;"

python logbook_postgres/scripts/db_bootstrap.py \
  --dsn "postgresql://postgres:postgres@localhost:5432/postgres"

python logbook_postgres/scripts/db_migrate.py \
  --dsn "postgresql://postgres:postgres@localhost:5432/engram" \
  --apply-roles --apply-openmemory-grants

# 补充 OpenMemory 运行时权限
sudo -u postgres psql -d engram -c "
GRANT ALL PRIVILEGES ON SCHEMA openmemory TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON TABLES TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON SEQUENCES TO openmemory_svc;
"
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

### B.9 Windows 访问说明
- Windows 侧可优先使用 `http://localhost:8787` / `http://localhost:8080`  
- 若 `localhost` 不通，使用 WSL2 IP（`hostname -I`）  
- 注意 Windows 防火墙放行端口

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

执行补充授权（Windows 原生用 `psql`，WSL2 用 `sudo -u postgres psql`）：
```sql
GRANT ALL PRIVILEGES ON SCHEMA openmemory TO openmemory_svc;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA openmemory TO openmemory_svc;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA openmemory TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON TABLES TO openmemory_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA openmemory GRANT ALL ON SEQUENCES TO openmemory_svc;
```
</details>

<details>
<summary><b>OpenMemory 报错 "column does not have dimensions"（PostgreSQL 18）</b></summary>

pgvector HNSW 索引要求 vector 列必须指定维度：
```bash
psql -d engram -c "DROP INDEX IF EXISTS openmemory.openmemory_vectors_v_idx;"
psql -d engram -c "ALTER TABLE openmemory.openmemory_vectors ALTER COLUMN v TYPE vector(1536);"
```
然后重启 OpenMemory 服务。
</details>

<details>
<summary><b>OpenMemory 警告 "OM_TIER not set"</b></summary>

设置环境变量：
```bash
export OM_TIER=hybrid  # 可选: hybrid/fast/smart/deep
```
</details>

---

## 安全建议（即便内网）
- 启用 API Key（OpenMemory 与 Gateway）
- 限制端口访问网段（Windows 防火墙 / WSL2 iptables）
- 日志保留至少 7 天，用于审计与故障定位

备注：OpenMemory 为 Apache-2.0 协议，适合企业内部二次集成。
