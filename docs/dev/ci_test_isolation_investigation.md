# CI 测试隔离调查报告

> **调查日期**: 2026-02-02
> **调查范围**: `tests/ci/test_workflow_contract*.py` 相关测试隔离问题

---

## 1. 调查背景

在运行 CI 测试时发现 `test_workflow_contract_doc_anchors.py` 在不同运行顺序下表现不一致，怀疑存在测试隔离问题。

---

## 2. 复现步骤

### 2.1 独立运行各测试文件

```bash
# 清除 pyc 缓存（重要！）
rm -rf scripts/ci/__pycache__ tests/ci/__pycache__

# 独立运行各测试文件
pytest -q tests/ci/test_workflow_contract_docs_sync.py      # 31 passed
pytest -q tests/ci/test_workflow_contract.py                # 144 passed, 2 skipped
pytest -q tests/ci/test_workflow_contract_doc_anchors.py    # 30 passed (清除缓存后)
pytest -q tests/ci/                                          # 全部测试
```

### 2.2 不同顺序组合运行

```bash
# 顺序 1: docs_sync -> contract -> doc_anchors
pytest -q tests/ci/test_workflow_contract_docs_sync.py \
         tests/ci/test_workflow_contract.py \
         tests/ci/test_workflow_contract_doc_anchors.py
# 结果: 205 passed, 2 skipped, 138 errors (sys.path/sys.modules 污染)

# 顺序 2: doc_anchors -> contract -> docs_sync
pytest -q tests/ci/test_workflow_contract_doc_anchors.py \
         tests/ci/test_workflow_contract.py \
         tests/ci/test_workflow_contract_docs_sync.py
# 结果: 类似的 errors

# 整个目录
pytest -q tests/ci/
# 结果: 844 passed, 3 skipped, 265 errors
```

---

## 3. 诊断信息

### 3.1 sys.path 检查

```python
# sys.path[0:5] 典型输出:
# 0:                                          (空字符串 = 当前目录)
# 1: /Users/.../versions/3.13.2/lib/python313.zip
# 2: /Users/.../versions/3.13.2/lib/python3.13
# 3: /Users/.../versions/3.13.2/lib/python3.13/lib-dynload
# 4: /Users/.../versions/3.13.2/lib/python3.13/site-packages
```

### 3.2 sys.modules 污染检测

conftest.py 中的 `_func_sysmodules_guard` fixture 检测到以下被污染的模块:

```python
['check_workflow_contract_docs_sync', 'workflow_contract_common']
```

### 3.3 sys.path 污染检测

conftest.py 中的 `_func_syspath_guard` fixture 检测到以下被添加的路径:

```
['/Users/a4399/Documents/ai/onlyfeng/engram/scripts/ci']
```

### 3.4 关键错误日志

**teardown 阶段的隔离检查失败**:
```
Failed: Test '...test_missing_job_id_in_doc' has forbidden top-level CI modules in sys.modules:
  ['check_workflow_contract_docs_sync', 'workflow_contract_common']

These modules should be imported via 'scripts.ci.*' namespace, e.g.:
  from scripts.ci.validate_workflows import ...
NOT:
  import validate_workflows
```

**清除缓存前（旧 .pyc 存在时）**:
```
NameError: name 'REQUIRED_ANCHORS' is not defined
scripts/ci/check_workflow_contract_doc_anchors.py:328
```

---

## 4. 根因分析

### 4.1 发现的问题

| 问题类型 | 描述 | 严重性 |
|----------|------|--------|
| **双模式导入模式** | `check_workflow_contract_docs_sync.py` 使用 try/except 导入回退，导致顶层模块污染 | 🔴 高 |
| **pyc 缓存污染** | 旧版本 .pyc 文件与新 .py 源码不一致，导致 `NameError` | 🟡 中 |

### 4.2 详细分析

#### 问题 1: 双模式导入模式（根本原因）

**位置**: `scripts/ci/check_workflow_contract_docs_sync.py` 第 34-38 行

```python
# Dual-mode import: prefer relative import (for python -m), fallback to top-level (for direct run)
try:
    from .workflow_contract_common import discover_workflow_keys
except ImportError:
    from workflow_contract_common import discover_workflow_keys  # ❌ 污染 sys.modules
```

**问题机制**:
1. 当以 `python scripts/ci/check_workflow_contract_docs_sync.py` 直接运行时，相对导入 `from .workflow_contract_common` 会失败
2. 回退到 `from workflow_contract_common import ...` 时，会将 `scripts/ci` 添加到 `sys.path`
3. 这导致 `workflow_contract_common` 作为顶层模块被注册到 `sys.modules`
4. 后续测试运行时，conftest.py 的隔离检查会检测到这些污染

**影响范围**:
- 所有在 `check_workflow_contract_docs_sync.py` 之后运行的测试
- 所有使用了类似双模式导入模式的脚本

#### 问题 2: pyc 缓存污染（次要问题）

- 旧版代码使用全局常量 `REQUIRED_ANCHORS`
- 新版代码改为实例属性 `self.required_anchors`
- 当旧 .pyc 被加载时，引用未定义的 `REQUIRED_ANCHORS` 导致 `NameError`

### 4.3 conftest.py 隔离机制

```python
# tests/ci/conftest.py 中的 FORBIDDEN_TOPLEVEL_MODULES
FORBIDDEN_TOPLEVEL_MODULES = {
    'validate_workflows',
    'check_workflow_contract_docs_sync',
    'workflow_contract_common',
    # ...
}
```

conftest.py 正确地检测到了问题，但问题的根源在于脚本本身的导入模式。

---

## 5. 结论

### 5.1 问题性质

**这是一个真正的测试隔离问题，由"双模式导入"模式引起：**

1. 🔴 **sys.modules 污染**: 顶层模块名被注册到全局 `sys.modules`
2. 🔴 **sys.path 污染**: `scripts/ci` 被添加到 `sys.path`
3. 🟡 **pyc 缓存污染**: 旧版缓存与新代码不一致

### 5.2 受影响的测试

当 `test_workflow_contract_docs_sync.py` 先于其他测试运行时，所有后续测试的 teardown 都会因 conftest.py 的隔离检查而失败：

| 触发源 | 受影响测试 | 错误类型 |
|--------|-----------|----------|
| `test_workflow_contract_docs_sync.py` | 所有后续 `test_workflow_contract*.py` 测试 | teardown ERROR |

### 5.3 修复建议

1. **移除双模式导入**（推荐）:
   ```python
   # 改用单一导入方式
   from scripts.ci.workflow_contract_common import discover_workflow_keys
   ```

2. **如需保留直接运行支持**，使用 `if __name__ == "__main__"` 块中的 `sys.path` 修改：
   ```python
   if __name__ == "__main__":
       import sys
       from pathlib import Path
       sys.path.insert(0, str(Path(__file__).parent.parent.parent))
       # 然后使用 from scripts.ci.xxx import ...
   ```

3. **CI 防护**: 在 CI 中添加 `--cache-clear` 或 `PYTHONDONTWRITEBYTECODE=1`

---

## 6. 后续行动

- [ ] 修复 `scripts/ci/check_workflow_contract_docs_sync.py` 的导入模式
- [ ] 检查其他脚本是否有类似的双模式导入问题
- [ ] 清除所有 `__pycache__` 目录后重新运行测试
- [ ] 将此发现添加到编码规范文档

---

## 7. 相关文件

| 文件 | 说明 |
|------|------|
| `scripts/ci/check_workflow_contract_docs_sync.py` | 文档同步检查脚本（包含问题导入模式） |
| `scripts/ci/workflow_contract_common.py` | 被污染的公共模块 |
| `tests/ci/conftest.py` | 包含隔离检查的 fixture |
| `tests/ci/test_workflow_contract_docs_sync.py` | 触发污染的测试文件 |
| `scripts/ci/check_workflow_contract_doc_anchors.py` | 锚点检查脚本（新增，未跟踪） |
| `tests/ci/test_workflow_contract_doc_anchors.py` | 锚点检查测试（新增，未跟踪） |

---

*文档创建: 2026-02-02 | 调查完成*
*更新: 发现真正的测试隔离问题 - 双模式导入模式污染*
