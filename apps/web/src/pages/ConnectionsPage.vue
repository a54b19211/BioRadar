<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { ElButton, ElTag } from "element-plus";
import { getSystemStatus, type SystemStatus } from "../api/system";

const data = ref<SystemStatus | null>(null);
const error = ref("");
const loading = ref(false);
let controller: AbortController | null = null;
async function refresh() {
  controller?.abort();
  const current = new AbortController();
  controller = current;
  loading.value = true;
  error.value = "";
  data.value = null;
  try {
    data.value = await getSystemStatus(current.signal);
  } catch (e) {
    if (!current.signal.aborted)
      error.value = e instanceof Error ? e.message : "服务读取失败";
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
  ready: "已连接",
  not_configured: "未配置",
  unavailable: "暂不可用",
  migration_required: "待更新数据库",
};
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">DATA & CAPABILITIES</p>
      <h1>数据与能力接入</h1>
      <p class="description">
        分别确认运行环境、共享服务，以及情报业务的实际可用范围。
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
        <span>{{ labels[name] ?? name }}</span
        ><ElTag :type="status === 'ready' ? 'success' : 'warning'">{{
          statusLabels[status] ?? status
        }}</ElTag>
      </div>
    </div>
    <div class="notice">
      运行环境可用不代表调查能力已接通。下列共享能力完成接口与真实样例验收后，才会用于研究。
    </div>
    <section class="provider-list">
      <article
        v-for="provider in data.providers"
        :key="provider.id"
        class="provider-row"
      >
        <div>
          <h2>{{ provider.name }}</h2>
          <p>{{ provider.note }}</p>
          <span class="owner">负责人：{{ provider.owner }}</span>
        </div>
        <ElTag type="info">待接入验收</ElTag>
      </article>
    </section>
  </template>
</template>
