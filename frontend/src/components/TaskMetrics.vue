<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import type { Job, StageProgress } from "../api";

const props = defineProps<{ job: Job; showingParent?: boolean }>();
const progress = computed(() => props.job.progress ?? props.job.result?.metrics?.progress ?? null);
const running = computed(() => props.job.status === "running");
const deltaSeconds = ref(0);
let timer: number | undefined;

// 只平滑耗时，不外推 token 数或生成速度；终态保留服务端快照值。
watch(
  [() => props.job.id, () => props.job.status, () => progress.value?.attempt, () => progress.value?.updated_at],
  () => {
    window.clearInterval(timer);
    deltaSeconds.value = 0;
    if (running.value && progress.value) {
      const receivedAt = performance.now();
      timer = window.setInterval(() => {
        deltaSeconds.value = (performance.now() - receivedAt) / 1000;
      }, 250);
    }
  },
  { immediate: true },
);
onUnmounted(() => window.clearInterval(timer));

const stages = [
  { key: "search_plan", label: "检索规划" },
  { key: "rerank", label: "LLM 重排" },
  { key: "analysis", label: "最终分析" },
] as const;
const phases: Record<StageProgress["phase"], string> = {
  waiting: "等待开始",
  preparing: "准备请求",
  thinking: "正在思考",
  answering: "正在生成结果",
  validating: "核对输出",
  done: "已完成",
  failed: "未完成",
  not_called: "未调用 LLM",
};
const states: Record<Job["status"], string> = {
  queued: "等待处理",
  running: "正在执行",
  retrying: "等待重试",
  succeeded: "已完成",
  failed: "未完成",
};

function duration(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(1)} 秒`;
}

function tokens(value: number | null, estimated = false): string {
  return value === null ? "—" : `${estimated ? "≈ " : ""}${value.toLocaleString()}`;
}

function elapsed(stage: StageProgress): number {
  return stage.elapsed_seconds + (running.value && ["running", "validating"].includes(stage.status) ? deltaSeconds.value : 0);
}
</script>

<template>
  <section class="task-metrics" aria-label="本次任务处理指标" aria-live="off">
    <div class="metrics-heading">
      <h4>处理指标</h4>
      <span class="metrics-job-state" :class="job.status">{{ states[job.status] }}</span>
    </div>
    <p v-if="showingParent" class="metrics-context">
      这是最近补充任务的指标；下方仍显示上一轮分析。
    </p>
    <p v-if="job.result?.origin === 'safety_rule'" class="metrics-note">
      本轮由症状提示规则直接返回，未调用模型。
    </p>
    <template v-else-if="progress">
      <div class="metrics-summary">
        <span>第 {{ progress.attempt }} 次执行</span>
        <span>本次执行 {{ duration(progress.execution_seconds + (running ? deltaSeconds : 0)) }}</span>
        <span>排队等待 {{ duration(progress.queue_wait_seconds) }}</span>
      </div>
      <div class="metrics-table-wrap" tabindex="0" aria-label="三阶段指标表，可横向滚动">
        <table class="metrics-table">
          <thead>
            <tr>
              <th scope="col">阶段</th>
              <th scope="col">输入 tokens</th>
              <th scope="col">生成 tokens</th>
              <th scope="col">生成速度</th>
              <th scope="col">耗时</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="stage in stages" :key="stage.key" :class="progress.stages[stage.key].status">
              <th scope="row">
                {{ stage.label }}
                <small>{{ phases[progress.stages[stage.key].phase] }}</small>
              </th>
              <td>{{ tokens(progress.stages[stage.key].input_tokens) }}</td>
              <td>{{ tokens(progress.stages[stage.key].output_tokens, progress.stages[stage.key].is_estimate) }}</td>
              <td>
                <template v-if="progress.stages[stage.key].tokens_per_second !== null">
                  {{ progress.stages[stage.key].is_estimate ? "≈ " : "" }}{{ progress.stages[stage.key].tokens_per_second?.toFixed(1) }}
                  <small>tokens/s{{ progress.stages[stage.key].is_estimate ? " · 估算" : " · 平均" }}</small>
                </template>
                <template v-else>—</template>
              </td>
              <td>{{ duration(elapsed(progress.stages[stage.key])) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="metrics-note metrics-scroll-hint">左右滑动查看完整指标。</p>
      <p class="metrics-retrieval">资料检索耗时 <strong>{{ duration(progress.retrieval_seconds) }}</strong></p>
      <p class="metrics-note">
        生成 tokens 包含思考与结果。≈ 表示流式估算；完成后显示官方计数和平均生成速度。
        <template v-if="job.status === 'failed'">未完成的阶段保留最后快照。</template>
      </p>
    </template>
    <p v-else class="metrics-note">
      {{ ['queued', 'running', 'retrying'].includes(job.status) ? "等待本轮阶段指标；收到快照后自动更新。" : "这条记录未采集分阶段指标。" }}
    </p>
  </section>
</template>
