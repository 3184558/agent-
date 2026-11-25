# 在后端和 worker 各自的终端中 dot-source 此文件；只影响当前终端环境。
# 新资料/正文重排试用配置；使用双查询向量召回。
$env:HEALTH_EVIDENCE_FAMILY = 'medlineplus_summary'
$env:HEALTH_RERANKER = 'body_llm'
# xhigh 的阶段生成额度已扩大，窗口也需留出输入空间。
$env:HEALTH_NUM_CTX = '32768'
$env:HEALTH_CANDIDATE_LIMIT = '6'
$env:HEALTH_CONTEXT_NEIGHBORS = '0'
