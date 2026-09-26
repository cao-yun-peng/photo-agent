# Photo Agent 文档中心

本目录包含当前实现说明、设计提案和历史交付记录。各文档的日期与验证边界以文内说明为准；
项目阶段、发布 Gate 和验收状态以本地 `.project-to-act/` 台账为准。GitHub 仓库不包含该本地台账，
可公开查看的当前边界见 [项目 README](../README.md)、[当前模型复测](33-current-model-retest.md)
和 [发布质量复核](23-release-quality-review.md)。

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

## 近期实现与评测

- [检索评测](27-retrieval-evaluation.md)：冻结数据、真实模型结果及质量局限。
- [检索优化](28-search-optimization.md)：实现改动与隔离集成验证。
- [搜索执行预算](29-search-execution-budget.md)：超时、额度和续查规则。
- [Agent loop 迁移](30-agent-loop-migration.md)：工具协议和真实模型质量 Gate。
- [仓库发布文件范围](31-repository-publishing.md)：源码与本地数据的 GitHub 边界。
- [Web 账号注册与登录](32-web-authentication.md)：接口、迁移、限流和客户端行为。
- [当前模型回归复测](33-current-model-retest.md)：2026-09-12 的真实调用结果与限制。

## 设计与改造提案

- [产品与 Agent 演进方案](17-agent-product-evolution-plan.md)：两轮讨论汇总，P0–P7 实施顺序、反馈兜底、可上传修图 Skill、后续相册与验收。

以下为提案，不能作为已实现能力或发布验收依据；各文档单独标明代码基线。

- [优化方案](12-optimization-plan.md)：基于 `d2983f1` 的审查复核、目标架构、实施切片、验证与回滚。

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
| Agent | `app/services/agent*.py`（前置路由已退出主流程） |
| 照片处理 | `app/workers/tasks.py`、`app/services/image.py`、`ai.py` |
| 检索 | `app/api/search.py`、`app/services/search*.py` |
| 图像生成 | `generation_service.py`、`app/workers/gen_tasks.py` |
| 部署与观测 | `docker-compose*.yml`、`observability/`、`.env.example` |
| 客户端 | `web/`、`miniprogram/` |

`docs/1.md` 是一份保留的 TC-001 评测用例讲解，不属于核心设计文档。

- [第一批修复交付与运行说明](13-batch1-delivery.md)：配置、迁移、恢复及测试边界。

- [第二批修复交付说明](14-batch2-delivery.md)：客户端隔离、SSE、模型/参数契约、时区与确定性排序。

- [第三批统一搜索服务交付](15-batch3-delivery.md)

- [第四批SQL相册与缓存性能实测](16-batch4-delivery.md)

- [P2反馈驱动搜索交付](18-feedback-search-delivery.md)：批次计数、分级兜底、客户端兼容与验证边界。

- [P3 Skill包导入交付](19-skill-package-delivery.md)：私有版本、兼容报告、Web上传及迁移限制。

- [P4 Skill 创作方案与执行交付](20-package-execution-delivery.md)：冻结原图/风格参考、标题与确认、Docker 实库验证及模型边界。

- [P5 生成生命周期交付](21-generation-lifecycle-delivery.md)：取消、租约恢复、受限调整和进度展示。

- [P6 选片与明确记忆交付](22-workspace-memory-delivery.md)：稳定选片、结构化记忆、私有相册和版本化撤销。

- [P7 全链路质量与发布复核](23-release-quality-review.md)：费用口径、耗时、真实 TCP 验证、发布 Gate 和回退步骤。

- [真实可用性收尾](26-real-usability-closeout.md)：供应商契约、流程包调用账本、规划去重和核验规则校准。
