<script setup lang="ts">
import { ref, watch } from "vue";
import type { components } from "../../api/generated/engine";

type Definition = components["schemas"]["TopicDefinition"];
type Template = components["schemas"]["TopicTemplate"];
const props = defineProps<{
  templates: Template[];
  initial?: Definition;
  isDemo?: boolean;
  editing?: boolean;
  busy: boolean;
}>();
const emit = defineEmits<{
  save: [body: { definition: Definition; is_demo: boolean }];
  cancel: [];
}>();
const templateId = ref("");
const definition = ref<Definition | null>(null);
const demo = ref(false);
const termsAll = ref("");
const termsAny = ref("");
const sourceIds = ref("");
const sourceSites = ref("");
const metricIds = ref("");
const error = ref("");
const domains: Record<string, string> = {
  pipeline: "管线",
  research: "科研",
  business: "商业",
  public_health: "公共卫生",
  policy: "政策",
  regulatory: "监管与安全",
  supply: "制造与供应",
  regional: "区域布局",
  other: "其他专题",
};
function apply(value: Definition) {
  definition.value = JSON.parse(JSON.stringify(value));
  termsAll.value = (value.terms_all ?? []).join("，");
  termsAny.value = (value.terms_any ?? []).join("，");
  sourceIds.value = value.source_ids.join("，");
  sourceSites.value = (value.source_sites ?? []).join("，");
  metricIds.value = (value.metric_ids ?? []).join("，");
  error.value = "";
}
function chooseTemplate() {
  const selected = props.templates.find((item) => item.id === templateId.value);
  if (selected) apply(selected.definition);
}
watch(
  () => props.initial,
  (value) => {
    demo.value = props.isDemo ?? false;
    if (value) apply(value);
    else if (props.templates[0]) {
      templateId.value = props.templates[0].id;
      chooseTemplate();
    }
  },
  { immediate: true },
);
function list(value: string): string[] {
  return value
    .split(/[,，;；\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}
function save() {
  if (!definition.value) return;
  const value = {
    ...definition.value,
    source_instance: demo.value ? "fixture" : "datafabric-test",
    title: definition.value.title.trim(),
    question: definition.value.question.trim(),
    terms_all: list(termsAll.value),
    terms_any: list(termsAny.value),
    source_ids: list(sourceIds.value),
    source_sites: list(sourceSites.value),
    metric_ids: list(metricIds.value),
  };
  if (
    !value.title ||
    !value.question ||
    !value.source_ids.length ||
    !value.profiles.length
  ) {
    error.value = "请填写标题、研究问题，并保留至少一个来源和观察方法。";
    return;
  }
  emit("save", { definition: value, is_demo: demo.value });
}
</script>

<template>
  <form
    v-if="definition"
    class="engine-panel topic-form"
    @submit.prevent="save"
  >
    <div class="section-heading">
      <h2>{{ editing ? "修改研究范围" : "新建长期专题" }}</h2>
      <button
        type="button"
        class="text-button"
        :disabled="busy"
        @click="emit('cancel')"
      >
        取消
      </button>
    </div>
    <p v-if="editing" class="notice">
      修改后会保留旧范围和旧记录，按新范围重新检查已采用资料。
    </p>
    <label v-else
      >从一个例子开始
      <select v-model="templateId" :disabled="busy" @change="chooseTemplate">
        <option v-for="item in templates" :key="item.id" :value="item.id">
          {{ item.title }}
        </option>
      </select>
    </label>
    <p v-if="!editing" class="muted">
      {{ templates.find((item) => item.id === templateId)?.qualification }}
    </p>
    <label
      >专题名称<input
        v-model="definition.title"
        maxlength="200"
        required
        :disabled="busy"
    /></label>
    <label
      >要长期回答的问题
      <textarea
        v-model="definition.question"
        rows="3"
        maxlength="2000"
        required
        :disabled="busy"
      ></textarea>
    </label>
    <div class="topic-form-row">
      <label
        >所属方向
        <select v-model="definition.domain" :disabled="busy">
          <option v-for="(label, key) in domains" :key="key" :value="key">
            {{ label }}
          </option>
        </select>
      </label>
      <label
        >定期复查间隔（小时）
        <input
          v-model.number="definition.cadence_hours"
          type="number"
          min="1"
          max="720"
          required
          :disabled="busy"
        />
      </label>
    </div>
    <label
      >这些词至少出现一个（逗号分隔，留空观察整个所选范围）
      <input
        v-model="termsAny"
        :disabled="busy"
        placeholder="例如：GLP-1，semaglutide"
      />
    </label>
    <details>
      <summary>调整来源和计算口径</summary>
      <div class="topic-form-fields">
        <label>来源编号<input v-model="sourceIds" :disabled="busy" /></label>
        <label
          >旧库中的站点编号（其他来源可留空）<input
            v-model="sourceSites"
            :disabled="busy"
        /></label>
        <label
          >必须同时出现的词（逗号分隔）<input
            v-model="termsAll"
            :disabled="busy"
        /></label>
        <label
          >指标编号（没有指定指标时留空）<input
            v-model="metricIds"
            :disabled="busy"
        /></label>
        <fieldset :disabled="busy">
          <legend>观察什么</legend>
          <label
            ><input
              v-model="definition.profiles"
              type="checkbox"
              value="text_exposure"
            />
            所选材料中的词组变化</label
          >
          <label
            ><input
              v-model="definition.profiles"
              type="checkbox"
              value="registered_state"
            />
            登记状态和里程碑</label
          >
          <label
            ><input
              v-model="definition.profiles"
              type="checkbox"
              value="typed_metric"
            />
            有明确单位、时间和对象的数值</label
          >
          <label
            ><input
              v-model="definition.profiles"
              type="checkbox"
              value="source_event"
            />
            来源明确记载的事件</label
          >
        </fieldset>
        <div class="topic-form-row">
          <label
            >汇总周期<select v-model="definition.bucket" :disabled="busy">
              <option value="month">月</option>
              <option value="week">周</option>
            </select></label
          >
          <label
            >展示最近几个周期<input
              v-model.number="definition.lookback_buckets"
              type="number"
              min="3"
              max="120"
              required
              :disabled="busy"
          /></label>
        </div>
        <label
          >词组线索最低材料数<input
            v-model.number="definition.minimum_support"
            type="number"
            min="2"
            max="100"
            required
            :disabled="busy"
        /></label>
        <p class="muted">
          词组计数用于发现待核问题。材料数不等于独立研究数，模板也不表示这些来源已经完整覆盖。
        </p>
      </div>
    </details>
    <label class="topic-checkbox"
      ><input v-model="demo" type="checkbox" :disabled="busy || editing" />
      仅使用教学资料（需对应教学来源许可，不会把真实资料改为教学）</label
    >
    <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
    <button type="submit" class="topic-primary" :disabled="busy">
      {{ busy ? "正在保存…" : editing ? "保存新范围" : "开始持续观察" }}
    </button>
  </form>
</template>

<style scoped>
.topic-form,
.topic-form-fields {
  display: grid;
  gap: 15px;
}
.topic-form label {
  display: grid;
  gap: 7px;
  font-size: 13px;
  line-height: 1.7;
}
.topic-form input:not([type="checkbox"]),
.topic-form textarea,
.topic-form select {
  width: 100%;
  box-sizing: border-box;
  padding: 9px 11px;
  border: 1px solid #cbd6c5;
  border-radius: 5px;
  color: #304d3c;
  background: white;
  font: inherit;
}
.topic-form textarea {
  resize: vertical;
}
.topic-form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
}
.topic-form details summary {
  cursor: pointer;
  padding: 8px 0;
  font-size: 13px;
}
.topic-form fieldset {
  border: 1px solid #dce4d7;
  border-radius: 6px;
  padding: 14px;
}
.topic-form fieldset label,
.topic-form .topic-checkbox {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.topic-primary {
  justify-self: start;
  padding: 10px 18px;
  background: #385e45;
  color: white;
  border: 0;
  border-radius: 5px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.6;
  cursor: default;
}
.text-button {
  border: 0;
  background: none;
  color: #45624d;
  cursor: pointer;
}
@media (max-width: 650px) {
  .topic-form-row {
    grid-template-columns: 1fr;
  }
}
</style>
