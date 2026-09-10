# 第一批修复交付与运行说明

日期：2026-09-05。基于 Git `d2983f1` 的工作树修改，对应优化方案 PR 00–04。未发布。

## 已实现

| 范围 | 行为 |
|---|---|
| Python CI | Python 3.12、全量 Ruff、pytest、真实 PostgreSQL/Redis 集成测试、迁移升级/回退/再升级；后端修改触发 Web CI |
| Admin | 默认关闭；启用后要求有效用户 JWT 和 UUID 白名单，移除开发模式认证绕过 |
| Mock OSS | 只允许显式启用的 dev/test；带方法、路径、过期时间、类型和大小上限的签名；路径约束、流式限额、临时文件原子替换 |
| Agent/SSE | 执行任务拥有独立 DB 会话；断线、超时和失锁取消执行；Redis 续租失败通知与数据库提交令牌防止旧任务提交 |
| Photo Worker | 原子领取、数据库租约、处理次数上限、显式 Retry、描述/分析检查点复用、启动及每 30 秒恢复扫描；调度租约避免扫描器重复派发 |
| 预取 | 按用户/会话/搜索代次隔离；Redis Lua 原子判断当前代次；旧任务停止或拒绝发布；重复入队保留候选；消费时复核归属、状态和排除项并重新签 URL |

进程被强制结束时无法执行清理，由持久化租约到期后的扫描恢复。第三方调用若忽略取消，清理只等待限定时间并继续追踪任务；数据库提交令牌与失锁标记阻止旧执行提交，不能保证撤回已发送的外部请求。

## 配置与升级

1. 暂停旧 API/Worker 并等待在途任务结束。备份数据库；旧 Worker 不支持新租约，禁止与新 Worker 混跑。
2. 使用应用部署环境执行 `alembic upgrade head`，目标 `20260905_0001`。本迁移仅增加用户执行令牌和照片处理租约/调度字段及索引。
3. 配置后启动新版 API/Worker。开发本地存储需要 `APP_ENV=dev`、`OSS_BACKEND=mock`、`MOCK_OSS_ENABLED=true`。生产使用 `APP_ENV=production`、`OSS_BACKEND=oss`、`MOCK_OSS_ENABLED=false`、真实 OSS 配置和足够强的 JWT 密钥；启动检查会拒绝不安全组合。
4. 管理接口默认 `ADMIN_ENABLED=false`。需要时显式启用并设置 `ADMIN_USER_IDS=["用户UUID"]`，请求同时携带对应登录 JWT。
5. 检查 pending/processing 恢复、失败原因、失锁日志和预取状态；旧无签名 Mock URL 失效，需要重新获取。旧预取任务被丢弃，后续搜索产生新代次。

可配置项：`MOCK_OSS_MAX_UPLOAD_BYTES` 默认 20 MiB；`PHOTO_PROCESSING_LEASE_SECONDS` 默认 60；`PHOTO_PROCESSING_MAX_ATTEMPTS` 默认 3；`PHOTO_RECOVERY_BATCH_SIZE` 默认 100；`TASK_CLEANUP_TIMEOUT_SECONDS` 默认 5。重试上限是数据库中的累计处理次数，手工重新处理已耗尽照片需经过明确的运维流程。

回退时先停止新版进程。数据库新增列可先保留；测试环境已验证 `alembic downgrade 20260822_0002` 后再次升级。降级会删除租约与尝试记录，不应在任务运行中执行。旧版 Admin/Mock 安全缺陷不能通过回退重新开放。

## 可复现验证

使用 `tests/compose.batch1.yml` 启动独立测试服务：

```powershell
docker compose -p photo-agent-batch1-test -f tests/compose.batch1.yml up -d --wait
$env:PHOTO_AGENT_TEST_DATABASE_URL='postgresql+asyncpg://batch1:batch1_test_only@127.0.0.1:55439/photo_agent_batch1_test'
$env:PHOTO_AGENT_TEST_REDIS_URL='redis://127.0.0.1:56389/15'
$env:DATABASE_URL=$env:PHOTO_AGENT_TEST_DATABASE_URL
$env:APP_ENV='test'
python -m alembic upgrade head
python -m ruff check app tests scripts
python -m pytest -q
```

测试数据库名必须为 `photo_agent_batch1_test` 且使用回环地址。测试覆盖真实数据库领取竞争、旧提交拒绝、强杀 Worker 后恢复、真实 ASGI 断线、Redis 代次竞争；模型调用使用桩，未调用付费模型或访问用户照片。

历史迁移与 ORM 已存在表、索引、类型和约束差异，原始 `alembic check` 尚不能通过。`tests/schema_drift_baseline.txt` 记录完整已知差异；测试比较规范化后的完整差异以禁止新增漂移。基线不是迁移脚本，不可执行，不可盲目重新生成。

本批未覆盖真实模型质量、浏览器完整 E2E、负载/成本、生产部署及网络/代理配置验收。后续统一 SearchService、前端修复和分层评测仍按原方案推进；阶段 6 发布 Gate 保持阻塞。
