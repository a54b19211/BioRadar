<script setup lang="ts">
import { ref, watch } from "vue";
import { getEvidence, type EvidenceText } from "../api";

// Shows the stored text of one evidence item, e.g. E3, of an analysis.
const props = defineProps<{ analysisId: string; refId: string | null }>();
defineEmits<{ close: [] }>();

const item = ref<EvidenceText | null>(null);

watch(
  () => props.refId,
  async (refId) => {
    item.value = null;
    if (!refId) return;
    const got = await getEvidence(props.analysisId, refId);
    if (props.refId === refId) item.value = got; // ignore an answer for a reference closed meanwhile
  },
  { immediate: true },
);
</script>

<template>
  <el-drawer :model-value="!!refId" :title="`[${refId}] 原始材料`" size="45%" @close="$emit('close')">
    <el-skeleton v-if="!item" :rows="6" animated />
    <template v-else>
      <h3 style="margin-top: 0">{{ item.title }}</h3>
      <p class="muted">版本 {{ item.version_id }}</p>
      <p v-if="item.url">
        <a v-if="/^https?:\/\//.test(item.url)" :href="item.url" target="_blank" rel="noopener">{{ item.url }}</a>
        <span v-else>{{ item.url }}</span>
      </p>
      <p class="text">{{ item.text }}</p>
    </template>
  </el-drawer>
</template>
