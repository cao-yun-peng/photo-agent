# 密钥审计记录（2026-08-15）

## 范围与方法

本次审计覆盖当前工作区、Git 跟踪文件、全部本地分支/标签/远端跟踪引用的提交历史、
`.env` 跟踪状态、Docker 构建路径和 Windows 文件 ACL。扫描过程只输出文件名、键名、长度
和占位符分类，不打印密钥值。

高置信模式包括 DashScope/OpenAI 风格 key、阿里云 AccessKey ID、AWS key、JWT 和私钥
头；另外对 API key、token、password、client secret 和带凭据 URL 做通用赋值审计。

## 结果

| 检查项 | 结果 |
|---|---|
| 当前 Git 跟踪文件高置信密钥 | 0 文件 |
| 当前工作区（排除 `.git/.venv/artifacts`）高置信密钥 | 0 文件 |
| 全部本地 Git 历史高置信密钥 | 0 命中 |
| `.env` 当前是否被 Git 跟踪 | 否 |
| `.env` 历史提交次数 | 0 |
| 通用赋值候选 | 4 条，均为示例/测试占位符 |
| 私钥文件或私钥头 | 未发现 |
| Git remote | SSH URL，无内嵌用户名/密码/token |

通用赋值候选位于 `.env.example`、`docs/agent-evaluation.md`、`scripts/agent_eval.py` 和
`tests/conftest.py`，对应 `changeme`、测试数据库密码或示例 API key，不是真实凭据。

本地 `.env` 中 JWT、OSS 和 DashScope 等运行凭据已经配置，但文件被 `.gitignore` 忽略，
扫描未输出其内容。Dockerfile 使用精确 `COPY`，当前不会复制 `.env`；本次新增
`.dockerignore` 作为纵深防御，防止未来改成 `COPY . .` 后意外把密钥写入镜像层。

## 风险与处置

1. **Windows ACL（中风险）**：`.env` 当前继承项目目录权限，本机 `BUILTIN\\Users` 可读，
   `Authenticated Users` 可修改。个人单用户设备风险有限，但共享电脑、实验室机器或远程
   多用户主机上不合适。ACL 未被自动修改，避免因本地化组名或继承关系导致项目不可读；
   部署机应仅授权服务账户、SYSTEM 和管理员。
2. **运行时注入（预期行为）**：`docker-compose.yml` 通过 `env_file: .env` 向容器注入配置。
   不要把容器环境转储、`docker inspect` 全量输出或崩溃包公开上传。
3. **远端平台能力边界**：本次验证的是本地已有的完整 Git 引用；没有启用 GitHub Secret
   Scanning/Push Protection。公开仓库建议在 GitHub 设置中开启这两项。
4. **轮换原则**：当前没有证据表明真实密钥进入 Git，因此不要求因本次审计强制轮换。
   如果密钥曾通过聊天、截图、日志或其他仓库公开，则本地 Git 扫描无法证明安全，应立即
   在阿里云/OpenAI 控制台轮换并删除旧凭据。

## 后续约束

- 真实值只放 `.env` 或部署平台 Secret Manager；仓库只保留 `.env.example` 占位符；
- 日志和评测产物不得记录 JWT、签名 OSS URL、Authorization header 或模型 API key；
- 新增 CI 时运行带 `--redact` 的 gitleaks 或同类扫描，并在 push/PR 阶段阻断；
- 发布 Docker 镜像前检查 history 和 image config，确认没有 secret 环境变量或文件层。
