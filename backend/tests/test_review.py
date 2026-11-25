import asyncio
from app import database, vector_store
from app.config import settings
from test_api import env
from test_index_versions import Embeddings, seed_review


def test_default_policy_indexes_only_current_checked_summaries(env):
    _,_,monkeypatch=env
    monkeypatch.setattr(settings,'review_policy','reviewed_only')
    seed_review()
    with database.engine.begin() as connection:
        connection.execute(database.documents.insert().values(id='unchecked', name='Unchecked', content='unsafe raw advice', dataset_hash='dataset'))
    manifest=asyncio.run(vector_store.build_index(Embeddings()))
    assert manifest['documents']==1
    sources=asyncio.run(vector_store.retrieve(['wheeze'],Embeddings()))
    assert len(sources)==1 and sources[0]['parent_document_id']=='parent'
    assert sources[0]['snippet']=='A checked symptom summary.'
    assert sources[0]['clinical_review']=='pending'


def test_knowledge_status_does_not_claim_medical_approval(env):
    api,_,_=env
    status=api.get('/api/knowledge/review').json()
    assert status['clinical_review']=='pending'
    assert status['source_checked']==0
