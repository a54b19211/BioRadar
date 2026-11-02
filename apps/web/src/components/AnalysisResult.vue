<script setup lang="ts">
import { computed } from "vue";
import type { AnalysisFull } from "../api";
import { pretty } from "../labels";
import CiteLink from "./CiteLink.vue";
import StatusTag from "./StatusTag.vue";

// What the analysis concluded: judgments, checks, computations and the mechanism evaluation.
const props = defineProps<{ a: AnalysisFull }>();

const assessment = computed(() => props.a.result?.assessment);
const checks = computed(() => props.a.result?.checks ?? props.a.progress.checks ?? []);
const computations = computed(() => props.a.result?.computations ?? []);
const mechanism = computed(() => props.a.result?.mechanism);
const titles = { overall: "总体评价", gaps: "缺口", conflicts: "冲突" } as const;
const mechanismParts = computed(() =>
  (Object.keys(titles) as (keyof typeof titles)[])
    .filter((k) => mechanism.value?.[k] != null)
    .map((k) => ({ key: k, title: titles[k], text: pretty(mechanism.value?.[k]) })),
);
</script>

<template>
  <template v-if="assessment">
    <h3>判断　<StatusTag kind="alert" :value="assessment.alert" /></h3>
    <el-table :data="assessment.judgments">
      <el-table-column label="问题" prop="question" min-width="160" />
      <el-table-column label="结论" prop="conclusion" min-width="220" />
      <el-table-column label="置信" width="90">
        <template #default="{ row }"><StatusTag kind="confidence" :value="row.confidence" /></template>
      </el-table-column>
      <el-table-column label="真实性置信" width="100">
        <template #default="{ row }"><StatusTag kind="level" :value="row.credibility" /></template>
      </el-table-column>
      <el-table-column label="潜在后果" width="90">
        <template #default="{ row }"><StatusTag kind="level" :value="row.consequence" /></template>
      </el-table-column>
      <el-table-column label="核实优先级" width="100">
        <template #default="{ row }"><StatusTag kind="level" :value="row.priority" /></template>
      </el-table-column>
      <el-table-column label="依据" width="110">
        <template #default="{ row }"><CiteLink v-for="g in row.grounds" :key="g" :id="g" /></template>
      </el-table-column>
      <el-table-column label="尚未证实" prop="unproven" min-width="200" />
    </el-table>
  </template>

  <template v-if="checks.length">
    <h3>待查问题</h3>
    <el-table :data="checks">
      <el-table-column label="编号" prop="id" width="70" />
      <el-table-column label="问题" prop="text" min-width="300" />
      <el-table-column label="必需" width="70">
        <template #default="{ row }">{{ row.required ? "是" : "否" }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><StatusTag kind="checkStatus" :value="row.status" /></template>
      </el-table-column>
      <el-table-column label="证据" width="180">
        <template #default="{ row }"><CiteLink v-for="e in row.evidence" :key="e" :id="e" /></template>
      </el-table-column>
    </el-table>
  </template>

  <template v-if="computations.length">
    <h3>计算</h3>
    <el-table :data="computations">
      <el-table-column label="计算内容" prop="label" min-width="180" />
      <el-table-column label="结果" width="130">
        <template #default="{ row }">{{ row.value }} {{ row.unit }}</template>
      </el-table-column>
      <el-table-column label="算式" prop="expression" width="150" />
      <el-table-column label="总体 / 期间" min-width="160">
        <template #default="{ row }">{{ [row.population, row.period, row.denominator].filter(Boolean).join(" / ") || "-" }}</template>
      </el-table-column>
      <el-table-column label="数值来源" min-width="220">
        <template #default="{ row }">
          <div v-for="(o, i) in row.operands" :key="i">x{{ i + 1 }} = {{ o.value }}（{{ o.fact }}，<CiteLink :id="o.ref" />）</div>
        </template>
      </el-table-column>
    </el-table>
  </template>

  <template v-if="mechanism">
    <h3>机制评价　<StatusTag kind="mechanismStatus" :value="mechanism.status" /></h3>
    <p v-if="mechanism.reason" class="muted">{{ mechanism.reason }}</p>
    <div v-for="part in mechanismParts" :key="part.key">
      <b>{{ part.title }}</b>
      <pre class="text">{{ part.text }}</pre>
    </div>
  </template>
</template>
