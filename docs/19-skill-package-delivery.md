# P3：可上传的 Skill 流程包

2026-09-06；S6-P3-001。P3开发切片完成，生产发布及真实数据库验收尚未通过。

## 使用流程

进入 Web「Skill 广场」，选择含唯一 SKILL.md 的 ZIP，查看名称、参考图、资源数量、许可证与兼容报告，再点击「保存私有流程包」。保存后切换至「我的 Skill」，详情中可查看版本、入口说明与按需读取的资源，也可以为同一个 Skill 上传新版本。

旧模板 Skill 继续按原有流程使用。流程包独立标识，不能直接编辑或公开；版本内容不可变。重复内容返回已存在版本，不增加存储，也不把当前版本回切到历史版本。

本次可导入和管理流程说明、规范及参考图；没有接通流程包的图像生成。生成准备与 Worker 均拒绝流程包，推荐器暂不推荐此类型。P4才实现原图分析、创作方案、资源角色、确认与生成绑定。

## 导入契约

- SKILL.md需有YAML头：name（1–64字）、description（1–4000字）；source和license可选，入口原文完整保留。
- 自动定位唯一入口。根目录外文件明确列为未导入；多个入口报错。读取包内Markdown链接、引用式链接、行内代码路径，以及agents/openai.yaml的图标引用。不是任意自然语言或程序的依赖分析器。
- 支持UTF-8 Markdown、文本、声明资料和PNG/JPEG/WebP静态图片。agents/openai.yaml只作为资料保存，不能安装或授权其中的工具；远程引用只保留并提示，不访问。脚本、不支持格式或缺失本地资源导致报告can_import=false。
- 上传上限16 MiB、解压总量24 MiB、单文件8 MiB、单文本128 KiB、128条目、20百万像素、压缩比200倍。每用户最多100个已保存版本。
- 路径越界、绝对路径、反斜杠、控制字符、归一化重复路径、链接、加密ZIP与异常图片均拒绝。不向磁盘解压、不执行包内内容。Markdown按纯文本显示。
- schema_version=1、importer_version=skill-zip-v1；文件清单和内容SHA256形成与ZIP包装/压缩方式无关的内容身份。保存必须回传预览的expected_hash，内容变化返回409。

接口：

| 接口 | 作用 |
|---|---|
| POST /skills/packages/preview | 原始application/zip请求体，返回兼容报告与可选参考图预览 |
| POST /skills/packages/import?expected_hash=…&skill_id=… | 同样上传ZIP，skill_id省略为新包，否则追加版本 |
| GET /skills/{skill_id}/versions | 所有者查看不可变版本和报告 |
| GET /skills/{skill_id}/versions/{version_id}/assets?path=… | 所有者按需读取资源；private/no-store、nosniff |

版本和资源以SkillVersion、SkillAsset存储；有限大小的资源使用数据库二进制字段，与元数据同事务提交，避免首版引入对象存储和数据库之间的半完成状态。用户级行锁串行化导入与版本额度检查。该锁及事务性质仍需在真实PostgreSQL验证；不是并发性能结论。当前版本指针由服务端同事务设置，不提供客户端任意赋值接口。

## 样例和验证

指定photo-to-organic-knit包已在内存打包并离线解析：4文件；SKILL.md中的references/style-spec.md和assets/style-reference.png均成功解析，图片格式核验通过。未把用户本地资产复制或发布到仓库。报告见任务evidence/knit-import.json。

最终Python非集成118 passed、1 skipped、35 deselected，其中导入专项20项；Web37 passed，lint、typecheck及build通过。测试覆盖恶意包、引用、资源核验、权限查询范围、私有版本写入、哈希变化、重复导入和生成阻断。API存储测试使用Session替身；另提供待运行的真实持久化测试test_skill_package_integration.py。

Docker服务不可用，既有测试端口未连接成功，因此没有运行真实数据库/Redis集成、迁移往返、并发导入或浏览器E2E。迁移仅生成PostgreSQL升级与回退SQL供检查；没有调用付费模型或消费评测Validation/Test集。阶段6 revision4及发布阻塞不变。

## 升级与回退

新增依赖PyYAML==6.0.3；迁移20260906_0001在20260905_0001之后增加Skill类型/版本指针与两个表。部署前先在隔离数据库运行迁移、Schema差异检查和集成测试，再迁移目标数据库并同步部署API、Worker与Web。当前环境尚未执行实库迁移，直接启动新代码连接旧Schema会报错。

旧API/Worker不认识流程包，不能与支持包导入的新实例混跑，也不能在已有包时直接切回旧应用。回退迁移遇到仍存在的流程包会中止：先备份并由所有者明确处理包数据，或保持新版代码关闭入口，不能自动删除资源或转换成模板。发布验收与P4执行能力分别验证。
