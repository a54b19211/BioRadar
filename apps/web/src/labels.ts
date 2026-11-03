// Chinese labels for the codes the backend sends, and tag colours. Unknown codes are shown as they are.
type TagType = "primary" | "success" | "warning" | "danger" | "info";

const maps = {
  direction: {
    pipeline: "管线",
    science: "科研",
    business: "商业",
    public_health: "公卫",
  },
  method: { planner: "逐步规划", baseline: "固定流程" },
  state: {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
  },
  stage: {
    plan: "制定计划",
    investigate: "取证调查",
    compute: "计算",
    mechanism: "机制评价",
    report: "撰写报告",
    assess: "综合判断",
    done: "完成",
  },
  topicState: {
    emerging: "萌发",
    rising: "升温",
    cooling: "降温",
    dormant: "休眠",
  },
  alert: { none: "无", normal: "一般", important: "重要", urgent: "紧急" },
  confidence: {
    insufficient: "证据不足",
    possible: "可能",
    probable: "很可能",
    confirmed: "已证实",
  },
  level: { low: "低", medium: "中", high: "高", urgent: "紧急" },
  judgmentCheck: { supported: "核验通过", contradicted: "与原文矛盾", unverified: "未核验通过" },
  checkStatus: { open: "未查清", answered: "已回答", conflict: "有冲突" },
  mechanismStatus: { not_run: "未运行", failed: "失败", completed: "已完成" },
  action: {
    search: "检索",
    fetch: "联网抓取",
    next_page: "翻页",
    read: "阅读",
  },
  stance: {
    supports: "支持",
    refutes: "反驳",
    limits: "限制条件",
    context: "背景",
  },
  eventKind: {
    created: "创建专题",
    observed: "观察到新材料",
    state: "状态变化",
    analysis: "自动分析",
  },
  trigger: { manual: "手动提问", followup: "追问", topic: "专题触发" },
  reason: {
    records_or_alert: "材料累计或警示升级",
    no_new_records: "长时间没有新材料",
    closure_notice: "收到结案通知",
    new_related_record: "出现相关新材料",
    rising: "开始升温",
    awakened: "被唤醒",
    alert_important: "警示升为重要",
    alert_urgent: "警示升为紧急",
    triage_failed: "分诊失败",
  },
};

export type LabelKind = keyof typeof maps;

export function label(kind: LabelKind, key: string | null | undefined): string {
  if (!key) return "-";
  return (maps[kind] as Record<string, string>)[key] ?? key;
}

const colors: Record<string, TagType> = {
  // analysis state, report status, check status
  queued: "info",
  running: "primary",
  completed: "success",
  failed: "danger",
  PASS: "success",
  REVIEW: "warning",
  FAIL: "danger",
  NOT_RUN: "info",
  answered: "success",
  conflict: "danger",
  supported: "success",
  contradicted: "danger",
  unverified: "warning",
  // alert, priority, confidence, topic state
  none: "info",
  normal: "info",
  low: "info",
  medium: "warning",
  high: "danger",
  important: "warning",
  urgent: "danger",
  confirmed: "success",
  probable: "primary",
  possible: "warning",
  rising: "danger",
  emerging: "warning",
  cooling: "primary",
  dormant: "info",
};

export const tagType = (key: string | null | undefined): TagType => colors[key ?? ""] ?? "info";

export function fmt(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}

/** Strings as they are, anything else as indented JSON (for fields whose shape the backend does not fix). */
export const pretty = (v: unknown): string => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
