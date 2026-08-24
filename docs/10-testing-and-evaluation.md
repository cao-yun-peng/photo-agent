# 测试与评测

## 1. 当前资产

当前工作区保留：

- Python：`tests/test_agent_eval.py`、`tests/test_agent_e2e.py`。
- Web 单元测试：API client、SSE parser、media URL、generation、session、format、file policy、
  search page。
- Web E2E：主路径、响应式、可访问性。
- 评测数据：`tests/eval/agent_eval_dataset.json`、`agent_e2e_dataset.json`、
  `photo_manifest.json`、检索查询集和 VL Prompt。
- 脚本：`agent_eval.py`、`agent_e2e.py`、`offline_eval.py`、`vl_prompt_experiment.py`。

大量历史专项测试和实验脚本已从工作区删除，因此不能把过去的覆盖率或实验报告当作当前
可复现结论。

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

## 3. Agent 离线评测

### Replay

```bash
python scripts/agent_eval.py --mode replay
```

Replay 按数据集中的标准动作回放，验证数据集、工具 stub 和评分器本身。它不调用真实模型，
不能衡量模型的工具选择能力。

### Real

```bash
python scripts/agent_eval.py --mode real --output artifacts/agent-eval-real.json
python scripts/agent_eval.py --mode real --dimensions D1,D2,D8 --priority P0
```

Real 让真实模型自主选择工具，衡量 Agent 决策、参数和最终答复。它仍使用受控工具环境，
不等同于真实 HTTP/数据库/Redis 全链路结果。

评分维度包括工具选择、顺序、禁止工具、参数、最终状态、内容、安全和预算。每条用例的
`pass_threshold` 是最终通过线；rubric 文字主要用于人工理解，结构化 `expected` 才由程序判分。

## 4. Agent 真实 E2E

```bash
python scripts/agent_e2e.py --confirm-test-account
```

该脚本通过真实 HTTP、JWT、PostgreSQL、Redis、LLM、Tool 和 SSE 运行隔离测试账号。运行前：

- 确认 `.env` 使用明确的测试用户和预算。
- 不对生产用户或生产相册执行。
- 使用 `--cases` 限定用例时记录选择原因。
- 将 `--output` 指向不提交敏感内容的 artifacts 目录。

## 5. VL 评测

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

## 6. Web E2E

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

## 7. 质量门禁建议

发布至少要求：

- Python/TypeScript 静态检查和现存单测通过。
- Alembic upgrade 在空库和最近备份副本上通过。
- 登录、上传、处理轮询、搜索、Agent SSE、生成确认主路径通过。
- 搜索改动报告 Recall、Precision、MRR、零结果准确率、延迟和外部模型调用比例。
- Agent 改动同时通过 replay、real 的目标维度和真实 E2E 子集。
- 任何真实模型结果标注模型、Prompt 版本、数据 split、时间、配置和产物 hash。

## 8. 结果解释边界

- Mock 通过只证明协议闭环，不证明模型质量。
- Replay 通过只证明评测器和预期动作自洽。
- 小数据集上的 100% 不能外推到生产泛化。
- 与当前代码不匹配、没有原始产物或 hash 的历史指标只可作为背景，不应作为发布证据。
