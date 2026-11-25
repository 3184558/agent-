"""保留原始 JSONL 及按字段分离的研究文档；不自动发布为医学证据。"""

import argparse
import hashlib
import json
import sys
import asyncio
from pathlib import Path
from filelock import FileLock

from sqlalchemy.dialects.sqlite import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.database import documents, engine, initialize, knowledge_records
from app.config import settings
from app.knowledge import DATASET_FIELDS, load_record_fields, save_documents
from app.ollama_client import create_client
from app.vector_store import build_index


parser = argparse.ArgumentParser(
    description="按字段导入疾病研究资料，保留完整原记录与父文档关系"
)
parser.add_argument("path", type=Path)
parser.add_argument(
    "--fields", nargs="+", choices=tuple(DATASET_FIELDS), default=["description"]
)
parser.add_argument(
    "--build-index",
    action="store_true",
    help="显式为所选字段分别建立研究索引；不修改默认医学证据集合",
)
args = parser.parse_args()
# 用完整文件哈希标记这次导入的数据版本，字段快照由父记录关联回原文件行。
digest = hashlib.sha256(args.path.read_bytes()).hexdigest()
initialize()
count = 0
# 导入复用现有文件锁；父记录、字段文档和当前疾病表在同一事务中写入。
with FileLock(settings.data_dir / "ollama.lock"):
    with engine.begin() as connection, args.path.open(encoding="utf-8-sig") as file:
        for count, line in enumerate(file, 1):
            record = json.loads(line)
            parent, loaded = load_record_fields(
                record,
                dataset_hash=digest,
                line_number=count,
                fields=tuple(args.fields),
            )
            connection.execute(
                insert(knowledge_records)
                .values(**parent)
                .on_conflict_do_nothing(index_elements=["version_id"])
            )
            save_documents(connection, loaded)
            # 保留既有审核表的疾病标识；原字段快照始终另存，不随更新覆盖。
            values = {
                "id": parent["parent_document_id"],
                "name": record["name"],
                "content": record["description"],
                "icd10": record["icd10"],
                "references": record["pubmed_refs"],
                "dataset_hash": digest,
            }
            connection.execute(
                insert(documents)
                .values(**values)
                .on_conflict_do_update(index_elements=["id"], set_=values)
            )
print(f"Imported {count} records; dataset_sha256={digest}; verification=unverified")


async def index():
    """仅在显式 --build-index 时，为所选字段分别建立研究索引并卸载嵌入模型。"""
    async with create_client() as client:
        for field in args.fields:
            info = await build_index(client, digest, family=DATASET_FIELDS[field])
            print(info)
        await client.embed(model=settings.embedding_model, input="", keep_alive=0)


# 默认只导入快照，不顺带批量调用嵌入模型，也不修改默认摘要索引。
if args.build_index:
    with FileLock(settings.data_dir / "ollama.lock"):
        asyncio.run(index())
