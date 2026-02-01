# mypy 错误码修复 Playbook

> 状态: **生效中**  
> 创建日期: 2026-02-01  
> 决策者: Engram Core Team

---

## 1. 概述

本文档定义每个迭代的 mypy 错误码清理目标、常见修复模板、以及临时抑制策略。

**关联文档**：
- [ADR: mypy 基线管理与 Gate 门禁策略](../architecture/adr_mypy_baseline_and_gating.md)
- [mypy 基线管理操作指南](./mypy_baseline.md)

---

## 2. 迭代错误码清理计划

### 2.1 迭代收敛路线

| 迭代 | 目标错误码 | 目标减少条数 | 减少占比 | 验收标准 |
|------|-----------|-------------|---------|----------|
| **v1.0** | `[no-any-return]` | ≥ 30 条 | ~18% | gateway/ 无此类错误 |
| **v1.1** | `[assignment]`, `[arg-type]` | ≥ 40 条 | ~24% | logbook/ 核心模块清零 |
| **v1.2** | `[union-attr]`, `[return-value]` | ≥ 25 条 | ~15% | Optional 收敛完成 |
| **v1.3** | `[call-arg]`, `[attr-defined]` | ≥ 20 条 | ~12% | 接口调用一致 |
| **v1.4** | `[misc]`, `[var-annotated]`, `[operator]` | ≥ 15 条 | ~9% | 杂项清零 |
| **v2.0** | 全部 | 剩余全部 | 100% | 基线归零 |

### 2.2 当前基线错误统计（按错误码）

```
[no-any-return]    ~25 条  # Returning Any from function
[assignment]       ~35 条  # Incompatible types in assignment
[arg-type]         ~30 条  # Argument type incompatible
[union-attr]       ~15 条  # Item "None" of ... has no attribute
[return-value]     ~10 条  # Incompatible return value type
[call-arg]         ~15 条  # Missing/unexpected keyword argument
[attr-defined]     ~10 条  # "X" has no attribute "Y"
[misc]             ~10 条  # Various issues
[var-annotated]    ~5 条   # Need type annotation
[operator]         ~5 条   # Unsupported operand types
[no-redef]         ~5 条   # Name already defined
其他               ~5 条
```

---

## 3. 错误码修复模板

### 3.1 `[no-any-return]` - 返回值 Any 问题

**错误示例**：
```python
def get_config():
    return os.environ.get("KEY")  # error: Returning Any from function
```

**修复模板 A：显式返回类型注解**
```python
def get_config() -> str | None:
    return os.environ.get("KEY")
```

**修复模板 B：运行时类型断言**
```python
def get_config() -> str:
    value = os.environ.get("KEY")
    assert value is not None, "KEY must be set"
    return value
```

**修复模板 C：使用 cast（慎用）**
```python
from typing import cast

def get_config() -> str:
    return cast(str, os.environ.get("KEY"))
```

---

### 3.2 `[assignment]` - 类型赋值不兼容

**错误示例**：
```python
result: str = some_func()  # error: Incompatible types in assignment
```

**修复模板 A：修正变量类型注解**
```python
result: str | None = some_func()  # 如果 some_func 可能返回 None
```

**修复模板 B：收窄返回类型**
```python
def some_func() -> str:  # 明确返回 str，内部处理 None 情况
    value = _inner_func()
    return value or ""
```

**修复模板 C：Optional 收敛**
```python
# Before
data: dict = func()  # func 返回 dict | None

# After
data = func()
if data is None:
    data = {}
```

---

### 3.3 `[arg-type]` - 参数类型不匹配

**错误示例**：
```python
def process(items: list[str]) -> None: ...
process(["a", None])  # error: Argument 1 has incompatible type
```

**修复模板 A：过滤 None**
```python
items = [x for x in raw_items if x is not None]
process(items)
```

**修复模板 B：修改函数签名**
```python
def process(items: list[str | None]) -> None:
    clean_items = [x for x in items if x is not None]
    ...
```

---

### 3.4 `[union-attr]` - Optional 对象属性访问

**错误示例**：
```python
result = obj.method()  # error: Item "None" of "X | None" has no attribute "method"
```

**修复模板 A：显式 None 检查（推荐）**
```python
if obj is not None:
    result = obj.method()
else:
    result = default_value
```

**修复模板 B：使用 assert 收窄**
```python
assert obj is not None, "obj should not be None at this point"
result = obj.method()
```

**修复模板 C：使用 Optional 链式调用**
```python
result = obj.method() if obj else default_value
```

---

### 3.5 `[call-arg]` - 函数调用参数问题

**错误示例**：
```python
def func(a: int, b: str) -> None: ...
func(1)  # error: Missing named argument "b"
func(1, "x", c=3)  # error: Unexpected keyword argument "c"
```

**修复模板 A：补全必需参数**
```python
func(1, b="default")
```

**修复模板 B：使用 **kwargs 显式忽略（谨慎）**
```python
def func(a: int, b: str, **kwargs: Any) -> None: ...
```

---

### 3.6 `[attr-defined]` - 属性不存在

**错误示例**：
```python
obj.nonexistent_attr  # error: "X" has no attribute "nonexistent_attr"
```

**修复模板 A：使用 hasattr 防御**
```python
if hasattr(obj, "nonexistent_attr"):
    value = obj.nonexistent_attr
```

**修复模板 B：使用 getattr 带默认值**
```python
value = getattr(obj, "nonexistent_attr", default_value)
```

**修复模板 C：修复类型声明**
```python
class X:
    nonexistent_attr: str  # 添加属性声明
```

---

### 3.7 `[var-annotated]` - 需要类型注解

**错误示例**：
```python
data = {}  # error: Need type annotation for "data"
```

**修复模板**：
```python
data: dict[str, Any] = {}
# 或更精确
data: dict[str, int] = {}
```

---

### 3.8 `[operator]` - 操作符类型不兼容

**错误示例**：
```python
value: float | None = get_value()
result = value * 2  # error: Unsupported operand types for * ("None" and "int")
```

**修复模板**：
```python
value = get_value()
if value is not None:
    result = value * 2
else:
    result = 0
```

---

### 3.9 Mapping 替换 dict invariance 问题

**错误示例**：
```python
def func(data: dict[str, str]) -> None: ...
subtype_dict: dict[str, MyStr] = {}  # MyStr 是 str 子类
func(subtype_dict)  # error: dict is invariant
```

**修复模板**：
```python
from collections.abc import Mapping

def func(data: Mapping[str, str]) -> None: ...
# Mapping 是 covariant，接受子类型
```

**场景说明**：
- `dict[K, V]` 是 **invariant** — K 和 V 必须精确匹配
- `Mapping[K, V]` 是 **covariant** — V 可以是子类型
- 当函数只读取 dict 时，应使用 `Mapping` 代替 `dict`

---

## 4. 临时抑制策略

### 4.1 允许的抑制格式

> **强制规则**：所有 `# type: ignore` 必须带有错误码。

**✅ 允许**：
```python
value = func()  # type: ignore[no-any-return]
data = obj.attr  # type: ignore[union-attr]
result = call(x)  # type: ignore[arg-type]  # 第三方库类型不完整
```

**❌ 禁止**：
```python
value = func()  # type: ignore  # 裸抑制，禁止
data = obj.attr  # noqa  # noqa 不适用于 mypy
```

### 4.2 Strict Island 专用约束（强制）

> **CI 强阻断**：strict-island 路径下的 `# type: ignore` 有更严格的约束。

Strict Island 是 CI 强阻断岛屿集合，定义在 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths`。
这些模块有最高的类型安全要求，对 `# type: ignore` 的使用有额外限制。

#### 4.2.1 Strict Island 的 type: ignore 规则

| 规则 | 要求 | CI 检查 |
|------|------|---------|
| **规则 1** | 必须带 `[error-code]` | `check_type_ignore_policy.py` |
| **规则 2** | 必须带原因说明 | `check_type_ignore_policy.py` |

#### 4.2.2 原因说明格式

**✅ 允许的原因说明格式**：

```python
# 方式 1: 同行注释描述
result = lib.call(x)  # type: ignore[arg-type]  # 第三方库类型不完整

# 方式 2: TODO + issue 编号
data = legacy_func()  # type: ignore[no-any-return]  # TODO: #456

# 方式 3: TODO + issue 名称
value = external_api()  # type: ignore[union-attr]  # TODO: #fix-external-types

# 方式 4: URL 引用
config = load()  # type: ignore[assignment]  # https://github.com/org/repo/issues/123
```

**❌ Strict Island 内禁止**：

```python
# 缺少错误码
value = func()  # type: ignore

# 有错误码但缺少原因说明
data = obj.attr  # type: ignore[union-attr]

# 原因说明在下一行（不会被检测）
# TODO: #123 修复类型问题
result = call(x)  # type: ignore[arg-type]
```

#### 4.2.3 当前 Strict Island 范围

> **SSOT**: 以 `pyproject.toml` 的 `[tool.engram.mypy].strict_island_paths` 为准。

**查看当前列表**（权威来源）：

```bash
python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
```

**当前已纳入的模块**（以 SSOT 为准）：

```
# Gateway 核心模块（DI 相关）
src/engram/gateway/di.py
src/engram/gateway/container.py
src/engram/gateway/services/
src/engram/gateway/handlers/

# Gateway 策略与审计模块
src/engram/gateway/policy.py
src/engram/gateway/audit_event.py

# Logbook 核心配置模块
src/engram/logbook/config.py
src/engram/logbook/uri.py

# Logbook 数据结构模块
src/engram/logbook/cursor.py
src/engram/logbook/governance.py
src/engram/logbook/outbox.py

# Logbook 数据库与视图模块
src/engram/logbook/db.py
src/engram/logbook/views.py
src/engram/logbook/artifact_gc.py
```

**计划扩面的模块**（下一阶段待规划）：

| 阶段 | 模块 | 准入检查 |
|------|------|----------|
| 阶段 5 | `logbook/scm_*.py` | `grep "logbook/scm_" scripts/ci/mypy_baseline.txt \| wc -l` |
| 阶段 5 | `gateway/app.py` | `grep "gateway/app.py" scripts/ci/mypy_baseline.txt \| wc -l` |
| 阶段 5 | `gateway/main.py` | `grep "gateway/main.py" scripts/ci/mypy_baseline.txt \| wc -l` |

#### 4.2.4 CI 检查命令

**CI 自动执行**：在 `.github/workflows/ci.yml` 的 `lint` job 中会自动运行此检查。

**本地运行方式**：

```bash
# 方式 1: 使用 Makefile（推荐）
make check-type-ignore-policy

# 方式 2: 直接运行脚本
python scripts/ci/check_type_ignore_policy.py --verbose

# 仅统计（不阻断）
python scripts/ci/check_type_ignore_policy.py --stats-only

# 检查指定路径（覆盖 strict_island_paths）
python scripts/ci/check_type_ignore_policy.py --paths src/engram/gateway/

# 运行完整 CI 检查（包含此检查）
make ci
```

#### 4.2.5 违规修复示例

**场景 1: 缺少错误码**

```python
# Before (违规)
value = external_lib.func()  # type: ignore

# After (合规)
value = external_lib.func()  # type: ignore[no-any-return]  # requests 无类型桩
```

**场景 2: 缺少原因说明**

```python
# Before (违规)
data = legacy_api.get_data()  # type: ignore[union-attr]

# After (合规 - 方式 1: 描述)
data = legacy_api.get_data()  # type: ignore[union-attr]  # API 返回 Optional 但已有运行时检查

# After (合规 - 方式 2: TODO)
data = legacy_api.get_data()  # type: ignore[union-attr]  # TODO: #789 升级 API 类型定义
```

### 4.3 各错误码抑制策略

| 错误码 | 允许临时抑制 | 抑制条件 | 禁止项 |
|--------|-------------|---------|--------|
| `[no-any-return]` | ⚠️ 受限 | 第三方库返回 Any | 新代码禁用 |
| `[assignment]` | ❌ 不推荐 | - | 应修复而非抑制 |
| `[arg-type]` | ⚠️ 受限 | 第三方库签名不完整 | 自有代码禁用 |
| `[union-attr]` | ⚠️ 受限 | 已有运行时检查 | 缺乏运行时保护时禁用 |
| `[call-arg]` | ⚠️ 受限 | 第三方库签名错误 | 自有代码禁用 |
| `[attr-defined]` | ⚠️ 受限 | 动态属性（如 ORM） | 静态类禁用 |
| `[import-untyped]` | ✅ 允许 | 第三方库无 stubs | - |
| `[misc]` | ⚠️ 个案评估 | 需附说明 | 无说明禁用 |
| `[var-annotated]` | ❌ 禁止 | - | 应添加注解 |
| `[operator]` | ❌ 禁止 | - | 应修复类型 |
| `[no-redef]` | ⚠️ 受限 | 条件导入等 | 应重构 |

### 4.4 抑制注释规范

**必须包含说明**（当使用 ignore 时）：

```python
# Good: 带说明
result = external_lib.func()  # type: ignore[no-any-return]  # requests 无类型桩

# Good: 引用 issue
value = legacy_func()  # type: ignore[assignment]  # TODO: fix in #456

# Bad: 无说明
data = func()  # type: ignore[arg-type]
```

### 4.5 批量抑制审批

| 同一 PR 中 ignore 数量 | 审批要求 |
|-----------------------|---------|
| 1-3 条 | Reviewer 批准 |
| 4-10 条 | 2 位 Reviewer + 附修复计划 |
| > 10 条 | ❌ 禁止，应拆分 PR |

---

## 5. 各错误码详细修复指南

### 5.1 `[no-any-return]` 专项

**优先级**：P0 - 迭代 v1.0 目标

**常见来源**：
1. `os.environ.get()` 返回值
2. JSON 解析结果
3. 外部库函数返回值

**修复策略**：

| 来源 | 推荐修复方式 |
|------|-------------|
| `os.environ.get()` | 添加返回类型 `-> str \| None` |
| `json.loads()` | 使用 TypedDict 或 `-> dict[str, Any]` |
| 外部库 | 添加 `cast()` 或 `# type: ignore[no-any-return]` |

**示例迁移**：

```python
# Before
def load_config():
    return json.loads(Path("config.json").read_text())

# After
from typing import TypedDict

class Config(TypedDict):
    host: str
    port: int

def load_config() -> Config:
    data = json.loads(Path("config.json").read_text())
    return Config(host=data["host"], port=data["port"])
```

---

### 5.2 `[assignment]` + `[union-attr]` 专项

**优先级**：P1 - 迭代 v1.1/v1.2 目标

**核心问题**：Optional 类型收敛不彻底

**推荐模式**：Early Return + Guard Clause

```python
# Before (问题代码)
def process(data: dict | None) -> str:
    result: str = data.get("key")  # error: [union-attr], [assignment]
    return result

# After (推荐模式)
def process(data: dict | None) -> str:
    if data is None:
        return ""  # Early return
    result = data.get("key", "")
    return result
```

**TypeGuard 高级用法**：

```python
from typing import TypeGuard

def is_valid_config(data: dict | None) -> TypeGuard[dict]:
    return data is not None and "required_key" in data

def process(data: dict | None) -> str:
    if not is_valid_config(data):
        raise ValueError("Invalid config")
    # data 现在被收窄为 dict
    return data["required_key"]
```

---

### 5.3 `[arg-type]` 专项

**优先级**：P1 - 迭代 v1.1 目标

**常见场景**：

| 场景 | 错误原因 | 修复方式 |
|------|---------|---------|
| 传入 None | 参数声明不接受 None | 添加 None 检查或修改签名 |
| 子类型传递 | dict invariance | 改用 Mapping |
| 字面量类型 | 字符串字面量 vs str | 使用 Literal |

---

## 6. PR 检查清单

提交包含 mypy 修复的 PR 时，请确认：

```markdown
### mypy 修复检查清单

- [ ] 所有 `# type: ignore` 都带有 `[error-code]`
- [ ] 抑制注释包含说明（第三方库/TODO/issue 链接）
- [ ] 未使用裸 `# type: ignore`（无错误码）
- [ ] 修复数量符合迭代目标
- [ ] 未引入新的错误类型
- [ ] 核心模块（gateway/di.py 等）优先修复
```

---

## 7. 第三方库类型桩（Stubs）管理策略

### 7.1 当前第三方依赖类型支持情况

| 库 | 类型支持方式 | 来源 | 备注 |
|----|-------------|------|------|
| `psycopg` | 内置 py.typed | 官方 | 3.x 版本内置完整类型 |
| `yaml (PyYAML)` | types-PyYAML | typeshed | dev 依赖中配置 |
| `requests` | types-requests | typeshed | dev 依赖中配置 |
| `fastapi` | 内置 py.typed | 官方 | 完整类型支持 |
| `pydantic` | 内置 py.typed | 官方 | 完整类型支持 |
| `typer` | 内置 py.typed | 官方 | 完整类型支持 |
| `httpx` | 内置 py.typed | 官方 | 完整类型支持 |
| `boto3` | boto3-stubs | 社区 | dev 依赖中配置（[s3] extra） |

### 7.2 Strict Island 的 import 检查策略

Strict Island 模块启用 `ignore_missing_imports = false`，强制要求所有导入都有类型信息。

**配置位置**：`pyproject.toml` 的 `[[tool.mypy.overrides]]`

```toml
[[tool.mypy.overrides]]
module = "engram.gateway.di"
disallow_untyped_defs = true
disallow_incomplete_defs = true
ignore_missing_imports = false  # 强制要求类型信息
```

**当前 Strict Island 范围**（以 `pyproject.toml` 为准，运行下方命令查看）：

```bash
python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['tool']['engram']['mypy']['strict_island_paths']))"
```

当前已纳入（共 14 个路径）：
- Gateway: `di.py`, `container.py`, `services/`, `handlers/`, `policy.py`, `audit_event.py`
- Logbook: `config.py`, `uri.py`, `cursor.py`, `governance.py`, `outbox.py`, `db.py`, `views.py`, `artifact_gc.py`

### 7.3 添加新第三方库时的操作流程

1. **检查类型支持**：
   - 查看库是否包含 `py.typed` 文件（内置类型）
   - 搜索 typeshed 是否有对应 `types-XXX` 包
   - 检查 PyPI 上是否有社区维护的 stubs

2. **有类型支持**：
   - 如果是 `types-XXX` 包，添加到 `pyproject.toml` 的 dev 依赖
   - 如果内置 py.typed，无需额外操作

3. **无类型支持**：
   - 在 `pyproject.toml` 中添加精准豁免：
   ```toml
   [[tool.mypy.overrides]]
   module = "library_name.*"
   ignore_missing_imports = true
   ```
   - **禁止**在全局启用 `ignore_missing_imports = true`

### 7.4 精准豁免配置模板

当确实无法获得类型桩时，使用模块级豁免：

```toml
# --- 第三方库桩缺失时的精准豁免 ---
# 仅对确实无 stubs 的库启用，禁止全局豁免
[[tool.mypy.overrides]]
module = [
    "some_untyped_lib.*",
    "another_legacy_lib",
]
ignore_missing_imports = true
```

### 7.5 常用 stubs 包参考

| 库 | Stubs 包 | 安装命令 |
|----|---------|---------|
| PyYAML | types-PyYAML | `pip install types-PyYAML` |
| requests | types-requests | `pip install types-requests` |
| boto3 | boto3-stubs | `pip install boto3-stubs[s3]` |
| redis | types-redis | `pip install types-redis` |
| Pillow | types-Pillow | `pip install types-Pillow` |
| python-dateutil | types-python-dateutil | `pip install types-python-dateutil` |

---

## 8. 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: mypy 基线管理](../architecture/adr_mypy_baseline_and_gating.md) | 设计决策与迁移路线 |
| [ADR: Logbook Strict Island 扩展计划](../architecture/adr_logbook_strict_island_expansion_config_uri_db.md) | **Logbook 模块纳入计划、临时 ignore 策略、清零顺序** |
| [mypy 基线操作指南](./mypy_baseline.md) | 日常操作手册 |
| [PR 模板](../../.github/pull_request_template.md) | 包含 mypy 检查项 |
| [mypy 官方文档](https://mypy.readthedocs.io/en/stable/error_codes.html) | 错误码完整列表 |
| [mypy stubs 文档](https://mypy.readthedocs.io/en/stable/stubs.html) | 类型桩编写指南 |
| [typeshed](https://github.com/python/typeshed) | 官方类型桩仓库 |
| [Mapping vs dict](https://mypy.readthedocs.io/en/stable/common_issues.html#variance) | 协变/逆变说明 |
