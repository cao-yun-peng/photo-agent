# Photo Agent 图片数据集与三层评测

## 1. 数据集状态

`tests/eval/photo_manifest.json` 是当前唯一可用于自动评分的图片标准答案。它由
`test_photos` 中实际存在的 112 张最终成图逐张复核得到，而不是从生成 Prompt
直接复制标签。

人工标注策略：

- 场景允许多个合理答案，例如 `餐厅/室内`；
- 物体只标必须识别的核心对象，额外合理对象不算误检；
- 小规模清晰人物使用精确人数，大群像和遮挡场景使用人数区间；
- OCR 分为必识别文字和可选文字，可选文字漏识别不扣分；
- 所有路径均为仓库相对路径，不再使用生成机器的绝对路径。

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

只验证 112 张图片的路径、哈希、尺寸和 MIME，不访问外部服务：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py --dry-run
```

导入并将 pending 照片送入现有 Worker：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py
```

如果 Worker 暂时未启动，可先只上传和写库，之后再次执行不带
`--no-enqueue` 的命令即可复用照片并补入队：

```powershell
.\.venv\Scripts\python.exe scripts\import_photo_eval_dataset.py --no-enqueue
```

导入映射保存在 `artifacts/photo-eval/import-map.json`，记录每个 `p-xxx` 对应的
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
相似干扰项和无结果查询。先将 112 张图片通过真实上传和处理链路写入 OSS、
PostgreSQL/pgvector，再调用 `/api/search`，按查询保存排序后的稳定 `p-xxx` ID：

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
  --token $env:PHOTO_EVAL_JWT `
  --uuid-map artifacts\photo-id-map.json `
  --split test
```

`photo-id-map.json` 的格式为 `{"数据库 UUID": "p-004"}`。JWT 只通过环境变量传入，
不要写进脚本、结果文件或 Git。

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
