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
import type { ResearchAnalysisView } from "../../api/research";
import { reviewTargets, reviewLabels, type ReviewTarget } from "../../api/humanReview";
const props = defineProps<{
  investigation: InvestigationView;
  reviewAnalysis?: ResearchAnalysisView | null;
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
const reviewTargetKey = ref("");
const targetOptions = ref<{ value: string; label: string; target: ReviewTarget }[]>([]);
const decision = ref<"needs_verification" | "dismissed" | "accepted_within_scope">("needs_verification");
const reviewScope = ref("");
const reviewError = ref("");
const basisIds = ref<string[]>([]);
const rateSource = ref(false);
const rateInformation = ref(false);
const ratingRecordId = ref("");
const sourceGrade = ref("F");
const informationGrade = ref("6");
const publisherScope = ref("");
const ratingBasis = ref("");
const confirmationIds = ref<string[]>([]);
const confirmationBasis = ref("");
let targetGeneration = 0;
watch(() => JSON.stringify([
  props.reviewAnalysis?.id, props.reviewAnalysis?.input_hash, props.reviewAnalysis?.state,
  props.reviewAnalysis?.result?.report?.text_sha256, props.reviewAnalysis?.result?.report?.final_text,
  props.reviewAnalysis?.result?.claims, props.investigation.origin?.run_id,
  props.investigation.origin?.result_hash, props.investigation.origin?.candidate_key,
]), async () => {
  const token = ++targetGeneration;
  const oldTarget = targetOptions.value.find(item => item.value === reviewTargetKey.value)?.target;
  const origin = props.investigation.origin;
  const sameCandidate = oldTarget?.type === "candidate" && origin?.run_id === oldTarget.run_id && origin.result_hash === oldTarget.result_hash && origin.candidate_key === oldTarget.candidate_key;
  if (reviewTargetKey.value && !sameCandidate) {
    reviewTargetKey.value = "stale-target";
    reviewError.value = "目标内容或固定版已变化，请重新选择审核对象；已写判断保留。";
  }
  const candidates: { value: string; label: string; target: ReviewTarget }[] = origin ? [{ value: "candidate", label: "本调查的原始固定候选", target: { type: "candidate", run_id: origin.run_id, result_hash: origin.result_hash, candidate_key: origin.candidate_key } }] : [];
  try {
    const options = await reviewTargets(props.reviewAnalysis);
    if (token === targetGeneration) targetOptions.value = [...candidates, ...options.map((item,index) => ({ ...item, value: "analysis-" + index }))];
  } catch {
    if (token === targetGeneration) { targetOptions.value = candidates; reviewError.value = "无法核对命题哈希；请使用已保存的固定候选或稍后重新读取。"; }
  }
}, { immediate: true });
const chosenTarget = computed(() => targetOptions.value.find(item => item.value === reviewTargetKey.value)?.target);
const chosenMaterials = computed(() => materials.value.filter(entry => basisIds.value.includes(entry.id)));
const chosenClaim = computed(() => chosenTarget.value?.type === "claim" ? props.reviewAnalysis?.result?.claims.find(claim => claim.id === (chosenTarget.value as { claim_id: string }).claim_id) : undefined);
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
  reviewError.value = "";
  const target = chosenTarget.value;
  const fixedReview = reviewTargetKey.value !== "";
  if (fixedReview && (!target || !reviewScope.value.trim())) { reviewError.value = "请选择固定审核对象并填写适用范围。"; return; }
  const basis = fixedReview ? chosenMaterials.value : materials.value;
  const review = target && fixedReview ? { decision: decision.value, scope: reviewScope.value.trim(), target } : undefined;
  let axes: Record<string, unknown> | undefined;
  if (fixedReview && (rateSource.value || rateInformation.value)) {
    const primary = basis.find(entry => entry.id === ratingRecordId.value);
    if (!primary || primary.payload.kind !== "material" || !ratingBasis.value.trim()) { reviewError.value = "显式评级须选择已采用的固定原件并说明评级依据。"; return; }
    const references = [...new Map(basis.filter(entry => entry.payload.kind === "material").map(entry => { const r = entry.payload.kind === "material" ? entry.payload.support.record_ref : null; return [JSON.stringify(r), r]; })).values()].filter(Boolean);
    const common = { record_ref: primary.payload.support.record_ref, assessment_basis_refs: references, assessment_basis: ratingBasis.value.trim() };
    axes = { source_reliability: [], information_credibility: [] };
    if (rateSource.value && !publisherScope.value.trim()) { reviewError.value = "请说明所选发布者评级的适用范围。"; return; }
    if (rateSource.value) axes.source_reliability = [{ ...common, grade: sourceGrade.value, publisher_scope: publisherScope.value.trim() }];
    if (rateInformation.value) {
      if (!chosenClaim.value) { reviewError.value = "信息可信度须选择具体命题，不能给整份报告合成分数。"; return; }
      axes.information_credibility = [{ ...common, grade: informationGrade.value, claim_id: chosenClaim.value.id, claim_text: chosenClaim.value.text,
        independent_confirmation_refs: basis.filter(entry => confirmationIds.value.includes(entry.id) && entry.payload.kind === "material").map(entry => entry.payload.kind === "material" ? entry.payload.support.record_ref : null),
        independent_confirmation_basis: confirmationBasis.value.trim() }];
    }
  }
  const request = {
    expected_revision: props.investigation.revision, kind: "judgment" as const,
    text: judgmentText.value.trim(), conditions: conditions.value.trim(), assessment: assessment.value,
    basis_entry_ids: basis.map(entry => entry.id), ...(review ? { review } : {}), ...(axes ? { assessment_axes: axes } : {}),
  };
  const saved = await props.save(request as InvestigationEntryCreate);
  if (saved && !disposed) { judgmentText.value = ""; conditions.value = ""; reviewScope.value = ""; }
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
      <label for="review-target">审核对象</label><ElSelect id="review-target" v-model="reviewTargetKey" :disabled="!writable">
        <ElOption value="" label="一般调查判断（不批准具体报告）" />
        <ElOption v-for="item in targetOptions" :key="item.value" :value="item.value" :label="item.label" />
      </ElSelect>
      <template v-if="reviewTargetKey">
        <label for="review-decision">人工决定</label><ElSelect id="review-decision" v-model="decision" :disabled="!writable"><ElOption v-for="(label,key) in reviewLabels" :key="key" :value="key" :label="label" /></ElSelect>
        <label for="review-scope">这次核实适用的对象与范围</label><ElInput id="review-scope" v-model="reviewScope" :disabled="!writable" maxlength="2000" />
        <fieldset :disabled="!writable"><legend>明确选择本次判断的固定依据（包括反面材料）</legend>
          <label v-for="entry in materials" :key="entry.id"><input v-model="basisIds" type="checkbox" :value="entry.id">第 {{ entry.revision }} 版 · {{ entry.payload.text }}</label>
        </fieldset>
        <fieldset :disabled="!writable"><legend>可选：分别评定来源与具体信息，不改变证据等级或热度</legend>
          <label><input v-model="rateSource" type="checkbox">评定所选来源可靠性</label>
          <label><input v-model="rateInformation" type="checkbox" :disabled="!chosenClaim">评定所选具体命题可信度（须先选择命题）</label>
          <template v-if="rateSource || rateInformation">
            <label for="rating-record">评级对象所在的固定原件</label><ElSelect id="rating-record" v-model="ratingRecordId"><ElOption v-for="entry in chosenMaterials" :key="entry.id" :value="entry.id" :label="entry.payload.text" /></ElSelect>
            <template v-if="rateSource"><label for="source-grade">来源可靠性（A–F）</label><ElSelect id="source-grade" v-model="sourceGrade"><ElOption v-for="grade in ['A','B','C','D','E','F']" :key="grade" :value="grade" :label="grade === 'F' ? 'F · 无依据评定' : grade" /></ElSelect><label for="publisher-scope">该来源评级的适用范围</label><ElInput id="publisher-scope" v-model="publisherScope" maxlength="2000" /></template>
            <template v-if="rateInformation"><label for="information-grade">具体信息可信度（1–6）</label><ElSelect id="information-grade" v-model="informationGrade"><ElOption v-for="grade in ['1','2','3','4','5','6']" :key="grade" :value="grade" :label="grade === '6' ? '6 · 无依据评定' : grade" /></ElSelect>
              <template v-if="informationGrade === '1'"><p>1 级须至少两份可核对的独立来源原件确认；转载不算独立确认。程序还会核对来源独立性。</p><label v-for="entry in chosenMaterials" :key="entry.id"><input v-model="confirmationIds" type="checkbox" :value="entry.id">独立确认依据：{{ entry.payload.text }}</label><label for="confirmation-basis">独立确认的判断依据</label><ElInput id="confirmation-basis" v-model="confirmationBasis" maxlength="4000" /></template>
            </template>
            <label for="rating-basis">评级依据（不能仅填机构名或传播量）</label><ElInput id="rating-basis" v-model="ratingBasis" type="textarea" maxlength="4000" />
          </template>
          <p class="muted">F／6 是未能评定，不是虚假或 50% 概率；评级人、时间、原件版本和追加修订由系统保存。</p>
        </fieldset>
      </template>
      <p v-if="reviewError" role="alert">{{ reviewError }}</p>
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
        {{ reviewTargetKey ? '本次只纳入明确勾选的固定依据' : '一般调查判断纳入当前所有材料（含支持与反面）' }}，保留之前的判断；范围内采纳不签发对外警报。
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
