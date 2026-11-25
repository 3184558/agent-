"""汇总真实运行的确定性检查；语义结论读取单独审阅文件，不以关键词冒充医学判分。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_patient_narratives import CASES, read_jsonl, save_json


def summarize(run_dir):
    records = read_jsonl(run_dir / "results.jsonl")
    cases = {r["case_id"]: r for split in ("development", "holdout") for r in read_jsonl(CASES / f"{split}.inputs.jsonl")}
    oracles = {r["case_id"]: r for split in ("development", "holdout") for r in read_jsonl(CASES / f"{split}.oracles.jsonl")}
    # 缺失记录仍占计划分母，避免只汇总已成功输出的简单病例。
    recorded_keys = {(r["case_id"], r["turn"]) for r in records}
    missing_records = [
        {"case_id": case_id, "turn": turn, "split": case["split"], "category": case["category"],
         "action": "initial" if turn == 0 else case["steps"][turn - 1]["action"],
         "execution_status": "not_recorded", "reason": "results_not_recorded"}
        for case_id, case in cases.items() for turn in range(1 + len(case["steps"]))
        if (case_id, turn) not in recorded_keys
    ]
    assessments, rounds = [], []
    for record in [*records, *missing_records]:
        case_id, turn = record["case_id"], record["turn"]
        base = {"case_id": case_id, "turn": turn, "split": record["split"], "reviewer": "deterministic_evaluator"}
        job = record.get("job", {})
        result = job.get("result")
        round_item = {**base, "category": record["category"], "execution_status": record["execution_status"],
                      "reason": record.get("reason"), "status": job.get("status"), "origin": result.get("origin") if result else None,
                      "wall_seconds": record.get("wall_seconds"), "attempts": job.get("attempts"),
                      "error": job.get("error"), "attempt_runs": record.get("attempts", []),
                      "submitted_request": "http_status" in record,
                      "accepted_task": record.get("http_status") in (200, 202) and bool(job.get("id")),
                      "http_status": record.get("http_status"),
                      "execution_route": "model_queue" if record.get("http_status") == 202 else
                      "safety_rule" if record.get("http_status") == 200 and result and result["origin"] == "safety_rule" else "unknown",
                      "successful_attempt_seconds": result.get("elapsed_seconds") if result else None}
        rounds.append(round_item)
        task_status = ("pass" if job.get("status") == "succeeded" else
                       "fail" if job.get("status") == "failed" or record["execution_status"] == "api_rejected" else
                       "not_evaluated")
        assessments.append({**base, "dimension": "task_completion", "status": task_status,
                            "evidence": {"http_status": record.get("http_status"), "job_status": job.get("status"), "reason": record.get("reason")}})
        facts = result["analysis"]["facts"] if result else []
        if result:
            metadata = record.get("turn_metadata")
            evidence = metadata["evidence_text"] if metadata else cases[case_id]["initial_text"]
            for field, values in (("facts", facts), ("clues", result.get("clues", []))):
                for index, fact in enumerate(values):
                    assessments.append({**base, "dimension": f"quote_{field}", "item": index,
                                        "measurement_unit": "fact_quote" if field == "facts" else "clue_quote",
                                        "status": "pass" if fact["quote"] in evidence else "fail", "input_evidence": evidence, "output_evidence": fact["quote"]})
                if not values:
                    assessments.append({**base, "dimension": f"quote_{field}", "measurement_unit": "round_scope",
                                        "status": "not_applicable", "reason": "本轮成功输出中没有该类待查引文"})
            source_ids = {s["id"] for s in result["sources"]}
            for index, direction in enumerate(result["analysis"]["directions"]):
                assessments.append({**base, "dimension": "source_membership", "item": index,
                                    "measurement_unit": "direction_source_ids",
                                    "status": "not_applicable" if not direction["source_ids"] else
                                    "pass" if set(direction["source_ids"]) <= source_ids else "fail", "output_evidence": direction})
            if not result["analysis"]["directions"]:
                assessments.append({**base, "dimension": "source_membership", "measurement_unit": "round_scope",
                                    "status": "not_applicable", "reason": "本轮成功输出没有疾病方向或待查来源编号"})
            chain = record["conversation"]
            valid_chain = len(chain) == turn + 1 and chain[-1]["id"] == job["id"]
            if turn:
                valid_chain = valid_chain and metadata["parent_id"] == record["parent_id"] and metadata["root_id"] == chain[0]["id"]
            assessments.append({**base, "dimension": "conversation_chain", "status": "pass" if valid_chain else "fail", "evidence": chain})
            assessments.append({**base, "dimension": "output_schema", "status": "pass", "reason": "真实API成功读取已按当前JobResponse/Analysis验证的结果；只证明契约合法"})
        else:
            for dimension in ("quote_facts", "quote_clues", "source_membership", "conversation_chain", "output_schema"):
                row = {**base, "dimension": dimension, "status": "not_evaluated", "reason": "没有可评价的成功输出"}
                if dimension in ("quote_facts", "quote_clues", "source_membership"):
                    row["measurement_unit"] = "round_scope"
                assessments.append(row)
        rule_without_facts = bool(result and result["origin"] == "safety_rule" and not facts)
        for fact in oracles[case_id]["atomic_facts"]:
            applicable = turn in fact.get("applies_turns", [0])
            assessments.append({**base, "dimension": "key_fact_coverage" if fact["importance"] == "key" else "supporting_fact_coverage",
                                "item": fact["id"], "status": "not_evaluated" if applicable and not rule_without_facts else "not_applicable",
                                "expected": fact, "reason": "即时规则只给优先提醒，不承担逐项事实提取；提醒语境另行审阅" if applicable and rule_without_facts else
                                "等待逐项语义审阅" if result and applicable else "无成功输出" if applicable else "该轮不适用"})
        # 忠实度按输出事实或检索线索计数，资料支持按带引用的方向主张计数。
        # 无输出时保留逐轮未评价记录，不猜测本来应生成多少条主张。
        for dimension, items, unit in (
            ("fact_fidelity", list(enumerate(facts)), "output_fact"),
            ("clue_fidelity", list(enumerate(result.get("clues", []))) if result else [], "output_clue"),
            ("source_support", [(index, direction) for index, direction in enumerate(result["analysis"]["directions"])
                                if direction["source_ids"]] if result else [], "cited_direction"),
        ):
            if items:
                for index, item in items:
                    assessments.append({**base, "dimension": dimension, "item": index, "measurement_unit": unit,
                                        "status": "not_evaluated", "output_evidence": item,
                                        "reason": "等待逐项语义审阅；医学含义未经专业审核"})
            else:
                assessments.append({**base, "dimension": dimension, "measurement_unit": "round_scope",
                                    "status": "not_applicable" if result else "not_evaluated",
                                    "reason": "本轮没有该类输出项；规则提醒语境另行审阅" if result else "无成功输出，主张数量未知"})
        for dimension in ("uncertainty", "inference_boundary", "followup_quality", "useful_help", "correction_quality", "urgent_behavior"):
            applicable = True
            if dimension == "correction_quality":
                applicable = record["action"] == "correct"
            elif dimension == "urgent_behavior":
                applicable = oracles[case_id]["safety"]["expected"] != "not_applicable" or bool(result and result["analysis"]["status"] == "urgent_attention")
            if rule_without_facts and dimension != "urgent_behavior":
                applicable = False
            assessments.append({**base, "dimension": dimension, "status": "not_evaluated" if applicable else "not_applicable",
                                "reason": "即时规则未输出事实或分析；提醒是否符合语境在urgent_behavior单独审阅" if rule_without_facts and not applicable else
                                "该轮不适用" if not applicable else "等待语义审阅；医学含义未经专业审核" if result else "无成功输出"})
    # 审阅文件按case/turn/dimension/item覆盖待判状态，保留理由和实际片段。
    reviews = []
    for path in sorted(run_dir.glob("semantic_review_*.jsonl")):
        reviews.extend(read_jsonl(path))
    key = lambda r: (r["case_id"], r["turn"], r["dimension"], r.get("item"))
    review_map = {key(r): r for r in reviews}
    assert len(review_map) == len(reviews), "语义审阅项目重复"
    for index, row in enumerate(assessments):
        if key(row) in review_map:
            assessments[index] = {**row, **review_map.pop(key(row))}
    assert not review_map, "审阅条目不属于预置检查项"
    assert all(row["status"] in ("pass", "fail", "needs_review", "not_evaluated", "not_applicable") for row in assessments)
    with (run_dir / "assessments.jsonl").open("x", encoding="utf-8") as output:
        for row in assessments:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    protocol_end_reasons = {"upstream_failed", "ended_by_rule", "target_missing", "ambiguous_target"}
    completed_by_protocol = []
    started_cases = {r["case_id"] for r in records if "http_status" in r}
    fully_accounted = []
    for case_id, case in cases.items():
        own = [r for r in records if r["case_id"] == case_id]
        planned = 1 + len(case["steps"])
        if {r["turn"] for r in own} == set(range(planned)):
            fully_accounted.append(case_id)
            if all(r["execution_status"] == "terminal" or
                   (r["execution_status"] == "not_reached" and r.get("reason") in protocol_end_reasons) for r in own):
                completed_by_protocol.append(case_id)
    summary = {"planned_cases": len(cases), "planned_turns": sum(1 + len(c["steps"]) for c in cases.values()), "recorded_turns": len(records),
               "started_cases": len({r["case_id"] for r in records if "http_status" in r}),
               "recorded_cases": len({r["case_id"] for r in records}), "not_started_cases": len(cases) - len(started_cases),
               "completed_by_protocol_cases": len(completed_by_protocol), "fully_accounted_cases": len(fully_accounted),
               "unexpected_interrupted_cases": len(started_cases - set(completed_by_protocol)),
               "missing_result_records": len(missing_records), "groups": {}, "assessment_counts": {}}
    for split in ("development", "holdout"):
        subset = [r for r in rounds if r["split"] == split]
        accepted = [r for r in subset if r["accepted_task"]]
        succeeded = sum(r["status"] == "succeeded" for r in accepted)
        summary["groups"][split] = {"planned_turns": len(subset), "recorded_turns": sum(r["execution_status"] != "not_recorded" for r in subset),
                                  "submitted_requests": sum(r["submitted_request"] for r in subset), "accepted_tasks": len(accepted),
                                  "api_rejected_requests": sum(r["submitted_request"] and not r["accepted_task"] for r in subset),
                                  "unfinished_accepted_tasks": sum(r["status"] not in ("succeeded", "failed") for r in accepted),
                                  "business_success_rate": succeeded / len(accepted) if accepted else None,
                                  "execution_statuses": dict(Counter(r["execution_status"] for r in subset)),
                                  "not_reached_reasons": dict(Counter(r["reason"] for r in subset if r["reason"])), "origins": {}, "execution_routes": {}}
        for route in ("model_queue", "safety_rule", "unknown"):
            routed = [r for r in accepted if r["execution_route"] == route]
            success_count = sum(r["status"] == "succeeded" for r in routed)
            summary["groups"][split]["execution_routes"][route] = {
                "accepted_tasks": len(routed), "succeeded": success_count,
                "failed": sum(r["status"] == "failed" for r in routed),
                "unfinished": sum(r["status"] not in ("succeeded", "failed") for r in routed),
                "business_success_rate": success_count / len(routed) if routed else None,
                "basis": "original HTTP202 queued model workflow / HTTP200 immediate rule; not inferred medical quality",
            }
        for origin in ("model", "safety_rule", "no_success_output"):
            selected = [r for r in accepted if (r["origin"] or "no_success_output") == origin]
            wall = [r["wall_seconds"] for r in selected if r["wall_seconds"] is not None]
            execution = [r["successful_attempt_seconds"] for r in selected if r["successful_attempt_seconds"] is not None]
            summary["groups"][split]["origins"][origin] = {
                "submitted_tasks": len(selected), "succeeded": sum(r["status"] == "succeeded" for r in selected),
                "failed": sum(r["status"] == "failed" for r in selected),
                "unfinished": sum(r["status"] not in ("succeeded", "failed") for r in selected),
                "rule_immediate_success": sum(r["status"] == "succeeded" for r in selected) if origin == "safety_rule" else 0,
                "first_attempt_success": sum(r["status"] == "succeeded" and r["attempts"] == 1 for r in selected),
                "retry_success": sum(r["status"] == "succeeded" and r["attempts"] > 1 for r in selected),
                "successful_attempt_seconds_total": sum(execution), "successful_attempt_seconds_median": statistics.median(execution) if execution else None,
                "wall_seconds_total": sum(wall), "wall_seconds_median": statistics.median(wall) if wall else None,
                "wall_seconds_min": min(wall) if wall else None, "wall_seconds_max": max(wall) if wall else None}
    item_units = {"fact_fidelity": "output_fact", "clue_fidelity": "output_clue", "source_support": "cited_direction",
                  "quote_facts": "fact_quote", "quote_clues": "clue_quote", "source_membership": "direction_source_ids"}
    for dimension in sorted({r["dimension"] for r in assessments}):
        dimension_rows = [r for r in assessments if r["dimension"] == dimension]
        measured_rows = [r for r in dimension_rows if r.get("measurement_unit") != "round_scope"]
        counts = Counter(r["status"] for r in measured_rows)
        judged = counts["pass"] + counts["fail"]
        applicable = judged + counts["needs_review"] + counts["not_evaluated"]
        summary["assessment_counts"][dimension] = {**dict(counts), "judged_items": judged, "applicable_items": applicable,
                                                   "pass_rate_judged": counts["pass"] / judged if judged else None,
                                                   "judgment_coverage": judged / applicable if applicable else None}
        if dimension in item_units:
            summary["assessment_counts"][dimension].update(
                measurement_unit=item_units[dimension],
                rounds_without_success_output=sum(r.get("measurement_unit") == "round_scope" and r["status"] == "not_evaluated" for r in dimension_rows),
                not_applicable_rounds=sum(r.get("measurement_unit") == "round_scope" and r["status"] == "not_applicable" for r in dimension_rows),
            )
        if dimension in ("key_fact_coverage", "supporting_fact_coverage"):
            summary["assessment_counts"][dimension]["correct_over_all_applicable_facts"] = counts["pass"] / applicable if applicable else None
    summary["all_attempts"] = dict(Counter(a["status"] for r in rounds for a in r["attempt_runs"]))
    summary["error_stages"] = dict(Counter(a["metrics"].get("error_stage") for r in rounds for a in r["attempt_runs"] if a["status"] == "failed"))
    save_json(run_dir / "summary.json", summary)
    with (run_dir / "case_results.md").open("x", encoding="utf-8") as output:
        output.write("# 逐例逐轮结果\n\n|病例|集合|轮次|执行|业务状态|来源|耗时秒|未执行原因|\n|---|---|---:|---|---|---|---:|---|\n")
        for r in rounds:
            wall = f"{r['wall_seconds']:.2f}" if r["wall_seconds"] is not None else "—"
            output.write(f"|{r['case_id']}|{r['split']}|{r['turn']}|{r['execution_status']}|{r['status'] or '—'}|{r['origin'] or '—'}|{wall}|{r['reason'] or '—'}|\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    summarize(parser.parse_args().run_dir.resolve())
