<script setup lang="ts">
import { ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";
import { addReview, canReview, getTopic, type Alert, type TopicEvent, type TopicFull } from "../api";
import { fmt, label } from "../labels";
import StatusTag from "../components/StatusTag.vue";
import ObservationTable from "../components/ObservationTable.vue";

const route = useRoute();
const topic = ref<TopicFull | null>(null);

watch(
  () => route.params.id as string | undefined,
  async (id) => {
    topic.value = null;
    if (id) topic.value = await getTopic(id);
  },
  { immediate: true },
);

async function setAlert(alert: Alert) {
  const t = topic.value!;
  await addReview({ action: "set_alert", topic_id: t.id, alert });
  ElMessage.success("已记录");
  topic.value = await getTopic(t.id);
}

function describe(e: TopicEvent): string {
  switch (e.kind) {
    case "created":
      return `首条记录 ${e.record}：${e.title}`;
    case "observed":
      return `观察到记录 ${e.record}，警示${label("alert", e.alert)}${e.closure ? "，宣布结案" : ""}`;
    case "alert":
      return `${label("alert", e.before)} → ${label("alert", e.after)}（${e.by}）`;
    case "state":
      return `${label("topicState", e.before)} → ${label("topicState", e.after)}（${label("reason", e.reason)}）`;
    default:
      return `原因：${label("reason", e.reason)}`;
  }
}
</script>

<template>
  <RouterLink to="/topics">返回专题列表</RouterLink>
  <el-skeleton v-if="!topic" :rows="6" animated style="margin-top: 16px" />
  <template v-else>
    <h2 style="margin-top: 12px">{{ topic.title }}</h2>
    <div class="row">
      <StatusTag kind="topicState" :value="topic.state" />
      <StatusTag kind="alert" :value="topic.alert" />
      <el-select
        v-if="canReview()"
        :model-value="topic.alert"
        size="small"
        style="width: 110px"
        @change="setAlert"
      >
        <el-option v-for="a in ['normal', 'important', 'urgent']" :key="a" :value="a" :label="label('alert', a)" />
      </el-select>
      <el-tag size="small" effect="plain">{{ label("direction", topic.direction) }}</el-tag>
      <el-tag v-if="topic.is_demo" size="small" effect="plain" type="warning">模拟资料</el-tag>
      <el-tag v-if="topic.closed" size="small" effect="plain">已结案</el-tag>
      <span class="muted"
        >热度 {{ topic.heat }}，唤醒 {{ topic.awakenings }} 次，最近活动
        {{ fmt(topic.last_activity_at) }}</span
      >
      <RouterLink v-if="topic.last_analysis_id" :to="`/analyses/${topic.last_analysis_id}`"
        >最近分析</RouterLink
      >
    </div>

    <h3>事件</h3>
    <el-timeline>
      <el-timeline-item v-for="(e, i) in topic.events" :key="i" :timestamp="fmt(e.at)">
        <b>{{ label("eventKind", e.kind) }}</b
        >　{{ describe(e) }}
        <RouterLink v-if="e.analysis_id" :to="`/analyses/${e.analysis_id}`">查看分析</RouterLink>
      </el-timeline-item>
    </el-timeline>

    <h3>观察到的材料</h3>
    <ObservationTable :rows="topic.observations" />
  </template>
</template>
