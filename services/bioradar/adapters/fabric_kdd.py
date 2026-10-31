"""Native fields to analysis units; original values remain in Fabric."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from bioradar.research.mining.models import (
    EngineRecord,
    MetricObservation,
    MiningEntity,
    SectionProvenance,
    StudyContext,
    canonical_hash,
)

VERSION = "bioradar.fabric.kdd-fields.v1"
SEC_DOC = "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"


def pointer(raw, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("source pointer required")
    value = raw
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def fact(record, raw, name):
    fields = record.get("facts") or {}
    locator = (record.get("field_locators") or {}).get(name) or {}
    try:
        original = pointer(raw, locator["json_pointer"])
        return fields[name] if canonical_hash(original) == canonical_hash(fields[name]) else None
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def source_date(value):
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    if parsed.date().isoformat() != value:
        raise ValueError("source day required")
    return parsed


def enrich_record(reader, record, base, *, as_of, source_available_at=None):
    """Map verified native fields without a model, guessed dates or entity resolution."""
    fields = record.get("facts") or {}
    gaps, changes = set(base.missing_fields), {}
    received = record.get("received_at")
    received_known = datetime.fromisoformat(received.replace("Z", "+00:00")) if received else base.observed_at
    known = source_available_at or received_known
    provenance = SectionProvenance(
        schema_version=VERSION, provider_ref=reader.ref(record), available_at=known
    )
    try:
        raw = json.loads(record["raw_content"])
    except (ValueError, TypeError, KeyError):
        raw = None
    types = fields.get("publication_types") or []
    if isinstance(types, dict):
        types = types.get("pubType", [])
    if isinstance(types, list) and any(str(t).casefold() == "retracted publication" for t in types):
        changes["record_flags"] = tuple(sorted(set(base.record_flags) | {"retracted"}))
    if record["source_id"] == "clinicaltrials" and isinstance(raw, dict):
        trial = fact(record, raw, "trial_id")
        native = next(
            (
                e.canonical_entity_ref
                for e in base.canonical_entity_refs
                if e.entity_type == "registered_trial"
                and e.canonical_entity_ref
                and e.canonical_entity_ref.object_id == trial
            ),
            None,
        )
        if native:
            diseases = tuple(
                reader.ref(record).model_copy(
                    update={
                        "provider": "clinicaltrials.gov.native-field",
                        "object_type": "registered_condition_literal",
                        "object_id": value,
                    }
                )
                for value in (fact(record, raw, "conditions") or [])
                if isinstance(value, str) and value.strip()
            )
            eligibility = fact(record, raw, "eligibility") or {}
            outcomes = fact(record, raw, "primary_outcomes") or []
            context = StudyContext(
                disease_refs=diseases,
                population=eligibility.get("studyPopulation"),
                study_design=fact(record, raw, "study_type"),
                endpoint="; ".join(
                    x["measure"]
                    for x in outcomes
                    if isinstance(x, dict) and isinstance(x.get("measure"), str)
                )
                or None,
                provenance=provenance,
            )
            phase = fact(record, raw, "phase")
            changes.update(
                trial_refs=(native,),
                context=context,
                project_stage=" / ".join(phase) if isinstance(phase, list) else phase,
                project_status=fact(record, raw, "status"),
                exit_reason=fact(record, raw, "why_stopped"),
            )
            gaps.update({"native_registration_not_drug_program", "cross_trial_project_lineage_unresolved"})
    metrics = []
    if record["source_id"] == "sec_companyfacts" and isinstance(raw, dict):
        cik, taxonomy, concept = raw.get("cik"), raw.get("taxonomy"), raw.get("tag")
        if (
            type(cik) is int
            and cik > 0
            and taxonomy in {"us-gaap", "ifrs-full", "dei", "srt"}
            and isinstance(concept, str)
            and concept
            and fields.get("cik") == cik
            and fields.get("taxonomy") == taxonomy
            and fields.get("concept") == concept
        ):
            subject = reader.ref(record).model_copy(
                update={
                    "provider": "sec.gov",
                    "object_type": "filing_entity",
                    "object_id": "CIK" + str(cik).zfill(10),
                }
            )
            definition = canonical_hash([taxonomy, concept, raw.get("description"), VERSION])
            for observation in fields.get("financial_observations", []):
                try:
                    row_path = observation["source_row_pointer"]
                    row = pointer(raw, row_path)
                    for key, locator in observation["field_locators"].items():
                        if canonical_hash(observation[key]) != canonical_hash(
                            pointer(raw, locator["json_pointer"])
                        ) or canonical_hash(observation[key]) != canonical_hash(row[key]):
                            raise ValueError()
                    if any(key not in row for key in ("val", "start", "end", "filed", "accn")):
                        gaps.add("sec_missing_duration_or_filing_fields")
                        continue
                    unit = observation["unit"]
                    locator = observation["unit_locator"]
                    units = pointer(raw, locator["json_pointer"])
                    if (
                        locator.get("object_key") != unit
                        or units is not raw["units"]
                        or row not in units[unit]
                    ):
                        raise ValueError()
                    value = Decimal(str(row["val"]))
                    if type(row["val"]) is bool or not value.is_finite():
                        raise ValueError()
                    start, end, filed = (source_date(row[key]) for key in ("start", "end", "filed"))
                    if start > end or filed > as_of:
                        gaps.add("sec_period_or_filing_not_available_at_cutoff")
                        continue
                    locators = dict(observation["field_locators"])
                    locators.update(
                        unit=locator,
                        subject={"json_pointer": "/cik"},
                        reporting_scope={
                            "rule": "sec.xbrl.entire_filing_entity.v1",
                            "source_documentation": SEC_DOC,
                            "native_identity_pointer": "/cik",
                            "qualification": "consolidation_boundary_may_change",
                        },
                        period_end_conversion={
                            "rule": "inclusive_source_day_to_exclusive_next_day.v1",
                            "source_value": row["end"],
                        },
                    )
                    currency = unit if unit in {"USD", "EUR", "GBP", "CNY"} else None
                    metrics.append(
                        MetricObservation(
                            observation_id=canonical_hash([record["version_id"], row_path]),
                            domain="business",
                            subject_ref=subject,
                            metric_id=taxonomy + ":" + concept,
                            metric_version=definition,
                            value_lower=value,
                            value_upper=value,
                            unit=unit,
                            value_kind="monetary" if currency else "other",
                            currency=currency,
                            reporting_scope="sec_filing_entity_wide_as_reported",
                            period_start=start,
                            period_end=end + timedelta(days=1),
                            measurement_basis="reported",
                            binding_status="source_bound",
                            field_locators=locators,
                            evidence_refs=(reader.ref(record),),
                            provenance=provenance,
                        )
                    )
                except (ValueError, TypeError, KeyError, IndexError, InvalidOperation):
                    gaps.add("sec_native_field_binding_invalid")
            changes["company_refs"] = (subject,)
            changes["canonical_entity_refs"] = (
                *base.canonical_entity_refs,
                MiningEntity(
                    canonical_entity_ref=subject,
                    temporary_entity_ref=subject,
                    entity_type="filing_entity",
                    mapping_ref=reader.ref(record),
                    mapping_version="sec.cik.native-field.v1",
                    mapping_status="confirmed",
                    mapping_available_at=received_known,
                ),
            )
            gaps.update(
                {
                    "consolidation_changes_unknown_as_reported_comparison_only",
                    "sec_fy_fp_are_filing_context_not_comparable_period_identity",
                }
            )
        else:
            gaps.add("sec_native_company_concept_identity_unverified")
    if record["source_id"] == "who_don":
        from bioradar.adapters.fabric_who import metric_observations

        who_metrics, who_gaps = metric_observations(reader, record, provenance, as_of=as_of)
        metrics.extend(who_metrics)
        gaps.update(who_gaps)
    if metrics:
        changes["metric_observations"] = tuple(metrics)
    changes["missing_fields"] = tuple(sorted(gaps))
    changes["upstream_status"] = {**base.upstream_status, "kdd_field_projection": VERSION}
    return EngineRecord.model_validate({**base.model_dump(mode="json"), **changes})
