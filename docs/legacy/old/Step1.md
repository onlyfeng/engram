Step1

下面给你一套 **Step 1（SQLite）完整“设计 + 使用 + 部署”步骤**，并且把它落到你“SVN 合并同步流程”这类工作流上作为示例，但**能力本身是通用的：赋予 Agent 高效记录 / 查询 / 导出视图的能力**。整体尽量对齐业界成熟开源方案：以 **sqlite-utils +（可选）Datasette** 为基座，在其上做最薄的一层定制封装。 ([sqlite-utils.datasette.io][3])

> 说明：我在你历史“SVN 合并同步流程”对话中没有检索到可复用的“字段/目录/参数”细则，所以我以下按行业常见约定给出一套可直接启用的默认规范；你后续如有现成 manifest 字段，也可以无痛映射迁移。

---

## Step 1 总目标与原则

**目标**：把 Agent 现在靠 `grep + edit` 维护 `manifest.csv / index.md` 的方式，升级为：

* **SQLite 作为权威事实源（Single Source of Truth）**
* `manifest.csv / index.md` 变成 **可再生视图（render outputs）**，禁止手改
* Agent 写入只能走“受控命令”，不允许任意编辑文件或随意 SQL DML

**关键工程原则**

1. **写入口收敛**：所有状态与记录写入必须通过一个 CLI（Python）
2. **追加式事件优先**：保留审计与回放能力（谁在何时做了什么）
3. **视图可重建**：人看的文档/表格全由 `render` 生成
4. **并发更稳**：启用 WAL（Write-Ahead Logging）以提升并发读写稳定性 ([sqlite.org][4])

---

## 1. 仓库目录与文件约定（建议直接照搬）

在你 Cursor 工程里新增：

```
.agentx/
  logbook/                     # 通用记录能力（不绑定 SVN）
    agent.db                   # SQLite 数据库（权威源）
    schema.sql                 # 建表脚本
    scripts/
      logbook.py               # 受控写入口（Python CLI）
    views/                     # 可再生视图（禁止手改）
      manifest.csv
      index.md
    exports/                   # 可选：导出快照/共享产物
      events.jsonl
```

**约定**

* `.agentx/logbook/agent.db`：权威源
* `.agentx/logbook/views/*`：全部由 `logbook.py render` 生成，禁止手改
* 如需提交到 Git：优先提交 `views/`（可读）与 `exports/`（可审计），`agent.db` 是否提交取决于你是否需要共享（见第 8 节）

---

## 2. Python 依赖安装（两种主流方式）

### 方式 A：项目虚拟环境（推荐）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# mac/linux: source .venv/bin/activate
pip install sqlite-utils datasette typer jinja2
```

sqlite-utils 是成熟的 SQLite 操作工具（CLI + Python 库），并支持插件机制。([SQLite Tutorial PyCon 2023][5])
Datasette 用于把 SQLite 暴露为网页与 JSON API（可选）。([docs.datasette.io][6])

### 方式 B：pipx 安装 CLI（偏全局）

若你更想把 sqlite-utils / datasette 当系统工具用，也可用 pipx。([SQLite Tutorial PyCon 2023][5])

---

## 3. Schema 设计（通用：条目 Items + 事件 Events + 附件 Attachments）

把所有“要记录的对象”统一抽象成 `items`，并用 `events` 追加记录变化；任何文件/链接/证据走 `attachments`。

将以下内容保存为 `.agentx/logbook/schema.sql`：

```sql
PRAGMA foreign_keys = ON;

-- 条目：一个可追踪对象（任务、流程实例、合并批次、缺陷、评审……）
CREATE TABLE IF NOT EXISTS items (
  id            TEXT PRIMARY KEY,          -- 建议：时间戳+短随机，或外部ID（如 r1234）
  kind          TEXT NOT NULL,             -- 类别：svn_merge / workflow_run / incident / review ...
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'open',
  owner         TEXT,
  priority      INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL,             -- ISO8601
  updated_at    TEXT NOT NULL,             -- ISO8601
  meta_json     TEXT NOT NULL DEFAULT '{}'  -- 扩展字段（JSON 字符串）
);

CREATE INDEX IF NOT EXISTS idx_items_kind_status ON items(kind, status);
CREATE INDEX IF NOT EXISTS idx_items_updated_at ON items(updated_at);

-- 事件：追加式审计记录（推荐作为主要写入路径）
CREATE TABLE IF NOT EXISTS events (
  event_id      TEXT PRIMARY KEY,
  item_id       TEXT NOT NULL,
  event_type    TEXT NOT NULL,             -- started/exported/applied/reviewed/committed/failed/note...
  message       TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  actor         TEXT,                      -- 人或 Agent
  payload_json  TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_item_time ON events(item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, created_at);

-- 附件/证据：只存路径与摘要，不把大文件塞 DB
CREATE TABLE IF NOT EXISTS attachments (
  attachment_id TEXT PRIMARY KEY,
  item_id       TEXT NOT NULL,
  event_id      TEXT,
  role          TEXT NOT NULL,             -- patch/log/report/screenshot/url...
  path          TEXT NOT NULL,
  sha256        TEXT,
  note          TEXT,
  created_at    TEXT NOT NULL,
  FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE,
  FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_attach_item_role ON attachments(item_id, role);

-- 轻量 KV：工作流配置 / 指标阈值 / 最近一次运行点位（可选但实用）
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

---

## 4. 初始化数据库（含 WAL）与基础命令

### 4.1 初始化命令（一次性）

你可以直接用 sqlite3 或 Python 去设置 WAL。SQLite 官方推荐通过 PRAGMA 启用 WAL：`PRAGMA journal_mode=WAL;` ([sqlite.org][4])

建议你在 `logbook.py init` 里做这件事；如果先手工：

```bash
sqlite3 .agentx/logbook/agent.db "PRAGMA journal_mode=WAL;"
sqlite3 .agentx/logbook/agent.db < .agentx/logbook/schema.sql
```

---

## 5. 受控写入口：logbook.py（最薄封装，Agent 只调用它）

将以下保存为 `.agentx/logbook/scripts/logbook.py`（尽量短、只做约束与便利性）：

```python
from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
import typer
import sqlite3
from jinja2 import Template

app = typer.Typer(no_args_is_help=True)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "agent.db"
SCHEMA_PATH = ROOT / "schema.sql"
VIEWS_DIR = ROOT / "views"
EXPORTS_DIR = ROOT / "exports"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def gen_id(prefix: str) -> str:
    return f"{prefix}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

@app.command()
def init():
    """初始化数据库 + 建表 + 启用 WAL + 创建目录"""
    VIEWS_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect()
    # WAL for better concurrent read/write
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    typer.echo(f"OK: initialized {DB_PATH}")

@app.command()
def create_item(kind: str, title: str, status: str = "open", owner: str = "", priority: int = 0, meta: str = "{}"):
    """创建一个条目（item）"""
    item_id = gen_id("item")
    ts = now_iso()
    conn = connect()
    conn.execute(
        "INSERT INTO items(id, kind, title, status, owner, priority, created_at, updated_at, meta_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (item_id, kind, title, status, owner or None, priority, ts, ts, meta),
    )
    conn.commit()
    conn.close()
    typer.echo(item_id)

@app.command()
def add_event(item_id: str, event_type: str, message: str, actor: str = "agent", payload: str = "{}"):
    """追加事件（推荐作为主写入）"""
    event_id = gen_id("evt")
    ts = now_iso()
    conn = connect()
    conn.execute(
        "INSERT INTO events(event_id, item_id, event_type, message, created_at, actor, payload_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (event_id, item_id, event_type, message, ts, actor, payload),
    )
    conn.execute("UPDATE items SET updated_at=? WHERE id=?", (ts, item_id))
    conn.commit()
    conn.close()
    typer.echo(event_id)

@app.command()
def attach(item_id: str, role: str, path: str, event_id: str = "", note: str = ""):
    """登记附件/证据（patch/log/report/url 等）"""
    p = Path(path)
    sha = sha256_file(p) if p.exists() and p.is_file() else None
    attachment_id = gen_id("att")
    ts = now_iso()
    conn = connect()
    conn.execute(
        "INSERT INTO attachments(attachment_id, item_id, event_id, role, path, sha256, note, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (attachment_id, item_id, event_id or None, role, path, sha, note or None, ts),
    )
    conn.execute("UPDATE items SET updated_at=? WHERE id=?", (ts, item_id))
    conn.commit()
    conn.close()
    typer.echo(attachment_id)

@app.command()
def set_status(item_id: str, status: str):
    """受控更新：只允许改 status（避免任意字段被乱写）"""
    ts = now_iso()
    conn = connect()
    conn.execute("UPDATE items SET status=?, updated_at=? WHERE id=?", (status, ts, item_id))
    conn.commit()
    conn.close()
    typer.echo("OK")

@app.command()
def export_events_jsonl(out: str = ""):
    """导出事件 JSONL（便于共享/审计）"""
    out_path = Path(out) if out else (EXPORTS_DIR / "events.jsonl")
    conn = connect()
    cur = conn.execute("SELECT event_id, item_id, event_type, message, created_at, actor, payload_json FROM events ORDER BY created_at")
    with out_path.open("w", encoding="utf-8") as f:
        for r in cur:
            f.write(json.dumps({
                "event_id": r[0], "item_id": r[1], "event_type": r[2], "message": r[3],
                "created_at": r[4], "actor": r[5], "payload": json.loads(r[6] or "{}")
            }, ensure_ascii=False) + "\n")
    conn.close()
    typer.echo(f"OK: {out_path}")

@app.command()
def render():
    """生成 views/manifest.csv 与 views/index.md（禁止手改）"""
    conn = connect()

    # manifest.csv：给人/工具看的结构化清单
    manifest_path = VIEWS_DIR / "manifest.csv"
    rows = conn.execute(
        "SELECT id, kind, title, status, owner, priority, created_at, updated_at FROM items ORDER BY updated_at DESC"
    ).fetchall()
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "title", "status", "owner", "priority", "created_at", "updated_at"])
        w.writerows(rows)

    # index.md：给人看的导航（可按你需要改模板）
    items = conn.execute(
        "SELECT id, kind, title, status, updated_at FROM items ORDER BY updated_at DESC LIMIT 200"
    ).fetchall()

    tpl = Template(
        "# Logbook 索引\n\n"
        "说明：本文件由 `logbook.py render` 自动生成，禁止手工编辑。\n\n"
        "## 最近更新（Top 200）\n\n"
        "{% for i in items %}"
        "- `{{ i[1] }}` **{{ i[2] }}**  | id={{ i[0] }} | status={{ i[3] }} | updated={{ i[4] }}\n"
        "{% endfor %}\n"
    )
    index_path = VIEWS_DIR / "index.md"
    index_path.write_text(tpl.render(items=items), encoding="utf-8")

    conn.close()
    typer.echo("OK: rendered views/manifest.csv and views/index.md")

@app.command()
def validate():
    """基础一致性校验（可按工作流逐步加强）"""
    conn = connect()
    # 1) 孤儿事件
    orphan = conn.execute(
        "SELECT count(*) FROM events e LEFT JOIN items i ON e.item_id=i.id WHERE i.id IS NULL"
    ).fetchone()[0]
    if orphan:
        raise typer.Exit(code=2)

    # 2) attachments path 为空
    bad = conn.execute("SELECT count(*) FROM attachments WHERE path='' OR path IS NULL").fetchone()[0]
    if bad:
        raise typer.Exit(code=2)

    conn.close()
    typer.echo("OK")

if __name__ == "__main__":
    app()
```

---

## 6. 用法：把 SVN 合并同步流程“接入记录能力”（示例用例，不绑定设计）

你在 SVN 流程里通常会经历：开始 → 导出补丁 → 应用补丁/处理冲突 → 验证 → 提交 → 收尾。
每个关键节点都追加事件，并在必要处挂附件。

示例（命令行）：

```bash
# 0) 初始化一次
python .agentx/logbook/scripts/logbook.py init

# 1) 创建一个“本次合并批次”条目（通用 item）
ITEM_ID=$(python .agentx/logbook/scripts/logbook.py create-item \
  --kind svn_merge --title "A分支 r1200-r1250 合并到B分支")

# 2) 开始事件
python .agentx/logbook/scripts/logbook.py add-event --item-id $ITEM_ID \
  --event-type started --message "开始导出补丁/准备合并" --actor "onlyfeng"

# 3) 导出补丁后：登记补丁文件
EVT=$(python .agentx/logbook/scripts/logbook.py add-event --item-id $ITEM_ID \
  --event-type exported --message "已导出补丁 r1200-r1250" --payload '{"rev_range":"1200-1250"}')
python .agentx/logbook/scripts/logbook.py attach --item-id $ITEM_ID --event-id $EVT \
  --role patch --path "patches/r1200-1250.diff" --note "A->B 导出补丁"

# 4) 应用补丁
EVT2=$(python .agentx/logbook/scripts/logbook.py add-event --item-id $ITEM_ID \
  --event-type applied --message "补丁已应用，开始处理冲突")
python .agentx/logbook/scripts/logbook.py attach --item-id $ITEM_ID --event-id $EVT2 \
  --role log --path "logs/apply.log"

# 5) 验证完成/提交
python .agentx/logbook/scripts/logbook.py add-event --item-id $ITEM_ID \
  --event-type verified --message "编译/运行验证通过"
python .agentx/logbook/scripts/logbook.py add-event --item-id $ITEM_ID \
  --event-type committed --message "已提交到 SVN：r13000" --payload '{"commit_rev":"13000"}'
python .agentx/logbook/scripts/logbook.py set-status --item-id $ITEM_ID --status closed

# 6) 生成视图
python .agentx/logbook/scripts/logbook.py render
```

---

## 7. 让 Cursor Agent “必须走受控写入口”的工作流规则（建议直接写进你的 workflow 说明）

建议你在 SVN 工作流的说明文档/规则里增加硬约束（核心是防止回到 `grep/edit`）：

* 禁止 Agent 直接编辑：

  * `.agentx/logbook/views/manifest.csv`
  * `.agentx/logbook/views/index.md`
* 禁止 Agent 用 `sed/grep` 修改任何“状态源”，状态源只允许 SQLite
* 允许的写入动作只有：

  * `python .agentx/logbook/scripts/logbook.py create-item`
  * `... add-event`
  * `... attach`
  * `... set-status`
  * `... render`
  * `... validate`

---

## 8. 共享与提交策略（避免 DB 文件产生合并冲突）

给你两个模式，按团队协作强度选择：

### 模式 A：本地权威（默认推荐）

* `agent.db` 不入库（或放到 `.gitignore`）
* 入库：`views/*` + `exports/events.jsonl`（可审计、可 diff）

这样既保留“权威源在本地”的高效写入，又能让团队共享“人可读/可审计”的产物。

### 模式 B：库内权威（单人维护或强约束线性提交）

* `agent.db` 入库
* 前提：同一时间只有一个维护者写；或者严格线性提交，避免频繁分支合并 DB

---

## 9. （可选）加 Datasette：给 Agent 一个稳定的 JSON 读取接口

如果你希望 Agent 查询结果更结构化（减少解析文本），可以跑 Datasette：

```bash
datasette .agentx/logbook/agent.db --cors
```

Datasette 的 JSON API 规则是：任何页面 URL 末尾加 `.json` 即可获得 JSON；`--cors` 会增加跨域头。([docs.datasette.io][6])

例：

* `http://127.0.0.1:8001/`（网页）
* `http://127.0.0.1:8001/.json`（JSON）

---

## 你现在可以直接执行的最小落地顺序（建议照此推进）

1. 按第 1–4 节把目录、依赖、schema、init 跑通
2. 把 SVN 合并脚本的每个关键节点接入 `add-event/attach/set-status`（第 6 节）
3. 把原来的 `manifest.csv/index.md` 改成 `render` 产物（第 5/7 节）
4. （可选）开启 Datasette 做 JSON 查询接口（第 9 节）

如果你希望我下一步继续“逐步执行”，我建议从 **把你现有的 manifest.csv 字段映射到 items/events** 开始：你把当前 `manifest.csv` 的表头（第一行）贴出来即可，我会给你一份不破坏现有字段的迁移脚本与 render 模板调整方案。

[1]: https://chatgpt.com/c/69677656-bc48-8330-9de2-29420c06555c "Agent 状态存储优化"
[2]: https://chatgpt.com/c/695be950-af20-8320-9bd3-f31bf844e03d "SVN合并同步流程"
[3]: https://sqlite-utils.datasette.io/en/stable/cli.html?utm_source=chatgpt.com "sqlite-utils command-line tool"
[4]: https://sqlite.org/wal.html?utm_source=chatgpt.com "Write-Ahead Logging"
[5]: https://sqlite-tutorial-pycon-2023.readthedocs.io/en/latest/sqlite-utils.html?utm_source=chatgpt.com "sqlite-utils - Data analysis with SQLite and Python, PyCon 2023"
[6]: https://docs.datasette.io/en/stable/json_api.html?utm_source=chatgpt.com "JSON API"
