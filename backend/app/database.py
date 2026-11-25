"""本机业务数据库：任务、审核状态及不可变知识文档快照。"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
)

from .config import settings

settings.data_dir.mkdir(parents=True, exist_ok=True)
engine = create_engine(
    f"sqlite:///{(settings.data_dir / 'health.db').as_posix()}",
    connect_args={"timeout": 30},
)
metadata = MetaData()
# 一次分析任务的输入、状态、结果与执行次数；队列本身由 Huey 另库存储。
jobs = Table(
    "analysis_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("text", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("result", JSON),
    Column("error", Text),
    Column("trace", JSON),
    Column("prompt_version", String),
    Column("created_at", String, nullable=False),
    Column("elapsed_seconds", Float),
)
# 疾病当前导入版本，供审核记录关联；历史原记录另存知识快照表。
documents = Table(
    "disease_documents",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("icd10", String),
    Column("references", JSON),
    Column("dataset_hash", String, nullable=False),
)
# parent_id 指向上一轮，root_id 指向会话起点；evidence_text 仅累积用户原话。
turns = Table(
    "conversation_turns",
    metadata,
    Column("job_id", String, primary_key=True),
    Column("parent_id", String, nullable=False),
    Column("root_id", String, nullable=False),
    Column("contribution", JSON, nullable=False),
    Column("evidence_text", Text, nullable=False),
)
# 当前来源核对状态；source_checked 只表示公开来源核对，不表示临床审核。
reviews = Table(
    "source_reviews",
    metadata,
    Column("document_id", String, primary_key=True),
    Column("dataset_hash", String, nullable=False),
    Column("status", String, nullable=False),
    Column("excerpt", Text, nullable=False),
    Column("source_url", String, nullable=False),
    Column("source_title", String, nullable=False),
    Column("checked_at", String, nullable=False),
    Column("clinical_review", String, nullable=False),
)


# 原始 JSON 记录的版本快照，保留全部字段，不随当前版本更新而覆盖。
knowledge_records = Table(
    "knowledge_record_versions",
    metadata,
    Column("version_id", String, primary_key=True),
    Column("parent_document_id", String, nullable=False),
    Column("dataset_hash", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("line_number", Integer, nullable=False),
    Column("record", JSON, nullable=False),
)
# 从父记录或核对摘要加载的字段文档快照，保存正文、用途与来源元数据。
knowledge_versions = Table(
    "knowledge_document_versions",
    metadata,
    Column("version_id", String, primary_key=True),
    Column("document_id", String, nullable=False),
    Column("parent_document_id", String, nullable=False),
    Column("family", String, nullable=False),
    Column("dataset_hash", String, nullable=False),
    Column("text", Text, nullable=False),
    Column("attributes", JSON, nullable=False),
)


# 追加每次尝试的时间记录，兼容已有任务表，无须改写旧历史。
attempt_runs = Table(
    "analysis_attempt_runs",
    metadata,
    Column("job_id", String, primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("metrics", JSON, nullable=False),
    Column("status", String, nullable=False),
)


def initialize():
    """创建缺失的数据表；不会删除、清空或迁移已有表。"""
    metadata.create_all(engine)


def now():
    """生成带 UTC 时区的 ISO 时间，供数据库记录和验收报告使用。"""
    return datetime.now(timezone.utc).isoformat()


def get_job(job_id: str):
    """按任务编号返回字段字典；任务不存在时返回 None。"""
    with engine.connect() as connection:
        row = (
            connection.execute(select(jobs).where(jobs.c.id == job_id))
            .mappings()
            .first()
        )
        attempt_metrics = connection.execute(
            select(attempt_runs.c.metrics).where(attempt_runs.c.job_id == job_id)
            .order_by(attempt_runs.c.attempt.desc()).limit(1)
        ).scalar_one_or_none()
    if row:
        return {**dict(row), "progress": attempt_metrics.get("progress") if attempt_metrics else None}
    else:
        return None


def update_job(job_id: str, **values):
    """在单次事务中更新指定任务的字段，供任务执行边界维护状态。"""
    with engine.begin() as connection:
        connection.execute(jobs.update().where(jobs.c.id == job_id).values(**values))


def get_turn(job_id: str):
    """读取某轮补充的父链和用户原文；最初分析没有对应补充记录。"""
    with engine.connect() as connection:
        row = (
            connection.execute(select(turns).where(turns.c.job_id == job_id))
            .mappings()
            .first()
        )
    if row:
        return dict(row)
    else:
        return None


def source_reviews():
    """返回当前标记为 source_checked 的记录；数据版本匹配由使用方判断。"""
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(reviews).where(reviews.c.status == "source_checked")
            )
            .mappings()
            .all()
        )
    return {row["document_id"]: dict(row) for row in rows}
