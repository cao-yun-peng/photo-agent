# P4：Skill 创作方案与确认执行

2026-09-06，任务 S6-P4-001。承接 P3，完成开发切片；Agent 生命周期仍为阶段 6 revision 4，整体发布 Gate 不变。

## 用户流程

Web「Skills」导入流程包后选择「预览创作方案」，选原图、补充要求及标题模式（自动 / 无文字 / 指定文字）。预览显示冻结的原图和风格参考、观察、保留/转换/舍弃内容、构图、标题、画幅、完整生成提示词、版本与费用估算。调整要求后重新生成方案；旧方案确认立即失效。确认才占用生成额度、进入队列，结果保留原图并创建独立输出。

流程包一律需要确认，包括旧 control 灰度路径和 Agent 的 apply_skill。推荐列表仍沿用模板筛选；流程包主要从 Web Skills 入口选择，已知 ID 可通过 apply_skill 使用。小程序未增加流程包方案预览界面。

## 实现契约

- `POST /photos/{id}/generate` 增加 `package_options: {title_mode: auto|none|exact, title?: string}`。用户明确要求无文字优先；自动标题校验中文 2–12 字或英文 2–4 词，允许无标题。指定标题最多 60 字。
- 返回 `execution_snapshot` 和 `execution_digest`，包含 Skill 版本/包摘要、原图标识/摘要、规范化图片哈希与角色、结构化方案、完整提示词、模型、画幅及费用估算。图片字节存入私有 `generation_inputs`，首张为主体，其余为风格参考。旧 Skill 更新或删除不能替换已冻结输入。
- `GET /generations/{id}/inputs/{position}` 仅向任务所有者提供冻结 PNG，禁止缓存；错误所有者返回 404。
- `POST /generations/{id}/confirm` 必须同时提交 confirmation_token 和 execution_digest。确认和 Worker 都检查快照、费用、原图归属及冻结字节；旧确认、篡改和来源失效被拒绝。
- 用户行锁、幂等键、稳定队列任务 ID 与额度事务约束重复确认。并发准备可能产生多次规划模型请求，但只持久化一个同键任务；不宣称模型调用全局 exactly-once。
- 包内文档按 SKILL.md 的可达本地引用读取并校验哈希，作为低权限创作资料。不会执行包内代码、安装依赖或授予网络/工具权限。超限明确拒绝，不静默截断引用或提示词。
- 首版最多原图 1 张、风格图 4 张，资料 24000 字；每张输入及标准化 PNG 最多 16 MiB、2000 万像素，拒绝动画。

## 模型与结果边界

规划采用现有视觉模型接口，结构化 JSON 输出，不采纳 reasoning 字段。生成适配器使用 `gpt-image-2` 的多图编辑，原图在第一位，依据方向选择 1536×1024、1024×1536 或 1024×1024。流程包选用不支持此契约的模型会在调用前被拒绝。实现依据 [官方模型说明](https://developers.openai.com/api/docs/models/gpt-image-2) 与 [图像生成指南](https://developers.openai.com/api/docs/guides/image-generation)，本轮仅核对接口，未调用付费模型。

`PACKAGE_GENERATION_ESTIMATED_COST_YUAN` 默认 0.30 元，仅为可配置估算，既不是供应商账单，也不是供应商侧硬预算上限，未逐项计入规划/核验费用。实际费用对账仍待后续完善。

生成后做尺寸检查及可用时的一次视觉核验（主体、风格、标题、参考内容误复制）；失败标记 needs_review，不自动重画。Mock 明确标记 simulated / not_evaluated，不能表示修图质量通过。供应商异常时流程包不自动重试，释放本地预留额度；超时可能已发生供应商费用，不能据此认定零成本。进程中断后的恢复、人工校准、取消与受控迭代留给 P5。

## 数据库与运行范围

新增迁移 `20260906_0002_package_execution`，继承 P3 的 `20260906_0001`，增加执行快照、摘要、核验字段及冻结输入表。有执行快照时降级会主动中止，不能通过回滚迁移静默丢弃任务数据。部署时先备份，统一迁移并更新 API/Worker，不能混用旧 Worker。

本轮使用 `tests/compose.batch1.yml`、项目名 `photo-agent-p4-test`，PostgreSQL 55439、Redis 56389。只迁移隔离数据库，现有业务 Docker 数据库与服务没有升级或重启。因此本交付是代码与隔离运行验证，不表示当前业务容器已部署 P4。测试容器保留以便后续复用。

## 验证结果

详见 `.project-to-act/tasks/S6-P4-001/evidence/E-S6-P4-001.md`。

- 实库补验 P3 私有版本、资源与所有权；P3、P4 迁移各自升级/降级/再升级成功，模型 Schema 漂移未增加。
- PostgreSQL + Redis + 实际 ARQ 入队/Worker 验证确认重放、额度、快照不受 Skill 更新影响；其他集成用例覆盖旧方案失效、费用篡改、供应商超时不重复调用、资源所有权。
- 用户提供的原始针织目录只读构建 ZIP，实际导入、规划、冻结风格参考、确认并执行模拟生成成功；测试不保存用户原始素材到证据目录。
- Playwright 覆盖方案预览、调整、再次确认及 simulated 标记；浏览器使用 API 固定响应，与真实数据库集成测试是两层证据，不是单次全栈真实模型 E2E。
- Python、Web 单测、Ruff、Web lint/typecheck/build 已验证。所有模型与 OSS 使用测试替身；真实针织效果、质量/延迟/费用、完整发布验收未完成。

复现后端时设置 PHOTO_AGENT_TEST_DATABASE_URL / PHOTO_AGENT_TEST_REDIS_URL 到上述隔离容器后执行 `.venv/Scripts/python.exe -m pytest -q --basetemp .pytest_tmp_p4verify`。设置 PHOTO_AGENT_KNIT_SAMPLE_DIR 可启用原始针织包用例，未设置则该项明确跳过。测试 conftest 清理真实模型与 OSS 配置。Web 在 web 目录执行 npm run test、npm run lint、npm run build，以及 Playwright 的 e2e/package-plan.spec.ts。Windows 管理测试服务器退出曾挂起，改用独立服务器加 WEB_BASE_URL 后浏览器测试正常退出。

## 下一步 P5

完善任务恢复/取消与供应商不确定状态，加入有次数和费用限制的人工确认迭代、可解释核验和界面进度；固定照片集单独开展真实模型评估。不因本次 mock 链路通过而直接开放自动重绘或推进发布 Gate。


## P4 对话接入补齐（S6-P4-002）

本节更新前文“推荐列表仅模板”的限制：推荐工具现支持当前用户拥有、当前版本可导入的流程包，返回 kind/current_version_id/requires_plan_review；即使流程包被错误标成 public/official，也不会跨所有者推荐。模板可见性保持。

apply_skill 公开 package_options，服务端严格校验指定标题，v2 会话幂等键纳入规范化标题选项。Agent 收到待确认结果就停止；Web 对话显示“查看创作方案与生成任务”，链接只含任务 ID，在鉴权方案页面重新获取当前快照及确认凭证。模型没有自动确认工具。

修复通过 generationId 恢复的方案在调整时丢失 Skill/标题的问题；恢复原图、补充要求、Skill 和标题选项后重新创建方案，仍由服务端使旧确认失效。没有新增迁移或业务部署。小程序方案界面仍未扩展。

本轮新增参数/幂等测试、实库推荐隔离测试、Web 对话入口测试；浏览器用例扩展到无 Skill 路由的方案恢复和调整。最新结果见 E-S6-P4-002，不将上一轮针织样例测试误记为本轮重新运行。
