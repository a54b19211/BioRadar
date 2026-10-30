"""Fabric's explicit document-evidence contract; no inferred primary or source confirmation."""

import copy
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bioradar.api.errors import DomainError
from bioradar.research.mining.models import SignalContext, SourceRelation


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _SourceReference(_Model):
    source_id: str = Field(min_length=1, max_length=80)
    source_record_id: str = Field(min_length=1, max_length=400)
    version_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class _Locator(_Model):
    json_pointer: str | None = Field(default=None, pattern=r"^/")
    xpath: str | None = Field(default=None, min_length=1, max_length=2000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    quote: str | None = Field(default=None, min_length=1, max_length=10_000)


class _Relation(_Model):
    relation_type: Literal["corrects", "retracts", "supplements", "reposts"]
    target_source_id: str = Field(min_length=1, max_length=80)
    target_source_record_id: str = Field(min_length=1, max_length=400)
    target_version_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    basis_locator: _Locator | None = None


class _Independence(_Model):
    shared_identifiers: list[str] = Field(default_factory=list, max_length=20)
    status: Literal["unknown", "same_origin"] = "unknown"
    origin_group_id: str | None = Field(default=None, min_length=1, max_length=500)
    original_source: _SourceReference | None = None
    basis: str | None = Field(default=None, min_length=1, max_length=2000)


class _Time(_Model):
    value: str = Field(min_length=4, max_length=40)
    precision: Literal["year", "month", "day", "instant"]
    meaning: str = Field(default="source_reported", max_length=80)


class _InformationEvent(_Model):
    event_type: Literal["reported_deletion", "official_denial", "moderation", "access_failure"]
    description: str = Field(min_length=1, max_length=2000)
    reported_at: _Time | None = None
    detected_at: str | None = None
    target: _SourceReference | None = None
    basis_locator: _Locator

    @field_validator("detected_at")
    @classmethod
    def observed_clock(cls, value):
        if value is not None and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("event detection requires an actual timezone")
        return value


class _DocumentEvidence(_Model):
    schema_version: Literal["datafabric.document.evidence.v1"] = "datafabric.document.evidence.v1"
    material_kind: (
        Literal[
            "primary_research",
            "review",
            "trial_registry",
            "press_release",
            "corporate_disclosure",
            "official_notice",
            "news_report",
            "social_media",
            "repost",
            "conference_abstract",
            "graph",
        ]
        | None
    ) = None
    text_level: Literal["full_text", "abstract", "title", "excerpt", "metadata"] | None = None
    document_relations: list[_Relation] = Field(default_factory=list, max_length=100)
    source_independence: _Independence = Field(default_factory=_Independence)
    statement_status: Literal["source_reported", "unverified_report"] = "source_reported"
    information_events: list[_InformationEvent] = Field(default_factory=list, max_length=50)


def _check_locator(locator, original, clean):
    from bioradar.adapters.fabric_history import json_pointer

    if locator is None:
        return
    pointer = locator.json_pointer
    if pointer is not None:
        json_pointer(original, pointer)
    if locator.xpath is not None:
        from xml.etree import ElementTree

        if not isinstance(original, ElementTree.Element):
            raise ValueError("XML locator used for a non-XML original")
        path = locator.xpath
        if path.startswith("/"):
            prefix = "/" + original.tag
            if not path.startswith(prefix + "/") and path != prefix:
                raise ValueError("XML root differs from declared locator")
            path = "." + path[len(prefix) :]
        if not original.findall(path):
            raise ValueError("XML locator does not resolve in the fixed original")
    if locator.start is not None or locator.end is not None:
        if (
            locator.start is None
            or locator.end is None
            or locator.quote is None
            or not 0 <= locator.start < locator.end <= len(clean)
            or clean[locator.start : locator.end] != locator.quote
        ):
            raise ValueError("document quote is not the exact clean-text Unicode window")
    elif locator.quote is not None:
        if clean.count(locator.quote) != 1:
            raise ValueError("document quote does not have one exact clean-text location")
    elif pointer is None and locator.xpath is None:
        raise ValueError("document provenance requires a source location")


def validate_document_evidence(record):
    value = record.get("document_evidence")
    if value is None:
        return None
    try:
        model = _DocumentEvidence.model_validate(value)
        if record["raw_media_type"] == "application/json":
            raw = json.loads(record["raw_content"])
        elif record["raw_media_type"] == "application/xml":
            from xml.etree import ElementTree

            raw = ElementTree.fromstring(record["raw_content"])
        else:
            raw = None
        for item in [*model.document_relations, *model.information_events]:
            _check_locator(item.basis_locator, raw, record["clean_text"])
        if (
            record["source_id"] == "external_archive"
            and model.text_level == "full_text"
            and isinstance(raw, dict)
            and isinstance(raw.get("full_text"), str)
            and raw["full_text"] not in record["clean_text"]
        ):
            raise ValueError("full archive text was omitted from its declared full-text projection")
    except (ValidationError, ValueError, TypeError, KeyError, IndexError, SyntaxError):
        raise DomainError(
            "history_document_invalid", "文档材料类型、关系或原文字符位置不符合来源契约", 502
        ) from None
    return model.model_dump(mode="json")


def source_context(record):
    value = record.get("document_evidence")
    return copy.deepcopy(value) if value is not None else None


def project_signal_context(reader, record, *, known_records=()):
    value = record.get("document_evidence")
    if value is None:
        return None
    known = {(r["source_id"], r["source_record_id"], r["version_id"]): reader.ref(r) for r in known_records}
    relations = []
    kinds = {
        "corrects": "correction",
        "retracts": "retraction",
        "supplements": "supplement",
        "reposts": "repost",
    }
    for relation in value.get("document_relations", []):
        key = (
            relation["target_source_id"],
            relation["target_source_record_id"],
            relation.get("target_version_id"),
        )
        if key in known:
            relations.append(SourceRelation(kind=kinds[relation["relation_type"]], target_ref=known[key]))
    independence = value.get("source_independence") or {}
    return SignalContext(
        material_type=value.get("material_kind"),
        origin_group=independence.get("origin_group_id"),
        source_relations=tuple(relations),
        statement_status=value.get("statement_status", "source_reported"),
        verification_status="unverified"
        if value.get("statement_status") == "unverified_report"
        or value.get("material_kind") in {"social_media", "repost"}
        else "unknown",
        document_evidence=copy.deepcopy(value),
        evidence_refs=(reader.ref(record),),
    )


def context_at(value, *, mode="current", cutoff=None, text=""):
    """A retrospective dataset censorship label is not contemporaneous source content."""
    context = copy.deepcopy(value)
    if context is None or mode == "current":
        return context
    from bioradar.adapters.fabric_history import time_upper

    limit = time_upper(cutoff)
    events = []
    for event in context.get("information_events", []):
        locator = event.get("basis_locator") or {}
        quoted = locator.get("quote")
        when = time_upper(event.get("reported_at"))
        detected = time_upper(event.get("detected_at"))
        if (
            isinstance(quoted, str)
            and quoted
            and quoted in text
            and (when is None or limit is not None and when <= limit)
            and (detected is None or limit is not None and detected <= limit)
        ):
            events.append(event)
    context["information_events"] = events
    return context
