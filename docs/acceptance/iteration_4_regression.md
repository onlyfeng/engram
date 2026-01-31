# Iteration 4 Regression - 代码质量修复记录

## 执行日期
2026-01-31

## 任务概述
执行 `make format`、`make lint`、`make typecheck` 并修复发现的问题。

## 修复统计

### 1. Format Check
- **状态**: ✅ 通过
- **修复文件数**: 172 files reformatted (首次), 13 files reformatted (后续格式化)

### 2. Lint Check (`make lint`)
- **状态**: ✅ 通过
- **初始错误数**: 2074 errors
- **最终错误数**: 0 errors
- **修复方式**:
  - 自动修复 (`ruff check --fix`): 1528 errors
  - 自动修复 (`ruff check --fix --unsafe-fixes`): 追加修复
  - 手动修复: 约 50 errors (import 顺序, 未使用导入, 模糊变量名等)
  - pyproject.toml 配置忽略测试文件的 E402

#### 主要手动修复内容:
1. **未使用导入 (F401)**:
   - `src/engram/gateway/__init__.py`: 使用显式重导出 `as` 语法
   - `src/engram/gateway/evidence_store.py`: 移除未使用的 `ArtifactWriteError`, `get_connection`
   - `src/engram/gateway/logbook_adapter.py`: 移除未使用的 `get_config`
   - `src/engram/gateway/main.py`: 改为仅检查模块可用性
   - 测试文件: 添加 `# noqa: F401` 注释处理故意的导入检查

2. **类型比较 (E721)**:
   - `src/engram/logbook/config.py`: `value_type == float` → `value_type is float`
   - `src/engram/logbook/scm_sync_policy.py`: 同上

3. **模糊变量名 (E741)**:
   - `tests/logbook/test_render_views.py`: `l` → `ln`
   - `tests/logbook/test_scm_sync_integration.py`: `l` → `lock`
   - `tests/logbook/test_scm_sync_reaper.py`: `l` → `lk`

4. **重定义 (F811)**:
   - `tests/gateway/test_error_codes.py`: 移除重复的 `GatewayDeps` 导入

5. **pyproject.toml 配置**:
   ```toml
   [tool.ruff.lint.per-file-ignores]
   "tests/**/*.py" = ["E402"]  # 允许测试文件延迟导入
   "src/engram/logbook/db.py" = ["E402"]
   "src/engram/logbook/scm_auth.py" = ["E402"]
   ```

### 3. Type Check (`make typecheck`)
- **状态**: ⚠️ 部分通过
- **初始错误数**: 289 errors
- **当前错误数**: 263 errors
- **修复数量**: 26 errors

#### 已修复的类型错误:
1. **Implicit Optional (PEP 484)**:
   - `src/engram/logbook/errors.py:58`: `status_code: int = None` → `status_code: Optional[int] = None`
   - `src/engram/gateway/logbook_adapter.py:158-174`: 添加 `Optional` 类型注解
   - `src/engram/logbook/config.py:1795`: `details: dict = None` → `details: Optional[dict] = None`

2. **类型断言**:
   - `src/engram/gateway/config.py:163-165`: 添加 `assert` 断言帮助 mypy 理解控制流

3. **可选依赖类型忽略**:
   - `src/engram/gateway/mcp_rpc.py:68-80`: 添加 `# type: ignore[misc, assignment]`
   - `src/engram/gateway/logbook_db.py:85-87,119`: 添加 `# type: ignore` 注释

4. **缺失字段声明**:
   - `src/engram/gateway/container.py:74`: 添加 `_deps_cache` 字段类型声明

5. **函数参数修复**:
   - `src/engram/gateway/main.py:131`: 添加缺失的 `deps` 参数

#### 剩余类型错误分析 (263 errors):
| 错误类型 | 数量 | 说明 |
|---------|------|------|
| `no-any-return` | ~50 | 函数返回 Any 类型 |
| `arg-type` | ~40 | 参数类型不匹配 |
| `assignment` | ~30 | 赋值类型不兼容 |
| `import-untyped` | ~10 | 缺少类型桩 (boto3, botocore, requests) |
| `union-attr` | ~10 | 访问 Optional 类型的属性 |
| `index` | ~10 | 索引 None 类型 |
| `call-arg` | ~15 | 调用参数错误 |
| 其他 | ~98 | misc, no-redef, 等 |

### 4. 依赖更新
- **pyproject.toml** 添加类型桩:
  ```toml
  dev = [
      ...
      "types-requests>=2.28.0",
      "boto3-stubs[s3]>=1.28.0",
  ]
  ```

## 后续建议

### 高优先级 (CI Blocker)
1. 修复 `src/engram/gateway/handlers/memory_store.py` 中的 `str | None` 参数传递问题
2. 修复 `src/engram/gateway/app.py` 中的类型不匹配问题
3. 修复 `src/engram/gateway/mcp_rpc.py` 中的 JsonRpcResponse 构造问题

### 中优先级
1. 添加类型桩 `boto3-stubs` 和 `types-requests` 并安装
2. 修复 `no-any-return` 错误（添加类型标注或 cast）
3. 修复 `src/engram/logbook/` 中的类型错误

### 低优先级
1. 重构以减少 `# type: ignore` 注释
2. 为第三方模块创建 stub 文件
3. 启用 mypy 的 `--strict` 模式

## 验证命令
```bash
make format      # ✅ 通过
make lint        # ✅ 通过  
make typecheck   # ⚠️ 当前 263 errors (需要后续迭代修复)
```

## 修复的文件清单

### Gateway 目录 (`src/engram/gateway/`)
- `__init__.py` - 显式重导出
- `app.py` - 移除未使用导入
- `config.py` - 类型断言
- `container.py` - 添加 `_deps_cache` 字段
- `evidence_store.py` - 移除未使用导入
- `logbook_adapter.py` - Optional 类型修复
- `logbook_db.py` - type: ignore 注释
- `main.py` - 添加缺失参数
- `mcp_rpc.py` - type: ignore 注释
- `handlers/evidence_upload.py` - 移除未使用导入

### Logbook 目录 (`src/engram/logbook/`)
- `config.py` - import 顺序, Optional 类型, 类型比较
- `errors.py` - Optional 类型
- `scm_sync_policy.py` - 类型比较

### 测试目录 (`tests/`)
- 多个测试文件添加 `# noqa: F401` 注释
- 变量重命名 (`l` → `ln`, `lock`, `lk`)
- 移除重复导入

### 配置文件
- `pyproject.toml` - 添加类型桩依赖和 per-file-ignores
