# Photo Agent 真实端到端测试

`scripts/agent_e2e.py` 通过正在运行的 HTTP API 测试 Agent，不替换 Agent Tool，也不直接
调用 Python 内部服务。核心路径包括：

```text
测试用例 → HTTP/JWT → Redis AgentLock → 真实 LLM 决策
        → 生产 Tool → PostgreSQL/pgvector → AgentSession → HTTP/SSE 响应
```

它与 `scripts/agent_eval.py` 的职责不同：`agent_eval.py` 用确定性工具桩评估模型决策，
本测试验证部署后的真实组件能否共同工作。

## 前置条件

1. API、Worker、PostgreSQL 和 Redis 已启动；`GET /ready` 返回 200。
2. Agent 使用有效的真实模型配置，不是 `sk-xxx` Mock 模式。
3. 使用 `photo-eval-*` 隔离测试用户，并已按 `docs/photo-evaluation.md` 导入评测相册。
4. JWT 只放在 `PHOTO_EVAL_JWT` 环境变量，不写入命令或结果文件。

## 运行

PowerShell：

```powershell
$env:PHOTO_EVAL_JWT = "隔离测试用户 JWT"
.\.venv\Scripts\python.exe scripts\agent_e2e.py `
  --confirm-test-account `
  --output artifacts\agent-e2e-result.json
```

只运行少量用例：

```powershell
.\.venv\Scripts\python.exe scripts\agent_e2e.py `
  --confirm-test-account `
  --cases AE2E-002,AE2E-003
```

远端测试环境必须额外传 `--allow-remote`。本地请求默认不继承系统代理；确实需要代理时
才传 `--trust-env`。

## 用例范围

- `AE2E-001`：能力说明，不误调搜索或生成工具；
- `AE2E-002`：普通搜索选择 `result_mode=browse`，返回 1–5 个真实候选；
- `AE2E-003`：最佳单图选择 `result_mode=best`，只返回 1 个候选；
- `AE2E-004`：模糊查询由 Turn Resolver 返回结构化 `clarify`；用户补充后通过同一 `session_id` 继续搜索；
- `AE2E-005`：完成搜索后通过同一会话修改查询条件；
- `AE2E-006`：提示注入和删除请求不触发任何业务副作用；
- `AE2E-007`：SSE 顺序包含 `start/think/final/done`；
- `AE2E-008`：不存在的会话返回 404；
- `AE2E-009`：搜索后创建真实生成任务，默认跳过。
- `AE2E-010`：同一会话中“狗的照片 → 金毛的照片”应替换搜索条件，并持续命中普通搜索快路径。
- `AE2E-011`：“全部自拍”首轮直接交付 `select + complete_result_set`，不经过完整 Agent 循环。

`AE2E-005` 是完成态会话续接的固定回归测试：`/agent/run` 把产生 `final` 的上一轮标记为
`completed`，但只要会话未过期且仍属于当前用户，就应支持“不要猫了，改找狗”等后续
指令。`failed`、`abandoned` 和过期会话仍不可续接。

## 会产生副作用的用例

默认套件只读。`AE2E-009` 会创建真实生成记录、投递 Worker 任务，并可能产生模型费用，
只有显式确认后才运行：

```powershell
.\.venv\Scripts\python.exe scripts\agent_e2e.py `
  --confirm-test-account `
  --include-mutations `
  --cases AE2E-009
```

## 报告与判定

报告记录数据集哈希、预检结果、每轮耗时、HTTP 状态、Agent 事件、工具参数、工具结果和
具体断言失败原因。Token 不会写入报告；`thumb_url`、`result_url` 等签名地址会被清除。

真实模型存在随机性，正式发布前应在冻结代码和数据集后至少运行三次，并报告每个用例的
通过次数、延迟分布和失败明细。Runner 默认不自动重试，避免用重试掩盖不稳定性。
