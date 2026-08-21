# Photo Agent 日志与 Trace 全链路实施方案

## 当前交付边界

本方案已经在代码中打通以下链路：

```text
微信小程序 X-Log-ID
  → FastAPI server span / JSON log(trace_id, span_id, logId)
    → PhotoAgent / LLM / Tool / Search Funnel
      → ARQ producer span + W3C trace_context
        → Worker consumer span
          → PostgreSQL / Redis / HTTPX 自动子 span
          → 候补池预取结果 --Span Link--> 后续“还有一张”请求
```

默认 `OTEL_ENABLED=false`，因此开发者只运行 API/测试时没有 Collector 依赖。Docker Compose 联调或生产开启后，应用通过 OTLP/HTTP 把 Trace 和 Log 发送到 Collector；Collector 批处理并脱敏，再分别写入 Tempo 和 Loki。Tempo 的 span metrics 与 service graph 写入 Prometheus，Grafana 统一展示。

## 需求与验收口径

| 需求 | 实现 | 验收 |
|---|---|---|
| HTTP 入口可定位 | 返回 `X-Log-ID`、`X-Trace-ID` | 小程序错误对象能取得两者；Grafana 可按 Trace ID 查询 |
| 日志与 Trace 联通 | JSON 日志包含 `trace_id`、`span_id`、`logId` | Trace 瀑布图跳转 Loki 后命中同一链路日志 |
| Agent 可回放 | `invoke_agent`、`chat`、`execute_tool` span | 能查看步骤、耗时、Token 数、工具结果数，不展示正文 |
| 搜索可解释 | 记录召回、强约束、重排、视觉核验和结果数量 | 能区分“没有召回”“约束过滤”“判同拒绝”“视觉降级” |
| API→Worker 不断链 | 入队注入 W3C carrier，消费端恢复父上下文 | 上传/生成/画像任务在同一 Trace 下出现 producer/consumer |
| 跨请求候补池可关联 | Worker 产物保存 carrier，续搜使用 Span Link | “还有一张”的 `candidate_pool consume` 能反查预取 Trace |
| 隐私优先 | 应用不记录 prompt/回复/图片，Collector 二次删除敏感键 | 导出样本中没有 Authorization、Cookie、Prompt、Completion |
| 关闭时无侵入 | 配置默认关闭，初始化失败不阻断业务 | 未启动观测栈时原有测试和 API 正常工作 |

## Span 规范

| Span | 类型 | 关键属性 |
|---|---|---|
| HTTP route | SERVER | route、method、status（自动埋点） |
| `invoke_agent photo-search` | INTERNAL | `session.id`、`user.id_hash`、follow-up 类型 |
| `chat qwen-plus` | CLIENT | provider、model、输入/输出/总 Token 数 |
| `execute_tool <name>` | INTERNAL | tool 名、超时、结果数、错误类型 |
| `search retrieve` | INTERNAL | fetch/constraint/rerank/visual/result 数量 |
| `search rerank text` | CLIENT | 文本判同模型调用耗时与状态 |
| `search verify visual` | CLIENT | 视觉复核调用耗时与状态 |
| `arq publish <job>` | PRODUCER | Redis、任务名 |
| `arq process <job>` | CONSUMER | job id、任务状态、异常 |
| `candidate_pool consume` | CONSUMER | 池状态、命中与否，并链接预取 span |

`session.id` 用于团队内部按会话回放；用户主键在 span 中只以 `user.id_hash`、在 JSON 日志中只以 `userIdHash` 导出。模型调用默认只记录模型名、Token、状态和时延。`OTEL_CAPTURE_CONTENT` 当前保留为显式配置闸门，但代码不因其开启而自动采集正文，后续任何内容采集都必须单独安全评审。

## Grafana 五类视图

预置看板：`Photo Agent · 全链路可观测性`。

1. 系统总览：Trace 错误率、端到端 P95、服务吞吐；通过 Tempo Service Graph 查看 API、Redis、DB、外部模型和 Worker 依赖。
2. Trace 与日志联查：最近异常 Trace + `trace_id` 过滤 Loki；支持从 Trace 瀑布图直接跳日志。
3. Agent 会话回放：按 `session.id` 展示 LLM、工具、搜索/生成步骤，不展示用户正文。
4. 搜索漏斗：查看 `search.*` 属性，判断候选在哪一阶段被过滤，并对比各阶段 P95。
5. Worker/异步任务：查看 ARQ publish/process、重试、预取和失败，producer/consumer 使用同一 Trace。

## 启动与验证

```powershell
# .env 中打开（生产还要修改 Grafana 密码和采样率）
# OTEL_ENABLED=true
# LOG_JSON_FORMAT=true

docker compose config
docker compose up -d --build
docker compose exec api alembic upgrade head
```

入口：Grafana `http://localhost:3000`，Tempo `http://localhost:3200`，Prometheus `http://localhost:9090`。首次登录 Grafana 使用 `.env` 的 `GRAFANA_ADMIN_PASSWORD`。

建议按下面的验收用例执行：

1. 调用 `/search`，记录响应头的 `X-Trace-ID`，在 Tempo 搜索该 ID，并从 Trace 跳到 Loki 日志。
2. 调用 `/agent/stream` 完成一次搜索，确认瀑布图包含 `invoke_agent`、`chat`、`execute_tool`、`search retrieve`。
3. 搜索命中后等待候补池预取，再发送“还有一张”，确认续搜 Trace 中存在指向 Worker 预取的 Span Link。
4. 上传一张照片，确认 API 入队和 `process_photo` Worker 消费属于同一 Trace。
5. 故意使用不可用的模型地址，确认错误 span、熔断/降级日志可以联查，同时观测后端中不存在认证头和模型正文。

## 排期与发布门禁

| 阶段 | 时间 | 状态/工作 | 门禁 |
|---|---|---|---|
| D1 基线与规范 | 2026-08-20 | 已完成：结合知识图谱与最新候补池变更确定边界、字段和脱敏规则 | 原有 Log ID 行为不回退 |
| D2 应用埋点 | 2026-08-20～21 | 已实现：HTTP、日志、Agent、搜索、ARQ、Worker、候补池 Span Link | 单测/静态检查通过 |
| D3 观测栈 | 2026-08-21 | 已实现配置：Collector、Tempo、Loki、Prometheus、Grafana 预置 | Compose 配置和容器配置可加载 |
| D4 联调验收 | 2026-08-22 | 待部署环境执行上述 5 条真实链路验收 | 关键链路完整率 ≥ 95%，日志跳转命中率 100% |
| D5 灰度上线 | 2026-08-23～24 | 先单实例/测试用户，再逐步放量；依据存储量调整采样 | API P95 增幅 < 5%，业务错误率不升高 |
| D6 复盘固化 | 2026-08-25 | 固化告警阈值、值班查询和数据保留策略 | 至少完成一次故障演练 |

当前实现完成的是代码与基础设施交付，不等同于生产验收完成。D4 必须在能访问真实 Redis、PostgreSQL、模型服务和 Docker 镜像的环境执行后，才能把“全链路已打通”标记为生产完成。

## 告警建议

- API/Agent：5 分钟错误率 > 2%，或端到端 P95 > 15 秒。
- 搜索：`search.result_count=0` 比例突增；rerank/visual `degraded=true` 连续 5 分钟。
- Worker：消费错误率 > 2%；相同 job 重试耗尽；publish 后 3 分钟无 consumer。
- Collector：发送失败、队列持续增长、内存限制丢弃数据。
- 隐私：每日抽样检查敏感键，任何命中立即停止内容类采集并清理相关数据。
