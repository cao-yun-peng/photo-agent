# Photo Agent 图片数据集与三层评测

## 1. 数据集状态

`tests/eval/photo_manifest.json` 是当前唯一可用于自动评分的图片标准答案。它由
`test_photos` 的 112 张基础成图与 `test_photos_realistic` 的 25 张拟真合成困难图
逐张复核得到，共 137 张，而不是从生成 Prompt 直接复制标签。后 25 张全部带
“AI生成”水印，只能单独报告为 synthetic robustness slice，不能表述为真实手机照片效果。

人工标注策略：

- 场景允许多个合理答案，例如 `餐厅/室内`；
- 物体只标必须识别的核心对象，额外合理对象不算误检；
- 小规模清晰人物使用精确人数，大群像和遮挡场景使用人数区间；
- OCR 分为必识别文字和可选文字，可选文字漏识别不扣分；
- 所有路径均为仓库相对路径，不再使用生成机器的绝对路径。
- `p-162/p-163` 同属 `cat_window` 近似连拍组，固定在 development，禁止跨切分；
- 拟真合成困难集按 15/5/5 加入 development/validation/test，合并后为 81/28/28。

### 导入隔离测试用户

`scripts/import_photo_eval_dataset.py` 会创建或复用固定测试用户
`photo-eval-manifest-v2`，把清单图片上传到该用户独立的 OSS 前缀，再写入
`photos` 表。脚本按用户和 SHA256 去重，中断后可直接重跑。人工标准答案只保留
在清单中，不写入业务表，避免检索评测的数据泄漏。

图片 MIME 和宽高以解码后的真实文件内容为准，而不是依赖扩展名；重跑时会幂等
修正已有记录的 `size_bytes`、`mime_type`、`width` 和 `height`。

先检查数据库、Redis 和 OSS（输出不会包含密钥）：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py --check
```

只验证 137 张图片的路径、哈希、尺寸和 MIME，不访问外部服务：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py --dry-run
```

导入并将 pending 照片送入现有 Worker：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py `
  --output artifacts\photo-eval\import-map-137.json
```

如果 Worker 暂时未启动，可先只上传和写库，之后再次执行不带
`--no-enqueue` 的命令即可复用照片并补入队：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py `
  --no-enqueue `
  --output artifacts\photo-eval\import-map-137.json
```

本评测使用的导入映射保存在 `artifacts/photo-eval/import-map-137.json`，记录每个 `p-xxx` 对应的
数据库 Photo UUID、OSS key、处理状态和是否成功入队。该文件属于运行产物，不应
提交到 Git。

重新构建清单：

```powershell
.\.venv\Scripts\python.exe scripts\build_photo_manifest.py
```

清单完整性检查：

```powershell
.\.venv\Scripts\python.exe scripts\offline_eval.py `
  --dataset tests\eval\photo_manifest.json `
  --validate-only
```

## 2. 第一层：VL 图片理解

目标是单独检验 Qwen-VL 能否把图片转成可靠的结构化信息。指标包括场景准确率、
人数区间准确率、核心物体 Macro Recall、OCR Macro Recall 和 JSON 解析成功率。

DashScope 必须能通过 HTTP(S) 获取图片。先把 `test_photos` 上传到专用的公开或
临时签名 OSS 目录，然后执行：

```powershell
.\.venv\Scripts\python.exe scripts\offline_eval.py `
  --dataset tests\eval\photo_manifest.json `
  --split development `
  --url-prefix "https://your-bucket.example.com/photo-eval" `
  --output artifacts\vl-eval-development.json
```

调好 Prompt 后只运行冻结测试集：

```powershell
.\.venv\Scripts\python.exe scripts\offline_eval.py `
  --dataset tests\eval\photo_manifest.json `
  --split test `
  --url-prefix "https://your-bucket.example.com/photo-eval" `
  --output artifacts\vl-eval-test.json
```

也可以把模型输出保存成 `photo_id -> analysis` JSON，然后离线复算：

```powershell
.\.venv\Scripts\python.exe scripts\offline_eval.py `
  --predictions artifacts\vl-predictions.json
```

不要用 mock 模式的固定描述评价 VL 能力。

## 3. 第二层：真实检索排名

`tests/eval/retrieval_queries.json` 包含 50 条人工查询，包括语义、场景、群像、OCR、
相似干扰项和无结果查询。Top-K 判同另使用 35 条人工查询与候选标签
`tests/eval/retrieval_rerank_queries.json`。先将 137 张图片通过真实上传和处理链路写入 OSS、
PostgreSQL/pgvector，再调用 `/search`，按查询保存排序后的稳定 `p-xxx` ID：

```json
{
  "results": {
    "RQ-001": ["p-004", "p-118"],
    "RQ-002": ["p-006"]
  }
}
```

先检查查询集：

```powershell
.\.venv\Scripts\python.exe scripts\retrieval_eval.py --validate-only
```

服务和测试用户相册准备好后，可直接采集真实搜索排名：

```powershell
.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --base-url http://127.0.0.1:8000 `
  --uuid-map artifacts\photo-eval\import-map-137.json `
  --split test
```

采集器可直接读取导入脚本生成的 `import-map-137.json`，也兼容
`{"数据库 UUID": "p-004"}` 形式的平面映射。JWT 默认从 `PHOTO_EVAL_JWT` 读取，
不要写进脚本、结果文件或 Git。本地 `127.0.0.1` 默认不继承系统代理；只有确实要走代理时
才传 `--trust-env`。结果除了稳定 ID，还会保留查询解析与各候选分数，供拒识阈值分析。

计算指标：

```powershell
.\.venv\Scripts\python.exe scripts\retrieval_eval.py `
  --results artifacts\retrieval-results.json `
  --split test `
  -k 5
```

指标为 Recall@5、Precision@5、MRR、无结果准确率和禁返图片命中率。注意数据库中
的 UUID 与数据集 `p-xxx` 不是同一个标识；导入测试图片时必须把 `p-xxx` 保存在
独立的 source_id 或导出映射中。

在调用真实服务前，先生成结构审计报告和人工对照表：

```powershell
.\.venv\Scripts\python.exe scripts\audit_retrieval_queries.py `
  --output artifacts\retrieval-query-audit.json

.\.venv\Scripts\python.exe scripts\render_retrieval_query_audit.py `
  --queries tests\eval\retrieval_queries.json `
  --manifest tests\eval\photo_manifest.json `
  --source-root . `
  --output-dir artifacts\retrieval-query-audit-sheets
```

如果 FastAPI 应用壳无法启动，可用
`scripts/collect_retrieval_results_direct.py` 调用同一个 `semantic_search` 处理函数。
该诊断路径保留查询解析、真实 Embedding、pgvector 和混合排序，只绕过 HTTP/JWT；
报告中必须明确标注，修复应用壳后仍需用 HTTP 采集器复验。

原 50 条只有 2 条无结果查询，不足以校准拒识。开发期使用经过人工核对的 30 条负样本：

```powershell
.\.venv\Scripts\python.exe scripts\audit_retrieval_queries.py `
  --queries tests\eval\retrieval_negative_development.json `
  --manifest tests\eval\photo_manifest.json `
  --output artifacts\retrieval-negative-development-audit.json

.\.venv\Scripts\python.exe scripts\collect_retrieval_results.py `
  --queries tests\eval\retrieval_negative_development.json `
  --uuid-map artifacts\photo-eval\import-map-137.json `
  --output artifacts\retrieval-negative-development-http.json
```

阈值只能在 development 上选择。下面的脚本要求正样本接受率至少 95%，并将原
validation/test 作为冻结检查：

```powershell
.\.venv\Scripts\python.exe scripts\calibrate_retrieval_threshold.py `
  --positive-queries tests\eval\retrieval_queries.json `
  --positive-results artifacts\retrieval-real-50-no-auto-parse.json `
  --negative-queries tests\eval\retrieval_negative_development.json `
  --negative-results artifacts\retrieval-negative-development-http.json `
  --output artifacts\retrieval-threshold-calibration.json
```

若报告中的 `single_threshold_suitable` 为 `false`，不要把阈值直接加入生产搜索；
应先增加 OCR/品牌/数值等结构化矛盾校验或 Top-K 判同重排。

2026-08-14 的 50 条真实运行、标签审计、两种解析模式对照和失败归因见
`docs/retrieval-evaluation-results-2026-08-14.md`。

### 结构化矛盾校验

搜索默认启用 `verify_constraints=true`。当查询明确指定图片中的文字、品牌、价格、
座位、锁屏时间、台历日期或路线时，服务会扩大候选池并要求 `ai_analysis` 提供匹配证据。
普通语义查询不受影响。响应的 `constraint_check` 只返回约束和计数，不返回被过滤图片内容。

需要做未校验对照时显式传 `verify_constraints=false`。Agent 搜索也默认开启；强约束全部失败时
不会继续全相册兜底。实现、API 示例、标签修订、真实 HTTP 指标和限制见
`docs/structured-constraint-validation.md`。

### Top-K 判同重排

搜索可通过 `verify_semantic=true/false` 开关 Top-K 判同。它在结构化约束校验和游标
过滤后，对当前页前 5 个候选的冻结 `ai_analysis` 与 `ai_description` 做一次批量判断，
输出 `match / contradiction / uncertain`。高置信度 contradiction 删除，match 排在
uncertain 前；严格模式在零 match 时返回空结果，且不会用未经判定的候选回填。模型、缓存、
熔断或 JSON 解析失败时仍 fail-open，原排名继续返回。

离线人工上界回放：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_search_reranker.py `
  --queries tests\eval\retrieval_rerank_queries.json `
  --output artifacts\reranker-offline-replay.json
```

真实 HTTP 基线与重排对照：

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

Prompt、缓存键、熔断、分页和门禁细节见 `docs/search-reranker.md`；2026-08-15 的真实
结果见 `docs/search-reranker-results-2026-08-15.md`。

## 4. 第三层：Agent 决策

Agent 评测仍使用确定性工具桩隔离数据库和 OSS，只检验真实大模型的意图识别、
工具选择、参数、多步流程、边界和安全。可用本批人工复核相册替换旧模拟相册：

```powershell
.\.venv\Scripts\python.exe scripts\agent_eval.py `
  --mode real `
  --photo-manifest tests\eval\photo_manifest.json `
  --output artifacts\agent-eval-real.json
```

这里的 `--photo-manifest` 不会让 Agent 访问真实数据库；真实检索能力只看第二层。
三层结果必须分别报告，不能把回放分数、工具桩分数或基础设施故障混成模型能力分。

## 5. 生成新图片

生成脚本不再保存密钥。先设置环境变量：

```powershell
$env:IMAGE_API_KEY="新密钥"
$env:IMAGE_API_BASE_URL="https://example.com/v1"
.\.venv\Scripts\python.exe gen_photo\generate_text_photos.py
```

新增或重生成任何图片后，都必须重新查看最终成图、更新
`scripts/build_photo_manifest.py` 的人工标注、重建清单并运行完整性检查。生成 Prompt
只表达期望，不能直接作为 ground truth。
