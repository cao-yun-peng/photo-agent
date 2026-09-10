# GitHub 中转仓库文件范围

GitHub 保存可重建项目的源码，云服务器拉取源码后安装依赖、构建镜像，并单独配置运行环境。

## 保留并提交

- `app/`、`web/`、`miniprogram/`：业务源码与静态资源。
- `alembic/`、`alembic.ini`：数据库迁移，包含新增的版本文件。
- `requirements.txt`、`web/package.json`、`web/package-lock.json`：依赖及锁文件。
- Dockerfile、Compose 文件、`.dockerignore`、`web/deploy/`、`observability/`：构建与部署配置。
- `.env.example`、`web/.env.example`、`app/data/prompts.json.example`：配置模板。
- `.github/`、`tests/`、`scripts/`、文档：CI、回归测试、维护脚本和项目说明。
- `tests/schema_drift_baseline.txt` 与 `tests/eval/prompts/*.txt` 是有效测试输入，不能按 `*.txt` 全部忽略。

## 仅保存在本地

`.gitignore` 排除了真实 `.env` 及其环境变体、密钥文件、本地提示词覆盖、虚拟环境、依赖目录、构建输出、日志、数据库文件与备份、测试临时目录、测试报告、`docs/benchmarks/`、测试照片和 `.project-to-act/` 本地工作记录。

已跟踪的测试照片和本地工作记录须从 Git 索引取消跟踪，本机原文件保留。取消跟踪会在下一次提交中表现为删除；历史提交里的副本仍然存在，不会因此缩小历史仓库。

图片评估清单和评估代码继续保留。需要运行真实图片评估时，应从本地备份或单独的数据存储恢复 `test_photos/` 和 `test_photos_realistic/`；部署应用无需这些照片。

## 提交与服务器衔接

提交前检查 `git status --short` 和 `git diff --cached --stat`，将需要发布的源码改动及忽略规则一并提交。不要通过 `git add -f` 加入被忽略的数据或配置。

服务器拉取后从 `.env.example` 创建自己的 `.env`，配置真实密钥、数据库连接和域名。生产部署需按 `docs/08-configuration-and-deployment.md` 核对配置：当前基础 Compose 含开发热更新、默认密码和数据库等端口映射，不能直接视作已完成生产加固；Web 构建默认也开启开发登录入口。

本次整理仅处理发布文件边界，不代表完成云部署验证，也不会自动提交或推送。
