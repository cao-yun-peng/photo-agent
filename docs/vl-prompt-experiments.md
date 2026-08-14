# Qwen-VL Prompt 实验记录

## 实验协议

- 数据集固定为 `tests/eval/photo_manifest.json`，每次记录文件 SHA256。
- `development` 用于分析 bad case 和调整 Prompt；`validation` 用于选择版本；
  `test` 在 Prompt 冻结后只运行一次。
- 每个版本的 Prompt 单独保存在 `tests/eval/prompts/`，不得覆盖旧版本。
- 单次实验固定模型、Prompt、split 和并发；失败不静默重试。
- 原始响应、解析结果、逐样本评分、耗时和用量保存到
  `artifacts/vl-experiments/`。该目录是运行产物，不提交 Git。
- 正式指标包括场景准确率、人数区间准确率、核心物体 Macro Recall、
  OCR Macro Recall 和 JSON 解析成功率。

## 执行方法

先用少量开发集做连通性预检：

```powershell
.\.venv\Scripts\python.exe scripts\vl_prompt_experiment.py `
  --experiment-id v1-pilot-development `
  --prompt-file tests\eval\prompts\vl-analysis-v1.txt `
  --split development --limit 5 `
  --output artifacts\vl-experiments\v1-pilot-development.json
```

再运行完整开发集基线：

```powershell
.\.venv\Scripts\python.exe scripts\vl_prompt_experiment.py `
  --experiment-id v1-baseline-development `
  --prompt-file tests\eval\prompts\vl-analysis-v1.txt `
  --split development `
  --output artifacts\vl-experiments\v1-baseline-development.json
```

## 结果日志

每轮完成后记录：实验 ID、日期、模型、split/样本数、Prompt 哈希、五项指标、
主要 bad case、唯一改动、结论和下一步。不得只记录“分数提高”，必须保留失败项。

### v1-baseline-development（2026-08-14）

- 模型：`qwen-vl-plus`；样本：development 66；并发：2；失败重试：无。
- Prompt SHA256：`7e4fb27e0305f484f192356cf68457313aaf9d9187f36c0ca1c54caecaba0783`。
- 原始数据集 SHA256：`fd316ed99ad9f78e580e798653e37af12a1a60a7828161df46b87bc24aba1cbf`。
- 原始指标：场景 65.15%，人数 80.30%，物体 Macro Recall 60.61%，
  OCR Macro Recall 97.22%（18 张），JSON 解析 100%，门禁 FAIL。
- 系统表现：66/66 请求成功；总 Token 100,569，其中 image tokens 69,500。
- 审计结论：23 个场景失败中有 21 个是室内/户外父子层级冲突；13 个人数
  失败中，人工看图确认 7 张标注错误，另有屏幕人物和模糊背景人物口径不明确。
  因此原始分数不能全部归因于 Prompt。
- 保留结果：`artifacts/vl-experiments/v1-baseline-development.json`。

### 标注与评分审计（2026-08-14，无模型调用）

- 明确人数口径：只统计主体场景中清晰可辨的人；排除屏幕/海报/照片、
  孤立肢体和很小的模糊背景人物。
- 修正开发集人数：`p-038=8..20`、`p-060=1`、`p-068=0`、`p-078=3`、
  `p-086=6`、`p-092=13`、`p-093=4`。
- 修正开发集场景：`p-060=车内/街道`、`p-108=户外/门廊`、
  `p-134=户外/街道`。
- 评分器新增场景家族匹配，同时保留精确场景准确率；人物数量大于 0 时派生
  “人物”对象；仅补充语义明确的开放词表同义词。

### v1-rescored-development（2026-08-14，无模型调用）

- 使用 v1 原始预测在修正后的数据集与评分器上离线复算。
- 场景家族 100.00%，场景精确 69.70%，人数 89.39%，物体 Macro Recall
  75.00%，OCR 97.22%，解析 100%，门禁 PASS。
- 结论：原始 FAIL 的主要原因是标签错误、场景层级与开放词表评分缺陷；这部分
  提升不得归因于 Prompt。

### v2-development（2026-08-14，拒绝上线）

- Prompt SHA256：`e292c0e880f0eac259d4e1851c81cda498136ee204d0558ca67d5ca9a72baf16`。
- 数据集 SHA256：`789a6bead0db3ccc92b060df4a71283eab53ef7350ff33b92318992b61b89ded`。
- 场景家族 71.21%，场景精确 16.67%，人数 87.88%，物体 Macro Recall
  76.26%，OCR 100%，解析 100%，门禁 FAIL。
- 性能：66/66 请求成功；墙钟 118.0 秒；API 延迟 P50 2.91 秒；总 Token
  111,394。
- 唯一改动：要求更具体的场景、对象、人数排除规则和完整 OCR。
- 失败原因：模型把 `scene` 自由扩写为“面包店、大学宿舍、森林、足球场”等，
  破坏受控分类字段；对象与 OCR 的小幅提升不足以抵消场景退化。
- 决策：拒绝 v2。v3 恢复受控场景枚举，自由场景描述仅写入 `scene_detail`。
