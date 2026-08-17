# 细粒度视觉检索与二次视觉判定（v4）

## 1. 为什么需要两层方案

文本 Top-K 判同能显著减少误召回，但它只能看到已经写入数据库的描述。旧版 VL 元数据
缺少动作、年龄、模糊类型、拍摄载体和空间方位时，真实正例会被判为 `uncertain`；严格
零匹配门禁随后会把它过滤。另一方面，对每次搜索的所有候选重新看原图会显著增加延迟和
模型费用。

v4 因此使用级联架构：

```text
VL v4 结构化提取 → 细粒度字段展开进 embedding → 向量/混合召回
→ 文本 Top-K 判同 → 仅低置信或细粒度近分候选看原图 → 最终门禁
```

第一层提升召回上限，第二层只解决文本证据不足和近重复歧义。二次视觉判定不是全量
reranker，也不会在召回阶段凭空找回没有进入候选集的照片。

## 2. VL v4 字段

`ImageAnalysis` 在保留旧字段的基础上增加：

- `actions`：画面可见的具体动作；
- `age_groups`：儿童/青年/中年/老年，无法可靠判断时为空；
- `blur_type`：运动模糊、失焦、相机抖动、镜头雾化、隔窗模糊等；
- `capture_context`：公交车内、隔窗拍摄、自拍、俯拍、截图等；
- `spatial_layout`：左/中/右、前景/背景、朝向和相对位置；
- `distinctive_details`：区分近似图片的姿态、服饰、局部物体或光线细节；
- `analysis_version`：当前为 `v4`。

`build_retrieval_text()` 会把上述字段连同描述、摘要、场景、物体、OCR 和人物信息展开成
带标签的自然文本，再调用 `text-embedding-v3`。这一步是必要的：只把字段写进 JSONB 而
不重算 embedding，不会改善向量召回。

## 3. 二次视觉判定触发条件

功能默认关闭，打开后只在以下情况触发：

1. 文本判同没有 `match`，但至少有一个 `uncertain`（含低置信 contradiction）；
2. 查询包含位置、朝向、年龄、动作、模糊或拍摄载体等细粒度词，并且 Top-2 混合分差
   不超过 `SEARCH_VISUAL_VERIFY_SCORE_GAP`。

最多查看 `SEARCH_VISUAL_VERIFY_TOP_K` 张原图。视觉层返回 `match / contradiction /
uncertain`；只有视觉 `match` 或达到拒绝阈值的 `contradiction` 会覆盖文本结论，视觉
`uncertain` 不会抹掉已有文本匹配。

视觉层使用独立 Redis 缓存和 `dashscope_search_visual_verify` 熔断器。签名 OSS URL 只在
发起调用时生成，不进入缓存；缓存键使用 query、photo ID、内容哈希、模型和 Prompt 版本。
视觉调用失败只标记 `rerank_check.visual_degraded=true` 并保留文本层结论，不会让搜索 5xx，
也不会让外层错误处理撤销文本重排。

## 4. 配置

```dotenv
SEARCH_VISUAL_VERIFY_ENABLED=false
SEARCH_VISUAL_VERIFY_TOP_K=3
SEARCH_VISUAL_VERIFY_SCORE_GAP=0.05
SEARCH_VISUAL_VERIFY_TIMEOUT_SECONDS=45
SEARCH_VISUAL_VERIFY_CACHE_TTL_SECONDS=604800
SEARCH_VISUAL_VERIFY_IMAGE_URL_TTL_SECONDS=300
CB_SEARCH_VISUAL_VERIFY_RECOVERY_INTERVAL=180
```

建议保持生产默认关闭，先在 development 和 validation 做 A/B。打开后可从 API 响应的
`rerank_check` 查看触发原因、检查候选数、三类判定计数、缓存命中、降级状态与视觉延迟。

## 5. 安全重算已有评测图片

脚本只允许 `photo-eval-*` 隔离用户，默认 dry-run；必须显式 `--apply` 才访问 OSS、
DashScope 和数据库。单张图片的描述、v4 分析和 1024 维 embedding 全部成功后才原子提交，
失败不会覆盖旧结果。

先查看 development 拟真切片计划：

```powershell
.\.venv\Scripts\python.exe scripts\reprocess_photo_analysis.py `
  --import-map artifacts\photo-eval\import-map-137.json `
  --manifest tests\eval\photo_manifest.json `
  --split development --synthetic-only
```

确认后执行，建议低并发以避免 VL 突发限流：

```powershell
.\.venv\Scripts\python.exe scripts\reprocess_photo_analysis.py `
  --import-map artifacts\photo-eval\import-map-137.json `
  --manifest tests\eval\photo_manifest.json `
  --split development --synthetic-only `
  --concurrency 1 --apply `
  --output artifacts\photo-eval\reprocess-v4-development.json
```

也可以重复传入 `--dataset-id p-144` 精确选择图片。不要对私人用户或未备份数据手工改写
`ai_analysis`；该脚本故意不提供绕过隔离用户检查的选项。

## 6. 评测规则

- 先冻结查询、标签、Prompt 版本和阈值，再运行 HTTP A/B；
- development 可用于定位和修改；validation 只用于选择方案；已经查看过的 test 不再称为
  新的盲测；
- 同时报告 Recall@5、Precision@5、MRR、空结果准确率、禁返命中率、视觉触发率、降级率、
  缓存命中率和 P50/P95 增量延迟；
- 单独报告“召回失败”（正例未进入 Top-K）和“判定失败”（正例进入 Top-K 后被过滤）；
- v4 重算会改变 embedding，必须重新采集 baseline，不能把旧 baseline 与新 reranker 直接
  对比。

当前实现验证：Ruff 通过；完成单张照片质量门禁语义统一后，完整 pytest 为 143 passed。

2026-08-15 的真实 development/validation 重算、HTTP A/B、调用量、延迟和产物哈希见
`docs/visual-retrieval-v4-results-2026-08-15.md`。
