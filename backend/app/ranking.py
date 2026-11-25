"""合并多条向量查询的召回排名。"""


def rrf(rankings, weights, constant=60, limit=100):
    """融合各路名次；同一路内重复片段只计一次，保留各路排名供审计。"""
    scores = {}
    representative_hits = {}
    route_ranks = {}
    for route_index, (hits, weight) in enumerate(zip(rankings, weights, strict=True)):
        seen_ids = set()
        for rank, hit in enumerate(hits, 1):
            chunk_id = hit["id"]
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)
            representative_hits.setdefault(chunk_id, hit)
            scores[chunk_id] = scores.get(chunk_id, 0) + weight / (constant + rank)
            route_ranks.setdefault(chunk_id, {})[str(route_index)] = rank

    ranked_ids = sorted(scores, key=scores.get, reverse=True)
    results = []
    for chunk_id in ranked_ids[:limit]:
        results.append(
            {
                **representative_hits[chunk_id],
                "fusion_score": scores[chunk_id],
                "route_ranks": route_ranks[chunk_id],
            }
        )
    return results
