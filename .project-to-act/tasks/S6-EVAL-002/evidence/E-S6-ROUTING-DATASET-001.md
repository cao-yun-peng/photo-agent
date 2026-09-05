# E-S6-ROUTING-DATASET-001：路由种子基线数据集

- 时间：2026-08-28T21:47:36+08:00
- 任务：S6-EVAL-002
- Git 基线：`39fdb587e368730895ecbb3b92ba50ae2722690e`；工作树包含尚未提交的评测重建变更
- 对象：L1 前置语义路由种子数据集、Schema、元数据和纯数据校验

## 产物与哈希

| 产物 | SHA-256 |
|---|---|
| `tests/eval/routing/turn_routing_v1.jsonl` | `EF40F8C7052DD14FC524F644F5301F53802F0AE7A90914907CA2CF6F856AF8E6` |
| `tests/eval/routing/turn_routing_v1.schema.json` | `7328874E5119F3466239347C5D7C3FAB4479018A784FA58E4E1CC1B818757EF1` |
| `tests/eval/routing/turn_routing_v1.meta.json` | `58B21A80D144CF555FE490EAB7DA5B96FCB3A504C4DF8AEF1A110F4E244931FF` |
| `tests/test_routing_eval_dataset.py` | `B69EAE2A14BD0A8C866485AECB35644B7A6AB52D4C7E9BF43A3A4C6CC3E855AE` |

## 数据摘要

- 共 80 条合成、脱敏案例：Development 48、Validation 20、Test 12。
- 覆盖全部 5 个 Intent 和全部 5 个 Relation。
- 明确拆分 `rule_outcome=plan` 与 `rule_outcome=defer`，后者至少 12 条。
- 覆盖新搜索、替换、细化、续搜、结果反馈、选择模式、模糊输入、丢失引用、日期、地点、
  画面日期、提示注入和安全关键请求。
- 只使用合成文本和 `controlled-photo-*` 标识，不包含用户照片、真实会话或未脱敏 Trace。

## 验证

| 命令 | 退出状态 | 结果 |
|---|---:|---|
| `.venv\\Scripts\\python.exe -m pytest tests/test_routing_eval_dataset.py -q` | 0 | `2 passed in 0.04s`；JSONL、计数、唯一 ID、枚举、切分和覆盖约束通过 |
| `.venv\\Scripts\\python.exe -m pytest tests/test_routing_eval_dataset.py tests/test_turn_resolver.py -q` | 0 | 最终复验 `8 passed in 1.67s`；数据校验与现有路由规则回归均通过 |
| `.venv\\Scripts\\ruff.exe check tests/test_routing_eval_dataset.py` | 0 | `All checks passed!` |
| `git diff --check` | 0 | 无空白错误；仅有既有 Windows LF/CRLF 提示 |
| 两套 Project-to-Act / Lifecycle 验证器 | 0 | Managed 账本有效；Lifecycle revision 4、阶段 6 `in_progress` |

## 结论与限制

结论：路由种子基线的数据结构和最低覆盖有效，可以进入独立标签复核和评测器实现。

本证据不证明路由规则或真实模型质量。数据仅完成单次标注，尚未进行第二人盲审、真实模型
重复运行、指标聚合或数值 Gate 校准；Test 也未被用作发布结论。阶段 6 和生产发布仍不通过。

有效期：数据、Schema、元数据或校验测试任一内容变化前。
