# Embedding 补算与客户端短轮询

## 目标

照片的 VL 描述和结构化分析已经成功，但 embedding 服务短时不可用时，不让用户重复上传，
也不重复调用 VL。系统保存已有成果，仅补算缺失向量，并让客户端明确展示当前进度。

## 重试规则

- 单次 embedding HTTP 超时保持 **15 秒**；
- 每张照片最多 **5 次真实模型调用**：首次调用 + 4 次补算；
- 每次真实失败结束后再等待：**2 秒、8 秒、25 秒、60 秒**；
- 等待时间是相对间隔，不是从首次上传开始累计；
- 全部 5 次都超时的最坏耗时约为 `5 × 15 + 2 + 8 + 25 + 60 = 170 秒`，实际 HTTP
  提前失败时通常更短；
- 重试任务只调用 embedding，复用数据库中的 `ai_description` 和 `ai_analysis`。

状态流转：

```text
processing
  ├─ embedding 成功且门禁通过 → done / ready
  └─ embedding 真实失败 → partial_done / retrying
       ├─ 等 2s → 第 2 次
       ├─ 等 8s → 第 3 次
       ├─ 等 25s → 第 4 次
       ├─ 等 60s → 第 5 次
       ├─ 任一次成功 → done / ready
       └─ 第 5 次仍失败 → partial_done / embedding_retry_exhausted
```

重试耗尽不会删除 VL 产物。照片仍可展示、按结构化字段浏览，但不能通过向量文字检索；用户
可调用手动重试接口开启新一轮 5 次尝试。

## 熔断器语义

embedding 熔断器连续失败达到阈值后进入 `open`：新任务不会请求 DashScope，而是立即返回
“服务繁忙”。因此被熔断拒绝不算一次真实调用，也不增加照片的 `embedding_retry_count`。

30 秒恢复窗口结束后进入 `half_open`，只允许一个真实探测请求：

- 探测成功：回到 `closed`，后续请求正常执行；
- 探测失败：重新 `open` 并从失败时重新计 30 秒；
- 同时到达的其他照片不抢探测名额，不消耗各自重试次数。

## 客户端接口

### 单张轮询

```http
GET /photos/{photo_id}/processing-status
```

响应示例：

```json
{
  "photo_id": "...",
  "photo_status": "partial_done",
  "search_index_status": "retrying",
  "retry_count": 2,
  "max_attempts": 5,
  "next_retry_at": "2026-08-15T12:00:08Z",
  "next_retry_in_seconds": 7,
  "message": "智能搜索服务繁忙，正在继续尝试"
}
```

### 批量轮询

```http
POST /photos/processing-status/batch
Content-Type: application/json

{"photo_ids": ["uuid-1", "uuid-2"]}
```

一次最多 100 个 ID。响应保持请求顺序并自动去重；不存在或不属于当前用户的 ID 不回显。

### 手动重试

```http
POST /photos/{photo_id}/retry-search-index
```

仅 `embedding_retry_exhausted` 或 `embedding_retry_enqueue_failed` 可手动重试。已经就绪时直接
返回 `ready`；仍在自动处理时返回 HTTP 409，避免建立两条并发重试链。

### 搜索覆盖提示

`POST /search` 和 `POST /search/album-fallback` 的响应新增 `index_coverage`：

```json
{
  "total_photos": 25,
  "indexed_photos": 22,
  "retrying_photos": 2,
  "unavailable_photos": 1,
  "coverage_ratio": 0.88,
  "message": "2 张仍在建立智能搜索；1 张暂时无法被文字检索，当前结果可能不完整"
}
```

这样用户不会把“搜索没找到”误解为“相册里没有”。

## 小程序短轮询建议

第一版不需要 WebSocket。上传回调成功后立即显示照片卡片，并按以下节奏轮询：

1. 前 30 秒每 2 秒批量轮询一次；
2. 之后每 5 秒一次；
3. 所有照片进入 `ready` 或 `unavailable` 后停止；
4. 页面进入后台时暂停，回到前台时立即补一次批量查询；
5. `next_retry_in_seconds` 只用于展示倒计时，不需要客户端自己触发自动补算。

## 配置与迁移

```env
EMBEDDING_MAX_ATTEMPTS=5
EMBEDDING_RETRY_DELAYS_SECONDS=[2,8,25,60]
CB_EMBEDDING_RECOVERY_INTERVAL=30
```

部署前执行：

```bash
alembic upgrade head
```

迁移会新增 `embedding_retry_count`、`embedding_next_retry_at`、
`embedding_last_attempt_at`、`embedding_last_error` 四个持久化字段。数据库只记录异常类型或
稳定原因码，不保存第三方响应正文和密钥。
