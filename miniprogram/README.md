# photo-agent-mini · 微信小程序端

> D10–D12 阶段产物 — 一个能真的点、真的传、真的搜的照片管家小程序。

## 目录结构

```
miniprogram/
├── app.js / app.json / app.wxss / sitemap.json / project.config.json
├── utils/
│   ├── config.js     # 只有一行：API_BASE，改这里指向你的后端
│   ├── api.js        # HTTP 封装 + JWT 自动加头
│   └── file.js       # SHA-256 + 文件大小 + PUT 直传
└── pages/
    ├── login/        # 微信登录 + 昵称/头像
    ├── timeline/     # 时间线三列栅格 + 下拉刷新 + 无限滚动
    ├── upload/       # 批量选图 → 逐张 hash → 签名 → PUT → 回调
    └── search/       # 底部 Agent 对话窗 + SSE 搜索进度 + 照片结果
```

## 60 秒跑起来（本机调试）

1. **确保后端在跑**
   ```bash
   cd ..                       # 回到 photo-agent 根
   docker compose up -d
   docker compose exec api alembic upgrade head
   ```

2. **打开微信开发者工具**（版本 ≥ 稳定版 1.06 · 需要有微信账号）

3. **导入项目**
   - "导入项目" → 选择本 `miniprogram/` 目录
   - AppID 选 **测试号（无 AppID）** — 无需注册也能跑
   - 项目名字随意，比如 `photo-agent-mini`

4. **勾选调试选项**
   - 点顶栏"详情" → "本地设置"
   - 勾 **☑ 不校验合法域名、web-view（业务域名）、TLS 版本以及 HTTPS 证书**
   - 勾 **☑ 增强编译**

5. **点"编译"看效果**
   - 底部弹出模拟器 → 会自动跳到"登录页"
   - 点"使用微信登录"（模拟器里也能走通 mock code）→ 进入时间线

## 三个页面的用法

### 登录页
- 头像和昵称都可以留空（后端 dev 模式允许）
- 想测试真微信登录：在 `.env` 里填真 `WECHAT_APPID / WECHAT_SECRET`，同时小程序的 AppID 也要匹配

### 时间线页
- 首次打开是空的
- 拉下屏幕会刷新
- 长按任意照片可查看详情或删除

### 上传页
- 点"选择照片"从相册/相机拿最多 9 张
- 点"开始上传"，屏幕会显示每张的进度：`hashing → signing → uploading → finishing → done`
- 完成后切到"时间线"即可看到；后端 AI 处理约 3–8 秒后 `ai_description` 会填上

### 搜索页
- 底部固定 Photo Agent 对话窗，通过 `POST /agent/stream` 实时接收搜索进度、工具结果和最终回复
- Agent 找到的照片显示在对话窗上方；普通模式展示候选，点“帮我从结果里选最好的一张”会要求 Agent 只选最佳 1 张
- Agent 需要补充条件时，澄清问题和快捷选项直接显示在聊天气泡中；后续输入携带同一 `session_id`
- 点“新对话”会清空当前会话、消息和照片结果
- 快捷 chips：一键发送常用查询
- 语音按钮长按录音，MVP 版本还未接 ASR，只是录音下来

## 常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 登录报"网络请求失败" | 后端没起 or `API_BASE` 不对 | 检查 `docker compose ps` 与 `utils/config.js` |
| 请求报"不在合法域名" | 忘了勾"不校验合法域名" | 详情 → 本地设置里勾上 |
| 上传后照片一直 `pending` | worker 挂了 | `docker compose logs worker` 看 |
| Agent 一直显示处理中或报 409 | Redis/LLM 未配置，或上一请求仍持有 Agent 锁 | 检查 API 日志、Redis 和模型环境变量，稍后重试 |
| 头像/昵称输入无反应 | 微信版本太低 | 升级到 8.0+ |

## 真机预览

微信开发者工具顶栏 "预览" → 用手机微信扫码 → 手机上真的能用了。

**但**：预览到手机后，`API_BASE = http://localhost:8000` 手机就打不到了。两种解决方案：

- **A · 内网直连**（简单）：把 `utils/config.js` 里改成 `http://你的电脑局域网IP:8000`，手机连同一 WiFi 即可
- **B · 上线部署**（正式）：把后端部署到公网服务器（阿里云 ECS + Nginx），配 HTTPS，然后 `API_BASE` 改成 `https://your-domain.com`

## 下一步

- **接 ASR** 让语音搜索真正可用（微信内置"同声传译"插件需要独立申请）
- **加照片详情页**：当前详情用的是 Modal 弹窗，可以改成独立页面显示大图 + EXIF + 位置
- **接微信支付** 做订阅制
- **兼容 iPad**：分栏布局
