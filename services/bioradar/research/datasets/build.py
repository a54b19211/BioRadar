"""Pure dataset construction; source parsing, access and persistence stay outside."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC

from bioradar.research.datasets.intelligence import public_health_applicability, source_units
from bioradar.research.datasets.narrative import applicable_directions, extract_narrative_observations
from bioradar.research.mining.methods_pipeline import context_key
from bioradar.research.mining.methods_text import is_structured_json
from bioradar.research.mining.models import KddPolicy, NormalizedSnapshot, canonical_hash
from bioradar.research.mining.prepare import (
    PreparedRecord,
    PreparedSnapshot,
    confirmed_ref,
    prepare,
    publication_interval,
    ref_key,
    section_visible,
    version_key,
)
from bioradar.research.mining.signals import checkable_claims

DATASET_VERSION = "bioradar-analysis-dataset-v1"
UNIT_CONTRACTS = {
    "research": "study_x_conditions_or_explicit_exploratory_document",
    "pipeline": "project_or_registration_x_indication_x_conditions_x_observed_time",
    "business": "source_qualified_company_x_metric_x_comparable_period",
    "public_health": "region_x_indicator_x_period_x_population",
}


def dataset_version(policy):
    return (
        "bioradar-analysis-dataset-v2"
        if getattr(policy, "source_intelligence_version", None)
        and getattr(policy, "kdd_direction", None) == "public_health"
        else DATASET_VERSION
    )


def _known(record):
    return (record.available_at or record.observed_at).astimezone(UTC).isoformat()


def _member(item):
    record = item.record
    return {
        "record_ref": record.record_ref.model_dump(mode="json"),
        "text_hash": record.normalized_text_hash,
        "source_instance": record.source_instance,
        "source_id": record.source_id,
        "available_at": record.available_at.isoformat() if record.available_at else None,
        "observed_at": record.observed_at.isoformat(),
        "event_time": record.published_at.isoformat()
        if record.publication_time_verified and record.published_at
        else None,
        "normalization_version": record.normalization_version,
        "study_ref": record.study_cluster_ref.model_dump(mode="json") if record.study_cluster_ref else None,
        "mapping_versions": [
            {
                "mapping_ref": entity.mapping_ref.model_dump(mode="json") if entity.mapping_ref else None,
                "mapping_version": entity.mapping_version,
                "status": entity.mapping_status,
                "available_at": entity.mapping_available_at.isoformat()
                if entity.mapping_available_at
                else None,
            }
            for entity in record.canonical_entity_refs
        ],
        "context_provenance": record.context.provenance.model_dump(mode="json")
        if record.context and record.context.provenance
        else None,
    }


@dataclass(frozen=True)
class AnalysisDataset:
    prepared: PreparedSnapshot
    direction: str
    units: tuple[dict, ...]
    exclusions: tuple[dict, ...]
    gaps: tuple[str, ...]
    quantitative_exclusions: tuple[dict, ...] = ()

    def manifest(self):
        units = [
            {key: value for key, value in unit.items() if key != "features"}
            | {"feature_hash": canonical_hash(unit["features"])}
            | (
                {"correction_links": unit["features"]["correction_links"]}
                if unit["features"].get("correction_links")
                else {}
            )
            for unit in self.units
        ]
        value = {
            "schema_version": dataset_version(self.prepared.policy),
            "direction": self.direction,
            "unit_contract": UNIT_CONTRACTS[self.direction],
            "input_count": len(self.prepared.snapshot.records),
            "qualified_count": len(units),
            "qualified_count_unit": "analysis_units_not_documents_or_population",
            "units": units,
            "excluded_records": list(self.exclusions),
            "unknown_reasons": list(self.gaps),
            "denominator": {
                "kind": "qualified_adopted_units",
                "count": sum(
                    unit["unit_kind"]
                    not in {
                        "graph_relation_hint",
                        "source_claim",
                        "source_statement",
                        "source_information_event",
                    }
                    for unit in units
                ),
                "excluded_graph_hint_units": sum(
                    unit["unit_kind"] == "graph_relation_hint" for unit in units
                ),
                **(
                    {
                        "excluded_source_claim_units": sum(
                            unit["unit_kind"] == "source_claim" for unit in units
                        )
                    }
                    if any(unit["unit_kind"] == "source_claim" for unit in units)
                    else {}
                ),
                "population_total": None,
                "population_rate_permitted": False,
            },
            "coverage": {
                "scope": "adopted_snapshot",
                "mode": self.prepared.snapshot.coverage_mode,
                "adopted_scope_complete": self.prepared.coverage_complete,
                "record_budget_omitted": self.prepared.truncated_count,
                "source_population_coverage": "unknown",
                "sources": [c.model_dump(mode="json") for c in self.prepared.snapshot.coverage],
            },
            "versions": {
                "dataset": dataset_version(self.prepared.policy),
                "identity_rule": "source-qualified-native-or-confirmed-mapping-v1",
                "feature_aggregation": "unique-fixed-texts-equal-weight-v1",
            },
            "as_of": self.prepared.policy.as_of.isoformat(),
            "time_mode": self.prepared.policy.time_mode,
        }
        if dataset_version(self.prepared.policy) != DATASET_VERSION:
            source_kinds = {"source_statement", "source_information_event"}
            value.update(
                unit_contract="fixed_source_statements_and_information_events_or_qualified_public_health_metrics",
                retained_source_version_count=sum(u["unit_kind"] == "source_statement" for u in units),
                source_statement_count=sum(u["unit_kind"] == "source_statement" for u in units),
                source_information_event_count=sum(
                    u["unit_kind"] == "source_information_event" for u in units
                ),
                quantitative_qualified_count=sum(
                    u["unit_kind"] == "region_indicator_period_population" for u in units
                ),
                quantitative_exclusions=list(self.quantitative_exclusions),
            )
            value["denominator"]["excluded_source_intelligence_units"] = sum(
                u["unit_kind"] in source_kinds for u in units
            )
            value["versions"]["source_intelligence"] = self.prepared.policy.source_intelligence_version
        return value | {"dataset_hash": canonical_hash(value)}


def build_dataset(snapshot: NormalizedSnapshot, policy: KddPolicy) -> AnalysisDataset:
    prepared = prepare(snapshot, policy)
    direction = policy.kdd_direction
    exclusions = [
        {"record_ref": item.record_ref.model_dump(mode="json"), "reason": item.reason}
        for item in prepared.excluded_records
    ]
    gaps = set(prepared.limitations)
    groups = defaultdict(list)
    source_mode = dataset_version(policy) != DATASET_VERSION
    relevance = public_health_applicability(prepared) if source_mode else {}
    retained = [
        unit
        for item in prepared.history
        for unit in (
            source_units(item, relevance.get(item.record.record_ref.version_key), policy)
            if source_mode
            else ()
        )
    ]
    retained_refs = {canonical_hash(member["record_ref"]) for unit in retained for member in unit["members"]}
    quantitative_exclusions = []

    def exclude(item, reason, section=None, **details):
        value = {"record_ref": item.record.record_ref.model_dump(mode="json"), "reason": reason, **details}
        if section is not None:
            value["section_id"] = section
        if source_mode and canonical_hash(value["record_ref"]) in retained_refs:
            quantitative_exclusions.append(value)
        else:
            exclusions.append(value)
        gaps.add(reason)

    items = prepared.history if direction == "pipeline" else prepared.records
    for item in items:
        record = item.record
        if {"deleted", "retracted"} & set(record.record_flags):
            exclude(item, "record_deleted_or_retracted")
            continue
        applicability = applicable_directions(record)
        if source_mode:
            if record.record_ref.version_key in relevance:
                applicability["public_health"] = relevance[record.record_ref.version_key]["reason"]
            else:
                exclude(item, "direction_not_applicable_to_visible_content_or_typed_fields")
                continue
        if record.source_id == "external_archive" and direction not in applicability:
            exclude(item, "direction_not_applicable_to_visible_content_or_typed_fields")
            continue
        claims = checkable_claims(
            record.normalized_text or "", record.signal_context.entity_names if record.signal_context else ()
        )
        if claims and direction in applicability:
            claim_key = canonical_hash(["source_claim", direction, record.record_ref.version_key])
            groups[claim_key].append(
                (
                    item,
                    {
                        "unit_kind": "source_claim",
                        "condition_key": None,
                        "independent_study": False,
                        "dimensions": {
                            "direction": direction,
                            "statement_status": "unverified_source_claim",
                            "applicability_reason": applicability[direction],
                        },
                        "claims": claims,
                    },
                )
            )
        context = record.context
        if context and (not context.provenance or not section_visible(context.provenance, record, policy)):
            context = None
            gaps.add("context_not_available_at_cutoff")
        dimensions = context.model_dump(mode="json", exclude={"provenance"}) if context else {}
        condition = context_key(context)
        if direction == "pipeline" and record.graph_hints:
            for hint in record.graph_hints:
                if not section_visible(hint.provenance, record, policy):
                    exclude(item, "graph_hint_not_available_at_cutoff", hint.hint_ref.object_id)
                    continue
                groups[canonical_hash(["graph_hint", hint.hint_ref.version_key])].append(
                    (
                        item,
                        {
                            "unit_kind": "graph_relation_hint",
                            "condition_key": None,
                            "independent_study": False,
                            "dimensions": {
                                "subject_id": hint.subject_id,
                                "object_id": hint.object_id,
                                "relation_type": hint.relation_type,
                            },
                            "graph_hint": hint.model_dump(mode="json"),
                        },
                    )
                )
            gaps.add("graph_hypotheses_not_project_counts_or_clinical_support")
            if not record.project_ref and not record.trial_refs:
                continue
        if direction == "research":
            if (
                not record.normalized_text
                or record.text_status != "inline_authorized"
                or is_structured_json(record.normalized_text)
            ):
                exclude(item, "authorized_text_required")
                continue
            study_qualified = item.confirmed_study
            if study_qualified and record.study_cluster_ref:
                mappings = [
                    entity
                    for entity in record.canonical_entity_refs
                    if entity.canonical_entity_ref
                    and entity.canonical_entity_ref.identity_key == record.study_cluster_ref.identity_key
                ]
                if policy.time_mode == "availability_replay" or mappings:
                    study_qualified = confirmed_ref(record, record.study_cluster_ref, policy)
                    if not study_qualified:
                        gaps.add("study_independence_mapping_not_available_at_cutoff")
            independent = study_qualified and condition is not None
            identity = (
                ["study_condition", ref_key(record.study_cluster_ref), condition]
                if independent
                else ["exploratory_document", record.normalized_text_hash, canonical_hash(dimensions)]
            )
            if not independent:
                gaps.add("independent_study_or_conditions_unresolved_document_exploration_only")
            groups[canonical_hash(identity)].append(
                (
                    item,
                    {
                        "unit_kind": "study_condition" if independent else "exploratory_document",
                        "dimensions": dimensions,
                        "condition_key": condition,
                        "independent_study": independent,
                    },
                )
            )
        elif direction == "pipeline":
            if record.project_ref and confirmed_ref(record, record.project_ref, policy):
                projects = [(record.project_ref, "confirmed_project")]
            else:
                projects = [(ref, "native_registration") for ref in record.trial_refs]
            if not projects:
                exclude(item, "project_or_native_registration_identity_required")
                continue
            if not condition:
                gaps.add("competitive_conditions_incomplete_inventory_only")
            known_at = record.available_at or record.observed_at
            if known_at > policy.as_of:
                exclude(item, "pipeline_state_not_known_by_cutoff")
                continue
            conditions = (
                [context.model_copy(update={"disease_refs": (disease,)}) for disease in context.disease_refs]
                if context and context.disease_refs
                else [context]
            )
            for project, identity_basis in projects:
                for selected_context in conditions:
                    selected_condition = context_key(selected_context)
                    selected_dimensions = (
                        selected_context.model_dump(mode="json", exclude={"provenance"})
                        if selected_context
                        else {}
                    )
                    selected_dimensions |= {
                        "project_ref": project.model_dump(mode="json"),
                        "known_at": _known(record),
                    }
                    identity_dimensions = {
                        key: value for key, value in selected_dimensions.items() if key != "project_ref"
                    }
                    key = canonical_hash(
                        [ref_key(project), selected_condition, identity_dimensions, identity_basis]
                    )
                    groups[key].append(
                        (
                            item,
                            {
                                "unit_kind": identity_basis,
                                "dimensions": selected_dimensions,
                                "condition_key": selected_condition,
                                "independent_study": False,
                            },
                        )
                    )
        else:
            notice = direction == "public_health" and record.source_id == "who_don"
            if notice:
                interval = publication_interval(record)
                if interval is None:
                    gaps.add("notice_publication_time_unknown_no_period_pattern")
                key = canonical_hash(["native_who_notice", ref_key(record.record_ref)])
                groups[key].append(
                    (
                        item,
                        {
                            "unit_kind": "native_notice_publication",
                            "dimensions": {
                                "notice_source": record.source_id,
                                "notice_ref": record.record_ref.model_dump(mode="json"),
                                "region": None,
                                "disease": None,
                                "publication_start": interval[0].isoformat() if interval else None,
                                "publication_end": interval[1].isoformat() if interval else None,
                                "known_at": _known(record),
                            },
                            "condition_key": None,
                            "independent_study": False,
                        },
                    )
                )
                gaps.add("notification_event_only_population_and_incidence_unknown")
            natural = (
                extract_narrative_observations(
                    record, version=getattr(policy, "narrative_contract_version", None)
                )
                if direction == "business" and record.text_status == "inline_authorized"
                else {"observations": [], "gaps": []}
            )
            metrics = [
                metric
                for metric in (*record.metric_observations, *natural["observations"])
                if metric.domain == direction
            ]
            if getattr(policy, "narrative_contract_version", None) == "literal-narrative-observations-v2":
                metrics = list({metric.observation_id: metric for metric in metrics}.values())
            if natural["gaps"]:
                gaps.add("narrative_metric_scope_or_period_requires_review")
                for rejected in natural["gaps"]:
                    exclude(
                        item,
                        "narrative_observation_rejected",
                        observation_index=rejected["observation_index"],
                        issues=rejected.get("issues")
                        or [
                            {
                                "stage": "scope",
                                "reason": rejected["reason"],
                            }
                        ],
                    )
            if not metrics and not notice:
                exclude(item, "typed_metric_observations_not_supplied")
            for metric in metrics:
                reason = None
                if not section_visible(metric.provenance, record, policy):
                    reason = "metric_not_available_at_cutoff"
                elif metric.period_end > policy.as_of:
                    reason = "metric_period_not_complete_by_cutoff"
                elif metric.binding_status != "source_bound" or not metric.field_locators:
                    reason = "metric_source_field_binding_required"
                elif metric.measurement_basis != "reported":
                    reason = "metric_forecast_or_unverified_not_reported"
                elif direction == "business" and not metric.reporting_scope:
                    reason = "business_reporting_scope_required"
                elif metric.value_kind == "monetary" and not metric.currency:
                    reason = "monetary_currency_required"
                elif direction == "public_health" and (not metric.population or not metric.region):
                    reason = "public_health_population_and_region_required"
                elif (
                    direction == "public_health"
                    and metric.value_kind == "ratio"
                    and metric.denominator is None
                ):
                    reason = "public_health_rate_denominator_required"
                elif metric.denominator is not None and not metric.denominator_unit:
                    reason = "metric_denominator_unit_required"
                elif any(abs(v).adjusted() > 100 for v in (metric.value_lower, metric.value_upper)):
                    reason = "metric_numeric_calculation_limit"
                if reason:
                    exclude(item, reason, metric.observation_id)
                    continue
                mapped = confirmed_ref(record, metric.subject_ref, policy)
                subject = ref_key(metric.subject_ref)
                if not mapped:
                    subject = canonical_hash([record.source_instance, record.source_id, subject])
                    gaps.add("metric_subject_source_qualified_not_cross_source_confirmed")
                dimensions = {
                    "subject_key": subject,
                    "subject_ref": metric.subject_ref.model_dump(mode="json"),
                    "identity_basis": "confirmed_mapping" if mapped else "source_qualified_literal",
                    "metric_id": metric.metric_id,
                    "metric_version": metric.metric_version,
                    "unit": metric.unit,
                    "currency": metric.currency,
                    "value_kind": metric.value_kind,
                    "reporting_scope": metric.reporting_scope,
                    "population": metric.population,
                    "region": metric.region,
                    "measurement_basis": metric.measurement_basis,
                    "denominator_unit": metric.denominator_unit,
                    "period_start": metric.period_start.astimezone(UTC).isoformat(),
                    "period_end": metric.period_end.astimezone(UTC).isoformat(),
                }
                key = canonical_hash(
                    {key: value for key, value in dimensions.items() if key != "subject_ref"}
                )
                groups[key].append(
                    (
                        item,
                        {
                            "unit_kind": "company_metric_period"
                            if direction == "business"
                            else "region_indicator_period_population",
                            "dimensions": dimensions,
                            "condition_key": None,
                            "independent_study": False,
                            "metric": metric.model_dump(mode="json"),
                        },
                    )
                )
    # Only exact-version, same-dimensional correction inputs can replace a value.
    # Keep both source members and the original numbers in the fixed dataset.
    for key, members in list(groups.items()):
        if not members or "metric" not in members[0][1]:
            continue
        targets = {}
        present = {item.record.record_ref.version_key for item, _ in members}
        for item, meta in members:
            if item.record.signal_context:
                for relation in item.record.signal_context.source_relations:
                    target = relation.target_ref.version_key
                    if relation.kind == "correction" and target in present:
                        source_observations = {
                            m["metric"]["observation_id"]
                            for source, m in members
                            if source.record.record_ref.version_key == target
                        }
                        replacement_observations = {
                            m["metric"]["observation_id"]
                            for source, m in members
                            if source.record.record_ref.version_key == item.record.record_ref.version_key
                        }
                        if len(source_observations) != 1 or len(replacement_observations) != 1:
                            gaps.add("metric_correction_observation_target_ambiguous")
                            continue
                        targets.setdefault(target, set()).add(item.record.record_ref.version_key)
        for item, meta in members:
            replacements = targets.get(item.record.record_ref.version_key, set())
            if replacements and len(replacements) == 1 and not replacements.intersection(targets):
                meta["superseded_by"] = [list(ref) for ref in sorted(replacements)]
                for replacement, corrected in members:
                    if replacement.record.record_ref.version_key in replacements:
                        corrected.setdefault("correction_links", []).append(
                            {
                                "observation_id": corrected["metric"]["observation_id"],
                                "corrects_observation_ids": [meta["metric"]["observation_id"]],
                                "target_ref": item.record.record_ref.model_dump(mode="json"),
                                "qualification": (
                                    "explicit_source_correction_same_subject_metric_period_unit_and_scope"
                                ),
                            }
                        )
            elif replacements:
                gaps.add("metric_correction_cycle_or_multiple_replacements_unresolved")
    units = list(retained)
    text_budget = policy.limits.max_text_characters
    for key, members in sorted(groups.items()):
        meta = members[0][1]
        by_version = {version_key(item.record.record_ref): item for item, _ in members}
        records = [by_version[k] for k in sorted(by_version)]
        features = {}
        if meta["unit_kind"] == "source_claim":
            features = {
                "claims": meta["claims"],
                "qualification": "attributed_claim_not_confirmed_case_study_or_project",
            }
        elif direction == "research":
            unique_texts = {}
            for item in records:
                unique_texts.setdefault(item.record.normalized_text_hash, item.record.normalized_text)
            full = "\n".join(unique_texts[k] for k in sorted(unique_texts))
            features["text"] = full[:text_budget]
            features["text_truncated"] = len(full) > text_budget
            text_budget -= len(features["text"])
            if features["text_truncated"]:
                gaps.add("text_feature_budget_exhausted")
            windows = sorted({item.window for item in records})
            features["window"] = windows[0] if len(windows) == 1 else "mixed"
            if len(windows) > 1:
                gaps.add("study_members_span_time_windows_not_independent_new_studies")
            assessments = []
            for item in records:
                for assessment in item.record.assessments:
                    if item.record.study_cluster_ref is None or ref_key(
                        assessment.study_cluster_ref
                    ) != ref_key(item.record.study_cluster_ref):
                        exclude(
                            item, "assessment_study_identity_mismatch", assessment.assessment_ref.object_id
                        )
                        continue
                    if (
                        assessment.review_status == "reviewed"
                        and section_visible(assessment.provenance, item.record, policy)
                        and context_key(assessment.context, item.record, policy) == meta["condition_key"]
                    ):
                        assessments.append(assessment.model_dump(mode="json"))
            features["assessments"] = sorted(assessments, key=canonical_hash)
            embeddings = []
            for item in records:
                embedding = item.record.embedding
                if embedding and section_visible(embedding.provenance, item.record, policy):
                    embeddings.append(embedding.model_dump(mode="json"))
            features["embeddings"] = embeddings
            features["embedding_member_count"] = len(embeddings)
            features["text_member_count"] = len(unique_texts)
        elif meta["unit_kind"] == "graph_relation_hint":
            features = {"hint": meta["graph_hint"]}
        elif direction == "pipeline":
            features = {
                "stages": sorted(
                    {item.record.project_stage for item in records if item.record.project_stage}
                ),
                "statuses": sorted(
                    {item.record.project_status for item in records if item.record.project_status}
                ),
                "historical_backfill": any(
                    item.record.ingestion_mode == "historical_backfill" for item in records
                ),
                "exit_reasons": sorted(
                    {item.record.exit_reason for item in records if item.record.exit_reason}
                ),
            }
        elif meta["unit_kind"] == "native_notice_publication":
            features = {
                "window": records[0].window,
                "historical_backfill": any(
                    item.record.ingestion_mode == "historical_backfill"
                    or "historical_backfill" in item.record.record_flags
                    for item in records
                ),
                "source_new_to_scope": any("new_source" in item.record.record_flags for item in records),
            }
        else:
            metrics = [m["metric"] for _, m in members if not m.get("superseded_by")]
            superseded = [
                {"observation": m["metric"], "superseded_by_version_keys": m["superseded_by"]}
                for _, m in members
                if m.get("superseded_by")
            ]
            signatures = {
                canonical_hash([value["value_lower"], value["value_upper"], value["denominator"]])
                for value in metrics
            }
            features = {
                "observations": sorted(metrics, key=canonical_hash),
                "conflicting_values": len(signatures) > 1,
            }
            correction_links = [link for _, row in members for link in row.get("correction_links", [])]
            if correction_links:
                features["correction_links"] = sorted(correction_links, key=canonical_hash)
            if superseded:
                features["superseded_observations"] = superseded
                features["correction_qualification"] = "exact_version_same_metric_period_scope_only"
            if features["conflicting_values"]:
                gaps.add("conflicting_metric_values_not_averaged")
        unit = {
            "unit_id": key,
            **{k: meta[k] for k in ("unit_kind", "dimensions", "condition_key", "independent_study")},
        }
        unit["members"] = [_member(item) for item in records]
        unit["features"] = features
        units.append(unit)
    return AnalysisDataset(
        prepared,
        direction,
        tuple(sorted(units, key=lambda unit: unit["unit_id"])) if source_mode else tuple(units),
        tuple(sorted(exclusions, key=canonical_hash)),
        tuple(sorted(gaps)),
        tuple(sorted(quantitative_exclusions, key=canonical_hash)),
    )


def unit_records(dataset: AnalysisDataset, units: list[dict]) -> list[PreparedRecord]:
    refs = {canonical_hash(member["record_ref"]) for unit in units for member in unit["members"]}
    index = {
        canonical_hash(item.record.record_ref.model_dump(mode="json")): item
        for item in dataset.prepared.history
    }
    return [index[key] for key in sorted(refs)]
