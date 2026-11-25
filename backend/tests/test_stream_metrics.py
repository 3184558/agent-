"""流式模型指标经过任务记录和HTTP查询的行为验证。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import httpx
import pytest
from ollama import AsyncClient
from sqlalchemy import select

from app import database, tasks
from app.config import settings
from app.ollama_client import progress_sink
from app.rerank import rerank_candidates
from test_api import RESULT, env, install_transport, success


def chunk(*, content="", thinking="", tokens=0, **usage):
    return {"message": {"role": "assistant", "content": content, "thinking": thinking},
            "done": False,
            "logprobs": [{"token": "x", "logprob": -0.1} for _ in range(tokens)], **usage}


def ndjson(frames):
    return httpx.Response(200, content="\n".join(json.dumps(frame) for frame in frames) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})


def test_live_counts_are_token_records_and_final_usage_replaces_estimate(env):
    api, queue, patch = env
    sampled = threading.Event()
    release = threading.Event()

    class GatedPlan(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (json.dumps(chunk(thinking="这不是JSON", tokens=3)) + "\n").encode()
            await asyncio.sleep(1.05)
            yield (json.dumps(chunk(thinking="仍在思考", tokens=2)) + "\n").encode()
            sampled.set()
            await asyncio.to_thread(release.wait)
            body = json.dumps({"terms": ["dizziness"], "queries": ["dizziness"], "clues": []})
            split = len(body) // 2
            yield (json.dumps(chunk(content=body[:split], tokens=3)) + "\n").encode()
            yield (json.dumps(chunk(content=body[split:], tokens=4)) + "\n").encode()
            yield (json.dumps(chunk(done=True, prompt_eval_count=42, eval_count=17,
                                    eval_duration=2_000_000_000, load_duration=0,
                                    done_reason="stop")) + "\n").encode()

    def upstream(request):
        body = json.loads(request.content)
        assert body["stream"] is True and body["logprobs"] is True
        assert body["top_logprobs"] == 0
        if "terms" in body["format"]["properties"]:
            return httpx.Response(200, stream=GatedPlan(), headers={"Content-Type": "application/x-ndjson"})
        return success(request)

    install_transport(patch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    with ThreadPoolExecutor(1) as worker:
        future = worker.submit(queue.execute, queue.dequeue())
        try:
            assert sampled.wait(5), "模型分片未被消费"
            live = api.get(f'/api/tasks/{job["id"]}').json()
            stage = live["progress"]["stages"]["search_plan"]
            assert live["status"] == "running" and live["result"] is None
            assert stage["phase"] == "thinking" and stage["is_estimate"] is True
            assert stage["output_tokens"] == 5  # 两个分片中包含五条token记录。
            assert stage["tokens_per_second"] > 0
        finally:
            release.set()
        future.result(timeout=10)

    final = api.get(f'/api/tasks/{job["id"]}').json()
    stage = final["progress"]["stages"]["search_plan"]
    assert final["status"] == "succeeded"
    assert final["result"]["analysis"]["facts"][0]["quote"] == "头晕"
    assert stage["status"] == "completed" and stage["is_estimate"] is False
    assert (stage["input_tokens"], stage["output_tokens"]) == (42, 17)
    assert stage["tokens_per_second"] == pytest.approx(8.5)
    assert stage["elapsed_seconds"] >= 1
    assert final["progress"]["execution_seconds"] >= stage["elapsed_seconds"]
    assert final["progress"]["retrieval_seconds"] >= 0
    assert final["progress"]["stages"]["rerank"]["status"] == "skipped"
    assert "这不是JSON" not in json.dumps(final["progress"], ensure_ascii=False)


def test_missing_terminal_frame_fails_and_keeps_partial_metrics(env):
    api, queue, patch = env

    def upstream(request):
        if "terms" in json.loads(request.content)["format"]["properties"]:
            return success(request)
        return ndjson([chunk(thinking="只有部分思考", tokens=5)])

    install_transport(patch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    final = api.get(f'/api/tasks/{job["id"]}').json()
    stage = final["progress"]["stages"]["analysis"]
    assert final["status"] == "failed" and final["attempts"] == 1
    assert "ValueError" in final["error"]
    assert stage["status"] == "failed" and stage["is_estimate"] is True
    assert stage["output_tokens"] == 5
    assert final["progress"]["stages"]["search_plan"]["status"] == "completed"
    assert queue.pending_count() == 0


def test_invalid_business_json_keeps_precise_usage(env):
    api, queue, patch = env

    def upstream(request):
        if "terms" in json.loads(request.content)["format"]["properties"]:
            return success(request)
        return ndjson([chunk(content="bad JSON", tokens=2),
                       chunk(done=True, prompt_eval_count=31, eval_count=29,
                             eval_duration=2_000_000_000, done_reason="stop")])

    install_transport(patch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    final = api.get(f'/api/tasks/{job["id"]}').json()
    stage = final["progress"]["stages"]["analysis"]
    assert final["status"] == "failed"
    assert stage["status"] == "failed" and stage["is_estimate"] is False
    assert (stage["input_tokens"], stage["output_tokens"]) == (31, 29)
    assert stage["tokens_per_second"] == pytest.approx(14.5)


def test_retry_retains_prior_attempt_and_starts_new_counts(env):
    api, queue, patch = env

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (json.dumps(chunk(thinking="开始思考", tokens=2)) + "\n").encode()
            # 同阶段不足一秒的后续分片，仍应由异常边界保留最新计数。
            yield (json.dumps(chunk(thinking="尚未完成", tokens=3)) + "\n").encode()
            raise httpx.ReadTimeout("stream interrupted")

    def upstream(request):
        if "terms" in json.loads(request.content)["format"]["properties"]:
            return success(request)
        return httpx.Response(200, stream=Interrupted(), headers={"Content-Type": "application/x-ndjson"})

    install_transport(patch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    assert database.get_job(job["id"])["status"] == "retrying"
    install_transport(patch, success)
    tasks.run_analysis.call_local(job["id"])
    with database.engine.connect() as connection:
        attempts = connection.execute(select(database.attempt_runs).where(
            database.attempt_runs.c.job_id == job["id"]).order_by(database.attempt_runs.c.attempt)).mappings().all()
    assert [row["status"] for row in attempts] == ["failed", "succeeded"]
    first = attempts[0]["metrics"]["progress"]
    second = attempts[1]["metrics"]["progress"]
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert first["stages"]["analysis"]["output_tokens"] == 5
    assert first["stages"]["analysis"]["is_estimate"] is True
    assert second["stages"]["analysis"]["output_tokens"] == 7
    assert second["stages"]["analysis"]["is_estimate"] is False


def test_body_rerank_aggregates_usage_across_batches(env):
    _, _, patch = env
    patch.setattr(settings, "reranker", "body_llm")
    patch.setattr(settings, "rerank_batch_size", 1)
    requests = []
    progress = []

    def upstream(request):
        body = json.loads(request.content)
        candidates = json.loads(body["messages"][1]["content"])["candidates"]
        requests.append(candidates)
        return ndjson([chunk(content=json.dumps({"indexes": [candidates[0]["index"]]}),
                             done=True, prompt_eval_count=10, eval_count=20,
                             eval_duration=1_000_000_000, done_reason="stop")])

    candidates = [{"id": str(index), "name": f"候选{index}", "snippet": "实际正文",
                   "parent_document_id": str(index)} for index in range(3)]

    async def run():
        async with AsyncClient(host="http://model.test", transport=httpx.MockTransport(upstream), trust_env=False) as client:
            return await rerank_candidates("头晕", candidates, client, "选择候选")

    token = progress_sink.set(lambda stage, values: progress.append((stage, values)))
    try:
        sources, metrics = asyncio.run(run())
    finally:
        progress_sink.reset(token)
    assert len(requests) == 3 and [source["id"] for source in sources] == ["0", "1", "2"]
    generation = metrics["rerank_generation"]
    assert (generation["prompt_tokens"], generation["output_tokens"]) == (30, 60)
    assert generation["generation_seconds"] == 3
    completed_calls = [values for stage, values in progress if values.get("status") == "validating"]
    assert [values["input_tokens"] for values in completed_calls] == [10, 20, 30]
    assert [values["output_tokens"] for values in completed_calls] == [20, 40, 60]
    assert [values["tokens_per_second"] for values in completed_calls] == [20, 20, 20]


def test_legacy_and_rule_records_have_no_fabricated_progress(env):
    api, _, _ = env
    rule = api.post("/api/analysis", json={"text": "现在严重胸痛"}).json()
    assert rule["result"]["origin"] == "safety_rule"
    assert rule["progress"] is None
    # 模拟旧版本已有完整业务结果和指标，但未记录progress的历史尝试。
    legacy = api.post("/api/analysis", json={"text": "头晕"}).json()
    database.update_job(
        legacy["id"], status="succeeded", attempts=1,
        result={"analysis": RESULT, "origin": "model", "elapsed_seconds": 1,
                "model": "legacy-model", "sources": [],
                "metrics": {"generation": {"output_tokens": 21}}},
    )
    with database.engine.begin() as connection:
        connection.execute(database.attempt_runs.insert().values(
            job_id=legacy["id"], attempt=1, status="succeeded", metrics={"generation": {"output_tokens": 21}}))
    history = api.get(f'/api/tasks/{legacy["id"]}').json()
    assert history["progress"] is None
    assert history["result"]["metrics"]["generation"]["output_tokens"] == 21
