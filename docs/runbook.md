# 运维手册

## 1. 服务清单

| 服务 | 健康入口 | 关键依赖 |
| --- | --- | --- |
| API | `/live`、`/ready`、`/health` | PostgreSQL、Redis、OSS、AI |
| Worker | 日志/Trace/队列活动 | Redis、PostgreSQL、OSS、AI |
| Web gateway | `/login`、`/api/ready` | web-runtime、API |
| PostgreSQL | `pg_isready` | 数据卷 |
| Redis | `PING` | 数据卷 |
| Grafana | 3000 | Tempo、Loki、Prometheus |

## 2. 启动与停止

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose restart api worker
docker compose ps
```

包含 Web：

```bash
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d --build
```

停止但保留数据卷：

```bash
docker compose stop
```

不要在未确认备份和目标环境时执行 `down -v`，它会删除数据库、Redis、Mock OSS 和观测数据卷。

## 3. 发布流程

1. 记录当前镜像、Git SHA、环境配置版本和数据库 revision。
2. 备份 PostgreSQL；必要时备份对象存储元数据。
3. 构建并扫描新镜像。
4. 在副本库运行 `alembic upgrade head` 和关键查询。
5. 正式迁移数据库。
6. 先更新 Worker，再更新 API/Web，或按兼容性说明执行双向兼容滚动。
7. 检查 `/ready`、主路径、队列积压、错误率、熔断器和 Trace。
8. 记录发布结果与回滚点。

## 4. 常用检查

```bash
docker compose ps
docker compose logs --tail=200 api
docker compose logs --tail=200 worker
docker compose exec api alembic current
docker compose exec api alembic heads
curl -fsS http://localhost:8000/live
curl -fsS http://localhost:8000/ready
curl -fsS http://localhost:8000/health
```

从客户端问题开始排查时，先取得 `X-Log-ID` 和 `X-Trace-ID`，再在 Loki/Tempo 中关联。

## 5. 故障：`/ready` 返回 503

1. 查看响应中的 `database` 与 `redis` 字段。
2. `docker compose ps db redis` 检查容器状态。
3. PostgreSQL：检查凭据、连接数、磁盘和迁移锁。
4. Redis：检查内存、持久化错误和连接上限。
5. 依赖恢复后再次请求 `/ready`；不要只重启 API 掩盖根因。

## 6. 故障：照片长期 pending/indexing

1. 查看 Worker 是否运行及 Redis 是否有积压。
2. 使用照片 ID/LogID 搜索 `process_photo` 日志和 Trace。
3. 检查 OSS 对象是否存在、大小是否匹配。
4. 查看 `photos.status`、`partial_reason` 和 Embedding 重试字段。
5. 若照片主体处理成功但索引失败，调用
   `POST /photos/{id}/retry-search-index`，不要重新跑 VL。
6. 批量异常时先检查 Embedding 熔断器和模型配额，再限速恢复。

## 7. 故障：搜索无结果或结果异常

1. 查看响应 `index_coverage` 和 `coverage_hint`。
2. 检查是否有未完成/不可用向量。
3. 查看 `constraint_check` 是否过度过滤。
4. 查看 `rerank_check.degraded_reason`、拒绝计数和视觉核验状态。
5. 对 `complete_result_set` 检查硬分面覆盖率，而不是只看向量召回。
6. 不要在生产直接放宽阈值；先用隔离评测集对比 Recall/Precision。

## 8. 故障：Agent 409、超时或重复结果

- 409/忙：检查用户/会话 Redis 锁是否仍在 TTL 内；不要手工删除不确定归属的锁。
- 超时：区分总预算、单工具超时、LLM 超时和搜索视觉预算。
- 重复结果：检查 `rejected_photo_ids`、`active_search` 和候选池 TTL。
- 会话无法续接：检查 `agent_sessions.status/expires_at` 和 session 是否属于当前用户。
- v2 异常：先启用 `AGENT_V2_KILL_SWITCH` 回到 control，再调查分桶和事件。

## 9. 故障：生成停滞或额度异常

1. 查看 `generations.status`、`enqueue_status`、`attempt_count`、`last_error_code`。
2. `awaiting_confirmation`：确认 token 是否过期，客户端是否调用 confirm。
3. `queue_failed`：修复 Redis/Worker 后使用同一任务和 token 重试确认。
4. `processing/retryable_failed`：检查图像生成、下载和 OSS Trace。
5. `quota_reserved=true` 长期未释放：确认任务已不可恢复后，使用受审计的事务修复额度；
   不要直接把 `gen_count` 清零。

## 10. 熔断器恢复

`/health` 提供各熔断器状态。OPEN 时先修复下游和配额，再等待 recovery interval 的探测；
避免同时重启大量 Worker 造成恢复风暴。需要临时降载时降低 Worker 并发，而不是关闭质量门禁。

## 11. 数据库备份与恢复

示例逻辑备份（命令和保留位置按部署平台调整）：

```bash
pg_dump --format=custom --dbname "$DATABASE_URL" --file photo-agent.dump
pg_restore --list photo-agent.dump
```

恢复演练必须在隔离实例执行，并验证：Alembic revision、用户/照片数量、向量维度、生成额度和
对象键抽样。数据库备份不包含 OSS 原图，需要独立对象存储版本/生命周期策略。

## 12. 回滚

- 应用回滚优先使用上一不可变镜像。
- 数据库只在确认 downgrade 安全且新数据兼容时执行 `alembic downgrade`。
- 含破坏性 Schema 的发布应采用 expand/contract，保证旧新应用可同时运行。
- 配置回滚记录旧值，模型/阈值/灰度可先回滚配置，再决定是否回滚镜像。

## 13. 事故记录最小字段

- 开始/恢复时间、影响用户和功能。
- Git SHA、镜像、数据库 revision、配置变更。
- LogID/TraceID、关键状态和熔断器快照。
- 临时缓解、根因、永久修复、验证和负责人。
- 是否涉及用户图片、Prompt、token 或签名 URL；如涉及立即进入安全事件流程。
