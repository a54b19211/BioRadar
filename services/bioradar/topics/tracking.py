"""Source-aware topic attention and lifecycle transitions over frozen contributions."""

import copy
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from bioradar.research.mining.models import ProviderRef, SignalContext, canonical_hash
from bioradar.research.mining.signals import REACTION, affirmed_event_anchors, source_groups

TRACKING_VERSION = "source-evidence-heat-lifecycle-v2"


def tracking_view(state):
    return {k: copy.deepcopy(v) for k, v in (state or {}).items() if not k.startswith("_")}


def update_tracking(values, definition, previous, *, as_of, source_errors=None):
    """Silence changes exposure, not truth or unresolved public-health risk."""
    old = copy.deepcopy(previous or {})
    values = [v for v in values if v.get("matched")]
    half_life = 36 if definition["domain"] == "public_health" else 21 * 24
    base_cadence = definition["cadence_hours"]
    seen = copy.deepcopy(old.get("_seen_source_groups", {}))
    refs, groups, confirmed, studies, claims, corrections, counter, environment = (
        {},
        {},
        set(),
        set(),
        [],
        [],
        [],
        [],
    )
    unknown_studies = 0
    grouped = source_groups(
        [
            SimpleNamespace(
                record_ref=ProviderRef.model_validate(value["record_ref"]),
                source_family=value.get("source_family"),
                source_independence_status=value.get("source_independence_status", "unresolved"),
                signal_context=SignalContext.model_validate(value["signal_context"])
                if value.get("signal_context")
                else None,
            )
            for value in values
        ]
    )
    # Group labels may gain another DOI/PMID or a more informative origin name.
    # An already observed member keeps that group's exposure clock and identity.
    prior_groups = old.get("_source_groups_by_identity", {})
    identity_groups = {}
    for group in grouped["confirmed"] + grouped["unknown"]:
        identities = [
            canonical_hash(ProviderRef.model_validate(ref).identity_key) for ref in group["record_refs"]
        ]
        prior_keys = sorted({prior_groups[key] for key in identities if key in prior_groups})
        if prior_keys:
            group["group_key"] = prior_keys[0]
        identity_groups.update({key: group["group_key"] for key in identities})
    group_by_ref = {
        canonical_hash(ref): group["group_key"]
        for group in grouped["confirmed"] + grouped["unknown"]
        for ref in group["record_refs"]
    }
    confirmed = {group["group_key"] for group in grouped["confirmed"]}
    for value in values:
        ref = value["record_ref"]
        ref_key = canonical_hash(ref)
        refs[ref_key] = ref
        attention = value.get("attention") or {}
        group_key = group_by_ref[ref_key]
        groups.setdefault(group_key, []).append(ref_key)
        if value.get("study_confirmed"):
            studies.add(value["unit"])
        else:
            unknown_studies += 1
        claims.extend(attention.get("checkable_claims", []))
        corrections.extend(attention.get("corrections", []))
        counter.extend(attention.get("reviewed_counterevidence_refs", []))
        environment.extend(attention.get("environment_signals", []))
        if group_key not in seen:
            # The first adopted exposure time is explicit. It is not an invented event date.
            raw = (
                value.get("exposure_available_at")
                or value.get("received_at")
                or value.get("first_seen_at")
                or as_of.isoformat()
            )
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    continue
                seen[group_key] = min(when, as_of).isoformat()
            except (TypeError, ValueError):
                continue
    new_groups = set(groups) - set(old.get("_seen_source_groups", {}))

    def decay(keys):
        return sum(
            2 ** (-max(0, (as_of - datetime.fromisoformat(seen[key])).total_seconds()) / (3600 * half_life))
            for key in keys
            if key in seen
        )

    provisional_score = decay(set(groups) - confirmed)
    score = decay(confirmed)
    level = "independent_sources" if len(confirmed) >= 2 else "unverified" if claims else "unknown"
    if any((v.get("attention") or {}).get("evidence_level") == "institution_confirmed" for v in values):
        level = "institution_confirmed"
    numeric = any((v.get("observation") or {}).get("kind") == "metric" for v in values)
    correction_status = "corrected" if corrections else "counterevidence" if counter else "none"
    semantic_claims = sorted(
        {
            re.sub(r"[\s：:；;，,]+", "", re.sub(r"^(?:转发|转载|网传|据称)[:：\s]*", "", claim["text"]))
            for claim in claims
        }
    )
    semantic_observations = [
        {
            key: observation.get(key)
            for key in (
                "metric_id",
                "metric_version",
                "value",
                "unit",
                "period_start",
                "period_end",
                "population",
                "region",
                "status",
            )
        }
        for value in values
        if (observation := value.get("observation"))
    ]
    semantic_states = [
        {
            key: (value.get("facts") or {}).get(key)
            for key in (
                "status",
                "phase",
                "why_stopped",
                "event_type",
                "primary_completion_date",
                "completion_date",
                "correction_links",
                "publication_state",
            )
        }
        for value in values
    ]
    has_state = any(any(value is not None for value in row.values()) for row in semantic_states)
    evidence_fingerprint = canonical_hash(
        [
            level,
            sorted(confirmed),
            semantic_claims,
            sorted({canonical_hash(row) for row in semantic_observations}),
            sorted({canonical_hash(row) for row in semantic_states}),
            corrections,
            counter,
        ]
    )
    changed = evidence_fingerprint != old.get("_evidence_fingerprint")
    risk = copy.deepcopy(old.get("risk") or {"status": "unknown", "critical_gaps": [], "basis_refs": []})
    risk_claims = sorted(
        {
            re.sub(r"[\s：:；;，,]+", "", re.sub(r"^(?:转发|转载|网传|据称)[:：\s]*", "", claim["text"]))
            for claim in claims
            if affirmed_event_anchors(claim) and not REACTION.search(claim["text"])
        }
    )
    # Versions/origin labels and environment reactions are not new risk semantics.
    # A recorded resolution covers its reviewed basis, not future substantive claims.
    risk_numeric = sorted(
        {
            canonical_hash(
                {
                    key: observation.get(key)
                    for key in (
                        "metric_id",
                        "metric_version",
                        "value",
                        "unit",
                        "period_start",
                        "period_end",
                        "population",
                        "region",
                    )
                }
            )
            for value in values
            if (observation := value.get("observation"))
            and observation.get("kind") == "metric"
            and observation.get("basis", {}).get("kind") == "source_bound"
            and observation.get("status") != "withdrawn"
        }
    )
    risk_atoms = sorted(
        {"claim:" + canonical_hash(claim) for claim in risk_claims}
        | {"metric:" + item for item in risk_numeric}
    )
    if definition["domain"] == "public_health" and risk_atoms:
        reviewed_atoms = set(risk.get("basis_atoms", []))
        # Disappearance of an old source/window is not newly observed risk. Keep
        # the reviewed set so the same claim returning later is still recognized.
        reopened = risk["status"] == "resolved" and bool(set(risk_atoms) - reviewed_atoms)
        if risk["status"] != "resolved" or reopened:
            if reopened:
                risk["prior_resolution"] = copy.deepcopy(risk.get("assessment"))
                risk["reopened_reason"] = "new_semantic_risk_outside_reviewed_basis"
            risk.update(
                status="unresolved",
                critical_gaps=["pathogen_transmission_and_control_not_resolved_by_adopted_evidence"],
                basis_refs=list(refs.values()),
                basis_sha256=canonical_hash(risk_atoms),
                basis_atoms=risk_atoms,
            )
    if source_errors:
        risk["critical_gaps"] = sorted(
            set(risk.get("critical_gaps", [])) | {"source_unavailable_not_evidence_of_resolution"}
        )
    unresolved = risk["status"] == "unresolved" or bool(risk.get("critical_gaps"))
    reaction_present = bool(environment)
    negative_signals = [
        signal for value in values for signal in (value.get("attention") or {}).get("negative_signals", [])
    ]
    not_found_present = any(signal.get("kind") == "not_found" for signal in negative_signals)
    new_independent = new_groups & confirmed
    semantic_changed = (
        bool(old.get("_evidence_fingerprint"))
        and changed
        and bool(claims or corrections or counter or numeric or has_state)
    )
    quiet = 0 if new_independent or semantic_changed else old.get("_quiet_rounds", 0) + 1
    phase = old.get("lifecycle", "incipient")
    if old.get("_manual_closed"):
        phase = "closed"
    elif semantic_changed:
        phase = "warming"
    elif new_independent and old.get("lifecycle") in {"cooling", "dormant"}:
        phase = "warming"
    elif new_independent and old.get("_seen_source_groups"):
        phase = "high" if len(confirmed) >= 3 else "warming"
    elif (
        not new_independent
        and quiet >= 2
        and not unresolved
        and not reaction_present
        and not not_found_present
    ):
        phase = "dormant"
    elif not new_independent and old:
        phase = "cooling"
    if (reaction_present or not_found_present) and phase == "dormant":
        phase = "cooling"
    cadence = (
        min(base_cadence, 1 if definition["domain"] == "public_health" else 24)
        if reaction_present or not_found_present
        else 1
        if unresolved and definition["domain"] == "public_health"
        else max(1, base_cadence // 2)
        if phase in {"warming", "high"}
        else min(720, base_cadence * 4)
        if phase == "dormant"
        else base_cadence
    )
    reasons = []
    if changed and (claims or corrections or counter or numeric or has_state):
        reasons.append("source_evidence_or_correction_state_changed")
    if phase == "warming" and old.get("lifecycle") in {"dormant", "cooling"} and changed:
        reasons.append("new_semantic_source_evidence_wakes_topic")
    if phase == "warming" and old.get("lifecycle") == "dormant" and new_groups & confirmed:
        reasons.append("new_independent_evidence_wakes_topic")
    # Quiet lifecycle changes and environment reactions alone never justify a new model draft.
    needed = bool(reasons) and phase not in {"closed", "dormant"}
    result = {
        "version": TRACKING_VERSION,
        "automatic": bool(old.get("automatic")),
        "origin": copy.deepcopy(old.get("origin")),
        "lifecycle": phase,
        "cadence_hours": cadence,
        "next_due_at": (as_of + timedelta(hours=cadence)).isoformat(),
        "evidence": {
            "level": level,
            "confirmed_source_groups": len(confirmed),
            "independent_study_count": None if unknown_studies else len(studies),
            "confirmed_independent_study_count": len(studies),
            "unknown_source_records": sum(
                not v.get("source_family") or v.get("source_independence_status") != "confirmed"
                for v in values
            ),
            "comparable_numeric_present": numeric,
            "basis_refs": list(refs.values()),
        },
        "heat": {
            "score": round(score, 8),
            "new_source_groups": len(new_independent),
            "provisional_exposure_score": round(provisional_score, 8),
            "new_provisional_groups": len(new_groups - confirmed),
            "confirmed_new_source_groups": len(new_groups & confirmed),
            "unknown_origin_groups": len(set(groups) - confirmed),
            "half_life_hours": half_life,
            "qualification": "adopted_origin_group_exposure_not_population_heat_or_confirmed_independence",
        },
        "correction": {"status": correction_status, "refs": corrections, "counterevidence_refs": counter},
        "risk": risk,
        "environment_signals": environment,
        "negative_signals": negative_signals,
        "model_update": {"needed": needed, "reasons": reasons, "status": "ready" if needed else "not_needed"},
        "_seen_source_groups": seen,
        "_source_groups_by_identity": identity_groups,
        "_quiet_rounds": quiet,
        "_evidence_fingerprint": evidence_fingerprint,
        "_manual_closed": bool(old.get("_manual_closed")),
        "assessment_reason": reasons or ["pure_exposure_update_no_new_evidence_reason_for_model"],
    }
    if old.get("hypothesis_comparison"):
        result["hypothesis_comparison"] = copy.deepcopy(old["hypothesis_comparison"])
        judgment = result["hypothesis_comparison"]
        judgment["current_basis_unchanged"] = bool(
            judgment.get("current_basis_unchanged")
            and judgment.get("evidence_fingerprint") == evidence_fingerprint
        )
    return result


def hypothesis_comparison(hypotheses, claims, *, model_judgment=None):
    """Reuse the actual synthesis judgment; evidence counts never elect a hypothesis."""
    known = {row.get("id", row.get("claim_id")) for row in claims}
    result = []
    for item in hypotheses:
        row = copy.deepcopy(item)
        for key in ("supporting_claim_ids", "opposing_claim_ids"):
            if not set(row.get(key, [])) <= known:
                raise ValueError("hypothesis evidence references an undeclared claim")
        row.setdefault("testability", "not_assessed")
        row.setdefault("gaps", [])
        row.setdefault("discriminating_evidence", [])
        row.pop("winner", None)
        result.append(row)
    comparison = {
        "hypotheses": result,
        "selection": "not_automatically_ranked",
        "qualification": "support_counterevidence_testability_and_gaps_are_parallel",
    }
    if model_judgment is not None:
        from bioradar.research.investigations.analysis_models import HypothesisJudgment

        judgment = HypothesisJudgment.model_validate(model_judgment).model_dump(mode="json")
        if judgment["least_refuted_hypothesis_id"] not in {row["id"] for row in result}:
            raise ValueError("model judgment references an undeclared hypothesis")
        comparison.update(
            selection="model_judgment_unverified",
            current_judgment=judgment,
            qualification="这是模型判断，未证实",
        )
    return comparison


def automatic_topic_definition(candidate, records, *, source_instance):
    from bioradar.research.mining.signals import narrative_anchors
    from bioradar.topics.models import TopicDefinition

    attention = candidate.metrics.get("attention") or {}
    if candidate.importance not in {"medium", "high"} or not attention.get("automatic_investigation_allowed"):
        return None
    records = list(records)
    if not records:
        return None
    text = "\n".join(row["text"] for row in attention.get("checkable_claims", [])) or candidate.title
    anchors = narrative_anchors(text)
    terms = list(dict.fromkeys(a["text"] for a in anchors if a["kind"] in {"event", "entity"}))
    if not terms:
        return None
    direction = candidate.metrics.get("kdd_direction") or "other"
    return TopicDefinition(
        title=("持续核查：" + candidate.title)[:200],
        question=(
            "持续核查这一有出处但尚待验证的事件："
            + candidate.title
            + "。比较支持、反证、条件与竞争解释；热度变化不能替代风险证据。"
        )[:2000],
        domain=direction,
        source_instance=source_instance,
        source_ids=sorted({record.source_id for record in records}),
        terms_any=terms[:16],
        profiles=["text_exposure", "source_event"],
        bucket="day" if direction == "public_health" else "week",
        cadence_hours=1 if direction == "public_health" else 24,
        lookback_buckets=24,
    )
