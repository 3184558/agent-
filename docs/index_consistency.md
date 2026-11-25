# 独立摘要索引与版本一致性（2026-11-01）

此次修复针对“检索原知识字段的向量，返回时却替换成已核对摘要”的不一致。现在默认检索已核对摘要自身的向量，返回正文就是用于生成该向量的snippet，分析提示词也使用同一正文。提示词只选择必要的引用字段；完整版本元数据保留在任务结果中。

## 资料与集合

| 资料 | 加载字段 | 用途 | 本次落地 |
|---|---|---|---|
| 公开来源核对摘要 | source_reviews.excerpt | reviewed_summary，默认医学参考 | 9份快照，9条向量 |
| 原数据描述 | description | research_description | 2194份快照，未批量嵌入 |
| 原数据案例摘要 | executive_summary | research_summary | 2194份快照，未批量嵌入 |
| 合成医患对话 | conversation | research_dialogue | 2194份快照，未批量嵌入 |

“医学参考集合”是路由用途，不是医学专家审核结论；9份摘要的clinical_review仍为pending。

knowledge_record_versions保留2194条原记录的全部字段与JSON结构。knowledge_document_versions追加字段文档快照，不覆盖旧版本。研究文档带parent_version、parent_document_id、dataset_hash、line_number及原field。对话按原顺序以role: content渲染，完整原消息数组仍在父记录中；其中system文字没有进入运行时system prompt。别名、药物、PMID等原字段没有混入description的嵌入正文。

默认Agent查询reviewed_summary。显式research_all仅查询research_description；summary/dialogue研究索引有独立构建入口，尚未开放在线Agent检索它们。没有用对话反向补充患者事实。

## 发布与更新

1. 原JSONL按文件SHA-256确定数据版本，原记录和字段文档分别留存。
2. 核对记录决定可发布的摘要正文和来源元数据；字段内容与来源信息共同确定document_version。
3. 文档版本、模型名称与权重digest、分块配置和构建格式共同确定index_version。向量维度也写入发布清单。
4. 新集合全部写完后才切换data/indexes/<family>.json。历史清单保存在versions/，旧集合保留；同版重复执行复用已有集合。发布失败不切换指针。
5. 默认查询取“当前仍被核对的文档版本”与“已发布版本”的交集。修改、撤销、数据版本不一致都会让旧片段停止参与新查询；新摘要必须重建后才可用。查询不覆盖payload正文或来源信息。
6. 嵌入权重digest改变时拒绝使用旧向量；显式重新建索引。旧index.json不作为兼容回退。

现有单机文件锁由导入、构建、核验脚本和worker共同复用。没有新增锁机制、自动重试或自动清理。快照与旧集合会占用更多磁盘；本版本保留历史，清理策略尚未实现。数据集哈希变化会产生新的字段版本，当前优先保证可追溯性，未实现跨版本嵌入缓存。

## 实测

- 43项工程测试通过，覆盖文字一致性、原字段保留、父子关系、研究集合隔离、状态撤销、权重变化、索引幂等及分批构建失败时不发布。
- 原文件SHA-256：f828c30cee7006a3cf5b88909f9b865688f98b11d4c886108b9b9a39e5402e0b。逐行比对2194个数据库父快照与原JSON对象相同；三个研究字段分别2194份，其正文及父关系均核验。
- 默认集合：reviewed_summary_edec86ec439316dd15a6e79e；1024维。9个片段均核对原文切片、正文哈希、文档哈希、来源元数据及索引版本。
- 使用实际Ollama重新嵌入9条正文后，最低余弦相似度0.999664081558328、最大分量差0.003631801759728475。正文一致不意味着浮点向量逐位相等；数值差异原因未定位。核验脚本将这些数值作为观测结果报告，不把它们解释为医学准确率。
- 实际检索“鼻部干痂、鼻出血、痰血、镜下血尿、疲劳”测试描述，返回当前摘要集合中的Wegener's granulomatosis、Common cold、Allergic rhinitis；三条均携带当前索引版本。这只验证检索链路，不构成疾病判断或独立医学评估。
- API进程内检查确认active_index显示新集合；旧3158片段的diseases_799fd6b785a7be28及旧清单仍保留。

机器可读记录：review/index_consistency_20261101.json。scripts/verify_knowledge_index.py可重复检查当前发布索引；加--reembed才会重新调用嵌入模型。

本次没有重跑之前12条案例的独立效果评估。旧评估报告属于旧索引；新索引是否提升疾病相关方向的召回与引用支持程度，仍需新的冻结评估。当前9份摘要覆盖面仍很小，未命中不意味着排除疾病。

## 使用

本机摘要索引已经构建，无需立即重跑。停止并重启现有后端和worker即可加载新代码，不要重复启动占用8000端口的进程。Vue无需修改。

以后修改review/checked_sources.json后，依次执行scripts/apply_source_reviews.py和scripts/build_knowledge_indexes.py。若需研究向量，先按字段导入，再指定--family与--dataset-hash单独构建；它们不会自动进入默认医学参考集合。
