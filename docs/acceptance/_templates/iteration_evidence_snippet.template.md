# 验收证据片段模板

> **使用说明**：将本模板内容复制到 `iteration_N_regression.md` 的"验收证据"段落中，替换 `{PLACEHOLDER}` 占位符。
>
> **重要**：回归文档应放置于 `docs/acceptance/` 目录下，证据文件放置于 `docs/acceptance/evidence/` 目录下。

---

## 验收证据

| 项目 | 值 |
|------|-----|
| **证据文件** | [`iteration_{N}_evidence.json`](evidence/iteration_{N}_evidence.json) |
| **Schema 版本** | `iteration_evidence_v1.schema.json` |
| **记录时间** | {YYYY-MM-DDTHH:MM:SSZ} |
| **Commit SHA** | `{commit_sha}` |

### 门禁命令执行摘要

| 命令 | 结果 | 耗时 | 摘要 |
|------|------|------|------|
| `make ci` | {PASS/FAIL} | {N}s | {摘要} |
| `pytest tests/gateway/ -q` | {PASS/FAIL} | {N}s | {N} passed, {M} failed |
| `pytest tests/acceptance/ -q` | {PASS/FAIL} | {N}s | {N} passed, {M} skipped |

### 整体验收结果

- **结果**: {PASS / PARTIAL / FAIL}
- **说明**: {验收结论说明}

> **证据文件位置**: `docs/acceptance/evidence/iteration_{N}_evidence.json`
>
> **创建证据文件**：
> 1. 复制模板 `docs/acceptance/_templates/iteration_evidence.template.json` 到 `docs/acceptance/evidence/iteration_{N}_evidence.json`
> 2. 替换所有占位符（`iteration_number`、`recorded_at`、`commit_sha` 等）为实际值
> 3. 运行校验确保格式正确
>
> **校验命令**:
> ```bash
> python -m jsonschema -i docs/acceptance/evidence/iteration_{N}_evidence.json schemas/iteration_evidence_v1.schema.json
> ```
>
> **注意**：模板文件中的占位值仅供参考，**不可直接提交**，必须替换为实际迭代数据。

---

_模板版本：v1.1 | 更新日期：2026-02-02（统一证据文件命名与校验命令）_
