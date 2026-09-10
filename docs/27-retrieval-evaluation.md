# 照片检索评测：执行协议与证据

**全流程已完成，1,028 条首轮查询结果的报告检查与最终独立证据审计均通过。** 开发集四组各 217 条、独立验证 A/D 各 80 条均实际进入服务执行，包含超时、连接失败和熔断拦截，未删除或重跑失败结果。检索减少了误匹配，但召回与交互可靠性存在明显代价，不能据此宣称全面提升。累计保守估算费用 **16.947760 元**，低于已授权的 30 元上限，尚未核对供应商账单。

## 数据与标签

| 数据 | 图片 | 查询 | 有正例查询 | 零结果查询 | 用途 |
|---|---:|---:|---:|---:|---|
| 复核开发集 | 137 | 217 | 207 | 10 | 四组消融与方案选择 |
| 独立验证集 | 40 | 80 | 68 | 12 | 固定方案的一次性验证 |

原始开发集保存在 `tests/eval/retrieval`，没有覆盖。复核版在 `tests/eval/retrieval_v2`：查看全部图片和查询，在没有观察检索输出的条件下修订了 9 条查询措辞，保留原有正例集合，并保存逐图观察和逐查询复核记录。例如：把棕褐色旧全家福要求为“黑白照”、把无法确认的架子写为“窗台”、把普通黑色笔写成“钢笔”，都会引入标签噪声。

独立验证集在 `tests/eval/retrieval_validation`，来自有来源、作者与许可记录的 Wikimedia Commons 真实照片。采样方案在查看开发结果前确定，包含动物、物品、动作、自然场景、OCR、排除条件和零结果。40 张图与开发集逐对检查了 5,480 个跨集感知哈希距离，未发现既定阈值内的近似或精确重复；具体阈值、结果和局限见 `leakage-audit.json`。图片族和查询族均保留标识。验证图使用来源记录中实际下载的版本（多为 1280 像素缩图）；本报告的原图核验指该评测图像文件的字节，不等同于 Commons 的最高分辨率原件。

两套标签都是 Codex 视觉标注/复核，**没有独立人工双标**。开发图片全部为合成图；验证集是分层便利抽样，不能代表真实个人相册的总体分布，也不能证明图片未进入基础模型训练。

## 对照条件

四组调用同一份应用 `SearchService` 源码、相同数据库快照、相同查询和固定排序权重。

| 组别 | 强约束校验 | 严格文本判同与继续补候选 | 按需原图核验 |
|---|---|---|---|
| A | 关闭 | 关闭 | 关闭 |
| B | 开启 | 关闭 | 关闭 |
| C | 开启 | 开启 | 关闭 |
| D | 开启 | 开启 | 开启 |

A 保留现有代码共同使用的照片类型、自拍及人数推断，不应称为完全不带条件的纯向量搜索。所有组关闭隐式查询改写，返回第一页最多 5 张；用户画像为空，评分时钟固定。

保留当前产品预算：整次检索 45 秒、每次文本判断 12 秒、最多核验 60 个候选、20 次模型调用、3 次原图核验及 60 个内部预算单位。触及预算后的空结果不能当作正确拒绝。不同组使用独立缓存命名空间，按相同随机种子排列查询，组内并发为 2；实际缓存命中会单独报告。

被测链路使用真实 PostgreSQL、Redis 与 DashScope HTTP。原图通过原始字节的 base64 输入传给 VL；预览链接使用本地占位符。这是服务层检索实验，**不构成真实 HTTP 接口、微信客户端或 Agent 多轮对话的 E2E 证明**。

## 指标与选择规则

有正例查询报告 Recall@1/5、Hit@1/5、MRR@5、nDCG@5 和固定分母 Precision@5；后者按正例数量分层。207 条有正例开发查询中，正例数为 1/2/3/4/5 的查询分别有 188/12/4/2/1 条。因此即使每条都返回全部正确照片，宏 Precision@5 的理论上限也只有 `(188 + 12×2 + 4×3 + 2×4 + 1×5) / (207×5) = 22.8986%`。零结果查询不进入这个指标的分母。

本次 A/B/C/D 的固定 Precision@5 实测为 **22.1256% / 21.7391% / 20.3865% / 21.2560%**；它没有体现“20% 提升至 80%”。此前仅保留记忆数字、缺少原始报告的历史指标不能与本次数据和口径混用。这里同时报告召回、难负例误收与运行完整性，避免把“返回少于五张但已找到目标”简单解释为低准确率。

零结果只在运行成功且核验没有超时、不可用或未完成时计为正确空答案。重复照片占据原排名但不重复获得相关性分数。另报硬负例命中率、运行错误、停止原因、结果数、延迟、模型调用、Token、原图核验比例与估算费用。未知 Token 用量明确标记，不能当成真实零用量。

开发集的选择损失为每条查询等权平均：有正例时取 `1 - Recall@5`；无正例时取 `1 - 正确空答案`；再加 `0.5 × Top-5 是否命中标注硬负例`。进入最低损失 0.02 范围的方案先比较实测延迟中位数，再比较估算费用。查询家族进行配对 bootstrap，而不是把改写查询当独立样本。

只有开发四组全部查询都实际执行、结果身份及冻结校验通过，汇总器才会选择配置。独立验证仅运行固定 A 与开发集选出的配置，不根据验证结果调参。如果选中 A，则验证只需这一种既定配置，不伪造第二个不同方案。

## 开发集真实结果

以下数据来自 [开发汇总](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/report/development-summary.json)，每组包含全部 217 条首次查询；Recall@5 是 207 条有正例查询的宏平均。难负例命中按**全部 217 条查询**计分，不采用有标注难负例的 170 条作为分母；该指标也不等于所有非正例返回的比例。

| 组别 | Recall@5 | 难负例命中@5 | 正确空答案 | 核验未完成/运行异常 | 延迟中位 / P95（秒） | 原图核验触发 | 检索估算费用（元） |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 97.57% | 116/217，53.46% | 0/10 | 0/217 | 1.328 / 1.959 | 0/217 | 0.001530 |
| B | 95.64% | 108/217，49.77% | 2/10 | 0/217 | 1.422 / 1.721 | 0/217 | 0.001530 |
| C | 89.11% | 6/217，2.76% | 3/10 | 192/217 | 45.031 / 45.362 | 0/217 | 6.993385 |
| D | 93.14% | 13/217，5.99% | 3/10 | 188/217 | 45.047 / 45.597 | 177/217，81.57% | 7.468885 |

“核验未完成/运行异常”采用冻结评分器的 operational error 口径，包含总时限、核验不完整和核验不可用；不等同于捕获了同等数量的网络异常。C 的 192 条由 184 个 `deadline_exceeded`、5 个 `verification_incomplete`、3 个 `verification_unavailable` 构成；D 的 188 条对应 185、1、2 个。部分超时查询已经返回正例，质量指标按实际返回内容记分，完整性问题仍单独统计，不能把这些查询描述为顺利完成的交互。

四组共计 **14.465330 元**检索估算费用，仅含开发查询调用，不含预检、历史开发图片解析与后续验证建索引。C/D 分别有 186/185 次调用缺少已确认用量，费用保留其预留额；不是将取消或未知调用当作免费。全部组的本地缓存命中观测为 0，延迟包含真实服务和调用开销、排除建索引，不证明供应商内部缓存状态。D 共发起 356 次原图调用，“触发率”按至少发起一次的查询数计算。

## D 的选择依据与当前发现

[固定选择记录](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/report/selection.json)中，A/B/C/D 的平均损失分别为 **0.336559 / 0.327343 / 0.149923 / 0.127650**。D 最低，C 与 D 相差 0.022273，超过预先指定的 0.02 容差，因此只有 D 进入最终候选，不需要用延迟或费用打破平局。D 已按此固定选择完成独立验证；这项选择没有为正例查询额外加入超时惩罚，不能作为产品交互已达标的结论。D 的 188/217 次不完整、约 45 秒中位延迟和 81.57% 原图触发均是本次实测限制。

按 117 个查询家族进行 2,000 次配对 bootstrap，D 相对 A 的损失差为 **−0.2089，95% 区间 [−0.2634, −0.1462]**；难负例命中率减少 **47.47 个百分点，区间 [38.32, 56.27]**；Recall@5 同时下降 **4.43 个百分点，区间 [1.71, 7.73]**。这些区间来自参与选型的开发数据；独立验证结果见下文，不能据此推断私人相册总体效果。完整方向与指标见 [配对区间](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/report/paired-bootstrap.json)。

已有 A/B/C 原始轨迹复核确认了三类问题，详细根因和原图证据分别见 [基线与强约束复核](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/analysis/baseline-review.md)及 [文本核验复核](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/analysis/text-verification-review.md)：

- **共同召回过滤与旧索引冲突**：自拍描述与 `is_selfie=false` 列值不一致会在 SQL 层排除正例；“不要天气应用截图”中的“截图”还会被共同推断逻辑当作正向类型条件。后续核验无法判断根本没有进入候选池的照片。B 的部分新增漏图另来自结构化约束对复合物体描述的匹配错误。
- **严格文本接收依赖索引证据完整性**：例如婚礼花拱门在原图可见，但旧描述只写花丛，C 将候选判为 uncertain 后不予返回。相对 B，C 新丢失的 18 个查询—正例对中，15 个已判 uncertain、2 个 contradiction、1 个首次核验不可用；不能把它们都归为网络超时。
- **持续补满五张造成额外等待**：`search_engine.py` 在返回不足五张且候选尚未耗尽时继续分批核验。真实汉堡案例第一批已命中唯一正例，随后继续拒绝其他候选，最后因 45 秒总时限取消后续调用。此机制有代码和逐次调用记录支持；当前版本未根据这些结果修改停止条件。

D 的[完整配对复核](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/analysis/visual-verification-review.md)确认：新增救回 11 个查询—正例对，其中 10 个有同次运行的 `text uncertain → visual match → 最终返回` 证据，另 1 个来自文本调用恢复；新增丢失 2 对没有视觉误拒证据。同时新增 7 条难负例误收，均由视觉把 uncertain 改成 match。已完成的 49 对视觉新增接收中，仅 13 对符合冻结正例标签。视觉补证有实际收益，也有误接风险。

补充的 D−C 家族配对 bootstrap 中，Recall@5 增加 4.03 个百分点，95% 区间 [1.03, 7.12]；难负例命中增加 3.23 个百分点，区间 [0.99, 5.91]；综合 loss 差为 −0.02227，区间 [−0.05365, +0.00995] 跨零。因此 D 依照预注册点估计入选，但不能声称其相对 C 的综合优势已获得明确统计支持。补充区间未用于修改选择规则，见 `evidence/analysis/DC-paired-bootstrap.json`。

## 独立验证首轮结果

40 张新图片经本次真实模型解析全部达到 done、可检索状态；120 次建索引调用均有已确认用量，未自动重试。索引与 D 选择绑定后冻结，随后仅运行 A/D。结果见[验证汇总](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/report/validation-summary.json)和[验证错例复核](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/analysis/validation-review.md)。

| 组别 | Recall@5（68 条有正例） | 难负例命中@5（全部 80 条） | 正确空答案 | 核验未完成/运行异常 | 中位 / P95（秒） | 原图触发 | 检索估算费用（元） |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 97.55% | 70/80，87.50% | 0/12 | 0/80 | 1.953 / 2.549 | 0/80 | 0.000558 |
| D | 68.38% | 1/80，1.25% | 2/12 | 73/80 | 45.047 / 45.928 | 41/80，51.25% | 2.319422 |

A 的 80 条均返回 5 张。三条未召全中，两条涉及睡猫排序落在第 6/7；val-049 有 6 个正例，Top-5 已全为正例，少一张是 K=5 的容量上限。这使该验证集宏 Recall@5 的理论上限为 99.7549%，不是 100%。新索引 40/40 为 v5，但格式完整不保证描述和语义判断完全正确。

D 的 73 条不完整包括：54 次总时限、5 次核验不可用、3 次 ConnectError、1 次 ConnectTimeout、10 次 ServiceDegradedError。最后三次 embedding ConnectError 后，十条查询未发模型请求即快速降级，与连续失败阈值为 3 的[熔断实现](E:/project/agent/photo-agent/app/services/circuit_breaker.py)路径一致；此前已进入调用的请求仍可能随后超时。阈值 3 和恢复窗口属于源码默认及事后只读配置观察，未保留当时的熔断状态日志，故归因依据是代码与调用时序一致，而非历史状态直接证明。完整调用时序见验证复核。val-008/013 的供应商响应为裸 JSON 数组，离线重放实际解析函数不能得到要求的 decisions 对象，进而触发 malformed decisions；这是输出格式与解析契约不一致，不能算网络断连。

事后于 2026-09-09 04:32:40 UTC 进行不带凭据、照片或查询的 DNS/HTTPS 检查，域名可解析、根路径返回 404，见[连通性记录](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/network-after-validation.json)。这只能证明事后已连通，无法证明历史故障属于本地网络、链路还是供应商。未用恢复后的选择性重跑覆盖首轮记录，也未把失败空答案计作正确拒绝。

按 18 个查询家族配对 bootstrap，D−A 的 Recall@5 差为 −29.17 个百分点，95% 区间 [−38.44, −17.80]；难负例命中差为 −86.25 个百分点，区间 [−93.26, −77.03]。mean loss 从 0.608333 降至 0.400000，差值区间 [−0.32176, −0.11341]。损失下降反映既定权重下减少误返的取舍，不能替代对召回、运行完成率和时延的判断。区间仅对本次家族样本重采样，未覆盖模型多次随机输出或集中网络故障的时序不确定性，不能据此估计“网络正常时”的纯语义效果。

## 结论与后续设计优先级

本轮完成了规范化评测与失败定位，**没有验证出可用于宣称全面改善的产品效果**。验证 D 相对 A 新增丢失 24 个查询—正例对：15 个涉及 embedding 连接故障或熔断、2 个响应格式失败、7 个约束或语义核验损失；仅新增救回 1 对。网络故障不能解释全部质量损失。开发集 D 保留较高召回并减少易混淆结果，但大多数查询达到 45 秒边界；独立验证既有语义误杀，也受连接与熔断影响。已有预算和熔断机制限制了无效请求，但交互成功率仍不足。

建议后续按真实问题调整设计，以下均为建议，尚未在本轮实施：

1. 修复自然语言否定与强约束解析，将推断条件与用户明确条件区分；结合索引证据覆盖率，避免不完整旧字段直接造成硬过滤。
2. 在[检索循环](E:/project/agent/photo-agent/app/services/search_engine.py)中区分“已找到可用目标”和“必须继续补足五张”，制定与交互意图一致的提前停止策略。
3. 收紧[原图触发策略](E:/project/agent/photo-agent/app/services/search_reranker.py)和[视觉核验](E:/project/agent/photo-agent/app/services/search_visual_verifier.py)的接收条件，把调用优先用于可能改变答案的细节证据；当前按批次的 uncertain 触发可能在已找到正例后继续大量调用。
4. 补齐响应解析、缓存/用量写入与熔断状态的错误阶段记录；对无法离线复现的响应后失败保留异常证据，不能以 HTTP 成功代替业务成功。
5. 在下一版本选择规则中先明确可接受的交互时延与完成率门槛，再比较检索质量。现有 loss 没有为正例查询额外惩罚运行不完整，容易选出交互体验不达标的配置。

本验证集已被使用；后续看到这些结果再改提示词或参数，需另建未见验证样本。Agent 的候选浏览、全量相册兜底及后续对话不在本次 SUT 内，最终用户选图成功率还需单独测量。现有单次 AI 标签、合成开发图、40 张便利抽样真实图、历史开发索引和模型别名漂移限制继续有效。没有本次 Agent 多轮或微信端 E2E 证据，也未据此改变整体产品发布结论。

## 费用与执行核对

| 阶段 | 保守估算费用（元） |
|---|---:|
| 预检（包括最初 3 次连接失败的预留额） | 0.017286 |
| 开发集 A/B/C/D | 14.465330 |
| 40 张验证图片建索引 | 0.145164 |
| 验证集 A/D | 2.319980 |
| 合计 | **16.947760** |

总账本记录 3,826 次模型调用尝试，其中 510 次包含图片；已确认输入/输出用量分别为 5,265,701 / 676,201 Token，另有 433 次调用用量未知并保留预留额。估算不等于供应商实际扣费，未使用未知用量的零值冒充真实免费调用。检索的 1,028 条首轮记录包含熔断拦截的零模型调用查询，不能描述成 1,028 条交互全通过。最终[独立证据审计](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/final-evidence-audit.json)已通过，问题清单为空，核对了全部 1,028 条结果与调用账本、响应引用和结果身份；[SHA256 清单](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/result-evidence-manifest.json)封存 5,947 个证据文件。此审计确认记录完整性，不代表检索质量通过。

离线实现验证已完成 85 项测试，最终依赖清单冻结后另有 7 项冻结专项复验；真实 SQL/Redis 固定向量集成检查通过。实测没有因结果好坏修改冻结源码、配置、标签或选择规则。

## 证据和执行顺序

本次任务目录：`.project-to-act/tasks/S6-RETRIEVAL-20260908`。

- `PROTOCOL.md`：结果出现前确定的协议及澄清。
- `tests/eval/retrieval_v2/review.json`：开发复核记录、9 条变更和逐图观察。
- `tests/eval/retrieval_validation/freeze.json`：独立验证集文件哈希；来源见 `ATTRIBUTION.md` 和 `provenance.json`。
- `evidence/development-index-snapshot.json`：原测试账号只读导出的索引；隔离库已逐项核对。
- `evidence/offline-integration-smoke.json`：真实 SQL/Redis 链路在禁止外部 POST 的情况下通过检查；固定向量的 Top-5 与独立 Python 余弦排序一致，模型调用为零。这是集成检查，不是准确率结果。
- `evidence/freeze-v1.json`：应用、评测代码、开发数据、配置和评分规则冻结清单，生成后禁止覆盖。
- `evidence/provider-ledger.sqlite3`：获得授权并启动真实调用后生成。每次调用先预留费用；错误、取消或用量不明时保留预留额。
- `evidence/runs`：每条查询的唯一首轮结果、候选及核验轨迹；损坏文件或中断标记不会被静默当作成功续跑。
- `evidence/report`：完整性审核、开发选择、成对置信区间、错误清单与验证汇总。

实际源码与图片已保存在 [冻结输入归档](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/frozen-inputs-v1.zip)：389 个去重文件，222,537,286 字节，包含 157 个冻结源/配置/数据文件、177 张开发及验证图片、验证集冻结文件和两个冻结清单。归档逐文件通过来源字节与哈希核对，详见 [归档清单](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/frozen-inputs-v1-manifest.json)。ZIP SHA256 为 `ab620d6deef363bab751334d011388b2746ca83c19cf9e26eefbdbaa2d1f5f0e`。它不包含密钥、环境文件或后续持续变化的运行账本与结果，最终运行证据已由上述独立审计与清单分别封存。

[运行环境观测](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/runtime-environment-observed.json)记录执行期间的 91 个 Python 分发包和实际 Docker Image ID/RepoDigests；Python 与预冻结的 9 个核心包逐项一致。这是执行期间观测，未倒填为执行前冻结。实际 NumPy 为 2.5.1，而 requirements 写为 2.1.2，恢复环境需要核对实际记录。开发索引可通过现有 seed 从已归档快照恢复；验证快照目前没有直接导入入口。离线重算已保存结果与重新付费调用别名模型的区别、可用恢复步骤及限制见 [复现说明](E:/project/agent/photo-agent/.project-to-act/tasks/S6-RETRIEVAL-20260908/evidence/analysis/reproduction-notes.md)。

以下是首次执行顺序，已完成的阶段不应为了更新报告而再次调用模型。在项目根目录使用虚拟环境执行：

```powershell
$env:PYTHONUTF8="1"
# freeze 仅用于首次建立冻结记录；已有 freeze-v1.json 时不要覆盖。
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run freeze
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run verify
# 取得明确数据发送授权后，才执行后续真实模型调用。
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.probe
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run run --dataset development --variant A
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run run --dataset development --variant B
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run run --dataset development --variant C
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run run --dataset development --variant D
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.report development
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.seed_validation
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.freeze_validation
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run run --dataset validation --variant A
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.run run --dataset validation --variant D
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.report validation
.venv/Scripts/python.exe -B -m scripts.retrieval_eval.report audit
```

开发与验证已完成首轮实测、失败根因复核、报告检查与独立证据封存。本次评测任务已验收完成，整体产品阶段 6 与发布验收状态保持不变；实测暴露的召回、延迟和可靠性问题仍需后续修复与新的独立验证。
