# 测试与评测

## 1. 当前资产

当前工作区保留：

- Python 产品回归测试：结果反馈、工作流状态转换和上下文续接。
- Web 单元测试：API client、SSE parser、media URL、generation、session、format、file policy、
  search page。
- Web E2E：主路径、响应式、可访问性。
- VL 评测数据：`tests/eval/photo_manifest.json`、`object_aliases.json` 和
  `tests/eval/prompts/vl-analysis-v*.txt`。
- VL 脚本：`scripts/offline_eval.py`、`scripts/vl_prompt_experiment.py`。
- 图片资产：`test_photos/`、`test_photos_realistic/`。

旧 Agent Replay/Real、Agent HTTP E2E 和检索评测脚本、数据集与评分器已于
2026-08-28 退役，等待重新设计。历史报告只保留在治理记录中作为审计背景，不能作为当前
Gate 或可复现结论。

## 2. 基础验证

```bash
ruff check app tests scripts
pytest -q

cd web
npm run lint
npm run typecheck
npm run test
npm run build
```

`npm run check` 等价于 lint + test + build；`check:ci` 还包含 Playwright E2E。

## 3. VL 评测

只验证数据：

```bash
python scripts/offline_eval.py --validate-only
```

用已有预测避免模型调用：

```bash
python scripts/offline_eval.py --predictions path/to/predictions.json --split validation
```

Prompt 实验必须提供 experiment ID、Prompt、split 和输出路径。Development 用于调参，
Validation 用于模型/Prompt 选择，Test 应保持盲测；看过 Test 结果后不能再称其为盲测。

## 4. Web E2E

GitHub Actions 会：

1. 执行 lint、typecheck、unit test 和 build。
2. 启动 Mock API/Worker/Web/Nginx Compose 栈并迁移数据库。
3. 等待 `/api/ready`。
4. 用 Chromium 执行 Playwright。
5. 失败时上传报告并输出后端日志。

本地复现：

```bash
docker compose -f docker-compose.yml -f docker-compose.web.yml -f docker-compose.e2e.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.web.yml -f docker-compose.e2e.yml exec api alembic upgrade head
cd web && npm run test:e2e
```

## 5. 待重新设计的评测范围

分层对象、数据契约、评分边界和实施顺序见
[`11-evaluation-plan-v2.md`](11-evaluation-plan-v2.md)。该文件当前是设计草案，不代表任何
新评测 Gate 已通过。

以下能力目前没有可执行的专用评测器，不能声称已通过质量 Gate：

- 图片处理、Embedding 和索引写入的完整流水线。
- 检索召回、硬约束、重排、零结果、延迟、成本和外部调用比例。
- Agent 意图、工具、参数、多轮状态、安全和预算。
- 真实 HTTP/JWT/PostgreSQL/Redis/SSE 端到端链路。
- 图像生成确认、权限、额度、幂等、质量和安全。

新评测体系建立前，Python/Web 自动化测试只作为工程回归，不替代模型或产品质量评测。
任何未来真实模型结果必须记录模型、Prompt 版本、数据 split、时间、配置和产物 hash。

### 路由种子基线

前置语义判断的首版种子集位于 `tests/eval/routing/turn_routing_v1.jsonl`，对应元数据、Schema
和说明位于同目录。当前共 80 条合成脱敏案例，切分为 Development 48、Validation 20、
Test 12；只完成单次标注和结构/覆盖校验，尚未独立复核或运行真实模型，因此不是质量 Gate。

结果反馈与候选集体验改动还应覆盖以下回归：

- “第 2 张不需要”只排除第 2 张。
- 多张结果下的“有我不需要的”必须澄清序号，不得猜测。
- “不要这张，再换一张”排除当前照片后续搜，不能再次返回已展示或已拒绝照片。
- “不要猫了，找狗的照片”是替换搜索，不是对某张猫照片的反馈。
- 客户端在 `new` / `replace` 路由到达时清空旧结果，在 `refine` / `continue` 时保留必要上下文。

## 6. 结果解释边界

- Mock 通过只证明协议闭环，不证明模型质量。
- 普通单元测试通过只证明被覆盖的确定性代码行为。
- 小数据集上的 100% 不能外推到生产泛化。
- 与当前代码不匹配、没有原始产物或 hash 的历史指标只可作为背景，不应作为发布证据。
