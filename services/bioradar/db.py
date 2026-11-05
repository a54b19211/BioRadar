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
    """One record's text as actually sent to a model or a service (v3 6.1): stage, receiver, the material's
    sha256 and its binding — every segment's part (fixed version, processor or parser version, renderer),
    range and hash, or the literal radar text (bioradar.material). The text itself is not stored again: the
    evidence row keeps it, and reread() rebuilds it from the parts."""

    __tablename__ = "material_reads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), index=True)
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id"), index=True)  # triage batches
    stage: Mapped[str] = mapped_column(String(30))  # triage | read | report | mechanism
    service: Mapped[str] = mapped_column(String(30))  # radar | biocheck | mechevidence
    ref: Mapped[str] = mapped_column(String(20))  # E1 ... in an analysis, R1 ... in a triage batch
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


class Measurement(Base):
    """One quoted value of one metric of one object for one period, from one fixed record version (T-24).
    Series are read with available_at <= the clock, so a replay never sees a later value."""

    __tablename__ = "measurements"
    __table_args__ = (
        UniqueConstraint("version_id", "object", "metric", "unit", "kind", "scope", "period"),
        Index(
            "ix_measurements_series", "workspace_id", "is_demo", "object", "metric", "unit", "kind", "scope"
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
