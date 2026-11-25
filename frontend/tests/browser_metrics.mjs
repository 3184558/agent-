// 独立 Edge/CDP 验收：仅访问本脚本的静态页面与合成 mock API，不调用模型。
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:http";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = fileURLToPath(new URL("..", import.meta.url));
const output = resolve(frontend, "../evaluation/runs/live_metrics/20261114_feature_01/browser");
await mkdir(output, { recursive: true });
const profile = join(output, `edge-profile-${Date.now()}`);
const fixtureText = "合成浏览器验收：右手间歇发麻。";
const observed = [];
let finishParent = false;
let finishChild = false;
let runningCount = 128;
let created = false;
let childCreated = false;

function stage(status = "waiting", phase = "waiting", values = {}) {
  return { status, phase, input_tokens: null, output_tokens: null, tokens_per_second: null,
    is_estimate: false, elapsed_seconds: 0, ...values };
}

function progress(child = false, done = false) {
  return {
    attempt: 1, started_at: "2026-11-14T00:00:00Z", updated_at: new Date().toISOString(),
    execution_seconds: done ? 34.5 : 5, retrieval_seconds: done || child ? 0.6 : null,
    queue_wait_seconds: 0.2,
    stages: child ? {
      search_plan: stage("completed", "done", { input_tokens: 91, output_tokens: 420, tokens_per_second: 50, elapsed_seconds: 9 }),
      rerank: stage("completed", "done", { input_tokens: 33, output_tokens: 45, tokens_per_second: 20, elapsed_seconds: 2 }),
      analysis: stage(done ? "failed" : "running", done ? "failed" : "thinking", {
        output_tokens: 17, tokens_per_second: 8.5, is_estimate: true, elapsed_seconds: 3 }),
    } : done ? {
      search_plan: stage("completed", "done", { input_tokens: 88, output_tokens: 512, tokens_per_second: 50, elapsed_seconds: 10.2 }),
      rerank: stage("completed", "done", { input_tokens: 30, output_tokens: 40, tokens_per_second: 20, elapsed_seconds: 2.2 }),
      analysis: stage("completed", "done", { input_tokens: 120, output_tokens: 800, tokens_per_second: 40, elapsed_seconds: 21.5 }),
    } : {
      search_plan: stage("running", "thinking", { output_tokens: runningCount,
        tokens_per_second: runningCount === 128 ? 12.5 : 24, is_estimate: true, elapsed_seconds: 4 }),
      rerank: stage(), analysis: stage(),
    },
  };
}

function job(child = false, done = false) {
  const snapshot = progress(child, done);
  return {
    id: child ? "fixture-child" : "fixture-parent", text: fixtureText,
    status: done ? child ? "failed" : "succeeded" : "running", attempts: 1,
    created_at: "2026-11-14T00:00:00Z", prompt_version: "browser-fixture",
    error: child && done ? "合成失败：模型校验未完成。" : null,
    trace: ["浏览器合成验收"], progress: snapshot,
    result: !child && done ? {
      origin: "model", model: "browser-fixture", elapsed_seconds: 34.5, sources: [],
      metrics: { progress: snapshot }, analysis: {
        status: "needs_clarification", summary: "合成父轮结果：还需要了解持续时间。",
        facts: [{ kind: "reported", quote: fixtureText, meaning: "合成症状记录" }],
        directions: [], missing_information: ["手麻持续多久？"], safety_notice: "这是虚构界面验收数据。",
      },
    } : null,
  };
}

const staticFiles = new Map([["/", await readFile(join(frontend, "dist/index.html"))]]);
for (const name of await readdir(join(frontend, "dist/assets"))) {
  staticFiles.set(`/assets/${name}`, await readFile(join(frontend, "dist/assets", name)));
}
const server = createServer(async (request, response) => {
  const path = new URL(request.url, "http://fixture.invalid").pathname;
  observed.push({ method: request.method, path });
  const json = (value, status = 200) => {
    response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
    response.end(JSON.stringify(value));
  };
  if (path === "/api/knowledge/review") {
    json({ policy: "reviewed_only", total_records: 0, active_family: "browser-fixture",
      active_documents: 0, source_checked: 0, clinical_review: "pending" });
  } else if (path === "/api/tasks") {
    const history = childCreated ? [job(true, finishChild), job(false, finishParent)] : created ? [job(false, finishParent)] : [];
    json(history.map(({ progress: ignored, ...item }) => item));
  } else if (path === "/api/analysis" && request.method === "POST") {
    const parts = [];
    for await (const part of request) parts.push(part);
    assert.equal(JSON.parse(Buffer.concat(parts)).text, fixtureText);
    created = true;
    json({ ...job(), status: "queued", progress: null }, 202);
  } else if (path.endsWith("/conversation")) {
    json([{ id: "fixture-parent", status: "succeeded", contribution: { action: "initial", text: fixtureText } },
      ...(path.includes("fixture-child") ? [{ id: "fixture-child", status: "running",
        contribution: { action: "answer", question: "手麻持续多久？", text: "持续一周。" } }] : [])]);
  } else if (path === "/api/tasks/fixture-parent/follow-up" && request.method === "POST") {
    const parts = [];
    for await (const part of request) parts.push(part);
    assert.equal(JSON.parse(Buffer.concat(parts)).items[0].text, "持续一周。");
    childCreated = true;
    json({ ...job(true), status: "queued", progress: null }, 202);
  } else if (path === "/api/tasks/fixture-parent") {
    json(job(false, finishParent));
  } else if (path === "/api/tasks/fixture-child") {
    json(job(true, finishChild));
  } else if (path === "/favicon.ico") {
    response.writeHead(204).end();
  } else if (staticFiles.has(path)) {
    response.writeHead(200, { "Content-Type": { ".js": "text/javascript", ".css": "text/css" }[extname(path)] ?? "text/html; charset=utf-8" });
    response.end(staticFiles.get(path));
  } else {
    json({ message: `未定义的合成验收路由：${path}` }, 404);
  }
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = spawn(process.env.EDGE_BINARY ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
], { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
let socket;
let closeBrowser;
let browserLog = "";
try {
  const endpoint = await new Promise((resolveEndpoint, reject) => {
    browser.stderr.on("data", data => {
      browserLog += data;
      const match = browserLog.match(/DevTools listening on (ws:\/\/[^\r\n]+)/);
      if (match) resolveEndpoint(match[1]);
    });
    browser.once("error", reject);
    browser.once("exit", code => reject(new Error(`Edge exited before CDP: ${code}`)));
  });
  socket = new WebSocket(endpoint);
  await new Promise(resolveOpen => socket.addEventListener("open", resolveOpen, { once: true }));
  let nextId = 0;
  let sessionId;
  const pending = new Map();
  const exceptions = [];
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    if (message.id) {
      const callback = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) callback.reject(new Error(JSON.stringify(message.error)));
      else callback.resolve(message.result);
    }
  });
  function cdp(method, params = {}, session = sessionId) {
    return new Promise((resolveResult, reject) => {
      const id = ++nextId;
      pending.set(id, { resolve: resolveResult, reject });
      socket.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
    });
  }
  closeBrowser = () => cdp("Browser.close", {}, null);
  async function evaluate(expression) {
    const result = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    assert.equal(result.exceptionDetails, undefined, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async function waitFor(expression) {
    const started = Date.now();
    while (!(await evaluate(expression))) {
      assert.ok(Date.now() - started < 12000, `页面等待超时：${expression}`);
      await new Promise(resolveWait => setTimeout(resolveWait, 100));
    }
  }
  async function screenshot(name, width, height) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 });
    await evaluate("document.querySelector('.task-metrics').scrollIntoView({block:'center'})");
    await new Promise(resolvePaint => setTimeout(resolvePaint, 200));
    const captured = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(join(output, name), Buffer.from(captured.data, "base64"));
  }
  const target = await cdp("Target.createTarget", { url: "about:blank" });
  sessionId = (await cdp("Target.attachToTarget", { targetId: target.targetId, flatten: true })).sessionId;
  await cdp("Runtime.enable");
  await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url: origin });
  await waitFor("Boolean(document.querySelector('#description'))");
  await evaluate(`document.querySelector('#description').value = ${JSON.stringify(fixtureText)}; document.querySelector('#description').dispatchEvent(new Event('input', {bubbles:true}));`);
  await evaluate("document.querySelector('.input-panel form').requestSubmit()");
  await waitFor("document.querySelector('.task-metrics')?.innerText.includes('≈ 128')");
  const initial = await evaluate("document.querySelector('.task-metrics').innerText");
  assert.match(initial, /正在思考/);
  assert.match(initial, /估算/);
  await screenshot("desktop-thinking.png", 1440, 1000);
  runningCount = 256;
  await waitFor("document.querySelector('.task-metrics').innerText.includes('≈ 256')");
  await screenshot("mobile-thinking.png", 390, 844);
  assert.equal(await evaluate("innerWidth"), 390, "窄屏不应因表格撑宽而自动缩放");
  assert.ok(await evaluate("document.documentElement.scrollWidth <= innerWidth"), "页面不应整体横向溢出");
  await evaluate("document.querySelector('.metrics-table-wrap').scrollLeft=1000");
  assert.ok(await evaluate("document.querySelector('.metrics-table-wrap').scrollLeft>0"), "窄屏完整指标可横向查看");
  await screenshot("mobile-thinking-details.png", 390, 844);
  finishParent = true;
  await waitFor("Boolean(document.querySelector('#answer-0'))");
  const completed = await evaluate("document.querySelector('.task-metrics').innerText");
  assert.match(completed, /800/);
  assert.match(completed, /平均/);
  assert.doesNotMatch(completed, /≈\s*800/);
  assert.equal(await evaluate("document.querySelectorAll('.metrics-table tbody tr.completed').length"), 3);
  await screenshot("desktop-completed.png", 1440, 1000);
  await evaluate("document.querySelector('#answer-0').value='持续一周。'; document.querySelector('#answer-0').dispatchEvent(new Event('input', {bubbles:true}));");
  await evaluate("document.querySelector('.follow-up-cards').requestSubmit()");
  await waitFor("document.querySelector('.task-metrics')?.innerText.includes('≈ 17')");
  finishChild = true;
  await waitFor("document.querySelector('.metrics-context')?.innerText.includes('上一轮分析')");
  const failed = await evaluate("document.querySelector('.task-metrics').innerText");
  assert.match(failed, /≈ 17/);
  assert.match(failed, /未完成/);
  assert.match(await evaluate("document.querySelector('.result-title').innerText"), /合成父轮结果/);
  assert.equal(await evaluate("document.querySelector('#answer-0').value"), "持续一周。");
  await screenshot("desktop-followup-failed.png", 1440, 1000);
  await screenshot("mobile-followup-failed.png", 390, 844);
  assert.deepEqual(exceptions, []);
  const report = { passed: true, origin, scope: "real headless Edge App rendering, isolated static build and synthetic mock API; no model/production API",
    initial, completed, failed, requests: observed, console_exceptions: exceptions,
    screenshots: ["desktop-thinking.png", "mobile-thinking.png", "mobile-thinking-details.png", "desktop-completed.png", "desktop-followup-failed.png", "mobile-followup-failed.png"] };
  await writeFile(join(output, "browser_report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: true, requests: observed.length, output }, null, 2));
} finally {
  if (closeBrowser && socket.readyState === WebSocket.OPEN) await closeBrowser();
  socket?.close();
  if (browser.exitCode === null) browser.kill();
  await writeFile(join(output, "edge.log"), browserLog);
  await new Promise(resolveClose => server.close(resolveClose));
}
