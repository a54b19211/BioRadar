"""Ordinary saved studies and append-only evaluations, outside analysis_runs."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.api.errors import DomainError
from bioradar.data.database import Base
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.history.compute import horizon_end, make_predictions, score_predictions
from bioradar.research.history.facts import summarize_records
from bioradar.research.history.models import (
    HISTORY_COMPUTE_VERSION,
    HistoryPrediction,
    HistoryStudyCreate,
    HistoryStudySummary,
    HistoryStudyView,
)
from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import MiningPolicy, ResourceLimits, canonical_hash
from bioradar.research.mining.runner import run_mining


class HistoricalStudyRecord(Base):
    __tablename__ = "historical_studies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    prediction_hash: Mapped[str | None] = mapped_column(String(64))
    investigation_id: Mapped[str | None] = mapped_column(ForeignKey("investigations.id"))


class HistoricalEvaluationRecord(Base):
    __tablename__ = "historical_evaluations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("historical_studies.id"), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prediction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_hash: Mapped[str | None] = mapped_column(String(64))


@contextmanager
def history_reader(settings, request=None):
    if settings.data_fabric_url is None or settings.data_fabric_history_read_token is None:
        raise DomainError("history_not_connected", "尚未配置历史资料读取服务和只读许可", 503)
    with httpx.Client(
        base_url=settings.data_fabric_url,
        trust_env=False,
        headers={"Authorization": "Bearer " + settings.data_fabric_history_read_token.get_secret_value()},
    ) as client:
        yield FabricHistoryReader(
            client,
            authorization_ref=DevelopmentAccess(settings).actor().grant_ref,
            max_records=request.max_records if request else settings.engine_max_records,
            max_bytes=request.max_bytes if request else settings.engine_max_snapshot_bytes,
            max_seconds=request.max_seconds if request else 5,
        )


class HistoryService:
    def __init__(self, engine, reader=None):
        self.engine = engine
        self.reader = reader

    def _authorize(self, actor, source_id, is_demo):
        if actor != DevelopmentAccess(self.engine.settings).actor():
            raise DomainError("permission_denied", "研究授权上下文不匹配", 403)
        instance = "fixture" if is_demo else "datafabric-test"
        if (
            instance not in actor.source_allowlist
            or source_id not in self.engine.settings.engine_source_id_allowlist.get(instance, [])
        ):
            raise DomainError("source_not_authorized", "历史来源不在当前采用许可范围", 403)

    @property
    def reader(self):
        return self._reader

    @reader.setter
    def reader(self, value):
        self._reader = value
        self.engine.history_source_reader = value.sources if value is not None else None

    def _check_online_source(self, source_id):
        self.engine.authorize_history_source(source_id)

    def _row(self, session, actor, identifier, *, lock=False):
        query = select(HistoricalStudyRecord).where(
            HistoricalStudyRecord.id == identifier,
            HistoricalStudyRecord.workspace_id == actor.workspace_id,
        )
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise DomainError("not_found", "未找到可访问的历史研究", 404)
        self._authorize(actor, row.source_id, row.is_demo)
        for binding in row.payload.get("snapshots", []):
            saved = self.engine._snapshot(session, actor, binding["snapshot_id"])
            if saved.snapshot_hash != binding["snapshot_hash"]:
                raise DomainError("history_integrity_error", "历史采用证据版本不一致", 502)
        return row

    def _view(self, session, row):
        from bioradar.research.history.analysis import semantic_summaries

        request = HistoryStudyCreate.model_validate(row.request)
        evaluation_rows = list(
            session.scalars(
                select(HistoricalEvaluationRecord)
                .where(HistoricalEvaluationRecord.study_id == row.id)
                .order_by(HistoricalEvaluationRecord.created_at.desc(), HistoricalEvaluationRecord.id)
                .limit(20)
            )
        )
        payload = row.payload
        return HistoryStudyView(
            id=row.id,
            title=request.title,
            source_id=row.source_id,
            is_demo=row.is_demo,
            state=row.state,
            created_at=row.created_at,
            target=request.target,
            cutoff=request.cutoff,
            prediction_hash=row.prediction_hash,
            investigation_id=row.investigation_id,
            request=request,
            compute_version=payload.get("compute_version", HISTORY_COMPUTE_VERSION),
            predictions=payload.get("predictions", []),
            cohort=payload.get("cohort"),
            training=payload.get("training", []),
            sources=payload.get("snapshots", []),
            queries=payload.get("queries", []),
            mining=payload.get("mining"),
            fact_summary=payload.get("fact_summary"),
            next_questions=payload.get("next_questions", []),
            limitations=payload.get("limitations", []),
            semantic_analyses=semantic_summaries(session, row),
            evaluations=tuple(
                {
                    "id": e.id,
                    "state": e.state,
                    "cutoff": e.cutoff.isoformat(),
                    "prediction_hash": e.prediction_hash,
                    "result_hash": e.result_hash,
                    "metrics": e.payload.get("metrics"),
                    "limitations": e.payload.get("limitations", []),
                }
                for e in evaluation_rows
            ),
        )

    def get(self, actor, identifier):
        with Session(self.engine.database) as session:
            row = self._row(session, actor, identifier)
            self._check_online_source(row.source_id)
            return self._view(session, row)

    def list(self, actor, limit=30):
        if actor != DevelopmentAccess(self.engine.settings).actor():
            raise DomainError("permission_denied", "研究授权上下文不匹配", 403)
        with Session(self.engine.database) as session:
            rows = session.scalars(
                select(HistoricalStudyRecord)
                .where(HistoricalStudyRecord.workspace_id == actor.workspace_id)
                .order_by(HistoricalStudyRecord.created_at.desc(), HistoricalStudyRecord.id)
                .limit(limit)
            )
            result = []
            for row in rows:
                try:
                    self._authorize(actor, row.source_id, row.is_demo)
                except DomainError:
                    continue
                request = HistoryStudyCreate.model_validate(row.request)
                result.append(
                    HistoryStudySummary(
                        id=row.id,
                        title=request.title,
                        source_id=row.source_id,
                        is_demo=row.is_demo,
                        state=row.state,
                        created_at=row.created_at,
                        target=request.target,
                        cutoff=request.cutoff,
                        prediction_hash=row.prediction_hash,
                        investigation_id=row.investigation_id,
                    )
                )
            return {
                "items": [r.model_dump(mode="json") for r in result],
                "scope": "metadata_only_source_content_requires_current_upstream_permission",
            }

    def _save(self, actor, identifier, payload, *, state="collecting", prediction_hash=None):
        if (
            len(json.dumps(payload, ensure_ascii=False).encode())
            > self.engine.settings.engine_max_result_bytes
        ):
            raise DomainError("history_result_budget", "历史结果达到保存预算，未伪装为完整研究", 422)
        with Session(self.engine.database) as session, session.begin():
            row = self._row(session, actor, identifier, lock=True)
            if row.prediction_hash is not None:
                raise DomainError("history_prediction_frozen", "固定预测不能覆盖", 409)
            row.payload = payload
            row.state = state
            row.prediction_hash = prediction_hash

    def _adopt(self, actor, collection, payload, *, purpose, identifier):
        snapshot_ids, ref_snapshots = [], {}
        evidence = payload.setdefault("evidence", {})
        for record in collection["items"]:
            if (record["record_kind"] == "outcome") is (purpose == "outcomes"):
                evidence[record["version_id"]] = record
        selected = [
            r for r in collection["items"] if (r["record_kind"] == "outcome") is (purpose == "outcomes")
        ]
        maximum = min(self.engine.settings.engine_max_records, 5000)
        for start in range(0, len(selected), maximum):
            segment = {**collection, "items": selected[start : start + maximum]}
            snapshot = self.reader.snapshot(segment, outcomes=purpose == "outcomes")
            if snapshot is None:
                continue
            _, adopted = self.engine.adopt_snapshot(
                actor, snapshot, "history:" + identifier + ":" + canonical_hash(snapshot)[:64]
            )
            binding = {
                "snapshot_id": adopted["id"],
                "snapshot_hash": adopted["snapshot_hash"],
                "purpose": purpose,
                "cutoff": collection["cutoff"].isoformat(),
                "source_id": collection["source_id"],
                "is_demo": collection["is_demo"],
                "coverage": collection["coverage"],
            }
            if not any(b["snapshot_id"] == adopted["id"] for b in payload["snapshots"]):
                payload["snapshots"].append(binding)
            snapshot_ids.append(adopted["id"])
            for record in snapshot.records:
                ref_snapshots[record.record_ref.version_key] = adopted["id"]
        observation = self.reader.observation(
            collection,
            snapshot_id=snapshot_ids[0] if snapshot_ids else None,
            snapshot_hash=next(
                (b["snapshot_hash"] for b in payload["snapshots"] if b["snapshot_id"] in snapshot_ids), None
            ),
        )
        return observation, ref_snapshots

    @staticmethod
    def _append(runtime, actor, view, kind, key, **fields):
        body = InvestigationEntryCreate.model_validate(
            {
                "expected_revision": view["revision"],
                "kind": kind,
                **fields,
            }
        )
        return runtime.append(actor, view["id"], body, key)[1]

    def create(self, actor, request, key):
        self._authorize(actor, request.source_id, request.is_demo)
        self._check_online_source(request.source_id)
        now = datetime.now(UTC)
        if request.cutoff > now or any(
            horizon_end(t, request.horizon_months) > request.cutoff
            for t in (*request.training_cutoffs, *(r.cutoff for r in request.training_runs))
        ):
            raise DomainError("history_time_invalid", "截止时间不得在未来，训练窗口须已成熟", 422)
        created = False
        with Session(self.engine.database) as session, session.begin():

            def mutate():
                nonlocal created
                created = True
                row = HistoricalStudyRecord(
                    id=str(uuid4()),
                    workspace_id=actor.workspace_id,
                    created_by=actor.actor_id,
                    source_id=request.source_id,
                    is_demo=request.is_demo,
                    state="collecting",
                    request=request.model_dump(mode="json"),
                    payload={"compute_version": HISTORY_COMPUTE_VERSION, "snapshots": []},
                )
                session.add(row)
                session.flush()
                return 201, {"id": row.id}

            _, accepted = self.engine._idempotent(
                session, actor, "history.study.create", key, request, mutate
            )
        identifier = accepted["id"]
        if not created:
            return self.get(actor, identifier)
        payload = {
            "compute_version": HISTORY_COMPUTE_VERSION,
            "snapshots": [],
            "queries": [],
            "training": [],
            "predictions": [],
            "limitations": [
                "declared_source_scope_not_all_global_drug_programs",
                "modern_model_and_professional_assessment_not_connected",
                "this_reconstruction_does_not_claim_the_system_ran_at_historical_cutoff",
                "followups_search_fabric_adopted_records_not_a_new_whole_source_fetch",
            ],
            "next_questions": [
                "核实未决项目谱系、适应证、地区及映射当时是否可见。",
                "补齐每个项目的独立结局与明确覆盖窗口；未取得不能补失败。",
            ],
        }
        investigations = InvestigationService(self.engine)
        _, view = investigations.create(
            actor,
            InvestigationCreate(
                title=request.title,
                question=request.question,
                is_demo=request.is_demo,
                questions=payload["next_questions"],
            ),
            "history:" + identifier,
        )
        with Session(self.engine.database) as session, session.begin():
            row = self._row(session, actor, identifier, lock=True)
            row.investigation_id = view["id"]
        try:
            collection = self.reader.collect(
                source_id=request.source_id,
                is_demo=request.is_demo,
                mode=request.mode,
                cutoff=request.cutoff,
                terms=request.cohort_terms,
                run_id=request.run_id,
            )
            cohort, ref_snapshots = self._adopt(
                actor, collection, payload, purpose="features", identifier=identifier
            )
            payload["cohort"] = cohort.model_dump(mode="json")
            payload["queries"].append(
                {
                    "purpose": "declared_cohort",
                    "terms": list(request.cohort_terms),
                    "cutoff": request.cutoff.isoformat(),
                    "records": len(collection["items"]),
                    "coverage": collection["coverage"],
                    "excluded_counts": collection["excluded_counts"],
                }
            )
            self._save(actor, identifier, payload)
            # Existing methods consume only the qualified feature view, never outcome records.
            feature_snapshot = self.reader.snapshot(collection)
            if feature_snapshot is not None:
                mining = run_mining(
                    feature_snapshot,
                    MiningPolicy(
                        as_of=request.cutoff,
                        time_mode="publication_retrospective",
                        method_ids=("lexical_emergence", "text_topics", "counterevidence_impact"),
                        limits=ResourceLimits(review_capacity=min(30, request.review_budget + 10)),
                    ),
                )
                payload["mining"] = mining.model_dump(mode="json")
                payload["next_questions"].extend(
                    q for candidate in mining.candidates[:3] for q in candidate.next_questions[:1]
                )
            # Fixed literal identifiers, from actual source fields, with exact original locators.
            followups = []
            seen_terms = {t.casefold() for t in request.cohort_terms}
            for record in collection["items"]:
                if record["record_kind"] == "outcome" or record.get("raw_media_type") != "application/json":
                    continue
                for field in (
                    "application_numbers",
                    "interventions",
                    "trial_id",
                    "pmid",
                    "doi",
                    "program_id",
                ):
                    value = record["facts"].get(field)
                    values = value if isinstance(value, list) else [value]
                    for index, literal in enumerate(values):
                        if isinstance(literal, dict):
                            literal = literal.get("name")
                        if (
                            not isinstance(literal, str)
                            or not 2 <= len(literal) <= 120
                            or literal.casefold() in seen_terms
                        ):
                            continue
                        seen_terms.add(literal.casefold())
                        followups.append(
                            {
                                "term": literal,
                                "source_record_id": record["source_record_id"],
                                "version_id": record["version_id"],
                                "field": field,
                                "field_locator": record["field_locators"].get(field),
                                "index": index,
                                "reason": "从首轮真实字段取得原样标识，补查相关材料；不认定对象已对齐。",
                            }
                        )
            for followup in followups[: request.followup_queries]:
                found = self.reader.collect(
                    source_id=request.source_id,
                    is_demo=request.is_demo,
                    mode=request.mode,
                    cutoff=request.cutoff,
                    terms=(followup["term"],),
                )
                _, refs = self._adopt(actor, found, payload, purpose="features", identifier=identifier)
                ref_snapshots.update(refs)
                payload["queries"].append(
                    {
                        "purpose": "source_derived_followup",
                        "scope": "fabric_adopted_source_versions_literal_search",
                        "derivation": followup,
                        "terms": [followup["term"]],
                        "cutoff": request.cutoff.isoformat(),
                        "records": len(found["items"]),
                        "coverage": found["coverage"],
                        "new_versions": sum(
                            r["version_id"] not in {x["version_id"] for x in collection["items"]}
                            for r in found["items"]
                        ),
                    }
                )
                for record in found["items"]:
                    if all(r["version_id"] != record["version_id"] for r in collection["items"]):
                        collection["items"].append(record)
                self._save(actor, identifier, payload)
            payload["fact_summary"] = summarize_records(collection["items"])
            payload["next_questions"].extend(payload["fact_summary"]["next_questions"])
            self._save(actor, identifier, payload)
            training = []
            landmarks = [(t, None) for t in request.training_cutoffs] + [
                (r.cutoff, r.run_id) for r in request.training_runs
            ]
            labels_cutoff = request.cutoff
            if landmarks:
                labels = self.reader.collect(
                    source_id=request.source_id,
                    is_demo=request.is_demo,
                    mode=request.mode,
                    cutoff=labels_cutoff,
                    record_kind="outcome",
                )
                label_observation, _ = self._adopt(
                    actor, labels, payload, purpose="outcomes", identifier=identifier
                )
            for cutoff, training_run_id in sorted(landmarks):
                features = self.reader.collect(
                    source_id=request.source_id,
                    is_demo=request.is_demo,
                    mode=request.mode,
                    cutoff=cutoff,
                    terms=request.cohort_terms,
                    run_id=training_run_id,
                )
                feature_observation, _ = self._adopt(
                    actor, features, payload, purpose="features", identifier=identifier
                )
                training.append((feature_observation, label_observation))
                payload["training"].append(
                    {
                        "feature_cutoff": cutoff.isoformat(),
                        "label_cutoff": labels_cutoff.isoformat(),
                        "run_id": training_run_id,
                        "feature_records": feature_observation.record_count,
                        "label_records": label_observation.record_count,
                        "feature_coverage": features["coverage"],
                        "label_coverage": labels["coverage"],
                    }
                )
                self._save(actor, identifier, payload)
            predictions, gaps = make_predictions(request, cohort, training)
            payload["predictions"] = [p.model_dump(mode="json") for p in predictions]
            payload["limitations"].extend(gaps)
            payload["limitations"].extend(cohort.gaps)
            payload["next_questions"] = list(dict.fromkeys(payload["next_questions"]))[:32]
            # Material count is an explicit review budget, not the cohort denominator.
            for record in [r for r in collection["items"] if r["record_kind"] != "outcome"][
                : min(12, request.review_budget)
            ]:
                ref = self.reader.ref(record)
                snapshot_id = ref_snapshots.get(ref.version_key)
                if snapshot_id is None:
                    continue
                view = self._append(
                    investigations,
                    actor,
                    view,
                    "material",
                    "history:" + identifier + ":" + record["version_id"],
                    snapshot_id=snapshot_id,
                    record_ref=ref.model_dump(mode="json"),
                    quote=None,
                    stance="context",
                    text="程序采用资料，尚未作支持或反面专业判断：" + record["title"][:7500],
                )
            grouping = (
                "经审核项目谱系分组"
                if request.unit_kind == "program"
                else "仅试验登记ID分组，不代表药物项目谱系隔离"
            )
            view = self._append(
                investigations,
                actor,
                view,
                "note",
                "history:" + identifier + ":summary",
                text=(
                    f"历史资料研究；模式{request.mode}；目标{request.target}；"
                    f"声明队列{len(predictions)}个单位，"
                    f"可计算概率{sum(p.probability is not None for p in predictions)}个。"
                    f"预测采用已公开成熟的历史事件率；{grouping}。当前没有自动专业判断。"
                    "查询、预算、出处和未知详见对应历史研究记录。"
                ),
            )
            self._append(
                investigations,
                actor,
                view,
                "status",
                "history:" + identifier + ":pause",
                status="paused",
                text="固定预测已保存，等待独立结局或研究人员核实。",
            )
            valid = sum(p.probability is not None for p in predictions)
            state = (
                "predicted" if valid and valid == len(predictions) else "partial" if valid else "unsupported"
            )
            fixed_hash = canonical_hash(
                {
                    "compute_version": HISTORY_COMPUTE_VERSION,
                    "request": request.model_dump(mode="json"),
                    "predictions": [p.model_dump(mode="json") for p in predictions],
                    "cohort": cohort.model_dump(mode="json"),
                    "training": payload["training"],
                    "sources": payload["snapshots"],
                }
            )
            self._save(actor, identifier, payload, state=state, prediction_hash=fixed_hash)
        except DomainError as exc:
            if exc.code == "history_result_budget":
                # Keep the last successful persisted progress, rather than
                # attempting the same oversized write again.
                with Session(self.engine.database) as session:
                    payload = dict(self._row(session, actor, identifier).payload)
            payload.setdefault("limitations", []).append(exc.code)
            self._save(actor, identifier, payload, state="partial" if payload["snapshots"] else "failed")
            # A PostgreSQL error is deliberately not caught/retried.
        return self.get(actor, identifier)

    def evaluate(self, actor, identifier, request, key):
        self.get(actor, identifier)  # current permission once, before any write lock
        created = False
        with Session(self.engine.database) as session, session.begin():
            study = self._row(session, actor, identifier)
            if not study.prediction_hash:
                raise DomainError("history_prediction_unavailable", "尚无可固定评价的预测", 409)
            feature_request = HistoryStudyCreate.model_validate(study.request)
            if request.cutoff > datetime.now(UTC) or request.cutoff <= feature_request.cutoff:
                raise DomainError("history_time_invalid", "评价截止须在预测之后且不得在未来", 422)

            def mutate():
                nonlocal created
                created = True
                row = HistoricalEvaluationRecord(
                    id=str(uuid4()),
                    study_id=identifier,
                    created_by=actor.actor_id,
                    state="collecting",
                    cutoff=request.cutoff,
                    prediction_hash=study.prediction_hash,
                    payload={},
                )
                session.add(row)
                session.flush()
                return 201, {"id": row.id}

            _, accepted = self.engine._idempotent(
                session, actor, "history.evaluate:" + identifier, key, request, mutate
            )
            frozen = dict(study.payload)
            prediction_hash = study.prediction_hash
        if not created:
            return self.evaluation(actor, identifier, accepted["id"])
        payload = {
            "snapshots": [],
            "prediction_hash": prediction_hash,
            "compute_version": HISTORY_COMPUTE_VERSION,
        }
        state, result_hash = "failed", None
        try:
            collection = self.reader.collect(
                source_id=feature_request.source_id,
                is_demo=feature_request.is_demo,
                mode=feature_request.mode,
                cutoff=request.cutoff,
                record_kind="outcome",
            )
            observed, _ = self._adopt(
                actor, collection, payload, purpose="outcomes", identifier=accepted["id"]
            )
            predictions = tuple(HistoryPrediction.model_validate(p) for p in frozen.get("predictions", []))
            outcomes, metrics = score_predictions(predictions, observed, feature_request.review_budget)
            payload.update(
                outcomes=[o.model_dump(mode="json") for o in outcomes],
                metrics=metrics.model_dump(mode="json"),
                coverage=collection["coverage"],
                observation=observed.model_dump(mode="json"),
                limitations=list(metrics.limitations),
            )
            result_hash = canonical_hash(payload)
            state = (
                "completed"
                if metrics.evaluable_count == metrics.predicted_count and metrics.predicted_count
                else "partial"
            )
        except DomainError as exc:
            payload["limitations"] = [exc.code]
        with Session(self.engine.database) as session, session.begin():
            row = session.get(HistoricalEvaluationRecord, accepted["id"], with_for_update=True)
            if row.state != "collecting" or row.result_hash is not None:
                raise DomainError("history_evaluation_frozen", "已有评价版本不能覆盖", 409)
            row.state, row.payload, row.result_hash = state, payload, result_hash
        return self.evaluation(actor, identifier, accepted["id"])

    def evaluation(self, actor, identifier, evaluation_id):
        with Session(self.engine.database) as session:
            study = self._row(session, actor, identifier)
            self._check_online_source(study.source_id)
            row = session.scalar(
                select(HistoricalEvaluationRecord).where(
                    HistoricalEvaluationRecord.id == evaluation_id,
                    HistoricalEvaluationRecord.study_id == identifier,
                )
            )
            if row is None:
                raise DomainError("not_found", "未找到可访问的评价版本", 404)
            for binding in row.payload.get("snapshots", []):
                self.engine._snapshot(session, actor, binding["snapshot_id"])
            return {
                "id": row.id,
                "study_id": identifier,
                "state": row.state,
                "created_at": row.created_at.isoformat(),
                "cutoff": row.cutoff.isoformat(),
                "prediction_hash": row.prediction_hash,
                "result_hash": row.result_hash,
                **row.payload,
            }

    def evidence(self, actor, identifier, version_id, evaluation_id=None):
        with Session(self.engine.database) as session:
            row = self._row(session, actor, identifier)
            self._check_online_source(row.source_id)
            payload = row.payload
        if evaluation_id is not None:
            payload = self.evaluation(actor, identifier, evaluation_id)
        evidence = payload.get("evidence", {}).get(version_id)
        if evidence is None:
            raise DomainError("not_found", "本研究未采用该原件版本", 404)
        return evidence

    def export(self, actor, identifier):
        view = self.get(actor, identifier)
        lines = [
            "# " + view.title,
            "",
            view.request.question,
            "",
            f"模式：{view.request.mode}；资料截止：{view.cutoff.isoformat()}。",
            f"目标：{view.target}；窗口：{view.request.horizon_months}个月；状态：{view.state}。",
            f"固定预测：{view.prediction_hash or '未取得'}。",
            "",
            "程序历史基线；不是临床疗效评价、投资承诺或专业发布。",
            "",
            "|单位|历史事件率|训练谱系数|资格/限制|",
            "|---|---:|---:|---|",
        ]
        for prediction in view.predictions:
            label = prediction.unit.label.replace("|", "/").replace("\n", " ")
            value = "资格不足" if prediction.probability is None else f"{prediction.probability:.4f}"
            lines.append(f"|{label}|{value}|{prediction.training_units}|{'、'.join(prediction.reasons)}|")
        lines.extend(["", "## 实际采用的程序事实整理（非专业判断）", ""])
        summary = view.fact_summary or {}
        lines.append(
            f"展示{summary.get('displayed_versions', 0)}个版本，"
            f"采用范围内有{summary.get('available_versions', 0)}个版本；"
            f"展示截断：{summary.get('truncated', False)}。"
        )
        for fact in summary.get("rows", []):
            lines.extend(
                [
                    "",
                    "### " + fact["source_record_id"],
                    fact["title"],
                    "",
                    "来源：" + str(fact.get("source_url") or "原站未提供精确详情URL"),
                    "采用版本：" + fact["version_id"],
                    "原件SHA256：" + fact["raw_sha256"],
                    "冻结原件入口：/api/engine/history/studies/"
                    + view.id
                    + "/evidence/"
                    + fact["version_id"],
                    "边界：" + fact["boundary"],
                    "",
                ]
            )
            for name, field in fact["fields"].items():
                value = json.dumps(field["value"], ensure_ascii=False)
                locator = json.dumps(field["locator"], ensure_ascii=False)
                lines.append("- " + name + " = " + value + "；原件定位：" + locator)
            excerpt = fact.get("source_field_excerpt")
            if excerpt:
                lines.extend(
                    [
                        "",
                        "来源字段节选（非全文）：",
                        excerpt["text"],
                        "节选字段定位：" + json.dumps(excerpt["locator"], ensure_ascii=False),
                        "节选截断：" + str(excerpt["truncated"]),
                    ]
                )
            if fact["missing_fields"]:
                lines.append("当前投影缺字段（不等于事实不存在）：" + "、".join(fact["missing_fields"]))
        lines.extend(["", "## 实际查询与范围", ""])
        for query in view.queries:
            lines.append("- " + json.dumps(query, ensure_ascii=False))
        lines.extend(["", "独立评价版本："])
        for evaluation in view.evaluations:
            lines.append(f"- {evaluation['id']}：{evaluation['state']}；{evaluation['cutoff']}")
        lines.extend(["", "待核问题：", *("- " + q for q in view.next_questions)])
        lines.extend(["", "限制：", *("- " + reason for reason in view.limitations)])
        return "\n".join(lines) + "\n"
