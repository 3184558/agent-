"""一次性冻结案例对照；不使用模型自己给医学正确性打分。"""

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx
import ollama
from filelock import FileLock
from langsmith import tracing_context
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.agent import PROMPT_VERSION, graph
from app.config import settings
from app.ollama_client import create_client, generate
from app.schemas import AnalysisRequest
from app.safety import check_safety

# 冻结案例及预设主题用于评估记录；预设答案不作为检索或分析输入。
case_path = ROOT / "evaluation/holdout_stage23.jsonl"
cases = [
    json.loads(line) for line in case_path.read_text(encoding="utf-8").splitlines()
]
output = (
    ROOT / "evaluation/runs" / ("stage23_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
)
output.mkdir(parents=True)
(output / "metadata.json").write_text(
    json.dumps(
        {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
            "prompt_version": PROMPT_VERSION,
            "settings": settings.model_dump(mode="json"),
            "medical_grading": "not performed; author-created frozen scenarios, not external blind validation",
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


async def evaluate():
    """对每个冻结案例分别运行无检索和有检索两组，输出结构及召回指标。

    单个案例失败后记录原因并继续；预设主题命中和状态匹配不等于医学
    正确率，这里也没有医学专家评分或第三方盲测。
    """
    with tracing_context(enabled=False), (output / "results.jsonl").open(
        "w", encoding="utf-8"
    ) as file:
        async with create_client() as client:
            for case in cases:
                for arm in ("baseline", "reviewed_rag"):
                    started = perf_counter()
                    record = {
                        "case_id": case["id"],
                        "arm": arm,
                        "expected_topic": case.get("expected_topic"),
                        "expected_status": case.get("expected_status"),
                    }
                    try:
                        rule = check_safety(case["text"])
                        sources = []
                        if rule:
                            analysis = rule.model_dump()
                            record["origin"] = "safety_rule"
                        elif arm == "baseline":
                            result, _ = await generate(
                                AnalysisRequest(text=case["text"]), client
                            )
                            analysis = result.model_dump()
                        else:
                            state = await graph.ainvoke(
                                {
                                    "text": case["text"],
                                    "evidence_text": case["text"],
                                    "trace": [],
                                },
                                config={
                                    "configurable": {"client": client},
                                    "callbacks": [],
                                },
                            )
                            analysis, sources = state["analysis"], state["sources"]
                            record["trace"] = state["trace"]
                        record.update(
                            parsed=True,
                            analysis=analysis,
                            sources=sources,
                            topic_recalled=(
                                any(
                                    s["name"] == case.get("expected_topic")
                                    for s in sources
                                )
                                if case.get("expected_topic")
                                else None
                            ),
                            status_match=(
                                analysis["status"] == case["expected_status"]
                                if case.get("expected_status")
                                else None
                            ),
                        )
                    except (
                        ValidationError,
                        ValueError,
                        httpx.RequestError,
                        ConnectionError,
                        ollama.ResponseError,
                    ) as exc:
                        record.update(parsed=False, error_type=type(exc).__name__)
                    record["seconds"] = round(perf_counter() - started, 3)
                    # 每个案例执行后立即写出，便于检查已完成项和已知失败。
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    file.flush()
                    print(
                        case["id"], arm, record["parsed"], record["seconds"], flush=True
                    )
    print(output, flush=True)


with FileLock(settings.data_dir / "ollama.lock"):
    asyncio.run(evaluate())
