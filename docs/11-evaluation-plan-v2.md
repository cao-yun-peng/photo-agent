# Photo Agent 评测体系 V2

> 状态：设计草案；本文件冻结评测对象和实现顺序，不代表任何质量 Gate 已通过。
> 当前唯一可执行的模型质量基线仍是 VL 评测。

## 1. 结论与边界

评测按“越靠下越便宜、越确定，越靠上越真实、越昂贵”分层。用户提出的三类评测是
Agent 控制面的主干：前置语义判断、对话工具使用、真实对话 Trace。项目还必须并行评测
照片检索能力，否则即使 Agent 选对工具，也可能返回错误照片。

本轮设计把对象分成五层：

| 层 | 评测对象 | 运行依赖 | 主要回答的问题 |
|---|---|---|---|
| L0 | 确定性契约与 VL | 无真实对话模型或只调用 VL | Schema、规则、权限、确认、预算与图片理解是否正确 |
| L1 | Turn Resolver 路由 | 规则测试；歧义子集调用真实模型 | 当前输入应搜索、续搜、反馈、进入复杂 Agent，还是澄清 |
| L2 | Agent 工具轨迹 | 真实对话模型 + 可控假工具 | 是否选对工具、参数、顺序、状态和停止条件 |
| L3 | 检索能力 | 冻结相册 + 真实检索栈 | 召回、硬约束、重排、零结果和延迟是否合格 |
| L4 | 真实 HTTP/SSE Trace | 隔离账号 + 完整服务和外部依赖 | 整条产品链路、可观测性与副作用是否正确 |

安全、可靠性、延迟和成本不是单独的最后一项，而是贯穿每一层的横向 Gate。

## 2. L0：确定性契约与现有 VL

### 2.1 确定性契约

用普通单元测试覆盖不需要模型判断的行为：

- Turn Resolver 的明确规则、阈值和安全回退。
- 工具参数 Schema、未知工具拒绝、用户 ID 注入和跨用户资源隔离。
- 图像生成前确认、额度、幂等和禁止隐式付费。
- Agent 最大步数、超时、Token/成本预算和循环终止。
- SSE 事件 Schema、状态迁移、失败降级和敏感字段脱敏。

这些断言必须由代码判定，不使用 LLM-as-a-judge。

### 2.2 VL

继续使用 `tests/eval/photo_manifest.json`、`scripts/offline_eval.py` 和
`scripts/vl_prompt_experiment.py`。后续先修正评分器定义，再冻结 Gate：

- 空标注不得自动得到满分。
- Object precision 与 recall 必须分别计算。
- Prompt 实验退出码必须同时反映执行错误和质量 Gate。
- Development 用于改 Prompt；Validation 用于选择；Test 只做盲测。

## 3. L1：前置语义判断评测

### 3.1 需要拆成两个对象

当前前置判断不是“每轮都调用一次模型”，而是混合路由：确定性规则先处理高置信场景，
只有带上下文且规则无法确定的输入才调用模型。因此必须分开报告：

1. `rule_router`：只测规则，不调用模型。
2. `contextual_router`：只测需要模型消歧的样本。
3. `router_system`：按生产路径运行规则 + 模型，统计最终质量和模型调用率。

若混在一个准确率里，规则样本会稀释模型错误，也无法判断增加模型调用是否真的有收益。

### 3.2 数据集契约

建议文件：`tests/eval/routing/turn_routing_v1.jsonl`。一行代表一个待决策回合：

```json
{
  "id": "route-refine-001",
  "split": "development",
  "context": {
    "active_search": {"query": "海边照片", "place": null},
    "recent_messages": ["帮我找海边的照片"],
    "last_result_ids": ["controlled-photo-01"]
  },
  "user_input": "只看去年在厦门拍的",
  "expected": {
    "intent": "photo_search",
    "relation": "refine",
    "query_contains": ["海边"],
    "place": "厦门",
    "needs_clarification": false,
    "allowed_sources": ["contextual_model"]
  },
  "tags": ["model_required", "refine", "date", "place"],
  "risk": "normal"
}
```

数据必须覆盖：`new`、`replace`、`refine`、`continue`、结果反馈、选择模式、模糊输入、
丢失的照片引用、非搜索复杂请求、日期/地点组合、冲突上下文、提示注入和越权诱导。
对可以有多种正确表达的查询，保存结构化槽位和允许集合，不做整句字符串精确匹配。

### 3.3 评分器

- Intent macro-F1 与关键样本准确率。
- Relation accuracy。
- 日期、地点、结果模式和检索策略的槽位 F1/精确匹配。
- 查询保留、替换和合并的语义断言。
- 应澄清而未澄清、以及不必要澄清的比例。
- 危险 fast path 率、模型调用率、模型失败回退率。
- 延迟、Token 和估算成本。

安全关键错误按条计数，不能被平均分掩盖。初始 Gate 只作为候选值：危险 fast path 和
越权动作必须为 0；其余阈值在 Development 建立基线、Validation 校准后冻结，Test 前不得再改。

## 4. L2：对话与工具轨迹评测

### 4.1 为什么不是只看最终回复

相同的最终话术可能来自错误工具、错误参数或多余调用。此层运行真实对话模型，但把搜索、
照片详情、技能和生成工具替换为确定性 fixture，使模型策略变化与后端波动解耦。

### 4.2 数据集契约

建议文件：`tests/eval/agent/agent_trajectory_v1.jsonl`。一个案例是一段多轮场景：

```json
{
  "id": "agent-reject-and-continue-001",
  "split": "validation",
  "initial_state": {"active_search": null, "quota": 1},
  "turns": ["找去年在上海拍的猫", "第二张不要，再找一张"],
  "tool_fixtures": {"search_photos": "fixture://search/cats-shanghai"},
  "expected": {
    "required_tools": ["search_photos"],
    "allowed_sequences": [["search_photos", "final_answer"]],
    "forbidden_tools": ["apply_skill"],
    "argument_assertions": ["from_date is set", "place == 上海"],
    "state_assertions": ["rejected_photo_ids contains controlled-photo-02"],
    "max_tool_calls": 3,
    "side_effects": "none"
  },
  "tags": ["multi_turn", "result_feedback", "continuation"]
}
```

案例族至少包括：搜索、续搜、筛选、选择、错误照片反馈、照片详情、技能推荐、生成确认、
额度不足、参数缺失澄清、工具超时、空结果、未知工具、上下文恢复和停止条件。

### 4.3 评分器

- 场景任务成功率。
- 必需/允许/禁止工具断言，工具选择 precision/recall。
- 参数 Schema、业务槽位和受控 ID 精确断言。
- 工具顺序的偏序约束，不强制唯一完整序列。
- 状态迁移、副作用次数、确认和幂等断言。
- 过早执行、遗漏澄清、循环、超预算和异常终止率。
- 同一案例重复 3–5 次后的稳定通过率。

权限、付费、删除、越权和工具调用由确定性评分器判断。LLM judge 只可用于最终回复的帮助性、
清晰度和是否忠于工具结果，并且必须先用人工双标样本做一致性校准。

## 5. L3：照片检索能力评测

使用版本化的冻结相册和人工标注查询，单独评价 Agent 下方的检索栈。建议数据分成：

- 正常语义查询、同义表达和组合条件。
- 日期、地点等硬约束。
- 零结果和近似结果不得冒充命中。
- 难负例、相似场景、OCR/人物/物体混淆。
- 重排与视觉复核的收益和额外调用成本。

核心指标为 Recall@K、MRR/nDCG、硬约束违规率、零结果准确率、重复率、P50/P95 延迟、
重排/视觉复核调用率和单查询成本。必须同时保存原始候选列表，才能定位召回、重排还是
视觉验证导致错误。

## 6. L4：真实对话 Trace 评测

此层只保留 10–20 条关键用户旅程作为首批套件，使用隔离账号、固定相册和完整
HTTP/JWT/PostgreSQL/Redis/SSE/模型/工具链。每次运行都生成可脱敏回放的 Trace 产物。

Trace 评分不只看最终文本，还应断言：

- HTTP 状态、Session ID 和 SSE 事件完整性。
- `start -> route -> tool_call -> tool_result -> final/done` 的必要偏序。
- HTTP、Agent、模型、工具、数据库/Redis Span 的父子或链接关系。
- 路由来源、模型调用数、工具参数和状态变化符合场景契约。
- 不出现越权资源、未确认生成、重复副作用、秘密、签名 URL 或原始敏感内容。
- 重试、超时、回退、Token、成本和 P50/P95 延迟在预算内。

模型相关关键旅程重复 3–5 次；有写副作用的案例在隔离环境运行，并验证幂等和清理结果。
真实 E2E 是最终证据，不用来代替便宜且容易定位的 L0–L3 回归。

## 7. 数据集建立方法

1. 从产品能力和风险建立案例矩阵，不从历史脚本反推需求。
2. 每个案例先由一人编写，再由另一人只看契约复核标签和允许答案。
3. 用户失败案例只回灌结构化意图、受控 ID 和脱敏摘要，不保存原照片或完整对话。
4. 固定 Development / Validation / Test；同一语义变体和同一照片族不能跨 split 泄漏。
5. 增加独立 Adversarial 与故障集，不计入普通准确率来掩盖风险。
6. 每次失败按 `路由 / 参数 / 工具策略 / 状态 / 检索 / 基础设施 / 评分器` 分类。
7. 已修复的真实失败加入 Regression；Test 被查看后必须换新版本才能重新称为盲测。

建议首批规模不是追求大，而是追求覆盖和复核：路由 120–200 条、Agent 轨迹 40–60 个、
检索查询 100–200 条、真实 E2E 10–20 条。规模应在覆盖矩阵审查后调整。

## 8. 运行产物与可复现元数据

每次评测必须保存：代码 SHA、模型与供应商、Prompt 版本、工具 Schema hash、数据集版本和
split、运行配置、随机/采样参数、开始时间、原始结构化结果、聚合指标、失败分类、Token、
成本与延迟。默认不保存原始用户照片、完整对话和未脱敏工具输出。

报告必须同时给出：总分、按标签切片、关键失败清单、与基线差异、置信区间或重复稳定性，
以及明确的 Gate 决策。没有原始结构化产物和版本信息的分数不能作为发布证据。

## 9. 实施顺序

1. 冻结通用 JSONL 元数据、结果格式和评测运行 manifest。
2. 实现 L1 路由数据集与确定性评分器，先分清规则和模型子集。
3. 实现 L2 假工具轨迹执行器和状态/副作用断言。
4. 重建 L3 冻结相册检索集和分阶段指标。
5. 实现 L4 隔离 HTTP/SSE Trace runner 与 Span 断言。
6. 加入对抗、故障、延迟和成本 Gate；建立基线后在 Validation 冻结阈值。
7. 最后运行 Test 和阶段 6 Gate 评审；Test 失败只进入下一数据集版本，不回头调当前 Test。

## 10. 当前推荐方案（待项目所有者确认）

- 建议接受“前置语义判断—工具轨迹—真实 Trace”作为 Agent 评测主干。
- 前置语义判断必须拆分规则路由和模型路由。
- 工具评测使用真实模型与确定性工具 fixture，不能只靠最终回复或纯 LLM judge。
- 检索评测作为独立并行能力层，不能由 Agent E2E 分数代替。
- 真实 Trace 套件保持少而关键，在下层评测稳定后执行。
- 本文件只形成设计草案，不创建新评测脚本、不冻结未经基线验证的数值 Gate；项目所有者
  确认方向后再冻结接口并开始实现。

当前进展：已在 `tests/eval/routing/` 建立 80 条 L1 路由种子案例及 Schema/元数据，并完成
纯数据结构与覆盖校验；标签仍待独立复核，尚未运行真实模型或冻结 Gate。
