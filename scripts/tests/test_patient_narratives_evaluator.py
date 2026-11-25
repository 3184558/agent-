"""评测器自身的目标定位与冻结测试；不读取真实病例或调用模型。"""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "evaluate_patient_narratives.py"
spec = importlib.util.spec_from_file_location("patient_narratives_evaluator", SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def parent(*, questions=(), facts=()):
    return {"result": {"analysis": {
        "missing_information": list(questions), "facts": list(facts),
    }}}


@pytest.mark.parametrize("choices", [[], ["有没有服药？", "什么时候开始？"]])
def test_missing_target_is_not_replaced_with_first_question(choices):
    index, match = evaluator.select_target(choices, ["体温", "发热"])
    assert index is None
    assert match["status"] == "target_missing"


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_score_stays_ambiguous_in_either_order(reverse):
    choices = ["咳嗽持续多久？", "咳嗽有没有痰？"]
    if reverse:
        choices.reverse()
    index, match = evaluator.select_target(choices, ["咳嗽"])
    assert index is None
    assert match["status"] == "ambiguous_target"
    assert {item["choice"] for item in match["candidates"]} == set(choices)


@pytest.mark.parametrize("reverse", [False, True])
def test_unique_best_match_uses_actual_index_in_either_order(reverse):
    choices = ["咳嗽有没有痰？", "咳嗽持续多久？"]
    if reverse:
        choices.reverse()
    index, match = evaluator.select_target(choices, ["咳嗽", "多久"])
    assert index == choices.index("咳嗽持续多久？")
    assert match["matched_keywords"] == ["咳嗽", "多久"]


def test_correction_targets_meaning_when_facts_share_one_quote():
    quote = "咳嗽两天，气短大概三周了。"
    facts = [
        {"kind": "reported", "quote": quote, "meaning": "咳嗽持续两天"},
        {"kind": "uncertain", "quote": quote, "meaning": "气短约持续三周"},
    ]
    payload, matching = evaluator.submit_step(
        parent(facts=facts),
        {"action": "correct", "target_keywords": ["气短"], "text": "这一项其实已经一个月了"},
    )
    assert payload == {"action": "correct", "index": 1, "text": "这一项其实已经一个月了"}
    assert matching[0]["choice"] == facts[1]


def test_shared_quote_does_not_make_a_missing_meaning_match_valid():
    payload, matching = evaluator.submit_step(
        parent(facts=[{"quote": "咳嗽和气短", "meaning": "咳嗽持续两天"}]),
        {"action": "correct", "target_keywords": ["气短"], "text": "已有一个月"},
    )
    assert payload is None
    assert matching[0]["status"] == "target_missing"


def test_batch_does_not_submit_partial_answers_when_one_target_is_missing():
    payload, matching = evaluator.submit_step(
        parent(questions=["持续多久？"]),
        {"action": "batch", "items": [
            {"action": "answer", "target_keywords": ["多久"], "text": "两天"},
            {"action": "answer", "target_keywords": ["服药"], "text": "没有"},
        ]},
    )
    assert payload is None
    assert [item["status"] for item in matching] == ["matched", "target_missing"]


def test_batch_rejects_two_semantic_targets_that_resolve_to_same_index():
    payload, matching = evaluator.submit_step(
        parent(questions=["体温多少，是否发热？"]),
        {"action": "batch", "items": [
            {"action": "answer", "target_keywords": ["体温"], "text": "36.7度"},
            {"action": "skip", "target_keywords": ["发热"]},
        ]},
    )
    assert payload is None
    assert matching[-1]["status"] == "ambiguous_target"


def test_batch_preserves_real_question_indices_and_skip_semantics():
    payload, matching = evaluator.submit_step(
        parent(questions=["体温多少？", "服用过什么药？", "持续多久？"]),
        {"action": "batch", "items": [
            {"action": "answer", "target_keywords": ["多久"], "text": "两天"},
            {"action": "skip", "target_keywords": ["服用"]},
        ]},
    )
    assert payload == {"action": "batch", "items": [
        {"action": "answer", "index": 2, "text": "两天"},
        {"action": "skip", "index": 1},
    ]}
    assert [item["status"] for item in matching] == ["matched", "matched"]


@pytest.fixture
def frozen_files(tmp_path, monkeypatch):
    # 纯临时占位材料，不加载项目中真实的开发集或锁定验收集。
    monkeypatch.setattr(evaluator, "CASES", tmp_path)
    material = tmp_path / "temporary.inputs.jsonl"
    material.write_text('{"case_id":"fixture-only"}\n', encoding="utf-8")
    protocol = {"version": "fixture", "files": {material.name: evaluator.digest(material)}}
    evaluator.save_json(tmp_path / "protocol.json", protocol)
    evaluator.save_json(tmp_path / "protocol.freeze.json", {
        "protocol.json": evaluator.digest(tmp_path / "protocol.json"),
    })
    return tmp_path, material, protocol


def test_unchanged_frozen_materials_are_accepted(frozen_files):
    _, _, protocol = frozen_files
    assert evaluator.verify_frozen() == protocol


def test_input_tampering_is_detected(frozen_files):
    _, material, _ = frozen_files
    material.write_text('{"case_id":"changed-fixture"}\n', encoding="utf-8")
    with pytest.raises(AssertionError):
        evaluator.verify_frozen()


def test_protocol_tampering_is_detected(frozen_files):
    directory, _, _ = frozen_files
    (directory / "protocol.json").write_text('{"version":"changed"}', encoding="utf-8")
    with pytest.raises(AssertionError):
        evaluator.verify_frozen()


def test_missing_frozen_material_is_an_error(frozen_files):
    directory, _, _ = frozen_files
    protocol = {"files": {"never-created.jsonl": "not-a-real-hash"}}
    # 在全新的临时目录构造独立冻结记录，不删除已有材料。
    another = directory / "missing-material"
    another.mkdir()
    evaluator.save_json(another / "protocol.json", protocol)
    evaluator.save_json(another / "protocol.freeze.json", {
        "protocol.json": evaluator.digest(another / "protocol.json"),
    })
    original_cases = evaluator.CASES
    evaluator.CASES = another
    try:
        with pytest.raises(FileNotFoundError):
            evaluator.verify_frozen()
    finally:
        evaluator.CASES = original_cases


@pytest.fixture
def phase_environment(tmp_path, monkeypatch):
    """用临时元数据验证阶段门禁，不导入产品模块或启动真实Worker。"""
    import httpx

    run_dir = tmp_path / "evaluation/runs/patient_narratives_v1/fixture"
    index_dir = run_dir / "data/indexes"
    index_dir.mkdir(parents=True)
    cases = tmp_path / "cases"
    cases.mkdir()
    evaluator.save_json(cases / "protocol.json", {"fixture": True})
    index = {"collection": "fixture-collection"}
    evaluator.save_json(index_dir / "reviewed_summary.json", index)
    frozen_settings = {"data_dir": str(run_dir / "data"), "model": "fixture-model",
                       "embedding_model": "fixture-embedding", "num_ctx": 4096,
                       "timeout": 180.0}
    actual_settings = {**frozen_settings, "ollama_url": "http://fixture.invalid"}
    settings = SimpleNamespace(data_dir=run_dir / "data", model="fixture-model",
                               embedding_model="fixture-embedding",
                               ollama_url="http://fixture.invalid",
                               model_dump=lambda **_: dict(actual_settings))
    monkeypatch.setitem(sys.modules, "app.config", SimpleNamespace(settings=settings))
    models = [{"name": "fixture-model", "digest": "fixture-model-digest"},
              {"name": "fixture-embedding", "digest": "fixture-embedding-digest"}]
    current_models = [dict(item) for item in models]

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            assert url == "http://fixture.invalid/api/tags"
            return SimpleNamespace(json=lambda: {"models": current_models})

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator, "CASES", cases)
    monkeypatch.setenv("HEALTH_DATA_DIR", str(run_dir / "data"))
    monkeypatch.setattr(evaluator, "verify_frozen", lambda: {"fixture": True})
    called = []
    monkeypatch.setattr(evaluator, "run_phase", lambda *args: called.append(args))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--phase", "development",
                                      "--run-dir", str(run_dir)])
    evaluator.save_json(run_dir / "run_metadata.json", {
        "settings": frozen_settings, "protocol_hash": evaluator.digest(cases / "protocol.json"),
        "index": index, "model_digests": models, "runner_hash": evaluator.digest(SCRIPT),
        "code_hashes": {},
    })
    return actual_settings, current_models, called


def test_unchanged_configuration_allows_the_next_phase(phase_environment):
    _, _, called = phase_environment
    evaluator.main()
    assert len(called) == 1


@pytest.mark.parametrize("name,value", [
    ("num_ctx", 8192), ("timeout", 360.0), ("model", "other-fixture-model"),
])
def test_changed_settings_cannot_be_joined_into_same_run(phase_environment, name, value):
    actual_settings, _, called = phase_environment
    actual_settings[name] = value
    with pytest.raises(AssertionError, match="配置"):
        evaluator.main()
    assert not called


def test_changed_model_digest_cannot_be_joined_into_same_run(phase_environment):
    _, current_models, called = phase_environment
    current_models[0]["digest"] = "changed-fixture-model-digest"
    with pytest.raises(AssertionError, match="权重"):
        evaluator.main()
    assert not called
