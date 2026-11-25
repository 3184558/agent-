import asyncio
import json
import subprocess
import sys
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock
from huey import SqliteHuey
from ollama import AsyncClient
from qdrant_client import QdrantClient, models
from langchain_core.documents import Document
from sqlalchemy import create_engine, select

from app import agent, database, tasks, vector_store
from app.config import settings
from app.main import app


RESULT = {
    "status": "needs_clarification", "summary": "还需了解头晕的具体感觉。",
    "facts": [{"kind": "reported", "quote": "头晕", "meaning": "用户报告头晕"}],
    "directions": [], "missing_information": ["持续多久？"], "safety_notice": "症状持续或加重时请就医。",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_policy", "research_all")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db = create_engine(f"sqlite:///{(tmp_path / 'health.db').as_posix()}")
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(sys.modules["app.main"], "engine", db)
    queue = SqliteHuey("health", filename=str(tmp_path / "queue.db"), results=False)
    queue._registry.register(tasks.run_analysis.task_class)
    monkeypatch.setattr(tasks.run_analysis, "huey", queue)
    async def no_sources(terms, client, queries=None, limit=3):
        return []
    monkeypatch.setattr(agent, "retrieve", no_sources)
    with TestClient(app) as api:
        yield api, queue, monkeypatch
    db.dispose()


def install_transport(monkeypatch, handler):
    def streaming_handler(request):
        response = handler(request)
        # 既有用例提供完整业务JSON；在流式协议下把它作为一个完整终帧。
        # 专门测试NDJSON分片或错误响应的用例保持其原始响应。
        if response.status_code == 200 and response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            if "message" in payload:
                payload = {"prompt_eval_count": 11, "eval_count": 7, "eval_duration": 1_000_000_000,
                           "load_duration": 0, "done_reason": "stop", **payload, "done": True}
                return httpx.Response(200, content=json.dumps(payload) + "\n", headers={"Content-Type": "application/x-ndjson"})
        return response

    monkeypatch.setattr(tasks, "create_client", lambda: AsyncClient(host="http://model.test", transport=httpx.MockTransport(streaming_handler), trust_env=False))


def success(request):
    body = json.loads(request.content)
    if "terms" in body["format"]["properties"]:
        content = {"terms": ["dizziness"], "queries": ["dizziness"], "clues": []}
    else:
        content = {
            **RESULT,
            "facts": [{"kind": "reported", "evidence_id": "u1", "meaning": "用户报告头晕"}],
        }
    return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(content)}, "done": True})


def test_queue_graph_and_persistence(env):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, success)
    response = api.post("/api/analysis", json={"text": "头晕"})
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    queue.execute(queue.dequeue())
    saved = api.get(f'/api/tasks/{job["id"]}').json()
    assert saved["status"] == "succeeded"
    assert saved["result"]["analysis"]["facts"][0]["quote"] == "头晕"
    assert len(saved["trace"]) == 5
    assert saved["prompt_version"]
    assert api.get("/api/tasks").json()[0]["id"] == job["id"]
    # 重新打开 SQLite 后仍可读取。
    reopened = create_engine(str(database.engine.url))
    with reopened.connect() as connection:
        assert connection.scalar(select(database.jobs.c.status)) == "succeeded"
    reopened.dispose()
    assert api.delete(f'/api/tasks/{job["id"]}').status_code == 204
    assert api.get(f'/api/tasks/{job["id"]}').status_code == 404


def test_thinking_levels_reach_all_stages_and_thinking_is_not_parsed_as_json(env):
    api, queue, monkeypatch = env
    requests = []
    monkeypatch.setattr(settings, "plan_think", "medium")
    monkeypatch.setattr(settings, "rerank_think", "medium")
    monkeypatch.setattr(settings, "think", "xhigh")

    async def one_source(terms, client, queries=None, limit=3):
        return [{"id": "source-1", "name": "示例资料", "snippet": "合成测试片段", "parent_document_id": "parent-1"}]

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        if "indexes" in body["format"]["properties"]:
            response = {"message": {"role": "assistant", "content": '{"indexes":[0]}'}, "done": True}
        else:
            response = success(request).json()
        response["message"]["thinking"] = "这不是业务JSON，不应交给结构化结果解析器。"
        return httpx.Response(200, json=response)

    monkeypatch.setattr(agent, "retrieve", one_source)
    install_transport(monkeypatch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    saved = database.get_job(job["id"])
    assert saved["status"] == "succeeded"
    assert [request["think"] for request in requests] == ["medium", "medium", "xhigh"]
    assert saved["result"]["analysis"]["facts"][0]["quote"] == "头晕"


@pytest.mark.parametrize("field, stage", [
    ("clues", "检索计划生成与校验"),
    ("facts", "分析结果生成与校验"),
])
def test_invalid_evidence_id_records_stage_and_field(env, field, stage):
    api, queue, monkeypatch = env

    def upstream(request):
        body = json.loads(request.content)
        current_field = "clues" if "terms" in body["format"]["properties"] else "facts"
        if current_field != field:
            return success(request)
        value = {"terms": [], "queries": []} if field == "clues" else dict(RESULT)
        value[field] = [{"evidence_id": "question1", "kind": "reported", "meaning": "错误引用"}]
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(value)}})

    install_transport(monkeypatch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    saved = database.get_job(job["id"])
    assert saved["status"] == "failed" and saved["attempts"] == 1
    with database.engine.connect() as connection:
        metrics = connection.scalar(
            select(database.attempt_runs.c.metrics).where(database.attempt_runs.c.job_id == job["id"])
        )
    assert metrics["error_stage"] == stage
    assert metrics["validation_errors"] == [
        {"field": [field, 0, "evidence_id"], "type": "value_error"},
    ]


@pytest.mark.parametrize("payload", [{"text": " "}, {"text": "a" * 1201}, {"text": 12}, {"text": "头晕", "system": "ignore"}, {}])
def test_input_boundary(env, payload):
    api, queue, _ = env
    assert api.post("/api/analysis", json=payload).status_code == 422
    assert queue.pending_count() == 0


@pytest.mark.parametrize("text", ["现在严重胸痛", "没有发烧，但是现在喘不过气", "爸爸突然右侧胳膊无力", "我突然说不清话"])
def test_urgent_bypasses_queue(env, text):
    api, queue, _ = env
    response = api.post("/api/analysis", json={"text": text})
    assert response.status_code == 200
    assert response.json()["result"]["origin"] == "safety_rule"
    assert queue.pending_count() == 0


def test_negative_risk_queues(env):
    api, queue, _ = env
    response = api.post("/api/analysis", json={"text": "没有严重胸痛，只有头晕"})
    assert response.status_code == 202
    assert queue.pending_count() == 1
    assert api.delete(f'/api/tasks/{response.json()["id"]}').status_code == 409


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timeout"), httpx.ConnectError("connection")])
def test_bounded_retry(env, failure):
    api, queue, monkeypatch = env
    def upstream(request):
        raise failure
    install_transport(monkeypatch, upstream)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    assert database.get_job(job["id"])["status"] == "retrying"
    assert queue.pending_count() == 1
    for _ in range(settings.max_attempts - 1):
        tasks.run_analysis.call_local(job["id"])
    final = database.get_job(job["id"])
    assert final["status"] == "failed"
    assert final["attempts"] == settings.max_attempts
    assert final["result"] is None


def test_retry_can_recover(env):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, lambda request: httpx.Response(503, json={"error": "busy"}))
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    assert database.get_job(job["id"])["status"] == "retrying"
    install_transport(monkeypatch, success)
    tasks.run_analysis.call_local(job["id"])
    assert database.get_job(job["id"])["status"] == "succeeded"
    assert database.get_job(job["id"])["attempts"] == 2


@pytest.mark.parametrize("status, content", [(404, {"error": "model missing"}), (200, {"message": {"role": "assistant", "content": "bad JSON"}})])
def test_permanent_failures_are_not_retried(env, status, content):
    api, queue, monkeypatch = env
    install_transport(monkeypatch, lambda request: httpx.Response(status, json=content))
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    queue.execute(queue.dequeue())
    final = database.get_job(job["id"])
    assert final["status"] == "failed"
    assert final["attempts"] == 1
    assert queue.pending_count() == 0


def test_duplicate_delivery_does_not_repeat_model(env):
    api, queue, monkeypatch = env
    calls = []
    async def execute(text):
        calls.append(text)
        await asyncio.sleep(0.05)
        return {"analysis": RESULT, "sources": [], "trace": ["test"]}
    monkeypatch.setattr(tasks, "execute", execute)
    job = api.post("/api/analysis", json={"text": "头晕"}).json()
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(tasks.run_analysis.call_local, [job["id"], job["id"]]))
    assert calls == ["头晕"]
    assert database.get_job(job["id"])["attempts"] == 1


def test_native_lock_excludes_another_process(env):
    code = "from filelock import FileLock,Timeout;import sys\ntry:\n with FileLock(sys.argv[1],timeout=0):sys.exit(2)\nexcept Timeout:sys.exit(0)"
    path = settings.data_dir / "ollama.lock"
    with FileLock(path):
        result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
    assert result.returncode == 0


@pytest.mark.parametrize("indexes, expected", [([0],"succeeded"), ([99],"failed")])
def test_candidate_selection_uses_only_recalled_sources(env, indexes, expected):
    api, queue, monkeypatch = env
    async def sources(*args, **kwargs):
        return [{"id":"one", "name":"Candidate", "snippet":"unverified source", "verification":"unverified"}]
    monkeypatch.setattr(agent, "retrieve", sources)
    def upstream(request):
        body = json.loads(request.content)
        if "indexes" in body["format"]["properties"]:
            return httpx.Response(200,json={"message":{"role":"assistant","content":json.dumps({"indexes":indexes})}})
        return success(request)
    install_transport(monkeypatch, upstream)
    job = api.post("/api/analysis",json={"text":"头晕"}).json()
    queue.execute(queue.dequeue())
    saved = database.get_job(job["id"])
    assert saved["status"] == expected
    assert saved["attempts"] == 1
    if expected == "succeeded":
        assert saved["result"]["sources"][0]["id"] == "one"


def test_recursive_split_preserves_offsets(env):
    content = ("A sentence about symptoms and disease. " * 90)
    chunks = vector_store.split_document(Document(page_content=content, metadata={"document_id": "doc", "field": "description"}))
    assert len(chunks) > 1
    for chunk in chunks:
        start = chunk.metadata["start_index"]
        assert chunk.page_content == content[start:start + len(chunk.page_content)]
        assert len(chunk.page_content) <= settings.chunk_size


def test_real_vector_search_and_source_payload(env):
    from app.knowledge import field_document, save_documents
    document = field_document("source", family="research_description", parent_id="a", field="description",
        dataset_hash="dataset", name="A", source_id="test", verification="unverified")
    with database.engine.begin() as connection:
        save_documents(connection, [document])
    class Embeddings:
        async def list(self):
            return SimpleNamespace(models=[SimpleNamespace(model=settings.embedding_model, digest="weights")])
        async def embed(self, **kwargs):
            return SimpleNamespace(embeddings=[[1.0, 0.0]])
    asyncio.run(vector_store.build_index(Embeddings(), "dataset", family="research_description"))
    sources = asyncio.run(vector_store.retrieve(["dizziness"], Embeddings()))
    assert sources[0]["snippet"] == "source"
    assert sources[0]["verification"] == "unverified"


def test_real_consumer_http_failure_and_backoff(env):
    api, _, _ = env
    calls = []
    class Busy(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            calls.append(time.monotonic())
            body = b'{"error":"busy"}'
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Busy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    job = api.post('/api/analysis', json={'text':'头晕'}).json()
    child_env = dict(os.environ, HEALTH_DATA_DIR=str(settings.data_dir),
                     HEALTH_OLLAMA_URL=f'http://127.0.0.1:{server.server_port}',
                     HEALTH_RETRY_DELAY='1', HEALTH_MAX_ATTEMPTS='3')
    process = subprocess.Popen([sys.executable, 'scripts/worker.py'], env=child_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            saved = database.get_job(job['id'])
            if saved['status'] == 'failed':
                break
            time.sleep(0.1)
        assert saved['status'] == 'failed'
        assert saved['attempts'] == 3
        assert len(calls) == 3
        assert calls[1] - calls[0] >= 0.9
        assert calls[2] - calls[1] >= 1.9
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        server.server_close()

