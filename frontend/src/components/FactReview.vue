<script setup lang="ts">
// 展示模型提取的事实及原话；用户纠正仍单条提交，不混入批量追问答案。
import { ref } from "vue";
import type { AnalysisResponse } from "../api";
defineProps<{
  facts: AnalysisResponse["analysis"]["facts"];
  disabled: boolean;
}>();
// 只向父页面发送纠正意图，由服务端根据当前轮事实编号恢复被纠正内容。
const emit = defineEmits<{
  correct: [value: { action: "correct"; index: number; text: string }];
}>();
// 同时只展开一条事实的编辑框；纠正草稿是组件内的临时状态。
const editing = ref<number | null>(null);
const correction = ref("");
const kinds = {
  reported: "明确陈述",
  denied: "明确否定",
  hypothesis: "个人猜测",
  uncertain: "尚不确定",
};
</script>
<template>
  <div class="facts">
    <h4>你提到的信息 <small>理解有误时可以纠正</small></h4>
    <div v-for="(fact, index) in facts" :key="index" class="fact-review">
      <div class="fact">
        <span class="tag" :class="fact.kind">{{ kinds[fact.kind] }}</span>
        <div>
          <p>{{ fact.meaning }}</p>
          <blockquote>“{{ fact.quote }}”</blockquote>
        </div>
        <button
          type="button"
          class="correct-link"
          :disabled="disabled"
          @click="
            editing = index;
            correction = '';
          "
        >
          纠正
        </button>
      </div>
      <form
        v-if="editing === index"
        class="correction-form"
        @submit.prevent="
          emit('correct', { action: 'correct', index, text: correction })
        "
      >
        <label :for="`correction-${index}`">请写出应以什么说法为准</label>
        <textarea
          :id="`correction-${index}`"
          v-model="correction"
          maxlength="500"
          :disabled="disabled"
          placeholder="例如：我没有测过体温，不能确定是否发热。"
        ></textarea>
        <div class="card-actions">
          <button type="button" class="history-action" @click="editing = null">
            取消</button
          ><button
            type="submit"
            class="reply-button"
            :disabled="disabled || !correction.trim()"
          >
            提交纠正并重新分析
          </button>
        </div>
      </form>
    </div>
  </div>
</template>
