<script setup lang="ts">
import { computed } from "vue";
import { ElButton } from "element-plus";

type PageKind = "radar" | "topics" | "investigations" | "deliverables";
const props = defineProps<{ kind: PageKind }>();
const pages = {
  radar: {
    eyebrow: "INTELLIGENCE RADAR",
    title: "发现变化，积累判断",
    description:
      "从持续更新的证据中，识别新的变化、重要影响和需要及时核实的事件。",
    action: "发起调查",
    empty: "情报监测尚未启用",
    detail:
      "接入真实证据与监测规则后，待核实信号会出现在这里。当前没有运行监测，不能据此判断没有重要变化。",
    cards: ["有什么新变化", "什么会影响判断", "什么需要尽快核实"],
  },
  topics: {
    eyebrow: "RESEARCH TOPICS",
    title: "让研究持续积累",
    description:
      "围绕管线、技术方向和政策问题，保存研究范围、证据演变与历次判断。",
    action: "新建专题",
    empty: "专题服务尚未启用",
    detail: "专题将保留每次调查与判断的历史，新的证据不会覆盖过去的依据。",
    cards: ["固定研究范围", "记录判断历史", "跟踪证据变化"],
  },
  investigations: {
    eyebrow: "INVESTIGATIONS",
    title: "把问题调查清楚",
    description: "检查资料覆盖、查找反证、计算比较，并明确判断成立的条件。",
    action: "发起调查",
    empty: "调查服务尚未启用",
    detail:
      "调查执行、暂停与修订将在此呈现。外部能力尚未接通，当前不会生成研究结论。",
    cards: ["覆盖与调查取证", "计算比较与研判", "内容核验与复核"],
  },
  deliverables: {
    eyebrow: "RESEARCH OUTPUTS",
    title: "每项结论都有来处",
    description: "阅读情报简报和研究报告，从结论追溯计算、证据与原文。",
    action: "查看待审成果",
    empty: "成果服务尚未启用",
    detail:
      "正式成果绑定固定证据和审阅版本。生成草稿或核验通过，都不代表成果已获准发布。",
    cards: ["证据可以追溯", "分歧明确保留", "修订版本可查"],
  },
} satisfies Record<PageKind, Record<string, string | string[]>>;
const page = computed(() => pages[props.kind]);
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">{{ page.eyebrow }}</p>
      <h1>{{ page.title }}</h1>
      <p class="description">{{ page.description }}</p>
    </div>
    <ElButton type="primary" disabled :title="'该业务功能将在后续任务卡中实现'"
      >＋ {{ page.action }}</ElButton
    >
  </div>
  <div class="principle-grid">
    <div v-for="(card, index) in page.cards" :key="card" class="principle-card">
      <span>0{{ index + 1 }}</span
      >{{ card }}
    </div>
  </div>
  <section class="empty-panel">
    <div class="radar-symbol" aria-hidden="true"><i></i><i></i><b></b></div>
    <h2>{{ page.empty }}</h2>
    <p>{{ page.detail }}</p>
    <RouterLink to="/admin/data" class="text-link"
      >查看数据与能力接入 →</RouterLink
    >
  </section>
  <p class="page-note">当前为开发框架，未载入演示情报，也未执行真实调查。</p>
</template>
