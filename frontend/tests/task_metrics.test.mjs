// 使用现有 Vue 编译器验证静态展示，不访问后端或运行模型。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";

const source = await readFile(new URL("../src/components/TaskMetrics.vue", import.meta.url), "utf8");
const { descriptor } = parse(source);
const compiled = compileScript(descriptor, {
  id: "task-metrics-test",
  inlineTemplate: true,
  templateOptions: { ssr: true },
});
const javascript = ts.transpileModule(compiled.content, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText.replace(/from ['"](vue(?:\/server-renderer)?)['"]/g, (_, name) => `from "${import.meta.resolve(name)}"`);
const { default: TaskMetrics } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

// SSR没有浏览器计时器；此处只观察给定快照，不模拟token增长。
globalThis.window = { clearInterval() {}, setInterval() { return 0; } };

function jobFixture(status = "running") {
  const stage = (state = "waiting", phase = "waiting") => ({
    status: state, phase, input_tokens: null, output_tokens: null,
    tokens_per_second: null, is_estimate: false, elapsed_seconds: 0,
  });
  return {
    id: "fixture-child", status, attempts: 1, result: null,
    progress: {
      attempt: 1, started_at: "2026-11-14T00:00:00Z", updated_at: "2026-11-14T00:00:05Z",
      execution_seconds: 5, retrieval_seconds: null, queue_wait_seconds: 0.2,
      stages: { search_plan: stage("running", "thinking"), rerank: stage(), analysis: stage() },
    },
  };
}

test("运行快照明确标估算，不展示thinking正文", async () => {
  const job = jobFixture();
  Object.assign(job.progress.stages.search_plan, {
    output_tokens: 128, tokens_per_second: 12.5, is_estimate: true,
    thinking: "禁止展示的内部思考正文",
  });
  const html = await renderToString(createSSRApp(TaskMetrics, { job }));
  assert.match(html, /≈ 128/);
  assert.match(html, /12\.5/);
  assert.match(html, /估算/);
  assert.match(html, /检索规划/);
  assert.match(html, /LLM 重排/);
  assert.match(html, /最终分析/);
  assert.doesNotMatch(html, /禁止展示的内部思考正文/);
});

test("已完成快照显示官方计数与平均速度", async () => {
  const job = jobFixture("succeeded");
  Object.assign(job.progress.stages.search_plan, {
    status: "completed", phase: "done", input_tokens: 88, output_tokens: 128,
    tokens_per_second: 12.5, elapsed_seconds: 10.2,
  });
  const html = await renderToString(createSSRApp(TaskMetrics, { job }));
  assert.match(html, /<td>128<\/td>/);
  assert.doesNotMatch(html, /≈ 128/);
  assert.match(html, /tokens\/s · 平均/);
  assert.match(html, /10\.2 秒/);
});

test("兼容完成结果里的progress，真正缺失时显示未采集", async () => {
  const job = jobFixture("succeeded");
  job.result = { origin: "model", metrics: { progress: job.progress } };
  delete job.progress;
  const compatible = await renderToString(createSSRApp(TaskMetrics, { job }));
  assert.match(compatible, /检索规划/);
  delete job.result.metrics.progress;
  const unavailable = await renderToString(createSSRApp(TaskMetrics, { job }));
  assert.match(unavailable, /未采集分阶段指标/);
  assert.doesNotMatch(unavailable, /<table/);
});

test("规则结果明确未调用模型，不显示伪造速度", async () => {
  const job = jobFixture("succeeded");
  job.result = { origin: "safety_rule" };
  const html = await renderToString(createSSRApp(TaskMetrics, { job }));
  assert.match(html, /规则直接返回，未调用模型/);
  assert.doesNotMatch(html, /<table/);
  assert.doesNotMatch(html, /tokens\/s/);
});

test("父结果恢复后仍可呈现失败子轮的最后估算", async () => {
  const job = jobFixture("failed");
  Object.assign(job.progress.stages.search_plan, {
    status: "failed", phase: "failed", output_tokens: 128,
    tokens_per_second: 12.5, is_estimate: true, elapsed_seconds: 5,
  });
  const html = await renderToString(createSSRApp(TaskMetrics, { job, showingParent: true }));
  assert.match(html, /下方仍显示上一轮分析/);
  assert.match(html, /未完成/);
  assert.match(html, /≈ 128/);
  assert.match(html, /保留最后快照/);
});
