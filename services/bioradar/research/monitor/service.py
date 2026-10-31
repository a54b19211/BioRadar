"""Bounded polling coordinator; mining remains in existing RQ runs."""

import copy
import json
import secrets
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bioradar.adapters.fabric_monitor import (
    build_snapshot,
    change_page,
    followup_plan,
    project_record,
    search_records,
    select_history_versions,
)
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.models import EngineIdempotency, EngineRun
from bioradar.research.engine.schemas import RunAction, RunCreate
from bioradar.research.engine.service import SOURCE_READ_UNAVAILABLE
from bioradar.research.history.facts import summarize_records
from bioradar.research.investigations.models import (
    InvestigationAutomation,
    InvestigationCreate,
    InvestigationEntryCreate,
    InvestigationOriginCreate,
)
from bioradar.research.investigations.service import (
    InvestigationEntryRecord,
    InvestigationRecord,
    InvestigationService,
)
from bioradar.research.mining.intelligence_assessment import (
    default_assessment_axes,
    project_reviewed_assessment_axes,
)
from bioradar.research.mining.models import (
    Candidate,
    DiscoveryPolicy,
    EngineRecord,
    KddPolicy,
    MiningPolicy,
    NormalizedSnapshot,
    ProviderRef,
    ResourceLimits,
    canonical_hash,
)
from bioradar.research.monitor.models import (
    MONITOR_VERSION,
    MonitorBatch,
    MonitorBatchList,
    MonitorBatchView,
    MonitorControl,
    MonitorLead,
    MonitorLeadList,
    MonitorLeadView,
    MonitorPublicationStatus,
    MonitorSourceStatus,
    MonitorState,
    MonitorStatus,
)

LIMITATIONS = [
    "bounded_feature_window_not_complete_source_or_global_research_coverage",
    "adoption_or_source_revision_is_not_a_new_clinical_event",
    "unresolved_identity_and_independence_do_not_become_confirmed_growth",
    "automatic_context_is_not_a_professional_judgment",
]
ACTIVE = ("accepted", "queued", "investigating")


def identity(record):
    return json.dumps(
        [record.source_instance, *record.record_ref.identity_key], ensure_ascii=False, separators=(",", ":")
    )


def fingerprint(record):
    """Do not create progress merely because adoption time or mapping version changed."""
    value = {
        "text": record.normalized_text_hash,
        "states": sorted((a.property, a.value) for a in record.state_assertions),
        "flags": sorted(set(record.record_flags) & {"deleted", "retracted"}),
        "stage": record.project_stage,
        "relations": sorted(
            (r.relation_type, r.subject_ref.object_id, r.object_ref.object_id) for r in record.relations
        ),
    }
    if record.graph_hints:
        value["graph_hints"] = [
            hint.model_dump(mode="json", exclude={"provenance", "hint_ref"}) for hint in record.graph_hints
        ]
    if record.metric_observations:
        value["metric_observations"] = [
            metric.model_dump(mode="json", exclude={"provenance", "observation_id"})
            for metric in record.metric_observations
        ]
    return canonical_hash(value)


def fair_window(progress, limit):
    pools, total, owners = {}, 0, {}
    for source, value in sorted(progress.items()):
        records = [EngineRecord.model_validate(r) for r in value.get("context", [])]
        for record in records:
            if record.source_id == value.get("source_id"):
                owners[record.record_ref.version_key] = record
        records.sort(key=lambda r: (r.observed_at, r.record_ref.version_key), reverse=True)
        pools[source] = deque(records)
        total += len(records)
    withdrawn = {
        record.record_ref.identity_key
        for record in owners.values()
        if set(record.record_flags) & {"deleted", "retracted"}
    }
    selected, seen = [], set()
    while any(pools.values()) and len(selected) < limit:
        for source in sorted(pools):
            if not pools[source] or len(selected) >= limit:
                continue
            record = pools[source].popleft()
            record = owners.get(record.record_ref.version_key, record)
            if record.record_ref.version_key in seen:
                continue
            seen.add(record.record_ref.version_key)
            # Preserve old versions in their fixed snapshots. A current graph
            # must not keep qualifying relations through withdrawn evidence.
            if record.record_ref.identity_key in withdrawn and not (
                set(record.record_flags) & {"deleted", "retracted"}
            ):
                continue
            if record.relations and any(
                ref.identity_key in withdrawn
                for relation in record.relations
                for ref in relation.evidence_refs
            ):
                continue
            selected.append(record)
    return selected, max(0, total - len(selected))


def byte_window(records, byte_limit):
    """Keep complete records; source versions remain available outside this calculation window."""
    selected, used, omitted = [], 0, 0
    for record in records:
        size = len(record.model_dump_json().encode())
        if used + size > byte_limit:
            omitted += 1
            continue
        selected.append(record)
        used += size
    return selected, omitted


def evidence_row(reader, record):
    return {
        **{
            key: record[key]
            for key in (
                "source_id",
                "source_record_id",
                "version_id",
                "raw_sha256",
                "record_kind",
                "is_demo",
                "facts",
                "field_locators",
            )
        },
        "ref_key": list(reader.ref(record).version_key),
    }


class MonitorService:
    def __init__(self, engine, reader=None):
        self.engine, self.settings, self.reader = engine, engine.settings, reader
        self.actor = DevelopmentAccess(self.settings).actor()
        self.token = None
        self.online_ids = None
        self.allow_paused = False

    def stream_key(self, source_id):
        return json.dumps([source_id, self.settings.monitor_is_demo], separators=(",", ":"))

    def _actor(self, actor):
        if actor != self.actor:
            raise DomainError("permission_denied", "开发授权上下文不匹配", 403)

    def _state(self, session, *, lock=False):
        session.execute(
            insert(MonitorState)
            .values(
                workspace_id=self.actor.workspace_id,
                paused=False,
                control_revision=1,
                source_progress={},
                next_source=0,
            )
            .on_conflict_do_nothing(index_elements=["workspace_id"])
        )
        statement = select(MonitorState).where(MonitorState.workspace_id == self.actor.workspace_id)
        return session.scalar(statement.with_for_update() if lock else statement)

    def _catalog(self):
        if self.online_ids is None:
            rows = self.reader.sources() if self.reader is not None else []
            self.online_ids = {
                r["source_id"]
                for r in rows
                if isinstance(r, dict)
                and r.get("read_enabled") is True
                and isinstance(r.get("source_id"), str)
            }
            self.engine._history_source_ids = set(self.online_ids)
        return self.online_ids

    def permitted(self, *, online=True):
        instance = (
            (self.settings.data_fabric_demo_source_instance or "fixture")
            if self.settings.monitor_is_demo
            else self.settings.data_fabric_source_instance
        )
        if instance not in self.actor.source_allowlist:
            return set()
        local = set(self.settings.engine_source_id_allowlist.get(instance, []))
        return local & self._catalog() if online else local

    def _guard(self, row):
        if self.token is None or row.lease_token != self.token or row.paused and not self.allow_paused:
            raise DomainError("monitor_lease_lost", "后台已暂停或执行资格已变更", 409)

    def claim(self):
        with Session(self.engine.database) as session, session.begin():
            row = self._state(session, lock=True)
            now = session.scalar(select(func.now()))
            if (row.paused and not self.allow_paused) or row.lease_until and row.lease_until > now:
                return False
            self.token = secrets.token_hex(32)
            row.lease_token, row.lease_until = self.token, now + timedelta(seconds=180)
            row.heartbeat_at = now
            return True

    def release(self, error_code=None):
        with Session(self.engine.database) as session, session.begin():
            row = self._state(session, lock=True)
            if row.lease_token == self.token:
                row.lease_until, row.heartbeat_at, row.error_code = None, datetime.now(UTC), error_code

    def control(self, actor, body: MonitorControl, key):
        self._actor(actor)
        with Session(self.engine.database) as session, session.begin():

            def mutate():
                row = self._state(session, lock=True)
                if row.control_revision != body.expected_revision:
                    raise DomainError("revision_conflict", "监测开关已变化，请刷新后操作", 409)
                row.paused = body.paused
                row.control_revision += 1
                return 200, {"paused": row.paused, "control_revision": row.control_revision}

            return self.engine._idempotent(session, actor, "monitor.control", key, body, mutate)

    def status(self, actor):
        self._actor(actor)
        allowed = self.permitted()
        publication = []
        for source in sorted(allowed & {"legacy_documents", "reviewed_graph"}):
            try:
                value = self.reader._get(
                    "/api/history/publication/status", {"source_id": source}, request_seconds=3
                )
                if (
                    value.get("schema_version") != "datafabric.publication.status.v1"
                    or value.get("source_id") != source
                ):
                    raise ValueError("invalid publication metadata")
                counts = value["counts"]
                if not isinstance(counts, dict) or any(type(v) is not int or v < 0 for v in counts.values()):
                    raise ValueError("invalid publication counters")
                publication.append(
                    MonitorPublicationStatus(
                        source_id=source,
                        publisher_state=value["publisher_state"],
                        last_heartbeat=value.get("last_heartbeat"),
                        counts=counts,
                        disk_paused=value["disk"]["paused"],
                    )
                )
            except (DomainError, KeyError, TypeError, ValueError):
                publication.append(
                    MonitorPublicationStatus(
                        source_id=source,
                        publisher_state="unavailable",
                        error_code="publication_status_unavailable",
                    )
                )
        with Session(self.engine.database) as session, session.begin():
            row = self._state(session)
            sources = [
                MonitorSourceStatus(
                    source_id=value["source_id"],
                    is_demo=value["is_demo"],
                    **{
                        k: v
                        for k, v in value.items()
                        if k in MonitorSourceStatus.model_fields
                        and k not in {"source_id", "is_demo", "retained_records"}
                    },
                    retained_records=len(value.get("context", [])),
                )
                for source, value in sorted(row.source_progress.items())
                if value.get("source_id") in allowed and value.get("is_demo") is self.settings.monitor_is_demo
            ]
            batches = session.scalar(
                select(func.count())
                .select_from(MonitorBatch)
                .where(
                    MonitorBatch.workspace_id == actor.workspace_id,
                    MonitorBatch.source_id.in_(allowed),
                    MonitorBatch.is_demo == self.settings.monitor_is_demo,
                    MonitorBatch.state.in_(ACTIVE),
                )
            )
            leads = session.scalar(
                select(func.count())
                .select_from(MonitorLead)
                .join(MonitorBatch, MonitorLead.batch_id == MonitorBatch.id)
                .where(
                    MonitorLead.workspace_id == actor.workspace_id,
                    MonitorBatch.source_id.in_(allowed),
                    MonitorBatch.is_demo == self.settings.monitor_is_demo,
                    MonitorLead.state.in_(("pending", "investigating")),
                )
            )
            connected = bool(self.settings.data_fabric_url and self.settings.data_fabric_history_read_token)
            state = (
                "disabled"
                if not (self.settings.monitor_enabled and DevelopmentAccess(self.settings).enabled)
                else "paused"
                if row.paused
                else "not_connected"
                if not connected
                else "starting"
                if row.heartbeat_at is None
                else "stale"
                if datetime.now(UTC) - row.heartbeat_at
                > timedelta(seconds=max(180, self.settings.monitor_poll_seconds * 3))
                else "running"
            )
            return MonitorStatus(
                enabled=self.settings.monitor_enabled and DevelopmentAccess(self.settings).enabled,
                paused=row.paused,
                control_revision=row.control_revision,
                process_status=state,
                last_heartbeat=row.heartbeat_at,
                limits={
                    "page_records": self.settings.monitor_batch_records,
                    "window_records": min(
                        self.settings.monitor_context_records, self.settings.engine_max_records
                    ),
                    "pending_runs": self.settings.monitor_pending_runs,
                    "investigations_per_batch": self.settings.monitor_investigations_per_batch,
                    "investigations_per_hour": self.settings.monitor_hourly_investigations,
                    "cooldown_minutes": self.settings.monitor_cooldown_minutes,
                    "followup_queries_per_lead": 2,
                    "records_per_followup": 3,
                    "poll_seconds": self.settings.monitor_poll_seconds,
                },
                sources=sources,
                publication_sources=publication,
                counts={"pending_batches": batches, "pending_leads": leads},
                limitations=LIMITATIONS,
            )

    def batches(self, actor, cursor=None, limit=30):
        self._actor(actor)
        allowed = self.permitted()
        with Session(self.engine.database) as session:
            rows = list(
                session.scalars(
                    self.engine._page(
                        select(MonitorBatch).where(
                            MonitorBatch.workspace_id == actor.workspace_id,
                            MonitorBatch.is_demo == self.settings.monitor_is_demo,
                        ),
                        MonitorBatch,
                        cursor,
                        limit,
                    )
                )
            )
            items, hidden = [], 0
            for row in rows[:limit]:
                if row.source_id not in allowed:
                    hidden += 1
                    continue
                try:
                    if row.snapshot_id:
                        self.engine._snapshot(session, actor, row.snapshot_id)
                    elif row.payload.get("snapshot"):
                        self.engine._authorize_snapshot(
                            actor, NormalizedSnapshot.model_validate(row.payload["snapshot"])
                        )
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}:
                        hidden += 1
                        continue
                    raise
                p = row.payload
                items.append(
                    MonitorBatchView(
                        id=row.id,
                        source_id=row.source_id,
                        is_demo=row.is_demo,
                        state=row.state,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        run_id=row.run_id,
                        snapshot_id=row.snapshot_id,
                        from_after=p.get("after"),
                        next_after=p.get("next_after"),
                        max_seq=p.get("max_seq"),
                        historical_sampling=p.get("historical_sampling"),
                        changed_versions=len(p["manifest"]),
                        selection_summary=p.get("selection_summary"),
                        window_records=len((p.get("snapshot") or {}).get("records", [])),
                        investigation_ids=p.get("investigation_ids", []),
                        error_code=row.error_code,
                        limitations=p.get("limitations", LIMITATIONS),
                    )
                )
            return MonitorBatchList(
                items=items, next_cursor=self.engine._next_cursor(rows, limit), unavailable_count=hidden
            )

    def leads(self, actor, cursor=None, limit=30):
        self._actor(actor)
        allowed = self.permitted()
        with Session(self.engine.database) as session:
            rows = list(
                session.scalars(
                    self.engine._page(
                        select(MonitorLead)
                        .join(MonitorBatch, MonitorLead.batch_id == MonitorBatch.id)
                        .where(
                            MonitorLead.workspace_id == actor.workspace_id,
                            MonitorBatch.is_demo == self.settings.monitor_is_demo,
                        ),
                        MonitorLead,
                        cursor,
                        limit,
                    )
                )
            )
            items, hidden = [], 0
            for row in rows[:limit]:
                batch = session.get(MonitorBatch, row.batch_id)
                if (
                    batch is None
                    or batch.source_id not in allowed
                    or batch.is_demo != self.settings.monitor_is_demo
                ):
                    hidden += 1
                    continue
                try:
                    self.engine._run(session, actor, row.run_id)
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}:
                        hidden += 1
                        continue
                    raise
                p, c = row.payload, row.payload["candidate"]
                base_axes = c.get("assessment_axes") or default_assessment_axes(
                    [support["record_ref"] for support in c.get("supports", [])],
                    evidence_level=(c.get("metrics", {}).get("attention") or {}).get("evidence_level"),
                )
                entries = (
                    InvestigationService(self.engine).get(actor, row.investigation_id).entries
                    if row.investigation_id
                    else []
                )
                axes = project_reviewed_assessment_axes(
                    base_axes, entries, cutoff=(p.get("historical_context") or {}).get("cutoff")
                )
                items.append(
                    MonitorLeadView(
                        assessment_axes=axes,
                        id=row.id,
                        lead_key=row.lead_key,
                        title=c["title"],
                        method_id=c["method_id"],
                        state=row.state,
                        run_id=row.run_id,
                        investigation_id=row.investigation_id,
                        batch_id=row.batch_id,
                        new_to_radar=p.get("new_to_radar", False),
                        needs_investigation=True,
                        importance=c.get("importance"),
                        importance_reason=p.get("importance_reason"),
                        urgency_reason=p.get("urgency_reason"),
                        urgency=c["urgency"],
                        changed_support_versions=len(p.get("changed_supports", [])),
                        error_code=p.get("error_code"),
                        data_gaps=c["data_gaps"],
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                    )
                )
            return MonitorLeadList(
                items=items, next_cursor=self.engine._next_cursor(rows, limit), unavailable_count=hidden
            )

    def _saved_response(self, operation, key):
        # Query durable local receipt before reconciling a possibly committed write.
        with Session(self.engine.database) as session:
            row = session.scalar(
                select(EngineIdempotency).where(
                    EngineIdempotency.workspace_id == self.actor.workspace_id,
                    EngineIdempotency.actor_id == self.actor.actor_id,
                    EngineIdempotency.operation == operation,
                    EngineIdempotency.key == key,
                )
            )
            return copy.deepcopy(row.response_body) if row and row.response_code is not None else None

    def _load_batch(self, identifier):
        with Session(self.engine.database) as session:
            row = session.get(MonitorBatch, identifier)
            if row is None or row.workspace_id != self.actor.workspace_id:
                raise DomainError("monitor_batch_not_found", "监测批次不存在", 404)
            return row.id, row.source_id, row.state, copy.deepcopy(row.payload), row.snapshot_id, row.run_id

    def _save_batch(self, identifier, *, payload=None, state=None, run_id=None, snapshot_id=None, error=None):
        with Session(self.engine.database) as session, session.begin():
            self._guard(self._state(session, lock=True))
            row = session.get(MonitorBatch, identifier, with_for_update=True)
            if payload is not None:
                row.payload = payload
            if state is not None:
                row.state = state
            if run_id is not None:
                row.run_id = run_id
            if snapshot_id is not None:
                row.snapshot_id = snapshot_id
            row.error_code, row.updated_at = error, datetime.now(UTC)

    def _graph_projection(self, record):
        from bioradar.adapters.fabric_graph import (
            adopted_graph_snapshot,
            project_graph_hints,
            read_graph_evidence,
            read_neighborhood,
        )
        from bioradar.research.investigations.graph import GraphBundle, GraphInvestigationCreate

        try:
            bundle = GraphBundle.model_validate_json(record["raw_content"])
        except ValidationError:
            raise DomainError(
                "monitor_graph_bundle_invalid", "已发布图包不符合固定图契约，保留原游标待核", 502
            ) from None
        if not bundle.nodes:
            return [], ["empty_published_graph"]
        request = GraphInvestigationCreate(
            question="核对本次发布关系及原件",
            source_id=record["source_id"],
            source_record_id=record["source_record_id"],
            graph_version_id=record["version_id"],
            is_demo=record["is_demo"],
            seed_ids=tuple(n.wire for n in sorted(bundle.nodes, key=lambda n: n.key)[:20]),
            max_depth=2,
            max_nodes=100,
            max_edges=200,
            search_clinicaltrials=False,
        )
        cutoff = datetime.now(UTC)
        neighborhood = read_neighborhood(
            self.reader, record, bundle, request, cutoff, review_scope="with_candidates"
        )
        documents, seen = [], set()
        for edge in neighborhood.edges:
            for evidence in edge.evidence:
                if evidence.key not in seen and len(documents) < 4:
                    seen.add(evidence.key)
                    documents.append(
                        read_graph_evidence(
                            self.reader, evidence, is_demo=record["is_demo"], mode="current", cutoff=cutoff
                        )
                    )
        snapshot = adopted_graph_snapshot(
            self.reader, record, bundle, neighborhood=neighborhood, documents=documents
        )
        records = list(snapshot.records)
        if getattr(self.settings, "monitor_kdd_directions", {}).get(record["source_id"]) == "pipeline":
            records[0] = project_graph_hints(
                self.reader,
                record,
                bundle,
                engine_record=records[0],
                documents=documents,
                neighborhood=neighborhood,
            )
        return records, ["graph_evidence_or_neighborhood_is_bounded"]

    def _mining_policy(self, source_id, now, *, changed_refs=None, mode="current"):
        direction = getattr(self.settings, "monitor_kdd_directions", {}).get(source_id)
        model = KddPolicy if direction else DiscoveryPolicy
        values = (
            {"kdd_direction": direction, "monitor_changed_refs": changed_refs or None}
            if direction
            else {
                "discovery_directions": ("research", "pipeline", "business", "public_health"),
                "monitor_changed_refs": changed_refs or None,
            }
        )
        if mode == "public_history_reconstruction":
            values.update(time_mode="availability_replay", availability_basis=mode)
        current_start = now - timedelta(days=getattr(self.settings, "monitor_current_window_days", 30))
        return model(
            as_of=now,
            current_start=current_start,
            baseline_end=current_start,
            discovery_mode="stock_and_incremental",
            source_intelligence_version="public-health-source-intelligence-v1",
            limits=ResourceLimits(
                max_records=self.settings.engine_max_records,
                review_capacity=12,
                max_candidates_per_method=30,
            ),
            **values,
        )

    def _candidate_followup_plan(self, payload, candidate):
        plans = followup_plan(payload["evidence"], candidate)
        if not candidate.metrics.get("graph_relation_hint"):
            return plans
        sources = set()
        for support in candidate.supports:
            for ref in support.evidence_refs:
                if ref.provider != "data_fabric_history":
                    continue
                try:
                    source, _, _ = json.loads(ref.object_id)
                except (ValueError, TypeError):
                    continue
                sources.add(source)
        sources.difference_update({"reviewed_graph", "primekg_graph"})
        if not sources:
            sources.update(
                source
                for source, direction in getattr(self.settings, "monitor_kdd_directions", {}).items()
                if direction == "pipeline" and source not in {"reviewed_graph", "primekg_graph"}
            )
        sources.intersection_update(self.permitted())
        for term in candidate.metrics.get("query_terms", []):
            if not isinstance(term, str) or not 2 <= len(term.strip()) <= 120:
                continue
            for source in sorted(sources):
                plans.append(
                    {
                        "term": term,
                        "field": "fixed_graph_hint_label",
                        "source_id": source,
                        "qualification": "hypothesis_for_original_search_not_confirmed_relation",
                        "review_status": candidate.metrics.get("review_status"),
                    }
                )
                if len(plans) >= 2:
                    return plans[:2]
        return plans[:2]

    @staticmethod
    def _candidate_question(candidate, direction=None):
        question = (
            f"待调查线索（程序候选，尚未核实）：{candidate.title[:200]}\n"
            f"调查问题：{candidate.next_questions[0]}"
        )
        attention = candidate.metrics.get("attention") or {}
        if attention.get("checkable_claims"):
            question += "\n原件待核说法：" + attention["checkable_claims"][0]["text"][:800]
        question += "\n区分原文声称、独立佐证与已核实事实；列出反证、成立条件和最能区分竞争解释的补查。"
        if direction:
            labels = {
                "pipeline": "管线与竞争",
                "research": "科研与技术",
                "business": "商业与产业",
                "public_health": "公共卫生",
            }
            question = "分析方向：" + labels[direction] + "\n" + question
            if candidate.metrics.get("graph_relation_hint"):
                question += (
                    "\n图关系仅是待补证线索，保留原身份、审核与绑定状态；"
                    "核对固定原件，不能据图推断药效或项目确认。"
                )
                question += "\n图中的原名或登记号：" + "；".join(candidate.metrics.get("query_terms", [])[:6])
            if candidate.metrics.get("native_notice_event"):
                question += "\n这是通报事件，不是病例数或发病率；区分发布、采集补录与疫情发生时间。"
            if candidate.data_gaps:
                question += "\n需补证范围：" + "；".join(candidate.data_gaps[:6])
        return question[:4000]

    def _save_history(self, replay):
        with Session(self.engine.database) as session, session.begin():
            state = self._state(session, lock=True)
            self._guard(state)
            values = copy.deepcopy(state.source_progress)
            values["_history_replay"] = copy.deepcopy(replay)
            state.source_progress = values

    def advance_history_day(self, *, mode, cutoff, source_ids, topic_ids=()):
        """One explicit public-time step using real collection, batch and RQ receipts."""
        cutoff = datetime.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
        if (
            mode != "public_history_reconstruction"
            or not isinstance(cutoff, datetime)
            or cutoff.tzinfo is None
            or cutoff > datetime.now(UTC)
        ):
            raise DomainError("history_replay_invalid", "日推进需要明确历史公开模式和过去UTC时点", 422)
        cutoff = cutoff.astimezone(UTC).isoformat()
        sources, topics = sorted(set(source_ids)), sorted(set(topic_ids))
        if not sources or not set(sources).issubset(self.permitted()):
            raise DomainError("source_not_authorized", "历史集合包含未许可来源", 403)
        if not self.claim():
            return {"status": "waiting", "reason": "paused_or_busy", "mode": mode, "cutoff": cutoff}
        error = None
        try:
            with Session(self.engine.database) as session:
                values = copy.deepcopy(self._state(session).source_progress)
            scope_hash = canonical_hash(
                {
                    "sources": sources,
                    "is_demo": self.settings.monitor_is_demo,
                    "instance": self.reader.demo_source_instance or "fixture"
                    if self.settings.monitor_is_demo
                    else self.reader.source_instance,
                }
            )
            replay = values.get("_history_replay")
            if replay is None:
                if any(value.get("initialized") or value.get("active_batch_id") for value in values.values()):
                    raise DomainError("history_replay_workspace", "历史回放必须使用专属工作空间", 409)
                replay = {
                    "mode": mode,
                    "scope_hash": scope_hash,
                    "source_ids": sources,
                    "is_demo": self.settings.monitor_is_demo,
                    "last_committed_cutoff": None,
                    "current_records": {},
                }
            if replay["mode"] != mode or replay["scope_hash"] != scope_hash:
                raise DomainError("history_replay_scope_changed", "回放来源与模式已冻结", 409)
            day = replay.get("pending_day")
            if day and day["cutoff"] != cutoff and day["status"] != "completed":
                raise DomainError("history_replay_pending", "先恢复原固定历史日", 409)
            if day and day["cutoff"] == cutoff:
                if day.get("requested_topic_ids", day["topic_ids"]) != topics:
                    raise DomainError("history_replay_scope_changed", "同日专题集合已冻结", 409)
                if day["status"] == "completed":
                    return copy.deepcopy(day)
            else:
                if replay["last_committed_cutoff"] and cutoff <= replay["last_committed_cutoff"]:
                    raise DomainError("history_replay_nonmonotonic", "新日必须严格晚于已完成历史日", 409)
                day = {
                    "mode": mode,
                    "cutoff": cutoff,
                    "scope_hash": scope_hash,
                    "status": "prepared",
                    "collections": {},
                    "batch_ids": [],
                    "run_ids": [],
                    "topic_ids": sorted(set(topics) | set(day["topic_ids"] if day else [])),
                    "requested_topic_ids": topics,
                    "topic_receipts": {},
                    "previous_records": copy.deepcopy(replay["current_records"]),
                }
                replay["pending_day"] = day
                self._save_history(replay)
            for source in sources:
                if source in day["collections"]:
                    continue
                collection = self.reader.collect(
                    source_id=source,
                    is_demo=self.settings.monitor_is_demo,
                    mode=mode,
                    cutoff=datetime.fromisoformat(cutoff),
                )
                collection["cutoff"] = cutoff
                selected, ambiguous = select_history_versions(
                    self.reader, collection, previous=replay["current_records"].get(source, [])
                )
                day["collections"][source] = {
                    "receipt_id": canonical_hash([scope_hash, cutoff, source, collection]),
                    "collection": collection,
                    "selected_refs": [self.reader.ref(r).model_dump(mode="json") for r in selected],
                    "ambiguous_refs": ambiguous,
                    "status": "collected",
                }
                replay["current_records"][source] = selected
                self._save_history(replay)  # Received collection always precedes mining/adoption.
            for source in sources:
                identifier = self.accept_history_collection(source)
                self.advance(identifier)
            with Session(self.engine.database) as session:
                replay = copy.deepcopy(self._state(session).source_progress["_history_replay"])
                states = [
                    session.get(MonitorBatch, identifier).state
                    for identifier in replay["pending_day"]["batch_ids"]
                ]
            day = replay["pending_day"]
            with Session(self.engine.database) as session:
                created = [
                    lead.payload.get("auto_topic", {}).get("topic_id")
                    for lead in session.scalars(
                        select(MonitorLead).where(MonitorLead.batch_id.in_(day["batch_ids"]))
                    )
                ]
            day["topic_ids"] = sorted(set(day["topic_ids"]) | {topic for topic in created if topic})
            if any(state != "completed" for state in states):
                day["status"] = "blocked" if "blocked" in states else "waiting"
            else:
                from bioradar.topics.service import TopicService

                service = TopicService(self.engine, self.reader)
                service._active_monitor = self
                for topic in day["topic_ids"]:
                    if day["topic_receipts"].get(topic, {}).get("status") == "completed":
                        continue
                    day["topic_receipts"][topic] = service.advance_history_day(
                        topic, mode=mode, cutoff=cutoff
                    )
                    self._save_history(replay)
                receipts = list(day["topic_receipts"].values())
                day["status"] = (
                    "blocked"
                    if any(r["status"] == "blocked" for r in receipts)
                    else "waiting"
                    if any(r["status"] != "completed" for r in receipts)
                    else "completed"
                )
            if day["status"] == "completed":
                replay["last_committed_cutoff"] = cutoff
            self._save_history(replay)
            return copy.deepcopy(day)
        except DomainError as exc:
            error = exc.code
            raise
        finally:
            self.release(error)

    def accept_history_collection(self, source_id):
        """Freeze an actual received collection into the ordinary monitor batch table."""
        if source_id not in self.permitted():
            raise DomainError("source_not_authorized", "历史来源当前已无读取许可", 403)
        with Session(self.engine.database) as session:
            replay = copy.deepcopy(self._state(session).source_progress.get("_history_replay") or {})
        day = replay.get("pending_day") or {}
        if source_id not in day.get("collections", {}):
            raise DomainError("history_collection_not_frozen", "该来源没有已收到的固定历史集合", 409)
        receipt = day["collections"][source_id]
        if receipt.get("batch_id"):
            return receipt["batch_id"]
        cutoff = datetime.fromisoformat(day["cutoff"])
        mode = replay["mode"]
        prior = {r["source_record_id"]: r for r in day["previous_records"].get(source_id, [])}
        current = replay["current_records"].get(source_id, [])
        changed = [
            r for r in current if r["version_id"] != prior.get(r["source_record_id"], {}).get("version_id")
        ]
        originals = [r for values in replay["current_records"].values() for r in values]
        originals.extend(
            old
            for native, old in prior.items()
            if any(r["source_record_id"] == native and r["version_id"] != old["version_id"] for r in changed)
        )
        records = [
            project_record(self.reader, r, as_of=cutoff, mode=mode, cutoff=cutoff, kdd=True)
            for r in originals
        ]
        records = [r for r in records if r is not None]
        limit = min(self.settings.monitor_context_records, self.settings.engine_max_records)
        omitted = max(0, len(records) - limit)
        records, bytes_omitted = byte_window(records[:limit], self.settings.engine_max_snapshot_bytes // 2)
        snapshot = (
            build_snapshot(
                records,
                is_demo=self.settings.monitor_is_demo,
                as_of=cutoff,
                omitted=omitted + bytes_omitted,
                mode=mode,
            )
            if changed
            else None
        )
        context = {
            "mode": mode,
            "cutoff": day["cutoff"],
            "history_series_id": canonical_hash([self.actor.workspace_id, replay["scope_hash"]]),
            "history_scope_hash": replay["scope_hash"],
        }
        manifest = [
            {k: r[k] for k in ("source_id", "source_record_id", "version_id", "raw_sha256", "is_demo")}
            for r in changed
        ]
        refs = [self.reader.ref(r).model_dump(mode="json") for r in changed]
        payload = {
            "monitor_version": MONITOR_VERSION,
            "feature_adoption": {
                "status": "not_adopted",
                "gaps": ["historical_external_features_not_proven_at_cutoff"],
            },
            "historical_context": context,
            "historical_collection": copy.deepcopy(receipt["collection"]),
            "historical_sampling": {
                "mode": mode,
                "cutoff": day["cutoff"],
                "source_ids": replay["source_ids"],
                "receipt_id": receipt["receipt_id"],
                "coverage": receipt["collection"]["coverage"],
                "excluded_counts": receipt["collection"].get("excluded_counts", {}),
                "ambiguous_refs": receipt["ambiguous_refs"],
            },
            "manifest": manifest,
            "changed_refs": [list(self.reader.ref(r).version_key) for r in changed],
            "context_update_refs": refs,
            "changed_text_hashes": sorted(
                {r.normalized_text_hash for r in records if r.normalized_text_hash}
            ),
            "evidence": [evidence_row(self.reader, r) for r in originals],
            "historical_selected_records": [
                {k: v for k, v in r.items() if k != "raw_content"} for r in current
            ],
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "policy": self._mining_policy(source_id, cutoff, changed_refs=refs, mode=mode).model_dump(
                mode="json"
            ),
            "limitations": [
                *LIMITATIONS,
                "historical_collection_is_not_a_current_heads_or_change_feed",
                "historical_external_features_not_proven_at_cutoff",
            ],
            "selected_leads": [],
            "investigation_ids": [],
        }
        payload["selection_summary"] = self._selection_summary(payload)
        identifier = str(uuid4())
        with Session(self.engine.database) as session, session.begin():
            state = self._state(session, lock=True)
            self._guard(state)
            values = copy.deepcopy(state.source_progress)
            current_day = values["_history_replay"]["pending_day"]
            if current_day["cutoff"] != day["cutoff"]:
                raise DomainError("history_replay_conflict", "历史日已变化", 409)
            if current_day["collections"][source_id].get("batch_id"):
                return current_day["collections"][source_id]["batch_id"]
            session.add(
                MonitorBatch(
                    id=identifier,
                    workspace_id=self.actor.workspace_id,
                    batch_key=canonical_hash([replay["scope_hash"], day["cutoff"], source_id]),
                    source_id=source_id,
                    is_demo=self.settings.monitor_is_demo,
                    state="accepted",
                    payload=payload,
                    updated_at=datetime.now(UTC),
                )
            )
            current_day["collections"][source_id]["batch_id"] = identifier
            current_day["batch_ids"].append(identifier)
            state.source_progress = values
        return identifier

    def accept_page(self, source_id):
        with Session(self.engine.database) as session:
            progress = copy.deepcopy(self._state(session).source_progress)
        if progress.get("_history_replay"):
            raise DomainError("history_replay_workspace", "该工作空间只接受显式历史日推进", 409)
        stream = self.stream_key(source_id)
        current = progress.get(stream, {})
        if current.get("active_batch_id"):
            return current["active_batch_id"]
        after = current.get("scan_after", current.get("acked_after", 0))
        page = change_page(
            self.reader,
            source_id=source_id,
            is_demo=self.settings.monitor_is_demo,
            after=after,
            cursor=current.get("scan_cursor"),
            limit=self.settings.monitor_batch_records,
            minimum_after=current.get("acked_after", 0),
        )
        if current.get("scan_cursor") and page["max_seq"] != current.get("scan_max_seq"):
            raise DomainError("monitor_watermark_changed", "变更分页不能切换固定发布水位", 502)
        now = datetime.now(UTC)
        manifest = [
            {
                k: item[k]
                for k in (
                    "source_id",
                    "source_record_id",
                    "version_id",
                    "raw_sha256",
                    "published_seq",
                    "is_demo",
                )
            }
            for item in page["items"]
        ]
        batch_key = canonical_hash(
            {
                "source": source_id,
                "demo": self.settings.monitor_is_demo,
                "after": after,
                "cursor": current.get("scan_cursor"),
                "max_seq": page["max_seq"],
                "manifest": manifest,
            }
        )
        if not manifest:
            with Session(self.engine.database) as session, session.begin():
                row = self._state(session, lock=True)
                self._guard(row)
                values = copy.deepcopy(row.source_progress)
                values[stream] = {
                    **values.get(stream, {}),
                    "source_id": source_id,
                    "is_demo": self.settings.monitor_is_demo,
                    "acked_after": page["next_after"],
                    "scan_after": page["next_after"],
                    "scan_cursor": None,
                    "scan_max_seq": None,
                    "state": "idle",
                    "error_code": None,
                    "last_poll_at": now.isoformat(),
                    "last_success_at": now.isoformat(),
                }
                row.source_progress = values
            return None
        old = {identity(r): r for r in (EngineRecord.model_validate(v) for v in current.get("context", []))}
        previous, changes, heads, gaps, update_refs = [], [], [], [], []
        graph_keys = {
            identity(record)
            for record in old.values()
            if any(
                e.get("record_kind") == "graph" and tuple(e["ref_key"]) == record.record_ref.version_key
                for e in current.get("evidence", [])
            )
        }
        attached = {ref.identity_key for key in graph_keys for ref in old[key].evidence_refs}
        known_texts = {
            r.get("normalized_text_hash")
            for v in progress.values()
            if v.get("is_demo") is self.settings.monitor_is_demo
            for r in v.get("context", [])
        }
        changed_texts = set()
        evidence = list(current.get("evidence", []))
        page_items = {r["version_id"]: r for r in page["items"]}
        for native, version_id in page["current_version_ids"].items():
            if version_id is None:
                continue  # context-only evidence is logged, never a source-record head.
            value = page_items.get(version_id)
            if value is None:
                value = self.reader.read_version(
                    {
                        "source_id": source_id,
                        "source_record_id": native,
                        "version_id": version_id,
                        "is_demo": self.settings.monitor_is_demo,
                    },
                    mode="current",
                    cutoff=now,
                )
                if value["published_seq"] > page["max_seq"]:
                    raise DomainError("monitor_head_mismatch", "当前版本超出固定批次水位", 502)
            if value["source_record_id"] != native:
                raise DomainError("monitor_head_mismatch", "当前版本与指定来源记录不一致", 502)
            heads.append({k: v for k, v in value.items() if k != "raw_content"})
            record = project_record(
                self.reader,
                value,
                as_of=now,
                bootstrap=not current.get("initialized"),
                kdd=True,  # New monitor policies are KDD or multi-domain discovery.
            )
            if record is None:
                continue
            key = identity(record)
            prior = old.get(key)
            if prior and prior.record_ref.version_key == record.record_ref.version_key:
                continue
            update_refs.append(record.record_ref.model_dump(mode="json"))
            if (
                prior
                and value["record_kind"] != "graph"
                and not set(record.record_flags) & {"deleted", "retracted"}
            ):
                previous.append(prior)
            if record.normalized_text_hash and record.normalized_text_hash not in known_texts:
                changed_texts.add(record.normalized_text_hash)
            if prior is None or fingerprint(prior) != fingerprint(record):
                changes.append(list(record.record_ref.version_key))
            old[key] = record
            if value["record_kind"] == "graph":
                graph_keys.add(key)
                try:
                    projected, graph_gaps = self._graph_projection(value)
                    gaps.extend(graph_gaps)
                    for item in projected:
                        old[identity(item)] = item
                        if item.record_ref.version_key != record.record_ref.version_key:
                            attached.add(item.record_ref.identity_key)
                except DomainError as exc:
                    if exc.code == "monitor_graph_bundle_invalid":
                        raise
                    gaps.append("graph_projection_unavailable_text_monitoring_continues")
            evidence = [r for r in evidence if r["source_record_id"] != native]
            evidence.append(evidence_row(self.reader, value))
        retained = sorted(old.values(), key=lambda r: (r.observed_at, r.record_ref.version_key), reverse=True)
        per_source = max(
            1,
            min(self.settings.monitor_context_records, self.settings.engine_max_records)
            // max(1, len(self.permitted())),
        )
        omitted_local = max(0, len(retained) - per_source)
        retained = retained[:per_source]
        retained, omitted_bytes = byte_window(
            retained, self.settings.engine_max_snapshot_bytes // (2 * max(1, len(self.permitted())))
        )
        omitted_local += omitted_bytes
        referenced = {
            ref.version_key
            for record in retained
            if identity(record) in graph_keys
            for ref in record.evidence_refs
        }
        own_records = {tuple(e["ref_key"]) for e in evidence if e.get("record_kind") != "graph"}
        retained = [
            record
            for record in retained
            if record.record_ref.identity_key not in attached
            or record.record_ref.version_key in referenced | own_records
        ]
        keep = {tuple(r.record_ref.version_key) for r in retained}
        evidence = [r for r in evidence if tuple(r["ref_key"]) in keep]
        proposed = {
            **current,
            "source_id": source_id,
            "is_demo": self.settings.monitor_is_demo,
            "context": [r.model_dump(mode="json") for r in retained],
            "evidence": evidence,
            "initialized": True,
        }
        progress[stream] = proposed
        visible = {
            k: v
            for k, v in progress.items()
            if v.get("source_id") in self.permitted() and v.get("is_demo") is self.settings.monitor_is_demo
        }
        previous = previous[: self.settings.engine_max_records // 2]
        previous, omitted_previous_bytes = byte_window(previous, self.settings.engine_max_snapshot_bytes // 4)
        records, omitted = fair_window(
            visible,
            min(self.settings.monitor_context_records, self.settings.engine_max_records - len(previous)),
        )
        records.extend(previous)
        snapshot = build_snapshot(
            records,
            is_demo=self.settings.monitor_is_demo,
            as_of=now,
            omitted=omitted + omitted_local + omitted_previous_bytes,
        )
        # Coverage/schema overhead is checked against the same adoption limit.
        # Halving takes logarithmic steps and never truncates a quoted record.
        while snapshot and len(snapshot.model_dump_json().encode()) > self.settings.engine_max_snapshot_bytes:
            kept = records[: len(records) // 2]
            omitted += len(records) - len(kept)
            records = kept
            snapshot = build_snapshot(
                records,
                is_demo=self.settings.monitor_is_demo,
                as_of=now,
                omitted=omitted + omitted_local + omitted_previous_bytes,
            )
        if omitted_local or omitted or omitted_previous_bytes:
            gaps.append("whole_records_omitted_from_bounded_feature_window_versions_retained_at_source")
        policy = self._mining_policy(source_id, now, changed_refs=update_refs)
        payload = {
            "monitor_version": MONITOR_VERSION,
            "after": after,
            "page_cursor": current.get("scan_cursor"),
            "max_seq": page["max_seq"],
            "next_after": page["next_after"],
            "next_cursor": page["next_cursor"],
            "manifest": manifest,
            "current_versions": page["current_version_ids"],
            "changed_refs": changes,
            "context_update_refs": [
                ref
                for ref in update_refs
                if snapshot
                and any(record.record_ref.model_dump(mode="json") == ref for record in snapshot.records)
            ],
            "changed_text_hashes": sorted(changed_texts),
            "evidence": [e for v in visible.values() for e in v.get("evidence", [])],
            "heads": heads,
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "policy": policy.model_dump(mode="json"),
            "limitations": [*LIMITATIONS, *gaps],
            "investigation_ids": [],
            "selected_leads": [],
        }
        payload["selection_summary"] = self._selection_summary(payload)
        with Session(self.engine.database) as session, session.begin():
            row = self._state(session, lock=True)
            self._guard(row)
            values = copy.deepcopy(row.source_progress)
            if values.get(stream, {}).get("active_batch_id"):
                return values[stream]["active_batch_id"]
            identifier = str(uuid4())
            session.add(
                MonitorBatch(
                    id=identifier,
                    workspace_id=self.actor.workspace_id,
                    batch_key=batch_key,
                    source_id=source_id,
                    is_demo=self.settings.monitor_is_demo,
                    state="accepted",
                    payload=payload,
                    updated_at=now,
                )
            )
            values[stream] = {
                **proposed,
                "active_batch_id": identifier,
                "state": "accepted",
                "scan_max_seq": page["max_seq"],
                "last_poll_at": now.isoformat(),
                "error_code": None,
            }
            row.source_progress = values
        return identifier

    def _selection_summary(self, payload, result=None, selected_candidates=()):
        """Partition this manifest only; successful method execution is not a global negative."""
        manifest = payload["manifest"]
        keys = [(item, self.reader.ref(item).version_key) for item in manifest]
        snapshot = payload.get("snapshot")
        window = {
            EngineRecord.model_validate(record).record_ref.version_key
            for record in (snapshot or {}).get("records", [])
        }
        selected = {
            support.record_ref.version_key
            for candidate in selected_candidates
            for support in candidate.supports
        }
        methods, processed, excluded = [], set(), set()
        outcomes = result.method_results if result is not None else ()
        kdd_direction = payload.get("policy", {}).get("kdd_direction")
        evaluations = {item["candidate_key"]: item for item in (result.pattern_evaluations if result else ())}
        decisions = {item["candidate_key"]: item for item in (result.selection_decisions if result else ())}
        record_decisions = defaultdict(list)
        if kdd_direction:
            for outcome in outcomes:
                for candidate in outcome.candidates:
                    decision = decisions.get(candidate.candidate_key)
                    if decision:
                        for support in candidate.supports:
                            record_decisions[support.record_ref.version_key].append(decision)
        for method in outcomes:
            omitted = {item.record_ref.version_key for item in method.excluded_records}
            handled = {
                support.record_ref.version_key
                for candidate in method.candidates
                for support in candidate.supports
            }
            successful = method.execution_status == "success" and not method.truncated_count
            if not successful:
                handled = set()
            elif method.method_id in {"lexical_emergence", "text_topics"} and method.statistics.get(
                "text_records"
            ) == len(window):
                handled.update(window)
            handled.difference_update(omitted)
            processed.update(handled)
            excluded.update(omitted)
            methods.append(
                {
                    "method_id": method.method_id,
                    "execution_status": method.execution_status,
                    "readiness": method.readiness,
                    "processed_manifest_versions": sum(key in handled for _, key in keys),
                    "excluded_manifest_versions": sum(key in omitted for _, key in keys),
                    "truncated_count": method.truncated_count,
                    "statistics": method.statistics,
                    "reasons": list(method.reasons),
                    "limitations": list(method.limitations),
                }
            )
        if kdd_direction:
            for summary, outcome in zip(methods, outcomes, strict=True):
                candidate_keys = [candidate.candidate_key for candidate in outcome.candidates]
                summary.update(
                    kdd_direction=kdd_direction,
                    generated_candidate_keys=candidate_keys,
                    all_generated_candidates_retained_in_run=True,
                    pattern_evaluations=[evaluations[key] for key in candidate_keys if key in evaluations],
                    selection_decisions=[decisions[key] for key in candidate_keys if key in decisions],
                )
            from bioradar.research.mining.kdd import kdd_versions

            expected = set(kdd_versions(KddPolicy.model_validate(payload["policy"]))) - {"__engine_compute__"}
        else:
            expected = set(payload.get("policy", {}).get("method_ids", []))
        complete_methods = (
            bool(outcomes)
            and expected <= {method.method_id for method in outcomes}
            and all(
                method.execution_status == "success" and not method.truncated_count for method in outcomes
            )
        )
        rows = []
        for item, key in keys:
            if key in selected:
                outcome, reason = "selected", "actual_selected_lead_supports_this_manifest_version"
            elif key not in window:
                outcome, reason = "unknown", "not_in_frozen_computation_window"
            elif result is None:
                outcome, reason = "unknown", "method_receipts_not_available"
            elif kdd_direction and record_decisions.get(key):
                statuses = {value["decision"] for value in record_decisions[key]}
                outcome = "no_candidate"
                if statuses & {"capacity_deferred", "redundancy_deferred"}:
                    reason = "value_eligible_pattern_deferred_by_review_capacity"
                elif statuses == {"value_excluded"}:
                    reason = "generated_patterns_excluded_by_frozen_value_policy"
                else:
                    reason = "selected_pattern_already_known_or_without_new_meaningful_support"
            elif key in excluded:
                outcome, reason = "unknown", "record_excluded_by_method_preparation"
            elif not complete_methods:
                outcome, reason = "unknown", "failed_unsupported_partial_or_missing_method_scope"
            elif key not in processed:
                outcome, reason = "unknown", "no_record_level_processing_receipt"
            else:
                outcome, reason = "no_candidate", "processed_by_successful_methods_but_not_selected_this_run"
            rows.append(
                {
                    "source_id": item["source_id"],
                    "source_record_id": item["source_record_id"],
                    "version_id": item["version_id"],
                    "in_computation_window": key in window,
                    "outcome": outcome,
                    "reason": reason,
                }
            )
            if kdd_direction:
                rows[-1]["candidate_selection_decisions"] = copy.deepcopy(record_decisions.get(key, []))
        return {
            "schema_version": "bioradar.monitor-selection.v1",
            "scope": "this_batch_manifest_and_frozen_computation_window",
            "new_records": len(manifest),
            "evaluated": sum(row["in_computation_window"] for row in rows),
            **{
                name: sum(row["outcome"] == name for row in rows)
                for name in ("selected", "no_candidate", "unknown")
            },
            "methods": methods,
            "record_outcomes": rows,
            "runtime_configuration": {
                "investigations_per_batch": getattr(self.settings, "monitor_investigations_per_batch", None),
                "investigations_per_hour": getattr(self.settings, "monitor_hourly_investigations", None),
                "topic_investigations_per_hour": getattr(self.settings, "topic_hourly_investigations", None),
            },
            "limitations": [
                "new_records_are_published_versions_not_new_scientific_events",
                "evaluated_means_in_computation_window_not_evaluated_by_every_method",
                "no_candidate_means_not_selected_in_this_completed_method_scope_not_no_value",
                "unproven_record_processing_and_incomplete_method_scope_remain_unknown",
                *payload.get("limitations", []),
            ],
        }

    def _lead_reasons(self, candidate, changed_supports, new_to_radar):
        importance = {
            "undetermined": "未确定",
            "low": "低",
            "medium": "中",
            "high": "高",
        }[candidate.importance]
        urgency = {
            "undetermined": "未确定",
            "routine": "常规",
            "soon": "尽快",
            "immediate": "及时",
        }[candidate.urgency]
        versions = len({support.record_ref.version_key for support in changed_supports})
        reasons = "；".join(candidate.reasons) or "本次方法未提供额外理由"
        first = "本轮首次记录此线索；" if new_to_radar else "本轮更新已有线索；"
        importance_reason = (
            f"{first}实际涉及本批{versions}个新增或修订支持版本，选中继续查证。"
            f"方法{candidate.method_id}标记的重要程度为{importance}（{candidate.importance}）。"
            f"方法理由：{reasons}。选中仅表示继续调查，专业结论仍待核对。"
        )
        urgency_reason = (
            f"方法{candidate.method_id}给定紧急度为{urgency}（{candidate.urgency}），"
            f"依据为本批{versions}个固定支持版本。方法理由：{reasons}。"
            "该时限用于安排排查，保留方法原有等级。"
        )
        return importance_reason, urgency_reason

    def _feature_snapshot(self, snapshot, payload):
        """Read every eligible fixed member in source scopes, retaining the complete window."""
        from bioradar.adapters.weknora_kdd import features_for_snapshot, retrieval_scope

        mapping = self.settings.weknora_kdd_feature_source_scopes
        graph_versions = {
            tuple(item["ref_key"])
            for item in payload.get("evidence", [])
            if item.get("record_kind") == "graph"
        }
        groups, scopes, excluded = defaultdict(list), {}, []
        by_ref = {row.record_ref.version_key: row for row in snapshot.records}
        gaps = set(snapshot.metadata_gaps)
        for row in snapshot.records:
            source_scope = mapping.get(row.source_id)
            reason = None
            if row.graph_hints or row.record_ref.version_key in graph_versions:
                reason = "graph_package_is_not_document_embedding_unit"
            elif (
                not snapshot.is_demo
                and source_scope is None
                and self.settings.weknora_kdd_source_scope is None
            ):
                reason = "feature_source_scope_not_configured"
            else:
                try:
                    scope = retrieval_scope(self.settings, direction=source_scope, is_demo=snapshot.is_demo)
                except DomainError as error:
                    reason = error.code
                else:
                    source_scope = scope["source_scope"]
                    scopes[source_scope] = scope
            if reason:
                excluded.append({"record_ref": row.record_ref.model_dump(mode="json"), "reason": reason})
                gaps.add("weknora_feature:" + reason)
            else:
                groups[source_scope].append(row)
        receipts = []
        adopted_count = 0
        for source_scope, members in sorted(groups.items()):
            for index in range(0, len(members), 100):
                chunk = members[index : index + 100]
                scope = snapshot.model_copy(
                    update={
                        "records": tuple(chunk),
                        "snapshot_id": snapshot.snapshot_id
                        + f"-scope-{canonical_hash(scopes[source_scope])[:12]}-{index}",
                    }
                )
                try:
                    enriched, receipt = features_for_snapshot(self.settings, scope, direction=source_scope)
                except DomainError as error:
                    receipt = {
                        "status": "unavailable",
                        "error_code": error.code,
                        "model_token_usage": None,
                    }
                    for row in chunk:
                        excluded.append(
                            {
                                "record_ref": row.record_ref.model_dump(mode="json"),
                                "reason": error.code,
                            }
                        )
                    gaps.add("weknora_feature:" + error.code)
                else:
                    # The adapter has verified exact members, text hashes and model versions.
                    for row in enriched.records:
                        by_ref[row.record_ref.version_key] = row
                    gaps.update(enriched.metadata_gaps)
                    adopted_count += len(receipt["adopted"])
                    excluded.extend(receipt["excluded"])
                receipts.append({**scopes[source_scope], "start_index": index, "receipt": receipt})
        receipt = {
            "schema_version": "bioradar.monitor-feature-adoption.v2",
            "population": "complete_fixed_monitor_window_not_search_top_k",
            "record_count": len(snapshot.records),
            "adopted_count": adopted_count,
            "status": "partial" if excluded else "complete",
            "groups": receipts,
            "excluded": excluded,
            "model_token_usage": None,
        }
        return snapshot.model_copy(
            update={
                "records": tuple(by_ref[row.record_ref.version_key] for row in snapshot.records),
                "snapshot_id": snapshot.snapshot_id + "-features-" + canonical_hash(receipt)[:12],
                "metadata_gaps": tuple(sorted(gaps)),
            }
        ), receipt

    def _adopt_and_queue(self, identifier, payload):
        snapshot = NormalizedSnapshot.model_validate(payload["snapshot"]) if payload.get("snapshot") else None
        if snapshot is None:
            self._complete(identifier, payload)
            return
        if payload.get("historical_context") and "feature_adoption" not in payload:
            payload = {
                **payload,
                "feature_adoption": {
                    "status": "not_adopted",
                    "gaps": ["historical_external_features_not_proven_at_cutoff"],
                },
            }
            self._save_batch(identifier, payload=payload)
        key = "monitor-snapshot:" + identifier
        adopted = self._saved_response("snapshot.create", key)
        if adopted is None:
            if (
                ("kdd_direction" in payload["policy"] or "discovery_directions" in payload["policy"])
                and self.settings.weknora_kdd_url
                and not payload.get("historical_context")
                and "feature_adoption" not in payload
            ):
                self.engine._authorize_snapshot(self.actor, snapshot)
                snapshot, receipt = self._feature_snapshot(snapshot, payload)
                payload = {
                    **payload,
                    "snapshot": snapshot.model_dump(mode="json"),
                    "feature_adoption": receipt,
                }
                self._save_batch(identifier, payload=payload)
            _, adopted = self.engine.adopt_snapshot(self.actor, snapshot, key)
        self._save_batch(identifier, snapshot_id=adopted["id"])
        policy_model = (
            DiscoveryPolicy
            if "discovery_directions" in payload["policy"]
            else KddPolicy
            if "kdd_direction" in payload["policy"]
            else MiningPolicy
        )
        request = RunCreate(snapshot_id=adopted["id"], policy=policy_model.model_validate(payload["policy"]))
        key = "monitor-run:" + identifier
        run = self._saved_response("run.create", key)
        if run is None:
            _, run = self.engine.create_run(self.actor, request, key)
        self._save_batch(identifier, state="queued", run_id=run["id"])
        self.engine.dispatch(run["id"])

    def _select_leads(self, identifier, run_id, payload, result):
        changed = {tuple(r) for r in payload["changed_refs"]}
        selected, tracking = [], []
        selected_keys = {candidate.candidate_key for candidate in result.candidates}
        candidates = {candidate.candidate_key: candidate for candidate in result.candidates}
        # Topic creation follows attention level even when review capacity deferred
        # its investigation. Keep the complete generated candidate in the same lead table.
        for method in result.method_results:
            for candidate in method.candidates:
                if candidate.importance in {"medium", "high"}:
                    candidates.setdefault(candidate.candidate_key, candidate)
        importance_order = {"high": 0, "medium": 1, "low": 2, "undetermined": 3}
        urgency_order = {"immediate": 0, "soon": 1, "routine": 2, "undetermined": 3}
        ordered = sorted(
            candidates.values(),
            key=lambda candidate: (
                importance_order[candidate.importance],
                urgency_order[candidate.urgency],
                candidate.kind != "observed_change",
                -candidate.score,
                candidate.candidate_key,
            ),
        )
        for item in ordered:
            attention = item.metrics.get("attention") or {}
            if not attention.get("automatic_investigation_allowed") or item.importance == "undetermined":
                continue
            meaningful = [s for s in item.supports if tuple(s.record_ref.version_key) in changed]
            if item.method_id in {
                "lexical_emergence",
                "text_topics",
                "research_tfidf_topics",
                "research_embedding_clusters",
            }:
                meaningful = [
                    s for s in meaningful if s.normalized_text_hash in payload["changed_text_hashes"]
                ]
                if (
                    item.method_id == "lexical_emergence"
                    and len({s.normalized_text_hash for s in item.supports if s.normalized_text_hash}) < 2
                ):
                    continue
            if not meaningful or not item.next_questions or not item.supports:
                continue
            if "initial_observation_not_state_progress" in item.reasons:
                continue
            # The existing runner has already used method-local scores and fair rounds.
            group = item.group_key
            if item.method_id in {"text_topics", "research_tfidf_topics"}:
                terms = item.metrics.get("terms")
                group = (
                    {"exact_topic_terms": sorted(set(terms))}
                    if terms
                    else {
                        "exact_text_hashes": sorted(
                            {s.normalized_text_hash for s in item.supports if s.normalized_text_hash}
                        )
                    }
                )
            lead_key = canonical_hash(
                [MONITOR_VERSION, self.settings.monitor_is_demo, item.method_id, item.method_version, group]
            )
            support_hash = canonical_hash(
                sorted(
                    (s.record_ref.object_id, s.document_hash, s.normalized_text_hash) for s in item.supports
                )
            )
            with Session(self.engine.database) as session, session.begin():
                self._guard(self._state(session, lock=True))
                prior = session.scalar(
                    select(MonitorLead)
                    .where(
                        MonitorLead.workspace_id == self.actor.workspace_id, MonitorLead.lead_key == lead_key
                    )
                    .with_for_update()
                )
                if prior and prior.support_hash == support_hash:
                    # The lead may have committed before the batch's selection
                    # receipt. Rejoin this batch; only a later batch is a duplicate.
                    if prior.batch_id == identifier:
                        if item.candidate_key in selected_keys:
                            selected.append(prior.id)
                        if item.importance in {"medium", "high"}:
                            tracking.append(prior.id)
                    continue
                if prior and prior.state == "investigating" and prior.batch_id != identifier:
                    raise DomainError("monitor_lead_busy", "同一线索尚有未确认的保存步骤，先完成原批次", 409)
                candidate_payload = item.model_dump(mode="json")
                importance_reason, urgency_reason = self._lead_reasons(item, meaningful, prior is None)
                body = {
                    "importance_reason": importance_reason,
                    "urgency_reason": urgency_reason,
                    "candidate": candidate_payload,
                    "changed_supports": [s.record_ref.model_dump(mode="json") for s in meaningful],
                    "new_to_radar": prior is None,
                    "investigation_eligible": item.candidate_key in selected_keys,
                    "plan": self._candidate_followup_plan(payload, item),
                    "steps": [],
                    "material_receipts": [],
                    "queries": [],
                }
                direction = item.metrics.get("kdd_direction") or payload.get("policy", {}).get(
                    "kdd_direction"
                )
                if payload.get("historical_context"):
                    body["historical_context"] = copy.deepcopy(payload["historical_context"])
                if direction:
                    body["kdd_direction"] = direction
                    body["question"] = self._candidate_question(item, direction)
                    body["pattern_evaluation"] = next(
                        (
                            copy.deepcopy(value)
                            for value in result.pattern_evaluations
                            if value["candidate_key"] == item.candidate_key
                        ),
                        None,
                    )
                    body["selection_decision"] = next(
                        (
                            copy.deepcopy(value)
                            for value in result.selection_decisions
                            if value["candidate_key"] == item.candidate_key
                        ),
                        None,
                    )
                    body["dataset_hash"] = (result.data_preparation or {}).get("dataset_hash")
                if prior and prior.payload.get("auto_topic"):
                    body["auto_topic"] = copy.deepcopy(prior.payload["auto_topic"])
                if prior:
                    # A user's closed/paused investigation is never silently reopened.
                    if prior.investigation_id:
                        view = InvestigationService(self.engine).get(self.actor, prior.investigation_id)
                        if view.status != "open":
                            prior.state = "deferred"
                            continue
                    prior.payload, prior.support_hash = body, support_hash
                    prior.batch_id, prior.run_id, prior.state = (
                        identifier,
                        run_id,
                        "pending" if item.candidate_key in selected_keys else "deferred",
                    )
                    prior.updated_at = datetime.now(UTC)
                    lead_id = prior.id
                else:
                    lead_id = str(uuid4())
                    session.add(
                        MonitorLead(
                            id=lead_id,
                            workspace_id=self.actor.workspace_id,
                            lead_key=lead_key,
                            batch_id=identifier,
                            run_id=run_id,
                            state="pending" if item.candidate_key in selected_keys else "deferred",
                            support_hash=support_hash,
                            payload=body,
                            updated_at=datetime.now(UTC),
                        )
                    )
                if item.candidate_key in selected_keys:
                    selected.append(lead_id)
                if item.importance in {"medium", "high"}:
                    tracking.append(lead_id)
        payload["selected_leads"] = selected
        payload["tracking_leads"] = tracking
        with Session(self.engine.database) as session:
            actual = [
                Candidate.model_validate(lead.payload["candidate"])
                for lead in session.scalars(select(MonitorLead).where(MonitorLead.id.in_(selected)))
            ]
        payload["selection_summary"] = self._selection_summary(payload, result, actual)
        self._save_batch(identifier, state="investigating", payload=payload)

    def _save_lead(self, identifier, *, payload=None, state=None, investigation_id=None):
        with Session(self.engine.database) as session, session.begin():
            self._guard(self._state(session, lock=True))
            row = session.get(MonitorLead, identifier, with_for_update=True)
            if payload is not None:
                row.payload = payload
            if state is not None:
                row.state = state
            if investigation_id is not None:
                row.investigation_id = investigation_id
            row.updated_at = datetime.now(UTC)

    def _append(self, lead_id, view, kind, text, *, snapshot_id=None, record_ref=None, quote=None, tag):
        with Session(self.engine.database) as session:
            row = session.get(MonitorLead, lead_id)
            payload = copy.deepcopy(row.payload)
            batch_id, lead_key = row.batch_id, row.lead_key
        key = "monitor-entry:" + canonical_hash([lead_key, batch_id, tag])
        operation = "investigation.append:" + view.id
        saved = self._saved_response(operation, key)
        if saved is not None:
            return InvestigationService(self.engine).get(self.actor, view.id)
        intent = next((s for s in payload["steps"] if s["key"] == key), None)
        if intent is None:
            body = {"kind": kind, "expected_revision": view.revision, "text": text[:8000]}
            if kind == "material":
                body.update(
                    snapshot_id=snapshot_id,
                    record_ref=record_ref.model_dump(mode="json"),
                    quote=quote,
                    stance="context",
                )
            intent = {"key": key, "body": body}
            payload["steps"].append(intent)
            self._save_lead(lead_id, payload=payload, state="investigating")
        request = InvestigationEntryCreate.model_validate(intent["body"])
        _, saved = InvestigationService(self.engine).append(
            self.actor,
            view.id,
            request,
            key,
            automation=InvestigationAutomation(batch_id=batch_id, lead_key=lead_key),
        )
        return InvestigationService(self.engine).get(self.actor, saved["id"])

    def _reservation_origin(self, session, receipt):
        origin = (receipt.response_body or {}).get("origin")
        if origin in {"monitor", "topic"}:
            return origin
        # Old receipts predate this field. Existing topic attempts are identifiable
        # by their ordinary evaluation ID; other old attempts belong to the monitor.
        target = (receipt.response_body or {}).get("batch_id")
        if session.scalar(
            select(MonitorBatch.id).where(
                MonitorBatch.workspace_id == self.actor.workspace_id, MonitorBatch.id == target
            )
        ):
            return "monitor"
        from bioradar.topics.models import TopicEvaluationRecord, TopicScopeRecord

        topic = session.scalar(
            select(TopicEvaluationRecord.id)
            .join(TopicScopeRecord)
            .where(
                TopicScopeRecord.workspace_id == self.actor.workspace_id,
                TopicEvaluationRecord.id == target,
            )
        )
        return "topic" if topic else "monitor"

    def _has_runnable_rival(self, session, origin, now):
        """Only an unreserved, currently resumable opposite inquiry can claim a turn."""
        from bioradar.topics.models import TopicEvaluationRecord, TopicScopeRecord
        from bioradar.topics.service import TopicService

        def needs_slot(lead_key, target, urgency="routine"):
            key = "monitor-dig:" + canonical_hash([lead_key, target])
            existing = session.scalar(
                select(EngineIdempotency.id).where(
                    EngineIdempotency.workspace_id == self.actor.workspace_id,
                    EngineIdempotency.actor_id == self.actor.actor_id,
                    EngineIdempotency.operation == "monitor.dig.start",
                    EngineIdempotency.key == key,
                )
            )
            if existing:
                return False
            if urgency == "immediate":
                return True
            recent = session.scalar(
                select(EngineIdempotency.id)
                .where(
                    EngineIdempotency.workspace_id == self.actor.workspace_id,
                    EngineIdempotency.actor_id == self.actor.actor_id,
                    EngineIdempotency.operation == "monitor.dig.start",
                    EngineIdempotency.response_body["lead_key"].astext == lead_key,
                    EngineIdempotency.created_at
                    > now - timedelta(minutes=self.settings.monitor_cooldown_minutes),
                )
                .limit(1)
            )
            return recent is None

        def open_inquiry(identifier, create_key):
            if not identifier:
                saved = session.scalar(
                    select(EngineIdempotency.response_body).where(
                        EngineIdempotency.workspace_id == self.actor.workspace_id,
                        EngineIdempotency.actor_id == self.actor.actor_id,
                        EngineIdempotency.operation == "investigation.create",
                        EngineIdempotency.key == create_key,
                    )
                )
                identifier = saved.get("id") if saved else None
            if not identifier:
                return True
            return InvestigationService(self.engine)._row(session, self.actor, identifier)[0].status == "open"

        if origin == "monitor":
            candidates = session.execute(
                select(
                    TopicEvaluationRecord.id,
                    TopicScopeRecord.id.label("scope_id"),
                    TopicScopeRecord.topic_id,
                    TopicEvaluationRecord.followup["investigation_id"].astext.label("investigation_id"),
                )
                .join(TopicScopeRecord)
                .where(
                    TopicScopeRecord.workspace_id == self.actor.workspace_id,
                    TopicScopeRecord.is_current.is_(True),
                    TopicScopeRecord.paused.is_(False),
                    TopicEvaluationRecord.followup["status"].astext == "deferred",
                )
                .order_by(TopicEvaluationRecord.created_at, TopicEvaluationRecord.id)
                .execution_options(yield_per=32)
            )
            topics = TopicService(self.engine)
            scopes = {}
            for item in candidates:
                lead_key = canonical_hash(["topic", item.scope_id])
                if not needs_slot(lead_key, item.id):
                    continue
                # A scope's progress may be large. Keep it once per scan rather
                # than returning the same JSON for every historical evaluation.
                if item.scope_id not in scopes:
                    scopes[item.scope_id] = session.get(TopicScopeRecord, item.scope_id)
                scope = scopes[item.scope_id]
                if all(
                    scope.progress.get(source, {}).get("error_code")
                    for source in scope.definition["source_ids"]
                ):
                    continue
                try:
                    topics._evaluation_row(session, self.actor, item.topic_id, item.id)
                    if open_inquiry(item.investigation_id, "topic-investigation:" + item.scope_id):
                        return True
                except DomainError as exc:
                    if (
                        exc.code in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}
                        or exc.status_code == 404
                    ):
                        continue
                    raise
        else:
            candidates = session.scalars(
                select(MonitorLead)
                .join(MonitorBatch, MonitorLead.batch_id == MonitorBatch.id)
                .where(
                    MonitorLead.workspace_id == self.actor.workspace_id,
                    MonitorLead.state.in_(("pending", "deferred", "investigating")),
                    MonitorBatch.state.in_(("investigating", "completed")),
                    MonitorBatch.is_demo == self.settings.monitor_is_demo,
                    MonitorBatch.source_id.in_(self.permitted()),
                )
                .order_by(MonitorLead.updated_at, MonitorLead.id)
                .execution_options(yield_per=16)
            )
            checked_runs = set()
            for lead in candidates:
                if lead.payload.get("investigation_eligible") is False:
                    continue
                urgency = lead.payload.get("candidate", {}).get("urgency", "routine")
                if not needs_slot(lead.lead_key, lead.batch_id, urgency):
                    continue
                try:
                    if lead.run_id not in checked_runs:
                        self.engine._run(session, self.actor, lead.run_id)
                        checked_runs.add(lead.run_id)
                    if open_inquiry(lead.investigation_id, "monitor-investigation:" + lead.lead_key):
                        return True
                except DomainError as exc:
                    if (
                        exc.code in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}
                        or exc.status_code == 404
                    ):
                        continue
                    raise
        return False

    def reserve_investigation(self, lead_key, batch_id, urgency, *, origin="monitor"):
        if origin not in {"monitor", "topic"}:
            raise ValueError("Unsupported automatic investigation origin")
        key = "monitor-dig:" + canonical_hash([lead_key, batch_id])
        if self._saved_response("monitor.dig.start", key) is not None:
            return True
        self._catalog()
        with Session(self.engine.database) as session, session.begin():
            state = self._state(session, lock=True)
            self._guard(state)
            now = session.scalar(select(func.now()))
            base = select(EngineIdempotency).where(
                EngineIdempotency.workspace_id == self.actor.workspace_id,
                EngineIdempotency.actor_id == self.actor.actor_id,
                EngineIdempotency.operation == "monitor.dig.start",
            )
            # Admission pools are independent. Old missing-origin receipts remain
            # conservatively in the monitor pool; their saved responses are unchanged.
            pool = (
                base.where(EngineIdempotency.response_body["origin"].astext == "topic")
                if origin == "topic"
                else base.where(
                    or_(
                        EngineIdempotency.response_body["origin"].astext != "topic",
                        EngineIdempotency.response_body["origin"].astext.is_(None),
                    )
                )
            )
            limit = (
                self.settings.topic_hourly_investigations
                if origin == "topic"
                else self.settings.monitor_hourly_investigations
            )
            count = session.scalar(
                select(func.count()).select_from(
                    pool.where(EngineIdempotency.created_at >= now - timedelta(hours=1)).subquery()
                )
            )
            if count >= limit:
                return False
            if origin == "monitor":
                batch_count = session.scalar(
                    select(func.count()).select_from(
                        pool.where(EngineIdempotency.response_body["batch_id"].astext == batch_id).subquery()
                    )
                )
                if batch_count >= self.settings.monitor_investigations_per_batch:
                    return False
            latest = session.scalar(
                pool.where(EngineIdempotency.response_body["lead_key"].astext == lead_key)
                .order_by(EngineIdempotency.created_at.desc())
                .limit(1)
            )
            if (
                urgency != "immediate"
                and latest
                and latest.created_at > now - timedelta(minutes=self.settings.monitor_cooldown_minutes)
            ):
                return False
            self.engine._idempotent(
                session,
                self.actor,
                "monitor.dig.start",
                key,
                {"lead_key": lead_key, "batch_id": batch_id},
                lambda: (
                    201,
                    {
                        "lead_key": lead_key,
                        "batch_id": batch_id,
                        "started_at": now.isoformat(),
                        "origin": origin,
                    },
                ),
            )
            return True

    def _ensure_tracking(self, lead_id):
        from bioradar.topics.models import TopicCreate
        from bioradar.topics.service import TopicService
        from bioradar.topics.tracking import automatic_topic_definition

        with Session(self.engine.database) as session:
            lead = session.get(MonitorLead, lead_id)
            payload = copy.deepcopy(lead.payload)
            candidate = Candidate.model_validate(payload["candidate"])
            batch = session.get(MonitorBatch, lead.batch_id)
            snapshot = batch.payload.get("snapshot")
            if snapshot is None:
                return
            intent = payload.get("auto_topic")
            if intent and intent.get("status") == "created":
                return
            if intent is None:
                selected = {support.record_ref.version_key for support in candidate.supports}
                records = [
                    EngineRecord.model_validate(row)
                    for row in snapshot["records"]
                    if ProviderRef.model_validate(row["record_ref"]).version_key in selected
                ]
                definition = automatic_topic_definition(
                    candidate, records, source_instance=snapshot["source_instance"]
                )
                if definition is None:
                    return
                intent = {
                    "status": "queued",
                    "key": "monitor-topic:" + lead.lead_key,
                    "body": TopicCreate(definition=definition, is_demo=batch.is_demo).model_dump(mode="json"),
                    "origin": {
                        "source": "monitor",
                        "lead_id": lead.id,
                        "lead_key": lead.lead_key,
                        "batch_id": lead.batch_id,
                        "run_id": lead.run_id,
                    },
                }
                payload["auto_topic"] = intent
        self._save_lead(lead_id, payload=payload)
        try:
            _, created = TopicService(self.engine, self.reader).create(
                self.actor,
                TopicCreate.model_validate(intent["body"]),
                intent["key"],
                automation=intent["origin"],
            )
        except DomainError as exc:
            if exc.code not in {
                "automatic_topic_capacity",
                "source_not_authorized",
                "history_source_unavailable",
            }:
                raise
            intent.update(status="queued", error_code=exc.code)
        else:
            intent.update(status="created", topic_id=created["id"])
            intent.pop("error_code", None)
        with Session(self.engine.database) as session:
            latest = copy.deepcopy(session.get(MonitorLead, lead_id).payload)
        latest["auto_topic"] = intent
        self._save_lead(lead_id, payload=latest)

    def _drain_tracking(self):
        with Session(self.engine.database) as session:
            identifiers = list(
                session.scalars(
                    select(MonitorLead.id)
                    .join(MonitorBatch)
                    .where(
                        MonitorLead.workspace_id == self.actor.workspace_id,
                        MonitorLead.payload["auto_topic"]["status"].astext == "queued",
                        MonitorBatch.source_id.in_(self.permitted()),
                        MonitorBatch.is_demo == self.settings.monitor_is_demo,
                    )
                    .order_by(MonitorLead.created_at)
                    .limit(8)
                )
            )
        for identifier in identifiers:
            self._ensure_tracking(identifier)

    def drain_deferred(self):
        with Session(self.engine.database) as session:
            identifiers = list(
                session.scalars(
                    select(MonitorLead.id)
                    .join(MonitorBatch, MonitorLead.batch_id == MonitorBatch.id)
                    .where(
                        MonitorLead.workspace_id == self.actor.workspace_id,
                        MonitorLead.state.in_(("deferred", "investigating")),
                        or_(
                            MonitorLead.payload["investigation_eligible"].astext != "false",
                            MonitorLead.payload["investigation_eligible"].astext.is_(None),
                        ),
                        MonitorBatch.state == "completed",
                        MonitorBatch.source_id.in_(self.permitted()),
                        MonitorBatch.is_demo == self.settings.monitor_is_demo,
                    )
                    .order_by(MonitorLead.updated_at)
                    .limit(8)
                )
            )
        for identifier in identifiers:
            try:
                result = self.investigate(identifier)
            except DomainError as exc:
                temporary = (
                    exc.code
                    in SOURCE_READ_UNAVAILABLE
                    | {
                        "source_not_authorized",
                        "history_read_failed",
                        "history_not_connected",
                    }
                    or exc.status_code == 503
                )
                repairable = exc.code in {"revision_conflict", "investigation_closed", "monitor_lead_busy"}
                if not (temporary or repairable or exc.status_code in {422, 502}):
                    raise
                with Session(self.engine.database) as session:
                    payload = copy.deepcopy(session.get(MonitorLead, identifier).payload)
                payload["error_code"] = exc.code
                self._save_lead(identifier, payload=payload, state="deferred" if temporary else "blocked")
                continue
            if result:
                return

    def investigate(self, lead_id):
        with Session(self.engine.database) as session:
            row = session.get(MonitorLead, lead_id)
            p = copy.deepcopy(row.payload)
            candidate = Candidate.model_validate(p["candidate"])
            batch_id, run_id, lead_key, investigation_id = (
                row.batch_id,
                row.run_id,
                row.lead_key,
                row.investigation_id,
            )
        if p.get("investigation_eligible") is False and not investigation_id:
            self._save_lead(lead_id, state="deferred")
            return None
        _, _, _, batch, snapshot_id, _ = self._load_batch(batch_id)
        runtime = InvestigationService(self.engine)
        run = self.engine.get_run(self.actor, run_id)
        view = runtime.get(self.actor, investigation_id) if investigation_id else None
        if view is not None and view.status != "open":
            self._save_lead(lead_id, state="deferred")
            return None
        reservation = self._saved_response(
            "monitor.dig.start", "monitor-dig:" + canonical_hash([lead_key, batch_id])
        )
        if reservation is None and not (candidate.metrics.get("attention") or {}).get(
            "automatic_investigation_allowed"
        ):
            self._save_lead(
                lead_id, state="deferred", payload={**p, "error_code": "unqualified_automatic_signal"}
            )
            return None
        if not self.reserve_investigation(lead_key, batch_id, candidate.urgency):
            self._save_lead(lead_id, state="deferred")
            return None
        if view is None:
            request = InvestigationCreate(
                title=candidate.title[:200],
                question=p.get("question") or self._candidate_question(candidate, p.get("kdd_direction")),
                questions=list(candidate.next_questions[:32]),
                is_demo=self.settings.monitor_is_demo,
                origin=InvestigationOriginCreate(
                    run_id=run_id, result_hash=run.result.result_hash, candidate_key=candidate.candidate_key
                ),
            )
            key = "monitor-investigation:" + lead_key
            saved = self._saved_response("investigation.create", key)
            if saved is None:
                _, saved = runtime.create(self.actor, request, key)
            investigation_id = saved["id"]
            self._save_lead(lead_id, investigation_id=investigation_id, state="investigating")
            view = runtime.get(self.actor, investigation_id)
        if view.status != "open":
            self._save_lead(lead_id, state="deferred")
            return None
        view = self._append(
            lead_id,
            view,
            "note",
            "程序根据新增采用版本继续核对；采用、补录和来源修订不等于刚发生的科研进展。"
            + "；".join(candidate.reasons)
            + "。尚未专业评价，不签研究判断。",
            tag="candidate",
        )
        # Only actual support refs from the fixed mining snapshot are adopted.
        attention = candidate.metrics.get("attention") or {}
        priority = {
            canonical_hash(row["record_ref"])
            for row in attention.get("corrections", [])
            if row.get("record_ref")
        } | {canonical_hash(ref) for ref in attention.get("reviewed_counterevidence_refs", [])}
        supports = sorted(candidate.supports, key=lambda s: canonical_hash(s.record_ref) not in priority)
        for support in supports:
            view = self._append(
                lead_id,
                view,
                "material",
                "后台线索的固定依据；仅作为待核背景。",
                snapshot_id=snapshot_id,
                record_ref=support.record_ref,
                quote=support.quote[:4000] if support.quote else None,
                tag="support:" + canonical_hash(support.record_ref),
            )
        from bioradar.research.investigations.analysis_models import AnalysisCreate
        from bioradar.research.investigations.analysis_service import AnalysisService, semantic_enabled

        if semantic_enabled(self.settings):
            with Session(self.engine.database) as session:
                latest = copy.deepcopy(session.get(MonitorLead, lead_id).payload)
            intent = latest.get("analysis_intent")
            if intent is None:
                if latest.get("kdd_direction"):
                    analysis_request = AnalysisCreate(
                        expected_revision=view.revision,
                        question=latest.get("question")
                        or self._candidate_question(candidate, latest["kdd_direction"]),
                        auto_retrieve=True,
                        kdd_direction=latest["kdd_direction"],
                    )
                else:
                    analysis_request = AnalysisCreate(
                        expected_revision=view.revision,
                        question=self._candidate_question(candidate),
                        auto_retrieve=True,
                    )
                intent = {
                    "key": "monitor-analysis:" + canonical_hash([lead_key, batch_id]),
                    "body": analysis_request.model_dump(mode="json"),
                }
                if batch.get("historical_context"):
                    intent["historical_context"] = copy.deepcopy(batch["historical_context"])
                    intent["body"]["source_ids"] = list(batch["historical_sampling"]["source_ids"])
                latest["analysis_intent"] = intent
                self._save_lead(lead_id, payload=latest)
            _, analysis = AnalysisService(self.engine, self.reader).start(
                self.actor,
                investigation_id,
                AnalysisCreate.model_validate(intent["body"]),
                intent["key"],
                historical_context=intent.get("historical_context"),
                origin={
                    "source": "monitor",
                    "batch_id": batch_id,
                    "lead_key": lead_key,
                    "run_id": run_id,
                    "reservation_key": "monitor-dig:" + canonical_hash([lead_key, batch_id]),
                },
            )
            latest["analysis_id"] = analysis["id"]
            self._save_lead(lead_id, payload=latest, state="investigated")
            return investigation_id
        for index, plan in enumerate(p["plan"][:2]):
            with Session(self.engine.database) as session:
                latest = copy.deepcopy(session.get(MonitorLead, lead_id).payload)
            receipt = next((q for q in latest["queries"] if q["index"] == index), None)
            if receipt is None:
                intents = latest.setdefault("query_intents", [])
                intent = next((q for q in intents if q["index"] == index), None)
                if intent is None:
                    try:
                        found = search_records(
                            self.reader,
                            source_id=plan["source_id"],
                            is_demo=self.settings.monitor_is_demo,
                            term=plan["term"],
                            **(
                                {
                                    "mode": batch["historical_context"]["mode"],
                                    "cutoff": datetime.fromisoformat(batch["historical_context"]["cutoff"]),
                                }
                                if batch.get("historical_context")
                                else {}
                            ),
                        )
                    except DomainError as exc:
                        receipt = {
                            "index": index,
                            "status": "failed",
                            "plan": plan,
                            "error_code": exc.code,
                            "scope": "fabric_adopted_versions_not_the_web",
                        }
                    else:
                        adopted_snapshot = self.reader.snapshot(found)
                        intent = {
                            "index": index,
                            "key": "monitor-followup:" + canonical_hash([lead_key, batch_id, index]),
                            "snapshot": adopted_snapshot.model_dump(mode="json")
                            if adopted_snapshot
                            else None,
                            "receipt": {
                                "index": index,
                                "status": "read" if found["items"] else "empty",
                                "plan": plan,
                                "scope": "fabric_adopted_versions_literal_title_or_clean_text",
                                "returned": len(found["items"]),
                                "coverage": found["coverage"],
                                "records": [r.model_dump(mode="json") for r in adopted_snapshot.records]
                                if adopted_snapshot
                                else [],
                                "fact_summary": summarize_records(found["items"], limit=3),
                            },
                        }
                        intents.append(intent)
                        # Freeze the input BEFORE a local adopt that might commit without a reply.
                        self._save_lead(lead_id, payload=latest)
                if intent is not None:
                    adopted = None
                    if intent["snapshot"] is not None:
                        adopted = self._saved_response("snapshot.create", intent["key"])
                        if adopted is None:
                            _, adopted = self.engine.adopt_snapshot(
                                self.actor,
                                NormalizedSnapshot.model_validate(intent["snapshot"]),
                                intent["key"],
                            )
                    receipt = {**intent["receipt"], "snapshot_id": adopted["id"] if adopted else None}
                latest["queries"].append(receipt)
                self._save_lead(lead_id, payload=latest)
            view = self._append(
                lead_id,
                view,
                "note",
                "自动补查回执（空结果不等于不存在或失败）："
                + json.dumps({k: v for k, v in receipt.items() if k != "records"}, ensure_ascii=False)[:7300],
                tag="query:" + str(index),
            )
            for record in receipt.get("records", []):
                value = EngineRecord.model_validate(record)
                view = self._append(
                    lead_id,
                    view,
                    "material",
                    "按前轮真实字段派生查询取得，关系待人工核对。",
                    snapshot_id=receipt["snapshot_id"],
                    record_ref=value.record_ref,
                    tag="query-material:" + str(index) + ":" + canonical_hash(value.record_ref),
                )
        if not p["plan"]:
            view = self._append(
                lead_id,
                view,
                "note",
                "没有可核对来源字段派生的受支持查询；未自动猜关键词，保留缺口供研究人员继续。",
                tag="no-query-plan",
            )
        with Session(self.engine.database) as session:
            latest = copy.deepcopy(session.get(MonitorLead, lead_id).payload)
        latest.pop("error_code", None)
        self._save_lead(lead_id, payload=latest, state="investigated")
        return investigation_id

    def _propagate_context_updates(self, identifier, payload, snapshot_id):
        """Append current versions to exactly linked existing investigations, even without a new lead."""
        if payload.get("historical_context"):
            # Historical revisions belong to the new day's analyses, never current auto-updates.
            payload["context_updates_complete"] = True
            return True
        if payload.get("context_updates_complete"):
            return True
        refs = [ProviderRef.model_validate(ref) for ref in payload.get("context_update_refs", [])]
        if not refs or not snapshot_id:
            payload["context_updates_complete"] = True
            return True
        material_tests, origin_tests = [], []
        for ref in refs:
            fields = ref.model_dump(include={"provider", "object_type", "object_id"})
            material_tests.extend(
                [
                    InvestigationEntryRecord.payload.contains({"support": {"record_ref": fields}}),
                    InvestigationEntryRecord.payload.contains({"support": {"evidence_refs": [fields]}}),
                ]
            )
            origin_tests.extend(
                [
                    InvestigationRecord.origin.contains(
                        {"candidate": {"supports": [{"record_ref": fields}]}}
                    ),
                    InvestigationRecord.origin.contains(
                        {"candidate": {"supports": [{"evidence_refs": [fields]}]}}
                    ),
                ]
            )
        with Session(self.engine.database) as session:
            batch = session.get(MonitorBatch, identifier)
            linked = select(InvestigationEntryRecord.investigation_id).where(
                InvestigationEntryRecord.kind == "material", or_(*material_tests)
            )
            statement = select(InvestigationRecord.id).where(
                InvestigationRecord.workspace_id == self.actor.workspace_id,
                InvestigationRecord.is_demo == self.settings.monitor_is_demo,
                InvestigationRecord.created_at <= batch.created_at,
                or_(InvestigationRecord.id.in_(linked), *origin_tests),
            )
            if payload.get("context_update_cursor"):
                statement = statement.where(InvestigationRecord.id > payload["context_update_cursor"])
            identifiers = list(session.scalars(statement.order_by(InvestigationRecord.id).limit(8)))
        runtime = InvestigationService(self.engine)
        for investigation_id in identifiers:
            try:
                view = runtime.get(self.actor, investigation_id)
            except DomainError as exc:
                if exc.code not in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}:
                    raise
                payload.setdefault("context_update_gaps", []).append(
                    {"status": "source_unavailable", "error_code": exc.code}
                )
                payload["context_update_cursor"] = investigation_id
                self._save_batch(identifier, payload=payload)
                continue
            if view.status != "open":
                payload.setdefault("context_update_gaps", []).append({"status": "user_paused_or_closed"})
                payload["context_update_cursor"] = investigation_id
                self._save_batch(identifier, payload=payload)
                continue
            supports = [entry.payload.support for entry in view.entries if entry.kind == "material"]
            if view.origin:
                supports.extend(view.origin.candidate.supports)
            adopted = [ref for support in supports for ref in (support.record_ref, *support.evidence_refs)]
            identities, versions = (
                {r.identity_key for r in adopted},
                {r.version_key for r in adopted},
            )
            for ref in refs:
                if ref.identity_key not in identities or ref.version_key in versions:
                    continue
                key = "monitor-context:" + canonical_hash([investigation_id, ref.version_key])
                operation = "investigation.append:" + investigation_id
                saved = self._saved_response(operation, key)
                if saved is None:
                    intents = payload.setdefault("context_update_intents", [])
                    intent = next((item for item in intents if item["key"] == key), None)
                    if intent is None:
                        intent = {
                            "key": key,
                            "investigation_id": investigation_id,
                            "body": {
                                "kind": "material",
                                "expected_revision": view.revision,
                                "snapshot_id": snapshot_id,
                                "record_ref": ref.model_dump(mode="json"),
                                "quote": None,
                                "stance": "context",
                                "text": "程序核对到本调查已采用来源的另一固定版本。新版本可能是正文修订、"
                                "身份审核或关系范围调整；不代表新研究、疗效变化或研究对象被删除。"
                                "旧材料和判断保留，请重新检查依赖该来源的判断条件。",
                            },
                        }
                        intents.append(intent)
                        self._save_batch(identifier, payload=payload)
                    if (
                        intent.get("waiting_for_analysis")
                        and intent["body"]["expected_revision"] != view.revision
                    ):
                        intent.setdefault("revision_rebases", []).append(
                            {
                                "from": intent["body"]["expected_revision"],
                                "to": view.revision,
                                "reason": "analysis_in_progress_append_not_written",
                            }
                        )
                        intent["body"]["expected_revision"] = view.revision
                        self._save_batch(identifier, payload=payload)
                    try:
                        _, saved = runtime.append(
                            self.actor,
                            investigation_id,
                            InvestigationEntryCreate.model_validate(intent["body"]),
                            key,
                            automation=InvestigationAutomation(
                                batch_id=identifier,
                                lead_key=canonical_hash(["source_change", ref.identity_key]),
                            ),
                        )
                    except DomainError as exc:
                        if exc.code != "analysis_in_progress":
                            raise
                        intent["waiting_for_analysis"] = True
                        payload["context_update_waiting"] = {
                            "code": exc.code,
                            "investigation_id": investigation_id,
                            "intent_key": key,
                        }
                        self._save_batch(
                            identifier,
                            payload=payload,
                            state="investigating",
                            error=exc.code,
                        )
                        return False
                    intent.pop("waiting_for_analysis", None)
                    payload.pop("context_update_waiting", None)
                view = runtime.get(self.actor, saved["id"])
                versions.add(ref.version_key)
                if investigation_id not in payload["investigation_ids"]:
                    payload["investigation_ids"].append(investigation_id)
            payload["context_update_cursor"] = investigation_id
            self._save_batch(identifier, payload=payload)
        payload["context_updates_complete"] = len(identifiers) < 8
        self._save_batch(identifier, payload=payload)
        return payload["context_updates_complete"]

    def _complete(self, identifier, payload):
        with Session(self.engine.database) as session, session.begin():
            state = self._state(session, lock=True)
            self._guard(state)
            row = session.get(MonitorBatch, identifier, with_for_update=True)
            values = copy.deepcopy(state.source_progress)
            if payload.get("historical_context"):
                replay = values["_history_replay"]
                day = replay["pending_day"]
                receipt = day["collections"].get(row.source_id, {})
                if (
                    day["cutoff"] != payload["historical_context"]["cutoff"]
                    or receipt.get("batch_id") != identifier
                ):
                    raise DomainError("history_replay_conflict", "固定历史日或批次已变化", 409)
                receipt["status"], receipt["run_id"] = "completed", row.run_id
                if row.run_id and row.run_id not in day["run_ids"]:
                    day["run_ids"].append(row.run_id)
                state.source_progress = values
                row.payload, row.state, row.updated_at = payload, "completed", datetime.now(UTC)
                return
            stream = self.stream_key(row.source_id)
            source = values.get(stream, {})
            if source.get("active_batch_id") != identifier:
                raise DomainError("monitor_cursor_conflict", "监测游标已变化，不能跳过批次", 409)
            values[stream] = {
                **source,
                "active_batch_id": None,
                "acked_after": payload["next_after"],
                "scan_after": payload["after"] if payload["next_cursor"] else payload["next_after"],
                "scan_cursor": payload["next_cursor"],
                "scan_max_seq": payload["max_seq"] if payload["next_cursor"] else None,
                "state": "idle",
                "last_success_at": datetime.now(UTC).isoformat(),
                "error_code": None,
            }
            state.source_progress = values
            row.payload, row.state, row.updated_at = payload, "completed", datetime.now(UTC)

    def advance(self, identifier):
        _, _, state, payload, snapshot_id, run_id = self._load_batch(identifier)
        if state == "accepted":
            self._adopt_and_queue(identifier, payload)
            return
        if state == "queued":
            run = self.engine.get_run(self.actor, run_id)
            if run.state in {"queued", "running"}:
                self.engine.dispatch(run_id)
                return
            if (
                run.state == "failed"
                or run.result
                and any(m.execution_status == "failed" for m in run.result.method_results)
            ):
                payload["selection_summary"] = self._selection_summary(payload, run.result)
                self._save_batch(identifier, payload=payload)
                if run.state == "failed" and run.attempt < self.settings.engine_retry_limit:
                    key = "monitor-retry:" + identifier + ":" + str(run.attempt)
                    saved = self._saved_response("run.retry:" + run_id, key)
                    if saved is None:
                        self.engine.retry_run(
                            self.actor,
                            run_id,
                            RunAction(action="retry_failed", expected_revision=run.revision),
                            key,
                        )
                    self.engine.dispatch(run_id)
                else:
                    self._save_batch(identifier, state="blocked", error="mining_failed_no_cursor_advance")
                return
            if run.result is None:
                self._save_batch(identifier, state="blocked", error="mining_result_missing")
                return
            self._select_leads(identifier, run_id, payload, run.result)
            _, _, _, payload, _, _ = self._load_batch(identifier)
        if state == "investigating" or payload.get("selected_leads") is not None:
            for lead_id in dict.fromkeys([*payload.get("tracking_leads", []), *payload["selected_leads"]]):
                self._ensure_tracking(lead_id)
            if not self._propagate_context_updates(identifier, payload, snapshot_id):
                return
            for lead_id in payload["selected_leads"]:
                with Session(self.engine.database) as session:
                    lead = session.get(MonitorLead, lead_id)
                    finished = lead.state in {"investigated", "deferred"}
                if not finished:
                    investigation_id = self.investigate(lead_id)
                    if investigation_id and investigation_id not in payload["investigation_ids"]:
                        payload["investigation_ids"].append(investigation_id)
                        self._save_batch(identifier, payload=payload)
            self._complete(identifier, payload)

    def _refresh_lead_append_precondition(self, actor, lead_id, *, state=None):
        with Session(self.engine.database) as session:
            lead = session.get(MonitorLead, lead_id)
            body, investigation_id = copy.deepcopy(lead.payload), lead.investigation_id
        if investigation_id:
            current = InvestigationService(self.engine).get(actor, investigation_id)
            if current.status != "open":
                raise DomainError("investigation_closed", "请先由研究人员明确恢复调查，后台不会自动开启", 409)
            for step in body.get("steps", []):
                operation = "investigation.append:" + investigation_id
                if self._saved_response(operation, step["key"]) is None:
                    # Refresh only the failed revision precondition.
                    step["body"]["expected_revision"] = current.revision
        body.pop("error_code", None)
        self._save_lead(lead_id, payload=body, state=state)

    def _retry_completed_leads(self, actor, identifier, payload):
        with Session(self.engine.database) as session:
            leads = list(
                session.scalars(
                    select(MonitorLead).where(
                        MonitorLead.batch_id == identifier, MonitorLead.state == "blocked"
                    )
                )
            )
            if any(
                lead.payload.get("error_code")
                not in {"revision_conflict", "investigation_closed", "monitor_lead_busy"}
                for lead in leads
            ):
                raise DomainError(
                    "monitor_retry_unsupported", "该线索输入错误需要先人工核对，未重复执行", 422
                )
            identifiers = [lead.id for lead in leads]
        if not identifiers:
            if payload.get("deferred_retry_requested"):
                return {"id": identifier, "state": "completed", "already_requested": True}
            raise DomainError("monitor_retry_state", "该已确认批次没有可恢复的阻塞查证", 409)
        self.allow_paused = True
        if not self.claim():
            raise DomainError("monitor_busy", "后台正在处理，请稍后核对，未重复执行", 409)
        try:
            payload["deferred_retry_requested"] = True
            self._save_batch(identifier, payload=payload)
            for lead_id in identifiers:
                self._refresh_lead_append_precondition(actor, lead_id, state="deferred")
            return {
                "id": identifier,
                "state": "completed",
                "same_fixed_input": True,
                "released_leads": len(identifiers),
            }
        finally:
            self.release()

    def retry_batch(self, actor, identifier):
        """Explicit repair of the same fixed input, including an uncertain prior retry receipt."""
        self._actor(actor)
        _, source, state, payload, snapshot_id, run_id = self._load_batch(identifier)
        if source not in self.permitted():
            raise DomainError("source_not_authorized", "本批来源当前不可读取", 403)
        if snapshot_id:
            self.engine.get_snapshot(actor, snapshot_id)
        if state in {"blocked", "completed"} and run_id:
            from bioradar.research.engine.service import computation_versions

            with Session(self.engine.database) as session:
                stored = session.get(EngineRun, run_id)
                if (
                    computation_versions(tuple(stored.policy["method_ids"]), policy=stored.policy)
                    != stored.method_versions
                ):
                    raise DomainError(
                        "method_version_changed", "方法版本已变，不能用新算法重写原固定批次", 409
                    )
        if state == "completed":
            self.engine.get_run(actor, run_id)
            return self._retry_completed_leads(actor, identifier, payload)
        if state != "blocked":
            if state in {"accepted", "queued", "investigating"} and payload.get("retry_intent"):
                return {"id": identifier, "state": state, "already_requested": True}
            raise DomainError("monitor_retry_state", "只有已阻塞批次可核对恢复", 409)
        with Session(self.engine.database) as session:
            error = session.get(MonitorBatch, identifier).error_code
        if error not in {
            "mining_failed_no_cursor_advance",
            "revision_conflict",
            "monitor_lead_busy",
            "investigation_closed",
        }:
            raise DomainError(
                "monitor_retry_unsupported",
                "该固定输入错误不能自动修复；旧批次保留，请先核对来源或保存预算，不能跳过游标",
                422,
            )
        self.allow_paused = True
        if not self.claim():
            raise DomainError("monitor_busy", "后台正在处理，请稍后核对，未重复执行", 409)
        try:
            run = self.engine.get_run(actor, run_id) if run_id else None
            target = "queued"
            if error == "mining_failed_no_cursor_advance":
                intent = payload.get("retry_intent")
                if intent is None:
                    intent = {
                        "key": "monitor-retry:" + identifier + ":" + str(run.attempt),
                        "expected_revision": run.revision,
                    }
                    payload["retry_intent"] = intent
                    self._save_batch(identifier, payload=payload)
                if self._saved_response("run.retry:" + run_id, intent["key"]) is None:
                    self.engine.retry_run(
                        actor,
                        run_id,
                        RunAction(action="retry_failed", expected_revision=intent["expected_revision"]),
                        intent["key"],
                    )
            else:
                target = "investigating"
                payload["retry_intent"] = {"kind": "reconcile_local_append"}
                for intent in payload.get("context_update_intents", []):
                    current = InvestigationService(self.engine).get(actor, intent["investigation_id"])
                    if self._saved_response("investigation.append:" + current.id, intent["key"]) is None:
                        intent["body"]["expected_revision"] = current.revision
                for lead_id in payload.get("selected_leads", []):
                    self._refresh_lead_append_precondition(actor, lead_id)
            self._save_batch(identifier, payload=payload, state=target)
            with Session(self.engine.database) as session, session.begin():
                row = self._state(session, lock=True)
                self._guard(row)
                values = copy.deepcopy(row.source_progress)
                stream = self.stream_key(source)
                values[stream] = {**values[stream], "state": target, "error_code": None}
                row.source_progress = values
            if run_id and target == "queued":
                self.engine.dispatch(run_id)
            return {"id": identifier, "state": target, "same_fixed_input": True}
        finally:
            self.release()

    def tick(self):
        if not self.settings.monitor_enabled or not DevelopmentAccess(self.settings).enabled:
            return {"state": "disabled"}
        with Session(self.engine.database) as session:
            if self._state(session).source_progress.get("_history_replay"):
                return {"state": "history_step_required"}
        if not self.claim():
            return {"state": "paused_or_busy"}
        error, source, allowed, offset = None, None, [], 0
        try:
            allowed = sorted(self.permitted())
            if not allowed:
                return {"state": "no_permitted_sources"}
            self.engine.recover(limit=30)
            # Recovery belongs to the selected source's active batch below.
            # A revoked dependency in one batch must not monopolize every tick.
            self._drain_tracking()
            self.drain_deferred()
            with Session(self.engine.database) as session:
                row = self._state(session)
                offset = row.next_source % len(allowed)
                progress = copy.deepcopy(row.source_progress)
                pending = session.scalar(
                    select(func.count())
                    .select_from(EngineRun)
                    .where(
                        EngineRun.workspace_id == self.actor.workspace_id,
                        EngineRun.state.in_(("queued", "running")),
                    )
                )
            source = allowed[offset]
            current = progress.get(self.stream_key(source), {})
            active = current.get("active_batch_id")
            if active:
                _, _, state, _, _, _ = self._load_batch(active)
                if state != "blocked":
                    self.advance(active)
            elif pending < self.settings.monitor_pending_runs:
                identifier = self.accept_page(source)
                if identifier:
                    self.advance(identifier)
            return {"state": "polled", "source_id": source}
        except DomainError as exc:
            error = exc.code
            if source and (
                exc.status_code == 422
                or exc.code in {"revision_conflict", "monitor_lead_busy", "investigation_closed"}
            ):
                with Session(self.engine.database) as session:
                    active = (
                        self._state(session)
                        .source_progress.get(self.stream_key(source), {})
                        .get("active_batch_id")
                    )
                if active:
                    self._save_batch(active, state="blocked", error=error)
            return {
                "state": "source_unavailable" if exc.status_code in {403, 502, 503} else "blocked",
                "error_code": exc.code,
            }
        finally:
            if source:
                with Session(self.engine.database) as session, session.begin():
                    row = self._state(session, lock=True)
                    if row.lease_token == self.token:
                        row.next_source = (offset + 1) % len(allowed)
                        values = copy.deepcopy(row.source_progress)
                        current = values.get(self.stream_key(source), {})
                        active = current.get("active_batch_id")
                        batch = session.get(MonitorBatch, active) if active else None
                        values[self.stream_key(source)] = {
                            **current,
                            "source_id": source,
                            "is_demo": self.settings.monitor_is_demo,
                            "last_poll_at": datetime.now(UTC).isoformat(),
                            "error_code": error,
                            "state": batch.state if batch else ("unavailable" if error else "idle"),
                        }
                        row.source_progress = values
            self.release(error)
