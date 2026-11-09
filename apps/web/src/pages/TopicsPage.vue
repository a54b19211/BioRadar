<script setup lang="ts">
import { ref, watch } from "vue";
import { listTopics, type Topic } from "../api";
import { fmt, label } from "../labels";
import StatusTag from "../components/StatusTag.vue";

const isDemo = ref(false);
const topics = ref<Topic[]>([]);

watch(
  isDemo,
  async (demo) => {
    const got = await listTopics(demo);
    if (demo === isDemo.value) topics.value = got; // ignore a slow answer for the previous switch position
  },
  { immediate: true },
);
</script>

<template>
  <h2>专题</h2>
  <div class="row" style="margin-bottom: 12px">
    <el-switch v-model="isDemo" />
    <span>模拟资料</span>
  </div>
  <el-table :data="topics">
    <el-table-column label="专题" min-width="300">
      <template #default="{ row }"
        ><RouterLink :to="`/topics/${row.id}`">{{ row.title }}</RouterLink></template
      >
    </el-table-column>
    <el-table-column label="方向" width="80">
      <template #default="{ row }">{{ label("direction", row.direction) }}</template>
    </el-table-column>
    <el-table-column label="状态" width="90">
      <template #default="{ row }"><StatusTag kind="topicState" :value="row.state" /></template>
    </el-table-column>
    <el-table-column label="情报等级" width="130">
      <template #default="{ row }">
        <StatusTag kind="alert" :value="row.alert" />
        <el-tag
          v-if="row.publishable === false"
          type="warning"
          size="small"
          effect="plain"
          :title="label('hold', row.hold_reason)"
          >待核</el-tag
        >
      </template>
    </el-table-column>
    <el-table-column label="热度" prop="heat" width="70" />
    <el-table-column label="唤醒次数" prop="awakenings" width="90" />
    <el-table-column label="最近活动" width="170">
      <template #default="{ row }">{{ fmt(row.last_activity_at) }}</template>
    </el-table-column>
    <el-table-column label="最近分析" width="100">
      <template #default="{ row }">
        <RouterLink v-if="row.last_analysis_id" :to="`/analyses/${row.last_analysis_id}`">查看</RouterLink>
      </template>
    </el-table-column>
  </el-table>
</template>
