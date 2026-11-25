"""80例合成自述的隔离串行评测；真实API和独立Huey消费者，不修改产品配置。"""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evaluation/patient_narratives_v1"
CATEGORIES = {"routine": 12, "uncertainty": 12, "self_diagnosis": 8,
              "negation_time_subject": 12, "multiturn": 12, "coverage_gap": 8,
              "urgent_context": 8, "complex_noise": 8}


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def append_jsonl(path, value):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False) + "\n")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def freeze_cases():
    """病例入口一次校验数量、分组、长度与操作，再冻结协议。"""
    inputs, files = [], []
    for split in ("development", "holdout"):
        paths = [CASES / f"{split}.{kind}.jsonl" for kind in ("inputs", "oracles", "manifest")]
        rows, oracles, manifest = [read_jsonl(path) for path in paths]
        assert len(rows) == (50 if split == "development" else 30)
        assert {r["case_id"] for r in rows} == {r["case_id"] for r in oracles} == {r["case_id"] for r in manifest}
        for row in rows:
            assert row["split"] == split and row["synthetic"] is True
            assert 1 <= len(row["initial_text"].strip()) <= 1200
            assert len(row["steps"]) == (2 if row["category"] == "multiturn" else 0)
            for step in row["steps"]:
                items = step["items"] if step["action"] == "batch" else [step]
                assert 1 <= len(items) <= 4
                for item in items:
                    assert item["action"] in ("answer", "skip", "correct")
                    assert step["action"] != "batch" or item["action"] in ("answer", "skip")
                    assert item["target_keywords"]
                    if item["action"] != "skip":
                        assert 1 <= len(item["text"]) <= 500
        by_id = {row["case_id"]: row for row in rows}
        for oracle in oracles:
            row = by_id[oracle["case_id"]]
            contributions = [row["initial_text"]]
            for step in row["steps"]:
                contributions.extend(item["text"] for item in (step["items"] if step["action"] == "batch" else [step]) if item["action"] != "skip")
            evidence = "\n".join(contributions)
            assert all(fact["evidence"] and fact["evidence"] in evidence for fact in oracle["atomic_facts"]), oracle["case_id"]
            for turn in range(1 + len(row["steps"])):
                assert sum(f["importance"] == "key" and turn in f["applies_turns"] for f in oracle["atomic_facts"]) <= 12
        inputs.extend(rows)
        files.extend(paths)
    assert Counter(r["category"] for r in inputs) == CATEGORIES
    assert len({r["case_id"] for r in inputs}) == 80
    families = {}
    for row in inputs:
        assert families.setdefault(row["family_id"], row["split"]) == row["split"]
    pairs = Counter(r["pair_id"] for r in inputs if r.get("pair_id"))
    assert len(pairs) >= 8 and all(n == 2 for n in pairs.values())
    assert all(len({r["family_id"] for r in inputs if r.get("pair_id") == pair}) == 1 for pair in pairs)
    pilot = [next(r["case_id"] for r in inputs if r["split"] == "development" and r["category"] == category)
             for category in CATEGORIES]
    protocol = {"version": "1.0", "frozen_at": utc_now(), "synthetic": True,
                "cases": 80, "planned_turns": 104, "splits": {"development": 50, "holdout": 30},
                "category_counts": CATEGORIES, "pilot_ids": pilot,
                "files": {p.name: digest(p) for p in files},
                "generator": "Codex agents; development and holdout generated separately before outputs",
                "medical_review": "not professionally reviewed", "old_holdout_used": False,
                "semantic_statuses": ["pass", "fail", "needs_review", "not_evaluated", "not_applicable"],
                "semantic_pass_rate": "pass/(pass+fail); always display judgment coverage separately",
                "product_changes_authorized": False,
                "matching": "literal Chinese topic keywords; unique highest score, ties remain ambiguous; no forced indices",
                "observation_limit_seconds": 1800, "evaluator_retries": 0}
    save_json(CASES / "protocol.json", protocol)
    save_json(CASES / "protocol.freeze.json", {"protocol.json": digest(CASES / "protocol.json")})
    print(json.dumps({"cases": 80, "turns": 104, "pilot_ids": pilot, "frozen": True}))


def verify_frozen():
    protocol = json.loads((CASES / "protocol.json").read_text(encoding="utf-8"))
    freeze = json.loads((CASES / "protocol.freeze.json").read_text(encoding="utf-8"))
    assert digest(CASES / "protocol.json") == freeze["protocol.json"]
    assert all(digest(CASES / name) == value for name, value in protocol["files"].items())
    return protocol


def prepare(run_dir, source_data):
    """仅迁移活动知识和向量；不复制日常任务、会话、尝试或队列。"""
    import httpx
    from app import database
    from app.config import settings
    from app.knowledge import fingerprint, load_documents
    from qdrant_client import QdrantClient

    assert source_data.resolve() != settings.data_dir.resolve()
    assert settings.review_policy == "reviewed_only" and settings.evidence_family == "reviewed_summary", "本轮只实现当前核实的资料迁移路径"
    manifest_path = source_data / "indexes/reviewed_summary.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vector_meta_path = source_data / "vectors/meta.json"
    vector_meta = json.loads(vector_meta_path.read_text(encoding="utf-8"))
    source_collection = source_data / "vectors/collection" / manifest["collection"]
    before = {str(p.relative_to(source_data)): digest(p) for p in [manifest_path, vector_meta_path, *source_collection.rglob("*")] if p.is_file()}
    database.initialize()
    with sqlite3.connect(f"{(source_data / 'health.db').as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(settings.data_dir / "health.db") as target:
        source.row_factory = sqlite3.Row
        versions = manifest["document_versions"]
        placeholders = ",".join("?" for _ in versions)
        snapshots = source.execute(f"SELECT * FROM knowledge_document_versions WHERE version_id IN ({placeholders})", versions).fetchall()
        assert len(snapshots) == manifest["documents"]
        parents = [r["parent_document_id"] for r in snapshots]
        params = ",".join("?" for _ in parents)
        rows_by_table = {
            "knowledge_document_versions": snapshots,
            "disease_documents": source.execute(f"SELECT * FROM disease_documents WHERE id IN ({params})", parents).fetchall(),
            "source_reviews": source.execute(f"SELECT * FROM source_reviews WHERE document_id IN ({params})", parents).fetchall(),
        }
        for table, rows in rows_by_table.items():
            for row in rows:
                columns = ",".join(f'"{column}"' for column in row.keys())
                target.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row))
        counts = {table: target.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("analysis_jobs", "conversation_turns", "analysis_attempt_runs", *rows_by_table)}
        assert all(counts[t] == 0 for t in ("analysis_jobs", "conversation_turns", "analysis_attempt_runs"))
    index_dir = settings.data_dir / "indexes"
    (index_dir / "versions").mkdir(parents=True)
    shutil.copy2(manifest_path, index_dir / manifest_path.name)
    shutil.copy2(source_data / "indexes/versions" / f"{manifest['index_version']}.json", index_dir / "versions" / f"{manifest['index_version']}.json")
    vector_dir = settings.data_dir / "vectors"
    (vector_dir / "collection").mkdir(parents=True)
    shutil.copytree(source_collection, vector_dir / "collection" / manifest["collection"])
    vector_meta["collections"] = {manifest["collection"]: vector_meta["collections"][manifest["collection"]]}
    vector_meta["aliases"] = {}
    save_json(vector_dir / "meta.json", vector_meta)
    assert before == {name: digest(source_data / name) for name in before}, "复制时源索引发生变化"
    assert {d.metadata["document_version"] for d in load_documents("reviewed_summary")} == set(versions)
    with closing(QdrantClient(path=str(vector_dir))) as vectors:
        assert vectors.count(manifest["collection"], exact=True).count == manifest["chunks"]
        assert vectors.get_collection(manifest["collection"]).config.params.vectors.size == manifest["embedding_dimensions"]
    with (run_dir / "index_verification.log").open("x", encoding="utf-8") as log:
        subprocess.run([sys.executable, str(ROOT / "scripts/verify_knowledge_index.py"),
                        "--family", "reviewed_summary", "--output", str(run_dir / "index_verification.json")],
                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    with httpx.Client(trust_env=False, timeout=10) as client:
        installed = client.get(settings.ollama_url + "/api/tags").json()["models"]
        models = [{k: m[k] for k in ("name", "digest", "size")} for m in installed]
        assert next(m["digest"] for m in models if m["name"] == settings.embedding_model) == manifest["embedding_digest"]
        assert any(m["name"] == settings.model for m in models)
        version = client.get(settings.ollama_url + "/api/version").json()
        active = client.get(settings.ollama_url + "/api/ps").json()
    config = settings.model_dump(mode="json")
    config.pop("ollama_url")
    from app.agent import PROMPT_VERSION
    files = [*list((ROOT / "backend/app").rglob("*.py")), *list((ROOT / "backend/app/prompts").glob("*.txt"))]
    metadata = {"run_id": run_dir.name, "prepared_at": utc_now(), "settings": config,
                "prompt_version": PROMPT_VERSION, "code_hashes": {p.relative_to(ROOT).as_posix(): digest(p) for p in files},
                "runner_hash": digest(Path(__file__)), "protocol_hash": digest(CASES / "protocol.json"),
                "model_digests": models, "ollama_version": version, "ollama_ps_before": active,
                "gpu_before": subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip(),
                "index": manifest, "source_index_hashes": before, "isolated_counts_before": counts,
                "generation_options": {"plan": {"stream": True, "logprobs": True, "top_logprobs": 0, "think": settings.plan_think, "temperature": 0, "num_predict": settings.plan_num_predict, "seed": "not set"},
                    "rerank": {"stream": True, "logprobs": True, "top_logprobs": 0, "think": settings.rerank_think, "temperature": 0, "num_predict": settings.rerank_num_predict, "seed": "not set"},
                    "analysis": {"stream": True, "logprobs": True, "top_logprobs": 0, "think": settings.think, "temperature": 0, "num_predict": settings.num_predict, "seed": 42}},
                "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "no commit",
                "chain": "TestClient real API -> persistent queue -> independent Huey consumer -> real Ollama/retrieval -> DB -> API",
                "product_code_modified": False, "frontend_verified": False}
    save_json(run_dir / "run_metadata.json", metadata)
    print(json.dumps({"prepared": True, "knowledge_documents": counts["disease_documents"], "business_rows": 0}))


def select_target(choices, keywords, correction=False):
    """语义目标的可审查关键词匹配；同分不猜选，纠正按meaning定位。"""
    scored = []
    for index, choice in enumerate(choices):
        text = choice["meaning"] if correction else choice
        matches = [word for word in keywords if word in text]
        if matches:
            scored.append({"index": index, "score": len(matches), "matched_keywords": matches, "choice": choice})
    if not scored:
        return None, {"status": "target_missing", "keywords": keywords}
    top = max(item["score"] for item in scored)
    best = [item for item in scored if item["score"] == top]
    if len(best) != 1:
        return None, {"status": "ambiguous_target", "candidates": best}
    return best[0]["index"], {"status": "matched", **best[0]}


def submit_step(parent, step):
    analysis = parent["result"]["analysis"]
    payloads, matching = [], []
    for item in step["items"] if step["action"] == "batch" else [step]:
        correct = item["action"] == "correct"
        choices = analysis["facts"] if correct else analysis["missing_information"]
        index, match = select_target(choices, item["target_keywords"], correct)
        matching.append(match)
        if index is None:
            return None, matching
        payload = {"action": item["action"], "index": index}
        if item["action"] != "skip":
            payload["text"] = item["text"]
        payloads.append(payload)
    if len({p["index"] for p in payloads}) != len(payloads):
        return None, [*matching, {"status": "ambiguous_target", "reason": "多个目标匹配同一问题，未伪造索引"}]
    return ({"action": "batch", "items": payloads} if step["action"] == "batch" else payloads[0]), matching


def run_phase(run_dir, phase, protocol):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import database
    from sqlalchemy import select

    split = "holdout" if phase == "holdout" else "development"
    rows = read_jsonl(CASES / f"{split}.inputs.jsonl")
    path = run_dir / "results.jsonl"
    previous = read_jsonl(path) if path.exists() else []
    done = {r["case_id"] for r in previous}
    rows = [row for row in rows if row["case_id"] not in done and (phase != "pilot" or row["case_id"] in protocol["pilot_ids"])]
    log = (run_dir / f"worker_{phase}.log").open("x", encoding="utf-8")
    worker = subprocess.Popen([sys.executable, str(Path(__file__)), "--phase", "worker", "--run-dir", str(run_dir)],
                              cwd=ROOT, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True)
    try:
        with TestClient(app) as api:
            for case in rows:
                parent, blocked = None, None
                for turn in range(1 + len(case["steps"])):
                    record = {"run_id": run_dir.name, "phase": phase, "case_id": case["case_id"], "family_id": case["family_id"],
                              "split": split, "category": case["category"], "turn": turn, "synthetic": True,
                              "action": "initial" if turn == 0 else case["steps"][turn - 1]["action"],
                              "planned_status": "planned", "observed_at": utc_now()}
                    if blocked:
                        record.update(execution_status="not_reached", reason=blocked)
                    elif turn and parent["status"] != "succeeded":
                        blocked = "upstream_failed"
                        record.update(execution_status="not_reached", reason=blocked)
                    elif turn and parent["result"]["origin"] == "safety_rule":
                        blocked = "ended_by_rule"
                        record.update(execution_status="not_reached", reason=blocked)
                    else:
                        if turn:
                            payload, matching = submit_step(parent, case["steps"][turn - 1])
                            record.update(matching=matching, parent_id=parent["id"])
                            endpoint = f"/api/tasks/{parent['id']}/follow-up"
                        else:
                            payload, endpoint = {"text": case["initial_text"]}, "/api/analysis"
                        if payload is None:
                            blocked = record["matching"][-1]["status"]
                            record.update(execution_status="not_reached", reason=blocked)
                        else:
                            record.update(submitted_json=payload, endpoint=endpoint, submitted_at=utc_now())
                            started = time.perf_counter()
                            response = api.post(endpoint, json=payload)
                            record.update(http_status=response.status_code, accepted=response.json())
                            if response.status_code not in (200, 202):
                                blocked = "api_rejected"
                                record.update(execution_status="api_rejected", wall_seconds=time.perf_counter() - started)
                            else:
                                parent = response.json()
                                while parent["status"] not in ("succeeded", "failed") and time.perf_counter() - started < protocol["observation_limit_seconds"]:
                                    time.sleep(0.5)
                                    parent = api.get(f"/api/tasks/{parent['id']}").json()
                                status = "terminal" if parent["status"] in ("succeeded", "failed") else "observation_interrupted"
                                if status != "terminal":
                                    blocked = status
                                with database.engine.connect() as connection:
                                    attempts = [dict(r) for r in connection.execute(select(database.attempt_runs).where(database.attempt_runs.c.job_id == parent["id"])).mappings()]
                                record.update(execution_status=status, job_id=parent["id"], job=parent,
                                              terminal_observed_at=utc_now(), wall_seconds=time.perf_counter() - started,
                                              attempts=attempts, turn_metadata=database.get_turn(parent["id"]),
                                              conversation=api.get(f"/api/tasks/{parent['id']}/conversation").json())
                    append_jsonl(path, record)
                    print(json.dumps({k: record.get(k) for k in ("case_id", "turn", "execution_status", "wall_seconds", "reason")}, ensure_ascii=False), flush=True)
    finally:
        worker.communicate("stop\n")
        log.close()
        database.engine.dispose()
    save_json(run_dir / f"phase_{phase}.json", {"completed_at": utc_now(), "case_ids": [r["case_id"] for r in rows], "worker_exit_code": worker.returncode})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("freeze", "prepare", "pilot", "development", "holdout", "worker"), required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--source-data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if args.phase == "freeze":
        freeze_cases()
        return
    assert args.run_dir is not None
    run_dir = args.run_dir.resolve()
    allowed = (ROOT / "evaluation/runs/patient_narratives_v1").resolve()
    assert run_dir.is_relative_to(allowed) and run_dir != allowed
    if args.phase == "prepare":
        run_dir.mkdir(parents=True, exist_ok=False)
    else:
        assert run_dir.is_dir()
    os.environ["HEALTH_DATA_DIR"] = str(run_dir / "data")
    sys.path.insert(0, str(ROOT / "backend"))
    if args.phase == "worker":
        from huey.consumer import Consumer
        from app.tasks import queue
        from app.database import engine
        consumer = Consumer(queue, workers=1, worker_type="thread", periodic=False)
        consumer.start()
        sys.stdin.readline()
        consumer.stop(graceful=True)
        queue.storage.close()
        engine.dispose()
        return
    protocol = verify_frozen()
    if args.phase == "prepare":
        prepare(run_dir, args.source_data)
    else:
        import httpx
        from app.config import settings
        metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
        actual_settings = settings.model_dump(mode="json")
        actual_settings.pop("ollama_url")
        assert actual_settings == metadata["settings"], "阶段实际配置有变更，必须新建run_id"
        assert digest(CASES / "protocol.json") == metadata["protocol_hash"]
        assert json.loads((settings.data_dir / "indexes/reviewed_summary.json").read_text(encoding="utf-8")) == metadata["index"]
        with httpx.Client(trust_env=False, timeout=10) as client:
            installed = client.get(settings.ollama_url + "/api/tags").json()["models"]
        expected_models = {m["name"]: m["digest"] for m in metadata["model_digests"]}
        actual_models = {m["name"]: m["digest"] for m in installed}
        assert all(actual_models.get(name) == expected_models[name] for name in (settings.model, settings.embedding_model)), "模型权重发生变更"
        assert digest(Path(__file__)) == metadata["runner_hash"], "运行器有变更，必须新建run_id"
        assert all(digest(ROOT / name) == value for name, value in metadata["code_hashes"].items()), "产品代码有变更，必须新建run_id"
        run_phase(run_dir, args.phase, protocol)


if __name__ == "__main__":
    main()
