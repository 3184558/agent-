"""LangGraph 有界工作流：检索词 → 本地检索 → 结构化分析。"""

import hashlib
import json
from pathlib import Path
from typing import TypedDict
from time import perf_counter

from langgraph.graph import END, START, StateGraph
from langchain_core.runnables import RunnableConfig

from .config import settings
from .vector_store import retrieve
from .ollama_client import PROMPT, generate, check_prompt_budget, stream_chat, publish_progress
from .schemas import (
    Analysis, AnalysisRequest, SearchPlan, CandidateSelection,
    build_evidence, evidence_schema, parse_evidence_output,
)
from .rerank import rerank_candidates

SEARCH_PROMPT = (Path(__file__).parent / "prompts/search.txt").read_text(
    encoding="utf-8"
)
RERANK_PROMPT = (
    (Path(__file__).parent / "prompts/rerank.txt").read_text(encoding="utf-8").strip()
)
# 记录三份提示词的组合版本，便于追溯任务当时使用的提示词内容。
PROMPT_VERSION = hashlib.sha256(
    (
        SEARCH_PROMPT
        + RERANK_PROMPT
        + PROMPT
        + json.dumps(
            [
                Analysis.model_json_schema(),
                SearchPlan.model_json_schema(),
                CandidateSelection.model_json_schema(),
                evidence_schema(Analysis, {"u1": "schema"}),
                evidence_schema(SearchPlan, {"u1": "schema"}),
            ],
            sort_keys=True,
        )
    ).encode()
).hexdigest()[:16]


class State(TypedDict):
    """LangGraph 节点间传递的共享状态；每个节点返回本节点产生的字段。

    text 含会话问答结构，evidence_text 仅含用户原文；sources 保留检索版本信息。
    """

    text: str
    terms: list[str]
    queries: list[str]
    sources: list[dict]
    analysis: dict
    trace: list[str]
    clues: list[dict]
    metrics: dict
    retrieval_audit: dict
    evidence_text: str
    evidence_spans: dict[str, str]


async def search_plan(state: State, config: RunnableConfig):
    """调用本地推理模型生成英文检索计划，返回词项、查询描述和过程记录。"""
    started = perf_counter()
    publish_progress("search_plan", status="running", phase="preparing")
    evidence = state.get("evidence_text", state["text"])
    spans = build_evidence(evidence)
    schema = evidence_schema(SearchPlan, spans)
    user_content = {"user_evidence": spans}
    if state["text"] != evidence:
        user_content["conversation"] = state["text"]
    messages = [
        {"role": "system", "content": SEARCH_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_content, ensure_ascii=False),
        },
    ]
    check_prompt_budget(messages, schema, settings.plan_num_predict)
    content, generation = await stream_chat(
        config["configurable"]["client"], "search_plan",
        model=settings.model,
        messages=messages,
        format=schema,
        think=settings.plan_think,
        options={"temperature": 0, "num_ctx": settings.num_ctx, "num_predict": settings.plan_num_predict},
        keep_alive="10m",
    )
    plan = parse_evidence_output(
        SearchPlan, content, spans,
        context={
            "text": state.get("evidence_text", state["text"]),
            "require_clues": settings.evidence_family == "medlineplus_summary",
        },
    )
    publish_progress("search_plan", status="completed", phase="done")
    return {
        "terms": plan.terms,
        "queries": plan.queries,
        "clues": [c.model_dump() for c in plan.clues],
        "evidence_spans": spans,
        "metrics": {
            "plan_seconds": perf_counter() - started,
            "plan_generation": generation,
        },
        "trace": [
            "检索词：" + ", ".join(plan.terms),
            "检索描述：" + " | ".join(plan.queries),
        ],
    }


async def search_documents(state: State, config: RunnableConfig):
    """召回块级候选并应用所选重排配置，保留最多3条实际片段。

    编号必须来自本次候选列表；初筛只是检索步骤，不是疾病诊断或证据审核。
    """
    client = config["configurable"]["client"]
    started = perf_counter()
    candidates = await retrieve(
        state["terms"], client, state["queries"], limit=settings.candidate_limit
    )
    retrieval_seconds = perf_counter() - started
    publish_progress(None, retrieval_seconds=retrieval_seconds)
    sources, rerank_metrics = await rerank_candidates(
        state["text"], candidates, client, RERANK_PROMPT
    )
    metrics = {
        **state.get("metrics", {}),
        "retrieval_seconds": retrieval_seconds,
        **rerank_metrics,
    }
    if candidates and "retrieval" in candidates[0]:
        metrics.update(candidates[0]["retrieval"]["timings"])
    return {
        "sources": sources,
        "metrics": metrics,
        "retrieval_audit": {
            "candidates": candidates,
            "queries": state["queries"],
            "clues": state.get("clues", []),
        },
        "trace": state["trace"]
        + [
            f"检索召回：{len(candidates)} 个片段",
            f"{settings.reranker}初筛：{len(sources)} 条片段（未核验，不是诊断）",
        ],
    }


async def analyze(state: State, config: RunnableConfig):
    """结合会话上下文与实际参考片段生成分析，使用用户原文约束事实引用。"""
    result, model_metrics = await generate(
        AnalysisRequest.model_construct(text=state.get("evidence_text", state["text"])),
        config["configurable"]["client"],
        state["sources"],
        conversation_text=state["text"],
        evidence_spans=state["evidence_spans"],
    )
    return {
        "analysis": result.model_dump(),
        "metrics": {**state.get("metrics", {}), "generation": model_metrics},
        "trace": state["trace"] + ["生成并校验结构化分析"],
    }


# 固定的有界流程：生成检索计划 → 检索和候选初筛 → 分析；没有自主循环调用工具。
builder = StateGraph(State)
builder.add_node("search_plan", search_plan)
builder.add_node("retrieve", search_documents)
builder.add_node("analyze", analyze)
builder.add_edge(START, "search_plan")
builder.add_edge("search_plan", "retrieve")
builder.add_edge("retrieve", "analyze")
builder.add_edge("analyze", END)
graph = builder.compile()
