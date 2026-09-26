# Web 账号注册与登录

Web 支持账号密码注册和登录，复用现有 JWT、用户 ID 与资源归属机制。微信账号继续使用原入口；新 Web 账号不会按昵称自动合并到微信账号，也不会自动获得旧开发账号的照片。

## 使用方式

- 登录页默认显示账号和密码；点击“立即注册”可创建账号并进入时间线。
- 账号为 3–32 位英文字母、数字或下划线，不区分大小写。
- 注册密码为 12–128 位；允许空格和 Unicode，不自动截断或去掉密码首尾空格。
- 退出登录会清除当前标签页的会话并取消该会话的客户端请求；这不是服务端吊销所有已签发 JWT。JWT 的过期时间继续由 `JWT_EXPIRE_MINUTES` 控制。
- 此版本不含自助找回密码、修改密码、邮箱验证或微信绑定；部署前向内测用户说明账号恢复方式，勿通过昵称认领已有账户。

## 部署顺序

1. 备份数据库，并使用含本次代码的 API 镜像执行 `alembic upgrade head`，目标版本为 `20260912_0001`。
2. 配置 `WEB_REGISTRATION_ENABLED=true`。改为 false 会关闭新注册，已有账号仍可登录。
3. Web 构建时设 `NEXT_PUBLIC_ENABLE_DEV_LOGIN=false`（现为默认值），然后重新构建 Web。此项是构建参数，仅重启容器不会改变已编译的页面。
4. 启动 API/Worker 与 Web，检查 `/auth/options`，验证注册、退出、重新登录和过期会话。

例如，在已经配置好生产环境与部署 Compose 的服务器执行迁移：

```bash
docker compose exec -T api alembic upgrade head
```

上述命令不会将基础开发 Compose 自动变为生产配置。生产环境仍需 HTTPS、`APP_ENV=prod`、强 JWT 密钥、精确 CORS 和已有的模型/OSS 配置。不要用真实用户密码在 HTTP 公网入口登录。

`NEXT_PUBLIC_ENABLE_DEV_LOGIN=true` 仅用于显式的本地联调；还需要后端确认处于可 Mock 的 dev 模式，页面才显示折叠的开发入口。E2E Compose 显式打开此构建参数以兼容旧开发态用例。正式账号流程不依赖该入口。

## 接口与数据

| 接口 | 行为 |
|---|---|
| `GET /auth/options` | 返回是否开放注册、是否允许开发入口 |
| `POST /auth/register` | JSON `username`、`password`；成功返回 201 和 TokenResponse |
| `POST /auth/login` | JSON `username`、`password`；成功返回 200 和 TokenResponse |
| `GET /auth/me` | 用 Bearer JWT 查询当前用户 |

注册冲突返回 409，关闭注册返回 403，错误账号/密码统一返回 401，限流返回 429 并携带 `Retry-After`，认证限流依赖不可用返回 503。无效、过期或不存在用户的 JWT 返回 401，保留既有业务错误码。

新增 `web_credentials` 表，使用 `user_id` 一对一外键和唯一的规范化 `username`；`users.wechat_openid` 改为可空。用户资料响应不包含密码哈希。注册用户与凭据在同一事务提交，唯一约束处理并发重名注册，失败回滚避免孤立用户。

密码使用随机 128 位盐、PBKDF2-HMAC-SHA256 600,000 次迭代和常量时间摘要比较；密码计算在后台线程执行，避免阻塞事件循环。工作因子参考 [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。请求校验日志/响应不保留认证原始输入，SQLAlchemy 隐藏绑定参数。

## 限流和代理

Redis 原子计数覆盖所有尝试，包括成功请求：登录每账号 10 次/15 分钟、每来源 IP 50 次/15 分钟；注册每账号 5 次/小时、每来源 IP 10 次/小时。键中不直接保存账号和 IP。Redis 不可用时不绕过限流。

来源采用 ASGI 已解析的 peer IP，不自行信任 `X-Forwarded-For`。反向代理部署时，应只信任受控代理地址，并阻止直接访问 API；未正确配置代理信任时，多用户可能共享网关 IP 的额度。不要通过信任任意转发头解决这个问题。

## 迁移与回滚

空的新表可以降级回 `20260907_0001` 再升级。已有 Web 凭据或无微信身份的用户时，降级会在删除表之前主动拒绝，避免丢失账号；保留新 schema 并回滚应用制品，或制定人工数据迁移方案。禁止以删掉真实 Web 用户作为常规回滚步骤。

## 验证入口

`tests/test_web_auth.py` 覆盖密码、敏感输入保护和限流异常；`tests/test_web_auth_integration.py` 在隔离 PostgreSQL/Redis 上覆盖生产模式注册、并发重名、凭据失败、限流和微信身份隔离。迁移契约测试保留已有 schema drift 基线。

前端测试为 `web/features/auth/login-view.test.tsx`；真实浏览器用例为 `web/e2e/web-auth.spec.ts`，覆盖注册、退出、错误密码、重登和本地过期会话。应仅在隔离测试环境运行该用例，它会创建测试账号。
