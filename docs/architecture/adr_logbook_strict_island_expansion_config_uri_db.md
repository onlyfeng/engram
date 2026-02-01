# ADR: Logbook 核心模块 Strict Island 扩展 (config/uri/db)

| 状态 | 已接受 |
|------|--------|
| 日期 | 2026-02-01 |
| 作者 | engram |

## 背景

Logbook 核心模块（`config.py`、`uri.py`、`db.py`）已纳入 Strict Island 范围，
在 `pyproject.toml` 中配置了严格的类型检查选项：

```toml
# 查看实际配置: grep -A 5 'tool.mypy.overrides' pyproject.toml

# 当前已配置的模块示例（以 pyproject.toml 为准）:
[[tool.mypy.overrides]]
module = "engram.logbook.config"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true

[[tool.mypy.overrides]]
module = "engram.logbook.uri"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true
```

## 当前 Strict Island 范围

> **SSOT**: 以 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths` 为准。

**查看当前 Strict Island 列表**：

```bash
# 方式 1: 使用 grep 提取
grep -A 20 'strict_island_paths' pyproject.toml | grep '"src/'

# 方式 2: 使用 Python 解析（推荐）
python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
```

**当前已纳入 Strict Island 的模块**（以 SSOT 为准）：

| 阶段 | 模块 | 说明 |
|------|------|------|
| 已完成 | `src/engram/gateway/di.py` | Gateway DI 核心 |
| 已完成 | `src/engram/gateway/container.py` | Gateway 容器配置 |
| 已完成 | `src/engram/gateway/services/` | Gateway 服务层 |
| 已完成 | `src/engram/gateway/handlers/` | Gateway 处理器 |
| 已完成 | `src/engram/gateway/policy.py` | Gateway 策略检查 |
| 已完成 | `src/engram/gateway/audit_event.py` | Gateway 审计事件 |
| 已完成 | `src/engram/logbook/config.py` | Logbook 配置模块 |
| 已完成 | `src/engram/logbook/uri.py` | Logbook URI 处理 |
| 已完成 | `src/engram/logbook/cursor.py` | Logbook 游标模块（阶段 3） |
| 已完成 | `src/engram/logbook/governance.py` | Logbook 治理模块（阶段 3） |
| 已完成 | `src/engram/logbook/outbox.py` | Logbook Outbox 模块（阶段 3） |

---

## 分阶段扩面计划

### 阶段 1: Gateway 核心（已完成）

| 模块 | 状态 | 验收命令 |
|------|------|----------|
| `gateway/di.py` | ✅ 已纳入 | `mypy src/engram/gateway/di.py` |
| `gateway/container.py` | ✅ 已纳入 | `mypy src/engram/gateway/container.py` |
| `gateway/services/` | ✅ 已纳入 | `mypy src/engram/gateway/services/` |

### 阶段 2: Gateway Handlers（计划中）

**准入条件**：
1. 模块在 baseline 中错误数 = 0
2. 已配置 `[[tool.mypy.overrides]]` 并启用 `disallow_untyped_defs = true`
3. `check_type_ignore_policy.py` 检查通过

| 模块 | 当前状态 | 准入检查 |
|------|----------|----------|
| `gateway/handlers/` | 📋 待清零 | `grep "gateway/handlers" scripts/ci/mypy_baseline.txt \| wc -l` |
| `gateway/audit_event.py` | 📋 待清零 | `grep "gateway/audit_event" scripts/ci/mypy_baseline.txt \| wc -l` |
| `gateway/policy.py` | 📋 待清零 | `grep "gateway/policy" scripts/ci/mypy_baseline.txt \| wc -l` |

### 阶段 3: Logbook 核心扩展（已完成）

**准入条件**：同阶段 2

| 模块 | 当前状态 | 准入检查 |
|------|----------|----------|
| `logbook/db.py` | 📋 待清零 | `grep "logbook/db.py" scripts/ci/mypy_baseline.txt \| wc -l` |
| `logbook/cursor.py` | ✅ 已纳入 | `mypy src/engram/logbook/cursor.py` |
| `logbook/outbox.py` | ✅ 已纳入 | `mypy src/engram/logbook/outbox.py` |
| `logbook/governance.py` | ✅ 已纳入 | `mypy src/engram/logbook/governance.py` |

### 阶段 4: 其他模块（待规划）

待阶段 2、3 完成后规划。

### mypy 检查结果（第一波核心模块）

使用项目配置（`scripts/ci/check_mypy_gate.py --gate strict`）：

| 文件 | 错误数 | 状态 |
|------|--------|------|
| config.py | 0 | ✅ 通过 |
| uri.py | 0 | ✅ 通过 |
| db.py | 0 | ✅ 通过 |

使用 `mypy --strict` 模式（含 `disallow_any_generics=true`）：

| 错误码 | 数量 | 典型修复手段 | 受影响文件/函数 |
|--------|------|--------------|-----------------|
| `[type-arg]` | 16 | `dict` → `dict[str, Any]` | config.py: `from_dict` |
|  |  | `list` → `list[Any]` | uri.py: `try_convert_to_artifact_key` |
|  |  | `tuple` → `tuple[bool, str \| None]` | uri.py: `parse_evidence_uri` |
|  |  |  | uri.py: `build_evidence_ref_for_patch_blob` |
|  |  |  | uri.py: `build_evidence_refs_json` |
|  |  |  | uri.py: `validate_evidence_ref` |
|  |  |  | uri.py: `AttachmentUriParseResult.to_dict` |
|  |  |  | uri.py: `parse_attachment_evidence_uri` |
|  |  |  | uri.py: `build_attachment_evidence_ref` |

### 配置差异分析

| 选项 | 项目 Strict Island | mypy --strict |
|------|-------------------|---------------|
| `disallow_untyped_defs` | ✅ true | ✅ true |
| `disallow_incomplete_defs` | ✅ true | ✅ true |
| `ignore_missing_imports` | ✅ false | ✅ false |
| `warn_return_any` | ✅ true | ✅ true |
| `disallow_any_generics` | ❌ false | ✅ true |

## 清零顺序建议

如需进一步提升类型安全性（启用 `disallow_any_generics=true`），建议按以下顺序修复：

### 阶段 1: 接口稳定区（最高优先级）

**错误码**: `[no-any-return]` / `[return-value]`

- 目标：函数返回值类型明确
- 影响范围小：仅涉及函数签名
- 修复难度低：通常只需添加返回类型注解

**当前状态**: 三个文件已无此类错误 ✅

### 阶段 2: 调用点收敛区

**错误码**: `[arg-type]` / `[assignment]`

- 目标：函数参数和变量赋值类型一致
- 需要追溯调用方：修改可能影响上游代码
- 建议策略：从叶子函数向上收敛

**当前状态**: 三个文件已无此类错误 ✅

### 阶段 3: 结构化数据收敛区

**错误码**: `[type-arg]`（泛型参数缺失）

- 目标：消除 `dict`、`list`、`tuple` 等裸泛型
- 修复方式：
  - `dict` → `dict[str, Any]`
  - `list` → `list[EvidenceRef]`（定义 TypedDict）
  - `tuple` → `tuple[bool, str | None]`
- 建议：引入 TypedDict 定义统一数据结构

**当前待修复**: 16 处

### 阶段 4: TypedDict 引入（推荐）

为 `evidence_refs_json` 等结构化数据定义 TypedDict：

```python
# uri.py
from typing import TypedDict

class EvidenceRef(TypedDict, total=False):
    """Evidence Reference 结构类型"""
    artifact_uri: str
    sha256: str
    source_id: str
    source_type: str
    kind: str
    size_bytes: int

class EvidenceRefsJson(TypedDict, total=False):
    """evidence_refs_json 结构类型"""
    patches: list[EvidenceRef]
    attachments: list[EvidenceRef]
```

### 阶段 5: 全局泛型严格模式（可选）

启用 `disallow_any_generics=true`：

```toml
[[tool.mypy.overrides]]
module = "engram.logbook.uri"
disallow_any_generics = true  # 新增
```

## 决策

1. **维持现状**: 当前项目 Strict Island 配置已满足 CI 门禁要求
2. **记录差距**: 16 个 `[type-arg]` 错误作为技术债务记录
3. **渐进改进**: 在后续迭代中按上述顺序逐步修复
4. **TypedDict 优先**: 优先为 `evidence_refs_json` 等核心结构定义 TypedDict

---

## 阶段 3 已完成：cursor/governance/outbox

> **已纳入**: cursor.py、governance.py、outbox.py 已于 2026-02-01 纳入 Strict Island。
> 当前实际已纳入的模块请运行以下命令查看：
> ```bash
> python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
> ```

### 纳岛完成记录（阶段 3）

| 顺序 | 模块 | 主要工作 | 状态 | 验证命令 |
|------|------|----------|------|----------|
| 1 | `cursor.py` | KV/水位线结构 TypedDict 完善 | ✅ 已纳入 | `mypy src/engram/logbook/cursor.py` |
| 2 | `governance.py` | policy_json/evidence_refs_json TypedDict 完善 | ✅ 已纳入 | `mypy src/engram/logbook/governance.py` |
| 3 | `outbox.py` | OutboxRow TypedDict + payload_sha 类型收紧 | ✅ 已纳入 | `mypy src/engram/logbook/outbox.py` |

### 模块分析

#### 1. cursor.py（KV/水位线结构）

**当前状态**: 已定义完整的 TypedDict 体系

已有 TypedDict：
- `SvnWatermark`, `GitLabWatermark`, `GitLabMRWatermark`, `GitLabReviewsWatermark`
- `CursorStats`, `CursorDict`
- `WatermarkType` 联合类型

**关键对外 API**:

| 函数/类 | 签名 | 调用方 |
|---------|------|--------|
| `load_cursor` | `(cursor_type, repo_id, config?) -> Cursor` | SCM sync runner/worker |
| `save_cursor` | `(cursor_type, repo_id, cursor, config?) -> bool` | SCM sync runner/worker |
| `upgrade_cursor` | `(data, cursor_type) -> Cursor` | 内部（load_cursor 调用）|
| `Cursor` dataclass | `version, watermark, stats` | SCM sync 全链路 |
| `should_advance_*_cursor` | `(...) -> bool` | gitlab_commits/gitlab_mrs 任务 |

**潜在 Breaking Change 点**:

1. `Cursor.watermark` 返回类型从 `Dict[str, Any]` 收紧为 `WatermarkType`
   - 影响：调用方需要处理 Union 类型的 narrowing
   - 缓解：保留 `Dict[str, Any]` 作为 fallback 分支

2. `_validate_watermark_type` 返回类型精确化
   - 影响：当前返回 `WatermarkType` 但内部修改原 dict
   - 建议：改为返回新 dict，避免原地修改

3. `Cursor.stats` 的 `# type: ignore[assignment]` 注释
   - 当前位置：第 265 行
   - 原因：`field(default_factory=dict)` 与 `CursorStats` 类型不兼容
   - 修复：使用 `field(default_factory=lambda: CursorStats())` 或保持 ignore

**与 Gateway 联动点**:

- **无直接联动**：cursor.py 主要服务于 SCM sync 模块
- **间接影响**：SCM sync 产生的 items 会进入 outbox，Gateway 消费

**纳岛前置条件**:

```bash
# 1. mypy 检查当前错误数
mypy --strict src/engram/logbook/cursor.py 2>&1 | grep "error:" | wc -l

# 2. 关键测试覆盖
pytest tests/logbook/test_cursor_overlap.py -v
pytest tests/logbook/test_gitlab_commit_cursor_tie_break.py -v
```

---

#### 2. governance.py（治理设置与审计）

**当前状态**: 已定义完整的 TypedDict 体系

已有 TypedDict：
- `SettingsRow`（settings 表行结构）
- `PatchEvidenceRef`, `AttachmentEvidenceRef`, `ExternalEvidenceRef`
- `EvidenceRefsJson`（evidence_refs_json 完整结构）
- `WriteAuditRow`（write_audit 表行结构）

**关键对外 API**:

| 函数/类 | 签名 | 调用方 |
|---------|------|--------|
| `get_settings` | `(project_key, config?, dsn?) -> Optional[SettingsRow]` | Gateway policy 检查 |
| `get_or_create_settings` | `(project_key, config?, dsn?) -> SettingsRow` | Gateway 初始化 |
| `upsert_settings` | `(project_key, team_write_enabled, policy_json?, ...) -> bool` | Admin CLI |
| `insert_write_audit` | `(actor, space, action, ..., evidence_refs_json?) -> int` | Gateway 审计写入 |
| `write_audit` | `(space, action, ..., patch_refs?) -> int` | SCM sync 审计写入 |
| `query_write_audit` | `(since?, limit?, actor?, ...) -> List[WriteAuditRow]` | 审计查询 CLI |
| `GovernanceSettings` class | `.get(key, project_key)`, `.set(key, value, ...)` | Gateway policy |

**潜在 Breaking Change 点**:

1. `policy_json` 参数类型收紧
   - 当前：`Optional[Dict]` 接受任意 dict
   - 收紧后：可定义 `PolicyJson(TypedDict)` 限制结构
   - 影响：调用方传入非法结构时 mypy 会报错

2. `evidence_refs_json` 返回类型
   - `query_write_audit` 返回的 `WriteAuditRow` 中 `evidence_refs_json` 类型
   - 当前使用 `cast(EvidenceRefsJson, row[6])`
   - 风险：数据库中存储的 JSON 可能不符合 TypedDict 定义

3. `_validate_policy_json` 返回值类型
   - 当前返回 `Dict`（裸泛型）
   - 应改为 `Dict[str, Any]` 满足 strict 检查

**与 Gateway 联动点**:

| 联动场景 | Gateway 模块 | 数据流向 |
|----------|--------------|----------|
| Policy 检查 | `gateway/policy.py` | Gateway → `get_settings()` → DB |
| 审计写入 | `gateway/handlers/*.py` | Gateway → `insert_write_audit()` → write_audit 表 |
| 设置更新 | `gateway/handlers/governance_update.py` | Gateway → `upsert_settings()` → settings 表 |

**纳岛前置条件**:

```bash
# 1. mypy 检查当前错误数
mypy --strict src/engram/logbook/governance.py 2>&1 | grep "error:" | wc -l

# 2. 关键测试覆盖
pytest tests/logbook/test_contract_shape.py -v
pytest tests/gateway/test_validate_refs.py -v
```

---

#### 3. outbox.py（Outbox 队列）

**当前状态**: 已定义核心 TypedDict

已有 TypedDict：
- `OutboxStatus = Literal["pending", "sent", "dead"]`
- `OutboxRowBase`（必需字段）
- `OutboxRow`（完整字段，继承 OutboxRowBase）
- `OutboxRowWithConn`（含 _conn，用于 claim_pending）
- `DedupResult`

**关键对外 API**:

| 函数 | 签名 | 调用方 |
|------|------|--------|
| `enqueue_memory` | `(payload_md?, target_space?, ...) -> int` | Gateway memory_store handler |
| `check_dedup` | `(target_space, payload_sha, config?) -> Optional[DedupResult]` | Gateway 幂等检查 |
| `claim_outbox` | `(worker_id, limit?, lease_seconds?, config?) -> List[OutboxRow]` | Gateway outbox_worker |
| `ack_sent` | `(outbox_id, worker_id, memory_id?, config?) -> bool` | Gateway outbox_worker |
| `fail_retry` | `(outbox_id, worker_id, error, next_attempt_at, config?) -> bool` | Gateway outbox_worker |
| `mark_dead_by_worker` | `(outbox_id, worker_id, error, config?) -> bool` | Gateway outbox_worker |
| `renew_lease` | `(outbox_id, worker_id, config?) -> bool` | Gateway outbox_worker |
| `get_pending` | `(limit?, config?, dsn?) -> List[OutboxRow]` | 诊断/测试 |
| `get_by_id` | `(outbox_id, config?) -> Optional[OutboxRow]` | 诊断/测试 |

**潜在 Breaking Change 点**:

1. `payload_sha` 类型收紧
   - 当前：`str`（任意字符串）
   - 建议：定义 `Sha256Hex = NewType('Sha256Hex', str)` 类型别名
   - 影响：需要在 `hashing.sha256()` 返回值处统一

2. `OutboxRowWithConn._conn` 类型
   - 当前：`Any`
   - 收紧后：`psycopg.Connection[Any]`
   - 注意：此字段仅内部使用，对外影响小

3. `enqueue_memory` 参数过多
   - 当前有 11 个参数，部分为兼容性参数（kind, project_key）
   - 建议：使用 `**kwargs` 或 dataclass 封装

4. `next_attempt_at` 参数类型
   - `fail_retry` 接受 `Union[datetime, str]`
   - 收紧后可能仅接受 `datetime`，需要检查调用方

**与 Gateway 联动点**:

| 联动场景 | Gateway 模块 | 数据流向 |
|----------|--------------|----------|
| 入队 | `gateway/handlers/memory_store.py` | Gateway → `enqueue_memory()` → outbox_memory 表 |
| 幂等检查 | `gateway/handlers/memory_store.py` | Gateway → `check_dedup()` → outbox_memory 表 |
| 消费 | `gateway/outbox_worker.py` | `claim_outbox()` → Gateway → OpenMemory → `ack_sent()` |
| 重试 | `gateway/outbox_worker.py` | 失败 → `fail_retry()` → 退避等待 |
| 死信 | `gateway/outbox_worker.py` | 不可恢复 → `mark_dead_by_worker()` |
| 续期 | `gateway/outbox_worker.py` | 长调用前 → `renew_lease()` |

**纳岛前置条件**:

```bash
# 1. mypy 检查当前错误数
mypy --strict src/engram/logbook/outbox.py 2>&1 | grep "error:" | wc -l

# 2. 关键测试覆盖
pytest tests/logbook/test_unified_stack_integration.py -v -k outbox
pytest tests/gateway/test_unified_stack_integration.py -v -k outbox
```

---

### 清零报告任务（历史记录）

为保持与第一波一致的治理流程，建议创建以下清零报告：

| 任务 | 输出文件 | 内容 |
|------|----------|------|
| cursor.py 清零报告 | `artifacts/mypy_cursor_strict_report.txt` | mypy --strict 输出 + 分类统计 |
| governance.py 清零报告 | `artifacts/mypy_governance_strict_report.txt` | mypy --strict 输出 + 分类统计 |
| outbox.py 清零报告 | `artifacts/mypy_outbox_strict_report.txt` | mypy --strict 输出 + 分类统计 |

**清零报告生成命令**:

```bash
# cursor.py
mypy --strict src/engram/logbook/cursor.py 2>&1 | tee artifacts/mypy_cursor_strict_report.txt

# governance.py
mypy --strict src/engram/logbook/governance.py 2>&1 | tee artifacts/mypy_governance_strict_report.txt

# outbox.py
mypy --strict src/engram/logbook/outbox.py 2>&1 | tee artifacts/mypy_outbox_strict_report.txt
```

### pyproject.toml 配置（已应用）

> **已配置**: 以下配置已于 2026-02-01 添加到 `pyproject.toml`。

```bash
# 查看当前实际配置
grep -A 5 'tool.mypy.overrides' pyproject.toml
```

**阶段 3 已应用配置**：

```toml
# cursor.py - 已纳入
[[tool.mypy.overrides]]
module = "engram.logbook.cursor"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true

# governance.py - 已纳入
[[tool.mypy.overrides]]
module = "engram.logbook.governance"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true

# outbox.py - 已纳入
[[tool.mypy.overrides]]
module = "engram.logbook.outbox"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false
warn_return_any = true
```

---

## 参考文件

- mypy 检查报告: `artifacts/mypy_strict_island_check.txt`
- pyproject.toml: `[tool.mypy.overrides]` 配置
- Strict Island 路径: `[tool.engram.mypy].strict_island_paths`
