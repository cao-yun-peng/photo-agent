# Turn Routing seed baseline

> 2026-09-09：前置路由已从主流程移除。下文原成绩为历史基线，不代表当前loop质量。新方案使用 `tests/eval/agent/loop_migration_v1.jsonl` 与 `scripts/eval_loop_migration.py`；迁移报告见 `docs/30-agent-loop-migration.md`，当前模型质量Gate未通过。

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

## P1 可执行评分器（2026-09-06）

在根目录运行：

```powershell
.venv/Scripts/python.exe scripts/eval_agent.py routing --output .project-to-act/tasks/S6-P1-001/evidence/routing-development-final.json
```

默认只跑 Development 的 rule_router，不调用模型；48 条中 41 条通过，其中 10 条正确 defer 不计入模型意图准确率。38 条规则计划的 intent macro-F1 为约 0.932。基线退出码 1，保留失败。

`--mode contextual_router --live` 只选择标签为 defer 的案例；`--mode router_system --live` 运行完整规则+模型。真实模式不允许 mock 替代，当前未运行。两种模式仍走现有生产 resolve_turn，因规则变化未进入模型时会被 source 断言捕获；不声称强制测到了模型能力。

本地 parser 日期固定为案例 reference_date；现有上下文模型 Prompt 没有注入参考日期，真实模式相对日期可复现性尚有限制，报告明确列出。该问题先记录，不能偷偷改生产 Prompt 来适配评测。

支持 intent/relation/source、澄清、search 和 feedback 条件断言；查询关键词检查只是种子集词项断言，不是语义相似度评分。缺失字段失败，未知断言由严格契约拒绝。分标签、关键失败、意图 macro-F1、关系准确率、澄清错误计数、危险 fast path、模型调用及延迟在报告中输出；币价未知保持 null。

只在最终验证时使用 `--split test --ack-test-consumption`；本轮未运行 Validation/Test。元数据及原有种子标签不变，第二标注者复核仍待完成。
