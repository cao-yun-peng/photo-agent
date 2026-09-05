# E-S6-EVAL-CLEANUP-001：旧评测体系清理

- 时间：2026-08-28
- 范围：退役旧 Agent Replay/Real、Agent HTTP E2E 和检索评测资产；保留 VL 评测链路、图片资产及普通产品/Web 测试
- 确认来源：用户明确选择“只清理旧评测体系”

## 删除范围

- `scripts/agent_eval.py`
- `scripts/agent_e2e.py`
- `tests/test_agent_eval.py`
- `tests/test_agent_e2e.py`
- `tests/eval/agent_eval_dataset.json`
- `tests/eval/agent_e2e_dataset.json`
- `tests/eval/retrieval_queries.json`
- `tests/eval/retrieval_rerank_queries.json`
- `tests/eval/retrieval_negative_development.json`
- `agent_eval_result.json`
- `docs/1.md`

## 保留范围

- `scripts/offline_eval.py`
- `scripts/vl_prompt_experiment.py`
- `tests/eval/photo_manifest.json`
- `tests/eval/object_aliases.json`
- `tests/eval/prompts/vl-analysis-v*.txt`
- `test_photos/`、`test_photos_realistic/`
- 普通 Python、Web 单元测试和 Playwright E2E

## 验证记录

- Git 基线：`39fdb587e368730895ecbb3b92ba50ae2722690e`；验证针对包含本次清理和既有未提交改动的当前工作树
- 删除清单：11 个目标均不存在；退出状态 0
- 保留清单：VL 两个脚本、manifest、别名、5 个 Prompt、两组图片目录及普通 Python/Web/Playwright 测试均存在；退出状态 0
- 活跃代码与文档残留引用扫描：无旧脚本或数据集引用；退出状态 0
- `python scripts/offline_eval.py --validate-only`：137 条记录有效，Development 81、Validation 28、Test 28；退出状态 0
- `pytest -q`：8 passed；退出状态 0
- 受影响 Ruff：All checks passed；退出状态 0
- Project-to-Act validator：valid；退出状态 0
- 验证状态：成功

## 结论边界

本证据只证明旧评测体系清理和保留资产可用，不证明重新设计的评测体系已经建立，也不证明产品达到阶段 6 或生产发布 Gate。
