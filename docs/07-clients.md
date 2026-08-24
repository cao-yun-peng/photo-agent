# 客户端

## 1. 客户端边界

仓库包含两个客户端：

- `web/`：React 19 + Next.js/Vinext + TypeScript，覆盖登录、上传、时间线、搜索、Skill 和生成。
- `miniprogram/`：微信小程序原生实现，覆盖相同主要闭环并使用 chunked SSE。

两端都只保存 JWT 和短期会话状态；业务事实保存在后端。对象存储上传由客户端直接执行。

## 2. Web

### 2.1 页面

| 路径 | 功能 |
| --- | --- |
| `/login` | 开发登录/微信登录入口 |
| `/photos` | 照片时间线 |
| `/upload` | 选择、校验、hash、签名、PUT、回调和状态轮询 |
| `/search` | Agent 对话、普通检索和结果交互 |
| `/skills` | Skill 广场和个人 Skill |
| `/skills/new`、`/{id}/edit` | Skill 编辑 |
| `/generate`、`/generate/{skillId}` | 选图并准备生成 |
| `/generations` | 生成历史和轮询 |

API 访问集中在 `web/lib/api/`；`openapi-fetch` 使用生成的
`web/lib/api/generated.ts` 类型。接口发生变化后运行：

```bash
cd web
npm run api:types
```

### 2.2 本地运行

Node 版本要求 `>=22.13.0`：

```powershell
Set-Location web
npm ci
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
npm run dev
```

默认端口 3001。`NEXT_PUBLIC_API_ORIGIN` 指向 API；
`NEXT_PUBLIC_ENABLE_DEV_LOGIN` 只应在开发环境开启。

### 2.3 上传

`file-policy.ts` 校验文件，`hash-file.ts` 在 Worker 中计算 hash，`put-file.ts` 执行签名 PUT。
页面完成回调后批量轮询 `processing-status/batch`，不要用固定 sleep 假设 AI 已处理完成。

### 2.4 SSE

Web 使用 fetch 流而非原生 EventSource，因为请求需要 POST JSON 和 Authorization。解析器必须：

- 支持一个 chunk 包含多个帧。
- 支持一个帧跨多个 chunk。
- 只拼接 `data:` 行。
- 处理流内 `error` 和最终 `done`。
- 保留并回传 `session_id` 续接会话。

## 3. 微信小程序

`app.json` 注册登录、时间线、上传、搜索、Skill 编辑、生成和历史页面；TabBar 包含时间线、
搜索、广场和上传。

API 地址在 `miniprogram/utils/config.js`。开发工具可使用 `localhost` 并关闭域名校验；真机必须
改为同网段地址或有 HTTPS 的公网域名。

`utils/api.js` 负责：

- 自动添加 Bearer JWT 和 `X-Log-ID`。
- 从响应头保存 LogID/TraceID。
- 统一 REST 错误结构。
- 对 `wx.request(enableChunked=true)` 做 UTF-8 流式解码和 SSE 分帧。

## 4. 认证与状态

- 登录后保存 `access_token`；客户端不得解析 token 决定权限，权限以 API 响应为准。
- 收到 401/无效 token 时清理会话并回到登录页。
- Agent 新对话清空 `session_id`、消息和候选；追问使用相同 `session_id`。
- 生成准备后展示费用和过期时间，再调用 confirm；网络重试复用任务 ID 和确认 token。

## 5. 生产交付

Web Dockerfile 有四个阶段：dependencies、build、runtime 和 Nginx gateway。Compose 中：

- `web-runtime` 在 3001 运行 Vinext standalone server。
- `web-gateway` 对外暴露 `${WEB_PORT:-8080}:80`，代理 `/api` 到后端并提供静态资源。

构建参数使用 `NEXT_PUBLIC_API_ORIGIN=/api`，避免浏览器绕过网关访问内部 API。

## 6. 客户端验证

```bash
cd web
npm run lint
npm run typecheck
npm run test
npm run build
npm run test:e2e
```

E2E 覆盖主路径、响应式和可访问性。小程序当前依赖微信开发者工具进行手工验证；仓库没有
自动化小程序 UI 测试。

## 7. 已知边界

- 小程序语音只录音，不做 ASR。
- 小程序 API 地址仍是源码常量，正式发布应改为环境化构建配置。
- Web 的 Cloudflare/Sites 本地绑定来自 `web/.openai/hosting.json`；Docker 交付不依赖这些云
  绑定，但修改 Vite 配置时需要保持该文件存在。
- 客户端不能把 Mock 登录开关带到公开生产构建。
