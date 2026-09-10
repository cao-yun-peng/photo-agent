# 第三批：统一 SearchService

2026-09-05。基于 `d2983f1` 和前两批未提交工作树完成。本批没有数据库迁移。

## 实现

HTTP `/search`、`/search/album-fallback`、Agent 搜索/浏览/兜底与 prefetch Worker 都调用 `app/services/search_engine.py` 的 SearchService。过滤 SQL 只在 SearchRepository，核验策略只在 search_verification；旧重复搜索编排、全量相册函数和分数游标已删除。详情接口也使用共用照片序列化。

QueryPlan 冻结原句、解析结果、最终日期、时区、本地日期、评分时间、过滤参数、核验策略和预算 ID。翻页与预取载入同一计划，兜底派生计划继承解析、评分时间和预算。HTTP 保留默认不自动解析，Agent 保留默认自动解析；select 不做模型核验；tags OR、JSON 条件 OR、done 包含 partial_done 的行为保留。

严格模式只接受 match。每批核验后继续补位，前五张被拒绝时可以继续找到第六张。uncertain、未核验、供应商故障和预算不足不会被报告为匹配或全库耗尽。视觉最高开关在策略和调用网关两层执行，force_visual_verify 不能越过它。

有签名的 v3 游标绑定用户、计划、位置与到期时间。Redis 快照固定 ID、分数、顺序与照片版本，不存签名 URL。每页重新检查归属、状态和版本并签发 URL；删除或修改的照片跳过，新增照片需新搜索才能进入。快照变化会标记未完成。相同计划并发修改返回 search_busy，写回和释放均检查所有权令牌；取消时有界保存已完成进度。不同计划不合并请求。

## 预算与配置

前台、派生兜底与后台使用同一 Redis 原子预算；排队时间也计入总时限。缓存未命中后的模型调用受网关约束。额度预留后不对结果不明的调用退款；预算拒绝不触发供应商熔断。供应商返回的 input/output/total tokens 按实际值累计，未提供时不推算。

| 配置 | 默认值 | 含义 |
|---|---:|---|
| SEARCH_SNAPSHOT_TTL_SECONDS | 600 | 快照与预算记录有效秒数 |
| SEARCH_SNAPSHOT_MAX_CANDIDATES | 300 | 单计划召回上限，达到上限明确标记截断 |
| SEARCH_TOTAL_TIMEOUT_SECONDS | 45 | 新解析/召回/核验共享总时限；已缓存结果仍可在快照有效期内翻页 |
| SEARCH_MAX_MODEL_CALLS | 20 | 调用额度 |
| SEARCH_MAX_VISUAL_CALLS | 3 | 视觉调用额度，仍服从最高开关 |
| SEARCH_MAX_VERIFIED_CANDIDATES | 60 | 核验候选额度 |
| SEARCH_MAX_BUDGET_UNITS | 60 | 加权调用额度 |

parse/embedding/text/visual 分别计 1/1/2/10 单位。这些单位是资源策略，不是人民币或供应商账单；Mock embedding 也会消耗调用额度。search_usage 暴露累计计数和供应商 token。Embedding 缓存键包含版本、模型、维度和用途；文本核验缓存包含证据、模型与提示版本；视觉缓存包含照片内容哈希和模型版本。

## 客户端与运行边界

原字段保留，新增 search_id、stop_reason、unverified_count、search_usage 和 verification_status，Web 类型同步。旧版或伪造游标返回 400，快照过期返回 410，计划忙返回 409；客户端应重新搜索。API 与搜索 Worker 应一起更新，缺少 plan_id 的预取作业拒绝执行；不需要清空 Redis。Redis 是搜索状态和预算的必要依赖，故障时请求失败，不绕过额度直接调用模型。

完整结果必须同时满足可靠范围、索引完整、候选未截断、分页结束、核验完成和快照未变化。全量召回性能、ANN 与跨计划 singleflight、真实模型质量/延迟/货币成本、完整浏览器 E2E 和微信真机仍需后续验证。本批未部署、未提交，也未推进阶段 6 发布 Gate。

## 验证

专项测试使用真实隔离 PostgreSQL/pgvector 与 Redis，模型接口使用可控替身，不产生付费调用。覆盖 HTTP/Agent 条件一致、HTTP 计划被真实 prefetch Worker 复用、前五拒绝第六匹配、零视觉调用、parser 复用、跨入口共享预算、并发预算、取消与旧所有者写入、JSON OR 和 partial_done、稳定同分分页、跨用户/篡改/过期游标、照片增删改与候选上限。

原第二批的同分游标单测迁移为更强的真实快照分页集成测试，原有预取和双入口测试迁移到新公共边界。最终结果与证据见 `.project-to-act/tasks/S6-BATCH3-001/evidence/E-S6-BATCH3-001.md`。
