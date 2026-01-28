# Role Profile（示例）

## 基本信息
- user_id: u_example
- 负责模块：Client/UI/某系统
- 质量门槛：必须有可复现步骤与验证口径

## 审查偏好（Review Heuristics）
1. 行为不变优先（保持兼容）
2. 先给出证据链：日志/截图/测试项
3. 风险分级：高风险改动必须拆分与加监控点

## 常见坑点（Pitfalls）
- 资源加载路径差异导致的平台兼容问题
- Native/JS 边界数据结构变更未同步

## 默认检索范围（Scope）
- team:<project> + private:<user_id>
- 路径前缀：src/client/**

## 输出格式偏好
- 先结论后证据，列出可执行下一步
