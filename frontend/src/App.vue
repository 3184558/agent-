<script setup lang="ts">
// 页面控制层：连接首次分析、批量补充、历史查看及任务状态轮询。
import { computed, onMounted, ref } from "vue";
import {
  request,
  submitAnalysis,
  type AnalysisResponse,
  type Job,
  type FollowUp,
  type FollowUpBatch,
  type ConversationTurn,
  type KnowledgeReview,
} from "./api";
import FollowUpCards from "./components/FollowUpCards.vue";
import FactReview from "./components/FactReview.vue";
import TaskMetrics from "./components/TaskMetrics.vue";

// 输入框与已提交原文分别保存，查看历史时不把内部上下文写回输入框。
const text = ref("");
const submitted = ref("");
const loading = ref(false);
const error = ref("");
const result = ref<AnalysisResponse | null>(null);
const job = ref<Job | null>(null);
// 指标属于最近提交或查看的任务；补充失败回到父结果时仍保留失败轮指标。
const metricsJob = ref<Job | null>(null);
const history = ref<Job[]>([]);
const conversation = ref<ConversationTurn[]>([]);
// 草稿放在父组件，加载期间追问卡片卸载也不会丢失；刷新页面则不保留。
const questionDrafts = ref<Record<number, string>>({});
const skippedQuestions = ref<number[]>([]);
const knowledge = ref<KnowledgeReview | null>(null);
const jobStates = {
  queued: "已排队，等待处理",
  running: "正在分析",
  retrying: "模型暂时不可用，等待重试",
  succeeded: "已完成",
  failed: "未完成",
};
const data = computed(() => result.value?.analysis);
const examples = [
  "最近两天头晕，我猜是低血糖，但没有测过。",
  "咳嗽三天，没有发烧，也没有胸痛。",
  "头有点疼，说不太清楚。",
];
const kinds = {
  reported: "明确陈述",
  denied: "明确否定",
  hypothesis: "个人猜测",
  uncertain: "尚不确定",
};
const statuses = {
  needs_clarification: "还需要一些信息",
  analysis_available: "可以探索的方向",
  urgent_attention: "请优先寻求帮助",
  unable_to_analyze: "暂时无法分析",
};

/**
 * 统一处理首次分析、打开已有任务和接续补充，等待任务结束后展示结果。
 * existing 表示读取历史任务；followUp 表示基于当前父轮提交补充或纠正。
 * 补充失败时恢复父轮的页面状态，保留草稿，不删除已保存的失败任务。
 */
async function submit(existing?: Job, followUp?: FollowUp | FollowUpBatch) {
  const previousJob = job.value;
  const previousResult = result.value;
  const previousConversation = conversation.value;
  if (!followUp) {
    questionDrafts.value = {};
    skippedQuestions.value = [];
    conversation.value = [];
    result.value = null;
    submitted.value = text.value;
  }
  loading.value = true;
  error.value = "";
  metricsJob.value = null;
  try {
    if (existing !== undefined && existing !== null) {
      job.value = await request<Job>(`/api/tasks/${existing.id}`);
    } else if (followUp) {
      job.value = await request<Job>(`/api/tasks/${job.value!.id}/follow-up`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(followUp),
      });
    } else {
      job.value = await submitAnalysis(text.value);
    }
    metricsJob.value = job.value;
    conversation.value = await request<ConversationTurn[]>(
      `/api/tasks/${job.value.id}/conversation`,
    );
    submitted.value = conversation.value[0].contribution.text;
    history.value = await request<Job[]>("/api/tasks");
    // HTTP 202 只表示任务已接收，仍需轮询任务自身的 status。
    while (!["succeeded", "failed"].includes(job.value.status)) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      job.value = await request<Job>(`/api/tasks/${job.value.id}`);
      metricsJob.value = job.value;
    }
    history.value = await request<Job[]>("/api/tasks");
    if (job.value.status === "failed") {
      throw new Error(job.value.error ?? "分析未能完成。");
    }
    result.value = job.value.result;
    // 完成后才清空本轮草稿，避免新问题误用上一轮相同编号的回答。
    questionDrafts.value = {};
    skippedQuestions.value = [];
  } catch (cause) {
    if (followUp) {
      job.value = previousJob;
      result.value = previousResult;
      conversation.value = previousConversation;
    }
    if (cause instanceof Error) {
      error.value = cause.message;
    } else {
      error.value = "请求失败，请检查本地服务后重试。";
    }
  } finally {
    loading.value = false;
  }
}

/** 刷新本机历史及资料核对范围；这里不测试模型或 worker 是否在线。 */
async function loadHistory() {
  try {
    history.value = await request<Job[]>("/api/tasks");
    knowledge.value = await request<KnowledgeReview>("/api/knowledge/review");
  } catch (cause) {
    if (cause instanceof Error) {
      error.value = cause.message;
    } else {
      error.value = "无法加载本地记录。";
    }
  }
}

/** 读取选中轮次的真实父链和结果，不新建分析任务。 */
function openHistory(item: Job) {
  text.value = "";
  void submit(item);
}

/** 请求删除终态记录；能否删除由后端根据状态及子轮次关系决定。 */
async function deleteHistory(item: Job) {
  try {
    await request<void>(`/api/tasks/${item.id}`, { method: "DELETE" });
    history.value = history.value.filter((row) => row.id !== item.id);
    if (metricsJob.value?.id === item.id) {
      metricsJob.value = null;
    }
    if (job.value?.id === item.id) {
      job.value = null;
      result.value = null;
      error.value = "";
      text.value = "";
      submitted.value = "";
      conversation.value = [];
    }
  } catch (cause) {
    if (cause instanceof Error) {
      error.value = cause.message;
    } else {
      error.value = "删除失败。";
    }
  }
}

onMounted(loadHistory);
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="拾言首页"
        ><span class="brand-mark">拾</span
        ><span>拾言<span class="brand-sub">健康信息整理</span></span></a
      >
      <span class="local-badge"><i></i>本地运行 · 多轮整理</span>
    </header>
    <main>
      <section class="intro">
        <p class="eyebrow">从你的描述开始</p>
        <h1>零散的话，也值得<br /><span>被认真理解。</span></h1>
        <p class="intro-copy">
          不必知道医学术语。说说哪里不舒服、持续了多久，<br
            class="desktop-break"
          />我们一起把已知信息和不确定的地方理清楚。
        </p>
      </section>
      <div class="workspace">
        <section class="input-panel panel">
          <div class="section-heading">
            <span class="step">01</span>
            <h2>描述你的感受</h2>
          </div>
          <form @submit.prevent="submit()">
            <label for="description">症状、时间，以及你的疑问</label>
            <textarea
              id="description"
              v-model="text"
              :disabled="loading"
              maxlength="1200"
              required
              placeholder="例如：最近两天偶尔头晕，站起来时更明显。我猜是低血糖，但没有测过……"
            ></textarea>
            <div class="input-meta">
              <span>不需要填写姓名或联系方式</span
              ><span>{{ text.length }} / 1200</span>
            </div>
            <div class="example-area">
              <p>也可以从一个例子开始</p>
              <button
                v-for="(example, index) in examples"
                :key="example"
                type="button"
                class="example"
                :disabled="loading"
                @click="text = example"
              >
                <span>0{{ index + 1 }}</span
                >{{ example }}<span aria-hidden="true">↗</span>
              </button>
            </div>
            <button
              class="submit"
              type="submit"
              :disabled="loading || !text.trim()"
            >
              <span>{{
                loading ? "正在整理描述…" : job ? "开始新分析" : "整理并分析"
              }}</span
              ><span aria-hidden="true">{{ loading ? "◌" : "→" }}</span>
            </button>
            <p class="privacy-note">
              描述与结果保存在本机，可删除已完成的记录。
            </p>
          </form>
          <div v-if="conversation.length" class="conversation-thread">
            <h4>本次会话</h4>
            <article
              v-for="turn in conversation"
              :key="turn.id"
              class="conversation-card"
            >
              <strong>{{
                turn.contribution.action === "initial"
                  ? "最初描述"
                  : turn.contribution.action === "correct"
                    ? "用户纠正（以此为准）"
                    : turn.contribution.action === "skip"
                      ? "已跳过，仍未知"
                      : "补充回答"
              }}</strong>
              <template v-if="turn.contribution.action === 'batch'">
                <div
                  v-for="item in turn.contribution.items"
                  :key="item.index"
                  class="conversation-answer"
                >
                  <p class="conversation-question">{{ item.question }}</p>
                  <p>{{ item.text }}</p>
                </div>
              </template>
              <template v-else>
                <p
                  v-if="turn.contribution.action !== 'initial'"
                  class="conversation-question"
                >
                  {{ turn.contribution.question }}
                </p>
                <p>{{ turn.contribution.text }}</p>
              </template>
            </article>
          </div>
        </section>
        <section
          class="result-panel panel"
          aria-live="polite"
          :aria-busy="loading"
        >
          <div class="section-heading">
            <span class="step">02</span>
            <h2>理解与线索</h2>
            <span
              v-if="result && !loading && metricsJob?.id === job?.id"
              class="elapsed"
              title="本次成功执行耗时，不含排队和此前失败的尝试"
              >执行 {{ result.elapsed_seconds.toFixed(1) }} 秒</span
            >
          </div>
          <TaskMetrics
            v-if="metricsJob"
            :job="metricsJob"
            :showing-parent="!loading && Boolean(result) && metricsJob.id !== job?.id"
          />
          <div v-if="loading" class="empty-state" :class="{ 'with-metrics': metricsJob }">
            <div class="orbit loading-orbit" aria-hidden="true">✳</div>
            <h3>正在梳理这段描述</h3>
            <p v-if="job">
              {{ job.trace?.at(-1) || jobStates[job.status] }} · 第
              {{ job.attempts }} 次执行
            </p>
            <p>
              本地模型需要一些时间。<br />首次运行可能需要加载模型，请稍候。
            </p>
            <p class="muted">
              若长时间停留在排队状态，请确认任务处理程序已启动。
            </p>
          </div>
          <div v-if="!loading && error" class="error-state" role="alert">
            <h3>{{ data ? "本次补充未能完成" : "这次分析未能完成" }}</h3>
            <p>{{ error }}</p>
            <p class="muted">
              {{
                data
                  ? "原问题、已填写的回答和跳过选择均已保留，可以修改后再次提交。"
                  : "输入已保留，服务恢复后可以再次提交。"
              }}
            </p>
          </div>
          <template v-if="!loading && data">
            <div
              v-if="data.status === 'urgent_attention'"
              class="urgent"
              role="alert"
            >
              <strong>请优先寻求帮助</strong>
              <p>{{ data.safety_notice }}</p>
            </div>
            <div class="result-title">
              <span class="eyebrow">{{ statuses[data.status] }}</span>
              <h3>{{ data.summary }}</h3>
            </div>
            <details class="original">
              <summary>最初描述（补充与纠正见会话记录）</summary>
              <p>{{ submitted }}</p>
            </details>
            <FactReview
              v-if="data.facts.length"
              :key="job?.id"
              :facts="data.facts"
              :disabled="loading"
              @correct="submit(undefined, $event)"
            />
            <div v-if="data.directions.length" class="directions">
              <h4>可能相关的方向 <small>并非诊断</small></h4>
              <article
                v-for="(direction, index) in data.directions"
                :key="index"
              >
                <h3>
                  <span>0{{ index + 1 }}</span
                  >{{ direction.name }}
                </h3>
                <p>{{ direction.reason }}</p>
                <p class="limitation">仍需确认：{{ direction.limitation }}</p>
                <p class="source-note">
                  {{
                    direction.source_ids?.length
                      ? "关联参考片段见下方；引用不等于诊断成立"
                      : "未关联资料依据，属于模型推测"
                  }}
                </p>
                <p
                  v-for="source in result?.sources.filter((source) =>
                    direction.source_ids?.includes(source.id),
                  )"
                  :key="source.id"
                  class="source-note"
                >
                  参考：<a
                    v-if="source.source_url"
                    :href="source.source_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    >{{ source.source_title }}</a
                  ><span v-else>{{ source.name }}（未核验）</span>
                </p>
              </article>
            </div>
            <FollowUpCards
              v-if="
                data.missing_information.length &&
                data.status !== 'urgent_attention'
              "
              :questions="data.missing_information"
              :disabled="loading"
              v-model:drafts="questionDrafts"
              v-model:skipped="skippedQuestions"
              @reply="submit(undefined, $event)"
            />
            <p v-if="data.status !== 'urgent_attention'" class="result-note">
              {{ data.safety_notice }}
            </p>
            <p class="source-note">
              {{
                result?.origin === "safety_rule"
                  ? "症状提示规则 · 未调用模型"
                  : "本地模型生成 · 资料核对范围见下方；未经医学专家审核"
              }}
            </p>
            <details v-if="result?.sources.length" class="original">
              <summary>本次参考片段与来源状态</summary>
              <article v-for="source in result.sources" :key="source.id">
                <h4>{{ source.name }}</h4>
                <p>{{ source.snippet }}</p>
                <p
                  v-for="neighbor in source.context"
                  :key="neighbor.start_index"
                  class="source-note"
                >
                  同版本相邻片段（偏移 {{ neighbor.start_index }}）：{{
                    neighbor.snippet
                  }}
                </p>
                <p v-if="source.attribution" class="source-note">
                  {{ source.attribution }}
                </p>
                <p class="source-note">
                  {{
                    source.verification === "source_checked"
                      ? "摘要已核对公开来源；医学专家审核待完成"
                      : source.verification === "official_public_summary"
                        ? "MedlinePlus 官方摘要；主题分类及医学专家审核待完成"
                        : "原数据集片段，未经核验"
                  }}
                </p>
                <a
                  v-if="source.source_url"
                  :href="source.source_url"
                  target="_blank"
                  rel="noopener noreferrer"
                  >{{ source.source_title }}</a
                >
              </article>
              <p>
                这是检索命中记录，不代表对应疾病适用于当前描述，也不证明模型结论受到资料支持。
              </p>
            </details>
            <details v-if="job?.trace.length" class="original">
              <summary>处理过程</summary>
              <p v-for="step in job.trace" :key="step">{{ step }}</p>
            </details>
          </template>
          <div v-if="!loading && !data && !error" class="empty-state">
            <div class="orbit" aria-hidden="true">✳</div>
            <h3>先说说你的感受</h3>
            <p>这里会呈现你的陈述、个人猜测，<br />以及仍需了解的信息。</p>
            <div class="empty-steps">
              <span>理解描述</span><b>·</b><span>保留疑问</span><b>·</b
              ><span>探索方向</span>
            </div>
          </div>
        </section>
      </div>
      <aside v-if="knowledge" class="knowledge-status">
        <template v-if="knowledge.active_family === 'medlineplus_summary'"
          >资料范围：{{ knowledge.active_documents }} 个 MedlinePlus
          英文健康主题摘要；医学专家审核待完成，库外问题可能缺少依据。</template
        >
        <template v-else
          >资料范围：{{ knowledge.source_checked }} /
          {{ knowledge.total_records }}
          条已核对公开来源摘要；医学专家审核待完成。{{
            knowledge.policy === "reviewed_only"
              ? "当前只检索已核对条目，库外问题可能缺少依据。"
              : "当前为全库研究模式，含未经核验内容。"
          }}
        </template>
      </aside>
      <section v-if="history.length" class="panel history-panel">
        <div class="section-heading">
          <h2>本机记录</h2>
          <button
            type="button"
            class="history-action"
            :disabled="loading"
            @click="loadHistory"
          >
            刷新
          </button>
        </div>
        <div v-for="item in history" :key="item.id" class="history-row">
          <button
            type="button"
            class="history-item"
            :disabled="loading"
            @click="openHistory(item)"
          >
            {{ (item.display_text ?? item.text).slice(0, 50)
            }}<small
              >{{
                item.turn_action === "correct"
                  ? "纠正 · "
                  : ["answer", "batch"].includes(item.turn_action ?? "")
                    ? "补充 · "
                    : item.turn_action === "skip"
                      ? "跳过 · "
                      : ""
              }}{{ jobStates[item.status] }} ·
              {{ new Date(item.created_at).toLocaleString() }}</small
            >
          </button>
          <button
            v-if="['succeeded', 'failed'].includes(item.status)"
            type="button"
            class="history-action"
            :disabled="loading"
            @click="deleteHistory(item)"
          >
            删除
          </button>
        </div>
      </section>
      <aside class="safety-footer">
        <span class="safety-symbol" aria-hidden="true">＋</span>
        <p>
          <strong>严重不适时，请先寻求医疗帮助。</strong>
          如出现严重胸痛、呼吸困难或突然一侧肢体无力，请立即联系当地急救服务，不要等待本工具分析。未出现提醒不代表安全。
        </p>
      </aside>
    </main>
    <footer>
      <span>拾言 / HEALTH EXPLORER</span
      ><span>用于信息整理与探索，不代替专业诊疗</span>
    </footer>
  </div>
</template>
