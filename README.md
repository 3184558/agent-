# 拾言 · 本地健康信息整理 V0.3

Vue 3 + FastAPI + Ollama，支持独立追问卡片、批量补充、连续会话、事实纠正、经来源核对资料检索，以及本机记录。仅用于个人学习和研究。

## 启动

Python 解释器选择项目 .venv/Scripts/python.exe。先启动 Ollama，再分别在 PyCharm 运行：

1. run_backend.py：后端 127.0.0.1:8000。
2. scripts/worker.py：任务处理程序。
3. 前端终端执行 `cd frontend`，然后 `npm.cmd run dev`。

打开 <http://127.0.0.1:5173>。已有服务升级后需停止并重新运行后端和 worker，前端刷新即可。不要重复占用8000或5173。

仍可使用 start_backend.bat、start_worker.bat、start_frontend.bat。接口文档位于 <http://127.0.0.1:8000/docs>。

## 多轮使用

每个追问保留独立文本框和“不清楚，跳过”勾选项。填写完后点击底部“提交已填写的回答”，一批最多4项，只创建一个任务、分析一次；不必全部回答。空白项目不提交，明确勾选跳过的项目记录为未知。勾选跳过会暂停该框编辑，取消勾选可继续使用原草稿。

会话历史在同一轮内逐项显示问题、回答和跳过状态。提交失败或超过长度预算时，原问题、回答草稿和跳过选择保留，可修改后再次提交；成功后清空这批草稿，展示新一轮问题。未提交草稿仅在当前页面内存中保留，刷新或切换会话不会保存。事实纠正仍使用单条提交。

“开始新分析”会创建新会话。历史列表可打开已有轮次并恢复其父链；也可从旧轮次继续形成新分支。有后续轮次的记录应先从最新轮开始删除，不能直接删掉父记录。

首次描述上限1200字符，每条补充或纠正上限500字符，整个合成会话上限3000字符。超过预算会提示新建分析，不静默截断。模型仍可能理解错误，用户纠正功能不能代替内容评估。

## 资料审核范围

默认只使用已与公开来源核对的9个摘要，不代表整库已获医学批准。页面显示覆盖范围和来源链接；没有资料编号支持的方向标为模型推测。摘要不包含原数据的药物、剂量或治疗方案。

全库2194条记录的结构及引用元数据审计已执行；4198个唯一PMID取得NLM元数据。标题近似匹配和年份一致不等于文章支持具体医学陈述，医学专家审核尚未完成。数据文件没有许可字段，使用授权仍待核实。

审计与评估详情见 [阶段2/3交付记录](docs/stage23_acceptance.md)。不要把旧版运行记录当作当前评估结论。

重新发布公开来源核对摘要：

```powershell
.\.venv\Scripts\python.exe scripts\apply_source_reviews.py
.\.venv\Scripts\python.exe scripts\build_knowledge_indexes.py
```

重新审计全库引用元数据（只向NLM发送公开PMID，不上传病例描述）：

```powershell
.\.venv\Scripts\python.exe scripts\audit_dataset.py
```

未缓存的元数据请求不自动重试；失败记录为待核查。PubMed返回元数据缓存在review/pubmed_metadata.json，技术检查结果在review/dataset_audit.csv。

## 向量库与Agent

SQLite + SQLAlchemy 保存任务、父轮次和来源核对状态；Huey管理任务；LangGraph执行检索词生成、多查询向量召回、候选初筛与分析。三个Prompt位于backend/app/prompts，任务记录组合哈希。

默认使用独立的 reviewed_summary 集合：9条已核对摘要、9个分块。嵌入输入与返回的 snippet 完全相同，不再检索原文后替换成摘要。RecursiveCharacterTextSplitter配置为800字符、目标重叠120字符；qwen3-embedding:0.6b生成向量，qwen3.8:27b负责推理。source_checked仍不等于医学专家审核通过。

SQLite已保留2194份完整原记录，以及 description、executive_summary、conversation 各2194份独立字段快照。字段文档保留原字段名、父文档标识、父记录版本和数据集哈希；原文件没有改写。原始对话中的 system 内容仅作为研究文本保存，不成为运行时指令。

重新导入所选字段（只保存数据库快照，不自动计算研究向量）：

```powershell
.\.venv\Scripts\python.exe scripts\import_dataset.py "C:\Users\fyy31\Desktop\疾病对话数据集.jsonl" --fields description executive_summary conversation
```

三个研究集合分别为 research_description、research_summary、research_dialogue，不与 reviewed_summary 合并。需要时显式建立某一研究索引，dataset-hash 使用导入命令输出的SHA-256，例如：

```powershell
.\.venv\Scripts\python.exe scripts\build_knowledge_indexes.py --family research_description --dataset-hash f828c30cee7006a3cf5b88909f9b865688f98b11d4c886108b9b9a39e5402e0b
```

索引发布清单位于 data/indexes/<family>.json，历史清单位于 data/indexes/versions/。版本由文档版本、分块配置、嵌入模型名称与Ollama权重digest共同确定。旧 data/index.json 和3158分块的旧集合保留，但新检索不再使用。摘要修改、撤销或数据版本失配后旧片段停止参与默认检索；修改后先更新核对记录，再运行摘要索引发布命令。权重digest变化时拒绝混用旧向量，需重建索引。

检查当前索引与原文快照、文本哈希及元数据的一致性：

```powershell
.\.venv\Scripts\python.exe scripts\verify_knowledge_index.py
# 可选：调用本地模型重新计算全部片段向量，报告数值差异，不要求浮点结果逐位相等
.\.venv\Scripts\python.exe scripts\verify_knowledge_index.py --reembed
```

本次实现与实测详情见 [索引一致性交付记录](docs/index_consistency.md)。

## 配置

环境变量前缀HEALTH_，定义见backend/app/config.py。主要包括MODEL、OLLAMA_URL、EMBEDDING_MODEL、TIMEOUT、NUM_CTX、NUM_PREDICT、DATA_DIR、CHUNK_SIZE、CHUNK_OVERLAP。

HEALTH_REVIEW_POLICY默认reviewed_only；research_all只读取显式建立的 research_description 索引，不自动混入研究摘要或对话。研究摘要和对话目前提供独立加载及建索引入口，未接入在线医学分析流程。本次未批量建立研究向量索引。MAX_ATTEMPTS默认3，RETRY_DELAY默认2，保持既有2秒/4秒退避策略，仅暂时性模型故障重试，格式错误不重试。

同机worker共享既有文件锁，模型调用串行。本版本没有新增并发锁或自动恢复。业务库与队列不是跨库原子事务，硬退出后running状态仍可能需要人工处理；不保证exactly-once。

原文与结果持久保存在项目data/，已加入Git忽略。界面可删除终态记录，删除不是磁盘安全擦除。无账号或跨用户隔离，只绑定本机。LangSmith tracing在此工作流中关闭。

## 测试与评估

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
cd frontend
npm.cmd run build
```

57项工程测试通过，包含批量补充的一批一个任务、逐项记录、空白省略、长度预算、即时规则提醒及索引一致性。前端构建通过，隔离浏览器验证覆盖统一提交、请求失败保留草稿、总预算超限保留草稿和成功后更新问题。验证使用固定模型响应，没有对真实用户记录写入测试数据。

既有阶段2/3评估另有12条冻结新案例、无检索/资料检索两组共24次真实模型运行；它们属于之前的索引版本，不代表本次新索引的医学效果，尚未对新版本重新进行独立临床效果评估。历史结果见evaluation/stage23_comparison.csv与evaluation/stage23_summary.json。

独立于开发案例的评估脚本：scripts/evaluate_stage23.py。它会使用现有模型锁，运行时普通分析可能等待；不要根据同一批测试结果反复调参后声称独立评估通过。旧scripts/replay_retrieval.py属于开发回归，不属于独立评估。

为避免占用用户端口，浏览器验证使用8001/5174与隔离数据库；frontend/vite.verify.config.ts仅用于该验证，正常使用无需启动它。API变更详见 [接口说明](docs/api.md)。


## V0.4 本地资料与检索实验（2026-11-05）

已加入 MedlinePlus 公共主题摘要试点、双查询向量召回与等权 RRF、正文筛选与本地 Cross-Encoder 实验选项，以及真实阶段和尝试耗时。旧资料和历史保留。历史对照后选定纯向量召回；Cross-Encoder 也未通过阈值选择。

操作说明见 [V0.4 实施说明](docs/retrieval_v04.md)，本次结果及限制见 [V0.4 评估报告](evaluation/v04_report.md)。试用新资料的后端和 worker 终端均先执行 `. .\scripts\profile_v04.ps1`，然后按原方式启动。原有服务未被本次脚本接管或重启。
