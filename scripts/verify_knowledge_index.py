"""检查发布索引与文档快照的一致性；--reembed 可重新计算向量作比对。"""

import argparse
import asyncio
import hashlib
import json
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
from filelock import FileLock
from qdrant_client import QdrantClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import database
from app.config import settings
from app.knowledge import FAMILIES, fingerprint
from app.ollama_client import create_client
from app.vector_store import model_digest


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--family", choices=tuple(FAMILIES), default="reviewed_summary")
parser.add_argument(
    "--reembed",
    action="store_true",
    help="调用本地 Ollama，重新嵌入全部片段并与库中向量比较",
)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
database.initialize()


async def verify():
    """核对发布配置、文档快照和每个片段，打印或写出机器可读报告。

    --reembed 才调用本地模型复算向量；数值差异仅作为观测值报告，
    不要求浮点向量逐位相同，也不将文本一致性解释为医学正确性。
    """
    directory = settings.data_dir / "indexes"
    manifest = json.loads(
        (directory / f"{args.family}.json").read_text(encoding="utf-8")
    )
    config = {
        key: value
        for key, value in manifest.items()
        if key
        not in (
            "index_version",
            "collection",
            "chunks",
            "documents",
            "embedding_dimensions",
            "lexical_hash",
            "lexical_file",
        )
    }
    assert fingerprint(config) == manifest["index_version"], "发布配置哈希不一致"
    assert (
        json.loads(
            (directory / "versions" / f'{manifest["index_version"]}.json').read_text(
                encoding="utf-8"
            )
        )
        == manifest
    )
    with database.engine.connect() as connection:
        rows = (
            connection.execute(
                select(database.knowledge_versions).where(
                    database.knowledge_versions.c.version_id.in_(
                        manifest["document_versions"]
                    )
                )
            )
            .mappings()
            .all()
        )
    # 以发布时的不可变快照为对照，不拿后来改过的摘要覆盖历史内容。
    snapshots = {row["version_id"]: row for row in rows}
    assert len(snapshots) == manifest["documents"], "文档快照缺失"
    count = 0
    max_vector_delta = 0.0
    min_cosine_similarity = 1.0
    async with create_client() as client:
        if args.reembed:
            assert (
                await model_digest(client, manifest["embedding_model"])
                == manifest["embedding_digest"]
            ), "模型权重变化"
        with closing(QdrantClient(path=str(settings.data_dir / "vectors"))) as vectors:
            offset = None
            while True:
                points, offset = vectors.scroll(
                    manifest["collection"],
                    limit=32,
                    offset=offset,
                    with_vectors=args.reembed,
                )
                # 同时检查正文切片、两个文本哈希、来源元数据以及用途隔离。
                for point in points:
                    payload = point.payload
                    snapshot = snapshots[payload["document_version"]]
                    start, text = payload["start_index"], payload["snippet"]
                    assert (
                        snapshot["text"][start : start + len(text)] == text
                    ), "引用与文档原文不一致"
                    assert (
                        hashlib.sha256(text.encode("utf-8")).hexdigest()
                        == payload["text_hash"]
                    )
                    assert (
                        hashlib.sha256(snapshot["text"].encode("utf-8")).hexdigest()
                        == payload["document_text_hash"]
                    )
                    assert all(
                        payload[key] == value
                        for key, value in snapshot["attributes"].items()
                    ), "文档元数据不一致"
                    assert (
                        payload["family"] == args.family
                        and payload["purpose"] == FAMILIES[args.family][1]
                    )
                    assert payload["index_version"] == manifest["index_version"]
                if args.reembed:
                    response = await client.embed(
                        model=manifest["embedding_model"],
                        input=[point.payload["snippet"] for point in points],
                        truncate=False,
                        keep_alive=0,
                    )
                    expected = np.array(response.embeddings)
                    # Qdrant COSINE 集合按单位向量存储，复算结果也先归一化再比较。
                    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
                    stored = np.array([point.vector for point in points])
                    delta = float(np.max(np.abs(expected - stored)))
                    max_vector_delta = max(max_vector_delta, delta)
                    cosine = np.sum(expected * stored, axis=1) / np.linalg.norm(
                        stored, axis=1
                    )
                    min_cosine_similarity = min(
                        min_cosine_similarity, float(np.min(cosine))
                    )
                count += len(points)
                if offset is None:
                    break
    assert count == manifest["chunks"], "分块数量不一致"
    result = {
        "checked_at": database.now(),
        "family": args.family,
        "index_version": manifest["index_version"],
        "documents": len(snapshots),
        "chunks_checked": count,
        "text_and_metadata_match": True,
        "reembedded": args.reembed,
        "max_vector_component_delta": max_vector_delta if args.reembed else None,
        "min_reembedding_cosine_similarity": (
            min_cosine_similarity if args.reembed else None
        ),
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized)


with FileLock(settings.data_dir / "ollama.lock"):
    asyncio.run(verify())
