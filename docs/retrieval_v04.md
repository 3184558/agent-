# V0.4 检索实施与操作说明

实施日期：2026-11-05。原计划见 `本地健康分析系统_检索优化方案与实施计划_20261105.md`。

## 当前交付

- 保存实施前源码 ZIP、文件哈希、配置、旧索引清单和一次真实模型基线；原有资料、索引、会话未清空。
- 公共资料独立 family：MedlinePlus 英文健康主题摘要。保留官方 XML、原始 HTML、抽取正文、偏移、来源 ID、哈希、下载时间、主题类型和审核状态。未生成中文翻译，也未抓外链、百科、图片或模拟对话。
- 检索计划一次模型调用同时整理原话线索与英文查询，区分肯定、否定、猜测、未知，校验原话来源并规范化去重。纠正/矛盾语义仍需模型理解，不能把逐字引用校验解释为语义保证。
- 最多两条查询分别走向量召回，使用等权 RRF 合并排名。默认 c=60，同一查询内每个片段只计一次。
- 支持名称初筛基线、正文 LLM、小型本地 Cross-Encoder、无重排四个互斥配置。正文 LLM 分批判断相关性，再按原融合排名收敛；它是正文过滤，不能冒充全局 LLM 打分排序。
- 同版本邻接块回取可选，最多一侧相邻块；正文、偏移和哈希保留，不回取整个病例。默认关闭，尚无质量收益证据。
- 持久记录真实阶段 trace、每次失败/成功尝试、首轮排队观测、既有文件锁等待、执行及节点耗时。没有新增锁、重试或并行 Agent。普通分析不联网更新知识。
- 前端显示真实阶段、当前资料范围、来源状态和同版本邻接片段，兼容旧记录。

新公共资料流程没有保留相关片段时，只整理事实与证据缺口，不发布无资料疾病方向；该规则在模型输出入口校验。

新算法未自动提升为默认。开发集混合检索表现低于纯向量，Cross-Encoder 阈值也尚未通过评估。原默认资料仍是原来的已核对摘要；试用新资料和正文筛选见下方独立配置。

## 启动

所有命令从项目根目录执行，使用项目 `.venv`。依赖安装：`.\.venv\Scripts\python.exe -m pip install -r backend/requirements-lock.txt`。保持原有后端、worker、前端启动方式。需要试用新资料时，在后端和 worker 各自终端先执行：

```powershell
. .\scripts\profile_v04.ps1
```

随后分别执行原有后端/worker 启动命令：

```powershell
.\.venv\Scripts\python.exe run_backend.py
```

```powershell
.\.venv\Scripts\python.exe scripts\worker.py
```

前端：`npm.cmd run dev --prefix frontend`。原有进程需要自行按原运维方式重启才能读取新环境；本次没有接管正在运行的服务进程。


新资料/正文流程使用保守 UTF-8 字节上界控制模型输入预算（加输出与模板余量）；不静默丢弃用户事实。16384 是本次试验值，不保证最长会话都可容纳；超预算会明确失败，用户可新建描述或配置更大上下文。旧流程维持其既有预算行为，未宣称完成旧模型 tokenizer 精确预算。

## 下载、导入、发布、撤销

```powershell
.\.venv\Scripts\python.exe scripts\download_medlineplus.py
.\.venv\Scripts\python.exe scripts\import_medlineplus.py <上一步路径> --selection evaluation\medlineplus_pilot.json
.\.venv\Scripts\python.exe scripts\build_knowledge_indexes.py --family medlineplus_summary
.\.venv\Scripts\python.exe scripts\verify_knowledge_index.py --family medlineplus_summary --output evaluation\medlineplus_consistency.json
```

下载器只请求官方 XML 页面和最新主题 XML，不传用户输入。选题表中未找到的标题写入清单，不以近似名称静默替代。主题类别为开发者初标，仍待审核。

导入只生成候选清单。向量构建并核对分块数量后替换发布指针，构建失败时继续使用旧指针。已有版本清单可继续读取。

撤销：在 `data/sources/medlineplus/revoked.json` 中保存被撤销的 `document_version` 字符串数组。该允许集合同时约束向量召回和邻接回取；旧版本保留用于审计，不再新召回。删除撤销记录属于显式恢复操作。

显式构建旧资料快照：给建库命令加 `--dataset-hash <资料哈希>`。默认取最近导入候选，撤销版本不会进入新索引。

切回旧发布：

```powershell
.\.venv\Scripts\python.exe scripts\activate_index.py --family medlineplus_summary --version <旧index_version>
```

也需切回当时完整配置。支持读取既有 schema 2 和 3 清单。原先 9 条摘要的旧清单未被本次公共资料建库覆盖。完全回到实施前代码可参考 `evaluation/baseline_v04/source_before.zip`，不要覆盖后来新增的用户工作。

## Cross-Encoder

试验模型为 `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`，Apache-2.0，固定 revision `1427fd652930e4ba29e8149678df786c240d8825`。本机已下载至 `data/models/mmarco`，其他机器可显式运行 `scripts/download_reranker.py` 下载同一 revision；运行时设置 `local_files_only=True`，使用 CPU。设置 `HEALTH_RERANKER=cross_encoder`、`HEALTH_CROSS_ENCODER_PATH` 和 `HEALTH_CROSS_ENCODER_REVISION`；模型对输入逐对评分，超其 512 token 上限明确失败。最低保留分数由 `HEALTH_CROSS_ENCODER_MIN_SCORE` 控制，分数不是概率。

同候选初试中阈值 0 未保留任何片段，不能认为它已适合本项目，也不能用这项观察断言模型本身没有相关性能力。尚未进行阈值调优与专业相关性裁决，保留为实验选项。

## 复现评估

```powershell
.\.venv\Scripts\python.exe scripts\compare_rerank_v04.py --results evaluation\v04_development_results.json --cases evaluation\v04_development.jsonl --output evaluation\v04_rerank_comparison.json
.\.venv\Scripts\python.exe scripts\evaluate_live_v04.py --cases evaluation\v04_live_edges.jsonl --count 8 --output evaluation\v04_live_edges_results.json
```

顺序运行这些脚本；不要与 worker 同时打开本地 Qdrant。沿用既有单机运行约束，没有为此增加并发保护。检索评估预先批量计算真实本地嵌入并缓存复用，各配置共享向量，缓存阶段耗时不能作为在线端到端速度。英文查询由人工预置，生成查询的质量由独立真实模型案例检查，不能混为一个指标。

开发/保留各 40 条，按场景组隔离同义改写，调参前冻结文本与哈希；初标不是医学真值。每个分区有两条库外输入，不强算 Recall。主题标签只标到预期主题，不是穷尽片段级判断；Precision 是初标下的观测，尚不能作为严格相关性精度。未做专家医学裁决、公众部署或临床有效性验证。

评估文件含本地案例、查询和逐片段审计，请按本地健康数据管理，不上传外部追踪。资源采样每秒一次，GPU 是设备总占用，包含 Ollama/其他进程；不能保证捕捉瞬时峰值。第一次尝试的排队观测含调度延迟，后续尝试的 since_created 不能当成新增排队时间。

## 公共来源核验

2026-11-05 复核：[官方 XML](https://medlineplus.gov/xml.html)、[XML 字段说明](https://medlineplus.gov/xmldescription.html)、[内容使用规则](https://medlineplus.gov/about/using/usingcontent/)、[Cross-Encoder 模型卡](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1)。公共领域范围限定为主题摘要，来源署名保留；不能据此推定整站资料同许可。
