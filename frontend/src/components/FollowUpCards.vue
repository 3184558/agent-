<script setup lang="ts">
// 独立填写、统一提交；组件只收集输入，任务创建和失败恢复由父页面负责。
import { computed } from "vue";
import type { FollowUpBatch } from "../api";

const props = defineProps<{ questions: string[]; disabled: boolean }>();
// 编号只属于当前父轮次；草稿和跳过选择通过 v-model 存在父组件中。
const drafts = defineModel<Record<number, string>>("drafts", {
  required: true,
});
const skipped = defineModel<number[]>("skipped", { required: true });
const emit = defineEmits<{
  reply: [value: FollowUpBatch];
}>();
/** 生成本批提交内容：明确跳过优先，非空回答保留，其他项目不提交。 */
const items = computed(() => {
  const submittedItems: FollowUpBatch["items"] = [];
  for (let index = 0; index < props.questions.length; index += 1) {
    if (skipped.value.includes(index)) {
      submittedItems.push({ action: "skip", index, text: "" });
      continue;
    }
    const answerText = (drafts.value[index] ?? "").trim();
    if (answerText) {
      submittedItems.push({ action: "answer", index, text: answerText });
    }
  }
  return submittedItems;
});
// 用于展示本批组成，不把明确跳过计入已回答数量。
const answerCount = computed(
  () => items.value.filter((item) => item.action === "answer").length,
);
</script>

<template>
  <form
    class="questions follow-up-cards"
    @submit.prevent="emit('reply', { action: 'batch', items })"
  >
    <h4>补充信息</h4>
    <p class="muted">
      分别填写后统一提交，只分析一次。可以只答部分问题；空白表示尚未回答，不清楚时可勾选跳过。
    </p>
    <div
      v-for="(question, index) in questions"
      :key="index"
      class="follow-up-card"
    >
      <label :for="`answer-${index}`"
        ><span class="step">{{ index + 1 }}</span
        >{{ question }}</label
      >
      <textarea
        :id="`answer-${index}`"
        v-model="drafts[index]"
        maxlength="500"
        :disabled="disabled || skipped.includes(index)"
        placeholder="只回答这个问题，也可以说明你不确定的地方。"
      ></textarea>
      <div class="card-actions">
        <label class="skip-choice">
          <input
            v-model="skipped"
            type="checkbox"
            :value="index"
            :disabled="disabled"
          />
          不清楚，跳过
        </label>
        <span class="muted">{{ (drafts[index] ?? "").length }} / 500</span>
      </div>
    </div>
    <div class="batch-submit">
      <p class="muted">
        已填写 {{ answerCount }} 项，跳过 {{ items.length - answerCount }} 项。
        每条最多500字，会话上下文上限3000字。
      </p>
      <button
        class="reply-button"
        type="submit"
        :disabled="disabled || !items.length"
      >
        提交已填写的回答 →
      </button>
    </div>
  </form>
</template>
