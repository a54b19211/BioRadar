<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { ElButton, ElInput, ElOption, ElSelect } from "element-plus";
import {
  engineRequest,
  type InvestigationView,
  type InvestigationEntryCreate,
  type SnapshotSummary,
  type NormalizedSnapshot,
} from "../../api/engine";
const props = defineProps<{
  investigation: InvestigationView;
  snapshots: SnapshotSummary[];
  busy: boolean;
  loading: boolean;
  sourceCursor: string | null;
  save: (body: InvestigationEntryCreate) => Promise<boolean>;
}>();
const emit = defineEmits<{ loadSources: []; readError: [error: unknown] }>();
const entryMode = ref<"material" | "judgment" | "note">("material");
const materialSnapshotId = ref("");
const materialSnapshot = ref<NormalizedSnapshot | null>(null);
const recordIndex = ref<number | null>(null);
const sourceLoading = ref(false);
const stance = ref<"support" | "oppose" | "context" | "uncertain">("context");
const materialNote = ref("");
const quote = ref("");
const judgmentText = ref("");
const conditions = ref("");
const assessment = ref<"provisional" | "unable_to_determine">(
  "unable_to_determine",
);
const note = ref("");
const stanceLabels = {
  support: "支持材料",
  oppose: "反面材料",
  context: "背景材料",
  uncertain: "关系未确定",
};
const assessmentLabels = {
  provisional: "临时判断，仍需核实",
  unable_to_determine: "当前无法判断",
};
const materials = computed(() =>
  props.investigation.entries.filter(
    (entry) => entry.payload.kind === "material",
  ),
);
const materialRecord = computed(() =>
  recordIndex.value == null
    ? null
    : (materialSnapshot.value?.records[recordIndex.value] ?? null),
);
const writable = computed(
  () =>
    props.investigation.status !== "closed" && !props.busy && !props.loading,
);
let disposed = false;
let sourceVersion = 0;
async function loadMaterialSnapshot(id: string) {
  const version = ++sourceVersion;
  materialSnapshot.value = null;
  recordIndex.value = null;
  quote.value = "";
  if (!id) {
    sourceLoading.value = false;
    return;
  }
  sourceLoading.value = true;
  try {
    const detail = await engineRequest<{ snapshot: NormalizedSnapshot }>(
      "/snapshots/" + encodeURIComponent(id),
    );
    if (
      !disposed &&
      version === sourceVersion &&
      materialSnapshotId.value === id
    )
      materialSnapshot.value = detail.snapshot;
  } catch (error) {
    if (!disposed && version === sourceVersion) emit("readError", error);
  } finally {
    if (!disposed && version === sourceVersion) sourceLoading.value = false;
  }
}
async function addMaterial() {
  if (
    !writable.value ||
    !materialRecord.value ||
    sourceLoading.value ||
    !materialNote.value.trim()
  )
    return;
  const saved = await props.save({
    expected_revision: props.investigation.revision,
    kind: "material",
    text: materialNote.value.trim(),
    snapshot_id: materialSnapshotId.value,
    record_ref: materialRecord.value.record_ref,
    quote: quote.value.trim() || null,
    stance: stance.value,
  });
  if (saved && !disposed) {
    materialNote.value = "";
    quote.value = "";
  }
}
async function addJudgment() {
  if (!writable.value || !judgmentText.value.trim()) return;
  const saved = await props.save({
    expected_revision: props.investigation.revision,
    kind: "judgment",
    text: judgmentText.value.trim(),
    conditions: conditions.value.trim(),
    assessment: assessment.value,
    basis_entry_ids: materials.value.map((entry) => entry.id),
  });
  if (saved && !disposed) {
    judgmentText.value = "";
    conditions.value = "";
  }
}
async function addNote() {
  if (!writable.value || !note.value.trim()) return;
  const saved = await props.save({
    expected_revision: props.investigation.revision,
    kind: "note",
    text: note.value.trim(),
  });
  if (saved && !disposed) note.value = "";
}
watch(
  materialSnapshotId,
  (id) => {
    void loadMaterialSnapshot(id);
  },
  { flush: "sync" },
);
watch(
  recordIndex,
  () => {
    quote.value = "";
  },
  { flush: "sync" },
);
onUnmounted(() => {
  disposed = true;
  ++sourceVersion;
});
</script>

<template>
  <section class="engine-panel no-print investigation-form">
    <h2>继续补充这项调查</h2>
    <p class="muted">记录更新后，输入区重新开始。未保存内容不会写入历史。</p>
    <p v-if="investigation.status === 'closed'" class="muted">
      已停止跟进。需要新增记录时，先点“继续跟进”。
    </p>
    <div class="source-tabs">
      <ElButton
        :type="entryMode === 'material' ? 'primary' : 'default'"
        @click="entryMode = 'material'"
        >追加材料</ElButton
      >
      <ElButton
        :type="entryMode === 'judgment' ? 'primary' : 'default'"
        @click="entryMode = 'judgment'"
        >记录判断</ElButton
      >
      <ElButton
        :type="entryMode === 'note' ? 'primary' : 'default'"
        @click="entryMode = 'note'"
        >写笔记</ElButton
      >
    </div>
    <template v-if="entryMode === 'material'">
      <label for="investigation-source">选择已采用资料</label>
      <ElSelect
        id="investigation-source"
        v-model="materialSnapshotId"
        :disabled="!writable"
        placeholder="先在雷达采用资料包"
        ><ElOption
          v-for="item in snapshots"
          :key="item.id"
          :value="item.id"
          :label="
            (item.is_demo ? '教学演示' : '采用资料') +
            ' · ' +
            item.source_instance +
            ' · ' +
            item.record_count +
            ' 条 · ' +
            item.snapshot_hash.slice(0, 10)
          "
      /></ElSelect>
      <ElButton
        v-if="sourceCursor"
        text
        :disabled="busy || loading"
        @click="emit('loadSources')"
        >加载更多采用资料</ElButton
      >
      <p v-if="sourceLoading" class="muted">正在读取固定资料…</p>
      <template v-if="materialSnapshot">
        <label for="investigation-record">选择其中的记录</label
        ><ElSelect
          id="investigation-record"
          v-model="recordIndex"
          :disabled="!writable"
          placeholder="选择具体记录"
          ><ElOption
            v-for="(record, index) in materialSnapshot.records"
            :key="index"
            :value="index"
            :label="
              record.record_ref.object_id +
              ' · ' +
              record.record_ref.adopted_version
            "
        /></ElSelect>
      </template>
      <details v-if="materialRecord" class="source-reading">
        <summary>核对本次固定资料</summary>
        <pre class="source-text">{{
          materialRecord.normalized_text ??
          "本记录没有可读正文，请结合采用的结构化字段。"
        }}</pre>
        <pre v-if="!materialRecord.normalized_text" class="source-text">{{
          JSON.stringify(materialRecord, null, 2)
        }}</pre>
      </details>
      <label for="investigation-stance">与当前问题的关系（人工分类）</label
      ><ElSelect
        id="investigation-stance"
        v-model="stance"
        :disabled="!writable"
        ><ElOption
          v-for="(label, value) in stanceLabels"
          :key="value"
          :label="label"
          :value="value"
      /></ElSelect>
      <label for="investigation-quote">引用固定文本中的原句（可留空）</label
      ><ElInput
        id="investigation-quote"
        v-model="quote"
        :disabled="!writable"
        type="textarea"
        :rows="2"
        maxlength="4000"
      />
      <label for="investigation-material-note">为什么纳入</label
      ><ElInput
        id="investigation-material-note"
        v-model="materialNote"
        :disabled="!writable"
        type="textarea"
        :rows="2"
        maxlength="2000"
      />
      <ElButton
        type="primary"
        :loading="busy"
        :disabled="
          !writable || !materialRecord || sourceLoading || !materialNote.trim()
        "
        @click="addMaterial"
        >保存材料</ElButton
      >
    </template>
    <template v-else-if="entryMode === 'judgment'">
      <label for="investigation-assessment">当前能否判断</label
      ><ElSelect
        id="investigation-assessment"
        v-model="assessment"
        :disabled="!writable"
        ><ElOption
          v-for="(label, value) in assessmentLabels"
          :key="value"
          :label="label"
          :value="value"
          :disabled="value === 'provisional' && !materials.length"
      /></ElSelect>
      <label for="investigation-judgment">研究人员判断</label
      ><ElInput
        id="investigation-judgment"
        v-model="judgmentText"
        :disabled="!writable"
        type="textarea"
        :rows="4"
        maxlength="4000"
      />
      <label for="investigation-conditions">成立条件、仍缺什么、下一步</label
      ><ElInput
        id="investigation-conditions"
        v-model="conditions"
        :disabled="!writable"
        type="textarea"
        :rows="3"
        maxlength="4000"
      />
      <p class="muted">
        本版将明确纳入当前
        {{ materials.length }}
        项材料（含支持与反面），保留之前的判断。无材料时仍只是一项人工记录。
      </p>
      <ElButton
        type="primary"
        :loading="busy"
        :disabled="!writable || !judgmentText.trim()"
        @click="addJudgment"
        >保存新一版判断</ElButton
      >
    </template>
    <template v-else>
      <label for="investigation-note">调查笔记（不算来源证据）</label
      ><ElInput
        id="investigation-note"
        v-model="note"
        :disabled="!writable"
        type="textarea"
        :rows="4"
        maxlength="4000"
      />
      <ElButton
        type="primary"
        :loading="busy"
        :disabled="!writable || !note.trim()"
        @click="addNote"
        >保存笔记</ElButton
      >
    </template>
  </section>
</template>
