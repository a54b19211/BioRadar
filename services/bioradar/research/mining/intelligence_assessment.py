"""Radar's separate Admiralty judgments bound to existing immutable source versions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from bioradar.research.mining.models import (
    AssessmentAxes,
    AssessmentAxesReviewInput,
    InformationCredibilityAssessment,
    ProviderRef,
    SourceReliabilityAssessment,
    canonical_hash,
)


def _value(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _ref(record):
    return ProviderRef.model_validate(_value(_field(record, "record_ref", record)))


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("assessment timestamps require a timezone")
    return value.astimezone(UTC)


def default_assessment_axes(records, *, evidence_level=None, heat=None, claims=None):
    """F/6 mean unassessed; native labels, attention and evidence never assign ratings."""
    records = list(records)
    refs = {_ref(record).version_key: _ref(record) for record in records}
    if claims is None:
        from bioradar.research.mining.signals import checkable_claims

        claims = []
        for record in records:
            text = _field(record, "normalized_text") or ""
            context = _field(record, "signal_context")
            names = _field(context, "entity_names", ())
            claims.extend({**claim, "record_ref": _ref(record)} for claim in checkable_claims(text, names))
    information = {}
    for claim in claims:
        reference = ProviderRef.model_validate(_value(claim["record_ref"]))
        if reference.version_key not in refs:
            continue
        text = claim["text"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        identifier = (
            "claim-"
            + canonical_hash([reference.version_key, claim.get("start"), claim.get("end"), digest])[:24]
        )
        information[(reference.version_key, identifier)] = InformationCredibilityAssessment(
            record_ref=reference, claim_id=identifier, claim_text=text, claim_text_sha256=digest
        )
    for key, reference in refs.items():
        if not any(identity[0] == key for identity in information):
            information[(key, None)] = InformationCredibilityAssessment(record_ref=reference)
    return AssessmentAxes(
        source_reliability=tuple(SourceReliabilityAssessment(record_ref=ref) for ref in refs.values()),
        information_credibility=tuple(information.values()),
        evidence_level=evidence_level,
        heat=heat,
    )


def combine_assessment_axes(values, *, evidence_level=None, heat=None):
    sources, information = {}, {}
    for value in values:
        if value is None:
            continue
        axes = AssessmentAxes.model_validate(value)
        sources.update((item.record_ref.version_key, item) for item in axes.source_reliability)
        information.update(
            ((item.record_ref.version_key, item.claim_id), item) for item in axes.information_credibility
        )
    return AssessmentAxes(
        source_reliability=tuple(sources.values()),
        information_credibility=tuple(information.values()),
        evidence_level=evidence_level,
        heat=heat,
    )


def finalize_human_assessment_axes(
    request, records, *, actor_id, assessed_at, entry_id, revision, previous_assessment_ref=None
):
    """Called only by the existing judgment writer with its actual native entry identity."""
    request = AssessmentAxesReviewInput.model_validate(request)
    records = list(records)
    adopted = {_ref(record).version_key: record for record in records}
    when = _instant(assessed_at)
    if not actor_id or not entry_id or type(revision) is not int or revision < 1:
        raise ValueError("human assessment requires native actor, entry and revision")

    def common(item):
        required = [item.record_ref, *item.assessment_basis_refs]
        if any(
            reference.version_key not in adopted or _ref(adopted[reference.version_key]) != reference
            for reference in required
        ):
            raise ValueError("assessment basis must use exact adopted fixed original versions and hashes")
        if len({ref.version_key for ref in item.assessment_basis_refs}) != len(item.assessment_basis_refs):
            raise ValueError("assessment basis contains duplicate fixed versions")
        if not item.assessment_basis.strip():
            raise ValueError("assessment requires explicit basis")
        return {
            "record_ref": item.record_ref,
            "assessment_status": "human_reviewed",
            "reason_code": "human_assessment",
            "assessment_basis_refs": item.assessment_basis_refs,
            "assessment_basis": item.assessment_basis,
            "assessed_by": actor_id,
            "assessed_at": when,
            "native_review_entry_id": entry_id,
            "review_revision": revision,
            "previous_assessment_ref": previous_assessment_ref,
        }

    sources, information = [], []
    if len({item.record_ref.version_key for item in request.source_reliability}) != len(
        request.source_reliability
    ):
        raise ValueError("duplicate source assessment target")
    if len({(item.record_ref.version_key, item.claim_id) for item in request.information_credibility}) != len(
        request.information_credibility
    ):
        raise ValueError("duplicate claim assessment target")
    for item in request.source_reliability:
        if not item.publisher_scope.strip():
            raise ValueError("source reliability requires publisher scope")
        sources.append(
            SourceReliabilityAssessment(
                **common(item), grade=item.grade, publisher_scope=item.publisher_scope
            )
        )
    for item in request.information_credibility:
        metadata = common(item)
        if not item.claim_id.strip() or not item.claim_text.strip():
            raise ValueError("information credibility requires an explicit claim")
        if item.grade == "1":
            if not item.independent_confirmation_basis.strip():
                raise ValueError("information 1 requires explicit independent confirmation basis")
            confirmations = item.independent_confirmation_refs
            basis = {ref.version_key for ref in item.assessment_basis_refs}
            if (
                len({ref.version_key for ref in confirmations}) < 2
                or len({ref.version_key for ref in confirmations}) != len(confirmations)
                or any(
                    ref.version_key not in basis or _ref(adopted[ref.version_key]) != ref
                    for ref in confirmations
                )
            ):
                raise ValueError(
                    "information 1 requires independent original confirmations in the adopted basis"
                )
            selected = [adopted[ref.version_key] for ref in confirmations]
            for record in selected:
                context = _field(record, "signal_context")
                relations = _field(context, "source_relations", ())
                metadata_doc = _field(context, "document_evidence", {}) or {}
                if not isinstance(metadata_doc.get("source_independence", {}), dict):
                    raise ValueError("independent source basis is unavailable")
                if (
                    any(_field(relation, "kind") == "repost" for relation in relations)
                    or metadata_doc.get("material_kind") == "repost"
                ):
                    raise ValueError("independent confirmation must cite originals, not reposts")
            from bioradar.research.mining.signals import source_groups

            groups = source_groups(selected)
            if len(groups["confirmed"]) < 2 or groups["unknown"]:
                raise ValueError("information 1 requires at least two traceable independent source groups")
        information.append(
            InformationCredibilityAssessment(
                **metadata,
                grade=item.grade,
                claim_id=item.claim_id,
                claim_text=item.claim_text,
                claim_text_sha256=hashlib.sha256(item.claim_text.encode()).hexdigest(),
                independent_confirmation_refs=item.independent_confirmation_refs,
                independent_confirmation_basis=item.independent_confirmation_basis,
            )
        )
    if not sources and not information:
        raise ValueError("assessment review must contain an explicit source or claim rating")
    return AssessmentAxes(source_reliability=tuple(sources), information_credibility=tuple(information))


def project_reviewed_assessment_axes(base, entries, *, cutoff=None):
    """Only matching native judgment records confer human authority; never a snapshot claim."""
    base = AssessmentAxes.model_validate(base)
    sources = {
        item.record_ref.version_key: SourceReliabilityAssessment(record_ref=item.record_ref)
        for item in base.source_reliability
    }
    information = {
        (item.record_ref.version_key, item.claim_id): InformationCredibilityAssessment(
            record_ref=item.record_ref,
            claim_id=item.claim_id,
            claim_text=item.claim_text,
            claim_text_sha256=item.claim_text_sha256,
        )
        for item in base.information_credibility
    }
    allowed = {
        item.record_ref.version_key: item.record_ref
        for item in (*base.source_reliability, *base.information_credibility)
    }
    until = _instant(cutoff) if cutoff is not None else None
    reviews = []
    for entry in entries:
        if _field(entry, "kind") != "judgment":
            continue
        payload = _value(_field(entry, "payload", {})) or {}
        if not payload.get("assessment_axes"):
            continue
        created_at = _instant(_field(entry, "created_at"))
        if until is not None and created_at > until:
            continue
        reviews.append((created_at, _field(entry, "revision"), _field(entry, "id"), entry, payload))
    for created_at, revision, identifier, entry, payload in sorted(reviews, key=lambda value: value[:3]):
        review = AssessmentAxes.model_validate(payload["assessment_axes"])
        for kind in ("source_reliability", "information_credibility"):
            for item in getattr(review, kind):
                if not (
                    item.assessment_status == "human_reviewed"
                    and item.native_review_entry_id == identifier
                    and item.review_revision == revision
                    and item.assessed_by == _field(entry, "created_by")
                    and item.assessed_at == created_at
                    and allowed.get(item.record_ref.version_key) == item.record_ref
                ):
                    continue
                if kind == "source_reliability":
                    sources[item.record_ref.version_key] = item
                else:
                    information.pop((item.record_ref.version_key, None), None)
                    information[(item.record_ref.version_key, item.claim_id)] = item
    return AssessmentAxes(
        source_reliability=tuple(sources.values()),
        information_credibility=tuple(information.values()),
        evidence_level=base.evidence_level,
        heat=base.heat,
    )


def analysis_assessment_axes(analysis, entries, *, cutoff=None):
    """Project only native reviews of this exact report/claim and its used source versions.

    Historical routes pass the native fixed evidence cutoff; source-list search_scope
    alone is not a historical time binding. This function never changes final_text.
    """
    analysis = _value(analysis)
    temporal = analysis.get("evidence_time") or {}
    if temporal.get("mode", "current") != "current":
        if temporal["mode"] not in {"operational_replay", "public_history_reconstruction"}:
            raise ValueError("unsupported assessment historical mode")
        frozen_cutoff = _instant(temporal.get("cutoff"))
        if cutoff is not None and _instant(cutoff) != frozen_cutoff:
            raise ValueError("assessment cutoff differs from native fixed evidence time")
        cutoff = frozen_cutoff
    result = analysis.get("result") or {}
    evidence = analysis.get("evidence") or []
    by_key = {item["key"]: ProviderRef.model_validate(item["record_ref"]) for item in evidence}
    base = default_assessment_axes(evidence, claims=[])
    claims = {claim["id"]: claim for claim in result.get("claims", [])}
    claim_refs = {}
    information = []
    for identifier, claim in claims.items():
        refs = {
            by_key[link["evidence_key"]].version_key: by_key[link["evidence_key"]]
            for link in claim.get("evidence_links", [])
            if link.get("evidence_key") in by_key
        }
        claim_refs[identifier] = refs
        for reference in refs.values():
            information.append(
                InformationCredibilityAssessment(
                    record_ref=reference,
                    claim_id=identifier,
                    claim_text=claim["text"],
                    claim_text_sha256=hashlib.sha256(claim["text"].encode()).hexdigest(),
                )
            )
    base = base.model_copy(update={"information_credibility": tuple(information)})
    final_text = (result.get("report") or {}).get("final_text")
    selected = []
    for entry in entries:
        if (
            _field(entry, "kind") != "judgment"
            or not analysis.get("investigation_id")
            or _field(entry, "investigation_id") != analysis["investigation_id"]
        ):
            continue
        payload = _value(_field(entry, "payload", {})) or {}
        review = payload.get("review") or {}
        target = review.get("target") or {}
        if (
            target.get("type") not in {"report", "claim"}
            or target.get("analysis_id") != analysis.get("id")
            or not analysis.get("input_hash")
            or review.get("analysis_input_hash") != analysis["input_hash"]
        ):
            continue
        if target["type"] == "report":
            if (
                not isinstance(final_text, str)
                or not final_text
                or target.get("text_sha256") != hashlib.sha256(final_text.encode()).hexdigest()
            ):
                continue
        else:
            claim = claims.get(target.get("claim_id"))
            if claim is None or target.get("claim_sha256") != canonical_hash(claim):
                continue
        if not payload.get("assessment_axes"):
            continue
        rated = AssessmentAxes.model_validate(payload["assessment_axes"])
        basis = {
            ProviderRef.model_validate(ref).version_key: ProviderRef.model_validate(ref)
            for ref in review.get("basis_record_refs", [])
        }
        information = []
        for item in rated.information_credibility:
            claim = claims.get(item.claim_id)
            if (
                claim is not None
                and item.claim_text == claim["text"]
                and item.claim_text_sha256 == hashlib.sha256(claim["text"].encode()).hexdigest()
                and claim_refs[item.claim_id].get(item.record_ref.version_key) == item.record_ref
                and (target["type"] != "claim" or target["claim_id"] == item.claim_id)
                and all(basis.get(ref.version_key) == ref for ref in item.assessment_basis_refs)
            ):
                information.append(item)
        sources = tuple(
            item
            for item in rated.source_reliability
            if all(basis.get(ref.version_key) == ref for ref in item.assessment_basis_refs)
        )
        filtered = rated.model_copy(
            update={"source_reliability": sources, "information_credibility": tuple(information)}
        )
        selected.append(
            {
                "id": _field(entry, "id"),
                "kind": "judgment",
                "revision": _field(entry, "revision"),
                "created_by": _field(entry, "created_by"),
                "created_at": _field(entry, "created_at"),
                "payload": {**payload, "assessment_axes": filtered.model_dump(mode="json")},
            }
        )
    return project_reviewed_assessment_axes(base, selected, cutoff=cutoff)
