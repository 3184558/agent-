"""FastAPI 接口：接收输入、维护会话父链并调度任务，模型工作由 worker 执行。"""

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .config import settings
from .database import (
    documents,
    engine,
    get_job,
    get_turn,
    initialize,
    jobs,
    now,
    turns,
    reviews,
    attempt_runs,
)
from .safety import check_safety
from .schemas import AnalysisRequest, JobResponse, FollowUpRequest, FollowUpBatchRequest
import json
from .tasks import run_analysis


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时创建缺失表；不在 HTTP 进程内启动任务消费者。"""
    initialize()
    yield


app = FastAPI(title="拾言 · 健康信息整理", version="0.4.0", lifespan=lifespan)


@app.get("/api/health")
def health():
    """返回应用状态与已导入条目数；此接口不证明模型或 worker 可用。"""
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(documents))
    return {
        "status": "ok",
        "model": settings.model,
        "model_checked": False,
        "documents": count,
    }


@app.post("/api/analysis", response_model=JobResponse, status_code=202)
def analyze(payload: AnalysisRequest, response: Response):
    """创建首次分析任务；即时规则命中则直接返回，否则交给 Huey 排队。"""
    result = check_safety(payload.text)
    job = {
        "id": uuid4().hex,
        "text": payload.text,
        "status": "queued",
        "attempts": 0,
        "created_at": now(),
        "trace": [],
    }
    if result is not None:
        job.update(
            status="succeeded",
            result={
                "analysis": result.model_dump(),
                "origin": "safety_rule",
                "elapsed_seconds": 0,
                "model": settings.model,
                "sources": [],
            },
            trace=["即时症状规则提醒；未排队或调用模型"],
        )
        response.status_code = 200
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(**job))
    if result is None:
        run_analysis(job["id"])
    return job


@app.get("/api/tasks", response_model=list[JobResponse])
def history():
    """返回最近20轮记录，使用用户贡献摘要代替内部拼接的会话上下文。"""
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(jobs, turns.c.contribution)
                .outerjoin(turns, turns.c.job_id == jobs.c.id)
                .order_by(jobs.c.created_at.desc())
                .limit(20)
            )
            .mappings()
            .all()
        )
    history_items = []
    for row in rows:
        if row["contribution"]:
            display_text = row["contribution"]["text"]
            turn_action = row["contribution"]["action"]
        else:
            display_text = row["text"]
            turn_action = "initial"
        history_items.append(
            {**row, "display_text": display_text, "turn_action": turn_action}
        )
    return history_items


@app.get("/api/tasks/{job_id}", response_model=JobResponse)
def task_status(job_id: str):
    """供前端轮询单个任务；HTTP 成功后仍需根据任务 status 判断执行结果。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在。")
    return job


@app.delete("/api/tasks/{job_id}", status_code=204)
def delete_task(job_id: str):
    """删除一个终态叶子轮次，保留仍被后续会话引用的父记录。"""
    with engine.begin() as connection:
        row = connection.execute(
            select(jobs.c.status).where(jobs.c.id == job_id)
        ).first()
        if row is None:
            raise HTTPException(404, "任务不存在。")
        if row.status not in ("succeeded", "failed"):
            raise HTTPException(409, "任务正在排队或执行，完成后可删除。")
        child = connection.execute(
            select(turns.c.job_id).where(turns.c.parent_id == job_id)
        ).first()
        if child:
            raise HTTPException(409, "此轮仍有后续对话，请先从最新一轮开始删除。")
        connection.execute(attempt_runs.delete().where(attempt_runs.c.job_id == job_id))
        connection.execute(turns.delete().where(turns.c.job_id == job_id))
        connection.execute(jobs.delete().where(jobs.c.id == job_id))


@app.post("/api/tasks/{job_id}/follow-up", response_model=JobResponse, status_code=202)
def follow_up(
    job_id: str, payload: FollowUpRequest | FollowUpBatchRequest, response: Response
):
    """将单条或整批补充接到指定父轮次，只创建一个新任务和一条会话记录。

    问题原文从父轮次恢复；上下文保留问答关系，evidence 仅累积用户回答，
    防止模型把自己生成的问题或跳过标记作为用户原文引用。
    """
    parent = get_job(job_id)
    if parent is None:
        raise HTTPException(404, "任务不存在。")
    if parent["status"] != "succeeded":
        raise HTTPException(409, "请在分析成功后继续此轮对话。")
    # 输入模型负责字段校验，此处一次性检查编号与父轮次实际内容是否对应。
    analysis = parent["result"]["analysis"]
    if payload.action == "correct":
        choices = analysis["facts"]
    else:
        choices = analysis["missing_information"]
    if isinstance(payload, FollowUpBatchRequest):
        submitted = payload.items
    else:
        submitted = [payload]
    if any(item.index >= len(choices) for item in submitted):
        raise HTTPException(422, "问题或事实编号不存在，请刷新当前结果。")
    # 统一整理问题、回答和跳过状态；批量历史摘要与模型问答结构分别保存。
    entries = []
    for submitted_item in submitted:
        parent_choice = choices[submitted_item.index]
        if isinstance(parent_choice, str):
            question = parent_choice
        else:
            question = parent_choice["quote"]
        if submitted_item.action == "skip":
            answer = "用户跳过，仍未知"
        else:
            answer = submitted_item.text
        entry = {
            "action": submitted_item.action,
            "index": submitted_item.index,
            "question": question,
            "text": answer,
        }
        if submitted_item.action == "correct":
            # 同一句原话可能对应多项事实，保留用户选中的具体解释。
            entry["target_fact"] = parent_choice
        entries.append(entry)
    if payload.action == "batch":
        supplement = {"action": "batch", "items": entries}
        contribution = {
            **supplement,
            "text": "\n".join(
                f"{entry['question']}：{entry['text']}" for entry in entries
            ),
        }
    else:
        contribution = supplement = entries[0]
    # 跳过保持未知，不把问题、历史摘要或系统包装文字加入用户事实原文。
    answer_text = "\n".join(item.text for item in submitted if item.action != "skip")
    prior = get_turn(job_id)
    if prior:
        prior_evidence = prior["evidence_text"]
        root_id = prior["root_id"]
    else:
        prior_evidence = parent["text"]
        root_id = job_id
    evidence = prior_evidence + "\n" + answer_text
    context = (
        parent["text"]
        + "\n【本轮用户补充；纠正内容优先于此前对应陈述】\n"
        + json.dumps(supplement, ensure_ascii=False, separators=(",", ":"))
    )
    # 预算针对完整拼接上下文；超过后整批拒绝，不静默截断或部分保存。
    if len(context) > 3000:
        raise HTTPException(
            422, "当前会话已达到上下文预算，请新建分析；历史内容不会被静默截断。"
        )
    result = check_safety(answer_text)
    job = {
        "id": uuid4().hex,
        "text": context,
        "status": "queued",
        "attempts": 0,
        "created_at": now(),
        "trace": [],
    }
    if result:
        job.update(
            status="succeeded",
            result={
                "analysis": result.model_dump(),
                "origin": "safety_rule",
                "elapsed_seconds": 0,
                "model": settings.model,
                "sources": [],
            },
            trace=["补充内容触发即时提醒；未调用模型"],
        )
        response.status_code = 200
    # 任务与本轮贡献同事务落库，提交完成后才进入既有任务队列。
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(**job))
        connection.execute(
            turns.insert().values(
                job_id=job["id"],
                parent_id=job_id,
                root_id=root_id,
                contribution=contribution,
                evidence_text=evidence,
            )
        )
    if result is None:
        run_analysis(job["id"])
    return job


@app.get("/api/tasks/{job_id}/conversation")
def conversation(job_id: str):
    """沿 parent_id 回溯并按时间方向返回祖先链，不把其他分支混入会话。"""
    chain = []
    current = get_job(job_id)
    if current is None:
        raise HTTPException(404, "任务不存在。")
    while current:
        turn = get_turn(current["id"])
        if turn:
            contribution = turn["contribution"]
        else:
            contribution = {
                "action": "initial",
                "question": "最初描述",
                "text": current["text"],
            }
        chain.append(
            {
                "id": current["id"],
                "status": current["status"],
                "contribution": contribution,
            }
        )
        if turn:
            current = get_job(turn["parent_id"])
        else:
            current = None
    return list(reversed(chain))


@app.get("/api/knowledge/review")
def knowledge_review():
    """返回当前版本的来源核对范围及发布清单；核对数量不等于已发布数量。"""
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(documents))
        checked = (
            connection.execute(
                select(reviews)
                .join(documents, reviews.c.document_id == documents.c.id)
                .where(
                    reviews.c.status == "source_checked",
                    reviews.c.dataset_hash == documents.c.dataset_hash,
                )
            )
            .mappings()
            .all()
        )
    if settings.review_policy == "reviewed_only":
        family = settings.evidence_family
    else:
        family = "research_description"
    path = settings.data_dir / "indexes" / f"{family}.json"
    if path.exists():
        active = json.loads(path.read_text(encoding="utf-8"))
    else:
        active = None
    active_documents = 0
    if active:
        active_documents = active["documents"]
    return {
        "policy": settings.review_policy,
        "total_records": count,
        "source_checked": len(checked),
        "clinical_review": "pending",
        "active_family": family,
        "active_documents": active_documents,
        "active_index": active,
        "records": [dict(row) for row in checked],
    }


@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    """在 HTTP 边界统一解释输入结构错误，不在业务函数中重复捕获。"""
    return JSONResponse(
        status_code=422,
        content={
            "message": "输入不符合要求：首次描述为1～1200字；每条补充或纠正为1～500字；批量补充为1～4项，同一问题不能重复，跳过项不能同时填写回答。"
        },
    )


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    """将业务 HTTP 异常转成前端统一读取的 message 格式。"""
    return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})
