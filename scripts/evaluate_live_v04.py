"""隔离于业务会话库的真实模型工作流评估；保存逐例失败与资源观测。"""

import argparse
import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from time import perf_counter

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.agent import PROMPT_VERSION
from app.tasks import execute
from app.ollama_client import create_client

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cases", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--count", type=int, default=4)
args = parser.parse_args()
rows = [
    json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines()
][: args.count]
settings.evidence_family = "medlineplus_summary"
settings.reranker = "body_llm"
settings.num_ctx = 16384
settings.candidate_limit = 6
observations = []
finished = threading.Event()


def sample_resources():
    while not finished.is_set():
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
        )
        observations.append(
            {
                "elapsed_seconds": perf_counter() - started,
                "python_rss_bytes": psutil.Process().memory_info().rss,
                "gpu_used_mib": (
                    [int(value) for value in gpu.stdout.splitlines()]
                    if gpu.returncode == 0
                    else None
                ),
            }
        )
        finished.wait(1)


async def evaluate():
    results = []
    async with create_client() as client:
        tags = await client.list()
    for row in rows:
        mark = perf_counter()
        # 评估边界捕获一次，保留失败案例，不改变运行时异常处理。
        try:
            state = await execute(row["text"], row.get("evidence_text", row["text"]))
            results.append(
                {
                    "case_id": row["id"],
                    "text": row["text"],
                    "state": state,
                    "elapsed_seconds": perf_counter() - mark,
                    "status": "succeeded",
                }
            )
        except Exception as error:
            results.append(
                {
                    "case_id": row["id"],
                    "text": row["text"],
                    "status": "failed",
                    "error": type(error).__name__ + ": " + str(error),
                    "elapsed_seconds": perf_counter() - mark,
                }
            )
        print(row["id"], results[-1]["status"], flush=True)
        args.output.write_text(
            json.dumps(
                {
                    "results": results,
                    "resources": observations,
                    "resource_sampling": "1-second best effort; total GPU usage includes Ollama and other processes; no guaranteed peak",
                    "models": [
                        {"name": m.model, "digest": m.digest} for m in tags.models
                    ],
                    "prompt_schema_version": PROMPT_VERSION,
                    "config": settings.model_dump(mode="json"),
                    "clinical_adjudication": "pending",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


started = perf_counter()
sampler = threading.Thread(target=sample_resources)
sampler.start()
try:
    asyncio.run(evaluate())
finally:
    finished.set()
    sampler.join()
