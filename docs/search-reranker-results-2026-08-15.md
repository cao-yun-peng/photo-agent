# Top-K 判同重排实验记录（2026-08-15）

## Material Passport

- Experiment ID：`search-reranker-v1-20260815`
- Type：合成鲁棒性切片导入、真实 Qwen-VL/Embedding 处理、真实 HTTP A/B
- Execution Status：`VERIFIED`（35/35 请求成功、0 运行错误、0 降级）
- Quality Gate：development `PASS`；validation/test/overall `FAIL`
- Data Access：隔离测试用户、独立 OSS 前缀、PostgreSQL、Redis
- External Data：未上传私人相册；新增图片均为带水印的拟真合成图
- Prompt Version：`topk_match_v1`
- Model：`qwen-plus`；图片理解和向量模型沿用项目 `.env` 配置
- Secret Handling：密钥只由 `.env` 读取；JWT 仅在执行进程内生成，未写入产物

## 1. 数据、标签与哈希

- manifest：137 张，development/validation/test = 81/28/28；
- 新增困难切片：25 张，切分 15/5/5；全部标记为 `synthetic_robustness`；
- 查询：35 条，其中正查询 25、开放集/属性冲突负查询 10；
- 查询切分：development/validation/test = 21/7/7；
- 静态标签审计：25 张新图全部被引用，0 错误、0 警告；
- `p-162/p-163` 同属 `group_id=cat_window` 且都位于 development，避免近重复跨切分泄漏；
- development 的 RRQ-001 原文同时合理匹配两张食物图，已在冻结前改为明确的“蜡烛旁饺子和炒菜”。

| 产物 | SHA256 |
|---|---|
| `tests/eval/photo_manifest.json` | `49193cc94155c147dc3e45109b64f7c197e0e97ead80dc7b209412061e63b5ae` |
| `tests/eval/retrieval_rerank_queries.json` | `1750aed46804f464d3c22c5de3614cfd07262b0e4b5301fbb4e9bc922e78ec97` |
| `app/services/search_reranker.py` | `a89531b7eb343f4c7844422522ee120d699b85c043f70b646fbb5aabf7d6d8f4` |
| `artifacts/photo-eval/import-map-137.json` | `a55cb01c7bebb274b1695bb22ffcac162551caf7e6df50940921809c2de03e05` |
| `artifacts/reranker-query-audit-final.json` | `59c5d71c11860f6507b24608561ed660fb7e14070ba0d1a5313d19335e814f91` |

## 2. 导入与图片解析

第一次正式导入复用原有 112 张，新建并入队 25 张：

```text
requested=137 created=25 reused=112 queued=25 failed=0
database=reachable redis=reachable oss=reachable
```

真实运行暴露了两个基础设施问题：

1. Windows 失效系统代理被 `httpx` 继承，使 DashScope 请求出现 `ConnectTimeout`；
2. worker 固定 `max_jobs=10` 会并发发起多组 VL/Embedding 请求，直连后仍有少量连接超时。

对应修改：

- `app/services/ai.py` 与 `app/services/query_parser.py` 的 DashScope 客户端统一
  `trust_env=False`；
- 新增 `WORKER_MAX_JOBS`，默认从 10 下调为 4；
- 单图真实冒烟测试得到非空描述和 1024 维 embedding 后，只重试未完成 UUID；
- 最终刷新 import map 时使用 `--no-enqueue`，确认 25/25 为 `done`，描述、结构化分析和
  embedding 均为 25/25。

这两项修改修复的是网络代理和突发并发，不是通过修改图片标签或伪造模型输出绕过失败。

## 3. 重排策略

- 当前页前 5 个候选一次批量调用文本模型；
- 输出 `match / contradiction / uncertain`；
- 高置信 `contradiction` 删除，`match` 排在 `uncertain` 前；
- `SEARCH_RERANK_REQUIRE_MATCH=true` 时，零 match 返回空列表；
- 严格模式不再用未经过模型判定的候选回填被过滤位置；
- Redis 缓存键包含查询、候选证据、模型和 Prompt 版本；
- 使用独立 `dashscope_search_rerank` 熔断器；
- 模型、缓存、网络或 JSON 解析失败时 fail-open，保留原排序；
- HTTP 和 Agent 搜索共用同一实现；`verify_semantic=false` 提供同服务基线。

零 match 门禁和禁止未判定回填只依据 development 失败案例确定。随后冻结 Prompt、阈值
0.8、Top-K=5 和门禁策略；validation/test 结果没有继续用于调参。

## 4. 真实 HTTP A/B

统一最终原始结果：

| 产物 | SHA256 |
|---|---|
| `artifacts/reranker-http-baseline-final.json` | `2f28780f8ad351cb01eeab0b429964e7a4d5977127d82bcbf20c2ea2ac413df5` |
| `artifacts/reranker-http-enabled-final.json` | `b39d0b1210bf6b9880b9e9fbc6f4a65cbbc66158120c89ed6184bbf3535834a8` |
| `artifacts/reranker-http-baseline-final-metrics.json` | `19db06029f017c099216c196ee25309bb373843b1f4aab9dd4b53b4fadaf2d1b` |
| `artifacts/reranker-http-enabled-final-metrics.json` | `4c093b53cdf043b782c470c101619c0945928907b2af4d17d34b5031fda97870` |

整体结果：

| 指标 | baseline | Top-K 判同 | 变化 |
|---|---:|---:|---:|
| Recall@5 | 88.00% | 80.00% | -8.00 pp |
| Precision@5 | 18.40% | 61.00% | +42.60 pp |
| MRR | 0.7433 | 0.7800 | +0.0367 |
| 无结果准确率 | 0.00% | 100.00% | +100.00 pp |
| 禁返图片命中率 | 48.57% | 5.71% | -42.86 pp |
| 门禁 | FAIL | FAIL | held-out 召回未达标 |

分切结果：

| Split | Variant | Recall@5 | Precision@5 | MRR | 空结果准确率 | 禁返命中率 | Gate |
|---|---|---:|---:|---:|---:|---:|---|
| development | baseline | 86.67% | 18.67% | 0.8000 | 0.00% | 47.62% | FAIL |
| development | rerank | 86.67% | 64.44% | 0.8333 | 100.00% | 4.76% | PASS |
| validation | baseline | 80.00% | 16.00% | 0.6000 | 0.00% | 28.57% | FAIL |
| validation | rerank | 60.00% | 46.67% | 0.6000 | 100.00% | 0.00% | FAIL |
| test | baseline | 100.00% | 20.00% | 0.7167 | 0.00% | 71.43% | FAIL |
| test | rerank | 80.00% | 65.00% | 0.8000 | 100.00% | 14.29% | FAIL |

运行特征：

- 最终两组均为 35/35 请求成功、0 HTTP 错误；
- 重排 0 次降级；最终复跑 34/35 命中判定缓存，另 1 条无候选、无需模型调用；
- 34 次冷模型调用 P50 = 7.328 s，P95 = 10.062 s，最大 10.937 s。

## 5. 失败分析

重排明显改善了开放集拒识和精度，但严格零 match 门禁会放大上游元数据遗漏：

- RRQ-017 的目标 p-144 实际 VL 描述只有“车内、阴雨、车流”，缺少查询要求的“公交车”和
  “拍糊”，模型判为 uncertain，严格门禁返回空；
- RRQ-022 的目标 p-145 被 VL 描述成“一人走过沙发”，缺少“孩子”和“跑动”，同样被误删；
- RRQ-020 的目标 p-161 根本没有进入 baseline Top-5，重排器无法召回一个不在候选集中的正例；
- RRQ-025 中 p-158 也是雨天车内透窗街景，与查询存在合理部分匹配，因此仍被保留；
- development 唯一禁返错误 RRQ-015 是 p-162/p-163 橘猫近重复，现有文本元数据没有
  “右侧/侧身”信息，文本判同器无法可靠区分。

因此当前结论不是“重排器无效”，而是：文本 Top-K 验证能显著降低误召回，但严格拒识的
召回上限受候选召回和 VL 元数据粒度共同限制。下一轮应先在 development/validation 上增加
图像质量、动作、年龄、拍摄载体和空间方位字段，或只对低置信检索启用二次视觉判定；现有
test 已查看，后续调参不能继续把它当作未见测试集。

## 6. 代码验证与环境说明

- 变更文件 Ruff：PASS；
- Top-K/manifest 定向测试：22/22 PASS；
- 后续完成单张照片质量门禁语义统一后，完整 pytest：143 PASS；
- `embedding_missing` 统一为可重试的 `partial_done`；畸形、非有限值或异常范数向量进入
  `skip`，避免污染索引；
- Docker Desktop、PostgreSQL 和 Redis 正常；Docker Hub 鉴权端点不可达，API/worker 因此
  使用项目 `.venv` 在宿主机后台运行，数据库与 Redis 仍使用隔离容器；
- 未修改或输出项目 `.env` 中的 OSS、DashScope、数据库和 JWT 密钥。

## 7. 复现命令

```powershell
# 标签审计
.\.venv\Scripts\python.exe scripts\audit_retrieval_queries.py `
  --queries tests\eval\retrieval_rerank_queries.json `
  --manifest tests\eval\photo_manifest.json `
  --output artifacts\reranker-query-audit-final.json

# baseline
.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --queries tests\eval\retrieval_rerank_queries.json `
  --uuid-map artifacts\photo-eval\import-map-137.json `
  --no-verify-semantic `
  --output artifacts\reranker-http-baseline-final.json

# rerank
.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --queries tests\eval\retrieval_rerank_queries.json `
  --uuid-map artifacts\photo-eval\import-map-137.json `
  --verify-semantic `
  --output artifacts\reranker-http-enabled-final.json
```

运行采集脚本前应通过 `PHOTO_EVAL_JWT` 提供隔离用户 token；不要把 token 写进命令历史、
结果 JSON 或 Git。
