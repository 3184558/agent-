"""按字段加载知识；原记录与字段文档分别保留，不执行对话中的指令。"""

import hashlib
import json

from langchain_core.documents import Document
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from . import database


# family 决定独立集合；kind 表示字段类型，purpose 决定医学参考或研究用途。
FAMILIES = {
    "reviewed_summary": ("summary", "medical_evidence"),
    "medlineplus_summary": ("summary", "medical_evidence"),
    "research_description": ("description", "research"),
    "research_summary": ("summary", "research"),
    "research_dialogue": ("dialogue", "research"),
}
# 原数据三个字段各自加载，全部归入研究用途，不自动获得来源核对状态。
DATASET_FIELDS = {
    "description": "research_description",
    "executive_summary": "research_summary",
    "conversation": "research_dialogue",
}


def fingerprint(value) -> str:
    """对规范化 JSON 计算 SHA-256，避免字典键顺序改变文档版本。"""
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def field_document(
    text: str,
    *,
    family: str,
    parent_id: str,
    field: str,
    dataset_hash: str,
    name: str,
    **provenance,
) -> Document:
    """把一个字段正文和来源信息封装为 LangChain Document，不改写正文。

    document_id 标识来源中的同一字段；document_version 另包含正文哈希、
    数据集版本及来源元数据，内容或来源变更时形成新的快照版本。
    """
    kind, purpose = FAMILIES[family]
    attributes = {
        "document_id": fingerprint([provenance["source_id"], parent_id, field]),
        "parent_document_id": parent_id,
        "dataset_hash": dataset_hash,
        "name": name,
        "field": field,
        "family": family,
        "kind": kind,
        "purpose": purpose,
        "document_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        **provenance,
    }
    attributes["document_version"] = fingerprint(attributes)
    return Document(page_content=text, metadata=attributes)


def load_record_fields(
    record: dict,
    *,
    dataset_hash: str,
    line_number: int,
    fields: tuple[str, ...],
    source_id: str = "local-disease-jsonl",
) -> tuple[dict, list[Document]]:
    """校验一条已解析 JSONL 记录，返回完整父快照和所选字段文档。

    描述、案例摘要分别保留原字符串；对话只渲染角色与正文供研究使用，
    原消息数组及其他未选字段仍完整保留在父记录中。
    """
    # 导入入口校验一次；未知字段与非文本内容不静默转换为医学资料。
    if not isinstance(record.get("name"), str) or not record["name"].strip():
        raise ValueError(f"第{line_number}行缺少疾病名称")
    if any(field not in DATASET_FIELDS for field in fields):
        raise ValueError("字段仅支持 description、executive_summary、conversation")
    for field in fields:
        value = record[field]
        if field == "conversation":
            if not isinstance(value, list) or any(
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)
                for message in value
            ):
                raise ValueError(
                    f"第{line_number}行 conversation 必须是 role/content 消息数组"
                )
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"第{line_number}行 {field} 必须是非空文本")
    # 沿用旧疾病标识以关联审核表；父版本另包含文件版本、行号和整条记录。
    parent_id = hashlib.sha256(record["name"].lower().encode()).hexdigest()[:24]
    parent_version = fingerprint([source_id, dataset_hash, line_number, record])
    parent = {
        "version_id": parent_version,
        "parent_document_id": parent_id,
        "dataset_hash": dataset_hash,
        "source_id": source_id,
        "line_number": line_number,
        "record": record,
    }
    loaded = []
    for field in fields:
        value = record[field]
        # system 在此只是研究正文中的角色标签，不转换为运行时模型指令。
        if field == "conversation":
            text = "\n\n".join(
                (f"{message['role']}: {message['content']}" for message in value)
            )
        else:
            text = value
        loaded.append(
            field_document(
                text,
                family=DATASET_FIELDS[field],
                parent_id=parent_id,
                field=field,
                dataset_hash=dataset_hash,
                name=record["name"],
                source_id=source_id,
                parent_version=parent_version,
                line_number=line_number,
                verification="unverified",
                synthetic_dialogue=field == "conversation",
            )
        )
    return parent, loaded


def save_documents(connection, loaded: list[Document]):
    """使用调用方事务追加文档快照；相同版本重复导入时保留已有记录。"""
    for document in loaded:
        attributes = document.metadata
        connection.execute(
            insert(database.knowledge_versions)
            .values(
                version_id=attributes["document_version"],
                document_id=attributes["document_id"],
                parent_document_id=attributes["parent_document_id"],
                family=attributes["family"],
                dataset_hash=attributes["dataset_hash"],
                text=document.page_content,
                attributes=attributes,
            )
            .on_conflict_do_nothing(index_elements=["version_id"])
        )


def load_documents(family: str, dataset_hash: str | None = None) -> list[Document]:
    """按集合加载文档：默认来源核对摘要，研究集合必须指定数据集版本。

    摘要从当前有效核对记录生成，供发布和撤销过滤使用；研究字段从已保存
    快照加载。此函数不做文本分块、嵌入或在线来源审核。
    """
    if family not in FAMILIES or (
        family not in ("reviewed_summary", "medlineplus_summary") and not dataset_hash
    ):
        raise ValueError("研究索引必须指定 dataset_hash；family 必须是已定义的集合类型")
    with database.engine.connect() as connection:
        if family == "reviewed_summary":
            statement = (
                select(database.reviews, database.documents.c.name)
                .join(
                    database.documents,
                    database.reviews.c.document_id == database.documents.c.id,
                )
                .where(
                    database.reviews.c.status == "source_checked",
                    database.reviews.c.dataset_hash
                    == database.documents.c.dataset_hash,
                )
            )
            if dataset_hash:
                statement = statement.where(
                    database.reviews.c.dataset_hash == dataset_hash
                )
            rows = connection.execute(statement).mappings().all()
            return [
                field_document(
                    row["excerpt"],
                    family=family,
                    parent_id=row["document_id"],
                    field="excerpt",
                    dataset_hash=row["dataset_hash"],
                    name=row["name"],
                    source_id="checked-public-source",
                    verification="source_checked",
                    clinical_review=row["clinical_review"],
                    source_url=row["source_url"],
                    source_title=row["source_title"],
                    checked_at=row["checked_at"],
                )
                for row in rows
            ]
        if family == "medlineplus_summary":
            directory = database.settings.data_dir / "sources/medlineplus"
            if dataset_hash:
                pointer = directory / f"{dataset_hash}.json"
            else:
                pointer = (
                    database.settings.data_dir / "indexes/medlineplus_summary.json"
                )
            if not pointer.exists():
                return []
            release = json.loads(pointer.read_text(encoding="utf-8"))
            revoked_path = directory / "revoked.json"
            if revoked_path.exists():
                revoked = set(json.loads(revoked_path.read_text(encoding="utf-8")))
            else:
                revoked = set()
            versions = []
            for document_version in release["document_versions"]:
                if document_version not in revoked:
                    versions.append(document_version)
            statement = select(database.knowledge_versions).where(
                database.knowledge_versions.c.family == family,
                database.knowledge_versions.c.version_id.in_(versions),
            )
        else:
            statement = select(database.knowledge_versions).where(
                database.knowledge_versions.c.family == family,
                database.knowledge_versions.c.dataset_hash == dataset_hash,
            )
        rows = connection.execute(statement).mappings().all()
    return [
        Document(page_content=row["text"], metadata=row["attributes"]) for row in rows
    ]
