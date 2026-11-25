"""只读提取指定补充案例，隔离重放真实工作流并保存校验前模型响应。"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_patient_narratives as evaluator


def save(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def append(path, value):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failed-job", required=True)
    parser.add_argument("--short-job", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--num-ctx", type=int, help="显式指定对照上下文；省略时复用原任务配置")
    parser.add_argument("--num-predict", type=int)
    parser.add_argument("--plan-num-predict", type=int)
    parser.add_argument("--rerank-num-predict", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--think", choices=("off", "low", "medium", "xhigh"))
    parser.add_argument("--plan-think", choices=("off", "low", "medium", "xhigh"))
    parser.add_argument("--rerank-think", choices=("off", "low", "medium", "xhigh"))
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    allowed = (ROOT / "evaluation/runs/followup_validation").resolve()
    assert run_dir.is_relative_to(allowed) and run_dir != allowed
    source_data = ROOT / "data"
    source_stats = {name: {"size": (source_data / name).stat().st_size,
                           "mtime_ns": (source_data / name).stat().st_mtime_ns}
                    for name in ("health.db", "queue.db")}
    snapshots = {}
    with sqlite3.connect(f"{(source_data / 'health.db').as_uri()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        for label, job_id in (("long", args.failed_job), ("short", args.short_job)):
            job = dict(source.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone())
            turn = dict(source.execute("SELECT * FROM conversation_turns WHERE job_id=?", (job_id,)).fetchone())
            turn["contribution"] = json.loads(turn["contribution"])
            job["result"] = json.loads(job["result"]) if job["result"] else None
            snapshots[label] = {"job": job, "turn": turn}
        assert snapshots["long"]["job"]["status"] == "failed"
        assert snapshots["short"]["job"]["status"] == "succeeded"
        assert snapshots["long"]["turn"]["parent_id"] == snapshots["short"]["turn"]["parent_id"]
        parent_id = snapshots["long"]["turn"]["parent_id"]
        parent = dict(source.execute("SELECT text,result FROM analysis_jobs WHERE id=?", (parent_id,)).fetchone())
        parent["result"] = json.loads(parent["result"])
        attempts = [dict(row) for row in source.execute("SELECT attempt,status,metrics FROM analysis_attempt_runs WHERE job_id=?", (args.failed_job,))]
        for attempt in attempts:
            attempt["metrics"] = json.loads(attempt["metrics"])
    source_config = parent["result"]["runtime_config"]
    # 旧任务尚未记录这些字段；这些是该基线源码当时的明确取值。
    legacy_fields = {key: value for key, value in
                     {"think": False, "plan_think": False, "rerank_think": False,
                      "plan_num_predict": 1200, "rerank_num_predict": 128}.items()
                     if key not in source_config}
    original_config = {**legacy_fields, **source_config}
    replay_config = dict(original_config)
    for key in ("num_ctx", "num_predict", "plan_num_predict", "rerank_num_predict", "timeout", "think", "plan_think", "rerank_think"):
        value = getattr(args, key)
        if value is not None:
            replay_config[key] = False if key.endswith("think") and value == "off" else value
    config_changes = {key: {"from": original_config[key], "to": value}
                      for key, value in replay_config.items() if original_config[key] != value}
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture = run_dir / "fixture"
    fixture.mkdir()
    save(fixture / "protocol.json", {
        "purpose": "specific user-supplied follow-up JSON failure diagnosis",
        "synthetic_status": "not independently established; user supplied test case",
        "original_initial_chars": len(parent["text"]),
        "source_failed_context_chars": len(snapshots["long"]["job"]["text"]),
        "original_failed_attempts": attempts,
        "variants": ["original failed long supplement", "existing successful short sibling"],
        "comparison_limit": "short sibling changes content as well as length; not a pure length experiment",
        "path": "real tasks.execute workflow; diagnostic replay, not a fresh API/Worker acceptance test",
        "config_changes_from_source_job": config_changes,
        "legacy_runtime_fields_from_baseline_code": legacy_fields,
        "product_logic_modified_for_replay": False,
    })
    save(fixture / "source_case.json", {"parent": parent, "snapshots": snapshots})
    os.environ["HEALTH_DATA_DIR"] = str(run_dir / "data")
    for key in ("num_ctx", "num_predict", "plan_num_predict", "rerank_num_predict", "timeout", "think", "plan_think", "rerank_think"):
        value = replay_config[key]
        os.environ[f"HEALTH_{key.upper()}"] = json.dumps(value) if isinstance(value, bool) else str(value)
    sys.path.insert(0, str(ROOT / "backend"))
    evaluator.CASES = fixture
    evaluator.prepare(run_dir, source_data)
    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["chain"] = "real tasks.execute -> LangGraph -> real Ollama/retrieval; diagnostic replay, no API/independent Worker"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    from app import tasks, database
    from app.config import settings
    from pydantic import ValidationError
    actual_config = settings.model_dump(mode="json")
    excluded = {"data_dir", "ollama_url"}
    assert {k: v for k, v in actual_config.items() if k not in excluded} == {k: v for k, v in replay_config.items() if k not in excluded}
    original_client = tasks.create_client
    current_label = ""

    def observed_client():
        # 只记录本次隔离重放：调用原始客户端并原样返回真实响应。
        client = original_client()
        original_chat = client.chat

        async def observed_chat(**request):
            started = perf_counter()
            response = await original_chat(**request)
            fields = request["format"]["properties"]
            stage = "search_plan" if "terms" in fields else "rerank" if "indexes" in fields else "analysis"
            if request.get("stream"):
                async def observed_stream():
                    contents, thinking = [], []
                    last = None
                    chunks, token_records = 0, 0
                    append(run_dir / "raw_model_stream.jsonl", {
                        "event": "request", "variant": current_label, "stage": stage,
                        "observed_at": datetime.now(timezone.utc).isoformat(), "request": request,
                    })
                    try:
                        async for chunk in response:
                            last = chunk
                            chunks += 1
                            token_records += len(chunk.logprobs or [])
                            contents.append(chunk.message.content or "")
                            thinking.append(chunk.message.thinking or "")
                            append(run_dir / "raw_model_stream.jsonl", {
                                "event": "chunk", "variant": current_label, "stage": stage,
                                "chunk": chunks, "wall_seconds": perf_counter() - started,
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                                "response": chunk.model_dump(mode="json"),
                            })
                            yield chunk
                    finally:
                        summary = {
                            "variant": current_label, "stage": stage,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                            "wall_seconds": perf_counter() - started, "request": request,
                            "streaming": True, "chunks": chunks,
                            "received_token_records": token_records,
                            "complete": bool(last is not None and last.done),
                            "response": None,
                        }
                        if last is not None:
                            combined = last.model_dump(mode="json")
                            combined["message"]["content"] = "".join(contents)
                            combined["message"]["thinking"] = "".join(thinking)
                            # 逐 token 概率只保留在原始流文件，避免重复保存整段记录。
                            combined["logprobs"] = None
                            summary["response"] = combined
                        append(run_dir / "raw_model_responses.jsonl", summary)

                return observed_stream()
            append(run_dir / "raw_model_responses.jsonl", {
                "variant": current_label, "stage": stage,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "wall_seconds": perf_counter() - started,
                "request": request, "response": response.model_dump(mode="json"),
            })
            return response

        client.chat = observed_chat
        return client

    tasks.create_client = observed_client
    outcomes = []
    try:
        for label, snapshot in snapshots.items():
            current_label = label
            started = perf_counter()
            outcome = {"variant": label, "started_at": datetime.now(timezone.utc).isoformat(),
                       "context_chars": len(snapshot["job"]["text"]),
                       "evidence_chars": len(snapshot["turn"]["evidence_text"]),
                       "answer_chars": [len(item["text"]) for item in snapshot["turn"]["contribution"]["items"]]}
            try:
                state = asyncio.run(tasks.execute(snapshot["job"]["text"], snapshot["turn"]["evidence_text"]))
                outcome.update(status="succeeded", result=state)
            except Exception as error:
                # 唯一诊断边界保留失败，再执行预定的短回答对照；不重试。
                outcome.update(status="failed", error_type=type(error).__name__, error_message=str(error))
                if isinstance(error, ValidationError):
                    outcome["validation_errors"] = [{"field": list(item["loc"]), "type": item["type"], "message": item["msg"]}
                                                    for item in error.errors(include_input=False, include_url=False)]
            outcome["wall_seconds"] = perf_counter() - started
            append(run_dir / "replay_results.jsonl", outcome)
            outcomes.append(outcome)
            print(json.dumps({k: outcome.get(k) for k in ("variant", "status", "wall_seconds", "validation_errors")}, ensure_ascii=False), flush=True)
    finally:
        tasks.create_client = original_client
        database.engine.dispose()
        tasks.queue.storage.close()
    generations = []
    for line in (run_dir / "raw_model_responses.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        response = record["response"]
        if response is None:
            generations.append({"variant": record["variant"], "stage": record["stage"],
                                "raw_response_received": False, "complete": False})
            continue
        content = response["message"]["content"]
        item = {"variant": record["variant"], "stage": record["stage"],
                "prompt_tokens": response.get("prompt_eval_count"), "output_tokens": response.get("eval_count"),
                "done_reason": response.get("done_reason"), "response_chars": len(content),
                "thinking_chars": len(response["message"].get("thinking") or ""),
                "think": record["request"].get("think"),
                "request_utf8_bytes": len(json.dumps(record["request"], ensure_ascii=False).encode("utf-8")),
                "options": record["request"]["options"]}
        if record.get("streaming"):
            item.update(streaming=True, complete=record["complete"], chunks=record["chunks"],
                        received_token_records=record["received_token_records"])
        try:
            json.loads(content)
            item["json_valid"] = True
        except json.JSONDecodeError as error:
            item.update(json_valid=False, json_error=str(error), response_tail=content[-200:])
        generations.append(item)
    after = {name: {"size": (source_data / name).stat().st_size,
                    "mtime_ns": (source_data / name).stat().st_mtime_ns}
             for name in source_stats}
    save(run_dir / "diagnosis_metrics.json", {"generations": generations,
         "source_business_files_unchanged": source_stats == after,
         "diagnostic_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         "config_changes_from_source_job": config_changes, "full_api_worker_replay": False})
    print(json.dumps(generations, ensure_ascii=False, indent=2))
    raise SystemExit(1 if any(item["status"] == "failed" for item in outcomes) else 0)


if __name__ == "__main__":
    main()
