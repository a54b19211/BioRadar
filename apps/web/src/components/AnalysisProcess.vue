<script setup lang="ts">
import { computed } from "vue";
import type { AnalysisFull } from "../api";
import { label } from "../labels";
import CiteLink from "./CiteLink.vue";
import StatusTag from "./StatusTag.vue";

// How the analysis got there: investigation steps, extracted facts, materials, model calls and notes.
const props = defineProps<{ a: AnalysisFull }>();

const actions = computed(() => props.a.progress.actions ?? []);
const facts = computed(() => props.a.progress.facts ?? []);
const stop = computed(() => props.a.result?.stop ?? props.a.progress.stop);
const notes = computed(() => props.a.result?.notes ?? props.a.progress.notes ?? []);
</script>

<template>
  <h3>调查过程</h3>
  <el-empty v-if="!actions.length" description="还没有调查动作" :image-size="60" />
  <el-timeline v-else>
    <el-timeline-item v-for="(x, i) in actions" :key="i" :type="x.status === 'failed' ? 'danger' : 'primary'">
      <b>{{ label("action", x.kind) }}</b> {{ x.source }}
      <template v-if="x.args.terms">　检索词：{{ x.args.terms.join(" + ") }}</template>
      <template v-if="x.args.page">（第 {{ x.args.page }} 页）</template>
      <template v-if="x.args.record_id">　记录：{{ x.args.record_id }}</template>
      <CiteLink v-if="x.ref" :id="x.ref" />
      <span v-if="x.found != null" class="muted">　命中 {{ x.found }}，新增 {{ x.new }}</span>
      <StatusTag kind="state" :value="x.status" style="margin-left: 8px" />
      <span v-if="x.error" style="color: var(--el-color-danger)">　{{ x.error }}</span>
    </el-timeline-item>
  </el-timeline>
  <p v-if="stop">停止判断：{{ stop.decision }}（{{ stop.reason }}）</p>

  <template v-if="facts.length">
    <h3>摘录的事实</h3>
    <el-table :data="facts">
      <el-table-column label="编号" prop="id" width="70" />
      <el-table-column label="来源" width="80">
        <template #default="{ row }"><CiteLink :id="row.ref" /></template>
      </el-table-column>
      <el-table-column label="事实" prop="text" min-width="240" />
      <el-table-column label="原文" prop="quote" min-width="260" />
      <el-table-column label="立场" width="90">
        <template #default="{ row }">{{ label("stance", row.stance) }}</template>
      </el-table-column>
      <el-table-column label="待查问题" width="100">
        <template #default="{ row }">{{ row.checks.join("、") }}</template>
      </el-table-column>
    </el-table>
  </template>

  <h3>材料</h3>
  <el-table :data="a.evidence">
    <el-table-column label="编号" width="80">
      <template #default="{ row }"><CiteLink :id="row.ref" /></template>
    </el-table-column>
    <el-table-column label="标题" prop="title" min-width="260" />
    <el-table-column label="来源" prop="source" width="120" />
    <el-table-column label="记录" prop="record_id" min-width="140" />
    <el-table-column label="版本" width="130">
      <template #default="{ row }">{{ row.version_id.slice(0, 12) }}</template>
    </el-table-column>
    <el-table-column label="发表时间" prop="published_at" width="110" />
    <el-table-column label="材料类型" prop="material_kind" width="140" />
    <el-table-column label="已核实" width="80">
      <template #default="{ row }">{{ row.verified ? "是" : "否" }}</template>
    </el-table-column>
  </el-table>

  <h3>模型调用</h3>
  <el-table :data="a.calls">
    <el-table-column label="阶段" prop="stage" width="110" />
    <el-table-column label="服务" prop="service" width="110" />
    <el-table-column label="模型" prop="model" min-width="160" />
    <el-table-column label="tokens" prop="tokens" width="90" />
    <el-table-column label="结束原因" prop="finish_reason" width="110" />
    <el-table-column label="用时（秒）" width="100">
      <template #default="{ row }">{{ row.seconds.toFixed(1) }}</template>
    </el-table-column>
    <el-table-column label="错误" prop="error" min-width="140" />
  </el-table>

  <template v-if="notes.length">
    <h3>备注</h3>
    <ul>
      <li v-for="(n, i) in notes" :key="i">
        <b>{{ n.code }}</b>（{{ label("stage", n.stage) }}）{{ n.detail }}
      </li>
    </ul>
  </template>
</template>
