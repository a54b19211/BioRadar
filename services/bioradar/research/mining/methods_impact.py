"""Negative-cue recall and exact dependency/new-counterevidence matching."""

from __future__ import annotations

import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from bioradar.research.mining.common import candidate, result, unsupported
from bioradar.research.mining.methods_pipeline import context_key
from bioradar.research.mining.methods_text import SEGMENT, _analyzer, is_structured_json
from bioradar.research.mining.prepare import (
    PreparedSnapshot,
    ref_key,
    section_visible,
    time_interval,
    version_key,
)

NEGATIVE = re.compile(
    r"\b(?:retract(?:ed|ion)|terminated|failed|failure|negative|withdrawn|recall|not\s+significant|"
    r"no\s+improvement|did\s+not\s+improve|safety\s+signal)\b|撤稿|终止|失败|未见改善|不显著|召回|反证",
    re.IGNORECASE,
)


def counterevidence_impact(prepared: PreparedSnapshot):
    method = "counterevidence_impact"
    dependencies = {}
    for item in prepared.records:
        for dep in item.record.dependencies:
            if section_visible(dep.provenance, item.record, prepared.policy):
                dependencies.setdefault((dep.judgment_id, dep.claim_id), (item, dep))
    candidates, cues = [], []
    retraction_items = []
    text_count = 0
    remaining_chars = prepared.policy.limits.max_text_characters
    truncated = 0
    for item in prepared.records:
        record = item.record
        visible_events = [
            event
            for event in record.events
            if event.source_status == "supported"
            and event.event_type in {"retraction", "source_correction"}
            and section_visible(event.provenance, record, prepared.policy)
            and time_interval(event.effective_time)
            and time_interval(event.effective_time)[1] <= prepared.policy.as_of
        ]
        if {"retracted", "revised", "deleted"} & set(record.record_flags) or visible_events:
            retraction_items.append(item)
        if record.text_status != "inline_authorized" or not record.normalized_text:
            continue
        if is_structured_json(record.normalized_text):
            continue
        text_count += 1
        if remaining_chars <= 0:
            truncated += 1
            continue
        text = record.normalized_text[:remaining_chars]
        truncated += len(text) != len(record.normalized_text)
        remaining_chars -= len(text)
        for segment in SEGMENT.finditer(text):
            match = NEGATIVE.search(segment.group())
            if not match:
                continue
            start = max(0, match.start() - 100)
            quote = segment.group()[start : match.end() + 100]
            cues.append((item, quote))
            candidates.append(
                candidate(
                    method,
                    {"negative_cue": record.record_ref.identity_key, "quote": quote},
                    "负面词句取证线索",
                    [item],
                    kind="hypothesis",
                    score=1,
                    reasons=("negative_cue_requires_conditions_and_negation_review",),
                    metrics={"cue": match.group(), "clinical_failure_confirmed": False},
                    quotes={record.document_hash: quote},
                    questions=("否定与限定词的范围是什么，是否涉及同一研究对象和条件？",),
                    requirements=("S03原文与反证检索", "S04/S05适用条件和主张核验"),
                )
            )
            break
    direct_matches = 0
    for item in retraction_items:
        record = item.record
        # Do not silently broaden a version-specific source action to all versions.
        targets = {version_key(record.record_ref)}
        if "revised" in record.record_flags:
            targets.update(version_key(ref) for ref in record.lineage_refs)
        targets.update(
            version_key(ref)
            for event in record.events
            if event.source_status == "supported"
            and event.event_type in {"retraction", "source_correction"}
            and section_visible(event.provenance, record, prepared.policy)
            and time_interval(event.effective_time)
            and time_interval(event.effective_time)[1] <= prepared.policy.as_of
            for ref in event.object_refs
        )
        for _, (dep_item, dep) in sorted(dependencies.items()):
            hits = [ref for ref in dep.evidence_refs if version_key(ref) in targets]
            if not hits:
                continue
            direct_matches += 1
            candidates.append(
                candidate(
                    method,
                    {"dependency": (dep.judgment_id, dep.claim_id), "event": record.record_ref.version_key},
                    "原证据版本变化影响旧主张",
                    [item, dep_item],
                    kind="data_quality",
                    score=3,
                    reasons=(
                        "exact_version_dependency_match",
                        "source_change_is_not_automatic_judgment_reversal",
                    ),
                    metrics={
                        "judgment_id": dep.judgment_id,
                        "claim_id": dep.claim_id,
                        "matched_dependency_refs": [ref.model_dump(mode="json") for ref in hits],
                        "record_flags": list(record.record_flags),
                    },
                    questions=("变化是否影响旧主张的关键条件或计算，应该维持还是修订？",),
                    requirements=("B07影响裁决", "固定版本支持与反证原文"),
                )
            )
    new_matches = 0
    if dependencies and cues:
        ordered_deps = [value for _, value in sorted(dependencies.items())]
        documents = [dep.claim_text for _, dep in ordered_deps] + [quote for _, quote in cues]
        try:
            vectorizer = TfidfVectorizer(analyzer=_analyzer, max_features=prepared.policy.limits.max_features)
            matrix = vectorizer.fit_transform(documents)
        except ValueError:
            matrix = None
        if matrix is not None:
            for index, (item, quote) in enumerate(cues):
                scores = cosine_similarity(matrix[len(ordered_deps) + index], matrix[: len(ordered_deps)])[0]
                for dep_index in sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:3]:
                    if scores[dep_index] < 0.2:
                        continue
                    dep_item, dep = ordered_deps[dep_index]
                    if any(
                        ref.identity_key == item.record.record_ref.identity_key for ref in dep.evidence_refs
                    ):
                        continue  # This is the existing-dependency route, not unseen counterevidence.
                    known_entities = {ref_key(ref) for ref in dep.entity_refs}
                    observed_entities = {
                        ref_key(e.canonical_entity_ref)
                        for e in item.record.canonical_entity_refs
                        if e.canonical_entity_ref and e.mapping_status == "confirmed"
                    }
                    if known_entities and observed_entities and not known_entities & observed_entities:
                        continue
                    old_context = context_key(dep.context, dep_item.record, prepared.policy)
                    new_context = context_key(item.record.context, item.record, prepared.policy)
                    if old_context and new_context and old_context != new_context:
                        continue
                    new_matches += 1
                    candidates.append(
                        candidate(
                            method,
                            {
                                "counterevidence": item.record.record_ref.identity_key,
                                "claim": (dep.judgment_id, dep.claim_id),
                            },
                            "未引用的新反证匹配线索",
                            [item, dep_item],
                            kind="hypothesis",
                            score=float(scores[dep_index]) + 1,
                            reasons=("previously_unreferenced_counterevidence_recall",),
                            metrics={
                                "judgment_id": dep.judgment_id,
                                "claim_id": dep.claim_id,
                                "claim_text": dep.claim_text,
                                "text_similarity": round(float(scores[dep_index]), 8),
                                "conditions_comparable": bool(old_context and new_context),
                            },
                            quotes={item.record.document_hash: quote},
                            data_gaps=()
                            if old_context and new_context
                            else ("comparability_context_missing",),
                            questions=("新材料是否真正反驳同一主张和适用条件？",),
                            requirements=("B07专家影响裁决", "S04/S05支持与反证核验"),
                        )
                    )
    if not text_count and not retraction_items:
        return unsupported(method, prepared, "authorized_text_or_versioned_source_change_required")
    return result(
        method,
        prepared,
        candidates,
        statistics={
            "text_records_scanned": text_count,
            "negative_cues": len(cues),
            "dependency_count": len(dependencies),
            "direct_matches": direct_matches,
            "unreferenced_matches": new_matches,
            "dependency_variant_status": "success" if dependencies else "unsupported",
        },
        truncated=truncated,
        limitations=("negative_cues_do_not_prove_failure", "no_comprehensive_counterevidence_absence_claim"),
    )
