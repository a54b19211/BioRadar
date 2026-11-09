<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { addReview, canReview, type AnalysisFull, type ReviewAction } from "../api";
import { fmt, label } from "../labels";
import CiteLink from "./CiteLink.vue";
import StatusTag from "./StatusTag.vue";

// BioCheck's sentence-by-sentence check, and what people decided about it. Rejecting a sentence or
// flagging a material owes the topic (if there is one) a new analysis.
const props = defineProps<{ a: AnalysisFull }>();
const emit = defineEmits<{ changed: [] }>();

const claims = computed(() => props.a.result?.review?.claims ?? []);
const target = ref<{ action: ReviewAction; sentence?: number; evidence_ref?: string } | null>(null);
const note = ref("");
const busy = ref(false);

function open(action: ReviewAction, sentence?: number, evidence_ref?: string) {
  target.value = { action, sentence, evidence_ref };
  note.value = "";
}

async function submit() {
  busy.value = true;
  try {
    await addReview({ ...target.value!, analysis_id: props.a.id, note: note.value.trim() });
    target.value = null;
    ElMessage.success("已记录");
    emit("changed");
  } finally {
    busy.value = false;
  }
}

function what(r: AnalysisFull["reviews"][number]) {
  return r.sentence != null ? `第 ${r.sentence} 句` : r.evidence_ref ? `材料 ${r.evidence_ref}` : "";
}
</script>

<template>
  <template v-if="claims.length">
    <h3>逐句核验</h3>
    <el-table :data="claims">
      <el-table-column label="句" prop="index" width="60" />
      <el-table-column label="句子" prop="text" min-width="320" />
      <el-table-column label="核验" width="110">
        <template #default="{ row }"><StatusTag kind="claimVerdict" :value="row.verdict" /></template>
      </el-table-column>
      <el-table-column label="核验意见" prop="reason" min-width="160" />
      <el-table-column label="引用" width="140">
        <template #default="{ row }"><CiteLink v-for="c in row.citations" :key="c" :id="c" /></template>
      </el-table-column>
      <el-table-column v-if="canReview()" label="复核" width="150">
        <template #default="{ row }">
          <el-button link type="primary" @click="open('confirm', row.index)">确认</el-button>
          <el-button link type="danger" @click="open('reject', row.index)">驳回</el-button>
        </template>
      </el-table-column>
    </el-table>
  </template>

  <div v-if="canReview() && a.evidence.length" class="row" style="margin-top: 8px">
    <span class="muted">标记材料有误：</span>
    <el-button v-for="e in a.evidence" :key="e.ref" size="small" @click="open('flag_source', undefined, e.ref)">{{
      e.ref
    }}</el-button>
  </div>

  <template v-if="a.reviews.length">
    <h3>人工复核记录</h3>
    <el-table :data="a.reviews">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmt(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="人" prop="by" width="100" />
      <el-table-column label="动作" width="120">
        <template #default="{ row }">{{ label("reviewAction", row.action) }}</template>
      </el-table-column>
      <el-table-column label="对象" width="100">
        <template #default="{ row }">{{ what(row) }}</template>
      </el-table-column>
      <el-table-column label="说明" prop="note" min-width="220" />
    </el-table>
  </template>

  <el-dialog :model-value="!!target" :title="target ? label('reviewAction', target.action) : ''" width="480px" @close="target = null">
    <p class="muted">
      {{ target?.sentence != null ? `报告第 ${target.sentence} 句` : `材料 ${target?.evidence_ref}` }}
      <template v-if="target?.action !== 'confirm'">；记录后，所在专题会重新分析并回应这条意见。</template>
    </p>
    <el-input v-model="note" type="textarea" :rows="3" maxlength="2000" placeholder="说明（可选）" />
    <template #footer>
      <el-button @click="target = null">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">记录</el-button>
    </template>
  </el-dialog>
</template>
