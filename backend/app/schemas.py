"""HTTP 输入、模型输出和任务响应契约；参数校验集中在这些入口模型中。"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


# 首次输入：去掉首尾空白，限制长度，拒绝额外字段和客户端自编历史。
class AnalysisRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    text: str = Field(min_length=1, max_length=1200)


# 一条用户信息：kind 区分陈述、否定、猜测和未知，quote 必须保留原话。
class Fact(BaseModel):
    kind: Literal["reported", "denied", "hypothesis", "uncertain"]
    quote: str = Field(min_length=1, description="从用户输入逐字摘录的原文，不得改写")
    meaning: str = Field(min_length=1, description="保留否定、时间和不确定性的中文解释")

    @field_validator("quote")
    @classmethod
    def check_quote(cls, quote: str, info: ValidationInfo):
        """旧格式输入在事实入口校验原文；编号回填使用已验证的数据构造。"""
        if info.context is not None and quote not in info.context["text"]:
            raise ValueError("事实引用必须来自用户原文")
        return quote


# 疾病相关方向及局限；source_ids 指向本次实际提供的参考片段，不代表确诊。
class Direction(BaseModel):
    name: str = Field(description="一个疾病或疾病类别的名称，不填检查、评估或生活建议")
    reason: str = Field(description="仅根据用户已报告的信息说明相关性，不做确定诊断")
    limitation: str = Field(description="仍缺少的依据，不推荐具体检查项目或治疗")
    source_ids: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="支持相关性说明的实际参考片段id；没有资料支持时为空，不得编造",
    )

    @field_validator("source_ids")
    @classmethod
    def check_source_ids(cls, source_ids: list[str], info: ValidationInfo):
        if info.context is not None and "allowed_source_ids" in info.context:
            if any(source_id not in info.context["allowed_source_ids"] for source_id in source_ids):
                raise ValueError("分析引用了未提供的参考片段")
        return source_ids


# 推理模型的结构化输出，也是即时症状规则返回结果的统一结构。
class Analysis(BaseModel):
    status: Literal[
        "needs_clarification",
        "analysis_available",
        "urgent_attention",
        "unable_to_analyze",
    ]
    summary: str
    facts: list[Fact] = Field(max_length=12)
    directions: list[Direction] = Field(max_length=3)
    missing_information: list[str] = Field(max_length=4)
    safety_notice: str

    @model_validator(mode="after")
    def check_consistency(self, info: ValidationInfo):
        """检查资料和分析状态约束；结构合法不等于医学结论正确。"""
        if info.context is not None:
            if (
                info.context.get("evidence_required")
                and not info.context["has_sources"]
                and self.directions
            ):
                raise ValueError("本地资料不足时不能生成无依据疾病方向")
        if self.status != "analysis_available" and self.directions:
            raise ValueError("仅可在信息支持分析时提供疾病方向")
        if self.status == "urgent_attention" and not self.safety_notice:
            raise ValueError("紧急状态必须提供就医提示")
        return self


# 完成后的分析结果：保留结果来源、模型名称、耗时和实际参考片段。
class AnalysisResponse(BaseModel):
    analysis: Analysis
    origin: Literal["model", "safety_rule"]
    elapsed_seconds: float
    model: str
    sources: list[dict] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    clues: list[Fact] = Field(default_factory=list)
    runtime_config: dict = Field(default_factory=dict)
    retrieval_audit: dict = Field(default_factory=dict)
    model_digests: dict = Field(default_factory=dict)


# 第一次模型调用的输出；数量由结构校验限制，语义策略由搜索提示词约束。
class SearchPlan(BaseModel):
    clues: list[Fact] = Field(default_factory=list, max_length=16)
    terms: list[str] = Field(
        max_length=16,
        description="去重后的英文阳性症状及异常检验线索，覆盖所有系统，不采纳未经证实的自我诊断",
    )
    queries: list[str] = Field(
        default_factory=list,
        max_length=2,
        description="最多两个互补的英文检索描述，综合跨系统线索，不预设疾病名称",
    )

    @model_validator(mode="after")
    def check_plan(self, info: ValidationInfo):
        if (
            info.context is not None
            and info.context.get("require_clues")
            and self.terms
            and not self.clues
        ):
            raise ValueError("有健康检索词时必须返回原话线索")
        self.terms = list(
            dict.fromkeys(
                " ".join(term.casefold().split()) for term in self.terms if term.strip()
            )
        )
        self.queries = list(
            dict.fromkeys(
                " ".join(query.casefold().split())
                for query in self.queries
                if query.strip()
            )
        )
        if len(self.queries) > 1 and self.terms:
            comprehensive = ", ".join(self.terms)
            self.queries = list(dict.fromkeys([comprehensive, self.queries[1]]))
        return self


# 候选初筛只返回列表编号；编号是否属于本次召回列表在 Agent 节点中检查。
class CandidateSelection(BaseModel):
    indexes: list[int] = Field(
        max_length=3,
        description="从提供的编号中选择最多3个相关候选，不得生成不存在的编号",
    )


# 排队、运行和终态共用的接口响应，前端依据 status 轮询并展示结果。
class JobResponse(BaseModel):
    id: str
    text: str
    status: Literal["queued", "running", "retrying", "succeeded", "failed"]
    attempts: int
    created_at: str
    result: AnalysisResponse | None = None
    error: str | None = None
    trace: list[str] = Field(default_factory=list)
    prompt_version: str | None = None
    display_text: str | None = None
    turn_action: str = "initial"
    progress: dict | None = None


# 单条补充或纠正；index 对应父轮问题，correct 时对应父轮事实。
class FollowUpRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: Literal["answer", "skip", "correct"]
    index: int = Field(ge=0)
    text: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_answer(self):
        """在输入边界拒绝空回答；明确跳过可以没有正文。"""
        if self.action != "skip" and not self.text:
            raise ValueError("回答或纠正不能为空；不清楚时可以跳过")
        return self


# 批量补充的一项，继承单条长度校验，但不允许夹带事实纠正操作。
class FollowUpAnswer(FollowUpRequest):
    action: Literal["answer", "skip"]


# 一批补充对应一个任务；空白项由前端省略，明确跳过的项目单独发送。
class FollowUpBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["batch"]
    items: list[FollowUpAnswer] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def check_items(self):
        """校验批内编号唯一，且跳过项目不能同时带有回答。"""
        if len({item.index for item in self.items}) != len(self.items):
            raise ValueError("同一问题不能在一批补充中重复提交")
        if any(item.action == "skip" and item.text for item in self.items):
            raise ValueError("跳过的项目不能同时提交回答")
        return self


class FactRef(BaseModel):
    """模型响应中的事实引用；原文由后端按编号恢复。"""

    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(description="选择 user_evidence 表中实际存在的编号")
    kind: Literal["reported", "denied", "hypothesis", "uncertain"]
    meaning: str = Field(min_length=1, description="保留否定、时间和不确定性的中文解释")

    @field_validator("evidence_id")
    @classmethod
    def check_evidence_id(cls, evidence_id: str, info: ValidationInfo):
        if evidence_id not in info.context["evidence_spans"]:
            raise ValueError("模型引用了无效的用户原话编号")
        return evidence_id


class SearchPlanOutput(SearchPlan):
    """模型输出的三个数组必填，复用检索计划的业务约束。"""

    model_config = ConfigDict(extra="forbid")
    clues: list[FactRef] = Field(max_length=16)
    queries: list[str] = Field(max_length=2, description="最多两个互补的英文检索描述")


class AnalysisOutput(Analysis):
    """模型响应入口复用分析状态约束，事实使用编号引用。"""

    model_config = ConfigDict(extra="forbid")
    facts: list[FactRef] = Field(max_length=12)


EVIDENCE_OUTPUTS = {SearchPlan: SearchPlanOutput, Analysis: AnalysisOutput}


def build_evidence(text: str) -> dict[str, str]:
    """每轮建立一次原话表，两个模型阶段共享同一份引用。"""
    return {
        f"u{index}": match.group()
        for index, match in enumerate(re.finditer(r"[^。！？\n]+[。！？]?", text), 1)
        if match.group().strip()
    }


def evidence_schema(model, spans: dict[str, str]) -> dict:
    """将本轮可选编号写入模型响应 Schema。"""
    schema = EVIDENCE_OUTPUTS[model].model_json_schema()
    schema["$defs"]["FactRef"]["properties"]["evidence_id"]["enum"] = list(spans)
    return schema


def parse_evidence_output(model, content, spans, context):
    """在响应入口校验结构、编号和业务约束一次，再组装对外结果。"""
    parsed = EVIDENCE_OUTPUTS[model].model_validate_json(
        content, context={**context, "evidence_spans": spans}
    )
    value = dict(parsed)
    field = "clues" if model is SearchPlan else "facts"
    value[field] = [
        Fact.model_construct(kind=fact.kind, quote=spans[fact.evidence_id], meaning=fact.meaning)
        for fact in value[field]
    ]
    # 字段及业务规则已由响应模型校验，回填原文后无需再次验证。
    return model.model_construct(**value)
