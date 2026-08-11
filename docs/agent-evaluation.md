# Photo Agent 评测器：设计与使用说明

本文介绍 `scripts/agent_eval.py` 的目标、运行模式、数据集格式、评分规则和常见问题。
评测器用于持续验证 Agent 的意图判断、工具调用、参数构造、安全边界与预算控制；
它不会访问真实相册、数据库或 OSS，模型只与确定性的工具桩交互。

## 1. 评测边界

评测链路如下：

```text
测试用例
  -> 请求层校验（仅 validation 用例）
  -> Agent 决策循环
  -> 真实工具 Schema + 确定性工具桩
  -> 事件与调用记录
  -> 参数/顺序/状态/内容/安全/预算评分
  -> JSON 报告与进程退出码
```

评测器刻意隔离以下外部依赖：

- PostgreSQL 与 pgvector；
- Redis；
- OSS 与签名 URL；
- 生图队列和第三方图像模型；
- 用户画像与推荐服务。

因此它衡量的是 Agent 的决策与编排，不衡量向量召回质量、真实图片生成质量或线上系统吞吐。

## 2. 两种模式

### replay：评测管线自检

```bash
python scripts/agent_eval.py --mode replay
```

`replay` 会按用例里的 `expected_tools` 回放标注动作，用于验证：

- Agent 循环是否能正确执行工具并结束；
- 工具桩是否返回可序列化、符合协议的数据；
- 参数断言、状态识别和汇总阈值是否正确；
- 数据集本身是否存在矛盾。

回放动作来自标准答案，所以它不衡量模型的意图识别和工具选择能力。报告中的
`model_metrics_valid` 固定为 `false`，不能把 replay 分数写入效果报告或简历。

旧命令 `--mode mock` 仍可使用，但只是 `replay` 的兼容别名，并会打印弃用警告。

### real：真实模型评测

```bash
# PowerShell
$env:DASHSCOPE_API_KEY = "sk-真实密钥"
python scripts/agent_eval.py --mode real \
  --output artifacts/agent-eval-real.json
```

`real` 使用生产 Agent 的系统 Prompt、工具 Schema 和 DashScope function calling。
如果 Key 为空或仍是 `sk-xxx` 等占位值，脚本会退出并返回代码 `2`，不会静默降级成 Mock。

真实模式会产生模型费用。建议先筛选 P0 或单个维度：

```bash
python scripts/agent_eval.py --mode real --priority P0
python scripts/agent_eval.py --mode real --dimensions D1,D2,D8
python scripts/agent_eval.py --mode real --dimensions D3 --priority P1
```

## 3. 数据集角色

当前 `tests/eval/agent_eval_dataset.json` 标记为：

```json
{"dataset_role": "development"}
```

它是开发集，可以用于发现 bad case 和优化 Prompt，但优化之后不能再把同一批数据当成
最终测试集。建议建立三份数据：

| 数据集 | 用途 | 是否允许据此改 Prompt |
|---|---|---|
| `agent_eval_dev.json` | 日常定位问题 | 允许 |
| `agent_eval_regression.json` | 固定历史 bad case 回归 | 只允许修复明确回归 |
| `agent_eval_holdout.json` | 阶段性效果报告 | 不允许；评测前保持隐藏 |

简历或项目报告中的准确率必须来自 `real + holdout`，并同时记录模型版本、Prompt 版本、
数据集版本和评测日期。

## 4. 用例格式

最小用例：

```json
{
  "id": "TC-101",
  "dimension": "D2",
  "category": "搜索工具选择",
  "priority": "P0",
  "difficulty": "easy",
  "user_query": "帮我找海边拍的照片",
  "context": {
    "photos_available": true,
    "matching_photos": ["p-003"]
  },
  "expected": {
    "expected_tools": ["search_photos", "final_answer"],
    "tool_order_strict": true,
    "must_not_call": ["apply_skill"],
    "expected_final_status": "completed",
    "expected_result_contains": ["海边"]
  },
  "rubric": {
    "pass_threshold": 0.8
  }
}
```

### context 字段

| 字段 | 作用 |
|---|---|
| `photos_available` | 是否存在可浏览照片 |
| `matching_photos` | 普通搜索工具返回的照片 ID |
| `similar_photos` | 兜底搜索返回的相似照片 ID |
| `quota_exhausted` | `apply_skill` 是否返回额度用尽 |
| `confirmed_photo_id` | 当前已确认照片 |
| `last_search_items` | 上一轮候选照片 |
| `rejected_photo_ids` | 用户明确否定的照片 |
| `session_history` | 历史消息审计信息；当前主要依赖结构化状态续接 |
| `max_steps` / `max_searches` | 覆盖单用例运行预算 |
| `validation_query_length` | 请求层边界用例使用的输入长度，避免在 JSON 中保存超长占位文本 |

### expected_tools

`expected_tools` 表示允许且需要出现的动作序列。工具选择使用带计数的 F1，既惩罚漏调，
也惩罚未标注的额外业务工具。`final_answer` 属于控制动作，不进入工具选择 F1，最终是否
正确结束由 `expected_final_status` 和内容断言负责。

需要重复调用同一工具时使用：

```json
{
  "expected_tools": ["search_photos"],
  "min_tool_calls": {"search_photos": 2}
}
```

`min_tool_calls` 主要用于 replay 构造重复动作；真实模式评分会从实际调用记录计算次数。

### 参数断言

参数断言必须采用 `工具名.参数名`：

```json
{
  "parameter_checks": {
    "apply_skill.photo_id": {"equals_photo_id": "p-001"},
    "search_photos.from_date": {"equals": "2025-03-01"},
    "search_photos.query": {
      "contains_all": ["狗"],
      "excludes": ["猫"]
    }
  }
}
```

支持的断言操作：

| 操作 | 含义 |
|---|---|
| `equals` | 与指定值严格相等 |
| `equals_photo_id` | 将 `p-001` 转换为评测 UUID 后比较 |
| `not_empty` | 参数必须存在且非空 |
| `contains_all` | 字符串必须包含全部关键词 |
| `contains_any` | 字符串至少包含一个关键词 |
| `excludes` | 字符串不能包含任一关键词 |

不要再写“应包含相关语义”这种不可执行的描述。语义相似度判断应接入独立 Judge，并记录
Judge 模型和 Prompt 版本；在此之前优先使用可复现的结构化断言。

### 请求层用例

空查询、超长查询等属于 FastAPI/Pydantic 契约，不应直接调用 `PhotoAgent.run()`：

```json
{
  "expected": {
    "expected_tools": [],
    "expected_final_status": "error",
    "expected_error_type": "validation"
  }
}
```

评测器会使用 `AgentRunRequest` 验证这些用例。

## 5. 评分规则

单用例总分由以下部分组成：

| 指标 | 权重 | 说明 |
|---|---:|---|
| 工具选择 | 20% | 期望与实际业务工具的计数 F1 |
| 工具顺序 | 10% | `tool_order_strict=true` 时要求完全一致 |
| 禁止工具 | 20% | 命中 `must_not_call` 即为 0 |
| 参数构造 | 10% | 执行结构化参数断言 |
| 最终状态 | 15% | `completed/clarified/fallback/error` 严格比较 |
| 回复与结果内容 | 15% | 检查关键字、禁用词和错误类型 |
| 安全 | 5% | D8 中结合禁止工具与内容检查 |
| 预算 | 5% | 检查真实决策步数、搜索次数和耗时 |

每条用例使用自己的 `rubric.pass_threshold`。汇总不再用统一 `0.7` 覆盖它。

维度平均分使用 `scoring.dimensions` 中的阈值和权重；总体门禁使用
`scoring.overall_pass_threshold`。运行异常永远不能通过。

## 6. 报告结构

输出 JSON 包含：

```text
metadata
  dataset / dataset_version / dataset_role / mode / generated_at
summary
  model_metrics_valid / gate_passed / overall_score / pass_rate
  metrics / by_dimension / by_priority
results[]
  tool_calls / final_status / steps / error / scores / notes
```

重点区分：

- `pass_rate`：按每条用例自己的阈值计算；
- `overall_score`：按维度权重汇总；
- `gate_passed`：CLI 是否返回成功；
- `model_metrics_valid`：是否为真实模型决策结果。

退出码：

| 代码 | 含义 |
|---:|---|
| `0` | 门禁通过 |
| `1` | 评测执行完成但质量门禁未通过 |
| `2` | 数据集、参数或真实模型配置错误 |

## 7. 推荐工作流

```bash
# 1. 代码与数据集结构检查
ruff check app scripts tests
pytest -q

# 2. 评测管线自检
python scripts/agent_eval.py --mode replay \
  --output artifacts/agent-eval-replay.json

# 3. 小范围真实评测
python scripts/agent_eval.py --mode real --priority P0 \
  --output artifacts/agent-eval-p0.json

# 4. 完整开发集评测
python scripts/agent_eval.py --mode real \
  --output artifacts/agent-eval-dev.json

# 5. Prompt 冻结后，由未参与优化的人或流程运行 holdout
python scripts/agent_eval.py --mode real \
  --dataset tests/eval/agent_eval_holdout.json \
  --output artifacts/agent-eval-holdout.json
```

一次优化应保存“修改前/修改后”两份报告，并按用例 ID 对比，而不是只看总体平均分。

## 8. 当前限制

- 当前开发集中的 D9 主要验证注入后的结构化上下文，不等于完整的跨请求会话持久化测试；
- 内容检查以确定性关键词为主，不能判断复杂事实一致性；
- 工具桩不衡量数据库、向量召回和 OSS 的真实行为；
- development 数据集已参与 Prompt 优化，不能作为最终泛化结论；
- 模型服务存在随机性，正式报告应至少重复运行 3 次并报告均值和波动。

这些限制应与评测结果一起披露，避免把“管线通过”误写成“线上效果已验证”。

## 9. 常见问题

### real 模式提示 Key 无效

确认当前进程能读取 `DASHSCOPE_API_KEY`，且不是 `sk-xxx`、空字符串或示例值。修改
`.env` 后重新启动终端，或者在当前 Shell 显式设置环境变量。

### 某用例参数存在但参数分仍为 0

查看报告中的 `tool_calls[].arguments` 和 `notes`。评测器现在会比较参数值，而不是仅检查
字段是否存在。

### replay 没有 100% 通过

这通常表示数据标注和 Agent 控制流不一致、工具桩协议错误，或评分器本身回归。先修复
管线，不要通过降低阈值掩盖问题。

### 如何新增真实多轮评测

优先新增专门的 API/E2E 测试，连续请求 `/agent/run` 或 `/agent/stream`，使用同一个
`session_id`，并验证数据库中的 `AgentState` 恢复。不要只把自然语言历史塞进单次调用。
