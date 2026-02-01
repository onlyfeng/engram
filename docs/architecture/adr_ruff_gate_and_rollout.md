# ADR: Ruff Gate 门禁与 Rollout 分阶段策略

> 状态: **已批准**  
> 创建日期: 2026-02-01  
> 决策者: Engram Core Team

---

## 1. 背景

项目当前使用 ruff 进行代码风格检查，但仅启用了基础规则集（E/F/I/W）。为提升代码质量，计划渐进式引入更多规则（如 B/UP/SIM/PERF 等），同时避免一次性全仓启用导致大量历史代码报错、阻塞开发。

本 ADR 定义：
- **Ruff Phases**: 规则集分阶段启用策略
- **例外机制**: 统一 `# noqa` 注释规范
- **Rollout 节奏**: 迭代收紧计划与验收命令

---

## 2. Ruff Phases 定义

### 2.1 四阶段规则集

| Phase | 规则集 | 作用范围 | 说明 |
|-------|--------|----------|------|
| **P0** | `E`, `F`, `I`, `W` | 全仓强制 | **当前状态**，基础错误与警告 |
| **P1** | `B`, `UP`, `SIM`, `PERF`, `PTH` | 仅 Lint-Island 强制 | 代码健壮性、现代化、简化、性能 |
| **P2** | P0 + P1 | 全仓强制 | 全仓启用 P1 规则 |
| **P3** | - | 全仓 | 清理旧 `# noqa` 注释与冗余豁免 |

### 2.2 规则集详解

#### P0 规则（当前已启用）

| 前缀 | 名称 | 说明 |
|------|------|------|
| `E` | pycodestyle errors | 代码风格错误 |
| `F` | Pyflakes | 逻辑错误（未使用变量、未定义等） |
| `I` | isort | 导入排序 |
| `W` | pycodestyle warnings | 代码风格警告 |

#### P1 规则（待引入）

| 前缀 | 名称 | 说明 | 示例规则 |
|------|------|------|----------|
| `B` | flake8-bugbear | 潜在 bug 与设计问题 | B006（可变默认参数）, B008（函数调用作为默认值） |
| `UP` | pyupgrade | Python 版本升级建议 | UP006（使用 `list` 而非 `List`）, UP035（废弃导入） |
| `SIM` | flake8-simplify | 代码简化建议 | SIM102（嵌套 if 合并）, SIM108（三元表达式） |
| `PERF` | Perflint | 性能优化建议 | PERF102（使用 `dict.get`）, PERF401（列表推导优化） |
| `PTH` | flake8-use-pathlib | 使用 pathlib 替代 os.path | PTH110（`os.path.exists` → `Path.exists`） |

#### 可选扩展规则（按需引入）

| 前缀 | 名称 | 说明 | 引入条件 |
|------|------|------|----------|
| `C4` | flake8-comprehensions | 推导式优化 | P2 完成后 |
| `RUF` | Ruff-specific | Ruff 自定义规则 | P2 完成后 |
| `PLR` | Pylint Refactor | 重构建议 | 视团队需求 |
| `ANN` | flake8-annotations | 类型注解检查 | 配合 mypy strict |

### 2.3 Lint-Island 定义

> **Lint-Island**（Lint 岛屿）是一组代码质量较高、可率先启用 P1 规则的模块。

#### 配置位置

> **SSOT**: 以 `pyproject.toml` 的 `[tool.engram.ruff].lint_island_paths` 为准。

**初始 Lint-Island 模块**：

```toml
# pyproject.toml
[tool.engram.ruff]
lint_island_paths = [
    # Gateway 核心模块
    "src/engram/gateway/di.py",
    "src/engram/gateway/container.py",
    "src/engram/gateway/services/",
    # Logbook 核心模块
    "src/engram/logbook/config.py",
    "src/engram/logbook/uri.py",
]
```

**扩展原则**：
- 与 mypy Strict-Island 保持一致
- 新增模块需先清理 P1 规则违规
- 逐步扩展直至全仓覆盖

---

## 3. 例外机制

### 3.1 核心原则

> **禁止裸 `# noqa`**：所有抑制注释必须指明具体规则码。

#### 合法格式

```python
# ✅ 正确：指明具体规则码
result = eval(user_input)  # noqa: S307

# ✅ 正确：多个规则码
from typing import List  # noqa: UP006, UP035

# ✅ 正确：行内注释 + noqa
x = 1  # important constant  # noqa: E501
```

#### 禁止格式

```python
# ❌ 错误：裸 noqa（不指明规则）
result = eval(user_input)  # noqa

# ❌ 错误：通配符
result = eval(user_input)  # noqa: *
```

### 3.2 检查脚本

CI 使用 `scripts/ci/check_noqa_policy.py` 检查 noqa 注释规范：

```bash
# 检查是否存在裸 noqa（默认模式）
python scripts/ci/check_noqa_policy.py --verbose

# 或使用 make 目标
make check-noqa-policy

# lint-island 严格模式（额外要求原因说明）
make check-noqa-island
```

**检查逻辑**：
- 正则匹配 `# noqa(?!:)` 或 `# noqa$` 检测裸 noqa
- 发现裸 noqa 时返回非零退出码
- 输出违规文件与行号
- 可选：`--lint-island-strict` 模式对 lint-island 路径要求原因说明

### 3.3 per-file-ignores 规范

使用 `per-file-ignores` 豁免特定文件/目录的规则：

```toml
[tool.ruff.lint.per-file-ignores]
# 测试文件允许使用 assert（S101）
"tests/**/*.py" = ["S101"]
# 迁移脚本允许长行
"sql/*.py" = ["E501"]
# 历史模块暂时豁免 P1 规则
"src/engram/legacy/*.py" = ["B", "UP", "SIM", "PERF", "PTH"]
```

**约束**：
- 每条豁免必须附注释说明原因
- 定期审查豁免列表，清理不再需要的条目
- 禁止使用通配符 `"**/*.py" = ["ALL"]`

### 3.4 noqa 使用审批

| 规则类别 | 自行添加 | 需审批 |
|----------|----------|--------|
| E501（行长度） | ✅ | - |
| I001（导入顺序） | ✅ | - |
| B/UP/SIM 单次豁免 | ✅ | - |
| 安全相关（S 系列） | ⚠️ 需注释理由 | 建议审批 |
| 批量豁免（per-file-ignores 新增） | ❌ | **必须审批** |

---

## 4. Rollout 节奏

### 4.1 迭代收紧计划

| 迭代 | 变更内容 | 验收命令 | 验收标准 |
|------|----------|----------|----------|
| **v1.0** | P0 全仓启用（现状） | `make lint` | 退出码 0 |
| **v1.1** | 新增 Lint-Island + P1 规则 | `make ruff-lint-island` | Lint-Island 无 P1 违规 |
| **v1.2** | 扩展 Lint-Island 至 gateway/ | `make ruff-lint-island` | gateway/ 全部纳入 |
| **v1.3** | 扩展 Lint-Island 至 logbook/ | `make ruff-lint-island` | logbook/ 核心模块纳入 |
| **v2.0** | P2：P1 规则全仓强制 | `make lint` | 全仓无 P1 违规 |
| **v2.1** | P3：清理旧 noqa | `ruff check . --extend-select RUF100` | 无冗余 noqa |

### 4.2 验收命令详解

#### make lint（P0 全仓检查）

```bash
# 当前配置
ruff check src/ tests/
```

#### make ruff-lint-island（P1 岛屿检查）

```bash
# 使用脚本检查 Lint-Island 模块
python scripts/ci/check_ruff_lint_island.py --verbose

# 或使用 Make 目标
make ruff-lint-island
```

脚本从 `pyproject.toml` 的 `[tool.engram.ruff].lint_island_paths` 读取模块列表，
并使用 `p1_rules` 中定义的扩展规则进行检查。

#### P1 全仓预览（手动执行）

```bash
# 预览全仓 P1 违规（不阻断）
ruff check . --extend-select B,UP,SIM,PERF,PTH --exit-zero
```

#### P3 noqa 清理验证（手动执行）

```bash
# 检查冗余 noqa（已修复但未移除）
ruff check . --extend-select RUF100
```

### 4.3 迭代验收检查清单

每迭代完成后，确认以下检查通过：

```markdown
## 迭代验收 Checklist

- [ ] `make lint` 通过（P0 全仓）
- [ ] `make ruff-lint-island` 通过（P1 岛屿）
- [ ] `make check-noqa-policy` 通过（无裸 noqa）
- [ ] `make format-check` 通过（格式一致）
- [ ] pyproject.toml 配置已更新
- [ ] 本 ADR 迭代状态已更新
```

---

## 5. pyproject.toml 配置结构

### 5.1 当前配置（P0）

```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "W"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["E402"]
"src/engram/logbook/db.py" = ["E402"]
"src/engram/logbook/scm_auth.py" = ["E402"]
```

### 5.2 P1 配置（岛屿启用）

```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "W"]
ignore = ["E501"]

# P1 扩展规则（用于 lint-island 检查）
# 注意：extend-select 会与 select 合并
# 全仓启用时取消注释以下行
# extend-select = ["B", "UP", "SIM", "PERF", "PTH"]

[tool.ruff.lint.per-file-ignores]
# 测试文件
"tests/**/*.py" = ["E402"]
# 历史原因延迟导入
"src/engram/logbook/db.py" = ["E402"]
"src/engram/logbook/scm_auth.py" = ["E402"]
# P1 过渡期豁免（待逐步清理）
# "src/engram/legacy/*.py" = ["B", "UP", "SIM", "PERF", "PTH"]

# ============================================================================
# Engram Ruff 扩展配置
# ============================================================================
[tool.engram.ruff]
# Lint-Island 模块列表
# 这些模块需要通过 P1 规则检查
# 详见: docs/architecture/adr_ruff_gate_and_rollout.md §2.3
lint_island_paths = [
    # Gateway 核心模块
    "src/engram/gateway/di.py",
    "src/engram/gateway/container.py",
    "src/engram/gateway/services/",
    # Logbook 核心模块
    "src/engram/logbook/config.py",
    "src/engram/logbook/uri.py",
]

# 当前 Phase（用于 CI 脚本判断）
# 0=P0, 1=P1-island, 2=P2-全仓, 3=P3-清理
current_phase = 0

# P1 扩展规则列表
p1_rules = ["B", "UP", "SIM", "PERF", "PTH"]
```

### 5.3 P2 配置（全仓启用）

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "W"]
extend-select = ["B", "UP", "SIM", "PERF", "PTH"]  # 取消注释，全仓启用
ignore = ["E501"]
```

---

## 6. CI 集成

### 6.1 当前 CI 配置

当前 CI (`lint` job) 中的 ruff 相关检查：

```yaml
# .github/workflows/ci.yml
jobs:
  lint:
    steps:
      - name: Run ruff check (lint)
        run: ruff check src/ tests/

      - name: Run ruff format check
        run: ruff format --check src/ tests/

      - name: Collect ruff metrics
        run: |
          python scripts/ci/ruff_metrics.py \
            --output artifacts/ruff_metrics.json \
            --verbose
        continue-on-error: true

      - name: Check noqa policy
        run: python scripts/ci/check_noqa_policy.py --verbose
```

### 6.2 本地门禁命令

| Make 目标 | 说明 | 对应 CI Step |
|-----------|------|--------------|
| `make lint` | P0 全仓 ruff check | "Run ruff check (lint)" |
| `make format-check` | 格式检查 | "Run ruff format check" |
| `make ruff-metrics` | 收集 ruff 指标 | "Collect ruff metrics" |
| `make check-noqa-policy` | noqa 策略检查 | "Check noqa policy" |
| `make ruff-lint-island` | P1 岛屿检查 | **待接入 CI** |
| `make ruff-gate` | ruff 门禁（current 模式） | - |

### 6.3 配置位置（SSOT）

| 配置项 | 位置 |
|--------|------|
| lint-island 路径 | `pyproject.toml [tool.engram.ruff].lint_island_paths` |
| P1 扩展规则 | `pyproject.toml [tool.engram.ruff].p1_rules` |
| 当前 Phase | `pyproject.toml [tool.engram.ruff].current_phase`（默认值） |
| ruff 基础规则 | `pyproject.toml [tool.ruff.lint].select` |
| CI Phase 注入 | GitHub Repository Variables: `ENGRAM_RUFF_PHASE` |
| future-baseline 文件 | `scripts/ci/ruff_baseline_future.json` |

### 6.4 future-baseline 模式定位

> **重要**：`future-baseline` 模式**不接入 CI 门禁**，仅用于手动预演和规划。

#### 设计定位

| 门禁模式 | CI 集成 | 用途 |
|----------|---------|------|
| `current` | ✅ 已集成 | 日常门禁，严格阻断任何 violation |
| `future-baseline` | ❌ 仅本地 | 预演新规则影响范围，生成/更新 baseline |

#### 为何不接入 CI

1. **已有渐进机制**：`ruff-lint-island` + Phase 机制已覆盖逐步启用新规则的需求
2. **定位不同**：future-baseline 用于预演评估，而非日常门禁
3. **避免复杂度**：维护两套 baseline（mypy + ruff）会增加 CI 复杂度
4. **Phase 推进足够**：Phase 1→2→3 的推进流程已提供明确的规则启用路径

#### 使用场景

| 场景 | 命令 | 说明 |
|------|------|------|
| 预演新规则影响 | `make ruff-gate-future` | 查看当前代码对 future-baseline 规则的违规情况 |
| 生成新 baseline | `make ruff-baseline-update RULES=B,UP` | 为指定规则生成 baseline 快照 |
| 评估 Phase 推进可行性 | `make ruff-gate-future` | 判断是否可以将规则从 future 移至 current |

#### 典型工作流

```bash
# 1. 评估新规则（如 B, UP）的影响范围
python scripts/ci/check_ruff_gate.py --gate future-baseline --rules B,UP --verbose

# 2. 生成 baseline（记录当前违规）
make ruff-baseline-update RULES=B,UP

# 3. 后续开发中定期检查（手动，不阻断）
make ruff-gate-future

# 4. 当 future-baseline violation 清零后，推进到 lint-island 或全仓启用
# 更新 pyproject.toml [tool.engram.ruff].lint_island_paths 或 current_phase
```

#### 文件说明

| 文件 | 用途 |
|------|------|
| `scripts/ci/ruff_baseline_future.json` | 存储 future 规则的已知违规快照 |
| `scripts/ci/check_ruff_gate.py` | 门禁脚本，支持 `--gate future-baseline` |

> **维护建议**：`ruff_baseline_future.json` 仅在规划 Phase 推进时更新，无需频繁维护。

#### Phase 解析优先级（`check_ruff_lint_island.py`）

脚本按以下优先级确定当前 Phase：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1（最高） | CLI 参数 `--phase` | 用于本地调试/测试 |
| 2 | 环境变量 `ENGRAM_RUFF_PHASE` | **CI 使用此方式注入** |
| 3 | `pyproject.toml [tool.engram.ruff].current_phase` | 项目默认配置 |
| 4（兜底） | 默认值 `0` | Phase 0 跳过 lint-island 检查 |

> **CI 配置**：CI 通过 `${{ vars.ENGRAM_RUFF_PHASE || '0' }}` 注入 Phase 值，优先级高于 pyproject.toml 中的 `current_phase`。

---

## 7. 回滚策略

### 7.1 Phase 回滚

如新规则导致大量阻塞：

```toml
# pyproject.toml - 调整 current_phase
[tool.engram.ruff]
current_phase = 0  # 回滚到 P0
```

或临时跳过 CI 中的 lint-island 检查（`.github/workflows/ci.yml`）：

```yaml
- name: Ruff lint-island (P1)
  run: python scripts/ci/check_ruff_lint_island.py --verbose || true  # 临时跳过
```

### 7.2 规则级回滚

如单条规则误报严重：

```toml
# pyproject.toml
[tool.ruff.lint]
ignore = [
    "E501",
    "B008",  # 临时禁用，待 https://github.com/xxx/issues/123 修复
]
```

---

## 8. 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: mypy 基线管理与 Gate 门禁策略](./adr_mypy_baseline_and_gating.md) | mypy 类似策略参考 |
| [Ruff 官方文档](https://docs.astral.sh/ruff/) | 规则完整列表 |
| `pyproject.toml` | Ruff 配置（SSOT） |
| `scripts/ci/check_ruff_lint_island.py` | Lint-Island 检查脚本 |
| `scripts/ci/check_noqa_policy.py` | noqa 策略检查脚本 |
| `scripts/ci/check_ruff_gate.py` | Ruff 门禁脚本 |
| `scripts/ci/ruff_metrics.py` | Ruff 指标收集脚本 |

---

## 9. 决策记录

| 日期 | 决策 | 原因 |
|------|------|------|
| 2026-02-01 | 采用四阶段 Rollout | 渐进式引入，避免一次性大量报错 |
| 2026-02-01 | 禁止裸 noqa | 提高代码可追溯性，明确豁免原因 |
| 2026-02-01 | Lint-Island 与 mypy Strict-Island 对齐 | 统一高质量模块标准 |
| 2026-02-01 | 文档修正：更新脚本/目标引用 | 对齐实际实现：`check_noqa_policy.py`、`ruff-lint-island` |
| 2026-02-01 | 实现 P2/P3 阶段脚本 | P2: 全仓 P1 规则；P3: + RUF100 冗余 noqa 检查 |
| TBD | 切换到 P2 全仓强制 | 待 Lint-Island 覆盖核心模块 |

---

## 10. 迭代状态追踪

| Phase | 状态 | 启用日期 | 备注 |
|-------|------|----------|------|
| P0 | **进行中** | 2026-01-01 | 基础规则全仓启用 |
| P1 | **已实现** | - | Lint-Island 定义完成，脚本已实现 |
| P2 | **已实现** | - | 全仓 P1 规则检查，扫描 `src/` + `tests/` |
| P3 | **已实现** | - | 全仓 P1 + RUF100 冗余 noqa 检查 |

> **实现说明**：
> - P2/P3 通过设置 `ENGRAM_RUFF_PHASE` 环境变量启用
> - P3 与 `check_noqa_policy.py` 分工：前者检查 noqa 语义有效性（RUF100），后者检查 noqa 语法规范（禁止裸 noqa）
