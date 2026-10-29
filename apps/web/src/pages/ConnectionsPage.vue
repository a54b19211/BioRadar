<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElButton, ElTag } from "element-plus";
import {
  getSystemStatus,
  type ProviderStatus,
  type SystemStatus,
} from "../api/system";
import { engineRequest } from "../api/engine";
import type { ResearchOptions, ResearchProvider } from "../api/research";

const data = ref<SystemStatus | null>(null);
const research = ref<ResearchOptions | null>(null);
const error = ref("");
const scopeError = ref("");
const authenticated = ref(false);
const loading = ref(false);
let controller: AbortController | null = null;
async function refresh() {
  controller?.abort();
  const current = new AbortController();
  controller = current;
  loading.value = true;
  error.value = "";
  scopeError.value = "";
  data.value = null;
  research.value = null;
  try {
    const results = await Promise.allSettled([
      getSystemStatus(current.signal),
      engineRequest<{ authenticated: boolean }>("/session", {
        signal: current.signal,
      }),
    ]);
    if (current.signal.aborted) return;
    if (results[0].status === "fulfilled") data.value = results[0].value;
    else
      error.value =
        results[0].reason instanceof Error
          ? results[0].reason.message
          : "运行状态暂不可读取。";
    authenticated.value =
      results[1].status === "fulfilled" && results[1].value.authenticated;
    if (authenticated.value) {
      try {
        const options = await engineRequest<ResearchOptions>(
          "/investigations/research-options",
          { signal: current.signal },
        );
        if (!current.signal.aborted) research.value = options;
      } catch (value) {
        if (!current.signal.aborted)
          scopeError.value =
            value instanceof Error ? value.message : "研究能力状态暂不可读取。";
      }
    }
  } finally {
    if (controller === current) loading.value = false;
  }
}
onMounted(refresh);
onUnmounted(() => controller?.abort());
const labels: Record<string, string> = {
  postgres: "业务数据库",
  redis: "任务队列",
  worker: "后台执行服务",
};
const statusLabels: Record<string, string> = {
  ready: "配置就绪",
  available: "可调用",
  connected: "已连接",
  configured: "已配置",
  not_configured: "未配置",
  unavailable: "暂不可用",
  migration_required: "待更新数据库",
  not_connected: "尚无连接回执",
  disabled: "未启用",
  failed: "调用失败",
};
const providerKeys: Record<string, string> = {
  generation: "biocheck",
  mechanism: "mech_evidence",
  knowledge: "weknora",
};
function currentProvider(provider: ProviderStatus): ResearchProvider | null {
  return (
    research.value?.providers?.[providerKeys[provider.id] || provider.id] ??
    null
  );
}
function providerState(provider: ProviderStatus) {
  const live = currentProvider(provider);
  const state = live?.status || live?.state;
  if (state) return statusLabels[state] || state;
  if (live?.configured === true) return "已配置";
  if (live?.configured === false) return "未配置";
  if (provider.id === "data_fabric" && research.value)
    return "可补查 " + research.value.retrieval_sources.length + " 类来源";
  return statusLabels[provider.state] || provider.state || "状态待确认";
}
const providerEntries = computed(() => data.value?.providers ?? []);
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">DATA & CAPABILITIES</p>
      <h1>数据与能力接入</h1>
      <p class="description">
        查看运行环境、获准来源与共享服务配置；具体调用结果保存在每一版研究记录中。
      </p>
    </div>
    <ElButton :loading="loading" @click="refresh">刷新状态</ElButton>
  </div>
  <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
  <p v-if="loading" role="status">正在读取服务状态…</p>
  <template v-if="data">
    <div class="principle-grid">
      <div
        v-for="(status, name) in data.dependencies"
        :key="name"
        class="connection-card"
      >
        <span>{{ labels[name] ?? name }}</span>
        <ElTag :type="status === 'ready' ? 'success' : 'warning'">{{
          statusLabels[status] ?? status
        }}</ElTag>
      </div>
    </div>
    <p v-if="scopeError" class="notice" role="status">{{ scopeError }}</p>
    <p v-if="!authenticated" class="notice">
      登录研究空间后可读取当前研究能力和来源范围。
      <RouterLink to="/investigations" class="text-link"
        >前往研究空间 →</RouterLink
      >
    </p>
    <p v-if="research" class="muted">
      研究模型：{{
        research.enabled && research.configured
          ? "已配置并启用"
          : "尚未配置或启用"
      }}； 获准补查：{{ research.retrieval_sources.join("、") || "无" }}。
      <RouterLink to="/radar" class="text-link"
        >查看持续取数与后台状态 →</RouterLink
      >
    </p>
    <section class="provider-list">
      <article
        v-for="provider in providerEntries"
        :key="provider.id"
        class="provider-row"
      >
        <div>
          <h2>{{ provider.name }}</h2>
          <p>
            {{
              currentProvider(provider)?.message ||
              currentProvider(provider)?.reason ||
              provider.note
            }}
          </p>
          <span class="owner">负责人：{{ provider.owner }}</span>
        </div>
        <ElTag type="info">{{ providerState(provider) }}</ElTag>
      </article>
    </section>
  </template>
</template>
