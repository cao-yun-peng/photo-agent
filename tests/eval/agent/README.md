# Agent 工具轨迹 Development 基线

> 2026-09-09：前置路由已从主流程移除。下文原成绩为历史基线，不代表当前loop质量。新方案使用 `tests/eval/agent/loop_migration_v1.jsonl` 与 `scripts/eval_loop_migration.py`；迁移报告见 `docs/30-agent-loop-migration.md`，当前模型质量Gate未通过。

11 个合成场景，覆盖搜索→拒绝第二张→续搜、模糊澄清、隐藏工具、生成确认、未选图、非法参数、Skill 推荐、工具超时、空结果、control 结束及步数预算。

这是单次标注的开发种子，尚无独立复核、Validation/Test 集和真实模型成绩。未实现的“两批不满策略”和 Skill 包导入验收在 P2/P3 新增，不能用当前成功率声称它们已经支持。

## 执行

在项目根运行：

```powershell
.venv/Scripts/python.exe scripts/eval_agent.py trajectory --repeat 3 --output .project-to-act/tasks/S6-P1-001/evidence/trajectory-development-final.json
```

默认脚本模型，真实运行 Agent 路由、循环、参数策略、状态回写和反馈逻辑。替换的是注册工具函数、数据库、候选维护、Redis 入口及反馈日志；不会调用真实生成。脚本响应不读取 expected；expected 仅供事后评分。脚本耗尽、缺失 fixture 和异常均计失败，不能靠错误提示完成场景。

`--live` 显式启用已配置的真实对话模型，业务工具仍为假工具；仅支持受控合成输入。每个案例最多 8 次模型调用（数据契约上限 20），每轮最多 4 步、50 秒外层截止；上下文路由计入同一模型调用预算。脚本和真实调用次数分开报告。运行默认每例一次，`--repeat 3` 至 `5` 可观察稳定性。未配置凭据时真实模式拒绝启动，不静默切成 mock。

## 契约与评分

- 数据：`agent_trajectory_v1.jsonl`；元数据：`agent_trajectory_v1.meta.json`。
- 可执行契约：`app/evaluation/contracts.py`；JSON Schema：`agent_trajectory_v1.schema.json`。
- `turns[].expected` 必须声明终态和最大工具尝试次数，可添加必需工具、禁止执行工具、偏序、参数、状态、最终答复关键词和模拟写入次数。
- 参数断言采用明确工具名、从零开始的调用序号、字段值；仅支持相等、contains、includes、lte，不执行字符串表达式。
- `attempted_tools` 是事件中的尝试；`invocations` 是实际进入 fixture 的业务工具。隐藏工具被拒绝属于成功防护，不应误算为已经执行。final_answer 为拦截伪工具，使用终态和尝试次数检查；本版必需/禁止工具统计面向业务 fixture，不用于伪工具。
- `simulated_writes` 仅表示 fixture 生成动作，不证明真实额度、数据库确认、队列幂等或所有权。
- 输出只留结构化路由/参数/状态和断言结果，不存原始模型回复、推理字段、完整事件或图片。

退出码：0 为所有选定断言通过，1 为基线存在失败，2 为数据/参数契约错误。没有数值质量门槛校准，本版 `baseline_pass` 仅为断言全通过，不是发布批准。

## 当前失败与复核

2026-09-06：8/11 通过，重复三次为24/33，同样3个场景每次失败。模糊找图、已选图生成、非法生图参数场景在预期工具之前进入搜索路径，触发缺失搜索 fixture。应先复核路由和标签，不能通过给无关搜索补成功响应来掩盖偏离任务。

禁止在本轮反向修改标签或产品规则使分数变好。后续复核产生标签变更时保存新版本、原因和对照。
# P2反馈轨迹补充

`feedback_trajectory_v2.jsonl`沿用TrajectoryCase严格Schema，包含3条合成Development多轮案例：两批反馈升级和候选分页、显式全相册分页、换目标清零。通过CLI的`--dataset`指定运行；使用脚本决策与隔离工具，不代表真实模型效果，不替换原有基线。单作者编写，尚未独立标注复核。


## 自然语言澄清实验

text_clarification_v1.jsonl 为2026-09-10调用前冻结的16场景（已消费），每场景3次。使用 eval_loop_migration.py 的 --dataset 和 --text-clarification；该标志跳过旧事件断言并标记 manual_pending，原始 passed 仅为状态断言结果，绝非最终语义评分。必须按预先冻结 rubric 审阅所有回复并检查状态；本轮人工复核见 .project-to-act/tasks/S6-CLARIFY-20260910/evidence/review.json。不可拿本集反复调参后声称留出集提升。


## 舒适度验证（2026-09-10）

comfort_development_v1/focus是已消费案例的开发回放；comfort_validation_v1.jsonl是本轮调用前冻结的新16场景，重复3次后已消费。评分检查明确拒绝ID，并对所有公开文本做人工语义审阅。最终47/48；原始文本和1次未执行却声称成功的失败保留于S6-LOOP-20260909/evidence/new-validation-comfort-final.json，正式复核和限制见S6-COMFORT-20260910/evidence/review.json。不得把raw的manual_pending事件评分当作完成语义审核。
