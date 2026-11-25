import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from ollama import ChatResponse
from pydantic import ValidationError

from app import database, vector_store
from app.config import settings
from app.ranking import rrf
from app.schemas import SearchPlan
from app.medlineplus import import_snapshot
from app.rerank import rerank_candidates
from test_api import env
from test_index_versions import Embeddings, seed_review


def test_rrf_counts_block_once_and_preserves_distinct_blocks():
    a, b = {"id": "a"}, {"id": "b"}
    result = rrf([[a, a, b], [b]], [0.5, 0.5])
    assert result[0]["id"] == "b"
    assert next(x for x in result if x["id"] == "a")["fusion_score"] == 0.5 / 61
    assert rrf([[a], [a]], [0.5, 0.5])[0]["fusion_score"] == rrf([[a]], [1])[0]["fusion_score"]


def test_vector_recall_respects_revocation(env):
    _, _, patch = env
    patch.setattr(settings, "review_policy", "reviewed_only")
    seed_review()
    manifest = asyncio.run(vector_store.build_index(Embeddings()))
    result = asyncio.run(vector_store.retrieve(["symptom"], Embeddings(), ["symptom", " SYMPTOM "], limit=12))
    assert result[0]["index_version"] == manifest["index_version"]
    assert result[0]["retrieval"]["queries"] == ["symptom"]
    with database.engine.begin() as c:
        c.execute(database.reviews.update().values(status="revoked"))
    assert asyncio.run(vector_store.retrieve(["symptom"], Embeddings())) == []


def test_query_clues_quote_user_only_and_deduplicate():
    value = {"terms": ["Dizziness", " dizziness "], "queries": ["Dizziness", " dizziness "],
        "clues": [{"kind": "denied", "quote": "没有发烧", "meaning": "明确否认发烧"},
                  {"kind": "uncertain", "quote": "没测", "meaning": "血糖未知"}]}
    plan = SearchPlan.model_validate(value, context={"text": "头晕，没有发烧，血糖没测"})
    assert plan.terms == ["dizziness"] and plan.queries == ["dizziness"]
    value["clues"][0]["quote"] = "你发烧吗？"
    with pytest.raises(ValidationError, match="原文"):
        SearchPlan.model_validate(value, context={"text": "没测"})


def test_official_import_only_summary_and_revocation(env, tmp_path):
    _, _, patch = env
    patch.setattr(settings, "review_policy", "reviewed_only")
    patch.setattr(settings, "evidence_family", "medlineplus_summary")
    xml = tmp_path / "topics.xml"
    xml.write_text('<health-topics date-generated="11/04/2026"><health-topic title="Cough" id="1" language="English" url="https://medlineplus.gov/cough.html"><full-summary>&lt;p&gt;A cough symptom.&lt;/p&gt;</full-summary><site>EXCLUDED EXTERNAL TEXT</site></health-topic></health-topics>', encoding="utf-8")
    release = import_snapshot(xml, {"Cough": "symptom", "Absent": "disease"},
        {"sha256": hashlib.sha256(xml.read_bytes()).hexdigest(), "downloaded_at": "2026-11-05"})
    assert release["missing_titles"] == ["Absent"]
    asyncio.run(vector_store.build_index(Embeddings(), family="medlineplus_summary"))
    source = asyncio.run(vector_store.retrieve(["cough"], Embeddings()))[0]
    assert source["snippet"] == "A cough symptom."
    assert source["topic_type"] == "symptom" and source["source_updated_at"] is None
    (settings.data_dir / "sources/medlineplus/revoked.json").write_text(json.dumps(release["document_versions"]), encoding="utf-8")
    assert asyncio.run(vector_store.retrieve(["cough"], Embeddings())) == []


def test_body_rerank_sees_real_text_and_allows_empty(env):
    _, _, patch = env
    patch.setattr(settings, "reranker", "body_llm")
    seen = []
    class Client:
        async def chat(self, **kwargs):
            seen.append(json.loads(kwargs["messages"][1]["content"]))
            async def chunks():
                yield ChatResponse(message={"role": "assistant", "content": '{"indexes":[]}'}, done=True,
                                   prompt_eval_count=8, eval_count=4, eval_duration=1_000_000_000, done_reason="stop")
            return chunks()
    sources, metrics = asyncio.run(rerank_candidates("头晕，没测血糖", [{"id":"a", "name":"A", "snippet":"actual source"}], Client(), "select"))
    assert seen[0]["candidates"][0]["snippet"] == "actual source"
    assert sources == [] and metrics["reranker"] == "body_llm"


def test_neighbors_keep_same_version_and_citation_offsets(env):
    _, _, patch = env
    patch.setattr(settings, "review_policy", "reviewed_only")
    patch.setattr(settings, "context_neighbors", 1)
    patch.setattr(settings, "chunk_size", 100)
    patch.setattr(settings, "chunk_overlap", 0)
    seed_review()
    text = "First paragraph symptom. " * 30
    with database.engine.begin() as c:
        c.execute(database.reviews.update().values(excerpt=text))
    asyncio.run(vector_store.build_index(Embeddings()))
    results = asyncio.run(vector_store.retrieve(["symptom"], Embeddings(), limit=12))
    assert len(results) > 1
    for result in results:
        for neighbor in result["context"]:
            assert neighbor["document_version"] == result["document_version"]
            start = neighbor["start_index"]
            assert text[start:start + len(neighbor["snippet"])] == neighbor["snippet"]


def test_failed_candidate_update_keeps_published_public_documents(env, tmp_path):
    _, _, patch = env
    patch.setattr(settings, "review_policy", "reviewed_only")
    patch.setattr(settings, "evidence_family", "medlineplus_summary")
    xml = tmp_path / "topics.xml"
    def import_text(text):
        xml.write_text(f'<health-topics date-generated="11/04/2026"><health-topic title="Cough" id="1" language="English" url="https://medlineplus.gov/cough.html"><full-summary>{text}</full-summary></health-topic></health-topics>', encoding="utf-8")
        return import_snapshot(xml, {"Cough": "symptom"}, {"sha256": hashlib.sha256(xml.read_bytes()).hexdigest(), "downloaded_at": "2026-11-05"})
    import_text("Original cough summary.")
    asyncio.run(vector_store.build_index(Embeddings(), family="medlineplus_summary"))
    old_pointer = (settings.data_dir / "indexes/medlineplus_summary.json").read_bytes()
    import_text("Revised cough summary.")
    class Failing(Embeddings):
        async def embed(self, **kwargs):
            raise RuntimeError("build failed")
    with pytest.raises(RuntimeError):
        asyncio.run(vector_store.build_index(Failing(), family="medlineplus_summary"))
    assert (settings.data_dir / "indexes/medlineplus_summary.json").read_bytes() == old_pointer
    assert asyncio.run(vector_store.retrieve(["cough"], Embeddings()))[0]["snippet"] == "Original cough summary."


def test_new_pipeline_rejects_over_budget_without_truncation(env):
    from app.ollama_client import check_prompt_budget
    _, _, patch = env
    patch.setattr(settings, "evidence_family", "medlineplus_summary")
    patch.setattr(settings, "num_ctx", 8192)
    with pytest.raises(ValueError, match="预算"):
        check_prompt_budget([{"role": "user", "content": "原话" * 3000}], {}, 1600)


def test_final_topic_limit_is_applied_after_reranking(env):
    _, _, patch = env
    patch.setattr(settings, "reranker", "none")
    candidates = [{"id": str(i), "parent_document_id": "same" if i < 3 else "other"} for i in range(4)]
    selected, _ = asyncio.run(rerank_candidates("text", candidates, None, "prompt"))
    assert [s["id"] for s in selected] == ["0", "1", "3"]


def test_comprehensive_query_retains_all_reported_terms():
    plan = SearchPlan.model_validate({"terms": ["cough", "dysuria"], "queries": ["cough", "dysuria"]})
    assert plan.queries[0] == "cough, dysuria"
    with pytest.raises(ValidationError, match="原话线索"):
        SearchPlan.model_validate({"terms": ["cough"]}, context={"text": "咳嗽", "require_clues": True})


def test_cross_encoder_revision_checked_at_settings_entry():
    from app.config import Settings
    with pytest.raises(ValidationError, match="revision"):
        Settings(reranker="cross_encoder", cross_encoder_revision="")


def test_public_pipeline_cannot_publish_directions_without_any_sources():
    from app.schemas import Analysis
    from test_api import RESULT
    value = {**RESULT, "status": "analysis_available", "directions": [
        {"name": "guess", "reason": "guess", "limitation": "unknown", "source_ids": []}]}
    with pytest.raises(ValidationError, match="无依据"):
        Analysis.model_validate(value, context={"text": "头晕", "evidence_required": True, "has_sources": False})


@pytest.mark.parametrize("model_name, field", [("SearchPlan", "clues"), ("Analysis", "facts")])
def test_numbered_evidence_restores_original_and_rejects_unknown_ids(model_name, field):
    from app import schemas
    from test_api import RESULT

    model = getattr(schemas, model_name)
    text = "医生好像说有冠心病。\n没注意到出汗，药盒写100 mg。"
    spans = schemas.build_evidence(text)
    schema = schemas.evidence_schema(model, spans)
    assert schema["$defs"]["FactRef"]["properties"]["evidence_id"]["enum"] == ["u1", "u2"]
    assert "quote" not in schema["$defs"]["FactRef"]["properties"]
    value = {"terms": [], "queries": []} if model_name == "SearchPlan" else dict(RESULT)
    value[field] = [{"evidence_id": "u2", "kind": "uncertain", "meaning": "相关情况待核实"}]
    result = schemas.parse_evidence_output(model, json.dumps(value), spans, {"text": text})
    assert getattr(result, field)[0].quote == "没注意到出汗，药盒写100 mg。"
    value[field][0]["evidence_id"] = "question1"
    with pytest.raises(ValidationError, match="原话编号") as error:
        schemas.parse_evidence_output(model, json.dumps(value), spans, {"text": text})
    assert error.value.errors()[0]["loc"] == (field, 0, "evidence_id")


@pytest.mark.parametrize("value, location", [
    ({"terms": []}, ("clues",)),
    ({"terms": [], "clues": [], "queries": None}, ("queries",)),
    ({"terms": [], "clues": [None], "queries": []}, ("clues", 0)),
    ({"terms": [], "clues": [{"kind": "reported", "meaning": "咳嗽"}], "queries": []},
     ("clues", 0, "evidence_id")),
    ({"terms": [], "clues": [{"evidence_id": "u1", "kind": "reported", "meaning": "咳嗽",
                               "quote": "模型自行转写"}], "queries": []},
     ("clues", 0, "quote")),
])
def test_model_output_structure_errors_have_field_paths(value, location):
    from app.schemas import build_evidence, parse_evidence_output

    with pytest.raises(ValidationError) as error:
        parse_evidence_output(SearchPlan, json.dumps(value), build_evidence("咳嗽"), {"text": "咳嗽"})
    assert location in [item["loc"] for item in error.value.errors()]


def test_numbered_analysis_preserves_business_rules_and_nested_models():
    from app.schemas import Analysis, Direction, build_evidence, parse_evidence_output
    from test_api import RESULT

    spans = build_evidence("咳嗽，但没注意到发热。")
    value = {**RESULT, "status": "analysis_available", "facts": [
        {"evidence_id": "u1", "kind": "reported", "meaning": "用户报告咳嗽"},
        {"evidence_id": "u1", "kind": "uncertain", "meaning": "是否发热尚不确定"},
    ], "directions": [{"name": "方向", "reason": "理由", "limitation": "局限", "source_ids": []}]}
    context = {"text": spans["u1"], "evidence_required": True, "has_sources": True}
    result = parse_evidence_output(Analysis, json.dumps(value), spans, context)
    assert isinstance(result.directions[0], Direction)
    assert result.facts[0].quote == result.facts[1].quote == spans["u1"]
    assert [fact.kind for fact in result.facts] == ["reported", "uncertain"]
    context["allowed_source_ids"] = {"reference1"}
    value["directions"][0]["source_ids"] = ["u1"]
    with pytest.raises(ValidationError, match="未提供") as error:
        parse_evidence_output(Analysis, json.dumps(value), spans, context)
    assert error.value.errors()[0]["loc"] == ("directions", 0, "source_ids")
    value["directions"][0]["source_ids"] = ["reference1"]
    assert parse_evidence_output(Analysis, json.dumps(value), spans, context).directions[0].source_ids == ["reference1"]
    context["has_sources"] = False
    with pytest.raises(ValidationError, match="无依据"):
        parse_evidence_output(Analysis, json.dumps(value), spans, context)
    context["has_sources"] = True
    value["status"] = "needs_clarification"
    with pytest.raises(ValidationError, match="疾病方向"):
        parse_evidence_output(Analysis, json.dumps(value), spans, context)
