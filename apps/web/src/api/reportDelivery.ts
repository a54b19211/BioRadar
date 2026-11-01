import type { ResearchAnalysisView } from "./research";

export interface ReportSource {
  citation: string;
  citationId: string;
  evidenceKey?: string;
  sourceName?: string;
  publicationTime?: string | null;
  title?: string;
  version?: string;
  role?: string;
}
const sourceNames: Record<string, string> = {
  clinicaltrials: "ClinicalTrials 临床登记", europe_pmc: "Europe PMC 文献",
  who_don: "WHO 通报", sec_edgar: "SEC 公司披露", fda_crl: "FDA 审评信函",
  legacy_documents: "Fabric 已采用资料", external_archive: "已导入原始档案",
};
export function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function sameRef(left: unknown, right: unknown) {
  const a = object(left), b = object(right);
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => JSON.stringify(a[key]) === JSON.stringify(b[key]));
}
export function reportSources(analysis: ResearchAnalysisView): ReportSource[] {
  const body = analysis.result?.report?.final_text || "";
  const evidence = analysis.evidence ?? [];
  const mapping = object(analysis.result?.report?.evidence_map);
  const rows: ReportSource[] = [], seen = new Set<string>();
  for (const match of body.matchAll(/\[(record:)?([A-Za-z][A-Za-z0-9_-]{0,127})\]/g)) {
    const citation = match[0], citationId = match[2]!;
    if (!match[1] && !citationId.startsWith("q") && !evidence.some(item => item.key === citationId) && !(citationId in mapping)) continue;
    if (seen.has(citation)) continue;
    seen.add(citation);
    const binding = object(mapping[citationId]);
    const key = typeof binding.key === "string" ? binding.key : citationId;
    let item = evidence.find(entry => entry.key === key);
    if (!item && binding.record_ref) {
      const exact = evidence.filter(entry => sameRef(entry.record_ref, binding.record_ref));
      if (exact.length === 1) item = exact[0];
    }
    if (item && binding.record_ref && !sameRef(item.record_ref, binding.record_ref)) item = undefined;
    if (item && "version" in binding && binding.version !== item.record_ref.adopted_version) item = undefined;
    const row: ReportSource = { citation, citationId };
    if (item && item.record_ref.adopted_version) {
      Object.assign(row, {
        evidenceKey: item.key, sourceName: sourceNames[item.source_id] || item.source_id || "来源未记录",
        publicationTime: item.publication_time, title: item.title || "标题未记录",
        version: item.record_ref.adopted_version, role: item.evidence_role,
      });
    }
    rows.push(row);
  }
  return rows;
}
export function reportReadingParts(body: string, sources: ReportSource[]): { text: string; source?: ReportSource }[] {
  const parts: { text: string; source?: ReportSource }[] = [];
  const index = new Map(sources.map(source => [source.citation, source]));
  let start = 0;
  for (const match of body.matchAll(/\[(?:record:)?[A-Za-z][A-Za-z0-9_-]{0,127}\]/g)) {
    const source = index.get(match[0]);
    if (!source) continue;
    const position = match.index!;
    if (position > start) parts.push({ text: body.slice(start, position) });
    parts.push({ text: match[0], source });
    start = position + match[0].length;
  }
  if (start < body.length) parts.push({ text: body.slice(start) });
  return parts;
}
export function sourceLabel(source: ReportSource) {
  return [source.sourceName, source.publicationTime || "公开时间未记录", source.title].filter(Boolean).join(" · ");
}
