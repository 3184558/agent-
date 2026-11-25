"""发布一个独立知识索引；默认仅索引已核对摘要。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from filelock import FileLock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.database import initialize
from app.knowledge import FAMILIES
from app.ollama_client import create_client
from app.vector_store import build_index


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--family", choices=tuple(FAMILIES), default="reviewed_summary")
parser.add_argument("--dataset-hash", help="研究集合必须指定原 JSONL 的 SHA-256")
args = parser.parse_args()
initialize()


async def publish():
    """构建指定 family 并打印发布清单；成功后请求 Ollama 卸载嵌入模型。"""
    async with create_client() as client:
        manifest = await build_index(client, args.dataset_hash, family=args.family)
        await client.embed(model=settings.embedding_model, input="", keep_alive=0)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))


# 与正常分析流程共用同一把既有锁，避免本地集合被多个进程同时打开。
with FileLock(settings.data_dir / "ollama.lock"):
    asyncio.run(publish())
