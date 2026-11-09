<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import {
  ask,
  getOptions,
  listAnalyses,
  type Analysis,
  type Direction,
  type Method,
  type Options,
} from "../api";
import { fmt, label } from "../labels";
import StatusTag from "../components/StatusTag.vue";

const router = useRouter();
const options = ref<Options | null>(null);
const recent = ref<Analysis[]>([]);
const busy = ref(false);

const form = reactive({
  question: "",
  direction: "science" as Direction,
  method: "planner" as Method,
  is_demo: false,
  cutoff: "", // "YYYY-MM-DD HH:mm:ss" from the picker, read as Beijing time
});

const services = {
  model: "模型",
  planner: "规划服务",
  biocheck: "报告核验",
  mechevidence: "机制评价",
  weknora: "知识库",
};

onMounted(async () => {
  [options.value, recent.value] = await Promise.all([getOptions(), listAnalyses()]);
});

async function submit() {
  busy.value = true;
  try {
    const cutoff = form.cutoff ? form.cutoff.replace(" ", "T") + "+08:00" : null;
    const created = await ask({
      ...form,
      question: form.question.trim(),
      cutoff,
    });
    router.push(`/analyses/${created.id}`);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <h2>提问与调查</h2>
  <div class="row" v-if="options" style="margin-bottom: 12px">
    <el-tag
      v-for="(name, key) in services"
      :key="key"
      :type="options.configured[key] ? 'success' : 'info'"
      size="small"
    >
      {{ name }}{{ options.configured[key] ? "已配置" : "未配置" }}
    </el-tag>
  </div>

  <el-card>
    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="研究问题（至少 4 个字）">
        <el-input v-model="form.question" type="textarea" :rows="3" maxlength="2000" show-word-limit />
      </el-form-item>
      <div class="row" style="align-items: flex-start; gap: 24px">
        <el-form-item label="研究方向">
          <el-select v-model="form.direction" style="width: 140px">
            <el-option
              v-for="d in ['pipeline', 'science', 'business', 'public_health']"
              :key="d"
              :value="d"
              :label="label('direction', d)"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="调查方法">
          <el-select v-model="form.method" style="width: 140px">
            <el-option v-for="m in ['planner', 'baseline']" :key="m" :value="m" :label="label('method', m)" />
          </el-select>
        </el-form-item>
        <el-form-item label="模拟资料">
          <el-switch v-model="form.is_demo" />
        </el-form-item>
        <el-form-item label="历史回放截止时间（北京时间，可不填）">
          <el-date-picker
            v-model="form.cutoff"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="不限"
          />
        </el-form-item>
      </div>
      <el-button
        type="primary"
        native-type="submit"
        :loading="busy"
        :disabled="form.question.trim().length < 4"
      >
        开始调查
      </el-button>
    </el-form>
  </el-card>

  <h3>最近的分析</h3>
  <el-table
    :data="recent"
    class="clickable"
    @row-click="(row: Analysis) => router.push(`/analyses/${row.id}`)"
  >
    <el-table-column label="问题" prop="question" min-width="320" show-overflow-tooltip />
    <el-table-column label="方法" width="100">
      <template #default="{ row }">{{ label("method", row.method) }}</template>
    </el-table-column>
    <el-table-column label="状态" width="90">
      <template #default="{ row }"><StatusTag kind="state" :value="row.state" /></template>
    </el-table-column>
    <el-table-column label="阶段" width="100">
      <template #default="{ row }">{{ label("stage", row.stage) }}</template>
    </el-table-column>
    <el-table-column label="核验" width="100">
      <template #default="{ row }"
        ><StatusTag v-if="row.result?.report_status" kind="reportStatus" :value="row.result.report_status"
      /></template>
    </el-table-column>
    <el-table-column label="创建时间" width="170">
      <template #default="{ row }">{{ fmt(row.created_at) }}</template>
    </el-table-column>
  </el-table>
</template>
