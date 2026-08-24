# Photo Agent 文档中心

本目录描述当前工作区中的实现，而不是历史规划。文档基线为 Git 提交
`21ac64c38859120a0f1715b949b12e8453d1bb9e` 加上当前未提交代码；最近校验日期为
2026-08-24。

> 阅读原则：以代码、Alembic 迁移和 OpenAPI 为最终事实来源。本文档会记录已实现能力、
> 默认关闭的开关和已知风险，不把实验结论写成生产承诺。

| **文档内容** | **说明** |
| --- | --- |
| [项目总览](00-project-overview.md) | 产品目标、核心能力、技术栈和快速阅读路径 |
| [架构蓝图](01-architecture.md) | 系统边界、运行时拓扑、同步与异步数据流 |
| [数据库 Schema](02-database-schema.md) | PostgreSQL、pgvector、实体关系、状态与迁移 |
| [API 规范](03-api-specification.md) | REST、SSE、认证、分页、错误和幂等约定 |
| [Agent 系统](04-agent-system.md) | 编排器、工具、状态机、会话、预算与灰度 |
| [照片处理与检索](05-photo-processing-and-search.md) | 上传、VL、Embedding、混合排序、重排与索引修复 |
| [Skill 与图像生成](06-generation-and-skills.md) | Skill 模型、生成确认、额度、队列和失败恢复 |
| [客户端](07-clients.md) | Web、微信小程序、认证、上传和 SSE 集成 |
| [配置与部署](08-configuration-and-deployment.md) | 环境变量、Docker Compose、服务依赖和上线检查 |
| [可观测性与安全](09-observability-and-security.md) | LogID、Trace、日志、熔断器、认证与风险清单 |
| [测试与评测](10-testing-and-evaluation.md) | 当前测试资产、Agent/VL 评测模式和质量边界 |
| [运维手册](runbook.md) | 启停、迁移、检查、备份、告警与故障排查 |

## 推荐阅读路径

- 第一次接触项目：总览 → 架构 → 数据库 → API。
- 修改 Agent 或检索：架构 → Agent 系统 → 照片处理与检索 → 测试与评测。
- 修改 Web/小程序：API 规范 → 客户端 → 配置与部署。
- 准备部署或值班：配置与部署 → 可观测性与安全 → 运维手册。

## 事实来源

| 主题 | 权威文件 |
| --- | --- |
| API 路由 | `app/api/*.py`、`app/main.py` |
| 请求/响应模型 | `app/schemas/*.py` |
| 数据库实体 | `app/models/*.py`、`alembic/versions/*.py` |
| Agent | `app/services/agent*.py`、`turn_resolver.py` |
| 照片处理 | `app/workers/tasks.py`、`app/services/image.py`、`ai.py` |
| 检索 | `app/api/search.py`、`app/services/search*.py` |
| 图像生成 | `generation_service.py`、`app/workers/gen_tasks.py` |
| 部署与观测 | `docker-compose*.yml`、`observability/`、`.env.example` |
| 客户端 | `web/`、`miniprogram/` |

`docs/1.md` 是一份保留的 TC-001 评测用例讲解，不属于核心设计文档。
