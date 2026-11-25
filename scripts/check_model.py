"""仅使用仓库内人工编写的开发案例，结果会写入 evaluation/runs。"""

import argparse
import asyncio
import json
import hashlib
import sys
from datetime import datetime
from pathlib import Path

import httpx
import ollama
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings
from app.ollama_client import PROMPT, create_client, generate
from app.schemas import AnalysisRequest

parser = argparse.ArgumentParser(
    description="运行本地模型开发案例；默认前四条，--limit 0 运行全部"
)
parser.add_argument("--limit", type=int, default=4)
parser.add_argument("--start", type=int, default=0)
args = parser.parse_args()
if args.limit < 0 or args.start < 0:
    parser.error("limit 和 start 必须大于等于 0")
cases = [
    json.loads(line)
    for line in (ROOT / "evaluation/development_cases.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
]
cases = cases[args.start : args.start + args.limit if args.limit else None]
output = ROOT / "evaluation/runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
output.mkdir(parents=True)


async def run_cases():
    """对选定开发案例运行无检索分析，逐条记录解析结果、失败类型和耗时。

    开发案例用于调试，不能作为独立临床准确率评估；这里只记录输出，
    不用模型自身的回答给自己评分。
    """
    async with create_client() as client:
        before = await client.ps()
        metadata = {
            "settings": settings.model_dump(mode="json"),
            "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
            "before": before.model_dump(mode="json"),
        }
        (output / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (output / "results.jsonl").open("w", encoding="utf-8") as file:
            for case in cases:
                record = {
                    "id": case["id"],
                    "category": case["category"],
                    "review_points": case["review_points"],
                }
                try:
                    result, metrics = await generate(
                        AnalysisRequest(text=case["text"]), client
                    )
                    record.update(
                        {
                            "parsed": True,
                            "analysis": result.model_dump(),
                            "metrics": metrics,
                        }
                    )
                    print(
                        f'{case["id"]}: {result.status}, {metrics["wall_seconds"]}s',
                        flush=True,
                    )
                except (
                    ValidationError,
                    httpx.RequestError,
                    ConnectionError,
                    ollama.ResponseError,
                ) as exc:
                    # 实验边界记录已知失败，以便继续检查其他案例；业务层不吞异常。
                    record.update({"parsed": False, "error_type": type(exc).__name__})
                    if isinstance(exc, ollama.ResponseError):
                        record["upstream_status"] = exc.status_code
                        record["upstream_error"] = exc.error
                    print(f'{case["id"]}: {type(exc).__name__}', flush=True)
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
                file.flush()
        (output / "after.json").write_text(
            (await client.ps()).model_dump_json(indent=2), encoding="utf-8"
        )
    print(f"Results: {output}")


asyncio.run(run_cases())
