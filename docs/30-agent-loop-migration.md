# Agent loop 与状态工具迁移

## 2026-09-10：相关性与选图模式解耦

语义搜索 browse/best/select 均严格核验，只展示 match。select 不再关闭核验；verify_semantic=false 也不能绕过语义搜索核验。旧 off/soft 搜索需重新发起。连续负反馈仍记录，但不会自动丢弃语义条件：第二层返回 scope_requires_user，只有用户明确要求浏览时才调用 browse_album。显式 timeline/album 返回浏览候选，并标注未核验；普通 fallback 不自动转时间线或全相册。

本地开发实现已改为每轮自然语言输入直接进入 Agent loop。工具决定业务动作，服务端执行状态迁移；不再运行前置关键词分类或独立上下文路由模型。质量验收状态和最新实测数值见文末证据链接；实现完成不等于发布通过。

## 工具契约

契约版本为 `agent-contract-v4-comfort`。`search_photos`、`continue_search`、`feedback_results`、`browse_album` 由统一执行器调度。原搜索、范围扩展和候选浏览保留为内部服务，不向模型暴露相近的多个检索入口。

| 工具 | 参数与行为 |
| --- | --- |
| `search_photos` | 必填 `query`、`change=new/refine`。筛选参数代表最终条件；地点写入完整查询。`new` 清空旧目标的选择、分页、拒绝记录和会话生成引用；`refine` 保留目标及拒绝历史，重建当前候选和分页。不会取消后台生成任务。 |
| `continue_search` | 可指定本次数量；计划 ID、游标、过滤条件、已展示与拒绝 ID 均来自服务端。成功后推进进度，失败保留游标。支持已验证候选池和全相册/线索范围翻页。 |
| `feedback_results` | `kind=reject_items/reject_batch/satisfied`，照片 ID、批次 ID；不执行续搜、不接受 `continue_search` 参数。用户要求同时再找时 `finish_turn=false`，随后单独调用 `continue_search`，可以在同一次模型决策中顺序返回两个调用。批次反馈幂等，历史批次必须属于当前目标。 |
| `undo_feedback` | 使用服务端 `undo_id` 撤销最近反馈，恢复候选、选择和拒绝/批次记录；新搜索、细化、续搜、选图或修图后失效。 |
| `browse_album` | 新建全相册浏览目标。后续使用 `continue_search`；范围候选明确标注为非确认匹配。 |

搜索类动作的 `finish_turn` 默认 true；正常结果直接使用本地文案结束。需要后续工作时设 false；进入等待用户选图仍必须结束。工具错误返回给 loop 修正或解释；时间、步骤、权限、所有权和生成确认仍由代码控制。

模型可见状态包括完整当前查询和过滤条件、当前批次照片序号/ID、最近批次历史、选择和拒绝项。旧 `followup_type` 仅作为兼容字段，不再驱动执行；`search_action` 和事件回调是请求内字段，绝不持久化。

## 自然语言澄清

2026-09-10 起模型不再获得 ask_clarification 工具，信息不足时直接提问并结束本轮；候选不变，下一轮读取对话历史继续。旧注册函数和 clarify 事件仅用于兼容。实验与限制见 [澄清实验](../.project-to-act/tasks/S6-CLARIFY-20260910/ACCEPTANCE.md)。

## 明确按钮操作与短澄清

2026-09-10舒适度修改：当前唯一选图在上下文顶部用结构化字段和简短说明展示。澄清只问必要短问题，已知对象的拒图调用业务工具，不要求二次确认。Web/小程序提供不要这张、再看一些及撤销；按钮通过原HTTP入口的可选ui_action直接调用统一执行器，保留会话归属、锁和所有权校验，无模型决策。照片携带稳定批次和原始序号。撤销前检查照片归属和版本；新增feedback_undone、undo_available事件。整批反馈先记录pending_expansion，只有续搜时执行升级。详见 [舒适度验收](../.project-to-act/tasks/S6-COMFORT-20260910/ACCEPTANCE.md)。

## 状态与客户端协议

HTTP 请求入口和原有事件类型保持兼容。新增 `search_state` 事件携带 `search_goal_id` 和 `display_mode=replace`，在新搜索执行前通知客户端清空当前候选及选择。成功工具结果携带 `display_mode=replace/append`、目标和批次 ID、当前选择、浏览范围。

Web 与小程序按 ID 去重追加续搜结果，保留有效选择；反馈按 ID 移除照片并取消被拒照片的选择。各批次显示批次/照片序号。搜索换目标后即便失败，也不会重新把旧照片当作当前结果。已有聊天消息保留。

沿用服务端用户级互斥与所有权续租。Web 保留请求序号过滤并增加同步请求锁检查；小程序新增请求序号过滤和卸载失效处理。

“还有吗”本身不记负反馈，也不触发扩范围。整批拒绝沿用现有升级：第一次加强筛选，第二次可转线索范围；重复反馈不增加次数，无可靠计划时返回限制，不能无条件声称找遍全相册。

旧 JSON 会话补默认批次字段。缺少可用计划的续搜提示重新搜索；不会猜游标。数据库结构没有变化。

## 迁移、验证与回滚

修改前保存了 app 源码、原评测集及界面入口快照和 SHA-256。旧实现只在单独进程中作为评测基线加载；在线入口不依赖旧路由。旧路由专用单测仍属于历史算法回归，不代表新线上路径的模型质量。

新建 60 条合成多轮用例（40 开发、20 验证），在首次真实运行前封存。验证用例重复三次；开发与验证共享场景家族，只分话术变体，且未经独立标注复核，因此不能声称独立泛化或生产质量。预期从不进入模型输入。

真实评测仅调用已配置的 qwen-plus 文本模型；搜索、照片、推荐和生成均为隔离夹具，没有真实生成或用户照片读取。计价依据：[阿里云 qwen-plus 官方价格](https://help.aliyun.com/zh/model-studio/qwen-plus)，北京、非思考、输入不超过128K：输入0.8元/百万Token、输出2元/百万Token。每次请求前按保守上界预留费用；不确定请求保留预留，所有开发、验证及旧方案对照共用10元上限。脚本以锁文件禁止并发写同一预算账本。

运行方式（需要已配置文本模型凭据）：

```powershell
.venv/Scripts/python.exe scripts/eval_loop_migration.py --split development
.venv/Scripts/python.exe scripts/eval_loop_migration.py --split validation --repeat 3 --suffix=-review
.venv/Scripts/python.exe scripts/eval_loop_migration.py --baseline --split validation --repeat 3 --suffix=-review
```

验证集已使用，后续不得把它用于调 Prompt；质量改进需要开发集和新的独立验证集。脚本运行结束并不等于质量通过，应读取报告及任务验收结论。

回滚应停止当前任务后，仅恢复本次改动对应的快照文件及客户端变更，不重置整个工作区，避免覆盖其他未提交工作。旧会话可继续读取，新增 JSON 字段由旧代码忽略；未执行部署或数据库变更。

证据与最终统计：`.project-to-act/tasks/S6-LOOP-20260909/ACCEPTANCE.md`、`evidence/summary.json`。生命周期维持阶段6 revision9，发布 Gate 不变。
