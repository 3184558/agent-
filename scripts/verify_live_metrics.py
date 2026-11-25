"""隔离验证流式进度：新合成短病例经真实 API/独立 Worker，并做极短 logprobs 开关对照。"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from math import isclose
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_patient_narratives as evaluator


def now():
    return datetime.now(timezone.utc).isoformat()


def append(path, value):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False) + "\n")


def observe_client(tasks, run_dir):
    """只在本次独立 Worker 中旁路保存原响应，返回给产品的 chunk 保持原样。"""
    original_client = tasks.create_client

    def create_client():
        client = original_client()
        original_chat = client.chat

        async def observed_chat(**request):
            started = perf_counter()
            fields = request["format"]["properties"]
            stage = "search_plan" if "terms" in fields else "rerank" if "indexes" in fields else "analysis"
            response = await original_chat(**request)
            if not request.get("stream"):
                append(run_dir / "model_calls.jsonl", {
                    "stage": stage, "request": request, "streaming": False,
                    "wall_seconds": perf_counter() - started,
                    "response": response.model_dump(mode="json"),
                })
                return response

            async def observed_stream():
                contents, thinking = [], []
                last = None
                chunks, token_records = 0, 0
                append(run_dir / "model_stream.jsonl", {
                    "event": "request", "observed_at": now(), "stage": stage, "request": request,
                })
                try:
                    async for chunk in response:
                        last = chunk
                        chunks += 1
                        token_records += len(chunk.logprobs or [])
                        contents.append(chunk.message.content or "")
                        thinking.append(chunk.message.thinking or "")
                        append(run_dir / "model_stream.jsonl", {
                            "event": "chunk", "observed_at": now(), "stage": stage,
                            "chunk": chunks, "wall_seconds": perf_counter() - started,
                            "response": chunk.model_dump(mode="json"),
                        })
                        yield chunk
                finally:
                    combined = None
                    if last is not None:
                        combined = last.model_dump(mode="json")
                        combined["message"]["content"] = "".join(contents)
                        combined["message"]["thinking"] = "".join(thinking)
                        combined["logprobs"] = None
                    append(run_dir / "model_calls.jsonl", {
                        "stage": stage, "observed_at": now(), "request": request,
                        "streaming": True, "chunks": chunks,
                        "received_token_records": token_records,
                        "complete": bool(last is not None and last.done),
                        "wall_seconds": perf_counter() - started, "response": combined,
                    })

            return observed_stream()

        client.chat = observed_chat
        return client

    tasks.create_client = create_client


def worker(run_dir):
    from huey.consumer import Consumer
    from app import database, tasks

    observe_client(tasks, run_dir)
    consumer = Consumer(tasks.queue, workers=1, worker_type="thread", periodic=False)
    consumer.start()
    sys.stdin.readline()
    consumer.stop(graceful=True)
    tasks.queue.storage.close()
    database.engine.dispose()


async def microprobe(run_dir):
    """仅比较同一极短合成 JSON 请求；两次固定顺序，不作为严格性能基准。"""
    from app.config import settings
    from app.ollama_client import create_client

    outcomes = []
    request = {
        "model": settings.model,
        "messages": [{"role": "user", "content": "只返回 ok 为 true 的 JSON 对象。"}],
        "format": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                   "required": ["ok"], "additionalProperties": False},
        "stream": True, "think": "medium",
        "options": {"temperature": 0, "seed": 42, "num_ctx": 32768, "num_predict": 128},
        "keep_alive": "10m", "top_logprobs": 0,
    }
    async with create_client() as client:
        for enabled in (False, True):
            label = "on" if enabled else "off"
            probe_request = {**request, "logprobs": enabled}
            path = run_dir / f"microprobe_{label}.jsonl"
            append(path, {"event": "request", "observed_at": now(), "request": probe_request})
            started = perf_counter()
            contents, thinking = [], []
            token_records, chunks = 0, 0
            first_chunk_seconds = None
            first_output_seconds = None
            last = None
            item = {"logprobs": enabled, "started_at": now(), "status": "running"}
            try:
                stream = await client.chat(**probe_request)
                async for chunk in stream:
                    last = chunk
                    chunks += 1
                    elapsed = perf_counter() - started
                    if chunks == 1:
                        first_chunk_seconds = elapsed
                    if first_output_seconds is None and (
                        chunk.message.content or chunk.message.thinking or chunk.logprobs
                    ):
                        first_output_seconds = elapsed
                    token_records += len(chunk.logprobs or [])
                    contents.append(chunk.message.content or "")
                    thinking.append(chunk.message.thinking or "")
                    append(path, {"event": "chunk", "observed_at": now(),
                                  "wall_seconds": perf_counter() - started,
                                  "response": chunk.model_dump(mode="json")})
                item["status"] = "complete" if last is not None and last.done else "missing_final_chunk"
                if item["status"] == "complete":
                    if last.done_reason == "length":
                        item.update(business_json_valid=False, business_json_status="generation_budget_exhausted")
                    else:
                        item["business_json_valid"] = json.loads("".join(contents)) == {"ok": True}
            except Exception as error:
                # 评测边界保留本次失败，再完成预定的另一组对照；不重试或调整参数。
                item.update(status="failed", error_type=type(error).__name__, error_message=str(error))
            item.update(wall_seconds=perf_counter() - started, chunks=chunks,
                        first_chunk_seconds=first_chunk_seconds, first_output_seconds=first_output_seconds,
                        received_token_records=token_records, content="".join(contents),
                        thinking_chars=len("".join(thinking)))
            if last is not None and last.done:
                item.update(official_output_tokens=last.eval_count, official_input_tokens=last.prompt_eval_count,
                            generation_seconds=(last.eval_duration or 0) / 1e9,
                            load_seconds=(last.load_duration or 0) / 1e9, done_reason=last.done_reason)
                if last.eval_duration:
                    item["official_average_tokens_per_second"] = last.eval_count * 1e9 / last.eval_duration
            append(path, {"event": "summary", "observed_at": now(), **item})
            outcomes.append(item)
            print(json.dumps({"microprobe": label, **item}, ensure_ascii=False), flush=True)
    evaluator.save_json(run_dir / "microprobe_metrics.json", {
        "outcomes": outcomes,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": "One request per setting, fixed off/on order, prompt-cache/load/order effects are uncontrolled; non-clinical smoke comparison, not a strict performance benchmark. Received logprob records need not equal official eval_count.",
    })
    return outcomes


def run(run_dir, source_data, observation_limit, sample_interval, skip_microprobe=False):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app import database, tasks
    from app.config import settings

    assert (settings.num_ctx, settings.plan_think, settings.rerank_think, settings.think) == (
        32768, "medium", "medium", "xhigh")
    assert (settings.plan_num_predict, settings.rerank_num_predict, settings.num_predict) == (16384, 4096, 16384)
    fixture = run_dir / "fixture"
    fixture.mkdir()
    case = {"case_id": "live-metrics-synthetic-001", "synthetic": True,
            "text": "我36岁，从昨晚开始嗓子痒，偶尔干咳，没有发烧和胸痛。"}
    evaluator.save_json(fixture / "case.json", case)
    evaluator.save_json(fixture / "protocol.json", {
        "purpose": "Verify live generation progress through real API routes and independent Worker",
        "synthetic": True, "new_case_created_before_output": True, "old_holdout_used": False,
        "medical_review": "not professionally reviewed; engineering verification only",
        "chain": "FastAPI TestClient routes -> isolated persistent queue -> independent single-thread Huey Worker -> real Ollama/retrieval -> isolated DB -> task polling API",
        "poll_seconds": sample_interval, "observation_limit_seconds": observation_limit,
        "microprobe": "Skipped; independent probe results are reported separately" if skip_microprobe else "Fixed medium/temp0/seed42/32k, max128 generated tokens, logprobs off then on; non-clinical and not a strict performance benchmark",
    })
    evaluator.CASES = fixture
    source_stats = {name: {"size": (source_data / name).stat().st_size,
                           "mtime_ns": (source_data / name).stat().st_mtime_ns}
                    for name in ("health.db", "queue.db")}
    evaluator.prepare(run_dir, source_data)
    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(chain="TestClient real API -> isolated queue -> independent Worker -> real Ollama/retrieval -> polling API",
                    validation_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    source_business_files_before=source_stats,
                    product_code_modified_for_run=False, frontend_verified=False)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshots = []
    started = perf_counter()
    with (run_dir / "worker.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__)), "--phase", "worker",
                                    "--run-dir", str(run_dir)], cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            with TestClient(__import__("app.main", fromlist=["app"]).app) as api:
                response = api.post("/api/analysis", json={"text": case["text"]})
                response.raise_for_status()
                job = response.json()
                evaluator.save_json(run_dir / "submission.json", {
                    "observed_at": now(), "http_status": response.status_code,
                    "request": {"text": case["text"]}, "response": job, "worker_pid": process.pid,
                })
                previous_state = None
                while True:
                    snapshot = {"observed_at": now(), "wall_seconds": perf_counter() - started, "job": job}
                    append(run_dir / "progress_samples.jsonl", snapshot)
                    snapshots.append(snapshot)
                    progress = job.get("progress") or {}
                    stages = progress.get("stages", {})
                    state = (job["status"], [(name, stage["status"], stage["phase"]) for name, stage in stages.items()])
                    if state != previous_state:
                        print(json.dumps({"job_status": job["status"], "stages": stages,
                                          "wall_seconds": snapshot["wall_seconds"]}, ensure_ascii=False), flush=True)
                        previous_state = state
                    if job["status"] in ("succeeded", "failed") or perf_counter() - started >= observation_limit:
                        break
                    sleep(sample_interval)
                    response = api.get(f"/api/tasks/{job['id']}")
                    response.raise_for_status()
                    job = response.json()
        finally:
            process.communicate("stop\n")

    with database.engine.connect() as connection:
        attempts = [dict(row) for row in connection.execute(
            select(database.attempt_runs).where(database.attempt_runs.c.job_id == job["id"])
        ).mappings()]
    evaluator.save_json(run_dir / "job_result.json", {"job": job, "attempts": attempts})
    calls_path = run_dir / "model_calls.jsonl"
    calls = evaluator.read_jsonl(calls_path) if calls_path.exists() else []
    official_calls = []
    for call in calls:
        response = call["response"] or {}
        official_calls.append({
            "stage": call["stage"], "streaming": call["streaming"],
            "complete": bool(response.get("done")), "output_tokens": response.get("eval_count"),
            "input_tokens": response.get("prompt_eval_count"), "eval_duration": response.get("eval_duration"),
            "done_reason": response.get("done_reason"), "received_token_records": call.get("received_token_records"),
            "wall_seconds": call["wall_seconds"], "request_think": call["request"].get("think"),
            "request_logprobs": call["request"].get("logprobs"), "options": call["request"]["options"],
        })
    running = [sample for sample in snapshots if sample["job"]["status"] == "running"]
    with_progress = [sample for sample in running if sample["job"].get("progress")]
    estimated = [sample for sample in with_progress if any(
        stage["is_estimate"] and stage["tokens_per_second"] is not None
        for stage in sample["job"]["progress"]["stages"].values()
    )]
    # 逐阶段核对完成后的面板值，不能只凭“有进度”就判验收通过。
    final_stages = (job.get("progress") or {}).get("stages", {})
    reconciliation = []
    for stage_name in ("search_plan", "rerank", "analysis"):
        group = [call for call in official_calls if call["stage"] == stage_name]
        usage_complete = bool(group) and all(
            isinstance(call["input_tokens"], int) and isinstance(call["output_tokens"], int)
            and isinstance(call["eval_duration"], int) and call["eval_duration"] > 0
            for call in group
        )
        official_input = sum(call["input_tokens"] for call in group) if usage_complete else None
        official_output = sum(call["output_tokens"] for call in group) if usage_complete else None
        official_rate = (official_output * 1e9 / sum(call["eval_duration"] for call in group)
                         if usage_complete else None)
        displayed = final_stages.get(stage_name, {})
        displayed_rate = displayed.get("tokens_per_second")
        matched = (usage_complete and displayed.get("status") == "completed"
                   and displayed.get("is_estimate") is False
                   and displayed.get("input_tokens") == official_input
                   and displayed.get("output_tokens") == official_output
                   and isinstance(displayed_rate, (int, float))
                   and isclose(displayed_rate, official_rate, rel_tol=1e-9, abs_tol=1e-9))
        reconciliation.append({"stage": stage_name, "passed": matched,
                               "official_input_tokens": official_input,
                               "official_output_tokens": official_output,
                               "official_average_tokens_per_second": official_rate,
                               "displayed": displayed})
    probes = [] if skip_microprobe else asyncio.run(microprobe(run_dir))
    after = {name: {"size": (source_data / name).stat().st_size,
                    "mtime_ns": (source_data / name).stat().st_mtime_ns} for name in source_stats}
    passed = (job["status"] == "succeeded" and job["result"]["origin"] == "model"
              and bool(with_progress) and bool(estimated) and process.returncode == 0
              and all(call["streaming"] and call["complete"] for call in official_calls)
              and all(check["passed"] for check in reconciliation))
    summary = {
        "engineering_pass": passed, "job_status": job["status"], "worker_exit_code": process.returncode,
        "wall_seconds": snapshots[-1]["wall_seconds"], "samples": len(snapshots),
        "running_samples": len(running), "running_samples_with_progress": len(with_progress),
        "samples_with_live_estimated_rate": len(estimated), "official_calls": official_calls,
        "stage_reconciliation": reconciliation,
        "source_business_files_stat_unchanged": source_stats == after,
        "source_business_files_before": source_stats, "source_business_files_after": after,
        "microprobe_completed": [item["status"] for item in probes],
        "microprobe_skipped": skip_microprobe,
        "limits": "Engineering smoke verification only. No browser rendering or clinical judgment was validated; TestClient exercises application routes without a listening HTTP server. Source business rows were never selected or copied.",
    }
    evaluator.save_json(run_dir / "verification_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    tasks.queue.storage.close()
    database.engine.dispose()
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("run", "worker", "probe"), default="run")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-data", type=Path, default=ROOT / "data")
    parser.add_argument("--observation-limit", type=float, default=1800)
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--skip-microprobe", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    allowed = (ROOT / "evaluation/runs/live_metrics").resolve()
    assert run_dir.is_relative_to(allowed) and run_dir != allowed
    assert args.observation_limit > 0 and args.sample_interval > 0
    if args.phase in ("run", "probe"):
        run_dir.mkdir(parents=True, exist_ok=False)
    else:
        assert run_dir.is_dir()
    os.environ["HEALTH_DATA_DIR"] = str(run_dir / "data")
    sys.path.insert(0, str(ROOT / "backend"))
    if args.phase == "worker":
        worker(run_dir)
    elif args.phase == "probe":
        asyncio.run(microprobe(run_dir))
    else:
        raise SystemExit(0 if run(run_dir, args.source_data.resolve(), args.observation_limit, args.sample_interval, args.skip_microprobe) else 1)


if __name__ == "__main__":
    main()
