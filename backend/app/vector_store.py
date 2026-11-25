"""本地 Qdrant 索引发布与多查询召回；向量正文和返回的引用正文保持一致。"""

import hashlib
import json
from contextlib import closing
from uuid import NAMESPACE_URL, uuid5
from time import perf_counter

from .ranking import rrf

from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient, models

from . import database
from .config import settings
from .knowledge import FAMILIES, fingerprint, load_documents, save_documents


def split_document(document):
    """用递归字符分割器拆分单个字段，保留来源元数据与正文起始偏移。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", ". ", "；", "; ", "，", ", ", " ", ""],
        add_start_index=True,
    )
    return splitter.split_documents([document])


async def model_digest(client, model: str) -> str:
    """读取 Ollama 已安装模型的权重摘要；同名模型的权重也可能更新。"""
    installed = await client.list()
    client.health_model_digests = {item.model: item.digest for item in installed.models}
    matches = []
    for installed_model in installed.models:
        if installed_model.model == model:
            matches.append(installed_model.digest)
    if not matches:
        raise ValueError(f"Ollama 未安装嵌入模型 {model}")
    return matches[0]


async def build_index(
    client, dataset_hash: str | None = None, family: str = "reviewed_summary"
):
    """为指定集合构建并发布独立索引，返回包含版本信息的发布清单。

    调用脚本负责持有既有文件锁。先留存文档快照、写完新集合，再切换当前
    清单；异常向上传递，失败时不发布半成品，也不删除旧集合或旧版本。
    """
    if family == "medlineplus_summary" and dataset_hash is None:
        candidate = settings.data_dir / "sources/medlineplus/candidate.json"
        dataset_hash = json.loads(candidate.read_text(encoding="utf-8"))["dataset_hash"]
    loaded = sorted(
        load_documents(family, dataset_hash),
        key=lambda item: item.metadata["document_version"],
    )
    if not loaded:
        raise ValueError(f"{family} 没有可索引文档；请先导入对应字段或核对摘要")
    # 版本同时绑定输入文档、分块方式及权重，不能只用模型名称判断兼容。
    config = {
        "schema_version": 2,
        "family": family,
        "purpose": FAMILIES[family][1],
        "dataset_hashes": sorted({item.metadata["dataset_hash"] for item in loaded}),
        "embedding_model": settings.embedding_model,
        "embedding_digest": await model_digest(client, settings.embedding_model),
        "document_versions": [item.metadata["document_version"] for item in loaded],
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "splitter": "RecursiveCharacterTextSplitter-v2",
        "embedding_text": "exact_snippet",
    }
    version = fingerprint(config)
    collection = f"{family}_{version[:24]}"
    chunks = []
    for source_document in loaded:
        for document_chunk in split_document(source_document):
            chunks.append(document_chunk)
    with database.engine.begin() as connection:
        save_documents(connection, loaded)
    with closing(QdrantClient(path=str(settings.data_dir / "vectors"))) as vectors:
        # 相同文档、配置及模型权重复用同一发布版本，不覆写已完成集合。
        complete = vectors.collection_exists(collection) and vectors.count(
            collection, exact=True
        ).count == len(chunks)
        if not complete:
            for offset in range(0, len(chunks), 32):
                batch = chunks[offset : offset + 32]
                # 嵌入的正是稍后返回的 snippet，不额外拼疾病名或替换摘要。
                embeddings = await client.embed(
                    model=settings.embedding_model,
                    input=[chunk.page_content for chunk in batch],
                    truncate=False,
                    keep_alive="10m",
                )
                if offset == 0 and not vectors.collection_exists(collection):
                    vectors.create_collection(
                        collection,
                        vectors_config=models.VectorParams(
                            size=len(embeddings.embeddings[0]),
                            distance=models.Distance.COSINE,
                        ),
                    )
                # 同一发布版本中，分块编号稳定；payload 保留父文档和文本哈希。
                points = []
                for batch_index, (chunk, embedding) in enumerate(
                    zip(batch, embeddings.embeddings, strict=True)
                ):
                    chunk_index = offset + batch_index
                    text_hash = hashlib.sha256(
                        chunk.page_content.encode("utf-8")
                    ).hexdigest()
                    payload = {
                        **chunk.metadata,
                        "index_version": version,
                        "chunk_index": chunk_index,
                        "text_hash": text_hash,
                        "snippet": chunk.page_content,
                    }
                    points.append(
                        models.PointStruct(
                            id=str(uuid5(NAMESPACE_URL, f"{version}:{chunk_index}")),
                            vector=embedding,
                            payload=payload,
                        )
                    )
                vectors.upsert(collection, points=points, wait=True)
                print(
                    f"Embedded {min(offset + 32, len(chunks))}/{len(chunks)} chunks",
                    flush=True,
                )
        count = vectors.count(collection, exact=True).count
        dimensions = vectors.get_collection(collection).config.params.vectors.size
    if count != len(chunks):
        raise ValueError("索引分块数量不一致，未发布")
    config.update(
        index_version=version,
        collection=collection,
        chunks=count,
        documents=len(loaded),
        embedding_dimensions=dimensions,
    )
    directory = settings.data_dir / "indexes"
    (directory / "versions").mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(config, ensure_ascii=False, indent=2)
    (directory / "versions" / f"{version}.json").write_text(
        serialized, encoding="utf-8"
    )
    # 全部向量写入后才切换发布指针；保留旧集合、旧发布清单和原文快照。
    temporary = directory / f"{family}.pending.json"
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(directory / f"{family}.json")
    return config


async def retrieve(
    terms: list[str], client, queries: list[str] | None = None, limit: int = 3
) -> list[dict]:
    """同发布版本的向量召回；块级融合，不提前折叠父主题。"""
    if not terms:
        return []
    started = perf_counter()
    if settings.review_policy == "reviewed_only":
        family = settings.evidence_family
    else:
        family = "research_description"
    path = settings.data_dir / "indexes" / f"{family}.json"
    if not path.exists():
        raise ValueError(
            f"缺少 {family} 索引；请运行 scripts/build_knowledge_indexes.py。旧 index.json 不能用于新检索。"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest["schema_version"] not in (2, 3)
        or manifest["family"] != family
        or manifest["purpose"] != FAMILIES[family][1]
    ):
        raise ValueError("索引类型或版本不匹配，请重新建立对应索引")
    release_config = {
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
    if (
        fingerprint(release_config) != manifest["index_version"]
        or manifest["collection"] != f"{family}_{manifest['index_version'][:24]}"
    ):
        raise ValueError("发布清单配置不匹配")
    allowed = set(manifest["document_versions"])
    if family in ("reviewed_summary", "medlineplus_summary"):
        allowed.intersection_update(
            item.metadata["document_version"] for item in load_documents(family)
        )
    if not allowed:
        return []
    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="document_version", match=models.MatchAny(any=sorted(allowed))
            )
        ]
    )
    descriptions = list(
        dict.fromkeys(
            " ".join(query.casefold().split())
            for query in (queries or [", ".join(terms)])
            if query.strip()
        )
    )
    descriptions = descriptions[:2]
    timings = {"open_index_seconds": perf_counter() - started}
    if (
        await model_digest(client, manifest["embedding_model"])
        != manifest["embedding_digest"]
    ):
        raise ValueError("嵌入模型权重已变化，请重建索引，不能混用新旧向量")
    mark = perf_counter()
    inputs = [
        "Instruct: Retrieve health topic summaries relevant to the reported symptoms.\nQuery: "
        + query
        for query in descriptions
    ]
    embedding = await client.embed(
        model=manifest["embedding_model"],
        input=inputs,
        truncate=False,
        keep_alive=0,
    )
    timings["embedding_seconds"] = perf_counter() - mark
    mark = perf_counter()
    with closing(QdrantClient(path=str(settings.data_dir / "vectors"))) as vectors:
        rankings = []
        for query_vector in embedding.embeddings:
            response = vectors.query_points(
                manifest["collection"],
                query=query_vector,
                limit=settings.recall_limit,
                query_filter=query_filter,
            )
            query_hits = []
            for hit in response.points:
                query_hits.append(
                    {"id": str(hit.id), **hit.payload, "score": hit.score}
                )
            rankings.append(query_hits)
        neighbors = []
        if settings.context_neighbors:
            offset = None
            while True:
                points, offset = vectors.scroll(
                    manifest["collection"], scroll_filter=query_filter, offset=offset
                )
                neighbors.extend(
                    {"id": str(point.id), **point.payload} for point in points
                )
                if offset is None:
                    break
    timings["vector_seconds"] = perf_counter() - mark
    mark = perf_counter()
    weights = [1 / len(rankings)] * len(rankings)
    result = rrf(rankings, weights, settings.rrf_constant)[:limit]
    timings["fusion_seconds"] = perf_counter() - mark
    audit = {"vector": [[hit["id"] for hit in hits] for hits in rankings]}
    for item in result:
        item["retrieval"] = {
            "mode": "vector",
            "layout": "hierarchical",
            "queries": descriptions,
            "rankings": audit,
            "timings": timings,
            "rrf_constant": settings.rrf_constant,
            "weights": weights,
            "index_version": manifest["index_version"],
            "embedding_digest": manifest["embedding_digest"],
        }
        if settings.context_neighbors:
            adjacent_chunks = []
            for other in neighbors:
                if other["document_version"] != item["document_version"]:
                    continue
                chunk_distance = abs(other["chunk_index"] - item["chunk_index"])
                if 0 < chunk_distance <= settings.context_neighbors:
                    adjacent_chunks.append(other)
            adjacent_chunks.sort(key=lambda chunk: chunk["chunk_index"])
            item["context"] = adjacent_chunks
    return result
