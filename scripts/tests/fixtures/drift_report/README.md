# OpenMemory Drift Report Fixtures

本目录包含用于演练和测试的固定 drift report JSON 文件。

## 用途

这些 fixture 文件用于：
1. **CI 演练模式**：验证 Issue 创建和 Summary 输出逻辑，无需实际调用 GitHub API
2. **单元测试**：测试 `openmemory_drift_parse.py` 的解析逻辑
3. **文档示例**：展示各种场景的 JSON 结构

## 文件清单

| 文件 | 场景 | 预期 Exit Code | 主要 Action |
|------|------|---------------|-------------|
| `security_drift.json` | 安全更新（非冻结） | 1 | 创建 Security Issue + Summary 告警 |
| `security_frozen.json` | 安全更新 + 冻结 | 1 | 告警 + Issue + 提示需 override |
| `frozen_invalid_override.json` | 冻结 + 无效 override | 3 | 阻断，提示 override 无效 |
| `no_drift.json` | 无漂移 | 0 | 正常通过 |

## 演练模式使用

```bash
# 通过 workflow_dispatch 触发演练模式
gh workflow run nightly.yml \
  -f drift_rehearsal_mode=true \
  -f drift_rehearsal_fixture=security_frozen

# 或者在本地测试
python scripts/openmemory_drift_parse.py \
  scripts/tests/fixtures/drift_report/security_frozen.json \
  --github-output /tmp/test_output
```

## 结构说明

每个 fixture 包含以下额外字段（仅用于文档，解析时忽略）：
- `_fixture_description`: 场景描述
- `_expected_exit_code`: 预期的 exit code
- `_expected_actions`: 预期触发的 CI 动作

## 相关文档

- [Vendoring 与补丁管理](../../../../docs/openmemory/00_vendoring_and_patches.md) - 5.5/5.6 节
- [test_openmemory_parse_policy.py](../test_openmemory_parse_policy.py) - 测试用例
