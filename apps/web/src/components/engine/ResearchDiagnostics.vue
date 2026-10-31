<script setup lang="ts">
import type { ResearchAnalysisDiagnostics } from "../../api/research";

defineProps<{ diagnostics: ResearchAnalysisDiagnostics }>();
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function rows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(object) : [];
}
function field(row: Record<string, unknown>, name: string) {
  const value = row[name];
  return value == null ? "未记录" : Array.isArray(value) ? value.join("、") : String(value);
}
function amount(value: unknown) {
  return typeof value === "number" ? value.toLocaleString() : "未知";
}
function reference(value: unknown) {
  const row = object(value);
  return [row.source_id || row.provider, row.object_id || row.source_record_id,
    row.adopted_version || row.version_id].filter(Boolean).join(" / ") || "固定身份未记录";
}
function issues(row: Record<string, unknown>) {
  return rows(row.issues).map(item => field(item, "stage") + "：" + field(item, "reason")).join("；") || "规则码未记录";
}
</script>

<template>
  <section class="research-diagnostics" aria-label="无原文执行诊断">
    <h3>执行诊断与本稿用量</h3>
    <p>系统诊断不是原文或原核验稿。保留错误码、规则和记录编号，不含材料正文、提示或引句。</p>
    <p v-if="diagnostics.error_code">本版停止代码：<code>{{ diagnostics.error_code }}</code>。</p>
    <p><strong>本稿全流程实耗 token：{{ amount(diagnostics.usage.total_tokens) }}</strong>；
      已知 {{ amount(diagnostics.usage.known_total_tokens) }}，
      {{ diagnostics.usage.unknown_usage_calls }} 次实际调用缺少用量，未知不补零。
    </p>
    <p v-if="diagnostics.usage.verified_pass_report">文字核验PASS稿总 token：{{ amount(diagnostics.usage.verified_pass_total_tokens) }}。只计逐字绑定的文字核验PASS，不表示科学结论成立。</p>
    <p v-else>本稿尚不计入逐字绑定的文字核验PASS稿费用，原报告状态保持不变。</p>
    <div class="diagnostic-table">
      <table>
        <thead><tr><th>实际阶段</th><th>记录数</th><th>实际 token</th><th>已知 token</th><th>编码请求 UTF-8 字节</th></tr></thead>
        <tbody><tr v-for="(stage, index) in diagnostics.usage.stages" :key="index">
          <td>{{ field(stage, 'stage') }}</td><td>{{ field(stage, 'call_count') }}</td>
          <td>{{ amount(stage.total_tokens) }}</td><td>{{ amount(stage.known_total_tokens) }}</td>
          <td>{{ amount(stage.request_bytes) }}</td>
        </tr></tbody>
      </table>
    </div>
    <p class="muted">字节表示实际保存的编码输入大小，不能当作 token；旧记录未保存的字节数显示未知。模型调用、修复及失败阶段均保留。</p>
    <details class="no-print">
      <summary>逐调用原用量回执（{{ diagnostics.usage.calls.length }} 条）</summary>
      <ul><li v-for="(call, index) in diagnostics.usage.calls" :key="index">
        {{ field(call, 'stage') }} · {{ field(call, 'call_key') }} · {{ field(call, 'receipt_status') }}；
        prompt {{ amount(object(call.usage).prompt_tokens) }} /
        completion {{ amount(object(call.usage).completion_tokens) }} /
        total {{ amount(object(call.usage).total_tokens) }} token；
        {{ amount(call.request_bytes) }} 字节。
      </li></ul>
    </details>
    <details class="no-print">
      <summary>检索词、页码与续页回执（{{ diagnostics.queries.length }} 条）</summary>
      <ul><li v-for="(query, index) in diagnostics.queries" :key="index">
        查询记录 {{ index + 1 }} · {{ field(query, 'source_id') }} · {{ field(query, 'kind') }} · {{ field(query, 'status') }}；
        检索词：{{ field(query, 'terms') }}；页码 {{ field(object(query.pagination), 'page_number') }}；
        还有下一页：{{ field(object(query.pagination), 'has_more') }}；
        前序查询索引 {{ field(query, 'previous_query_index') }}。
        <p>动作 {{ field(query, 'action_id') }}；游标摘要 {{ field(query, 'cursor_sha256') }}；
          下一页游标摘要 {{ field(object(query.pagination), 'next_cursor_sha256') }}。</p>
      </li></ul>
    </details>
    <details class="no-print">
      <summary>读取修复触发记录（{{ diagnostics.reading_repairs.length }} 条）</summary>
      <ul><li v-for="(repair, index) in diagnostics.reading_repairs" :key="index">
        {{ field(repair, 'original_call_key') }} → {{ field(repair, 'repair_call_key') }}；
        触发码 {{ field(repair, 'reason') }}；{{ field(repair, 'status') }}。
        <p>字段位置：{{ JSON.stringify(repair.field_paths ?? []) }}。</p>
        <p>原请求 {{ amount(repair.original_request_bytes) }} /
          修复 {{ amount(repair.repair_request_bytes) }} 字节；修复错误 {{ field(repair, 'repair_error_code') }}。</p>
        <pre>{{ JSON.stringify(repair.allowed_reference_ids ?? {}, null, 2) }}</pre>
      </li></ul>
    </details>
    <details class="no-print">
      <summary>BioCheck 问题标签与原位置（{{ diagnostics.biocheck_issues.length }} 条）</summary>
      <ul><li v-for="(issue, index) in diagnostics.biocheck_issues" :key="index">
        {{ field(issue, 'source') }} · 单元 {{ field(issue, 'unit_id') }} · 原句序 {{ field(issue, 'sentence_index') }}
        · 陈述索引 {{ field(issue, 'claim_index') }}；
        标签 {{ field(issue, 'label') }}；规则码 {{ field(issue, 'code') }}。
        <small v-if="issue.description_present">自由问题说明仅保留摘要 {{ field(issue, 'description_sha256') }}，未导出引句。</small>
      </li></ul>
      <p class="muted">沿用原单元编号、句序和从0开始的陈述索引；未提供的句序不推测。</p>
    </details>
    <details class="no-print">
      <summary>数值候选逐条拒因（{{ diagnostics.numeric_candidates.length }} 条）</summary>
      <ul><li v-for="(candidate, index) in diagnostics.numeric_candidates" :key="index">
        {{ reference(candidate.record_ref) }} · {{ field(candidate, 'origin') }} · 原候选索引 {{ field(candidate, 'observation_index') }}：
        {{ issues(candidate) }}。
        <small>来源产物 {{ field(candidate, 'artifact_id') }}；提取记录 {{ field(candidate, 'extraction_id') }}。</small>
      </li></ul>
      <p class="muted">程序提议与模型提议的序号分开，不能因同一序号就视为同一候选。单位、期间、主体、并列关系和指标角色分别记录。</p>
    </details>
  </section>
</template>

<style scoped>
.research-diagnostics { margin-top: 16px; border-top: 1px solid var(--el-border-color); }
.diagnostic-table { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px; border-bottom: 1px solid var(--el-border-color); text-align: left; }
details { margin: 10px 0; }
li { margin: 8px 0; }
small { display: block; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
