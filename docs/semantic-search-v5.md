# v5 检索语义增强与存量重索引

## 能力边界

v5 为每张照片增加三项稳定字段：

- `photo_type`：`selfie / screenshot / group_photo / portrait / document / food / scenery / other`；
- `is_selfie`：是否为自拍；
- `people_count`：画面中可清楚辨认的真实人物数量。

“全部自拍、全部截图、全部合照”会使用数据库硬过滤扫描，不再把向量 Top-K 当作
完整集合。合照还会约束 `people_count >= 2`。普通语义查询仍使用 embedding 排序。

## 上线顺序

1. 升级数据库：

   ```bash
   docker compose exec api alembic upgrade head
   ```

2. 重启 API 和 Worker。新上传照片会直接生成 v5 字段。
3. 先预览存量规模（不调用模型、不写数据）：

   ```bash
   docker compose exec api python scripts/migrate_structured_analysis.py
   ```

4. 确认 DashScope 调用量和费用后，小批量执行：

   ```bash
   docker compose exec api python scripts/migrate_structured_analysis.py \
     --apply --batch-size 10 --interval 12 --max-batches 5
   ```

5. 观察覆盖率后逐步扩大批次；单用户修复可追加 `--user-id <uuid>`。

迁移会重新调用 VL，并用 v5 检索文本重建 embedding。单张失败不会覆盖其旧结果。
数据库迁移本身只从 v4 JSON 做兼容回填，不会把这些近似结果计入 v5 完整覆盖率。

## 覆盖率与用户提示

搜索响应同时返回：

- `coverage_ratio`：embedding 覆盖率；
- `facet_coverage_ratio`：解析成功的 v5 集合字段覆盖率；
- `semantic_complete`：语义集合索引是否覆盖整个相册；
- `coverage_hint`：需要展示给用户的遗漏风险说明。

只有 embedding 与 v5 语义覆盖率都为 100%，系统才把结构化完整集合标记为
`result_set_complete=true`。否则仍返回当前可检索结果，但明确提示可能有遗漏。

## 相似度阈值

请求可传 `min_semantic_score`，服务端默认值由 `SEARCH_SEMANTIC_MIN_SCORE` 控制。
当前默认 `0`（关闭），因为现有离线评测显示单一全局阈值会显著误杀相关照片。
启用阈值后响应包含阈值和被过滤数量。自拍、截图、合照等结构化集合搜索自动绕过
阈值，确保属于集合的低相似照片不会被删掉。

## 验收查询

- `把全部自拍给我，我自己选`
- `把所有手机截图给我`
- `把所有合照给我`
- `找不是自拍的单人照`

前三项在覆盖完整时必须返回 `result_set_complete=true`；覆盖未完成时必须出现
`coverage_hint`，不能声称“一张不漏”。
