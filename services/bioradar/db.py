"""PostgreSQL tables. Evidence text is stored once in `evidence`; model calls once in `llm_calls`."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from bioradar.config import settings

Json = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Investigation(Base):
    __tablename__ = "investigations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(100))
    question: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(30))  # pipeline | science | business | public_health
    is_demo: Mapped[bool] = mapped_column(Boolean)
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"))
    trigger: Mapped[str] = mapped_column(String(30))  # manual | followup | topic | material_update | replay
    method: Mapped[str] = mapped_column(String(30))  # planner | baseline
    state: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | completed | failed
    stage: Mapped[str] = mapped_column(String(30), default="plan")
    input: Mapped[dict] = mapped_column(Json)
    input_hash: Mapped[str] = mapped_column(String(64))
    progress: Mapped[dict] = mapped_column(Json, default=dict)
    result: Mapped[dict | None] = mapped_column(Json)
    error_code: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("investigation_id", "input_hash"),)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    ref: Mapped[str] = mapped_column(String(10))  # E1, E2 ... assigned by the radar
    source: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(400))
    version_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(String(40))
    available_at: Mapped[str | None] = mapped_column(String(40))
    material_kind: Mapped[str | None] = mapped_column(String(40))
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(Json, default=dict)  # url, relations, origin_group, events, text_level
    __table_args__ = (UniqueConstraint("analysis_id", "ref"),)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), index=True)
    stage: Mapped[str] = mapped_column(String(60))
    service: Mapped[str] = mapped_column(String(30))  # radar | biocheck | mechevidence
    model: Mapped[str | None] = mapped_column(String(100))
    finish_reason: Mapped[str | None] = mapped_column(String(30))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    seconds: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(String(100))
    requests: Mapped[int] = mapped_column(Integer, default=1)  # HTTP requests behind it (format retry = 2)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MaterialRead(Base):
    """One record's text as sent to a model or a service (v3 6.1): stage, receiver, the material's sha256 and
    its binding — every segment's part (fixed version, raw hash, processor or parser version, renderer),
    range, hash and Fabric locators, or the literal radar text (bioradar.material). Append-only: each attempt
    is its own row (attempt 1, 2 ... per analysis, stage and ref), never replaced, with its status: prepared
    (recorded before the call), sent (the model call was made or the service answered), not_sent (no call:
    budget) or failed (the call raised). A prepared row left behind by a worker that died says the material
    may have gone out. The text itself is not stored again: reread() rebuilds it from the parts."""

    __tablename__ = "material_reads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), index=True)
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id"), index=True)  # triage batches
    stage: Mapped[str] = mapped_column(String(30))  # triage | read | report | mechanism
    service: Mapped[str] = mapped_column(String(30))  # radar | biocheck | mechevidence
    ref: Mapped[str] = mapped_column(String(20))  # E1 ... in an analysis, R1 ... in a triage batch
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="prepared")  # prepared | sent | not_sent | failed
    source: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(400))
    version_id: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    chars: Mapped[int] = mapped_column(Integer)
    binding: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def material_read(material, *, stage, service, ref, source, record_id, version_id, **links) -> MaterialRead:
    """The row for one outbound material (a bioradar.material.Material); links: analysis_id or llm_call_id."""
    binding = material.binding()
    return MaterialRead(
        stage=stage,
        service=service,
        ref=ref,
        source=source,
        record_id=record_id,
        version_id=version_id,
        sha256=binding["sha256"],
        chars=binding["chars"],
        binding=binding,
        **links,
    )


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(30))
    is_demo: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(
        String(20), default="emerging"
    )  # emerging | rising | cooling | dormant
    alert: Mapped[str] = mapped_column(String(20), default="normal")  # normal | important | urgent
    heat: Mapped[float] = mapped_column(Float, default=0)
    awakenings: Mapped[int] = mapped_column(Integer, default=0)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)  # a closure notice was seen
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_analysis_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(Text, default="")
    # An automatic analysis that is owed but not created yet (hourly cap, or the round died first).
    pending_reason: Mapped[str | None] = mapped_column(String(200))
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Observation(Base):
    """One new record seen by the monitor, with the triage result."""

    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(400))
    version_id: Mapped[str] = mapped_column(String(128), unique=True)
    is_demo: Mapped[bool] = mapped_column(Boolean)
    title: Mapped[str] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"), index=True)
    alert: Mapped[str] = mapped_column(String(20))  # none | normal | important | urgent
    closure: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    # what daily activity counts once (T-15): a repost's original, else the same-origin group, else itself
    activity_key: Mapped[str | None] = mapped_column(String(700))


class RecordCorrection(Base):
    """A correction or retraction Fabric states for a record (T-24, Codex r166): recorded when the correcting
    record is seen, apart from whether any replacement value passes checking. From visible_at on, the target
    record's points (of the named version, when Fabric names one) are withdrawn from comparison; a reader
    with an earlier cutoff does not see it."""

    __tablename__ = "record_corrections"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "is_demo",
            "target_source",
            "target_record_id",
            "target_version_id",
            "by_version_id",
        ),
        Index("ix_record_corrections_target", "workspace_id", "is_demo", "target_source", "target_record_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(100))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    target_source: Mapped[str] = mapped_column(String(80))
    target_record_id: Mapped[str] = mapped_column(String(400))
    target_version_id: Mapped[str] = mapped_column(String(128), default="")  # "" = every version
    relation: Mapped[str] = mapped_column(String(20))  # corrects | retracts
    by_source: Mapped[str] = mapped_column(String(80))
    by_record_id: Mapped[str] = mapped_column(String(400))
    by_version_id: Mapped[str] = mapped_column(String(128))
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Measurement(Base):
    """One quoted value of one metric of one object for one period, from one fixed record version (T-24).
    Series are read with available_at <= the clock, so a replay never sees a later value."""

    __tablename__ = "measurements"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "object",
            "metric",
            "unit",
            "kind",
            "scope",
            "scope_mode",
            "period",
            "period_type",
            "period_precision",
            "family",
            "dims_key",
            "value_text",
            name="measurements_version_series_period_domain_value_key",
        ),
        Index("ix_measurements_domain", "workspace_id", "is_demo", "domain_basis", "domain_id"),
        Index(
            "ix_measurements_series",
            "workspace_id",
            "is_demo",
            "object",
            "metric",
            "unit",
            "kind",
            "scope",
            "scope_mode",
            "period_type",
            "period_precision",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(100))
    is_demo: Mapped[bool] = mapped_column(Boolean)
    object: Mapped[str] = mapped_column(String(200), index=True)
    metric: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(50), default="")
    # stock (level at a date) | flow (added in the period) | cumulative | rate | other: never compared across
    kind: Mapped[str] = mapped_column(String(20), default="other")
    scope: Mapped[str] = mapped_column(String(200), default="")  # coverage / population, e.g. 5个哨点
    # the domain the point belongs to (T-24): only points of one known domain are one series. basis "topic":
    # domain_id is the topic the record was placed in; "unknown": nothing is computed from the point
    domain_basis: Mapped[str] = mapped_column(String(20), default="unknown")
    domain_id: Mapped[str | None] = mapped_column(String(64))
    # the complete domain (T-24 r2): the metric family, each required dimension with its state and proof, the
    # group key they make, and the gaps that keep the point out of automatic comparison (empty = comparable)
    family: Mapped[str] = mapped_column(String(30), default="")
    dims: Mapped[list] = mapped_column(Json, default=list)
    dims_key: Mapped[str] = mapped_column(String(400), default="")
    dims_reasons: Mapped[list] = mapped_column(Json, default=lambda: ["family_not_determined"])
    value: Mapped[float] = mapped_column(Float)
    numerator: Mapped[float | None] = mapped_column(Float)  # a rate's parts, when the quote states them
    denominator: Mapped[float | None] = mapped_column(Float)
    period: Mapped[str] = mapped_column(String(40), default="")  # as written; "" = as of the record
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="other")
    source: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(400))
    version_id: Mapped[str] = mapped_column(String(128))
    quote: Mapped[str] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when it could be known
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )  # when the radar got it
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"), index=True)
    # set when a correction of this record gives the same object/metric/period again
    superseded_by: Mapped[int | None] = mapped_column(Integer)
    # T-29 P3: the candidate and the check it was adopted from (NULL: a point from before checking, never in
    # the effective view); the exact checked numbers as decimal text (value/denominator floats are only
    # written when they read back equal); the period's type and precision and the scope mode, all series keys
    candidate_id: Mapped[int | None] = mapped_column(Integer, index=True)
    check_id: Mapped[int | None] = mapped_column(Integer, index=True)
    value_text: Mapped[str | None] = mapped_column(String(40))
    denominator_text: Mapped[str | None] = mapped_column(String(40))
    period_type: Mapped[str] = mapped_column(String(10), default="")
    period_precision: Mapped[str] = mapped_column(String(10), default="")
    scope_mode: Mapped[str] = mapped_column(String(30), default="")


class MeasurementCandidate(Base):
    """One measure as triage gave it (T-29 P3): the literal texts that locate it, the final values to check,
    the
    fixed artifact it was read from (from the material binding, re-read from Fabric before any check). Only a
    consistent check whose stored values read back equal makes it a series point; otherwise it stays here as a
    lead (not_built: it could not be sent as stated, with the reason)."""

    __tablename__ = "measurement_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(100))
    is_demo: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(400))
    version_id: Mapped[str] = mapped_column(String(128), index=True)
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    processor_version: Mapped[str | None] = mapped_column(String(120))
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    quote_start: Mapped[int | None] = mapped_column(Integer)
    quote_end: Mapped[int | None] = mapped_column(Integer)
    measure: Mapped[dict] = mapped_column(Json)  # the triage output as given (literal texts + final forms)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | not_built | checked
    reason: Mapped[str | None] = mapped_column(String(120))
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"), index=True)
    direction: Mapped[str | None] = mapped_column(
        String(30)
    )  # triage's direction, for a topic a series opens
    # the material_read of the window this candidate was read from (and so its model call and attempt): the
    # sources a measure cites are looked up in that send only, never in another send of the same text (B r2)
    material_read_id: Mapped[int | None] = mapped_column(ForeignKey("material_reads.id"), index=True)
    corrects: Mapped[list | None] = mapped_column(
        Json
    )  # [(source, record_id, version_id)] the record corrects
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the record's availability
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MeasurementCheckTask(Base):
    """The check owed to a candidate by one checker version (an upgrade owes a new one), leased."""

    __tablename__ = "measurement_check_tasks"
    candidate_id: Mapped[int] = mapped_column(ForeignKey("measurement_candidates.id"), primary_key=True)
    checker_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")  # queued | waiting | done | blocked
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(Json)


class RecordRead(Base):
    """T-31: the reading plan of one fixed version's cleaned artifact: its windows, how far it was read
    and why it stopped. state: reading | complete | capped | paused_unreadable | failed | not_planned
    (reason says why: a composite material or not a cleaned artifact - then only the triage window was
    read)."""

    __tablename__ = "record_reads"
    __table_args__ = (UniqueConstraint("workspace_id", "is_demo", "version_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(100))
    is_demo: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(400))
    version_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    part: Mapped[dict] = mapped_column(Json)  # the artifact's identity (bioradar.material part, no text hash)
    processor_version: Mapped[str | None] = mapped_column(String(120))
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    total_chars: Mapped[int] = mapped_column(Integer)
    window_chars: Mapped[int] = mapped_column(Integer)
    overlap_chars: Mapped[int] = mapped_column(Integer)
    windows_total: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(30), default="reading")
    reason: Mapped[str | None] = mapped_column(String(120))
    read_until: Mapped[int] = mapped_column(Integer, default=0)
    topic_id: Mapped[str | None] = mapped_column(String(36))
    direction: Mapped[str] = mapped_column(String(30), default="public_health")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    relations: Mapped[list] = mapped_column(Json, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RecordReadWindow(Base):
    """One window of a plan, leased. state: sent | pending | failed | failed_final | budget_spent |
    source_unreadable | skipped_no_digit | capped."""

    __tablename__ = "record_read_windows"
    read_id: Mapped[int] = mapped_column(ForeignKey("record_reads.id"), primary_key=True)
    window_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecordReadAttempt(Base):
    """Append-only: what happened to a window each time it was handled (its material_read and model call)."""

    __tablename__ = "record_read_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    read_id: Mapped[int] = mapped_column(ForeignKey("record_reads.id"), index=True)
    window_index: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(60))
    material_read_id: Mapped[int | None] = mapped_column(Integer)
    llm_call_id: Mapped[int | None] = mapped_column(Integer)
    measures_found: Mapped[int | None] = mapped_column(Integer)
    measures_enqueued: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MeasurementCheck(Base):
    """One check of one candidate, append-only: the full request and response, the checker, the outcome and
    the gate's decision and reason (series only when consistent and the stored values read back equal)."""

    __tablename__ = "measurement_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("measurement_candidates.id"), index=True)
    checker_version: Mapped[str] = mapped_column(String(80))
    contract_version: Mapped[str] = mapped_column(String(40))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    request: Mapped[dict | None] = mapped_column(Json)
    response: Mapped[dict | None] = mapped_column(Json)
    status: Mapped[str] = mapped_column(String(20))  # consistent | inconsistent | unchecked
    error_code: Mapped[str | None] = mapped_column(String(60))
    gate: Mapped[str] = mapped_column(String(20))  # series | candidate
    gate_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TopicEvent(Base):
    __tablename__ = "topic_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # source clock, not wall clock
    kind: Mapped[str] = mapped_column(String(30))  # created | observed | state | analysis
    detail: Mapped[dict] = mapped_column(Json, default=dict)


class Cursor(Base):
    __tablename__ = "monitor_cursors"
    source: Mapped[str] = mapped_column(String(80), primary_key=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    after_seq: Mapped[int] = mapped_column(Integer, default=0)
    # when a live round last found new records in this feed (source status page)
    last_new_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(Base):
    """Who may sign in: viewer reads; analyst asks and reviews; admin also manages users."""

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    role: Mapped[str] = mapped_column(String(20))  # viewer | analyst | admin
    credential_hash: Mapped[str] = mapped_column(String(200))  # scrypt$salt$hash
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # in every session token; a new credential or deactivation bumps it, so older sessions stop working
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Review(Base):
    """A person's decision on a machine result; the table is itself the audit record of human actions."""

    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))  # confirm | reject | flag_source | set_alert
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), index=True)
    sentence: Mapped[int | None] = mapped_column(Integer)  # BioCheck's sentence number
    evidence_ref: Mapped[str | None] = mapped_column(String(10))
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"), index=True)
    alert: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True)


@lru_cache
def _sessions():
    return sessionmaker(engine(), expire_on_commit=False)


@contextmanager
def session():
    with _sessions()() as s, s.begin():
        yield s
