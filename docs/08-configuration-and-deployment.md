# 配置与部署

## 1. 配置加载

后端通过 Pydantic Settings 从进程环境和项目根 `.env` 读取配置，不区分大小写，未知字段忽略。
`get_settings()` 使用进程内缓存；普通环境变量变更需要重启 API/Worker。Skill 和 Prompt 注册表
支持管理接口热刷新，但不等价于重载所有 Settings。

不要提交 `.env`。`.env.example` 只提供开发模板。

## 2. 配置分组

### 2.1 基础与数据

| 变量 | 说明 |
| --- | --- |
| `APP_ENV` | `dev` 启用开发行为；生产使用 `prod` 等明确值 |
| `LOG_LEVEL`、`LOG_JSON_FORMAT` | 日志级别与格式 |
| `CORS_ORIGINS` | 允许的浏览器 Origin 列表 |
| `DATABASE_URL` | asyncpg 连接串 |
| `REDIS_URL` | ARQ、缓存、锁和候选池共用 Redis |

### 2.2 认证与存储

| 变量 | 说明 |
| --- | --- |
| `JWT_SECRET`、`JWT_EXPIRE_MINUTES` | JWT 签名与有效期 |
| `WECHAT_APPID`、`WECHAT_SECRET` | 真实微信 code2session |
| `OSS_ENDPOINT`、`OSS_BUCKET` | 对象存储位置 |
| `OSS_KEY_ID`、`OSS_KEY_SECRET` | OSS 凭据 |
| `OSS_UPLOAD_TTL` | 上传签名 TTL，默认 900 秒 |

### 2.3 AI 与生成

| 变量 | 说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` | VL、Embedding、Chat 的主凭据 |
| `QWEN_VL_MODEL`、`QWEN_EMBEDDING_MODEL`、`QWEN_CHAT_MODEL` | 模型选择 |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL` | OpenAI 图像/可选 Agent 适配 |
| `GEN_DAILY_FREE_QUOTA` | 每日生成额度 |
| `GENERATION_CONFIRMATION_TTL_SECONDS` | 生成确认窗口 |
| `GENERATION_ESTIMATED_COST_YUAN` | 确认页费用估算，不代表实际账单 |

### 2.4 搜索与 Agent

- `EMBEDDING_MAX_ATTEMPTS`、`EMBEDDING_RETRY_DELAYS_SECONDS`。
- `SEARCH_RERANK_*`：Top-K、拒绝置信度、超时、缓存和匹配要求。
- `SEARCH_VISUAL_VERIFY_*`：视觉核验开关、Top-K、触发分差、超时和 TTL。
- `SEARCH_SEMANTIC_MIN_SCORE`：全局硬阈值，0 表示关闭。
- `AGENT_SEARCH_*`：候选池、索引修复、搜索/视觉预算和 TTL。
- `AGENT_MAX_TOTAL_TOKENS` 及 Settings 中的时间/费用/工具超时。
- `AGENT_V2_*`：稳定灰度、百分比、salt 和 kill switch。

### 2.5 可观测性和熔断

`OTEL_ENABLED` 默认 false；生产开启后配置服务名、OTLP endpoint、采样率、日志导出和排除路径。
`OTEL_CAPTURE_CONTENT` 应保持 false，除非经过隐私审查。

`CB_*_RECOVERY_INTERVAL` 控制各外部服务熔断恢复窗口；修改时需要同时观察失败率与恢复风暴。

## 3. 本地 Compose 拓扑

基础 `docker-compose.yml` 启动：

- `api`：Uvicorn 8000，开发热更新。
- `worker`：ARQ Worker。
- `db`：`pgvector/pgvector:pg16`，端口 5432。
- `redis`：Redis 7，端口 6379。
- `otel-collector`：4317/4318/8888。
- `tempo`：3200。
- `loki`：3100。
- `prometheus`：9090。
- `grafana`：3000。

本地 Mock OSS 使用命名卷 `ossmock` 在 API 和 Worker 间共享。

## 4. Web 交付

组合启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.web.yml exec api alembic upgrade head
```

浏览器访问 `http://localhost:${WEB_PORT:-8080}`。Nginx gateway 暴露 Web，并通过同源 `/api`
访问后端。

## 5. 生产部署顺序

1. 在密钥管理系统配置 JWT、数据库、Redis、微信、OSS 与模型凭据。
2. 构建不可变镜像；不要在生产挂载源码和使用 `--reload`。
3. 备份数据库并执行 `alembic upgrade head`。
4. 启动/滚动更新 Redis、数据库依赖、Worker、API、Web runtime 和 gateway。
5. `/live` 检查进程，`/ready` 作为流量门禁，`/health` 查看熔断器。
6. 验证登录、签名上传、处理轮询、搜索、Agent SSE 和生成确认。
7. 检查 Grafana 中 API/Worker Trace 和错误日志。

## 6. 生产必改项

- 使用高强度随机 `JWT_SECRET`；轮换策略需考虑现有 token 失效。
- 将 PostgreSQL、Redis 和 Grafana 端口限制在私网，不直接暴露公网。
- 对外只开放 HTTPS gateway；微信与 OSS CORS 使用精确域名白名单。
- 关闭开发登录和 Mock OSS/AI。
- 为 `/admin/*` 增加强认证或在网关完全阻断。
- 使用非默认 Grafana 密码和独立数据库凭据。
- 去掉 API/Worker 源码卷和 Uvicorn `--reload`。
- 为数据库、对象存储和监控数据制定备份/保留策略。

## 7. 配置变更检查

- 环境变量新增后同步 `Settings`、`.env.example`、Compose 和本文件。
- 布尔/列表变量在 Pydantic 中的解析方式应在实际容器环境验证。
- 模型、阈值、采样率和灰度比例属于行为配置，变更需要记录原因、负责人和回滚值。
