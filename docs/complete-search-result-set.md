# 完整搜索结果集

第二阶段把“用户自选”从固定 Top-30 改为两种明确协议：

- 指定数量：`result_mode="select"`，`limit` 原样保留，例如 50 张返回 50 张；
- 全部匹配：`result_mode="select"`，`complete_result_set=true`，不使用固定数量截断。

## 完整性的含义

`result_set_complete=true` 需要同时满足：

1. 搜索没有使用 Top-N 截断；
2. 用户相册的可搜索索引覆盖完整；
3. 返回结果经过当前查询的结构化硬条件过滤。

若仍有照片正在建立索引，服务端会返回当前可检索的全部结果，但
`result_set_complete=false`，前端显示“当前可检索结果”，不能向用户声称一张不漏。

“全部自拍”和“全部截图”会分别映射到 VL 分析中的 `capture_context=自拍` 与
`capture_context=屏幕截图`。其他自然语言概念仍以现有向量召回契约为准，不使用未经
校准的单一相似度阈值冒充精确边界。

## 交付和渲染

完整结果在第一次 Agent 响应的 `tool_result.items` 中交付。小程序把所有结果保存在
本地缓冲区，每次只渲染 30 个节点，点击“再展示 30 张”不再访问后端。

会话状态保存全部结果 ID，用于校验第 31 张以后的用户选择；详细照片信息只保留前
30 条，避免 AgentSession JSON 和模型上下文随相册大小线性膨胀。

## 关键返回字段

| 字段 | 含义 |
|---|---|
| `total` | 本轮实际交付数量 |
| `total_matches` | 当前搜索阶段匹配总数 |
| `complete_result_set` | 本轮是否请求无 Top-N 截断 |
| `result_set_complete` | 服务端是否确认当前结果集完整 |
| `completeness_reason` | 不完整原因，例如索引未完成或语义边界无法可靠确认 |
| `truncated` | 是否因数量参数发生截断 |
