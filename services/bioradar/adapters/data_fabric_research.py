"""Read bounded research queries from Data-Fabric; source parsing stays upstream.

The adopted text is the exact protocol JSON. Existing text mining excludes JSON;
this reader supplies evidence for investigation, not a cleaned prose corpus.
"""

import json
import re
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx

from bioradar.api.errors import DomainError
from bioradar.research.mining.models import (
    EngineRecord,
    Locator,
    NormalizedSnapshot,
    ProviderRef,
    SnapshotCoverage,
    canonical_hash,
)

SOURCE_SITE = "clinicaltrials"
GAPS = (
    "title_search_only_not_full_corpus_review",
    "source_current_record_not_immutable_version",
    "upstream_first_seen_and_availability_unknown",
    "study_independence_and_entity_alignment_unresolved",
    "registration_is_not_efficacy_or_safety_evaluation",
    "results_availability_not_checked",
)


def protocol_error():
    return DomainError("provider_protocol_error", "Data-Fabric 研究接口返回格式不一致", 502)


def document_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise protocol_error() from None
    return value


PROTOCOL_PATHS = {
    "nct_id": "identificationModule/nctId",
    "brief_title": "identificationModule/briefTitle",
    "brief_summary": "descriptionModule/briefSummary",
    "conditions": "conditionsModule/conditions",
    "interventions": "armsInterventionsModule/interventions",
    "phases": "designModule/phases",
    "overall_status": "statusModule/overallStatus",
    "why_stopped": "statusModule/whyStopped",
    "study_first_post_date": "statusModule/studyFirstPostDateStruct",
    "last_update_post_date": "statusModule/lastUpdatePostDateStruct",
    "start_date": "statusModule/startDateStruct",
    "primary_completion_date": "statusModule/primaryCompletionDateStruct",
    "completion_date": "statusModule/completionDateStruct",
}


def validate_document(result, identifier):
    """Check source claims against their literal evidence; never repair bad values."""
    try:
        projection = result["projection"]
        if (result["schema_version"] != "datafabric.research.document.v1"
                or result["uuid"] != identifier or result["source_site"] != SOURCE_SITE
                or not isinstance(result["title"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["document_sha256"])
                or projection["version"] != "clinicaltrials.protocol.v1"
                or projection["status"] not in {"available", "unavailable"}):
            raise protocol_error()
        if projection["status"] == "unavailable":
            if not isinstance(projection.get("reason"), str) or not projection["reason"]:
                raise protocol_error()
            return result
        locator, text = projection["locator"], projection["text"]
        protocol = json.loads(text)
        facts, paths = projection["facts"], projection["field_paths"]
        if (not isinstance(text, str) or not isinstance(protocol, dict)
                or locator["field"] != "raw_content" or locator["offset_unit"] != "unicode_codepoint"
                or type(locator["start"]) is not int or locator["start"] < 0
                or type(locator["end"]) is not int or locator["end"] - locator["start"] != len(text)
                or not isinstance(facts, dict) or not isinstance(paths, dict)
                or not re.fullmatch(r"NCT[0-9]{8}", facts["nct_id"])):
            raise protocol_error()
        for field, path in PROTOCOL_PATHS.items():
            if paths.get(field) != "/protocolSection/" + path:
                raise protocol_error()
            value = protocol
            for part in path.split("/"):
                if value is not None and not isinstance(value, dict):
                    raise protocol_error()
                value = value.get(part) if value is not None else None
            if value is not None and field == "interventions":
                if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                    raise protocol_error()
                value = [{"type": item.get("type"), "name": item.get("name")} for item in value]
            if value is not None and field.endswith("_date"):
                if not isinstance(value, dict):
                    raise protocol_error()
                value = {key: value.get(key) for key in ("date", "type")}
            if canonical_hash(facts.get(field)) != canonical_hash(value):
                raise protocol_error()
        return result
    except (KeyError, TypeError, ValueError, RecursionError):
        raise protocol_error() from None


class DataFabricResearchReader:
    def __init__(
        self, client: httpx.Client, *, source_instance: str,
        authorization_ref: str, allowed_source_sites: set[str],
        provider: str | None = None,
    ):
        if not source_instance or not authorization_ref or SOURCE_SITE not in allowed_source_sites:
            raise DomainError("source_not_authorized", "本次调查没有 ClinicalTrials 来源许可", 403)
        url = client.base_url
        if (url.scheme not in {"http", "https"} or url.username or url.password
                or url.path != "/" or url.query or url.fragment):
            raise DomainError("invalid_provider_configuration", "数据服务地址需由运行配置提供", 422)
        self.client = client
        self.source_instance = source_instance
        self.authorization_ref = authorization_ref
        self.provider = provider or "data_fabric:" + source_instance
        self.deadline = time.monotonic() + 120
        self.total_bytes = 0
        self.calls = 0

    def _get(self, path, params=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls >= 21:
            raise DomainError("provider_budget_exceeded", "本次读取已达时间或请求次数上限", 503)
        self.calls += 1
        chunks, size = [], 0
        try:
            with self.client.stream(
                "GET", path, params=params, follow_redirects=False, timeout=min(15, remaining),
            ) as response:
                if response.status_code == 404:
                    raise DomainError("provider_not_found", "来源记录或研究接口不可用", 404)
                if response.status_code != 200:
                    raise DomainError("provider_unavailable", "Data-Fabric 未完成本次读取", 503)
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    self.total_bytes += len(chunk)
                    if size > 3_000_000 or self.total_bytes > 8_000_000:
                        raise DomainError("provider_response_too_large", "资料超过本次读取大小上限", 502)
                    if time.monotonic() > self.deadline:
                        raise DomainError("provider_timeout", "资料读取超过本次时间上限", 503)
                    chunks.append(chunk)
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict):
                raise protocol_error()
            return value
        except httpx.TimeoutException:
            raise DomainError("provider_timeout", "Data-Fabric 读取超时", 503) from None
        except httpx.HTTPError:
            raise DomainError("provider_unavailable", "无法读取 Data-Fabric", 503) from None
        except (ValueError, TypeError):
            raise protocol_error() from None

    def search(self, terms, limit=6):
        if (not terms or len(terms) > 5 or type(limit) is not int or not 1 <= limit <= 6
                or any(not isinstance(term, str) or not 2 <= len(term.strip()) <= 120 for term in terms)):
            raise DomainError("invalid_research_query", "请提供一至五个长度受限的明确检索词", 422)
        terms = tuple(term.strip() for term in terms)
        result = self._get(
            "/api/research/documents/search", [("terms", term) for term in terms] + [("limit", limit)],
        )
        try:
            if (result["schema_version"] != "datafabric.research.search.v1"
                    or result["source_site"] != SOURCE_SITE
                    or result["scope"] != "title_literal_and_current_db"
                    or result["terms"] != list(terms) or result["limit"] != limit
                    or type(result["has_more"]) is not bool
                    or not isinstance(result["items"], list) or len(result["items"]) > limit):
                raise protocol_error()
            seen = set()
            for item in result["items"]:
                identifier = document_uuid(item["uuid"])
                if (identifier in seen or item["source_site"] != SOURCE_SITE
                        or not isinstance(item["title"], str)):
                    raise protocol_error()
                seen.add(identifier)
            return result
        except (KeyError, TypeError):
            raise protocol_error() from None

    def read_document(self, identifier):
        identifier = document_uuid(identifier)
        return validate_document(self._get("/api/research/documents/" + identifier), identifier)

    def snapshot_from_documents(self, documents):
        if not documents or len(documents) > 18:
            raise DomainError("invalid_document_manifest", "本次采用需要一至十八条已读取资料", 422)
        observed = datetime.now(UTC)
        records = []
        for document in documents:
            validate_document(document, document_uuid(document.get("uuid")))
            projection = document["projection"]
            if projection["status"] != "available":
                raise DomainError("provider_projection_unavailable", "未取得可定位原文的记录不能采用", 422)
            version = canonical_hash({
                "document_sha256": document["document_sha256"], "projection": projection,
                "source_url": document.get("source_url"), "title": document["title"],
            })
            ref = ProviderRef(
                provider=self.provider, object_type="document",
                object_id=document["uuid"], adopted_version=version, version_source="export_hash",
                content_hash=version, authorization_ref=self.authorization_ref,
            )
            records.append(EngineRecord(
                record_ref=ref, evidence_refs=(ref,), document_hash=document["document_sha256"],
                source_instance=self.source_instance, source_id=SOURCE_SITE,
                source_url=document.get("source_url"), observed_at=observed,
                published_time_raw=document.get("publish_date_raw"), publication_time_verified=False,
                text_status="inline_authorized", normalized_text=projection["text"],
                normalization_version="data-fabric.clinicaltrials.protocol.literal.v1",
                locator=Locator(type="offset", start=projection["locator"]["start"],
                                end=projection["locator"]["end"]),
                missing_fields=GAPS, available_fields=("protocol_json", "source_locator"),
                upstream_status={"projection_version": projection["version"],
                                 "nct_id": projection["facts"]["nct_id"]},
            ))
        return NormalizedSnapshot(
            snapshot_id="digging-" + uuid4().hex[:24], source_instance=self.source_instance,
            is_demo=False, coverage_mode="bounded_sample", observed_at=observed,
            records=tuple(records), metadata_gaps=GAPS,
            coverage=(SnapshotCoverage(
                source_instance=self.source_instance, source_id=SOURCE_SITE, mode="bounded_sample",
                scope_description="本次标题检索命中的指定记录；非完整专题、非增量流。",
                record_count=len(records), is_consistent_snapshot=False, reasons=GAPS,
            ),),
        )
