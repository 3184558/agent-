"""更新公开来源核对记录；另运行 build_knowledge_indexes.py 发布摘要索引。"""

import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.database import documents, engine, initialize, reviews

# 人工整理的核对包是状态来源；此脚本不自动抓取网页或进行临床审核。
bundle = json.loads((ROOT / "review/checked_sources.json").read_text(encoding="utf-8"))
initialize()
# 只更新当前审核记录；向量发布由单独的构建脚本显式执行。
with engine.begin() as connection:
    for item in bundle["records"]:
        row = connection.execute(
            select(documents.c.id, documents.c.dataset_hash).where(
                documents.c.name == item["name"]
            )
        ).one()
        values = {
            "document_id": row.id,
            "dataset_hash": row.dataset_hash,
            "status": "source_checked",
            "excerpt": item["excerpt"],
            "source_url": item["source_url"],
            "source_title": item["source_title"],
            "checked_at": bundle["checked_at"],
            "clinical_review": "pending",
        }
        connection.execute(
            insert(reviews)
            .values(**values)
            .on_conflict_do_update(index_elements=["document_id"], set_=values)
        )
print(
    f'Updated {len(bundle["records"])} source-checked summaries; clinical review remains pending. Run scripts/build_knowledge_indexes.py to publish their index.'
)
