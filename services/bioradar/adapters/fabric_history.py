"""Bounded, read-only access to Data-Fabric's adopted historical versions."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import httpx

from bioradar.api.errors import DomainError
from bioradar.research.history.models import (
    HistoricalEvent,
    HistoricalUnit,
    HistoryObservation,
)
from bioradar.research.mining.models import (
    EngineRecord,
    Locator,
    NormalizedSnapshot,
    ProviderRef,
    SnapshotCoverage,
    TimeExpression,
)
from bioradar.research.mining.prepare import time_interval


def history_error():
    return DomainError("history_source_invalid", "历史来源响应、出处或时间资格不符合契约", 502)


def time_upper(value):
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(UTC)
    if not isinstance(value, dict):
        raise ValueError("invalid time value")
    precision = value.get("precision", "unknown")
    raw = value.get("value")
    if precision == "instant":
        return time_upper(raw)
    interval = time_interval(TimeExpression(raw=raw, precision=precision))
    return interval[1] - timedelta(microseconds=1) if interval else None


def fact_time(value, *, lower=False):
    """Source facts may have coarse precision; server receipt never uses this."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("source fact time must be a string")
    precision = (
        "year"
        if len(value) == 4
        else "month"
        if len(value) == 7
        else "day"
        if len(value) == 10
        else "instant"
    )
    if precision == "instant":
        return time_upper(value)
    interval = time_interval(TimeExpression(raw=value, precision=precision))
    if interval is None:
        raise ValueError("invalid source date")
    return interval[0] if lower else interval[1] - timedelta(microseconds=1)


def json_pointer(value, pointer):
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("facts require a JSON pointer")
    for component in pointer[1:].split("/"):
        component = component.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(component)]
        elif isinstance(value, dict):
            value = value[component]
        else:
            raise ValueError("pointer outside original JSON")
    return value


def exact_json(left, right):
    # Python considers True == 1; typed source values must not.
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False, allow_nan=False
    )


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate original JSON keys")
        result[key] = value
    return result


def validate_record(record, source_id, is_demo, mode, cutoff):
    if (
        not isinstance(record, dict)
        or record.get("source_id") != source_id
        or record.get("is_demo") is not is_demo
        or not isinstance(record.get("source_record_id"), str)
        or not record.get("source_record_id")
        or not isinstance(record.get("version_id"), str)
        or len(record["version_id"]) != 64
        or record.get("record_kind") not in {"trial", "program", "outcome", "document"}
        or record.get("schema_version") != "datafabric.history.record.v1"
    ):
        raise history_error()
    try:
        raw = record["raw_content"]
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != record["raw_sha256"]:
            raise ValueError("raw hash mismatch")
        if not isinstance(record["clean_text"], str) or not record["parser_version"]:
            raise ValueError("missing deterministic projection")
        facts = record["facts"]
        locators = record["field_locators"]
        if not isinstance(facts, dict) or not isinstance(locators, dict):
            raise ValueError("invalid facts")
        if record.get("raw_media_type") == "application/json":
            original = json.loads(raw, object_pairs_hook=_no_duplicates) if facts else {}
            for key, value in facts.items():
                locator = locators[key]
                if not exact_json(json_pointer(original, locator["json_pointer"]), value):
                    raise ValueError("unbound fact")
        elif record["record_kind"] == "document" and record.get("raw_media_type") == "application/xml":
            # XML remains a source-projected document, never a program/outcome or
            # a derived follow-up identifier in this implementation.
            from xml.etree import ElementTree

            ElementTree.fromstring(raw)
            if any(not locators.get(key, {}).get("xpath") for key in facts):
                raise ValueError("missing source XML locator")
        else:
            raise ValueError("unsupported raw media type")
        first_seen = time_upper(record["first_seen_at"])
        received = time_upper(record["received_at"])
        if first_seen is None or received is None or first_seen > received:
            raise ValueError("invalid server observation")
        if mode == "operational_replay" and (first_seen > cutoff or received > cutoff):
            raise ValueError("not observed at operational cutoff")
        if mode == "public_history_reconstruction":
            proof = record.get("availability_proof") or {}
            available = time_upper(proof.get("available_at"))
            if (
                not proof.get("verified_by")
                or not proof.get("verified_at")
                or not proof.get("uri")
                or not proof.get("sha256")
                or available is None
                or available > cutoff
            ):
                raise ValueError("unverified or future historical proof")
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, SyntaxError):
        raise history_error() from None
    return record


class FabricHistoryReader:
    def __init__(
        self,
        client: httpx.Client,
        *,
        authorization_ref: str,
        max_records=5000,
        max_bytes=8_000_000,
        max_seconds=90,
        source_instance="datafabric-test",
    ):
        self.client = client
        self.authorization_ref = authorization_ref
        self.source_instance = source_instance
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.deadline = time.monotonic() + max_seconds
        self.bytes_read = 0
        self.records_read = 0
        self.requests_made = 0

    def _get(self, path, params=None, *, request_seconds=20):
        if time.monotonic() >= self.deadline or self.bytes_read >= self.max_bytes:
            raise DomainError("history_read_budget", "历史读取达到时间或字节预算", 422)
        try:
            remaining = self.max_bytes - self.bytes_read
            with self.client.stream(
                "GET",
                path,
                params=params,
                timeout=min(request_seconds, max(0.1, self.deadline - time.monotonic())),
            ) as response:
                self.requests_made += 1
                if response.status_code in {401, 403, 404}:
                    raise DomainError("history_source_not_accessible", "来源或版本当前不可读取", 403)
                if not response.is_success:
                    raise DomainError("history_source_unavailable", "历史来源未成功响应", 502, True)
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    self.bytes_read += len(chunk)
                    if time.monotonic() >= self.deadline or len(data) > remaining:
                        raise DomainError("history_read_budget", "历史读取达到字节预算", 422)
                return json.loads(data)
        except (httpx.HTTPError, ValueError):
            raise DomainError(
                "history_source_unavailable", "历史来源读取失败，未自动重试", 502, True
            ) from None

    def sources(self):
        value = self._get("/api/history/sources", request_seconds=3)
        if isinstance(value, dict):
            value = value.get("items", value.get("sources"))
        if not isinstance(value, list):
            raise history_error()
        return value

    def collect(
        self, *, source_id, is_demo, mode, cutoff, terms=(), run_id=None, record_ids=(), record_kind=None
    ):
        items, cursors, excluded = [], set(), {}
        cursor, coverage, exhausted = None, None, False
        page_limit = min(100, max(1, self.max_records - self.records_read))
        while True:
            left = self.max_records - self.records_read
            if left <= 0:
                exhausted = True
                break
            params = [
                ("source_id", source_id),
                ("is_demo", str(is_demo).lower()),
                ("mode", mode),
                ("cutoff", cutoff.isoformat()),
                ("limit", str(page_limit)),
            ]
            if record_kind:
                params.append(("record_kind", record_kind))
            if run_id:
                params.append(("run_id", run_id))
            params.extend(("terms", term) for term in terms)
            params.extend(("source_record_ids", identifier) for identifier in record_ids)
            if cursor:
                params.append(("cursor", cursor))
            try:
                page = self._get("/api/history/records", params)
            except DomainError as exc:
                if not items:
                    raise
                exhausted = True
                excluded[exc.code] = excluded.get(exc.code, 0) + 1
                break
            if (
                not isinstance(page, dict)
                or page.get("schema_version") != "datafabric.history.collection.v1"
                or not isinstance(page.get("items"), list)
                or len(page["items"]) > page_limit
                or not isinstance(page.get("coverage"), dict)
            ):
                raise history_error()
            new_coverage = page["coverage"]
            if coverage is not None and any(
                new_coverage.get(k) != coverage.get(k) for k in ("run_id", "max_seq", "as_of")
            ):
                raise history_error()
            coverage = new_coverage
            for record in page["items"][:left]:
                items.append(validate_record(record, source_id, is_demo, mode, cutoff))
                self.records_read += 1
            for key, count in page.get("excluded_counts", {}).items():
                if type(count) is not int or count < 0:
                    raise history_error()
                excluded[key] = max(excluded.get(key, 0), count)
            if len(page["items"]) > left:
                exhausted = True
                excluded["record_budget_truncated_page"] = len(page["items"]) - left
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors or len(cursor) > 8192:
                raise history_error()
            cursors.add(cursor)
        coverage = dict(
            coverage
            or {
                "mode": "unknown",
                "scope_description": "未成功取得声明范围",
                "complete": False,
                "as_of": cutoff.isoformat(),
            }
        )
        if exhausted or cursor:
            coverage["complete"] = False
            coverage["stop_reason"] = "budget_or_read_failure"
        unique = {}
        for item in items:
            key = (item["source_id"], item["source_record_id"], item["version_id"])
            if key in unique and unique[key] != item:
                raise history_error()
            unique[key] = item
        return {
            "items": list(unique.values()),
            "coverage": coverage,
            "excluded_counts": excluded,
            "source_id": source_id,
            "is_demo": is_demo,
            "cutoff": cutoff,
        }

    def read_version(self, binding, *, mode="current", cutoff=None):
        params = {
            "source_id": binding["source_id"],
            "source_record_id": binding["source_record_id"],
            "is_demo": str(binding["is_demo"]).lower(),
            "mode": mode,
        }
        if cutoff is not None:
            params["cutoff"] = cutoff.isoformat()
        value = self._get("/api/history/versions/" + binding["version_id"], params)
        record = value.get("record", value) if isinstance(value, dict) else value
        checked = validate_record(record, binding["source_id"], binding["is_demo"], mode, cutoff)
        if checked["version_id"] != binding["version_id"]:
            raise history_error()
        return checked

    def ref(self, record):
        return ProviderRef(
            provider="data_fabric_history",
            object_type="source_record",
            object_id=json.dumps(
                [record["source_id"], record["source_record_id"], record["is_demo"]],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            adopted_version=record["version_id"],
            version_source="provider",
            content_hash=record["raw_sha256"],
            authorization_ref=self.authorization_ref,
        )

    def snapshot(self, collection, *, outcomes=False):
        selected = [r for r in collection["items"] if (r["record_kind"] == "outcome") is outcomes]
        if not selected:
            return None
        instance = "fixture" if collection["is_demo"] else self.source_instance
        records = []
        for record in selected:
            proof = record.get("availability_proof") or {}
            available = time_upper(proof.get("available_at"))
            published = record.get("source_published_at")
            precision = published.get("precision", "unknown") if isinstance(published, dict) else "unknown"
            published_instant = time_upper(published) if precision == "instant" else None
            records.append(
                EngineRecord(
                    record_ref=self.ref(record),
                    document_hash=record["raw_sha256"],
                    source_instance=instance,
                    source_id=record["source_id"],
                    observed_at=time_upper(record["received_at"]),
                    first_seen_at=time_upper(record["first_seen_at"]),
                    available_at=available,
                    published_at=published_instant,
                    published_time_raw=published.get("value") if isinstance(published, dict) else None,
                    time_precision=precision,
                    publication_time_verified=published is not None,
                    normalized_text=record["clean_text"] or None,
                    text_status="inline_authorized" if record["clean_text"] else "unavailable",
                    normalization_version=record["parser_version"],
                    source_url=record.get("source_url"),
                    ingestion_mode="fixture" if collection["is_demo"] else "historical_backfill",
                    locator=Locator(
                        reason="normalized quotation; exact original available by source version"
                    ),
                    missing_fields=("unresolved_study_and_source_independence",),
                )
            )
        return NormalizedSnapshot(
            snapshot_id="history-"
            + hashlib.sha256("|".join(sorted(r["version_id"] for r in selected)).encode()).hexdigest()[:24],
            source_instance=instance,
            is_demo=collection["is_demo"],
            observed_at=max(r.observed_at for r in records),
            coverage_mode="bounded_sample",
            records=tuple(records),
            coverage=(
                SnapshotCoverage(
                    source_instance=instance,
                    source_id=collection["source_id"],
                    mode="bounded_sample",
                    scope_description=collection["coverage"].get("scope_description", "限定采用资料"),
                    record_count=len(records),
                    reasons=("historical_cohort_not_independent_study_exposure",),
                ),
            ),
            metadata_gaps=("history_mode_checked_before_adoption_no_growth_confirmation",),
        )

    def observation(self, collection, *, snapshot_id=None, snapshot_hash=None, unit_kind=None):
        units, events, gaps, conflicted = {}, [], [], set()
        cutoff = collection["cutoff"]
        for record in collection["items"]:
            facts = record["facts"]
            ref = self.ref(record)
            kind = record["record_kind"]
            if kind == "outcome":
                try:
                    observed = facts.get("event_observed")
                    if observed is not None and type(observed) is not bool:
                        raise ValueError("event_observed is not a typed boolean")
                    event_type = facts["event_type"]
                    if event_type not in {"trial_stoppage", "regulatory_approval", "next_phase_disclosure"}:
                        gaps.append("unsupported_outcome_type")
                        continue
                    public_time = fact_time(facts.get("public_available_at"))
                    if public_time is None or public_time > cutoff:
                        gaps.append("outcome_not_public_by_observation_cutoff")
                        continue
                    event_at = fact_time(facts.get("event_at"))
                    events.append(
                        HistoricalEvent(
                            unit_id=facts["unit_id"],
                            unit_kind=facts["unit_kind"],
                            event_type=event_type,
                            public_available_at=public_time,
                            event_at=event_at,
                            event_observed=facts.get("event_observed"),
                            observation_start=fact_time(facts.get("observation_start")),
                            observation_end=fact_time(facts.get("observation_end"), lower=True),
                            evidence_ref=ref,
                            is_backfill=event_at is not None and event_at < public_time,
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    gaps.append("invalid_or_unknown_outcome_not_a_negative")
                continue
            if kind not in {"trial", "program"} or (unit_kind and kind != unit_kind):
                continue
            reasons = []
            identifier = facts.get("trial_id" if kind == "trial" else "program_id")
            if not isinstance(identifier, str) or not identifier:
                gaps.append("unit_identity_missing")
                continue
            lineage = facts.get("lineage_id") if kind == "program" else identifier
            if lineage is not None and not isinstance(lineage, str):
                lineage = None
            if any(
                facts.get(key) is not None and not isinstance(facts[key], str)
                for key in ("indication", "jurisdiction")
            ):
                gaps.append("invalid_unit_scope_not_guessed")
                continue
            if kind in {"program", "trial"}:
                review = record.get("mapping_review") or {}
                try:
                    claimed = fact_time(facts.get("mapping_available_at"))
                    reviewed_available = time_upper(review.get("available_at"))
                except (TypeError, ValueError):
                    claimed = reviewed_available = None
                mapped_lineage = facts.get("lineage_id")
                mapping_valid = (
                    bool(review.get("reviewed_by"))
                    and bool(review.get("basis"))
                    and claimed is not None
                    and reviewed_available is not None
                    and max(claimed, reviewed_available) <= cutoff
                    and isinstance(mapped_lineage, str)
                    and bool(mapped_lineage)
                    and bool(facts.get("program_id"))
                    and bool(facts.get("indication"))
                    and bool(facts.get("jurisdiction"))
                )
                if mapping_valid:
                    lineage = mapped_lineage
                elif kind == "program":
                    reasons.append("program_mapping_not_reviewed_or_not_available_at_cutoff")
                else:
                    gaps.append("trial_registry_identity_not_drug_program_lineage_isolation")
            status = facts.get("status")
            if kind == "trial" and status in {"COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"}:
                reasons.append("trial_not_at_risk_at_feature_cutoff")
            if kind == "program" and status in {
                "TERMINATED",
                "WITHDRAWN",
                "DISCONTINUED",
                "COMPLETED",
                "APPROVED",
            }:
                reasons.append("program_not_at_risk_at_feature_cutoff")
            active_statuses = {
                "ACTIVE",
                "RECRUITING",
                "NOT_YET_RECRUITING",
                "ENROLLING_BY_INVITATION",
                "ACTIVE_NOT_RECRUITING",
                "IN_DEVELOPMENT",
                "ON_HOLD",
            }
            terminal_statuses = {
                "COMPLETED",
                "TERMINATED",
                "WITHDRAWN",
                "SUSPENDED",
                "DISCONTINUED",
                "APPROVED",
            }
            if status not in active_statuses | terminal_statuses:
                reasons.append("risk_set_status_unknown")
            phase = facts.get("phase")
            if isinstance(phase, list):
                phase = "|".join(phase) if all(isinstance(p, str) for p in phase) else "unknown"
            else:
                phase = phase if isinstance(phase, str) and phase else "unknown"
            unit = HistoricalUnit(
                unit_id=identifier,
                unit_kind=kind,
                lineage_id=str(lineage or "unresolved:" + identifier),
                phase=phase,
                label=record["title"],
                indication=facts.get("indication"),
                jurisdiction=facts.get("jurisdiction"),
                evidence_ref=ref,
                eligible=not reasons,
                reasons=tuple(reasons),
            )
            key = (kind, identifier)
            if key in conflicted or (
                key in units
                and units[key].model_dump(exclude={"evidence_ref"})
                != unit.model_dump(exclude={"evidence_ref"})
            ):
                gaps.append("conflicting_unit_records_excluded")
                conflicted.add(key)
                units[key] = unit.model_copy(
                    update={
                        "eligible": False,
                        "reasons": ("conflicting_unit_records",),
                    }
                )
            else:
                units[key] = unit
        if not units:
            gaps.append("no_source_bound_trial_or_reviewed_program_units")
        gaps.extend(collection["coverage"].get("limitations", []))
        return HistoryObservation(
            cutoff=cutoff,
            source_id=collection["source_id"],
            is_demo=collection["is_demo"],
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            units=tuple(units[key] for key in sorted(units)),
            events=tuple(events),
            coverage_complete=collection["coverage"].get("complete") is True,
            scope_description=collection["coverage"].get("scope_description", "限定资料"),
            record_count=len(collection["items"]),
            gaps=tuple(sorted(set(gaps))) + tuple(sorted(collection["excluded_counts"])),
        )


def current_history_sources(settings):
    """Short current permission read, also used by legacy snapshot/material endpoints."""
    if settings.data_fabric_url is None or settings.data_fabric_history_read_token is None:
        raise DomainError("history_permission_unavailable", "来源当前许可未配置，正文未返回", 503)
    with httpx.Client(
        base_url=settings.data_fabric_url,
        trust_env=False,
        headers={"Authorization": "Bearer " + settings.data_fabric_history_read_token.get_secret_value()},
    ) as client:
        return FabricHistoryReader(
            client, authorization_ref="permission-check", max_seconds=3, max_bytes=1_000_000
        ).sources()
