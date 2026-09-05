# 任务交接：S6-EVAL-002

- 状态：进行中；评测架构草案和 L1 路由种子集已形成，路由评分器及后续分层尚未实现
- 阶段：6（评测、安全测试与调优），生命周期 revision 4 为 `in_progress`；上一轮 Gate 的
  `blocked` 结论仍有效，尚未产生新的通过证据
- 设计文档：`docs/11-evaluation-plan-v2.md`
- 推荐方向：L0 确定性契约/VL、L1 路由、L2 Agent 工具轨迹、L3 检索、L4 真实 Trace
- 当前产物：`tests/eval/routing/` 下 80 条路由种子案例、元数据、Schema 和说明；
  `tests/test_routing_eval_dataset.py` 提供纯数据校验
- 核心边界：规则路由与模型路由分开；工具策略与检索质量分开；安全副作用不用 LLM judge
- 当前证据：`E-S6-ROUTING-DATASET-001` 只证明种子集结构和最低覆盖，不证明规则或模型质量
- 下一步：独立复核 Development/Validation 标签和歧义，再实现规则层及上下文模型评分器
- 不变量：不新增产品功能；不保存原始用户照片、完整对话或未脱敏 Trace；Test 保持盲测
