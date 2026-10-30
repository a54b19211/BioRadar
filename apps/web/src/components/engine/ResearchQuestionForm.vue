<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  ElButton,
  ElCheckbox,
  ElInput,
  ElInputNumber,
  ElOption,
  ElSelect,
} from "element-plus";
import { engineRequest, EngineError } from "../../api/engine";
import type {
  ResearchBudget,
  ResearchOptions,
  ResearchQuestionSubmission,
} from "../../api/research";

const props = defineProps<{
  disabled?: boolean;
  submitting?: boolean;
  initialQuestion?: string;
  options?: ResearchOptions | null;
  followUp?: boolean;
  isDemo?: boolean;
  allowGraph?: boolean;
}>();
const emit = defineEmits<{
  submit: [value: ResearchQuestionSubmission];
  graph: [question: string];
  unauthorized: [error: unknown];
}>();
const question = ref(props.initialQuestion ?? "");
const selectedSources = ref<string[]>([]);
const queryPolicy = ref<"" | "baseline" | "query_planner">("");
const loadedOptions = ref<ResearchOptions | null>(null);
const optionsLoading = ref(false);
const error = ref("");
const customBudget = ref(false);
const budget = ref<ResearchBudget>({});
let disposed = false;
const available = computed(() => props.options ?? loadedOptions.value);
const blocked = computed(() =>
  Boolean(
    props.disabled ||
    props.submitting ||
    optionsLoading.value ||
    (available.value &&
      (!available.value.enabled || !available.value.configured)),
  ),
);
const sourceNames: Record<string, string> = {
  clinicaltrials: "ClinicalTrials 临床登记",
  europe_pmc: "Europe PMC 文献",
  legacy_documents: "Fabric 已采用资料",
  fda_crl: "FDA 审评信函",
  who_don: "WHO 疾病通报",
  external_archive: "已导入原始档案",
};
const budgetFields = [
  { key: "max_calls", label: "模型调用次数", unit: "次", minimum: 1 },
  { key: "max_tokens", label: "模型 token 用量", unit: "token", minimum: 128 },
  { key: "max_seconds", label: "调查执行时间", unit: "秒", minimum: 5 },
  { key: "max_queries", label: "检索次数", unit: "次", minimum: 0 },
] as const;

async function loadOptions() {
  if (props.options || optionsLoading.value) return;
  optionsLoading.value = true;
  error.value = "";
  try {
    const value = await engineRequest<ResearchOptions>(
      "/investigations/research-options?is_demo=" +
        String(Boolean(props.isDemo)),
    );
    if (!disposed) loadedOptions.value = value;
  } catch (value) {
    if (!disposed) {
      error.value =
        value instanceof Error ? value.message : "研究范围暂时无法读取。";
      if (value instanceof EngineError && [401, 403].includes(value.status))
        emit("unauthorized", value);
    }
  } finally {
    if (!disposed) optionsLoading.value = false;
  }
}

function submit() {
  if (blocked.value || !question.value.trim()) return;
  const limits: ResearchBudget = {};
  if (customBudget.value) {
    for (const { key } of budgetFields) {
      const value = budget.value[key];
      if (typeof value === "number" && Number.isFinite(value))
        limits[key] = value;
    }
  }
  emit("submit", {
    question: question.value.trim(),
    source_ids: [...selectedSources.value],
    ...(Object.keys(limits).length ? { budget: limits } : {}),
    ...(queryPolicy.value ? { query_policy: queryPolicy.value } : {}),
  });
}

onMounted(() => void loadOptions());
onUnmounted(() => {
  disposed = true;
});
</script>

<template>
  <form class="research-question-form no-print" @submit.prevent="submit">
    <label
      class="question-label"
      :for="followUp ? 'follow-up-question' : 'research-question'"
    >
      {{ followUp ? "继续追问" : "你想调查什么？" }}
    </label>
    <ElInput
      :id="followUp ? 'follow-up-question' : 'research-question'"
      v-model="question"
      type="textarea"
      :rows="followUp ? 3 : 4"
      maxlength="4000"
      show-word-limit
      :disabled="disabled || submitting"
      :placeholder="
        followUp
          ? '提出一个新问题，或说明需要补查的反例与适用条件。'
          : '例如：Elranatamab 治疗复发/难治性多发性骨髓瘤，作用机制和疗效的现有证据分别到哪一步？哪些资料仍不足以支持布局决策？'
      "
    />
    <div class="research-scope">
      <label
        >本轮资料来源
        <ElSelect
          v-model="selectedSources"
          multiple
          clearable
          :disabled="blocked"
          placeholder="使用当前获准的研究来源"
        >
          <ElOption
            v-for="source in available?.retrieval_sources ?? []"
            :key="source"
            :value="source"
            :label="sourceNames[source] || source"
          />
        </ElSelect>
      </label>
      <p class="muted" v-if="available">
        {{
          selectedSources.length ? "按所选来源调查" : "由后台在获准来源内选择"
        }}； 当前可补查 {{ available.retrieval_sources.length }} 类来源。
      </p>
    </div>
    <label>
      本轮查询策略
      <ElSelect v-model="queryPolicy" :disabled="blocked">
        <ElOption label="沿用当前研究策略" value="" />
        <ElOption label="逐步查询策略" value="query_planner" />
        <ElOption label="原有顺序基线" value="baseline" />
      </ElSelect>
    </label>
    <details class="research-budget-settings">
      <summary>本轮预算</summary>
      <p class="muted">
        默认使用服务预算。可以为这次调查降低上限，实际用量会随进度显示。
      </p>
      <div v-if="available" class="budget-defaults">
        <span v-for="field in budgetFields" :key="field.key">
          {{ field.label }}：{{
            available.limits[field.key]?.toLocaleString() ?? "由服务确定"
          }}
          {{ available.limits[field.key] == null ? "" : field.unit }}
        </span>
      </div>
      <ElCheckbox v-model="customBudget" :disabled="blocked"
        >为本轮设置更低的上限</ElCheckbox
      >
      <div v-if="customBudget" class="budget-inputs">
        <label v-for="field in budgetFields" :key="field.key">
          {{ field.label }}
          <ElInputNumber
            v-model="budget[field.key]"
            :min="field.minimum"
            :max="available?.limits[field.key]"
            :precision="0"
            :disabled="blocked"
            :placeholder="String(available?.limits[field.key] ?? '默认')"
          />
        </label>
      </div>
    </details>
    <p
      v-if="available && (!available.enabled || !available.configured)"
      class="notice"
    >
      {{
        available.unavailable_reason ||
        "研究模型尚未启用或配置，已有调查与报告仍可阅读。"
      }}
    </p>
    <p v-if="error" class="error-inline" role="alert">
      {{ error }}
      <ElButton text :disabled="optionsLoading" @click="loadOptions"
        >重新读取范围</ElButton
      >
    </p>
    <div class="research-submit">
      <ElButton
        type="primary"
        native-type="submit"
        :loading="submitting"
        :disabled="blocked || !question.trim()"
      >
        {{ followUp ? "继续调查" : "开始调查" }}
      </ElButton>
      <ElButton
        v-if="allowGraph"
        text
        :disabled="disabled || submitting"
        @click="emit('graph', question.trim())"
        >使用图谱辅助调查</ElButton
      >
      <span class="muted">{{
        followUp
          ? "新问题和结果另存一版，原版可继续阅读。"
          : "后台会规划查询、采用原文、形成报告；离开页面后继续执行。"
      }}</span>
    </div>
  </form>
</template>

<style scoped>
.research-question-form {
  display: grid;
  gap: 14px;
}
.question-label {
  font-size: 18px;
  font-weight: 600;
}
.research-scope {
  display: grid;
  gap: 6px;
}
.research-scope label {
  display: grid;
  gap: 6px;
}
.research-scope :deep(.el-select) {
  width: 100%;
}
.research-scope p {
  margin: 0;
}
.research-budget-settings summary {
  cursor: pointer;
}
.budget-defaults,
.budget-inputs {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 22px;
  margin: 12px 0;
}
.budget-defaults {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.budget-inputs label {
  display: grid;
  gap: 6px;
  font-size: 13px;
}
.research-submit {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.research-submit .muted {
  font-size: 13px;
}
</style>
