"""Deterministic triage of automatic source-version updates; never a semantic verdict."""

from bioradar.api.errors import DomainError
from bioradar.research.mining.models import canonical_hash

MATERIAL_IMPACT_VERSION = "dependency-aware-material-update-v1"

CALCULATION_UNITS = {
    "research_tfidf_topics": {"study_condition", "exploratory_document"},
    "research_embedding_clusters": {"study_condition", "exploratory_document"},
    "research_evidence_balance": {"study_condition"},
    "conditional_pipeline_landscape": {"confirmed_project", "native_registration"},
    "business_metric_change": {"company_metric_period"},
    "business_utilization": {"company_metric_period"},
    "public_health_metric_change": {"region_indicator_period_population"},
    "public_health_notifications": {"native_notice_publication"},
}


def _ref(record):
    return record.record_ref.model_dump(mode="json")


def _dependencies(fixed, progress, result):
    evidence = {item["key"]: item for item in [*fixed.get("evidence", []), *progress.get("evidence", [])]}
    dependencies = {}
    for claims in [
        (result or {}).get("claims", []),
        ((progress.get("query_policy") or {}).get("reading") or {}).get("claims", []),
    ]:
        for claim in claims:
            for link in claim.get("evidence_links", []):
                item = evidence.get(link.get("evidence_key"))
                if item is not None and link.get("role") in {"supports", "refutes", "limits"}:
                    dependencies.setdefault(canonical_hash(item["record_ref"]), set()).add(claim["id"])
    for artifact in progress.get("kdd_artifacts", []):
        if artifact.get("kind") != "pattern_result":
            continue
        calculation = progress.get("kdd_results", {}).get(artifact["id"], {})
        kinds = set().union(
            *(
                CALCULATION_UNITS.get(method.get("method_id"), set())
                for method in calculation.get("method_results", [])
                if method.get("execution_status") in {"success", "partial"}
            )
        )
        for unit in (calculation.get("data_preparation") or {}).get("units", []):
            if unit.get("unit_kind") not in kinds:
                continue
            for member in unit.get("members", []):
                if member.get("record_ref"):
                    dependencies.setdefault(canonical_hash(member["record_ref"]), set()).add("calculation")
    return dependencies


def _inputs(record):
    own = _ref(record)
    binding = {
        key: own[key]
        for key in ("provider", "object_type", "object_id", "version_source", "authorization_ref")
    }

    def normalize(value):
        if isinstance(value, dict):
            if value == own:
                return binding
            return {key: normalize(child) for key, child in value.items()}
        return [normalize(child) for child in value] if isinstance(value, list) else value

    value = record.model_dump(mode="json", exclude={"record_ref", "document_hash"})
    return {"binding": binding, **normalize(value)}


def _complete(record):
    return record.text_status == "inline_authorized" and bool(record.normalized_text)


def evaluate_material_impact(fixed, progress, result, updates, prior_records, updated_records):
    """Only exact positive input equality or actual saved dependencies decide the outcome."""
    if fixed.get("query_policy", {}).get("material_impact_version") != MATERIAL_IMPACT_VERSION:
        raise DomainError("analysis_material_impact_version", "材料影响判断版本不受支持", 409)
    dependencies = _dependencies(fixed, progress, result)
    new_by_ref = {}
    for record in updated_records:
        new_by_ref.setdefault(canonical_hash(_ref(record)), {})[canonical_hash(record)] = record
    prior = {canonical_hash(record): record for record in prior_records}
    decisions = []
    for update in updates:
        decision = {
            **update,
            "old_ref": None,
            "old_refs": [],
            "status": "impact_pending",
            "reason": "fixed_version_mapping_incomplete",
            "affected_claim_ids": [],
        }
        matches = list(new_by_ref.get(canonical_hash(update["new_ref"]), {}).values())
        if len(matches) == 1:
            new = matches[0]
            olds = sorted(
                (
                    old
                    for old in prior.values()
                    if old.record_ref.identity_key == new.record_ref.identity_key
                    and old.source_instance == new.source_instance
                    and _ref(old) != _ref(new)
                ),
                key=lambda old: canonical_hash(_ref(old)),
            )
            decision["old_refs"] = [_ref(old) for old in olds]
            decision["old_ref"] = _ref(olds[0]) if len(olds) == 1 else None
            if not olds:
                decision["reason"] = "previous_fixed_version_unavailable"
            elif not _complete(new) or any(not _complete(old) for old in olds):
                decision["reason"] = "complete_source_inputs_unavailable"
            else:
                equivalent = [
                    old.record_ref.content_hash == new.record_ref.content_hash
                    and old.document_hash == new.document_hash
                    and _inputs(old) == _inputs(new)
                    for old in olds
                ]
                affected = [
                    old
                    for old, same in zip(olds, equivalent, strict=True)
                    if not same and canonical_hash(_ref(old)) in dependencies
                ]
                if affected:
                    decision.update(
                        status="affects_dependency",
                        reason="actual_dependency_source_inputs_changed",
                        old_ref=_ref(affected[0]),
                        affected_claim_ids=sorted(
                            {claim for old in affected for claim in dependencies[canonical_hash(_ref(old))]}
                        ),
                    )
                elif all(equivalent):
                    decision.update(status="equivalent_inputs", reason="complete_fixed_inputs_equal")
                else:
                    decision["reason"] = "changed_context_or_unmapped_dependency"
        decisions.append(decision)
    status = next(
        (
            value
            for value in ("affects_dependency", "impact_pending")
            if any(item["status"] == value for item in decisions)
        ),
        "equivalent_inputs",
    )
    return {
        "version": MATERIAL_IMPACT_VERSION,
        "checked_revision": max(item["revision"] for item in updates),
        "status": status,
        "report_currentness": "prior_fixed_materials",
        "report_as_of": fixed.get("evidence_time", {}).get("cutoff"),
        "decisions": decisions,
    }
