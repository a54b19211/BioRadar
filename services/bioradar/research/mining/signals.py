"""Grounded narrative proposals and source-aware attention rules; no truth inference."""

from __future__ import annotations

import re
from collections import defaultdict

from bioradar.research.mining.models import ProviderRef, canonical_hash

SIGNAL_POLICY_VERSION = "anchored-source-signals-v1"
ZH_STOP = frozenset(
    "不是 没有 目前 现在 因为 所以 但是 以及 已经 仍然 可能 可以 需要 表示 认为 指出 "
    "公司 企业 医院 研究 发现 结果 情况 相关 方面 工作 开展 进行 我们 他们 这个 那个 "
    "一种 一个 一些 通过 进一步 据悉 据说 此外 其中 分别 已于 将于 对于 关于 作为 "
    "具有 也是 还是 不是说 并不是 暂时 没有发现 未发现 无明显 不存在 不能 尚未".split()
)
EVENT = re.compile(
    r"不明原因肺炎|聚集性?(?:病例|感染)|病例聚集|疫情|暴发|医护(?:人员)?感染|人传人|不明病原|肺炎聚集|"
    r"随机(?:对照)?(?:三|III|3)期|(?:三|III|3)期(?:临床)?试验|随机对照试验|预设(?:主要)?终点|"
    r"肝毒性|严重不良事件|安全(?:警告|事件|隐患|风险)|产品召回|召回|订单更正|净订单|毛订单|订单|"
    r"有效工时|实际工时|可用工时|工时利用率|产能利用率|利用率|确认收入|营业收入|现金流|预收款|撤回|更正|"
    r"pneumonia cluster|cluster of cases|outbreak|healthcare worker infection|"
    r"phase\s*(?:3|iii)|randomi[sz]ed trial|"
    r"primary endpoint|recall|order correction|capacity utili[sz]ation|cash flow",
    re.I,
)
QUANTITY = re.compile(
    r"(?:\d+(?:[.,]\d+)*|[一二三四五六七八九十百千万]+)\s*(?:例|人|名|套|台|万?元|亿元|%|％|小时|cases?\b|patients?\b|USD\b)",
    re.I,
)
ENTITY = re.compile(
    r"(?<![A-Za-z0-9_])(?:NCT\d{8}|[A-Z]{1,8}-\d{2,8}|[A-Z][A-Za-z0-9]*[0-9][A-Za-z0-9]*)"
    r"(?![A-Za-z0-9_])|"
    r"[\u3400-\u9fff]{2,16}(?:医院|公司|集团|大学|研究所|卫健委)"
)
CLAUSE = re.compile(r"[^。！？!?\n]+")
NOT_FOUND = re.compile(r"未发现|没有发现|未确认|未证实|无明显|not\s+(?:found|confirmed)|no evidence", re.I)
EXCLUDED = re.compile(r"已排除|确认不存在|证实不存在|has been ruled out|confirmed absent", re.I)
REACTION = re.compile(r"辟谣|谣言|不实|处理造谣者|造谣者|删帖|屏蔽|否认|谣言处理|censor|debunk", re.I)
UNVERIFIED_CLAIM = re.compile(r"网传|据称|据说|传言|传闻|疑似|尚待核实|未经证实|rumou?r|reportedly", re.I)
HEALTH_OR_SAFETY = re.compile(
    r"不明原因肺炎|聚集|病例|疫情|暴发|医护(?:人员)?感染|人传人|不明病原|"
    r"肝毒性|严重不良事件|安全|召回|pneumonia|cluster of cases|outbreak|healthcare worker infection|recall",
    re.I,
)


def narrative_anchors(text: str, entity_names=()) -> list[dict]:
    anchors = []
    for kind, pattern in (("event", EVENT), ("quantity", QUANTITY), ("entity", ENTITY)):
        for match in pattern.finditer(text):
            value = match.group().strip()
            if value in ZH_STOP:
                continue
            if kind == "entity" and re.fullmatch(r"[\u3400-\u9fff]+", value):
                stem = re.sub(r"医院|公司|集团|大学|研究所|卫健委", "", value)
                if stem in ZH_STOP | {"某某", "上市", "这家", "当地", "本地", "相关"}:
                    continue
            anchors.append({"kind": kind, "text": match.group(), "start": match.start(), "end": match.end()})
    for name in entity_names:
        if not isinstance(name, str) or len(name.strip()) < 2 or name.strip() in ZH_STOP:
            continue
        for m in re.finditer(re.escape(name), text):
            anchors.append({"kind": "entity", "text": name, "start": m.start(), "end": m.end()})
    return sorted(anchors, key=lambda a: (a["start"], a["end"], a["kind"]))


def checkable_claims(text: str, entity_names=()) -> list[dict]:
    """Keep exact attributed clauses, including rumors, instead of asserting their truth."""
    anchors = narrative_anchors(text, entity_names)
    claims = []
    for clause in CLAUSE.finditer(text):
        selected = [a for a in anchors if clause.start() <= a["start"] < clause.end()]
        kinds = {a["kind"] for a in selected}
        explicit_combination = len({a["text"] for a in selected if a["kind"] == "entity"}) >= 2 and re.search(
            r"联合|组合|搭配|协同|结合|combin(?:ation|ed)|co-treatment", clause.group(), re.I
        )
        if not explicit_combination and (
            "event" not in kinds or not kinds.intersection({"quantity", "entity"})
        ):
            continue
        claims.append(
            {
                "text": clause.group().strip(),
                "start": clause.start() + len(clause.group()) - len(clause.group().lstrip()),
                "end": clause.start() + len(clause.group().rstrip()),
                "anchors": selected,
            }
        )
    return claims


def affirmed_event_anchors(claim):
    """Local source phrasing, not a factual truth decision about the event."""
    text = claim["text"]
    events = []
    for anchor in claim["anchors"]:
        if anchor["kind"] != "event":
            continue
        offset = anchor["start"] - claim["start"]
        prefix = re.split(r"[，,；;]", text[:offset])[-1]
        # No detection and a public reaction do not establish absence. An explicit
        # exclusion remains visible in the attributed claim, but is not an event affirmation.
        if not EXCLUDED.search(prefix):
            events.append(anchor)
    return events


def anchored_phrases(text: str, stoplist=()) -> dict[str, str]:
    """Chinese features are whole recorded anchors, never arbitrary character n-grams."""
    stops = ZH_STOP | frozenset(stoplist)
    return {
        a["text"].casefold(): a["text"]
        for a in narrative_anchors(text)
        if a["kind"] in {"event", "entity"} and a["text"].casefold() not in stops
    }


def source_groups(records) -> dict:
    """Explicit repost links merge exposure groups; unknown independence stays unknown."""
    records = list(records)
    parent = {r.record_ref.identity_key: r.record_ref.identity_key for r in records}

    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            key = parent[key]
        return key

    identifiers = {}
    for record in records:
        context = record.signal_context
        metadata = context.document_evidence if context and context.document_evidence else {}
        independence = metadata.get("source_independence") or {}
        for identifier in independence.get("shared_identifiers", []):
            if not isinstance(identifier, str) or not identifier.strip():
                continue
            token = identifier.strip().casefold()
            key = root(record.record_ref.identity_key)
            if token in identifiers:
                other = root(identifiers[token])
                parent[max(key, other)] = min(key, other)
            else:
                identifiers[token] = key
        for relation in context.source_relations if context else ():
            if relation.kind == "repost":
                a, b = root(record.record_ref.identity_key), root(relation.target_ref.identity_key)
                parent[max(a, b)] = min(a, b)
    groups = defaultdict(list)
    for record in records:
        context = record.signal_context
        origin = (
            ("family", record.source_family)
            if record.source_family and record.source_independence_status == "confirmed"
            else ("origin", context.origin_group)
            if context and context.origin_group
            else ("linked_record", root(record.record_ref.identity_key))
        )
        groups[canonical_hash(origin)].append(record)
    # A repost carrying another source family does not become an independent confirmation.
    merged = {}
    for key, members in groups.items():
        link_roots = [root(r.record_ref.identity_key) for r in members]
        connected = [k for k, row in merged.items() if set(row["roots"]) & set(link_roots)]
        if connected:
            row = merged[connected[0]]
            row["records"].extend(members)
            row["roots"].extend(link_roots)
            for other in connected[1:]:
                row["records"].extend(merged[other]["records"])
                row["roots"].extend(merged.pop(other)["roots"])
        else:
            merged[key] = {"records": members, "roots": link_roots}
    confirmed, unknown = [], []
    for key, row in sorted(merged.items()):
        refs = [r.record_ref.model_dump(mode="json") for r in row["records"]]
        originals = [
            r
            for r in row["records"]
            if not r.signal_context
            or not any(relation.kind == "repost" for relation in r.signal_context.source_relations)
        ]
        origin = sorted(originals or row["records"], key=lambda r: r.record_ref.identity_key)[0]
        stable_origin = (
            origin.signal_context.origin_group
            if origin.signal_context and origin.signal_context.origin_group
            else origin.source_family
            if origin.source_family and origin.source_independence_status == "confirmed"
            else root(origin.record_ref.identity_key)
        )
        value = {"group_key": canonical_hash(stable_origin), "record_refs": refs}
        if any(r.source_family and r.source_independence_status == "confirmed" for r in row["records"]):
            confirmed.append(value)
        else:
            unknown.append(value)
    return {"confirmed": confirmed, "unknown": unknown}


def assess_records(records, *, qualified_study_refs=None, policy=None) -> dict:
    records = list(records)
    groups = source_groups(records)
    claims, anchors, relations, institutions, environment, negative = [], [], [], [], [], []
    negative_signals, confirmed_institution_refs, official_confirmation_refs = [], set(), []
    studies = set()
    study_unknown = False
    numeric = []
    for record in records:
        context = record.signal_context
        text = record.normalized_text or ""
        names = context.entity_names if context else ()
        ref = record.record_ref.model_dump(mode="json")
        claim_rows = checkable_claims(text, names)
        claims.extend({**c, "record_ref": ref, "status": "unverified_source_claim"} for c in claim_rows)
        for kind, pattern, qualification in (
            ("not_found", NOT_FOUND, "热度可降、风险未排除"),
            ("excluded", EXCLUDED, "有出处的明确排除；结束跟踪仍须核对其范围"),
            ("reaction", REACTION, "反应信号，不证明事件为真或为假"),
        ):
            negative_signals.extend(
                {
                    "kind": kind,
                    "record_ref": ref,
                    "start": match.start(),
                    "end": match.end(),
                    "qualification": qualification,
                }
                for match in pattern.finditer(text)
            )
        anchors.extend({**a, "record_ref": ref} for a in narrative_anchors(text, names))
        study_qualified = (
            qualified_study_refs.get(record.record_ref.version_key, False)
            if qualified_study_refs is not None
            else record.study_independence_status == "confirmed"
        )
        if study_qualified and record.study_cluster_ref:
            studies.add(record.study_cluster_ref.identity_key)
        elif record.study_independence_status != "not_applicable":
            study_unknown = True
        if context:
            relations.extend(
                {**r.model_dump(mode="json"), "record_ref": ref} for r in context.source_relations
            )
            official = (
                context.material_type == "official_notice"
                or (context.document_evidence or {}).get("material_kind") == "official_notice"
            )
            confirming = any(
                affirmed_event_anchors(claim)
                and not (
                    NOT_FOUND.search(claim["text"])
                    or EXCLUDED.search(claim["text"])
                    or REACTION.search(claim["text"])
                    or UNVERIFIED_CLAIM.search(claim["text"])
                )
                for claim in claim_rows
            )
            if confirming and context.evidence_refs and context.statement_status != "unverified_report":
                if official or (
                    context.verification_status == "source_confirmed"
                    and (policy is None or policy.time_mode != "availability_replay")
                ):
                    institutions.append(ref)
                    if official:
                        official_confirmation_refs.append(ref)
                if (
                    context.attribution == "named_institution"
                    and context.verification_status == "source_confirmed"
                    and (policy is None or policy.time_mode != "availability_replay")
                ):
                    confirmed_institution_refs.add(record.record_ref.version_key)
        metadata = context.document_evidence if context and context.document_evidence else {}
        information_events = metadata.get("information_events") or []
        information_events = (
            [event for event in information_events if isinstance(event, dict)]
            if isinstance(information_events, (list, tuple))
            else []
        )
        environment.extend(
            {"kind": kind, "record_ref": ref, "qualification": "context_not_proof_of_claim_or_risk"}
            for kind, found in (
                (
                    "deletion",
                    "deleted" in record.record_flags
                    or any(e.get("event_type") == "reported_deletion" for e in information_events),
                ),
                (
                    "denial_or_censorship",
                    bool(REACTION.search(text))
                    or any(
                        e.get("event_type") in {"official_denial", "moderation"} for e in information_events
                    ),
                ),
            )
            if found
        )
        from bioradar.research.mining.prepare import section_visible

        negative.extend(
            ref
            for a in record.assessments
            if a.review_status == "reviewed"
            and a.stance in {"negative", "mixed"}
            and (policy is None or section_visible(a.provenance, record, policy))
        )
        numeric.extend(
            o.observation_id
            for o in record.metric_observations
            if o.binding_status == "source_bound"
            and (policy is None or section_visible(o.provenance, record, policy))
        )
    corrections = [r for r in relations if r["kind"] in {"correction", "retraction"}]
    event_refs = [
        r.record_ref.model_dump(mode="json")
        for r in records
        if r.events and any(e.object_refs for e in r.events)
    ]
    checkable = bool(claims or numeric or event_refs)
    importance, urgency = ("low", "routine") if checkable else ("undetermined", "undetermined")
    reasons = (
        ["specific_attributed_claim_for_verification"]
        if checkable
        else ["no_checkable_entity_quantity_event_claim"]
    )
    evidence_level = "unverified" if claims or event_refs else "unknown"
    if groups["confirmed"] and len(groups["confirmed"]) >= 2:
        evidence_level = "independent_sources"
    if institutions:
        evidence_level = "institution_confirmed"
    if numeric:
        reasons.append("source_bound_metric_not_population_or_causal_proof")
    if corrections or negative:
        importance, urgency = "medium", "soon"
        reasons.append("source_linked_correction_or_reviewed_counterevidence")
    positive_events = [c for c in claims if affirmed_event_anchors(c)]
    serious = any(
        HEALTH_OR_SAFETY.search(anchor["text"])
        for claim in positive_events
        for anchor in affirmed_event_anchors(claim)
    )
    if serious:
        importance, urgency = "medium", "soon"
        reasons.append("specific_potentially_serious_claim_requires_verification_truth_unresolved")
    if any(
        re.search(r"\d", claim["text"]) and re.search(r"更正|修正|correction|corrected", claim["text"], re.I)
        for claim in claims
    ):
        importance, urgency = "medium", "soon"
        reasons.append("attributed_numeric_correction_requires_verification")
    institutional_groups = sum(
        any(
            ProviderRef.model_validate(ref).version_key in confirmed_institution_refs
            for ref in group["record_refs"]
        )
        for group in groups["confirmed"]
    )
    if positive_events and (official_confirmation_refs or institutional_groups >= 2):
        importance, urgency = "high", "immediate"
        reasons.append("specific_event_confirmed_by_official_or_two_independent_institutions")
    return {
        "version": SIGNAL_POLICY_VERSION,
        "automatic_investigation_allowed": checkable,
        "potentially_serious": serious,
        "importance": importance,
        "urgency": urgency,
        "attention_reasons": reasons,
        "evidence_level": evidence_level,
        "confirmed_source_group_count": len(groups["confirmed"]),
        "unknown_source_group_count": len(groups["unknown"]),
        "source_groups": groups,
        "confirmed_independent_study_count": len(studies),
        "independent_study_count": None if study_unknown else len(studies),
        "checkable_claims": claims,
        "anchors": anchors,
        "corrections": corrections,
        "reviewed_counterevidence_refs": negative,
        "institution_confirmation_refs": institutions,
        "source_bound_observation_ids": numeric,
        "environment_signals": environment,
        "negative_signals": negative_signals,
        "risk_not_excluded": any(row["kind"] in {"not_found", "reaction"} for row in negative_signals),
        "qualification": "attention_and_attributed_claims_not_scientific_approval_or_rumor_verification",
    }


def with_attention(candidate, records, *, qualified_study_refs=None, policy=None):
    """Annotate every saved candidate; eligibility for automation is a separate decision."""
    records = list(records)
    assessment = assess_records(records, qualified_study_refs=qualified_study_refs, policy=policy)
    state_method = candidate.method_id in {
        "state_transitions",
        "pipeline_landscape",
        "conditional_pipeline_landscape",
        "pipeline_graph_hints",
        "maturity_shift",
        "counterevidence_impact",
        "translation_dwell",
        "event_exposure",
    }
    typed_state = state_method and any(
        r.state_assertions or r.graph_hints or (r.trial_refs and (r.project_status or r.project_stage))
        for r in records
    )
    native_notice = candidate.metrics.get("native_notice_event") is True
    typed_combination = (
        candidate.method_id == "entity_combinations"
        and bool(candidate.metrics.get("technology_forms"))
        and bool(candidate.metrics.get("disease_keys"))
    )
    computed_metric = candidate.metrics.get("comparable") is True and bool(
        candidate.metrics.get("period_unit_ids")
    )
    allowed = (
        assessment["automatic_investigation_allowed"]
        or typed_state
        or computed_metric
        or native_notice
        or typed_combination
    )
    if (typed_state or computed_metric or native_notice or typed_combination) and not assessment[
        "automatic_investigation_allowed"
    ]:
        assessment["attention_reasons"].append("fixed_typed_state_or_graph_claim_for_verification")
        assessment["importance"], assessment["urgency"] = "low", "routine"
    importance_order = {"undetermined": 0, "low": 1, "medium": 2, "high": 3}
    urgency_order = {"undetermined": 0, "routine": 1, "soon": 2, "immediate": 3}
    importance = max((candidate.importance, assessment["importance"]), key=importance_order.get)
    urgency = max((candidate.urgency, assessment["urgency"]), key=urgency_order.get)
    attention = {
        key: assessment[key]
        for key in (
            "version",
            "attention_reasons",
            "evidence_level",
            "confirmed_source_group_count",
            "unknown_source_group_count",
            "confirmed_independent_study_count",
            "independent_study_count",
            "environment_signals",
            "negative_signals",
            "risk_not_excluded",
            "qualification",
        )
    }
    attention.update(
        automatic_investigation_allowed=allowed,
        truth_confidence={
            "status": "unverified",
            "qualification": "source_attribution_is_not_truth_verification",
        },
        potential_consequence={
            "level": "potentially_serious" if assessment["potentially_serious"] else "undetermined",
            "qualification": "conditional_on_claim_not_a_disease_risk_estimate",
        },
        verification_priority={"importance": importance, "urgency": urgency},
        basis_refs=[r.record_ref.model_dump(mode="json") for r in records],
        checkable_claims=[
            {
                "text": c["text"],
                "start": c["start"],
                "end": c["end"],
                "record_ref": c["record_ref"],
                "status": c["status"],
            }
            for c in assessment["checkable_claims"][:16]
        ],
        omitted_claim_count=max(0, len(assessment["checkable_claims"]) - 16),
        corrections=assessment["corrections"],
        reviewed_counterevidence_refs=assessment["reviewed_counterevidence_refs"],
    )
    title = candidate.title
    if (
        candidate.method_id in {"lexical_emergence", "text_topics", "research_tfidf_topics"}
        and assessment["checkable_claims"]
    ):
        title = "待核事件：" + assessment["checkable_claims"][0]["text"][:170]
    return candidate.model_copy(
        update={
            "title": title,
            "importance": importance,
            "urgency": urgency,
            "metrics": {
                **candidate.metrics,
                "independent_study_count": assessment["independent_study_count"],
                "confirmed_independent_study_count": assessment["confirmed_independent_study_count"],
                "attention": attention,
            },
        }
    )
