# Turn Routing seed baseline

`turn_routing_v1.jsonl` 是前置语义判断的首版种子基线。每一行是一道独立考题：
`context + user_input` 是输入，`expected` 是结构化标准答案。

本数据集同时支持三个视角：

1. `rule_router`：`rule_outcome=plan` 必须返回规则计划，`defer` 必须返回 `None`。
2. `contextual_router`：只运行 `rule_outcome=defer` 的样本并调用真实上下文模型。
3. `router_system`：按生产路径运行，分别报告最终质量和模型调用率。

## 文件

- `turn_routing_v1.meta.json`：版本、切分、来源和使用边界。
- `turn_routing_v1.schema.json`：单条案例的机器可读契约。
- `turn_routing_v1.jsonl`：80 条合成、脱敏案例。

## 标注约定

- `active_search.resolved_query` 与当前产品状态字段一致。
- `rule_outcome=defer` 表示规则层应把输入交给上下文模型，不表示评测失败。
- `query_all_terms` 只要求保留关键语义，不比较完整句子。
- 相对日期使用固定的 `reference_date=2026-08-28`，避免“去年”随运行时间变化。
- `safety_critical` 样本单独计数，不能被总体平均分抵消。
- Test 只用于最终盲测；查看 Test 结果后不得用它继续调 Prompt 或规则。

## 当前限制

这是单次标注的种子集，尚未完成独立第二人复核，也没有真实模型运行结果，因此不能作为
阶段 6 或生产发布 Gate。后续应先复核标签和歧义，再建立 Development 基线并用 Validation
冻结阈值。
