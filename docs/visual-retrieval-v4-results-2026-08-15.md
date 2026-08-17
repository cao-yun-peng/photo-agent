# 细粒度视觉检索 v4 实验记录（2026-08-15）

## Material Passport

- Experiment ID：`visual-retrieval-v4-20260815`
- Type：VL 细粒度字段、embedding 重算、选择性原图视觉核验、真实 HTTP A/B
- Execution Status：`VERIFIED`（development/validation 共 28/28 请求成功）
- Quality Gate：development `PASS`；validation `PASS`
- Data Access：`photo-eval-manifest-v2` 隔离用户、独立 OSS 前缀、PostgreSQL、Redis
- VL Prompt/Schema：`v4`
- Text Judge：`topk_match_v1` / `qwen-plus`
- Visual Judge：`visual_match_v1` / 项目 `QWEN_VL_MODEL`
- Secret Handling：密钥只从 `.env` 读取；JWT 仅存在于采集进程环境变量
- Test Policy：已查看过的 test 未运行、未用于调参，本记录不把它称为盲测

## 1. 实施内容

第一层在 `ImageAnalysis` 中增加 `actions`、`age_groups`、`blur_type`、
`capture_context`、`spatial_layout`、`distinctive_details` 和 `analysis_version`。新函数
`build_retrieval_text()` 把这些字段展开进 embedding 文本，避免只存 JSON 不改善召回。

第二层在文本 Top-K 判同之后按需查看原图：

- 文本没有 match 但存在 uncertain；或
- 查询包含动作、年龄、模糊、拍摄载体、位置/朝向等细粒度条件，且 Top-2 分差不超过 0.05。

最多看 3 张图。视觉明确 match 或高置信 contradiction 才覆盖文本结论；visual uncertain
保留原文本结论。视觉层使用独立缓存、熔断器、超时和响应诊断，失败只做局部降级。

新增安全重算脚本默认 dry-run，仅允许 `photo-eval-*` 隔离用户。单张照片的描述、v4 分析
和 1024 维 embedding 全成功后才原子提交。

## 2. 重算结果

保持 Prompt、阈值和触发逻辑不变，按 development → validation 顺序执行：

| Split | 选择图片 | 更新成功 | 失败 | parse_quality=ok |
|---|---:|---:|---:|---:|
| development | 15 | 15 | 0 | 15 |
| validation | 5 | 5 | 0 | 5 |

这 20 张都是已导入隔离用户的拟真合成困难图片。基础集 112 张和未运行的 5 张 test 合成图
仍保留旧分析；因此当前数据库是 v3/v4 混合版本，不能把本结果外推成“全库已迁移”。

## 3. 真实 HTTP A/B

embedding 已改变，所以两组 baseline 均在重算后重新采集，不能复用旧 baseline。

### Development（21 条查询）

| 指标 | v4 baseline | 文本 + 按需视觉 | 变化 |
|---|---:|---:|---:|
| Recall@5 | 93.33% | 93.33% | 0.00 pp |
| Precision@5 | 20.00% | 71.67% | +51.67 pp |
| MRR | 0.9000 | 0.9333 | +0.0333 |
| 无结果准确率 | 0.00% | 100.00% | +100.00 pp |
| 禁返图片命中率 | 42.86% | 0.00% | -42.86 pp |
| Gate | FAIL | PASS | — |

旧 v3 development baseline Recall@5 为 86.67%、MRR 为 0.8000；v4 baseline 达到 93.33%
和 0.9000，说明细粒度字段进入 embedding 后提升了上游召回。启用判同后 Recall 不再下降。

### Validation（7 条查询）

| 指标 | v4 baseline | 文本 + 按需视觉 | 变化 |
|---|---:|---:|---:|
| Recall@5 | 100.00% | 100.00% | 0.00 pp |
| Precision@5 | 20.00% | 80.00% | +60.00 pp |
| MRR | 1.0000 | 1.0000 | 0.0000 |
| 无结果准确率 | 0.00% | 100.00% | +100.00 pp |
| 禁返图片命中率 | 57.14% | 0.00% | -57.14 pp |
| Gate | FAIL | PASS | — |

旧 v3 validation rerank Recall@5 为 60%；冻结参数下的新方案达到 100%，没有继续依据
validation 修改 Prompt 或阈值。

## 4. 调用与延迟

development + validation 合计：

- 28 条查询，27 次实际文本判同（1 条在结构化约束层后无候选）；
- 二次视觉实际触发 7/28（25%），共查看 9 张图，即每条查询平均 0.32 张；
- 视觉判定汇总：4 match、4 contradiction、1 uncertain；
- 文本冷调用 P50/P95：7.078 s / 9.688 s；
- 视觉冷调用 P50/P95：1.812 s / 3.141 s；
- 文本缓存命中 0、视觉缓存命中 0；
- 文本和视觉均 0 降级、0 HTTP 运行错误。

这些是单机冷调用延迟，不是生产并发压测。缓存命中后的延迟、真实流量触发率、费用和限流
仍需单独评估。

## 5. 产物哈希

| 产物 | SHA256 |
|---|---|
| `artifacts/photo-eval/reprocess-v4-development.json` | `e4f4078f3c6738a626b4121ce72c6b2afbb45a370d9b99796689866dd8bed9a1` |
| `artifacts/photo-eval/reprocess-v4-validation.json` | `cd7f2d9003a2baf735af5e865c0f9952d458fd801b237b8fb7d2004c4fdd59ec` |
| `artifacts/reranker-http-baseline-v4-development.json` | `2a90b522abe0bef12ffd8ca720455c6c49be10348a64730428bbb900e0010119` |
| `artifacts/reranker-http-enabled-v4-development.json` | `531755cbaec9fad7ccbe78e605c18bc50936b516f9935b00b05ddbac71c784ee` |
| `artifacts/reranker-http-baseline-v4-validation.json` | `7d96b60472c0a80dd9c4f735a8d49ca61f1f7b08bf50e4bde2f67674c21a9fc6` |
| `artifacts/reranker-http-enabled-v4-validation.json` | `c76062aa5ca6a489fab422158db1b0efd17870dcaa76dc8dde03fec363ee394b` |
| `artifacts/reranker-http-baseline-v4-development-metrics.json` | `8a90fc44c97bc54e0fcd948d94a0b10eda8ed8ab4c329ecd98d2f3d8f891ff04` |
| `artifacts/reranker-http-enabled-v4-development-metrics.json` | `f50893dbd3184c255728146c83c8764dd61f5f092596530ad9aa4f75622dee0f` |
| `artifacts/reranker-http-baseline-v4-validation-metrics.json` | `35592b8ad098075f629fb0dccc0d417c871f3689ac36c8ed5e835bc89c9ac6c8` |
| `artifacts/reranker-http-enabled-v4-validation-metrics.json` | `621ba22a68777d8c786647f9a811FDA79a8cbc9944e62d0b249f8e54df1a9e1e` |

## 6. 验证与剩余工作

- 变更文件 Ruff：PASS；
- 新增/相关定向测试：12/12 PASS；
- 完成单张照片质量门禁语义统一后，完整 pytest：143 PASS；
- API 健康页：PostgreSQL、Redis 正常，文本与视觉熔断器均 closed；
- worker 已加载 v4 并连接 Redis；
- `.env` 未修改，视觉功能配置默认仍为关闭；本次真实 A/B 的 API 进程通过临时环境变量开启。

尚未完成：全库 v3→v4 迁移、缓存命中回放、并发/费用评估、真实用户图片验证，以及新的
未见盲测集。现有 test 已经查看，不应继续用它证明泛化。
