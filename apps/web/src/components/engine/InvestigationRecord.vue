<script setup lang="ts">
import { computed } from "vue";
import { ElButton, ElOption, ElSelect, ElTag } from "element-plus";
import type { InvestigationView, EvidenceSupport } from "../../api/engine";
const props = defineProps<{
  investigation: InvestigationView;
  busy: boolean;
  loading: boolean;
}>();
const emit = defineEmits<{
  print: [];
  status: [status: "open" | "paused" | "closed"];
  question: [
    id: string,
    status: "pending" | "checking" | "resolved" | "blocked",
  ];
  readSource: [
    snapshotId: string,
    snapshotHash: string,
    support: EvidenceSupport,
  ];
}>();
const statusLabels = {
  open: "继续跟进",
  paused: "暂停跟进",
  closed: "已停止跟进",
};
const stanceLabels = {
  support: "支持材料",
  oppose: "反面材料",
  context: "背景材料",
  uncertain: "关系未确定",
};
const questionLabels = {
  pending: "待核对",
  checking: "研究人员核对中",
  resolved: "已记录处理",
  blocked: "暂缺资料",
};
const kindLabels = {
  material: "追加材料",
  judgment: "记录判断",
  note: "调查笔记",
  status: "跟进状态",
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
const latestJudgment = computed(() => {
  const entry = props.investigation.entries.find(
    (entry) => entry.id === props.investigation.latest_judgment_entry_id,
  );
  return entry?.payload.kind === "judgment"
    ? { entry, judgment: entry.payload }
    : null;
});
const writable = computed(
  () =>
    props.investigation.status !== "closed" && !props.busy && !props.loading,
);
function basisEntries(ids: string[]) {
  return props.investigation.entries.filter(
    (entry) => entry.payload.kind === "material" && ids.includes(entry.id),
  );
}
</script>

<template>
  <article class="investigation-record">
    <section class="engine-panel">
      <div class="section-heading">
        <ElTag v-if="investigation.is_demo" type="warning"
          >教学演示 · 非真实情报</ElTag
        ><ElTag>{{ statusLabels[investigation.status] }}</ElTag>
        <ElButton
          class="no-print"
          :disabled="busy || loading"
          @click="emit('print')"
          >打印已保存记录</ElButton
        >
      </div>
      <h2 class="investigation-title">{{ investigation.title }}</h2>
      <p class="investigation-question">{{ investigation.question }}</p>
      <p class="muted">
        研究人员记录，尚未经专业评价；保存、停止跟进或打印都不代表专业批准。
      </p>
      <p v-if="investigation.origin" class="muted">
        原候选：{{ investigation.origin.candidate.title }} ·
        <RouterLink to="/radar" class="text-link">回情报雷达</RouterLink>
      </p>
      <p v-else class="muted">这项调查来自研究人员直接提出的问题。</p>
      <p class="muted">
        可通过命令行从 Data-Fabric 进行有限追查；实际查找过程见调查笔记。
        WeKnora 自动取证与专业评价尚未接通，研究人员仍需核对材料。
      </p>
      <details v-if="investigation.origin" class="run-policy">
        <summary>核对起始计算与资料版本</summary>
        <pre>{{
          JSON.stringify(
            {
              run_id: investigation.origin.run_id,
              result_hash: investigation.origin.result_hash,
              snapshot_id: investigation.origin.snapshot_id,
              snapshot_hash: investigation.origin.snapshot_hash,
            },
            null,
            2,
          )
        }}</pre>
      </details>
      <ul
        v-if="investigation.questions.length"
        class="evidence-list investigation-questions"
      >
        <li v-for="item in investigation.questions" :key="item.question_id">
          <span>{{ item.text }}</span>
          <ElSelect
            class="no-print"
            :model-value="item.status"
            :disabled="!writable"
            aria-label="待查问题进度"
            @change="emit('question', item.question_id, $event)"
          >
            <ElOption
              v-for="(label, value) in questionLabels"
              :key="value"
              :label="label"
              :value="value"
            /> </ElSelect
          ><span class="print-only">{{ questionLabels[item.status] }}</span>
        </li>
      </ul>
      <div class="feedback-actions no-print">
        <ElButton
          v-if="investigation.status !== 'open'"
          :disabled="busy || loading"
          @click="emit('status', 'open')"
          >继续跟进</ElButton
        >
        <ElButton
          v-if="investigation.status === 'open'"
          :disabled="busy || loading"
          @click="emit('status', 'paused')"
          >暂停跟进</ElButton
        >
        <ElButton
          v-if="investigation.status !== 'closed'"
          :disabled="busy || loading"
          @click="emit('status', 'closed')"
          >停止跟进</ElButton
        >
      </div>
    </section>
    <section v-if="investigation.origin" class="engine-panel">
      <h2>原候选的贡献资料</h2>
      <p class="muted">
        随原分析保留，尚未自动归类或纳入研究人员判断。需要使用时，在下面“追加材料”里明确纳入。
      </p>
      <article
        v-for="(support, index) in investigation.origin.candidate.supports"
        :key="index"
        class="investigation-entry"
      >
        <p class="source-id">
          {{ support.record_ref.provider }} / {{ support.record_ref.object_id }}
        </p>
        <blockquote v-if="support.quote">{{ support.quote }}</blockquote>
        <p v-else class="muted">这是结构化记录引用，请阅读固定资料。</p>
        <ElButton
          class="no-print"
          text
          type="primary"
          @click="
            emit(
              'readSource',
              investigation.origin.snapshot_id,
              investigation.origin.snapshot_hash,
              support,
            )
          "
          >阅读原候选依据</ElButton
        >
      </article>
    </section>
    <section class="engine-panel">
      <h2>当前研究人员判断</h2>
      <p v-if="investigation.needs_review" class="notice">
        有材料尚未纳入最新判断，请重新检查。旧判断及其原依据范围保留。
      </p>
      <template v-if="latestJudgment">
        <p class="muted">
          {{ assessmentLabels[latestJudgment.judgment.assessment] }} ·
          {{ latestJudgment.entry.created_by }} ·
          {{ latestJudgment.entry.created_at }}
        </p>
        <p class="investigation-prose">
          {{ latestJudgment.judgment.text }}
        </p>
        <p
          v-if="latestJudgment.judgment.conditions"
          class="investigation-prose"
        >
          <strong>条件、缺口与下一步：</strong
          >{{ latestJudgment.judgment.conditions }}
        </p>
        <p class="muted">
          本版明确纳入
          {{ latestJudgment.judgment.basis_entry_ids.length }}
          项材料；依据固定在当时的调查版本，不随后来资料自动扩大。
        </p>
        <ul class="evidence-list">
          <template
            v-for="entry in basisEntries(
              latestJudgment.judgment.basis_entry_ids,
            )"
            :key="entry.id"
            ><li v-if="entry.payload.kind === 'material'">
              {{ stanceLabels[entry.payload.stance] }} ·
              {{ entry.payload.support.record_ref.provider }} /
              {{ entry.payload.support.record_ref.object_id }} ·
              {{ entry.payload.support.record_ref.adopted_version }}
            </li></template
          >
        </ul>
      </template>
      <p v-else class="muted">
        尚未记录判断。资料不足时可以明确记录“当前无法判断”。
      </p>
    </section>
    <section class="engine-panel">
      <h2>采用材料 · {{ materials.length }} 项</h2>
      <p class="muted">
        支持、反面和背景是研究人员分类；材料数量不等于独立研究数量。
      </p>
      <article
        v-for="entry in materials"
        :key="entry.id"
        class="investigation-entry"
      >
        <template v-if="entry.payload.kind === 'material'">
          <div class="candidate-meta">
            <ElTag
              :type="entry.payload.stance === 'oppose' ? 'warning' : 'info'"
              >{{ stanceLabels[entry.payload.stance] }}</ElTag
            ><span
              >人工分类 · {{ entry.created_by }} · {{ entry.created_at }}</span
            >
          </div>
          <p class="source-id">
            {{ entry.payload.support.record_ref.provider }} /
            {{ entry.payload.support.record_ref.object_id }}
          </p>
          <blockquote v-if="entry.payload.support.quote">
            {{ entry.payload.support.quote }}
          </blockquote>
          <p v-else class="muted">
            本项保存了固定记录引用；未指定引文，不自动推断其支持含义。
          </p>
          <p v-if="entry.payload.text" class="investigation-prose">
            {{ entry.payload.text }}
          </p>
          <p class="muted">
            采用版本：{{ entry.payload.support.record_ref.adopted_version }}
          </p>
          <ElButton
            class="no-print"
            text
            type="primary"
            @click="
              emit(
                'readSource',
                entry.payload.snapshot_id,
                entry.payload.snapshot_hash,
                entry.payload.support,
              )
            "
            >阅读固定资料</ElButton
          >
        </template>
      </article>
      <p v-if="!materials.length" class="muted">
        尚无采用材料；调查问题和笔记不是已取得的证据。
      </p>
    </section>
    <section class="engine-panel">
      <h2>完整记录与判断历史</h2>
      <article
        v-for="entry in investigation.entries"
        :key="entry.id"
        class="investigation-entry"
      >
        <p class="muted">
          第 {{ entry.revision }} 版 · {{ kindLabels[entry.kind] }} ·
          {{ entry.created_by }} · {{ entry.created_at }}
        </p>
        <p class="investigation-prose">{{ entry.payload.text }}</p>
        <template v-if="entry.payload.kind === 'judgment'">
          <p class="muted">
            {{ assessmentLabels[entry.payload.assessment] }} · 依据调查第
            {{ entry.payload.based_on_revision }} 版的
            {{ entry.payload.basis_entry_ids.length }} 项材料
          </p>
          <p v-if="entry.payload.conditions" class="investigation-prose">
            条件、缺口与下一步：{{ entry.payload.conditions }}
          </p>
          <ul class="evidence-list">
            <template
              v-for="basis in basisEntries(entry.payload.basis_entry_ids)"
              :key="basis.id"
              ><li v-if="basis.payload.kind === 'material'">
                {{ stanceLabels[basis.payload.stance] }} ·
                {{ basis.payload.support.record_ref.provider }} /
                {{ basis.payload.support.record_ref.object_id }} ·
                {{ basis.payload.support.record_ref.adopted_version }}
              </li></template
            >
          </ul>
        </template>
        <p v-if="entry.payload.kind === 'material'" class="muted">
          {{ stanceLabels[entry.payload.stance] }} ·
          {{ entry.payload.support.record_ref.object_id }} ·
          {{ entry.payload.support.record_ref.adopted_version }}
        </p>
        <p v-if="entry.payload.kind === 'status'" class="muted">
          {{ statusLabels[entry.payload.status] }}
        </p>
      </article>
      <p class="muted">
        调查编号 {{ investigation.id }} · 第 {{ investigation.revision }} 版
      </p>
    </section>
  </article>
</template>
