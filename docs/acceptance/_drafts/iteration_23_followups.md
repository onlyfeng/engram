# Iteration 23 Follow-ups (Draft)

## SCM Sync

- [ ] 独立实现 `gitlab_reviews` 执行链路（替代当前 `reviews -> gitlab_mrs` 兼容映射）
  - 范围：
    - 增加默认 handler：`gitlab_reviews`
    - 增加任务实现模块：`src/engram/logbook/scm_sync_tasks/gitlab_reviews.py`
    - 补齐 scheduler/executor/contract 回归测试
  - 验收标准：
    - scheduler 产出的 `gitlab_reviews` 任务可被默认 worker 正常执行
    - 不影响现有 `gitlab_mrs` 与 `reviews` 兼容调用路径
