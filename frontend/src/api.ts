/** 阶段指标来自后端快照；估算值不可当作已完成的官方 token 统计。 */
export interface StageProgress {
  status: "waiting" | "running" | "validating" | "completed" | "failed" | "skipped";
  phase: "waiting" | "preparing" | "thinking" | "answering" | "validating" | "done" | "failed" | "not_called";
  input_tokens: number | null;
  output_tokens: number | null;
  tokens_per_second: number | null;
  is_estimate: boolean;
  elapsed_seconds: number;
}

export interface TaskProgress {
  attempt: number;
  started_at: string;
  updated_at: string;
  execution_seconds: number;
  retrieval_seconds: number | null;
  queue_wait_seconds: number | null;
  stages: {
    search_plan: StageProgress;
    rerank: StageProgress;
    analysis: StageProgress;
  };
}

/** 后端完成后的分析结果；参考片段由检索流程提供，不由模型编造。 */
export interface AnalysisResponse {
  origin: "model" | "safety_rule";
  model: string;
  elapsed_seconds: number;
  metrics?: Record<string, unknown> & { progress?: TaskProgress | null };
  sources: {
    id: string;
    name: string;
    snippet: string;
    verification: string;
    clinical_review?: string;
    source_url?: string;
    source_title?: string;
    attribution?: string;
    topic_type?: string;
    context?: { snippet: string; start_index: number }[];
  }[];
  analysis: {
    status:
      | "needs_clarification"
      | "analysis_available"
      | "urgent_attention"
      | "unable_to_analyze";
    summary: string;
    facts: {
      kind: "reported" | "denied" | "hypothesis" | "uncertain";
      quote: string;
      meaning: string;
    }[];
    directions: {
      name: string;
      reason: string;
      limitation: string;
      source_ids?: string[];
    }[];
    missing_information: string[];
    safety_notice: string;
  };
}

/** 单条补充或事实纠正，index 对应所选父轮的问题或事实。 */
export interface FollowUp {
  action: "answer" | "skip" | "correct";
  index: number;
  text: string;
}
/** 一批待提交的回答与明确跳过项；未填写的问题不放入 items。 */
export interface FollowUpBatch {
  action: "batch";
  items: { action: "answer" | "skip"; index: number; text: string }[];
}
/** 一轮会话贡献；兼容旧单条记录和新批量 items，不由前端拼接父链。 */
export interface ConversationTurn {
  id: string;
  status: string;
  contribution: {
    action: "initial" | "answer" | "skip" | "correct" | "batch";
    question?: string;
    text: string;
    items?: {
      action: "answer" | "skip";
      index: number;
      question: string;
      text: string;
    }[];
  };
}
/** 来源核对覆盖范围；source_checked 不代表医学专家已审核。 */
export interface KnowledgeReview {
  policy: string;
  total_records: number;
  active_family: string;
  active_documents: number;
  source_checked: number;
  clinical_review: string;
}

/** 任务状态快照；attempts 是工作流执行次数，result 在完成前可为空。 */
export interface Job {
  id: string;
  text: string;
  status: "queued" | "running" | "retrying" | "succeeded" | "failed";
  attempts: number;
  created_at: string;
  result: AnalysisResponse | null;
  error: string | null;
  trace: string[];
  prompt_version: string | null;
  display_text?: string | null;
  turn_action?: string;
  progress?: TaskProgress | null;
}

/**
 * 统一发送 HTTP 请求并返回类型为 T 的 JSON；204 返回空值。
 * 非成功响应转换成可展示的错误，向上交给页面处理，不在此自动重试。
 */
export async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    if (response.headers.get("content-type")?.includes("application/json")) {
      const error = await response.json();
      throw new Error(error.message ?? "分析服务暂时不可用，请稍后重试。");
    }
    throw new Error("无法连接分析服务，请确认后端已启动。");
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json();
}

/** 提交首次描述并返回任务对象；后续是否轮询取决于任务状态。 */
export function submitAnalysis(text: string) {
  return request<Job>("/api/analysis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}
