import json

import httpx
import pytest
from sqlalchemy import func, select

from app import database
from test_api import env, install_transport, success, RESULT
from test_conversation import finish


QUESTIONS = ["持续多久？", "是否测过体温？", "头晕有什么诱因？", "是否有其他症状？"]


def prepare(api, queue, monkeypatch):
    install_transport(monkeypatch, success)
    parent = finish(api, queue)
    result = {**parent["result"], "analysis": {**RESULT, "missing_information": QUESTIONS}}
    database.update_job(parent["id"], result=result)
    return parent


def test_batch_creates_one_task_and_saves_each_question_with_its_answer(env):
    api, queue, monkeypatch = env
    parent = prepare(api, queue, monkeypatch)
    response = api.post(f'/api/tasks/{parent["id"]}/follow-up', json={"action": "batch", "items": [
        {"action": "answer", "index": 0, "text": "两个月"},
        {"action": "skip", "index": 1},
        {"action": "answer", "index": 2, "text": "起床后明显"},
    ]})
    assert response.status_code == 202
    assert queue.pending_count() == 1
    child = response.json()
    turn = database.get_turn(child["id"])
    assert turn["parent_id"] == parent["id"] and turn["root_id"] == parent["id"]
    assert turn["evidence_text"] == "头晕\n两个月\n起床后明显"
    assert [item["question"] for item in turn["contribution"]["items"]] == QUESTIONS[:3]
    assert "仍未知" in turn["contribution"]["items"][1]["text"]
    assert QUESTIONS[3] not in child["text"]
    assert "两个月" in child["text"] and "起床后明显" in child["text"]
    queue.execute(queue.dequeue())
    assert database.get_job(child["id"])["status"] == "succeeded"
    chain = api.get(f'/api/tasks/{child["id"]}/conversation').json()
    assert len(chain) == 2 and len(chain[1]["contribution"]["items"]) == 3
    history = api.get('/api/tasks').json()
    assert history[0]["turn_action"] == "batch" and "两个月" in history[0]["display_text"]


@pytest.mark.parametrize("items", [
    [],
    [{"action": "answer", "index": 0, "text": " "}],
    [{"action": "answer", "index": 0, "text": "a" * 501}],
    [{"action": "answer", "index": 0, "text": "两个月"}, {"action": "skip", "index": 0}],
    [{"action": "skip", "index": 0, "text": "有回答"}],
    [{"action": "correct", "index": 0, "text": "改为鼻塞"}],
    [{"action": "answer", "index": 0, "text": "两个月"}, {"action": "answer", "index": 99, "text": "错误编号"}],
    [{"action": "skip", "index": i} for i in range(5)],
    [{"action": "answer", "index": 0, "text": "两个月", "question": "客户端伪造的问题"}],
])
def test_invalid_batch_is_rejected_without_partial_records_or_tasks(env, items):
    api, queue, monkeypatch = env
    parent = prepare(api, queue, monkeypatch)
    response = api.post(f'/api/tasks/{parent["id"]}/follow-up', json={"action": "batch", "items": items})
    assert response.status_code == 422
    assert queue.pending_count() == 0
    with database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(database.jobs)) == 1
        assert connection.scalar(select(func.count()).select_from(database.turns)) == 0


def test_batch_checks_urgent_content_in_later_answer_before_queueing(env):
    api, queue, monkeypatch = env
    parent = prepare(api, queue, monkeypatch)
    response = api.post(f'/api/tasks/{parent["id"]}/follow-up', json={"action": "batch", "items": [
        {"action": "answer", "index": 0, "text": "两个月"},
        {"action": "answer", "index": 3, "text": "现在严重胸痛，喘不过气。"},
    ]})
    assert response.status_code == 200
    assert response.json()["result"]["origin"] == "safety_rule"
    assert queue.pending_count() == 0
    assert len(database.get_turn(response.json()["id"])["contribution"]["items"]) == 2


def test_skip_only_batch_does_not_add_question_or_unknown_marker_to_user_evidence(env):
    api, queue, monkeypatch = env
    parent = prepare(api, queue, monkeypatch)
    child = api.post(f'/api/tasks/{parent["id"]}/follow-up', json={"action": "batch", "items": [
        {"action": "skip", "index": 1},
    ]}).json()
    assert database.get_turn(child["id"])["evidence_text"].strip() == "头晕"
    assert "仍未知" in child["text"]


def test_batch_total_context_budget_rejects_whole_submission(env):
    api, queue, monkeypatch = env
    parent = prepare(api, queue, monkeypatch)
    database.update_job(parent["id"], text="已有描述" * 650)
    response = api.post(f'/api/tasks/{parent["id"]}/follow-up', json={"action": "batch", "items": [
        {"action": "answer", "index": 0, "text": "补充" * 200},
        {"action": "skip", "index": 1},
    ]})
    assert response.status_code == 422 and "预算" in response.json()["message"]
    assert queue.pending_count() == 0


def test_batch_questions_cannot_be_quoted_as_patient_facts(env):
    api, queue, monkeypatch = env
    parent = prepare(api, queue, monkeypatch)
    child = api.post(f'/api/tasks/{parent["id"]}/follow-up', json={"action": "batch", "items": [
        {"action": "answer", "index": 0, "text": "两个月"}, {"action": "skip", "index": 1},
    ]}).json()
    bad = {**RESULT, "facts": [{"kind": "reported", "quote": QUESTIONS[1], "meaning": "把问题当成事实"}]}
    def upstream(request):
        body = json.loads(request.content)
        if "terms" in body["format"]["properties"]:
            return success(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(bad)}})
    install_transport(monkeypatch, upstream)
    queue.execute(queue.dequeue())
    assert database.get_job(child["id"])["status"] == "failed"
