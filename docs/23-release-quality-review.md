# P7 全链路质量、成本、延迟与发布复核

P7 交付评测和发布复核能力；正式发布仍需真实质量、完整费用和负载证据。产品计划 P7 不代表生命周期阶段 7：当前仍在阶段 6。最终实测结果见本文件末尾及 `.project-to-act/tasks/S6-P7-001/evidence/`。

## 交付内容

- 新增 `app/evaluation/release.py`、`scripts/review_release.py`，沿用评测 V2，汇总工程、安全、路由、检索、修图、全链路、费用、延迟和运维九项 Gate。缺项、非通过、证据文件变更、代码文件新增/变更、越界路径、证据过期均拒绝。有效期 24 小时，发布前重新生成证据。
- 真实质量/成本/延迟要求 live 证据；mock 仅用于工程验收。安全关键失败必须为零。费用要求覆盖率 100% 且已对账；只有估算或者未知账单不能通过。延迟使用 nearest-rank，至少 100 个真实样本、有限的正阈值和负载描述；这是最低数据完整性要求，并不自动证明样本有代表性。
- 工具校验工件完整性和评审声明，不替代人工对数据、标注、代表性、阈值和供应商账单的复核。`reviewer` 和 `acceptance` 必填；即使全部通过也只输出 `ready_for_owner_review`，不会授权部署。
- 生成适配器只保留白名单 token 数，不保存供应商完整响应。GPT 图片费用估算与配置一致，代替写死的 0.30 元。`cost_yuan` 为兼容字段，继续表示估算。
- 生成结果 `verification.execution_metrics` 持久保存 Worker 总耗时、生成/核验耗时及费用来源。`actual_yuan=null` 表示真实账单未知，mock 明确为零；`complete=false` 明确规划/核验费用尚未汇总。Worker 耗时不含用户确认等待和队列等待。已收到供应商结果后先写入用量，再核验；核验失败不抹掉已知记录。中断或租约丢失可能缺少尾部记录，不据此推断未计费。
- 生产 API/Worker 启动时要求聊天/VL 和图片生成的非空、非占位密钥，防止缺配置静默演示。仅检查配置存在，不证明密钥有效；不输出密钥。当前发布范围含流程包生图，模板专用部署如需省略 OpenAI，须先增加明确的能力开关与契约。
- 新增 TCP 全链路测试：真实 FastAPI 生命周期、中间件/JWT、隔离 PG/Redis、上传签名和 PUT、后台分析、SSE、包预览/导入、计划、错误摘要拒绝、重复确认、ARQ Worker、结果、跨用户访问拒绝、保存相册及撤销。模型与 OSS 为本地模拟，不覆盖微信登录、真实供应商网络或生产反向代理。
- Python CI 运行上述测试并保留 JUnit 工件。浏览器主路径已接入隔离真实 API/Worker，覆盖开发登录、上传、搜索和旧模板生成；P4–P6 的包方案与选片交互检查仍使用 API fixtures。真实模型及生产代理链路未验证。

## 复跑

只使用隔离测试数据库。`scripts/serve_release_sandbox.py` 提供本地 API/Worker（58007），固定测试库/Redis，清空真实模型和微信配置，15 分钟自动结束；Web 在 `NEXT_PUBLIC_API_ORIGIN=http://127.0.0.1:58007` 下运行 `vinext dev --port 3002`，Playwright 使用 `WEB_BASE_URL=http://127.0.0.1:3002`。隔离库中的开发测试账号与本地模拟对象可保留用于排错。不要将 `docker-compose.e2e.yml` 与业务 compose 直接组合；旧 override 不提供独立端口/卷隔离。

```powershell
docker compose -p photo-agent-p4-test -f tests/compose.batch1.yml up -d
$env:PYTHONUTF8='1'
$env:PHOTO_AGENT_TEST_DATABASE_URL='postgresql+asyncpg://batch1:batch1_test_only@127.0.0.1:55439/photo_agent_batch1_test'
$env:PHOTO_AGENT_TEST_REDIS_URL='redis://127.0.0.1:56389/15'
.venv/Scripts/python.exe -m pytest -q --junitxml=test-results/python.xml
.venv/Scripts/python.exe scripts/eval_agent.py routing --output test-results/routing.json
.venv/Scripts/python.exe scripts/eval_agent.py trajectory --output test-results/trajectory.json
.venv/Scripts/python.exe scripts/review_release.py .project-to-act/tasks/S6-P7-001/evidence/release-manifest.json --output test-results/release-review.json
```

测试入口清空真实模型密钥；新库先在同一隔离环境执行 Alembic upgrade head（参考 P4 交付）。不要直接在加载业务 `.env` 的环境运行迁移。发布审查退出码：0=等待所有者最终评审，1=阻塞，2=输入无效。已保存 manifest 只适用于记录的文件版本和 24 小时时间窗；代码变化后必须重测受影响项，而不是单纯刷新哈希。

## 真实验收协议与解除阻塞

| 项目 | 需要补齐的证据 | 负责人 / 复审点 |
| --- | --- | --- |
| 路由与工具质量 | 独立复核 route-dev-009；Development 调试后，在 Validation 冻结阈值；授权消耗 Test 后验证，多轮案例重复 3–5 次 | 项目所有者指定标注复核人；下次真实评测前 |
| 检索及成组选择 | 冻结且获得使用许可的相册、人工相关性标签、12 张旅行/至少合照/锁定/排除约束；Recall@K、nDCG、零结果与重复率按场景报告 | 所有者提供样本与评价人；选模型/阈值前 |
| 修图效果 | 固定原图/Skill 版本/参考角色；方向、主体保留、风格、误复制参考内容、中文标题、无文字要求；人工评分校准，不以模型自评代替 | 所有者指定评价人；上线前 |
| 全链路费用 | 规划、路由、检索模型、生成、核验、重试的逐次用量及价格版本，账单对账；超时/取消的未知费用不能按零统计 | 所有者指定费用负责人、真实测试总预算与停止线；付费实验前 |
| 延迟 | 冻结负载、照片数量、并发、模型、超时率；SSE 首事件与首个有意义结果分别报告；搜索/生成分别测 P50/P95，单列失败/超时，禁止只统计成功样本 | 所有者与运维确认目标；Validation 后冻结 |
| 运维 | 制品摘要、配置核验、备份恢复演练、同版本 API/Worker、代理 SSE 断线测试、告警到达、值守人及真实浏览器验收 | 发布负责人指定；灰度前 |

尚无真实数据与预算上限时，不运行真实模型，不消费 Test，不将合成图/脚本结果登记为真实质量。具体业务质量及 SLO 数值尚未冻结；不得为了本次实测通过倒推阈值。

## 发布顺序、停止和回退

1. 固定发布能力范围、代码制品摘要、迁移 head `20260906_0004`、Prompt/工具契约、Skill 版本、模型及配置。当前脏工作区的 Git HEAD 不代表发布包；以 source_sha256 复核，正式制品另存构建摘要。
2. 在独立 staging 数据库备份并演练恢复；迁移到 head 后验证 schema drift、所有权、确认与租约。新旧 Worker 不混跑；先停止旧消费者并处理已有任务，未知供应商结果人工对账，不重新入队付费任务。
3. 所有 Gate 具备新鲜证据且负责人批准后，再由少量内部账号灰度；观察错误率、队列年龄、过期租约、outcome_unknown、未确认/重复付费、费用覆盖率、SSE 断线与尾延迟。
4. 任一越权、未确认付费、重复生效或硬约束静默丢失立即停止扩大灰度并关闭新增写入/生成流量；模型费用未知、尾延迟持续越线时暂停付费入口、排查并核实账单。生产阈值需在灰度前冻结。
5. 回退先停止流量和消费者，保留任务/账单证据，再切回已验证兼容制品。含 package/workspace 数据的降级迁移会主动拒绝；禁止强行删除新表或手改迁移版本。不能兼容时使用前向修复或经过演练的备份恢复，并处理备份之后的写入。

本轮未部署、未修改业务数据库，未运行真实模型。历史迁移往返证据在 P4–P6；P7 全量回归重新验证 schema contract，是否另做备份恢复演练必须独立记录。

## 复核中修正的问题

- 新耗时记录在取消/超时前尚未开始核验时引用了未初始化计时变量；全量回归发现后修正，最终 200 项通过。
- 旧浏览器用例把工作台截图固定写回 P6 证据目录；已改为 Playwright 本次运行目录。P6 截图在本次初次复跑中被刷新，不能再作为原始 P6 截图证据，P6 测试结果文字保留。
- CI 的 E2E override 原来使用 test 环境，但开发微信登录只在 dev 环境允许。现改为明确 dev + 空微信凭据，生产配置不受影响；仅做本地 compose 解析，远端 CI 尚未运行。

## 本轮验收结果（2026-09-07）

- Python：200 passed / 1 skipped，89.76 秒；跳过项为 Windows 符号链接权限。真实 PG/Redis 和原始针织 Skill 包参与回归。
- Web：15 文件、45 项通过；lint/typecheck/build 通过。浏览器全六项通过，截图路径调整后单项复测通过，随后 typecheck/定向 ESLint 通过。
- Development：L1 47/48，唯一失败 route-dev-009；L2 11/11，反馈轨迹 3/3。真实模型、Validation/Test 未运行。
- 本地 TCP：选片 GET 20 次，P50 22.97 ms、P95 27.15 ms；SSE 首事件 140.75 ms、终止 248.68 ms；确认到模拟生成结果 1148.24 ms。后两条各一次，不作为分位数或生产 SLO。模型真实花费为零（未调用），真实付费路径的费用覆盖率未知。
- Ruff、Compose 解析通过，远端 CI / 备份恢复 / 生产代理未执行。
- 发布 Gate：blocked。工程交付及本次复核完成，真实质量、费用、延迟和运维验收继续阻塞生命周期阶段 6。
