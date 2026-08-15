# Photo Agent 结构化矛盾校验：设计、使用与评测

## Material Passport

- **Origin Skill**：experiment-agent
- **Origin Mode**：run + validate
- **Origin Date**：2026-08-15
- **Verification Status**：VERIFIED（本地单测、真实 HTTP 与数据库链路均已执行）
- **Version Label**：structured_constraint_validation_v1
- **数据范围**：112 张隔离测试用户相册、原 50 条查询、30 条 development 负样本
- **模型调用**：真实 HTTP 评测沿用 Qwen 查询解析与 Embedding；校验器自身不新增模型调用
- **规则调试切分**：development
- **重要限制**：初次冻结 test 暴露“杯子/咖啡杯”词形回归；修复后的 test 复跑属于 post-fix regression，不再是未见测试

## 1. 解决的问题

向量检索擅长判断“整体相似”，但容易忽略只差一个关键属性的冲突。例如：

- 查询要“蒙牛纯牛奶”，相册只有“伊利纯牛奶”；
- 查询要座位 `9F`，登机牌实际是 `3A`；
- 查询要“上海虹桥→北京南”，车票实际方向相反；
- 查询要 `WORLD'S BEST MOM`，杯子实际写着 `WORLD'S BEST BOSS`。

这些候选语义很相似，不能依靠统一相似度阈值解决。校验器从查询提取少量高置信度强约束，
再要求候选的 `ai_analysis` 提供正向匹配证据。

## 2. 决策原则

1. 普通场景、物体和自然语言描述不触发校验，仍走原向量排序。
2. 只有明确的可见文字、品牌/专名、价格、座位、时间、台历日期或路线才触发。
3. 强约束查询把候选池扩大到至少 30 张，再检查结构化证据。
4. 候选必须满足该查询的全部强约束；缺少证据时不返回。
5. “写着 X 的 Y”同时检查文字 X 和承载物 Y，避免把代码里的 `Hello World` 当成
   “写着 HELLO 的门垫”。
6. Agent 的全相册兜底不能绕过已失败的强约束，否则会重新返回已知冲突图片。

支持的约束：

| 类型 | 查询示例 | 候选证据 |
|---|---|---|
| `visible_text` | 写着 WELCOME 的门垫 | `text_in_image`、对象、摘要中的规范化文字 |
| `object` | 写着 X 的杯子 | 承载物及通用别名，如杯子/咖啡杯 |
| `entity` | 依云矿泉水瓶 | OCR、对象和摘要中的品牌或专名 |
| `price` | 价格是 199 元 | 独立数字 token，避免把 99 错配到 199 |
| `seat` | 座位是 3A | 独立字母数字座位 token |
| `time` | 锁屏时间九点四十一 | 规范为 `09:41` 后比较 |
| `calendar_date` | 2026 年八月十五日的台历 | 台历/日历对象 + 年月日证据 |
| `route` | 北京南到上海虹桥的高铁票 | 有方向的起点→终点，不按无序地点集合处理 |

实现位于 `app/services/search_constraints.py`。

## 3. HTTP API 使用

`verify_constraints` 默认为 `true`：

```json
{
  "q": "写着WELCOME的门垫",
  "limit": 5,
  "auto_parse": true,
  "verify_constraints": true
}
```

响应额外包含不泄露被过滤图片内容的摘要：

```json
{
  "constraint_check": {
    "applied": true,
    "constraints": [
      {"kind": "visible_text", "value": "WELCOME", "source": "写着…的"},
      {"kind": "object", "value": "门垫", "source": "文字承载物"}
    ],
    "candidates_checked": 30,
    "matched_count": 1,
    "rejected_count": 29,
    "rejected_by_kind": {"object": 28, "visible_text": 29}
  }
}
```

排障或 A/B 对照时可以显式关闭：

```json
{"q": "写着WELCOME的门垫", "verify_constraints": false}
```

Agent 的 `search_photos` Tool 同样默认开启 `verify_constraints`。如果强约束候选全部失败，
`fallback_search` 返回空和明确提示，不再降级到时间线或全相册绕过约束。

## 4. 标签正确性修订

原 development 负样本 `DNQ-017` 为“三星电视遥控器”。人工复核原图后发现型号
`BN59-01199F` 和 Smart Hub 按键具有明显三星关联，因此把它作为确定负样本不可靠。
用例修订为“印着 SONY 标志的电视遥控器”，图片上明确没有 SONY 文字，仍以 `p-123`
作为近邻干扰项。修订后结构审计错误 0、警告 0。

## 5. 真实 HTTP 结果

所有批次均使用真实 JWT、Qwen 查询解析/Embedding、PostgreSQL/pgvector 和当前
`ai_analysis`，顺序执行、Top-5、失败不重试。

### 5.1 原 50 条查询

| 指标 | 校验前 | 校验后 |
|---|---:|---:|
| Recall@5 | 100.00% | 100.00% |
| Precision@5 | 20.83% | 36.46% |
| MRR | 0.9896 | 0.9896 |
| 无结果准确率 | 0.00% | 0.00% |
| 禁返命中率 | 8.00% | 8.00% |

原 50 条中 10 条触发强约束，共检查 300 个候选，保留 11 个、过滤 289 个。两条原始
无结果查询是“北极熊”和“火星表面自拍”，没有 OCR/数值等强约束，因此仍返回语义近邻。

### 5.2 development 负样本

| 类型 | 数量 | 校验前空结果率 | 校验后空结果率 |
|---|---:|---:|---:|
| 属性冲突 | 20 | 0.00% | 100.00% |
| 概念缺失 | 10 | 0.00% | 0.00% |
| 合计 | 30 | 0.00% | 66.67% |

development 31 条正样本 Recall@5 保持 100%，MRR 为 0.9839。4 条强约束正查询均从
30 个候选中保留唯一正确图片。

### 5.3 冻结检查和回归处理

- validation：Recall@5 100%、MRR 1.0；3 条强约束正查询全部保留。
- 初次 test：`RQ-045` 因“杯子/咖啡杯”对象词形不一致被误拒，Recall@5 降到 87.5%。
- 修复：增加通用对象别名并补单测；post-fix test Recall@5 恢复 100%、MRR 1.0，
  `RQ-045` 只返回正确的 `p-135`。

由于修复受 test 结果启发，post-fix 数字只能作为回归验证。需要新增未参与规则设计的查询，
才能重新声称冻结泛化成绩。

## 6. 复现

真实 HTTP 采集：

```powershell
.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --base-url http://127.0.0.1:8000 `
  --queries tests\eval\retrieval_negative_development.json `
  --uuid-map artifacts\photo-eval\import-map.json `
  --output artifacts\retrieval-structured-development-negative-http.json `
  --limit 5
```

冻结 VL 结果上的离线回放：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_structured_constraints.py `
  --queries tests\eval\retrieval_negative_development.json `
  --results artifacts\retrieval-negative-development-http.json `
  --predictions artifacts\vl-experiments\v3-development.json `
                artifacts\vl-experiments\v3-validation.json `
                artifacts\vl-experiments\v3-frozen-test.json `
  --split development `
  --output artifacts\structured-constraints-development-negative-replay.json
```

主要运行产物：

- `artifacts/retrieval-structured-all-http.json`
- `artifacts/retrieval-structured-all-eval.json`
- `artifacts/retrieval-structured-development-negative-http.json`
- `artifacts/retrieval-structured-development-negative-eval.json`
- `artifacts/retrieval-structured-validation-http.json`
- `artifacts/retrieval-structured-test-http.json`（初次冻结 test）
- `artifacts/retrieval-structured-test-postfix-http.json`（修复后回归）

## 7. 已知限制与下一步

1. 本功能不是通用 open-set 拒识器，不能判断“相册里完全没有熊猫/北极熊”。
2. 强约束依赖 VL 的 OCR 和对象字段；如果图片文字模糊或 VL 漏识别，可能误拒。
3. 当前中文规则和对象别名规模较小，应从新的 development bad case 扩展，不能从冻结集持续调参。
4. 强约束查询固定检查至少 30 个向量候选；超大相册需要评估延迟，并考虑数据库级候选预算。
5. 下一阶段应新增独立 validation/test 强约束用例，并另做“概念缺失”的判同模型或轻量重排，
   不应把统一语义阈值重新混入本规则。
