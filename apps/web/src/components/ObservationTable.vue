<script setup lang="ts">
import type { Observation } from "../api";
import { fmt, label } from "../labels";
import StatusTag from "./StatusTag.vue";

// Records the monitor has seen, with the alert level and why it was given.
defineProps<{ rows: Observation[] }>();
</script>

<template>
  <el-table :data="rows">
    <el-table-column label="标题" prop="title" min-width="260" />
    <el-table-column label="来源" prop="source" width="120" />
    <el-table-column label="记录" prop="record_id" min-width="140" />
    <el-table-column label="可见时间" width="170">
      <template #default="{ row }">{{ fmt(row.available_at) }}</template>
    </el-table-column>
    <el-table-column label="警示" width="80">
      <template #default="{ row }"><StatusTag kind="alert" :value="row.alert" /></template>
    </el-table-column>
    <el-table-column label="结案" width="70">
      <template #default="{ row }">{{ row.closure ? "是" : "" }}</template>
    </el-table-column>
    <el-table-column label="理由" min-width="200">
      <template #default="{ row }">{{ label("reason", row.reason) }}</template>
    </el-table-column>
    <el-table-column v-if="rows.some((r) => 'topic_id' in r)" label="专题" width="100">
      <template #default="{ row }">
        <RouterLink v-if="row.topic_id" :to="`/topics/${row.topic_id}`">查看</RouterLink>
      </template>
    </el-table-column>
  </el-table>
</template>
