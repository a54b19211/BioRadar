<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import {
  getSources,
  getUsage,
  listObservations,
  tick,
  type Observation,
  type SourceReport,
  type TickSummary,
  type UsageRow,
} from "../api";
import { fmt, label } from "../labels";
import ObservationTable from "../components/ObservationTable.vue";

const isDemo = ref(false);
const observations = ref<Observation[]>([]);
const usage = ref<UsageRow[]>([]);
const sources = ref<SourceReport | null>(null);
const summary = ref<TickSummary | null>(null);
const running = ref(false);

async function loadObservations() {
  const demo = isDemo.value;
  const got = await listObservations(demo);
  if (demo === isDemo.value) observations.value = got;
}

watch(
  isDemo,
  () => {
    summary.value = null;
    loadObservations();
  },
  { immediate: true },
);
onMounted(async () => {
  [usage.value, sources.value] = await Promise.all([getUsage(), getSources()]);
});

async function runRound() {
  running.value = true;
  try {
    summary.value = await tick(isDemo.value);
    [usage.value] = await Promise.all([getUsage(), loadObservations()]);
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <h2>监测</h2>
  <div class="row" style="margin-bottom: 12px">
    <el-switch v-model="isDemo" />
    <span>模拟资料</span>
    <el-button type="primary" :loading="running" style="margin-left: 16px" @click="runRound"
      >运行一轮</el-button
    >
    <span v-if="running" class="muted">正在读取新材料并分诊，可能需要几分钟</span>
  </div>

  <el-card v-if="sources" style="margin-bottom: 16px">
    <template #header>
      来源状态（今日自动 token {{ sources.auto_tokens_today }} / {{ sources.daily_auto_tokens }}）
    </template>
    <el-table :data="sources.sources" size="small">
      <el-table-column label="来源" prop="source" width="140" />
      <el-table-column label="采集任务（Fabric）" min-width="220">
        <template #default="{ row }">
          {{ row.collection.state ?? "未知" }}
          <span v-if="row.collection.last_recorded_success_at" class="muted">
            · 最近成功 {{ fmt(row.collection.last_recorded_success_at) }}</span
          >
          <span v-if="row.collection.scope" class="muted"> · 范围 {{ row.collection.scope }}</span>
        </template>
      </el-table-column>
      <el-table-column label="最近新增入库" width="170">
        <template #default="{ row }">{{ row.last_new_at ? fmt(row.last_new_at) : "无" }}</template>
      </el-table-column>
      <el-table-column label="最新材料发布时间" width="170">
        <template #default="{ row }">{{ row.latest_published_at ?? "未知" }}</template>
      </el-table-column>
      <el-table-column label="待读" prop="lag" width="80" />
      <el-table-column label="说明" prop="note" min-width="200" />
    </el-table>
  </el-card>

  <el-card v-if="summary" style="margin-bottom: 16px">
    <p style="margin-top: 0">
      本轮读取新材料 {{ summary.records }} 条，监测时钟 {{ fmt(summary.clock) }}。
      <span v-if="summary.backlog">还有积压，下一轮继续。</span>
      <span v-if="summary.paused">今日自动 token 已到上限，自动分诊和自动分析暂停。</span>
    </p>
    <p v-for="(reason, topicId) in summary.topics" :key="topicId">
      <RouterLink :to="`/topics/${topicId}`">专题 {{ topicId.slice(0, 8) }}</RouterLink
      >：{{ label("reason", reason) }}
    </p>
    <p v-for="id in summary.analyses" :key="id">
      <RouterLink :to="`/analyses/${id}`">已自动启动分析 {{ id.slice(0, 8) }}</RouterLink>
    </p>
    <p v-if="!Object.keys(summary.topics).length" class="muted" style="margin-bottom: 0">
      没有专题需要处理。
    </p>
  </el-card>

  <h3>最近观察到的材料</h3>
  <ObservationTable :rows="observations" />

  <h3>模型用量</h3>
  <el-table :data="usage">
    <el-table-column label="日期" prop="day" width="140" />
    <el-table-column label="服务" prop="service" width="160" />
    <el-table-column label="调用次数" prop="calls" width="110" />
    <el-table-column label="HTTP 请求" prop="requests" width="110" />
    <el-table-column label="tokens" prop="tokens" />
  </el-table>
</template>
