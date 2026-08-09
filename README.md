# photo-agent

> 中文语境 · 隐私优先 · AI 语义搜索 & 二次创作照片管家 — MVP 已完成 D1–D17（**后端 + 小程序 + AI 改造 + 用户 Skill 生态**）

## 目录结构

```
photo-agent/
├── app/                     # FastAPI 后端
│   ├── main.py              # 服务入口
│   ├── config.py            # Pydantic Settings
│   ├── database.py          # 异步 SQLAlchemy 引擎
│   ├── core/
│   │   └── security.py      # JWT 与当前用户依赖
│   ├── models/              # ORM：User / Photo / Tag / PhotoTag
│   ├── schemas/             # Pydantic 请求/响应体
│   ├── api/                 # 路由：auth / photos / search
│   ├── services/            # 业务：wechat / oss / ai / image / search / query_parser
│   └── workers/             # ARQ 任务：process_photo
├── alembic/                 # 数据库迁移
├── miniprogram/             # 微信小程序客户端（D10–D12）
│   ├── pages/               # login / timeline / upload / search
│   ├── utils/               # api / file / config
│   └── README.md            # 小程序端使用说明
├── scripts/                 # 联调脚本（e2e_upload / e2e_ai / e2e_search / setup_oss_cors）
├── docker-compose.yml       # api / worker / db / redis
├── Dockerfile
├── requirements.txt
└── .env.example
```

## 快速启动（60 秒）

```bash
# 1. 准备环境变量
cp .env.example .env
# 用 openssl rand -hex 32 生成 JWT_SECRET 填进去

# 2. 起服务
docker compose up -d --build

# 3. 执行数据库迁移
docker compose exec api alembic upgrade head

# 4. 打开 Swagger UI
open http://localhost:8000/docs
```

启动后可以试的接口：

| 接口                | 说明                                     |
|---------------------|------------------------------------------|
| `GET /health`       | 健康检查                                 |
| `POST /auth/wechat` | 用 `code` 换 JWT（dev 环境用假 code 即可） |
| `GET /auth/me`      | 需要 Bearer Token，验证 JWT 是否可用     |
| `POST /photos/upload-url` | 拿 OSS 直传签名（dev 返回 mock URL） |
| `POST /photos`      | 上传完成回调                             |
| `GET /photos`       | 时间线分页                               |
| `POST /search`      | 语义搜索（当前用 mock 向量）             |

## 联调示例：验证 JWT 闭环

```bash
# 1. 登录（dev 环境不需要真实 wx code）
TOKEN=$(curl -s -X POST http://localhost:8000/auth/wechat \
  -H "Content-Type: application/json" \
  -d '{"code":"test123","nickname":"张三"}' | jq -r .access_token)

# 2. 拿自己的信息
curl http://localhost:8000/auth/me \
  -H "Authorization: Bearer $TOKEN"
# => {"id":"...","nickname":"张三",...}
```

## D3–D4 上传闭环

### 一键端到端验证

```bash
./scripts/e2e_upload.sh
```

脚本会完整走一遍 **登录 → 生图 + 计算 SHA-256 → 请求签名 → PUT 直传 → 上传完成回调 → 拉时间线 → 重复上传去重** 七个步骤，全部通过打印 `✅ D3–D4 闭环全部通过`。

### 客户端接入流程

```
POST /photos/upload-url  →  { upload_url, oss_key, headers, expires_in }
   ↓  按返回的 headers 里 Content-Type 值原样回带
PUT  <upload_url>        →  binary body
   ↓
POST /photos             →  { oss_key, hash, size_bytes, mime_type }
   ← { id, status: "pending", ... }
```

关键约定：

- `hash` 是文件二进制的 **SHA-256**（64 位十六进制），后端用它做同用户去重。
- `PUT` 请求必须回带 `/photos/upload-url` 返回的 `headers`（默认 `Content-Type: image/jpeg`），否则真实 OSS 会拒绝。
- `/photos` 回调会去 OSS 上 `head_object` 校验对象真的存在且大小一致；对象不存在时返回 400，防止客户端"没传就说传完了"。
- 若同 hash 已存在，`/photos/upload-url` 直接返回 `{"duplicate": true}`，客户端可跳过 PUT。

### Dev 模式的假 OSS

`.env` 里 `OSS_KEY_ID=LTAI_xxx` 或 `OSS_BUCKET=photo-agent-dev` 时，签名接口会返回一个本地 mock URL（`/_mock/oss/...`），配套的假 OSS 端点在 `app/api/_oss_mock.py` 提供 `PUT / GET / HEAD` 三个方法，文件落到容器内 `/tmp/photo-agent-oss-mock/`，重启即清。**填入真实 KEY 后 mock 会自动关闭**。

### 生产 OSS Bucket CORS 一次性配置

小程序、H5 都是走浏览器直传，Bucket 必须开 CORS，否则会被 preflight 拒绝：

```bash
# 在填好真实 OSS_KEY_ID 后跑一次
docker compose exec api python scripts/setup_oss_cors.py
```

允许的 Origin 生产上要收窄成自家域名白名单。

## 常用命令

```bash
# 生成新的迁移
docker compose exec api alembic revision --autogenerate -m "add xxx"

# 回滚一个迁移
docker compose exec api alembic downgrade -1

# 看日志
docker compose logs -f api
docker compose logs -f worker

# 进 Python shell
docker compose exec api python

# 停服务
docker compose down

# 彻底清库（连数据卷一起删）
docker compose down -v
```

## D5–D7 AI 处理管道

### 一键端到端验证

```bash
./scripts/e2e_ai.sh
```

脚本在 D3–D4 基础上多做三件事：**入队 → 轮询 status 从 pending 到 done → 打印 AI 生成的中文描述 → 用描述里前几个字做一次语义搜索**。

### AI 处理链路（worker 每收到一张照片就走一遍）

```
1. status = processing
2. OSS get_object → 拿到原图字节
3. Pillow 处理：抽 EXIF（taken_at / GPS） + 生成 512px 缩略图
4. OSS put_object → 缩略图落到 <oss_key>.thumb.jpg
5. sign_get_url → 生成 10 分钟有效的公网可达 URL
6. DashScope qwen-vl-plus → 中文描述
7. DashScope text-embedding-v3 → 1024 维向量
8. 一次性回写：ai_description / embedding / width / height / taken_at / thumb_key / status=done
```

任何一步抛异常都会把 status 置为 failed 并在 worker 日志留栈。

### 关键：填 DashScope API Key

打开 `.env`，把这一行的 `sk-xxx` 换成真实 key，然后重启 worker：

```bash
# .env
DASHSCOPE_API_KEY=sk-你自己的完整 key
QWEN_VL_MODEL=qwen-vl-plus              # 想更快更便宜可以用 qwen-vl-plus
QWEN_EMBEDDING_MODEL=text-embedding-v3
```

```bash
docker compose restart worker
./scripts/e2e_ai.sh
```

**申请入口**：https://dashscope.console.aliyun.com/ → 左侧「API-KEY 管理」→ 创建。

留空或保持 `sk-xxx` 时，`services/ai.py` 自动走 mock 分支——描述是固定占位文案、embedding 是确定性伪随机向量。这样在没配 key 的机器上开发也不会阻塞。

### 真实模式的一个约束：DashScope 需要公网 URL

`qwen-vl` 是把 `image_url` 交给阿里云的服务器去下载图片的，所以：

- **生产环境**：`OSS_BUCKET` 是真实公网可达的 Bucket，`sign_get_url` 返回的 https URL 阿里云能自己拉，一切正常。
- **本地 mock 模式 + 真 key**：`sign_get_url` 返回的是 `/_mock/oss/...` 这种本机相对路径，DashScope 拉不到。想真正跑通有两种做法：
  - 用一个真实 OSS Bucket（花费很低，一天几分钱）
  - 或者先跑 mock 模式验证代码链路，等有真 Bucket 再切

### 客户端不感知这套流程

客户端只需要照旧调 `POST /photos` 这个回调——入队是后端自动的。用户上传后几秒钟再拉一次 `GET /photos/{id}`，就能看到 `status: done` 和 `ai_description: <中文描述>`。

## D8–D9 搜索优化

### 一键端到端验证

```bash
./scripts/e2e_search.sh
```

脚本会上传 3 张假图，然后验证：**多维过滤 / auto_parse / Embedding 缓存 / 游标分页 / 混合排序**。

### 请求参数

`POST /search` 现在支持以下字段：

| 字段 | 说明 |
|---|---|
| `q` | 查询文本，中文自然语言 |
| `limit` | 每页数量（1–100） |
| `from_date` / `to_date` | 按 `taken_at` 过滤，格式 `YYYY-MM-DD` |
| `tags` | 标签白名单，命中任一即可（OR） |
| `status` | 默认 `done`，避免把还在处理的照片当结果 |
| `w_semantic` / `w_recency` / `w_interaction` | 三段权重，会自动归一 |
| `cursor` | 上一次响应的 `next_cursor`，用于翻下一页 |
| `auto_parse` | `true` 时后端用 qwen-plus 或规则拆解查询，把"上个月西湖的雨天照"拆成时间+地点+语义 |

### 返回

```json
{
  "items": [
    {
      "id": "uuid",
      "thumb_url": "...",
      "taken_at": "2026-07-15T09:30:00Z",
      "ai_description": "...",
      "status": "done",
      "score_semantic": 0.72,     // 向量相似度分（0–1）
      "score_recency": 0.61,       // 时间新鲜度分（0–1）
      "score_final": 0.68          // 混合最终分
    }
  ],
  "total": 20,
  "next_cursor": "MC40MDU3...",   // null 表示已是最后一页
  "parsed": {                       // 仅当 auto_parse=true 时非空
    "semantic": "雨天的照片",
    "from_date": "2026-07-01",
    "to_date": "2026-07-31",
    "place": "西湖",
    "tags": []
  },
  "cache_hit": true                 // Embedding 是否命中 Redis 缓存
}
```

### 三个关键机制

- **Embedding 缓存**：查询文本经过 sha1 哈希做 Redis key，命中后省一次 DashScope 调用（30ms 内响应），TTL 24 小时。
- **混合排序**：`final = 语义 × w_semantic + 时间新鲜度 × w_recency + 交互度 × w_interaction`，先按向量距离召回 `limit × 3` 条候选再重排，避免"意思对但拍得很久前"的照片盖过最近的。
- **游标分页**：`next_cursor` 是 `final_score:photo_id` 的 base64 编码，避免深翻页时 `OFFSET` 性能崩溃。

### auto_parse 的两档实现

- **真模式**（填了 DashScope Key）：`app/services/query_parser.py` 调 qwen-plus 让它输出 JSON。
- **mock 模式**：一个小型规则引擎，能识别 "今天 / 昨天 / 前天 / 最近一周 / 上周 / 上个月 / 今年" 等中文时间词。

## D15–D17 AI 改造 & Skill 生态

### 一键端到端验证

```bash
docker compose exec api alembic upgrade head       # 应用新迁移
docker compose exec api python scripts/seed_skills.py  # 灌 10 个官方 Skill
./scripts/e2e_generate.sh                          # 跑闭环
```

预期最后打印 `✅ D15–D17 AI 改造闭环全部通过` + AI 生成图的公网 URL。

### 核心概念

- **Skill = 生图配方**：一段中文提示词模板 + 若干张风格参考图 + 用哪个模型。既可由官方预置（`is_official=true`），也可由用户创建。
- **Generation = 一次改造记录**：谁 · 拿哪张原图 · 用哪个 Skill · 加了什么附加要求 · 用哪个模型 · 生了什么。
- **每日免费额度**：`GEN_DAILY_FREE_QUOTA=3`，超了返回 429，用户明天再来。

### 新增接口

| 接口 | 说明 |
|---|---|
| `GET /skills` | 我可见的 Skill（官方 + 我自己的） |
| `GET /skills/plaza` | 广场：官方 + 全体公开的用户 Skill |
| `POST /skills` | 创建自定义 Skill |
| `PATCH /skills/{id}` | 修改自定义 Skill |
| `DELETE /skills/{id}` | 删除自定义 Skill |
| `GET /skills/_/quota` | 我今日剩余生成次数 |
| `POST /photos/{id}/generate` | 用某个 Skill 改造某张照片（异步） |
| `GET /generations` | 我的生成历史 |
| `GET /generations/{id}` | 轮询单个生成任务的状态 |

### 图像生成抽象层

`app/services/image_gen.py` 里的 `generate(source, prompt, refs, model)` 是唯一对外入口。目前实现：

| model 值 | 后端 | 状态 |
|---|---|---|
| `wanx-v1` | DashScope 通义万相 image2image | ✓（同一个 DashScope Key） |
| `gpt-image-2` | OpenAI images/edits | ✓（需填 `OPENAI_API_KEY`） |
| `mock` | 直接返回原图 | ✓（本地开发用） |

想接第三个模型（Stable Diffusion / Midjourney）只需实现一个 `_generate_xxx()` 函数并注册。

### 参考图会真的被送到模型

D15–D17 特意选了 "**方案 B**"：用户在 Skill 里加的参考图不只是 UI 展示，会作为 `style_ref_img`（万相）或 `image[]`（gpt-image）一起送给模型，让 AI 学它们的风格。这也是"用户 Skill = 真正的个性化"的关键。

### Worker 任务链路（`generate_photo`）

```
1. status = processing
2. 拉 Skill → 拉 Photo → 拼 prompt（skill.prompt_template + extra_prompt）
3. sign_get_url(source) + sign_get_url(refs)
4. image_gen.generate(source_url, prompt, ref_urls, model)
5. httpx 下载模型返回的 URL → oss.put_object 存回自己 OSS（避免 24h 过期）
6. 更新 generations 表 · 增 rate_limit · 增 skill.use_count
```

### 灌官方 Skill

```bash
docker compose exec api python scripts/seed_skills.py
# ✓ inserted=10  skipped(existing)=0
```

现在内置 10 个：**宫崎骏动画风 / 复古油画 / 港风霓虹 / 证件照 / 赛博朋克 2077 / 水墨中国画 / 3D 泡泡玛特 / 宝丽来胶片 / 低饱和电影感 / 极简线条插画**。想自加就编辑 `scripts/seed_skills.py` 里的 `OFFICIAL_SKILLS` 列表。

### 小程序新页面

| Tab / 页面 | 路径 | 用途 |
|---|---|---|
| **广场** (tab) | `pages/skills/` | 官方 + 用户公开 Skill · 切换"广场/我的" |
| Skill 编辑 | `pages/skill-edit/` | 起名 · 写提示词 · 上传参考图 · 选模型 · 是否公开 |
| AI 改造 | `pages/generate/` | 选源图 + 附加要求 → 轮询到 done → 保存到相册 |
| 我的生成 | `pages/generations/` | 生成历史，点击可预览大图 |

时间线页长按一张照片的 ActionSheet 现在多了 "AI 改造" → 跳到广场页选 Skill → 生成。

## 阶段留白

以下几处 D15–D17 未做，按路线图往后推进：

| 位置 | 现状 | 计划阶段 |
|------|------|----------|
| 语音搜索 ASR | 只录音未识别，MVP 版本给提示 | 上线前接微信同声传译 |
| Nginx 反代 + HTTPS | 未启用 | D13–D14 |
| Sentry / 日志采集 | 只用 logging | D13–D14 |
| 生产 OSS Bucket + CORS | 需自行申请 + 运行 `setup_oss_cors.py` | 上线前 |
| 真机预览 | 需要局域网 IP 或部署到公网 | 详见 `miniprogram/README.md` |

## 依赖版本策略

- Python 3.12
- FastAPI 0.115+
- SQLAlchemy 2.x（**必须** 2.x，代码用了 `Mapped[...]` 语法）
- pgvector-python 0.3+（配 PG 16 + pgvector ≥ 0.5，才有 HNSW）
- Redis 7+

## 目录约定

- 每个 API 路由都要写 `summary`，Swagger 上更友好。
- 所有 SQL 都过 SQLAlchemy 或 op.execute，禁止在业务层拼字符串。
- 敏感字段（`.env`）绝不入库、绝不入 Git。
- 提交前跑 `ruff check .` 保证风格一致。
