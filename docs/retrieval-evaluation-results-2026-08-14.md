# Photo Agent 真实检索评测与修复报告（2026-08-14）

## Material Passport

- **任务**：审计现有 50 条检索查询，在 112 张测试用户相册上运行真实检索，并修复已定位的问题
- **运行链路**：FastAPI `/search` + JWT + DashScope 查询解析/Embedding + PostgreSQL/pgvector + 混合排序
- **测试用户**：隔离用户 `photo-eval-manifest-v2`，112/112 张图片处理完成
- **原始查询集**：48 条正样本、2 条无结果查询
- **扩展开发集**：30 条无结果查询，其中 20 条属性冲突、10 条相册内概念缺失
- **运行策略**：顺序执行、Top-5、单批失败不重试
- **外部数据发送**：只发送短查询文本到 DashScope；本轮未重新上传图片
- **原始查询 SHA256**：`DFEF4A299FE94700A0262AFA0489D3AB1A813D1CB51B981F3B740E8A4939C5F1`
- **扩展负样本 SHA256**：`B33713C4F90C9578CDA4DD582D8B6D4A986270EDA590636BC237A44C98A2D2DF`
- **验证状态**：`ANALYZED`。修复后的 50 条 HTTP 批次与 30 条开发负样本各运行一次；未做同配置重复性复跑

## 1. 评测集正确性审计

### 1.1 原 50 条查询

逐条检查了查询文本、目标 `p-xxx`、最终成图和干扰项：

- 48 条正样本均与最终成图语义一致，没有直接把生成 Prompt 当标准答案；
- 两条多正例标注完整：生日蛋糕（`p-007/p-086`）和演唱会（`p-012/p-089`）；
- “北极熊”和“火星表面自拍”在 112 张相册内确实不存在；
- 查询 ID、引用 ID、正负样本互斥和重复项检查均通过；
- 28 个查询—目标图片关系跨越图片自身切分。这不影响当前“查询级切分”，但不能声称测试图片从未参与开发。

审计产物：

- `artifacts/retrieval-query-audit.json`
- `artifacts/retrieval-query-audit-sheets/retrieval-query-audit-*.jpg`

### 1.2 新增 30 条 development 负样本

原数据只有 2 条负样本，无法稳定校准拒识。新增
`tests/eval/retrieval_negative_development.json`：

| 类型 | 数量 | 示例 | 目的 |
|---|---:|---|---|
| 属性冲突 | 20 | 蒙牛 vs 伊利、9F vs 3A、299 元 vs 199 元、HELLO vs WELCOME | 检查检索是否忽略 OCR/品牌/数值/方向否定条件 |
| 概念缺失 | 10 | 雪地哈士奇、熊猫吃竹子、埃菲尔铁塔夜景 | 检查相册内不存在目标时能否返回空 |

每条属性冲突用例都记录了 `confuser_photo_ids`，人工核对“近邻图片存在，但并不满足查询”；
10 条概念缺失也逐条检查了 112 张相册的覆盖范围。结构审计结果：30 条有效，错误 0、警告 0。

审计产物：

- `artifacts/retrieval-negative-development-audit.json`
- `artifacts/retrieval-negative-development-sheets/retrieval-query-audit-*.jpg`

### 1.3 指标解释限制

- 原 validation/test 各只有 1 条负样本，无结果准确率只能是 0% 或 100%，统计证据很弱。
- 46 条正查询只有 1 张相关图，2 条有 2 张。固定返回 5 张且全部命中时，宏平均
  Precision@5 的数学上限就是 20.83%；本数据集应主要看 Recall@5、MRR 和拒识能力。
- 查询大多是规范描述，尚缺口语、省略、错别字、多轮指代和含糊查询。

## 2. 基线故障与代码修复

### 2.1 基线结果与归因

修复前 `auto_parse=True` 的直接处理函数基线为：Recall@5 60.42%、MRR 0.5938。
19/48 条正样本被自动解析生成的硬过滤清空：

- 16 条被自动 `tags` 转成 `PhotoTag` 硬过滤；
- 3 条把画面里的日期误当照片拍摄时间，包括银杏季节、春节聚餐和台历日期。

关闭自动解析硬过滤后，48/48 正样本都进入 Top-5，47/48 排在 Top-1，说明主要问题不在向量召回。

### 2.2 已实施修复

1. **自动标签改为软语义**：LLM 自动提取的普通物体/OCR 词不再写入 `payload.tags`；只有调用方显式传入的标签才进入数据库硬过滤。
2. **日期过滤增加拍摄意图门控**：只有“去年拍的、上周拍摄、某天照的”等明确拍摄时间表达才使用 `taken_at`。台历、车票、登机牌、锁屏、菜单、标签、便签和“写着/显示”等保留完整原句做语义搜索。
3. **HTTP 应用启动修复**：补齐 `app/main.py` 缺失的 `AsyncSessionLocal`、`init_registries` 导入。
4. **注册表启动修复**：移除对模型不存在字段 `Skill.is_deleted` 的查询，服务启动时可正常刷新 Skill Registry。
5. **采集器加固**：JWT 默认从 `PHOTO_EVAL_JWT` 读取；直接支持导入脚本的 `import-map.json`；本地请求默认 `trust_env=False`，避免系统代理劫持 `127.0.0.1`；保存解析结果和各阶段分数。

第一次 HTTP 复验产生的 `artifacts/retrieval-real-50-fixed-http.json` 为 0/50、全部 503。
诊断确认请求被系统代理接管，没有到达本地服务，因此该批次属于基础设施失败，**不计入模型或检索指标**。

## 3. 修复后的真实 HTTP 结果

修复后通过 `/search`、真实 JWT 和当前测试用户运行 50 条，50/50 完成、错误 0、耗时
251.6 秒。原始输出为 `artifacts/retrieval-real-50-fixed-http-v2.json`，SHA256：
`D2EBE13ED0E38AFD14E9D71026E9850B3200D6C17928E20ACA4FE624872B1196`。

| 切分 | 正/负查询 | Recall@5 | Precision@5 | MRR | 无结果准确率 | 禁返命中率 | 门禁 |
|---|---:|---:|---:|---:|---:|---:|---|
| 全量 | 48 / 2 | 100.00% | 20.83% | 0.9896 | 0.00% | 8.00% | FAIL |
| development | 31 / 0 | 100.00% | 21.29% | 0.9839 | N/A | 6.45% | FAIL |
| validation | 9 / 1 | 100.00% | 20.00% | 1.0000 | 0.00% | 10.00% | FAIL |
| test | 8 / 1 | 100.00% | 20.00% | 1.0000 | 0.00% | 11.11% | FAIL |

HTTP 修复批次的 50 组 Top-5 ID 与此前 `auto_parse=False` 的诊断对照 **50/50 完全一致**。
这说明修复恢复了语义召回，同时 HTTP/JWT/序列化壳层没有改变排序。

门禁仍失败不是召回问题，而是系统尚没有可靠的“无匹配”拒识，并且严格禁返指标要求
干扰图完全不能进入 Top-5。5 组人工干扰项中，目标图都排在干扰图之前；后续应同时报告
Top-K contamination 与 pairwise accuracy，不能互相替代。

## 4. 30 条负样本与阈值校准

30 条 development 负样本通过修复后的真实 HTTP 链路运行，30/30 完成、错误 0、耗时
242.6 秒。系统当前总会返回 5 张，因此未经拒识时无结果准确率为 0%。原始输出：
`artifacts/retrieval-negative-development-http.json`，SHA256：
`2CE99D47CA41514B0F1AC97E86618A237C2FC9E684024ED0570D497306BADFED`。

### 4.1 分数分布

| development 组别 | 数量 | 最低 | 中位数 | 最高 | 均值 |
|---|---:|---:|---:|---:|---:|
| 正样本 | 31 | 0.7242 | 0.8896 | 0.9442 | 0.8846 |
| 全部负样本 | 30 | 0.7344 | 0.8355 | 0.8954 | 0.8310 |
| 属性冲突负样本 | 20 | 0.7344 | 0.8413 | 0.8954 | 0.8408 |
| 概念缺失负样本 | 10 | 0.7685 | 0.7972 | 0.8781 | 0.8113 |

正负区间高度重叠，属性冲突尤其容易取得高相似度，因为查询和图片共享大量正确词，只有一个
品牌、文字、数值或方向不一致。

### 4.2 只在 development 选择阈值

规则为 `top score_semantic < threshold` 时返回空；选择时要求正样本接受率至少 95%，
目标负样本拒绝率至少 90%。校准脚本为 `scripts/calibrate_retrieval_threshold.py`。

| 选择方式 | 阈值 | 正样本接受率 | 负样本拒绝率 | 属性冲突拒绝 | 概念缺失拒绝 |
|---|---:|---:|---:|---:|---:|
| 保证正样本 ≥95% 的最优点 | 0.8006 | 96.77%（30/31） | 26.67%（8/30） | 15.00% | 50.00% |
| 不加召回约束、平衡准确率最优 | 0.88645 | 58.06%（18/31） | 93.33%（28/30） | 90.00% | 100.00% |

结论：**单一语义阈值不能同时达到 95% 正样本接受率和 90% 负样本拒绝率，暂不应写入生产搜索。**
0.8006 会放过 22/30 负样本；0.88645 虽能拒绝大多数负样本，却会误拒 13/31 正样本。

把 0.8006 应用于冻结 validation/test 时，现有各 1 条负样本均被拒绝且正样本均保留，
但每个切分只有一条负样本，样本量不足，不能用“100%”否定扩展 development 的结果。

完整产物：`artifacts/retrieval-threshold-calibration.json`。

## 5. 下一步方案

按优先级：

1. **结构化矛盾校验**：当查询包含明确 OCR、品牌、数值、日期、座位号或路线时，从
   `ai_analysis.text_in_image`/结构字段中做一致性检查；冲突候选直接降权或剔除。这比抬高全局阈值更针对属性冲突。
2. **Top-K 轻量重排/判同**：将查询与前 5 个候选的结构化描述交给同一判别 Prompt，输出
   `match / contradiction / uncertain`；先在 30 条 development 负样本调 Prompt，再冻结到新验证集。
3. **扩充冻结负样本**：validation/test 各补至少 15–20 条，且不参与阈值或 Prompt 设计。
4. **增加自然改写**：为核心查询补短句、错别字、口语、省略和多轮指代，报告稳健性而非只报标准查询。
5. **明确切分政策**：若要宣称图片级泛化，应让查询与目标图片处于同一切分；否则明确使用查询级切分。

## 6. 复现命令

JWT 只通过环境变量提供；本地 `127.0.0.1` 默认不继承系统代理：

```powershell
.\.venv\Scripts\python.exe scripts\audit_retrieval_queries.py `
  --queries tests\eval\retrieval_negative_development.json `
  --manifest tests\eval\photo_manifest.json `
  --output artifacts\retrieval-negative-development-audit.json

.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --base-url http://127.0.0.1:8000 `
  --queries tests\eval\retrieval_negative_development.json `
  --uuid-map artifacts\photo-eval\import-map.json `
  --output artifacts\retrieval-negative-development-http.json `
  --limit 5

.\.venv\Scripts\python.exe scripts\calibrate_retrieval_threshold.py `
  --positive-queries tests\eval\retrieval_queries.json `
  --positive-results artifacts\retrieval-real-50-no-auto-parse.json `
  --negative-queries tests\eval\retrieval_negative_development.json `
  --negative-results artifacts\retrieval-negative-development-http.json `
  --output artifacts\retrieval-threshold-calibration.json
```

校准使用的正样本分数来自修复前保存的纯语义诊断文件，因为最初的修复后 HTTP 采集器尚未
保存诊断分数。两份结果的 50 组 Top-5 ID 已逐项核对完全一致；未来复跑应直接使用增强后的
HTTP 采集器输出完成全链路校准。

## 7. 最终验证

- Ruff：本轮涉及的应用、采集、审计、评分与校准文件全部通过；
- pytest：查询解析、API/Agent 搜索策略、注册表、采集器、图片评测、阈值校准及 Phase 3/4
  共 70 项通过；
- 服务健康检查：`/ready` 返回 API、Redis、数据库均为 `ok`；
- 修复后 HTTP Top-5 与纯语义诊断结果逐条对比：50/50 完全一致。

## 8. 2026-08-15：结构化矛盾校验

统一语义阈值无法区分“整体相似但品牌/数值相反”的候选，因此新增了可见文字、承载物、
品牌/专名、价格、座位、时钟时间、台历日期和有方向路线的结构化证据校验。

- 原 50 条：Recall@5 保持 100%，Precision@5 从 20.83% 提升到 36.46%；
- 20 条 development 属性冲突：空结果率从 0% 提升到 100%；
- 10 条概念缺失：仍为 0%，明确不属于该规则覆盖范围；
- 初次 test 发现“杯子/咖啡杯”词形回归，增加通用对象别名后恢复；修复后 test 只作为
  post-fix regression，不再宣称为未见测试。

详细设计、API 和实验记录见 `docs/structured-constraint-validation.md`。
