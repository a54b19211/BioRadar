<script setup lang="ts">
import { computed, onBeforeUnmount, provide, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { exportAnalysis, followup, getAnalysis, type AnalysisFull } from "../api";
import { fmt, label } from "../labels";
import StatusTag from "../components/StatusTag.vue";
import CiteLink from "../components/CiteLink.vue";
import EvidenceDrawer from "../components/EvidenceDrawer.vue";
import AnalysisResult from "../components/AnalysisResult.vue";
import AnalysisProcess from "../components/AnalysisProcess.vue";

const route = useRoute();
const router = useRouter();
const id = computed(() => route.params.id as string | undefined);
const a = ref<AnalysisFull | null>(null);
const citing = ref<string | null>(null); // evidence reference shown in the drawer
provide("cite", (ref: string) => (citing.value = ref));

const running = computed(() => a.value?.state === "queued" || a.value?.state === "running");
const usage = computed(() => {
  if (!a.value) return null;
  // result.usage exists only after the run; until then count the calls recorded so far
  return (
    a.value.result?.usage ?? {
      llm_calls: a.value.calls.length,
      requests: a.value.calls.reduce((n, c) => n + c.requests, 0),
      tokens: a.value.calls.reduce((n, c) => n + c.tokens, 0),
    }
  );
});
// the draft with each "[E3]" turned into a clickable link: odd entries are references
const draftParts = computed(() => (a.value?.result?.draft ?? "").split(/\[(E\d+)\]/));

// ---- loading, polling every 4 s while the analysis is queued or running ----

let timer: number | undefined;

async function refresh() {
  const mine = id.value;
  if (!mine) return;
  try {
    const got = await getAnalysis(mine);
    if (mine !== id.value) return; // the page moved on to another analysis meanwhile
    a.value = got;
  } catch (e) {
    if (mine !== id.value) return;
    ElMessage.error(e instanceof Error ? e.message : String(e));
  }
  if (running.value) timer = window.setTimeout(refresh, 4000);
}

watch(
  id,
  () => {
    clearTimeout(timer);
    a.value = null;
    citing.value = null;
    refresh();
  },
  { immediate: true },
);
onBeforeUnmount(() => clearTimeout(timer));

// ---- follow-up question and export ----

const asking = ref(false);
const followText = ref("");
const busy = ref(false);

async function submitFollowup() {
  busy.value = true;
  try {
    const created = await followup(a.value!.id, followText.value.trim());
    asking.value = false;
    followText.value = "";
    router.push(`/analyses/${created.id}`);
  } finally {
    busy.value = false;
  }
}

async function download() {
  const text = await exportAnalysis(a.value!.id);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  link.download = `bioradar-${a.value!.id}.md`;
  link.click();
  URL.revokeObjectURL(link.href);
}
</script>

<template>
  <el-skeleton v-if="!a" :rows="8" animated />
  <template v-else>
    <h2>{{ a.question }}</h2>
    <div class="row">
      <StatusTag kind="state" :value="a.state" />
      <StatusTag v-if="a.state !== 'completed'" kind="stage" :value="a.stage" />
      <span>核验</span>
      <StatusTag :value="a.result?.report_status ?? 'NOT_RUN'" />
      <el-tag size="small" effect="plain">{{ label("direction", a.direction) }}</el-tag>
      <el-tag size="small" effect="plain">{{ label("method", a.method) }}</el-tag>
      <el-tag v-if="a.is_demo" size="small" effect="plain" type="warning">模拟资料</el-tag>
      <el-tag size="small" effect="plain">{{ label("trigger", a.trigger) }}</el-tag>
      <span class="muted">创建 {{ fmt(a.created_at) }}</span>
      <span v-if="a.cutoff" class="muted">历史回放截止 {{ fmt(a.cutoff) }}</span>
      <span v-if="usage" class="muted">模型调用 {{ usage.llm_calls }} 次（HTTP 请求 {{ usage.requests }} 次），{{ usage.tokens }} tokens</span>
      <RouterLink v-if="a.parent_id" :to="`/analyses/${a.parent_id}`">上一轮分析</RouterLink>
    </div>
    <div class="row" style="margin-top: 12px">
      <el-button type="primary" @click="asking = true">追问</el-button>
      <el-button @click="download">导出</el-button>
      <span v-if="running" class="muted">正在运行，页面每 4 秒自动刷新</span>
    </div>

    <el-alert
      v-if="a.state === 'failed'"
      type="error"
      :closable="false"
      style="margin-top: 16px"
      :title="`分析失败：${a.error_code ?? '未知原因'}`"
    >
      <pre v-if="a.result?.trace" class="text">{{ a.result.trace }}</pre>
    </el-alert>

    <h3>报告草稿</h3>
    <el-card v-if="a.result?.draft">
      <p class="text">
        <template v-for="(part, i) in draftParts" :key="i">
          <CiteLink v-if="i % 2" :id="part" />
          <template v-else>{{ part }}</template>
        </template>
      </p>
    </el-card>
    <p v-else class="muted">
      {{ running ? "报告尚未生成。" : "没有生成报告，原因见下方备注。" }}
    </p>

    <AnalysisResult :a="a" />
    <AnalysisProcess :a="a" />
  </template>

  <EvidenceDrawer v-if="id" :analysis-id="id" :ref-id="citing" @close="citing = null" />

  <el-dialog v-model="asking" title="追问" width="520px">
    <el-input
      v-model="followText"
      type="textarea"
      :rows="4"
      maxlength="2000"
      show-word-limit
      placeholder="基于这一轮的结论继续提问（至少 4 个字）"
    />
    <template #footer>
      <el-button @click="asking = false">取消</el-button>
      <el-button
        type="primary"
        :loading="busy"
        :disabled="followText.trim().length < 4"
        @click="submitFollowup"
        >提交</el-button
      >
    </template>
  </el-dialog>
</template>
