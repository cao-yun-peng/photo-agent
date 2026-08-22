# Agent v2：复杂任务收口与灰度说明

## 本次改变

Agent v2 把搜索、选择、生成拆成显式状态：

`idle → searching → results_ready / awaiting_selection → selection_confirmed → awaiting_generation_confirmation → generation_queued`

- 普通搜索仍走快路径；“全部自拍”等可由结构化字段穷举的集合搜索可以直接返回完整结果。
- “全部鸟/全部动物”等开放类别不会再把整个相册误报成完整结果。它会进入 `exhaustive_semantic` 路径；在没有可验证硬条件时，接口明确返回 `semantic_scope_unverified`。
- v2 中模型只看到 `search_photos`、`ask_clarification`、`apply_skill`、`recommend_skills` 四个业务级工具。浏览、兜底和详情查询由代码控制。
- 生成先创建 `awaiting_confirmation` 任务，前端展示照片、效果、模型和预计费用；用户确认后才预占当日额度并入队。
- `user_id + idempotency_key` 唯一，重复点击、网络重试和重复确认都复用同一任务。Worker 使用稳定 ARQ job ID，并支持一次自动重试；最终失败会释放预占额度。

## 数据库升级

部署 API 和 Worker 前执行：

```powershell
alembic upgrade head
```

API 与 Worker 必须使用同一版本代码。旧任务仍可由 Worker 处理；旧客户端在控制组保持一步式生成。

## 开启灰度

初始建议配置：

```dotenv
AGENT_V2_ENABLED=true
AGENT_V2_ROLLOUT_PERCENT=10
AGENT_V2_ROLLOUT_SALT=photo-agent-v2
AGENT_V2_KILL_SWITCH=false
GENERATION_CONFIRMATION_TTL_SECONDS=600
GENERATION_ESTIMATED_COST_YUAN=0.14
```

用户通过稳定哈希分桶，同一用户不会在请求间来回切换。推荐放量节奏：

1. 10% 至少观察 24 小时；
2. 50% 至少观察 24 小时；
3. 100% 后继续保留 Kill Switch 一个发布周期。

需要立即回滚时只设置：

```dotenv
AGENT_V2_KILL_SWITCH=true
```

重启 API 后新请求回到控制组，不需要回滚数据库。已经确认并入队的任务继续由 Worker 完成。

## 生成接口

1. `POST /photos/{photo_id}/generate`：创建准备任务。客户端应传 8～128 字符的 `idempotency_key`。
2. v2 返回 `status=awaiting_confirmation`、`confirmation_token`、`confirmation_expires_at` 和 `estimated_cost_yuan`。
3. 用户确认后调用 `POST /generations/{id}/confirm`，请求体为 `{"confirmation_token":"..."}`。
4. 确认接口可用相同参数安全重试；随后按原方式轮询 `GET /generations/{id}`。

## 灰度验收与面板

Grafana 的“6. Agent v2 灰度与质量”展示：

- Control / v2 路由分布；
- 每轮模型调用数 P95；
- 搜索空结果率；
- 生成准备、确认、额度拒绝与入队失败。

离线评测 `scripts/agent_eval.py` 在用例提供 `expected.expected_route` 时输出 `route_confusion_matrix` 和 `route_accuracy`。放量门槛：

- P0 安全用例全部通过；
- 普通搜索模型调用 P95 不高于 2；
- 首张照片 P95 小于 5 秒；
- v2 空结果率、错误率不显著劣于控制组；
- `quota_rejected`、`enqueue_failed` 和 Worker 最终失败没有异常突增。

## 当前边界

开放类别的“全部”只有在已有可枚举的结构化标签或经过逐图验证后才能声称完整。本次修正确保系统不会误把 Top-N 或整个相册说成“全部匹配”；要真正覆盖任意开放类别，后续需建设通用对象层级标签与存量重索引，而不是继续添加“鸟、狗、自拍”等特例。
