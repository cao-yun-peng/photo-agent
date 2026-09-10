# 测评后的搜索优化：第一批修复

2026-09-09，任务 S6-SEARCH-OPT-20260909，证据 E-S6-SEARCH-OPT-20260909。第一批修复及 Docker 集成补验均已完成：最终合并运行 198 项测试全部通过，无跳过或失败。未进行新的付费模型评测或生产发布；上一轮数据与报告保持原样。

## 已实现的变化

| 位置 | 原行为 | 当前行为 |
|---|---|---|
| 严格核验的浏览结果 | 找到明确匹配后，仍继续扫描以凑满请求数量，容易耗尽 45 秒预算 | 消费完当前已核验结果后，只要本页非空就先返回；保留后续游标，不要求凑满五张 |
| 文字和载体约束 | 将复杂请求整句当专名、将分开的 OCR 当连续字符串、要求颜色和对象紧挨出现 | 不确定关系句交给后续语义核验；拆分明确 OCR 列表，识别常见载体别名，跳过“照片”等泛称 |
| 核验响应解析 | 合法裸 JSON 数组被对象解析器丢弃；部分非法字段可能被跳过或夹到有效范围 | 搜索专用解析器接受完整对象、数组及合法代码围栏；候选集合、字段类型、置信度和判断枚举严格校验 |

入口仍是 [SearchService](E:/project/agent/photo-agent/app/services/search_engine.py)。Agent 搜索工具和搜索 API 继续复用同一实现。本轮不修改数据库结构或 HTTP 请求字段。

## 提前返回的边界

`SEARCH_BROWSE_EARLY_RETURN=true` 为默认值。只有严格核验的 `browse` 前台请求启用；`soft`、`select`、完整集合和后台预取不启用。没有找到明确匹配时仍继续扫描，不会把 `uncertain` 当作成功。

短页还有后续候选时，返回 `stop_reason=verified_batch_ready`、非空 `items` 和 `next_cursor`，不把计划写成终止或相册已穷尽。已有结果按原索引继续消费；排除项不会造成空页提前结束。配置设为 false 可回退到原来的填页行为。

控制流回归中，同样的 12 个候选、三个分散匹配，在开关关闭时首轮执行三批文本核验；开启时首轮执行一批并返回首批匹配，后续两页仍能取到另外两个匹配。这是固定响应下的调用行为验证，不是实际模型延迟从 45 秒缩短到某个数值的证据。

**共享预算仍是计划创建起计算的 45 秒绝对时间。** 游标默认有效 600 秒，并不表示仍有 600 秒可用于模型核验。提前返回后，已有核验结果可继续读取；等待过久再请求未核验候选仍会得到 `deadline_exceeded`，不会重置调用、候选或费用额度。已增加延迟续页回归。将用户等待时间与实际处理时间分开计费/计时，需要后续单独设计。首批核验自身仍可能慢或失败，本轮不作延迟达标承诺。

## 约束层的实际回放

对之前已见的开发/验证图片索引与查询，使用旧归档中的原函数和新函数比较全部 32,929 个查询—照片对。标签只用于事后归类，不传给规则函数；没有重跑检索、模型或改写旧成绩。

| 数据 | 正例新增通过规则 | 普通负例新增通过规则 | 指定难负例变化 | 正例新增拒绝 |
|---|---:|---:|---:|---:|
| 开发：29,729 对 | 4 | 136 | 0 | 0 |
| 已见验证：3,200 对 | 3 | 0 | 0 | 0 |

7 个被修复的正例涉及关系句中的报纸名、I/心形/NY、棕色门垫、门店招牌，以及限速/停车标志。136 个普通负例新增通过都来自取消一个报纸关系句的错误专名过滤；这些候选仍需后续语义核验，可能增加后续检查量。颜色、心形和复杂关系的语义事实没有由规则层证明。

明确的数值和文字冲突仍有反例测试，例如 `50` 不应匹配 `150`，`2 hours` 不应匹配 `12 hours`，`I` 不应匹配 `FIRST`。支持的是通用表达边界，不按图片 ID 特判。

回放脚本：[constraint-replay.py](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/constraint-replay.py)；结果：[constraint-replay.json](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/constraint-replay.json)。这是已见数据上的规则回归，不能称为新的独立验证或最终检索精度提升。

## 输出格式契约

[search_decisions.py](E:/project/agent/photo-agent/app/services/search_decisions.py)先解析整个 JSON，再验证完整候选集合。重复/未知/缺失候选、重复 JSON 字段、非法判断、缺失字段、非数值或非有限/越界置信度均整批拒绝，不默认补 `match`。带解释的非完整 JSON、缺少 rationale、字符串置信度等也会拒绝；没有承诺接受任意畸形格式。

旧 val-008/013 两条真实裸数组内容已复制到独立回归测试：新入口保留全部五项原判断，val-013 的 contradiction 仍为 contradiction。格式可解析不代表模型判断正确。文本与视觉缓存均加入 `search_decisions_v2` 契约版本，避免复用旧的宽松解析缓存；没有修改提示词。

## 验证与后续工作

首轮离线命令（历史记录）：

```powershell
.venv/Scripts/python.exe -B -m pytest tests/test_search_optimization.py tests/test_search_constraint_regression.py tests/test_search_decision_contract.py tests/test_batch2_correctness.py tests/test_batch3_search.py tests/test_batch4_search.py tests/test_search_feedback.py -m 'not integration' -q -p no:cacheprovider
```

结果 **172 passed，21 deselected**：13 项搜索控制流、25 项约束、100 项输出契约，以及 34 项既有日期/参数/反馈回归。调用轨迹测试采用固定模型响应、内存状态与预算替身；两类解析器还通过模拟 HTTP 响应覆盖实际请求后的解析和用量处理。无外部模型调用。

改动文件 Ruff 通过。只读审查确认分页和新停止码兼容，并指出上述时间预算边界，已加入回归与说明。首轮因 Docker daemon 不可用，21 项真实 PostgreSQL/Redis 集成测试未执行；这项缺口现已通过下述补验关闭。[测试输出](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/pytest-final.txt)和 [JUnit](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/pytest-final.xml)已保存。

连接故障、语义误判、否定表达与旧索引缺失、视觉触发策略，以及浏览等待期间的预算续接仍需后续处理。隔离集成验证已补齐；上线前仍应使用新的配置版本与未见样本测量首屏可用结果、跨页找回率、延迟和失败率；首屏提前返回会改变一次请求的 Top-5 含义，不能直接与旧填满五张的指标作无条件比较。

当前工作区源码已经是新版本，原 `scripts/retrieval_eval/run.py` 对旧冻结文件的校验预期会拒绝直接运行。旧版本的完整源码/数据归档与证据仍在 S6-RETRIEVAL-20260908；后续实验应独立版本化，不修改旧清单来强行通过。整体阶段 6 revision9 与发布验收保持不变。

## Docker 集成补验（2026-09-09）

用户开启 Docker 后，创建独立的 `search-opt-it-20260909` 项目，PostgreSQL 绑定本机 55439、Redis 绑定 56389/DB15；应用现有迁移至 `20260907_0001`。测试数据库身份显式校验为 `photo_agent_batch1_test`，不使用主应用数据库。环境为 PostgreSQL 16.15、pgvector 0.8.6、Redis 7.4.10。

先运行原 21 项：全部通过；再运行新增 5 项真实集成：全部通过；最后与原 172 项离线回归在同一进程合并运行，**198 passed，0 skipped，0 failures，0 errors**。合并运行耗时 51.96 秒是测试套件耗时，不是搜索请求延迟。

新增用例使用真实 SQL 查询、Redis 快照、签名游标与预算；只在模型边界提供固定响应，并经真实 `model_call` 进行 Redis 预留。覆盖：跨新数据库会话翻页不丢失/重复；读取核验余项不增加调用；绝对 deadline 到期停止；调用上限不能经翻页重置；数据库删除已保存候选后继续寻找。共有 26 项数据库/Redis 集成用例。全部受测行为通过，未发现需要修改业务源码的新问题。

记录：[完整输出](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/integration-20260909/regression-20260909T072915912161Z.txt)、[JUnit](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/integration-20260909/regression-20260909T072915912161Z.xml)、[环境](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/integration-20260909/environment.json)、[补验汇总](E:/project/agent/photo-agent/.project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/integration-20260909/summary.json)。

复跑入口（先启动专用测试容器）：

```powershell
docker compose -p search-opt-it-20260909 -f tests/compose.batch1.yml up -d --wait
.venv/Scripts/python.exe -B .project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/integration-20260909/run-integration.py prepare
.venv/Scripts/python.exe -B .project-to-act/tasks/S6-SEARCH-OPT-20260909/evidence/integration-20260909/run-integration.py regression
```

模型密钥在测试子进程中清空，测试不产生真实模型费用。测试样本用户和照片已由 fixture 清理，检查为 0/0；本次专用容器已停止并保留数据卷，主应用未重启或停止。源码及上一轮证据未因补验改动；新增测试文件与日志单独保存。补验确认工程行为，不提供真实模型准确率或延迟提升结论。
