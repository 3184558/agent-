"""重放人工构造的跨系统案例；目标名称仅用于评估，不送入模型。"""

import asyncio
import json
import sys
from pathlib import Path

from filelock import FileLock
from langsmith import tracing_context

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.agent import graph
from app.config import settings
from app.ollama_client import create_client

case = json.loads(
    (ROOT / "evaluation/cross_system_case.json").read_text(encoding="utf-8")
)


async def replay():
    """运行一个开发回归案例并保存完整状态，再检查预设条目是否进入结果。

    expected_sources 只参与运行后的断言；该回放不是独立医学效果评估。
    """
    with tracing_context(enabled=False):
        async with create_client() as client:
            state = await graph.ainvoke(
                {"text": case["text"], "trace": []},
                config={"configurable": {"client": client}, "callbacks": []},
            )
    path = ROOT / "evaluation/retrieval_replay_20261101.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "terms": state["terms"],
                "queries": state["queries"],
                "sources": [s["name"] for s in state["sources"]],
                "analysis": state["analysis"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    assert any(
        s["name"] in case["expected_sources"] for s in state["sources"]
    ), "该案例的预设参考条目未进入前三；不等同于临床诊断错误"


with FileLock(settings.data_dir / "ollama.lock"):
    asyncio.run(replay())
