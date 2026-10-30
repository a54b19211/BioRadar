<script setup lang="ts">
import { computed } from "vue";
import { reportSources, reportReadingParts, sourceLabel } from "../../api/reportDelivery";
import type { ResearchAnalysisView } from "../../api/research";
const props = defineProps<{ analysis: ResearchAnalysisView }>();
const emit = defineEmits<{ source: [key: string] }>();
const sources = computed(() => reportSources(props.analysis));
const parts = computed(() => reportReadingParts(props.analysis.result?.report?.final_text || "", sources.value));
</script>

<template>
  <section class="report-source-index" aria-label="系统来源索引">
    <h4>来源索引（系统补充）</h4>
    <p class="muted">来源标签与下方阅读注释不属于原保存正文，未纳入原正文核验。可打开本版固定原文；转述准确不等于传言属实。</p>
    <details v-if="sources.length" class="source-reading no-print">
      <summary>用来源名、时间与标题阅读引用</summary>
      <p class="muted">这是添加系统来源标签的阅读视图；已核文本及原编号见上方原稿，下载文件将索引单独列在正文之后。</p>
      <div class="annotated-report"><template v-for="(part, index) in parts" :key="index"><button v-if="part.source?.evidenceKey" type="button" class="citation-link" @click="emit('source', part.source.evidenceKey)">{{ sourceLabel(part.source) }}</button><span v-else-if="part.source" class="notice">【{{ part.text }} 无法绑定本版固定原文】</span><template v-else>{{ part.text }}</template></template></div>
    </details>
    <ol v-if="sources.length">
      <li v-for="source in sources" :key="source.citation">
        <template v-if="source.evidenceKey">
          <button type="button" class="citation-link" @click="emit('source', source.evidenceKey)">{{ sourceLabel(source) }}</button>
          <small class="muted"> · 原编号 {{ source.citation }} · 固定版本 {{ source.version }}</small>
          <span v-if="source.role === 'graph_background'"> · 图谱背景，不替代原文证据</span>
        </template>
        <span v-else class="notice">{{ source.citation }}：无法绑定本版固定原文，未猜测对应来源。</span>
      </li>
    </ol>
    <p v-else class="muted">正文未找到可识别的编号引用，不能据此推断引用完整或事实已经核实。</p>
  </section>
</template>

<style scoped>
.report-source-index { border: 1px solid var(--el-border-color); padding: 14px; margin: 16px 0; }
.report-source-index li { margin: 10px 0; overflow-wrap: anywhere; }
.citation-link { font: inherit; color: var(--el-color-primary); background: none; border: none; padding: 0; text-align: left; cursor: pointer; text-decoration: underline; }
.annotated-report { white-space: pre-wrap; line-height: 1.8; }
.annotated-report .citation-link { background: var(--el-fill-color-light); margin: 0 3px; }
</style>
