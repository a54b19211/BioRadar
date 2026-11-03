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
