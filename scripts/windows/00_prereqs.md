# Windows 部署前置依赖（原生 / WSL2）

## 方案 A：Windows 原生全栈（非 Docker）
### 必需
1) PostgreSQL 18+（建议 18）  
- PATH 中可用：`psql.exe`, `pg_dump.exe`  

2) Visual Studio C++ Build Tools  
- 用于编译安装 pgvector  

3) Python 3.10+（建议 3.11）  

4) Node.js（需 >=18，建议最新 LTS）  
- OpenMemory 后端依赖  

5) GNU Make（推荐）  
- 用于执行项目 `Makefile`（如 `make ci`、`make gateway`）  
- 推荐安装：

```powershell
scoop install make
make --version
```

6) NSSM（Non-Sucking Service Manager）  
- 用于把 Engram / OpenMemory 托管为 Windows 服务  
- 建议把 `nssm.exe` 放到：`scripts/windows/tools/nssm/nssm.exe`  

7) PowerShell 5.1+（Windows Server 默认满足）  

> 未安装 GNU Make 时，可使用等价入口：`.\scripts\windows\ci.ps1`（CI 门禁）和其他 `scripts/windows/*.ps1` 便携脚本。

### pgvector 编译安装（Windows）
```cmd
set "PGROOT=C:\Program Files\PostgreSQL\18"
cd %TEMP%
git clone --branch v0.8.1 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

> `nmake` 仅用于 pgvector 的 `Makefile.win` 编译安装，不等价于项目根目录的 GNU `make`。

## 方案 B：WSL2 + Debian 全栈（非 Docker）
### 必需
1) 启用 WSL2 + Debian（建议开启 systemd）  
2) Debian 内安装 PostgreSQL 18 与 pgvector  
3) Python 3.10+ 与 Node.js（>=18，建议最新 LTS）  

### Debian 侧安装示例
```bash
sudo apt update
sudo apt install -y postgresql-18 postgresql-18-pgvector
```

## 可选但建议
- pgAdmin（管理）或 Metabase（看板）
