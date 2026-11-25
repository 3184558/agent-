# 接口契约 V0.3

POST /api/tasks/{id}/follow-up 支持批量补充，也保留原单条answer/skip/correct请求供旧客户端与事实纠正使用。只有成功轮次可以继续，id指定这一批回答所对应的父轮次。

批量请求：

```json
{"action":"batch","items":[{"action":"answer","index":0,"text":"持续两个月"},{"action":"skip","index":2,"text":""}]}
```

items为1～4项，index指向父轮次missing_information中的问题，不能重复；每条回答为1～500字符，skip的text必须为空。未填写的问题不发送，不能用空回答代替“未知”。服务端根据父记录恢复问题原文，不接受客户端提交question或自编会话历史。批内不支持correct；事实纠正仍提交单条action=correct，index指向facts。

一批只创建一条analysis_jobs和一条conversation_turns记录，只调度一个工作流任务。整批校验完成后才写入数据库；编号、字段或总长度不合规均返回422，不部分保存。上下文上限仍为3000字符，不静默截断；回答中触发现有即时规则时直接返回200及提醒，不排队等待模型。空白和跳过不会成为明确否定；问题原文和跳过标记不加入用户事实引用原文。

新增 GET /api/tasks/{id}/conversation：返回所选轮次的祖先链，含每轮贡献，不把后台构造格式直接当作用户对话展示。旧版单轮记录可以作为根轮继续。

新批量轮次的contribution包含action=batch、items和text。items逐项保存action、index、服务端问题原文question及text；跳过项的text显示“用户跳过，仍未知”。外层text用于历史摘要，模型上下文使用逐项结构，不重复拼接摘要。旧单条轮次结构保持可读，无需数据库表迁移。新前端必须与支持batch的新后端一起使用。

新增 GET /api/knowledge/review：返回当前检索政策、总条目数、当前数据版本下未撤销的来源核对数量、核对记录和clinical_review=pending。active_index 为当前集合的发布清单，包含 collection、index_version、document_versions、embedding_model、embedding_digest、embedding_dimensions、chunks；没有发布索引时为null。核对记录数量与已发布向量数量是两项不同指标，更新摘要后应重新发布索引。

来源source_checked仅表示摘要已核对列出的公开页面，不表示临床专业批准。默认reviewed_only仅查询独立的 reviewed_summary 集合，嵌入输入与返回snippet相同。核对状态撤销或文档版本变化后，旧片段不再参与查询，不以新摘要覆盖旧向量命中。directions.source_ids绑定实际提供的参考片段；不存在的编号使任务失败。绑定存在不证明语义支持。

历史列表增加display_text与turn_action，区分初始描述、补充、批量补充（batch）、纠正和跳过。父记录有子轮次时删除返回409，必须先从最新轮删除。

POST /api/analysis 接收 {"text":"健康描述"}。text 去除首尾空白后为 1～1200 字符，不接受额外字段。

普通请求返回 HTTP 202 与任务对象；即时规则提醒返回 HTTP 200 与已完成任务，result.origin=safety_rule。与 V0.1 同步返回分析的契约有变化，Vue 已同步更新。

任务字段：id、text、status、attempts、created_at、result、error、trace、prompt_version。
status 为 queued、running、retrying、succeeded、failed。result 在成功后包含原有 analysis、origin、elapsed_seconds、model，以及 sources。sources 来自实际向量片段，包含 id、name、snippet、dataset_hash、start_index、field、family、purpose、parent_document_id、document_id、document_version、index_version、text_hash、document_text_hash、verification及来源信息。研究字段另带parent_version，指向完整原记录快照；已核对摘要以parent_document_id+dataset_hash关联疾病记录，source_url标明公开来源。metadata随片段发布，后续查询不重新改写，历史任务保留当时实际使用的片段。旧任务不补造新版版本字段。

elapsed_seconds 是成功那次执行的耗时，不包含排队、之前失败的调用和退避等待。attempts 是工作流执行次数，不是聊天或嵌入请求数量。

GET /api/tasks/{id}：查询状态、结果和失败原因；不存在返回 404。
GET /api/tasks：按时间倒序返回最近 20 条本机记录。
DELETE /api/tasks/{id}：删除已成功或失败的记录，成功返回 204；运行中或排队中返回 409。
GET /api/health：应用状态、配置的模型名、SQL 疾病记录数；model_checked=false，不检查模型或 worker 是否就绪。

输入错误返回 422。HTTP 错误体为 {"message":"可读信息"}。任务执行失败不改变状态查询 HTTP 200，而通过 status=failed、error 和 attempts 表达，前端必须检查任务状态。

OpenAPI 根据同一组 Pydantic Schema 自动生成于 /openapi.json。没有身份认证或跨用户隔离；用于本机个人应用。

## 分阶段处理指标（2026-11-14）

`GET /api/tasks/{id}` 新增可空的 `progress`，前端沿用每秒轮询。它来自当前尝试的 `analysis_attempt_runs.metrics.progress`；新任务运行中持续更新，终态保留。成功结果的 `result.metrics.progress` 同时保留快照。旧任务及即时规则分支可以为 null，不补造数据。历史列表仍用于摘要，打开历史时应查询单个任务取得完整指标。

`progress` 包含 `attempt`、`started_at`、`updated_at`、`execution_seconds`、`retrieval_seconds`、`queue_wait_seconds`、`stages`。本次执行时间不包含先前尝试和排队等待，重试按 attempt 分别记录。`queue_wait_seconds` 是首次执行前的队列等待，不含原有模型锁等待；重试未单独采集此项时为 null。

`stages` 固定为 `search_plan`、`rerank`、`analysis`。每项含 `status`（waiting/running/validating/completed/failed/skipped）、`phase`（waiting/preparing/thinking/answering/validating/done/failed/not_called）、`input_tokens`、`output_tokens`、`tokens_per_second`、`is_estimate`、`elapsed_seconds`。未采集数值为 null；未调用 LLM 的重排标为 skipped，不把它解释成模型生成了零个 token。

运行中按收到的逐 token 概率记录估算生成量与近一秒接收速率，`is_estimate=true`；不能按网络片段数计 token，也不应把此值称为精确模型吞吐。结束时以 Ollama 的 `prompt_eval_count`、`eval_count`、`eval_duration` 校准，平均速率为 `eval_count / (eval_duration / 1e9)`，`is_estimate=false`。生成量含思考及最终内容，不提供精确的二者拆分。输入 token 数通常在该调用结束时才可用。

阶段耗时含准备、加载、输入处理、生成及校验；平均生成速度仅使用生成时间。重排分批时累加各次官方用量和生成时间。失败保留最近采样；若已收到终帧但业务校验失败，仍保留已获得的官方计数。页面只展示指标，分析正文继续在完整收齐且校验通过后展示。
