# 项目总览

## 1. 项目定位

Photo Agent 是面向中文照片管理场景的多端应用，将以下能力串成一个闭环：

1. 用户从 Web 或微信小程序选择照片并直接上传到对象存储。
2. 后台任务提取 EXIF、生成缩略图、调用视觉模型并建立向量索引。
3. 用户用中文自然语言、时间、照片类型或人数等条件查找照片。
4. Photo Agent 负责多轮澄清、搜索、选图和发起 Skill 图像生成。
5. 生成任务经过显式确认、额度预占和异步执行，结果回写对象存储与数据库。

项目同时支持“无云密钥本地闭环”和真实云服务模式。占位密钥会启用确定性 Mock，
便于开发；真实环境可接入 OSS、DashScope 与 OpenAI Images。

## 2. 核心技术栈

| 区域 | 实现 |
| --- | --- |
| API | Python 3、FastAPI、Pydantic v2、Uvicorn |
| 数据 | PostgreSQL 16、SQLAlchemy Async、Alembic、pgvector 1024 维向量 |
| 异步任务 | Redis 7、ARQ Worker |
| AI | Qwen VL、text-embedding-v3、Qwen Chat、通义万相、OpenAI Images |
| Web | React 19、Next.js 16、Vinext、TypeScript、TanStack Query |
| 小程序 | 微信小程序原生 JavaScript、WXML、WXSS |
| 可观测性 | OpenTelemetry、Tempo、Loki、Prometheus、Grafana |
| 交付 | Docker、Docker Compose、Nginx、GitHub Actions |

## 3. 用户可见能力

- 微信 code 登录和 JWT 会话。
- SHA-256 用户内去重、签名 URL 直传、批量处理状态轮询。
- 时间线、详情、删除和浏览/收藏/分享/下载事件上报。
- 中文语义检索、结构化硬条件、三种结果模式和游标分页。
- Agent 普通响应与 SSE 流式响应、多轮会话和显式工作流守卫。
- 官方/公开/私有 Skill，Skill 创建、修改、删除和广场浏览。
- 图像生成预览确认、每日额度、幂等提交、异步状态轮询。

## 4. 主要目录

```text
photo-agent/
├── app/
│   ├── api/          # HTTP 与 SSE 路由
│   ├── core/         # 安全、错误、日志、遥测和注册表
│   ├── models/       # SQLAlchemy ORM
│   ├── schemas/      # Pydantic API 契约
│   ├── services/     # Agent、检索、OSS、AI、生成等领域逻辑
│   └── workers/      # ARQ 后台任务
├── alembic/          # 数据库迁移
├── web/              # Web 客户端与浏览器测试
├── miniprogram/      # 微信小程序
├── observability/    # Collector、Tempo、Loki、Prometheus、Grafana
├── scripts/          # 当前保留的 Agent/VL 评测与真实 E2E 脚本
├── tests/            # 当前保留的 Python 测试和评测数据集
└── docs/             # 稳定设计与运维文档
```

## 5. 本地启动

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose restart api worker
Invoke-RestMethod http://localhost:8000/ready
```

成功标准：`/ready` 返回 HTTP 200，且 `database`、`redis` 均为 `ok`。Swagger UI
位于 `http://localhost:8000/docs`。

## 6. 设计原则

- 大图片不经过 API 转发；客户端通过签名 URL 直传对象存储。
- 请求身份由 JWT `sub` 绑定用户，照片/会话/生成任务查询均应携带用户过滤。
- HTTP 请求和 ARQ 任务通过 W3C Trace Context 与 LogID 串联。
- AI 服务允许降级；照片元数据和缩略图成功时可以进入 `partial_done`。
- 生成是有成本操作，必须经历准备、确认、额度预占和幂等保护。
- 检索优先保证硬约束正确性，视觉核验仅在配置开启且满足触发条件时执行。

## 7. 当前边界

- 管理端 `/admin/*` 仍是开发态认证实现，生产部署前必须在应用或网关补充强认证。
- `SEARCH_VISUAL_VERIFY_ENABLED` 默认关闭；启用前应针对自己的数据重新评估费用和延迟。
- 小程序语音按钮只完成录音交互，尚未接入 ASR。
- 当前 Python 自动化测试比历史版本精简，不能将 README 中的历史实验数字视为当前回归结果。
- 生产环境仍需提供 HTTPS、真实微信配置、受限 OSS CORS、密钥托管和告警通知。
