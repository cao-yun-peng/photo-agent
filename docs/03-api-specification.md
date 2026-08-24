# API 规范

## 1. 基本约定

- 默认本地地址：`http://localhost:8000`。
- 正式契约：运行实例的 `/openapi.json` 和 `/docs`。
- 除登录、健康检查和开发 Mock OSS 外，业务接口要求
  `Authorization: Bearer <JWT>`。
- 中间件接受 `X-Log-ID` 或 `X-Request-ID`，响应回传 `X-Log-ID`；启用 OTel 时还返回
  `X-Trace-ID`。
- 时间采用 ISO 8601；ID 通常为 UUID 字符串。
- 资源列表使用 `limit/offset`，搜索使用复合 `cursor`。

## 2. 健康检查

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/live` | 仅证明进程能响应，不访问依赖 |
| GET | `/ready` | PostgreSQL 与 Redis 均正常时返回 200，否则 503 |
| GET | `/health` | 综合状态、版本、环境和所有熔断器快照 |

## 3. 认证

| 方法 | 路径 | 请求/响应 |
| --- | --- | --- |
| POST | `/auth/wechat` | `LoginRequest` → `TokenResponse` |
| GET | `/auth/me` | JWT → `UserOut` |

开发环境允许任意非空 code 走 Mock 登录。生产环境必须配置匹配的小程序 AppID/Secret。

## 4. 照片

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/photos/upload-url` | 用户内 hash 去重并返回 PUT 签名 |
| POST | `/photos` | 上传完成回调；校验 OSS 对象并创建任务 |
| GET | `/photos` | 时间线，`limit` 1–100，支持 `offset` |
| POST | `/photos/processing-status/batch` | 批量查询最多 100 张照片处理状态 |
| GET | `/photos/{photo_id}` | 当前用户的照片详情 |
| GET | `/photos/{photo_id}/processing-status` | 单张处理/索引状态 |
| POST | `/photos/{photo_id}/retry-search-index` | 手动补算已失败或耗尽的搜索索引 |
| POST | `/photos/{photo_id}/interact` | 上报 view/favorite/share/download，204 |
| DELETE | `/photos/{photo_id}` | 删除当前用户照片，204 |

上传协议：

```json
POST /photos/upload-url
{
  "hash": "64-char-sha256",
  "size_bytes": 123456,
  "mime_type": "image/jpeg"
}
```

若响应 `duplicate=true`，客户端不再 PUT；否则必须使用返回的 `method`、`upload_url` 和
`headers` 完成直传，再调用 `POST /photos`。单文件 Schema 上限为 100 MiB。

## 5. 搜索

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/search` | 自然语言、日期、标签、分面和混合排序 |
| POST | `/search/click` | 上报结果点击，204 |
| POST | `/search/album-fallback` | 全相册智能兜底排序 |

`POST /search` 的核心请求字段：

- `q`：1–200 字符。
- `result_mode`：`browse`（最多 5）、`best`（最佳 1）、`select`（按用户数量/完整集）。
- `complete_result_set`：仅可与 `select` 组合。
- `from_date`、`to_date`、`tags`、`status`。
- `scene`、`objects`、`text_in_image`、`mood`、`colors`。
- `photo_types`、`is_selfie`、`people_count_min/max`。
- `w_semantic`、`w_recency`、`w_interaction`。
- `auto_parse`、`verify_constraints`、`verify_semantic`。
- `cursor` 与可选 `min_semantic_score`。

响应除 `items` 外还可能包含解析结果、强约束检查、重排/视觉核验摘要、索引覆盖率、阈值
过滤原因和结果集完整性说明。不要假设 `total == items.length`。

## 6. Agent

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/agent/run` | 一次性返回状态与完整事件数组 |
| POST | `/agent/stream` | 相同语义，以 SSE 实时返回事件 |

请求：

```json
{
  "query": "帮我找去年夏天在海边拍的照片",
  "session_id": null,
  "selected_photo_id": null
}
```

`query` 长度为 1–500。续接会话时携带 `session_id`；用户明确点击候选时携带
`selected_photo_id`。

SSE 帧格式：

```text
data: {"type":"start","payload":{},"step":0}

```

事件类型包括 `start`、思考/工具过程事件、`final`、`done` 和 `error`。客户端必须按空行
切帧、支持 UTF-8 跨 chunk 解码，并在 `done` 或 `error` 后结束读取。

## 7. Skill 与生成

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/skills` | 官方 + 当前用户 Skill |
| GET | `/skills/plaza` | 官方 + 公开 Skill |
| GET | `/skills/{skill_id}` | 可见 Skill 详情 |
| POST | `/skills` | 创建自定义 Skill |
| PATCH | `/skills/{skill_id}` | 修改自己的非官方 Skill |
| DELETE | `/skills/{skill_id}` | 删除自己的非官方 Skill |
| GET | `/skills/_/quota` | 今日生成额度 |
| POST | `/photos/{photo_id}/generate` | 创建待确认生成任务，202 |
| POST | `/generations/{generation_id}/confirm` | 校验 token、预占额度并入队，202 |
| GET | `/generations` | 当前用户生成历史 |
| GET | `/generations/{generation_id}` | 生成状态与签名结果 URL |

生成请求支持 `skill_id`、`extra_prompt`、模型和 `idempotency_key`。推荐所有客户端为一次用户
操作生成稳定幂等键；网络重试时复用同一键。

## 8. 管理接口

`/admin/refresh`、`/admin/refresh/skills`、`/admin/refresh/prompts` 和 `/admin/stats`
用于热刷新注册表。目前 `_verify_admin` 在非开发环境也会放行，属于明确的生产阻塞风险；
在修复前必须由反向代理限制来源或完全禁止这些路径。

## 9. 错误与重试

- Pydantic 输入错误由 FastAPI 返回 422。
- 未认证或 JWT 无效由统一 `ApiError` 协议返回；客户端应优先展示 `errMsg/detail/message`。
- 409 常用于会话并发、确认 token 无效/过期或状态冲突。
- 429 表示生成额度耗尽。
- 503 可表示依赖未就绪、生成入队失败或外部服务降级。
- 仅对幂等 GET，或带稳定 `idempotency_key` 的生成准备请求做自动网络重试。
- 使用响应中的 `X-Log-ID`/`X-Trace-ID` 关联服务端日志，不记录 JWT 或签名 URL。
