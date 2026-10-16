<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { ElButton, ElOption, ElSelect, ElTag } from "element-plus";
import {
  engineRequest,
  type SnapshotSummary,
  type SnapshotComparison,
} from "../../api/engine";
const props = defineProps<{ snapshots: SnapshotSummary[] }>();
const beforeId = ref("");
const afterId = ref("");
const busy = ref(false);
const error = ref("");
const result = ref<SnapshotComparison | null>(null);
let version = 0;
let disposed = false;
const before = computed(() =>
  props.snapshots.find((item) => item.id === beforeId.value),
);
const after = computed(() =>
  props.snapshots.find((item) => item.id === afterId.value),
);
const compatible = computed(() =>
  props.snapshots.filter(
    (item) =>
      !before.value ||
      (item.source_instance === before.value.source_instance &&
        item.is_demo === before.value.is_demo),
  ),
);
const statuses: Record<string, string> = {
  added: "本次才纳入",
  content_updated: "内容变更",
  same: "未检测到内容变更",
  not_seen: "本批未见",
};
const reasons: Record<string, string> = {
  new_to_selected_adopted_set_not_new_research_or_event:
    "仅表示新加入所选资料范围，不表示新研究或新事件",
  historical_backfill: "上游标明为历史补录",
  publication_time_semantics_unverified: "日期的公开时间含义未核实",
  not_in_selected_after_package_not_deletion_or_read_failure:
    "所选后一份资料没有此记录，不证明删除或单条读取失败",
  adopted_source_document_hash_changed_not_new_research:
    "采用的源文本内容变化，不等于新研究",
  adopted_text_changed_under_same_normalization_version:
    "相同规范化方法下的采用文本变化",
  source_content_fingerprint_unchanged: "未检测到源内容变化；元数据仍可能不同",
  normalization_changed_text_is_not_a_source_event:
    "文本处理版本变化，不当作来源事件",
  adopted_text_not_comparable: "采用文本不具备直接比较条件",
  source_reference_metadata_changed: "来源版本信息变化",
  source_status_annotations_changed: "来源状态标记变化",
  metadata_or_enrichment_changed_not_new_research:
    "元数据或身份等增强变化，不当作新研究",
  selected_packages_only_no_automatic_direction_filter_or_full_source_sync:
    "只比较这两份已采用资料；没有执行新采集、研究方向过滤或全源同步",
  record_counts_are_not_independent_study_counts: "记录数不是独立研究数",
  adoption_time_is_not_publication_or_event_time:
    "采用观察时间不是公开或事件发生时间",
  missing_record_is_not_deletion_and_has_no_per_record_read_outcome:
    "本批未见不表示删除；资料包没有单条读取结果",
  identity_or_professional_enrichment_is_not_new_research:
    "身份或专业信息补充不等于新的科研进展",
};
function label(item: SnapshotSummary) {
  return (
    (item.is_demo ? "教学演示" : "采用资料") +
    " · " +
    item.source_instance +
    " · " +
    item.record_count +
    " 条 · " +
    item.observed_at.slice(0, 10) +
    " · " +
    item.snapshot_hash.slice(0, 8)
  );
}
async function compare() {
  if (busy.value || !before.value || !after.value) return;
  const request = ++version;
  const left = before.value,
    right = after.value;
  busy.value = true;
  error.value = "";
  result.value = null;
  try {
    const data = await engineRequest<SnapshotComparison>(
      "/snapshot-comparisons?before_id=" +
        encodeURIComponent(left.id) +
        "&after_id=" +
        encodeURIComponent(right.id),
    );
    if (disposed || request !== version) return;
    if (
      data.before_hash !== left.snapshot_hash ||
      data.after_hash !== right.snapshot_hash
    )
      throw new Error("比较返回的资料版本不匹配，请重新选择。");
    result.value = data;
  } catch (value) {
    if (!disposed && request === version)
      error.value =
        value instanceof Error ? value.message : "这两份资料暂时无法比较。";
  } finally {
    if (!disposed && request === version) busy.value = false;
  }
}
watch(
  [beforeId, afterId],
  () => {
    ++version;
    result.value = null;
    error.value = "";
    busy.value = false;
  },
  { flush: "sync" },
);
onUnmounted(() => {
  disposed = true;
  ++version;
});
</script>

<template>
  <section class="engine-panel snapshot-comparison">
    <details>
      <summary>比较两批已采用资料</summary>
      <p class="muted">
        看清所选资料发生了什么变化；不会重新采集上游，也不表示全源同步。
      </p>
      <div class="comparison-controls">
        <div>
          <label for="comparison-before">对照资料</label
          ><ElSelect
            id="comparison-before"
            v-model="beforeId"
            placeholder="选择第一份采用包"
            ><ElOption
              v-for="item in snapshots"
              :key="item.id"
              :value="item.id"
              :label="label(item)"
          /></ElSelect>
        </div>
        <div>
          <label for="comparison-after">本次比较资料</label
          ><ElSelect
            id="comparison-after"
            v-model="afterId"
            placeholder="同一来源实例的另一份采用包"
            ><ElOption
              v-for="item in compatible"
              :key="item.id"
              :value="item.id"
              :label="label(item)"
          /></ElSelect>
        </div>
        <ElButton
          type="primary"
          :loading="busy"
          :disabled="!before || !after"
          @click="compare"
          >比较资料</ElButton
        >
      </div>
      <p v-if="error" class="error-inline" role="alert">{{ error }}</p>
      <template v-if="result">
        <ElTag v-if="result.is_demo" type="warning"
          >教学演示 · 非真实情报</ElTag
        >
        <p class="muted">
          范围：两份已采用记录 · {{ result.source_instance }}<br />对照观察：{{
            result.before_observed_at
          }}
          · 本次比较观察：{{ result.after_observed_at }}
        </p>
        <ul class="evidence-list comparison-counts">
          <li v-for="(count, status) in result.counts" :key="status">
            {{ statuses[status] }}：{{ count }} 条
          </li>
        </ul>
        <div class="decision-table-wrap">
          <table class="decision-table">
            <thead>
              <tr>
                <th>记录</th>
                <th>比较结果</th>
                <th>具体说明</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in result.items"
                :key="JSON.stringify(item.identity)"
              >
                <td>
                  {{ item.identity.provider }} / {{ item.identity.object_id
                  }}<small
                    >前版本：{{ item.before_ref?.adopted_version ?? "本批没有"
                    }}<br />后版本：{{
                      item.after_ref?.adopted_version ?? "本批没有"
                    }}</small
                  >
                </td>
                <td>{{ statuses[item.status] }}</td>
                <td>
                  <p v-for="reason in item.reason_codes" :key="reason">
                    {{ reasons[reason] ?? reason }}
                  </p>
                  <small v-if="item.changed_fields.length"
                    >变化字段：{{ item.changed_fields.join("、") }}</small
                  >
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <ul class="evidence-list muted">
          <li v-for="limitation in result.limitations" :key="limitation">
            {{ reasons[limitation] ?? limitation }}
          </li>
        </ul>
        <details class="run-policy">
          <summary>查看固定输入版本</summary>
          <pre>{{
            JSON.stringify(
              {
                before_hash: result.before_hash,
                after_hash: result.after_hash,
                comparison_version: result.comparison_version,
              },
              null,
              2,
            )
          }}</pre>
        </details>
      </template>
    </details>
  </section>
</template>
