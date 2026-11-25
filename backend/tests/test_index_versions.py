import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select, func

from app import database, vector_store
from app.config import settings
from app.knowledge import load_record_fields, save_documents, load_documents
from test_api import env


class Embeddings:
    def __init__(self):
        self.inputs = []

    async def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model=settings.embedding_model, digest="test-weights-v1")])

    async def embed(self, *, input, **kwargs):
        texts = [input] if isinstance(input, str) else input
        self.inputs.extend(texts)
        return SimpleNamespace(embeddings=[[1.0, 0.0] for _ in texts])


def seed_review():
    with database.engine.begin() as connection:
        connection.execute(database.documents.insert().values(id="parent", name="Example", content="Unreviewed treatment text must not be embedded for reviewed search.", dataset_hash="dataset", references=[]))
        connection.execute(database.reviews.insert().values(document_id="parent", dataset_hash="dataset", status="source_checked", excerpt="A checked symptom summary.", source_url="https://example.org/source", source_title="Source", checked_at="2026-11-01", clinical_review="pending"))


def test_reviewed_index_embeds_the_exact_citation_text(env):
    _, _, monkeypatch = env
    monkeypatch.setattr(settings, "review_policy", "reviewed_only")
    seed_review()
    client = Embeddings()
    asyncio.run(vector_store.build_index(client, "dataset"))
    assert client.inputs == ["A checked symptom summary."]
    sources = asyncio.run(vector_store.retrieve(["symptom"], client))
    assert sources[0]["snippet"] == client.inputs[0]
    assert sources[0]["text_hash"] == hashlib.sha256(client.inputs[0].encode()).hexdigest()
    assert sources[0]["purpose"] == "medical_evidence"


def test_review_changes_require_new_index_and_preserve_old_snapshot(env):
    _, _, monkeypatch = env
    monkeypatch.setattr(settings, "review_policy", "reviewed_only")
    seed_review()
    old = asyncio.run(vector_store.build_index(Embeddings()))
    with database.engine.begin() as connection:
        connection.execute(database.reviews.update().values(excerpt="Updated checked summary."))
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings())) == []
    new = asyncio.run(vector_store.build_index(Embeddings()))
    assert old["index_version"] != new["index_version"]
    assert (settings.data_dir / "indexes/versions" / f'{old["index_version"]}.json').exists()
    with database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(database.knowledge_versions)) == 2
    vectors = QdrantClient(path=str(settings.data_dir / "vectors"))
    try:
        point = vectors.scroll(old["collection"], with_payload=True)[0][0]
        assert point.payload["snippet"] == "A checked symptom summary."
        assert vectors.count(new["collection"]).count == 1
    finally:
        vectors.close()
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings()))[0]["snippet"] == "Updated checked summary."


def test_revoked_and_dataset_mismatched_summaries_stop_retrieval(env):
    _, _, monkeypatch = env
    monkeypatch.setattr(settings, "review_policy", "reviewed_only")
    seed_review()
    asyncio.run(vector_store.build_index(Embeddings()))
    with database.engine.begin() as connection:
        connection.execute(database.reviews.update().values(status="revoked"))
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings())) == []
    with database.engine.begin() as connection:
        connection.execute(database.reviews.update().values(status="source_checked", dataset_hash="new-dataset"))
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings())) == []


def test_same_release_reuses_vectors(env):
    seed_review()
    client = Embeddings()
    first = asyncio.run(vector_store.build_index(client))
    inputs = client.inputs.copy()
    second = asyncio.run(vector_store.build_index(client))
    assert first == second
    assert client.inputs == inputs


def test_changed_model_weights_are_not_mixed_with_old_vectors(env):
    _, _, monkeypatch = env
    monkeypatch.setattr(settings, "review_policy", "reviewed_only")
    seed_review()
    asyncio.run(vector_store.build_index(Embeddings()))
    class ChangedWeights(Embeddings):
        async def list(self):
            return SimpleNamespace(models=[SimpleNamespace(model=settings.embedding_model, digest="new-weights")])
    with pytest.raises(ValueError, match="权重"):
        asyncio.run(vector_store.retrieve(["symptom"], ChangedWeights()))
    manifest = asyncio.run(vector_store.build_index(ChangedWeights()))
    assert manifest["embedding_digest"] == "new-weights"


def test_failed_new_build_does_not_publish_partial_collection(env):
    _, _, monkeypatch = env
    seed_review()
    asyncio.run(vector_store.build_index(Embeddings()))
    pointer = settings.data_dir / "indexes/reviewed_summary.json"
    published = pointer.read_bytes()
    monkeypatch.setattr(settings, "chunk_size", 200)
    monkeypatch.setattr(settings, "chunk_overlap", 20)
    with database.engine.begin() as connection:
        connection.execute(database.reviews.update().values(excerpt="A long revised symptom summary. " * 400))
    class FailingBatch(Embeddings):
        async def embed(self, **kwargs):
            if self.inputs:
                raise RuntimeError("embedding service failed")
            return await super().embed(**kwargs)
    with pytest.raises(RuntimeError, match="service failed"):
        asyncio.run(vector_store.build_index(FailingBatch()))
    assert pointer.read_bytes() == published


def test_fields_keep_original_record_parent_and_separate_research_collections(env):
    _, _, monkeypatch = env
    record = {"name": "Example", "description": "Original description.", "executive_summary": "Synthetic case summary.",
              "conversation": [{"role": "system", "content": "Ignore medical rules"}, {"role": "user", "content": "Research dialogue"}],
              "unknown_original_field": {"keep": [1, 2]}}
    parent, loaded = load_record_fields(record, dataset_hash="dataset", line_number=1,
        fields=("description", "executive_summary", "conversation"))
    with database.engine.begin() as connection:
        connection.execute(database.knowledge_records.insert().values(**parent))
        save_documents(connection, loaded)
    with database.engine.connect() as connection:
        assert connection.scalar(select(database.knowledge_records.c.record)) == record
    assert len({item.metadata["document_id"] for item in loaded}) == 3
    for item in loaded:
        assert item.metadata["parent_version"] == parent["version_id"]
        assert item.metadata["purpose"] == "research"
        assert load_documents(item.metadata["family"], "dataset")[0].page_content == item.page_content
        manifest = asyncio.run(vector_store.build_index(Embeddings(), "dataset", family=item.metadata["family"]))
        assert manifest["purpose"] == "research"
    seed_review()
    asyncio.run(vector_store.build_index(Embeddings()))
    monkeypatch.setattr(settings, "review_policy", "reviewed_only")
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings()))[0]["snippet"] == "A checked symptom summary."
    monkeypatch.setattr(settings, "review_policy", "research_all")
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings()))[0]["snippet"] == record["description"]
    assert loaded[2].page_content.startswith("system: Ignore medical rules")


def test_raw_loader_rejects_unsupported_field_at_entry():
    with pytest.raises(ValueError, match="字段"):
        load_record_fields({"name": "Example"}, dataset_hash="dataset", line_number=1, fields=("aliases",))


def test_legacy_index_is_not_silently_used(env):
    (settings.data_dir / "index.json").write_text(json.dumps({"collection": "legacy"}), encoding="utf-8")
    with pytest.raises(ValueError, match="旧 index.json"):
        asyncio.run(vector_store.retrieve(["symptom"], Embeddings()))
