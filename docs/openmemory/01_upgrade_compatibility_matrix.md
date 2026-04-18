# OpenMemory 升级兼容矩阵

本文档用于记录 Engram 对 OpenMemory 的**已验证兼容范围**、升级前核查项、以及回滚边界。

> **背景**：OpenMemory 上游仓库当前明确标注正在进行 “fully rewritten”。  
> 因此这里不把“语义版本号”视为唯一事实来源，而是同时记录：
> - GitHub Releases / Pre-release
> - 上游主线代码形态（路由、响应结构、环境变量默认值）
> - 本仓库已落地的兼容层范围

---

## 1. 当前兼容策略

### 1.0 Engram 当前固定基线

- OpenMemory 镜像默认基线：`ghcr.io/caviraoss/openmemory:v1.3.3`
- npm SDK 参考基线：`openmemory-js@1.3.3`
- 本仓库兼容层按 `1.3.3 / 当前主线行为` 设计，并保留对部分 `1.2.x` 差异的回退

### 1.1 我们兼容的上游差异面

当前 `src/engram/gateway/openmemory_client.py` 已覆盖以下差异：

| 兼容面 | 旧/另一种形态 | 当前上游常见形态 | 处理方式 |
|---|---|---|---|
| 搜索路径 | `/memory/search` | `/memory/query` | 自动尝试两者 |
| REST 前缀 | `/memory/*` | `/api/memory/*` | 自动尝试两者 |
| 搜索分页字段 | `limit` | `k` | 同时发送 |
| 列表分页字段 | `limit, offset` | `l, u` | 同时发送 |
| 列表结果字段 | `memories` / `results` | `items` | 自动解析 |
| 强化字段 | `memory_id, delta` | `id, boost` | 自动回退 |
| 健康返回 | `status=ok` | `ok=true` | 双格式兼容 |
| wipe 能力 | 独立 wipe 端点 | 无稳定 wipe 端点 | 逐条 DELETE 回退 |

### 1.2 当前仍不保证稳定的部分

- 上游镜像 tag 与 npm / Python SDK 版本不总是同步
- GitHub Releases、主线代码、发布到包管理器的版本可能不一致
- `latest` 镜像可能跨版本切换路由或默认值
- 向量维度、嵌入默认值、摘要长度等默认配置可能变化

---

## 2. 版本锁定策略

### 2.1 必须锁定的对象

升级或部署时，至少锁定以下对象中的一项：

- Docker 镜像 tag
- Docker 镜像 digest（优先）
- npm 包版本（`openmemory-js`）
- Python 包版本（`openmemory-py`）

### 2.2 禁止依赖的对象

- 生产环境禁止直接依赖 `ghcr.io/caviraoss/openmemory:latest`
- 不要只根据 GitHub Releases 判断“当前真实接口”
- 不要假设同名小版本一定保持完全兼容

### 2.3 推荐记录格式

每次升级建议在变更说明或迭代记录里至少写清：

```md
- OpenMemory source: ghcr.io/caviraoss/openmemory@sha256:...
- OpenMemory npm sdk: openmemory-js@...
- OpenMemory python sdk: openmemory-py@...
- Upstream reference commit: <sha>
- Verified routes:
  - GET /health -> ok=true
  - POST /memory/query
  - GET /memory/all (limit/offset + l/u)
  - POST /memory/reinforce (id/boost fallback needed?)
```

---

## 3. 升级前检查清单

升级前至少核查以下项目：

### 3.1 接口形态

- `GET /health` 返回的是 `status=ok` 还是 `ok=true`
- 搜索主路径是 `/memory/query` 还是 `/memory/search`
- 列表接口是否仍接受 `limit/offset`
- 强化接口是否使用 `memory_id/delta` 还是 `id/boost`
- 是否仍存在稳定的 wipe 端点

### 3.2 默认配置

- `OM_EMBEDDINGS` 默认值
- `OM_VEC_DIM` 默认值与推荐值
- `OM_SUMMARY_MAX_LENGTH` 默认值
- `OM_DECAY_INTERVAL_MINUTES` 默认值
- 服务端主 API Key 变量是否仍为 `OM_API_KEY`

### 3.3 认证与路由

- `Authorization: Bearer <key>` 是否继续生效
- `x-api-key` 是否继续生效
- public endpoints 是否发生变化
- `/api/*` 与无前缀路由是否共存

---

## 4. 本仓库最低验证动作

升级 OpenMemory 后，至少执行：

```bash
ruff check src/engram/gateway/openmemory_client.py tests/gateway/test_openmemory_client_compat.py
ruff format --check src/engram/gateway/openmemory_client.py tests/gateway/test_openmemory_client_compat.py
python -m py_compile src/engram/gateway/openmemory_client.py tests/gateway/test_openmemory_client_compat.py
```

如果本地具备完整测试依赖，再执行：

```bash
pytest tests/gateway/test_openmemory_client_compat.py -q
```

若统一栈可启动，再补充：

```bash
python scripts/ops/stack_doctor.py --full
```

---

## 5. 回滚条件

出现以下任一情况，应优先回滚到上一已验证版本，而不是继续堆兼容补丁：

- `memory_store` 无法返回 `memory_id` 且持续退化为 deferred
- `/health`、搜索、列表、强化接口同时发生多处断裂
- 上游默认向量维度改变导致现有 pgvector 列不兼容
- 服务端配置变量语义变化，导致部署脚本无法稳定复用

---

## 6. 当前建议

- 短期：继续保留当前 Gateway client 的多变体兼容策略
- 中期：把部署配置从 `latest` 切换到显式 tag / digest
- 长期：在确认上游 rewrite 稳定后，收敛兼容分支，回归单一主路径
