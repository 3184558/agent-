"""在完全相同的已冻结候选上比较正文 LLM / Cross-Encoder；结果不自动提升为默认。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.agent import RERANK_PROMPT, PROMPT_VERSION
from app.config import settings
from app.ollama_client import create_client
from app.rerank import rerank_candidates

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--results", type=Path, required=True)
parser.add_argument("--cases", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--count", type=int, default=4)
parser.add_argument(
    "--modes",
    nargs="+",
    choices=("body_llm", "cross_encoder"),
    default=["body_llm", "cross_encoder"],
)
args = parser.parse_args()


async def compare():
    artifact = json.loads(args.results.read_text(encoding="utf-8"))
    texts = {
        r["id"]: r["text"]
        for r in map(json.loads, args.cases.read_text(encoding="utf-8").splitlines())
    }
    rows = [
        r
        for r in artifact["results"]
        if r["mode"] == "vector"
        and r["layout"] == "hierarchical"
        and len(r["queries"]) == 2
    ][: args.count]
    model = json.loads(
        Path("evaluation/cross_encoder_model.json").read_text(encoding="utf-8")
    )
    settings.cross_encoder_path = Path(model["path"])
    settings.cross_encoder_revision = model["revision"]
    settings.num_ctx = 16384
    results = []
    async with create_client() as client:
        for row in rows:
            candidates = row["candidates"][:6]
            for mode in args.modes:
                settings.reranker = mode
                try:
                    selected, metrics = await rerank_candidates(
                        texts[row["case_id"]], candidates, client, RERANK_PROMPT
                    )
                    result = {
                        "case_id": row["case_id"],
                        "mode": mode,
                        "prompt_schema_version": PROMPT_VERSION,
                        "candidate_ids": [c["id"] for c in candidates],
                        "selected": selected,
                        "metrics": metrics,
                    }
                except Exception as error:
                    result = {
                        "case_id": row["case_id"],
                        "mode": mode,
                        "error": type(error).__name__ + ": " + str(error),
                    }
                results.append(result)
                print(
                    row["case_id"],
                    mode,
                    "failed" if "error" in result else "done",
                    flush=True,
                )
                args.output.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                )


asyncio.run(compare())
