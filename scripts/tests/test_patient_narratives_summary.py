"""用临时占位材料核对评测分母；不读取项目病例或运行模型。"""

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "summarize_patient_narratives.py"
spec = importlib.util.spec_from_file_location("patient_narratives_summary", SCRIPT)
summary_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary_module)


def completed_record(case_id="fixture-1", *, origin="model", status="succeeded", facts=None, directions=None):
    facts = [{"quote": "占位事实", "meaning": "占位事实", "kind": "reported"}] if facts is None else facts
    result = {
        "origin": origin, "elapsed_seconds": 1.5, "sources": [{"id": "fixture-source"}],
        "analysis": {"status": "urgent_attention" if origin == "safety_rule" else "needs_clarification",
                     "facts": facts, "directions": directions or []}, "clues": [],
    } if status == "succeeded" else None
    return {"case_id": case_id, "turn": 0, "split": "development", "category": "routine",
            "action": "initial", "execution_status": "terminal" if status in ("succeeded", "failed") else "observation_interrupted",
            "http_status": 200 if origin == "safety_rule" else 202, "wall_seconds": 2.0,
            "job": {"id": case_id, "status": status, "attempts": 0 if origin == "safety_rule" else 1,
                    "result": result, "error": "fixture-service-failure" if status == "failed" else None},
            "attempts": [{"status": "failed", "metrics": {"error_stage": "fixture-generation"}}] if status == "failed" else [],
            "conversation": [{"id": case_id}], "turn_metadata": None}


@pytest.fixture
def run_fixture(tmp_path, monkeypatch):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    monkeypatch.setattr(summary_module, "CASES", cases_dir)

    def make(records, case_ids=("fixture-1",), steps=(), reviews=()):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        cases = [{"case_id": case_id, "split": "development", "category": "routine",
                  "initial_text": "占位事实", "steps": list(steps)} for case_id in case_ids]
        oracles = [{"case_id": case_id, "atomic_facts": [
            {"id": "fixture-key-fact", "importance": "key", "content": "占位事实",
             "applies_turns": list(range(1 + len(steps)))},
        ], "safety": {"expected": "not_applicable"}} for case_id in case_ids]
        for name, rows in (("development.inputs.jsonl", cases), ("development.oracles.jsonl", oracles),
                           ("holdout.inputs.jsonl", []), ("holdout.oracles.jsonl", [])):
            with (cases_dir / name).open("x", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (run_dir / "results.jsonl").open("x", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        if reviews:
            with (run_dir / "semantic_review_fixture.jsonl").open("x", encoding="utf-8") as stream:
                for row in reviews:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary_module.summarize(run_dir)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assessments = [json.loads(line) for line in (run_dir / "assessments.jsonl").read_text(encoding="utf-8").splitlines()]
        return summary, assessments

    return make


def test_rule_without_facts_has_no_extraction_denominator_but_keeps_urgent_review(run_fixture):
    summary, assessments = run_fixture([completed_record(origin="safety_rule", facts=[])])
    coverage = next(row for row in assessments if row["dimension"] == "key_fact_coverage")
    fidelity = next(row for row in assessments if row["dimension"] == "fact_fidelity")
    urgent = next(row for row in assessments if row["dimension"] == "urgent_behavior")
    assert coverage["status"] == fidelity["status"] == "not_applicable"
    assert urgent["status"] == "not_evaluated"
    assert summary["assessment_counts"]["key_fact_coverage"]["applicable_items"] == 0
    assert summary["assessment_counts"]["key_fact_coverage"]["correct_over_all_applicable_facts"] is None
    assert summary["groups"]["development"]["origins"]["safety_rule"]["rule_immediate_success"] == 1


def test_service_failure_is_engineering_failure_and_unknown_semantic_output(run_fixture):
    summary, assessments = run_fixture([completed_record(status="failed")])
    origin = summary["groups"]["development"]["origins"]
    assert origin["no_success_output"]["failed"] == 1
    assert origin["model"]["failed"] == 0
    route = summary["groups"]["development"]["execution_routes"]["model_queue"]
    assert route["accepted_tasks"] == route["failed"] == 1
    assert route["business_success_rate"] == 0
    assert next(row for row in assessments if row["dimension"] == "task_completion")["status"] == "fail"
    assert next(row for row in assessments if row["dimension"] == "key_fact_coverage")["status"] == "not_evaluated"
    assert summary["assessment_counts"]["fact_fidelity"]["applicable_items"] == 0
    assert summary["assessment_counts"]["fact_fidelity"]["rounds_without_success_output"] == 1
    assert summary["assessment_counts"]["fact_fidelity"]["pass_rate_judged"] is None


def test_observation_interruption_is_not_a_terminal_failure(run_fixture):
    summary, assessments = run_fixture([completed_record(status="running")])
    group = summary["groups"]["development"]
    assert group["accepted_tasks"] == group["unfinished_accepted_tasks"] == 1
    assert group["origins"]["no_success_output"]["failed"] == 0
    assert next(row for row in assessments if row["dimension"] == "task_completion")["status"] == "not_evaluated"
    assert summary["unexpected_interrupted_cases"] == 1
    assert summary["completed_by_protocol_cases"] == 0


def test_api_rejection_does_not_inflate_valid_business_task_denominator(run_fixture):
    rejection = {"case_id": "fixture-1", "turn": 0, "split": "development", "category": "routine",
                 "action": "initial", "execution_status": "api_rejected", "http_status": 422,
                 "wall_seconds": 0.01}
    summary, _ = run_fixture([rejection])
    group = summary["groups"]["development"]
    assert group["submitted_requests"] == group["api_rejected_requests"] == 1
    assert group["accepted_tasks"] == 0
    assert group["business_success_rate"] is None
    assert summary["unexpected_interrupted_cases"] == 1


def test_missing_records_preserve_planned_case_and_fact_denominators(run_fixture):
    summary, _ = run_fixture([], case_ids=("fixture-1", "fixture-2"))
    assert summary["planned_cases"] == summary["planned_turns"] == 2
    assert summary["recorded_turns"] == summary["started_cases"] == 0
    assert summary["not_started_cases"] == summary["missing_result_records"] == 2
    coverage = summary["assessment_counts"]["key_fact_coverage"]
    assert coverage["not_evaluated"] == coverage["applicable_items"] == 2
    assert coverage["judgment_coverage"] == 0
    assert coverage["pass_rate_judged"] is None


def test_predefined_missing_target_ends_branch_without_invented_submissions(run_fixture):
    records = [completed_record()]
    for turn in (1, 2):
        records.append({"case_id": "fixture-1", "turn": turn, "split": "development", "category": "routine",
                        "action": "correct", "execution_status": "not_reached", "reason": "target_missing"})
    summary, _ = run_fixture(records, steps=({"action": "correct"}, {"action": "correct"}))
    assert summary["planned_turns"] == summary["recorded_turns"] == 3
    assert summary["completed_by_protocol_cases"] == summary["fully_accounted_cases"] == 1
    assert summary["unexpected_interrupted_cases"] == 0
    assert summary["groups"]["development"]["accepted_tasks"] == 1
    assert summary["groups"]["development"]["not_reached_reasons"] == {"target_missing": 2}


def test_fact_coverage_reports_judged_accuracy_and_all_applicable_coverage_separately(run_fixture):
    records = [completed_record("fixture-1"), completed_record("fixture-2"), completed_record("fixture-3", status="failed")]
    reviews = [{"case_id": case_id, "turn": 0, "dimension": "key_fact_coverage",
                "item": "fixture-key-fact", "status": status, "reason": "fixture judgment"}
               for case_id, status in (("fixture-1", "pass"), ("fixture-2", "fail"))]
    summary, _ = run_fixture(records, case_ids=("fixture-1", "fixture-2", "fixture-3"), reviews=reviews)
    coverage = summary["assessment_counts"]["key_fact_coverage"]
    assert coverage["pass_rate_judged"] == 0.5
    assert coverage["judgment_coverage"] == 2 / 3
    assert coverage["correct_over_all_applicable_facts"] == 1 / 3
    assert summary["groups"]["development"]["execution_routes"]["model_queue"]["business_success_rate"] == 2 / 3


def test_fidelity_and_citation_support_count_actual_items_with_original_indices(run_fixture):
    facts = [{"quote": "占位事实", "kind": "reported", "meaning": label} for label in ("事实一", "事实二")]
    directions = [{"source_ids": []}, {"source_ids": ["fixture-source"]}, {"source_ids": ["fixture-source"]}]
    record = completed_record(facts=facts, directions=directions)
    record["job"]["result"]["clues"] = facts
    summary, assessments = run_fixture([record])
    fidelity = [row for row in assessments if row["dimension"] == "fact_fidelity"]
    support = [row for row in assessments if row["dimension"] == "source_support"]
    clues = [row for row in assessments if row["dimension"] == "clue_fidelity"]
    assert [row["item"] for row in fidelity] == [0, 1]
    assert [row["item"] for row in support] == [1, 2]
    assert [row["item"] for row in clues] == [0, 1]
    assert summary["assessment_counts"]["fact_fidelity"]["applicable_items"] == 2
    assert summary["assessment_counts"]["source_support"]["applicable_items"] == 2
    assert summary["assessment_counts"]["clue_fidelity"]["applicable_items"] == 2
    assert summary["assessment_counts"]["clue_fidelity"]["measurement_unit"] == "output_clue"


def test_repeated_summary_does_not_append_duplicate_assessments(run_fixture):
    run_fixture([completed_record()])
    run_dir = summary_module.CASES.parent / "run"
    original = (run_dir / "assessments.jsonl").read_bytes()
    with pytest.raises(FileExistsError):
        summary_module.summarize(run_dir)
    assert (run_dir / "assessments.jsonl").read_bytes() == original


def test_null_and_missing_item_match_the_same_round_level_review(run_fixture):
    review = {"case_id": "fixture-1", "turn": 0, "dimension": "uncertainty", "item": None,
              "status": "pass", "reason": "fixture round-level judgment"}
    summary, assessments = run_fixture([completed_record()], reviews=[review])
    row = next(item for item in assessments if item["dimension"] == "uncertainty")
    assert row["status"] == "pass"
    assert summary["assessment_counts"]["uncertainty"]["judged_items"] == 1


def test_empty_citations_are_not_counted_as_valid_citation_membership(run_fixture):
    directions = [{"source_ids": []}, {"source_ids": ["fixture-source"]}, {"source_ids": ["not-provided"]}]
    summary, assessments = run_fixture([completed_record(directions=directions)])
    membership = [row for row in assessments if row["dimension"] == "source_membership"]
    assert [row["status"] for row in membership] == ["not_applicable", "pass", "fail"]
    metric = summary["assessment_counts"]["source_membership"]
    assert metric["applicable_items"] == metric["judged_items"] == 2
    assert metric["pass_rate_judged"] == 0.5


def test_quote_and_source_item_denominators_exclude_unavailable_round_placeholders(run_fixture):
    facts = [{"quote": "占位事实", "kind": "reported", "meaning": label} for label in ("事实一", "事实二")]
    success = completed_record("fixture-1", facts=facts, directions=[
        {"source_ids": ["fixture-source"]}, {"source_ids": []},
    ])
    success["job"]["result"]["clues"] = facts[:1]
    records = [success, completed_record("fixture-2", status="failed"),
               completed_record("fixture-3", origin="safety_rule", facts=[])]
    summary, _ = run_fixture(records, case_ids=("fixture-1", "fixture-2", "fixture-3"))
    metrics = summary["assessment_counts"]
    for dimension, expected in (("quote_facts", 2), ("quote_clues", 1), ("source_membership", 1)):
        assert metrics[dimension]["judged_items"] == expected
        assert metrics[dimension]["applicable_items"] == expected
        assert metrics[dimension]["judgment_coverage"] == 1
        assert metrics[dimension]["rounds_without_success_output"] == 1
        assert metrics[dimension]["not_applicable_rounds"] == 1
    assert metrics["source_membership"]["not_applicable"] == 1  # 一个实际方向没有引用。
    assert metrics["conversation_chain"]["applicable_items"] == 3
    assert metrics["output_schema"]["applicable_items"] == 3
