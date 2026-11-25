"""任务边界负责一次捕获、有限重试和可查询的失败状态。"""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter

import httpx
import ollama
from filelock import FileLock
from huey import SqliteHuey
from langsmith import tracing_context
from pydantic import ValidationError

from .agent import PROMPT_VERSION, graph
from .config import settings
from .database import get_job, get_turn, update_job
from . import database
from .ollama_client import create_client, progress_sink

# Huey 持久保存待执行任务；结果统一写入 health.db，由 HTTP 接口查询。
queue = SqliteHuey(
    "health", filename=str(settings.data_dir / "queue.db"), results=False
)


_active_job = ContextVar("health_active_job", default=None)


async def execute(text: str, evidence_text: str | None = None):
    """管理一次工作流的本地客户端和追踪开关，返回 LangGraph 最终状态。

    此处不捕获模型或校验异常，由外层 run_analysis 统一记录执行结果。
    """
    # 即使本机设置过 LangSmith 环境变量，此流程也不上传健康描述。
    with tracing_context(enabled=False):
        async with create_client() as client:
            if evidence_text is None:
                user_evidence = text
            else:
                user_evidence = evidence_text
            state = {
                "text": text,
                "evidence_text": user_evidence,
                "trace": [],
            }
            labels = {
                "search_plan": "正在检索本地资料与筛选正文",
                "retrieve": "正在生成分析并核对引用",
                "analyze": "分析完成",
            }
            async for changes in graph.astream(
                state,
                config={"configurable": {"client": client}, "callbacks": []},
                stream_mode="updates",
            ):
                for node, values in changes.items():
                    state.update(values)
                    active_job = _active_job.get()
                    if not active_job:
                        continue
                    job_id, attempt_metrics = active_job
                    attempt_metrics.update(state.get("metrics", {}))
                    update_job(job_id, trace=state["trace"] + [labels[node]])
            state["model_digests"] = getattr(client, "health_model_digests", {})
            return state


@queue.task(retries=0)
def run_analysis(job_id: str):
    """消费一个任务，维护状态、有限重试和最终结果；复用既有本机文件锁。

    attempts 统计整轮工作流执行次数。仅既定暂时性连接或模型服务错误重试，
    格式及引用校验失败直接记为失败，Huey 自身的自动重试在装饰器中关闭。
    """
    # 同机进程共享文件锁；获取后再读状态，避免重复投递改写已完成结果。
    waiting = perf_counter()
    with FileLock(settings.data_dir / "ollama.lock"):
        job = get_job(job_id)
        if job is None or job["status"] in ("succeeded", "failed"):
            return
        lock_seconds = perf_counter() - waiting
        attempt = job["attempts"] + 1
        since_created_seconds = (
            datetime.now(timezone.utc) - datetime.fromisoformat(job["created_at"])
        ).total_seconds()
        queue_before_lock_seconds = None
        if attempt == 1:
            queue_before_lock_seconds = (
                datetime.now(timezone.utc) - datetime.fromisoformat(job["created_at"])
            ).total_seconds() - lock_seconds
        metrics = {
            "lock_wait_seconds": lock_seconds,
            "since_created_seconds": since_created_seconds,
            "queue_before_lock_seconds": queue_before_lock_seconds,
        }
        started = perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()
        progress = {
            "attempt": attempt, "started_at": timestamp, "updated_at": timestamp,
            "execution_seconds": 0, "retrieval_seconds": None,
            "queue_wait_seconds": queue_before_lock_seconds,
            "stages": {
                name: {"status": "waiting", "phase": "waiting", "input_tokens": None,
                       "output_tokens": None, "tokens_per_second": None,
                       "is_estimate": True, "elapsed_seconds": 0}
                for name in ("search_plan", "rerank", "analysis")
            },
        }
        metrics["progress"] = progress
        stage_starts = {}
        last_persisted = started

        def record_progress(stage, values):
            """片段只更新内存；每秒及阶段切换时把快照写入当前尝试。"""
            nonlocal last_persisted
            current = perf_counter()
            if stage is None:
                progress.update(values)
                changed = True
            else:
                row = progress["stages"][stage]
                changed = any(row[key] != values[key] for key in ("status", "phase") if key in values)
                stage_starts.setdefault(stage, current)
                row.update(values)
                row["elapsed_seconds"] = round(current - stage_starts[stage], 3)
            progress["execution_seconds"] = round(current - started, 3)
            progress["updated_at"] = datetime.now(timezone.utc).isoformat()
            if changed or current - last_persisted >= 1:
                with database.engine.begin() as connection:
                    connection.execute(
                        database.attempt_runs.update().where(
                            database.attempt_runs.c.job_id == job_id,
                            database.attempt_runs.c.attempt == attempt,
                        ).values(metrics=metrics)
                    )
                last_persisted = current

        with database.engine.begin() as connection:
            connection.execute(database.attempt_runs.insert().values(
                job_id=job_id, attempt=attempt, metrics=metrics, status="running",
            ))
        token = _active_job.set((job_id, metrics))
        progress_token = progress_sink.set(record_progress)
        update_job(
            job_id,
            status="running",
            attempts=attempt,
            error=None,
            prompt_version=PROMPT_VERSION,
            trace=["正在整理原话线索和检索计划"],
        )
        try:
            turn = get_turn(job_id)
            if turn:
                state = asyncio.run(execute(job["text"], turn["evidence_text"]))
            else:
                state = asyncio.run(execute(job["text"]))
        except Exception as exc:
            metrics["execution_seconds"] = perf_counter() - started
            for name, row in progress["stages"].items():
                if row["status"] in ("running", "validating"):
                    record_progress(name, {"status": "failed", "phase": "failed"})
            progress["execution_seconds"] = metrics["execution_seconds"]
            progress["updated_at"] = datetime.now(timezone.utc).isoformat()
            metrics["error_type"] = type(exc).__name__
            if isinstance(exc, ValidationError):
                metrics["validation_errors"] = [
                    {"field": list(error["loc"]), "type": error["type"]}
                    for error in exc.errors(include_input=False, include_url=False)
                ]
            stage = (
                "分析结果生成与校验" if "retrieval_seconds" in metrics
                else "资料检索与筛选" if "plan_seconds" in metrics
                else "检索计划生成与校验"
            )
            metrics["error_stage"] = stage
            with database.engine.begin() as connection:
                connection.execute(
                    database.attempt_runs.update().where(
                        database.attempt_runs.c.job_id == job_id,
                        database.attempt_runs.c.attempt == attempt,
                    ).values(metrics=metrics, status="failed")
                )
            transient = isinstance(
                exc, (httpx.TimeoutException, httpx.ConnectError, ConnectionError)
            ) or (
                isinstance(exc, ollama.ResponseError)
                and exc.status_code in (429, 500, 502, 503, 504)
            )
            if transient and attempt < settings.max_attempts:
                update_job(
                    job_id,
                    status="retrying",
                    error="模型暂时不可用，等待自动重试。",
                    trace=[f"第 {attempt} 次调用遇到暂时性故障"],
                )
                run_analysis.schedule(
                    (job_id,), delay=settings.retry_delay * 2 ** (attempt - 1)
                )
            else:
                update_job(
                    job_id,
                    status="failed",
                    error=f"{stage}失败（{type(exc).__name__}），本次输入已保留。",
                    trace=[f"第 {attempt} 次执行失败；未生成分析结果"],
                )
            return
        finally:
            progress_sink.reset(progress_token)
            _active_job.reset(token)
        # 耗时只统计本次成功执行，不包含排队、退避和前面失败的尝试。
        elapsed = round(perf_counter() - started, 3)
        metrics.update(state.get("metrics", {}))
        metrics["execution_seconds"] = elapsed
        progress["execution_seconds"] = elapsed
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        with database.engine.begin() as connection:
            connection.execute(
                database.attempt_runs.update().where(
                    database.attempt_runs.c.job_id == job_id,
                    database.attempt_runs.c.attempt == attempt,
                ).values(metrics=metrics, status="succeeded")
            )
        result = {
            "model_digests": state.get("model_digests", {}),
            "retrieval_audit": state.get("retrieval_audit", {}),
            "metrics": metrics,
            "clues": state.get("clues", []),
            "runtime_config": settings.model_dump(mode="json"),
            "analysis": state["analysis"],
            "origin": "model",
            "elapsed_seconds": elapsed,
            "model": settings.model,
            "sources": state["sources"],
        }
        update_job(
            job_id,
            status="succeeded",
            result=result,
            elapsed_seconds=elapsed,
            trace=state["trace"],
        )
