import json

import httpx
import pytest
from sqlalchemy import select

from app import database
from test_api import env, install_transport, success, RESULT


def finish(api, queue):
    parent = api.post('/api/analysis',json={'text':'头晕'}).json()
    queue.execute(queue.dequeue())
    return api.get('/api/tasks/'+parent['id']).json()


def test_answer_continues_same_conversation_without_client_history(env):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, success)
    parent = finish(api, queue)
    response = api.post(f'/api/tasks/{parent["id"]}/follow-up',json={'action':'answer','index':0,'text':'两个月，起床后明显。'})
    assert response.status_code == 202
    child = response.json()
    assert '头晕' in child['text'] and '两个月' in child['text']
    queue.execute(queue.dequeue())
    assert database.get_job(child['id'])['status'] == 'succeeded'
    chain = api.get(f'/api/tasks/{child["id"]}/conversation').json()
    assert len(chain) == 2 and chain[1]['contribution']['question'] == '持续多久？'
    assert api.delete('/api/tasks/'+parent['id']).status_code == 409
    assert api.delete('/api/tasks/'+child['id']).status_code == 204


def test_skip_keeps_unknown_and_correction_is_latest_user_input(env):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, success)
    parent = finish(api, queue)
    child = api.post(f'/api/tasks/{parent["id"]}/follow-up',json={'action':'skip','index':0}).json()
    queue.execute(queue.dequeue())
    turn = database.get_turn(child['id'])
    assert turn['contribution']['action'] == 'skip'
    assert '仍未知' in turn['contribution']['text']
    corrected = api.post(f'/api/tasks/{child["id"]}/follow-up',json={'action':'correct','index':0,'text':'我没有头晕，是鼻塞。'}).json()
    assert '纠正' in corrected['text']
    assert '我没有头晕，是鼻塞。' in database.get_turn(corrected['id'])['evidence_text']


def test_correction_identifies_selected_fact_when_quotes_are_shared(env):
    api, queue, _ = env
    original = "咳嗽两天，气短三周。"
    facts = [
        {"kind": "reported", "quote": original, "meaning": "用户报告咳嗽持续两天"},
        {"kind": "reported", "quote": original, "meaning": "用户报告气短持续三周"},
    ]
    parent = api.post('/api/analysis', json={"text": original}).json()
    database.update_job(
        parent["id"], status="succeeded",
        result={
            "analysis": {**RESULT, "facts": facts}, "origin": "model",
            "elapsed_seconds": 0, "model": "test", "sources": [],
        },
    )
    correction = "这一项其实已经一个月了。"
    response = api.post(
        f'/api/tasks/{parent["id"]}/follow-up',
        json={"action": "correct", "index": 1, "text": correction},
    )
    assert response.status_code == 202
    child = response.json()
    turn = database.get_turn(child["id"])
    assert turn["parent_id"] == parent["id"]
    assert turn["contribution"]["index"] == 1
    assert turn["contribution"]["target_fact"] == facts[1]
    assert turn["evidence_text"] == original + "\n" + correction
    model_context = json.loads(child["text"].rsplit("\n", 1)[1])
    assert model_context["target_fact"] == facts[1]
    assert database.get_job(parent["id"])["result"]["analysis"]["facts"] == facts
    chain = api.get(f'/api/tasks/{child["id"]}/conversation').json()
    assert chain[-1]["contribution"]["target_fact"] == facts[1]


def test_new_urgent_answer_does_not_wait_for_worker(env):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, success)
    parent = finish(api, queue)
    response = api.post(f'/api/tasks/{parent["id"]}/follow-up',json={'action':'answer','index':0,'text':'现在严重胸痛，喘不过气。'})
    assert response.status_code == 200
    assert response.json()['result']['origin'] == 'safety_rule'
    assert queue.pending_count() == 0


@pytest.mark.parametrize('payload',[{'action':'answer','index':0,'text':' '},{'action':'correct','index':0,'text':'a'*501},{'action':'answer','index':99,'text':'不清楚'}])
def test_followup_entry_checks(env,payload):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, success)
    parent = finish(api, queue)
    assert api.post(f'/api/tasks/{parent["id"]}/follow-up',json=payload).status_code == 422


def test_generated_question_cannot_become_quoted_user_fact(env):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, success)
    parent = finish(api, queue)
    child = api.post(f'/api/tasks/{parent["id"]}/follow-up',json={'action':'answer','index':0,'text':'两个月'}).json()
    bad = {**RESULT,'facts':[{'kind':'reported','quote':'持续多久？','meaning':'将问题当作事实'}]}
    def upstream(request):
        body=json.loads(request.content)
        if 'terms' in body['format']['properties']:
            return success(request)
        return httpx.Response(200,json={'message':{'role':'assistant','content':json.dumps(bad)}})
    install_transport(monkeypatch, upstream)
    queue.execute(queue.dequeue())
    saved=database.get_job(child['id'])
    assert saved['status']=='failed' and saved['attempts']==1


def test_context_budget_is_explicit_without_silent_truncation(env):
    api,queue,monkeypatch=env
    install_transport(monkeypatch,success)
    parent=finish(api,queue)
    database.update_job(parent['id'],text='已有描述'*650)
    response=api.post(f'/api/tasks/{parent["id"]}/follow-up',json={'action':'answer','index':0,'text':'补充'*240})
    assert response.status_code==422
    assert '预算' in response.json()['message']
