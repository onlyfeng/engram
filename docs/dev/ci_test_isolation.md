# CI 测试隔离规范

> 本文档定义 `tests/ci/` 测试文件与 `scripts/ci/` 脚本之间的导入规范，确保测试隔离性，避免 `sys.path` 和 `sys.modules` 污染。

---

## 目录

- [1. 问题背景](#1-问题背景)
- [2. 允许的导入方式](#2-允许的导入方式)
- [3. 禁止的写法](#3-禁止的写法)
  - [3.5 过渡期规则](#35-过渡期规则)
- [4. ImportError 修复路径](#4-importerror-修复路径)
  - [4.3 违反规范时的修复路径](#43-违反规范时的修复路径)
- [5. 门禁检查](#5-门禁检查)
- [6. 相关文档](#6-相关文档)

---

## 1. 问题背景

### 1.1 隔离问题症状

当 `tests/ci/` 中的测试文件使用"双模式导入"（try/except 回退）时，会导致：

1. **sys.modules 污染**：顶层模块名被注册到全局 `sys.modules`
2. **sys.path 污染**：`scripts/ci` 被添加到 `sys.path`
3. **测试顺序敏感**：不同运行顺序下测试结果不一致

### 1.2 典型错误示例

```
Failed: Test '...test_xxx' has forbidden top-level CI modules in sys.modules:
  ['check_workflow_contract_docs_sync', 'workflow_contract_common']

These modules should be imported via 'scripts.ci.*' namespace, e.g.:
  from scripts.ci.validate_workflows import ...
NOT:
  import validate_workflows
```

### 1.3 根本原因

"双模式导入"模式会在 pytest 运行时触发 `sys.path` 修改：

```python
# ❌ 问题代码（双模式导入）
try:
    from .workflow_contract_common import discover_workflow_keys  # 相对导入
except ImportError:
    from workflow_contract_common import discover_workflow_keys   # ❌ 触发 sys.path 污染
```

详细调查报告参见 [ci_test_isolation_investigation.md](./ci_test_isolation_investigation.md)。

---

## 2. 允许的导入方式

### 2.1 唯一正确的导入方式

**核心约束**：

| 约束点 | 规则 | 说明 |
|--------|------|------|
| **唯一导入路径** | `scripts.ci.*` | 所有 CI 脚本必须通过此命名空间导入 |
| **唯一执行方式** | `python -m scripts.ci.<module>` 或从项目根目录运行 | 确保 `sys.path` 包含项目根目录 |
| **禁止顶层导入** | 不允许 `import validate_workflows` | 会污染 `sys.modules` |
| **禁止 sys.path 修改** | 模块顶层禁止 `sys.path.insert/append` | 影响所有测试 |

**在测试代码和脚本中，必须且只能使用 `scripts.ci.*` 命名空间导入**：

```python
# ✅ 正确：使用 scripts.ci.* 命名空间
from scripts.ci.workflow_contract_common import discover_workflow_keys
from scripts.ci.validate_workflows import validate_workflow_contract
from scripts.ci.check_noqa_policy import NoqaPolicyChecker

# ✅ 正确：导入整个模块
import scripts.ci.workflow_contract_common as wc_common
```

### 2.2 测试辅助模块导入

测试辅助模块应放在 `tests/ci/helpers/` 目录下：

```python
# ✅ 正确：从测试辅助目录导入
from tests.ci.helpers.workflow_fixtures import create_mock_workflow
```

### 2.3 正确导入示例

```python
# tests/ci/test_workflow_contract.py

# ✅ 推荐写法
from scripts.ci.validate_workflows import (
    validate_workflow_contract,
    WorkflowValidator,
)
from scripts.ci.workflow_contract_common import (
    discover_workflow_keys,
    load_contract,
)

def test_validate_workflow():
    validator = WorkflowValidator()
    result = validator.validate()
    assert result.passed
```

---

## 3. 禁止的写法

### 3.1 禁止：模块级 sys.path.insert

**禁止在模块顶层修改 `sys.path`**：

```python
# ❌ 禁止：模块级 sys.path 修改
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ci"))

from workflow_contract_common import discover_workflow_keys  # ❌ 顶层导入
```

**原因**：模块级 `sys.path` 修改在 pytest 收集测试时就会执行，影响所有后续测试。

### 3.2 禁止：双模式导入（try/except 回退）

**禁止使用相对导入回退到顶层导入**：

```python
# ❌ 禁止：双模式导入
try:
    from .workflow_contract_common import discover_workflow_keys
except ImportError:
    from workflow_contract_common import discover_workflow_keys  # ❌ 污染 sys.modules
```

**原因**：当以 `pytest` 运行时，相对导入会失败，触发顶层导入，污染 `sys.modules`。

### 3.3 禁止：顶层导入脚本同名模块

**禁止直接导入与 `scripts/ci/` 脚本同名的顶层模块**：

```python
# ❌ 禁止：直接导入脚本同名模块
import validate_workflows                    # ❌
import workflow_contract_common              # ❌
import check_noqa_policy                     # ❌
from check_workflow_contract_docs_sync import ...  # ❌
```

**正确写法**：

```python
# ✅ 正确：使用 scripts.ci.* 命名空间
from scripts.ci.validate_workflows import ...
from scripts.ci.workflow_contract_common import ...
from scripts.ci.check_noqa_policy import ...
from scripts.ci.check_workflow_contract_docs_sync import ...
from scripts.ci.check_workflow_contract_coupling_map_sync import ...
```

### 3.4 禁止写法汇总

| 禁止写法 | 原因 | 正确替代 |
|----------|------|----------|
| `sys.path.insert(0, ...)` 在模块顶层 | 污染所有测试的 sys.path | 使用 `scripts.ci.*` 导入 |
| `try: from .xxx except: from xxx` | 双模式导入污染 sys.modules | 只用 `from scripts.ci.xxx import ...` |
| `import validate_workflows` | 顶层模块污染 | `from scripts.ci.validate_workflows import ...` |
| `from workflow_contract_common import ...` | 顶层模块污染 | `from scripts.ci.workflow_contract_common import ...` |

---

## 3.5 过渡期规则

> 本节定义从旧写法迁移到规范写法的过渡期策略。

### 3.5.1 过渡期时间线

| 阶段 | 时间 | 行为 | 说明 |
|------|------|------|------|
| **Phase 0（当前）** | 立即生效 | 门禁阻断 | 静态检查发现违规时 CI 失败 |
| **Phase 1** | 下一迭代 | 运行时检查 | conftest.py fixture 检测 `sys.modules` 污染 |

### 3.5.2 已有代码的迁移要求

| 代码类型 | 迁移要求 | 截止时间 |
|----------|----------|----------|
| `tests/ci/*.py` | 必须完成迁移 | 立即 |
| `scripts/ci/*.py` | 必须完成迁移 | 立即 |
| 其他测试文件 | 建议迁移 | 无强制截止 |

### 3.5.3 迁移豁免机制

当前**不提供豁免机制**。所有 `tests/ci/` 和 `scripts/ci/` 文件必须遵守规范。

如有特殊情况无法迁移，需：
1. 创建 GitHub Issue 说明原因
2. 在 PR 中添加 `ci-isolation-exception` 标签
3. 获得 Tech Lead 审批

---

## 4. ImportError 修复路径

### 4.1 遇到 ImportError 的诊断流程

当导入 CI 脚本时遇到 `ImportError`，按以下步骤排查：

```bash
# 1. 确认从项目根目录运行
cd /path/to/engram
pwd  # 应显示项目根目录

# 2. 确认 scripts/ci/ 目录存在
ls scripts/ci/

# 3. 确认目标模块存在
ls scripts/ci/workflow_contract_common.py

# 4. 使用正确的导入方式测试
python -c "from scripts.ci.workflow_contract_common import discover_workflow_keys; print('OK')"
```

### 4.2 常见 ImportError 场景与修复

#### 场景 1：直接运行脚本时报错

**症状**：

```bash
python scripts/ci/check_workflow_contract_docs_sync.py
# ImportError: attempted relative import with no known parent package
```

**修复**：使用 `-m` 参数运行或从项目根目录运行：

```bash
# ✅ 方法 1：使用 -m 参数
python -m scripts.ci.check_workflow_contract_docs_sync

# ✅ 方法 2：确保从项目根目录运行
cd /path/to/engram
python scripts/ci/check_workflow_contract_docs_sync.py
```

#### 场景 2：脚本需要同时支持直接运行和被导入

**修复**：在 `if __name__ == "__main__"` 块中处理路径，而非模块顶层：

```python
# ✅ 正确：仅在直接运行时修改路径
def main():
    # 业务逻辑
    pass

if __name__ == "__main__":
    # 仅在直接运行时添加路径
    import sys
    from pathlib import Path
    
    project_root = Path(__file__).parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    
    # 然后使用正确的导入
    from scripts.ci.workflow_contract_common import discover_workflow_keys
    
    main()
```

#### 场景 3：测试文件导入报错

**症状**：

```bash
pytest tests/ci/test_workflow_contract.py
# ModuleNotFoundError: No module named 'scripts'
```

**修复**：

1. 确认从项目根目录运行 pytest
2. 确认 `pyproject.toml` 或 `pytest.ini` 配置了正确的 `pythonpath`

```toml
# pyproject.toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

### 4.3 违反规范时的修复路径

当门禁检查失败或测试报告隔离问题时，按以下步骤定位和修复：

#### 4.3.1 定位问题文件

```bash
# 1. 运行门禁检查，获取违规详情
python -m scripts.ci.check_ci_test_isolation --verbose

# 输出示例：
# [ERROR] 发现违规导入:
#   tests/ci/test_workflow_contract.py:5: from workflow_contract_common import ...
#   tests/ci/test_validate_workflows.py:3: import validate_workflows

# 2. 手动搜索问题导入（补充）
grep -rn "^from \w\+ import" tests/ci/*.py | grep -v "scripts.ci\|tests.ci"
grep -rn "^import \w\+$" scripts/ci/*.py | grep -v "^import sys\|^import os\|^import re"
```

#### 4.3.2 脚本迁移修复步骤

如果现有脚本使用了禁止的导入模式，按以下步骤迁移：

```bash
# 1. 识别问题导入
grep -rn "^from \w\+ import" scripts/ci/*.py | grep -v "scripts.ci"
grep -rn "^import \w\+$" scripts/ci/*.py | grep -v "^import sys\|^import os"

# 2. 替换为正确的导入
# 将：from workflow_contract_common import xxx
# 改为：from scripts.ci.workflow_contract_common import xxx

# 3. 清除缓存并测试
rm -rf scripts/ci/__pycache__ tests/ci/__pycache__
pytest tests/ci/ -q

# 4. 运行隔离检查
make check-ci-test-isolation
```

#### 4.3.3 运行时隔离错误修复

当 pytest 运行时报告 `sys.modules` 污染：

```bash
# 错误示例
# Failed: Test '...test_xxx' has forbidden top-level CI modules in sys.modules:
#   ['check_workflow_contract_docs_sync', 'workflow_contract_common']

# 修复步骤：
# 1. 定位问题测试文件
grep -l "workflow_contract_common" tests/ci/*.py

# 2. 检查该文件的导入语句
head -30 tests/ci/test_xxx.py

# 3. 修改为正确的导入方式
# 错误: from workflow_contract_common import discover_workflow_keys
# 正确: from scripts.ci.workflow_contract_common import discover_workflow_keys

# 4. 清除缓存并重新测试
rm -rf tests/ci/__pycache__
pytest tests/ci/test_xxx.py -v
```

### 4.4 修复前后对照

| 修复前（❌） | 修复后（✅） |
|-------------|-------------|
| `from workflow_contract_common import discover_workflow_keys` | `from scripts.ci.workflow_contract_common import discover_workflow_keys` |
| `import validate_workflows` | `from scripts.ci import validate_workflows` 或 `import scripts.ci.validate_workflows as validate_workflows` |
| `sys.path.insert(0, "scripts/ci")` 在模块顶层 | 移到 `if __name__ == "__main__":` 块内，或完全移除 |

---

## 5. 门禁检查

### 5.1 本地检查命令

```bash
# 运行 CI 测试隔离检查
make check-ci-test-isolation

# 或直接调用脚本
python -m scripts.ci.check_ci_test_isolation --verbose
```

### 5.2 检查内容

| 检查项 | 说明 | 失败时 |
|--------|------|--------|
| 模块级 sys.path 修改 | 检测顶层 `sys.path.insert` / `sys.path.append` | CI 失败 |
| 顶层 CI 模块导入 | 检测直接导入 `scripts/ci/` 下模块名 | CI 失败 |
| 双模式导入模式 | 检测 try/except 导入回退 | CI 失败 |

### 5.3 conftest.py 隔离机制

`tests/ci/conftest.py` 包含运行时隔离检查：

```python
# FORBIDDEN_TOPLEVEL_MODULES 列表定义了禁止的顶层模块名
FORBIDDEN_TOPLEVEL_MODULES = {
    'validate_workflows',
    'check_workflow_contract_docs_sync',
    'workflow_contract_common',
    # ...
}

# _func_sysmodules_guard fixture 在测试 teardown 时检查 sys.modules 污染
# _func_syspath_guard fixture 在测试 teardown 时检查 sys.path 污染
```

### 5.4 CI 集成

该检查已集成到 CI 流程中：

- **CI Job**: `lint`
- **CI Step**: `Check CI test isolation`
- **Makefile 目标**: `check-ci-test-isolation`

---

## 6. 相关文档

| 文档 | 说明 |
|------|------|
| [CI 测试隔离调查报告](./ci_test_isolation_investigation.md) | 问题根因分析与诊断详情 |
| [CI 门禁 Runbook](./ci_gate_runbook.md) | 所有 CI 门禁的配置与操作指南 |
| [Agent 协作指南](./agents.md) | AI Agent 开发规范 |
| `tests/ci/conftest.py` | 测试隔离 fixture 实现 |
| `scripts/ci/check_ci_test_isolation.py` | 静态检查脚本 |

---

> 更新时间：2026-02-02（添加核心约束表、过渡期规则、违反时修复路径）
