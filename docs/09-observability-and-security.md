# 可观测性与安全

## 1. 关联标识

每个 HTTP 请求从 `X-Log-ID`、`X-Request-ID` 或 cookie 读取关联 ID；仅接受 1–64 位
字母数字及 `._:-`，否则生成新的 16 位 ID。响应返回 `X-Log-ID`，有效 Trace 存在时返回
`X-Trace-ID`。

API 入队时注入 W3C Trace Context 和 LogID，Worker 通过 `traced_job` 恢复为 consumer span，
从而串联：

```text
浏览器/小程序 -> FastAPI -> Redis publish -> ARQ consume -> AI/OSS/DB
```

## 2. 日志

生产 JSON 日志包含时间、级别、应用、logger、源码位置、LogID、用户 ID 哈希、路径、TraceID、
SpanID 和消息。用户 ID 不直接导出到观测系统。

开发环境可用彩色控制台。可选文件日志采用 10 MiB 轮转、保留 5 份；容器环境更推荐 stdout
交给日志平台收集。

健康检查、Swagger 和 OpenAPI 路径默认不记录访问日志，减少噪声。客户端断开以 499 记录。

## 3. OpenTelemetry

`OTEL_ENABLED=true` 后：

- FastAPI 自动创建 server span。
- HTTPX URL 查询参数被剥离，避免泄露 OSS 签名。
- Redis span 只保留命令名，不导出队列 Payload 或查询正文。
- SQLAlchemy、HTTPX、Redis 自动埋点。
- Trace 和可选日志通过 OTLP HTTP 导出。
- `OTEL_TRACE_SAMPLE_RATIO` 限制采样比例。

默认不采集用户问题、Prompt、模型回复或图片内容。`OTEL_CAPTURE_CONTENT` 即使存在也不应在未
完成隐私评审时开启。

## 4. 观测拓扑

| 组件 | 用途 | 默认端口 |
| --- | --- | ---: |
| OTel Collector | 接收 OTLP，转发 Trace/Log/指标 | 4317/4318/8888 |
| Tempo | Trace 存储与查询 | 3200 |
| Loki | 日志存储与查询 | 3100 |
| Prometheus | 指标采集/远程写入接收 | 9090 |
| Grafana | 统一查询和看板 | 3000 |

`observability/grafana/` 已提供数据源和 Photo Agent 看板 provisioning。

## 5. 健康与熔断

`/health` 返回 VL、Embedding、Agent LLM、文本重排、视觉核验、图像生成和 OSS 熔断器快照。
熔断器用于快速失败和恢复探测，不替代超时、重试和业务降级。

推荐告警：

- `/ready` 连续 2–3 分钟失败。
- API 5xx/499、Worker 失败或队列等待时间异常上升。
- 任一熔断器长时间 OPEN。
- `partial_done`、`embedding_retrying`、`queue_failed` 比例突增。
- 生成额度预占长期不释放。
- Trace/日志停止上报或 Collector 丢弃数据。

## 6. 已实现安全控制

- JWT HS256，有 `iat`、`exp` 和用户 UUID `sub`。
- FastAPI 依赖从 JWT 加载当前用户。
- 照片、生成和会话查询执行用户所有权过滤。
- 上传采用用户内 SHA-256 去重，并在回调时重新验证 OSS 对象。
- CORS 由显式 Origin 列表控制。
- 生成使用确认 token、过期时间、幂等键和原子额度预占。
- 外部调用有超时、熔断和错误正文截断/分类。
- Trace 默认脱敏 URL、Redis Payload 和用户标识。

## 7. 生产阻塞风险

### P0：管理接口认证未完成

`app/api/admin.py::_verify_admin` 当前在生产分支仍返回 true。修复前必须在网关阻断
`/admin/*` 或实现管理员 token/身份认证、审计和最小权限。

### P0：开发配置泄露

生产必须关闭开发登录、Mock 服务和 Web dev login；不能使用 `.env.example` 中的占位密钥、
默认数据库密码或默认 Grafana 密码。

### P1：网络暴露

Compose 为开发便利暴露了数据库、Redis、Tempo、Loki、Prometheus 和 Grafana 端口。生产应
放入私网或仅允许管理网络访问，对外只开放 HTTPS gateway。

### P1：转发头信任

LogID 中间件直接读取 `X-Forwarded-For`/`X-Real-IP`。只有在受信反向代理覆盖这些头时，
客户端 IP 才可信；安全决策不能直接依赖该值。

## 8. 安全变更清单

- 新端点：认证、所有权、输入上限、错误脱敏和速率限制。
- 新日志/Trace：确认没有 token、签名 URL、Prompt、图片、openid 或原始用户内容。
- 新外部服务：连接/读取超时、熔断、重试边界和凭据最小权限。
- 新对象键：用户命名空间、不可猜测名称、短期签名和删除策略。
- 新管理能力：强认证、审计日志、来源限制和回滚方案。
