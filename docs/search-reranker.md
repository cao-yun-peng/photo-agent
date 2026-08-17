# Photo Agent Top-K 判同重排

## 1. 目标与边界

向量相似度负责“召回可能相关的照片”，结构化矛盾校验负责文字、品牌、价格、日期、
路线等显式约束；两者都无法可靠处理“语义相近但关键主体/动作不存在”的开放集查询。
Top-K 判同重排在最终返回前判断查询与候选是否真正一致。

文本 v1 只向 `qwen-plus` 发送已经存入数据库的结构化描述。可选的视觉 v1 只在低置信
或细粒度近分候选上查看最多 3 张原图：

```text
向量召回 → 混合排序 → 结构化约束校验 → 游标过滤
→ 当前页前 K 个候选批量判同 → 按需二次视觉核验 → 高置信矛盾删除/分层重排 → 返回
```

它不是重复图片检测器。pHash、连拍聚类和相册去重属于另一条链路。

## 2. 运行时规则

- `match`：证据明确满足主体、场景、动作和关键属性；
- `contradiction`：证据明确与关键主体、动作、文字、颜色或场景冲突；
- `uncertain`：描述不足，生产中保留；
- 只有 `contradiction` 且 `confidence >= SEARCH_RERANK_REJECT_CONFIDENCE` 才删除；
- match 在 uncertain 前，同一层仍保留原混合分数顺序；
- 只判定 `min(SEARCH_RERANK_TOP_K, page_limit)` 个候选，保证一次分页内被重排的候选
  都会被消费；游标锚点按页面中最低原始分数计算，避免重排导致重复页；
- DashScope、Redis、熔断或 JSON 解析失败全部 fail-open；响应中的 `rerank_check`
  会标记 `degraded=true`，搜索本身仍成功。

Prompt 将 query/candidates 明确声明为不可信数据，并要求只能依据候选证据判断。缓存键包括
查询、候选证据、模型和 Prompt 版本；修改任一项都会自动产生新缓存。

## 3. 配置

```dotenv
SEARCH_RERANK_ENABLED=true
SEARCH_RERANK_MODEL=
SEARCH_RERANK_TOP_K=5
SEARCH_RERANK_REJECT_CONFIDENCE=0.8
SEARCH_RERANK_REQUIRE_MATCH=true
SEARCH_RERANK_TIMEOUT_SECONDS=12
SEARCH_RERANK_CACHE_TTL_SECONDS=604800
CB_SEARCH_RERANK_RECOVERY_INTERVAL=120
SEARCH_VISUAL_VERIFY_ENABLED=false
SEARCH_VISUAL_VERIFY_TOP_K=3
SEARCH_VISUAL_VERIFY_SCORE_GAP=0.05
SEARCH_VISUAL_VERIFY_TIMEOUT_SECONDS=45
SEARCH_VISUAL_VERIFY_CACHE_TTL_SECONDS=604800
SEARCH_VISUAL_VERIFY_IMAGE_URL_TTL_SECONDS=300
CB_SEARCH_VISUAL_VERIFY_RECOVERY_INTERVAL=180
```

`SEARCH_RERANK_MODEL` 为空时复用 `QWEN_CHAT_MODEL`。开启
`SEARCH_RERANK_REQUIRE_MATCH` 后，Top-K 中没有任何明确 `match` 时返回空结果，避免把
全是矛盾或证据不足的候选伪装成搜索成功；同时不会用未经过模型判定的候选回填被过滤的
位置，因此结果条数可以少于请求的 `limit`。API 请求可用
`verify_semantic=false` 显式关闭重排，便于同一服务运行基线。

VL v4 字段、触发策略、已有图片安全重算和评测协议见
`docs/visual-retrieval-v4.md`。二次视觉功能默认关闭，完成 development/validation A/B
前不应直接在生产流量开启。

冻结参数下的 development/validation 真实 A/B 均通过，详见
`docs/visual-retrieval-v4-results-2026-08-15.md`；该结果不包含新的未见 test。

## 4. 数据集

`tests/eval/retrieval_rerank_queries.json` 包含：

- 25 条正查询，每张新图至少一条；
- 10 条开放集/属性冲突负查询；
- 每条查询的 `candidate_judgments` 人工标签与理由；
- development/validation/test 为 21/7/7 条；
- validation/test 冻结，不参与 Prompt 或置信度调节。

25 张新图片全部为带“AI生成”水印的拟真合成图，写入 manifest 时使用
`source=synthetic`、`dataset_role=synthetic_robustness`，水印进入 `ignored_text`。
p-162/p-163 使用同一个 `group_id=cat_window` 并固定在 development。

## 5. 离线回放

不传 `--results` 时，脚本按人工候选标签顺序构造候选，验证数据与过滤逻辑的人工 oracle
上界；这不是模型成绩：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_search_reranker.py `
  --output artifacts\reranker-offline-derived.json
```

有真实未重排结果后，应在相同候选上做更可信的离线回放：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_search_reranker.py `
  --results artifacts\reranker-http-baseline.json `
  --output artifacts\reranker-offline-on-http-baseline.json
```

未被人工逐对标注的候选按 `uncertain_keep` 处理，不允许 oracle 擅自删除。

## 6. 真实 HTTP A/B

确保测试用户 JWT 位于 `PHOTO_EVAL_JWT`，然后运行：

```powershell
.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --queries tests\eval\retrieval_rerank_queries.json `
  --uuid-map artifacts\photo-eval\import-map-137.json `
  --no-verify-semantic `
  --output artifacts\reranker-http-baseline.json

.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --queries tests\eval\retrieval_rerank_queries.json `
  --uuid-map artifacts\photo-eval\import-map-137.json `
  --verify-semantic `
  --output artifacts\reranker-http-enabled.json
```

分别用 `scripts/retrieval_eval.py` 计算 Recall@5、Precision@5、MRR、无结果准确率和
禁返命中率。额外从 `diagnostics.*.rerank_check` 汇总：模型调用数、缓存命中、降级次数、
候选删除数和 P50/P95 增量延迟。

上线最低条件：正查询 Recall@5 不得下降超过 2 个百分点；高置信误删率不超过 5%；
validation/test 不允许因继续调 Prompt 被污染。任何结果都应把 112 张基础切片与 25 张
拟真合成困难切片分开报告。

2026-08-15 的冻结哈希、25 张真实导入状态、真实 HTTP A/B、held-out 失败分析和测试结果见
`docs/search-reranker-results-2026-08-15.md`。
