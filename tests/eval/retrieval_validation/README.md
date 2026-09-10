# 独立真实图片验证集

当前版本：`1.0.1-independent-real-commons`。40 张 Wikimedia Commons 真实照片，80 条新写的中文查询，3,200 个闭集二元相关性判断。数据与标签在查看任何开发集或验证集检索输出之前完成。验证集只用于冻结方案的一次性验证，不用于选择或调整检索配置。

| 查询类型 | 数量 |
| --- | ---: |
| 每图具体可见内容 | 40 |
| 多正例与概括问法 | 12 |
| 排除条件与 OCR 冲突 | 16 |
| 预期无结果 | 12 |

共 68 条有正例查询、12 条无结果查询；其中 12 条查询有多个正例。每张图至少在一条查询中为正例。按语义与来源使用 18 个查询族，避免同图改写被当成完全独立的样本。单图查询的固定分母 Precision@5 上限只有 20%，评测报告需要同时给出 Recall、MRR、nDCG 和正例数分层。

## 来源与选择

采样方案在 `sampling-plan.json` 中预先固定：动物、人物动作、食品、交通、花草、自然风景、乐器、厨房、雨伞、运动、电脑、购物，以及清晰的真实路牌文字。按照 Commons 返回顺序检查候选，对不符合类别的绘画、图标、自动演奏钢琴或缺少主体的图片记录剔除原因。受网络连接中断和 HTTP 429 影响的类别通过保留日志的重试和同类别精确文件名搜索补齐。选择理由逐图保存在 `annotations.json` 和 `provenance.json`。

40 张最终图片都是 JPEG，合计约 12.67 MiB。使用 Wikimedia 提供的缩略图原始字节，通常宽 1,280 像素，较小源图保留原分辨率；没有进行本地内容修改或生图。部分照片由原作者采用黑白、棚拍或其他摄影处理，来源真实性依赖 Commons 的来源与摄影元数据，不能据此证明是未经处理的相机原件。

完整原始来源页、原图 URL、实际下载 URL、作者、许可、SHA256 与尺寸见 `provenance.json`，逐图署名见 [ATTRIBUTION.md](ATTRIBUTION.md)。来源 API 响应、候选图片旁的元数据及 `acquisition.jsonl` 保存下载成功与失败记录。下载的是用于真实检索验证的评测输入，不是通过替代下载方式规避媒体展示限制。

许可构成为 CC BY-SA 4.0（15）、CC BY-SA 3.0（5）、CC BY-SA 2.0（7）、CC BY 4.0（2）、CC BY 3.0（1）、CC BY 2.0（2）、CC0（5）、Public domain（3）。再次分发时保留各自来源、作者和对应许可。

## 标注与独立性

标注者为 Codex 的视觉复核，**不是人类双标**。先看完全部 40 张图片，再编写与逐条检查 80 个查询；相关性要求全部明确可见条件同时满足。图像描述、正例、困难负例、来源标题均属于评测侧资料，**不得进入被测模型的检索请求或图片解析提示**。`exclude_ids_from_request` 全为空；困难负例只是评分标注，不是提前排除条件。

与 137 张开发图比较了全部 5,480 个跨集图片对：SHA256 精确重复为 0，64 位 DCT pHash 汉明距离不大于 8 的近似告警为 0，最近跨集距离为 18。验证集内部也无精确重复或该阈值下的近似告警；相邻拍摄候选只选一张，未拆到多个集合。80 条新查询与开发集 217 条查询在去标点及空白后的完全重复数为 0。这些检查不能保证所有裁剪或语义重复都被识别，也不意味着公开图片从未进入基础模型训练数据。

最终原尺寸复核发现遮阳伞照片可见四把近处伞，早先缩略图观察误记为三把。执行前将该查询改为“几把”，相关图片集合不变，版本由 1.0.0 升为 1.0.1。旧预执行快照保存在 `history/1.0.0-pre-execution`；尚未基于任何检索结果修改标签。

限制：这是按概念方便采样的公开照片闭集，不能代表私人相册分布；标注有单一 AI 复核的主观误差；OCR 主要是英文和阿拉伯数字，未建立独立中文 OCR 验证；不覆盖个人身份识别、拍摄日期或 GPS 过滤。最终验证应如实保留模型错误、超时和零结果失败。

## 复现与校验

在仓库根目录运行，读取冻结数据且不访问网络、不调用模型：

```powershell
.venv/Scripts/python.exe -B scripts/retrieval_eval/build_validation_verify.py
```

应通过 40 图、80 查询、3,200 判断和 93 个冻结文件的检查。冻结依据为 `freeze.json`；图片 UUID 由稳定 UUID5 规则生成。`corpus.json` 中的 `index_available_at_snapshot: false` 表示冻结数据时尚未进入被测服务，后续索引完成情况应记录在运行证据中，不要回写冻结清单。

从保存的候选与视觉判断重新生成数据（仅用于明确的新构建；评测开始后不要重写标签）：

```powershell
.venv/Scripts/python.exe -B scripts/retrieval_eval/build_validation_queries.py
.venv/Scripts/python.exe -B scripts/retrieval_eval/build_validation_finalize.py
.venv/Scripts/python.exe -B scripts/retrieval_eval/build_validation_verify.py
```

原始获取过程可通过 `build_validation.py plan`、`build_validation.py acquire`、`build_validation_supplement.py` 重放；开放网页与 API 搜索顺序会变化，保留的原始 API 响应、候选文件与其哈希才是本版本的权威输入。完整过程没有使用付费模型、图像生成或人工标注服务。
